"""core.py — assessment pipeline (shared by the API and the evaluation scripts).

For EACH criterion, separate processing layers:
  Layer A  RAG      : retrieve the top-k passages of the application relevant to the criterion.
  Layer B  EXTRACT  : LLM pass 1 — extracts neutral FACTS only (fact, chunk_id, key_phrase). No judgement.
  Layer C  JUDGE    : LLM pass 2 — sees only the normalised fact list + the criterion. It NEVER sees the original text
                      => the applicant's writing style and grammar cannot influence the verdict (language-bias control).
  Layer D  CITE     : code cuts the VERBATIM sentence from the chunk using the key_phrase (citation-by-retrieval)
                      + a second-layer string match.
  Layer E  GUARD    : code re-checks quantitative / pattern-checkable rules and blocks false passes (guards.py).
"""
import json
from pathlib import Path
from functools import lru_cache
from . import llm
from .rag import Retriever
from .verify import extract_quote, quote_in_source

DATA = Path(__file__).resolve().parent.parent / "data"
VERDICTS = ["met", "not_met", "unclear", "not_addressed"]
CONFIDENCE = ["high", "medium", "low"]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array", "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "chunk_id": {"type": "integer"},
                    "key_phrase": {"type": "string"},
                },
                "required": ["fact", "chunk_id", "key_phrase"],
            },
        },
        "coverage": {"type": "string", "enum": ["direct", "partial", "none"]},
    },
    "required": ["facts", "coverage"],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "supporting_fact": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["verdict", "confidence", "supporting_fact", "note"],
}

SYS_EXTRACT = """You are a FACT EXTRACTOR for a grants office. You do NOT judge eligibility.
Given one eligibility rule and passages from an application, list the plain FACTS in the passages that are relevant to the rule.
Rules for facts:
- Each fact is ONE neutral, style-free statement in standard English (e.g. "Appointment is 40% tenure-track as Assistant Professor", "Total budget is $350,000 over 5 years", "Directorate is BIO", "Cover page lists a co-PI").
- Preserve numbers, dates, percentages, names of directorates, page counts EXACTLY as stated.
- Ignore grammar, vocabulary and tone completely. Never describe the writing quality.
- chunk_id: the id of the passage the fact comes from. key_phrase: 3-8 words copied VERBATIM from that passage, pointing at the fact.
- If the passages contain nothing about the rule, return an empty facts list and coverage "none".
- coverage: "direct" if the facts settle the rule, "partial" if related but incomplete, "none" if nothing relevant."""

SYS_JUDGE = """You are an ELIGIBILITY JUDGE for a grants office. You only see (1) one rule and (2) a numbered list of normalized FACTS extracted from the application. You never see the application text, so writing style cannot influence you.
Decide strictly from the facts:
- "met": the facts clearly satisfy the rule.
- "not_met": the facts clearly violate the rule (e.g. a number below a stated minimum, a forbidden item present, a required item absent when its absence is stated).
- "unclear": facts exist but are insufficient or ambiguous to decide; a human must request or judge more information.
- "not_addressed": no fact relates to the rule.
Check thresholds carefully: compare numbers to the rule's minimum/maximum; note when a threshold depends on a directorate (e.g. BIO/ENG/OPP) and use the directorate fact if present.
Never guess. If facts are missing or ambiguous, answer "unclear" or "not_addressed".
supporting_fact: the 1-based index of the single fact that best supports your verdict (0 if no facts).
confidence: "high" when the facts are explicit and decisive; "medium" when some interpretation is needed; "low" when you are close to unclear.
note: one short sentence in English explaining the mapping from facts to verdict."""

SYS_LETTER = """You draft outcome letters for a grants office. Write in PLAIN English (CEFR B1: short sentences, common words, no jargon). Max 350 words.
Structure:
1) Decision line (eligible / not eligible / more information needed).
2) For each rule that is not met or needs information: name the rule in plain words, put the exact rule fragment and the exact application fragment in quotation marks (use the quotes given, verbatim — do not edit them), and one plain sentence why. If the officer wrote a reason, reflect it in plain words.
3) "What you can do": how to fix or what to send.
4) "How to appeal": they may appeal in writing within 30 days to the review board and may request the full assessment record.
Never comment on the applicant's English or writing style. Output plain text only, no markdown."""

SYS_RULES = """You convert a funding guideline into a checklist of ELIGIBILITY rules for automated evidence mapping.
Only include eligibility / compliance requirements (who may apply, PI status, limits, required documents, page limits, budget minimums, prohibitions). Skip review criteria and general advice.
Each rule: id (R01, R02...), type ("quantitative" if it contains a number/threshold/count, else "qualitative"), title (short English title), quote (the VERBATIM sentence(s) from the guideline, max 60 words, do not paraphrase)."""

