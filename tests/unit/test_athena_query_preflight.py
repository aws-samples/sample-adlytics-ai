# SPDX-License-Identifier: MIT-0
"""Athena query preflight semantics before any remote execution begins."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from test_athena_execution import (
    _CSV,  # pyright: ignore[reportPrivateUsage]
    _LOCATION,  # pyright: ignore[reportPrivateUsage]
    _PAGED,  # pyright: ignore[reportPrivateUsage]
    _paged_athena,  # pyright: ignore[reportPrivateUsage]
    _s3_with,  # pyright: ignore[reportPrivateUsage]
)

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
    ResultTransport,
)
from domain.shared_kernel.errors import DependencyError, InvariantError, ValidationError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.query import QueryRequest, RowCeiling


class _NoWithholding:
    """Stands in for the adapter's withholding accumulator over a stub plan."""

    def __init__(self, _plan: object) -> None:
        return None

    def add(self, _row: object) -> None:
        return None

    def result(self) -> Suppression:
        return Suppression(rows_suppressed=False)


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
        AthenaQueryTarget(
            "unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, ResultTransport.PAGED
        ),
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

    def fetched(*_arguments: object) -> Iterator[list[str | None]]:
        return iter([])

    monkeypatch.setattr(athena_query, "iter_pages", fetched)
    monkeypatch.setattr(athena_query, "_SuppressionAccumulator", _NoWithholding)
    if client is None:
        monkeypatch.setattr(athena_query, "await_completion", completion)
    target = AthenaQueryTarget("unit-workgroup", "unit-current", 0, wait, ResultTransport.PAGED)
    return AthenaQueryAdapter(target, cast("AthenaClient", client or object())), recorded


def _plan_request() -> Any:
    logical = SimpleNamespace(
        metric=SimpleNamespace(name="spend"),
        facts_read=("ads",),
        selected_columns=(),
        result_aliases=(),
    )
    return SimpleNamespace(
        plan=SimpleNamespace(logical=logical), max_rows=10, ranked=None, ceiling=RowCeiling.BAKE
    )


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


# --- ADR-143: a fold streams the rows `run` holds, on both transports -------------------------

_STREAMED_ROWS: tuple[dict[str, object], ...] = (
    {"marketplace_key": "amazon_us", "note": None, "value_0": Decimal("1.50")},
    {"marketplace_key": "amazon_us", "note": "", "value_0": Decimal(2)},
    {
        "marketplace_key": "amazon_us",
        "note": 'line one\nline two, with "quotes"',
        "value_0": Decimal(3),
    },
    {"marketplace_key": "amazon_us", "note": "café", "value_0": None},
)
"""`_CSV` (or `_PAGED`) as rows: the selected columns as strings, the aggregate a `Decimal`."""


class _Collected:
    """A sink that keeps what it is handed, so a fold's rows compare to a held read's."""

    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row: object) -> None:
        self.rows.append(row)


