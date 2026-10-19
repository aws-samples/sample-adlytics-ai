# SPDX-License-Identifier: MIT-0
"""P5's pure verdicts: the stuck-flow `ALARM -> OK` at the destination and the expected alarms."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_ingestion_traceability_p5 as p5  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

TOPIC = "arn:aws:sns:us-east-1:111122223333:adlytics-test-ingestion-alarms"
T0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)


def _state(at: int, value: str) -> dict[str, object]:
    return {
        "Timestamp": T0 + timedelta(seconds=at),
        "HistoryItemType": "StateUpdate",
        "HistoryData": json.dumps({"newState": {"stateValue": value}}),
    }


def _action(at: int, ok: bool = True, topic: str = TOPIC) -> dict[str, object]:
    word = "Successfully executed action" if ok else "Failed to execute action"
    return {
        "Timestamp": T0 + timedelta(seconds=at),
        "HistoryItemType": "Action",
        "HistorySummary": f"{word} {topic}",
    }


def test_alarm_then_ok_with_both_actions_executed_passes() -> None:
    history = [_state(10, "ALARM"), _action(11), _state(90, "OK"), _action(91)]
    assert p5.alarm_transition_verdict(history, TOPIC, T0)["passed"] is True


@pytest.mark.parametrize(
    "history",
    [
        [_state(10, "ALARM"), _action(11)],
        [_state(10, "OK"), _action(11), _state(20, "ALARM"), _action(21)],
        [_state(10, "ALARM"), _action(11, ok=False), _state(90, "OK"), _action(91)],
        [_state(10, "ALARM"), _action(11, topic=TOPIC + "-x"), _state(90, "OK"), _action(91)],
        [_state(-30, "ALARM"), _action(-29), _state(90, "OK"), _action(91)],
    ],
)
def test_a_missing_failed_misdirected_or_earlier_transition_fails(
    history: list[dict[str, object]],
) -> None:
    assert p5.alarm_transition_verdict(history, TOPIC, T0)["passed"] is False


def test_only_the_probe_stuck_flow_alarm_may_fire() -> None:
    transitions = {
        "adlytics-test-open-flows-past-ceiling-probe": ["ALARM", "OK"],
        "adlytics-test-semantic-write-oldest": ["OK"],
    }
    assert p5.unexpected_alarms(transitions, "test", p5.EXPECTED_ALARMS) == []
    transitions["adlytics-test-open-flows-past-ceiling"] = ["ALARM"]
    assert p5.unexpected_alarms(transitions, "test", p5.EXPECTED_ALARMS) == [
        "adlytics-test-open-flows-past-ceiling"
    ]
