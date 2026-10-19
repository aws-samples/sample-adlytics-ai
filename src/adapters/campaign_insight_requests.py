# SPDX-License-Identifier: MIT-0
"""Shared deterministic campaign insight request derivation from measured artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.insight_finding_units import dictionary_id, finding_unit
from adapters.insight_next_steps import comparison_window_entry, data_sufficiency_entry
from adapters.insight_requests import (
    DeclaredInsightRequestInput,
    InsightRequestContext,
    analytical_scope_id,
    declared_insight_requests,
)
from domain.presentation.artifacts import canonical_artifact_json
from domain.presentation.campaign_attribution import (
    CampaignAttributionDetailArtifact,
    CampaignAttributionDetailV2,
    CampaignDelivery,
    CampaignDeliverySlot,
    CampaignMetricValue,
    CampaignNumber,
    CampaignSummarySlot,
    campaign_attribution_detail_v2_payload,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    campaign_portfolio_artifact_payload,
    campaigns_in_view_sentence,
    portfolio_row_operand_labels,
    portfolio_spend_change_split,
)
from domain.presentation.currency_display import currency_display_text
from domain.presentation.insights import (
    DeliveryFollowUpEvidence,
    DeterministicFinding,
    DeterministicFindingRequest,
    FindingUnit,
    InsightContextEntry,
    InsightContract,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightFindingOperand,
    InsightSlotId,
    InsightWindowProfile,
    OperandBasis,
    PredicateFinding,
    calculate_deterministic_findings,
    insight_window_profile,
    nonzero_movers,
)
from domain.presentation.metric_focus import (
    MetricFocusDriverEntry,
    MetricFocusDriverGrain,
    MetricFocusDrivers,
    MetricFocusDriversAbsent,
    MetricFocusDriversSummary,
    MetricFocusIdentity,
    MetricFocusIngredients,
    direction_counts_sentence,
    focus_drivers,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.values import DateRange


@dataclass(frozen=True, slots=True)
class CampaignInsightRequestDerivation:
    """One revision-bound declared model request set and its deterministic findings.

    Every declared slot the derivation does not request is classified in exactly one
    map: ``silence_reasons`` for slots evaluated in full whose contract silence
    condition held, ``missing_evidence_by_slot`` for slots a required evidence input
    made ineligible for this subject and window.
    """

    context: InsightRequestContext
    findings_by_slot: dict[InsightSlotId, tuple[DeterministicFinding, ...]]
    evidence_by_slot: dict[InsightSlotId, InsightEvidenceBundle]
    requests: tuple[InsightAssemblyRequest, ...]
    silence_reasons: Mapping[InsightSlotId, str]
    missing_evidence_by_slot: Mapping[InsightSlotId, tuple[InsightEvidenceInput, ...]]


@dataclass(frozen=True, slots=True)
class DeclaredInsightDerivationInput:
    """One declared slot batch's identity, findings, evidence, and display context."""

    scope: AuthorizedAnalyticalScope
    subject: str
    window: DateRange
    artifact_revision: str
    slot_ids: tuple[InsightSlotId, ...]
    findings_by_slot: dict[InsightSlotId, tuple[DeterministicFinding, ...]]
    evidence_by_slot: dict[InsightSlotId, InsightEvidenceBundle]
    contracts: Mapping[InsightSlotId, InsightContract]
    window_profile: InsightWindowProfile
    display_context_by_slot: dict[InsightSlotId, tuple[InsightContextEntry, ...]] | None = None
    filter_id: str = "all"


@dataclass(frozen=True, slots=True)
class DetailInsightRequestDerivationInput:
    """The server-owned measured detail and identity required for its insight slots."""

    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery
    subject: str
    scope: AuthorizedAnalyticalScope
    contracts: Mapping[InsightSlotId, InsightContract]


def artifact_revision(payload: dict[str, object]) -> str:
    """Fingerprint canonical server-owned measured artifact bytes."""
    canonical = canonical_artifact_json(payload)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _portfolio_payload_without_ephemera(artifact: CampaignPortfolioArtifact) -> dict[str, object]:
    """Return measured portfolio bytes without capability or insight delivery state."""
    payload = campaign_portfolio_artifact_payload(artifact)
    rows = payload["rows"]
    if not isinstance(rows, list):  # pragma: no cover - typed artifact codec guarantees this
        raise ValueError("campaign portfolio payload rows are malformed.")
    rendered_rows = cast("list[dict[str, object]]", rows)
    return {
        **payload,
        "insights": [],
        "rows": [
            {key: value for key, value in row.items() if key != "capability"}
            for row in rendered_rows
        ],
    }


def _detail_payload_without_ephemera(
    detail: CampaignAttributionDetailArtifact, delivery: CampaignDelivery
) -> dict[str, object]:
    """Return measured V2 detail bytes without insight delivery state."""
    payload = campaign_attribution_detail_v2_payload(CampaignAttributionDetailV2(detail, delivery))
    # The coverage block is written from the delivery's own statuses, so it adds no measured
    # bytes; fingerprints read the statuses directly.
    return {
        key: value for key, value in payload.items() if key not in {"insights", "evidence_status"}
    }


