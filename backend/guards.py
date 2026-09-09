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


def normalise(text: str) -> str:
    """Chuẩn hoá trước khi cho guard chạy — regex tay hay trượt vì wording/định dạng:
    bỏ markdown (**bold**, *nghiêng*, `code`), gộp khoảng trắng/xuống dòng, chuẩn hoá gạch nối và
    dấu nháy cong. Sửa đúng ca W06 lọt trong eval Wine ('does **not** own' không khớp 'does not own')."""
    t = re.sub(r"[*_`]{1,3}", "", text or "")
    t = t.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"[ \t]+", " ", t)
    # Tiền tệ Úc viết nhiều kiểu — quy hết về "$1,250,000" để MỌI guard (tay lẫn compiler) đọc được,
    # thay vì phải sửa từng regex: "A$1,250,000" / "AUD 1,250,000" / "1,250,000 AUD" / "... dollars".
    t = re.sub(r"\bA\$\s*(?=\d)", "$", t)
    t = re.sub(r"\bAUD\s*\$?\s*(?=\d)", "$", t, flags=re.I)
    t = re.sub(r"(?<![$\d.])(\d[\d,]*\d|\d)\s*(?:AUD|australian dollars|dollars)\b", r"$\1", t, flags=re.I)
    return t


def _sentences_with(text: str, pat: str):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if re.search(pat, s, re.I)]


def _alt_satisfied(text: str, alt_pat: str, need_pat: str) -> bool:
    """Có câu nào cho thấy NHÁNH THAY THẾ đã thỏa mãn không (vd related entity sở hữu cellar door)?
    Nếu có, guard KHÔNG được kết luận vi phạm từ câu phủ định về chủ thể chính."""
    for s in _sentences_with(text, alt_pat):
        if re.search(need_pat, s, re.I) and not re.search(r"\b(no|not|none|without)\b", s, re.I):
            return True
    return False


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
    if rule_id == "W04":
        # LOGIC 2 LỚP (backlog needs-manual-guard đã đóng bằng guard tay):
        # cellar door sales phải CÒN PHẦN VƯỢT sau khi đã dùng để đạt ngưỡng $1,207,000.
        # => phần doanh số KHÔNG phải cellar door (tổng rebatable - cellar door) phải tự nó đạt ngưỡng;
        #    nếu không, mọi doanh số cellar door đã bị dùng hết để chạm ngưỡng -> không còn "in excess".
        if re.search(r"(all|entire|whole)[^.\n]{0,40}cellar door sales[^.\n]{0,60}(used|applied|counted)[^.\n]{0,40}(threshold|meet)|"
                     r"cellar door sales[^.\n]{0,50}(were|was|are|is)[^.\n]{0,30}(entirely|fully|wholly)[^.\n]{0,30}used", text, re.I):
            return "not_met", "Hồ sơ nêu toàn bộ doanh số cellar door đã dùng để đạt ngưỡng — không còn phần vượt (code khớp mẫu chữ)"
        THR = 1_207_000
        total = next((int(m.group(1).replace(",", "")) for s in _sentences_with(text, r"rebatable wine")
                      for m in [re.search(r"\$\s?(\d[\d,]*\d)", s)] if m), None)
        cellar = next((int(m.group(1).replace(",", "")) for s in _sentences_with(text, r"cellar door sales")
                       for m in [re.search(r"\$\s?(\d[\d,]*\d)", s)] if m), None)
        # Phần cellar door CÒN DƯ sau khi đã dùng để chạm ngưỡng = min(cellar, total − ngưỡng).
        # Chỉ kết luận khi ngưỡng đã đạt (total ≥ THR) — nếu chưa đạt thì đó là vi phạm W03,
        # không bắn chồng sang W04 để khỏi nhiễu.
        if total is not None and cellar is not None and total >= THR:
            excess = min(cellar, total - THR)
            if excess <= 0:
                return "not_met", (f"Không còn doanh số cellar door vượt ngưỡng: tổng {total:,}$ − ngưỡng {THR:,}$ "
                                   f"= {total - THR:,}$, cellar door {cellar:,}$ → phần vượt {max(excess, 0):,}$ "
                                   "(code tính 2 lớp theo đúng câu chữ 'in excess of')")
    if rule_id == "W06":  # BẪY: chỉ bán buôn / không có cellar door vật lý
        # Rule cho phép THAY THẾ: "and/or their related entity/ies have owned or leased..."
        # -> nếu hồ sơ nêu related entity có cellar door thì KHÔNG được kết luận vi phạm.
        if _alt_satisfied(text, r"related entit|associated entit|subsidiar|parent (company|entity)",
                          r"(own|lease|operat)\w*[^.]{0,60}cellar door"):
            return None
        if re.search(r"wholesale only|only .{0,25}wholesale|no (physical )?cellar door|"
                     r"does not (own|operate|lease|have)[^.\n]{0,60}cellar door|ceased .{0,40}cellar door|"
                     r"(sold|closed|disposed of)[^.\n]{0,40}(cellar door|tasting room)", text, re.I):
            return "not_met", "Hồ sơ/tài liệu nêu chỉ bán buôn hoặc không còn/không có cellar door vật lý (và không có nhánh related entity thay thế) — bẫy đặc thù quỹ (code khớp mẫu chữ)"
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
    if rule_id == "C06":
        # "board supports ... (or CEO or equivalent if there is no board)" — nhánh thay thế:
        # chỉ kết luận vi phạm khi hồ sơ nói rõ KHÔNG có xác nhận từ CẢ board LẪN CEO/tương đương.
        if _alt_satisfied(text, r"chief executive|CEO|equivalent|managing director|board",
                          r"(certif|support|approv|endorse)\w*"):
            return None
        if re.search(r"(board|chief executive|CEO)[^.\n]{0,60}(has not|have not|not yet|did not|declin\w+)[^.\n]{0,40}"
                     r"(approv|support|certif|endors)|no (board|CEO|executive) (approval|certification|support|endorsement)|"
                     r"without (board|CEO|executive) (approval|support|certification)", text, re.I):
            return "not_met", "Hồ sơ nêu không có xác nhận của board/CEO hỗ trợ dự án — rule bắt buộc (code khớp mẫu chữ)"
        if re.search(r"cannot (meet|cover|fund)[^.\n]{0,50}(costs|expenditure) not covered|"
                     r"unable to (meet|cover)[^.\n]{0,40}remaining (costs|cost)", text, re.I):
            return "not_met", "Hồ sơ nêu không cam kết được phần chi phí ngoài tài trợ — rule bắt buộc (code khớp mẫu chữ)"
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


