"""verify.py — trích dẫn "by construction".
Model 8B KHÔNG được tự sinh quote (dễ sửa chữ). Model chỉ chọn chunk + key_phrase ngắn;
code tìm CÂU nguyên văn trong chunk khớp key_phrase nhất và dùng câu đó làm trích dẫn.
Lớp 2: string-match lại quote với văn bản gốc — luôn phải pass; nếu fail là bug, gắn cờ.
"""
from difflib import SequenceMatcher
from .rag import split_sentences


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def extract_quote(chunk_text: str, key_phrase: str, max_words: int = 40) -> str:
    """Trả về 1 câu NGUYÊN VĂN trong chunk khớp key_phrase nhất (fuzzy)."""
    sentences = split_sentences(chunk_text)
    if not sentences:
        return chunk_text[:200]
    if not key_phrase:
        best = sentences[0]
    else:
        kp = _norm(key_phrase)
        best, best_score = sentences[0], -1.0
        for s in sentences:
            sn = _norm(s)
            score = SequenceMatcher(None, kp, sn).ratio()
            if kp in sn:               # chứa trọn key phrase -> ưu tiên tuyệt đối
                score += 1.0
            if score > best_score:
                best, best_score = s, score
    words = best.split()
    if len(words) > max_words:
        best = " ".join(words[:max_words]) + " ..."
    return best


def quote_in_source(quote: str, source: str) -> bool:
    q = _norm(quote.replace(" ...", ""))
    return bool(q) and q in _norm(source)
