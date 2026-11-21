"""Hybrid retrieval for ``search_knowledge`` (DESIGN §6.2).

Two rankings over the chunks the run may see (``agent.kb_sources``), merged with
reciprocal-rank fusion (RRF, ``score = sum(1 / (kb.rrf_k + rank))``):

- **embedding similarity**: cosine between the query and each chunk (``kb.index``);
- **keyword match**: BM25 over the chunk's title, text and keywords. Keywords (metric names,
  labels, synonyms, column names) count ``KEYWORD_WEIGHT`` times, so "screen time" finds
  ``est_watch_minutes`` through its synonym list. Identifiers are also split on ``_``.

A chunk that matches no query token and is outside the top ``DENSE_ONLY_DEPTH`` of the
embedding ranking is not returned, so a nonsense query returns little rather than k
arbitrary chunks.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from scrollback.config import Settings
from scrollback.kb.chunks import Chunk
from scrollback.kb.index import KnowledgeIndex, ensure_index, get_embedder
from scrollback.semantic.metrics import render

KEYWORD_WEIGHT = 3
BM25_K1 = 1.2
BM25_B = 0.75
DENSE_ONLY_DEPTH = 3
STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "to", "in", "on", "at", "for", "by", "with", "and", "or", "is",
        "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "i",
        "me", "my", "we", "you", "your", "how", "what", "which", "when", "where", "who", "why",
        "do", "does", "did", "much", "many",
    }
)  # fmt: skip


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in re.findall(r"[a-z0-9_]+", text.lower()):
        parts = [p for p in word.split("_") if p]
        for tok in {word, *parts} if len(parts) > 1 else {word}:
            if tok not in STOPWORDS and len(tok) > 1:
                out.append(tok)
    return out


def _doc_tokens(c: Chunk) -> list[str]:
    return tokens(f"{c.title} {c.text}") + tokens(c.keywords) * KEYWORD_WEIGHT


def bm25_scores(query: str, chunks: list[Chunk]) -> list[float]:
    docs = [Counter(_doc_tokens(c)) for c in chunks]
    if not docs:
        return []
    lengths = [sum(d.values()) for d in docs]
    avg = sum(lengths) / len(docs)
    n = len(docs)
    scores = [0.0] * n
    for tok in set(tokens(query)):
        df = sum(1 for d in docs if tok in d)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, d in enumerate(docs):
            tf = d.get(tok, 0)
            if tf:
                norm = tf + BM25_K1 * (1 - BM25_B + BM25_B * lengths[i] / avg)
                scores[i] += idf * tf * (BM25_K1 + 1) / norm
    return scores


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    text: str  # rendered: {param} placeholders filled from the run's settings


def _ranks(scores: list[float]) -> dict[int, int]:
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    return {i: r for r, i in enumerate(order, start=1)}


def search(
    query: str, settings: Settings, k: int | None = None, index: KnowledgeIndex | None = None
) -> list[Hit]:
    index = index or ensure_index(settings)
    allowed = set(settings.agent.kb_sources)
    rows = [i for i, c in enumerate(index.chunks) if c.source in allowed]
    if not rows or not query.strip():
        return []
    chunks = [index.chunks[i] for i in rows]
    k = k or settings.llm.retrieval_k

    qvec = get_embedder(settings).embed_query(query)
    dense = (index.vectors[rows] @ qvec).astype(float).tolist()
    sparse = bm25_scores(query, chunks)
    dense_rank, sparse_rank = _ranks(dense), _ranks(sparse)
    rrf = settings.kb.rrf_k
    fused = []
    for i in range(len(chunks)):
        if sparse[i] <= 0 and dense_rank[i] > DENSE_ONLY_DEPTH:
            continue
        score = 1.0 / (rrf + dense_rank[i])
        if sparse[i] > 0:
            score += 1.0 / (rrf + sparse_rank[i])
        fused.append((score, i))
    fused.sort(key=lambda t: (-t[0], t[1]))
    return [
        Hit(chunks[i], round(float(s), 6), render(chunks[i].text, settings)) for s, i in fused[:k]
    ]


def recall_at_k(retrieved: list[str], expected: list[str]) -> float | None:
    """Share of the expected chunk ids found in ``retrieved``; None when nothing is expected."""
    if not expected:
        return None
    return float(np.mean([e in retrieved for e in expected]))
