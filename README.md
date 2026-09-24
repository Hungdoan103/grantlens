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
  auth.py      Đăng nhập + phiên ký HMAC + middleware ép danh tính từ phiên (không tin tên tự khai)
  i18n.py      Lớp hiển thị tiếng Anh: dịch JSON/DOM bằng từ điển (i18n_en.py); nguyên văn không bao giờ dịch
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
- **⚠ SỐ WINE 3/3 (09/09/2026) ĐÃ BỊ THU HỒI — đừng trích dẫn lại.** Khi mở rộng đo sang quỹ thứ hai, bộ lọc mới phát hiện **bộ test tự sinh bị LỘ ĐÁP ÁN**: văn bản thi có câu tự thú như `"...only $250,000 were generated from the physical cellar door, violating W08..."` (case `03-W08`), nhiều case khác nhắc thẳng mã rule `W01..W09`. Hồ sơ thật không bao giờ viết vậy — hệ thẩm định có thể chấm đúng vì **đọc được đáp án** chứ không phải vì suy luận, nên **1 trong 3 case tính điểm bị nhiễm** và con số 3/3 không dùng được. Bản nhiễm giữ lại để truy vết (nhãn + kết quả eval tại `data/labels/withdrawn/`, văn bản tại `data/applications/withdrawn/`), cờ `withdrawn: true` kèm lý do.
- **Lỗi thứ hai lộ ra khi mở rộng đo sang quỹ khác — bộ sinh không cài được vi phạm.** Sau khi cấm LLM tự bình luận, **7/7 hồ sơ "vi phạm" ở Cyber Skills + Female Founders thực chất hợp lệ hoàn toàn** (vd C07 ghi chi tiêu hợp lệ $620,000 ≥ ngưỡng $500,000; F01 ghi phụ nữ sở hữu 60%). Verifier gắn cờ đúng cả 7, nhưng nó cho thấy "dặn LLM hãy vi phạm rule X" là không đủ tin. `casegen` được thiết kế lại:
  1. **Vi phạm do mã nguồn chốt trước**: LLM chỉ đề xuất *một câu dữ kiện* vi phạm; câu đó phải được guard (mã nguồn) hoặc verifier xác nhận là vi phạm thật (lần thử sau được xem các câu đã bị loại, vì ở temperature 0 không đổi đề thì kết quả y hệt). Người viết chỉ đặt dấu `[[FACT]]`; **mã nguồn tự chèn nguyên văn câu đã chốt** — model nhỏ chép lại hay sai lệch. Với vi phạm định lượng, guard phải **vẫn bắt trên toàn văn**, chặn văn bản có con số khác đè lên câu vi phạm.
  2. **Chặn lộ đáp án bằng mã nguồn** (`_leak_check`, `_repair_meta`): câu nhắc mã rule hoặc tự bình luận (`violates`, `satisfies`, `meets all criteria`, `exceeds the minimum … requirement`, `as required`…) bị **mã nguồn xoá từng câu** (không bao giờ xoá câu vi phạm đã chốt), sau đó hồ sơ phải qua lại mọi chốt kiểm; các câu đã xoá được lưu vào nhãn để người rà xem. Người viết không được đưa mã rule ngay từ đầu.
  3. **Tách vai model**: `GRANTLENS_PLANNER_MODEL` chốt câu vi phạm, `GRANTLENS_CASEGEN_MODEL` viết hồ sơ, `GRANTLENS_VERIFIER_MODEL` kiểm nhãn. Đợt đo này: `qwen3:4b` chốt vi phạm (đầu ra JSON bị ép schema), **`gemma3:4b` viết + kiểm** (khác họ model), `qwen3:8b` là hệ bị đo. Nếu verifier cùng model với hệ bị đo, case nào Qwen "không nhìn ra" sẽ bị chính Qwen loại khỏi đề, số đo tự đẹp lên. **Hai phiếu hỏi hai cách** ("có thoả không?" / "có vi phạm không?") — phiếu cũ lặp một câu y hệt ở temperature 0 thì luôn trùng nhau, không phải phiếu độc lập.
  4. **Chốt "đây có phải hồ sơ không"** (`_doc_problems`, mã nguồn): đo thật ngày 14/09, `qwen3:4b` (bản 2507 chỉ-suy-nghĩ) bỏ qua `think=false` và trả nguyên đoạn độc thoại ("Okay, the user wants me to write…"). Câu vi phạm vẫn được chèn vào giữa, verifier vẫn xác nhận — **mọi chốt tự động cũ đều cho qua, chỉ bước người rà bắt được** (cả 3 case Cyber đợt đó bị huỷ). Nay chặn bằng mã nguồn: dấu hiệu độc thoại, độ dài bất thường, văn bản phải xoá quá 3 câu bình luận. Model viết văn bản tự do phải là model không có chế độ suy nghĩ.
  5. **Cờ không tự loại case**: người rà phải `--confirm` (vi phạm có thật) hoặc `--dispute` (nhãn sai); còn cờ thì `--approve` bị từ chối (API trả 409). Sửa nhãn sau khi duyệt → mất duyệt.
  6. **Đối chứng trên hồ sơ sạch**: rule mục tiêu còn được chấm trên hồ sơ hợp lệ để đo **báo động giả** — chỉ đo bắt vi phạm thì một hệ "từ chối tất cả" cũng đạt 100%.
  7. **`GET /api/measurement-status`** + bảng trên màn Bộ tiêu chí: từng quỹ đang đứng ở bước nào của chuỗi; kết quả eval phải khớp **đúng bộ nhãn đang phê chuẩn** (dấu thời gian sinh + duyệt), không khớp thì hiện "phải chạy lại eval"; dưới 10 case vi phạm thì gắn "cỡ mẫu nhỏ — bằng chứng sơ bộ".

