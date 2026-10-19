# SPDX-License-Identifier: MIT-0
"""validation/dev_operations.py's pure classifiers and local reads, over plain dict inputs.

No AWS: each classifier takes the shape the I/O layer reads. Every stop is paired with the clean
input it departs from. The alarm-window rows copy the shape of the first traceability S5 window
(four transitions into ALARM off the expected list, and the expected probe alarm going to ALARM
and back), with fictional alarm names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

import pytest
from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from validation import dev_operations as ops  # noqa: E402

from domain.operations.execution_graph_release import (  # noqa: E402
    BindingKind,
    ServingBinding,
    serving_generation,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-136")]

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
PROBE = "adlytics-test-open-flows-past-ceiling-probe"


def _entries(report: dict[str, object], key: str) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", report[key])


def _row(at: str, alarm: str, change: str) -> dict[str, str]:
    return {
        "timestamp": f"2026-01-01T{at}+00:00",
        "alarm": alarm,
        "summary": f"Alarm updated from {change}",
    }


_WINDOW = [
    _row("10:01:00", PROBE, "OK to ALARM"),
    _row("10:03:00", "adlytics-test-ingestion-blocked", "OK to ALARM"),
    _row("10:04:00", "adlytics-test-ingestion-alarming-errors-ingest-delivery", "OK to ALARM"),
    _row("10:06:00", PROBE, "ALARM to OK"),
    _row(
        "10:09:00", "adlytics-test-ingestion-alarming-errors-semantic-table-writer", "OK to ALARM"
    ),
    _row("10:12:00", "adlytics-test-ingestion-oldest", "OK to ALARM"),
    _row("10:40:00", "adlytics-test-ingestion-alarming-errors-ingest-delivery", "ALARM to OK"),
]
_OFF_LIST = frozenset(
    {
        "adlytics-test-ingestion-blocked",
        "adlytics-test-ingestion-alarming-errors-ingest-delivery",
        "adlytics-test-ingestion-alarming-errors-semantic-table-writer",
        "adlytics-test-ingestion-oldest",
    }
)


# --- alarm window -------------------------------------------------------------------------------


def _now(name: str, state: str = "OK", entered: str = "09:00:00") -> dict[str, str]:
    """One prefixed alarm's current state, as `_alarms` reads it."""
    return {"name": name, "state": state, "transitioned": f"2026-01-01T{entered}+00:00"}


_ALL_OK = [_now(name) for name in sorted(_OFF_LIST | {PROBE})]


def test_an_unexpected_transition_into_alarm_stops_with_3_and_names_every_one() -> None:
    code, report = ops.classify_alarm_window(_WINDOW, _ALL_OK, frozenset({PROBE}), 15, NOW)

    assert code == ops.UNEXPECTED_ALARM == 3
    assert report["unexpected_alarm"] == sorted(_OFF_LIST)


def test_the_same_window_with_every_alarm_expected_and_recovered_is_clean() -> None:
    recovered = [
        *_WINDOW,
        _row("10:20:00", "adlytics-test-ingestion-blocked", "ALARM to OK"),
        _row(
            "10:21:00",
            "adlytics-test-ingestion-alarming-errors-semantic-table-writer",
            "ALARM to OK",
        ),
        _row("10:22:00", "adlytics-test-ingestion-oldest", "ALARM to OK"),
    ]

    code, report = ops.classify_alarm_window(recovered, _ALL_OK, _OFF_LIST | {PROBE}, 15, NOW)

    assert code == ops.OK
    assert report["unexpected_alarm"] == []
    assert report["not_recovered"] == []


def test_an_expected_alarm_still_in_alarm_past_the_recovery_window_stops_with_4() -> None:
    stuck = [_row("10:01:00", PROBE, "OK to ALARM")]
    alarms = [_now(PROBE, "ALARM", "10:01:00")]

    late = ops.classify_alarm_window(stuck, alarms, frozenset({PROBE}), 15, NOW)
    within = ops.classify_alarm_window(
        stuck, alarms, frozenset({PROBE}), 15, datetime(2026, 1, 1, 10, 10, tzinfo=UTC)
    )

    assert late[0] == ops.EXPECTED_ALARM_NOT_RECOVERED == 4
    assert late[1]["not_recovered"] == [PROBE]
    assert within[0] == ops.OK


@pytest.mark.parametrize(
    ("now", "code"),
    [
        # In ALARM since 09:00, before a window that opened at 09:05: past the 15-minute bound.
        (NOW, ops.EXPECTED_ALARM_NOT_RECOVERED),
        # The same alarm, read at 09:10, still within the bound of when it entered ALARM.
        (datetime(2026, 1, 1, 9, 10, tzinfo=UTC), ops.OK),
    ],
)
def test_an_expected_alarm_already_in_alarm_before_the_window_is_held_to_the_bound(
    now: datetime, code: int
) -> None:
    # No row in the window: the alarm entered ALARM before SINCE and has not left it.
    alarms = [_now(PROBE, "ALARM", "09:00:00")]

    got, report = ops.classify_alarm_window([], alarms, frozenset({PROBE}), 15, now)

    assert got == code
    assert report["not_recovered"] == ([PROBE] if code else [])
    assert report["expected_now"] == [
        {"alarm": PROBE, "state": "ALARM", "entered": "2026-01-01T09:00:00Z"}
    ]


def test_an_expected_alarm_in_alarm_before_the_window_and_recovered_in_it_is_clean() -> None:
    # Paired control: the same pre-window ALARM, with a recorded recovery in the window.
    recovered = [_row("10:05:00", PROBE, "ALARM to OK")]

    code, report = ops.classify_alarm_window(
        recovered, [_now(PROBE, "OK", "10:05:00")], frozenset({PROBE}), 15, NOW
    )

    assert code == ops.OK
    assert report["not_recovered"] == []


@pytest.mark.parametrize(
    ("state", "code"),
    [
        # OK now: recovered, though history (lagging, or bounded before the recovery) shows none.
        ("OK", ops.OK),
        # Paired control: the same history with the alarm in ALARM now, since 10:01, is not.
        ("ALARM", ops.EXPECTED_ALARM_NOT_RECOVERED),
    ],
)
def test_the_current_state_not_a_stale_last_row_decides_recovery(state: str, code: int) -> None:
    stale = [_row("10:01:00", PROBE, "OK to ALARM")]

    got, report = ops.classify_alarm_window(
        stale, [_now(PROBE, state, "10:01:00")], frozenset({PROBE}), 15, NOW
    )

    assert got == code
    assert report["not_recovered"] == ([PROBE] if code else [])


@pytest.mark.parametrize(
    ("state", "code"),
    [
        # In ALARM since 09:00, before the window, and not expected: no row, still a stop.
        ("ALARM", ops.UNEXPECTED_ALARM),
        # Paired control: the same alarm, with no row, in a state alarm-window does not stop on.
        ("INSUFFICIENT_DATA", ops.OK),
    ],
)
def test_an_unexpected_alarm_already_in_alarm_before_the_window_stops_with_3(
    state: str, code: int
) -> None:
    name = "adlytics-test-evaluation-worst-label"
    alarms = [*_ALL_OK, _now(name, state, "09:00:00")]

    got, report = ops.classify_alarm_window([], alarms, frozenset(), 15, NOW)

    assert got == code
    assert report["transitions"] == []
    if code:
        assert report["unexpected_alarm"] == [name]
        assert report["standing_unexpected"] == [
            {"alarm": name, "state": "ALARM", "transitioned": "2026-01-01T09:00:00+00:00"}
        ]
    else:
        assert report["unexpected_alarm"] == []
        assert report["standing_unexpected"] == []


