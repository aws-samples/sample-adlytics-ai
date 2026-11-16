# SPDX-License-Identifier: MIT-0
"""The weekly-aggregate statements and the Parquet row count.

An unfenced read would forecast from publications nobody pinned; a statement that grew past the
envelope would fail at Athena instead of refusing first; a wrong footer read would put false row
counts in every manifest.
"""

from __future__ import annotations

import pytest
from forecast_parquet_support import parquet_bytes

from adapters.outbound.athena.weekly_series import compose_unload, parquet_row_count
from ports.outbound.weekly_series_source import WeeklySeriesPlan

pytestmark = pytest.mark.unit


def _plan(sales: int = 3, traffic: int = 2) -> WeeklySeriesPlan:
    return WeeklySeriesPlan(
        sales_publication_ids=tuple(f"pub-sales-{n}" for n in range(sales)),
        traffic_publication_ids=tuple(f"pub-traffic-{n}" for n in range(traffic)),
        distributor_view="MANUFACTURING",
        selling_program="RETAIL",
        shard_max_asins=2000,
        target_destination="s3://adlytics-dev-demand-forecast/runs/run-1/input/r0a0/target/",
        traffic_destination="s3://adlytics-dev-demand-forecast/runs/run-1/input/r0a0/traffic/",
    )


def test_every_read_is_fenced_to_the_bound_pins() -> None:
    statements = compose_unload(_plan())
    assert statements.roster.sql.count("?") == 3
    assert statements.roster.parameters == ("pub-sales-0", "pub-sales-1", "pub-sales-2")
    assert statements.target.parameters == (
        *(f"pub-sales-{n}" for n in range(3)),
        *(f"pub-sales-{n}" for n in range(3)),
        "MANUFACTURING",
        "RETAIL",
    )
    assert statements.target.sql.count("?") == len(statements.target.parameters)
    assert statements.traffic.parameters[-2:] == ("pub-traffic-0", "pub-traffic-1")
    assert statements.traffic.sql.count("?") == len(statements.traffic.parameters)


def test_the_unloads_are_partitioned_by_shard_into_their_attempt_prefix() -> None:
    statements = compose_unload(_plan())
    assert statements.target.sql.startswith("UNLOAD (WITH roster AS")
    assert "TO 's3://adlytics-dev-demand-forecast/runs/run-1/input/r0a0/target/'" in (
        statements.target.sql
    )
    assert "partitioned_by = ARRAY['shard']" in statements.target.sql
    assert statements.target.sql.index(", r.shard FROM days") > 0
    assert "/ 2000 AS shard" in statements.roster.sql


def test_a_destination_with_a_quote_is_refused() -> None:
    plan = WeeklySeriesPlan(
        sales_publication_ids=("p",),
        traffic_publication_ids=(),
        distributor_view="MANUFACTURING",
        selling_program="RETAIL",
        shard_max_asins=2000,
        target_destination="s3://bucket/runs/x'y/",
        traffic_destination="s3://bucket/runs/x/",
    )
    with pytest.raises(ValueError, match="plain s3:// prefix"):
        compose_unload(plan)


def test_a_publication_id_with_a_quote_is_refused() -> None:
    plan = WeeklySeriesPlan(
        sales_publication_ids=("p'1",),
        traffic_publication_ids=(),
        distributor_view="MANUFACTURING",
        selling_program="RETAIL",
        shard_max_asins=2000,
        target_destination="s3://bucket-a/runs/x/target/",
        traffic_destination="s3://bucket-a/runs/x/traffic/",
    )
    with pytest.raises(ValueError, match="unexpected shape"):
        compose_unload(plan)


@pytest.mark.parametrize("rows", [0, 1, 1234])
def test_the_footer_row_count_matches_a_written_file(rows: int) -> None:
    data = parquet_bytes(["canonical_asin", "days_with_row"], rows)
    assert parquet_row_count(data[-4096:] if len(data) > 4096 else data) == rows


def test_a_tail_without_the_magic_is_refused() -> None:
    with pytest.raises(ValueError, match="not a Parquet file tail"):
        parquet_row_count(b"x" * 64)
