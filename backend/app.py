"""app.py — GrantLens backend (FastAPI).
Run:   uvicorn backend.app:app --port 8000   (from the project root)
Open:  http://localhost:8000
"""
import json
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from . import core, store, workflow, llm, rag, screening, tables, feedback, coi, auth, i18n
from .llm import LLMError
from .workflow import WorkflowError

# Every JSON response goes through the locale layer (only changes anything when cookie gl_lang selects another UI
# language; verbatim application/rule text is never translated)
app = FastAPI(title="GrantLens", version="2.4", default_response_class=i18n.I18nJSONResponse)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

# --- access key for a public demo: when GRANTLENS_ACCESS_KEY is set every request must carry the key ---
import os
ACCESS_KEY = os.environ.get("GRANTLENS_ACCESS_KEY", "")


@app.middleware("http")
async def access_gate(request, call_next):
    if ACCESS_KEY:
        from fastapi.responses import HTMLResponse
        supplied = request.query_params.get("key") or request.cookies.get("gl_key")
        if supplied != ACCESS_KEY:
            return HTMLResponse(
                "<div style='font-family:sans-serif;max-width:420px;margin:15vh auto;text-align:center'>"
                "<h2>GrantLens — private demo</h2><p>An access key is required. Open the link in this form:</p>"
                "<code>…/?key=YOUR-ACCESS-KEY</code></div>", status_code=401)
        resp = await call_next(request)
        if request.query_params.get("key") == ACCESS_KEY:
            resp.set_cookie("gl_key", ACCESS_KEY, httponly=True, max_age=86400 * 7)
        return resp
    return await call_next(request)


# Identity + role come from the server-side SESSION; officer/role fields in requests are overwritten (see auth.py).
app.add_middleware(auth.IdentityMiddleware)


@app.exception_handler(auth.AuthError)
async def _auth_err(request, exc: auth.AuthError):
    return i18n.I18nJSONResponse(status_code=exc.code, content={"detail": str(exc), "need_login": exc.code == 401})


@app.exception_handler(HTTPException)
async def _http_err(request, exc: HTTPException):
    return i18n.I18nJSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.get("/api/i18n/{lang}")
def i18n_dictionary(lang: str):
    """UI locale dictionary (same source as the server-side JSON locale layer). Public, no login needed."""
    from fastapi.responses import JSONResponse
    if lang not in i18n.LANGS:
        raise HTTPException(404, f"No locale '{lang}'")
    return JSONResponse(i18n.dictionary_payload(lang))


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def auth_login(req: LoginReq, response: Response):
    try:
        user = auth.login(req.username, req.password)
    except auth.AuthError as e:
        store.log("System", f"SIGN-IN FAILED: '{req.username[:40]}' — {e}", None, "system")
        raise
    response.set_cookie(auth.COOKIE, auth.make_session(user), httponly=True, samesite="strict",
                        max_age=int(auth.SESSION_HOURS * 3600))
    store.log(user["name"], "SIGNED IN", None, user["role"], {"username": user["username"]})
    return {"user": user}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    u = getattr(request.state, "user", None)
    if u:
        store.log(u["name"], "SIGNED OUT", None, u["role"])
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = getattr(request.state, "user", None)
    return {"auth_enabled": auth.ENABLED, "user": ({k: u[k] for k in ("name", "username", "role")} if u else None)}


@app.on_event("startup")
def seed():
    # Always refresh demo metadata (scenario, applicant, …) from the English manifest.
    # Existing workflow state / application text are left untouched on conflict.
    for c in core.load_manifest():
        existed = store.case_exists(c["id"])
        store.upsert_case({**c, "text": core.load_app_text(c)})
        if not existed and c.get("ruleset"):
            rs = core.get_ruleset(c["ruleset"])
            store.update_case(c["id"], ruleset_id=rs["id"], ruleset_version=rs["version"])
    if not store.audit_events(limit=1):
        store.log("System", "Audit log initialised (genesis)", None, "system")


@app.exception_handler(WorkflowError)
async def _wf(request, exc: WorkflowError):
    return i18n.I18nJSONResponse(status_code=exc.code, content={"detail": str(exc), **exc.extra})


@app.exception_handler(LLMError)
async def _llm(request, exc: LLMError):
    return i18n.I18nJSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/api/meta")
