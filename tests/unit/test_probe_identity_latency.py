# SPDX-License-Identifier: MIT-0
"""Unit contract for the ADR-077 P4 identity-chain latency probe's reading and judging."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import boto3
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_identity_latency as probe  # noqa: E402

from adapters.inbound.gateway_interceptor.handler import InterceptionOutcome  # noqa: E402

pytestmark = pytest.mark.unit

_SESSION = cast("boto3.Session", object())
_WINDOW = probe.LogWindow(0, 1)
_ROUTE, _ISSUER, _INTERCEPTOR = probe.BUDGETS


def _report(request_id: str, duration: str, init: str | None = None) -> str:
    init_field = f"\tInit Duration: {init} ms" if init is not None else ""
    return (
        f"REPORT RequestId: {request_id}\tDuration: {duration} ms\tBilled Duration: 999 ms\t"
        f"Memory Size: 512 MB\tMax Memory Used: 90 MB{init_field}\t\n"
    )


def _warm(*durations: str) -> probe.FunctionLatency:
    return probe.FunctionLatency(
        label="route exchange",
        invocations=tuple(probe.Invocation(Decimal(value), None) for value in durations),
    )


class TestWarmMedian:
    @pytest.mark.parametrize(
        ("durations", "expected_ms"),
        [(("40",), "40"), (("30", "10"), "10"), (("30", "10", "20"), "20")],
        ids=["one", "two", "three"],
    )
    def test_the_warm_p50_is_the_nearest_rank_observed_duration_in_milliseconds(
        self, durations: tuple[str, ...], expected_ms: str
    ) -> None:
        assert _warm(*durations).warm_p50 == Decimal(expected_ms)

    def test_a_cold_invocation_never_enters_the_warm_p50(self) -> None:
        latency = probe.FunctionLatency(
            label="route exchange",
            invocations=(
                probe.Invocation(Decimal(10), None),
                probe.Invocation(Decimal(5), Decimal(400)),
            ),
        )
        assert latency.warm == [Decimal(10)]
        assert latency.warm_p50 == Decimal(10)
        assert latency.cold == [Decimal(405)]

    def test_no_warm_invocation_has_no_warm_p50(self) -> None:
        assert _warm().warm_p50 is None


class TestReportLine:
    def test_an_init_duration_makes_the_invocation_cold_and_counts_toward_its_total(
        self,
    ) -> None:
        parsed = probe.parse_report(_report("req-1", "12.5", init="300.25"))
        assert parsed is not None
        request_id, invocation = parsed
        assert request_id == "req-1"
        assert invocation.cold
        assert invocation.duration_ms == Decimal("12.5")
        assert invocation.total_ms == Decimal("312.75")

    def test_a_report_without_init_duration_is_warm_and_reads_duration_not_billed(self) -> None:
        parsed = probe.parse_report(_report("req-2", "7.01"))
        assert parsed is not None
        _, invocation = parsed
        assert not invocation.cold
        assert invocation.duration_ms == Decimal("7.01")
        assert invocation.total_ms == Decimal("7.01")

    def test_a_line_that_is_not_a_report_is_skipped(self) -> None:
        assert probe.parse_report("START RequestId: req-3 Version: $LATEST") is None


def _statuses(latency: probe.FunctionLatency, turns_done: int) -> dict[str, str]:
    return {
        result.budget: result.status
        for result in probe._judge(_ROUTE, latency, turns_done)  # pyright: ignore[reportPrivateUsage]
    }


class TestJudge:
    def test_a_warm_p50_at_the_limit_passes_and_above_it_fails(self) -> None:
        limit = str(_ROUTE.warm_p50_ms)
        above = str(_ROUTE.warm_p50_ms + Decimal("0.01"))
        assert _statuses(_warm(limit), 1)["route exchange warm p50"] == "PASS"
        assert _statuses(_warm(above), 1)["route exchange warm p50"] == "FAIL"

    def test_a_cold_max_at_the_limit_passes_and_above_it_fails(self) -> None:
        def cold(total: Decimal) -> probe.FunctionLatency:
            return probe.FunctionLatency(
                label="route exchange",
                invocations=(probe.Invocation(Decimal(0), total),),
            )

        assert _statuses(cold(_ROUTE.cold_max_ms), 1)["route exchange cold max"] == "PASS"
        above = _ROUTE.cold_max_ms + Decimal("0.01")
        assert _statuses(cold(above), 1)["route exchange cold max"] == "FAIL"

    def test_a_warm_budget_without_a_sample_fails_once_a_turn_completed(self) -> None:
        results = probe._judge(_ROUTE, _warm(), 1)  # pyright: ignore[reportPrivateUsage]
        warm = next(result for result in results if result.budget.endswith("warm p50"))
        assert warm.status == "FAIL"
        assert warm.observed_ms is None

    def test_a_warm_budget_without_a_sample_is_no_sample_when_no_turn_completed(self) -> None:
        assert _statuses(_warm(), 0)["route exchange warm p50"] == "NO-SAMPLE"

    def test_a_cold_budget_without_a_sample_is_no_sample_even_after_completed_turns(
        self,
    ) -> None:
        assert _statuses(_warm("10"), 3)["route exchange cold max"] == "NO-SAMPLE"


class TestInterceptorBudget:
    def test_only_forwarded_tools_calls_count_toward_the_interceptor_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [
            {"function_request_id": "forwarded-call", "outcome": "forwarded"},
            {"function_request_id": "tools-list", "outcome": "passed_through"},
            {"function_request_id": "no-bearer", "outcome": "unauthorized"},
        ]
        messages = [
            _report("forwarded-call", "4"),
            _report("tools-list", "900"),
            _report("no-bearer", "800"),
        ]

        def terminal_records(*_: object, **__: object) -> list[dict[str, str]]:
            return records

        def log_messages(*_: object, **__: object) -> list[str]:
            return messages

        monkeypatch.setattr(probe, "terminal_records", terminal_records)
        monkeypatch.setattr(probe, "log_messages", log_messages)

        latency = probe._invocations(  # pyright: ignore[reportPrivateUsage]
            _SESSION, environment="dev", budget=_INTERCEPTOR, window=_WINDOW
        )

        assert _INTERCEPTOR.terminal_outcome == InterceptionOutcome.FORWARDED.value
        assert [item.duration_ms for item in latency.invocations] == [Decimal(4)]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _scripted(monkeypatch: pytest.MonkeyPatch, route_counts: list[int]) -> _Clock:
    polls = iter(route_counts)
    current = {"route": 0}

    def invocations(
        _session: boto3.Session, *, environment: str, budget: probe.Budget, window: object
    ) -> probe.FunctionLatency:
        del environment, window
        if budget is _ROUTE:
            current["route"] = next(polls, current["route"])
            count = current["route"]
        else:
            count = 1
        return _warm(*(["10"] * count))

    clock = _Clock()
    monkeypatch.setattr(probe, "_invocations", invocations)
    monkeypatch.setattr(
        probe, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    return clock


class TestSettled:
    def test_the_run_fails_when_the_deadline_passes_short_of_one_exchange_per_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _scripted(monkeypatch, [1, 2, 2])
        with pytest.raises(probe.UnsettledLogsError) as raised:
            probe._settled(  # pyright: ignore[reportPrivateUsage]
                _SESSION, environment="dev", window=_WINDOW, turns=3
            )
        assert "for 3 completed turn(s)" in str(raised.value)
        assert "'route exchange': 2" in str(raised.value)
        assert clock.now > probe._LOG_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]

    def test_stable_counts_with_one_exchange_per_turn_return_before_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _scripted(monkeypatch, [1, 3, 3])
        latencies = probe._settled(  # pyright: ignore[reportPrivateUsage]
            _SESSION, environment="dev", window=_WINDOW, turns=3
        )
        assert len(latencies["route exchange"].invocations) == 3
        assert clock.now < probe._LOG_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]