def change_findings(
    values: tuple[tuple[str, CampaignNumber, CampaignNumber], ...],
    kind: InsightFindingKind,
) -> tuple[DeterministicFinding, ...]:
    """Calculate one change-finding kind over named current/prior operand pairs."""
    # A percentage change is undefined against a zero prior; such operands are stated only as
    # absolute changes, never as an invented rate.
    admitted = (
        tuple(value for value in values if Decimal(value[2]) != 0)
        if kind is InsightFindingKind.PERCENTAGE_CHANGE
        else values
    )
    return (
        calculate_deterministic_findings(
            DeterministicFindingRequest(
                kind,
                tuple(
                    InsightFindingOperand(name, Decimal(current), Decimal(prior))
                    for name, current, prior in admitted
                ),
            )
        )
        if admitted
        else ()
    )


def absolute_findings(
    values: tuple[tuple[str, CampaignNumber, CampaignNumber], ...],
) -> tuple[DeterministicFinding, ...]:
    """Calculate exact absolute changes over named current/prior operand pairs."""
    return change_findings(values, InsightFindingKind.ABSOLUTE_CHANGE)


def _rendered_value(value: CampaignMetricValue | None) -> str:
    """Render one summary value with its currency when the units carry one."""
    if value is None:
        return "unavailable"
    code = value.units.currency_code
    return f"{value.value} {code}" if code else str(value.value)


def _window_context(period: DateRange, prior: DateRange | None) -> tuple[InsightContextEntry, ...]:
    entries = [InsightContextEntry("reporting window", f"{period.start} to {period.end}")]
    if prior is not None:
        entries.append(comparison_window_entry(prior))
    return tuple(entries)


def _portfolio_display_context(
    artifact: CampaignPortfolioArtifact, labels: tuple[str, ...]
) -> tuple[InsightContextEntry, ...]:
    """Render the portfolio sections the ADR-068 context-depth amendment declares as inputs."""
    entries = list(_window_context(artifact.period, artifact.prior_period))
    entries.append(
        data_sufficiency_entry(
            (
                *(item for evidence in artifact.evidence for item in evidence.freshness_qualifiers),
                *(item for row in artifact.rows for item in row.freshness_qualifiers),
            ),
            (hole for evidence in artifact.evidence for hole in evidence.coverage_holes),
        )
    )
    for total in artifact.totals:
        entries.append(
            InsightContextEntry(
                f"portfolio {total.metric.value} (current vs prior)",
                f"{total.current.value} vs {total.prior.value}",
            )
        )
    if labels:
        entries.append(
            InsightContextEntry(
                "campaigns listed",
                "; ".join(
                    f"{label} — {row.signature.channel}"
                    for label, row in zip(labels, artifact.rows, strict=True)
                ),
            )
        )
    return tuple(entries)


def _detail_selection_context(
    detail: CampaignAttributionDetailArtifact,
) -> tuple[InsightContextEntry, ...]:
    signature = detail.selection
    prior = (
        detail.comparison.metrics[0].prior.period
        if detail.comparison is not None
        and detail.comparison.metrics
        and detail.comparison.metrics[0].prior is not None
        else None
    )
    return (
        *_window_context(detail.period, prior),
        data_sufficiency_entry(
            (), (hole for evidence in detail.evidence_by_slot for hole in evidence.coverage_holes)
        ),
        InsightContextEntry(
            "selected campaign",
            f"{signature.effective_name or 'Unnamed campaign'} — {signature.channel}, "
            f"{signature.marketplace}, {signature.platform}",
        ),
    )


def _detail_summary_context(
    detail: CampaignAttributionDetailArtifact,
) -> tuple[InsightContextEntry, ...]:
    if detail.comparison is None:
        return ()
    return tuple(
        InsightContextEntry(
            f"{item.slot.value} (current vs prior)",
            f"{_rendered_value(item.current)} vs {_rendered_value(item.prior)}",
        )
        for item in detail.comparison.metrics
    )


def _purchase_series_context(
    detail: CampaignAttributionDetailArtifact,
) -> tuple[InsightContextEntry, ...]:
    if detail.purchase_series is None or not detail.purchase_series.points:
        return ()
    return (
        InsightContextEntry(
            "daily attributed purchases",
            "; ".join(
                f"{point.day.isoformat()}: {point.purchases}"
                for point in detail.purchase_series.points
            ),
        ),
    )


def _delivery_context(delivery: CampaignDelivery) -> tuple[InsightContextEntry, ...]:
    values = tuple(f"{item.slot.value}: {_rendered_value(item.value)}" for item in delivery.summary)
    return (InsightContextEntry("delivery summary", "; ".join(values)),) if values else ()


