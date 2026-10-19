# SPDX-License-Identifier: MIT-0
"""Fixtures for the S6 slice 4b `publish` tests: a granted run over both real adapters.

One in-memory fake holds the execution-graph table (`pk`/`sk`) and the publications table
(`partition_key`/`record_key`) side by side, so the flip's cross-table transaction evaluates
every member's condition atomically, as DynamoDB does. The graph side is driven through the real
instantiation, release, dispatcher and claim code; the publications side through the real
`DynamoPublicationStore` and `DynamoCurrentPublications`. Every identity is fictional.
"""

from __future__ import annotations

# The publication fixture spec and the store's item renderer are shared test seams.
# pyright: reportPrivateUsage=false
import copy
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from dynamodb_reserved_words import guard_reserved_words
from execution_graph_dispatch_support import (
    TABLE,
    UNIT,
    SpySender,
    now_text,
    seed_run,
    set_mode,
    store,
    write_text,
)
from execution_graph_release_support import WINDOW, context, release_store
from execution_graph_support import FakeDynamo
from publication_support import _PublicationSpec, successful_publication

from adapters.inbound.ingestion_handlers.graph_claim import GraphClaimInput, claim_graph_envelope
from adapters.inbound.ingestion_handlers.graph_materialize import materialize_effect_digest
from adapters.inbound.ingestion_handlers.graph_publish import (
    PublishEnvelope,
    PublishPorts,
    PublishResult,
    publish_claimed,
)
from adapters.inbound.ingestion_handlers.graph_unit import UnitTarget, unit_effect_record
from adapters.jobs.execution_graph.dispatcher import DispatchContext, dispatch_shard
from adapters.jobs.execution_graph.node_verifier import UnreachablePostGrantFacts
from adapters.jobs.execution_graph.release import ReleaseOutcome, release_run
from adapters.outbound.dynamodb.execution_graph import due_shard
from adapters.outbound.dynamodb.semantic_state import (
    CURRENT_RECORD,
    GENERATION_POINTER_RECORD,
    PARTITION_KEY,
    PUBLICATION_RECORD_KEY,
    DynamoCurrentPublications,
    DynamoPublicationStore,
    DynamoPublicationStoreAccess,
    _publication_item,
    generation_pointer_partition,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import NodePhase
from domain.semantics.publication import CanonicalPublisherKey, SuccessfulPublication
from ports.outbound.execution_graph_dispatch import ClaimRecord, NodeSpec, PutNodesRequest

PUBLICATIONS = "fictional-publications"
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
LEASE = timedelta(seconds=960)
CURATION = CanonicalPublisherKey("canonical-curation")
STAGE1_FLOW = "77777777-7777-4777-8777-777777777777"


@guard_reserved_words
class Tables(FakeDynamo):
    """The graph table and the publications table in one transactional fake."""

    def _key(self, key: dict[str, Any]) -> tuple[str, str]:
        if PARTITION_KEY in key:
            return (f"P|{key[PARTITION_KEY]['S']}", str(key[PUBLICATION_RECORD_KEY]["S"]))
        return super()._key(key)

    def transact_get_items(self, **request: Any) -> dict[str, Any]:
        gets = cast("list[dict[str, Any]]", request["TransactItems"])
        assert 1 <= len(gets) <= 100
        responses: list[dict[str, Any]] = []
        for get in gets:
            item = self.items.get(self._key(get["Get"]["Key"]))
            responses.append({} if item is None else {"Item": copy.deepcopy(item)})
        return {"Responses": responses}

    def current(self, partition_key: str) -> dict[str, Any]:
        """Return the live CURRENT item of one partition (mutable, for interleavings)."""
        return self.items[(f"P|{partition_key}", CURRENT_RECORD)]

    def pointer(self, conflict_unit: str = UNIT) -> dict[str, Any] | None:
        """Return the window generation pointer, if any."""
        return self.items.get(
            (f"P|{generation_pointer_partition(conflict_unit)}", GENERATION_POINTER_RECORD)
        )

    def publication_writes(self) -> list[dict[str, Any]]:
        """Every committed member that touched the publications table."""
        return [
            member
            for transaction in self.transactions
            for member in transaction
            if PARTITION_KEY in next(iter(member.values())).get("Key", {})
        ]


def publication(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    day: date, name: str, token: int, *, curation: bool = False
) -> SuccessfulPublication:
    """Build one fictional daily publication at an exact fencing token."""
    built = replace(
        successful_publication("unit_dataset", day, _PublicationSpec(f"{name}-{day}", NOW)),
        fencing_token=token,
    )
    return replace(built, publisher=CURATION) if curation else built


def seed_current(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    fake: Tables,
    authority: SuccessfulPublication,
    base: SuccessfulPublication | None,
    *,
    others: tuple[SuccessfulPublication, ...] = (),
) -> None:
    """Store the immutables and one stamped CURRENT whose base is `base` (None is `NONE`)."""
    for item in (authority, *others, *(() if base is None else (base,))):
        stored: dict[str, Any] = dict(_publication_item(item, current=False))
        fake.items[fake._key(stored)] = stored
    current: dict[str, Any] = dict(_publication_item(authority, current=True))
    current["projected_publication_id"] = {
        "S": "NONE" if base is None else base.publication_id.value
    }
    current["projected_fencing_token"] = {"N": "0" if base is None else str(base.fencing_token)}
    current["projected_at"] = {"S": NOW.isoformat()}
    fake.items[fake._key(current)] = current


def _target(item: SuccessfulPublication) -> dict[str, object]:
    return {
        "partition": item.partition.identity,
        "publication_id": item.publication_id.value,
        "fencing_token": item.fencing_token,
    }


def materialize_record(
    units: tuple[str, ...], zero_row: tuple[SuccessfulPublication, ...]
) -> tuple[str, str]:
    """Render a `materialize_raw` effect record in slice 3's exact shape, and its digest."""
    record = json.dumps(
        {
            "schema_version": "1",
            "node": "materialize_raw",
            "roster_digest": "r" * 64,
            "units": list(units),
            "zero_row_targets": [
                _target(item)
                for item in sorted(
                    zero_row, key=lambda item: (item.partition.identity, item.publication_id.value)
                )
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return record, materialize_effect_digest(record)


@dataclass
class PublishWorld:
    """One granted run whose `publish` node is claimed, and its two tables."""

    fake: Tables
    run: str
    claim: ClaimRecord
    envelope: PublishEnvelope
    metrics: list[str] = field(default_factory=list[str])
    publications: Any = None

    @property
    def store(self) -> Any:
        """The real graph dispatch adapter over the fake."""
        return store(self.fake)

    def ports(self) -> PublishPorts:
        """Compose the publish ports over the fake, recording the invariant metric."""
        return PublishPorts(
            publications=self.publications,
            graph_table=TABLE,
            invariant_metric=self.metrics.append,
        )

    def publish(self, receive_count: int = 1) -> PublishResult:
        """Run `publish_claimed` for the held claim on this receive."""
        from aws_lambda_powertools import Logger  # noqa: PLC0415

        return publish_claimed(
            self.store,
            self.ports(),
            replace(self.envelope, receive_count=receive_count),
            self.claim,
            lambda: NOW,
            Logger(service="adlytics-graph-publish-test"),
        )

    def reclaim(self) -> None:
        """Take the next receive's claim (the lease was released, so it reads RECLAIM)."""
        node = self.publish_node()
        disposition = claim_graph_envelope(
            self.store,
            UnreachablePostGrantFacts(),
            GraphClaimInput(
                run_id=self.run,
                node_id=node.node_id,
                dispatch_id=node.dispatch_id,
                body=node.body,
                now=lambda: NOW + timedelta(seconds=1),
                lease=LEASE,
            ),
        )
        assert disposition.claim is not None, disposition.outcome
        self.claim = disposition.claim

    def publish_node(self) -> Any:
        """Read the `publish` node strongly."""
        return next(item for item in self.store.read_nodes(self.run) if item.name == "publish")

    def reader(self) -> DynamoCurrentPublications:
        """Return a serving reader over the publications table."""
        return DynamoCurrentPublications(
            PUBLICATIONS, client=cast("Any", self.fake), retry_pause=lambda: None
        )

    def claim_item(self) -> dict[str, Any]:
        """Return the raw `EXEC_CLAIM#` item of the publish node."""
        return self.fake.items[(f"EXEC_CLAIM#{self.claim.node_id}", "CLAIM")]


def publications_store(fake: Tables) -> DynamoPublicationStore:
    """Return the real publications adapter over the fake, at the fixed clock."""
    return DynamoPublicationStore(
        PUBLICATIONS,
        "fictional-leases",
        DynamoPublicationStoreAccess(client=cast("Any", fake), clock=lambda: NOW),
    )


def publish_world(  # noqa: PLR0913 -- one fixture builder with explicit knobs
    *,
    units: dict[str, tuple[SuccessfulPublication, ...]],
    zero_row: tuple[SuccessfulPublication, ...] = (),
    fake: Tables | None = None,
    claimed: bool = True,
) -> PublishWorld:
    """Seed, grant and apply one run, dispatch its `publish`, and take the claim.

    `units` maps each `write_unit:<k>` key to the servable targets its effect record names;
    every target's CURRENT must already be seeded (`seed_current`). `claimed=False` leaves the
    dispatched node for TableWriter to claim (the held claim is then a placeholder).
    """
    tables = fake or Tables()
    if ("MODE#ingestion", "MODE") not in tables.items:
        set_mode(tables, AuthorityMode.GRAPH)
    seeded = seed_run(tables, compute=True, instantiate=False)
    name = seeded.record.run_id
    tables.items[(f"RUN#{name}", "RUN")]["window"] = write_text(WINDOW)
    tables.items[(f"RUN#{name}", "RUN")]["root_clock_started_at"] = write_text(
        (NOW - timedelta(hours=2)).isoformat()
    )
    keys = tuple(sorted(units))
    specs = [
        NodeSpec("materialize_raw", (), NodePhase.PRE_GRANT, f"MATERIALIZE#{UNIT}", UNIT, "{}"),
        *(
            NodeSpec(
                f"write_unit:{key}",
                ("materialize_raw",),
                NodePhase.PRE_GRANT,
                f"TABLE#{key}",
                key,
                json.dumps({"unit": key}),
            )
            for key in keys
        ),
        NodeSpec(
            "grant_apply",
            tuple(f"write_unit:{key}" for key in keys) or ("materialize_raw",),
            NodePhase.PRE_GRANT,
            f"GRANT_APPLY#{UNIT}",
            f"grant_apply#{UNIT}",
            "{}",
        ),
    ]
    run = release_store(tables).read_run(name)
    assert run is not None
    release_store(tables).put_nodes(PutNodesRequest(run, tuple(specs), now_text()))
    effects = {"materialize_raw": materialize_record(keys, zero_row)}
    for key in keys:
        effects[f"write_unit:{key}"] = unit_effect_record(
            key,
            STAGE1_FLOW,
            tuple(
                UnitTarget(item.partition.identity, item.publication_id.value, item.fencing_token)
                for item in units[key]
            ),
        )
    for node_name, (record, digest) in effects.items():
        item = tables.items[(f"RUN#{name}", f"NODE#{node_name}")]
        item["state"] = write_text("SUCCEEDED")
        item["effect_record"] = write_text(record)
        item["effect_digest"] = write_text(digest)
    shard = due_shard(UNIT)
    tables.items[(shard, f"RELEASE#{name}")] = {
        "pk": write_text(shard),
        "sk": write_text(f"RELEASE#{name}"),
        "item_kind": write_text("release"),
        "run_id": write_text(name),
        "due_at": write_text(now_text()),
    }
    assert release_run(context(tables), name) is ReleaseOutcome.APPLIED
    dispatch_shard(DispatchContext(store(tables), SpySender(), now_text), shard)
    node = next(item for item in store(tables).read_nodes(name) if item.name == "publish")
    body = json.loads(node.body)
    envelope = PublishEnvelope(
        run_id=name,
        node_id=node.node_id,
        dispatch_id=node.dispatch_id,
        grant_id=str(body["grant_id"]),
        granted_run=str(body["granted_run"]),
        conflict_unit=str(body["conflict_unit"]),
        receive_count=1,
    )
    if not claimed:
        placeholder = ClaimRecord(node.node_id, name, 0, NOW.isoformat(), node.dispatch_id, "")
        return PublishWorld(
            tables, name, placeholder, envelope, publications=publications_store(tables)
        )
    disposition = claim_graph_envelope(
        store(tables),
        UnreachablePostGrantFacts(),
        GraphClaimInput(
            run_id=name,
            node_id=node.node_id,
            dispatch_id=node.dispatch_id,
            body=node.body,
            now=lambda: NOW,
            lease=LEASE,
        ),
    )
    assert disposition.claim is not None, disposition.outcome
    return PublishWorld(
        tables, name, disposition.claim, envelope, publications=publications_store(tables)
    )
