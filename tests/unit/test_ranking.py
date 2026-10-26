# SPDX-License-Identifier: MIT-0
"""The ranked restriction: one rank rule rendered as SQL and as a Python sort (ADR-143).

A divergence would show one product order, total or mover on the page and compute another;
the population figures (ADR-152) are what Retail and New customers state about every key.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from publication_support import StubPublications

from adapters.inbound.tool_handlers.measurement import (
    _presentation_requests,  # pyright: ignore[reportPrivateUsage]
    prepare_batch,
    query_request_digest,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailHealthMeasurementBatchRequest,
    retail_health_measurement_batch,
    retail_ranked_restriction,
)
from adapters.outbound.athena.composition import compose
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import MetricKind
from domain.measurement.ranking import (
    RANK_ORDER,
    RANKED_INVARIANT,
    RANKING_INELIGIBLE,
    RankablePlans,
    RankBy,
    RankDirection,
    RankedMembership,
    RankedPopulation,
    RankedProjection,
    RankedRead,
    RankedRestriction,
    RankedValue,
    RankField,
    require_rankable,
)
from domain.presentation.retail_health import RETAIL_HEALTH_MAX_ROWS, RETAIL_HEALTH_TREND_METRICS
from domain.shared_kernel.errors import InvariantError, ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import QueryRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143", "ADR-152")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))


def _ranked_requests() -> tuple[QueryRequest, ...]:
    """Prepare and bind the real Retail batch; return its ranked requests, keys first."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    )
    prepared = batch.prepared
    inspection = StubPublications().inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    requests = _presentation_requests(prepared, inspection)
    ranked = tuple(item for item in requests if item is not None and item.ranked is not None)
    # The ranked keys first, then every read restricted to them, in batch order.
    return tuple(
        sorted(
            ranked,
            key=lambda item: (
                item.ranked is None
                or item.ranked.restriction.projection is not RankedProjection.RANKED_KEYS
            ),
        )
    )


def _series(requests: tuple[QueryRequest, ...]) -> QueryRequest:
    return next(item for item in requests if "date_key" in item.plan.logical.group_by)


def _movers(requests: tuple[QueryRequest, ...]) -> QueryRequest:
    return next(
        item
        for item in requests
        if item.ranked is not None and item.ranked.restriction.rank_by is RankBy.ABS_CHANGE
    )


def test_the_retail_restriction_is_eligible_and_binds_its_batchs_own_reads() -> None:
    """The positive pair of every refusal below: the batch's compares are its population."""
    ranked = _ranked_requests()
    # The keys, 14 row compares, 4 series, 2 movers and 4 ads: every product-grain read.
    assert len(ranked) == 25
    keys, *restricted = ranked
    assert keys.ranked is not None and keys.ranked.restriction.projection is (
        RankedProjection.RANKED_KEYS
    )
    assert {item.ranked.restriction.projection for item in restricted if item.ranked} == {
        RankedProjection.SEMI_JOIN
    }
    read = keys.ranked
    assert tuple(
        (item.logical.metric.name, item.logical.window) for item in read.population
    ) == tuple(
        (metric, window) for metric in RETAIL_HEALTH_TREND_METRICS for window in (PERIOD, PRIOR)
    )
    assert read.rank.logical.metric.name == "retail_revenue_shipped"
    assert read.rank.logical.window == PERIOD


def test_the_ranked_keys_statement_selects_from_ranked_and_orders_by_the_one_rule() -> None:
    keys = _ranked_requests()[0]
    assert keys.ranked is not None
    composed = compose(keys.plan, keys.max_rows, keys.ranked)
    assert composed.sql.startswith('WITH "ranked" AS (SELECT ')
    assert composed.sql.endswith(
        '"population_count", "population_suppressed", "population_sum_0", "population_sum_1" '
        'FROM "ranked" ORDER BY "rank_value" DESC, "key" ASC LIMIT 51'
    )
    # The CTE keeps the top n by the same rendering, and a key without a rank row ranks at 0.
    assert (
        'ROW_NUMBER() OVER (ORDER BY "rank_value" DESC, "key" ASC) AS "rank_position"'
        in composed.sql
    )
    assert 'WHERE "rank_position" <= 50)' in composed.sql
    assert 'COALESCE("r"."rank_value", 0) AS "rank_value"' in composed.sql
    # The count and withholding are taken over the whole population, before the top n.
    assert 'COUNT(*) OVER () AS "population_count"' in composed.sql
    assert 'MAX("suppressed") OVER () AS "population_suppressed"' in composed.sql
    assert '"rank_value" AS value_0' in composed.sql
    assert composed.sql.count("?") == len(composed.parameters)