def _streaming_adapter(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> AthenaQueryAdapter:
    """Build an adapter with a stubbed composition, start and completion and a real reader."""
    _stub_statement(monkeypatch)
    target = AthenaQueryTarget("unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, transport)
    if transport is ResultTransport.OBJECT:
        s3, _ = _s3_with(_CSV)
        return AthenaQueryAdapter(target, cast("AthenaClient", object()), s3=s3)
    return AthenaQueryAdapter(target, _paged_athena(_PAGED))


def _stub_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the preflight, composition, start and completion; leave the reader real."""

    def nothing(*_arguments: object) -> None:
        return None

    def composed(*_arguments: object) -> object:
        return SimpleNamespace(sql="SELECT 1", parameters=())

    def started(*_arguments: object, **_keywords: object) -> StartedStatement:
        return StartedStatement("execution-1", 0)

    def completed(*_arguments: object, **_keywords: object) -> StatementOutcome:
        return StatementOutcome(output_location=_LOCATION)

    monkeypatch.setattr(athena_query, "_refuse_withheld_only", nothing)
    monkeypatch.setattr(athena_query, "_refuse_unreadable_suppression", nothing)
    monkeypatch.setattr(athena_query, "compose", composed)
    monkeypatch.setattr(athena_query, "start_counted", started)
    monkeypatch.setattr(athena_query, "await_completion", completed)
    monkeypatch.setattr(athena_query, "_SuppressionAccumulator", _NoWithholding)


def _streamed_request(max_rows: int, ceiling: RowCeiling = RowCeiling.BAKE_STREAMED) -> Any:
    logical = SimpleNamespace(
        metric=SimpleNamespace(name="spend"),
        facts_read=("ads",),
        selected_columns=("marketplace_key", "note"),
        result_aliases=("value_0",),
    )
    return SimpleNamespace(
        plan=SimpleNamespace(logical=logical), max_rows=max_rows, ranked=None, ceiling=ceiling
    )


@pytest.mark.parametrize("transport", [ResultTransport.PAGED, ResultTransport.OBJECT])
def test_a_fold_hands_its_sink_the_rows_run_holds_on_either_transport(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> None:
    """Paired: `run` over the same result holds exactly the rows the fold's sink was handed."""
    sink = _Collected()
    read = _streaming_adapter(monkeypatch, transport).fold(_streamed_request(10), sink)
    assert tuple(sink.rows) == _STREAMED_ROWS
    assert (read.rows_read, read.truncated) == (4, False)

    held = _streaming_adapter(monkeypatch, transport).run(_streamed_request(10))
    assert held.rows == _STREAMED_ROWS and held.truncated is False


@pytest.mark.parametrize("transport", [ResultTransport.PAGED, ResultTransport.OBJECT])
def test_a_fold_past_its_rows_counts_the_extra_row_and_hands_on_none_of_it(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> None:
    """At `max_rows` 3 of 4 rows the sink gets 3, the read counts 4 and is truncated."""
    sink = _Collected()
    read = _streaming_adapter(monkeypatch, transport).fold(_streamed_request(3), sink)
    assert tuple(sink.rows) == _STREAMED_ROWS[:3]
    assert (read.rows_read, read.truncated) == (4, True)


@pytest.mark.parametrize("transport", [ResultTransport.PAGED, ResultTransport.OBJECT])
def test_a_grouped_fold_reads_every_row_and_is_never_held(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> None:
    """Paired: at `max_rows` 3 a grouped fold still hands on all 4 rows untruncated; `run` refuses.

    `BAKE_GROUPED`'s `max_rows` is what the sink keeps, so its statement has no `LIMIT`.
    """
    limits: list[object] = []
    adapter = _streaming_adapter(monkeypatch, transport)

    def composed(_plan: object, max_rows: object, _ranked: object = None) -> object:
        limits.append(max_rows)
        return SimpleNamespace(sql="SELECT 1", parameters=())

    monkeypatch.setattr(athena_query, "compose", composed)
    sink = _Collected()
    read = adapter.fold(_streamed_request(3, RowCeiling.BAKE_GROUPED), sink)
    assert tuple(sink.rows) == _STREAMED_ROWS
    assert (read.rows_read, read.truncated, limits) == (4, False, [None])
    with pytest.raises(ValueError, match="folded, never held"):
        adapter.run(_streamed_request(3, RowCeiling.BAKE_GROUPED))


def test_an_object_target_makes_its_s3_client_once_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never on a worker thread: a fold run on one makes no client.

    Paired control: a paged target makes no S3 client at all.
    """
    s3, _ = _s3_with(_CSV)
    made: list[tuple[str, str]] = []

    def client(service: str) -> object:
        made.append((service, threading.current_thread().name))
        return s3

    _stub_statement(monkeypatch)
    monkeypatch.setattr(athena_query, "boto3", SimpleNamespace(client=client))
    target = AthenaQueryTarget(
        "unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, ResultTransport.OBJECT
    )
    adapter = AthenaQueryAdapter(target, cast("AthenaClient", object()))
    assert made == [("s3", threading.current_thread().name)]
    sink = _Collected()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="measurement-query") as pool:
        pool.submit(adapter.fold, _streamed_request(10), sink).result()
    assert tuple(sink.rows) == _STREAMED_ROWS
    assert len(made) == 1

    paged = AthenaQueryTarget(
        "unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, ResultTransport.PAGED
    )
    AthenaQueryAdapter(paged, cast("AthenaClient", object()))
    assert len(made) == 1


# --- ADR-152: a ranked-keys statement's population columns are lifted, never answered --------

_RANKED_COLUMNS = (
    "channel_key",
    "amazon_asin",
    "value_0",
    "population_count",
    "population_suppressed",
    "population_sum_0",
)
_RANKED_CELLS: list[list[str | None]] = [
    ["ch_a", "k1", "5", "2", "0", "9"],
    ["ch_a", "k2", "4", "2", "0", "9"],
    ["ch_b", "k3", "1", "1", "1", "1"],
]


def _ranked_adapter(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport, cells: list[list[str | None]]
) -> AthenaQueryAdapter:
    _stub_statement(monkeypatch)
    target = AthenaQueryTarget("unit-workgroup", "unit-current", 0, INTERACTIVE_WAIT, transport)
    if transport is ResultTransport.OBJECT:
        lines = [",".join(f'"{name}"' for name in _RANKED_COLUMNS)]
        lines.extend(",".join(f'"{value}"' for value in row) for row in cells)
        s3, _ = _s3_with(("\n".join(lines) + "\n").encode())
        return AthenaQueryAdapter(target, cast("AthenaClient", object()), s3=s3)
    return AthenaQueryAdapter(target, _paged_athena(cells))


def _ranked_request() -> Any:
    from domain.measurement.ranking import RankedProjection  # noqa: PLC0415

    logical = SimpleNamespace(
        metric=SimpleNamespace(name="spend"),
        facts_read=("ads",),
        selected_columns=("channel_key", "amazon_asin"),
        result_aliases=("value_0",),
        grain=SimpleNamespace(dimensions=("product",)),
    )
    ranked = SimpleNamespace(
        restriction=SimpleNamespace(
            projection=RankedProjection.RANKED_KEYS,
            population_sums=("spend",),
            rank_metric="spend",
        ),
        partition_columns=("channel_key",),
    )
    return SimpleNamespace(
        plan=SimpleNamespace(logical=logical), max_rows=10, ranked=ranked, ceiling=RowCeiling.BAKE
    )


@pytest.mark.covers("ADR-152")
@pytest.mark.parametrize("transport", [ResultTransport.PAGED, ResultTransport.OBJECT])
def test_a_ranked_keys_read_lifts_its_population_per_partition_on_either_transport(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> None:
    """The rows keep only the plan's columns; one partition's withholding marks the read."""
    from domain.measurement.ranking import RankedPopulation  # noqa: PLC0415

    held = _ranked_adapter(monkeypatch, transport, _RANKED_CELLS).run(_ranked_request())
    assert held.rows == (
        {"channel_key": "ch_a", "amazon_asin": "k1", "value_0": Decimal(5)},
        {"channel_key": "ch_a", "amazon_asin": "k2", "value_0": Decimal(4)},
        {"channel_key": "ch_b", "amazon_asin": "k3", "value_0": Decimal(1)},
    )
    assert held.population == (
        RankedPopulation(("ch_a",), 2, (("spend", Decimal(9)),), suppressed=False),
        RankedPopulation(("ch_b",), 1, (("spend", Decimal(1)),), suppressed=True),
    )
    assert held.suppression.rows_suppressed is True
    assert held.suppression.reason is not None and "population" in held.suppression.reason


@pytest.mark.covers("ADR-152")
@pytest.mark.parametrize("transport", [ResultTransport.PAGED, ResultTransport.OBJECT])
def test_population_figures_that_differ_within_a_partition_are_a_ranking_invariant(
    monkeypatch: pytest.MonkeyPatch, transport: ResultTransport
) -> None:
    """Never a figure picked from one row: the SQL and the declaration disagree."""
    from domain.measurement.ranking import RANKED_INVARIANT  # noqa: PLC0415

    drifted = [list(row) for row in _RANKED_CELLS]
    drifted[1][3] = "3"
    with pytest.raises(InvariantError) as raised:
        _ranked_adapter(monkeypatch, transport, drifted).run(_ranked_request())
    assert raised.value.code == RANKED_INVARIANT
