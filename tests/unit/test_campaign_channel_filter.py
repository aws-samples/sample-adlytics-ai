# SPDX-License-Identifier: MIT-0
"""Campaigns channel filter (ADR-127): render-only siblings of one bake, served by filter."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from campaign_channel_filter_support import (
    CHANNEL_LABELS,
    CHANNEL_ORDER,
    ESTATE,
    FictionalCampaign,
    FilterCompiler,
    ViewabilityCoverage,
    _f,  # pyright: ignore[reportPrivateUsage]
    filter_siblings,
)
from preset_artifact_support import staged_custody, staged_index, staged_pointer
from test_campaign_preset_source import (
    CAMPAIGN_POLICY,
    REQUIREMENT,
    Catalog,
    Coverage,
    Pointers,
)
from test_presentation_handler import (
    CAMPAIGN_PLATFORM_LABELS,
    DIMENSION_LABELS,
    METRIC_CATALOG,
    SCHEMAS,
    SESSION,
    RecordingCatalog,
    RecordingCustodyStore,
    RecordingPages,
    RecordingPresetArtifacts,
    ScenarioQueries,
    _analytical_runtime,  # pyright: ignore[reportPrivateUsage]
    _AnalyticalRuntimeRequest,  # pyright: ignore[reportPrivateUsage]
    _body,  # pyright: ignore[reportPrivateUsage]
    _campaign_preset_artifact_with_custody,  # pyright: ignore[reportPrivateUsage]
    _event,  # pyright: ignore[reportPrivateUsage]
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    _RecordingDeepAssembler,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from adapters.deep_insight_inputs import WHOLE_PORTFOLIO_SUFFIX
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    RetainedCampaignPresetArtifactSource,
)
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_custody import campaign_custody_manifest_from_payload
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    CampaignPortfolioRosterEntry,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_artifact_payload,
    campaign_roster_shard_from_payload,
)
from domain.presentation.evidence_status import withheld_slots
from domain.presentation.next_steps import NextStepPageTargets, NextStepTargets
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactFilterIdentity,
    PresetArtifactIndexShardIdentity,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactPublicationRequest,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
    index_shard_descriptors,
    split_parent_custody,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
)
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.errors import AdlyticsError, ErrorCode, ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import AnalyticalEntityCatalogPort, EntityCatalogBrowseResult
from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort

pytestmark = pytest.mark.unit

VARIANT = PresetArtifactVariant("7d")
LIST_PATH = "/api/presentation/campaigns"
FOCUS_PATH = "/api/presentation/portfolio-metric-focus"
DEEP_DIVE_PATH = "/api/presentation/insight-deep-dive"
PRESENTATION_REFUSED = ErrorCode("PRESENTATION", 1)
FILTER_NOT_OFFERED = ErrorCode("PRESENTATION", 5)
ARTIFACT_MALFORMED = ErrorCode("PAGE_CONTEXT", 4)
"""The serve path's malformed-artifact refusal."""
_ATTRIBUTED_TOTALS = {"purchases", "portfolio_roas_attributed_14d"}
LABELS = (
    PresentationDimensionLabel("platform_key", "amazon_ads", "platform", "Amazon Ads"),
    PresentationDimensionLabel("marketplace_key", "amazon_us", "marketplace", "United States"),
    *(
        PresentationDimensionLabel("channel_key", key, "channel", label)
        for key, label in CHANNEL_LABELS.items()
    ),
)


def _alias(campaign: FictionalCampaign) -> EntityAlias:
    selection = campaign.selection
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=selection.campaign,
        commerce_scope=selection.campaign.commerce_scope,
        labels=(EffectiveLabel(campaign.name, date(2026, 1, 1), None),),
        source_identifiers=(SourceIdentifier("fictional.campaign", selection.campaign.value),),
        evidence=MappingEvidence("fictional-channel-filter", 1),
        valid_from=date(2026, 1, 1),
        valid_to=None,
        platform=selection.campaign.platform,
        channel=selection.channel,
        marketplace=selection.marketplace,
    )


def _bake(
    compiler: FilterCompiler | None = None,
    estate: tuple[FictionalCampaign, ...] = ESTATE,
) -> PresetArtifactPublicationRequest:
    source = RetainedCampaignPresetArtifactSource(
        coverage=Coverage(),
        coverage_requirements=(REQUIREMENT,),
        temporal_policy=CAMPAIGN_POLICY,
        catalog=cast(
            "AnalyticalEntityCatalogPort",
            Catalog(
                EntityCatalogBrowseResult(
                    tuple(_alias(item) for item in estate),
                    truncated=False,
                    authorized_count=len(estate),
                )
            ),
        ),
        scope=AuthorizedAnalyticalScope(frozenset({estate[0].selection.campaign.commerce_scope})),
        compiler=compiler or FilterCompiler(estate),
        pointers=cast("PresetArtifactPointerStorePort", Pointers()),
        clock=lambda: __import__("datetime").datetime(2026, 8, 22, 12),
        labels=LABELS,
        channel_order=CHANNEL_ORDER,
    )
    request = source.publication_for(VARIANT)
    assert isinstance(request, PresetArtifactPublicationRequest)
    return request