@dataclass(frozen=True, slots=True)
class FindingSource:
    """Which slot emitted findings, what they compare, and each operand's artifact metric.

    `metric` is one artifact metric id for every operand, or a map from operand name to id.
    """

    slot: InsightSlotId
    basis: OperandBasis
    metric: str | Mapping[str, str]

    def metric_for(self, name: str) -> str:
        """Return the artifact metric id one named operand measures."""
        return self.metric if isinstance(self.metric, str) else self.metric[name]


def predicate_findings(  # noqa: PLR0913 — the source and priors are keyword-only
    findings: tuple[DeterministicFinding, ...],
    kind: InsightFindingKind,
    *evidence_inputs: InsightEvidenceInput,
    source: FindingSource,
    priors: Mapping[str, Decimal] | None = None,
) -> tuple[PredicateFinding, ...]:
    """Bind findings of one kind to their evidence inputs, unit, basis and metric.

    The unit and prior scale come from the metric dictionary through `finding_unit`, which
    refuses a (kind, metric, basis) the slot does not declare (ADR-076 §5). ``priors`` maps
    operand names to their prior values, so a percentage finding carries the prior the
    near-zero bound is enforced against at selection.
    """
    bound: list[PredicateFinding] = []
    for finding in findings:
        metric = source.metric_for(finding.name)
        unit, scale = finding_unit(source.slot, kind, metric_id=metric, basis=source.basis)
        bound.append(
            PredicateFinding(
                finding,
                kind,
                evidence_inputs,
                unit,
                source.basis,
                dictionary_id(metric),
                scale,
                prior_magnitude=None if priors is None else priors.get(finding.name),
            )
        )
    return tuple(bound)


def change_priors(
    values: tuple[tuple[str, CampaignNumber, CampaignNumber], ...],
) -> dict[str, Decimal]:
    """Project named change operands into the prior map ``predicate_findings`` takes."""
    return {name: Decimal(prior) for name, _, prior in values}


def evidence_bundle(
    available_evidence_inputs: tuple[InsightEvidenceInput, ...],
    findings: tuple[PredicateFinding, ...],
    delivery_follow_ups: tuple[DeliveryFollowUpEvidence, ...] = (),
) -> InsightEvidenceBundle:
    """Assemble one slot's complete offered evidence for its executable predicate."""
    return InsightEvidenceBundle(available_evidence_inputs, findings, delivery_follow_ups)


def declared_derivation(
    request: DeclaredInsightDerivationInput,
) -> CampaignInsightRequestDerivation:
    """Build the declared request batch and its shared cache context from one input."""
    context = InsightRequestContext(
        analytical_scope_id(request.scope),
        request.subject,
        request.window,
        request.artifact_revision,
        request.filter_id,
    )
    batch = declared_insight_requests(
        DeclaredInsightRequestInput(
            context=context,
            contracts=request.contracts,
            findings_by_slot=request.findings_by_slot,
            slot_ids=request.slot_ids,
            window_profile=request.window_profile,
            evidence_by_slot=request.evidence_by_slot,
            display_context_by_slot=request.display_context_by_slot,
        )
    )
    return CampaignInsightRequestDerivation(
        context=context,
        findings_by_slot=request.findings_by_slot,
        evidence_by_slot=request.evidence_by_slot,
        requests=batch.requests,
        silence_reasons=batch.silence_reasons,
        missing_evidence_by_slot=batch.missing_evidence_by_slot,
    )


_row_operand_labels = portfolio_row_operand_labels


