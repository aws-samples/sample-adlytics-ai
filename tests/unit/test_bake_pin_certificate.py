# SPDX-License-Identifier: MIT-0
"""Spike S0 (plan A5.3 (b), O3; A5.1 I9): the horizon-based D2 pin-size certificate.

For every D2 roster row this builds the row's maximum-cardinality read list from the A5.1
prefetch families, each whole-scope read at the row's declared horizon:

- one `inspect_exact` per scope over every partition `PublicationSelector.resolve_partitions`
  yields for the row's certified plans and for the spend plan of every roster read it can issue,
  every slot present (window-bounded by construction);
- **coverage** (`PageWindowCatalog.source_coverage`, campaigns, retail-health, scorecard and
  new-customers): the scopes the real catalog reads for the page's own coverage requirements,
  each from `anchor - source_horizon_days - longest bucket`;
- **served periods** (`PublicationSelector.served_periods`, reach, retention, audiences and
  new-customers): the scopes the real selector reads for the page's periodic contracts and
  variants, at the same windowed horizon;
- **latest only** (`newest=1`): the entity-catalog `dim_campaign` and `dim_product` scopes for
  every row and the segment-label scopes for Audiences;
- **reported totals** (retail-health): from one bucket before the row's window;
- **earliest** (bounded, one pin): one per coverage scope and one per marketplace for
  Retention's quarterly vintage.

It encodes each list with the real `encode_reads`, builds the exact pin transaction with the real
store's `_bake_pin_members` over all 20 blobs, and measures every item and the aggregate with
`Stage1DynamoByteOracle.validate_envelope` and `graph_transaction_bytes`. A scope's history is
grown from a governed fixture shape (one CURRENT bucket per period), with pins that carry the
stored shape and entropy. The certified figures are taken at saturation: once history is longer
than every row's horizon footprint, more history leaves the pin byte-identical (invariance). The
unbounded read is the paired negative control. Every identity is fictional except the governed
scope identities the contracts derive.
"""

from __future__ import annotations

# The pin builder is the store's private transaction seam: the certificate measures it exactly.
# pyright: reportPrivateUsage=false
import contextlib
import hashlib
import io
import json
import pathlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from boto3.dynamodb.types import TypeDeserializer
from execution_graph_release_support import TABLE, release_store
from execution_graph_support import FakeDynamo

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.insight_precompute.planning import campaign_coverage_requirements
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    DELIVERY_CONTRACT,
    METADATA_CONTRACT,
    PROFILE_CONTRACTS,
)
from adapters.jobs.preset_artifact_publisher.bakeability import (
    BAKEABILITY_REFERENCE_INSTANT,
    D2RowFigure,
    HorizonDerivation,
    certify_d2_roster,
    d2_main,
    page_source_horizons,
)
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    _BUYERS_CONTRACT,
    _CHANNEL_CONTRACT,
    new_customers_coverage_requirements,
)
from adapters.jobs.preset_artifact_publisher.reach_source import (
    EXPOSURE_CONTRACT,
    FREQUENCY_CHANNEL,
    FREQUENCY_PLATFORM,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    retail_health_coverage_requirements,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    ARP_CONTRACT,
    STANDARD_CONTRACT,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    scorecard_coverage_requirements,
)
from adapters.outbound.bake_sources import BakeRowHorizon, PinnedRead, encode_reads, reads_digest
from adapters.outbound.catalog import PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.byte_oracle import (
    MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES,
    MAX_CONSERVATIVE_SERVICE_ITEM_BYTES,
    ConditionCheckAction,
    ExactPreState,
    PutAction,
    Stage1DynamoByteOracle,
    TransactionAction,
    TransactionEnvelopeRequest,
)
from adapters.outbound.dynamodb.graph_transaction_bytes import item_bytes, transaction_bytes
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.segment_labels import NAMESPACE_WRITERS
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SPEND_FACT, SPEND_METRIC
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.operations.execution_graph_release import BakeTaskSource, DurableBakeSourceManifest
from domain.semantics.time import FactTimeClass, TimeGrain
from domain.shared_kernel.errors import ValidationError
from ports.outbound.publication_selection import PinnedPublication
from ports.outbound.semantic_state import MAX_EXACT_CURRENT_PARTITIONS, ScopeHorizon
from ports.outbound.served_periods import ServedPeriodsRequest
from ports.outbound.source_coverage import SourceCoverageRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from adapters.outbound.dynamodb.graph_transactions import Member
    from domain.measurement.planning import QueryPlan
    from domain.semantics.publication import PartitionIdentity, PublicationScope
    from domain.semantics.source_pack import SourcePack
    from ports.outbound.execution_graph_release import BakeSourcePinRequest

