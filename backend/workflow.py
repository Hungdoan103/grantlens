"""workflow.py — máy trạng thái ca hồ sơ + các quy tắc nghiệp vụ "trust twist".

Trạng thái:
  new -> assessed -> in_review -> signed -> letter_drafted -> letter_approved
              ^          |  ^                  |
              |          v  +---- reopen ------+   (phải ghi lý do)
              +-- awaiting_supplement              (yêu cầu bổ sung, nhiều vòng, có hạn SLA)

Quy tắc bắt buộc kiểm tra Ở SERVER (không tin frontend):
  1. Multi-GO + versioning: mỗi hồ sơ KHÓA bộ tiêu chí (id + version + nội dung) tại lần đánh giá
     đầu — đổi rule giữa chừng không ảnh hưởng hồ sơ đang xử lý; snapshot lưu trong case.
  2. Spot-check mù; xác nhận từng dòng; sửa AI / AI chưa rõ -> bắt buộc lý do.
  3. Ký duyệt: 100% tiêu chí + ngưỡng thời gian tối thiểu (GRANTLENS_MIN_SECONDS_PER_RULE, mặc định 15s/tiêu chí).
  4. KÝ CẤP 2: hồ sơ có tiêu chí KHÔNG ĐẠT / CHƯA RÕ chỉ được phát hành thư sau khi một CÁN BỘ QUẢN LÝ
     (role=manager trong sổ cán bộ, khác người thẩm định, qua kiểm COI) ký xác nhận.
  5. Vòng bổ sung hồ sơ: cán bộ yêu cầu bổ sung (danh mục + hạn) -> chờ tài liệu -> đính kèm -> đánh giá lại;
     số vòng được đếm và ghi nhật ký.
  6. False-pass guard (backend/guards.py) chạy trong pipeline đánh giá — xem core.assess_rule.
"""
import json, os, random
from datetime import date, timedelta
from . import store, core, rag, coi, feedback, crosscheck

STATES = ["new", "assessed", "in_review", "awaiting_supplement", "signed", "letter_drafted", "letter_approved"]
STATE_VI = {"new": "Mới", "assessed": "AI đã đánh giá (nháp)", "in_review": "Đang thẩm định",
            "awaiting_supplement": "Chờ bổ sung hồ sơ", "signed": "Đã ký duyệt",
            "letter_drafted": "Thư nháp", "letter_approved": "Thư đã phê duyệt"}
FINAL_ALLOWED = ["met", "not_met", "unclear"]
MIN_SECONDS_PER_RULE = int(os.environ.get("GRANTLENS_MIN_SECONDS_PER_RULE", "15"))
# Xác nhận ĐẠT trên tiêu chí KHÔNG có lưới đỡ mã nguồn phải kèm bằng chứng tự kiểm chứng
ATTESTATION_MIN_CHARS = int(os.environ.get("GRANTLENS_ATTESTATION_MIN_CHARS", "25"))


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


def case_rules(c: dict) -> list:
    """Bộ tiêu chí CỦA HỒ SƠ NÀY: snapshot đã khóa nếu có, ngược lại bộ mặc định/được gán."""
    snap = c.get("ruleset_snapshot")
    if snap and snap.get("rules"):
        return snap["rules"]
    return core.get_ruleset(c.get("ruleset_id"))["rules"]


def needs_countersign(c: dict, vs: list = None) -> bool:
    """Hồ sơ đã ký mà có tiêu chí không đạt/chưa rõ -> phải có quản lý ký cấp 2 mới phát hành thư."""
    if _rank(c["status"]) < _rank("signed") or c.get("countersigned_by"):
        return False
    vs = vs if vs is not None else store.get_verdicts(c["id"])
    return any(v["final_verdict"] != "met" for v in vs)


