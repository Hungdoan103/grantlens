"""casegen.py — AI-GENERATED LABELLED TEST SETS per fund (ruleset), with anti-noise mechanisms and label approval.

Standard measurement chain for EACH fund (one fund's figures are NEVER used for another):
    --gen  ->  review the texts (--confirm / --dispute / --flag-weak)  ->  --approve  ->  --eval  ->  [TARGET]

Why the labels can be trusted (and why a human reviewer is STILL required):
  1. THE VIOLATION IS FIXED BY CODE FIRST. Step 1: the LLM proposes ONE factual sentence that violates the target rule;
     that sentence must be confirmed as a real violation by the guard (code) or by the verifier. Step 2: the LLM writes
     the application and MUST contain that sentence VERBATIM — checked by code. (Fix of 2026-09-12: merely instructing
     "violate rule X" made the LLM write a perfectly valid application — 7/7 cases in Cyber Skills + Female Founders
     failed exactly this way.)
  2. ANSWER LEAKS BLOCKED BY CODE: text that mentions rule IDs or comments on itself ("violates / satisfies /
     meets all criteria...") is rejected (the old Wine set contained "violating W08" and was withdrawn).
  3. GUARD CROSS-CHECK: if a guard fires on a rule other than the target -> rewrite. For a quantitative violation
     confirmed by the guard, the guard must STILL fire on the full text -> blocks texts where another figure overrides
     the violation sentence.
  4. THE VERIFIER uses a DIFFERENT MODEL from the system under test (GRANTLENS_CASEGEN_MODEL) and asks TWO DIFFERENTLY
     PHRASED questions ("is it satisfied?" / "is it violated?"). At temperature 0, repeating the same question gives the
     same vote every time — that is not an independent vote. The verifier only FLAGS; the reviewer decides; while a flag
     is unresolved the set CANNOT be approved — avoids silently dropping hard cases (selection bias that flatters the numbers).
  5. CONTROL: in 'target' scope the target rule is also assessed on the CLEAN application. Without a control a
     "reject everything" system would also score 100% on catching violations.
  6. Labels are stored with approved=false; editing labels after approval revokes the approval; eval marks results
     provisional while unapproved.

CLI:
  python -m backend.casegen <ruleset_id> --gen [N] [--target-only]     generate a test set
  python -m backend.casegen <ruleset_id> --confirm <case_id> "reason"   confirm the violation is real despite a verifier flag
  python -m backend.casegen <ruleset_id> --confirm-control <case_id> <rule_id> "reason"   clean case clearly satisfies the rule -> control
  python -m backend.casegen <ruleset_id> --dispute <case_id> "reason"   remove a mislabelled case from the metric
  python -m backend.casegen <ruleset_id> --flag-weak <case_id> <rule_id> "reason"
  python -m backend.casegen <ruleset_id> --approve "Reviewer name"      approve the labels
  python -m backend.casegen <ruleset_id> --eval                         measure
Environment: GRANTLENS_CASEGEN_MODEL = model that writes + verifies labels (default: the system model).
"""
import json, os, re, sys
from datetime import datetime
from . import core, llm, guards

GEN_DIR = core.DATA / "applications" / "generated"
LBL_DIR = core.DATA / "labels"
GEN_MODEL = os.environ.get("GRANTLENS_CASEGEN_MODEL") or None
VERIFIER_MODEL = os.environ.get("GRANTLENS_VERIFIER_MODEL") or GEN_MODEL
# The planner only returns schema-enforced JSON -> a "thinking-only" model works there; the free-text writer must NOT be one.
PLANNER_MODEL = os.environ.get("GRANTLENS_PLANNER_MODEL") or GEN_MODEL


def _gen_model_name() -> str:
    return GEN_MODEL or llm.describe()["model"]


def _planner_model_name() -> str:
    return PLANNER_MODEL or llm.describe()["model"]


def _verifier_model_name() -> str:
    return VERIFIER_MODEL or llm.describe()["model"]


SYS_PLAN = """You design ONE test fact for an eligibility-screening test.
You are given ONE eligibility rule. Write ONE short factual sentence (at most 35 words) that a grant applicant could plainly write about itself and that makes the applicant clearly FAIL this rule.
- The sentence must be about EXACTLY the same topic as the rule and must directly contradict what the rule demands (or put the applicant inside a category the rule excludes). A fact about any other topic is useless.
- State a concrete, specific fact: an exact number clearly on the failing side of any threshold (never at the boundary), a named entity type, or an explicit status.
- Write it as a plain statement about a named organisation.
- Do NOT mention rules, requirements, criteria, eligibility or compliance, and do not use words such as violate, breach, satisfy, fail. No rule identifiers.
Examples (other programs):
  RULE: "Applicants must have traded for at least three years."
  FACT: "Kestrel Bakery Pty Ltd began trading in March 2024, eighteen months before this application."
  RULE: "You are not eligible if you are a trust or a partnership."
  FACT: "Harbour Street Traders operates as a family trust managed by two individual trustees."
  RULE: "Each application must include at least two partner organisations."
  FACT: "Coastline Health Ltd is submitting this application alone, with no partner organisations involved."
Return JSON: {"fact": "..."}"""

