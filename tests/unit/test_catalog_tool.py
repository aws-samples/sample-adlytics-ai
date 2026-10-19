# SPDX-License-Identifier: MIT-0
"""The catalog tool: what can be asked, at what grain, and why anything else is refused.

The first tool in the repository, so these tests pin decisions every later tool inherits.
Three are worth naming.

A refusal is **raised**, not returned as an empty response. Zero rows means "we looked and
there was nothing"; a refusal means "we did not look, because" — and since no query ran, a
response would have to invent the measurement basis and coverage window `Metadata` requires.

A capability answer's `units` describe the **response**, not the metrics it lists. A listing
spanning currency, count and ratio has no single denomination, and picking one described
metric's units would be the mistake.

`mixes_basis` on a mixed listing is **correct rather than noise**. Those metrics come from
measurement systems that do not reconcile with each other by design, and a user choosing
between them should be told before they ask.
"""

import datetime as dt
import pathlib
from dataclasses import dataclass, replace

import pytest

from adapters.inbound.tool_handlers.catalog import CatalogTool, CatalogToolRequest, handle
from adapters.outbound.metric_dictionary import load_catalog
from domain.capabilities.paid_features import PaidFeatureState
from domain.catalog.answers import (
    AnswerContext,
    DescribeMetricRequest,
    ListMetricsRequest,
    describe_metric,
    list_metrics,
)
from domain.catalog.capability import (
    Catalog,
    CatalogError,
    DeferredMetric,
    DeploymentRefusalRequest,
    InventoryCategory,
    Metric,
    MetricKind,
    Refusal,
    RefusalReason,
    facts_in_definition,
    references_in_definition,
)
from domain.narrative.publication import (
    AnswerPublicationRequest,
    CandidateNarration,
    RenderingKind,
    validate_answer_publication,
)
from domain.narrative.shaped_response import FreshnessStatus, UnitKind
from domain.semantics.compatibility import (
    AggregationKind,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    SemanticAxis,
)
from domain.shared_kernel.errors import DomainError, ValidationError
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit]

NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _context() -> AnswerContext:
    return AnswerContext(computed_at=NOW, correlation_id="corr-1")


def _test_policy() -> AggregationPolicy:
    axes = frozenset({SemanticAxis.TIME, SemanticAxis.CAMPAIGN})
    return AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=axes,
        axes={axis: AxisPolicy(AxisBehavior.ADDITIVE, True, True, True) for axis in axes},
    )


def _simple(name: str, **overrides: object) -> Metric:
    defaults: dict[str, object] = {
        "name": name,
        "label": name.replace("_", " ").title(),
        "kind": MetricKind.SIMPLE,
        "unit_kind": UnitKind.COUNT,
        "measurement_basis": (MeasurementBasis.AD_ATTRIBUTED,),
        "valid_grains": ("date", "campaign"),
        "definition": f"sum(fct_ad_exposure.{name})",
        # Required on every simple metric, so the factory supplies them rather than each
        # test restating a provenance it is not asserting about.
        "source_system": "amc",
        "source_datasets": ("dsp_impressions",),
        "aggregation_policy": _test_policy(),
    }
    return Metric(**(defaults | overrides))  # type: ignore[arg-type]


def _catalog(*metrics: Metric, deferred: tuple[DeferredMetric, ...] = ()) -> Catalog:
    declared = metrics or (_simple("impressions"),)
    if not any(metric.is_default_roas for metric in declared):
        declared = (replace(declared[0], is_default_roas=True), *declared[1:])
    return Catalog(
        metrics=declared,
        deferred=deferred,
        inventory_categories=(
            InventoryCategory(
                category_id="test_metrics",
                label="Test metrics",
                display_order=1,
                metric_ids=tuple(dict.fromkeys(metric.name for metric in declared)),
            ),
        ),
    )


def _deferred(name: str) -> DeferredMetric:
    return DeferredMetric(
        name=name,
        label=name.replace("_", " ").title(),
        kind=MetricKind.RATIO,
        unit_kind=UnitKind.RATIO,
    )


def _paid_state(*, observed: bool) -> PaidFeatureState:
    return PaidFeatureState(
        feature_observed=observed,
        period_published=False,
        expected_after=dt.date(2026, 10, 2),
        latest_available_period=None,
    )


@dataclass(frozen=True)
class _RefusalCodeCase:
    catalog: Catalog
    name: str
    grain: str | None
    filters: dict[str, str] | None
    reason: RefusalReason
    number: int


class TestFactResolution:
    def test_a_definition_names_the_tables_it_reads(self) -> None:
        assert facts_in_definition("sum(fct_ad_exposure.spend)") == ("fct_ad_exposure",)

    def test_a_table_named_twice_appears_once(self) -> None:
        """Deduplicated, so `facts_read` is a set of sources rather than a hit count."""
        expression = "sum(fct_ad_exposure.spend) / sum(fct_ad_exposure.impressions)"
        assert facts_in_definition(expression) == ("fct_ad_exposure",)

    def test_a_reference_carries_its_column(self) -> None:
        """check-lineage needs the column to verify a contract produces it."""
        assert references_in_definition("sum(fct_retail_sales.units)") == (
            ("fct_retail_sales", "units"),
        )

    def test_something_that_is_not_a_canonical_table_is_not_a_fact(self) -> None:
        # The prefixes are the canonical model's own. Matching anything dotted would report
        # a Python attribute access as a fact read.
        assert facts_in_definition("sum(orders.total)") == ()

    def test_a_ratio_resolves_its_facts_through_its_operands(self) -> None:
        """The case that makes this live on Catalog rather than Metric.

        `roas_blended` reads two facts and names neither: its operands are metric names, so
        answering "what does this read" needs the whole dictionary.
        """
        catalog = _catalog(
            _simple("ad_spend"),
            _simple("retail_revenue", definition="sum(fct_retail_sales.revenue)"),
            Metric(
                name="roas_blended",
                label="ROAS (blended)",
                kind=MetricKind.RATIO,
                unit_kind=UnitKind.RATIO,
                measurement_basis=(MeasurementBasis.RETAIL_ACTUAL, MeasurementBasis.AD_ATTRIBUTED),
                valid_grains=("date",),
                numerator="retail_revenue",
                denominator="ad_spend",
            ),
        )
        blended = catalog.metric("roas_blended")
        assert blended is not None
        assert catalog.facts_read(blended) == ("fct_retail_sales", "fct_ad_exposure")

    def test_a_ratio_operand_must_be_a_declared_metric(self) -> None:
        """A raw column has no source or semantic policy, so it is not a valid operand."""
        with pytest.raises(CatalogError, match="unknown metric operands"):
            _catalog(
                _simple("clicks"),
                Metric(
                    name="ctr",
                    label="CTR",
                    kind=MetricKind.RATIO,
                    unit_kind=UnitKind.RATIO,
                    measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
                    valid_grains=("date",),
                    numerator="clicks",
                    denominator="fct_ad_exposure.impressions",
                ),
            )


