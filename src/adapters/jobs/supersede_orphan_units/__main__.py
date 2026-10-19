# SPDX-License-Identifier: MIT-0
r"""``adapters.jobs.supersede_orphan_units``: the reviewed TD-50 disposition of orphan plan units.

Orphan plans sit `planned` in the legacy coordinator (publications) table with `queued`, admitted,
never-begun units, no queue message and no live intent. `verify-drain` counts them as durable
nonterminal authority, which is right, so they block the drain proof until each one is
dispositioned. This command proves, unit by unit, that an orphan's data is superseded
(`evidence.py`: the partition's `CURRENT` projection is at or past the unit's publication, or a
newer attested source append covers it) and records that proof.

- **Dry run by default.** Reads only: one consistent `Scan` of the `WRITE_PLAN#` partitions, strong
  `CURRENT` reads, immutable S3 objects, and the semantic-write queue and DLQ counts. It writes the
  evidence file and nothing in AWS.
- **`--apply`** first writes the same evidence file, then refuses unless the semantic-write queue
  and DLQ are empty (so no message is behind any unit), then commits one `TransactWriteItems` per
  proven plan: each orphan unit `queued -> superseded` with its evidence digest, the plan `META`
  `planned -> superseded` with the plan's evidence digest, the successor owner's active fields
  cleared when they name the plan, and condition checks on every attested sibling and the origin
  intent. Every member is conditioned on the exact state read, so a unit that moved is refused and
  reported, never overwritten. Any other transaction failure stops the run, and the plans after
  it are recorded as not attempted. It then writes the outcome file.

A plan with any unproven orphan unit is left untouched, and every such unit is reported with its
reason. It writes no lifecycle event: the plan flows' operational-status heads are not advanced.
`verify-drain` accepts a `superseded` unit as terminal only with its evidence digest.

FEAT-011 adds two plan-shape cases (`evidence.py`). Gate 2 admits a plan whose `META` carries
`plan_reservation_count` only when it equals the attested siblings' `unit_reservation_count` sum;
its transaction pins that count (`plan_reservation_count = :reservations`) and each sibling's
count. The failed-unit variant (`--failed-unit-plan` with `--dlq-archive` and
`--dlq-deletion-record`) supersedes the named plan's `META` only, leaving its one failed unit
`failed` and pinned, with that unit's projection evidence and the dead-letter accounting bound
into the plan's evidence digest.

Why a superseded plan takes no new reservation: Begin's `META` member is conditioned on
`#status = :planned` and its unit member on `#status = :writing`, and the claim that moves a
unit to `writing` is conditioned on the `META` `planned` and on the unit's exact read. This
transaction moves the `META` and every orphan unit out of those states in one commit, so no
Begin or claim built from an earlier read can succeed after it; one that commits first changes
a counter or status this transaction pins, and it is refused as `refused_state_moved`.

The exit code is 0 only when every orphan unit was proven (and, with `--apply`, committed).

Usage:
    uv run python -m adapters.jobs.supersede_orphan_units \
        --profile <PROFILE> --env <ENV> --region <REGION> --expected-account <ACCOUNT_ID> \
        --evidence-dir <DIR> [--failed-unit-plan <PLAN_KEY> --dlq-archive <JSON> \
        --dlq-deletion-record <FILE>] [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import boto3
from botocore.exceptions import ClientError

from adapters.inbound.ingestion_handlers.table_attempt_protocol import instant
from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
from adapters.jobs.stage1_operator_bootstrap import parameter_path
from adapters.jobs.supersede_orphan_units.evidence import (
    OrphanPlan,
    PlanRows,
    PlanShape,
    Rule,
    SourceFacts,
    Unproven,
    evidence_digest,
    orphan_plan,
    projection_evidence,
    source_evidence,
    source_facts,
    unit_reservations,
)
from adapters.outbound.dynamodb.stage1_writer_authority import unit_entry_from_item
from adapters.outbound.s3.semantic_storage import (
    projection_source_publication,
    projection_unit_source,
    source_object_key,
)
from domain.operations.write_plan import (
    INTENT_SEGMENT,
    META_RECORD_KEY,
    SUCCESSOR_OWNER_RECORD_KEY,
    UNIT_RECORD_PREFIX,
    UNIT_SUPERSESSION_EVIDENCE_FIELD,
    WRITE_PLAN_PREFIX,
    CanonicalObjectEntry,
    IntentState,
    PlanStatus,
    PlanUnitAdmission,
    PlanUnitStatus,
    intent_partition_key,
    owner_partition_key,
    unit_record_key,
)
from domain.semantics.canonical_compiler import canonical_checksum_digest
from ports.outbound.canonical import CanonicalDestination

__all__ = [
    "Clients",
    "FailedUnitAccounting",
    "Targets",
    "build_parser",
    "disposition",
    "failed_unit_accounting",
    "main",
    "transaction",
]

QUEUE_KEY: Final = "semanticWriteQueueArn"
DLQ_KEY: Final = "semanticWriteDlqArn"
SCHEMA_VERSION: Final = "1"
_ACCOUNT: Final = re.compile(r"\A\d{12}\Z")
_QUEUE_COUNTS: Final = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
_TRANSACTION_CANCELLED: Final = "TransactionCanceledException"
_CONDITION_FAILED: Final = "ConditionalCheckFailed"
_CURRENT_RECORD: Final = "CURRENT"
_KINDS: Final = {"current": "canonical", "candidate": "canonical", "source_aligned": "source"}

type Plain = Mapping[str, object]


# --- Plain items ----------------------------------------------------------------------------------


def _plain_value(value: Mapping[str, object]) -> object:
    ((tag, raw),) = value.items()
    if tag == "S":
        return raw
    if tag == "N":
        return int(cast("str", raw))
    if tag == "BOOL":
        return bool(raw)
    if tag == "NULL":
        return None
    if tag == "L":
        return [
            _plain_value(cast("Mapping[str, object]", member))
            for member in cast("list[object]", raw)
        ]
    if tag == "M":
        return {
            str(name): _plain_value(cast("Mapping[str, object]", member))
            for name, member in cast("dict[object, object]", raw).items()
        }
    raise ValueError(f"an attribute type outside the write-plan families: {tag}")


def _plain(item: Mapping[str, object]) -> dict[str, object]:
    return {name: _plain_value(cast("Mapping[str, object]", value)) for name, value in item.items()}


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


# --- Reads ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Targets:
    """The resolved DataV2 resources and the semantic-write queues."""

    table: str
    bucket: str
    queue_url: str
    dlq_url: str


@dataclass(frozen=True, slots=True)
class Clients:
    """The low-level clients the disposition reads and writes through."""

    dynamodb: Any
    sqs: Any
    read_object: Callable[[str], bytes]
    """One exact immutable object from the semantic bucket, by key."""


@dataclass(slots=True)
class _Scanned:
    plans: dict[str, PlanRows] = field(default_factory=dict[str, PlanRows])
    owners: dict[str, Plain] = field(default_factory=dict[str, Plain])
    pages: int = 0


def _scan(clients: Clients, table: str) -> _Scanned:
    """One complete strongly consistent scan of every `WRITE_PLAN#` item."""
    metas: dict[str, Plain] = {}
    units: dict[str, dict[str, Plain]] = {}
    intents: dict[str, Plain] = {}
    scanned = _Scanned()
    start: Mapping[str, object] | None = None
    while True:
        request: dict[str, object] = {
            "TableName": table,
            "ConsistentRead": True,
            "FilterExpression": "begins_with(partition_key, :prefix)",
            "ExpressionAttributeValues": {":prefix": _s(WRITE_PLAN_PREFIX)},
        }
        if start is not None:
            request["ExclusiveStartKey"] = start
        response = clients.dynamodb.scan(**request)
        scanned.pages += 1
        for low in response.get("Items", []):
            item = _plain(low)
            partition, record = str(item["partition_key"]), str(item["record_key"])
            if record == SUCCESSOR_OWNER_RECORD_KEY:
                scanned.owners[partition] = item
            elif INTENT_SEGMENT in partition:
                if record == META_RECORD_KEY:
                    intents[partition] = item
            elif record == META_RECORD_KEY:
                metas[partition] = item
            elif record.startswith(UNIT_RECORD_PREFIX):
                units.setdefault(partition, {})[str(item.get("unit_key", ""))] = item
        start = response.get("LastEvaluatedKey")
        if start is None:
            break
    for plan_key, meta in metas.items():
        intent_key = intent_partition_key(str(meta["plan_digest"]), str(meta["origin_intent_id"]))
        scanned.plans[plan_key] = PlanRows(
            plan_key, meta, units.get(plan_key, {}), intents.get(intent_key)
        )
    return scanned


def _current(clients: Clients, table: str, partition: str) -> Plain | None:
    response = clients.dynamodb.get_item(
        TableName=table,
        Key={"partition_key": _s(partition), "record_key": _s(_CURRENT_RECORD)},
        ConsistentRead=True,
    )
    found = response.get("Item")
    return None if found is None else _plain(found)


def _queue_depth(clients: Clients, url: str) -> int:
    attributes = clients.sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=list(_QUEUE_COUNTS)
    ).get("Attributes", {})
    return sum(int(attributes.get(name, "0")) for name in _QUEUE_COUNTS)


# --- Assessment -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _UnitVerdict:
    unit: Plain
    record: dict[str, object] | None
    reason: Unproven | None
    detail: dict[str, object]

    @property
    def digest(self) -> str:
        if self.record is None:
            raise ValueError("an unproven unit has no evidence digest.")
        return evidence_digest(self.record)


@dataclass(slots=True)
class _Assessor:
    clients: Clients
    targets: Targets
    scanned: _Scanned
    attested_sources: dict[str, list[tuple[str, Plain]]] = field(
        default_factory=dict[str, list[tuple[str, Plain]]]
    )
    facts_cache: dict[tuple[str, str], SourceFacts | None] = field(
        default_factory=dict[tuple[str, str], SourceFacts | None]
    )

    def __post_init__(self) -> None:
        for plan in self.scanned.plans.values():
            for unit_key, unit in plan.units.items():
                if (
                    unit.get("status") == PlanUnitStatus.ATTESTED.value
                    and unit.get("table_kind") == "source_aligned"
                ):
                    self.attested_sources.setdefault(unit_key, []).append((plan.plan_key, unit))

    def _source(self, entry: CanonicalObjectEntry, plan_key: str, unit_key: str) -> SourceFacts:
        """Read and verify one source object against its unit entry (raises when it disagrees)."""
        publication = projection_source_publication(
            self.clients.read_object(source_object_key(entry.reference))
        )
        if publication.reference.value != entry.reference or (
            canonical_checksum_digest(publication.content_checksum) != entry.content_checksum
        ):
            raise ValueError("the source object differs from its unit entry.")
        return source_facts(publication, plan_key, unit_key)

    def _candidate(self, plan_key: str, unit: Plain) -> list[SourceFacts]:
        """Every readable source publication of one attested unit; unreadable ones prove nothing."""
        found: list[SourceFacts] = []
        unit_key = str(unit.get("unit_key"))
        try:
            entries = unit_entry_from_item(unit).objects
        # error-propagation: an unreadable unit proves nothing; it adds no candidate
        except Exception:
            return found
        for entry in entries:
            if entry.kind != "source":
                continue
            cached = (plan_key, entry.reference)
            if cached not in self.facts_cache:
                try:
                    self.facts_cache[cached] = self._source(entry, plan_key, unit_key)
                # error-propagation: an unreadable source proves nothing; None adds no candidate
                except Exception:
                    self.facts_cache[cached] = None
            facts = self.facts_cache[cached]
            if facts is not None:
                found.append(facts)
        return found

    def _canonical(
        self, table_kind: str, entry: CanonicalObjectEntry
    ) -> tuple[dict[str, object] | None, Unproven | None]:
        """Decode the immutable object and verify it is the unit entry's exact workload.

        The same identity TableWriter checks before it composes the write: destination,
        publication, fencing token, partition (a candidate's comes from its object) and the
        canonicalized content checksum. Any disagreement leaves the unit unproven.
        """
        destination = (
            CanonicalDestination.CANDIDATE
            if table_kind == "candidate"
            else CanonicalDestination.SUCCESSFUL
        )
        try:
            source = projection_unit_source(self.clients.read_object(entry.reference))
        # error-propagation: the evidence records OBJECT_UNREADABLE; the unit is not superseded
        except Exception:
            return None, Unproven.OBJECT_UNREADABLE
        try:
            checksum = canonical_checksum_digest(source.content_checksum)
        except ValueError:
            return None, Unproven.OBJECT_DISAGREES
        if (
            source.destination != destination.value
            or source.publication_id != entry.publication_id
            or source.fencing_token != entry.fencing_token
            or (entry.partition_key is not None and source.partition_key != entry.partition_key)
            or checksum != entry.content_checksum
        ):
            return None, Unproven.OBJECT_DISAGREES
        partition = source.partition_key
        current = _current(self.clients, self.targets.table, partition)
        return projection_evidence(partition, entry.publication_id, entry.fencing_token, current)

    def _sourced(
        self, plan_key: str, unit: Plain, entry: CanonicalObjectEntry
    ) -> tuple[dict[str, object] | None, Unproven | None]:
        unit_key = str(unit.get("unit_key"))
        try:
            orphan = self._source(entry, plan_key, unit_key)
        # error-propagation: the evidence records OBJECT_UNREADABLE; the unit is not superseded
        except Exception:
            return None, Unproven.OBJECT_UNREADABLE
        # Plan-unit creation order is not part of the rule: a strictly later source publication
        # proves the append newer, whichever plan was created first.
        candidates = [
            facts
            for other_plan, other in self.attested_sources.get(unit_key, [])
            if other_plan != plan_key
            for facts in self._candidate(other_plan, other)
        ]
        return source_evidence(orphan, candidates)

    def unit(self, plan: PlanRows, unit: Plain) -> _UnitVerdict:
        """Prove one orphan unit object by object, or name the first object that is unproven."""
        table_kind = str(unit.get("table_kind", ""))
        try:
            entry = unit_entry_from_item(unit)
        # error-propagation: the evidence records UNIT_SHAPE and its cause; not superseded
        except Exception as error:
            return _UnitVerdict(unit, None, Unproven.UNIT_SHAPE, {"cause": type(error).__name__})
        expected = _KINDS.get(table_kind)
        objects: list[dict[str, object]] = []
        for item in entry.objects:
            if expected is None or item.kind != expected:
                return _UnitVerdict(unit, None, Unproven.UNKNOWN_KIND, {"kind": item.kind})
            proof, reason = (
                self._sourced(plan.plan_key, unit, item)
                if expected == "source"
                else self._canonical(table_kind, item)
            )
            if proof is None:
                return _UnitVerdict(
                    unit, None, reason, {"reference": item.reference, "kind": item.kind}
                )
            # The checksum was verified against the immutable object above; recording it binds
            # the evidence digest to the exact workload TableWriter would have executed.
            objects.append(
                {"reference": item.reference, "content_checksum": item.content_checksum, **proof}
            )
        rule = (
            Rule.NEWER_ATTESTED_SOURCE_APPEND
            if expected == "source"
            else Rule.PROJECTION_AT_OR_PAST
        )
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "plan_key": plan.plan_key,
            "unit_key": entry.unit_key,
            "table_kind": table_kind,
            "unit_flow_id": unit.get("flow_id"),
            "unit_created_at": unit.get("created_at"),
            "rule": rule.value,
            "objects": objects,
        }
        return _UnitVerdict(unit, record, None, {})


# --- The plan decision and its transaction --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedUnitAccounting:
    """The operator's account of one failed unit's dead-letter message (FEAT-011 class b).

    A failed unit's message went to the semantic-write DLQ. The variant admits its plan only when
    that message is accounted for: archived whole to an operator-local evidence file, then deleted
    from the DLQ. The archive must name
    this plan and the failed unit, and the deletion record must name the archived message id;
    both files' digests are bound into the plan's evidence digest.
    """

    plan_key: str
    unit_key: str
    record: Mapping[str, object]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def failed_unit_accounting(plan_key: str, archive: Path, deletion: Path) -> FailedUnitAccounting:
    """Read and check the archived DLQ message and its deletion record; raise when they disagree."""
    document = cast("dict[str, object]", json.loads(archive.read_text(encoding="utf-8")))
    body = cast("dict[str, object]", json.loads(str(document.get("body", ""))))
    message_id = document.get("message_id")
    queue = str(document.get("queue", ""))
    if body.get("plan_key") != plan_key:
        raise ValueError("the archived dead-letter message names another plan.")
    if not queue.endswith("semantic-write-dlq.fifo"):
        raise ValueError("the archived message is not from the semantic-write DLQ.")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("the archived message carries no message id.")
    if message_id not in deletion.read_text(encoding="utf-8"):
        raise ValueError("the deletion record does not name the archived message id.")
    unit_key = body.get("unit_key")
    if not isinstance(unit_key, str) or not unit_key.strip():
        raise ValueError("the archived message names no unit.")
    return FailedUnitAccounting(
        plan_key,
        unit_key,
        {
            "archive": str(archive),
            "archive_sha256": _sha256(archive),
            "message_id": message_id,
            "queue": queue,
            "unit_key": unit_key,
            "deletion_record": str(deletion),
            "deletion_record_sha256": _sha256(deletion),
        },
    )


@dataclass(frozen=True, slots=True)
class _PlanDecision:
    plan: PlanRows
    verdicts: tuple[_UnitVerdict, ...]
    reason: Unproven | None
    owner: Plain | None
    admitted: OrphanPlan | None = None
    failed: _UnitVerdict | None = None
    """The failed unit's projection evidence (variant b); the unit itself is never written."""
    accounting: FailedUnitAccounting | None = None

    @property
    def proven(self) -> bool:
        return (
            self.reason is None
            and self.admitted is not None
            and all(v.record is not None for v in self.verdicts)
            and (self.failed is None or self.failed.record is not None)
        )

    def _spent(self) -> list[tuple[str, Plain]]:
        """Return the units whose reservations the gate-2 sum counts, in unit-key order."""
        statuses = {PlanUnitStatus.ATTESTED.value, PlanUnitStatus.FAILED.value}
        return [
            (key, unit)
            for key, unit in sorted(self.plan.units.items())
            if unit.get("status") in statuses
        ]

    def record(self) -> dict[str, object]:
        record = self._base_record()
        admitted = self.admitted
        if admitted is None or admitted.shape is PlanShape.NO_RESERVATION:
            # Gate 1 records stay byte-identical to the FEAT-010 apply's, digest included.
            return record
        record["plan_shape"] = admitted.shape.value
        record["reservations"] = {
            "plan_reservation_count": admitted.reservations,
            "unit_reservation_counts": {
                key: unit_reservations(unit) for key, unit in self._spent()
            },
        }
        if self.failed is not None and self.accounting is not None:
            record["failed_unit"] = {
                "unit_key": self.failed.unit.get("unit_key"),
                "unit_reservation_count": self.failed.unit.get("unit_reservation_count"),
                "evidence_digest": self.failed.digest,
            }
            record["dead_letter_accounting"] = dict(self.accounting.record)
        return record

    def _base_record(self) -> dict[str, object]:
        meta, intent, owner = self.plan.meta, self.plan.intent, self.owner
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_key": self.plan.plan_key,
            "meta": {
                name: meta.get(name)
                for name in (
                    "status",
                    "plan_digest",
                    "origin_intent_id",
                    "generation",
                    "unit_count",
                    "admitted_count",
                    "attested_count",
                    "failed_count",
                    "created_at",
                )
            },
            "intent_state": None if intent is None else intent.get("state"),
            "owner": None
            if owner is None
            else {
                "revision": owner.get("revision"),
                "active_plan_key": owner.get("active_plan_key"),
            },
            "attested_units": sorted(
                key
                for key, unit in self.plan.units.items()
                if unit.get("status") == PlanUnitStatus.ATTESTED.value
            ),
            "unit_evidence_digests": {
                str(v.unit.get("unit_key")): v.digest for v in self.verdicts if v.record is not None
            },
        }

    def document(self) -> dict[str, object]:
        """Render the plan's reviewable member, with every unit's evidence or reason."""
        document: dict[str, object] = {
            "plan_key": self.plan.plan_key,
            "decision": "supersede" if self.proven else "left_untouched",
            "units": [
                {
                    "unit_key": verdict.unit.get("unit_key"),
                    "evidence": verdict.record,
                    "evidence_digest": verdict.digest,
                }
                if verdict.record is not None
                else {
                    "unit_key": verdict.unit.get("unit_key"),
                    "unproven": (verdict.reason or Unproven.SIBLING_UNPROVEN).value,
                    "detail": verdict.detail,
                }
                for verdict in self.verdicts
            ],
        }
        if self.admitted is not None:
            document["plan_shape"] = self.admitted.shape.value
        if self.failed is not None:
            # Recorded, never written: the failed unit stays `failed` (FEAT-011 class b).
            document["failed_unit"] = (
                {
                    "unit_key": self.failed.unit.get("unit_key"),
                    "evidence": self.failed.record,
                    "evidence_digest": self.failed.digest,
                }
                if self.failed.record is not None
                else {
                    "unit_key": self.failed.unit.get("unit_key"),
                    "unproven": (self.failed.reason or Unproven.UNIT_SHAPE).value,
                    "detail": self.failed.detail,
                }
            )
        if self.reason is not None:
            document["unproven"] = self.reason.value
        if self.proven:
            plan_record = self.record()
            document["plan_evidence"] = plan_record
            document["plan_evidence_digest"] = evidence_digest(plan_record)
        return document


