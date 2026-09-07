"""guards.py — lớp chặn FALSE-PASS bằng mã nguồn (yêu cầu khách: not_met→met nguy hiểm hơn false fail).

Sau khi LLM phán quyết, code kiểm tra lại các tiêu chí kiểm được bằng số liệu/mẫu chữ xác định:
  - Rule ĐỊNH LƯỢNG (ngân sách, số trang, %, đếm lần, co-PI): code phát hiện vi phạm mà LLM nói "met"
    -> GHI ĐÈ thành not_met (số liệu do code so, không tranh cãi), gắn nhãn nguồn "code-guard".
  - Rule ĐỊNH TÍNH (tenured, cost sharing dạng chữ): code nghi vi phạm mà LLM nói "met"
    -> HẠ xuống "unclear" + needs_attention — ép con người quyết, không bao giờ lặng lẽ cho qua.
Guard KHÔNG BAO GIỜ tự nâng lên "met". LLM đã nói not_met/unclear thì guard chỉ xác nhận thêm.

Guard đặc thù viết theo (ruleset_id, rule_id); ruleset khác dùng guard tổng quát
"minimum of $X" so với số tiền bóc bằng crosscheck.extract_values.
"""
import re
from .crosscheck import extract_values

BIO_ENG_OPP = re.compile(r"\b(BIO|ENG|OPP)\b|Directorate for (Biological|Engineering)|Office of Polar", re.I)


def _budget_values(text: str):
    return [v for cat, v, _, _ in extract_values("x", text) if cat == "budget_total"]


def _sentences_with(text: str, pat: str):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if re.search(pat, s, re.I)]


# ---------- guard đặc thù NSF 22-586 CAREER ----------
def _career(rule_id: str, text: str, meta: dict):
    """Trả (verdict_nghi_vấn, lý do) hoặc None."""
    if rule_id == "R04":
        m = re.search(r"(\d{1,3})%\s*(is |as )?(a )?tenure-track", text, re.I)
        if m and int(m.group(1)) < 50:
            return "not_met", f"Vị trí tenure-track chỉ {m.group(1)}% < 50% (code so số liệu)"
        if re.search(r"\b(I am|is|as)\s+(an?\s+)?Associate Professor", text):
            return "not_met", "Hồ sơ ghi chức danh Associate Professor — rule loại trừ rõ (code khớp mẫu chữ)"
    if rule_id == "R05":
        if re.search(r"received tenure in \d{4}|\bI am tenured\b|granted tenure", text, re.I):
            return "not_met", "Hồ sơ ghi đã nhận tenure (code khớp mẫu chữ)"
    if rule_id == "R07":
        if re.search(r"\b(fourth|fifth|sixth|4th|5th)\b[^.\n]{0,40}(submission|time)[^.\n]{0,40}(CAREER|competition)|(CAREER|competition)[^.\n]{0,60}\b(fourth|fifth|4th|5th)\b", text, re.I):
            return "not_met", "Hồ sơ ghi đây là lần dự thi thứ 4 trở lên — vượt giới hạn 3 lần (code khớp mẫu chữ)"
    if rule_id == "R08":
        hits = _sentences_with(text, r"co-?PI\b|co-?Principal Investigator")
        bad = [s for s in hits if not re.search(r"\bno co-?PIs?\b|not permit|sole Principal", s, re.I)]
        if bad:
            return "not_met", "Hồ sơ nêu có co-PI trên trang bìa — rule cấm co-PI (code khớp mẫu chữ)"
    if rule_id == "R09":
        m = re.search(r"letter[^.\n]{0,90}?\bis\s+(\d+)\s+pages", text, re.I)
        if m and int(m.group(1)) > 2:
            return "not_met", f"Thư trưởng khoa {m.group(1)} trang > giới hạn 2 trang (code so số liệu)"
    if rule_id == "R10":
        m = re.search(r"Project Description is\s+(\d+)\s+pages", text, re.I)
        if m and int(m.group(1)) > 15:
            return "not_met", f"Thuyết minh {m.group(1)} trang > giới hạn 15 trang (code so số liệu)"
    if rule_id == "R11":
        vals = _budget_values(text)
        if vals:
            thr = 500_000 if (BIO_ENG_OPP.search(meta.get("directorate") or "") or BIO_ENG_OPP.search(text)) else 400_000
            if all(v < thr for v in vals):
                return "not_met", f"Tổng ngân sách {max(vals):,}$ < mức tối thiểu {thr:,}$ (code so số liệu)"
    if rule_id == "R12":
        hits = _sentences_with(text, r"voluntary( committed)? cost shar")
        bad = [s for s in hits if not re.search(r"\bno\b|not include|do(es)? not|without", s, re.I)]
        if bad:
            return "not_met", "Hồ sơ nêu có voluntary cost sharing — rule cấm (code khớp mẫu chữ)"
    return None


