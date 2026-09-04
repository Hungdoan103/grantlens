"""crosscheck.py — đối chiếu chéo chống gian lận (anti-cheating) giữa các tài liệu trong MỘT hồ sơ.

Ca khách nêu: form khai doanh thu 500k nhưng PDF báo cáo tài chính ghi 1,5 triệu; làm giả ngày tháng.
Cách làm: hồ sơ có thể gồm nhiều tài liệu, ngăn bằng dòng đánh dấu
    === TÀI LIỆU: <tên tài liệu> ===
(phần trước dấu đầu tiên là "Đơn khai chính"; upload đính kèm qua API tự chèn dấu này).

Engine là MÃ NGUỒN thuần (regex + chuẩn hoá số) — không phải LLM — nên kết quả tái lập được
và giải trình được từng con số. Các đại lượng được gắn nhãn theo ngữ cảnh rồi so theo nhãn:
  budget_total   tổng ngân sách/chi phí (cả trong bảng [BẢNG n] — dòng grand total)
  revenue        doanh thu
  phd_year       năm nhận bằng tiến sĩ
  pd_pages       số trang thuyết minh (Project Description)
  letter_pages   số trang thư trưởng khoa
Cùng nhãn mà giá trị khác nhau giữa các tài liệu (hoặc giữa lời khai và bảng số liệu
trong chính tài liệu đó) -> phát hiện KHÔNG KHỚP, kèm nguồn + trích ngữ cảnh.
Ngoài ra kiểm tra ngày vô lý: ngày kết thúc trước ngày bắt đầu, năm ở tương lai.

Như mọi lớp khác: đây là BẰNG CHỨNG cho cán bộ — hệ thống không tự kết luận gian lận.
"""
import re
from datetime import date

DOC_SPLIT = re.compile(r"^===\s*TÀI LIỆU:\s*(.+?)\s*===\s*$", re.M)
MAIN_DOC = "Đơn khai chính"

CATEGORY_VI = {
    "budget_total": "Tổng ngân sách",
    "revenue": "Doanh thu",
    "phd_year": "Năm nhận bằng tiến sĩ",
    "pd_pages": "Số trang thuyết minh",
    "letter_pages": "Số trang thư trưởng khoa",
}


def split_documents(text: str):
    """[(tên tài liệu, nội dung)] — phần trước marker đầu tiên là Đơn khai chính."""
    parts = DOC_SPLIT.split(text)
    docs = []
    if parts[0].strip():
        docs.append((MAIN_DOC, parts[0]))
    for i in range(1, len(parts), 2):
        docs.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    return docs


def _money(s: str) -> int:
    return int(re.sub(r"[,.](?=\d{3}\b)", "", s).split(".")[0].replace(",", ""))


def _ctx(text: str, start: int, end: int, w: int = 55) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - w):min(len(text), end + w)]).strip()


def extract_values(doc_name: str, text: str):
    """[(category, value, source, context)] — source phân biệt văn xuôi vs bảng."""
    out = []
    # --- tách phần bảng và phần văn xuôi ---
    tables = [(m.group(1), m.group(0)) for m in re.finditer(r"\[BẢNG (\d+)\](.*?)\[/BẢNG \1\]", text, re.S)]
    prose = re.sub(r"\[BẢNG (\d+)\].*?\[/BẢNG \1\]", " ", text, flags=re.S)

    # --- số tiền có nhãn ngữ cảnh (văn xuôi) ---
    for m in re.finditer(r"\$\s?([\d][\d,]*(?:\.\d+)?)", prose):
        before = prose[max(0, m.start() - 90):m.start()].lower()
        val = _money(m.group(1))
        if val < 1000:
            continue
        if re.search(r"(total|grand|tổng)[^.]*?(budget|cost|amount|request|ngân sách)|budget[^.]*?total", before) \
                or re.search(r"(total (requested )?budget|tổng ngân sách)", before):
            out.append(("budget_total", val, doc_name, _ctx(prose, m.start(), m.end())))
        elif re.search(r"revenue|doanh thu|turnover", before):
            out.append(("revenue", val, doc_name, _ctx(prose, m.start(), m.end())))
    # --- số tiền trong bảng: dòng grand total / total ---
    for tid, ttext in tables:
        for line in ttext.splitlines():
            if re.search(r"grand total|(^|\|)\s*total\b|tổng cộng", line, re.I):
                amts = re.findall(r"\$\s?([\d][\d,]*)", line)
                if amts:
                    out.append(("budget_total", _money(amts[-1]), f"{doc_name} · BẢNG {tid}",
                                re.sub(r"\s+", " ", line).strip()[:120]))
    # --- năm PhD ---
    for m in re.finditer(r"Ph\.?\s?D\.?[^.\n]{0,90}?\b(19\d{2}|20\d{2})\b", text, re.I):
        out.append(("phd_year", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    # --- số trang ---
    for m in re.finditer(r"Project Description[^.\n]{0,50}?\b(\d{1,2})\s?pages", text, re.I):
        out.append(("pd_pages", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    for m in re.finditer(r"[Ll]etter[^.\n]{0,60}?\b(\d{1,2})\s?pages?", text):
        out.append(("letter_pages", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    return out


def _date_sanity(doc_name: str, text: str):
    finds = []
    year_now = date.today().year
    for m in re.finditer(r"\b(\d{2})/(\d{2})/(\d{4})\b", text):
        y = int(m.group(3))
        if y > year_now + 1:
            finds.append({"category": "date_future", "label_vi": "Ngày ở tương lai",
                          "severity": "medium", "values": [{"value": m.group(0), "source": doc_name,
                                                            "context": _ctx(text, m.start(), m.end())}]})
    m = re.search(r"start(?:ed|ing)? (?:date|in)?[^.\n]{0,20}?(\d{4})[^.\n]{0,80}?end(?:ed|ing)? (?:date|in)?[^.\n]{0,20}?(\d{4})", text, re.I)
    if m and int(m.group(2)) < int(m.group(1)):
        finds.append({"category": "date_order", "label_vi": "Ngày kết thúc trước ngày bắt đầu",
                      "severity": "high", "values": [{"value": f"{m.group(1)} → {m.group(2)}", "source": doc_name,
                                                     "context": _ctx(text, m.start(), m.end())}]})
    return finds


def run(text: str) -> dict:
    """Đối chiếu chéo toàn hồ sơ. Trả {docs, findings, checked, risk}."""
    docs = split_documents(text)
    values, findings = [], []
    for name, body in docs:
        values.extend(extract_values(name, body))
        findings.extend(_date_sanity(name, body))
    by_cat = {}
    for cat, val, src, ctx in values:
        by_cat.setdefault(cat, []).append({"value": val, "source": src, "context": ctx})
    for cat, vals in by_cat.items():
        distinct = sorted({v["value"] for v in vals})
        if len(distinct) > 1:
            spread = (max(distinct) - min(distinct)) / max(distinct)
            findings.append({
                "category": cat, "label_vi": CATEGORY_VI.get(cat, cat),
                "severity": "high" if cat in ("budget_total", "revenue") or spread > 0.2 else "medium",
                "values": vals, "distinct": distinct,
            })
    return {
        "n_docs": len(docs), "docs": [d[0] for d in docs],
        "checked": {cat: len(vs) for cat, vs in by_cat.items()},
        "findings": findings,
        "risk": ("mismatch_high" if any(f["severity"] == "high" for f in findings)
                 else "mismatch" if findings else "consistent"),
    }
