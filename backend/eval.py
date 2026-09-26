"""eval.py — score the pipeline against the ground truth (no database involved, runs purely on files).
Run:  python -m backend.eval [--only HS-01,HS-02]
Metrics:
  1. Verdict accuracy vs labels (overall, per rule type, per confidence level) + confusion matrix + failure cases
  2. Citation match rate (quotation matches the source verbatim — expected 100% by construction)
  3. Bias score: % of criteria with the same verdict within EACH bias pair (expected 100%)
  4. Safety: share of truly "not met" cases that the AI marked "met" (false pass — the most dangerous error)
Results are printed as a table and saved to eval-results-<model>.json.
"""
import json, sys, time
from pathlib import Path
from . import core, llm

DATA = Path(__file__).resolve().parent.parent / "data"


def main():
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    gt_doc = json.loads((DATA / "labels" / "ground-truth.json").read_text(encoding="utf-8"))
    gt, pairs = gt_doc["labels"], gt_doc["bias_pairs"]
    cases = [c for c in core.load_manifest() if not only or c["id"] in only]
    rules = {}  # rule_id -> rule; manifest entries may point at different rulesets
    for c in cases:
        rs = core.get_ruleset(c.get("ruleset"))
        for r in rs["rules"]:
            rules[r["id"]] = r
    info = llm.describe()
    if info["is_mock"]:
        print("!! Running in MOCK mode — these figures have NO reporting value. Set GRANTLENS_LLM=ollama.")
    all_results, t0 = {}, time.time()
    for c in cases:
        print(f"\n=== {c['id']} — {c['scenario']} ===")
        text = core.load_app_text(c)
        res = []
        for v in core.assess_case(c["id"], text, progress=lambda rid, i, n: print(f"  {rid} ({i + 1}/{n})", end="\r"),
                                  ruleset_id=c.get("ruleset"), meta={"directorate": c.get("directorate", "")}):
            res.append(v)
        all_results[c["id"]] = res
        print(f"  done: " + " ".join(f"{v['r']}={v['v'][:3]}" for v in res))

    total = correct = 0
    by_type = {"quantitative": [0, 0], "qualitative": [0, 0]}
    by_conf = {"high": [0, 0], "medium": [0, 0], "low": [0, 0]}
    confusion, errors, false_pass = {}, [], 0
    for cid, res in all_results.items():
        for v in res:
            truth = gt[cid][v["r"]]
            total += 1
            bt, bc = by_type[rules[v["r"]]["type"]], by_conf[v["confidence"]]
            bt[1] += 1; bc[1] += 1
            if v["v"] == truth:
                correct += 1; bt[0] += 1; bc[0] += 1
            else:
                errors.append({"case": cid, "rule": v["r"], "pred": v["v"], "truth": truth,
                               "confidence": v["confidence"], "facts": [f["fact"] for f in v["facts"]], "note": v["note"]})
                if truth == "not_met" and v["v"] == "met":
                    false_pass += 1
            confusion[f"{truth}->{v['v']}"] = confusion.get(f"{truth}->{v['v']}", 0) + 1

    cites = [v for res in all_results.values() for v in res if v["aq"]]
    cite_ok = sum(1 for v in cites if v["cite_app_ok"])

    bias = []
    for a, b in pairs:
        if a in all_results and b in all_results:
            va = {v["r"]: v["v"] for v in all_results[a]}
            vb = {v["r"]: v["v"] for v in all_results[b]}
            m = sum(1 for r in va if va[r] == vb[r])
            bias.append({"pair": f"{a}/{b}", "match": m, "total": len(va),
                         "diff": [{"rule": r, a: va[r], b: vb[r]} for r in va if va[r] != vb[r]]})

    elapsed = time.time() - t0
    print("\n" + "=" * 66)
    print(f"EVALUATION RESULTS — GrantLens · LLM: {info['model']} ({info['backend']}) · {len(cases)} applications")
    print("=" * 66)
    print(f"1. Verdict accuracy:         {correct}/{total} = {correct / total:.1%}")
    for t, (c, n) in by_type.items():
        print(f"   - rule {t:<13}: {c}/{n} = {c / n:.1%}" if n else "")
    for t, (c, n) in by_conf.items():
        if n:
            print(f"   - confidence {t:<8}: {c}/{n} = {c / n:.1%}")
    print(f"2. Citation match rate:      {cite_ok}/{len(cites)} = {cite_ok / max(len(cites), 1):.1%} (expected 100%)")
    for b in bias:
        print(f"3. Bias score {b['pair']:<14}: {b['match']}/{b['total']} = {b['match'] / b['total']:.1%} (expected 100%)")
    n_not_met = sum(1 for cid in all_results for r in gt[cid] if gt[cid][r] == "not_met")
    print(f"4. False pass (not_met→met): {false_pass}/{n_not_met} (expected 0)")
    print(f"Time: {elapsed:.0f}s for {total} rule × application evaluations ({elapsed / max(total, 1):.1f}s each, 2 LLM calls per rule)")
    if errors:
        print("\nFailure cases:")
        for e in errors:
            print(f"  [{e['case']} {e['rule']}] predicted {e['pred']} / label {e['truth']} (conf {e['confidence']}) — {e['note']}")

    out = Path(__file__).resolve().parent.parent / f"eval-results-{info['model'].replace(':', '_').replace('/', '_')}.json"
    out.write_text(json.dumps({
        "llm": info, "n_cases": len(cases),
        "accuracy": {"total": f"{correct}/{total}", "pct": round(correct / total, 4),
                     "by_type": {t: f"{c}/{n}" for t, (c, n) in by_type.items()},
                     "by_confidence": {t: f"{c}/{n}" for t, (c, n) in by_conf.items()}},
        "confusion": confusion,
        "citation_match": {"ok": cite_ok, "total": len(cites)},
        "bias": bias, "false_pass": {"n": false_pass, "of": n_not_met},
        "errors": errors, "elapsed_sec": round(elapsed), "raw": all_results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
