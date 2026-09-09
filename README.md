# GrantLens v2 — Trợ lý thẩm định điều kiện hồ sơ tài trợ
**AI đưa bằng chứng — con người quyết định.** Lõi AI: Qwen3-8B chạy local qua Ollama (dữ liệu không rời máy, không phí API); có thể đổi sang endpoint OpenAI-compatible cho khách có hạ tầng riêng.

## Kiến trúc
```
frontend/index.html   Ứng dụng web 1 trang: Tổng quan · Hồ sơ · Bộ tiêu chí · Bias Lab · Nhật ký · Nạp dữ liệu
backend/
  app.py       FastAPI — REST + streaming NDJSON tiến độ đánh giá
  workflow.py  Máy trạng thái ca hồ sơ + quy tắc "trust twist" kiểm tra Ở SERVER
  core.py      Pipeline 4 lớp: RAG → EXTRACT (LLM lượt 1) → JUDGE (LLM lượt 2) → CITE (code)
  store.py     SQLite: ca hồ sơ, kết luận, nhật ký kiểm toán chuỗi hash SHA-256 (tamper-evident)
  llm.py       Backend LLM: ollama (mặc định) | openai-compatible | mock (kiểm thử không cần GPU)
  rag.py       Chunk theo đoạn (giữ offset) + TF-IDF (mặc định) / BGE-M3+FAISS (tùy chọn)
  verify.py    Citation-by-retrieval: code cắt câu nguyên văn + string-match lớp 2
  eval.py      Chấm accuracy / citation / bias / false-pass theo ground truth
data/
  guideline/   NSF 22-586 CAREER nguyên văn + rules.json (12 tiêu chí)
  applications/ 15 hồ sơ synthetic có nhãn (manifest.json) — 2 cặp bias-test
  labels/      ground-truth.json
  grantlens.db SQLite tự tạo khi chạy (xoá file = reset dữ liệu demo)
```

## Luồng nghiệp vụ (trạng thái ca hồ sơ)
`new → assessed → in_review → signed → letter_drafted → letter_approved` (có `reopen` kèm lý do)

| Bước | Ai | Cơ chế bắt buộc (server kiểm tra, frontend không thể lách) |
|---|---|---|
| Chạy đánh giá AI | AI | 12 tiêu chí × 2 lượt gọi; kết quả gắn nhãn NHÁP, có confidence |
| Bắt đầu thẩm định | Cán bộ | **Spot-check mù**: hệ thống chọn ngẫu nhiên 1 tiêu chí, che toàn bộ kết quả AI cho tới khi cán bộ tự kết luận |
| Xác nhận từng tiêu chí | Cán bộ | Không có "duyệt tất cả"; sửa AI hoặc AI CHƯA RÕ/KHÔNG ĐỀ CẬP ⇒ bắt buộc lý do; kết luận cuối không được là "không đề cập" |
| Ký duyệt | Cán bộ | Chỉ khi 12/12 đã xác nhận; ký < 8s/tiêu chí ⇒ cảnh báo, ghi nhật ký, phải xác nhận lần 2 |
| Soạn thư | AI | Chỉ sau khi ký; tiếng Anh B1, trích dẫn hai chiều nguyên văn, mục bổ sung + khiếu nại 30 ngày |
| Phê duyệt thư | Cán bộ | Có thể sửa tay (nhật ký ghi "có chỉnh sửa"); in/lưu PDF |

## Sàng lọc danh sách cấm (denied-party screening)
Dữ liệu tham chiếu công khai của Úc trong `data/external/` (xem README ở đó): ASIC Banned & Disqualified (7.212 cá nhân, lọc theo ngày hiệu lực), DFAT Consolidated List (cấm vận, 3.906 đối tượng gộp alias), ABR/ABN (~20,5 triệu doanh nghiệp).
- Tự chạy khi tải hồ sơ mới; nút "Chạy sàng lọc / Chạy lại" trong màn hồ sơ; tra tay ở tab Nạp dữ liệu.
- Khớp mờ không dấu, chịu đảo thứ tự tên và sai chính tả (ngưỡng: ≥90% khớp mạnh, ≥78% nghi ngờ); hit chỉ là bằng chứng — cán bộ quyết, không tự loại hồ sơ; kết quả ghi nhật ký.
- Tra ABN cần lập chỉ mục một lần: `python -m backend.abn_index` (stream từ zip, không giải nén 12 GB; ra `data/external/abn/abn.sqlite`).

