# SPDX-License-Identifier: MIT-0
"""The §2.5 uninterrupted-quiet drain timer, as a pure state machine over observations.

The timer starts only at a strongly consistent all-clear read, resets to zero on any nonquiet
evidence or incomplete/failed read, denies a passing report before the interval has elapsed, and
permits one at exactly the interval only after a final all-clear read. Every reset names the
evidence class that caused it so the report can surface it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = ["RESET_REASONS", "DrainObservation", "DrainTimer"]

RESET_REASONS: Final = (
    "queue",
    "dlq",
    "execution",
    "plan",
    "attempt",
    "pending_fence",
    "blocked_fence",
    "incomplete_read",
    "failed_read",
)


@dataclass(frozen=True, slots=True)
class DrainObservation:
    """One strongly consistent read: either all-clear, or the exact reset reasons it carried."""

    at_seconds: float
    reset_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = [reason for reason in self.reset_reasons if reason not in RESET_REASONS]
        if unknown:
            raise ValueError("a drain reset reason is outside the closed vocabulary.")

    @property
    def quiet(self) -> bool:
        """Whether this read carried no nonquiet evidence."""
        return not self.reset_reasons


@dataclass(slots=True)
class DrainTimer:
    """Track the uninterrupted-quiet window from observations only; no wall clock inside."""

    interval_seconds: int
    quiet_since: float | None = None
    last_observation: DrainObservation | None = None
    resets: list[dict[str, object]] = field(default_factory=lambda: [])

    def __post_init__(self) -> None:
        if isinstance(self.interval_seconds, bool) or self.interval_seconds <= 0:
            raise ValueError("the drain observation interval must be a positive integer.")

    def observe(self, observation: DrainObservation) -> None:
        """Advance the timer with one read: start on the first all-clear, reset on nonquiet."""
        if self.last_observation is not None and observation.at_seconds < (
            self.last_observation.at_seconds
        ):
            raise ValueError("drain observations must be monotonic in time.")
        self.last_observation = observation
        if observation.quiet:
            if self.quiet_since is None:
                self.quiet_since = observation.at_seconds
            return
        self.resets.append(
            {"at_seconds": observation.at_seconds, "reasons": list(observation.reset_reasons)}
        )
        self.quiet_since = None

    @property
    def elapsed_quiet_seconds(self) -> float:
        """Seconds of uninterrupted quiet through the last observation, or zero."""
        if self.quiet_since is None or self.last_observation is None:
            return 0.0
        return self.last_observation.at_seconds - self.quiet_since

    @property
    def passing(self) -> bool:
        """Permit only at or after the interval, and only when the last read was all-clear."""
        return (
            self.last_observation is not None
            and self.last_observation.quiet
            and self.elapsed_quiet_seconds >= self.interval_seconds
        )

    def document(self) -> dict[str, object]:
        """Render the timer facts for the report."""
        return {
            "interval_seconds": self.interval_seconds,
            "elapsed_quiet_seconds": self.elapsed_quiet_seconds,
            "resets": list(self.resets),
            "passing": self.passing,
        }