PLAN_SCHEMA = {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}

SYS_GEN = """You write a REALISTIC grant application summary used to test an eligibility-screening system.
You are given the verbatim rules of one grant program and, optionally, ONE MARKER TOPIC.
Write the summary in English, 200-300 words, as short labeled paragraphs (Applicant, entity and registration details, then one paragraph per topic). Plain text, no markdown.
- If a MARKER TOPIC is given: do NOT write that fact yourself. Put the exact marker [[FACT]] on its own line inside the paragraph about that topic (it will be replaced by the given sentence), use the same organisation name as that sentence, and write NOTHING else about that topic - no figure for the same quantity, no correction, no exemption, no "however".
- For every other rule: state an explicit plain fact showing the applicant clearly meets it (numbers comfortably on the passing side, required items present, excluded statuses explicitly negated). Do not introduce related entities, equivalents, government bodies or exemptions unless a rule explicitly allows them.
- Write like a real applicant. NEVER use rule numbers, and never use the words: rule, requirement, criteria, eligibility, guidelines, minimum, maximum, threshold, violate, breach, satisfy, "as required". Never say the applicant meets or fails anything, and do not add a concluding summary.
- Use realistic, fresh Australian or US organisation and person names."""

SYS_VERIFY = """You are a strict evidence checker. You are given an application text and ONE eligibility rule.
Answer ONLY whether the APPLICATION TEXT contains explicit evidence that the applicant SATISFIES the rule.
- "yes"     : the text states facts that clearly satisfy the rule.
- "no"      : the text states a fact that clearly violates the rule.
- "unclear" : the text is silent, ambiguous, or relies on an arrangement the rule does not clearly permit.
Judge only what is written. "evidence" = one short quote, at most 25 words."""

SYS_VERIFY_VIOL = """You are a strict evidence checker. You are given an application text and ONE eligibility rule.
Answer ONLY whether the APPLICATION TEXT states a fact that makes the applicant FAIL the rule (including falling into a category the rule excludes).
- "yes"     : the text states a fact that clearly fails the rule.
- "no"      : the text states facts showing the applicant clearly meets the rule.
- "unclear" : the text is silent or ambiguous.
Judge only what is written. "evidence" = one short quote, at most 25 words."""

VERIFY_SCHEMA = {"type": "object",
                 "properties": {"answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
                                "evidence": {"type": "string"}},
                 "required": ["answer", "evidence"]}


# ---------------- mechanical checks (no LLM) ----------------
_META_PAT = re.compile(
    r"\b(violat(?:e|es|ed|ing|ion)|breach(?:es|ed|ing)?|non-?complian(?:ce|t)|fails? to (?:meet|satisfy)|"
    r"satisf(?:y|ies|ied|ying)|in compliance with|as required(?: by)?|meeting the requirements?|"
    r"(?:in line with|align(?:s|ed)? with) (?:the |all )?(?:program(?:me)?'?s? )?guidelines|"
    r"eligibility (?:rules?|criteri\w+|requirements?)|meets? all|all (?:other )?(?:eligibility )?(?:criteria|requirements)|"
    r"(?:minimum|maximum|required)[^.\n]{0,25}(?:requirement|threshold|limit)|(?:above|below|exceeds?|under) the threshold|"
    r"(?:exceed\w*|above|over|below|under|meets?|within|short of) the (?:required|stipulated|minimum|maximum)|stipulated|"
    r"thresholds?|falling short|short of the|benchmark|maximum allowable|allowable claim|"
    r"(?:fulfil\w*|meet\w*|adher\w*|align\w*) (?:to |with )?(?:the|our|all|this) (?:[\w-]+ ){0,3}"
    r"(?:requirements?|commitments?|constraints?|expectations?|limits?|obligations?)|"
    r"(?:exceed\w*|within|under|over|beyond) the (?:[\w-]+ ){0,3}limit|"
    r"align\w* (?:directly )?with the (?:\w+ ){0,3}(?:program(?:me)?|fund|initiative)'?s? (?:focus|aims?|objectives?|priorities)|"
    r"this rule|the rules?|target rule)\b", re.I)


def _leak_check(ruleset: dict, text: str):
    """Block ANSWER LEAKS with CODE (no LLM involved).

    Generated test applications tend to confess: "This violates C01", "satisfying W08", "meets all criteria".
    Real applications NEVER say that, and if it slips through the system under test may answer correctly because it
    READ THE ANSWER, not because it reasoned -> inflated figures. Detected -> rewrite.
    """
    problems = []
    ids = sorted({r["id"] for r in ruleset["rules"]}, key=len, reverse=True)
    hit_ids = [i for i in ids if re.search(rf"\b{re.escape(i)}\b", text)]
    if hit_ids:
        problems.append("rule IDs leaked: " + ", ".join(hit_ids))
    m = _META_PAT.search(text)
    if m:
        problems.append(f"sentence commenting on the rules: '{m.group(0)}'")
    if "[[FACT]]" in text:
        problems.append("leftover [[FACT]] marker from the generator")
    return problems


