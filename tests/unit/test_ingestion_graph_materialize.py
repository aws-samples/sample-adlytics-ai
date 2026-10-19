# SPDX-License-Identifier: MIT-0
"""S6 §2.4: `materialize_raw` in the delivery handler (D2, D16; AGG3-5, G2-5, G2-8).

The executor runs over the real `DynamoGraphDispatchStore` and `FakeDynamo`, which evaluates the
adapter's own transaction conditions, from a run admitted in GRAPH through the real admission
service and armed by the real instantiation. The materializer is the seam: each test's fake
re-attests the claim as the governed port does, then returns or raises what the row needs. The
load-level tests at the end drive `load.py`'s parser, router and GRAPH materializer. Every
identity is fictional.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import pathlib
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from execution_graph_dispatch_support import TABLE, store
from execution_graph_support import FakeDynamo, FakeManifests
from phase_one_support import (
    CATALOG,
    DELIVERY_ENVIRONMENT,
    LANDED,
    SETTINGS,
    TARGET,
    prepared,
)

from adapters.inbound.ingestion_handlers import graph_materialize
from adapters.inbound.ingestion_handlers.delivery_plan import (
    FEASIBILITY_REFUSED,
    POST_AUTHORITY_INFEASIBLE,
    DeliveryRoster,
    FeasibilityCheck,
    PreparedRosterRequest,
    ProjectionComposition,
    prepared_roster,
    require_feasible,
)
from adapters.inbound.ingestion_handlers.graph_claim import GraphClaimInput, claim_graph_envelope
from adapters.inbound.ingestion_handlers.graph_materialize import (
    DELIVERY_CLAIM_LEASE,
    DELIVERY_QUEUE_VISIBILITY_SECONDS,
    GRAPH_MAX_UNITS,
    GRAPH_RUN_FAILED,
    MAX_RECEIVES,
    SIDECAR_BUSY_RETRY_SECONDS,
    TRANSIENT_REFUSAL_CODES,
    GraphEnvelopeDelivery,
    GraphMaterialization,
    GraphRunFailedError,
    MaterializeRequest,
    MaterializeRuntime,
    ZeroRowTarget,
    classify_failure,
    effect_record,
    is_transient_refusal,
    materialize_graph_envelope,
    transient_refusal_visibility_seconds,
)
from adapters.inbound.run_requests.service import (
    AdmissionDependencies,
    RunRequest,
    admit_run_request,
)
from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    InstantiationOutcome,
    ingestion_node_plan,
    instantiate_run,
)
from adapters.jobs.execution_graph.node_verifier import MaterializationFacts
from adapters.jobs.ingest_delivery.landing import LoadedDelivery, ManifestInput
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    PARTITION_LEASE_DURATION,
    SIDECAR_BUSY,
    SIDECAR_INVARIANT,
    SemanticSidecarOutcome,
    SemanticSidecarResult,
    SemanticSidecarTooling,
)
from adapters.outbound.dynamodb.execution_graph import DynamoExecutionGraphStore, due_shard
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.semantic_state import (
    LEASE_CALL_BOUND_SECONDS,
    LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS,
    LEASE_CLIENT_MAX_ATTEMPTS,
    LEASE_CLIENT_READ_TIMEOUT_SECONDS,
    bounded_lease_client,
)
from adapters.outbound.governed_materialization import (
    MANIFEST_INVALID,
    FullHistoryRefusedError,
    MaterializationClaim,
    MaterializationClaimLostError,
)
from domain.curation.landing import Manifest
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_STALE,
    PROVIDER_CEILING_BYTES,
    RUN_STATE_CHANGED,
    AttemptFact,
    FenceReason,
    GraphGuardError,
    NodeState,
)
from domain.operations.generated_execution_graphs import CUTOVER
from domain.operations.ingestion_root import window_identity
from domain.operations.run_request import RunState
from domain.operations.write_plan import CanonicalObjectEntry, CanonicalUnitEntry
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.execution_graph_dispatch import ClaimRecord, NodeRecord, RunRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from adapters.jobs.ingest_delivery.semantic_ingestion import SemanticIngestionRequest

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
WINDOW = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/dsp_impressions/query_pack=amc-dsp-impressions/source_scope=s/marketplace=ATVPDKIKX0DER/"
    "window=2026-10-04_2026-10-06/options=44136fa355b3678a/"
)
MANIFEST = f"{WINDOW}retrieved=20261006T092520Z/manifest.json"
VERSION = "manifest-version-0001"
QUEUE = "https://sqs.us-east-1.amazonaws.com/000000000000/adlytics-test-ingestion"
FENCE = ["Update:RUN/RUN", "Put:DUE/FENCE"]
"""The `build_fence_run` members `_fence_members` reports: the `RUN#` update and `FENCE#`."""


class _NoLegacy:
    def completed_plan_key(self, key: str, version_id: str) -> str | None:
        del key, version_id
        return None

    def head_after(self, window: str, r: datetime) -> datetime | None:
        del window, r
        return None


def _now() -> datetime:
    return NOW


def _entry(key: str) -> CanonicalUnitEntry:
    return CanonicalUnitEntry(
        unit_key=key,
        table_kind="current",
        merge_count=1,
        objects=(
            CanonicalObjectEntry(
                kind="canonical",
                reference="object-fictional-0001",
                content_checksum="a" * 64,
                publication_id="publication-fictional-0001",
                partition_key="partition-fictional",
                fencing_token=3,
            ),
        ),
    )


def _mode_item(mode: AuthorityMode) -> dict[str, Any]:
    return {
        "pk": {"S": "MODE#ingestion"},
        "sk": {"S": "MODE"},
        "mode": {"S": mode.value},
        "revision": {"N": "4"},
    }


def _admit(fake: FakeDynamo, request_id: str) -> Any:
    return admit_run_request(
        RunRequest(
            request_id=request_id,
            protocol_version="v1",
            caller_kind="on_demand",
            reason="operator_rerun",
            manifest_key=MANIFEST,
            version_id=VERSION,
            requester="arn:aws:iam::000000000000:user/operator",
            requester_is_iam=True,
            via_api_group=False,
        ),
        AdmissionDependencies(
            store=DynamoExecutionGraphStore(TABLE, cast("Any", fake)),
            manifests=FakeManifests(retrieved_at=window_identity(MANIFEST).retrieved_at),
            now=lambda: NOW - timedelta(minutes=10),
            legacy=_NoLegacy(),
        ),
    )


@dataclass
class World:
    """One run admitted in GRAPH, instantiated and permitted: `materialize_raw` is on the queue."""

    fake: FakeDynamo
    run_id: str
    node: NodeRecord
    reports: list[Any] = field(default_factory=list[Any])
    """Every error the executor reported (each an `AdlyticsError` in these rows)."""
    logs: list[Any] = field(default_factory=list[Any])
    visibility: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    delivered: list[int] = field(default_factory=list[int])
    terminals: list[float] = field(default_factory=list[float])
    materialize_calls: int = 0

    @property
    def store(self) -> DynamoGraphDispatchStore:
        return store(self.fake)

    def run(self) -> RunRecord:
        found = self.store.read_run(self.run_id)
        assert found is not None
        return found

    def nodes(self) -> dict[str, NodeRecord]:
        return {node.name: node for node in self.store.read_nodes(self.run_id)}

    def materialize_node(self) -> NodeRecord:
        return self.nodes()["materialize_raw"]

    def claim(self) -> ClaimRecord:
        found = self.store.read_claim(self.node.node_id)
        assert found is not None
        return found

    def set_run(self, **attributes: str | int) -> None:
        item = self.fake.items[(f"RUN#{self.run_id}", "RUN")]
        for name, value in attributes.items():
            item[name] = {"N": str(value)} if isinstance(value, int) else {"S": value}

    def cancel(self) -> None:
        """Commit a cancel elsewhere: the run is FENCING (its fence settler finishes it)."""
        self.set_run(state=RunState.FENCING.value, fence_reason=FenceReason.CANCELLED.value)

    def delivery(self, receive_count: int = 1) -> GraphEnvelopeDelivery:
        body = json.loads(self.node.body)
        return GraphEnvelopeDelivery(
            body=self.node.body,
            run_id=self.run_id,
            node_id=self.node.node_id,
            dispatch_id=self.node.dispatch_id,
            node=str(body["node"]),
            conflict_unit=str(body["conflict_unit"]),
            message_id="message-fictional-0001",
            receipt_handle="handle-fictional-0001",
            receive_count=receive_count,
            queue_url=QUEUE,
        )

    def runtime(
        self,
        materialize: Callable[[MaterializeRequest], GraphMaterialization] | None = None,
        *,
        mode: AuthorityMode | None = None,
    ) -> MaterializeRuntime:
        dispatch = self.store
        chosen = materialize or self.reattesting()

        def read_mode() -> AuthorityMode:
            return dispatch.read_mode().mode if mode is None else mode

        def counted(request: MaterializeRequest) -> GraphMaterialization:
            self.materialize_calls += 1
            return chosen(request)

        def hold(queue: str, handle: str, seconds: int) -> None:
            self.visibility.append((queue, handle, seconds))

        return MaterializeRuntime(
            store=dispatch,
            mode=read_mode,
            materialize=counted,
            visibility=hold,
            lease=DELIVERY_CLAIM_LEASE,
            logger=MagicMock(),
            now=_now,
            count_delivery=self.count,
        )

    def count(self) -> None:
        """Count one `IngestDeliveries`."""
        self.delivered.append(1)

    def materialization(
        self,
        units: tuple[str, ...] = ("m_current.fct_alpha", "m_current.fct_beta"),
        targets: tuple[ZeroRowTarget, ...] = (),
    ) -> GraphMaterialization:
        return GraphMaterialization(
            units=tuple(_entry(key) for key in units),
            zero_row_targets=targets,
            report_terminal=self.terminals.append,
        )

    def reattesting(  # noqa: PLR0913 -- one fixture builder with explicit knobs
        self,
        *,
        result: GraphMaterialization | None = None,
        raises: BaseException | None = None,
        before: Callable[[], None] | None = None,
        after: Callable[[], None] | None = None,
    ) -> Callable[[MaterializeRequest], GraphMaterialization]:
        """Return a materializer that re-attests the claim first, as the governed port does.

        `before` runs ahead of the re-attestation (a race it loses); `after` runs once it has
        passed (a race during materialization).
        """

        def materialize(request: MaterializeRequest) -> GraphMaterialization:
            if before is not None:
                before()
            if not request.claim.reattest(request.claim.node_id, request.claim.lease_version):
                raise MaterializationClaimLostError
            if after is not None:
                after()
            if raises is not None:
                raise raises
            return result if result is not None else self.materialization()

        return materialize

    def hold_claim(self, at: datetime, lease: timedelta = DELIVERY_CLAIM_LEASE) -> ClaimRecord:
        """Take the claim directly, as an earlier receive did."""
        held = claim_graph_envelope(
            self.store,
            MaterializationFacts(),
            GraphClaimInput(
                self.run_id,
                self.node.node_id,
                self.node.dispatch_id,
                self.node.body,
                now=lambda: at,
                lease=lease,
            ),
        )
        assert held.claim is not None
        return held.claim


def _world(monkeypatch: pytest.MonkeyPatch) -> World:
    fake = FakeDynamo()
    fake.items[("MODE#ingestion", "MODE")] = _mode_item(AuthorityMode.GRAPH)
    receipt = _admit(fake, "request-fictional-0001")
    assert receipt.decision == "ADMITTED"
    assert receipt.run_id is not None
    dispatch = store(fake)
    outcome = instantiate_run(
        dispatch,
        InstantiateRequest(receipt.run_id, ingestion_node_plan, NOW.isoformat),
    )
    assert outcome is InstantiationOutcome.ARMED
    run = dispatch.read_run(receipt.run_id)
    assert run is not None
    (node,) = (n for n in dispatch.read_nodes(receipt.run_id) if n.name == "materialize_raw")
    outbox = dispatch.read_outbox(due_shard(run.conflict_unit), node.node_id)
    assert outbox is not None
    dispatch.acquire_permit(run, node, outbox)
    (node,) = (n for n in dispatch.read_nodes(receipt.run_id) if n.name == "materialize_raw")
    world = World(fake, receipt.run_id, node)

    def capture_error(error: BaseException, _report: object) -> None:
        world.reports.append(error)

    def capture_log(event: object, _logger: object) -> None:
        world.logs.append(event)

    monkeypatch.setattr(graph_materialize, "report_error", capture_error)
    monkeypatch.setattr(graph_materialize, "report_operation", capture_log)
    return world


def _fence_members(world: World) -> list[str]:
    """List the `RUN#` updates and `FENCE#` puts in the last committed transaction."""
    found: list[str] = []
    for member in world.fake.transactions[-1]:
        ((kind, body),) = member.items()
        key = body["Item"] if kind == "Put" else body["Key"]
        pk, sk = str(key["pk"]["S"]), str(key["sk"]["S"])
        if (kind == "Update" and sk == "RUN") or sk.startswith("FENCE#"):
            found.append(f"{kind}:{pk.split('#', 1)[0]}/{sk.split('#', 1)[0]}")
    return found


def _feasibility(reason: str) -> ValidationError:
    return ValidationError(
        FEASIBILITY_REFUSED, "refused before mutation.", context={"reason": reason}
    )


def _dependency() -> DependencyError:
    return DependencyError(ErrorCode("EXEC_GRAPH", 43), "the manifest version read failed.")


# --- Inputs: the receipt names the admitted manifest version --------------------------------------


def test_an_epoch_1_receipt_names_its_manifest_version_and_a_shadow_receipt_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    receipt = world.store.read_receipt(world.run().receipt_request_id)
    assert receipt is not None
    assert (receipt.manifest_key, receipt.version_id) == (MANIFEST, VERSION)
    # Control: a SHADOW (epoch-0) receipt is written exactly as before.
    fake = FakeDynamo()
    fake.items[("MODE#ingestion", "MODE")] = _mode_item(AuthorityMode.SHADOW)
    shadow = _admit(fake, "request-fictional-0002")
    assert (shadow.authority_epoch, shadow.manifest_key, shadow.version_id) == (0, None, None)
    stored = fake.items[("RECEIPT#request-fictional-0002", "RECEIPT")]
    assert "manifest_key" not in stored
    assert "version_id" not in stored


# --- SUCCEEDED: the settlement transaction and its successors -------------------------------------


def test_a_materialized_node_settles_succeeded_with_units_arm_and_release_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    target = ZeroRowTarget("partition-fictional-rowless", "publication-fictional-0009", 4)
    result = world.materialization(targets=(target,))
    assert materialize_graph_envelope(
        world.runtime(world.reattesting(result=result)), world.delivery()
    )
    nodes = world.nodes()
    settled = nodes["materialize_raw"]
    assert settled.state is NodeState.SUCCEEDED
    record, digest = effect_record(result.units, result.zero_row_targets)
    assert (settled.effect_record, settled.effect_digest) == (record, digest)
    parsed = json.loads(record)
    assert parsed["units"] == ["m_current.fct_alpha", "m_current.fct_beta"]
    assert parsed["zero_row_targets"] == [
        {
            "partition": "partition-fictional-rowless",
            "publication_id": "publication-fictional-0009",
            "fencing_token": 4,
        }
    ]
    for key in ("m_current.fct_alpha", "m_current.fct_beta"):
        unit = nodes[f"write_unit:{key}"]
        assert (unit.state, unit.after, unit.lock_key, unit.group_id) == (
            NodeState.PENDING,
            ("materialize_raw",),
            f"TABLE#{key}",
            key,
        )
        body = json.loads(unit.body)
        assert (body["node"], body["unit"][0]) == ("write_unit", key)
    run = world.run()
    shard = due_shard(run.conflict_unit)
    assert world.fake.items[(shard, f"ARM#{world.run_id}")]["scope"] == {"S": "units"}
    # S5-10: RELEASE# carries the run's root clock.
    assert run.root_clock_started_at is not None
    release = world.fake.items[(shard, f"RELEASE#{world.run_id}")]
    assert release["root_clock_started_at"] == {"S": run.root_clock_started_at}
    # Lock and token released; the settlement bumped the lease (it is lease-fenced).
    assert (f"TOKEN#{settled.node_id}", "TOKEN") not in world.fake.items
    assert (f"LOCK#{settled.lock_key}", "LOCK") not in world.fake.items
    claim = world.claim()
    assert claim.lease_version == 2  # the claim is 1, the settlement's bump 2
    assert claim.materialization_began_at == NOW.isoformat()
    # settle_node's 4, the new RUN# check, 2 units, ARM# and RELEASE#.
    assert len(world.fake.transactions[-1]) == 4 + 1 + 2 + 2
    assert world.delivered == [1]
    assert len(world.terminals) == 1
    assert world.reports == []


def test_an_eleven_unit_roster_settles_in_eighteen_items(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch)
    keys = tuple(f"m_current.fct_{index:02d}" for index in range(GRAPH_MAX_UNITS))
    assert materialize_graph_envelope(
        world.runtime(world.reattesting(result=world.materialization(keys))), world.delivery()
    )
    assert GRAPH_MAX_UNITS == 11  # the design's N + 5 <= 16
    assert len(world.fake.transactions[-1]) == 18  # 5 + 11 + 2
    assert sum(name.startswith("write_unit:") for name in world.nodes()) == GRAPH_MAX_UNITS


def test_an_empty_roster_writes_release_and_no_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch)
    assert materialize_graph_envelope(
        world.runtime(world.reattesting(result=world.materialization(()))), world.delivery()
    )
    shard = due_shard(world.run().conflict_unit)
    assert (shard, f"RELEASE#{world.run_id}") in world.fake.items
    assert (shard, f"ARM#{world.run_id}") not in world.fake.items
    assert not any(name.startswith("write_unit:") for name in world.nodes())
    assert world.materialize_node().state is NodeState.SUCCEEDED
    # Control: a one-unit roster writes ARM# (scope units).
    other = _world(monkeypatch)
    one = other.materialization(("m_current.fct_alpha",))
    assert materialize_graph_envelope(
        other.runtime(other.reattesting(result=one)), other.delivery()
    )
    assert (shard, f"ARM#{other.run_id}") in other.fake.items


# --- The mode gate, duplicates and the claim ------------------------------------------------------


def test_a_graph_envelope_outside_graph_has_zero_effect_and_stays_unacknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    before = len(world.fake.transactions)
    assert (
        materialize_graph_envelope(world.runtime(mode=AuthorityMode.SHADOW), world.delivery())
        is False
    )
    assert len(world.fake.transactions) == before
    assert world.materialize_calls == 0
    (refused,) = world.reports
    assert isinstance(refused, graph_materialize.GraphEnvelopeRefusedError)
    assert refused.code == ErrorCode("STAGE1_DELIVERY", 6)
    assert refused.context["signal"] == "GRAPH_DISPATCH_NOT_ACTIVE"
    # Control: the same envelope in GRAPH is claimed and materialized.
    assert materialize_graph_envelope(world.runtime(), world.delivery())
    assert world.materialize_node().state is NodeState.SUCCEEDED


def test_a_graph_envelope_whose_owner_node_is_not_materialize_raw_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    before = len(world.fake.transactions)
    wrong = replace(world.delivery(), node="write_unit")
    assert materialize_graph_envelope(world.runtime(), wrong) is False
    assert len(world.fake.transactions) == before
    (refused,) = world.reports
    assert refused.context["signal"] == "GRAPH_ENVELOPE_NOT_MATERIALIZE"


def test_a_live_duplicate_waits_out_the_remaining_lease_without_materializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    world.hold_claim(NOW - timedelta(seconds=60))
    assert materialize_graph_envelope(world.runtime(), world.delivery(2)) is False
    assert world.materialize_calls == 0
    assert world.visibility == [(QUEUE, "handle-fictional-0001", 900)]
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.delivered == []


def test_the_claim_lease_is_the_handler_timeout_plus_its_visibility_margin() -> None:
    assert timedelta(seconds=960) == DELIVERY_CLAIM_LEASE
    assert MAX_RECEIVES == 3  # routes.delivery.max_receive_count


# --- The §2.4 failure table, one test per row -----------------------------------------------------


def test_row_1_a_dependency_error_with_receives_remaining_releases_and_stays_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    error = _dependency()
    assert (
        materialize_graph_envelope(
            world.runtime(world.reattesting(raises=error)), world.delivery(1)
        )
        is False
    )
    assert world.materialize_node().state is NodeState.CLAIMED
    # Released (§2.3): the redelivery reads RECLAIM rather than LIVE_DUPLICATE.
    assert world.claim().lease_expires_at == NOW.isoformat()
    assert world.reports == [error]
    assert world.run().state is RunState.OPEN
    # Control: the redelivery reclaims (admission 2) and settles SUCCEEDED.
    assert materialize_graph_envelope(world.runtime(), world.delivery(2))
    assert world.materialize_node().state is NodeState.SUCCEEDED
    assert world.claim().admission == 2


def test_row_1_an_unclassified_error_with_receives_remaining_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    runtime = world.runtime(world.reattesting(raises=RuntimeError("an unclassified defect")))
    assert materialize_graph_envelope(runtime, world.delivery(2)) is False
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.claim().lease_expires_at == NOW.isoformat()


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (_dependency(), "dependency_exhausted"),
        (RuntimeError("an unclassified defect"), "unclassified_error"),
    ),
)
def test_row_2_a_final_receive_settles_failed_and_fences_a_live_run(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, reason: str
) -> None:
    world = _world(monkeypatch)
    runtime = world.runtime(world.reattesting(raises=error))
    assert materialize_graph_envelope(runtime, world.delivery(MAX_RECEIVES)) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, reason)
    run = world.run()
    assert (run.state, run.fence_reason) == (RunState.FENCING, FenceReason.NODE_FAILED)
    assert _fence_members(world) == FENCE
    (reported,) = world.reports
    assert isinstance(reported, GraphRunFailedError)
    assert reported.context["signal"] == GRAPH_RUN_FAILED
    assert reported.context["failure_reason"] == reason


def test_row_2_a_final_receive_on_a_fencing_run_omits_the_fence_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGG3-5: on FENCING the fence member is omitted; the fence settler terminalizes."""
    world = _world(monkeypatch)
    runtime = world.runtime(world.reattesting(after=world.cancel, raises=_dependency()))
    assert materialize_graph_envelope(runtime, world.delivery(MAX_RECEIVES)) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, "dependency_exhausted")
    assert _fence_members(world) == []
    assert world.run().fence_reason is FenceReason.CANCELLED


