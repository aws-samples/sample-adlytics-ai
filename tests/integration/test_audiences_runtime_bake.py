# SPDX-License-Identifier: MIT-0
"""The D2 traced source render: the full Retention and Audiences rosters against the real lake.

This is a non-authoritative diagnostic, not the D2 runtime acceptance. It renders in this process
with the deployed Baker's own configuration, so every Athena and DynamoDB read stays in the dev
account, and records one phase trace per render:

- Retention: the full governed roster for the latest vintage.
- Audiences month A: the full governed one-market profile and campaign roster, as a body.
- Audiences month B, the newest month: forced exactly one row over the body bound by the fixture,
  rendered as the typed `audiences_body_over_bound` body.

It writes nothing: no pointer, body or custody object, because a direct publish would bypass
GRAPH finalization and serve a grant-less pointer. The pointer store it hands the sources only
reads, and refuses any other call. The GRAPH-path runtime acceptance (a deployed 20-row root,
its finalized grant and the served generation) is `test_d2_graph_root_acceptance.py`, which runs
only in D2's live-acceptance step.

Every bound is the generated S0 row certificate's, checked against the committed
`d2-certificate.json`: Audiences' five phases (list; roster rounds P + C, C and C; measurement) at
the shown C, its Q10 certification seconds and its certified count, and Retention's certified
count of six.

It is opt-in and fails closed on its target. It runs only when `ADL_RUNTIME_BAKE_FUNCTION` names
the deployed Baker. Once opted in, it refuses to run unless `ADL_TEST_ENV` is set explicitly to
`dev`, `ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT` is a 12-digit account that STS confirms, and the
function is exactly `adlytics-dev-PresetArtifactBaker` in that account. All of these checks run
before any read. `ADL_RUNTIME_BAKE_EVIDENCE_DIR` names the ignored durable directory the trace JSON
is written to, whether the render passes or fails.

This file is authored against the feature branch's source factories, which build their own
`AthenaQueryAdapter`. The D2 merge fix-up (S1) changes them to take an injected `queries`. When
that lands, `_recording_adapters` must pass the recorder in instead of patching the adapter
class. The Athena census test forces that edit.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from time import monotonic
from typing import Any, cast

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from runtime_bake_target import (
    BAKER_FUNCTION,
    REFUSE_CALLER,
    TARGET_ENV,
    identity_refusal,
    target_refusal,
)

from adapters.config import PresetArtifactBakeSettings, preset_artifact_bake_settings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    AUDIENCES_RENDER_FORMAT,
    LATEST_MONTH_VARIANT,
    PROFILE_CONTRACTS,
    AudiencesArtifactRenderer,
    AudiencesCompiler,
    AudiencesRenderInput,
    AudiencesRenderResult,
    RetainedAudiencesPresetArtifactSource,
    _body_rows,  # pyright: ignore[reportPrivateUsage]
    retained_audiences_preset_artifact_source,
)
from adapters.jobs.preset_artifact_publisher.bakeability import (
    AUDIENCES_NON_QUERY_MARGIN_MS,
    AUDIENCES_RENDER_TAIL_MS,
    BAKER_TIMEOUT_SECONDS,
    CERTIFIED_QUERY_CAP,
    D2RowFigure,
    audiences_phases,
    audiences_row_budget,
    certify_d2_roster,
    phased_task_seconds,
)
from adapters.jobs.preset_artifact_publisher.retention_source import (
    LATEST_VINTAGE_VARIANT,
    retained_retention_preset_artifact_source,
    retention_bake_body_bytes,
)
from adapters.outbound.athena import query as athena_query
from adapters.outbound.bake_sources import BakeClock, BakeClockMode, BakeSourceReads
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.presentation.audiences import (
    AUDIENCES_ARTIFACT_TYPE,
    AUDIENCES_BODY_OVER_BOUND,
    AUDIENCES_UNAVAILABLE_ARTIFACT_TYPE,
    audiences_artifact_payload,
)
from domain.presentation.preset_artifacts import (
    MAX_PRESET_ARTIFACT_BYTES,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain
from ports.outbound.query import PeriodSeriesRequest, QueryRequest, QueryResult

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-070")]

_TARGET_ENV = TARGET_ENV
_BAKER_FUNCTION = BAKER_FUNCTION
_BURST_WINDOW_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _Query:
    """One Athena call: which bake and phase issued it, and when it started and ended."""

    bake: str
    phase: str
    method: str
    started: float
    ended: float


@dataclass
class _Trace:
    """Every Athena call and phase interval, by bake. Shared across the source's worker threads."""

    bake: str = "unbound"
    phase: str = "unphased"
    in_flight: int = 0
    peak: dict[str, int] = field(default_factory=dict[str, int])
    queries: list[_Query] = field(default_factory=list[_Query])
    intervals: dict[tuple[str, str], list[tuple[float, float]]] = field(
        default_factory=dict[tuple[str, str], list[tuple[float, float]]]
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self, bake: str, phase: str = "unphased") -> None:
        with self.lock:
            self.bake, self.phase = bake, phase

    def enter(self, phase: str) -> tuple[str, str]:
        with self.lock:
            self.phase = phase
            return self.bake, phase

    def interval(self, key: tuple[str, str], started: float, ended: float) -> None:
        with self.lock:
            self.intervals.setdefault(key, []).append((started, ended))

    def call[T](self, method: str, call: Callable[[], T]) -> T:
        with self.lock:
            bake, phase = self.bake, self.phase
            self.in_flight += 1
            self.peak[bake] = max(self.peak.get(bake, 0), self.in_flight)
        started = monotonic()
        try:
            return call()
        finally:
            ended = monotonic()
            with self.lock:
                self.in_flight -= 1
                self.queries.append(_Query(bake, phase, method, started, ended))

    def of(self, bake: str, phase: str | None = None) -> list[_Query]:
        return [
            item
            for item in self.queries
            if item.bake == bake and (phase is None or item.phase == phase)
        ]

    def span_ms(self, bake: str, phase: str) -> int:
        spans = self.intervals.get((bake, phase), [])
        if not spans:
            return 0
        return int((max(end for _, end in spans) - min(start for start, _ in spans)) * 1_000)

    def burst(self, bake: str) -> int:
        """Count the most calls one bake started inside any one-second window."""
        starts = sorted(item.started for item in self.of(bake))
        most, low = 0, 0
        for high, started in enumerate(starts):
            while started - starts[low] > _BURST_WINDOW_SECONDS:
                low += 1
            most = max(most, high - low + 1)
        return most


