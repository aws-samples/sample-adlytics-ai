# SPDX-License-Identifier: MIT-0
# ruff: noqa: T201
# print is exempted because this is a read-only repository gate run by
# `make check-bakeability`; like the other job entrypoints, its stdout is its interface.
"""Prove every declared artifact-served preset bakes within its snapshot and time bounds.

ADR-070 decision point 8: a declared preset is a checked promise. For every V2 page whose
temporal custom control is disabled — the artifact-served set — this build gate proves each
declared candidate preset fits one per-pack bake snapshot, and that the page's real
measurement batch fits the baker's timeout, so a page can never again declare a window its
serving path cannot honor.
"""

from __future__ import annotations

import functools
import math
import pathlib
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Any, TypedDict, cast

from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
)
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTooling,
    MeasurementToolRequest,
    PreparedMeasurementBatch,
    attach_pack_coverage,
    attach_roster,
    prepare_batch,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    CAMPAIGN_STREAMED_READS,
    PROFILE_CONTRACTS,
    SEGMENT_METRICS,
    TOTAL_METRICS,
    AudiencesArtifactRenderer,
    audiences_marketplace_count,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    OBSERVATION_MONTHS as AUDIENCES_OBSERVATION_MONTHS,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _Batch as _AudiencesBatch,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _contract as _audiences_contract,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _Served as _AudiencesServed,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    REPRESENTATIVE_CAMPAIGNS,
    campaign_portfolio_bake_plan_count,
    representative_bake_batch,
    representative_windows,
)
from adapters.jobs.preset_artifact_publisher.group_stream import RESIDENT_GROUPS, SinkSlot
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    NewCustomersRenderInput,
    new_customers_bake_plan_count,
    new_customers_labels,
)
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    _BatchBuilder as _NewCustomersBatchBuilder,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    _MarketPlan as _NewCustomersMarketPlan,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    _RostersOnce as _NewCustomersRostersOnce,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.reach_source import (
    ReachArtifactRenderer,
    ReachRenderInput,
    reach_bake_plan_count,
)
from adapters.jobs.preset_artifact_publisher.reach_source import (
    _Batch as _ReachBatch,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailHealthMeasurementBatchRequest,
    retail_channel_labels,
    retail_health_bake_plan_count,
    retail_health_measurement_batch,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    ARP_CONTRACT as RETENTION_ARP_CONTRACT,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    ARP_METRICS,
    STANDARD_METRICS,
    RetentionArtifactRenderer,
    retention_bake_body_bytes,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    STANDARD_CONTRACT as RETENTION_STANDARD_CONTRACT,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    _contract as _retention_contract,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    ScorecardMeasurementBatchRequest,
    scorecard_bake_plan_count,
    scorecard_labels,
    scorecard_measurement_batch,
)
from adapters.jobs.preset_artifact_publisher.transport import load_declared_bake_intents
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import MAX_PRESET_SNAPSHOT_PARTITIONS
from adapters.outbound.spend_roster import SPEND_FACT
from adapters.producer.packs import load_query_packs
from domain.measurement.folds import KeyedColumn
from domain.measurement.pack_coverage import PackCoverageVerdict, PackDayVersions
from domain.measurement.rosters import Roster
from domain.operations.bake_memory import (
    BAKE_BYTES_PER_ROW,
    BAKE_FIXED_BYTES,
    BAKE_MEMORY_TERMS,
    BAKE_RESIDENT_SHARE,
    BAKE_WRITE_WIDTH,
    BAKER_MEMORY_BYTES,
    MIB,
    WRITES_IN_FLIGHT_BYTES,
    BakeMemoryTerms,
    write_buffer_bytes,
)
from domain.operations.bake_scheduling import (
    BakeTaskLoad,
    LanePlan,
    task_seconds,
    two_lane_partition,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CampaignPortfolioSortField,
    campaign_roster_shard_count,
)
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    is_calendar_preset,
)
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    SIBLING_INDEX_SHARD_ENTRIES,
)
from domain.presentation.reach import FREQUENCY_DECLARED_PERIODS, OVERLAP_DECLARED_PERIODS
from domain.presentation.retail_health import RETAIL_HEALTH_MAX_ROWS
from domain.presentation.retention import ARP_PATH, STANDARD_PATH
from domain.presentation.temporal import preset_days
from domain.producer.models import DEFAULT_CADENCE_LAG_DAYS
from domain.semantics.amc_instance import AmcInstanceKind
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.calendar_periods import (
    CalendarPeriod,
    latest_complete_period,
    longest_grain_days,
    period_containing,
    vintage_ending,
)
from domain.semantics.identity import AdProductKey, ChannelKey, EcosystemKey, PlatformKey
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.query import BAKE_LONGEST_WINDOW_DAYS, BAKE_MAX_ROWS
from ports.outbound.rosters import (
    CampaignHistories,
    CampaignHistoriesRequest,
    CampaignHistory,
    RosterRequest,
)
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping

    from domain.data_contracts.contract import DataContract
    from domain.measurement.planning import QueryPlan
    from domain.presentation.page_schemas import PresentationPageSchemaSet

__all__ = [
    "BAKER_LAMBDA_TIMEOUT_SECONDS",
    "BAKER_MEMORY_BYTES",
    "BAKER_TIMEOUT_SECONDS",
    "BAKE_RESIDENT_SHARE",
    "CERTIFIED_QUERY_CAP",
    "MAX_PRESET_SNAPSHOT_PARTITIONS",
    "PER_OBJECT_WRITE_MS",
    "PER_QUERY_SECONDS",
    "WRITE_ALLOWANCE_SECONDS",
    "WRITE_WIDTH_MEASURED_MAX",
    "ArtifactServedPage",
    "BakeabilityFinding",
    "RosterTaskCertificate",
    "WriteWidth",
    "artifact_served_pages",
    "audiences_phases",
    "campaign_bake_object_count",
    "certify_roster_tasks",
    "derive_write_width",
    "evaluate_bakeability",
    "main",
    "roster_plan_counts",
    "sibling_index_shard_count",
    "write_allowance_findings",
    "write_term_object_count",
    "write_width_findings",
]

# Conservative per-execution latency budget. The observed envelope in
# docs/ui/campaign-portfolio/operational-design.md §1 budgets ≈1,300 executions to roughly
# 6-12 minutes at the configured concurrency — a few seconds per execution; 30 s per query
# keeps roughly an order of magnitude of headroom over that observation. The contract
# `bake_tasks` block is its one home, shared with the GRAPH bake scheduler.
PER_QUERY_SECONDS = int(BAKE_TASKS["per_query_seconds"])

# Must match BAKE_TIMEOUT_SECONDS in infra/lib/stacks/publisher-stack.ts — the baker
# Lambda's timeout and the SQS visibility base. A drift here makes this gate prove the
# wrong promise.
BAKER_LAMBDA_TIMEOUT_SECONDS = 900
WRITE_ALLOWANCE_SECONDS = float(BAKE_TASKS["lambda_timeout_seconds"]) / 2
"""The write half of one bake invocation (ADR-144, class A): a page's statement deadline
reserves its row's `write_seconds` before any statement starts, so every row's write term must
fit this half, leaving the other for statements. Derived from the contract's Lambda timeout."""
# The budget this gate proves one variant's bake within. S6 §2.8 raised the Lambda to 900 s
# because one graph bake node iterates every registered intent in one invocation; this gate
# still proves one variant per invocation, so each variant keeps its 300 s budget rather than
# taking the whole invocation.
BAKER_TIMEOUT_SECONDS = 300

# The Baker's in-process Athena cap Q every bake budget is certified at (plan S1); the deployed
# `athenaStartBudget.bakerStartCap` may not go below it, so this gate's waves are an upper bound.
CERTIFIED_QUERY_CAP = int(BAKE_TASKS["certified_query_cap"])
CAMPAIGN_ORDERING_COUNT = len(tuple(CampaignPortfolioSortField)) * 2
CAMPAIGN_GOVERNED_CHANNEL_COUNT = 5
"""The governed channels in contracts/shared/media-taxonomy.yaml; a test holds them equal."""
CAMPAIGN_MAX_FILTER_SIBLINGS = 2 + CAMPAIGN_GOVERNED_CHANNEL_COUNT
"""All channels, Sponsored ads and one per channel: up to seven siblings per preset (ADR-127)."""
CAMPAIGN_SIZING_ESTATES = (1_000, 10_000, 100_000)
"""The campaign counts `main` prints each variant's object count at: the sizing table's source."""


def sibling_index_shard_count(siblings: int) -> int:
    """Count the index shards a stage writes for `siblings` sibling pairs (ADR-145)."""
    return math.ceil(siblings / SIBLING_INDEX_SHARD_ENTRIES)


