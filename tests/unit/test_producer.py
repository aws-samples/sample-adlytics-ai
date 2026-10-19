# SPDX-License-Identifier: MIT-0
"""Governed AMC pack, registration, result, and manifest conformance."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.manifests import read_manifest
from adapters.producer.landing import write_governed_manifest
from adapters.producer.packs import (
    load_query_packs,
    pack_by_id,
    validate_attribution_model_binding,
)
from adapters.producer.registrations import registration_from_json, write_registration
from adapters.producer.results import parse_csv_result
from domain.producer.models import AccessTier, LiveValidationPolicy, ScheduleKind
from domain.producer.planning import (
    PlanLandingRequest,
    PlanManifestRequest,
    canonical_result_key,
    plan_landing,
    plan_manifest,
)
from domain.producer.validation import (
    REQUIRED_RESULT_COLUMNS,
    validate_registration,
    validate_result,
    validate_result_window,
)
from domain.semantics.identity import SourceIdentifier

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-050", "ADR-030", "ADR-053")]
ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 8, 10, tzinfo=UTC)
END = datetime(2026, 8, 17, tzinfo=UTC)


def _registration(pack_id: str = "amc-dsp-impressions"):
    pack = pack_by_id(ROOT, pack_id)
    return pack, plan_landing(
        PlanLandingRequest(
            pack=pack,
            execution_id="integration_probe_execution_001",
            workflow_id=f"adlytics_{pack_id.replace('-', '_')}_v1",
            schedule_kind=ScheduleKind.ADHOC,
            schedule_id=None,
            source_scope=SourceIdentifier("amc.entity", "fictional-producer-entity"),
            marketplace="US",
            period_start=date(2026, 8, 10),
            period_end=date(2026, 8, 16),
            time_window_start=START,
            time_window_end=END,
            source_watermark=date(2026, 8, 19),
            retrieved_at=datetime(2026, 8, 19, 6, tzinfo=UTC),
            object_version_id="incoming-version-001",
        )
    )


def _csv(
    pack_id: str = "amc-dsp-impressions",
    *,
    reverse: bool = False,
    suppressed: bool = False,
) -> bytes:
    pack, registration = _registration(pack_id)
    columns = list(pack.expected_columns)
    if reverse:
        columns.reverse()
    row = dict.fromkeys(columns, "1")
    row["time_window_start"] = str(registration.option_set["timeWindowStart"])
    row["time_window_end"] = str(registration.option_set["timeWindowEnd"])
    row["rows_suppressed"] = "true" if suppressed else "false"
    row["suppression_reason"] = "1 source users < 2 required" if suppressed else ""
    row["distinct_user_count"] = "2"
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode()


def _deduplicated_contract():
    return next(
        contract
        for _path, contract in load_contracts(ROOT)
        if contract.contract_id == "amc-deduplicated-temporal-attribution"
    )


def test_registry_contains_every_governed_pack() -> None:
    packs = load_query_packs(ROOT)
    assert {pack.pack_id for pack in packs} == {
        "amc-acquisition-cohorts",
        "amc-acquisition-cohorts-arp",
        "amc-attributed-conversion",
        "amc-audience-performance",
        "amc-campaign-dsp",
        "amc-campaign-sponsored",
        "amc-deduplicated-temporal-attribution",
        "amc-dsp-clicks",
        "amc-dsp-impressions",
        "amc-dsp-reach",
        "amc-dsp-segment-names",
        "amc-dsp-views",
        "amc-exposure-frequency-channel",
        "amc-exposure-frequency-platform",
        "amc-exposure-groups",
        "amc-new-to-brand-buyers-campaign",
        "amc-new-to-brand-buyers-channel",
        "amc-new-to-brand-buyers-platform",
        "amc-reach-frequency",
        "amc-retail-purchases",
        "amc-segment-buyer-profile-dsp-exposed",
        "amc-segment-buyer-profile-inmarket",
        "amc-segment-buyer-profile-lifestyle",
        "amc-segment-metadata",
    }
    assert len({pack.digest for pack in packs}) == len(packs)


def test_attribution_pack_binds_registry_contract_and_sql_vocabularies() -> None:
    pack = pack_by_id(ROOT, "amc-deduplicated-temporal-attribution")

    validate_attribution_model_binding(ROOT, _deduplicated_contract(), pack.sql)


def test_attribution_pack_rejects_contract_vocabulary_drift() -> None:
    contract = _deduplicated_contract()
    fields = tuple(
        replace(field, value_domain=("first_touch_v1",))
        if field.target_column == "attribution_model_key"
        else field
        for field in contract.fields
    )

    pack = pack_by_id(ROOT, "amc-deduplicated-temporal-attribution")
    with pytest.raises(ValueError, match="attribution model vocabulary drift"):
        validate_attribution_model_binding(ROOT, replace(contract, fields=fields), pack.sql)


def test_attribution_pack_rejects_registry_vocabulary_drift(tmp_path: Path) -> None:
    registry = (ROOT / "contracts/shared/attribution-models.yaml").read_text()
    path = tmp_path / "contracts/shared/attribution-models.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(registry.replace("linear_v1", "invented_linear_v1", 1))
    pack = pack_by_id(ROOT, "amc-deduplicated-temporal-attribution")

    with pytest.raises(ValueError, match="attribution model vocabulary drift"):
        validate_attribution_model_binding(tmp_path, _deduplicated_contract(), pack.sql)


def test_attribution_pack_rejects_missing_or_repeated_sql_branches() -> None:
    contract = _deduplicated_contract()
    pack = pack_by_id(ROOT, "amc-deduplicated-temporal-attribution")

    with pytest.raises(ValueError, match="attribution model vocabulary drift"):
        validate_attribution_model_binding(
            ROOT,
            contract,
            pack.sql.replace("'linear_v1' AS attribution_model_key", "'invented' AS model"),
        )
    with pytest.raises(ValueError, match="repeats an attribution_model_key branch"):
        validate_attribution_model_binding(
            ROOT,
            contract,
            f"{pack.sql} SELECT 'first_touch_v1' AS attribution_model_key",
        )


def test_a_not_applicable_attribution_column_binds_no_vocabulary_until_a_branch_writes_it() -> None:
    """Packs 6-7 carry attribution_model_key for a credit branch not built yet (plan D11, D12)."""
    contract = next(
        item
        for _, item in load_contracts(ROOT)
        if item.contract_id == "amc-exposure-frequency-channel"
    )
    pack = pack_by_id(ROOT, "amc-exposure-frequency-channel")
    validate_attribution_model_binding(ROOT, contract, pack.sql)
    # Paired control: SQL that emits a model on that contract is a branch it doesn't declare.
    with pytest.raises(ValueError, match="declares not applicable"):
        validate_attribution_model_binding(
            ROOT, contract, f"{pack.sql} SELECT 'linear_v1' AS attribution_model_key"
        )
    # And a declared model vocabulary still binds: the deduplicated pack's drift is refused.
    with pytest.raises(ValueError, match="attribution model vocabulary drift"):
        validate_attribution_model_binding(ROOT, _deduplicated_contract(), "SELECT 1")


def test_every_pack_is_single_line_aggregated_and_self_describing() -> None:
    for pack in load_query_packs(ROOT):
        assert "\n" not in pack.sql
        assert "SELECT *" not in pack.sql.upper()
        assert "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start" in pack.sql
        assert "BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end" in pack.sql
        assert set(pack.expected_columns) >= REQUIRED_RESULT_COLUMNS


def test_unsafe_fields_are_absent_and_the_remaining_risks_are_typed() -> None:
    impressions = pack_by_id(ROOT, "amc-dsp-impressions")
    retail = pack_by_id(ROOT, "amc-retail-purchases")
    reach = pack_by_id(ROOT, "amc-reach-frequency")
    assert "impression_cost" not in impressions.expected_columns
    assert "reach_users" not in impressions.expected_columns
    assert "unit_price" not in retail.expected_columns
    assert "revenue_purchased" in retail.expected_columns
    assert retail.access_tier is AccessTier.PAID_FEATURE
    assert retail.live_validation is LiveValidationPolicy.CAPABILITY_CONDITIONAL
    assert reach.safety_blockers == ()
    assert reach.reject_suppressed_rows is True


def test_registration_round_trips_with_exact_pack_query_and_object_version() -> None:
    pack, registration = _registration()
    encoded = write_registration(registration)
    assert registration_from_json(encoded) == registration
    assert registration.schema_version == "2"
    assert registration.source_scope == SourceIdentifier("amc.entity", "fictional-producer-entity")
    assert registration.object.key == (
        "incoming/amc/dsp_impressions/integration_probe_execution_001/result.csv"
    )
    assert registration.query_pack.query_digest == pack.query_digest
    assert validate_registration(pack, registration).valid


def test_result_header_is_order_independent_and_window_bound() -> None:
    pack, registration = _registration()
    summary, evidence = parse_csv_result(_csv(reverse=True))
    assert validate_result(pack, summary).valid
    assert validate_result_window(registration, summary).valid
    assert set(evidence.declared_columns) == set(pack.expected_columns)
    assert evidence.byte_count > 0
    assert evidence.checksum.startswith("sha256:")


def test_result_reports_missing_and_unexpected_columns_together() -> None:
    pack, _registration_value = _registration()
    body = _csv().decode().replace("currency_iso_code", "invented_column")
    summary, _evidence = parse_csv_result(body.encode())
    report = validate_result(pack, summary)
    assert {finding.code for finding in report.findings} >= {
        "RESULT_COLUMNS_MISSING",
        "RESULT_COLUMNS_UNEXPECTED",
    }


def test_reach_pack_refuses_any_suppressed_non_additive_result() -> None:
    pack, _registration_value = _registration("amc-reach-frequency")
    clean, _clean_evidence = parse_csv_result(_csv("amc-reach-frequency"))
    suppressed, _suppressed_evidence = parse_csv_result(
        _csv("amc-reach-frequency", suppressed=True)
    )
    assert validate_result(pack, clean).valid
    report = validate_result(pack, suppressed)
    assert any(finding.code == "RESULT_SUPPRESSION_REFUSED" for finding in report.findings)


def test_final_manifest_binds_destination_version_and_every_governed_digest() -> None:
    pack, registration = _registration()
    summary, evidence = parse_csv_result(_csv())
    assert validate_result(pack, summary).valid
    planned = plan_manifest(
        PlanManifestRequest(
            registration=registration,
            pack=pack,
            evidence=evidence,
            destination_version_id="canonical-version-001",
        )
    )
    rendered = write_governed_manifest(planned)
    parsed = read_manifest(rendered)
    assert parsed == planned.manifest
    assert parsed.objects[0].version_id == "canonical-version-001"
    assert parsed.governance == pack.governance
    assert parsed.manifest_key == canonical_result_key(registration).replace(
        "result.csv", "manifest.json"
    )
    document = json.loads(rendered)
    assert document["governance"]["query_pack_digest"] == pack.digest


def test_governed_producer_refuses_legacy_raw_layout() -> None:
    pack, registration = _registration()
    summary, evidence = parse_csv_result(_csv())
    assert validate_result(pack, summary).valid
    planned = plan_manifest(
        PlanManifestRequest(
            registration=registration,
            pack=pack,
            evidence=evidence,
            destination_version_id="canonical-version-001",
        )
    )
    legacy = replace(planned, manifest=replace(planned.manifest, layout_version=1))

    with pytest.raises(ValueError, match="layout v2"):
        write_governed_manifest(legacy)


def test_scheduled_execution_requires_a_schedule_id() -> None:
    pack = pack_by_id(ROOT, "amc-dsp-clicks")
    with pytest.raises(ValueError, match="schedule_id"):
        plan_landing(
            PlanLandingRequest(
                pack=pack,
                execution_id="scheduled_execution",
                workflow_id="scheduled_workflow",
                schedule_kind=ScheduleKind.SCHEDULED,
                schedule_id=None,
                source_scope=SourceIdentifier("amc.entity", "fictional-producer-entity"),
                marketplace="US",
                period_start=START.date(),
                period_end=date(2026, 8, 16),
                time_window_start=START,
                time_window_end=END,
                source_watermark=date(2026, 8, 19),
                retrieved_at=datetime(2026, 8, 19, 6, tzinfo=UTC),
                object_version_id="incoming-version-001",
            )
        )


def test_registration_refuses_subsecond_retrieval_identity() -> None:
    _pack, registration = _registration()
    with pytest.raises(ValueError, match="whole-second precision"):
        replace(
            registration,
            retrieved_at=registration.retrieved_at.replace(microsecond=1),
        )


def test_registration_refuses_fractional_offset_retrieval_identity() -> None:
    _pack, registration = _registration()
    fractional_offset = timezone(timedelta(seconds=1, microseconds=500_000))
    retrieved = registration.retrieved_at.replace(tzinfo=fractional_offset)
    assert retrieved.microsecond == 0
    assert retrieved.astimezone(UTC).microsecond == 500_000

    with pytest.raises(ValueError, match="whole-second precision"):
        replace(registration, retrieved_at=retrieved)


def test_live_amc_millisecond_z_window_spelling_matches_registration() -> None:
    pack, registration = _registration()
    body = (
        _csv()
        .replace(
            b"2026-08-10T00:00:00",
            b"2026-08-10T00:00:00.000Z",
        )
        .replace(
            b"2026-08-17T00:00:00",
            b"2026-08-17T00:00:00.000Z",
        )
    )
    summary, _evidence = parse_csv_result(body)
    assert validate_result(pack, summary).valid
    assert validate_result_window(registration, summary).valid


def test_malformed_suppression_cell_is_refused_instead_of_read_as_clean() -> None:
    with pytest.raises(ValueError, match="rows_suppressed"):
        parse_csv_result(_csv().replace(b",false,", b",yes,"))


def test_zero_row_registration_still_requires_parseable_ordered_window() -> None:
    pack, registration = _registration("amc-retail-purchases")
    malformed = replace(
        registration,
        option_set={**registration.option_set, "timeWindowStart": "not-a-timestamp"},
    )
    report = validate_registration(pack, malformed)
    assert any(finding.code == "REGISTRATION_PERIOD_WINDOW" for finding in report.findings)


def test_amc_registration_requires_source_watermark_before_canonical_writes() -> None:
    pack, registration = _registration()
    report = validate_registration(pack, replace(registration, source_watermark=None))
    assert any(finding.code == "REGISTRATION_WATERMARK_REQUIRED" for finding in report.findings)


def test_execution_option_policy_and_size_are_bound_into_pack_digest() -> None:
    pack = pack_by_id(ROOT, "amc-dsp-clicks")
    changed_options = replace(
        pack,
        execution_options=tuple(
            (name, "changed" if name == "timeWindowTimeZone" else value)
            for name, value in pack.execution_options
        ),
    )
    changed_size = replace(pack, max_result_bytes=pack.max_result_bytes + 1)
    assert changed_options.digest != pack.digest
    assert changed_size.digest != pack.digest
    assert pack.max_result_bytes == 20_000_000


def test_registration_refuses_option_not_bound_by_pack_digest() -> None:
    pack, registration = _registration()
    widened = replace(
        registration,
        option_set={**registration.option_set, "ungovernedOption": "enabled"},
    )
    report = validate_registration(pack, widened)
    assert any(finding.code == "REGISTRATION_OPTION_KEYS" for finding in report.findings)


def test_registration_v2_refuses_missing_blank_unknown_and_legacy_source_scope() -> None:
    _pack, registration = _registration()
    baseline = json.loads(write_registration(registration))

    missing = dict(baseline)
    missing.pop("source_scope")
    with pytest.raises(ValueError, match="source_scope"):
        registration_from_json(json.dumps(missing))

    blank = json.loads(json.dumps(baseline))
    blank["source_scope"]["value"] = " "
    with pytest.raises(ValueError, match="source_scope"):
        registration_from_json(json.dumps(blank))

    unknown = json.loads(json.dumps(baseline))
    unknown["source_scope"]["unexpected"] = "value"
    with pytest.raises(ValueError, match="source_scope"):
        registration_from_json(json.dumps(unknown))

    legacy = json.loads(json.dumps(baseline))
    legacy["schema_version"] = "1"
    with pytest.raises(ValueError, match="schema_version"):
        registration_from_json(json.dumps(legacy))


def test_source_scope_separates_canonical_keys_and_amc_namespace_is_governed() -> None:
    pack, registration = _registration()
    other = replace(
        registration,
        source_scope=SourceIdentifier("amc.entity", "fictional-other-producer-entity"),
    )
    assert canonical_result_key(registration) != canonical_result_key(other)

    wrong_namespace = replace(
        registration,
        source_scope=SourceIdentifier("delivery.marketplace", registration.marketplace),
    )
    report = validate_registration(pack, wrong_namespace)
    assert any(finding.code == "REGISTRATION_SOURCE_SCOPE_NAMESPACE" for finding in report.findings)