# Dấu hiệu rule có cấu trúc mà REGEX KHÔNG diễn đạt nổi -> phải thú nhận, không được im lặng.
# (phê bình #1 của khách: W04 "in excess of any such sales used to meet the threshold" là logic 2 lớp)
_DERIVED_PAT = re.compile(
    r"in excess of any such|in excess of (?:the|those|any)|relative to|proportion of the|"
    r"used to meet the|after (?:deducting|excluding)|net of|remainder of|balance of|"
    r"as a (?:share|percentage) of|calculated by reference to|equivalent to the (?:sum|total)", re.I)
# Nhánh THAY THẾ: thỏa một trong nhiều chủ thể/cách -> không được kết luận vi phạm từ một câu
# (phê bình #1: W06 cho phép applicant HOẶC related entity)
_ALT_PAT = re.compile(r"and/or|(?<!\w)or their\b|or (?:its|his|her|the) related|"
                      r"related entit|or equivalent|or (?:an?\s+)?alternative|either .{2,40} or ", re.I)


def compile_rule_guards(rule: dict):
    """Biên dịch quote -> ràng buộc máy kiểm được + GHI NHẬN chỗ không biên dịch được.
    Mỗi ràng buộc: {kind, op, value, anchors, label, [conditional], [alt]}.
    kind='uncompilable' = rule có dấu hiệu định lượng/logic nhưng regex không diễn đạt nổi."""
    q = rule.get("quote", "")
    out = []
    has_alt = bool(_ALT_PAT.search(q))
    # --- ngưỡng TIỀN ---
    money = []
    for m in re.finditer(r"(at least|a minimum of|minimum of|more than|no more than|not exceed|may not exceed|cannot exceed|up to|maximum(?: that can be claimed)? is|expected to total a minimum of)\s*\$\s?([\d,]+)", q, re.I):
        op = "min" if re.search(r"least|minimum|more than", m.group(1), re.I) else "max"
        money.append({"kind": "money", "op": op, "value": int(m.group(2).replace(",", "")),
                      "anchors": _anchors(q, m.start()), "label": m.group(0)[:60]})
    # NGƯỠNG CÓ ĐIỀU KIỆN: nhiều ngưỡng cùng chiều trong một quote (vd $400k chung, $500k cho BIO/ENG/OPP).
    # Compiler KHÔNG biết hồ sơ thuộc nhánh nào -> chỉ giữ ngưỡng AN TOÀN NHẤT (min của các "min",
    # max của các "max") để không bao giờ báo oan; đồng thời gắn cờ conditional -> cần guard tay.
    for op in ("min", "max"):
        same = [c for c in money if c["op"] == op]
        if len(same) > 1:
            keep = min(same, key=lambda c: c["value"]) if op == "min" else max(same, key=lambda c: c["value"])
            keep = dict(keep, conditional=True,
                        label=keep["label"] + f" (rule có {len(same)} ngưỡng theo điều kiện — dùng ngưỡng an toàn nhất)")
            money = [c for c in money if c["op"] != op] + [keep]
            vals = ", ".join("${:,}".format(c["value"]) for c in same)
            out.append({"kind": "uncompilable", "label": "ngưỡng điều kiện",
                        "reason": f"rule có {len(same)} ngưỡng '{op}' theo điều kiện ({vals}) — compiler chỉ dùng "
                                  "ngưỡng an toàn nhất để không báo oan; cần guard tay để phân nhánh chính xác"})
    out.extend(money)
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
    # --- THÚ NHẬN: logic dẫn xuất/2 lớp mà regex không diễn đạt nổi ---
    if _DERIVED_PAT.search(q):
        out.append({"kind": "uncompilable", "label": "logic dẫn xuất",
                    "reason": "rule so sánh giá trị DẪN XUẤT (phần vượt/phần đã dùng/tỷ lệ của tổng) — "
                              "regex không diễn đạt được; cần guard tay hoặc để LLM + cán bộ quyết"})
    # --- Gắn cờ nhánh thay thế cho mọi ràng buộc dạng chữ ---
    if has_alt:
        for c in out:
            if c["kind"] in ("forbidden", "excluded"):
                c["alt"] = True
        out.append({"kind": "uncompilable", "label": "nhánh thay thế (OR)",
                    "reason": "rule cho phép thỏa qua chủ thể/cách THAY THẾ (and/or, related entity, or equivalent) — "
                              "một câu phủ định trong hồ sơ KHÔNG đủ kết luận vi phạm"})
    return out