def test_a_ranked_read_is_never_composed_without_a_row_limit() -> None:
    keys = _ranked_requests()[0]
    assert keys.ranked is not None
    assert compose(keys.plan, keys.max_rows, keys.ranked).sql.endswith("LIMIT 51")
    with pytest.raises(ValueError, match="a ranked read is never folded without a row limit"):
        compose(keys.plan, None, keys.ranked)


def test_the_population_sums_are_scalar_sums_over_their_own_fenced_reads() -> None:
    """Each sum binds first, in text order: the sums sit in the CTE's outer select list."""
    keys = _ranked_requests()[0]
    assert keys.ranked is not None
    composed = compose(keys.plan, keys.max_rows, keys.ranked)
    revenue, units = keys.ranked.sums
    assert (revenue.logical.metric.name, units.logical.metric.name) == (
        "retail_revenue_shipped",
        "units_shipped",
    )
    assert '(SELECT SUM("revenue_shipped") FROM "fct_retail_sales" WHERE ' in composed.sql
    assert 'AS population_sum_0, (SELECT SUM("units_shipped")' in composed.sql
    first = (revenue.logical.window.start.isoformat(), revenue.logical.window.end.isoformat())
    assert composed.parameters[:2] == first
    assert composed.sql.count("?") == len(composed.parameters)


def test_the_movers_rank_by_the_absolute_change_nonzero_only() -> None:
    movers = _movers(_ranked_requests())
    assert movers.ranked is not None and movers.ranked.change is not None
    composed = compose(movers.plan, movers.max_rows, movers.ranked)
    assert (
        'ABS(COALESCE("r"."rank_value", 0) - COALESCE("c"."rank_value", 0)) AS "rank_value"'
        in composed.sql
    )
    assert 'WHERE ABS(COALESCE("r"."rank_value", 0) - COALESCE("c"."rank_value", 0)) > 0' in (
        composed.sql
    )
    assert 'WHERE "rank_position" <= 3)' in composed.sql
    assert movers.ranked.change.logical.window == PRIOR
    assert composed.sql.count("?") == len(composed.parameters)


def test_a_semi_join_statement_restricts_its_own_read_to_the_ranked_keys() -> None:
    series = _series(_ranked_requests())
    assert series.ranked is not None
    composed = compose(series.plan, series.max_rows, series.ranked)
    fact = series.plan.logical.fact
    semi_join = f'EXISTS (SELECT 1 FROM "ranked" WHERE "ranked"."key" = "{fact}"."amazon_asin"'  # noqa: S608  # nosec B608 # A-SQL-001: an expected text, not a statement
    assert f"WHERE {semi_join}" in composed.sql
    assert composed.sql.endswith(f"LIMIT {series.max_rows + 1}")
    assert composed.sql.count("?") == len(composed.parameters)
    # Paired: the same plan without the restriction is the ordinary read, with no CTE.
    plain = compose(series.plan, series.max_rows)
    assert "ranked" not in plain.sql and composed.sql.endswith(plain.sql.split(" WHERE ", 1)[1])


def test_the_statement_names_no_key_so_its_size_is_independent_of_the_catalog() -> None:
    """No key list is built: the text and parameters are fenced publications and windows only."""
    for request in _ranked_requests():
        assert request.ranked is not None
        composed = compose(request.plan, request.max_rows, request.ranked)
        assert not re.search(r"amazon_asin:|'[A-Z0-9]{10}'", composed.sql)
        change = () if request.ranked.change is None else (request.ranked.change,)
        bound = (*request.ranked.population, request.ranked.rank, *change, request.plan)
        allowed = {
            *(pid for item in bound for pid in item.primary_ids),
            *(
                str(day)
                for item in bound
                for day in (item.logical.window.start, item.logical.window.end)
            ),
            *(value for item in bound for _, value in item.primary_filters),
        }
        assert set(composed.parameters) <= allowed