RULES_SCHEMA = {
    "type": "object",
    "properties": {"rules": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "type": {"type": "string", "enum": ["quantitative", "qualitative"]},
        "title": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["id", "type", "title", "quote"]}}},
    "required": ["rules"],
}


RULESETS_DIR = DATA / "rulesets"


@lru_cache(maxsize=1)
def load_rulesets() -> dict:
    """Multi-GO: one criteria set per grant round / fund, with a version. {id -> ruleset}."""
    idx = json.loads((RULESETS_DIR / "index.json").read_text(encoding="utf-8"))
    out = {}
    for e in idx["rulesets"]:
        rs = json.loads((RULESETS_DIR / e["file"]).read_text(encoding="utf-8"))
        rs.setdefault("name", e.get("name", rs["id"]))
        rs.setdefault("version", e.get("version", "1"))
        rs.setdefault("region", e.get("region", ""))
        out[rs["id"]] = rs
    out["_default"] = idx.get("default") or next(iter(out))
    return out


def get_ruleset(ruleset_id: str = None, require_approved: bool = False) -> dict:
    all_rs = load_rulesets()
    rid = ruleset_id or all_rs["_default"]
    if rid not in all_rs:
        raise KeyError(f"No criteria set '{rid}'")
    rs = all_rs[rid]
    if require_approved and rs.get("status", "approved") != "approved":
        raise KeyError(f"Criteria set '{rid}' is a DRAFT — a manager must approve it "
                       f"(with a guard-coverage check) before it can be assigned to cases")
    return rs


def reload_rulesets():
    load_rulesets.cache_clear()


def load_rules():
    """The default criteria set (kept for older call sites)."""
    return get_ruleset()["rules"]


@lru_cache(maxsize=1)
def load_manifest():
    return json.loads((DATA / "applications" / "manifest.json").read_text(encoding="utf-8"))["cases"]


def load_app_text(case: dict) -> str:
    return (DATA / "applications" / case["file"]).read_text(encoding="utf-8")


_retrievers: dict = {}


def get_retriever(case_id: str, text: str) -> Retriever:
    key = (case_id, hash(text))
    if key not in _retrievers:
        _retrievers.clear() if len(_retrievers) > 64 else None
        _retrievers[key] = Retriever(text)
    return _retrievers[key]


def _short(q: str, n: int = 45) -> str:
    w = q.split()
    return q if len(w) <= n else " ".join(w[:n]) + " ..."


def judge_consistency(verdict: str, confidence: str, note: str, raw_idx, n_facts: int, coverage: str):
    """CONSISTENCY LOCK for the judgement pass (code, no second model call).

    "NOT MET" affects the applicant's rights, so it MUST point at a fact that states the violation.
    If the model returns not_met but (a) points at no fact (supporting_fact = 0 / out of range), or
    (b) the extraction pass reported nothing relevant (coverage = none) -> that is "missing information", not a
    violation: downgrade to UNCLEAR and route to the officer. Observed on 2026-09-14 (Wine W06): a valid application
    was judged "not met" with the reason "no information about ownership or lease".

    PROMPT UNCHANGED: a 2026-09-19 trial added two sentences to SYS_JUDGE ("not_met REQUIRES one fact..."). A
    controlled re-measurement showed the two prompt versions produce IDENTICAL aggregate results (6 funds + NSF), i.e.
    the added sentences had no measurable effect -> dropped; every behaviour change lives in this function (code:
    testable, repeatable). Note when reading the numbers: the NSF set was 24/24 on 09-07 and 23/24 on 09-19 with BOTH
    prompt versions — the cause is NOT the prompt or this function but bit-level non-reproducibility across runs
    (see README, phase 6).

    Previously the line `idx = supporting_fact or 1` coerced 0 into 1 -> the system still quoted fact #1 as
    "evidence" for a verdict the model itself said had no supporting fact.
    Returns (verdict, confidence, note, idx) — idx is always valid for the citation layer."""
    valid = isinstance(raw_idx, int) and 1 <= raw_idx <= n_facts
    if verdict == "not_met" and (not valid or coverage == "none"):
        why = "could not point to any fact stating a violation" if not valid else "the extraction pass reported nothing relevant"
        note = (f"[CONSISTENCY LOCK] AI concluded NOT MET but {why} — missing information is not a violation; "
                f"downgraded to UNCLEAR for the officer to decide. | LLM: {note}")
        verdict, confidence = "unclear", "low"
    return verdict, confidence, note, (raw_idx if valid else 1)