## Bổ sung theo phản hồi khách (09/2026)
- **Bảng biểu trong PDF/DOCX** (`backend/tables.py`): phân tích layout, bảng giữ nguyên hàng–cột thành khối `[BẢNG n]` — RAG không cắt đôi bảng; trang scan gắn cờ cần OCR (tự OCR nếu cài pytesseract).
- **Feedback loop** (`backend/feedback.py`): lúc ký duyệt, tiêu chí bị cán bộ sửa / phải quyết thay AI → `data/feedback/feedback.jsonl`; tự bơm few-shot vào lượt phán quyết (tắt: `GRANTLENS_FEEDBACK=off`); xuất fine-tune: `python -m backend.feedback export`.
- **Xung đột lợi ích** (`backend/coi.py` + `data/officers.json`): trước khi nhận thẩm định — trùng tổ chức ⇒ chặn; trùng tên / chưa khai báo ⇒ cảnh báo + bắt ghi lý do, ghi nhật ký.
- **Đối chiếu chéo chống gian lận** (`backend/crosscheck.py`): hồ sơ nhiều tài liệu (marker `=== TÀI LIỆU: tên ===`, API `/attach`); mã nguồn so số tiền / năm bằng cấp / số trang giữa các tài liệu + ngày vô lý; lệch → panel "KHÔNG KHỚP — nghi vấn" kèm trích ngữ cảnh. Test case HS-14 (khai $480k, báo cáo $1,52tr). Tự chạy khi đánh giá / đính kèm.
- **Thư theo mẫu đơn vị**: đặt mẫu vào `data/letter-template.txt` (dòng đầu `[ghi chú]` bị bỏ qua) — thư sinh ra theo đúng cấu trúc mẫu; xoá file để về khung mặc định.

## Cập nhật theo phản hồi khách đợt 2 (09/2026)
- **Multi-GO + versioning** (`data/rulesets/`): mỗi đợt/quỹ tài trợ một bộ tiêu chí riêng có version; hồ sơ **KHÓA snapshot bộ tiêu chí** tại lần đánh giá đầu (đổi rule sau không ảnh hưởng hồ sơ đang xử lý). API: GET/POST `/api/rulesets`, POST `/api/cases/{id}/ruleset`. Trích rule từ guideline mới → rà → lưu thành bộ mới ngay trên UI.
- **Guard chặn false-pass** (`backend/guards.py`): rule định lượng (ngân sách theo directorate, số trang, %, co-PI, số lần dự thi...) được mã nguồn kiểm lại sau LLM — LLM nói "met" mà số liệu vi phạm ⇒ ghi đè not_met (định lượng) hoặc hạ unclear (định tính), gắn nhãn, ghi nhật ký; cán bộ muốn vẫn cho ĐẠT phải ghi lý do bác cảnh báo.
- **Ký cấp 2**: hồ sơ có tiêu chí không đạt/chưa rõ ⇒ thư chỉ phát hành sau khi một **manager** (data/officers.json, khác người thẩm định, qua kiểm COI) ký xác nhận. Ngưỡng chống ký nhanh nâng 8→15s/tiêu chí (`GRANTLENS_MIN_SECONDS_PER_RULE`).
- **Vòng bổ sung hồ sơ**: trạng thái mới `awaiting_supplement` — cán bộ ghi danh mục cần bổ sung + hạn; đính kèm tài liệu là quay lại đánh giá, đếm số vòng. **SLA** theo trạng thái (data/officers.json `sla_days`) — quá hạn UI cảnh báo đỏ.

