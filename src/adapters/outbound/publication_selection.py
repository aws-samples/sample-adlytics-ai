# SPDX-License-Identifier: MIT-0
"""Select exact Semantic V2 CURRENT publications for validated logical measurement plans."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final, cast

from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import Report, report_error
from domain.catalog.freshness import Coverage
from domain.data_contracts.contract import DataContract
from domain.measurement.answers import LENS_DEFAULTS
from domain.measurement.pack_coverage import (
    PackDayVersions,
    pack_version_from_evidence,
    pack_versioned_columns,
)
from domain.measurement.planning import QueryPlan
from domain.measurement.publications import BoundQueryPlan
from domain.producer.models import DEFAULT_CADENCE_LAG_DAYS
from domain.semantics.calendar_periods import CalendarPeriod, is_complete_period
from domain.semantics.commerce_identity import (
    CommerceIdentityBridge,
    ResolveCommerceIdentityRequest,
    resolve_commerce_identity,
)
from domain.semantics.identity import (
    AdProductKey,
    ChannelGroupKey,
    ChannelKey,
    EcosystemKey,
    MarketplaceKey,
    PlatformKey,
    ProductKey,
    SourceIdentifier,
)
from domain.semantics.media_taxonomy import MediaTaxonomy, MediaTaxonomyKey
from domain.semantics.publication import (
    PACK_VARIANT_OPTION,
    PERIOD_GRAIN_OPTION,
    PartitionIdentity,
    PublicationScope,
    SuccessfulPublication,
    lens_source_options,
    publication_lens_for,
)
from domain.semantics.publication import coverage_from as publication_coverage_from
from domain.semantics.source_pack import SourcePack
from domain.semantics.time import (
    BusinessCalendarKey,
    FactTimeClass,
    MeasurementBucket,
    ObservationTimeBasis,
    TimeGrain,
)
from domain.shared_kernel.canonical import canonical_model_digest
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from domain.shared_kernel.provenance import FreshnessStatus
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionRequest,
    PresentationEvidenceInspectionResult,
    PresentationEvidenceInspectionSlot,
    PublicationSelection,
    PublicationSelectionExactRequest,
    PublicationSelectionRequest,
)
from ports.outbound.semantic_state import (
    MAX_EXACT_CURRENT_PARTITIONS,
    CurrentPublicationInspectionPort,
    ScopeHorizonPolicy,
)
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

__all__ = [
    "MAX_PRESET_SNAPSHOT_PARTITIONS",
    "PerPackPresentationEvidenceInspection",
    "PopulationCoverage",
    "PublicationScopeRequest",
    "PublicationSelector",
    "coverage_by_population",
    "publication_digest",
    "publication_scopes_for",
]

SELECTION_FAILED = ErrorCode("PUBLICATION_SELECTION", 1)
SELECTION_TOO_LARGE = ErrorCode("PUBLICATION_SELECTION", 2)
# Product policy for one per-pack artifact-bake snapshot. Each pack is attested by
# `inspect_exact`, the chunked two-pass verified read (heads, publications, heads again) that
# Publication State bounds at MAX_EXACT_CURRENT_PARTITIONS (ADR-070, 2026-10-25 amendment), so
# one pack's snapshot is one instant at any size up to that bound.
MAX_PRESET_SNAPSHOT_PARTITIONS = MAX_EXACT_CURRENT_PARTITIONS
PACK_SNAPSHOT_TOO_LARGE = ErrorCode("PUBLICATION_SELECTION", 3)
PIN_NOT_MEASURED = ErrorCode("PUBLICATION_SELECTION", 4)
"""A customer-pinned media value no writer of the measure serves (`_pin_refusal`)."""
_LOGGER: Final = Logger(service="adlytics-publication-selection")
_COVERAGE_DIAGNOSIS: Final = "publication_selection.coverage_diagnosis"
_MEDIA_PINS: Final[Mapping[str, tuple[Callable[[str], MediaTaxonomyKey], str]]] = {
    "ecosystem_key": (EcosystemKey, "an ecosystem"),
    "platform_key": (PlatformKey, "a platform"),
    "channel_group_key": (ChannelGroupKey, "a channel group"),
    "channel_key": (ChannelKey, "a channel"),
    "ad_product_key": (AdProductKey, "an ad product"),
}
"""The media taxonomy columns a customer pins, each with its key type and refusal noun."""


@dataclass(frozen=True, slots=True)
class _FactSide:
    fact: str
    source_datasets: tuple[str, ...]
    filters: dict[str, str]
    window: DateRange
    measures: tuple[str, ...]
    """The columns this side aggregates; a contract writing none of them isn't bound."""
    metric_label: str
    """The requested metric's governed label, which a pin refusal names."""


@dataclass(frozen=True, slots=True)
class _SelectedSide:
    partitions: tuple[PartitionIdentity, ...]
    filters: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _PlanRequirement:
    plan: QueryPlan
    primary: _SelectedSide
    joined: _SelectedSide | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedSelection:
    requirements: tuple[_PlanRequirement, ...]
    partitions: tuple[PartitionIdentity, ...]


@dataclass(frozen=True, slots=True)
class _ProductFilterRequest:
    side: _FactSide
    packs: tuple[SourcePack, ...]
    populations: frozenset[str]
    supplied: str


def _days(window: DateRange) -> tuple[DateRange, ...]:
    return tuple(
        DateRange(window.start + timedelta(days=offset), window.start + timedelta(days=offset))
        for offset in range(window.days)
    )


def _compilation_dates(pack: SourcePack, window: DateRange) -> tuple[date, ...]:
    """Return business dates used to resolve identity for this pack's buckets."""
    if pack.time.grain is not TimeGrain.DAY:
        return (window.start,)
    return tuple(window.start + timedelta(days=offset) for offset in range(window.days))


