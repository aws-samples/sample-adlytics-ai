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

import math
import pathlib
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
    CampaignPortfolioMeasurementInput,
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
    AUDIENCES_ENVELOPE_MAX_BYTES,
    AUDIENCES_ROW_MAX_BYTES,
    CAMPAIGN_METRICS,
    PROFILE_CONTRACTS,
    SEGMENT_METRICS,
    TOTAL_METRICS,
    AudiencesArtifactRenderer,
    audiences_bake_phase_plan_counts,
    audiences_bake_plan_count,
    audiences_marketplace_count,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    CAMPAIGN_GRAINS as AUDIENCES_CAMPAIGN_GRAINS,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    DELIVERY_CONTRACT as AUDIENCES_DELIVERY_CONTRACT,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    OBSERVATION_MONTHS as AUDIENCES_OBSERVATION_MONTHS,
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _Batch as _AudiencesBatch,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _campaign_base as _audiences_campaign_base,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _contract as _audiences_contract,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    _Served as _AudiencesServed,  # pyright: ignore[reportPrivateUsage]
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    CAMPAIGN_PRESET_POPULATION_BOUND,
    _representative_selections,  # pyright: ignore[reportPrivateUsage]
    campaign_portfolio_bake_plan_count,
    representative_windows,
)
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
    retail_health_bake_plan_count,
    retail_health_measurement_batch,
    retail_product_ad_batch,
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
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import MAX_PRESET_SNAPSHOT_PARTITIONS
from adapters.outbound.spend_roster import SPEND_FACT
from adapters.producer.packs import load_query_packs
from domain.measurement.pack_coverage import PackCoverageVerdict, PackDayVersions
from domain.measurement.rosters import Roster, RosterKind
from domain.operations.bake_scheduling import (
    BakeTaskLoad,
    LanePlan,
    task_seconds,
    two_lane_partition,
)
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
)
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    is_calendar_preset,
)
from domain.presentation.preset_artifacts import MAX_PRESET_ARTIFACT_BYTES
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
    vintage_ending,
)
from domain.semantics.time import TimeGrain
from domain.shared_kernel.values import DateRange
from ports.outbound.pack_coverage import PackCoverageReadRequest
from ports.outbound.rosters import RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping

    from domain.data_contracts.contract import DataContract
    from domain.measurement.planning import QueryPlan
    from domain.presentation.page_schemas import PresentationPageSchemaSet

