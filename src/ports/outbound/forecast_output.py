# SPDX-License-Identifier: MIT-0
"""The job's candidate-output port (design §4b outputs; adapted from the source engine's port).

Callers may rely on: the series file is written before the run report, and the run report names
the series file's SHA-256, so a reader that finds the report finds a complete candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from domain.forecasting.boundary import CandidateSeriesV1, RunReportV1


@dataclass(frozen=True)
class WrittenCandidate:
    """What the adapter wrote: the series file's digest and record count."""

    series_sha256: str
    records: int


class ForecastOutputPort(Protocol):
    """Writes one shard attempt's candidate."""

    def write_series(self, records: Sequence[CandidateSeriesV1]) -> WrittenCandidate:
        """Write every candidate record; return the file's digest."""
        ...

    def write_report(self, report: RunReportV1) -> None:
        """Write the run report, after the series."""
        ...
