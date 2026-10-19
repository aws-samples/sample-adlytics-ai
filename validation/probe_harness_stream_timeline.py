#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Measure when each part of a managed Harness turn arrives on the wire.

    uv run python validation/probe_harness_stream_timeline.py --profile <profile> --env dev \
        --expected-account <account> --question route

**The question streamed answers rest on.** ADR-083 relays the model's Markdown answer to the
browser as the Harness delivers it, so what the user waits for is the first answer text, and what
they watch is its cadence. This probe measures when the first text delta arrives, how many deltas
and bytes the answer takes, the gaps between deltas, how long the model takes from its last
governed tool result to its first answer text, and when the turn ends.

**Records arrival, never content.** Every raw event is reduced to its kind, its elapsed time, the
logical tool name where one is declared by this repository, and a byte count. No question text
beyond the fictional prompts below, no tool arguments, no tool results, no answer text and no
identifiers leave this process. The stream is consumed directly from `InvokeHarness` so the
measurement is the service's own delivery, not the Conversation API's. The call presents the
synthetic dev chat persona's access token as its bearer (ADR-077 §7), because the Harness refuses
SigV4; the operator profile only reads the target and signs the persona in.

Dev only, one fictional actor, one fresh session per run. Nothing is written by this probe; the
governed tools it triggers are the deployed dev tools answering a fictional question.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import boto3
from cognito_personas import app_client, bearer_agentcore_client, load_persona, sign_in