class _RecordingQueries:
    """Forward every read to the real adapter and record it in the trace."""

    def __init__(self, inner: Any, trace: _Trace) -> None:
        self._inner = inner
        self._trace = trace

    def run(self, request: QueryRequest) -> QueryResult:
        return self._trace.call("run", lambda: cast("QueryResult", self._inner.run(request)))

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        return self._trace.call(
            "run_series", lambda: cast("QueryResult", self._inner.run_series(request))
        )


@dataclass
class _CachingCompiler:
    """Render each month once, so the forced bound reuses the traced render, never a second one."""

    inner: AudiencesCompiler
    rendered: dict[AudiencesRenderInput, AudiencesRenderResult] = field(
        default_factory=dict[AudiencesRenderInput, AudiencesRenderResult]
    )

    def render_audiences(self, request: AudiencesRenderInput) -> AudiencesRenderResult:
        if request not in self.rendered:
            self.rendered[request] = self.inner.render_audiences(request)
        return self.rendered[request]


class _ReadOnlyPointers:
    """The pointer store the sources see: strong reads pass through, every other call refuses."""

    _READS = frozenset({"current", "roster"})

    def __init__(self, inner: DynamoPresetArtifactPointerStore) -> None:
        self._inner = inner

    def __getattr__(self, attribute: str) -> Any:
        if attribute not in self._READS:
            raise AssertionError(f"the diagnostic render called pointers.{attribute}")
        return getattr(self._inner, attribute)