def meta():
    all_rs = core.load_rulesets()
    return {"rules": core.load_rules(), "llm": llm.describe(), "llm_health": llm.health(),
            "embed_backend": rag.EMBED_BACKEND, "states": workflow.STATES, "state_labels": workflow.STATE_LABELS,
            "min_seconds_per_rule": workflow.MIN_SECONDS_PER_RULE,
            "attestation_min_chars": workflow.ATTESTATION_MIN_CHARS,
            "attestation_min_chars_llm": workflow.ATTESTATION_MIN_CHARS_LLM_ONLY,
            "attestation_min_quote_words": workflow.ATTESTATION_MIN_QUOTE_WORDS,
            "letter_template": bool(core.load_letter_template()),
            "rulesets": [{"id": r["id"], "name": r["name"], "version": r["version"], "region": r.get("region", ""),
                          "n_rules": len(r["rules"]), "status": r.get("status", "approved"),
                          "default": r["id"] == all_rs["_default"]}
                         for k, r in all_rs.items() if k != "_default"],
            "sla_days": coi.sla_days(), "auth_enabled": auth.ENABLED, "languages": list(i18n.LANGS),
            "managers": [o["name"] for o in coi.load_officers() if o.get("role") == "manager"]}


@app.get("/api/stats")
def stats():
    return {**store.stats(), "feedback": feedback.stats()}


@app.get("/api/feedback/stats")
def feedback_stats():
    return feedback.stats()


@app.get("/api/officers")
def officers():
    return {"officers": [{"name": o["name"], "role": o.get("role", "officer"),
                          "affiliations": o.get("affiliations", [])} for o in coi.load_officers()]}


# ---------------- cases ----------------
@app.get("/api/cases")
def cases():
    return {"cases": store.list_cases()}


@app.get("/api/cases/{case_id}")
def case(case_id: str):
    return workflow.case_view(case_id)


class NewCase(BaseModel):
    applicant: str
    org: str = ""
    directorate: str = ""
    text: str
    officer: str = "Officer"
    ruleset_id: str = ""


def _create_case(applicant, org, directorate, text, officer, filename=None, ruleset_id=""):
    text = text.replace("\r\n", "\n").strip()
    if len(text.split()) < 40:
        raise HTTPException(400, "Application text is too short (< 40 words)")
    cid = store.next_upload_id()
    store.upsert_case({"id": cid, "applicant": applicant.strip() or cid, "org": org, "directorate": directorate,
                       "scenario": "Uploaded application" + (f" ({filename})" if filename else ""), "tags": ["upload"],
                       "source": "upload", "text": text})
    try:
        rs = core.get_ruleset(ruleset_id or None, require_approved=True)
    except KeyError as e:
        raise HTTPException(409, str(e))
    store.update_case(cid, ruleset_id=rs["id"], ruleset_version=rs["version"])
    store.log(officer, f"Uploaded new application {cid} — {applicant} ({len(text.split())} words) · criteria set {rs['name']} v{rs['version']}",
              cid, "officer", {"filename": filename, "words": len(text.split()), "ruleset": rs["id"]})
    try:
        _screen(cid, "System")
    except Exception:  # a screening error must not block case creation
        pass
    return store.get_case(cid, with_text=False)


@app.post("/api/cases")
def create_case(req: NewCase):
    return _create_case(req.applicant, req.org, req.directorate, req.text, req.officer, ruleset_id=req.ruleset_id)


@app.post("/api/cases/upload")
async def upload_case(request: Request, file: UploadFile = File(...), applicant: str = Form(""), org: str = Form(""),
                      directorate: str = Form(""), officer: str = Form("Officer"), ruleset_id: str = Form("")):
    officer = auth.current_user(request, officer)["name"]
    try:
        meta = tables.file_to_text(file.filename, await file.read())
    except Exception as e:
        raise HTTPException(400, f"Cannot read file {file.filename}: {e}")
    c = _create_case(applicant or Path(file.filename).stem, org, directorate, meta["text"], officer, file.filename,
                     ruleset_id=ruleset_id)
    if meta["n_tables"] or meta["scanned_pages"]:
        store.log("System", f"Layout analysis {file.filename}: {meta['n_tables']} tables kept structured"
                  + (f", {len(meta['scanned_pages'])} scanned pages NEED OCR (pages {meta['scanned_pages']})" if meta["scanned_pages"] else "")
                  + (", fallback OCR applied" if meta["ocr_used"] else ""), c["id"], "system", meta | {"text": None})
    return {**c, "ingest": {k: v for k, v in meta.items() if k != "text"}}


