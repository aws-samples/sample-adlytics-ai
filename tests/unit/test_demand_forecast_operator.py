# SPDX-License-Identifier: MIT-0
"""Demand-forecast operator commands (design §6c, §6d): the operator start and the rollback.

A start named outside `<set hash 12>-<UTC date>-op-<n>` is refused by `SelectInput`, so the
operator could not recover a run; a second start the same day must not fail on the taken name.
A rollback without its fence, its reason or its preconditions could point planners at a
generation that was never published, or overwrite a newer publish.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from forecast_series_support import INPUT_END, policy

from adapters.jobs.demand_forecast_input.handler import SelectRequest
from adapters.jobs.demand_forecast_publisher.handler import Publisher
from adapters.outbound.demand_forecast_policy import LoadedPolicy
from adapters.outbound.telemetry.demand_forecast_metrics import Datapoint
from domain.forecasting.publish import (
    CurrentPointer,
    Disposition,
    GenerationRecord,
    LastAttempt,
    ReceiptRecord,
    RollbackRefusal,
    RollbackRefusedError,
    RunOutcome,
)
from ports.outbound.forecast_store import (
    Attestation,
    CommitDisposition,
    GenerationMeta,
    GenerationSeries,
    ListPage,
    OutcomeCommitRequest,
    OutcomeCommitResult,
    StoreWriteResult,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.demand_forecast_operator import (  # noqa: E402
    NameTakenError,
    OperatorRefusedError,
    OperatorRetry,
    RollbackCommand,
    StartRunRequest,
    identity_refusal,
    main,
    name_taken,
    rollback,
    start_run,
)

pytestmark = pytest.mark.unit

SET = "0123456789ab"
TODAY = date(2026, 11, 16)
AT = datetime(2026, 11, 16, 9, 30, 5, tzinfo=UTC)
_FORBIDDEN = set(' <>{}[]?*"#%\\^|~`$&,;:/')


# ---- operator start ----


@dataclass
class _Starter:
    """Step Functions' name rules (API_StartExecution): 1 to 80 characters, none forbidden."""

    taken: set[str] = field(default_factory=set[str])
    started: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def start(self, name: str, document: str) -> str:
        if not 1 <= len(name) <= 80 or _FORBIDDEN & set(name):
            raise ValueError(f"StartExecution refuses the name {name!r}")
        if name in self.taken:
            raise NameTakenError(name)
        self.taken.add(name)
        self.started.append((name, document))
        return name


def test_an_operator_start_takes_the_first_free_op_name_of_the_day() -> None:
    starter = _Starter(taken={f"{SET}-2026-11-16-op-1", f"{SET}-2026-11-16-op-2"})
    retry = OperatorRetry(retry_failed=True, reason="quota raised")
    name = start_run(starter, StartRunRequest(SET, TODAY, retry))
    assert name == f"{SET}-2026-11-16-op-3"
    assert starter.started == [
        (name, json.dumps({"retry_failed": True, "operator_reason": "quota raised"}))
    ]
    # The input builder accepts the name and fields as an operator start, not a scheduled one.
    accepted = SelectRequest(name, retry_failed=True, operator_reason="quota raised")
    assert accepted.trigger.value == "operator"


def test_the_first_operator_start_of_a_day_is_op_1_and_carries_no_scheduled_time() -> None:
    starter = _Starter(taken={f"{SET}-2026-11-15-op-1"})
    name = start_run(starter, StartRunRequest(SET, TODAY, OperatorRetry()))
    assert name == f"{SET}-2026-11-16-op-1"
    document = json.loads(starter.started[0][1])
    assert document == {"retry_failed": False, "operator_reason": None}


def test_a_start_failure_other_than_a_taken_name_is_raised_after_one_call() -> None:
    calls: list[str] = []

    class Denied:
        def start(self, name: str, document: str) -> str:
            del document
            calls.append(name)
            raise PermissionError("AccessDeniedException")

    with pytest.raises(PermissionError, match="AccessDenied"):
        start_run(Denied(), StartRunRequest(SET, TODAY, OperatorRetry()))
    assert calls == [f"{SET}-2026-11-16-op-1"]


@pytest.mark.parametrize(
    ("code", "taken"),
    [("ExecutionAlreadyExists", True), ("ExecutionLimitExceeded", False), ("", False)],
)
def test_only_execution_already_exists_reads_as_a_taken_name(code: str, taken: bool) -> None:
    error = ClientError({"Error": {"Code": code, "Message": "m"}}, "StartExecution")
    assert name_taken(error) is taken


