# SPDX-License-Identifier: MIT-0
"""Registration event parsing and Finalizer orchestration at port seams, without AWS."""

from __future__ import annotations

# This module intentionally exercises the handler's private composition seams.
# pyright: reportPrivateUsage=false
import csv
import importlib
import inspect
import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from adapters.config import settings_without_client_secret, tracing_settings
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.telemetry.reporting import Report
from adapters.producer.packs import pack_by_id
from adapters.producer.registrations import write_registration
from domain.producer.models import (
    GovernedQueryPack,
    InputParameter,
    ParameterDataType,
    PeriodGrain,
    PeriodPolicy,
    ScheduleKind,
    SourceRegistration,
    digest_bytes,
)
from domain.producer.parameters import CommerceIdentityRequest, resolve_commerce_identities
from domain.producer.planning import PlanLandingRequest, plan_landing
from domain.producer.status import (
    RegistrationAttempt,
    RegistrationState,
    RegistrationStatus,
    RegistrationWriteOutcome,
)
from domain.semantics.identity import SourceIdentifier
from domain.shared_kernel.errors import DependencyError, ErrorClass
from ports.outbound.producer_landing import StoredProducerObject, VersionedProducerObject

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-050", "ADR-053")]
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 19, 7, tzinfo=UTC)
BUCKET = "adlytics-test-raw"


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "ADL_ENVIRONMENT": "test",
        "ADL_AWS_REGION": "us-east-1",
        "ADL_COGNITO_USER_POOL_ID": "unused-by-finalizer",
        "ADL_COGNITO_CLIENT_ID": "unused-by-finalizer",
        "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
        "ADL_APP_BASE_URL": "https://app.test.invalid",
        "ADL_API_BASE_URL": "https://api.test.invalid",
        "ADL_COOKIE_SCOPE": "host_only",
        "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
        "ADL_ATHENA_WORKGROUP": "unused-by-finalizer",
        "ADL_SEMANTIC_ATTEMPTS_TABLE": "unused-by-finalizer",
        "ADL_SEMANTIC_LEASES_TABLE": "unused-by-finalizer",
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": "unused-by-finalizer",
        "ADL_RAW_BUCKET": BUCKET,
        "ADL_REGISTRATION_STATUS_TABLE": "adlytics-test-registration-status",
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)


def _finalizer(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()
    return importlib.import_module("adapters.inbound.producer_handlers.finalizer")


def _registration():
    pack = pack_by_id(ROOT, "amc-dsp-impressions")
    registration = plan_landing(
        PlanLandingRequest(
            pack=pack,
            execution_id="integration_probe_finalizer_001",
            workflow_id="adlytics_amc_dsp_impressions_v1",
            schedule_kind=ScheduleKind.ADHOC,
            schedule_id=None,
            source_scope=SourceIdentifier("amc.entity", "fictional-finalizer-entity"),
            marketplace="US",
            period_start=date(2026, 8, 10),
            period_end=date(2026, 8, 16),
            time_window_start=datetime(2026, 8, 10, tzinfo=UTC),
            time_window_end=datetime(2026, 8, 17, tzinfo=UTC),
            source_watermark=date(2026, 8, 19),
            retrieved_at=datetime(2026, 8, 19, 6, tzinfo=UTC),
            object_version_id="incoming-result-version",
        )
    )
    return pack, registration


def _result() -> bytes:
    pack, registration = _registration()
    columns = list(pack.expected_columns)
    row = dict.fromkeys(columns, "1")
    row["time_window_start"] = str(registration.option_set["timeWindowStart"])
    row["time_window_end"] = str(registration.option_set["timeWindowEnd"])
    row["rows_suppressed"] = "false"
    row["suppression_reason"] = ""
    row["distinct_user_count"] = "2"
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode()


class _Statuses:
    def __init__(self) -> None:
        self.value: RegistrationStatus | None = None
        self.recorded: list[RegistrationStatus] = []

    def get(self, attempt: RegistrationAttempt) -> RegistrationStatus | None:
        del attempt
        return self.value

    def record(self, status: RegistrationStatus) -> RegistrationWriteOutcome:
        if self.value is None or self.value.state is RegistrationState.PROCESSING:
            self.value = status
            self.recorded.append(status)
            return RegistrationWriteOutcome(True, status)
        return RegistrationWriteOutcome(False, self.value, "already terminal")


class _Landing:
    def __init__(self, registration_key: str, registration_version: str) -> None:
        _pack, registration = _registration()
        self.objects: dict[tuple[str, str], bytes] = {
            (registration_key, registration_version): write_registration(registration).encode(),
            (registration.object.key, registration.object.version_id): _result(),
        }
        self.puts: list[tuple[str, bytes, str]] = []

    def size(self, key: str, version_id: str) -> int:
        return len(self.objects[(key, version_id)])

    def body(self, key: str, version_id: str) -> VersionedProducerObject:
        return VersionedProducerObject(key, version_id, self.objects[(key, version_id)])

    def put(self, key: str, body: bytes, content_type: str) -> StoredProducerObject:
        self.puts.append((key, body, content_type))
        version = f"created-version-{len(self.puts)}"
        self.objects[(key, version)] = body
        return StoredProducerObject(key, version)


def _event(key: str, version_id: str) -> dict[str, Any]:
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "detail": {
                            "bucket": {"name": BUCKET},
                            "object": {"key": key, "version-id": version_id},
                        }
                    }
                )
            }
        ]
    }


