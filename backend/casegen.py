"""casegen.py — AI SINH BỘ TEST CÓ NHÃN cho từng quỹ (ruleset), kèm cơ chế chống nhiễu và phê chuẩn nhãn.

Chuỗi đo chuẩn cho MỖI quỹ (số của quỹ này KHÔNG dùng cho quỹ khác):
    --gen  ->  rà văn bản (--confirm / --dispute / --flag-weak)  ->  --approve  ->  --eval  ->  [MỤC TIÊU]

Vì sao nhãn đáng tin (và vì sao vẫn PHẢI có người rà):
  1. VI PHẠM DO MÃ NGUỒN CHỐT TRƯỚC. Bước 1: LLM chỉ đề xuất MỘT câu dữ kiện vi phạm rule mục tiêu; câu đó
     phải được guard (mã nguồn) hoặc verifier xác nhận là vi phạm thật. Bước 2: LLM viết hồ sơ và BẮT BUỘC
     chứa NGUYÊN VĂN câu đó — mã nguồn kiểm. (Sửa lỗi 12/09/2026: chỉ dặn "hãy vi phạm rule X" thì LLM
     thường viết ra hồ sơ hợp lệ hoàn toàn — 7/7 case ở Cyber Skills + Female Founders hỏng đúng kiểu này.)
  2. CHỐNG LỘ ĐÁP ÁN bằng mã nguồn: văn bản nhắc mã rule hoặc tự bình luận "violates / satisfies /
     meets all criteria..." bị loại (bộ Wine cũ có câu "violating W08" nên đã bị thu hồi).
  3. GUARD ĐỐI SOÁT: guard bắn vào rule ngoài mục tiêu -> viết lại. Với vi phạm định lượng đã được guard
     xác nhận, guard phải VẪN bắt trên toàn văn -> chặn văn bản có con số khác đè lên câu vi phạm.
  4. VERIFIER dùng MODEL KHÁC hệ đang bị đo (GRANTLENS_CASEGEN_MODEL) và hỏi HAI CÁCH ĐẶT CÂU khác nhau
     ("có thoả không?" / "có vi phạm không?"). Ở temperature 0, hỏi lặp một câu y hệt thì phiếu nào cũng
     giống nhau — đó không phải phiếu độc lập. Verifier chỉ GẮN CỜ, người rà quyết; còn cờ chưa xử lý thì
     KHÔNG phê chuẩn được — tránh âm thầm loại các case khó (thiên lệch chọn mẫu làm đẹp số).
  5. ĐỐI CHỨNG: ở phạm vi 'target', rule mục tiêu còn được chấm trên hồ sơ SẠCH. Không có đối chứng thì một
     hệ "từ chối tất cả" cũng đạt 100% bắt vi phạm.
  6. Nhãn lưu approved=false; sửa nhãn sau khi phê chuẩn thì mất phê chuẩn; eval ghi provisional nếu chưa duyệt.

CLI:
  python -m backend.casegen <ruleset_id> --gen [N] [--target-only]     sinh bộ test
  python -m backend.casegen <ruleset_id> --confirm <case_id> "lý do"   xác nhận vi phạm có thật dù verifier gắn cờ
  python -m backend.casegen <ruleset_id> --confirm-control <case_id> <rule_id> "lý do"   hồ sơ sạch thoả rõ rule -> đối chứng
  python -m backend.casegen <ruleset_id> --dispute <case_id> "lý do"   loại 1 case sai nhãn khỏi metric
  python -m backend.casegen <ruleset_id> --flag-weak <case_id> <rule_id> "lý do"
  python -m backend.casegen <ruleset_id> --approve "Tên người rà"      phê chuẩn nhãn
  python -m backend.casegen <ruleset_id> --eval                         đo
Biến môi trường: GRANTLENS_CASEGEN_MODEL = model sinh + kiểm nhãn (mặc định: cùng model hệ thống).
"""
import json, os, re, sys
from datetime import datetime
from . import core, llm, guards

