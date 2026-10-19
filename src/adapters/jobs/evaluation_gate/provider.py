# SPDX-License-Identifier: MIT-0
"""Create, update, and delete one family's online evaluation for the enablement gate (ADR-079).

`Custom::AgentCoreOnlineEvaluation` is the only owner of the configuration and the only writer of
the family's release record, which the outbox dispatcher reads as the switch. The provider runs
the LLD's gate steps on every callback and answers CloudFormation exactly once, never SUCCESS
before the configuration, its evaluators, the owned result group, and the release record read
back as intended.

- **Create** first refuses, touching nothing, when a configuration with the spec's name already
  exists: it belongs to another resource, and so does the record. It then writes the release
  record not enabled, creates the configuration disabled and bound
  to the owned group, waits for it, and asserts every control. When execution is requested it
  calibrates every evaluator, enables, reads `ENABLED` back, and only then writes the record
  enabled. Once the configuration exists its id is the PhysicalResourceId, so a rollback's Delete
  removes it.
- **Update** replaces the configuration when the live one is missing or has another name; the new
  PhysicalResourceId makes CloudFormation delete the old one. Otherwise, when execution is not
  requested, it writes the record not enabled and rebinds the configuration disabled. When it is
  requested and the configuration is enabled, every assertion holds, and the record already
  names exactly this configuration and evaluator set, the configuration is left running and only
  calibrated; any other state follows the evaluator-set change order: record not enabled,
  disable and rebind, assert, calibrate, enable, read back, record enabled.
- **Delete** writes the record not enabled, disables the configuration, deletes it, and deletes
  the record. A record that enables another configuration (a replacement's) is left alone.

An execution-status change is skipped when the configuration already reads back as its target,
so a replayed client token's no-op is never what a poll waits on. No state-changing call, poll,
or Evaluate starts after the run's deadline: a run that overstays it may be racing a rollback or
Lambda's retry of the same event. Only the fail-closed steps, which write not enabled and
disable, run past it.

Any failed assertion or calibration leaves the configuration disabled and the record not enabled
and fails the resource with a Reason naming only closed assertion names, evaluator names, and
calibration case names. No prompt, question, answer, or evaluator explanation is logged or
returned.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import EVALUATION_GATE
from domain.evaluation.gate import (
    CalibrationFailureV1,
    CalibrationObservationV1,
    CalibrationOutcomeV1,
    EvaluationGateSpecError,
    EvaluationGateSpecV1,
    ExecutionStatus,
    GateCallbackEvent,
    GateFinding,
    GateRequestType,
    OnlineEvaluationConfigState,
    OnlineEvaluationStatus,
    binding_findings,
    calibration_inputs,
    calibration_outcome,
    client_token,
    evaluator_findings,
    is_config_id,
    parse_gate_event,
    release_enabled,
    release_matches,
    release_not_enabled,
)
from domain.evaluation.release import EvaluationReleaseV1
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.custom_resource_response import (
    CustomResourceResponderPort,
    CustomResourceResponse,
)
from ports.outbound.evaluation_gate import (
    CalibrationEvaluateRequest,
    CreateOnlineEvaluationRequest,
    EvaluationReleaseStorePort,
    OnlineEvaluationControlPort,
    OnlineEvaluationNotFoundError,
    ResultGroupReadbackPort,
    UpdateOnlineEvaluationRequest,
)

__all__ = [
    "EVALUATION_GATE_PROVISION_FAILED",
    "EVALUATION_GATE_REFUSED",
    "EVALUATION_GATE_RELEASE_MISMATCH",
    "EVALUATION_GATE_SPEC_INVALID",
    "EVALUATION_GATE_UNANSWERABLE",
    "LOGGER",
    "MAX_REASON_LENGTH",
    "GateRefusedError",
    "ProviderRuntime",
    "RunRequest",
    "RunResult",
    "run",
]

LOGGER: Final = Logger(service="adlytics-evaluation-gate")
_OPERATION: Final = EVALUATION_GATE
_COMPONENT: Final = "evaluation_gate"

EVALUATION_GATE_SPEC_INVALID: Final = ErrorCode("EVALUATION", 8)
EVALUATION_GATE_PROVISION_FAILED: Final = ErrorCode("EVALUATION", 12)
EVALUATION_GATE_REFUSED: Final = ErrorCode("EVALUATION", 13)
EVALUATION_GATE_UNANSWERABLE: Final = ErrorCode("EVALUATION", 14)
EVALUATION_GATE_RELEASE_MISMATCH: Final = ErrorCode("EVALUATION", 15)

MAX_REASON_LENGTH: Final = 1000
"""Keeps the whole response well under CloudFormation's 4096-byte response limit."""