def test_event_parser_binds_exact_incoming_registration_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    _pack, registration = _registration()
    [message] = module.registration_messages(
        _event(registration.registration_key, "registration-version-001")
    )
    assert message.bucket == BUCKET
    assert message.key == registration.registration_key
    assert message.version_id == "registration-version-001"
    assert message.attempt.attempt_id.startswith("registration-")
    assert vars(module.handler)["__trace_boundaries__"] == (module.PRODUCER_REGISTRATION_FINALIZE,)


def test_event_parser_refuses_nonincoming_keys_and_missing_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    with pytest.raises(ValueError, match="incoming"):
        module.registration_messages(_event("amc/result/registration.json", "v1"))
    with pytest.raises(ValueError, match="VersionId"):
        module.registration_messages(_event("incoming/amc/x/e/registration.json", ""))


def test_finalizer_writes_unchanged_result_then_governed_manifest_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-001"
    landing = _Landing(registration.registration_key, version)
    statuses = _Statuses()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, statuses, module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)

    outcome = module.finalize_registration(runtime, message, NOW)

    assert outcome.state is RegistrationState.FINALIZED
    assert [status.state for status in statuses.recorded] == [
        RegistrationState.PROCESSING,
        RegistrationState.FINALIZED,
    ]
    assert len(landing.puts) == 2
    result_key, result_body, result_type = landing.puts[0]
    manifest_key, manifest_body, manifest_type = landing.puts[1]
    assert result_body == _result()
    assert result_type == "text/csv"
    assert result_key.endswith("/result.csv")
    assert manifest_key == result_key.replace("result.csv", "manifest.json")
    assert manifest_type == "application/json"
    manifest = json.loads(manifest_body)
    assert manifest["objects"][0]["version_id"] == "created-version-1"
    assert manifest["source_scope"] == {
        "namespace": "amc.entity",
        "value": "fictional-finalizer-entity",
    }
    assert "source_scope=" in manifest_key
    assert manifest["governance"]["query_pack_digest"] == pack.digest
    assert outcome.output is not None
    assert outcome.output.manifest_version_id == "created-version-2"


def _periodic_parameterized_registration() -> tuple[GovernedQueryPack, SourceRegistration]:
    """Build a synthetic weekly DSP pack filtered on one governed parameter (ADR-110)."""
    base, daily = _registration()
    sql = base.sql.replace(
        " GROUP BY", " WHERE campaign_country = CUSTOM_PARAMETER('dsp_campaign_country') GROUP BY"
    )
    pack = replace(
        base,
        sql=sql,
        query_digest=digest_bytes(sql.encode()),
        period_policy=PeriodPolicy((PeriodGrain.WEEK,)),
        input_parameters=(
            InputParameter(
                "dsp_campaign_country", ParameterDataType.STRING, "amc.campaign_country"
            ),
        ),
    )
    registration = replace(
        daily,
        option_set={**daily.option_set, "period_grain": "week", "dsp_campaign_country": "US"},
        query_pack=replace(daily.query_pack, query_digest=pack.query_digest),
    )
    return pack, registration


@pytest.mark.covers("ADR-110")
@pytest.mark.parametrize(
    ("changes", "outcome"),
    [
        ({}, "FINALIZED"),
        ({"dsp_campaign_country": "GB"}, "REGISTRATION_PARAMETER"),
        ({"period_grain": "month"}, "REGISTRATION_PERIOD_GRAIN"),
    ],
)
def test_finalizer_re_resolves_parameters_and_checks_the_period_grain(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str], outcome: str
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _periodic_parameterized_registration()
    registration = replace(registration, option_set={**registration.option_set, **changes})
    version = "registration-version-adr110"
    landing = _Landing(registration.registration_key, version)
    landing.objects[(registration.registration_key, version)] = write_registration(
        registration
    ).encode()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, _Statuses(), module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)

    if outcome == "FINALIZED":
        assert module.finalize_registration(runtime, message, NOW).state is (
            RegistrationState.FINALIZED
        )
        return
    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)
    assert raised.value.code == outcome
    assert landing.puts == []


