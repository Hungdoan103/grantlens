"""workflow.py — máy trạng thái ca hồ sơ + các quy tắc nghiệp vụ "trust twist".

Trạng thái:  new -> assessed -> in_review -> signed -> letter_drafted -> letter_approved
                                   ^            |
                                   +-- reopen --+  (phải ghi lý do)

Quy tắc bắt buộc được kiểm tra Ở SERVER (không tin frontend):
  1. Spot-check mù: khi bắt đầu thẩm định, hệ thống chọn ngẫu nhiên 1 tiêu chí; cán bộ phải tự
     kết luận tiêu chí đó TRƯỚC KHI được xem bất kỳ đề xuất nào của AI (API che kết quả AI tới lúc đó).
  2. Không có "duyệt tất cả": mỗi tiêu chí xác nhận riêng, kèm danh tính cán bộ + thời điểm.
  3. Sửa kết luận AI hoặc AI trả CHƯA RÕ / KHÔNG ĐỀ CẬP -> bắt buộc ghi lý do.
  4. Kết luận cuối không được là "không đề cập" (phải thành ĐẠT / KHÔNG ĐẠT / CHƯA RÕ = yêu cầu bổ sung).
  5. Ký duyệt chỉ khi 100% tiêu chí đã xác nhận; ký quá nhanh -> cảnh báo + phải xác nhận lần 2, ghi nhật ký.
  6. Thư kết quả chỉ sinh sau khi ký; thư là NHÁP tới khi cán bộ phê duyệt nội dung.
"""
import json, random
from . import store, core, rag, coi, feedback, crosscheck

STATES = ["new", "assessed", "in_review", "signed", "letter_drafted", "letter_approved"]
STATE_VI = {"new": "Mới", "assessed": "AI đã đánh giá (nháp)", "in_review": "Đang thẩm định",
            "signed": "Đã ký duyệt", "letter_drafted": "Thư nháp", "letter_approved": "Thư đã phê duyệt"}
FINAL_ALLOWED = ["met", "not_met", "unclear"]
MIN_SECONDS_PER_RULE = 8


class WorkflowError(Exception):
    def __init__(self, msg, code=409, extra=None):
        super().__init__(msg)
        self.code, self.extra = code, extra or {}


def _case(case_id):
    c = store.get_case(case_id)
    if not c:
        raise WorkflowError("Không có hồ sơ này", 404)
    return c


def _rank(s):
    return STATES.index(s)


# ---------- AI assessment ----------
def can_assess(case_id):
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("Hồ sơ đã ký duyệt — muốn đánh giá lại phải Mở lại hồ sơ (có lý do) trước.")
    return c


def assess_stream(case_id, actor="AI"):
    """Generator NDJSON events; lưu từng verdict ngay khi có."""
    c = can_assess(case_id)
    had = bool(store.get_verdicts(case_id))
    store.clear_verdicts(case_id)
    store.update_case(case_id, status="new", review_started_at=None, signed_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None, letter=None, letter_status=None)
    info = core.llm.describe()
    store.log(actor, f"Bắt đầu đánh giá AI{' (đánh giá LẠI, xoá xác nhận cũ)' if had else ''}", case_id, "system",
              {"llm": info["model"], "backend": info["backend"], "pipeline": "RAG -> extract facts -> judge on facts -> cite"})
    # --- đối chiếu chéo chống gian lận (deterministic, chạy trước AI) ---
    cc = run_crosscheck(case_id, c["text"])
    yield {"type": "crosscheck", **cc}
    rules = core.load_rules()
    for i, rule in enumerate(rules):
        yield {"type": "progress", "rule": rule["id"], "title": rule["title_vi"], "i": i, "n": len(rules)}
        v = core.assess_rule(case_id, c["text"], rule)
        store.save_ai_verdict(case_id, v)
        yield {"type": "verdict", **v}
    store.update_case(case_id, status="assessed", assessed_at=store.now(),
                      llm_model=info["model"], embed_backend=rag.EMBED_BACKEND)
    vs = store.get_verdicts(case_id)
    summary = {k: sum(1 for x in vs if x["ai_verdict"] == k) for k in core.VERDICTS}
    store.log(actor, "Hoàn tất 12 kết luận NHÁP", case_id, "system", {"summary": summary, "mock": info["is_mock"]})
    yield {"type": "done", "summary": summary}