_FAILED_PLACEHOLDER: Final = "evaluation-gate:failed:"
_NOT_READ: Final = "not yet read"
_FAIL_CLOSED_ERRORS: Final = (AdlyticsError, OnlineEvaluationNotFoundError, ValueError)
"""What a best-effort fail-closed step may swallow; the original failure is what is answered."""


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """The three gate seams, the response channel, and the clocks this provider runs against."""

    control: OnlineEvaluationControlPort
    readback: ResultGroupReadbackPort
    releases: EvaluationReleaseStorePort
    responder: CustomResourceResponderPort
    clock: Callable[[], float]
    """Monotonic seconds."""
    sleep: Callable[[float], None]
    now: Callable[[], datetime]
    """Aware UTC wall time, for a release record's decision time."""
    poll_interval_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One raw callback, the runtime to serve it with, and when waiting must give up."""

    event: object
    runtime: ProviderRuntime
    deadline: float
    """Monotonic instant after which no further poll or Evaluate starts."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """The one answer sent, and the content-free facts the handler's span records."""

    status: str
    request_type: str
    family: str = ""
    requested: bool | None = None
    execution_status: str = ""
    failed_assertion_count: int = 0
    calibration_case_count: int = 0
    calibration_failure_count: int = 0
    error: Exception | None = None


class GateRefusedError(InvariantError):
    """A readback assertion or a calibration expectation did not hold."""

    def __init__(
        self,
        description: str,
        findings: tuple[GateFinding, ...] = (),
        calibration: tuple[CalibrationFailureV1, ...] = (),
    ) -> None:
        super().__init__(
            EVALUATION_GATE_REFUSED,
            description,
            context={
                "failed_assertions": [str(finding) for finding in findings],
                "calibration_failures": [
                    {
                        "case": failure.case_name,
                        "evaluator": failure.evaluator_name,
                        "kind": str(failure.kind),
                        "expected": list(failure.expected),
                        "observed": list(failure.observed),
                    }
                    for failure in calibration
                ],
            },
        )
        self.findings = findings
        self.calibration = calibration


class _ProvisionFailedError(DependencyError):
    """A configuration operation that ended failed, or did not finish in time."""

    def __init__(self, reason: str) -> None:
        super().__init__(EVALUATION_GATE_PROVISION_FAILED, reason)