# ---------- ruleset (multi-GO + versioning) ----------
def set_ruleset(case_id, ruleset_id, officer, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("Hồ sơ đã ký — không đổi được bộ tiêu chí.")
    try:
        rs = core.get_ruleset(ruleset_id, require_approved=True)
    except KeyError as e:
        raise WorkflowError(str(e), 409)
    store.clear_verdicts(case_id)
    store.update_case(case_id, ruleset_id=rs["id"], ruleset_version=rs["version"], ruleset_snapshot=None,
                      status="new", review_started_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None)
    store.log(officer, f"Gán bộ tiêu chí '{rs['name']}' v{rs['version']} cho {case_id} — kết quả cũ (nếu có) bị vô hiệu",
              case_id, role, {"ruleset": rs["id"], "version": rs["version"]})
    return store.get_case(case_id, with_text=False)


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
                      spot_rule=None, spot_answer=None, spot_ai=None, letter=None, letter_status=None,
                      countersigned_by=None, countersigned_at=None)
    info = core.llm.describe()
    # --- KHÓA bộ tiêu chí tại thời điểm đánh giá đầu tiên (versioning) ---
    snap = c.get("ruleset_snapshot")
    if snap and snap.get("rules"):
        rs = snap
        store.log(actor, f"Dùng bộ tiêu chí ĐÃ KHÓA của hồ sơ: {rs['name']} v{rs['version']}", case_id, "system",
                  {"ruleset": rs["id"], "version": rs["version"], "locked": True})
    else:
        try:
            full = core.get_ruleset(c.get("ruleset_id"), require_approved=True)
        except KeyError as e:
            raise WorkflowError(str(e), 409)
        rs = {k: full[k] for k in ("id", "name", "version", "rules")}
        store.update_case(case_id, ruleset_id=rs["id"], ruleset_version=rs["version"],
                          ruleset_snapshot=json.dumps(rs, ensure_ascii=False))
        store.log(actor, f"KHÓA bộ tiêu chí cho hồ sơ: {rs['name']} v{rs['version']} ({len(rs['rules'])} tiêu chí) — "
                  "rule đổi sau này không ảnh hưởng hồ sơ đang xử lý", case_id, "system",
                  {"ruleset": rs["id"], "version": rs["version"], "n_rules": len(rs["rules"])})
    rules = rs["rules"]
    store.log(actor, f"Bắt đầu đánh giá AI{' (đánh giá LẠI, xoá xác nhận cũ)' if had else ''}", case_id, "system",
              {"llm": info["model"], "backend": info["backend"],
               "pipeline": "RAG -> extract facts -> judge on facts -> guard false-pass -> cite"})
    # --- đối chiếu chéo chống gian lận (deterministic, chạy trước AI) ---
    cc = run_crosscheck(case_id, c["text"])
    yield {"type": "crosscheck", **cc}
    meta = {"directorate": c.get("directorate") or ""}
    for i, rule in enumerate(rules):
        yield {"type": "progress", "rule": rule["id"], "title": rule["title_vi"], "i": i, "n": len(rules)}
        v = core.assess_rule(case_id, c["text"], rule, ruleset_id=rs["id"], meta=meta)
        store.save_ai_verdict(case_id, v)
        if v.get("guard") and v["guard"]["action"] in ("override", "flag"):
            store.log("Hệ thống", f"GUARD chặn false-pass {rule['id']}: {v['guard']['reason']} → {v['v']}",
                      case_id, "system", {"rule": rule["id"], "guard": v["guard"]})
        yield {"type": "verdict", **v}
    store.update_case(case_id, status="assessed", assessed_at=store.now(),
                      llm_model=info["model"], embed_backend=rag.EMBED_BACKEND)
    vs = store.get_verdicts(case_id)
    summary = {k: sum(1 for x in vs if x["ai_verdict"] == k) for k in core.VERDICTS}
    store.log(actor, f"Hoàn tất {len(rules)} kết luận NHÁP", case_id, "system",
              {"summary": summary, "mock": info["is_mock"]})
    yield {"type": "done", "summary": summary}


