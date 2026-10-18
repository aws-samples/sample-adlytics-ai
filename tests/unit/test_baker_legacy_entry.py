# SPDX-License-Identifier: MIT-0
"""The legacy bake entry, every deployment outside dev (ADR-142, ADR-144).

Roster pages run through the one page execution; a declared page with no bake source runs
through the registry's fallback, with no window and no statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from bake_application_support import (
    COMPOSED_SETTINGS,
    apply_with_deadline,
    bake_parts,
    compose_baker,
)

from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.preset_artifact_publisher import baker as baker_module
from adapters.jobs.preset_artifact_publisher.bake_results import BakeResult, BakeUnavailable
from adapters.jobs.preset_artifact_publisher.baker import (
    BakeRegistryMismatchError,
    PresetArtifactBakeApplication,
    RegisteredPresetArtifactBaker,
    UnavailablePresetArtifactBaker,
)
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
from domain.operations.execution_graph_release import (
    BakeRoster,
    DurableBakeEffectInvalidError,
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
