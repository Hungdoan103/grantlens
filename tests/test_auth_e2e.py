"""E2E: authentication + roles + post-audit + consistency lock. Mock LLM, temporary DB, NEVER touches real data.
Test accounts use the real officer register but passwords are set in memory (nothing written to disk): auth._load is patched."""
import os, sys, json, tempfile, copy
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="grantlens-auth-"))
os.environ["GRANTLENS_LLM"] = "mock"
os.environ["GRANTLENS_DB"] = str(TMP / "test.db")
os.environ["GRANTLENS_FEEDBACK"] = "off"
os.environ["GRANTLENS_SECRET"] = "test-secret-not-for-production"
os.environ["GRANTLENS_AUTH"] = "on"
ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT); os.chdir(ROOT)

from backend import auth, core, store, workflow
# in-memory account book: nothing is written to data/officers.json
_book = json.loads(auth.OFFICERS.read_text(encoding="utf-8"))
PW = {"sarah.mitchell": "password-mitchell-123", "emily.carter": "password-carter-123",
      "david.thompson": "password-thompson-123", "olivia.bennett": "password-bennett-123"}
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

print("=== 1. not signed in ===")
anon = client()
chk("GET /api/cases without a session -> 401", anon.get("/api/cases").status_code == 401)
chk("POST confirm without a session -> 401", anon.post("/api/cases/HS-01/confirm", json={"officer": "David Thompson", "role": "manager", "rule_id": "R01", "verdict": "met"}).status_code == 401)
chk("/api/meta and /api/auth/me stay public (login screen needs them)", anon.get("/api/meta").status_code == 200 and anon.get("/api/auth/me").json()["user"] is None)
chk("wrong password -> 401", anon.post("/api/auth/login", json={"username": "sarah.mitchell", "password": "wrong"}).status_code == 401)
chk("unknown username -> 401 (same message, no name enumeration)",
    anon.post("/api/auth/login", json={"username": "does.not.exist", "password": "x"}).json()["detail"] ==
    anon.post("/api/auth/login", json={"username": "emily.carter", "password": "x"}).json()["detail"])
for _ in range(5):
    r = anon.post("/api/auth/login", json={"username": "james.miller", "password": "wrong"})
chk("5 failures -> temporary lock (429)", anon.post("/api/auth/login", json={"username": "james.miller", "password": "wrong"}).status_code == 429)

print("=== 2. forged cookie / edited role ===")
an = client("sarah.mitchell")
tok = an.cookies.get(auth.COOKIE)
body, sig = tok.split(".")
forged = json.loads(auth._unb64(body)); forged.update(name="David Thompson", username="david.thompson", role="manager")
bad = auth._b64(json.dumps(forged).encode()) + "." + sig
fake = TestClient(app); fake.__enter__(); fake.cookies.set(auth.COOKIE, bad)
chk("cookie with edited payload (old signature) -> 401", fake.get("/api/cases").status_code == 401)
chk("valid session -> /me returns the right person + role", an.get("/api/auth/me").json()["user"] == {"name": "Sarah Mitchell", "username": "sarah.mitchell", "role": "officer"})

print("=== 3. self-declared identity in the request is OVERWRITTEN ===")
CASE = "HS-01"
with an.stream("POST", f"/api/cases/{CASE}/assess") as r:
    for _ in r.iter_lines(): pass
r = an.post(f"/api/cases/{CASE}/start-review", json={"officer": "David Thompson", "role": "manager"})
chk("start-review OK", r.status_code == 200)
c = store.get_case(CASE, with_text=False)
chk("case records the SIGNED-IN officer, not the self-declared name", c["officer"] == "Sarah Mitchell")
ev = store.audit_events(CASE, limit=5)
chk("audit log records the real actor + real role", all(e["actor"] in ("Sarah Mitchell", "AI", "System") for e in ev) and
    any(e["actor"] == "Sarah Mitchell" and e["role"] == "officer" for e in ev))