def _with_index(pair_objects: int) -> int:
    """Pair objects (a parent pair and its sibling pairs) plus the index shards they need."""
    return pair_objects + sibling_index_shard_count(pair_objects // 2 - 1)


def campaign_bake_object_count(filter_populations: tuple[int, ...]) -> int:
    """Count the immutable objects one Campaigns variant writes for its filters' populations.

    Each filter writes every ordering's segments and its name roster's shards, each a public
    body and a private custody; its canonical first segment is the filter's own parent (the
    pointer's body for All channels), so a filter of S segments and R roster shards writes
    2 x (orderings x S + R) objects. Every pair but the parent's is a sibling, and the stage
    indexes them in shards of `SIBLING_INDEX_SHARD_ENTRIES` (ADR-145). The local estate
    acceptance holds this count equal to a real seven-sibling bake's.
    """
    return _with_index(
        sum(
            2
            * (
                CAMPAIGN_ORDERING_COUNT * max(1, math.ceil(count / CAMPAIGN_PORTFOLIO_MAX_ROWS))
                + campaign_roster_shard_count(count)
            )
            for count in filter_populations
        )
    )


RETAIL_HEALTH_BAKE_BODY_COUNT = _with_index(2 + 2 * RETAIL_HEALTH_MAX_ROWS + 2)
"""The parent pair, one child pair per served row, the Products and ads pair (render format
12), and the index shard binding them."""

# --- ADR-143, ADR-152: the memory model (the gate counts everything a row holds) -------------
# Its constants live in `domain.operations.bake_memory`, where `BAKE_MAX_ROWS` is derived.

SIZING_MEMORY_MIB = (1024, 2048, 4096, 10240)
"""The Baker sizes `main` derives the ceiling at: the README sizing table's source."""

_DAYS = "date_key"


def fixed_axes(root: pathlib.Path) -> dict[str, int]:
    """Bound each fixed grain column by its governed cardinality (ADR-152).

    The marketplaces the commerce bridge maps, and each media-taxonomy axis: ecosystems,
    platforms, channels, ad products, exposure groups and acquisition channel groups. A read
    over only these and its window's days is bounded by them, never by the catalog. An axis
    with no governed enumeration keeps the population's width.
    """
    taxonomy = load_media_taxonomy(root)
    kinds = (
        ("ecosystem_key", EcosystemKey),
        ("platform_key", PlatformKey),
        ("channel_key", ChannelKey),
        ("ad_product_key", AdProductKey),
    )
    axes = {
        column: sum(isinstance(term.key, kind) for term in taxonomy.terms) for column, kind in kinds
    }
    axes["exposure_group"] = len({item.exposure_group for item in taxonomy.exposure_source_sets})
    axes["acquisition_channel_group"] = len(
        {item.acquisition_channel_group for item in taxonomy.exposure_source_sets}
    )
    axes["marketplace_key"] = audiences_marketplace_count(root)
    return axes


def _certified_width(plan: QueryPlan, max_rows: int, axes: Mapping[str, int]) -> int:
    """Return a held slot's width: its fixed axes' own bound, or `max_rows` for a population."""
    if not plan.group_by or any(column != _DAYS and column not in axes for column in plan.group_by):
        return max_rows
    bound = 1
    for column in plan.group_by:
        bound *= plan.window.days if column == _DAYS else axes[column]
    return min(bound, max_rows)


@dataclass(frozen=True, slots=True)
class ResidentModel:
    """One roster row's resident bytes, term by term (03 §3.5), at one held ceiling."""

    key: str
    ceiling: int
    writes: int
    raw: int
    shaped: int
    kept: int
    render: int
    memory_bytes: int
    records: int = 0

    @property
    def total(self) -> int:
        """`resident(row)`: the sum of every term."""
        return self.writes + self.raw + self.shaped + self.kept + self.render + self.records

    @property
    def limit(self) -> int:
        """`BAKE_RESIDENT_SHARE x BAKER_MEMORY_BYTES`."""
        return int(BAKE_RESIDENT_SHARE * self.memory_bytes)

    @property
    def certified(self) -> bool:
        """Whether the row's resident bytes fit its share of the baker."""
        return self.total <= self.limit

    def terms(self) -> str:
        """Print every term, so the gate's number is derived in the open, never typed."""
        return (
            f"W {self.writes} + raw {self.raw} + shaped {self.shaped} + kept {self.kept} + "
            f"render {self.render} + records {self.records} = {self.total} B <= {self.limit} B "
            f"({BAKE_RESIDENT_SHARE:g} x {self.memory_bytes}) at ceiling {self.ceiling}"
        )


def resident(  # noqa: PLR0913 -- one row's certified widths, the ceiling, terms and memory
    certificate: RowCertificate,
    key: str,
    *,
    ceiling: int = BAKE_MAX_ROWS,
    terms: BakeMemoryTerms = BAKE_MEMORY_TERMS,
    memory_bytes: int = BAKER_MEMORY_BYTES,
    axes: Mapping[str, int] | None = None,
    writes: int = WRITES_IN_FLIGHT_BYTES,
) -> ResidentModel:
    """Model one row's resident bytes with every held ceiling-wide slot at `ceiling` (03 §3.5).

        resident(row) = W + Q x max_held x B_raw + sum(held widths) x B_shaped
                        + sum(fold max_kept) x B_kept + max_held x B_render
                        + sum(record stores' max_kept) x B_record

    A slot certified at `BAKE_MAX_ROWS` is counted at `ceiling`, so the probe can ask what each
    candidate value means; a slot with its own smaller bound (a fixed axis, the ranked 50)
    keeps it. At most Q slots' raw rows exist at once (the shaping seam). `writes` is W, the write
    buffer at the contract's width unless the width derivation asks about another.
    """
    fixed = {"marketplace_key": 1} if axes is None else axes
    widths = tuple(
        _certified_width(plan, ceiling if width == BAKE_MAX_ROWS else width, fixed)
        for plan, width in zip(certificate.plans, certificate.widths, strict=False)
    )
    kept = tuple(
        ceiling if item == BAKE_MAX_ROWS else item
        for item in (*certificate.kept, *certificate.retained)
    )
    records = tuple(ceiling if item == BAKE_MAX_ROWS else item for item in certificate.records)
    held = max(widths, default=0)
    return ResidentModel(
        key=key,
        ceiling=ceiling,
        writes=writes,
        raw=CERTIFIED_QUERY_CAP * held * terms.raw,
        shaped=sum(widths) * terms.shaped,
        kept=sum(kept) * terms.kept,
        render=held * terms.render,
        memory_bytes=memory_bytes,
        records=sum(records) * terms.record,
    )


@functools.cache
def roster_certificates(root: pathlib.Path) -> tuple[tuple[str, RowCertificate], ...]:
    """Certify every roster row (both bake entries) on its own counter, in roster order.

    Cached per root: the counters are pure over the checked-out contracts, and the memory model
    and the row-ceiling probe ask for the same rows at each candidate ceiling.
    """
    rows: list[tuple[str, RowCertificate]] = []
    for row in BAKE_TASKS["roster"]:
        page, variant = str(row["page"]), str(row["variant"])
        certificate = _TASK_COUNTERS[page](root, variant)
        rows.append((f"{page}#{variant}", certificate))
    return tuple(rows)


def longest_daily_window_days(rows: tuple[tuple[str, RowCertificate], ...]) -> int:
    """Return the longest window any bake read sums day by day, in days.

    A daily series (grouped by `date_key`) spans at most its window's days. A periodic read is
    one row set per complete period whatever its length (a quarter, a vintage), and a read that
    sums its window into one row per key (Reach's quarter campaign count) multiplies nothing.
    """
    return max(
        (
            plan.window.days
            for _key, certificate in rows
            for plan in certificate.plans
            if not plan.periodic and _DAYS in plan.group_by
        ),
        default=0,
    )


@dataclass(frozen=True, slots=True)
class BindingRow:
    """The roster row whose resident bytes reach the Baker's share at the smallest ceiling."""

    key: str
    fixed_bytes: int
    bytes_per_row: int
    ceiling: int


def binding_row(models: tuple[tuple[ResidentModel, ResidentModel], ...]) -> BindingRow:
    """Read each row's intercept and slope from two adjacent ceilings; return the binding one.

    `models` pairs each row's model at V and V + 1. `resident` is affine in V, so the difference
    is the row's bytes per held row and the rest its fixed bytes. A row with no ceiling-wide
    slot never binds.
    """
    candidates: list[BindingRow] = []
    for at, above in models:
        slope = above.total - at.total
        if slope <= 0:
            continue
        fixed = at.total - slope * at.ceiling
        candidates.append(BindingRow(at.key, fixed, slope, (at.limit - fixed) // slope))
    if not candidates:
        raise ValueError("no roster row holds a ceiling-wide slot.")
    return min(candidates, key=lambda item: (item.ceiling, item.key))


def memory_findings(root: pathlib.Path) -> tuple[BakeabilityFinding, ...]:
    """Refuse a row whose resident bytes exceed its share, a drifted model or longest window.

    Every daily series a bake reads spans at most `BAKE_LONGEST_WINDOW_DAYS`, so a page policy
    that adds a longer one fails here rather than under-sizing every reduction. A read that sums
    its window into one row per key is no series, whatever its length (ADR-152).
    `BAKE_FIXED_BYTES` and `BAKE_BYTES_PER_ROW` are held equal to the binding row's own, so the
    derived `BAKE_MAX_ROWS` is the ceiling the real batches certify (ADR-152).
    """
    rows = roster_certificates(root)
    axes = fixed_axes(root)
    findings: list[BakeabilityFinding] = []
    bound = binding_row(
        tuple(
            zip(
                memory_models(root, ceiling=BAKE_MAX_ROWS),
                memory_models(root, ceiling=BAKE_MAX_ROWS + 1),
                strict=True,
            )
        )
    )
    if (bound.fixed_bytes, bound.bytes_per_row) != (BAKE_FIXED_BYTES, BAKE_BYTES_PER_ROW):
        findings.append(
            BakeabilityFinding(
                "roster",
                "bake-memory-derivation",
                f"{bound.key} binds at {bound.fixed_bytes} B + {bound.bytes_per_row} B per row; "
                f"bake_memory declares {BAKE_FIXED_BYTES} B + {BAKE_BYTES_PER_ROW} B.",
            )
        )
    longest = longest_daily_window_days(rows)
    if longest > BAKE_LONGEST_WINDOW_DAYS:
        findings.append(
            BakeabilityFinding(
                "roster",
                "bake-longest-window",
                f"BAKE_LONGEST_WINDOW_DAYS is {BAKE_LONGEST_WINDOW_DAYS}; the longest daily "
                f"series a bake reads spans {longest} days.",
            )
        )
    for key, certificate in rows:
        model = resident(certificate, key, axes=axes)
        if not model.certified:
            findings.append(
                BakeabilityFinding(
                    key.split("#", 1)[0], "bake-memory-model", f"{key}: {model.terms()}"
                )
            )
    return tuple(findings)


def memory_models(  # noqa: PLR0913 -- the root, the candidate ceiling, memory, terms and W
    root: pathlib.Path,
    *,
    ceiling: int = BAKE_MAX_ROWS,
    memory_bytes: int = BAKER_MEMORY_BYTES,
    terms: BakeMemoryTerms = BAKE_MEMORY_TERMS,
    writes: int = WRITES_IN_FLIGHT_BYTES,
) -> tuple[ResidentModel, ...]:
    """Model every D2 row at one ceiling, memory size, set of terms and write buffer."""
    axes = fixed_axes(root)
    return tuple(
        resident(
            certificate,
            key,
            ceiling=ceiling,
            terms=terms,
            memory_bytes=memory_bytes,
            axes=axes,
            writes=writes,
        )
        for key, certificate in roster_certificates(root)
    )


def memory_binding_row(
    root: pathlib.Path,
    *,
    memory_bytes: int = BAKER_MEMORY_BYTES,
    writes: int = WRITES_IN_FLIGHT_BYTES,
) -> BindingRow:
    """Return the binding row and its ceiling at one memory size and write buffer."""
    return binding_row(
        tuple(
            zip(
                memory_models(root, memory_bytes=memory_bytes, writes=writes),
                memory_models(
                    root, ceiling=BAKE_MAX_ROWS + 1, memory_bytes=memory_bytes, writes=writes
                ),
                strict=True,
            )
        )
    )


# Budget for one sequential immutable write (an S3 PutObject of at most one capped body) from
# the baker. A budget, not a measurement: the deployed bake's duration is its live check. On dev
# one chain's measured equivalent is inside it: 8 in flight at 9.07-11.62 ms per fresh Campaigns
# object (four bakes, 2026-10-25) and 12.36 ms per fresh Audiences object (trace 10a33158,
# 2026-10-27) is 72.6-98.9 ms per object per chain [derived].
PER_OBJECT_WRITE_MS = 100


@dataclass(frozen=True, slots=True)
class BakeStorageEstimate:
    """Conservative immutable-body fan-out and byte terms for one variant bake."""

    body_count: int
    maximum_body_bytes: int
    total_bytes: int

    def __post_init__(self) -> None:
        if self.body_count < 1 or self.maximum_body_bytes < 1 or self.total_bytes < 1:
            raise ValueError("bake storage estimate terms must be positive.")
        if self.total_bytes < self.maximum_body_bytes:
            raise ValueError("bake storage total cannot be smaller than one body.")


def write_term_object_count(page: str) -> int:
    """Count the objects a page's certified write term writes one after another.

    Audiences' parts and Campaigns' segments grow with their campaigns, so their write term is
    the whole write allowance (`page_write_seconds`) and the stage's runtime deadline is their
    one write limit (ADR-145): this is what that term writes at `PER_OBJECT_WRITE_MS` each,
    sequentially. The stage writes several chains at once, so it under-counts what the term
    holds.
    """
    return int(page_write_seconds(page).seconds * 1_000) // PER_OBJECT_WRITE_MS


# --- ADR-145: the write width, derived from the memory model and the write budget -----------

AUDIENCES_OBJECTS_PER_CAMPAIGN = Fraction(63, 30)
"""[measured on dev] objects one fresh Audiences bake wrote per DSP campaign: 63 PutObject (30
campaign pages, one index shard, the parent body and custody) for 30 campaigns (trace
10a33158, 2026-10-27). Each campaign there had one 50-row page; a campaign with more segments
writes more pages, so the stage's runtime write deadline stays the page's one write limit."""

WRITE_WIDTH_MEASURED_MAX = 64
"""[measured] the widest width the write-throughput probe ran against the dev bucket, from one
operator host (2026-10-27, validation/RESULTS.md): sibling throughput still rose from 32 to 64
pairs in flight in every arm, with no 503 SlowDown, so no knee was reached at or below it. A
wider stage is unmeasured, so the derivation stops here."""

_WRITE_ALLOWANCE_MS = int(WRITE_ALLOWANCE_SECONDS * 1_000)
_GROWING_WRITE_PAGES = ("audiences", "campaigns")
"""The pages whose objects grow with their campaigns; every other page writes a fixed set."""


def ceiling_objects(page: str, ceiling: int) -> int:
    """Count the objects a growing page writes at `ceiling` campaigns.

    Campaigns: `campaign_bake_object_count` with All channels, Sponsored ads and one channel each
    holding every campaign, its at-most count. Audiences: the measured objects per DSP campaign.
    """
    if page == "campaigns":
        return campaign_bake_object_count((ceiling, ceiling, ceiling))
    if page == "audiences":
        return math.ceil(ceiling * AUDIENCES_OBJECTS_PER_CAMPAIGN)
    raise ValueError(f"{page} writes a fixed set of objects, not one that grows.")


def width_for_objects(objects: int) -> int:
    """Return `ceil(objects x PER_OBJECT_WRITE_MS / WRITE_ALLOWANCE)`, the chains writing them."""
    return max(1, -(-objects * PER_OBJECT_WRITE_MS // _WRITE_ALLOWANCE_MS))


@dataclass(frozen=True, slots=True)
class WriteWidth:
    """The write width one Baker memory derives, and the terms it is derived from."""

    width: int
    bound: BindingRow
    """The memory model's binding row at this width's write buffer, and its ceiling."""
    page: str
    """The growing page whose objects at that ceiling need the most chains."""
    objects: int
    capped: bool
    """Whether those objects need more chains than `WRITE_WIDTH_MEASURED_MAX`."""

    @property
    def writable_objects(self) -> int:
        """The objects `width` chains write within the write allowance at the budget."""
        return self.width * _WRITE_ALLOWANCE_MS // PER_OBJECT_WRITE_MS

    def terms(self) -> str:
        """Print the derivation, so the gate's width is derived in the open, never typed."""
        text = (
            f"width {self.width}: ceil({self.objects} {self.page} objects at BAKE_MAX_ROWS="
            f"{self.bound.ceiling} x {PER_OBJECT_WRITE_MS} ms / {_WRITE_ALLOWANCE_MS} ms) = "
            f"{width_for_objects(self.objects)}, W {write_buffer_bytes(self.width)} B"
        )
        if self.capped:
            text += (
                f"; capped at the widest measured width {WRITE_WIDTH_MEASURED_MAX}, whose "
                f"{self.writable_objects} objects are the page's write ceiling"
            )
        return text


def derive_write_width(root: pathlib.Path, memory_bytes: int) -> WriteWidth:
    """Return the smallest width whose own memory ceiling's objects it writes in time (ADR-145).

    W grows with the width and lowers the ceiling, so the width is the smallest one the objects
    at its own ceiling need no more than, capped at `WRITE_WIDTH_MEASURED_MAX`.
    """
    derived: WriteWidth | None = None
    for width in range(1, WRITE_WIDTH_MEASURED_MAX + 1):
        bound = memory_binding_row(
            root, memory_bytes=memory_bytes, writes=write_buffer_bytes(width)
        )
        objects, page = max(
            (ceiling_objects(page, bound.ceiling), page) for page in _GROWING_WRITE_PAGES
        )
        derived = WriteWidth(width, bound, page, objects, capped=False)
        if width_for_objects(objects) <= width:
            return derived
    return replace(cast("WriteWidth", derived), capped=True)


def write_width_findings(root: pathlib.Path) -> tuple[BakeabilityFinding, ...]:
    """Refuse a contract width other than the one the deployed memory derives (ADR-145)."""
    derived = derive_write_width(root, BAKER_MEMORY_BYTES)
    if derived.width == BAKE_WRITE_WIDTH:
        return ()
    return (
        BakeabilityFinding(
            "roster",
            "bake-write-width",
            f"bake_tasks.baker_write_width is {BAKE_WRITE_WIDTH}; "
            f"{BAKER_MEMORY_BYTES // MIB} MiB derives {derived.terms()}.",
        ),
    )


@dataclass(frozen=True, slots=True)
class BakeabilityFinding:
    """One deterministic reason a declared page cannot keep its bake promise."""

    page_id: str
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class ArtifactServedPage:
    """One page served exclusively from baked preset artifacts, and its declared presets."""

    page_id: str
    candidate_presets: tuple[TemporalPresetId, ...]
    calendar_compares: bool = False
    """Whether a calendar preset compares with the prior calendar period (two periods read)."""
    periodic_snapshots: bool = False
    """Whether each calendar period is one periodic publication rather than day partitions."""


_CALENDAR_MAX_DAYS = {
    TemporalPresetId.LAST_COMPLETE_WEEK: 7,
    TemporalPresetId.LAST_COMPLETE_MONTH: 31,
    TemporalPresetId.LAST_COMPLETE_QUARTER: 92,
}


def preset_partitions(page: ArtifactServedPage, preset: TemporalPresetId) -> int:
    """Day-partitions per source pack one bake of `preset` reads (ADR-070, 2026-09-16).

    A rolling preset reads its window and the equal prior. A calendar preset reads its longest
    period, and the prior period too only when the page compares calendar periods; on an
    optional equal-prior page a calendar period compares nothing (ADR-100 point 8).
    """
    if not is_calendar_preset(preset):
        return 2 * preset_days(preset)
    if page.periodic_snapshots:
        return 2 if page.calendar_compares else 1
    return _CALENDAR_MAX_DAYS[preset] * (2 if page.calendar_compares else 1)


def artifact_served_pages(schemas: PresentationPageSchemaSet) -> tuple[ArtifactServedPage, ...]:
    """Select every V2 page whose temporal custom control is disabled (ADR-070 point 3)."""
    return tuple(
        ArtifactServedPage(
            page.page_id,
            (
                page.time_policy.candidate_presets
                if isinstance(page.time_policy, PresentationTemporalPolicy)
                else ()
            ),
            isinstance(page.time_policy, PresentationTemporalPolicy)
            and page.time_policy.comparison_policy
            is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD,
            isinstance(page.time_policy, PresentationTemporalPolicy)
            and page.time_policy.display_grains == (PageTime.DECLARED_PERIOD,),
        )
        for page in schemas.pages
        if not isinstance(page.time_policy, PresentationTemporalPolicy)
        or not page.time_policy.custom_enabled
    )


def _page_bake_budget_seconds(page_id: str) -> int:
    """Return the largest generated task budget declared for one artifact-served page."""
    budgets = BAKE_TASKS["budget_seconds"]
    declared = [
        int(budgets[f"{row['page']}#{row['variant']}"])
        for row in BAKE_TASKS["roster"]
        if row["page"] == page_id
    ]
    return max(declared, default=BAKER_TIMEOUT_SECONDS)


def evaluate_bakeability(
    pages: tuple[ArtifactServedPage, ...],
    plan_counts: Mapping[str, int],
    storage: Mapping[str, BakeStorageEstimate],
) -> tuple[BakeabilityFinding, ...]:
    """Return every snapshot-bound and bake-budget finding at once, never just the first."""
    findings: list[BakeabilityFinding] = []
    for page in pages:
        for preset in page.candidate_presets:
            # A per-pack bake snapshot attests both comparison windows — current plus the
            # required equal prior — of a daily-grain pack in one verified exact read (ADR-070,
            # 2026-10-25 amendment), so 2 x preset-days must fit its bound.
            required = preset_partitions(page, preset)
            if required > MAX_PRESET_SNAPSHOT_PARTITIONS:
                findings.append(
                    BakeabilityFinding(
                        page.page_id,
                        "preset-snapshot-bound",
                        (
                            f"preset {preset.value} needs {required} day-partitions per "
                            f"source pack; one per-pack snapshot attests at most "
                            f"{MAX_PRESET_SNAPSHOT_PARTITIONS}."
                        ),
                    )
                )
        count = plan_counts.get(page.page_id)
        if count is None:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "bake-budget",
                    "no measurement batch is registered for this artifact-served page.",
                )
            )
            continue
        waves = math.ceil(count / CERTIFIED_QUERY_CAP)
        # The scheduler's per-task formula: query waves plus the tail. This gate budgets the
        # writes itself, sequentially per object, below, so none are counted here.
        seconds = task_seconds(count, CERTIFIED_QUERY_CAP, 0)
        page_budget = _page_bake_budget_seconds(page.page_id)
        if seconds > page_budget:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "bake-budget",
                    (
                        f"{count} measurement plans need {waves} waves of "
                        f"{CERTIFIED_QUERY_CAP} at {PER_QUERY_SECONDS} s and the "
                        f"{BAKE_TASKS['tail_seconds']} s tail — {seconds} s exceeds the "
                        f"{page_budget} s task budget."
                    ),
                )
            )
        estimate = storage.get(page.page_id)
        if estimate is None:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "artifact-storage-bound",
                    "no immutable-body storage estimate is registered for this page.",
                )
            )
            continue
        if estimate.maximum_body_bytes > MAX_PRESET_ARTIFACT_BYTES:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "artifact-storage-bound",
                    (
                        f"a {estimate.maximum_body_bytes}-byte body exceeds the "
                        f"{MAX_PRESET_ARTIFACT_BYTES}-byte artifact cap."
                    ),
                )
            )
        # Bodies stream (ADR-145): a bake holds the pairs in flight, the memory model's `W` term
        # (`resident`, ADR-143), never every body it writes, so no all-bodies share is checked.
        # The writes follow measurement in one invocation; an already over-budget batch
        # has its own finding.
        write_ms = estimate.body_count * PER_OBJECT_WRITE_MS
        timeout_ms = page_budget * 1_000
        if seconds <= page_budget and seconds * 1_000 + write_ms > timeout_ms:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "bake-write-budget",
                    (
                        f"{seconds} s of measurement and {estimate.body_count} writes at "
                        f"{PER_OBJECT_WRITE_MS} ms ({write_ms / 1_000:.1f} s) exceed the "
                        f"{page_budget} s task budget."
                    ),
                )
            )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class RosterTaskCertificate:
    """One roster task's certified envelope: its true plan count, its seconds and its budget."""

    key: str
    plan_count: int
    seconds: float
    budget_seconds: int

    @property
    def certified(self) -> bool:
        """Whether the task fits its contract budget at the certified query cap."""
        return self.seconds <= self.budget_seconds