### Đo theo từng quỹ (14/09/2026) — sổ rà nhãn

Mỗi quỹ đi riêng chuỗi `--gen --target-only` → rà từng hồ sơ (`--confirm` / `--confirm-control` / `--dispute` / `--flag-weak`) → `--approve` → `--eval`. Người rà: *Rà kỹ thuật (nhà cung cấp) — chưa thay cán bộ đơn vị*; lý do từng quyết định lưu trong `data/labels/generated-<quỹ>.json`.

| Quỹ | Sinh (vi phạm + sạch) | Case vi phạm dùng được | Bị người rà loại | Đối chứng (hồ sơ sạch) |
|---|---|---|---|---|
| Cyber Security Skills R2 | 4 + 1 | C01, C07 | C06 *(mơ hồ: CEO đã ký xác nhận)*, C08 *(mơ hồ: không rõ ai là người nộp)* | C01, C06 |
| Wine Tourism R8 | 4 + 1 | W03, W08 | W04 *(nhãn sai: logic "in excess of")*, W06 *(mơ hồ: chủ thể lệch)* | W06, W08 |
| Boosting Female Founders R1 | 3 + 1 | F01, F05 | F06 *(nhãn sai: chi phí dự án ≠ tiền xin)* | F01, F06 |
| On-farm Water | 3 + 1 *(O01 bị bỏ khi sinh)* | O06 | O05 *(mơ hồ: "mùa vụ bình thường")*, O07 *(nhãn sai: trần là tiền hoàn, không phải chi phí)* | O06 |
| NSF 22-586 CAREER | 3 + 1 *(R04 bị bỏ khi sinh)* | R08 | R05 *(nhãn sai: người có tenure không phải PI)*, R07 *(nhãn sai + lộ đáp án)* | R05 |
| Quỹ minh hoạ (demo) | 1 + 1 | A02 | — | A02 |

**Phát hiện đáng nói với khách:** 18 case vi phạm do AI sinh, **đã qua mọi chốt kiểm tự động**, vẫn có **9 case (50%) bị người rà loại** — 5 nhãn sai, 4 đáp án mơ hồ. Đề thi tự sinh **không dùng làm chuẩn được nếu không có người đọc từng hồ sơ**; mọi con số bên dưới chỉ đo trên phần đã qua rà. Cỡ mẫu còn lại 1–2 case vi phạm mỗi quỹ ⇒ **bằng chứng sơ bộ theo quỹ, không phải tỷ lệ thống kê**.