pytestmark = [pytest.mark.unit]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CERTIFICATE = ROOT / "tests" / "fixtures" / "bake-certification" / "d2-certificate.json"
ANCHOR = date(2026, 10, 14)
"""The fictional run's `compute_started_at` date; the newest daily bucket ends the day before."""
FIRST_PUBLISHED = date(2025, 7, 1)
"""Every scope's first bucket start (the governed fixture span's first day)."""
MEASURED_HISTORY_DAYS = 85
"""[measured] run 69e71e5d (2026-10-14): each daily coverage scope the stage-1 pin read held 85
CURRENT publications (`BAKE_SOURCE#campaigns#30d`)."""
PHASE_B_SPAN_DAYS = 463
"""The approved full-history load (`synthetic-entity-catalog.yaml` `fixture_span`)."""
INVARIANCE_DAYS = (2_000, 3_650, 7_300)
"""History depths past every row's horizon footprint: the pin must be byte-identical at each."""
PREVIOUS_CERTIFIED_AGGREGATE = 1_214_959
PREVIOUS_CERTIFIED_LARGEST_ITEM = 93_533
"""The first S0 run's unbounded pin at the measured 85 days (the figure this remedy answers)."""
_COVERAGE_PAGES = frozenset({"campaigns", "retail-health", "scorecard", "new-customers"})
_SERVED_CONTRACTS = {
    "reach": (EXPOSURE_CONTRACT, FREQUENCY_PLATFORM, FREQUENCY_CHANNEL),
    "retention": (STANDARD_CONTRACT, ARP_CONTRACT),
    "audiences": (*PROFILE_CONTRACTS.values(), METADATA_CONTRACT, DELIVERY_CONTRACT),
    "new-customers": (_BUYERS_CONTRACT, _CHANNEL_CONTRACT),
}
"""Each page's served-period contracts, as its renderer and roster readers ask for them."""
_CATALOG_DATASETS = frozenset({"dim_campaign", "dim_product"})
_SHAPES = {
    TimeGrain.DAY: (1, 1),
    TimeGrain.WEEK: (7, 7),
    TimeGrain.MONTH: (28, 28),
    TimeGrain.QUARTER: (89, 89),
    TimeGrain.CUSTOM: (28, 397),
}
"""(step, length) days of one scope's CURRENT buckets: periodic steps are each grain's shortest
instance, so a scope holds at least as many buckets as any real calendar; a cohort vintage is
published monthly and spans 13 months."""
_RUN_ID = hashlib.sha256(b"fictional-d2-certificate-run").hexdigest()
_DESERIALIZER = TypeDeserializer()


@dataclass(frozen=True, slots=True)
class ScopeRead:
    """One whole-scope read a row pins: its scope, grain, family and declared horizon."""

    scope: PublicationScope
    grain: TimeGrain
    longest_bucket_days: int
    family: str
    horizon: ScopeHorizon | None

    @property
    def key(self) -> tuple[str, ...]:
        """The pinned key: the scope with its horizon token (A5.1 I9)."""
        if self.horizon is None:
            return (self.scope.identity,)
        return (self.scope.identity, self.horizon.token)


@cache
def _world() -> tuple[Any, ...]:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    packs = load_source_packs(ROOT)
    bridge = load_commerce_identity_bridge(ROOT)
    return contracts, packs, bridge, load_catalog(ROOT), PublicationSelector(None, packs, bridge)  # type: ignore[arg-type]


@cache
def figures() -> tuple[D2RowFigure, ...]:
    """Return the 20 certified rows (the counters' own maximum-cardinality batches)."""
    return certify_d2_roster(ROOT)


