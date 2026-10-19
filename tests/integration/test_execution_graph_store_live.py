# SPDX-License-Identifier: MIT-0
"""P3 store-level live tests against the dev probe table pair only (S3).

Every write goes to `adlytics-<env>-execution-graph-probe` (and, for the cross-table check, its
`-probe-legacy` twin) through the probe role, which holds exactly the generated action set on
the probe tables and nothing on the live table (V-15). Each test works in its own fresh
namespace (a random fingerprint and conflict unit), so runs never collide. Fictional data only.

Run only through the probe runner's fail-closed JUnit check, one file at a time:

    AWS_PROFILE=<bound> AWS_REGION=us-east-1 ADL_TEST_ENV=dev uv run pytest -m integration
        tests/integration/test_execution_graph_store_live.py   (one line; the runner adds JUnit)

`ADL_P3_CONFLICT_REPORT`, when set, receives the parallel-claim `RUN#` conflict count (R2-7).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
from botocore.exceptions import ClientError

from adapters.inbound.ingestion_handlers.graph_claim import (
    MAX_PARALLEL_CLAIMS,
    ClaimDisposition,
    ClaimRetryExhaustedError,
    GraphClaimInput,
    claim_graph_envelope,
    release_claim_lease,
)
from adapters.jobs.execution_graph.commands import (
    OperatorCommand,
    RunCommandKind,
    run_operator_command,
)
from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.fence_settler import settle_shard
from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    InstantiationOutcome,
    ingestion_node_plan,
    instantiate_run,
)
from adapters.jobs.execution_graph.node_verifier import VerifierContext, verify_shard
from adapters.jobs.execution_graph.promoter import (
    PromoterContext,
    PromotionCounts,
    arm_pass,
    promote_unit,
)
from adapters.outbound.dynamodb.execution_graph import (
    DynamoExecutionGraphStore,
    due_shard,
    receipt_item,
)
from adapters.outbound.dynamodb.execution_graph_dispatch import (
    DynamoGraphDispatchStore,
    batch_row_shard,
)
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.items import write_bool, write_int, write_text
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from domain.operations.execution_graph import AuthorityMode, graph_id, run_id
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_STALE,
    PLAN_DISPATCH_FENCED,
    RUN_ARMING_OPEN,
    ArmScope,
    AttemptFact,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    PromoterBranch,
    ReturnReason,
)
from domain.operations.run_request import RunState, Waiter
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph import (
    AdmissionCommit,
    AdmissionConflictError,
    EffectKind,
    NewRun,
    ReceiptRecord,
    ReplacedRun,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    FenceRunRequest,
    GraphMessage,
    NodeRecord,
    NodeSettlement,
    NodeSpec,
    ReturnUnitRequest,
    RunRecord,
    SuccessorRequest,
)
from ports.outbound.projection_coordinator import AuthorityModeGuard, ModeGuardKind

pytestmark = pytest.mark.integration

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")
PROBE = f"adlytics-{ENV_NAME}-execution-graph-probe"
PROBE_LEGACY = f"adlytics-{ENV_NAME}-execution-graph-probe-legacy"
ROLE = f"adlytics-{ENV_NAME}-execution-graph-probe"
OPERATOR = "arn:aws:iam::000000000000:user/probe-operator"


@pytest.fixture(scope="module")
def client() -> Any:
    """Return a DynamoDB client under the probe role, or skip when the probe pair is absent."""
    session = boto3.Session()
    try:
        account = session.client("sts").get_caller_identity()["Account"]
        assumed = session.client("sts").assume_role(
            RoleArn=f"arn:aws:iam::{account}:role/{ROLE}", RoleSessionName="p3-store-live"
        )["Credentials"]
    except ClientError as error:  # pragma: no cover - only without a deployed probe role
        pytest.skip(f"the probe role is not assumable: {error.response.get('Error', {})}")
    dynamodb = boto3.client(
        "dynamodb",
        region_name=session.region_name or "us-east-1",
        aws_access_key_id=assumed["AccessKeyId"],
        aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"],
    )
    # The probe role holds exactly the generated item actions (no DescribeTable), so presence
    # is proven by a strong read of the probe table's mode key.
    dynamodb.get_item(
        TableName=PROBE,
        Key={"pk": write_text("MODE#ingestion"), "sk": write_text("MODE")},
        ConsistentRead=True,
    )
    return dynamodb


@dataclass
class _Hooked:
    """Wrap the real client so a test can commit a competing transaction first."""

    inner: Any
    before: Callable[[], None] | None = None

    def transact_write_items(self, **kwargs: Any) -> Any:
        hook, self.before = self.before, None
        if hook is not None:
            hook()
        return self.inner.transact_write_items(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _Scoped(DynamoGraphReleaseStore):
    """The real store, with due-shard reads limited to one test's runs (shared probe shards)."""

    def __init__(self, client: Any, runs: set[str]) -> None:
        super().__init__(PROBE, client)
        self.runs = runs

    def read_due(self, shard: str, prefix: str) -> tuple[Any, ...]:
        return tuple(
            item
            for item in super().read_due(shard, prefix)
            if (item.run_id or item.subject) in self.runs
        )


