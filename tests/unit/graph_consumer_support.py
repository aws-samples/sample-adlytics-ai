# SPDX-License-Identifier: MIT-0
"""Fixtures for the S6 slice 5i consumer tests: a granted run whose `publish` has settled.

The graph side runs through the real release, promoter (arm pass) and dispatcher code over
`FakeDynamo`, so the bake and precompute nodes a test consumes were armed in serving order and
dispatched with their real envelopes. The preset-artifact side is in-memory fakes: a scripted
registered baker, a pointer table that the persistence fake advances, and per-intent outcomes.
Every identity is fictional.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from aws_lambda_powertools import Logger
from execution_graph_dispatch_support import NOW, SpySender, now_text, write_text
from execution_graph_release_support import granted_run, release_store, settle_publish
from execution_graph_support import FakeDynamo
from preset_artifact_support import staged_result

from adapters.inbound.ingestion_handlers.graph_publish import publish_effect_record
from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.post_grant_consumer import (
    ConsumerPorts,
    PostGrantEnvelope,
    post_grant_envelope,
)
from adapters.jobs.execution_graph.promoter import PromoterContext, arm_pass
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeResult,
    BakeUnavailable,
    BakeUnchanged,
)
from adapters.jobs.preset_artifact_publisher.transport import BakeIntent, DeclaredBakeIntents
from adapters.outbound.dynamodb.semantic_state import PublishCounts
from domain.operations.execution_graph_release import (
    CohortContribution,
    CohortMember,
    CohortOutcome,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerTemplate,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.errors import AdlyticsError
from ports.outbound.execution_graph_dispatch import NodeRecord

LOGGER = Logger(service="adlytics-graph-consumer-test")
CAMPAIGNS_7D = BakeIntent("campaigns", "7d")
CAMPAIGNS_30D = BakeIntent("campaigns", "30d")
RETAIL_7D = BakeIntent("retail-health", "7d")
BRAND_7D = BakeIntent("brand-health", "7d")
"""A declared page with no registered baker: never planned (AGG4-13)."""
DECLARED = DeclaredBakeIntents((CAMPAIGNS_7D, CAMPAIGNS_30D, BRAND_7D, RETAIL_7D))
PLANNED = (CAMPAIGNS_7D, CAMPAIGNS_30D, RETAIL_7D)


def key(intent: BakeIntent) -> str:
    """Return an intent's `page#variant` key."""
    return f"{intent.page}#{intent.variant}"


# --- the graph side --------------------------------------------------------------------------


def publish_settled(
    fake: FakeDynamo, *, advanced: int = 2, advanced_curation: int = 1, **seed: Any
) -> str:
    """Grant and apply a run, settle `publish` with its effect record, write `ARM#` (serving)."""
    run = granted_run(fake, **seed)
    settle_publish(fake, run)
    record = release_store(fake).read_run(run)
    assert record is not None and record.grant_id is not None
    counts = PublishCounts(advanced, advanced_curation, 0, 0)
    effect, digest = publish_effect_record((record.grant_id, 1), counts, advanced)
    item = fake.items[(f"RUN#{run}", "NODE#publish")]
    item["effect_record"] = write_text(effect)
    item["effect_digest"] = write_text(digest)
    return run


def arm_and_dispatch(fake: FakeDynamo, run: str, name: str) -> NodeRecord:
    """Run the arm pass and the dispatcher until `name` is DISPATCHED; return the node."""
    arm_pass(
        PromoterContext(release_store(fake), ingestion_node_plan, lambda: NOW),
        due_shards(),
        lambda: 60_000,
        "test-request",
    )
    sender = SpySender()
    for shard in due_shards():
        dispatch_shard(DispatchContext(release_store(fake), sender, now_text), shard)
    node = release_store(fake).read_node(run, name)
    assert node is not None and node.state.value == "DISPATCHED", node
    return node