class TestMetricDeclaration:
    def test_a_metric_valid_at_no_grain_is_rejected(self) -> None:
        """Valid at nothing cannot be asked for, so declaring none is a mistake."""
        with pytest.raises(CatalogError, match="valid grain"):
            _simple("thing", valid_grains=())

    def test_a_metric_without_a_measurement_basis_is_rejected(self) -> None:
        # It reaches Metadata.measurement_basis, and mixes_basis is derived from it — a
        # missing basis turns a response needing a caveat into one that appears not to.
        with pytest.raises(CatalogError, match="measurement_basis"):
            _simple("thing", measurement_basis=())

    def test_a_ratio_without_both_operands_is_rejected(self) -> None:
        with pytest.raises(CatalogError, match="numerator and a denominator"):
            _simple("thing", kind=MetricKind.RATIO, definition="")

    def test_metric_aliases_require_an_immutable_tuple(self) -> None:
        with pytest.raises(CatalogError, match="immutable tuple"):
            _simple("thing", aliases=["sales"])

    def test_a_catalog_cannot_declare_a_metric_both_computable_and_deferred(self) -> None:
        """Guessing which is right would make a metric silently unavailable."""
        with pytest.raises(CatalogError, match="both computable and deferred"):
            _catalog(
                _simple("ad_spend"),
                deferred=(
                    DeferredMetric(
                        name="ad_spend",
                        label="Ad spend",
                        kind=MetricKind.SIMPLE,
                        unit_kind=UnitKind.CURRENCY,
                    ),
                ),
            )

    @pytest.mark.covers("ADR-062")
    def test_a_deferred_surface_normalizing_onto_a_computable_one_is_rejected(self) -> None:
        """Deferred resolution runs first, so the collision would silently shadow the metric."""
        with pytest.raises(CatalogError, match="normalizes onto a computable"):
            _catalog(
                _simple("ad_spend", aliases=("total spend",)),
                deferred=(
                    DeferredMetric(
                        name="spend_total",
                        label="Total Spend",
                        kind=MetricKind.SIMPLE,
                        unit_kind=UnitKind.CURRENCY,
                    ),
                ),
            )

    def test_a_catalog_rejects_a_duplicate_metric_name(self) -> None:
        with pytest.raises(CatalogError, match="declared twice"):
            _catalog(_simple("ad_spend"), _simple("ad_spend"))

    def test_a_catalog_requires_exactly_one_default_roas(self) -> None:
        with pytest.raises(CatalogError, match="exactly one metric"):
            Catalog(metrics=(_simple("ad_spend"),), deferred=(), inventory_categories=())
        with pytest.raises(CatalogError, match="exactly one metric"):
            Catalog(
                metrics=(
                    _simple("first", is_default_roas=True),
                    _simple("second", is_default_roas=True),
                ),
                deferred=(),
                inventory_categories=(),
            )

    def test_the_unique_default_roas_is_directly_available(self) -> None:
        catalog = _catalog(_simple("ad_spend"))
        assert catalog.default_roas().name == "ad_spend"


class TestMetricLanguage:
    def test_candidates_match_canonical_name_label_and_alias_exactly(self) -> None:
        catalog = _catalog(
            _simple(
                "attributed_return",
                label="Attributed ROAS",
                aliases=("ad-attributed ROAS",),
            )
        )

        assert [metric.name for metric in catalog.metric_candidates("ATTRIBUTED_RETURN")] == [
            "attributed_return"
        ]
        assert [metric.name for metric in catalog.metric_candidates("attributed-roas")] == [
            "attributed_return"
        ]
        assert [metric.name for metric in catalog.metric_candidates("AD attributed ROAS")] == [
            "attributed_return"
        ]

    def test_candidates_do_not_fuzzy_or_partially_match(self) -> None:
        catalog = _catalog(_simple("attributed_return", aliases=("ad-attributed ROAS",)))
        assert catalog.metric_candidates("attributed") == ()
        assert catalog.metric_candidates("ROAS") == ()

    def test_a_shared_alias_returns_deterministic_ambiguity_candidates(self) -> None:
        catalog = _catalog(
            _simple("retail_revenue", aliases=("sales",)),
            _simple("attributed_revenue", aliases=("sales",)),
        )
        assert [metric.name for metric in catalog.metric_candidates("SALES")] == [
            "attributed_revenue",
            "retail_revenue",
        ]