def run_crosscheck(case_id: str, text: str = None, actor: str = "Hệ thống") -> dict:
    """Đối chiếu chéo tài liệu trong hồ sơ; lưu vào case + ghi nhật ký."""
    c = _case(case_id) if text is None else None
    cc = crosscheck.run(text if text is not None else c["text"])
    store.update_case(case_id, crosscheck=json.dumps(cc, ensure_ascii=False))
    if cc["findings"]:
        store.log(actor, f"ĐỐI CHIẾU CHÉO {case_id}: {len(cc['findings'])} điểm KHÔNG KHỚP giữa {cc['n_docs']} tài liệu — "
                  + "; ".join(f.get("label_vi", f["category"]) for f in cc["findings"]),
                  case_id, "system", {"risk": cc["risk"], "findings": len(cc["findings"])})
    else:
        store.log(actor, f"Đối chiếu chéo {case_id}: {cc['n_docs']} tài liệu nhất quán", case_id, "system",
                  {"risk": cc["risk"]})
    return cc


def attach_document(case_id: str, name: str, text: str, officer: str, role="officer"):
    """Thêm tài liệu đính kèm (trước khi ký; kể cả khi đang Chờ bổ sung). Đánh giá AI cũ bị vô hiệu."""
    c = _case(case_id)
    if _rank(c["status"]) >= _rank("signed"):
        raise WorkflowError("Hồ sơ đã ký — muốn thêm tài liệu phải Mở lại hồ sơ trước.")
    text = text.replace("\r\n", "\n").strip()
    if len(text.split()) < 10:
        raise WorkflowError("Tài liệu đính kèm quá ngắn", 400)
    was_supp = c["status"] == "awaiting_supplement"
    new_text = c["text"].rstrip() + f"\n\n=== TÀI LIỆU: {name.strip()} ===\n\n" + text
    store.clear_verdicts(case_id)
    store.update_case(case_id, text=new_text, status="new", review_started_at=None, officer=None,
                      spot_rule=None, spot_answer=None, spot_ai=None)
    rnd = f" — hoàn tất vòng bổ sung #{c.get('supplement_round') or 0}" if was_supp else ""
    store.log(officer, f"Đính kèm tài liệu '{name}' ({len(text.split())} từ){rnd} — kết quả AI cũ bị vô hiệu, cần đánh giá lại",
              case_id, role, {"doc": name, "words": len(text.split()), "supplement_round": c.get("supplement_round") or 0})
    return run_crosscheck(case_id, new_text, actor=officer)


# ---------- vòng bổ sung hồ sơ (nhiều lần, có hạn) ----------
def request_supplement(case_id, items, days, officer, role="officer"):
    c = _case(case_id)
    if c["status"] not in ("in_review", "assessed"):
        raise WorkflowError("Chỉ yêu cầu bổ sung khi hồ sơ đang thẩm định / đã có kết quả AI")
    items = (items or "").strip()
    if len(items) < 8:
        raise WorkflowError("Ghi rõ danh mục cần bổ sung (≥ 8 ký tự)", 400)
    days = max(1, min(int(days or 15), 90))
    rnd = (c.get("supplement_round") or 0) + 1
    supp = {"round": rnd, "items": items, "requested_at": store.now(),
            "deadline": (date.today() + timedelta(days=days)).isoformat(), "by": officer}
    store.update_case(case_id, status="awaiting_supplement", supplement=json.dumps(supp, ensure_ascii=False),
                      supplement_round=rnd)
    store.log(officer, f"YÊU CẦU BỔ SUNG (vòng #{rnd}, hạn {supp['deadline']}): {items}", case_id, role, supp)
    return store.get_case(case_id, with_text=False)