GEN_DIR = core.DATA / "applications" / "generated"
LBL_DIR = core.DATA / "labels"
GEN_MODEL = os.environ.get("GRANTLENS_CASEGEN_MODEL") or None
VERIFIER_MODEL = os.environ.get("GRANTLENS_VERIFIER_MODEL") or GEN_MODEL
# Planner chỉ trả JSON bị ép schema -> dùng được cả model "chỉ-suy-nghĩ"; người viết văn bản tự do thì KHÔNG.
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


# ---------------- kiểm cơ học (không dùng LLM) ----------------
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
    """Chặn LỘ ĐÁP ÁN bằng MÃ NGUỒN (không hỏi LLM).

    Hồ sơ thi tự sinh hay kèm lời tự thú: "This violates C01", "satisfying W08", "meets all criteria".
    Hồ sơ thật KHÔNG BAO GIỜ viết vậy, và nếu để lọt thì hệ thẩm định có thể chấm đúng vì ĐỌC ĐƯỢC
    ĐÁP ÁN chứ không phải vì suy luận -> số đo bị thổi phồng. Phát hiện -> viết lại.
    """
    problems = []
    ids = sorted({r["id"] for r in ruleset["rules"]}, key=len, reverse=True)
    hit_ids = [i for i in ids if re.search(rf"\b{re.escape(i)}\b", text)]
    if hit_ids:
        problems.append("lộ mã rule: " + ", ".join(hit_ids))
    m = _META_PAT.search(text)
    if m:
        problems.append(f"câu tự bình luận quy tắc: '{m.group(0)}'")
    if "[[FACT]]" in text:
        problems.append("còn sót dấu [[FACT]] của bộ sinh")
    return problems


def _norm_cmp(s: str) -> str:
    return re.sub(r"\s+", " ", guards.normalise(s or "")).strip().strip('"').rstrip(".").lower()


def _contains(text: str, fact: str) -> bool:
    """Văn bản có chứa NGUYÊN VĂN câu vi phạm đã chốt không (so sau chuẩn hoá khoảng trắng/dấu/tiền tệ)."""
    return bool(fact) and _norm_cmp(fact) in _norm_cmp(text)


def _insert_fact(text: str, fact: str):
    """MÃ NGUỒN chèn câu vi phạm đã chốt vào chỗ đánh dấu [[FACT]] — không phó mặc LLM chép lại (model nhỏ
    hay chép sai/diễn giải lại). Thiếu dấu thì nối thành đoạn cuối."""
    if "[[FACT]]" in text:
        out, how = text.replace("[[FACT]]", fact, 1).replace("[[FACT]]", ""), "marker"
    else:
        out, how = text.rstrip() + "\n\nAdditional Details\n" + fact, "appended"
    # người viết hay chép luôn câu vi phạm cạnh dấu -> câu lặp đôi: giữ lần xuất hiện đầu tiên
    first = out.find(fact)
    if first >= 0:
        cut = first + len(fact)
        out = out[:cut] + out[cut:].replace(fact, "")
        out = re.sub(r"[ \t]{2,}", " ", out)
    return out, how


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _repair_meta(ruleset: dict, text: str, keep: str = None):
    """Bỏ từng CÂU tự bình luận quy tắc / nhắc mã rule (vd "This exceeds the minimum $500,000 requirement.")
    thay vì vứt cả hồ sơ. KHÔNG BAO GIỜ bỏ câu vi phạm đã chốt. Hồ sơ sau khi bỏ vẫn phải qua lại mọi chốt
    kiểm (lộ đề, đối soát guard, verifier), và danh sách câu đã bỏ được lưu cho người rà xem."""
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


