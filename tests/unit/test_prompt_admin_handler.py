# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Prompt-registry handlers enforce authorizer-owned operator identity and contracts."""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from adapters.config import settings_without_client_secret, tracing_settings
from adapters.outbound.dynamodb.insight_prompt_registry import PROMPT_REGISTRY_MALFORMED
from domain.identity.operator_directory import OperatorEmailResolution, OperatorEmailStatus
from domain.presentation.insights import (
    PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
    InsightCalibrationCaseResult,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightCalibrationResult,
    InsightModelRoute,
    InsightPendingPromptVersion,
    InsightPromptActivationRequest,
    InsightPromptCalibrationRequest,
    InsightPromptCalibrationWorkState,
    InsightPromptRetirementRequest,
    InsightPromptSlotState,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightPromptVersionRecord,
    InsightSlotId,
)
from domain.presentation.prompt_authorship import (
    OperatorPromptAuthor,
    PromptVersionAuthor,
    SystemPromptAuthor,
)
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]

_ENV = {
    "ADL_ENVIRONMENT": "test",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "example-client",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.example.test",
    "ADL_APP_BASE_URL": "https://app.example.test",
    "ADL_API_BASE_URL": "https://api.example.test",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "3600",
    "ADL_ATHENA_WORKGROUP": "interactive",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "publications",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "2",
    "ADL_INSIGHT_PROMPT_REGISTRY_TABLE": "prompt-registry",
    "ADL_INSIGHT_PROMPT_CALIBRATION_QUEUE_URL": "https://sqs.example.test/queue",
}


@dataclass
class Registry:
    create_requests: list[InsightPromptVersionCreateRequest] = field(
        default_factory=list[InsightPromptVersionCreateRequest]
    )
    calibration_requests: list[InsightPromptCalibrationRequest] = field(
        default_factory=list[InsightPromptCalibrationRequest]
    )
    pending_reads: list[tuple[InsightSlotId, int]] = field(
        default_factory=list[tuple[InsightSlotId, int]]
    )
    pending: InsightPendingPromptVersion = field(
        default_factory=lambda: InsightPendingPromptVersion(
            InsightSlotId.CONTRIBUTION, 3, "Stored prompt.", InsightModelRoute.DEEP
        )
    )
    activation_requests: list[InsightPromptActivationRequest] = field(
        default_factory=list[InsightPromptActivationRequest]
    )
    retirement_requests: list[InsightPromptRetirementRequest] = field(
        default_factory=list[InsightPromptRetirementRequest]
    )
    states: tuple[InsightPromptSlotState, ...] = ()
    version_records: tuple[InsightPromptVersionRecord, ...] = ()
    slot_state_reads: list[tuple[InsightSlotId, ...]] = field(
        default_factory=list[tuple[InsightSlotId, ...]]
    )
    version_reads: list[InsightSlotId] = field(default_factory=list[InsightSlotId])

    work_state: InsightPromptCalibrationWorkState = field(
        default_factory=lambda: InsightPromptCalibrationWorkState("pending", False)
    )
    state_reads: list[tuple[InsightSlotId, int, int]] = field(
        default_factory=list[tuple[InsightSlotId, int, int]]
    )
    outbox_acknowledgements: list[tuple[InsightSlotId, int]] = field(
        default_factory=list[tuple[InsightSlotId, int]]
    )

    def acknowledge_calibration_outbox(self, slot_id: InsightSlotId, version: int) -> None:
        self.outbox_acknowledgements.append((slot_id, version))

    def calibration_work_state(
        self, slot_id: InsightSlotId, version: int, now_epoch_seconds: int
    ) -> InsightPromptCalibrationWorkState:
        self.state_reads.append((slot_id, version, now_epoch_seconds))
        return self.work_state

    def create_version(self, request: InsightPromptVersionCreateRequest) -> int:
        self.create_requests.append(request)
        return 3

    def pending_version(self, slot_id: InsightSlotId, version: int) -> InsightPendingPromptVersion:
        self.pending_reads.append((slot_id, version))
        return self.pending

    def record_calibration(self, request: InsightPromptCalibrationRequest) -> None:
        self.calibration_requests.append(request)

    def activate(self, request: InsightPromptActivationRequest) -> None:
        self.activation_requests.append(request)

    def retire(self, request: InsightPromptRetirementRequest) -> None:
        self.retirement_requests.append(request)

    def slot_states(
        self, slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptSlotState, ...]:
        self.slot_state_reads.append(slot_ids)
        if self.states:
            return self.states
        return tuple(InsightPromptSlotState(slot_id, None, None, None) for slot_id in slot_ids)

    def versions(self, slot_id: InsightSlotId) -> tuple[InsightPromptVersionRecord, ...]:
        self.version_reads.append(slot_id)
        return self.version_records


