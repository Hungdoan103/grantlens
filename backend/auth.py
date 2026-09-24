"""auth.py — xác thực + phiên đăng nhập. Chỉ dùng thư viện chuẩn (không thêm phụ thuộc).

Vì sao cần: trước đây tên cán bộ và vai trò do TRÌNH DUYỆT tự gửi lên trong nội dung request -> ai gõ đúng tên
một quản lý là qua mọi cổng, và nhật ký chuỗi băm chỉ chống SỬA SAU KHI GHI chứ không chống MẠO DANH.
Từ bản này: danh tính và vai trò lấy từ PHIÊN phía máy chủ; mọi trường officer/role trong request bị GHI ĐÈ
(xem IdentityMiddleware) nên không endpoint nào còn tin dữ liệu tự khai.

  • Mật khẩu: PBKDF2-HMAC-SHA256, 240.000 vòng, muối riêng từng tài khoản — lưu trong data/officers.json.
  • Phiên: cookie httpOnly + SameSite=Strict, nội dung ký HMAC-SHA256 bằng khoá máy chủ (data/.secret hoặc
    biến GRANTLENS_SECRET), hết hạn sau GRANTLENS_SESSION_HOURS giờ (mặc định 8).
  • Chống dò mật khẩu: sai 5 lần -> khoá tài khoản đó 5 phút.
  • GRANTLENS_AUTH=off tắt xác thực (CHỈ cho kiểm thử tự động); /api/meta báo trạng thái để UI cảnh báo đỏ.

CLI:
  python -m backend.auth list
  python -m backend.auth set-password "<tên cán bộ | username>" "<mật khẩu>"
  python -m backend.auth init-demo        tạo username + mật khẩu ngẫu nhiên cho tài khoản chưa có, ghi ra
                                          data/demo-accounts.txt (file này KHÔNG đưa vào git)
"""
import base64, hashlib, hmac, json, os, re, secrets, sys, time, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OFFICERS = ROOT / "data" / "officers.json"
SECRET_FILE = ROOT / "data" / ".secret"
DEMO_FILE = ROOT / "data" / "demo-accounts.txt"

ENABLED = os.environ.get("GRANTLENS_AUTH", "on").lower() != "off"
SESSION_HOURS = float(os.environ.get("GRANTLENS_SESSION_HOURS", "8"))
COOKIE = "gl_session"
PBKDF2_ROUNDS = 240_000
MAX_FAILS, LOCK_SECONDS = 5, 300
ROLES = ("officer", "manager", "auditor")

# Đường dẫn không cần phiên: trang chủ, đăng nhập, thông tin hệ thống (để màn đăng nhập hiển thị trạng thái).
PUBLIC_PATHS = {"/", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/meta", "/favicon.ico", "/api/i18n/en"}

# THANH TRA (auditor) chỉ ĐỌC + HẬU KIỂM: mọi thao tác ghi khác bị chặn ngay ở middleware (một chỗ duy nhất,
# không phụ thuộc từng endpoint có nhớ kiểm hay không) — người hậu kiểm không được đồng thời là người thẩm định.
AUDITOR_WRITE_OK = {"/api/post-audit", "/api/auth/logout", "/api/screening/lookup"}

_fails: dict = {}   # username -> [số lần sai, thời điểm bị khoá tới]


# ---------------- khoá máy chủ ----------------
def _secret() -> bytes:
    env = os.environ.get("GRANTLENS_SECRET")
    if env:
        return env.encode("utf-8")
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


# ---------------- mật khẩu ----------------
def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


# ---------------- sổ tài khoản (data/officers.json) ----------------
def _load() -> dict:
    return json.loads(OFFICERS.read_text(encoding="utf-8"))


def _save(doc: dict):
    OFFICERS.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    from . import coi
    coi.reload()


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFD", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn").replace("đ", "d").replace("Đ", "D")
    return re.sub(r"[^a-z0-9]+", ".", s.lower()).strip(".")


def find_account(login: str):
    login = (login or "").strip().lower()
    for o in _load().get("officers", []):
        if login and login in ((o.get("username") or "").lower(), o["name"].lower()):
            return o
    return None


