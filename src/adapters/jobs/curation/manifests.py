# SPDX-License-Identifier: MIT-0
"""Read and write the manifest that accompanies a landed delivery.

The adapter half of the landing contract: this module owns JSON, byte counts and digests, and
the rules that judge what it produces live in `domain/curation/landing.py`. Same split as
contract loading, and for the same reason — every rule stays unit testable without a file.

**Two directions, and they are not symmetric.** `read_manifest` parses what a producer wrote
and is the one that must be hostile: upstream is untrusted, so every value is narrowed from
`object` before it reaches the domain, and a missing key names itself rather than surfacing as
a `TypeError` three frames away. `build_manifest` is the opposite case — it stands in for a
producer when we land fixtures ourselves, so it takes the provenance a producer would know and
computes the facts a producer would compute.

**A landing declaration is not a manifest.** The sidecar files committed beside the fixtures
carry provenance only: which dataset, which options, which window, which watermark. They
deliberately do not carry `byte_count`, `checksum`, `declared_columns` or the object list,
because those are facts about bytes — a committed checksum would be stale the moment a fixture
is regenerated, and a committed column list would let the header assertion pass against a
column the file does not actually have.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from adapters.amc_csv import read_rows
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    LEGACY_MANIFEST_LAYOUT,
    LandedObject,
    LandingError,
    Manifest,
)
from domain.curation.mapping import options_hash
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.provenance import ProducerGovernance

__all__ = [
    "BuildManifestRequest",
    "build_manifest",
    "checksum_of",
    "declared_columns_of",
    "read_manifest",
    "write_manifest",
]

CHECKSUM_PREFIX = "sha256:"
"""Algorithm named in the value, not assumed by position.