def _norm_cmp(s: str) -> str:
    return re.sub(r"\s+", " ", guards.normalise(s or "")).strip().strip('"').rstrip(".").lower()


def _contains(text: str, fact: str) -> bool:
    """Does the text contain the fixed violation sentence VERBATIM (compared after whitespace/punctuation/currency normalisation)?"""
    return bool(fact) and _norm_cmp(fact) in _norm_cmp(text)


def _insert_fact(text: str, fact: str):
    """CODE inserts the fixed violation sentence at the [[FACT]] marker — not left to the LLM to copy (small models
    copy inaccurately or paraphrase). Without a marker it is appended as a final paragraph."""
    if "[[FACT]]" in text:
        out, how = text.replace("[[FACT]]", fact, 1).replace("[[FACT]]", ""), "marker"
    else:
        out, how = text.rstrip() + "\n\nAdditional Details\n" + fact, "appended"
    # the writer often copies the violation sentence next to the marker too -> duplicate: keep the first occurrence
    first = out.find(fact)
    if first >= 0:
        cut = first + len(fact)
        out = out[:cut] + out[cut:].replace(fact, "")
        out = re.sub(r"[ \t]{2,}", " ", out)
    return out, how


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _repair_meta(ruleset: dict, text: str, keep: str = None):
    """Remove each SENTENCE that comments on the rules / mentions a rule ID (e.g. "This exceeds the minimum $500,000
    requirement.") instead of discarding the whole application. NEVER removes the fixed violation sentence. After repair
    the application must pass every gate again (leaks, guard cross-check, verifier), and the removed sentences are
    stored for the reviewer."""
    id_pat = re.compile(r"\b(?:" + "|".join(re.escape(r["id"]) for r in ruleset["rules"]) + r")\b")
    lines, removed = [], []
    for line in text.split("\n"):
        kept = []
        for s in _SENT_SPLIT.split(line):
            if s.strip() and (_META_PAT.search(s) or id_pat.search(s)) and not (keep and _contains(s, keep)):
                removed.append(s.strip())
                continue
            kept.append(s)
        lines.append(" ".join(kept).rstrip())
    return "\n".join(lines), removed


# Signs that the text is NOT an application but the model talking to itself ("Okay, the user wants me to write...").
# Measured 2026-09-14: qwen3:4b (thinking-only build) ignored think=false and returned its whole monologue; every earlier
# gate still passed because the violation sentence was inserted in the middle of it — only the human reviewer caught it.
# Now blocked by code.
_MONOLOGUE_PAT = re.compile(
    r"\b(?:the user wants|user wants|the user (?:asked|specified|has given)|I need to|I'll|I will write|let me|let's|"
    r"we are writing|we must (?:not )?write|the marker|marker topic|placeholder|the instructions?|the prompt|"
    r"case seed|word count|brainstorm\w*|application summary for)\b|^\s*(?:okay|ok|alright|sure|hmm)\b", re.I | re.M)


def _doc_problems(raw: str) -> list:
    """Is the draft an APPLICATION at all — mechanical check on the raw text, before inserting the violation / removing commentary."""
    problems = []
    hits = sorted({m.group(0).strip().lower() for m in _MONOLOGUE_PAT.finditer(raw)})
    if hits:
        problems.append("not an application — model monologue: " + ", ".join(hits[:4]))
    n = len(raw.split())
    if not 120 <= n <= 520:
        problems.append(f"abnormal length ({n} words)")
    return problems


def _guard_crosscheck(ruleset: dict, text: str, target_id: str):
    fired = {}
    for r in ruleset["rules"]:
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] in ("override", "flag"):
            fired[r["id"]] = g["reason"][:70]
    wrong = [rid for rid in fired if rid != target_id]
    return (not wrong), (target_id in fired), fired


# ---------------- verifier (LLM, model separate from the system under test) ----------------
def _votes(rule: dict, text: str):
    """Two votes ASKED TWO WAYS, mapped onto one scale "is the rule satisfied": yes / no / unclear.
    Rule IDs are kept out of the question so the verifier cannot lean on the ID."""
    body = f'RULE: "{rule["quote"]}"\n\nAPPLICATION TEXT:\n{text}'
    out = []
    for system, flip in ((SYS_VERIFY, False), (SYS_VERIFY_VIOL, True)):
        try:
            a = llm.chat_json(system, body, VERIFY_SCHEMA, max_tokens=160, model=VERIFIER_MODEL).get("answer", "unclear")
        except llm.LLMError:
            out.append("error")   # record the model-call error explicitly — never let it look like "unclear" (shared GPU or timeout)
            continue
        out.append({"yes": "no", "no": "yes"}.get(a, a) if flip else a)
    return out


