# SPDX-License-Identifier: MIT-0
"""ADR-139 layer 4: the presentation grant-serving refusal count and its closed dimensions.

Every identity is fictional.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.outbound.telemetry import reporting
from adapters.outbound.telemetry.reporting import report_serving_refusal

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-139")]


def _capture(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, float, str, list[tuple[str, str]]]]:
    emitted: list[tuple[str, float, str, list[tuple[str, str]]]] = []

    @contextmanager
    def single_metric(**kwargs: Any) -> Generator[Any]:
        dimensions: list[tuple[str, str]] = []

        def add_dimension(name: str, value: str) -> None:
            dimensions.append((name, value))

        yield SimpleNamespace(add_dimension=add_dimension)
        emitted.append((kwargs["name"], kwargs["value"], kwargs["namespace"], dimensions))

    monkeypatch.setattr(reporting, "single_metric", single_metric)
    return emitted


@pytest.mark.parametrize("reason", ["ungranted_generation", "serving_refresh_pending"])
@pytest.mark.parametrize(
    ("past_settle", "settle"), [(True, "past_settle"), (False, "within_settle")]
)
def test_a_refusal_emits_one_count_with_exactly_reason_and_settle(  # noqa: PLR0913, PLR0917 -- fixture and cases
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    past_settle: bool,
    settle: str,
) -> None:
    emitted = _capture(monkeypatch)

    report_serving_refusal(reason, past_settle=past_settle)

    assert emitted == [
        ("PresentationServingRefused", 1, "Adlytics", [("Reason", reason), ("Settle", settle)])
    ]


@pytest.mark.parametrize("reason", ["", "other", "UNGRANTED_GENERATION", "binding_incomplete"])
def test_a_reason_outside_the_closed_set_is_refused_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    emitted = _capture(monkeypatch)

    with pytest.raises(ValueError, match="closed values"):
        report_serving_refusal(reason, past_settle=True)

    assert emitted == []
