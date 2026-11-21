"""Knowledge chunks for ``search_knowledge`` (DESIGN §6.2, Phase 5).

Sources, each a switch in ``agent.kb_sources``:

- ``metrics``: one chunk per metric in ``knowledge/metrics.yml`` (definition, SQL, caveat
  text, synonyms);
- ``docs``: one chunk per ``##`` section of the glossary docs in ``knowledge/docs/``, plus the
  text before the first section when it says something;
- ``tables``: one chunk per mart in ``dbt/models/marts/_marts.yml`` (table and column
  descriptions);
- ``cannot_answer``: the sections of ``knowledge/docs/cannot_answer.md`` (E6 adds them);
- ``examples``: ``knowledge/examples.yml`` (E3 adds them).

Chunk text keeps its ``{param}`` placeholders. They are filled from the run's settings when a
chunk is returned (``retrieve.search``), so the index doesn't depend on config values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from scrollback.config import KBSource as Source
from scrollback.semantic.metrics import load_metrics

ChunkKind = Literal["metric", "doc", "table", "example"]
CANNOT_ANSWER_DOC = "cannot_answer"
MIN_INTRO_CHARS = 40

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
MARTS_YML = REPO_ROOT / "dbt" / "models" / "marts" / "_marts.yml"


@dataclass(frozen=True)
class Chunk:
    id: str  # metric:<name>, doc:<file>#<section>, table:<name>, example:<id>
    kind: ChunkKind
    source: Source
    title: str
    text: str  # with {param} placeholders
    keywords: str  # names, synonyms and identifiers, weighted up in keyword matching


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def metric_chunks(path: Path) -> list[Chunk]:
    mf = load_metrics(path)
    out = []
    for m in mf.metrics:
        lines = [
            f"Metric `{m.name}`: {m.label}.",
            _one_line(m.description),
            f"Unit: {m.unit}. Table: {m.table} (time column {m.time_column}, default grain "
            f"{m.default_grain}).",
            f"SQL ({m.type}): {_one_line(m.sql)}",
        ]
        if m.caveats:
            lines.append("Caveats:")
            lines += [f"- {_one_line(mf.caveats[c])}" for c in m.caveats]
        if m.synonyms:
            lines.append("Also called: " + ", ".join(m.synonyms) + ".")
        keywords = " ".join([m.name, m.label, *m.synonyms])
        out.append(Chunk(f"metric:{m.name}", "metric", "metrics", m.label, "\n".join(lines),
                         keywords))  # fmt: skip
    return out


def _sections(markdown: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(doc title, text before the first ## section, [(section title, body)])."""
    title = ""
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            intro.append(line)
    return (
        title,
        "\n".join(intro).strip(),
        [(t, "\n".join(body).strip()) for t, body in sections],
    )


def doc_chunks(docs_dir: Path) -> list[Chunk]:
    out = []
    for path in sorted(docs_dir.glob("*.md")):
        name = path.stem
        source: Source = "cannot_answer" if name == CANNOT_ANSWER_DOC else "docs"
        title, intro, sections = _sections(path.read_text(encoding="utf-8"))
        title = title or name.replace("_", " ")
        parts = []
        if len(re.sub(r"\s", "", intro)) >= MIN_INTRO_CHARS:
            parts.append(("intro", title, intro))
        parts += [(slug(t), f"{title}: {t}", body) for t, body in sections if body]
        for section, heading, body in parts:
            idents = " ".join(sorted(set(re.findall(r"`([a-z_][a-z0-9_]*)`", body))))
            out.append(Chunk(f"doc:{name}#{section}", "doc", source, heading,
                             f"# {heading}\n{body}", f"{heading} {idents}"))  # fmt: skip
    return out


def table_chunks(marts_yml: Path) -> list[Chunk]:
    data = yaml.safe_load(marts_yml.read_text(encoding="utf-8"))
    out = []
    for model in data.get("models", []):
        name = model["name"]
        lines = [f"Table `{name}`: {_one_line(model.get('description', ''))}", "Columns:"]
        cols = model.get("columns", [])
        lines += [f"- {c['name']}: {_one_line(c.get('description', ''))}" for c in cols]
        keywords = " ".join([name, *(c["name"] for c in cols)])
        out.append(Chunk(f"table:{name}", "table", "tables", f"Table {name}", "\n".join(lines),
                         keywords))  # fmt: skip
    return out


def example_chunks(path: Path) -> list[Chunk]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for ex in data.get("examples", []):
        text = f"Example question: {ex['question']}\nSQL:\n{ex['sql'].strip()}"
        if ex.get("notes"):
            text += f"\nNotes: {_one_line(ex['notes'])}"
        out.append(Chunk(f"example:{ex['id']}", "example", "examples", ex["question"], text,
                         " ".join(ex.get("keywords", []))))  # fmt: skip
    return out


def all_chunks(knowledge_dir: Path = KNOWLEDGE_DIR, marts_yml: Path = MARTS_YML) -> list[Chunk]:
    """Every chunk from every source; ``search`` filters by the run's ``kb_sources``."""
    chunks = [
        *metric_chunks(knowledge_dir / "metrics.yml"),
        *doc_chunks(knowledge_dir / "docs"),
        *table_chunks(marts_yml),
        *example_chunks(knowledge_dir / "examples.yml"),
    ]
    ids = [c.id for c in chunks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate chunk ids {duplicates}")
    return chunks
