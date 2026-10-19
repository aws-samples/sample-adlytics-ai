#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""ADR-077 probe P4: the latency the identity chain adds to a turn, against declared budgets.

    uv run python validation/probe_identity_latency.py --profile <profile> --environment dev \
        --region us-east-1 --expected-account <account> [--turns 6]

**What it settles.** ADR-077's Consequences say every turn adds one route exchange and every tool
call may add an agent exchange and an interceptor invocation, and that P4 measures that added
latency against warm and cold budgets declared before it runs. `BUDGETS` below is that
declaration and the source of truth for it.

**How.** The `probe-a` persona drives `--turns` tool turns through `/api/chat`, one after another
(the first may meet cold functions). The probe then reads the CloudWatch `REPORT` lines in the
run's window for the three identity-chain functions:

- the route exchange: invocations whose terminal record ended `exchanged`;
- the issuer: only `POST /token` invocations, correlated by the request id of each terminal
  record that ended `issued` (the issuer also serves discovery and the key set);
- the Gateway interceptor: only invocations whose terminal record ended `forwarded`, which is
  the one outcome a `tools/call` carrying a bearer reaches (every other method passes through).

Per function it prints the invocation count, the cold count (`Init Duration` present), the warm
p50 (nearest rank, so the value is one observed `Duration`) and max `Duration`, the cold max of
`Duration + Init Duration`, and the exchanges per turn. Every number printed is read from the
logs; none is estimated. Each budget is PASS or FAIL. A cold budget with no cold invocation is
NO-SAMPLE (every function may have been warm), which is neither. A warm budget with no warm
invocation is FAIL once any turn completed, because every completed turn exchanges and calls
tools; it is NO-SAMPLE only when no turn completed. The run also fails when the logs do not
show one route exchange per completed turn before the log deadline. The exit status is non-zero
on any FAIL, on unsettled logs, or on a turn that did not complete. Sanitized evidence
(durations and counts; no token, password, or request id) is written to
`validation/evidence/identity-probes/<timestamp>-latency.json`, which is gitignored.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import boto3
from cognito_personas import app_client, load_persona, published_parameter, sign_in
from probe_identity_chain import (
    TOOL_QUESTION,
    LogWindow,
    chat_turn,
    deployed_function_name,
    log_messages,
    now_ms,
    terminal_records,
)

from adapters.outbound.https import require_https

_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE_DIR = _ROOT / "validation" / "evidence" / "identity-probes"
_REQUIRED_ENVIRONMENT = "dev"
_REQUIRED_REGION = "us-east-1"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_PERSONA = "probe-a"
_DEFAULT_TURNS = 6
_MAX_TURNS = 20
_REPORT_PATTERN = '"REPORT RequestId"'
_REPORT_ID = re.compile(r"REPORT RequestId: (\S+)")
_DURATION = re.compile(r"\tDuration: ([0-9.]+) ms")
_INIT_DURATION = re.compile(r"\tInit Duration: ([0-9.]+) ms")
_LOG_POLL_SECONDS = 15
_LOG_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Budget:
    """One function's declared budgets, in milliseconds.

    `terminal_outcome` selects which invocations count: only those whose terminal record ended
    with it, correlated by request id. `None` counts every invocation in the window.
    """

    label: str
    logical_id: str
    terminal_outcome: str | None
    warm_p50_ms: Decimal
    cold_max_ms: Decimal


# Declared by the owner-facing run of 2026-09-26, before P4 was first run. Cold is the whole
# cold invocation: Duration plus Init Duration.
BUDGETS: tuple[Budget, ...] = (
    Budget("route exchange", "TokenRouteExchange", "exchanged", Decimal(150), Decimal(2500)),
    Budget("/token", "TokenIssuer", "issued", Decimal(200), Decimal(2500)),
    Budget("interceptor", "GatewayInterceptor", "forwarded", Decimal(50), Decimal(1500)),
)


@dataclass(frozen=True, slots=True)
class Invocation:
    """One `REPORT` line, reduced to its durations."""

    duration_ms: Decimal
    init_ms: Decimal | None

    @property
    def cold(self) -> bool:
        """Whether this invocation paid an init phase."""
        return self.init_ms is not None

    @property
    def total_ms(self) -> Decimal:
        """Duration plus Init Duration, the whole of a cold invocation."""
        return self.duration_ms + (self.init_ms or Decimal(0))


