"""Knowledge chunks, index and hybrid retrieval (Phase 5). Offline: the hashing embedder."""

from pathlib import Path

import pytest

from scrollback.config import Settings
from scrollback.kb.chunks import all_chunks, doc_chunks, slug
from scrollback.kb.index import (
    HashingEmbedder,
    build_index,
    content_hash,
    ensure_index,
    load_index,
)
from scrollback.kb.retrieve import bm25_scores, recall_at_k, search, tokens

from .conftest import repo_settings


@pytest.fixture
def kb_settings(tmp_path: Path) -> Settings:
    settings = repo_settings()
    settings.paths = settings.paths.model_copy(update={"kb_index": tmp_path / "kb.duckdb"})
    settings.kb = settings.kb.model_copy(update={"embedding_model": "hashing"})
    return settings


def test_chunks_cover_every_source() -> None:
    chunks = all_chunks()
    ids = {c.id for c in chunks}
    assert "metric:est_watch_minutes" in ids and "metric:avg_session_minutes" in ids
    assert "table:fct_sessions" in ids and "doc:sessions#definition" in ids
    assert {c.source for c in chunks} >= {"metrics", "docs", "tables", "cannot_answer"}
    assert all(c.source == "cannot_answer" for c in chunks if c.id.startswith("doc:cannot_"))


def test_chunk_text_keeps_placeholders() -> None:
    sessions = next(c for c in all_chunks() if c.id == "doc:sessions#definition")
    assert "{session_gap_min}" in sessions.text


def test_doc_sections(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        "# Title\n\nshort\n\n## First part\nbody one `col_a`\n\n## Empty\n\n## Second\nbody two\n"
    )
    chunks = doc_chunks(tmp_path)
    assert [c.id for c in chunks] == ["doc:x#first-part", "doc:x#second"]  # short intro dropped
    assert chunks[0].title == "Title: First part" and "col_a" in chunks[0].keywords
    assert slug("The method (gap-based, version 1)") == "the-method-gap-based-version-1"


def test_index_round_trip_and_staleness(kb_settings: Settings) -> None:
    chunks = all_chunks()
    path = kb_settings.paths.kb_index
    built = build_index(chunks, HashingEmbedder(), path)
    loaded = load_index(path)
    assert loaded is not None
    assert [c.id for c in loaded.chunks] == [c.id for c in chunks]
    assert loaded.content_hash == built.content_hash == content_hash(chunks, "hashing")
    assert loaded.vectors.shape == (len(chunks), 384)
    # A changed chunk text changes the hash, so ensure_index would rebuild.
    edited = [chunks[0].__class__(**{**chunks[0].__dict__, "text": "changed"}), *chunks[1:]]
    assert content_hash(edited, "hashing") != built.content_hash
    assert content_hash(chunks, "BAAI/bge-small-en-v1.5") != built.content_hash


def test_ensure_index_builds_once(kb_settings: Settings) -> None:
    first = ensure_index(kb_settings)
    assert kb_settings.paths.kb_index.exists()
    assert ensure_index(kb_settings) is first  # cached in-process


def test_tokens_split_identifiers() -> None:
    assert set(tokens("est_watch_sec per day")) == {"est_watch_sec", "est", "watch", "sec", "per",
                                                     "day"}  # fmt: skip
    assert tokens("what is the") == []


def test_synonyms_find_the_metric(kb_settings: Settings) -> None:
    hits = search("screen time on tiktok yesterday", kb_settings, k=3)
    assert hits[0].chunk.id == "metric:est_watch_minutes"
    hits = search("rewatches", kb_settings, k=3)
    assert "metric:rewatch_rate" in [h.chunk.id for h in hits]


def test_bm25_prefers_keyword_matches() -> None:
    chunks = all_chunks()
    scores = bm25_scores("binge sessions", chunks)
    best = chunks[max(range(len(chunks)), key=lambda i: scores[i])]
    assert best.id == "metric:binge_sessions"


def test_search_respects_kb_sources_and_renders_params(kb_settings: Settings) -> None:
    query = "what this data cannot answer creators ads"
    ids = [h.chunk.id for h in search(query, kb_settings, k=10)]
    assert not any(i.startswith("doc:cannot_answer") for i in ids)  # E2a sources
    kb_settings.agent.kb_sources = ["metrics", "docs", "tables", "cannot_answer"]
    ids = [h.chunk.id for h in search(query, kb_settings, k=10)]
    assert any(i.startswith("doc:cannot_answer") for i in ids)
    hit = next(h for h in search("session gap rule", kb_settings, k=10)
               if h.chunk.id == "doc:sessions#definition")  # fmt: skip
    assert "{session_gap_min}" not in hit.text
    assert f"most {kb_settings.metrics.session_gap_min} minutes" in hit.text


def test_nonsense_query_returns_few(kb_settings: Settings) -> None:
    assert len(search("zzqx vvkp", kb_settings, k=6)) <= 3
    assert search("   ", kb_settings) == []


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b"], ["a", "c"]) == 0.5
    assert recall_at_k(["a"], []) is None


def test_retrieval_report_offline(kb_settings: Settings) -> None:
    from scrollback.evals.questions import load_questions
    from scrollback.evals.retrieval import question_recalls, retrieval_report

    qset = load_questions()
    rows = question_recalls(qset, kb_settings, split="dev", k=6)
    assert len(rows) == len(qset.by_split("dev"))
    unanswerable = [r for r in rows if r.category == "unanswerable"]
    assert unanswerable and all(r.recall is None for r in unanswerable)  # cannot_answer is E6
    text = retrieval_report(qset, kb_settings, split="dev", k=6, show_misses=True)
    assert "recall@6" in text and "| **all** |" in text and "unanswerable" not in text


def test_cli_kb_search(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from scrollback.cli import app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n  kb_index: kb.duckdb\nkb:\n  embedding_model: hashing\n", encoding="utf-8"
    )
    runner = CliRunner()
    out = runner.invoke(app, ["kb", "index", "--config", str(cfg)])
    assert out.exit_code == 0 and "chunks" in out.output and "hashing" in out.output
    out = runner.invoke(app, ["kb", "search", "binge sessions", "--k", "2", "--config", str(cfg)])
    assert out.exit_code == 0 and "metric:binge_sessions" in out.output
