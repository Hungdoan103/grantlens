"""coi.py — kiểm tra xung đột lợi ích (conflict of interest) trước khi cán bộ nhận hồ sơ.

Rủi ro: người nộp hồ sơ có thể chung tổ chức với chính cán bộ thẩm định.
Sổ cán bộ: data/officers.json (tên + danh sách tổ chức liên quan). Quy tắc, kiểm tra Ở SERVER:
  BLOCK : tổ chức của cán bộ trùng tổ chức nộp hồ sơ (khớp ≥ 0.85) — không được thẩm định,
          phải chuyển hồ sơ; không có đường "xác nhận bỏ qua".
  WARN  : tên cán bộ trùng/na ná người nộp, tên cán bộ xuất hiện trong văn bản hồ sơ,
          hoặc tổ chức khớp mức nghi ngờ (0.70–0.85) — được tiếp tục nhưng phải ghi lý do,
          tất cả vào nhật ký.
Cán bộ KHÔNG có trong sổ -> không kiểm tra được lịch sử tổ chức: trả cảnh báo "chưa khai báo".
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
    """Hạn xử lý (ngày) theo trạng thái — UI cảnh báo quá hạn."""
    return _load_file().get("sla_days", {})


def reload():
    _load_file.cache_clear()


def role_of(name: str):
    """Vai trò trong sổ cán bộ: 'officer' | 'manager' | None (chưa khai báo)."""
    q = norm(name)
    rec = next((o for o in load_officers() if norm(o["name"]) == q), None)
    return (rec or {}).get("role")


def check(officer_name: str, case: dict) -> dict:
    """case cần: applicant, org, text. Trả {level: 'none'|'warn'|'block', reasons: [...], registered: bool}."""
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
                reasons.append(f"Cán bộ thuộc/từng thuộc '{aff}' — trùng tổ chức nộp hồ sơ '{case_org}' (khớp {s:.0%}). Phải chuyển hồ sơ cho cán bộ khác.")
            elif s >= ORG_WARN:
                bump("warn")
                reasons.append(f"Tổ chức liên quan của cán bộ '{aff}' khớp mức nghi ngờ với '{case_org}' ({s:.0%}).")
            if norm(aff) and norm(aff) in text_norm and score(norm(aff), norm(case_org)) < ORG_WARN:
                bump("warn")
                reasons.append(f"Tổ chức liên quan của cán bộ '{aff}' xuất hiện trong văn bản hồ sơ.")
    else:
        bump("warn")
        reasons.append("Cán bộ chưa khai báo trong sổ cán bộ (data/officers.json) — không kiểm tra được lịch sử tổ chức; ghi lý do để tiếp tục.")

    s = score(qname, norm(applicant)) if applicant else 0
    if s >= NAME_WARN:
        bump("warn")
        reasons.append(f"Tên cán bộ trùng/na ná người nộp hồ sơ '{applicant}' ({s:.0%}).")
    if qname and qname in text_norm:
        bump("warn")
        reasons.append("Tên cán bộ xuất hiện nguyên văn trong hồ sơ (có thể là người viết thư giới thiệu / cộng sự).")

    return {"level": level, "reasons": reasons, "registered": bool(rec)}
