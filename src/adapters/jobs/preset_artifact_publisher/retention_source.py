# SPDX-License-Identifier: MIT-0
"""Bake `RetentionCohortArtifact` v1 for the latest cohort vintage (docs/ui/retention, ADR-075).

Per governed marketplace, every per-cell figure is planned through the measurement layer: pack
14's cohort sizes (all IDs and Amazon account IDs), repeat buyers and cumulative account-ID repeat
buyers on the standard path, and pack 15's repeat and cumulative repeat buyers on the paid path,
grouped by cohort, follow-up month and acquisition channel group, for the vintage (`as_of_month`,
13 calendar months). Nothing is planned across cells or paths; the domain computes every rate,
the D-3 curve and the ARP rate (`domain.presentation.retention`), so a `not_returned` cell reads
"Not available" rather than a share of the cells that came back.

The ARP rate's cohort-size check reads pack 15's own count of each cohort,
`cohort_buyers_account_ids_check`, from the cell-loss evidence of the publication the paid plan
bound (`arp_cohort_checks`): an integrity count, never a dictionary metric, so it's never served
as a figure (design §6.6, review DS-13). Chat answers the same ARP rates through the same domain
rule (`arp_cell_rate`, owner decision 2026-10-12).

Each vintage is read from the one series holding its CURRENT publication: monthly cohorts, or the
quarterly variant once switched on (plan D10). A vintage two series hold bakes nothing.

Registered with the publisher, and fails closed: the source composes only once the v2 page entry
`retention` declares a vintage policy, `RetentionCohortArtifact` v1 and the sections this bake
fills (the UI workstream adds it, as `docs/ui/retention/low-level-design.md` declares it). It
bakes the policy's default variant, `latest_with_current_publication`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    execute_presentation_batch,
    prepare_batch,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import capped_preset_artifact_json
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    MARKETPLACE_SELECTOR,
    GroupedAnswer,
    grouped_answer_complete,
    parse_grouped_answer,
)
from adapters.outbound.bake_sources import source_closed
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PerPackPresentationEvidenceInspection,
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import load_query_packs
from domain.data_contracts.contract import CohortGrain
from domain.measurement.periodic import (
    AS_OF_MONTH_COLUMN,
    COHORT_GRAIN_COLUMN,
    COHORT_PATH_COLUMN,
    VariantMismatchError,
    active_variant,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightContract,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightSlotId,
    OperandBasis,
    ShiftPolicy,
    finding_is_material,
    horizon_role_for,
)
from domain.presentation.page_schemas import RETENTION_CURVE_SECTION
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
)
from domain.presentation.retention import (
    ARP_PATH,
    RETENTION_ARTIFACT_TYPE,
    RETENTION_ARTIFACT_VERSION,
    RETENTION_PAGE_ID,
    STANDARD_PATH,
    CohortDelivery,
    RetentionCohortArtifact,
    RetentionMarket,
    RetentionVintage,
    ShiftFinding,
    ShiftSlotRequest,
    arp_cohort_checks,
    build_market,
    retention_artifact_payload,
    retention_shift_slot,
    retention_shifts,
)
from domain.presentation.scorecard import scorecard_marketplace_label
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import BAKE_MAX_ROWS, RowCeiling
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.page_schemas import (
        PresentationPageSchema,
        PresentationPageSchemaSet,
        PresentationVintagePolicy,
    )
    from domain.semantics.commerce_identity import CommerceIdentityBridge
    from domain.semantics.source_pack import SourcePack
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import (
        PresentationEvidenceInspectionPort,
        PresentationEvidenceInspectionResult,
    )
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

__all__ = [
    "LATEST_VINTAGE_VARIANT",
    "RETENTION_MARKET_MAX_ENCODED_BYTES",
    "RETENTION_MATERIALIZATION_UNAVAILABLE",
    "RETENTION_RENDER_FORMAT",
    "RetainedRetentionPresetArtifactSource",
    "RetentionArtifactRenderer",
    "RetentionCompiler",
    "RetentionRenderInput",
    "RetentionRenderResult",
    "quarterly_vintage_reader",
    "retained_retention_preset_artifact_source",
    "retention_bake_body_bytes",
    "retention_policy",
]

RETENTION_RENDER_FORMAT = 3
"""Generation 3: each market's `marketplace_text`; an older pointer is rebuilt, never kept."""
RETENTION_MARKET_MAX_ENCODED_BYTES = 160_000
"""A ceiling on one marketplace's encoded share of a Retention body, envelope included.

The shape is fixed by validation: every acquisition group, every cohort of the vintage and every
follow-up, on both paths, plus one curve line per group. The monthly triangle (13 cohorts, 12
follow-ups) is the largest; a test bakes it with eight-digit cohort sizes and holds one market
and each added market within this ceiling."""
STANDARD_CONTRACT = "amc-acquisition-cohorts"
ARP_CONTRACT = "amc-acquisition-cohorts-arp"
STANDARD_METRICS = (
    "cohort_buyers_new_to_brand_12mo",
    "cohort_buyers_account_ids",
    "cohort_repeat_buyers",
    "cohort_repeat_buyers_cumulative_account_ids",
)
ARP_METRICS = ("cohort_repeat_buyers", "cohort_repeat_buyers_cumulative")
CELL_GRAINS = ("marketplace", "cohort", "months_since_acquisition", "acquisition_channel_group")
LATEST_VINTAGE_VARIANT = "latest_with_current_publication"
"""The one variant a vintage page bakes: its policy's default (`vintage_policy.default`)."""
REQUIRED_SECTIONS = ("path", "triangle", RETENTION_CURVE_SECTION, "boundary")
_ARTIFACT_ID = "retention"
_CORRELATION_ID = "retention-artifact"
_PERIOD_GRAIN_SELECTOR = "time_grain"
_GRAIN_SET_SELECTOR = "grain_set"
_PLATFORM_GRAIN_SET = "platform"
_SILENT_SHIFT = (
    "No acquisition group's month-1 or month-3 repeat rate moved clearly between its latest two "
    "cohorts."
)
_FINDING_QUANTUM = Decimal("0.01")
_RENDER_INPUTS_IDENTITY_PREFIX = "retention-render-inputs.v1:"
"""Names the digest of every non-measurement input a Retention body renders."""