def test_the_two_projections_have_different_request_digests() -> None:
    keys = _ranked_requests()[0]
    assert keys.ranked is not None
    semi = replace(
        keys,
        ranked=replace(
            keys.ranked,
            restriction=replace(keys.ranked.restriction, projection=RankedProjection.SEMI_JOIN),
        ),
    )
    assert query_request_digest(keys) != query_request_digest(semi)


def test_both_renderings_come_from_one_order_list() -> None:
    assert RANK_ORDER == (
        (RankField.RANK_VALUE, RankDirection.DESC),
        (RankField.KEY, RankDirection.ASC),
    )
    rows = (
        RankedValue("b", Decimal(5)),
        RankedValue("a", Decimal(5)),
        RankedValue("d", 0),
        RankedValue("c", Decimal(9)),
        RankedValue("e", 0),
    )
    expected = sorted(rows, key=lambda item: (-Decimal(item.rank_value), item.key))
    assert RankedRestriction.order(rows) == tuple(expected)
    assert [item.key for item in RankedRestriction.order(rows)] == ["c", "a", "b", "d", "e"]


def _restriction(**changes: object) -> RankedRestriction:
    base = retail_ranked_restriction(PERIOD, PRIOR, RankedProjection.RANKED_KEYS)
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"rank_metric": "asp_shipped"}, "not a simple one-measure metric"),
        (
            {
                "population_metrics": ("retail_revenue_shipped", "conversion_rate_retail"),
                "population_sums": (),
            },
            "simple",
        ),
    ],
)
def test_an_ineligible_restriction_is_refused_when_the_batch_is_prepared(
    change: dict[str, object], problem: str
) -> None:
    """Each ineligible input is an `InvariantError` at prepare time, never a runtime surprise."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    )
    (keys,) = (
        operation.request
        for operation in batch.prepared.operations
        if operation.request.ranked is not None
        and operation.request.ranked.projection is RankedProjection.RANKED_KEYS
    )
    with pytest.raises((InvariantError, ValidationError)) as raised:
        prepare_batch((replace(keys, ranked=_restriction(**change)),))
    error = raised.value
    if isinstance(error, InvariantError):
        assert error.code == RANKING_INELIGIBLE
        assert problem in str(error.context["problem"])


def test_a_restricted_read_without_the_ranked_key_is_refused() -> None:
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    )
    keys = next(
        operation.request
        for operation in batch.prepared.operations
        if operation.request.ranked is not None
    )
    with pytest.raises(InvariantError) as raised:
        prepare_batch((replace(keys, grains=("marketplace",)),))
    assert raised.value.code == RANKING_INELIGIBLE
    assert raised.value.context["problem"] == "a restricted read without the ranked key"


def test_a_restriction_without_its_rank_window_in_the_population_does_not_construct() -> None:
    with pytest.raises(ValueError, match="within its population"):
        _restriction(population_windows=(PRIOR,))
    with pytest.raises(ValueError, match="at least one key"):
        _restriction(n=0)
    assert _restriction().n == RETAIL_HEALTH_MAX_ROWS


def test_a_ranked_or_folded_request_is_refused_on_the_narration_ceiling() -> None:
    """Chat can't reach either: a narration request with a restriction doesn't validate."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    )
    keys = next(
        operation.request
        for operation in batch.prepared.operations
        if operation.request.ranked is not None
    )
    from ports.outbound.query import RowCeiling  # noqa: PLC0415

    with pytest.raises(ValidationError, match="publish-time"):
        replace(keys, row_ceiling=RowCeiling.NARRATION)