## Sửa theo phê bình kỹ thuật đợt 3 (09/2026) — giới hạn của guard được nói thẳng
- **Compiler biết tự thú nhận** (`guards.compile_rule_guards`): rule có *logic dẫn xuất* ("in excess of any such sales used to meet…" — W04), *nhánh thay thế* ("and/or their related entity" — W06) hoặc *ngưỡng theo điều kiện* (NSF R11: $400k chung / $500k BIO-ENG-OPP) được đánh dấu `uncompilable` kèm lý do, thay vì im lặng bỏ qua. Ngưỡng điều kiện: compiler chỉ dùng ngưỡng **an toàn nhất** để không báo oan, và ghi rõ là cần guard tay.
- **Độ phủ 3 mức** (`guards.coverage`, hiện trên UI + API): `code-guarded` / **`needs-manual-guard`** (có logic định lượng mà code không diễn đạt nổi — chỗ false-pass dễ lọt nhất) / `llm-only`. Kèm danh sách `partial_rules` = rule đã có guard tay nhưng vẫn còn khoảng trống.
- **Guard tay + compiler chạy SONG SONG** (trước đây có tay là tắt compiler → regex trượt wording là mất lưới, đúng ca W06 `does **not** own` có markdown chen giữa). Quy tắc hợp nhất: cả hai bắt → `not_met`; chỉ compiler bắt trên rule đã có tay → hạ `unclear` (cảnh báo, không ghi đè). Thêm `guards.normalise()` bỏ markdown/dấu nháy cong trước khi khớp.
- **Casegen chống nhiễu** (`casegen.py`): thêm **verifier độc lập** (lượt LLM riêng, không thấy nhãn) kiểm từng rule phụ có bằng chứng rõ không → rule "yếu" bị **loại khỏi metric**; eval **tách `[MỤC TIÊU]` (thước đo thật) khỏi `[PHỤ]` (tham khảo)**.
- **Nhãn phải qua phê chuẩn**: bộ sinh mặc định `approved=false`; eval in cảnh báo và ghi `provisional: true`. Có `--approve` (ghi tên người duyệt) và `--dispute` (loại case nhãn sai khỏi metric, giữ để truy vết). Ví dụ thật: case Wine W06 đã bị dispute vì nhãn sai — rule cho phép related entity, AI chấm đúng.
- **Số Wine chính thức (nhãn ĐÃ phê chuẩn, qwen3:8b, 09/2026)** — chạy đúng pipeline khách đề nghị (rà văn bản → dispute/flag → approve → `--eval` → chỉ trích metric MỤC TIÊU):

  ```
  [MỤC TIÊU] bắt đúng vi phạm cài sẵn : 3/3  = 100%
  [MỤC TIÊU] FALSE-PASS               : 0/3            <- chỉ số quan trọng nhất
  [THAM KHẢO] rule phụ có bằng chứng rõ: 31/31 (KHÔNG dùng làm thước đo)
  [LOẠI]      2/36 lượt (6%) nhãn phụ yếu đã gắn cờ; 1 case bị dispute
  ```

  Số Wine cũ (41/45, FP 1/4) **đã lỗi thời và chưa bao giờ là chuẩn vàng** — khi đó nhãn `approved=false` và chứa 1 case sai nhãn (W06/related entity) + 2 nhãn phụ sai (W04 ở case doanh số đúng bằng ngưỡng nên không thể có phần vượt). Chỉ trích dẫn khối số ở trên.

**Ranh giới trung thực để nói với khách:** số false-pass chỉ có giá trị với **đúng quỹ đã đo bằng bộ test đã phê chuẩn nhãn**; quỹ mới chỉ được đảm bảo lớp compiler cơ bản, và các rule `needs-manual-guard` vẫn cần guard tay + cán bộ.