def run_crosscheck(case_id: str, text: str = None, actor: str = "Hệ thống") -> dict:
    """Đối chiếu chéo tài liệu trong hồ sơ; lưu vào case + ghi nhật ký."""
    c = _case(case_id) if text is None else None
    cc = crosscheck.run(text if text is not None else c["text"])
    store.update_case(case_id, crosscheck=json.dumps(cc, ensure_ascii=False))
    if cc["findings"]:
        store.log(actor, f"ĐỐI CHIẾU CHÉO {case_id}: {len(cc['findings'])} điểm KHÔNG KHỚP giữa {cc['n_docs']} tài liệu — "
                  + "; ".join(f["label_vi"] if "label_vi" in f else f["category"] for f in cc["findings"]),
                  case_id, "system", {"risk": cc["risk"], "findings": len(cc["findings"])})
    else:
        store.log(actor, f"Đối chiếu chéo {case_id}: {cc['n_docs']} tài liệu nhất quán", case_id, "system",
                  {"risk": cc["risk"]})
    return cc


def attach_document(case_id: str, name: str, text: str, officer: str, role="officer"):
    """Thêm tài liệu đính kèm vào hồ sơ (trước khi ký). Đánh giá AI cũ bị vô hiệu."""
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("Hồ sơ đã ký — muốn thêm tài liệu phải Mở lại hồ sơ trước.")
    text = text.replace("\r\n", "\n").strip()
    if len(text.split()) < 10:
        raise WorkflowError("Tài liệu đính kèm quá ngắn", 400)
    new_text = c["text"].rstrip() + f"\n\n=== TÀI LIỆU: {name.strip()} ===\n\n" + text
    store.clear_verdicts(case_id)
    store.update_case(case_id, text=new_text, status="new", review_started_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None)
    store.log(officer, f"Đính kèm tài liệu '{name}' ({len(text.split())} từ) — kết quả AI cũ bị vô hiệu, cần đánh giá lại",
              case_id, role, {"doc": name, "words": len(text.split())})
    return run_crosscheck(case_id, new_text, actor=officer)


# ---------- review ----------
def start_review(case_id, officer, role="officer", acknowledge_coi=False, coi_reason=""):
    c = _case(case_id)
    if not officer.strip():
        raise WorkflowError("Cần tên cán bộ thẩm định", 400)
    if c["status"] == "in_review":
        return c
    if c["status"] != "assessed":
        raise WorkflowError(f"Chưa thể thẩm định ở trạng thái '{STATE_VI[c['status']]}'")
    # --- kiểm tra xung đột lợi ích TRƯỚC khi giao hồ sơ ---
    ci = coi.check(officer, c)
    if ci["level"] == "block":
        store.log("Hệ thống", f"CHẶN COI: {officer} không được thẩm định {case_id} — " + " | ".join(ci["reasons"]),
                  case_id, "system", {"coi": ci})
        raise WorkflowError("Xung đột lợi ích — không thể nhận hồ sơ này: " + " ".join(ci["reasons"]), 409,
                            {"coi": ci})
    if ci["level"] == "warn":
        if not acknowledge_coi:
            raise WorkflowError("Cảnh báo xung đột lợi ích: " + " ".join(ci["reasons"]) +
                                " — ghi lý do và xác nhận để tiếp tục.", 428,
                                {"coi": ci, "need_coi_ack": True})
        if len((coi_reason or "").strip()) < 8:
            raise WorkflowError("Tiếp tục với cảnh báo COI bắt buộc ghi lý do (≥ 8 ký tự)", 400, {"coi": ci})
        store.log(officer, f"Xác nhận tiếp tục dù cảnh báo COI — lý do: {coi_reason}. Cảnh báo: " + " | ".join(ci["reasons"]),
                  case_id, role, {"coi": ci, "reason": coi_reason})
    vs = store.get_verdicts(case_id)
    spot = random.choice(vs)
    store.update_case(case_id, status="in_review", officer=officer, review_started_at=store.now(),
                      spot_rule=spot["rule_id"], spot_ai=spot["ai_verdict"], spot_answer=None)
    store.log(officer, f"Bắt đầu thẩm định. Spot-check mù: tiêu chí {spot['rule_id']} (AI bị che tới khi trả lời)",
              case_id, role, {"spot_rule": spot["rule_id"]})
    return store.get_case(case_id)