<!-- KET-QUA-DO -->
**Kết quả đo** — hệ bị đo `qwen3:8b`, chỉ trên nhãn đã qua rà + phê chuẩn (nguồn: `GET /api/measurement-status`):

| Quỹ | Bắt đúng vi phạm | False-pass | Đẩy về cán bộ (chưa rõ) | Hồ sơ sạch: đúng · báo động giả |
|---|---|---|---|---|
| NSF 22-586 CAREER (Mỹ) | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
| Wine Tourism R8 | 2/2 | 0/2 | 0 | 1/2 · 0/2 |
| Cyber Security Skills R2 | 2/2 | 0/2 | 0 | 2/2 · 0/2 |
| Boosting Female Founders R1 | 1/2 | 0/2 | 1 | 2/2 · 0/2 |
| On-farm Water | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
| Quỹ minh hoạ (demo) | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
<!-- /KET-QUA-DO -->

**Phát hiện từ lần đo 14/09 — TRƯỚC khi có chốt nhất quán (bảng trên là số mới nhất, xem mục Đợt 6 để so trước/sau):**
- **Tổng 6 quỹ:** bắt đúng **8/9** vi phạm cài sẵn · **false-pass 0/9** · 1 case đẩy về cán bộ · hồ sơ sạch đúng 8/9 · **báo động giả 1/9**.
- **Guard chặn false-pass thật của AI** (Female Founders F05): hồ sơ ghi "registered income tax exempt entity" nhưng `qwen3:8b` chấm **đạt**; guard tay F05 bắt, hạ `unclear` + cảnh báo `[NGHI FALSE-PASS]` → cán bộ quyết. Đây là case "đẩy về cán bộ" duy nhất.
- **Báo động giả** (Wine W06, hồ sơ sạch): hồ sơ ghi "operates a dedicated physical cellar door located on our property"; lượt phán quyết trả **`not_met`** với ghi chú "không có thông tin về việc sở hữu hoặc thuê" — thiếu thông tin lẽ ra là `unclear`. Prompt đã dặn đúng nhưng model không tuân, và **code chưa có chốt nhất quán** cho trường hợp này.
- **Backlog — chưa sửa, vì sửa là đổi hành vi sản phẩm ⇒ phải đo lại cả 6 quỹ + eval NSF 24/24:** (1) chốt mã nguồn cho lượt phán quyết: `not_met` mà judge trả `supporting_fact = 0` hoặc coverage của lượt trích ≠ `direct` ⇒ hạ `unclear` và đẩy về cán bộ; (2) `core.assess_rule` đang ép `supporting_fact = 0` thành 1 (`or 1`), nên vẫn trích dữ kiện số 1 làm "bằng chứng" khi judge tự nói không có dữ kiện hỗ trợ — sửa cùng (1).

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
- **Guard loại trừ (compiler) — sửa 3 lỗi lộ ra khi sinh bộ test cho Cyber Skills** (đối chiếu trên 18 hồ sơ mẫu × mọi rule: 23 lượt guard bắn trước = 23 sau, không thêm/mất/đổi):
  1. *Báo động giả*: hồ sơ hợp lệ ghi "is **neither** an individual **nor** an unincorporated association" bị gắn cờ vì compiler chỉ hiểu phủ định "not". Nay phủ định phải nằm **sát cụm từ** (trước hoặc sau, cùng mệnh đề) và hiểu neither/nor; ngược lại, câu "is an unincorporated association and has no board" không còn được tha chỉ vì trong câu có chữ "no".
  2. *Từ loại trừ một chữ* ("individual") chỉ tính khi hồ sơ **khai tư cách** ("is/as an individual", "individual applicant") — không bắt nhầm "individual mentoring".
  3. *Báo cáo độ phủ nói quá*: mục "an employer of 100 or more employees that has not complied with the Workplace Gender Equality Act" trước đây bị compiler **lặng lẽ bỏ qua** trong khi C08 vẫn hiện "có lưới đỡ code". Nay compiler ghi rõ khoảng trống (`loại trừ có điều kiện` / `loại trừ phức hợp`), và đã viết **guard tay C08**: ≥ 100 nhân viên **và** chưa tuân thủ WGEA → vi phạm; chưa tuân thủ nhưng không rõ số nhân viên → gắn cờ cho cán bộ. C08 và F05 chuyển sang "còn khoảng trống" trong báo cáo độ phủ — trung thực hơn, không phải kém đi.