print("=== 4. a plain officer cannot pass the 4 manager gates ===")
chk("approve criteria set -> 403", an.post("/api/rulesets/au-demo-fund/approve", json={"officer": "David Thompson", "role": "manager"}).status_code == 403)
chk("approve test-set labels -> 403", an.post("/api/rulesets/au-demo-fund/testset/approve", json={"officer": "David Thompson", "role": "manager"}).status_code == 403)
chk("countersign -> 403", an.post(f"/api/cases/{CASE}/countersign", json={"officer": "David Thompson", "role": "manager"}).status_code == 403)
chk("draw post-audit sample -> 403", an.get("/api/post-audit/sample").status_code == 403)

print("=== 5. review -> sign -> post-audit ===")
cv = an.get(f"/api/cases/{CASE}").json()
spot = cv["case"]["spot_rule"]
an.post(f"/api/cases/{CASE}/spot-check", json={**{"officer": "x"}, "verdict": "met"})
text = store.get_case(CASE)["text"]
sents = [s.strip() for s in text.replace("\n", " ").split(". ") if len(s.split()) >= 9]
used = 0
for v in store.get_verdicts(CASE):
    payload = {"officer": "x", "rule_id": v["rule_id"], "verdict": "met", "reason": ""}
    if v["ai_verdict"] != "met" or (v.get("guard_level") in ("needs-manual-guard", "llm-only")) or "FALSE-PASS" in (v.get("note") or "") or "FALSE PASS" in (v.get("note") or ""):
        payload["reason"] = f"Checked the application for {v['rule_id']}: " + " ".join(sents[used].split()[:9]); used += 1
    r = an.post(f"/api/cases/{CASE}/confirm", json=payload)
    assert r.status_code == 200, (v["rule_id"], r.text)
r = an.post(f"/api/cases/{CASE}/sign", json={"officer": "x", "acknowledge_fast": True})
chk("sign-off OK", r.status_code == 200 and r.json()["status"] == "signed")
chk("every confirmation records the signed-in officer", all(v["confirmed_by"] == "Sarah Mitchell" for v in store.get_verdicts(CASE)))

mg = client("david.thompson")
s = mg.get("/api/post-audit/sample?n=3").json()
chk("manager can draw a post-audit sample", len(s["items"]) >= 1 and all(i["confirmed_by"] == "Sarah Mitchell" for i in s["items"]))
chk("the SAMPLING itself is logged", any("POST-AUDIT SAMPLE" in e["action"] for e in store.audit_events(limit=5)))
it = s["items"][0]
chk("disagree without a reason -> 400", mg.post("/api/post-audit", json={"case_id": it["case_id"], "rule_id": it["rule_id"], "outcome": "disagree"}).status_code == 400)
r = mg.post("/api/post-audit", json={"case_id": it["case_id"], "rule_id": it["rule_id"], "outcome": "disagree", "note": "the quoted passage is not about this criterion"})
chk("post-audit saved + stats updated", r.status_code == 200 and r.json()["stats"]["audited"] == 1 and r.json()["stats"]["disagree"] == 1)
chk("audit log records POST-AUDIT under the manager's name", any("POST-AUDIT" in e["action"] and e["actor"] == "David Thompson" for e in store.audit_events(it["case_id"], limit=5)))
chk("an audited sample is not drawn again", all((i["case_id"], i["rule_id"]) != (it["case_id"], it["rule_id"]) for i in mg.get("/api/post-audit/sample?n=20").json()["items"]))
au = client("olivia.bennett")
chk("auditor can also draw samples", au.get("/api/post-audit/sample?n=1").status_code == 200)
chk("auditor CANNOT countersign -> 403", au.post(f"/api/cases/{CASE}/countersign", json={}).status_code == 403)
chk("auditor CANNOT review: start-review -> 403", au.post("/api/cases/HS-03/start-review", json={}).status_code == 403)
chk("auditor CANNOT confirm a criterion -> 403", au.post(f"/api/cases/{CASE}/confirm", json={"rule_id": "R01", "verdict": "met"}).status_code == 403)
chk("auditor CANNOT create cases / run the AI -> 403", au.post("/api/cases/HS-03/assess").status_code == 403 and
    au.post("/api/cases", json={"applicant": "x", "text": "y " * 60}).status_code == 403)