# ---------- guard đặc thù các quỹ Úc (theo "bẫy AI" khách phân tích) ----------
def _au_wine(rule_id: str, text: str, meta: dict):
    if rule_id == "W03":  # ngưỡng doanh số rượu rebatable $1,207,000
        for s in _sentences_with(text, r"rebatable wine"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) < 1_207_000:
                return "not_met", f"Doanh số rượu rebatable {m.group(1)}$ < ngưỡng $1,207,000 (code so số liệu)"
    if rule_id == "W06":  # BẪY: chỉ bán buôn / không có cellar door vật lý
        if re.search(r"wholesale only|only .{0,25}wholesale|no (physical )?cellar door|does not (own|operate|have)[^.\n]{0,40}cellar door|ceased .{0,30}cellar door", text, re.I):
            return "not_met", "Hồ sơ/tài liệu nêu chỉ bán buôn hoặc không còn/không có cellar door vật lý — bẫy đặc thù quỹ (code khớp mẫu chữ)"
    if rule_id == "W08":  # <50% doanh số từ cellar door vật lý
        m = re.search(r"(\d{1,2})\s?(?:per cent|%)[^.\n]{0,80}(physical )?cellar door", text, re.I)
        if m and int(m.group(1)) < 50:
            return "not_met", f"Chỉ {m.group(1)}% doanh số từ cellar door vật lý < 50% (code so số liệu)"
    if rule_id == "W09":
        m = re.search(r"grant (?:amount )?(?:requested|of)[^.\n]{0,25}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m and int(m.group(1).replace(",", "")) > 100_000:
            return "not_met", f"Số tiền xin {m.group(1)}$ vượt trần $100,000 (code so số liệu)"
    return None


def _au_cyber(rule_id: str, text: str, meta: dict):
    if rule_id == "C01":  # BẪY: nộp đơn lẻ, không liên danh
        if re.search(r"sole applicant|apply(ing)? alone|no project partner|without (a |any )?partner|single (organisation|entity) appl", text, re.I):
            return "not_met", "Hồ sơ nêu nộp đơn lẻ / không có project partner — quỹ bắt buộc liên danh (code khớp mẫu chữ)"
    if rule_id == "C07":
        for s in _sentences_with(text, r"eligible expenditure"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) < 500_000:
                return "not_met", f"Chi tiêu hợp lệ {m.group(1)}$ < mức tối thiểu $500,000 (code so số liệu)"
    return None


def _au_bff(rule_id: str, text: str, meta: dict):
    if rule_id == "F01":  # BẪY: đúng 50% nữ sở hữu -> không phải majority
        m = re.search(r"(\d{1,2})(?:\.\d+)?\s?(?:per cent|%)[^.\n]{0,70}(women|female)|(?:women|female)[^.\n]{0,70}?(\d{1,2})(?:\.\d+)?\s?(?:per cent|%)", text, re.I)
        if m:
            pct = int(m.group(1) or m.group(3))
            if pct <= 50:
                return "not_met", f"Tỷ lệ nữ sở hữu/lãnh đạo {pct}% — không đạt 'majority' (>50%) (code so số liệu)"
    if rule_id == "F05":
        for s in _sentences_with(text, r"income tax exempt"):
            if not re.search(r"\bnot\b|\bno\b", s, re.I):
                return "not_met", "Hồ sơ nêu tổ chức thuộc diện income tax exempt — bị loại trừ (code khớp mẫu chữ)"
    if rule_id == "F06":
        m = re.search(r"grant (?:amount )?(?:requested|of)[^.\n]{0,25}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m:
            v = int(m.group(1).replace(",", ""))
            if v < 25_000 or v > 480_000:
                return "not_met", f"Số tiền xin {m.group(1)}$ ngoài khung $25,000–$480,000 (code so số liệu)"
    return None


def _au_onfarm(rule_id: str, text: str, meta: dict):
    if rule_id == "O01":  # BẪY: nông trại trồng trọt thuần túy, không chăn nuôi
        if re.search(r"cropping only|solely (grows|crops)|no livestock|does not (run|keep|hold)[^.\n]{0,30}(livestock|stock)|grain[- ]only", text, re.I):
            return "not_met", "Hồ sơ nêu trồng trọt thuần túy / không có vật nuôi — quỹ chỉ dành cho ngành chăn nuôi (code khớp mẫu chữ)"
    if rule_id == "O05":
        m = re.search(r"(\d{1,2})\s?(?:per cent|%)[^.\n]{0,70}(gross income|income from)", text, re.I)
        if m and int(m.group(1)) <= 50:
            return "not_met", f"Chỉ {m.group(1)}% thu nhập từ sản xuất nông nghiệp — không vượt 50% (code so số liệu)"
    if rule_id == "O06":
        for s in _sentences_with(text, r"off[- ]farm assets"):
            m = re.search(r"\$\s?(\d[\d,]*\d|\d)", s)
            if m and int(m.group(1).replace(",", "")) > 5_000_000:
                return "not_met", f"Tài sản ngoài nông trại {m.group(1)}$ vượt trần $5,000,000 (code so số liệu)"
    if rule_id == "O07":
        m = re.search(r"(?:rebate|claim(?:ed|ing)?|amount)[^.\n]{0,40}\$\s?(\d[\d,]*\d|\d)", text, re.I)
        if m and int(m.group(1).replace(",", "")) > 25_000:
            return "not_met", f"Số tiền xin {m.group(1)}$ vượt trần $25,000 (code so số liệu)"
    return None


_FUND_GUARDS = {"nsf-22-586": _career, "au-wine-tourism-r8": _au_wine,
                "au-cyber-skills-r2": _au_cyber, "au-female-founders-r1": _au_bff,
                "au-onfarm-water": _au_onfarm}


# ======================================================================
# GUARD COMPILER TỔNG QUÁT — tự biên dịch ràng buộc từ NGUYÊN VĂN rule.
# Trả lời phê bình của khách: quỹ mới chỉ cần nạp ruleset là có ngay lớp
# guard cơ bản (ngưỡng tiền, %, mục cấm, danh sách loại trừ) — không phụ
# thuộc việc có ai ngồi viết guard tay hay không. Guard tay (nếu có) là
# lớp tinh chỉnh CHỒNG LÊN, không phải điều kiện tiên quyết.
# ======================================================================
_STOP = set("the a an of in for and or to be is are with under have has must you your that this "
            "any all not no on at by from as it its their they per cent gst exclusive".split())


def _anchors(quote: str, pos: int, window: int = 60):
    """Cụm từ định danh quanh vị trí ràng buộc trong quote — dùng để chỉ so số
    trong những câu của hồ sơ nói về ĐÚNG chủ đề đó (tránh so nhầm số khác)."""
    seg = quote[max(0, pos - window):pos + window]
    words = [w.strip(".,;:()").lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", seg)]
    return [w for w in words if w not in _STOP][:6]


def compile_rule_guards(rule: dict):
    """Biên dịch quote -> danh sách ràng buộc máy kiểm được.
    Mỗi ràng buộc: {kind, op, value, anchors, label}."""
    q = rule.get("quote", "")
    out = []
    # --- ngưỡng TIỀN ---
    for m in re.finditer(r"(at least|a minimum of|minimum of|more than|no more than|not exceed|may not exceed|cannot exceed|up to|maximum(?: that can be claimed)? is|expected to total a minimum of)\s*\$\s?([\d,]+)", q, re.I):
        op = "min" if re.search(r"least|minimum|more than", m.group(1), re.I) else "max"
        out.append({"kind": "money", "op": op, "value": int(m.group(2).replace(",", "")),
                    "anchors": _anchors(q, m.start()), "label": m.group(0)[:60]})
    m = re.search(r"[Ff]rom \$\s?([\d,]+)(?:\.\d+)? to \$\s?([\d,]+)", q)
    if m:
        a = _anchors(q, m.start())
        out.append({"kind": "money", "op": "min", "value": int(m.group(1).replace(",", "")), "anchors": a, "label": "khung dưới"})
        out.append({"kind": "money", "op": "max", "value": int(m.group(2).replace(",", "")), "anchors": a, "label": "khung trên"})
    # --- ngưỡng PHẦN TRĂM ---
    for m in re.finditer(r"(at least|more than|majority[^.]{0,20}?|no more than|up to|minimum of)\s*(\d{1,3})\s*(?:per cent|%)", q, re.I):
        op = "min" if re.search(r"least|more than|majority|minimum", m.group(1), re.I) else "max"
        out.append({"kind": "percent", "op": op, "value": int(m.group(2)),
                    "anchors": _anchors(q, m.start()), "label": m.group(0)[:60]})
    if re.search(r"majority owned and led by women", q, re.I):
        out.append({"kind": "percent", "op": "min", "value": 51, "anchors": ["women", "female", "owned", "led"],
                    "label": "majority owned and led by women (>50%)"})
    # --- MỤC CẤM: "No X are permitted / is prohibited / must not include X" ---
    for m in re.finditer(r"\bNo ([\w\- ]{2,30}?) (?:is|are) (?:permitted|allowed)|inclusion of ([\w\- ]{3,40}?) is prohibited|must not (?:include|contain) ([\w\- ]{3,40})", q, re.I):
        term = next(t for t in m.groups() if t)
        out.append({"kind": "forbidden", "term": term.strip().rstrip("s"), "label": f"cấm: {term.strip()}"})
    # --- DANH SÁCH LOẠI TRỪ: "not eligible ... if you are: a; b; c" ---
    m = re.search(r"not eligible[^:]{0,40}:\s*(.+)", q, re.I | re.S)
    if m:
        items = [it.strip(" .;•·") for it in re.split(r";|•|\n|(?<=\))\s*(?=[a-z])", m.group(1)) if 4 < len(it.strip()) < 90]
        for it in items[:8]:
            core_term = re.sub(r"^(an?|the)\s+", "", it, flags=re.I)
            core_term = re.split(r"\(|,| unless | however | including ", core_term)[0].strip()
            if 4 < len(core_term) < 60:
                out.append({"kind": "excluded", "term": core_term, "label": f"loại trừ: {core_term}"})
    return out


def _eval_compiled(cons: list, text: str):
    """Chạy các ràng buộc đã biên dịch trên văn bản hồ sơ. Trả (verdict, reason) hoặc None."""
    sents = re.split(r"(?<=[.!?])\s+", text)
    for c in cons:
        if c["kind"] in ("money", "percent"):
            pat = r"\$\s?(\d[\d,]*\d|\d)" if c["kind"] == "money" else r"(\d{1,3})\s*(?:per cent|%)"
            matched_vals = []
            for s in sents:
                if not any(a in s.lower() for a in c.get("anchors", [])):
                    continue
                for m in re.finditer(pat, s):
                    matched_vals.append(int(m.group(1).replace(",", "")))
            if matched_vals:
                bad = ([v for v in matched_vals if v < c["value"]] if c["op"] == "min"
                       else [v for v in matched_vals if v > c["value"]])
                # min: chỉ kết luận khi MỌI giá trị liên quan đều dưới ngưỡng (tránh oan khi có nhiều số)
                if c["op"] == "min" and bad and len(bad) == len(matched_vals):
                    return "not_met", f"Giá trị {min(bad):,} dưới ngưỡng {c['value']:,} trong rule ('{c['label']}') — guard tự biên dịch từ nguyên văn"
                if c["op"] == "max" and bad:
                    return "not_met", f"Giá trị {max(bad):,} vượt trần {c['value']:,} trong rule ('{c['label']}') — guard tự biên dịch từ nguyên văn"
        elif c["kind"] in ("forbidden", "excluded"):
            term = c["term"]
            hits = [s for s in sents if re.search(re.escape(term), s, re.I)]
            bad = [s for s in hits if not re.search(r"\bno\b|\bnot\b|\bnone\b|without|sole |do(es)? not", s, re.I)]
            if bad:
                return ("not_met" if c["kind"] == "forbidden" else "unclear",
                        f"Hồ sơ nêu '{term}' — rule {('cấm' if c['kind']=='forbidden' else 'loại trừ')} mục này ('{c['label']}') — guard tự biên dịch từ nguyên văn")
    return None


# Các rule đã có guard TAY (lớp tinh chỉnh) — dùng cho báo cáo độ phủ
HAND_COVERAGE = {
    "nsf-22-586": {"R04", "R05", "R07", "R08", "R09", "R10", "R11", "R12"},
    "au-wine-tourism-r8": {"W03", "W06", "W08", "W09"},
    "au-cyber-skills-r2": {"C01", "C07"},
    "au-female-founders-r1": {"F01", "F05", "F06"},
    "au-onfarm-water": {"O01", "O05", "O06", "O07"},
}


def coverage(ruleset: dict) -> dict:
    """Độ phủ guard của một bộ tiêu chí: rule nào được code bảo vệ (auto/tay), rule nào CHỈ dựa LLM + người.
    Đây là câu trả lời trung thực cho câu hỏi 'false-pass=0 có đúng với quỹ mới không'."""
    hand = HAND_COVERAGE.get(ruleset["id"], set())
    rows = []
    for r in ruleset["rules"]:
        auto = compile_rule_guards(r)
        kind = ("hand+auto" if r["id"] in hand and auto else
                "hand" if r["id"] in hand else
                "auto" if auto else "llm-only")
        rows.append({"rule": r["id"], "title_vi": r["title_vi"], "guard": kind,
                     "auto_constraints": [c["label"] for c in auto]})
    n_guarded = sum(1 for x in rows if x["guard"] != "llm-only")
    return {"ruleset": ruleset["id"], "n_rules": len(rows), "n_guarded": n_guarded,
            "pct": round(n_guarded / max(len(rows), 1), 2), "rows": rows,
            "note": "Rule 'llm-only' KHÔNG có lưới đỡ code — false-pass ở đó phụ thuộc LLM + cán bộ. "
                    "Muốn nâng độ phủ: viết guard tay hoặc sửa quote cho chứa ngưỡng/mục cấm tường minh."}


# ---------- guard tổng quát cho ruleset bất kỳ ----------
def _generic(rule: dict, text: str):
    m = re.search(r"minimum of \$([\d,]+)", rule.get("quote", ""))
    if m:
        thr = int(m.group(1).replace(",", ""))
        vals = _budget_values(text)
        if vals and all(v < thr for v in vals):
            return "not_met", f"Tổng ngân sách {max(vals):,}$ < mức tối thiểu {thr:,}$ ghi trong rule (code so số liệu)"
    m = re.search(r"(?:may not|must not|no more than|not) exceed (\d+) pages", rule.get("quote", ""), re.I)
    if m:
        thr = int(m.group(1))
        pm = re.search(r"Project Description is\s+(\d+)\s+pages", text, re.I)
        if pm and int(pm.group(1)) > thr:
            return "not_met", f"{pm.group(1)} trang > giới hạn {thr} trang trong rule (code so số liệu)"
    return None


def check(ruleset_id: str, rule: dict, verdict: str, text: str, meta: dict = None):
    """Trả None (không có gì) hoặc dict {action, verdict, reason} để lớp trên áp vào kết luận."""
    meta = meta or {}
    fund_guard = _FUND_GUARDS.get(ruleset_id)
    hit = fund_guard(rule["id"], text, meta) if fund_guard else None
    hit = hit or _generic(rule, text)
    hand_covered = rule["id"] in HAND_COVERAGE.get(ruleset_id, set())
    if not hit and not hand_covered:
        # Lớp nền cho MỌI ruleset: ràng buộc TỰ BIÊN DỊCH từ nguyên văn rule.
        # Rule đã có guard tay thì KHÔNG chạy compiler đè lên — guard tay hiểu ngữ cảnh
        # (vd ngưỡng theo directorate) mà compiler tổng quát không hiểu.
        hit = _eval_compiled(compile_rule_guards(rule), text)
    if not hit:
        return None
    g_verdict, reason = hit
    if verdict == "met":  # LLM cho qua trong khi code thấy vi phạm -> chặn false-pass
        if rule.get("type") == "quantitative":
            return {"action": "override", "verdict": g_verdict, "reason": reason}
        return {"action": "flag", "verdict": "unclear", "reason": reason}
    return {"action": "confirm", "verdict": verdict, "reason": reason}
