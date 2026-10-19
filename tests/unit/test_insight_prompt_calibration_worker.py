# SPDX-License-Identifier: MIT-0
"""Queued prompt calibration admission, lease handling, and partial-batch retries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from adapters.jobs.insight_prompt_calibration.handler import (
    InsightPromptCalibrationApplication,
    InsightPromptCalibrationOutboxDispatcher,
    apply_records,
)
from adapters.outbound.sqs.insight_prompt_calibration import (
    SqsInsightPromptCalibrationQueue,
    encode,
    parse,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCalibrationCase,
    InsightCalibrationCaseResult,
    InsightCalibrationClaimDisposition,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightCalibrationResult,
    InsightClaimedPromptCalibrationRequest,
    InsightModelRoute,
    InsightOutputClass,
    InsightPendingPromptVersion,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationClaimResult,
    InsightPromptCalibrationOutboxWork,
    InsightSlotId,
)

if TYPE_CHECKING:
    from types_boto3_sqs.client import SQSClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]


def _result() -> InsightCalibrationResult:
    return InsightCalibrationResult(
        tuple(
            InsightCalibrationCaseResult(kind, InsightCalibrationOutcome.PASSED)
            for kind in InsightCalibrationKind
        ),
        evaluator="fictional-evaluator",
    )


@dataclass
class Registry:
    disposition: InsightCalibrationClaimDisposition
    claims: list[InsightPromptCalibrationClaimRequest] = field(
        default_factory=list[InsightPromptCalibrationClaimRequest]
    )
    completions: list[InsightClaimedPromptCalibrationRequest] = field(
        default_factory=list[InsightClaimedPromptCalibrationRequest]
    )

    def claim_calibration(
        self, request: InsightPromptCalibrationClaimRequest
    ) -> InsightPromptCalibrationClaimResult:
        self.claims.append(request)
        pending = (
            InsightPendingPromptVersion(
                InsightSlotId.CONTRIBUTION,
                3,
                "A fictional prompt.",
                InsightModelRoute.DEEP,
            )
            if self.disposition is InsightCalibrationClaimDisposition.CLAIMED
            else None
        )
        return InsightPromptCalibrationClaimResult(self.disposition, pending)

    def complete_claimed_calibration(self, request: InsightClaimedPromptCalibrationRequest) -> None:
        self.completions.append(request)


@dataclass
class Runner:
    requests: list[object] = field(default_factory=list[object])
    error: Exception | None = None

    def run(self, request: object) -> InsightCalibrationResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _result()


def _application(
    disposition: InsightCalibrationClaimDisposition,
    *,
    error: Exception | None = None,
) -> tuple[InsightPromptCalibrationApplication, Registry, Runner]:
    registry = Registry(disposition)
    runner = Runner(error=error)
    contract = cast(
        "object",
        SimpleNamespace(
            calibration_cases=tuple(
                InsightCalibrationCase(
                    kind,
                    "A fictional input.",
                    "A fictional expected outcome.",
                    (DeterministicFinding("Fictional value", Decimal("1")),),
                    draft=(
                        None
                        if kind
                        in {
                            InsightCalibrationKind.KNOWN_CORRECT,
                            InsightCalibrationKind.EXPLORATORY,
                        }
                        else InsightCalibrationDraft(("A fictional narration.",))
                    ),
                )
                for kind in InsightCalibrationKind
            ),
            output_class=InsightOutputClass.PERSPECTIVE,
            purpose="Explain fictional contribution.",
        ),
    )
    application = InsightPromptCalibrationApplication(
        registry=cast("Any", registry),
        runner=cast("Any", runner),
        contracts={InsightSlotId.CONTRIBUTION: contract},  # type: ignore[dict-item]
        lease_seconds=90,
        wall=lambda: 100.0,
    )
    return application, registry, runner


def test_queue_body_carries_only_the_immutable_version_identity() -> None:
    body = encode(InsightSlotId.CONTRIBUTION, 3)

    assert json.loads(body) == {
        "schema_version": "InsightPromptCalibrationWork.v1",
        "slot_id": "contribution",
        "version": 3,
    }
    assert parse(body).version == 3
    assert "prompt" not in body


@pytest.mark.parametrize("body", ["{}", "[]", "not-json", None])
def test_queue_body_refuses_malformed_or_incomplete_work(body: object) -> None:
    with pytest.raises(ValueError):
        parse(body)


def test_worker_claims_runs_and_completes_under_the_message_lease() -> None:
    application, registry, runner = _application(InsightCalibrationClaimDisposition.CLAIMED)

    acknowledge = application.apply(
        encode(InsightSlotId.CONTRIBUTION, 3),
        lease_ref="attempt-fictional",
        receive_count=1,
    )

    assert acknowledge
    assert registry.claims[0].lease_ref == "attempt-fictional"
    assert registry.claims[0].now_epoch_seconds == 100
    assert registry.claims[0].lease_expires_epoch_seconds == 190
    assert len(runner.requests) == 1
    assert registry.completions[0].lease_ref == "attempt-fictional"
    assert registry.completions[0].completed_at_epoch_seconds == 100
    assert registry.completions[0].result.outcome is InsightCalibrationOutcome.PASSED


@pytest.mark.parametrize(
    ("disposition", "acknowledge"),
    [
        (InsightCalibrationClaimDisposition.TERMINAL, True),
        (InsightCalibrationClaimDisposition.ACTIVE_LEASE, False),
    ],
)
def test_worker_skips_model_work_for_terminal_or_actively_leased_delivery(
    disposition: InsightCalibrationClaimDisposition, acknowledge: bool
) -> None:
    application, registry, runner = _application(disposition)

    observed = application.apply(
        encode(InsightSlotId.CONTRIBUTION, 3),
        lease_ref="attempt-fictional",
        receive_count=1,
    )

    assert observed is acknowledge
    assert len(registry.claims) == 1
    assert runner.requests == []
    assert registry.completions == []


def test_provider_failure_returns_only_that_message_for_queue_retry() -> None:
    application, registry, _ = _application(
        InsightCalibrationClaimDisposition.CLAIMED,
        error=RuntimeError("fictional unavailable"),
    )
    event = {
        "Records": [
            {
                "messageId": "message-fictional",
                "body": encode(InsightSlotId.CONTRIBUTION, 3),
                "attributes": {"ApproximateReceiveCount": "2"},
            }
        ]
    }

    assert apply_records(event, application) == ("message-fictional",)
    lease_ref = registry.claims[0].lease_ref
    assert lease_ref.startswith("message-fictional:2:")
    assert lease_ref != "message-fictional"


@dataclass
class SqsClient:
    calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def send_message(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"MessageId": "message-fictional"}


def test_queue_admission_returns_provider_identity_after_send() -> None:
    client = SqsClient()
    queue = SqsInsightPromptCalibrationQueue(
        "https://sqs.example.test/calibration", cast("SQSClient", client)
    )

    message_id = queue.enqueue(InsightSlotId.CONTRIBUTION, 3)

    assert message_id == "message-fictional"
    assert client.calls[0]["QueueUrl"] == "https://sqs.example.test/calibration"
    assert parse(client.calls[0]["MessageBody"]).version == 3


@dataclass
class OutboxRegistry:
    work: tuple[InsightPromptCalibrationOutboxWork, ...]
    acknowledged: list[tuple[InsightSlotId, int]] = field(
        default_factory=list[tuple[InsightSlotId, int]]
    )

    def pending_calibration_outbox(
        self, _slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptCalibrationOutboxWork, ...]:
        return self.work

    def acknowledge_calibration_outbox(self, slot_id: InsightSlotId, version: int) -> None:
        self.acknowledged.append((slot_id, version))


@dataclass
class OutboxQueue:
    enqueued: list[tuple[InsightSlotId, int]] = field(
        default_factory=list[tuple[InsightSlotId, int]]
    )

    def enqueue(self, slot_id: InsightSlotId, version: int) -> str:
        self.enqueued.append((slot_id, version))
        return "queued-fictional"


def test_outbox_dispatch_acknowledges_only_after_queue_acceptance() -> None:
    registry = OutboxRegistry((InsightPromptCalibrationOutboxWork(InsightSlotId.CONTRIBUTION, 3),))
    queue = OutboxQueue()
    dispatcher = InsightPromptCalibrationOutboxDispatcher(
        cast("Any", registry), cast("Any", queue), (InsightSlotId.CONTRIBUTION,)
    )

    assert dispatcher.dispatch() == 1
    assert queue.enqueued == [(InsightSlotId.CONTRIBUTION, 3)]
    assert registry.acknowledged == [(InsightSlotId.CONTRIBUTION, 3)]