_PAGE_COUNTERS: dict[str, Callable[[pathlib.Path, int], int]] = {
    "campaigns": campaign_portfolio_bake_plan_count,
    "retail-health": retail_health_bake_plan_count,
}
"""The real batch builder of each roster page; a roster page without one is a finding."""


def roster_plan_counts(root: pathlib.Path) -> dict[str, int]:
    """Count every roster task's true plans at its own window (plan 4.2; no 136-as-runtime).

    Keyed `<page>#<variant>` in roster order; a roster page with no registered batch builder
    is absent, and `certify_roster_tasks` reports it.
    """
    counts: dict[str, int] = {}
    for row in BAKE_TASKS["roster"]:
        page, variant = str(row["page"]), str(row["variant"])
        counter = _PAGE_COUNTERS.get(page)
        if counter is not None:
            counts[f"{page}#{variant}"] = counter(root, preset_days(TemporalPresetId(variant)))
    return counts


def certify_roster_tasks(
    plan_counts: Mapping[str, int],
) -> tuple[tuple[RosterTaskCertificate, ...], tuple[BakeabilityFinding, ...]]:
    """Certify each roster task's `task_seconds(count, Q10, write_seconds) <= budget` (3.1, 4.2).

    Every roster row is certified on its own true count, never a page total, so a variant can
    never borrow another's budget. A row with no count, or over its budget, is a finding.
    """
    certificates: list[RosterTaskCertificate] = []
    findings: list[BakeabilityFinding] = []
    write_seconds = BAKE_TASKS["write_seconds"]
    budgets = BAKE_TASKS["budget_seconds"]
    for row in BAKE_TASKS["roster"]:
        page = str(row["page"])
        if page not in _PAGE_COUNTERS:
            continue  # a D2 row: certified on its own counter by `d2_contract_findings`
        key = f"{page}#{row['variant']}"
        count = plan_counts.get(key)
        if count is None:
            findings.append(
                BakeabilityFinding(page, "roster-task-budget", f"{key} has no true plan count.")
            )
            continue
        certificate = RosterTaskCertificate(
            key,
            count,
            task_seconds(count, CERTIFIED_QUERY_CAP, write_seconds[page]),
            int(budgets[key]),
        )
        certificates.append(certificate)
        if not certificate.certified:
            findings.append(
                BakeabilityFinding(
                    page,
                    "roster-task-budget",
                    (
                        f"{key}: {count} plans need {certificate.seconds} s at Q"
                        f"{CERTIFIED_QUERY_CAP}, over its {certificate.budget_seconds} s budget."
                    ),
                )
            )
    return tuple(certificates), tuple(findings)