def _key(partition: str, record: str) -> dict[str, dict[str, str]]:
    return {"partition_key": _s(partition), "record_key": _s(record)}


def transaction(table: str, decision: _PlanDecision, at: str) -> list[dict[str, object]]:
    """Build the one conditioned transaction that dispositions a proven plan."""
    if not decision.proven:
        raise ValueError("only a proven plan is dispositioned.")
    plan, meta = decision.plan, decision.plan.meta
    members: list[dict[str, object]] = []
    for verdict in decision.verdicts:
        unit_key = str(verdict.unit.get("unit_key"))
        members.append(
            {
                "Update": {
                    "TableName": table,
                    "Key": _key(plan.plan_key, unit_record_key(unit_key)),
                    "UpdateExpression": (
                        "SET #status = :superseded, terminal_at = :at, "
                        f"{UNIT_SUPERSESSION_EVIDENCE_FIELD} = :digest, supersession_rule = :rule"
                    ),
                    "ConditionExpression": (
                        "#status = :queued AND admission = :admitted AND "
                        "attribute_not_exists(attempt_references) AND "
                        "attribute_not_exists(unit_reservation_count) AND "
                        "attribute_not_exists(current_attempt_id) AND "
                        "attribute_not_exists(terminal_at)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":superseded": _s(PlanUnitStatus.SUPERSEDED.value),
                        ":queued": _s(PlanUnitStatus.QUEUED.value),
                        ":admitted": _s(PlanUnitAdmission.ADMITTED.value),
                        ":at": _s(at),
                        ":digest": _s(verdict.digest),
                        ":rule": _s(str(cast("dict[str, object]", verdict.record)["rule"])),
                    },
                }
            }
        )
    admitted = cast("OrphanPlan", decision.admitted)
    pinned = admitted.shape is not PlanShape.NO_RESERVATION
    for unit_key in sorted(plan.units):
        unit = plan.units[unit_key]
        if unit.get("status") == PlanUnitStatus.ATTESTED.value:
            members.append(
                _spent_check(table, plan.plan_key, unit_key, unit, PlanUnitStatus.ATTESTED)
                if pinned
                else {
                    "ConditionCheck": {
                        "TableName": table,
                        "Key": _key(plan.plan_key, unit_record_key(unit_key)),
                        "ConditionExpression": "#status = :attested",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":attested": _s(PlanUnitStatus.ATTESTED.value)
                        },
                    }
                }
            )
    if admitted.failed is not None:
        # The failed unit is pinned, never rewritten: it stays `failed` with its reservations.
        failed_key = str(admitted.failed.get("unit_key"))
        members.append(
            _spent_check(table, plan.plan_key, failed_key, admitted.failed, PlanUnitStatus.FAILED)
        )
    # Gate 1 conditions on the counter's absence. Gate 2 and the failed variant pin it to the
    # exact sum read: a Begin that lands after the read adds one to it, so this transaction is
    # refused as `refused_state_moved` rather than superseding a plan with a fresh reservation.
    reservation_clause = (
        "plan_reservation_count = :reservations"
        if pinned
        else "attribute_not_exists(plan_reservation_count)"
    )
    meta_values: dict[str, object] = {
        ":superseded": _s(PlanStatus.SUPERSEDED.value),
        ":planned": _s(PlanStatus.PLANNED.value),
        ":generation": _n(cast("int", meta["generation"])),
        ":unit_count": _n(cast("int", meta["unit_count"])),
        ":attested": _n(cast("int", meta["attested_count"])),
        # 0, or exactly the one accounted failed unit (variant b).
        ":failed": _n(1 if admitted.failed is not None else 0),
        ":at": _s(at),
        ":digest": _s(evidence_digest(decision.record())),
    }
    if pinned:
        meta_values[":reservations"] = _n(cast("int", admitted.reservations))
    members.append(
        {
            "Update": {
                "TableName": table,
                "Key": _key(plan.plan_key, META_RECORD_KEY),
                "UpdateExpression": (
                    "SET #status = :superseded, terminal_at = :at, "
                    f"{UNIT_SUPERSESSION_EVIDENCE_FIELD} = :digest"
                ),
                "ConditionExpression": (
                    "#status = :planned AND generation = :generation AND "
                    "unit_count = :unit_count AND admitted_count = :unit_count AND "
                    "attested_count = :attested AND failed_count = :failed AND "
                    f"{reservation_clause} AND "
                    "attribute_not_exists(terminal_at)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": meta_values,
            }
        }
    )
    intent_key = intent_partition_key(str(meta["plan_digest"]), str(meta["origin_intent_id"]))
    if pinned and plan.intent is None:
        raise ValueError("gate 2 and the failed-unit variant need a present admitted intent.")
    # Only gate 1 may condition on an absent intent. Gate 2 and the variant pin a present,
    # admitted intent: `#state = :admitted` is false on an absent item, so it is refused.
    members.append(
        {
            "ConditionCheck": {
                "TableName": table,
                "Key": _key(intent_key, META_RECORD_KEY),
                "ConditionExpression": "attribute_not_exists(partition_key)",
            }
        }
        if plan.intent is None
        else {
            "ConditionCheck": {
                "TableName": table,
                "Key": _key(intent_key, META_RECORD_KEY),
                "ConditionExpression": "#state = :admitted",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":admitted": _s(IntentState.ADMITTED.value)},
            }
        }
    )
    members.append(_owner_member(table, decision))
    return members