def assess_rule(case_id: str, text: str, rule: dict, k: int = 3, ruleset_id: str = None, meta: dict = None) -> dict:
    ret = get_retriever(case_id, text)
    hits = ret.retrieve(f"{rule['title']} — {rule['quote']}", k=k)
    valid_ids = {h["id"] for h in hits}
    passages = "\n".join(f"[chunk {h['id']}] {h['text']}" for h in hits)

    # ---- Layer B: EXTRACT (pass 1) ----
    ex = llm.chat_json(
        SYS_EXTRACT,
        f"RULE {rule['id']}: \"{rule['quote']}\"\n\nPASSAGES:\n{passages}\n\nExtract the relevant facts.",
        EXTRACT_SCHEMA,
    )
    facts = []
    for f in (ex.get("facts") or [])[:4]:
        if not isinstance(f, dict) or not f.get("fact"):
            continue
        cid = f.get("chunk_id")
        facts.append({"fact": str(f["fact"]).strip(), "chunk_id": cid if cid in valid_ids else hits[0]["id"],
                      "key_phrase": str(f.get("key_phrase", "")).strip()})
    coverage = ex.get("coverage", "none" if not facts else "partial")

    # ---- Layer C: JUDGE (pass 2) — facts only, never the original text ----
    if facts:
        from . import feedback
        fact_list = "\n".join(f"{i + 1}. {f['fact']}" for i, f in enumerate(facts))
        jd = llm.chat_json(
            SYS_JUDGE,
            f"CASE {case_id} — RULE {rule['id']}: \"{rule['quote']}\"\n\nFACTS (normalized, style removed; coverage={coverage}):\n{fact_list}"
            f"{feedback.fewshot_block(rule['id'], ruleset_id)}\n\nDecide.",
            JUDGE_SCHEMA,
        )
        verdict = jd.get("verdict") if jd.get("verdict") in VERDICTS else "unclear"
        confidence = jd.get("confidence") if jd.get("confidence") in CONFIDENCE else "low"
        note = jd.get("note", "")
        verdict, confidence, note, idx = judge_consistency(verdict, confidence, note, jd.get("supporting_fact"),
                                                           len(facts), coverage)
    else:
        verdict, confidence, idx, note = "not_addressed", "high", 0, "No facts in the application relate to this criterion."

    # ---- Layer D: CITE — code cuts the verbatim sentence ----
    if facts:
        sf = facts[idx - 1]
        chunk = next(h for h in hits if h["id"] == sf["chunk_id"])
        aq = extract_quote(chunk["text"], sf["key_phrase"])
        chunk_id = chunk["id"]
    else:
        aq, chunk_id = "", hits[0]["id"]
    cite_ok = (aq == "") or quote_in_source(aq, text)

    # ---- Layer E: GUARD — code blocks false passes on rules checkable by figures / text patterns ----
    from . import guards
    guard = guards.check(ruleset_id or get_ruleset()["id"], rule, verdict, text, meta or {})
    if guard and guard["action"] == "override":
        note = f"[FALSE-PASS BLOCKED] {guard['reason']}. (LLM returned 'met' but the code check of the figures found a violation — quantitative rules are decided by code.) | LLM: {note}"
        verdict, confidence = guard["verdict"], "high"
    elif guard and guard["action"] == "flag":
        note = f"[SUSPECTED FALSE PASS] {guard['reason']} — downgraded to UNCLEAR, officer must decide. | LLM: {note}"
        verdict, confidence = "unclear", "low"

    # Protection level of this criterion -> the workflow layer uses it to enforce friction when an officer confirms MET
    guard_level = guards.rule_guard_level(ruleset_id or get_ruleset()["id"], rule)
    # A criterion without a code safety net where the AI says MET always needs attention —
    # this is exactly where a false pass slips through if the officer skims (customer critique).
    unguarded_pass = guard_level == "needs-manual-guard" and verdict == "met"
    needs_attention = (verdict in ("unclear", "not_addressed") or confidence == "low"
                       or not cite_ok or bool(guard) or unguarded_pass)

    return {
        "guard": guard, "guard_level": guard_level,
        "r": rule["id"], "title": rule["title"], "type": rule["type"],
        "v": verdict, "confidence": confidence,
        "facts": facts, "coverage": coverage, "supporting_fact": idx,
        "rq": _short(rule["quote"]), "aq": aq, "chunk_id": chunk_id,
        "retrieval_scores": [{"chunk": h["id"], "score": h["score"]} for h in hits],
        "cite_rule_ok": True, "cite_app_ok": cite_ok,
        "needs_attention": needs_attention,
        "note": note,
    }


def assess_case(case_id: str, text: str, progress=None, ruleset_id: str = None, meta: dict = None,
                only_rules=None):
    """Generator: yields one verdict at a time so the API can stream progress.

    only_rules: restrict to a subset of rules (used for the [TARGET] measurement — each case only needs the rule
    with the planted violation, not the whole set). Default None = assess every rule as before.
    """
    rules = get_ruleset(ruleset_id)["rules"]
    if only_rules:
        rules = [r for r in rules if r["id"] in set(only_rules)]
    for i, rule in enumerate(rules):
        if progress:
            progress(rule["id"], i, len(rules))
        yield assess_rule(case_id, text, rule, ruleset_id=ruleset_id, meta=meta)