# Dấu hiệu văn bản KHÔNG PHẢI hồ sơ mà là lời model tự nói với mình ("Okay, the user wants me to write...").
# Đo thật 14/09/2026: qwen3:4b (bản chỉ-suy-nghĩ) bỏ qua think=false và trả nguyên đoạn độc thoại; mọi chốt kiểm
# cũ vẫn cho qua vì câu vi phạm được chèn vào giữa độc thoại — chỉ bước người rà phát hiện. Nay chặn bằng mã nguồn.
_MONOLOGUE_PAT = re.compile(
    r"\b(?:the user wants|user wants|the user (?:asked|specified|has given)|I need to|I'll|I will write|let me|let's|"
    r"we are writing|we must (?:not )?write|the marker|marker topic|placeholder|the instructions?|the prompt|"
    r"case seed|word count|brainstorm\w*|application summary for)\b|^\s*(?:okay|ok|alright|sure|hmm)\b", re.I | re.M)


def _doc_problems(raw: str) -> list:
    """Bản viết có phải một HỒ SƠ không — kiểm cơ học trên bản thô, trước khi chèn câu vi phạm / xoá câu bình luận."""
    problems = []
    hits = sorted({m.group(0).strip().lower() for m in _MONOLOGUE_PAT.finditer(raw)})
    if hits:
        problems.append("không phải hồ sơ — model tự độc thoại: " + ", ".join(hits[:4]))
    n = len(raw.split())
    if not 120 <= n <= 520:
        problems.append(f"độ dài bất thường ({n} từ)")
    return problems


def _guard_crosscheck(ruleset: dict, text: str, target_id: str):
    fired = {}
    for r in ruleset["rules"]:
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] in ("override", "flag"):
            fired[r["id"]] = g["reason"][:70]
    wrong = [rid for rid in fired if rid != target_id]
    return (not wrong), (target_id in fired), fired


# ---------------- verifier (LLM, model tách khỏi hệ bị đo) ----------------
def _votes(rule: dict, text: str):
    """Hai phiếu HỎI HAI CÁCH, quy về cùng một thang "rule có được thoả không": yes / no / unclear.
    Không đưa mã rule vào câu hỏi để verifier không thiên theo mã."""
    body = f'RULE: "{rule["quote"]}"\n\nAPPLICATION TEXT:\n{text}'
    out = []
    for system, flip in ((SYS_VERIFY, False), (SYS_VERIFY_VIOL, True)):
        try:
            a = llm.chat_json(system, body, VERIFY_SCHEMA, max_tokens=160, model=VERIFIER_MODEL).get("answer", "unclear")
        except llm.LLMError:
            out.append("error")   # ghi rõ lỗi gọi model — không để lỗi trông giống "không rõ" (GPU dùng chung hay timeout)
            continue
        out.append({"yes": "no", "no": "yes"}.get(a, a) if flip else a)
    return out


