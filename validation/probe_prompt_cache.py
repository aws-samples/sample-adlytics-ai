# SPDX-License-Identifier: MIT-0
"""Measure what Bedrock prompt caching saves on this model at our prompt size (dev only).

    uv run python validation/probe_prompt_cache.py --profile <profile> --expected-account <acct>

Calls `ConverseStream` directly with the operator's credentials, not through the managed
Harness: it answers how large the saving would be, not whether the Harness can use it. A
fictional prefix of about the size the Harness sends (system prompt plus tool schemas, ~6.5k
input tokens) is sent in pairs, without a cache checkpoint and with one; each call asks for a
one-word answer so time to first token is not confounded by output length. Prints only times
and the token counters Bedrock returns. Nothing about this platform's data is sent.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, cast

import boto3

_REGION = "us-east-1"
_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_PARAGRAPH = (
    "The fictional Harbor Lantern workspace tracks lantern deliveries across seven imaginary "
    "harbors. Each harbor keeps a daily ledger of lanterns shipped, lanterns returned, and "
    "lanterns lit after dusk, and every figure carries its coverage window, its freshness, and "
    "whether any day was withheld. A careful assistant states each qualifier with the figure. "
)


def _prefix(paragraphs: int) -> str:
    """Build a fictional system prompt; a per-run salt keeps each run's cache separate."""
    salt = uuid.uuid4().hex
    return f"Run {salt}.\n" + "\n".join(
        f"Section {index}. {_PARAGRAPH}" for index in range(paragraphs)
    )


def _call(client: Any, system: list[Any], question: str) -> dict[str, int | None]:
    started = time.monotonic()
    response = client.converse_stream(
        modelId=_MODEL,
        system=system,
        messages=[{"role": "user", "content": [{"text": question}]}],
        inferenceConfig={"maxTokens": 8, "temperature": 0},
    )
    first: float | None = None
    usage: dict[str, int] = {}
    latency: int | None = None
    for event in cast("Iterable[Mapping[str, Any]]", response["stream"]):
        if "contentBlockDelta" in event and first is None:
            first = time.monotonic() - started
        if "metadata" in event:
            metadata = cast("Mapping[str, Mapping[str, object]]", event["metadata"])
            usage = {
                str(key): value
                for key, value in metadata.get("usage", {}).items()
                if isinstance(value, int)
            }
            reported = metadata.get("metrics", {}).get("latencyMs")
            latency = reported if isinstance(reported, int) else None
    return {
        "ttft_ms": None if first is None else round(first * 1000),
        "total_ms": round((time.monotonic() - started) * 1000),
        "provider_latency_ms": latency,
        **usage,
    }


def _median(values: Iterable[int | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def main(argv: list[str] | None = None) -> int:
    """Run the paired calls and print the sanitized timing and cache counters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--pairs", type=int, default=4)
    parser.add_argument("--paragraphs", type=int, default=90)
    args = parser.parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=_REGION)
    if session.client("sts").get_caller_identity()["Account"] != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    client = session.client("bedrock-runtime")
    results: dict[str, list[dict[str, int | None]]] = {"uncached": [], "write": [], "read": []}
    for _ in range(args.pairs):
        text = _prefix(args.paragraphs)
        plain: list[Any] = [{"text": text}]
        cached: list[Any] = [{"text": text}, {"cachePoint": {"type": "default"}}]
        results["write"].append(_call(client, cached, "Reply with the word ready."))
        # The uncached and cache-read calls run in random order, so drift in Bedrock's own
        # latency during the run cannot masquerade as a caching effect.
        order = ["uncached", "read"]
        random.shuffle(order)
        for kind in order:
            system = plain if kind == "uncached" else cached
            results[kind].append(_call(client, system, "Reply with the word yes."))
        time.sleep(2)
    summary = {
        kind: {
            "ttft_ms_median": _median(row["ttft_ms"] for row in rows),
            "ttft_ms": [row["ttft_ms"] for row in rows],
            "provider_latency_ms_median": _median(row["provider_latency_ms"] for row in rows),
            "input_tokens": [row.get("inputTokens") for row in rows],
            "cache_write": [row.get("cacheWriteInputTokens") for row in rows],
            "cache_read": [row.get("cacheReadInputTokens") for row in rows],
        }
        for kind, rows in results.items()
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