def main() -> int:
    """Report every deterministic bakeability finding for the declared V2 pages."""
    root = pathlib.Path.cwd()
    print("PRESET ARTIFACT BAKEABILITY CHECK")
    print("=" * 70)
    pages = artifact_served_pages(load_v2_page_schemas(root))
    task_counts = roster_plan_counts(root)
    certificates, roster_findings = certify_roster_tasks(task_counts)
    # The page-level gate budgets each roster page at its largest variant's true count. The five
    # D2 pages keep their page-level counts until spike S0 (plan A5.3) certifies each of their
    # roster rows on its own counter.
    plan_counts = {
        **{
            page: max(count for key, count in task_counts.items() if key.split("#", 1)[0] == page)
            for page in {key.split("#", 1)[0] for key in task_counts}
        },
        "scorecard": scorecard_bake_plan_count(root),
        "new-customers": new_customers_bake_plan_count(root),
        "reach": reach_bake_plan_count(root),
        # One query per standard metric for each governed marketplace; the live roster is
        # bounded elsewhere, while this gate needs a deterministic nonzero registration.
        "retention": len(STANDARD_METRICS),
        # Audiences' roster certificate: one model, at any campaign count (design 01 §6).
        "audiences": max(
            certificate.count
            for key, certificate in roster_certificates(root)
            if key.split("#", 1)[0] == "audiences"
        ),
    }
    retention_body = retention_bake_body_bytes(root)
    storage = {
        # Segments grow with campaigns: the objects its write term certifies, each at the cap.
        "campaigns": BakeStorageEstimate(
            write_term_object_count("campaigns"),
            MAX_PRESET_ARTIFACT_BYTES,
            write_term_object_count("campaigns") * MAX_PRESET_ARTIFACT_BYTES,
        ),
        # The parent body and custody, one child body and custody per displayed row, the
        # Products and ads part and its custody, the index.
        "retail-health": BakeStorageEstimate(
            RETAIL_HEALTH_BAKE_BODY_COUNT,
            MAX_PRESET_ARTIFACT_BYTES,
            RETAIL_HEALTH_BAKE_BODY_COUNT * MAX_PRESET_ARTIFACT_BYTES,
        ),
        # One body per variant: the Scorecard bakes no child, custody or sibling.
        "scorecard": BakeStorageEstimate(1, MAX_PRESET_ARTIFACT_BYTES, MAX_PRESET_ARTIFACT_BYTES),
        # One body per variant: New customers bakes no child, custody or sibling either.
        "new-customers": BakeStorageEstimate(
            1, MAX_PRESET_ARTIFACT_BYTES, MAX_PRESET_ARTIFACT_BYTES
        ),
        # One body per variant: Reach bakes no child, custody or sibling.
        "reach": BakeStorageEstimate(1, MAX_PRESET_ARTIFACT_BYTES, MAX_PRESET_ARTIFACT_BYTES),
        # One body per variant, at most the per-marketplace ceiling per governed marketplace.
        "retention": BakeStorageEstimate(1, retention_body, retention_body),
        # Parts grow with campaigns: the objects its write term certifies, each at the cap.
        "audiences": BakeStorageEstimate(
            write_term_object_count("audiences"),
            MAX_PRESET_ARTIFACT_BYTES,
            write_term_object_count("audiences") * MAX_PRESET_ARTIFACT_BYTES,
        ),
    }
    findings = (
        *evaluate_bakeability(pages, plan_counts, storage),
        *roster_findings,
        *write_allowance_findings(),
        *source_horizon_findings(root),
        *d2_contract_findings(root),
        *memory_findings(root),
        *write_width_findings(root),
    )
    if findings:
        for finding in sorted(findings, key=lambda item: (item.page_id, item.rule, item.message)):
            print(f"  [{finding.rule}] {finding.page_id}: {finding.message}")
        print(f"\n{len(findings)} bakeability finding(s).")
        return 1
    for page in pages:
        presets = ", ".join(preset.value for preset in page.candidate_presets)
        print(f"  {page.page_id}: presets [{presets}], {plan_counts[page.page_id]} plans")
    for certificate in certificates:
        print(
            f"  task {certificate.key}: {certificate.plan_count} plans, "
            f"{certificate.seconds} s <= {certificate.budget_seconds} s"
        )
    print(f"  WRITE_ALLOWANCE_SECONDS={WRITE_ALLOWANCE_SECONDS:g}")
    # ADR-143: the memory model, term by term, and the binding row.
    models = memory_models(root)
    print(
        f"  BAKE_MAX_ROWS={BAKE_MAX_ROWS}, BAKE_LONGEST_WINDOW_DAYS={BAKE_LONGEST_WINDOW_DAYS}, "
        f"BAKE_MAX_STREAMED_ROWS={BAKE_LONGEST_WINDOW_DAYS * BAKE_MAX_ROWS}; terms "
        f"B_raw {BAKE_MEMORY_TERMS.raw}, B_shaped {BAKE_MEMORY_TERMS.shaped}, B_kept "
        f"{BAKE_MEMORY_TERMS.kept}, B_render {BAKE_MEMORY_TERMS.render} B "
        f"({BAKE_MEMORY_TERMS.measured}); W {WRITES_IN_FLIGHT_BYTES} B"
    )
    for model in models:
        print(f"  memory {model.key}: {model.terms()}")
    # ADR-152: the ceiling is the deployed memory's, derived from the binding row; the sizing
    # table reads the same derivation at each larger Baker.
    print(
        f"  BAKE_MAX_ROWS = floor(({BAKE_RESIDENT_SHARE:g} x memory - {BAKE_FIXED_BYTES} B) / "
        f"{BAKE_BYTES_PER_ROW} B)"
    )
    # ADR-145: each size's write width is derived with its own write buffer, so each size's
    # ceiling counts the W that size would deploy.
    for memory_mib in SIZING_MEMORY_MIB:
        derived = derive_write_width(root, memory_mib * MIB)
        print(
            f"  {memory_mib} MiB Baker: BAKE_MAX_ROWS={derived.bound.ceiling} (binding "
            f"{derived.bound.key}); write {derived.terms()}"
        )
    for row in BAKE_TASKS["roster"]:
        page = str(row["page"])
        print(
            f"  write {page}#{row['variant']}: write_seconds "
            f"{float(BAKE_TASKS['write_seconds'][page]):g} s <= {WRITE_ALLOWANCE_SECONDS:g} s"
        )
    for campaigns in CAMPAIGN_SIZING_ESTATES:
        # At most: All channels, Sponsored ads and one channel each holding every campaign.
        objects = campaign_bake_object_count((campaigns, campaigns, campaigns))
        print(
            f"  campaigns at {campaigns} campaigns: at most {objects} objects per variant over "
            f"up to {CAMPAIGN_MAX_FILTER_SIBLINGS} filter siblings; the write term budgets "
            f"{write_term_object_count('campaigns')} sequential objects"
        )
    print("Every declared preset fits its per-pack snapshot and the bake budget.")
    return 0