def test_an_expected_alarm_in_alarm_without_its_transition_time_is_a_read_failure() -> None:
    alarms = [{"name": PROBE, "state": "ALARM", "transitioned": ""}]

    with pytest.raises(ops.OperationsError) as caught:
        ops.classify_alarm_window([], alarms, frozenset({PROBE}), 15, NOW)

    assert caught.value.name == "READ_FAILED"


def test_an_expected_alarm_the_prefix_does_not_list_is_refused() -> None:
    with pytest.raises(ops.OperationsError, match="the prefix does not list"):
        ops.classify_alarm_window([], _ALL_OK, frozenset({"adlytics-test-unlisted"}), 15, NOW)


def test_parse_expected_reads_none_as_the_empty_set_and_refuses_an_empty_list() -> None:
    assert ops.parse_expected("none") == frozenset()
    assert ops.parse_expected("a, b") == frozenset({"a", "b"})
    with pytest.raises(ops.OperationsError):
        ops.parse_expected(" , ")


# --- state --------------------------------------------------------------------------------------

_DLQ: dict[str, object] = {
    "name": "adlytics-test-ingestion-dlq",
    "stack": "Adlytics-Ingestion-test",
    "visible": 1,
    "in_flight": 0,
    "delayed": 0,
}
_WORK: dict[str, object] = {
    "name": "adlytics-test-ingestion",
    "stack": "Adlytics-Ingestion-test",
    "visible": 40,
    "in_flight": 3,
    "delayed": 0,
}
_STACKS = [{"name": "Adlytics-Ingestion-test", "status": "UPDATE_COMPLETE"}]
_NO_LOCK: dict[str, object] = {"path": "/custody/deploy.lock", "present": False}


def test_a_standing_dlq_message_with_no_disposition_is_undisposed_state() -> None:
    code, report = ops.classify_state([], [_DLQ, _WORK], _STACKS, _NO_LOCK, {})

    assert code == ops.UNDISPOSED_STATE == 3
    assert [entry["name"] for entry in _entries(report, "undisposed")] == [
        "adlytics-test-ingestion-dlq"
    ]


def _disposed(*states: str) -> ops.Disposition:
    return ops.Disposition(states, "A fictional known baseline.")


def test_the_same_dlq_message_with_a_disposition_is_clean() -> None:
    sentence = "A pre-existing refusal, kept as evidence of the fictional malformed delivery."
    disposition = ops.Disposition(("visible=1 in_flight=0 delayed=0",), sentence)

    code, report = ops.classify_state(
        [], [_DLQ, _WORK], _STACKS, _NO_LOCK, {"adlytics-test-ingestion-dlq": disposition}
    )

    assert code == ops.OK
    assert report["undisposed"] == []
    assert report["disposed"] == [
        {
            "kind": "failure_queue",
            **_DLQ,
            "disposition": sentence,
            "allowed_states": ["visible=1 in_flight=0 delayed=0"],
        }
    ]
    # A busy work queue is reported, never a stop.
    assert report["work_queues"] == [_WORK]


def test_a_dlq_disposition_for_one_message_refuses_a_second() -> None:
    grown = {**_DLQ, "visible": 2}
    dispositions = {"adlytics-test-ingestion-dlq": _disposed("visible=1 in_flight=0 delayed=0")}

    code, report = ops.classify_state([], [grown], _STACKS, _NO_LOCK, dispositions)

    assert code == ops.UNDISPOSED_STATE
    assert report["disposed"] == []
    [entry] = _entries(report, "undisposed")
    assert entry["name"] == "adlytics-test-ingestion-dlq"
    assert entry["refused_disposition"] == {
        "disposition": "A fictional known baseline.",
        "allowed_states": ["visible=1 in_flight=0 delayed=0"],
    }


def test_an_insufficient_data_only_disposition_refuses_alarm_and_passes_its_own_state() -> None:
    name = "adlytics-test-evaluation-worst-label"
    dispositions = {name: _disposed("INSUFFICIENT_DATA")}
    in_alarm = [{"name": name, "state": "ALARM", "updated": "t1"}]
    insufficient = [{"name": name, "state": "INSUFFICIENT_DATA", "updated": "t1"}]

    alarm_code, alarm_report = ops.classify_state(in_alarm, [], _STACKS, _NO_LOCK, dispositions)
    allowed_code, allowed_report = ops.classify_state(
        insufficient, [], _STACKS, _NO_LOCK, dispositions
    )

    assert alarm_code == ops.UNDISPOSED_STATE
    assert alarm_report["disposed"] == []
    [entry] = _entries(alarm_report, "undisposed")
    assert (entry["name"], entry["state"]) == (name, "ALARM")
    assert entry["refused_disposition"] == {
        "disposition": "A fictional known baseline.",
        "allowed_states": ["INSUFFICIENT_DATA"],
    }
    # A refused disposition is not reported as unused: its name matched, its state did not.
    assert alarm_report["unused_dispositions"] == []
    assert allowed_code == ops.OK
    assert [entry["name"] for entry in _entries(allowed_report, "disposed")] == [name]


def test_an_alarm_not_ok_needs_a_disposition_and_an_unused_one_is_reported() -> None:
    alarms = [
        {"name": "adlytics-test-ingestion-blocked", "state": "ALARM", "updated": "t1"},
        {
            "name": "adlytics-test-new-function-errors",
            "state": "INSUFFICIENT_DATA",
            "updated": "t2",
        },
        {"name": "adlytics-test-ingestion-oldest", "state": "OK", "updated": "t3"},
    ]

    undisposed = ops.classify_state(alarms, [], _STACKS, _NO_LOCK, {})
    disposed = ops.classify_state(
        alarms,
        [],
        _STACKS,
        _NO_LOCK,
        {
            "adlytics-test-ingestion-blocked": _disposed("ALARM"),
            "adlytics-test-new-function-errors": _disposed("INSUFFICIENT_DATA"),
            "adlytics-test-retired-alarm": _disposed("ALARM"),
        },
    )

    assert undisposed[0] == 3
    assert [entry["name"] for entry in _entries(undisposed[1], "undisposed")] == [
        "adlytics-test-ingestion-blocked",
        "adlytics-test-new-function-errors",
    ]
    assert disposed[0] == ops.OK
    assert disposed[1]["unused_dispositions"] == ["adlytics-test-retired-alarm"]


def test_a_stale_lock_is_undisposed_with_the_force_release_remedy_and_a_live_one_is_not() -> None:
    stale = {
        "path": "/custody/deploy.lock",
        "present": True,
        "pid": 4242,
        "content": "pid=4242 label=killed",
        "owner_alive": False,
    }
    live = {**stale, "owner_alive": True}

    stale_code, stale_report = ops.classify_state([], [], _STACKS, stale, {})
    live_code, _ = ops.classify_state([], [], _STACKS, live, {})

    assert stale_code == 3
    entry = _entries(stale_report, "undisposed")[0]
    assert entry["kind"] == "stale_lock"
    assert "deploy-lock-force-release" in str(entry["remedy"])
    assert live_code == ops.OK


def test_an_in_progress_stack_is_listed() -> None:
    stacks = [*_STACKS, {"name": "Adlytics-Api-test", "status": "UPDATE_IN_PROGRESS"}]

    _, report = ops.classify_state([], [], stacks, _NO_LOCK, {})

    assert report["stacks_in_progress"] == ["Adlytics-Api-test"]