# --- TD-61: a classified transient refusal redelivers after a short, bounded step ----------------


def _busy() -> DependencyError:
    return DependencyError(
        SIDECAR_BUSY,
        "The semantic publication group is temporarily busy.",
        context={"partition": "partition-fictional-0001", "reason": "held"},
    )


def test_td61_a_busy_refusal_redelivers_after_its_short_step_and_still_spends_each_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receives 1 and 2 get 220 s and 440 s; the final receive settles, so nothing loops."""
    world = _world(monkeypatch)
    busy = _busy()
    runtime = world.runtime(world.reattesting(raises=busy))
    for receive in range(1, MAX_RECEIVES):
        assert materialize_graph_envelope(runtime, world.delivery(receive)) is False
        assert world.materialize_node().state is NodeState.CLAIMED
        # Released first (§2.3), so the short redelivery reclaims instead of LIVE_DUPLICATE.
        assert world.claim().lease_expires_at == NOW.isoformat()
    assert world.visibility == [
        (QUEUE, "handle-fictional-0001", SIDECAR_BUSY_RETRY_SECONDS * receive)
        for receive in range(1, MAX_RECEIVES)
    ]
    assert [seconds for _, _, seconds in world.visibility] == [220, 440]
    # The final receive is still final: the node settles FAILED and the message is
    # acknowledged, with no further override, so every retry counted against the budget.
    assert materialize_graph_envelope(runtime, world.delivery(MAX_RECEIVES)) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, "dependency_exhausted")
    assert len(world.visibility) == MAX_RECEIVES - 1
    assert world.materialize_calls == MAX_RECEIVES
    assert world.claim().admission == MAX_RECEIVES


@pytest.mark.parametrize(
    "error",
    (
        _dependency(),
        RuntimeError("an unclassified defect"),
        DependencyError(SIDECAR_INVARIANT, "a sidecar invariant, not a refusal."),
    ),
)
def test_td61_control_every_other_retryable_error_keeps_the_full_visibility(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    world = _world(monkeypatch)
    runtime = world.runtime(world.reattesting(raises=error))
    assert materialize_graph_envelope(runtime, world.delivery(1)) is False
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.visibility == []


def test_td61_control_a_permanent_refusal_settles_at_once_with_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    runtime = world.runtime(world.reattesting(raises=_feasibility("units_exceed_cap")))
    assert materialize_graph_envelope(runtime, world.delivery(1)) is True
    assert world.materialize_node().state is NodeState.FAILED
    assert world.visibility == []


def test_td61_an_unavailable_override_falls_back_to_the_queue_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    base = world.runtime(world.reattesting(raises=_busy()))

    def unavailable(_queue: str, _handle: str, _seconds: int) -> None:
        raise RuntimeError("sqs unavailable")

    runtime = replace(base, visibility=unavailable)
    assert materialize_graph_envelope(runtime, world.delivery(1)) is False
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.claim().lease_expires_at == NOW.isoformat()
    outcomes = [event.outcome for event in world.logs]
    assert "graph_transient_refusal_visibility_unavailable" in outcomes
    assert "graph_transient_refusal_redelivery" not in outcomes


def test_td61_the_busy_schedule_outlasts_one_partition_lease_inside_the_queue_visibility() -> None:
    assert frozenset({SIDECAR_BUSY}) == TRANSIENT_REFUSAL_CODES
    assert is_transient_refusal(_busy())
    assert not is_transient_refusal(_dependency())
    assert not is_transient_refusal(InvariantError(SIDECAR_BUSY, "same code, other class."))
    assert DELIVERY_CLAIM_LEASE.total_seconds() == DELIVERY_QUEUE_VISIBILITY_SECONDS
    waits = [transient_refusal_visibility_seconds(n) for n in range(1, MAX_RECEIVES)]
    margin = int(CUTOVER["delivery_queue_visibility_margin_seconds"])
    assert sum(waits) >= PARTITION_LEASE_DURATION.total_seconds() + margin
    assert all(0 < wait < DELIVERY_QUEUE_VISIBILITY_SECONDS for wait in waits)
    # Bounded at every receive count, including past the route's budget.
    assert transient_refusal_visibility_seconds(0) == SIDECAR_BUSY_RETRY_SECONDS
    assert transient_refusal_visibility_seconds(100) == DELIVERY_QUEUE_VISIBILITY_SECONDS


def test_td61_the_delivery_handler_lease_client_bounds_every_lease_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    config: Any = bounded_lease_client().meta.config
    assert (config.connect_timeout, config.read_timeout) == (
        LEASE_CLIENT_CONNECT_TIMEOUT_SECONDS,
        LEASE_CLIENT_READ_TIMEOUT_SECONDS,
    )
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": LEASE_CLIENT_MAX_ATTEMPTS,
    }
    assert LEASE_CALL_BOUND_SECONDS == 34
    # The delivery handler composes its lease store on that client, not the default one.
    load = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src/adapters/inbound/ingestion_handlers/load.py"
    ).read_text()
    assert "DynamoLeaseStore(SETTINGS.semantic_leases_table, bounded_lease_client())" in load
    assert load.count("DynamoLeaseStore(") == 1


def test_row_3_a_refused_reattestation_after_a_lease_move_acknowledges_with_zero_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    committed: list[int] = []

    def reclaimed_elsewhere() -> None:
        item = world.fake.items[(f"EXEC_CLAIM#{world.node.node_id}", "CLAIM")]
        item["lease_version"] = {"N": str(int(item["lease_version"]["N"]) + 1)}
        committed.append(len(world.fake.transactions))

    runtime = world.runtime(world.reattesting(before=reclaimed_elsewhere))
    assert materialize_graph_envelope(runtime, world.delivery()) is True
    assert len(world.fake.transactions) == committed[0]
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.reports == []
    assert world.logs[-1].outcome == "graph_claim_lost"


@pytest.mark.parametrize(
    ("began", "state", "reason"),
    (
        (False, NodeState.SUPERSEDED_UNCLAIMED, None),
        (True, NodeState.FAILED, "materialization_interrupted"),
    ),
)
def test_row_4_a_refused_reattestation_on_a_fencing_run_settles_by_takeover_facts(  # noqa: PLR0913, PLR0917 -- one parametrized row
    monkeypatch: pytest.MonkeyPatch, began: bool, state: NodeState, reason: str | None
) -> None:
    world = _world(monkeypatch)

    def cancelled_before_the_fetch() -> None:
        if began:
            # An earlier receive's re-attestation stamped the claim.
            item = world.fake.items[(f"EXEC_CLAIM#{world.node.node_id}", "CLAIM")]
            item["materialization_began_at"] = {"S": (NOW - timedelta(hours=1)).isoformat()}
        world.cancel()

    runtime = world.runtime(world.reattesting(before=cancelled_before_the_fetch))
    assert materialize_graph_envelope(runtime, world.delivery()) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (state, reason)
    assert _fence_members(world) == []
    assert world.reports == []
    assert world.logs[-1].outcome == "graph_takeover_settled"


def test_row_4_control_a_refused_reattestation_on_a_live_run_includes_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)

    def refused(_store: object, _claim: ClaimRecord, _began: str) -> None:
        raise GraphGuardError(RUN_STATE_CHANGED)

    monkeypatch.setattr(DynamoGraphDispatchStore, "reattest_materialization", refused)
    assert materialize_graph_envelope(world.runtime(), world.delivery()) is True
    assert world.materialize_node().state is NodeState.FAILED
    assert _fence_members(world) == FENCE
    assert world.run().state is RunState.FENCING


@pytest.mark.parametrize("live", (True, False))
def test_row_5_a_historical_manifest_settles_failed_and_fences_only_while_live(
    monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    world = _world(monkeypatch)

    def refused(request: MaterializeRequest) -> GraphMaterialization:
        del request  # the governed port refuses before the re-attestation and any data read
        if not live:
            world.cancel()
        raise FullHistoryRefusedError(MANIFEST)

    assert materialize_graph_envelope(world.runtime(refused), world.delivery(1)) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, "full_history_not_enabled")
    assert _fence_members(world) == (FENCE if live else [])
    (reported,) = world.reports
    assert reported.context["fenced"] is live


@pytest.mark.parametrize("live", (True, False))
@pytest.mark.parametrize(
    ("error", "reason", "fence"),
    (
        (_feasibility("units_exceed_cap"), "units_exceed_cap", FenceReason.NODE_FAILED),
        (_feasibility("unit_envelope_oversize"), "unit_envelope_oversize", FenceReason.OVERSIZE),
        (InvariantError(MANIFEST_INVALID, "not a manifest."), "invalid_manifest", None),
        (
            InvariantError(POST_AUTHORITY_INFEASIBLE, "outside the proof."),
            "post_authority_infeasible",
            None,
        ),
    ),
)
def test_row_6_a_non_retryable_refusal_settles_failed_with_its_reason_and_fence(  # noqa: PLR0913, PLR0917 -- one parametrized row
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    reason: str,
    fence: FenceReason | None,
    live: bool,
) -> None:
    world = _world(monkeypatch)

    def refused(request: MaterializeRequest) -> GraphMaterialization:
        assert request.claim.reattest(request.claim.node_id, request.claim.lease_version)
        if not live:
            world.cancel()
        # The feasibility callback refuses before any publication commit; the load-level
        # test below observes that no commit follows the refusal.
        raise error

    assert materialize_graph_envelope(world.runtime(refused), world.delivery(1)) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, reason)
    assert not any(name.startswith("write_unit:") for name in world.nodes())
    if live:
        assert _fence_members(world) == FENCE
        assert world.run().fence_reason is (fence or FenceReason.NODE_FAILED)
    else:
        assert _fence_members(world) == []
        assert world.run().fence_reason is FenceReason.CANCELLED
    (reported,) = world.reports
    assert reported.context["signal"] == GRAPH_RUN_FAILED


@pytest.mark.parametrize("live", (True, False))
def test_row_6_a_missing_admitting_receipt_settles_failed_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    world = _world(monkeypatch)
    receipt_id = world.run().receipt_request_id
    del world.fake.items[(f"RECEIPT#{receipt_id}", "RECEIPT")]
    if not live:
        original = DynamoGraphDispatchStore.read_receipt

        def cancelled_then_read(self: DynamoGraphDispatchStore, request_id: str) -> Any:
            world.cancel()
            return original(self, request_id)

        monkeypatch.setattr(DynamoGraphDispatchStore, "read_receipt", cancelled_then_read)
    assert materialize_graph_envelope(world.runtime(), world.delivery()) is True
    assert world.materialize_calls == 0
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, "admitting_receipt_missing")
    assert _fence_members(world) == (FENCE if live else [])


def test_row_6_a_receipt_naming_another_manifest_version_is_admitting_receipt_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    item = world.fake.items[(f"RECEIPT#{world.run().receipt_request_id}", "RECEIPT")]
    item["version_id"] = {"S": "manifest-version-other"}
    assert materialize_graph_envelope(world.runtime(), world.delivery())
    assert world.materialize_calls == 0
    assert world.materialize_node().failure_reason == "admitting_receipt_missing"


def test_row_7_a_run_fencing_at_settlement_fails_interrupted_without_successors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)

    def cancel(_fake: FakeDynamo) -> None:
        world.cancel()

    def cancelled_before_the_settlement() -> None:
        world.fake.before_transaction = cancel

    runtime = world.runtime(world.reattesting(after=cancelled_before_the_settlement))
    assert materialize_graph_envelope(runtime, world.delivery()) is True
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (NodeState.FAILED, "materialization_interrupted")
    assert (due_shard(world.run().conflict_unit), f"RELEASE#{world.run_id}") not in (
        world.fake.items
    )
    assert not any(name.startswith("write_unit:") for name in world.nodes())
    assert world.terminals == []
    assert world.reports == []


def test_row_8_a_pause_between_read_and_commit_redecides_under_the_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)

    def pause(_fake: FakeDynamo) -> None:
        world.set_run(state=RunState.PAUSED.value, dispatch_generation=1)

    def paused_before_the_settlement() -> None:
        world.fake.before_transaction = pause

    runtime = world.runtime(world.reattesting(after=paused_before_the_settlement))
    assert materialize_graph_envelope(runtime, world.delivery()) is True
    assert world.materialize_node().state is NodeState.SUCCEEDED
    assert world.run().state is RunState.PAUSED
    assert len(world.terminals) == 1


def test_row_8_control_exhausting_max_redecide_leaves_the_message_unacknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    moves = [0]

    def move(fake: FakeDynamo) -> None:
        moves[0] += 1
        world.set_run(dispatch_generation=moves[0])
        fake.before_transaction = move

    def keeps_moving() -> None:
        world.fake.before_transaction = move

    runtime = world.runtime(world.reattesting(after=keeps_moving))
    assert materialize_graph_envelope(runtime, world.delivery()) is False
    world.fake.before_transaction = None
    assert world.materialize_node().state is NodeState.CLAIMED
    assert world.claim().lease_expires_at == NOW.isoformat()
    assert moves[0] == 8 + 1  # max_redecide settlements, then the lease release
    assert world.terminals == []


def test_row_9_a_settlement_that_cannot_commit_is_reported_and_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)

    def store_down_at_settlement() -> None:
        world.fake.fail_next = True

    runtime = world.runtime(world.reattesting(after=store_down_at_settlement))
    with pytest.raises(DependencyError):
        materialize_graph_envelope(runtime, world.delivery())
    assert world.materialize_node().state is NodeState.CLAIMED
    assert len(world.reports) == 1


# --- Takeover facts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("began", "fact"), ((None, AttemptFact.NONE), (NOW.isoformat(), AttemptFact.FAILED))
)
def test_materialization_facts_read_materialization_began_at(
    monkeypatch: pytest.MonkeyPatch, began: str | None, fact: AttemptFact
) -> None:
    world = _world(monkeypatch)
    claim = replace(world.hold_claim(NOW), materialization_began_at=began)
    assert MaterializationFacts().attempt(world.node, claim) == (fact, None)


@pytest.mark.parametrize(
    ("began", "state", "reason"),
    (
        (False, NodeState.SUPERSEDED_UNCLAIMED, None),
        (True, NodeState.FAILED, "materialization_interrupted"),
    ),
)
def test_a_takeover_of_an_expired_claim_on_a_fencing_run_settles_from_the_facts(  # noqa: PLR0913, PLR0917 -- one parametrized row
    monkeypatch: pytest.MonkeyPatch, began: bool, state: NodeState, reason: str | None
) -> None:
    world = _world(monkeypatch)
    first = world.hold_claim(NOW - timedelta(hours=1))
    if began:
        world.store.reattest_materialization(first, (NOW - timedelta(hours=1)).isoformat())
    world.cancel()
    assert materialize_graph_envelope(world.runtime(), world.delivery(2)) is True
    assert world.materialize_calls == 0
    node = world.materialize_node()
    assert (node.state, node.failure_reason) == (state, reason)


# --- The two-member re-attestation (AGG3-17) ------------------------------------------------------


def test_the_reattestation_stamps_the_first_instant_and_refuses_a_moved_lease_or_dead_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    held = world.hold_claim(NOW)
    world.store.reattest_materialization(held, "2026-10-09T09:00:00+00:00")
    assert len(world.fake.transactions[-1]) == 2  # EXEC_CLAIM# and RUN#
    stamped = world.claim()
    world.store.reattest_materialization(stamped, "2026-10-09T09:05:00+00:00")
    assert world.claim().materialization_began_at == "2026-10-09T09:00:00+00:00"
    with pytest.raises(GraphGuardError) as stale:
        world.store.reattest_materialization(replace(stamped, lease_version=9), NOW.isoformat())
    assert stale.value.signal == CLAIM_LEASE_STALE
    world.cancel()
    with pytest.raises(GraphGuardError) as fenced:
        world.store.reattest_materialization(stamped, NOW.isoformat())
    assert fenced.value.signal == RUN_STATE_CHANGED


# --- The settlement rule --------------------------------------------------------------------------


def test_the_settlement_rule_classifies_once_by_error_class() -> None:
    dependency = _dependency()
    assert classify_failure(dependency, final=False) == (True, "dependency_exhausted")
    assert classify_failure(dependency, final=True) == (False, "dependency_exhausted")
    assert classify_failure(RuntimeError("x"), final=False) == (True, "unclassified_error")
    assert classify_failure(RuntimeError("x"), final=True) == (False, "unclassified_error")
    assert classify_failure(_feasibility("units_exceed_cap"), final=False) == (
        False,
        "units_exceed_cap",
    )
    assert classify_failure(FullHistoryRefusedError(MANIFEST), final=False) == (
        False,
        "full_history_not_enabled",
    )


# --- Bounds before any mutation (AGG3-10) ---------------------------------------------------------


def _roster(count: int) -> DeliveryRoster:
    base = prepared_roster(
        PreparedRosterRequest(plan=prepared(), target=TARGET, catalog=CATALOG, now=NOW)
    ).units[0]
    return DeliveryRoster(
        tuple(
            replace(base, entry=replace(base.entry, unit_key=f"m_current.fct_{index:02d}"))
            for index in range(count)
        )
    )


_WIDE = replace(SETTINGS, max_accepted_attempts_per_plan=10_000)
"""The plan-wide attempt budget opened up, so only the unit bound under test can refuse."""


def test_twelve_units_are_refused_units_exceed_cap_and_eleven_are_accepted() -> None:
    with pytest.raises(ValidationError) as refused:
        require_feasible(FeasibilityCheck(_roster(12), (), _WIDE, max_units=GRAPH_MAX_UNITS))
    assert refused.value.code == FEASIBILITY_REFUSED
    assert refused.value.context["reason"] == "units_exceed_cap"
    require_feasible(FeasibilityCheck(_roster(11), (), _WIDE, max_units=GRAPH_MAX_UNITS))
    # Legacy keeps the §11 cap of 15: twelve units pass without the GRAPH bound.
    require_feasible(FeasibilityCheck(_roster(12), (), _WIDE))


def test_an_envelope_above_the_ceiling_is_refused_and_one_at_the_ceiling_is_accepted() -> None:
    roster = _roster(1)

    def oversize(_entry: CanonicalUnitEntry) -> str:
        return "x" * (PROVIDER_CEILING_BYTES + 1)

    def at_ceiling(_entry: CanonicalUnitEntry) -> str:
        return "x" * PROVIDER_CEILING_BYTES

    with pytest.raises(ValidationError) as refused:
        require_feasible(FeasibilityCheck(roster, (), SETTINGS, envelope=oversize))
    assert refused.value.code == FEASIBILITY_REFUSED
    assert refused.value.context["reason"] == "unit_envelope_oversize"
    require_feasible(FeasibilityCheck(roster, (), SETTINGS, envelope=at_ceiling))


# --- load.py: parse, route, span, the GRAPH materializer (G2-8) -----------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give `load` enough stand-in environment to import without reaching AWS."""
    for key, value in {
        "ADL_ENVIRONMENT": "test",
        "ADL_AWS_REGION": "us-east-1",
        "ADL_COGNITO_USER_POOL_ID": "unused-by-ingestion",
        "ADL_COGNITO_CLIENT_ID": "unused-by-ingestion",
        "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
        "ADL_APP_BASE_URL": "https://app.test.invalid",
        "ADL_API_BASE_URL": "https://api.test.invalid",
        "ADL_COOKIE_SCOPE": "host_only",
        "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
        "ADL_ATHENA_WORKGROUP": "adlytics-test-interactive",
        "ADL_SEMANTIC_ATTEMPTS_TABLE": "adlytics-test-semantic-attempts",
        "ADL_SEMANTIC_LEASES_TABLE": "adlytics-test-semantic-leases",
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": "adlytics-test-semantic-publications",
        "ADL_RAW_BUCKET": "adlytics-test-raw",
        "ADL_EXECUTION_GRAPH_TABLE": "adlytics-test-execution-graph",
        "ADL_SEMANTIC_V2_BUCKET": "adlytics-test-semantic",
        "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": "adlytics-test-semantic-bake",
        "ADL_SEMANTIC_V2_SOURCE_DATABASE": "adlytics_test_source",
        "ADL_SEMANTIC_V2_CANDIDATE_DATABASE": "adlytics_test_candidate",
        "ADL_SEMANTIC_V2_CURRENT_DATABASE": "adlytics_test_current",
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
        "AWS_DEFAULT_REGION": "us-east-1",
        **DELIVERY_ENVIRONMENT,
    }.items():
        monkeypatch.setenv(key, value)


