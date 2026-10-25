# SPDX-License-Identifier: MIT-0
"""The Baker handler hands the invocation's remaining time to both entries (ADR-144).

One source, the Lambda context's callable, and no fallback: a V3 task and a legacy roster intent
both receive that callable, and an invocation without it refuses every record before any runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from bake_application_support import bake_parts, legacy_application, remaining_ms

from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher import graph_bake
from adapters.jobs.preset_artifact_publisher.bake_results import BakeResult, BakeUnavailable
from adapters.jobs.preset_artifact_publisher.page_bake import PageWindow, render_page
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeDelivery,
    BakeIntent,
    DeclaredBakeIntents,
    bake_task_body,
)
from adapters.outbound.athena import query as athena_query
from adapters.outbound.athena.execution import StartedStatement, StatementOutcome
from adapters.outbound.athena.query import (
    BAKE_INVOCATION_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
    ResultTransport,
)
from adapters.outbound.bake_sources import BakeQueryCapture
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_release import BakeRoster
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import Suppression
from ports.outbound.query import RowCeiling


class _NoWithholding:
    """Stands in for the adapter's withholding accumulator over a stub plan."""

    def __init__(self, _plan: object) -> None:
        return None

    def add(self, _row: object) -> None:
        return None

    def result(self) -> Suppression:
        return Suppression(rows_suppressed=False)


pytestmark = pytest.mark.unit

SCORECARD = BakeIntent("scorecard", "7d")
ROSTER = BakeRoster.from_contract()


@dataclass(frozen=True)
class _Context:
    """The attributes Powertools reads, plus the remaining-time callable as a plain attribute.

    A plain function attribute reads back as the same object each time, so a test can assert
    the dispatch passed exactly the context's callable (a bound method would be a new object).
    """

    get_remaining_time_in_millis: Callable[[], int]
    function_name: str = "adlytics-preset-baker"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:fictional"
    memory_limit_in_mb: int = 1024
    aws_request_id: str = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class _NoRemainingContext:
    """A mis-composed caller's context: everything Powertools reads, but no remaining time."""

    function_name: str = "adlytics-preset-baker"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:fictional"
    memory_limit_in_mb: int = 1024
    aws_request_id: str = "00000000-0000-4000-8000-000000000002"


def _six_hundred_seconds() -> int:
    return 600_000


@dataclass
class _GraphRecorder:
    """A graph bake that records what each dispatch handed it."""

    v3: list[tuple[BakeDelivery, Callable[[], int]]] = field(
        default_factory=list[tuple[BakeDelivery, Callable[[], int]]]
    )
    fan_out: list[tuple[object, Callable[[], int]]] = field(
        default_factory=list[tuple[object, Callable[[], int]]]
    )

    def consume(self, envelope: object, remaining_ms: Callable[[], int]) -> object:
        self.fan_out.append((envelope, remaining_ms))
        return type("Result", (), {"acknowledge": True})()

    def consume_v3(self, delivery: BakeDelivery, remaining_ms: Callable[[], int]) -> object:
        self.v3.append((delivery, remaining_ms))
        return type("Result", (), {"acknowledge": True})()


@dataclass
class _Baker:
    received: list[BakeIntent] = field(default_factory=list[BakeIntent])

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.received.append(intent)
        return BakeUnavailable(intent, "scorecard_materialization_unavailable")


_OWNER = {
    "grant_id": "g",
    "granted_run": "r",
    "conflict_unit": "u",
    "served_revision": "2026-10-07T08:00:00+00:00",
    "root_flow_id": "f",
    "cohort_revision": "c",
}
_FAN_OUT_BODY = (
    '{"conflict_unit":"u","dispatch_id":"d","grant_id":"g","granted_run":"r",'
    '"node_id":"n","post_grant":"bake","root_flow_id":"f","run_id":"r",'
    '"schema_version":"1","served_revision":"2026-10-07T08:00:00+00:00"}'
)


def _v3_task_record() -> dict[str, object]:
    body = json.loads(
        bake_task_body(
            _OWNER,
            page="scorecard",
            variant="7d",
            lane=str(BAKE_TASKS["lane_groups"][0]),
            manifest_digest="sha256:" + "a" * 64,
            plan_count=3,
        )
    )
    body |= {"run_id": "r", "node_id": "bake_task:scorecard#7d", "dispatch_id": "d"}
    return {
        "messageId": "v3-task",
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
        "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": "1790108000000"},
    }