def _verify(ruleset: dict, text: str, target_id: str):
    """Check the SECONDARY labels ('full' scope):
      • Every vote says there is evidence -> the met label is trustworthy. Contradicting votes -> keep in the metric, flag.
        Every vote says unclear -> 'weak', excluded from the metric.
      • CODE HAS A VETO: if a guard asserts a violation by figures, the met label is certainly wrong -> weak.
    """
    weak, detail, disagree = [], {}, []
    for r in ruleset["rules"]:
        if r["id"] == target_id:
            continue
        answers = _votes(r, text)
        if not answers:
            continue
        detail[r["id"]] = answers
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] == "override":
            detail[r["id"]] = answers + ["code:violation"]
            weak.append(r["id"])
            continue
        if all(a == "yes" for a in answers):
            continue
        if any(a == "yes" for a in answers):
            disagree.append(r["id"])
            continue
        weak.append(r["id"])
    return weak, detail, disagree


# ---------------- generation ----------------
def _confirm_fact(ruleset: dict, rule: dict, fact: str):
    """Is the fact sentence REALLY a violation: prefer code (guard); otherwise two verifier votes."""
    g = guards.check(ruleset["id"], rule, "met", fact, {})
    if g and g["action"] in ("override", "flag"):
        return True, "code"
    v = _votes(rule, fact)
    if v and all(a == "no" for a in v):
        return True, "verifier"
    return False, f"verifier {v}"


def _plan_fact(ruleset: dict, rule: dict, seed: str, tries: int = 4):
    why, rejected = "not attempted", []
    for k in range(tries):
        # temperature 0: an unchanged prompt gives the identical answer -> feed back the rejected sentences WITH REASONS to force a new direction
        retry = ("\n\nThese earlier attempts were REJECTED:\n"
                 + "\n".join(f'- "{r}" (reason: {why_en})' for r, why_en in rejected[-3:])
                 + "\nWrite a different sentence that directly contradicts the rule and avoids those problems.") if rejected else ""
        try:
            # the "seed" is kept out of the prompt: the model would use it as the organisation name ("Case 2-0")
            d = llm.chat_json(SYS_PLAN, f'PROGRAM: {ruleset["name"]}\nRULE: "{rule["quote"]}"{retry}\n\n'
                                        "Write the fact sentence now.",
                              PLAN_SCHEMA, max_tokens=120, model=PLANNER_MODEL)
        except llm.LLMError as e:
            why = f"LLM error: {e}"
            continue
        fact = re.sub(r"\s+", " ", guards.normalise(d.get("fact", ""))).strip().strip('"')
        if len(fact.split()) < 6:
            why = f"sentence too short: {fact!r}"
            continue
        if _MONOLOGUE_PAT.search(fact) or re.search(r"\bcase\s*\d", fact, re.I):
            why = f"sentence is not an application fact: {fact[:90]}"
            rejected.append((fact, "it is not a plain statement of fact about a named applicant"))
            continue
        leaks = _leak_check(ruleset, fact)
        if leaks:
            why = f"{leaks[0]} — {fact[:90]}"
            rejected.append((fact, "it uses rule wording such as 'required', 'threshold', 'minimum', 'violate' or a rule "
                                   "code; state only the plain fact and the number"))
            continue
        ok, how = _confirm_fact(ruleset, rule, fact)
        if ok:
            return fact, how
        why = f"not a clear violation ({how}) — {fact[:90]}"
        rejected.append((fact, "it does not clearly fail this exact rule"))
    return None, why


# temperature 0: retrying with the identical prompt returns the identical text -> vary the STYLE between attempts (no IDs/seeds).
_STYLE = ["", "Use title-case paragraph headings.", "Use slightly more formal wording and different paragraph headings."]


def _gen_text(ruleset: dict, target_rule: dict = None, fact: str = None, variant: int = 0) -> str:
    rules_txt = "\n".join(f'- "{r["quote"]}"' for r in ruleset["rules"])   # NO rule IDs for the writer
    if fact:
        req = (f'MARKER TOPIC: "{target_rule["quote"]}"\n'
               f'The marker [[FACT]] will be replaced by this sentence (use the same organisation name): "{fact}"')
    else:
        req = "MARKER TOPIC: none - the applicant must clearly meet every rule."
    txt = llm.chat_text(SYS_GEN, f"PROGRAM: {ruleset['name']}\nRULES:\n{rules_txt}\n\n{req}\n\n"
                                 f"{_STYLE[variant % len(_STYLE)]}\nWrite the application summary now.",
                        max_tokens=600, model=GEN_MODEL)
    return guards.normalise(txt).strip()