def _source_options(contract: DataContract, filters: dict[str, str]) -> tuple[tuple[str, str], ...]:
    options: dict[str, str] = {}
    for field in contract.fields:
        if ".reportOptions." not in field.source_path:
            continue
        selector = field.target_column
        value = filters.get(selector, LENS_DEFAULTS.get(selector))
        if value is None:
            raise ValueError(
                f"{contract.contract_id}: serving cannot resolve source option {selector!r}."
            )
        options[field.source_path.rsplit(".", 1)[-1]] = json.dumps(value, sort_keys=True)
    return tuple(sorted(options.items()))


def _normalize(windows: tuple[DateRange, ...]) -> tuple[DateRange, ...]:
    if not windows:
        return ()
    ordered = sorted(set(windows), key=lambda item: (item.start, item.end))
    merged: list[DateRange] = [ordered[0]]
    for following in ordered[1:]:
        previous = merged[-1]
        if following.start <= previous.end + timedelta(days=1):
            merged[-1] = DateRange(previous.start, max(previous.end, following.end))
        else:
            merged.append(following)
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class PublicationScopeRequest:
    """Inputs that must stay identical wherever a publisher-owned scope is reconstructed."""

    pack: SourcePack
    contract: DataContract
    bridge: CommerceIdentityBridge
    filters: dict[str, str]
    period_grain: TimeGrain | None = None
    """A periodic pack's one grain to scope, or None for every grain it declares.

    The grain is in a periodic publication's lens, as the registration's `period_grain` option
    (`lens_source_options`), so a week and a month of one pack are two families.
    """
    variant: str | None = None
    """A declared pack variant's series to scope (plan D9), or None for the base pack's.

    The variant is in the lens too, as the registration's `pack_variant` option, so a variant's
    rows are never read beside the base pack's. When None, a filter pinning a cell value only a
    variant declares (`row_kind = exposure` on pack 5) scopes that variant
    (`ExpectedCells.variant_pinned`), so a plan reading a variant's rows binds that variant's
    publications and no other. The load's variant check names its series explicitly.
    """

    @property
    def series(self) -> str | None:
        """The variant this request scopes: the one named, else the one its filters pin."""
        cells = self.contract.expected_cells
        pinned = None if cells is None else cells.variant_pinned(self.filters)
        if self.variant is not None and pinned is not None and pinned != self.variant:
            raise ValueError(
                f"{self.contract.contract_id}: filters pin variant {pinned}, not {self.variant}."
            )
        return self.variant if self.variant is not None else pinned


def _population_for(bridge: CommerceIdentityBridge, filtered: str) -> str:
    """Resolve a canonical population or any governed source alias to one population."""
    populations = {mapping.target.value for mapping in bridge.marketplace_mappings}
    if filtered in populations:
        return filtered
    resolved = {
        mapping.target.value
        for mapping in bridge.bare_value_marketplace_mappings()
        if mapping.source.value == filtered
    }
    if len(resolved) != 1:
        qualifier = "outside" if not resolved else "ambiguous within"
        raise ValueError(f"marketplace_key {filtered!r} is {qualifier} the governed scope.")
    return resolved.pop()


def _binds(contract: DataContract, side: _FactSide) -> bool:
    """Whether a side's read needs this contract's publications.

    A contract writing none of the side's measures contributes only rows that are NULL in them,
    so it isn't bound: `amc-dsp-reach` writes `reach_users` alone, and requiring its publications
    for DSP spend would let a refused reach delivery block spend (ADR-109 point 3). Every writer
    bound before `amc-dsp-reach` existed writes one of its metric's measures, so none changes.

    A publication-gated writer (`served_where_published`, plan D6) is bound only when the side
    pins the rows it owns, which is also exactly when the planner's coverage gate checked those
    days. Unpinned and sponsored reach read what they read before pack 4.

    A contract stamping a literal on a column the side pins to another value reads none of the
    side's rows and isn't bound to it. A shopper-aggregate grain set's contract stamps its own
    `grain_set` (ADR-099 point 2), so pack 1's platform publications never gate a per-channel
    read, nor pack 2's a platform read; a cohort path's contract stamps its own `cohort_path`
    (ADR-104), so the paid ARP pack never gates a standard-path read, nor pack 14 an ARP read.
    """
    if not set(side.measures) & set(contract.target_columns):
        return False
    if _literal_conflicts(contract, side):
        return False
    gated = tuple(
        column
        for column in pack_versioned_columns((contract,))
        if column.publication_gated and column.column in side.measures
    )
    return all(column.applies_to(side.filters) for column in gated)


def _literal_conflicts(contract: DataContract, side: _FactSide) -> tuple[tuple[str, str], ...]:
    """Each `(column, literal)` the contract stamps on a column the side pins to another value."""
    return tuple(
        (mapping.target_column, str(mapping.literal_value))
        for mapping in contract.fields
        if mapping.literal_value is not None
        and (pinned := side.filters.get(mapping.target_column)) is not None
        and str(mapping.literal_value) != pinned
    )