SYS_TEMPLATE = """You are a compliance officer designing an OUTCOME-LETTER TEMPLATE for a grants office.
You are given the office's RULES/REGULATIONS about how decision letters must be written (mandatory sections, deadlines, tone, legal references...). Reverse-engineer them into ONE reusable letter template.
Requirements:
- Follow EVERY requirement in the rules exactly (sections, order, deadlines, named bodies, required statements). Do not invent requirements that are not in the rules; where the rules are silent, use this safe default skeleton: decision -> findings with two-sided verbatim quotes -> what the applicant can do -> right to appeal.
- Structure: letterhead lines, date/reference, salutation, numbered sections with UPPERCASE headings, sign-off.
- Use {PLACEHOLDERS} in curly braces for variable content: {APPLICANT_NAME}, {CASE_ID}, {DATE}, {OFFICER_NAME}, {ORGANIZATION_LETTERHEAD}, and section bodies described in (parentheses) telling the writer what to fill in.
- Language of the template follows the language the rules are written in. Keep instructions in parentheses short.
- Output ONLY the template text. No commentary, no markdown fences."""


def generate_letter_template(rules_text: str) -> str:
    return llm.chat_text(SYS_TEMPLATE, f"OFFICE RULES ABOUT DECISION LETTERS:\n{rules_text[:8000]}\n\nProduce the template.", max_tokens=900).strip()


def load_letter_template() -> str:
    """The office's letter template (data/letter-template.txt) — a first line in [brackets] is an internal note and is skipped."""
    p = DATA / "letter-template.txt"
    if not p.exists():
        return ""
    lines = p.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].lstrip().startswith("["):
        lines = lines[1:]
    return "\n".join(lines).strip()


def draft_letter(case_id: str, applicant: str, finals: list, officer: str = "", org: str = "") -> str:
    from datetime import date
    fails = [f for f in finals if f["verdict"] != "met"]
    user = (f"Applicant: {applicant} (case {case_id}, submitting organisation: {org or 'not stated'}).\n"
            f"Reviewing officer (signs the letter): {officer or 'not stated'}. Today's date: {date.today().isoformat()}.\n"
            f"Officer-approved verdicts (JSON):\n{json.dumps(finals, ensure_ascii=False)}\n")
    if not fails:
        user += "All rules met — write a short eligibility-confirmed letter (still include the appeal note).\n"
    tpl = load_letter_template()
    if tpl:
        user += ("\nThe organisation REQUIRES this letter template. Follow its structure, headings and order EXACTLY; "
                 "keep the plain-B1-English rule. Fill {PLACEHOLDERS} ONLY with values given above or in the verdicts. "
                 "NEVER invent names, addresses, emails, phone numbers or dates — if a value is not given, leave the "
                 "{PLACEHOLDER} exactly as it is for staff to complete:\n---TEMPLATE---\n"
                 f"{tpl}\n---END TEMPLATE---")
    return _sanitize_letter(llm.chat_text(SYS_LETTER, user, max_tokens=800), user)


def _sanitize_letter(letter: str, allowed: str) -> str:
    """Deterministic blocking layer: an email / phone number / date that does not appear in the input data was
    invented by the model -> replaced with a placeholder for the officer to fill in. Never trust the prompt alone."""
    import re
    from datetime import date
    letter = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+",
                    lambda m: m.group(0) if m.group(0) in allowed else "{CONTACT EMAIL — to be completed by staff}", letter)
    letter = re.sub(r"\+?\d[\d ()\-]{7,}\d",
                    lambda m: m.group(0) if m.group(0) in allowed else "{CONTACT PHONE — to be completed by staff}", letter)
    today = date.today().isoformat()
    letter = re.sub(r"\b\d{4}-\d{2}-\d{2}\b",
                    lambda m: m.group(0) if (m.group(0) == today or m.group(0) in allowed)
                    else "{DEADLINE — to be set by staff}", letter)
    return letter


def extract_rules(guideline_text: str) -> list:
    out = llm.chat_json(SYS_RULES, f"GUIDELINE TEXT:\n{guideline_text[:12000]}", RULES_SCHEMA, max_tokens=2500)
    rules = out.get("rules") or []
    # Defensive layer: the quote must appear verbatim in the guideline, otherwise it is flagged
    norm = " ".join(guideline_text.split()).lower()
    for r in rules:
        r["verbatim_ok"] = " ".join(str(r.get("quote", "")).split()).lower() in norm
    return rules
