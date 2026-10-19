# SPDX-License-Identifier: MIT-0
"""Prove one bounded Agent Quality receipt chain through the deployed dev conversation route.

The probe uses fixed fictional requests. It reads each turn's receipt, findings, and evaluation
chain only through `POST /api/quality-review/turn` with a reviewer persona's bearer (ADR-078 §5),
never from the record tables, and reads the aggregate table directly. It writes raw HTTP frames
and opaque operational mappings only beneath ignored validation/evidence. Standard output exposes
bounded terminal and audit-chain facts only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from probe_conversation_route import _refreshed_session, call_with_correlation
from quality_review_client import (
    DEFAULT_REVIEWER_PERSONA,
    PREDATES_GATE,
    ReviewClient,
    ReviewedDelivery,
    ReviewedEvaluation,
    ReviewedReceipt,
    ReviewedVerdict,
    ReviewUnavailableError,
    reviewed_evaluation,
    reviewed_findings,
    reviewed_receipt,
    reviewer_client,
)

from adapters.inbound.conversation.contract_codec import decode_public_conversation_stream_event
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.dynamodb import audit as audit_adapter
from adapters.outbound.https import require_https
from adapters.outbound.ssm.evaluation_release import SsmEvaluationReleaseReader
from domain.conversation.answer_stream import (
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    validate_conversation_stream,
)
from domain.conversation.audit import ConversationAuditRecordV6
from domain.conversation.audit_findings import aggregate_metric, derive_findings
from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationNotDispatchedReasonV1,
)
from domain.evaluation.release import (
    CURRENT_EVALUATION_FAMILIES,
    EvaluationFamilyV1,
    EvaluationReleaseV1,
)
from domain.shared_kernel.errors import AdlyticsError

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = (_ROOT / "validation" / "evidence").resolve()
_REGION = "us-east-1"
_POLL_SECONDS = 60
_EVALUATION_POLL_SECONDS = 1020
_DEFAULT_VERDICT_TIMEOUT_SECONDS = 900
"""AgentCore online evaluation returns results in fixed 5-minute batches, so a verdict lands
434-675 s after its span in dev (2026-09-29) and at most about 734 s by that cadence; 900 s
no longer depends on the minute a turn is dispatched."""
_MAX_VERDICT_TIMEOUT_SECONDS = 3600
_MAX_POLL_INTERVAL_SECONDS = 13
_ACCOUNT_ID_LENGTH = 12
_HTTP_OK = 200
_SEMANTIC_RESULT_STATES = frozenset({"completed", "evaluator_failed"})
_STACK_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
# ADR-079: the ingress is invoked only by CloudWatch Logs through one full resource policy, so the
# stack carries that policy and no `AWS::Lambda::Permission` at all. Subscription filters are
# attested separately: exactly one per current family's stack-owned result group.
_RESULT_LINK_RESOURCE_COUNTS = {
    "AWS::Lambda::Function": 2,
    "AWS::SQS::Queue": 2,
    "AWS::Lambda::EventSourceMapping": 1,
    "AWS::Lambda::ResourcePolicy": 1,
    "AWS::Lambda::Permission": 0,
}
_SUBSCRIPTION_FILTER_TYPE = "AWS::Logs::SubscriptionFilter"
_EXPECTED_RESULT_LINK_STACK = "Adlytics-EvaluationResultLinkV3-dev"


def _audit_receipt_summary(
    record: ConversationAuditRecordV6 | ReviewedReceipt,
) -> dict[str, object]:
    """Project only sanitized terminal classifications and the content-free trajectory.

    A streamed-answer receipt (ADR-083) holds governed steps only and counts the characters the
    user was sent; the count is a size, never the text.
    """
    return {
        "runtime_version": record.runtime_version,
        "response_contract_version": record.response_contract_version,
        "trace_present": record.trace_id is not None,
        "trajectory": [
            {
                "ordinal": step.ordinal,
                "tool_name": step.tool_name,
                "disposition": step.disposition.value,
                "violations": list(step.violations),
            }
            for step in record.trajectory
        ],
        "released_character_count": record.released_character_count,
        "operation_category": record.operation_category.value,
        "operation_outcome": record.operation_outcome.value,
        "publication_outcome": record.publication_outcome.value,
        "terminal_outcome": record.terminal_outcome.value,
    }


def semantic_evaluation_result_state(result: object) -> str:
    """Return only one persisted semantic evaluator state; all other states fail acceptance."""
    state = getattr(result, "state", None)
    value = getattr(state, "value", None)
    if not isinstance(value, str) or value not in _SEMANTIC_RESULT_STATES:
        raise _ProbeStageError("evaluation_result_missing_or_invalid")
    return value


@dataclass(frozen=True, slots=True)
class Scenario:
    """One fixed fictional authenticated turn and its required audit-derived result class."""

    name: str
    question: str
    expects_finding: bool


class _ProbeStageError(Exception):
    """Fixed probe-internal stage marker that never carries provider or content detail."""


# Each question is one a user would ask, never an instruction to the assistant: the Memory
# resource's user-preference and semantic strategies are scoped to the actor across
# conversations, so an instruction-shaped question is learned as a durable preference and
# steers every later probe turn for the same user (observed 2026-09-27).
_SCENARIOS = (
    Scenario(
        name="workspace-capability",
        question="What kinds of declared analysis can this fictional workspace support?",
        expects_finding=False,
    ),
    Scenario(
        name="clarification",
        # Refers to a comparison no earlier turn named, so answering needs a clarification.
        question="How does it compare with the earlier one?",
        expects_finding=False,
    ),
)


def probe_parser() -> argparse.ArgumentParser:
    """Build the dev-target arguments the probe and the agent regression runner share."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--result-link-stack", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument(
        "--reviewer-persona",
        default=DEFAULT_REVIEWER_PERSONA,
        help="Synthetic dev persona in quality-reviewer whose bearer reads each turn's review.",
    )
    parser.add_argument(
        "--verdict-timeout-seconds",
        type=_verdict_timeout,
        default=_DEFAULT_VERDICT_TIMEOUT_SECONDS,
        help="Bounded wait for every dispatched evaluator's verdict per scenario.",
    )
    return parser