- **Compiler trần tài trợ đem nhầm chi phí dự án ra so — lỗi sản phẩm lộ ra khi rà bộ test Female Founders**: ràng buộc trần (`max`) trước đây so *mọi* con số trong câu có đủ 2 từ khoá của rule, nên câu hồ sơ **hợp lệ** "xin $450,000, bằng 50% chi phí dự án ước tính $900,000" bị đem $900,000 so với trần tài trợ $480,000 (câu có "estimated" và "grant"). Ở F06 có guard tay nên chỉ hạ "chưa rõ"; nhưng **với quỹ mới chưa có guard tay, hồ sơ hợp lệ bị ghi đè thành "không đạt"** (đã mô phỏng xác nhận). Nay `max` chỉ so con số có ≥ 2 từ khoá **đứng sát trước nó** (60 ký tự, không vượt qua con số tiền trước đó). Hồi quy: 4 câu hợp lệ không còn bị bắt (kể cả mô phỏng quỹ mới), 2 câu vi phạm thật vẫn bị bắt, 18 hồ sơ mẫu 23 lượt bắn trước = 23 sau.
- **Hai lỗi tự phát hiện khi rà và đã sửa**: (1) guard W04 bản đầu tính sai chiều logic (dùng `total − cellar` thay vì phần vượt ngưỡng) → sửa đúng câu chữ rule; (2) compiler báo oan trần grant $100k khi câu chỉ tình cờ chứa một từ khóa → ràng buộc `max` nay đòi **≥ 2 từ khóa** trong câu mới kết luận (`min` giữ 1 vì vốn đã an toàn).
- **Bộ test Wine đã qua pipeline đầy đủ**: rà từng case → `dispute` 1 case sai nhãn (W06/related entity) → `flag_weak` 2 nhãn phụ sai (W04 ở case W03 và W09, vì doanh số đúng bằng ngưỡng nên không thể có phần vượt) → `approve` → `--eval` lấy **metric MỤC TIÊU**.