def _pin_refusal(
    side: _FactSide, writers: tuple[DataContract, ...], taxonomy: MediaTaxonomy
) -> DomainError | None:
    """Refuse the media pins that every writer of the side's measures stamps other values for.

    `writers` is every writer of the measures whatever the window (`_measure_writers`), so a
    writer outside the window that would serve the pin is a writer without a conflict here.
    Only when each writer is unbound by literals on media taxonomy columns alone, and every
    writer on the same columns, the ones the customer pinned: Viewable impressions has one
    writer, `amc-dsp-views`, which stamps `channel_group_key = display` and `channel_key` and
    `ad_product_key = amazon_dsp`, so it is never measured for Sponsored Products, named as a
    channel, an ad product, or both. Each column then states what the writers measure, so
    "measured only for" is true of every one. Anything else (no writer at all, a gated writer, a
    planner-owned literal such as `grain_set`, writers contradicting different columns, a value
    the taxonomy cannot name) is None, so the caller's no-pack defect stays an invariant. The
    sentence is built from governed names.
    """
    conflicts = tuple(_literal_conflicts(contract, side) for contract in writers)
    if not conflicts or not all(conflicts):
        return None
    columns = {column for column, _ in conflicts[0]}
    if any({column for column, _ in found} != columns for found in conflicts):
        return None
    if not columns <= _MEDIA_PINS.keys():
        return None
    pinned = tuple(column for column in _MEDIA_PINS if column in columns)
    supported = {
        column: sorted(
            {literal for found in conflicts for name, literal in found if name == column}
        )
        for column in pinned
    }
    try:
        requested = tuple(_media_name(taxonomy, column, side.filters[column]) for column in pinned)
        offered = tuple(
            " or ".join(_media_name(taxonomy, column, value) for value in supported[column])
            for column in pinned
        )
    except ValueError:
        return None
    measured = _series(offered)
    return DomainError(
        PIN_NOT_MEASURED,
        (
            f"The {side.metric_label} metric is measured only for {measured}, "
            f"not {_series(requested)}. Ask about {measured}, or ask without "
            f"{_series(tuple(_MEDIA_PINS[column][1] for column in pinned))}."
        ),
        context={
            "fact": side.fact,
            "requested": {column: side.filters[column] for column in pinned},
            "measured_for": supported,
        },
    )


def _media_name(taxonomy: MediaTaxonomy, column: str, value: str) -> str:
    """Name a media value from the taxonomy; `ValueError` when it has no unique term."""
    key_type, _ = _MEDIA_PINS[column]
    return taxonomy.term_for(key_type(value)).name


def _series(names: tuple[str, ...]) -> str:
    """Join distinct names in order: `A`, `A and B`, `A, B and C`."""
    distinct = tuple(dict.fromkeys(names))
    if len(distinct) == 1:
        return distinct[0]
    return f"{', '.join(distinct[:-1])} and {distinct[-1]}"


def publication_scopes_for(request: PublicationScopeRequest) -> tuple[PublicationScope, ...]:
    """Derive publisher-identical declared scopes for serving and capability reads."""
    pack = request.pack
    contract = request.contract
    bridge = request.bridge
    filters = request.filters
    populations = tuple(sorted({mapping.target.value for mapping in bridge.marketplace_mappings}))
    filtered = filters.get("marketplace_key")
    if filtered is not None:
        populations = (_population_for(bridge, filtered),)
    system = pack.time.observation_basis is ObservationTimeBasis.SYSTEM_FIRST_OBSERVED
    calendar = (
        BusinessCalendarKey("technical_utc")
        if system
        else BusinessCalendarKey("iso_gregorian", week_starts_on=0)
    )
    timezone = "UTC" if system else contract.source_timezone
    options = _source_options(contract, filters)
    profile = f"source_pack.{pack.contract_id.replace('-', '_')}"
    return tuple(
        PublicationScope(
            dataset=pack.dataset,
            commerce_scope=bridge.commerce_scope,
            population_key=population,
            lens=publication_lens_for(
                profile,
                lens_source_options(contract.source_system, family),
                pack.registry_version,
            ),
            time_class=pack.time.time_class,
            business_calendar=calendar,
            source_timezone=timezone,
            observation_basis=pack.time.observation_basis,
        )
        for family in _lens_options(request, options)
        for population in populations
    )


@dataclass(frozen=True, slots=True)
class PopulationCoverage:
    """One population's CURRENT publications and exact coverage, across its pack's scopes."""

    scope: PublicationScope
    """The population's first scope, which names it as a contributor."""
    publications: tuple[SuccessfulPublication, ...]
    intervals: tuple[DateRange, ...]


def coverage_by_population(
    scopes: tuple[PublicationScope, ...],
    read: Callable[[PublicationScope], tuple[SuccessfulPublication, ...]],
) -> tuple[PopulationCoverage, ...]:
    """Read CURRENT once per scope and join each population's coverage, first scope first.

    A daily pack has one scope per population, so this is the plain read. A periodic pack has one
    per declared grain, and a population is covered wherever any of its grains is: each scope's
    exact coverage is derived on its own (`coverage_from` never mixes scopes), then joined.
    """
    joined: dict[str, tuple[PublicationScope, list[SuccessfulPublication], list[DateRange]]] = {}
    for scope in scopes:
        _first, found, windows = joined.setdefault(scope.population_key, (scope, [], []))
        publications = read(scope)
        found.extend(publications)
        if publications:
            windows.extend(publication_coverage_from(publications).intervals)
    return tuple(
        PopulationCoverage(first, tuple(found), _normalize(tuple(windows)))
        for first, found, windows in joined.values()
    )


