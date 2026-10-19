# SPDX-License-Identifier: MIT-0
"""The whole GRAPH chain in process (S6 part 2, design §4 "Whole chain in process", G2-12).

One run goes admission, materialize, units, release, `publish`, precompute, the V3 bake (the
fan-out control, one task per roster row, the finalizer), the serving binding and a presented
page, over one transactional fake holding the graph, publications and preset pointer tables.
Every authority step is the real code: instantiation, the dispatcher, the claim decision, the
settlements (`settle_materialization`, `graph_unit`), the release issuer, `publish_claimed`,
the shared POST_GRANT consumer with the real precompute work and the real V3 fan-out, the V3
task and finalize consumers, and presentation's serving rule over the real graph reader. Only
the edges are fakes: the admitting transaction's items (`seed_run`, as S1 writes them), the
materialize and unit compute (their effect records, in slice 3's and 4b's exact shapes), the
bake's lake sources and stage-only persistence, and the slot assembler.

Under plan Addendum A3 semantic visibility stays at publish: each run flips its own
`GENERATION#<conflict_unit>` at its own publish, losers of a coalesced cohort included, and the
winner's finalizer writes only page pointers and its `FINALIZED` receipt.

Slice 7 test 14 runs two runs on one table through one unit batch, both published and served,
with a poison control: one run's unit fails on its final attempt, that run is fenced, and the
other run is still granted and served.

Three failure variants run beside it:
- a task exhausted on its final receive (`BAKE_RECOVERY#`) settles the cohort withheld: every page
  keeps the earlier grant's pointer and its build time, and a page with no pointer stays not
  available;
- `publish` dead-letters after staging: readers keep the previous generation, and a redrive
  completes the run;
- `publish` dead-letters after the flip: readers see the new generation, and a redrive folds
  and settles.
Every identity is fictional.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final, cast
from unittest.mock import patch

import pytest
from aws_lambda_powertools import Logger
from execution_graph_dispatch_support import (
    TABLE,
    SpySender,
    now_text,
    seed_run,
    set_mode,
    store,
    unit_specs,
    write_text,
)
from execution_graph_release_support import WINDOW, context, release_store
from graph_bake_v3_support import Bake, PointerKeys, current_identity
from graph_consumer_support import (
    CAMPAIGNS_7D,
    CAMPAIGNS_30D,
    RETAIL_7D,
    Clock,
    envelope,
    expire_claim,
    key,
    ports,
)
from graph_publish_support import (
    NOW,
    STAGE1_FLOW,
    Tables,
    materialize_record,
    publication,
    publications_store,
    seed_current,
)

from adapters.config import settings_without_client_secret
from adapters.inbound.ingestion_handlers import graph_publish
from adapters.inbound.ingestion_handlers.graph_claim import GraphClaimInput, claim_graph_envelope
from adapters.inbound.ingestion_handlers.graph_publish import (
    MAX_RECEIVES,
    PublishEnvelope,
    PublishPorts,
    PublishResult,
    PublishRetryExhaustedError,
    publish_claimed,
)
from adapters.inbound.ingestion_handlers.graph_unit import (
    UnitClaim,
    UnitSuccess,
    UnitTarget,
    settle_set_succeeded,
    unit_effect_record,
)
from adapters.jobs.execution_graph import post_grant_consumer
from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    InstantiationOutcome,
    ingestion_node_plan,
    instantiate_run,
)
from adapters.jobs.execution_graph.node_verifier import UnreachablePostGrantFacts
from adapters.jobs.execution_graph.post_grant_consumer import ConsumeResult, consume_post_grant
from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass
from adapters.jobs.execution_graph.release import (
    ReleaseCounts,
    ReleaseOutcome,
    release_run,
    release_shard,
)
from adapters.jobs.insight_precompute.graph_precompute import GraphPrecomputeWork
from adapters.jobs.insight_precompute.materializer import ArtifactPrecomputePlan
from adapters.jobs.preset_artifact_publisher import graph_bake
from adapters.jobs.preset_artifact_publisher.graph_bake import read_task_effect, task_node_name
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
from adapters.outbound.dynamodb.preset_artifacts import CURRENT_RECORD
from adapters.outbound.dynamodb.semantic_state import (
    DynamoCurrentPublications,
    FlipOutcome,
    FlipRequest,
    FoldOutcome,
)
from adapters.outbound.telemetry.reporting import Report
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import ArmScope, NodeState
from domain.operations.execution_graph_release import (
    BakeEffectKind,
    BakeRoster,
    BakeRosterRow,
    BindingKind,
    CohortMember,
    CohortOutcome,
    binding_authorizes_serving,
    serving_generation,
    serving_withheld,
    unavailable_reason,
)
from domain.operations.generated_execution_graphs import ROUTES
from domain.operations.status import canonical_authority_digest
from domain.presentation.graph_serving import PageGeneration
from domain.presentation.insights import InsightSlotState
from domain.presentation.preset_artifacts import (
    PresetArtifactBodyCeilingError,
    PresetArtifactPointer,
    PresetArtifactRevision,
)
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    MaterializationSettlement,
    NodeRecord,
    NodeSettlement,
    SuccessorRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

LOGGER = Logger(service="adlytics-graph-whole-chain-test")
LEASE = timedelta(seconds=960)
PAGES = (CAMPAIGNS_7D, CAMPAIGNS_30D, RETAIL_7D)
ROSTER = BakeRoster(
    (
        BakeRosterRow("campaigns", "7d", 240, 16.6, 123, 26),
        BakeRosterRow("campaigns", "30d", 300, 16.6, 123, 26),
        BakeRosterRow("retail-health", "7d", 420, 1.6, 123, 26),
    )
)
"""An injected three-row roster (A1.5): the chain's pages are data, not the contract's seven."""
BAKE_FINAL_RECEIVE = int(ROUTES["bake"]["max_receive_count"])
LANES = {"preset-bake-lane-0", "preset-bake-lane-1"}
_PRESENTATION_ENV = {
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    "ADL_ATHENA_WORKGROUP": "interactive",
    "ADL_PAGE_CONTEXT_TABLE": "page-context",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "semantic-attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "semantic-leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "semantic-publications",
    "ADL_SEMANTIC_V2_CURRENT_DATABASE": "semantic-current",
    "ADL_SEMANTIC_V2_BUCKET": "semantic-bucket",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.customer.com",
    "ADL_APP_BASE_URL": "https://app.customer.com",
    "ADL_API_BASE_URL": "https://api.customer.com",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
}
"""The presentation module resolves its settings at import (as `test_presentation_handler.py`)."""


def _unit(name: str) -> str:
    return canonical_authority_digest(["fictional-whole-chain", name])


@dataclass
class _Planner:
    def plan(self, inputs: Any) -> Any:
        return ArtifactPrecomputePlan(
            (), tuple((entry.key, "no_request") for entry in inputs.entries)
        )


@dataclass
class _Assembler:
    def many(self, requests: tuple[Any, ...], *, correlation_id: str = "") -> tuple[Any, ...]:
        del correlation_id
        return tuple(SimpleNamespace(state=InsightSlotState.FILLED) for _ in requests)


@dataclass
class _Generations:
    """The publications adapter with one scripted failure: after staging, or after the flip."""

    real: Any
    fail_flip: bool = False
    fail_fold: bool = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def flip_generation(self, request: FlipRequest) -> FlipOutcome:
        if self.fail_flip:
            raise DependencyError(ErrorCode("FICTIONAL", 1), "the flip transaction timed out")
        return self.real.flip_generation(request)

    def fold_for_grant(self, grant_id: str, partition_key: str) -> FoldOutcome:
        if self.fail_fold:
            raise DependencyError(ErrorCode("FICTIONAL", 2), "the fold update timed out")
        return self.real.fold_for_grant(grant_id, partition_key)


class ChainTables(PointerKeys, Tables):
    """The graph, publications and preset pointer tables in one transactional fake."""


@dataclass
class Chain:
    """The three tables, the bake's lake at the edge, and the other fakes at the edges."""

    tables: ChainTables = field(default_factory=ChainTables)
    clock: Clock = field(default_factory=Clock)
    metrics: list[str] = field(default_factory=list[str])
    sender: SpySender = field(default_factory=SpySender)
    generations: _Generations | None = None
    served: Callable[[Any, PresetArtifactPointer], PageGeneration | None] | None = None
    roster: BakeRoster = ROSTER
    lake: Bake = field(init=False)
    """The V3 bake world over these tables: its runtime, lake sources, queries and stage."""

    def __post_init__(self) -> None:
        set_mode(self.tables, AuthorityMode.GRAPH)
        self.generations = _Generations(publications_store(self.tables))
        self.redeploy(self.roster)

    def redeploy(self, roster: BakeRoster) -> None:
        """Compose the Baker over a roster, as a deploy does (the roster is composition data)."""
        self.roster = roster
        self.lake = Bake(roster=roster, tables=self.tables, sender=self.sender)

    # --- authority steps, each the real code ---------------------------------------------------

    def admit(self, name: str) -> str:
        """Write the admitting transaction's items (S1), with the window and its root clock."""
        seeded = seed_run(
            self.tables,
            fingerprint=canonical_authority_digest(["fingerprint", name]),
            unit=_unit(name),
            request_id=f"request-{name}",
        )
        run = seeded.record.run_id
        item = self.tables.items[(f"RUN#{run}", "RUN")]
        item["window"] = write_text(WINDOW)
        item["root_clock_started_at"] = write_text((NOW - timedelta(hours=2)).isoformat())
        return run

    def dispatch(self) -> None:
        for shard in due_shards():
            dispatch_shard(DispatchContext(store(self.tables), self.sender, now_text), shard)

    def arm(self) -> None:
        arm_pass(
            PromoterContext(release_store(self.tables), ingestion_node_plan, lambda: NOW),
            due_shards(),
            lambda: 60_000,
            "test-request",
        )

    def node(self, run: str, name: str) -> NodeRecord:
        found = release_store(self.tables).read_node(run, name)
        assert found is not None
        return found

    def batches(self) -> dict[str, str]:
        """Slice 7: each unit's batch, from the batch messages the dispatcher sent (D31)."""
        found: dict[str, str] = {}
        for body, _group, _dedup in self.sender.calls:
            message = json.loads(body)
            if message.get("node") == "write_unit_batch":
                for member in message["members"]:
                    found[member["node_id"]] = message["batch_id"]
        return found

    def claim(self, run: str, name: str, batch_id: str | None = None) -> ClaimRecord:
        node = self.node(run, name)
        disposition = claim_graph_envelope(
            store(self.tables),
            UnreachablePostGrantFacts(),
            GraphClaimInput(
                run_id=run,
                node_id=node.node_id,
                dispatch_id=node.dispatch_id,
                body=node.body,
                now=lambda: NOW,
                lease=LEASE,
                queue_enqueued_at=(NOW - timedelta(seconds=30)).isoformat(),
                batch_id=batch_id,
            ),
        )
        assert disposition.claim is not None, disposition.outcome
        return disposition.claim

    def materialize(self, run: str, units: dict[str, tuple[SuccessfulPublication, ...]]) -> None:
        """Instantiate and dispatch `materialize_raw`, claim it, settle it with its units."""
        outcome = instantiate_run(
            store(self.tables), InstantiateRequest(run, ingestion_node_plan, now_text)
        )
        assert outcome is InstantiationOutcome.ARMED
        self.dispatch()
        assert self.node(run, "materialize_raw").state is NodeState.DISPATCHED
        claim = self.claim(run, "materialize_raw")
        record, digest = materialize_record(tuple(sorted(units)), ())
        current = store(self.tables).read_run(run)
        assert current is not None
        store(self.tables).settle_materialization(
            MaterializationSettlement(
                NodeSettlement(
                    current,
                    self.node(run, "materialize_raw"),
                    NodeState.SUCCEEDED,
                    now_text(),
                    claim=claim,
                    effect_digest=digest,
                    effect_record=record,
                ),
                successors=SuccessorRequest(
                    current, unit_specs(run, *sorted(units)), ArmScope.UNITS, True, now_text()
                ),
            )
        )

    def write_units(self, run: str, units: dict[str, tuple[SuccessfulPublication, ...]]) -> None:
        """Arm, dispatch, claim and settle each unit with its servable targets (D14).

        Slice 7 (conversion 3): each unit is claimed through its table's batch message and
        settles through `settle_set_succeeded`, the one `SUCCEEDED` path (D34).
        """
        self.arm()
        self.dispatch()
        batches = self.batches()
        for unit_key, targets in sorted(units.items()):
            name = f"write_unit:{unit_key}"
            node = self.node(run, name)
            batch_id = batches[node.node_id]
            claim = self.claim(run, name, batch_id)
            record, digest = unit_effect_record(
                unit_key,
                STAGE1_FLOW,
                tuple(
                    UnitTarget(
                        item.partition.identity, item.publication_id.value, item.fencing_token
                    )
                    for item in targets
                ),
            )
            unit = UnitClaim(
                store(self.tables),
                claim,
                node.dispatch_id,
                1,
                lambda: NOW,
                Report(operation="table_writer", correlation_id=name, logger=LOGGER),
                batch_id=batch_id,
            )
            settled = settle_set_succeeded(
                (UnitSuccess(unit, record, digest),), lock_key=node.lock_key, batch_id=batch_id
            )
            assert settled[node.node_id].node_state is NodeState.SUCCEEDED

    def unit(  # noqa: PLR0913, PLR0917 -- one claim builder with an explicit receive count
        self, run: str, name: str, batch_id: str, receive_count: int = 1
    ) -> UnitClaim:
        """Claim one unit through its table's batch message (D31) at a receive count."""
        node = self.node(run, name)
        return UnitClaim(
            store(self.tables),
            self.claim(run, name, batch_id),
            node.dispatch_id,
            receive_count,
            lambda: NOW,
            Report(operation="table_writer", correlation_id=name, logger=LOGGER),
            batch_id=batch_id,
        )

    def release(self, run: str) -> None:
        assert release_run(context(self.tables), run) is ReleaseOutcome.APPLIED

    def publish(self, run: str, receive_count: int = 1) -> PublishResult:
        """Dispatch `publish` (on its first receive), claim it and run the executor."""
        if receive_count == 1 and self.node(run, "publish").state is NodeState.READY:
            self.dispatch()
        node = self.node(run, "publish")
        body = json.loads(node.body)
        claim = self.claim(run, "publish")
        return publish_claimed(
            store(self.tables),
            PublishPorts(cast("Any", self.generations), TABLE, self.metrics.append),
            PublishEnvelope(
                run_id=run,
                node_id=node.node_id,
                dispatch_id=node.dispatch_id,
                grant_id=str(body["grant_id"]),
                granted_run=str(body["granted_run"]),
                conflict_unit=str(body["conflict_unit"]),
                receive_count=receive_count,
            ),
            claim,
            lambda: NOW,
            LOGGER,
        )

    def precompute(self, run: str) -> ConsumeResult:
        self.arm()
        self.dispatch()
        return consume_post_grant(
            ports(self.tables, self.clock, self.metrics),
            envelope(self.node(run, "precompute"), CohortMember.PRECOMPUTE),
            GraphPrecomputeWork(self.roster, _Planner(), _Assembler(), 40),
        )

    def fan_out(self, run: str) -> ConsumeResult:
        """Arm and dispatch the `bake` control, then consume it: pin, partition, arm tasks."""
        self.arm()
        self.dispatch()
        return self.lake.fan_out(run)

    def tasks(self, run: str) -> list[ConsumeResult]:
        """Dispatch the roster tasks on their lanes and consume each once."""
        self.dispatch()
        return [self.lake.task(run, key) for key in self.roster.keys]

    def finalize(self, run: str, receive_count: int = 1) -> ConsumeResult:
        """Arm the finalizer, then consume one delivery or redelivery."""
        self.arm()
        self.dispatch()
        return self.lake.deliver(run, "bake_finalize", receive_count)

    def generation(self, run: str) -> dict[str, Any] | None:
        """Return the run's own semantic `GENERATION#<conflict_unit>` pointer, if any."""
        return self.tables.pointer(self.run_item(run)["conflict_unit"]["S"])

    # --- reads -------------------------------------------------------------------------------

    def run_item(self, run: str) -> dict[str, Any]:
        return self.tables.items[(f"RUN#{run}", "RUN")]

    def grant(self, run: str) -> str:
        record = release_store(self.tables).read_run(run)
        assert record is not None and record.grant_id is not None
        return record.grant_id

    def resolved(self, item: SuccessfulPublication) -> str:
        """Return the publication a serving reader resolves for the item's partition."""
        found = self.reader().current(item.partition)
        assert found is not None
        return found.publication_id.value

    def reader(self) -> DynamoCurrentPublications:
        return DynamoCurrentPublications(
            "fictional-publications", client=cast("Any", self.tables), retry_pause=lambda: None
        )

    def present(
        self, intent: BakeIntent
    ) -> tuple[PresetArtifactPointer | None, PageGeneration | None]:
        """Presentation's serving rule for one page: its pointer and its build-time label.

        No pointer is the existing not-available page (`PRESENTATION_UNAVAILABLE`); a pointer,
        read from the pointer table, is served only through `_served_generation` over the real
        graph reader.
        """
        pointer = self.tables.page_pointer(intent.page, intent.variant)
        if pointer is None:
            return None, None
        assert self.served is not None
        runtime = SimpleNamespace(
            graph_serving=DynamoPresentationGraphReader(TABLE, cast("Any", self.tables))
        )
        return pointer, self.served(runtime, pointer)


