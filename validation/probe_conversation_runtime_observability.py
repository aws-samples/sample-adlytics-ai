# SPDX-License-Identifier: MIT-0
"""Diagnose one guarded Conversation measurement refusal through its exact audit receipt.

The probe sends one natural-language fictional customer request aligned to the deployed synthetic
integration fixture through the development Conversation API. It writes the server-minted
correlation the route returned (ADR-081 §2) and
its exact, content-minimized Runtime audit receipt only beneath ignored local validation evidence.
It never prints request payloads, cookies, session identifiers, correlation identifiers, trace
identifiers, opaque receipt references, or stream content. Standard output contains only bounded
audit terminal, publication, operation, admission, and failure classifications.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from probe_conversation_route import (
    _refreshed_session,  # pyright: ignore[reportPrivateUsage]
    call_with_correlation,
)
from quality_review_client import (
    ReviewedReceipt,
    ReviewUnavailableError,
    reviewed_receipt,
    reviewer_client,
)

from adapters.inbound.conversation.contract_codec import decode_public_conversation_stream_event
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from domain.conversation.answer_stream import (
    ConversationRefusedEventV4,
    ConversationStreamEventV4,
    validate_conversation_stream,
)
from domain.shared_kernel.errors import AdlyticsError

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_ROOT = (_ROOT / "validation" / "evidence").resolve()
_API_STACK = "Adlytics-Api-dev"
_RUNTIME_STACK = "Adlytics-AgentRuntime-dev"
_AGENT_STACK = "Adlytics-Agent-dev"
_QUALITY_STACK = "Adlytics-AgentQuality-dev"
_REQUIRED_ENVIRONMENT = "dev"
_REQUIRED_REGION = "us-east-1"
_ACCOUNT_ID_LENGTH = 12
_HTTP_OK = 200
_AUDIT_RECEIPT_POLL_SECONDS = 60
_AUDIT_RECEIPT_POLL_INTERVAL_SECONDS = 2
# This natural-language request is limited to the deployed synthetic integration fixture's
# known shipped-revenue grain, marketplace, and date. The measurement request contract supplies
# reporting-lens defaults; directing the model to use a tool or plan would not be customer language.
_FICTIONAL_MEASUREMENT_QUESTION = (
    "What was the shipped revenue by ASIN in the United States marketplace on August 11, 2026?"
)


@dataclass(frozen=True, slots=True)
class _RouteProbeResult:
    """One route result plus the locally retained correlation needed for exact audit retrieval."""

    status: int
    terminal: str
    correlation_id: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--api-stack", required=True)
    parser.add_argument("--runtime-stack", required=True)
    parser.add_argument("--agent-stack", required=True)
    parser.add_argument("--quality-stack", required=True)
    parser.add_argument("--evidence-dir", required=True)
    return parser


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


def _validate_target(args: argparse.Namespace) -> None:
    if args.environment != _REQUIRED_ENVIRONMENT:
        raise ValueError("Conversation Runtime observability probe accepts only environment dev")
    if args.region != _REQUIRED_REGION:
        raise ValueError("Conversation Runtime observability probe accepts only region us-east-1")
    if (
        args.api_stack != _API_STACK
        or args.runtime_stack != _RUNTIME_STACK
        or args.agent_stack != _AGENT_STACK
        or args.quality_stack != _QUALITY_STACK
    ):
        raise ValueError("Conversation Runtime observability probe requires exact dev stacks")
    if not args.expected_account.isdecimal() or len(args.expected_account) != _ACCOUNT_ID_LENGTH:
        raise ValueError("expected account must be a 12-digit account identifier")


def _account(session: boto3.Session, expected_account: str) -> None:
    """Make STS the first AWS call and reject an explicitly mismatched profile."""
    account = session.client("sts").get_caller_identity().get("Account")
    if account != expected_account:
        raise ValueError("explicit profile did not resolve to the expected account")


def _read_deployment_values(session: boto3.Session, args: argparse.Namespace) -> str:
    cloudformation = session.client("cloudformation")
    for stack_name in (
        args.api_stack,
        args.runtime_stack,
        args.agent_stack,
        args.quality_stack,
    ):
        # Existence only: a missing or unreadable stack raises here and stops the probe.
        stack_resource_summaries(cloudformation, stack_name)
    ssm = session.client("ssm")
    route_url = cast(
        "object",
        ssm.get_parameter(Name=_parameter_path(args.environment, "conversationFunctionUrl"))
        .get("Parameter", {})
        .get("Value"),
    )
    if not isinstance(route_url, str) or not route_url.startswith("https://"):
        raise ValueError("published Conversation API URL is malformed")
    return route_url.rstrip("/")


def _cookies(session: boto3.Session) -> tuple[str, str]:
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    access = os.environ.get("ADL_SESSION", "").strip()
    if refresh:
        if not csrf:
            raise ValueError("ADL_CSRF is required with ADL_REFRESH")
        # The shared helper emits a user-facing message; suppress it because it concerns a cookie.
        with contextlib.redirect_stdout(io.StringIO()):
            access = _refreshed_session(session, refresh, csrf)
    if not access:
        raise ValueError("a local ADL_REFRESH or ADL_SESSION is required")
    cookie = f"adl_session={access}"
    if csrf:
        cookie += f"; adl_csrf={csrf}"
    return cookie, csrf


def _terminal(chunks: list[tuple[float, bytes]]) -> str:
    frames = b"".join(raw for _, raw in chunks).decode("utf-8", errors="strict").split("\n\n")
    events: list[ConversationStreamEventV4] = []
    for frame in frames:
        if not frame.strip():
            continue
        data = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data or any(not line.startswith("data:") for line in frame.splitlines()):
            raise ValueError("Conversation API returned a malformed SSE frame")
        events.append(decode_public_conversation_stream_event("\n".join(data)))
    if validate_conversation_stream(tuple(events), closed=True) is not None:
        return "response_done"
    if events and isinstance(events[-1], ConversationRefusedEventV4):
        return "refused"
    raise ValueError("Conversation API did not return a legal V4 terminal")


def _write_transport_artifacts(
    evidence_dir: Path, status: int, chunks: list[tuple[float, bytes]]
) -> tuple[Path, Path]:
    """Persist raw route bytes and timing only until strict terminal parsing succeeds."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_path = evidence_dir / "conversation-runtime-observability-response.sse"
    transport_path = evidence_dir / "conversation-runtime-observability-transport.json"
    raw_path.write_bytes(b"".join(raw for _, raw in chunks))
    transport_path.write_text(
        json.dumps(
            {
                "status": status,
                "chunks": [
                    {"arrival_ms": round(elapsed * 1_000, 3), "size_bytes": len(raw)}
                    for elapsed, raw in chunks
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return raw_path, transport_path


def _terminal_with_failure_evidence(
    evidence_dir: Path, status: int, chunks: list[tuple[float, bytes]]
) -> str:
    """Write protected transport evidence before parsing; discard it after a valid terminal."""
    artifacts = _write_transport_artifacts(evidence_dir, status, chunks)
    try:
        terminal = _terminal(chunks)
    except TypeError, UnicodeError, ValueError:
        # The ignored artifacts are the only record of a structural stream failure.
        raise
    for artifact in artifacts:
        artifact.unlink()
    return terminal


def _write_audit_evidence(
    evidence_dir: Path,
    correlation_id: str,
    receipt: ReviewedReceipt | None = None,
) -> None:
    """Keep correlation and opaque receipt material solely in ignored local evidence."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    content: dict[str, object] = {"correlation_id": correlation_id}
    if receipt is not None:
        content["receipt"] = asdict(receipt)
    (evidence_dir / "conversation-runtime-observability-audit.json").write_text(
        json.dumps(content, indent=2, sort_keys=True), encoding="utf-8"
    )


def _route_probe(session: boto3.Session, route_url: str, evidence_dir: Path) -> _RouteProbeResult:
    cookie, csrf = _cookies(session)
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": _FICTIONAL_MEASUREMENT_QUESTION,
            "session_id": f"conversation-runtime-observability-{uuid.uuid4()}",
        }
    ).encode()
    # ADR-081 §2: the route mints the correlation and returns it; the probe sends none.
    status, chunks, correlation_id = call_with_correlation(
        session,
        f"{route_url}/api/chat",
        "POST",
        payload,
        {"Cookie": cookie, "x-adl-csrf": csrf, "Sec-Fetch-Site": "same-origin"},
    )
    _write_audit_evidence(evidence_dir, correlation_id)
    return _RouteProbeResult(
        status=status,
        terminal=_terminal_with_failure_evidence(evidence_dir, status, chunks),
        correlation_id=correlation_id,
    )


class _TurnReviewer(Protocol):
    """The one review read this development-only diagnostic needs (ADR-078 §5)."""

    def turn(self, correlation_id: str) -> dict[str, object] | None: ...


def _exact_audit_receipt(review: _TurnReviewer, correlation_id: str) -> ReviewedReceipt:
    """Read the turn's receipt through the review API, which admits exactly one per correlation."""
    deadline = time.monotonic() + _AUDIT_RECEIPT_POLL_SECONDS
    while time.monotonic() < deadline:
        document = review.turn(correlation_id)
        if document is not None:
            receipt = reviewed_receipt(document)
            if receipt.correlation_id != correlation_id:
                raise ValueError("review returned a receipt for another correlation")
            return receipt
        time.sleep(_AUDIT_RECEIPT_POLL_INTERVAL_SECONDS)
    raise TimeoutError("audit_receipt_absent")


def _audit_classifications(receipt: ReviewedReceipt) -> dict[str, object]:
    """Project only the bounded classifications allowed outside ignored local evidence."""
    return {
        "terminal_outcome": receipt.terminal_outcome.value,
        "publication_outcome": receipt.publication_outcome.value,
        "operation_category": receipt.operation_category.value,
        "operation_outcome": receipt.operation_outcome.value,
        "step_dispositions": [step.disposition.value for step in receipt.trajectory],
        "failure_error_code": receipt.failure_error_code,
        "completion_refusal_stage": receipt.completion_refusal_stage,
    }


def _sanitized_summary(receipt: ReviewedReceipt) -> dict[str, object]:
    """Return the approved classification-only public projection for the exact audit receipt."""
    return {"audit": _audit_classifications(receipt)}


def _write_summary(evidence_dir: Path, summary: dict[str, object]) -> None:
    """Persist only bounded audit classifications, never transport or opaque receipt material."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "conversation-runtime-observability-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the exact guarded Conversation refusal and exact Runtime audit receipt diagnostic."""
    args = _parser().parse_args(argv)
    stage = "target_validation"
    try:
        _validate_target(args)
        evidence_dir = _evidence_dir(args.evidence_dir)
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        stage = "identity_verification"
        _account(session, args.expected_account)
        stage = "deployment_resolution"
        route_url = _read_deployment_values(session, args)
        stage = "route_terminal"
        route = _route_probe(session, route_url, evidence_dir)
        stage = "audit_receipt"
        review = reviewer_client(session, args.environment)
        receipt = _exact_audit_receipt(review, route.correlation_id)
        _write_audit_evidence(evidence_dir, route.correlation_id, receipt)
        stage = "summary_projection"
        summary = _sanitized_summary(receipt)
        stage = "summary_persistence"
        _write_summary(evidence_dir, summary)
    except (
        AdlyticsError,
        BotoCoreError,
        ClientError,
        ReviewUnavailableError,
        TimeoutError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        print(
            "Conversation Runtime observability probe could not establish bounded dev evidence at "
            f"{stage}: {type(error).__name__}"
        )
        return 2
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0 if route.status == _HTTP_OK else 1


if __name__ == "__main__":
    sys.exit(main())
