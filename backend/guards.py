"""guards.py — FALSE-PASS blocking layer implemented in code (customer requirement: not_met→met is more dangerous than a false fail).

After the LLM has judged, code re-checks the criteria that can be verified with figures / definite text patterns:
  - QUANTITATIVE rules (budget, page counts, %, attempt counts, co-PI): code finds a violation while the LLM said "met"
    -> OVERRIDE to not_met (figures compared by code are not debatable), tagged with source "code-guard".
  - QUALITATIVE rules (tenured, cost sharing in words): code suspects a violation while the LLM said "met"
    -> DOWNGRADE to "unclear" + needs_attention — a human must decide; never silently let it through.
A guard NEVER upgrades to "met". If the LLM already said not_met/unclear the guard only confirms.

Fund-specific guards are written per (ruleset_id, rule_id); other rulesets use the generic guard
"minimum of $X" compared with the amounts extracted by crosscheck.extract_values.
"""
import re
from .crosscheck import extract_values

BIO_ENG_OPP = re.compile(r"\b(BIO|ENG|OPP)\b|Directorate for (Biological|Engineering)|Office of Polar", re.I)


def _budget_values(text: str):
    return [v for cat, v, _, _ in extract_values("x", text) if cat == "budget_total"]


def normalise(text: str) -> str:
    """Normalise before guards run — hand-written regexes often miss because of wording/formatting:
    strip markdown (**bold**, *italic*, `code`), collapse whitespace/newlines, normalise dashes and curly quotes.
    Fixes the W06 case that slipped through the Wine eval ('does **not** own' did not match 'does not own')."""
    t = re.sub(r"[*_`]{1,3}", "", text or "")
    t = t.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"[ \t]+", " ", t)
    # Australian currency is written in many ways — normalise everything to "$1,250,000" so EVERY guard (hand-written
    # and compiled) can read it, instead of fixing each regex: "A$1,250,000" / "AUD 1,250,000" / "1,250,000 AUD" / "... dollars".
    t = re.sub(r"\bA\$\s*(?=\d)", "$", t)
    t = re.sub(r"\bAUD\s*\$?\s*(?=\d)", "$", t, flags=re.I)
    t = re.sub(r"(?<![$\d.])(\d[\d,]*\d|\d)\s*(?:AUD|australian dollars|dollars)\b", r"$\1", t, flags=re.I)
    return t


def _sentences_with(text: str, pat: str):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if re.search(pat, s, re.I)]


def _alt_satisfied(text: str, alt_pat: str, need_pat: str) -> bool:
    """Does any sentence show that an ALTERNATIVE BRANCH is satisfied (e.g. a related entity owns the cellar door)?
    If so, the guard must NOT conclude a violation from a negative sentence about the main entity."""
    for s in _sentences_with(text, alt_pat):
        if re.search(need_pat, s, re.I) and not re.search(r"\b(no|not|none|without)\b", s, re.I):
            return True
    return False


# ---------- fund-specific guards: NSF 22-586 CAREER ----------
def _career(rule_id: str, text: str, meta: dict):
    """Returns (suspected_verdict, reason) or None."""
    if rule_id == "R04":
        m = re.search(r"(\d{1,3})%\s*(is |as )?(a )?tenure-track", text, re.I)
        if m and int(m.group(1)) < 50:
            return "not_met", f"Tenure-track position only {m.group(1)}% < 50% (code compared figures)"
        if re.search(r"\b(I am|is|as)\s+(an?\s+)?Associate Professor", text):
            return "not_met", "Application states the title Associate Professor — explicitly excluded by the rule (code pattern match)"
    if rule_id == "R05":
        if re.search(r"received tenure in \d{4}|\bI am tenured\b|granted tenure", text, re.I):
            return "not_met", "Application states tenure has been granted (code pattern match)"
    if rule_id == "R07":
        if re.search(r"\b(fourth|fifth|sixth|4th|5th)\b[^.\n]{0,40}(submission|time)[^.\n]{0,40}(CAREER|competition)|(CAREER|competition)[^.\n]{0,60}\b(fourth|fifth|4th|5th)\b", text, re.I):
            return "not_met", "Application states this is the 4th or later submission — exceeds the 3-attempt limit (code pattern match)"
    if rule_id == "R08":
        hits = _sentences_with(text, r"co-?PI\b|co-?Principal Investigator")
        bad = [s for s in hits if not re.search(r"\bno co-?PIs?\b|not permit|sole Principal", s, re.I)]
        if bad:
            return "not_met", "Application lists a co-PI on the cover page — the rule prohibits co-PIs (code pattern match)"
    if rule_id == "R09":
        m = re.search(r"letter[^.\n]{0,90}?\bis\s+(\d+)\s+pages", text, re.I)
        if m and int(m.group(1)) > 2:
            return "not_met", f"Departmental letter {m.group(1)} pages > 2-page limit (code compared figures)"
    if rule_id == "R10":
        m = re.search(r"Project Description is\s+(\d+)\s+pages", text, re.I)
        if m and int(m.group(1)) > 15:
            return "not_met", f"Project description {m.group(1)} pages > 15-page limit (code compared figures)"
    if rule_id == "R11":
        vals = _budget_values(text)
        if vals:
            thr = 500_000 if (BIO_ENG_OPP.search(meta.get("directorate") or "") or BIO_ENG_OPP.search(text)) else 400_000
            if all(v < thr for v in vals):
                return "not_met", f"Total budget ${max(vals):,} < minimum ${thr:,} (code compared figures)"
    if rule_id == "R12":
        hits = _sentences_with(text, r"voluntary( committed)? cost shar")
        bad = [s for s in hits if not re.search(r"\bno\b|not include|do(es)? not|without", s, re.I)]
        if bad:
            return "not_met", "Application mentions voluntary cost sharing — prohibited by the rule (code pattern match)"
    return None