def set_password(login: str, password: str) -> dict:
    if len(password) < 8:
        raise ValueError("Mật khẩu tối thiểu 8 ký tự")
    doc = _load()
    for o in doc["officers"]:
        if login.strip().lower() in ((o.get("username") or "").lower(), o["name"].lower()):
            o.setdefault("username", _slug(o["name"]))
            o["password"] = hash_password(password)
            _save(doc)
            return {"name": o["name"], "username": o["username"], "role": o.get("role", "officer")}
    raise KeyError(f"Không có tài khoản '{login}' trong sổ cán bộ")


def init_demo() -> list:
    """Tạo username + mật khẩu ngẫu nhiên cho mọi tài khoản CHƯA có mật khẩu. Trả danh sách để in một lần."""
    doc, made = _load(), []
    for o in doc["officers"]:
        o.setdefault("username", _slug(o["name"]))
        if not o.get("password"):
            pw = secrets.token_urlsafe(8)
            o["password"] = hash_password(pw)
            made.append({"name": o["name"], "username": o["username"], "role": o.get("role", "officer"), "password": pw})
    _save(doc)
    if made:
        lines = ["# Tài khoản demo GrantLens — sinh tự động, ĐỔI MẬT KHẨU trước khi triển khai thật.",
                 "# File này không đưa vào git. Đổi mật khẩu: python -m backend.auth set-password <username> <mật khẩu>", ""]
        lines += [f"{m['role']:8s}  {m['username']:24s}  {m['password']:14s}  ({m['name']})" for m in made]
        old = DEMO_FILE.read_text(encoding="utf-8") if DEMO_FILE.exists() else ""
        DEMO_FILE.write_text((old + "\n" if old else "") + "\n".join(lines) + "\n", encoding="utf-8")
    return made


# ---------------- đăng nhập ----------------
class AuthError(Exception):
    def __init__(self, msg, code=401):
        super().__init__(msg)
        self.code = code


def login(username: str, password: str) -> dict:
    key = (username or "").strip().lower()
    n, locked_until = _fails.get(key, [0, 0])
    if time.time() < locked_until:
        raise AuthError(f"Tài khoản tạm khoá do đăng nhập sai nhiều lần — thử lại sau {int(locked_until - time.time())} giây", 429)
    acc = find_account(username)
    # luôn chạy phép băm kể cả khi không có tài khoản -> thời gian phản hồi không lộ tên nào tồn tại
    ok = verify_password(password or "", (acc or {}).get("password") or hash_password("x", b"0" * 16))
    if not acc or not acc.get("password") or not ok:
        n += 1
        _fails[key] = [0, time.time() + LOCK_SECONDS] if n >= MAX_FAILS else [n, 0]
        raise AuthError("Sai tên đăng nhập hoặc mật khẩu")
    _fails.pop(key, None)
    role = acc.get("role", "officer")
    return {"name": acc["name"], "username": acc.get("username"), "role": role if role in ROLES else "officer"}


# ---------------- phiên (cookie ký HMAC) ----------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(user: dict) -> str:
    payload = dict(user, exp=int(time.time() + SESSION_HOURS * 3600), sid=secrets.token_hex(8))
    body = _b64(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str):
    """Trả user nếu cookie hợp lệ, chưa hết hạn VÀ tài khoản vẫn còn trong sổ với đúng vai trò; ngược lại None.
    Vai trò luôn đọc lại từ sổ cán bộ -> hạ quyền/xoá tài khoản có hiệu lực ngay, không chờ cookie hết hạn."""
    try:
        body, sig = (token or "").split(".")
        good = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        p = json.loads(_unb64(body))
        if p.get("exp", 0) < time.time():
            return None
        acc = find_account(p.get("username") or p.get("name"))
        if not acc:
            return None
        role = acc.get("role", "officer")
        return {"name": acc["name"], "username": acc.get("username"), "role": role if role in ROLES else "officer",
                "sid": p.get("sid"), "exp": p.get("exp")}
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _cookie_from_scope(scope, cookie_name: str = None) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == (cookie_name or COOKIE):
                    return val
    return ""


