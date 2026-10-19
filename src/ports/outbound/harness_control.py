# SPDX-License-Identifier: MIT-0
"""Harness control-plane port: create, update, read and delete one managed Harness (ADR-077).

Used only by the custom resource that owns the Harness because CloudFormation's Harness schema
rejects the Gateway tool's `TOKEN_EXCHANGE` grant. Every method raises `HarnessNotFoundError`
when the Harness does not exist and `DependencyError` for any other refusal or failure, carrying
the provider diagnostic in its context and never an SDK exception type.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from domain.operations.harness_resource import HarnessSpec, is_harness_id

__all__ = [
    "CreateHarnessRequest",
    "HarnessControlPort",
    "HarnessNotFoundError",
    "HarnessState",
    "HarnessStatus",
    "ReplaceTagsRequest",
    "UpdateHarnessRequest",
]

_CLIENT_TOKEN: Final = re.compile(r"\A[a-zA-Z0-9](-*[a-zA-Z0-9])*\Z")
_CLIENT_TOKEN_MIN: Final = 33
_CLIENT_TOKEN_MAX: Final = 256


class HarnessNotFoundError(LookupError):
    """The named Harness does not exist (or no longer exists)."""


class HarnessStatus(StrEnum):
    """GetHarness `status`, closed to the service model's enum."""

    CREATING = "CREATING"
    CREATE_FAILED = "CREATE_FAILED"
    UPDATING = "UPDATING"
    UPDATE_FAILED = "UPDATE_FAILED"
    READY = "READY"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"

    @property
    def in_progress(self) -> bool:
        """Whether a create or update is still running and a new mutation would conflict."""
        return self in {HarnessStatus.CREATING, HarnessStatus.UPDATING}

    @property
    def failed(self) -> bool:
        """Whether the last operation ended in failure."""
        return self in {
            HarnessStatus.CREATE_FAILED,
            HarnessStatus.UPDATE_FAILED,
            HarnessStatus.DELETE_FAILED,
        }


@dataclass(frozen=True, slots=True)
class HarnessState:
    """What the control plane reports about one Harness."""

    harness_id: str
    harness_name: str
    arn: str
    status: HarnessStatus
    failure_reason: str = ""
    runtime_arn: str = ""
    """The AgentCore runtime the Harness provisioned; empty until the service reports one."""


def _require_client_token(token: str) -> None:
    in_bounds = _CLIENT_TOKEN_MIN <= len(token) <= _CLIENT_TOKEN_MAX
    if not in_bounds or _CLIENT_TOKEN.fullmatch(token) is None:
        raise ValueError("A client token is 33-256 alphanumeric characters.")


def _require_harness_id(harness_id: str) -> None:
    if not is_harness_id(harness_id):
        raise ValueError("A Harness id is <name>-<10 alphanumeric characters>.")


@dataclass(frozen=True, slots=True)
class CreateHarnessRequest:
    """Create the Harness a spec declares, idempotently under one client token."""

    spec: HarnessSpec
    client_token: str

    def __post_init__(self) -> None:
        _require_client_token(self.client_token)


@dataclass(frozen=True, slots=True)
class UpdateHarnessRequest:
    """Patch an existing Harness with an UpdateHarness document (see HarnessSpec)."""

    harness_id: str
    document: Mapping[str, object]
    client_token: str

    def __post_init__(self) -> None:
        _require_harness_id(self.harness_id)
        _require_client_token(self.client_token)
        if {"harnessId", "clientToken", "harnessName"} & set(self.document):
            raise ValueError("An update document carries neither identity nor client token.")


@dataclass(frozen=True, slots=True)
class ReplaceTagsRequest:
    """Make a Harness's tags exactly this set."""

    arn: str
    tags: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.arn.strip():
            raise ValueError("A Harness ARN is required.")


@runtime_checkable
class HarnessControlPort(Protocol):
    """Mutate and observe one managed Harness through its control plane."""

    def create(self, request: CreateHarnessRequest) -> HarnessState:
        """Start creating a Harness and return its initial state."""
        ...

    def update(self, request: UpdateHarnessRequest) -> HarnessState:
        """Start patching a Harness and return its initial state."""
        ...

    def get(self, harness_id: str) -> HarnessState:
        """Return a Harness's current state."""
        ...

    def delete(self, harness_id: str) -> None:
        """Start deleting a Harness. Never deletes memory: this resource owns none."""
        ...

    def replace_tags(self, request: ReplaceTagsRequest) -> None:
        """Add or overwrite the desired tags and remove every other tag."""
        ...
