"""E2E: xác thực + phân quyền + hậu kiểm + chốt nhất quán. Mock LLM, DB tạm, KHÔNG đụng dữ liệu thật.
Tài khoản test dùng sổ cán bộ thật nhưng mật khẩu đặt qua biến tạm (không ghi file): ta vá auth._load trong bộ nhớ."""
import os, sys, json, tempfile, copy
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="grantlens-auth-"))
os.environ["GRANTLENS_LLM"] = "mock"
os.environ["GRANTLENS_DB"] = str(TMP / "test.db")
os.environ["GRANTLENS_FEEDBACK"] = "off"
os.environ["GRANTLENS_SECRET"] = "test-secret-khong-dung-that"
os.environ["GRANTLENS_AUTH"] = "on"
ROOT = r"E:/AI/project_earn_money/FILE_CHUA_NEN/grantlens-qwen"
sys.path.insert(0, ROOT); os.chdir(ROOT)

from backend import auth, core, store, workflow
# sổ tài khoản trong bộ nhớ: không ghi vào data/officers.json
_book = json.loads(auth.OFFICERS.read_text(encoding="utf-8"))
PW = {"nguyen.van.an": "matkhau-an-123", "tran.thi.binh": "matkhau-binh-123",
      "pham.van.quyet": "matkhau-quyet-123", "le.minh.chau": "matkhau-chau-123"}
for o in _book["officers"]:
    if o.get("username") in PW:
        o["password"] = auth.hash_password(PW[o["username"]])
auth._load = lambda: copy.deepcopy(_book)

from fastapi.testclient import TestClient
from backend.app import app

OK, FAIL = [0], [0]
def chk(name, cond):
    print(("  [OK]   " if cond else "  [FAIL] ") + name); (OK if cond else FAIL)[0] += 1

def client(user=None):
    c = TestClient(app); c.__enter__()
    if user:
        r = c.post("/api/auth/login", json={"username": user, "password": PW[user]})
        assert r.status_code == 200, r.text
    return c

print("=== 1. chưa đăng nhập ===")
anon = client()
chk("GET /api/cases không phiên -> 401", anon.get("/api/cases").status_code == 401)
chk("POST confirm không phiên -> 401", anon.post("/api/cases/HS-01/confirm", json={"officer": "Pham Van Quyet", "role": "manager", "rule_id": "R01", "verdict": "met"}).status_code == 401)
chk("/api/meta và /api/auth/me vẫn mở (để hiện màn đăng nhập)", anon.get("/api/meta").status_code == 200 and anon.get("/api/auth/me").json()["user"] is None)
chk("sai mật khẩu -> 401", anon.post("/api/auth/login", json={"username": "nguyen.van.an", "password": "sai"}).status_code == 401)
chk("tên không tồn tại -> 401 (cùng thông báo, không lộ tên nào có thật)",
    anon.post("/api/auth/login", json={"username": "khong.co", "password": "x"}).json()["detail"] ==
    anon.post("/api/auth/login", json={"username": "tran.thi.binh", "password": "x"}).json()["detail"])
for _ in range(5):
    r = anon.post("/api/auth/login", json={"username": "james.miller", "password": "sai"})
chk("sai 5 lần -> khoá tạm (429)", anon.post("/api/auth/login", json={"username": "james.miller", "password": "sai"}).status_code == 429)

print("=== 2. cookie giả mạo / sửa vai trò ===")
an = client("nguyen.van.an")
tok = an.cookies.get(auth.COOKIE)
body, sig = tok.split(".")
forged = json.loads(auth._unb64(body)); forged.update(name="Pham Van Quyet", username="pham.van.quyet", role="manager")
bad = auth._b64(json.dumps(forged).encode()) + "." + sig
fake = TestClient(app); fake.__enter__(); fake.cookies.set(auth.COOKIE, bad)
chk("cookie bị sửa nội dung (giữ chữ ký cũ) -> 401", fake.get("/api/cases").status_code == 401)
chk("phiên hợp lệ -> /me trả đúng người + vai trò", an.get("/api/auth/me").json()["user"] == {"name": "Nguyen Van An", "username": "nguyen.van.an", "role": "officer"})

print("=== 3. tự khai danh tính trong request bị GHI ĐÈ ===")
CASE = "HS-01"
with an.stream("POST", f"/api/cases/{CASE}/assess") as r:
    for _ in r.iter_lines(): pass
r = an.post(f"/api/cases/{CASE}/start-review", json={"officer": "Pham Van Quyet", "role": "manager"})
chk("start-review OK", r.status_code == 200)
c = store.get_case(CASE, with_text=False)
chk("hồ sơ ghi cán bộ = người ĐĂNG NHẬP, không phải tên tự khai", c["officer"] == "Nguyen Van An")
ev = store.audit_events(CASE, limit=5)
chk("nhật ký ghi actor thật + vai trò thật", all(e["actor"] in ("Nguyen Van An", "AI", "Hệ thống") for e in ev) and
    any(e["actor"] == "Nguyen Van An" and e["role"] == "officer" for e in ev))

