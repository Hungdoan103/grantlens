"""casegen.py — AI SINH BỘ TEST CÓ NHÃN cho từng ruleset (trả lời phê bình "eval hẹp" của khách).

Cách sinh giữ được độ tin của nhãn:
  - LLM CHỈ viết văn bản hồ sơ; NHÃN do code gán "by construction":
    mỗi case nhắm đúng MỘT rule vi phạm (not_met), mọi rule khác phải được viết rõ là thỏa (met);
    case đầu tiên là hồ sơ sạch (met toàn bộ).
  - Mỗi case sinh xong được KIỂM CHÉO bằng guard compiler: rule nhắm vi phạm mà guard bắt được
    -> "guard_agree"; guard bắn nhầm vào rule khác -> case bị loại, sinh lại.
  - Nhãn lưu với approved=false — CÁN BỘ rà và phê chuẩn trước khi coi là chuẩn vàng.

CLI:
  python -m backend.casegen <ruleset_id> --gen [N]   # sinh: 1 case sạch + N case vi phạm (mặc định: mọi rule có guard)
  python -m backend.casegen <ruleset_id> --eval      # chạy eval trên bộ đã sinh: accuracy + FALSE-PASS
"""
import json, re, sys
from pathlib import Path
from . import core, llm, guards

GEN_DIR = core.DATA / "applications" / "generated"
LBL_DIR = core.DATA / "labels"

SYS_GEN = """You write REALISTIC grant application summaries for testing an eligibility-screening system.
You are given a rule set (verbatim eligibility rules) and ONE target rule to violate.
Write an application summary (220-320 words, English, structured like a cover/eligibility summary with short labeled paragraphs: Applicant, entity/registration details, then one short paragraph per topic).
Hard requirements:
- The application must CLEARLY VIOLATE the target rule with an explicit, quotable fact (state the offending number/entity/fact plainly; do not hint, state it).
- Every OTHER rule in the set must be CLEARLY SATISFIED with an explicit, quotable fact (numbers above thresholds, required items present, excluded statuses explicitly negated).
- Realistic Australian/US organisation and person names; do not reuse names across runs; no markdown, plain text only.
- Do not mention the words "violate", "rule", "eligible" meta-commentary — write as a genuine applicant would.
If the target rule is "NONE", write a fully compliant application instead (every rule clearly satisfied)."""


def _gen_text(ruleset: dict, target_rule: dict = None) -> str:
    rules_txt = "\n".join(f"- {r['id']}: \"{r['quote']}\"" for r in ruleset["rules"])
    tgt = f"{target_rule['id']}: \"{target_rule['quote']}\"" if target_rule else "NONE"
    return llm.chat_text(SYS_GEN, f"RULE SET ({ruleset['name']}):\n{rules_txt}\n\nTARGET RULE TO VIOLATE: {tgt}\n\nWrite the application summary now.", max_tokens=650).strip()


def _validate(ruleset: dict, text: str, target_id: str):
    """Guard đối soát case sinh ra. Trả (ok, guard_agree, fired_wrong)."""
    fired = {}
    for r in ruleset["rules"]:
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] in ("override", "flag"):
            fired[r["id"]] = g["reason"][:70]
    wrong = [rid for rid in fired if rid != target_id]
    return (not wrong), (target_id in fired), fired


