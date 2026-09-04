# data/external — dữ liệu đối chiếu / sàng lọc (KHÔNG phải dữ liệu huấn luyện)

Nguồn: file `Dữ liệu -20260903T071159Z-1-001.zip` (Google Drive export, giải nén 2026-09-03).
Đây là dữ liệu **tham chiếu công khai của Úc** dùng để sàng lọc người nộp / tổ chức (denied-party screening),
không phải dữ liệu có nhãn để huấn luyện model.

## sanctions/individuals/ — ASIC "Banned and Disqualified Persons"
- 7.212 bản ghi cá nhân bị cấm/tước quyền hành nghề (chứng khoán, tín dụng, quản trị công ty...) tại Úc.
- Cột chính: `BD_PER_NAME` (HỌ, Tên), `BD_PER_TYPE` (loại lệnh cấm), `BD_PER_DOC_NUM`,
  `BD_PER_START_DT` / `BD_PER_END_DT` (dd/mm/yyyy; nhiều lệnh đã hết hạn → phải lọc theo ngày),
  địa chỉ (bang, mã bưu chính, quốc gia).
- 3 UUID × 3 định dạng (csv/json/xml) là **cùng một bộ dữ liệu** tải 3 lần — chỉ cần dùng 1 file CSV.
  JSON dạng CKAN datastore (`{fields, records}`).

## sanctions/companies/ — DFAT Australian Sanctions Consolidated List
- `Australian_Sanctions_Consolidated_List.xlsx`, sheet "Consolidated List", 11.163 dòng.
- Gồm CẢ cá nhân lẫn tổ chức bị cấm vận (cột `Type` = Individual / Entity); một đối tượng nhiều dòng
  (Primary Name + Alias + Original Script — khớp tên phải gộp theo cột `Reference`).
- Cột chính: Reference, Name, Type, Name Type, Date of Birth, Place of Birth, Citizenship, Address, Committees (căn cứ pháp lý).

## abn/ — ABR Bulk Extract (đăng ký doanh nghiệp Úc)
- 2 zip (~500 MB/cái) chứa 20 file XML `20260826_PublicNN.xml`, tổng ~12,6 GB giải nén, ~20,5 triệu bản ghi ABN.
- Mỗi `<ABR>`: ABN, trạng thái, tên pháp nhân, tên giao dịch, loại hình, bang/mã bưu chính, GST.
- **Giữ nguyên dạng zip** — code đọc stream trực tiếp từ zip (`zipfile` + `iterparse`), không cần giải nén 12 GB.

## Dùng cho GrantLens
Phù hợp làm **lớp sàng lọc tự động** trước khi thẩm định điều kiện:
1. Khớp tên người nộp với ASIC banned list + DFAT list (fuzzy match, lọc lệnh còn hiệu lực).
2. Khớp tổ chức với DFAT list (Entity) và xác minh tồn tại/trạng thái qua ABN (nếu là tổ chức Úc).
3. Kết quả sàng lọc = một dòng bằng chứng trong ma trận (đạt / cần người xem hit), không tự động loại hồ sơ.

Không dùng để fine-tune LLM: không có cặp (đầu vào → nhãn) cho bài toán thẩm định; muốn huấn luyện cần bộ hồ sơ + nhãn như `data/labels/ground-truth.json`.