def _spent_check(  # noqa: PLR0913, PLR0917 -- one unit's key, its read and its status
    table: str, plan_key: str, unit_key: str, unit: Plain, status: PlanUnitStatus
) -> dict[str, object]:
    """Pin one spent unit's status and exact `unit_reservation_count` (gate 2, variant b).

    Together with the `META` `plan_reservation_count = :reservations` condition this binds the
    whole gate-2 sum to the state read. A failed unit is also pinned unbound and admitted, so a
    redrive (`ClaimUnit` with `redrive`, which moves it to `writing`) refuses this transaction.
    The gate admits only a present count from 1 to the per-unit cap on every spent unit; anything
    else here is a programming error, never pinned as an absent or above-cap counter.
    """
    count = unit_reservations(unit)
    if count is None or count < 1:
        raise ValueError(
            "a spent unit is pinned only on a present reservation count from 1 to the unit cap."
        )
    condition = "#status = :status AND unit_reservation_count = :count"
    values: dict[str, object] = {":status": _s(status.value), ":count": _n(count)}
    if status is PlanUnitStatus.FAILED:
        condition += " AND admission = :admitted AND attribute_not_exists(current_attempt_id)"
        values[":admitted"] = _s(PlanUnitAdmission.ADMITTED.value)
    return {
        "ConditionCheck": {
            "TableName": table,
            "Key": _key(plan_key, unit_record_key(unit_key)),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": values,
        }
    }


