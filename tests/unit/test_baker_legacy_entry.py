# SPDX-License-Identifier: MIT-0
"""The legacy bake entry, every deployment outside dev (ADR-142, ADR-144).

Roster pages run through the one page execution; a declared page with no bake source runs
through the registry's fallback, with no window and no statement.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from bake_application_support import (
    COMPOSED_SETTINGS,
    apply_with_deadline,
    bake_parts,
    compose_baker,
)
from refusal_telemetry_support import Telemetry, capture

from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakePublication,
    BakeResult,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    BakeRegistryMismatchError,
    PresetArtifactBakeApplication,
    RegisteredPresetArtifactBaker,
    UnavailablePresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.isolated_refusals import note_isolated_refusal
from adapters.jobs.preset_artifact_publisher.page_bake import (
    PageWindow,
    ProcessBakeLedger,
    render_page,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    DeclaredBakeIntents,
    load_declared_bake_intents,
)
from adapters.outbound.bake_sources import BakeQueryCapture
from adapters.outbound.telemetry.reporting import OperationLog
from domain.operations.bake_refusals import (
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.operations.execution_graph_release import (
    BakeRoster,
    DurableBakeEffectInvalidError,
)
from domain.presentation.preset_artifacts import (
    ArtifactBodyWriteResult,
    PresetArtifactBody,
    PresetArtifactCustodyManifest,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPublicationRequest,
    PresetArtifactPublicationResult,
    PresetArtifactRevision,
    PresetArtifactSiblingBody,
    PresetArtifactSiblingCustody,
    PresetArtifactSiblingIdentity,
    PresetArtifactVariant,
    canonical_preset_artifact_json,
)
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

pytestmark = pytest.mark.unit

ROSTER = BakeRoster.from_contract()
WORKSPACE = BakeIntent("workspace", "7d")
SCORECARD = BakeIntent("scorecard", "7d")


@dataclass
class _PageBaker:
    """One roster page's baker: its designed absence, recording every intent."""

    received: list[BakeIntent] = field(default_factory=list[BakeIntent])

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.received.append(intent)
        return BakeUnavailable(intent, "scorecard_materialization_unavailable")


def _registry(page: _PageBaker) -> RegisteredPresetArtifactBaker:
    return RegisteredPresetArtifactBaker(
        bakers=dict.fromkeys(ROSTER.pages, page),
        fallback=UnavailablePresetArtifactBaker(),
        roster_pages=ROSTER.pages,
    )


def _application(page: _PageBaker, ledger: ProcessBakeLedger | None = None) -> Any:
    return PresetArtifactBakeApplication(
        declared=DeclaredBakeIntents((WORKSPACE, SCORECARD)),
        parts=bake_parts(_registry(page), process=ledger),
        roster=ROSTER,
        persistence=object(),  # pyright: ignore[reportArgumentType]
    )


def test_a_declared_page_without_a_roster_row_takes_the_fallback_with_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[OperationLog] = []

    def record(event: OperationLog, _logger: object) -> None:
        lines.append(event)

    monkeypatch.setattr(baker_module, "report_operation", record)
    windows: list[tuple[BakeIntent, PageWindow]] = []

    def recording(parts: Any, intent: BakeIntent, window: PageWindow, **keywords: Any) -> Any:
        windows.append((intent, window))
        return render_page(parts, intent, window, **keywords)

    monkeypatch.setattr(baker_module, "render_page", recording)
    page = _PageBaker()
    failures = apply_with_deadline(
        {
            "Records": [
                {"messageId": "workspace", "body": WORKSPACE.serialize()},
                {"messageId": "scorecard", "body": SCORECARD.serialize()},
            ]
        },
        _application(page),
    )
    assert failures == ()
    # The fallback ran no window and reached no page baker; the roster intent built its own.
    assert [(intent, window.row.key) for intent, window in windows] == [(SCORECARD, "scorecard#7d")]
    assert page.received == [SCORECARD]
    terminal = [line for line in lines if line.phase == "terminal" and "page" in line.details]
    assert [(line.details["page"], line.outcome) for line in terminal] == [
        ("workspace", "unavailable"),
        ("scorecard", "unavailable"),
    ]
    assert "throttled_starts" not in terminal[0].details  # no render, no observation
    assert terminal[1].details["throttled_starts"] == 0
    # Paired: the branch, not a relaxed `row`, keeps the non-roster intent acknowledged.
    with pytest.raises(DurableBakeEffectInvalidError):
        ROSTER.row("workspace#7d")