def _verify(ruleset: dict, text: str, target_id: str):
    """Kiểm nhãn PHỤ (phạm vi 'full'):
      • Mọi phiếu nói có bằng chứng -> nhãn met đáng tin. Phiếu mâu thuẫn -> giữ metric, gắn cờ.
        Mọi phiếu nói không rõ -> 'weak', loại khỏi metric.
      • Mã nguồn PHỦ QUYẾT: guard khẳng định rule vi phạm bằng số liệu -> nhãn met chắc chắn sai -> weak.
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


# ---------------- sinh ----------------
def _confirm_fact(ruleset: dict, rule: dict, fact: str):
    """Câu dữ kiện có THẬT là vi phạm không: ưu tiên mã nguồn (guard), không được thì 2 phiếu verifier."""
    g = guards.check(ruleset["id"], rule, "met", fact, {})
    if g and g["action"] in ("override", "flag"):
        return True, "code"
    v = _votes(rule, fact)
    if v and all(a == "no" for a in v):
        return True, "verifier"
    return False, f"verifier {v}"


def _plan_fact(ruleset: dict, rule: dict, seed: str, tries: int = 4):
    why, rejected = "chưa thử", []
    for k in range(tries):
        # temperature 0: không đổi đề thì lần thử sau y hệt lần trước -> đưa câu bị loại KÈM LÝ DO vào để buộc đổi hướng
        retry = ("\n\nThese earlier attempts were REJECTED:\n"
                 + "\n".join(f'- "{r}" (reason: {why_en})' for r, why_en in rejected[-3:])
                 + "\nWrite a different sentence that directly contradicts the rule and avoids those problems.") if rejected else ""
        try:
            # không đưa "hạt giống" vào prompt: model lấy luôn chuỗi đó làm tên tổ chức ("Case 2-0")
            d = llm.chat_json(SYS_PLAN, f'PROGRAM: {ruleset["name"]}\nRULE: "{rule["quote"]}"{retry}\n\n'
                                        "Write the fact sentence now.",
                              PLAN_SCHEMA, max_tokens=120, model=PLANNER_MODEL)
        except llm.LLMError as e:
            why = f"lỗi LLM: {e}"
            continue
        fact = re.sub(r"\s+", " ", guards.normalise(d.get("fact", ""))).strip().strip('"')
        if len(fact.split()) < 6:
            why = f"câu quá ngắn: {fact!r}"
            continue
        if _MONOLOGUE_PAT.search(fact) or re.search(r"\bcase\s*\d", fact, re.I):
            why = f"câu không phải dữ kiện hồ sơ: {fact[:90]}"
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
        why = f"chưa phải vi phạm rõ ({how}) — {fact[:90]}"
        rejected.append((fact, "it does not clearly fail this exact rule"))
    return None, why


# temperature 0: thử lại với prompt y hệt thì ra y hệt -> đổi VĂN PHONG giữa các lần (không dùng mã/hạt giống).
_STYLE = ["", "Use title-case paragraph headings.", "Use slightly more formal wording and different paragraph headings."]


def _gen_text(ruleset: dict, target_rule: dict = None, fact: str = None, variant: int = 0) -> str:
    rules_txt = "\n".join(f'- "{r["quote"]}"' for r in ruleset["rules"])   # KHÔNG đưa mã rule cho người viết
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
    """scope='full'  : kiểm TẤT CẢ nhãn phụ (đắt) -> đo được cả metric phụ.
       scope='target': chỉ kiểm rule MỤC TIÊU + đối chứng trên hồ sơ sạch (rẻ) -> đo [MỤC TIÊU] + đối chứng;
                       file nhãn ghi rõ phạm vi để eval KHÔNG báo cáo số phụ như thể đã kiểm."""
    rs = core.get_ruleset(ruleset_id)
    rules = rs["rules"]
    guarded = [r for r in rules if guards.compile_rule_guards(r) or r["id"] in guards.HAND_COVERAGE.get(ruleset_id, set())]
    targets = (guarded if n_violations is None else guarded[:n_violations]) or rules[:n_violations or 3]
    out_dir = GEN_DIR / ruleset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.txt"):   # bộ mới thay hẳn bộ cũ (file nhãn cũng bị ghi đè) — lịch sử nằm trong git
        old.unlink()
    print(f"[casegen] {ruleset_id} · chốt vi phạm: {_planner_model_name()} · viết: {_gen_model_name()} · "
          f"kiểm nhãn: {_verifier_model_name()} · phạm vi: {scope} · "
          f"rule mục tiêu: {[t['id'] for t in targets]}", flush=True)
    cases, skipped = [], []
    for i, tgt in enumerate([None] + targets):  # case đầu = hồ sơ sạch (đối chứng)
        cid = f"GEN-{ruleset_id}-{i:02d}" + ("-clean" if tgt is None else f"-{tgt['id']}")
        tid = tgt["id"] if tgt else "__none__"
        fact, how = None, None
        # hạt giống TRUNG TÍNH: mã case chứa mã rule (vd "...-01-C01") -> đưa vào prompt là tự lộ đáp án
        seed = f"case{i}"
        if tgt:
            fact, how = _plan_fact(rs, tgt, seed)
            if not fact:
                skipped.append({"id": cid, "reason": "không chốt được câu vi phạm rõ ràng sau 4 lần", "detail": how})
                print(f"  {cid}: BỎ — không chốt được câu vi phạm ({how})", flush=True)
                continue
        problem, agree, text = "chưa viết", False, ""
        placement, removed, rest_votes = None, [], None
        for k in range(3):
            text = _gen_text(rs, tgt, fact, k)
            shape = _doc_problems(text)
            if shape:
                problem = shape[0]
                continue
            if fact:
                text, placement = _insert_fact(text, fact)
            # hồ sơ SẠCH cũng có thể bị người viết chèn dấu [[FACT]] (On-farm 14/09) -> xoá dấu sót ở mọi case
            text = re.sub(r"[ \t]*\[\[FACT\]\][ \t]*", " ", text)
            text, removed = _repair_meta(rs, text, keep=fact)
            if len(removed) > 3:   # hồ sơ thật lỡ vài câu bình luận thì sửa được; nhiều hơn là văn bản hỏng
                problem = f"quá nhiều câu bình luận quy tắc phải xoá ({len(removed)})"
                continue
            if fact and not _contains(text, fact):
                problem = "văn bản không chứa nguyên văn câu vi phạm"
                continue
            leaks = _leak_check(rs, text)
            if leaks:
                problem = leaks[0]
                continue
            ok, agree, fired = _guard_crosscheck(rs, text, tid)
            if not ok:
                problem = f"guard bắn vào rule ngoài mục tiêu: {sorted(r for r in fired if r != tid)}"
                continue
            if how == "code" and not agree:
                problem = "guard không còn bắt trên toàn văn — có dữ kiện khác đè lên câu vi phạm"
                continue
            if tgt and verify:
                # CHỐNG TỰ MÂU THUẪN: bỏ câu vi phạm đi thì phần còn lại phải IM LẶNG về tiêu chí mục tiêu.
                # Nếu cả hai phiếu vẫn thấy tiêu chí được thoả (vd "CEO đã ký xác nhận" bên cạnh "không có board"),
                # hồ sơ tự mâu thuẫn, không phải đề thi sạch -> viết lại. (Cyber 14/09: 2/3 case hỏng đúng kiểu này.)
                rest_votes = _votes(tgt, re.sub(r"[ \t]{2,}", " ", text.replace(fact, " ")))
                if rest_votes and all(a == "yes" for a in rest_votes):
                    problem = f"tự mâu thuẫn — bỏ câu vi phạm đi, phần còn lại vẫn cho thấy tiêu chí được thoả {rest_votes}"
                    continue
            problem = None
            break
        if problem:
            skipped.append({"id": cid, "reason": problem + " (sau 3 lần viết)", "fact": fact,
                            "last_text": text[:2000]})   # giữ bản viết cuối để người rà xem vì sao bị bỏ
            print(f"  {cid}: BỎ — {problem}", flush=True)
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
        elif verify and not tgt:  # đối chứng: rule mục tiêu chấm trên hồ sơ sạch
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
            print(f"  {cid}: vi phạm {tgt['id']} [chốt bởi {how}] · guard {'XÁC NHẬN' if agree else 'không bắt'} · "
                  + ("verifier xác nhận" if clear else f"!! VERIFIER GẮN CỜ {verifier.get(tgt['id'])} — cần người rà"),
                  flush=True)
        else:
            print(f"  {cid}: sạch · đối chứng dùng được cho {control}", flush=True)

    doc = {"ruleset": ruleset_id, "version": rs["version"], "label_scope": scope,
           "generator_model": _gen_model_name(), "verifier_model": _verifier_model_name(),
           "planner_model": _planner_model_name(),
           "generated_by": _gen_model_name(), "targets": [t["id"] for t in targets],
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "approved": False, "approved_by": None, "approved_at": None,
           "n_needs_review": sum(1 for c in cases if c["status"] == "needs_review"),
           "note": "Vi phạm do mã nguồn chốt trước (câu dữ kiện được guard/verifier xác nhận, văn bản phải chứa "
                   "nguyên văn); chặn lộ đáp án bằng mã nguồn; verifier dùng model tách khỏi hệ bị đo. "
                   "CHƯA PHÊ CHUẨN — số đo từ bộ này là TẠM (provisional). Rà văn bản, xử lý hết cờ rồi --approve.",
           "cases": cases, "skipped": skipped}
    (LBL_DIR / f"generated-{ruleset_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


# ---------------- rà & phê chuẩn ----------------
def _load(ruleset_id):
    p = LBL_DIR / f"generated-{ruleset_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"Chưa có bộ test sinh cho '{ruleset_id}' — chạy --gen trước")
    return p, json.loads(p.read_text(encoding="utf-8"))


def _case(doc: dict, case_id: str) -> dict:
    hit = next((c for c in doc["cases"] if c["id"] == case_id), None)
    if not hit:
        raise KeyError(case_id)
    return hit


def _save_edited(p, doc):
    """Mọi chỉnh sửa nhãn sau khi phê chuẩn đều làm MẤT phê chuẩn — phải duyệt lại."""
    doc.update(approved=False, approved_by=None, approved_at=None,
               n_needs_review=sum(1 for c in doc["cases"] if c.get("status") == "needs_review"))
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def approve(ruleset_id: str, officer: str):
    """Phê chuẩn nhãn sau khi đã rà văn bản — từ đây số đo mới hết 'provisional'.
    Từ chối nếu còn case bị verifier gắn cờ mà chưa ai xử lý (confirm hoặc dispute)."""
    p, doc = _load(ruleset_id)
    pending = [c["id"] for c in doc["cases"] if c.get("status") == "needs_review"]
    if pending:
        raise ValueError(f"Còn {len(pending)} case bị verifier gắn cờ chưa rà: {pending} — "
                         "đọc văn bản rồi --confirm (vi phạm có thật) hoặc --dispute (nhãn sai) trước khi phê chuẩn")
    # Soát lại bằng BỘ LỌC HIỆN HÀNH trước khi phê chuẩn: bộ lọc lộ đáp án được siết dần qua các lượt rà
    # (vd "falling short of the $1,207,000 threshold" lọt ở Wine 14/09) — case vi phạm sinh bằng bộ lọc cũ
    # vẫn phải qua bộ lọc mới, vì câu lộ đáp án ở case vi phạm thổi phồng thẳng vào metric chính.
    rs = core.get_ruleset(ruleset_id)
    leaky = {c["id"]: _leak_check(rs, (core.DATA / "applications" / c["file"]).read_text(encoding="utf-8"))
             for c in doc["cases"] if c.get("target") and c.get("status") != "disputed"}
    leaky = {k: v for k, v in leaky.items() if v}
    if leaky:
        raise ValueError(f"Case vi phạm còn lộ đáp án theo bộ lọc hiện hành: {leaky} — --dispute hoặc sinh lại trước khi phê chuẩn")
    live = [c for c in doc["cases"] if c.get("status") != "disputed"]
    doc.update(approved=True, approved_by=officer, approved_at=datetime.now().isoformat(timespec="seconds"))
    for c in doc["cases"]:
        if c.get("status") == "pending_review":
            c["status"] = "approved"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "approved_by": officer, "n_cases": len(live)}


def confirm_target(ruleset_id: str, case_id: str, reason: str):
    """Người rà đọc văn bản và xác nhận vi phạm mục tiêu CÓ THẬT dù verifier gắn cờ.
    Đây chính là các case khó mà metric cần giữ lại, không được âm thầm loại."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    if not hit.get("target"):
        raise ValueError("case sạch không có rule mục tiêu để xác nhận")
    hit.update(target_label_clear=True, target_confirmed_by_reviewer=reason, status="pending_review")
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "reason": reason}


