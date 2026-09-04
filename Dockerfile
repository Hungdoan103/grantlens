FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ backend/
COPY frontend/ frontend/
COPY data/guideline/ data/guideline/
COPY data/applications/ data/applications/
COPY data/labels/ data/labels/
COPY data/officers.json data/letter-template.txt data/
COPY data/external/sanctions/ data/external/sanctions/
# ABN index (1.8GB) không đóng vào image — mount volume hoặc chạy backend/abn_index.py nếu cần
ENV GRANTLENS_LLM=mock PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT}"]
