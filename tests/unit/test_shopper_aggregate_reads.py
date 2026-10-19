# SPDX-License-Identifier: MIT-0
"""D2 shopper-aggregate reads run in the engine and disclose a withheld cell (ADR-099 pt 7-8).

The D2 facts load under `discard_suppressed_rows`: no suppressed row reaches them, so they store
no `rows_suppressed`. A withheld cell is disclosed the way the D2 contracts and page bodies
already do it: an expected cell that didn't come back is stored `not_returned` with NULL counts
("not available: too few shoppers, or none"), and a rostered key with no row reads "not
available". Every fake query port here first holds the composed statement to the generated
schema (`athena_engine_support`), so a fold over a column the fact doesn't store, or a DATE
column compared with a varchar, fails here as it failed live (2026-10-17, dev: COLUMN_NOT_FOUND
`rows_suppressed`, TYPE_MISMATCH `as_of_month`). Chat and the Reach, Retention and Audiences
bakes share `prepare_batch`, so each is driven through its own real path.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
import test_reach_source as reach
import test_retention_source as retention
import test_segment_profile as audiences
from athena_engine_support import engine_refusal, require_engine_accepts, stored_columns
from publication_support import StubPublications, bound_plan

from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena import query as athena_query
from adapters.outbound.athena.composition import compose
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.shared_kernel.values import DateRange
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-035")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
NOT_AVAILABLE = "not available: too few shoppers, or none"


# --- the shape the composer folds on ------------------------------------------------------------


def test_every_column_a_fact_shape_folds_on_is_stored_by_its_fact() -> None:
    facts = sorted({contract.target_table for contract in CONTRACTS if contract.targets_a_fact})
    folded = {fact: FactShape.for_fact(fact, CONTRACTS).suppression_columns[:1] for fact in facts}
    stored = {
        fact: [column for column in columns if column in stored_columns(fact)]
        for fact, columns in folded.items()
    }
    assert stored == {fact: list(columns) for fact, columns in folded.items()}
    # The discard facts fold on nothing; a fact that stores the flag still folds on it.
    assert folded["fct_segment_delivery"] == ()
    assert folded["fct_shopper_acquisition"] == ()
    assert folded["fct_ad_exposure"] == ("rows_suppressed",)


def test_the_v1_rows_suppressed_fold_is_unchanged_and_still_reports_a_withholding() -> None:
    week = DateRange(date(2026, 8, 10), date(2026, 8, 16))
    logical = plan(
        load_catalog(ROOT),
        PlanRequest(
            metric_name="impressions",
            grains=("campaign",),
            window=week,
            filters={"marketplace_key": "ATVPDKIKX0DER"},
        ),
        FactShape.for_fact("fct_ad_exposure", CONTRACTS),
    )
    composed = compose(bound_plan(logical, published_at=datetime(2026, 8, 19, tzinfo=UTC)), 100)
    assert 'SUM(CASE WHEN NOT "rows_suppressed" THEN "impressions" END) AS value_0' in composed.sql
    assert engine_refusal(composed.sql) is None
    withheld = athena_query._suppression_from(  # pyright: ignore[reportPrivateUsage]
        logical, ({"campaign_key": "c", "value_0": 10, "withheld_0": 4, "withheld_rows_0": 1},)
    )
    assert withheld.rows_suppressed is True
    whole = athena_query._suppression_from(  # pyright: ignore[reportPrivateUsage]
        logical, ({"campaign_key": "c", "value_0": 10, "withheld_0": 0, "withheld_rows_0": 0},)
    )
    assert whole.rows_suppressed is False


# --- chat --------------------------------------------------------------------------------------


@dataclass
class _CheckedReach(reach.ReachQueries):
    def run(self, request: QueryRequest) -> QueryResult:
        require_engine_accepts(request)
        return super().run(request)


def _chat_reach(queries: reach.ReachQueries) -> Any:
    return handle(
        MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=reach.CATALOG,
            contracts=reach.CONTRACTS,
            metric_name="shoppers_reached",
            grains=("marketplace", "exposure_group", "identity_space"),
            window=reach.WEEK.window,
            computed_at=reach.NOW,
            correlation_id="corr-d2-withheld",
            filters={"marketplace_key": reach.US},
        ),
        MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            periods=reach._Served(),  # pyright: ignore[reportPrivateUsage]
        ),
    )


def test_chat_answers_a_d2_slice_with_a_withheld_cell_and_discloses_it() -> None:
    response = _chat_reach(_CheckedReach(not_returned=(("dsp_only", "device_or_browser"),)))
    narrative = cast("tuple[str, ...]", response.narrative)
    assert any(
        f"1 row(s) are not shown because they're {NOT_AVAILABLE}" in line for line in narrative
    )
    values = {
        (row["exposure_group"], row["identity_space"]): row["value"] for row in response.data.values
    }
    # The other cells' figures are answered; the withheld one is not shown as a number.
    assert values[("dsp_only", "account")] == 2000
    assert ("dsp_only", "device_or_browser") not in values


def test_chat_answers_a_d2_slice_with_nothing_withheld_with_no_withheld_qualifier() -> None:
    response = _chat_reach(_CheckedReach())
    narrative = cast("tuple[str, ...]", response.narrative)
    assert not any(NOT_AVAILABLE in line for line in narrative)
    assert not any("not shown" in line for line in narrative)
    assert len(response.data.values) == 6


# --- the shared bake path ----------------------------------------------------------------------


def test_the_reach_bake_reads_in_the_engine_and_bakes_a_withheld_cell_not_available() -> None:
    queries = _CheckedReach(not_returned=(("dsp_only", "device_or_browser"),))
    body = reach._baked(queries)  # pyright: ignore[reportPrivateUsage]
    (market,) = cast("list[dict[str, Any]]", body["markets"])
    tiles = {tile["metric"]: tile["current"] for tile in market["overlap"]["tiles"]}
    assert tiles["overlap_share_all_ids"] == {
        "absence": {"reason": "not_returned", "text": "too few shoppers, or none"}
    }
    assert tiles["overlap_share"]["value"] == "0.3000"


@dataclass
class _CheckedCohorts(retention.CohortQueries):
    def run(self, request: QueryRequest) -> QueryResult:
        require_engine_accepts(request)
        return super().run(request)


def test_the_retention_bake_reads_in_the_engine_and_bakes_a_withheld_cell_not_available() -> None:
    gap = ("standard", date(2025, 12, 1), 3, "dsp_only")
    queries = _CheckedCohorts(not_returned=(gap,))
    body = retention._baked(queries)  # pyright: ignore[reportPrivateUsage]
    cell = retention._cell(body, "standard", "dsp_only", "2025-12-01", 3)  # pyright: ignore[reportPrivateUsage]
    assert (cell["state"], cell["text"], cell["rate"]) == ("not_returned", "Not available", None)
    assert retention._cell(body, "standard", "dsp_only", "2025-12-01", 4)["state"] == "value"  # pyright: ignore[reportPrivateUsage]
    # Every cohort read pins its vintage, and the engine compares it as a date.
    vintage = [
        require_engine_accepts(request).sql
        for request in queries.seen
        if "as_of_month" in request.plan.logical.filters
    ]
    assert vintage
    assert all('"as_of_month" = CAST(? AS DATE)' in sql for sql in vintage)


def test_the_audiences_bake_reads_in_the_engine_and_lists_a_withheld_segment_not_available() -> (
    None
):
    def checked(request: QueryRequest, result: QueryResult) -> QueryResult:
        require_engine_accepts(request)
        return result

    body = audiences._bake_with(checked)  # pyright: ignore[reportPrivateUsage]
    dsp = body["markets"][0]["views"][1]
    # The roster's 41003 didn't come back (withheld rows are discarded): listed not available.
    assert [(item["segment_id"], item["text"]) for item in dsp["not_available"]] == [
        ("41003", audiences.ROSTER_GAP_TEXT)
    ]
    assert [row["segment_id"] for row in dsp["rows"]] == ["41001", "41002"]


@dataclass
class _CheckedSegments(audiences._ByMetric):  # pyright: ignore[reportPrivateUsage]
    def run(self, request: QueryRequest) -> QueryResult:
        require_engine_accepts(request)
        return super().run(request)


def test_chat_lists_a_rostered_segment_with_no_row_not_available_beside_the_figures() -> None:
    """A withheld open-slice row is discarded, so no cell exists; the roster names the gap."""
    queries = _CheckedSegments({"segment_buyers": audiences._segment_rows({"41001": 48})})  # pyright: ignore[reportPrivateUsage]
    rosters = audiences._Rosters(  # pyright: ignore[reportPrivateUsage]
        {"dsp_exposed_only": audiences._roster("dsp_exposed_only", "41001", "41002")}  # pyright: ignore[reportPrivateUsage]
    )
    response = handle(
        audiences._ask("segment_buyers", profile_path="dsp_exposed_only"),  # pyright: ignore[reportPrivateUsage]
        MeasurementTooling(
            queries=queries,  # type: ignore[arg-type]
            publications=StubPublications(),
            periods=audiences._Served(),  # pyright: ignore[reportPrivateUsage]
            rosters=rosters,
        ),
    )
    narrative = " ".join(response.narrative)
    assert "41002 is not available: too few shoppers this month" in narrative
    assert [row["segment_id"] for row in response.data.values] == ["41001"]
