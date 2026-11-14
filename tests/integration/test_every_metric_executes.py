# SPDX-License-Identifier: MIT-0
"""Every declared metric, asked the way chat asks it, executed against real Iceberg.

**Planning proves the model; only executing proves the SQL.** A wrong alias, a measure Athena
cannot sum, a ratio whose sides join on a column one spells differently: each plans and fails on
execution, and the customer sees a broken tool or, worse, an unqualified figure.

Parametrized from the metric dictionary, so a metric added tomorrow is covered without editing
this file. Nothing about a question is written down per metric either; each is derived from the
declarations the runtime itself reads:

- **Tooling** is composed as `measurement_entrypoint.py` composes chat's: served periods, pack
  coverage and rosters from the deployed selector, governed labels, the media taxonomy and the
  packs' cadence lags. A harness missing one of them tests a runtime nobody deploys.
- **Window** is the governed default a question naming no period gets
  (`MetricWindowDefaults`): the newest window the metric's own sources fully cover. A periodic
  metric is answered at the latest measured complete period instead (ADR-100), and the test
  derives that period itself from the served-period publications the answer read.
- **Grain**: a daily metric is asked by its first declared grain (`valid_grains`) that every fact
  it reads carries. A periodic metric is asked at its declared grains: a grain column a writer
  bounds is pinned to its first declared value, an open one (segment, cohort) is grouped, so
  every grain column is accounted for (ADR-024).
- **Pins**: `FILTER_POOL` on the columns the facts declare, with a value no writer of the
  metric's measures can produce replaced by the writer's own; plus the metric's required source
  options and mandatory selectors.

Values are checked loosely and shape strictly. What each figure *is* belongs to the tests that
pin a known number (`test_measurement_end_to_end`, `test_roas_end_to_end`,
`test_suppression_end_to_end`). Some declared kinds are refused by the catalog on purpose
(`ADLY-CATALOG-0002`) when the answer is planned, before the answer binds a publication; they
are asserted as refusals, and every answered metric here is their positive control. The
served-period and roster reads a request needs happen before planning by design
(`handle_batch`), so they are not part of that boundary.
"""

from __future__ import annotations

import pathlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from adapters.config import Settings
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import (
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.catalog import MetricWindowDefaults, PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.segment_labels import CanonicalSegmentLabels
from adapters.outbound.segment_roster import RosterReaders, SegmentRosterReader
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.producer.packs import load_query_packs
from domain.catalog.capability import Metric, MetricKind, references_in_definition
from domain.data_contracts.contract import DataContract
from domain.measurement.periodic import periodic_leaves
from domain.measurement.planning import GRAIN_COLUMNS, FactShape, GrainSetShape, leaf_references
from domain.narrative.shaped_response import ResolutionProvenance, ShapedResponse, UnitKind
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope, FieldOrigin
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PublicationSelection,
    PublicationSelectionRequest,
)
from ports.outbound.query import QueryPort
from ports.outbound.served_periods import ServedPeriods, ServedPeriodsRequest

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-028", "ADR-035", "ADR-100")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))

FILTER_POOL = {
    "marketplace_key": "ATVPDKIKX0DER",
    "channel_key": "sponsored_brands",
    "ad_product_key": "sponsored_brands",
    "campaign_key": "campaign_key-001",
    "amazon_asin": "B0FIXTUR01",
    "currency_code": "USD",
    "distributor_view": "MANUFACTURING",
    "selling_program": "RETAIL",
}
"""Every value a question might pin, applied per fact rather than wholesale.

Naming a column the fact does not declare is `ADLY-PLAN-0001`, and the facts differ —
`fct_retail_sales` has a distributor view and no campaign — so each metric receives only the
columns its own facts declare. `rows_suppressed` is deliberately absent: ADR-035 made the
discriminator the query's business.
"""

NOT_YET_COMPUTABLE = "ADLY-CATALOG-0002"
"""The catalog's refusal for a declared metric the live planner computes no figure for."""