@pytest.mark.parametrize(
    ("retry_failed", "reason", "code"),
    [
        (True, None, "OPERATOR_REASON_REQUIRED"),
        (True, "", "OPERATOR_REASON_INVALID"),
        (True, "x" * 201, "OPERATOR_REASON_INVALID"),
        (True, "line\nbreak", "OPERATOR_REASON_INVALID"),
        (False, "tab\there", "OPERATOR_REASON_INVALID"),
    ],
    ids=["retry-without-reason", "empty", "201-chars", "newline", "tab"],
)
def test_a_reason_outside_1_to_200_printable_characters_is_refused(
    retry_failed: bool, reason: str | None, code: str
) -> None:
    with pytest.raises(OperatorRefusedError, match=f"^{code}$"):
        OperatorRetry(retry_failed=retry_failed, reason=reason)


@pytest.mark.parametrize(
    ("retry_failed", "reason"), [(True, "x" * 200), (True, "a"), (False, None), (False, "note")]
)
def test_a_reason_of_1_to_200_printable_characters_or_none_without_retry_is_accepted(
    retry_failed: bool, reason: str | None
) -> None:
    assert OperatorRetry(retry_failed=retry_failed, reason=reason).reason == reason


_TARGET_ARGS = [
    "--profile=adlytics-unit-no-such-profile",
    "--env=dev",
    "--expected-account=" + "1" * 12,
    "--marketplace-key=amazon_us",
]


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["run", *_TARGET_ARGS, "--retry-failed"], "OPERATOR_REASON_REQUIRED"),
        (["run", *_TARGET_ARGS, "--reason=bad\nreason"], "OPERATOR_REASON_INVALID"),
        (["rollback", *_TARGET_ARGS, "--generation=g-1"], "OPERATOR_REASON_REQUIRED"),
        (["rollback", *_TARGET_ARGS, "--generation=", "--reason=r"], "GENERATION_REQUIRED"),
    ],
    ids=["run-retry", "run-reason", "rollback-reason", "rollback-generation"],
)
def test_a_refused_command_exits_2_before_any_aws_call(
    argv: list[str], code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The profile doesn't exist, so any AWS call would raise instead of exiting 2."""
    assert main(argv) == 2
    assert capsys.readouterr().err == f"refused: {code}\n"


@pytest.mark.parametrize(
    ("expected", "caller", "refusal"),
    [
        ("1" * 12, "1" * 12, None),
        ("1" * 12, "2" * 12, "ACCOUNT_MISMATCH"),
        ("1" * 11, "1" * 11, "EXPECTED_ACCOUNT_INVALID"),
    ],
)
def test_the_profile_must_resolve_to_the_expected_account(
    expected: str, caller: str, refusal: str | None
) -> None:
    reads: list[str] = []

    def account() -> str:
        reads.append(caller)
        return caller

    assert identity_refusal(expected, account) == refusal
    # The account is read (one STS call) only once the ID has 12 digits.
    assert reads == ([caller] if len(expected) == 12 else [])


# ---- rollback ----


G1 = "run-1-aaaaaaaaaaaa"
G2 = "run-2-cccccccccccc"
G1_RECEIPT = "RUN#run-1#r0#commit"
FINGERPRINT_1, FINGERPRINT_2 = "a" * 64, "b" * 64


def _current(fence: int, generation: str = G2) -> CurrentPointer:
    attempt = LastAttempt(
        run_id="run-2",
        attempt_id="r0a0",
        run_fingerprint=FINGERPRINT_2,
        outcome=RunOutcome.PUBLISHED,
        failure_category=None,
        retry_disposition=Disposition.SUPPRESS,
        transient_retries=0,
        at=AT - timedelta(days=1),
    )
    return CurrentPointer(
        fence=fence,
        generation_id=generation,
        input_end=INPUT_END + timedelta(weeks=1),
        published_at=AT - timedelta(days=1),
        run_fingerprint=FINGERPRINT_2,
        last_attempt=attempt,
    )


@dataclass
class _Store:
    """The four reads and the commit a rollback makes; the commit applies the §6c fence."""

    current: CurrentPointer | None
    receipts: dict[str, ReceiptRecord]
    moved_fence: int | None = None
    """The fence a concurrent run leaves after the operator's first read."""
    commits: list[OutcomeCommitRequest] = field(default_factory=list[OutcomeCommitRequest])
    reads: int = 0

    def get_current(self, set_id: str) -> CurrentPointer | None:
        assert set_id == SET
        self.reads += 1
        if self.reads > 1 and self.moved_fence is not None and self.current is not None:
            self.current = replace(self.current, fence=self.moved_fence)
        return self.current

    def get_generation(self, generation: str) -> GenerationRecord | None:
        if generation != G1:
            return None
        return GenerationRecord(G1, SET, G1_RECEIPT, INPUT_END, FINGERPRINT_1)

    def get_receipt(self, set_id: str, sk: str) -> ReceiptRecord | None:
        assert set_id == SET
        return self.receipts.get(sk)

    def commit_outcome(self, request: OutcomeCommitRequest) -> OutcomeCommitResult:
        sk = request.receipt.sk
        if self.current is None or self.current.fence != request.expected_fence:
            return OutcomeCommitResult(CommitDisposition.SUPERSEDED, sk, RunOutcome.SUPERSEDED, 0)
        self.commits.append(request)
        self.current = request.pointer
        return OutcomeCommitResult(CommitDisposition.APPLIED, sk, request.receipt.outcome, 0)

    def put_generation_items(
        self, generation: str, series: tuple[GenerationSeries, ...]
    ) -> StoreWriteResult:
        raise AssertionError(f"a rollback writes no generation item: {generation} {series}")

    def put_list_pages(self, generation: str, pages: tuple[ListPage, ...]) -> StoreWriteResult:
        raise AssertionError(f"a rollback writes no page: {generation} {pages}")

    def put_generation_meta(self, meta: GenerationMeta) -> StoreWriteResult:
        raise AssertionError(f"a rollback writes no META: {meta}")

    def attest_generation(self, meta: GenerationMeta) -> Attestation:
        raise AssertionError(f"a rollback attests nothing: {meta}")


@dataclass
class _Metrics:
    points: list[Datapoint] = field(default_factory=list[Datapoint])

    def emit(self, points: Sequence[Datapoint], timestamp: datetime) -> None:
        del timestamp
        self.points.extend(points)


def _published() -> dict[str, ReceiptRecord]:
    return {G1_RECEIPT: ReceiptRecord(G1_RECEIPT, RunOutcome.PUBLISHED, G1, AT - timedelta(7))}


def _publisher(store: _Store, metrics: _Metrics | None = None) -> Publisher:
    class NoObjects:
        def get(self, key: str) -> bytes:
            raise AssertionError(key)

        def put_if_absent(self, key: str, body: bytes) -> bool:
            raise AssertionError(key, body)

    return Publisher(
        policy=LoadedPolicy(policy(), "f" * 64, {}),
        set_id=SET,
        objects=NoObjects(),
        store=store,
        metrics=metrics or _Metrics(),
        now=lambda: AT,
    )


def test_a_rollback_fences_on_the_current_fence_and_restores_the_published_generation() -> None:
    store, metrics = _Store(_current(5), _published()), _Metrics()
    done = rollback(_publisher(store, metrics), RollbackCommand(G1, "bad model"))
    assert (done.outcome, done.generation_id, done.expected_fence) == ("rolled_back", G1, 5)
    assert done.receipt_sk == f"RUN#{SET}-20261116T093005Z-rollback#r0#rollback"
    assert store.current is not None
    assert (store.current.generation_id, store.current.fence) == (G1, 6)
    assert store.current.rolled_back_from == G2
    attempt = store.current.last_attempt
    assert attempt is not None
    assert (attempt.run_fingerprint, attempt.retry_disposition) == (
        FINGERPRINT_2,
        Disposition.SUPPRESS,
    )
    (commit,) = store.commits
    assert (commit.expected_fence, commit.receipt.operator_reason) == (5, "bad model")
    assert [(p.spec.name, p.value, p.dimensions) for p in metrics.points] == [
        ("ForecastRunOutcome", 1.0, (("outcome", "rolled_back"),))
    ]


def test_a_rollback_whose_fence_moves_after_the_operator_read_writes_nothing() -> None:
    store = _Store(_current(5), _published(), moved_fence=6)
    with pytest.raises(RollbackRefusedError) as refused:
        rollback(_publisher(store), RollbackCommand(G1, "bad model"))
    assert refused.value.code is RollbackRefusal.FENCE_MOVED
    assert store.commits == []


_Refusal = tuple[CurrentPointer | None, dict[str, ReceiptRecord], str, RollbackRefusal]


@pytest.mark.parametrize(
    "case",
    [
        (None, _published(), G1, RollbackRefusal.NOTHING_CURRENT),
        (_current(5, generation=G1), _published(), G1, RollbackRefusal.TARGET_IS_CURRENT),
        (_current(5), {}, G1, RollbackRefusal.TARGET_NEVER_PUBLISHED),
        (_current(5), _published(), "run-9-dddddddddddd", RollbackRefusal.TARGET_UNKNOWN),
    ],
    ids=["nothing-current", "target-is-current", "never-published", "unknown"],
)
def test_a_rollback_outside_its_preconditions_is_refused_with_its_code_and_writes_nothing(
    case: _Refusal,
) -> None:
    current, receipts, target, code = case
    store = _Store(current, receipts)
    with pytest.raises(RollbackRefusedError) as refused:
        rollback(_publisher(store), RollbackCommand(target, "bad model"))
    assert refused.value.code is code
    assert store.commits == []
    assert store.current == current
