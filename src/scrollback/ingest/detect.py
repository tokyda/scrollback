"""Detect which TikTok export format variant a parsed JSON document is (DESIGN §4.1).

The export format is not stable, so each known variant is described declaratively in
``VARIANTS``. Detection fails loudly with :class:`UnknownExportFormatError` when the structure
is not recognised or a required field is absent. Unknown *extra* record fields are tolerated:
they are logged as a warning (key names only) and carried through to an ``extras`` column.
Error and warning messages name **keys only, never values**, so they are safe to print for
real exports.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_KEYS_IN_ERROR = 40

logger = logging.getLogger(__name__)


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
    """One export shape. ``paths`` run from the top of the document to each dataset's list:
    ``(top-level key, ..., section key, list key)``, so a dataset may live outside the activity
    root. ``markers`` are further top-level keys the variant requires; when several variants
    match, the one with the most markers wins (it is the more specific shape)."""

    name: str
    root: str
    paths: Mapping[Dataset, tuple[str, ...]]
    markers: tuple[str, ...] = ()


def _under(root: str, paths: Mapping[Dataset, tuple[str, str]]) -> dict[Dataset, tuple[str, ...]]:
    return {dataset: (root, *path) for dataset, path in paths.items()}


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
        paths=_under(
            "Activity", {Dataset.WATCH: ("Video Browsing History", "VideoList"), **_COMMON_PATHS}
        ),
    ),
    VariantSpec(
        name="v2_your_activity",
        root="Your Activity",
        paths=_under(
            "Your Activity", {Dataset.WATCH: ("Watch History", "VideoList"), **_COMMON_PATHS}
        ),
    ),
    # Seen in the first real export (2026-11): likes and favourites moved to a top-level
    # "Likes and Favorites" object, and searches to "Your Activity > Searches".
    VariantSpec(
        name="v3_likes_favorites",
        root="Your Activity",
        markers=("Likes and Favorites",),
        paths={
            Dataset.WATCH: ("Your Activity", "Watch History", "VideoList"),
            Dataset.LIKE: ("Likes and Favorites", "Like List", "ItemFavoriteList"),
            Dataset.FAVORITE: ("Likes and Favorites", "Favorite Videos", "FavoriteVideoList"),
            Dataset.SEARCH: ("Your Activity", "Searches", "SearchList"),
            Dataset.SHARE: ("Your Activity", "Share History", "ShareHistoryList"),
        },
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
    key_map: dict[str, str]  # known record key as found -> canonical field name
    unknown_keys: list[str] = field(default_factory=list)  # extra keys, kept as extras

    def canonical(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Known fields under their canonical names."""
        return {self.key_map[k]: v for k, v in record.items() if k in self.key_map}

    def extras(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Unknown fields under their original names (empty when there are none)."""
        return {k: v for k, v in record.items() if k not in self.key_map}


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
    document: Mapping[str, Any], spec: VariantSpec, dataset: Dataset
) -> DatasetSection | None:
    path = tuple(spec.paths[dataset])
    *parents, section_key, list_key = path
    container: Any = document
    for depth, key in enumerate(parents):
        if not isinstance(container, Mapping):
            raise UnknownExportFormatError(
                f"{_path_str(tuple(parents[:depth]))}: expected an object, "
                f"found {type(container).__name__}"
            )
        if key not in container or container[key] is None:
            return None
        container = container[key]
    if not isinstance(container, Mapping):
        raise UnknownExportFormatError(
            f"{_path_str(tuple(parents))}: expected an object, found {type(container).__name__}"
        )
    if section_key not in container:
        return None
    section = container[section_key]
    section_path = (*parents, section_key)
    if section is None:
        return DatasetSection(dataset, path, [], {})
    if not isinstance(section, Mapping):
        raise UnknownExportFormatError(
            f"{_path_str(section_path)}: expected an object, found {type(section).__name__}"
        )
    if list_key not in section:
        raise UnknownExportFormatError(
            f"{_path_str(section_path)}: missing list key {list_key!r}. "
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
        logger.warning(
            "%s: unrecognised record keys %s; stored in the extras column. "
            "Expected fields (case-insensitive): %s",
            _path_str(path),
            _fmt_keys(unknown),
            list(spec_fields.known),
        )
    if items:
        missing = [f for f in spec_fields.required if f not in key_map.values()]
        if missing:
            raise UnknownExportFormatError(
                f"{_path_str(path)}: no record has required field(s) {missing}. "
                f"Keys found: {_fmt_keys(found_keys)}"
            )
    return DatasetSection(dataset, path, list(items), key_map, [str(k) for k in unknown])


def _find_key(
    node: Any, key: str, path: tuple[str, ...] = (), depth: int = 0
) -> list[tuple[str, ...]]:
    """Paths (keys only) where ``key`` occurs as an object key, a few levels deep."""
    if depth > 4 or not isinstance(node, Mapping):
        return []
    found = [(*path, key)] if key in node else []
    for k, v in node.items():
        if isinstance(v, Mapping):
            found += _find_key(v, key, (*path, str(k)), depth + 1)
    return found


def _check_not_moved(document: Mapping[str, Any], spec: VariantSpec, absent: list[Dataset]) -> None:
    """An absent dataset whose list key exists somewhere else means the export moved it: fail
    loudly rather than load without it (the first real export moved likes and searches)."""
    for dataset in absent:
        list_key = spec.paths[dataset][-1]
        elsewhere = _find_key(document, list_key)
        if elsewhere:
            raise UnknownExportFormatError(
                f"{spec.name}: {dataset.value} expected at {_path_str(spec.paths[dataset])}, "
                f"but {list_key!r} was found at {_path_str(elsewhere[0])}. The export layout "
                "changed: add a variant to VARIANTS in ingest/detect.py"
            )


def _pick_variant(document: Mapping[str, Any]) -> VariantSpec:
    matches = [
        spec for spec in VARIANTS
        if spec.root in document and all(m in document for m in spec.markers)
    ]  # fmt: skip
    if not matches:
        roots = ", ".join(sorted({repr(s.root) for s in VARIANTS}))
        raise UnknownExportFormatError(
            "Unrecognised TikTok export: no known activity root. "
            f"Top-level keys found: {_fmt_keys(document.keys())}. Expected one of: {roots}"
        )
    if len({s.root for s in matches}) > 1:
        raise UnknownExportFormatError(
            "Ambiguous TikTok export: several activity roots present "
            f"{_fmt_keys({s.root for s in matches})}"
        )
    most = max(len(s.markers) for s in matches)
    best = [s for s in matches if len(s.markers) == most]
    if len(best) > 1:
        raise UnknownExportFormatError(
            f"Ambiguous TikTok export: matches variants {_fmt_keys(s.name for s in best)}"
        )
    return best[0]


def detect(document: Any) -> DetectedExport:
    """Identify the format variant and extract every known dataset section."""
    if not isinstance(document, Mapping):
        raise UnknownExportFormatError(
            f"Unrecognised TikTok export: top level is {type(document).__name__}, expected an "
            "object"
        )

    spec = _pick_variant(document)
    root = document[spec.root]
    if not isinstance(root, Mapping):
        raise UnknownExportFormatError(
            f"{spec.root!r}: expected an object, found {type(root).__name__}"
        )

    watch_section_key = spec.paths[Dataset.WATCH][1]
    if watch_section_key not in root:
        raise UnknownExportFormatError(
            f"Unrecognised TikTok export: {spec.root!r} has no watch history section "
            f"{watch_section_key!r}. Keys found under {spec.root!r}: {_fmt_keys(root.keys())}"
        )

    sections: dict[Dataset, DatasetSection] = {}
    absent: list[Dataset] = []
    for dataset in Dataset:
        extracted = _extract_section(document, spec, dataset)
        if extracted is None:
            absent.append(dataset)
        else:
            sections[dataset] = extracted

    _check_not_moved(document, spec, absent)

    used_tops = {path[0] for path in spec.paths.values()}
    used_sections = {path[:2] for path in spec.paths.values()}
    ignored = [k for k in document if k not in used_tops]
    for top in (k for k in document if k in used_tops):
        if isinstance(document[top], Mapping):
            ignored += [f"{top} > {k}" for k in document[top] if (top, k) not in used_sections]
    return DetectedExport(
        variant=spec.name, sections=sections, absent=absent, ignored_top_level=ignored
    )