def _snapshot_registration() -> tuple[GovernedQueryPack, SourceRegistration, bytes]:
    """Plan pack 10's September 2026 registration, as the producer does (design §5.5)."""
    pack = pack_by_id(ROOT, "amc-segment-buyer-profile-inmarket")
    period = (date(2026, 9, 1), date(2026, 9, 30))
    registration = plan_landing(
        PlanLandingRequest(
            pack=pack,
            execution_id="integration_probe_finalizer_010",
            workflow_id="adlytics_amc_segment_buyer_profile_inmarket_v1",
            schedule_kind=ScheduleKind.ADHOC,
            schedule_id=None,
            source_scope=SourceIdentifier("amc.entity", "fictional-finalizer-entity"),
            marketplace="US",
            period_start=period[0],
            period_end=period[1],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 10, 1, tzinfo=UTC),
            source_watermark=date(2026, 10, 3),
            retrieved_at=datetime(2026, 10, 3, 6, tzinfo=UTC),
            object_version_id="incoming-result-version",
            period_grain=PeriodGrain.MONTH,
            commerce_identities=resolve_commerce_identities(
                load_commerce_identity_bridge(ROOT),
                CommerceIdentityRequest(pack=pack, marketplace="US", period=period),
            ),
        )
    )
    columns = list(pack.expected_columns)
    row = dict.fromkeys(columns, "")
    row.update(
        {
            "row_kind": "profiled_total",
            "profiled_exposed_total": "2400",
            "profiled_buyers_total": "120",
            "marketplace_name": "AMAZON.COM",
            "time_window_start": str(registration.option_set["timeWindowStart"]),
            "time_window_end": str(registration.option_set["timeWindowEnd"]),
            "rows_suppressed": "false",
            "distinct_user_count": "2",
        }
    )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return pack, registration, stream.getvalue().encode()


@pytest.mark.covers("ADR-110")
@pytest.mark.parametrize(
    ("changes", "outcome"),
    [
        ({}, "FINALIZED"),
        ({"snapshot_start": "2026-09-01 00:00:00"}, "REGISTRATION_PARAMETER"),
        ({"snapshot_end": "2026-09-05 00:00:00"}, "REGISTRATION_PARAMETER"),
    ],
)
def test_finalizer_re_derives_the_asi_snapshot_range_from_the_period(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str], outcome: str
) -> None:
    """The planner derives the month's first-Thursday range; the Finalizer refuses any other."""
    module = _finalizer(monkeypatch)
    pack, registration, result = _snapshot_registration()
    assert (registration.option_set["snapshot_start"], registration.option_set["snapshot_end"]) == (
        "2026-09-02 00:00:00",
        "2026-09-04 00:00:00",
    )
    registration = replace(registration, option_set={**registration.option_set, **changes})
    version = "registration-version-snapshot"
    landing = _Landing(registration.registration_key, version)
    landing.objects[(registration.registration_key, version)] = write_registration(
        registration
    ).encode()
    landing.objects[(registration.object.key, registration.object.version_id)] = result
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, _Statuses(), module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)
    if outcome == "FINALIZED":
        assert module.finalize_registration(runtime, message, NOW).state is (
            RegistrationState.FINALIZED
        )
        return
    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)
    assert raised.value.code == outcome
    assert landing.puts == []


def test_terminal_replay_performs_no_s3_work(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-001"
    landing = _Landing(registration.registration_key, version)
    statuses = _Statuses()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, statuses, module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)
    first = module.finalize_registration(runtime, message, NOW)
    landing.puts.clear()

    assert module.finalize_registration(runtime, message, NOW) == first
    assert landing.puts == []


def test_invalid_result_is_terminal_content_not_a_dependency_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-001"
    landing = _Landing(registration.registration_key, version)
    landing.objects[(registration.object.key, registration.object.version_id)] = b"wrong\n1\n"
    statuses = _Statuses()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, statuses, module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)

    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)

    assert raised.value.code == "RESULT_COLUMNS_MISSING"
    assert statuses.value is not None
    assert statuses.value.state is RegistrationState.PROCESSING
    assert landing.puts == []


def test_rejection_record_is_terminal_and_emits_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    _pack, registration = _registration()
    message = module.FinalizerMessage(
        BUCKET,
        registration.registration_key,
        "registration-version-001",
    )
    statuses = _Statuses()
    statuses.record(RegistrationStatus.processing(message.attempt, at=NOW))
    runtime = module.FinalizerRuntime(
        BUCKET,
        {},
        cast(Any, object()),
        statuses,
        module.COMMERCE_IDENTITIES,
    )
    reported: list[tuple[Any, Report]] = []
    actions: list[str] = []

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    def capture_action(action: str, selected: object) -> None:
        del selected
        actions.append(action)

    monkeypatch.setattr(module, "report_error", capture_error)
    monkeypatch.setattr(module, "_report", capture_action)

    module._reject(
        module._RejectRequest(
            runtime=runtime,
            message=message,
            error=module.RegistrationContentError("RESULT_INVALID", "wrong header"),
            now=NOW,
            report=Report(module.OPERATION, message.attempt.attempt_id, module.LOGGER),
        )
    )

    assert statuses.value is not None
    assert statuses.value.state is RegistrationState.REJECTED
    assert statuses.value.rejection_code == "RESULT_INVALID"
    assert len(reported) == 1
    error, report = reported[0]
    assert error.error_class is ErrorClass.VALIDATION
    assert error.alarms is False
    assert report.operation == module.OPERATION
    assert actions == ["rejected"]


