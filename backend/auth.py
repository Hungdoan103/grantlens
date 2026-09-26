"""auth.py — authentication + login sessions. Standard library only (no new dependencies).

Why it is needed: previously the officer name and role were sent by the BROWSER inside the request body -> anyone who
typed a manager's name passed every gate, and the hash-chained audit log only protects against EDITING AFTER THE FACT,
not against IMPERSONATION. From this version identity and role come from the server-side SESSION; every officer/role
field in a request is OVERWRITTEN (see IdentityMiddleware), so no endpoint trusts self-declared data any more.

  • Passwords: PBKDF2-HMAC-SHA256, 240,000 rounds, per-account salt — stored in data/officers.json.
  • Session: httpOnly + SameSite=Strict cookie whose payload is signed with HMAC-SHA256 using the server key
    (data/.secret or the GRANTLENS_SECRET variable); expires after GRANTLENS_SESSION_HOURS hours (default 8).
  • Brute-force protection: 5 failures -> that account is locked for 5 minutes.
  • GRANTLENS_AUTH=off disables authentication (ONLY for automated tests); /api/meta reports it so the UI shows a red banner.

CLI:
  python -m backend.auth list
  python -m backend.auth set-password "<officer name | username>" "<password>"
  python -m backend.auth init-demo        create a username + random password for every account without one; written to
                                          data/demo-accounts.txt (this file is NOT committed to git)
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

# Paths that need no session: home page, login, system info (so the login screen can show the status), UI locale.
PUBLIC_PATHS = {"/", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/meta", "/favicon.ico",
                "/api/i18n/vi", "/api/i18n/en"}

# AUDITORS only READ + POST-AUDIT: every other write is blocked right in the middleware (one place, independent of
# whether each endpoint remembers to check) — the person who post-audits may not also be the reviewer.
AUDITOR_WRITE_OK = {"/api/post-audit", "/api/auth/logout", "/api/screening/lookup"}

_fails: dict = {}   # username -> [failure count, locked-until timestamp]


# ---------------- server key ----------------
def _secret() -> bytes:
    env = os.environ.get("GRANTLENS_SECRET")
    if env:
        return env.encode("utf-8")
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


# ---------------- passwords ----------------
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


# ---------------- account register (data/officers.json) ----------------
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
        raise ValueError("Password must be at least 8 characters")
    doc = _load()
    for o in doc["officers"]:
        if login.strip().lower() in ((o.get("username") or "").lower(), o["name"].lower()):
            o.setdefault("username", _slug(o["name"]))
            o["password"] = hash_password(password)
            _save(doc)
            return {"name": o["name"], "username": o["username"], "role": o.get("role", "officer")}
    raise KeyError(f"No account '{login}' in the officer register")


def init_demo() -> list:
    """Create a username + random password for every account WITHOUT a password. Returns the list to print once."""
    doc, made = _load(), []
    for o in doc["officers"]:
        o.setdefault("username", _slug(o["name"]))
        if not o.get("password"):
            pw = secrets.token_urlsafe(8)
            o["password"] = hash_password(pw)
            made.append({"name": o["name"], "username": o["username"], "role": o.get("role", "officer"), "password": pw})
    _save(doc)
    if made:
        lines = ["# GrantLens demo accounts — generated automatically; CHANGE THE PASSWORDS before a real deployment.",
                 "# This file is not committed to git. Change a password: python -m backend.auth set-password <username> <password>", ""]
        lines += [f"{m['role']:8s}  {m['username']:24s}  {m['password']:14s}  ({m['name']})" for m in made]
        old = DEMO_FILE.read_text(encoding="utf-8") if DEMO_FILE.exists() else ""
        DEMO_FILE.write_text((old + "\n" if old else "") + "\n".join(lines) + "\n", encoding="utf-8")
    return made


# ---------------- login ----------------
class AuthError(Exception):
    def __init__(self, msg, code=401):
        super().__init__(msg)
        self.code = code


def login(username: str, password: str) -> dict:
    key = (username or "").strip().lower()
    n, locked_until = _fails.get(key, [0, 0])
    if time.time() < locked_until:
        raise AuthError(f"Account temporarily locked after repeated failed sign-ins — try again in {int(locked_until - time.time())} seconds", 429)
    acc = find_account(username)
    # always run the hash even when the account does not exist -> response time does not reveal which names exist
    ok = verify_password(password or "", (acc or {}).get("password") or hash_password("x", b"0" * 16))
    if not acc or not acc.get("password") or not ok:
        n += 1
        _fails[key] = [0, time.time() + LOCK_SECONDS] if n >= MAX_FAILS else [n, 0]
        raise AuthError("Wrong username or password")
    _fails.pop(key, None)
    role = acc.get("role", "officer")
    return {"name": acc["name"], "username": acc.get("username"), "role": role if role in ROLES else "officer"}


# ---------------- session (HMAC-signed cookie) ----------------
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
    """Return the user when the cookie is valid, unexpired AND the account still exists in the register with a role;
    otherwise None. The role is always re-read from the register -> demotion/removal takes effect immediately,
    without waiting for the cookie to expire."""
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


# ---------------- ASGI middleware: identity is enforced from the session ----------------
class IdentityMiddleware:
    """1) /api/* (except PUBLIC_PATHS) without a valid session -> 401.
       2) Every JSON request: the officer + role fields are OVERWRITTEN with the session identity — every endpoint
          receives the real name, including endpoints written later that forget to check. Multipart requests read the
          identity from request.state.user.
       3) The user is attached to scope['state'] for endpoints (request.state.user)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        state = scope.setdefault("state", {})
        # display language of the request (cookie gl_lang, set by the language switch) -> context variable for the output locale layer
        from . import i18n
        i18n.set_lang(_cookie_from_scope(scope, i18n.COOKIE))
        if not ENABLED:
            state["user"] = None
            return await self.app(scope, receive, send)
        user = read_session(_cookie_from_scope(scope))
        state["user"] = user
        public = path in PUBLIC_PATHS or path.startswith("/api/i18n/")
        if path.startswith("/api/") and not public and not user:
            body = json.dumps({"detail": i18n.tr("Not signed in or session expired"), "need_login": True},
                              ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})

        if user and user["role"] == "auditor" and scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") \
                and path.startswith("/api/") and path not in AUDITOR_WRITE_OK:
            body = json.dumps({"detail": i18n.tr(f"Auditor account '{user['name']}' may only view and post-audit — "
                                                 "no reviewing, signing or approving")}, ensure_ascii=False).encode("utf-8")
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
    """Identity of the request: from the session; only when authentication is OFF (tests) is the self-declared value used."""
    u = getattr(request.state, "user", None)
    if u:
        return u
    if ENABLED:
        raise AuthError("Not signed in")
    return {"name": fallback_name or "Officer", "role": fallback_role, "username": None}


def require_role(request, *roles, fallback_name: str = None):
    """Role lock at the gate. When authentication is OFF the role is looked up in the officer register as before (tests)."""
    u = getattr(request.state, "user", None)
    if u:
        if u["role"] not in roles:
            raise AuthError(f"Account '{u['name']}' has role '{u['role']}' — this action requires: {', '.join(roles)}", 403)
        return u
    if ENABLED:
        raise AuthError("Not signed in")
    from . import coi
    role = coi.role_of(fallback_name or "")
    if role not in roles:
        raise AuthError(f"'{fallback_name}' does not have role {', '.join(roles)} in the officer register", 403)
    return {"name": fallback_name, "role": role, "username": None}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["list"]:
        for o in _load()["officers"]:
            print(f"{o.get('role', 'officer'):8s} {o.get('username') or '-':24s} {'has password' if o.get('password') else 'NO password':16s} {o['name']}")
    elif args[:1] == ["set-password"] and len(args) == 3:
        print("Password set:", set_password(args[1], args[2]))
    elif args[:1] == ["init-demo"]:
        made = init_demo()
        print(f"Created {len(made)} demo accounts -> {DEMO_FILE}" if made else "Every account already has a password.")
        for m in made:
            print(f"  {m['role']:8s} {m['username']:24s} {m['password']}")
    else:
        print(__doc__)
