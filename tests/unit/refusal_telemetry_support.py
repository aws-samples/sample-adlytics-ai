# SPDX-License-Identifier: MIT-0
"""Capturing the one refusal emitter's span, line and datapoint (ADR-142).

`capture` replaces `page_bake`'s span and `report_operation` with recorders, so a test reads
exactly what the one emission site produced: each `bake.isolated_refusal` span (its attributes
and whether it was failed), each `isolated_refusal` and `over_budget` line, and, through the
metric callables a test injects, each datapoint. Every identity is fictional.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.jobs.preset_artifact_publisher import page_bake
from adapters.outbound.telemetry.reporting import OperationLog


class _QuietLogger:
    """A logger stand-in: the emitter's `report_operation` is replaced; others log nowhere."""

    def __getattr__(self, name: str) -> Any:
        del name
        return _ignore


def _ignore(*_args: object, **_keywords: object) -> None:
    return None


LOGGER: Any = _QuietLogger()
"""Pass it where a component logs only when it has a logger."""


@dataclass
class Telemetry:
    """What the emitter produced: spans, lines and datapoints, in order."""

    spans: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    lines: list[OperationLog] = field(default_factory=list[OperationLog])
    datapoints: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    over_budget: list[int] = field(default_factory=list[int])

    def metric(self, refusal_class: str, count: int) -> None:
        """Record one refusal datapoint (the injected `emit_isolated_refusal`)."""
        self.datapoints.append((refusal_class, count))

    def budget(self) -> None:
        """Record one over-budget datapoint (the injected `emit_task_over_budget`)."""
        self.over_budget.append(1)

    def refusal_lines(self) -> list[OperationLog]:
        """Return the `isolated_refusal` lines."""
        return [line for line in self.lines if line.phase == "isolated_refusal"]

    def budget_lines(self) -> list[OperationLog]:
        """Return the `over_budget` lines."""
        return [line for line in self.lines if line.outcome == "over_budget"]

    def triples(self) -> list[tuple[str, str, int]]:
        """Each emitted group as (scope, reason, count), read from its span."""
        return [
            (str(item["scope"]), str(item["reason"]), int(str(item["count"])))
            for item in self.spans
        ]


def capture(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    """Replace the emitter's span and line sinks; return the recorder."""
    telemetry = Telemetry()

    @contextmanager
    def recording_span(name: str, **attributes: object) -> Generator[Any]:
        assert name == "bake.isolated_refusal"
        carried: dict[str, object] = dict(attributes)
        telemetry.spans.append(carried)

        def record(**more: object) -> None:
            carried.update({key: value for key, value in more.items() if value is not None})

        def note_failure(error: BaseException) -> None:
            carried["failed"] = type(error).__name__

        yield SimpleNamespace(record=record, note_failure=note_failure)

    def recording_line(event: OperationLog, _logger: object) -> None:
        telemetry.lines.append(event)

    monkeypatch.setattr(page_bake, "span", recording_span)
    monkeypatch.setattr(page_bake, "report_operation", recording_line)
    return telemetry


__all__ = ["LOGGER", "Telemetry", "capture"]
