#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Accept the streamed Markdown answer in dev (ADR-083).

    uv run python validation/probe_streamed_answer.py --profile <profile> --environment dev \
        --region us-east-1 --expected-account <account> [--evidence-dir <dir>]

Sends each fictional question below through the deployed same-origin chat route as the synthetic
`probe-a` persona, then reads the turn's receipt through the review API. For each turn it reports:

- arrival milestones in milliseconds after the request: first event, first text delta, first
  answer text after the last tool call, and the terminal;
- whether the text arrived progressively: how many text deltas, and over how many distinct
  network chunks and how much time they were spread;
- whether the receipt is the streamed shape, counts exactly the released characters, and is the
  receipt the `done` event names; and whether a complete turn was admitted to evaluation.

Prints milestones, counts, and booleans only. The streamed text, correlations, and references are
written only to the ignored evidence directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import boto3
from accept_managed_harness import (
    _decoded_frames,  # pyright: ignore[reportPrivateUsage]
    _same_origin_call,  # pyright: ignore[reportPrivateUsage]
    streamed_receipt_mismatch,
)
from cognito_personas import app_client, load_persona, published_parameter, sign_in
from quality_review_client import (
    DEFAULT_REVIEWER_PERSONA,
    ReviewClient,
    ReviewedReceipt,
    reviewed_receipt,
    reviewer_client,
)

from adapters.outbound.https import HttpsSession, require_https
from domain.conversation.answer_stream import (
    ConversationDoneEventV4,
    ConversationRefusedEventV4,
    ConversationTextEventV4,
    ConversationToolEventV4,
    validate_conversation_stream,
)

_REGION = "us-east-1"
_QUESTIONS = {
    "greeting": "Hello. Briefly, what can you help me with here?",
    "catalog": "Which declared metrics can this fictional workspace report? Put them in a table.",
    "measurement": (
        "Report retail_revenue_shipped by product in the United States marketplace on "
        "August 11, 2026."
    ),
}
_REVIEW_TIMEOUT_SECONDS = 180
_POLL_SECONDS = 10


@dataclass(frozen=True, slots=True)
class _TurnFacts:
    """Sanitized facts about one streamed turn."""

    question: str
    http_status: int
    terminal: str | None
    first_event_ms: int | None
    first_text_ms: int | None
    first_answer_text_ms: int | None
    terminal_ms: int | None
    text_deltas: int
    text_chunks: int
    text_spread_ms: int | None
    tool_calls: int
    receipt_found: bool
    receipt_mismatch: str | None
    reference_equals_receipt: bool
    evaluation_admitted: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--evidence-dir", default="")
    parser.add_argument(
        "--disconnect",
        action="store_true",
        help="also close one turn's connection after its first text event",
    )
    return parser


def _receipt(review: ReviewClient, correlation_id: str) -> ReviewedReceipt | None:
    deadline = time.monotonic() + _REVIEW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = review.turn(correlation_id)
        if document is not None:
            return reviewed_receipt(document)
        time.sleep(_POLL_SECONDS)
    return None


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


def _turn(
    label: str, *, origin: str, token: str, csrf: str, review: ReviewClient
) -> tuple[_TurnFacts, dict[str, object]]:
    status, chunks, correlation_id, _, _ = _same_origin_call(
        origin=origin,
        token=token,
        csrf=csrf,
        question=_QUESTIONS[label],
        session_id=f"streamed-answer-{uuid.uuid4()}",
    )
    events, _ = _decoded_frames(chunks)
    texts = [(at, event) for at, event in events if isinstance(event, ConversationTextEventV4)]
    tools = [(at, event) for at, event in events if isinstance(event, ConversationToolEventV4)]
    terminal = next(
        (
            (at, event)
            for at, event in events
            if isinstance(event, ConversationDoneEventV4 | ConversationRefusedEventV4)
        ),
        None,
    )
    released = validate_conversation_stream(tuple(event for _, event in events), closed=True)
    last_tool_at = max((at for at, _ in tools), default=None)
    answer_texts = [at for at, _ in texts if last_tool_at is None or at > last_tool_at]
    receipt = _receipt(review, correlation_id) if correlation_id else None
    done = terminal[1] if terminal and isinstance(terminal[1], ConversationDoneEventV4) else None
    facts = _TurnFacts(
        question=label,
        http_status=status,
        terminal=None if terminal is None else terminal[1].kind,
        first_event_ms=_ms(events[0][0]) if events else None,
        first_text_ms=_ms(texts[0][0]) if texts else None,
        first_answer_text_ms=_ms(answer_texts[0]) if answer_texts else None,
        terminal_ms=_ms(terminal[0]) if terminal else None,
        text_deltas=len(texts),
        text_chunks=len({at for at, _ in texts}),
        text_spread_ms=_ms(texts[-1][0] - texts[0][0]) if texts else None,
        tool_calls=sum(1 for _, event in tools if event.status.value == "requested"),
        receipt_found=receipt is not None,
        receipt_mismatch=(
            None
            if receipt is None or released is None
            else streamed_receipt_mismatch(receipt, released)
        ),
        reference_equals_receipt=(
            done is not None and receipt is not None and done.audit_id == receipt.audit_id
        ),
        evaluation_admitted=receipt is not None and receipt.evaluation_admission_ref is not None,
    )
    evidence: dict[str, object] = {
        "question": label,
        "correlation_id": correlation_id,
        "audit_id": None if receipt is None else receipt.audit_id,
        "released_text": released,
    }
    return facts, evidence