type _Label = tuple[str, str, str]
"""(marketplace, path, metric)."""


@dataclass(frozen=True, slots=True)
class RetentionRenderInput:
    """One resolved cohort vintage."""

    period: CalendarPeriod


@dataclass(frozen=True, slots=True)
class RetentionRenderResult:
    """The baked artifact, or None, and the publication identities it read."""

    artifact: RetentionCohortArtifact | None
    publication_identities: tuple[str, ...]


class RetentionCompiler(Protocol):
    """Render one vintage from retained truth only."""

    def render_retention(self, request: RetentionRenderInput) -> RetentionRenderResult:
        """Return the artifact for the vintage, or None when it can't be baked whole."""
        ...


@dataclass
class _Batch:
    plans: list[MeasurementToolRequest] = field(default_factory=list[MeasurementToolRequest])
    labels: list[_Label] = field(default_factory=list[_Label])


@dataclass(frozen=True, slots=True)
class _Served:
    """One marketplace's series for the vintage on each path, and its first quarterly vintage."""

    standard: str | VariantMismatchError | None
    paid: str | VariantMismatchError | None
    standard_served: bool
    paid_served: bool
    first_quarterly: date | None


@dataclass(frozen=True, slots=True)
class RetentionArtifactRenderer:
    """Plan packs 14 and 15's per-cell figures per marketplace and reduce them in the domain."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    marketplaces: tuple[str, ...]
    insight_contract: InsightContract
    """`retention-shift` v1."""
    clock: Callable[[], datetime]
    first_quarterly: Callable[[str], date | None]
    """A marketplace's first quarterly vintage (`as_of_month`), from the quarterly series'
    earliest CURRENT publication (`quarterly_vintage_reader`), never a horizon-bounded set."""
    marketplace_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """Each marketplace's short or governed name, which names its sections with two shown."""

    def __post_init__(self) -> None:
        if self.insight_contract.id is not InsightSlotId.RETENTION_SHIFT:
            raise ValueError("Retention renders retention-shift.")
        if not self.marketplaces:
            raise ValueError("Retention bakes at least one governed marketplace.")

    def _contract(self, contract_id: str) -> DataContract:
        return next(item for item in self.contracts if item.contract_id == contract_id)

    def _grain(self, contract_id: str, variant: str | None) -> CohortGrain:
        cells = self._contract(contract_id).expected_cells
        value = None if cells is None else cells.value_for(variant, COHORT_GRAIN_COLUMN, ())
        if value is None:
            raise ValueError(f"{contract_id} declares no cohort grain for {variant!r}.")
        return CohortGrain(value)

    def _served(self, market: str, period: CalendarPeriod) -> _Served:
        reader = self.tooling.periods
        if reader is None:
            raise ValueError("Retention needs the served-period reader.")
        found: dict[str, ServedPeriods] = {
            contract_id: reader.served_periods(
                ServedPeriodsRequest(
                    contracts=(self._contract(contract_id),),
                    filters={MARKETPLACE_SELECTOR: market},
                    now=self.clock(),
                )
            )
            for contract_id in (STANDARD_CONTRACT, ARP_CONTRACT)
        }

        def series(contract_id: str) -> str | VariantMismatchError | None:
            served = found[contract_id]
            try:
                return active_variant(served.series_of(contract_id, period), period.label)
            except VariantMismatchError as error:
                return error

        standard = found[STANDARD_CONTRACT]
        return _Served(
            standard=series(STANDARD_CONTRACT),
            paid=series(ARP_CONTRACT),
            standard_served=(STANDARD_CONTRACT, period) in standard.publications,
            paid_served=(ARP_CONTRACT, period) in found[ARP_CONTRACT].publications,
            first_quarterly=self.first_quarterly(market),
        )

    def _request(  # noqa: PLR0913, PLR0917 -- one cell metric: marketplace, path and vintage
        self, market: str, path: str, metric: str, read: tuple[CalendarPeriod, CohortGrain]
    ) -> MeasurementToolRequest:
        """Plan one cell metric on one path for the vintage, pinned to its series' cohort grain."""
        period, grain = read
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.catalog,
            contracts=self.contracts,
            metric_name=metric,
            grains=CELL_GRAINS,
            window=period.window,
            computed_at=self.clock(),
            correlation_id=_CORRELATION_ID,
            filters={
                MARKETPLACE_SELECTOR: market,
                _PERIOD_GRAIN_SELECTOR: period.grain.value,
                _GRAIN_SET_SELECTOR: _PLATFORM_GRAIN_SET,
                AS_OF_MONTH_COLUMN: period.as_of_month.isoformat(),
                COHORT_PATH_COLUMN: path,
                COHORT_GRAIN_COLUMN: grain.value,
            },
            max_rows=BAKE_MAX_ROWS,
            row_ceiling=RowCeiling.BAKE,
        )

    def render_retention(self, request: RetentionRenderInput) -> RetentionRenderResult:
        """Measure every marketplace's standard and paid cells for the vintage, then reduce them.

        A marketplace whose vintage two series hold bakes nothing. A paid path that hasn't
        delivered for the vintage reads the paid-feature callout.
        """
        period = request.period
        batch = _Batch()
        served: dict[str, _Served] = {}
        for market in self.marketplaces:
            item = self._served(market, period)
            if isinstance(item.standard, VariantMismatchError) or isinstance(
                item.paid, VariantMismatchError
            ):
                return RetentionRenderResult(None, ())
            served[market] = item
            if item.standard_served:
                grain = self._grain(STANDARD_CONTRACT, item.standard)
                for metric in STANDARD_METRICS:
                    batch.plans.append(
                        self._request(market, STANDARD_PATH, metric, (period, grain))
                    )
                    batch.labels.append((market, STANDARD_PATH, metric))
            if item.standard_served and item.paid_served:
                grain = self._grain(ARP_CONTRACT, item.paid)
                for metric in ARP_METRICS:
                    batch.plans.append(self._request(market, ARP_PATH, metric, (period, grain)))
                    batch.labels.append((market, ARP_PATH, metric))
        if not batch.plans:
            return RetentionRenderResult(None, ())
        measured = self._measured(batch, period)
        if measured is None:
            return RetentionRenderResult(None, ())
        answers, checks, measured_identities = measured
        # A marketplace's label and its first quarterly vintage change the body without changing
        # a measured publication, so the revision names them too.
        identities = tuple(sorted({*measured_identities, self._render_inputs_identity(served)}))
        revision = PresetArtifactRevision.from_publication_identities(
            identities, render_format=RETENTION_RENDER_FORMAT
        ).value
        markets = tuple(
            self._market(period, market, served[market], (answers, checks, revision))
            for market in self.marketplaces
        )
        return RetentionRenderResult(RetentionCohortArtifact(period, markets), identities)

    def _measured(
        self, batch: _Batch, period: CalendarPeriod
    ) -> (
        tuple[
            dict[_Label, GroupedAnswer],
            dict[str, dict[tuple[date, str], Decimal]],
            tuple[str, ...],
        ]
        | None
    ):
        prepared = prepare_batch(tuple(batch.plans))
        if len(prepared.plans) != len(batch.labels):
            raise RuntimeError("the Retention batch and its labels must align.")
        inspection = self.evidence.inspect(
            PresentationEvidenceInspectionRequest(
                plans=tuple(
                    PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                    for plan in prepared.plans
                ),
                contracts=prepared.contracts,
                now=prepared.now,
            )
        )
        executed = execute_presentation_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(
                queries=self.tooling.queries,
                query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
            ),
        )
        answers = _parsed(executed.slots, batch, period)
        if answers is None:
            return None
        identities = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        return answers, _checks(inspection, batch), identities

    def _label(self, market: str) -> str:
        return self.marketplace_labels.get(market, market)

    def _render_inputs_identity(self, served: dict[str, _Served]) -> str:
        """Digest every non-measurement input one bake's markets render.

        The label is registry configuration and the first quarterly vintage comes from
        publication history outside the vintage's plans; the series and served flags are digested
        too, so no served-state change can leave a stale body current.
        """
        evidence = {
            market: [
                self._label(market),
                None if item.first_quarterly is None else item.first_quarterly.isoformat(),
                item.standard,
                item.paid,
                item.standard_served,
                item.paid_served,
            ]
            for market, item in sorted(served.items())
        }
        digest = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        return f"{_RENDER_INPUTS_IDENTITY_PREFIX}{digest}"

    def _market(  # noqa: PLR0913, PLR0917 -- the vintage, a marketplace, its series, its answers
        self,
        period: CalendarPeriod,
        market: str,
        served: _Served,
        measured: tuple[
            dict[_Label, GroupedAnswer], dict[str, dict[tuple[date, str], Decimal]], str
        ],
    ) -> RetentionMarket:
        answers, checks, revision = measured
        standard = (
            _delivery(
                answers,
                (market, STANDARD_PATH),
                STANDARD_METRICS,
                self._grain(STANDARD_CONTRACT, cast("str | None", served.standard)),
            )
            if served.standard_served
            else None
        )
        paid = (
            _delivery(
                answers,
                (market, ARP_PATH),
                ARP_METRICS,
                self._grain(ARP_CONTRACT, cast("str | None", served.paid)),
                checks.get(market, {}),
            )
            if served.standard_served and served.paid_served
            else None
        )
        vintage = RetentionVintage(period, market, standard, paid, served.first_quarterly)
        policy = cast("ShiftPolicy", self.insight_contract.shift_policy)
        findings = tuple(
            finding
            for finding in retention_shifts(vintage, policy)
            if self._shift_material(finding, period)
        )
        slot = retention_shift_slot(ShiftSlotRequest(vintage, findings, revision, _SILENT_SHIFT))
        return build_market(vintage, (slot,), self._label(market))

    def _shift_material(self, finding: ShiftFinding, period: CalendarPeriod) -> bool:
        """Whether a named shift clears `retention-shift`'s materiality (rate points)."""
        (bound,) = predicate_findings(
            (
                DeterministicFinding(
                    "Repeat rate change", (finding.points / 100).quantize(Decimal("0.0001"))
                ),
            ),
            InsightFindingKind.ABSOLUTE_CHANGE,
            InsightEvidenceInput.COHORT_CELLS,
            source=FindingSource(
                InsightSlotId.RETENTION_SHIFT, OperandBasis.COHORT_DELIVERY, "repeat_buyer_rate"
            ),
            priors={"Repeat rate change": finding.earlier_rate},
        )
        return finding_is_material(
            self.insight_contract.predicate.materiality,
            bound,
            horizon_role_for(period.window.days),
        )


