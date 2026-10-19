# SPDX-License-Identifier: MIT-0
"""Fixtures for the S3 graph dispatch, claim, settlement and promotion tests.

Every test drives the real DynamoDB adapters (`DynamoGraphDispatchStore`, and the S1 admission
store where a race needs it) over `FakeDynamo`, which evaluates the adapters' own condition and
update expressions transactionally. Every identity is fictional.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from execution_graph_support import FakeDynamo

from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    InstantiationOutcome,
    ingestion_node_plan,
    instantiate_run,
)
from adapters.outbound.dynamodb.execution_graph import due_shard, receipt_item
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from domain.operations.execution_graph import AuthorityMode, graph_id, run_id
from domain.operations.execution_graph_dispatch import ArmScope, NodePhase
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph import ReceiptRecord
from ports.outbound.execution_graph_dispatch import (
    GraphMessage,
    NodeRecord,
    NodeSpec,
    RunRecord,
    SuccessorRequest,
)


def write_text(value: str) -> dict[str, Any]:
    """Render a string attribute for the fake table."""
    return {"S": value}


def write_int(value: int) -> dict[str, Any]:
    """Render a number attribute for the fake table."""
    return {"N": str(value)}


def write_bool(value: bool) -> dict[str, Any]:
    """Render a boolean attribute for the fake table."""
    return {"BOOL": value}


TABLE = "execution-graph-probe-fixture"
TEMPLATE = "ingestion_manifest_to_served.v1"
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
UNIT = canonical_authority_digest(["fictional-window", "amc", "dsp_impressions"])
SHARD = due_shard(UNIT)


def now_text() -> str:
    """Return the fixed recording instant, as the store writes it."""
    return NOW.isoformat()


def store(fake: FakeDynamo) -> DynamoGraphDispatchStore:
    """Return the real dispatch adapter over the fake table."""
    return DynamoGraphDispatchStore(TABLE, fake)  # type: ignore[arg-type]


def set_mode(fake: FakeDynamo, mode: AuthorityMode, *, hold: bool = False) -> None:
    """Seed the mode item (the operator's CAS is S1's; tests seed it directly)."""
    item: dict[str, Any] = {
        "pk": write_text("MODE#ingestion"),
        "sk": write_text("MODE"),
        "mode": write_text(mode.value),
        "revision": write_int(3),
    }
    if hold:
        item["hold"] = write_bool(True)
        item["hold_revision"] = write_int(1)
    fake.items[("MODE#ingestion", "MODE")] = item


@dataclass(frozen=True, slots=True)
class SeededRun:
    """A run the fixture admitted: its record, receipt and identities."""

    record: RunRecord
    request_id: str


def seed_run(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: FakeDynamo,
    *,
    fingerprint: str = "f" * 64,
    retrieved: datetime = NOW - timedelta(hours=1),
    unit: str = UNIT,
    request_id: str = "request-0001",
    state: RunState = RunState.OPEN,
    compute: bool = False,
    grant: bool = False,
    slot: bool = True,
    instantiate: bool = True,
) -> SeededRun:
    """Put what an epoch-1 admitting transaction writes: receipt, run, slot, `INSTANTIATE#`."""
    name = run_id(fingerprint, 1, 0)
    graph = graph_id(fingerprint, 1, 0)
    root = canonical_authority_digest(["root", fingerprint])
    receipt = ReceiptRecord(
        request_id=request_id,
        request_digest="d" * 64,
        decision="ADMITTED",
        reason=None,
        row=20,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=unit,
        input_revision=retrieved.isoformat(),
        fingerprint=fingerprint,
        recorded_at=now_text(),
        protocol_version="v1",
        contract_revision=1,
        run_id=name,
        graph_id=graph,
        root_flow_id=root,
    )
    fake.items[(f"RECEIPT#{request_id}", "RECEIPT")] = cast("Any", receipt_item(receipt))
    run: dict[str, Any] = {
        "pk": write_text(f"RUN#{name}"),
        "sk": write_text("RUN"),
        "item_kind": write_text("run"),
        "state": write_text(state.value),
        "authority_epoch": write_int(1),
        "fingerprint": write_text(fingerprint),
        "conflict_unit": write_text(unit),
        "retrieved_at": write_text(retrieved.isoformat()),
        "root_flow_id": write_text(root),
        "generation": write_int(0),
        "graph_id": write_text(graph),
        "template": write_text(TEMPLATE),
        "dispatch_generation": write_int(0),
        "revision": write_int(1),
        "receipt_request_id": write_text(request_id),
        "protocol_version": write_text("v1"),
    }
    if compute:
        run["compute_started_at"] = write_text(now_text())
    if grant:
        run["grant_id"] = write_text("grant-0001")
    fake.items[(f"RUN#{name}", "RUN")] = run
    if slot:
        fake.items[(f"SLOT#{unit}", "SLOT")] = {
            "pk": write_text(f"SLOT#{unit}"),
            "sk": write_text("SLOT"),
            "active_run_id": write_text(name),
            "revision": write_int(1),
            "authority_epoch": write_int(1),
        }
    if instantiate:
        fake.items[(due_shard(unit), f"INSTANTIATE#{name}")] = {
            "pk": write_text(due_shard(unit)),
            "sk": write_text(f"INSTANTIATE#{name}"),
            "item_kind": write_text("instantiate"),
            "authority_epoch": write_int(1),
            "run_id": write_text(name),
            "due_at": write_text((NOW - timedelta(minutes=5)).isoformat()),
        }
    record = store(fake).read_run(name)
    assert record is not None
    return SeededRun(record, request_id)


def unit_body(run: str, node: str, unit_key: str) -> str:
    """Render a fictional §4 owner body (the six owner fields) for one node.

    Instantiation adds the dispatch binding, so the body carries no `run_id` or `node_id`; the
    plan key varies by run and node so every envelope differs.
    """
    plan = canonical_authority_digest(["fictional-plan", run, node])
    return json.dumps(
        {
            "schema_version": "1",
            "plan_key": "WRITE_PLAN#" + plan + "#" + "b" * 64,
            "plan_digest": plan,
            "origin_intent_id": "c" * 64,
            "generation": 0,
            "unit_key": unit_key,
        },
        sort_keys=True,
    )


def specs(
    run: str,
    *names: str,
    lock_prefix: str = "fictional-table",
    successors: tuple[str, ...] = (),
) -> tuple[NodeSpec, ...]:
    """Root nodes, each holding its own physical-table lock, plus successors after every root."""
    return tuple(
        NodeSpec(
            name=name,
            after=names if name in successors else (),
            phase=NodePhase.PRE_GRANT,
            lock_key=f"{lock_prefix}-{name}",
            group_id=f"{lock_prefix}-{name}",
            body=unit_body(run, name, f"{lock_prefix}-{name}"),
        )
        for name in (*names, *successors)
    )


def planner(
    *names: str, lock_prefix: str = "fictional-table", successors: tuple[str, ...] = ()
) -> Callable[..., Any]:
    """Return a node planner for a generic test graph."""

    def plan(run: RunRecord, receipt: ReceiptRecord) -> tuple[NodeSpec, ...]:
        del receipt
        return specs(run.run_id, *names, lock_prefix=lock_prefix, successors=successors)

    return plan


def instantiate(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: FakeDynamo,
    run: str,
    *names: str,
    lock_prefix: str = "fictional-table",
    successors: tuple[str, ...] = (),
) -> InstantiationOutcome:
    """Run instantiation for a seeded run with a generic planner."""
    plan = planner(*names or ("write_a",), lock_prefix=lock_prefix, successors=successors)
    return instantiate_run(store(fake), InstantiateRequest(run, plan, now_text))


def node(fake: FakeDynamo, run: str, name: str) -> NodeRecord:
    """Read one node strongly."""
    found = [item for item in store(fake).read_nodes(run) if item.name == name]
    assert len(found) == 1
    return found[0]


@dataclass
class SpySender:
    """The executor adapter spy: counts every send (BF-2 asserts zero)."""

    calls: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    nodes: list[str] = field(default_factory=list[str])
    """The node name each message was routed by (slice 7: `write_unit` for a batch)."""
    oversize: bool = False

    def send(self, message: GraphMessage) -> str:
        """Record the exact bytes, group and deduplication identity."""
        if self.oversize:
            from adapters.jobs.execution_graph.dispatcher import (  # noqa: PLC0415
                ProviderOversizeError,
            )

            raise ProviderOversizeError("InvalidParameterValue")
        self.nodes.append(message.node)
        self.calls.append((message.body, message.group_id, message.deduplication_id))
        return f"message-{len(self.calls)}"


ROOT_CLOCK = (NOW - timedelta(hours=2)).isoformat()
"""S6: the root clock the ingestion fixtures stamp on `RUN#` (the manifest's `LastModified`)."""


def unit_specs(run: str, *keys: str) -> tuple[NodeSpec, ...]:
    """S6 §2.2: the `write_unit:<k>` successors the `materialize_raw` settlement puts PENDING."""
    return tuple(
        NodeSpec(
            name=f"write_unit:{key}",
            after=("materialize_raw",),
            phase=NodePhase.PRE_GRANT,
            lock_key=f"TABLE#{key}",
            group_id=key,
            body=unit_body(run, f"write_unit:{key}", key),
        )
        for key in keys
    )


def finish(  # noqa: PLR0913, PLR0917 -- one fixture settler with an explicit state
    fake: FakeDynamo, run: str, name: str, state: str = "SUCCEEDED"
) -> None:
    """Settle one armed node directly: its state, and its token, lock and outbox released."""
    item = fake.items[(f"RUN#{run}", f"NODE#{name}")]
    item["state"] = write_text(state)
    item["revision"] = write_int(int(item["revision"]["N"]) + 1)
    node_id, lock = item["node_id"]["S"], item["lock_key"]["S"]
    unit = fake.items[(f"RUN#{run}", "RUN")]["conflict_unit"]["S"]
    fake.items.pop((f"TOKEN#{node_id}", "TOKEN"), None)
    fake.items.pop((f"LOCK#{lock}", "LOCK"), None)
    fake.items.pop((due_shard(unit), f"OUTBOX#{node_id}"), None)


def materialized_run(fake: FakeDynamo, *keys: str, unit: str = UNIT, **seed: Any) -> str:
    """S6: an ingestion run whose `materialize_raw` settled and put its units with `ARM#`.

    Instantiation runs the real spine planner; the settlement's successor members are committed
    through `put_successors` (slice 3 joins `build_successors` to its own settlement instead).
    """
    set_mode(fake, AuthorityMode.GRAPH)
    seeded = seed_run(fake, unit=unit, **seed)
    name = seeded.record.run_id
    fake.items[(f"RUN#{name}", "RUN")]["root_clock_started_at"] = write_text(ROOT_CLOCK)
    outcome = instantiate_run(store(fake), InstantiateRequest(name, ingestion_node_plan, now_text))
    assert outcome is InstantiationOutcome.ARMED
    finish(fake, name, "materialize_raw")
    run = store(fake).read_run(name)
    assert run is not None
    store(fake).put_successors(
        SuccessorRequest(run, unit_specs(name, *keys), ArmScope.UNITS, True, now_text())
    )
    return name


def kinds(fake: FakeDynamo, prefix: str) -> list[tuple[str, str]]:
    """Every stored key whose pk or sk starts with the prefix."""
    return sorted(
        key for key in fake.items if key[0].startswith(prefix) or key[1].startswith(prefix)
    )