def portfolio_insight_request_derivation(
    *,
    artifact: CampaignPortfolioArtifact,
    scope: AuthorizedAnalyticalScope,
    contracts: Mapping[InsightSlotId, InsightContract],
) -> tuple[CampaignInsightRequestDerivation, tuple[DeterministicFinding, ...]]:
    """Derive portfolio model requests and direct movers from complete server artifacts only."""
    profile = insight_window_profile(artifact.period, artifact.prior_period)
    labels = _row_operand_labels(artifact.rows)
    spend_changes = tuple(
        (
            labels[index],
            row.comparison_for(CampaignPortfolioMetric.SPEND).current.number,
            row.comparison_for(CampaignPortfolioMetric.SPEND).prior.number,
        )
        for index, row in enumerate(artifact.rows)
    )
    # A ROAS over zero spend is typed absent (ADR-087): it is undefined, so it is never a
    # finding operand and never reaches the model as 0.
    roas_changes = tuple(
        (labels[index], current, prior)
        for index, row in enumerate(artifact.rows)
        for comparison in (row.comparison_for(CampaignPortfolioMetric.ATTRIBUTED_ROAS),)
        for current, prior in ((comparison.current.value, comparison.prior.value),)
        if current is not None and prior is not None
    )
    total_spend = next(
        (
            Decimal(total.current.number)
            for total in artifact.totals
            if total.metric is CampaignPortfolioMetric.SPEND
        ),
        Decimal(0),
    )
    exposure = (
        calculate_deterministic_findings(
            DeterministicFindingRequest(
                InsightFindingKind.SHARE_OF_TOTAL,
                tuple(
                    InsightFindingOperand(name, Decimal(current))
                    for name, current, _ in spend_changes
                ),
                total=total_spend,
            )
        )
        if spend_changes and total_spend > 0
        else ()
    )
    contribution_source = FindingSource(
        InsightSlotId.CONTRIBUTION,
        OperandBasis.CAMPAIGN_WINDOW,
        CampaignPortfolioMetric.SPEND.value,
    )
    contribution_absolute = absolute_findings(spend_changes)
    contribution_percentage = change_findings(spend_changes, InsightFindingKind.PERCENTAGE_CHANGE)
    # The portfolio-level direction is the PORTFOLIO_ROAS total: a validated COMPLETE
    # artifact's totals are exactly (spend, purchases, impressions, portfolio ROAS), so a
    # lookup for the row-grain ATTRIBUTED_ROAS metric here can never match — that dead
    # lookup silenced divergence on every portfolio until 2026-09-23.
    # A ROAS over a window with no spend is typed absent (ADR-087), so it has no direction; a
    # multi-channel sibling serves no ROAS total at all (ADR-127).
    portfolio_roas = next(
        (
            (Decimal(current), Decimal(prior))
            for total in artifact.totals
            if total.metric is CampaignPortfolioMetric.PORTFOLIO_ROAS
            for current, prior in ((total.current.value, total.prior.value),)
            if current is not None and prior is not None
        ),
        None,
    )
    divergence_findings: tuple[PredicateFinding, ...] = ()
    if portfolio_roas is not None and portfolio_roas[1] != 0:
        # The divergence pre-filter applies the thresholds the predicate enforces at
        # selection, each at its own basis (ADR-076 §5): the portfolio's ROAS at
        # portfolio_window, each campaign's at campaign_window.
        materiality = contracts[InsightSlotId.DIVERGENCE].predicate.materiality
        portfolio_threshold = materiality.threshold_for(
            profile.role,
            OperandBasis.PORTFOLIO_WINDOW,
            unit=FindingUnit.MULTIPLIER,
            metric_id=dictionary_id(CampaignPortfolioMetric.PORTFOLIO_ROAS.value),
        )
        campaign_threshold = materiality.threshold_for(
            profile.role,
            OperandBasis.CAMPAIGN_WINDOW,
            unit=FindingUnit.MULTIPLIER,
            metric_id=dictionary_id(CampaignPortfolioMetric.ATTRIBUTED_ROAS.value),
        )
        portfolio_values = (("Portfolio attributed ROAS", *portfolio_roas),)
        portfolio_absolute = absolute_findings(portfolio_values)
        portfolio_change = portfolio_absolute[0].value
        if abs(portfolio_change) >= portfolio_threshold and portfolio_change != 0:
            divergent_changes = tuple(
                value
                for value in roas_changes
                if (Decimal(value[1]) - Decimal(value[2])) * portfolio_change < 0
                and abs(Decimal(value[1]) - Decimal(value[2])) >= campaign_threshold
            )
            if divergent_changes:
                inputs = (
                    InsightEvidenceInput.PORTFOLIO_DIRECTION,
                    InsightEvidenceInput.CAMPAIGN_COMPARISONS,
                )
                portfolio_source = FindingSource(
                    InsightSlotId.DIVERGENCE,
                    OperandBasis.PORTFOLIO_WINDOW,
                    CampaignPortfolioMetric.PORTFOLIO_ROAS.value,
                )
                campaign_source = FindingSource(
                    InsightSlotId.DIVERGENCE,
                    OperandBasis.CAMPAIGN_WINDOW,
                    CampaignPortfolioMetric.ATTRIBUTED_ROAS.value,
                )
                # Each group carries its own priors, so a campaign display-named like the
                # portfolio operand can never overwrite the portfolio's prior.
                divergence_findings = (
                    predicate_findings(
                        portfolio_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        *inputs,
                        source=portfolio_source,
                    )
                    + predicate_findings(
                        absolute_findings(divergent_changes),
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        *inputs,
                        source=campaign_source,
                    )
                    + predicate_findings(
                        change_findings(portfolio_values, InsightFindingKind.PERCENTAGE_CHANGE),
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        *inputs,
                        source=portfolio_source,
                        priors=change_priors(portfolio_values),
                    )
                    + predicate_findings(
                        change_findings(divergent_changes, InsightFindingKind.PERCENTAGE_CHANGE),
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        *inputs,
                        source=campaign_source,
                        priors=change_priors(divergent_changes),
                    )
                )
    findings = {
        InsightSlotId.CONTRIBUTION: contribution_absolute + contribution_percentage,
        InsightSlotId.DIVERGENCE: tuple(item.finding for item in divergence_findings),
        InsightSlotId.EXPOSURE: exposure,
    }
    # Availability declares what this artifact actually delivered, never the static
    # contract shape (declared-outcomes rule); a portfolio-level direction exists only
    # when the portfolio ROAS total carries a nonzero prior to move against.
    comparison_inputs = (InsightEvidenceInput.CAMPAIGN_COMPARISONS,) if artifact.rows else ()
    totals_inputs = (InsightEvidenceInput.PORTFOLIO_TOTALS,) if artifact.totals else ()
    direction_inputs = (
        (InsightEvidenceInput.PORTFOLIO_DIRECTION,)
        if portfolio_roas is not None and portfolio_roas[1] != 0
        else ()
    )
    evidence_by_slot = {
        InsightSlotId.CONTRIBUTION: evidence_bundle(
            totals_inputs + comparison_inputs,
            predicate_findings(
                contribution_absolute,
                InsightFindingKind.ABSOLUTE_CHANGE,
                InsightEvidenceInput.CAMPAIGN_COMPARISONS,
                source=contribution_source,
            )
            + predicate_findings(
                contribution_percentage,
                InsightFindingKind.PERCENTAGE_CHANGE,
                InsightEvidenceInput.CAMPAIGN_COMPARISONS,
                source=contribution_source,
                priors=change_priors(spend_changes),
            ),
        ),
        InsightSlotId.DIVERGENCE: evidence_bundle(
            direction_inputs + comparison_inputs, divergence_findings
        ),
        InsightSlotId.EXPOSURE: evidence_bundle(
            totals_inputs + comparison_inputs,
            predicate_findings(
                exposure,
                InsightFindingKind.SHARE_OF_TOTAL,
                InsightEvidenceInput.PORTFOLIO_TOTALS,
                InsightEvidenceInput.CAMPAIGN_COMPARISONS,
                source=FindingSource(
                    InsightSlotId.EXPOSURE,
                    OperandBasis.CAMPAIGN_WINDOW,
                    CampaignPortfolioMetric.SPEND.value,
                ),
            ),
        ),
    }
    movers = (
        nonzero_movers(
            calculate_deterministic_findings(
                DeterministicFindingRequest(
                    InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
                    tuple(
                        InsightFindingOperand(name, Decimal(current), Decimal(prior))
                        for name, current, prior in spend_changes
                    ),
                )
            )
        )
        if spend_changes
        else ()
    )
    portfolio_context = _portfolio_display_context(artifact, labels)
    return (
        declared_derivation(
            DeclaredInsightDerivationInput(
                scope=scope,
                subject="campaign-portfolio",
                window=artifact.period,
                window_profile=profile,
                artifact_revision=artifact_revision(_portfolio_payload_without_ephemera(artifact)),
                # Divergence compares each campaign with the portfolio's ROAS, which a
                # multi-channel sibling never totals, so it isn't scheduled there (ADR-127 d5).
                slot_ids=portfolio_model_slot_ids(artifact),
                findings_by_slot=findings,
                evidence_by_slot=evidence_by_slot,
                contracts=contracts,
                display_context_by_slot={
                    # C1: contribution states its scope and the move outside it; divergence
                    # gets the scope sentence only (design §4.5, R2-14).
                    InsightSlotId.CONTRIBUTION: portfolio_context
                    + _contribution_scope_context(artifact),
                    InsightSlotId.DIVERGENCE: (
                        *portfolio_context,
                        InsightContextEntry(
                            "campaigns in view", campaigns_in_view_sentence(artifact)
                        ),
                    ),
                    InsightSlotId.EXPOSURE: portfolio_context,
                },
                filter_id=artifact.filter_id,
            )
        ),
        movers,
    )