## Đợt 6 (09/2026) — đóng các lỗ cấu trúc: xác thực, phân quyền, hậu kiểm, chốt nhất quán
Bối cảnh: cam kết của sản phẩm chuyển từ **thống kê** ("false-pass dưới x%" — không chứng minh nổi khi chưa có hồ sơ thật) sang **cấu trúc** ("không có kết luận nào đi qua mà không có người ký tên" — kiểm được trên một hồ sơ). Muốn cam kết cấu trúc đứng vững thì các lỗ dưới đây phải đóng.
- **Đăng nhập + phiên phía máy chủ** (`backend/auth.py`, chỉ dùng thư viện chuẩn): mật khẩu PBKDF2-SHA256 240.000 vòng có muối; phiên là cookie `httpOnly` + `SameSite=Strict` ký HMAC-SHA256, hết hạn 8 giờ; sai 5 lần khoá tài khoản 5 phút; thông báo lỗi và thời gian phản hồi không lộ tên nào tồn tại. Vai trò **đọc lại từ sổ cán bộ ở mỗi request** nên hạ quyền có hiệu lực ngay.
- **Không endpoint nào còn tin danh tính tự khai**: `IdentityMiddleware` (ASGI) chặn mọi `/api/*` chưa đăng nhập (401) và **ghi đè** trường `officer`/`role` trong mọi request JSON bằng danh tính của phiên — kể cả endpoint viết sau này quên kiểm. Endpoint multipart (tải hồ sơ, đính kèm) đọc từ `request.state.user`. Nhật ký chuỗi băm vì thế ghi **danh tính đã xác thực**, không còn là tên gõ vào.
- **Bốn cổng khoá vai trò**: phê chuẩn bộ tiêu chí, **phê chuẩn nhãn bộ test** (trước đây ai cũng gọi được), ký cấp 2 → `manager`; rút mẫu và ghi hậu kiểm → `manager` hoặc `auditor`. Ba vai trò: `officer` · `manager` · `auditor` (chỉ hậu kiểm, không thẩm định, không ký).
- **Tách nhiệm vụ ở bộ nhãn**: người đã loại case (rà nhãn) qua API **không được tự phê chuẩn** bộ nhãn đó.
- **Hậu kiểm lấy mẫu** (`GET /api/post-audit/sample`, `POST /api/post-audit`, bảng trên Tổng quan): quản lý/thanh tra rút ngẫu nhiên các lần cán bộ xác nhận **ĐẠT ở tiêu chí không có lưới đỡ mã nguồn** trên hồ sơ đã ký, không tự kiểm việc của mình; không đồng ý phải ghi lý do; **cả việc rút mẫu cũng vào nhật ký** (không rút đi rút lại tới khi gặp mẫu dễ). Lấp đúng rủi ro còn lại đã công bố: máy kiểm được đoạn chứng thực *có thật trong hồ sơ*, không kiểm được nó *đúng tiêu chí*; và hồ sơ đạt toàn bộ vốn không qua ký cấp 2.
- **Chốt nhất quán lượt phán quyết** (`core.judge_consistency`): "KHÔNG ĐẠT" phải trỏ được vào một dữ kiện nêu vi phạm; model trả `not_met` mà `supporting_fact = 0`/ngoài phạm vi, hoặc lượt trích báo `coverage = none` → hạ **CHƯA RÕ**, đẩy về cán bộ. Sửa luôn lỗi `supporting_fact or 1` (ép 0 thành 1 rồi trích dữ kiện số 1 làm "bằng chứng"). Nguồn gốc: ca báo động giả Wine W06 đo ngày 14/09. **Prompt phán quyết giữ nguyên**: đã thử thêm 2 câu vào prompt, đo đối chứng thấy kết quả gộp y hệt bản không thêm → bỏ, vì thay đổi không có tác dụng đo được thì không giữ.
- **Kiểm thử**: `test_auth_e2e` 39/39 (chưa đăng nhập → 401; cookie sửa nội dung → 401; tên tự khai bị ghi đè; cán bộ thường bị 403 ở cả 4 cổng; hậu kiểm; ký cấp 2; chốt nhất quán; tách nhiệm vụ; chuỗi băm toàn vẹn; không lộ hash mật khẩu) · guard 42/42 · casegen 66/66 · giao diện thật chạy qua Playwright, không lỗi JS.
- `GRANTLENS_AUTH=off` tắt xác thực **chỉ cho kiểm thử tự động**; giao diện hiện cảnh báo đỏ khi tắt.

### Đo lại sau khi thêm chốt nhất quán (19/09/2026, qwen3:8b, cùng bộ nhãn đã phê chuẩn)
| | Trước | Sau (chỉ chốt mã nguồn) |
|---|---|---|
| 6 quỹ — bắt đúng vi phạm | 8/9 | 8/9 |
| 6 quỹ — false-pass | 0/9 | 0/9 |
| 6 quỹ — đẩy về cán bộ | 1 (F05, guard chặn) | 1 (F05) |
| 6 quỹ — **báo động giả trên hồ sơ sạch** | **1/9** (W06) | **0/9** — W06 nay là CHƯA RÕ kèm nhãn `[CHỐT NHẤT QUÁN]` |
| NSF HS-02 + HS-07 — accuracy | 24/24 (đo 07/09) | **23/24** |
| NSF — false-pass | 0/6 | 0/6 |

AI **vẫn sai** ở W06 (vẫn kết luận "không đạt"); cái thay đổi là lỗi đó không còn đi thẳng thành kết luận mà bị mã nguồn hạ xuống CHƯA RÕ và đẩy về cán bộ.

