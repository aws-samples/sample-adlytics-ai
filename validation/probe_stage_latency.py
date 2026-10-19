# SPDX-License-Identifier: MIT-0
"""Produce the per-stage processing-latency picture for one deployed environment.

One disputed fact per probe: where the time goes across the processing path — ingestion
(raw landing to publication), insight precompute, bake (source and persist stages), and
the serve path — over one explicit window. Every target-affecting input arrives as an
argument; nothing is baked in. Output is aggregate figures only: counts and percentile
durations, never identifiers, names, or measured values.

Usage:
    uv run python validation/probe_stage_latency.py --profile <PROFILE> --env <ENV> --hours 24
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import boto3

_STAGES: tuple[tuple[str, str, str], ...] = (
    # (stage label, log group suffix, Logs Insights query)
    (
        "ingestion (per delivery)",
        "IngestionFunction",
        "filter @type = 'REPORT' | stats count(*) as n, "
        "percentile(@duration, 50) as p50_ms, percentile(@duration, 95) as p95_ms, "
        "max(@duration) as max_ms",
    ),
    (
        "insight precompute (per run)",
        "InsightPrecompute",
        "filter @type = 'REPORT' | stats count(*) as n, "
        "percentile(@duration, 50) as p50_ms, percentile(@duration, 95) as p95_ms, "
        "max(@duration) as max_ms",
    ),
    (
        "bake · source stage (resolve+snapshot+measure+render)",
        "PresetArtifactBaker",
        "filter message = 'preset artifact bake finished' | stats count(*) as n, "
        "percentile(source_ms, 50) as p50_ms, percentile(source_ms, 95) as p95_ms, "
        "max(source_ms) as max_ms by page, variant, outcome",
    ),
    (
        "bake · persist stage (body+custody+pointer)",
        "PresetArtifactBaker",
        "filter message = 'preset artifact bake finished' and outcome = 'published' | "
        "stats count(*) as n, percentile(persist_ms, 50) as p50_ms, "
        "percentile(persist_ms, 95) as p95_ms, max(persist_ms) as max_ms by page, variant",
    ),
    (
        "serve path (per operation)",
        "PresentationFunction",
        "filter message = 'operation lifecycle' and phase = 'terminal' | "
        "stats count(*) as n, percentile(duration_ms, 50) as p50_ms, "
        "percentile(duration_ms, 95) as p95_ms, max(duration_ms) as max_ms by operation",
    ),
)


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Explicit operator inputs; the probe carries no identity or environment of its own."""

    profile: str
    env: str
    hours: int


@dataclass(frozen=True, slots=True)
class _StageQuery:
    """One stage's log group, Logs Insights query, and explicit window."""

    group: str
    query: str
    hours: int


def _run_query(client: object, request: _StageQuery) -> list[list[dict[str, str]]]:
    end = int(time.time())
    start = end - request.hours * 3600
    started = client.start_query(  # type: ignore[attr-defined]
        logGroupName=request.group, startTime=start, endTime=end, queryString=request.query
    )
    query_id = started["queryId"]
    while True:
        result = client.get_query_results(queryId=query_id)  # type: ignore[attr-defined]
        if result["status"] in {"Complete", "Failed", "Cancelled", "Timeout"}:
            if result["status"] != "Complete":
                raise RuntimeError(f"Logs Insights query {result['status']} for {request.group}.")
            return result["results"]
        time.sleep(1)


def _render(rows: list[list[dict[str, str]]]) -> str:
    if not rows:
        return "    (no events in the window)"
    lines: list[str] = []
    for row in rows:
        fields = {item["field"]: item["value"] for item in row if item["field"] != "@ptr"}
        lines.append("    " + " · ".join(f"{key}={value}" for key, value in fields.items()))
    return "\n".join(lines)


def main() -> int:
    """Run every stage query and print the aggregate per-stage latency picture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--hours", type=int, default=24)
    arguments = parser.parse_args()
    request = ProbeRequest(arguments.profile, arguments.env, arguments.hours)
    if request.hours < 1 or request.hours > 24 * 14:
        raise SystemExit("--hours must be between 1 and 336.")
    session = boto3.Session(profile_name=request.profile)
    client = session.client("logs")
    print(f"per-stage latency picture · env={request.env} · window={request.hours}h")
    for label, suffix, query in _STAGES:
        group = f"/aws/lambda/adlytics-{request.env}-{suffix}"
        print(f"  {label} [{group}]")
        try:
            print(_render(_run_query(client, _StageQuery(group, query, request.hours))))
        except client.exceptions.ResourceNotFoundException:
            print("    (log group absent in this environment)")
    print(
        "reprocess stage: operator-run job; read its terminal acceptance from the detached "
        "log named in validation/README.md — it is not a Lambda and has no log group here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