@dataclass(frozen=True)
class _World:
    client: Any
    hooked: _Hooked
    unit: str
    fingerprint: str

    @property
    def store(self) -> DynamoGraphReleaseStore:
        return DynamoGraphReleaseStore(PROBE, cast("Any", self.hooked))

    @property
    def shard(self) -> str:
        return due_shard(self.unit)

    def scoped(self, *runs: str) -> _Scoped:
        """Return a store whose sweeps see only these runs."""
        return _Scoped(cast("Any", self.hooked), set(runs))


def _now() -> datetime:
    return datetime.now(UTC)


def _now_text() -> str:
    return _now().isoformat()


@pytest.fixture
def world(client: Any) -> Iterator[_World]:
    """Yield a fresh namespace on the probe table, with the probe mode item set to GRAPH."""
    token = uuid.uuid4().hex
    client.put_item(
        TableName=PROBE,
        Item={
            "pk": write_text("MODE#ingestion"),
            "sk": write_text("MODE"),
            "mode": write_text(AuthorityMode.GRAPH.value),
            "revision": write_int(1),
        },
    )
    yield _World(
        client,
        _Hooked(client),
        canonical_authority_digest(["p3-unit", token]),
        canonical_authority_digest(["p3-fingerprint", token]),
    )


def _seed(world: _World, *, fingerprint: str | None = None, slot: bool = True) -> RunRecord:
    """Write what an epoch-1 admitting transaction writes, in one transaction."""
    finger = fingerprint or world.fingerprint
    name = run_id(finger, 1, 0)
    request_id = f"p3-{uuid.uuid4().hex[:16]}"
    root = canonical_authority_digest(["p3-root", finger])
    receipt = ReceiptRecord(
        request_id=request_id,
        request_digest="d" * 64,
        decision="ADMITTED",
        reason=None,
        row=20,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=world.unit,
        input_revision=(_now() - timedelta(hours=1)).isoformat(),
        fingerprint=finger,
        recorded_at=_now_text(),
        protocol_version="v1",
        contract_revision=1,
        run_id=name,
        graph_id=graph_id(finger, 1, 0),
        root_flow_id=root,
    )
    items: list[dict[str, Any]] = [
        receipt_item(receipt),
        {
            "pk": write_text(f"RUN#{name}"),
            "sk": write_text("RUN"),
            "item_kind": write_text("run"),
            "state": write_text("OPEN"),
            "authority_epoch": write_int(1),
            "fingerprint": write_text(finger),
            "conflict_unit": write_text(world.unit),
            "retrieved_at": write_text(receipt.input_revision),
            "root_flow_id": write_text(root),
            "generation": write_int(0),
            "graph_id": write_text(graph_id(finger, 1, 0)),
            "template": write_text("ingestion_manifest_to_served.v1"),
            "dispatch_generation": write_int(0),
            "revision": write_int(1),
            "receipt_request_id": write_text(request_id),
            "protocol_version": write_text("v1"),
        },
        {
            "pk": write_text(world.shard),
            "sk": write_text(f"INSTANTIATE#{name}"),
            "item_kind": write_text("instantiate"),
            "authority_epoch": write_int(1),
            "run_id": write_text(name),
            "due_at": write_text(_now_text()),
        },
    ]
    if slot:
        items.append(
            {
                "pk": write_text(f"SLOT#{world.unit}"),
                "sk": write_text("SLOT"),
                "active_run_id": write_text(name),
                "revision": write_int(1),
                "authority_epoch": write_int(1),
            }
        )
    world.client.transact_write_items(
        TransactItems=[{"Put": {"TableName": PROBE, "Item": item}} for item in items]
    )
    record = world.store.read_run(name)
    assert record is not None
    return record


