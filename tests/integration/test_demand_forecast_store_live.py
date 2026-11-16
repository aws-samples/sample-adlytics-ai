# SPDX-License-Identifier: MIT-0
"""`DynamoDbForecastStore` against the deployed demand-forecast table (design §6b, F-A8 ruling).

What no unit fake can establish: that the real `TransactWriteItems` applies the fence conditions
the adapter writes, so a stale run can never move what planners see; that a rollback moves the
pointer back with `fence + 1`; and that the slot marker's conditional put admits one execution
per period. A failure here means a published forecast could be overwritten by an older run or a
week could start two scheduled runs.

Every key lives under a forecast set named from the probe prefix and the test's node ID
(`SET#`, `SLOT#` and `GEN#` partitions), and teardown deletes exactly the items the test wrote,
each conditioned on existing, then re-reads to prove none is left. Dev only, synthetic values.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from probe_identity import PROBE_PREFIX, pytest_node_id

from adapters.outbound.dynamodb.demand_forecast_store import (
    CURRENT_SK,
    META_SK,
    DynamoDbForecastStore,
    meta_key,
    set_key,
    slot_key,
)
from domain.forecasting.boundary import RunIdentityV1, boundary_schema_digests
from domain.forecasting.list_pages import CLOSED_FILTERS, FilterPages, ListPages
from domain.forecasting.publish import (
    CurrentPointer,
    Disposition,
    LastAttempt,
    PointerAdvanceRequest,
    PublishedGeneration,
    RollbackRequest,
    RollbackState,
    RunOutcome,
    ShardPublishResult,
    next_pointer,
    rollback_pointer,
)
from ports.outbound.forecast_store import (
    CommitDisposition,
    ForecastStoreConflictError,
    GenerationMeta,
    OutcomeCommitRequest,
    RunReceipt,
    SlotClaimRequest,
)

pytestmark = pytest.mark.integration

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")
INPUT_END = date(2030, 1, 5)
"""A Saturday; fictional, so no probe item resembles a real generation's week."""
HEX = "a" * 64
ITEM_MAX_BYTES = 350_000


@dataclass
class Probe:
    """One test's store, its forecast set and every key it wrote."""

    store: DynamoDbForecastStore
    table: str
    set_id: str
    written: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])