**⚠ Phát hiện quan trọng — hệ KHÔNG tái lập y hệt giữa các lần chạy.** Bộ NSF tụt 24/24 → 23/24 (HS-07 R07: AI trích "this is her second submission" rồi phán sai rằng 2 lần vượt giới hạn 3 lần). Đã loại trừ lần lượt: model cùng digest (bản 03/09), cùng phiên bản Ollama, prompt giống hệt (đo cả hai bản prompt đều 23/24), bộ tiêu chí và hồ sơ không đổi, kho phản hồi few-shot trống. Nhưng **5/24 tiêu chí có dữ kiện trích khác** giữa lần đo 07/09 và 19/09 dù đầu vào y hệt. Nguyên nhân khả dĩ nhất: qwen3:8b (5,2 GB) chạy trên GPU 4 GB nên bị chia tầng GPU/CPU, tỷ lệ chia đổi theo VRAM trống lúc nạp → sai khác số học nhỏ → ở temperature 0 vẫn lật token khi hai lựa chọn sát nhau. Hệ quả phải nói thẳng:
  - Con số **24/24 ngày 07/09 có phần may**; một lần đo đơn lẻ không phải thước đo tin cậy. Muốn báo cáo thì phải đo lặp và báo khoảng dao động.
  - Chỉ số an toàn **false-pass ổn định 0/6 ở cả ba lần đo**; sai khác rơi vào phía báo động giả — phía có người xem lại và có ký cấp 2.
  - Triển khai thật nên dùng GPU đủ chứa trọn model (≥ 8 GB) để bỏ việc chia tầng; chưa kiểm chứng được trên máy này.
  - **Cố ý KHÔNG thêm guard cho R07** để kéo lại 24/24: đó đúng là kiểu vá theo đề thi (overfit). Ghi vào backlog là điểm yếu suy luận số đếm của model 8B; chỉ thêm guard khi hồ sơ thật lộ lỗ.

## Đợt 7 (09/2026) — bản tiếng Anh của ứng dụng (VI là bản gốc, EN là lớp hiển thị)

Khách cần trình bày cho hội đồng/đối tác đọc tiếng Anh nhưng lo "dịch ngược" làm sai nội dung lấy nguyên văn từ tài liệu của họ. Cách làm:

- **Tiếng Việt vẫn là bản gốc** trong mã nguồn và dữ liệu. Tiếng Anh là **một lớp áp lên đầu ra**: nút **VI | EN** trên thanh đầu trang (và ở màn đăng nhập) đặt cookie `gl_lang`; khi `en`, máy chủ dịch mọi JSON trả về (`backend/i18n.py`, `I18nJSONResponse`) và giao diện dịch DOM (text node, placeholder/title, hộp thoại) bằng **cùng một từ điển** lấy từ `GET /api/i18n/en` (`backend/i18n_en.py`: ~550 chuỗi khớp nguyên + ~170 mẫu regex có nhóm, kèm `title_en` cho 46 tiêu chí trong 6 bộ).
- **Không có câu chữ nguyên văn nào đi qua từ điển**: văn bản hồ sơ, câu tiêu chí (`quote`), trích dẫn hai chiều (`rq`/`aq`), dữ kiện AI trích, thư kết quả, mẫu thư, tên người/tổ chức, và lời chứng thực/lý do do cán bộ tự gõ đều nằm trong danh sách bỏ qua ở cả hai phía (`SKIP_KEYS` phía máy chủ; vùng `.doc/.rquote/.qt/.facts/.letter/.notranslate` phía giao diện). Đây là câu trả lời cho nỗi lo "dịch sai nội dung lấy từ tài liệu của tôi": nội dung đó không được dịch, chỉ nhãn/giải thích của hệ thống mới được dịch.
- **Ghi chú của AI** (câu tiếng Việt do model viết ở lượt phán quyết) là văn tự do nên không dịch bằng từ điển: ngay sau mỗi lượt đánh giá, hệ thống gọi model dịch riêng một lần và lưu `note_en` cạnh bản gốc (cột mới trong `verdicts`; prompt phán quyết **không đổi**, nên số đo không bị ảnh hưởng; tắt bằng `GRANTLENS_NOTE_EN=off` khi đo/kiểm thử). Kết luận cũ chưa có bản dịch thì hiện nguyên tiếng Việt kèm nhãn *(AI note in Vietnamese — not yet translated)*; dịch bù bằng `python -m backend.i18n backfill`.
- **Nhật ký kiểm toán** lưu đúng chuỗi tiếng Việt lúc ghi (chuỗi băm không đổi); bản EN chỉ dịch lúc hiển thị theo mẫu của từng loại sự kiện. Lý do tự gõ trong dòng nhật ký được giữ nguyên câu chữ.
- Kiểm: bộ test `test_i18n` 43/43 (từ điển sạch, vùng không dịch, cookie → JSON EN, VI mặc định không đổi, stream đánh giá, note_en, backfill) + quét Playwright toàn bộ màn ở chế độ EN: **0 chuỗi tiếng Việt còn sót ngoài vùng nguyên văn** (chỉ còn ghi chú AI cũ chưa dịch, có nhãn rõ). Bộ xác thực/phân quyền 43/43 vẫn xanh.
- Sửa kèm một lỗi có sẵn: sự kiện stream `verdict` bị khoá `type` của rule (qualitative/quantitative) ghi đè nên giao diện chưa bao giờ nhận được nó; nay là `type: "verdict"` + `rule_type`.