# ---------------- AI assessment (streaming NDJSON) ----------------
@app.post("/api/cases/{case_id}/assess")
def assess(case_id: str):
    workflow.can_assess(case_id)
    lang = i18n.get_lang()   # the generator runs outside the request context -> capture the language here

    def gen():
        def line(ev):
            return json.dumps(i18n.localize(ev, lang), ensure_ascii=False) + "\n"
        try:
            for ev in workflow.assess_stream(case_id):
                yield line(ev)
        except LLMError as e:
            store.log("System", f"LLM ERROR during assessment: {e}", case_id, "system")
            yield line({"type": "error", "detail": str(e)})
        except WorkflowError as e:
            yield line({"type": "error", "detail": str(e)})
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------------- review workflow ----------------
class Officer(BaseModel):
    officer: str
    role: str = "officer"


class StartReviewReq(Officer):
    acknowledge_coi: bool = False
    coi_reason: str = ""


@app.post("/api/cases/{case_id}/start-review")
def start_review(case_id: str, req: StartReviewReq):
    workflow.start_review(case_id, req.officer, req.role, req.acknowledge_coi, req.coi_reason)
    return workflow.case_view(case_id)


class SpotReq(Officer):
    verdict: str


@app.post("/api/cases/{case_id}/spot-check")
def spot(case_id: str, req: SpotReq):
    res = workflow.spot_check(case_id, req.verdict, req.officer, req.role)
    return {"result": res, **workflow.case_view(case_id)}


class ConfirmReq(Officer):
    rule_id: str
    verdict: str
    reason: str = ""
    evidence_ack: bool = False   # llm-only: the officer confirms the quotation the AI cut


@app.post("/api/cases/{case_id}/confirm")
def confirm(case_id: str, req: ConfirmReq):
    return workflow.confirm(case_id, req.rule_id, req.verdict, req.reason, req.officer, req.role,
                            evidence_ack=req.evidence_ack)


class RuleReq(Officer):
    rule_id: str


@app.post("/api/cases/{case_id}/unconfirm")
def unconfirm(case_id: str, req: RuleReq):
    workflow.unconfirm(case_id, req.rule_id, req.officer, req.role)
    return {"ok": True}


class SignReq(Officer):
    acknowledge_fast: bool = False


@app.post("/api/cases/{case_id}/sign")
def sign(case_id: str, req: SignReq):
    return workflow.sign(case_id, req.officer, req.acknowledge_fast, req.role)


class ReopenReq(Officer):
    reason: str


@app.post("/api/cases/{case_id}/reopen")
def reopen(case_id: str, req: ReopenReq):
    return workflow.reopen(case_id, req.officer, req.reason, req.role)


@app.post("/api/cases/{case_id}/letter")
def letter(case_id: str, req: Officer):
    return {"letter": workflow.draft_letter(case_id, req.officer, req.role)}


class ApproveReq(Officer):
    text: Optional[str] = None


@app.post("/api/cases/{case_id}/letter/approve")
def approve_letter(case_id: str, req: ApproveReq):
    return workflow.approve_letter(case_id, req.officer, req.text, req.role)


# ---------------- multi-GO: criteria sets per grant round + versioning ----------------
@app.get("/api/rulesets")
def rulesets():
    all_rs = core.load_rulesets()
    return {"default": all_rs["_default"],
            "rulesets": [r for k, r in all_rs.items() if k != "_default"]}


class SetRulesetReq(Officer):
    ruleset_id: str


@app.post("/api/cases/{case_id}/ruleset")
def set_case_ruleset(case_id: str, req: SetRulesetReq):
    try:
        return workflow.set_ruleset(case_id, req.ruleset_id, req.officer, req.role)
    except KeyError as e:
        raise HTTPException(404, str(e))


class SaveRulesetReq(Officer):
    id: str
    name: str
    region: str = ""
    version: str = "1"
    source: str = ""
    rules: list


