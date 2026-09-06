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
    hit = _career(rule["id"], text, meta) if ruleset_id == "nsf-22-586" else None
    hit = hit or _generic(rule, text)
    if not hit:
        return None
    g_verdict, reason = hit
    if verdict == "met":  # LLM cho qua trong khi code thấy vi phạm -> chặn false-pass
        if rule.get("type") == "quantitative":
            return {"action": "override", "verdict": g_verdict, "reason": reason}
        return {"action": "flag", "verdict": "unclear", "reason": reason}
    return {"action": "confirm", "verdict": verdict, "reason": reason}
