"""workflow.py — case state machine + the "trust twist" business rules.

States:
  new -> assessed -> in_review -> signed -> letter_drafted -> letter_approved
              ^          |  ^                  |
              |          v  +---- reopen ------+   (a reason is mandatory)
              +-- awaiting_supplement              (supplement requested, several rounds, SLA deadline)

Rules enforced ON THE SERVER (the frontend is never trusted):
  1. Multi-GO + versioning: every case LOCKS its criteria set (id + version + content) at the first assessment —
     changing rules mid-way does not affect cases in progress; the snapshot is stored in the case.
  2. Blind spot-check; line-by-line confirmation; overriding the AI / AI unclear -> reason mandatory.
  3. Sign-off: 100% of criteria confirmed + minimum time threshold (GRANTLENS_MIN_SECONDS_PER_RULE, default 15s/criterion).
  4. COUNTERSIGNATURE: a case with NOT MET / UNCLEAR criteria may only issue a letter after a MANAGER
     (role=manager in the officer register, different from the reviewing officer, COI-checked) countersigns.
  5. Supplement rounds: the officer requests a supplement (items + deadline) -> wait for documents -> attach -> re-assess;
     the number of rounds is counted and logged.
  6. False-pass guard (backend/guards.py) runs inside the assessment pipeline — see core.assess_rule.
"""
import json, os, random
from datetime import date, timedelta
from . import store, core, rag, coi, feedback, crosscheck
from . import verify as verify_mod

STATES = ["new", "assessed", "in_review", "awaiting_supplement", "signed", "letter_drafted", "letter_approved"]
STATE_LABELS = {"new": "New", "assessed": "AI assessed (draft)", "in_review": "Under review",
                "awaiting_supplement": "Awaiting supplement", "signed": "Signed off",
                "letter_drafted": "Letter drafted", "letter_approved": "Letter approved"}
FINAL_ALLOWED = ["met", "not_met", "unclear"]
MIN_SECONDS_PER_RULE = int(os.environ.get("GRANTLENS_MIN_SECONDS_PER_RULE", "15"))
# Confirming MET on a criterion WITHOUT a code safety net requires self-verified evidence.
# Friction is tiered by risk so that criteria with a safety net are not bothered needlessly:
#   needs-manual-guard : must be typed by the officer, longer, may not borrow the AI's quotation
#   llm-only           : shorter input OR confirmation of the quotation the AI cut (the row must be opened to read it)
# Both typed variants are checked by code: they must contain a VERBATIM passage that really exists in the application.
ATTESTATION_MIN_CHARS = int(os.environ.get("GRANTLENS_ATTESTATION_MIN_CHARS", "25"))
ATTESTATION_MIN_CHARS_LLM_ONLY = int(os.environ.get("GRANTLENS_ATTESTATION_MIN_CHARS_LLM", "15"))
ATTESTATION_MIN_QUOTE_WORDS = int(os.environ.get("GRANTLENS_ATTESTATION_MIN_QUOTE_WORDS", "5"))


class WorkflowError(Exception):
    def __init__(self, msg, code=409, extra=None):
        super().__init__(msg)
        self.code, self.extra = code, extra or {}


def _case(case_id):
    c = store.get_case(case_id)
    if not c:
        raise WorkflowError("No such application", 404)
    return c


def _rank(s):
    return STATES.index(s)


def case_rules(c: dict) -> list:
    """The criteria set OF THIS CASE: the locked snapshot when present, otherwise the default/assigned set."""
    snap = c.get("ruleset_snapshot")
    if snap and snap.get("rules"):
        return snap["rules"]
    return core.get_ruleset(c.get("ruleset_id"))["rules"]


def needs_countersign(c: dict, vs: list = None) -> bool:
    """A signed case with not-met/unclear criteria -> a manager must countersign before the letter is issued."""
    if _rank(c["status"]) < _rank("signed") or c.get("countersigned_by"):
        return False
    vs = vs if vs is not None else store.get_verdicts(c["id"])
    return any(v["final_verdict"] != "met" for v in vs)