@cache
def horizons() -> dict[str, HorizonDerivation]:
    """Each page's derived `source_horizon_days` (A5.1 *The declared horizon*)."""
    return page_source_horizons(ROOT, figures())


@cache
def _scope_index() -> dict[str, tuple[SourcePack, TimeGrain]]:
    """Every governed scope identity, with its pack and its one bucket grain."""
    contracts, packs, bridge, _catalog, _selector = _world()
    by_id = {contract.contract_id: contract for contract in contracts}
    index: dict[str, tuple[SourcePack, TimeGrain]] = {}
    for pack in packs:
        contract = by_id.get(pack.contract_id)
        if contract is None:
            continue
        cells = contract.expected_cells
        variants = (None, *(() if cells is None else (name for name, _ in cells.variants)))
        periodic = pack.time.time_class is FactTimeClass.PERIODIC
        for grain in pack.time.period_grains if periodic else (None,):
            for variant in variants if periodic else (None,):
                try:
                    scopes = publication_scopes_for(
                        PublicationScopeRequest(
                            pack, contract, bridge, {}, period_grain=grain, variant=variant
                        )
                    )
                except ValueError:
                    continue
                for scope in scopes:
                    index[scope.identity] = (pack, grain or pack.time.grain)
    return index


@dataclass
class _Recorder:
    """A CURRENT that records each whole-scope read the real reader makes, and answers none."""

    scopes: list[PublicationScope]

    def current_for_scope(self, scope: PublicationScope, horizon: object = None) -> tuple[()]:
        del horizon
        self.scopes.append(scope)
        return ()


@cache
def _coverage_scopes(page: str) -> tuple[PublicationScope, ...]:
    """Return the scopes the real `PageWindowCatalog` reads for the page's coverage needs."""
    contracts, packs, bridge, catalog, _selector = _world()
    requirements = {
        "campaigns": lambda: campaign_coverage_requirements(load_page_schemas(ROOT), catalog),
        "retail-health": lambda: retail_health_coverage_requirements(
            load_v2_page_schemas(ROOT), catalog
        ),
        "scorecard": lambda: scorecard_coverage_requirements(catalog),
        "new-customers": lambda: new_customers_coverage_requirements(catalog),
    }[page]()
    recorder = _Recorder([])
    PageWindowCatalog(cast("Any", recorder), packs, contracts, bridge).source_coverage(
        SourceCoverageRequest(requirements)
    )
    return tuple(dict.fromkeys(recorder.scopes))


@cache
def _served_scopes(page: str) -> tuple[PublicationScope, ...]:
    """Return the scopes the real selector's `served_periods` reads, per governed marketplace."""
    contracts, packs, bridge, _catalog, _selector = _world()
    by_id = {contract.contract_id: contract for contract in contracts}
    recorder = _Recorder([])
    selector = PublicationSelector(cast("Any", recorder), packs, bridge)
    markets = sorted({mapping.target.value for mapping in bridge.marketplace_mappings})
    for market in markets:
        for contract_id in _SERVED_CONTRACTS[page]:
            selector.served_periods(
                ServedPeriodsRequest(
                    (by_id[contract_id],),
                    {"marketplace_key": market},
                    datetime.combine(ANCHOR, datetime.min.time(), UTC),
                )
            )
    return tuple(dict.fromkeys(recorder.scopes))


def _dataset_scopes(datasets: frozenset[str] | None, contract_ids: frozenset[str]) -> list[Any]:
    contracts, packs, bridge, _catalog, _selector = _world()
    by_id = {contract.contract_id: contract for contract in contracts}
    found: list[PublicationScope] = []
    for pack in packs:
        if (datasets is not None and pack.dataset.value in datasets) or (
            pack.contract_id in contract_ids
        ):
            found.extend(
                publication_scopes_for(
                    PublicationScopeRequest(pack, by_id[pack.contract_id], bridge, {})
                )
            )
    return found


