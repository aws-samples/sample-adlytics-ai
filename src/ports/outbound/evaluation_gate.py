# SPDX-License-Identifier: MIT-0
"""Ports of the evaluation enablement gate, `Custom::AgentCoreOnlineEvaluation` (ADR-079).

Three seams: the AgentCore control plane and on-demand evaluation, the CloudWatch Logs readback
of the owned result group, and the family's release record in Parameter Store. Every method
raises `DependencyError` for a refusal or failure, carrying the provider diagnostic in its
context and never an SDK exception type; only the control plane's not-found is control flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from domain.evaluation.gate import (
    CalibrationInputV1,
    CalibrationObservationV1,
    EvaluationGateSpecV1,
    EvaluatorState,
    ExecutionStatus,
    GateFinding,
    OnlineEvaluationConfigState,
    ResultGroupCustodyV1,
    is_config_id,
)
from domain.evaluation.release import EvaluationReleaseV1

__all__ = [
    "CalibrationEvaluateRequest",
    "CreateOnlineEvaluationRequest",
    "EvaluationReleaseStorePort",
    "OnlineEvaluationControlPort",
    "OnlineEvaluationNotFoundError",
    "ResultGroupReadbackPort",
    "UpdateOnlineEvaluationRequest",
]

_CLIENT_TOKEN: Final = re.compile(r"\A[a-zA-Z0-9](-*[a-zA-Z0-9])*\Z")
_CLIENT_TOKEN_MIN: Final = 33
_CLIENT_TOKEN_MAX: Final = 256


class OnlineEvaluationNotFoundError(LookupError):
    """The named online-evaluation configuration does not exist (or no longer exists)."""


def _require_client_token(token: str) -> None:
    in_bounds = _CLIENT_TOKEN_MIN <= len(token) <= _CLIENT_TOKEN_MAX
    if not in_bounds or _CLIENT_TOKEN.fullmatch(token) is None:
        raise ValueError("A client token is 33-256 alphanumeric characters.")


@dataclass(frozen=True, slots=True)
class CreateOnlineEvaluationRequest:
    """Create the spec's configuration, disabled and bound to its owned result group."""

    spec: EvaluationGateSpecV1
    client_token: str

    def __post_init__(self) -> None:
        _require_client_token(self.client_token)


@dataclass(frozen=True, slots=True)
class UpdateOnlineEvaluationRequest:
    """Set a configuration's execution state, and with a spec, rebind every member to it."""

    config_id: str
    execution_status: ExecutionStatus
    client_token: str
    spec: EvaluationGateSpecV1 | None = None
    """None changes only the execution state (Delete's disable); otherwise the full state."""

    def __post_init__(self) -> None:
        if not is_config_id(self.config_id):
            raise ValueError("An online-evaluation configuration id is <name>-<10 characters>.")
        _require_client_token(self.client_token)


@dataclass(frozen=True, slots=True)
class CalibrationEvaluateRequest:
    """Score one fictional calibration case with one evaluator through on-demand Evaluate."""

    calibration: CalibrationInputV1
    service_name: str
    """The span resource's service name: the family's input service, as a released span has."""

    def __post_init__(self) -> None:
        if not self.service_name.strip():
            raise ValueError("A calibration span needs a service name.")


@runtime_checkable
class OnlineEvaluationControlPort(Protocol):
    """Mutate and observe one configuration and score calibration cases."""

    def create(self, request: CreateOnlineEvaluationRequest) -> str:
        """Start creating the configuration, never enabled, and return its id."""
        ...

    def get(self, config_id: str) -> OnlineEvaluationConfigState:
        """Return a configuration's current state; raises OnlineEvaluationNotFoundError."""
        ...

    def find_by_name(self, config_name: str) -> OnlineEvaluationConfigState | None:
        """Return the configuration with this name, or None when the account has none.

        Only identity, status, execution state, and failure reason are populated: the listing
        carries no binding. Used to refuse a create that would collide on the fixed name.
        """
        ...

    def update(self, request: UpdateOnlineEvaluationRequest) -> None:
        """Start updating a configuration; raises OnlineEvaluationNotFoundError."""
        ...

    def delete(self, config_id: str) -> None:
        """Start deleting a configuration; raises OnlineEvaluationNotFoundError."""
        ...

    def evaluator(self, evaluator_id: str) -> EvaluatorState:
        """Return one evaluator's identity, model, and prompt digest; never its prompt."""
        ...

    def evaluate(self, request: CalibrationEvaluateRequest) -> CalibrationObservationV1:
        """Score one case; a per-result service error is an observation, not a raise."""
        ...


@runtime_checkable
class ResultGroupReadbackPort(Protocol):
    """Read back every custody control on the owned result group (observability §3.7)."""

    def findings(self, custody: ResultGroupCustodyV1) -> tuple[GateFinding, ...]:
        """Return every failed assertion, empty when all hold; raises when one cannot be read."""
        ...


@runtime_checkable
class EvaluationReleaseStorePort(Protocol):
    """Read, write, and delete one family's release record; the gate is its only writer."""

    def read(self, parameter_name: str) -> EvaluationReleaseV1 | None:
        """Return the record, or None when the parameter does not exist."""
        ...

    def write(self, parameter_name: str, release: EvaluationReleaseV1) -> None:
        """Overwrite the record with one canonical `EvaluationRelease.v1` document."""
        ...

    def delete(self, parameter_name: str) -> None:
        """Delete the record; a parameter that does not exist is already deleted."""
        ...