def _body(run: str, name: str) -> str:
    """Render a fictional owner body; instantiation adds the dispatch binding (S3V-2)."""
    return json.dumps({"probe_run": run, "probe_node": name, "probe": True}, sort_keys=True)


def _planner(
    *names: str, lock_prefix: str, successors: tuple[str, ...] = ()
) -> Callable[[RunRecord, ReceiptRecord], tuple[NodeSpec, ...]]:
    """Roots, plus successors that run after every root (S3V-1's multi-node graph)."""

    def plan(run: RunRecord, receipt: ReceiptRecord) -> tuple[NodeSpec, ...]:
        del receipt
        return tuple(
            NodeSpec(
                name,
                names if name in successors else (),
                NodePhase.PRE_GRANT,
                f"{lock_prefix}-{name}",
                f"{lock_prefix}-{name}",
                _body(run.run_id, name),
            )
            for name in (*names, *successors)
        )

    return plan


def _instantiate(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    world: _World,
    run: str,
    *names: str,
    lock: str | None = None,
    successors: tuple[str, ...] = (),
) -> InstantiationOutcome:
    prefix = lock or f"p3-table-{world.unit[:12]}"
    planner = _planner(*names, lock_prefix=prefix, successors=successors)
    return instantiate_run(world.store, InstantiateRequest(run, planner, _now_text))


def _node(world: _World, run: str, name: str) -> NodeRecord:
    (found,) = [item for item in world.store.read_nodes(run) if item.name == name]
    return found


@dataclass
class _Spy:
    calls: int = 0

    def send(self, message: GraphMessage) -> str:
        del message
        self.calls += 1
        return f"probe-message-{self.calls}"


class _Facts:
    def __init__(self, fact: AttemptFact) -> None:
        self.fact = fact

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        del node, claim
        return self.fact, None if self.fact in {AttemptFact.NONE, AttemptFact.OPEN} else "e" * 64


def _claim(world: _World, run: str, name: str, **overrides: Any) -> GraphClaimInput:
    item = _node(world, run, name)
    values: dict[str, Any] = {
        "run_id": run,
        "node_id": item.node_id,
        "dispatch_id": item.dispatch_id,
        "body": item.body,
        "now": _now,
        "lease": timedelta(minutes=16),
    }
    values.update(overrides)
    return GraphClaimInput(**values)


def _command(kind: RunCommandKind, run: str) -> OperatorCommand:
    return OperatorCommand(kind, f"p3-{uuid.uuid4().hex[:20]}", OPERATOR, _now_text, run_id=run)


def _fence(world: _World, run: str, reason: FenceReason) -> None:
    current = world.store.read_run(run)
    assert current is not None
    world.store.fence_run(FenceRunRequest(current, reason, _now_text()))


def _get(world: _World, pk: str, sk: str) -> dict[str, Any] | None:
    found = world.client.get_item(
        TableName=PROBE, Key={"pk": write_text(pk), "sk": write_text(sk)}, ConsistentRead=True
    )
    return found.get("Item")


# --- tests ---------------------------------------------------------------------------------------


def test_p3_ready_token_lock_and_outbox_commit_atomically(world: _World) -> None:
    run = _seed(world)
    assert _instantiate(world, run.run_id, "write_a") is InstantiationOutcome.ARMED
    armed = _node(world, run.run_id, "write_a")
    assert armed.state is NodeState.READY
    assert _get(world, f"TOKEN#{armed.node_id}", "TOKEN") is not None
    assert _get(world, f"LOCK#{armed.lock_key}", "LOCK") is not None
    assert _get(world, world.shard, f"OUTBOX#{armed.node_id}") is not None
    assert _get(world, world.shard, f"INSTANTIATE#{run.run_id}") is None


def test_p3_bf2_a_pause_before_the_permit_refuses_it_with_zero_sends(world: _World) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    assert run_operator_command(world.store, _command(RunCommandKind.PAUSE, run.run_id)).committed
    spy = _Spy()
    counts = dispatch_shard(DispatchContext(world.scoped(run.run_id), spy, _now_text), world.shard)
    assert spy.calls == 0
    assert counts.fenced >= 1
    item = _node(world, run.run_id, "write_a")
    assert (item.state, item.permit_generation) == (NodeState.READY, None)
    outbox = world.store.read_outbox(world.shard, item.node_id)
    current = world.store.read_run(run.run_id)
    assert outbox is not None and current is not None
    with pytest.raises(GraphGuardError) as refused:
        world.store.acquire_permit(current, item, outbox)
    assert refused.value.signal == PLAN_DISPATCH_FENCED