@app.post("/api/rulesets")
def save_ruleset(req: SaveRulesetReq):
    import re as _re
    rid = req.id.strip().lower()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9\-_.]{2,60}", rid):
        raise HTTPException(400, "criteria-set id: lowercase/digits/dashes, 3-60 characters")
    rules = [r for r in req.rules if r.get("id") and r.get("quote") and r.get("title")]
    if len(rules) < 3:
        raise HTTPException(400, "A criteria set needs ≥ 3 valid rules (id, title, quote)")
    for r in rules:
        r["type"] = r.get("type") if r.get("type") in ("quantitative", "qualitative") else "qualitative"
    rs = {"id": rid, "name": req.name.strip(), "region": req.region, "version": req.version.strip() or "1",
          "source": req.source, "saved_by": req.officer, "saved_at": store.now(), "rules": rules,
          "status": "draft"}  # saving = DRAFT; changing/overwriting the active set also goes back to draft — must be re-approved
    (core.RULESETS_DIR / f"{rid}.json").write_text(json.dumps(rs, ensure_ascii=False, indent=2), encoding="utf-8")
    idx = json.loads((core.RULESETS_DIR / "index.json").read_text(encoding="utf-8"))
    idx["rulesets"] = [e for e in idx["rulesets"] if e["id"] != rid] + [
        {"id": rid, "file": f"{rid}.json", "name": rs["name"], "region": rs["region"], "version": rs["version"]}]
    (core.RULESETS_DIR / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    core.reload_rulesets()
    store.log(req.officer, f"SAVED criteria set '{rs['name']}' v{rs['version']} ({len(rules)} rules) — id {rid}, status DRAFT. "
              "Cannot be assigned to cases until a MANAGER approves it (with a guard-coverage check); existing cases keep their locked set.",
              None, req.role, {"ruleset": rid, "version": rs["version"], "n_rules": len(rules), "status": "draft"})
    return {"ok": True, "ruleset": {k: v for k, v in rs.items() if k != "rules"}, "n_rules": len(rules)}


class ApproveRulesetReq(Officer):
    acknowledge_low_coverage: bool = False
    reason: str = ""


@app.post("/api/rulesets/{ruleset_id}/approve")
def approve_ruleset(ruleset_id: str, req: ApproveRulesetReq, request: Request):
    """A MANAGER approves a criteria set (minimum guard coverage is checked before cases can be assigned)."""
    from . import guards
    auth.require_role(request, "manager", fallback_name=req.officer)
    try:
        rs = core.get_ruleset(ruleset_id)
    except KeyError:
        raise HTTPException(404, f"No criteria set '{ruleset_id}'")
    if rs.get("status", "approved") == "approved":
        raise HTTPException(409, "This criteria set is already approved")
    cov = guards.coverage(rs)
    MIN_COV = 0.5
    if cov["pct"] < MIN_COV and not req.acknowledge_low_coverage:
        raise HTTPException(428, f"Guard coverage {cov['n_guarded']}/{cov['n_rules']} ({cov['pct']:.0%}) is below the minimum {MIN_COV:.0%} — "
                            "'llm-only' rules have no code safety net. Add explicit thresholds/prohibitions to the quote, "
                            "or accept the risk with a reason (acknowledge_low_coverage=true).")
    if cov["pct"] < MIN_COV and len((req.reason or "").strip()) < 8:
        raise HTTPException(400, "Accepting low guard coverage requires a reason (≥ 8 characters)")
    p = core.RULESETS_DIR / f"{ruleset_id}.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc.update(status="approved", approved_by=req.officer, approved_at=store.now(),
               approved_coverage=f"{cov['n_guarded']}/{cov['n_rules']}")
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    core.reload_rulesets()
    store.log(req.officer, f"APPROVED criteria set '{rs['name']}' v{rs['version']} — guard coverage {cov['n_guarded']}/{cov['n_rules']}"
              + (f" (BELOW threshold, risk accepted — reason: {req.reason})" if cov["pct"] < MIN_COV else "")
              + ". Can now be assigned to cases.", None, "manager",
              {"ruleset": ruleset_id, "coverage": cov["pct"], "low_cov_ack": cov["pct"] < MIN_COV})
    return {"ok": True, "status": "approved", "coverage": cov}


@app.get("/api/rulesets/{ruleset_id}/coverage")
def ruleset_coverage(ruleset_id: str):
    """Guard coverage: which rules are protected by code (hand-written/compiled), which rely ONLY on LLM + officer."""
    from . import guards
    try:
        return guards.coverage(core.get_ruleset(ruleset_id))
    except KeyError:
        raise HTTPException(404, f"No criteria set '{ruleset_id}'")


class CasegenReq(Officer):
    n: int = 3


@app.post("/api/rulesets/{ruleset_id}/casegen")
def ruleset_casegen(ruleset_id: str, req: CasegenReq):
    """The AI generates a labelled test set for the ruleset (1 clean case + n violation cases; labels by construction,
    cross-checked by guards). A local model is slow — ~60-90s per case. Large sets: python -m backend.casegen <id> --gen"""
    from . import casegen
    try:
        core.get_ruleset(ruleset_id)
    except KeyError:
        raise HTTPException(404, f"No criteria set '{ruleset_id}'")
    doc = casegen.generate(ruleset_id, n_violations=max(1, min(req.n, 5)), officer=req.officer)
    store.log(req.officer, f"AI generated a test set for '{ruleset_id}': {len(doc['cases'])} cases (skipped {len(doc['skipped'])}) — "
              "labels by construction + guard cross-check, AWAITING officer approval", None, req.role,
              {"ruleset": ruleset_id, "n_cases": len(doc["cases"]), "skipped": len(doc["skipped"])})
    return doc