def _owner_member(table: str, decision: _PlanDecision) -> dict[str, object]:
    """Clear the successor owner's active fields when they name this plan; else pin its read."""
    owner_key = owner_partition_key(str(decision.plan.meta["plan_digest"]))
    key = _key(owner_key, SUCCESSOR_OWNER_RECORD_KEY)
    owner = decision.owner
    if owner is None:
        return {
            "ConditionCheck": {
                "TableName": table,
                "Key": key,
                "ConditionExpression": "attribute_not_exists(partition_key)",
            }
        }
    revision = cast("int", owner["revision"])
    if owner.get("active_plan_key") != decision.plan.plan_key:
        return {
            "ConditionCheck": {
                "TableName": table,
                "Key": key,
                "ConditionExpression": "revision = :revision",
                "ExpressionAttributeValues": {":revision": _n(revision)},
            }
        }
    return {
        "Update": {
            "TableName": table,
            "Key": key,
            "UpdateExpression": (
                "SET revision = :new_revision REMOVE active_plan_key, active_intent_id"
            ),
            "ConditionExpression": "revision = :revision AND active_plan_key = :plan_key",
            "ExpressionAttributeValues": {
                ":revision": _n(revision),
                ":new_revision": _n(revision + 1),
                ":plan_key": _s(decision.plan.plan_key),
            },
        }
    }