@dataclass(slots=True)
class _Session:
    """One callback's progress: what CloudFormation must be told if anything fails now."""

    callback: GateCallbackEvent
    request: RunRequest
    physical_resource_id: str
    execution_status: ExecutionStatus | None = None
    findings: tuple[GateFinding, ...] = ()
    calibration: CalibrationOutcomeV1 | None = None
    calibrated_cases: int = 0

    @property
    def spec(self) -> EvaluationGateSpecV1:
        return self.callback.spec

    @property
    def runtime(self) -> ProviderRuntime:
        return self.request.runtime

    def token(self, operation: str) -> str:
        return client_token(self.callback, operation)

    def guard(self, step: str) -> None:
        """Refuse to start a state-changing step once the provider deadline has passed.

        A run past its deadline may be racing a rollback or Lambda's retry of the same event, so
        it changes nothing more; only the fail-closed steps, which disable, run past it.
        """
        if self.runtime.clock() >= self.request.deadline:
            raise _ProvisionFailedError(
                f"{step}: gave up before the provider timeout; nothing further was changed."
            )

    def decided_at(self) -> str:
        return self.runtime.now().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class _Poll:
    session: _Session
    operation: str

    def _expire(self, config_id: str, last: str) -> float:
        """Raise once the deadline has passed; otherwise return the time left."""
        now = self.session.runtime.clock()
        deadline = self.session.request.deadline
        if now >= deadline:
            raise _ProvisionFailedError(
                f"{self.operation}: gave up waiting for online evaluation configuration "
                f"{config_id} before the provider timeout; last status {last}."
            )
        return deadline - now

    def _pause(self, config_id: str, last: str) -> None:
        left = self._expire(config_id, last)
        runtime = self.session.runtime
        runtime.sleep(min(runtime.poll_interval_seconds, left))

    def observe(self, config_id: str, last: str = _NOT_READ) -> OnlineEvaluationConfigState:
        """Read the configuration once, never past the deadline; not-found propagates."""
        self._expire(config_id, last)
        return self.session.runtime.control.get(config_id)

    def failure(self, state: OnlineEvaluationConfigState) -> _ProvisionFailedError:
        detail = f": {state.failure_reason}" if state.failure_reason else ""
        return _ProvisionFailedError(
            f"{self.operation}: online evaluation configuration {state.config_id} reached "
            f"{state.status}{detail}"
        )

    def ready(self, config_id: str, execution: ExecutionStatus) -> OnlineEvaluationConfigState:
        """Wait for ACTIVE in the requested execution state; fail on a failed or deleting one."""
        last = _NOT_READ
        while True:
            try:
                state = self.observe(config_id, last)
            except OnlineEvaluationNotFoundError:
                raise _ProvisionFailedError(
                    f"{self.operation}: online evaluation configuration {config_id} disappeared "
                    "while waiting for ACTIVE."
                ) from None
            if state.status.failed or state.status is OnlineEvaluationStatus.DELETING:
                raise self.failure(state)
            if (
                state.status is OnlineEvaluationStatus.ACTIVE
                and state.execution_status is execution
            ):
                return state
            last = f"{state.status}/{state.execution_status}"
            self._pause(config_id, last)

    def settled(self, config_id: str) -> OnlineEvaluationConfigState:
        """Wait out a create or update already running; not-found propagates."""
        last = _NOT_READ
        while True:
            state = self.observe(config_id, last)
            if not state.status.in_progress:
                return state
            last = str(state.status)
            self._pause(config_id, last)

    def gone(self, config_id: str) -> None:
        """Wait until the configuration reads as absent; only the deadline ends the wait.

        The service model names no delete-failed status, and a read right after the delete may
        still report the prior one, so no status short of absence is final here.
        """
        last = _NOT_READ
        while True:
            try:
                state = self.observe(config_id, last)
            except OnlineEvaluationNotFoundError:
                return
            last = str(state.status)
            self._pause(config_id, last)


def _data(config_id: str, execution: ExecutionStatus) -> dict[str, str]:
    return {"ConfigId": config_id, "ExecutionStatus": str(execution)}


def _write_release(
    session: _Session, release: EvaluationReleaseV1, *, bounded: bool = True
) -> None:
    """Write the record and read it back through the dispatcher's own decoder.

    Unbounded only for the fail-closed not-enabled write, which may run past the deadline.
    """
    releases = session.runtime.releases
    name = session.spec.release_parameter_name
    if bounded:
        session.guard(f"Evaluation release record {session.spec.family} write")
    releases.write(name, release)
    if releases.read(name) != release:
        raise InvariantError(
            EVALUATION_GATE_RELEASE_MISMATCH,
            "The evaluation release record did not read back as written.",
        )


def _record_not_enabled(session: _Session, *, bounded: bool = True) -> None:
    _write_release(
        session, release_not_enabled(session.spec.family, session.decided_at()), bounded=bounded
    )