def test_p3_bf2_a_permit_before_the_pause_sends_once_and_resume_sends_once(world: _World) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a", "write_b")
    first = _node(world, run.run_id, "write_a")
    outbox = world.store.read_outbox(world.shard, first.node_id)
    assert outbox is not None
    world.store.acquire_permit(run, first, outbox)
    run_operator_command(world.store, _command(RunCommandKind.PAUSE, run.run_id))
    spy = _Spy()
    dispatch_shard(DispatchContext(world.scoped(run.run_id), spy, _now_text), world.shard)
    assert spy.calls == 1
    run_operator_command(world.store, _command(RunCommandKind.RESUME, run.run_id))
    dispatch_shard(DispatchContext(world.scoped(run.run_id), spy, _now_text), world.shard)
    dispatch_shard(DispatchContext(world.scoped(run.run_id), spy, _now_text), world.shard)
    assert spy.calls == 2
    assert _node(world, run.run_id, "write_b").state is NodeState.DISPATCHED


def test_p3_bf10_a_failed_member_commits_neither_the_receipt_nor_its_effect(world: _World) -> None:
    run = _seed(world)
    receipt = ReceiptRecord(
        request_id=f"p3-{uuid.uuid4().hex[:16]}",
        request_digest="d" * 64,
        decision="QUEUED_LATEST",
        reason=None,
        row=19,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=world.unit,
        input_revision=_now_text(),
        fingerprint="9" * 64,
        recorded_at=_now_text(),
        protocol_version="v1",
        contract_revision=1,
    )
    stale = AdmissionCommit(
        receipt,
        EffectKind.QUEUE_WAITER,
        world.unit,
        0,
        new_waiter=Waiter(receipt.request_id, "9" * 64, _now(), False, 1),
    )
    with pytest.raises(AdmissionConflictError) as refused:
        DynamoExecutionGraphStore(PROBE, world.client).commit_admission(stale)
    assert refused.value.signal == "SLOT_REVISION_CHANGED"
    assert _get(world, f"RECEIPT#{receipt.request_id}", "RECEIPT") is None
    assert _get(world, world.shard, f"WAITER#{world.unit}") is None
    assert run.state is RunState.OPEN


def _replace(world: _World, run: str) -> str | None:
    receipt = ReceiptRecord(
        request_id=f"p3-{uuid.uuid4().hex[:16]}",
        request_digest="e" * 64,
        decision="REPLACED_BEFORE_COMPUTE",
        reason=None,
        row=17,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=world.unit,
        input_revision=_now_text(),
        fingerprint=uuid.uuid4().hex * 2,
        recorded_at=_now_text(),
        protocol_version="v1",
        contract_revision=1,
    )
    commit = AdmissionCommit(
        receipt,
        EffectKind.REPLACE_RUN,
        world.unit,
        1,
        new_run=NewRun(
            f"p3-new-{uuid.uuid4().hex}",
            "p3-graph",
            receipt.fingerprint,
            _now_text(),
            "p3-root",
            0,
            "t",
        ),
        replaced=ReplacedRun(run, invalidating=False),
    )
    try:
        DynamoExecutionGraphStore(PROBE, world.client).commit_admission(commit)
    except AdmissionConflictError as conflict:
        return conflict.signal
    return None


@pytest.mark.parametrize("claim_first", [True, False])
def test_p3_v10_claim_and_replace_cas_one_run_record(world: _World, claim_first: bool) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    if claim_first:
        claim_graph_envelope(
            world.store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
        )
        assert _replace(world, run.run_id) == "ACTIVE_COMPUTE_NOT_REPLACEABLE"
    else:
        assert _replace(world, run.run_id) is None
        disposition = claim_graph_envelope(
            world.store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
        )
        assert disposition.signal == "EXECUTION_SUPERSEDED"
    current = world.store.read_run(run.run_id)
    assert current is not None
    assert (current.compute_started_at is not None) != (
        current.fence_reason is FenceReason.REPLACED
    )


def test_p3_v9_a_claim_on_a_fenced_run_is_superseded_unclaimed(world: _World) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    _fence(world, run.run_id, FenceReason.CANCELLED)
    disposition = claim_graph_envelope(
        world.store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
    )
    assert disposition.acknowledge
    item = _node(world, run.run_id, "write_a")
    assert item.state is NodeState.SUPERSEDED_UNCLAIMED
    assert _get(world, f"LOCK#{item.lock_key}", "LOCK") is None
    assert _get(world, f"EXEC_CLAIM#{item.node_id}", "CLAIM") is None