def _decide(
    assessor: _Assessor, plan: PlanRows, accounting: FailedUnitAccounting | None = None
) -> _PlanDecision | None:
    """Assess one plan; None when it is already terminal.

    Every `planned` plan is reported, an orphan or not, because each one blocks `verify-drain`;
    only a proven orphan plan is dispositioned. `accounting` admits the failed-unit variant for
    the one plan it names, and only when it names that plan's failed unit.
    """
    owner = assessor.scanned.owners.get(owner_partition_key(str(plan.meta["plan_digest"])))
    named = accounting if accounting is not None and accounting.plan_key == plan.plan_key else None
    admitted, reason = orphan_plan(plan, failed_unit_accounted=named is not None)
    if reason is Unproven.NOT_PLANNED:
        return None
    if (
        admitted is not None
        and named is not None
        and (admitted.failed is None or admitted.failed.get("unit_key") != named.unit_key)
    ):
        admitted, reason = None, Unproven.FAILED_UNIT_UNACCOUNTED
    if admitted is None:
        queued = tuple(
            unit
            for _, unit in sorted(plan.units.items())
            if unit.get("status") != PlanUnitStatus.ATTESTED.value
        )
        verdicts = tuple(_UnitVerdict(unit, None, reason, {}) for unit in queued)
        return _PlanDecision(plan, verdicts, reason, owner)
    verdicts = tuple(assessor.unit(plan, unit) for unit in admitted.orphans)
    # Variant b: the failed unit is proven by the same per-object rules (for STOP 1, its three
    # `current` objects are projected past: fences 81 > 77, 90 > 83 and 101 > 79), but it is
    # only recorded and condition-checked, never rewritten.
    failed = None if admitted.failed is None else assessor.unit(plan, admitted.failed)
    blocked = any(verdict.record is None for verdict in verdicts) or (
        failed is not None and failed.record is None
    )
    return _PlanDecision(
        plan,
        verdicts,
        Unproven.SIBLING_UNPROVEN if blocked else None,
        owner,
        admitted,
        failed,
        named,
    )


