# SPDX-License-Identifier: MIT-0
"""Retained-publication precompute planning guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.jobs.insight_precompute.planning import (
    RetainedPresetRequests,
    RetainedPublicationPlanInput,
    RetainedPublicationPlanner,
    RetainedPublicationRequestBuilder,
)
from adapters.jobs.insight_precompute.requests import (
    CampaignRetainedPublicationRequestBuilder,
    RetainedCampaignInsightArtifacts,
)
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
)
from domain.presentation.temporal import TemporalPresetId
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
)
from ports.outbound.source_coverage import (
    SourceCoverageBounds,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    SourceCoverageResult,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

pytestmark = pytest.mark.unit


@dataclass
class RecordingLogger:
    lines: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def info(self, _message: str, *, extra: dict[str, object]) -> None:
        self.lines.append(("INFO", extra))

    def warning(self, _message: str, *, extra: dict[str, object]) -> None:
        self.lines.append(("WARNING", extra))


@dataclass
class Coverage:
    intervals: tuple[DateRange, ...]
    calls: list[SourceCoverageRequest] = field(default_factory=list[SourceCoverageRequest])

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        self.calls.append(request)
        return SourceCoverageResult(
            (),
            SourceCoverageBounds(
                self.intervals,
                self.intervals[0].start if self.intervals else None,
                self.intervals[-1].end if self.intervals else None,
            ),
            None,
        )


@dataclass
class Catalog:
    result: EntityCatalogBrowseResult
    calls: list[EntityCatalogBrowseRequest] = field(
        default_factory=list[EntityCatalogBrowseRequest]
    )

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        self.calls.append(request)
        return self.result


@dataclass
class Builder:
    inputs: list[RetainedPublicationPlanInput] = field(
        default_factory=list[RetainedPublicationPlanInput]
    )

    def requests_for(self, request: RetainedPublicationPlanInput) -> RetainedPresetRequests:
        self.inputs.append(request)
        return RetainedPresetRequests()


def _policy(
    presets: tuple[TemporalPresetId, ...] = (TemporalPresetId.SEVEN_DAYS,),
) -> PresentationTemporalPolicy:
    return PresentationTemporalPolicy(
        candidate_presets=presets,
        default_preset=TemporalPresetId.SEVEN_DAYS,
        custom_enabled=True,
        custom_min_duration_days=1,
        custom_max_duration_days=30,
        comparison_policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR,
        display_grains=(PageTime.DAY,),
    )


def _planner(  # noqa: PLR0913 — the bound and logger are the admission test's inputs
    coverage: Coverage,
    catalog: Catalog,
    builder: RetainedPublicationRequestBuilder,
    *,
    max_requests: int = 8,
    logger: RecordingLogger | None = None,
    presets: tuple[TemporalPresetId, ...] = (TemporalPresetId.SEVEN_DAYS,),
) -> RetainedPublicationPlanner:
    return RetainedPublicationPlanner(
        coverage=coverage,
        coverage_requirements=(SourceCoverageRequirement((("fictional", "campaigns"),)),),
        temporal_policy=_policy(presets),
        catalog=cast("AnalyticalEntityCatalogPort", catalog),
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
        request_builder=builder,
        max_requests=max_requests,
        logger=cast("Logger", logger or RecordingLogger()),
    )


@pytest.mark.covers("ADR-068")
def test_planner_enumerates_only_available_declared_presets_with_exact_required_prior() -> None:
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    catalog = Catalog(EntityCatalogBrowseResult(()))
    builder = Builder()

    _planner(coverage, catalog, builder).requests_for(cast("SuccessfulPublication", object()))

    assert len(coverage.calls) == 1
    assert len(catalog.calls) == 1
    assert catalog.calls[0].max_candidates == 50
    assert builder.inputs[0].window == DateRange(date(2026, 1, 25), date(2026, 1, 31))
    assert builder.inputs[0].prior_window == DateRange(date(2026, 1, 18), date(2026, 1, 24))


@pytest.mark.covers("ADR-068")
def test_planner_refuses_all_work_when_authorized_campaign_snapshot_is_truncated() -> None:
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    catalog = Catalog(EntityCatalogBrowseResult((), truncated=True, authorized_count=1))
    builder = Builder()

    requests = _planner(coverage, catalog, builder).requests_for(
        cast("SuccessfulPublication", object())
    )

    assert requests == ()
    assert builder.inputs == []


@dataclass
class ArtifactSource:
    inputs: list[RetainedPublicationPlanInput] = field(
        default_factory=list[RetainedPublicationPlanInput]
    )

    def artifacts_for(
        self, request: RetainedPublicationPlanInput
    ) -> RetainedCampaignInsightArtifacts:
        self.inputs.append(request)
        return RetainedCampaignInsightArtifacts(None, ())


@pytest.mark.covers("ADR-068")
def test_concrete_retained_builder_receives_only_planner_resolved_server_inputs() -> None:
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    catalog = Catalog(EntityCatalogBrowseResult(()))
    artifacts = ArtifactSource()
    builder = CampaignRetainedPublicationRequestBuilder(
        artifacts=artifacts,
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
        contracts={},
    )

    requests = _planner(
        coverage,
        catalog,
        cast("RetainedPublicationRequestBuilder", builder),
    ).requests_for(cast("SuccessfulPublication", object()))

    assert requests == ()
    assert len(artifacts.inputs) == 1
    assert artifacts.inputs[0].window == DateRange(date(2026, 1, 25), date(2026, 1, 31))
    assert artifacts.inputs[0].prior_window == DateRange(date(2026, 1, 18), date(2026, 1, 24))
    assert artifacts.inputs[0].campaign_aliases == ()


def _slot(slot_id: InsightSlotId, subject: str) -> InsightAssemblyRequest:
    """Return a stand-in request; admission reads only its slot identity and keeps the object."""
    return cast(
        "InsightAssemblyRequest",
        SimpleNamespace(key=SimpleNamespace(slot_id=slot_id, subject=subject)),
    )


DETAIL_SLOTS = (
    InsightSlotId.TREND_SHAPE,
    InsightSlotId.EFFICIENCY_DRIFT,
    InsightSlotId.FUNNEL,
    InsightSlotId.NEXT_CHECK,
)
PORTFOLIO_SLOTS = (InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE)


def _preset_requests(tag: str, campaigns: int) -> RetainedPresetRequests:
    """One preset's full portfolio and ``campaigns`` complete details of four slots each."""
    return RetainedPresetRequests(
        portfolio=tuple(_slot(slot, f"{tag}/campaign-portfolio") for slot in PORTFOLIO_SLOTS),
        details=tuple(
            tuple(_slot(slot, f"{tag}/campaign-{index:02d}") for slot in DETAIL_SLOTS)
            for index in range(campaigns)
        ),
    )