def spot_pending(c):
    return c["status"] == "in_review" and c.get("spot_rule") and not c.get("spot_answer")


def spot_check(case_id, verdict, officer, role="officer"):
    c = _case(case_id)
    if not spot_pending(c):
        raise WorkflowError("Không có spot-check đang chờ")
    if verdict not in core.VERDICTS:
        raise WorkflowError("Kết luận không hợp lệ", 400)
    agree = verdict == c["spot_ai"]
    store.update_case(case_id, spot_answer=verdict)
    store.log(officer, f"Spot-check {c['spot_rule']}: cán bộ = {verdict}, AI = {c['spot_ai']} → {'TRÙNG' if agree else 'KHÁC'}",
              case_id, role, {"rule": c["spot_rule"], "officer": verdict, "ai": c["spot_ai"], "agree": agree})
    return {"rule": c["spot_rule"], "officer": verdict, "ai": c["spot_ai"], "agree": agree}


def confirm(case_id, rule_id, final_verdict, reason, officer, role="officer"):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Chỉ xác nhận được khi hồ sơ đang thẩm định")
    if spot_pending(c):
        raise WorkflowError("Phải hoàn thành spot-check mù trước khi xem/xác nhận đề xuất của AI")
    v = store.get_verdict(case_id, rule_id)
    if not v:
        raise WorkflowError("Không có tiêu chí này", 404)
    if final_verdict not in FINAL_ALLOWED:
        raise WorkflowError('Kết luận cuối phải là ĐẠT / KHÔNG ĐẠT / CHƯA RÕ (không được để "không đề cập")', 400)
    reason = (reason or "").strip()
    changed = final_verdict != v["ai_verdict"]
    need_reason = changed or v["ai_verdict"] in ("unclear", "not_addressed") or final_verdict == "unclear"
    if need_reason and len(reason) < 8:
        raise WorkflowError("Tiêu chí này bắt buộc ghi lý do (≥ 8 ký tự) trước khi xác nhận", 400)
    store.confirm_verdict(case_id, rule_id, final_verdict, reason, officer)
    store.log(officer, f"Xác nhận {rule_id} = {final_verdict}" + (f" (AI: {v['ai_verdict']} → SỬA)" if changed else "")
              + (f" — lý do: {reason}" if reason else ""), case_id, role,
              {"rule": rule_id, "ai": v["ai_verdict"], "final": final_verdict, "override": changed, "reason": reason})
    return store.get_verdict(case_id, rule_id)


def unconfirm(case_id, rule_id, officer, role="officer"):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Chỉ bỏ xác nhận được khi hồ sơ đang thẩm định")
    store.unconfirm_verdict(case_id, rule_id)
    store.log(officer, f"Bỏ xác nhận {rule_id}", case_id, role, {"rule": rule_id})


