import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Real local model via Ollama (default in the app)
os.environ["GRANTLENS_LLM"] = "ollama"
os.environ.setdefault("GRANTLENS_MODEL", "qwen3:8b")

print(f"Starting GrantLens from {ROOT}")
print(f"GRANTLENS_LLM={os.environ['GRANTLENS_LLM']} MODEL={os.environ['GRANTLENS_MODEL']}")
from backend.app import app  # noqa: E402
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
