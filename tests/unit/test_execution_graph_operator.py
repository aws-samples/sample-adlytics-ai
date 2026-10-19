# SPDX-License-Identifier: MIT-0
"""Human-only operator commands (V-3) and the S1 mode CAS table.

An agent principal is refused before any write (`AGENT_MUTATION_NOT_ENABLED`); the human
operator's `set_mode` commits exactly one command receipt with the CAS. S6 enables the drain,
abort and GRAPH transitions; GRAPH is entered only through the zero-proof command
(`test_execution_graph_cutover.py`), and a CAS against a moved revision is refused without writing.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, cast

import pytest
from execution_graph_support import FakeDynamo
from test_run_request_admission import (
    FakeManifests,
    _NoLegacy,  # pyright: ignore[reportPrivateUsage]
    fixed_now,
    graph_store,
    key,
)

from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    LegacyLookupUnavailableError,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph import operator
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
    ModeGuardError,
    ModeState,
    PrincipalKind,
    classify_principal,
    require_human_principal,
    validate_mode_transition,
)
from domain.operations.ingestion_root import window_identity
from ports.outbound.execution_graph import AdmissionConflictError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

HUMAN = "arn:aws:iam::000000000000:user/operator"
AGENTS = (
    "arn:aws:sts::000000000000:assumed-role/adlytics-dev-AgentRole/session",
    "arn:aws:sts::000000000000:assumed-role/adlytics-dev-RunRequestsFunctionServiceRole/x",
    "arn:aws:iam::000000000000:role/some-role",
)


def test_v3_human_and_agent_principals_are_classified() -> None:
    assert classify_principal(HUMAN) is PrincipalKind.HUMAN
    assert (
        classify_principal("arn:aws:sts::000000000000:assumed-role/AWSReservedSSO_Admin_1/me")
        is PrincipalKind.HUMAN
    )
    for agent in AGENTS:
        assert classify_principal(agent) is PrincipalKind.AGENT
        with pytest.raises(GraphCommandRefusedError) as raised:
            require_human_principal(agent)
        assert raised.value.signal == "AGENT_MUTATION_NOT_ENABLED"


def _store() -> tuple[FakeDynamo, DynamoExecutionGraphStore]:
    client = FakeDynamo()
    return client, DynamoExecutionGraphStore("graph", client)  # type: ignore[arg-type]


def _context(store: DynamoExecutionGraphStore) -> Any:
    return operator._Context(None, store, HUMAN)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage, reportArgumentType]


def _arguments(**values: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "command": "set-mode",
        "command_id": "command-0001",
        "to": "SHADOW",
        "expected_revision": 0,
        "protocol_version": "v1",
    }
    base.update(values)
    return argparse.Namespace(**base)


def test_v3_the_human_set_mode_commits_one_command_receipt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, store = _store()
    assert operator._set_mode(_context(store), _arguments()) == 0  # pyright: ignore[reportPrivateUsage]
    assert store.read_mode() == ModeState(AuthorityMode.SHADOW, 1)
    commands = [key for key in client.items if key[0].startswith("COMMAND#")]
    assert commands == [("COMMAND#command-0001", "COMMAND")]
    receipt = client.items[commands[0]]
    assert receipt["authority_epoch"] == {"N": "0"}
    assert receipt["mode"] == {"S": "LEGACY"}
    printed = capsys.readouterr().out.strip().removeprefix("RESULT ")
    assert json.loads(printed)["mode"] == "SHADOW"


def test_a_plain_set_mode_into_graph_is_refused_without_writing() -> None:
    """S6: GRAPH is entered only through the zero-proof command, never a plain CAS."""
    client, store = _store()
    client.items[("MODE#ingestion", "MODE")] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": "LEGACY_DRAINING"},
        "revision": {"N": "3"},
    }
    before = dict(client.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        operator._set_mode(_context(store), _arguments(to="GRAPH", expected_revision=3))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.signal == "CUTOVER_REQUIRES_ZERO_PROOF"
    assert client.items == before


def test_v3_an_agent_set_mode_or_admit_is_refused_before_any_write() -> None:
    for agent in AGENTS:
        with pytest.raises(GraphCommandRefusedError):
            require_human_principal(agent)


@pytest.mark.parametrize(
    ("current", "to", "signal"),
    [
        (AuthorityMode.LEGACY, AuthorityMode.GRAPH, "MODE_TRANSITION_NOT_PERMITTED"),
        (AuthorityMode.GRAPH, AuthorityMode.LEGACY, "MODE_TRANSITION_NOT_PERMITTED"),
        (AuthorityMode.SHADOW, AuthorityMode.SHADOW, "MODE_TRANSITION_NOT_PERMITTED"),
    ],
)
def test_transitions_outside_the_table_are_refused(
    current: AuthorityMode, to: AuthorityMode, signal: str
) -> None:
    with pytest.raises(GraphCommandRefusedError) as raised:
        validate_mode_transition(ModeState(current, 3), to, 3)
    assert raised.value.signal == signal


@pytest.mark.parametrize(
    ("current", "to"),
    [
        (AuthorityMode.LEGACY, AuthorityMode.LEGACY_DRAINING),
        (AuthorityMode.SHADOW, AuthorityMode.LEGACY_DRAINING),
        (AuthorityMode.LEGACY_DRAINING, AuthorityMode.LEGACY),
        (AuthorityMode.LEGACY_DRAINING, AuthorityMode.GRAPH),
    ],
)
def test_s6_enables_the_drain_abort_and_graph_transitions(
    current: AuthorityMode, to: AuthorityMode
) -> None:
    validate_mode_transition(ModeState(current, 3), to, 3)


def test_a_mode_cas_against_a_moved_revision_is_refused_without_writing() -> None:
    client, store = _store()
    with pytest.raises(ModeGuardError):
        operator._set_mode(_context(store), _arguments(expected_revision=5))  # pyright: ignore[reportPrivateUsage]
    assert client.items == {}
    validate_mode_transition(ModeState(AuthorityMode.SHADOW, 1), AuthorityMode.LEGACY, 1)


def test_a_non_v1_set_mode_is_refused_before_any_read() -> None:
    client, store = _store()
    with pytest.raises(GraphCommandRefusedError) as raised:
        operator._set_mode(_context(store), _arguments(protocol_version="v2"))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.signal == "PROTOCOL_MIGRATION_UNSUPPORTED"
    assert client.items == {}


def _fence_arguments(**values: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "command": "set-rolling-freshness-fence",
        "command_id": "acceptance-0001",
        "expected_mode_revision": 12,
        "protocol_version": "v1",
        "env": "dev",
    }
    base.update(values)
    return argparse.Namespace(**base)


def _held_graph_12(client: FakeDynamo) -> None:
    set_mode(client, AuthorityMode.GRAPH, hold=True)
    client.items[("MODE#ingestion", "MODE")]["revision"] = {"N": "12"}


def test_rolling_freshness_fence_set_show_and_exact_replay_are_strong_and_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, store = _store()
    _held_graph_12(client)
    context = _context(store)

    assert operator._set_rolling_freshness_fence(context, _fence_arguments()) == 0  # pyright: ignore[reportPrivateUsage]
    assert len(client.transactions) == 1
    transaction = client.transactions[0]
    assert [next(iter(member)) for member in transaction] == ["ConditionCheck", "Put", "Put"]
    assert transaction[0]["ConditionCheck"]["Key"] == {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
    }
    fence = client.items[("MIGRATION#GRAPH_ROLLING_FRESHNESS", "FENCE")]
    assert fence["state"] == {"S": "SET"}
    assert fence["migration_id"] == {"S": "acceptance-0001"}
    assert fence["owner"] == {"S": HUMAN}
    assert fence["revision"] == {"N": "12"}

    printed = json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))
    assert (printed["mode"], printed["mode_revision"], printed["hold"]) == ("GRAPH", 12, True)
    assert printed["receipt"]["command"] == "set_rolling_freshness_fence"

    assert operator._set_rolling_freshness_fence(context, _fence_arguments()) == 0  # pyright: ignore[reportPrivateUsage]
    assert len(client.transactions) == 1
    replay = json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))
    assert replay["replay"] is True

    assert (
        operator._show_rolling_freshness_fence(  # pyright: ignore[reportPrivateUsage]
            context,
            argparse.Namespace(
                command="show-rolling-freshness-fence", migration_id="acceptance-0001"
            ),
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))
    assert shown["fence"] == replay["fence"]
    assert shown["receipt"] == replay["receipt"]


@pytest.mark.parametrize(
    ("mode", "hold", "revision"),
    [
        (AuthorityMode.SHADOW, True, 12),
        (AuthorityMode.GRAPH, False, 12),
        (AuthorityMode.GRAPH, True, 11),
    ],
)
def test_rolling_freshness_fence_refuses_wrong_mode_hold_or_revision_without_write(
    mode: AuthorityMode, hold: bool, revision: int
) -> None:
    client, store = _store()
    set_mode(client, mode, hold=hold)
    client.items[("MODE#ingestion", "MODE")]["revision"] = {"N": str(revision)}
    with pytest.raises(AdmissionConflictError) as raised:
        operator._set_rolling_freshness_fence(_context(store), _fence_arguments())  # pyright: ignore[reportPrivateUsage]
    assert raised.value.signal == "MODE_REVISION_CHANGED"
    assert ("MIGRATION#GRAPH_ROLLING_FRESHNESS", "FENCE") not in client.items
    assert not any(pk.startswith("COMMAND#acceptance-0001") for pk, _ in client.items)


def test_rolling_freshness_fence_refuses_mismatched_reuse_without_write() -> None:
    client, store = _store()
    _held_graph_12(client)
    context = _context(store)
    operator._set_rolling_freshness_fence(context, _fence_arguments())  # pyright: ignore[reportPrivateUsage]
    before = dict(client.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        operator._set_rolling_freshness_fence(  # pyright: ignore[reportPrivateUsage]
            context, _fence_arguments(command_id="acceptance-0002")
        )
    assert raised.value.signal == "MIGRATION_FENCE_SET"
    assert str(raised.value.code) == "ADLY-EXEC_GRAPH-0036"
    assert client.items == before


# --- S3: run and hold commands (V-14, V-29, BF-2) ------------------------------------------------

from execution_graph_dispatch_support import (  # noqa: E402
    instantiate,
    kinds,
    now_text,
    seed_run,
    set_mode,
)
from execution_graph_dispatch_support import (  # noqa: E402
    store as dispatch_store,
)

from adapters.jobs.execution_graph.commands import (  # noqa: E402
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from domain.operations.run_request import RunState  # noqa: E402


def _run_command(kind: RunCommandKind, run: str | None = None, command_id: str = "cmd-00001"):
    return OperatorCommand(kind, command_id, HUMAN, now_text, run_id=run)


def _receipts(client: FakeDynamo) -> list[dict[str, Any]]:
    return [item for (pk, _sk), item in client.items.items() if pk.startswith("COMMAND#")]


@pytest.mark.parametrize(
    "mode", [AuthorityMode.LEGACY, AuthorityMode.SHADOW, AuthorityMode.LEGACY_DRAINING]
)
@pytest.mark.parametrize("kind", [RunCommandKind.SET_HOLD, RunCommandKind.CLEAR_HOLD])
def test_v14b_the_hold_outside_graph_writes_one_epoch_0_refusal_and_changes_nothing(
    mode: AuthorityMode, kind: RunCommandKind
) -> None:
    client = FakeDynamo()
    set_mode(client, mode)
    before = dict(client.items[("MODE#ingestion", "MODE")])
    result = run_operator_command(dispatch_store(client), _run_command(kind))
    assert not result.committed
    assert result.details["signal"] == "AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH"
    assert client.items[("MODE#ingestion", "MODE")] == before
    (receipt,) = _receipts(client)
    assert receipt["authority_epoch"] == {"N": "0"}
    assert receipt["outcome"] == {"S": "refused:AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH"}
    assert len(client.transactions) == 1


def test_v14b_positive_in_graph_the_hold_commits_one_receipt_and_clearing_records_it() -> None:
    client = FakeDynamo()
    set_mode(client, AuthorityMode.GRAPH)
    store = dispatch_store(client)
    assert run_operator_command(store, _run_command(RunCommandKind.SET_HOLD)).committed
    assert store.read_mode().hold
    cleared = run_operator_command(
        store, _run_command(RunCommandKind.CLEAR_HOLD, command_id="cmd-00002")
    )
    assert cleared.committed
    mode = store.read_mode()
    assert not mode.hold
    assert mode.hold_cleared_at == now_text()
    assert len(_receipts(client)) == 2


@pytest.mark.parametrize("kind", [RunCommandKind.PAUSE, RunCommandKind.CANCEL])
def test_v29_pause_and_cancel_against_a_pending_grant_write_one_refusal_receipt(
    kind: RunCommandKind,
) -> None:
    client = FakeDynamo()
    set_mode(client, AuthorityMode.GRAPH)
    run = seed_run(client, grant=True).record.run_id
    before = dict(client.items[(f"RUN#{run}", "RUN")])
    result = run_operator_command(dispatch_store(client), _run_command(kind, run))
    assert not result.committed
    assert result.details["signal"] == "RUN_GRANT_PENDING_APPLY"
    assert client.items[(f"RUN#{run}", "RUN")] == before
    assert len(_receipts(client)) == 1
    assert kinds(client, "PROMOTE#") == []


def test_v29_a_grant_racing_a_pause_is_refused_from_the_runs_old_image() -> None:
    client = FakeDynamo()
    set_mode(client, AuthorityMode.GRAPH)
    run = seed_run(client).record.run_id

    def grant(_client: object) -> None:
        client.items[(f"RUN#{run}", "RUN")]["grant_id"] = {"S": "grant-0001"}

    client.before_transaction = grant
    result = run_operator_command(dispatch_store(client), _run_command(RunCommandKind.PAUSE, run))
    assert result.details["signal"] == "RUN_GRANT_PENDING_APPLY"
    current = dispatch_store(client).read_run(run)
    assert current is not None and current.state is RunState.OPEN


def test_pause_then_resume_advances_the_generation_twice_with_one_receipt_each() -> None:
    client = FakeDynamo()
    set_mode(client, AuthorityMode.GRAPH)
    run = seed_run(client).record.run_id
    instantiate(client, run, "write_a")
    store = dispatch_store(client)
    assert run_operator_command(store, _run_command(RunCommandKind.PAUSE, run)).committed
    resumed = run_operator_command(
        store, _run_command(RunCommandKind.RESUME, run, command_id="cmd-00002")
    )
    assert resumed.committed
    current = store.read_run(run)
    assert current is not None
    assert (current.state, current.dispatch_generation) == (RunState.OPEN, 2)
    assert len(_receipts(client)) == 2


_TABLE_PATH = "/adlytics/dev/operations/execution-graph-table-name"


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "000000000000", "Arn": AGENTS[0]}


class _Ssm:
    def get_parameter(self, **_kwargs: object) -> dict[str, dict[str, str]]:
        return {"Parameter": {"Value": "graph"}}


class _Cfn:
    """Two ListStackResources pages: the table on the first, its SSM parameter on the second."""

    def get_paginator(self, operation: str) -> _Cfn:
        assert operation == "list_stack_resources"
        return self

    def paginate(self, **_kwargs: object) -> list[dict[str, list[dict[str, str]]]]:
        return [
            {
                "StackResourceSummaries": [
                    {"ResourceType": "AWS::DynamoDB::Table", "PhysicalResourceId": "graph"}
                ]
            },
            {
                "StackResourceSummaries": [
                    {"ResourceType": "AWS::SSM::Parameter", "PhysicalResourceId": _TABLE_PATH}
                ]
            },
        ]


class _Session:
    """A boto3 session stand-in whose caller is an agent role (V-3)."""

    def __init__(self, client: FakeDynamo) -> None:
        self.clients: dict[str, object] = {
            "sts": _Sts(),
            "ssm": _Ssm(),
            "cloudformation": _Cfn(),
            "dynamodb": client,
        }

    def client(self, name: str) -> object:
        return self.clients[name]


def test_v3_an_agent_cannot_set_the_rolling_freshness_fence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeDynamo()
    _held_graph_12(client)

    def session(**_kwargs: object) -> _Session:
        return _Session(client)

    monkeypatch.setattr(operator.boto3, "Session", session)
    assert (
        operator.main(
            [
                "--profile",
                "operator",
                "--region",
                "us-east-1",
                "--expected-account",
                "000000000000",
                "--env",
                "dev",
                "--store-stack",
                "store",
                "set-rolling-freshness-fence",
                "--command-id",
                "acceptance-0001",
                "--expected-mode-revision",
                "12",
            ]
        )
        == operator.REFUSED_EXIT
    )
    printed = json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))
    assert printed["refused"] == "AGENT_MUTATION_NOT_ENABLED"
    assert client.transactions == []


@pytest.mark.parametrize("command", ["pause", "resume", "cancel", "set-hold", "clear-hold"])
def test_v3_an_agent_run_or_hold_command_is_refused_before_any_write(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeDynamo()
    set_mode(client, AuthorityMode.GRAPH)

    def session(**_kwargs: object) -> _Session:
        return _Session(client)

    monkeypatch.setattr(operator.boto3, "Session", session)
    arguments = [
        "--profile",
        "operator",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--env",
        "dev",
        "--store-stack",
        "store",
        command,
        "--command-id",
        "cmd-00001",
    ]
    if command in {"pause", "resume", "cancel"}:
        arguments += ["--run-id", "run-1"]
    assert operator.main(arguments) == operator.REFUSED_EXIT
    printed = json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))
    assert printed["refused"] == "AGENT_MUTATION_NOT_ENABLED"
    assert client.transactions == []


# S6 part 2 (design §2.12): `dlq-status` reads and `redrive` moves, over a fake SQS client.

from adapters.jobs.execution_graph.dead_letters import (  # noqa: E402
    DEAD_LETTER_QUEUES,
    dead_letter_route,
    dlq_status,
    redrive,
)

_SQS_READS = frozenset({"get_queue_url", "get_queue_attributes", "list_message_move_tasks"})


class _FakeSqs:
    """Queues by name with visible and in-flight counts; records every call it receives."""

    def __init__(
        self,
        visible: dict[str, int] | None = None,
        in_flight: dict[str, int] | None = None,
        moves: dict[str, str] | None = None,
    ) -> None:
        self.visible = visible or {}
        self.in_flight = in_flight or {}
        # The latest move-task status, by the move's source (dead-letter) queue name.
        self.moves = moves or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_queue_url(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("get_queue_url", kwargs))
        return {"QueueUrl": f"https://sqs.example/000000000000/{kwargs['QueueName']}"}

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, dict[str, str]]:
        self.calls.append(("get_queue_attributes", kwargs))
        name = str(kwargs["QueueUrl"]).rsplit("/", 1)[-1]
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(self.visible.get(name, 0)),
                "ApproximateNumberOfMessagesNotVisible": str(self.in_flight.get(name, 0)),
                "QueueArn": f"arn:aws:sqs:us-east-1:000000000000:{name}",
            }
        }

    def list_message_move_tasks(self, **kwargs: Any) -> dict[str, list[dict[str, object]]]:
        self.calls.append(("list_message_move_tasks", kwargs))
        status = self.moves.get(str(kwargs["SourceArn"]).rsplit(":", 1)[-1])
        return {"Results": [] if status is None else [{"Status": status}]}

    def start_message_move_task(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("start_message_move_task", kwargs))
        return {"TaskHandle": "task-handle-1"}

    def names(self) -> set[str]:
        return {name for name, _ in self.calls}


def test_dlq_status_reads_the_four_dead_letter_queues_and_writes_nothing() -> None:
    sqs = _FakeSqs({"adlytics-dev-ingestion-dlq": 1})
    result = dlq_status(sqs, "dev")
    queues = cast("dict[str, dict[str, Any]]", result["queues"])
    assert sorted(queues) == ["bake", "delivery", "precompute", "semantic-write"]
    assert queues["delivery"]["visible"] == 1
    assert queues["delivery"]["dead_letter_queue"] == "adlytics-dev-ingestion-dlq"
    assert queues["precompute"]["dead_letter_queue"] == "adlytics-dev-insight-precompute-dlq.fifo"
    assert all(queues[name]["visible"] == 0 for name in ("bake", "precompute", "semantic-write"))
    assert sqs.names() <= _SQS_READS


def test_redrive_refuses_an_unknown_queue_before_any_sqs_call() -> None:
    sqs = _FakeSqs()
    with pytest.raises(GraphCommandRefusedError) as refused:
        dead_letter_route("dev", "registration")
    assert refused.value.signal == "REDRIVE_QUEUE_UNKNOWN"
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0031"
    assert sqs.calls == []
    # Paired control: each of the four graph routes resolves to its deterministic names.
    assert {name: dead_letter_route("dev", name).source for name in DEAD_LETTER_QUEUES} == {
        "delivery": "adlytics-dev-ingestion",
        "semantic-write": "adlytics-dev-semantic-write.fifo",
        "bake": "adlytics-dev-preset-bake.fifo",
        "precompute": "adlytics-dev-insight-precompute.fifo",
    }


def test_redrive_refuses_a_count_mismatch_and_starts_no_move() -> None:
    sqs = _FakeSqs({"adlytics-dev-preset-bake-dlq.fifo": 2})
    with pytest.raises(GraphCommandRefusedError) as refused:
        redrive(sqs, dead_letter_route("dev", "bake"), 1)
    assert refused.value.signal == "REDRIVE_COUNT_MISMATCH"
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0032"
    assert "start_message_move_task" not in sqs.names()


def test_redrive_refuses_an_in_flight_dead_letter_and_starts_no_move() -> None:
    dlq = "adlytics-dev-preset-bake-dlq.fifo"
    sqs = _FakeSqs({dlq: 2}, in_flight={dlq: 1})
    with pytest.raises(GraphCommandRefusedError) as refused:
        redrive(sqs, dead_letter_route("dev", "bake"), 2)
    assert refused.value.signal == "REDRIVE_IN_FLIGHT"
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0033"
    assert "start_message_move_task" not in sqs.names()
    # Paired control: the same count with nothing in flight moves.
    assert redrive(_FakeSqs({dlq: 2}), dead_letter_route("dev", "bake"), 2)["started"] is True


@pytest.mark.parametrize("status", ["RUNNING", "CANCELLING"])
def test_redrive_refuses_while_the_latest_move_task_is_active(status: str) -> None:
    dlq = "adlytics-dev-preset-bake-dlq.fifo"
    sqs = _FakeSqs({dlq: 2}, moves={dlq: status})
    with pytest.raises(GraphCommandRefusedError) as refused:
        redrive(sqs, dead_letter_route("dev", "bake"), 2)
    assert refused.value.signal == "REDRIVE_MOVE_ACTIVE"
    assert str(refused.value.code) == "ADLY-EXEC_GRAPH-0034"
    assert "start_message_move_task" not in sqs.names()


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED", "FAILED"])
def test_redrive_after_a_terminal_move_task_moves(status: str) -> None:
    dlq = "adlytics-dev-preset-bake-dlq.fifo"
    sqs = _FakeSqs({dlq: 2}, moves={dlq: status})
    result = redrive(sqs, dead_letter_route("dev", "bake"), 2)
    assert result["started"] is True
    assert result["latest_move_task"] == {"status": status}


def test_redrive_at_the_matching_count_moves_the_dead_letters_back_to_their_source() -> None:
    sqs = _FakeSqs({"adlytics-dev-preset-bake-dlq.fifo": 2})
    result = redrive(sqs, dead_letter_route("dev", "bake"), 2)
    assert (result["started"], result["task_handle"], result["visible"]) == (
        True,
        "task-handle-1",
        2,
    )
    (move,) = [kwargs for name, kwargs in sqs.calls if name == "start_message_move_task"]
    assert move == {
        "SourceArn": "arn:aws:sqs:us-east-1:000000000000:adlytics-dev-preset-bake-dlq.fifo",
        "DestinationArn": "arn:aws:sqs:us-east-1:000000000000:adlytics-dev-preset-bake.fifo",
    }


def test_redrive_of_an_empty_dead_letter_queue_at_zero_starts_nothing() -> None:
    sqs = _FakeSqs()
    result = redrive(sqs, dead_letter_route("dev", "precompute"), 0)
    assert (result["started"], result["task_handle"]) == (False, None)
    assert "start_message_move_task" not in sqs.names()


class _DeadLetterSession:
    """A boto3 session stand-in: one caller and SQS only (the graph table is never resolved)."""

    def __init__(self, arn: str, sqs: _FakeSqs) -> None:
        self.arn, self.sqs = arn, sqs

    def client(self, name: str) -> object:
        if name == "sts":
            arn = self.arn

            class _Caller:
                def get_caller_identity(self) -> dict[str, str]:
                    return {"Account": "000000000000", "Arn": arn}

            return _Caller()
        assert name == "sqs", name
        return self.sqs


def _main(arn: str, sqs: _FakeSqs, monkeypatch: pytest.MonkeyPatch, *command: str) -> int:
    def session(**_kwargs: object) -> _DeadLetterSession:
        return _DeadLetterSession(arn, sqs)

    monkeypatch.setattr(operator.boto3, "Session", session)
    return operator.main(
        [
            "--profile",
            "operator",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
            "--store-stack",
            "store",
            *command,
        ]
    )


def _printed(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().removeprefix("RESULT "))


def test_the_cli_prints_one_result_for_dlq_status_and_a_matching_redrive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sqs = _FakeSqs({"adlytics-dev-semantic-write-dlq.fifo": 1})
    assert _main(HUMAN, sqs, monkeypatch, "dlq-status") == 0
    assert _printed(capsys)["queues"]["semantic-write"]["visible"] == 1
    assert sqs.names() <= _SQS_READS
    command = ("redrive", "--queue", "semantic-write", "--expected-count", "1")
    assert _main(HUMAN, sqs, monkeypatch, *command) == 0
    assert _printed(capsys)["started"] is True


@pytest.mark.parametrize(
    "case",
    [("registration", "1", "REDRIVE_QUEUE_UNKNOWN"), ("delivery", "3", "REDRIVE_COUNT_MISMATCH")],
)
def test_the_cli_refuses_redrive_with_exit_3_and_its_signal(
    case: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queue, count, signal = case
    sqs = _FakeSqs({"adlytics-dev-ingestion-dlq": 1})
    command = ("redrive", "--queue", queue, "--expected-count", count)
    assert _main(HUMAN, sqs, monkeypatch, *command) == operator.REFUSED_EXIT
    assert _printed(capsys) == {"command": "redrive", "refused": signal}
    assert "start_message_move_task" not in sqs.names()


def test_an_agent_redrive_is_refused_before_any_sqs_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sqs = _FakeSqs({"adlytics-dev-ingestion-dlq": 1})
    command = ("redrive", "--queue", "delivery", "--expected-count", "1")
    assert _main(AGENTS[0], sqs, monkeypatch, *command) == operator.REFUSED_EXIT
    assert _printed(capsys)["refused"] == "AGENT_MUTATION_NOT_ENABLED"
    assert sqs.calls == []


class _Clients:
    """A boto3 session stand-in: every client is a named sentinel (no AWS call is made)."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def client(self, name: str) -> object:
        self.made.append(name)
        return f"client:{name}"