@dataclass
class FixedBuilder:
    """Return one prepared request split per planner-resolved preset, in call order."""

    presets: list[RetainedPresetRequests]

    def requests_for(self, request: RetainedPublicationPlanInput) -> RetainedPresetRequests:
        _ = request
        return self.presets.pop(0)


def _admission(logger: RecordingLogger) -> dict[str, object]:
    (line,) = logger.lines
    level, extra = line
    assert level == "INFO"
    return {
        key: extra[key]
        for key in (
            "operation",
            "phase",
            "outcome",
            "requested",
            "portfolio_requested",
            "detail_requested",
            "admitted",
            "skipped",
            "max_requests",
        )
    }


@pytest.mark.covers("ADR-068")
def test_planner_admits_the_portfolio_then_whole_details_to_the_bound_in_campaign_order() -> None:
    """28 complete DSP details (4 slots each) plus the portfolio against the 40-request bound.

    Before CPC derivation no detail was ever complete, so only the portfolio was planned. Now
    every DSP detail is, and the union is 3 + 112 = 115. The portfolio is admitted first, then
    whole campaigns in campaign_key order until the next one would pass 40: 3 + 9 x 4 = 39.
    """
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    preset = _preset_requests("7d", 28)
    logger = RecordingLogger()

    requests = _planner(
        coverage,
        Catalog(EntityCatalogBrowseResult(())),
        FixedBuilder([preset]),
        max_requests=40,
        logger=logger,
    ).requests_for(cast("SuccessfulPublication", object()))

    assert requests == preset.portfolio + tuple(
        request for campaign in preset.details[:9] for request in campaign
    )
    assert len(requests) == 39
    assert _admission(logger) == {
        "operation": "insight.precompute",
        "phase": "admission",
        "outcome": "bounded",
        "requested": 115,
        "portfolio_requested": 3,
        "detail_requested": 112,
        "admitted": 39,
        "skipped": 76,
        "max_requests": 40,
    }


@pytest.mark.covers("ADR-068")
def test_planner_admits_every_presets_portfolio_before_any_detail() -> None:
    """The bound is per publication, so a later preset's portfolio outranks an earlier detail."""
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    seven, fourteen = _preset_requests("7d", 28), _preset_requests("14d", 28)
    logger = RecordingLogger()

    requests = _planner(
        coverage,
        Catalog(EntityCatalogBrowseResult(())),
        FixedBuilder([seven, fourteen]),
        max_requests=40,
        logger=logger,
        presets=(TemporalPresetId.SEVEN_DAYS, TemporalPresetId.FOURTEEN_DAYS),
    ).requests_for(cast("SuccessfulPublication", object()))

    assert requests == seven.portfolio + fourteen.portfolio + tuple(
        request for campaign in seven.details[:8] for request in campaign
    )
    assert len(requests) == 38
    assert _admission(logger) == {
        "operation": "insight.precompute",
        "phase": "admission",
        "outcome": "bounded",
        "requested": 230,
        "portfolio_requested": 6,
        "detail_requested": 224,
        "admitted": 38,
        "skipped": 192,
        "max_requests": 40,
    }


@pytest.mark.covers("ADR-068")
def test_planner_admits_an_under_bound_union_unchanged_and_in_order() -> None:
    coverage = Coverage((DateRange(date(2026, 1, 1), date(2026, 1, 31)),))
    preset = _preset_requests("7d", 2)
    logger = RecordingLogger()

    requests = _planner(
        coverage,
        Catalog(EntityCatalogBrowseResult(())),
        FixedBuilder([preset]),
        max_requests=40,
        logger=logger,
    ).requests_for(cast("SuccessfulPublication", object()))

    assert requests == preset.portfolio + preset.details[0] + preset.details[1]
    assert _admission(logger) == {
        "operation": "insight.precompute",
        "phase": "admission",
        "outcome": "admitted",
        "requested": 11,
        "portfolio_requested": 3,
        "detail_requested": 8,
        "admitted": 11,
        "skipped": 0,
        "max_requests": 40,
    }
