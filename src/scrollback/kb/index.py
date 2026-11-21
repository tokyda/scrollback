"""The knowledge index: chunk embeddings in a separate, git-ignored DuckDB file.

The index lives at ``paths.kb_index`` (default ``data/kb/knowledge.duckdb``), not in a
warehouse: there is one warehouse per eval anchor, and the agent's warehouse connection is
read-only with a table allowlist. The index records a hash of the chunk texts and the
embedding model. ``ensure_index`` rebuilds it whenever either changes, so a run never
searches stale knowledge.

Embedders:
- ``FastEmbedder``: ``BAAI/bge-small-en-v1.5`` through fastembed (ONNX, local). The model is
  downloaded once into ``paths.kb_models``. Queries get BGE's retrieval instruction; passages
  don't.
- ``HashingEmbedder`` (``kb.embedding_model: hashing``): a deterministic bag-of-words hash,
  for tests and CI, where nothing is downloaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import duckdb
import numpy as np
import numpy.typing as npt

from scrollback.config import Settings
from scrollback.kb.chunks import Chunk, all_chunks

Vectors = npt.NDArray[np.float32]
HASHING = "hashing"
HASHING_DIM = 384
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
INDEX_SCHEMA_VERSION = 1


class Embedder(Protocol):
    name: str

    def embed_passages(self, texts: list[str]) -> Vectors: ...

    def embed_query(self, text: str) -> Vectors: ...


def _normalise(m: Vectors) -> Vectors:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    out: Vectors = (m / norms).astype(np.float32)
    return out


class HashingEmbedder:
    """Token-hash bag of words: similar texts share tokens, so tests see sensible rankings."""

    name = HASHING

    def _one(self, text: str) -> Vectors:
        v = np.zeros(HASHING_DIM, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
            v[h % HASHING_DIM] += 1.0
        return v

    def embed_passages(self, texts: list[str]) -> Vectors:
        return _normalise(np.stack([self._one(t) for t in texts]))

    def embed_query(self, text: str) -> Vectors:
        return _normalise(self._one(text)[None, :])[0]


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: Path) -> None:
        from fastembed import TextEmbedding

        cache_dir.mkdir(parents=True, exist_ok=True)
        self.name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))

    def embed_passages(self, texts: list[str]) -> Vectors:
        return _normalise(np.stack(list(self._model.passage_embed(texts))).astype(np.float32))

    def embed_query(self, text: str) -> Vectors:
        vec = next(iter(self._model.query_embed(BGE_QUERY_INSTRUCTION + text)))
        return _normalise(np.asarray(vec, dtype=np.float32)[None, :])[0]


_EMBEDDERS: dict[tuple[str, str], Embedder] = {}
_LOCK = threading.Lock()


def get_embedder(settings: Settings) -> Embedder:
    """One embedder per (model, cache dir) per process; loading the model takes a second."""
    model = settings.kb.embedding_model
    key = (model, str(settings.paths.kb_models))
    with _LOCK:
        if key not in _EMBEDDERS:
            _EMBEDDERS[key] = (
                HashingEmbedder() if model == HASHING
                else FastEmbedder(model, settings.paths.kb_models)
            )  # fmt: skip
        return _EMBEDDERS[key]


def content_hash(chunks: list[Chunk], model: str) -> str:
    rows = [[c.id, c.source, c.title, c.text, c.keywords] for c in chunks]
    payload = json.dumps([INDEX_SCHEMA_VERSION, model, rows], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class KnowledgeIndex:
    chunks: list[Chunk]
    vectors: Vectors  # one normalised row per chunk
    model: str
    content_hash: str


def build_index(chunks: list[Chunk], embedder: Embedder, path: Path) -> KnowledgeIndex:
    vectors = embedder.embed_passages([f"{c.title}\n{c.text}" for c in chunks])
    digest = content_hash(chunks, embedder.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        dim = int(vectors.shape[1])
        con.execute(
            "CREATE TABLE kb_chunks (id VARCHAR PRIMARY KEY, kind VARCHAR, source VARCHAR, "
            f"title VARCHAR, text VARCHAR, keywords VARCHAR, embedding FLOAT[{dim}])"
        )
        rows = [
            [c.id, c.kind, c.source, c.title, c.text, c.keywords, v.tolist()]
            for c, v in zip(chunks, vectors, strict=True)
        ]
        con.executemany("INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute("CREATE TABLE kb_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        meta = [
            ["model", embedder.name],
            ["content_hash", digest],
            ["schema_version", str(INDEX_SCHEMA_VERSION)],
        ]
        con.executemany("INSERT INTO kb_meta VALUES (?, ?)", meta)
    finally:
        con.close()
    tmp.replace(path)
    return KnowledgeIndex(chunks, vectors, embedder.name, digest)


def load_index(path: Path) -> KnowledgeIndex | None:
    if not path.exists():
        return None
    con = duckdb.connect(str(path), read_only=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM kb_meta").fetchall())
        rows = con.execute(
            "SELECT id, kind, source, title, text, keywords, embedding FROM kb_chunks "
            "ORDER BY rowid"
        ).fetchall()
    except duckdb.Error:
        return None
    finally:
        con.close()
    chunks = [Chunk(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    vectors = np.asarray([r[6] for r in rows], dtype=np.float32)
    return KnowledgeIndex(chunks, vectors, meta.get("model", ""), meta.get("content_hash", ""))


_INDEXES: dict[str, KnowledgeIndex] = {}
_BUILD_LOCK = threading.Lock()


def ensure_index(settings: Settings, rebuild: bool = False) -> KnowledgeIndex:
    """The index for the current knowledge files, rebuilt if missing or out of date."""
    chunks = all_chunks()
    digest = content_hash(chunks, settings.kb.embedding_model)
    path = settings.paths.kb_index
    with _BUILD_LOCK:  # eval workers share one index; only one of them builds it
        cached = _INDEXES.get(str(path))
        if cached is not None and cached.content_hash == digest and not rebuild:
            return cached
        index = None if rebuild else load_index(path)
        if index is None or index.content_hash != digest:
            index = build_index(chunks, get_embedder(settings), path)
        _INDEXES[str(path)] = index
        return index
