# SPDX-License-Identifier: MIT-0
r"""``adapters.jobs.supersede_orphan_units.requeue``: resend one orphan plan's unit messages.

FEAT-011 class (c). Plan `4f186631…` is `planned` with two `queued`, admitted, never-begun
source units and no message behind them, like a TD-50 orphan. It is not superseded: it is the
only copy of source day 2026-08-11 for fixture vendor group `fictional-fixture-vendor-group`, and
no newer attested append covers that day, so superseding it would lose data. The orchestrator's
decision is a reviewed requeue of exactly that plan's units to the semantic-write FIFO, so the
live TableWriter (the legacy unit body; GRAPH is in SHADOW) claims, writes and attests them.

- **The message is not hand-built.** `domain.operations.write_plan.unit_message` is the one
  constructor of the six-field §4 body, its `MessageGroupId` (the unit key) and its
  `MessageDeduplicationId` (a digest of the plan and unit keys), and
  `plan_intent_reconciler.SqsWriteUnitSender` is the one sender, the same pair the reconciler
  uses. TableWriter's `_legacy_delivery` parses exactly that body. The deduplication id is a
  pure function of (plan, unit), so a resend inside SQS's deduplication window collapses.
- **Dry run by default.** It reads the plan partition, its origin intent, its successor owner
  and the two queue depths, all strongly consistent, and writes the evidence file: one member
  per unit with the exact body, its SHA-256, the group and deduplication ids, and the state
  read. It sends nothing.
- **`--apply`** writes the same evidence first, then reserves the outcome file, then sends one
  message per unit in unit-key order, only when every precondition holds. Immediately before
  every send, the first included, it re-reads the plan, intent, owner and both queue depths
  (after the evidence and the pending outcome exist). Before the first send it repeats every
  assessment precondition; before a later one it rechecks authority, the message identity and
  every unit not yet sent, and requires an empty DLQ and a queue holding no more than the
  messages this run sent. A re-read is not a fence (TD-88): it narrows the window, it does not
  close it.
- **Preconditions** (each refusal is named, and a refused run sends nothing): the `META` is
  `planned`, not terminal, fully admitted, with no attested or failed unit and no spent
  reservation; every unit is `queued`, admitted, never begun and at the plan's generation; the
  origin intent is `admitted` at the plan's generation; the successor owner's active pair names
  this plan and intent; and the semantic-write queue and DLQ are empty. These mirror
  TableWriter's `_classify_authority`: a message that fails them is acknowledged away
  (`plan_replaced`, `intent_replaced`, `stale_generation`, `plan_<status>`), which would be a
  silent no-op, so the requeue refuses instead.
- A send failure, a failed re-read, or a precondition that moved before a send stops the run
  (`send_failed`, `refresh_failed` or `state_moved`) and records the units sent and the units
  not attempted in the outcome file. The exit code is 0 only for a requeueable
  dry run or a complete apply.

This is a producer event: TD-88's residual applies to any `--apply` (its own acceptance, with
producers stopped, or the fence).

Usage:
    uv run python -m adapters.jobs.supersede_orphan_units.requeue \
        --profile <PROFILE> --env <ENV> --region <REGION> --expected-account <ACCOUNT_ID> \
        --evidence-dir <DIR> --plan-id <WRITE_PLAN#...> [--apply]
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
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import boto3

from adapters.jobs.plan_intent_reconciler import SqsWriteUnitSender
from adapters.jobs.supersede_orphan_units.__main__ import (
    SCHEMA_VERSION,
    Clients,
    Targets,
    _account,  # pyright: ignore[reportPrivateUsage] -- the disposition's own CLI validators
    _non_blank,  # pyright: ignore[reportPrivateUsage] -- the disposition's own CLI validators
    _plain,  # pyright: ignore[reportPrivateUsage] -- the write-plan families' item decoder
    _print,  # pyright: ignore[reportPrivateUsage] -- one JSON summary on stdout
    _profile,  # pyright: ignore[reportPrivateUsage] -- the disposition's own CLI validators
    _queue_depth,  # pyright: ignore[reportPrivateUsage] -- the three-counter queue depth
    _targets,  # pyright: ignore[reportPrivateUsage] -- the same typed target resolution
    _write,  # pyright: ignore[reportPrivateUsage] -- exclusive-create evidence files
)
from adapters.jobs.supersede_orphan_units.evidence import never_begun
from domain.operations.write_plan import (
    META_RECORD_KEY,
    SUCCESSOR_OWNER_RECORD_KEY,
    UNIT_RECORD_PREFIX,
    IntentState,
    PlanStatus,
    WriteUnitMessageRequest,
    intent_partition_key,
    owner_partition_key,
    unit_message,
)

__all__ = ["Refusal", "build_parser", "main", "no_object_reads", "requeue"]

_PLAN_KEY: Final = re.compile(r"\AWRITE_PLAN#[0-9a-f]{64}#[0-9a-f]{64}\Z")
_MESSAGE_IDENTITY: Final = ("plan_digest", "origin_intent_id", "generation")
"""The `META` fields each message body was built from; a send needs them unchanged."""

type Plain = Mapping[str, object]


class Refusal(StrEnum):
    """Why a requeue sends nothing (each mirrors a TableWriter classification or a fence)."""

    PLAN_ABSENT = "plan_meta_absent"
    NOT_PLANNED = "plan_not_planned"
    PLAN_SHAPE = "plan_counters_not_queued_never_begun"
    UNIT_SHAPE = "unit_not_queued_never_begun"
    INTENT_NOT_ADMITTED = "origin_intent_not_admitted_at_the_plan_generation"
    OWNER_NOT_ACTIVE = "successor_owner_does_not_name_the_plan"
    QUEUE_NOT_EMPTY = "semantic_write_queue_not_empty"
    AUTHORITY_MOVED = "plan_message_identity_moved_since_the_evidence"


@dataclass(frozen=True, slots=True)
class _Read:
    meta: Plain | None
    units: Mapping[str, Plain]
    intent: Plain | None
    owner: Plain | None


def no_object_reads(key: str) -> bytes:
    """Refuse every S3 read: the requeue reads no immutable object."""
    raise PermissionError(f"the requeue reads no immutable object ({len(key)}-byte key).")


def _get(clients: Clients, table: str, key: tuple[str, str]) -> Plain | None:
    partition, record = key
    found = clients.dynamodb.get_item(
        TableName=table,
        Key={"partition_key": {"S": partition}, "record_key": {"S": record}},
        ConsistentRead=True,
    ).get("Item")
    return None if found is None else _plain(found)


def _read(clients: Clients, table: str, plan_key: str) -> _Read:
    """Strongly read the plan partition, then its origin intent and its successor owner."""
    items: list[Plain] = []
    start: Mapping[str, object] | None = None
    while True:
        request: dict[str, object] = {
            "TableName": table,
            "ConsistentRead": True,
            "KeyConditionExpression": "#pk = :pk",
            "ExpressionAttributeNames": {"#pk": "partition_key"},
            "ExpressionAttributeValues": {":pk": {"S": plan_key}},
        }
        if start is not None:
            request["ExclusiveStartKey"] = start
        response = clients.dynamodb.query(**request)
        items.extend(_plain(item) for item in response.get("Items", []))
        start = response.get("LastEvaluatedKey")
        if start is None:
            break
    meta = next((item for item in items if item.get("record_key") == META_RECORD_KEY), None)
    units = {
        str(item.get("unit_key", "")): item
        for item in items
        if str(item.get("record_key", "")).startswith(UNIT_RECORD_PREFIX)
    }
    if meta is None:
        return _Read(None, units, None, None)
    digest = str(meta.get("plan_digest", ""))
    intent_key = intent_partition_key(digest, str(meta.get("origin_intent_id", "")))
    intent = _get(clients, table, (intent_key, META_RECORD_KEY))
    owner = _get(clients, table, (owner_partition_key(digest), SUCCESSOR_OWNER_RECORD_KEY))
    return _Read(meta, units, intent, owner)


def _int(item: Plain, name: str) -> int | None:
    value = item.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _authority_refusals(read: _Read, plan_key: str) -> list[Refusal]:
    """Return the plan-level refusals; these must be clear before every send."""
    meta = read.meta
    if meta is None:
        return [Refusal.PLAN_ABSENT]
    refusals: list[Refusal] = []
    if meta.get("status") != PlanStatus.PLANNED.value or "terminal_at" in meta:
        refusals.append(Refusal.NOT_PLANNED)
    generation = _int(meta, "generation")
    intent = read.intent
    if (
        intent is None
        or generation is None
        or intent.get("state") != IntentState.ADMITTED.value
        or _int(intent, "claimed_generation") != generation
    ):
        refusals.append(Refusal.INTENT_NOT_ADMITTED)
    owner = read.owner
    if (
        owner is None
        or owner.get("active_plan_key") != plan_key
        or owner.get("active_intent_id") != meta.get("origin_intent_id")
    ):
        refusals.append(Refusal.OWNER_NOT_ACTIVE)
    return refusals


def _plan_shape_refusals(read: _Read) -> list[Refusal]:
    """Return the first-send refusals: the whole plan must be queued and never begun."""
    meta = cast("Plain", read.meta)
    count = _int(meta, "unit_count")
    if (
        count is None
        or count < 1
        or count != len(read.units)
        or _int(meta, "admitted_count") != count
        or _int(meta, "attested_count") != 0
        or _int(meta, "failed_count") != 0
        or "plan_reservation_count" in meta
    ):
        return [Refusal.PLAN_SHAPE]
    return []


def _unit_ready(read: _Read, unit_key: str) -> bool:
    unit = read.units.get(unit_key)
    meta = read.meta
    return (
        unit is not None
        and meta is not None
        and never_begun(unit)
        and _int(unit, "owner_generation") == _int(meta, "generation")
    )


@dataclass(slots=True)
class _RecordingSqs:
    """Pass `send_message` through and keep its response, so the outcome names each MessageId."""

    client: Any
    responses: list[Mapping[str, object]] = field(default_factory=list[Mapping[str, object]])

    def send_message(self, **request: object) -> Mapping[str, object]:
        response = cast("Mapping[str, object]", self.client.send_message(**request))
        self.responses.append(response)
        return response


def _member(meta: Plain, plan_key: str, unit: Plain) -> dict[str, object]:
    unit_key = str(unit.get("unit_key"))
    message = unit_message(
        WriteUnitMessageRequest(
            plan_key=plan_key,
            plan_digest=str(meta.get("plan_digest")),
            origin_intent_id=str(meta.get("origin_intent_id")),
            generation=cast("int", meta.get("generation")),
            unit_key=unit_key,
        )
    )
    return {
        "unit_key": unit_key,
        "body": message.body,
        "body_sha256": hashlib.sha256(message.body.encode("utf-8")).hexdigest(),
        "group_id": message.group_id,
        "deduplication_id": message.deduplication_id,
        "unit_state": {
            name: unit.get(name)
            for name in ("status", "admission", "owner_generation", "flow_id", "created_at")
        },
    }


def _recheck(  # noqa: PLR0913 -- the clients, the targets, the plan and what was sent
    clients: Clients,
    targets: Targets,
    plan_key: str,
    *,
    identity: Mapping[str, object],
    sent_count: int,
    remaining: list[str],
) -> tuple[list[Refusal], dict[str, int]]:
    """Re-read authority, every unit not yet sent and both queues, right before a send.

    The evidence and the messages were built from the assessment read; this read is taken after
    the evidence file and the pending outcome exist. Before the first send it repeats every
    assessment precondition. After a send the writer may claim the sent units (so the plan
    counters move), and the queue holds at most the messages this run sent: the DLQ must stay
    empty and the queue may not exceed `sent_count`. A failure here propagates to the caller,
    which records it as the named `refresh_failed` stop.
    """
    fresh = _read(clients, targets.table, plan_key)
    moved = _authority_refusals(fresh, plan_key)
    if fresh.meta is not None:
        if any(fresh.meta.get(name) != value for name, value in identity.items()):
            moved.append(Refusal.AUTHORITY_MOVED)
        if not sent_count:
            moved += _plan_shape_refusals(fresh)
        if any(not _unit_ready(fresh, key) for key in remaining):
            moved.append(Refusal.UNIT_SHAPE)
    depth = {
        "semantic_write_queue": _queue_depth(clients, targets.queue_url),
        "semantic_write_dlq": _queue_depth(clients, targets.dlq_url),
    }
    if depth["semantic_write_queue"] > sent_count or depth["semantic_write_dlq"]:
        moved.append(Refusal.QUEUE_NOT_EMPTY)
    return moved, depth


def requeue(  # noqa: PLR0913 -- the clients, the targets and three keyword-only facts
    clients: Clients,
    targets: Targets,
    *,
    plan_key: str,
    apply: bool,
    now: datetime,
) -> tuple[dict[str, object], Callable[[], dict[str, object]] | None]:
    """Assess one plan; return the evidence and, when `apply` and requeueable, the send."""
    table, queue_url, dlq_url = targets.table, targets.queue_url, targets.dlq_url
    read = _read(clients, table, plan_key)
    depth = {
        "semantic_write_queue": _queue_depth(clients, queue_url),
        "semantic_write_dlq": _queue_depth(clients, dlq_url),
    }
    refusals = _authority_refusals(read, plan_key)
    if read.meta is not None:
        refusals += _plan_shape_refusals(read)
        if not read.units or any(not _unit_ready(read, key) for key in read.units):
            refusals.append(Refusal.UNIT_SHAPE)
    if any(depth.values()):
        refusals.append(Refusal.QUEUE_NOT_EMPTY)
    meta = read.meta
    members = (
        []
        if meta is None or Refusal.UNIT_SHAPE in refusals
        else [_member(meta, plan_key, unit) for _, unit in sorted(read.units.items())]
    )
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "generated_at": now.isoformat(),
        "table": table,
        "plan_key": plan_key,
        "queue_depth": depth,
        "state_read": {
            "meta": None
            if meta is None
            else {
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
                    "plan_reservation_count",
                    "created_at",
                )
            },
            "intent": None
            if read.intent is None
            else {name: read.intent.get(name) for name in ("state", "claimed_generation")},
            "owner": None
            if read.owner is None
            else {
                name: read.owner.get(name)
                for name in ("revision", "active_plan_key", "active_intent_id")
            },
        },
        "refusals": sorted({refusal.value for refusal in refusals}),
        "requeueable": not refusals,
        "units": members,
    }
    if not apply or refusals:
        return document, None
    identity = {name: cast("Plain", meta).get(name) for name in _MESSAGE_IDENTITY}

    def send() -> dict[str, object]:
        recording = _RecordingSqs(clients.sqs)
        sender = SqsWriteUnitSender(queue_url, recording)
        sent: list[dict[str, object]] = []
        stop: dict[str, object] = {}
        for index, member in enumerate(members):
            unit_key = str(member["unit_key"])
            remaining = [str(m["unit_key"]) for m in members[index:]]
            try:
                moved, depth = _recheck(
                    clients,
                    targets,
                    plan_key,
                    identity=identity,
                    sent_count=len(sent),
                    remaining=remaining,
                )
            # error-propagation: operator CLI; the refresh_failed stop names error_class, exit 1
            except Exception as error:
                # A failed re-read is a named stop that keeps what was already sent.
                stop = {
                    "stopped": "refresh_failed",
                    "error_class": type(error).__name__,
                    "not_attempted": remaining,
                }
                break
            if moved:
                stop = {
                    "stopped": "state_moved",
                    "refusals": sorted({refusal.value for refusal in moved}),
                    "queue_depth": depth,
                    "not_attempted": remaining,
                }
                break
            try:
                sender.send(
                    str(member["body"]), str(member["group_id"]), str(member["deduplication_id"])
                )
            # error-propagation: operator CLI; the send_failed stop record names error_class, exit 1
            except Exception as error:
                stop = {
                    "stopped": "send_failed",
                    "failed_unit": unit_key,
                    "error_class": type(error).__name__,
                    "not_attempted": [str(m["unit_key"]) for m in members[index + 1 :]],
                }
                break
            response = recording.responses[-1]
            sent.append(
                {
                    "unit_key": unit_key,
                    "deduplication_id": member["deduplication_id"],
                    "message_id": response.get("MessageId"),
                    "sequence_number": response.get("SequenceNumber"),
                    "queue_depth_before": depth,
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "plan_key": plan_key,
            "sent": sent,
            **stop,
            "complete": not stop and len(sent) == len(members),
        }

    return document, send


# --- The command ----------------------------------------------------------------------------------


def _plan_id(value: str) -> str:
    normalized = _non_blank(value)
    if _PLAN_KEY.match(normalized) is None:
        raise argparse.ArgumentTypeError("--plan-id must be a full WRITE_PLAN#<digest>#<id> key")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    """Every target-affecting input is required; only `--apply` sends."""
    parser = argparse.ArgumentParser(
        prog="python -m adapters.jobs.supersede_orphan_units.requeue",
        description="Resend one orphan plan's queued unit messages, with evidence (FEAT-011).",
    )
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--env", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--evidence-dir", required=True, type=_non_blank)
    parser.add_argument("--plan-id", required=True, type=_plan_id)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="send the unit messages; without it nothing in AWS is written",
    )
    return parser


def main(  # noqa: PLR0911, PLR0913 -- one exit per fail-closed stage; injected clock and run id
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> int:
    """Prove the account, resolve the targets, assess, record, and send only with --apply."""
    args = build_parser().parse_args(argv)
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
    stamp = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{run_id()}"
    try:
        targets = _targets(session, args.env)
        clients = Clients(session.client("dynamodb"), session.client("sqs"), no_object_reads)
        document, send = requeue(clients, targets, plan_key=args.plan_id, apply=args.apply, now=now)
    # error-propagation: operator CLI; the printed assess-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "assess", "error_class": type(error).__name__})
        return 1
    try:
        evidence = _write(directory, f"requeue-{stamp}-evidence.json", document)
    except OSError as error:
        # No proof on disk means no send.
        _print({"stage": "evidence", "error_class": type(error).__name__})
        return 1
    summary: dict[str, object] = {
        "mode": document["mode"],
        "evidence": str(evidence),
        "plan_key": args.plan_id,
        "refusals": document["refusals"],
        "queue_depth": document["queue_depth"],
    }
    if not document["requeueable"]:
        _print(summary)
        return 1
    if send is None:
        _print(summary)
        return 0
    try:
        reserved = _write(
            directory,
            f"requeue-{stamp}-outcome.json",
            {"schema_version": SCHEMA_VERSION, "status": "pending", "evidence": str(evidence)},
        )
    except OSError as error:
        _print({**summary, "stage": "outcome", "error_class": type(error).__name__})
        return 1
    summary["outcome"] = str(reserved)
    try:
        outcome = send()
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
        # The messages were sent: print the outcome itself so it is not lost.
        _print({**summary, "stage": "outcome", "error_class": type(error).__name__, **outcome})
        return 1
    summary["sent"] = outcome["sent"]
    for name in ("stopped", "failed_unit", "error_class", "not_attempted"):
        if name in outcome:
            summary[name] = outcome[name]
    if "refusals" in outcome:
        summary["stop_refusals"] = outcome["refusals"]
    _print(summary)
    return 0 if outcome["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