@pytest.fixture(scope="module")
def baker_environment() -> dict[str, str]:
    """Read the dev Baker's own environment after the target guard, or skip until opted in."""
    if not os.environ.get("ADL_RUNTIME_BAKE_FUNCTION", "").strip():
        pytest.skip("set ADL_RUNTIME_BAKE_FUNCTION to the deployed dev Baker to opt in")
    refusal = target_refusal(os.environ)
    if refusal is not None:
        pytest.fail(f"refused before any AWS call: {refusal}")
    expected = os.environ["ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT"]
    try:
        caller = boto3.client("sts").get_caller_identity()["Account"]
        if caller != expected:
            pytest.fail(f"refused before any read: {REFUSE_CALLER}")
        configuration = boto3.client("lambda").get_function_configuration(
            FunctionName=_BAKER_FUNCTION
        )
    except (BotoCoreError, ClientError) as exc:  # opted in: an unreadable target refuses
        pytest.fail(f"the dev target could not be verified: {type(exc).__name__}")
    refusal = identity_refusal(expected, caller, configuration.get("FunctionArn", ""))
    if refusal is not None:
        pytest.fail(f"refused before any read: {refusal}")
    variables = configuration.get("Environment", {}).get("Variables", {})
    if not variables:
        pytest.fail(f"the deployed Baker {_BAKER_FUNCTION} carries no environment")
    return dict(variables)


@pytest.fixture(scope="module")
def evidence_dir() -> Path:
    """Name the ignored durable directory the trace is written to."""
    raw = os.environ.get("ADL_RUNTIME_BAKE_EVIDENCE_DIR", "").strip()
    if not raw:
        pytest.skip("set ADL_RUNTIME_BAKE_EVIDENCE_DIR to record the runtime bake trace")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def bake_settings(
    baker_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> PresetArtifactBakeSettings:
    """Resolve the Baker's settings exactly as the deployed function does at initialization."""
    for name, value in baker_environment.items():
        monkeypatch.setenv(name, value)
    return preset_artifact_bake_settings()


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Trace]:
    """Record every Athena call the two sources make and mark Audiences' three barriers."""
    recorded = _Trace()
    _recording_adapters(monkeypatch, recorded)
    for name, phase in (
        ("_campaigns", "list"),
        ("_roster", "roster"),
        ("_measured", "measurement"),
    ):
        monkeypatch.setattr(
            AudiencesArtifactRenderer,
            name,
            _phase(getattr(AudiencesArtifactRenderer, name), phase, recorded),
        )
    yield recorded


def _recording_adapters(monkeypatch: pytest.MonkeyPatch, recorded: _Trace) -> None:
    real = athena_query.AthenaQueryAdapter

    def adapter(*args: Any, **kwargs: Any) -> _RecordingQueries:
        return _RecordingQueries(real(*args, **kwargs), recorded)

    # Both sources take the injected port (A5.4); `_composition` builds it from this class.
    monkeypatch.setattr(athena_query, "AthenaQueryAdapter", adapter)


def _composition(settings: PresetArtifactBakeSettings) -> tuple[Any, BakeSourceReads]:
    """Build the closed factory inputs: the (recording) adapter and one CURRENT reader.

    The reader's clock is opened on the wall clock, as the legacy render opens it: this test
    renders outside a V3 run, so no run-pinned instant exists.
    """
    queries = athena_query.AthenaQueryAdapter(
        athena_query.AthenaQueryTarget(
            workgroup=settings.athena_workgroup,
            database=settings.semantic_v2_current_database,
            reuse_minutes=settings.athena_result_reuse_max_age_minutes,
            # This harness renders sources directly, outside the page execution that sets a
            # bake statement deadline, so its statements take the interactive wait.
            wait=athena_query.INTERACTIVE_WAIT,
            results=athena_query.ResultTransport.PAGED,
        )
    )
    current = DynamoCurrentPublications(settings.semantic_publications_table)
    root = contracts_root()
    selector = PublicationSelector(
        current, load_source_packs(root), load_commerce_identity_bridge(root)
    )
    return queries, BakeSourceReads(current, selector, BakeClock(BakeClockMode.LEGACY_WALL))


def _phase(method: Callable[..., Any], phase: str, recorded: _Trace) -> Callable[..., Any]:
    def wrapped(self: object, *args: Any, **kwargs: Any) -> Any:
        key = recorded.enter(phase)
        started = monotonic()
        try:
            return method(self, *args, **kwargs)
        finally:
            recorded.interval(key, started, monotonic())

    return wrapped


def _ms(started: float, ended: float) -> int:
    return int((ended - started) * 1_000)


def _months(source: RetainedAudiencesPresetArtifactSource) -> tuple[CalendarPeriod, ...]:
    return tuple(
        sorted(
            {
                period
                for contract_id, period in source.served()
                if contract_id in PROFILE_CONTRACTS.values() and period.grain is TimeGrain.MONTH
            }
        )
    )


