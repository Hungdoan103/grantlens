# Kiểm thử (không cần GPU, không đụng dữ liệu thật)

| Lệnh | Kiểm gì |
|---|---|
| `python tests/test_auth_e2e.py` | Đăng nhập, phiên, phân quyền officer/manager/auditor, hậu kiểm lấy mẫu, chốt nhất quán (43 kiểm) |
| `python tests/test_i18n.py` | Lớp tiếng Anh: từ điển sạch, vùng nguyên văn không dịch, cookie `gl_lang` → JSON EN, VI mặc định không đổi, stream đánh giá, `note_en`, backfill (43 kiểm) |
| `python tests/scan_en.py http://127.0.0.1:8000/` | Quét toàn bộ giao diện ở chế độ EN bằng Playwright, liệt kê chuỗi tiếng Việt còn sót ngoài vùng nguyên văn (cần server đang chạy `GRANTLENS_LLM=mock` + `data/demo-accounts.txt`) |

Cả hai bộ test dùng mock LLM và DB tạm (`GRANTLENS_DB` trỏ vào thư mục tạm), sổ tài khoản vá trong bộ nhớ — không ghi gì vào `data/`.