@app.get("/api/guard-backlog")
def guard_backlog():
    """The to-do list for reducing false-pass risk: every criterion without a code safety net across ALL criteria
    sets, ordered by danger. This backlog is managed in the open and never allowed to drift."""
    from . import guards
    all_rs = core.load_rulesets()
    items = []
    for k, rs in all_rs.items():
        if k == "_default":
            continue
        cov = guards.coverage(rs)
        for r in cov["rows"]:
            if r["guard"] == "needs-manual-guard":
                items.append({"ruleset": rs["id"], "ruleset_name": rs["name"], "rule": r["rule"],
                              "title": r["title"], "priority": "high", "reason": "; ".join(g["reason"] for g in r["gaps"]),
                              "action": "write a hand guard for this criterion"})
            elif r["guard"] == "code-guarded" and r["gaps"]:
                items.append({"ruleset": rs["id"], "ruleset_name": rs["name"], "rule": r["rule"],
                              "title": r["title"], "priority": "medium",
                              "reason": "; ".join(g["reason"] for g in r["gaps"]),
                              "action": "re-check the hand guard covers every branch"})
            elif r["guard"] == "llm-only":
                items.append({"ruleset": rs["id"], "ruleset_name": rs["name"], "rule": r["rule"],
                              "title": r["title"], "priority": "low",
                              "reason": "purely qualitative criterion — no figures for code to check",
                              "action": "LLM + mandatory officer attestation by quotation (enabled)"})
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (order[x["priority"]], x["ruleset"], x["rule"]))
    return {"total": len(items), "by_priority": {p: sum(1 for i in items if i["priority"] == p) for p in order},
            "items": items,
            "note": "Priority 'high' = criteria with quantitative logic that code cannot yet express: a hand guard must be "
                    "written for that fund. Until then the system forces officers to attest with a verbatim quotation "
                    "and logs it for post-audit."}


@app.get("/api/measurement-status")
def measurement_status():
    """MEASUREMENT STATUS PER FUND — prevents the misreading "measuring 1 fund = proving the whole system".

    A false-pass figure is only valid for the fund that completed the full chain:
        generate test set -> review/dispute/flag -> approve labels -> eval -> [TARGET] metric.
    A fund that has not completed the chain shows which step it is at and NEVER borrows another fund's figure.
    """
    from . import guards
    root = core.DATA.parent
    out = []
    for k, rs in core.load_rulesets().items():
        if k == "_default":
            continue
        cov = guards.coverage(rs)
        lbl_p = core.DATA / "labels" / f"generated-{rs['id']}.json"
        ev_p = root / f"eval-generated-{rs['id']}.json"
        lbl = json.loads(lbl_p.read_text(encoding="utf-8")) if lbl_p.exists() else None
        ev = json.loads(ev_p.read_text(encoding="utf-8")) if ev_p.exists() else None
        measured = False
        if not lbl:
            stage, claim = "no test set yet", "NOT MEASURED — no figure to report"
        elif any(c.get("status") == "needs_review" for c in lbl.get("cases", [])):
            stage, claim = "test set exists, verifier-flagged cases still unreviewed", "NOT MEASURED — labels under review"
        elif not lbl.get("approved"):
            stage, claim = "test set exists, labels not approved", "PROVISIONAL — not for reporting"
        elif not ev:
            stage, claim = "labels approved, evaluation not run", "NOT MEASURED"
        elif (ev.get("labels_generated_at") != lbl.get("generated_at")
              or ev.get("labels_approved_at") != lbl.get("approved_at")):
            # no stale figures: the evaluation must have run on EXACTLY the currently approved labels
            stage, claim = "evaluation is stale — does not match the current labels", "NOT MEASURED — evaluation must be re-run"
        elif ev.get("provisional"):
            stage, claim = "evaluated but labels not approved", "PROVISIONAL — not for reporting"
        else:
            t, ctl = ev.get("target") or {}, ev.get("control") or {}
            measured, stage = True, "measured through the full chain"
            claim = (f"caught violations {t.get('correct')}/{t.get('total')}, "
                     f"false passes {t.get('false_pass')}/{t.get('total')}"
                     + (f"; clean cases: false alarms {ctl.get('false_alarm')}/{ctl.get('total')}" if ctl else "")
                     + (" · small sample, preliminary evidence" if (t.get("total") or 0) < 10 else ""))
        out.append({
            "ruleset": rs["id"], "name": rs["name"], "version": rs.get("version"),
            "region": rs.get("region", ""), "ruleset_status": rs.get("status", "approved"),
            "guard_coverage": {"code_guarded": cov["n_guarded"], "needs_manual": cov["n_needs_manual"],
                               "llm_only": cov["n_llm_only"], "n_rules": cov["n_rules"]},
            "testset": None if not lbl else {
                "n_cases": len(lbl.get("cases", [])),
                "label_scope": lbl.get("label_scope", "full"),
                "generator_model": lbl.get("generator_model") or lbl.get("generated_by"),
                "approved": bool(lbl.get("approved")), "approved_by": lbl.get("approved_by")},
            "measured": measured,
            "assessor_model": (ev or {}).get("assessor_model") or (ev or {}).get("llm"),
            "stage": stage, "claim": claim,
            "target_metric": (ev or {}).get("target") if measured else None,
            "control_metric": (ev or {}).get("control") if measured else None,
        })
    out.sort(key=lambda x: (not x["measured"], x["ruleset"]))
    n_measured = sum(1 for x in out if x["measured"])
    return {"n_rulesets": len(out), "n_measured": n_measured,
            "headline": f"{n_measured}/{len(out)} funds have their own measurement; "
                        f"{len(out) - n_measured} funds only have a ruleset + code guards, NOT measured yet.",
            "warning": "Never use one fund's figure to speak for another. Each fund must complete its own chain: "
                       "generate test set → review → approve labels → evaluate.",
            "rulesets": out}