## Sửa rủi ro còn lại (đợt 4, 09/2026) — không để AI một mình ở chỗ không có lưới đỡ
- **Ép ma sát ở tầng server, không chỉ cảnh báo trên UI** (phê bình: "khai báo trên UI không chặn được false-pass"). Mỗi kết luận lưu kèm `guard_level`. Tiêu chí `needs-manual-guard` mà AI nói ĐẠT thì **luôn bị kéo vào diện cần chú ý**, và khi cán bộ xác nhận ĐẠT, server **bắt buộc ghi bằng chứng tự kiểm chứng ≥ 25 ký tự** (`GRANTLENS_ATTESTATION_MIN_CHARS`) — bấm qua theo AI là bị từ chối (HTTP 400, cờ `need_attestation`). Nội dung chứng thực vào nhật ký chuỗi băm mang tên cán bộ; Tổng quan có ô đếm số lần xác nhận ĐẠT ở tiêu chí không lưới đỡ để quản lý giám sát. Tiêu chí đã có lưới đỡ **không** bị làm phiền thêm.
- **Verifier không được tin tuyệt đối** (phê bình: "verifier cũng là LLM"): chạy **2 lượt độc lập** — chỉ loại nhãn khi *mọi* lượt nói không có bằng chứng; hai lượt mâu thuẫn → **giữ trong metric + gắn cờ `verifier_disagreement`** cho người rà (không loại oan). **Mã nguồn có quyền phủ quyết**: guard bắt vi phạm rõ ràng thì thắng phiếu LLM (đã kiểm: LLM khăng khăng "yes" nhưng code thấy $150k > trần $100k → vẫn loại nhãn).
- **Ưu tiên metric MỤC TIÊU**: eval in `[MỤC TIÊU]` (bắt vi phạm cài sẵn + false-pass) trong khung nổi bật, `[THAM KHẢO]` cho rule phụ kèm ghi chú không dùng để kết luận; cảnh báo khi tỷ lệ loại > 30% (`low_quality_testset`) và nhắc lại "tạm tính" nếu nhãn chưa phê chuẩn.

## Sửa rủi ro đợt 5 (09/2026) — ma sát theo tầng rủi ro + đóng backlog guard
- **`llm-only` giờ cũng bị ép ma sát** (trước chỉ `needs-manual-guard`): xác nhận ĐẠT ở tiêu chí thuần định tính phải kèm bằng chứng ≥ 15 ký tự **hoặc** bấm nút "xác nhận đúng câu trích dẫn AI đã cắt" (phải mở dòng ra mới bấm được, nhật ký ghi rõ loại `ai_quote_ack` để hậu kiểm đếm). Tiêu chí `code-guarded` **không** bị làm phiền thêm.
- **Chống "gõ đủ chữ cho qua cổng"** — kiểm bằng mã nguồn, không dùng LLM nên không có false positive ngữ nghĩa: lời chứng thực phải **chứa đoạn nguyên văn ≥ 5 từ liên tiếp có thật trong hồ sơ** (`verify.attestation_evidence`); và **không được dán trùng** lời chứng thực giữa các tiêu chí. Vùng `needs-manual-guard` **không cho mượn câu trích của AI** — phải tự dán.
- **Đóng backlog guard tay**: viết guard cho `W04` (logic 2 lớp "in excess of" — tính `min(cellar, total − ngưỡng) ≤ 0`) và `C06` (nhánh "board **or** CEO or equivalent"). Backlog ưu tiên **cao = 0** trên cả 5 bộ tiêu chí; API `GET /api/guard-backlog` liệt kê công khai phần còn lại theo mức ưu tiên.
- **Chuẩn hoá tiền tệ trước khi guard chạy** (lỗ hổng tự bắt được khi viết test hồi quy): guard chỉ đọc `$1,250,000`, hồ sơ ghi `1,250,000 AUD` / `AUD 1,250,000` / `A$1,250,000` / `... dollars` là lọt hết. Nay `guards.normalise()` quy mọi cách viết về một dạng, nên **mọi guard tay lẫn compiler cùng hưởng** thay vì phải sửa từng regex. Đã kiểm không đụng số thường (năm 2019 giữ nguyên) và không làm đổi bộ test Wine — số eval bên dưới vẫn còn giá trị.
- **Hai lỗi tự phát hiện khi rà và đã sửa**: (1) guard W04 bản đầu tính sai chiều logic (dùng `total − cellar` thay vì phần vượt ngưỡng) → sửa đúng câu chữ rule; (2) compiler báo oan trần grant $100k khi câu chỉ tình cờ chứa một từ khóa → ràng buộc `max` nay đòi **≥ 2 từ khóa** trong câu mới kết luận (`min` giữ 1 vì vốn đã an toàn).
- **Bộ test Wine đã qua pipeline đầy đủ**: rà từng case → `dispute` 1 case sai nhãn (W06/related entity) → `flag_weak` 2 nhãn phụ sai (W04 ở case W03 và W09, vì doanh số đúng bằng ngưỡng nên không thể có phần vượt) → `approve` → `--eval` lấy **metric MỤC TIÊU**.