@dataclass(frozen=True, slots=True)
class FunctionLatency:
    """What one function's `REPORT` lines in the window say."""

    label: str
    invocations: tuple[Invocation, ...]

    @property
    def warm(self) -> list[Decimal]:
        """Warm durations, ascending."""
        return sorted(item.duration_ms for item in self.invocations if not item.cold)

    @property
    def cold(self) -> list[Decimal]:
        """Cold whole-invocation durations, ascending."""
        return sorted(item.total_ms for item in self.invocations if item.cold)

    @property
    def warm_p50(self) -> Decimal | None:
        """Nearest-rank median of the warm durations: always one observed value."""
        warm = self.warm
        return warm[math.ceil(len(warm) / 2) - 1] if warm else None

    @property
    def warm_max(self) -> Decimal | None:
        """The slowest warm duration."""
        return self.warm[-1] if self.warm else None

    @property
    def cold_max(self) -> Decimal | None:
        """The slowest cold whole invocation."""
        return self.cold[-1] if self.cold else None


@dataclass(frozen=True, slots=True)
class BudgetResult:
    """One budget judged against the run."""

    budget: str
    limit_ms: str
    observed_ms: str | None
    status: str


@dataclass(slots=True)
class _Run:
    """Per-run state: the turns driven and every secret to keep out of evidence."""

    turns: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    secrets: set[str] = field(default_factory=set[str], repr=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--turns", type=int, default=_DEFAULT_TURNS)
    return parser


def _drive_turns(session: boto3.Session, *, environment: str, count: int, run: _Run) -> None:
    """Send `count` persona tool turns through `/api/chat`, one after another."""
    ssm = session.client("ssm")
    origin = require_https(published_parameter(ssm, environment, "publicBaseUrl").rstrip("/"))
    client = app_client(session, environment)
    run.secrets.add(client.secret)
    credential = load_persona(ssm, environment, _PERSONA)
    run.secrets.add(credential.password)
    cognito = session.client("cognito-idp")
    for index in range(count):
        tokens = sign_in(cognito, credential, client)
        run.secrets.update({tokens.access_token, tokens.id_token})
        turn = chat_turn(
            origin,
            tokens.access_token,
            question=TOOL_QUESTION,
            session_id=f"identity-latency-{index}-{uuid.uuid4().hex}",
            redactions=run.secrets,
        )
        run.turns.append({"turn": index + 1, "http_status": turn.status, "done": turn.done})
        print(f"TURN {index + 1} | {turn.describe()} | {'done' if turn.done else 'NOT DONE'}")


def _invocations(
    session: boto3.Session, *, environment: str, budget: Budget, window: LogWindow
) -> FunctionLatency:
    """Read one function's `REPORT` lines in the window, filtered to the counted invocations."""
    name = deployed_function_name(environment, budget.logical_id)
    wanted: set[str] | None = None
    if budget.terminal_outcome is not None:
        wanted = {
            str(record.get("function_request_id"))
            for record in terminal_records(session, function_name=name, window=window)
            if record.get("outcome") == budget.terminal_outcome
        }
    invocations: list[Invocation] = []
    for message in log_messages(
        session, function_name=name, window=window, pattern=_REPORT_PATTERN
    ):
        parsed = parse_report(message)
        if parsed is None:
            continue
        request_id, invocation = parsed
        if wanted is not None and request_id not in wanted:
            continue
        invocations.append(invocation)
    return FunctionLatency(label=budget.label, invocations=tuple(invocations))


def parse_report(message: str) -> tuple[str, Invocation] | None:
    """Read one `REPORT` line's request id and durations; `None` when it is not one."""
    request = _REPORT_ID.search(message)
    duration = _DURATION.search(message)
    if request is None or duration is None:
        return None
    init = _INIT_DURATION.search(message)
    return request.group(1), Invocation(
        Decimal(duration.group(1)), Decimal(init.group(1)) if init else None
    )


class UnsettledLogsError(RuntimeError):
    """The logs never showed one route exchange per completed turn before the deadline.

    Judging a partial window would pass budgets on the invocations that happened to arrive.
    """

    def __init__(self, counts: Mapping[str, int], turns: int) -> None:
        super().__init__(
            f"logs did not settle within {_LOG_TIMEOUT_SECONDS}s: invocations seen "
            f"{dict(counts)} for {turns} completed turn(s)"
        )


def _settled(
    session: boto3.Session, *, environment: str, window: LogWindow, turns: int
) -> dict[str, FunctionLatency]:
    """Poll until every counted function's invocations are visible and stop changing.

    Stable means two consecutive reads agree and the route exchange shows at least one exchange
    per completed turn, which is the one count the run fixes in advance. Raises
    `UnsettledLogsError` when the deadline passes with fewer route exchanges than turns.
    """
    deadline = time.monotonic() + _LOG_TIMEOUT_SECONDS
    previous: tuple[int, ...] | None = None
    while True:
        latencies = {
            budget.label: _invocations(
                session, environment=environment, budget=budget, window=window
            )
            for budget in BUDGETS
        }
        counts = tuple(len(item.invocations) for item in latencies.values())
        enough = counts[0] >= turns
        if enough and counts == previous:
            return latencies
        if time.monotonic() > deadline:
            if enough:
                return latencies
            raise UnsettledLogsError(
                {label: len(item.invocations) for label, item in latencies.items()}, turns
            )
        previous = counts
        time.sleep(_LOG_POLL_SECONDS)


def _judge(budget: Budget, latency: FunctionLatency, turns_done: int) -> list[BudgetResult]:
    """Judge one function's warm and cold budgets.

    A missing warm sample after a completed turn is a FAIL, not a NO-SAMPLE: every completed
    turn invokes each function, so a run that shows no warm invocation cannot show the warm
    budget held. A missing cold sample stays NO-SAMPLE, since every invocation may have been
    warm.
    """
    results: list[BudgetResult] = []
    for kind, limit, observed, required in (
        ("warm p50", budget.warm_p50_ms, latency.warm_p50, turns_done > 0),
        ("cold max", budget.cold_max_ms, latency.cold_max, False),
    ):
        if observed is None:
            status = "FAIL" if required else "NO-SAMPLE"
        else:
            status = "PASS" if observed <= limit else "FAIL"
        results.append(
            BudgetResult(
                budget=f"{budget.label} {kind}",
                limit_ms=str(limit),
                observed_ms=None if observed is None else str(observed),
                status=status,
            )
        )
    return results


def _text(value: Decimal | None) -> str:
    return "-" if value is None else str(value)


def _report(
    latencies: Mapping[str, FunctionLatency], results: Sequence[BudgetResult], turns_done: int
) -> None:
    print("FUNCTION | invocations | cold | warm p50 ms | warm max ms | cold max ms | per turn")
    for latency in latencies.values():
        per_turn = f"{len(latency.invocations)}/{turns_done}"
        print(
            f"{latency.label} | {len(latency.invocations)} | {len(latency.cold)} | "
            f"{_text(latency.warm_p50)} | {_text(latency.warm_max)} | "
            f"{_text(latency.cold_max)} | {per_turn}"
        )
    for result in results:
        observed = result.observed_ms if result.observed_ms is not None else "no sample"
        print(
            f"BUDGET {result.budget} <= {result.limit_ms} ms | observed {observed} | "
            f"{result.status}"
        )


def _write_evidence(
    environment: str,
    run: _Run,
    *,
    latencies: Mapping[str, FunctionLatency],
    results: Sequence[BudgetResult],
) -> Path:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _EVIDENCE_DIR / f"{stamp}-latency.json"
    document = {
        "probe": "identity-latency",
        "decision": "ADR-077",
        "environment": environment,
        "recorded_at": stamp,
        "turns": run.turns,
        "functions": {
            label: {
                "invocations": [
                    {
                        "duration_ms": str(item.duration_ms),
                        "init_duration_ms": None if item.init_ms is None else str(item.init_ms),
                    }
                    for item in latency.invocations
                ],
                "warm_p50_ms": _text(latency.warm_p50),
                "warm_max_ms": _text(latency.warm_max),
                "cold_max_ms": _text(latency.cold_max),
            }
            for label, latency in latencies.items()
        },
        "budgets": [asdict(result) for result in results],
    }
    text = json.dumps(document, indent=2, sort_keys=True)
    for secret in run.secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    path.write_text(text + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """Drive the turns, read the logs, and exit 0 only if no budget failed."""
    args = _parser().parse_args(argv)
    if (
        args.environment != _REQUIRED_ENVIRONMENT
        or args.region != _REQUIRED_REGION
        or not _ACCOUNT_ID.fullmatch(args.expected_account)
    ):
        print("the latency probe accepts only ENV=dev, REGION=us-east-1, a 12-digit account")
        return 2
    if not 1 <= args.turns <= _MAX_TURNS:
        print(f"--turns must be between 1 and {_MAX_TURNS}")
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity().get("Account") != args.expected_account:
        print("explicit profile did not resolve to the expected account")
        return 2
    run = _Run()
    started = now_ms()
    _drive_turns(session, environment=args.environment, count=args.turns, run=run)
    window = LogWindow.around(started, now_ms())
    turns_done = sum(bool(turn["done"]) for turn in run.turns)
    try:
        latencies = _settled(session, environment=args.environment, window=window, turns=turns_done)
    except UnsettledLogsError as error:
        print(f"FAILED RUN | {error}")
        return 1
    results = [
        result
        for budget in BUDGETS
        for result in _judge(budget, latencies[budget.label], turns_done)
    ]
    _report(latencies, results, turns_done)
    path = _write_evidence(args.environment, run, latencies=latencies, results=results)
    failed = [result.budget for result in results if result.status == "FAIL"]
    print(
        f"{turns_done}/{len(run.turns)} turns done; {len(failed)} budget(s) failed; "
        f"evidence {path.relative_to(_ROOT)}"
    )
    return 0 if not failed and turns_done == len(run.turns) else 1


if __name__ == "__main__":
    sys.exit(main())
