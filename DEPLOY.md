# Deploy GrantLens

Backend FastAPI phục vụ luôn frontend — chỉ cần chạy MỘT service. Ba kịch bản:

## A. Demo online cho khách (host thường, không GPU) — khuyên dùng để "gửi link"
Chạy chế độ `GRANTLENS_LLM=mock`: toàn bộ luồng nghiệp vụ, sàng lọc ASIC/DFAT, đối chiếu chéo,
COI, audit hash-chain chạy THẬT; riêng kết luận AI là giả lập (UI có banner ghi rõ).

**Render.com (free tier):**
1. Render → New → Web Service → kết nối repo GitHub `grantlens` → Runtime: **Docker**.
2. Environment: `GRANTLENS_LLM=mock`, `GRANTLENS_ACCESS_KEY=<khóa tự đặt>` (bắt buộc — demo riêng tư).
3. Deploy xong gửi khách link dạng `https://<app>.onrender.com/?key=<khóa>`.

Lưu ý free tier: ngủ sau 15 phút không dùng (lần mở đầu chậm ~30s); ổ đĩa không bền —
SQLite reset khi redeploy (demo thì tốt: luôn sạch). ABN index 1.8GB không kèm theo,
màn sàng lọc sẽ ghi "ABN chưa lập chỉ mục" (ASIC + DFAT vẫn chạy đủ).

**Chạy local bằng Docker:** `docker build -t grantlens . && docker run -p 8000:8000 grantlens`

## B. Chạy AI thật trên cloud (GPU VPS)
RunPod / Vast.ai / LightNode GPU ≥ 8 GB VRAM (RTX 3060 12GB trở lên):
```bash
curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen3:8b
git clone <repo> && cd grantlens && pip install -r requirements.txt
python -m backend.abn_index                    # nếu có dữ liệu ABN (tùy chọn)
GRANTLENS_ACCESS_KEY=<khóa> uvicorn backend.app:app --host 0.0.0.0 --port 8000
```
Tốc độ tham khảo: RTX 3060 12GB ≈ 1–2 phút/hồ sơ 12 tiêu chí.

## C. Máy chủ nội bộ của khách (triển khai chính thức)
Như B nhưng trong mạng LAN của đơn vị — dữ liệu không rời máy, không cần ACCESS_KEY
(GĐ2 thay bằng đăng nhập/phân quyền thật). SQLite → PostgreSQL theo lộ trình báo giá.

## Biến môi trường
| Biến | Ý nghĩa |
|---|---|
| `GRANTLENS_LLM` | `ollama` (mặc định) / `openai` / `mock` |
| `GRANTLENS_MODEL` | mặc định `qwen3:8b` |
| `GRANTLENS_ACCESS_KEY` | đặt là bật khóa truy cập (`/?key=...`, cookie 7 ngày) |
| `OLLAMA_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` | endpoint lõi AI |
| `GRANTLENS_DB` | đường dẫn SQLite (mount volume nếu cần bền) |
| `EMBED_BACKEND=bge` | RAG semantic (cần sentence-transformers + faiss) |