def test_p3_v9_takeover_with_an_open_attempt_parks_then_the_verifier_settles(world: _World) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    claim_graph_envelope(
        world.store,
        _Facts(AttemptFact.NONE),
        _claim(world, run.run_id, "write_a", lease=timedelta(milliseconds=1)),
    )
    _fence(world, run.run_id, FenceReason.INVALIDATED)
    parked = claim_graph_envelope(
        world.store,
        _Facts(AttemptFact.OPEN),
        _claim(world, run.run_id, "write_a", now=lambda: _now() + timedelta(seconds=5)),
    )
    assert parked.acknowledge
    item = _node(world, run.run_id, "write_a")
    assert item.state is NodeState.AWAITING_PROVIDER_TERMINAL
    assert _get(world, world.shard, f"VERIFY#{item.node_id}") is not None
    verify_shard(
        VerifierContext(world.scoped(run.run_id), _Facts(AttemptFact.SUCCEEDED), _now_text),
        world.shard,
    )
    assert _node(world, run.run_id, "write_a").state is NodeState.SUCCEEDED
    assert _get(world, world.shard, f"VERIFY#{item.node_id}") is None
    assert _get(world, f"LOCK#{item.lock_key}", "LOCK") is None


def test_p3_v11_fence_settlement_releases_the_lock_for_the_replacement(world: _World) -> None:
    run = _seed(world)
    lock = f"p3-shared-{world.unit[:12]}"
    # A root and a successor nothing armed: both settle, so the run turns terminal (S3V-1).
    _instantiate(world, run.run_id, "write_a", lock=lock, successors=("publish",))
    assert _node(world, run.run_id, "publish").state is NodeState.PENDING
    run_operator_command(world.store, _command(RunCommandKind.CANCEL, run.run_id))
    newer = _seed(world, fingerprint=uuid.uuid4().hex * 2, slot=False)
    assert _instantiate(world, newer.run_id, "write_a", lock=lock) is InstantiationOutcome.LOCK_WAIT
    counts = settle_shard(world.scoped(run.run_id), world.shard, _now_text)
    assert counts.fenced_unsent >= 2
    assert counts.failed == 0
    assert _node(world, run.run_id, "write_a").state is NodeState.FENCED_UNSENT
    assert _node(world, run.run_id, "publish").state is NodeState.FENCED_UNSENT
    assert _get(world, world.shard, f"FENCE#{run.run_id}") is None
    assert _instantiate(world, newer.run_id, "write_a", lock=lock) is InstantiationOutcome.ARMED
    final = world.store.read_run(run.run_id)
    assert final is not None and final.state is RunState.CANCELLED


def _coordinator(world: _World) -> DynamoProjectionCoordinator:
    from adapters.outbound.dynamodb.stage1_lifecycle_head import (  # noqa: PLC0415
        DynamoStage1LifecycleHeadStore,
    )

    return DynamoProjectionCoordinator(
        PROBE_LEGACY,
        f"{PROBE_LEGACY}-unused",
        DynamoStage1LifecycleHeadStore(PROBE_LEGACY, world.client),
        world.client,
        graph_table=PROBE,
    )


