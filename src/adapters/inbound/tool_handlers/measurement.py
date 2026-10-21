# SPDX-License-Identifier: MIT-0
"""The measurement tool: what happened, from the curated facts.

The second tool, and the one build-order Phase 2 exists to reach — *"What was my shipped revenue
by ASIN last week?"* answered from real Iceberg with every caveat the figures need.

Thin, like the catalog handler: it plans, queries, reads coverage, and hands all three to
`domain/measurement/answers`. Every refusal, every default and every narrative sentence is in the
domain, so all of it is unit tested without an AWS account.

**It supplies the reporting-lens defaults ADR-024 requires, and the answer states them.** The
target question is under-specified as usually phrased — nobody asks about the MANUFACTURING
distributor view — so the tool pins those and the narrative says which. It does **not** default
the marketplace: that selects a different population and a different currency, so a question
without one is refused by ADR-024's rule rather than answered about a country nobody named.

**Ordinary tool execution binds exact Semantic V2 CURRENT publications before answering.** A
figure with no immutable publication set and no statement of what is loaded behind it is the
whole-looking number this platform exists to avoid, so every logical plan in `handle` and
`handle_batch` is selected transactionally before Athena runs. Coverage and qualified DQ are
derived from that same selected set rather than read independently. Presentation pages may instead
supply the ordered result of their shared exact-evidence inspection; this module validates every
present bound plan, executes no absent plan, and never performs a second publication read.

**It takes the contract, not a `FactShape`.** The shape is derived here by `FactShape.of`. Passing
the shape instead would mean also passing the contract id, the staleness threshold and the
measurement basis alongside it — at which point `FactShape` stops being "what a plan needs to know
about a fact" and becomes the contract with its field mappings removed, under a name that no
longer describes it.

Two absences carried over from the catalog handler, both still decisions rather than omissions.
*No Lambda entry point and no Powertools wiring*, because a tool reaches this through an
AgentCore Gateway target and `AgentStack` does not exist — fixing an event shape against a
contract nobody has configured would be guessing. *No module-scope catalogue or contract load*,
because how `contracts/` reaches the Lambda bundle is the same packaging question, and passing
them in is also what lets a test supply them without a file.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from domain.catalog.capability import (
    PERIOD_GRAIN_SELECTOR,
    CrossDeliveryException,
    DeliveryRatio,
    MetricKind,
    Refusal,
    RefusalReason,
)
from domain.catalog.freshness import Coverage
from domain.conversation.periodic import (
    ChatPeriodAnswer,
    ChatPeriodRequest,
    resolve_chat_period,
    resolve_default_period,
)
from domain.data_contracts.contract import CohortGrain
from domain.measurement.answers import (
    AnswerRequest,
    ComparisonAnswerRequest,
    DimensionDisplayLabels,
    FoldingAnswer,
    MeasurementContext,
    RankingAnswerRequest,
    answer_comparison,
    answer_measurement,
    answer_ranking,
    defaults_for,
    plan_roster_outcome,
)
from domain.measurement.comparison import RankOrder, RankRequest, compare, rank
from domain.measurement.figures import measure
from domain.measurement.folds import Fold, FoldResult, require_foldable
from domain.measurement.lenses import LENS_DEFAULTS
from domain.measurement.pack_coverage import PackDayVersions, pack_versioned_columns
from domain.measurement.periodic import (
    AS_OF_MONTH_COLUMN,
    BUCKET_SCHEME_COLUMN,
    COHORT_GRAIN_COLUMN,
    COHORT_PATH_COLUMN,
    GRAIN_SET_COLUMN,
    PROFILE_PATH_COLUMN,
    ROW_KIND_COLUMN,
    CrossDeliveryCheck,
    CrossDeliveryOperation,
    DeliveryOperand,
    VariantMismatchError,
    active_variant,
    check_cross_delivery,
    periodic_leaves,
    require_same_variant,
)
from domain.measurement.planning import (
    DATE_COLUMN,
    GRAIN_COLUMNS,
    MEASURE_ALIAS,
    FactShape,
    FactShapes,
    GrainSetShape,
    PlanRequest,
    QueryPlan,
    exact_only_grain_columns,
    leaf_references,
    measure_alias,
    plan,
    served_grain_set,
)
from domain.measurement.publications import BoundQueryPlan
from domain.measurement.ranking import RankedRead, RankedRestriction, require_rankable
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    ROSTER_WITHDRAWN,
    SEGMENT_PROFILE_FACT,
    Roster,
    RosterKind,
    roster_column,
    roster_filters,
    roster_kind_for,
    roster_lines,
    roster_scope_required,
)
from domain.narrative.shaped_response import (
    ClaimField,
    ResolutionProvenance,
    ShapedResponse,
    UnitKind,
    Units,
)
from domain.presentation.audiences import (
    SEGMENT_PARTIAL_TEXT,
    ProfileDelivery,
    ProfileSegment,
    SegmentFigures,
    segment_figures,
)
from domain.presentation.calendar_presets import PeriodicContract
from domain.presentation.retention import (
    ARP_PATH,
    STANDARD_PATH,
    CohortDelivery,
    arp_cell_rate,
    arp_cohort_checks,
)
from domain.producer.variants import PACK_VARIANT_MISMATCH
from domain.semantics.analytical_resolution import FieldOrigin
from domain.semantics.audience_segments import (
    SEGMENT_ID_COLUMN,
    SEGMENT_NAMESPACE_COLUMN,
    SegmentNamespace,
)
from domain.semantics.calendar_periods import (
    CalendarPeriod,
    is_complete_period,
    period_containing,
    vintage_ending,
)
from domain.semantics.time import TimeGrain
from domain.shared_kernel.canonical import canonical_model_digest
from domain.shared_kernel.errors import (
    AdlyticsError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from domain.shared_kernel.provenance import Suppression
from domain.shopper_aggregates.cohorts import cohort_grain_changed_text
from ports.outbound.pack_coverage import PackCoveragePort, PackCoverageReadRequest
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionResult,
    PresentationEvidenceInspectionSlot,
    PresentationEvidenceRole,
    PublicationSelection,
    PublicationSelectionPort,
    PublicationSelectionRequest,
)
from ports.outbound.query import (
    MAX_ROWS_CEILING,
    FoldingQueryPort,
    FoldRead,
    PeriodSeriesRequest,
    QueryPort,
    QueryRequest,
    QueryResult,
    RowCeiling,
    row_ceiling,
)
from ports.outbound.rosters import RosterPort, RosterRequest
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsPort, ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Generator, Mapping

    from domain.catalog.capability import Catalog, Metric
    from domain.data_contracts.contract import DataContract
    from domain.measurement.figures import Measurement
    from domain.shared_kernel.values import DateRange

__all__ = [
    "DEFAULT_MAX_ROWS",
    "QUERY_FAN_IN_COLLISION",
    "BoundMeasurementBatch",
    "MeasurementExecutionTooling",
    "MeasurementSlot",
    "MeasurementTool",
    "MeasurementToolRequest",
    "MeasurementTooling",
    "PreparedMeasurementBatch",
    "PreparedMeasurementOperation",
    "PreparedMeasurementPlan",
    "PresentationMeasurementAnswerSlot",
    "PresentationMeasurementExecutionResult",
    "PresentationMeasurementQueryResult",
    "PresentationMeasurementQuerySlot",
    "answer_arp_rate",
    "answer_segment_profile_ratio",
    "attach_delivered_keys",
    "attach_pack_coverage",
    "attach_roster",
    "bind_batch",
    "delivery_request",
    "distinct_query_count",
    "execute_batch",
    "execute_presentation_batch",
    "execute_presentation_query_batch",
    "handle",
    "handle_batch",
    "prepare_batch",
    "query_request_digest",
    "require_answered",
    "run_bounded",
    "series_request_digest",
]

MARKETPLACE_COLUMN = "marketplace_key"
"""The population column a roster is read for (`attach_roster`)."""

BAD_REQUEST = ErrorCode(area="MEASUREMENT", number=1)

RESULT_TOO_WIDE = ErrorCode(area="MEASUREMENT", number=2)
"""A comparison or a ranking hit the row ceiling, so the whole population was not read.

`DomainError` rather than `ValidationError`, and deliberately not an alarm. The request is
well-formed and the question is understood; what is unavailable is an honest answer to it at this
grain, which is a product signal and a refusal the caller can act on.
"""

SELECTION_MISMATCH = ErrorCode(area="MEASUREMENT", number=3)
"""A publication selector returned plans other than the ones requested, or changed their order."""

QUERY_FAN_IN_COLLISION = ErrorCode(area="MEASUREMENT", number=4)
"""Plan 4.4 (DB20): two complete requests share a canonical digest but are not equal; fatal."""

PUBLISH_TIME_ONLY = ErrorCode(area="MEASUREMENT", number=7)
"""A ranked or folded request reached a path that serves narration (ADR-143): a defect."""
RANKED_POPULATION_UNBOUND = ErrorCode(area="MEASUREMENT", number=8)
"""A ranked read's declared population isn't a read of its own batch (ADR-143): a defect."""

PERIODS_UNAVAILABLE = ErrorCode(area="MEASUREMENT", number=5)
"""A periodic metric was asked for on a runtime with no served-period reader (ADR-100)."""
VARIANT_MISMATCH = ErrorCode(area="MEASUREMENT", number=6)
"""A periodic figure would read two pack variants' series, or one variant's two branches (D9)."""

DEFAULT_MAX_ROWS = 500
"""How many rows one measurement answer may carry before it is truncated.