def _load(monkeypatch: pytest.MonkeyPatch) -> Any:
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415 -- env before import

    return load


def _graph_record(body: dict[str, object]) -> dict[str, Any]:
    return {
        "messageId": "message-fictional-0001",
        "receiptHandle": "handle-fictional-0001",
        "attributes": {"ApproximateReceiveCount": "2", "SentTimestamp": "1791536400000"},
        "eventSourceARN": "arn:aws:sqs:us-east-1:000000000000:adlytics-test-ingestion",
        "body": json.dumps(body),
    }


_BINDING: dict[str, object] = {
    "schema_version": "1",
    "node": "materialize_raw",
    "conflict_unit": "unit-fictional",
    "root_flow_id": "root-fictional",
    "dispatch_id": "dispatch-fictional",
    "run_id": "run-fictional",
    "node_id": "node-fictional",
}


def test_load_parses_a_graph_envelope_beside_an_s3_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    load = _load(monkeypatch)
    marker = {
        "messageId": "message-fictional-0002",
        "attributes": {"ApproximateReceiveCount": "1"},
        "body": json.dumps(
            {
                "detail": {
                    "bucket": {"name": "adlytics-test-raw"},
                    "object": {"key": MANIFEST, "version-id": VERSION},
                }
            }
        ),
    }
    parsed = load.delivery_messages({"Records": [_graph_record(_BINDING), marker]})
    graph, s3 = parsed
    assert isinstance(graph, GraphEnvelopeDelivery)
    assert (graph.run_id, graph.node_id, graph.dispatch_id, graph.receive_count) == (
        "run-fictional",
        "node-fictional",
        "dispatch-fictional",
        2,
    )
    assert graph.queue_url.endswith("/adlytics-test-ingestion")
    assert graph.queue_enqueued_at is not None
    assert isinstance(s3, load.IngestionMessage)
    # The compatibility projection names manifest markers only.
    assert load.manifest_keys({"Records": [_graph_record(_BINDING), marker]}) == (MANIFEST,)
    # Control: a body missing one binding field is read as an S3 event, and refused as one.
    partial = {name: value for name, value in _BINDING.items() if name != "node_id"}
    with pytest.raises(ValueError, match="S3"):
        load.delivery_messages({"Records": [_graph_record(partial)]})