def _roster_plans(figure: D2RowFigure) -> tuple[QueryPlan, ...]:
    """Return the spend plan each roster request plans, as `SpendRosterReader.roster` does."""
    contracts, _packs, _bridge, catalog, _selector = _world()
    return tuple(
        plan(
            catalog,
            PlanRequest(
                SPEND_METRIC,
                (request.kind.value,),
                request.period,
                {
                    "marketplace_key": request.marketplace_key,
                    "platform_key": request.platform_key,
                    **dict(request.filters),
                },
            ),
            FactShape.for_fact(SPEND_FACT, contracts),
        )
        for request in figure.certificate.rosters
    )


def over_cap_plans() -> tuple[tuple[str, str, int], ...]:
    """Each (row, metric, partitions) whose one plan exceeds the strict selector's exact bound."""
    contracts, _packs, _bridge, _catalog, selector = _world()
    return tuple(
        (figure.key, item.metric.name, len(resolved))
        for figure in figures()
        for item in (*figure.certificate.plans, *_roster_plans(figure))
        if len(resolved := selector._resolve_requirements((item,), contracts).partitions)
        > MAX_EXACT_CURRENT_PARTITIONS
    )


def _partitions(
    selector: PublicationSelector, item: QueryPlan, contracts: tuple[Any, ...]
) -> tuple[PartitionIdentity, ...]:
    """`resolve_partitions`, or for a plan over the exact bound its whole requirement set.

    The strict selector refuses such a plan before any read (`SELECTION_TOO_LARGE`), so pinning
    its partitions anyway keeps the certificate an upper bound.
    """
    try:
        return selector.resolve_partitions((item,), contracts)
    except ValidationError:
        return selector._resolve_requirements((item,), contracts).partitions


@cache
def row_partitions(key: str) -> dict[str, PartitionIdentity]:
    """Every partition the row's plans and roster plans bind, resolved one plan at a time."""
    contracts, _packs, _bridge, _catalog, selector = _world()
    figure = next(item for item in figures() if item.key == key)
    found: dict[str, PartitionIdentity] = {}
    for item in (*figure.certificate.plans, *_roster_plans(figure)):
        for partition in _partitions(selector, item, contracts):
            found[partition.identity] = partition
    return found


def _row_window_start(figure: D2RowFigure) -> date:
    """Return the row's longest window start, ending the day before the anchor (reported totals)."""
    longest = max(plan.window.days for plan in figure.certificate.plans)
    return ANCHOR - timedelta(days=longest)


def row_scope_reads(
    figure: D2RowFigure, *, bounded: bool = True, horizon_days: int | None = None
) -> tuple[ScopeRead, ...]:
    """Every whole-scope read the row pins, each with its family's horizon (None: unbounded)."""
    page = figure.key.split("#", 1)[0]
    index = _scope_index()
    row = BakeRowHorizon(ANCHOR, horizon_days or horizons()[page].days)
    reads: dict[tuple[str, str], ScopeRead] = {}

    def add(scope: PublicationScope, family: str, horizon: ScopeHorizon | None) -> None:
        pack, grain = index[scope.identity]
        reads.setdefault(
            (scope.identity, family),
            ScopeRead(
                scope,
                grain,
                pack.time.longest_bucket_days,
                family,
                horizon if bounded else None,
            ),
        )

    windowed: list[tuple[PublicationScope, str]] = []
    if page in _COVERAGE_PAGES:
        windowed.extend((scope, "coverage") for scope in _coverage_scopes(page))
    if page in _SERVED_CONTRACTS:
        windowed.extend((scope, "served_periods") for scope in _served_scopes(page))
    for scope, family in windowed:
        add(scope, family, row.window(index[scope.identity][0].time.longest_bucket_days))
    for scope in _dataset_scopes(_CATALOG_DATASETS, frozenset()):
        add(scope, "latest_only", ScopeHorizon(newest=1))
    if page == "audiences":
        for scope in _dataset_scopes(None, frozenset(NAMESPACE_WRITERS.values())):
            add(scope, "latest_only", ScopeHorizon(newest=1))
    if page == "retail-health":
        for scope in _dataset_scopes(frozenset({"fct_retail_sales_reported_total"}), frozenset()):
            pack = index[scope.identity][0]
            start = _row_window_start(figure) - timedelta(days=pack.time.longest_bucket_days)
            add(scope, "reported_totals", ScopeHorizon(from_bucket_start=start))
    return tuple(reads[key] for key in sorted(reads))


