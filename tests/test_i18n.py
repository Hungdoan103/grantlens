"""Locale layer: English is the source language; the Vietnamese locale is an optional display layer.
Checks: no Vietnamese in any API output by default, the VI dictionary is clean, verbatim regions are never translated,
cookie gl_lang=vi localises JSON/errors/stream events, unknown locales fall back to English.
Mock LLM, temporary DB, NEVER touches real data."""
import os, sys, json, tempfile, copy, re
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="grantlens-i18n-"))
os.environ["GRANTLENS_LLM"] = "mock"
os.environ["GRANTLENS_DB"] = str(TMP / "test.db")
os.environ["GRANTLENS_FEEDBACK"] = "off"
os.environ["GRANTLENS_SECRET"] = "test-secret-not-for-production"
os.environ["GRANTLENS_AUTH"] = "on"
ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT); os.chdir(ROOT)

from backend import auth, i18n, locale_vi, store
_book = json.loads(auth.OFFICERS.read_text(encoding="utf-8"))
PW = {"sarah.mitchell": "password-mitchell-123", "david.thompson": "password-thompson-123"}
for o in _book["officers"]:
    if o.get("username") in PW:
        o["password"] = auth.hash_password(PW[o["username"]])
auth._load = lambda: copy.deepcopy(_book)

from fastapi.testclient import TestClient
from backend.app import app

