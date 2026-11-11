# SPDX-License-Identifier: MIT-0
"""The input builder's select and build steps, with fakes at the ports.

A wrong attempt prefix would UNLOAD into a non-empty location after a redrive; a manifest that
skipped validation would hand the job an input it can't trust; titles from an unpinned product
publication would show names from a revision the run never read.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from forecast_parquet_support import parquet_bytes

from adapters.config import DemandForecastInputSettings
from adapters.jobs.demand_forecast_input.handler import (
    BuildRequest,
    InputBuilder,
    InputBuildFailedError,
    build_input,
    select_input,
)
from adapters.jobs.preset_artifact_publisher.retail_source import (
    RetailProductHierarchyResult,
    RetailProductIdentity,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.demand_forecast_policy import load_policy
from domain.forecasting.boundary import InputManifestV1, TitleMapV1
from domain.shared_kernel.values import DateRange
from ports.outbound.forecast_history_selection import (
    ForecastHistorySelectionRequest,
    ForecastHistorySelectionResult,
    PinnedContractHistory,
)
from ports.outbound.publication_selection import PinnedPublication
from ports.outbound.weekly_series_source import (
    RosterResult,
    StatementStat,
    UnloadResult,
    WeeklySeriesError,
    WeeklySeriesPlan,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
INPUT_END = date(2026, 10, 3)  # a Saturday
TODAY = date(2026, 10, 8)
ROSTER = ("amazon_asin:b0fixtur01", "amazon_asin:b0fixtur02")
SETTINGS = DemandForecastInputSettings(
    semantic_publications_table="publications",
    semantic_v2_bucket="semantic",
    athena_batch_workgroup="batch",
    semantic_v2_current_database="current",
    processing_bucket="adlytics-dev-demand-forecast",
    marketplace_key="amazon_us",
    model_artifact_digest="1" * 64,
    processor_code_identity="2" * 64,
    lock_digest="3" * 64,
    instance_type="ml.m5.xlarge",
    max_runtime_seconds=3600,
)


def _history(contract: str, span: DateRange, prefix: str) -> PinnedContractHistory:
    first, last = span.start, span.end
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return PinnedContractHistory(
        contract_id=contract,
        pins=tuple(PinnedPublication(f"{prefix}-{d}", f"{prefix}-{d}", 1, "a" * 64) for d in days),
        buckets=tuple(DateRange(d, d) for d in days),
        covered=(DateRange(first, last),) if days else (),
        newest_published_at=datetime(2026, 10, 5, tzinfo=UTC) if days else None,
    )


PRODUCTS = _history(
    "vendor-product-hierarchy", DateRange(date(2026, 10, 1), date(2026, 10, 1)), "prod"
)
SELECTED = ForecastHistorySelectionResult(
    sales=_history(
        "vendor-sales-by-asin", DateRange(date(2024, 9, 1), INPUT_END + timedelta(days=2)), "s"
    ),
    traffic=_history("vendor-traffic-by-asin", DateRange(date(2024, 9, 1), INPUT_END), "t"),
    products=PRODUCTS,
)


@dataclass
class FakeSelection:
    result: ForecastHistorySelectionResult
    requests: list[ForecastHistorySelectionRequest] = field(
        default_factory=list[ForecastHistorySelectionRequest]
    )

    def select(self, request: ForecastHistorySelectionRequest) -> ForecastHistorySelectionResult:
        self.requests.append(request)
        return self.result


@dataclass
class FakeTitles:
    result: RetailProductHierarchyResult | None

    def hierarchy(self, window: DateRange) -> RetailProductHierarchyResult | None:
        del window
        return self.result


@dataclass
class FakeObjects:
    bodies: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def list_sizes(self, prefix: str) -> dict[str, int]:
        return {k: len(v) for k, v in self.bodies.items() if k.startswith(prefix)}

    def sha256(self, key: str) -> str:
        import hashlib  # noqa: PLC0415

        return hashlib.sha256(self.bodies[key]).hexdigest()

    def tail(self, key: str, size: int) -> bytes:
        return self.bodies[key][-size:]

    def put(self, key: str, body: bytes) -> None:
        self.bodies[key] = body


def _parquet(rows: int) -> bytes:
    return parquet_bytes(["canonical_asin", "x"], rows)


@dataclass
class FakeWeekly:
    objects: FakeObjects
    shards: tuple[int, ...] = (0, 0)
    fail: str | None = None
    plans: list[WeeklySeriesPlan] = field(default_factory=list[WeeklySeriesPlan])

    def roster(self, plan: WeeklySeriesPlan) -> RosterResult:
        self.plans.append(plan)
        return RosterResult(ROSTER, self.shards, StatementStat("roster", "q1", 10, 1, 2))

    def unload(self, plan: WeeklySeriesPlan) -> UnloadResult:
        if self.fail:
            raise WeeklySeriesError(self.fail, "q2")
        bucket = f"s3://{SETTINGS.processing_bucket}/"
        for destination, rows in ((plan.target_destination, 8), (plan.traffic_destination, 2)):
            key = destination.removeprefix(bucket) + "shard=0/part-0.parquet"
            self.objects.put(key, _parquet(rows))
        return UnloadResult(
            (StatementStat("target", "q2", 100, 3, 4), StatementStat("traffic", "q3", 50, 3, 4))
        )


def _builder(
    titles: RetailProductHierarchyResult | None = None,
    weekly: FakeWeekly | None = None,
    marketplace: str = "amazon_us",
) -> tuple[InputBuilder, FakeObjects]:
    objects = weekly.objects if weekly else FakeObjects()
    fake = weekly or FakeWeekly(objects)
    builder = InputBuilder(
        settings=replace(SETTINGS, marketplace_key=marketplace),
        policy=load_policy(ROOT / "contracts/forecasting/demand-forecast.yaml"),
        bridge=load_commerce_identity_bridge(ROOT),
        selection=FakeSelection(SELECTED),
        titles=FakeTitles(titles),
        weekly=lambda _remaining: fake,
        objects=objects,
        today=lambda: TODAY,
    )
    return builder, objects


def _request(builder: InputBuilder, redrive: int = 0) -> BuildRequest:
    selected = select_input(builder)
    return BuildRequest("df-s1-unit", redrive, 0, str(selected["input_fingerprint"]))


def test_select_runs_on_the_newest_complete_saturday() -> None:
    builder, _ = _builder()
    result = select_input(builder)
    assert result["outcome"] == "run"
    assert result["input_end"] == "2026-10-03"
    assert result["sales_pins"] == 728


def test_an_unknown_marketplace_is_refused() -> None:
    builder, _ = _builder(marketplace="amazon_mars")
    with pytest.raises(InputBuildFailedError, match="MARKETPLACE_UNKNOWN"):
        select_input(builder)


def test_build_writes_a_valid_manifest_and_titles_under_the_attempt_prefix() -> None:
    titles = RetailProductHierarchyResult(
        {ROSTER[0]: RetailProductIdentity("b0fixtur01", "b0fixtur01", "Product 1")},
        ("prod-2026-10-01",),
    )
    builder, objects = _builder(titles)
    result = build_input(builder, _request(builder, redrive=1), lambda: 900.0)
    assert result["manifest_key"] == "runs/df-s1-unit/input/r1a0/manifest.json"
    manifest = InputManifestV1.model_validate_json(objects.bodies[str(result["manifest_key"])])
    assert manifest.build_attempt_id == "r1a0"
    assert manifest.expected_fence == 0
    assert [(o.kind, o.rows, o.shard) for o in manifest.parquet_objects] == [
        ("target", 8, 0),
        ("traffic", 2, 0),
    ]
    assert len(manifest.pins.sales) == 728
    assert manifest.shards[0].asin_count == 2
    title_map = TitleMapV1.model_validate_json(objects.bodies[manifest.titles_key])
    assert [(e.display_asin, e.title) for e in title_map.entries] == [
        ("B0FIXTUR01", "Product 1"),
        ("B0FIXTUR02", None),
    ]


def test_no_hierarchy_gives_all_null_titles_and_no_failure() -> None:
    builder, objects = _builder(None)
    result = build_input(builder, _request(builder), lambda: 900.0)
    manifest = InputManifestV1.model_validate_json(objects.bodies[str(result["manifest_key"])])
    title_map = TitleMapV1.model_validate_json(objects.bodies[manifest.titles_key])
    assert [e.title for e in title_map.entries] == [None, None]


def test_a_hierarchy_naming_an_unpinned_publication_is_selection_moved() -> None:
    titles = RetailProductHierarchyResult({}, ("prod-unpinned",))
    builder, _ = _builder(titles)
    with pytest.raises(InputBuildFailedError, match="SELECTION_MOVED"):
        build_input(builder, _request(builder), lambda: 900.0)


def test_a_moved_input_fingerprint_is_selection_moved() -> None:
    builder, _ = _builder()
    with pytest.raises(InputBuildFailedError, match="SELECTION_MOVED"):
        build_input(builder, BuildRequest("df-s1-unit", 0, 0, "f" * 64), lambda: 900.0)


def test_a_roster_the_shard_rule_does_not_reproduce_is_refused() -> None:
    objects = FakeObjects()
    builder, _ = _builder(weekly=FakeWeekly(objects, shards=(0, 1)))
    with pytest.raises(InputBuildFailedError, match="SHARD_PLAN_MISMATCH"):
        build_input(builder, _request(builder), lambda: 900.0)


def test_a_statement_timeout_surfaces_as_its_category() -> None:
    objects = FakeObjects()
    builder, _ = _builder(weekly=FakeWeekly(objects, fail="athena_timeout"))
    with pytest.raises(InputBuildFailedError, match="athena_timeout"):
        build_input(builder, _request(builder), lambda: 900.0)