def _earliest_scopes(figure: D2RowFigure) -> tuple[PublicationScope, ...]:
    page = figure.key.split("#", 1)[0]
    scopes = list(_coverage_scopes(page)) if page in _COVERAGE_PAGES else []
    if page == "retention":
        contracts, packs, bridge, _catalog, _selector = _world()
        contract = next(item for item in contracts if item.contract_id == STANDARD_CONTRACT)
        pack = next(item for item in packs if item.contract_id == STANDARD_CONTRACT)
        for market in sorted({mapping.target.value for mapping in bridge.marketplace_mappings}):
            scopes.extend(
                publication_scopes_for(
                    PublicationScopeRequest(
                        pack,
                        contract,
                        bridge,
                        {"marketplace_key": market},
                        period_grain=TimeGrain.CUSTOM,
                        variant="quarterly",
                    )
                )
            )
    return tuple(dict.fromkeys(scopes))


@cache
def _pin(partition: str) -> PinnedPublication:
    seed = hashlib.sha256(partition.encode()).hexdigest()
    return PinnedPublication(
        partition,
        f"publication-{seed[:32]}",
        1 + int(seed[32], 16) % 9,
        hashlib.sha256(seed.encode()).hexdigest(),
    )


def _bucket(scope: str, grain: TimeGrain, number: int) -> PinnedPublication:
    """Return the scope's `number`-th newest bucket (0 ends the day before ANCHOR)."""
    step, length = _SHAPES[grain]
    end = ANCHOR - timedelta(days=1 + number * step)
    start = end - timedelta(days=length - 1)
    return _pin(f"{scope}|{grain.value}|{start.isoformat()}|{end.isoformat()}")


