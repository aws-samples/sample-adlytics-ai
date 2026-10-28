# SPDX-License-Identifier: MIT-0
"""Fixtures for the V3 GRAPH bake tests (plan 4.6 under Addendum A3).

The graph side is the real release, promoter, dispatcher and V3 store over `BakeTables`, one
transactional fake holding the graph table and the preset pointer table. The source side is
`LakeSources`: a registered baker whose every source read goes through the runtime's real
`BakeSourceReads` (over `LakeCurrent`, an in-memory CURRENT with immutable records) and whose
every query goes through the runtime's real `BakeQueryCapture` (over `CountingQueries`). So the
pin, the replay, the bind check and the zero-Athena bind pass are the production code. Every
identity is fictional.
"""

from __future__ import annotations

# The publication fixture spec is a shared test seam.
# pyright: reportPrivateUsage=false
import copy
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from execution_graph_dispatch_support import NOW, SpySender, now_text
from execution_graph_release_support import release_store
from execution_graph_support import FakeDynamo
from graph_consumer_support import (
    arm_and_dispatch,
    precompute_contributed,
    publish_settled,
    sqs_record,
)
from preset_artifact_support import staged_advance, staged_result
from publication_support import _PublicationSpec, successful_publication

from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.post_grant_consumer import ConsumeResult, post_grant_envelope
from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.graph_bake import (
    BakeRuntime,
    GraphBakeRuntime,
    task_node_name,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import note_isolated_refusal
from adapters.jobs.preset_artifact_publisher.page_bake import ProcessBakeLedger
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeDelivery,
    BakeIntent,
    parse_bake_v3_body,
)
from adapters.outbound.bake_sources import BakeQueryCapture, BakeSourceReads
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.preset_artifacts import (
    CURRENT_RECORD,
    pointer_from_candidate_item,
    pointer_transaction_items,
)
from adapters.outbound.publication_selection import publication_digest
from domain.operations.bake_refusals import IsolatedRefusal
from domain.operations.execution_graph_release import (
    BakeRoster,
    BakeRosterRow,
    CohortMember,
    unavailable_reason,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactCustodyHeader,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactStageResult,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.semantics.publication import PublicationScope, SuccessfulPublication
from ports.outbound.execution_graph_dispatch import NodeRecord
from ports.outbound.publication_selection import PublicationSelectionExactRequest
from ports.outbound.semantic_state import ScopeHorizon

POINTER_TABLE = "fictional-pointers"


@dataclass(frozen=True, slots=True)
class SeedHistoryRequest:
    """An older writer's superseded history for a task's candidate, with pinned perturbations."""

    candidate: dict[str, Any]
    """A task's stored CURRENT-envelope pointer candidate item."""
    body_digest: str | None = None
    """A 64-hex literal replacing the candidate's body digest (other bytes), when given."""
    drop_custody: bool = False
    """Remove `custody_digest` (an unattested record)."""
    grant: str = "a" * 64
    """The older writer's grant."""


class PointerKeys(FakeDynamo):
    """A transactional fake that also holds the preset pointer table's keys.

    Mixed before another fake (`class ChainTables(PointerKeys, Tables)`), it keys pointer-table
    items itself and delegates every other key to the next fake in the MRO.
    """

    def _key(self, key: dict[str, Any]) -> tuple[str, str]:
        if "page_variant_key" in key:
            return (f"PTR|{key['page_variant_key']['S']}", str(key["record_key"]["S"]))
        return super()._key(key)

    def page_pointer(self, page: str, variant: str) -> PresetArtifactPointer | None:
        """Return one page/variant's CURRENT pointer, if any."""
        item = self.items.get((f"PTR|{page}#{variant}", CURRENT_RECORD))
        return None if item is None else pointer_from_candidate_item(cast("Any", item))

    def pointer_writes(self, since: int = 0) -> list[dict[str, Any]]:
        """Every committed member, from transaction `since` on, keyed in the pointer table."""
        return [
            member
            for transaction in self.transactions[since:]
            for member in transaction
            if is_pointer_member(member)
        ]

    def seed_pointer(self, key: str, identity: str = "publication-predecessor") -> None:
        """Commit one key's CURRENT pointer over `identity` before the bake runs.

        A predecessor identity makes the key's next write supersede it (three pointer members);
        the key's own CURRENT publication identity makes its bake `unchanged`.
        """
        page, variant = key.split("#")
        request = _publication(BakeIntent(page, variant), identity, None)
        items = pointer_transaction_items(POINTER_TABLE, staged_advance(request))
        self.transact_write_items(TransactItems=list(items))

    def seed_history(self, seed: SeedHistoryRequest) -> str:
        """Store an earlier write's history record of the candidate's revision; return it.

        BSORT-DEV-1's shape: a build re-derived a revision the pointer history already holds,
        written by another grant at another token and retained with an expiry. Every value is
        fictional.
        """
        revision = str(seed.candidate["revision"]["S"])
        record = copy.deepcopy(seed.candidate)
        record["record_key"] = {"S": f"REVISION#{revision}"}
        record["forward_token"] = {"N": "1"}
        record.pop("supersedes", None)
        record["grant_id"] = {"S": seed.grant}
        record["serving_generation"] = {"S": "b" * 64}
        record["written_at"] = {"S": "2026-01-01T00:00:00Z"}
        record["expires_at"] = {"N": "1800000000"}
        if seed.body_digest is not None:
            record["body_digest"] = {"S": seed.body_digest}
        if seed.drop_custody:
            record.pop("custody_digest", None)
        self.items[(f"PTR|{seed.candidate['page_variant_key']['S']}", f"REVISION#{revision}")] = (
            record
        )
        return revision


class BakeTables(PointerKeys):
    """The graph table and the preset pointer table in one transactional fake."""


def is_pointer_member(member: dict[str, Any]) -> bool:
    """Return whether one transaction member's own key is a pointer-table key."""
    body = cast("dict[str, dict[str, Any]]", next(iter(member.values())))
    key = body.get("Key") or body.get("Item") or {}
    return "page_variant_key" in key


def final_transaction(tables: FakeDynamo, since: int) -> list[dict[str, Any]]:
    """Return the one committed transaction, from `since` on, that wrote a page pointer."""
    return next(
        transaction
        for transaction in tables.transactions[since:]
        if any(is_pointer_member(member) for member in transaction)
    )


def current_identity(key: str) -> str:
    """Return the publication identity the lake holds CURRENT for one roster key."""
    return f"publication-{key.replace('#', '-')}-1"


def roster(count: int, *, page: str = "campaigns") -> BakeRoster:
    """Return an injected N-row roster over one page (A1.5 fixtures)."""
    return BakeRoster(
        tuple(BakeRosterRow(page, f"{index + 1}d", 240, 16.6, 123, 26) for index in range(count))
    )


def _scope(name: str) -> PublicationScope:
    return successful_publication(
        name, date(2026, 9, 1), _PublicationSpec("x", NOW)
    ).partition.scope


@dataclass
class LakeCurrent:
    """An in-memory CURRENT with immutable records; it also serves `select_exact`."""

    by_scope: dict[str, tuple[SuccessfulPublication, ...]] = field(
        default_factory=dict[str, tuple[SuccessfulPublication, ...]]
    )
    immutable: dict[str, SuccessfulPublication] = field(
        default_factory=dict[str, SuccessfulPublication]
    )
    current_calls: int = 0
    exact_calls: int = 0
    horizons: list[ScopeHorizon | None] = field(default_factory=list[ScopeHorizon | None])
    """The horizon each whole-scope read declared (A5.1 I9)."""

    def load(self, name: str, publication_id: str) -> PublicationScope:
        """Make one publication CURRENT in a dataset scope (its immutable record too)."""
        item = successful_publication(name, date(2026, 9, 1), _PublicationSpec(publication_id, NOW))
        self.by_scope[item.partition.scope.identity] = (item,)
        self.immutable[publication_id] = item
        return item.partition.scope

    def current_for_scope(
        self, scope: PublicationScope, horizon: ScopeHorizon | None = None
    ) -> tuple[SuccessfulPublication, ...]:
        self.current_calls += 1
        self.horizons.append(horizon)
        return self.by_scope.get(scope.identity, ())

    def select_exact(
        self, request: PublicationSelectionExactRequest
    ) -> tuple[SuccessfulPublication, ...]:
        self.exact_calls += 1
        found = tuple(self.immutable[pin.publication_id] for pin in request.pins)
        for pin, item in zip(request.pins, found, strict=True):
            assert publication_digest(item) == pin.digest
        return found


@dataclass(frozen=True, slots=True)
class FakeRequest:
    """A complete-bound request stand-in: equal values share one execution."""

    key: str
    index: int
    publication: str


@dataclass
class CountingQueries:
    """The admission-wrapped port: counts real executions per task key."""

    calls: list[str] = field(default_factory=list[str])
    lock: threading.Lock = field(default_factory=threading.Lock)
    fail: dict[str, Exception] = field(default_factory=dict[str, Exception])

    def run(self, request: Any) -> Any:
        with self.lock:
            self.calls.append(cast("FakeRequest", request).key)
        error = self.fail.get(cast("FakeRequest", request).key)
        if error is not None:
            raise error
        return None


@dataclass
class LakeSources:
    """A registered baker reading the lake through the runtime's reader and port.

    Per key: `queries[key]` complete requests (duplicates share), its scope's CURRENT publication
    decides the revision, `unavailable` returns typed absence with the page's generated reason,
    `raises` raises, `oversize` returns a body over the D18 ceiling.
    """

    current: LakeCurrent
    tables: PointerKeys
    pages: tuple[str, ...] = ("campaigns",)
    queries: dict[str, int] = field(default_factory=dict[str, int])
    unavailable: set[str] = field(default_factory=set[str])
    raises: dict[str, Exception] = field(default_factory=dict[str, Exception])
    extra_on_task: set[str] = field(default_factory=set[str])
    """Keys that bind one more request in a task than at fan-out (a count mismatch)."""
    reads: BakeSourceReads | None = None
    port: BakeQueryCapture | None = None
    bakes: list[str] = field(default_factory=list[str])

    prefetched: list[str] = field(default_factory=list[str])
    prefetch_query: set[str] = field(default_factory=set[str])
    """Keys whose prefetch issues a query (the A5.1 prefetch-guard control)."""
    prefetch_raises: dict[str, Exception] = field(default_factory=dict[str, Exception])
    swallow: set[str] = field(default_factory=set[str])
    """Keys whose render catches every query error and renders anyway (sticky-bound control)."""
    gather: set[str] = field(default_factory=set[str])
    """Keys whose render runs every query, then raises the first query error (precedence)."""
    render_raises: dict[str, Exception] = field(default_factory=dict[str, Exception])
    """Keys whose task render (replay, not capturing) raises after its queries ran."""
    notes: dict[str, tuple[IsolatedRefusal, ...]] = field(
        default_factory=dict[str, tuple[IsolatedRefusal, ...]]
    )
    """Item or section refusals a key's source notes on every render (ADR-142)."""

    def registered_pages(self) -> tuple[str, ...]:
        return self.pages

    def prefetch(self, intent: BakeIntent) -> None:
        name = f"{intent.page}#{intent.variant}"
        self.prefetched.append(name)
        if name in self.prefetch_raises:
            raise self.prefetch_raises[name]
        if name in self.prefetch_query:
            assert self.port is not None
            self.port.run(cast("Any", FakeRequest(name, -1, "prefetch")))

    def bake(self, intent: BakeIntent) -> BakeResult:
        name = f"{intent.page}#{intent.variant}"
        self.bakes.append(name)
        assert self.reads is not None and self.port is not None
        scope = _scope(name.replace("#", "-"))
        # A whole-scope read declares the row's horizon, as every bake read family does (I9).
        horizon = self.reads.window_horizon(1)
        found = (
            self.reads.current_for_scope(scope)
            if horizon is None
            else self.reads.current_for_scope(scope, horizon)
        )
        (publication,) = found or (None,)
        if name in self.raises:
            raise self.raises[name]
        identity = "none" if publication is None else publication.publication_id.value
        count = self.queries.get(name, 2)
        if name in self.extra_on_task and self.reads.mode.value == "replay":
            count += 1
        with self.port.phase("query"):
            errors: list[Exception] = []
            for index in range(count):
                try:
                    self.port.run(cast("Any", FakeRequest(name, index, identity)))
                except Exception as error:
                    if name not in self.swallow | self.gather:
                        raise
                    errors.append(error)
            if errors and name in self.gather:
                raise errors[0]
        for note in self.notes.get(name, ()):
            note_isolated_refusal(
                note.reason, note.scope, note.count, section=cast("str", note.section)
            )
        if (
            name in self.render_raises
            and self.reads.mode.value == "replay"
            and not self.port.capturing
        ):
            raise self.render_raises[name]
        with self.port.phase("render"):
            if name in self.unavailable or publication is None:
                return BakeUnavailable(intent, unavailable_reason(intent.page))
            current = self.tables.page_pointer(intent.page, intent.variant)
            revision = PresetArtifactRevision.from_publication_identities((identity,))
            if current is not None and current.revision == revision:
                return BakeUnchanged(intent, revision)
            return BakePublication(_publication(intent, identity, current))


def _publication(
    intent: BakeIntent, identity: str, current: PresetArtifactPointer | None
) -> PresetArtifactPublicationRequest:
    page, variant = PresetArtifactPage(intent.page), PresetArtifactVariant(intent.variant)
    revision = PresetArtifactRevision.from_publication_identities((identity,))
    body = PresetArtifactBody(
        page, variant, revision, canonical_preset_artifact_json({"identity": identity})
    )
    return PresetArtifactPublicationRequest(
        body,
        PresetArtifactPointerTemplate(
            page,
            variant,
            revision,
            body.digest,
            1 if current is None else current.forward_token + 1,
            None if current is None else current.revision,
            None if current is None else NOW + timedelta(days=30),
        ),
        PresetArtifactCustodyHeader(canonical_preset_artifact_json({"body_digest": body.digest})),
    )


@dataclass
class Stage:
    """The stage-only persistence: attests every object, never touches a pointer."""

    staged: list[str] = field(default_factory=list[str])

    calls: list[tuple[int, float]] = field(default_factory=list[tuple[int, float]])
    """Each stage call's (remaining ms, tail seconds): the write deadline it was given."""

    def stage(
        self,
        request: PresetArtifactPublicationRequest,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> PresetArtifactStageResult:
        self.calls.append((remaining_ms(), tail_seconds))
        body = request.body
        self.staged.append(f"{body.page.value}#{body.variant.value}")
        return staged_result(request)


@dataclass(frozen=True)
class PointerReader:
    """Adapt the mixed fake's page-pointer helper to the runtime's read-only seam."""

    tables: PointerKeys

    def current(self, page: Any, variant: Any) -> PresetArtifactPointer | None:
        return self.tables.page_pointer(page.value, variant.value)


@dataclass
class Bake:
    """One V3 bake world: the tables, the lake, the runtime and its consumers."""

    roster: BakeRoster = field(default_factory=lambda: roster(3))
    tables: PointerKeys = field(default_factory=BakeTables)
    current: LakeCurrent = field(default_factory=LakeCurrent)
    queries: CountingQueries = field(default_factory=CountingQueries)
    stage: Stage = field(default_factory=Stage)
    sender: SpySender = field(default_factory=SpySender)
    invariants: list[str] = field(default_factory=list[str])
    """Every POST_GRANT invariant signal a task counted."""
    visibility_calls: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    """Every bake-queue visibility override: (queue URL, receipt handle, seconds)."""
    remaining_ms: Callable[[], int] = field(default=lambda: 600_000)
    """The fictional invocation's remaining time (the Lambda context's callable)."""
    slept: list[float] = field(default_factory=list[float])
    """Every pin-pass backoff sleep (ADR-142), recorded instead of slept."""
    sources: LakeSources = field(init=False)
    runtime: BakeRuntime = field(init=False)
    graph: GraphBakeRuntime = field(init=False)

    def __post_init__(self) -> None:
        self.sources = LakeSources(self.current, self.tables, pages=self.roster.pages)
        reads = BakeSourceReads(self.current, self.current)
        port = BakeQueryCapture(self.queries)
        self.sources.reads, self.sources.port = reads, port
        self.runtime = BakeRuntime(
            roster=self.roster,
            baker=self.sources,
            reads=reads,
            queries=port,
            stage=self.stage,
            pointers=cast("Any", PointerReader(self.tables)),
            pointer_table=POINTER_TABLE,
            query_cap=10,
            now=lambda: NOW,
            process=ProcessBakeLedger(peak=lambda: 0.0),
            sleep=self.slept.append,
            jitter=random.Random(0),  # noqa: S311 -- a deterministic test jitter
        )
        self.graph = GraphBakeRuntime(
            release_store(self.tables),
            self.runtime,
            lambda: NOW,
            invariant_metric=self.invariants.append,
            visibility=lambda url, handle, seconds: self.visibility_calls.append(
                (url, handle, seconds)
            ),
        )
        for row in self.roster.rows:
            self.current.load(row.key.replace("#", "-"), current_identity(row.key))

    def seed_predecessors(self) -> None:
        """Give every roster key a predecessor pointer: each write then has three members."""
        for key in self.roster.keys:
            self.tables.seed_pointer(key)

    # --- the graph steps ------------------------------------------------------------------

    def granted(self) -> str:
        """Return a granted winner whose bake fan-out control is dispatched first."""
        run = publish_settled(self.tables)
        arm_and_dispatch(self.tables, run, "bake")
        return run

    def fan_out(
        self, run: str, receive_count: int = 1, remaining_ms: Callable[[], int] = lambda: 800_000
    ) -> ConsumeResult:
        node = self.node(run, "bake")
        envelope = post_grant_envelope(sqs_record(node, receive_count), CohortMember.BAKE)
        assert envelope is not None
        return self.graph.consume(envelope, remaining_ms)

    def dispatch(self, run: str) -> None:
        record = release_store(self.tables).read_run(run)
        assert record is not None
        dispatch_shard(
            DispatchContext(release_store(self.tables), self.sender, now_text),
            due_shard(record.conflict_unit),
        )

    def arm(self) -> None:
        arm_pass(
            PromoterContext(release_store(self.tables), ingestion_node_plan, lambda: NOW),
            due_shards(),
            lambda: 60_000,
            "test-request",
        )

    def node(self, run: str, name: str) -> NodeRecord:
        found = release_store(self.tables).read_node(run, name)
        assert found is not None, name
        return found

    def deliver(self, run: str, name: str, receive_count: int = 1) -> ConsumeResult:
        node = self.node(run, name)
        record = sqs_record(node, receive_count)
        body = parse_bake_v3_body(record["body"])
        assert body is not None
        return self.graph.consume_v3(
            BakeDelivery(body, record["body"], record["messageId"], receive_count),
            self.remaining_ms,
        )

    def task(self, run: str, key: str, receive_count: int = 1) -> ConsumeResult:
        return self.deliver(run, task_node_name(key), receive_count)

    def all_tasks(self, run: str) -> list[ConsumeResult]:
        self.dispatch(run)
        return [self.task(run, key) for key in self.roster.keys]

    def finalize(self, run: str, receive_count: int = 1) -> ConsumeResult:
        self.arm()
        self.dispatch(run)
        first = self.deliver(run, "bake_finalize", receive_count)
        if first.outcome != "graph_bake_precompute_armed":
            return first
        stored = release_store(self.tables).read_precompute_input(run)
        assert stored is not None
        precompute_contributed(self.tables, run, stored.digest, self.roster.keys)
        return self.deliver(run, "bake_finalize", receive_count + 1)

    def run_item(self, run: str) -> dict[str, Any]:
        return self.tables.items[(f"RUN#{run}", "RUN")]


__all__ = [
    "POINTER_TABLE",
    "Bake",
    "BakeTables",
    "CountingQueries",
    "LakeCurrent",
    "LakeSources",
    "PointerKeys",
    "SeedHistoryRequest",
    "Stage",
    "current_identity",
    "final_transaction",
    "is_pointer_member",
    "roster",
]