def _cross_table(world: _World, kind: ModeGuardKind, race: Callable[[], None] | None) -> str | None:
    """Commit a legacy-table write with the coordinator's exact mode-item member."""
    observed = DynamoGraphDispatchStore(PROBE, world.client).read_mode_item()
    member = _coordinator(world)._mode_guard_member(AuthorityModeGuard(kind, observed))  # pyright: ignore[reportPrivateUsage]
    put = {
        "Put": {
            "TableName": PROBE_LEGACY,
            "Item": {
                "pk": write_text(f"P3_LEGACY#{world.unit}"),
                "sk": write_text(uuid.uuid4().hex),
                "probe": write_bool(True),
            },
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }
    if race is not None:
        race()
    try:
        world.client.transact_write_items(TransactItems=[put, member.transact_item])
    except ClientError as error:
        reasons = error.response.get("CancellationReasons", [])
        if reasons and reasons[-1].get("Code") == "ConditionalCheckFailed":
            return (
                "AUTHORITY_MODE_DRAINING"
                if kind is ModeGuardKind.NEW_ROOT_ADMITTING
                else "AUTHORITY_MODE_CHANGED"
            )
        raise
    return None


def _set_probe_mode(world: _World, mode: AuthorityMode) -> None:
    world.client.put_item(
        TableName=PROBE,
        Item={
            "pk": write_text("MODE#ingestion"),
            "sk": write_text("MODE"),
            "mode": write_text(mode.value),
            "revision": write_int(2),
        },
    )


def test_p3_v13a_the_graph_cas_between_a_draining_read_and_finalize_is_mode_changed(
    world: _World,
) -> None:
    _set_probe_mode(world, AuthorityMode.LEGACY_DRAINING)
    assert _cross_table(world, ModeGuardKind.FINALIZE_NOT_GRAPH, None) is None
    signal = _cross_table(
        world, ModeGuardKind.FINALIZE_NOT_GRAPH, lambda: _set_probe_mode(world, AuthorityMode.GRAPH)
    )
    assert signal == "AUTHORITY_MODE_CHANGED"


def test_p3_v13b_the_drain_cas_between_ingress_read_and_submit_is_mode_draining(
    world: _World,
) -> None:
    _set_probe_mode(world, AuthorityMode.SHADOW)
    assert _cross_table(world, ModeGuardKind.NEW_ROOT_ADMITTING, None) is None
    signal = _cross_table(
        world,
        ModeGuardKind.NEW_ROOT_ADMITTING,
        lambda: _set_probe_mode(world, AuthorityMode.LEGACY_DRAINING),
    )
    assert signal == "AUTHORITY_MODE_DRAINING"


def _waiter(world: _World, *, held: bool) -> None:
    request_id = f"p3-w-{uuid.uuid4().hex[:12]}"
    receipt = ReceiptRecord(
        request_id=request_id,
        request_digest="d" * 64,
        decision="QUEUED_LATEST",
        reason=None,
        row=19,
        authority_epoch=1,
        mode=AuthorityMode.GRAPH,
        conflict_unit=world.unit,
        input_revision=_now_text(),
        fingerprint=uuid.uuid4().hex * 2,
        recorded_at=_now_text(),
        protocol_version="v1",
        contract_revision=1,
        root_flow_id="p3-root-waiter",
    )
    world.client.transact_write_items(
        TransactItems=[
            {"Put": {"TableName": PROBE, "Item": receipt_item(receipt)}},
            {
                "Put": {
                    "TableName": PROBE,
                    "Item": {
                        "pk": write_text(world.shard),
                        "sk": write_text(f"WAITER#{world.unit}"),
                        "item_kind": write_text("waiter"),
                        "authority_epoch": write_int(1),
                        "request_id": write_text(request_id),
                        "fingerprint": write_text(receipt.fingerprint),
                        "retrieved_at": write_text(receipt.input_revision),
                        "held": write_bool(held),
                        "revision": write_int(1),
                        "due_at": write_text(_now_text()),
                    },
                }
            },
        ]
    )


def test_p3_v20_a_release_between_the_promoter_read_and_delete_is_refused_then_promoted(
    world: _World,
) -> None:
    run = _seed(world)
    _waiter(world, held=False)
    world.client.update_item(
        TableName=PROBE,
        Key={"pk": write_text(world.shard), "sk": write_text(f"PROMOTE#{world.unit}")},
        UpdateExpression="ADD wake_seq :one",
        ExpressionAttributeValues={":one": write_int(1)},
    )
    world.hooked.before = lambda: _fence(world, run.run_id, FenceReason.CANCELLED)
    counts = PromotionCounts()
    context = PromoterContext(
        world.store, _planner("write_a", lock_prefix=f"p3-w-{world.unit[:8]}"), _now
    )
    branch = promote_unit(context, world.unit, counts)
    assert counts.guards >= 1
    assert branch is PromoterBranch.PROMOTE_WAITER
    assert _get(world, world.shard, f"PROMOTE#{world.unit}") is None
    assert _get(world, world.shard, f"WAITER#{world.unit}") is None


def test_p3_v20_a_held_waiter_waits_for_the_hold_then_is_promoted_once(world: _World) -> None:
    store = world.store
    assert run_operator_command(
        store,
        OperatorCommand(
            RunCommandKind.SET_HOLD, f"p3-{uuid.uuid4().hex[:20]}", OPERATOR, _now_text
        ),
    ).committed
    try:
        _waiter(world, held=True)
        counts = PromotionCounts()
        context = PromoterContext(
            store, _planner("write_a", lock_prefix=f"p3-h-{world.unit[:8]}"), _now
        )
        assert promote_unit(context, world.unit, counts) is PromoterBranch.SKIP_HELD
    finally:
        run_operator_command(
            store,
            OperatorCommand(
                RunCommandKind.CLEAR_HOLD, f"p3-{uuid.uuid4().hex[:20]}", OPERATOR, _now_text
            ),
        )
    assert promote_unit(context, world.unit, counts) is PromoterBranch.PROMOTE_WAITER
    assert promote_unit(context, world.unit, counts) is PromoterBranch.NOTHING


def test_p3_run_conflicts_under_the_maximum_parallel_claims_never_exhaust(world: _World) -> None:
    run = _seed(world)
    names = tuple(f"write_{index:02d}" for index in range(MAX_PARALLEL_CLAIMS))
    assert _instantiate(world, run.run_id, *names) is InstantiationOutcome.ARMED
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    inputs = [_claim(world, run.run_id, name) for name in names]

    def claim(item: GraphClaimInput) -> ClaimDisposition | ClaimRetryExhaustedError:
        try:
            return claim_graph_envelope(
                DynamoGraphDispatchStore(PROBE, world.client), _Facts(AttemptFact.NONE), item
            )
        except ClaimRetryExhaustedError as exhausted:
            return exhausted

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CLAIMS) as pool:
        outcomes = list(pool.map(claim, inputs))
    results = [item for item in outcomes if isinstance(item, ClaimDisposition)]
    exhausted = [item for item in outcomes if isinstance(item, ClaimRetryExhaustedError)]
    conflicts = sum(result.conflicts for result in results) + sum(
        item.conflicts for item in exhausted
    )
    report = os.environ.get("ADL_P3_CONFLICT_REPORT")
    if report:
        Path(report).write_text(
            json.dumps(
                {
                    "parallel_claims": len(outcomes),
                    "committed": sum(1 for result in results if result.claim is not None),
                    "run_conflicts": conflicts,
                    "exhausted": len(exhausted),
                    "max_attempts": max((result.attempts for result in results), default=0),
                    "bound": MAX_PARALLEL_CLAIMS,
                }
            )
        )
    assert not exhausted, "a claim exhausted its RUN# conflict retry: the S3 stop rule"
    assert all(result.claim is not None for result in results)
    current = world.store.read_run(run.run_id)
    assert current is not None and current.compute_started_at is not None