def test_two_roster_records_share_the_process_ledger_and_a_fallback_does_not_advance_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries: list[int] = []

    def recording(parts: Any, intent: BakeIntent, window: PageWindow, **keywords: Any) -> Any:
        page = render_page(parts, intent, window, **keywords)
        entries.append(page.entry.process_bake_ordinal)
        return page

    monkeypatch.setattr(baker_module, "render_page", recording)
    ledger = ProcessBakeLedger(peak=lambda: 100.0)
    application = _application(_PageBaker(), ledger)
    for body in (SCORECARD, WORKSPACE, SCORECARD):
        apply_with_deadline(
            {"Records": [{"messageId": "m", "body": body.serialize()}]}, application
        )
    assert entries == [1, 2]


def _guard_failures(declared: DeclaredBakeIntents, roster: BakeRoster) -> list[str]:
    """Each declared intent must be a roster row, or a page outside the roster (the fallback)."""
    return [
        f"{intent.page}#{intent.variant}"
        for intent in declared.intents
        if not roster.contains(f"{intent.page}#{intent.variant}") and intent.page in roster.pages
    ]


def test_every_declared_intent_is_a_roster_row_or_a_page_outside_the_roster() -> None:
    declared = load_declared_bake_intents(contracts_root())
    assert _guard_failures(declared, ROSTER) == []
    # Paired control: a roster page declared with a variant that has no row would reach a page
    # baker with no window, so the guard names it.
    unrostered = DeclaredBakeIntents((*declared.intents, BakeIntent("scorecard", "90d")))
    assert _guard_failures(unrostered, ROSTER) == ["scorecard#90d"]


@pytest.mark.parametrize("graph_table", ["", "fictional-graph"], ids=["legacy", "graph"])
def test_an_unconfigured_composition_still_has_a_query_capture_on_both_returns(
    monkeypatch: pytest.MonkeyPatch, graph_table: str
) -> None:
    unconfigured = replace(COMPOSED_SETTINGS, athena_workgroup="")
    application, targets = compose_baker(monkeypatch, unconfigured, graph_table=graph_table)
    assert targets == []  # no admission, so no adapter
    queries = application.parts.queries
    assert isinstance(queries, BakeQueryCapture)
    assert type(queries.inner).__name__ == "_NoQueries"
    if graph_table:
        assert application.graph is not None


def test_a_registry_naming_a_page_outside_the_roster_does_not_construct() -> None:
    """The fallback branch can't hide a real source: a non-roster baker refuses composition."""
    with pytest.raises(BakeRegistryMismatchError):
        RegisteredPresetArtifactBaker(
            bakers={"workspace": _PageBaker()},
            fallback=UnavailablePresetArtifactBaker(),
            roster_pages=(),
        )


# --- isolation on the legacy entry (ADR-142, design 02 §3.6, §9) ---------------------------

AUDIENCES = BakeIntent("audiences", "last_complete_month")
CAMPAIGNS = BakeIntent("campaigns", "7d")


def _request_for(intent: BakeIntent, siblings: int = 0) -> PresetArtifactPublicationRequest:
    """Return a fictional publication for a roster intent, with `siblings` pairs."""
    page, variant = PresetArtifactPage(intent.page), PresetArtifactVariant(intent.variant)
    revision = PresetArtifactRevision.from_publication_identities(("publication-1",))
    body = PresetArtifactBody(page, variant, revision, canonical_preset_artifact_json({"k": 1}))
    names = tuple(f"order{index:02d}" for index in range(siblings))
    publics = tuple(
        PresetArtifactSiblingBody(
            page,
            variant,
            revision,
            PresetArtifactSiblingIdentity(name, 0),
            canonical_preset_artifact_json({"sibling": name}),
        )
        for name in names
    )
    privates = tuple(
        PresetArtifactSiblingCustody(
            page,
            variant,
            revision,
            PresetArtifactSiblingIdentity(name, 0),
            canonical_preset_artifact_json({"custody": name}),
        )
        for name in names
    )
    custody = PresetArtifactCustodyManifest(
        page, variant, revision, canonical_preset_artifact_json({"parent": "c"})
    )
    pointer = PresetArtifactPointer(page, variant, revision, body.digest, 1, None, custody.digest)
    return PresetArtifactPublicationRequest(
        body, PresetArtifactPointerAdvanceRequest(pointer), custody, publics, privates
    )


@dataclass
class _Source:
    """One roster page's source: a result or an error, after noting item refusals."""

    result: BakeResult | None = None
    error: Exception | None = None
    notes: tuple[IsolatedRefusal, ...] = ()

    def bake(self, intent: BakeIntent) -> BakeResult:
        for note in self.notes:
            note_isolated_refusal(
                note.reason, note.scope, note.count, section=cast("str", note.section)
            )
        if self.error is not None:
            raise self.error
        return self.result or BakePublication(_request_for(intent))