# --- Spike S0 (plan A5.3): the D2 roster's per-row certified bounds -------------------------
#
# Every row is certified on its own (page, variant) counter, built with the page's real batch
# builder at its maximum cardinality. A row's `count` is the certified upper bound on the
# distinct requests a render executes (measurement plans plus roster reads); its `phases` are
# the sequential execution ceilings, used only for certification seconds.

BAKEABILITY_REFERENCE_YEAR = 2025
"""Calendar variants are certified at the longest instance over this year's 12 month starts."""
BAKEABILITY_REFERENCE_INSTANT = datetime(BAKEABILITY_REFERENCE_YEAR, 1, 1, tzinfo=UTC)
"""The one instant every counter that stamps a request uses: no counter reads a wall clock."""
_REFERENCE_DATES = tuple(date(BAKEABILITY_REFERENCE_YEAR, month, 1) for month in range(1, 13))
SEGMENT_ROSTER_PROFILE_READS = 1
"""Queries one profile-path segment roster issues at most (`SegmentRosterReader` census)."""
CAMPAIGN_HISTORY_READS = 2
"""Sequential queries the grouped campaign history walk issues at most: the older months, then
the part from a withheld month on (`SegmentRosterReader.campaign_histories` census)."""
_NOT_GRAPH_BAKED = frozenset(
    {"channel-performance", "workspace", "data-health", "media-efficiency"}
)


@dataclass(frozen=True, slots=True)
class RowCertificate:
    """One roster row's certified bound: its distinct request ceiling and sequential phases."""

    count: int
    phases: tuple[int, ...]
    derivation: str
    plans: tuple[QueryPlan, ...] = field(default=(), compare=False, repr=False)
    """The row's maximum-cardinality measurement plans (the pin certificate resolves them)."""
    rosters: tuple[RosterRequest, ...] = field(default=(), compare=False, repr=False)
    """The spend-roster requests the row can issue, one query each (`SpendRosterReader`)."""
    builder: tuple[str, ...] = field(default=(), compare=False, repr=False)
    """The real production builders the counter invoked, as `module.qualname` (rev5 O2)."""
    fixture: tuple[tuple[str, str], ...] = field(default=(), compare=False, repr=False)
    """The closed maximum-cardinality fixture's parameters, by name (rev5 O2)."""
    widths: tuple[int, ...] = field(default=(), compare=False, repr=False)
    """Each held measurement slot's certified row bound (`max_rows`), read from the batch."""
    kept: tuple[int, ...] = field(default=(), compare=False, repr=False)
    """Each reduction slot's enforced `max_kept`, read from the fold the source declares."""
    retained: tuple[int, ...] = field(default=(), compare=False, repr=False)
    """What a source keeps beyond its slots' own bound, each bounded: Audiences' groups queued
    and drawn beside the one each streamed read builds, and its campaign tallies and segments."""
    records: tuple[int, ...] = field(default=(), compare=False, repr=False)
    """Each compact per-entity store's bound (`KeyedColumns.max_kept`), counted once however
    many slots fold into it: Campaigns' one record per campaign (ADR-152)."""

    def __post_init__(self) -> None:
        if self.count < 1 or not self.phases or sum(self.phases) != self.count:
            raise ValueError("a row certificate's phases must sum to its positive count.")
        if any(phase < 1 for phase in self.phases):
            raise ValueError("every certified phase holds at least one request.")


def phased_task_seconds(phases: tuple[int, ...], q: int, write_seconds: float) -> float:
    """Certification seconds: each sequential phase runs in waves of at most `q`.

    One phase is `task_seconds` exactly. Phases are sequential because a later request needs an
    earlier result, so the waves are summed per phase, never over the pooled count.
    """
    if len(phases) == 1:
        return task_seconds(phases[0], q, write_seconds)
    waves = sum(math.ceil(count / q) for count in phases)
    return task_seconds(0, q, write_seconds) + waves * PER_QUERY_SECONDS


def d2_roster_keys(root: pathlib.Path) -> tuple[tuple[str, str], ...]:
    """Return the D2 roster: every declared intent of a GRAPH-baked page, in declaration order."""
    return tuple(
        (intent.page, str(intent.variant))
        for intent in load_declared_bake_intents(root).intents
        if intent.page not in _NOT_GRAPH_BAKED
    )


@dataclass
class _CountingRosters:
    """A roster port that records each request it reads and answers an empty roster."""

    read: list[RosterRequest] = field(default_factory=list[RosterRequest])

    def roster(self, request: RosterRequest) -> Roster:
        self.read.append(request)
        return Roster(
            request.kind,
            request.period,
            request.marketplace_key,
            request.platform_key,
            (),
            filters=request.filters,
        )


@dataclass(frozen=True, slots=True)
class _FullPackCoverage:
    """Every day of every window carries every pack-versioned column: the widest plan set."""

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        days: dict[date, int | None] = {
            window.start + timedelta(days=offset): _ANY_PACK_VERSION
            for window in request.windows
            for offset in range(window.days)
        }
        return tuple(PackDayVersions(item.contract_id, days) for item in request.contracts)


_ANY_PACK_VERSION = 1_000_000
"""A pack version at or above every declared `from_version`."""


def _contracts(root: pathlib.Path) -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(root))


def _marketplaces(root: pathlib.Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                mapping.target.value
                for mapping in load_commerce_identity_bridge(root).marketplace_mappings
            }
        )
    )


def _calendar_periods(variant: str) -> tuple[CalendarPeriod, ...]:
    """Every instance of a `last_complete_*` variant over the 12 reference month starts."""
    grain = {
        TemporalPresetId.LAST_COMPLETE_WEEK.value: TimeGrain.WEEK,
        TemporalPresetId.LAST_COMPLETE_MONTH.value: TimeGrain.MONTH,
        TemporalPresetId.LAST_COMPLETE_QUARTER.value: TimeGrain.QUARTER,
    }[variant]
    return tuple(
        dict.fromkeys(
            latest_complete_period(grain, reference - timedelta(days=1))
            for reference in _REFERENCE_DATES
        )
    )


def _largest(certificates: tuple[RowCertificate, ...]) -> RowCertificate:
    """Return the instance with the most requests, then the longest plans' windows."""
    chosen = max(
        certificates,
        key=lambda item: (item.count, sum(plan.window.days for plan in item.plans)),
    )
    instances = (
        "instances",
        f"{len(certificates)} over the 12 {BAKEABILITY_REFERENCE_YEAR} month starts; the largest",
    )
    return replace(chosen, fixture=(*chosen.fixture, instances))


def _single_phase(  # noqa: PLR0913 -- the row's batch, derivation and its O2 protocol
    prepared: PreparedMeasurementBatch,
    derivation: str,
    *,
    builder: tuple[str, ...],
    fixture: tuple[tuple[str, str], ...],
) -> RowCertificate:
    plans = _distinct_plans(prepared)
    return RowCertificate(
        len(plans),
        (len(plans),),
        derivation,
        plans,
        builder=builder,
        fixture=fixture,
        **_width_terms(prepared),
    )


def _distinct_plans(prepared: PreparedMeasurementBatch) -> tuple[QueryPlan, ...]:
    return tuple(plan.logical for plan in prepared.plans)


class _WidthTerms(TypedDict):
    widths: tuple[int, ...]
    kept: tuple[int, ...]
    records: tuple[int, ...]


def _width_terms(*batches: PreparedMeasurementBatch) -> _WidthTerms:
    """Read each slot's certified width: a held slot's `max_rows`, a fold's enforced `max_kept`.

    Read from the batch the source really builds, never declared beside it (ADR-143), so the
    memory model counts exactly what the code can hold.
    """
    widths: list[int] = []
    kept: list[int] = []
    stores: dict[int, int] = {}
    for prepared in batches:
        for operation in prepared.operations:
            reduction = operation.request.reduction
            for item in operation.plans:
                if reduction is None:
                    widths.append(item.max_rows)
                elif isinstance(reduction, SinkSlot):
                    # The group a streamed read builds; its queued and drawn groups are retained.
                    kept.append(reduction.group_max_kept)
                elif isinstance(reduction, KeyedColumn):
                    # Every column of one store is one record per entity: counted once.
                    stores[id(reduction.store)] = reduction.store.max_kept
                else:
                    kept.append(reduction().max_kept)
    return _WidthTerms(widths=tuple(widths), kept=tuple(kept), records=tuple(stores.values()))


def _symbols(*builders: Callable[..., object]) -> tuple[str, ...]:
    """Name each builder a counter invoked by its defining module and qualified name."""
    return tuple(f"{item.__module__}.{item.__qualname__}" for item in builders)


def _range(window: DateRange | None) -> str:
    return "none" if window is None else f"{window.start.isoformat()}..{window.end.isoformat()}"


_REFERENCE_FIXTURE = ("reference_instant", BAKEABILITY_REFERENCE_INSTANT.isoformat())
_COUNTING_ROSTERS = ("roster_port", "counting port answering an empty roster")