OK, FAIL = [0], [0]
def chk(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   <- {extra}" if (extra and not cond) else "")); (OK if cond else FAIL)[0] += 1

VI = re.compile("[àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ]", re.I)

print("=== 1. engine ===")
chk("VI dictionary: English keys carry no Vietnamese, values are non-empty",
    all(k and v and not VI.search(k) for k, v in locale_vi.EXACT.items()), [k for k in locale_vi.EXACT if VI.search(k)][:3])
chk("every VI pattern compiles (Python)", all(re.compile(p) for p, _ in locale_vi.PATTERNS + locale_vi.PATTERNS_LAST))
chk("English is a no-op", i18n.translate("Not signed in", "en") == "Not signed in")
chk("unknown locale is a no-op", i18n.translate("Not signed in", "fr") == "Not signed in")
chk("exact", i18n.translate("Not signed in", "vi") == "Chưa đăng nhập")
chk("keeps leading/trailing whitespace", i18n.translate("  Under review ", "vi") == "  Đang thẩm định ")
chk("pattern with numbers", i18n.translate("2 criteria not yet confirmed: R01, R05", "vi") == "Còn 2 tiêu chí chưa xác nhận: R01, R05")
chk("pattern with $$", i18n.translate("Total budget $300,000 < minimum $400,000 (code compared figures)", "vi")
    == "Tổng ngân sách 300,000$ < mức tối thiểu 400,000$ (code so số liệu)")
chk("phrase inside a composite string", "báo động giả" in i18n.translate("clean cases: false alarms 0/2 · small sample, preliminary evidence", "vi"))
chk("uppercase labels run last", i18n.translate("R04: officer MET / AI UNCLEAR", "vi") == "R04: cán bộ ĐẠT / AI CHƯA RÕ")
chk("multi-line (confirm/alert)", i18n.translate("Generated 4 cases (skipped 0).\nSaved: a.json (approved=false).", "vi") == "Đã sinh 4 case (bỏ 0).\nLưu: a.json (approved=false).")
chk("rule titles via the loaded rulesets", i18n.translate("PI is untenured", "vi") == "PI chưa có tenure (untenured)")
i18n.set_lang("vi"); chk("tr() follows the context variable", i18n.tr("Signed off") == "Đã ký duyệt"); i18n.set_lang("en")

print("=== 2. localize: verbatim regions ===")
o = i18n.localize({"text": "Application text stays", "quote": "Rule quote stays", "aq": "Not signed in", "rq": "Not signed in",
                   "officer_reason": "typed by the officer: Not signed in", "applicant": "Nguyen Van A", "note": "AI note stays as written: Not signed in",
                   "detail": "Not signed in", "state_labels": {"new": "New"}, "actor": "System",
                   "rules": [{"title": "Registered for GST", "quote": "Registered for GST"}],
                   "facts": [{"fact": "Registered for GST", "key_phrase": "Registered for GST"}]}, "vi")
chk("text/quote/aq/rq/officer_reason/applicant/note untouched",
    o["text"] == "Application text stays" and o["quote"] == "Rule quote stays" and o["aq"] == o["rq"] == "Not signed in"
    and o["officer_reason"].startswith("typed") and o["note"].startswith("AI note") and o["applicant"] == "Nguyen Van A", o)
chk("detail/state_labels/actor/title localised", o["detail"] == "Chưa đăng nhập" and o["state_labels"]["new"] == "Mới"
    and o["actor"] == "Hệ thống" and o["rules"][0]["title"] == "Đăng ký GST")
chk("quote inside rules + facts untouched", o["rules"][0]["quote"] == "Registered for GST" and o["facts"][0]["fact"] == "Registered for GST")

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
chk("/api/i18n/vi is public and complete", (lambda r: r.status_code == 200 and {"exact", "patterns", "patterns_last"} <= set(r.json()))(anon.get("/api/i18n/vi")))
chk("/api/i18n/en is empty (no translation)", anon.get("/api/i18n/en").json()["exact"] == {})
chk("/api/i18n/xx -> 404", anon.get("/api/i18n/xx").status_code == 404)
chk("default: 401 in English", anon.get("/api/cases").json()["detail"] == "Not signed in or session expired")
chk("cookie gl_lang=vi: 401 in Vietnamese", client(lang="vi").get("/api/cases").json()["detail"] == "Chưa đăng nhập hoặc phiên đã hết hạn")
chk("unknown cookie value -> English", client(lang="fr").get("/api/cases").json()["detail"].startswith("Not signed"))
m_en, m_vi = anon.get("/api/meta").json(), client(lang="vi").get("/api/meta").json()
chk("/api/meta EN: no Vietnamese anywhere", not VI.search(json.dumps(m_en, ensure_ascii=False)))
chk("/api/meta VI: state labels + rule titles localised", m_vi["state_labels"]["in_review"] == "Đang thẩm định" and all(VI.search(r["title"]) for r in m_vi["rules"]))
chk("/api/meta VI: rule quotes untouched", [r["quote"] for r in m_vi["rules"]] == [r["quote"] for r in m_en["rules"]])

off = client("sarah.mitchell")
cases = off.get("/api/cases").json()["cases"]
new = next(c for c in cases if c["status"] == "new")
lines = [json.loads(l) for l in off.post(f"/api/cases/{new['id']}/assess").text.splitlines() if l.strip()]
vd = [l for l in lines if l["type"] == "verdict"]
chk("stream: verdict events arrive with type 'verdict' + rule_type", vd and all(v.get("rule_type") in ("qualitative", "quantitative") for v in vd))
cv = off.get(f"/api/cases/{new['id']}").json()
au = off.get("/api/audit").json()["events"]
ms = off.get("/api/measurement-status").json()
cov = off.get("/api/rulesets/nsf-22-586/coverage").json()
bl = off.get("/api/guard-backlog").json()
r400 = off.post(f"/api/cases/{new['id']}/sign", json={"officer": "x"})
r403 = off.post("/api/rulesets/au-demo-fund/approve", json={"officer": "x"})
blob = json.dumps([cases, lines, cv, au, ms, cov, bl, r400.json(), r403.json()], ensure_ascii=False)
chk("EN default: no Vietnamese in cases/stream/case view/audit/measurement/coverage/backlog/errors", not VI.search(blob),
    [m.group(0) for m in re.finditer(r".{30}[àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ].{30}", blob)][:3])
chk("workflow error EN", r400.status_code in (400, 409) and r400.json()["detail"] == "Signing is only possible while the case is under review", r400.text)
chk("403 EN", r403.status_code == 403 and r403.json()["detail"].startswith("Account 'Sarah Mitchell' has role 'officer'"), r403.text)

vi = client("sarah.mitchell", lang="vi")
chk("VI: workflow error localised", vi.post(f"/api/cases/{new['id']}/sign", json={"officer": "x"}).json()["detail"] == "Chỉ ký duyệt được khi hồ sơ đang thẩm định")
cvv = vi.get(f"/api/cases/{new['id']}").json()
chk("VI: application text + quotations identical to EN", cvv["case"]["text"] == cv["case"]["text"] and [v["rq"] for v in cvv["verdicts"]] == [v["rq"] for v in cv["verdicts"]])
chk("VI: AI note left as written by the model", [v["note"] for v in cvv["verdicts"]] == [v["note"] for v in cv["verdicts"]])
chk("VI: state labels localised in the case view", cvv["state_labels"]["signed"] == "Đã ký duyệt")
lines_vi = [json.loads(l) for l in vi.post(f"/api/cases/{new['id']}/assess").text.splitlines() if l.strip()]
chk("VI: stream progress titles localised", all(VI.search(l["title"]) for l in lines_vi if l["type"] == "progress"))
chk("VI: audit actions localised where a pattern exists", any(VI.search(e["action"]) for e in vi.get("/api/audit").json()["events"]))

print(f"\n=== RESULT: {OK[0]} OK / {FAIL[0]} FAIL ===")
sys.exit(1 if FAIL[0] else 0)