def test_one_graph_materialize_span_per_claimed_node_and_none_for_an_s3_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    traced = MagicMock()
    recording = MagicMock()
    traced.return_value.__enter__.return_value = recording
    monkeypatch.setattr(graph_materialize, "span", traced)
    assert materialize_graph_envelope(world.runtime(), world.delivery())
    traced.assert_called_once_with(
        graph_materialize.INGEST_GRAPH_MATERIALIZE,
        dispatch_id=world.node.dispatch_id,
        run_id=world.run_id,
        node_id=world.node.node_id,
        conflict_unit=world.run().conflict_unit,
        receive_count=1,
        stage="materialize",
        completed=0,
        total=1,
    )
    recording.record.assert_called_once_with(
        completed=1,
        outcome="succeeded",
        node_state="SUCCEEDED",
        failure_reason="none",
        unit_count=2,
    )
    # Control: an S3-event record routes to the legacy ingress and opens no graph span.
    load = _load(monkeypatch)
    traced.reset_mock()
    legacy = MagicMock(return_value=True)
    monkeypatch.setattr(load, "_load", legacy)
    message = load.IngestionMessage("raw-v2", ManifestInput(MANIFEST, VERSION), "execution-1", None)
    assert load._route(message, lambda: 0) is True
    legacy.assert_called_once()
    traced.assert_not_called()