def _campaigns_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Return the real portfolio bake batch, through a counting roster port (ADR-152).

    Its 26 reads fold into one compact record per campaign and seven filters' daily sums, so
    the certificate is the same at any population; the store's bound is `BAKE_MAX_ROWS`.
    """
    days = preset_days(TemporalPresetId(variant))
    period, prior = representative_windows(days)
    bridge = load_commerce_identity_bridge(root)
    rosters = _CountingRosters()
    service = CampaignArtifactMeasurementService(
        CampaignMeasurementDependencies(
            scope=AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
            catalog=load_catalog(root),
            contracts=_contracts(root),
            clock=lambda: BAKEABILITY_REFERENCE_INSTANT,
            rosters=rosters,
        )
    )
    batch = representative_bake_batch(service, bridge, days, CAMPAIGN_MAX_FILTER_SIBLINGS)
    plans = _distinct_plans(batch.prepared)
    phases = (len(rosters.read), len(plans)) if rosters.read else (len(plans),)
    return RowCertificate(
        sum(phases),
        phases,
        f"portfolio bake batch over {variant} and its equal prior, at any campaign count: "
        f"22 compares into one compact record per campaign, 4 daily sums over "
        f"{CAMPAIGN_MAX_FILTER_SIBLINGS} filters; {len(rosters.read)} spend-roster reads",
        plans,
        tuple(rosters.read),
        builder=_symbols(CampaignArtifactMeasurementService.portfolio_batch),
        **_width_terms(batch.prepared),
        fixture=(
            _REFERENCE_FIXTURE,
            ("window", _range(period)),
            ("prior_window", _range(prior)),
            ("campaign_selections", str(REPRESENTATIVE_CAMPAIGNS)),
            ("selection_marketplaces", "1"),
            ("filters", str(CAMPAIGN_MAX_FILTER_SIBLINGS)),
            ("sort", "spend desc"),
            _COUNTING_ROSTERS,
        ),
    )


def _retail_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Return the one Retail batch: every read page-sized, constant in the catalog (ADR-152)."""
    period, prior = representative_windows(preset_days(TemporalPresetId(variant)))
    request = RetailHealthMeasurementBatchRequest(
        catalog=load_catalog(root),
        contracts=_contracts(root),
        period=period,
        prior_period=prior,
        computed_at=BAKEABILITY_REFERENCE_INSTANT,
        channel_count=len(retail_channel_labels(load_media_taxonomy(root))),
    )
    main = retail_health_measurement_batch(request).prepared
    plans = _distinct_plans(main)
    return RowCertificate(
        len(plans),
        (len(plans),),
        f"one batch of {len(plans)} over {variant} and its prior: compares and trends, the "
        "ranked top-50 keys with the population count and sums, the 50's rows, series and "
        "product-ad reads, and the three movers (constant in the catalog, ADR-152)",
        plans,
        builder=_symbols(retail_health_measurement_batch),
        **_width_terms(main),
        fixture=(
            _REFERENCE_FIXTURE,
            ("window", _range(period)),
            ("prior_window", _range(prior)),
        ),
    )


def _scorecard_row(root: pathlib.Path, variant: str) -> RowCertificate:
    catalog = load_catalog(root)
    labels = scorecard_labels(root, catalog)

    def batch(period: DateRange, prior: DateRange | None) -> RowCertificate:
        prepared = scorecard_measurement_batch(
            ScorecardMeasurementBatchRequest(
                catalog=catalog,
                contracts=_contracts(root),
                models=tuple(key for key, _ in labels.models),
                period=period,
                prior_period=prior,
                computed_at=BAKEABILITY_REFERENCE_INSTANT,
                marketplaces=labels.marketplaces,
            )
        ).prepared
        return _single_phase(
            prepared,
            f"one batch per governed marketplace ({len(labels.marketplaces)}), "
            + ("window and equal prior" if prior is not None else "the calendar period alone"),
            builder=_symbols(scorecard_measurement_batch),
            fixture=(
                _REFERENCE_FIXTURE,
                ("window", _range(period)),
                ("prior_window", _range(prior)),
                ("marketplaces", str(len(labels.marketplaces))),
                ("models", str(len(labels.models))),
            ),
        )

    if is_calendar_preset(TemporalPresetId(variant)):
        return _largest(tuple(batch(item.window, None) for item in _calendar_periods(variant)))
    return batch(*representative_windows(preset_days(TemporalPresetId(variant))))


def _new_customers_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Every section planned for every marketplace, rosters read once per bake (`_RostersOnce`)."""
    catalog = load_catalog(root)
    labels = new_customers_labels(root, catalog)
    builder = _NewCustomersBatchBuilder(  # pyright: ignore[reportPrivateUsage]
        catalog, _contracts(root), lambda: BAKEABILITY_REFERENCE_INSTANT, len(labels.ad_products)
    )

    def batch(render: NewCustomersRenderInput) -> RowCertificate:
        covered = PackCoverageVerdict(render.period, True, render.period.start, True)
        plans_in = tuple(
            _NewCustomersMarketPlan(  # pyright: ignore[reportPrivateUsage]
                market, covered, buyers_published=True, channel_published=True
            )
            for market in labels.marketplaces
        )
        requests, _labels = builder.batch(render, plans_in)
        port = _CountingRosters()
        once = _NewCustomersRostersOnce(port)  # pyright: ignore[reportPrivateUsage]
        prepared = prepare_batch(
            tuple(
                attach_roster(attach_pack_coverage(item, _FullPackCoverage()), once)
                for item in requests
            )
        )
        plans = _distinct_plans(prepared)
        # The reads run one at a time while the batch is prepared: one sequential phase each.
        phases = (*(1 for _ in port.read), len(plans))
        return RowCertificate(
            sum(phases),
            phases,
            f"{len(labels.marketplaces)} marketplace(s), every section published; "
            f"{len(port.read)} spend-roster read(s), each once per bake, then measurement",
            plans,
            tuple(port.read),
            **_width_terms(prepared),
            builder=_symbols(
                _NewCustomersBatchBuilder.batch,  # pyright: ignore[reportPrivateUsage]
                _NewCustomersRostersOnce,  # pyright: ignore[reportPrivateUsage]
                prepare_batch,
            ),
            fixture=(
                _REFERENCE_FIXTURE,
                ("window", _range(render.period)),
                ("prior_window", _range(render.prior_period)),
                ("marketplaces", str(len(labels.marketplaces))),
                ("sections", "buyers and channel published, every market pack-covered"),
                ("pack_coverage", "every day of every window at every pack version"),
                _COUNTING_ROSTERS,
            ),
        )

    preset = TemporalPresetId(variant)
    if is_calendar_preset(preset):
        return _largest(
            tuple(
                batch(NewCustomersRenderInput(preset, period.window, calendar_period=period))
                for period in _calendar_periods(variant)
            )
        )
    window, prior = representative_windows(preset_days(preset))
    return batch(NewCustomersRenderInput(preset, window, prior_period=prior))


@dataclass(frozen=True, slots=True)
class _AllServed:
    """Every requested contract published for the given periods on the base series."""

    periods: tuple[CalendarPeriod, ...]

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        ids = tuple(contract.contract_id for contract in request.contracts)
        return ServedPeriods(
            frozenset((item, period) for item in ids for period in self.periods),
            dict.fromkeys(ids, 16),
        )


def _reach_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Pack 5 cells for the period and prior, then pack 6 and 7 frequency, per marketplace."""
    catalog = load_catalog(root)
    contracts = _contracts(root)
    preset = TemporalPresetId(variant)
    insights = {contract.id: contract for _, contract in load_insight_contracts(root)}

    def batch(period: CalendarPeriod) -> RowCertificate:
        port = _CountingRosters()
        renderer = ReachArtifactRenderer(
            catalog=catalog,
            contracts=contracts,
            evidence=cast("Any", None),
            tooling=MeasurementTooling(
                queries=cast("Any", None),
                publications=cast("Any", None),
                periods=_AllServed((period, period.previous())),
                rosters=port,
            ),
            marketplaces=_marketplaces(root),
            insight_contracts=(
                insights[InsightSlotId.REACH_OVERLAP],
                insights[InsightSlotId.FULL_FUNNEL_ASSOCIATION],
                insights[InsightSlotId.FREQUENCY_SATURATION],
            ),
            clock=lambda: BAKEABILITY_REFERENCE_INSTANT,
            instance=AmcInstanceKind.STANDARD,
        )
        render = ReachRenderInput(preset, period)
        plan_batch = _ReachBatch()  # pyright: ignore[reportPrivateUsage]
        if preset in OVERLAP_DECLARED_PERIODS:
            renderer._plan_exposure(render, plan_batch, ({}, set()))  # pyright: ignore[reportPrivateUsage]
        if preset in FREQUENCY_DECLARED_PERIODS:
            for market in renderer.marketplaces:
                renderer._frequency(market, period, plan_batch)  # pyright: ignore[reportPrivateUsage]
        reach_prepared = prepare_batch(tuple(plan_batch.plans))
        plans = _distinct_plans(reach_prepared)
        phases = (*(1 for _ in port.read), len(plans))
        return RowCertificate(
            sum(phases),
            phases,
            f"{len(renderer.marketplaces)} marketplace(s), every pack published on the base "
            f"series; {len(port.read)} sequential roster read(s) (channel, campaign), then "
            "measurement",
            plans,
            tuple(port.read),
            **_width_terms(reach_prepared),
            builder=_symbols(
                ReachArtifactRenderer._plan_exposure,  # pyright: ignore[reportPrivateUsage]
                ReachArtifactRenderer._frequency,  # pyright: ignore[reportPrivateUsage]
                prepare_batch,
            ),
            fixture=(
                _REFERENCE_FIXTURE,
                ("window", _range(period.window)),
                ("prior_window", _range(period.previous().window)),
                ("marketplaces", str(len(renderer.marketplaces))),
                ("served", "every contract for the period and prior, base series version 16"),
                ("overlap_declared", str(preset in OVERLAP_DECLARED_PERIODS).lower()),
                ("frequency_declared", str(preset in FREQUENCY_DECLARED_PERIODS).lower()),
                _COUNTING_ROSTERS,
            ),
        )

    return _largest(tuple(batch(period) for period in _calendar_periods(variant)))


