# SPDX-License-Identifier: MIT-0
"""Outbound boundary for one incrementally observed managed Harness invocation."""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol, runtime_checkable

from domain.conversation.managed import HarnessTurnV2, ManagedHarnessEventV1


@runtime_checkable
class ManagedHarnessPort(Protocol):
    """Stream the answer text, tool progress, and the end of one Harness turn."""

    def stream(self, turn: HarnessTurnV2) -> Generator[ManagedHarnessEventV1]:
        """Yield text deltas and tool projections as they arrive, then the turn-ended event.

        A generator, so the caller can close it when its own consumer goes away and the
        underlying service stream is released at once rather than at garbage collection.
        """
        ...
