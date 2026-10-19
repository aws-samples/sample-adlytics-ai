# SPDX-License-Identifier: MIT-0
"""Create, update and delete the managed Harness for `Custom::AgentCoreHarness` (ADR-077).

CloudFormation's Harness schema rejects the Gateway tool's `TOKEN_EXCHANGE` grant, so this
provider is the Harness's one owner until the schema accepts it. Every callback gets exactly one
response. On CREATE_FAILED CloudFormation destroys the provider's log group with the rollback, so
the response Reason, not the log, is the diagnostic: it names the failing operation, the
provider's error code and redacted message, or the last status polled.

- Create starts CreateHarness under a client token derived from the RequestId and waits for
  READY. Once a Harness exists its id is the PhysicalResourceId even when the wait fails, so the
  rollback's Delete removes it rather than orphaning it.
- Update replaces the Harness when its name changed (or when it no longer exists); the new
  PhysicalResourceId makes CloudFormation delete the old one. Otherwise it patches every mutable
  member as the full desired state, which corrects out-of-band drift, and waits for READY.
- After a Create or Update reaches READY, the runtime's own log group is created if absent and
  given the configured retention, and its name is returned as `RuntimeLogGroupName`.
- Delete removes the Harness, never its memory, and waits until it is gone. The runtime log
  group is left to age out under its retention: it holds the diagnostics of the turns just run.
  A PhysicalResourceId that names no Harness (a failed create's placeholder) succeeds without a
  call.

Span-free: CloudFormation is not a traced caller. Logs and metrics use the shared reporting seam.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast

from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from domain.operations.harness_resource import (
    CallbackEvent,
    HarnessResourceError,
    HarnessSpec,
    RequestType,
    client_token,
    is_harness_id,
    parse_event,
    runtime_log_group,
)
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
from ports.outbound.harness_control import (
    CreateHarnessRequest,
    HarnessControlPort,
    HarnessNotFoundError,
    HarnessState,
    HarnessStatus,
    ReplaceTagsRequest,
    UpdateHarnessRequest,
)
from ports.outbound.log_retention import LogRetentionPort, RetainLogGroupRequest

__all__ = [
    "HARNESS_PROVISION_FAILED",
    "HARNESS_SPEC_INVALID",
    "MAX_REASON_LENGTH",
    "ProviderRuntime",
    "RunRequest",
    "run",
]

LOGGER: Final = Logger(service="adlytics-harness-resource")
_OPERATION: Final = "harness_resource.callback"
_COMPONENT: Final = "harness_resource"

HARNESS_SPEC_INVALID: Final = ErrorCode("HARNESSCR", 1)
HARNESS_PROVISION_FAILED: Final = ErrorCode("HARNESSCR", 4)
HARNESS_UNANSWERABLE: Final = ErrorCode("HARNESSCR", 5)

MAX_REASON_LENGTH: Final = 1000
"""Keeps the whole response well under CloudFormation's 4096-byte response limit."""