A bare hex string is indistinguishable from a different algorithm's bare hex string, so a
producer switching digests would be silently accepted. Naming it means a change fails.
"""


def _mapping(value: object, *, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LandingError(f"manifest: {key} must be an object, got {type(value).__name__}.")
    return cast("dict[str, object]", value)


def _require(source: dict[str, object], key: str) -> object:
    if key not in source:
        raise LandingError(
            f"manifest: required key {key!r} is missing. Every manifest field is required — "
            "an optional provenance field is one that gets dropped, and its absence then "
            "reads downstream as a finding rather than as a gap."
        )
    return source[key]


def _text(source: dict[str, object], key: str) -> str:
    value = _require(source, key)
    if not isinstance(value, str):
        raise LandingError(f"manifest: {key} must be a string, got {type(value).__name__}.")
    return value


def _source_scope(source: dict[str, object]) -> SourceIdentifier:
    raw = _mapping(_require(source, "source_scope"), key="source_scope")
    required = {"namespace", "value"}
    if set(raw) != required:
        raise LandingError(
            f"manifest: source_scope requires exactly {sorted(required)}, got {sorted(raw)}."
        )
    try:
        return SourceIdentifier(_text(raw, "namespace"), _text(raw, "value"))
    except ValueError as error:
        raise LandingError(f"manifest: source_scope is invalid ({error}).") from error


def _whole(source: dict[str, object], key: str) -> int:
    value = _require(source, key)
    # bool is a subclass of int, and `byte_count: true` must not read as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise LandingError(f"manifest: {key} must be an integer, got {type(value).__name__}.")
    return value


def _day(source: dict[str, object], key: str) -> date:
    raw = _text(source, key)
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise LandingError(f"manifest: {key} is {raw!r}, which is not an ISO date.") from error


def _optional_day(source: dict[str, object], key: str) -> date | None:
    value = _require(source, key)
    if value is None:
        return None
    return _day(source, key)


def _moment(source: dict[str, object], key: str) -> datetime:
    raw = _text(source, key)
    try:
        # `Z` is legal ISO-8601 and legal in Python 3.11+, kept explicit so a producer using
        # it is not a special case somebody has to remember.
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise LandingError(f"manifest: {key} is {raw!r}, which is not an ISO timestamp.") from error
    if parsed.tzinfo is None:
        raise LandingError(
            f"manifest: {key} is {raw!r}, which carries no offset. A provenance instant "
            "without a zone is ambiguous by up to a day at a boundary."
        )
    return parsed


def _names(source: dict[str, object], key: str) -> tuple[str, ...]:
    value = _require(source, key)
    if not isinstance(value, list):
        raise LandingError(f"manifest: {key} must be a list, got {type(value).__name__}.")
    items = cast("list[object]", value)
    for item in items:
        if not isinstance(item, str):
            raise LandingError(f"manifest: {key} must hold strings, found {type(item).__name__}.")
    return tuple(cast("list[str]", items))


def _landed_object(entry: dict[str, object]) -> LandedObject:
    raw_version = entry.get("version_id")
    if raw_version is not None and not isinstance(raw_version, str):
        raise LandingError("manifest: objects[].version_id must be a string when supplied.")
    return LandedObject(
        name=_text(entry, "name"),
        byte_count=_whole(entry, "byte_count"),
        checksum=_text(entry, "checksum"),
        version_id=raw_version,
    )


def _objects(source: dict[str, object]) -> tuple[LandedObject, ...]:
    value = _require(source, "objects")
    if not isinstance(value, list):
        raise LandingError(f"manifest: objects must be a list, got {type(value).__name__}.")
    return tuple(
        _landed_object(entry)
        for entry in (_mapping(item, key="objects[]") for item in cast("list[object]", value))
    )


def _layout_version(source: dict[str, object]) -> int:
    value = source.get("layout_version", LEGACY_MANIFEST_LAYOUT)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LandingError("manifest: layout_version must be an integer when supplied.")
    if value not in {LEGACY_MANIFEST_LAYOUT, GOVERNED_MANIFEST_LAYOUT}:
        raise LandingError(f"manifest: unsupported layout_version {value}.")
    return value


def _governance(source: dict[str, object]) -> ProducerGovernance | None:
    value = source.get("governance")
    if value is None:
        return None
    block = _mapping(value, key="governance")
    required = {
        "query_pack_id",
        "query_pack_version",
        "query_pack_digest",
        "query_digest",
        "data_contract_id",
        "data_contract_digest",
        "source_pack_id",
        "source_pack_digest",
    }
    if set(block) != required:
        raise LandingError(
            f"manifest: governance requires exactly {sorted(required)}, got {sorted(block)}."
        )
    try:
        return ProducerGovernance(
            query_pack_id=_text(block, "query_pack_id"),
            query_pack_version=_whole(block, "query_pack_version"),
            query_pack_digest=_text(block, "query_pack_digest"),
            query_digest=_text(block, "query_digest"),
            data_contract_id=_text(block, "data_contract_id"),
            data_contract_digest=_text(block, "data_contract_digest"),
            source_pack_id=_text(block, "source_pack_id"),
            source_pack_digest=_text(block, "source_pack_digest"),
        )
    except ValueError as error:
        raise LandingError(f"manifest: governance is invalid ({error}).") from error


def read_manifest(text: str) -> Manifest:
    """Parse a landed manifest, letting the domain refuse anything it cannot verify.

    Constructed rather than assembled into something looser, so a manifest that could not have
    been produced by a conforming producer fails here instead of reaching the mapper.
    """
    try:
        parsed = cast("object", json.loads(text))
    except json.JSONDecodeError as error:
        raise LandingError(f"the manifest is not valid JSON: {error}") from error

    source = _mapping(parsed, key="the manifest")
    return Manifest(
        source_system=_text(source, "source_system"),
        source_dataset=_text(source, "source_dataset"),
        source_scope=_source_scope(source),
        option_set=_mapping(_require(source, "option_set"), key="option_set"),
        option_set_hash=_text(source, "option_set_hash"),
        marketplace=_text(source, "marketplace"),
        period_start=_day(source, "period_start"),
        period_end=_day(source, "period_end"),
        source_watermark=_optional_day(source, "source_watermark"),
        retrieved_at=_moment(source, "retrieved_at"),
        declared_columns=_names(source, "declared_columns"),
        objects=_objects(source),
        governance=_governance(source),
        layout_version=_layout_version(source),
    )


def write_manifest(manifest: Manifest) -> str:
    """Serialise a manifest to the JSON a producer would land.

    Sorted keys and a trailing newline, so landing the same delivery twice produces
    byte-identical text and a diff means something changed rather than that a dict reordered.
    """
    payload = {
        **(
            {"layout_version": manifest.layout_version}
            if manifest.layout_version != LEGACY_MANIFEST_LAYOUT
            else {}
        ),
        "source_system": manifest.source_system,
        "source_dataset": manifest.source_dataset,
        "source_scope": {
            "namespace": manifest.source_scope.namespace,
            "value": manifest.source_scope.value,
        },
        "option_set": dict(manifest.option_set),
        "option_set_hash": manifest.option_set_hash,
        "marketplace": manifest.marketplace,
        "period_start": manifest.period_start.isoformat(),
        "period_end": manifest.period_end.isoformat(),
        "source_watermark": (
            None if manifest.source_watermark is None else manifest.source_watermark.isoformat()
        ),
        "retrieved_at": manifest.retrieved_at.isoformat(),
        "declared_columns": list(manifest.declared_columns),
        "objects": [
            {
                "name": item.name,
                "byte_count": item.byte_count,
                "checksum": item.checksum,
                **({"version_id": item.version_id} if item.version_id is not None else {}),
            }
            for item in manifest.objects
        ],
        **(
            {"governance": manifest.governance.as_dict()} if manifest.governance is not None else {}
        ),
    }
    # `default=str` because a fixture's option set may hold a Decimal, which the reader
    # produces and json cannot serialise. Same reason `options_hash` needs it.
    return json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"


def checksum_of(body: bytes) -> str:
    """Digest a delivered object's bytes, with the algorithm named in the value."""
    return f"{CHECKSUM_PREFIX}{hashlib.sha256(body).hexdigest()}"