def precompute_contributed(  # noqa: PLR0913, PLR0917 -- readiness fixture inputs
    fake: FakeDynamo,
    run: str,
    input_digest: str,
    roster: tuple[str, ...],
) -> None:
    """Record one exact readiness-bound precompute contribution for bake-only fixtures."""
    store = release_store(fake)
    record = store.read_run(run)
    assert record is not None and record.grant_id is not None
    store.record_contribution(
        CohortContribution(
            record.grant_id,
            CohortMember.PRECOMPUTE,
            CohortOutcome.NO_ELIGIBLE,
            None,
            NOW,
            record.root_flow_id,
            input_effect_roster_digest=input_digest,
            request_roster=tuple((key, "no_request") for key in roster),
        )
    )


def bake_node(fake: FakeDynamo, **seed: Any) -> tuple[str, NodeRecord]:
    """Return a granted run and its first dispatched POST_GRANT bake node."""
    run = publish_settled(fake, **seed)
    return run, arm_and_dispatch(fake, run, "bake")


def sqs_record(node: NodeRecord, receive_count: int = 1) -> dict[str, Any]:
    """Return the SQS record the dispatcher's send delivers for one node."""
    sent = int((NOW - timedelta(minutes=1)).timestamp() * 1_000)
    return {
        "messageId": f"message-{node.name}-{receive_count}",
        "body": node.body,
        "attributes": {
            "ApproximateReceiveCount": str(receive_count),
            "SentTimestamp": str(sent),
            "ApproximateFirstReceiveTimestamp": str(sent + 1_000),
        },
    }


def envelope(node: NodeRecord, member: CohortMember, receive_count: int = 1) -> PostGrantEnvelope:
    """Parse the node's delivered envelope exactly as the worker does."""
    parsed = post_grant_envelope(sqs_record(node, receive_count), member)
    assert parsed is not None
    return parsed


@dataclass
class Clock:
    """A wall clock and a remaining-time budget a test advances."""

    remaining: list[int] = field(default_factory=lambda: [800_000])

    def now(self) -> Any:
        """Return the fixed wall instant."""
        return NOW

    def remaining_ms(self) -> int:
        """Return the next scripted remaining time (the last value repeats)."""
        if len(self.remaining) > 1:
            return self.remaining.pop(0)
        return self.remaining[0]


def ports(fake: FakeDynamo, clock: Clock, metrics: list[str]) -> ConsumerPorts:
    """Compose the consumer ports over the fake, recording the invariant metric."""
    return ConsumerPorts(
        release_store(fake),
        clock.now,
        clock.remaining_ms,
        invariant_metric=metrics.append,
        logger=LOGGER,
    )


# --- the preset-artifact side ----------------------------------------------------------------


def publication(
    intent: BakeIntent, identity: str, current: PresetArtifactPointer | None
) -> PresetArtifactPublicationRequest:
    """Build a real source publication for one intent over a current pointer."""
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
    )


@dataclass
class Pointers:
    """A page/variant pointer table with strong reads."""

    current_by_key: dict[str, PresetArtifactPointer] = field(
        default_factory=dict[str, PresetArtifactPointer]
    )
    reads: list[str] = field(default_factory=list[str])

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        """Return the current pointer of one page/variant."""
        self.reads.append(f"{page.value}#{variant.value}")
        return self.current_by_key.get(f"{page.value}#{variant.value}")


@dataclass
class Sources:
    """A registered baker whose per-intent source identity decides each result.

    `identities[key]` is the publication identity the source computes; a source whose computed
    revision equals the current pointer's returns unchanged, as the real sources do (D26). A
    key in `unavailable` returns no artifact; a key in `raises` raises that error.
    """

    pointers: Pointers
    identities: dict[str, str] = field(default_factory=dict[str, str])
    unavailable: set[str] = field(default_factory=set[str])
    raises: dict[str, AdlyticsError] = field(default_factory=dict[str, AdlyticsError])
    calls: list[str] = field(default_factory=list[str])
    pages: tuple[str, ...] = ("campaigns", "retail-health")

    def registered_pages(self) -> tuple[str, ...]:
        """Return the registered pages (campaigns and retail-health, as at the pin)."""
        return self.pages

    def bake(self, intent: BakeIntent) -> BakeResult:
        """Render one intent from its scripted source identity."""
        name = key(intent)
        self.calls.append(name)
        if name in self.raises:
            raise self.raises[name]
        if name in self.unavailable:
            return BakeUnavailable(intent, "campaign_materialization_unavailable")
        current = self.pointers.current_by_key.get(name)
        identity = self.identities.get(name, f"publication-{name}-1")
        revision = PresetArtifactRevision.from_publication_identities((identity,))
        if current is not None and current.revision == revision:
            return BakeUnchanged(intent, current.revision)
        return BakePublication(publication(intent, identity, current))