def _all_findings(
    session: _Session, state: OnlineEvaluationConfigState, execution: ExecutionStatus
) -> tuple[GateFinding, ...]:
    """Every readback assertion: the binding, the evaluators, and the owned group."""
    spec = session.spec
    control = session.runtime.control
    evaluators = [control.evaluator(evaluator_id) for evaluator_id in spec.evaluator_ids]
    findings = (
        *binding_findings(spec, state, execution),
        *evaluator_findings(spec, evaluators),
        *session.runtime.readback.findings(spec.custody()),
    )
    return tuple(dict.fromkeys(findings))


def _assert_bound(
    session: _Session, state: OnlineEvaluationConfigState, execution: ExecutionStatus
) -> None:
    findings = _all_findings(session, state, execution)
    session.findings = findings
    if findings:
        names = ", ".join(str(finding) for finding in findings)
        raise GateRefusedError(
            f"Evaluation gate {session.spec.family} refused: failed assertions {names}.",
            findings=findings,
        )


def _calibrate(session: _Session) -> None:
    """Score every declared (case, evaluator) expectation; any miss blocks enablement."""
    spec = session.spec
    runtime = session.runtime
    inputs = calibration_inputs(spec)
    observations: list[CalibrationObservationV1] = []
    for index, item in enumerate(inputs):
        if runtime.clock() >= session.request.deadline:
            raise _ProvisionFailedError(
                f"Calibration of evaluation family {spec.family} gave up before the provider "
                f"timeout after {index} of {len(inputs)} evaluations."
            )
        observations.append(
            runtime.control.evaluate(
                CalibrationEvaluateRequest(
                    calibration=item, service_name=spec.input_service_names[0]
                )
            )
        )
    outcome = calibration_outcome(spec, observations)
    session.calibration = outcome
    session.calibrated_cases = len(spec.calibration)
    if not outcome.passed:
        names = ", ".join(str(failure) for failure in outcome.failures) or "no expectations"
        raise GateRefusedError(
            f"Evaluation gate {spec.family} refused: calibration failed for {names}.",
            calibration=outcome.failures,
        )


def _disable_best_effort(session: _Session, config_id: str, operation: str) -> None:
    """Issue a status-only disable under its own token; a refusal is swallowed.

    Not bounded by the deadline: disabling only narrows what the configuration does.
    """
    with contextlib.suppress(*_FAIL_CLOSED_ERRORS):
        session.runtime.control.update(
            UpdateOnlineEvaluationRequest(
                config_id=config_id,
                execution_status=ExecutionStatus.DISABLED,
                client_token=session.token(operation),
            )
        )
        session.execution_status = ExecutionStatus.DISABLED


def _fail_closed(session: _Session, config_id: str, operation: str) -> None:
    """Best effort after a failure past the disabled state: record not enabled, then disable."""
    with contextlib.suppress(*_FAIL_CLOSED_ERRORS):
        _record_not_enabled(session, bounded=False)
    _disable_best_effort(session, config_id, f"{operation}:fail-closed")


def _already(
    spec: EvaluationGateSpecV1, state: OnlineEvaluationConfigState, execution: ExecutionStatus
) -> bool:
    """Whether the configuration already is exactly the target an update would request.

    Skipping that update keeps a replayed client token, whose earlier operation the service
    answers as a no-op, from being the only thing a poll then waits on.
    """
    return not binding_findings(spec, state, execution) and state.description == spec.description


def _disable_and_bind(
    session: _Session, operation: str, current: OnlineEvaluationConfigState
) -> None:
    """LLD steps 2-4: record not enabled first, then disabled and bound, then asserted.

    If the bound update or its wait fails, a status-only disable under its own token follows
    before the failure propagates, so a configuration that was enabled is not left running
    while its record says not enabled.
    """
    config_id = current.config_id
    _record_not_enabled(session)
    try:
        if not _already(session.spec, current, ExecutionStatus.DISABLED):
            session.guard(f"{operation}: disable")
            session.runtime.control.update(
                UpdateOnlineEvaluationRequest(
                    config_id=config_id,
                    execution_status=ExecutionStatus.DISABLED,
                    client_token=session.token(f"{operation}:disable"),
                    spec=session.spec,
                )
            )
        session.execution_status = ExecutionStatus.DISABLED
        state = _Poll(session, operation).ready(config_id, ExecutionStatus.DISABLED)
    except Exception:
        _disable_best_effort(session, config_id, f"{operation}:fail-closed-disable")
        raise
    _assert_bound(session, state, ExecutionStatus.DISABLED)