_FAILED_PLACEHOLDER: Final = "harness-resource:failed:"


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """The control plane, the response channel, and the clock this provider runs against."""

    control: HarnessControlPort
    responder: CustomResourceResponderPort
    logs: LogRetentionPort
    clock: Callable[[], float]
    """Monotonic seconds."""
    sleep: Callable[[float], None]
    poll_interval_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One raw callback, the runtime to serve it with, and when polling must give up."""

    event: object
    runtime: ProviderRuntime
    deadline: float
    """Monotonic instant after which no further poll starts; set with margin before timeout."""


@dataclass(frozen=True, slots=True)
class _Outcome:
    physical_resource_id: str
    data: Mapping[str, str] | None


class _ProvisionFailedError(DependencyError):
    """A Harness operation that ended FAILED, or did not finish in time."""

    def __init__(self, physical_resource_id: str, reason: str) -> None:
        super().__init__(HARNESS_PROVISION_FAILED, reason)
        self.physical_resource_id = physical_resource_id


def _data(state: HarnessState, log_group: str | None) -> dict[str, str]:
    data = {"Arn": state.arn, "HarnessId": state.harness_id, "HarnessName": state.harness_name}
    if log_group is not None:
        data["RuntimeLogGroupName"] = log_group
    return data


def _retain_runtime_logs(
    callback: CallbackEvent, request: RunRequest, ready: HarnessState
) -> str | None:
    """Apply the configured retention to the READY Harness's runtime log group; return its name.

    Every Create and Update re-applies it, so a retention changed outside CloudFormation is
    corrected on the next deploy, as the Harness itself is. A failure fails the callback: a
    Harness whose transcripts would be kept forever is not reported ready. Any failure, expected
    or not, names the Harness, so a failed Create's rollback deletes it rather than orphaning it.
    A failure that persists also fails the rollback of an Update, which then needs
    `continue-update-rollback`; that is the cost of refusing to report an unretained group.
    """
    days = callback.runtime_log_retention_days
    if days is None:
        return None
    try:
        group = runtime_log_group(ready.runtime_arn, ready.harness_name)
    except HarnessResourceError as error:
        raise _ProvisionFailedError(ready.harness_id, _bounded(str(error))) from error
    try:
        request.runtime.logs.retain(
            RetainLogGroupRequest(
                log_group_name=group, retention_days=days, tags=callback.spec.tags or {}
            )
        )
    except AdlyticsError as error:
        raise _ProvisionFailedError(ready.harness_id, _described(error)) from error
    except Exception as error:
        # Named by type alone: an unexpected message may carry a response value.
        raise _ProvisionFailedError(
            ready.harness_id,
            _bounded(f"Retaining {group}: unexpected {type(error).__name__}."),
        ) from error
    return group


@dataclass(frozen=True, slots=True)
class _Poll:
    runtime: ProviderRuntime
    deadline: float
    operation: str
    physical_resource_id: str
    """What the failure response names if this wait fails."""

    def _pause(self, harness_id: str, last: str) -> None:
        now = self.runtime.clock()
        if now >= self.deadline:
            raise _ProvisionFailedError(
                self.physical_resource_id,
                f"{self.operation}: gave up waiting for Harness {harness_id} before the provider "
                f"timeout; last status {last}.",
            )
        self.runtime.sleep(min(self.runtime.poll_interval_seconds, self.deadline - now))

    def failure(self, state: HarnessState) -> _ProvisionFailedError:
        """Build the failure naming a Harness that reached a status this wait cannot accept."""
        detail = f": {state.failure_reason}" if state.failure_reason else ""
        return _ProvisionFailedError(
            self.physical_resource_id,
            f"{self.operation}: Harness {state.harness_id} reached {state.status}{detail}",
        )

    def ready(self, harness_id: str) -> HarnessState:
        """Wait for READY; fail on any *_FAILED status or at the deadline."""
        while True:
            try:
                state = self.runtime.control.get(harness_id)
            except HarnessNotFoundError:
                raise _ProvisionFailedError(
                    self.physical_resource_id,
                    f"{self.operation}: Harness {harness_id} disappeared while waiting for READY.",
                ) from None
            if state.status is HarnessStatus.READY:
                return state
            if state.status.failed or state.status is HarnessStatus.DELETING:
                raise self.failure(state)
            self._pause(harness_id, state.status)

    def settled(self, harness_id: str) -> HarnessState:
        """Wait out a create or update already running; HarnessNotFoundError propagates."""
        while True:
            state = self.runtime.control.get(harness_id)
            if not state.status.in_progress:
                return state
            self._pause(harness_id, state.status)

    def gone(self, harness_id: str) -> None:
        """Wait until GetHarness reports the Harness absent; fail on DELETE_FAILED."""
        while True:
            try:
                state = self.runtime.control.get(harness_id)
            except HarnessNotFoundError:
                return
            if state.status is HarnessStatus.DELETE_FAILED:
                raise self.failure(state)
            self._pause(harness_id, state.status)


def _create(callback: CallbackEvent, request: RunRequest, operation: str) -> _Outcome:
    started = request.runtime.control.create(
        CreateHarnessRequest(
            spec=callback.spec, client_token=client_token(callback, f"{operation}:create")
        )
    )
    poll = _Poll(request.runtime, request.deadline, operation, started.harness_id)
    try:
        ready = poll.ready(started.harness_id)
    except _ProvisionFailedError:
        raise
    except Exception as error:
        # The Harness exists, so any failure must name it for the rollback's Delete to find it.
        # An unexpected type (an unknown status, a missing response member) is named by its
        # type alone: its message may carry a response value.
        reason = (
            _described(error)
            if isinstance(error, AdlyticsError)
            else _bounded(
                f"{operation}: unexpected {type(error).__name__} while waiting for Harness "
                f"{started.harness_id}."
            )
        )
        raise _ProvisionFailedError(started.harness_id, reason) from error
    return _Outcome(ready.harness_id, _data(ready, _retain_runtime_logs(callback, request, ready)))


def _replace(callback: CallbackEvent, request: RunRequest) -> _Outcome:
    """Create the new Harness; on failure remove it so the rollback leaves no orphan."""
    try:
        return _create(callback, request, "Update (replacement)")
    except _ProvisionFailedError as failure:
        if is_harness_id(failure.physical_resource_id):
            # Best effort; the failure below is what CloudFormation must see. A failed cleanup
            # can leave an orphan Harness, so it is reported rather than discarded.
            try:
                request.runtime.control.delete(failure.physical_resource_id)
            except HarnessNotFoundError:
                pass
            except DependencyError as cleanup:
                report_error(
                    cleanup, Report(operation=_OPERATION, correlation_id="", logger=LOGGER)
                )
        raise _ProvisionFailedError(callback.physical_resource_id, failure.description) from failure


def _update(callback: CallbackEvent, request: RunRequest) -> _Outcome:
    previous = cast("HarnessSpec", callback.previous_spec)
    harness_id = callback.physical_resource_id
    if previous.name != callback.spec.name or not is_harness_id(harness_id):
        return _replace(callback, request)
    poll = _Poll(request.runtime, request.deadline, "Update", harness_id)
    try:
        current = poll.settled(harness_id)
    except HarnessNotFoundError:
        # Deleted outside CloudFormation: recreate, and the old id's Delete then finds nothing.
        return _replace(callback, request)
    if current.status in {HarnessStatus.DELETING, HarnessStatus.DELETE_FAILED}:
        raise poll.failure(current)
    document = callback.spec.update_document(previous)
    request.runtime.control.update(
        UpdateHarnessRequest(
            harness_id=harness_id,
            document=document,
            client_token=client_token(callback, "Update:update"),
        )
    )
    ready = poll.ready(harness_id)
    tags = callback.spec.tags
    if tags is not None:
        request.runtime.control.replace_tags(ReplaceTagsRequest(arn=ready.arn, tags=tags))
    return _Outcome(harness_id, _data(ready, _retain_runtime_logs(callback, request, ready)))


def _delete(callback: CallbackEvent, request: RunRequest) -> _Outcome:
    harness_id = callback.physical_resource_id
    outcome = _Outcome(harness_id, None)
    if not is_harness_id(harness_id):
        return outcome
    poll = _Poll(request.runtime, request.deadline, "Delete", harness_id)
    try:
        current = poll.settled(harness_id)
        if current.status is not HarnessStatus.DELETING:
            request.runtime.control.delete(harness_id)
    except HarnessNotFoundError:
        return outcome
    poll.gone(harness_id)
    return outcome


def _dispatch(callback: CallbackEvent, request: RunRequest) -> _Outcome:
    if callback.request_type is RequestType.CREATE:
        return _create(callback, request, "Create")
    if callback.request_type is RequestType.UPDATE:
        return _update(callback, request)
    return _delete(callback, request)


def _text(event: Mapping[str, object], name: str) -> str:
    value = event.get(name)
    return value if isinstance(value, str) else ""


def _bounded(text: str) -> str:
    single_line = " ".join(text.split())
    if len(single_line) <= MAX_REASON_LENGTH:
        return single_line
    return single_line[: MAX_REASON_LENGTH - 3] + "..."


def _described(error: AdlyticsError) -> str:
    """Join the error's description and the provider's code and redacted message, bounded."""
    detail = [error.description]
    code = error.context.get("service_code")
    message = error.context.get("provider_message")
    if isinstance(code, str) and code:
        detail.append(f"{code}:")
    if isinstance(message, str) and message:
        detail.append(message)  # already redacted by aws_failure_context
    return _bounded(" ".join(detail))


def _reason(error: Exception, request_id: str) -> str:
    """Name the failure precisely without any value from the spec or a credential."""
    if isinstance(error, AdlyticsError):
        return _described(error)
    return _bounded(
        f"Unexpected {type(error).__name__} in the Harness provider; RequestId {request_id}."
    )


def _failure_physical_id(event: Mapping[str, object], error: Exception) -> str:
    if isinstance(error, _ProvisionFailedError):
        return error.physical_resource_id
    incoming = _text(event, "PhysicalResourceId")
    if incoming.strip():
        return incoming
    return f"{_FAILED_PLACEHOLDER}{_text(event, 'RequestId') or 'unknown'}"


@dataclass(frozen=True, slots=True)
class _Response:
    status: str
    reason: str
    outcome: _Outcome


def _body(event: Mapping[str, object], response: _Response) -> bytes:
    document: dict[str, object] = {
        "Status": response.status,
        "Reason": response.reason,
        "PhysicalResourceId": response.outcome.physical_resource_id,
        "StackId": _text(event, "StackId"),
        "RequestId": _text(event, "RequestId"),
        "LogicalResourceId": _text(event, "LogicalResourceId"),
    }
    if response.outcome.data is not None:
        document["Data"] = dict(response.outcome.data)
    return json.dumps(document).encode("utf-8")


def _event_mapping(event: object) -> Mapping[str, object]:
    if isinstance(event, Mapping):
        return cast("Mapping[str, object]", event)
    return {}


def run(request: RunRequest) -> str:
    """Serve one callback and deliver exactly one response to its ResponseURL."""
    event = _event_mapping(request.event)
    request_id = _text(event, "RequestId")
    url = _text(event, "ResponseURL")
    if not url.startswith("https://"):
        # Nothing can be answered, so nothing is done: no Harness is touched.
        raise InvariantError(HARNESS_UNANSWERABLE, "The callback carries no https ResponseURL.")
    try:
        try:
            outcome = _dispatch(parse_event(event), request)
        except HarnessResourceError as error:
            raise ValidationError(HARNESS_SPEC_INVALID, str(error)) from error
        response = _Response("SUCCESS", "", outcome)
    except Exception as error:
        report_error(error, Report(operation=_OPERATION, correlation_id="", logger=LOGGER))
        failed = _Outcome(_failure_physical_id(event, error), None)
        response = _Response("FAILED", _reason(error, request_id), failed)
    request.runtime.responder.send(
        CustomResourceResponse(response_url=url, body=_body(event, response))
    )
    status = response.status
    outcome = response.outcome
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=status.lower(),
            severity=LogSeverity.INFO if status == "SUCCESS" else LogSeverity.WARNING,
            details={
                "request_id": request_id,
                "request_type": _text(event, "RequestType"),
                "physical_resource_id": outcome.physical_resource_id,
            },
        ),
        LOGGER,
    )
    return status
