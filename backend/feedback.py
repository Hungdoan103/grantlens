"""feedback.py — vòng phản hồi: quyết định của cán bộ trở thành dữ liệu huấn luyện.

Khi cán bộ KÝ DUYỆT một hồ sơ, mọi tiêu chí mà (a) cán bộ SỬA kết luận AI, hoặc (b) AI trả
CHƯA RÕ / KHÔNG ĐỀ CẬP và người phải quyết — được ghi vào data/feedback/feedback.jsonl:
    {case, rule, rule_quote, facts AI trích, ai_verdict, final_verdict, officer_reason, aq, ts, llm}

Dùng theo 2 tầng:
  1. NGAY LẬP TỨC (không cần huấn luyện): các ca đã sửa của CÙNG tiêu chí được bơm ngược vào
     prompt phán quyết làm ví dụ few-shot ("lần trước với dữ kiện X, kết luận đúng là Y vì Z").
     Bật/tắt: GRANTLENS_FEEDBACK=on|off (mặc định on).
  2. ĐỊNH KỲ: python -m backend.feedback export -> feedback-sft.jsonl (định dạng chat messages)
     để fine-tune lại bước trích dữ kiện / phán quyết (GĐ3, cần đủ ~vài trăm mẫu).
"""
import json, os, sys
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"
FILE = DIR / "feedback.jsonl"
ENABLED = os.environ.get("GRANTLENS_FEEDBACK", "on").lower() != "off"
MAX_FEWSHOT = 2


def _load():
    if not FILE.exists():
        return []
    out = []
    for line in FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record_signed_case(case: dict, verdicts: list, rules_by_id: dict, llm_model: str, ruleset_id: str = "") -> int:
    """Gọi lúc ký duyệt: ghi các tiêu chí cán bộ sửa / phải quyết thay AI. Trả số mẫu ghi."""
    DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(FILE, "a", encoding="utf-8") as f:
        for v in verdicts:
            override = v["final_verdict"] != v["ai_verdict"]
            human_decided = v["ai_verdict"] in ("unclear", "not_addressed")
            if not (override or human_decided):
                continue
            rule = rules_by_id.get(v["rule_id"], {})
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "case": case["id"], "rule": v["rule_id"], "rule_quote": rule.get("quote", ""),
                "facts": [x.get("fact") for x in (v.get("facts") or [])],
                "ai_verdict": v["ai_verdict"], "ai_confidence": v.get("ai_confidence"),
                "final_verdict": v["final_verdict"], "officer_reason": v.get("officer_reason") or "",
                "applicant_quote": v.get("aq") or "", "kind": "override" if override else "human_decided",
                "officer": v.get("confirmed_by"), "llm": llm_model, "ruleset": ruleset_id,
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def examples_for_rule(rule_id: str, ruleset_id: str = None, limit: int = MAX_FEWSHOT):
    """Ví dụ few-shot: các lần cán bộ SỬA kết luận AI của tiêu chí này TRONG CÙNG bộ tiêu chí."""
    if not ENABLED:
        return []
    ex = [e for e in _load() if e["rule"] == rule_id and e["kind"] == "override" and e.get("facts")
          and (ruleset_id is None or e.get("ruleset", "nsf-22-586") == ruleset_id)]
    return ex[-limit:]


def fewshot_block(rule_id: str, ruleset_id: str = None) -> str:
    ex = examples_for_rule(rule_id, ruleset_id)
    if not ex:
        return ""
    lines = ["\nPAST OFFICER CORRECTIONS for this rule (learn from them; the officer verdict is ground truth):"]
    for e in ex:
        facts = "; ".join(e["facts"][:3])
        lines.append(f'- Facts: "{facts}" -> AI said {e["ai_verdict"]}, but the CORRECT verdict was '
                     f'{e["final_verdict"]}. Officer reason: {e["officer_reason"][:160]}')
    return "\n".join(lines)


def stats():
    data = _load()
    by_rule, by_kind = {}, {"override": 0, "human_decided": 0}
    for e in data:
        by_rule[e["rule"]] = by_rule.get(e["rule"], 0) + 1
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    return {"total": len(data), "by_kind": by_kind, "by_rule": dict(sorted(by_rule.items())),
            "enabled": ENABLED, "file": str(FILE)}


def export_sft(out_path=None) -> str:
    """Xuất định dạng fine-tune (chat messages) cho bước phán quyết."""
    from .core import SYS_JUDGE
    out_path = Path(out_path or DIR / "feedback-sft.jsonl")
    data = _load()
    with open(out_path, "w", encoding="utf-8") as f:
        for e in data:
            if not e.get("facts"):
                continue
            fact_list = "\n".join(f"{i+1}. {x}" for i, x in enumerate(e["facts"]))
            f.write(json.dumps({"messages": [
                {"role": "system", "content": SYS_JUDGE},
                {"role": "user", "content": f'CASE {e["case"]} — RULE {e["rule"]}: "{e["rule_quote"]}"\n\nFACTS (normalized, style removed):\n{fact_list}\n\nDecide.'},
                {"role": "assistant", "content": json.dumps({
                    "verdict": e["final_verdict"], "confidence": "high", "supporting_fact": 1,
                    "note_vi": e["officer_reason"] or "Theo kết luận đã ký của cán bộ."}, ensure_ascii=False)},
            ]}, ensure_ascii=False) + "\n")
    return str(out_path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        p = export_sft()
        print(f"Đã xuất {stats()['total']} mẫu -> {p}")
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
