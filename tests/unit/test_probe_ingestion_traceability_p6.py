# SPDX-License-Identifier: MIT-0
"""P6 part 1's pure verdicts: the shared shadow binding and the shortest legal drain deadline."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_ingestion_traceability_p6 as p6  # noqa: E402

from domain.operations.cutover import drain_deadline_bounds, require_drain_deadline  # noqa: E402

pytestmark = pytest.mark.unit


def _receipt(request_id: str, decision: str, caller: str, **extra: object) -> dict[str, object]:
    return {
        "request_id": request_id,
        "decision": decision,
        "caller_kind": caller,
        "authority_epoch": 0,
        "fingerprint": "f",
        "graph_id": "g",
        **extra,
    }


def test_the_twin_and_the_delivery_event_share_one_shadow_graph() -> None:
    twin = _receipt("twin-1", "ADMITTED", "on_demand")
    delivery = _receipt("delivery-1", "JOINED", "delivery_event")
    assert p6.shadow_binding_verdict(twin, delivery)["passed"] is True


@pytest.mark.parametrize(
    "delivery",
    [
        None,
        _receipt("twin-1", "JOINED", "delivery_event"),
        _receipt("delivery-1", "JOINED", "delivery_event", graph_id="other"),
        _receipt("delivery-1", "JOINED", "delivery_event", authority_epoch=1),
        _receipt("delivery-1", "ADMITTED", "delivery_event"),
    ],
)
def test_any_other_pair_fails(delivery: dict[str, object] | None) -> None:
    twin = _receipt("twin-1", "ADMITTED", "on_demand")
    assert p6.shadow_binding_verdict(twin, delivery)["passed"] is False


def test_the_rehearsal_deadline_is_legal_and_short() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    deadline = p6.drain_deadline(now)
    require_drain_deadline(now + timedelta(seconds=30), deadline)
    earliest, _ = drain_deadline_bounds(now)
    assert deadline - earliest == timedelta(seconds=p6.DRAIN_BUFFER_SECONDS)


def test_the_plan_read_waits_for_the_reconciler_to_persist_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[dict[str, object]] = [
        {"status": None},
        {"status": "claimed"},
        {"status": "planned"},
    ]
    reads = iter(states)

    def plan_state(*_: object) -> dict[str, object]:
        return next(reads)

    def plan_digest(*_: object) -> str:
        return "d"

    monkeypatch.setattr(p6.gate, "_plan_state", plan_state)
    monkeypatch.setattr(p6.gate, "_plan_digest", plan_digest)
    monkeypatch.setattr(p6.time, "sleep", _no_sleep)
    assert p6.settled_plan(object(), "dev", "k", "v", 60)["status"] == "planned"


def _no_sleep(_: float) -> None:
    return None


def test_b0_finish_waits_past_planned_for_a_terminal_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The units still run after the handler's terminal record, so `planned` is not settled."""
    states: list[dict[str, object]] = [
        {"status": "planned"},
        {"status": "planned"},
        {"status": "completed"},
    ]
    reads = iter(states)

    def plan_state(*_: object) -> dict[str, object]:
        return next(reads)

    def plan_digest(*_: object) -> str:
        return "d"

    monkeypatch.setattr(p6.gate, "_plan_state", plan_state)
    monkeypatch.setattr(p6.gate, "_plan_digest", plan_digest)
    monkeypatch.setattr(p6.time, "sleep", _no_sleep)
    finished = p6._FINISHED  # pyright: ignore[reportPrivateUsage]
    assert p6.settled_plan(object(), "dev", "k", "v", 60, finished)["status"] == "completed"


def test_the_expected_drain_alarms_name_the_deployed_alarms_not_the_retired_guesses() -> None:
    """S6-1: the synthesized names; `infra/test/p6-expected-alarms.test.ts` checks the synth."""
    assert "ingestion-oldest" in p6.EXPECTED_DRAIN_ALARMS
    assert "ingestion-dead-lettered" in p6.EXPECTED_DRAIN_ALARMS
    assert not {"ingestion-queue-oldest-message-age", "ingestion-dlq-messages"} & set(
        p6.EXPECTED_DRAIN_ALARMS
    )