chk("auditor CAN still view cases + the audit log", au.get(f"/api/cases/{CASE}").status_code == 200 and au.get("/api/audit").status_code == 200)

print("=== 6. countersignature: must be a manager, different from the reviewer ===")
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
        payload["reason"] = f"Checked {v['rule_id']}: " + " ".join(s2[k].split()[:9]); k += 1
    r = an.post(f"/api/cases/{C2}/confirm", json=payload); assert r.status_code == 200, (v["rule_id"], r.text)
an.post(f"/api/cases/{C2}/sign", json={"acknowledge_fast": True})
chk("officer countersigning their own case -> 403", an.post(f"/api/cases/{C2}/countersign", json={}).status_code == 403)
_r = an.post(f"/api/cases/{C2}/letter", json={}); print("     letter ->", _r.status_code, _r.text[:160], "| verdicts:", [(v["rule_id"], v["final_verdict"]) for v in store.get_verdicts(C2)][:6])
chk("no letter before countersignature (428 need_countersign)", _r.status_code == 428 and _r.json().get("need_countersign"))
r = mg.post(f"/api/cases/{C2}/countersign", json={"officer": "Sarah Mitchell"})
chk("manager countersigns OK, recorded under the manager's name", r.status_code == 200 and r.json()["countersigned_by"] == "David Thompson")

print("=== 7. consistency lock on the judgement pass ===")
jc = core.judge_consistency
chk("not_met + supporting_fact=0 -> UNCLEAR", jc("not_met", "high", "x", 0, 3, "partial")[:2] == ("unclear", "low"))
chk("not_met + coverage=none -> UNCLEAR", jc("not_met", "high", "x", 1, 3, "none")[0] == "unclear")
chk("not_met + valid supporting fact -> KEEPS not_met", jc("not_met", "high", "x", 2, 3, "direct") == ("not_met", "high", "x", 2))
chk("met + supporting_fact=0 -> keeps met, valid idx", jc("met", "medium", "x", 0, 3, "direct") == ("met", "medium", "x", 1))
chk("supporting_fact out of range / None -> idx=1, no crash", jc("met", "high", "x", 9, 3, "direct")[3] == 1 and jc("unclear", "low", "x", None, 2, "partial")[3] == 1)
chk("note explains the downgrade", "CONSISTENCY LOCK" in jc("not_met", "high", "x", 0, 3, "partial")[2])

print("=== 8. separation of duties on label approval + logout + audit-chain integrity ===")
from backend import casegen
casegen.LBL_DIR = TMP; (TMP / "generated-au-demo-fund.json").write_text(json.dumps({"ruleset": "au-demo-fund", "cases": [
    {"id": "A", "file": "x", "target": None, "labels": {}, "status": "pending_review"},
    {"id": "B", "file": "x", "target": None, "labels": {}, "status": "pending_review"}]}), encoding="utf-8")
casegen.dispute("au-demo-fund", "B", "clearly wrong label", by="David Thompson")
try:
    casegen.approve("au-demo-fund", "David Thompson"); chk("reviewer approving their own label set -> REFUSED", False)
except ValueError:
    chk("reviewer approving their own label set -> REFUSED", True)
chk("another person approves -> allowed", casegen.approve("au-demo-fund", "Olivia Bennett")["ok"])
an.post("/api/auth/logout")
chk("after logout -> 401", an.get("/api/cases").status_code == 401)
chk("audit hash chain intact", store.verify_chain()["ok"])
chk("password hashes never exposed through /api/officers", "password" not in mg.get("/api/officers").text)

print(f"\n=== {OK[0]} PASS / {FAIL[0]} FAIL ===")
sys.exit(1 if FAIL[0] else 0)