def _parsed(
    slots: tuple[PresentationMeasurementAnswerSlot, ...], batch: _Batch, period: CalendarPeriod
) -> dict[_Label, GroupedAnswer] | None:
    """Parse every answer; a partial, truncated or mis-pinned one bakes nothing."""
    parsed: dict[_Label, GroupedAnswer] = {}
    for answer, label in zip(slots, batch.labels, strict=True):
        item = parse_grouped_answer(answer, period.window, CELL_GRAINS)
        if item is None or not grouped_answer_complete(item, period.window):
            # Every vintage planned has a CURRENT publication, so anything short of a complete
            # answer is missing evidence: no bake, never an undercount.
            return None
        if any(key[0] != label[0] for key in item.rows):
            return None
        parsed[label] = item
    return parsed


def _checks(
    inspection: PresentationEvidenceInspectionResult, batch: _Batch
) -> dict[str, dict[tuple[date, str], Decimal]]:
    """Pack 15's own count of each cohort and group, per marketplace, from its load evidence."""
    found: dict[str, dict[tuple[date, str], Decimal]] = {}
    for slot, label in zip(inspection.slots, batch.labels, strict=True):
        market, path, _metric = label
        if path != ARP_PATH or slot.bound is None:
            continue
        found.setdefault(market, {}).update(
            arp_cohort_checks(slot.bound.all_publications, ARP_CONTRACT)
        )
    return found