def _retention_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Both paths served for every governed marketplace at the reference vintage."""
    if variant != "latest_with_current_publication":
        raise ValueError(f"retention bakes only its vintage default, not {variant!r}.")
    contracts = _contracts(root)
    renderer = RetentionArtifactRenderer(
        catalog=load_catalog(root),
        contracts=contracts,
        evidence=cast("Any", None),
        tooling=MeasurementTooling(queries=cast("Any", None), publications=cast("Any", None)),
        marketplaces=_marketplaces(root),
        insight_contract=_retention_contract(InsightSlotId.RETENTION_SHIFT),  # pyright: ignore[reportPrivateUsage]
        clock=lambda: BAKEABILITY_REFERENCE_INSTANT,
        first_quarterly=lambda _market: None,
    )
    period = vintage_ending(_REFERENCE_DATES[0])
    requests: list[MeasurementToolRequest] = []
    for market in renderer.marketplaces:
        for contract_id, path, metrics in (
            (RETENTION_STANDARD_CONTRACT, STANDARD_PATH, STANDARD_METRICS),
            (RETENTION_ARP_CONTRACT, ARP_PATH, ARP_METRICS),
        ):
            grain = renderer._grain(contract_id, None)  # pyright: ignore[reportPrivateUsage]
            requests.extend(
                renderer._request(market, path, metric, (period, grain))  # pyright: ignore[reportPrivateUsage]
                for metric in metrics
            )
    return _single_phase(
        prepare_batch(tuple(requests)),
        f"M x ({len(STANDARD_METRICS)} standard + {len(ARP_METRICS)} ARP), "
        f"M = {len(renderer.marketplaces)}",
        builder=_symbols(
            RetentionArtifactRenderer._request,  # pyright: ignore[reportPrivateUsage]
            prepare_batch,
        ),
        fixture=(
            _REFERENCE_FIXTURE,
            ("vintage", _range(period.window)),
            ("marketplaces", str(len(renderer.marketplaces))),
            ("paths", "standard and ARP both served for every marketplace"),
        ),
    )


def audiences_phases(marketplaces: int) -> tuple[int, int, int]:
    """Return the Audiences phase ceilings: rosters and campaign reads; profile and history; R2b.

    Every campaign is read grouped, so no term is per campaign (design 01 §6). Phase 1 is each
    profile path's roster and the campaigns' count pass (three figures and R1); phase 2 the
    profile measurement and the history walk's older months (R2a); phase 3 its withheld part
    (R2b). The page pass re-reads the three figures, which result reuse answers, so it adds no
    distinct request.
    """
    profiles = len(PROFILE_CONTRACTS)
    return (
        marketplaces * (profiles * SEGMENT_ROSTER_PROFILE_READS + CAMPAIGN_STREAMED_READS),
        marketplaces * (profiles * (len(SEGMENT_METRICS) + len(TOTAL_METRICS)) + 1),
        marketplaces * (CAMPAIGN_HISTORY_READS - 1),
    )


@dataclass
class _CountingHistories:
    """A history port that records each request and grades every campaign a first month."""

    read: list[CampaignHistoriesRequest] = field(default_factory=list[CampaignHistoriesRequest])

    def campaign_histories(self, request: CampaignHistoriesRequest) -> CampaignHistories:
        self.read.append(request)
        return CampaignHistories(dict.fromkeys(request.campaigns, CampaignHistory.FIRST_MONTH))


def _audiences_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Return the rosters and campaign reads, the profile measurement and history, then R2b."""
    if variant != TemporalPresetId.LAST_COMPLETE_MONTH.value:
        raise ValueError(f"audiences bakes last_complete_month only, not {variant!r}.")
    markets = _marketplaces(root)
    contracts = _contracts(root)
    port = _CountingRosters()
    renderer = AudiencesArtifactRenderer(
        catalog=load_catalog(root),
        contracts=contracts,
        evidence=cast("Any", None),
        tooling=MeasurementTooling(
            queries=cast("Any", None),
            publications=cast("Any", None),
            query_batch_max_concurrency=CERTIFIED_QUERY_CAP,
        ),
        rosters=port,
        histories=_CountingHistories(),
        segment_labels=cast("Any", None),
        marketplaces=markets,
        insight_contract=_audiences_contract(InsightSlotId.SEGMENT_OVER_INDEX),  # pyright: ignore[reportPrivateUsage]
        clock=lambda: BAKEABILITY_REFERENCE_INSTANT,
        campaign_labels=lambda keys, _window: {key: key for key in keys},
    )
    period = max(_calendar_periods(variant), key=lambda item: item.window.days)
    prior = period_containing(TimeGrain.MONTH, period.start - timedelta(days=1))
    served = _AudiencesServed(  # pyright: ignore[reportPrivateUsage]
        dict.fromkeys(PROFILE_CONTRACTS.values(), frozenset({period.window})), {}
    )
    plan_batch = _AudiencesBatch()  # pyright: ignore[reportPrivateUsage]
    for market in markets:
        for scope in renderer._scopes(market, period, served):  # pyright: ignore[reportPrivateUsage]
            roster = renderer._roster(period, scope)  # pyright: ignore[reportPrivateUsage]
            renderer._plan(scope, (period, cast("Roster", roster)), plan_batch)  # pyright: ignore[reportPrivateUsage]
    campaign_batch = prepare_batch(
        tuple(
            request
            for market in markets
            for request in renderer.campaign_requests(market, period, prior)
        )
    )
    measured_batch = prepare_batch(tuple(plan_batch.plans))
    campaign, measured = _distinct_plans(campaign_batch), _distinct_plans(measured_batch)
    phases = audiences_phases(len(markets))
    histories = len(markets) * CAMPAIGN_HISTORY_READS
    observed = (len(port.read) + len(campaign), len(measured) + histories - phases[2], phases[2])
    if observed != phases:
        raise RuntimeError(f"the Audiences builders give {observed}, not the derived {phases}.")
    widths = _width_terms(campaign_batch, measured_batch)
    return RowCertificate(
        sum(phases),
        phases,
        f"24 per marketplace at any campaign count (M = {len(markets)}, P = "
        f"{len(PROFILE_CONTRACTS)}): P rosters + {CAMPAIGN_STREAMED_READS} grouped campaign "
        "reads, P x 5 measurement + the history walk, then its withheld part",
        (*campaign, *measured),
        widths=widths["widths"],
        kept=widths["kept"],
        # Each streamed read's queued and drawn groups beside the one it builds, then the count
        # pass's campaign tallies and its segment set: each bounded by V.
        retained=(
            *(BAKE_MAX_ROWS,) * ((RESIDENT_GROUPS - 1) * len(widths["kept"])),
            BAKE_MAX_ROWS,
            BAKE_MAX_ROWS,
        ),
        builder=_symbols(
            AudiencesArtifactRenderer.campaign_requests,
            AudiencesArtifactRenderer._scopes,  # pyright: ignore[reportPrivateUsage]
            AudiencesArtifactRenderer._roster,  # pyright: ignore[reportPrivateUsage]
            AudiencesArtifactRenderer._plan,  # pyright: ignore[reportPrivateUsage]
            prepare_batch,
        ),
        fixture=(
            _REFERENCE_FIXTURE,
            ("window", _range(period.window)),
            ("prior", _range(prior.window)),
            ("marketplaces", str(len(markets))),
            ("profile_scopes", str(len(PROFILE_CONTRACTS))),
            ("served", "every profile contract for the period; pack 8 and its prior month"),
            (
                "roster_reads",
                f"profile {SEGMENT_ROSTER_PROFILE_READS}, campaign history "
                f"{CAMPAIGN_HISTORY_READS}",
            ),
            _COUNTING_ROSTERS,
        ),
    )


_TASK_COUNTERS: dict[str, Callable[[pathlib.Path, str], RowCertificate]] = {
    "audiences": _audiences_row,
    "campaigns": _campaigns_row,
    "retail-health": _retail_row,
    "scorecard": _scorecard_row,
    "new-customers": _new_customers_row,
    "reach": _reach_row,
    "retention": _retention_row,
}
"""Each GRAPH-baked page's per-variant counter."""


@dataclass(frozen=True, slots=True)
class WriteSeconds:
    """One page's adopted write seconds, its evidence marker and its source."""

    seconds: float
    marker: str
    source: str


CAMPAIGNS_MEASURED_WRITE_SECONDS = 11.2
"""[measured] run 69e71e5d (2026-10-14): the largest per-task sum of `artifact.write` span
durations over the 3 written campaigns tasks (180 spans), rounded up to one decimal place."""


def page_write_seconds(page: str) -> WriteSeconds:
    """Adopt each page's write seconds for certification (plan A5.3 O2).

    Audiences' parts and Campaigns' segments grow with their campaigns, so each is certified at
    the whole write allowance and the stage's runtime write deadline is its one write limit
    (ADR-141, ADR-152). Every other page writes a fixed set of bodies; none has a measured
    `artifact.write` span (retail-health's tasks were all unchanged), so each adopts 16.6 s, the
    deployed upper bound over Campaigns' measured 11.2 s at 1,000 campaigns, as a declared
    conservative projection.
    """
    if page in {"audiences", "campaigns"}:
        return WriteSeconds(
            WRITE_ALLOWANCE_SECONDS,
            "derived",
            "WRITE_ALLOWANCE_SECONDS: bodies grow with campaigns; the stage's deadline limits them",
        )
    seconds = float(BAKE_TASKS["write_seconds"][page])
    return WriteSeconds(
        seconds,
        "projection",
        f"{seconds} s adopted as a conservative upper bound over the measured "
        f"{CAMPAIGNS_MEASURED_WRITE_SECONDS} s: no deployed write trace",
    )


HORIZON_SLACK_DAYS = 31
"""One month of fixed slack on every row's declared source horizon (plan A5.1 I9)."""


@dataclass(frozen=True, slots=True)
class HorizonDerivation:
    """One page's declared source horizon and its terms (plan A5.1 *Scope-read horizons*)."""

    window_days: int
    comparison_days: int
    lookback_days: int
    contributing: tuple[str, ...]
    """The contracts whose facts the page's certified plans and roster reads bind."""

    @property
    def days(self) -> int:
        """`window + comparison + lookback + slack`: the row's `source_horizon_days`."""
        return self.window_days + self.comparison_days + self.lookback_days + HORIZON_SLACK_DAYS


def _preset_window_days(preset: TemporalPresetId) -> int:
    if is_calendar_preset(preset):
        return _CALENDAR_MAX_DAYS[preset]
    return preset_days(preset)