# --- The run --------------------------------------------------------------------------------------


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _cancellation_codes(error: ClientError) -> list[str]:
    reasons = cast("list[dict[str, object]]", error.response.get("CancellationReasons") or [])
    return [str(reason.get("Code", "")) for reason in reasons]


def _state_moved(error: ClientError) -> bool:
    """Only a cancellation whose every failing member is a condition check: the state moved.

    Any other cancellation reason (a conflict, throttling, a validation problem) or any other
    client error is not evidence that the plan moved, so it is never labelled as such.
    """
    if _error_code(error) != _TRANSACTION_CANCELLED:
        return False
    failing = {code for code in _cancellation_codes(error) if code != "None"}
    return failing == {_CONDITION_FAILED}


def disposition(  # noqa: PLR0913 -- the clients, the targets and three keyword-only facts
    clients: Clients,
    targets: Targets,
    *,
    apply: bool,
    now: datetime,
    failed_unit: FailedUnitAccounting | None = None,
) -> tuple[dict[str, object], Callable[[], dict[str, object]] | None]:
    """Assess every orphan plan; return the evidence document and, with `apply`, the commit.

    The evidence is complete before any write; the commit callable is None for a dry run or when
    a semantic-write queue is not empty. The commit re-reads both queue depths before each plan's
    transaction and stops at the first non-empty read. Those reads are a diagnostic, not a
    fence: nothing binds an enqueue to the transaction (TD-88). The fences are each
    transaction's conditions on the exact plan, unit, intent and owner state read, and the
    writer's own begin conditions, which refuse a superseded unit.

    A transaction cancelled only by its condition checks is `refused_state_moved` and the run
    continues. Any other client error, a cancellation for another reason included, is `failed`
    and stops the run (`transaction_failed`) with its error code and the plans not attempted.
    Every stop leaves the run incomplete, so the command exits non-zero.
    """
    scanned = _scan(clients, targets.table)
    assessor = _Assessor(clients, targets, scanned)
    decisions = [
        decision
        for plan in sorted(scanned.plans.values(), key=lambda rows: rows.plan_key)
        if (decision := _decide(assessor, plan, failed_unit)) is not None
    ]
    depth = {
        "semantic_write_queue": _queue_depth(clients, targets.queue_url),
        "semantic_write_dlq": _queue_depth(clients, targets.dlq_url),
    }
    proven = [decision for decision in decisions if decision.proven]
    units = [verdict for decision in decisions for verdict in decision.verdicts]
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "generated_at": now.isoformat(),
        "table": targets.table,
        "pages_read": scanned.pages,
        "queue_depth": depth,
        "counts": {
            "plans_scanned": len(scanned.plans),
            "planned_plans": len(decisions),
            "plans_proven": len(proven),
            "plans_left_untouched": len(decisions) - len(proven),
            "orphan_units": len(units),
            "units_proven": sum(verdict.record is not None for verdict in units),
            "units_unproven": sum(verdict.record is None for verdict in units),
        },
        "complete": len(proven) == len(decisions),
        "plans": [decision.document() for decision in decisions],
    }
    if failed_unit is not None:
        # A named plan that no decision admitted under the variant is reported, never ignored.
        admitted = any(
            decision.accounting is failed_unit and decision.proven for decision in decisions
        )
        document["failed_unit_accounting"] = {
            "plan_key": failed_unit.plan_key,
            **failed_unit.record,
            "plan_proven": admitted,
        }
        document["complete"] = bool(document["complete"]) and admitted
    if not apply:
        return document, None
    if any(depth.values()):
        document["apply_refused"] = "semantic_write_queue_not_empty"
        return document, None
    at = instant(now)

    def commit() -> dict[str, object]:
        outcomes: dict[str, str] = {}
        stop: dict[str, object] = {}
        for index, decision in enumerate(proven):
            # Re-read both queues before every plan: a message that appeared after the last read
            # stops the run here, so no later plan is dispositioned under a stale "no message".
            current = {
                "semantic_write_queue": _queue_depth(clients, targets.queue_url),
                "semantic_write_dlq": _queue_depth(clients, targets.dlq_url),
            }
            if any(current.values()):
                stop = {
                    "stopped": "semantic_write_queue_not_empty",
                    "queue_depth_at_stop": current,
                    "not_attempted": [item.plan.plan_key for item in proven[index:]],
                }
                break
            try:
                clients.dynamodb.transact_write_items(
                    TransactItems=transaction(targets.table, decision, at)
                )
            # error-propagation: operator CLI; the stop record names error_class and codes, exit 1
            except ClientError as error:
                if _state_moved(error):
                    outcomes[decision.plan.plan_key] = "refused_state_moved"
                    continue
                # Not a condition refusal: stop here rather than repeat it on every later plan.
                outcomes[decision.plan.plan_key] = "failed"
                stop = {
                    "stopped": "transaction_failed",
                    "failed_plan": decision.plan.plan_key,
                    "error_class": type(error).__name__,
                    "error_code": _error_code(error),
                    "cancellation_codes": _cancellation_codes(error),
                    "not_attempted": [item.plan.plan_key for item in proven[index + 1 :]],
                }
                break
            outcomes[decision.plan.plan_key] = "superseded"
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "terminal_at": at,
            "outcomes": outcomes,
            "committed": sum(outcome == "superseded" for outcome in outcomes.values()),
            **stop,
            "complete": bool(document["complete"])
            and not stop
            and all(outcome == "superseded" for outcome in outcomes.values()),
        }

    return document, commit