def sign(case_id, officer, acknowledge_fast=False, role="officer"):
    c = _case(case_id)
    if c["status"] != "in_review":
        raise WorkflowError("Chỉ ký duyệt được khi hồ sơ đang thẩm định")
    vs = store.get_verdicts(case_id)
    missing = [v["rule_id"] for v in vs if not v["confirmed_at"]]
    if missing:
        raise WorkflowError(f"Còn {len(missing)} tiêu chí chưa xác nhận: {', '.join(missing)}", 409, {"missing": missing})
    from datetime import datetime
    elapsed = (datetime.now() - datetime.fromisoformat(c["review_started_at"])).total_seconds()
    min_expected = len(vs) * MIN_SECONDS_PER_RULE
    fast = elapsed < min_expected
    if fast and not acknowledge_fast:
        store.log("Hệ thống", f"CẢNH BÁO: ký duyệt sau {elapsed:.0f}s cho {len(vs)} tiêu chí (< {min_expected}s) — dấu hiệu rubber-stamping",
                  case_id, "system", {"elapsed": round(elapsed), "min_expected": min_expected})
        raise WorkflowError(f"Bạn thẩm định {len(vs)} tiêu chí trong {elapsed:.0f} giây — nhanh bất thường (kỳ vọng ≥ {min_expected}s). "
                            f"Cảnh báo đã ghi nhật ký. Xác nhận lại nếu vẫn muốn ký.", 428,
                            {"elapsed": round(elapsed), "min_expected": min_expected, "need_ack": True})
    store.update_case(case_id, status="signed", signed_at=store.now())
    overrides = sum(1 for v in vs if v["final_verdict"] != v["ai_verdict"])
    # --- feedback loop: quyết định đã ký -> kho ground-truth để tinh chỉnh model ---
    rules_by_id = {r["id"]: r for r in core.load_rules()}
    n_fb = feedback.record_signed_case(c, vs, rules_by_id, c.get("llm_model") or "")
    if n_fb:
        store.log("Hệ thống", f"Feedback loop: lưu {n_fb} mẫu (cán bộ sửa AI / người quyết thay AI) vào kho ground-truth",
                  case_id, "system", {"n_samples": n_fb, "file": "data/feedback/feedback.jsonl"})
    store.log(officer, f"KÝ DUYỆT kết quả thẩm định ({elapsed:.0f}s, {overrides} tiêu chí sửa so với AI"
              + (", ký nhanh — đã xác nhận cảnh báo" if fast else "") + ")", case_id, role,
              {"elapsed": round(elapsed), "overrides": overrides, "fast_ack": fast})
    return store.get_case(case_id, with_text=False)


def reopen(case_id, officer, reason, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("Hồ sơ chưa ký duyệt, không cần mở lại")
    if len((reason or "").strip()) < 8:
        raise WorkflowError("Mở lại hồ sơ bắt buộc ghi lý do (≥ 8 ký tự)", 400)
    store.update_case(case_id, status="in_review", signed_at=None, letter=None, letter_status=None,
                      letter_approved_at=None, review_started_at=store.now())
    store.log(officer, f"MỞ LẠI hồ sơ đã ký — lý do: {reason}", case_id, role, {"reason": reason})
    return store.get_case(case_id, with_text=False)


# ---------- letter ----------
def draft_letter(case_id, officer, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("Thư kết quả chỉ được soạn sau khi cán bộ ký duyệt 100% tiêu chí")
    vs = store.get_verdicts(case_id)
    finals = [{"rule": v["rule_id"], "title": r["title_vi"], "rule_quote": v["rq"], "applicant_quote": v["aq"],
               "verdict": v["final_verdict"], "officer_reason": v["officer_reason"] or ""}
              for v in vs for r in core.load_rules() if r["id"] == v["rule_id"]]
    text = core.draft_letter(case_id, c["applicant"] or case_id, finals, officer=c.get("officer") or officer,
                             org=c.get("org") or "")
    store.update_case(case_id, status="letter_drafted", letter=text, letter_status="draft")
    store.log("AI", "Soạn NHÁP thư kết quả từ kết luận đã ký", case_id, "system", {"words": len(text.split())})
    return text


def approve_letter(case_id, officer, edited_text=None, role="officer"):
    c = _case(case_id)
    if c["status"] != "letter_drafted":
        raise WorkflowError("Chưa có thư nháp để phê duyệt")
    text = (edited_text or c["letter"] or "").strip()
    edited = bool(edited_text) and edited_text.strip() != (c["letter"] or "").strip()
    store.update_case(case_id, status="letter_approved", letter=text, letter_status="approved",
                      letter_approved_at=store.now())
    store.log(officer, "PHÊ DUYỆT nội dung thư kết quả" + (" (có chỉnh sửa tay)" if edited else ""), case_id, role,
              {"edited": edited})
    return store.get_case(case_id, with_text=False)


# ---------- view helpers ----------
def case_view(case_id):
    """Trả case + verdicts; CHE kết quả AI nếu spot-check đang chờ."""
    c = _case(case_id)
    vs = store.get_verdicts(case_id)
    masked = spot_pending(c)
    if masked:
        for v in vs:
            for k in ("ai_verdict", "ai_confidence", "facts", "aq", "note", "chunk_id", "retrieval", "needs_attention"):
                v[k] = None
    return {"case": c, "verdicts": vs, "ai_masked": masked, "states": STATES, "state_vi": STATE_VI}