_REGION = "us-east-1"
_BEARER_PERSONA = "probe-a"
_QUESTIONS = {
    "route": "Which declared metrics can this fictional workspace report?",
    "describe": "Explain retail_revenue_shipped.",
    "multi": (
        "First explain retail_revenue_shipped, then report retail_revenue_shipped by product in "
        "the United States marketplace on August 11, 2026."
    ),
    "none": "Hello. Please briefly introduce how you can help.",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", default="dev", dest="environment")
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--question", choices=sorted(_QUESTIONS), default="route")
    parser.add_argument("--output", help="Write the sanitized timeline JSON here.")
    return parser


def _logical(raw_name: str) -> str:
    return raw_name.rpartition("___")[2] or "unknown"


def _is_text_delta(row: Mapping[str, object]) -> bool:
    """Whether one reduced row is a delta of the model's own text, the streamed answer's source."""
    return (
        row.get("event") == "contentBlockDelta"
        and row.get("block") == "assistant"
        and row.get("member") == "text"
    )


class _Timeline:
    """Reduce raw Harness events to kinds, times, and sizes."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.rows: list[dict[str, object]] = []
        self.uses: dict[str, str] = {}
        self.active: dict[int, tuple[str, str]] = {}
        self.role: str | None = None

    def _at(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def accept(  # noqa: PLR0912, PLR0915 - one branch per stream member
        self, event: Mapping[str, object]
    ) -> None:
        keys = [key for key, value in event.items() if value is not None]
        kind = keys[0] if len(keys) == 1 else "|".join(keys)
        value = event.get(kind)
        row: dict[str, object] = {"at_ms": self._at(), "event": kind}
        if kind == "messageStart" and isinstance(value, Mapping):
            self.role = cast("str | None", value.get("role"))
            row["role"] = self.role
        elif kind == "contentBlockStart" and isinstance(value, Mapping):
            index = cast("int", value.get("contentBlockIndex"))
            start = cast("Mapping[str, object]", value.get("start") or {})
            use = cast("Mapping[str, object] | None", start.get("toolUse"))
            result = cast("Mapping[str, object] | None", start.get("toolResult"))
            if use is not None:
                name = _logical(str(use.get("name", "")))
                use_id = str(use.get("toolUseId", ""))
                self.uses[use_id] = name
                self.active[index] = ("use", use_id)
                row["block"] = "toolUse"
                row["tool"] = name
            elif result is not None:
                use_id = str(result.get("toolUseId", ""))
                self.active[index] = ("result", use_id)
                row["block"] = "toolResult"
                row["tool"] = self.uses.get(use_id, "unknown")
                row["status"] = result.get("status")
            else:
                self.active[index] = ("assistant", "")
                row["block"] = "assistant"
        elif kind == "contentBlockDelta" and isinstance(value, Mapping):
            index = cast("int", value.get("contentBlockIndex"))
            delta = cast("Mapping[str, object]", value.get("delta") or {})
            member = next((key for key, item in delta.items() if item is not None), "?")
            block_kind, use_id = self.active.get(index, ("assistant", ""))
            row["block"] = block_kind
            row["member"] = member
            if block_kind == "use":
                row["tool"] = self.uses.get(use_id, "unknown")
                tool_use = cast("Mapping[str, object]", delta.get("toolUse") or {})
                fragment = tool_use.get("input")
                row["bytes"] = len(str(fragment).encode()) if isinstance(fragment, str) else None
            elif block_kind == "result":
                row["tool"] = self.uses.get(use_id, "unknown")
                fragments = delta.get("toolResult")
                if isinstance(fragments, list):
                    row["bytes"] = len(json.dumps(fragments, default=str).encode())
            elif member == "text":
                row["bytes"] = len(str(delta.get("text", "")).encode())
            else:
                row["bytes"] = None
        elif kind == "contentBlockStop" and isinstance(value, Mapping):
            index = cast("int", value.get("contentBlockIndex"))
            block_kind, use_id = self.active.pop(index, ("assistant", ""))
            row["block"] = block_kind
            if use_id:
                row["tool"] = self.uses.get(use_id, "unknown")
        elif kind == "messageStop" and isinstance(value, Mapping):
            row["stop_reason"] = value.get("stopReason")
        self.rows.append(row)

    def summary(self) -> dict[str, object]:
        """Phase boundaries and the answer's text cadence, without the raw row list."""
        governed = [
            {"tool": row["tool"], "requested_at_ms": row["at_ms"]}
            for row in self.rows
            if row.get("event") == "contentBlockStart" and row.get("block") == "toolUse"
        ]
        results = [
            {"tool": row["tool"], "result_complete_at_ms": row["at_ms"]}
            for row in self.rows
            if row.get("event") == "contentBlockStop" and row.get("block") == "result"
        ]
        text_deltas = [row for row in self.rows if _is_text_delta(row)]
        end_turn = [
            row["at_ms"]
            for row in self.rows
            if row.get("event") == "messageStop" and row.get("stop_reason") == "end_turn"
        ]
        last_result = cast("int", results[-1]["result_complete_at_ms"]) if results else None
        # The answer is the text written after the last governed result; with no tool call, all.
        # Stream position decides "after", because two events can share a millisecond.
        result_rows = [
            index
            for index, row in enumerate(self.rows)
            if row.get("event") == "contentBlockStop" and row.get("block") == "result"
        ]
        after = result_rows[-1] if result_rows else -1
        answer = [row for row in self.rows[after + 1 :] if _is_text_delta(row)]
        first_answer = cast("int", answer[0]["at_ms"]) if answer else None
        gaps = [cast("int", b["at_ms"]) - cast("int", a["at_ms"]) for a, b in pairwise(text_deltas)]
        return {
            "events": len(self.rows),
            "first_event_at_ms": self.rows[0]["at_ms"] if self.rows else None,
            "governed_tool_requests": governed,
            "governed_tool_results": results,
            "text": {
                "first_delta_at_ms": text_deltas[0]["at_ms"] if text_deltas else None,
                "deltas": len(text_deltas),
                "bytes": sum(cast("int", row.get("bytes") or 0) for row in text_deltas),
                "delta_gap_ms_median_max": (
                    (sorted(gaps)[len(gaps) // 2], max(gaps)) if gaps else None
                ),
                "last_tool_result_at_ms": last_result,
                "first_answer_delta_at_ms": first_answer,
                "last_tool_result_to_first_answer_ms": (
                    None
                    if last_result is None or first_answer is None
                    else first_answer - last_result
                ),
            },
            "end_turn_at_ms": end_turn[0] if end_turn else None,
            "total_ms": self.rows[-1]["at_ms"] if self.rows else None,
        }


def main(argv: list[str] | None = None) -> int:
    """Run one fictional turn and print the sanitized arrival summary."""
    args = _parser().parse_args(argv)
    if args.environment != "dev" or args.region != _REGION:
        print("this probe runs only against the approved dev region")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    harness_arn = session.client("ssm").get_parameter(
        Name=f"/adlytics/{args.environment}/agent/harness-arn"
    )["Parameter"]["Value"]
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(session.client("ssm"), args.environment, _BEARER_PERSONA),
        app_client(session, args.environment),
    )
    client = bearer_agentcore_client(session, tokens.access_token, read_timeout=300)
    timeline = _Timeline()
    response: Any = client.invoke_harness(
        harnessArn=harness_arn,
        qualifier="DEFAULT",
        runtimeSessionId=f"validation-timeline-{uuid.uuid4().hex}",
        actorId="validation-timeline-actor",
        messages=[{"role": "user", "content": [{"text": _QUESTIONS[args.question]}]}],
    )
    stream = response["stream"]
    try:
        for event in stream:
            timeline.accept(cast("Mapping[str, object]", event))
    finally:
        stream.close()
    report = {"question": args.question, "summary": timeline.summary(), "rows": timeline.rows}
    print(json.dumps(report["summary"], indent=1))
    if args.output:
        with Path(args.output).open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