@pytest.fixture(scope="module")
def served() -> Callable[[Any, PresetArtifactPointer], PageGeneration | None]:
    """Presentation's `_served_generation`, imported under fictional settings."""
    settings_without_client_secret.cache_clear()
    with patch.dict("os.environ", _PRESENTATION_ENV, clear=False):
        module = importlib.import_module("adapters.inbound.presentation_handlers.handler")
    settings_without_client_secret.cache_clear()
    return cast("Any", module)._served_generation


@pytest.fixture
def make_chain(
    served: Callable[[Any, PresetArtifactPointer], PageGeneration | None],
) -> Callable[..., Chain]:
    """Build a chain whose presentation step is the real serving rule."""

    def build(**options: Any) -> Chain:
        return Chain(served=served, **options)

    return build


@pytest.fixture
def errors(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Capture `publish`'s reported errors (the retry path reports each receive once)."""
    reported: list[BaseException] = []

    def record(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(graph_publish, "report_error", record)
    monkeypatch.setattr("adapters.jobs.preset_artifact_publisher.graph_bake.report_error", record)
    return reported


def _targets(
    chain: Chain, name: str, day: date
) -> tuple[
    dict[str, tuple[SuccessfulPublication, ...]], SuccessfulPublication, SuccessfulPublication
]:
    """One unit advancing a curation publication over an older base, seeded in CURRENT."""
    old = publication(day, f"{name}-old", 1)
    new = publication(day, f"{name}-new", 2, curation=True)
    seed_current(chain.tables, new, old)
    return {f"db.{name}": (new,)}, old, new


def _through_publish(chain: Chain, name: str, day: date) -> tuple[str, Any, Any]:
    run = chain.admit(name)
    units, old, new = _targets(chain, name, day)
    chain.materialize(run, units)
    chain.write_units(run, units)
    chain.release(run)
    return run, old, new


def _serve(chain: Chain, run: str) -> None:
    """Serve one winner: bake tasks, readiness, precompute, then finalization."""
    assert chain.fan_out(run).node_state is NodeState.SUCCEEDED
    assert {result.node_state for result in chain.tasks(run)} == {NodeState.SUCCEEDED}
    armed = chain.finalize(run)
    assert (armed.acknowledge, armed.outcome) == (
        False,
        "graph_bake_precompute_armed",
    )
    assert chain.precompute(run).node_state is NodeState.SUCCEEDED
    assert chain.finalize(run, 2).node_state is NodeState.SUCCEEDED


def _require_flipped(chain: Chain, run: str) -> dict[str, Any]:
    """A3 (a): the run's own semantic pointer names its own grant; return the pointer item."""
    pointer = chain.generation(run)
    assert pointer is not None, f"{run}: GENERATION# was never flipped"
    assert pointer["grant_id"]["S"] == chain.grant(run), f"{run}: GENERATION# names another grant"
    return copy.deepcopy(pointer)


# --- the whole chain ---------------------------------------------------------------------------


def test_the_whole_chain_runs_admission_to_a_presented_page_and_succeeded(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    run = chain.admit("a")
    units, old, new = _targets(chain, "a", date(2026, 9, 1))
    chain.materialize(run, units)
    assert chain.node(run, "materialize_raw").state is NodeState.SUCCEEDED
    chain.write_units(run, units)
    assert chain.node(run, "write_unit:db.a").state is NodeState.SUCCEEDED
    chain.release(run)
    assert chain.run_item(run)["state"]["S"] == "GRANTED"
    # Before publish, readers resolve the previous generation.
    assert chain.resolved(new) == old.publication_id.value

    published = chain.publish(run)

    assert (published.acknowledge, published.node_state) == (True, NodeState.SUCCEEDED)
    assert chain.resolved(new) == new.publication_id.value
    flipped = _require_flipped(chain, run)  # A3: semantic visibility is publish's
    # DB25 serving order: bake fan-out/tasks, readiness arm, precompute, finalization.
    assert chain.fan_out(run).node_state is NodeState.SUCCEEDED
    assert chain.present(CAMPAIGNS_7D) == (None, None)  # no page before the finalizer
    assert {result.node_state for result in chain.tasks(run)} == {NodeState.SUCCEEDED}
    assert chain.present(CAMPAIGNS_7D) == (None, None)  # a task has no pointer capability
    armed = chain.finalize(run)
    assert (armed.acknowledge, armed.outcome) == (
        False,
        "graph_bake_precompute_armed",
    )
    assert chain.precompute(run).node_state is NodeState.SUCCEEDED
    finalized = chain.finalize(run, 2)
    assert (finalized.acknowledge, finalized.node_state) == (True, NodeState.SUCCEEDED)

    item = chain.run_item(run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    binding = release_store(chain.tables).read_serving_binding(chain.grant(run))
    assert binding is not None and binding.unavailable == ()
    assert binding.artifact_revision == serving_generation(chain.grant(run))
    for intent in PAGES:
        pointer, generation = chain.present(intent)
        assert pointer is not None and pointer.grant_id == chain.grant(run)
        assert generation == PageGeneration(built_at=pointer.written_at)
    # A3: the finalizer left the semantic pointer exactly as publish set it.
    assert chain.generation(run) == flipped
    assert chain.resolved(new) == new.publication_id.value
    assert errors == []
    assert chain.metrics == []
    # Every route the chain used was sent through the real dispatcher: materialize, the unit
    # batch, publish, precompute, the fan-out control, one message per task, the finalizer.
    assert len(chain.sender.calls) == 6 + len(ROSTER.rows)
    task_groups = {group for _body, group, _dedup in chain.sender.calls if group in LANES}
    assert task_groups <= LANES


# --- variant 1: a task exhausted on its final receive ------------------------------------------


def test_a_task_exhausted_on_its_final_receive_settles_withheld_and_keeps_bound_pointers(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    """Plan 4.6: an exhausted task is `BAKE_RECOVERY#`, never a partial binding (no partial).

    B4: the finalizer arms once every task is terminal and abandons the cohort withheld, so the
    run completes and its recovery obligation is deleted with the completion.
    """
    chain = make_chain(roster=BakeRoster(ROSTER.rows[:2]))  # G1's roster: campaigns only
    g1, _, _ = _through_publish(chain, "g1", date(2026, 9, 1))
    assert chain.publish(g1).node_state is NodeState.SUCCEEDED
    _serve(chain, g1)
    kept = {intent: chain.present(intent) for intent in (CAMPAIGNS_7D, CAMPAIGNS_30D)}
    assert {pointer.grant_id for pointer, _ in kept.values() if pointer} == {chain.grant(g1)}
    assert chain.present(RETAIL_7D) == (None, None)

    # G2 (another window, after a deploy adding retail) never gets Athena for campaigns#7d.
    chain.redeploy(ROSTER)
    g2, _, _ = _through_publish(chain, "g2", date(2026, 9, 2))
    assert chain.publish(g2).node_state is NodeState.SUCCEEDED
    _require_flipped(chain, g2)  # A3: G2's semantic data is servable at its publish
    assert chain.fan_out(g2).node_state is NodeState.SUCCEEDED
    chain.dispatch()
    failing = key(CAMPAIGNS_7D)
    chain.lake.queries.fail[failing] = DependencyError(ErrorCode("FICTIONAL", 3), "throttled")
    retried = chain.lake.task(g2, failing)
    assert (retried.acknowledge, retried.node_state) == (False, NodeState.CLAIMED)
    task = chain.node(g2, task_node_name(failing))
    expire_claim(chain.tables, task)
    exhausted = chain.lake.task(g2, failing, receive_count=BAKE_FINAL_RECEIVE)
    assert exhausted.node_state is NodeState.FAILED
    for other in (CAMPAIGNS_30D, RETAIL_7D):
        assert chain.lake.task(g2, key(other)).node_state is NodeState.SUCCEEDED
    chain.arm()

    recovery = chain.tables.items[(due_shard(_unit("g2")), f"BAKE_RECOVERY#{task.node_id}")]
    assert recovery["reason"]["S"] == "retry_exhausted"
    # B4: every task is terminal, so the finalizer arms and abandons the cohort withheld.
    assert chain.node(g2, "bake_finalize").state is NodeState.READY
    abandoned = chain.finalize(g2)
    assert (abandoned.outcome, abandoned.node_state) == (
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    assert _release_pass(chain, []).completed == 1
    assert chain.run_item(g2)["state"]["S"] == "SUCCEEDED"
    binding = release_store(chain.tables).read_serving_binding(chain.grant(g2))
    assert binding is not None and serving_withheld(binding)
    assert (due_shard(_unit("g2")), f"BAKE_RECOVERY#{task.node_id}") not in chain.tables.items
    # Every page keeps its last bound pointer (G1's) and that generation's build time; no G2
    # page is visible, though two of its three tasks succeeded and staged.
    for intent, (pointer, generation) in kept.items():
        assert chain.present(intent) == (pointer, generation)
        assert pointer is not None and generation == PageGeneration(built_at=pointer.written_at)
    assert key(RETAIL_7D) in chain.lake.stage.staged
    # A page with no bound pointer is not available.
    assert chain.present(RETAIL_7D) == (None, None)
    assert errors == []


@pytest.mark.parametrize("count", [3, 9])
def test_db25_whole_chain_is_roster_generic(count: int, make_chain: Callable[..., Chain]) -> None:
    dynamic = BakeRoster(
        tuple(
            BakeRosterRow("campaigns", f"{index + 1}d", 240, 16.6, 123, 26)
            for index in range(count)
        )
    )
    chain = make_chain(roster=dynamic)
    run, _, _ = _through_publish(chain, f"roster-{count}", date(2026, 9, 1))
    assert chain.publish(run).node_state is NodeState.SUCCEEDED

    _serve(chain, run)

    assert chain.run_item(run)["state"]["S"] == "SUCCEEDED"
    contribution = next(
        item
        for item in release_store(chain.tables).read_contributions(chain.grant(run))
        if item.member is CohortMember.PRECOMPUTE
    )
    assert tuple(key for key, _ in contribution.request_roster) == dynamic.keys


def test_db25_control_nine_row_chain_never_arms_from_seven_effects(
    make_chain: Callable[..., Chain],
) -> None:
    dynamic = BakeRoster(
        tuple(BakeRosterRow("campaigns", f"{index + 1}d", 240, 16.6, 123, 26) for index in range(9))
    )
    chain = make_chain(roster=dynamic)
    run, _, _ = _through_publish(chain, "roster-subset", date(2026, 9, 1))
    assert chain.publish(run).node_state is NodeState.SUCCEEDED
    assert chain.fan_out(run).node_state is NodeState.SUCCEEDED
    chain.dispatch()
    for task_key in dynamic.keys[:7]:
        assert chain.lake.task(run, task_key).node_state is NodeState.SUCCEEDED

    chain.arm()

    assert chain.node(run, "bake_finalize").state is NodeState.PENDING
    assert chain.node(run, "precompute").state is NodeState.WAITING_INPUT
    assert release_store(chain.tables).read_precompute_input(run) is None
    assert release_store(chain.tables).read_serving_binding(chain.grant(run)) is None


# --- variant 2: publish dead-letters after staging --------------------------------------------


def test_publish_dead_lettered_after_staging_keeps_the_previous_generation_until_redrive(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    run, old, new = _through_publish(chain, "s", date(2026, 9, 3))
    assert chain.generations is not None
    chain.generations.fail_flip = True

    for receive in range(1, MAX_RECEIVES + 1):
        result = chain.publish(run, receive_count=receive)
        assert (result.acknowledge, result.outcome) == (False, "graph_publish_retryable")
    # The final receive is reported exhausted on its way to the dead-letter queue (§2.12).
    assert isinstance(errors[-1], PublishRetryExhaustedError)
    assert chain.node(run, "publish").state is NodeState.CLAIMED
    current = chain.tables.current(new.partition.identity)
    assert any(name.startswith("staged#") for name in current)  # staged, never flipped
    assert chain.tables.pointer(_unit("s")) is None
    assert chain.resolved(new) == old.publication_id.value

    # The operator's redrive is a new message: receive count 1, a RECLAIM of the released lease.
    chain.generations.fail_flip = False
    redriven = chain.publish(run, receive_count=1)

    assert (redriven.acknowledge, redriven.node_state) == (True, NodeState.SUCCEEDED)
    assert chain.resolved(new) == new.publication_id.value
    _serve(chain, run)
    item = chain.run_item(run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")


# --- variant 3: publish dead-letters after the flip -------------------------------------------


def test_publish_dead_lettered_after_the_flip_serves_new_and_the_redrive_folds_and_settles(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    run, _, new = _through_publish(chain, "f", date(2026, 9, 4))
    assert chain.generations is not None
    chain.generations.fail_fold = True

    for receive in range(1, MAX_RECEIVES + 1):
        result = chain.publish(run, receive_count=receive)
        assert (result.acknowledge, result.outcome) == (False, "graph_publish_retryable")
    assert isinstance(errors[-1], PublishRetryExhaustedError)
    assert chain.node(run, "publish").state is NodeState.CLAIMED
    pointer = chain.tables.pointer(_unit("f"))
    assert pointer is not None and pointer["grant_id"]["S"] == chain.grant(run)
    assert chain.resolved(new) == new.publication_id.value
    assert any(name.startswith("staged#") for name in chain.tables.current(new.partition.identity))

    chain.generations.fail_fold = False
    redriven = chain.publish(run, receive_count=1)

    assert (redriven.acknowledge, redriven.node_state) == (True, NodeState.SUCCEEDED)
    # The replay folded: the staged entry is gone and resolution is unchanged (still new).
    current = chain.tables.current(new.partition.identity)
    assert not any(name.startswith("staged#") for name in current)
    assert chain.resolved(new) == new.publication_id.value
    _serve(chain, run)
    item = chain.run_item(run)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")


# --- slice 7 test 14: two runs on one table through one batch ---------------------------------

SHARED_TABLE = "db.shared"


def _shared_targets(
    chain: Chain, name: str, day: date
) -> tuple[
    dict[str, tuple[SuccessfulPublication, ...]], SuccessfulPublication, SuccessfulPublication
]:
    """One run's unit on the shared table: a curation publication over an older base."""
    old = publication(day, f"{name}-old", 1)
    new = publication(day, f"{name}-new", 2, curation=True)
    seed_current(chain.tables, new, old)
    return {SHARED_TABLE: (new,)}, old, new


def _success(unit: UnitClaim, targets: tuple[SuccessfulPublication, ...]) -> UnitSuccess:
    record, digest = unit_effect_record(
        SHARED_TABLE,
        STAGE1_FLOW,
        tuple(
            UnitTarget(item.partition.identity, item.publication_id.value, item.fencing_token)
            for item in targets
        ),
    )
    return UnitSuccess(unit, record, digest)


def _two_runs_one_batch(chain: Chain) -> tuple[dict[str, Any], str]:
    """Admit and materialize two runs whose units share `TABLE#db.shared`, then arm once."""
    runs: dict[str, Any] = {}
    for name, day in (("a", date(2026, 9, 5)), ("b", date(2026, 9, 6))):
        run = chain.admit(name)
        units, old, new = _shared_targets(chain, name, day)
        chain.materialize(run, units)
        runs[name] = SimpleNamespace(run=run, units=units, old=old, new=new)
    chain.arm()
    chain.dispatch()
    batches = chain.batches()
    unit_name = f"write_unit:{SHARED_TABLE}"
    ids = {batches[chain.node(item.run, unit_name).node_id] for item in runs.values()}
    assert len(ids) == 1  # one TABLE#db.shared grant covers both runs' units
    (batch_id,) = ids
    return runs, batch_id


def _lock_open(chain: Chain) -> int:
    return int(chain.tables.items[(f"LOCK#TABLE#{SHARED_TABLE}", "LOCK")]["open_members"]["N"])


def test_slice7_14_two_runs_on_one_table_settle_in_one_batch_and_both_serve(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    runs, batch_id = _two_runs_one_batch(chain)
    unit_name = f"write_unit:{SHARED_TABLE}"
    assert _lock_open(chain) == 2
    successes = [
        _success(chain.unit(item.run, unit_name, batch_id), item.units[SHARED_TABLE])
        for item in runs.values()
    ]
    settled = settle_set_succeeded(successes, lock_key=f"TABLE#{SHARED_TABLE}", batch_id=batch_id)
    assert {result.node_state for result in settled.values()} == {NodeState.SUCCEEDED}
    assert _lock_open(chain) == 0
    for item in runs.values():
        assert chain.node(item.run, unit_name).batch_id == batch_id
        chain.release(item.run)
        assert chain.resolved(item.new) == item.old.publication_id.value  # before publish
        assert chain.publish(item.run).node_state is NodeState.SUCCEEDED
        assert chain.resolved(item.new) == item.new.publication_id.value
        _serve(chain, item.run)
        state = chain.run_item(item.run)
        assert (state["state"]["S"], state["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    assert errors == []


def test_slice7_14_control_a_poison_fails_its_run_only_and_the_other_run_serves(
    make_chain: Callable[..., Chain],
    errors: list[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters.inbound.ingestion_handlers import graph_unit  # noqa: PLC0415
    from domain.operations.execution_graph_dispatch import (  # noqa: PLC0415
        ROUTES,
        SEMANTIC_WRITE_ROUTE,
    )

    alarms: list[BaseException] = []

    def capture(error: BaseException, _report: object) -> None:
        alarms.append(error)

    monkeypatch.setattr(graph_unit, "report_error", capture)
    chain = make_chain()
    runs, batch_id = _two_runs_one_batch(chain)
    unit_name = f"write_unit:{SHARED_TABLE}"
    poison, healthy = runs["a"], runs["b"]
    final = int(ROUTES[SEMANTIC_WRITE_ROUTE]["max_receive_count"])
    isolated = chain.unit(poison.run, unit_name, batch_id, receive_count=final)
    assert isolated.final is True
    # The isolated poison's final attempt (D35): FAILED, its share released, its run fenced.
    cause = DependencyError(ErrorCode("FICTIONAL", 9), "injected statement failure")
    failed = graph_unit.settle_failed(isolated, "statement_failed", cause)
    assert (failed.node_state, failed.failure_reason) == (NodeState.FAILED, "statement_failed")
    assert _lock_open(chain) == 1  # the healthy member still holds its share
    (alarm,) = alarms
    assert str(cast("Any", alarm).code) == "ADLY-TABLE_WRITER-0006"
    assert cast("Any", alarm).context["batch_id"] == batch_id
    settled = settle_set_succeeded(
        [_success(chain.unit(healthy.run, unit_name, batch_id), healthy.units[SHARED_TABLE])],
        lock_key=f"TABLE#{SHARED_TABLE}",
        batch_id=batch_id,
    )
    assert [result.node_state for result in settled.values()] == [NodeState.SUCCEEDED]
    assert _lock_open(chain) == 0  # a poison never holds the table (4b)
    fenced = release_store(chain.tables).read_run(poison.run)
    assert fenced is not None and fenced.fence_reason is not None
    assert fenced.fence_reason.value == "node_failed"
    assert release_run(context(chain.tables), poison.run) is ReleaseOutcome.NOT_LIVE
    assert chain.node(poison.run, unit_name).failure_code == "ADLY-FICTIONAL-0009"
    # The other run is granted, published and served while the failed run is fenced.
    chain.release(healthy.run)
    assert chain.publish(healthy.run).node_state is NodeState.SUCCEEDED
    _serve(chain, healthy.run)
    state = chain.run_item(healthy.run)
    assert (state["state"]["S"], state["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    assert chain.resolved(healthy.new) == healthy.new.publication_id.value
    # The failed run's readers keep the previous generation.
    assert chain.resolved(poison.new) == poison.old.publication_id.value
    assert errors == []


# --- Addendum A3 (a): every cohort member flips its own semantic pointer at its own publish --


def _granted_pair(chain: Chain) -> dict[str, Any]:
    """Admit, materialize, write and release two runs of one co-release serving domain."""
    runs: dict[str, Any] = {}
    for name, day in (("early", date(2026, 9, 7)), ("late", date(2026, 9, 8))):
        run, old, new = _through_publish(chain, name, day)
        runs[name] = SimpleNamespace(run=run, old=old, new=new)
    return runs


def _elected(chain: Chain, runs: dict[str, Any]) -> tuple[Any, Any]:
    """Run the election once; return (winner, loser) by whose `bake` it armed."""
    chain.arm()
    armed = [
        item
        for item in runs.values()
        if chain.node(item.run, "bake").state is not NodeState.PENDING
    ]
    assert len(armed) == 1, "one cohort elects exactly one winner"
    (winner,) = armed
    (loser,) = (item for item in runs.values() if item is not winner)
    return winner, loser


def test_a3_every_cohort_member_flips_at_its_own_publish_and_the_finalizer_keeps_them(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    runs = _granted_pair(chain)
    flipped: dict[str, dict[str, Any]] = {}
    for item in runs.values():
        assert chain.resolved(item.new) == item.old.publication_id.value  # before its publish
        assert chain.publish(item.run).node_state is NodeState.SUCCEEDED
        flipped[item.run] = _require_flipped(chain, item.run)  # at its own publish
        assert chain.resolved(item.new) == item.new.publication_id.value
    winner, loser = _elected(chain, runs)

    _serve(chain, winner.run)
    chain.arm()  # the bound domain drains the loser onto the winner's binding

    for item in (winner, loser):
        state = chain.run_item(item.run)
        assert (state["state"]["S"], state["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
        # The finalizer and the drain left every member's semantic pointer as publish set it.
        assert chain.generation(item.run) == flipped[item.run]
        assert chain.resolved(item.new) == item.new.publication_id.value
    assert chain.node(loser.run, "bake").state is NodeState.COALESCED
    assert not any(
        task.name.startswith("bake_task")
        for task in release_store(chain.tables).read_nodes(loser.run)
    )
    for intent in PAGES:
        pointer, _generation = chain.present(intent)
        assert pointer is not None and pointer.grant_id == chain.grant(winner.run)
    assert errors == []


_RETIRED_SERVING_PREFIXES: Final = ("PUBLISH_WAIT#", "CAPACITY_WAIT#", "SERVING_DAY#")


def _heads(chain: Chain) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        key: item
        for key, item in chain.tables.items.items()
        if key[0].startswith("SERVING_DOMAIN#") and key[1] == "HEAD"
    }


def test_rolling_wave_b_publishes_while_a_is_active_and_a_finalizes_on_its_snapshot(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    """Plan item 4: B flips and settles during ACTIVE A; A finalizes on its frozen authority."""
    chain = make_chain()
    wave_a, _a_old, a_new = _through_publish(chain, "wave-a", date(2026, 9, 7))
    assert chain.publish(wave_a).node_state is NodeState.SUCCEEDED
    chain.arm()  # A elects alone
    ((head_key, head),) = _heads(chain).items()
    assert (head["state"]["S"], head["winner_run_id"]["S"]) == ("ACTIVE", wave_a)
    frozen = copy.deepcopy(head)

    wave_b, b_old, b_new = _through_publish(chain, "wave-b", date(2026, 9, 8))
    assert chain.resolved(b_new) == b_old.publication_id.value  # before its publish
    assert chain.publish(wave_b).node_state is NodeState.SUCCEEDED  # no park, no wait
    _require_flipped(chain, wave_b)
    assert chain.resolved(b_new) == b_new.publication_id.value
    assert chain.tables.items[head_key] == frozen  # B never joins or mutates A
    assert wave_b not in {entry["S"] for entry in frozen["elected_run_ids"]["L"]}
    assert chain.node(wave_b, "bake").state is NodeState.PENDING

    _serve(chain, wave_a)

    state = chain.run_item(wave_a)
    assert (state["state"]["S"], state["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    assert chain.resolved(a_new) == a_new.publication_id.value
    assert chain.node(wave_b, "bake").state is NodeState.PENDING  # not a member of A
    chain.arm()  # A's drained domain elects B as the next immutable cohort
    successor = chain.tables.items[head_key]
    assert (successor["state"]["S"], successor["winner_run_id"]["S"]) == ("ACTIVE", wave_b)
    assert successor["cohort_revision"] != frozen["cohort_revision"]
    assert chain.node(wave_b, "bake").state is not NodeState.PENDING
    assert not any(
        pk.startswith(_RETIRED_SERVING_PREFIXES) or sk.startswith(_RETIRED_SERVING_PREFIXES)
        for pk, sk in chain.tables.items
    )
    assert errors == []


def test_a3_control_a_skipped_loser_publish_fails_the_flip_requirement(
    make_chain: Callable[..., Chain],
) -> None:
    chain = make_chain()
    runs = _granted_pair(chain)
    skipped, published = runs["early"], runs["late"]
    assert chain.publish(published.run).node_state is NodeState.SUCCEEDED
    _require_flipped(chain, published.run)  # the published member passes

    with pytest.raises(AssertionError, match="GENERATION# was never flipped"):
        _require_flipped(chain, skipped.run)
    assert chain.resolved(skipped.new) == skipped.old.publication_id.value


# --- ADR-140: a bake that skips the finalizer still settles its cohort -----------------------

LIVE_BAKE_SETTLED = datetime(2026, 10, 16, 23, 21, 50, 759789, tzinfo=UTC)
"""The live wedge's U1: consumer step 3 settled the winner's bake `NO_ELIGIBLE` (no finalizer)."""


def _uncurated_through_release(chain: Chain, name: str, day: date) -> str:
    """Admit, materialize, write and release one run that advances no curation publication."""
    run = chain.admit(name)
    old = publication(day, f"{name}-old", 1)
    new = publication(day, f"{name}-new", 2)
    seed_current(chain.tables, new, old)
    units = {f"db.{name}": (new,)}
    chain.materialize(run, units)
    chain.write_units(run, units)
    chain.release(run)
    return run


def _release_pass(chain: Chain, signals: list[int]) -> ReleaseCounts:
    """One Release sweep of every due shard, recording `GraphServingWithheld` datapoints."""
    released = replace(context(chain.tables), serving_signal=signals.append)
    total = ReleaseCounts()
    for shard in due_shards():
        counts = release_shard(released, shard)
        total.unarmed_settled += counts.unarmed_settled
        total.completed += counts.completed
        total.failed += counts.failed
    return total


def _head(chain: Chain) -> dict[str, Any]:
    ((_key, head),) = _heads(chain).items()
    return head


def _pages(chain: Chain) -> dict[BakeIntent, Any]:
    return {intent: chain.present(intent) for intent in PAGES}


def _binding(chain: Chain, run: str) -> Any:
    return release_store(chain.tables).read_serving_binding(chain.grant(run))


@pytest.mark.covers("ADR-140")
def test_adr140_w1_the_live_u1_replay_settles_on_one_release_pass_and_the_next_cohort_serves(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    g1, _old, _new = _through_publish(chain, "g1", date(2026, 9, 1))
    assert chain.publish(g1).node_state is NodeState.SUCCEEDED
    _serve(chain, g1)
    served = _pages(chain)
    assert {pointer.grant_id for pointer, _ in served.values() if pointer} == {chain.grant(g1)}
    # The live wedge: a sole-member winner with no curation settles its bake U1, no finalizer.
    run2 = _uncurated_through_release(chain, "run2", date(2026, 9, 2))
    assert chain.publish(run2).node_state is NodeState.SUCCEEDED
    assert chain.fan_out(run2).node_state is NodeState.SUCCEEDED
    assert chain.node(run2, "precompute").state is NodeState.WAITING_INPUT
    assert (_head(chain)["state"]["S"], _head(chain)["winner_run_id"]["S"]) == ("ACTIVE", run2)
    signals: list[int] = []

    counts = _release_pass(chain, signals)

    assert (counts.unarmed_settled, counts.completed, counts.failed) == (1, 1, 0)
    item = chain.run_item(run2)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "no_eligible")
    assert not any(sk == f"COMPLETE#{run2}" for _pk, sk in chain.tables.items)
    assert _head(chain)["state"]["S"] == "IDLE"
    assert _pages(chain) == served  # byte-identical pointers, G1 serves with the same built_at
    assert signals == []
    # The next curated publication is elected, bakes and serves.
    g3, _old, _new = _through_publish(chain, "g3", date(2026, 9, 3))
    assert chain.publish(g3).node_state is NodeState.SUCCEEDED
    _serve(chain, g3)
    item = chain.run_item(g3)
    assert (item["state"]["S"], item["serving_outcome"]["S"]) == ("SUCCEEDED", "complete")
    binding = _binding(chain, g3)
    assert binding is not None and binding_authorizes_serving(binding)
    for pointer, generation in _pages(chain).values():
        assert pointer is not None and generation == PageGeneration(built_at=pointer.written_at)
    assert errors == []


@pytest.mark.covers("ADR-140")
def test_adr140_w1_control_without_the_release_pass_the_domain_stays_wedged(
    make_chain: Callable[..., Chain],
) -> None:
    chain = make_chain()
    run2 = _uncurated_through_release(chain, "run2", date(2026, 9, 2))
    assert chain.publish(run2).node_state is NodeState.SUCCEEDED
    assert chain.fan_out(run2).node_state is NodeState.SUCCEEDED
    g3, _old, _new = _through_publish(chain, "g3", date(2026, 9, 3))
    assert chain.publish(g3).node_state is NodeState.SUCCEEDED
    for _ in range(3):
        chain.arm()
    assert (_head(chain)["state"]["S"], _head(chain)["winner_run_id"]["S"]) == ("ACTIVE", run2)
    assert chain.node(g3, "bake").state is NodeState.PENDING  # never elected


def _winner_only(state: Any, execution: Any, effect: Any) -> bool:
    """Decide as the pre-ADR-140 step 3 did: the winner's own publish effect alone."""
    del execution
    return bool(state.work.eligible(effect))


@pytest.mark.covers("ADR-140")
def test_adr140_w2_an_old_baker_u1_over_a_four_run_cohort_settles_drains_and_elects(
    make_chain: Callable[..., Chain],
    errors: list[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = make_chain()
    members = [
        _through_publish(chain, f"loser-{index}", date(2026, 9, 1 + index))[0] for index in range(3)
    ]
    winner = _uncurated_through_release(chain, "winner", date(2026, 9, 4))
    for run in (*members, winner):
        assert chain.publish(run).node_state is NodeState.SUCCEEDED
    # The old Baker: winner-only eligibility, so the zero-curation winner settles U1.
    with monkeypatch.context() as old_baker:
        old_baker.setattr(post_grant_consumer, "_cohort_eligible", _winner_only)
        assert chain.fan_out(winner).node_state is NodeState.SUCCEEDED
    head = _head(chain)
    assert head["winner_run_id"]["S"] == winner
    assert {entry["S"] for entry in head["elected_run_ids"]["L"]} == {*members, winner}
    stored = chain.tables.items[(f"COHORT#{chain.grant(winner)}", "CONTRIBUTION#bake")]
    assert stored["outcome"]["S"] == "no_eligible"
    stored["settled_at"] = write_text(LIVE_BAKE_SETTLED.isoformat())  # the live instant
    # A non-cohort candidate published behind the ACTIVE cohort (the next frontier coordinate).
    successor, _old, _new = _through_publish(chain, "successor", date(2026, 9, 5))
    assert chain.publish(successor).node_state is NodeState.SUCCEEDED
    marker = chain.tables.items[(f"SERVING_PUBLICATION#{successor}", "MARKER")]
    assert marker["state"]["S"] == "SETTLED_ELIGIBLE"
    signals: list[int] = []

    counts = _release_pass(chain, signals)
    for _ in range(len(members) + 1):
        chain.arm()

    assert (counts.unarmed_settled, counts.completed) == (1, 1)
    assert chain.run_item(winner)["state"]["S"] == "SUCCEEDED"
    for loser in members:
        assert chain.run_item(loser)["state"]["S"] == "SUCCEEDED"
        alias = _binding(chain, loser)
        assert alias is not None and alias.binding_kind is BindingKind.ALIAS
    head = _head(chain)
    assert (head["state"]["S"], head["winner_run_id"]["S"]) == ("ACTIVE", successor)
    assert chain.node(successor, "bake").state is NodeState.READY
    assert signals == []
    assert errors == []


def _cohort_pair(chain: Chain, *, loser_curated: bool) -> tuple[str, str]:
    """Publish a loser (curated or not), then a zero-curation winner, and elect them."""
    day = date(2026, 9, 7)
    loser = (
        _through_publish(chain, "early", day)[0]
        if loser_curated
        else _uncurated_through_release(chain, "early", day)
    )
    winner = _uncurated_through_release(chain, "late", date(2026, 9, 8))
    for run in (loser, winner):
        assert chain.publish(run).node_state is NodeState.SUCCEEDED
    chain.arm()
    assert _head(chain)["winner_run_id"]["S"] == winner
    return loser, winner


@pytest.mark.covers("ADR-140")
def test_adr140_w3_a_curated_loser_makes_its_zero_curation_winner_bake_and_authorize(
    make_chain: Callable[..., Chain], errors: list[BaseException]
) -> None:
    chain = make_chain()
    loser, winner = _cohort_pair(chain, loser_curated=True)

    _serve(chain, winner)
    chain.arm()  # the bound domain drains the loser onto the winner's binding

    binding = _binding(chain, winner)
    assert binding is not None and binding_authorizes_serving(binding)
    for pointer, _generation in _pages(chain).values():
        assert pointer is not None and pointer.grant_id == chain.grant(winner)
    assert chain.run_item(loser)["state"]["S"] == "SUCCEEDED"
    assert errors == []


@pytest.mark.covers("ADR-140")
def test_adr140_w3_control_a_cohort_with_no_curation_is_u1_and_the_release_settles_it(
    make_chain: Callable[..., Chain],
) -> None:
    chain = make_chain()
    loser, winner = _cohort_pair(chain, loser_curated=False)

    assert chain.fan_out(winner).node_state is NodeState.SUCCEEDED
    assert not any(
        node.name.startswith("bake_task") for node in release_store(chain.tables).read_nodes(winner)
    )
    assert _release_pass(chain, []).unarmed_settled == 1
    chain.arm()
    assert chain.run_item(winner)["state"]["S"] == "SUCCEEDED"
    assert chain.run_item(loser)["state"]["S"] == "SUCCEEDED"
    assert _pages(chain) == dict.fromkeys(PAGES, (None, None))


@dataclass
class _InvariantPlanner:
    """A precompute planner whose work raises an unclassified invariant (P2)."""

    def plan(self, inputs: Any) -> Any:
        del inputs
        raise InvariantError(ErrorCode("FICTIONAL", 11), "a fictional precompute invariant")


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("stale", ["old_precompute", "precompute_invariant"])
def test_adr140_w4_a_stale_precompute_settles_withheld_and_drains(
    stale: str,
    make_chain: Callable[..., Chain],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = make_chain()
    g1, _old, _new = _through_publish(chain, "g1", date(2026, 9, 1))
    assert chain.publish(g1).node_state is NodeState.SUCCEEDED
    _serve(chain, g1)
    served = _pages(chain)
    signals: list[int] = []
    chain.lake.runtime.serving_signal = signals.append
    loser, winner = _cohort_pair(chain, loser_curated=True)
    assert chain.fan_out(winner).node_state is NodeState.SUCCEEDED  # the new Baker fans out
    assert {result.node_state for result in chain.tasks(winner)} == {NodeState.SUCCEEDED}
    assert chain.finalize(winner).outcome == "graph_bake_precompute_armed"
    chain.arm()
    chain.dispatch()
    work = GraphPrecomputeWork(chain.roster, _Planner(), _Assembler(), 40)
    with monkeypatch.context() as old:
        if stale == "old_precompute":  # winner-only eligibility: NO_ELIGIBLE, no digest
            old.setattr(post_grant_consumer, "_cohort_eligible", _winner_only)
        else:
            work = GraphPrecomputeWork(chain.roster, _InvariantPlanner(), _Assembler(), 40)
        consume_post_grant(
            ports(chain.tables, chain.clock, chain.metrics),
            envelope(chain.node(winner, "precompute"), CohortMember.PRECOMPUTE),
            work,
        )
    precompute = next(
        item
        for item in release_store(chain.tables).read_contributions(chain.grant(winner))
        if item.member is CohortMember.PRECOMPUTE
    )
    assert precompute.input_effect_roster_digest is None
    expected = CohortOutcome.NO_ELIGIBLE if stale == "old_precompute" else CohortOutcome.UNAVAILABLE
    assert precompute.outcome is expected

    finalized = chain.finalize(winner, 2)
    chain.arm()

    assert (finalized.acknowledge, finalized.node_state) == (True, NodeState.SUCCEEDED)
    binding = _binding(chain, winner)
    assert binding is not None and serving_withheld(binding)
    assert not binding_authorizes_serving(binding)
    assert _pages(chain) == served  # every page keeps G1, its last-good generation
    assert chain.run_item(winner)["state"]["S"] == "SUCCEEDED"
    assert chain.run_item(loser)["state"]["S"] == "SUCCEEDED"
    assert _head(chain)["state"]["S"] == "IDLE"
    assert signals == [1]


# --- D2 hardening: a source refusal is one row's designed absence (F1, F2) --------------------

CONTRACT_ROSTER = BakeRoster.from_contract()
_REFUSAL_PHASES: Final = ("prefetch", "pin-bake", "render")


def _refuse(  # noqa: PLR0913, PLR0917 -- the chain, the row, its phase and the error
    chain: Chain, task_key: str, phase: str, error: Exception
) -> None:
    """Make one roster row's source refuse in one phase: pin prefetch, pin bake, or task render."""
    sources = chain.lake.sources
    if phase == "prefetch":
        sources.prefetch_raises[task_key] = error
    elif phase == "pin-bake":
        sources.raises[task_key] = error
    else:
        sources.render_raises[task_key] = error


def _pointer_items(chain: Chain) -> dict[str, Any]:
    return {
        task_key: copy.deepcopy(chain.tables.items.get((f"PTR|{task_key}", CURRENT_RECORD)))
        for task_key in chain.roster.keys
    }


def _effect_shape(chain: Chain, run: str, task_key: str) -> tuple[Any, ...]:
    effect, _extra = read_task_effect(chain.node(run, task_node_name(task_key)))
    return effect.effect_kind, effect.reason_code, effect.artifact_revision


def _written(task_key: str) -> tuple[Any, ...]:
    """Return the no-refusal control's effect for one row: written over its CURRENT one."""
    revision = PresetArtifactRevision.from_publication_identities((current_identity(task_key),))
    return BakeEffectKind.WRITTEN, None, revision.value


def _contract_run(chain: Chain, signals: list[int]) -> tuple[str, dict[str, Any]]:
    """Publish a contract-roster winner with a last-good pointer on every row."""
    chain.lake.runtime.serving_signal = signals.append
    for task_key in chain.roster.keys:
        chain.tables.seed_pointer(task_key)
    before = _pointer_items(chain)
    run, _old, _new = _through_publish(chain, "d2-refusal", date(2026, 9, 1))
    assert chain.publish(run).node_state is NodeState.SUCCEEDED
    return run, before


def _assert_finalized_serving(chain: Chain, run: str, signals: list[int]) -> Any:
    item = chain.run_item(run)
    assert item["state"]["S"] == "SUCCEEDED"
    binding = _binding(chain, run)
    assert binding is not None and binding_authorizes_serving(binding)
    assert chain.node(run, "bake_finalize").state is NodeState.SUCCEEDED
    assert 1 not in signals
    return binding


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("phase", _REFUSAL_PHASES)
@pytest.mark.parametrize("refused", CONTRACT_ROSTER.keys)
def test_h1_a_source_refusal_is_its_rows_designed_absence_and_the_cohort_finalizes(
    refused: str, phase: str, make_chain: Callable[..., Chain]
) -> None:
    chain = make_chain(roster=CONTRACT_ROSTER)
    signals: list[int] = []
    run, before = _contract_run(chain, signals)
    _refuse(chain, refused, phase, DomainError(ErrorCode("FICTIONAL", 21), "suppressed result"))

    _serve(chain, run)

    page = refused.split("#", maxsplit=1)[0]
    assert _effect_shape(chain, run, refused) == (
        BakeEffectKind.UNAVAILABLE,
        unavailable_reason(page),
        None,
    )
    after = _pointer_items(chain)
    assert after[refused] == before[refused]  # the refused page keeps its last-good CURRENT
    for other in chain.roster.keys:
        if other != refused:
            assert _effect_shape(chain, run, other) == _written(other), other
            assert after[other] != before[other], other
    binding = _assert_finalized_serving(chain, run, signals)
    assert binding.unavailable == ((refused, unavailable_reason(page)),)


@pytest.mark.covers("ADR-140")
def test_h1_a_validation_refusal_is_also_designed_absence(
    make_chain: Callable[..., Chain],
) -> None:
    chain = make_chain(roster=CONTRACT_ROSTER)
    signals: list[int] = []
    run, before = _contract_run(chain, signals)
    refused = CONTRACT_ROSTER.keys[-1]
    _refuse(chain, refused, "render", ValidationError(ErrorCode("FICTIONAL", 22), "bad grain"))

    _serve(chain, run)

    assert _effect_shape(chain, run, refused)[0] is BakeEffectKind.UNAVAILABLE
    assert _pointer_items(chain)[refused] == before[refused]
    _assert_finalized_serving(chain, run, signals)


@pytest.mark.covers("ADR-140")
def test_h1_control_with_no_refusal_every_contract_row_is_written(
    make_chain: Callable[..., Chain],
) -> None:
    chain = make_chain(roster=CONTRACT_ROSTER)
    signals: list[int] = []
    run, _before = _contract_run(chain, signals)

    _serve(chain, run)

    for task_key in chain.roster.keys:
        assert _effect_shape(chain, run, task_key) == _written(task_key)
    assert _assert_finalized_serving(chain, run, signals).unavailable == ()


# --- B4: a genuine task failure arms the finalizer, which settles the cohort withheld ---------

_GENUINE_FAILURES: Final = ("invariant", "d18", "retry_exhausted")


def _fail_task(  # noqa: PLR0913, PLR0917 -- the chain, its run, the row and the failure
    chain: Chain, run: str, task_key: str, failure: str
) -> ConsumeResult:
    """Fail one dispatched task in a genuine, non-designed way; return its last result."""
    if failure == "invariant":
        chain.lake.sources.render_raises[task_key] = InvariantError(
            ErrorCode("FICTIONAL", 23), "a fictional render invariant"
        )
    elif failure == "d18":
        chain.lake.sources.raises[task_key] = PresetArtifactBodyCeilingError(
            "retail_body_ceiling", 256_001
        )
    else:
        chain.lake.queries.fail[task_key] = DependencyError(ErrorCode("FICTIONAL", 24), "throttled")
        retried = chain.lake.task(run, task_key)
        assert (retried.acknowledge, retried.node_state) == (False, NodeState.CLAIMED)
        expire_claim(chain.tables, chain.node(run, task_node_name(task_key)))
        return chain.lake.task(run, task_key, receive_count=BAKE_FINAL_RECEIVE)
    return chain.lake.task(run, task_key)


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("failure", _GENUINE_FAILURES)
def test_h3_a_genuine_task_failure_ends_the_cohort_terminal_and_withheld(
    failure: str, make_chain: Callable[..., Chain]
) -> None:
    chain = make_chain()
    signals: list[int] = []
    chain.lake.runtime.serving_signal = signals.append
    for task_key in ROSTER.keys:
        chain.tables.seed_pointer(task_key)
    before = _pointer_items(chain)
    run, _old, _new = _through_publish(chain, "b4", date(2026, 9, 2))
    assert chain.publish(run).node_state is NodeState.SUCCEEDED
    assert chain.fan_out(run).node_state is NodeState.SUCCEEDED
    chain.dispatch()
    failing, *others = ROSTER.keys
    for other in others:
        assert chain.lake.task(run, other).node_state is NodeState.SUCCEEDED
    # Control: a non-terminal task leaves the finalizer PENDING.
    chain.arm()
    assert chain.node(run, "bake_finalize").state is NodeState.PENDING

    failed = _fail_task(chain, run, failing, failure)

    assert failed.node_state is NodeState.FAILED
    task = chain.node(run, task_node_name(failing))
    recovery = (due_shard(_unit("b4")), f"BAKE_RECOVERY#{task.node_id}")
    assert recovery in chain.tables.items
    chain.arm()
    assert chain.node(run, "bake_finalize").state is NodeState.READY  # the real arm pass
    abandoned = chain.finalize(run)
    assert (abandoned.acknowledge, abandoned.outcome, abandoned.node_state) == (
        True,
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    bake = next(
        item
        for item in release_store(chain.tables).read_contributions(chain.grant(run))
        if item.member is CohortMember.BAKE
    )
    assert (bake.outcome, bake.artifact_revision) == (CohortOutcome.UNAVAILABLE, None)
    assert {reason for _key, reason in bake.unavailable} == {"post_grant_invariant"}
    assert chain.node(run, "precompute").state is NodeState.WAITING_INPUT

    counts = _release_pass(chain, signals)

    assert (counts.unarmed_settled, counts.completed, counts.failed) == (1, 1, 0)
    precompute = next(
        item
        for item in release_store(chain.tables).read_contributions(chain.grant(run))
        if item.member is CohortMember.PRECOMPUTE
    )
    assert precompute.outcome is CohortOutcome.NO_ELIGIBLE
    binding = _binding(chain, run)
    assert binding is not None and serving_withheld(binding)
    assert not binding_authorizes_serving(binding)
    assert chain.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert not any(sk == f"COMPLETE#{run}" for _pk, sk in chain.tables.items)
    assert recovery not in chain.tables.items
    assert _head(chain)["state"]["S"] != "ACTIVE"
    assert _pointer_items(chain) == before
    assert signals == [1]


# --- H5(c): an armed precompute races the abandoning finalizer to the one binding (§2.4) -----

_RACES: Final = ("c1_contributes_between_write_and_read", "c2_precompute_bind_loses")


def _armed_unbound(chain: Chain, name: str, signals: list[int]) -> tuple[str, dict[str, Any]]:
    """Serve a winner up to an armed, dispatched precompute that has not contributed."""
    chain.lake.runtime.serving_signal = signals.append
    for task_key in ROSTER.keys:
        chain.tables.seed_pointer(task_key)
    before = _pointer_items(chain)
    run, _old, _new = _through_publish(chain, name, date(2026, 9, 3))
    assert chain.publish(run).node_state is NodeState.SUCCEEDED
    assert chain.fan_out(run).node_state is NodeState.SUCCEEDED
    assert {result.node_state for result in chain.tasks(run)} == {NodeState.SUCCEEDED}
    assert chain.finalize(run).outcome == "graph_bake_precompute_armed"
    chain.arm()
    chain.dispatch()
    assert chain.node(run, "precompute").state is NodeState.DISPATCHED
    return run, before


def _precompute_over(chain: Chain, run: str, precompute_store: Any) -> ConsumeResult:
    """Consume the dispatched precompute through the real consumer over one store."""
    return consume_post_grant(
        replace(ports(chain.tables, chain.clock, chain.metrics), store=precompute_store),
        envelope(chain.node(run, "precompute"), CohortMember.PRECOMPUTE),
        GraphPrecomputeWork(chain.roster, _Planner(), _Assembler(), 40),
    )


def _binding_writes(chain: Chain, run: str, since: int) -> int:
    """Count the committed `SERVING#<grant>/BINDING` puts from transaction `since` on."""
    binding_key = (f"SERVING#{chain.grant(run)}", "BINDING")
    key_of = chain.tables._key  # pyright: ignore[reportPrivateUsage]
    return sum(
        1
        for transaction in chain.tables.transactions[since:]
        for member in transaction
        if "Put" in member and key_of(member["Put"]["Item"]) == binding_key
    )


@pytest.mark.covers("ADR-140")
@pytest.mark.parametrize("order", _RACES)
def test_h5c_an_armed_precompute_and_an_abandoning_finalizer_bind_once_and_complete_withheld(
    order: str, make_chain: Callable[..., Chain], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Armed, not contributed, the finalizer's final receive raises (F5) and abandons.

    c1: precompute contributes and binds between the finalizer's withheld write and its binding
    read, so the finalizer settles under precompute's binding. c2: both derive before either
    binds and precompute's bind loses (a store stub binds the finalizer's stale bytes first), so
    precompute adopts the stored binding instead of retrying to a dead letter.
    """
    chain = make_chain()
    signals: list[int] = []
    run, before = _armed_unbound(chain, order[:2], signals)
    precompute_store = release_store(chain.tables)
    if order.startswith("c2"):
        real_bind = precompute_store.bind_serving

        def finalizer_first(binding: Any) -> None:
            real_bind(replace(binding, stale=True))  # the finalizer's stale bind lands first
            real_bind(binding)

        object.__setattr__(precompute_store, "bind_serving", finalizer_first)
    precomputed: list[ConsumeResult] = []
    real_record = graph_bake.record_contribution_once

    def then_precompute(store: Any, contribution: Any) -> Any:
        recorded = real_record(store, contribution)
        precomputed.append(_precompute_over(chain, run, precompute_store))
        return recorded

    def failing(*_args: object) -> None:
        raise InvariantError(ErrorCode("FICTIONAL", 25), "a fictional finalizer invariant")

    monkeypatch.setattr(graph_bake, "_precompute_input", failing)
    monkeypatch.setattr(graph_bake, "record_contribution_once", then_precompute)
    since = len(chain.tables.transactions)

    abandoned = chain.finalize(run, BAKE_FINAL_RECEIVE)

    assert (abandoned.acknowledge, abandoned.outcome, abandoned.node_state) == (
        True,
        "graph_bake_finalize_abandoned",
        NodeState.FAILED,
    )
    (settled,) = precomputed
    assert (settled.acknowledge, settled.node_state) == (True, NodeState.SUCCEEDED)
    assert chain.node(run, "precompute").state is NodeState.SUCCEEDED
    assert _binding_writes(chain, run, since) == 1  # one binder won; the other adopted
    binding = _binding(chain, run)
    assert binding is not None and serving_withheld(binding)
    assert not binding_authorizes_serving(binding)
    assert binding.stale is order.startswith("c2")
    assert chain.tables.pointer_writes(since) == []

    counts = _release_pass(chain, signals)

    assert (counts.unarmed_settled, counts.completed, counts.failed) == (0, 1, 0)
    assert chain.run_item(run)["state"]["S"] == "SUCCEEDED"
    assert _binding(chain, run) == binding
    assert _head(chain)["state"]["S"] != "ACTIVE"
    assert _pointer_items(chain) == before
    assert 1 in signals and 0 not in signals
