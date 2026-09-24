"""Kiểm lớp tiếng Anh: từ điển, vùng không dịch, cookie gl_lang -> JSON EN, VI mặc định không đổi, stream đánh giá,
ghi chú AI (note_en), CLI backfill (mock). Mock LLM, DB tạm, KHÔNG đụng dữ liệu thật."""
import os, sys, json, tempfile, copy
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="grantlens-i18n-"))
os.environ["GRANTLENS_LLM"] = "mock"
os.environ["GRANTLENS_DB"] = str(TMP / "test.db")
os.environ["GRANTLENS_FEEDBACK"] = "off"
os.environ["GRANTLENS_SECRET"] = "test-secret-khong-dung-that"
os.environ["GRANTLENS_AUTH"] = "on"
ROOT = r"E:/AI/project_earn_money/FILE_CHUA_NEN/grantlens-qwen"
sys.path.insert(0, ROOT); os.chdir(ROOT)

from backend import auth, i18n, i18n_en, store, workflow
_book = json.loads(auth.OFFICERS.read_text(encoding="utf-8"))
PW = {"nguyen.van.an": "matkhau-an-123", "pham.van.quyet": "matkhau-quyet-123"}
for o in _book["officers"]:
    if o.get("username") in PW:
        o["password"] = auth.hash_password(PW[o["username"]])
auth._load = lambda: copy.deepcopy(_book)

from fastapi.testclient import TestClient
from backend.app import app

