# SPDX-License-Identifier: MIT-0
"""The ranked restriction: one rank rule rendered as SQL and as a Python sort (ADR-143)."""

from __future__ import annotations

import pathlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

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
from domain.measurement.ranking import (
    RANK_ORDER,
    RANKING_INELIGIBLE,
    RankDirection,
    RankedProjection,
    RankedRestriction,
    RankedValue,
    RankField,
)
from domain.presentation.retail_health import RETAIL_HEALTH_MAX_ROWS, RETAIL_HEALTH_TREND_METRICS
from domain.shared_kernel.errors import InvariantError, ValidationError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import QueryRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-143")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 8, 13), date(2026, 8, 19))
PRIOR = DateRange(date(2026, 8, 6), date(2026, 8, 12))


def _ranked_requests() -> tuple[QueryRequest, ...]:
    """Prepare and bind the real Retail batch; return its ranked requests, keys first."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW)
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
    return tuple(item for item in requests if item is not None and item.ranked is not None)


def test_the_retail_restriction_is_eligible_and_binds_its_batchs_own_reads() -> None:
    """The positive pair of every refusal below: the batch's compares are its population."""
    ranked = _ranked_requests()
    assert len(ranked) == 5
    keys, *series = ranked
    assert keys.ranked is not None and keys.ranked.restriction.projection is (
        RankedProjection.RANKED_KEYS
    )
    assert {item.ranked.restriction.projection for item in series if item.ranked} == {
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
    assert 'FROM "ranked" ORDER BY "rank_value" DESC, "key" ASC LIMIT 51' in composed.sql
    # The CTE keeps the top n by the same rendering, and a key without a rank row ranks at 0.
    assert 'ORDER BY "rank_value" DESC, "key" ASC LIMIT 50)' in composed.sql
    assert 'COALESCE("r"."rank_value", 0) AS "rank_value"' in composed.sql
    assert '"rank_value" AS value_0' in composed.sql
    assert composed.sql.count("?") == len(composed.parameters)


def test_a_semi_join_statement_restricts_its_own_read_to_the_ranked_keys() -> None:
    series = _ranked_requests()[1]
    assert series.ranked is not None
    composed = compose(series.plan, series.max_rows, series.ranked)
    fact = series.plan.logical.fact
    semi_join = f'EXISTS (SELECT 1 FROM "ranked" WHERE "ranked"."key" = "{fact}"."amazon_asin"'  # noqa: S608 -- an expected text, not a statement
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
        bound = (*request.ranked.population, request.ranked.rank, request.plan)
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
        ({"population_metrics": ("retail_revenue_shipped", "conversion_rate_retail")}, "simple"),
    ],
)
def test_an_ineligible_restriction_is_refused_when_the_batch_is_prepared(
    change: dict[str, object], problem: str
) -> None:
    """Each ineligible input is an `InvariantError` at prepare time, never a runtime surprise."""
    batch = retail_health_measurement_batch(
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW)
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
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW)
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
        RetailHealthMeasurementBatchRequest(CATALOG, CONTRACTS, PERIOD, PRIOR, NOW)
    )
    keys = next(
        operation.request
        for operation in batch.prepared.operations
        if operation.request.ranked is not None
    )
    from ports.outbound.query import RowCeiling  # noqa: PLC0415

    with pytest.raises(ValidationError, match="publish-time"):
        replace(keys, row_ceiling=RowCeiling.NARRATION)