def _record(message_id: str, body: str, receive: str = "1") -> dict[str, object]:
    return {
        "messageId": message_id,
        "body": body,
        "attributes": {"ApproximateReceiveCount": receive, "SentTimestamp": "1790108000000"},
    }


def _handle(  # noqa: PLR0913, PLR0917 -- the patch, the application, the records, the context
    monkeypatch: pytest.MonkeyPatch,
    application: object,
    records: list[dict[str, object]],
    context: object,
) -> dict[str, list[dict[str, str]]]:
    monkeypatch.setattr(baker_module, "application", lambda: application)
    return baker_module.handler({"Records": records}, context)


def test_a_v3_task_receives_the_context_callable_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _GraphRecorder()
    application = legacy_application(
        DeclaredBakeIntents((SCORECARD,)),
        _Baker(),
        object(),
        roster=ROSTER,
        graph=cast("baker_module.GraphBakeConsumer", graph),
        mode=lambda: AuthorityMode.GRAPH,
    )
    context = _Context(_six_hundred_seconds)
    assert _handle(monkeypatch, application, [_v3_task_record()], context) == {
        "batchItemFailures": []
    }
    ((delivery, passed),) = graph.v3
    assert passed is context.get_remaining_time_in_millis
    assert delivery.body.node_id == "bake_task:scorecard#7d"