def portfolio_model_slot_ids(artifact: CampaignPortfolioArtifact) -> tuple[InsightSlotId, ...]:
    """Return the model slots one sibling schedules; divergence needs a portfolio ROAS."""
    if artifact.multi_channel:
        return (InsightSlotId.CONTRIBUTION, InsightSlotId.EXPOSURE)
    return (InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE)


UNCALIBRATED_IN_VIEW_SHARE_FLOOR = Decimal("25")
"""Uncalibrated: the in-view share of the spend change is supplied only at or above 25 %."""
UNCALIBRATED_TOTAL_CHANGE_FLOOR = Decimal("3")
"""Uncalibrated: and only when the total spend change is at least 3 % of its prior."""


def _signs_agree(*values: Decimal) -> bool:
    return all(value > 0 for value in values) or all(value < 0 for value in values)


def _contribution_scope_context(
    artifact: CampaignPortfolioArtifact,
) -> tuple[InsightContextEntry, ...]:
    """State which campaigns the contribution card reads and how their spend moved (C1)."""
    entries = [InsightContextEntry("campaigns in view", campaigns_in_view_sentence(artifact))]
    split = portfolio_spend_change_split(artifact)
    total = next(
        (item for item in artifact.totals if item.metric is CampaignPortfolioMetric.SPEND), None
    )
    if split is None or total is None:
        return tuple(entries)
    in_view, outside = split
    currency = total.current.units.currency_code
    entries.append(
        InsightContextEntry("spend change in view", currency_display_text(in_view, currency))
    )
    entries.append(
        InsightContextEntry(
            "spend change outside the view", currency_display_text(outside, currency)
        )
    )
    change = in_view + outside
    prior = Decimal(total.prior.number)
    if (
        change != 0
        and prior > 0
        and _signs_agree(in_view, outside, change)
        and in_view / change * 100 >= UNCALIBRATED_IN_VIEW_SHARE_FLOOR
        and abs(change) / prior * 100 >= UNCALIBRATED_TOTAL_CHANGE_FLOOR
    ):
        share = (in_view / change * 100).quantize(Decimal(1))
        entries.append(
            InsightContextEntry(
                "share of the portfolio change",
                f"{share}% of the portfolio's spend change",
            )
        )
    spend_changes = tuple(
        Decimal(change)
        for row in artifact.rows
        for change in (row.comparison_for(CampaignPortfolioMetric.SPEND).absolute_change,)
        if change is not None
    )
    if spend_changes:
        entries.append(
            InsightContextEntry(
                "direction counts in view", direction_counts_sentence(spend_changes)
            )
        )
    return tuple(entries)