Well under the port's own ceiling and well under the Gateway's 6 MB response limit, because the
binding constraint is narration rather than transport: a breakdown of five hundred ASINs is
already more than a model can say anything true about, and the answer says it was truncated.
"""


class MeasurementTool(StrEnum):
    """The tool names this handler answers to.

    An enum rather than free strings, so a name registered as a Gateway target and a name handled
    here cannot drift apart silently.
    """

    METRIC_BY_GRAIN = "measurement_metric_by_grain"
    """One declared metric, over a window, broken down by one or more dimensions."""

    COMPARE_WINDOWS = "measurement_compare_windows"
    """The same metric and grain over two non-overlapping windows, with the change between them."""

    TOP_N = "measurement_top_n"
    """The largest n at a grain, by figure or by change, with the total they are judged against."""

    @property
    def needs_a_prior_window(self) -> bool:
        """Whether this tool cannot be answered without a second window.

        Only comparison requires one outright. A ranking takes one when it is ordering by change
        and refuses one otherwise, which `MeasurementToolRequest` enforces because the requirement
        depends on `order_by` rather than on the tool alone.
        """
        return self is MeasurementTool.COMPARE_WINDOWS


@dataclass(frozen=True, slots=True)
class MeasurementToolRequest:
    """One measurement tool invocation.

    Carries no identity of any kind — `ports/inbound/tools.py` records why that is structural
    rather than an oversight, and `FORBIDDEN_IDENTITY_FIELDS` is the list a test asserts against.
    """

    tool: MeasurementTool
    catalog: Catalog
    contracts: tuple[DataContract, ...]
    """Every contract, not the one this question reads.

    Plural for two reasons that arrived together. A fact is described by **every** contract
    writing it — `fct_ad_exposure` has four, each supplying different measures, so no single one
    declares the whole table and `ctr` cannot be planned from one. And a ratio across two facts
    needs both described, which no single contract can do at all.

    Resolving which contracts matter is this object's job rather than the caller's, because the
    answer comes from the metric's own declaration and a caller choosing would be choosing what
    the dictionary already states.
    """

    metric_name: str
    grains: tuple[str, ...]
    window: DateRange
    computed_at: datetime
    correlation_id: str
    resolution_provenance: tuple[ResolutionProvenance, ...] = ()
    """Sanitized semantic origins, empty for direct exact callers."""

    filters: Mapping[str, str] = field(default_factory=dict[str, str])
    """What the caller pinned. The tool adds the lens defaults on top, never replacing one."""

    defaulted_filters: Mapping[str, str] = field(default_factory=dict[str, str])
    """Resolver-applied defaults, kept separate so no governed choice looks explicit."""

    max_rows: int = DEFAULT_MAX_ROWS

    row_ceiling: RowCeiling = RowCeiling.NARRATION
    """Which caller kind bounds `max_rows` (ADR-143). Never a tool input: chat and request-time
    presentation reads are `NARRATION`; only publish-time bake sources name a bake ceiling."""

    ranked: RankedRestriction | None = None
    """Restrict a publish-time read to a ranked set computed inside the query (Retail's top 50).
    Never a tool input. Its population must be reads of the same batch (`prepare_batch`)."""

    reduction: Callable[[], Fold[Any]] | None = field(default=None, compare=False)
    """A publish-time reduction: a fresh fold per execution, so the read keeps only the fold's
    bounded summary (ADR-143). Never a tool input; eligibility is checked at prepare time."""

    prior_window: DateRange | None = None
    """The earlier period to compare against, for the two tools that take one.

    Must end before `window` begins. Two overlapping periods double-count the days they share, so
    the change between them is partly a comparison of a period with itself — and calling the later
    one "prior" would be a lie in the field name.
    """

    n: int = 0
    """How many rows a ranking shows. Zero for the tools that do not rank."""

    order_by: RankOrder = RankOrder.VALUE
    """What a ranking sorts by. Ignored by the tools that do not rank."""

    period_note: str = ""
    """The server-built sentence naming the complete period a periodic answer reads (ADR-100)."""

    pack_coverage: tuple[PackDayVersions, ...] | None = None
    """Per-day query pack versions read from publication state, for a metric over a column a
    later pack added (`attach_pack_coverage`). The planner refuses such a metric without it."""

    rosters: tuple[Roster, ...] = ()
    """The roster for each window, for a figure at a roster-graded grain set (`attach_roster`).

    Read from `fct_ad_exposure` publications; the planner refuses such a figure without the one
    for its window (`domain.measurement.rosters`)."""

    def __post_init__(self) -> None:
        """Refuse a combination of arguments no tool can answer.

        Here rather than in the entrypoint because these are relationships *between* arguments,
        and the entrypoint's job is reading one argument at a time out of the wire. A rule about
        two fields checked at the reading site is a rule each future reader has to rediscover.
        """
        if self.defaulted_filters.keys() & self.filters.keys():
            raise ValidationError(
                code=BAD_REQUEST,
                description="A resolver-applied default cannot also be an explicit filter.",
            )
        invalid_defaults = {
            key: value
            for key, value in self.defaulted_filters.items()
            if LENS_DEFAULTS.get(key) != value
        }
        if invalid_defaults:
            raise ValidationError(
                code=BAD_REQUEST,
                description="Resolver-applied defaults must use the governed report-lens values.",
                context={"invalid_default_keys": sorted(invalid_defaults)},
            )
        if self.tool.needs_a_prior_window and self.prior_window is None:
            raise ValidationError(
                code=BAD_REQUEST,
                description=(
                    "A comparison needs prior_window_start and prior_window_end, the earlier "
                    "period to compare against."
                ),
            )
        if self.tool is MeasurementTool.TOP_N:
            if self.n < 1:
                raise ValidationError(
                    code=BAD_REQUEST,
                    description="A ranking needs n, how many rows to return, of at least 1.",
                    context={"n": self.n},
                )
            if self.order_by is RankOrder.CHANGE and self.prior_window is None:
                raise ValidationError(
                    code=BAD_REQUEST,
                    description=(
                        "Ranking by CHANGE needs prior_window_start and prior_window_end to "
                        "measure the change against."
                    ),
                )
            if self.order_by is RankOrder.VALUE and self.prior_window is not None:
                # Refused rather than ignored. A caller who sent a prior window meant to rank by
                # change, and silently ordering by value would answer a different question with
                # every argument they supplied looking honoured.
                raise ValidationError(
                    code=BAD_REQUEST,
                    description=(
                        "A prior window was supplied for a ranking ordered by VALUE, which "
                        "reports no prior period. Set order_by to CHANGE, or drop the window."
                    ),
                )
        # **Checked here because the port raises a bare `ValueError` and this is wire input.**
        # `QueryRequest` is a port value object and rightly refuses a ceiling it cannot narrate
        # honestly, but `ValueError` is not an `AdlyticsError` — so it escapes the entry point's
        # translation, Lambda reports a failure, and Gateway tells the model the tool is broken
        # rather than that the question was declined. `max_rows` is a *declared tool argument*, so
        # that path is reachable by any caller: `max_rows: 999999` returned
        # `FunctionError: Unhandled` on deployed dev [verified 2026-08-22]. Refusing it here keeps
        # the port's convention intact and makes this adapter responsible for handing the port only
        # values it accepts, which is where that responsibility belongs.
        ceiling = row_ceiling(self.row_ceiling)
        if not 1 <= self.max_rows <= ceiling:
            raise ValidationError(
                code=BAD_REQUEST,
                description=(
                    f"max_rows must be between 1 and {MAX_ROWS_CEILING}. A result larger than "
                    "that cannot be narrated honestly, so asking for it would hide a truncation."
                    if self.row_ceiling is RowCeiling.NARRATION
                    else f"max_rows must be between 1 and the {self.row_ceiling.value} ceiling "
                    f"{ceiling}, which the baker's memory model certifies."
                ),
                context={"max_rows": self.max_rows, "ceiling": ceiling},
            )
        if (self.ranked is not None or self.reduction is not None) and (
            self.row_ceiling is RowCeiling.NARRATION
            or self.tool is not MeasurementTool.METRIC_BY_GRAIN
        ):
            raise ValidationError(
                code=BAD_REQUEST,
                description="A ranked or folded read is a publish-time breakdown of one window.",
            )
        if self.prior_window is not None and self.prior_window.end >= self.window.start:
            raise ValidationError(
                code=BAD_REQUEST,
                description=(
                    f"The prior period must end before the later one begins. "
                    f"{self.prior_window} does not end before {self.window} starts, so the two "
                    "would double-count the days they share."
                ),
            )

    @property
    def context(self) -> MeasurementContext:
        """The per-request values every answer carries. Validated by `MeasurementContext`."""
        return MeasurementContext(
            computed_at=self.computed_at,
            correlation_id=self.correlation_id,
            resolution_provenance=self.resolution_provenance,
        )

    @property
    def facts(self) -> tuple[str, ...]:
        """The canonical facts this question reads, in numerator-then-denominator order.

        Read from the metric's declaration through the dictionary, so nothing here decides which
        facts a question touches.
        """
        metric = self.catalog.metric(self.metric_name)
        if metric is None:
            raise ValidationError(
                code=BAD_REQUEST,
                description=f"{self.metric_name} is not a metric this platform declares.",
            )
        return self.catalog.facts_read(metric)

    @property
    def contributing_contracts(self) -> tuple[DataContract, ...]:
        """The contracts that write the facts this question reads.

        Derived here for the same reason `facts` and `shape` are: it follows from the metric's own
        declaration, so a caller choosing would be choosing what the dictionary already states.

        Two predicates, both load-bearing. `target_table in facts` spans **both** sides of a
        drill-across, so a ratio names the contracts behind its numerator and its denominator
        rather than one of them. `targets_a_fact` excludes the dimension contracts — a product
        hierarchy is joined against, not measured, and listing it as provenance for a figure would
        misstate where the figure came from.
        """
        return tuple(
            contract
            for contract in self.contracts
            if contract.target_table in self.facts and contract.targets_a_fact
        )

    @property
    def shape(self) -> FactShape | FactShapes:
        """Describe the fact or facts this question reads, derived rather than supplied.

        One shape for a question over a single fact, two for a ratio spanning a pair. Each is
        built from **every** contract writing that fact, because a fact written by four contracts
        is only fully described by all four.
        """
        facts = self.facts
        shapes = tuple(FactShape.for_fact(fact, self.contracts) for fact in facts)
        if len(shapes) == 1:
            return shapes[0]
        if len(shapes) == 2:  # noqa: PLR2004 — a ratio has two operands, not a tunable count
            return FactShapes(numerator=shapes[0], denominator=shapes[1])
        raise ValidationError(
            code=BAD_REQUEST,
            description=(
                f"{self.metric_name} reads {len(shapes)} facts, and a plan joins at most two."
            ),
            context={"metric": self.metric_name, "facts": list(facts)},
        )


@dataclass(frozen=True, slots=True)
class MeasurementTooling:
    """The outbound collaborators a measurement answer needs.

    Grouped so `handle` takes two arguments rather than four. They are genuinely one thing — the
    ports this tool reads through — and a caller holding them separately would be free to pass a
    registry from one environment beside a query port from another.
    """

    queries: QueryPort
    publications: PublicationSelectionPort
    labels: DimensionLabelResolver | None = None
    """Governed display-label projection for released rows, on the conversational path only.

    `None` on the presentation path deliberately: page projection substitutes its own labels
    against canonical selector values, and rows already carrying display text would break that
    exact-match projection."""
    query_batch_max_concurrency: int = 1
    periods: ServedPeriodsPort | None = None
    """Which complete periods are published, for a periodic metric's chat period (ADR-100).

    `None` refuses every periodic metric rather than guessing a period; daily metrics never
    read it."""
    pack_coverage: PackCoveragePort | None = None
    """Each day's published query pack version, for a metric over a pack-versioned column.

    `None` leaves such a metric without evidence, which the planner refuses; every other metric
    never reads it."""
    rosters: RosterPort | None = None
    """The keys with spend in a period, for a figure at a roster-graded grain set (plan D4).

    `None` leaves such a figure without its roster, which the planner refuses; every other figure
    never reads it."""

    def __post_init__(self) -> None:
        if self.query_batch_max_concurrency < 1:
            raise ValueError("query batch concurrency must be positive.")


@dataclass(frozen=True, slots=True)
class MeasurementExecutionTooling:
    """Outbound collaborators available after publication binding has completed."""

    queries: QueryPort
    labels: DimensionLabelResolver | None = None
    query_batch_max_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.query_batch_max_concurrency < 1:
            raise ValueError("query batch concurrency must be positive.")


@dataclass(frozen=True, slots=True)
class PreparedMeasurementPlan:
    """One governed logical plan with its stable presentation-period identity."""

    role: PresentationEvidenceRole
    logical: QueryPlan
    max_rows: int
    ceiling: RowCeiling = RowCeiling.NARRATION


@dataclass(frozen=True, slots=True)
class PreparedRanking:
    """A ranked request's planned rank and population reads, found in its batch at execution."""

    restriction: RankedRestriction
    rank: QueryPlan
    population: tuple[QueryPlan, ...]
    key_columns: tuple[str, str]


@dataclass(frozen=True, slots=True)
class PreparedMeasurementOperation:
    """One request whose defaults and every required logical plan are resolved."""

    request: MeasurementToolRequest
    applied: dict[str, str]
    current: QueryPlan
    prior: QueryPlan | None = None
    ranking: PreparedRanking | None = None

    @property
    def plans(self) -> tuple[PreparedMeasurementPlan, ...]:
        """Return the operation's plans in current-then-comparator order."""
        current = PreparedMeasurementPlan(
            PresentationEvidenceRole.CURRENT,
            self.current,
            self.request.max_rows,
            self.request.row_ceiling,
        )
        if self.prior is None:
            return (current,)
        return (
            current,
            PreparedMeasurementPlan(
                PresentationEvidenceRole.COMPARATOR,
                self.prior,
                self.request.max_rows,
                self.request.row_ceiling,
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedMeasurementBatch:
    """A non-empty ordered batch ready for one publication binding decision."""

    operations: tuple[PreparedMeasurementOperation, ...]
    contracts: tuple[DataContract, ...]
    now: datetime

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError("a measurement batch requires at least one request.")

    @property
    def plans(self) -> tuple[PreparedMeasurementPlan, ...]:
        """Flatten operations without losing request or period order."""
        return tuple(plan for operation in self.operations for plan in operation.plans)

    @property
    def logical_plans(self) -> tuple[QueryPlan, ...]:
        """Return exactly the logical sequence the publication selector must bind."""
        return tuple(prepared.logical for prepared in self.plans)


@dataclass(frozen=True, slots=True)
class BoundMeasurementBatch:
    """A prepared batch bound to the exact all-or-none publication selection."""

    prepared: PreparedMeasurementBatch
    selection: PublicationSelection

    def __post_init__(self) -> None:
        _require_exact_bound_plans(self.prepared.logical_plans, self.selection.plans)


@dataclass(frozen=True, slots=True)
class PresentationMeasurementQuerySlot:
    """One aligned presentation query result with its exact inspection evidence."""

    role: PresentationEvidenceRole
    logical: QueryPlan
    coverage: Coverage | None
    qualified_dq: tuple[str, ...]
    result: QueryResult | None

    def __post_init__(self) -> None:
        if (self.coverage is None) != (self.result is None):
            raise ValueError("presentation query slot must be wholly present or absent.")
        if self.coverage is None and self.qualified_dq:
            raise ValueError("absent presentation query cannot carry publication caveats.")

    @property
    def present(self) -> bool:
        """Whether exact evidence was present and the governed query ran."""
        return self.result is not None


@dataclass(frozen=True, slots=True)
class PresentationMeasurementQueryResult:
    """Ordered raw query-or-absent slots aligned to a prepared measurement batch."""

    slots: tuple[PresentationMeasurementQuerySlot, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("presentation query execution requires at least one slot.")


@dataclass(frozen=True, slots=True)
class PresentationMeasurementAnswerSlot:
    """One prepared presentation role's shaped answer, or stable absence.

    A held slot carries `answer`; a reduction slot carries `folded` (ADR-143), never both.
    """

    role: PresentationEvidenceRole
    logical: QueryPlan
    answer: ShapedResponse | None
    folded: FoldResult[Any] | None = None

    def __post_init__(self) -> None:
        if self.answer is not None and self.folded is not None:
            raise ValueError("a presentation slot is held or folded, never both.")

    @property
    def present(self) -> bool:
        """Whether exact evidence was present and the governed query ran."""
        return self.answer is not None or self.folded is not None


@dataclass(frozen=True, slots=True)
class PresentationMeasurementExecutionResult:
    """Ordered answer-or-absent slots aligned to a prepared measurement batch."""

    slots: tuple[PresentationMeasurementAnswerSlot, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("presentation measurement execution requires at least one slot.")


def prepare_batch(requests: tuple[MeasurementToolRequest, ...]) -> PreparedMeasurementBatch:
    """Resolve every request, default, and logical plan without reading publications."""
    if not requests:
        raise ValueError("a measurement batch requires at least one request.")

    operations: list[PreparedMeasurementOperation] = []
    contracts_by_id: dict[str, DataContract] = {}
    for request in requests:
        shape = request.shape
        applied = _lens_defaults(request, shape)
        later, earlier = _plan_requests(request, applied)
        current = plan(request.catalog, later, shape)
        prior = plan(request.catalog, earlier, shape) if earlier is not None else None
        if request.reduction is not None:
            require_foldable(current)
        operations.append(
            PreparedMeasurementOperation(
                request=request,
                applied=applied,
                current=current,
                prior=prior,
                ranking=None if request.ranked is None else _prepared_ranking(request, current),
            )
        )
        for contract in request.contributing_contracts:
            contracts_by_id.setdefault(contract.contract_id, contract)

    return PreparedMeasurementBatch(
        operations=tuple(operations),
        contracts=tuple(contracts_by_id.values()),
        now=requests[0].computed_at,
    )


def _prepared_ranking(request: MeasurementToolRequest, restricted: QueryPlan) -> PreparedRanking:
    """Plan a ranked request's rank and population reads exactly as their own requests would.

    Each is planned through the same defaults and filters as a breakdown of that metric over
    that window, so it equals the batch's own read of it (`_ranked_read` finds it there).
    """
    restriction = restricted_ranking(request)
    key_columns = (GRAIN_COLUMNS[restriction.key], GRAIN_COLUMNS["marketplace"])

    def _planned(metric: str, window: DateRange) -> QueryPlan:
        read = replace(
            request,
            metric_name=metric,
            window=window,
            grains=(restriction.key, "marketplace"),
            ranked=None,
            reduction=None,
        )
        shape = read.shape
        later, _ = _plan_requests(read, _lens_defaults(read, shape))
        return plan(read.catalog, later, shape)

    rank = _planned(restriction.rank_metric, restriction.rank_window)
    population = tuple(_planned(metric, window) for metric, window in restriction.population)
    require_rankable(
        restriction,
        restricted=restricted,
        rank=rank,
        population=population,
        key_columns=key_columns,
    )
    return PreparedRanking(restriction, rank, population, key_columns)


def restricted_ranking(request: MeasurementToolRequest) -> RankedRestriction:
    """Return a ranked request's restriction (a defect to ask of one without)."""
    if request.ranked is None:  # pragma: no cover - callers branch on `ranked` first
        raise InvariantError(PUBLISH_TIME_ONLY, "a ranked read was expected.")
    return request.ranked


def bind_batch(
    prepared: PreparedMeasurementBatch,
    publications: PublicationSelectionPort,
) -> BoundMeasurementBatch:
    """Bind the complete prepared sequence once, failing before analytical queries."""
    selection = publications.select(
        PublicationSelectionRequest(
            plans=prepared.logical_plans,
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    return BoundMeasurementBatch(prepared=prepared, selection=selection)


def execute_batch(
    bound: BoundMeasurementBatch,
    tooling: MeasurementExecutionTooling,
) -> tuple[ShapedResponse, ...]:
    """Execute an exactly bound batch in request order without publication authority."""
    operations: list[_Operation] = []
    offset = 0
    for prepared in bound.prepared.operations:
        if prepared.ranking is not None or prepared.request.reduction is not None:
            raise InvariantError(
                PUBLISH_TIME_ONLY,
                "a ranked or folded read reached the chat execution path.",
            )
        bound_current = bound.selection.plans[offset]
        offset += 1
        bound_prior = None
        if prepared.prior is not None:
            bound_prior = bound.selection.plans[offset]
            offset += 1
        operations.append(
            _Operation(
                request=prepared.request,
                tooling=tooling,
                applied=prepared.applied,
                current=prepared.current,
                bound_current=bound_current,
                selection=bound.selection,
                prior=prepared.prior,
                bound_prior=bound_prior,
            )
        )
    requests = tuple(
        QueryRequest(
            plan=plan, max_rows=operation.request.max_rows, ceiling=operation.request.row_ceiling
        )
        for operation in operations
        for plan in (operation.bound_current, operation.bound_prior)
        if plan is not None
    )
    results = _execute_queries(requests, tooling)
    executed: list[_Operation] = []
    result_offset = 0
    for operation in operations:
        current_result = results[result_offset]
        result_offset += 1
        if current_result is None:  # pragma: no cover - ordinary bound plans are always present
            raise AssertionError("bound measurement query returned no result")
        prior_result = None
        if operation.bound_prior is not None:
            prior_result = results[result_offset]
            result_offset += 1
            if prior_result is None:  # pragma: no cover - ordinary bound plans are always present
                raise AssertionError("bound comparator query returned no result")
        executed.append(
            replace(
                operation,
                current_result=current_result,
                prior_result=prior_result,
            )
        )
    return tuple(_answer(operation) for operation in executed)


def query_request_digest(request: QueryRequest) -> str:
    """Return the versioned canonical digest of every complete post-bind request field."""
    return canonical_model_digest("measurement-query-request/v1", request)


def series_request_digest(request: PeriodSeriesRequest) -> str:
    """Return a period series request's digest, in its own namespace (A5.4).

    A series and a single query can never share a digest, so the bake's bind pin and its
    certified-bound limiter count the two kinds apart.
    """
    return canonical_model_digest("measurement-period-series-request/v1", request)


def run_bounded[T](calls: tuple[Callable[[], T], ...], bound: int) -> tuple[T, ...]:
    """Run calls at most `bound` wide; observe every call, then raise the first error in order.

    Each worker runs in a copy of the caller's context, so an `athena.query` span parents under
    the caller's span and carries its run/task/cohort correlation (plan A2, A5.4). The one
    measurement fan-out every bake and chat batch shares.
    """
    if not calls:
        return ()
    if isinstance(bound, bool) or bound < 1:
        raise ValueError("a bounded fan-out runs at least one call at a time.")
    results: dict[int, T] = {}
    first_error: Exception | None = None
    with ThreadPoolExecutor(
        max_workers=min(bound, len(calls)), thread_name_prefix="measurement-query"
    ) as executor:
        futures = [executor.submit(copy_context().run, call) for call in calls]
        for index, future in enumerate(futures):
            try:
                results[index] = future.result()
            except Exception as error:  # retain the original error after every future is observed
                if first_error is None:
                    first_error = error
    if first_error is not None:
        raise first_error
    return tuple(results[index] for index in range(len(calls)))


def _fan_in(requests: tuple[QueryRequest | None, ...]) -> tuple[int | None, ...]:
    """Map each slot to the first slot holding an exactly equal complete request (DB20).

    Slots share one execution only on exact equality. An equal digest with an unequal value is
    a canonicalization defect, so it is fatal rather than silently shared or split.
    """
    leaders: dict[str, int] = {}
    mapped: list[int | None] = []
    for index, request in enumerate(requests):
        if request is None:
            mapped.append(None)
            continue
        leader = leaders.setdefault(query_request_digest(request), index)
        if leader != index and requests[leader] != request:
            raise InvariantError(
                QUERY_FAN_IN_COLLISION,
                "two complete query requests share a canonical digest but differ.",
                context={"slot": index, "leader": leader},
            )
        mapped.append(leader)
    return tuple(mapped)


def distinct_query_count(requests: tuple[QueryRequest | None, ...]) -> int:
    """Count the executions a complete-bound batch makes after exact fan-in (4.3, 4.6)."""
    return len({leader for leader in _fan_in(requests) if leader is not None})


def _execute_queries(
    requests: tuple[QueryRequest | None, ...],
    tooling: MeasurementExecutionTooling,
) -> tuple[QueryResult | None, ...]:
    """Run each distinct request once under the configured bound; restore every slot in order.

    Exactly equal complete requests share one execution and its result is copied into every
    original slot (plan 4.4). Every submitted future is observed and the first error in slot
    order is raised.
    """
    leaders = _fan_in(requests)
    distinct = tuple(
        (index, request)
        for index, request in enumerate(requests)
        if request is not None and leaders[index] == index
    )
    if not distinct:
        return tuple(None for _ in requests)
    results = run_bounded(
        tuple(partial(tooling.queries.run, request) for _index, request in distinct),
        tooling.query_batch_max_concurrency,
    )
    shared = {index: result for (index, _request), result in zip(distinct, results, strict=True)}
    return tuple(None if leader is None else shared[leader] for leader in leaders)


@contextmanager
def _execution_phase(queries: QueryPort, name: str) -> Generator[None]:
    """Expose a measurement boundary when the query adapter supplies a task observer."""
    phase = getattr(queries, "phase", None)
    if phase is None:
        yield
        return
    with phase(name):
        yield


def _presentation_requests(
    prepared: PreparedMeasurementBatch, inspection: PresentationEvidenceInspectionResult
) -> tuple[QueryRequest | None, ...]:
    """Build each present slot's complete request; a ranked one binds its batch's own reads."""
    plans = prepared.plans
    _require_exact_inspection(plans, inspection)
    operations = tuple(operation for operation in prepared.operations for _ in operation.plans)
    requests: list[QueryRequest | None] = []
    for operation, expected, inspected in zip(operations, plans, inspection.slots, strict=True):
        if inspected.bound is None:
            requests.append(None)
            continue
        ranked = None
        if operation.ranking is not None:
            ranked = _ranked_read(operation.ranking, prepared, inspection)
            if ranked is None:
                requests.append(None)
                continue
        requests.append(
            QueryRequest(
                plan=inspected.bound,
                max_rows=expected.max_rows,
                ceiling=expected.ceiling,
                ranked=ranked,
            )
        )
    return tuple(requests)


def _ranked_read(
    ranking: PreparedRanking,
    prepared: PreparedMeasurementBatch,
    inspection: PresentationEvidenceInspectionResult,
) -> RankedRead | None:
    """Bind a ranked read to the batch's own bound reads of its rank and population.

    The population is the very reads the stated total is measured from, so the ranked set and
    the total can't drift apart. A declared read missing from the batch is a defect; one whose
    evidence is absent leaves the ranked read absent too.
    """
    rows = tuple(
        (expected.logical, inspected.bound)
        for expected, inspected in zip(prepared.plans, inspection.slots, strict=True)
    )

    def _bound(logical: QueryPlan) -> BoundQueryPlan | bool | None:
        for candidate, bound in rows:
            if candidate == logical:
                return bound
        return False

    found = tuple(_bound(item) for item in (ranking.rank, *ranking.population))
    if any(item is False for item in found):
        raise InvariantError(
            RANKED_POPULATION_UNBOUND,
            "a ranked read's population is not a read of its own batch.",
            context={"metric": ranking.restriction.rank_metric},
        )
    if any(item is None for item in found):
        return None
    rank, *population = cast("tuple[BoundQueryPlan, ...]", found)
    return RankedRead(
        restriction=ranking.restriction,
        key_column=ranking.key_columns[0],
        marketplace_column=ranking.key_columns[1],
        population=tuple(population),
        rank=rank,
    )


def execute_presentation_query_batch(
    prepared: PreparedMeasurementBatch,
    inspection: PresentationEvidenceInspectionResult,
    tooling: MeasurementExecutionTooling,
) -> PresentationMeasurementQueryResult:
    """Validate the complete inspection, then execute each present raw plan exactly once.

    Raw results for the one caller that shapes nothing itself; a fold slot has no raw result,
    so one here is a defect.
    """
    if any(operation.request.reduction is not None for operation in prepared.operations):
        raise InvariantError(PUBLISH_TIME_ONLY, "a folded read has no raw presentation result.")
    plans = prepared.plans
    requests = _presentation_requests(prepared, inspection)
    with _execution_phase(tooling.queries, "query"):
        results = _execute_queries(requests, tooling)
    slots = tuple(
        PresentationMeasurementQuerySlot(
            role=expected.role,
            logical=expected.logical,
            coverage=inspected.coverage,
            qualified_dq=inspected.qualified_dq,
            result=result,
        )
        for expected, inspected, result in zip(plans, inspection.slots, results, strict=True)
    )
    return PresentationMeasurementQueryResult(slots)


def execute_presentation_batch(
    prepared: PreparedMeasurementBatch,
    inspection: PresentationEvidenceInspectionResult,
    tooling: MeasurementExecutionTooling,
) -> PresentationMeasurementExecutionResult:
    """Execute every present slot and shape it in its own worker (the shaping seam, ADR-143).

    Each distinct request runs once (exact fan-in), and its worker shapes every slot sharing it
    (held) or folds it (reduction) before returning, so a slot's raw rows are released when its
    worker returns and at most `query_batch_max_concurrency` slots' raw rows exist at once. The
    answers are the ones shaping after the whole batch gave: shaping was already per slot and
    pure.

    Whether a request is held or folded is each slot's own declaration, never its leader's: a
    request any slot folds is streamed once, every fold slot sharing it gets its own fresh fold
    of that one stream, and a held slot sharing it keeps the same rows `run` would have held. So
    one distinct request is always one statement, whatever mix of slots shares it.
    """
    requests = _presentation_requests(prepared, inspection)
    aligned = tuple(
        (operation, plan) for operation in prepared.operations for plan in operation.plans
    )
    leaders = _fan_in(requests)
    followers: dict[int, list[int]] = {}
    for index, leader in enumerate(leaders):
        if leader is not None:
            followers.setdefault(leader, []).append(index)

    def _worker(leader: int) -> tuple[tuple[int, PresentationMeasurementAnswerSlot], ...]:
        request = requests[leader]
        if request is None:  # pragma: no cover - a leader is always a present request
            raise AssertionError("a fan-in leader has no request")
        group = followers[leader]
        if any(aligned[index][0].request.reduction is not None for index in group):
            return _folded_group(group, request, aligned, inspection, tooling)
        result = tooling.queries.run(request)
        return tuple(
            (index, _shaped_slot(aligned[index], inspection.slots[index], tooling, result))
            for index in group
        )

    with _execution_phase(tooling.queries, "query"):
        shaped = run_bounded(
            tuple(partial(_worker, leader) for leader in sorted(followers)),
            tooling.query_batch_max_concurrency,
        )
    with _execution_phase(tooling.queries, "render"):
        by_index = {index: slot for group in shaped for index, slot in group}
        return PresentationMeasurementExecutionResult(
            tuple(
                by_index.get(index)
                or PresentationMeasurementAnswerSlot(expected.role, expected.logical, None)
                for index, (_operation, expected) in enumerate(aligned)
            )
        )


def _slot_operation(  # noqa: PLR0913, PLR0917 -- one slot's operation, plan, evidence and tooling
    operation: PreparedMeasurementOperation,
    expected: PreparedMeasurementPlan,
    inspected: PresentationEvidenceInspectionSlot,
    tooling: MeasurementExecutionTooling,
) -> _Operation:
    """Build the one-plan operation a presentation slot is shaped as."""
    bound = inspected.bound
    coverage = inspected.coverage
    if bound is None or coverage is None:  # pragma: no cover - present slots carry both
        raise AssertionError("present presentation query lost its bound plan")
    selected = PublicationSelection(
        plans=(bound,), coverage=coverage, qualified_dq=inspected.qualified_dq
    )
    return _Operation(
        request=operation.request,
        tooling=tooling,
        applied=operation.applied,
        current=expected.logical,
        bound_current=bound,
        selection=selected,
    )


def _shaped_slot(  # noqa: PLR0913, PLR0917 -- one slot, its evidence, tooling and result
    aligned: tuple[PreparedMeasurementOperation, PreparedMeasurementPlan],
    inspected: PresentationEvidenceInspectionSlot,
    tooling: MeasurementExecutionTooling,
    result: QueryResult,
) -> PresentationMeasurementAnswerSlot:
    """Shape one held slot's executed result, as a breakdown of its one plan."""
    operation, expected = aligned
    ready = _slot_operation(operation, expected, inspected, tooling)
    return PresentationMeasurementAnswerSlot(
        expected.role,
        expected.logical,
        answer_measurement(_slot_answer_request(ready, inspected, result.rows, result)),
    )


def _slot_answer_request(  # noqa: PLR0913, PLR0917 -- one slot's operation, evidence, rows and read
    ready: _Operation,
    inspected: PresentationEvidenceInspectionSlot,
    rows: tuple[dict[str, str | int | Decimal | None], ...],
    read: QueryResult | FoldRead,
) -> AnswerRequest:
    coverage = inspected.coverage
    if coverage is None:  # pragma: no cover - present slots carry coverage
        raise AssertionError("present presentation query lost its coverage")
    return AnswerRequest(
        plan=ready.current,
        context=ready.request.context,
        rows=rows,
        suppression=read.suppression,
        coverage=coverage,
        applied_defaults=ready.applied,
        additive=ready.additive,
        truncated=read.truncated,
        contract_ids=ready.contract_ids,
        qualified_dq=inspected.qualified_dq,
    )


class _HeldRows:
    """A held slot's share of a streamed read: every row it is handed, as `run` holds them."""

    def __init__(self) -> None:
        self.rows: list[dict[str, str | int | Decimal | None]] = []

    def add(self, row: dict[str, str | int | Decimal | None]) -> None:
        self.rows.append(row)


@dataclass(frozen=True, slots=True)
class _EverySink:
    """Hand each streamed row to every slot sharing the read, in slot order."""

    sinks: tuple[FoldingAnswer | _HeldRows, ...]

    def add(self, row: dict[str, str | int | Decimal | None]) -> None:
        for sink in self.sinks:
            sink.add(row)


def _folded_group(  # noqa: PLR0913, PLR0917 -- one shared request, its slots, evidence and tooling
    group: list[int],
    request: QueryRequest,
    aligned: tuple[tuple[PreparedMeasurementOperation, PreparedMeasurementPlan], ...],
    inspection: PresentationEvidenceInspectionResult,
    tooling: MeasurementExecutionTooling,
) -> tuple[tuple[int, PresentationMeasurementAnswerSlot], ...]:
    """Stream one request any slot folds, once; shape each sharing slot as it declared.

    Each fold slot gets a fresh fold, so only its bounded summary survives. A held slot sharing
    the request keeps the rows the stream handed it, at most `max_rows`, which are the rows
    `run` would have held, with the same truncation and suppression.
    """
    queries = tooling.queries
    if not isinstance(queries, FoldingQueryPort):
        raise InvariantError(PUBLISH_TIME_ONLY, "a folded read needs a folding query port.")
    sinks: dict[int, FoldingAnswer | _HeldRows] = {}
    for index in group:
        factory = aligned[index][0].request.reduction
        sinks[index] = (
            _HeldRows() if factory is None else FoldingAnswer(aligned[index][1].logical, factory())
        )
    read = queries.fold(request, _EverySink(tuple(sinks[index] for index in group)))
    shaped: list[tuple[int, PresentationMeasurementAnswerSlot]] = []
    for index in group:
        sink, inspected = sinks[index], inspection.slots[index]
        if isinstance(sink, _HeldRows):
            result = QueryResult(
                rows=tuple(sink.rows),
                bytes_scanned=read.bytes_scanned,
                truncated=read.truncated,
                suppression=read.suppression,
            )
            shaped.append((index, _shaped_slot(aligned[index], inspected, tooling, result)))
            continue
        operation, expected = aligned[index]
        ready = _slot_operation(operation, expected, inspected, tooling)
        folded = sink.finish(
            _slot_answer_request(ready, inspected, (), read),
            rows_read=read.rows_read,
            bytes_scanned=read.bytes_scanned,
        )
        shaped.append(
            (
                index,
                PresentationMeasurementAnswerSlot(expected.role, expected.logical, None, folded),
            )
        )
    return tuple(shaped)


def handle(request: MeasurementToolRequest, tooling: MeasurementTooling) -> ShapedResponse:
    """Answer one measurement invocation with the envelope, and nothing else."""
    return require_answered(handle_batch((request,), tooling))[0]


MeasurementSlot = ShapedResponse | DomainError | ValidationError
"""One request's result in a batch: its answer, or the typed refusal of that request alone."""


def require_answered(slots: tuple[MeasurementSlot, ...]) -> tuple[ShapedResponse, ...]:
    """Return every slot's answer, or raise the first slot's refusal.

    For a caller served whole or not at all: a single tool call, and a page (a page with one
    refused measurement isn't served in part).
    """
    answers: list[ShapedResponse] = []
    for slot in slots:
        if not isinstance(slot, ShapedResponse):
            raise slot
        answers.append(slot)
    return tuple(answers)


def handle_batch(
    requests: tuple[MeasurementToolRequest, ...], tooling: MeasurementTooling
) -> tuple[MeasurementSlot, ...]:
    """Answer a page's measurements after one all-or-none publication binding.

    Every logical plan for every metric, variant, and window is constructed before the
    publication port is called. The complete ordered plan sequence is then bound by one
    selection before the first query runs. This broadens the single-tool ordering guarantee
    without changing that tool's behavior.

    A request pinning one rostered key brings its whole delivery's request
    (`delivery_request`) into the same batch, so its grade and its figure read the same bound
    publications (`attach_delivered_keys`).

    An ARP rate pairs two deliveries, so it sits outside that binding and is answered on its
    own (`answer_arp_rate`). Its typed refusal (a paid delivery that hasn't arrived, or a
    malformed ARP request) is returned in its own slot, beside the batch's other answers; a
    caller served whole raises it with `require_answered`. Every other slot is an answer.
    """
    if any(_is_arp_rate(request) for request in requests):
        others = tuple(request for request in requests if not _is_arp_rate(request))
        answered = iter(handle_batch(others, tooling) if others else ())
        return tuple(
            _arp_slot(request, tooling) if _is_arp_rate(request) else next(answered)
            for request in requests
        )
    if any(_is_profile_ratio(request) for request in requests):
        # A C2 share or index pairs a segment row with its delivery's total row, so it's answered
        # on its own (`answer_segment_profile_ratio`); its refusal stays in its own slot.
        others = tuple(request for request in requests if not _is_profile_ratio(request))
        answered = iter(handle_batch(others, tooling) if others else ())
        return tuple(
            _profile_slot(request, tooling) if _is_profile_ratio(request) else next(answered)
            for request in requests
        )
    attached = tuple(
        attach_roster(
            attach_pack_coverage(_at_served_period(request, tooling), tooling.pack_coverage),
            tooling.rosters,
        )
        for request in requests
    )
    deliveries = tuple(delivery_request(request) for request in attached)
    prepared = prepare_batch(
        (*attached, *(delivery for delivery in deliveries if delivery is not None))
    )
    bound = bind_batch(prepared, tooling.publications)
    execution = MeasurementExecutionTooling(
        queries=tooling.queries,
        labels=tooling.labels,
        query_batch_max_concurrency=tooling.query_batch_max_concurrency,
    )
    return execute_batch(attach_delivered_keys(bound, deliveries, execution), execution)


def attach_pack_coverage(
    request: MeasurementToolRequest, port: PackCoveragePort | None
) -> MeasurementToolRequest:
    """Attach per-day pack versions when the metric reads a pack-versioned or late writer's column.

    A pack-versioned column is one a later pack version added; a late writer's column is read only
    over the rows its contract owns (`served_where_published`). Read from publication state for
    the request's window and prior window. The planner decides: any uncovered day refuses the
    window naming the first fully covered date
    (`domain.measurement.pack_coverage`). A request whose metric reads no such column, or a
    runtime without the port, is returned unchanged; the planner refuses the second.
    """
    metric = request.catalog.metric(request.metric_name)
    if metric is None or port is None:
        return request
    references = leaf_references(request.catalog, metric)
    # A publication-gated column (`served_where_published`) is read only when the request pins
    # the rows its contract owns, so a sponsored reach request never consults DSP reach coverage.
    gated = {
        column.contract_id
        for column in pack_versioned_columns(request.contributing_contracts)
        if (column.fact, column.column) in references and column.applies_to(request.filters)
    }
    contracts = tuple(
        contract for contract in request.contributing_contracts if contract.contract_id in gated
    )
    if not contracts:
        return request
    windows = (
        request.window,
        *(() if request.prior_window is None else (request.prior_window,)),
    )
    coverage = port.pack_coverage(
        PackCoverageReadRequest(contracts=contracts, filters=request.filters, windows=windows)
    )
    return replace(request, pack_coverage=coverage)


def attach_roster(
    request: MeasurementToolRequest, port: RosterPort | None
) -> MeasurementToolRequest:
    """Attach the roster for each window when the figure reads a roster-graded grain set (D4).

    A grain set whose cells come back one row per key (pack 2's ad products) is read against the
    keys with spend in the same period, marketplace and platform, so a rostered key with no row
    reads "not available" in chat, the bake and insights alike (`domain.measurement.rosters`).
    A roster is per marketplace, so such a figure needs one: refused naming it otherwise. Every
    other figure, or a runtime without the port, is returned unchanged; the planner refuses the
    second.
    """
    pinned = request.filters.get(GRAIN_SET_COLUMN)
    if port is None or pinned is None:
        return request
    graded = tuple(
        (contract, kind)
        for contract in _periodic_contracts(request)
        if any(item.name == pinned for item in GrainSetShape.of(contract))
        and (kind := roster_kind_for(contract)) is not None
    )
    if not graded:
        return request
    contract, kind = graded[0]
    if (
        kind is RosterKind.SEGMENT
        and kind.value not in request.grains
        and roster_column(kind) not in request.filters
    ):
        # A segment roster grades segment rows only; a C2 total read from the delivery's
        # profiled_total row names no segment, so nothing is graded (packs 10-12).
        return request
    marketplace = request.filters.get(MARKETPLACE_COLUMN)
    if marketplace is None:
        raise ValidationError(
            code=ROSTER_REFUSED,
            description=(
                f"{request.metric_name} per {kind.value.replace('_', ' ')} is read for one "
                "marketplace at a time. Name the marketplace."
            ),
            context={"metric": request.metric_name, "grain_set": pinned},
        )
    missing_scope = tuple(
        column
        for column in roster_scope_required(kind, contract.target_table)
        if column not in request.filters
    )
    if missing_scope and contract.target_table == SEGMENT_PROFILE_FACT:
        raise ValidationError(
            code=ROSTER_REFUSED,
            description=(
                f"{request.metric_name} per audience segment is read for one profile path at a "
                "time, because each path's segments are listed against its own previous month. "
                "Name the profile path."
            ),
            context={"metric": request.metric_name, "grain_set": pinned},
        )
    if missing_scope:
        # Each campaign's segments are its own roster (design §6.5): a segment breakdown over
        # several campaigns would grade one campaign's segments against another's.
        raise ValidationError(
            code=ROSTER_REFUSED,
            description=(
                f"{request.metric_name} per {kind.value.replace('_', ' ')} is read for one "
                "campaign at a time, because each campaign's segments are listed against its "
                "own previous month. Name the campaign."
            ),
            context={"metric": request.metric_name, "grain_set": pinned},
        )
    windows = (
        request.window,
        *(() if request.prior_window is None else (request.prior_window,)),
    )
    rosters = tuple(
        port.roster(
            RosterRequest(
                kind=kind,
                period=window,
                marketplace_key=marketplace,
                platform_key=contract.platform_key,
                filters=roster_filters(kind, request.filters),
                fact=contract.target_table,
                ceiling=request.row_ceiling,
            )
        )
        for window in windows
    )
    return replace(request, rosters=rosters)


def delivery_request(request: MeasurementToolRequest) -> MeasurementToolRequest | None:
    """Return the whole-delivery request a request pinning one rostered key is graded on.

    Withdrawal is per delivery (design §4.4), so a question about one campaign is graded on the
    same keys the per-campaign breakdown would return: the same metric, window and filters,
    without the pinned key, by the roster's key. None for every other request.
    """
    current = next((item for item in request.rosters if item.period == request.window), None)
    if current is None:
        return None
    column = roster_column(current.kind)
    if column not in request.filters:
        return None
    return replace(
        request,
        tool=MeasurementTool.METRIC_BY_GRAIN,
        grains=(current.kind.value,),
        filters={key: value for key, value in request.filters.items() if key != column},
        prior_window=None,
        n=0,
        order_by=RankOrder.VALUE,
        max_rows=MAX_ROWS_CEILING,
        rosters=(current,),
    )


def attach_delivered_keys(
    bound: BoundMeasurementBatch,
    deliveries: tuple[MeasurementToolRequest | None, ...],
    tooling: MeasurementExecutionTooling,
) -> BoundMeasurementBatch:
    """Grade each pinned request on the keys its delivery returned, from the same binding.

    `bound` holds the requests, then each non-None entry of `deliveries` (`delivery_request`,
    one per request, in order), all bound by one selection. The delivery plans run first, under
    that selection; each pinned request's current roster then carries the keys its delivery
    returned (`Roster.returned`), so the grade and the figure read the same immutable
    publications. Returns the requests alone, still bound to that selection; a batch with no
    pinned request is returned unchanged. A truncated delivery read is refused `ROSTER_REFUSED`.
    """
    count = len(deliveries)
    operations = bound.prepared.operations
    graded_by = operations[count:]
    if not graded_by:
        return bound
    widths = tuple(len(operation.plans) for operation in operations[:count])
    delivery_plans = bound.selection.plans[sum(widths) :]
    results = _execute_queries(
        tuple(QueryRequest(plan=item, max_rows=MAX_ROWS_CEILING) for item in delivery_plans),
        tooling,
    )
    graded = iter(zip(graded_by, results, strict=True))
    graded_operations: list[PreparedMeasurementOperation] = []
    graded_plans: list[BoundQueryPlan] = []
    offset = 0
    for operation, delivery, width in zip(operations[:count], deliveries, widths, strict=True):
        plans = bound.selection.plans[offset : offset + width]
        offset += width
        if delivery is None:
            graded_operations.append(operation)
            graded_plans.extend(plans)
            continue
        whole, result = next(graded)
        if result is None:  # pragma: no cover - ordinary bound plans are always present
            raise AssertionError("bound delivery query returned no result")
        current = _with_delivered_keys(operation, whole, result)
        graded_operations.append(replace(operation, current=current))
        graded_plans.extend((replace(plans[0], logical=current), *plans[1:]))
    return BoundMeasurementBatch(
        prepared=replace(bound.prepared, operations=tuple(graded_operations)),
        selection=replace(bound.selection, plans=tuple(graded_plans)),
    )


def _with_delivered_keys(
    operation: PreparedMeasurementOperation,
    whole: PreparedMeasurementOperation,
    result: QueryResult,
) -> QueryPlan:
    """Return the pinned request's current plan with its roster's delivery keys attached."""
    roster = operation.current.roster
    if roster is None:  # pragma: no cover - a delivery request exists only beside a roster
        raise AssertionError("a pinned request's plan lost its roster")
    if result.truncated:
        raise ValidationError(
            code=ROSTER_REFUSED,
            description=(
                f"{operation.request.metric_name} per {roster.kind.value.replace('_', ' ')} "
                "can't be graded in full for this period, so no figure for one of them is "
                "answered."
            ),
            context={"metric": operation.request.metric_name, "roster": roster.kind.value},
        )
    column = roster_column(roster.kind)
    returned = tuple(
        dict.fromkeys(
            value
            for figure in measure(whole.current, result.rows).figures
            if isinstance(value := dict(figure.dimensions).get(column), str)
        )
    )
    return replace(operation.current, roster=replace(roster, returned=returned))


def _periodic_contracts(request: MeasurementToolRequest) -> tuple[DataContract, ...]:
    """Return the contracts writing the periodic facts a metric reads, in contract order."""
    metric = request.catalog.metric(request.metric_name)
    if metric is None:
        return ()
    facts = {
        fact
        for leaf in periodic_leaves(request.catalog, metric)
        for fact in request.catalog.facts_read(leaf)
    }
    return tuple(
        contract for contract in request.contributing_contracts if contract.target_table in facts
    )


def _grain_sets(contracts: tuple[DataContract, ...]) -> tuple[GrainSetShape, ...]:
    """Return each periodic contract's grain set, in contract order."""
    return tuple(item for contract in contracts for item in GrainSetShape.of(contract))


def _served_grain_set(
    request: MeasurementToolRequest, contracts: tuple[DataContract, ...]
) -> str | None:
    """Return the grain set a periodic question reads, or None when it can't be chosen (D-1).

    One served grain set is pinned as it is. With several, a question naming only its marketplace
    reads the platform grain set and one naming the ad product reads the channel grain set
    (`served_grain_set`); neither is derived from the other.
    """
    grain_sets = _grain_sets(contracts)
    names = {item.name for item in grain_sets}
    if len(names) == 1:
        return next(iter(names))
    requested = frozenset(
        {
            *(GRAIN_COLUMNS[name] for name in request.grains if name in GRAIN_COLUMNS),
            *request.filters,
        }
    )
    return served_grain_set(grain_sets, requested)


def _at_served_period(
    request: MeasurementToolRequest, tooling: MeasurementTooling
) -> MeasurementToolRequest:
    """Move a periodic question to the complete measured period it can be answered at.

    Chat's window arrives as a governed reference period (never model arithmetic). A complete
    period that was measured is answered as asked; the calendar's latest period that hasn't
    landed yet is answered at the latest measured one, and the answer says so; a rolling or
    partial window is refused naming the latest measured period (`resolve_chat_period`). The
    period's grain is pinned as `time_grain`, and the one served grain set as `grain_set`
    (D-1, D-7). A daily metric is returned unchanged.
    """
    contracts = _periodic_contracts(request)
    metric = request.catalog.metric(request.metric_name)
    if not contracts or metric is None:
        return request
    grain_set = request.filters.get(GRAIN_SET_COLUMN) or _served_grain_set(request, contracts)
    if grain_set is not None:
        # Only the chosen grain set's contract decides which periods are measured, so a late
        # per-channel delivery never holds back a platform answer, nor the reverse.
        contracts = (
            tuple(
                contract
                for contract in contracts
                if all(item.name == grain_set for item in GrainSetShape.of(contract))
            )
            or contracts
        )
    contracts = _on_cohort_path(contracts, request.filters.get(COHORT_PATH_COLUMN))
    contracts = _on_path(contracts, PROFILE_PATH_COLUMN, request.filters.get(PROFILE_PATH_COLUMN))
    if tooling.periods is None:
        raise InvariantError(
            PERIODS_UNAVAILABLE,
            "A periodic metric needs the served-period reader, which this runtime lacks.",
            context={"metric": request.metric_name},
        )
    served = tooling.periods.served_periods(
        ServedPeriodsRequest(contracts=contracts, filters=request.filters, now=request.computed_at)
    )
    leaves = periodic_leaves(request.catalog, metric)
    grains = tuple(
        grain
        for grain in leaves[0].period_grains
        if all(grain in leaf.period_grains for leaf in leaves)
    )
    # The chosen grain set may deliver fewer periods than the metric declares (pack 3's
    # campaigns are monthly only), so a week question there is refused naming the month.
    measured = {
        grain
        for contract in contracts
        for item in GrainSetShape.of(contract)
        for grain in item.period_grains
    }
    grains = tuple(grain for grain in grains if grain in measured) or grains
    periodic = tuple(
        PeriodicContract(contract.contract_id, served.cadence_lag_days[contract.contract_id])
        for contract in contracts
    )

    def chat(window: DateRange) -> ChatPeriodRequest:
        return ChatPeriodRequest(
            metric_name=request.metric_name,
            period_grains=grains,
            window=window,
            as_of=request.computed_at.date(),
            contracts=periodic,
            publications=served.publications,
        )

    def resolved(window: DateRange) -> ChatPeriodAnswer:
        return resolve_chat_period(chat(window))

    # A cohort question reads its vintage; a question naming no period reads the latest
    # measured complete period, never the 7-day coverage default a daily metric uses (seam
    # finding 3); any other window is answered as asked or refused.
    current = (
        resolved(_vintage_window(request, served, contracts))
        if grains == (TimeGrain.CUSTOM,)
        else (resolve_default_period if _window_defaulted(request) else resolve_chat_period)(
            chat(request.window)
        )
    )
    if current.period is None:
        raise ValidationError(code=BAD_REQUEST, description=current.sentence)
    prior_window = None
    if request.prior_window is not None:
        prior = resolved(request.prior_window)
        grain = current.period.grain
        if prior.period is None or prior.substituted or prior.period.grain is not grain:
            raise ValidationError(
                code=BAD_REQUEST,
                description=(
                    f"The comparison period isn't a measured complete {grain.value}. "
                    f"{prior.sentence}"
                ),
            )
        prior_window = prior.period.window
    pins = {PERIOD_GRAIN_SELECTOR: current.period.grain.value}
    if grain_set is not None and GRAIN_SET_COLUMN not in request.filters:
        pins[GRAIN_SET_COLUMN] = grain_set
    if current.period.grain is TimeGrain.CUSTOM:
        pins.update(
            _cohort_pins(
                _RowKindRequest(request, metric, contracts, served, (current.period, prior_window))
            )
        )
    if ROW_KIND_COLUMN not in request.filters:
        series = _served_series_pins(
            _RowKindRequest(request, metric, contracts, served, (current.period, prior_window))
        )
        pins.update(
            {column: value for column, value in series.items() if column not in request.filters}
        )
    namespaces = {
        field.literal_value
        for contract in contracts
        if (field := contract.field_for(SEGMENT_NAMESPACE_COLUMN)) is not None
    }
    if SEGMENT_NAMESPACE_COLUMN not in request.filters and len(namespaces) == 1:
        # One segment namespace per answer (ADR-103 point 6): pack 8's rows are all DSP
        # behaviour segments, a grain column no figure sums across or compares with ASI.
        (namespace,) = namespaces
        if namespace is not None:
            pins[SEGMENT_NAMESPACE_COLUMN] = namespace
    filters = {name: value for name, value in request.filters.items() if name != AS_OF_MONTH_COLUMN}
    return replace(
        request,
        window=current.period.window,
        prior_window=prior_window,
        filters={**filters, **pins},
        period_note=current.sentence,
    )


NO_VINTAGE = "No cohort vintage has been measured yet."
CURRENT_PERIOD_FIELD = "current_period"
"""The resolution provenance field a question's own window is recorded under."""


def _window_defaulted(request: MeasurementToolRequest) -> bool:
    """Whether the question named no period, so the window is the governed coverage default."""
    return any(
        item.field == CURRENT_PERIOD_FIELD and item.origin is FieldOrigin.GOVERNED_DEFAULT
        for item in request.resolution_provenance
    )


_AS_OF_MONTH = re.compile(r"\d{4}-\d{2}(?:-01)?")
"""The whole `as_of_month` selector: `YYYY-MM` or that month's first day, nothing after it."""


def _on_cohort_path(
    contracts: tuple[DataContract, ...], path: str | None
) -> tuple[DataContract, ...]:
    """Keep the deliveries of the one cohort path a figure reads (ADR-104).

    Pack 14's for `standard`, pack 15's for `arp`, so a late paid delivery never holds back a
    standard answer, nor the reverse. Without a path, or with no contract on it, all are kept.
    """
    return _on_path(contracts, COHORT_PATH_COLUMN, path)


def _on_path(
    contracts: tuple[DataContract, ...], column: str, path: str | None
) -> tuple[DataContract, ...]:
    """Keep the deliveries whose `column` literal is the one path a figure reads.

    A cohort path (ADR-104) or a C2 profile path (packs 10-12): each is its own delivery, so a
    late paid delivery never holds back another path's answer, and the path's one segment
    namespace is pinned. Without a path, or with no contract on it, all are kept.
    """
    if path is None:
        return contracts
    return (
        tuple(
            contract
            for contract in contracts
            if (field := contract.field_for(column)) is None or field.literal_value == path
        )
        or contracts
    )


def _vintage_window(
    request: MeasurementToolRequest, served: ServedPeriods, contracts: tuple[DataContract, ...]
) -> DateRange:
    """Return the cohort vintage a cohort question reads (ADR-104): `as_of_month`, else the latest.

    `as_of_month` is an exact selector (`YYYY-MM` or its first day). Without it, a question naming
    one whole vintage reads it; any other window reads the latest vintage every contract the
    figure reads has published, and the answer names it.
    """
    named = request.filters.get(AS_OF_MONTH_COLUMN)
    if named is not None:
        not_a_month = ValidationError(
            code=BAD_REQUEST, description=f"as_of_month {named!r} isn't a month (YYYY-MM)."
        )
        # The whole selector is checked: a trailing suffix never reads as the month before it.
        if _AS_OF_MONTH.fullmatch(named) is None:
            raise not_a_month
        try:
            month = date.fromisoformat(f"{named[:7]}-01")
        except ValueError:
            raise not_a_month from None
        return vintage_ending(month).window
    window = request.window
    if is_complete_period(TimeGrain.CUSTOM, window.start, window.end):
        return window
    ids = {contract.contract_id for contract in contracts}
    vintages = sorted(
        {period for contract, period in served.publications if contract in ids},
        reverse=True,
    )
    latest = next(
        (
            period
            for period in vintages
            if period.grain is TimeGrain.CUSTOM
            and all((contract_id, period) in served.publications for contract_id in ids)
        ),
        None,
    )
    if latest is None:
        raise ValidationError(code=BAD_REQUEST, description=NO_VINTAGE)
    return latest.window


def _cohort_grain(contract: DataContract, variant: str | None) -> str | None:
    cells = contract.expected_cells
    return None if cells is None else cells.value_for(variant, COHORT_GRAIN_COLUMN, ())


def _cohort_pins(item: _RowKindRequest) -> dict[str, str]:
    """Pin a cohort vintage's `as_of_month` and the cohort grain its series holds (plan D9, D11).

    A vintage is served from the one series holding its CURRENT publication: monthly cohorts
    (`cohort_grain = month`), or the quarterly variant once switched on. A comparison of a
    monthly and a quarterly vintage is refused with D-8 (a)'s "Cohort grain changed in
    {period}.", {period} being the as-of month of the marketplace's first quarterly vintage.
    """
    period, prior_window = item.periods
    pins = {AS_OF_MONTH_COLUMN: period.as_of_month.isoformat()}
    for contract in item.contracts:
        if contract.field_for(COHORT_GRAIN_COLUMN) is None:
            continue
        try:
            variant = active_variant(
                item.served.series_of(contract.contract_id, period), period.label
            )
            grain = _cohort_grain(contract, variant)
            if prior_window is not None:
                prior = CalendarPeriod(prior_window.start, prior_window.end, period.grain)
                before = active_variant(
                    item.served.series_of(contract.contract_id, prior), prior.label
                )
                if _cohort_grain(contract, before) != grain:
                    raise ValidationError(
                        code=VARIANT_MISMATCH,
                        description=cohort_grain_changed_text(
                            _first_quarterly(item, contract, (period, prior))
                        ),
                        context={
                            "finding_code": PACK_VARIANT_MISMATCH,
                            "metric": item.metric.name,
                            "reason": "cohort_grain_changed",
                        },
                    )
        except VariantMismatchError as error:
            raise ValidationError(
                code=VARIANT_MISMATCH,
                description=str(error),
                context={"finding_code": PACK_VARIANT_MISMATCH, "metric": item.metric.name},
            ) from None
        if grain is not None:
            pins[COHORT_GRAIN_COLUMN] = grain
        break
    return pins


def _first_quarterly(
    item: _RowKindRequest, contract: DataContract, compared: tuple[CalendarPeriod, ...]
) -> str:
    """Name the as-of month of the marketplace's first served vintage on quarterly cohorts."""
    quarterly = sorted(
        period
        for contract_id, period in item.served.publications
        if contract_id == contract.contract_id
        and period.grain is TimeGrain.CUSTOM
        and any(
            _cohort_grain(contract, series) == "quarter"
            for series in item.served.series_of(contract_id, period)
        )
    )
    first = quarterly[0] if quarterly else max(compared)
    return period_containing(TimeGrain.MONTH, first.as_of_month).label


WITHHELD_REASON_FIELD = "withheld_reason"
"""The row field naming why an ARP rate cell is withheld; None on every other row."""
_STANDARD_RATES = ("repeat_buyer_rate_account_ids", "cumulative_repeat_rate_account_ids")
"""The standard path's account-ID rates, offered when the paid path hasn't delivered."""
_CELL_COLUMNS = ("cohort_month", "months_since_acquisition", "acquisition_channel_group")


def _is_arp_rate(request: MeasurementToolRequest) -> bool:
    """Whether the question asks an ARP rate, the one figure paired across two deliveries."""
    metric = request.catalog.metric(request.metric_name)
    return metric is not None and metric.cross_delivery is CrossDeliveryException.ARP_COHORT_RATIO


def _cohort_contract(request: MeasurementToolRequest, path: str) -> DataContract:
    """Return the one cohort contract on a path (pack 14 `standard`, pack 15 `arp`)."""
    found = next(
        (
            contract
            for contract in request.contributing_contracts
            if (stamped := contract.field_for(COHORT_PATH_COLUMN)) is not None
            and stamped.literal_value == path
        ),
        None,
    )
    if found is None:
        raise InvariantError(
            SELECTION_MISMATCH,
            f"No cohort contract writes the {path} path.",
            context={"metric": request.metric_name, "cohort_path": path},
        )
    return found


def _arp_not_delivered(
    metric: Metric, period: CalendarPeriod | None, latest: CalendarPeriod | None
) -> AdlyticsError:
    """Refuse an ARP rate whose paid delivery isn't here, naming what the paid path does hold."""
    what = (
        "any cohort vintage"
        if period is None
        else f"the vintage ending {period_containing(TimeGrain.MONTH, period.as_of_month).label}"
    )
    explanation = (
        f"{metric.name} needs the Amazon Retail Purchases paid feature (cohort_path arp), and no "
        f"delivery of {what} has arrived in this deployment. That doesn't mean the customer isn't "
        "subscribed: a producer that hasn't delivered looks the same. The standard path's rates "
        "still answer."
    )
    if latest is not None:
        label = period_containing(TimeGrain.MONTH, latest.as_of_month).label
        explanation += f" The latest vintage it has delivered ends {label}; name it as as_of_month."
    return Refusal(
        requested=metric.name,
        reason=RefusalReason.NOT_OBSERVED_IN_DEPLOYMENT,
        explanation=explanation,
        offer_instead=_STANDARD_RATES,
    ).to_error()


def _arp_vintage(
    request: MeasurementToolRequest,
    tooling: MeasurementTooling,
    contracts: tuple[DataContract, DataContract],
) -> tuple[CalendarPeriod, str]:
    """Return the vintage both paths read and the month a cohort-grain change is named after.

    `as_of_month` when named, else the latest vintage both paths have published. A paid path
    with no delivery of it refuses this rate alone (`_arp_not_delivered`).
    """
    metric = request.catalog.metric(request.metric_name)
    if metric is None:  # pragma: no cover - `_is_arp_rate` resolved it
        raise ValidationError(code=BAD_REQUEST, description=f"{request.metric_name} is unknown.")
    if tooling.periods is None:
        raise InvariantError(
            PERIODS_UNAVAILABLE,
            "A periodic metric needs the served-period reader, which this runtime lacks.",
            context={"metric": request.metric_name},
        )
    standard, paid = contracts
    served = tooling.periods.served_periods(
        ServedPeriodsRequest(contracts=contracts, filters=request.filters, now=request.computed_at)
    )
    held = sorted(
        period
        for contract_id, period in served.publications
        if contract_id == paid.contract_id and period.grain is TimeGrain.CUSTOM
    )
    if not held:
        raise _arp_not_delivered(metric, None, None)
    window = _vintage_window(request, served, contracts)
    period = vintage_ending(date(window.end.year, window.end.month, 1))
    if period not in held:
        raise _arp_not_delivered(metric, period, held[-1])
    item = _RowKindRequest(request, metric, contracts, served, (period, None))
    return period, _first_quarterly(item, standard, (period,))


def _cell_key(plan: QueryPlan, dimensions: Mapping[str, str]) -> tuple[date, int, str]:
    """Return a figure's cohort cell (cohort, follow-up, group), from its row or its pins."""
    values: list[str] = []
    for column in _CELL_COLUMNS:
        value = dimensions.get(column, plan.filters.get(column))
        if value is None:  # pragma: no cover - the operands' exact-only axes group or pin each
            raise InvariantError(
                SELECTION_MISMATCH,
                "An ARP operand's row names no cohort cell.",
                context={"metric": plan.metric.name, "column": column},
            )
        values.append(value)
    return date.fromisoformat(values[0]), int(values[1]), values[2]


@dataclass(frozen=True, slots=True)
class _ArpOperand:
    """One ARP operand's returned cells, keyed by cohort cell, and their row dimensions."""

    delivery: CohortDelivery
    dimensions: dict[tuple[date, int, str], dict[str, str]]


def _arp_operand(plan: QueryPlan, result: QueryResult, *, measure_name: str) -> _ArpOperand:
    """Read one operand's answer into the `CohortDelivery` the Retention bake builds too."""
    grain = plan.filters.get(COHORT_GRAIN_COLUMN)
    if grain is None:  # pragma: no cover - `_cohort_pins` pins every cohort contract's grain
        raise InvariantError(
            SELECTION_MISMATCH,
            "An ARP operand was planned without its cohort grain.",
            context={"metric": plan.metric.name},
        )
    cells: dict[tuple[date, int, str], dict[str, Decimal | None]] = {}
    dimensions: dict[tuple[date, int, str], dict[str, str]] = {}
    for figure in measure(plan, result.rows).figures:
        values = dict(figure.dimensions)
        key = _cell_key(plan, values)
        cells[key] = {measure_name: Decimal(figure.value)}
        dimensions[key] = values
    path = plan.filters[COHORT_PATH_COLUMN]
    return _ArpOperand(CohortDelivery(path, CohortGrain(grain), cells), dimensions)


def answer_arp_rate(request: MeasurementToolRequest, tooling: MeasurementTooling) -> ShapedResponse:
    """Answer an ARP rate per cohort cell for one vintage (D-8 (b); owner decision 2026-10-12).

    Chat answers every rate the Retention bake computes. The two operands are planned and bound on
    their own paths for one vintage: pack 15's repeat measure on `arp`, pack 14's account-ID
    cohort size on `standard`. Each cell's rate is `arp_cell_rate`, the rule the bake reads,
    under pack 15's own count of the cohort (`arp_cohort_checks`). A cell the check withholds
    carries its reason in `withheld_reason` and no figure; every other cell is answered. Without a
    paid delivery for the vintage only this rate is refused; standard-path figures never bind the
    paid pack.
    """
    metric = request.catalog.metric(request.metric_name)
    if metric is None:  # pragma: no cover - `_is_arp_rate` resolved it
        raise ValidationError(code=BAD_REQUEST, description=f"{request.metric_name} is unknown.")
    if request.tool is not MeasurementTool.METRIC_BY_GRAIN:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.NOT_YET_COMPUTABLE,
            explanation=(
                f"{metric.name} is answered per cohort cell for one vintage, so ask it by "
                "cohort, follow-up month and acquisition group; name another vintage with "
                "as_of_month."
            ),
            offer_instead=(metric.name,),
        ).to_error()
    if COHORT_PATH_COLUMN in request.filters:
        raise ValidationError(
            code=BAD_REQUEST,
            description=f"{metric.name} reads both cohort paths, so it takes no cohort_path.",
        )
    if MARKETPLACE_COLUMN not in request.filters:
        # Pack 15's own cohort counts are keyed by cohort and group inside one marketplace.
        raise ValidationError(
            code=BAD_REQUEST,
            description=f"{metric.name} reads one marketplace's deliveries; name its marketplace.",
        )
    contracts = (_cohort_contract(request, STANDARD_PATH), _cohort_contract(request, ARP_PATH))
    period, first_quarterly = _arp_vintage(request, tooling, contracts)
    base = {name: value for name, value in request.filters.items() if name != AS_OF_MONTH_COLUMN}

    vintage = period.as_of_month.isoformat()

    def operand(name: str, path: str) -> MeasurementToolRequest:
        filters = {**base, COHORT_PATH_COLUMN: path, AS_OF_MONTH_COLUMN: vintage}
        return _at_served_period(replace(request, metric_name=name, filters=filters), tooling)

    asked = (operand(metric.denominator, STANDARD_PATH), operand(metric.numerator, ARP_PATH))
    bound = bind_batch(prepare_batch(asked), tooling.publications)
    sizes, repeats = bound.selection.plans
    check_cross_delivery(
        CrossDeliveryCheck(
            metric.name,
            CrossDeliveryOperation.DIVIDE,
            DeliveryOperand(
                contracts[1].contract_id, repeats.logical.filters.get(GRAIN_SET_COLUMN, ""), period
            ),
            DeliveryOperand(
                contracts[0].contract_id, sizes.logical.filters.get(GRAIN_SET_COLUMN, ""), period
            ),
            exception=metric.cross_delivery,
        )
    )
    results = _execute_queries(
        tuple(
            QueryRequest(plan=item, max_rows=request.max_rows, ceiling=request.row_ceiling)
            for item in (sizes, repeats)
        ),
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    sizes_result, repeats_result = results
    if sizes_result is None or repeats_result is None:  # pragma: no cover - both plans are bound
        raise AssertionError("a bound ARP operand returned no result")
    standard = _arp_operand(sizes.logical, sizes_result, measure_name=metric.denominator)
    repeats_read = _arp_operand(repeats.logical, repeats_result, measure_name=metric.numerator)
    checks = arp_cohort_checks(repeats.all_publications, contracts[1].contract_id)
    paid = replace(repeats_read, delivery=replace(repeats_read.delivery, checks=checks))
    return _arp_answer(
        _ArpAnswerRequest(
            request=request,
            tooling=tooling,
            metric=metric,
            operands=(standard, paid),
            results=(sizes_result, repeats_result),
            bound=bound,
            first_quarterly=first_quarterly,
            period_note=asked[1].period_note,
            contract_ids=tuple(sorted(contract.contract_id for contract in contracts)),
        )
    )


def _arp_slot(request: MeasurementToolRequest, tooling: MeasurementTooling) -> MeasurementSlot:
    """Answer an ARP rate in its batch slot, keeping its typed refusal to that slot alone.

    An invariant or dependency failure isn't a refusal of this request, so it still raises.
    """
    try:
        return answer_arp_rate(request, tooling)
    except (DomainError, ValidationError) as refused:
        return refused


_PROFILE_SEGMENT_OPERANDS = ("segment_exposed_shoppers", "segment_buyers")
"""A C2 ratio's per-segment operands: the reached shoppers and the buyers of one segment row."""
_PROFILE_TOTAL_OPERANDS = ("profiled_exposed_total", "profiled_buyers_total")
"""Its delivery's bases, read from the one `profiled_total` row."""
_PROFILE_FIGURES = {"segment_buyers": "buyer_share", "segment_exposed_shoppers": "exposed_share"}
"""Which `SegmentFigures` field a share reads, by its numerator; an index divides two shares."""


def _is_profile_ratio(request: MeasurementToolRequest) -> bool:
    """Whether the question asks a C2 share or index, which pairs two rows of one delivery."""
    metric = request.catalog.metric(request.metric_name)
    return metric is not None and metric.delivery_ratio is DeliveryRatio.SEGMENT_PROFILE


def _profile_slot(request: MeasurementToolRequest, tooling: MeasurementTooling) -> MeasurementSlot:
    """Answer a C2 ratio in its batch slot, keeping its typed refusal to that slot alone."""
    try:
        return answer_segment_profile_ratio(request, tooling)
    except (DomainError, ValidationError) as refused:
        return refused


def _profile_figure(metric: Metric, figures: SegmentFigures) -> Decimal | None:
    field_name = _PROFILE_FIGURES.get(metric.numerator or "", "index")
    value = getattr(figures, field_name)
    return None if value is None else Decimal(value)


def answer_segment_profile_ratio(
    request: MeasurementToolRequest, tooling: MeasurementTooling
) -> ShapedResponse:
    """Answer a segment's buyer share, reached share or index for one month and profile path.

    Chat answers every figure the Audiences bake computes (design §6.5). The segment rows'
    reached shoppers and buyers are planned by segment and graded against the path's segment
    roster; the delivery's two bases are planned from its `profiled_total` row; all four are bound
    in one selection and paired per segment by `segment_figures`, the rule the bake reads. A
    delivery whose total row is missing answers every segment "not available" with its reason.
    Asked across segments (a sum of shares) or without one profile path, it's refused.
    """
    metric = request.catalog.metric(request.metric_name)
    if metric is None:  # pragma: no cover - `_is_profile_ratio` resolved it
        raise ValidationError(code=BAD_REQUEST, description=f"{request.metric_name} is unknown.")
    if request.tool is not MeasurementTool.METRIC_BY_GRAIN or set(request.grains) - {
        "marketplace"
    } != {"audience_segment"}:
        raise Refusal(
            requested=metric.name,
            reason=RefusalReason.GRAIN_NOT_SUPPORTED,
            explanation=(
                f"{metric.name} is read per audience segment inside one month's delivery. One "
                "shopper is in many segments, so shares never add up and no total share or index "
                "exists; ask it by audience segment."
            ),
            offer_instead=("audience_segment",),
        ).to_error()
    if MARKETPLACE_COLUMN not in request.filters:
        raise ValidationError(
            code=BAD_REQUEST,
            description=f"{metric.name} reads one marketplace's delivery; name its marketplace.",
        )

    def operand(name: str, grains: tuple[str, ...]) -> MeasurementToolRequest:
        asked = replace(request, metric_name=name, grains=grains, prior_window=None)
        return _at_served_period(asked, tooling)

    # The ratio's own selectors are checked on the period it's served at: one profile path,
    # never a series across two (design §6.5).
    pinned = operand(_PROFILE_SEGMENT_OPERANDS[0], ("audience_segment",))
    refusal = request.catalog.refusal_for(metric.name, filters=pinned.filters)
    if refusal is not None:
        raise refusal.to_error()
    segments = tuple(
        attach_roster(operand(name, ("audience_segment",)), tooling.rosters)
        for name in _PROFILE_SEGMENT_OPERANDS
    )
    totals = tuple(operand(name, ("marketplace",)) for name in _PROFILE_TOTAL_OPERANDS)
    asked = (*segments, *totals)
    bound = bind_batch(prepare_batch(asked), tooling.publications)
    results = _execute_queries(
        tuple(
            QueryRequest(plan=item, max_rows=request.max_rows, ceiling=request.row_ceiling)
            for item in bound.selection.plans
        ),
        MeasurementExecutionTooling(
            queries=tooling.queries,
            query_batch_max_concurrency=tooling.query_batch_max_concurrency,
        ),
    )
    plans = tuple(item.logical for item in bound.selection.plans)
    read: list[dict[str, Decimal]] = []
    for plan_, result in zip(plans, results, strict=True):
        if result is None:  # pragma: no cover - every operand plan is bound
            raise AssertionError("a bound C2 operand returned no result")
        read.append(
            {
                str(dict(figure.dimensions).get(SEGMENT_ID_COLUMN, "")): Decimal(figure.value)
                for figure in measure(plan_, result.rows).figures
            }
        )
    exposed, buyers, exposed_total, buyers_total = read
    path = request.filters.get(PROFILE_PATH_COLUMN, "")
    namespace = plans[0].filters.get(SEGMENT_NAMESPACE_COLUMN)
    if namespace is None:  # pragma: no cover - `_at_served_period` pins a path's one namespace
        raise InvariantError(
            SELECTION_MISMATCH,
            "A C2 operand was planned without its segment namespace.",
            context={"metric": metric.name},
        )
    totals_read = tuple(next(iter(total.values()), None) for total in (exposed_total, buyers_total))
    # A segment one operand carries and the other lacks had no measured zero (a not_returned
    # cell yields no figure): it answers not available, never a share of 0.
    partial = sorted(set(exposed) ^ set(buyers))
    delivery = ProfileDelivery(
        path,
        SegmentNamespace(namespace),
        tuple(
            ProfileSegment(key, int(value), int(buyers[key]), 0)
            for key, value in sorted(exposed.items())
            if key in buyers
        ),
        *(
            (None, None)
            if None in totals_read
            else tuple(int(value) for value in totals_read if value is not None)
        ),
    )
    rows: list[dict[str, str | int | Decimal | None]] = []
    withheld: list[dict[str, str | int | Decimal | None]] = []
    for segment in delivery.segments:
        figures = segment_figures(delivery, segment.segment_id)
        row: dict[str, str | int | Decimal | None] = {SEGMENT_ID_COLUMN: segment.segment_id}
        if figures.reason is not None:
            withheld.append({**row, MEASURE_ALIAS: None, WITHHELD_REASON_FIELD: figures.reason})
            continue
        rows.append({**row, measure_alias(0): _profile_figure(metric, figures)})
    withheld.extend(
        {SEGMENT_ID_COLUMN: key, MEASURE_ALIAS: None, WITHHELD_REASON_FIELD: SEGMENT_PARTIAL_TEXT}
        for key in partial
    )
    head = bound.selection.plans[0].logical
    shaped = replace(
        head,
        metric=metric,
        source_datasets=tuple(
            dict.fromkeys(item for plan_ in plans for item in plan_.source_datasets)
        ),
        facts_read=request.catalog.facts_read(metric),
        measurement_basis=metric.measurement_basis,
        units=metric.unit_kind,
        not_returned_columns=(head.measures[0],),
        partially_written_columns=(),
        suppression_columns=(),
        carried_columns=(),
        semantic_qualifiers=(),
    )
    resolver = tooling.labels
    labels = (
        DimensionDisplayLabels()
        if resolver is None
        else resolver.resolve(
            rows=(*rows, *withheld),
            columns=shaped.grain.dimensions,
            window=shaped.window,
            fact=shaped.fact,
            marketplace=request.filters.get(MARKETPLACE_COLUMN),
            namespace=namespace,
        )
    )
    answer = answer_measurement(
        AnswerRequest(
            plan=shaped,
            context=request.context,
            rows=tuple(rows),
            suppression=Suppression(rows_suppressed=False),
            coverage=bound.selection.coverage,
            applied_defaults=bound.prepared.operations[0].applied,
            additive=False,
            truncated=any(result is not None and result.truncated for result in results),
            contract_ids=tuple(
                sorted(
                    {
                        contract.contract_id
                        for contract in request.contributing_contracts
                        if contract.target_table == head.fact
                        and (stamped := contract.field_for(PROFILE_PATH_COLUMN)) is not None
                        and stamped.literal_value == path
                    }
                )
            ),
            qualified_dq=bound.selection.qualified_dq,
            display_labels=labels,
            period_note=asked[0].period_note,
        )
    )
    return _with_withheld(answer, metric, labels.project(tuple(withheld)))


@dataclass(frozen=True, slots=True)
class _ArpAnswerRequest:
    request: MeasurementToolRequest
    tooling: MeasurementTooling
    metric: Metric
    operands: tuple[_ArpOperand, _ArpOperand]
    """The standard path's cohort sizes, then the paid path's repeats."""
    results: tuple[QueryResult, QueryResult]
    bound: BoundMeasurementBatch
    first_quarterly: str
    period_note: str
    contract_ids: tuple[str, ...]


def _arp_answer(item: _ArpAnswerRequest) -> ShapedResponse:
    """Shape the ARP cells: a rate per cell, not available, or withheld with its reason."""
    metric, (standard, paid) = item.metric, item.operands
    sizes_plan, repeats_plan = (plan.logical for plan in item.bound.selection.plans)
    rows: list[dict[str, str | int | Decimal | None]] = []
    withheld: list[dict[str, str | int | Decimal | None]] = []
    for key in sorted({*standard.delivery.cells, *paid.delivery.cells}):
        figure = arp_cell_rate(
            standard.delivery,
            paid.delivery,
            key,
            numerator=metric.numerator,
            first_quarterly=item.first_quarterly,
        )
        row: dict[str, str | int | Decimal | None] = dict(
            standard.dimensions.get(key) or paid.dimensions[key]
        )
        if isinstance(figure, str):
            withheld.append({**row, MEASURE_ALIAS: None, WITHHELD_REASON_FIELD: figure})
            continue
        rows.append({**row, measure_alias(0): None if figure is None else figure.value})
    plan_ = replace(
        repeats_plan,
        metric=metric,
        filters={
            name: value
            for name, value in repeats_plan.filters.items()
            if name != COHORT_PATH_COLUMN
        },
        source_datasets=tuple(
            dict.fromkeys((*repeats_plan.source_datasets, *sizes_plan.source_datasets))
        ),
        facts_read=item.request.catalog.facts_read(metric),
        measurement_basis=metric.measurement_basis,
        units=metric.unit_kind,
        not_returned_columns=(repeats_plan.measures[0],),
        partially_written_columns=(),
        suppression_columns=(),
        carried_columns=(),
        semantic_qualifiers=(),
    )
    sizes_result, repeats_result = item.results
    resolver = item.tooling.labels
    labels = (
        DimensionDisplayLabels()
        if resolver is None
        else resolver.resolve(
            rows=(*rows, *withheld),
            columns=plan_.grain.dimensions,
            window=plan_.window,
            fact=plan_.fact,
            marketplace=item.request.filters.get(MARKETPLACE_COLUMN),
        )
    )
    answer = answer_measurement(
        AnswerRequest(
            plan=plan_,
            context=item.request.context,
            rows=tuple(rows),
            suppression=next(
                (
                    result.suppression
                    for result in (repeats_result, sizes_result)
                    if result.suppression.rows_suppressed
                ),
                Suppression(rows_suppressed=False),
            ),
            coverage=item.bound.selection.coverage,
            applied_defaults=item.bound.prepared.operations[1].applied,
            additive=False,
            truncated=repeats_result.truncated or sizes_result.truncated,
            contract_ids=item.contract_ids,
            qualified_dq=item.bound.selection.qualified_dq,
            display_labels=labels,
            period_note=item.period_note,
        )
    )
    return _with_withheld(answer, metric, labels.project(tuple(withheld)))


def _with_withheld(
    answer: ShapedResponse,
    metric: Metric,
    withheld: tuple[dict[str, str | int | Decimal | None], ...],
) -> ShapedResponse:
    """Add the withheld cells as rows with their reason and no figure, and say why after line one.

    Each reason is the domain's exact text ("cohort sizes changed between runs", or "Cohort grain
    changed in {period}."), so a withheld rate is never read as a measured one nor as missing.
    """
    values = tuple({**row, WITHHELD_REASON_FIELD: None} for row in answer.data.values)
    if not withheld:
        return replace(
            answer,
            data=replace(answer.data, values=values),
            metadata=replace(answer.metadata, grounding=None),
        )
    reasons: dict[str, int] = {}
    for row in withheld:
        reason = str(row[WITHHELD_REASON_FIELD])
        reasons[reason] = reasons.get(reason, 0) + 1
    lines = tuple(
        f"{metric.label} is withheld for {count} cell(s): {reason.rstrip('.')}."
        for reason, count in reasons.items()
    )
    units = answer.data.units or Units(kind=UnitKind.RATIO)
    claims = answer.data.claim_fields or (ClaimField(MEASURE_ALIAS, units, label=metric.label),)
    narrative = (answer.narrative[0], *lines, *answer.narrative[1:])
    return ShapedResponse(
        data=replace(answer.data, values=(*values, *withheld), units=units, claim_fields=claims),
        narrative=narrative,
        metadata=replace(answer.metadata, row_count=len(values) + len(withheld), grounding=None),
        recommendations=answer.recommendations,
    )


@dataclass(frozen=True, slots=True)
class _RowKindRequest:
    """A periodic question, the contracts it reads, their served series and its two periods."""

    request: MeasurementToolRequest
    metric: Metric
    contracts: tuple[DataContract, ...]
    served: ServedPeriods
    periods: tuple[CalendarPeriod, DateRange | None]


def _served_series_pins(item: _RowKindRequest) -> dict[str, str]:
    """Pin the rows of the series the period is served from (plan D9, D11), or nothing.

    A period is served from the one series holding its CURRENT publication: the base pack's
    `combined` rows, or a variant's own (pack 5's touch-only reach on `exposure` rows, outcomes
    on `outcome` rows). Packs 6 and 7 pin their `window` rows and the series' bucket scheme
    (`fine`, or `coarse` once switched on), a grain column no figure sums across. A period held
    by two series, or a comparison across two, is refused with `PACK_VARIANT_MISMATCH`; across
    bucket schemes with D-8 (a)'s "bucket scheme changed in {period}". So is a figure whose
    operands sit on two branches of one variant (a rate on a touch-only period), which no one
    set of rows carries; the Reach page computes it.
    """
    period, prior_window = item.periods
    measures = tuple(
        column for _fact, column in sorted(leaf_references(item.request.catalog, item.metric))
    )
    for contract in item.contracts:
        cells = contract.expected_cells
        if contract.field_for(ROW_KIND_COLUMN) is None or cells is None:
            continue
        try:
            variant = active_variant(
                item.served.series_of(contract.contract_id, period), period.label
            )
            if prior_window is not None:
                prior = CalendarPeriod(prior_window.start, prior_window.end, period.grain)
                before = active_variant(
                    item.served.series_of(contract.contract_id, prior), prior.label
                )
                _require_same_bucket_scheme(item, contract, ((before, prior), (variant, period)))
                require_same_variant(variant, before, prior.label)
        except VariantMismatchError as error:
            raise ValidationError(
                code=VARIANT_MISMATCH,
                description=str(error),
                context={"finding_code": PACK_VARIANT_MISMATCH, "metric": item.metric.name},
            ) from None
        kind = cells.value_for(variant, ROW_KIND_COLUMN, measures)
        if kind is None:
            raise ValidationError(
                code=VARIANT_MISMATCH,
                description=(
                    f"{PACK_VARIANT_MISMATCH}: {period.label} was measured by variant "
                    f"{variant}, which returns {item.metric.name}'s figures on two kinds of "
                    "rows, so chat can't compute it. Ask for each figure, or open Reach and "
                    "funnel."
                ),
                context={"finding_code": PACK_VARIANT_MISMATCH, "metric": item.metric.name},
            )
        scheme = cells.value_for(variant, BUCKET_SCHEME_COLUMN, ())
        pins = {ROW_KIND_COLUMN: kind}
        if contract.field_for(BUCKET_SCHEME_COLUMN) is not None and scheme is not None:
            pins[BUCKET_SCHEME_COLUMN] = scheme
        return pins
    return {}


def _require_same_bucket_scheme(
    item: _RowKindRequest,
    contract: DataContract,
    compared: tuple[tuple[str | None, CalendarPeriod], tuple[str | None, CalendarPeriod]],
) -> None:
    """Refuse a comparison across frequency bucket schemes with D-8 (a)'s own reason.

    Packs 6 and 7 switch from the fine to the coarse scheme per marketplace (plan D10), and the
    scheme is in the option set, so two periods of one contract may sit in two schemes. Their
    buckets don't line up, so the comparison is refused: "bucket scheme changed in {period}".
    """
    cells = contract.expected_cells
    if contract.field_for(BUCKET_SCHEME_COLUMN) is None or cells is None:
        return
    grain_set = str(item.request.filters.get(GRAIN_SET_COLUMN, ""))
    first, second = (
        DeliveryOperand(
            contract.contract_id,
            grain_set,
            period,
            cells.value_for(variant, BUCKET_SCHEME_COLUMN, ()),
        )
        for variant, period in compared
    )
    try:
        check_cross_delivery(
            CrossDeliveryCheck(item.metric.name, CrossDeliveryOperation.COMPARE, first, second)
        )
    except DomainError as error:
        raise ValidationError(
            code=VARIANT_MISMATCH,
            description=f"{PACK_VARIANT_MISMATCH}: {error.description}",
            context={
                "finding_code": PACK_VARIANT_MISMATCH,
                "metric": item.metric.name,
                "reason": str(error.context.get("reason")),
            },
        ) from None


def _require_exact_bound_plans(
    expected: tuple[QueryPlan, ...],
    bound: tuple[BoundQueryPlan, ...],
) -> None:
    """Refuse a selector that changed any prepared plan, position, or count."""
    selected = tuple(item.logical for item in bound)
    if selected != expected:
        raise InvariantError(
            SELECTION_MISMATCH,
            "Publication selection changed the requested logical plan sequence.",
            context={
                "requested_plans": len(expected),
                "selected_plans": len(selected),
            },
        )


def _require_exact_inspection(
    expected: tuple[PreparedMeasurementPlan, ...],
    inspection: PresentationEvidenceInspectionResult,
) -> None:
    """Validate all inspected identities before any present plan can execute."""
    if len(inspection.slots) != len(expected):
        raise InvariantError(
            SELECTION_MISMATCH,
            "Presentation evidence inspection changed the prepared plan sequence.",
            context={
                "requested_plans": len(expected),
                "selected_plans": len(inspection.slots),
            },
        )
    for prepared, inspected in zip(expected, inspection.slots, strict=True):
        if inspected.role != prepared.role or (
            inspected.bound is not None and inspected.bound.logical != prepared.logical
        ):
            raise InvariantError(
                SELECTION_MISMATCH,
                "Presentation evidence inspection changed a prepared plan or role.",
                context={
                    "requested_plans": len(expected),
                    "selected_plans": len(inspection.slots),
                },
            )


def _answer(operation: _Operation) -> ShapedResponse:
    request = operation.request
    match request.tool:
        case MeasurementTool.METRIC_BY_GRAIN:
            return _breakdown(operation)
        case MeasurementTool.COMPARE_WINDOWS:
            return _comparison(operation)
        case MeasurementTool.TOP_N:
            return _ranking(operation)


@dataclass(frozen=True, slots=True)
class _Operation:
    """One exactly bound invocation ready to query and shape."""

    request: MeasurementToolRequest
    tooling: MeasurementExecutionTooling
    applied: dict[str, str]
    """The lens columns the tool pinned. Computed once and shared by both windows, so a comparison
    cannot pin one population in the later period and a different one in the earlier."""

    current: QueryPlan
    bound_current: BoundQueryPlan
    selection: PublicationSelection
    prior: QueryPlan | None = None
    bound_prior: BoundQueryPlan | None = None
    current_result: QueryResult | None = None
    prior_result: QueryResult | None = None

    @property
    def coverage(self) -> Coverage:
        """What is loaded behind every fact this answer reads."""
        return self.selection.coverage

    @property
    def contract_ids(self) -> tuple[str, ...]:
        """Name the contracts behind the figures, sorted and deduplicated.

        Sorted rather than left to arrive in load order. The order `load_contracts` happens to
        yield carries no meaning, so two identical answers could differ in this field alone and
        read as different provenance.
        """
        return tuple(
            sorted({contract.contract_id for contract in self.request.contributing_contracts})
        )

    @property
    def qualified_dq(self) -> tuple[str, ...]:
        """Caveats from current successful publications behind every plan in this answer."""
        return self.selection.qualified_dq

    @property
    def additive(self) -> bool:
        """Whether summing this metric across rows is arithmetic rather than nonsense.

        Two independent reasons it is not, and `is_non_additive` answers only the first.

        **A distinct count.** Read from the dictionary through a ratio's operands, so
        `avg_frequency` inherits it from `reach_users` without declaring it. A metric definition
        never belongs in code, which is why this is resolved rather than decided.

        **A ratio, whatever it is denominated in.** Adding two quotients is not a quotient, and the
        units alone do not say so: `asp_shipped` and `revenue_per_glance_view` declare **currency**,
        so they passed `total_of`'s unit-kind gate and a top-N over them stated a grand total that
        was a sum of average selling prices, labelled as covering the whole population (G-46).
        `is_non_additive` cannot carry this, and asking it to would be wrong rather than merely
        convenient: `_refuse_aggregating_a_distinct_count` uses the same predicate to refuse a
        coarser grain, and a ratio at a coarser grain is recomputed from re-aggregated operands,
        which is correct.
        The two questions are "does this contain a distinct count" and "does this sum". Only the
        second belongs here.
        """
        metric = self.current.metric
        if metric.kind is not MetricKind.SIMPLE:
            return False
        return not self.request.catalog.is_non_additive(metric)

    @property
    def exact_only_columns(self) -> tuple[str, ...]:
        """The grain columns the metric's aggregation policy forbids adding across.

        Read from the dictionary (`exact_only_grain_columns`): a breakdown of `segment_impressions`
        by segment has no total, because one impression repeats once per matched segment (D-4).
        """
        return exact_only_grain_columns(self.current.metric, self.request.grains)


def _plan_requests(
    request: MeasurementToolRequest, applied: Mapping[str, str]
) -> tuple[PlanRequest, PlanRequest | None]:
    """Build one plan request per window, identical in everything but the period.

    That identity is what makes a comparison a comparison rather than two questions: the same
    metric, the same grain and the same filters, differing only in the period — so a change
    between them is attributable to the period and to nothing else.
    """
    shape = request.shape
    shapes = (shape.numerator, shape.denominator) if isinstance(shape, FactShapes) else (shape,)
    grain_columns = {column for side in shapes for column in side.grain_columns}
    # A source option such as Vendor report_period selects the immutable publication lens but is
    # not necessarily a row-grain column on both sides of a drill-across. It remains in `applied`
    # for disclosure and scope reconstruction; only grain defaults become SQL filters.
    planned_defaults = {key: value for key, value in applied.items() if key in grain_columns}
    filters = {**request.filters, **planned_defaults}

    def _over(window: DateRange) -> PlanRequest:
        return PlanRequest(
            metric_name=request.metric_name,
            grains=request.grains,
            window=window,
            filters=filters,
            pack_coverage=request.pack_coverage,
            roster=next((item for item in request.rosters if item.period == window), None),
        )

    return _over(request.window), (
        _over(request.prior_window) if request.prior_window is not None else None
    )


def _breakdown(operation: _Operation) -> ShapedResponse:
    """Report one metric at one grain over one window.

    The one operation where truncation is a caveat rather than a refusal, because each row stands
    alone: a page of a breakdown is a page of figures that are individually correct, and the
    narrative says a total over them would be short.
    """
    request = operation.request
    built = operation.current
    result = _query_result(operation, operation.bound_current)
    return answer_measurement(
        AnswerRequest(
            plan=built,
            context=request.context,
            rows=result.rows,
            suppression=result.suppression,
            coverage=operation.coverage,
            applied_defaults=operation.applied,
            additive=operation.additive,
            truncated=result.truncated,
            exact_only_columns=operation.exact_only_columns,
            contract_ids=operation.contract_ids,
            qualified_dq=operation.qualified_dq,
            display_labels=_display_labels(operation, result.rows),
            period_note=request.period_note,
        )
    )


def _comparison(operation: _Operation) -> ShapedResponse:
    """Report the same metric over two periods, with the change between them."""
    prior = operation.prior
    if prior is None:  # pragma: no cover — refused by MeasurementToolRequest.__post_init__
        raise ValidationError(code=BAD_REQUEST, description="A comparison needs a prior window.")

    later, later_figures = _whole_population(operation, operation.bound_current)
    if operation.bound_prior is None:  # pragma: no cover - paired with prior above
        raise ValidationError(code=BAD_REQUEST, description="A comparison needs a bound prior.")
    earlier, earlier_figures = _whole_population(operation, operation.bound_prior)

    return answer_comparison(
        ComparisonAnswerRequest(
            comparison=compare(later_figures, earlier_figures),
            plan=operation.current,
            prior_window=prior.window,
            context=operation.request.context,
            coverage=operation.coverage,
            suppression=_either_period(later.suppression, earlier.suppression),
            applied_defaults=operation.applied,
            additive=operation.additive,
            exact_only_columns=operation.exact_only_columns,
            contract_ids=operation.contract_ids,
            qualified_dq=operation.qualified_dq,
            display_labels=_display_labels(operation, (*later.rows, *earlier.rows)),
            roster_note=_roster_note(operation.current, later_figures),
        )
    )


def _ranking(operation: _Operation) -> ShapedResponse:
    """Report the largest n at a grain, with the total the rest are judged against."""
    request = operation.request
    later, population = _whole_population(operation, operation.bound_current)
    suppression = later.suppression
    comparison = None

    if operation.bound_prior is not None:
        earlier, earlier_figures = _whole_population(operation, operation.bound_prior)
        comparison = compare(population, earlier_figures)
        suppression = _either_period(suppression, earlier.suppression)

    # Rows differing along an exact-only axis have no total and no subtotal of the shown ones.
    across = population.varying(operation.exact_only_columns) if operation.additive else ()
    additive = operation.additive and not across
    return answer_ranking(
        RankingAnswerRequest(
            ranking=rank(
                RankRequest(
                    measurement=population,
                    n=request.n,
                    additive=additive,
                    order_by=request.order_by,
                    comparison=comparison,
                )
            ),
            plan=operation.current,
            context=request.context,
            coverage=operation.coverage,
            suppression=suppression,
            applied_defaults=operation.applied,
            additive=additive,
            exact_only_across=across,
            prior_window=operation.prior.window if operation.prior is not None else None,
            contract_ids=operation.contract_ids,
            qualified_dq=operation.qualified_dq,
            display_labels=_display_labels(operation, later.rows),
            roster_note=_roster_note(operation.current, population),
        )
    )


def _roster_note(current: QueryPlan, population: Measurement) -> tuple[str, ...]:
    """Name the current period's rostered keys with no figure, as a breakdown does.

    A withdrawn grain set offers none of its figures (design §4.4), so a comparison or ranking
    over one is refused with the pointer; a breakdown names each rostered key instead.
    """
    outcome = plan_roster_outcome(current, population)
    if outcome is not None and outcome.withdrawn:
        raise ValidationError(
            code=ROSTER_WITHDRAWN,
            description=(
                f"{current.metric.name} per {outcome.roster.kind.value.replace('_', ' ')} isn't "
                f"offered for {current.window.start} to {current.window.end}: {outcome.note}."
            ),
            context={"metric": current.metric.name, "roster": outcome.roster.kind.value},
        )
    return () if outcome is None else roster_lines(outcome)


def _query_result(operation: _Operation, bound: BoundQueryPlan) -> QueryResult:
    """Return a pre-executed batch result, with a direct-call fallback for isolated use."""
    if bound is operation.bound_current and operation.current_result is not None:
        return operation.current_result
    if bound is operation.bound_prior and operation.prior_result is not None:
        return operation.prior_result
    return operation.tooling.queries.run(
        QueryRequest(
            plan=bound, max_rows=operation.request.max_rows, ceiling=operation.request.row_ceiling
        )
    )


def _whole_population(
    operation: _Operation, built: BoundQueryPlan
) -> tuple[QueryResult, Measurement]:
    """Run one query, refusing a result that did not read every row.

    **Truncation is a refusal here, not a caveat, and the difference is what the operation does
    with the rows.** A breakdown reports each row on its own, so a page of them is honest. A
    comparison pairs rows across two result sets and a ranking orders them — both are claims about
    the *whole* population, and neither query orders its own output. So a truncated read gives an
    arbitrary subset: a key present in one page and absent from the other reads as a period with no
    figure when it is really a period this answer did not look at, and a grand total over a page is
    short by an amount nobody can state. The tool steering's rule is the same one — aggregate to a
    coarser grain or refuse, never paginate — and this is the operation where it binds.
    """
    request = operation.request
    logical = built.logical
    result = _query_result(operation, built)
    if result.truncated:
        raise DomainError(
            RESULT_TOO_WIDE,
            (
                f"{logical.metric.label} at this grain returns more than {request.max_rows} rows "
                f"for {logical.window}, and this question is answered over the whole population "
                "rather than a page of it. Narrow the filters, ask at a coarser grain, or raise "
                "max_rows."
            ),
            context={
                "metric": logical.metric.name,
                "grain": str(logical.grain),
                "max_rows": request.max_rows,
            },
        )
    return result, measure(logical, result.rows)


def _display_labels(
    operation: _Operation,
    rows: tuple[dict[str, str | int | Decimal | None], ...],
) -> DimensionDisplayLabels:
    """Resolve governed display labels for the dimension values this answer will release.

    Resolved from the executed result's own rows, after the query and before the answer is
    built, so the labels are exactly the values the released rows carry and the model never
    fetches them through a second call it could skip or mispair.
    """
    resolver = operation.tooling.labels
    if resolver is None:
        return DimensionDisplayLabels()
    return resolver.resolve(
        rows=rows,
        columns=operation.current.grain.dimensions,
        window=operation.request.window,
        fact=operation.current.fact,
        marketplace=operation.request.filters.get(MARKETPLACE_COLUMN),
        namespace=operation.request.filters.get(SEGMENT_NAMESPACE_COLUMN),
    )


def _either_period(later: Suppression, earlier: Suppression) -> Suppression:
    """Report a withholding in either period, because either one distorts the change.

    Not the later period's alone. A key whose earlier figure Amazon withheld has an unknown change
    however complete the later period is, and an answer that reported no suppression because the
    recent side was clean would present that change as measured.
    """
    if later.rows_suppressed and earlier.rows_suppressed:
        return Suppression(
            rows_suppressed=True,
            reason=f"{later.reason} The earlier period is also affected: {earlier.reason}",
        )
    if later.rows_suppressed:
        return later
    if earlier.rows_suppressed:
        return Suppression(rows_suppressed=True, reason=f"In the earlier period: {earlier.reason}")
    return Suppression(rows_suppressed=False)


def _lens_defaults(
    request: MeasurementToolRequest, shape: FactShape | FactShapes
) -> dict[str, str]:
    """Pin every publisher-owned reporting lens the caller left open.

    Canonical grain columns catch lenses that change row uniqueness. Source report options also
    belong here even when the option is not stored in that grain: it still changes which immutable
    publication the selector admits. Omitting it would silently select (for example) a DAILY Vendor
    publication while the answer disclosed only distributor and selling-program defaults.
    """
    grouped = tuple(GRAIN_COLUMNS[name] for name in request.grains if name in GRAIN_COLUMNS)
    accounted = {*grouped, *request.filters, *request.defaulted_filters, DATE_COLUMN}
    # Both facts of a drill-across, because each carries its own grain and either may leave a
    # lens column open. Pinning only the numerator's would let the denominator sum across a
    # reporting view — `conversion_rate_retail` divides a sales fact that has a distributor view
    # by a traffic fact that has none, so the sides genuinely differ in what needs pinning.
    shapes = (shape.numerator, shape.denominator) if isinstance(shape, FactShapes) else (shape,)
    grain_lenses = tuple(
        column for side in shapes for column in side.grain_columns if column not in accounted
    )
    source_option_lenses = tuple(
        field.target_column
        for contract in request.contributing_contracts
        for field in contract.fields
        if ".reportOptions." in field.source_path and field.target_column not in accounted
    )
    discovered = defaults_for(tuple(dict.fromkeys((*grain_lenses, *source_option_lenses))))
    return {**request.defaulted_filters, **discovered}