# ---------- fund-specific guards: Australian funds (per the customer's "AI trap" analysis) ----------
def _au_wine(rule_id: str, text: str, meta: dict):
    if rule_id == "W03":  # rebatable wine sales threshold $1,207,000
        for s in _sentences_with(text, r"rebatable wine"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) < 1_207_000:
                return "not_met", f"Rebatable wine sales ${m.group(1)} < threshold $1,207,000 (code compared figures)"
    if rule_id == "W04":
        # TWO-LAYER LOGIC (needs-manual-guard backlog closed with a hand-written guard):
        # cellar-door sales must have an EXCESS left after being used to reach the $1,207,000 threshold.
        # => the NON-cellar-door sales (total rebatable - cellar door) must reach the threshold on their own;
        #    otherwise every cellar-door dollar was consumed to hit the threshold -> nothing "in excess".
        if re.search(r"(all|entire|whole)[^.\n]{0,40}cellar door sales[^.\n]{0,60}(used|applied|counted)[^.\n]{0,40}(threshold|meet)|"
                     r"cellar door sales[^.\n]{0,50}(were|was|are|is)[^.\n]{0,30}(entirely|fully|wholly)[^.\n]{0,30}used", text, re.I):
            return "not_met", "Application states all cellar-door sales were used to reach the threshold — nothing in excess remains (code pattern match)"
        THR = 1_207_000
        total = next((int(m.group(1).replace(",", "")) for s in _sentences_with(text, r"rebatable wine")
                      for m in [re.search(r"\$\s?(\d[\d,]*\d)", s)] if m), None)
        cellar = next((int(m.group(1).replace(",", "")) for s in _sentences_with(text, r"cellar door sales")
                       for m in [re.search(r"\$\s?(\d[\d,]*\d)", s)] if m), None)
        # Cellar-door excess after reaching the threshold = min(cellar, total − threshold).
        # Only conclude when the threshold is reached (total ≥ THR) — otherwise it is a W03 violation;
        # do not fire W04 on top of it and add noise.
        if total is not None and cellar is not None and total >= THR:
            excess = min(cellar, total - THR)
            if excess <= 0:
                return "not_met", (f"No cellar-door sales above the threshold remain: total ${total:,} − threshold ${THR:,} "
                                   f"= ${total - THR:,}, cellar door ${cellar:,} → excess ${max(excess, 0):,} "
                                   "(two-layer computation by code, per the wording 'in excess of')")
    if rule_id == "W06":  # TRAP: wholesale only / no physical cellar door
        # The rule allows an ALTERNATIVE: "and/or their related entity/ies have owned or leased..."
        # -> if the application says a related entity has a cellar door, do NOT conclude a violation.
        if _alt_satisfied(text, r"related entit|associated entit|subsidiar|parent (company|entity)",
                          r"(own|lease|operat)\w*[^.]{0,60}cellar door"):
            return None
        if re.search(r"wholesale only|only .{0,25}wholesale|no (physical )?cellar door|"
                     r"does not (own|operate|lease|have)[^.\n]{0,60}cellar door|ceased .{0,40}cellar door|"
                     r"(sold|closed|disposed of)[^.\n]{0,40}(cellar door|tasting room)", text, re.I):
            return "not_met", "Application/documents state wholesale only or no physical cellar door (and no related-entity alternative) — fund-specific trap (code pattern match)"
    if rule_id == "W08":  # <50% of sales from a physical cellar door
        m = re.search(r"(\d{1,2})\s?(?:per cent|%)[^.\n]{0,80}(physical )?cellar door", text, re.I)
        if m and int(m.group(1)) < 50:
            return "not_met", f"Only {m.group(1)}% of sales from a physical cellar door < 50% (code compared figures)"
    if rule_id == "W09":
        m = re.search(r"grant (?:amount )?(?:requested|of)[^.\n]{0,25}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m and int(m.group(1).replace(",", "")) > 100_000:
            return "not_met", f"Amount requested ${m.group(1)} exceeds the $100,000 cap (code compared figures)"
    return None


def _au_cyber(rule_id: str, text: str, meta: dict):
    if rule_id == "C01":  # TRAP: sole applicant, no consortium
        if re.search(r"sole applicant|apply(ing)? alone|no project partner|without (a |any )?partner|single (organisation|entity) appl", text, re.I):
            return "not_met", "Application states a sole submission / no project partner — the fund requires a consortium (code pattern match)"
    if rule_id == "C06":
        # "board supports ... (or CEO or equivalent if there is no board)" — alternative branch:
        # only conclude a violation when the application clearly says NEITHER the board NOR the CEO/equivalent endorsed it.
        if _alt_satisfied(text, r"chief executive|CEO|equivalent|managing director|board",
                          r"(certif|support|approv|endorse)\w*"):
            return None
        if re.search(r"(board|chief executive|CEO)[^.\n]{0,60}(has not|have not|not yet|did not|declin\w+)[^.\n]{0,40}"
                     r"(approv|support|certif|endors)|no (board|CEO|executive) (approval|certification|support|endorsement)|"
                     r"without (board|CEO|executive) (approval|support|certification)", text, re.I):
            return "not_met", "Application states no board/CEO endorsement of the project — required by the rule (code pattern match)"
        if re.search(r"cannot (meet|cover|fund)[^.\n]{0,50}(costs|expenditure) not covered|"
                     r"unable to (meet|cover)[^.\n]{0,40}remaining (costs|cost)", text, re.I):
            return "not_met", "Application states the non-grant share of costs cannot be committed — required by the rule (code pattern match)"
    if rule_id == "C07":
        for s in _sentences_with(text, r"eligible expenditure"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) < 500_000:
                return "not_met", f"Eligible expenditure ${m.group(1)} < minimum $500,000 (code compared figures)"
    if rule_id == "C08":
        # CONDITIONAL branch the compiler cannot express: "an employer of 100 or more employees that has not
        # complied with the Workplace Gender Equality Act (2012)" -> a violation only when BOTH conditions hold.
        bad = [s for s in _sentences_with(text, r"workplace gender equality|\bWGEA\b")
               if re.search(r"\b(?:has|have|had)\s+not\s+(?:yet\s+)?(?:complied|lodged|reported|submitted)|"
                            r"\bnot\s+(?:yet\s+)?compliant\b|non-?complian|failed to (?:comply|lodge|report|submit)", s, re.I)]
        if bad:
            counts = [int((a or b).replace(",", "")) for a, b in re.findall(
                r"(\d[\d,]*)\s+(?:full-time\s+|permanent\s+|FTE\s+)?(?:employees|staff members|staff|workers)\b|"
                r"employ\w*\s+(?:about\s+|approximately\s+|around\s+|over\s+)?(\d[\d,]*)\s+people", text, re.I)
                if (a or b)]
            n = max(counts) if counts else None
            if n is not None and n >= 100:
                return "not_met", (f"Application states {n} employees (≥ 100) and non-compliance with the Workplace Gender Equality Act "
                                   "— an excluded entity (code compared figures + pattern match)")
            if n is None:
                return "unclear", ("Application states non-compliance with the Workplace Gender Equality Act but the headcount is unclear — "
                                   "the rule excludes only at ≥ 100 employees, officer must check")
    return None


def _au_bff(rule_id: str, text: str, meta: dict):
    if rule_id == "F01":  # TRAP: exactly 50% female-owned -> not a majority
        m = re.search(r"(\d{1,2})(?:\.\d+)?\s?(?:per cent|%)[^.\n]{0,70}(women|female)|(?:women|female)[^.\n]{0,70}?(\d{1,2})(?:\.\d+)?\s?(?:per cent|%)", text, re.I)
        if m:
            pct = int(m.group(1) or m.group(3))
            if pct <= 50:
                return "not_met", f"Female ownership/leadership {pct}% — not a 'majority' (>50%) (code compared figures)"
    if rule_id == "F05":
        for s in _sentences_with(text, r"income tax exempt"):
            if not re.search(r"\bnot\b|\bno\b", s, re.I):
                return "not_met", "Application states the organisation is income tax exempt — excluded (code pattern match)"
    if rule_id == "F06":
        m = re.search(r"grant (?:amount )?(?:requested|of)[^.\n]{0,25}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m:
            v = int(m.group(1).replace(",", ""))
            if v < 25_000 or v > 480_000:
                return "not_met", f"Amount requested ${m.group(1)} outside the $25,000–$480,000 range (code compared figures)"
    return None


def _au_onfarm(rule_id: str, text: str, meta: dict):
    if rule_id == "O01":  # TRAP: cropping-only farm, no livestock
        if re.search(r"cropping only|solely (grows|crops)|no livestock|does not (run|keep|hold)[^.\n]{0,30}(livestock|stock)|grain[- ]only", text, re.I):
            return "not_met", "Application states cropping only / no livestock — the fund is for the grazing industry only (code pattern match)"
    if rule_id == "O05":
        m = re.search(r"(\d{1,2})\s?(?:per cent|%)[^.\n]{0,70}(gross income|income from)", text, re.I)
        if m and int(m.group(1)) <= 50:
            return "not_met", f"Only {m.group(1)}% of income from primary production — not above 50% (code compared figures)"
    if rule_id == "O06":
        for s in _sentences_with(text, r"off[- ]farm assets"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) > 5_000_000:
                return "not_met", f"Off-farm assets ${m.group(1)} exceed the $5,000,000 cap (code compared figures)"
    if rule_id == "O07":
        m = re.search(r"(?:rebate|claim(?:ed|ing)?|amount)[^.\n]{0,40}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m and int(m.group(1).replace(",", "")) > 25_000:
            return "not_met", f"Amount requested ${m.group(1)} exceeds the $25,000 cap (code compared figures)"
    return None


_FUND_GUARDS = {"nsf-22-586": _career, "au-wine-tourism-r8": _au_wine,
                "au-cyber-skills-r2": _au_cyber, "au-female-founders-r1": _au_bff,
                "au-onfarm-water": _au_onfarm}


# ======================================================================
# GENERIC GUARD COMPILER — compiles constraints from the VERBATIM rule text.
# Answer to the customer's critique: a new fund gets a basic guard layer
# (money thresholds, %, prohibited items, exclusion lists) as soon as its
# ruleset is loaded — it does not depend on someone writing hand guards.
# Hand guards (when present) are a refinement layer ON TOP, not a prerequisite.
# ======================================================================
_STOP = set("the a an of in for and or to be is are with under have has must you your that this "
            "any all not no on at by from as it its their they per cent gst exclusive".split())


def _anchors(quote: str, pos: int, window: int = 60):
    """Identifying words around the constraint's position in the quote — used so that figures are compared only in
    application sentences about THAT topic (avoids comparing an unrelated number)."""
    seg = quote[max(0, pos - window):pos + window]
    words = [w.strip(".,;:()").lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", seg)]
    return [w for w in words if w not in _STOP][:6]


# Signs that a rule has a structure REGEX CANNOT express -> must be admitted, never silently ignored.
# (customer critique #1: W04 "in excess of any such sales used to meet the threshold" is two-layer logic)
_DERIVED_PAT = re.compile(
    r"in excess of any such|in excess of (?:the|those|any)|relative to|proportion of the|"
    r"used to meet the|after (?:deducting|excluding)|net of|remainder of|balance of|"
    r"as a (?:share|percentage) of|calculated by reference to|equivalent to the (?:sum|total)", re.I)
# ALTERNATIVE branch: satisfied by one of several entities/routes -> a single sentence cannot establish a violation
# (critique #1: W06 allows the applicant OR a related entity)
_ALT_PAT = re.compile(r"and/or|(?<!\w)or their\b|or (?:its|his|her|the) related|"
                      r"related entit|or equivalent|or (?:an?\s+)?alternative|either .{2,40} or ", re.I)


def compile_rule_guards(rule: dict):
    """Compile quote -> machine-checkable constraints + RECORD what could not be compiled.
    Each constraint: {kind, op, value, anchors, label, [conditional], [alt]}.
    kind='uncompilable' = the rule shows quantitative/logical structure that regex cannot express."""
    q = rule.get("quote", "")
    out = []
    has_alt = bool(_ALT_PAT.search(q))
    # --- MONEY thresholds ---
    money = []
    for m in re.finditer(r"(at least|a minimum of|minimum of|more than|no more than|not exceed|may not exceed|cannot exceed|up to|maximum(?: that can be claimed)? is|expected to total a minimum of)\s*\$\s?([\d,]+)", q, re.I):
        op = "min" if re.search(r"least|minimum|more than", m.group(1), re.I) else "max"
        money.append({"kind": "money", "op": op, "value": int(m.group(2).replace(",", "")),
                      "anchors": _anchors(q, m.start()), "label": m.group(0)[:60]})
    # CONDITIONAL THRESHOLDS: several thresholds of the same direction in one quote (e.g. $400k general, $500k for BIO/ENG/OPP).
    # The compiler does NOT know which branch the application is in -> keep only the SAFEST threshold (min of the "min"s,
    # max of the "max"es) so it never raises a false alarm; also flag conditional -> a hand guard is needed.
    for op in ("min", "max"):
        same = [c for c in money if c["op"] == op]
        if len(same) > 1:
            keep = min(same, key=lambda c: c["value"]) if op == "min" else max(same, key=lambda c: c["value"])
            keep = dict(keep, conditional=True,
                        label=keep["label"] + f" (rule has {len(same)} conditional thresholds — safest one used)")
            money = [c for c in money if c["op"] != op] + [keep]
            vals = ", ".join("${:,}".format(c["value"]) for c in same)
            out.append({"kind": "uncompilable", "label": "conditional threshold",
                        "reason": f"the rule has {len(same)} conditional '{op}' thresholds ({vals}) — the compiler uses only "
                                  "the safest one to avoid false alarms; a hand guard is needed to branch correctly"})
    out.extend(money)
    m = re.search(r"[Ff]rom \$\s?([\d,]+)(?:\.\d+)? to \$\s?([\d,]+)", q)
    if m:
        a = _anchors(q, m.start())
        out.append({"kind": "money", "op": "min", "value": int(m.group(1).replace(",", "")), "anchors": a, "label": "lower bound"})
        out.append({"kind": "money", "op": "max", "value": int(m.group(2).replace(",", "")), "anchors": a, "label": "upper bound"})
    # --- PERCENTAGE thresholds ---
    for m in re.finditer(r"(at least|more than|majority[^.]{0,20}?|no more than|up to|minimum of)\s*(\d{1,3})\s*(?:per cent|%)", q, re.I):
        op = "min" if re.search(r"least|more than|majority|minimum", m.group(1), re.I) else "max"
        out.append({"kind": "percent", "op": op, "value": int(m.group(2)),
                    "anchors": _anchors(q, m.start()), "label": m.group(0)[:60]})
    if re.search(r"majority owned and led by women", q, re.I):
        out.append({"kind": "percent", "op": "min", "value": 51, "anchors": ["women", "female", "owned", "led"],
                    "label": "majority owned and led by women (>50%)"})
    # --- PROHIBITED ITEMS: "No X are permitted / is prohibited / must not include X" ---
    for m in re.finditer(r"\bNo ([\w\- ]{2,30}?) (?:is|are) (?:permitted|allowed)|inclusion of ([\w\- ]{3,40}?) is prohibited|must not (?:include|contain) ([\w\- ]{3,40})", q, re.I):
        term = next(t for t in m.groups() if t)
        out.append({"kind": "forbidden", "term": term.strip().rstrip("s"), "label": f"prohibited: {term.strip()}"})
    # --- EXCLUSION LIST: "not eligible ... if you are: a; b; c" ---
    m = re.search(r"not eligible[^:]{0,40}:\s*(.+)", q, re.I | re.S)
    if m:
        raw = [it.strip(" .;•·…") for it in re.split(r";|•|\n|(?<=\))\s*(?=[a-z])", m.group(1))]
        for it in [x for x in raw if len(x) > 4][:8]:
            # CONDITIONAL / quantitative exclusion items ("an employer of 100 or more employees that has not complied
            # with ...") used to be SILENTLY SKIPPED for being too long -> the coverage report overstated. Now admitted.
            conditional = re.search(r"\b(?:that|who|which|whose|unless|where|if)\b|"
                                    r"\d[\d,]*\s*(?:or more|or less|or fewer|employees|staff|per cent|%)|\$\s?\d", it, re.I)
            if conditional or len(it) >= 90:
                label = "conditional exclusion" if conditional else "complex exclusion"
                why = ("is conditional/quantitative — the compiler can only match the plain status, not check the condition"
                       if conditional else "is long and lists many entity types — the compiler cannot safely split it into matchable phrases")
                out.append({"kind": "uncompilable", "label": label, "reason": f"exclusion item {why} ('{it[:80]}'); hand guard needed"})
                continue
            core_term = re.sub(r"^(an?|the)\s+", "", it, flags=re.I)
            core_term = re.split(r"\(|,| unless | however | including ", core_term)[0].strip()
            if 4 < len(core_term) < 60:
                out.append({"kind": "excluded", "term": core_term, "label": f"excluded: {core_term}"})
    # --- ADMISSION: derived / two-layer logic that regex cannot express ---
    if _DERIVED_PAT.search(q):
        out.append({"kind": "uncompilable", "label": "derived logic",
                    "reason": "the rule compares a DERIVED value (excess / used portion / share of total) — "
                              "regex cannot express it; hand guard needed, or leave to LLM + officer"})
    # --- flag the alternative branch on every text-based constraint ---
    if has_alt:
        for c in out:
            if c["kind"] in ("forbidden", "excluded"):
                c["alt"] = True
        out.append({"kind": "uncompilable", "label": "alternative branch (OR)",
                    "reason": "the rule can be satisfied via an ALTERNATIVE entity/route (and/or, related entity, or equivalent) — "
                              "one negative sentence in the application is NOT enough to conclude a violation"})
    return out


# The negation must sit RIGHT NEXT to the term (same clause), not "somewhere in the sentence". Previously:
#   - "is neither an individual nor an unincorporated association" (a VALID application) was wrongly flagged because neither/nor was not understood;
#   - "is an unincorporated association and has no board" (a violation) was skipped because the sentence contained the word "no".
_NEG_BEFORE = re.compile(r"\b(?:no|not|never|neither|nor|none|without|isn't|aren't|wasn't|weren't)\b[^.;:]{0,30}$", re.I)
_NEG_AFTER = re.compile(r"^\w*\s*(?:\w+\s+){0,2}?(?:is|are|was|were|will be|has been|have been)\s+(?:not|never)\b|"
                        r"^\w*\s+(?:not|never)\s+(?:included|permitted|proposed|used|involved)\b", re.I)


def _term_pattern(c: dict):
    esc = re.escape(c["term"].strip())
    if c["kind"] == "excluded" and " " not in c["term"].strip():
        # A single bare word ("individual", "trust") easily collides with natural phrases ("individual mentoring") -> only
        # count it when the application declares a STATUS: "is / as / being (not) an individual", "individual applicant".
        return re.compile(rf"\b(?:is|are|am|as|being|be)\s+(?:\w+\s+){{0,2}}(?P<t>{esc})"
                          rf"(?=\s*(?:[.,;:)]|$)|\s+(?:applicant|person|entity|or|and|nor)\b)", re.I)
    return re.compile(rf"\b(?P<t>{esc})", re.I)


def _term_asserted(c: dict, sentence: str) -> bool:
    """Does the sentence ASSERT the prohibited/excluded term: the term is present and NOT negated right before/after it."""
    for m in _term_pattern(c).finditer(sentence):
        before = sentence[max(0, m.start("t") - 40):m.start("t")]
        after = sentence[m.end("t"):m.end("t") + 40]
        if not _NEG_BEFORE.search(before) and not _NEG_AFTER.search(after):
            return True
    return False


def _eval_compiled(cons: list, text: str):
    """Run the compiled constraints on the application text. Returns (verdict, reason) or None.
    kind='uncompilable' constraints are NOT evaluated (coverage reporting only);
    constraints with alt=True (rule allows an alternative branch) can only downgrade to 'unclear', never conclude not_met."""
    text = normalise(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    for c in cons:
        if c["kind"] == "uncompilable":
            continue
        if c["kind"] in ("money", "percent"):
            pat = r"\$\s?(\d[\d,]*\d|\d)" if c["kind"] == "money" else r"(\d{1,3})\s*(?:per cent|%)"
            # A 'max' constraint fires as soon as ONE value exceeds it -> very prone to false alarms when a sentence merely
            # happens to contain one keyword (e.g. the $100k grant cap compared with $1.5M revenue in a sentence containing
            # "cellar door sales"). So 'max' requires the sentence to match AT LEAST 2 of the rule's keywords;
            # 'min' only concludes when EVERY value is below the threshold, so 1 keyword is safe enough.
            need_anchors = 2 if c["op"] == "max" else 1
            anchors = set(c.get("anchors", []))
            matched_vals = []
            for s in sents:
                low = s.lower()
                if sum(1 for a in anchors if a in low) < need_anchors:
                    continue
                prev_end = 0
                for m in re.finditer(pat, s):
                    if c["op"] == "max":
                        # 'max' compares only figures with ≥ 2 rule keywords RIGHT BEFORE them (60 characters, not crossing the
                        # previous figure), not every figure in the sentence. Bug caught on 09-14 (BFF F06): "requests $450,000,
                        # being 50% of the estimated project cost of $900,000" compared $900,000 (project cost) with the GRANT cap
                        # $480,000 just because the sentence contained "estimated" and "grant" — a fund without hand guards would
                        # have OVERRIDDEN a valid application to not met.
                        window = low[max(prev_end, m.start() - 60):m.start()]
                        prev_end = m.end()
                        if sum(1 for a in anchors if a in window) < 2:
                            continue
                    matched_vals.append(int(m.group(1).replace(",", "")))
            if matched_vals:
                bad = ([v for v in matched_vals if v < c["value"]] if c["op"] == "min"
                       else [v for v in matched_vals if v > c["value"]])
                # min: conclude only when EVERY relevant value is below the threshold (avoids false alarms with several figures)
                if c["op"] == "min" and bad and len(bad) == len(matched_vals):
                    return "not_met", f"Value {min(bad):,} is below the threshold {c['value']:,} in the rule ('{c['label']}') — guard auto-compiled from the verbatim rule"
                if c["op"] == "max" and bad:
                    return "not_met", f"Value {max(bad):,} exceeds the cap {c['value']:,} in the rule ('{c['label']}') — guard auto-compiled from the verbatim rule"
        elif c["kind"] in ("forbidden", "excluded"):
            term = c["term"]
            bad = [s for s in sents if _term_asserted(c, s)]
            if bad:
                strong = c["kind"] == "forbidden" and not c.get("alt")
                return ("not_met" if strong else "unclear",
                        f"The application states '{term}' — the rule {('prohibits' if c['kind']=='forbidden' else 'excludes')} this item ('{c['label']}')"
                        + (" — the rule has an alternative branch, so only flagged UNCLEAR for the officer to decide" if c.get("alt") else "")
                        + " — guard auto-compiled from the verbatim rule")
    return None


# Rules that have a HAND-WRITTEN guard (refinement layer) — used by the coverage report
HAND_COVERAGE = {
    "nsf-22-586": {"R04", "R05", "R07", "R08", "R09", "R10", "R11", "R12"},
    "au-wine-tourism-r8": {"W03", "W04", "W06", "W08", "W09"},
    "au-cyber-skills-r2": {"C01", "C06", "C07", "C08"},
    "au-female-founders-r1": {"F01", "F05", "F06"},
    "au-onfarm-water": {"O01", "O05", "O06", "O07"},
}


def rule_guard_level(ruleset_id: str, rule: dict) -> str:
    """Protection level of ONE criterion — used to enforce friction in the workflow layer, not just for display:
      code-guarded       : has a hand guard and/or runnable constraints -> has a safety net.
      needs-manual-guard : has quantitative logic that code CANNOT express -> the AI alone is a risk.
      llm-only           : purely qualitative criterion.
    """
    cons = compile_rule_guards(rule)
    runnable = [c for c in cons if c["kind"] != "uncompilable"]
    if rule["id"] in HAND_COVERAGE.get(ruleset_id, set()) or runnable:
        return "code-guarded"
    return "needs-manual-guard" if cons else "llm-only"


def coverage(ruleset: dict) -> dict:
    """HONEST guard coverage — 3 levels, so nobody mistakes 'has a compiler = every fund is safe':
      code-guarded       : has a hand guard and/or runnable auto-compiled constraints.
      needs-manual-guard : the rule HAS quantitative/logical structure but the compiler CANNOT express it
                           (derived logic like W04, alternative branch like W06, conditional thresholds)
                           and nobody has written a hand guard yet -> THE RISKIEST SPOT, handle first.
      llm-only           : purely qualitative rule, nothing for code to check -> relies on LLM + officer.
    """
    hand = HAND_COVERAGE.get(ruleset["id"], set())
    rows = []
    for r in ruleset["rules"]:
        cons = compile_rule_guards(r)
        runnable = [c for c in cons if c["kind"] != "uncompilable"]
        gaps = [c for c in cons if c["kind"] == "uncompilable"]
        has_hand = r["id"] in hand
        if has_hand or runnable:
            kind = "code-guarded"
        elif gaps:
            kind = "needs-manual-guard"
        else:
            kind = "llm-only"
        rows.append({"rule": r["id"], "title": r["title"], "guard": kind,
                     "hand": has_hand, "auto_constraints": [c["label"] for c in runnable],
                     "gaps": [{"label": c["label"], "reason": c["reason"]} for c in gaps]})
    n_guarded = sum(1 for x in rows if x["guard"] == "code-guarded")
    n_manual = sum(1 for x in rows if x["guard"] == "needs-manual-guard")
    n_llm = sum(1 for x in rows if x["guard"] == "llm-only")
    # a rule with a hand guard that still has compiler gaps -> still worth reviewing
    partial = [x["rule"] for x in rows if x["guard"] == "code-guarded" and x["gaps"]]
    return {"ruleset": ruleset["id"], "n_rules": len(rows), "n_guarded": n_guarded,
            "n_needs_manual": n_manual, "n_llm_only": n_llm, "partial_rules": partial,
            "pct": round(n_guarded / max(len(rows), 1), 2), "rows": rows,
            "note": "'needs-manual-guard' = a rule with quantitative logic the compiler CANNOT express and no hand guard "
                    "yet — this is where a false pass slips through most easily; write a hand guard first. 'llm-only' = a "
                    "qualitative rule, relies on LLM + officer. False-pass figures are only valid for a set measured with a "
                    "test set whose labels an officer has approved."}


# ---------- generic guard for any ruleset ----------
def _generic(rule: dict, text: str):
    m = re.search(r"minimum of \$([\d,]+)", rule.get("quote", ""))
    if m:
        thr = int(m.group(1).replace(",", ""))
        vals = _budget_values(text)
        if vals and all(v < thr for v in vals):
            return "not_met", f"Total budget ${max(vals):,} < minimum ${thr:,} stated in the rule (code compared figures)"
    m = re.search(r"(?:may not|must not|no more than|not) exceed (\d+) pages", rule.get("quote", ""), re.I)
    if m:
        thr = int(m.group(1))
        pm = re.search(r"Project Description is\s+(\d+)\s+pages", text, re.I)
        if pm and int(pm.group(1)) > thr:
            return "not_met", f"{pm.group(1)} pages > {thr}-page limit in the rule (code compared figures)"
    return None


def check(ruleset_id: str, rule: dict, verdict: str, text: str, meta: dict = None):
    """Returns None or {action, verdict, reason, source}.

    The two layers are MERGED rather than mutually exclusive (fixes the critique "a hand guard switches the compiler off"):
      - The HAND guard and the COMPILER always BOTH run. If the hand guard misses the wording, the compiler still catches it.
      - Both fire                 -> not_met (double evidence, strongest).
      - Only the hand guard fires -> follow the hand guard (it understands fund-specific context).
      - Only the compiler fires & the rule HAS a hand guard -> downgrade to UNCLEAR, no override: the compiler may miss an
        exception the hand guard knows about, but it must NOT be silently ignored either.
      - Only the compiler fires & the rule has no hand guard -> by rule type (quantitative: override; qualitative: unclear).
    """
    meta = meta or {}
    ntext = normalise(text)
    fund_guard = _FUND_GUARDS.get(ruleset_id)
    hand = fund_guard(rule["id"], ntext, meta) if fund_guard else None
    hand = hand or _generic(rule, ntext)
    comp = _eval_compiled(compile_rule_guards(rule), ntext)
    hand_covered = rule["id"] in HAND_COVERAGE.get(ruleset_id, set())

    if hand and comp:
        source, hit = "hand+compiler", (("not_met" if "not_met" in (hand[0], comp[0]) else hand[0]),
                                        f"{hand[1]} | Auto-compiled layer confirms: {comp[1]}")
    elif hand:
        source, hit = "hand", hand
    elif comp:
        source, hit = "compiler", comp
    else:
        return None
    g_verdict, reason = hit

    if verdict == "met":  # the LLM let it through while code sees a violation -> block the false pass
        # the compiler alone on a rule that has a hand guard: warn, do not override
        if source == "compiler" and hand_covered:
            return {"action": "flag", "verdict": "unclear", "source": source,
                    "reason": reason + " — the hand guard did not catch this case; the auto-compiled layer is suspicious, officer must decide"}
        if rule.get("type") == "quantitative" and g_verdict == "not_met":
            return {"action": "override", "verdict": "not_met", "source": source, "reason": reason}
        return {"action": "flag", "verdict": "unclear", "source": source, "reason": reason}
    return {"action": "confirm", "verdict": verdict, "source": source, "reason": reason}