def _enable(session: _Session, config_id: str, operation: str) -> None:
    """LLD step 5 from the disabled state: calibrate, enable, read back, then record enabled."""
    _calibrate(session)  # on failure the configuration is still disabled, its record not enabled
    try:
        poll = _Poll(session, operation)
        if not _already(session.spec, poll.observe(config_id), ExecutionStatus.ENABLED):
            session.guard(f"{operation}: enable")
            session.runtime.control.update(
                UpdateOnlineEvaluationRequest(
                    config_id=config_id,
                    execution_status=ExecutionStatus.ENABLED,
                    client_token=session.token(f"{operation}:enable"),
                    spec=session.spec,
                )
            )
        state = poll.ready(config_id, ExecutionStatus.ENABLED)
        session.execution_status = ExecutionStatus.ENABLED
        _assert_bound(session, state, ExecutionStatus.ENABLED)
        _write_release(session, release_enabled(session.spec, config_id, session.decided_at()))
    except Exception:
        _fail_closed(session, config_id, operation)
        raise


def _create(session: _Session, operation: str) -> dict[str, str]:
    spec = session.spec
    runtime = session.runtime
    existing = runtime.control.find_by_name(spec.config_name)
    if existing is not None:
        # Another resource owns this name: its record is not this resource's to write or delete.
        raise GateRefusedError(
            f"{operation}: online evaluation configuration {existing.config_id} already has the "
            f"name {spec.config_name}; the release record was not written."
        )
    _record_not_enabled(session)
    session.guard(f"{operation}: create")
    config_id = runtime.control.create(
        CreateOnlineEvaluationRequest(spec=spec, client_token=session.token(f"{operation}:create"))
    )
    session.physical_resource_id = config_id  # from here a rollback's Delete must find it
    session.execution_status = ExecutionStatus.DISABLED
    state = _Poll(session, operation).ready(config_id, ExecutionStatus.DISABLED)
    _assert_bound(session, state, ExecutionStatus.DISABLED)
    if spec.execution_requested:
        _enable(session, config_id, operation)
    return _data(config_id, cast("ExecutionStatus", session.execution_status))


def _replace(session: _Session) -> dict[str, str]:
    """Create the new configuration; on failure remove it so the rollback leaves no orphan."""
    previous = session.physical_resource_id
    try:
        return _create(session, "Update (replacement)")
    except Exception:
        created = session.physical_resource_id
        if created != previous and is_config_id(created):
            with contextlib.suppress(*_FAIL_CLOSED_ERRORS):
                session.runtime.control.delete(created)
        session.physical_resource_id = previous
        raise


def _held(session: _Session, state: OnlineEvaluationConfigState) -> bool:
    """Whether a running configuration already is exactly what its record enables."""
    if state.status is not OnlineEvaluationStatus.ACTIVE:
        return False
    if state.execution_status is not ExecutionStatus.ENABLED:
        return False
    if _all_findings(session, state, ExecutionStatus.ENABLED):
        return False
    spec = session.spec
    record = session.runtime.releases.read(spec.release_parameter_name)
    return release_matches(record, spec, state.config_id)