def pinned_width(read: ScopeRead, history_days: int) -> int:
    """How many CURRENT buckets the read pins after `history_days` of history."""
    step, length = _SHAPES[read.grain]
    held = -(-history_days // step)
    horizon = read.horizon
    if horizon is None:
        return held
    if horizon.newest is not None:
        return min(held, horizon.newest)
    lower = cast("date", horizon.from_bucket_start)
    # A bucket is read when it starts on or after the bound (the index key condition).
    newest_start = ANCHOR - timedelta(days=length)
    reachable = (newest_start - lower).days // step + 1
    return max(0, min(held, reachable))


def _scope_pins(read: ScopeRead, history_days: int) -> tuple[PinnedPublication, ...]:
    width = pinned_width(read, history_days)
    return tuple(_bucket(read.scope.identity, read.grain, number) for number in range(width))[::-1]


def row_reads(  # noqa: PLR0913 -- one row, its history depth and its horizon controls
    figure: D2RowFigure, history_days: int, *, bounded: bool = True, horizon_days: int | None = None
) -> tuple[PinnedRead, ...]:
    """Return the row's canonical read list (A5.1 `recorded()`: deduplicated and sorted)."""
    by_scope: dict[str, list[str]] = {}
    for identity, partition in row_partitions(figure.key).items():
        by_scope.setdefault(partition.scope.identity, []).append(identity)
    reads: dict[tuple[str, tuple[str, ...]], PinnedRead] = {}
    for ids in by_scope.values():
        key = tuple(sorted(ids))
        reads[("inspect_exact", key)] = PinnedRead(
            "inspect_exact", key, tuple(_pin(item) for item in key)
        )
    for read in row_scope_reads(figure, bounded=bounded, horizon_days=horizon_days):
        reads[("current_for_scope", read.key)] = PinnedRead(
            "current_for_scope", read.key, _scope_pins(read, history_days)
        )
    index = _scope_index()
    for scope in _earliest_scopes(figure):
        # The earliest publication is fixed once the scope first publishes, so history that
        # grows at the newest end never changes it: one pin, always the same bucket.
        grain = index[scope.identity][1]
        first = FIRST_PUBLISHED + timedelta(days=_SHAPES[grain][1] - 1)
        reads[("earliest_current_for_scope", (scope.identity,))] = PinnedRead(
            "earliest_current_for_scope",
            (scope.identity,),
            (
                _pin(
                    f"{scope.identity}|{grain.value}|{FIRST_PUBLISHED.isoformat()}|"
                    f"{first.isoformat()}"
                ),
            ),
        )
    return tuple(reads[key] for key in sorted(reads))


@dataclass(frozen=True, slots=True)
class PinMeasure:
    """One pin transaction's measured sizes."""

    reads: tuple[int, ...]
    blobs: tuple[int, ...]
    items: tuple[tuple[str, int], ...]
    oracle_aggregate: int
    aggregate: int

    @property
    def largest(self) -> tuple[str, int]:
        """Return the largest resulting item's `sk` and conservative bytes."""
        return max(self.items, key=lambda item: item[1])


def _members(
    history_days: int, *, bounded: bool, horizon_days: int | None
) -> tuple[list[Member], list[bytes], list[int]]:
    tasks: list[BakeTaskSource] = []
    blobs: list[bytes] = []
    counts: list[int] = []
    for figure in figures():
        reads = row_reads(figure, history_days, bounded=bounded, horizon_days=horizon_days)
        blobs.append(encode_reads(reads))
        counts.append(len(reads))
        page, variant = figure.key.split("#", 1)
        digests = tuple(
            sorted(
                hashlib.sha256(f"{figure.key}:{index}".encode()).hexdigest()
                for index in range(figure.certificate.count)
            )
        )
        tasks.append(BakeTaskSource(page, variant, reads_digest(reads), digests, len(digests)))
    manifest = DurableBakeSourceManifest(
        _RUN_ID,
        "grant-fictional-d2-certificate",
        f"sha256:{hashlib.sha256(b'fictional-roster').hexdigest()}",
        tuple(tasks),
    )
    request = SimpleNamespace(
        run=SimpleNamespace(run_id=_RUN_ID),
        claim=SimpleNamespace(node_id=f"{_RUN_ID}:bake", lease_version=1),
        head=SimpleNamespace(
            domain="presentation-serving", version=1, serving_input_revision="x" * 64
        ),
        manifest=manifest,
        reads=tuple(blobs),
        recorded_at="2026-10-15T00:00:00+00:00",
    )
    store = release_store(FakeDynamo())
    members = store._bake_pin_members(cast("BakeSourcePinRequest", request))
    return members, blobs, counts


def _plain(item: dict[str, Any]) -> dict[str, object]:
    return {name: _DESERIALIZER.deserialize(value) for name, value in item.items()}


def _actions(members: list[Member]) -> tuple[TransactionAction, ...]:
    actions: list[TransactionAction] = []
    for member in members:
        ((kind, body),) = cast("dict[str, dict[str, Any]]", member.item).items()
        source = body.get("Item") or body["Key"]
        key = {name: _DESERIALIZER.deserialize(source[name]) for name in ("pk", "sk")}
        if kind == "Put":
            actions.append(PutAction(TABLE, key, _plain(body["Item"]), ExactPreState(None)))
        else:
            actions.append(ConditionCheckAction(TABLE, key, ExactPreState(None)))
    return tuple(actions)


@cache
def measure_pin(
    history_days: int, *, bounded: bool = True, horizon_days: int | None = None
) -> PinMeasure:
    """Build the exact 20-row pin at `history_days` of history and measure it."""
    members, blobs, counts = _members(history_days, bounded=bounded, horizon_days=horizon_days)
    items = tuple(
        (str(_DESERIALIZER.deserialize(body["Item"]["sk"])), item_bytes(body["Item"]))
        for member in members
        for kind, body in cast("dict[str, dict[str, Any]]", member.item).items()
        if kind == "Put"
    )
    oracle = sum(
        max(target.pre_state_bytes, target.result_bytes)
        for target in Stage1DynamoByteOracle()._measured_targets(
            TransactionEnvelopeRequest(_actions(members))
        )
    )
    return PinMeasure(
        tuple(counts),
        tuple(len(blob) for blob in blobs),
        items,
        oracle,
        transaction_bytes(members, {}),
    )


def validate_pin(history_days: int) -> None:
    """Pass the exact pin through the shared oracle's envelope validator (refuses typed)."""
    members, _blobs, _counts = _members(history_days, bounded=True, horizon_days=None)
    Stage1DynamoByteOracle().validate_envelope(TransactionEnvelopeRequest(_actions(members)))


def saturation_days() -> int:
    """Return the least history past which every bounded read is full (the longest footprint)."""
    footprint = 1
    for figure in figures():
        for read in row_scope_reads(figure):
            if read.horizon is not None and read.horizon.from_bucket_start is not None:
                step, _length = _SHAPES[read.grain]
                reach = (ANCHOR - read.horizon.from_bucket_start).days
                footprint = max(footprint, reach + step)
    return footprint


def unbounded_crossings() -> tuple[int, int]:
    """Negative control: the unbounded pin's first history depths over 400,000 / 4,000,000."""

    def first(predicate: Callable[[PinMeasure], bool]) -> int:
        low, high = MEASURED_HISTORY_DAYS, MEASURED_HISTORY_DAYS
        while not predicate(measure_pin(high, bounded=False)):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if predicate(measure_pin(middle, bounded=False)):
                high = middle
            else:
                low = middle
        return high

    return (
        first(lambda found: found.largest[1] > MAX_CONSERVATIVE_SERVICE_ITEM_BYTES),
        first(lambda found: found.aggregate > MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES),
    )


def certificate_pin_section() -> dict[str, Any]:
    """Return the pin-size section `d2-certificate.json` commits, measured at saturation."""
    saturated = saturation_days()
    measured = measure_pin(saturated)
    item_over, aggregate_over = unbounded_crossings()
    rows: list[dict[str, Any]] = []
    for figure, reads, blob, (sk, size) in zip(
        figures(), measured.reads, measured.blobs, measured.items, strict=False
    ):
        page = figure.key.split("#", 1)[0]
        rows.append(
            {
                "key": figure.key,
                "source_horizon_days": horizons()[page].days,
                "reads": reads,
                "blob_bytes": blob,
                "item_sk": sk,
                "item_bytes": size,
                "scope_reads": [
                    {
                        "scope": read.scope.identity,
                        "family": read.family,
                        "grain": read.grain.value,
                        "horizon": None if read.horizon is None else read.horizon.token,
                        "pinned_width": pinned_width(read, saturated),
                    }
                    for read in row_scope_reads(figure)
                ],
            }
        )
    largest_sk, largest = measured.largest
    return {
        "basis": "horizon",
        "anchor": ANCHOR.isoformat(),
        "saturation_history_days": saturated,
        "rows": rows,
        "pin_item_bytes": measured.items[-1][1],
        "largest_item": {"sk": largest_sk, "bytes": largest},
        "aggregate_bytes": measured.aggregate,
        "oracle_aggregate_bytes": measured.oracle_aggregate,
        "headroom": {
            "item_bytes": MAX_CONSERVATIVE_SERVICE_ITEM_BYTES - largest,
            "aggregate_bytes": MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES - measured.aggregate,
            "vs_previous_aggregate_bytes": PREVIOUS_CERTIFIED_AGGREGATE - measured.aggregate,
            "vs_previous_largest_item_bytes": PREVIOUS_CERTIFIED_LARGEST_ITEM - largest,
        },
        "by_history_days": {
            str(days): {
                "largest_item_bytes": measure_pin(days).largest[1],
                "aggregate_bytes": measure_pin(days).aggregate,
            }
            for days in (MEASURED_HISTORY_DAYS, PHASE_B_SPAN_DAYS, 731, saturated)
        },
        "invariance": {
            "history_days": [saturated, *INVARIANCE_DAYS],
            "byte_identical": all(
                measure_pin(days).items == measured.items
                and measure_pin(days).aggregate == measured.aggregate
                for days in INVARIANCE_DAYS
            ),
        },
        "negative_control_unbounded": {
            "item_over_400000_at_history_days": item_over,
            "aggregate_over_4000000_at_history_days": aggregate_over,
            "aggregate_bytes_at_measured_history": measure_pin(
                MEASURED_HISTORY_DAYS, bounded=False
            ).aggregate,
        },
        "over_cap_plans": [list(item) for item in over_cap_plans()],
    }


def _certificate() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(CERTIFICATE.read_text()))