def detail_insight_request_derivation(
    request: DetailInsightRequestDerivationInput,
) -> CampaignInsightRequestDerivation:
    """Derive detail model requests from its delivered server-owned evidence only."""
    detail = request.detail
    delivery = request.delivery
    profile = insight_window_profile(detail.period, detail.prior_period)
    comparisons = (
        tuple(
            (item.slot.value, item.current.value, item.prior.value)
            for item in detail.comparison.metrics
            if item.current is not None and item.prior is not None
        )
        if detail.comparison is not None
        else ()
    )
    trend_values: tuple[tuple[str, CampaignNumber, CampaignNumber], ...] = ()
    if detail.purchase_series is not None and len(detail.purchase_series.points) > 1:
        # The change finding is (current - prior): the series END is the current operand and
        # its START the prior one, or a rising series narrates as a decline.
        first, last = detail.purchase_series.points[0], detail.purchase_series.points[-1]
        trend_values = (("delivered purchases across the series", last.purchases, first.purchases),)
    trend_absolute = absolute_findings(trend_values)
    trend_percentage = change_findings(trend_values, InsightFindingKind.PERCENTAGE_CHANGE)
    efficiency = tuple(
        value for value in comparisons if value[0] == CampaignSummarySlot.ATTRIBUTED_ROAS.value
    )
    efficiency_absolute = absolute_findings(efficiency)
    efficiency_percentage = change_findings(efficiency, InsightFindingKind.PERCENTAGE_CHANGE)
    # The comparison operands are named by their summary slot, which is the artifact metric.
    trend_source = FindingSource(
        InsightSlotId.TREND_SHAPE, OperandBasis.CAMPAIGN_DAY, CampaignSummarySlot.PURCHASES.value
    )
    efficiency_source = FindingSource(
        InsightSlotId.EFFICIENCY_DRIFT,
        OperandBasis.CAMPAIGN_WINDOW,
        CampaignSummarySlot.ATTRIBUTED_ROAS.value,
    )
    next_check_source = FindingSource(
        InsightSlotId.NEXT_CHECK,
        OperandBasis.CAMPAIGN_WINDOW,
        {name: name for name, _, _ in comparisons},
    )
    next_check_absolute = absolute_findings(comparisons)
    next_check_percentage = change_findings(comparisons, InsightFindingKind.PERCENTAGE_CHANGE)
    delivery_values = {value.slot: value.value for value in delivery.summary}
    funnel = ()
    purchases = detail.summary.purchases
    clicks = delivery_values.get(CampaignDeliverySlot.CLICKS)
    if purchases is not None and clicks is not None and Decimal(clicks.value) > 0:
        funnel = calculate_deterministic_findings(
            DeterministicFindingRequest(
                InsightFindingKind.SHARE_OF_TOTAL,
                (InsightFindingOperand("delivered purchases", Decimal(purchases.value)),),
                total=Decimal(clicks.value),
            )
        )
    # Availability declares what this artifact actually delivered, never the static
    # contract shape: a claimed-but-absent input would misclassify an ineligible slot
    # as evaluated-quiet (declared-outcomes rule).
    trend_inputs = (InsightEvidenceInput.PURCHASE_SERIES,) if trend_values else ()
    comparison_inputs = (InsightEvidenceInput.DETAIL_COMPARISONS,) if comparisons else ()
    funnel_inputs = (
        (InsightEvidenceInput.DELIVERY_METRICS,)
        if purchases is not None and clicks is not None
        else ()
    )
    next_check_inputs = comparison_inputs
    next_check_follow_ups: tuple[DeliveryFollowUpEvidence, ...] = ()
    if clicks is not None:
        next_check_inputs += (InsightEvidenceInput.DELIVERY_FOLLOW_UP,)
        next_check_follow_ups = (DeliveryFollowUpEvidence.CLICKS,)
    selection_context = _detail_selection_context(detail)
    summary_context = _detail_summary_context(detail)
    delivery_context = _delivery_context(delivery)
    return declared_derivation(
        DeclaredInsightDerivationInput(
            scope=request.scope,
            subject=request.subject,
            window=detail.period,
            window_profile=profile,
            artifact_revision=artifact_revision(_detail_payload_without_ephemera(detail, delivery)),
            slot_ids=(
                InsightSlotId.TREND_SHAPE,
                InsightSlotId.EFFICIENCY_DRIFT,
                InsightSlotId.FUNNEL,
                InsightSlotId.NEXT_CHECK,
            ),
            display_context_by_slot={
                InsightSlotId.TREND_SHAPE: (
                    *selection_context,
                    *_purchase_series_context(detail),
                ),
                InsightSlotId.EFFICIENCY_DRIFT: (*selection_context, *summary_context),
                InsightSlotId.FUNNEL: (*selection_context, *delivery_context),
                InsightSlotId.NEXT_CHECK: (
                    *selection_context,
                    *summary_context,
                    *delivery_context,
                ),
            },
            findings_by_slot={
                InsightSlotId.TREND_SHAPE: trend_absolute + trend_percentage,
                InsightSlotId.EFFICIENCY_DRIFT: efficiency_absolute + efficiency_percentage,
                InsightSlotId.FUNNEL: funnel,
                InsightSlotId.NEXT_CHECK: next_check_absolute + next_check_percentage,
            },
            evidence_by_slot={
                InsightSlotId.TREND_SHAPE: evidence_bundle(
                    trend_inputs,
                    predicate_findings(
                        trend_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        InsightEvidenceInput.PURCHASE_SERIES,
                        source=trend_source,
                    )
                    + predicate_findings(
                        trend_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        InsightEvidenceInput.PURCHASE_SERIES,
                        source=trend_source,
                        priors=change_priors(trend_values),
                    ),
                ),
                InsightSlotId.EFFICIENCY_DRIFT: evidence_bundle(
                    comparison_inputs,
                    predicate_findings(
                        efficiency_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        InsightEvidenceInput.DETAIL_COMPARISONS,
                        source=efficiency_source,
                    )
                    + predicate_findings(
                        efficiency_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        InsightEvidenceInput.DETAIL_COMPARISONS,
                        source=efficiency_source,
                        priors=change_priors(efficiency),
                    ),
                ),
                InsightSlotId.FUNNEL: evidence_bundle(
                    funnel_inputs,
                    predicate_findings(
                        funnel,
                        InsightFindingKind.SHARE_OF_TOTAL,
                        InsightEvidenceInput.DELIVERY_METRICS,
                        source=FindingSource(
                            InsightSlotId.FUNNEL,
                            OperandBasis.CAMPAIGN_WINDOW,
                            CampaignSummarySlot.PURCHASES.value,
                        ),
                    ),
                ),
                InsightSlotId.NEXT_CHECK: evidence_bundle(
                    next_check_inputs,
                    predicate_findings(
                        next_check_absolute,
                        InsightFindingKind.ABSOLUTE_CHANGE,
                        InsightEvidenceInput.DETAIL_COMPARISONS,
                        source=next_check_source,
                    )
                    + predicate_findings(
                        next_check_percentage,
                        InsightFindingKind.PERCENTAGE_CHANGE,
                        InsightEvidenceInput.DETAIL_COMPARISONS,
                        source=next_check_source,
                        priors=change_priors(comparisons),
                    ),
                    next_check_follow_ups,
                ),
            },
            contracts=request.contracts,
        )
    )