def _filter_of(body: PresetArtifactSiblingBody) -> str:
    return cast("PresetArtifactFilterIdentity", body.identity).filter_id


@pytest.mark.covers("ADR-127")
def test_one_bake_binds_every_filter_sibling_under_one_pointer_and_its_custody() -> None:
    request = _bake()
    body = campaign_portfolio_artifact_from_payload(json.loads(request.body.payload_json))
    assert body.channel_filter is not None
    offered = tuple(option.filter_id for option in body.channel_filter.options)
    assert offered == (
        "all",
        "sponsored",
        "amazon_dsp",
        "amazon_sponsored_products",
        "amazon_sponsored_brands",
    )
    assert tuple(option.label for option in body.channel_filter.options)[:2] == (
        "All channels",
        "Sponsored ads (all sponsored channels)",
    )
    pairs = tuple(request.siblings)
    assert len(pairs) == request.siblings.count
    filters = {
        _filter_of(public): public
        for public, _ in pairs
        if isinstance(public.identity, PresetArtifactFilterIdentity)
    }
    assert set(filters) == set(offered) - {"all"}
    for filter_id, sibling in filters.items():
        assert sibling.key == (
            f"pages/campaigns/7d/{request.body.revision.value}/filters/{filter_id}.json"
        )
        assert sibling.revision == request.body.revision
    # ADR-145: All-channels custody is its own rows plus the sibling index, which binds every
    # filter sibling and every segment by digest; a filter's own custody carries its rows only.
    custody = staged_custody(request)
    assert custody is not None
    header, _ = split_parent_custody(custody)
    manifest = campaign_custody_manifest_from_payload(header)
    assert manifest.body_digest == request.body.digest
    indexed = {
        item.identity: item
        for shard, entry in staged_index(request)
        for item in index_shard_descriptors(shard, entry)
    }
    assert set(indexed) == {public.identity for public, _ in pairs}
    custody_by_identity = {private.identity: private for _, private in pairs}
    for filter_id, sibling in filters.items():
        descriptor = indexed[PresetArtifactFilterIdentity(filter_id)]
        assert descriptor.body_digest == sibling.digest
        private = custody_by_identity[descriptor.identity]
        assert descriptor.custody_digest == private.digest
        sibling_manifest = campaign_custody_manifest_from_payload(json.loads(private.payload_json))
        assert sibling_manifest.body_digest == sibling.digest
    # Each filter's segments live under its own filter in the segment identity.
    segments = [
        public.identity
        for public, _ in pairs
        if isinstance(public.identity, PresetArtifactSiblingIdentity)
    ]
    assert {item.filter_id for item in segments} == set(offered)
    first = next(public for public, _ in pairs if public.identity in segments)
    assert "/segments/" in first.key
    assert first.key.split("/segments/")[1].startswith(
        f"{cast('PresetArtifactSiblingIdentity', first.identity).filter_id}/"
    )
    # The pointer binds All channels, unchanged in shape, with the staged custody's digest.
    pointer = staged_pointer(request)
    assert (pointer.body_digest, pointer.custody_digest) == (request.body.digest, custody.digest)


@pytest.mark.covers("ADR-127")
def test_no_attributed_figure_is_totalled_on_a_multi_channel_sibling() -> None:
    siblings = {item.filter_id: item for item in filter_siblings()}
    for filter_id in ("all", "sponsored"):
        sibling = siblings[filter_id]
        payload = campaign_portfolio_artifact_payload(sibling)
        totals = {item["metric"] for item in cast("list[dict[str, object]]", payload["totals"])}
        assert totals == {"spend", "impressions"}
        trend = cast("dict[str, Any]", payload["trend"])
        assert set(trend["units"]) == {"spend"}
        assert all(set(point) == {"date", "spend"} for point in trend["points"])
        view = cast("dict[str, Any]", payload["channel_view"])
        # No per-channel or cross-channel attributed comparison of any kind.
        rows = cast("list[dict[str, object]]", [*view["by_channel"], *view["delivery"]])
        metrics = {
            cast("str", cell["metric"])
            for row in rows
            for value in row.values()
            if isinstance(value, dict)
            for cell in (cast("dict[str, object]", value),)
            if "metric" in cell
        }
        assert metrics.isdisjoint({"purchases", "portfolio_roas_attributed_14d", "attributed_roas"})
        assert metrics <= {"spend", "impressions", "clicks", "ctr", "cpc", "cpm"}
        not_added = view["not_added"]
        assert [tile["metric"] for tile in not_added] == sorted(_ATTRIBUTED_TOTALS)[::-1]
        assert all(set(tile) == {"label", "metric", "reason", "value_text"} for tile in not_added)
        assert {tile["value_text"] for tile in not_added} == {"Not added across channels"}
        # Each campaign row keeps its own credit: one campaign's ROAS is never a total.
        row_metrics = {
            item["metric"]
            for row in cast("list[dict[str, Any]]", payload["rows"])
            for item in row["comparisons"]
        }
        assert "attributed_roas" in row_metrics
    one_channel = siblings["amazon_dsp"]
    assert one_channel.channel_view is None
    assert {item.metric.value for item in one_channel.totals} >= _ATTRIBUTED_TOTALS
    # The contract itself refuses an attributed total on a multi-channel sibling.
    with pytest.raises(ValueError, match="contradictory"):
        replace(siblings["all"], totals=one_channel.totals)