@pytest.mark.covers("ADR-010")
class TestRefusals:
    @pytest.mark.covers("ADR-010")
    @pytest.mark.parametrize(
        "case",
        (
            _RefusalCodeCase(
                catalog=_catalog(_simple("ad_spend")),
                name="unknown metric",
                grain=None,
                filters=None,
                reason=RefusalReason.UNKNOWN_METRIC,
                number=1,
            ),
            _RefusalCodeCase(
                catalog=_catalog(
                    _simple("ad_spend"),
                    deferred=(
                        DeferredMetric(
                            name="roas_incremental",
                            label="Incremental ROAS",
                            kind=MetricKind.RATIO,
                            unit_kind=UnitKind.RATIO,
                        ),
                    ),
                ),
                name="Incremental ROAS",
                grain=None,
                filters=None,
                reason=RefusalReason.NOT_YET_COMPUTABLE,
                number=2,
            ),
            _RefusalCodeCase(
                catalog=_catalog(
                    _simple(
                        "conversion_rate_retail",
                        requires=(("distributor_view", "MANUFACTURING"),),
                    )
                ),
                name="conversion_rate_retail",
                grain=None,
                filters=None,
                reason=RefusalReason.REQUIRES_SOURCE_OPTION,
                number=4,
            ),
            _RefusalCodeCase(
                catalog=_catalog(_simple("ad_spend", valid_grains=("date",))),
                name="ad_spend",
                grain="campaign",
                filters=None,
                reason=RefusalReason.GRAIN_NOT_SUPPORTED,
                number=3,
            ),
        ),
    )
    def test_each_catalog_refusal_reason_has_its_stable_public_error_code(
        self, case: _RefusalCodeCase
    ) -> None:
        """Error numbers are telemetry dimensions, so a silent renumber corrupts aggregates."""
        refusal = case.catalog.refusal_for(case.name, grain=case.grain, filters=case.filters)
        assert refusal is not None
        error = refusal.to_error()
        assert refusal.reason is case.reason
        assert error.code.area == "CATALOG"
        assert error.code.number == case.number

    @pytest.mark.covers("ADR-107")
    def test_an_unobserved_paid_feature_refuses_as_not_observed_in_deployment(self) -> None:
        """A "not licensed here" refusal differs from "impossible" (Q-07), with its own code."""
        catalog = _catalog(_simple("segment_buyers"))
        unobserved = _paid_state(observed=False)

        refusal = catalog.refusal_in_deployment(
            DeploymentRefusalRequest(
                "segment_buyers", paid_feature_states={"segment_buyers": unobserved}
            )
        )

        assert refusal is not None
        assert refusal.reason is RefusalReason.NOT_OBSERVED_IN_DEPLOYMENT
        error = refusal.to_error()
        assert isinstance(error, DomainError)
        assert (error.code.area, error.code.number) == ("CATALOG", 5)
        assert error.context == {
            "requested": "segment_buyers",
            "reason": "not_observed_in_deployment",
        }
        assert "isn't subscribed" in refusal.explanation
        assert "doesn't mean" in refusal.explanation

    @pytest.mark.covers("ADR-107")
    def test_an_observed_paid_feature_or_an_unlisted_metric_is_not_refused(self) -> None:
        """Paired control: the same metric answers once a delivery has been observed."""
        catalog = _catalog(_simple("segment_buyers"))

        observed = catalog.refusal_in_deployment(
            DeploymentRefusalRequest(
                "segment_buyers",
                paid_feature_states={"segment_buyers": _paid_state(observed=True)},
            )
        )
        unlisted = catalog.refusal_in_deployment(
            DeploymentRefusalRequest(
                "segment_buyers",
                paid_feature_states={"other_metric": _paid_state(observed=False)},
            )
        )

        assert observed is None
        assert unlisted is None
        # The platform-level answer is unchanged by the deployment argument.
        assert catalog.refusal_for("segment_buyers") is None

    @pytest.mark.covers("ADR-107")
    def test_an_unsupported_grain_wins_before_not_observed_in_deployment(self) -> None:
        """Impossible for anyone must never read as merely not licensed here."""
        catalog = _catalog(_simple("segment_buyers", valid_grains=("date",)))

        refusal = catalog.refusal_in_deployment(
            DeploymentRefusalRequest(
                "segment_buyers",
                grain="campaign",
                paid_feature_states={"segment_buyers": _paid_state(observed=False)},
            )
        )

        assert refusal is not None
        assert refusal.reason is RefusalReason.GRAIN_NOT_SUPPORTED

    @pytest.mark.covers("ADR-010")
    def test_unknown_metric_wins_before_unsupported_grain(self) -> None:
        """A typo must never be misleadingly blamed on a requested breakdown."""
        refusal = _catalog(_simple("ad_spend", valid_grains=("date",))).refusal_for(
            "ad_spned", grain="campaign"
        )
        assert refusal is not None
        assert refusal.reason is RefusalReason.UNKNOWN_METRIC

    @pytest.mark.covers("ADR-010")
    def test_missing_source_option_wins_before_unsupported_grain(self) -> None:
        """The first unmet requirement tells the caller what to repair first."""
        refusal = _catalog(
            _simple(
                "conversion_rate_retail",
                valid_grains=("date",),
                requires=(("distributor_view", "MANUFACTURING"),),
            )
        ).refusal_for("conversion_rate_retail", grain="campaign")
        assert refusal is not None
        assert refusal.reason is RefusalReason.REQUIRES_SOURCE_OPTION

    @pytest.mark.covers("ADR-010")
    def test_unsupported_grain_follows_satisfied_source_options(self) -> None:
        refusal = _catalog(
            _simple(
                "conversion_rate_retail",
                valid_grains=("date",),
                requires=(("distributor_view", "MANUFACTURING"),),
            )
        ).refusal_for(
            "conversion_rate_retail",
            grain="campaign",
            filters={"distributor_view": "MANUFACTURING"},
        )
        assert refusal is not None
        assert refusal.reason is RefusalReason.GRAIN_NOT_SUPPORTED

    @pytest.mark.covers("ADR-010")
    def test_satisfied_source_options_and_grain_produce_no_refusal(self) -> None:
        assert (
            _catalog(
                _simple(
                    "conversion_rate_retail",
                    valid_grains=("date",),
                    requires=(("distributor_view", "MANUFACTURING"),),
                )
            ).refusal_for(
                "conversion_rate_retail",
                grain="date",
                filters={"distributor_view": "MANUFACTURING"},
            )
            is None
        )

    def test_an_undeclared_name_is_refused_as_outside_the_contract(self) -> None:
        refusal = _catalog(_simple("ad_spend")).refusal_for("spend")
        assert refusal is not None
        assert refusal.reason is RefusalReason.UNKNOWN_METRIC
        # ValidationError, not DomainError: the request is outside its contract rather than
        # understood-but-unanswerable.
        assert isinstance(refusal.to_error(), ValidationError)

    @pytest.mark.covers("ADR-062")
    def test_a_deferred_customer_label_is_refused_as_pending_not_unknown(self) -> None:
        """The deployed failure: the customer-facing label read as a metric nobody declares."""
        catalog = _catalog(
            _simple("ad_spend"),
            deferred=(
                DeferredMetric(
                    name="roas_incremental",
                    label="Incremental ROAS",
                    kind=MetricKind.RATIO,
                    unit_kind=UnitKind.RATIO,
                ),
            ),
        )
        for reference in ("Incremental ROAS", "incremental roas", "INCREMENTAL ROAS"):
            refusal = catalog.refusal_for(reference)
            assert refusal is not None
            assert refusal.reason is RefusalReason.NOT_YET_COMPUTABLE

    @pytest.mark.covers("ADR-062")
    def test_a_deferred_governed_alias_is_refused_as_pending_not_unknown(self) -> None:
        """Industry shorthand is a declared surface, not a lucky model translation."""
        catalog = _catalog(
            _simple("ad_spend"),
            deferred=(
                DeferredMetric(
                    name="roas_incremental",
                    label="Incremental ROAS",
                    kind=MetricKind.RATIO,
                    unit_kind=UnitKind.RATIO,
                    aliases=("iROAS",),
                ),
            ),
        )
        for reference in ("iROAS", "iroas", "IROAS"):
            refusal = catalog.refusal_for(reference)
            assert refusal is not None
            assert refusal.reason is RefusalReason.NOT_YET_COMPUTABLE

    def test_a_deferred_alias_normalizing_onto_a_computable_surface_is_rejected(self) -> None:
        with pytest.raises(CatalogError, match="normalizes onto a computable"):
            _catalog(
                _simple("ad_spend", aliases=("spend",)),
                deferred=(
                    DeferredMetric(
                        name="future_metric",
                        label="Future metric",
                        kind=MetricKind.SIMPLE,
                        unit_kind=UnitKind.COUNT,
                        aliases=("Spend",),
                    ),
                ),
            )

    def test_a_deferred_name_is_refused_as_pending_not_unknown(self) -> None:
        """Answering "no such metric" to a name the dictionary lists would be wrong.

        It is the difference between a typo and a roadmap item, and a user can act on one.
        """
        refusal = _catalog(
            _simple("ad_spend"),
            deferred=(
                DeferredMetric(
                    name="roas_incremental",
                    label="Incremental ROAS",
                    kind=MetricKind.RATIO,
                    unit_kind=UnitKind.RATIO,
                ),
            ),
        ).refusal_for("roas_incremental")
        assert refusal is not None
        assert refusal.reason is RefusalReason.NOT_YET_COMPUTABLE
        assert isinstance(refusal.to_error(), DomainError)

    def test_a_grain_outside_valid_grains_is_refused_with_the_grains_that_work(self) -> None:
        """The roas_blended-at-campaign case: computable, plausible and wrong."""
        refusal = _catalog(_simple("ad_spend", valid_grains=("date",))).refusal_for(
            "ad_spend", grain="campaign"
        )
        assert refusal is not None
        assert refusal.reason is RefusalReason.GRAIN_NOT_SUPPORTED
        assert refusal.offer_instead == ("date",)

    def test_a_metric_needing_a_source_option_says_which(self) -> None:
        """Not a permanent refusal — it depends on how this customer's data was pulled."""
        refusal = _catalog(
            _simple("conversion_rate_retail", requires=(("distributor_view", "MANUFACTURING"),))
        ).refusal_for("conversion_rate_retail")
        assert refusal is not None
        assert refusal.reason is RefusalReason.REQUIRES_SOURCE_OPTION
        assert "distributor_view=MANUFACTURING" in refusal.explanation

    def test_a_typo_is_not_reported_as_a_grain_problem(self) -> None:
        """Checking the grain first would blame the grain for a misspelled name."""
        refusal = _catalog(_simple("ad_spend")).refusal_for("ad_spned", grain="nonsense")
        assert refusal is not None
        assert refusal.reason is RefusalReason.UNKNOWN_METRIC

    def test_a_supported_request_produces_no_refusal(self) -> None:
        assert _catalog(_simple("ad_spend")).refusal_for("ad_spend", grain="date") is None

    def test_an_unexplained_refusal_cannot_be_built(self) -> None:
        """A refusal without a reason is indistinguishable from a defect."""
        with pytest.raises(CatalogError, match="must state why"):
            Refusal(requested="x", reason=RefusalReason.UNKNOWN_METRIC, explanation="")

    def test_the_alternatives_reach_the_user_visible_description(self) -> None:
        # Both classes surface their real description, which is what makes raising lossless
        # compared with returning a refusal in the envelope.
        error = _catalog(_simple("ad_spend")).refusal_for("spend")
        assert error is not None
        raised = error.to_error()
        assert "ad_spend" in raised.user_visible_description

    def test_a_refusal_never_alarms(self) -> None:
        """A refused request is a product signal, not a health signal (ADR-010)."""
        for name, grain in (("spend", None), ("ad_spend", "campaign")):
            refusal = _catalog(_simple("ad_spend", valid_grains=("date",))).refusal_for(
                name, grain=grain
            )
            assert refusal is not None
            assert refusal.to_error().alarms is False