def _eval_compiled(cons: list, text: str):
    """Chạy các ràng buộc đã biên dịch trên văn bản hồ sơ. Trả (verdict, reason) hoặc None.
    Ràng buộc kind='uncompilable' KHÔNG đánh giá (chỉ dùng cho báo cáo độ phủ);
    ràng buộc có alt=True (rule cho phép nhánh thay thế) chỉ được hạ 'unclear', không kết luận not_met."""
    text = normalise(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    for c in cons:
        if c["kind"] == "uncompilable":
            continue
        if c["kind"] in ("money", "percent"):
            pat = r"\$\s?(\d[\d,]*\d|\d)" if c["kind"] == "money" else r"(\d{1,3})\s*(?:per cent|%)"
            # Ràng buộc 'max' báo vi phạm ngay khi thấy MỘT giá trị vượt -> rất dễ báo oan nếu câu chỉ
            # tình cờ chứa một từ khóa (vd trần grant $100k bị so với doanh thu $1,5tr trong câu có chữ
            # "cellar door sales"). Vì vậy 'max' đòi câu phải khớp ÍT NHẤT 2 từ khóa của rule;
            # 'min' chỉ kết luận khi MỌI giá trị đều dưới ngưỡng nên 1 từ khóa là đủ an toàn.
            need_anchors = 2 if c["op"] == "max" else 1
            matched_vals = []
            for s in sents:
                low = s.lower()
                if sum(1 for a in set(c.get("anchors", [])) if a in low) < need_anchors:
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
                strong = c["kind"] == "forbidden" and not c.get("alt")
                return ("not_met" if strong else "unclear",
                        f"Hồ sơ nêu '{term}' — rule {('cấm' if c['kind']=='forbidden' else 'loại trừ')} mục này ('{c['label']}')"
                        + (" — rule có nhánh thay thế nên chỉ gắn CHƯA RÕ để cán bộ quyết" if c.get("alt") else "")
                        + " — guard tự biên dịch từ nguyên văn")
    return None


# Các rule đã có guard TAY (lớp tinh chỉnh) — dùng cho báo cáo độ phủ
HAND_COVERAGE = {
    "nsf-22-586": {"R04", "R05", "R07", "R08", "R09", "R10", "R11", "R12"},
    "au-wine-tourism-r8": {"W03", "W04", "W06", "W08", "W09"},
    "au-cyber-skills-r2": {"C01", "C06", "C07"},
    "au-female-founders-r1": {"F01", "F05", "F06"},
    "au-onfarm-water": {"O01", "O05", "O06", "O07"},
}


def rule_guard_level(ruleset_id: str, rule: dict) -> str:
    """Mức bảo vệ của MỘT tiêu chí — dùng để ép ma sát ở tầng nghiệp vụ, không chỉ hiển thị:
      code-guarded       : có guard tay và/hoặc ràng buộc chạy được -> có lưới đỡ.
      needs-manual-guard : có logic định lượng mà code KHÔNG diễn đạt nổi -> AI một mình là rủi ro.
      llm-only           : tiêu chí thuần định tính.
    """
    cons = compile_rule_guards(rule)
    runnable = [c for c in cons if c["kind"] != "uncompilable"]
    if rule["id"] in HAND_COVERAGE.get(ruleset_id, set()) or runnable:
        return "code-guarded"
    return "needs-manual-guard" if cons else "llm-only"


def coverage(ruleset: dict) -> dict:
    """Độ phủ guard TRUNG THỰC — 3 mức, để không ai hiểu nhầm 'có compiler = an toàn mọi quỹ':
      code-guarded       : có guard tay và/hoặc ràng buộc tự biên dịch chạy được.
      needs-manual-guard : rule CÓ cấu trúc định lượng/logic nhưng compiler KHÔNG diễn đạt nổi
                           (logic dẫn xuất kiểu W04, nhánh thay thế kiểu W06, ngưỡng theo điều kiện)
                           và chưa ai viết guard tay -> ĐÂY LÀ CHỖ RỦI RO NHẤT, phải ưu tiên xử lý.
      llm-only           : rule thuần định tính, không có gì để code kiểm -> dựa LLM + cán bộ.
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
        rows.append({"rule": r["id"], "title_vi": r["title_vi"], "guard": kind,
                     "hand": has_hand, "auto_constraints": [c["label"] for c in runnable],
                     "gaps": [{"label": c["label"], "reason": c["reason"]} for c in gaps]})
    n_guarded = sum(1 for x in rows if x["guard"] == "code-guarded")
    n_manual = sum(1 for x in rows if x["guard"] == "needs-manual-guard")
    n_llm = sum(1 for x in rows if x["guard"] == "llm-only")
    # rule vừa có guard tay vừa còn khoảng trống compiler -> vẫn nên rà lại
    partial = [x["rule"] for x in rows if x["guard"] == "code-guarded" and x["gaps"]]
    return {"ruleset": ruleset["id"], "n_rules": len(rows), "n_guarded": n_guarded,
            "n_needs_manual": n_manual, "n_llm_only": n_llm, "partial_rules": partial,
            "pct": round(n_guarded / max(len(rows), 1), 2), "rows": rows,
            "note": "'needs-manual-guard' = rule có logic định lượng mà compiler KHÔNG diễn đạt nổi và chưa có "
                    "guard tay — đây là chỗ false-pass dễ lọt nhất, ưu tiên viết guard tay. 'llm-only' = rule "
                    "định tính, dựa LLM + cán bộ. Số liệu false-pass chỉ có giá trị với bộ đã đo bằng bộ test "
                    "đã được cán bộ phê chuẩn nhãn."}


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
    """Trả None hoặc {action, verdict, reason, source}.

    HỢP NHẤT hai lớp thay vì loại trừ nhau (sửa phê bình: "có guard tay thì tắt compiler"):
      - Guard TAY và COMPILER luôn chạy CẢ HAI. Guard tay trượt wording thì compiler vẫn đỡ.
      - Cả hai cùng bắt        -> not_met (bằng chứng kép, mạnh nhất).
      - Chỉ guard tay bắt      -> theo guard tay (hiểu ngữ cảnh đặc thù).
      - Chỉ compiler bắt & rule ĐÃ có guard tay -> hạ CHƯA RÕ, không ghi đè: compiler có thể
        không hiểu ngoại lệ mà guard tay biết, nhưng cũng KHÔNG được im lặng bỏ qua.
      - Chỉ compiler bắt & rule chưa có guard tay -> theo loại rule (định lượng: ghi đè; định tính: chưa rõ).
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
                                        f"{hand[1]} | Lớp tự biên dịch xác nhận: {comp[1]}")
    elif hand:
        source, hit = "hand", hand
    elif comp:
        source, hit = "compiler", comp
    else:
        return None
    g_verdict, reason = hit

    if verdict == "met":  # LLM cho qua trong khi code thấy vi phạm -> chặn false-pass
        # compiler đơn độc trên rule đã có guard tay: cảnh báo chứ không ghi đè
        if source == "compiler" and hand_covered:
            return {"action": "flag", "verdict": "unclear", "source": source,
                    "reason": reason + " — guard tay không bắt ca này; lớp tự biên dịch nghi vấn, cần cán bộ quyết"}
        if rule.get("type") == "quantitative" and g_verdict == "not_met":
            return {"action": "override", "verdict": "not_met", "source": source, "reason": reason}
        return {"action": "flag", "verdict": "unclear", "source": source, "reason": reason}
    return {"action": "confirm", "verdict": verdict, "source": source, "reason": reason}
