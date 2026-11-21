"""Offline retrieval eval (Phase 5): recall@k of each question's ``expected_knowledge``.

The query is the question text itself, so this measures the index and the ranking, not the
agent: the agent writes its own queries, and its recall is reported per run
(``kb_recall``). Only expected chunks the run's ``agent.kb_sources`` can return count, so
E2a (no "cannot answer" doc) and E6 are each measured on what they can see.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean

from scrollback.config import Settings
from scrollback.evals.questions import QuestionSet
from scrollback.kb.chunks import all_chunks
from scrollback.kb.retrieve import recall_at_k, search


@dataclass(frozen=True)
class QuestionRecall:
    qid: str
    category: str
    expected: list[str]
    retrieved: list[str]
    recall: float | None


def question_recalls(
    qset: QuestionSet, settings: Settings, split: str = "dev", k: int | None = None
) -> list[QuestionRecall]:
    sources = {c.id: c.source for c in all_chunks()}
    allowed = set(settings.agent.kb_sources)
    out = []
    for q in qset.by_split(split):
        expected = [e for e in q.expected_knowledge if sources.get(e) in allowed]
        retrieved = [h.chunk.id for h in search(q.question, settings, k=k)]
        out.append(QuestionRecall(q.id, q.category, expected, retrieved,
                                  recall_at_k(retrieved, expected)))  # fmt: skip
    return out


def retrieval_report(
    qset: QuestionSet,
    settings: Settings,
    split: str = "dev",
    k: int | None = None,
    show_misses: bool = False,
) -> str:
    k = k or settings.llm.retrieval_k
    rows = question_recalls(qset, settings, split, k)
    scored = [r for r in rows if r.recall is not None]
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in scored:
        assert r.recall is not None
        by_cat[r.category].append(r.recall)
    full = sum(1 for r in scored if r.recall == 1.0)
    lines = [
        f"# Retrieval recall@{k} ({split} split; query = question text)",
        "",
        f"sources: {', '.join(settings.agent.kb_sources)}; model: {settings.kb.embedding_model}",
        f"{len(scored)} of {len(rows)} questions have expected knowledge in scope",
        "",
        "| Slice | recall@k | all found |",
        "|---|---|---|",
    ]
    if scored:
        lines.append(
            f"| **all** | {100 * mean(r.recall for r in scored if r.recall is not None):.1f}% "
            f"| {full}/{len(scored)} |"
        )
    if split == "dev":  # test is reported overall only (DESIGN §7.4)
        for cat, vals in sorted(by_cat.items()):
            n_full = sum(1 for v in vals if v == 1.0)
            lines.append(f"| {cat} | {100 * mean(vals):.1f}% | {n_full}/{len(vals)} |")
    if show_misses:
        lines += ["", "Misses:"]
        for r in scored:
            missed = [e for e in r.expected if e not in r.retrieved]
            if missed:
                lines.append(f"- {r.qid}: missed {missed}; got {r.retrieved}")
    return "\n".join(lines)