@app.get("/api/rulesets/{ruleset_id}/testset")
def get_testset(ruleset_id: str):
    """Status of the generated test set for a ruleset: labels approved or not, which cases are weak/disputed."""
    from . import casegen
    try:
        _, doc = casegen._load(ruleset_id)
    except FileNotFoundError:
        return {"exists": False, "ruleset": ruleset_id}
    return {"exists": True, "ruleset": ruleset_id, "approved": doc.get("approved", False),
            "approved_by": doc.get("approved_by"), "generated_by": doc.get("generated_by"),
            "cases": [{"id": c["id"], "target": c.get("target"), "status": c.get("status", "pending_review"),
                       "weak_labels": c.get("weak_labels", []), "guard_agree": c.get("guard_agree")}
                      for c in doc["cases"]]}


class ApproveLabelsReq(Officer):
    pass


@app.post("/api/rulesets/{ruleset_id}/testset/approve")
def approve_testset(ruleset_id: str, req: ApproveLabelsReq, request: Request):
    """A MANAGER approves the test-set labels — only after this step do measurements stop being 'provisional'.
    Role lock: previously anyone could call this gate."""
    from . import casegen
    auth.require_role(request, "manager", fallback_name=req.officer)
    try:
        res = casegen.approve(ruleset_id, req.officer)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:  # verifier-flagged cases still unreviewed -> approval refused
        raise HTTPException(409, str(e))
    store.log(req.officer, f"APPROVED test-set labels '{ruleset_id}' ({res['n_cases']} cases) — "
              "measurements on this set are no longer provisional", None, req.role, {"ruleset": ruleset_id})
    return res


class DisputeReq(Officer):
    case_id: str
    reason: str


@app.post("/api/rulesets/{ruleset_id}/testset/dispute")
def dispute_testcase(ruleset_id: str, req: DisputeReq):
    """Remove a generated case from the metric (wrong label / ambiguous test) — kept for traceability."""
    from . import casegen
    if len((req.reason or "").strip()) < 8:
        raise HTTPException(400, "Removing a case from the test set requires a reason (≥ 8 characters)")
    try:
        res = casegen.dispute(ruleset_id, req.case_id, req.reason, by=req.officer)
    except (FileNotFoundError, KeyError) as e:
        raise HTTPException(404, str(e))
    store.log(req.officer, f"REMOVED case '{req.case_id}' from test set '{ruleset_id}' — reason: {req.reason}",
              None, req.role, {"ruleset": ruleset_id, "case": req.case_id})
    return res