# --- The command ----------------------------------------------------------------------------------


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    normalized = _non_blank(value)
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value)
    if _ACCOUNT.match(normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    """Every target-affecting input is required; only `--apply` mutates."""
    parser = argparse.ArgumentParser(
        prog="python -m adapters.jobs.supersede_orphan_units",
        description="Disposition orphan plan units as superseded, with per-unit evidence (TD-50).",
    )
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--env", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--evidence-dir", required=True, type=_non_blank)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the proven dispositions; without it nothing in AWS is written",
    )
    variant = parser.add_argument_group(
        "failed-unit variant (FEAT-011 class b): all three, or none",
    )
    variant.add_argument(
        "--failed-unit-plan",
        type=_non_blank,
        help="the one WRITE_PLAN# key whose single failed unit is accounted for",
    )
    variant.add_argument(
        "--dlq-archive",
        type=_non_blank,
        help="the archived dead-letter message of that failed unit (JSON, with its body)",
    )
    variant.add_argument(
        "--dlq-deletion-record",
        type=_non_blank,
        help="the record of the approved deletion, naming the archived message id",
    )
    return parser


def _variant(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, ...] | None:
    named = (args.failed_unit_plan, args.dlq_archive, args.dlq_deletion_record)
    if all(value is None for value in named):
        return None
    if any(value is None for value in named):
        parser.error("--failed-unit-plan, --dlq-archive and --dlq-deletion-record go together")
    if not str(args.failed_unit_plan).startswith(WRITE_PLAN_PREFIX):
        parser.error("--failed-unit-plan must be a WRITE_PLAN# plan key")
    return cast("tuple[str, ...]", named)