@dataclass(frozen=True, slots=True)
class FocusInsightDerivationInput:
    """One focused metric's extracted ingredients plus the identities the caller owns."""

    ingredients: MetricFocusIngredients
    identity: MetricFocusIdentity
    subject: str
    scope: AuthorizedAnalyticalScope
    artifact_revision: str
    slot_id: InsightSlotId
    contracts: Mapping[InsightSlotId, InsightContract]
    basis: OperandBasis
    """What the focused total compares: the portfolio's, one campaign's, or the retail total."""
    filter_id: str = "all"
    """The channel filter sibling the focused total reads (ADR-127)."""


def _driver_text(entry: MetricFocusDriverEntry, suffix: str | None) -> str:
    change = (
        currency_display_text(Decimal(entry.change), entry.units.currency_code)
        if entry.units.currency_code
        else str(entry.change)
    )
    if entry.chip is not None:
        return f"{entry.name}: {change} ({entry.chip})"
    if entry.share_of_move is not None and suffix is not None:
        return f"{entry.name}: {change} ({entry.share_of_move} {suffix})"
    return f"{entry.name}: {change}"


def _focus_driver_context(
    ingredients: MetricFocusIngredients,
) -> tuple[InsightContextEntry, ...]:
    """Render the dialog's drivers, scope and summary as card context (design §4.6)."""
    drivers, summary = focus_drivers(ingredients)
    if isinstance(drivers, MetricFocusDriversAbsent):
        return ()
    return driver_context(drivers, summary)