@pytest.mark.covers("ADR-006")
class TestTheEnvelope:
    def test_a_listing_carries_one_summary_row_per_governed_collection(self) -> None:
        """Detailed metric attributes remain available through the description operation."""
        catalog = _catalog(
            _simple("ad_spend"), _simple("clicks"), deferred=(_deferred("roas_incremental"),)
        )
        response = list_metrics(ListMetricsRequest(catalog=catalog, context=_context()))

        assert response.metadata.row_count == 2
        assert len(response.data.values) == 2
        statuses = [row["status"] for row in response.data.values]
        assert statuses == ["available", "not_yet_computable"]

    def test_metric_rows_are_not_duplicated_into_grounding_context(self) -> None:
        response = list_metrics(
            ListMetricsRequest(catalog=load_catalog(REPO_ROOT), context=_context())
        )

        expected_fields = {
            "collection",
            "label",
            "status",
        }
        assert all(set(row) == expected_fields for row in response.data.values)
        grounding = response.metadata.grounding
        assert grounding is not None
        assert [item.kind for item in grounding.renderings] == [RenderingKind.CLAIM]
        # Eight inventory categories, 64 metrics: shoppers_and_reach adds the two periodic
        # buyer counts and cost per new-to-brand buyer (FEAT-003, ADR-099), and attributed
        # conversions adds the halo-inclusive gateway count (FEAT-004, pack version 2).
        assert len(grounding.collections) == 8
        # 64 before pack 5, which adds 15 metrics to Shoppers and reach and window spend to the
        # deferred collection (design §6.3); packs 14 and 15 add 15 cohort metrics (design §6.6).
        assert sum(len(collection.items) for collection in grounding.collections) == 124

    def test_units_describe_the_response_not_the_metrics_listed(self) -> None:
        """A listing spanning currency, count and ratio has no single denomination.

        The response's own figure is a count of metrics. Each row states its metric's units
        as text, which is a description rather than a figure.
        """
        catalog = _catalog(_simple("ad_spend", unit_kind=UnitKind.CURRENCY))
        response = list_metrics(ListMetricsRequest(catalog=catalog, context=_context()))

        # Narrowed rather than asserted through `Optional`: units are now optional on an empty
        # result, and a capability answer always carries rows, so `None` here would be a defect
        # rather than a case to tolerate.
        units = response.data.units
        assert units is not None
        assert units.kind is UnitKind.COUNT
        assert units.currency_code is None
        assert response.data.values[0]["label"] == "Test metrics"

    def test_a_capability_answer_states_its_grain(self) -> None:
        """Grain is mandatory precisely so an unstated one is impossible, listings included."""
        response = list_metrics(ListMetricsRequest(catalog=_catalog(), context=_context()))
        assert response.data.grain.dimensions == ("metric",)

    def test_the_coverage_window_is_zero_width_because_no_period_was_consulted(self) -> None:
        """Inventing a range would imply data was read. The real window arrives with 2.6b."""
        response = list_metrics(ListMetricsRequest(catalog=_catalog(), context=_context()))
        assert response.metadata.coverage_start == NOW
        assert response.metadata.coverage_end == NOW

    def test_a_capability_answer_is_fresh_and_suppresses_nothing(self) -> None:
        # The catalogue ships with the code, so it cannot lag its source the way a loaded
        # fact can. No fact was read, so no row was withheld.
        response = list_metrics(ListMetricsRequest(catalog=_catalog(), context=_context()))
        assert response.metadata.freshness_status is FreshnessStatus.FRESH
        assert response.metadata.suppression.rows_suppressed is False

    def test_a_listing_names_the_facts_its_metrics_would_read(self) -> None:
        """The envelope requires it, and it is useful: a caller learns the sources first."""
        catalog = _catalog(
            _simple("ad_spend"), _simple("revenue", definition="sum(fct_retail_sales.revenue)")
        )
        response = list_metrics(ListMetricsRequest(catalog=catalog, context=_context()))
        assert response.metadata.facts_read == ("fct_ad_exposure", "fct_retail_sales")

    def test_a_mixed_listing_warns_that_the_systems_do_not_reconcile(self) -> None:
        """Correct rather than a false positive on a capability answer.

        The listed metrics come from measurement systems that do not reconcile with each
        other by design, and a user choosing between them should know before asking.
        """
        catalog = _catalog(
            _simple("ad_spend"),
            _simple(
                "retail_revenue",
                definition="sum(fct_retail_sales.revenue)",
                measurement_basis=(MeasurementBasis.RETAIL_ACTUAL,),
            ),
        )
        response = list_metrics(ListMetricsRequest(catalog=catalog, context=_context()))

        assert response.metadata.mixes_basis is True
        assert response.requires_basis_warning is True
        assert any("reconcile" in sentence for sentence in response.narrative)

    def test_a_single_basis_listing_carries_no_reconciliation_warning(self) -> None:
        response = list_metrics(
            ListMetricsRequest(catalog=_catalog(_simple("ad_spend")), context=_context())
        )
        assert response.metadata.mixes_basis is False
        assert not any("reconcile" in sentence for sentence in response.narrative)

    def test_a_listing_says_which_metrics_are_merely_pending(self) -> None:
        response = list_metrics(
            ListMetricsRequest(
                catalog=_catalog(_simple("ad_spend"), deferred=(_deferred("roas_incremental"),)),
                context=_context(),
            )
        )
        assert any("Roas Incremental" in sentence for sentence in response.narrative)
        assert not any("roas_incremental" in sentence for sentence in response.narrative)

    def test_a_listing_says_it_does_not_speak_to_coverage(self) -> None:
        """The capability half must not be read as a statement that a period is loaded."""
        response = list_metrics(ListMetricsRequest(catalog=_catalog(), context=_context()))
        assert any("separate question" in sentence for sentence in response.narrative)