# ---------- review ----------
def start_review(case_id, officer, role="officer", acknowledge_coi=False, coi_reason=""):
    c = _case(case_id)
    if not officer.strip():
        raise WorkflowError("Cần tên cán bộ thẩm định", 400)
    if c["status"] == "in_review":
        return c
    if c["status"] != "assessed":
        raise WorkflowError(f"Chưa thể thẩm định ở trạng thái '{STATE_VI[c['status']]}'")
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
    guarded = "[CHẶN FALSE-PASS]" in (v.get("note") or "") or "[NGHI FALSE-PASS]" in (v.get("note") or "")
    need_reason = changed or v["ai_verdict"] in ("unclear", "not_addressed") or final_verdict == "unclear"
    if need_reason and len(reason) < 8:
        raise WorkflowError("Tiêu chí này bắt buộc ghi lý do (≥ 8 ký tự) trước khi xác nhận", 400)
    if guarded and final_verdict == "met" and len(reason) < 8:
        raise WorkflowError("Tiêu chí này bị guard cảnh báo vi phạm — chọn ĐẠT phải ghi rõ lý do bác cảnh báo", 400)
    # --- KHÔNG ĐỂ AI MỘT MÌNH Ở TIÊU CHÍ KHÔNG CÓ LƯỚI ĐỠ CODE ---
    # Tiêu chí 'needs-manual-guard' (logic dẫn xuất / nhánh thay thế / ngưỡng điều kiện) mà xác nhận ĐẠT
    # -> bắt buộc cán bộ TỰ CHỨNG THỰC bằng bằng chứng cụ thể, không được bấm qua theo AI.
    if v.get("guard_level") == "needs-manual-guard" and final_verdict == "met" and len(reason) < ATTESTATION_MIN_CHARS:
        raise WorkflowError(
            f"Tiêu chí {rule_id} KHÔNG có lưới đỡ mã nguồn (logic mã nguồn không diễn đạt nổi) — mọi kết luận ĐẠT ở "
            f"đây chỉ dựa vào AI. Bạn phải tự kiểm chứng và ghi rõ bằng chứng trong hồ sơ (≥ {ATTESTATION_MIN_CHARS} "
            "ký tự: trích số liệu/câu văn bạn đã đối chiếu). Nội dung này vào nhật ký kiểm toán.",
            400, {"need_attestation": True, "rule": rule_id, "min_chars": ATTESTATION_MIN_CHARS})
    store.confirm_verdict(case_id, rule_id, final_verdict, reason, officer)
    attested = v.get("guard_level") == "needs-manual-guard" and final_verdict == "met"
    store.log(officer, f"Xác nhận {rule_id} = {final_verdict}" + (f" (AI: {v['ai_verdict']} → SỬA)" if changed else "")
              + (" [TỰ CHỨNG THỰC — tiêu chí không có lưới đỡ mã nguồn]" if attested else "")
              + (f" — lý do: {reason}" if reason else ""), case_id, role,
              {"rule": rule_id, "ai": v["ai_verdict"], "final": final_verdict, "override": changed,
               "reason": reason, "guard_level": v.get("guard_level"), "attestation": attested})
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
    rules_by_id = {r["id"]: r for r in case_rules(c)}
    n_fb = feedback.record_signed_case(c, vs, rules_by_id, c.get("llm_model") or "", ruleset_id=c.get("ruleset_id") or "")
    if n_fb:
        store.log("Hệ thống", f"Feedback loop: lưu {n_fb} mẫu (cán bộ sửa AI / người quyết thay AI) vào kho ground-truth",
                  case_id, "system", {"n_samples": n_fb, "file": "data/feedback/feedback.jsonl"})
    rejection = any(v["final_verdict"] != "met" for v in vs)
    store.log(officer, f"KÝ DUYỆT kết quả thẩm định ({elapsed:.0f}s, {overrides} tiêu chí sửa so với AI"
              + (", ký nhanh — đã xác nhận cảnh báo" if fast else "") + ")"
              + (" — hồ sơ có tiêu chí không đạt/chưa rõ: CẦN QUẢN LÝ KÝ CẤP 2 trước khi phát hành thư" if rejection else ""),
              case_id, role, {"elapsed": round(elapsed), "overrides": overrides, "fast_ack": fast, "rejection": rejection})
    return store.get_case(case_id, with_text=False)


