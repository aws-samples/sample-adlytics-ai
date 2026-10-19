# SPDX-License-Identifier: MIT-0
"""ADR-051 fixture classification, evidence bounds, coverage, and privacy validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import yaml

from adapters.amc_csv import AmcCsvEscapeError, dict_rows
from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog

PROVENANCE_PATH = Path("contracts/shared/fixture-provenance.yaml")
_IGNORED = frozenset({".DS_Store", ".gitkeep"})
_ASIN = re.compile(r"\bB0[A-Z0-9]{8}\b")
_AMC_INSTANCE = re.compile(r"\bamc[a-z0-9]{8}\b")
_AMC_ENTITY = re.compile(r"\bENTITY[A-Z0-9]+\b")


class FixtureProvenanceError(ValueError):
    """Fixture evidence is unclassified, overclaimed, or has an unsafe identity."""


class FixtureProvenanceClass(StrEnum):
    """The five bounded fixture evidence classes accepted by ADR-051."""

    SCHEMA_DERIVED = "schema_derived"
    SOURCE_INVARIANT_SYNTHETIC = "source_invariant_synthetic"
    CONSISTENCY_SCENARIO = "consistency_scenario"
    PRESENTATION_SYNTHETIC = "presentation_synthetic"
    LIVE_INTEGRATION = "live_integration"


@dataclass(frozen=True, slots=True)
class FixtureEvidenceEntry:
    """One artifact group with exactly one evidence class and owner."""

    entry_id: str
    evidence_class: FixtureProvenanceClass
    patterns: tuple[str, ...]
    owner_key: str
    owner: str


@dataclass(frozen=True, slots=True)
class FixtureEvidenceManifest:
    """Every managed fixture root and its exhaustive evidence entries."""

    managed_roots: tuple[str, ...]
    entries: tuple[FixtureEvidenceEntry, ...]


type RawMapping = dict[str, object]


def _mapping(value: object, label: str) -> RawMapping:
    if not isinstance(value, dict):
        raise FixtureProvenanceError(f"{label} must be a mapping.")
    return cast("RawMapping", value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise FixtureProvenanceError(f"{label} must be a list.")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureProvenanceError(f"{label} must be a non-empty string.")
    return value.strip()


def load_fixture_evidence_manifest(root: Path) -> FixtureEvidenceManifest:
    """Load and validate the exhaustive fixture-evidence declaration."""
    path = root / PROVENANCE_PATH
    raw = _mapping(cast("object", yaml.safe_load(path.read_text())), str(path))
    if raw.get("version") != 1:
        raise FixtureProvenanceError(f"{path}: version must be 1.")
    entries = tuple(_entry(raw_entry) for raw_entry in _list(raw.get("entries"), "entries"))
    ids = tuple(item.entry_id for item in entries)
    if len(set(ids)) != len(ids):
        raise FixtureProvenanceError("fixture evidence entry ids must be unique.")
    return FixtureEvidenceManifest(
        managed_roots=tuple(
            _text(item, "managed root") for item in _list(raw.get("managed_roots"), "managed_roots")
        ),
        entries=entries,
    )


def _entry(raw_entry: object) -> FixtureEvidenceEntry:
    entry = _mapping(raw_entry, "entries[]")
    try:
        evidence_class = FixtureProvenanceClass(_text(entry.get("class"), "entry class"))
    except ValueError as error:
        raise FixtureProvenanceError("fixture entry names an unknown provenance class.") from error
    owner_key = {
        FixtureProvenanceClass.SCHEMA_DERIVED: "owner",
        FixtureProvenanceClass.SOURCE_INVARIANT_SYNTHETIC: "evidence",
        FixtureProvenanceClass.CONSISTENCY_SCENARIO: "scenario_rule",
        FixtureProvenanceClass.PRESENTATION_SYNTHETIC: "presentation_owner",
        FixtureProvenanceClass.LIVE_INTEGRATION: "validation_result",
    }[evidence_class]
    patterns = tuple(
        _text(item, "entry pattern") for item in _list(entry.get("patterns"), "patterns")
    )
    if evidence_class is FixtureProvenanceClass.LIVE_INTEGRATION and patterns:
        raise FixtureProvenanceError("live integration evidence cannot name a committed payload.")
    if evidence_class is not FixtureProvenanceClass.LIVE_INTEGRATION and not patterns:
        raise FixtureProvenanceError(f"{evidence_class.value} requires committed artifacts.")
    return FixtureEvidenceEntry(
        entry_id=_text(entry.get("id"), "entry id"),
        evidence_class=evidence_class,
        patterns=patterns,
        owner_key=owner_key,
        owner=_text(entry.get(owner_key), owner_key),
    )


def validate_fixture_provenance(
    root: Path,
    manifest: FixtureEvidenceManifest,
    catalog: SyntheticEntityCatalog,
) -> None:
    """Require one bounded evidence class per artifact and safe fictional identities."""
    coverage = _coverage(root, manifest)
    managed = _managed_files(root, manifest.managed_roots)
    _require_complete_coverage(root, managed, coverage)
    _scan_identities(root, managed, catalog)
    _require_generated_class(root, coverage)


def _coverage(
    root: Path, manifest: FixtureEvidenceManifest
) -> dict[Path, list[FixtureEvidenceEntry]]:
    coverage: dict[Path, list[FixtureEvidenceEntry]] = {}
    for entry in manifest.entries:
        for pattern in entry.patterns:
            matches = tuple(path for path in root.glob(pattern) if path.is_file())
            if not matches:
                raise FixtureProvenanceError(
                    f"{entry.entry_id}: pattern {pattern!r} matches no committed artifact."
                )
            for path in matches:
                coverage.setdefault(path.resolve(), []).append(entry)
    return coverage


def _managed_files(root: Path, relative_roots: tuple[str, ...]) -> set[Path]:
    managed: set[Path] = set()
    for relative in relative_roots:
        target = root / relative
        if not target.is_dir():
            raise FixtureProvenanceError(f"managed fixture root is missing: {relative}.")
        managed.update(
            path.resolve()
            for path in target.rglob("*")
            if path.is_file() and path.name not in _IGNORED and not path.name.startswith(".")
        )
    return managed


def _require_complete_coverage(
    root: Path,
    managed: set[Path],
    coverage: dict[Path, list[FixtureEvidenceEntry]],
) -> None:
    missing = sorted(str(path.relative_to(root)) for path in managed - set(coverage))
    repeated = sorted(
        str(path.relative_to(root)) for path, entries in coverage.items() if len(entries) != 1
    )
    outside = sorted(str(path.relative_to(root)) for path in set(coverage) - managed)
    if missing or repeated or outside:
        raise FixtureProvenanceError(
            f"fixture provenance coverage is invalid; missing={missing}, repeated={repeated}, "
            f"outside={outside}."
        )


@dataclass(frozen=True, slots=True)
class _CsvIdentityPolicy:
    campaign_ids: frozenset[str]
    campaign_names: frozenset[str]
    advertiser_ids: frozenset[str]
    advertiser_names: frozenset[str]


def _scan_identities(root: Path, managed: set[Path], catalog: SyntheticEntityCatalog) -> None:
    allowed_asins = catalog.asins
    policy = _CsvIdentityPolicy(
        campaign_ids=frozenset(
            {value for campaign in catalog.campaigns for value in campaign.source_aliases.values()}
            | set(catalog.trap_campaign_ids)
        ),
        campaign_names=frozenset(
            {name.value for campaign in catalog.campaigns for name in campaign.names}
            | set(catalog.unnamed_campaign_names)
        ),
        advertiser_ids=frozenset(
            value
            for _name, aliases in catalog.advertiser_names.values()
            for value in aliases.values()
        ),
        advertiser_names=frozenset(name for name, _aliases in catalog.advertiser_names.values()),
    )
    for path in sorted(managed):
        text = _fixture_text(root, path)
        for asin in _ASIN.findall(text):
            if asin not in allowed_asins and not asin.startswith(catalog.asin_prefixes):
                raise FixtureProvenanceError(
                    f"{path.relative_to(root)} contains undeclared ASIN {asin}."
                )
        if _AMC_INSTANCE.search(text) or _AMC_ENTITY.search(text):
            relative = path.relative_to(root)
            raise FixtureProvenanceError(
                f"{relative} contains a live-looking AMC account or instance identity."
            )
        if path.suffix != ".csv":
            continue
        try:
            _header, rows = dict_rows(text)
        except AmcCsvEscapeError as error:
            raise FixtureProvenanceError(f"{path.relative_to(root)}: {error}") from error
        for row in rows:
            _require_fictional_csv_identity(
                path.relative_to(root),
                cast("dict[str, str | None]", row),
                policy,
            )


def _require_fictional_csv_identity(
    relative: Path,
    row: dict[str, str | None],
    policy: _CsvIdentityPolicy,
) -> None:
    campaign_id = row.get("campaign_id_string") or ""
    campaign_name = row.get("campaign") or ""
    advertiser_id = row.get("advertiser_id") or ""
    advertiser_name = row.get("advertiser") or ""
    if campaign_id and campaign_id not in policy.campaign_ids:
        raise FixtureProvenanceError(
            f"{relative} contains undeclared campaign identity {campaign_id}."
        )
    if campaign_name and campaign_name not in policy.campaign_names:
        raise FixtureProvenanceError(
            f"{relative} contains undeclared campaign name {campaign_name}."
        )
    if advertiser_id and advertiser_id not in policy.advertiser_ids:
        raise FixtureProvenanceError(
            f"{relative} contains undeclared advertiser identity {advertiser_id}."
        )
    if advertiser_name and advertiser_name not in policy.advertiser_names:
        raise FixtureProvenanceError(
            f"{relative} contains undeclared advertiser name {advertiser_name}."
        )


def _fixture_text(root: Path, path: Path) -> str:
    try:
        return path.read_text()
    except UnicodeDecodeError as error:
        raise FixtureProvenanceError(
            f"{path.relative_to(root)} is not an approved text fixture format."
        ) from error


def _require_generated_class(root: Path, coverage: dict[Path, list[FixtureEvidenceEntry]]) -> None:
    generated = (root / "tests/fixtures/generated").resolve()
    for path, entries in coverage.items():
        if generated not in path.parents:
            continue
        if entries[0].evidence_class is not FixtureProvenanceClass.SCHEMA_DERIVED:
            raise FixtureProvenanceError(
                f"{path.relative_to(root)} is generated but is not schema_derived."
            )