def test_failure_queues_are_told_from_work_queues_by_their_declared_names() -> None:
    failure = [
        "adlytics-test-ingestion-dlq",
        "adlytics-test-preset-bake-dlq.fifo",
        "adlytics-test-InsightContinuationDlq",
        "adlytics-test-graph-projector-stream-failures",
    ]
    work = [
        "adlytics-test-ingestion",
        "adlytics-test-semantic-write.fifo",
        "adlytics-test-InsightContinuation",
    ]

    assert all(ops.is_failure_queue(name) for name in failure)
    assert not any(ops.is_failure_queue(name) for name in work)


# --- source key ---------------------------------------------------------------------------------


def _readback(keys: list[str]) -> dict[str, object]:
    return {
        "stacks": {
            "Adlytics-Api-test": {"equal": True, "stack_status": "UPDATE_COMPLETE"},
            "Adlytics-Ingestion-test": {"equal": True, "stack_status": "UPDATE_COMPLETE"},
        },
        "one_source_key": len(keys) <= 1,
        "source_keys": keys,
        "passed": len(keys) <= 1,
    }


def test_two_source_keys_stop_with_3_and_one_passes() -> None:
    two = ops.classify_source_key(_readback(["aaa.zip", "bbb.zip"]))
    one = ops.classify_source_key(_readback(["aaa.zip"]))

    assert two[0] == ops.NOT_ONE_SOURCE_KEY == 3
    assert two[1]["source_keys"] == ["aaa.zip", "bbb.zip"]
    assert one[0] == ops.OK


def test_no_source_key_or_an_unequal_stack_also_stops_with_3() -> None:
    unequal = _readback(["aaa.zip"])
    stacks = cast("dict[str, object]", unequal["stacks"])
    stacks["Adlytics-Api-test"] = {"equal": False, "stack_status": "UPDATE_ROLLBACK_COMPLETE"}

    none_code, _ = ops.classify_source_key(_readback([]))
    unequal_code, report = ops.classify_source_key(unequal)

    assert none_code == 3
    assert unequal_code == 3
    assert report["unequal_stacks"] == ["Adlytics-Api-test"]
    assert report["incomplete_stacks"] == ["Adlytics-Api-test"]


# --- work queues --------------------------------------------------------------------------------


def _work_queues(**depth: int) -> list[dict[str, object]]:
    queues: list[dict[str, object]] = [
        {
            "name": f"adlytics-test-{suffix}",
            "stack": "Adlytics-Ingestion-test",
            "visible": depth.get(suffix.split(".")[0].replace("-", "_"), 0),
            "in_flight": 0,
            "delayed": 0,
        }
        for suffix in ops.DAILY_WORK_QUEUES
    ]
    return [*queues, _DLQ]


def test_idle_work_queues_pass_and_one_visible_message_is_work_in_flight() -> None:
    idle = ops.classify_work_queues(_work_queues(), "test")
    busy = ops.classify_work_queues(_work_queues(semantic_write=1), "test")

    assert idle[0] == ops.OK
    assert busy[0] == ops.WORK_IN_FLIGHT == 82
    assert busy[1]["in_flight"] == ["adlytics-test-semantic-write.fifo"]


def test_a_work_queue_the_stacks_do_not_hold_is_a_read_failure_not_idle() -> None:
    missing = [queue for queue in _work_queues() if queue["name"] != "adlytics-test-ingestion"]

    code, report = ops.classify_work_queues(missing, "test")

    assert code == ops.READ_FAILED == 2
    assert report["not_found"] == ["adlytics-test-ingestion (found 0)"]


# --- local reads and the command line -----------------------------------------------------------


def test_read_lock_tells_a_live_owner_from_a_gone_one(tmp_path: Path) -> None:
    # A-SUBPROCESS-001: fixed argv; see docs/security-dispositions.md
    finished = subprocess.Popen(["/usr/bin/true"])
    finished.wait()
    gone = tmp_path / "gone.lock"
    gone.write_text(f"pid={finished.pid} label=killed\n")
    live = tmp_path / "live.lock"
    live.write_text(f"pid={os.getpid()} label=running\n")
    unnamed = tmp_path / "unnamed.lock"
    unnamed.write_text("held\n")

    assert ops.read_lock(tmp_path / "absent.lock")["present"] is False
    assert ops.read_lock(gone)["owner_alive"] is False
    assert ops.read_lock(live)["owner_alive"] is True
    assert ops.read_lock(unnamed)["owner_alive"] is False


def test_dispositions_must_list_allowed_states_and_a_reason(tmp_path: Path) -> None:
    name = "adlytics-test-ingestion-dlq"
    states = ["visible=1 in_flight=0 delayed=0"]

    def written(label: str, value: object) -> Path:
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(value))
        return path

    good = written("good", {name: {"allowed_states": states, "reason": "Kept as evidence."}})
    bad = [
        # The earlier sentence-only form names no state, so it is refused rather than read.
        written("sentence", {name: "Kept as evidence."}),
        written("list", [name]),
        written("blank-reason", {name: {"allowed_states": states, "reason": " "}}),
        written("no-states", {name: {"allowed_states": [], "reason": "Kept."}}),
        written("states-not-list", {name: {"allowed_states": "ALARM", "reason": "Kept."}}),
        written("blank-state", {name: {"allowed_states": [" "], "reason": "Kept."}}),
        written("repeated", {name: {"allowed_states": ["ALARM", "ALARM"], "reason": "Kept."}}),
        written("extra", {name: {"allowed_states": states, "reason": "Kept.", "until": "t"}}),
    ]

    assert ops.read_dispositions(good) == {
        name: ops.Disposition(("visible=1 in_flight=0 delayed=0",), "Kept as evidence.")
    }
    assert ops.read_dispositions(None) == {}
    for path in bad:
        with pytest.raises(ops.OperationsError):
            ops.read_dispositions(path)


def test_the_stack_list_must_name_this_environment_with_each_stack_region(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_listed("Adlytics-Api-test", "test-region-1"), _listed(*_EDGE)]))
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps(
            [_listed("Adlytics-Api-test", "test-region-1"), _listed("Adlytics-Api-prod", "r-x-1")]
        )
    )
    names_only = tmp_path / "names.txt"
    names_only.write_text("Adlytics-Api-test\n")
    empty = tmp_path / "empty.json"
    empty.write_text("[]")

    assert ops.read_stacks(good, "test", _ACCOUNT) == [
        ("Adlytics-Api-test", "test-region-1"),
        _EDGE,
    ]
    for bad in (other, names_only, empty):
        with pytest.raises(ops.OperationsError):
            ops.read_stacks(bad, "test", _ACCOUNT)


_ACCOUNT = "111111111111"
_APP_REGION = "test-region-1"
_EDGE = ("Adlytics-EdgeSecurity-test", "edge-region-1")


def _listed(name: str, region: str, account: str = "111111111111") -> dict[str, object]:
    # The shape `cdk list --long --json` prints per stack: id, name and environment.
    return {
        "id": name,
        "name": name,
        "environment": {"account": account, "region": region, "name": f"aws://{account}/{region}"},
    }


def test_lock_targets_read_each_stack_in_its_own_region_and_add_every_bootstrap_stack() -> None:
    listing = [
        _listed("Adlytics-Api-test", "test-region-1"),
        _listed("Adlytics-EdgeSecurity-test", "edge-region-1"),
        _listed("Adlytics-Frontend-test", "test-region-1"),
    ]

    assert ops.lock_targets(listing, "test", "111111111111") == [
        ("Adlytics-Api-test", "test-region-1"),
        ("Adlytics-EdgeSecurity-test", "edge-region-1"),
        ("Adlytics-Frontend-test", "test-region-1"),
        ("CDKToolkit", "test-region-1"),
        ("CDKToolkit", "edge-region-1"),
    ]