class TestDescribingOneMetric:
    def test_describing_a_metric_returns_exactly_one_row(self) -> None:
        response = describe_metric(
            DescribeMetricRequest(
                catalog=_catalog(_simple("ad_spend")),
                context=_context(),
                metric_name="ad_spend",
            )
        )
        assert response.metadata.row_count == 1
        assert response.data.values[0]["metric"] == "ad_spend"

    def test_a_supported_grain_is_confirmed_in_the_narrative(self) -> None:
        response = describe_metric(
            DescribeMetricRequest(
                catalog=_catalog(_simple("ad_spend")),
                context=_context(),
                metric_name="ad_spend",
                grain="campaign",
            )
        )
        assert any("campaign grain is supported" in s for s in response.narrative)

    def test_a_halo_metric_says_the_default_must_be_shown_alongside_it(self) -> None:
        """It reads higher than the default by construction, so alone it overstates."""
        response = describe_metric(
            DescribeMetricRequest(
                catalog=_catalog(_simple("revenue_incl_halo", includes_brand_halo=True)),
                context=_context(),
                metric_name="revenue_incl_halo",
            )
        )
        assert any("shown alongside" in s for s in response.narrative)

    def test_describing_an_unsupported_grain_raises_rather_than_returning_zero_rows(self) -> None:
        """No query ran, so a response would have to invent a basis and a coverage window."""
        with pytest.raises(DomainError, match="requested campaign breakdown is unsupported"):
            describe_metric(
                DescribeMetricRequest(
                    catalog=_catalog(_simple("ad_spend", valid_grains=("date",))),
                    context=_context(),
                    metric_name="ad_spend",
                    grain="campaign",
                )
            )

    def test_a_describe_request_must_name_a_metric(self) -> None:
        with pytest.raises(CatalogError, match="must name a metric"):
            DescribeMetricRequest(catalog=_catalog(), context=_context(), metric_name="")


class TestAnswerContext:
    def test_a_naive_timestamp_is_rejected(self) -> None:
        # Metadata requires tz-aware timestamps; catching it here names the field.
        with pytest.raises(CatalogError, match="timezone-aware"):
            AnswerContext(computed_at=dt.datetime(2026, 8, 19), correlation_id="c")  # noqa: DTZ001

    def test_a_missing_correlation_id_is_rejected(self) -> None:
        with pytest.raises(CatalogError, match="correlation_id"):
            AnswerContext(computed_at=NOW, correlation_id="")


class TestTheToolHandler:
    def test_it_dispatches_a_listing(self) -> None:
        response = handle(
            CatalogToolRequest(
                tool=CatalogTool.LIST_METRICS,
                catalog=_catalog(_simple("ad_spend")),
                computed_at=NOW,
                correlation_id="c",
            )
        )
        assert response.metadata.row_count == 1
        grounding = response.metadata.grounding
        assert grounding is not None
        assert grounding.requires_claim_selection
        assert len(grounding.collections) == 1
        assert grounding.collections[0].required is True
        claims = tuple(item for item in grounding.renderings if item.kind is RenderingKind.CLAIM)
        assert len(claims) == 1
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", (claims[0].rendering_id,)),
                (grounding,),
            )
        )
        assert decision.released
        assert decision.released_text == claims[0].text

    def test_it_dispatches_a_description(self) -> None:
        response = handle(
            CatalogToolRequest(
                tool=CatalogTool.DESCRIBE_METRIC,
                catalog=_catalog(_simple("ad_spend")),
                computed_at=NOW,
                correlation_id="c",
                metric_name="ad_spend",
            )
        )
        assert response.data.values[0]["metric"] == "ad_spend"
        grounding = response.metadata.grounding
        assert grounding is not None
        descriptor = next(
            item for item in grounding.renderings if item.kind is RenderingKind.CONTEXT
        )
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", (descriptor.rendering_id,)),
                (grounding,),
            )
        )
        assert decision.released_text == descriptor.text

    def test_describing_nothing_is_a_wiring_fault_not_a_question(self) -> None:
        """A missing argument is malformed, unlike a name that simply does not exist."""
        with pytest.raises(ValidationError, match="requires a metric name"):
            handle(
                CatalogToolRequest(
                    tool=CatalogTool.DESCRIBE_METRIC,
                    catalog=_catalog(),
                    computed_at=NOW,
                    correlation_id="c",
                )
            )

    def test_it_accepts_no_identity_argument(self) -> None:
        """Identity is never a tool parameter: if the model fills it in, it can impersonate.

        Asserted on the field names so adding one is a test failure rather than a review miss.
        """
        fields = set(CatalogToolRequest.__dataclass_fields__)
        for forbidden in ("actor", "actor_id", "user", "user_id", "tenant", "brand", "subject"):
            assert forbidden not in fields