@dataclass
class Persistence:
    """Write the body, then advance the pointer table; scripted outcomes per key."""

    pointers: Pointers
    outcomes: dict[str, PresetArtifactPointerAdvanceOutcome] = field(
        default_factory=dict[str, PresetArtifactPointerAdvanceOutcome]
    )
    raises: dict[str, Exception] = field(default_factory=dict[str, Exception])
    written: list[PresetArtifactPublicationRequest] = field(
        default_factory=list[PresetArtifactPublicationRequest]
    )
    before_publish: Callable[[PresetArtifactPublicationRequest], None] | None = None
    after_publish: Callable[[PresetArtifactPublicationRequest], None] | None = None
    """Runs after the pointer advanced: an error there is a write whose result was lost."""

    def publish(
        self, request: PresetArtifactPublicationRequest, **_deadline: object
    ) -> PresetArtifactPublicationResult:
        """Advance the pointer table unless an outcome or an error is scripted for the key."""
        staged = staged_result(request)
        pointer = request.pointer_template.complete(staged.custody_digest).pointer
        name = pointer.partition_key
        if self.before_publish is not None:
            self.before_publish(request)
        if name in self.raises:
            raise self.raises[name]
        outcome = self.outcomes.get(name, PresetArtifactPointerAdvanceOutcome.ADVANCED)
        if outcome is PresetArtifactPointerAdvanceOutcome.ADVANCED:
            self.written.append(request)
            self.pointers.current_by_key[name] = pointer
        if self.after_publish is not None:
            self.after_publish(request)
        return PresetArtifactPublicationResult(
            staged.body,
            PresetArtifactPointerAdvanceResult(outcome),
            staged.custody,
            staged.pairs,
            staged.written_bytes,
        )


def expire_claim(fake: FakeDynamo, node: NodeRecord) -> None:
    """Expire the claim's lease (a crash: the next receive is a RECLAIM)."""
    item = fake.items[(f"EXEC_CLAIM#{node.node_id}", "CLAIM")]
    item["lease_expires_at"] = write_text((NOW - timedelta(seconds=1)).isoformat())


def with_target(  # noqa: PLR0913, PLR0917 -- the run and its one target
    fake: FakeDynamo, run: str, partition: str, publication_id: str
) -> None:
    """Give the run a settled `materialize_raw` naming one zero-row target (slice 3's shape).

    The trigger the graph precompute attests is read from the run's effect records.
    """
    from adapters.inbound.ingestion_handlers.graph_materialize import (  # noqa: PLC0415
        materialize_effect_digest,
    )

    record = json.dumps(
        {
            "schema_version": "1",
            "node": "materialize_raw",
            "roster_digest": "r" * 64,
            "units": [],
            "zero_row_targets": [
                {"partition": partition, "publication_id": publication_id, "fencing_token": 1}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    source = next(
        item
        for (pk, sk), item in fake.items.items()
        if pk == f"RUN#{run}" and sk.startswith("NODE#write_")
    )
    item = copy.deepcopy(source)
    item["sk"] = write_text("NODE#materialize_raw")
    item["node"] = write_text("materialize_raw")
    item["node_id"] = write_text("m" * 64)
    item["state"] = write_text("SUCCEEDED")
    item["effect_record"] = write_text(record)
    item["effect_digest"] = write_text(materialize_effect_digest(record))
    fake.items[(f"RUN#{run}", "NODE#materialize_raw")] = item
