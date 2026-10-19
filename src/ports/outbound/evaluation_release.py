# SPDX-License-Identifier: MIT-0
"""Read-only access to each evaluation family's release record (ADR-079)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.evaluation.release import EvaluationReleaseV1


@runtime_checkable
class EvaluationReleaseReaderPort(Protocol):
    """Read every configured family's release record once per dispatcher run."""

    def read(self) -> tuple[EvaluationReleaseV1 | None, ...]:
        """Return one entry per configured family, `None` where no record exists.

        A record that exists but cannot be read or decoded raises, so the caller sends nothing
        that run rather than treating an unreadable record as a decision.
        """
        ...