print("=== 4. cán bộ thường không qua được 4 cổng quản lý ===")
chk("phê chuẩn bộ tiêu chí -> 403", an.post("/api/rulesets/au-demo-fund/approve", json={"officer": "Pham Van Quyet", "role": "manager"}).status_code == 403)
chk("phê chuẩn nhãn bộ test -> 403", an.post("/api/rulesets/au-demo-fund/testset/approve", json={"officer": "Pham Van Quyet", "role": "manager"}).status_code == 403)
chk("ký cấp 2 -> 403", an.post(f"/api/cases/{CASE}/countersign", json={"officer": "Pham Van Quyet", "role": "manager"}).status_code == 403)
chk("rút mẫu hậu kiểm -> 403", an.get("/api/post-audit/sample").status_code == 403)

print("=== 5. luồng thẩm định -> ký -> hậu kiểm ===")
cv = an.get(f"/api/cases/{CASE}").json()
spot = cv["case"]["spot_rule"]
an.post(f"/api/cases/{CASE}/spot-check", json={**{"officer": "x"}, "verdict": "met"})
text = store.get_case(CASE)["text"]
sents = [s.strip() for s in text.replace("\n", " ").split(". ") if len(s.split()) >= 9]
used = 0
for v in store.get_verdicts(CASE):
    payload = {"officer": "x", "rule_id": v["rule_id"], "verdict": "met", "reason": ""}
    if v["ai_verdict"] != "met" or (v.get("guard_level") in ("needs-manual-guard", "llm-only")) or "FALSE-PASS" in (v.get("note") or ""):
        payload["reason"] = f"Đối chiếu hồ sơ {v['rule_id']}: " + " ".join(sents[used].split()[:9]); used += 1
    r = an.post(f"/api/cases/{CASE}/confirm", json=payload)
    assert r.status_code == 200, (v["rule_id"], r.text)
r = an.post(f"/api/cases/{CASE}/sign", json={"officer": "x", "acknowledge_fast": True})
chk("ký duyệt OK", r.status_code == 200 and r.json()["status"] == "signed")
chk("mọi xác nhận ghi đúng người đăng nhập", all(v["confirmed_by"] == "Nguyen Van An" for v in store.get_verdicts(CASE)))

mg = client("pham.van.quyet")
s = mg.get("/api/post-audit/sample?n=3").json()
chk("quản lý rút được mẫu hậu kiểm", len(s["items"]) >= 1 and all(i["confirmed_by"] == "Nguyen Van An" for i in s["items"]))
chk("việc RÚT MẪU được ghi nhật ký", any("RÚT MẪU" in e["action"] for e in store.audit_events(limit=5)))
it = s["items"][0]
chk("không đồng ý mà không ghi lý do -> 400", mg.post("/api/post-audit", json={"case_id": it["case_id"], "rule_id": it["rule_id"], "outcome": "disagree"}).status_code == 400)
r = mg.post("/api/post-audit", json={"case_id": it["case_id"], "rule_id": it["rule_id"], "outcome": "disagree", "note": "đoạn trích không nói về tiêu chí này"})
chk("lưu hậu kiểm OK + thống kê cập nhật", r.status_code == 200 and r.json()["stats"]["audited"] == 1 and r.json()["stats"]["disagree"] == 1)
chk("nhật ký ghi HẬU KIỂM với tên quản lý", any("HẬU KIỂM" in e["action"] and e["actor"] == "Pham Van Quyet" for e in store.audit_events(it["case_id"], limit=5)))
chk("mẫu đã hậu kiểm không bị rút lại", all((i["case_id"], i["rule_id"]) != (it["case_id"], it["rule_id"]) for i in mg.get("/api/post-audit/sample?n=20").json()["items"]))
au = client("le.minh.chau")
chk("thanh tra (auditor) cũng rút mẫu được", au.get("/api/post-audit/sample?n=1").status_code == 200)
chk("thanh tra KHÔNG ký cấp 2 được -> 403", au.post(f"/api/cases/{CASE}/countersign", json={}).status_code == 403)
chk("thanh tra KHÔNG thẩm định được: start-review -> 403", au.post("/api/cases/HS-03/start-review", json={}).status_code == 403)
chk("thanh tra KHÔNG xác nhận tiêu chí được -> 403", au.post(f"/api/cases/{CASE}/confirm", json={"rule_id": "R01", "verdict": "met"}).status_code == 403)
chk("thanh tra KHÔNG tạo hồ sơ / chạy AI được -> 403", au.post("/api/cases/HS-03/assess").status_code == 403 and
    au.post("/api/cases", json={"applicant": "x", "text": "y " * 60}).status_code == 403)