def generate(ruleset_id: str, n_violations: int = None, officer: str = "casegen", verify: bool = True,
             scope: str = "full"):
    """scope='full'  : verify ALL secondary labels (expensive) -> the secondary metric can be measured too.
       scope='target': verify only the TARGET rule + control on the clean application (cheap) -> measures [TARGET] + control;
                       the label file records the scope so eval does NOT report secondary figures as if verified."""
    rs = core.get_ruleset(ruleset_id)
    rules = rs["rules"]
    guarded = [r for r in rules if guards.compile_rule_guards(r) or r["id"] in guards.HAND_COVERAGE.get(ruleset_id, set())]
    targets = (guarded if n_violations is None else guarded[:n_violations]) or rules[:n_violations or 3]
    out_dir = GEN_DIR / ruleset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.txt"):   # a new set fully replaces the old one (the label file is overwritten too) — history lives in git
        old.unlink()
    print(f"[casegen] {ruleset_id} · violation planner: {_planner_model_name()} · writer: {_gen_model_name()} · "
          f"label verifier: {_verifier_model_name()} · scope: {scope} · "
          f"target rules: {[t['id'] for t in targets]}", flush=True)
    cases, skipped = [], []
    for i, tgt in enumerate([None] + targets):  # first case = clean application (control)
        cid = f"GEN-{ruleset_id}-{i:02d}" + ("-clean" if tgt is None else f"-{tgt['id']}")
        tid = tgt["id"] if tgt else "__none__"
        fact, how = None, None
        # NEUTRAL seed: the case ID contains the rule ID (e.g. "...-01-C01") -> putting it in the prompt would leak the answer
        seed = f"case{i}"
        if tgt:
            fact, how = _plan_fact(rs, tgt, seed)
            if not fact:
                skipped.append({"id": cid, "reason": "could not fix a clear violation sentence after 4 attempts", "detail": how})
                print(f"  {cid}: SKIPPED — could not fix a violation sentence ({how})", flush=True)
                continue
        problem, agree, text = "not written", False, ""
        placement, removed, rest_votes = None, [], None
        for k in range(3):
            text = _gen_text(rs, tgt, fact, k)
            shape = _doc_problems(text)
            if shape:
                problem = shape[0]
                continue
            if fact:
                text, placement = _insert_fact(text, fact)
            # even a CLEAN application may get a [[FACT]] marker from the writer (On-farm 09-14) -> strip leftovers in every case
            text = re.sub(r"[ \t]*\[\[FACT\]\][ \t]*", " ", text)
            text, removed = _repair_meta(rs, text, keep=fact)
            if len(removed) > 3:   # a real application may slip a few commentary sentences; more than that means a broken text
                problem = f"too many rule-commentary sentences had to be removed ({len(removed)})"
                continue
            if fact and not _contains(text, fact):
                problem = "text does not contain the violation sentence verbatim"
                continue
            leaks = _leak_check(rs, text)
            if leaks:
                problem = leaks[0]
                continue
            ok, agree, fired = _guard_crosscheck(rs, text, tid)
            if not ok:
                problem = f"guard fired on a non-target rule: {sorted(r for r in fired if r != tid)}"
                continue
            if how == "code" and not agree:
                problem = "guard no longer fires on the full text — another fact overrides the violation sentence"
                continue
            if tgt and verify:
                # ANTI-SELF-CONTRADICTION: with the violation sentence removed, the rest must be SILENT about the target rule.
                # If both votes still see the rule satisfied (e.g. "the CEO signed the certification" next to "there is no
                # board"), the application contradicts itself and is not a clean test -> rewrite. (Cyber 09-14: 2/3 cases failed this way.)
                rest_votes = _votes(tgt, re.sub(r"[ \t]{2,}", " ", text.replace(fact, " ")))
                if rest_votes and all(a == "yes" for a in rest_votes):
                    problem = f"self-contradictory — with the violation sentence removed, the rest still shows the rule satisfied {rest_votes}"
                    continue
            problem = None
            break
        if problem:
            skipped.append({"id": cid, "reason": problem + " (after 3 attempts)", "fact": fact,
                            "last_text": text[:2000]})   # keep the last draft so the reviewer can see why it was dropped
            print(f"  {cid}: SKIPPED — {problem}", flush=True)
            continue

        verifier, weak, disagree, clear, control = {}, [], [], None, None
        if verify and tgt:
            verifier[tgt["id"]] = _votes(tgt, text)
            clear = bool(verifier[tgt["id"]]) and all(a == "no" for a in verifier[tgt["id"]])
        elif tgt:
            clear = True
        if verify and scope == "full":
            weak, sec, disagree = _verify(rs, text, tid)
            verifier.update(sec)
        elif verify and not tgt:  # control: target rules assessed on the clean application
            control = []
            for t in targets:
                verifier[t["id"]] = _votes(t, text)
                if verifier[t["id"]] and all(a == "yes" for a in verifier[t["id"]]):
                    control.append(t["id"])

        labels = {r["id"]: "met" for r in rules}
        if tgt:
            labels[tgt["id"]] = "not_met"
        (out_dir / f"{cid}.txt").write_text(text + "\n", encoding="utf-8")
        case = {"id": cid, "file": f"generated/{ruleset_id}/{cid}.txt", "target": tgt["id"] if tgt else None,
                "labels": labels, "violation_fact": fact, "fact_confirmed_by": how,
                "fact_placement": placement, "removed_meta_sentences": removed,
                "rest_without_fact_votes": rest_votes,
                "guard_agree": bool(agree) if tgt else None, "target_label_clear": clear,
                "weak_labels": weak, "verifier_disagreement": disagree, "verifier": verifier,
                "status": "needs_review" if (tgt and not clear) else "pending_review"}
        if control is not None:
            case["control_rules"] = control
        cases.append(case)
        if tgt:
            print(f"  {cid}: violates {tgt['id']} [fixed by {how}] · guard {'CONFIRMS' if agree else 'did not fire'} · "
                  + ("verifier confirms" if clear else f"!! FLAGGED BY VERIFIER {verifier.get(tgt['id'])} — needs a reviewer"),
                  flush=True)
        else:
            print(f"  {cid}: clean · usable as control for {control}", flush=True)

    doc = {"ruleset": ruleset_id, "version": rs["version"], "label_scope": scope,
           "generator_model": _gen_model_name(), "verifier_model": _verifier_model_name(),
           "planner_model": _planner_model_name(),
           "generated_by": _gen_model_name(), "targets": [t["id"] for t in targets],
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "approved": False, "approved_by": None, "approved_at": None,
           "n_needs_review": sum(1 for c in cases if c["status"] == "needs_review"),
           "note": "Violations fixed by code first (the fact sentence is confirmed by guard/verifier and the text must contain "
                   "it verbatim); answer leaks blocked by code; the verifier uses a model separate from the system under test. "
                   "NOT APPROVED — measurements from this set are PROVISIONAL. Review the texts, resolve every flag, then --approve.",
           "cases": cases, "skipped": skipped}
    (LBL_DIR / f"generated-{ruleset_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


# ---------------- review & approval ----------------
def _load(ruleset_id):
    p = LBL_DIR / f"generated-{ruleset_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"No generated test set for '{ruleset_id}' — run --gen first")
    return p, json.loads(p.read_text(encoding="utf-8"))


def _case(doc: dict, case_id: str) -> dict:
    hit = next((c for c in doc["cases"] if c["id"] == case_id), None)
    if not hit:
        raise KeyError(case_id)
    return hit


def _save_edited(p, doc):
    """Every label edit after approval REVOKES the approval — the set must be approved again."""
    doc.update(approved=False, approved_by=None, approved_at=None,
               n_needs_review=sum(1 for c in doc["cases"] if c.get("status") == "needs_review"))
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def approve(ruleset_id: str, officer: str):
    """Approve the labels after reviewing the texts — only then do measurements stop being 'provisional'.
    Refused while a verifier-flagged case is unresolved (confirm or dispute)."""
    p, doc = _load(ruleset_id)
    if officer in (doc.get("reviewers") or []):
        raise ValueError(f"Separation of duties: '{officer}' took part in reviewing this set's labels and cannot approve it — another person must approve")
    pending = [c["id"] for c in doc["cases"] if c.get("status") == "needs_review"]
    if pending:
        raise ValueError(f"{len(pending)} verifier-flagged cases still unreviewed: {pending} — "
                         "read the text then --confirm (real violation) or --dispute (wrong label) before approving")
    # Re-screen with the CURRENT filter before approving: the leak filter is tightened over review rounds
    # (e.g. "falling short of the $1,207,000 threshold" slipped through in Wine 09-14) — violation cases generated with an
    # older filter must still pass the new one, because a leaked answer in a violation case inflates the primary metric directly.
    rs = core.get_ruleset(ruleset_id)
    leaky = {c["id"]: _leak_check(rs, (core.DATA / "applications" / c["file"]).read_text(encoding="utf-8"))
             for c in doc["cases"] if c.get("target") and c.get("status") != "disputed"}
    leaky = {k: v for k, v in leaky.items() if v}
    if leaky:
        raise ValueError(f"Violation cases still leak the answer under the current filter: {leaky} — --dispute or regenerate before approving")
    live = [c for c in doc["cases"] if c.get("status") != "disputed"]
    doc.update(approved=True, approved_by=officer, approved_at=datetime.now().isoformat(timespec="seconds"))
    for c in doc["cases"]:
        if c.get("status") == "pending_review":
            c["status"] = "approved"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "approved_by": officer, "n_cases": len(live)}