METRIC_NAMES = tuple(sorted(metric.name for metric in CATALOG.metrics))
"""Read at import so the parametrization is the dictionary, not a copy of it."""


@dataclass(frozen=True, slots=True)
class _Question:
    """What one metric is asked by: its grain and its pins, both derived from declarations."""

    grains: tuple[str, ...]
    filters: dict[str, str]


@dataclass(frozen=True, slots=True)
class _DeclaredRefusal:
    """A metric the planner refuses by declaration: who it names, and what it offers instead."""

    requested: str
    offered: tuple[str, ...]


def _is_summed_across_facts(metric: Metric | None) -> bool:
    """Say whether a metric sums two facts' columns, which the planner never composes."""
    return (
        metric is not None
        and metric.kind is MetricKind.SIMPLE
        and len(references_in_definition(metric.definition)) > 1
        and "+" in metric.definition
    )


def _declared_refusal(metric: Metric) -> _DeclaredRefusal | None:
    """Return the refusal the dictionary declares for a metric, or None when it is answered.

    A product of operands is derived at publish time (ADR-032 amendment), a within-delivery ratio
    by the Reach and funnel bake (design §6.3), and a sum across two facts by its page bake; a
    ratio over such a sum is refused naming the sum.
    """
    if metric.kind is MetricKind.PRODUCT:
        return _DeclaredRefusal(metric.name, metric.operands)
    if metric.within_delivery is not None:
        return _DeclaredRefusal(metric.name, (metric.numerator,))
    if _is_summed_across_facts(metric):
        return _DeclaredRefusal(metric.name, ())
    for operand in (metric.numerator, metric.denominator):
        found = CATALOG.metric(operand) if operand else None
        if found is not None and _is_summed_across_facts(found):
            return _DeclaredRefusal(found.name, ())
    if _mixes_currency_columns(metric):
        return _DeclaredRefusal(metric.name, ())
    return None


def _mixes_currency_columns(metric: Metric) -> bool:
    """Say whether one fact's measures of a metric are labelled from two currency columns.

    A measure whose contract names its own currency column is labelled from it, any other money
    from `currency_code`; amounts in two columns' currencies are never combined in chat, and only
    a bake that checks both computes them (canonical model, `segment_roas_attributed`).
    """
    references = leaf_references(CATALOG, metric)
    for fact in {fact for fact, _ in references}:
        shape = FactShape.for_fact(fact, list(CONTRACTS))
        declared = dict(shape.currency_sources)
        defaulted = "currency_code" in shape.columns
        sources = {
            declared.get(column, "currency_code")
            for read, column in references
            if read == fact and (column in declared or defaulted)
        }
        if len(sources) > 1:
            return True
    return False


def _writers(metric: Metric) -> tuple[DataContract, ...]:
    """Every contract writing a column the metric's simple leaves read."""
    references = leaf_references(CATALOG, metric)
    return tuple(
        contract
        for contract in CONTRACTS
        if any(
            contract.target_table == fact and column in contract.target_columns
            for fact, column in references
        )
    )


