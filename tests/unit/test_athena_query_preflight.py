# SPDX-License-Identifier: MIT-0
"""Athena query preflight semantics before any remote execution begins."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from adapters.outbound.athena import query as athena_query
from adapters.outbound.athena.deadline import (
    BAKE_DEADLINE_UNSET,
    reset_bake_statement_deadline,
    set_bake_statement_deadline,
)
from adapters.outbound.athena.execution import (
    MAX_WAIT_SECONDS,
    QUERY_TIMED_OUT,
    StartedStatement,
    StatementOutcome,
)
from adapters.outbound.athena.query import (
    ALL_ROWS_WITHHELD,
    BAKE_INVOCATION_WAIT,
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
)
from domain.shared_kernel.errors import DependencyError, InvariantError, ValidationError
from ports.outbound.query import QueryRequest

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Evidence:
    withheld_only: bool


@dataclass(frozen=True, slots=True)
class _Publication:
    evidence: _Evidence


@dataclass(frozen=True, slots=True)
class _BoundPlan:
    all_publications: tuple[_Publication, ...]


@dataclass(frozen=True, slots=True)
class _Request:
    plan: _BoundPlan


class _NoAthenaCalls:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"withheld-only preflight must not call Athena: {name}")


def test_withheld_only_publication_refuses_before_athena() -> None:
    request = cast(
        "QueryRequest",
        _Request(
            _BoundPlan(
                (
                    _Publication(_Evidence(False)),
                    _Publication(_Evidence(True)),
                )
            )
        ),
    )
    adapter = AthenaQueryAdapter(
        AthenaQueryTarget("unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT),
        cast("AthenaClient", _NoAthenaCalls()),
    )

    with pytest.raises(ValidationError) as raised:
        adapter.run(request)

    assert raised.value.code == ALL_ROWS_WITHHELD
    assert raised.value.context == {"withheld_periods": 1}
    assert "no figure can be reported" in raised.value.user_visible_description


def test_ordinary_selected_publications_pass_withheld_only_preflight() -> None:
    request = cast(
        "QueryRequest",
        _Request(_BoundPlan((_Publication(_Evidence(False)),))),
    )

    athena_query._refuse_withheld_only(request)  # pyright: ignore[reportPrivateUsage]


# --- ADR-144: each target's wait policy -------------------------------------------------------


@dataclass
class _Recorded:
    limits: list[float]
    policies: list[str | None]
    windows: list[float | None]


def _composed_adapter(
    monkeypatch: pytest.MonkeyPatch, wait: athena_query.StatementWait, client: object = None
) -> tuple[AthenaQueryAdapter, _Recorded]:
    """Build an adapter whose composition, start and fetch are stubbed; the wait is real."""
    recorded = _Recorded([], [], [])

    def nothing(*_arguments: object) -> None:
        return None

    def composed(*_arguments: object) -> object:
        return SimpleNamespace(sql="SELECT 1", parameters=())

    def started(_client: object, _request: object, **arguments: Any) -> StartedStatement:
        recorded.windows.append(arguments["retry_throttled_until"])
        return StartedStatement("execution-1", 0)

    def completion(
        _client: object, _execution: str, *, limit: float, wait_policy: str | None
    ) -> StatementOutcome:
        recorded.limits.append(limit)
        recorded.policies.append(wait_policy)
        return StatementOutcome()

    monkeypatch.setattr(athena_query, "_refuse_withheld_only", nothing)
    monkeypatch.setattr(athena_query, "_refuse_unreadable_suppression", nothing)
    monkeypatch.setattr(athena_query, "compose", composed)
    monkeypatch.setattr(athena_query, "start_counted", started)

    def fetched(*_arguments: object) -> list[list[str | None]]:
        return []

    monkeypatch.setattr(athena_query, "fetch_rows", fetched)
    monkeypatch.setattr(athena_query, "_suppression_from", nothing)
    if client is None:
        monkeypatch.setattr(athena_query, "await_completion", completion)
    target = AthenaQueryTarget("unit-workgroup", "unit-current", 0, wait)
    return AthenaQueryAdapter(target, cast("AthenaClient", client or object())), recorded


def _plan_request() -> Any:
    logical = SimpleNamespace(
        metric=SimpleNamespace(name="spend"),
        facts_read=("ads",),
        selected_columns=(),
        result_aliases=(),
    )
    return SimpleNamespace(plan=SimpleNamespace(logical=logical), max_rows=10)


def test_the_interactive_wait_limits_thirty_seconds_and_the_bake_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, recorded = _composed_adapter(monkeypatch, INTERACTIVE_WAIT)
    adapter.run(_plan_request())
    assert 29.9 < recorded.limits[0] <= MAX_WAIT_SECONDS
    assert recorded.policies == ["interactive"]
    bake, baked = _composed_adapter(monkeypatch, BAKE_INVOCATION_WAIT)
    token = set_bake_statement_deadline(monotonic() + 600.0)
    try:
        bake.run(_plan_request())
    finally:
        reset_bake_statement_deadline(token)
    # Paired: the bake waits its deadline - now (600 s here), never the interactive 30 s.
    assert 599.0 < baked.limits[0] <= 600.0 and baked.policies == ["bake_invocation"]
    window = baked.windows[0]
    assert window is not None and 59.0 < window - monotonic() <= 60.0


def test_a_past_bake_deadline_waits_zero_and_times_out_with_its_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Running:
        def get_query_execution(self, **_arguments: str) -> dict[str, object]:
            return {"QueryExecution": {"Status": {"State": "RUNNING"}}}

        def stop_query_execution(self, **_arguments: str) -> dict[str, object]:
            return {}

    adapter, _ = _composed_adapter(monkeypatch, BAKE_INVOCATION_WAIT, _Running())
    token = set_bake_statement_deadline(monotonic() - 5.0)
    try:
        with pytest.raises(DependencyError) as raised:
            adapter.run(_plan_request())
    finally:
        reset_bake_statement_deadline(token)
    context = raised.value.context
    assert raised.value.code == QUERY_TIMED_OUT
    assert context["wait_policy"] == "bake_invocation" and context["wait_limit_seconds"] == 0.0
    assert isinstance(context["waited_seconds"], float)


def test_a_bake_statement_without_a_deadline_is_an_invariant_before_any_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, recorded = _composed_adapter(monkeypatch, BAKE_INVOCATION_WAIT)
    with pytest.raises(InvariantError) as raised:
        adapter.run(_plan_request())
    assert raised.value.code == BAKE_DEADLINE_UNSET
    assert str(raised.value.code) == "ADLY-ATHENA-0005"
    assert recorded.windows == [] and recorded.limits == []


def test_a_target_without_a_wait_does_not_construct() -> None:
    with pytest.raises(TypeError):
        AthenaQueryTarget("unit-workgroup", "unit-current", 0)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