@pytest.mark.parametrize(
    ("listing", "refusal"),
    [
        ([_listed("Adlytics-Api-test", "test-region-1", "222222222222")], "not --expected-account"),
        ([_listed("Adlytics-Api-test", "unknown-region")], "no concrete Region"),
        ([_listed("Adlytics-Api-prod", "test-region-1")], "not -test"),
        ([_listed("Adlytics-Api-test", "test-region-1")] * 2, "twice"),
        ([{"id": "Adlytics-Api-test"}], "no name and environment"),
        ([], "non-empty list"),
        ({"Adlytics-Api-test": "test-region-1"}, "non-empty list"),
    ],
    ids=[
        "other-account",
        "env-agnostic",
        "other-env",
        "duplicate",
        "no-environment",
        "empty",
        "object",
    ],
)
def test_lock_targets_refuse_a_stack_the_lock_could_not_read_where_it_is(
    listing: object, refusal: str
) -> None:
    with pytest.raises(ops.OperationsError, match=refusal):
        ops.lock_targets(listing, "test", "111111111111")


def test_the_lock_targets_command_writes_the_targets_file_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    listing = tmp_path / "cdk-list.json"
    listing.write_text(json.dumps([_listed("Adlytics-Api-test", "test-region-1")]))
    out = tmp_path / "targets.txt"
    argv = [
        "lock-targets",
        "--cdk-list",
        str(listing),
        "--env",
        "test",
        "--expected-account",
        "111111111111",
        "--out",
        str(out),
    ]

    first = ops.main(argv)
    first_result = _result(capsys)
    again = ops.main(argv)
    again_result = _result(capsys)

    assert first == 0
    assert first_result["name"] == "OK"
    assert out.read_text() == "Adlytics-Api-test test-region-1\nCDKToolkit test-region-1\n"
    # Paired refusal: an existing targets file is never overwritten.
    assert again == 2
    assert "FileExistsError" in str(again_result["error"])
    assert out.read_text() == "Adlytics-Api-test test-region-1\nCDKToolkit test-region-1\n"