# ---------------- post-audit sampling (manager / auditor) ----------------
# Closes the residual risk of the attestation gate: code can check that a quoted passage REALLY EXISTS in the application,
# not that it is RELEVANT to the criterion. Cases that meet every criterion never pass through countersignature -> this is
# the "sampled second pair of eyes" for exactly that zone.
@app.get("/api/post-audit/sample")
def post_audit_sample(request: Request, n: int = 5, officer: str = ""):
    import random
    u = auth.require_role(request, "manager", "auditor", fallback_name=officer)
    pool = store.post_audit_candidates(u["name"])
    picked = random.sample(pool, min(max(1, min(n, 20)), len(pool))) if pool else []
    items = []
    for v in picked:
        c = store.get_case(v["case_id"], with_text=False)
        rule = next((r for r in workflow.case_rules(c) if r["id"] == v["rule_id"]), {})
        items.append({"case_id": v["case_id"], "applicant": v.get("applicant"), "rule_id": v["rule_id"],
                      "rule_title": rule.get("title"), "rule_quote": rule.get("quote"),
                      "guard_level": v.get("guard_level"), "ai_verdict": v.get("ai_verdict"), "ai_quote": v.get("aq"),
                      "attestation": v.get("officer_reason"), "confirmed_by": v.get("confirmed_by"),
                      "confirmed_at": v.get("confirmed_at")})
    if items:  # log the SAMPLING itself: nobody redraws until an easy sample turns up without leaving a trace
        store.log(u["name"], f"POST-AUDIT SAMPLE drawn: {len(items)}/{len(pool)} MET confirmations on unguarded criteria",
                  None, u["role"], {"sample": [[i["case_id"], i["rule_id"]] for i in items]})
    return {"pool": len(pool), "items": items, "stats": store.post_audit_stats()}


class PostAuditReq(Officer):
    case_id: str
    rule_id: str
    outcome: str          # agree | disagree
    note: str = ""


@app.post("/api/post-audit")
def post_audit_save(req: PostAuditReq, request: Request):
    u = auth.require_role(request, "manager", "auditor", fallback_name=req.officer)
    if req.outcome not in ("agree", "disagree"):
        raise HTTPException(400, "outcome must be agree or disagree")
    v = store.get_verdict(req.case_id, req.rule_id)
    if not v or v.get("final_verdict") != "met":
        raise HTTPException(404, "No such MET confirmation")
    if (v.get("confirmed_by") or "") == u["name"]:
        raise HTTPException(409, "You cannot post-audit your own confirmation")
    note = (req.note or "").strip()
    if req.outcome == "disagree" and len(note) < 8:
        raise HTTPException(400, "Disagreeing requires a reason (≥ 8 characters)")
    store.save_post_audit(req.case_id, req.rule_id, u["name"], req.outcome, note)
    verb = "AGREES with the attestation by" if req.outcome == "agree" else "DISAGREES with the attestation by"
    store.log(u["name"], f"POST-AUDIT {req.rule_id}: {verb} {v.get('confirmed_by')}"
              + (f" — {note}" if note else "") + (" → recommends REOPENING the case" if req.outcome == "disagree" else ""),
              req.case_id, u["role"], {"rule": req.rule_id, "outcome": req.outcome, "confirmed_by": v.get("confirmed_by")})
    return {"ok": True, "stats": store.post_audit_stats()}


@app.get("/api/post-audit")
def post_audit_list():
    return {"stats": store.post_audit_stats(), "items": store.post_audit_list()}


# ---------------- countersignature (manager) + supplement rounds ----------------
@app.post("/api/cases/{case_id}/countersign")
def countersign(case_id: str, req: Officer, request: Request):
    auth.require_role(request, "manager", fallback_name=req.officer)
    return workflow.countersign(case_id, req.officer)


class SupplementReq(Officer):
    items: str
    days: int = 15


@app.post("/api/cases/{case_id}/request-supplement")
def request_supplement(case_id: str, req: SupplementReq):
    return workflow.request_supplement(case_id, req.items, req.days, req.officer, req.role)


# ---------------- bias lab ----------------
class BiasReq(BaseModel):
    a: str
    b: str


@app.post("/api/bias/compare")
def bias_compare(req: BiasReq):
    va = {v["rule_id"]: v for v in store.get_verdicts(req.a)}
    vb = {v["rule_id"]: v for v in store.get_verdicts(req.b)}
    if not va or not vb:
        raise HTTPException(409, "Both applications must be assessed by the AI first (open each → Run AI assessment)")
    rows = [{"rule": r["id"], "title": r["title"], "a": va[r["id"]]["ai_verdict"], "b": vb[r["id"]]["ai_verdict"],
             "a_conf": va[r["id"]]["ai_confidence"], "b_conf": vb[r["id"]]["ai_confidence"],
             "match": va[r["id"]]["ai_verdict"] == vb[r["id"]]["ai_verdict"]} for r in core.load_rules()]
    match = sum(1 for r in rows if r["match"])
    store.log("System", f"Bias test {req.a} vs {req.b}: {match}/{len(rows)} criteria match", None, "system",
              {"a": req.a, "b": req.b, "match": match})
    return {"rows": rows, "match": match, "total": len(rows)}


# ---------------- crosscheck (anti-cheating) + attachments ----------------
@app.post("/api/cases/{case_id}/crosscheck")
def crosscheck_run(case_id: str, req: Officer):
    return {"crosscheck": workflow.run_crosscheck(case_id, actor=req.officer)}