def test_a_v3_task_window_is_built_from_that_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph entry hands the same callable to the page execution, beside its row."""
    from graph_bake_v3_support import Bake, roster  # noqa: PLC0415

    windows: list[PageWindow] = []

    def recording(parts: Any, intent: BakeIntent, window: PageWindow, **keywords: Any) -> Any:
        windows.append(window)
        return render_page(parts, intent, window, **keywords)

    monkeypatch.setattr(graph_bake, "render_page", recording)
    bake = Bake(roster=roster(1), remaining_ms=_six_hundred_seconds)
    run = bake.granted()
    bake.fan_out(run)
    bake.dispatch(run)
    bake.task(run, bake.roster.keys[0])
    ((window,),) = (windows,)
    assert window.remaining_ms is _six_hundred_seconds
    assert window.row == bake.roster.row(bake.roster.keys[0]) and window.final is False
    assert window.tail_seconds == float(BAKE_TASKS["tail_seconds"])


def _deadline_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AthenaQueryAdapter, list[tuple[str | None, float]]]:
    """Build a real bake-wait adapter whose start and poll are stubbed; record each wait."""
    waits: list[tuple[str | None, float]] = []

    def nothing(*_arguments: object) -> None:
        return None

    def composed(*_arguments: object) -> object:
        return type("Composed", (), {"sql": "SELECT 1", "parameters": ()})()

    def started(*_arguments: object, **_keywords: object) -> StartedStatement:
        return StartedStatement("execution-1", 0)

    def completion(
        _client: object, _execution: str, *, limit: float, wait_policy: str | None
    ) -> StatementOutcome:
        waits.append((wait_policy, limit))
        return StatementOutcome()

    def fetched(*_arguments: object) -> Iterator[list[str | None]]:
        return iter([])

    for name, value in (
        ("_refuse_withheld_only", nothing),
        ("_refuse_unreadable_suppression", nothing),
        ("compose", composed),
        ("start_counted", started),
        ("await_completion", completion),
        ("iter_pages", fetched),
        ("_SuppressionAccumulator", _NoWithholding),
    ):
        monkeypatch.setattr(athena_query, name, value)
    target = AthenaQueryTarget(
        "adlytics-dev-v2-bake", "current", 0, BAKE_INVOCATION_WAIT, ResultTransport.PAGED
    )
    return AthenaQueryAdapter(target, cast("Any", object())), waits


@dataclass
class _QueryingBaker:
    """A legacy page source that runs one statement through the counted port."""

    queries: BakeQueryCapture

    def bake(self, intent: BakeIntent) -> BakeResult:
        logical = type(
            "Logical",
            (),
            {
                "metric": type("Metric", (), {"name": "spend"})(),
                "facts_read": ("ads",),
                "selected_columns": (),
                "result_aliases": (),
            },
        )()
        request = type(
            "Request",
            (),
            {
                "plan": type("Plan", (), {"logical": logical})(),
                "max_rows": 10,
                "ranked": None,
                "ceiling": RowCeiling.BAKE,
            },
        )()
        self.queries.run(cast("Any", request))
        return BakeUnavailable(intent, "scorecard_materialization_unavailable")


def test_a_legacy_roster_intent_waits_within_the_context_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adapters.outbound.bake_sources as sources_module  # noqa: PLC0415

    monkeypatch.setattr(sources_module, "request_key", lambda _request: "query:one")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    adapter, waits = _deadline_adapter(monkeypatch)
    queries = BakeQueryCapture(adapter)
    windows: list[PageWindow] = []

    def recording(parts: Any, intent: BakeIntent, window: PageWindow, **keywords: Any) -> Any:
        windows.append(window)
        return render_page(parts, intent, window, **keywords)

    monkeypatch.setattr(baker_module, "render_page", recording)
    application = baker_module.PresetArtifactBakeApplication(
        declared=DeclaredBakeIntents((SCORECARD,)),
        parts=bake_parts(_QueryingBaker(queries), queries=queries),
        roster=ROSTER,
        persistence=cast("Any", object()),
    )
    context = _Context(_six_hundred_seconds)
    assert _handle(
        monkeypatch, application, [_record("legacy", SCORECARD.serialize())], context
    ) == {"batchItemFailures": []}
    ((window,),) = (windows,)
    assert window.remaining_ms is context.get_remaining_time_in_millis
    assert window.row == ROSTER.row("scorecard#7d")
    ((policy, limit),) = waits
    row = ROSTER.row("scorecard#7d")
    expected = 600 - (float(BAKE_TASKS["tail_seconds"]) + row.write_seconds)
    assert policy == "bake_invocation" and expected - 1.0 < limit <= expected


def test_an_invocation_without_remaining_time_refuses_every_record_and_runs_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[BaseException] = []

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(baker_module, "report_error", record_error)
    graph, baker = _GraphRecorder(), _Baker()
    applied: list[object] = []

    def never(*arguments: object, **_keywords: object) -> None:
        applied.append(arguments)

    monkeypatch.setattr(baker_module, "render_page", never)
    application = legacy_application(
        DeclaredBakeIntents((SCORECARD,)),
        baker,
        object(),
        roster=ROSTER,
        graph=cast("baker_module.GraphBakeConsumer", graph),
        mode=lambda: AuthorityMode.GRAPH,
    )
    records = [
        _record("legacy", SCORECARD.serialize()),
        _v3_task_record(),
        _record("fan-out", _FAN_OUT_BODY),
    ]
    result = _handle(monkeypatch, application, records, _NoRemainingContext())
    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "legacy"},
            {"itemIdentifier": "v3-task"},
            {"itemIdentifier": "fan-out"},
        ]
    }
    (error,) = errors
    assert isinstance(error, InvariantError)
    assert error.context["signal"] == "BAKE_DEADLINE_UNBOUND"
    assert str(error.code) == "ADLY-PRESET_ARTIFACT_BAKE-0005"
    assert (graph.v3, graph.fan_out, baker.received, applied) == ([], [], [], [])
    # Paired: the same three records with the callable all run.
    errors.clear()
    assert _handle(monkeypatch, application, records, _Context(remaining_ms)) == {
        "batchItemFailures": []
    }
    assert (len(graph.v3), len(graph.fan_out), errors) == (1, 1, [])


def test_the_deadline_inputs_are_required_keywords() -> None:
    application = legacy_application(DeclaredBakeIntents((SCORECARD,)), _Baker(), object())
    with pytest.raises(TypeError):
        application.apply(SCORECARD.serialize())  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        application.apply(SCORECARD.serialize(), remaining_ms=remaining_ms)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    runtime = graph_bake.GraphBakeRuntime.__new__(graph_bake.GraphBakeRuntime)
    with pytest.raises(TypeError):
        runtime.consume_v3(cast("Any", object()))  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
