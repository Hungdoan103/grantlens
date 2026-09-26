"""llm.py — language-model access layer; the backend is selected by environment variable.

GRANTLENS_LLM = ollama (default) | openai | mock
  ollama : Qwen3-8B running locally through Ollama; JSON is enforced with structured outputs (format=schema).
           GRANTLENS_MODEL (qwen3:8b), OLLAMA_URL, GRANTLENS_NUM_CTX
  openai : any OpenAI-compatible endpoint (vLLM, LM Studio, OpenAI...) for customers with their own infrastructure.
           OPENAI_BASE_URL, OPENAI_API_KEY, GRANTLENS_MODEL
  mock   : NO model is called — returns simulated results to test the workflow / demo the UI without a GPU.
           Mock results are clearly labelled in the UI; they must never be used to report figures.
"""
import os, json, re, requests

BACKEND = os.environ.get("GRANTLENS_LLM", "ollama").lower()
MODEL = os.environ.get("GRANTLENS_MODEL", "qwen3:8b" if BACKEND != "openai" else "gpt-4o-mini")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
NUM_CTX = int(os.environ.get("GRANTLENS_NUM_CTX", "8192"))
TIMEOUT = int(os.environ.get("GRANTLENS_LLM_TIMEOUT", "300"))


class LLMError(Exception):
    pass


def describe() -> dict:
    return {"backend": BACKEND, "model": MODEL if BACKEND != "mock" else "mock (no model called)",
            "local": BACKEND == "ollama", "is_mock": BACKEND == "mock"}


def health() -> dict:
    """Is the model ready? (shown on the dashboard)."""
    if BACKEND == "mock":
        return {"ok": True, "msg": "MOCK mode — no model is called"}
    if BACKEND == "ollama":
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
            base = MODEL.split(":")[0]
            ok = any(n == MODEL or n.startswith(base) for n in names)
            return {"ok": ok, "msg": f"Ollama OK, model {'ready' if ok else 'NOT pulled: ollama pull ' + MODEL}",
                    "models": names}
        except Exception as e:  # noqa
            return {"ok": False, "msg": f"Cannot connect to Ollama at {OLLAMA_URL}: {e}"}
    return {"ok": bool(OPENAI_API_KEY) or "localhost" in OPENAI_BASE_URL, "msg": f"OpenAI-compatible: {OPENAI_BASE_URL}"}


# ---------------- Ollama ----------------
def _ollama(messages, fmt=None, max_tokens=700, model=None):
    payload = {"model": model or MODEL, "messages": messages, "stream": False, "think": False,
               "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": max_tokens}}
    if fmt is not None:
        payload["format"] = fmt
    last_err = None
    for attempt in range(3):  # retry transient errors (500 / timeout while the GPU is busy)
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
            import time as _t
            _t.sleep(5 * (attempt + 1))
    else:
        raise LLMError(f"Cannot reach Ollama at {OLLAMA_URL} — check `ollama serve` and `ollama pull {MODEL}`. Details: {last_err}")
    data = r.json()
    if "error" in data:
        raise LLMError(data["error"])
    return data["message"]["content"]


# ---------------- OpenAI-compatible ----------------
def _openai(messages, fmt=None, max_tokens=700, model=None):
    payload = {"model": model or MODEL, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if fmt is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": fmt, "strict": False}}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"} if OPENAI_API_KEY else {}
    try:
        r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=TIMEOUT)
        if r.status_code == 400 and fmt is not None:  # server does not support json_schema -> fall back to json_object
            payload["response_format"] = {"type": "json_object"}
            payload["messages"] = messages[:-1] + [{**messages[-1], "content": messages[-1]["content"] +
                                                    f"\n\nReturn ONLY JSON matching this schema:\n{json.dumps(fmt)}"}]
            r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Error calling {OPENAI_BASE_URL}: {e}")
    return r.json()["choices"][0]["message"]["content"]


# ---------------- Mock ----------------
_MOCK_LABELS = None


def _mock_labels():
    global _MOCK_LABELS
    if _MOCK_LABELS is None:
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "data" / "labels" / "ground-truth.json"
        _MOCK_LABELS = json.loads(p.read_text(encoding="utf-8"))["labels"]
    return _MOCK_LABELS


def _mock(messages, fmt=None, max_tokens=700, model=None):
    """Simulation: the first sentence of each chunk becomes a 'fact'; the verdict comes from the label (if any).
    ONLY for testing the workflow."""
    user = messages[-1]["content"]
    if fmt is None:
        return ("[MOCK LETTER — no model called]\nDecision: see officer-approved verdicts.\n"
                "What you can do: send the missing documents.\nHow to appeal: write to the review board within 30 days.")
    props = fmt.get("properties", {})
    if "answer" in props:  # casegen verifier
        return json.dumps({"answer": "unclear", "evidence": "[MOCK]"})
    if "fact" in props:  # casegen: planned violation sentence
        return json.dumps({"fact": "Riverbend Test Pty Ltd is an unincorporated association with no ABN."})
    if "facts" in props:  # extract step
        facts = []
        for m in re.finditer(r"\[chunk (\d+)\] (.+)", user):
            sent = re.split(r"(?<=[.!?])\s+", m.group(2).strip())[0]
            words = sent.split()
            facts.append({"fact": sent[:160], "chunk_id": int(m.group(1)), "key_phrase": " ".join(words[2:8]) or sent[:40]})
        return json.dumps({"facts": facts[:3], "coverage": "partial" if facts else "none"})
    if "verdict" in props:  # judge step
        m = re.search(r"CASE (\S+) .*?RULE ([A-Z]\d{2})", user, re.S)
        v = "unclear"
        if m:
            v = _mock_labels().get(m.group(1), {}).get(m.group(2), "unclear")
        return json.dumps({"verdict": v, "confidence": "medium", "supporting_fact": 1,
                           "note": "[MOCK] simulated verdict taken from the label — not a model."})
    if "rules" in props:  # rule extraction
        return json.dumps({"rules": [{"id": "R01", "type": "qualitative", "title": "[MOCK] sample criterion",
                                      "quote": user[:120]}]})
    return "{}"


_IMPL = {"ollama": _ollama, "openai": _openai, "mock": _mock}


def _chat(messages, fmt=None, max_tokens=700, model=None):
    if BACKEND not in _IMPL:
        raise LLMError(f"GRANTLENS_LLM={BACKEND} is invalid (ollama|openai|mock)")
    return _IMPL[BACKEND](messages, fmt, max_tokens, model)


def chat_json(system: str, user: str, schema: dict, max_tokens=600, model=None) -> dict:
    """model=None -> the system model; pass another model to separate roles (e.g. generating / verifying test labels)."""
    txt = _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], fmt=schema, max_tokens=max_tokens, model=model)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise LLMError(f"Model returned invalid JSON: {txt[:200]}")


def chat_text(system: str, user: str, max_tokens=700, model=None) -> str:
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=max_tokens, model=model)
