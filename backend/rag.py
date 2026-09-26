"""rag.py — chunking + retrieval.
Default: TF-IDF (runs on any machine, no GPU or model download needed).
Set EMBED_BACKEND=bge to use BAAI/bge-m3 through sentence-transformers (+FAISS when available).
Each chunk keeps (id, start, end, text) — the character offsets are the basis of citation-by-retrieval:
the final quotation is cut by CODE from the chunk; the model never generates a quotation itself.
"""
import os, re
import numpy as np

EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "tfidf")


def chunk_text(text: str):
    """Split on paragraphs (blank lines); each chunk carries its character offsets in the original text."""
    chunks, pos = [], 0
    for i, para in enumerate(re.split(r"\n\s*\n", text)):
        para_stripped = para.strip()
        if para_stripped:
            start = text.index(para_stripped, pos)
            end = start + len(para_stripped)
            chunks.append({"id": len(chunks), "start": start, "end": end, "text": para_stripped})
            pos = end
    return chunks


def split_sentences(text: str):
    """Simple sentence split that keeps every sentence verbatim."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


class _TfidfEmbedder:
    def __init__(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)

    def fit_index(self, texts):
        self.mat = self.vec.fit_transform(texts)

    def query(self, q, k):
        from sklearn.metrics.pairwise import cosine_similarity
        sims = cosine_similarity(self.vec.transform([q]), self.mat)[0]
        order = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]


class _BgeEmbedder:
    def __init__(self):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer("BAAI/bge-m3")

    def fit_index(self, texts):
        self.emb = self.model.encode(texts, normalize_embeddings=True)
        try:
            import faiss
            self.index = faiss.IndexFlatIP(self.emb.shape[1])
            self.index.add(self.emb.astype("float32"))
            self._faiss = True
        except ImportError:
            self._faiss = False

    def query(self, q, k):
        qv = self.model.encode([q], normalize_embeddings=True).astype("float32")
        if self._faiss:
            scores, ids = self.index.search(qv, k)
            return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]
        sims = (self.emb @ qv[0])
        order = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]


class Retriever:
    def __init__(self, text: str):
        self.chunks = chunk_text(text)
        self.embedder = _BgeEmbedder() if EMBED_BACKEND == "bge" else _TfidfEmbedder()
        self.embedder.fit_index([c["text"] for c in self.chunks])

    def retrieve(self, query: str, k: int = 3):
        hits = self.embedder.query(query, min(k, len(self.chunks)))
        return [{**self.chunks[i], "score": round(s, 4)} for i, s in hits]