# ---------- ký xác nhận cấp 2 (quản lý) ----------
def countersign(case_id, manager, role="manager"):
    from .screening import norm
    c = _case(case_id)
    if c["status"] != "signed":
        raise WorkflowError("Chỉ ký cấp 2 sau khi cán bộ thẩm định đã ký duyệt")
    if c.get("countersigned_by"):
        raise WorkflowError(f"Hồ sơ đã được {c['countersigned_by']} ký cấp 2")
    if not needs_countersign(c):
        raise WorkflowError("Hồ sơ đạt toàn bộ tiêu chí — không cần ký cấp 2")
    if norm(manager) == norm(c.get("officer") or ""):
        raise WorkflowError("Người ký cấp 2 phải KHÁC cán bộ thẩm định", 400)
    if coi.role_of(manager) != "manager":
        raise WorkflowError(f"'{manager}' không có vai trò quản lý (manager) trong sổ cán bộ — không được ký cấp 2", 403)
    ci = coi.check(manager, c)
    if ci["level"] == "block":
        store.log("Hệ thống", f"CHẶN COI cấp 2: {manager} không được ký {case_id} — " + " | ".join(ci["reasons"]),
                  case_id, "system", {"coi": ci})
        raise WorkflowError("Xung đột lợi ích — quản lý này không được ký hồ sơ: " + " ".join(ci["reasons"]), 409)
    store.update_case(case_id, countersigned_by=manager, countersigned_at=store.now())
    store.log(manager, f"KÝ XÁC NHẬN CẤP QUẢN LÝ: đồng ý kết quả loại/yêu cầu bổ sung của {c.get('officer')} — "
              "cho phép phát hành thư kết quả", case_id, role, {"officer": c.get("officer")})
    return store.get_case(case_id, with_text=False)


def reopen(case_id, officer, reason, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("Hồ sơ chưa ký duyệt, không cần mở lại")
    if len((reason or "").strip()) < 8:
        raise WorkflowError("Mở lại hồ sơ bắt buộc ghi lý do (≥ 8 ký tự)", 400)
    store.update_case(case_id, status="in_review", signed_at=None, letter=None, letter_status=None,
                      letter_approved_at=None, review_started_at=store.now(),
                      countersigned_by=None, countersigned_at=None)
    store.log(officer, f"MỞ LẠI hồ sơ đã ký — lý do: {reason} (chữ ký cấp 2 cũ bị vô hiệu)", case_id, role,
              {"reason": reason})
    return store.get_case(case_id, with_text=False)


# ---------- letter ----------
def draft_letter(case_id, officer, role="officer"):
    c = _case(case_id)
    if _rank(c["status"]) < _rank("signed"):
        raise WorkflowError("Thư kết quả chỉ được soạn sau khi cán bộ ký duyệt 100% tiêu chí")
    vs = store.get_verdicts(case_id)
    if needs_countersign(c, vs):
        raise WorkflowError("Hồ sơ có tiêu chí KHÔNG ĐẠT / CHƯA RÕ — cần quản lý (role manager) ký xác nhận cấp 2 "
                            "trước khi phát hành thư kết quả.", 428, {"need_countersign": True})
    rules_by_id = {r["id"]: r for r in case_rules(c)}
    finals = [{"rule": v["rule_id"], "title": rules_by_id.get(v["rule_id"], {}).get("title_vi", v["rule_id"]),
               "rule_quote": v["rq"], "applicant_quote": v["aq"],
               "verdict": v["final_verdict"], "officer_reason": v["officer_reason"] or ""}
              for v in vs]
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
    """Trả case + verdicts + bộ tiêu chí đã khóa; CHE kết quả AI nếu spot-check đang chờ."""
    c = _case(case_id)
    vs = store.get_verdicts(case_id)
    masked = spot_pending(c)
    if masked:
        for v in vs:
            for k in ("ai_verdict", "ai_confidence", "facts", "aq", "note", "chunk_id", "retrieval", "needs_attention"):
                v[k] = None
    snap = c.get("ruleset_snapshot") or {}
    rules = snap.get("rules") or core.get_ruleset(c.get("ruleset_id"))["rules"]
    rs_meta = {"id": snap.get("id") or c.get("ruleset_id") or core.get_ruleset()["id"],
               "name": snap.get("name") or core.get_ruleset(c.get("ruleset_id"))["name"],
               "version": snap.get("version") or c.get("ruleset_version") or core.get_ruleset(c.get("ruleset_id"))["version"],
               "locked": bool(snap)}
    c.pop("ruleset_snapshot", None)  # đã trả qua rules, tránh payload đôi
    return {"case": c, "verdicts": vs, "ai_masked": masked, "rules": rules, "ruleset": rs_meta,
            "needs_countersign": needs_countersign(c, vs), "states": STATES, "state_vi": STATE_VI}