def _verdict_timeout(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= _MAX_VERDICT_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError("verdict timeout must be 1 to 3600 seconds")
    return seconds


def _release_parameter_names(environment: str) -> tuple[str, ...]:
    """Name each current family's release record, which only its gate writes (ADR-079)."""
    return tuple(
        f"/adlytics/{environment}/evaluation/release-{family.value}"
        for family in CURRENT_EVALUATION_FAMILIES
    )


def _subscription_filter_name(environment: str, family: EvaluationFamilyV1) -> str:
    return f"adlytics-{environment}-evaluation-result-ingress-{family.value}"


def enabled_evaluation_releases(
    releases: Iterable[EvaluationReleaseV1 | None],
) -> tuple[EvaluationReleaseV1, ...]:
    """Return the enabled release records; a missing record reads as not enabled."""
    return tuple(release for release in releases if release is not None and release.enabled)


def _family_signature(
    family: EvaluationFamilyV1, config_id: str | None, evaluator_ids: Iterable[str]
) -> tuple[str, str | None, frozenset[str]]:
    return family.value, config_id, frozenset(evaluator_ids)


def evaluation_dispatch_mismatch(
    delivery: object,
    enabled: tuple[EvaluationReleaseV1, ...],
) -> str | None:
    """Return a fixed reason when a delivery receipt disagrees with the release records.

    With a family enabled the row must be dispatched to exactly the enabled families under their
    released configuration and evaluators. With none enabled it must end not dispatched because
    evaluation was not enabled. A receipt that predates the gate is never current evidence.
    """
    if delivery == PREDATES_GATE:
        return "delivery_predates_gate"
    if not isinstance(delivery, ReviewedDelivery):
        return "delivery_invalid"
    if not enabled:
        undispatched = (
            delivery.outcome is EvaluationDeliveryOutcomeV1.NOT_DISPATCHED
            and delivery.not_dispatched_reason
            is EvaluationNotDispatchedReasonV1.EVALUATION_NOT_ENABLED
        )
        return None if undispatched else "delivery_not_undispatched"
    if delivery.outcome is not EvaluationDeliveryOutcomeV1.DISPATCHED:
        return "delivery_not_dispatched"
    received = {
        _family_signature(
            item.family, item.config_id, (evaluator.evaluator_id for evaluator in item.evaluators)
        )
        for item in delivery.families
    }
    released = {
        _family_signature(
            release.family,
            release.config_id,
            (evaluator.evaluator_id for evaluator in release.evaluators),
        )
        for release in enabled
    }
    if received != released:
        return "delivery_family_mismatch"
    return None


def _provenance_matches(result: ReviewedVerdict, family: EvaluationDeliveryFamilyV1) -> bool:
    evaluator = family.evaluator(result.evaluator_ref)
    return (
        evaluator is not None
        and result.family is family.family
        and result.config_id == family.config_id
        and result.evaluator_name == evaluator.evaluator_name
        and result.evaluator_revision == evaluator.revision
        and result.model_id == evaluator.model_id
        and result.prompt_digest == evaluator.prompt_digest
    )


def dispatched_verdicts(
    receipt: ReviewedDelivery,
    results: Iterable[ReviewedVerdict],
) -> tuple[ReviewedVerdict, ...] | None:
    """Return one verdict per dispatched evaluator, or `None` while any is still pending.

    Every present verdict must carry the provenance its receipt entry names and a semantic state;
    a verdict from an evaluator the row was not sent to fails rather than waits.
    """
    by_evaluator: dict[str, ReviewedVerdict] = {}
    for result in results:
        family = next(
            (item for item in receipt.families if item.evaluator(result.evaluator_ref)), None
        )
        if family is None or not _provenance_matches(result, family):
            raise _ProbeStageError("evaluation_verdict_provenance")
        semantic_evaluation_result_state(result)
        by_evaluator[result.evaluator_ref] = result
    expected = [
        evaluator.evaluator_id for family in receipt.families for evaluator in family.evaluators
    ]
    if any(evaluator_id not in by_evaluator for evaluator_id in expected):
        return None
    return tuple(by_evaluator[evaluator_id] for evaluator_id in expected)


def evaluation_verdict_labels(results: Iterable[ReviewedVerdict]) -> dict[str, str | None]:
    """Project each evaluator's name to its normalized scale label, and nothing else."""
    return {result.evaluator_name: result.label for result in results}


def _parameter_path(environment: str, key: str) -> str:
    document = json.loads((_ROOT / "contracts" / "generated" / "parameter-paths.json").read_text())
    parameters = cast("dict[str, object]", document["parameters"])
    raw = cast("dict[str, object]", parameters[key])
    area = raw["area"]
    name = raw["name"]
    if not isinstance(area, str) or not isinstance(name, str):
        raise ValueError("parameter contract is malformed")
    return f"/adlytics/{environment}/{area}/{name}"


def _evidence_dir(value: str) -> Path:
    path = (_ROOT / value).resolve()
    if path != _EVIDENCE_ROOT and _EVIDENCE_ROOT not in path.parents:
        raise ValueError("evidence directory must be beneath validation/evidence")
    return path


def _account(session: boto3.Session, expected_account: str) -> None:
    if not expected_account.isdecimal() or len(expected_account) != _ACCOUNT_ID_LENGTH:
        raise ValueError("expected account must be a 12-digit account identifier")
    account = session.client("sts").get_caller_identity().get("Account")
    if not isinstance(account, str) or account != expected_account:
        raise ValueError("explicit profile did not resolve to the expected account")


# redirect_stdout swaps the process-wide sys.stdout, so concurrent refreshes (the regression
# runner's worker threads) must not interleave or one would restore the other's buffer.
_REFRESH_LOCK = threading.Lock()


def _cookies(session: boto3.Session) -> tuple[str, str]:
    with _REFRESH_LOCK:
        return _cookies_unlocked(session)


def _cookies_unlocked(session: boto3.Session) -> tuple[str, str]:
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    access = os.environ.get("ADL_SESSION", "").strip()
    if refresh:
        if not csrf:
            raise ValueError("ADL_CSRF is required with ADL_REFRESH")
        with contextlib.redirect_stdout(io.StringIO()):
            access = _refreshed_session(session, refresh, csrf)
    if not access:
        raise ValueError("a local ADL_REFRESH or ADL_SESSION is required")
    cookie = f"adl_session={access}"
    if csrf:
        cookie += f"; adl_csrf={csrf}"
    return cookie, csrf


def _terminal(chunks: list[tuple[float, bytes]]) -> tuple[str, list[ConversationStreamEventV4]]:
    frames = b"".join(raw for _, raw in chunks).decode("utf-8", errors="strict").split("\n\n")
    events: list[ConversationStreamEventV4] = []
    for frame in frames:
        # A line starting with ":" is an SSE comment, such as the route's keep-alive heartbeat;
        # the SSE format defines it as ignorable, so a frame of only comments carries no event.
        lines = [line for line in frame.splitlines() if not line.startswith(":")]
        if not any(line.strip() for line in lines):
            continue
        data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if not data or any(not line.startswith("data:") for line in lines):
            raise ValueError("SSE frame is malformed")
        events.append(decode_public_conversation_stream_event("\n".join(data)))
    if validate_conversation_stream(tuple(events), closed=True) is not None:
        return "response_done", events
    if events and isinstance(events[-1], ConversationRefusedEventV4):
        return "refused", events
    raise ValueError("SSE stream has an invalid V4 terminal")


def _write_raw(
    *,
    directory: Path,
    scenario: Scenario,
    correlation_id: str,
    chunks: list[tuple[float, bytes]],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{scenario.name}.json").write_text(
        json.dumps(
            {
                "correlation_id": correlation_id,
                "frames": [raw.decode("utf-8", errors="strict") for _, raw in chunks],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _processor_algorithms() -> tuple[Any, Any]:
    """Return the processor's pure calculations from the domain, never its Lambda handler."""
    return aggregate_metric, derive_findings


def reviewed_findings_match(expected: Iterable[object], reviewed: Iterable[object]) -> bool:
    """Compare the processor's derived findings with what the review API returned for them.

    Both sides reduce to closed kind, severity, creation time, and step anchor; a finding the
    API returned that the processor would not derive, or one missing, is a mismatch.
    """

    def _key(finding: object) -> tuple[str, str, str, int]:
        item = cast("Any", finding)
        return (
            item.kind.value,
            item.severity.value,
            item.created_at,
            -1 if item.step_ordinal is None else item.step_ordinal,
        )

    return sorted(map(_key, expected)) == sorted(map(_key, reviewed))


def _exact_processor_outputs(
    *,
    client: Any,
    aggregate_table: str,
    review: dict[str, object],
    record: ReviewedReceipt,
) -> tuple[bool, int, bool]:
    aggregate_metric, derive_findings = _processor_algorithms()

    audit_record = cast("Any", record)
    findings = derive_findings(audit_record)
    findings_attested = reviewed_findings_match(findings, reviewed_findings(review))

    metric = aggregate_metric(audit_record)
    response = client.get_item(
        TableName=aggregate_table,
        Key={"metric_id": {"S": metric.metric_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, dict):
        return findings_attested, len(findings), False
    audit_adapter._decode_metric(dict(item), metric)  # pyright: ignore[reportPrivateUsage]
    return findings_attested, len(findings), True


def _poll_intervals() -> Iterator[int]:
    """Yield increasing read intervals capped for responsive, bounded service polling."""
    prior, current = 1, 2
    while True:
        yield current
        prior, current = current, min(_MAX_POLL_INTERVAL_SECONDS, prior + current)


def _wait_for_chain(
    *,
    review: ReviewClient,
    client: Any,
    aggregate_table: str,
    correlation_id: str,
    expects_finding: bool,
) -> tuple[ReviewedReceipt, bool, int, bool]:
    deadline = time.monotonic() + _POLL_SECONDS
    state = "receipt_absent"
    intervals = _poll_intervals()
    while time.monotonic() < deadline:
        # Absent and non-unique correlations read alike through the API: both are not yet one.
        document = review.turn(correlation_id)
        if document is None:
            state = "receipt_absent"
            time.sleep(next(intervals))
            continue
        record = reviewed_receipt(document)
        if record.correlation_id != correlation_id:
            raise ValueError("reviewed receipt does not carry the selected correlation")
        findings_attested, finding_count, metric_attested = _exact_processor_outputs(
            client=client,
            aggregate_table=aggregate_table,
            review=document,
            record=record,
        )
        if not metric_attested:
            state = "aggregate_pending"
        elif expects_finding and not findings_attested:
            state = "finding_pending"
        else:
            return record, findings_attested, finding_count, metric_attested
        time.sleep(next(intervals))
    raise TimeoutError(f"audit_chain_{state}")


@dataclass(frozen=True, slots=True)
class _EvaluationTarget:
    """What this run expects of evaluation: the enabled releases and the verdict budget."""

    enabled: tuple[EvaluationReleaseV1, ...]
    verdict_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class _DispatchDecision:
    """The attested dispatcher decision for one admitted turn."""

    admission_ref: str
    receipt: ReviewedDelivery
    writer_trace_id: str | None


def _reviewed_chain(review: ReviewClient, correlation_id: str) -> ReviewedEvaluation:
    document = review.turn(correlation_id)
    if document is None:
        raise ValueError("a reviewed receipt stopped reading as present")
    evaluation = reviewed_evaluation(document)
    if evaluation is None:
        raise ValueError("audit receipt has no selected evaluation admission")
    return evaluation


def _wait_for_dispatch_decision(
    *,
    review: ReviewClient,
    correlation_id: str,
    record: ReviewedReceipt,
    enabled: tuple[EvaluationReleaseV1, ...],
) -> _DispatchDecision:
    """Wait for the dispatcher's receipt and attest it against the release records (ADR-079)."""
    admission_ref = record.evaluation_admission_ref
    if not isinstance(admission_ref, str) or not admission_ref:
        raise ValueError("audit receipt has no selected evaluation admission")
    deadline = time.monotonic() + _EVALUATION_POLL_SECONDS
    state = "admission_pending"
    intervals = _poll_intervals()
    while time.monotonic() < deadline:
        evaluation = _reviewed_chain(review, correlation_id)
        if not evaluation.admitted:
            state = "admission_pending"
        elif evaluation.delivery is None:
            state = "delivery_pending"
        else:
            mismatch = evaluation_dispatch_mismatch(evaluation.delivery, enabled)
            if mismatch is not None:
                raise _ProbeStageError(f"evaluation_{mismatch}")
            receipt = cast(ReviewedDelivery, evaluation.delivery)
            if receipt.outcome is EvaluationDeliveryOutcomeV1.NOT_DISPATCHED:
                # Nothing was sent, so nothing may have been emitted for or judged about it.
                if evaluation.writer_present:
                    raise _ProbeStageError("evaluation_writer_unexpected")
                if evaluation.verdicts:
                    raise _ProbeStageError("evaluation_verdict_unexpected")
                return _DispatchDecision(admission_ref, receipt, None)
            if evaluation.writer_present:
                return _DispatchDecision(admission_ref, receipt, evaluation.writer_trace_id)
            state = "writer_pending"
        time.sleep(next(intervals))
    raise TimeoutError(f"evaluation_linkage_{state}")


def _wait_for_verdicts(
    *,
    review: ReviewClient,
    correlation_id: str,
    decision: _DispatchDecision,
    timeout_seconds: int,
) -> tuple[tuple[ReviewedVerdict, ...], ReviewedEvaluation]:
    """Wait, bounded, for one provenance-stamped verdict per dispatched evaluator."""
    deadline = time.monotonic() + timeout_seconds
    intervals = _poll_intervals()
    while time.monotonic() < deadline:
        evaluation = _reviewed_chain(review, correlation_id)
        results = dispatched_verdicts(decision.receipt, evaluation.verdicts)
        if results is not None:
            return results, evaluation
        time.sleep(next(intervals))
    raise TimeoutError("evaluation_verdict_pending")


def results_are_content_free(
    evaluation: ReviewedEvaluation, protected_values: Iterable[str]
) -> bool:
    """Whether the returned verdicts hold none of the turn's protected values.

    Only the result projections are checked: the audit section carries the correlation and
    trace by design, and the chain names the writer's trace as its own provenance.
    """
    rendered = json.dumps(evaluation.results, sort_keys=True)
    return not any(value and value in rendered for value in protected_values)


def _evaluation_summary(
    decision: _DispatchDecision, results: tuple[ReviewedVerdict, ...]
) -> dict[str, object]:
    """Project the attested evaluation outcome into bounded, content-free summary fields."""
    dispatched = decision.receipt.outcome is EvaluationDeliveryOutcomeV1.DISPATCHED
    states = sorted({semantic_evaluation_result_state(result) for result in results})
    return {
        "evaluation_delivery_attested": True,
        "evaluation_release_attested": True,
        "evaluation_dispatch_outcome": decision.receipt.outcome.value,
        "evaluation_families": sorted(item.family.value for item in decision.receipt.families),
        "evaluation_writer_attested": decision.writer_trace_id is not None,
        "evaluation_result_attested": dispatched and bool(results),
        "evaluation_verdict_count": len(results),
        "evaluation_verdict_labels": evaluation_verdict_labels(results),
        "evaluation_provenance_attested": dispatched and bool(results),
        "evaluation_result_state": "+".join(states) if states else None,
    }


def _run_scenario(
    *,
    session: boto3.Session,
    endpoint: str,
    cookie: str,
    csrf: str,
    review: ReviewClient,
    client: Any,
    aggregate_table: str,
    target: _EvaluationTarget,
    evidence_dir: Path,
    scenario: Scenario,
) -> dict[str, object]:
    body = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": scenario.question,
            "session_id": f"agent-quality-{scenario.name}-{uuid.uuid4()}",
        },
        separators=(",", ":"),
    ).encode()
    # ADR-081 §2: the route mints the correlation and returns it; the probe sends none.
    status, chunks, correlation_id = call_with_correlation(
        session,
        f"{endpoint}/api/chat",
        "POST",
        body,
        {"Cookie": cookie, "x-adl-csrf": csrf, "Sec-Fetch-Site": "same-origin"},
    )
    if not correlation_id:
        raise _ProbeStageError("correlation_header")
    _write_raw(
        directory=evidence_dir,
        scenario=scenario,
        correlation_id=correlation_id,
        chunks=chunks,
    )
    if status != _HTTP_OK:
        raise ValueError("controlled conversation route returned a non-success status")
    try:
        terminal, events = _terminal(chunks)
    except TypeError, ValueError:
        raise _ProbeStageError("sse_terminal") from None
    refusal_reason_code: str | None = None
    if terminal == "refused":
        refusal = events[-1]
        if not isinstance(refusal, ConversationRefusedEventV4):
            raise _ProbeStageError("sse_terminal")
        refusal_reason_code = refusal.reason_code.value
    try:
        audit_record, findings_attested, finding_count, metric_attested = _wait_for_chain(
            review=review,
            client=client,
            aggregate_table=aggregate_table,
            correlation_id=correlation_id,
            expects_finding=scenario.expects_finding,
        )
    except (
        AdlyticsError,
        BotoCoreError,
        ClientError,
        ReviewUnavailableError,
        TypeError,
        ValueError,
    ):
        raise _ProbeStageError("audit_chain") from None
    except TimeoutError as error:
        raise _ProbeStageError(f"{terminal}_{error}") from None
    try:
        decision = _wait_for_dispatch_decision(
            review=review,
            correlation_id=correlation_id,
            record=audit_record,
            enabled=target.enabled,
        )
        results: tuple[ReviewedVerdict, ...] = ()
        if decision.receipt.outcome is EvaluationDeliveryOutcomeV1.DISPATCHED:
            results, evaluation = _wait_for_verdicts(
                review=review,
                correlation_id=correlation_id,
                decision=decision,
                timeout_seconds=target.verdict_timeout_seconds,
            )
            protected_values = (
                correlation_id,
                scenario.question,
                *(() if decision.writer_trace_id is None else (decision.writer_trace_id,)),
            )
            if not results_are_content_free(evaluation, protected_values):
                raise _ProbeStageError("evaluation_verdict_content")
    except AdlyticsError, ReviewUnavailableError, TypeError, ValueError:
        raise _ProbeStageError("evaluation_linkage") from None
    except TimeoutError as error:
        raise _ProbeStageError(f"{terminal}_{error}") from None
    return {
        "scenario": scenario.name,
        "terminal": terminal,
        "refusal_reason_code": refusal_reason_code,
        "audit_records": 1,
        "exact_receipt_attested": True,
        **_audit_receipt_summary(audit_record),
        "findings_attested": findings_attested,
        "finding_count": finding_count,
        "aggregate_attested": metric_attested,
        "evaluation_admission_attested": audit_record.evaluation_admission_ref is not None,
        "evaluation_deployment_attested": True,
        **_evaluation_summary(decision, results),
    }


def _parameter_value(client: Any, environment: str, key: str) -> str:
    response = client.get_parameter(Name=_parameter_path(environment, key))
    parameter = response.get("Parameter")
    if not isinstance(parameter, dict):
        raise ValueError("parameter lookup returned no parameter")
    value = parameter.get("Value")
    if not isinstance(value, str) or not value:
        raise ValueError("parameter lookup returned no value")
    return value


@dataclass(frozen=True, slots=True)
class _ResultLink:
    """The attested result-link stack: its ingress identity and the families it subscribes."""

    ingress_function: str
    families: frozenset[EvaluationFamilyV1]


def _subscribed_family(physical: object, environment: str) -> EvaluationFamilyV1 | None:
    """Return the current family whose ingress subscription this filter's physical ID names.

    The physical ID is compared as the filter name, or as the leading segment of a compound
    `name|group` identity, because the filter's primary identifier spans both.
    """
    if not isinstance(physical, str):
        return None
    name = physical.partition("|")[0]
    return next(
        (
            family
            for family in CURRENT_EVALUATION_FAMILIES
            if name == _subscription_filter_name(environment, family)
        ),
        None,
    )


def _attest_result_link_resources(
    resources: Sequence[Mapping[str, object]], environment: str
) -> _ResultLink:
    """Attest ADR-079's Logs-only ingress shape from one stack's exact resource records."""
    counts = Counter(
        resource.get("ResourceType")
        for resource in resources
        if isinstance(resource.get("ResourceType"), str)
    )
    for resource_type, expected_count in _RESULT_LINK_RESOURCE_COUNTS.items():
        if counts[resource_type] != expected_count:
            raise ValueError("result-link stack resource attestation failed")
    filters = [
        _subscribed_family(resource.get("PhysicalResourceId"), environment)
        for resource in resources
        if resource.get("ResourceType") == _SUBSCRIPTION_FILTER_TYPE
    ]
    families = frozenset(family for family in filters if family is not None)
    # Exactly one subscription per current family's owned group, and none for any other group:
    # a retired family's group is no longer subscribed.
    if len(filters) != len(CURRENT_EVALUATION_FAMILIES) or families != frozenset(
        CURRENT_EVALUATION_FAMILIES
    ):
        raise ValueError("result-link stack subscription attestation failed")
    ingress = [
        resource.get("PhysicalResourceId")
        for resource in resources
        if resource.get("ResourceType") == "AWS::Lambda::Function"
        and isinstance(resource.get("LogicalResourceId"), str)
        and cast("str", resource.get("LogicalResourceId")).startswith("Ingress")
    ]
    if len(ingress) != 1 or not isinstance(ingress[0], str) or not ingress[0]:
        raise ValueError("result-link stack ingress identity is invalid")
    return _ResultLink(ingress_function=ingress[0], families=families)


def _attest_result_link_stack(client: Any, stack_name: str, environment: str) -> _ResultLink:
    """Attest the caller-supplied stack; the probe never invokes the ingress it names."""
    if stack_name != _EXPECTED_RESULT_LINK_STACK:
        raise ValueError("result-link stack must be the exact dev result-link stack")
    if not _STACK_NAME_PATTERN.fullmatch(stack_name):
        raise ValueError("result-link stack name is invalid")
    resources: list[object] = list(stack_resource_summaries(client, stack_name))
    if not all(isinstance(resource, dict) for resource in resources):
        raise ValueError("result-link stack resource record is malformed")
    return _attest_result_link_resources(cast("list[Mapping[str, object]]", resources), environment)


def _read_enabled_releases(
    ssm: Any, environment: str, subscribed: frozenset[EvaluationFamilyV1]
) -> tuple[EvaluationReleaseV1, ...]:
    """Read each current family's release record once; an enabled one must be subscribed."""
    releases = SsmEvaluationReleaseReader(_release_parameter_names(environment), client=ssm).read()
    enabled = enabled_evaluation_releases(releases)
    if any(release.family not in subscribed for release in enabled):
        raise _ProbeStageError("evaluation_release_unsubscribed")
    return enabled


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """The attested dev chain one or more scenarios run through; built once per invocation."""

    session: boto3.Session
    endpoint: str
    cookie: str
    csrf: str
    review: ReviewClient
    client: Any
    aggregate_table: str
    target: _EvaluationTarget
    evidence_dir: Path


def prepare_target(args: argparse.Namespace) -> ProbeTarget:
    """Attest the account, the result link, and the release records, and bind the reviewer."""
    evidence_dir = _evidence_dir(args.evidence_dir)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    _account(session, args.expected_account)
    ssm = session.client("ssm")
    endpoint = require_https(
        _parameter_value(ssm, args.environment, "conversationFunctionUrl")
    ).rstrip("/")
    aggregate_table = _parameter_value(ssm, args.environment, "agentQualityAggregateTableName")
    result_link = _attest_result_link_stack(
        session.client("cloudformation"),
        args.result_link_stack,
        args.environment,
    )
    target = _EvaluationTarget(
        enabled=_read_enabled_releases(ssm, args.environment, result_link.families),
        verdict_timeout_seconds=args.verdict_timeout_seconds,
    )
    cookie, csrf = _cookies(session)
    return ProbeTarget(
        session=session,
        endpoint=endpoint,
        cookie=cookie,
        csrf=csrf,
        review=reviewer_client(session, args.environment, args.reviewer_persona),
        client=session.client("dynamodb"),
        aggregate_table=aggregate_table,
        target=target,
        evidence_dir=evidence_dir,
    )


def run_scenario(probe: ProbeTarget, scenario: Scenario) -> dict[str, object]:
    """Run one scenario through the attested chain and return its content-free summary.

    With a refresh cookie, each scenario takes a fresh access token: a run that waits on
    verdicts can outlive the 15-minute token it started with.
    """
    cookie = (
        _cookies(probe.session)[0] if os.environ.get("ADL_REFRESH", "").strip() else probe.cookie
    )
    return _run_scenario(
        session=probe.session,
        endpoint=probe.endpoint,
        cookie=cookie,
        csrf=probe.csrf,
        review=probe.review,
        client=probe.client,
        aggregate_table=probe.aggregate_table,
        target=probe.target,
        evidence_dir=probe.evidence_dir,
        scenario=scenario,
    )


class ProbeStageError(Exception):
    """A bounded, content-free reason the dev chain could not be attested."""


def run_scenario_marked(probe: ProbeTarget, scenario: Scenario) -> dict[str, object]:
    """Run one scenario; a chain that cannot be attested returns its fixed stage marker.

    For a runner that must score every question and report each, rather than stop at the first.
    """
    try:
        return run_scenario(probe, scenario)
    except _ProbeStageError as error:
        return {"scenario": scenario.name, "stage_error": str(error) or "unattested"}
    except TimeoutError:
        # Fixed probe timeouts arrive as stage errors; a bare one is the transport's, and its
        # text can name the Function URL host.
        return {"scenario": scenario.name, "stage_error": "timeout"}
    except BotoCoreError, ClientError, AdlyticsError, TypeError, ValueError:
        return {"scenario": scenario.name, "stage_error": "unattested"}
    except OSError:
        # requests' exceptions are OSError subclasses.
        return {"scenario": scenario.name, "stage_error": "transport"}
    except Exception as error:
        # The route stream is read from the raw urllib3 response, whose protocol errors are
        # not OSError; classify them by origin rather than by importing a transitive package.
        if type(error).__module__.split(".", 1)[0] in {"urllib3", "requests"}:
            return {"scenario": scenario.name, "stage_error": "transport"}
        raise


def prepare_attested(args: argparse.Namespace) -> ProbeTarget:
    """Prepare the target, mapping each internal failure to a content-free stage error."""
    try:
        return prepare_target(args)
    except _ProbeStageError as error:
        raise ProbeStageError(str(error)) from None
    except TimeoutError:
        raise ProbeStageError("timeout") from None
    except BotoCoreError, ClientError, AdlyticsError, TypeError, ValueError:
        raise ProbeStageError("") from None


def run_attested(
    args: argparse.Namespace, scenarios: Sequence[Scenario]
) -> list[dict[str, object]]:
    """Run every scenario, mapping each internal failure to a content-free stage error."""
    try:
        probe = prepare_target(args)
        return [run_scenario(probe, scenario) for scenario in scenarios]
    except (_ProbeStageError, TimeoutError) as error:
        raise ProbeStageError(str(error)) from None
    except (
        BotoCoreError,
        ClientError,
        AdlyticsError,
        ReviewUnavailableError,
        TypeError,
        ValueError,
    ):
        raise ProbeStageError("") from None


def main(argv: list[str] | None = None) -> int:
    """Run the fixed audit-chain scenarios and report bounded attestation results."""
    args = probe_parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("agent quality probe accepts only the approved dev region")
        return 2
    try:
        summaries = run_attested(args, _SCENARIOS)
    except ProbeStageError as error:
        detail = f": {error}" if str(error) else ""
        print(f"agent quality probe could not establish the bounded dev audit chain{detail}")
        return 1
    for summary in summaries:
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