def confirm_target(ruleset_id: str, case_id: str, reason: str):
    """The reviewer read the text and confirms the target violation is REAL despite the verifier flag.
    These are exactly the hard cases the metric must keep — never drop them silently."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    if not hit.get("target"):
        raise ValueError("a clean case has no target rule to confirm")
    hit.update(target_label_clear=True, target_confirmed_by_reviewer=reason, status="pending_review")
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "reason": reason}


def confirm_control(ruleset_id: str, case_id: str, rule_id: str, reason: str):
    """The reviewer read the CLEAN application and confirms it CLEARLY satisfies the target rule -> use that rule as control.
    The verifier only flags: a small verifier model may answer 'unclear' although the text is explicit (measured 09-14: the
    same Cyber text got yes/yes in one round and not in the next) — dropping the control for that would lose the false-alarm measurement."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    if hit.get("target"):
        raise ValueError("only applies to the clean case (the case without a target rule)")
    if rule_id not in (doc.get("targets") or []):
        raise ValueError(f"{rule_id} is not a target rule of this test set ({doc.get('targets')})")
    hit.setdefault("control_rules", [])
    if rule_id not in hit["control_rules"]:
        hit["control_rules"].append(rule_id)
    hit.setdefault("control_confirmed_by_reviewer", {})[rule_id] = reason
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "control_rules": hit["control_rules"], "reason": reason}