def _queue_url(arn: str) -> str:
    _, _, _, region, account, name = arn.split(":", 5)
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def _targets(session: Any, environment: str) -> Targets:
    ssm = session.client("ssm")
    resources = resolve_named_semantic_resources(environment, session.client("cloudformation"), ssm)

    def arn(key: str) -> str:
        return str(ssm.get_parameter(Name=parameter_path(environment, key))["Parameter"]["Value"])

    return Targets(
        table=resources["publications"],
        bucket=resources["bucket"],
        queue_url=_queue_url(arn(QUEUE_KEY)),
        dlq_url=_queue_url(arn(DLQ_KEY)),
    )


def _write(directory: Path, name: str, document: Mapping[str, object]) -> Path:
    """Create the file exclusively: an existing proof is never replaced (`FileExistsError`)."""
    path = directory / name
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True, indent=1) + "\n")
    return path


def _print(document: Mapping[str, object]) -> None:
    print(json.dumps(document, sort_keys=True, indent=1))  # noqa: T201


def main(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915 -- one exit per fail-closed stage
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> int:
    """Prove the account, resolve the targets, assess, record, and commit only with --apply."""
    parser = build_parser()
    args = parser.parse_args(argv)
    variant = _variant(parser, args)
    failed_unit: FailedUnitAccounting | None = None
    if variant is not None:
        plan_key, archive, deletion = variant
        try:
            failed_unit = failed_unit_accounting(plan_key, Path(archive), Path(deletion))
        except (OSError, ValueError, TypeError) as error:
            # Named stop before any AWS client: an unaccounted dead letter admits nothing.
            _print({"stage": "dead_letter_accounting", "error_class": type(error).__name__})
            return 1
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    # error-propagation: operator CLI; the printed identity-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "identity", "error_class": type(error).__name__})
        return 1
    if account != args.expected_account:
        _print({"stage": "identity", "error_class": "UnexpectedAccount"})
        return 1
    directory = Path(args.evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    now = clock()
    # The second-resolution stamp orders the files; the random run id keeps two invocations in
    # one second apart, and `_write` refuses an existing path either way.
    stamp = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{run_id()}"
    try:
        targets = _targets(session, args.env)
        s3 = session.client("s3")

        def read_object(key: str) -> bytes:
            return cast("bytes", s3.get_object(Bucket=targets.bucket, Key=key)["Body"].read())

        clients = Clients(session.client("dynamodb"), session.client("sqs"), read_object)
        document, commit = disposition(
            clients, targets, apply=args.apply, now=now, failed_unit=failed_unit
        )
    # error-propagation: operator CLI; the printed assess-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "assess", "error_class": type(error).__name__})
        return 1
    try:
        evidence = _write(directory, f"orphan-supersession-{stamp}-evidence.json", document)
    except OSError as error:
        # No proof on disk means no commit.
        _print({"stage": "evidence", "error_class": type(error).__name__})
        return 1
    summary: dict[str, object] = {
        "mode": document["mode"],
        "evidence": str(evidence),
        "counts": document["counts"],
        "queue_depth": document["queue_depth"],
    }
    if "apply_refused" in document:
        summary["apply_refused"] = document["apply_refused"]
        _print(summary)
        return 1
    complete = bool(document["complete"])
    if commit is not None:
        # Reserve the outcome file before any mutation: a run that stops after it is still named
        # on disk, beside its evidence, as pending.
        try:
            reserved = _write(
                directory,
                f"orphan-supersession-{stamp}-outcome.json",
                {"schema_version": SCHEMA_VERSION, "status": "pending", "evidence": str(evidence)},
            )
        except OSError as error:
            _print({**summary, "stage": "outcome", "error_class": type(error).__name__})
            return 1
        summary["outcome"] = str(reserved)
        try:
            outcome = commit()
        # error-propagation: operator CLI; the printed apply-stage error_class and exit 1
        except Exception as error:
            _print({**summary, "stage": "apply", "error_class": type(error).__name__})
            return 1
        try:
            reserved.write_text(
                json.dumps({**outcome, "evidence": str(evidence)}, sort_keys=True, indent=1) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            # The transactions committed: print the outcome itself so it is not lost.
            _print({**summary, "stage": "outcome", "error_class": type(error).__name__, **outcome})
            return 1
        summary["committed"] = outcome["committed"]
        for name in ("stopped", "failed_plan", "error_code"):
            if name in outcome:
                summary[name] = outcome[name]
        complete = bool(outcome["complete"])
    _print(summary)
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
