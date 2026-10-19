# SPDX-License-Identifier: MIT-0
"""S6 drain-to-zero cutover (V-17, V-17b, V-18; design.md, "Legacy cutover", steps 3-6).

The `GRAPH` CAS, the hold and `graph_entered_at` commit in one transaction that ConditionChecks
`DRAIN_SEQ` in the coordinator table; the abort is legal only while `graph_entered_at` is absent;
a counted enumeration before drain CAS + `T_max` + margin is refused; `D` is bounded both ways.
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words
from execution_graph_support import (
    FakeDynamo,
    _apply_update,  # pyright: ignore[reportPrivateUsage]
    _Condition,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.data_flow_registry.execution_graphs import (
    ExecutionGraphContractError,
    t_max_seconds,
)
from adapters.jobs.execution_graph import operator
from adapters.jobs.execution_graph.cutover import (
    CutoverCommand,
    CutoverDependencies,
    abort_drain,
    begin_drain,
    counted_enumeration,
    enter_graph,
    post_graph_enumeration,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore
from adapters.outbound.dynamodb.legacy_families import (
    MAX_SCAN_PAGES,
    DynamoLegacyFamilies,
    FamilyEnumeration,
    ZeroProofUnreadableError,
    classify_families,
)
from domain.operations.cutover import (
    DRAIN_SEQ_PARTITION_KEY,
    DRAIN_SEQ_RECORD_KEY,
    FamilyCounts,
    drain_deadline_bounds,
    drain_wait_visibility_seconds,
    is_last_receive,
    writer_counts_drain,
)
from domain.operations.execution_graph import (
    AuthorityMode,
    GraphCommandRefusedError,
)
from domain.operations.generated_execution_graphs import CUTOVER, T_MAX_SECONDS
from ports.outbound.execution_graph import AdmissionConflictError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

HUMAN = "arn:aws:iam::000000000000:user/operator"
GRAPH = "graph"
COORDINATOR = "publications"
CAS = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
DIGEST = "a" * 64
OTHER = "b" * 64


@guard_reserved_words
@dataclass
class TwoTables:
    """Two fake tables whose transactions span both atomically, as DynamoDB's do.

    The graph table is keyed pk/sk and the coordinator table partition_key/record_key.
    """

    graph: FakeDynamo = field(default_factory=FakeDynamo)
    coordinator: FakeDynamo = field(
        default_factory=lambda: FakeDynamo(key_names=("partition_key", "record_key"))
    )
    during_scan: Any = None

    def _table(self, name: str) -> FakeDynamo:
        return self.graph if name == GRAPH else self.coordinator

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return self._table(kwargs["TableName"]).get_item(**kwargs)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        if self.during_scan is not None:
            hook, self.during_scan = self.during_scan, None
            hook(self)
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        table = self._table(kwargs["TableName"])
        found = [
            copy.deepcopy(item) for (pk, _), item in table.items.items() if pk.startswith(prefix)
        ]
        return {"Items": found}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        members: list[dict[str, Any]] = kwargs["TransactItems"]
        reasons: list[dict[str, Any]] = []
        for member in members:
            ((kind, body),) = member.items()
            table = self._table(body["TableName"])
            key = table._key(body["Item"] if kind == "Put" else body["Key"])  # pyright: ignore[reportPrivateUsage]
            condition = body.get("ConditionExpression")
            ok = condition is None or _Condition(
                condition,
                body.get("ExpressionAttributeNames", {}),
                body.get("ExpressionAttributeValues", {}),
            ).evaluate(table.items.get(key))
            reasons.append({"Code": "None" if ok else "ConditionalCheckFailed"})
        if any(reason["Code"] != "None" for reason in reasons):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                    "CancellationReasons": reasons,  # type: ignore[typeddict-item]
                },
                "TransactWriteItems",
            )
        for member in members:
            ((kind, body),) = member.items()
            table = self._table(body["TableName"])
            if kind == "Put":
                table.items[table._key(body["Item"])] = copy.deepcopy(body["Item"])  # pyright: ignore[reportPrivateUsage]
            elif kind == "Update":
                key = table._key(body["Key"])  # pyright: ignore[reportPrivateUsage]
                item = table.items.setdefault(key, copy.deepcopy(body["Key"]))
                _apply_update(
                    item,
                    body["UpdateExpression"],
                    body.get("ExpressionAttributeNames", {}),
                    body.get("ExpressionAttributeValues", {}),
                )
        return {}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _world(
    mode: str = "SHADOW", revision: int = 1, **mode_fields: str
) -> tuple[TwoTables, CutoverDependencies, Clock]:
    tables = TwoTables()
    item: dict[str, Any] = {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": mode},
        "revision": {"N": str(revision)},
    }
    item |= {name: {"S": value} for name, value in mode_fields.items()}
    tables.graph.items[("MODE#ingestion", "MODE")] = item
    clock = Clock(CAS)
    deps = CutoverDependencies(
        DynamoExecutionGraphStore(GRAPH, tables),  # type: ignore[arg-type]
        DynamoLegacyFamilies(COORDINATOR, tables),  # type: ignore[arg-type]
        clock,
    )
    return tables, deps, clock


def _command(command_id: str = "cutover-0001", revision: int = 1) -> CutoverCommand:
    return CutoverCommand(HUMAN, command_id, revision)


def _fence_end() -> datetime:
    return CAS + timedelta(seconds=T_MAX_SECONDS + int(CUTOVER["drain_fence_margin_seconds"]))


def _drained(tables_deps_clock: tuple[TwoTables, CutoverDependencies, Clock]) -> None:
    tables, deps, clock = tables_deps_clock
    begin_drain(deps, _command(), _fence_end() + timedelta(minutes=30))
    clock.at = _fence_end() + timedelta(seconds=1)
    del tables


def _commands(tables: TwoTables) -> list[dict[str, Any]]:
    return [item for (pk, _), item in tables.graph.items.items() if pk.startswith("COMMAND#")]


def _put(tables: TwoTables, partition: str, record: str, **attributes: str) -> None:
    item: dict[str, Any] = {"partition_key": {"S": partition}, "record_key": {"S": record}}
    item |= {name: {"S": value} for name, value in attributes.items()}
    tables.coordinator.items[(partition, record)] = item


# --- T_max and the deadline bounds ---------------------------------------------------------------


def test_t_max_is_the_largest_inventoried_writer_timeout() -> None:
    writers = CUTOVER["legacy_writers"]
    assert max(int(writer["timeout_seconds"]) for writer in writers) == T_MAX_SECONDS
    assert {str(writer["writer"]) for writer in writers} >= {
        "ingestion_handler",
        "plan_intent_reconciler",
        "table_writer",
    }


def test_t_max_cannot_be_generated_for_a_writer_without_a_timeout() -> None:
    contract = {
        "cutover": {"legacy_writers": [{"writer": "x", "timeout_seconds": 900}, {"writer": "y"}]}
    }
    with pytest.raises(ExecutionGraphContractError, match="T_MAX_NOT_GENERATED"):
        t_max_seconds(contract)
    with pytest.raises(ExecutionGraphContractError, match="T_MAX_NOT_GENERATED"):
        t_max_seconds({"cutover": {"legacy_writers": []}})


def test_the_deadline_bounds_are_the_fence_and_twelve_hours_less_the_visibility() -> None:
    earliest, latest = drain_deadline_bounds(CAS)
    assert earliest == _fence_end()
    assert latest == CAS + timedelta(seconds=43200 - (900 + 60))


@pytest.mark.parametrize(
    "deadline",
    [_fence_end() - timedelta(seconds=1), CAS + timedelta(seconds=43200 - 960 + 1)],
)
def test_a_drain_deadline_outside_its_bounds_is_refused_before_any_write(
    deadline: datetime,
) -> None:
    tables, deps, _ = _world()
    before = copy.deepcopy(tables.graph.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        begin_drain(deps, _command(), deadline)
    assert raised.value.signal == "DRAIN_DEADLINE_OUT_OF_BOUND"
    assert tables.graph.items == before


def test_the_drain_cas_records_d_and_writes_one_epoch_0_receipt() -> None:
    tables, deps, _ = _world()
    deadline = _fence_end() + timedelta(minutes=30)
    receipt = begin_drain(deps, _command(), deadline)
    mode = deps.store.read_mode()
    assert mode.mode is AuthorityMode.LEGACY_DRAINING
    assert mode.drain_entered_at == CAS.isoformat()
    assert mode.drain_deadline == deadline.isoformat()
    assert receipt.authority_epoch == 0
    assert receipt.mode is AuthorityMode.SHADOW
    assert receipt.details["drain_deadline"] == deadline.isoformat()
    assert len(_commands(tables)) == 1


# --- V-17b: the writer-lifetime fence and the consistent cut -------------------------------------


def test_v17b_b_a_counted_enumeration_before_the_fence_is_refused_before_any_cas() -> None:
    world = _world()
    tables, deps, clock = world
    begin_drain(deps, _command(), _fence_end() + timedelta(minutes=30))
    clock.at = _fence_end() - timedelta(seconds=1)
    before = copy.deepcopy(tables.graph.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        enter_graph(deps, _command("cutover-0002", 2))
    assert raised.value.signal == "DRAIN_FENCE_NOT_ELAPSED"
    assert tables.graph.items == before
    with pytest.raises(GraphCommandRefusedError):
        counted_enumeration(deps)


def test_v17b_a_a_commit_during_the_enumeration_refuses_the_graph_cas() -> None:
    """Refuse the CAS when a counted commit lands mid-enumeration.

    A `fail_unit` successor commits during the scan: every family reads zero, but the commit
    moved `DRAIN_SEQ`, so the CAS is refused and nothing is entered.
    """
    world = _world()
    tables, deps, _ = world
    _drained(world)

    def successor_commits(state: TwoTables) -> None:
        state.coordinator.items[(DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY)] = {
            "partition_key": {"S": DRAIN_SEQ_PARTITION_KEY},
            "record_key": {"S": DRAIN_SEQ_RECORD_KEY},
            "seq": {"N": "1"},
        }

    tables.during_scan = successor_commits
    with pytest.raises(AdmissionConflictError) as raised:
        enter_graph(deps, _command("cutover-0002", 2))
    assert raised.value.signal == "DRAIN_SEQ_CHANGED"
    mode = deps.store.read_mode()
    assert mode.mode is AuthorityMode.LEGACY_DRAINING
    assert mode.graph_entered_at is None
    assert not mode.hold
    assert len(_commands(tables)) == 1


def test_v17b_positive_the_cas_commits_with_the_hold_and_graph_entered_at() -> None:
    world = _world()
    tables, deps, clock = world
    _drained(world)
    _put(tables, DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY)
    tables.coordinator.items[(DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY)]["seq"] = {"N": "7"}
    result = enter_graph(deps, _command("cutover-0002", 2))
    mode = deps.store.read_mode()
    assert mode.mode is AuthorityMode.GRAPH
    assert mode.graph_entered_at == clock.at.isoformat()
    assert mode.hold
    assert mode.hold_revision == 1
    assert result.receipt.authority_epoch == 1
    assert result.receipt.details["drain_seq"] == "7"
    assert result.proof.stable
    assert result.post_enumeration is not None
    assert result.post_enumeration.counts.zero
    # The CAS ConditionChecked DRAIN_SEQ in the coordinator table in the same transaction.
    assert tables.coordinator.items[(DRAIN_SEQ_PARTITION_KEY, DRAIN_SEQ_RECORD_KEY)]["seq"] == {
        "N": "7"
    }


def test_open_legacy_work_refuses_the_graph_cas() -> None:
    world = _world()
    tables, deps, _ = world
    _drained(world)
    _put(tables, f"WRITE_PLAN#{DIGEST}#INTENT#{OTHER}", "META", state="pending", intent_id=OTHER)
    before = copy.deepcopy(tables.graph.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        enter_graph(deps, _command("cutover-0002", 2))
    assert raised.value.signal == "DRAIN_NOT_ZERO"
    assert tables.graph.items == before


def test_a_non_zero_post_cas_enumeration_is_reported_with_the_hold_kept() -> None:
    """P6(d'): a stranded legacy root after the CAS is a stop; the hold stays set."""
    world = _world()
    tables, deps, _ = world
    _drained(world)
    original = deps.families.enumerate_families
    calls: list[int] = []

    def enumerate_then_strand() -> FamilyEnumeration:
        calls.append(1)
        if len(calls) == 2:
            _put(tables, f"WRITE_PLAN#{DIGEST}#INTENT#{OTHER}", "META", state="pending")
        return original()

    deps.families.enumerate_families = enumerate_then_strand  # type: ignore[method-assign]
    result = enter_graph(deps, _command("cutover-0002", 2))
    assert result.post_enumeration is not None
    assert not result.post_enumeration.counts.zero
    assert result.post_enumeration.counts.pending_intents == 1
    assert deps.store.read_mode().hold


def test_a_failed_post_cas_read_still_returns_the_committed_receipt() -> None:
    """S6-3: the CAS has committed; the result says the post-CAS read is unread, not zero."""
    world = _world()
    _, deps, _ = world
    _drained(world)
    original = deps.families.enumerate_families
    calls: list[int] = []

    def then_throttled() -> FamilyEnumeration:
        calls.append(1)
        if len(calls) == 2:
            raise ZeroProofUnreadableError("throttled")
        return original()

    deps.families.enumerate_families = then_throttled  # type: ignore[method-assign]
    result = enter_graph(deps, _command("cutover-0002", 2))
    assert result.post_enumeration is None
    assert result.post_unread == "throttled"
    assert result.receipt.authority_epoch == 1
    mode = deps.store.read_mode()
    assert mode.mode is AuthorityMode.GRAPH
    assert mode.hold


def test_the_post_graph_re_read_runs_uncounted_in_graph_and_sees_stranded_work() -> None:
    """S6-3: after the CAS the F1-F4 read can be repeated, with no fence and no counter."""
    world = _world()
    tables, deps, _ = world
    _drained(world)
    enter_graph(deps, _command("cutover-0002", 2))
    assert post_graph_enumeration(deps).enumeration.counts.zero
    _put(tables, f"WRITE_PLAN#{DIGEST}#INTENT#{OTHER}", "META", state="pending")
    before = copy.deepcopy(tables.graph.items)
    read = post_graph_enumeration(deps)
    assert read.enumeration.counts.pending_intents == 1
    assert read.mode.graph_entered_at is not None
    assert tables.graph.items == before


def _operator_with(monkeypatch: pytest.MonkeyPatch, deps: CutoverDependencies) -> Any:
    def resolved(*_: object) -> CutoverDependencies:
        return deps

    monkeypatch.setattr(operator, "_cutover_deps", resolved)
    return operator._Context(None, deps.store, HUMAN)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage, reportArgumentType]


