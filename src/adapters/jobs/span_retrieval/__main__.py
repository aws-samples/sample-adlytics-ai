# SPDX-License-Identifier: MIT-0
"""The span-retrieval CLI: one read-only lookup on `aws/spans`, printed as one `RESULT` line.

Profile, region and environment are explicit and have no defaults; no identity is baked in. The
lookup is read-only (`StartQuery`, `GetQueryResults`).

    python -m adapters.jobs.span_retrieval --profile <PROFILE> --region us-east-1 --env dev \
        --window-start 2026-10-09T00:00:00Z --window-end 2026-10-09T01:00:00Z \
        (--trace-id <TRACE> | --key run_id=<RUN> ... | --span-name <NAME> ...)

Exit 0 prints a `complete`, `empty` or `pending` result; exit 3 a refused lookup; exit 1 a failed
query. See docs/capabilities/operational-data-flow-status/operator-tools.md, "Span retrieval CLI".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Final

import boto3

from adapters.jobs.span_retrieval.client import LogsInsightsSpanClient, LogsQueryClient
from adapters.jobs.span_retrieval.parse import SpanRetrievalFailedError
from adapters.jobs.span_retrieval.query import (
    DEFAULT_LAG_SECONDS,
    SpanQueryRefusedError,
    build_span_query,
    declared_span_attributes,
)

__all__ = ["main"]

REFUSED_EXIT: Final = 3
FAILED_EXIT: Final = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m adapters.jobs.span_retrieval")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--window-start", required=True, help="ISO-8601 with a UTC offset")
    parser.add_argument("--window-end", required=True, help="ISO-8601 with a UTC offset")
    parser.add_argument("--trace-id")
    parser.add_argument("--key", action="append", default=[], metavar="ATTRIBUTE=VALUE")
    parser.add_argument("--span-name", action="append", default=[])
    parser.add_argument("--span-name-prefix", action="append", default=[])
    parser.add_argument("--limit", type=int, default=1_000)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--lag-seconds", type=int, default=DEFAULT_LAG_SECONDS)
    return parser


def _print(result: dict[str, object]) -> None:
    print(f"RESULT {json.dumps(result, sort_keys=True, default=str)}")  # noqa: T201


def _instant(value: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SpanQueryRefusedError("window_malformed", f"{name} is not ISO-8601.") from None


def _keys(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for value in values:
        key, separator, text = value.partition("=")
        if not separator or not key or not text:
            raise SpanQueryRefusedError("key_malformed", "a key is ATTRIBUTE=VALUE.")
        pairs.append((key, text))
    return tuple(pairs)


def run(arguments: argparse.Namespace, logs: LogsQueryClient | None = None) -> int:
    """Build, run and print one lookup; `logs` replaces the profile's client in tests."""
    try:
        if str(arguments.profile).casefold() == "default":
            raise SpanQueryRefusedError("profile_not_explicit", "name a nondefault profile.")
        query = build_span_query(
            environment=str(arguments.env),
            window_start=_instant(str(arguments.window_start), "--window-start"),
            window_end=_instant(str(arguments.window_end), "--window-end"),
            declared_attributes=declared_span_attributes(),
            trace_id=arguments.trace_id,
            keys=_keys(list(arguments.key)),
            span_names=list(arguments.span_name),
            span_name_prefixes=list(arguments.span_name_prefix),
            limit=int(arguments.limit),
            max_pages=int(arguments.max_pages),
            lag_seconds=int(arguments.lag_seconds),
        )
    except SpanQueryRefusedError as error:
        _print({"refused": error.reason, "code": str(error.code)})
        return REFUSED_EXIT
    if logs is None:
        session = boto3.Session(
            profile_name=str(arguments.profile), region_name=str(arguments.region)
        )
        logs = session.client("logs")
    try:
        result = LogsInsightsSpanClient(logs).retrieve(query)
    except SpanRetrievalFailedError as error:
        _print({"failed": error.reason, "code": str(error.code)})
        return FAILED_EXIT
    _print({**result, "region": str(arguments.region)})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the explicit inputs and run one lookup; argparse exits 2 on a missing input."""
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