def _delivery(  # noqa: PLR0913, PLR0917 -- one path's answers, scope, metrics, grain, checks
    answers: dict[_Label, GroupedAnswer],
    scope: tuple[str, str],
    metrics: tuple[str, ...],
    grain: CohortGrain,
    checks: dict[tuple[date, str], Decimal] | None = None,
) -> CohortDelivery:
    """Build one path's cells from its per-metric answers: a row per cell that came back."""
    market, path = scope
    cells: dict[tuple[date, int, str], dict[str, Decimal | None]] = {}
    for metric in metrics:
        answer = answers[(market, path, metric)]
        for key, value in answer.rows.items():
            _market, cohort, offset, group = key
            cell = (date.fromisoformat(str(cohort)[:10]), int(str(offset)), str(group))
            cells.setdefault(cell, {})[metric] = value
    return CohortDelivery(path, grain, cells, checks or {})


@dataclass(frozen=True, slots=True)
class RetainedRetentionPresetArtifactSource:
    """Compile the latest cohort vintage's Retention artifact from retained truth only."""

    vintage_policy: PresentationVintagePolicy
    compiler: RetentionCompiler
    served: Callable[[], frozenset[tuple[str, CalendarPeriod]]]
    """Every (contract, vintage) pack 14 has a CURRENT publication for, any marketplace."""
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(RETENTION_PAGE_ID))

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact."""
        if variant.value != self.vintage_policy.default:
            return None
        resolved = self.resolve()
        if resolved is None:
            return None
        rendered = self.compiler.render_retention(resolved)
        if rendered.artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities, render_format=RETENTION_RENDER_FORMAT
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            capped_preset_artifact_json(retention_artifact_payload(rendered.artifact)),
        )
        return PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerTemplate(
                self.page,
                variant,
                revision,
                body.digest,
                1 if current is None else current.forward_token + 1,
                None if current is None else current.revision,
                None if current is None else self.clock() + SUPERSEDED_HISTORY_RETENTION,
                resolved_window=rendered.artifact.period.window,
            ),
        )

    def resolve(self) -> RetentionRenderInput | None:
        """Return the latest vintage with a current pack 14 publication (the policy default)."""
        vintages = sorted(
            period
            for contract_id, period in self.served()
            if contract_id == STANDARD_CONTRACT and period.grain is TimeGrain.CUSTOM
        )
        return RetentionRenderInput(vintages[-1]) if vintages else None


def retention_bake_body_bytes(root: Path) -> int:
    """Register the largest Retention body: the per-market ceiling per governed marketplace."""
    marketplaces = {
        mapping.target.value for mapping in load_commerce_identity_bridge(root).marketplace_mappings
    }
    return RETENTION_MARKET_MAX_ENCODED_BYTES * len(marketplaces)


def retention_policy(schemas: PresentationPageSchemaSet) -> PresentationVintagePolicy | None:
    """Return the `retention` v2 page's vintage policy, or None until it declares this bake.

    The page id alone doesn't open the source: the entry must declare a vintage policy, exactly
    the artifact this renderer writes (`retention`, `RetentionCohortArtifact` v1, its three
    outcomes) and the path, triangle, curve and boundary sections. Any other declaration keeps
    the page unavailable.
    """
    page = next((item for item in schemas.pages if item.page_id == RETENTION_PAGE_ID), None)
    if page is None or not _declares_this_bake(page):
        return None
    return page.vintage_policy


def _declares_this_bake(page: PresentationPageSchema) -> bool:
    if page.vintage_policy is None or len(page.artifacts) != 1:
        return False
    (artifact,) = page.artifacts
    if (
        artifact.artifact_id != _ARTIFACT_ID
        or artifact.artifact_type != RETENTION_ARTIFACT_TYPE
        or artifact.version != RETENTION_ARTIFACT_VERSION
        or set(artifact.outcomes) != {"complete", "empty", "insufficient"}
        or len(artifact.outcomes) != 3  # noqa: PLR2004 -- the three declared outcomes
    ):
        return False
    return set(REQUIRED_SECTIONS) <= {section.section_id for section in page.sections}


def _contract(slot: InsightSlotId) -> InsightContract:
    for _, contract in load_insight_contracts(contracts_root()):
        if contract.id is slot:
            return contract
    raise RuntimeError(f"the {slot.value} insight contract is undeclared.")


RETENTION_MATERIALIZATION_UNAVAILABLE = "retention_materialization_unavailable"
"""The reason while the retained-truth configuration or the v2 page entry is absent."""


def quarterly_vintage_reader(  # noqa: PLR0913, PLR0917 -- the reader and the scope inputs
    current: CurrentPublicationInspectionPort,
    packs: tuple[SourcePack, ...],
    bridge: CommerceIdentityBridge,
    contract: DataContract,
) -> Callable[[str], date | None]:
    """Read a marketplace's first quarterly vintage from each quarterly series' earliest bucket.

    Each series is its own lens, so a quarterly series' scope holds only quarterly vintages and
    its earliest CURRENT publication is the first one: one bounded read (`EARLIEST_SCOPE_PAGE`),
    whatever the retained history, and never truncated by a bake's source horizon (A5.1 I9).
    """
    pack = next(item for item in packs if item.contract_id == contract.contract_id)
    cells = contract.expected_cells
    series = (None, *(() if cells is None else (name for name, _sets in cells.variants)))
    quarterly = tuple(
        variant
        for variant in series
        if cells is not None
        and cells.value_for(variant, COHORT_GRAIN_COLUMN, ()) == CohortGrain.QUARTER.value
    )

    def first(market: str) -> date | None:
        found = [
            CalendarPeriod(
                publication.partition.bucket.window.start,
                publication.partition.bucket.window.end,
                TimeGrain.CUSTOM,
            ).as_of_month
            for variant in quarterly
            for scope in publication_scopes_for(
                PublicationScopeRequest(
                    pack,
                    contract,
                    bridge,
                    {MARKETPLACE_SELECTOR: market},
                    period_grain=TimeGrain.CUSTOM,
                    variant=variant,
                )
            )
            if (publication := current.earliest_current_for_scope(scope)) is not None
        ]
        return min(found, default=None)

    return first


def retained_retention_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- the closed signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedRetentionPresetArtifactSource | None:
    """Compose the retained Retention source, or none, logging the closure condition once.

    The closed factory signature (A5.4): the invocation's admitted, captured `queries` and its
    one pinnable CURRENT reader, whose clock every renderer reads. Fails closed without the
    retained-truth configuration and while the v2 page entry `retention` isn't declared.
    """
    if not all(
        (
            settings.semantic_publications_table,
            settings.semantic_v2_bucket,
            settings.athena_workgroup,
            settings.semantic_v2_current_database,
            settings.athena_result_reuse_max_age_minutes > 0,
            settings.athena_query_batch_max_concurrency > 0,
        )
    ):
        return source_closed(RETENTION_PAGE_ID, "settings_incomplete")
    root = contracts_root()
    policy = retention_policy(load_v2_page_schemas(root))
    if policy is None:
        return source_closed(RETENTION_PAGE_ID, "page_entry_absent")
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    lags = {pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(root)}
    selector = PublicationSelector(
        reader, source_packs, bridge, cadence_lag_days=lags, horizons=horizons
    )
    marketplaces = tuple(sorted({mapping.target.value for mapping in bridge.marketplace_mappings}))
    governed = {item.marketplace.value: item.value for item in bridge.marketplace_display_names}
    anchoring = tuple(item for item in contracts if item.contract_id == STANDARD_CONTRACT)

    def served() -> frozenset[tuple[str, CalendarPeriod]]:
        return frozenset(
            item
            for market in marketplaces
            for item in selector.served_periods(
                ServedPeriodsRequest(anchoring, {MARKETPLACE_SELECTOR: market}, current.clock())
            ).publications
        )

    compiler = RetentionArtifactRenderer(
        catalog=load_catalog(root),
        contracts=contracts,
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=selector,
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
            periods=selector,
        ),
        marketplaces=marketplaces,
        insight_contract=_contract(InsightSlotId.RETENTION_SHIFT),
        clock=current.clock,
        first_quarterly=quarterly_vintage_reader(
            reader, source_packs, bridge, next(iter(anchoring))
        ),
        # As New customers: the Scorecard's short name ("US"), else the governed display name,
        # else the key.
        marketplace_labels={
            key: scorecard_marketplace_label(key, governed.get(key, key)) for key in marketplaces
        },
    )
    return RetainedRetentionPresetArtifactSource(
        vintage_policy=policy,
        compiler=compiler,
        served=served,
        pointers=pointers,
        clock=current.clock,
    )
