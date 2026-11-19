"""Validate and normalise records from a detected export (DESIGN §4.2 steps 2-3).

Each record becomes either a :class:`ParsedRecord` or a :class:`Reject` with a reason code.
Timestamps are kept exactly as written in the export (source timezone, naive); timezone
conversion happens in dbt staging, so changing ``SOURCE_TZ`` never changes a record's hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from scrollback.ingest.detect import Dataset, DetectedExport

EXPORT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
VIDEO_ID_RE = re.compile(r"/video/(\d+)")
VIDEO_DATASETS = frozenset({Dataset.WATCH, Dataset.LIKE, Dataset.FAVORITE})


class RejectReason(StrEnum):
    MISSING_DATE = "missing_date"
    INVALID_DATE = "invalid_date"
    MISSING_LINK = "missing_link"
    NO_VIDEO_ID = "no_video_id"
    MISSING_SEARCH_TERM = "missing_search_term"


def extract_video_id(link: str | None) -> str | None:
    """Return the numeric video ID from a TikTok link, or ``None``."""
    if not link:
        return None
    match = VIDEO_ID_RE.search(link)
    return match.group(1) if match else None


def normalise_search_term(term: str) -> str:
    """NFKC, case-fold, trim and collapse internal whitespace."""
    return " ".join(unicodedata.normalize("NFKC", term).casefold().split())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _fail(reason: RejectReason, message: str) -> PydanticCustomError:
    return PydanticCustomError(reason.value, message)


class ParsedRecord(BaseModel):
    """A validated record. Build with ``ParsedRecord.model_validate({dataset, **canonical})``."""

    model_config = ConfigDict(frozen=True)

    dataset: Dataset
    event_ts_raw: str
    event_ts: datetime  # naive, in the export's source timezone
    link: str | None = None
    video_id: str | None = None
    search_term: str | None = None
    method: str | None = None
    shared_content: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_canonical(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "event_ts" in data:
            return data
        dataset = Dataset(data["dataset"])

        raw_date = data.get("date")
        if raw_date is None or (isinstance(raw_date, str) and not raw_date.strip()):
            raise _fail(RejectReason.MISSING_DATE, "record has no date")
        if not isinstance(raw_date, str) or not _DATE_RE.fullmatch(raw_date.strip()):
            raise _fail(RejectReason.INVALID_DATE, "date is not 'YYYY-MM-DD HH:MM:SS'")
        try:
            event_ts = datetime.strptime(raw_date.strip(), EXPORT_DATE_FORMAT)
        except ValueError:
            raise _fail(RejectReason.INVALID_DATE, "date is not a real calendar time") from None

        link = _clean_str(data.get("link"))
        video_id = extract_video_id(link)
        if dataset in VIDEO_DATASETS:
            if link is None:
                raise _fail(RejectReason.MISSING_LINK, "record has no link")
            if video_id is None:
                raise _fail(RejectReason.NO_VIDEO_ID, "link has no /video/<id>")

        search_term = _clean_str(data.get("search_term"))
        if dataset is Dataset.SEARCH and search_term is None:
            raise _fail(RejectReason.MISSING_SEARCH_TERM, "record has no search term")

        return {
            "dataset": dataset,
            "event_ts_raw": raw_date,
            "event_ts": event_ts,
            "link": link,
            "video_id": video_id,
            "search_term": search_term,
            "method": _clean_str(data.get("method")),
            "shared_content": _clean_str(data.get("shared_content")),
        }

    @property
    def natural_key(self) -> str:
        if self.dataset in VIDEO_DATASETS:
            return self.video_id or ""
        if self.dataset is Dataset.SEARCH:
            return normalise_search_term(self.search_term or "")
        return self.video_id or self.link or self.shared_content or ""

    @property
    def record_hash(self) -> str:
        """sha256(dataset|canonical timestamp|video_id or normalised term) — DESIGN §4.2."""
        ts = self.event_ts.strftime(EXPORT_DATE_FORMAT)
        return _sha256(f"{self.dataset.value}|{ts}|{self.natural_key}")


@dataclass(frozen=True)
class Reject:
    dataset: Dataset
    reason: RejectReason
    payload: dict[str, Any]  # the record exactly as found in the export

    @property
    def payload_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, ensure_ascii=False, default=str)

    @property
    def reject_hash(self) -> str:
        return _sha256(f"{self.dataset.value}|{self.reason.value}|{self.payload_json}")


@dataclass
class ParseResult:
    variant: str
    records: dict[Dataset, list[ParsedRecord]] = field(default_factory=dict)
    rejects: list[Reject] = field(default_factory=list)
    read_counts: Counter[Dataset] = field(default_factory=Counter)


def parse_record(dataset: Dataset, canonical: dict[str, Any]) -> ParsedRecord | RejectReason:
    try:
        return ParsedRecord.model_validate({"dataset": dataset, **canonical})
    except ValidationError as exc:
        code = exc.errors()[0]["type"]
        try:
            return RejectReason(code)
        except ValueError:
            raise exc from None


def parse_export(detected: DetectedExport) -> ParseResult:
    result = ParseResult(variant=detected.variant)
    for dataset in Dataset:
        result.records[dataset] = []
        section = detected.sections.get(dataset)
        if section is None:
            continue
        for raw in section.records:
            result.read_counts[dataset] += 1
            parsed = parse_record(dataset, section.canonical(raw))
            if isinstance(parsed, RejectReason):
                result.rejects.append(Reject(dataset, parsed, dict(raw)))
            else:
                result.records[dataset].append(parsed)
    return result