## Điểm phương pháp
**Chống thiên lệch văn phong (2 lượt gọi thật):** lượt 1 chỉ trích *dữ kiện* trung tính (số liệu, ngày, directorate…) kèm chunk_id + key_phrase; lượt 2 phán quyết **chỉ nhìn danh sách dữ kiện**, không nhìn văn gốc ⇒ ngữ pháp/độ trôi chảy không thể ảnh hưởng. Kiểm chứng bằng 2 cặp HS-04A/B và HS-11A/B (Bias Lab + eval).

**Citation-by-retrieval:** model 8B không sinh quote. Model chỉ trỏ chunk + 3-8 từ khóa; code cắt *câu nguyên văn* khớp nhất và string-match lại với hồ sơ ⇒ trích dẫn đúng nguyên văn "by construction". UI bấm vào trích dẫn sẽ highlight ngay trong hồ sơ gốc.

**Nhật ký kiểm toán chuỗi hash:** mỗi sự kiện chứa SHA-256 của sự kiện trước; sửa/xoá một dòng làm chuỗi đứt và bị phát hiện (`GET /api/audit` trả `chain.ok`).

## Cài đặt & chạy
```bash
pip install -r requirements.txt
ollama pull qwen3:8b            # ~6GB VRAM; máy yếu: qwen3:8b-q4_K_M (đặt GRANTLENS_MODEL)
python -m backend.auth init-demo     # tạo tài khoản + mật khẩu ngẫu nhiên -> data/demo-accounts.txt (không vào git)
uvicorn backend.app:app --port 8000
# mở http://localhost:8000 — đăng nhập bằng tài khoản trong data/demo-accounts.txt
```
Quản lý tài khoản: sửa `data/officers.json` (tên, vai trò `officer|manager|auditor`, tổ chức), rồi `python -m backend.auth set-password <username> <mật khẩu>`; `python -m backend.auth list` để xem. **Đổi mật khẩu demo và đặt `GRANTLENS_SECRET` trước khi triển khai thật.**
Biến môi trường:
- `GRANTLENS_LLM=ollama|openai|mock` — `mock` để demo luồng nghiệp vụ không cần model (UI gắn nhãn rõ, không dùng báo cáo số liệu)
- `GRANTLENS_MODEL`, `OLLAMA_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- `EMBED_BACKEND=bge` (cần `pip install sentence-transformers faiss-cpu`)
- `GRANTLENS_DB` đường dẫn SQLite
- `GRANTLENS_SECRET` khoá ký phiên (mặc định tự sinh vào `data/.secret`), `GRANTLENS_SESSION_HOURS` (mặc định 8), `GRANTLENS_AUTH=off` chỉ cho kiểm thử

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