def _served_through(
    source: RetainedAudiencesPresetArtifactSource, month: CalendarPeriod
) -> Callable[[], frozenset[tuple[str, CalendarPeriod]]]:
    """Hide every month after `month`, so the source resolves `month` as its newest."""
    held = frozenset(item for item in source.served() if item[1].end <= month.end)
    return lambda: held


def _payload(request: PresetArtifactPublicationRequest) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(request.body.payload_json))


AUDIENCES_ROW = "audiences#last_complete_month"
RETENTION_ROW = "retention#latest_with_current_publication"
_CERTIFICATE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "bake-certification" / "d2-certificate.json"
)


@cache
def _figure(key: str) -> D2RowFigure:
    """Return the generated S0 row certificate, checked against the committed row."""
    (figure,) = (item for item in certify_d2_roster(contracts_root()) if item.key == key)
    rows = json.loads(_CERTIFICATE.read_text())["rows"]
    (committed,) = (row for row in rows if row["key"] == key)
    assert (committed["certified_count"], tuple(committed["phases"])) == (
        figure.certificate.count,
        figure.certificate.phases,
    ), f"{key}: the committed certificate is not this tree's counter"
    return figure


def _audiences_certified(campaigns: int) -> tuple[int, int, int, int, int]:
    """Return the five-phase S0 ceilings at `campaigns` shown, bound to the Audiences row.

    The row fixes M and the campaign bound C; a render that shows more than C campaigns, or a
    certificate whose own ceilings are not `audiences_phases(M, C)`, fails here.
    """
    certificate = _figure(AUDIENCES_ROW).certificate
    fixture = dict(certificate.fixture)
    markets, bound = int(fixture["marketplaces"]), int(fixture["campaign_bound"])
    assert certificate.phases == audiences_phases(markets, bound)
    assert campaigns <= bound, f"the render showed {campaigns} campaigns over the bound {bound}"
    return audiences_phases(markets, campaigns)


def _audiences_phases(recorded: _Trace, bake: str, campaigns: int) -> dict[str, object]:
    """One Audiences bake's barriers against the certified five-phase ceilings.

    The `roster` barrier wraps every scope's roster read, so it holds all three roster rounds
    (P + C, then C and C, a campaign scope's older-history series around withheld data).
    """
    listed, first, second, third, measured = _audiences_certified(campaigns)
    planned = {"list": listed, "roster": first + second + third, "measurement": measured}
    phases = {
        phase: {
            "duration_ms": recorded.span_ms(bake, phase),
            "queries": len(recorded.of(bake, phase)),
            "planned_upper_bound": planned[phase],
        }
        for phase in ("list", "roster", "measurement")
    }
    for phase, row in phases.items():
        assert row["queries"] <= row["planned_upper_bound"], (
            f"{bake} {phase} ran more queries than its certified plan count: {row}"
        )
    unphased = recorded.of(bake, "unphased")
    assert unphased == [], f"{bake} ran Athena outside its three barriers: {unphased}"
    queries = len(recorded.of(bake))
    certified = _figure(AUDIENCES_ROW).certificate.count
    assert queries <= certified, f"{bake} ran {queries} queries over the certified {certified}"
    return {
        "phases": phases,
        "makespan_ms": sum(row["duration_ms"] for row in phases.values()),
        "campaigns_shown": campaigns,
        "queries": queries,
        "certified_plan_count": certified,
    }


def _concurrency(recorded: _Trace, bake: str, bound: int) -> dict[str, int]:
    peak = recorded.peak.get(bake, 0)
    assert peak <= bound, f"{bake} ran {peak} queries at once; the configured bound is {bound}"
    return {"configured": bound, "peak_in_flight": peak, "burst_per_second": recorded.burst(bake)}


def _terms(total_ms: int, makespan_ms: int, terms: dict[str, int]) -> dict[str, object]:
    """Each non-query term beside its bakeability allowance; the total bound absorbs any excess."""
    allowances = {
        "render_finalization_ms": AUDIENCES_RENDER_TAIL_MS,
        "non_query_ms": AUDIENCES_NON_QUERY_MARGIN_MS,
    }
    for name, value in terms.items():
        assert value >= 0, f"{name} is negative: {terms}"
    return {
        "total_ms": total_ms,
        "makespan_ms": makespan_ms,
        **{
            name: {
                "measured": value,
                "allowance": allowances[name],
                "over_allowance": value > allowances[name],
            }
            for name, value in terms.items()
        },
    }