def flag_weak(ruleset_id: str, case_id: str, rule_id: str, reason: str):
    """The reviewer marks ONE secondary label as untrustworthy -> excluded from the metric (the case is kept)."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    hit.setdefault("weak_labels", [])
    if rule_id not in hit["weak_labels"]:
        hit["weak_labels"].append(rule_id)
    hit.setdefault("weak_reasons", {})[rule_id] = reason
    if rule_id in (hit.get("control_rules") or []):
        hit["control_rules"].remove(rule_id)
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "rule": rule_id, "reason": reason}


def dispute(ruleset_id: str, case_id: str, reason: str, by: str = None):
    """Mark a case as wrong/doubtful -> excluded from every metric (kept for traceability)."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    hit.update(status="disputed", dispute_reason=reason)
    if by:  # record the reviewer -> separation of duties: whoever reviewed the labels cannot approve the same set
        hit["disputed_by"] = by
        doc["reviewers"] = sorted(set(doc.get("reviewers") or []) | {by})
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "reason": reason}


# ---------------- measurement ----------------
def evaluate(ruleset_id: str):
    """Measure the assessment system (system model) on the generated set — SEPARATE metrics so test-set noise does not mix in:
      • target   : the target rule of each violation case — PRIMARY METRIC (caught / false pass / routed to officer).
      • control  : ('target' scope) target rules assessed on the CLEAN application — false alarms.
      • secondary: ('full' scope) secondary rules with clear evidence — reference only.
    """
    _, doc = _load(ruleset_id)
    info = llm.describe()
    approved = bool(doc.get("approved"))
    scope = doc.get("label_scope", "full")
    disputed = {c["id"] for c in doc["cases"] if c.get("status") == "disputed"}
    flagged = {c["id"] for c in doc["cases"] if c.get("status") == "needs_review"
               or (c.get("target") and c.get("target_label_clear") is False)} - disputed
    live = [c for c in doc["cases"] if c["id"] not in disputed | flagged]
    print("=" * 70)
    print(f"EVAL '{ruleset_id}' · system under test: {info['model']} · labels generated by: "
          f"{doc.get('generator_model') or doc.get('generated_by')} · {len(live)} cases"
          + (f" (excluding {len(disputed)} disputed)" if disputed else "")
          + (f" (excluding {len(flagged)} cases with unresolved flags)" if flagged else ""))
    if scope == "target":
        print("SCOPE: TARGET rule on violation cases + control on the clean application (secondary rules are neither measured nor reported).")
    if not approved:
        print("!! LABELS NOT APPROVED — the figures below are PROVISIONAL; do not report them.")
    print("=" * 70)
    T = {"n": 0, "ok": 0, "fp": 0}
    C = {"n": 0, "ok": 0, "false_alarm": 0}
    S = {"n": 0, "ok": 0, "fp": 0}
    excluded, errors = 0, []
    for c in live:
        weak = set(c.get("weak_labels") or [])
        if scope == "target":
            only = {c["target"]} if c.get("target") else set(c.get("control_rules") or []) - weak
            if not only:
                continue
        else:
            only = None
        text = (core.DATA / "applications" / c["file"]).read_text(encoding="utf-8")
        for v in core.assess_case(c["id"], text, ruleset_id=ruleset_id, only_rules=only,
                                  progress=lambda rid, i, n: print(f"  {c['id']} {rid} ({i+1}/{n})", end="\r")):
            truth = c["labels"][v["r"]]
            is_target = v["r"] == c.get("target")
            if scope == "target":
                bucket, kind = (T, "target") if is_target else (C, "control")
            else:
                if not is_target and v["r"] in weak:
                    excluded += 1
                    continue
                bucket, kind = (T, "target") if is_target else (S, "secondary")
            bucket["n"] += 1
            if v["v"] == truth:
                bucket["ok"] += 1
                continue
            if truth == "not_met" and v["v"] == "met":
                bucket["fp"] += 1
            if kind == "control" and v["v"] == "not_met":
                C["false_alarm"] += 1
            errors.append({"case": c["id"], "rule": v["r"], "kind": kind, "pred": v["v"], "truth": truth,
                           "note": (v.get("note") or "")[:100]})
        print()
    routed = T["n"] - T["ok"] - T["fp"]
    total_seen = T["n"] + S["n"] + excluded
    excl_rate = excluded / max(total_seen, 1)
    print("\n" + "#" * 70)
    print("# PRIMARY METRIC — [TARGET]: each case has exactly 1 planted violation; does the system catch it?")
    print(f"#   Violations caught      : {T['ok']}/{T['n']}" + (f" = {T['ok']/T['n']:.0%}" if T["n"] else ""))
    print(f"#   FALSE PASS             : {T['fp']}/{T['n']} (expected 0 — the most important figure)")
    print(f"#   Routed to officer (unclear): {routed}/{T['n']} (not a miss, but costs human time)")
    print("#" * 70)
    if scope == "target":
        print(f"[CONTROL] target rules on the CLEAN application: correct {C['ok']}/{C['n']} · FALSE ALARMS {C['false_alarm']}/{C['n']}"
              " — without this line a 'reject everything' system would also score 100% above.")
    else:
        print(f"[REFERENCE] secondary rules with clear evidence: {S['ok']}/{S['n']}" + (f" = {S['ok']/S['n']:.0%}" if S["n"] else "")
              + " — not used as a quality measure.")
        print(f"[EXCLUDED]  {excluded}/{total_seen} rule evaluations excluded ({excl_rate:.0%}) — weak secondary labels.")
        if excl_rate > 0.3:
            print("!! WARNING: >30% excluded — low-quality generated test set.")
    if T["n"] < 10:
        print(f"!! SMALL SAMPLE ({T['n']} violation cases): preliminary per-fund evidence, not a statistical rate.")
    if not approved:
        print("!! REMINDER: labels not approved -> every figure above is PROVISIONAL.")
    for e in errors:
        print(f"  [{e['kind']}] {e['case']} {e['rule']}: predicted {e['pred']} / label {e['truth']} — {e['note']}")
    out = {"ruleset": ruleset_id, "llm": info["model"], "assessor_model": info["model"],
           "generator_model": doc.get("generator_model") or doc.get("generated_by"),
           "verifier_model": doc.get("verifier_model"), "label_scope": scope,
           "labels_generated_at": doc.get("generated_at"), "labels_approved_at": doc.get("approved_at"),
           "evaluated_at": datetime.now().isoformat(timespec="seconds"),
           "provisional": not approved, "labels_approved": approved, "approved_by": doc.get("approved_by"),
           "primary_metric": "target",
           "target": {"correct": T["ok"], "total": T["n"], "false_pass": T["fp"], "routed_to_officer": routed,
                      "note": "PRIMARY METRIC — is the planted violation caught"},
           "control": ({"correct": C["ok"], "total": C["n"], "false_alarm": C["false_alarm"],
                        "note": "target rules assessed on the clean application — false alarms"} if scope == "target" else None),
           "secondary": ({"correct": S["ok"], "total": S["n"], "false_pass": S["fp"],
                          "note": "reference only — secondary rules are generated by the test writer and noisy"} if scope == "full" else None),
           "excluded_weak_labels": excluded, "excluded_rate": round(excl_rate, 3),
           "low_quality_testset": scope == "full" and excl_rate > 0.3,
           "small_sample": T["n"] < 10,
           "disputed_cases": len(disputed), "flagged_unresolved": len(flagged), "errors": errors}
    (core.DATA.parent / f"eval-generated-{ruleset_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    rid, cmd = sys.argv[1], sys.argv[2]
    args = [a for a in sys.argv[3:] if not a.startswith("--")]
    try:
        if cmd == "--gen":
            d = generate(rid, int(args[0]) if args else None,
                         scope="target" if "--target-only" in sys.argv else "full")
            print(f"\nGenerated: {len(d['cases'])} cases (skipped {len(d['skipped'])}, {d['n_needs_review']} flagged for review)"
                  f" -> data/labels/generated-{rid}.json\nLABELS NOT APPROVED — review the texts, then --confirm/--dispute, then --approve.")
        elif cmd == "--eval":
            evaluate(rid)
        elif cmd == "--approve":
            print(approve(rid, args[0] if args else "Officer"))
        elif cmd == "--confirm":
            print(confirm_target(rid, args[0], args[1] if len(args) > 1 else "no reason given"))
        elif cmd == "--confirm-control":
            print(confirm_control(rid, args[0], args[1], args[2] if len(args) > 2 else "no reason given"))
        elif cmd == "--dispute":
            print(dispute(rid, args[0], args[1] if len(args) > 1 else "no reason given"))
        elif cmd == "--flag-weak":
            print(flag_weak(rid, args[0], args[1], args[2] if len(args) > 2 else "no reason given"))
        else:
            print(__doc__); sys.exit(1)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"ERROR: {e}"); sys.exit(2)