def driver_context(
    drivers: MetricFocusDrivers, summary: MetricFocusDriversSummary | None
) -> tuple[InsightContextEntry, ...]:
    """Render ranked drivers and their summary lines as context, in the dialog's words."""
    suffix = None if drivers.scope is None else drivers.scope.share_suffix
    entries = [
        InsightContextEntry(
            "largest drivers", "; ".join(_driver_text(entry, suffix) for entry in drivers.entries)
        )
    ]
    if summary is not None:
        entries.extend(InsightContextEntry(name, text) for name, text in summary.lines)
    return tuple(entries)


def focus_insight_request_derivation(
    request: FocusInsightDerivationInput,
) -> CampaignInsightRequestDerivation:
    """Derive one focus slot's request grounded exclusively in what the dialog displays.

    ADR-068: the interpretation reads the same server-computed change and driver
    movements the drivers block renders, never anything the page does not show.
    """
    ingredients = request.ingredients
    identity = request.identity
    current = Decimal(ingredients.current.value)
    prior = Decimal(ingredients.prior.value)
    values = ((identity.label, cast("CampaignNumber", current), cast("CampaignNumber", prior)),)
    profile = insight_window_profile(ingredients.period, ingredients.prior_period)
    evidence_inputs = (InsightEvidenceInput.FOCUSED_METRIC,)
    focus_source = FindingSource(request.slot_id, request.basis, ingredients.metric_id)
    focus_predicate_findings: list[PredicateFinding] = [
        *predicate_findings(
            absolute_findings(values),
            InsightFindingKind.ABSOLUTE_CHANGE,
            *evidence_inputs,
            source=focus_source,
        ),
        *predicate_findings(
            change_findings(values, InsightFindingKind.PERCENTAGE_CHANGE),
            InsightFindingKind.PERCENTAGE_CHANGE,
            *evidence_inputs,
            source=focus_source,
            priors=change_priors(values),
        ),
    ]
    driver_context: list[InsightContextEntry] = []
    if ingredients.driver_candidates:
        ranked = calculate_deterministic_findings(
            DeterministicFindingRequest(
                InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
                tuple(
                    InsightFindingOperand(
                        candidate.name, Decimal(candidate.current), Decimal(candidate.prior)
                    )
                    for candidate in ingredients.driver_candidates
                ),
            )
        )
        # Ranked drivers carry each contributor's own metric where it names one; campaign
        # drivers compare campaign windows and product drivers product windows.
        driver_basis = (
            OperandBasis.PRODUCT_WINDOW
            if ingredients.drivers_grain is MetricFocusDriverGrain.PRODUCT
            else OperandBasis.CAMPAIGN_WINDOW
            if ingredients.drivers_grain is MetricFocusDriverGrain.CAMPAIGN
            else request.basis
        )
        driver_metrics = {
            candidate.name: candidate.metric_id or ingredients.metric_id
            for candidate in ingredients.driver_candidates
        }
        focus_predicate_findings.extend(
            predicate_findings(
                ranked,
                InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
                *evidence_inputs,
                source=FindingSource(request.slot_id, driver_basis, driver_metrics),
            )
        )
        # C2/C3: the card reads exactly the dialog's ranked drivers and summary (one home).
        driver_context.extend(_focus_driver_context(ingredients))
    display_context = (
        *_window_context(ingredients.period, ingredients.prior_period),
        InsightContextEntry("focused metric", identity.label),
        InsightContextEntry("focused metric definition", identity.definition),
        InsightContextEntry("focused metric (current vs prior)", f"{current} vs {prior}"),
        *driver_context,
        *(
            (InsightContextEntry("basis boundary", ingredients.boundary),)
            if ingredients.boundary
            else ()
        ),
    )
    findings = tuple(item.finding for item in focus_predicate_findings)
    return declared_derivation(
        DeclaredInsightDerivationInput(
            scope=request.scope,
            subject=request.subject,
            window=ingredients.period,
            window_profile=profile,
            artifact_revision=request.artifact_revision,
            slot_ids=(request.slot_id,),
            findings_by_slot={request.slot_id: findings},
            evidence_by_slot={
                request.slot_id: evidence_bundle(evidence_inputs, tuple(focus_predicate_findings))
            },
            contracts=request.contracts,
            display_context_by_slot={request.slot_id: display_context},
            filter_id=request.filter_id,
        )
    )