@pytest.mark.covers("ADR-127", "ADR-121")
def test_delivery_changes_are_computed_from_unrounded_summed_operands() -> None:
    estate = (
        FictionalCampaign(
            "Fictional SP One",
            "amazon_sponsored_products",
            _f("6172", "100", 1, 100000, 5000),
            _f("6170.5", "100", 1, 100000, 5000),
        ),
        FictionalCampaign(
            "Fictional SP Two",
            "amazon_sponsored_products",
            _f("6172", "100", 1, 300000, 5000),
            _f("6170.5", "100", 1, 300000, 5000),
        ),
        FictionalCampaign(
            "Fictional SB One",
            "amazon_sponsored_brands",
            _f("10", "20", 1, 1000, 10),
            _f("10", "20", 1, 1000, 10),
        ),
    )
    everything = filter_siblings(estate)[0]
    assert everything.channel_view is not None
    products = everything.channel_view.delivery[0]
    assert products.channel == "amazon_sponsored_products"
    # CPC: 12,344 / 10,000 = 1.2344 against 12,341 / 10,000 = 1.2341. Both read $1.23 at two
    # places, but the unrounded values differ, so the change is a real rise, not "No change".
    assert products.cpc.current.value == Decimal("1.234400000000")
    assert products.cpc.prior.value == Decimal("1.234100000000")
    assert products.cpc.current.units.currency_code == "USD"
    assert products.cpc.current.grain.dimensions == ("period", "channel", "marketplace")
    change = products.cpc.change
    assert change is not None
    assert change.direction.value == "up"
    assert change.display_text != "No change"
    assert products.cpc.absolute_change == Decimal("0.000300000000")
    # CTR is the channel's summed clicks over its summed impressions (10,000 / 400,000), never
    # the mean of the two campaigns' own rates (0.05 and 0.0167).
    assert products.ctr.current.value == Decimal("0.025000000000")
    # CPM is spend per thousand impressions from the same sums.
    assert products.cpm.current.value == Decimal("30.860000000000")


@pytest.mark.covers("ADR-127", "ADR-087")
def test_an_undefined_delivery_ratio_has_its_own_legend_line() -> None:
    """A channel with no clicks has no CPC: its "—" has a visible legend line (ia §7.2 item 8)."""
    estate = (
        FictionalCampaign(
            "Fictional SP One",
            "amazon_sponsored_products",
            _f("100", "300", 3, 10000, 50),
            _f("90", "250", 2, 9000, 45),
        ),
        FictionalCampaign(
            "Fictional STV One",
            "amazon_sponsored_television",
            _f("40", "0", 0, 2000, 0),
            _f("30", "0", 0, 1500, 0),
        ),
    )
    everything = filter_siblings(estate)[0]
    view = everything.channel_view
    assert view is not None
    television = next(row for row in view.delivery if row.channel == "amazon_sponsored_television")
    assert television.cpc.current.value is None
    assert television.ctr.current.value == Decimal(0)
    assert view.legend == ("— Not applicable: no clicks in this window.",)
    payload = campaign_portfolio_artifact_payload(everything)
    served = cast("dict[str, Any]", payload["channel_view"])
    assert served["delivery_legend"] == ["— Not applicable: no clicks in this window."]
    # Where every ratio is defined, only the viewability reason has a line.
    defined = filter_siblings()[0].channel_view
    assert defined is not None
    assert defined.legend == ("— Not available: sponsored ads don't report viewability.",)


@pytest.mark.covers("ADR-127", "ADR-118")
def test_a_partly_reported_dsp_viewable_rate_names_its_days_and_withholds_its_change() -> None:
    missing = (date(2026, 8, 18),)
    everything = filter_siblings(viewability=ViewabilityCoverage(missing=missing))[0]
    view = everything.channel_view
    assert view is not None and view.viewability
    dsp, *sponsored = view.delivery
    assert dsp.viewable is not None and dsp.viewable.comparison is not None
    assert dsp.viewable.comparison.withheld
    assert dsp.viewable.comparison.change is None
    assert dsp.viewable.coverage_text == "6 of 7 days"
    # (4,200 + 10,500) / (6,300 + 16,800), from the summed operands.
    assert dsp.viewable.comparison.current.value == Decimal("0.636363636364")
    assert all(
        row.viewable is not None
        and row.viewable.comparison is None
        and row.viewable.absence_reason == "Not available: sponsored ads don't report viewability"
        for row in sponsored
    )
    assert view.legend == ("— Not available: sponsored ads don't report viewability.",)
    assert everything.evidence_status is not None
    assert "channel_viewable_rate" in withheld_slots(everything.evidence_status)
    # Without any DSP viewability for the window, the row says so; it never reads zero.
    absent = filter_siblings(viewability=ViewabilityCoverage(absent=True))[0]
    assert absent.channel_view is not None
    cell = absent.channel_view.delivery[0].viewable
    assert cell is not None and cell.comparison is None
    assert cell.absence_reason is not None and "Amazon DSP" in cell.absence_reason
    # An option without Amazon DSP declares no viewable rate at all.
    sponsored_only = next(item for item in filter_siblings() if item.filter_id == "sponsored")
    assert sponsored_only.channel_view is not None
    assert not sponsored_only.channel_view.viewability