def _update(session: _Session) -> dict[str, str]:
    config_id = session.callback.physical_resource_id
    if not is_config_id(config_id):
        return _replace(session)
    poll = _Poll(session, "Update")
    try:
        current = poll.settled(config_id)
    except OnlineEvaluationNotFoundError:
        # Deleted outside CloudFormation: recreate, and the old id's Delete then finds nothing.
        return _replace(session)
    if current.config_name != session.spec.config_name:
        # Decided on the live name, not OldResourceProperties, so a rollback converges in place.
        return _replace(session)
    if current.status is OnlineEvaluationStatus.DELETING:
        raise poll.failure(current)
    session.execution_status = current.execution_status
    if not session.spec.execution_requested:
        _disable_and_bind(session, "Update", current)
        return _data(config_id, ExecutionStatus.DISABLED)
    if _held(session, current):
        # Left running so a routine deploy opens no evaluation gap, but still calibrated.
        try:
            _calibrate(session)
        except Exception:
            _fail_closed(session, config_id, "Update")
            raise
        return _data(config_id, ExecutionStatus.ENABLED)
    _disable_and_bind(session, "Update", current)
    _enable(session, config_id, "Update")
    return _data(config_id, ExecutionStatus.ENABLED)


def _delete(session: _Session) -> None:
    spec = session.spec
    runtime = session.runtime
    config_id = session.callback.physical_resource_id
    record = runtime.releases.read(spec.release_parameter_name)
    # A record that enables another configuration belongs to this resource's replacement.
    ours = record is None or not record.enabled or record.config_id == config_id
    # A failed create's placeholder never wrote the record, so its Delete never writes it either.
    placeholder = not is_config_id(config_id)
    if ours and not placeholder and record is not None and record.enabled:
        _record_not_enabled(session)
    if not placeholder:
        poll = _Poll(session, "Delete")
        try:
            current = poll.settled(config_id)
            if current.status is not OnlineEvaluationStatus.DELETING:
                if current.execution_status is ExecutionStatus.ENABLED:
                    session.guard("Delete: disable")
                    runtime.control.update(
                        UpdateOnlineEvaluationRequest(
                            config_id=config_id,
                            execution_status=ExecutionStatus.DISABLED,
                            client_token=session.token("Delete:disable"),
                        )
                    )
                    poll.settled(config_id)
                session.execution_status = ExecutionStatus.DISABLED
                session.guard("Delete: delete")
                runtime.control.delete(config_id)
            poll.gone(config_id)
        except OnlineEvaluationNotFoundError:
            pass
    if ours:
        session.guard(f"Evaluation release record {spec.family} delete")
        runtime.releases.delete(spec.release_parameter_name)


def _dispatch(session: _Session) -> dict[str, str] | None:
    request_type = session.callback.request_type
    if request_type is GateRequestType.CREATE:
        return _create(session, "Create")
    if request_type is GateRequestType.UPDATE:
        return _update(session)
    _delete(session)
    return None


def _text(event: Mapping[str, object], name: str) -> str:
    value = event.get(name)
    return value if isinstance(value, str) else ""


def _bounded(text: str) -> str:
    single_line = " ".join(text.split())
    if len(single_line) <= MAX_REASON_LENGTH:
        return single_line
    return single_line[: MAX_REASON_LENGTH - 3] + "..."


def _reason(error: Exception, request_id: str) -> str:
    """Name the failure precisely without any value from the spec, a prompt, or an answer."""
    if isinstance(error, AdlyticsError):
        detail = [error.description]
        code = error.context.get("service_code")
        message = error.context.get("provider_message")
        if isinstance(code, str) and code:
            detail.append(f"{code}:")
        if isinstance(message, str) and message:
            detail.append(message)  # already redacted by aws_failure_context
        return _bounded(" ".join(detail))
    return _bounded(
        f"Unexpected {type(error).__name__} in the evaluation gate provider; "
        f"RequestId {request_id}."
    )


def _parsed(event: Mapping[str, object], incoming: str) -> GateCallbackEvent | None:
    """Parse the callback; None only for a failed create's Delete whose spec is still refused.

    A create refused before any call left nothing behind, and its rollback's Delete carries the
    same refused spec, so answering that Delete FAILED would wedge the stack in rollback.
    """
    try:
        return parse_gate_event(event)
    except EvaluationGateSpecError as error:
        placeholder_delete = _text(event, "RequestType") == str(
            GateRequestType.DELETE
        ) and incoming.startswith(_FAILED_PLACEHOLDER)
        if placeholder_delete:
            return None
        raise ValidationError(EVALUATION_GATE_SPEC_INVALID, str(error)) from error