# --- S6 §2.2, §2.3: arming, ARM# retirement and the lease release (store level) ----------------


def _materialized(world: _World, *keys: str) -> RunRecord:
    """Instantiate the real spine, settle `materialize_raw`, and put its units with `ARM#`."""
    run = _seed(world)
    plan = InstantiateRequest(run.run_id, ingestion_node_plan, _now_text)
    assert instantiate_run(world.store, plan) is InstantiationOutcome.ARMED
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    current = world.store.read_run(run.run_id)
    assert current is not None
    root = _node(world, run.run_id, "materialize_raw")
    world.store.settle_node(NodeSettlement(current, root, NodeState.SUCCEEDED, _now_text()))
    units = tuple(
        NodeSpec(
            f"write_unit:{key}",
            ("materialize_raw",),
            NodePhase.PRE_GRANT,
            f"TABLE#p6-{world.unit[:12]}-{key}",
            key,
            _body(run.run_id, key),
        )
        for key in keys
    )
    current = world.store.read_run(run.run_id)
    assert current is not None
    world.store.put_successors(SuccessorRequest(current, units, ArmScope.UNITS, True, _now_text()))
    return current


def test_p6_units_arm_one_per_transaction_and_arm_retires_with_the_last(world: _World) -> None:
    run = _materialized(world, "orders", "returns")
    assert _get(world, world.shard, f"ARM#{run.run_id}") is not None
    assert _get(world, world.shard, f"RELEASE#{run.run_id}") is not None
    context = PromoterContext(world.scoped(run.run_id), ingestion_node_plan, _now)

    counts = arm_pass(context, (world.shard,), lambda: 60_000, "test-request")

    assert (counts.armed, counts.retired, counts.lock_wait) == (2, 1, 0)
    # Conversion 7 (slice 7, D30/D31): one `OUTBOX#<batch_id>` per table group, two here.
    batches: set[str] = set()
    for key in ("orders", "returns"):
        unit = _node(world, run.run_id, f"write_unit:{key}")
        assert unit.state is NodeState.READY
        assert unit.batch_id is not None
        batches.add(unit.batch_id)
        assert _get(world, batch_row_shard(unit.batch_id), f"OUTBOX#{unit.batch_id}") is not None
        assert _get(world, world.shard, f"OUTBOX#{unit.node_id}") is None
    assert len(batches) == 2
    assert _get(world, world.shard, f"ARM#{run.run_id}") is None


