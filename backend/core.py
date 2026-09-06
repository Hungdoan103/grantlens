"""core.py — pipeline thẩm định (dùng chung cho API và eval).

Với MỖI tiêu chí, 3 lớp xử lý tách biệt:
  Lớp A  RAG      : retrieve top-k đoạn của hồ sơ liên quan tới tiêu chí.
  Lớp B  EXTRACT  : LLM lượt 1 — chỉ trích DỮ KIỆN trung tính (fact, chunk_id, key_phrase). Không phán quyết.
  Lớp C  JUDGE    : LLM lượt 2 — chỉ nhìn danh sách dữ kiện đã chuẩn hóa + tiêu chí. KHÔNG nhìn văn gốc
                    => văn phong, ngữ pháp của người nộp không thể ảnh hưởng tới phán quyết (language-bias control).
  Lớp D  CITE     : code cắt CÂU nguyên văn từ chunk theo key_phrase (citation-by-retrieval) + string-match lớp 2.
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
        "note_vi": {"type": "string"},
    },
    "required": ["verdict", "confidence", "supporting_fact", "note_vi"],
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
note_vi: one short sentence in Vietnamese explaining the mapping from facts to verdict."""

SYS_LETTER = """You draft outcome letters for a grants office. Write in PLAIN English (CEFR B1: short sentences, common words, no jargon). Max 350 words.
Structure:
1) Decision line (eligible / not eligible / more information needed).
2) For each rule that is not met or needs information: name the rule in plain words, put the exact rule fragment and the exact application fragment in quotation marks (use the quotes given, verbatim — do not edit them), and one plain sentence why. If the officer wrote a reason, reflect it in plain words.
3) "What you can do": how to fix or what to send.
4) "How to appeal": they may appeal in writing within 30 days to the review board and may request the full assessment record.
Never comment on the applicant's English or writing style. Output plain text only, no markdown."""

SYS_RULES = """You convert a funding guideline into a checklist of ELIGIBILITY rules for automated evidence mapping.
Only include eligibility / compliance requirements (who may apply, PI status, limits, required documents, page limits, budget minimums, prohibitions). Skip review criteria and general advice.
Each rule: id (R01, R02...), type ("quantitative" if it contains a number/threshold/count, else "qualitative"), title_vi (short Vietnamese title), quote (the VERBATIM sentence(s) from the guideline, max 60 words, do not paraphrase)."""

RULES_SCHEMA = {
    "type": "object",
    "properties": {"rules": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "type": {"type": "string", "enum": ["quantitative", "qualitative"]},
        "title_vi": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["id", "type", "title_vi", "quote"]}}},
    "required": ["rules"],
}


RULESETS_DIR = DATA / "rulesets"


@lru_cache(maxsize=1)
def load_rulesets() -> dict:
    """Multi-GO: mỗi đợt/quỹ tài trợ một bộ tiêu chí riêng, có version. {id -> ruleset}."""
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


def get_ruleset(ruleset_id: str = None) -> dict:
    all_rs = load_rulesets()
    rid = ruleset_id or all_rs["_default"]
    if rid not in all_rs:
        raise KeyError(f"Không có bộ tiêu chí '{rid}'")
    return all_rs[rid]


def reload_rulesets():
    load_rulesets.cache_clear()


def load_rules():
    """Bộ tiêu chí mặc định (tương thích chỗ gọi cũ)."""
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


