# SPDX-License-Identifier: MIT-0
"""Slice 7 (1b) fixtures: runs with PENDING `write_unit` nodes, armed, permitted and claimed.

Every helper drives the real dispatch adapter over the transactional fake, so a batch fixture is
exactly what the store's own transactions wrote. Fictional data only.
"""

from __future__ import annotations

from typing import Any

from execution_graph_dispatch_support import materialized_run, now_text, store, write_text
from execution_graph_support import FakeDynamo

from domain.operations.execution_graph_dispatch import batch_id_for
from domain.operations.status import canonical_authority_digest
from ports.outbound.execution_graph_dispatch import (
    ArmUnitBatchRequest,
    BatchPermitRequest,
    BatchSendReceipt,
    ClaimRecord,
    ClaimRequest,
    NodeRecord,
    OutboxRecord,
    UnitMember,
)

LEASE_EXPIRES: str = "2026-10-07T09:20:00+00:00"


def unit_run(fake: FakeDynamo, index: int, *keys: str, clock: str | None = None) -> str:
    """Seed one materialized run (its own window) with PENDING units on the named tables."""
    run = materialized_run(
        fake,
        *keys,
        unit=canonical_authority_digest(["fictional-window", str(index)]),
        fingerprint=f"{index:02x}" * 32,
        request_id=f"request-{index:04d}",
    )
    if clock is not None:
        fake.items[(f"RUN#{run}", "RUN")]["root_clock_started_at"] = write_text(clock)
    return run


def member(fake: FakeDynamo, run: str, key: str) -> UnitMember:
    """Read one run and its `write_unit:<key>` node strongly."""
    record = store(fake).read_run(run)
    assert record is not None
    (node,) = [item for item in store(fake).read_nodes(run) if item.name == f"write_unit:{key}"]
    return UnitMember(record, node)


def arm(  # noqa: PLR0913 -- one fixture builder
    fake: FakeDynamo,
    key: str,
    runs: tuple[str, ...],
    *,
    retire: tuple[str, ...] | None = None,
    armed_at: str | None = None,
) -> str:
    """Arm one batch of the named runs' units on `TABLE#<key>`; return its batch ID."""
    members = tuple(member(fake, run, key) for run in runs)
    at = armed_at or now_text()
    batch = batch_id_for(f"TABLE#{key}", at, [item.node.node_id for item in members])
    store(fake).arm_unit_batch(
        ArmUnitBatchRequest(
            lock_key=f"TABLE#{key}",
            batch_id=batch,
            members=members,
            retire_runs=runs if retire is None else retire,
            recorded_at=at,
        )
    )
    return batch


def outbox(fake: FakeDynamo, batch: str) -> OutboxRecord:
    """Read a batch outbox row."""
    from adapters.outbound.dynamodb.execution_graph_dispatch import (  # noqa: PLC0415
        batch_row_shard,
    )

    row = store(fake).read_outbox(batch_row_shard(batch), batch)
    assert row is not None
    return row


def permit_and_send(fake: FakeDynamo, key: str, batch: str, runs: tuple[str, ...]) -> None:  # noqa: PLR0913, PLR0917 -- one fixture builder
    """Permit every member of a batch row and record its send (OPEN -> SENDING -> closed)."""
    row = outbox(fake, batch)
    members = tuple(member(fake, run, key) for run in runs)
    store(fake).acquire_batch_permit(BatchPermitRequest(row, members, now_text()))
    sent = tuple(member(fake, run, key).node for run in runs)
    store(fake).record_batch_send(
        BatchSendReceipt(outbox(fake, batch), sent, "message-batch", now_text())
    )


def claim(fake: FakeDynamo, run: str, key: str, batch: str) -> ClaimRecord:  # noqa: PLR0913, PLR0917 -- one fixture builder
    """Claim (or reclaim) one dispatched member with the batch's ID."""
    item = member(fake, run, key)
    existing = store(fake).read_claim(item.node.node_id)
    return store(fake).claim(
        ClaimRequest(
            run=item.run,
            node=item.node,
            claim=existing,
            lease_expires_at=LEASE_EXPIRES,
            claimed_at=now_text(),
            batch_id=batch,
        )
    )


def lock(fake: FakeDynamo, key: str) -> dict[str, Any]:
    """Return the table lock item."""
    return fake.items[(f"LOCK#TABLE#{key}", "LOCK")]


def open_members(fake: FakeDynamo, key: str) -> int:
    """Return the counted lock's open member count."""
    return int(lock(fake, key)["open_members"]["N"])


def node_item(fake: FakeDynamo, run: str, key: str) -> dict[str, Any]:
    """Return the raw node item."""
    return fake.items[(f"RUN#{run}", f"NODE#write_unit:{key}")]


def node_of(fake: FakeDynamo, run: str, key: str) -> NodeRecord:
    """Return the parsed node."""
    return member(fake, run, key).node