def _lens_options(
    request: PublicationScopeRequest, options: tuple[tuple[str, str], ...]
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Rebuild the option set the publisher hashed for each grain the request scopes.

    A periodic delivery's registration names its grain (`plan_landing`), and the publisher's
    lens keeps it (`lens_source_options`), so serving adds the grain exactly as the source
    compiler encodes an option value. Any other pack has the one option set.
    """
    pack = request.pack
    if pack.time.time_class is not FactTimeClass.PERIODIC:
        if request.period_grain is not None:
            raise ValueError(f"{pack.contract_id}: only a periodic pack has a period grain.")
        return (options,)
    grains = pack.time.period_grains if request.period_grain is None else (request.period_grain,)
    if not set(grains) <= set(pack.time.period_grains):
        raise ValueError(f"{pack.contract_id}: {request.period_grain} is not a declared grain.")
    series = request.series
    variant = () if series is None else ((PACK_VARIANT_OPTION, json.dumps(series, sort_keys=True)),)
    return tuple(
        tuple(
            sorted(
                (
                    *options,
                    (PERIOD_GRAIN_OPTION, json.dumps(grain.value, sort_keys=True)),
                    *variant,
                )
            )
        )
        for grain in grains
    )


class PublicationSelector:
    """Resolve publisher-owned scopes and transactionally bind required CURRENT evidence."""

    def __init__(  # noqa: PLR0913 -- three authorities plus the packs' declared lags
        self,
        current: CurrentPublicationInspectionPort,
        packs: tuple[SourcePack, ...],
        bridge: CommerceIdentityBridge,
        *,
        cadence_lag_days: Mapping[str, int] | None = None,
        horizons: ScopeHorizonPolicy | None = None,
        taxonomy: MediaTaxonomy | None = None,
    ) -> None:
        self._current = current
        self._packs = packs
        self._bridge = bridge
        self._cadence_lag_days = dict(cadence_lag_days or {})
        self._horizons = horizons
        """A bake's declared source horizon for served periods (A5.1 I9); None reads whole."""
        self._taxonomy = taxonomy
        """Names for a customer's media pin, given where the customer chooses pins (chat).

        With it, a pin no writer of the measure serves is the classified `PIN_NOT_MEASURED`
        refusal (`_pin_refusal`). A server-built batch (pages, bakes) passes None: it chose its
        own pins, so the same conflict there is its defect and stays an invariant that alarms.
        Chat's selector also serves the spend and segment rosters, which add the contract's own
        `platform_key`; that pin reaches `_pin_refusal` only if a contract stamps `platform_key`
        as a column literal, which none does. Give the rosters their own taxonomy-less selector
        before one does.
        """

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        """Return each periodic contract's complete periods CURRENT in every selected population.

        Reads publication state only (ADR-100). A period counts for a contract when every
        population the filters select has a CURRENT publication whose bucket is exactly that
        complete period; a missing population is a missing period, never a partial one.
        """
        filters = dict(request.filters)
        served: set[tuple[str, CalendarPeriod]] = set()
        held: dict[tuple[str, CalendarPeriod], list[str | None]] = {}
        for contract in request.contracts:
            pack = next(
                (item for item in self._packs if item.contract_id == contract.contract_id), None
            )
            if pack is None or pack.time.time_class is not FactTimeClass.PERIODIC:
                raise ValueError(f"{contract.contract_id}: no periodic source pack is installed.")
            # The base pack's series, and each declared variant's under its own lens (plan D9).
            cells = contract.expected_cells
            variants = () if cells is None else tuple(name for name, _sets in cells.variants)
            for variant in (None, *variants):
                common = self._served_in(
                    PublicationScopeRequest(
                        pack=pack,
                        contract=contract,
                        bridge=self._bridge,
                        filters=filters,
                        variant=variant,
                    )
                )
                served.update((contract.contract_id, period) for period in common)
                if variants:
                    for period in common:
                        held.setdefault((contract.contract_id, period), []).append(variant)
        return ServedPeriods(
            series={key: tuple(value) for key, value in held.items()},
            publications=frozenset(served),
            cadence_lag_days={
                contract.contract_id: self._cadence_lag_days.get(
                    contract.contract_id, DEFAULT_CADENCE_LAG_DAYS
                )
                for contract in request.contracts
            },
        )

    def _served_in(self, request: PublicationScopeRequest) -> set[CalendarPeriod]:
        """Return one series' complete periods CURRENT in every population its filters select.

        One scope per population and declared grain: a population's periods are the union over
        its grains, and a period counts only where every population has it.
        """
        pack = request.pack
        horizon = (
            None
            if self._horizons is None
            else self._horizons.window_horizon(pack.time.longest_bucket_days)
        )
        per_population: dict[str, set[CalendarPeriod]] = {}
        for scope in publication_scopes_for(request):
            periods = per_population.setdefault(scope.population_key, set())
            found = (
                self._current.current_for_scope(scope)
                if horizon is None
                else self._current.current_for_scope(scope, horizon=horizon)
            )
            for publication in found:
                bucket = publication.partition.bucket
                if bucket.grain in pack.time.period_grains and is_complete_period(
                    bucket.grain, bucket.window.start, bucket.window.end
                ):
                    periods.add(
                        CalendarPeriod(bucket.window.start, bucket.window.end, bucket.grain)
                    )
        return (
            set[CalendarPeriod].intersection(*per_population.values()) if per_population else set()
        )

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        """Return each day's lowest CURRENT query pack version across the selected populations.

        Reads publication state only: the version the load recorded on each day partition's
        evidence (`pack_version_evidence`). A day with no CURRENT publication in some population,
        or whose publication recorded no version, maps to None, so the gate fails closed.
        """
        filters = dict(request.filters)
        days = sorted(
            {
                window.start + timedelta(days=offset)
                for window in request.windows
                for offset in range(window.days)
            }
        )
        found: list[PackDayVersions] = []
        for contract in request.contracts:
            pack = next(
                (item for item in self._packs if item.contract_id == contract.contract_id), None
            )
            if pack is None or pack.time.grain is not TimeGrain.DAY:
                raise ValueError(f"{contract.contract_id}: no daily source pack is installed.")
            partitions = tuple(
                (
                    day,
                    PartitionIdentity(
                        scope,
                        MeasurementBucket(
                            DateRange(day, day),
                            pack.time.bucket_grain(DateRange(day, day)),
                            scope.business_calendar,
                            scope.source_timezone,
                            pack.time.observation_basis,
                        ),
                    ),
                )
                for scope in publication_scopes_for(
                    PublicationScopeRequest(
                        pack=pack, contract=contract, bridge=self._bridge, filters=filters
                    )
                )
                for day in days
            )
            versions: dict[date, int | None] = {}
            for start in range(0, len(partitions), MAX_EXACT_CURRENT_PARTITIONS):
                chunk = partitions[start : start + MAX_EXACT_CURRENT_PARTITIONS]
                inspected = self._current.inspect_exact(tuple(item for _, item in chunk))
                for (day, _partition), publication in zip(
                    chunk, inspected.publications, strict=True
                ):
                    version = (
                        None
                        if publication is None
                        else pack_version_from_evidence(publication.evidence, contract.contract_id)
                    )
                    if day not in versions:
                        versions[day] = version
                    else:
                        previous = versions[day]
                        versions[day] = (
                            None if previous is None or version is None else min(previous, version)
                        )
            first_published = (
                self._first_published(pack, contract, filters)
                if contract.publication_gated_fields and None in versions.values()
                else None
            )
            found.append(PackDayVersions(contract.contract_id, versions, first_published))
        return tuple(found)

    def _first_published(
        self, pack: SourcePack, contract: DataContract, filters: dict[str, str]
    ) -> date | None:
        """Return the day a gated contract is measured from in every selected population.

        Made only when a requested day isn't covered, so the refusal can name the day the writer
        is measured from (plan D6). One earliest-bucket CURRENT read per population scope
        (`earliest_current_for_scope`), never the scope's whole history: a population starts on
        its earliest published day, and the selection on the latest of those starts. A later gap
        in one population is that day's own refusal, as on any covered window. A gated contract
        records its pack version on every partition, so an earliest publication without one fails
        closed: no day is named, as when the bounded read finds no servable early bucket. The
        named day is each population's start, not a day proven covered in every population; see
        `PackDayVersions.first_published` for the multi-population limitation.
        """
        starts: dict[str, date] = {}
        for scope in publication_scopes_for(
            PublicationScopeRequest(
                pack=pack, contract=contract, bridge=self._bridge, filters=filters
            )
        ):
            publication = self._current.earliest_current_for_scope(scope)
            if (
                publication is None
                or pack_version_from_evidence(publication.evidence, contract.contract_id) is None
            ):
                return None
            day = publication.partition.bucket.window.start
            starts[scope.population_key] = min(day, starts.get(scope.population_key, day))
        return max(starts.values(), default=None)

    def inspect(
        self,
        request: PresentationEvidenceInspectionRequest,
    ) -> PresentationEvidenceInspectionResult:
        """Resolve once and preserve ordered whole-plan slots for stable absence."""
        resolved = self._resolve(
            tuple(requested.plan for requested in request.plans),
            request.contracts,
        )
        by_partition = self._attest(resolved.partitions)
        return self._inspection_result(request, resolved, by_partition)

    def _attest(
        self,
        partitions: tuple[PartitionIdentity, ...],
    ) -> dict[str, SuccessfulPublication | None]:
        """Read one exact snapshot, refusing misaligned or substituted evidence."""
        inspected = self._current.inspect_exact(partitions)
        if len(inspected.publications) != len(partitions):
            raise DependencyError(
                SELECTION_FAILED,
                "Semantic V2 CURRENT inspection returned misaligned evidence.",
                context={
                    "required_partitions": len(partitions),
                    "returned_slots": len(inspected.publications),
                },
            )
        by_partition: dict[str, SuccessfulPublication | None] = {}
        for partition, publication in zip(
            partitions,
            inspected.publications,
            strict=True,
        ):
            if publication is not None and publication.partition != partition:
                raise DependencyError(
                    SELECTION_FAILED,
                    "Semantic V2 CURRENT inspection returned substituted evidence.",
                    context={"required_partitions": len(partitions)},
                )
            by_partition[partition.identity] = publication
        return by_partition

    def _inspection_result(
        self,
        request: PresentationEvidenceInspectionRequest,
        resolved: _ResolvedSelection,
        by_partition: dict[str, SuccessfulPublication | None],
    ) -> PresentationEvidenceInspectionResult:
        """Build ordered whole-plan slots from already-attested per-partition evidence."""
        slots: list[PresentationEvidenceInspectionSlot] = []
        for requested, requirement in zip(
            request.plans,
            resolved.requirements,
            strict=True,
        ):
            bound = self._bind_if_present(requirement, by_partition)
            if bound is None:
                slots.append(PresentationEvidenceInspectionSlot(requested.role, None))
                continue
            publications = bound.all_publications
            slots.append(
                PresentationEvidenceInspectionSlot(
                    role=requested.role,
                    bound=bound,
                    coverage=self._coverage(
                        PublicationSelectionRequest(
                            plans=(requested.plan,),
                            contracts=request.contracts,
                            now=request.now,
                        ),
                        publications,
                    ),
                    qualified_dq=tuple(
                        sorted(
                            {
                                warning
                                for publication in publications
                                for warning in publication.evidence.qualified_warnings
                            }
                        )
                    ),
                )
            )
        return PresentationEvidenceInspectionResult(tuple(slots))

    def select_exact(
        self, request: PublicationSelectionExactRequest
    ) -> tuple[SuccessfulPublication, ...]:
        """Resolve exactly the pinned immutable records and compare each digest (plan 4.3).

        Never reads CURRENT: a source that advanced since the pin still resolves the pinned
        bytes, and a missing or changed record is the fatal manifest-invalid refusal.
        """
        reader = cast("Any", self._current)
        resolved = cast(
            "tuple[SuccessfulPublication, ...]",
            reader.immutable_exact(
                tuple(
                    (pin.partition, pin.publication_id, pin.fencing_token) for pin in request.pins
                )
            ),
        )
        for pin, publication in zip(request.pins, resolved, strict=True):
            if publication_digest(publication) != pin.digest:
                raise InvariantError(
                    SELECTION_FAILED,
                    "A pinned publication's digest differs from its manifest.",
                    context={"publication_id": pin.publication_id},
                )
        return resolved

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        """Bind all logical plans before the first query, with no partial fallback."""
        resolved = self._resolve(request.plans, request.contracts)
        selected = self._current.current_exact(resolved.partitions)
        if selected is None:
            # A window the platform has not published is an answer, not an outage
            # (ADR-062): the refusal names what is covered so the next question can
            # succeed, and it never pages anyone.
            raise DomainError(
                SELECTION_FAILED,
                self._coverage_refusal_reason(resolved.partitions),
                context={"required_partitions": len(resolved.partitions)},
            )
        by_partition = {item.partition.identity: item for item in selected}
        bound = tuple(
            self._bind(requirement, by_partition) for requirement in resolved.requirements
        )
        publications = tuple(
            {
                item.publication_id.value: item for plan in bound for item in plan.all_publications
            }.values()
        )
        return PublicationSelection(
            plans=bound,
            coverage=self._coverage(request, publications),
            qualified_dq=tuple(
                sorted(
                    {
                        warning
                        for publication in publications
                        for warning in publication.evidence.qualified_warnings
                    }
                )
            ),
        )

    def _coverage_refusal_reason(self, partitions: tuple[PartitionIdentity, ...]) -> str:
        """Name the covered part of the requested window, so the refusal is actionable."""
        base = "No published data covers the requested window."
        guidance = (
            " Ask about a covered period, or omit the window to use the latest published coverage."
        )
        try:
            inspected = self._current.inspect_exact(partitions)
        except Exception as error:
            # The refusal must never depend on the diagnosis succeeding; the failed diagnosis
            # is reported so the generic refusal is not the only trace of it.
            report_error(
                error,
                Report(operation=_COVERAGE_DIAGNOSIS, correlation_id="", logger=_LOGGER),
            )
            return base + guidance
        present: dict[date, bool] = {}
        for partition, publication in zip(partitions, inspected.publications, strict=True):
            for offset in range(partition.bucket.window.days):
                day = partition.bucket.window.start + timedelta(days=offset)
                present[day] = present.get(day, True) and publication is not None
        covered_days = sorted(day for day, whole in present.items() if whole)
        if not covered_days:
            return base + " None of the requested days are published." + guidance
        ranges: list[DateRange] = []
        for day in covered_days:
            if ranges and day == ranges[-1].end + timedelta(days=1):
                ranges[-1] = DateRange(ranges[-1].start, day)
            else:
                ranges.append(DateRange(day, day))
        listed = ", ".join(str(item) for item in ranges)
        return f"{base} Within the requested window, published coverage is {listed}.{guidance}"

    def resolve_partitions(
        self,
        plans: tuple[QueryPlan, ...],
        contributing_contracts: tuple[DataContract, ...],
    ) -> tuple[PartitionIdentity, ...]:
        """Return the exact CURRENT partitions these plans bind, with no read (plan A5.1).

        The same resolution `select` binds, under the same exact-read bound, so a bake can name
        every partition a render will inspect before it renders.
        """
        return self._resolve(plans, contributing_contracts).partitions

    def _resolve(
        self,
        plans: tuple[QueryPlan, ...],
        contributing_contracts: tuple[DataContract, ...],
    ) -> _ResolvedSelection:
        """Resolve and enforce the verified exact-read bound before any read."""
        resolved = self._resolve_requirements(plans, contributing_contracts)
        if len(resolved.partitions) > MAX_EXACT_CURRENT_PARTITIONS:
            raise ValidationError(
                SELECTION_TOO_LARGE,
                (
                    "A measurement requires too many exact publication partitions. "
                    "Narrow the date range or population."
                ),
                context={
                    "required_partitions": len(resolved.partitions),
                    "maximum_partitions": MAX_EXACT_CURRENT_PARTITIONS,
                },
            )
        return resolved

    def _resolve_requirements(
        self,
        plans: tuple[QueryPlan, ...],
        contributing_contracts: tuple[DataContract, ...],
    ) -> _ResolvedSelection:
        contracts = {contract.contract_id: contract for contract in contributing_contracts}
        requirements: list[_PlanRequirement] = []
        all_partitions: dict[str, PartitionIdentity] = {}
        for plan in plans:
            primary = self._selected_side(
                _FactSide(
                    plan.fact,
                    plan.source_datasets,
                    dict(plan.filters),
                    plan.window,
                    plan.measures,
                    plan.metric.label,
                ),
                contracts,
            )
            joined = (
                None
                if plan.join is None
                else self._selected_side(
                    _FactSide(
                        plan.join.fact,
                        plan.join.source_datasets,
                        dict(plan.join.filters),
                        plan.window,
                        (plan.join.measure,),
                        plan.metric.label,
                    ),
                    contracts,
                )
            )
            requirement = _PlanRequirement(plan, primary, joined)
            requirements.append(requirement)
            joined_partitions = () if joined is None else joined.partitions
            for partition in (*primary.partitions, *joined_partitions):
                all_partitions[partition.identity] = partition
        product_targets: dict[str, set[str]] = {}
        for requirement in requirements:
            sides = [(dict(requirement.plan.filters), requirement.primary)]
            if requirement.plan.join is not None and requirement.joined is not None:
                sides.append((dict(requirement.plan.join.filters), requirement.joined))
            for supplied_filters, selected_side in sides:
                supplied = supplied_filters.get("amazon_asin")
                selected = dict(selected_side.filters).get("amazon_asin")
                if supplied is not None and selected is not None:
                    product_targets.setdefault(supplied, set()).add(selected)
        if any(len(targets) != 1 for targets in product_targets.values()):
            raise ValueError(
                "a source product filter resolves to different canonical products across plans."
            )
        ordered = tuple(all_partitions[key] for key in sorted(all_partitions))
        return _ResolvedSelection(tuple(requirements), ordered)

    @staticmethod
    def _bind(
        requirement: _PlanRequirement,
        by_partition: dict[str, SuccessfulPublication],
    ) -> BoundQueryPlan:
        return BoundQueryPlan(
            logical=requirement.plan,
            primary=tuple(by_partition[item.identity] for item in requirement.primary.partitions),
            primary_filters=requirement.primary.filters,
            joined=(
                ()
                if requirement.joined is None
                else tuple(by_partition[item.identity] for item in requirement.joined.partitions)
            ),
            joined_filters=(() if requirement.joined is None else requirement.joined.filters),
        )

    @classmethod
    def _bind_if_present(
        cls,
        requirement: _PlanRequirement,
        by_partition: dict[str, SuccessfulPublication | None],
    ) -> BoundQueryPlan | None:
        selected: dict[str, SuccessfulPublication] = {}
        joined_partitions = () if requirement.joined is None else requirement.joined.partitions
        for partition in (*requirement.primary.partitions, *joined_partitions):
            publication = by_partition[partition.identity]
            if publication is None:
                return None
            selected[partition.identity] = publication
        return cls._bind(requirement, selected)

    def _measure_writers(
        self, side: _FactSide, contracts: dict[str, DataContract]
    ) -> tuple[DataContract, ...] | None:
        """Every writer of the side's measures for its fact and datasets, whatever the window.

        What `_pin_refusal` states as measured is a fact about these writers, not about the packs
        serving one window, so a writer outside the window that would serve the pin leaves the
        no-pack an invariant. None when the registry and the caller's contracts disagree about
        the fact (a pack without its contract, or a writer without its pack): an unread writer's
        literals could serve the pin, so there is no scope to state.
        """
        fact_packs = tuple(pack for pack in self._packs if pack.dataset.value == side.fact)
        if any(pack.contract_id not in contracts for pack in fact_packs):
            return None
        packed = {pack.contract_id for pack in fact_packs}
        writers = tuple(
            contract
            for contract in contracts.values()
            if contract.target_table == side.fact
            and contract.source_dataset in side.source_datasets
            and set(side.measures) & set(contract.target_columns)
        )
        if any(contract.contract_id not in packed for contract in writers):
            return None
        return writers

    def _selected_side(
        self,
        side: _FactSide,
        contracts: dict[str, DataContract],
    ) -> _SelectedSide:
        candidates = tuple(
            pack
            for pack in self._packs
            if pack.dataset.value == side.fact
            and contracts.get(pack.contract_id) is not None
            and contracts[pack.contract_id].source_dataset in side.source_datasets
            # A periodic pack has a bucket only over one complete declared period (ADR-100), so
            # any other window matches no pack rather than reaching `bucket_grain`'s refusal.
            and pack.time.serves_window(side.window)
        )
        packs = tuple(pack for pack in candidates if _binds(contracts[pack.contract_id], side))
        if not packs:
            taxonomy = self._taxonomy
            writers = None if taxonomy is None else self._measure_writers(side, contracts)
            refusal = (
                None
                if taxonomy is None or writers is None
                else _pin_refusal(side, writers, taxonomy)
            )
            if refusal is not None:
                raise refusal
            raise ValueError(f"{side.fact}: no source pack matches {side.source_datasets}.")
        filters = dict(side.filters)
        if "marketplace_key" in filters:
            filters["marketplace_key"] = self._marketplace_filter(
                side,
                packs,
                filters["marketplace_key"],
            )
        partitions: list[PartitionIdentity] = []
        for pack in packs:
            contract = contracts[pack.contract_id]
            windows = _days(side.window) if pack.time.grain is TimeGrain.DAY else (side.window,)
            periodic = pack.time.time_class is FactTimeClass.PERIODIC
            for selected_scope in publication_scopes_for(
                PublicationScopeRequest(
                    pack=pack,
                    contract=contract,
                    bridge=self._bridge,
                    filters=filters,
                    period_grain=pack.time.bucket_grain(side.window) if periodic else None,
                )
            ):
                partitions.extend(
                    PartitionIdentity(
                        selected_scope,
                        MeasurementBucket(
                            window,
                            pack.time.bucket_grain(window),
                            selected_scope.business_calendar,
                            selected_scope.source_timezone,
                            pack.time.observation_basis,
                        ),
                    )
                    for window in windows
                )
        populations = {partition.scope.population_key for partition in partitions}
        if "marketplace_key" in filters:
            if len(populations) != 1:
                raise ValueError("a filtered query must select one canonical marketplace.")
            filters["marketplace_key"] = next(iter(populations))
        if "amazon_asin" in filters:
            filters["amazon_asin"] = self._product_filter(
                _ProductFilterRequest(
                    side=side,
                    packs=packs,
                    populations=frozenset(populations),
                    supplied=filters["amazon_asin"],
                )
            )
        return _SelectedSide(tuple(partitions), tuple(sorted(filters.items())))

    def _marketplace_filter(
        self,
        side: _FactSide,
        packs: tuple[SourcePack, ...],
        supplied: str,
    ) -> str:
        targets = {mapping.target.value for mapping in self._bridge.marketplace_mappings}
        if supplied in targets:
            target = supplied
        else:
            resolved: set[str] = set()
            dates = {
                business_date
                for pack in packs
                for business_date in _compilation_dates(pack, side.window)
            }
            for business_date in dates:
                active = {
                    mapping.target.value
                    for mapping in self._bridge.bare_value_marketplace_mappings()
                    if mapping.source.value == supplied
                    and mapping.applies(mapping.source, on=business_date)
                }
                if len(active) != 1:
                    raise ValueError(
                        "a source marketplace filter must resolve throughout the request window."
                    )
                resolved.update(active)
            if len(resolved) != 1:
                raise ValueError(
                    "a source marketplace filter resolves to different canonical marketplaces."
                )
            target = resolved.pop()
        for pack in packs:
            for business_date in _compilation_dates(pack, side.window):
                active = tuple(
                    mapping
                    for mapping in self._bridge.marketplace_mappings
                    if mapping.source.namespace == pack.marketplace.namespace
                    and mapping.target.value == target
                    and mapping.applies(mapping.source, on=business_date)
                )
                if len(active) != 1:
                    raise ValueError(
                        f"{pack.contract_id}: serving cannot resolve one marketplace source alias."
                    )
        return target

    def _product_filter(self, request: _ProductFilterRequest) -> str:
        if len(request.populations) != 1:
            raise ValueError("a product filter requires one canonical marketplace.")
        marketplace_target = next(iter(request.populations))
        canonical_namespaces = {rule.canonical_namespace for rule in self._bridge.product_rules} | {
            mapping.target.value.partition(":")[0] for mapping in self._bridge.product_mappings
        }
        namespace, separator, _identifier = request.supplied.partition(":")
        if separator and namespace in canonical_namespaces:
            return ProductKey(
                request.supplied,
                commerce_scope=self._bridge.commerce_scope,
                marketplace=MarketplaceKey(marketplace_target),
            ).value

        products: set[ProductKey] = set()
        for pack in request.packs:
            if pack.product is None:
                continue
            for business_date in _compilation_dates(pack, request.side.window):
                marketplace = tuple(
                    mapping
                    for mapping in self._bridge.marketplace_mappings
                    if mapping.source.namespace == pack.marketplace.namespace
                    and mapping.target.value == marketplace_target
                    and mapping.applies(mapping.source, on=business_date)
                )
                if len(marketplace) != 1:
                    raise ValueError(
                        f"{pack.contract_id}: serving cannot resolve one marketplace source alias."
                    )
                resolved = resolve_commerce_identity(
                    self._bridge,
                    ResolveCommerceIdentityRequest(
                        marketplace=marketplace[0].source,
                        product=SourceIdentifier(pack.product.namespace, request.supplied),
                        business_date=business_date,
                    ),
                )
                if resolved.product is None:
                    raise ValueError(
                        f"{pack.contract_id}: serving cannot resolve the product source alias."
                    )
                products.add(resolved.product.target)
        if len(products) != 1:
            raise ValueError("a product filter must resolve to one canonical product.")
        return next(iter(products)).value

    @staticmethod
    def _coverage(
        request: PublicationSelectionRequest,
        publications: tuple[SuccessfulPublication, ...],
    ) -> Coverage:
        intervals = _normalize(tuple(item.partition.bucket.window for item in publications))
        by_scope: dict[PublicationScope, list[SuccessfulPublication]] = {}
        for publication in publications:
            by_scope.setdefault(publication.partition.scope, []).append(publication)
        latest_by_scope = tuple(
            max(items, key=lambda item: item.published_at).published_at
            for items in by_scope.values()
        )
        loaded_at = min(latest_by_scope)
        selected_datasets = {
            dataset
            for plan in request.plans
            for dataset in (
                *plan.source_datasets,
                *((plan.join.source_datasets) if plan.join is not None else ()),
            )
        }
        thresholds = tuple(
            contract.staleness_threshold_hours
            for contract in request.contracts
            if contract.source_dataset in selected_datasets
        )
        threshold = min(thresholds)
        freshness = (
            FreshnessStatus.STALE
            if request.now - loaded_at > timedelta(hours=threshold)
            else FreshnessStatus.FRESH
        )
        return Coverage(
            fact_name=" + ".join(
                sorted({fact for plan in request.plans for fact in plan.facts_read})
            ),
            window=intervals[0] if len(intervals) == 1 else None,
            freshness=freshness,
            measurement_basis=request.plans[0].measurement_basis[0],
            valid_grains=request.plans[0].metric.valid_grains,
            loaded_at=loaded_at,
            intervals=intervals,
        )


class PerPackPresentationEvidenceInspection(PublicationSelector):
    """Bake-scoped inspection that snapshots each source pack in its own verified exact read.

    ADR-070 decision point 5: at bake time, a batch's required partitions are grouped by
    source-pack scope identity and each pack's partitions — both comparison windows
    together — are attested in one exact read, so a restatement can never fabricate a
    delta within a pack. Cross-pack instant consistency is deliberately not claimed:
    independent sources deliver and restate on independent cadences, so a cross-pack
    single instant would assert nothing semantically real. The whole batch may exceed
    the exact-read bound, but no single pack may: an over-bound pack snapshot refuses before
    its first read, and a pack is never split across independent reads. Request serving
    keeps the selection-wide ``PublicationSelector``.
    """

    def inspect(
        self,
        request: PresentationEvidenceInspectionRequest,
    ) -> PresentationEvidenceInspectionResult:
        """Resolve once, attest one exact read per source pack, and merge ordered slots."""
        resolved = self._resolve_requirements(
            tuple(requested.plan for requested in request.plans),
            request.contracts,
        )
        groups: dict[str, list[PartitionIdentity]] = {}
        for partition in resolved.partitions:
            groups.setdefault(partition.scope.identity, []).append(partition)
        for scope_identity in sorted(groups):
            members = groups[scope_identity]
            if len(members) > MAX_PRESET_SNAPSHOT_PARTITIONS:
                # Refuse before the first read: a partially attested bake snapshot is
                # exactly the inconsistency the per-pack snapshot exists to prevent.
                raise ValidationError(
                    PACK_SNAPSHOT_TOO_LARGE,
                    (
                        "A source pack's bake snapshot requires more exact partitions "
                        "than one verified exact read attests. Shrink the declared preset "
                        "window; a pack is never split across independent reads."
                    ),
                    context={
                        "scope": scope_identity,
                        "required_partitions": len(members),
                        "maximum_partitions": MAX_PRESET_SNAPSHOT_PARTITIONS,
                    },
                )
        by_partition: dict[str, SuccessfulPublication | None] = {}
        for scope_identity in sorted(groups):
            by_partition.update(self._attest(tuple(groups[scope_identity])))
        return self._inspection_result(request, resolved, by_partition)


def publication_digest(publication: SuccessfulPublication) -> str:
    """Return the versioned canonical digest of every attested publication field (plan 4.3)."""
    return canonical_model_digest("successful-publication/v1", publication)