def declared_columns_of(text: str, *, source_format: str) -> tuple[str, ...]:
    """Read the column names a delivered object actually contains.

    Derived from the bytes and never from the contract, which is the only way it can be
    evidence. Taking it from the contract would make the header assertion compare a contract
    against itself and pass whatever arrived.

    Three shapes, matching the three readers. A CSV's header is its first line. A JSONL file's
    columns are the union of its records' keys. A vendor document's columns are the union of
    the keys of every repeating array's elements — found by shape rather than by name, because
    naming the array here would duplicate a contract's own knowledge of which one it reads.
    """
    if source_format == "csv":
        # Through AMC's dialect like every other AMC CSV read; only the header line is needed.
        header, _, _ = text.partition("\n")
        names = read_rows(header.strip())[:1]
        return tuple(name.strip() for row in names for name in row if name.strip())

    if source_format == "jsonl":
        found: set[str] = set()
        for line in text.splitlines():
            if line.strip():
                found |= set(_mapping(json.loads(line), key="a JSONL record"))
        return tuple(sorted(found))

    document = _mapping(json.loads(text), key="the report document")
    columns: set[str] = set()
    for value in document.values():
        if not isinstance(value, list):
            continue
        for item in cast("list[object]", value):
            if isinstance(item, dict):
                columns |= set(cast("dict[str, object]", item))
    return tuple(sorted(columns))


@dataclass(frozen=True, slots=True)
class BuildManifestRequest:
    """A landing declaration plus the facts about the bytes it accompanies.

    The split is the point. `declaration` is what a producer knows before it writes anything;
    everything else is measured from what it wrote.
    """

    declaration: dict[str, object]
    objects: tuple[LandedObject, ...]
    declared_columns: tuple[str, ...]


def build_manifest(request: BuildManifestRequest) -> Manifest:
    """Turn a landing declaration into the manifest a producer would write.

    `option_set_hash` is computed rather than declared, because the domain checks the hash
    against the option set and a declaration carrying both would just be two places to make
    the same mistake. It is computed with the *same* function the curated
    `report_options_hash` column uses, so a landing manifest and a partition cannot disagree
    about which pull a delivery is.
    """
    source = request.declaration
    option_set = _mapping(_require(source, "option_set"), key="option_set")
    return Manifest(
        source_system=_text(source, "source_system"),
        source_dataset=_text(source, "source_dataset"),
        source_scope=_source_scope(source),
        option_set=option_set,
        option_set_hash=options_hash(option_set),
        marketplace=_text(source, "marketplace"),
        period_start=_day(source, "period_start"),
        period_end=_day(source, "period_end"),
        source_watermark=_optional_day(source, "source_watermark"),
        retrieved_at=_moment(source, "retrieved_at"),
        declared_columns=request.declared_columns,
        objects=request.objects,
    )