@pytest.mark.covers("ADR-127", "ADR-118")
def test_a_partly_reported_prior_window_withholds_the_viewable_rate_change_and_says_why() -> None:
    """S8-8: a prior-window gap never drops the change silently; its own entry names the days."""
    prior_missing = (date(2026, 8, 10), date(2026, 8, 11))
    everything = filter_siblings(viewability=ViewabilityCoverage(prior_missing=prior_missing))[0]
    assert everything.channel_view is not None
    cell = everything.channel_view.delivery[0].viewable
    assert cell is not None and cell.comparison is not None
    # This window is whole, so its column names no covered days; the prior is read over the
    # days that arrived and the change is withheld.
    assert cell.coverage_text is None
    assert cell.comparison.withheld
    assert cell.comparison.change is None
    # (3,500 + 9,800) / (5,600 + 16,100), over the days that arrived.
    assert cell.comparison.prior.value == Decimal("0.612903225806")
    status = everything.evidence_status
    assert status is not None
    current = status.entry("channel_viewable_rate")
    prior = status.entry("channel_viewable_rate", "prior")
    assert not current.withheld
    assert prior.withheld
    assert prior.missing_dates == prior_missing
    assert prior.role_label == "Delivery per channel, prior window"
    assert prior.absence_text == "2 days not yet sent."
    assert prior.coverage_text == "5 of 7 days"
    # The entry sits right after the viewable rate's own, and the body round-trips.
    slots = [(entry.slot, entry.role) for entry in status.entries]
    assert slots[slots.index(("channel_viewable_rate", "current")) + 1] == (
        "channel_viewable_rate",
        "prior",
    )
    payload = json.loads(
        canonical_preset_artifact_json(campaign_portfolio_artifact_payload(everything))
    )
    assert campaign_portfolio_artifact_from_payload(payload) == everything
    # A prior window the source never sent serves no change, and its entry names every day.
    unsent = filter_siblings(
        viewability=ViewabilityCoverage(
            prior_missing=tuple(date(2026, 8, 8) + timedelta(days=offset) for offset in range(7))
        )
    )[0]
    assert unsent.channel_view is not None
    unsent_cell = unsent.channel_view.delivery[0].viewable
    assert unsent_cell is not None and unsent_cell.comparison is not None
    assert unsent_cell.comparison.prior.value is None
    assert unsent_cell.comparison.change is None
    assert unsent.evidence_status is not None
    assert unsent.evidence_status.entry("channel_viewable_rate", "prior").absence_text == (
        "7 days not yet sent."
    )
    # A whole prior window lists no prior entry and serves the change.
    whole = filter_siblings()[0]
    assert whole.evidence_status is not None
    assert all(entry.role == "current" for entry in whole.evidence_status.entries)


# --- serving --------------------------------------------------------------------------------


@dataclass
class _Stores:
    """Every object one publication wrote, read back as the S3 adapters do."""

    request: PresetArtifactPublicationRequest
    tamper: str | None = None

    @property
    def pointer(self) -> PresetArtifactPointer:
        return staged_pointer(self.request)

    def current(self, page: PresetArtifactPage, variant: PresetArtifactVariant):
        return (
            self.pointer if (page, variant) == (self.pointer.page, self.pointer.variant) else None
        )

    def roster(self, request: PresetArtifactPointerRosterRequest):
        return PresetArtifactPointerRosterResult(
            request, tuple(self.current(request.page, variant) for variant in request.variants)
        )

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactBody:
        assert pointer == self.pointer
        return self.request.body

    def read_sibling(self, request: PresetArtifactSiblingReadRequest) -> PresetArtifactSiblingBody:
        body = next(
            public
            for public, _ in self.request.siblings
            if public.identity == request.descriptor.identity
        )
        if (
            isinstance(body.identity, PresetArtifactFilterIdentity)
            and body.identity.filter_id == self.tamper
        ):
            payload = json.loads(body.payload_json)
            payload["total_count"] += 1
            return replace(body, payload_json=canonical_preset_artifact_json(payload))
        return body