def _audiences_render(  # noqa: PLR0913, PLR0917 -- the trace and one render's inputs
    trace: _Trace,
    bake: str,
    started: float,
    sourced: float,
    shown: int,
    bound: int,
) -> dict[str, object]:
    """One Audiences render's phases, concurrency and timing; no write term, nothing is written."""
    phases = _audiences_phases(trace, bake, shown)
    measured_end = max(span[1] for span in trace.intervals.get((bake, "measurement"), []))
    total = _ms(started, sourced)
    makespan = cast("int", phases["makespan_ms"])
    render = _ms(measured_end, sourced)
    # The Q10 certification formula over the five phases at the shown C, which includes the
    # row's adopted write seconds; the row's own budget caps it.
    figure = _figure(AUDIENCES_ROW)
    budget = int(
        phased_task_seconds(_audiences_certified(shown), CERTIFIED_QUERY_CAP, figure.write.seconds)
        * 1_000
    )
    assert total <= budget <= figure.budget_seconds * 1_000 <= BAKER_TIMEOUT_SECONDS * 1_000
    return {
        **phases,
        "concurrency": _concurrency(trace, bake, bound),
        "timing": _terms(
            total,
            makespan,
            {"render_finalization_ms": render, "non_query_ms": total - makespan - render},
        ),
        "budget_ms": budget,
    }


def test_full_roster_phase_trace_and_over_bound_render(  # noqa: PLR0915 -- one ordered trace
    bake_settings: PresetArtifactBakeSettings,
    evidence_dir: Path,
    trace: _Trace,
) -> None:
    """Render both full rosters and the over-bound month B; publish and write nothing."""
    bound = bake_settings.athena_query_batch_max_concurrency
    pointers = cast(
        "Any", _ReadOnlyPointers(DynamoPresetArtifactPointerStore(bake_settings.pointer_table))
    )
    evidence: dict[str, object] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": _TARGET_ENV,
        "baker_function": _BAKER_FUNCTION,
        "authority": "non-authoritative diagnostic; no pointer, body or custody write",
        "athena_workgroup": bake_settings.athena_workgroup,
        "result_reuse_minutes": bake_settings.athena_result_reuse_max_age_minutes,
        "outcome": "incomplete",
    }
    try:
        composed = retained_audiences_preset_artifact_source(
            bake_settings, pointers, *_composition(bake_settings)
        )
        if composed is None:
            pytest.fail(
                "the Audiences source stayed closed in dev (its closure condition is logged)"
            )
        compiler = _CachingCompiler(composed.compiler)
        audiences = replace(composed, compiler=compiler)
        variant = PresetArtifactVariant(LATEST_MONTH_VARIANT)
        registered = audiences_row_budget(1)
        assert audiences.row_budget == registered, "the source must carry the registered row budget"
        months = _months(audiences)
        if len(months) < 2:
            pytest.fail(f"the trace needs two published profile months; dev has {len(months)}")
        month_a, month_b = months[-2], months[-1]
        evidence["month_a"] = [month_a.start.isoformat(), month_a.end.isoformat()]
        evidence["month_b"] = [month_b.start.isoformat(), month_b.end.isoformat()]

        # Month A: the full governed roster, rendered as a body under the registered bound.
        trace.begin("audiences_a")
        started = monotonic()
        request_a = replace(audiences, served=_served_through(audiences, month_a)).publication_for(
            variant
        )
        sourced = monotonic()
        assert isinstance(request_a, PresetArtifactPublicationRequest), (
            "month A must produce a body: the dev pointer already holds it"
        )
        rendered_a = compiler.rendered[AudiencesRenderInput(month_a)]
        assert rendered_a.artifact is not None
        payload_a = _payload(request_a)
        assert payload_a["type"] == AUDIENCES_ARTIFACT_TYPE
        assert request_a.pointer_advance.pointer.resolved_window == month_a.window
        rows_a, bytes_a = _body_rows(payload_a), len(request_a.body.payload_json.encode())
        assert rows_a <= registered and bytes_a <= MAX_PRESET_ARTIFACT_BYTES
        shown_a = rendered_a.artifact.markets[0].campaigns.shown
        evidence["audiences_a"] = {
            **_audiences_render(trace, "audiences_a", started, sourced, shown_a, bound),
            "rows": {"encoded": rows_a, "registered_bound": registered},
            "bytes": {"encoded": bytes_a, "artifact_cap": MAX_PRESET_ARTIFACT_BYTES},
            "revision": request_a.body.revision.value,
        }

        # Month B: rendered once and traced, then forced exactly one row over the body bound.
        trace.begin("audiences_b")
        started = monotonic()
        rendered_b = compiler.render_audiences(AudiencesRenderInput(month_b))
        assert rendered_b.artifact is not None, "month B must render whole before it is forced"
        rows_b = _body_rows(audiences_artifact_payload(rendered_b.artifact))
        forced = rows_b - 1
        request_b = replace(audiences, row_budget=forced).publication_for(variant)
        sourced = monotonic()
        assert isinstance(request_b, PresetArtifactPublicationRequest)
        payload_b = _payload(request_b)
        assert (payload_b["type"], payload_b["reason_code"]) == (
            AUDIENCES_UNAVAILABLE_ARTIFACT_TYPE,
            AUDIENCES_BODY_OVER_BOUND,
        )
        window = month_b.window
        identity = (
            f"{AUDIENCES_BODY_OVER_BOUND}:{window.start.isoformat()}:{window.end.isoformat()}"
        )
        assert request_b.body.revision == PresetArtifactRevision.from_publication_identities(
            (*rendered_b.publication_identities, identity), render_format=AUDIENCES_RENDER_FORMAT
        )
        assert request_b.pointer_advance.pointer.resolved_window == window
        bytes_b = len(
            canonical_preset_artifact_json(audiences_artifact_payload(rendered_b.artifact)).encode()
        )
        shown_b = rendered_b.artifact.markets[0].campaigns.shown
        evidence["audiences_b"] = {
            **_audiences_render(trace, "audiences_b", started, sourced, shown_b, bound),
            "rows": {"encoded": rows_b, "forced_bound": forced, "registered_bound": registered},
            "bytes": {"encoded": bytes_b, "artifact_cap": MAX_PRESET_ARTIFACT_BYTES},
            "reason": payload_b["reason_code"],
            "revision": request_b.body.revision.value,
        }

        # Retention: the full governed roster for the latest vintage.
        evidence["retention"] = _retention_render(bake_settings, pointers, trace)
        evidence["outcome"] = "passed"
    except BaseException as error:
        evidence["outcome"] = f"failed: {type(error).__name__}"
        raise
    finally:
        # Nothing was written, so there is no disposition to run; the trace is always kept.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (evidence_dir / f"audiences-runtime-bake-trace-{stamp}.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        )


