"""Detect which TikTok export format variant a parsed JSON document is (DESIGN §4.1).

The export format is not stable, so each known variant is described declaratively in
``VARIANTS``. Detection fails loudly with :class:`UnknownExportFormatError` when the shape is
not recognised. Error messages name **keys only, never values**, so they are safe to print
for real exports.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_KEYS_IN_ERROR = 40


class Dataset(StrEnum):
    WATCH = "watch"
    LIKE = "like"
    FAVORITE = "favorite"
    SEARCH = "search"
    SHARE = "share"


class UnknownExportFormatError(ValueError):
    """The export does not match any known format variant."""


@dataclass(frozen=True)
class FieldSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()

    @property
    def known(self) -> tuple[str, ...]:
        return self.required + self.optional


# Canonical field names per dataset. Shape-level check: across all records of a list, every
# key must map to a known field, and each required field must appear at least once.
# Individual records missing a field are rejected later, not treated as a shape error.
FIELDS: dict[Dataset, FieldSpec] = {
    Dataset.WATCH: FieldSpec(required=("date", "link")),
    Dataset.LIKE: FieldSpec(required=("date", "link")),
    Dataset.FAVORITE: FieldSpec(required=("date", "link")),
    Dataset.SEARCH: FieldSpec(required=("date", "search_term")),
    Dataset.SHARE: FieldSpec(required=("date",), optional=("link", "method", "shared_content")),
}

# Normalised record key (lower-case, alphanumerics only) -> canonical field name.
FIELD_ALIASES: dict[str, str] = {
    "date": "date",
    "link": "link",
    "searchterm": "search_term",
    "method": "method",
    "sharedcontent": "shared_content",
}


@dataclass(frozen=True)
class VariantSpec:
    name: str
    root: str
    paths: Mapping[Dataset, tuple[str, str]]  # dataset -> (section key, list key) under root


_COMMON_PATHS: dict[Dataset, tuple[str, str]] = {
    Dataset.LIKE: ("Like List", "ItemFavoriteList"),
    Dataset.FAVORITE: ("Favorite Videos", "FavoriteVideoList"),
    Dataset.SEARCH: ("Search History", "SearchList"),
    Dataset.SHARE: ("Share History", "ShareHistoryList"),
}

VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec(
        name="v1_activity",
        root="Activity",
        paths={Dataset.WATCH: ("Video Browsing History", "VideoList"), **_COMMON_PATHS},
    ),
    VariantSpec(
        name="v2_your_activity",
        root="Your Activity",
        paths={Dataset.WATCH: ("Watch History", "VideoList"), **_COMMON_PATHS},
    ),
)
VARIANT_NAMES: tuple[str, ...] = tuple(v.name for v in VARIANTS)


def variant_by_name(name: str) -> VariantSpec:
    for spec in VARIANTS:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown variant {name!r}; known: {', '.join(VARIANT_NAMES)}")


@dataclass
class DatasetSection:
    dataset: Dataset
    path: tuple[str, ...]
    records: list[dict[str, Any]]
    key_map: dict[str, str]  # record key as found -> canonical field name

    def canonical(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {self.key_map[k]: v for k, v in record.items()}


@dataclass
class DetectedExport:
    variant: str
    sections: dict[Dataset, DatasetSection]
    absent: list[Dataset] = field(default_factory=list)
    ignored_top_level: list[str] = field(default_factory=list)


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _fmt_keys(keys: Any) -> str:
    names = sorted(str(k) for k in keys)
    shown = names[:MAX_KEYS_IN_ERROR]
    suffix = f", … (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    return "[" + ", ".join(repr(k) for k in shown) + suffix + "]"


def _path_str(path: tuple[str, ...]) -> str:
    return " > ".join(path)


def _extract_section(
    root: Mapping[str, Any], spec: VariantSpec, dataset: Dataset
) -> DatasetSection | None:
    section_key, list_key = spec.paths[dataset]
    path = (spec.root, section_key, list_key)
    if section_key not in root:
        return None
    section = root[section_key]
    if section is None:
        return DatasetSection(dataset, path, [], {})
    if not isinstance(section, Mapping):
        raise UnknownExportFormatError(
            f"{_path_str(path[:2])}: expected an object, found {type(section).__name__}"
        )
    if list_key not in section:
        raise UnknownExportFormatError(
            f"{_path_str(path[:2])}: missing list key {list_key!r}. "
            f"Keys found: {_fmt_keys(section.keys())}"
        )
    items = section[list_key]
    if items is None:
        return DatasetSection(dataset, path, [], {})
    if not isinstance(items, list):
        raise UnknownExportFormatError(
            f"{_path_str(path)}: expected a list, found {type(items).__name__}"
        )

    found_keys: dict[str, None] = {}  # ordered set of every key across records
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise UnknownExportFormatError(
                f"{_path_str(path)}[{index}]: expected an object, found {type(item).__name__}"
            )
        for key in item:
            found_keys.setdefault(key, None)

    spec_fields = FIELDS[dataset]
    key_map: dict[str, str] = {}
    unknown: list[str] = []
    for key in found_keys:
        canonical = FIELD_ALIASES.get(_normalise_key(str(key)))
        if canonical is None or canonical not in spec_fields.known:
            unknown.append(key)
        else:
            key_map[key] = canonical
    if unknown:
        raise UnknownExportFormatError(
            f"{_path_str(path)}: unrecognised record keys {_fmt_keys(unknown)}. "
            f"Keys found: {_fmt_keys(found_keys)}. "
            f"Expected fields (case-insensitive): {list(spec_fields.known)}"
        )
    if items:
        missing = [f for f in spec_fields.required if f not in key_map.values()]
        if missing:
            raise UnknownExportFormatError(
                f"{_path_str(path)}: no record has required field(s) {missing}. "
                f"Keys found: {_fmt_keys(found_keys)}"
            )
    return DatasetSection(dataset, path, list(items), key_map)


def detect(document: Any) -> DetectedExport:
    """Identify the format variant and extract every known dataset section."""
    if not isinstance(document, Mapping):
        raise UnknownExportFormatError(
            f"Unrecognised TikTok export: top level is {type(document).__name__}, expected an "
            "object"
        )

    matches = [spec for spec in VARIANTS if spec.root in document]
    if not matches:
        roots = ", ".join(f"{s.root!r} ({s.name})" for s in VARIANTS)
        raise UnknownExportFormatError(
            "Unrecognised TikTok export: no known activity root. "
            f"Top-level keys found: {_fmt_keys(document.keys())}. Expected one of: {roots}"
        )
    if len(matches) > 1:
        raise UnknownExportFormatError(
            "Ambiguous TikTok export: several activity roots present "
            f"{_fmt_keys(s.root for s in matches)}"
        )

    spec = matches[0]
    root = document[spec.root]
    if not isinstance(root, Mapping):
        raise UnknownExportFormatError(
            f"{spec.root!r}: expected an object, found {type(root).__name__}"
        )

    watch_section_key = spec.paths[Dataset.WATCH][0]
    if watch_section_key not in root:
        raise UnknownExportFormatError(
            f"Unrecognised TikTok export: {spec.root!r} has no watch history section "
            f"{watch_section_key!r}. Keys found under {spec.root!r}: {_fmt_keys(root.keys())}"
        )

    sections: dict[Dataset, DatasetSection] = {}
    absent: list[Dataset] = []
    for dataset in Dataset:
        extracted = _extract_section(root, spec, dataset)
        if extracted is None:
            absent.append(dataset)
        else:
            sections[dataset] = extracted

    known_sections = {section for section, _ in spec.paths.values()}
    ignored = [k for k in document if k != spec.root] + [
        f"{spec.root} > {k}" for k in root if k not in known_sections
    ]
    return DetectedExport(
        variant=spec.name, sections=sections, absent=absent, ignored_top_level=ignored
    )