@dataclass
class _Writes:
    """The legacy persistence: records each publication; the pointer is its last event."""

    events: list[str] = field(default_factory=list[str])

    def publish(
        self,
        request: PresetArtifactPublicationRequest,
        *,
        remaining_ms: Callable[[], int],
        tail_seconds: float,
    ) -> PresetArtifactPublicationResult:
        del remaining_ms, tail_seconds
        body = request.body
        self.events.extend(("body", "pointer"))
        return PresetArtifactPublicationResult(
            ArtifactBodyWriteResult(
                body.page, body.variant, body.revision, body.key, body.digest, None
            ),
            PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.ADVANCED),
        )


def _isolating(  # noqa: PLR0913 -- the recorders, the source, its store and its parts
    telemetry: Telemetry,
    source: object,
    persistence: object,
    *,
    intent: BakeIntent = CAMPAIGNS,
    ledger: ProcessBakeLedger | None = None,
    queries: BakeQueryCapture | None = None,
) -> PresetArtifactBakeApplication:
    return PresetArtifactBakeApplication(
        declared=DeclaredBakeIntents((intent,)),
        parts=bake_parts(source, process=ledger, queries=queries),
        roster=ROSTER,
        persistence=cast("Any", persistence),
        refusal_metric=telemetry.metric,
        over_budget_metric=telemetry.budget,
    )


def _event(intent: BakeIntent, receive_count: int = 1) -> dict[str, object]:
    return {
        "Records": [
            {
                "messageId": f"m{receive_count}",
                "body": intent.serialize(),
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
            }
        ]
    }


def test_a_page_refusal_is_acknowledged_writes_nothing_and_emits_once_with_entry_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    writes = _Writes()
    refused = _Source(error=ValidationError(ErrorCode("PLAN", 8), "Campaign names can't be read"))
    assert apply_with_deadline(_event(CAMPAIGNS), _isolating(telemetry, refused, writes)) == ()
    assert writes.events == []
    assert telemetry.triples() == [("page", "source_refused", 1)]
    assert telemetry.datapoints == [("data", 1)]
    (line,) = telemetry.refusal_lines()
    assert dict(line.details) == {
        "entry": "legacy",
        "execution_id": "fictional-execution",
        "page": "campaigns",
        "variant": "7d",
        "scope": "page",
        "refusal_class": "data",
        "count": 1,
        "replayed": False,
        "error_code": "ADLY-PLAN-0008",
    }
    # Paired: a publication writes and emits nothing.
    published = _Writes()
    assert apply_with_deadline(_event(CAMPAIGNS), _isolating(telemetry, _Source(), published)) == ()
    assert published.events == ["body", "pointer"]
    assert len(telemetry.spans) == len(telemetry.datapoints) == 1


def test_item_refusals_publish_the_body_and_emit_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    notes = tuple(
        IsolatedRefusal(
            IsolatedRefusalReason.HISTORY_GAP, RefusalScope.ITEM, section="campaign-audiences"
        )
        for _ in range(3)
    )
    writes = _Writes()
    application = _isolating(telemetry, _Source(notes=notes), writes, intent=AUDIENCES)
    assert apply_with_deadline(_event(AUDIENCES), application) == ()
    assert writes.events == ["body", "pointer"]
    assert telemetry.triples() == [("item", "history_gap", 3)]
    assert telemetry.datapoints == [("data", 3)]


def test_the_stage_write_deadline_moves_no_legacy_pointer_and_emits_the_page_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_preset_artifacts import (  # noqa: PLC0415 -- the sibling-store fixtures
        _persistence,  # pyright: ignore[reportPrivateUsage]
        _SiblingStores,  # pyright: ignore[reportPrivateUsage]
    )

    telemetry = capture(monkeypatch)
    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)
    source = _Source(result=BakePublication(_request_for(CAMPAIGNS, siblings=9)))
    application = _isolating(telemetry, source, persistence)
    # 29.999 s left: below the 30 s tail, so the stage submits nothing.
    assert apply_with_deadline(_event(CAMPAIGNS), application, lambda: 29_999) == ()
    assert stores.events == []  # no pair, no parent, no pointer
    assert telemetry.triples() == [("page", "bodies_over_invocation", 1)]
    (line,) = telemetry.refusal_lines()
    assert (line.details["written"], line.details["bodies"]) == (0, 9)
    # Paired: with time, the same render publishes and the pointer moves last.
    stores = _SiblingStores(hold_seconds=0)
    persistence, _ = _persistence(stores)
    assert apply_with_deadline(_event(CAMPAIGNS), _isolating(telemetry, source, persistence)) == ()
    assert stores.events[-1] == "pointer"


def test_a_dependency_error_retries_on_receive_1_and_is_unavailable_on_receive_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    down = _Source(error=DependencyError(ErrorCode("FICTIONAL", 41), "athena down"))
    writes = _Writes()
    application = _isolating(telemetry, down, writes)
    assert apply_with_deadline(_event(CAMPAIGNS, 1), application) == ("m1",)
    assert telemetry.spans == [] and telemetry.datapoints == []  # a raising receive emits nothing
    assert apply_with_deadline(_event(CAMPAIGNS, 3), application) == ()
    assert telemetry.triples() == [("page", "dependency_unavailable", 1)]
    assert telemetry.datapoints == [("dependency", 1)] and writes.events == []