def _retention_render(
    settings: PresetArtifactBakeSettings, pointers: Any, recorded: _Trace
) -> dict[str, object]:
    composed = retained_retention_preset_artifact_source(
        settings, pointers, *_composition(settings)
    )
    if composed is None:
        pytest.fail("the Retention source stayed closed in dev (its closure condition is logged)")
    variant = PresetArtifactVariant(LATEST_VINTAGE_VARIANT)
    recorded.begin("retention", "render")
    started = monotonic()
    request = composed.publication_for(variant)
    sourced = monotonic()
    body_bytes: int | None = None
    if isinstance(request, PresetArtifactPublicationRequest):
        body_bytes = len(request.body.payload_json.encode())
        assert body_bytes <= min(
            retention_bake_body_bytes(contracts_root()), MAX_PRESET_ARTIFACT_BYTES
        )
    total = _ms(started, sourced)
    figure = _figure(RETENTION_ROW)
    certified = figure.certificate.count
    # S0 certifies M x (4 standard + 2 ARP) at M = 1: the generated row's count is six.
    assert certified == 6, f"the generated Retention row certifies {certified}, not 6"
    queries = len(recorded.of("retention"))
    assert queries <= certified, f"retention ran {queries} queries over the certified {certified}"
    assert total <= figure.seconds * 1_000 <= BAKER_TIMEOUT_SECONDS * 1_000
    return {
        "outcome": "unchanged" if body_bytes is None else "body_rendered",
        "total_ms": total,
        "queries": queries,
        "certified_plan_count": certified,
        "certification_seconds": figure.seconds,
        "concurrency": _concurrency(
            recorded, "retention", settings.athena_query_batch_max_concurrency
        ),
        "body_bytes": body_bytes,
        "body_cap": retention_bake_body_bytes(contracts_root()),
    }