def generate(ruleset_id: str, n_violations: int = None, officer: str = "casegen"):
    rs = core.get_ruleset(ruleset_id)
    rules = rs["rules"]
    guarded = [r for r in rules if guards.compile_rule_guards(r) or r["id"] in guards.HAND_COVERAGE.get(ruleset_id, set())]
    targets = (guarded if n_violations is None else guarded[:n_violations]) or rules[:n_violations or 3]
    out_dir = GEN_DIR / ruleset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cases, skipped = [], []
    plan = [None] + targets  # case đầu = sạch
    for i, tgt in enumerate(plan):
        cid = f"GEN-{ruleset_id}-{i:02d}" + ("-clean" if tgt is None else f"-{tgt['id']}")
        text, ok, agree, fired = "", False, False, {}
        for attempt in range(2):  # sinh lại 1 lần nếu guard bắn nhầm rule khác
            text = _gen_text(rs, tgt)
            ok, agree, fired = _validate(rs, text, tgt["id"] if tgt else "__none__")
            if ok:
                break
        if not ok:
            skipped.append({"id": cid, "reason": "guard bắn vào rule ngoài mục tiêu sau 2 lần sinh", "fired": fired})
            continue
        labels = {r["id"]: "met" for r in rules}
        if tgt:
            labels[tgt["id"]] = "not_met"
        (out_dir / f"{cid}.txt").write_text(text + "\n", encoding="utf-8")
        cases.append({"id": cid, "file": f"generated/{ruleset_id}/{cid}.txt", "target": tgt["id"] if tgt else None,
                      "labels": labels, "guard_agree": bool(agree) if tgt else None})
        print(f"  {cid}: {'sạch' if not tgt else 'vi phạm ' + tgt['id']}"
              + (f" · guard {'XÁC NHẬN ✓' if agree else 'không bắt được (llm-only?) '}" if tgt else ""), flush=True)
    doc = {"ruleset": ruleset_id, "version": rs["version"], "generated_by": llm.describe()["model"],
           "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "approved": False,
           "note": "Nhãn gán by-construction (LLM chỉ viết văn bản, không tự gán nhãn); guard đối soát từng case. "
                   "CHƯA phê chuẩn — cán bộ rà văn bản + nhãn rồi đặt approved=true mới dùng làm chuẩn vàng.",
           "cases": cases, "skipped": skipped}
    (LBL_DIR / f"generated-{ruleset_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def evaluate(ruleset_id: str):
    """Eval pipeline (LLM + guard) trên bộ test đã sinh — đo accuracy + FALSE-PASS cho ruleset này."""
    doc = json.loads((LBL_DIR / f"generated-{ruleset_id}.json").read_text(encoding="utf-8"))
    info = llm.describe()
    print(f"EVAL bộ sinh cho {ruleset_id} · LLM {info['model']} · {len(doc['cases'])} case"
          + (" · NHÃN CHƯA PHÊ CHUẨN" if not doc.get("approved") else ""))
    total = correct = fp = n_notmet = 0
    errors = []
    for c in doc["cases"]:
        text = (core.DATA / "applications" / c["file"]).read_text(encoding="utf-8")
        for v in core.assess_case(c["id"], text, ruleset_id=ruleset_id,
                                  progress=lambda rid, i, n: print(f"  {c['id']} {rid} ({i+1}/{n})", end="\r")):
            truth = c["labels"][v["r"]]
            total += 1
            n_notmet += truth == "not_met"
            if v["v"] == truth:
                correct += 1
            else:
                if truth == "not_met" and v["v"] == "met":
                    fp += 1
                errors.append({"case": c["id"], "rule": v["r"], "pred": v["v"], "truth": truth, "note": v["note"][:90]})
        print()
    print(f"\nAccuracy: {correct}/{total} = {correct/max(total,1):.1%} | FALSE-PASS: {fp}/{n_notmet} (kỳ vọng 0)")
    for e in errors:
        print(f"  [{e['case']} {e['rule']}] {e['pred']} / nhãn {e['truth']} — {e['note']}")
    out = {"ruleset": ruleset_id, "llm": info["model"], "accuracy": f"{correct}/{total}",
           "false_pass": f"{fp}/{n_notmet}", "errors": errors, "labels_approved": doc.get("approved", False)}
    (core.DATA.parent / f"eval-generated-{ruleset_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[2] not in ("--gen", "--eval"):
        print(__doc__); sys.exit(1)
    rid = sys.argv[1]
    if sys.argv[2] == "--gen":
        n = int(sys.argv[3]) if len(sys.argv) > 3 else None
        d = generate(rid, n)
        print(f"\nSinh xong: {len(d['cases'])} case (bỏ {len(d['skipped'])}) -> data/labels/generated-{rid}.json")
    else:
        evaluate(rid)