OK, FAIL = [0], [0]
def chk(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   <- {extra}" if (extra and not cond) else "")); (OK if cond else FAIL)[0] += 1

VI = i18n.VI_RE

print("=== 1. engine ===")
chk("từ điển không có khoá rỗng / giá trị còn dấu tiếng Việt",
    all(k and v and not VI.search(v) for k, v in i18n_en.EXACT.items()),
    [k for k, v in i18n_en.EXACT.items() if VI.search(v)][:3])
chk("mọi pattern biên dịch được (Python)", all(i18n.re.compile(p) for p, _ in i18n_en.PATTERNS + i18n_en.PATTERNS_LAST))
chk("replacement không còn dấu tiếng Việt", all(not VI.search(r) for _, r in i18n_en.PATTERNS + i18n_en.PATTERNS_LAST))
i18n.set_lang("vi")
chk("lang=vi: tr() giữ nguyên", i18n.tr("Chưa đăng nhập") == "Chưa đăng nhập")
i18n.set_lang("en")
chk("exact", i18n.tr("Chưa đăng nhập") == "Not signed in")
chk("giữ khoảng trắng đầu/cuối", i18n.tr("  Đang thẩm định ") == "  Under review ")
chk("pattern có số", i18n.tr("Còn 2 tiêu chí chưa xác nhận: R01, R05") == "2 criteria not yet confirmed: R01, R05")
chk("pattern $$ (tiền)", i18n.tr("Tổng ngân sách 300,000$ < mức tối thiểu 400,000$ (code so số liệu)")
    == "Total budget $300,000 < minimum $400,000 (code compared figures)")
chk("cụm trong câu ghép", i18n.tr("⚠ logic dẫn xuất: rule so sánh giá trị DẪN XUẤT (phần vượt/phần đã dùng/tỷ lệ của tổng) — regex không diễn đạt được; cần guard tay hoặc để LLM + cán bộ quyết")
    .startswith("⚠ derived logic: the rule compares a DERIVED value"))
chk("nhãn viết hoa chạy sau cùng", i18n.tr("R04: cán bộ ĐẠT / AI CHƯA RÕ") == "R04: officer MET / AI UNCLEAR")
chk("cụm dài thắng nhãn viết hoa", i18n.tr("[NGHI FALSE-PASS] x — hạ xuống CHƯA RÕ, bắt buộc cán bộ quyết.")
    == "[SUSPECTED FALSE PASS] x — downgraded to UNCLEAR, officer must decide.")
chk("nhiều dòng (confirm/alert)", i18n.tr("Đã sinh 4 case (bỏ 0).\nLưu: a.json (approved=false).") == "Generated 4 cases (skipped 0).\nSaved: a.json (approved=false).")
chk("tiếng Anh giữ nguyên", i18n.tr("Nothing to translate here.") == "Nothing to translate here.")
chk("tiêu đề rule theo ruleset", i18n.tr("PI chưa có tenure (untenured)") == "PI is untenured")
chk("từ ngắn không bị thay trong câu ('Mã' trong 'Mã nguồn')",
    "ID" not in i18n.tr("Mã nguồn kiểm số liệu và không thấy gì lạ"), i18n.tr("Mã nguồn kiểm số liệu và không thấy gì lạ"))

print("=== 2. tr_obj: vùng không dịch ===")
o = i18n.tr_obj({"text": "Hồ sơ tiếng Việt nguyên văn", "quote": "Không dịch", "aq": "Không dịch", "rq": "Không dịch",
                 "officer_reason": "tôi đã đọc 'Chưa đăng nhập'", "applicant": "Nguyễn Văn Đạt",
                 "detail": "Chưa đăng nhập", "state_vi": {"new": "Mới"}, "actor": "Hệ thống",
                 "rules": [{"title_vi": "Đăng ký GST", "quote": "Đăng ký GST"}],
                 "facts": [{"fact": "Đăng ký GST", "key_phrase": "Đăng ký GST"}]})
chk("text/quote/aq/rq/officer_reason/applicant giữ nguyên",
    o["text"].startswith("Hồ sơ") and o["quote"] == o["aq"] == o["rq"] == "Không dịch"
    and o["officer_reason"].startswith("tôi") and o["applicant"] == "Nguyễn Văn Đạt", o)
chk("detail/state_vi/actor/title_vi dịch", o["detail"] == "Not signed in" and o["state_vi"]["new"] == "New"
    and o["actor"] == "System" and o["rules"][0]["title_vi"] == "Registered for GST")
chk("quote trong rule + facts giữ nguyên", o["rules"][0]["quote"] == "Đăng ký GST" and o["facts"][0]["fact"] == "Đăng ký GST")
v = i18n.tr_obj({"ai_verdict": "met", "note": "[CHẶN FALSE-PASS] x | LLM: Câu của model", "note_en": "[FALSE-PASS BLOCKED] x | LLM: Model sentence"})
chk("note_en thay note khi có", v["note"] == "[FALSE-PASS BLOCKED] x | LLM: Model sentence")
v = i18n.tr_obj({"ai_verdict": "met", "note": "[NGHI FALSE-PASS] Giá trị 3 vượt trần 2 trong rule ('x') — guard tự biên dịch từ nguyên văn — hạ xuống CHƯA RÕ, bắt buộc cán bộ quyết. | LLM: Câu của model", "note_en": None})
chk("chưa có note_en: dịch tiền tố, giữ câu model, gắn (vi)",
    v["note"].startswith("[SUSPECTED FALSE PASS] Value 3 exceeds the cap 2") and v["note"].endswith("| LLM (vi): Câu của model"), v["note"])
v = i18n.tr_obj({"ai_verdict": "met", "note": "Câu của model", "note_en": None})
chk("chưa có note_en, không tiền tố: gắn nhãn chưa dịch", v["note"].endswith("(AI note in Vietnamese — not yet translated)"))
chk("note_to_en (mock)", i18n.note_to_en("[CHẶN FALSE-PASS] x | LLM: Câu của model") == "[FALSE-PASS BLOCKED] x | LLM: [MOCK EN] Câu của model",
    i18n.note_to_en("[CHẶN FALSE-PASS] x | LLM: Câu của model"))
i18n.set_lang("vi")

print("=== 3. HTTP ===")
def client(user=None, lang=None):
    c = TestClient(app); c.__enter__()
    if lang:
        c.cookies.set("gl_lang", lang)
    if user:
        r = c.post("/api/auth/login", json={"username": user, "password": PW[user]})
        assert r.status_code == 200, r.text
    return c

anon = client()
r = anon.get("/api/i18n/en")
chk("/api/i18n/en công khai, đủ 3 phần", r.status_code == 200 and {"exact", "patterns", "patterns_last"} <= set(r.json()))
chk("VI mặc định: 401 tiếng Việt", anon.get("/api/cases").json()["detail"] == "Chưa đăng nhập hoặc phiên đã hết hạn")
anon_en = client(lang="en")
chk("cookie gl_lang=en: 401 tiếng Anh", anon_en.get("/api/cases").json()["detail"] == "Not signed in or session expired")
chk("/api/meta EN: state_vi + title_vi dịch", (lambda m: m["state_vi"]["in_review"] == "Under review"
    and all(not VI.search(r["title_vi"]) for r in m["rules"]))(anon_en.get("/api/meta").json()))
chk("/api/meta VI không đổi", anon.get("/api/meta").json()["state_vi"]["in_review"] == "Đang thẩm định")
chk("cookie giá trị lạ -> vi", client(lang="fr").get("/api/cases").json()["detail"].startswith("Chưa"))

off = client("nguyen.van.an", lang="en")
cases = off.get("/api/cases").json()["cases"]
new = next(c for c in cases if c["status"] == "new")
chk("danh sách hồ sơ EN: kịch bản dịch", all(not VI.search(c["scenario"] or "") for c in cases),
    [c["scenario"] for c in cases if VI.search(c["scenario"] or "")][:3])
chk("danh sách hồ sơ EN: tên người/tổ chức giữ nguyên như VI",
    [(c["applicant"], c["org"]) for c in cases] == [(c["applicant"], c["org"]) for c in client("nguyen.van.an").get("/api/cases").json()["cases"]])
r = off.post(f"/api/cases/{new['id']}/sign", json={"officer": "x"})
chk("lỗi nghiệp vụ EN", r.status_code in (400, 409) and r.json()["detail"] == "Signing is only possible while the case is under review", r.text)
r = off.post("/api/rulesets/au-demo-fund/approve", json={"officer": "x"})
chk("403 phân quyền EN", r.status_code == 403 and r.json()["detail"].startswith("Account 'Nguyen Van An' has role 'officer' — this action requires: manager"), r.text)
r = off.post("/api/cases/khong-co/crosscheck", json={"officer": "x"})
chk("HTTPException 404 EN", r.status_code == 404 and r.json()["detail"] == "No such application", r.text)
# stream đánh giá (mock) + note_en
lines = [json.loads(l) for l in off.post(f"/api/cases/{new['id']}/assess").text.splitlines() if l.strip()]
prog = [l for l in lines if l["type"] == "progress"]
chk("stream: tiêu đề rule EN", prog and all(not VI.search(p["title"]) for p in prog), prog[:1])
vd = [l for l in lines if l["type"] == "verdict"]
chk("stream: note EN (note_en mock), facts nguyên văn", vd and all("[MOCK EN]" in v["note"] or not VI.search(v["note"]) for v in vd))
cv = off.get(f"/api/cases/{new['id']}").json()
chk("case view EN: note_en lưu DB và thay note", all(v.get("note_en") and v["note"] == v["note_en"] for v in cv["verdicts"]),
    [ (v.get("note"), v.get("note_en")) for v in cv["verdicts"][:1]])
chk("case view EN: văn bản hồ sơ + trích dẫn nguyên văn", cv["case"]["text"] == store.get_case(new["id"])["text"]
    and all(v["rq"] == store.get_verdict(new["id"], v["rule_id"])["rq"] for v in cv["verdicts"]))
cv_vi = client("nguyen.van.an").get(f"/api/cases/{new['id']}").json()
chk("cùng hồ sơ ở VI: note gốc tiếng Việt", all(VI.search(v["note"]) for v in cv_vi["verdicts"]))
au = off.get("/api/audit").json()["events"]
chk("nhật ký EN: hành động dịch, actor 'Hệ thống' -> System", any(e["actor"] == "System" for e in au)
    and all(not VI.search(e["action"]) for e in au if "LLM" not in e["action"]), [e["action"] for e in au if VI.search(e["action"])][:3])
ms = off.get("/api/measurement-status").json()
chk("trạng thái đo EN", not VI.search(ms["headline"]) and all(not VI.search(r["stage"] + r["claim"]) for r in ms["rulesets"]),
    [(r["stage"], r["claim"]) for r in ms["rulesets"] if VI.search(r["stage"] + r["claim"])][:2])
cov = off.get("/api/rulesets/nsf-22-586/coverage").json()
chk("độ phủ guard EN (gaps/label)", all(not VI.search(json.dumps(x, ensure_ascii=False)) for x in cov["rows"]),
    [x for x in cov["rows"] if VI.search(json.dumps(x, ensure_ascii=False))][:1])
bl = off.get("/api/guard-backlog").json()
chk("backlog EN", not VI.search(bl["note"]) and all(not VI.search(i["reason"] + i["action"] + i["priority"]) for i in bl["items"]),
    [i for i in bl["items"] if VI.search(i["reason"] + i["action"] + i["priority"])][:1])

print("=== 4. backfill (mock) ===")
store.db().execute("UPDATE verdicts SET note_en=NULL"); store.db().commit()
n = i18n.backfill_notes()
chk("backfill dịch bù mọi note", n == len(store.get_verdicts(new["id"])) and all(v["note_en"] for v in store.get_verdicts(new["id"])), n)

print(f"\n=== KẾT QUẢ: {OK[0]} OK / {FAIL[0]} FAIL ===")
sys.exit(1 if FAIL[0] else 0)