@dataclass
class Queue:
    enqueued: list[tuple[InsightSlotId, int]] = field(
        default_factory=list[tuple[InsightSlotId, int]]
    )
    error: Exception | None = None

    def enqueue(self, slot_id: InsightSlotId, version: int) -> str:
        if self.error is not None:
            raise self.error
        self.enqueued.append((slot_id, version))
        return "message-fictional"


@dataclass
class Runner:
    requests: list[object] = field(default_factory=list[object])
    result: InsightCalibrationResult = field(
        default_factory=lambda: InsightCalibrationResult(
            tuple(
                InsightCalibrationCaseResult(kind, InsightCalibrationOutcome.PASSED)
                for kind in InsightCalibrationKind
            )
        )
    )

    def run(self, request: object) -> InsightCalibrationResult:
        self.requests.append(request)
        return self.result


@pytest.fixture
def prompt_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()
    sys.modules.pop("adapters.inbound.prompt_admin.handler", None)
    module = importlib.import_module("adapters.inbound.prompt_admin.handler")
    yield module
    sys.modules.pop("adapters.inbound.prompt_admin.handler", None)
    settings_without_client_secret.cache_clear()
    tracing_settings.cache_clear()


def _event(body: dict[str, object], can_manage_prompts: bool | None = True) -> dict[str, object]:
    context: dict[str, object] = {"subject": "operator-from-context"}
    if can_manage_prompts is not None:
        context["can_manage_prompts"] = str(can_manage_prompts).lower()
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"lambda": context}}}


def _call(
    module: ModuleType,
    route: str,
    body: dict[str, object],
    can_manage_prompts: bool | None = True,
) -> dict[str, Any]:
    handler_module = cast("Any", module)
    handler_module.ROUTE = route
    return cast(
        "dict[str, Any]",
        inspect.unwrap(handler_module.handler)(_event(body, can_manage_prompts), object()),
    )


def _body(response: dict[str, Any]) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(cast("str", response["body"])))