# ---------------- middleware ASGI: ép danh tính từ phiên ----------------
class IdentityMiddleware:
    """1) /api/* (trừ PUBLIC_PATHS) không có phiên hợp lệ -> 401.
       2) Mọi request JSON: GHI ĐÈ trường officer + role bằng danh tính của phiên — endpoint nào cũng nhận tên thật,
          kể cả endpoint viết sau này quên kiểm. Request multipart đọc danh tính từ request.state.user.
       3) Gắn user vào scope['state'] để endpoint dùng (request.state.user)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        state = scope.setdefault("state", {})
        # ngôn ngữ hiển thị của request (cookie gl_lang, do nút VI|EN đặt) -> contextvar cho lớp dịch đầu ra
        from . import i18n
        i18n.set_lang(_cookie_from_scope(scope, i18n.COOKIE))
        if not ENABLED:
            state["user"] = None
            return await self.app(scope, receive, send)
        user = read_session(_cookie_from_scope(scope))
        state["user"] = user
        if path.startswith("/api/") and path not in PUBLIC_PATHS and not user:
            body = json.dumps({"detail": i18n.tr("Chưa đăng nhập hoặc phiên đã hết hạn"), "need_login": True},
                              ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})

        if user and user["role"] == "auditor" and scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") \
                and path.startswith("/api/") and path not in AUDITOR_WRITE_OK:
            body = json.dumps({"detail": i18n.tr(f"Tài khoản thanh tra '{user['name']}' chỉ được xem và hậu kiểm — "
                                                 "không thẩm định, không ký, không phê chuẩn")}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})

        ctype = next((v for k, v in scope.get("headers", []) if k == b"content-type"), b"")
        if user and scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") and ctype.startswith(b"application/json") \
                and path not in PUBLIC_PATHS:
            raw, more = b"", True
            while more:
                msg = await receive()
                if msg["type"] != "http.request":
                    break
                raw += msg.get("body", b"")
                more = msg.get("more_body", False)
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                data["officer"], data["role"] = user["name"], user["role"]
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
            headers.append((b"content-length", str(len(raw)).encode()))
            scope = dict(scope, headers=headers)
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


def current_user(request, fallback_name: str = None, fallback_role: str = "officer") -> dict:
    """Danh tính của request: từ phiên; chỉ khi xác thực TẮT (kiểm thử) mới dùng giá trị tự khai."""
    u = getattr(request.state, "user", None)
    if u:
        return u
    if ENABLED:
        raise AuthError("Chưa đăng nhập")
    return {"name": fallback_name or "Cán bộ", "role": fallback_role, "username": None}


def require_role(request, *roles, fallback_name: str = None):
    """Khoá vai trò ở cổng. Khi xác thực TẮT thì tra vai trò theo sổ cán bộ như trước (kiểm thử)."""
    u = getattr(request.state, "user", None)
    if u:
        if u["role"] not in roles:
            raise AuthError(f"Tài khoản '{u['name']}' có vai trò '{u['role']}' — thao tác này cần: {', '.join(roles)}", 403)
        return u
    if ENABLED:
        raise AuthError("Chưa đăng nhập")
    from . import coi
    role = coi.role_of(fallback_name or "")
    if role not in roles:
        raise AuthError(f"'{fallback_name}' không có vai trò {', '.join(roles)} trong sổ cán bộ", 403)
    return {"name": fallback_name, "role": role, "username": None}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["list"]:
        for o in _load()["officers"]:
            print(f"{o.get('role', 'officer'):8s} {o.get('username') or '-':24s} {'có mật khẩu' if o.get('password') else 'CHƯA có mật khẩu':16s} {o['name']}")
    elif args[:1] == ["set-password"] and len(args) == 3:
        print("Đã đặt mật khẩu:", set_password(args[1], args[2]))
    elif args[:1] == ["init-demo"]:
        made = init_demo()
        print(f"Đã tạo {len(made)} tài khoản demo -> {DEMO_FILE}" if made else "Mọi tài khoản đã có mật khẩu.")
        for m in made:
            print(f"  {m['role']:8s} {m['username']:24s} {m['password']}")
    else:
        print(__doc__)