def _question(metric: Metric) -> _Question:
    """Derive the grain and pins a metric is asked by from the dictionary and its writers."""
    shapes = [FactShape.for_fact(fact, list(CONTRACTS)) for fact in CATALOG.facts_read(metric)]
    declared = set(shapes[0].columns).intersection(*(shape.columns for shape in shapes[1:]))
    owned = [contract.owned_grain_values for contract in _writers(metric)]

    def bounded(column: str) -> bool:
        return bool(owned) and all(column in values for values in owned)

    filters: dict[str, str] = {}
    for column, value in FILTER_POOL.items():
        if column not in declared:
            continue
        # A literal or value domain every writer declares, none holding the pool's value, means
        # no row can match it: Viewable impressions is measured for amazon_dsp only.
        unproducible = bounded(column) and all(value not in values[column] for values in owned)
        filters[column] = owned[0][column][0] if unproducible else value
    filters.update(dict(metric.requires))
    # The served-period resolution pins the period grain itself (ADR-100).
    filters.update(
        {name: values[0] for name, values in metric.required_selectors if name != "time_grain"}
    )
    if not periodic_leaves(CATALOG, metric):
        # The first declared grain whose column every fact carries and no pin already fixes.
        daily = next(
            (
                grain
                for grain in metric.valid_grains
                if GRAIN_COLUMNS[grain] in declared and GRAIN_COLUMNS[grain] not in filters
            ),
            None,
        )
        assert daily is not None, f"{metric.name}: no declared grain is in every fact it reads"
        return _Question(grains=(daily,), filters=filters)
    grain_columns = {column for shape in shapes for column in shape.grain_for({})}
    grains: list[str] = []
    for grain in metric.valid_grains:
        column = GRAIN_COLUMNS[grain]
        if column in filters or column not in grain_columns:
            continue
        if bounded(column):
            filters[column] = owned[0][column][0]
        else:
            grains.append(grain)
    return _Question(grains=tuple(grains) or metric.valid_grains[:1], filters=filters)


def _latest_measured_period(
    metric: Metric, reads: tuple[tuple[ServedPeriodsRequest, ServedPeriods], ...]
) -> CalendarPeriod:
    """Return the period a question naming none must be answered at, from what was published.

    ADR-100: the latest complete period every contract the answer reads has a CURRENT
    publication for, at the week when the metric and its contracts have one, else the metric's
    first declared grain the contracts deliver; a cohort-only metric reads its newest vintage.
    Derived from the publications the served-period port returned, never from the resolver.
    """
    assert reads, f"{metric.name}: a periodic answer read no served periods"
    leaves = periodic_leaves(CATALOG, metric)
    declared = tuple(
        grain
        for grain in leaves[0].period_grains
        if all(grain in leaf.period_grains for leaf in leaves)
    )
    delivered = {
        grain
        for request, _ in reads
        for contract in request.contracts
        for shape in GrainSetShape.of(contract)
        for grain in shape.period_grains
    }
    grains = tuple(grain for grain in declared if grain in delivered) or declared
    if grains == (TimeGrain.CUSTOM,):
        grain = TimeGrain.CUSTOM
    else:
        grain = TimeGrain.WEEK if TimeGrain.WEEK in grains else grains[0]
    as_of = reads[0][0].now.date()
    measured = [
        period
        for _, served in reads
        for _, period in served.publications
        if period.grain is grain
        and (grain is TimeGrain.CUSTOM or period.end <= as_of)
        and all(
            (contract.contract_id, period) in read.publications
            for request, read in reads
            for contract in request.contracts
        )
    ]
    assert measured, f"{metric.name}: no {grain.value} is published by every contract it reads"
    return max(measured)


class _RecordingSelector:
    """Delegate to the deployed selector while retaining the selection this test bound."""

    def __init__(self, selector: PublicationSelector) -> None:
        self._selector = selector
        self.selection: PublicationSelection | None = None

    def select(self, request: PublicationSelectionRequest) -> PublicationSelection:
        self.selection = self._selector.select(request)
        return self.selection