@app.post("/api/cases/{case_id}/attach")
async def attach(request: Request, case_id: str, file: UploadFile = File(None), name: str = Form(""),
                 text: str = Form(""), officer: str = Form("Officer")):
    officer = auth.current_user(request, officer)["name"]
    if file is not None:
        try:
            meta_doc = tables.file_to_text(file.filename, await file.read())
        except Exception as e:
            raise HTTPException(400, f"Cannot read file {file.filename}: {e}")
        doc_text, doc_name = meta_doc["text"], name or file.filename
    else:
        doc_text, doc_name = text, name or "Attached document"
    cc = workflow.attach_document(case_id, doc_name, doc_text, officer)
    return {"crosscheck": cc, **workflow.case_view(case_id)}


# ---------------- screening (denied-party lists + ABN) ----------------
def _screen(case_id: str, actor: str) -> dict:
    c = store.get_case(case_id, with_text=False)
    if not c:
        raise HTTPException(404, "No such application")
    res = screening.screen_case(c.get("applicant") or "", c.get("org") or "")
    store.update_case(case_id, screening=json.dumps(res, ensure_ascii=False))
    lbl = {"clear": "no hits", "note_expired": "only expired hits",
           "review": "HITS — officer must review", "review_strong": "STRONG HIT — officer must review"}[res["risk"]]
    store.log(actor, f"Sanctions screening {case_id}: {res['n_hits']} hits → {lbl}", case_id, "system",
              {"risk": res["risk"], "n_hits": res["n_hits"]})
    return res


@app.post("/api/cases/{case_id}/screen")
def screen(case_id: str, req: Officer):
    return {"screening": _screen(case_id, req.officer)}


@app.get("/api/screening/status")
def screening_status():
    return {"asic": len(screening.load_asic()), "dfat": len(screening.load_dfat()),
            "abn": screening.abn_status(), "thresholds": {"strong": screening.STRONG, "possible": screening.WEAK}}


class LookupReq(BaseModel):
    name: str


@app.post("/api/screening/lookup")
def screening_lookup(req: LookupReq):
    if len(req.name.strip()) < 4:
        raise HTTPException(400, "Lookup name is too short")
    return {"asic": screening.match_asic(req.name), "dfat": screening.match_dfat(req.name),
            "abn": screening.abn_lookup(req.name)}


# ---------------- audit ----------------
@app.get("/api/audit")
def audit(case_id: Optional[str] = None, limit: int = 300):
    return {"events": store.audit_events(case_id, limit), "chain": store.verify_chain()}


# ---------------- letter template: view / generate from office rules / save ----------------
LETTER_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "data" / "letter-template.txt"


@app.get("/api/letter-template")
def get_letter_template():
    return {"template": core.load_letter_template(), "active": bool(core.load_letter_template())}


class TemplateGenReq(BaseModel):
    rules_text: str


@app.post("/api/letter-template/generate")
def generate_template(req: TemplateGenReq):
    if len(req.rules_text.split()) < 20:
        raise HTTPException(400, "Rules text is too short (< 20 words)")
    tpl = core.generate_letter_template(req.rules_text)
    store.log("AI", f"Reverse-engineered the office rules ({len(req.rules_text.split())} words) into a DRAFT letter template — awaiting officer review and save",
              None, "system", {"words_rules": len(req.rules_text.split()), "words_template": len(tpl.split())})
    return {"template": tpl, "note": "Draft — takes effect only after an officer reviews/edits and clicks Save"}


class TemplateSaveReq(Officer):
    template: str


@app.post("/api/letter-template")
def save_template(req: TemplateSaveReq):
    tpl = req.template.strip()
    if len(tpl.split()) < 20:
        raise HTTPException(400, "Template is too short")
    LETTER_TEMPLATE_PATH.write_text(f"[Office letter template — saved by {req.officer} {store.now()}]\n{tpl}\n", encoding="utf-8")
    store.log(req.officer, "SAVED new office letter template — all outcome letters now follow it", None, req.role,
              {"words": len(tpl.split())})
    return {"ok": True, "active": True}


# ---------------- ingest: rule extraction from a new guideline ----------------
class GuidelineReq(BaseModel):
    text: str


@app.post("/api/rules/extract")
def rules_extract(req: GuidelineReq):
    if len(req.text.split()) < 50:
        raise HTTPException(400, "Guideline text is too short")
    rules = core.extract_rules(req.text)
    store.log("AI", f"Extracted {len(rules)} candidate criteria from a new guideline ({len(req.text.split())} words)", None, "system")
    return {"rules": rules, "note": "Preview — criteria take effect only after an officer reviews them and adds them to rules.json"}