def test_the_certified_rows_equal_the_committed_certificate() -> None:
    rows = {row["key"]: row for row in _certificate()["rows"]}
    assert list(rows) == [item.key for item in figures()]
    for item in figures():
        row = rows[item.key]
        assert (row["certified_count"], tuple(row["phases"])) == (
            item.certificate.count,
            item.certificate.phases,
        )
        assert (row["certification_seconds"], row["budget_seconds"]) == (
            item.seconds,
            item.budget_seconds,
        )
        assert row["write_seconds"]["value"] == item.write.seconds
        assert row["source_horizon_days"] == horizons()[item.key.split("#", 1)[0]].days
        # rev5 O2: each row's real builders and closed fixture reproduce its certified bound.
        assert row["derivation"] == item.certificate.derivation
        assert row["counter"]["builder"] == list(item.certificate.builder)
        assert row["counter"]["fixture"] == dict(item.certificate.fixture)
        assert item.certificate.builder, f"{item.key}: the counter names no real builder"
        assert ("reference_instant", BAKEABILITY_REFERENCE_INSTANT.isoformat()) in (
            item.certificate.fixture
        )


@cache
def roster_output() -> str:
    """Return the verbatim output of `bakeability --d2-roster` on this tree."""
    out = io.StringIO()
    with contextlib.chdir(ROOT), contextlib.redirect_stdout(out):
        assert d2_main() == 0
    return out.getvalue()