def _event_mapping(event: object) -> Mapping[str, object]:
    if isinstance(event, Mapping):
        return cast("Mapping[str, object]", event)
    return {}


@dataclass(frozen=True, slots=True)
class _Response:
    status: str
    reason: str
    physical_resource_id: str
    data: Mapping[str, str] | None


def _body(event: Mapping[str, object], response: _Response) -> bytes:
    document: dict[str, object] = {
        "Status": response.status,
        "Reason": response.reason,
        "PhysicalResourceId": response.physical_resource_id,
        "StackId": _text(event, "StackId"),
        "RequestId": _text(event, "RequestId"),
        "LogicalResourceId": _text(event, "LogicalResourceId"),
    }
    if response.data is not None:
        document["Data"] = dict(response.data)
    return json.dumps(document).encode("utf-8")


def _result(
    response: _Response, event: Mapping[str, object], session: _Session | None
) -> RunResult:
    request_type = _text(event, "RequestType")
    if session is None:
        return RunResult(status=response.status, request_type=request_type)
    calibration = session.calibration
    return RunResult(
        status=response.status,
        request_type=request_type,
        family=str(session.spec.family),
        requested=session.spec.execution_requested,
        execution_status="" if session.execution_status is None else str(session.execution_status),
        failed_assertion_count=len(session.findings),
        calibration_case_count=session.calibrated_cases,
        calibration_failure_count=0 if calibration is None else len(calibration.failures),
    )


def run(request: RunRequest) -> RunResult:
    """Serve one callback and deliver exactly one response to its ResponseURL."""
    event = _event_mapping(request.event)
    request_id = _text(event, "RequestId")
    url = _text(event, "ResponseURL")
    if not url.startswith("https://"):
        # Nothing can be answered, so nothing is done: no configuration or record is touched.
        raise InvariantError(
            EVALUATION_GATE_UNANSWERABLE, "The callback carries no https ResponseURL."
        )
    incoming = _text(event, "PhysicalResourceId").strip()
    fallback = incoming or f"{_FAILED_PLACEHOLDER}{request_id or 'unknown'}"
    session: _Session | None = None
    failure: Exception | None = None
    try:
        callback = _parsed(event, incoming)
        if callback is None:
            response = _Response("SUCCESS", "", incoming, None)
        else:
            session = _Session(callback=callback, request=request, physical_resource_id=fallback)
            data = _dispatch(session)
            response = _Response("SUCCESS", "", session.physical_resource_id, data)
    except Exception as error:
        failure = error
        report_error(error, Report(operation=_OPERATION, correlation_id="", logger=LOGGER))
        physical = fallback if session is None else session.physical_resource_id
        response = _Response("FAILED", _reason(error, request_id), physical, None)
    request.runtime.responder.send(
        CustomResourceResponse(response_url=url, body=_body(event, response))
    )
    result = _result(response, event, session)
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=response.status.lower(),
            severity=LogSeverity.INFO if response.status == "SUCCESS" else LogSeverity.WARNING,
            details={
                "request_id": request_id,
                "request_type": result.request_type,
                "physical_resource_id": response.physical_resource_id,
                "family": result.family,
                "requested": result.requested,
                "execution_status": result.execution_status,
                "failed_assertion_count": result.failed_assertion_count,
                "calibration_case_count": result.calibration_case_count,
                "calibration_failure_count": result.calibration_failure_count,
            },
        ),
        LOGGER,
    )
    return RunResult(
        status=result.status,
        request_type=result.request_type,
        family=result.family,
        requested=result.requested,
        execution_status=result.execution_status,
        failed_assertion_count=result.failed_assertion_count,
        calibration_case_count=result.calibration_case_count,
        calibration_failure_count=result.calibration_failure_count,
        error=failure,
    )