class _CountingQueries:
    """A real Athena query adapter that counts every call it forwards."""

    def __init__(self, queries: AthenaQueryAdapter) -> None:
        self._queries = queries
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._queries, name)
        if not callable(target):
            return target

        def _forward(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return target(*args, **kwargs)

        return _forward


class _RecordingPeriods:
    """Delegate to the deployed served-period reader while retaining every read the answer made."""

    def __init__(self, periods: PublicationSelector) -> None:
        self._periods = periods
        self.reads: list[tuple[ServedPeriodsRequest, ServedPeriods]] = []

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        served = self._periods.served_periods(request)
        self.reads.append((request, served))
        return served


@dataclass(frozen=True, slots=True)
class _Deployed:
    """The deployed adapters and contract sets, read once per session as the entry point does."""

    selector: PublicationSelector
    athena: AthenaQueryAdapter
    labels: DimensionLabelResolver
    rosters: RosterReaders
    window_defaults: MetricWindowDefaults


@dataclass(frozen=True, slots=True)
class _Harness:
    """One test's tooling: the shared deployed adapters behind this test's own recorders."""

    publications: _RecordingSelector
    periods: _RecordingPeriods
    queries: _CountingQueries
    tooling: MeasurementTooling
    window_defaults: MetricWindowDefaults


@pytest.fixture(scope="session")
def deployed(settings: Settings) -> _Deployed:
    """Compose the deployed adapters once, as `measurement_entrypoint.py` composes chat's."""
    packs = load_source_packs(ROOT)
    bridge = load_commerce_identity_bridge(ROOT)
    taxonomy = load_media_taxonomy(ROOT)
    current = DynamoCurrentPublications(settings.semantic_publications_table)
    canonical = S3CanonicalAdapter(settings.semantic_v2_bucket)
    selector = PublicationSelector(
        current,
        packs,
        bridge,
        cadence_lag_days={
            pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(ROOT)
        },
        taxonomy=taxonomy,
    )
    athena = AthenaQueryAdapter(
        AthenaQueryTarget(
            workgroup=settings.athena_workgroup,
            database=settings.semantic_v2_current_database,
            reuse_minutes=settings.athena_result_reuse_max_age_minutes,
            wait=INTERACTIVE_WAIT,
            results=ResultTransport.PAGED,
        )
    )
    segment_labels = CanonicalSegmentLabels(current, canonical, packs, CONTRACTS, bridge)
    labels = DimensionLabelResolver(
        CanonicalEntityCatalog(
            EntityCatalogTooling(
                current=current,
                canonical=canonical,
                packs=packs,
                contracts=CONTRACTS,
                bridge=bridge,
                taxonomy=taxonomy,
            )
        ),
        AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope})),
        segments=segment_labels,
    )
    return _Deployed(
        selector=selector,
        athena=athena,
        labels=labels,
        rosters=RosterReaders(
            spend=SpendRosterReader(
                catalog=CATALOG,
                contracts=CONTRACTS,
                publications=selector,
                queries=athena,
                labels=ad_product_labels(taxonomy),
                clock=lambda: datetime.now(UTC),
                names=labels,
            ),
            segments=SegmentRosterReader(
                catalog=CATALOG,
                contracts=CONTRACTS,
                publications=selector,
                periods=selector,
                queries=athena,
                labels=segment_labels,
                clock=lambda: datetime.now(UTC),
            ),
        ),
        window_defaults=MetricWindowDefaults(
            PageWindowCatalog(current, packs, CONTRACTS, bridge), CATALOG
        ),
    )


@pytest.fixture
def harness(deployed: _Deployed, settings: Settings) -> _Harness:
    """Wrap the shared adapters in fresh recorders, so no observation crosses tests.

    Only the answer's own binding, served-period reads and statements are recorded. The roster
    readers share the deployed selector and adapter unrecorded, as the entry point shares them.
    """
    publications = _RecordingSelector(deployed.selector)
    periods = _RecordingPeriods(deployed.selector)
    counting = _CountingQueries(deployed.athena)
    tooling = MeasurementTooling(
        queries=cast("QueryPort", counting),
        publications=publications,
        labels=deployed.labels,
        query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
        periods=periods,
        pack_coverage=deployed.selector,
        rosters=deployed.rosters,
    )
    return _Harness(
        publications=publications,
        periods=periods,
        queries=counting,
        tooling=tooling,
        window_defaults=deployed.window_defaults,
    )


