"""casegen.py — AI SINH BỘ TEST CÓ NHÃN cho từng ruleset, kèm cơ chế chống nhiễu và phê chuẩn nhãn.

Vì sao nhãn đáng tin (và vì sao vẫn PHẢI có người duyệt):
  1. LLM CHỈ viết văn bản hồ sơ. NHÃN do code gán "by construction": mỗi case nhắm đúng MỘT rule
     vi phạm (not_met), mọi rule khác mặc định met.
  2. GUARD ĐỐI SOÁT: guard bắn vào rule ngoài mục tiêu -> case bị loại, sinh lại.
  3. VERIFIER ĐỘC LẬP (lượt LLM riêng, không thấy nhãn): với từng rule phụ, hỏi "văn bản có nêu bằng
     chứng RÕ RÀNG thỏa rule này không?". Rule trả lời "no/unclear" -> đưa vào weak_labels ->
     LOẠI KHỎI METRIC. Đây là cách tách "lỗi hệ thẩm định" khỏi "lỗi đề thi tự sinh" (phê bình #4).
  4. Nhãn lưu approved=false. Eval trên nhãn chưa phê chuẩn LUÔN in cảnh báo và ghi provisional=true
     vào file kết quả — không được dùng để pitch là "đã chứng minh" (phê bình #3).

CLI:
  python -m backend.casegen <ruleset_id> --gen [N]        sinh bộ test
  python -m backend.casegen <ruleset_id> --eval           đo (tách metric mục tiêu / phụ)
  python -m backend.casegen <ruleset_id> --approve "Tên cán bộ"   phê chuẩn nhãn sau khi rà
  python -m backend.casegen <ruleset_id> --dispute <case_id> "lý do"   loại 1 case khỏi bộ
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from . import core, llm, guards

GEN_DIR = core.DATA / "applications" / "generated"
LBL_DIR = core.DATA / "labels"

SYS_GEN = """You write REALISTIC grant application summaries for testing an eligibility-screening system.
You are given a rule set (verbatim eligibility rules) and ONE target rule to violate.
Write an application summary (220-320 words, English, structured like a cover/eligibility summary with short labeled paragraphs: Applicant, entity/registration details, then one short paragraph per topic).
Hard requirements:
- The application must CLEARLY VIOLATE the target rule with an explicit, quotable fact (state the offending number/entity/fact plainly; do not hint, state it).
- Every OTHER rule in the set must be CLEARLY SATISFIED with an explicit, quotable fact (numbers above thresholds, required items present, excluded statuses explicitly negated). Do NOT introduce alternative arrangements (related entities, equivalents, exemptions) unless the rule explicitly allows them.
- Realistic Australian/US organisation and person names; do not reuse names across runs; plain text only, no markdown formatting.
If the target rule is "NONE", write a fully compliant application instead (every rule clearly satisfied)."""

SYS_VERIFY = """You are a strict evidence checker. You are given an application text and ONE eligibility rule.
Answer ONLY whether the APPLICATION TEXT contains explicit, quotable evidence that the rule is SATISFIED.
- "yes"     : the text states a fact that clearly satisfies the rule.
- "no"      : the text states a fact that clearly violates the rule.
- "unclear" : the text is silent, ambiguous, or relies on an arrangement the rule does not clearly permit.
Judge only what is written; do not assume, do not infer from typical practice."""

VERIFY_SCHEMA = {"type": "object",
                 "properties": {"answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
                                "evidence": {"type": "string"}},
                 "required": ["answer", "evidence"]}


def _gen_text(ruleset: dict, target_rule: dict = None) -> str:
    rules_txt = "\n".join(f"- {r['id']}: \"{r['quote']}\"" for r in ruleset["rules"])
    tgt = f"{target_rule['id']}: \"{target_rule['quote']}\"" if target_rule else "NONE"
    txt = llm.chat_text(SYS_GEN, f"RULE SET ({ruleset['name']}):\n{rules_txt}\n\nTARGET RULE TO VIOLATE: {tgt}\n\nWrite the application summary now.", max_tokens=650)
    return guards.normalise(txt).strip()


def _verify(ruleset: dict, text: str, target_id: str, votes: int = 2):
    """Kiểm nhãn phụ — verifier CŨNG là LLM nên không được tin tuyệt đối (phê bình của khách):
      • Bỏ phiếu {votes} lượt độc lập: chỉ đánh 'weak' khi MỌI lượt đều nói không có bằng chứng rõ.
        Hai lượt mâu thuẫn -> 'disagreement': GIỮ trong metric nhưng gắn cờ cho người rà.
      • Mã nguồn có quyền PHỦ QUYẾT: nếu guard/compiler khẳng định rule đó vi phạm hoặc thoả bằng
        số liệu cụ thể, kết luận của code thắng phiếu LLM (code không mơ hồ như LLM).
    """
    weak, detail, disagree = [], {}, []
    for r in ruleset["rules"]:
        answers = []
        for _ in range(max(1, votes)):
            try:
                v = llm.chat_json(SYS_VERIFY, f"RULE {r['id']}: \"{r['quote']}\"\n\nAPPLICATION TEXT:\n{text}\n\nIs this rule satisfied by explicit evidence?", VERIFY_SCHEMA, max_tokens=220)
                answers.append(v.get("answer", "unclear"))
            except Exception:
                continue
        if not answers:
            continue
        detail[r["id"]] = answers
        if r["id"] == target_id:
            continue  # rule mục tiêu: nhãn not_met do thiết kế, verifier chỉ tham khảo
        # code phủ quyết: guard bắt được vi phạm rõ ràng -> nhãn 'met' chắc chắn SAI, không phải "yếu"
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] == "override":
            detail[r["id"]] = answers + ["code:violation"]
            weak.append(r["id"])   # nhãn met không đúng -> loại khỏi metric, người rà xử lý
            continue
        if all(a == "yes" for a in answers):
            continue               # mọi lượt nói có bằng chứng -> nhãn met đáng tin
        if any(a == "yes" for a in answers):
            disagree.append(r["id"])  # lượt khác nhau -> giữ metric nhưng gắn cờ
            continue
        weak.append(r["id"])       # mọi lượt nói không rõ -> loại khỏi metric
    return weak, detail, disagree


def _guard_crosscheck(ruleset: dict, text: str, target_id: str):
    fired = {}
    for r in ruleset["rules"]:
        g = guards.check(ruleset["id"], r, "met", text, {})
        if g and g["action"] in ("override", "flag"):
            fired[r["id"]] = g["reason"][:70]
    wrong = [rid for rid in fired if rid != target_id]
    return (not wrong), (target_id in fired), fired


def generate(ruleset_id: str, n_violations: int = None, officer: str = "casegen", verify: bool = True):
    rs = core.get_ruleset(ruleset_id)
    rules = rs["rules"]
    guarded = [r for r in rules if guards.compile_rule_guards(r) or r["id"] in guards.HAND_COVERAGE.get(ruleset_id, set())]
    targets = (guarded if n_violations is None else guarded[:n_violations]) or rules[:n_violations or 3]
    out_dir = GEN_DIR / ruleset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cases, skipped = [], []
    for i, tgt in enumerate([None] + targets):  # case đầu = sạch
        cid = f"GEN-{ruleset_id}-{i:02d}" + ("-clean" if tgt is None else f"-{tgt['id']}")
        tid = tgt["id"] if tgt else "__none__"
        text, ok, agree, fired = "", False, False, {}
        for _ in range(2):  # sinh lại 1 lần nếu guard bắn nhầm rule khác
            text = _gen_text(rs, tgt)
            ok, agree, fired = _guard_crosscheck(rs, text, tid)
            if ok:
                break
        if not ok:
            skipped.append({"id": cid, "reason": "guard bắn vào rule ngoài mục tiêu sau 2 lần sinh", "fired": fired})
            continue
        weak, vdetail, disagree = _verify(rs, text, tid) if verify else ([], {}, [])
        labels = {r["id"]: "met" for r in rules}
        if tgt:
            labels[tgt["id"]] = "not_met"
        (out_dir / f"{cid}.txt").write_text(text + "\n", encoding="utf-8")
        cases.append({"id": cid, "file": f"generated/{ruleset_id}/{cid}.txt", "target": tgt["id"] if tgt else None,
                      "labels": labels, "guard_agree": bool(agree) if tgt else None,
                      "weak_labels": weak, "verifier_disagreement": disagree, "verifier": vdetail,
                      "status": "pending_review"})
        print(f"  {cid}: {'sạch' if not tgt else 'vi phạm ' + tgt['id']}"
              + (f" · guard {'XÁC NHẬN' if agree else 'không bắt'}" if tgt else "")
              + (f" · {len(weak)} nhãn phụ YẾU (loại metric): {weak}" if weak else " · nhãn phụ đều có bằng chứng")
              + (f" · {len(disagree)} nhãn verifier MÂU THUẪN (giữ metric, gắn cờ): {disagree}" if disagree else ""), flush=True)
    doc = {"ruleset": ruleset_id, "version": rs["version"], "generated_by": llm.describe()["model"],
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "approved": False, "approved_by": None, "approved_at": None,
           "note": "Nhãn gán by-construction (LLM chỉ viết văn bản); guard đối soát + verifier độc lập đánh dấu "
                   "nhãn phụ yếu. CHƯA PHÊ CHUẨN — số đo từ bộ này là TẠM (provisional), không dùng để tuyên bố "
                   "'đã chứng minh'. Cán bộ rà văn bản + nhãn rồi chạy --approve.",
           "cases": cases, "skipped": skipped}
    (LBL_DIR / f"generated-{ruleset_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def _load(ruleset_id):
    p = LBL_DIR / f"generated-{ruleset_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"Chưa có bộ test sinh cho '{ruleset_id}' — chạy --gen trước")
    return p, json.loads(p.read_text(encoding="utf-8"))


def approve(ruleset_id: str, officer: str):
    """Cán bộ phê chuẩn nhãn sau khi đã rà văn bản — từ đây số đo mới hết 'provisional'."""
    p, doc = _load(ruleset_id)
    live = [c for c in doc["cases"] if c.get("status") != "disputed"]
    doc.update(approved=True, approved_by=officer, approved_at=datetime.now().isoformat(timespec="seconds"))
    for c in doc["cases"]:
        if c.get("status") == "pending_review":
            c["status"] = "approved"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "approved_by": officer, "n_cases": len(live)}


def dispute(ruleset_id: str, case_id: str, reason: str):
    """Đánh dấu một case là sai/đáng ngờ -> loại khỏi mọi metric (giữ lại để truy vết)."""
    p, doc = _load(ruleset_id)
    hit = next((c for c in doc["cases"] if c["id"] == case_id), None)
    if not hit:
        raise KeyError(case_id)
    hit.update(status="disputed", dispute_reason=reason)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "case": case_id, "reason": reason}


def evaluate(ruleset_id: str):
    """Đo pipeline trên bộ sinh — TÁCH metric để không lẫn nhiễu đề thi (phê bình #4):
      • target : rule mục tiêu của từng case — thước đo THẬT (bắt được vi phạm hay không).
      • secondary: các rule phụ CÓ bằng chứng rõ (đã loại weak_labels) — tham khảo.
      • excluded: rule phụ nhãn yếu / case disputed — không tính điểm.
    """
    _, doc = _load(ruleset_id)
    info = llm.describe()
    approved = bool(doc.get("approved"))
    live = [c for c in doc["cases"] if c.get("status") != "disputed"]
    n_disputed = len(doc["cases"]) - len(live)
    print("=" * 70)
    print(f"EVAL bộ sinh '{ruleset_id}' · LLM {info['model']} · {len(live)} case"
          + (f" (bỏ {n_disputed} case disputed)" if n_disputed else ""))
    if not approved:
        print("!! NHÃN CHƯA ĐƯỢC CÁN BỘ PHÊ CHUẨN — số dưới đây là TẠM (provisional).")
        print("!! Không dùng để tuyên bố 'đã chứng minh quỹ này'. Rà xong chạy: "
              f"python -m backend.casegen {ruleset_id} --approve \"Tên cán bộ\"")
    print("=" * 70)
    T = {"n": 0, "ok": 0, "fp": 0}        # rule mục tiêu
    S = {"n": 0, "ok": 0, "fp": 0}        # rule phụ có bằng chứng
    excluded, errors = 0, []
    for c in live:
        weak = set(c.get("weak_labels") or [])
        text = (core.DATA / "applications" / c["file"]).read_text(encoding="utf-8")
        for v in core.assess_case(c["id"], text, ruleset_id=ruleset_id,
                                  progress=lambda rid, i, n: print(f"  {c['id']} {rid} ({i+1}/{n})", end="\r")):
            truth = c["labels"][v["r"]]
            is_target = v["r"] == c.get("target")
            if not is_target and v["r"] in weak:
                excluded += 1
                continue
            bucket = T if is_target else S
            bucket["n"] += 1
            if v["v"] == truth:
                bucket["ok"] += 1
            else:
                if truth == "not_met" and v["v"] == "met":
                    bucket["fp"] += 1
                errors.append({"case": c["id"], "rule": v["r"], "kind": "target" if is_target else "secondary",
                               "pred": v["v"], "truth": truth, "note": (v["note"] or "")[:80]})
        print()
    total_seen = T["n"] + S["n"] + excluded
    excl_rate = excluded / max(total_seen, 1)
    print("\n" + "#" * 70)
    print("# THƯỚC ĐO CHÍNH — [MỤC TIÊU]: mỗi case có đúng 1 vi phạm cài sẵn, hệ có bắt được không")
    print(f"#   Bắt đúng vi phạm : {T['ok']}/{T['n']}" + (f" = {T['ok']/T['n']:.0%}" if T["n"] else ""))
    print(f"#   FALSE-PASS       : {T['fp']}/{T['n']} (kỳ vọng 0 — chỉ số quan trọng nhất)")
    print("#" * 70)
    print(f"[THAM KHẢO] rule phụ có bằng chứng rõ: {S['ok']}/{S['n']}" + (f" = {S['ok']/S['n']:.0%}" if S["n"] else "")
          + "  — KHÔNG dùng làm thước đo chất lượng: rule phụ do đề thi tự sinh, dễ nhiễu.")
    print(f"[LOẠI]      {excluded}/{total_seen} lượt rule bị loại khỏi metric ({excl_rate:.0%}) — nhãn phụ yếu.")
    if excl_rate > 0.3:
        print("!! CẢNH BÁO: loại >30% — bộ test tự sinh chất lượng thấp, đừng dùng số phụ để kết luận gì.")
    if not approved:
        print("!! NHẮC LẠI: nhãn chưa phê chuẩn -> mọi số trên là TẠM TÍNH (provisional).")
    for e in errors:
        print(f"  [{e['kind']}] {e['case']} {e['rule']}: {e['pred']} / nhãn {e['truth']} — {e['note']}")
    out = {"ruleset": ruleset_id, "llm": info["model"], "provisional": not approved,
           "labels_approved": approved, "approved_by": doc.get("approved_by"),
           "primary_metric": "target",
           "target": {"correct": T["ok"], "total": T["n"], "false_pass": T["fp"],
                      "note": "THƯỚC ĐO CHÍNH — vi phạm cài sẵn có bị bắt không"},
           "secondary": {"correct": S["ok"], "total": S["n"], "false_pass": S["fp"],
                         "note": "chỉ tham khảo — rule phụ do đề thi tự sinh, dễ nhiễu"},
           "excluded_weak_labels": excluded, "excluded_rate": round(excl_rate, 3),
           "low_quality_testset": excl_rate > 0.3,
           "disputed_cases": n_disputed, "errors": errors}
    (core.DATA.parent / f"eval-generated-{ruleset_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    rid, cmd = sys.argv[1], sys.argv[2]
    if cmd == "--gen":
        n = int(sys.argv[3]) if len(sys.argv) > 3 else None
        d = generate(rid, n)
        print(f"\nSinh xong: {len(d['cases'])} case (bỏ {len(d['skipped'])}) -> data/labels/generated-{rid}.json"
              f"\nNHÃN CHƯA PHÊ CHUẨN — rà rồi chạy: python -m backend.casegen {rid} --approve \"Tên cán bộ\"")
    elif cmd == "--eval":
        evaluate(rid)
    elif cmd == "--approve":
        print(approve(rid, sys.argv[3] if len(sys.argv) > 3 else "Cán bộ"))
    elif cmd == "--dispute":
        print(dispute(rid, sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "không nêu lý do"))
    else:
        print(__doc__); sys.exit(1)