def test_the_recorded_roster_output_digest_reproduces_on_this_tree() -> None:
    """O4: the certificate's `--d2-roster` digest and each row's counter output are this tree's."""
    output = roster_output()
    (recorded,) = (
        item["sha256"]
        for item in _certificate()["commands"]
        if item["raw_output"] == "d2-roster.out"
    )
    assert recorded == f"sha256:{hashlib.sha256(output.encode()).hexdigest()}"
    lines = output.splitlines()
    for row in _certificate()["rows"]:
        assert row["counter"]["output"] in lines, f"{row['key']}: stale counter output"
        assert row["counter"]["output"].startswith(f"  {row['key']}: count ")


def test_pin_bytes_are_invariant_once_history_passes_every_horizon() -> None:
    """T1: past the horizon footprint, more history leaves every item and the aggregate fixed."""
    saturated = measure_pin(saturation_days())
    for days in INVARIANCE_DAYS:
        assert measure_pin(days).items == saturated.items
        assert measure_pin(days).aggregate == saturated.aggregate
    # Paired control: with the horizons removed the same pin grows with history.
    unbounded = [measure_pin(days, bounded=False).aggregate for days in (85, 463, 731)]
    assert unbounded == sorted(unbounded)
    assert unbounded[0] < unbounded[-1]


def test_the_saturated_pin_is_within_the_envelope_and_at_most_the_first_certificate() -> None:
    """T2: the certified (saturated) pin passes the oracle and is no larger than S0's first."""
    saturated = saturation_days()
    validate_pin(saturated)
    measured = measure_pin(saturated)
    assert measured.largest[1] <= PREVIOUS_CERTIFIED_LARGEST_ITEM
    assert measured.aggregate <= PREVIOUS_CERTIFIED_AGGREGATE
    # Paired control: horizons inflated to 1,000 days fail the same bound.
    inflated = measure_pin(3_650, horizon_days=1_000)
    assert inflated.aggregate > PREVIOUS_CERTIFIED_AGGREGATE


def test_the_unbounded_control_crosses_the_ceilings_the_first_certificate_found() -> None:
    """The negative control is the stop this remedy answers: an unbounded pin breaks."""
    item_over, aggregate_over = unbounded_crossings()
    assert aggregate_over < PHASE_B_SPAN_DAYS
    assert item_over < 3_650


def test_the_committed_pin_section_is_the_measured_one() -> None:
    assert _certificate()["pin_size"] == json.loads(json.dumps(certificate_pin_section()))
