# SPDX-License-Identifier: MIT-0
"""FEAT-011 class (c): the reviewed requeue of one orphan plan's queued, never-begun units.

The message comes from `unit_message` and goes through `SqsWriteUnitSender`, and TableWriter's
own parser reads it as the legacy unit body. Every refusal is named and sends nothing. The
table is the transactional `FakeDynamo` of the TD-50 suite. Every identity is fictional.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_supersede_orphan_units import (
    AT,
    DLQ,
    QUEUE,
    TARGETS,
    _low,
    _Plan,
    _queued,
    _World,
)
from test_table_writer import _INGESTION_ENV, _SETTINGS_ENV

from adapters import config
from adapters.config import settings_without_client_secret, tracing_settings
from adapters.jobs.supersede_orphan_units import __main__ as supersede
from adapters.jobs.supersede_orphan_units import requeue
from domain.operations.write_plan import (
    META_RECORD_KEY,
    SUCCESSOR_OWNER_RECORD_KEY,
    WriteUnitMessageRequest,
    intent_partition_key,
    owner_partition_key,
    unit_message,
    unit_record_key,
)

pytestmark = [pytest.mark.unit]

ONLY_COPY = _Plan("4")
OTHER = _Plan("5")
TRAFFIC = "z_source.sa_vendor_traffic_by_asin"
TOTAL = "z_source.sa_vendor_traffic_reported_total"


class _Sqs:
    """Queue depths and `send_message`; a send may fail or run a hook after it lands."""

    def __init__(self) -> None:
        self.depth = {QUEUE: 0, DLQ: 0}
        self.sent: list[dict[str, Any]] = []
        self.fail_on: int | None = None
        self.fail_depth = False
        self.after_send: Any = None

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_depth:
            raise RuntimeError("injected queue-depth failure")
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(self.depth[kwargs["QueueUrl"]]),
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
            }
        }

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_on == len(self.sent):
            raise RuntimeError("injected send failure")
        self.sent.append(kwargs)
        self.depth[QUEUE] += 1
        if self.after_send is not None:
            self.after_send(len(self.sent))
        return {"MessageId": f"message-{len(self.sent)}", "SequenceNumber": f"{len(self.sent)}"}


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """TableWriter loaded under its own test environment (as `test_table_writer` loads it)."""
    for name, value in {**_SETTINGS_ENV, **_INGESTION_ENV}.items():
        monkeypatch.setenv(name, value)
    caches = (
        settings_without_client_secret,
        tracing_settings,
        config.table_writer_settings,
        config._queued_ingestion_config,
        config.run_request_api_settings,
    )
    for accessor in caches:
        accessor.cache_clear()
    module_name = "adapters.inbound.ingestion_handlers.table_writer"
    sys.modules.pop(module_name, None)
    yield importlib.import_module(module_name)
    sys.modules.pop(module_name, None)
    for accessor in caches:
        accessor.cache_clear()


def _world() -> tuple[_World, _Sqs]:
    world = _World()
    world.plan(
        ONLY_COPY,
        {TRAFFIC: _queued("source_aligned", []), TOTAL: _queued("source_aligned", [])},
    )
    intent = world.item(intent_partition_key(ONLY_COPY.digest, ONLY_COPY.intent), META_RECORD_KEY)
    intent["claimed_generation"] = _low(0)
    return world, _Sqs()


def _clients(world: _World, sqs: _Sqs) -> supersede.Clients:
    return supersede.Clients(world.table, sqs, requeue.no_object_reads)


def _run(  # noqa: PLR0913 -- the world, its queues and two keyword-only knobs
    world: _World, sqs: _Sqs, *, apply: bool, plan: _Plan = ONLY_COPY
) -> tuple[Any, Any]:
    return requeue.requeue(_clients(world, sqs), TARGETS, plan_key=plan.key, apply=apply, now=AT)


def _expected(unit_key: str) -> Any:
    return unit_message(
        WriteUnitMessageRequest(ONLY_COPY.key, ONLY_COPY.digest, ONLY_COPY.intent, 0, unit_key)
    )


def test_a_dry_run_records_each_units_exact_message_and_sends_nothing(writer: Any) -> None:
    world, sqs = _world()
    before = copy.deepcopy(world.table.items)
    document, send = _run(world, sqs, apply=False)
    assert send is None
    assert sqs.sent == []
    assert world.table.items == before
    assert world.table.transactions == []
    assert (document["requeueable"], document["refusals"]) == (True, [])
    assert [member["unit_key"] for member in document["units"]] == [TRAFFIC, TOTAL]
    for member in document["units"]:
        message = _expected(member["unit_key"])
        assert (member["body"], member["group_id"], member["deduplication_id"]) == (
            message.body,
            message.group_id,
            message.deduplication_id,
        )
        # TableWriter's own parser reads it as the legacy six-field unit body, not a graph one.
        delivery = writer.table_writer_delivery(
            {
                "Records": [
                    {
                        "body": member["body"],
                        "messageId": "message-fictional",
                        "receiptHandle": "receipt-fictional",
                        "attributes": {"ApproximateReceiveCount": "1"},
                    }
                ]
            }
        )
        assert isinstance(delivery, writer.WriteUnitDelivery)
        assert (delivery.plan_key, delivery.unit_key, delivery.generation) == (
            ONLY_COPY.key,
            member["unit_key"],
            0,
        )
    assert document["state_read"]["intent"] == {"state": "admitted", "claimed_generation": 0}


def test_apply_sends_each_unit_once_through_the_reconcilers_sender() -> None:
    world, sqs = _world()
    document, send = _run(world, sqs, apply=True)
    assert send is not None
    outcome = send()
    assert outcome["complete"] is True
    assert [item["unit_key"] for item in outcome["sent"]] == [TRAFFIC, TOTAL]
    assert [item["message_id"] for item in outcome["sent"]] == ["message-1", "message-2"]
    assert sqs.sent == [
        {
            "QueueUrl": QUEUE,
            "MessageBody": member["body"],
            "MessageGroupId": member["group_id"],
            "MessageDeduplicationId": member["deduplication_id"],
        }
        for member in document["units"]
    ]
    # Nothing in the table is written by the requeue itself.
    assert world.table.transactions == []


def test_a_rerun_is_refused_while_the_requeued_messages_wait_and_keeps_the_same_ids() -> None:
    world, sqs = _world()
    first, send = _run(world, sqs, apply=True)
    assert send is not None
    send()
    second, again = _run(world, sqs, apply=True)
    assert again is None
    assert second["refusals"] == ["semantic_write_queue_not_empty"]
    assert [m["deduplication_id"] for m in second["units"]] == [
        m["deduplication_id"] for m in first["units"]
    ]
    assert len(sqs.sent) == 2


def _owner(world: _World) -> dict[str, Any]:
    return world.item(owner_partition_key(ONLY_COPY.digest), SUCCESSOR_OWNER_RECORD_KEY)


def _tamper(world: _World, sqs: _Sqs, case: str) -> None:
    meta = world.item(ONLY_COPY.key, META_RECORD_KEY)
    unit = world.item(ONLY_COPY.key, unit_record_key(TRAFFIC))
    intent = world.item(intent_partition_key(ONLY_COPY.digest, ONLY_COPY.intent), META_RECORD_KEY)
    if case == "queue":
        sqs.depth[QUEUE] = 1
    elif case == "dlq":
        sqs.depth[DLQ] = 1
    elif case == "begun":
        unit["unit_reservation_count"] = _low(1)
    elif case == "writing":
        unit["status"] = _low("writing")
    elif case == "reserved_plan":
        meta["plan_reservation_count"] = _low(1)
    elif case == "not_planned":
        meta["status"] = _low("completed")
    elif case == "intent_claimed":
        intent["state"] = _low("claimed")
    elif case == "stale_generation":
        intent["claimed_generation"] = _low(1)
    elif case == "owner_replaced":
        _owner(world)["active_plan_key"] = _low(OTHER.key)
    else:
        del _owner(world)["active_intent_id"]


_PRECONDITIONS = [
    ("queue", "semantic_write_queue_not_empty"),
    ("dlq", "semantic_write_queue_not_empty"),
    ("begun", "unit_not_queued_never_begun"),
    ("writing", "unit_not_queued_never_begun"),
    ("reserved_plan", "plan_counters_not_queued_never_begun"),
    ("not_planned", "plan_not_planned"),
    ("intent_claimed", "origin_intent_not_admitted_at_the_plan_generation"),
    ("stale_generation", "origin_intent_not_admitted_at_the_plan_generation"),
    ("owner_replaced", "successor_owner_does_not_name_the_plan"),
    ("owner_inactive", "successor_owner_does_not_name_the_plan"),
]


@pytest.mark.parametrize(("case", "refusal"), _PRECONDITIONS)
def test_every_precondition_refuses_by_name_and_sends_nothing(case: str, refusal: str) -> None:
    world, sqs = _world()
    _tamper(world, sqs, case)
    document, send = _run(world, sqs, apply=True)
    assert send is None
    assert document["requeueable"] is False
    assert refusal in document["refusals"]
    assert sqs.sent == []


def test_an_absent_plan_is_refused() -> None:
    world, sqs = _world()
    document, send = _run(world, sqs, apply=True, plan=OTHER)
    assert (send, document["refusals"], document["units"]) == (None, ["plan_meta_absent"], [])


def test_a_unit_that_moves_between_sends_stops_the_requeue_before_it() -> None:
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None

    def claimed(count: int) -> None:
        if count == 1:
            world.item(ONLY_COPY.key, unit_record_key(TOTAL))["status"] = _low("writing")

    sqs.after_send = claimed
    outcome = send()
    assert [item["unit_key"] for item in outcome["sent"]] == [TRAFFIC]
    assert outcome["stopped"] == "state_moved"
    assert outcome["refusals"] == ["unit_not_queued_never_begun"]
    assert outcome["not_attempted"] == [TOTAL]
    assert outcome["complete"] is False
    assert len(sqs.sent) == 1


@pytest.mark.parametrize(("case", "refusal"), _PRECONDITIONS)
def test_state_that_moves_after_the_evidence_stops_the_first_send(case: str, refusal: str) -> None:
    """B3: the first send re-reads every precondition after the assessment that built it."""
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None
    _tamper(world, sqs, case)
    outcome = send()
    assert (outcome["stopped"], outcome["sent"], outcome["complete"]) == ("state_moved", [], False)
    assert refusal in outcome["refusals"]
    assert outcome["not_attempted"] == [TRAFFIC, TOTAL]
    assert sqs.sent == []


def test_a_plan_generation_that_moves_after_the_evidence_stops_the_first_send() -> None:
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None
    world.item(ONLY_COPY.key, META_RECORD_KEY)["generation"] = _low(1)
    outcome = send()
    assert outcome["stopped"] == "state_moved"
    assert "plan_message_identity_moved_since_the_evidence" in outcome["refusals"]
    assert sqs.sent == []


def test_the_control_send_records_the_queue_depths_read_before_each_send() -> None:
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None
    outcome = send()
    assert [item["queue_depth_before"] for item in outcome["sent"]] == [
        {"semantic_write_queue": 0, "semantic_write_dlq": 0},
        {"semantic_write_queue": 1, "semantic_write_dlq": 0},
    ]


@pytest.mark.parametrize("queue", [QUEUE, DLQ])
def test_a_foreign_message_after_the_first_send_stops_the_next(queue: str) -> None:
    """The queue may hold only this run's messages before a later send, and the DLQ none."""
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None

    def foreign(count: int) -> None:
        if count == 1:
            sqs.depth[queue] += 1

    sqs.after_send = foreign
    outcome = send()
    assert [item["unit_key"] for item in outcome["sent"]] == [TRAFFIC]
    assert (outcome["stopped"], outcome["refusals"]) == (
        "state_moved",
        ["semantic_write_queue_not_empty"],
    )
    assert outcome["not_attempted"] == [TOTAL]
    assert len(sqs.sent) == 1


