"""app.py — GrantLens backend (FastAPI).
Chạy:  uvicorn backend.app:app --port 8000   (từ thư mục gốc dự án)
Mở:    http://localhost:8000
"""
import json
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from . import core, store, workflow, llm, rag, screening, tables, feedback, coi
from .llm import LLMError
from .workflow import WorkflowError

app = FastAPI(title="GrantLens", version="2.0")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.on_event("startup")
def seed():
    for c in core.load_manifest():
        if not store.case_exists(c["id"]):
            store.upsert_case({**c, "text": core.load_app_text(c)})
    if not store.audit_events(limit=1):
        store.log("Hệ thống", "Khởi tạo nhật ký kiểm toán (genesis)", None, "system")


@app.exception_handler(WorkflowError)
async def _wf(request, exc: WorkflowError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.code, content={"detail": str(exc), **exc.extra})


@app.exception_handler(LLMError)
async def _llm(request, exc: LLMError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/api/meta")
def meta():
    return {"rules": core.load_rules(), "llm": llm.describe(), "llm_health": llm.health(),
            "embed_backend": rag.EMBED_BACKEND, "states": workflow.STATES, "state_vi": workflow.STATE_VI,
            "min_seconds_per_rule": workflow.MIN_SECONDS_PER_RULE,
            "letter_template": bool(core.load_letter_template())}


@app.get("/api/stats")
def stats():
    return {**store.stats(), "feedback": feedback.stats()}


@app.get("/api/feedback/stats")
def feedback_stats():
    return feedback.stats()


@app.get("/api/officers")
def officers():
    return {"officers": [{"name": o["name"], "affiliations": o.get("affiliations", [])} for o in coi.load_officers()]}


# ---------------- cases ----------------
@app.get("/api/cases")
def cases():
    return {"cases": store.list_cases()}


@app.get("/api/cases/{case_id}")
def case(case_id: str):
    return workflow.case_view(case_id)


class NewCase(BaseModel):
    applicant: str
    org: str = ""
    directorate: str = ""
    text: str
    officer: str = "Cán bộ"


def _create_case(applicant, org, directorate, text, officer, filename=None):
    text = text.replace("\r\n", "\n").strip()
    if len(text.split()) < 40:
        raise HTTPException(400, "Văn bản hồ sơ quá ngắn (< 40 từ)")
    cid = store.next_upload_id()
    store.upsert_case({"id": cid, "applicant": applicant.strip() or cid, "org": org, "directorate": directorate,
                       "scenario": "Hồ sơ tải lên" + (f" ({filename})" if filename else ""), "tags": ["upload"],
                       "source": "upload", "text": text})
    store.log(officer, f"Tải lên hồ sơ mới {cid} — {applicant} ({len(text.split())} từ)", cid, "officer",
              {"filename": filename, "words": len(text.split())})
    try:
        _screen(cid, "Hệ thống")
    except Exception:  # sàng lọc lỗi không được chặn việc tạo hồ sơ
        pass
    return store.get_case(cid, with_text=False)


@app.post("/api/cases")
def create_case(req: NewCase):
    return _create_case(req.applicant, req.org, req.directorate, req.text, req.officer)


@app.post("/api/cases/upload")
async def upload_case(file: UploadFile = File(...), applicant: str = Form(""), org: str = Form(""),
                      directorate: str = Form(""), officer: str = Form("Cán bộ")):
    try:
        meta = tables.file_to_text(file.filename, await file.read())
    except Exception as e:
        raise HTTPException(400, f"Không đọc được file {file.filename}: {e}")
    c = _create_case(applicant or Path(file.filename).stem, org, directorate, meta["text"], officer, file.filename)
    if meta["n_tables"] or meta["scanned_pages"]:
        store.log("Hệ thống", f"Phân tích layout {file.filename}: {meta['n_tables']} bảng giữ nguyên cấu trúc"
                  + (f", {len(meta['scanned_pages'])} trang scan CẦN OCR (trang {meta['scanned_pages']})" if meta["scanned_pages"] else "")
                  + (", đã OCR dự phòng" if meta["ocr_used"] else ""), c["id"], "system", meta | {"text": None})
    return {**c, "ingest": {k: v for k, v in meta.items() if k != "text"}}


# ---------------- AI assessment (streaming NDJSON) ----------------
@app.post("/api/cases/{case_id}/assess")
def assess(case_id: str):
    workflow.can_assess(case_id)

    def gen():
        try:
            for ev in workflow.assess_stream(case_id):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except LLMError as e:
            store.log("Hệ thống", f"LỖI LLM khi đánh giá: {e}", case_id, "system")
            yield json.dumps({"type": "error", "detail": str(e)}, ensure_ascii=False) + "\n"
        except WorkflowError as e:
            yield json.dumps({"type": "error", "detail": str(e)}, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------------- review workflow ----------------
class Officer(BaseModel):
    officer: str
    role: str = "officer"


class StartReviewReq(Officer):
    acknowledge_coi: bool = False
    coi_reason: str = ""


@app.post("/api/cases/{case_id}/start-review")
def start_review(case_id: str, req: StartReviewReq):
    workflow.start_review(case_id, req.officer, req.role, req.acknowledge_coi, req.coi_reason)
    return workflow.case_view(case_id)


class SpotReq(Officer):
    verdict: str


@app.post("/api/cases/{case_id}/spot-check")
def spot(case_id: str, req: SpotReq):
    res = workflow.spot_check(case_id, req.verdict, req.officer, req.role)
    return {"result": res, **workflow.case_view(case_id)}


class ConfirmReq(Officer):
    rule_id: str
    verdict: str
    reason: str = ""


@app.post("/api/cases/{case_id}/confirm")
def confirm(case_id: str, req: ConfirmReq):
    return workflow.confirm(case_id, req.rule_id, req.verdict, req.reason, req.officer, req.role)


class RuleReq(Officer):
    rule_id: str


@app.post("/api/cases/{case_id}/unconfirm")
def unconfirm(case_id: str, req: RuleReq):
    workflow.unconfirm(case_id, req.rule_id, req.officer, req.role)
    return {"ok": True}


class SignReq(Officer):
    acknowledge_fast: bool = False


@app.post("/api/cases/{case_id}/sign")
def sign(case_id: str, req: SignReq):
    return workflow.sign(case_id, req.officer, req.acknowledge_fast, req.role)


class ReopenReq(Officer):
    reason: str


@app.post("/api/cases/{case_id}/reopen")
def reopen(case_id: str, req: ReopenReq):
    return workflow.reopen(case_id, req.officer, req.reason, req.role)


@app.post("/api/cases/{case_id}/letter")
def letter(case_id: str, req: Officer):
    return {"letter": workflow.draft_letter(case_id, req.officer, req.role)}


class ApproveReq(Officer):
    text: Optional[str] = None


@app.post("/api/cases/{case_id}/letter/approve")
def approve_letter(case_id: str, req: ApproveReq):
    return workflow.approve_letter(case_id, req.officer, req.text, req.role)


# ---------------- bias lab ----------------
class BiasReq(BaseModel):
    a: str
    b: str


@app.post("/api/bias/compare")
def bias_compare(req: BiasReq):
    va = {v["rule_id"]: v for v in store.get_verdicts(req.a)}
    vb = {v["rule_id"]: v for v in store.get_verdicts(req.b)}
    if not va or not vb:
        raise HTTPException(409, "Cả hai hồ sơ phải được AI đánh giá trước (mở từng hồ sơ → Chạy đánh giá AI)")
    rows = [{"rule": r["id"], "title": r["title_vi"], "a": va[r["id"]]["ai_verdict"], "b": vb[r["id"]]["ai_verdict"],
             "a_conf": va[r["id"]]["ai_confidence"], "b_conf": vb[r["id"]]["ai_confidence"],
             "match": va[r["id"]]["ai_verdict"] == vb[r["id"]]["ai_verdict"]} for r in core.load_rules()]
    match = sum(1 for r in rows if r["match"])
    store.log("Hệ thống", f"Bias test {req.a} vs {req.b}: {match}/{len(rows)} tiêu chí trùng", None, "system",
              {"a": req.a, "b": req.b, "match": match})
    return {"rows": rows, "match": match, "total": len(rows)}


# ---------------- crosscheck (đối chiếu chéo chống gian lận) + đính kèm ----------------
@app.post("/api/cases/{case_id}/crosscheck")
def crosscheck_run(case_id: str, req: Officer):
    return {"crosscheck": workflow.run_crosscheck(case_id, actor=req.officer)}


@app.post("/api/cases/{case_id}/attach")
async def attach(case_id: str, file: UploadFile = File(None), name: str = Form(""),
                 text: str = Form(""), officer: str = Form("Cán bộ")):
    if file is not None:
        try:
            meta_doc = tables.file_to_text(file.filename, await file.read())
        except Exception as e:
            raise HTTPException(400, f"Không đọc được file {file.filename}: {e}")
        doc_text, doc_name = meta_doc["text"], name or file.filename
    else:
        doc_text, doc_name = text, name or "Tài liệu đính kèm"
    cc = workflow.attach_document(case_id, doc_name, doc_text, officer)
    return {"crosscheck": cc, **workflow.case_view(case_id)}


# ---------------- screening (đối chiếu danh sách cấm + ABN) ----------------
def _screen(case_id: str, actor: str) -> dict:
    c = store.get_case(case_id, with_text=False)
    if not c:
        raise HTTPException(404, "Không có hồ sơ này")
    res = screening.screen_case(c.get("applicant") or "", c.get("org") or "")
    store.update_case(case_id, screening=json.dumps(res, ensure_ascii=False))
    lbl = {"clear": "không có hit", "note_expired": "chỉ hit đã hết hiệu lực",
           "review": "CÓ HIT — cần cán bộ xem", "review_strong": "HIT KHỚP MẠNH — cần cán bộ xem"}[res["risk"]]
    store.log(actor, f"Sàng lọc danh sách cấm {case_id}: {res['n_hits']} hit → {lbl}", case_id, "system",
              {"risk": res["risk"], "n_hits": res["n_hits"]})
    return res


@app.post("/api/cases/{case_id}/screen")
def screen(case_id: str, req: Officer):
    return {"screening": _screen(case_id, req.officer)}


@app.get("/api/screening/status")
def screening_status():
    return {"asic": len(screening.load_asic()), "dfat": len(screening.load_dfat()),
            "abn": screening.abn_status(), "thresholds": {"strong": screening.STRONG, "possible": screening.WEAK}}


class LookupReq(BaseModel):
    name: str


@app.post("/api/screening/lookup")
def screening_lookup(req: LookupReq):
    if len(req.name.strip()) < 4:
        raise HTTPException(400, "Tên tra cứu quá ngắn")
    return {"asic": screening.match_asic(req.name), "dfat": screening.match_dfat(req.name),
            "abn": screening.abn_lookup(req.name)}


# ---------------- audit ----------------
@app.get("/api/audit")
def audit(case_id: Optional[str] = None, limit: int = 300):
    return {"events": store.audit_events(case_id, limit), "chain": store.verify_chain()}


# ---------------- letter template: xem / sinh từ bộ quy tắc / lưu ----------------
LETTER_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "data" / "letter-template.txt"


@app.get("/api/letter-template")
def get_letter_template():
    return {"template": core.load_letter_template(), "active": bool(core.load_letter_template())}


class TemplateGenReq(BaseModel):
    rules_text: str


@app.post("/api/letter-template/generate")
def generate_template(req: TemplateGenReq):
    if len(req.rules_text.split()) < 20:
        raise HTTPException(400, "Bộ quy tắc quá ngắn (< 20 từ)")
    tpl = core.generate_letter_template(req.rules_text)
    store.log("AI", f"Dịch ngược bộ quy tắc ({len(req.rules_text.split())} từ) thành BẢN NHÁP mẫu thư — chờ cán bộ rà và lưu",
              None, "system", {"words_rules": len(req.rules_text.split()), "words_template": len(tpl.split())})
    return {"template": tpl, "note": "Bản nháp — cán bộ rà/sửa rồi bấm Lưu mới có hiệu lực"}


class TemplateSaveReq(Officer):
    template: str


@app.post("/api/letter-template")
def save_template(req: TemplateSaveReq):
    tpl = req.template.strip()
    if len(tpl.split()) < 20:
        raise HTTPException(400, "Mẫu thư quá ngắn")
    LETTER_TEMPLATE_PATH.write_text(f"[Mẫu thư đơn vị — lưu bởi {req.officer} {store.now()}]\n{tpl}\n", encoding="utf-8")
    store.log(req.officer, "LƯU mẫu thư đơn vị mới — mọi thư kết quả từ giờ soạn theo mẫu này", None, req.role,
              {"words": len(tpl.split())})
    return {"ok": True, "active": True}


# ---------------- ingest: rule extraction from a new guideline ----------------
class GuidelineReq(BaseModel):
    text: str


@app.post("/api/rules/extract")
def rules_extract(req: GuidelineReq):
    if len(req.text.split()) < 50:
        raise HTTPException(400, "Văn bản hướng dẫn quá ngắn")
    rules = core.extract_rules(req.text)
    store.log("AI", f"Trích {len(rules)} tiêu chí ứng viên từ hướng dẫn mới ({len(req.text.split())} từ)", None, "system")
    return {"rules": rules, "note": "Bản xem trước — tiêu chí chỉ có hiệu lực sau khi cán bộ rà soát và đưa vào rules.json"}
