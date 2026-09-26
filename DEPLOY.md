# Deploying GrantLens

The FastAPI backend also serves the frontend — a single service is all you run. Three scenarios:

## A. Online demo for a customer (ordinary host, no GPU) — recommended for "send a link"
Run with `GRANTLENS_LLM=mock`: the whole workflow, ASIC/DFAT screening, cross-check, COI checks and the
hash-chained audit log run FOR REAL; only the AI verdicts are simulated (the UI shows a clear banner).

**Render.com (free tier):**
1. Render → New → Web Service → connect the GitHub repo `grantlens` → Runtime: **Docker**.
2. Environment: `GRANTLENS_LLM=mock`, `GRANTLENS_ACCESS_KEY=<your key>` (required — private demo).
3. After the deploy, send the customer a link of the form `https://<app>.onrender.com/?key=<key>`.

Free-tier caveats: the service sleeps after 15 idle minutes (first open takes ~30s); the disk is not persistent —
SQLite resets on redeploy (fine for a demo: always clean). The 1.8 GB ABN index is not included; the screening
panel shows "ABN index not built" (ASIC + DFAT still run in full).

**Run locally with Docker:** `docker build -t grantlens . && docker run -p 8000:8000 grantlens`

## B. Real AI in the cloud (GPU VPS)
RunPod / Vast.ai / LightNode with a GPU ≥ 8 GB VRAM (RTX 3060 12 GB or better):
```bash
curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen3:8b
git clone <repo> && cd grantlens && pip install -r requirements.txt
python -m backend.abn_index                    # only if the ABN data is present (optional)
python -m backend.auth init-demo               # demo accounts -> data/demo-accounts.txt
GRANTLENS_ACCESS_KEY=<key> uvicorn backend.app:app --host 0.0.0.0 --port 8000
```
Indicative speed: RTX 3060 12 GB ≈ 1–2 minutes per 12-criteria application.

## C. The customer's own server (production)
As in B but inside the organisation's LAN — data never leaves the machine; no ACCESS_KEY needed (login and
roles are built in). SQLite → PostgreSQL per the roadmap. Change the demo passwords and set `GRANTLENS_SECRET`.

## Environment variables
| Variable | Meaning |
|---|---|
| `GRANTLENS_LLM` | `ollama` (default) / `openai` / `mock` |
| `GRANTLENS_MODEL` | default `qwen3:8b` |
| `GRANTLENS_ACCESS_KEY` | when set, enables the access key (`/?key=...`, 7-day cookie) |
| `OLLAMA_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` | AI endpoint |
| `GRANTLENS_DB` | SQLite path (mount a volume for persistence) |
| `GRANTLENS_SECRET`, `GRANTLENS_SESSION_HOURS`, `GRANTLENS_AUTH=off` | session signing key, session length (default 8 h), auth off (tests only) |
| `EMBED_BACKEND=bge` | semantic RAG (needs sentence-transformers + faiss) |
