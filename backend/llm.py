"""llm.py — lớp gọi mô hình ngôn ngữ, đổi backend bằng biến môi trường.

GRANTLENS_LLM = ollama (mặc định) | openai | mock
  ollama : Qwen3-8B local qua Ollama, JSON ép bằng structured outputs (format=schema).
           GRANTLENS_MODEL (qwen3:8b), OLLAMA_URL, GRANTLENS_NUM_CTX
  openai : bất kỳ endpoint OpenAI-compatible (vLLM, LM Studio, OpenAI...) cho khách có hạ tầng riêng.
           OPENAI_BASE_URL, OPENAI_API_KEY, GRANTLENS_MODEL
  mock   : KHÔNG gọi model — trả kết quả giả lập để kiểm thử luồng nghiệp vụ / demo UI khi chưa có GPU.
           Kết quả mock được gắn nhãn rõ trong UI; tuyệt đối không dùng để báo cáo số liệu.
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
    return {"backend": BACKEND, "model": MODEL if BACKEND != "mock" else "mock (không gọi model)",
            "local": BACKEND == "ollama", "is_mock": BACKEND == "mock"}


def health() -> dict:
    """Kiểm tra model có sẵn sàng không (hiện trên dashboard)."""
    if BACKEND == "mock":
        return {"ok": True, "msg": "Chế độ MOCK — không gọi model"}
    if BACKEND == "ollama":
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
            base = MODEL.split(":")[0]
            ok = any(n == MODEL or n.startswith(base) for n in names)
            return {"ok": ok, "msg": f"Ollama OK, model {'sẵn sàng' if ok else 'CHƯA pull: ollama pull ' + MODEL}",
                    "models": names}
        except Exception as e:  # noqa
            return {"ok": False, "msg": f"Không kết nối được Ollama tại {OLLAMA_URL}: {e}"}
    return {"ok": bool(OPENAI_API_KEY) or "localhost" in OPENAI_BASE_URL, "msg": f"OpenAI-compatible: {OPENAI_BASE_URL}"}


# ---------------- Ollama ----------------
def _ollama(messages, fmt=None, max_tokens=700):
    payload = {"model": MODEL, "messages": messages, "stream": False, "think": False,
               "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": max_tokens}}
    if fmt is not None:
        payload["format"] = fmt
    last_err = None
    for attempt in range(3):  # retry lỗi thoáng qua (500/timeout khi GPU bận)
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
            import time as _t
            _t.sleep(5 * (attempt + 1))
    else:
        raise LLMError(f"Không gọi được Ollama tại {OLLAMA_URL} — kiểm tra `ollama serve` và `ollama pull {MODEL}`. Chi tiết: {last_err}")
    data = r.json()
    if "error" in data:
        raise LLMError(data["error"])
    return data["message"]["content"]


# ---------------- OpenAI-compatible ----------------
def _openai(messages, fmt=None, max_tokens=700):
    payload = {"model": MODEL, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if fmt is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": fmt, "strict": False}}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"} if OPENAI_API_KEY else {}
    try:
        r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=TIMEOUT)
        if r.status_code == 400 and fmt is not None:  # server không hỗ trợ json_schema -> json_object
            payload["response_format"] = {"type": "json_object"}
            payload["messages"] = messages[:-1] + [{**messages[-1], "content": messages[-1]["content"] +
                                                    f"\n\nReturn ONLY JSON matching this schema:\n{json.dumps(fmt)}"}]
            r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Lỗi gọi {OPENAI_BASE_URL}: {e}")
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


def _mock(messages, fmt=None, max_tokens=700):
    """Giả lập: trích câu đầu mỗi chunk làm 'fact'; verdict lấy từ nhãn (nếu có) — CHỈ để kiểm thử luồng."""
    user = messages[-1]["content"]
    if fmt is None:
        return ("[MOCK LETTER — no model called]\nDecision: see officer-approved verdicts.\n"
                "What you can do: send the missing documents.\nHow to appeal: write to the review board within 30 days.")
    props = fmt.get("properties", {})
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
                           "note_vi": "[MOCK] kết luận giả lập từ nhãn — không phải model."})
    if "rules" in props:  # rule extraction
        return json.dumps({"rules": [{"id": "R01", "type": "qualitative", "title_vi": "[MOCK] tiêu chí mẫu",
                                      "quote": user[:120]}]})
    return "{}"


_IMPL = {"ollama": _ollama, "openai": _openai, "mock": _mock}


def _chat(messages, fmt=None, max_tokens=700):
    if BACKEND not in _IMPL:
        raise LLMError(f"GRANTLENS_LLM={BACKEND} không hợp lệ (ollama|openai|mock)")
    return _IMPL[BACKEND](messages, fmt, max_tokens)


def chat_json(system: str, user: str, schema: dict, max_tokens=600) -> dict:
    txt = _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], fmt=schema, max_tokens=max_tokens)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise LLMError(f"Model trả về JSON không hợp lệ: {txt[:200]}")


def chat_text(system: str, user: str, max_tokens=700) -> str:
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=max_tokens)
