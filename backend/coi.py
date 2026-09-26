"""coi.py — conflict-of-interest check before an officer takes a case.

Risk: the applicant may belong to the same organisation as the reviewing officer.
Officer register: data/officers.json (name + list of affiliated organisations). Rules, checked ON THE SERVER:
  BLOCK : the officer's organisation matches the applicant organisation (score ≥ 0.85) — the officer may not review,
          the case must be reassigned; there is no "acknowledge and continue" path.
  WARN  : the officer's name matches/resembles the applicant, the officer's name appears in the application text,
          or an organisation matches at the suspicious level (0.70–0.85) — the officer may continue but must give a
          reason, and everything is logged.
An officer NOT in the register -> affiliation history cannot be checked: returns a "not declared" warning.
"""
import json
from functools import lru_cache
from pathlib import Path
from .screening import norm, score

OFFICERS_FILE = Path(__file__).resolve().parent.parent / "data" / "officers.json"
ORG_BLOCK, ORG_WARN, NAME_WARN = 0.85, 0.70, 0.82


@lru_cache(maxsize=1)
def _load_file():
    if not OFFICERS_FILE.exists():
        return {}
    return json.loads(OFFICERS_FILE.read_text(encoding="utf-8"))


def load_officers():
    return _load_file().get("officers", [])


def sla_days() -> dict:
    """Processing deadline (days) per status — the UI warns when overdue."""
    return _load_file().get("sla_days", {})


def reload():
    _load_file.cache_clear()


def role_of(name: str):
    """Role in the officer register: 'officer' | 'manager' | 'auditor' | None (not declared)."""
    q = norm(name)
    rec = next((o for o in load_officers() if norm(o["name"]) == q), None)
    return (rec or {}).get("role")


def check(officer_name: str, case: dict) -> dict:
    """case needs: applicant, org, text. Returns {level: 'none'|'warn'|'block', reasons: [...], registered: bool}."""
    qname = norm(officer_name)
    rec = next((o for o in load_officers() if norm(o["name"]) == qname), None)
    reasons, level = [], "none"

    def bump(lv):
        nonlocal level
        order = ["none", "warn", "block"]
        if order.index(lv) > order.index(level):
            level = lv

    case_org, applicant, text_norm = case.get("org") or "", case.get("applicant") or "", norm(case.get("text") or "")

    if rec:
        for aff in rec.get("affiliations", []):
            s = score(norm(aff), norm(case_org)) if case_org else 0
            if s >= ORG_BLOCK:
                bump("block")
                reasons.append(f"The officer belongs/belonged to '{aff}' — same as the applicant organisation '{case_org}' (match {s:.0%}). The case must go to another officer.")
            elif s >= ORG_WARN:
                bump("warn")
                reasons.append(f"The officer's affiliated organisation '{aff}' is a possible match for '{case_org}' ({s:.0%}).")
            if norm(aff) and norm(aff) in text_norm and score(norm(aff), norm(case_org)) < ORG_WARN:
                bump("warn")
                reasons.append(f"The officer's affiliated organisation '{aff}' appears in the application text.")
    else:
        bump("warn")
        reasons.append("Officer not declared in the officer register (data/officers.json) — affiliation history cannot be checked; give a reason to continue.")

    s = score(qname, norm(applicant)) if applicant else 0
    if s >= NAME_WARN:
        bump("warn")
        reasons.append(f"Officer name matches/resembles the applicant '{applicant}' ({s:.0%}).")
    if qname and qname in text_norm:
        bump("warn")
        reasons.append("Officer name appears verbatim in the application (possibly a referee / collaborator).")

    return {"level": level, "reasons": reasons, "registered": bool(rec)}
