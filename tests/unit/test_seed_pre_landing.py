# SPDX-License-Identifier: MIT-0
"""Local pre-landing checks for the shaped dev seed (design §3.3 "Pre-landing checks", §2.5).

Each check runs a generated delivery through the real producer, ingestion or serving code
in-process, before anything lands, and each carries a positive control that shows the check can
fail.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import cast

import pytest
from fixture_artifacts_support import ROOT, fixture_catalog

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import write_manifest
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.fixture_generation.shape import Pause
from adapters.jobs.fixture_generation.vendor import (
    reconcile_vendor_aggregates,
    reported_units_adjustments,
)
from adapters.jobs.land_fixtures import bundle as bundle_module
from adapters.jobs.land_fixtures.bundle import LandedDelivery
from adapters.producer.packs import pack_by_id
from adapters.producer.results import parse_csv_result
from domain.curation.ingestion import (
    AssessmentRequest,
    ContractLoad,
    DeliveryAssessment,
    assess_source_delivery,
)
from domain.curation.landing import partition_for
from domain.curation.mapping import MappingRequest, map_rows
from domain.producer.validation import validate_result

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

_IMPRESSIONS = "generated/amc-dsp-impressions.csv"
_SALES = "generated/vendor-sales-by-asin.json"
_SALES_CONTRACTS = frozenset({"vendor-sales-by-asin", "vendor-sales-reported-total"})


def _deliveries(fixture: str) -> list[LandedDelivery]:
    context = bundle_module._plan_context(ROOT, fixture_catalog().shape)  # pyright: ignore[reportPrivateUsage]
    return bundle_module._plan_fixture(context, fixture)  # pyright: ignore[reportPrivateUsage]


def _assess(
    delivery: LandedDelivery, contract_ids: frozenset[str], text: str | None = None
) -> tuple[DeliveryAssessment, tuple[ContractLoad, ...]]:
    """Run one planned delivery through the ingestion reader, mapping and assessment."""
    contracts = [item for _, item in load_contracts(ROOT) if item.contract_id in contract_ids]
    body = delivery.data[0].body.decode() if text is None else text
    document = read(
        DeliveredFile(body, contracts[0].source_format, write_manifest(delivery.manifest))
    )
    loads = tuple(
        ContractLoad(
            contract,
            partition_for(delivery.manifest, contract),
            map_rows(MappingRequest(contract, document)),
        )
        for contract in contracts
    )
    return assess_source_delivery(
        AssessmentRequest(
            manifest=delivery.manifest,
            document=document,
            loads=loads,
            registry=load_registry(ROOT),
        )
    ), loads


def _violations(assessment: DeliveryAssessment) -> list[str]:
    return [
        violation.detail
        for item in assessment.assessments
        if item.outcome.quarantines
        for violation in item.outcome.violations
    ]


def _pause() -> Pause:
    return next(event for event in fixture_catalog().shape.events if isinstance(event, Pause))


def _pause_source_id() -> str:
    campaign = next(item for item in fixture_catalog().campaigns if item.key == _pause().campaign)
    return campaign.source_aliases["amc.dsp.campaign_id_string"]


# --- (1) a zero-delivery row is accepted at ingest and not read as suppressed --------------


def _pause_day_row() -> dict[str, str]:
    day = _pause().first.isoformat()
    rows = csv.DictReader(io.StringIO((ROOT / "tests/fixtures" / _IMPRESSIONS).read_text()))
    return next(
        row
        for row in rows
        if row["campaign_id_string"] == _pause_source_id() and row["impression_date_utc"] == day
    )


def _result_csv(row: dict[str, str], *, suppressed: bool) -> bytes:
    pack = pack_by_id(ROOT, "amc-dsp-impressions")
    columns = list(pack.expected_columns)
    values = dict.fromkeys(columns, "0") | {
        key: value for key, value in row.items() if key in columns
    }
    values |= {
        "time_window_start": f"{row['impression_date_utc']}T00:00:00Z",
        "time_window_end": f"{row['impression_date_utc']}T23:59:59Z",
        "rows_suppressed": "true" if suppressed else "false",
        "suppression_reason": "1 source users < 2 required" if suppressed else "",
    }
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerow(values)
    return stream.getvalue().encode()


def test_1_a_zero_delivery_row_passes_producer_validation_unsuppressed() -> None:
    row = _pause_day_row()
    assert (row["impressions"], row["total_cost"], row["rows_suppressed"]) == ("0", "0", "false")
    pack = pack_by_id(ROOT, "amc-dsp-impressions")
    summary, _evidence = parse_csv_result(_result_csv(row, suppressed=False))
    assert validate_result(pack, summary).valid
    assert (summary.row_count, summary.suppressed_row_count) == (1, 0)
    # Positive control: the same row flagged suppressed is counted as suppressed, and a pack
    # that refuses suppressed rows refuses it.
    flagged, _evidence = parse_csv_result(_result_csv(row, suppressed=True))
    assert flagged.suppressed_row_count == 1
    reach = pack_by_id(ROOT, "amc-reach-frequency")
    assert reach.reject_suppressed_rows
    assert any(
        finding.code == "RESULT_SUPPRESSION_REFUSED"
        for finding in validate_result(reach, flagged).findings
    )


def test_1_a_zero_delivery_row_is_admitted_at_ingest_and_not_read_as_suppressed() -> None:
    pause = _pause()
    delivery = next(
        item
        for item in _deliveries(_IMPRESSIONS)
        if item.run_day == pause.last
        and item.manifest.period_start <= pause.first
        and pause.first <= item.manifest.period_end
    )
    assessment, loads = _assess(delivery, frozenset({"amc-dsp-impressions"}))
    assert _violations(assessment) == []
    rows = [
        row
        for row in loads[0].rows
        if row.get("campaign_key") == _pause_source_id()
        and str(row.get("date_key")) == pause.first.isoformat()
    ]
    assert len(rows) == 1
    assert rows[0]["rows_suppressed"] is False
    assert int(cast("int", rows[0]["impressions"])) == 0
    # Positive control: the same delivery with the row flagged suppressed maps as suppressed.
    text = delivery.data[0].body.decode()
    flagged_line = next(
        line
        for line in text.splitlines()
        if line.startswith(f"{pause.first.isoformat()},{_pause_source_id()},")
    )
    # The suppression columns close the row in AMC's delivered order: count, reason, flag.
    assert flagged_line.endswith(",2,,false")
    flagged = text.replace(flagged_line, flagged_line.removesuffix(",,false") + ",threshold,true")
    _assessment, flagged_loads = _assess(delivery, frozenset({"amc-dsp-impressions"}), flagged)
    assert any(
        row.get("campaign_key") == _pause_source_id() and row["rows_suppressed"] is True
        for row in flagged_loads[0].rows
    )


# --- (3) mismatch reachability: a mismatched reported total quarantines the day -------------


def test_3_the_mismatch_event_is_removed_and_no_served_vendor_day_quarantines() -> None:
    shape = fixture_catalog().shape
    # RC-5: the event was removed in a recorded commit because of the positive control below.
    assert reported_units_adjustments(shape) == {}
    cut = shape.fixture_span.last_day - timedelta(days=27)
    served = [item for item in _deliveries(_SALES) if item.manifest.period_start >= cut]
    assert len(served) >= 28
    for delivery in served:
        assessment, _loads = _assess(delivery, _SALES_CONTRACTS)
        assert _violations(assessment) == [], delivery.fixture


def test_3_a_declared_units_delta_would_quarantine_its_whole_day() -> None:
    """Positive control: the removed event's delta (3 units) applied to one served day."""
    day = fixture_catalog().shape.event_anchor - timedelta(days=3)
    delivery = next(
        item
        for item in _deliveries(_SALES)
        if item.manifest.period_start == day and "#daily-run=" in item.fixture
    )
    document = json.loads(delivery.data[0].body.decode())
    mismatched = reconcile_vendor_aggregates(document, {day.isoformat(): 3})
    assessment, loads = _assess(delivery, _SALES_CONTRACTS, json.dumps(mismatched))
    violations = _violations(assessment)
    assert violations and all("units_shipped" in detail for detail in violations)
    # Every contract of the delivery quarantines, so the day's detail rows are withdrawn too.
    quarantined = {item.contract_id for item in assessment.assessments if item.outcome.quarantines}
    assert quarantined == {load.contract.contract_id for load in loads}
