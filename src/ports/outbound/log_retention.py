# SPDX-License-Identifier: MIT-0
"""Log-group retention port: make one named group exist with one retention, and prove it.

Used by the Harness custom resource for the log group AgentCore writes for the Harness runtime,
which no template owns (infra-composition-stacks). Raises `DependencyError` for any refusal, or
when the group does not read back with the requested retention, carrying the provider diagnostic
in its context and never an SDK exception type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["LogRetentionPort", "RetainLogGroupRequest"]


@dataclass(frozen=True, slots=True)
class RetainLogGroupRequest:
    """One log group, the retention it must have, and the tags it must carry."""

    log_group_name: str
    retention_days: int
    tags: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if not self.log_group_name.startswith("/"):
            raise ValueError("A log group name is an absolute path.")
        if self.retention_days < 1:
            raise ValueError("Retention is a positive number of days.")


@runtime_checkable
class LogRetentionPort(Protocol):
    """Create the group when it is absent, set its retention and tags, then read it back."""

    def retain(self, request: RetainLogGroupRequest) -> None:
        """Ensure the group exists and reads back with exactly the requested retention."""
        ...