def confirm_control(ruleset_id: str, case_id: str, rule_id: str, reason: str):
    """Người rà đọc hồ sơ SẠCH và xác nhận nó thoả RÕ RÀNG rule mục tiêu -> dùng rule đó làm đối chứng.
    Verifier chỉ gắn cờ: model kiểm nhỏ có thể trả 'không rõ' dù văn bản nêu rõ (đo thật 14/09: cùng một văn bản
    Cyber, lượt trước yes/yes, lượt sau không) — bỏ đối chứng vì thế sẽ làm mất phép đo báo động giả."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    if hit.get("target"):
        raise ValueError("chỉ áp dụng cho hồ sơ sạch (case không có rule mục tiêu)")
    if rule_id not in (doc.get("targets") or []):
        raise ValueError(f"{rule_id} không phải rule mục tiêu của bộ test này ({doc.get('targets')})")
    hit.setdefault("control_rules", [])
    if rule_id not in hit["control_rules"]:
        hit["control_rules"].append(rule_id)
    hit.setdefault("control_confirmed_by_reviewer", {})[rule_id] = reason
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "control_rules": hit["control_rules"], "reason": reason}


def flag_weak(ruleset_id: str, case_id: str, rule_id: str, reason: str):
    """Người rà đánh dấu MỘT nhãn phụ không đáng tin -> loại khỏi metric (giữ case)."""
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


def dispute(ruleset_id: str, case_id: str, reason: str):
    """Đánh dấu một case là sai/đáng ngờ -> loại khỏi mọi metric (giữ lại để truy vết)."""
    p, doc = _load(ruleset_id)
    hit = _case(doc, case_id)
    hit.update(status="disputed", dispute_reason=reason)
    _save_edited(p, doc)
    return {"ok": True, "case": case_id, "reason": reason}


# ---------------- đo ----------------
def evaluate(ruleset_id: str):
    """Đo hệ thẩm định (model hệ thống) trên bộ sinh — TÁCH metric để không lẫn nhiễu đề thi:
      • target  : rule mục tiêu của từng case vi phạm — THƯỚC ĐO CHÍNH (bắt được / false-pass / đẩy về cán bộ).
      • control : (phạm vi 'target') rule mục tiêu chấm trên hồ sơ SẠCH — báo động giả.
      • secondary: (phạm vi 'full') rule phụ có bằng chứng rõ — tham khảo.
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
    print(f"EVAL '{ruleset_id}' · hệ bị đo: {info['model']} · bộ nhãn sinh bởi: "
          f"{doc.get('generator_model') or doc.get('generated_by')} · {len(live)} case"
          + (f" (bỏ {len(disputed)} disputed)" if disputed else "")
          + (f" (bỏ {len(flagged)} case còn cờ chưa rà)" if flagged else ""))
    if scope == "target":
        print("PHẠM VI: rule MỤC TIÊU trên case vi phạm + đối chứng trên hồ sơ sạch (không đo, không báo cáo rule phụ).")
    if not approved:
        print("!! NHÃN CHƯA PHÊ CHUẨN — số dưới đây là TẠM (provisional), không dùng để tuyên bố.")
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
    print("# THƯỚC ĐO CHÍNH — [MỤC TIÊU]: mỗi case có đúng 1 vi phạm cài sẵn, hệ có bắt được không")
    print(f"#   Bắt đúng vi phạm      : {T['ok']}/{T['n']}" + (f" = {T['ok']/T['n']:.0%}" if T["n"] else ""))
    print(f"#   FALSE-PASS            : {T['fp']}/{T['n']} (kỳ vọng 0 — chỉ số quan trọng nhất)")
    print(f"#   Đẩy về cán bộ (chưa rõ): {routed}/{T['n']} (không phải lỗi lọt, nhưng tốn công người)")
    print("#" * 70)
    if scope == "target":
        print(f"[ĐỐI CHỨNG] rule mục tiêu trên hồ sơ SẠCH: đúng {C['ok']}/{C['n']} · BÁO ĐỘNG GIẢ {C['false_alarm']}/{C['n']}"
              " — không có dòng này thì hệ 'từ chối tất cả' cũng đạt 100% ở trên.")
    else:
        print(f"[THAM KHẢO] rule phụ có bằng chứng rõ: {S['ok']}/{S['n']}" + (f" = {S['ok']/S['n']:.0%}" if S["n"] else "")
              + " — không dùng làm thước đo chất lượng.")
        print(f"[LOẠI]      {excluded}/{total_seen} lượt rule bị loại ({excl_rate:.0%}) — nhãn phụ yếu.")
        if excl_rate > 0.3:
            print("!! CẢNH BÁO: loại >30% — bộ test tự sinh chất lượng thấp.")
    if T["n"] < 10:
        print(f"!! CỠ MẪU NHỎ ({T['n']} case vi phạm): đây là bằng chứng sơ bộ theo quỹ, không phải tỷ lệ thống kê.")
    if not approved:
        print("!! NHẮC LẠI: nhãn chưa phê chuẩn -> mọi số trên là TẠM TÍNH.")
    for e in errors:
        print(f"  [{e['kind']}] {e['case']} {e['rule']}: dự đoán {e['pred']} / nhãn {e['truth']} — {e['note']}")
    out = {"ruleset": ruleset_id, "llm": info["model"], "assessor_model": info["model"],
           "generator_model": doc.get("generator_model") or doc.get("generated_by"),
           "verifier_model": doc.get("verifier_model"), "label_scope": scope,
           "labels_generated_at": doc.get("generated_at"), "labels_approved_at": doc.get("approved_at"),
           "evaluated_at": datetime.now().isoformat(timespec="seconds"),
           "provisional": not approved, "labels_approved": approved, "approved_by": doc.get("approved_by"),
           "primary_metric": "target",
           "target": {"correct": T["ok"], "total": T["n"], "false_pass": T["fp"], "routed_to_officer": routed,
                      "note": "THƯỚC ĐO CHÍNH — vi phạm cài sẵn có bị bắt không"},
           "control": ({"correct": C["ok"], "total": C["n"], "false_alarm": C["false_alarm"],
                        "note": "rule mục tiêu chấm trên hồ sơ sạch — báo động giả"} if scope == "target" else None),
           "secondary": ({"correct": S["ok"], "total": S["n"], "false_pass": S["fp"],
                          "note": "chỉ tham khảo — rule phụ do đề thi tự sinh, dễ nhiễu"} if scope == "full" else None),
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
            print(f"\nSinh xong: {len(d['cases'])} case (bỏ {len(d['skipped'])}, {d['n_needs_review']} case bị gắn cờ cần rà)"
                  f" -> data/labels/generated-{rid}.json\nNHÃN CHƯA PHÊ CHUẨN — rà văn bản rồi --confirm/--dispute, sau đó --approve.")
        elif cmd == "--eval":
            evaluate(rid)
        elif cmd == "--approve":
            print(approve(rid, args[0] if args else "Cán bộ"))
        elif cmd == "--confirm":
            print(confirm_target(rid, args[0], args[1] if len(args) > 1 else "không nêu lý do"))
        elif cmd == "--confirm-control":
            print(confirm_control(rid, args[0], args[1], args[2] if len(args) > 2 else "không nêu lý do"))
        elif cmd == "--dispute":
            print(dispute(rid, args[0], args[1] if len(args) > 1 else "không nêu lý do"))
        elif cmd == "--flag-weak":
            print(flag_weak(rid, args[0], args[1], args[2] if len(args) > 2 else "không nêu lý do"))
        else:
            print(__doc__); sys.exit(1)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"LỖI: {e}"); sys.exit(2)