__all__ = [
    "AUDIENCES_NON_QUERY_MARGIN_MS",
    "AUDIENCES_RENDER_TAIL_MS",
    "BAKER_LAMBDA_TIMEOUT_SECONDS",
    "BAKER_MEMORY_BYTES",
    "BAKER_TIMEOUT_SECONDS",
    "BAKE_RESIDENT_SHARE",
    "CERTIFIED_QUERY_CAP",
    "MAX_PRESET_SNAPSHOT_PARTITIONS",
    "PER_OBJECT_WRITE_MS",
    "PER_QUERY_SECONDS",
    "ArtifactServedPage",
    "BakeabilityFinding",
    "RosterTaskCertificate",
    "artifact_served_pages",
    "audiences_bake_duration_ms",
    "audiences_page_plan_count",
    "audiences_row_budget",
    "audiences_runtime_campaign_bound",
    "audiences_storage_estimate",
    "audiences_timeout_headroom_campaigns",
    "campaign_bake_object_count",
    "certify_roster_tasks",
    "evaluate_bakeability",
    "main",
    "roster_plan_counts",
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
# The budget this gate proves one variant's bake within. S6 §2.8 raised the Lambda to 900 s
# because one graph bake node iterates every registered intent in one invocation; this gate
# still proves one variant per invocation, so each variant keeps its 300 s budget rather than
# taking the whole invocation.
BAKER_TIMEOUT_SECONDS = 300

# The Baker's in-process Athena cap Q every bake budget is certified at (plan S1); the deployed
# `athenaStartBudget.bakerStartCap` may not go below it, so this gate's waves are an upper bound.
CERTIFIED_QUERY_CAP = int(BAKE_TASKS["certified_query_cap"])
# The Audiences barrier bound as feat/ui-ia-impl derived it, at eight queries per wave: fewer
# than the certified cap, so its waves over-count. Spike S0 (plan A5.3) recertifies it at Q10.
_AUDIENCES_UNCERTIFIED_QUERY_CAP = 8
CAMPAIGN_MAX_SEGMENTS = math.ceil(CAMPAIGN_PRESET_POPULATION_BOUND / CAMPAIGN_PORTFOLIO_MAX_ROWS)
CAMPAIGN_ORDERING_COUNT = len(tuple(CampaignPortfolioSortField)) * 2
CAMPAIGN_GOVERNED_CHANNEL_COUNT = 5
"""The governed channels in contracts/shared/media-taxonomy.yaml; a test holds them equal."""
CAMPAIGN_MAX_FILTER_SIBLINGS = 2 + CAMPAIGN_GOVERNED_CHANNEL_COUNT
"""All channels, Sponsored ads and one per channel: up to seven siblings per preset (ADR-127)."""
# Siblings of one preset: All channels and Sponsored ads may each hold the whole population, and
# the one-channel siblings partition it, so together they hold at most one population plus one
# partly filled segment per channel.
CAMPAIGN_MAX_FILTER_SEGMENTS = 3 * CAMPAIGN_MAX_SEGMENTS + CAMPAIGN_GOVERNED_CHANNEL_COUNT


def campaign_bake_object_count(filter_populations: tuple[int, ...]) -> int:
    """Count the immutable objects one Campaigns variant writes for its filters' populations.

    Each filter writes every ordering's segments as a public body and a private custody; its
    canonical first segment is the filter's own parent (the pointer's body for All channels), so
    a filter of S segments writes 2 x orderings x S objects. The local estate acceptance holds
    this count equal to a real seven-sibling bake's.
    """
    return sum(
        2 * CAMPAIGN_ORDERING_COUNT * max(1, math.ceil(count / CAMPAIGN_PORTFOLIO_MAX_ROWS))
        for count in filter_populations
    )


MAX_BAKE_BODY_COUNT = 2 * CAMPAIGN_ORDERING_COUNT * CAMPAIGN_MAX_FILTER_SEGMENTS
"""The most objects a Campaigns variant writes at the estate bound: the estimate, not the bound."""
CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE = 245_000
MAX_BAKE_TOTAL_BYTES = MAX_BAKE_BODY_COUNT * MAX_PRESET_ARTIFACT_BYTES
RETAIL_HEALTH_BAKE_BODY_COUNT = 2 + 2 * RETAIL_HEALTH_MAX_ROWS

# The envelope a bake's writes are checked against, independent of any page's estimate.
# Must match the Baker's memorySize in infra/lib/stacks/publisher-stack.ts; a test reads it.
BAKER_MEMORY_BYTES = 1024 * 1024 * 1024
# A publication request holds every body it writes in memory at once, beside the parsed batch
# and the rendered artifacts, so its encoded bytes may use at most this share of the baker.
BAKE_RESIDENT_SHARE = 0.5
# Budget for one sequential immutable write (an S3 PutObject of at most one capped body) from
# the baker. A budget, not a measurement: the deployed bake's duration is its live check.
PER_OBJECT_WRITE_MS = 100
AUDIENCES_RENDER_TAIL_MS = PER_OBJECT_WRITE_MS
"""A separate render/finalization allowance derived from the governed write allowance."""
AUDIENCES_NON_QUERY_MARGIN_MS = PER_OBJECT_WRITE_MS
"""Positive headroom after all modeled Audiences phases, render/finalization and write."""


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


def audiences_bake_duration_ms(marketplaces: int, campaigns: int) -> int:
    """Conservative Audiences makespan across its sequential query barriers and tail work."""
    phase_waves = sum(
        math.ceil(plans / _AUDIENCES_UNCERTIFIED_QUERY_CAP)
        for plans in audiences_bake_phase_plan_counts(marketplaces, campaigns)
    )
    return (
        phase_waves * PER_QUERY_SECONDS * 1_000
        + PER_OBJECT_WRITE_MS
        + AUDIENCES_RENDER_TAIL_MS
        + AUDIENCES_NON_QUERY_MARGIN_MS
    )


def audiences_timeout_headroom_campaigns(marketplaces: int) -> int:
    """Estimate the campaigns that fit the 900 s Baker timeout; NOT the page limit.

    The page limit is `audiences_runtime_campaign_bound` (TD-D2-AUDIENCES-CAMPAIGN-LIMIT).
    """
    if marketplaces != 1:
        raise ValueError("Audiences bakeability requires exactly one governed marketplace.")
    timeout_ms = BAKER_TIMEOUT_SECONDS * 1_000
    return max(
        campaigns
        for campaigns in range(1, audiences_row_budget(marketplaces) + 1)
        if audiences_bake_duration_ms(marketplaces, campaigns) < timeout_ms
    )


def audiences_row_budget(marketplaces: int) -> int:
    """Derive how many profile and campaign rows one Audiences body holds under the cap."""
    room = MAX_PRESET_ARTIFACT_BYTES - marketplaces * AUDIENCES_ENVELOPE_MAX_BYTES
    return room // AUDIENCES_ROW_MAX_BYTES


def audiences_storage_estimate(marketplaces: int, rows: int) -> BakeStorageEstimate:
    """One body: each marketplace's envelope and `rows` rows at their ceiling."""
    body = marketplaces * AUDIENCES_ENVELOPE_MAX_BYTES + rows * AUDIENCES_ROW_MAX_BYTES
    return BakeStorageEstimate(1, body, body)


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
            # required equal prior — of a daily-grain pack in one exact read (ADR-070,
            # 2026-09-16 amendment), so 2 x preset-days must fit one transaction.
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
        resident = int(BAKER_MEMORY_BYTES * BAKE_RESIDENT_SHARE)
        if estimate.total_bytes > resident:
            findings.append(
                BakeabilityFinding(
                    page.page_id,
                    "bake-memory-bound",
                    (
                        f"{estimate.body_count} bodies of {estimate.total_bytes} bytes exceed "
                        f"the {resident}-byte share of the {BAKER_MEMORY_BYTES}-byte baker."
                    ),
                )
            )
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


def audiences_page_plan_count(root: pathlib.Path) -> int:
    """Return the page-level gate's Audiences count at the one campaign authority.

    The runtime bound (TD-D2-AUDIENCES-CAMPAIGN-LIMIT), never the timeout estimator.
    """
    markets = audiences_marketplace_count(root)
    return audiences_bake_plan_count(markets, audiences_runtime_campaign_bound(markets))


def main() -> int:
    """Report every deterministic bakeability finding for the declared V2 pages."""
    root = pathlib.Path.cwd()
    print("PRESET ARTIFACT BAKEABILITY CHECK")
    print("=" * 70)
    pages = artifact_served_pages(load_v2_page_schemas(root))
    task_counts = roster_plan_counts(root)
    certificates, roster_findings = certify_roster_tasks(task_counts)
    audiences_markets = audiences_marketplace_count(root)
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
        "audiences": audiences_page_plan_count(root),
    }
    retention_body = retention_bake_body_bytes(root)
    storage = {
        "campaigns": BakeStorageEstimate(
            MAX_BAKE_BODY_COUNT,
            CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE,
            MAX_BAKE_BODY_COUNT * CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE,
        ),
        # The parent body and custody, and one child body and custody per displayed row.
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
        # One body: every marketplace's envelope and the row budget at the row ceiling.
        "audiences": audiences_storage_estimate(
            audiences_markets, audiences_row_budget(audiences_markets)
        ),
    }
    findings = (
        *evaluate_bakeability(pages, plan_counts, storage),
        *roster_findings,
        *source_horizon_findings(root),
        *d2_contract_findings(root),
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
    # One live variant per task (streamed) is the shape the memory bound certifies; the pooled
    # shape (every variant of a page resident at once) is reported, never certified.
    for page_id, estimate in storage.items():
        variants = sum(1 for key in task_counts if key.split("#", 1)[0] == page_id)
        print(
            f"  {page_id}: streamed {estimate.total_bytes} B resident; pooled "
            f"{variants * estimate.total_bytes} B over {variants} variants (not certified)"
        )
    print(
        f"  campaigns: up to {CAMPAIGN_MAX_FILTER_SIBLINGS} filter siblings per preset, "
        f"{MAX_BAKE_BODY_COUNT} immutable objects per variant at the estate bound, "
        f"{MAX_BAKE_BODY_COUNT * PER_OBJECT_WRITE_MS // 1_000} s of writes budgeted"
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
AUDIENCES_PHASE_BUDGET_SECONDS = 300
"""The fixed Audiences task budget its phased seconds must fit (UI `BAKER_TIMEOUT_SECONDS`)."""
SEGMENT_ROSTER_PROFILE_READS = 1
"""Queries one profile-path segment roster issues at most (`SegmentRosterReader` census)."""
SEGMENT_ROSTER_CAMPAIGN_READS = 3
"""Sequential queries one campaign segment roster issues at most: the prior month, then the
older-history series split once at a withheld month (`SegmentRosterReader` census)."""
BAKER_WORKER_WIDTH = CERTIFIED_QUERY_CAP
"""The Audiences roster fan-out width: `tooling.query_batch_max_concurrency` = `bakerStartCap`."""
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


def _single_phase(  # noqa: PLR0913 -- the row's plans, derivation and its O2 protocol
    plans: tuple[QueryPlan, ...],
    derivation: str,
    *,
    builder: tuple[str, ...],
    fixture: tuple[tuple[str, str], ...],
) -> RowCertificate:
    return RowCertificate(
        len(plans), (len(plans),), derivation, plans, builder=builder, fixture=fixture
    )


def _distinct_plans(prepared: PreparedMeasurementBatch) -> tuple[QueryPlan, ...]:
    return tuple(plan.logical for plan in prepared.plans)


def _symbols(*builders: Callable[..., object]) -> tuple[str, ...]:
    """Name each builder a counter invoked by its defining module and qualified name."""
    return tuple(f"{item.__module__}.{item.__qualname__}" for item in builders)


def _range(window: DateRange | None) -> str:
    return "none" if window is None else f"{window.start.isoformat()}..{window.end.isoformat()}"


_REFERENCE_FIXTURE = ("reference_instant", BAKEABILITY_REFERENCE_INSTANT.isoformat())
_COUNTING_ROSTERS = ("roster_port", "counting port answering an empty roster")


def _campaigns_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Return the real portfolio batch at the estate bound, through a counting roster port."""
    period, prior = representative_windows(preset_days(TemporalPresetId(variant)))
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
    selections = _representative_selections(bridge)  # pyright: ignore[reportPrivateUsage]
    batch = service.portfolio_batch(
        CampaignPortfolioMeasurementInput(
            selections=selections,
            period=period,
            prior_period=prior,
            sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        )
    )
    plans = _distinct_plans(batch.prepared)
    phases = (len(rosters.read), len(plans)) if rosters.read else (len(plans),)
    return RowCertificate(
        sum(phases),
        phases,
        f"portfolio batch at the {CAMPAIGN_PRESET_POPULATION_BOUND}-campaign estate bound over "
        f"{variant} and its equal prior; {len(rosters.read)} spend-roster reads",
        plans,
        tuple(rosters.read),
        builder=_symbols(CampaignArtifactMeasurementService.portfolio_batch),
        fixture=(
            _REFERENCE_FIXTURE,
            ("window", _range(period)),
            ("prior_window", _range(prior)),
            ("campaign_selections", str(len(selections))),
            ("selection_marketplaces", str(len({item.marketplace for item in selections}))),
            ("sort", "spend desc"),
            _COUNTING_ROSTERS,
        ),
    )


def _retail_row(root: pathlib.Path, variant: str) -> RowCertificate:
    """Return the main batch, then the product-ad batch the render runs only after a main answer."""
    period, prior = representative_windows(preset_days(TemporalPresetId(variant)))
    request = RetailHealthMeasurementBatchRequest(
        catalog=load_catalog(root),
        contracts=_contracts(root),
        period=period,
        prior_period=prior,
        computed_at=BAKEABILITY_REFERENCE_INSTANT,
    )
    main_plans = _distinct_plans(retail_health_measurement_batch(request).prepared)
    ad_plans = _distinct_plans(retail_product_ad_batch(request).prepared)
    return RowCertificate(
        len(main_plans) + len(ad_plans),
        (len(main_plans), len(ad_plans)),
        f"main batch {len(main_plans)} over {variant} and its prior with one spark query per "
        f"day, then the product-ad batch {len(ad_plans)} (sequential: reached after the main "
        "answer)",
        (*main_plans, *ad_plans),
        builder=_symbols(retail_health_measurement_batch, retail_product_ad_batch),
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
            _distinct_plans(prepared),
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
        catalog, _contracts(root), lambda: BAKEABILITY_REFERENCE_INSTANT
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
        plans = _distinct_plans(prepare_batch(tuple(plan_batch.plans)))
        phases = (*(1 for _ in port.read), len(plans))
        return RowCertificate(
            sum(phases),
            phases,
            f"{len(renderer.marketplaces)} marketplace(s), every pack published on the base "
            f"series; {len(port.read)} sequential roster read(s) (channel, campaign), then "
            "measurement",
            plans,
            tuple(port.read),
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
    plans = _distinct_plans(prepare_batch(tuple(requests)))
    return _single_phase(
        plans,
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


def audiences_phases(marketplaces: int, campaigns: int) -> tuple[int, int, int, int, int]:
    """Return the Audiences phase ceilings: list; roster rounds 1-3; measurement (plan A5.3).

    A profile scope reads its roster at most once and a campaign scope at most three times in
    sequence, so round 1 is every scope and rounds 2 and 3 are the campaign scopes again.
    """
    profiles = len(PROFILE_CONTRACTS)
    return (
        marketplaces,
        marketplaces * (profiles * SEGMENT_ROSTER_PROFILE_READS + campaigns),
        marketplaces * campaigns * (SEGMENT_ROSTER_CAMPAIGN_READS - 2),
        marketplaces * campaigns * (SEGMENT_ROSTER_CAMPAIGN_READS - 2),
        marketplaces
        * (
            profiles * (len(SEGMENT_METRICS) + len(TOTAL_METRICS))
            + campaigns * len(CAMPAIGN_METRICS)
        ),
    )


def audiences_certified_campaign_bound(marketplaces: int, write_seconds: float) -> int:
    """Return the largest C within the row budget whose phased seconds fit the fixed 300 s budget.

    Refuses a bound whose round-1 roster fan-out exceeds the Baker worker width, because the
    rounds would then queue behind each other. Zero means no campaign fits (a stop rule).
    """
    fitting = [
        campaigns
        for campaigns in range(1, audiences_row_budget(marketplaces) + 1)
        if phased_task_seconds(
            audiences_phases(marketplaces, campaigns), CERTIFIED_QUERY_CAP, write_seconds
        )
        <= AUDIENCES_PHASE_BUDGET_SECONDS
        and marketplaces * (len(PROFILE_CONTRACTS) + campaigns) <= BAKER_WORKER_WIDTH
    ]
    return max(fitting, default=0)


def audiences_runtime_campaign_bound(marketplaces: int) -> int:
    """Return the one Audiences campaign bound: the runtime composes with the certified bound.

    Known limit (dev-only stopgap, owner-approved 2026-10-17): the certifier fits at most
    this many DSP campaigns in the 300 s phase budget (7 today), so the page shows no more.
    Known limit TD-D2-AUDIENCES-CAMPAIGN-LIMIT (validation/README.md), tracked by the
    outcome-first redesign (.agents/tasks/outcome-first-limits/).
    """
    return audiences_certified_campaign_bound(marketplaces, page_write_seconds("audiences").seconds)


def _audiences_row(root: pathlib.Path, variant: str, write_seconds: float) -> RowCertificate:
    """Return the campaign list, each scope's roster rounds, then the measurement at the bound C."""
    if variant != TemporalPresetId.LAST_COMPLETE_MONTH.value:
        raise ValueError(f"audiences bakes last_complete_month only, not {variant!r}.")
    markets = _marketplaces(root)
    bound = audiences_certified_campaign_bound(len(markets), write_seconds)
    if bound < 1:
        raise ValueError("no Audiences campaign bound fits the phase budget (A5.6).")
    contracts = _contracts(root)
    port = _CountingRosters()
    renderer = AudiencesArtifactRenderer(
        catalog=load_catalog(root),
        contracts=contracts,
        evidence=cast("Any", None),
        tooling=MeasurementTooling(queries=cast("Any", None), publications=cast("Any", None)),
        rosters=port,
        segment_labels=cast("Any", None),
        marketplaces=markets,
        insight_contract=_audiences_contract(InsightSlotId.SEGMENT_OVER_INDEX),  # pyright: ignore[reportPrivateUsage]
        clock=lambda: BAKEABILITY_REFERENCE_INSTANT,
        campaign_labels=lambda keys, _window: {key: key for key in keys},
        campaign_bound=bound,
    )
    period = max(_calendar_periods(variant), key=lambda item: item.window.days)
    platform = next(
        item.platform_key for item in contracts if item.contract_id == AUDIENCES_DELIVERY_CONTRACT
    )
    listing = tuple(
        renderer._request(  # pyright: ignore[reportPrivateUsage]
            market,
            "segment_impressions",
            AUDIENCES_CAMPAIGN_GRAINS,
            (period, _audiences_campaign_base()),  # pyright: ignore[reportPrivateUsage]
            Roster(RosterKind.SEGMENT, period.window, market, platform, ()),
        )
        for market in markets
    )
    served = _AudiencesServed(  # pyright: ignore[reportPrivateUsage]
        dict.fromkeys(
            (*PROFILE_CONTRACTS.values(), AUDIENCES_DELIVERY_CONTRACT), frozenset({period.window})
        ),
        {},
    )
    shown = tuple(f"campaign:bakeability-{index:03d}" for index in range(bound))
    plan_batch = _AudiencesBatch()  # pyright: ignore[reportPrivateUsage]
    scopes = tuple(
        scope
        for market in markets
        for scope in renderer._scopes(market, period, (served, shown))  # pyright: ignore[reportPrivateUsage]
    )
    for scope in scopes:
        roster = renderer._roster(period, scope)  # pyright: ignore[reportPrivateUsage]
        renderer._plan(scope, (period, roster), plan_batch)  # pyright: ignore[reportPrivateUsage]
    measured = _distinct_plans(prepare_batch(tuple(plan_batch.plans)))
    listed = _distinct_plans(prepare_batch(listing))
    phases = audiences_phases(len(markets), bound)
    observed = (len(listed), len(port.read), phases[2], phases[3], len(measured))
    if observed != phases:
        raise RuntimeError(f"the Audiences builders give {observed}, not the derived {phases}.")
    return RowCertificate(
        sum(phases),
        phases,
        f"19 + 6C at C = {bound} (M = {len(markets)}, P = {len(PROFILE_CONTRACTS)}): list, "
        "roster rounds of P + C, C and C, then P x 5 + 3C measurement",
        (*listed, *measured),
        builder=_symbols(
            AudiencesArtifactRenderer._request,  # pyright: ignore[reportPrivateUsage]
            AudiencesArtifactRenderer._scopes,  # pyright: ignore[reportPrivateUsage]
            AudiencesArtifactRenderer._roster,  # pyright: ignore[reportPrivateUsage]
            AudiencesArtifactRenderer._plan,  # pyright: ignore[reportPrivateUsage]
            prepare_batch,
        ),
        fixture=(
            _REFERENCE_FIXTURE,
            ("window", _range(period.window)),
            ("marketplaces", str(len(markets))),
            ("profile_scopes", str(len(PROFILE_CONTRACTS))),
            ("campaign_bound", str(bound)),
            ("write_seconds", str(write_seconds)),
            ("served", "every profile and delivery contract for the period"),
            (
                "roster_reads_per_scope",
                f"profile {SEGMENT_ROSTER_PROFILE_READS}, campaign {SEGMENT_ROSTER_CAMPAIGN_READS}",
            ),
            _COUNTING_ROSTERS,
        ),
    )


_TASK_COUNTERS: dict[str, Callable[[pathlib.Path, str], RowCertificate]] = {
    "campaigns": _campaigns_row,
    "retail-health": _retail_row,
    "scorecard": _scorecard_row,
    "new-customers": _new_customers_row,
    "reach": _reach_row,
    "retention": _retention_row,
}
"""Each GRAPH-baked page's per-variant counter; Audiences also takes its write seconds."""


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

    Campaigns keeps its deployed 16.6, an upper bound over its measured 11.2. An adopted bound
    is a projection under rev5 O2, so campaigns is marked one too, with both figures in its
    source. Every other page has no measured `artifact.write` span (retail-health's tasks were
    all unchanged), so it adopts campaigns' 16.6 as a declared conservative upper bound.
    """
    if page == "campaigns":
        seconds = float(BAKE_TASKS["write_seconds"]["campaigns"])
        return WriteSeconds(
            seconds,
            "projection",
            f"adopted (conservative) {seconds} s; measured {CAMPAIGNS_MEASURED_WRITE_SECONDS} s",
        )
    return WriteSeconds(
        float(BAKE_TASKS["write_seconds"]["campaigns"]),
        "projection",
        "campaigns' 16.6 adopted as a conservative upper bound: no deployed write trace",
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


def _budget(key: str, page: str, seconds: float) -> tuple[int, str]:
    # The contract's budget: deployed for the GRAPH rows, not yet for the 13 D2 rows.
    declared = BAKE_TASKS["budget_seconds"].get(key)
    if declared is not None:
        return int(declared), "contract budget, re-certified"
    if page == "audiences":
        return AUDIENCES_PHASE_BUDGET_SECONDS, "fixed AUDIENCES_PHASE_BUDGET_SECONDS"
    return 30 * math.ceil(seconds / 30), "certified seconds rounded up to a multiple of 30"


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
        certificate = (
            _audiences_row(root, variant, write.seconds)
            if page == "audiences"
            else _TASK_COUNTERS[page](root, variant)
        )
        seconds = phased_task_seconds(certificate.phases, CERTIFIED_QUERY_CAP, write.seconds)
        key = f"{page}#{variant}"
        budget, basis = _budget(key, page, seconds)
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