def test_a_held_or_ranked_request_is_refused_on_the_grouped_ceiling() -> None:
    """With no row limit, a held read would hold every row; only a plain fold may be grouped."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW, 2)
    )
    keys = next(
        operation.request
        for operation in batch.prepared.operations
        if operation.request.ranked is not None
    )
    from ports.outbound.query import RowCeiling  # noqa: PLC0415

    with pytest.raises(ValidationError, match="folded, never held"):
        replace(keys, row_ceiling=RowCeiling.BAKE_GROUPED)


# --- The restriction's own rules, on fictional stub plans (each refusal paired with a pass) ----

_KEY, _MARKET = "amazon_asin", "marketplace_key"


def _stub(  # noqa: PLR0913 -- one stub plan's closed set of eligibility fields
    metric: str,
    window: DateRange,
    *,
    group_by: tuple[str, ...] = (_KEY, _MARKET),
    kind: MetricKind = MetricKind.SIMPLE,
    measures: int = 1,
    join: object = None,
    roster: object = None,
) -> Any:
    return SimpleNamespace(
        metric=SimpleNamespace(name=metric, kind=kind),
        window=window,
        group_by=group_by,
        measures=tuple(f"m{index}" for index in range(measures)),
        join=join,
        roster=roster,
        suppression_columns=(),
        periodic=False,
    )


_RESTRICTION = RankedRestriction(
    key="product",
    rank_metric="metric_b",
    rank_window=PERIOD,
    population_metrics=("metric_b", "metric_c"),
    population_windows=(PERIOD, PRIOR),
    n=1,
    projection=RankedProjection.RANKED_KEYS,
)


def _population(**changes: Any) -> tuple[Any, ...]:
    return tuple(_stub(metric, window, **changes) for metric, window in _RESTRICTION.population)


def _rankable(
    *,
    rank: Any = None,
    population: tuple[Any, ...] | None = None,
    restricted: Any = None,
) -> None:
    require_rankable(
        _RESTRICTION,
        RankablePlans(
            restricted=restricted if restricted is not None else _stub("metric_b", PERIOD),
            rank=rank if rank is not None else _stub("metric_b", PERIOD),
            population=population if population is not None else _population(),
            columns=(_KEY, _MARKET),
        ),
    )


def test_the_ranking_error_codes_are_stable() -> None:
    assert (str(RANKING_INELIGIBLE), str(RANKED_INVARIANT)) == (
        "ADLY-RANKING-0001",
        "ADLY-RANKING-0002",
    )


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"rank": _stub("metric_a", PERIOD)}, "not the declared rank read"),
        ({"rank": _stub("metric_z", PERIOD)}, "not the declared rank read"),
        ({"rank": _stub("metric_b", PRIOR)}, "not the declared rank read"),
        ({"population": _population()[:-1]}, "not the declared population"),
        ({"population": (*_population(), _stub("metric_z", PRIOR))}, "not the declared population"),
        ({"population": tuple(reversed(_population()))}, "not the declared population"),
        ({"rank": _stub("metric_b", PERIOD, measures=2)}, "not a simple one-measure metric"),
        ({"rank": _stub("metric_b", PERIOD, kind=MetricKind.RATIO)}, "simple"),
        ({"population": _population(group_by=(_KEY,))}, "not at the ranked grain"),
        ({"population": _population(group_by=(_KEY, _MARKET, "x"))}, "not at the ranked grain"),
        ({"population": _population(group_by=("a", "b"))}, "not at the ranked grain"),
        ({"restricted": _stub("metric_b", PERIOD, join=object())}, "joined or rostered"),
        ({"restricted": _stub("metric_b", PERIOD, roster=object())}, "joined or rostered"),
        ({"restricted": _stub("metric_b", PERIOD, group_by=(_MARKET,))}, "without the ranked key"),
        (
            {"rank": SimpleNamespace(**{**vars(_stub("metric_b", PERIOD)), "periodic": True})},
            "a periodic read",
        ),
    ],
)
def test_each_ineligible_ranked_read_is_refused(change: dict[str, Any], problem: str) -> None:
    """Paired with the eligible stubs: each one change is refused with its own reason."""
    _rankable()
    with pytest.raises(InvariantError) as raised:
        _rankable(**change)
    assert raised.value.code == RANKING_INELIGIBLE
    assert problem in str(raised.value.context["problem"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"n": True}, "at least one key"),
        ({"key": " "}, "names its key"),
        ({"rank_metric": " "}, "names its key"),
        ({"population_metrics": ()}, "declares its population"),
        ({"population_windows": ()}, "declares its population"),
        ({"population_metrics": ("metric_b", "metric_b")}, "repeats a metric"),
        ({"population_windows": (PERIOD, PERIOD)}, "repeats a window"),
        ({"rank_by": RankBy.ABS_CHANGE}, "change window exactly when by change"),
        ({"change_window": PRIOR}, "change window exactly when by change"),
        ({"rank_by": RankBy.ABS_CHANGE, "change_window": PERIOD}, "another of the population"),
        (
            {"rank_by": RankBy.ABS_CHANGE, "change_window": DateRange(PRIOR.start, PRIOR.start)},
            "another of the population",
        ),
        ({"partition_by": ("channel", "channel")}, "distinct grains"),
        ({"partition_by": ("product",)}, "distinct grains"),
        ({"population_sums": ("metric_b", "metric_b")}, "sums repeat a metric"),
        ({"population_sums": ("metric_z",)}, "sums only its population metrics"),
        ({"population_sums": ("metric_b",), "partition_by": ("channel",)}, "no population sums"),
    ],
)
def test_a_malformed_restriction_does_not_construct(change: dict[str, Any], message: str) -> None:
    """Paired with `_RESTRICTION`, which constructs at n = 1."""
    assert _RESTRICTION.n == 1
    with pytest.raises(ValueError, match=message):
        replace(_RESTRICTION, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"rank_by": RankBy.ABS_CHANGE, "change_window": PRIOR},
        {"partition_by": ("channel", "ad_product"), "membership": RankedMembership.POSITIVE},
        {"population_sums": ("metric_b", "metric_c")},
    ],
)
def test_each_new_restriction_shape_constructs(change: dict[str, Any]) -> None:
    """The valid side of each malformed pair above: movers, a partitioned gateway, sums."""
    restriction = replace(_RESTRICTION, **change)
    assert {key: getattr(restriction, key) for key in change} == change


def test_a_change_ranking_binds_its_change_read_and_a_partition_its_columns() -> None:
    bound = cast("Any", object())
    by_change = replace(_RESTRICTION, rank_by=RankBy.ABS_CHANGE, change_window=PRIOR)
    RankedRead(by_change, _KEY, _MARKET, (bound,) * 4, bound, change=bound)
    with pytest.raises(ValueError, match="change read exactly"):
        RankedRead(by_change, _KEY, _MARKET, (bound,) * 4, bound)
    with pytest.raises(ValueError, match="change read exactly"):
        RankedRead(_RESTRICTION, _KEY, _MARKET, (bound,) * 4, bound, change=bound)
    partitioned = replace(_RESTRICTION, partition_by=("channel",))
    RankedRead(partitioned, _KEY, _MARKET, (bound,) * 4, bound, partition_columns=("channel_key",))
    with pytest.raises(ValueError, match="one column per partition"):
        RankedRead(partitioned, _KEY, _MARKET, (bound,) * 4, bound)


def test_a_ranked_population_counts_whole_keys_and_names_its_sums() -> None:
    population = RankedPopulation((), 3, (("metric_b", Decimal("12.5")),), suppressed=False)
    assert population.sum_of("metric_b") == Decimal("12.5")
    with pytest.raises(ValueError, match="zero or more keys"):
        RankedPopulation((), -1, (), suppressed=False)


def test_a_fact_that_can_withhold_is_rankable_its_withholding_reported_not_refused() -> None:
    """ADR-152: the gateway's AMC fact withholds; the statement reports it to the caller."""
    withholding = _stub("metric_b", PERIOD)
    withholding.suppression_columns = ("rows_suppressed",)
    _rankable(rank=withholding)