chk("thanh tra VẪN xem được hồ sơ + nhật ký", au.get(f"/api/cases/{CASE}").status_code == 200 and au.get("/api/audit").status_code == 200)

print("=== 6. ký cấp 2: phải là quản lý, khác người thẩm định ===")
C2 = "HS-02"
with an.stream("POST", f"/api/cases/{C2}/assess") as r:
    for _ in r.iter_lines(): pass
an.post(f"/api/cases/{C2}/start-review", json={})
an.post(f"/api/cases/{C2}/spot-check", json={"verdict": "met"})
t2 = store.get_case(C2)["text"]; s2 = [x.strip() for x in t2.replace("\n", " ").split(". ") if len(x.split()) >= 9]; k = 0
for v in store.get_verdicts(C2):
    fin = v["ai_verdict"] if v["ai_verdict"] in ("met", "not_met") else "unclear"
    payload = {"rule_id": v["rule_id"], "verdict": fin, "reason": ""}
    if fin == "unclear" or (fin == "met" and v.get("guard_level") in ("needs-manual-guard", "llm-only")):
        payload["reason"] = f"Đối chiếu {v['rule_id']}: " + " ".join(s2[k].split()[:9]); k += 1
    r = an.post(f"/api/cases/{C2}/confirm", json=payload); assert r.status_code == 200, (v["rule_id"], r.text)
an.post(f"/api/cases/{C2}/sign", json={"acknowledge_fast": True})
chk("cán bộ tự ký cấp 2 -> 403", an.post(f"/api/cases/{C2}/countersign", json={}).status_code == 403)
_r = an.post(f"/api/cases/{C2}/letter", json={}); print("     letter ->", _r.status_code, _r.text[:160], "| verdicts:", [(v["rule_id"], v["final_verdict"]) for v in store.get_verdicts(C2)][:6])
chk("chưa ký cấp 2 thì không soạn được thư (428 need_countersign)", _r.status_code == 428 and _r.json().get("need_countersign"))
r = mg.post(f"/api/cases/{C2}/countersign", json={"officer": "Nguyen Van An"})
chk("quản lý ký cấp 2 OK, ghi đúng tên quản lý", r.status_code == 200 and r.json()["countersigned_by"] == "Pham Van Quyet")

print("=== 7. chốt nhất quán lượt phán quyết ===")
jc = core.judge_consistency
chk("not_met + supporting_fact=0 -> CHƯA RÕ", jc("not_met", "high", "x", 0, 3, "partial")[:2] == ("unclear", "low"))
chk("not_met + coverage=none -> CHƯA RÕ", jc("not_met", "high", "x", 1, 3, "none")[0] == "unclear")
chk("not_met + trỏ đúng dữ kiện -> GIỮ not_met", jc("not_met", "high", "x", 2, 3, "direct") == ("not_met", "high", "x", 2))
chk("met + supporting_fact=0 -> giữ met, idx hợp lệ", jc("met", "medium", "x", 0, 3, "direct") == ("met", "medium", "x", 1))
chk("supporting_fact ngoài phạm vi / None -> idx=1, không văng lỗi", jc("met", "high", "x", 9, 3, "direct")[3] == 1 and jc("unclear", "low", "x", None, 2, "partial")[3] == 1)
chk("ghi chú nêu rõ lý do hạ", "CHỐT NHẤT QUÁN" in jc("not_met", "high", "x", 0, 3, "partial")[2])

print("=== 8. tách nhiệm vụ khi phê chuẩn nhãn + đăng xuất + toàn vẹn nhật ký ===")
from backend import casegen
casegen.LBL_DIR = TMP; (TMP / "generated-au-demo-fund.json").write_text(json.dumps({"ruleset": "au-demo-fund", "cases": [
    {"id": "A", "file": "x", "target": None, "labels": {}, "status": "pending_review"},
    {"id": "B", "file": "x", "target": None, "labels": {}, "status": "pending_review"}]}), encoding="utf-8")
casegen.dispute("au-demo-fund", "B", "nhãn sai rõ ràng", by="Pham Van Quyet")
try:
    casegen.approve("au-demo-fund", "Pham Van Quyet"); chk("người đã rà nhãn tự phê chuẩn -> TỪ CHỐI", False)
except ValueError:
    chk("người đã rà nhãn tự phê chuẩn -> TỪ CHỐI", True)
chk("người khác phê chuẩn -> được", casegen.approve("au-demo-fund", "Le Minh Chau")["ok"])
an.post("/api/auth/logout")
chk("đăng xuất xong -> 401", an.get("/api/cases").status_code == 401)
chk("chuỗi băm nhật ký toàn vẹn", store.verify_chain()["ok"])
chk("không lộ hash mật khẩu qua /api/officers", "password" not in mg.get("/api/officers").text)

print(f"\n=== {OK[0]} PASS / {FAIL[0]} FAIL ===")
sys.exit(1 if FAIL[0] else 0)