@pytest.fixture
def probe(request: pytest.FixtureRequest) -> Iterator[Probe]:
    """Yield the deployed table and a fresh probe set.

    Skips only with no AWS credentials or no deployed table parameter (R1-11). Any other error,
    `AccessDenied` or a throttle included, fails the test, so a broken dev never reads as
    "nothing to probe".
    """
    if ENV_NAME != "dev":
        pytest.skip("demand-forecast store probes run only against dev")
    try:
        parameter = boto3.client("ssm").get_parameter(
            Name=f"/adlytics/{ENV_NAME}/data/demand-forecast-table-name"
        )["Parameter"]
    except NoCredentialsError:
        pytest.skip("integration tier needs AWS credentials")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        pytest.skip(f"integration tier needs the deployed {ENV_NAME} forecast table")
    name = str(parameter.get("Value", ""))
    digest = hashlib.sha256(pytest_node_id(request).encode()).hexdigest()[:12]
    found = Probe(DynamoDbForecastStore(name, ITEM_MAX_BYTES), name, f"{PROBE_PREFIX}-{digest}")
    yield found
    client = boto3.client("dynamodb")
    for pk, sk in sorted(found.written):
        assert pk.split("#", 1)[1].startswith(PROBE_PREFIX)
        key: Any = {"PK": {"S": pk}, "SK": {"S": sk}}
        try:
            client.delete_item(
                TableName=name,
                Key=key,
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    left = [
        key
        for key in found.written
        if "Item"
        in client.get_item(
            TableName=name, Key={"PK": {"S": key[0]}, "SK": {"S": key[1]}}, ConsistentRead=True
        )
    ]
    assert left == []


def _identity() -> RunIdentityV1:
    return RunIdentityV1(
        input_fingerprint=HEX,
        policy_sha256=HEX,
        policy_section_versions={"engine": 1, "gate": 1, "retry": 1},
        processor_code_identity=HEX,
        lock_digest=HEX,
        model_id="amazon/chronos-2",
        model_artifact_digest=HEX,
        boundary_schema_digests=boundary_schema_digests(),
        instance_type="ml.m5.xlarge",
        max_runtime_seconds=3600,
    )


def _meta(probe: Probe, run: str, fingerprint: str) -> GenerationMeta:
    """Write an empty one-shard generation's META, as `commit_generation` writes it last."""
    run_id = f"{probe.set_id}-{run}"
    shard = ShardPublishResult(
        shard=0,
        attempt_id="r0a0",
        series_sha256=fingerprint,
        item_count=1,
        roster_part_key=f"runs/{run_id}/shards/0/attempts/r0a0/roster-part.jsonl.gz",
        roster_part_sha256=HEX,
    )
    index = dict.fromkeys(CLOSED_FILTERS, FilterPages(page_count=1, entry_count=0))
    meta = GenerationMeta(
        set_id=probe.set_id,
        run_id=run_id,
        receipt_sk=f"RUN#{run_id}#r0#commit",
        input_end=INPUT_END,
        run_fingerprint=fingerprint,
        run_identity=_identity(),
        attempts=(shard,),
        currency_codes=("USD",),
        counts_by_reason={},
        counts_by_model={},
        pages=ListPages((), index),
    )
    assert probe.store.put_generation_meta(meta).written == 1
    probe.written.add((meta_key(meta.generation_id), META_SK))
    return meta


def _publish(probe: Probe, meta: GenerationMeta, expected_fence: int) -> CommitDisposition:
    """Commit `meta` as `published` on `expected_fence`, as the publisher's commit does."""
    now = datetime.now(UTC)
    current = probe.store.get_current(probe.set_id)
    attempt = LastAttempt(
        run_id=meta.run_id,
        attempt_id=None,
        run_fingerprint=meta.run_fingerprint,
        outcome=RunOutcome.PUBLISHED,
        failure_category=None,
        retry_disposition=Disposition.SUPPRESS,
        transient_retries=0,
        at=now,
    )
    generation = PublishedGeneration(meta.generation_id, INPUT_END, meta.run_fingerprint, now)
    advance = next_pointer(PointerAdvanceRequest(current, expected_fence, attempt, generation))
    receipt = RunReceipt(
        sk=meta.receipt_sk,
        outcome=RunOutcome.PUBLISHED,
        recorded_at=now,
        generation_id=meta.generation_id,
        published_at=now,
        retry_disposition=Disposition.SUPPRESS,
        run_fingerprint=meta.run_fingerprint,
    )
    request = OutcomeCommitRequest(
        probe.set_id, expected_fence, receipt, advance.pointer, meta.series_sha256
    )
    probe.written |= {(set_key(probe.set_id), CURRENT_SK), (set_key(probe.set_id), receipt.sk)}
    return probe.store.commit_outcome(request).disposition


def _current(probe: Probe) -> CurrentPointer:
    current = probe.store.get_current(probe.set_id)
    assert current is not None
    return current


def test_a_fenced_publish_advances_current_and_a_stale_fence_is_superseded(probe: Probe) -> None:
    first = _meta(probe, "run-a", "1" * 64)
    assert _publish(probe, first, expected_fence=0) is CommitDisposition.APPLIED
    assert (_current(probe).generation_id, _current(probe).fence) == (first.generation_id, 1)
    stale = _meta(probe, "run-b", "2" * 64)
    assert _publish(probe, stale, expected_fence=0) is CommitDisposition.SUPERSEDED
    current = _current(probe)
    assert (current.generation_id, current.fence) == (first.generation_id, 1)
    receipt = probe.store.get_receipt(probe.set_id, stale.receipt_sk)
    assert receipt is not None
    # R1-10: a superseded commit names the generation it attested, never CURRENT.
    assert (receipt.outcome, receipt.generation_id) == (RunOutcome.SUPERSEDED, stale.generation_id)
    # Paired control: the same kind of publish on the fence it read advances.
    fresh = _meta(probe, "run-c", "3" * 64)
    assert _publish(probe, fresh, expected_fence=1) is CommitDisposition.APPLIED
    assert (_current(probe).generation_id, _current(probe).fence) == (fresh.generation_id, 2)


def test_a_commit_under_a_key_holding_another_generations_receipt_is_a_conflict(
    probe: Probe,
) -> None:
    """The real cancellation reason reaches the replay check, which refuses (R1-10)."""
    first = _meta(probe, "run-a", "1" * 64)
    assert _publish(probe, first, expected_fence=0) is CommitDisposition.APPLIED
    # The same run and pass with shard 0's late attempt r0a1: another generation, one key.
    late = replace(first.attempts[0], attempt_id="r0a1", series_sha256="2" * 64)
    other = replace(first, attempts=(late,))
    assert probe.store.put_generation_meta(other).written == 1
    probe.written.add((meta_key(other.generation_id), META_SK))
    assert other.receipt_sk == first.receipt_sk
    assert other.generation_id != first.generation_id
    with pytest.raises(ForecastStoreConflictError, match="the receipt names another generation"):
        _publish(probe, other, expected_fence=0)
    receipt = probe.store.get_receipt(probe.set_id, first.receipt_sk)
    assert receipt is not None
    assert (receipt.outcome, receipt.generation_id) == (RunOutcome.PUBLISHED, first.generation_id)
    current = _current(probe)
    assert (current.generation_id, current.fence) == (first.generation_id, 1)


def _rollback(probe: Probe, target: GenerationMeta, run: str) -> OutcomeCommitRequest:
    """Plan a rollback to `target` from fresh reads, as the operator's rollback does."""
    now = datetime.now(UTC)
    current = _current(probe)
    record = probe.store.get_generation(target.generation_id)
    receipt = probe.store.get_receipt(probe.set_id, target.receipt_sk)
    request = RollbackRequest(probe.set_id, target.generation_id, current.fence, "probe")
    plan = rollback_pointer(
        request, RollbackState(current, record, receipt, f"{probe.set_id}-{run}", now)
    )
    probe.written.add((set_key(probe.set_id), plan.receipt_sk))
    return OutcomeCommitRequest(
        probe.set_id,
        current.fence,
        RunReceipt(
            sk=plan.receipt_sk,
            outcome=RunOutcome.ROLLED_BACK,
            recorded_at=now,
            generation_id=target.generation_id,
            retry_disposition=Disposition.SUPPRESS,
            operator_reason="probe",
            run_fingerprint=plan.pointer.run_fingerprint,
        ),
        plan.pointer,
    )


def test_a_rollback_restores_the_target_with_fence_plus_one(probe: Probe) -> None:
    first = _meta(probe, "run-a", "1" * 64)
    second = _meta(probe, "run-b", "2" * 64)
    assert _publish(probe, first, expected_fence=0) is CommitDisposition.APPLIED
    assert _publish(probe, second, expected_fence=1) is CommitDisposition.APPLIED
    commit = _rollback(probe, first, "rollback-1")
    assert probe.store.commit_outcome(commit).disposition is CommitDisposition.APPLIED
    current = _current(probe)
    assert (current.generation_id, current.fence) == (first.generation_id, 3)
    assert current.rolled_back_from == second.generation_id
    assert current.last_attempt is not None
    assert (current.last_attempt.outcome, current.last_attempt.retry_disposition) == (
        RunOutcome.ROLLED_BACK,
        Disposition.SUPPRESS,
    )
    # Ruling 2026-11-16 R1-8: the next publish removes the rollback's rolled_back_from.
    third = _meta(probe, "run-c", "3" * 64)
    assert _publish(probe, third, expected_fence=3) is CommitDisposition.APPLIED
    after = _current(probe)
    assert (after.generation_id, after.rolled_back_from) == (third.generation_id, None)


def _held_from(probe: Probe, read: CurrentPointer, run: str) -> OutcomeCommitRequest:
    """Prepare a held outcome from `read`, as RecordOutcome does from the CURRENT it read."""
    now = datetime.now(UTC)
    run_id = f"{probe.set_id}-{run}"
    attempt = LastAttempt(
        run_id=run_id,
        attempt_id=None,
        run_fingerprint="4" * 64,
        outcome=RunOutcome.HELD_QUALITY,
        failure_category="target_empty",
        retry_disposition=Disposition.SUPPRESS,
        transient_retries=0,
        at=now,
    )
    advance = next_pointer(PointerAdvanceRequest(read, read.fence, attempt))
    receipt = RunReceipt(
        sk=f"RUN#{run_id}#r0#commit",
        outcome=RunOutcome.HELD_QUALITY,
        recorded_at=now,
        failure_category="target_empty",
        retry_disposition=Disposition.SUPPRESS,
        run_fingerprint="4" * 64,
    )
    probe.written.add((set_key(probe.set_id), receipt.sk))
    return OutcomeCommitRequest(
        probe.set_id, read.fence, receipt, advance.pointer, previous_attempt=read.last_attempt
    )


def test_two_holds_prepared_from_one_current_record_one_and_supersede_the_other(
    probe: Probe,
) -> None:
    """The real service compares the stored `last_attempt` map with the one the run read (R1-2)."""
    first = _meta(probe, "run-a", "1" * 64)
    assert _publish(probe, first, expected_fence=0) is CommitDisposition.APPLIED
    read = _current(probe)
    one, two = _held_from(probe, read, "held-1"), _held_from(probe, read, "held-2")
    assert probe.store.commit_outcome(one).disposition is CommitDisposition.APPLIED
    assert probe.store.commit_outcome(two).disposition is CommitDisposition.SUPERSEDED
    current = _current(probe)
    assert one.pointer is not None
    assert (current.fence, current.last_attempt) == (1, one.pointer.last_attempt)


def test_a_rollback_planned_before_the_fence_moved_is_superseded(probe: Probe) -> None:
    first = _meta(probe, "run-a", "1" * 64)
    second = _meta(probe, "run-b", "2" * 64)
    assert _publish(probe, first, expected_fence=0) is CommitDisposition.APPLIED
    assert _publish(probe, second, expected_fence=1) is CommitDisposition.APPLIED
    commit = _rollback(probe, first, "rollback-1")
    third = _meta(probe, "run-c", "3" * 64)
    assert _publish(probe, third, expected_fence=2) is CommitDisposition.APPLIED
    assert probe.store.commit_outcome(commit).disposition is CommitDisposition.SUPERSEDED
    current = _current(probe)
    assert (current.generation_id, current.fence) == (third.generation_id, 3)


def test_the_slot_marker_admits_one_execution_per_period(probe: Probe) -> None:
    slot = INPUT_END + timedelta(days=5)
    probe.written.add((slot_key(probe.set_id), slot.isoformat()))

    def claim(execution: str, run: str) -> tuple[bool, str]:
        result = probe.store.claim_slot(
            SlotClaimRequest(
                set_id=probe.set_id,
                slot_date=slot,
                run_id=f"{probe.set_id}-{run}",
                execution_name=execution,
                scheduled_time=datetime.combine(slot, time(13), UTC),
            )
        )
        return result.claimed, result.holder_run_id

    holder = f"{probe.set_id}-slot"
    assert claim("probe-exec-1", "slot") == (True, holder)
    assert claim("probe-exec-2", "duplicate") == (False, holder)
    assert claim("probe-exec-1", "slot") == (True, holder)
