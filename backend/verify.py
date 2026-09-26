"""verify.py — quotations "by construction".
An 8B model must NOT generate quotations itself (it easily alters words). The model only picks a chunk and a short
key_phrase; code finds the VERBATIM sentence in that chunk that best matches the key_phrase and uses it as the quotation.
Layer 2: string-match the quotation against the original text again — it must always pass; a failure is a bug and is flagged.
"""
from difflib import SequenceMatcher
from .rag import split_sentences


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def extract_quote(chunk_text: str, key_phrase: str, max_words: int = 40) -> str:
    """Return the ONE verbatim sentence of the chunk that best matches key_phrase (fuzzy)."""
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
            if kp in sn:               # contains the whole key phrase -> absolute priority
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


def attestation_evidence(attestation: str, source: str, min_words: int = 5):
    """MECHANICAL check (no LLM) that an officer's attestation really QUOTES the application.

    Returns the longest verbatim passage (≥ min_words consecutive words) that the officer pasted into the attestation
    and that actually exists in the application; None if there is none. This is the layer against "type enough
    characters to get through the gate": to confirm MET you must paste real evidence, so your eyes must touch the
    application. No semantic judgement is involved, so there are no LLM-style false positives.
    """
    src = _norm(source)
    words = _norm(attestation).split()
    if len(words) < min_words or not src:
        return None
    best = None
    for i in range(len(words) - min_words + 1):
        for j in range(len(words), i + min_words - 1, -1):   # try the longest span first
            seg = " ".join(words[i:j])
            if len(seg.split()) <= len(best.split() if best else []):
                break
            if seg in src:
                best = seg
                break
    return best