def test_handler_reports_rejection_persistence_dependency_once_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    _pack, registration = _registration()
    version = "registration-version-001"
    landing = _Landing(registration.registration_key, version)
    statuses = _Statuses()
    original_record = statuses.record
    failure = DependencyError(
        module.PRODUCER_STATE_CONFLICT,
        "registration status persistence failed",
        context={"cause": "fixture"},
    )

    def fail_rejection(status: RegistrationStatus) -> RegistrationWriteOutcome:
        if status.state is RegistrationState.REJECTED:
            raise failure
        return original_record(status)

    monkeypatch.setattr(statuses, "record", fail_rejection)
    monkeypatch.setattr(
        module,
        "RUNTIME",
        module.FinalizerRuntime(BUCKET, {}, landing, statuses, module.COMMERCE_IDENTITIES),
    )
    reported: list[tuple[Any, Report]] = []

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(module, "report_error", capture_error)

    with pytest.raises(DependencyError) as raised:
        inspect.unwrap(module.handler)(
            _event(registration.registration_key, version),
            object(),
        )

    assert raised.value is failure
    assert failure.error_class is ErrorClass.DEPENDENCY
    assert failure.context == {"cause": "fixture"}
    assert len(reported) == 1
    assert reported[0][0] is failure
    assert reported[0][1].operation == module.OPERATION


def test_finalizer_error_operation_is_in_the_ingestion_alarm_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    alarm_source = (ROOT / "infra/lib/stacks/ingestion-stage1.ts").read_text()
    assert f"  '{module.OPERATION}'," in alarm_source


def test_missing_amc_watermark_rejects_before_any_canonical_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    invalid = replace(registration, source_watermark=None)
    version = "registration-version-no-watermark"
    landing = _Landing(registration.registration_key, version)
    landing.objects[(registration.registration_key, version)] = write_registration(invalid).encode()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, _Statuses(), module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)

    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)

    assert raised.value.code == "REGISTRATION_WATERMARK_REQUIRED"
    assert landing.puts == []


def test_oversize_result_rejects_from_exact_head_before_body_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-oversize"
    landing = _Landing(registration.registration_key, version)
    statuses = _Statuses()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, statuses, module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)
    original_size = landing.size

    def oversize(key: str, version_id: str) -> int:
        if key == registration.object.key:
            return pack.max_result_bytes + 1
        return original_size(key, version_id)

    monkeypatch.setattr(landing, "size", oversize)

    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)

    assert raised.value.code == "RESULT_TOO_LARGE"
    assert landing.puts == []


def test_missing_source_scope_rejects_before_any_canonical_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-no-source-scope"
    landing = _Landing(registration.registration_key, version)
    document = json.loads(write_registration(registration))
    document.pop("source_scope")
    landing.objects[(registration.registration_key, version)] = json.dumps(document).encode()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, _Statuses(), module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)

    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)

    assert raised.value.code == "REGISTRATION_INVALID"
    assert landing.puts == []


def test_handler_root_span_encloses_empty_batch_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span
    reported: list[tuple[Any, Report]] = []
    monkeypatch.setattr(module, "span", traced)

    def capture_error(error: BaseException, report: Report) -> None:
        reported.append((error, report))

    monkeypatch.setattr(module, "report_error", capture_error)

    assert inspect.unwrap(module.handler)({"Records": []}, object()) is None

    traced.assert_called_once_with(
        module.PRODUCER_REGISTRATION_FINALIZE,
        operation=module.OPERATION,
    )
    assert [item.kwargs for item in process_span.record.call_args_list] == [
        {"record_count": 0},
        {"outcome": "completed"},
    ]
    assert reported == []


def test_handler_reports_malformed_batch_at_the_boundary_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    error_reports: list[BaseException] = []

    def capture_error(error: BaseException, _report: object) -> None:
        error_reports.append(error)

    monkeypatch.setattr(module, "report_error", capture_error)

    with pytest.raises(ValueError, match="Records list") as raised:
        inspect.unwrap(module.handler)({"Records": "invalid"}, object())

    assert error_reports == [raised.value]