def test_a_refresh_failure_after_a_partial_send_is_a_named_stop_that_keeps_the_sent_unit() -> None:
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None

    def broken(count: int) -> None:
        if count == 1:
            sqs.fail_depth = True

    sqs.after_send = broken
    outcome = send()
    assert [item["unit_key"] for item in outcome["sent"]] == [TRAFFIC]
    assert (outcome["stopped"], outcome["error_class"]) == ("refresh_failed", "RuntimeError")
    assert outcome["not_attempted"] == [TOTAL]
    assert outcome["complete"] is False
    assert len(sqs.sent) == 1


def test_a_send_failure_stops_loudly_and_names_the_units_not_attempted() -> None:
    world, sqs = _world()
    _, send = _run(world, sqs, apply=True)
    assert send is not None
    sqs.fail_on = 0
    outcome = send()
    assert outcome["sent"] == []
    assert (outcome["stopped"], outcome["failed_unit"]) == ("send_failed", TRAFFIC)
    assert outcome["error_class"] == "DependencyError"
    assert outcome["not_attempted"] == [TOTAL]
    assert outcome["complete"] is False


# --- The command ----------------------------------------------------------------------------------


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--profile",
        "operator",
        "--env",
        "test",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--evidence-dir",
        str(tmp_path),
        *extra,
    ]