@dataclass
class _Custody:
    request: PresetArtifactPublicationRequest

    def read(self, pointer: PresetArtifactPointer) -> PresetArtifactCustodyManifest:
        del pointer
        custody = staged_custody(self.request)
        assert custody is not None
        return custody

    def read_sibling(
        self, request: PresetArtifactSiblingReadRequest
    ) -> PresetArtifactSiblingCustody:
        identity = request.descriptor.identity
        if isinstance(identity, PresetArtifactIndexShardIdentity):
            return staged_index(self.request)[identity.shard_index][0]
        return next(private for _, private in self.request.siblings if private.identity == identity)


_TOKENS = tuple(f"{letter * 43}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _filter_runtime(
    module: _PresentationModule, stores: _Stores, assembler: object | None = None
) -> object:
    pages = RecordingPages(tokens=_TOKENS)
    return module.runtime_factory(
        catalog=RecordingCatalog(),
        page_context=pages,
        page_artifacts=pages,
        parent_capability_issuance=pages,
        campaign_capabilities=pages,
        scope=AuthorizedAnalyticalScope(frozenset({ESTATE[0].selection.campaign.commerce_scope})),
        clock=pages.clock,
        token_minter=iter(_TOKENS).__next__,
        schemas=SCHEMAS,
        metric_catalog=METRIC_CATALOG,
        preset_artifact_bodies=stores,
        preset_artifact_pointers=stores,
        preset_artifact_custody=_Custody(stores.request),
        dimension_labels=DIMENSION_LABELS,
        campaign_platform_labels=CAMPAIGN_PLATFORM_LABELS,
        insight_assembler=assembler,
    )


def _serve(  # noqa: PLR0913, PLR0917 -- one module, its stores, a body and a path
    module: _PresentationModule,
    stores: _Stores,
    body: dict[str, object],
    path: str = LIST_PATH,
) -> dict[str, Any]:
    event = _event(path, {"session_id": SESSION, **body})
    event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
    return module.handle(event, _filter_runtime(module, stores))


def _roster(
    module: _PresentationModule, stores: _Stores, artifact: CampaignPortfolioArtifact
) -> tuple[CampaignPortfolioRosterEntry, ...]:
    """Read one filter's whole roster through `roster_shard`, each shard of the body's revision."""
    assert artifact.roster == () and artifact.roster_shards is not None
    filter_id = artifact.filter_id
    entries: list[CampaignPortfolioRosterEntry] = []
    for shard in range(artifact.roster_shards.shards):
        response = _serve(module, stores, {"filter_id": filter_id, "roster_shard": shard})
        assert response["statusCode"] == 200, response["body"]
        body = _body(response)
        assert body["parent_revision"] == artifact.roster_shards.revision
        entries.extend(
            campaign_roster_shard_from_payload(
                body["shard"], shard, cast("str", body["parent_revision"])
            )
        )
    return tuple(entries)