@pytest.mark.parametrize(
    ("plan", "passed"),
    [
        ({"status": "completed", "unit_count": 3, "attested_count": 3, "failed_count": 0}, True),
        ({"status": "planned", "unit_count": 3, "attested_count": 3, "failed_count": 0}, False),
        ({"status": "completed", "unit_count": 3, "attested_count": 2, "failed_count": 0}, False),
        ({"status": "completed", "unit_count": 3, "attested_count": 3, "failed_count": 1}, False),
        ({"status": "completed", "unit_count": 0, "attested_count": 0}, False),
    ],
)
def test_the_legacy_plan_must_complete_with_every_unit_attested(
    plan: dict[str, object], passed: bool
) -> None:
    assert p6.plan_completed(plan) is passed


def test_only_terminal_records_at_or_after_d_plus_the_margin_count() -> None:
    visible = datetime(2026, 10, 9, 2, 48, 30, tzinfo=UTC)
    records = [
        {"phase": "terminal", "@timestamp": "2026-10-09 02:48:29.000", "receive_count": "1"},
        {"phase": "drain_wait", "@timestamp": "2026-10-09 02:49:00.000"},
        {"phase": "terminal", "@timestamp": "2026-10-09 02:48:36.000", "receive_count": "2"},
    ]
    assert [row["receive_count"] for row in p6.terminals_after(records, visible)] == ["2"]


def test_any_graph_execution_family_or_epoch_1_item_fails_the_epoch_0_check() -> None:
    epoch_0 = {"COMMAND": 3, "GRAPH": 4, "MODE": 1, "RECEIPT": 16}
    assert p6.epoch_1_verdict(epoch_0, [])["passed"] is True
    assert p6.epoch_1_verdict({**epoch_0, "INSTANTIATE": 1}, [])["passed"] is False
    assert p6.epoch_1_verdict({**epoch_0, "RUN": 1}, [])["passed"] is False
    assert p6.epoch_1_verdict(epoch_0, ["RECEIPT#x/RECEIPT"])["passed"] is False


def test_the_clock_skew_is_host_minus_the_aws_date_header() -> None:
    host = datetime(2026, 10, 9, 4, 0, 3, tzinfo=UTC)
    assert p6.clock_skew_seconds(host, "Fri, 09 Oct 2026 04:00:00 GMT") == 3.0
    assert p6.clock_skew_seconds(host, "Fri, 09 Oct 2026 04:00:20 GMT") == -17.0
    assert int(p6.CUTOVER["drain_fence_margin_seconds"]) > p6.CLOCK_SKEW_BUDGET_SECONDS


def test_any_message_on_a_work_queue_is_in_flight() -> None:
    assert p6.in_flight_verdict({"q": 0, "f": 0})["passed"] is True
    busy = p6.in_flight_verdict({"q": 0, "f": 2})
    assert busy == {"busy": {"f": 2}, "passed": False}


def test_the_handler_read_repeats_until_the_phase_is_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[list[dict[str, str]]] = [
        [],
        [{"phase": "admission"}],
        [{"phase": "admission"}, {"phase": "terminal"}],
    ]

    def query(*_: object) -> list[dict[str, str]]:
        return reads.pop(0)

    monkeypatch.setattr(p6, "_query_handler_records", query)
    monkeypatch.setattr(p6.time, "sleep", _no_sleep)
    records = p6.handler_records(object(), "dev", "k", datetime.now(UTC), "terminal")
    assert [row["phase"] for row in records] == ["admission", "terminal"]


# --- The drain window's alarm rule (owner-gate Item 21) -------------------------------------------