def _results(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = capsys.readouterr().out.splitlines()
    return [
        json.loads(line.removeprefix("RESULT ")) for line in lines if line.startswith("RESULT ")
    ]


def test_enter_graph_prints_the_committed_receipt_and_exits_1_when_post_cas_is_unread(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world()
    _, deps, _ = world
    _drained(world)
    original = deps.families.enumerate_families
    calls: list[int] = []

    def then_throttled() -> FamilyEnumeration:
        calls.append(1)
        if len(calls) == 2:
            raise ZeroProofUnreadableError("throttled")
        return original()

    deps.families.enumerate_families = then_throttled  # type: ignore[method-assign]
    context = _operator_with(monkeypatch, deps)
    arguments = argparse.Namespace(
        command="enter-graph", command_id="cutover-0002", expected_revision=2, protocol_version="v1"
    )
    assert operator._enter_graph(context, arguments) == 1  # pyright: ignore[reportPrivateUsage]
    (printed,) = _results(capsys)
    assert printed["committed"] is True
    assert printed["receipt"]["command_id"] == "cutover-0002"
    assert printed["post_cas"] is None
    assert printed["post_cas_unread"] == "throttled"
    assert printed["mode"] == "GRAPH"


def test_enumerate_in_graph_is_the_uncounted_re_read_and_exits_3_when_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world()
    tables, deps, _ = world
    _drained(world)
    enter_graph(deps, _command("cutover-0002", 2))
    context = _operator_with(monkeypatch, deps)
    arguments = argparse.Namespace(command="enumerate")
    assert operator._cutover_read(context, arguments) == 0  # pyright: ignore[reportPrivateUsage]
    _put(tables, f"WRITE_PLAN#{DIGEST}#INTENT#{OTHER}", "META", state="pending")
    assert operator._cutover_read(context, arguments) == operator.REFUSED_EXIT  # pyright: ignore[reportPrivateUsage]
    zero, stranded = _results(capsys)
    assert (zero["counted"], zero["zero"], zero["mode"]) == (False, True, "GRAPH")
    assert (stranded["counted"], stranded["zero"]) == (False, False)
    assert stranded["families"]["F1_pending_intents"] == 1


@pytest.mark.parametrize(
    ("mode", "fields"),
    [
        ("SHADOW", {}),
        ("LEGACY_DRAINING", {"drain_entered_at": CAS.isoformat()}),
        ("GRAPH", {}),
    ],
)
def test_the_post_graph_re_read_is_refused_outside_an_entered_graph(
    mode: str, fields: dict[str, str]
) -> None:
    _, deps, _ = _world(mode, 3, **fields)
    with pytest.raises(GraphCommandRefusedError) as raised:
        post_graph_enumeration(deps)
    assert raised.value.signal == "CUTOVER_REQUIRES_ZERO_PROOF"


# --- V-18: abort legality ------------------------------------------------------------------------


def test_v18_the_abort_is_refused_once_graph_entered_at_exists() -> None:
    tables, deps, _ = _world("LEGACY_DRAINING", 2, graph_entered_at="2026-10-09T00:00:00+00:00")
    before = copy.deepcopy(tables.graph.items)
    with pytest.raises(GraphCommandRefusedError) as raised:
        abort_drain(deps, _command("abort-00001", 2))
    assert raised.value.signal == "CUTOVER_ABORT_NOT_PERMITTED"
    assert tables.graph.items == before


def test_v18_the_abort_cas_itself_conditions_on_graph_entered_at() -> None:
    """Defense in depth: a graph entry that lands between the read and the CAS refuses it."""
    world = _world()
    tables, deps, _ = world
    _drained(world)
    original = deps.store.read_mode
    read = original()

    def stale_read() -> Any:
        tables.graph.items[("MODE#ingestion", "MODE")]["graph_entered_at"] = {"S": "now"}
        return read

    deps.store.read_mode = stale_read  # type: ignore[method-assign]
    with pytest.raises(AdmissionConflictError):
        abort_drain(deps, _command("abort-00001", 2))
    assert len(_commands(tables)) == 1


def test_v18_positive_the_rehearsal_abort_commits_with_an_epoch_0_receipt() -> None:
    world = _world()
    tables, deps, _ = world
    _drained(world)
    receipt = abort_drain(deps, _command("abort-00001", 2))
    mode = deps.store.read_mode()
    assert mode.mode is AuthorityMode.LEGACY
    assert mode.drain_deadline is None
    assert receipt.authority_epoch == 0
    assert receipt.mode is AuthorityMode.LEGACY_DRAINING
    assert all(item["authority_epoch"] == {"N": "0"} for item in _commands(tables))


# --- F1-F4 classification ------------------------------------------------------------------------


def _item(partition: str, record: str, **attributes: str) -> dict[str, Any]:
    item: dict[str, Any] = {"partition_key": {"S": partition}, "record_key": {"S": record}}
    item |= {name: {"S": value} for name, value in attributes.items()}
    return item


def test_each_family_is_classified_from_base_table_items() -> None:
    intent = f"WRITE_PLAN#{DIGEST}#INTENT#{OTHER}"
    persisted_intent = f"WRITE_PLAN#{DIGEST}#INTENT#{'c' * 64}"
    plan = f"WRITE_PLAN#{DIGEST}#{'d' * 64}"
    done_plan = f"WRITE_PLAN#{DIGEST}#{'e' * 64}"
    items = [
        _item(intent, "META", state="pending", intent_id=OTHER),
        _item(persisted_intent, "META", state="claimed", intent_id="c" * 64),
        _item(
            f"WRITE_PLAN#{DIGEST}#INTENT#{'f' * 64}", "META", state="claimed", intent_id="f" * 64
        ),
        _item(plan, "META", status="planned", plan_digest=DIGEST, origin_intent_id="c" * 64),
        _item(plan, "UNIT#u1", admission="pending", status="queued"),
        _item(plan, "UNIT#u2", admission="admitted", status="writing"),
        _item(plan, "UNIT#u3", admission="admitted", status="attested"),
        _item(done_plan, "META", status="completed", plan_digest=DIGEST, origin_intent_id="z"),
        _item(done_plan, "UNIT#u4", admission="pending", status="queued"),
        _item(f"WRITE_PLAN#{DIGEST}", "SUCCESSOR_OWNER"),
        _item("STATUS_FLOW#x", "HEAD"),
    ]
    found = classify_families(items)
    assert found.counts == FamilyCounts(1, 1, 1, 1)
    assert found.open_keys["F2"] == (f"WRITE_PLAN#{DIGEST}#INTENT#{'f' * 64}",)
    assert found.open_keys["F4"] == (f"{plan}/UNIT#u2",)
    assert classify_families([items[3], items[6], items[7]]).counts.zero


def test_drain_seq_reads_zero_when_absent() -> None:
    _, deps, _ = _world()
    assert deps.families.read_drain_seq() == 0


@guard_reserved_words
@dataclass
class PagedScan:
    """A scan that returns `pages` pages, each with one open intent on the last page only."""

    pages: int
    calls: int = 0

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        assert kwargs["ConsistentRead"] is True
        if self.calls < self.pages:
            return {"Items": [], "LastEvaluatedKey": {"partition_key": {"S": str(self.calls)}}}
        intent = f"WRITE_PLAN#{DIGEST}#INTENT#{'c' * 64}"
        return {
            "Items": [
                {
                    "partition_key": {"S": intent},
                    "record_key": {"S": "META"},
                    "state": {"S": "pending"},
                }
            ]
        }


def test_the_enumeration_reads_a_table_past_the_old_page_bound_to_its_end() -> None:
    # The dev coordinator table read 2,767 pages on 2026-10-11; the old bound was 2,000.
    scan = PagedScan(pages=2767)
    found = DynamoLegacyFamilies(COORDINATOR, scan).enumerate_families()  # type: ignore[arg-type]
    assert found.pages == 2767
    assert scan.calls == 2767
    # The last page is read: its open intent is counted, so no partial read can look like zero.
    assert found.counts.pending_intents == 1


def test_control_a_scan_past_the_page_bound_is_unreadable_never_a_count() -> None:
    scan = PagedScan(pages=MAX_SCAN_PAGES + 1)
    with pytest.raises(ZeroProofUnreadableError, match="page bound"):
        DynamoLegacyFamilies(COORDINATOR, scan).enumerate_families()  # type: ignore[arg-type]
    assert scan.calls == MAX_SCAN_PAGES


# --- Drain-wait visibility and the receive budget ------------------------------------------------


def test_a_refused_delivery_waits_until_d_plus_the_queue_margin() -> None:
    deadline = CAS + timedelta(hours=2)
    assert drain_wait_visibility_seconds(deadline, CAS) == 7200 + 60
    assert drain_wait_visibility_seconds(deadline, deadline + timedelta(minutes=5)) == 60
    assert drain_wait_visibility_seconds(CAS + timedelta(hours=13), CAS) == 43200 - 900


def test_the_third_receive_is_the_last_and_only_drain_counts() -> None:
    assert not is_last_receive(2)
    assert is_last_receive(3)
    assert writer_counts_drain(AuthorityMode.LEGACY_DRAINING)
    for mode in (AuthorityMode.LEGACY, AuthorityMode.SHADOW, AuthorityMode.GRAPH):
        assert not writer_counts_drain(mode)