def _ask(metric: Metric, harness: _Harness) -> tuple[MeasurementToolRequest, DateRange]:
    """Build the question chat sends when the customer names the metric and no period."""
    window = harness.window_defaults.default_window(metric)
    assert window is not None, f"{metric.name}: its sources have published no coverage"
    question = _question(metric)
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=CATALOG,
        contracts=CONTRACTS,
        metric_name=metric.name,
        grains=question.grains,
        window=window,
        computed_at=datetime.now(UTC),
        correlation_id=f"integration-sweep-{metric.name}",
        filters=question.filters,
        resolution_provenance=(
            ResolutionProvenance("current_period", FieldOrigin.GOVERNED_DEFAULT, str(window)),
        ),
    )
    return request, window


def _metric(name: str) -> Metric:
    metric = CATALOG.metric(name)
    assert metric is not None, f"{name} came from the dictionary and must resolve"
    return metric


@pytest.mark.parametrize("metric_name", METRIC_NAMES)
def test_the_metric_executes_and_returns_a_qualified_envelope(
    metric_name: str, harness: _Harness
) -> None:
    """The statement composes, Athena accepts it, and the answer qualifies itself.

    Zero rows is not a failure, because the fixtures do not populate every fact for every lens.
    A response that came back must still name its basis, its freshness and the exact period it
    covers, because an unqualified figure is what this platform exists not to produce.
    """
    metric = _metric(metric_name)
    refusal = _declared_refusal(metric)
    request, window = _ask(metric, harness)
    if refusal is not None:
        with pytest.raises(DomainError) as refused:
            handle(request, harness.tooling)
        error = refused.value
        assert type(error) is DomainError
        assert str(error.code) == NOT_YET_COMPUTABLE
        assert error.context == {"requested": refusal.requested, "reason": "not_yet_computable"}
        if refusal.offered:
            assert error.user_visible_description.endswith(
                f"Available instead: {', '.join(refusal.offered)}."
            )
        # Refused by the catalog when the answer is planned, so the answer bound no publication.
        assert harness.publications.selection is None
        return

    response: ShapedResponse = handle(request, harness.tooling)

    metadata = response.metadata
    assert metadata.measurement_basis, "an answer must name the system it measured"
    assert metadata.freshness_status is not None
    (asked,) = metadata.requested_coverage_intervals
    assert metadata.actual_coverage_intervals == (asked,)
    assert metadata.coverage_holes == ()
    if periodic_leaves(CATALOG, metric):
        # Never an older complete period: that would present stale figures as current.
        latest = _latest_measured_period(metric, tuple(harness.periods.reads))
        assert asked == latest.window
    else:
        assert asked == window
    assert metadata.loaded_at is not None
    assert metadata.loaded_at.tzinfo is not None
    selected = harness.publications.selection
    assert selected is not None
    assert all(bound.primary_ids for bound in selected.plans)
    assert all(bound.logical.join is None or bound.joined_ids for bound in selected.plans)
    assert sum(harness.queries.calls.values()) >= 1
    assert response.narrative, "an unqualified figure is the failure mode this product avoids"
    assert metadata.row_count == len(response.data.values)

    if response.data.values:
        assert response.data.grain.dimensions == tuple(
            GRAIN_COLUMNS[grain] for grain in request.grains
        )
        units = response.data.units
        assert units is not None, "a figure with no units cannot be read safely"
        if units.kind is UnitKind.CURRENCY:
            assert units.currency_code, "money must name its currency"
    else:
        # An empty result carries no units: there is nothing to denominate.
        assert response.data.units is None


def test_the_declared_refusals_are_exactly_the_dictionary_kinds() -> None:
    """The refusal branch above covers only what the dictionary declares, never a metric that fails.

    Pinned by name, so a metric moving into or out of a refused kind is a reviewed change here.
    """
    refused = {name for name in METRIC_NAMES if _declared_refusal(_metric(name)) is not None}
    assert refused == {
        "dsp_only_reach_share",
        "overlap_share",
        "overlap_share_all_ids",
        "purchaser_rate_index_vs_sponsored_only",
        "returns_value_estimate_retail",
        "roas_incl_halo_by_frequency",
        "segment_roas_attributed",
        "window_spend_in_frequency_bucket",
    }
