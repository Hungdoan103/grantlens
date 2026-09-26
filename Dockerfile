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
# The ABN index (1.8GB) is not baked into the image — mount a volume or run backend/abn_index.py if needed
ENV GRANTLENS_LLM=mock PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT}"]