class TestTheShippedDictionary:
    """Reads the committed file. No network and no credentials, so it stays in the unit tier.

    Worth having as more than a smoke test: it is the only thing asserting that the dictionary
    the repository ships can actually be loaded, and every response above is built from types
    this parse produces.
    """

    def test_the_committed_dictionary_loads(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        assert len(catalog.metrics) > 0
        assert catalog.deferred

    @pytest.mark.covers("ADR-062")
    def test_the_deployed_deferred_label_is_refused_as_pending_not_unknown(self) -> None:
        """Exact deployed regression: "Incremental ROAS" is a roadmap item, not a typo."""
        with pytest.raises(DomainError, match="not built") as raised:
            describe_metric(
                DescribeMetricRequest(
                    catalog=load_catalog(REPO_ROOT),
                    context=_context(),
                    metric_name="Incremental ROAS",
                )
            )
        assert raised.value.context["reason"] == "not_yet_computable"

    @pytest.mark.covers("ADR-062")
    def test_shipped_governed_aliases_resolve_to_their_declared_metrics(self) -> None:
        """Common industry surfaces stay declared; removing one is a contract change."""
        catalog = load_catalog(REPO_ROOT)
        expected = {
            "iROAS": (),
            "spend": ("ad_spend",),
            "reach": ("reach_users",),
            "frequency": ("avg_frequency",),
            "AOV": ("aov_attributed",),
            "COGS": ("cogs_shipped",),
            "shipped revenue": ("retail_revenue_shipped",),
            "ordered units": ("units_ordered",),
            "NTB rate": ("ntb_rate_12mo",),
            "total ACoS": ("tacos",),
            "ASP": ("asp_shipped", "retail_asp_purchased"),
            "CVR": ("click_to_purchase_rate", "conversion_rate_retail"),
        }
        for reference, names in expected.items():
            assert tuple(m.name for m in catalog.metric_candidates(reference)) == names, reference
        assert [m.name for m in catalog.deferred_candidates("iROAS")] == ["roas_incremental"]
        assert [m.name for m in catalog.deferred_candidates("i-ROAS")] == ["roas_incremental"]

    def test_a_single_match_alias_describes_its_canonical_metric(self) -> None:
        """The description tool must not be stricter than the measurement resolver."""
        response = describe_metric(
            DescribeMetricRequest(
                catalog=load_catalog(REPO_ROOT),
                context=_context(),
                metric_name="ad-attributed ROAS",
            )
        )
        assert response.narrative[0].startswith("ROAS (attributed, 14 day)")

    def test_an_ambiguous_shared_alias_still_refuses_naming_what_exists(self) -> None:
        """Three metrics share "sales"; choosing one would guess the customer's intent."""
        with pytest.raises(ValidationError, match="not a metric this platform declares"):
            describe_metric(
                DescribeMetricRequest(
                    catalog=load_catalog(REPO_ROOT), context=_context(), metric_name="sales"
                )
            )

    def test_every_computable_metric_can_be_described(self) -> None:
        """A metric the dictionary declares must not fail the tool that lists it."""
        catalog = load_catalog(REPO_ROOT)
        for metric in catalog.metrics:
            if metric.requires or metric.required_selectors:
                # Refused for a stated reason rather than described. Exercised above.
                continue
            response = describe_metric(
                DescribeMetricRequest(catalog=catalog, context=_context(), metric_name=metric.name)
            )
            assert response.metadata.row_count == 1

    def test_every_computable_metric_resolves_to_at_least_one_fact(self) -> None:
        """A metric reading no fact cannot be computed, so it belongs under `deferred`."""
        catalog = load_catalog(REPO_ROOT)
        for metric in catalog.metrics:
            assert catalog.facts_read(metric), f"{metric.name} resolves to no fact"

    def test_the_default_roas_is_declared_exactly_once(self) -> None:
        """Two defaults means the agent picks one, and ADR-017 chose which."""
        catalog = load_catalog(REPO_ROOT)
        assert catalog.default_roas().name == "roas_attributed_14d"

    def test_reviewed_metric_aliases_are_governed_by_the_dictionary(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        assert [metric.name for metric in catalog.metric_candidates("ad attributed ROAS")] == [
            "roas_attributed_14d"
        ]
        assert [metric.name for metric in catalog.metric_candidates("blended ROAS")] == [
            "roas_blended"
        ]
        assert [metric.name for metric in catalog.metric_candidates("halo inclusive ROAS")] == [
            "roas_attributed_incl_halo_14d"
        ]

    def test_shared_aliases_preserve_sales_and_conversion_ambiguity(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        assert [metric.name for metric in catalog.metric_candidates("sales")] == [
            "attributed_revenue_14d",
            "retail_revenue_purchased",
            "retail_revenue_shipped",
        ]
        assert [metric.name for metric in catalog.metric_candidates("conversion rate")] == [
            "click_to_purchase_rate",
            "conversion_rate_retail",
        ]

    def test_only_reviewed_metrics_publish_customer_education_copy(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        assert {
            metric.name
            for metric in catalog.metrics
            if metric.customer_definition or metric.customer_caveat
        } == {
            # Every focusable metric carries a reviewed definition line: the metric-focus
            # dialog's identity head serves it from the dictionary, never from code.
            "ad_spend",
            "amazon_margin_proxy",
            "asp_shipped",
            # The periodic buyer counts carry the identity caveat (design §3f, ADR-099).
            "buyers_attributed",
            "buyers_new_to_brand_12mo",
            # Pack 5's shopper counts and sums carry the identity and association caveats
            # (design §6.3, §6.4; ADR-102).
            "detail_page_view_shoppers_in_window",
            "dsp_window_spend_on_shoppers",
            "impressions_in_window",
            "ntb_purchasers_in_window",
            "purchasers_in_window",
            "purchases_in_window",
            "revenue_incl_halo_in_window",
            "search_shoppers_in_window",
            "shoppers_reached",
            "sponsored_window_spend_on_shoppers",
            # Packs 6 and 7's shopper counts and sums carry the same caveats (design §6.3 D2).
            "dsp_window_spend_in_frequency_bucket",
            "impressions_in_frequency_bucket",
            "ntb_purchasers_in_frequency_bucket",
            "purchasers_in_frequency_bucket",
            "purchases_in_frequency_bucket",
            "revenue_incl_halo_in_frequency_bucket",
            "shoppers_in_frequency_bucket",
            "sponsored_window_spend_in_frequency_bucket",
            "window_spend_in_frequency_bucket",
            # Pack 8's segment counts and sums carry the overlap caveat: segment figures never
            # add up (design §6.5, ADR-103).
            "segment_attributed_revenue",
            "segment_impressions",
            "segment_purchasers",
            "segment_purchases",
            "segment_reach",
            "segment_spend",
            # Packs 10-12's buyer-profile counts carry the same caveat and the C2 basis label
            # (design §5.5, §6.5).
            "profiled_buyers_total",
            "profiled_exposed_total",
            "segment_buyers",
            "segment_buyers_new_to_brand",
            "segment_exposed_shoppers",
            # Packs 14 and 15's cohort counts and sums carry the identity and "ad-touched repeat
            # only" caveats (design §6.6, ADR-104).
            "cohort_buyers_account_ids",
            "cohort_buyers_new_to_brand_12mo",
            "cohort_first_repeat_buyers",
            "cohort_repeat_buyers",
            "cohort_repeat_buyers_account_ids",
            "cohort_repeat_buyers_cumulative",
            "cohort_repeat_buyers_cumulative_account_ids",
            "cohort_repeat_purchases",
            "cohort_repeat_revenue",
            "clicks",
            "cogs_shipped",
            "conversion_rate_retail",
            "cpc",
            "cpm",
            "ctr",
            "customer_returns",
            "fulfilment_rate",
            "glance_views",
            "impressions",
            "portfolio_roas_attributed_14d",
            "purchases",
            # Purchases, not people: "new-to-brand customers" read this metric (D2 run 3).
            "purchases_new_to_brand_12mo",
            # The gateway metric's halo and per-channel captions (design §6.2).
            "purchases_new_to_brand_incl_halo_12mo",
            "retail_revenue_shipped",
            "return_rate",
            "returns_value_estimate_retail",
            "revenue_ordered",
            "revenue_per_glance_view",
            "roas_attributed_14d",
            "units_ordered",
            "units_shipped",
        }

    @pytest.mark.parametrize(
        "case",
        [
            (
                "glance_views",
                "Glance views count customer views of the vendor's product detail pages.",
                "The Vendor Central traffic report does not expose a distributor-view option.",
                "Glance views is available as a simple metric reported in count. Its measurement "
                "basis is retail actual. Supported breakdowns are date, ecosystem, platform, "
                "product, marketplace. Glance views count customer views of the vendor's product "
                "detail pages. The Vendor Central traffic report does not expose a "
                "distributor-view option.",
            ),
            (
                "roas_attributed_14d",
                "Attributed ROAS is aggregate 14-day attributed revenue for advertised products "
                "divided by aggregate fee-inclusive ad spend.",
                "This is the default ROAS, excludes brand-halo revenue, is not available at "
                "product breakdown, and reflects ad-attributed revenue rather than total business "
                "revenue.",
                "ROAS (attributed, 14 day) is available as a ratio metric reported in ratio. Its "
                "measurement basis is ad attributed. Supported breakdowns are date, ecosystem, "
                "platform, channel group, channel, campaign, ad product, marketplace. Attributed "
                "ROAS is aggregate 14-day attributed revenue for advertised products divided by "
                "aggregate fee-inclusive ad spend. This is the default ROAS, excludes brand-halo "
                "revenue, is not available at product breakdown, and reflects ad-attributed "
                "revenue rather than total business revenue.",
            ),
        ],
    )
    def test_governed_customer_copy_crosses_the_exact_release_boundary(
        self,
        case: tuple[str, str, str, str],
    ) -> None:
        metric_name, customer_definition, customer_caveat, expected_descriptor = case
        catalog = load_catalog(REPO_ROOT)
        metric = catalog.metric(metric_name)
        assert metric is not None
        assert metric.customer_definition == customer_definition
        assert metric.customer_caveat == customer_caveat

        response = describe_metric(
            DescribeMetricRequest(catalog=catalog, context=_context(), metric_name=metric_name)
        )
        row = response.data.values[0]
        assert row["customer_definition"] == customer_definition
        assert row["customer_caveat"] == customer_caveat
        grounding = response.metadata.grounding
        assert grounding is not None
        descriptor = next(
            item for item in grounding.renderings if item.kind is RenderingKind.CONTEXT
        )
        assert descriptor.text == expected_descriptor
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", (descriptor.rendering_id,)),
                (grounding,),
            )
        )
        assert decision.released_text == expected_descriptor
        released_text = decision.released_text
        assert released_text is not None
        assert all(line not in released_text for line in response.narrative)

    def test_advertiser_breakdown_refusal_names_only_governed_alternatives(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        metric = catalog.metric("ad_spend")
        assert metric is not None
        refusal = catalog.refusal_for("ad_spend", grain="advertiser")
        assert refusal is not None
        assert refusal.reason is RefusalReason.GRAIN_NOT_SUPPORTED
        assert refusal.offer_instead == metric.valid_grains
        assert refusal.to_error().user_visible_description == (
            "The requested advertiser breakdown is unsupported for ad_spend. A figure at an "
            "unsupported breakdown is computable and wrong, which is worse than absent. Available "
            "instead: date, ecosystem, platform, channel_group, channel, campaign, ad_product, "
            "marketplace."
        )

    def test_bare_roas_is_not_an_alias(self) -> None:
        assert load_catalog(REPO_ROOT).metric_candidates("ROAS") == ()


class TestSourceProvenance:
    """Where a figure came from, and why the measurement basis is not enough.

    `retail_revenue_shipped` is Vendor Central and `retail_revenue_purchased` is AMC. Both are
    `retail_actual`, both answer "what did I sell", and they will disagree — so `mixes_basis`
    stays false for a combination that is nonetheless a double count.
    """

    def test_a_simple_metric_without_a_source_is_refused(self) -> None:
        with pytest.raises(CatalogError, match="source_system, source_datasets"):
            Metric(
                name="orphan",
                label="Orphan",
                kind=MetricKind.SIMPLE,
                unit_kind=UnitKind.COUNT,
                measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
                valid_grains=("date",),
                definition="sum(fct_ad_exposure.impressions)",
                aggregation_policy=_test_policy(),
            )

    def test_a_ratio_declaring_a_source_is_refused(self) -> None:
        """One home. A ratio's sources are its operands', so a copy here would go stale."""
        with pytest.raises(CatalogError, match="must not declare a source"):
            Metric(
                name="ctr",
                label="CTR",
                kind=MetricKind.RATIO,
                unit_kind=UnitKind.RATIO,
                measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
                valid_grains=("date",),
                numerator="clicks",
                denominator="impressions",
                source_system="amc",
            )

    def test_a_ratio_resolves_its_sources_through_its_operands(self) -> None:
        catalog = _catalog(
            _simple("clicks", source_datasets=("dsp_clicks",)),
            _simple("impressions", source_datasets=("dsp_impressions",)),
            Metric(
                name="ctr",
                label="CTR",
                kind=MetricKind.RATIO,
                unit_kind=UnitKind.RATIO,
                measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
                valid_grains=("date",),
                numerator="clicks",
                denominator="impressions",
            ),
        )
        ctr = catalog.metric("ctr")
        assert ctr is not None
        assert catalog.sources_for(ctr) == (("amc", "dsp_clicks"), ("amc", "dsp_impressions"))

    def test_two_systems_under_one_basis_are_visible_where_mixes_basis_is_not(self) -> None:
        """The hazard this field exists for, asserted as the pair of facts that make it one."""
        vendor = _simple(
            "retail_revenue_shipped",
            measurement_basis=(MeasurementBasis.RETAIL_ACTUAL,),
            definition="sum(fct_retail_sales.revenue_shipped)",
            source_system="vendor_central",
            source_datasets=("GET_VENDOR_SALES_REPORT",),
        )
        amc = _simple(
            "retail_revenue_purchased",
            measurement_basis=(MeasurementBasis.RETAIL_ACTUAL,),
            definition="sum(fct_retail_purchase_amc.revenue_purchased)",
            source_system="amc",
            source_datasets=("amazon_retail_purchases",),
        )
        catalog = _catalog(vendor, amc)
        both = (vendor, amc)
        assert len(catalog.bases_across(both)) == 1, "one basis, so mixes_basis cannot warn"
        assert len(catalog.sources_across(both)) == 2, "two sources, which is the warning"

    def test_the_committed_dictionary_declares_a_source_for_every_simple_metric(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        for metric in catalog.metrics:
            assert catalog.sources_for(metric), f"{metric.name} resolves to no source"

    @pytest.mark.covers("G-23")
    def test_every_committed_ratio_operand_is_a_declared_metric(self) -> None:
        """The condition that makes a ratio's grains checkable and its sources derivable.

        `check-lineage` resolves an operand naming a declared metric and skips one naming a
        fact column, so a column operand is unverified. It is also unattributable: a column
        names a fact, and `fct_ad_exposure` is written by four datasets.
        """
        catalog = load_catalog(REPO_ROOT)
        for metric in catalog.metrics:
            if metric.kind is not MetricKind.RATIO:
                continue
            for operand in (metric.numerator, metric.denominator):
                assert catalog.metric(operand) is not None, (
                    f"{metric.name} names {operand}, which is not a declared metric"
                )


class TestNonAdditivity:
    def test_a_distinct_count_is_declared_non_additive(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        reach = catalog.metric("reach_users")
        assert reach is not None
        assert catalog.is_non_additive(reach)

    def test_a_ratio_inherits_non_additivity_from_an_operand(self) -> None:
        """Declared once on the count, not again on every ratio built over it."""
        catalog = load_catalog(REPO_ROOT)
        frequency = catalog.metric("avg_frequency")
        assert frequency is not None
        assert catalog.is_non_additive(frequency)

    def test_an_ordinary_count_is_additive(self) -> None:
        catalog = load_catalog(REPO_ROOT)
        impressions = catalog.metric("impressions")
        assert impressions is not None
        assert not catalog.is_non_additive(impressions)


class TestTheMultiplier:
    def test_only_a_ratio_may_carry_one(self) -> None:
        """A simple metric sums a column whose contract already applied a scale factor."""
        with pytest.raises(CatalogError, match="only a ratio may carry a multiplier"):
            _simple("impressions", multiplier=1000)

    def test_a_multiplier_below_one_is_refused(self) -> None:
        with pytest.raises(CatalogError, match="toward zero or invert"):
            Metric(
                name="cpm",
                label="CPM",
                kind=MetricKind.RATIO,
                unit_kind=UnitKind.CURRENCY,
                measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
                valid_grains=("date",),
                numerator="ad_spend",
                denominator="impressions",
                multiplier=0,
            )

    def test_cpm_carries_the_thousand_it_is_named_for(self) -> None:
        """Otherwise a metric called cpm returns cost per single impression."""
        catalog = load_catalog(REPO_ROOT)
        cpm = catalog.metric("cpm")
        assert cpm is not None
        assert cpm.multiplier == 1000


@pytest.mark.covers("ADR-027")
class TestASourceOptionIsCheckedAgainstTheQuestion:
    """A `requires` option is a property of the load, so it is read from the filters.

    It used to refuse whenever a metric declared one, whatever the question pinned, which made
    every such metric permanently unanswerable — indistinguishable from not declaring it. The
    reporting-lens default is what satisfies the condition on a real question.
    """

    def _catalog(self) -> Catalog:
        return _catalog(
            _simple(
                "units_ordered",
                measurement_basis=(MeasurementBasis.RETAIL_ACTUAL,),
                definition="sum(fct_retail_sales.units_ordered)",
                source_system="vendor_central",
                source_datasets=("GET_VENDOR_SALES_REPORT",),
                requires=(("distributor_view", "MANUFACTURING"),),
            )
        )

    def test_the_pinned_option_makes_it_answerable(self) -> None:
        refusal = self._catalog().refusal_for(
            "units_ordered", grain="date", filters={"distributor_view": "MANUFACTURING"}
        )
        assert refusal is None

    def test_a_contradicting_option_is_refused(self) -> None:
        """Under SOURCING the column is null by construction, so there is nothing to sum."""
        refusal = self._catalog().refusal_for(
            "units_ordered", grain="date", filters={"distributor_view": "SOURCING"}
        )
        assert refusal is not None
        assert refusal.reason is RefusalReason.REQUIRES_SOURCE_OPTION

    def test_unstated_options_are_refused_rather_than_assumed(self) -> None:
        refusal = self._catalog().refusal_for("units_ordered", grain="date")
        assert refusal is not None
        assert refusal.reason is RefusalReason.REQUIRES_SOURCE_OPTION

    def test_the_refusal_names_the_option_the_question_is_missing(self) -> None:
        refusal = self._catalog().refusal_for("units_ordered", grain="date")
        assert refusal is not None
        assert "distributor_view=MANUFACTURING" in refusal.explanation


class TestAClosedSetSelectorMustBeExplicit:
    def test_missing_and_unknown_attribution_models_are_refused(self) -> None:
        catalog = load_catalog(REPO_ROOT)

        missing = catalog.refusal_for("roas_deduplicated_incl_halo_14d", grain="date")
        unknown = catalog.refusal_for(
            "roas_deduplicated_incl_halo_14d",
            grain="date",
            filters={"attribution_model_key": "invented_touch"},
        )

        assert missing is not None
        assert missing.reason is RefusalReason.REQUIRES_SOURCE_OPTION
        assert unknown is not None
        assert unknown.reason is RefusalReason.REQUIRES_SOURCE_OPTION

    @pytest.mark.parametrize("model_key", ("first_touch_v1", "last_touch_v1", "linear_v1"))
    def test_each_governed_attribution_model_is_answerable(self, model_key: str) -> None:
        refusal = load_catalog(REPO_ROOT).refusal_for(
            "roas_deduplicated_incl_halo_14d",
            grain="date",
            filters={"attribution_model_key": model_key},
        )

        assert refusal is None


_AXIS_DEFAULTS_YAML = """axis_defaults:
  time: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  ecosystem: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  platform: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  channel_group: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  channel: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  campaign: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  ad_product: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  product: {aggregation: additive, groupable: true, filterable: true, alignable: true}
  marketplace: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  source_option: {aggregation: exact_only, groupable: false, filterable: true, alignable: false}
  attribution_model: {aggregation: exact_only, groupable: false, filterable: true, alignable: false}
  exposure_group:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  frequency_bucket:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  identity_space:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: false}
  audience_segment: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  acquisition_channel_group:
    {aggregation: disjoint_within_extraction, groupable: true, filterable: true,
     alignable: true}
  cohort: {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
  months_since_acquisition:
    {aggregation: exact_only, groupable: true, filterable: true, alignable: true}
compatibility_defaults:
  population: amazon_retail_us
  business_calendar: iso_gregorian
  week_starts_on: 0
  profile_version: 1
"""


class TestLoadingIsStrict:
    def _write(self, tmp_path: pathlib.Path, body: str) -> pathlib.Path:
        target = tmp_path / "contracts" / "shared"
        target.mkdir(parents=True)
        (target / "metric-dictionary.yaml").write_text(
            "version: 1\n"
            + _AXIS_DEFAULTS_YAML
            + body
            + "inventory_categories:\n  - id: test_metrics\n    label: Test metrics\n"
            "    display_order: 1\n    metric_ids: [thing]\n"
            "deferred: []\n"
        )
        return tmp_path

    def test_an_unknown_measurement_basis_fails_rather_than_being_dropped(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A dropped basis turns a response needing a caveat into one that appears not to."""
        root = self._write(
            tmp_path,
            """
units: {count: {kind: count}}
metrics:
  thing:
    type: simple
    aggregation: sum
    ecosystem_aggregation: exact_only
    time_class: daily_additive
    time_grain: day
    compatibility: strict
    definition: sum(fct_x.y)
    units: count
    measurement_basis: [guesswork]
    valid_grains: [date]
""",
        )
        with pytest.raises(CatalogError, match="guesswork"):
            load_catalog(root)

    def test_a_units_name_the_dictionary_does_not_declare_fails(
        self, tmp_path: pathlib.Path
    ) -> None:
        root = self._write(
            tmp_path,
            """
units: {count: {kind: count}}
metrics:
  thing:
    type: simple
    aggregation: sum
    time_class: daily_additive
    time_grain: day
    compatibility: strict
    definition: sum(fct_x.y)
    units: furlongs
    measurement_basis: [ad_attributed]
    valid_grains: [date]
""",
        )
        with pytest.raises(CatalogError, match="furlongs"):
            load_catalog(root)

    def test_an_unrecognised_key_is_refused(self, tmp_path: pathlib.Path) -> None:
        """Repository-authored executable semantics fail closed on unknown behavior."""
        root = self._write(
            tmp_path,
            """
units: {count: {kind: count}}
metrics:
  thing:
    type: simple
    aggregation: sum
    time_class: daily_additive
    time_grain: day
    compatibility: strict
    definition: sum(fct_x.y)
    units: count
    measurement_basis: [ad_attributed]
    valid_grains: [date]
    source_system: amc
    source_datasets: [dsp_impressions]
    some_future_field: whatever
""",
        )
        with pytest.raises(CatalogError, match="unknown fields"):
            load_catalog(root)

    def test_a_dictionary_with_no_metrics_fails(self, tmp_path: pathlib.Path) -> None:
        root = self._write(tmp_path, "units: {count: {kind: count}}\nmetrics: {}\n")
        with pytest.raises(CatalogError, match="declares no metrics"):
            load_catalog(root)

    def test_a_missing_dictionary_is_a_broken_deployment(self, tmp_path: pathlib.Path) -> None:
        """It ships with the code, so its absence is not a caller asking for the unavailable."""
        with pytest.raises(CatalogError, match="missing"):
            load_catalog(tmp_path)