def test_the_command_is_a_dry_run_unless_apply_and_needs_one_exact_plan_id(
    tmp_path: Path,
) -> None:
    parser = requeue.build_parser()
    assert parser.parse_args(_argv(tmp_path, "--plan-id", ONLY_COPY.key)).apply is False
    assert parser.parse_args(_argv(tmp_path, "--plan-id", ONLY_COPY.key, "--apply")).apply
    for bad in ([], ["--plan-id", "WRITE_PLAN#short"], ["--plan-id", ONLY_COPY.digest]):
        with pytest.raises(SystemExit):
            parser.parse_args(_argv(tmp_path, *bad))


def test_the_command_writes_evidence_then_the_outcome_and_exits_on_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, sqs = _world()

    class _Session:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, name: str) -> Any:
            if name == "sts":
                return SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})
            return world.table if name == "dynamodb" else sqs

    def targets(*_args: object) -> supersede.Targets:
        return TARGETS

    monkeypatch.setattr(requeue, "_targets", targets)
    code = requeue.main(
        _argv(tmp_path, "--plan-id", ONLY_COPY.key, "--apply"),
        session_factory=_Session,
        clock=lambda: AT,
        run_id=lambda: "run",
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert [item["unit_key"] for item in summary["sent"]] == [TRAFFIC, TOTAL]
    evidence = json.loads(Path(summary["evidence"]).read_text(encoding="utf-8"))
    assert evidence["requeueable"] is True
    assert len(evidence["units"]) == 2
    outcome = json.loads(Path(summary["outcome"]).read_text(encoding="utf-8"))
    assert outcome["complete"] is True
    assert outcome["evidence"] == summary["evidence"]
    # Control: the same command again is refused (the messages wait) and sends nothing more.
    code = requeue.main(
        _argv(tmp_path, "--plan-id", ONLY_COPY.key, "--apply"),
        session_factory=_Session,
        clock=lambda: AT,
        run_id=lambda: "run-2",
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["refusals"] == ["semantic_write_queue_not_empty"]
    assert len(sqs.sent) == 2


def test_a_refresh_failure_mid_apply_is_recorded_in_the_outcome_file_not_left_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, sqs = _world()

    def broken(count: int) -> None:
        if count == 1:
            sqs.fail_depth = True

    sqs.after_send = broken

    class _Session:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def client(self, name: str) -> Any:
            if name == "sts":
                return SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})
            return world.table if name == "dynamodb" else sqs

    def targets(*_args: object) -> supersede.Targets:
        return TARGETS

    monkeypatch.setattr(requeue, "_targets", targets)
    code = requeue.main(
        _argv(tmp_path, "--plan-id", ONLY_COPY.key, "--apply"),
        session_factory=_Session,
        clock=lambda: AT,
        run_id=lambda: "run",
    )
    assert code == 1
    summary = json.loads(capsys.readouterr().out)
    assert (summary["stopped"], summary["not_attempted"]) == ("refresh_failed", [TOTAL])
    outcome = json.loads(Path(summary["outcome"]).read_text(encoding="utf-8"))
    assert "status" not in outcome
    assert [item["unit_key"] for item in outcome["sent"]] == [TRAFFIC]
    assert (outcome["stopped"], outcome["complete"]) == ("refresh_failed", False)