def test_a_ranked_read_binds_exactly_its_declared_population() -> None:
    bound = cast("Any", object())
    read = RankedRead(_RESTRICTION, _KEY, _MARKET, (bound,) * 4, bound)
    with pytest.raises(ValueError, match="exactly its declared population"):
        RankedRead(_RESTRICTION, _KEY, _MARKET, (bound,) * 3, bound)
    with pytest.raises(ValueError, match="exactly its declared population"):
        RankedRead(_RESTRICTION, _KEY, _MARKET, (bound,) * 5, bound)
    for value in (read, _RESTRICTION, RankedValue("a", 1)):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            value.n = 2  # type: ignore[attr-defined, misc]


def test_the_order_rule_is_callable_from_a_restriction_value() -> None:
    rows = (RankedValue("b", 1), RankedValue("a", 1), RankedValue("c", 2))
    assert [item.key for item in _RESTRICTION.order(rows)] == ["c", "a", "b"]


def test_the_rank_window_is_compared_by_value_not_identity() -> None:
    """A planned rank read carries an equal window, not the declaration's own object."""
    equal = DateRange(date(PERIOD.start.year, PERIOD.start.month, PERIOD.start.day), PERIOD.end)
    assert equal == PERIOD and equal is not PERIOD
    _rankable(rank=_stub("metric_b", equal))