@pytest.mark.covers("ADR-145")
def test_a_roster_shard_request_is_closed_and_bound_to_the_index(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """A shard the bake never wrote, a bool, or a shard beside a sort or segment is refused."""
    stores = _Stores(_bake())
    assert _serve(presentation_module, stores, {"roster_shard": 0})["statusCode"] == 200
    bodies: tuple[dict[str, object], ...] = (
        {"roster_shard": 1},
        {"roster_shard": True},
        {"roster_shard": -1},
        {"roster_shard": 0, "segment": 1},
        {"roster_shard": 0, "sort": {"field": "spend", "direction": "asc"}},
    )
    for body in bodies:
        with pytest.raises(ValidationError) as refused:
            _serve(presentation_module, stores, body)
        assert refused.value.description == "The campaign roster shard is invalid.", body


def _served_artifact(response: dict[str, Any]) -> CampaignPortfolioArtifact:
    assert response["statusCode"] == 200, response["body"]
    page = cast("dict[str, Any]", _body(response)["page"])
    return campaign_portfolio_artifact_from_payload(page["artifact"])


@pytest.mark.covers("ADR-127")
def test_each_filter_serves_its_own_rows_and_roster_from_the_one_pointer(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores(_bake())
    everything_response = _serve(presentation_module, stores, {})
    everything = _served_artifact(everything_response)
    dsp_response = _serve(presentation_module, stores, {"filter_id": "amazon_dsp"})
    dsp = _served_artifact(dsp_response)
    # The context chip names the option on screen, All channels included (mockup campaigns.html).
    chips = [
        cast("dict[str, Any]", _body(response)["page"])["page_context_label"]
        for response in (everything_response, dsp_response)
    ]
    assert chips == [
        "Campaigns · All channels · Aug 15\u201321, 2026",
        "Campaigns · Amazon DSP · Aug 15\u201321, 2026",
    ]
    assert everything.filter_id == "all" and dsp.filter_id == "amazon_dsp"
    assert everything.period == dsp.period
    assert {row.signature.channel for row in dsp.rows} == {"Amazon DSP"}
    # Each filter's roster is its own shards, served through the one pointer's index (ADR-145).
    rosters = {
        artifact.filter_id: _roster(presentation_module, stores, artifact)
        for artifact in (everything, dsp)
    }
    assert {entry.channel for entry in rosters["amazon_dsp"]} == {"Amazon DSP"}
    assert len(rosters["all"]) == len(ESTATE)
    assert len(rosters["amazon_dsp"]) == 2
    # One-channel totals are that channel's own: 700 + 1,400 of spend.
    spend = next(item for item in dsp.totals if item.metric is CampaignPortfolioMetric.SPEND)
    assert spend.current.value == Decimal(2100)
    assert spend.current.units.currency_code == "USD"


@pytest.mark.covers("ADR-127")
def test_an_undeclared_filter_is_refused_and_a_tampered_sibling_never_falls_back(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores(_bake())
    # A well-formed id the preset doesn't offer has its own code, the one the browser shows All
    # channels for; a value outside the grammar is the generic refusal.
    with pytest.raises(ValidationError) as unoffered:
        _serve(presentation_module, stores, {"filter_id": "amazon_sponsored_television"})
    assert unoffered.value.code == FILTER_NOT_OFFERED
    assert str(unoffered.value.code) == "ADLY-PRESENTATION-0005"
    for value in ("Amazon DSP", 7):
        with pytest.raises(ValidationError) as refused:
            _serve(presentation_module, stores, {"filter_id": value})
        assert refused.value.code == PRESENTATION_REFUSED
    # A segment out of range keeps the generic refusal, so it never reads as an unoffered channel.
    with pytest.raises(ValidationError) as segment:
        _serve(presentation_module, stores, {"filter_id": "amazon_dsp", "segment": 40})
    assert segment.value.code == PRESENTATION_REFUSED
    # A sibling whose bytes don't match its descriptor is the malformed-artifact refusal: the
    # page never serves All channels in its place.
    with pytest.raises(AdlyticsError) as malformed:
        _serve(
            presentation_module, replace(stores, tamper="amazon_dsp"), {"filter_id": "amazon_dsp"}
        )
    assert malformed.value.code == ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-127")
def test_a_format_10_body_is_refused_and_the_format_11_body_serves(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """The format-10 read (S8-M1) retired: the integrated deploy rebakes every pointer."""
    pointer, body, custody = _campaign_preset_artifact_with_custody()

    def serve(served: PresetArtifactBody) -> dict[str, Any]:
        event = _event(LIST_PATH, {"session_id": SESSION})
        event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        return presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(
                        replace(pointer, body_digest=served.digest), served
                    ),
                    preset_custody=RecordingCustodyStore(custody),
                ),
            ),
        )

    # Paired positive control: the format-11 body names its channel filter and serves.
    page = cast("dict[str, Any]", _body(serve(body))["page"])
    assert campaign_portfolio_artifact_from_payload(page["artifact"]).channel_filter is not None
    # The format-10 form: no channel keys, and a spend spark on every row.
    format_10 = cast("dict[str, Any]", json.loads(body.payload_json))
    del format_10["channel_filter"], format_10["channel_view"]
    with pytest.raises(ValueError) as unfiltered:
        campaign_portfolio_artifact_from_payload(format_10)
    assert str(unfiltered.value) == "campaign portfolio artifact fields are not exact."
    sparked = cast("dict[str, Any]", json.loads(body.payload_json))
    sparked["rows"][0]["spend_spark"] = {}
    with pytest.raises(ValueError) as spark:
        campaign_portfolio_artifact_from_payload(sparked)
    assert str(spark.value) == "campaign portfolio row fields are not exact."
    for retired in (format_10, {**format_10, "rows": sparked["rows"]}):
        retired_body = replace(body, payload_json=canonical_preset_artifact_json(retired))
        with pytest.raises(AdlyticsError) as refused:
            serve(retired_body)
        assert refused.value.code == ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-070")
def test_a_format_13_body_is_the_pages_absence_and_never_a_500(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    """After the format-14 deploy, a retained body without return lists is unpublished.

    The parent serves the designed window absence with the preset marked unavailable, a
    dialog over it is the domain refusal, and any other drift stays the malformed refusal.
    """
    pointer, body, custody = _campaign_preset_artifact_with_custody()

    def serve(served: PresetArtifactBody, path: str = LIST_PATH, **fields: object) -> Any:
        event = _event(path, {"session_id": SESSION, **fields})
        event["headers"] = {"Accept": "application/vnd.adlytics.presentation-v2+json"}
        return presentation_module.handle(
            event,
            _analytical_runtime(
                presentation_module,
                _AnalyticalRuntimeRequest(
                    RecordingCatalog(),
                    RecordingPages(),
                    ScenarioQueries(),
                    preset_artifacts=RecordingPresetArtifacts(
                        replace(pointer, body_digest=served.digest), served
                    ),
                    preset_custody=RecordingCustodyStore(custody),
                ),
            ),
        )

    # Paired positive control: the format-14 body serves its parent with both lists.
    page = cast("dict[str, Any]", _body(serve(body))["page"])
    lists = campaign_portfolio_artifact_from_payload(page["artifact"]).return_lists
    assert lists is not None and lists.highest and len(lists.highest) == len(lists.lowest)
    earlier = cast("dict[str, Any]", json.loads(body.payload_json))
    del earlier["return_lists"]
    earlier_body = replace(body, payload_json=canonical_preset_artifact_json(earlier))
    response = serve(earlier_body)
    assert response["statusCode"] == 200
    absent = _body(response)
    assert (absent["kind"], absent["reason_code"]) == ("window_unavailable", "preset_unavailable")
    presets = cast("dict[str, Any]", absent["temporal"])["presets"]
    availability = {
        preset["id"]: (preset["availability"], preset["reason_code"]) for preset in presets
    }
    assert availability[pointer.variant.value] == ("unavailable", "preset_unavailable")
    with pytest.raises(AdlyticsError) as dialog:
        serve(earlier_body, FOCUS_PATH, metric="spend")
    assert str(dialog.value.code) == "ADLY-PRESENTATION-0002"
    with pytest.raises(AdlyticsError) as deep:
        serve(
            earlier_body,
            DEEP_DIVE_PATH,
            slot_id="contribution",
            sort={"direction": "desc", "field": "spend"},
        )
    assert str(deep.value.code) == "ADLY-PRESENTATION-0002"
    assert deep.value.description == "No campaign results are published for this period."
    # Another missing key is not an earlier format: it stays the malformed-artifact refusal.
    del earlier["roster"]
    with pytest.raises(AdlyticsError) as malformed:
        serve(replace(body, payload_json=canonical_preset_artifact_json(earlier)))
    assert malformed.value.code == ARTIFACT_MALFORMED


@pytest.mark.covers("ADR-127")
def test_a_multi_channel_focus_refuses_purchases_and_roas_and_a_channel_serves_them(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    stores = _Stores(_bake())
    for metric in ("purchases", "portfolio_roas_attributed_14d"):
        with pytest.raises(ValidationError) as refused:
            _serve(presentation_module, stores, {"metric": metric}, FOCUS_PATH)
        assert refused.value.code == PRESENTATION_REFUSED
    spend = _serve(presentation_module, stores, {"metric": "spend"}, FOCUS_PATH)
    assert spend["statusCode"] == 200
    served = _serve(
        presentation_module, stores, {"metric": "purchases", "filter_id": "amazon_dsp"}, FOCUS_PATH
    )
    assert served["statusCode"] == 200
    focus = _body(served)
    assert cast("dict[str, Any]", focus["metric"])["id"] == "purchases"


_NOT_FILLED = (
    "Deep analysis accompanies a filled insight only; this insight is not filled for this view."
)
_SPONSORED_ADS = {"filter_id": "sponsored"}


@pytest.mark.covers("ADR-127", "ADR-076")
@pytest.mark.parametrize(
    ("filter_body", "slot_id", "state"),
    [
        ({}, "contribution", "filled"),
        ({}, "exposure", "filled"),
        ({}, "divergence", "unavailable"),
        (_SPONSORED_ADS, "contribution", "unavailable"),
        (_SPONSORED_ADS, "exposure", "filled"),
        (_SPONSORED_ADS, "divergence", "unavailable"),
    ],
    ids=lambda value: value if isinstance(value, str) else "sponsored-ads" if value else "all",
)
def test_a_multi_channel_portfolio_deep_dive_serves_from_the_baked_sibling(  # noqa: PLR0913, PLR0917 -- fixture and cases
    presentation_module: _PresentationModule,  # noqa: F811
    filter_body: dict[str, object],
    slot_id: str,
    state: str,
) -> None:
    """The portfolio insight dialog's deep section serves on All channels and Sponsored ads.

    campaign-detail-500 cause B: the deep inputs read purchases off a sibling that carries only
    spend and impressions, and the bare StopIteration was a 500 ADLY-UNEXPECTED-0000. A filled
    card's deep inputs name only the two totals the sibling carries; an unfilled one is the
    designed absence.
    """
    stores = _Stores(_bake())
    assembler = _RecordingDeepAssembler()
    event = _event(
        DEEP_DIVE_PATH,
        {
            "session_id": SESSION,
            "slot_id": slot_id,
            "sort": {"direction": "desc", "field": "spend"},
            **filter_body,
        },
    )
    module = presentation_module.module
    with patch.object(module, "RUNTIME", _filter_runtime(presentation_module, stores, assembler)):
        response = presentation_module.handler(event, object())

    assert response["statusCode"] == 200, response["body"]
    payload = _body(response)
    assert (payload["slot_id"], payload["state"]) == (slot_id, state)
    if state == "unavailable":
        assert payload["absence_reason"] == _NOT_FILLED
        assert assembler.requests == []
        return
    (request,) = assembler.requests
    assert {
        item.name
        for item in request.findings
        if item.name.endswith(WHOLE_PORTFOLIO_SUFFIX)
        and " for " not in item.name
        and " on " not in item.name
    } == {
        f"{label} {side}{WHOLE_PORTFOLIO_SUFFIX}"
        for label in ("Ad spend", "Impressions")
        for side in ("current", "prior", "change")
    }


@pytest.mark.covers("ADR-127", "ADR-122")
def test_the_scorecard_step_is_served_once_routed_and_withheld_while_it_is_not(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = _Stores(_bake())
    page = cast("dict[str, Any]", _body(_serve(presentation_module, stores, {}))["page"])
    # UI slice 9a routes the Scorecard: with no Scorecard pointer yet, the label names its
    # latest preset instead of dates (ADR-116 decision 4 fallback).
    step = cast("dict[str, Any]", page["scorecard_step"])
    assert step["label"] == "Open Scorecard \u00b7 latest 7 days"
    view = cast("dict[str, Any]", page["artifact"]["channel_view"])
    assert view["scorecard_line"] == "Return on ad spend per channel is on the Scorecard."
    # Paired: without a Scorecard page target no step is served.
    module = presentation_module.module

    def no_scorecard(*_args: object, **_kwargs: object) -> NextStepTargets:
        return NextStepTargets({}, frozenset(CHANNEL_ORDER))

    monkeypatch.setattr(module.NextStepTargets, "from_page_schemas", no_scorecard)
    withheld = cast("dict[str, Any]", _body(_serve(presentation_module, stores, {}))["page"])
    assert withheld["scorecard_step"] is None


@pytest.mark.covers("ADR-127", "ADR-122")
def test_once_the_scorecard_is_routed_detail_steps_to_its_channel_row_as_open_page(
    presentation_module: _PresentationModule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step is the retired `inspect-channel-context` action's replacement (ia §7.2 13)."""
    module = presentation_module.module
    declared = NextStepTargets(
        {
            "scorecard": NextStepPageTargets(
                "scorecard",
                frozenset({"7d", "30d"}),
                frozenset(),
                frozenset({"channel-table"}),
            )
        },
        frozenset(CHANNEL_ORDER),
    )

    def from_page_schemas(*_args: object, **_kwargs: object) -> NextStepTargets:
        return declared

    monkeypatch.setattr(module.NextStepTargets, "from_page_schemas", from_page_schemas)
    stores = _Stores(_bake())
    page = cast("dict[str, Any]", _body(_serve(presentation_module, stores, {}))["page"])
    step = cast("dict[str, Any]", page["scorecard_step"])
    assert step["kind"] == "open_page"
    assert step["target"] == {
        "page_id": "scorecard",
        "preset_id": "7d",
        "section_id": "channel-table",
    }
    assert step["label"] == "Open Scorecard \u00b7 latest 7 days"

    # From detail, the step highlights the open campaign's own channel row.
    def served_preset(_temporal: object) -> TemporalPresetId:
        return TemporalPresetId("30d")

    monkeypatch.setattr(module, "_served_preset", served_preset)
    dated = _ScorecardPointerStores(stores, DateRange(date(2026, 8, 13), date(2026, 8, 19)))
    runtime = SimpleNamespace(preset_artifact_pointers=dated)
    detail_step = cast(
        "dict[str, Any]",
        module._scorecard_step(None, "amazon_dsp", runtime),  # pyright: ignore[reportPrivateUsage]
    )
    # The label names the dates the Scorecard serves now, from its pointer.
    assert detail_step["label"] == "Open Scorecard \u00b7 Aug 13\u201319"
    assert detail_step["target"] == {
        "highlight_channel": "amazon_dsp",
        "page_id": "scorecard",
        "preset_id": "30d",
        "section_id": "channel-table",
    }


_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "frontend/test/fixtures"


def browser_fixture_responses(module: _PresentationModule) -> dict[str, dict[str, Any]]:
    """Serve the All channels and Amazon DSP pages the browser tests decode."""
    stores = _Stores(
        _bake(FilterCompiler(viewability=ViewabilityCoverage(missing=(date(2026, 8, 18),))))
    )
    return {
        "campaigns-all-v2.json": _body(_serve(module, stores, {})),
        "campaigns-dsp-v2.json": _body(_serve(module, stores, {"filter_id": "amazon_dsp"})),
    }


@pytest.mark.covers("ADR-127")
def test_the_browser_fixtures_are_the_servers_own_filter_responses(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    for name, served in browser_fixture_responses(presentation_module).items():
        committed = json.loads((_FIXTURES / name).read_text())
        assert committed == json.loads(json.dumps(served)), name


@dataclass
class _ScorecardPointerStores:
    """The Campaigns stores, plus a dated Scorecard pointer for each declared preset."""

    inner: _Stores
    window: DateRange

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        if request.page.value != "scorecard":
            return self.inner.roster(request)
        template = self.inner.pointer
        return PresetArtifactPointerRosterResult(
            request,
            tuple(
                replace(
                    template,
                    page=request.page,
                    variant=variant,
                    resolved_window=self.window,
                    comparison_window=None,
                )
                for variant in request.variants
            ),
        )