## Điểm phương pháp
**Chống thiên lệch văn phong (2 lượt gọi thật):** lượt 1 chỉ trích *dữ kiện* trung tính (số liệu, ngày, directorate…) kèm chunk_id + key_phrase; lượt 2 phán quyết **chỉ nhìn danh sách dữ kiện**, không nhìn văn gốc ⇒ ngữ pháp/độ trôi chảy không thể ảnh hưởng. Kiểm chứng bằng 2 cặp HS-04A/B và HS-11A/B (Bias Lab + eval).

**Citation-by-retrieval:** model 8B không sinh quote. Model chỉ trỏ chunk + 3-8 từ khóa; code cắt *câu nguyên văn* khớp nhất và string-match lại với hồ sơ ⇒ trích dẫn đúng nguyên văn "by construction". UI bấm vào trích dẫn sẽ highlight ngay trong hồ sơ gốc.

**Nhật ký kiểm toán chuỗi hash:** mỗi sự kiện chứa SHA-256 của sự kiện trước; sửa/xoá một dòng làm chuỗi đứt và bị phát hiện (`GET /api/audit` trả `chain.ok`).

## Cài đặt & chạy
```bash
pip install -r requirements.txt
ollama pull qwen3:8b            # ~6GB VRAM; máy yếu: qwen3:8b-q4_K_M (đặt GRANTLENS_MODEL)
uvicorn backend.app:app --port 8000
# mở http://localhost:8000 — lần đầu nhập tên cán bộ thẩm định
```
Biến môi trường:
- `GRANTLENS_LLM=ollama|openai|mock` — `mock` để demo luồng nghiệp vụ không cần model (UI gắn nhãn rõ, không dùng báo cáo số liệu)
- `GRANTLENS_MODEL`, `OLLAMA_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- `EMBED_BACKEND=bge` (cần `pip install sentence-transformers faiss-cpu`)
- `GRANTLENS_DB` đường dẫn SQLite

## Đánh giá định lượng
```bash
python -m backend.eval                 # toàn bộ 15 hồ sơ (~180 lượt rule × 2 lượt LLM)
python -m backend.eval --only HS-02,HS-11A,HS-11B
GRANTLENS_MODEL=qwen3:14b python -m backend.eval   # so sánh 2 lõi AI trên cùng chỉ số
```
Chỉ số: accuracy (tổng / theo loại rule / theo confidence), confusion matrix, citation match (kỳ vọng 100%), bias score từng cặp (kỳ vọng 100%), **false pass** (not_met → met, kỳ vọng 0), failure cases kèm dữ kiện AI trích. Lưu `eval-results-<model>.json`.
### Kết quả thực đo (subset 4 hồ sơ × 12 tiêu chí, laptop RTX 3050 4GB)
| Chỉ số | qwen3:8b | qwen3:4b |
|---|---|---|
| Accuracy verdict | **89,6%** (43/48) | 83,3% (40/48) |
| — rule định lượng | 85,0% | 70,0% |
| Citation match | 100% | 100% |
| Bias score (HS-04A/B) | **12/12** | 12/12 |
| **False pass** (not_met→met) | **1/6** | 5/6 ❌ |
| Tốc độ | 50,6s/tiêu chí | 23,0s/tiêu chí |

Kết luận: dùng **8b** (4b nhanh gấp đôi nhưng bỏ lọt 5/6 lỗi trượt — không chấp nhận được).

**Sau khi thêm guard chặn false-pass (đợt 2):** đo lại HS-02 + HS-07 (chứa toàn bộ 6 nhãn not_met của subset) với qwen3:8b —
accuracy **24/24 = 100%**, citation 100%, **false-pass 0/6** (trước guard: 1/6). Chi tiết eval-results-qwen3_8b.json.


## Bộ dữ liệu (15 hồ sơ, mỗi hồ sơ nhắm 1 ca nghiệp vụ)
| Mã | Kịch bản | Tiêu chí không đạt / đặc biệt |
|---|---|---|
| HS-01 | Đạt rõ | — |
| HS-02 | Trượt rõ, tiếng Anh đơn giản | R04 R05 R08 R11 |
| HS-03 | Chưa rõ, cần người xử lý | R04 unclear, R07 not_addressed, R09 unclear |
| HS-04A/B | Cặp bias #1 | R11 ($380k) |
| HS-05 | Vượt 3 lần dự thi; community college | R07 |
| HS-06 | Tổ chức nước ngoài | R01 |
| HS-07 | BIO $450k < $500k; thư 3 trang | R09 R11 |
| HS-08 | Thuyết minh 18 trang, không có giáo dục; cost sharing | R10 R12 |
| HS-09 | Đã nhận CAREER; lĩnh vực nghiên cứu chưa rõ | R06; R03 unclear |
| HS-10 | Thiếu 4 mục | R07 R09 R11 R12 not_addressed |
| HS-11A/B | Cặp bias #2 (tiếng Anh kiểu Việt) | R04 (40%) R08 |
| HS-12 | Bảo tàng phi lợi nhuận, vị trí tương đương tenure-track, lần dự thi thứ 3 | đạt (edge) |
| HS-13 | Community college, tiếng Anh đơn giản, ngân sách đúng $400k | đạt |

Hồ sơ mới: tab **Nạp dữ liệu** (dán văn bản hoặc tải .txt/.docx/.pdf) → vào hàng đợi với mã `UP-xxx`. Hướng dẫn mới: dán solicitation → AI trích tiêu chí ứng viên (chỉ chép nguyên văn, câu không khớp bị gắn cờ) → cán bộ rà rồi đưa vào `rules.json`.

## Luồng demo cho khách (10 phút)
1. **Tổng quan** — hàng đợi, KPI: tỉ lệ cán bộ sửa AI, trích dẫn khớp, spot-check, chuỗi nhật ký toàn vẹn.
2. Mở **HS-02** → Chạy đánh giá AI (xem tiến độ từng tiêu chí) → Bắt đầu thẩm định → **spot-check mù** → mở R04: dữ kiện lượt 1, trích dẫn hai chiều, bấm để highlight trong hồ sơ → xác nhận từng dòng → thử ký nhanh (bị cảnh báo) → ký.
3. Mở **HS-03** — AI trả CHƯA RÕ/KHÔNG ĐỀ CẬP, hệ thống ép người quyết + ghi lý do.
4. Soạn **thư B1**, sửa tay, phê duyệt, in PDF.
5. **Bias Lab** HS-11A vs HS-11B → 12/12 trùng.
6. **Nhật ký** — mở "chi tiết" một sự kiện, chỉ hash chain.
7. **Nạp dữ liệu** — tải hồ sơ mới của khách, chạy ngay.

## Nguồn dữ liệu
- Guideline: NSF 22-586 CAREER, nguyên văn từ nsf.gov (US Government work, public domain).
- Hồ sơ: synthetic-with-ground-truth (hồ sơ thật không công khai), nhãn trong `data/labels/ground-truth.json`.
