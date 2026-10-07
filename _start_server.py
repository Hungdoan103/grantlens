import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("GRANTLENS_LLM", "mock")

print(f"Starting GrantLens from {ROOT} (GRANTLENS_LLM={os.environ.get('GRANTLENS_LLM')})")
from backend.app import app  # noqa: E402
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