def _result(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1
    return json.loads(lines[0].removeprefix("RESULT "))


def test_a_missing_input_exits_2_with_one_usage_result_and_no_aws_session(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_session(_target: object, _region: object) -> object:
        raise AssertionError("no session may open for a refused input")

    monkeypatch.setattr(ops, "_session", no_session)

    code = ops.main(
        [
            "state",
            "--profile",
            "my-profile",
            "--env",
            "test",
            "--region",
            "r",
            "--evidence-dir",
            str(tmp_path),
            "--cdk-list",
            "s",
            "--lock-file",
            "l",
        ]
    )

    assert code == 2
    result = _result(capsys)
    assert result["name"] == "USAGE"
    assert "--expected-account" in str(result["error"])


def test_a_read_failure_exits_2_and_writes_its_record(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = ops.main(
        [
            "state",
            "--profile",
            "my-profile",
            "--env",
            "test",
            "--region",
            "r",
            "--expected-account",
            "111111111111",
            "--evidence-dir",
            str(tmp_path),
            "--cdk-list",
            str(tmp_path / "absent.json"),
            "--lock-file",
            str(tmp_path / "deploy.lock"),
        ]
    )

    assert code == 2
    result = _result(capsys)
    assert result["name"] == "READ_FAILED"
    records = list(tmp_path.glob("state-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["code"] == 2


def test_a_clean_run_writes_a_record_stamped_like_a_gate_log(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stacks = tmp_path / "cdk-list.json"
    stacks.write_text(json.dumps([_listed("Adlytics-Ingestion-test", _APP_REGION)]))

    def session(_target: object, _region: object) -> object:
        return object()

    def stacks_and_queues(
        _session: object, _names: object
    ) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
        return _STACKS, [dict(_WORK)]

    def alarms(_session: object, _environment: object) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(ops, "_session", session)
    monkeypatch.setattr(ops, "_stacks_and_queues", stacks_and_queues)
    monkeypatch.setattr(ops, "_alarms", alarms)

    code = ops.main(
        [
            "state",
            "--profile",
            "my-profile",
            "--env",
            "test",
            "--region",
            _APP_REGION,
            "--expected-account",
            "111111111111",
            "--evidence-dir",
            str(tmp_path),
            "--cdk-list",
            str(stacks),
            "--lock-file",
            str(tmp_path / "deploy.lock"),
        ]
    )

    assert code == 0
    result = _result(capsys)
    record = json.loads(next(tmp_path.glob("state-*.json")).read_text())
    assert record == json.loads(json.dumps(result, default=str))
    for field in ("head", "tree", "branch", "porcelain_count", "porcelain"):
        assert field in record
    assert record["name"] == "OK"


# --- Each stack is read in its own Region (fake sessions per Region; never AWS) ------------------

_API = ("Adlytics-Api-test", _APP_REGION)
_SOURCE_KEY = "source.zip"


class _FakeAws:
    """Stacks deployed per `(stack, region)`, and every read as `(region, call, name)`."""

    def __init__(self, deployed: set[tuple[str, str]]) -> None:
        self.deployed = deployed
        self.reads: list[tuple[str, str, str]] = []

    def session(self, _target: object, region: str) -> _FakeSession:
        return _FakeSession(self, region)

    def regions_of(self, call: str, name: str) -> list[str]:
        return [region for region, made, read in self.reads if (made, read) == (call, name)]


class _FakeSession:
    def __init__(self, aws: _FakeAws, region: str) -> None:
        self.aws = aws
        self.region = region

    def client(self, service: str) -> object:
        return {
            "cloudformation": _FakeCloudFormation,
            "cloudwatch": _FakeCloudWatch,
            "sqs": _FakeSqs,
        }[service](self.aws, self.region)


class _FakeClient:
    def __init__(self, aws: _FakeAws, region: str) -> None:
        self.aws = aws
        self.region = region

    def _read(self, call: str, name: str) -> None:
        self.aws.reads.append((self.region, call, name))


class _FakeCloudFormation(_FakeClient):
    def _deployed(self, call: str, name: str) -> None:
        self._read(call, name)
        if (name, self.region) not in self.aws.deployed:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Stack with id {name} does not exist",
                    }
                },
                call,
            )

    def describe_stacks(self, **request: str) -> dict[str, object]:
        self._deployed("describe_stacks", request["StackName"])
        return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "LastUpdatedTime": "t"}]}

    def get_template(self, **request: str) -> dict[str, object]:
        self._deployed("get_template", request["StackName"])
        function = {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": _SOURCE_KEY}}}
        return {"TemplateBody": {"Resources": {"Fn": function}}}

    def get_paginator(self, name: str) -> _FakeCloudFormation:
        assert name == "list_stack_resources"
        return self

    def paginate(self, **request: str) -> list[dict[str, object]]:
        self._deployed("list_stack_resources", request["StackName"])
        return [{"StackResourceSummaries": []}]


class _FakeCloudWatch(_FakeClient):
    def get_paginator(self, name: str) -> _FakeCloudWatch:
        assert name == "describe_alarms"
        return self

    def paginate(self, **request: object) -> list[dict[str, object]]:
        self._read("describe_alarms", str(request["AlarmNamePrefix"]))
        return [{"MetricAlarms": [], "CompositeAlarms": []}]


class _FakeSqs(_FakeClient):
    pass


def _run_in_regions(  # noqa: PLR0913, PLR0917 -- subcommand, listing and fixtures
    command: str,
    listed: list[tuple[str, str]],
    aws: _FakeAws,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    listing = tmp_path / f"{command}-cdk-list.json"
    listing.write_text(json.dumps([_listed(stack, region) for stack, region in listed]))
    monkeypatch.setattr(ops, "_session", aws.session)
    evidence = tmp_path / f"{command}-evidence"
    evidence.mkdir()
    extra = ["--lock-file", str(tmp_path / "deploy.lock")] if command == "state" else []
    code = ops.main(
        [
            command,
            "--profile",
            "my-profile",
            "--env",
            "test",
            "--region",
            _APP_REGION,
            "--expected-account",
            _ACCOUNT,
            "--evidence-dir",
            str(evidence),
            "--cdk-list",
            str(listing),
            *extra,
        ]
    )
    return code, _result(capsys)


def test_state_reads_the_edge_stack_in_its_own_region_and_the_others_in_the_application_region(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aws = _FakeAws({_API, _EDGE})

    code, result = _run_in_regions("state", [_API, _EDGE], aws, tmp_path, monkeypatch, capsys)

    assert code == 0
    report = cast("dict[str, object]", result["report"])
    assert report["stacks"] == [
        {"name": _API[0], "region": _APP_REGION, "status": "UPDATE_COMPLETE"},
        {"name": _EDGE[0], "region": _EDGE[1], "status": "UPDATE_COMPLETE"},
    ]
    assert aws.regions_of("describe_stacks", _EDGE[0]) == [_EDGE[1]]
    assert aws.regions_of("list_stack_resources", _EDGE[0]) == [_EDGE[1]]
    # Single-Region control: an ordinary stack, and the alarms, are read in the application Region.
    assert aws.regions_of("describe_stacks", _API[0]) == [_APP_REGION]
    assert aws.regions_of("describe_alarms", "adlytics-test-") == [_APP_REGION]


def test_state_read_in_the_wrong_region_reports_the_edge_stack_absent(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Paired control: the fake is Region-faithful, so the edge stack read in the application Region
    # (the single-session defect) is ABSENT although it is deployed in its own Region.
    aws = _FakeAws({_API, _EDGE})

    code, result = _run_in_regions(
        "state", [_API, (_EDGE[0], _APP_REGION)], aws, tmp_path, monkeypatch, capsys
    )

    assert code == 0
    report = cast("dict[str, object]", result["report"])
    stacks = cast("list[dict[str, str]]", report["stacks"])
    assert stacks[1] == {"name": _EDGE[0], "region": _APP_REGION, "status": "ABSENT"}


def _record_assets(_worktree: Path, stack: str) -> dict[str, object]:
    function = {"handler": "", "runtime": "", "s3_key": _SOURCE_KEY}
    return {"stack": stack, "source_asset_key": _SOURCE_KEY, "functions": {"Fn": function}}


def test_source_key_reads_the_edge_stack_back_in_its_own_region(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "record_assets", _record_assets)
    aws = _FakeAws({_API, _EDGE})

    code, result = _run_in_regions("source-key", [_API, _EDGE], aws, tmp_path, monkeypatch, capsys)

    assert code == 0, result
    report = cast("dict[str, object]", result["report"])
    assert report["source_keys"] == [_SOURCE_KEY]
    assert report["unequal_stacks"] == []
    assert aws.regions_of("get_template", _EDGE[0]) == [_EDGE[1]]
    assert aws.regions_of("describe_stacks", _EDGE[0]) == [_EDGE[1]]
    # Single-Region control: the ordinary stack is read back in the application Region.
    assert aws.regions_of("get_template", _API[0]) == [_APP_REGION]


def test_source_key_read_back_in_the_wrong_region_is_a_read_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "record_assets", _record_assets)
    aws = _FakeAws({_API, _EDGE})

    code, result = _run_in_regions(
        "source-key", [_API, (_EDGE[0], _APP_REGION)], aws, tmp_path, monkeypatch, capsys
    )

    assert code == 2
    assert result["name"] == "READ_FAILED"
    assert "does not exist" in str(result["error"])


# --- The alarm window reads each alarm's current state (fake CloudWatch; never AWS) --------------


class _FakeAlarmSession:
    """One expected alarm in `state` (ALARM by default) since `entered`.

    `transitioned=False` omits StateTransitionedTimestamp from the alarm, as a read may. `history`
    is the requested window's StateUpdate rows for it, `(timestamp, summary)`; none by default.
    """

    def __init__(  # noqa: PLR0913 -- the fake alarm's state, keyword-only
        self,
        entered: datetime,
        *,
        transitioned: bool = True,
        state: str = "ALARM",
        history: tuple[tuple[datetime, str], ...] = (),
    ) -> None:
        self.entered = entered
        self.transitioned = transitioned
        self.state = state
        self.history = history
        self.requests: list[tuple[str, dict[str, object]]] = []
        self._name = ""

    def session(self, _target: object, _region: str) -> _FakeAlarmSession:
        return self

    def client(self, service: str) -> _FakeAlarmSession:
        assert service == "cloudwatch"
        return self

    def get_paginator(self, name: str) -> _FakeAlarmSession:
        assert name in {"describe_alarms", "describe_alarm_history"}
        self._name = name
        return self

    def paginate(self, **request: object) -> list[dict[str, object]]:
        self.requests.append((self._name, request))
        if self._name == "describe_alarm_history":
            items = [
                {"Timestamp": at, "AlarmName": PROBE, "HistorySummary": summary}
                for at, summary in self.history
            ]
            return [{"AlarmHistoryItems": items}]
        alarm: dict[str, object] = {
            "AlarmName": PROBE,
            "StateValue": self.state,
            "StateUpdatedTimestamp": self.entered,
        }
        if self.transitioned:
            alarm["StateTransitionedTimestamp"] = self.entered
        return [{"MetricAlarms": [alarm], "CompositeAlarms": []}]


@pytest.mark.parametrize(("recover_minutes", "code"), [("15", 4), ("180", 0)])
def test_alarm_window_bounds_an_alarm_already_in_alarm_before_since(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    recover_minutes: str,
    code: int,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entered ALARM two hours ago; the window opens one hour ago, so it holds no row for it. Past a
    # 15-minute bound it is not recovered (4); within a 180-minute bound it may stand (0).
    started = datetime.now(UTC)
    session = _FakeAlarmSession(started - timedelta(hours=2))
    monkeypatch.setattr(ops, "_session", session.session)

    got = ops.main(
        [
            "alarm-window",
            "--profile",
            "my-profile",
            "--env",
            "test",
            "--region",
            _APP_REGION,
            "--expected-account",
            _ACCOUNT,
            "--evidence-dir",
            str(tmp_path),
            "--since",
            (started - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--recover-minutes",
            recover_minutes,
            "--expected",
            PROBE,
        ]
    )

    result = _result(capsys)
    assert got == code, result
    report = cast("dict[str, object]", result["report"])
    assert report["transitions"] == []
    assert report["not_recovered"] == ([PROBE] if code else [])
    assert result["name"] == ("EXPECTED_ALARM_NOT_RECOVERED" if code else "OK")
    assert [name for name, _request in session.requests] == [
        "describe_alarms",
        "describe_alarm_history",
    ]


def _alarm_window_args(tmp_path: Path, started: datetime, expected: str) -> list[str]:
    return [
        "alarm-window",
        "--profile",
        "my-profile",
        "--env",
        "test",
        "--region",
        _APP_REGION,
        "--expected-account",
        _ACCOUNT,
        "--evidence-dir",
        str(tmp_path),
        "--since",
        (started - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--recover-minutes",
        "15",
        "--expected",
        expected,
    ]


_UNLISTED = "adlytics-test-unlisted"


@pytest.mark.parametrize(
    ("expected", "transitioned", "name", "error"),
    [
        (
            _UNLISTED,
            True,
            "USAGE",
            f"OperationsError: --expected names alarms the prefix does not list: ['{_UNLISTED}']",
        ),
        (
            PROBE,
            False,
            "READ_FAILED",
            f"OperationsError: alarm {PROBE} is in ALARM but the read has no "
            "StateTransitionedTimestamp",
        ),
    ],
    ids=["unlisted-expected-name", "alarm-without-transition-time"],
)
def test_alarm_window_refuses_an_unclassifiable_expectation_before_any_history_read(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    expected: str,
    transitioned: bool,
    name: str,
    error: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime.now(UTC)
    session = _FakeAlarmSession(started - timedelta(hours=2), transitioned=transitioned)
    monkeypatch.setattr(ops, "_session", session.session)

    got = ops.main(_alarm_window_args(tmp_path, started, expected))

    # Refused in run_alarm_window from the current states alone: exit 2, its own name and
    # message, the record written, and no describe_alarm_history request.
    result = _result(capsys)
    assert got == ops.READ_FAILED == 2
    assert result["code"] == 2
    assert result["name"] == name
    assert result["error"] == error
    assert "report" not in result
    assert [request for request, _ in session.requests] == ["describe_alarms"]
    assert len(list(tmp_path.glob("alarm-window-*.json"))) == 1

    # Paired control: the same alarm, listed and with its transition time, reaches history and is
    # held to the bound: in ALARM for two hours against 15 minutes is not recovered (4).
    capsys.readouterr()
    paired = _FakeAlarmSession(started - timedelta(hours=2))
    monkeypatch.setattr(ops, "_session", paired.session)

    control_dir = tmp_path / "control"
    control_dir.mkdir()

    assert ops.main(_alarm_window_args(control_dir, started, PROBE)) == 4
    control = _result(capsys)
    assert control["name"] == "EXPECTED_ALARM_NOT_RECOVERED"
    assert [request for request, _ in paired.requests] == [
        "describe_alarms",
        "describe_alarm_history",
    ]


@pytest.mark.parametrize(("state", "code"), [("OK", 0), ("ALARM", 4)])
def test_alarm_window_takes_recovery_from_the_current_state_over_a_stale_history_row(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    state: str,
    code: int,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window's last visible row for the expected alarm enters ALARM 50 minutes ago, past the
    # 15-minute bound, and no recovery row follows. OK now: recovered (0). ALARM now, since that
    # row: not recovered (4).
    started = datetime.now(UTC)
    entered = started - timedelta(minutes=50)
    session = _FakeAlarmSession(
        entered, state=state, history=((entered, "Alarm updated from OK to ALARM"),)
    )
    monkeypatch.setattr(ops, "_session", session.session)

    got = ops.main(_alarm_window_args(tmp_path, started, PROBE))

    result = _result(capsys)
    assert got == code, result
    report = cast("dict[str, object]", result["report"])
    assert [row["summary"] for row in _entries(report, "transitions")] == [
        "Alarm updated from OK to ALARM"
    ]
    assert report["not_recovered"] == ([PROBE] if code else [])
    assert result["name"] == ("EXPECTED_ALARM_NOT_RECOVERED" if code else "OK")


# --- owned (out-of-scope) dispositions ----------------------------------------------------------

_EVALUATION = "adlytics-test-evaluation-grounded-correctness-worst-label"
_GRAPH = "adlytics-test-graph-bake-failed"
_OWNED = ops.Disposition(
    ("OK", "INSUFFICIENT_DATA", "ALARM"), "Chat answer quality, not GRAPH serving.", "D2"
)


@pytest.mark.parametrize("state", ["ALARM", "OK", "INSUFFICIENT_DATA"])
def test_an_owned_alarm_is_recorded_with_its_actual_state_and_never_hidden(state: str) -> None:
    alarms = [{"name": _EVALUATION, "state": state, "updated": "t1"}]

    code, report = ops.classify_state(alarms, [], _STACKS, _NO_LOCK, {_EVALUATION: _OWNED})

    assert code == ops.OK
    assert report["undisposed"] == []
    assert report["disposed"] == []
    assert report["unused_dispositions"] == []
    assert report["out_of_scope"] == [
        {
            "alarm": _EVALUATION,
            "state": state,
            "transitioned": "t1",
            "conforms": True,
            "disposition": "Chat answer quality, not GRAPH serving.",
            "allowed_states": ["OK", "INSUFFICIENT_DATA", "ALARM"],
            "owner": "D2",
        }
    ]


def test_a_graph_scope_alarm_in_alarm_still_fails_beside_a_recorded_owned_one() -> None:
    alarms = [
        {"name": _EVALUATION, "state": "ALARM", "updated": "t1"},
        {"name": _GRAPH, "state": "ALARM", "updated": "t2"},
    ]

    code, report = ops.classify_state(alarms, [], _STACKS, _NO_LOCK, {_EVALUATION: _OWNED})

    assert code == ops.UNDISPOSED_STATE
    assert [entry["name"] for entry in _entries(report, "undisposed")] == [_GRAPH]
    assert [entry["alarm"] for entry in _entries(report, "out_of_scope")] == [_EVALUATION]


def test_an_owned_alarm_in_a_state_it_does_not_list_fails_and_is_still_recorded() -> None:
    narrow = ops.Disposition(("OK", "INSUFFICIENT_DATA"), "Quiet trend.", "D2")
    alarms = [{"name": _EVALUATION, "state": "ALARM", "updated": "t1"}]

    code, report = ops.classify_state(alarms, [], _STACKS, _NO_LOCK, {_EVALUATION: narrow})

    assert code == ops.UNDISPOSED_STATE
    [entry] = _entries(report, "undisposed")
    assert entry["refused_disposition"] == narrow.projection
    assert _entries(report, "out_of_scope")[0]["conforms"] is False


@pytest.mark.parametrize(
    ("name", "code"),
    [
        # The owned evaluation alarm, in ALARM since before the window: recorded, not a stop.
        (_EVALUATION, ops.OK),
        # Paired control: a GRAPH-scope alarm in the same standing ALARM still stops with 3.
        (_GRAPH, ops.UNEXPECTED_ALARM),
    ],
)
def test_the_window_records_a_standing_owned_alarm_and_stops_on_a_graph_one(
    name: str, code: int
) -> None:
    alarms = [*_ALL_OK, _now(_EVALUATION), _now(_GRAPH)]
    alarms = [_now(name, "ALARM", "09:00:00") if a["name"] == name else a for a in alarms]

    got, report = ops.classify_alarm_window([], alarms, frozenset(), 15, NOW, {_EVALUATION: _OWNED})

    assert got == code
    [scoped] = _entries(report, "out_of_scope")
    assert scoped["alarm"] == _EVALUATION
    assert scoped["state"] == ("ALARM" if name == _EVALUATION else "OK")
    assert scoped["owner"] == "D2"
    assert report["unexpected_alarm"] == ([] if name == _EVALUATION else [_GRAPH])


def test_an_owned_alarm_entering_alarm_in_the_window_is_recorded_not_unexpected() -> None:
    rows = [_row("10:01:00", _EVALUATION, "OK to ALARM")]
    alarms = [*_ALL_OK, _now(_EVALUATION, "ALARM", "10:01:00")]
    narrow = ops.Disposition(("OK", "INSUFFICIENT_DATA"), "Quiet trend.", "D2")

    code, report = ops.classify_alarm_window(
        rows, alarms, frozenset(), 15, NOW, {_EVALUATION: _OWNED}
    )
    refused, _ = ops.classify_alarm_window(
        rows, alarms, frozenset(), 15, NOW, {_EVALUATION: narrow}
    )

    assert code == ops.OK
    assert _entries(report, "out_of_scope")[0]["window_transitions"] == 1
    assert report["unexpected_alarm"] == []
    # Paired control: the same alarm under an owned disposition that does not list ALARM.
    assert refused == ops.UNEXPECTED_ALARM


def test_unowned_dispositions_do_not_change_the_window() -> None:
    alarms = [*_ALL_OK, _now(_EVALUATION, "ALARM", "09:00:00")]
    unowned = ops.Disposition(("ALARM",), "Not owned elsewhere.")

    code, report = ops.classify_alarm_window(
        [], alarms, frozenset(), 15, NOW, {_EVALUATION: unowned}
    )

    assert code == ops.UNEXPECTED_ALARM
    assert report["out_of_scope"] == []


def test_an_out_of_scope_alarm_must_be_listed_and_not_also_expected() -> None:
    alarms = [*_ALL_OK, _now(_EVALUATION)]

    with pytest.raises(ops.OperationsError, match="unlisted alarms"):
        ops.classify_alarm_window([], _ALL_OK, frozenset(), 15, NOW, {_EVALUATION: _OWNED})
    with pytest.raises(ops.OperationsError, match="both expected and out of scope"):
        ops.classify_alarm_window(
            [], alarms, frozenset({_EVALUATION}), 15, NOW, {_EVALUATION: _OWNED}
        )


def test_a_disposition_may_name_a_non_empty_owner(tmp_path: Path) -> None:
    states = ["OK", "INSUFFICIENT_DATA", "ALARM"]
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({_EVALUATION: {"allowed_states": states, "reason": "r", "owner": "D2"}})
    )
    blank = tmp_path / "blank.json"
    blank.write_text(
        json.dumps({_EVALUATION: {"allowed_states": states, "reason": "r", "owner": " "}})
    )

    assert ops.read_dispositions(good) == {_EVALUATION: ops.Disposition(tuple(states), "r", "D2")}
    with pytest.raises(ops.OperationsError, match="owner"):
        ops.read_dispositions(blank)


# --- serving-refusals (D2 ops check O3, ADR-139) -----------------------------------------------

_SERVED = {"key": "reach#last_complete_month", "pointer": "grant_bearing", "served": True}
_UNSERVED = {
    "key": "campaigns#7d",
    "pointer": "grant_bearing",
    "served": False,
    "reason": "ungranted_generation",
}
_LEGACY = {"key": "campaigns#1d", "pointer": "legacy"}
_QUIET = {
    "adlytics-test-presentation-serving-refused": "OK",
    "adlytics-test-graph-serving-withheld": "INSUFFICIENT_DATA",
}
_SUMS = {"past_settle": 0.0, "within_settle": 0.0}


@pytest.mark.covers("ADR-139")
def test_o3_passes_only_when_every_grant_bearing_current_is_served_and_nothing_alarms() -> None:
    code, report = ops.classify_serving_refusals(
        [_SERVED, _LEGACY], {"past_settle": 0.0, "within_settle": 2.0}, _QUIET, []
    )
    assert (code, report["verdict"]) == (ops.OK, "PASS")
    # The within-settle sum is a defect signal: recorded, never paged.
    assert report["within_settle_sum"] == 2.0


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    ("census", "sums", "alarms"),
    [
        ([_SERVED, _UNSERVED], _SUMS, _QUIET),
        ([_SERVED], {"past_settle": 1.0, "within_settle": 0.0}, _QUIET),
        ([_SERVED], _SUMS, {**_QUIET, "adlytics-test-graph-serving-withheld": "ALARM"}),
        ([_SERVED], _SUMS, {**_QUIET, "adlytics-test-presentation-serving-refused": "ALARM"}),
    ],
    ids=["unserved-current", "past-settle-refusal", "withheld-alarm", "refusal-alarm"],
)
def test_o3_fails_on_an_unserved_current_a_past_settle_refusal_or_an_alarm(
    census: list[dict[str, object]], sums: dict[str, float], alarms: dict[str, str | None]
) -> None:
    code, report = ops.classify_serving_refusals(census, sums, alarms, [])
    assert (code, report["verdict"]) == (ops.SERVING_REFUSALS_FAIL, "FAIL")


@pytest.mark.covers("ADR-139")
def test_o3_is_not_proven_when_an_alarm_does_not_exist_or_a_read_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, report = ops.classify_serving_refusals(
        [_SERVED], _SUMS, {**_QUIET, "adlytics-test-graph-serving-withheld": None}, []
    )
    assert (code, report["verdict"], report["cause"]) == (
        ops.SERVING_NOT_PROVEN,
        "NOT_PROVEN",
        "alarm_absent",
    )

    def unreadable(_session: object, _environment: str) -> list[dict[str, object]]:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "fictional"}}, "GetItem")

    def session(_target: object, _region: str) -> object:
        return object()

    monkeypatch.setattr(ops, "_session", session)
    monkeypatch.setattr(ops, "_serving_census", unreadable)
    target = ops.Target("fictional-profile", "test", "us-east-1", "000000000000")
    arguments = ops._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "serving-refusals",
            "--profile",
            "fictional-profile",
            "--env",
            "test",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--evidence-dir",
            str(tmp_path),
            "--since",
            (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
    )
    code, report = ops.run_serving_refusals(target, arguments)
    assert (code, report["verdict"]) == (ops.SERVING_NOT_PROVEN, "NOT_PROVEN")
    assert "AccessDenied" in str(report["cause"])


_REFUSAL_ALARM_SOURCE = _ROOT / "infra/lib/constructs/observability/serving-refusal-alarm.ts"


def _alarm_query_from_source() -> str:
    """Assemble the alarm's `SERVING_REFUSAL_QUERY` from its TypeScript literals."""
    source = _REFUSAL_ALARM_SOURCE.read_text()
    metric = re.search(r"export const SERVING_REFUSAL_METRIC = '([^']+)';", source)
    query = re.search(r"export const SERVING_REFUSAL_QUERY =(.*?);\n", source, re.S)
    assert metric is not None
    assert query is not None
    literals = re.findall(r"`([^`]*)`|\"([^\"]*)\"|'([^']*)'", query.group(1))
    text = "".join(next(part for part in parts if part) for parts in literals)
    return text.replace("${SERVING_REFUSAL_METRIC}", metric.group(1))


@pytest.mark.covers("ADR-139")
def test_o3_reads_past_settle_with_exactly_the_alarm_query_on_its_period() -> None:
    assert ops.serving_refusal_query("past_settle") == _alarm_query_from_source()
    assert "WHERE Settle = 'past_settle'" in _alarm_query_from_source()
    # The within-settle read differs from the alarm's only in its settle class.
    assert ops.serving_refusal_query("within_settle") == _alarm_query_from_source().replace(
        "'past_settle'", "'within_settle'"
    )
    assert ops.SERVING_REFUSAL_PERIOD_SECONDS == 60
    assert "BAKE_TASKS.worker_schedule_seconds" in _REFUSAL_ALARM_SOURCE.read_text()
    with pytest.raises(ValueError, match="settle class"):
        ops.serving_refusal_query("past_settle' OR Settle = 'within_settle")


class _MetricPages:
    """A `get_metric_data` paginator over recorded pages, keeping each call's arguments."""

    def __init__(self, pages: dict[str, list[dict[str, object]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def client(self, name: str) -> _MetricPages:
        assert name == "cloudwatch"
        return self

    def get_paginator(self, name: str) -> _MetricPages:
        assert name == "get_metric_data"
        return self

    def paginate(self, **arguments: object) -> list[dict[str, object]]:
        self.calls.append(arguments)
        queries = cast("list[dict[str, object]]", arguments["MetricDataQueries"])
        return self.pages[str(queries[0]["Expression"])]


def _metric_page(values: list[float], status: str = "Complete") -> dict[str, object]:
    return {"MetricDataResults": [{"Id": "refused", "Values": values, "StatusCode": status}]}


# A response page with no result object, and a read with no page at all: no evidence of a sum.
_NO_RESULTS: Final[dict[str, object]] = {"MetricDataResults": []}
_NO_PAGES: Final[list[dict[str, object]]] = []


@pytest.mark.covers("ADR-139")
def test_o3_sums_each_settle_class_from_one_paginated_metrics_insights_call() -> None:
    past = ops.serving_refusal_query("past_settle")
    within = ops.serving_refusal_query("within_settle")
    session = _MetricPages(
        {
            past: [_metric_page([1.0, 2.0]), _metric_page([3.0])],
            within: [_metric_page([])],
        }
    )
    since = NOW - timedelta(hours=6)

    sums = ops._serving_refusal_sums(session, since, NOW)  # pyright: ignore[reportPrivateUsage]

    assert sums == {"within_settle": 0.0, "past_settle": 6.0}
    # One Metrics Insights query per GetMetricData call, on the alarm's period, over the window.
    assert [call["MetricDataQueries"] for call in session.calls] == [
        [{"Id": "refused", "Expression": within, "Period": 60, "ReturnData": True}],
        [{"Id": "refused", "Expression": past, "Period": 60, "ReturnData": True}],
    ]
    assert all((c["StartTime"], c["EndTime"]) == (since, NOW) for c in session.calls)


@pytest.mark.covers("ADR-139")
@pytest.mark.parametrize(
    "pages",
    [
        [_metric_page([0.0], "PartialData")],
        [_metric_page([0.0], "PartialData"), _metric_page([0.0])],
        [_metric_page([0.0]), _metric_page([0.0], "InternalError"), _metric_page([0.0])],
        [{"MetricDataResults": [{"Id": "refused", "Values": [0.0]}]}],
        [_metric_page([0.0], "Forbidden")],
        [{**_metric_page([0.0]), "Messages": [{"Code": "MaxQueryTimeRangeExceed"}]}],
        [_NO_RESULTS],
        [{}],
        _NO_PAGES,
        [{"MetricDataResults": [{"Id": "other", "Values": [0.0], "StatusCode": "Complete"}]}],
        [_metric_page([0.0]), {"MetricDataResults": [{"Values": [0.0], "StatusCode": "Complete"}]}],
    ],
    ids=[
        "partial-last-page",
        "partial-then-complete",
        "mid-stream-error-then-complete",
        "missing-status",
        "forbidden",
        "range-exceeded",
        "empty-results",
        "no-results-key",
        "no-pages",
        "unexpected-id",
        "id-less-result-after-complete",
    ],
)
def test_o3_refuses_a_refusal_read_that_did_not_cover_the_window(
    pages: list[dict[str, object]],
) -> None:
    past = ops.serving_refusal_query("past_settle")
    session = _MetricPages({past: pages, ops.serving_refusal_query("within_settle"): pages})
    with pytest.raises(ops.OperationsError, match="refusal query"):
        ops._serving_refusal_sums(session, NOW - timedelta(hours=1), NOW)  # pyright: ignore[reportPrivateUsage]
    # Control: the same read, complete and message-free, sums.
    clean = _MetricPages(
        {
            past: [_metric_page([0.0])],
            ops.serving_refusal_query("within_settle"): [_metric_page([0.0])],
        }
    )
    assert ops._serving_refusal_sums(clean, NOW - timedelta(hours=1), NOW)["past_settle"] == 0.0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.covers("ADR-139")
def test_o3_is_not_proven_not_passed_when_cloudwatch_returns_no_refusal_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metrics = _MetricPages(
        {
            ops.serving_refusal_query("past_settle"): [_NO_RESULTS],
            ops.serving_refusal_query("within_settle"): [_NO_RESULTS],
        }
    )

    def session(_target: object, _region: str) -> object:
        return metrics

    def census(_session: object, _environment: str) -> list[dict[str, str | bool]]:
        return [_SERVED]

    def alarms(_session: object, _environment: str) -> dict[str, str | None]:
        return dict(_QUIET)

    def logs(*_arguments: object) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(ops, "_session", session)
    monkeypatch.setattr(ops, "_serving_census", census)
    monkeypatch.setattr(ops, "_serving_alarm_states", alarms)
    monkeypatch.setattr(ops, "_serving_refusal_logs", logs)
    target = ops.Target("fictional-profile", "test", "us-east-1", "000000000000")
    arguments = ops._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "serving-refusals",
            "--profile",
            "fictional-profile",
            "--env",
            "test",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--evidence-dir",
            str(tmp_path),
            "--since",
            (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
    )

    code, report = ops.run_serving_refusals(target, arguments)

    # A clean census and quiet alarms do not stand in for the missing refusal sum.
    assert (code, report["verdict"]) == (ops.SERVING_NOT_PROVEN, "NOT_PROVEN")
    assert "returned no result" in str(report["cause"])


@pytest.mark.covers("ADR-139")
def test_o3_refuses_a_since_beyond_the_metrics_insights_query_range(tmp_path: Path) -> None:
    target = ops.Target("fictional-profile", "test", "us-east-1", "000000000000")

    def arguments(since: datetime) -> object:
        return ops._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            [
                "serving-refusals",
                "--profile",
                "fictional-profile",
                "--env",
                "test",
                "--region",
                "us-east-1",
                "--expected-account",
                "000000000000",
                "--evidence-dir",
                str(tmp_path),
                "--since",
                since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ]
        )

    stale = datetime.now(UTC) - ops.METRICS_INSIGHTS_QUERY_RANGE - timedelta(minutes=1)
    with pytest.raises(ops.OperationsError, match="two-week"):
        ops.run_serving_refusals(target, arguments(stale))  # pyright: ignore[reportArgumentType]


@pytest.mark.covers("ADR-139")
def test_o3_census_applies_the_one_serving_predicate_to_each_pointer() -> None:
    @dataclass(frozen=True)
    class _Pointer:
        grant_id: str | None
        serving_generation: str | None
        written_at: str | None = "2026-01-01T00:00:00Z"

    grant = "1" * 64
    pointer = _Pointer(grant, serving_generation(grant))

    def binding(unavailable: tuple[tuple[str, str], ...]) -> ServingBinding:
        return ServingBinding(
            binding_id="d" * 64,
            grant_id=grant,
            conflict_unit="u" * 64,
            served_revision=NOW,
            artifact_revision=serving_generation(grant),
            contributor_roots=("root",),
            bound_at=NOW,
            unavailable=unavailable,
            domain="domain",
            cohort_revision="cohort",
            serving_input_revision="input",
            plan_digest="plan",
            binding_kind=BindingKind.DIRECT,
            winner_run_id="run",
            execution_attempts=1,
        )

    designed = binding((("reach#last_complete_month", "reach_materialization_unavailable"),))
    served = ops.serving_census_row("reach", "last_complete_month", pointer, designed)
    assert (served["served"], served["failure_entries"]) == (True, [])
    failed = binding((("precompute#x", "precompute_budget_exhausted"),))
    refused = ops.serving_census_row("reach", "last_complete_month", pointer, failed)
    assert (refused["served"], refused["reason"]) == (False, "ungranted_generation")
    assert refused["failure_entries"] == [["precompute#x", "precompute_budget_exhausted"]]
    pending = ops.serving_census_row("reach", "last_complete_month", pointer, None)
    assert (pending["served"], pending["reason"]) == (False, "serving_refresh_pending")
    assert ops.serving_census_row("reach", "v", None, None)["pointer"] == "absent"
    assert ops.serving_census_row("reach", "v", _Pointer(None, None), None)["pointer"] == "legacy"