def assess_rule(case_id: str, text: str, rule: dict, k: int = 3, ruleset_id: str = None, meta: dict = None) -> dict:
    ret = get_retriever(case_id, text)
    hits = ret.retrieve(f"{rule['title_vi']} — {rule['quote']}", k=k)
    valid_ids = {h["id"] for h in hits}
    passages = "\n".join(f"[chunk {h['id']}] {h['text']}" for h in hits)

    # ---- Lớp B: EXTRACT (lượt 1) ----
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

    # ---- Lớp C: JUDGE (lượt 2) — chỉ dữ kiện, không văn gốc ----
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
        idx = jd.get("supporting_fact") or 1
        idx = idx if 1 <= idx <= len(facts) else 1
        note = jd.get("note_vi", "")
    else:
        verdict, confidence, idx, note = "not_addressed", "high", 0, "Không có dữ kiện nào trong hồ sơ liên quan tới tiêu chí này."

    # ---- Lớp D: CITE — code cắt câu nguyên văn ----
    if facts:
        sf = facts[idx - 1]
        chunk = next(h for h in hits if h["id"] == sf["chunk_id"])
        aq = extract_quote(chunk["text"], sf["key_phrase"])
        chunk_id = chunk["id"]
    else:
        aq, chunk_id = "", hits[0]["id"]
    cite_ok = (aq == "") or quote_in_source(aq, text)

    # ---- Lớp E: GUARD — code chặn false-pass trên rule kiểm được bằng số liệu/mẫu chữ ----
    from . import guards
    guard = guards.check(ruleset_id or get_ruleset()["id"], rule, verdict, text, meta or {})
    if guard and guard["action"] == "override":
        note = f"[CHẶN FALSE-PASS] {guard['reason']}. (LLM trả 'met' nhưng mã nguồn kiểm số liệu xác định vi phạm — rule định lượng do code quyết.) | LLM: {note}"
        verdict, confidence = guard["verdict"], "high"
    elif guard and guard["action"] == "flag":
        note = f"[NGHI FALSE-PASS] {guard['reason']} — hạ xuống CHƯA RÕ, bắt buộc cán bộ quyết. | LLM: {note}"
        verdict, confidence = "unclear", "low"

    needs_attention = verdict in ("unclear", "not_addressed") or confidence == "low" or not cite_ok or bool(guard)

    return {
        "guard": guard,
        "r": rule["id"], "title": rule["title_vi"], "type": rule["type"],
        "v": verdict, "confidence": confidence,
        "facts": facts, "coverage": coverage, "supporting_fact": idx,
        "rq": _short(rule["quote"]), "aq": aq, "chunk_id": chunk_id,
        "retrieval_scores": [{"chunk": h["id"], "score": h["score"]} for h in hits],
        "cite_rule_ok": True, "cite_app_ok": cite_ok,
        "needs_attention": needs_attention,
        "note": note,
    }


def assess_case(case_id: str, text: str, progress=None, ruleset_id: str = None, meta: dict = None):
    """Generator: yield từng verdict để API stream tiến độ."""
    rules = get_ruleset(ruleset_id)["rules"]
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
- Language of the template follows the language the rules are written in (English rules -> English template; Vietnamese rules -> Vietnamese template). Keep instructions in parentheses short.
- Output ONLY the template text. No commentary, no markdown fences."""


def generate_letter_template(rules_text: str) -> str:
    return llm.chat_text(SYS_TEMPLATE, f"OFFICE RULES ABOUT DECISION LETTERS:\n{rules_text[:8000]}\n\nProduce the template.", max_tokens=900).strip()


def load_letter_template() -> str:
    """Mẫu thư của đơn vị (data/letter-template.txt) — dòng đầu là ghi chú nội bộ thì bỏ qua."""
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
    """Lớp chặn deterministic: email / số điện thoại / ngày không có trong dữ liệu đầu vào
    là do model bịa -> thay bằng placeholder cho cán bộ điền. Không tin prompt suông."""
    import re
    from datetime import date
    letter = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+",
                    lambda m: m.group(0) if m.group(0) in allowed else "{EMAIL LIÊN HỆ — cán bộ điền}", letter)
    letter = re.sub(r"\+?\d[\d ()\-]{7,}\d",
                    lambda m: m.group(0) if m.group(0) in allowed else "{SĐT LIÊN HỆ — cán bộ điền}", letter)
    today = date.today().isoformat()
    letter = re.sub(r"\b\d{4}-\d{2}-\d{2}\b",
                    lambda m: m.group(0) if (m.group(0) == today or m.group(0) in allowed)
                    else "{HẠN — cán bộ ấn định}", letter)
    return letter


def extract_rules(guideline_text: str) -> list:
    out = llm.chat_json(SYS_RULES, f"GUIDELINE TEXT:\n{guideline_text[:12000]}", RULES_SCHEMA, max_tokens=2500)
    rules = out.get("rules") or []
    # Lớp phòng thủ: quote phải là nguyên văn trong guideline, không thì gắn cờ
    norm = " ".join(guideline_text.split()).lower()
    for r in rules:
        r["verbatim_ok"] = " ".join(str(r.get("quote", "")).split()).lower() in norm
    return rules