def test_load_refuses_a_graph_envelope_without_a_graph_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = _load(monkeypatch)
    reported: list[Any] = []

    def capture(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(load, "GRAPH", None)
    monkeypatch.setattr(load, "report_error", capture)
    delivery = load.delivery_messages({"Records": [_graph_record(_BINDING)]})[0]
    assert load._route(delivery, lambda: 0) is False
    (refused,) = reported
    assert refused.context["signal"] == "GRAPH_DISPATCH_NOT_ACTIVE"


class _ReattestingPort:
    """The governed port with the historical check passed; it re-attests a GRAPH claim."""

    def __init__(self) -> None:
        self.claims: list[MaterializationClaim | None] = []

    def fetch(self, manifest: ManifestInput, claim: MaterializationClaim | None = None) -> Any:
        del manifest
        self.claims.append(claim)
        if claim is not None and not claim.reattest(claim.node_id, claim.lease_version):
            raise MaterializationClaimLostError
        return LoadedDelivery(
            manifest=cast("Manifest", object()),
            manifest_key=MANIFEST,
            text="landed bytes",
            landed_ref=LANDED,
        )


def _graph_load_runtime(load: Any, port: _ReattestingPort) -> Any:
    return load._V2Runtime(
        bucket="raw-v2",
        tooling=cast("SemanticSidecarTooling", object()),
        target=TARGET,
        catalog=CATALOG,
        publications=MagicMock(),
        coordinator=MagicMock(),
        reads=MagicMock(),
        settings=SETTINGS,
        composition=ProjectionComposition.QUEUED,
        inline=None,
        mode_item=lambda: None,
        ingress=MagicMock(),
        materialization=port,
    )


def _graph_request(world: World) -> MaterializeRequest:
    held = world.hold_claim(NOW)
    return MaterializeRequest(
        manifest=ManifestInput(MANIFEST, VERSION),
        claim=MaterializationClaim(held.node_id, held.lease_version, lambda _n, _v: True),
        run=world.run(),
        node_id=world.node.node_id,
        message_id="message-fictional-0001",
        receive_count=2,
        now=NOW,
    )


def test_g2_8_a_blocked_partition_emits_blocked_and_a_published_one_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = _load(monkeypatch)
    world = _world(monkeypatch)
    port = _ReattestingPort()
    monkeypatch.setattr(load, "RUNTIME", _graph_load_runtime(load, port))
    results = (
        SemanticSidecarResult("published-contract", SemanticSidecarOutcome.PUBLISHED, "ok"),
        SemanticSidecarResult("blocked-contract", SemanticSidecarOutcome.BLOCKED, "blocked"),
    )

    def ingest(_request: object, _tooling: object) -> tuple[SemanticSidecarResult, ...]:
        return results

    monkeypatch.setattr(load, "ingest_semantic_delivery", ingest)

    def never(*_args: object) -> None:
        raise AssertionError("GRAPH neither advances servability nor submits a plan intent")

    monkeypatch.setattr(load, "_advance_zero_row_publications", never)
    monkeypatch.setattr(load, "_submit_plan_intent", never)
    actions: list[str] = []
    terminals: list[Any] = []

    class _Metric:
        def add_dimension(self, *, name: str, value: str) -> None:
            assert name == "Action"
            actions.append(value)

    @contextmanager
    def metric(**_kwargs: object) -> Generator[_Metric]:
        yield _Metric()

    monkeypatch.setattr(load, "single_metric", metric)

    def capture(event: object, _logger: object) -> None:
        terminals.append(event)

    monkeypatch.setattr(load, "report_operation", capture)
    request = _graph_request(world)
    materialized = load._materialize_graph(request)
    assert materialized.units == ()
    assert port.claims == [request.claim]
    materialized.report_terminal(12.5)
    assert actions == ["published", "blocked"]
    (terminal,) = terminals
    assert terminal.description == "Loaded a Semantic V2 delivery."
    assert terminal.details["write_plan_key"] == world.node.node_id
    assert terminal.details["intent_id"] == "none"
    assert terminal.details["receive_count"] == 2


def test_the_graph_feasibility_callback_refuses_twelve_units_before_any_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = _load(monkeypatch)
    world = _world(monkeypatch)
    port = _ReattestingPort()
    runtime = replace(_graph_load_runtime(load, port), settings=_WIDE)
    monkeypatch.setattr(load, "RUNTIME", runtime)
    rosters = {"count": 12}

    def roster(_request: object) -> DeliveryRoster:
        return _roster(rosters["count"])

    monkeypatch.setattr(load, "prepared_roster", roster)
    commits: list[str] = []

    def ingest(request: SemanticIngestionRequest, _tooling: object) -> tuple[Any, ...]:
        request.feasibility(prepared())  # the callback runs before any authority mutation
        commits.append("publication")
        return ()

    monkeypatch.setattr(load, "ingest_semantic_delivery", ingest)
    with pytest.raises(ValidationError) as refused:
        load._materialize_graph(_graph_request(world))
    assert refused.value.context["reason"] == "units_exceed_cap"
    assert commits == []
    # Control: eleven units pass the same callback and the delivery commits.
    rosters["count"] = 11
    load._materialize_graph(replace(_graph_request(_world(monkeypatch))))
    assert commits == ["publication"]


# --- GRAPH and queued build the same ingestion inputs (D2 live acceptance, 2026-10-16) -----------
# Live in dev, every `records_cell_state` delivery on the GRAPH path quarantined CLOSED_ENUMERATION:
# `_materialize_graph` omitted the installed discard policies the queued `_execute` passes.


class _FixedPort:
    """The governed port returning one fixed delivery, so both paths read identical bytes."""

    def __init__(self, delivery: LoadedDelivery) -> None:
        self.delivery = delivery

    def fetch(self, manifest: ManifestInput, claim: MaterializationClaim | None = None) -> Any:
        del manifest, claim
        return self.delivery


_PATH_SPECIFIC_FIELDS = ("feasibility", "progress")
"""The only request fields allowed to differ: per-call closures (GRAPH adds its run bounds)."""


def test_graph_and_queued_build_the_same_ingestion_request_but_their_closures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses  # noqa: PLC0415

    from adapters.jobs.ingest_delivery.semantic_ingestion import (  # noqa: PLC0415
        SemanticIngestionRequest,
    )

    load = _load(monkeypatch)
    world = _world(monkeypatch)
    delivery = LoadedDelivery(
        manifest=cast("Manifest", object()), manifest_key=MANIFEST, text="bytes", landed_ref=LANDED
    )
    monkeypatch.setattr(
        load,
        "RUNTIME",
        replace(
            _graph_load_runtime(load, _ReattestingPort()), materialization=_FixedPort(delivery)
        ),
    )
    captured: list[SemanticIngestionRequest] = []

    def ingest(request: SemanticIngestionRequest, _tooling: object) -> tuple[Any, ...]:
        captured.append(request)
        return ()

    monkeypatch.setattr(load, "ingest_semantic_delivery", ingest)
    request = _graph_request(world)
    load._materialize_graph(request)
    queued = load.IngestionMessage(
        "raw-v2",
        request.manifest,
        load._execution_id(request.message_id, request.receive_count),
        load._execution_id(request.message_id, request.receive_count - 1),
        message_id=request.message_id,
        receive_count=request.receive_count,
    )
    load._execute(queued, request.now, lambda: 0)
    graph, queue = captured
    # Every field is accounted for: a new field must be added to one side or the other here.
    names = tuple(item.name for item in dataclasses.fields(SemanticIngestionRequest))
    assert names == (
        "delivery",
        "contracts",
        "columns",
        "now",
        "operation_id",
        "execution_id",
        "plan_digest",
        "predecessor_execution_id",
        "telemetry_operation_id",
        "installed_governance",
        "discard_policies",
        "progress",
        "feasibility",
    )
    shared = tuple(name for name in names if name not in _PATH_SPECIFIC_FIELDS)
    assert {name: getattr(graph, name) for name in shared} == {
        name: getattr(queue, name) for name in shared
    }
    assert graph.discard_policies == load.INSTALLED_DISCARD_POLICIES
    assert len(graph.discard_policies) == 14  # every installed discard_suppressed_rows pack
    assert graph.installed_governance == load.INSTALLED_GOVERNANCE


def _ntb_runtime(load: Any, text: str) -> tuple[Any, SemanticSidecarTooling]:
    """Compose the load runtime over in-memory semantic tooling, fetching one pack-1 delivery."""
    from test_new_to_brand_buyers import _delivery  # noqa: PLC0415
    from test_semantic_sidecar import _case  # noqa: PLC0415

    _request, tooling, _order, _attempts = _case()
    runtime = replace(
        _graph_load_runtime(load, _ReattestingPort()),
        tooling=tooling,
        materialization=_FixedPort(_delivery(text)),
    )
    return runtime, tooling


def _candidates(tooling: SemanticSidecarTooling) -> tuple[Any, ...]:
    """Return every candidate write request for the pack-1 fact, in write order."""
    from test_semantic_sidecar import MemoryCanonical  # noqa: PLC0415

    from ports.outbound.canonical import CanonicalDestination  # noqa: PLC0415

    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    return tuple(
        item
        for item in canonical.requests
        if item.destination is CanonicalDestination.CANDIDATE
        and item.publication.partition.scope.dataset.value == "fct_new_to_brand_buyers"
    )


def _loaded(events: list[Any]) -> list[Any]:
    """Return the delivery's terminal `Loaded a Semantic V2 delivery.` record(s)."""
    return [event for event in events if event.description == "Loaded a Semantic V2 delivery."]


def _ntb_text() -> str:
    from test_new_to_brand_buyers import KEPT, WITHHELD, _csv  # noqa: PLC0415

    return _csv(KEPT, WITHHELD)


def test_the_graph_path_discards_a_suppressed_row_and_publishes_the_kept_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the withheld row is dropped, the kept row publishes, the loss says so."""
    from test_new_to_brand_buyers import WEEK  # noqa: PLC0415

    load = _load(monkeypatch)
    runtime, tooling = _ntb_runtime(load, _ntb_text())
    monkeypatch.setattr(load, "RUNTIME", runtime)
    terminals: list[Any] = []

    def capture(event: object, _logger: object) -> None:
        terminals.append(event)

    monkeypatch.setattr(load, "report_operation", capture)
    materialized = load._materialize_graph(_graph_request(_world(monkeypatch)))
    (candidate,) = _candidates(tooling)
    assert tuple(dict(row) for row in candidate.rows) == (
        {
            "ad_product_key": None,
            "basis_key": "ad_attributed",
            "buyers_attributed": 9000,
            "buyers_new_to_brand_12mo": 4400,
            "campaign_key": None,
            "cell_state": "returned",
            "channel_group_key": None,
            "channel_key": None,
            "ecosystem_key": "amazon",
            "grain_set": "platform",
            "identity_unit": "amazon_user_id",
            "marketplace_key": "amazon_us",
            "period_end": WEEK.end,
            "period_start": WEEK.start,
            "platform_key": "amazon_ads",
            "source_timezone": "UTC",
            "time_grain": "week",
        },
    )
    evidence = {item.rule_id: item for item in candidate.publication.evidence.results}
    assert evidence["contract.amc-new-to-brand-buyers-platform.v1"].outcome.value == "pass"
    assert evidence["suppression:amc-new-to-brand-buyers-platform.cell-loss.v1"].detail == (
        '{"discarded_rows":1,"expected_cells":1,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )
    # The CURRENT unit is written: the delivery publishes.
    assert tuple((unit.unit_key, unit.table_kind) for unit in materialized.units) == (
        ("a_candidate.fct_new_to_brand_buyers", "candidate"),
        ("m_current.fct_new_to_brand_buyers", "current"),
        ("z_source.sa_amc_new_to_brand_buyers_platform", "source_aligned"),
    )
    materialized.report_terminal(1.0)
    (terminal,) = _loaded(terminals)
    assert terminal.details["outcomes"] == ["published"]


def test_the_graph_path_without_the_discard_policies_refuses_closed_enumeration_at_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the pre-fix input (no policies) quarantines at the contract assertion."""
    load = _load(monkeypatch)
    runtime, tooling = _ntb_runtime(load, _ntb_text())
    monkeypatch.setattr(load, "RUNTIME", runtime)
    monkeypatch.setattr(load, "INSTALLED_DISCARD_POLICIES", ())
    terminals: list[Any] = []

    def capture(event: object, _logger: object) -> None:
        terminals.append(event)

    monkeypatch.setattr(load, "report_operation", capture)
    materialized = load._materialize_graph(_graph_request(_world(monkeypatch)))
    (candidate,) = _candidates(tooling)
    failed = [
        (item.rule_id, item.family, item.outcome.value, item.detail)
        for item in candidate.publication.evidence.results
        if item.outcome.value != "pass"
    ]
    # The refusing layer is the load's contract assertion (domain.curation.ingestion), and only it.
    assert failed == [
        (
            "contract.amc-new-to-brand-buyers-platform.v1",
            "contract_assertions",
            "fail",
            "amc-new-to-brand-buyers-platform: records cell_state, so its delivery must come from "
            "a governed pack that declares discard_suppressed_rows; this one doesn't, so its "
            "withheld rows can't be discarded or its missing cells written not_returned.",
        )
    ]
    # No CURRENT unit: the quarantined candidate never becomes servable.
    assert tuple((unit.unit_key, unit.table_kind) for unit in materialized.units) == (
        ("a_candidate.fct_new_to_brand_buyers", "candidate"),
        ("z_source.sa_amc_new_to_brand_buyers_platform", "source_aligned"),
    )
    materialized.report_terminal(1.0)
    (terminal,) = _loaded(terminals)
    assert terminal.details["outcomes"] == ["blocked"]
    assert terminal.details["publications"] == 0