_VINTAGE_DAYS = max(
    vintage_ending(date(BAKEABILITY_REFERENCE_YEAR + offset // 12, offset % 12 + 1, 1)).window.days
    for offset in range(48)
)
"""The longest 13-month vintage window over four reference years (a leap February included)."""


def _cohort_follow_up_days(contracts: tuple[DataContract, ...]) -> int:
    """Return the longest cohort follow-up span a contributing contract declares, any series."""
    spans = [0]
    for contract in contracts:
        cells = contract.expected_cells
        if cells is None:
            continue
        for sets in (cells.sets, *(found for _name, found in cells.variants)):
            spans.extend(
                item.cohorts.follow_ups * longest_grain_days(TimeGrain(item.cohorts.grain.value))
                for item in sets
                if item.cohorts is not None
            )
    return max(spans)


def page_source_horizons(
    root: pathlib.Path, figures: tuple[D2RowFigure, ...]
) -> dict[str, HorizonDerivation]:
    """Derive each roster page's declared source horizon from its own policy (A5.1 I9).

    `window` is the longest candidate preset (or the vintage span), `comparison` the window
    again for an equal prior or the longest prior instance for a calendar prior, and `lookback`
    the largest `restatement_window_days + effective_cadence_lag_days` over the contracts the
    page's certified batches bind, plus Audiences' observation months and Retention's cohort
    follow-up span. Every variant of a page shares the page's horizon.
    """
    schemas = {page.page_id: page for page in load_v2_page_schemas(root).pages}
    contracts = _contracts(root)
    lags = {pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)}
    facts_by_page: dict[str, set[str]] = {}
    for item in figures:
        page = item.key.split("#", 1)[0]
        facts = facts_by_page.setdefault(page, set())
        for plan in item.certificate.plans:
            facts.add(plan.fact)
            if plan.join is not None:
                facts.add(plan.join.fact)
        if item.certificate.rosters:
            facts.add(SPEND_FACT)
    derived: dict[str, HorizonDerivation] = {}
    for page, facts in facts_by_page.items():
        policy = schemas[page].time_policy
        contributing = tuple(
            sorted(
                (contract for contract in contracts if contract.target_table in facts),
                key=lambda contract: contract.contract_id,
            )
        )
        lookback = max(
            contract.restatement_window_days
            + lags.get(contract.contract_id, DEFAULT_CADENCE_LAG_DAYS)
            for contract in contributing
        )
        if page == "audiences":
            lookback += (
                AUDIENCES_OBSERVATION_MONTHS
                * _CALENDAR_MAX_DAYS[TemporalPresetId.LAST_COMPLETE_MONTH]
            )
        if isinstance(policy, PresentationTemporalPolicy):
            window = max(_preset_window_days(preset) for preset in policy.candidate_presets)
            if policy.comparison_policy is TemporalComparisonPolicy.NONE:
                comparison = 0
            elif policy.comparison_policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD:
                # A calendar prior's longest instance is its preset's own longest instance.
                comparison = max(
                    _preset_window_days(preset)
                    for preset in policy.candidate_presets
                    if is_calendar_preset(preset)
                )
            else:
                comparison = window
        else:
            window, comparison = _VINTAGE_DAYS, 0
            lookback += _cohort_follow_up_days(contributing)
        derived[page] = HorizonDerivation(
            window, comparison, lookback, tuple(item.contract_id for item in contributing)
        )
    return derived


@dataclass(frozen=True, slots=True)
class D2RowFigure:
    """One D2 roster row's certified figures."""

    key: str
    certificate: RowCertificate
    write: WriteSeconds
    seconds: float
    budget_seconds: int
    budget_basis: str

    @property
    def certified(self) -> bool:
        """Whether the row's certified seconds fit its budget and the Lambda timeout."""
        return self.seconds <= self.budget_seconds <= BAKER_LAMBDA_TIMEOUT_SECONDS


def _budget(key: str, seconds: float) -> tuple[int, str]:
    # The contract's budget: deployed for the GRAPH rows, not yet for the 13 D2 rows.
    declared = BAKE_TASKS["budget_seconds"].get(key)
    if declared is not None:
        return int(declared), "contract budget, re-certified"
    return 30 * math.ceil(seconds / 30), "certified seconds rounded up to a multiple of 30"


def write_allowance_findings(
    write_seconds: Mapping[str, object] | None = None,
) -> tuple[BakeabilityFinding, ...]:
    """Refuse a roster row whose write term exceeds `WRITE_ALLOWANCE_SECONDS` (ADR-144)."""
    terms = (
        cast("Mapping[str, object]", BAKE_TASKS["write_seconds"])
        if write_seconds is None
        else write_seconds
    )
    findings: list[BakeabilityFinding] = []
    for row in BAKE_TASKS["roster"]:
        page = str(row["page"])
        seconds = float(cast("float", terms[page]))
        if seconds > WRITE_ALLOWANCE_SECONDS:
            findings.append(
                BakeabilityFinding(
                    page,
                    "write-allowance",
                    (
                        f"{page}#{row['variant']}: write_seconds {seconds:g} s exceeds "
                        f"WRITE_ALLOWANCE_SECONDS={WRITE_ALLOWANCE_SECONDS:g} s."
                    ),
                )
            )
    return tuple(findings)


def source_horizon_findings(root: pathlib.Path) -> tuple[BakeabilityFinding, ...]:
    """Refuse a contract `source_horizon_days` its page's policy doesn't derive (A5.1 I9)."""
    derived = page_source_horizons(root, certify_d2_roster(root))
    declared = BAKE_TASKS.get("source_horizon_days", {})
    findings: list[BakeabilityFinding] = []
    for row in BAKE_TASKS["roster"]:
        page, key = str(row["page"]), f"{row['page']}#{row['variant']}"
        value = declared.get(key)
        if value != derived[page].days:
            findings.append(
                BakeabilityFinding(
                    page,
                    "source-horizon",
                    f"{key}: the contract declares {value} source horizon days; the page's "
                    f"policy derives {derived[page].days}.",
                )
            )
    return tuple(findings)


def certify_d2_roster(root: pathlib.Path) -> tuple[D2RowFigure, ...]:
    """Certify every D2 roster row on its own counter at Q10 (spike S0)."""
    figures: list[D2RowFigure] = []
    for page, variant in d2_roster_keys(root):
        write = page_write_seconds(page)
        certificate = _TASK_COUNTERS[page](root, variant)
        seconds = phased_task_seconds(certificate.phases, CERTIFIED_QUERY_CAP, write.seconds)
        key = f"{page}#{variant}"
        budget, basis = _budget(key, seconds)
        figures.append(D2RowFigure(key, certificate, write, seconds, budget, basis))
    return tuple(figures)


def d2_contract_findings(root: pathlib.Path) -> tuple[BakeabilityFinding, ...]:
    """Refuse a contract D2 row the counter doesn't reproduce (A5.3).

    A row whose certified count, budget or write seconds differs, an uncertified row, and a
    contract roster that isn't the declared D2 roster are each a finding.
    """
    figures = certify_d2_roster(root)
    findings: list[BakeabilityFinding] = []
    contract = [f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"]]
    if contract != [item.key for item in figures]:
        findings.append(
            BakeabilityFinding(
                "roster", "d2-roster", "the contract roster is not the declared D2 roster."
            )
        )
    counts = BAKE_TASKS.get("certified_plan_counts", {})
    for item in figures:
        page = item.key.split("#", 1)[0]
        declared = (
            counts.get(item.key),
            BAKE_TASKS["budget_seconds"].get(item.key),
            BAKE_TASKS["write_seconds"].get(page),
        )
        derived = (item.certificate.count, item.budget_seconds, item.write.seconds)
        if declared != derived or not item.certified:
            findings.append(
                BakeabilityFinding(
                    page,
                    "certified-plan-count",
                    f"{item.key}: the contract declares {declared}; the counter certifies "
                    f"{derived} at {item.seconds} s (certified: {item.certified}).",
                )
            )
    return tuple(findings)


def root_bound_seconds(figures: tuple[D2RowFigure, ...]) -> int:
    """Return `sum(budget_seconds) + 2 x control_claim_seconds`, checked against 7,200 s."""
    return sum(item.budget_seconds for item in figures) + 2 * int(
        BAKE_TASKS["control_claim_seconds"]
    )


def d2_lane_plan(figures: tuple[D2RowFigure, ...]) -> LanePlan:
    """Return the fan-out's two-lane partition over each row's certified-count load (A5.3)."""
    return two_lane_partition(
        tuple(
            BakeTaskLoad(
                item.key,
                task_seconds(item.certificate.count, CERTIFIED_QUERY_CAP, item.write.seconds),
            )
            for item in figures
        )
    )


def d2_stop_findings(figures: tuple[D2RowFigure, ...]) -> tuple[str, ...]:
    """Every A5.6 stop rule spike S0's figures fire; empty means none."""
    findings = [
        f"{item.key}: {item.seconds} s over its {item.budget_seconds} s budget or 900 s"
        for item in figures
        if not item.certified
    ]
    bound = root_bound_seconds(figures)
    if bound > WINNER_POLICY_SECONDS:
        findings.append(f"root bound {bound} s over {WINNER_POLICY_SECONDS} s")
    return tuple(findings)


WINNER_POLICY_SECONDS = 7_200
"""`winner_policy_seconds`: one winner's root bound (`bake_winner_over_root_bound`)."""


def d2_main() -> int:
    """Print the D2 roster's S0 figures; a fired stop rule exits 1."""
    root = pathlib.Path.cwd()
    figures = certify_d2_roster(root)
    derived = page_source_horizons(root, figures)
    print("D2 ROSTER CERTIFICATION (spike S0, plan A5.3)")
    for item in figures:
        print(
            f"  {item.key}: count {item.certificate.count}, phases "
            f"{list(item.certificate.phases)}, write {item.write.seconds} s "
            f"[{item.write.marker}], {item.seconds} s <= {item.budget_seconds} s "
            f"({item.budget_basis}), source horizon "
            f"{derived[item.key.split('#', 1)[0]].days} d"
        )
    lanes = d2_lane_plan(figures)
    print(f"  N = {len(figures)}; sum budget_seconds {sum(i.budget_seconds for i in figures)} s")
    print(f"  root bound {root_bound_seconds(figures)} s <= {WINNER_POLICY_SECONDS} s")
    print(f"  lanes {list(lanes.loads)}; makespan {lanes.makespan}")
    stops = d2_stop_findings(figures)
    for stop in stops:
        print(f"  [stop] {stop}")
    return 1 if stops else 0


if __name__ == "__main__":
    sys.exit(d2_main() if sys.argv[1:] == ["--d2-roster"] else main())
