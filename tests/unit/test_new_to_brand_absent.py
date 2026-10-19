# SPDX-License-Identifier: MIT-0
"""New-to-brand stays off the Scorecard, and its halo-inclusive metric is gated (F-1, FEAT-004).

FEAT-001 removed S1.2 because it wasn't dark: its metric reached the agent catalog over rows that
read NULL before pack version 2, and its column became required at ingestion DQ. New customers
brings the metric back with a pack-version-2 coverage gate, so this test now proves the two
properties that keep it safe: the Scorecard artifact carries no new-to-brand section, and the
metric is answered only over a window whose every day is pack-v2 covered. Each assertion keeps a
positive control so it can't pass vacuously.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from datetime import date, timedelta
from typing import cast

import pytest

from adapters.inbound.tool_handlers.catalog import CatalogTool, CatalogToolRequest, handle
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.preset_artifact_publisher.scorecard_source import (
    ScorecardMeasurementBatchRequest,
    scorecard_measurement_batch,
)
from adapters.outbound.metric_dictionary import load_catalog
from adapters.producer.packs import pack_by_id
from domain.measurement.pack_coverage import PACK_COVERAGE_REFUSED, PackDayVersions
from domain.measurement.planning import FactShape, PlanRequest, QueryPlan, plan
from domain.presentation.scorecard import (
    NetworkScorecardArtifact,
    NetworkScorecardSlotRequest,
    ScorecardOutcome,
    network_scorecard_artifact_payload,
    network_scorecard_slot,
)
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)
GATED = "purchases_new_to_brand_incl_halo_12mo"
GATED_LABEL = "New-to-brand purchases including brand halo (12 month lookback)"
GATED_SOURCE = "new_to_brand_total_purchases"
KEPT = "purchases_new_to_brand_12mo"
KEPT_SOURCE = "new_to_brand_purchases"
_NEW_TO_BRAND_METRICS = frozenset(
    {
        KEPT,
        GATED,
        "revenue_new_to_brand_12mo",
        "ntb_rate_12mo",
        "ntb_revenue_share",
        "cost_per_ntb",
        "buyers_new_to_brand_12mo",
        "cost_per_new_to_brand_buyer_12mo",
    }
)
WINDOW = DateRange(date(2026, 8, 10), date(2026, 8, 16))


def _listed() -> tuple[str, ...]:
    """Every metric label `catalog_list_metrics` lists, across its inventory collections."""
    response = handle(
        CatalogToolRequest(
            tool=CatalogTool.LIST_METRICS,
            catalog=load_catalog(ROOT),
            computed_at=NOW,
            correlation_id="corr-ntb-gated",
        )
    )
    grounding = response.metadata.grounding
    assert grounding is not None
    return tuple(item for collection in grounding.collections for item in collection.items)


def _ruleset(table: str) -> dict[str, object]:
    generated = json.loads((ROOT / "contracts/generated/dq-rulesets.json").read_text())
    matches = [item for item in generated["rulesets"] if item["table"] == table]
    assert len(matches) == 1
    return matches[0]


def test_the_scorecard_measures_and_carries_no_new_to_brand_section() -> None:
    catalog = load_catalog(ROOT)
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    batch = scorecard_measurement_batch(
        ScorecardMeasurementBatchRequest(
            catalog, contracts, ("first_touch_v1",), WINDOW, None, NOW, ("amazon_us",)
        )
    )
    measured = {plan.logical.metric.name for plan in batch.prepared.plans}
    assert measured & _NEW_TO_BRAND_METRICS == set()
    # Positive control: the batch does measure the Scorecard's attributed revenue.
    assert "attributed_revenue_14d" in measured
    artifact = NetworkScorecardArtifact(
        outcome=ScorecardOutcome.EMPTY,
        period=WINDOW,
        prior_period=None,
        cards=(),
        tacos_rows=(),
        cross_network_spend=(),
        findings=(),
        insights=(network_scorecard_slot(NetworkScorecardSlotRequest((), "rev-1", "quiet")),),
        preset_label="7 days",
    )
    payload = network_scorecard_artifact_payload(artifact)
    # UI slice 9a's browser-shaped body (render format 2): still no new-to-brand section.
    assert set(payload) == {
        "type",
        "version",
        "outcome",
        "outcome_text",
        "preset_label",
        "period",
        "period_label",
        "prior_period",
        "scope_text",
        "cards",
        "tacos",
        "cross_network_spend",
        "findings",
        "insights",
        "insights_heading",
        "insight_cards",
        "also_changed",
        "retail_sales_heading",
        "pointers",
        "evidence_status",
        "not_available_text",
        "boundary_heading",
        "boundary",
    }
    assert "new_to_brand" not in json.dumps(payload)
    # Gateway products are only named by the pointer line to the New customers tab.
    pointers = cast("list[dict[str, object]]", payload.pop("pointers"))
    assert "gateway" not in json.dumps(payload)
    assert [item["tab_id"] for item in pointers if "gateway" in json.dumps(item)] == [
        "new-customers"
    ]


def test_the_catalog_lists_the_gated_metric_once_beside_the_advertised_only_count() -> None:
    catalog = load_catalog(ROOT)
    gated = catalog.metric(GATED)
    assert gated is not None
    assert gated.label == GATED_LABEL
    listed = _listed()
    # 64 before pack 5, which adds 15 metrics and the deferred window spend (design §6.3); packs
    # 6 and 7 add 11 frequency metrics; pack 8 adds 9 segment delivery metrics; packs 14 and 15
    # add 15 cohort metrics (design §6.6).
    assert len(listed) == 124
    assert listed.count(GATED_LABEL) == 1
    kept = catalog.metric(KEPT)
    assert kept is not None
    assert listed.count(kept.label) == 1


def test_the_gated_metric_plans_only_over_a_pack_2_covered_window() -> None:
    catalog = load_catalog(ROOT)
    shape = FactShape.for_fact(
        "fct_attributed_conversion", tuple(contract for _, contract in load_contracts(ROOT))
    )
    days: dict[date, int | None] = {
        WINDOW.start + timedelta(days=offset): 2 for offset in range(WINDOW.days)
    }

    def planned(versions: dict[date, int | None]) -> QueryPlan:
        return plan(
            catalog,
            PlanRequest(
                GATED,
                ("channel", "ad_product"),
                WINDOW,
                {"marketplace_key": "amazon_us"},
                pack_coverage=(PackDayVersions("amc-attributed-conversion", versions),),
            ),
            shape,
        )

    with pytest.raises(ValidationError) as refused:
        planned({**days, WINDOW.start: 1})
    assert refused.value.code == PACK_COVERAGE_REFUSED
    # Positive control: every day at pack version 2 plans, one channel and ad product at a time.
    covered = planned(days)
    assert (covered.fact, covered.group_by, covered.measures) == (
        "fct_attributed_conversion",
        ("channel_key", "ad_product_key"),
        (GATED,),
    )


def test_ingestion_dq_does_not_require_the_column() -> None:
    for table in ("fct_attributed_conversion", "fct_attributed_conversion_withheld"):
        ruleset = _ruleset(table)
        assert GATED not in json.dumps(ruleset)
    rules = _ruleset("fct_attributed_conversion")
    required = cast("dict[str, list[str]]", rules["runtimeRules"])["requiredColumns"]
    # Positive control: the advertised-only column is required, in both forms.
    assert required.count(KEPT) == 1
    assert str(rules["ruleset"]).count(f'IsComplete "{KEPT}"') == 1


def _named_columns(document: object, name: str) -> list[dict[str, object]]:
    """Every column object named `name`, in document order."""
    found: list[dict[str, object]] = []
    if isinstance(document, dict):
        mapping = cast("dict[str, object]", document)
        if mapping.get("name") == name:
            found.append(mapping)
        for value in mapping.values():
            found.extend(_named_columns(value, name))
    elif isinstance(document, list):
        for value in cast("list[object]", document):
            found.extend(_named_columns(value, name))
    return found


def test_every_generated_table_carries_the_column_nullable_under_options() -> None:
    for name, count in (("tables.json", 2), ("v2-tables.json", 4)):
        generated = json.loads((ROOT / "contracts/generated" / name).read_text())
        text = json.dumps(generated)
        assert text.count(f'"name": "{GATED}"') == count
        assert text.count(f'"name": "{KEPT}"') == count
    v2 = json.loads((ROOT / "contracts/generated/v2-tables.json").read_text())
    column = {
        "name": GATED,
        "type": "bigint",
        "nullable": True,
        "comment": f"amc-attributed-conversion: {GATED_SOURCE}",
        "nullMeaning": "not_applicable_under_options",
    }
    # Fact then withheld, candidate then current: appended, so each keeps its own field id.
    assert _named_columns(v2, GATED) == [
        {**column, "fieldId": 35},
        {**column, "fieldId": 34},
        {**column, "fieldId": 35},
        {**column, "fieldId": 34},
    ]


def test_the_attributed_conversion_pack_is_at_version_2_and_retains_version_1() -> None:
    packs = ROOT / "contracts/amc/query-packs"
    definition = json.loads((packs / "amc-attributed-conversion.json").read_text())
    assert definition["packVersion"] == 2
    assert definition["outputColumns"].count(GATED_SOURCE) == 1
    assert [item["packVersion"] for item in definition["retainedVersions"]] == [1]
    sql = (packs / "amc-attributed-conversion.sql").read_text()
    assert sql.count(f"SUM({GATED_SOURCE}) AS {GATED_SOURCE}") == 1
    assert sql.count(f"SUM({KEPT_SOURCE}) AS {KEPT_SOURCE}") == 1
    pack = pack_by_id(ROOT, "amc-attributed-conversion")
    assert (pack.pack_version, [item.query_pack_version for item in pack.retained_governance]) == (
        2,
        [1],
    )