_START = datetime(2026, 10, 9, 6, 5, tzinfo=UTC)
_END = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
_PLAN = "WRITE_PLAN#a#b"
_EXPECTED = frozenset({"adlytics-dev-ingestion-oldest"})
_CONDITIONAL = frozenset({"adlytics-dev-ingestion-alarming-errors-semantic-table-writer"})
_RACE_ALARM = "adlytics-dev-ingestion-alarming-errors-semantic-table-writer"


def _race_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "correlation_id": _PLAN,
        "error_code": "ADLY-UNEXPECTED-0000",
        "error_class": "invariant",
        "error_type": "ValueError",
        "stack": [
            {"filename": "table_writer.py", "function": "_finalize_unit", "line": 1651},
            {"filename": "projection_coordinator.py", "function": "_require_settled_history"},
        ],
    }
    return record | changes


def _fired(at: datetime) -> list[tuple[datetime, str, str]]:
    return [
        (at, _RACE_ALARM, "Alarm updated from OK to ALARM"),
        (at + timedelta(minutes=15), _RACE_ALARM, "Alarm updated from ALARM to OK"),
    ]


def _completed_at(at: datetime) -> dict[str, object]:
    return {"status": "completed", "terminal_at": f"{int(at.timestamp() * 1_000_000):020d}"}


def _verdict(
    records: list[dict[str, object]], plans: dict[str, dict[str, object]]
) -> dict[str, object]:
    window = p6.AlarmWindow(_START, _END, _fired(_START + timedelta(minutes=21)), records, plans)
    return p6.alarm_window_verdict(window, _EXPECTED, _CONDITIONAL)


def test_the_race_alarm_is_expected_when_signed_and_its_plan_completes_in_window() -> None:
    verdict = _verdict([_race_record()], {_PLAN: _completed_at(_START + timedelta(minutes=52))})
    assert verdict["passed"] is True
    conditional = cast("dict[str, object]", verdict["conditional"])
    assert conditional["classification"] == "known legacy defect, retires with legacy"


@pytest.mark.parametrize(
    ("records", "plans"),
    [
        # (a) fails: another invariant from another frame, or another code.
        ([_race_record(), _race_record(stack=[])], {_PLAN: _completed_at(_END)}),
        ([_race_record(error_code="ADLY-PLAN_COORD-0003")], {_PLAN: _completed_at(_END)}),
        # (a) fails: the alarm fired with no record to explain it.
        ([], {}),
        # (b) fails: the plan is still planned, or completed after the window.
        ([_race_record()], {_PLAN: {"status": "planned", "terminal_at": None}}),
        ([_race_record()], {_PLAN: _completed_at(_END + timedelta(seconds=1))}),
    ],
)
def test_the_race_alarm_stops_the_window_otherwise(
    records: list[dict[str, object]], plans: dict[str, dict[str, object]]
) -> None:
    assert _verdict(records, plans)["passed"] is False


def test_an_unlisted_alarm_or_one_that_never_recovers_stops_the_window() -> None:
    other = [(_START, "adlytics-dev-ingestion-blocked", "Alarm updated from OK to ALARM")]
    window = p6.AlarmWindow(_START, _END, other, [], {})
    assert p6.alarm_window_verdict(window, _EXPECTED, _CONDITIONAL)["unexpected"] == [
        "adlytics-dev-ingestion-blocked"
    ]
    stuck = [(_START, "adlytics-dev-ingestion-oldest", "Alarm updated from OK to ALARM")]
    window = p6.AlarmWindow(_START, _END, stuck, [], {})
    verdict = p6.alarm_window_verdict(window, _EXPECTED, _CONDITIONAL)
    assert verdict["not_recovered"] == ["adlytics-dev-ingestion-oldest"]
    assert verdict["passed"] is False


def test_a_quiet_window_passes() -> None:
    window = p6.AlarmWindow(_START, _END, [], [], {})
    assert p6.alarm_window_verdict(window, _EXPECTED, _CONDITIONAL)["passed"] is True