def test_p6e8_the_operator_admit_wires_the_legacy_lookup_epoch_1_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6E-8: the operator's admit wires the legacy lookup the probe twin wires.

    In GRAPH (epoch 1) admission reads row 9 and the legacy head (`land_fixtures/twin.py`).
    """
    resolved: list[str] = []

    def fake_resolve(environment: str, _cloudformation: object, _ssm: object) -> dict[str, str]:
        resolved.append(environment)
        return {"publications": "publications-table", "leases": "leases-table"}

    monkeypatch.setattr(operator, "resolve_named_semantic_resources", fake_resolve)
    _client, store = _store()
    session = _Clients()
    deps = operator.admission_dependencies(
        session,  # type: ignore[arg-type]
        store,
        argparse.Namespace(env="dev", bucket="raw-bucket"),
    )
    assert isinstance(deps.legacy, S3CoordinatorLegacyHead)
    assert deps.store is store
    assert resolved == ["dev"]
    assert {"s3", "dynamodb", "cloudformation", "ssm"} <= set(session.made)


def test_p6e8_control_epoch_1_admission_without_the_legacy_lookup_refuses() -> None:
    """Control: epoch-1 admission without the legacy lookup refuses before any write.

    These are the pre-P6E-8 operator dependencies (the live 02:55:46Z `ADLY-RUN_REQUEST-0013`);
    the same request with a legacy lookup is admitted.
    """
    manifest = key("20261006T092520Z")
    request = RunRequest(
        request_id="request-p6e8-01",
        protocol_version="v1",
        caller_kind="on_demand",
        reason="bounded_validation",
        manifest_key=manifest,
        version_id="v1",
        requester=HUMAN,
        requester_is_iam=True,
    )
    manifests = FakeManifests(retrieved_at=window_identity(manifest).retrieved_at)
    client, store = graph_store(AuthorityMode.GRAPH)
    before = dict(client.items)
    with pytest.raises(LegacyLookupUnavailableError):
        admit_run_request(request, AdmissionDependencies(store, manifests, fixed_now))
    assert client.items == before
    admitted = admit_run_request(
        request, AdmissionDependencies(store, manifests, fixed_now, legacy=_NoLegacy())
    )
    assert (admitted.decision, admitted.authority_epoch) == ("ADMITTED", 1)