# ---------- ruleset (multi-GO + versioning) ----------
def set_ruleset(case_id, ruleset_id, officer, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("The case is signed — the criteria set cannot be changed.")
    try:
        rs = core.get_ruleset(ruleset_id, require_approved=True)
    except KeyError as e:
        raise WorkflowError(str(e), 409)
    store.clear_verdicts(case_id)
    store.update_case(case_id, ruleset_id=rs["id"], ruleset_version=rs["version"], ruleset_snapshot=None,
                      status="new", review_started_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None)
    store.log(officer, f"Assigned criteria set '{rs['name']}' v{rs['version']} to {case_id} — previous results (if any) voided",
              case_id, role, {"ruleset": rs["id"], "version": rs["version"]})
    return store.get_case(case_id, with_text=False)


# ---------- AI assessment ----------
def can_assess(case_id):
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("The case is signed off — reopen it (with a reason) before re-assessing.")
    return c


def assess_stream(case_id, actor="AI"):
    """Generator of NDJSON events; every verdict is saved as soon as it is available."""
    c = can_assess(case_id)
    had = bool(store.get_verdicts(case_id))
    store.clear_verdicts(case_id)
    store.update_case(case_id, status="new", review_started_at=None, signed_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None, letter=None, letter_status=None,
                      countersigned_by=None, countersigned_at=None)
    info = core.llm.describe()
    # --- LOCK the criteria set at the first assessment (versioning) ---
    snap = c.get("ruleset_snapshot")
    if snap and snap.get("rules"):
        rs = snap
        store.log(actor, f"Using the case's LOCKED criteria set: {rs['name']} v{rs['version']}", case_id, "system",
                  {"ruleset": rs["id"], "version": rs["version"], "locked": True})
    else:
        try:
            full = core.get_ruleset(c.get("ruleset_id"), require_approved=True)
        except KeyError as e:
            raise WorkflowError(str(e), 409)
        rs = {k: full[k] for k in ("id", "name", "version", "rules")}
        store.update_case(case_id, ruleset_id=rs["id"], ruleset_version=rs["version"],
                          ruleset_snapshot=json.dumps(rs, ensure_ascii=False))
        store.log(actor, f"LOCKED criteria set for this case: {rs['name']} v{rs['version']} ({len(rs['rules'])} criteria) — "
                  "later rule changes do not affect cases in progress", case_id, "system",
                  {"ruleset": rs["id"], "version": rs["version"], "n_rules": len(rs["rules"])})
    rules = rs["rules"]
    store.log(actor, f"Started AI assessment{' (RE-assessment, old confirmations erased)' if had else ''}", case_id, "system",
              {"llm": info["model"], "backend": info["backend"],
               "pipeline": "RAG -> extract facts -> judge on facts -> guard false-pass -> cite"})
    # --- anti-cheating cross-check (deterministic, runs before the AI) ---
    cc = run_crosscheck(case_id, c["text"])
    yield {"type": "crosscheck", **cc}
    meta = {"directorate": c.get("directorate") or ""}
    for i, rule in enumerate(rules):
        yield {"type": "progress", "rule": rule["id"], "title": rule["title"], "i": i, "n": len(rules)}
        v = core.assess_rule(case_id, c["text"], rule, ruleset_id=rs["id"], meta=meta)
        store.save_ai_verdict(case_id, v)
        if v.get("guard") and v["guard"]["action"] in ("override", "flag"):
            store.log("System", f"GUARD blocked a false pass on {rule['id']}: {v['guard']['reason']} → {v['v']}",
                      case_id, "system", {"rule": rule["id"], "guard": v["guard"]})
        # the rule's own "type" (qualitative/quantitative) used to overwrite "type": "verdict" -> the UI never received the event
        yield {**v, "rule_type": v.get("type"), "type": "verdict"}
    store.update_case(case_id, status="assessed", assessed_at=store.now(),
                      llm_model=info["model"], embed_backend=rag.EMBED_BACKEND)
    vs = store.get_verdicts(case_id)
    summary = {k: sum(1 for x in vs if x["ai_verdict"] == k) for k in core.VERDICTS}
    store.log(actor, f"Completed {len(rules)} DRAFT verdicts", case_id, "system",
              {"summary": summary, "mock": info["is_mock"]})
    yield {"type": "done", "summary": summary}


def run_crosscheck(case_id: str, text: str = None, actor: str = "System") -> dict:
    """Cross-check the documents of a case; store the result in the case + log it."""
    c = _case(case_id) if text is None else None
    cc = crosscheck.run(text if text is not None else c["text"])
    store.update_case(case_id, crosscheck=json.dumps(cc, ensure_ascii=False))
    if cc["findings"]:
        store.log(actor, f"CROSS-CHECK {case_id}: {len(cc['findings'])} DISCREPANCIES across {cc['n_docs']} documents — "
                  + "; ".join(f.get("label", f["category"]) for f in cc["findings"]),
                  case_id, "system", {"risk": cc["risk"], "findings": len(cc["findings"])})
    else:
        store.log(actor, f"Cross-check {case_id}: {cc['n_docs']} documents consistent", case_id, "system",
                  {"risk": cc["risk"]})
    return cc


def attach_document(case_id: str, name: str, text: str, officer: str, role="officer"):
    """Add an attached document (before sign-off; also while Awaiting supplement). Previous AI results are voided."""
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("The case is signed — reopen it before adding documents.")
    text = text.replace("\r\n", "\n").strip()
    if len(text.split()) < 10:
        raise WorkflowError("Attached document is too short", 400)
    was_supp = c["status"] == "awaiting_supplement"
    new_text = c["text"].rstrip() + f"\n\n=== DOCUMENT: {name.strip()} ===\n\n" + text
    store.clear_verdicts(case_id)
    store.update_case(case_id, text=new_text, status="new", review_started_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None)
    rnd = f" — completes supplement round #{c.get('supplement_round') or 0}" if was_supp else ""
    store.log(officer, f"Attached document '{name}' ({len(text.split())} words){rnd} — previous AI results voided, re-assessment required",
              case_id, role, {"doc": name, "words": len(text.split()), "supplement_round": c.get("supplement_round") or 0})
    return run_crosscheck(case_id, new_text, actor=officer)


# ---------- supplement rounds (several, with a deadline) ----------
def request_supplement(case_id, items, days, officer, role="officer"):
    c = _case(case_id)
    if c["status"] not in ("in_review", "assessed"):
        raise WorkflowError("A supplement can only be requested while the case is under review / has AI results")
    items = (items or "").strip()
    if len(items) < 8:
        raise WorkflowError("State the items to supplement (≥ 8 characters)", 400)
    days = max(1, min(int(days or 15), 90))
    rnd = (c.get("supplement_round") or 0) + 1
    supp = {"round": rnd, "items": items, "requested_at": store.now(),
            "deadline": (date.today() + timedelta(days=days)).isoformat(), "by": officer}
    store.update_case(case_id, status="awaiting_supplement", supplement=json.dumps(supp, ensure_ascii=False),
                      supplement_round=rnd)
    store.log(officer, f"SUPPLEMENT REQUESTED (round #{rnd}, due {supp['deadline']}): {items}", case_id, role, supp)
    return store.get_case(case_id, with_text=False)


# ---------- review ----------
def start_review(case_id, officer, role="officer", acknowledge_coi=False, coi_reason=""):
    c = _case(case_id)
    if not officer.strip():
        raise WorkflowError("Reviewing officer name required", 400)
    if c["status"] == "in_review":
        return c
    if c["status"] != "assessed":
        raise WorkflowError(f"Cannot start the review in status '{STATE_LABELS[c['status']]}'")
    ci = coi.check(officer, c)
    if ci["level"] == "block":
        store.log("System", f"COI BLOCK: {officer} may not review {case_id} — " + " | ".join(ci["reasons"]),
                  case_id, "system", {"coi": ci})
        raise WorkflowError("Conflict of interest — cannot take this case: " + " ".join(ci["reasons"]), 409,
                            {"coi": ci})
    if ci["level"] == "warn":
        if not acknowledge_coi:
            raise WorkflowError("Conflict-of-interest warning: " + " ".join(ci["reasons"]) +
                                " — give a reason and confirm to continue.", 428,
                                {"coi": ci, "need_coi_ack": True})
        if len((coi_reason or "").strip()) < 8:
            raise WorkflowError("Proceeding despite the COI warning requires a reason (≥ 8 characters)", 400, {"coi": ci})
        store.log(officer, f"Proceeded despite the COI warning — reason: {coi_reason}. Warning: " + " | ".join(ci["reasons"]),
                  case_id, role, {"coi": ci, "reason": coi_reason})
    vs = store.get_verdicts(case_id)
    spot = random.choice(vs)
    store.update_case(case_id, status="in_review", officer=officer, review_started_at=store.now(),
                      spot_rule=spot["rule_id"], spot_ai=spot["ai_verdict"], spot_answer=None)
    store.log(officer, f"Review started. Blind spot-check: criterion {spot['rule_id']} (AI hidden until answered)",
              case_id, role, {"spot_rule": spot["rule_id"]})
    return store.get_case(case_id)


def spot_pending(c):
    return c["status"] == "in_review" and c.get("spot_rule") and not c.get("spot_answer")


def spot_check(case_id, verdict, officer, role="officer"):
    c = _case(case_id)
    if not spot_pending(c):
        raise WorkflowError("No spot-check pending")
    if verdict not in core.VERDICTS:
        raise WorkflowError("Invalid verdict", 400)
    agree = verdict == c["spot_ai"]
    store.update_case(case_id, spot_answer=verdict)
    store.log(officer, f"Spot-check {c['spot_rule']}: officer = {verdict}, AI = {c['spot_ai']} → {'MATCH' if agree else 'DIFFERS'}",
              case_id, role, {"rule": c["spot_rule"], "officer": verdict, "ai": c["spot_ai"], "agree": agree})
    return {"rule": c["spot_rule"], "officer": verdict, "ai": c["spot_ai"], "agree": agree}


def _norm_reason(s: str) -> str:
    return " ".join((s or "").lower().split())


def confirm(case_id, rule_id, final_verdict, reason, officer, role="officer", evidence_ack: bool = False):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Confirmation is only possible while the case is under review")
    if spot_pending(c):
        raise WorkflowError("Complete the blind spot-check before viewing/confirming the AI's suggestions")
    v = store.get_verdict(case_id, rule_id)
    if not v:
        raise WorkflowError("No such criterion", 404)
    if final_verdict not in FINAL_ALLOWED:
        raise WorkflowError('The final verdict must be MET / NOT MET / UNCLEAR ("not addressed" is not allowed)', 400)
    reason = (reason or "").strip()
    changed = final_verdict != v["ai_verdict"]
    guarded = "[FALSE-PASS BLOCKED]" in (v.get("note") or "") or "[SUSPECTED FALSE PASS]" in (v.get("note") or "")
    need_reason = changed or v["ai_verdict"] in ("unclear", "not_addressed") or final_verdict == "unclear"
    if need_reason and len(reason) < 8:
        raise WorkflowError("This criterion requires a reason (≥ 8 characters) before confirmation", 400)
    if guarded and final_verdict == "met" and len(reason) < 8:
        raise WorkflowError("The guard flagged a violation on this criterion — choosing MET requires a reason for overriding the warning", 400)
    # --- RISK-TIERED FRICTION: never leave the AI alone on ANY criterion without a code safety net ---
    attest_kind = None
    level = v.get("guard_level") or "llm-only"
    if final_verdict == "met" and level in ("needs-manual-guard", "llm-only"):
        strict = level == "needs-manual-guard"     # highest-risk zone: must be typed by the officer, may not borrow from the AI
        min_chars = ATTESTATION_MIN_CHARS if strict else ATTESTATION_MIN_CHARS_LLM_ONLY
        if not strict and evidence_ack and (v.get("aq") or "").strip():
            # llm-only: the officer may confirm the quotation the AI cut (the row must be opened to read it before the
            # button is available); the system records that quotation as evidence and tags the attestation kind so
            # post-audit can count it.
            reason = (reason + " | " if reason else "") + f"[AI quotation confirmed] \"{v['aq']}\""
            attest_kind = "ai_quote_ack"
        else:
            why = ("has NO code safety net (its logic cannot be expressed in code)" if strict
                   else "is purely qualitative — code has no figures to check")
            if len(reason) < min_chars:
                raise WorkflowError(
                    f"Criterion {rule_id} {why}, so a MET verdict here rests on the AI alone. You must check the application yourself and "
                    f"record evidence (≥ {min_chars} characters, PASTING a verbatim passage from the application)."
                    + ("" if strict else " Or click the button to confirm the AI's quotation."),
                    400, {"need_attestation": True, "rule": rule_id, "min_chars": min_chars,
                          "level": level, "can_ack_ai_quote": not strict and bool(v.get("aq"))})
            # ANTI-PADDING: the attestation must contain a VERBATIM passage that really exists in the application (checked by code)
            ev = verify_mod.attestation_evidence(reason, c["text"], ATTESTATION_MIN_QUOTE_WORDS)
            if not ev:
                raise WorkflowError(
                    f"The attestation for {rule_id} contains no passage that actually appears in the application. PASTE ≥ "
                    f"{ATTESTATION_MIN_QUOTE_WORDS} consecutive words verbatim from the application (the part you relied on for MET) — "
                    "code checks it; padding to length will not pass.",
                    400, {"need_verbatim_quote": True, "rule": rule_id,
                          "min_quote_words": ATTESTATION_MIN_QUOTE_WORDS})
            # ANTI-DUPLICATE: one sentence may not be pasted for several criteria
            dup = next((x for x in store.get_verdicts(case_id)
                        if x["rule_id"] != rule_id and x.get("officer_reason")
                        and _norm_reason(x["officer_reason"]) == _norm_reason(reason)), None)
            if dup:
                raise WorkflowError(
                    f"This attestation is identical to the one for criterion {dup['rule_id']} — each criterion needs its own "
                    "evidence matching its content.", 400, {"duplicate_of": dup["rule_id"], "rule": rule_id})
            attest_kind = "officer_quote"
    store.confirm_verdict(case_id, rule_id, final_verdict, reason, officer)
    ATT_LBL = {"officer_quote": "[SELF-ATTESTED — officer pasted a quotation from the application, verified by code]",
               "ai_quote_ack": "[AI QUOTATION CONFIRMED — officer read and agreed with the AI's cut]"}
    store.log(officer, f"Confirmed {rule_id} = {final_verdict}" + (f" (AI: {v['ai_verdict']} → OVERRIDDEN)" if changed else "")
              + (" " + ATT_LBL[attest_kind] if attest_kind else "")
              + (f" — reason: {reason}" if reason else ""), case_id, role,
              {"rule": rule_id, "ai": v["ai_verdict"], "final": final_verdict, "override": changed,
               "reason": reason, "guard_level": level, "attestation": attest_kind})
    return store.get_verdict(case_id, rule_id)


def unconfirm(case_id, rule_id, officer, role="officer"):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Unconfirming is only possible while the case is under review")
    store.unconfirm_verdict(case_id, rule_id)
    store.log(officer, f"Unconfirmed {rule_id}", case_id, role, {"rule": rule_id})


def sign(case_id, officer, acknowledge_fast=False, role="officer"):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Signing is only possible while the case is under review")
    vs = store.get_verdicts(case_id)
    missing = [v["rule_id"] for v in vs if not v["confirmed_at"]]
    if missing:
        raise WorkflowError(f"{len(missing)} criteria not yet confirmed: {', '.join(missing)}", 409, {"missing": missing})
    from datetime import datetime
    elapsed = (datetime.now() - datetime.fromisoformat(c["review_started_at"])).total_seconds()
    min_expected = len(vs) * MIN_SECONDS_PER_RULE
    fast = elapsed < min_expected
    if fast and not acknowledge_fast:
        store.log("System", f"WARNING: signed after {elapsed:.0f}s for {len(vs)} criteria (< {min_expected}s) — possible rubber-stamping",
                  case_id, "system", {"elapsed": round(elapsed), "min_expected": min_expected})
        raise WorkflowError(f"You reviewed {len(vs)} criteria in {elapsed:.0f} seconds — unusually fast (expected ≥ {min_expected}s). "
                            f"The warning has been logged. Confirm again if you still want to sign.", 428,
                            {"elapsed": round(elapsed), "min_expected": min_expected, "need_ack": True})
    store.update_case(case_id, status="signed", signed_at=store.now())
    overrides = sum(1 for v in vs if v["final_verdict"] != v["ai_verdict"])
    rules_by_id = {r["id"]: r for r in case_rules(c)}
    n_fb = feedback.record_signed_case(c, vs, rules_by_id, c.get("llm_model") or "", ruleset_id=c.get("ruleset_id") or "")
    if n_fb:
        store.log("System", f"Feedback loop: stored {n_fb} samples (officer overrode AI / human decided for AI) in the ground-truth store",
                  case_id, "system", {"n_samples": n_fb, "file": "data/feedback/feedback.jsonl"})
    rejection = any(v["final_verdict"] != "met" for v in vs)
    store.log(officer, f"SIGNED OFF the review ({elapsed:.0f}s, {overrides} criteria overridden vs AI"
              + (", fast sign — warning acknowledged" if fast else "") + ")"
              + (" — case has criteria not met/unclear: MANAGER COUNTERSIGNATURE REQUIRED before the letter is issued" if rejection else ""),
              case_id, role, {"elapsed": round(elapsed), "overrides": overrides, "fast_ack": fast, "rejection": rejection})
    return store.get_case(case_id, with_text=False)


# ---------- countersignature (manager) ----------
def countersign(case_id, manager, role="manager"):
    from .screening import norm
    c = _case(case_id)
    if c["status"] != "signed":
        raise WorkflowError("Countersigning is only possible after the reviewing officer has signed off")
    if c.get("countersigned_by"):
        raise WorkflowError(f"This case was already countersigned by {c['countersigned_by']}")
    if not needs_countersign(c):
        raise WorkflowError("The case meets every criterion — no countersignature needed")
    if norm(manager) == norm(c.get("officer") or ""):
        raise WorkflowError("The countersigner must be DIFFERENT from the reviewing officer", 400)
    if coi.role_of(manager) != "manager":
        raise WorkflowError(f"'{manager}' has no manager role in the officer register — cannot countersign", 403)
    ci = coi.check(manager, c)
    if ci["level"] == "block":
        store.log("System", f"COI BLOCK at countersign: {manager} may not sign {case_id} — " + " | ".join(ci["reasons"]),
                  case_id, "system", {"coi": ci})
        raise WorkflowError("Conflict of interest — this manager cannot sign this case: " + " ".join(ci["reasons"]), 409)
    store.update_case(case_id, countersigned_by=manager, countersigned_at=store.now())
    store.log(manager, f"MANAGER COUNTERSIGNED: agrees with the rejection/supplement outcome by {c.get('officer')} — "
              "the outcome letter may be issued", case_id, role, {"officer": c.get("officer")})
    return store.get_case(case_id, with_text=False)


def reopen(case_id, officer, reason, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("The case is not signed off, nothing to reopen")
    if len((reason or "").strip()) < 8:
        raise WorkflowError("Reopening requires a reason (≥ 8 characters)", 400)
    store.update_case(case_id, status="in_review", signed_at=None, letter=None, letter_status=None,
                      letter_approved_at=None, review_started_at=store.now(),
                      countersigned_by=None, countersigned_at=None)
    store.log(officer, f"REOPENED signed case — reason: {reason} (previous countersignature voided)", case_id, role,
              {"reason": reason})
    return store.get_case(case_id, with_text=False)


# ---------- letter ----------
def draft_letter(case_id, officer, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("The outcome letter can only be drafted after the officer signs off 100% of the criteria")
    vs = store.get_verdicts(case_id)
    if needs_countersign(c, vs):
        raise WorkflowError("The case has NOT MET / UNCLEAR criteria — a manager must countersign before the outcome "
                            "letter is issued.", 428, {"need_countersign": True})
    rules_by_id = {r["id"]: r for r in case_rules(c)}
    finals = [{"rule": v["rule_id"], "title": rules_by_id.get(v["rule_id"], {}).get("title", v["rule_id"]),
               "rule_quote": v["rq"], "applicant_quote": v["aq"],
               "verdict": v["final_verdict"], "officer_reason": v["officer_reason"] or ""}
              for v in vs]
    text = core.draft_letter(case_id, c["applicant"] or case_id, finals, officer=c.get("officer") or officer,
                             org=c.get("org") or "")
    store.update_case(case_id, status="letter_drafted", letter=text, letter_status="draft")
    store.log("AI", "Drafted outcome letter from signed verdicts", case_id, "system", {"words": len(text.split())})
    return text


def approve_letter(case_id, officer, edited_text=None, role="officer"):
    c = _case(case_id)
    if c["status"] != "letter_drafted":
        raise WorkflowError("No draft letter to approve")
    text = (edited_text or c["letter"] or "").strip()
    edited = bool(edited_text) and edited_text.strip() != (c["letter"] or "").strip()
    store.update_case(case_id, status="letter_approved", letter=text, letter_status="approved",
                      letter_approved_at=store.now())
    store.log(officer, "APPROVED outcome letter content" + (" (hand-edited)" if edited else ""), case_id, role,
              {"edited": edited})
    return store.get_case(case_id, with_text=False)


# ---------- view helpers ----------
def case_view(case_id):
    """Return case + verdicts + the locked criteria set; HIDE the AI results while a spot-check is pending."""
    c = _case(case_id)
    vs = store.get_verdicts(case_id)
    masked = spot_pending(c)
    if masked:
        for v in vs:
            for k in ("ai_verdict", "ai_confidence", "facts", "aq", "note", "chunk_id", "retrieval", "needs_attention"):
                v[k] = None
    snap = c.get("ruleset_snapshot") or {}
    rules = snap.get("rules") or core.get_ruleset(c.get("ruleset_id"))["rules"]
    rs_meta = {"id": snap.get("id") or c.get("ruleset_id") or core.get_ruleset()["id"],
               "name": snap.get("name") or core.get_ruleset(c.get("ruleset_id"))["name"],
               "version": snap.get("version") or c.get("ruleset_version") or core.get_ruleset(c.get("ruleset_id"))["version"],
               "locked": bool(snap)}
    c.pop("ruleset_snapshot", None)  # already returned as rules; avoids a double payload
    return {"case": c, "verdicts": vs, "ai_masked": masked, "rules": rules, "ruleset": rs_meta,
            "needs_countersign": needs_countersign(c, vs), "states": STATES, "state_labels": STATE_LABELS}