@pytest.mark.usefixtures("fake_request_keys")
def test_over_the_certified_count_is_a_batch_item_failure_and_at_the_bound_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = capture(monkeypatch)
    row = ROSTER.row("campaigns#7d")
    over = BakeQueryCapture(_CountingInner())
    application = _isolating(
        telemetry, _Querying(over, row.certified_plan_count + 1), _Writes(), queries=over
    )
    assert apply_with_deadline(_event(CAMPAIGNS), application) == ("m1",)
    assert telemetry.spans == []  # a defect is no isolated refusal
    at = BakeQueryCapture(_CountingInner())
    application = _isolating(
        telemetry, _Querying(at, row.certified_plan_count), _Writes(), queries=at
    )
    assert apply_with_deadline(_event(CAMPAIGNS), application) == ()


@dataclass
class _CountingInner:
    def run(self, request: Any) -> Any:
        del request
        return "rows"

    def run_series(self, request: Any) -> Any:
        del request
        return "rows"


@dataclass
class _Querying:
    queries: BakeQueryCapture
    count: int

    def bake(self, intent: BakeIntent) -> BakeResult:
        for index in range(self.count):
            with suppress(InvariantError):
                self.queries.run(cast("Any", f"request-{index}"))
        return BakePublication(_request_for(intent))


@pytest.fixture
def fake_request_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key fake string requests directly (the real digest needs a bound request)."""
    import adapters.outbound.bake_sources as module  # noqa: PLC0415

    monkeypatch.setattr(module, "request_key", lambda request: f"query:{request}")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


def _terminal_lines(monkeypatch: pytest.MonkeyPatch) -> list[OperationLog]:
    lines: list[OperationLog] = []

    def record(event: OperationLog, _logger: object) -> None:
        if event.phase == "terminal" and "page" in event.details:
            lines.append(event)

    monkeypatch.setattr(baker_module, "report_operation", record)
    return lines


def _scripted(*values: float) -> Callable[[], float]:
    reads = iter(values)
    return lambda: next(reads)


def test_terminal_lines_carry_the_process_fields_and_a_fallback_leaves_the_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture(monkeypatch)
    lines = _terminal_lines(monkeypatch)
    ledger = ProcessBakeLedger(peak=_scripted(100.0, 120.0, 140.0, 160.0))
    application = PresetArtifactBakeApplication(
        declared=DeclaredBakeIntents((CAMPAIGNS, WORKSPACE)),
        parts=bake_parts(
            RegisteredPresetArtifactBaker(
                bakers=dict.fromkeys(ROSTER.pages, _Source()),
                fallback=UnavailablePresetArtifactBaker(),
                roster_pages=ROSTER.pages,
            ),
            process=ledger,
        ),
        roster=ROSTER,
        persistence=cast("Any", _Writes()),
        refusal_metric=lambda _class, _count: None,
        over_budget_metric=lambda: None,
    )
    for intent in (CAMPAIGNS, WORKSPACE, CAMPAIGNS):
        apply_with_deadline(_event(intent), application)
    first, fallback, second = lines
    assert "process_id" not in fallback.details  # a non-roster intent has no observation
    assert first.details["process_id"] == second.details["process_id"]
    fields = ("process_bake_ordinal", "rss_before_mb", "process_baseline_mb", "memory_peak_mb")
    assert tuple(first.details[name] for name in fields) == (1, 100.0, 100.0, 120.0)
    assert tuple(second.details[name] for name in fields) == (2, 140.0, 100.0, 160.0)
    assert all(
        float(cast("float", line.details["rss_before_mb"]))
        <= float(cast("float", line.details["memory_peak_mb"]))
        for line in (first, second)
    )


def test_the_baseline_is_the_first_entry_even_when_that_render_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture(monkeypatch)
    lines = _terminal_lines(monkeypatch)
    ledger = ProcessBakeLedger(peak=_scripted(100.0, 140.0, 180.0))
    source = _Source(error=DependencyError(ErrorCode("FICTIONAL", 42), "down"))
    application = _isolating(Telemetry(), source, _Writes(), ledger=ledger)
    assert apply_with_deadline(_event(CAMPAIGNS, 1), application) == ("m1",)  # a `retry` line
    source.error = None
    assert apply_with_deadline(_event(CAMPAIGNS, 1), application) == ()
    (line,) = lines
    assert (line.details["process_bake_ordinal"], line.details["process_baseline_mb"]) == (2, 100.0)
    assert line.details["rss_before_mb"] == 140.0