def test_p6_slice7_a_returned_member_releases_its_share_on_the_probe_table(world: _World) -> None:
    """Test 15 (pending live): `arm_unit_batch` then `return_unit` on the real table."""
    run = _materialized(world, "orders")
    context = PromoterContext(world.scoped(run.run_id), ingestion_node_plan, _now)
    assert arm_pass(context, (world.shard,), lambda: 60_000, "test-request").batches_armed == 1
    unit = _node(world, run.run_id, "write_unit:orders")
    current = world.store.read_run(run.run_id)
    assert current is not None and unit.batch_id is not None
    world.store.return_unit(
        ReturnUnitRequest(current, unit, ReturnReason.RUN_PAUSED, _now().isoformat())
    )
    returned = _node(world, run.run_id, "write_unit:orders")
    assert (returned.state, returned.return_reason) == (NodeState.PENDING, "run_paused")
    lock = _get(world, f"LOCK#{unit.lock_key}", "LOCK")
    assert lock is not None and lock["open_members"] == {"N": "0"}


def test_p6_fence_pending_refuses_while_arm_exists_then_settles(world: _World) -> None:
    run = _materialized(world, "orders")
    _fence(world, run.run_id, FenceReason.CANCELLED)
    fencing = world.store.read_run(run.run_id)
    assert fencing is not None
    pending = _node(world, run.run_id, "write_unit:orders")
    with pytest.raises(GraphGuardError) as refused:
        world.store.fence_pending(fencing, pending)
    assert refused.value.signal == RUN_ARMING_OPEN
    # The arm pass deletes `ARM#` alone for a run that is not live; then the node settles.
    context = PromoterContext(world.scoped(run.run_id), ingestion_node_plan, _now)
    assert arm_pass(context, (world.shard,), lambda: 60_000, "test-request").retired == 1
    world.store.fence_pending(fencing, pending)
    assert _node(world, run.run_id, "write_unit:orders").state is NodeState.FENCED_UNSENT


def test_p6_a_released_lease_reclaims_and_a_stale_release_is_refused(world: _World) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    store = DynamoGraphDispatchStore(PROBE, world.client)
    first = claim_graph_envelope(
        store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
    )
    assert first.claim is not None and first.claim.admission == 1
    assert release_claim_lease(store, first.claim, _now()) is True
    again = claim_graph_envelope(
        store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
    )
    assert again.outcome == "graph_claimed"
    assert again.claim is not None and (again.claim.lease_version, again.claim.admission) == (2, 2)
    with pytest.raises(GraphGuardError) as stale:
        store.release_claim_lease(first.claim, _now_text())
    assert stale.value.signal == CLAIM_LEASE_STALE


# --- Plan 4.3 (DB21): the bake fan-out's selection flag on the claim (store level) ------------


def test_the_bake_selection_flag_survives_a_reclaim_and_a_stale_mark_is_refused(
    world: _World,
) -> None:
    run = _seed(world)
    _instantiate(world, run.run_id, "write_a")
    dispatch_shard(DispatchContext(world.scoped(run.run_id), _Spy(), _now_text), world.shard)
    store = DynamoGraphDispatchStore(PROBE, world.client)
    claimed = claim_graph_envelope(
        store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
    )
    assert claimed.claim is not None
    claim = claimed.claim
    item = _get(world, f"EXEC_CLAIM#{claim.node_id}", "CLAIM")
    assert item is not None and "bake_selection_started" not in item  # no such attribute yet

    store.mark_bake_selection(claim)

    recorded = store.read_claim(claim.node_id)
    assert recorded is not None and recorded.bake_selection_started is True
    # Control: after a release and a RECLAIM the flag survives and the held version is stale.
    assert release_claim_lease(store, claim, _now()) is True
    again = claim_graph_envelope(
        store, _Facts(AttemptFact.NONE), _claim(world, run.run_id, "write_a")
    )
    assert again.claim is not None and again.claim.bake_selection_started is True
    with pytest.raises(GraphGuardError) as stale:
        store.mark_bake_selection(claim)
    assert stale.value.signal == CLAIM_LEASE_STALE