def _disconnect(*, origin: str, token: str, csrf: str, review: ReviewClient) -> dict[str, object]:
    """Close the connection after the first text event and report what the receipt recorded."""
    payload = json.dumps(
        {
            "schema_version": "ConversationRequest.v3",
            "question": _QUESTIONS["catalog"],
            "session_id": f"streamed-answer-{uuid.uuid4()}",
        },
        separators=(",", ":"),
    ).encode()
    buffered = b""
    with HttpsSession().request(
        "POST",
        require_https(f"{origin}/api/chat"),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "Cookie": f"adl_session={token}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=180,
        stream=True,
    ) as response:
        correlation_id = response.headers.get("x-correlation-id", "")
        while b'"kind":"text"' not in buffered:
            chunk = response.raw.read1(8192) if hasattr(response.raw, "read1") else b""
            if not chunk:
                break
            buffered += chunk
        # Leaving the `with` block closes the connection mid-stream.
    receipt = _receipt(review, correlation_id) if correlation_id else None
    return {
        "saw_text_before_closing": b'"kind":"text"' in buffered,
        "receipt_found": receipt is not None,
        "terminal_outcome": None if receipt is None else receipt.terminal_outcome.value,
        "released_character_count": None if receipt is None else receipt.released_character_count,
        "trajectory_steps": None if receipt is None else len(receipt.trajectory),
        "evaluation_admitted": receipt is not None and receipt.evaluation_admission_ref is not None,
    }


def main(argv: list[str] | None = None) -> int:
    """Run each fictional question and print the sanitized streaming facts."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("this probe runs only against the approved dev region")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    ssm = session.client("ssm")
    origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(ssm, args.environment, "probe-a"),
        app_client(session, args.environment),
    )
    csrf = secrets.token_urlsafe(24)
    review = reviewer_client(session, args.environment, DEFAULT_REVIEWER_PERSONA)
    results: list[_TurnFacts] = []
    evidence: list[dict[str, object]] = []
    for label in _QUESTIONS:
        facts, raw = _turn(
            label, origin=origin, token=tokens.access_token, csrf=csrf, review=review
        )
        results.append(facts)
        evidence.append(raw)
    disconnect = (
        _disconnect(origin=origin, token=tokens.access_token, csrf=csrf, review=review)
        if args.disconnect
        else None
    )
    # The disconnect case is an observation: the edge may keep reading the origin after the
    # viewer leaves, in which case the route completes the turn and records it as complete.
    passed = all(
        item.http_status == 200  # noqa: PLR2004 - HTTP OK
        and item.terminal == "done"
        and item.text_chunks > 1
        and item.receipt_found
        and item.receipt_mismatch is None
        and item.reference_equals_receipt
        and item.evaluation_admitted
        for item in results
    )
    print(
        json.dumps(
            {
                "turns": [asdict(item) for item in results],
                "disconnect": disconnect,
                "passed": passed,
            },
            indent=1,
        )
    )
    if args.evidence_dir:
        directory = Path(args.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "streamed-answer.json").write_text(
            json.dumps(evidence, indent=1), encoding="utf-8"
        )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