def test_slots_returns_only_the_authorized_roster_projection(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(prompt_module, "slots", {})

    assert response["statusCode"] == 200
    assert _body(response)["slots"]


def test_slots_serve_each_contracts_readable_name_beside_its_id(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prompt_module, "STORE", Registry())

    slots = cast("list[dict[str, object]]", _body(_call(prompt_module, "slots", {}))["slots"])
    names = {slot["slot_id"]: slot["display_name"] for slot in slots}

    assert names["movers"] == "Largest changes (Campaigns)"
    assert names["retail-movers"] == "Largest movers (Retail health)"
    assert all(isinstance(name, str) and name.strip() for name in names.values())
    assert len(set(names.values())) == len(names)


_CASES = tuple(
    InsightCalibrationCaseResult(kind, InsightCalibrationOutcome.PASSED)
    for kind in InsightCalibrationKind
)
_RESOLVED_SUBJECT = "fictional-subject-resolved"
_GONE_SUBJECT = "fictional-subject-deleted"
_UNLOADED_SUBJECT = "fictional-subject-unloaded"
_EMAIL = "operator@example.com"
_AT = datetime(2026, 10, 1, 15, 25, tzinfo=UTC)


def _record(
    version: int, author: PromptVersionAuthor | None, created_at: datetime | None = _AT
) -> InsightPromptVersionRecord:
    return InsightPromptVersionRecord(
        InsightSlotId.CONTRIBUTION,
        version,
        "Fictional prompt.",
        InsightModelRoute.DEEP,
        author,
        created_at,
        "passed",
        _CASES,
    )


@dataclass
class Directory:
    """A plain `OperatorDirectoryPort` returning scripted resolutions."""

    resolutions: dict[str, OperatorEmailResolution] = field(
        default_factory=dict[str, OperatorEmailResolution]
    )
    calls: list[frozenset[str]] = field(default_factory=list[frozenset[str]])

    def email_addresses(self, subjects: frozenset[str]) -> dict[str, OperatorEmailResolution]:
        self.calls.append(subjects)
        return {
            subject: self.resolutions[subject]
            for subject in subjects
            if subject in self.resolutions
        }


@dataclass
class Telemetry:
    """Every operation and error record the handler writes."""

    operations: list[object] = field(default_factory=list[object])
    errors: list[BaseException] = field(default_factory=list[BaseException])

    def operation(self, record: object, _logger: object) -> None:
        self.operations.append(record)

    def error(self, error: BaseException, _report: object) -> None:
        self.errors.append(error)


def _telemetry(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    telemetry = Telemetry()
    monkeypatch.setattr(module, "report_operation", telemetry.operation)
    monkeypatch.setattr(module, "report_error", telemetry.error)
    return telemetry


def _five_kinds() -> Registry:
    return Registry(
        states=(InsightPromptSlotState(InsightSlotId.CONTRIBUTION, 2, 5, "passed"),),
        version_records=(
            _record(5, OperatorPromptAuthor(_RESOLVED_SUBJECT)),
            _record(4, OperatorPromptAuthor(_GONE_SUBJECT)),
            _record(3, OperatorPromptAuthor(_UNLOADED_SUBJECT)),
            _record(2, SystemPromptAuthor.CONTRACT_SEED),
            _record(1, None, None),
        ),
    )


def _directory() -> Directory:
    return Directory(
        {
            _RESOLVED_SUBJECT: OperatorEmailResolution(OperatorEmailStatus.RESOLVED, _EMAIL),
            _GONE_SUBJECT: OperatorEmailResolution(OperatorEmailStatus.NOT_FOUND),
            _UNLOADED_SUBJECT: OperatorEmailResolution(OperatorEmailStatus.UNAVAILABLE),
        }
    )


def test_versions_attribute_every_author_kind_and_never_return_the_subject(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory()
    telemetry = _telemetry(prompt_module, monkeypatch)
    monkeypatch.setattr(prompt_module, "STORE", _five_kinds())
    monkeypatch.setattr(prompt_module, "DIRECTORY", directory)

    response = _call(prompt_module, "versions", {"slot_id": "contribution"})

    assert response["statusCode"] == 200
    body = _body(response)
    versions = cast("list[dict[str, object]]", body["versions"])
    assert [(item["created_by"], item["created_at"]) for item in versions] == [
        ({"kind": "operator_email", "email": _EMAIL}, "2026-10-01T15:25:00+00:00"),
        ({"kind": "operator_not_found"}, "2026-10-01T15:25:00+00:00"),
        ({"kind": "operator_unavailable"}, "2026-10-01T15:25:00+00:00"),
        ({"kind": "system", "name": "contract-seed"}, "2026-10-01T15:25:00+00:00"),
        ({"kind": "not_recorded"}, None),
    ]
    assert directory.calls == [frozenset({_RESOLVED_SUBJECT, _GONE_SUBJECT, _UNLOADED_SUBJECT})]
    assert versions[0] == {
        "slot_id": "contribution",
        "version": 5,
        "prompt": "Fictional prompt.",
        "model_route": "deep",
        "tier": "card",
        "unsourced": True,
        "created_by": {"kind": "operator_email", "email": _EMAIL},
        "created_at": "2026-10-01T15:25:00+00:00",
        "calibration": "passed",
        "calibration_cases": [
            {"kind": "known-correct", "outcome": "passed"},
            {"kind": "contradiction", "outcome": "passed"},
            {"kind": "fluent-unsupported", "outcome": "passed"},
            {"kind": "exploratory", "outcome": "passed"},
            {"kind": "derived-figure", "outcome": "passed"},
            {"kind": "miscomputed-derivation", "outcome": "passed"},
            {"kind": "incompatible-basis", "outcome": "passed"},
        ],
    }
    for subject in (_RESOLVED_SUBJECT, _GONE_SUBJECT, _UNLOADED_SUBJECT):
        assert subject not in response["body"]
    terminal = cast("Any", telemetry.operations[-1])
    assert terminal.details == {
        "status": 200,
        "slot_id": "contribution",
        "versions": 5,
        "authors_unavailable": 1,
    }
    assert telemetry.errors == []


def test_versions_with_only_legacy_and_seeded_records_never_call_the_directory(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory()
    registry = Registry(
        version_records=(_record(2, SystemPromptAuthor.CONTRACT_SEED), _record(1, None, None))
    )
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "DIRECTORY", directory)

    response = _call(prompt_module, "versions", {"slot_id": "contribution"})

    assert response["statusCode"] == 200
    assert directory.calls == []


def test_a_degraded_directory_still_returns_the_versions(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _telemetry(prompt_module, monkeypatch)
    monkeypatch.setattr(prompt_module, "STORE", _five_kinds())
    monkeypatch.setattr(prompt_module, "DIRECTORY", Directory())

    response = _call(prompt_module, "versions", {"slot_id": "contribution"})

    assert response["statusCode"] == 200
    versions = cast("list[dict[str, object]]", _body(response)["versions"])
    assert [item["created_by"] for item in versions[:3]] == [{"kind": "operator_unavailable"}] * 3


@dataclass
class MalformedRegistry(Registry):
    def versions(self, slot_id: InsightSlotId) -> tuple[InsightPromptVersionRecord, ...]:
        self.version_reads.append(slot_id)
        raise InvariantError(PROMPT_REGISTRY_MALFORMED, "Prompt version record is malformed.")


def test_a_malformed_version_record_is_unavailable_and_alarms_never_a_conflict(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    telemetry = _telemetry(prompt_module, monkeypatch)
    monkeypatch.setattr(prompt_module, "STORE", MalformedRegistry())
    monkeypatch.setattr(prompt_module, "DIRECTORY", _directory())

    response = _call(prompt_module, "versions", {"slot_id": "contribution"})

    assert response["statusCode"] == 503
    assert _body(response) == {"error": "unavailable"}
    [error] = telemetry.errors
    assert isinstance(error, InvariantError)
    assert error.code == PROMPT_REGISTRY_MALFORMED


def test_an_unconfigured_directory_is_unavailable_before_the_registry_is_read(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    telemetry = _telemetry(prompt_module, monkeypatch)
    registry = _five_kinds()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "DIRECTORY", None)

    response = _call(prompt_module, "versions", {"slot_id": "contribution"})

    assert response["statusCode"] == 503
    assert _body(response) == {"error": "unavailable"}
    assert len(telemetry.errors) == 1
    assert registry.version_reads == []


def test_versions_telemetry_names_neither_the_email_nor_the_subject(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    telemetry = _telemetry(prompt_module, monkeypatch)
    monkeypatch.setattr(prompt_module, "STORE", _five_kinds())
    monkeypatch.setattr(prompt_module, "DIRECTORY", _directory())

    _call(prompt_module, "versions", {"slot_id": "contribution"})

    recorded = repr((telemetry.operations, telemetry.errors))
    assert telemetry.operations
    for secret in (_EMAIL, _RESOLVED_SUBJECT, _GONE_SUBJECT, _UNLOADED_SUBJECT):
        assert secret not in recorded


def test_create_version_durably_queues_identity_without_echoing_or_logging_prompt(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    queue = Queue()
    operations: list[object] = []
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def record_operation(record: object, _logger: object) -> None:
        operations.append(record)

    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", queue)
    monkeypatch.setattr(prompt_module, "report_operation", record_operation)
    monkeypatch.setattr(prompt_module, "span", traced)
    raw_prompt = "Do not retain this prompt in telemetry."

    response = _call(
        prompt_module,
        "create-version",
        {"slot_id": "contribution", "prompt_template": raw_prompt},
    )

    assert response["statusCode"] == 202
    assert vars(cast("Any", prompt_module).handler)["__trace_boundaries__"] == (
        cast("Any", prompt_module).PROMPT_ADMIN_REQUEST,
    )
    process_span.record.assert_called_once_with(outcome="completed", status=202)
    assert _body(response) == {
        "outcome": "pending",
        "slot_id": "contribution",
        "version": 3,
    }
    assert registry.create_requests[0].prompt == raw_prompt
    assert registry.create_requests[0].model_route is InsightModelRoute.QUICK
    # The author is the authorizer's subject, never a body field; the time is aware.
    assert registry.create_requests[0].author == OperatorPromptAuthor("operator-from-context")
    assert registry.create_requests[0].created_at.utcoffset() == timedelta(0)
    assert queue.enqueued == [(InsightSlotId.CONTRIBUTION, 3)]
    assert registry.outbox_acknowledgements == [(InsightSlotId.CONTRIBUTION, 3)]
    assert registry.calibration_requests == []
    assert all(raw_prompt not in str(operation) for operation in operations)


def test_create_version_accepts_the_optional_routing_hint(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", Queue())

    response = _call(
        prompt_module,
        "create-version",
        {
            "slot_id": "contribution",
            "prompt_template": "Fictional prompt.",
            "model_routing_hint": "deep",
        },
    )

    assert response["statusCode"] == 202
    assert registry.create_requests[0].model_route is InsightModelRoute.DEEP
    # Absent tier and digests: the direct operator loop stays a card-tier unsourced create.
    assert registry.create_requests[0].tier is InsightPromptTier.CARD
    assert registry.create_requests[0].fragment_digests is None


@pytest.mark.covers("ADR-076")
def test_create_version_accepts_a_composed_deep_version_with_fragment_digests(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", Queue())

    response = _call(
        prompt_module,
        "create-version",
        {
            "slot_id": "contribution",
            "prompt_template": "A fictional composed prompt.",
            "model_routing_hint": "deep",
            "tier": "deep",
            "fragment_digests": {
                "foundation": "a" * 64,
                "tier_core": "b" * 64,
                "slot_block": "c" * 64,
            },
        },
    )

    assert response["statusCode"] == 202
    request = registry.create_requests[0]
    assert request.tier is InsightPromptTier.DEEP
    assert request.fragment_digests is not None
    assert request.fragment_digests.foundation == "a" * 64


@pytest.mark.parametrize(
    "body",
    (
        {"slot_id": "contribution", "prompt": "legacy", "model_route": "quick"},
        {"slot_id": "contribution", "prompt_template": "valid", "operator": "attacker"},
        {"slot_id": "contribution", "prompt_template": "valid", "model_routing_hint": "unknown"},
        {"slot_id": "contribution", "prompt_template": "valid", "tier": "unknown"},
        {
            "slot_id": "contribution",
            "prompt_template": "valid",
            "fragment_digests": {"foundation": "not-hex"},
        },
    ),
)
def test_create_version_rejects_legacy_identity_and_unknown_fields(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(prompt_module, "create-version", body)

    assert response["statusCode"] == 400
    assert _body(response) == {"error": "invalid_request"}
    assert registry.create_requests == []


def test_calibration_extension_requeues_only_pending_work_without_an_active_lease(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    queue = Queue()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", queue)

    response = _call(prompt_module, "calibrate", {"slot_id": "contribution", "version": 3})

    assert response["statusCode"] == 202
    assert _body(response) == {
        "outcome": "pending",
        "slot_id": "contribution",
        "version": 3,
    }
    assert queue.enqueued == [(InsightSlotId.CONTRIBUTION, 3)]
    assert registry.outbox_acknowledgements == [(InsightSlotId.CONTRIBUTION, 3)]
    assert registry.state_reads[0][:2] == (InsightSlotId.CONTRIBUTION, 3)


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (InsightPromptCalibrationWorkState("pending", True), 202),
        (InsightPromptCalibrationWorkState("passed", False), 200),
        (InsightPromptCalibrationWorkState("failed", False), 200),
    ],
)
def test_calibration_extension_does_not_duplicate_active_or_terminal_work(
    prompt_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    state: InsightPromptCalibrationWorkState,
    status: int,
) -> None:
    registry = Registry(work_state=state)
    queue = Queue()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", queue)

    response = _call(prompt_module, "calibrate", {"slot_id": "contribution", "version": 3})

    assert response["statusCode"] == status
    assert _body(response)["outcome"] == state.calibration
    assert queue.enqueued == []


def test_activate_uses_the_authorizer_subject_and_returns_the_committed_event_projection(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(
        prompt_module,
        "activate",
        {"slot_id": "contribution", "version": 2, "expected_active_version": 3},
    )

    assert response["statusCode"] == 200
    request = registry.activation_requests[0]
    assert request.operator == "operator-from-context"
    assert _body(response)["event"] == {
        "slot_id": "contribution",
        "from_version": 3,
        "to_version": 2,
        "operator": "operator-from-context",
        "at": request.at.isoformat(),
    }


def test_retire_uses_the_authorizer_subject_and_returns_the_committed_event_projection(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(
        prompt_module,
        "retire",
        {"slot_id": "contribution", "expected_active_version": 3},
    )

    assert response["statusCode"] == 200
    request = registry.retirement_requests[0]
    assert request.operator == "operator-from-context"
    # Absent tier keeps the retained card-tier semantics.
    assert request.tier is InsightPromptTier.CARD
    assert _body(response)["event"] == {
        "slot_id": "contribution",
        "from_version": 3,
        "to_version": None,
        "operator": "operator-from-context",
        "at": request.at.isoformat(),
    }


@pytest.mark.covers("ADR-076")
def test_retire_forwards_the_named_tier_to_the_registry(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the tier passthrough would silently retire the wrong tier's pointer."""
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(
        prompt_module,
        "retire",
        {"slot_id": "contribution", "expected_active_version": 3, "tier": "deep"},
    )

    assert response["statusCode"] == 200
    assert registry.retirement_requests[0].tier is InsightPromptTier.DEEP


@pytest.mark.parametrize(
    ("route", "body"),
    (
        ("slots", {}),
        ("versions", {"slot_id": "contribution"}),
        ("create-version", {"slot_id": "contribution", "prompt_template": "Fictional prompt."}),
        ("calibrate", {"slot_id": "contribution", "version": 3}),
        ("activate", {"slot_id": "contribution", "version": 2, "expected_active_version": 3}),
        ("retire", {"slot_id": "contribution", "expected_active_version": 3}),
    ),
)
@pytest.mark.parametrize("can_manage_prompts", [False, None])
def test_non_entitled_or_unprojected_callers_receive_the_unknown_route_denial_without_side_effects(
    prompt_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, object],
    can_manage_prompts: bool | None,
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    denial = _call(prompt_module, route, body, can_manage_prompts)
    unknown_route = _call(prompt_module, "unknown", body)

    assert denial["statusCode"] == unknown_route["statusCode"] == 400
    assert _body(denial) == _body(unknown_route) == {"error": "invalid_request"}
    assert registry.create_requests == []
    assert registry.calibration_requests == []
    assert registry.pending_reads == []
    assert registry.slot_state_reads == []
    assert registry.version_reads == []
    assert registry.activation_requests == []
    assert registry.retirement_requests == []


@pytest.mark.parametrize(
    "body",
    (
        {"slot_id": "contribution", "version": 3, "cases": []},
        {"slot_id": "contribution", "version": 3, "outcome": "passed"},
        {"slot_id": "contribution", "version": 3, "prompt_template": "caller prompt"},
    ),
)
def test_calibration_extension_rejects_caller_supplied_calibration_content(
    prompt_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)

    response = _call(prompt_module, "calibrate", body)

    assert response["statusCode"] == 400
    assert _body(response) == {"error": "invalid_request"}
    assert registry.calibration_requests == []


def test_activate_returns_the_typed_calibration_refusal_without_an_internal_reason(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    error = InvariantError(
        PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
        "Prompt activation requires a passed calibration result.",
    )
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span

    def refuse(_: InsightPromptActivationRequest) -> None:
        raise error

    monkeypatch.setattr(registry, "activate", refuse)
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "span", traced)

    response = _call(
        prompt_module,
        "activate",
        {"slot_id": "contribution", "version": 2, "expected_active_version": 3},
    )

    assert response["statusCode"] == 409
    assert _body(response) == {"error": "calibration_required"}
    process_span.note_error.assert_called_once_with(error)
    process_span.record.assert_called_once_with(outcome="refused", status=409)


def test_create_returns_the_durable_identity_when_immediate_queue_send_fails(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    queue = Queue(error=RuntimeError("fictional queue unavailable"))
    reported: list[BaseException] = []

    def capture(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", queue)
    monkeypatch.setattr(prompt_module, "report_error", capture)

    response = _call(
        prompt_module,
        "create-version",
        {"slot_id": "contribution", "prompt_template": "A durable fictional prompt."},
    )

    assert response["statusCode"] == 202
    assert _body(response) == {
        "outcome": "pending",
        "slot_id": "contribution",
        "version": 3,
    }
    assert registry.outbox_acknowledgements == []
    assert [type(error).__name__ for error in reported] == ["RuntimeError"]


# ADR-078 §1 path-scoped body-size exclusion: the handler's own 32,768-byte ceiling, as defence in
# depth behind the edge rule. Every refusal asserts the whole refusal signal; each is paired with a
# positive control at the ceiling that proves the handler went on to `_body`.
_CEILING = 32_768


def _sized(fields: dict[str, object], pad_field: str, size: int, unit: str = "x") -> str:
    """Serialize `fields` as raw UTF-8 JSON padded in `pad_field` to exactly `size` bytes."""
    base = len(json.dumps({**fields, pad_field: ""}, ensure_ascii=False).encode("utf-8"))
    unit_bytes = len(unit.encode("utf-8"))
    units, remainder = divmod(size - base, unit_bytes)
    text = unit * units + "x" * remainder
    raw = json.dumps({**fields, pad_field: text}, ensure_ascii=False)
    assert len(raw.encode("utf-8")) == size
    return raw


def _raw_call(
    module: ModuleType, route: str, raw: str, *, base64_encoded: bool = False
) -> dict[str, Any]:
    handler_module = cast("Any", module)
    handler_module.ROUTE = route
    event = _event({}) | {"body": raw}
    if base64_encoded:
        event["isBase64Encoded"] = True
    return cast("dict[str, Any]", inspect.unwrap(handler_module.handler)(event, object()))


@dataclass
class Ceiling:
    """The handler's collaborators for a ceiling test: spans, telemetry and inert dependencies."""

    process_span: MagicMock
    telemetry: Telemetry
    store: MagicMock
    queue: MagicMock
    directory: MagicMock


def _ceiling(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Ceiling:
    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span
    collaborators = Ceiling(
        process_span, _telemetry(module, monkeypatch), MagicMock(), MagicMock(), MagicMock()
    )
    monkeypatch.setattr(module, "span", traced)
    monkeypatch.setattr(module, "STORE", collaborators.store)
    monkeypatch.setattr(module, "QUEUE", collaborators.queue)
    monkeypatch.setattr(module, "DIRECTORY", collaborators.directory)
    return collaborators


def _assert_refused_for_size(
    response: dict[str, Any], collaborators: Ceiling, body_bytes: int, prompt_text: str
) -> None:
    assert response["statusCode"] == 413
    assert response["body"] == '{"error":"request_too_large"}'
    collaborators.process_span.record.assert_called_once_with(outcome="refused", status=413)
    terminal = [
        cast("Any", record)
        for record in collaborators.telemetry.operations
        if cast("Any", record).phase == "terminal"
    ]
    assert len(terminal) == 1
    assert terminal[0].outcome == "refused"
    assert terminal[0].component == "prompt_admin_api"
    assert terminal[0].severity.name == "WARNING"
    assert terminal[0].details == {
        "status": 413,
        "body_bytes": body_bytes,
        "max_body_bytes": _CEILING,
    }
    assert prompt_text not in str(collaborators.telemetry.operations)
    assert collaborators.telemetry.errors == []
    assert collaborators.store.method_calls == []
    assert collaborators.queue.method_calls == []
    assert collaborators.directory.method_calls == []


def test_the_ceiling_matches_the_edge_constant(prompt_module: ModuleType) -> None:
    assert cast("Any", prompt_module).MAX_BODY_BYTES == _CEILING


def test_a_create_version_body_at_the_ceiling_is_accepted(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", Queue())
    raw = _sized({"slot_id": "contribution"}, "prompt_template", _CEILING)

    response = _raw_call(prompt_module, "create-version", raw)

    assert response["statusCode"] == 202
    assert len(registry.create_requests) == 1


def test_a_create_version_body_one_byte_over_the_ceiling_is_refused(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    raw = _sized({"slot_id": "contribution"}, "prompt_template", _CEILING + 1)

    response = _raw_call(prompt_module, "create-version", raw)

    _assert_refused_for_size(response, collaborators, _CEILING + 1, "x" * 1000)


def test_a_base64_body_is_measured_decoded_and_refused_over_the_ceiling(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    raw = _sized({"slot_id": "contribution"}, "prompt_template", _CEILING + 1)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")

    response = _raw_call(prompt_module, "create-version", encoded, base64_encoded=True)

    _assert_refused_for_size(response, collaborators, _CEILING + 1, "x" * 1000)


def test_a_base64_body_at_the_ceiling_passes_it_and_reaches_the_existing_parse_refusal(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    raw = _sized({"slot_id": "contribution"}, "prompt_template", _CEILING)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    assert len(encoded) > _CEILING

    response = _raw_call(prompt_module, "create-version", encoded, base64_encoded=True)

    # The encoded text is over the ceiling but its decoded bytes are not, so the handler reaches
    # `_body`, which does not decode base64 and refuses it as today.
    assert response["statusCode"] == 400
    assert _body(response) == {"error": "invalid_request"}
    collaborators.process_span.record.assert_called_once_with(outcome="refused", status=400)
    assert collaborators.store.method_calls == []


def test_an_invalid_base64_body_is_the_existing_invalid_request(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)

    response = _raw_call(prompt_module, "create-version", "not base64!", base64_encoded=True)

    assert response["statusCode"] == 400
    assert _body(response) == {"error": "invalid_request"}
    collaborators.process_span.record.assert_called_once_with(outcome="refused", status=400)


def test_a_multibyte_body_is_measured_in_utf8_bytes_and_refused(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    prompt = "é" * 16_385
    raw = json.dumps({"slot_id": "contribution", "prompt_template": prompt}, ensure_ascii=False)
    size = len(raw.encode("utf-8"))
    # Under the ceiling in characters, over it in bytes.
    assert len(raw) < _CEILING < size

    response = _raw_call(prompt_module, "create-version", raw)

    _assert_refused_for_size(response, collaborators, size, prompt)


def test_a_multibyte_create_version_body_at_the_ceiling_is_accepted(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    monkeypatch.setattr(prompt_module, "STORE", registry)
    monkeypatch.setattr(prompt_module, "QUEUE", Queue())
    raw = _sized({"slot_id": "contribution"}, "prompt_template", _CEILING, unit="é")
    assert "é" in raw

    response = _raw_call(prompt_module, "create-version", raw)

    assert response["statusCode"] == 202
    assert len(registry.create_requests) == 1


def test_an_activate_body_over_the_ceiling_is_refused(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    fields: dict[str, object] = {"slot_id": "contribution", "version": 2}
    raw = _sized(fields, "probe_padding", _CEILING + 1)

    response = _raw_call(prompt_module, "activate", raw)

    _assert_refused_for_size(response, collaborators, _CEILING + 1, "x" * 1000)


def test_an_activate_body_at_the_ceiling_reaches_the_existing_field_refusal(
    prompt_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collaborators = _ceiling(prompt_module, monkeypatch)
    fields: dict[str, object] = {"slot_id": "contribution", "version": 2}
    raw = _sized(fields, "probe_padding", _CEILING)

    response = _raw_call(prompt_module, "activate", raw)

    # The unknown field is refused by `_exact`, so the ceiling does not refuse every activate.
    assert response["statusCode"] == 400
    assert _body(response) == {"error": "invalid_request"}
    collaborators.process_span.record.assert_called_once_with(outcome="refused", status=400)
    assert collaborators.store.method_calls == []
