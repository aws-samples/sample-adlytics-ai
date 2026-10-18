# SPDX-License-Identifier: MIT-0
"""The one closed page->baker registry and the pages.yaml<->roster contract (plan F7, A1.2).

`PAGE_BAKERS` is the only registry: composition builds `RegisteredPresetArtifactBaker.bakers`
from it, and construction refuses unless its keys equal the roster's pages. The corrected A1.2
contract holds the declaration to the roster: every roster (page, variant) is a declared intent,
and the declared pages outside the roster are exactly `NOT_GRAPH_BAKED_PAGES`. Every refusal
names its exact reason and the page, beside a passing control. Every identity is fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import pytest

from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.preset_artifact_publisher.bake_results import (
    BakeResult,
    BakeUnavailable,
)
from adapters.jobs.preset_artifact_publisher.baker import (
    NOT_GRAPH_BAKED_PAGES,
    PAGE_BAKERS,
    BakeRegistryMismatchError,
    RegisteredPresetArtifactBaker,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    DeclaredBakeIntents,
    load_declared_bake_intents,
)
from domain.operations.execution_graph_release import BakeRoster, BakeRosterRow

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

ROSTER = BakeRoster.from_contract()


@dataclass
class Recording:
    """A page baker (or the fallback) that records every intent it is asked to bake."""

    name: str
    calls: list[BakeIntent] = field(default_factory=list[BakeIntent])

    def bake(self, intent: BakeIntent) -> BakeResult:
        self.calls.append(intent)
        return BakeUnavailable(intent, f"{self.name}_recorded")


def _registry(
    pages: tuple[str, ...], roster_pages: tuple[str, ...] = ROSTER.pages
) -> tuple[RegisteredPresetArtifactBaker, dict[str, Recording], Recording]:
    bakers = {page: Recording(page) for page in pages}
    fallback = Recording("fallback")
    return (
        RegisteredPresetArtifactBaker(bakers=bakers, fallback=fallback, roster_pages=roster_pages),
        bakers,
        fallback,
    )


def require_roster_declared(
    declared: DeclaredBakeIntents, roster: BakeRoster, not_graph_baked: frozenset[str]
) -> None:
    """A1.2: every roster row is declared; the declared pages outside it are the closed set."""
    for row in roster.rows:
        assert BakeIntent(row.page, row.variant) in declared.intents, f"undeclared row {row.key}"
    declared_pages = {intent.page for intent in declared.intents}
    unbaked = declared_pages - set(roster.pages)
    assert unbaked <= not_graph_baked, (
        f"declared pages without roster rows: {sorted(unbaked - not_graph_baked)}"
    )
    assert not_graph_baked <= unbaked, (
        f"stale NOT_GRAPH_BAKED_PAGES: {sorted(not_graph_baked - unbaked)}"
    )


# --- F7: the registry -----------------------------------------------------------------------


def test_the_registry_keys_equal_the_roster_pages() -> None:
    assert (
        tuple(PAGE_BAKERS)
        == ROSTER.pages
        == (
            "campaigns",
            "retail-health",
            "scorecard",
            "new-customers",
            "reach",
            "retention",
            "audiences",
        )
    )
    baker, _bakers, _fallback = _registry(tuple(PAGE_BAKERS))
    assert baker.registered_pages() == ROSTER.pages


def test_a_roster_page_without_a_baker_is_refused_by_name() -> None:
    with pytest.raises(BakeRegistryMismatchError) as refused:
        _registry(("campaigns",))
    assert (refused.value.reason, refused.value.page) == (
        "roster_page_without_baker",
        "retail-health",
    )
    # Control: the exact roster pages construct.
    _registry(ROSTER.pages)


def test_a_baker_for_a_page_with_no_roster_rows_is_refused_by_name() -> None:
    with pytest.raises(BakeRegistryMismatchError) as refused:
        _registry((*ROSTER.pages, "brand-health"))
    assert (refused.value.reason, refused.value.page) == (
        "baker_page_not_in_roster",
        "brand-health",
    )


def test_every_roster_intent_reaches_its_baker_and_the_fallback_sees_none() -> None:
    baker, bakers, fallback = _registry(ROSTER.pages)

    for row in ROSTER.rows:
        baker.bake(BakeIntent(row.page, row.variant))

    assert fallback.calls == []
    for page, recording in bakers.items():
        assert [intent.variant for intent in recording.calls] == [
            row.variant for row in ROSTER.rows if row.page == page
        ]
    # Control: a declared page outside the roster reaches the fallback, and only it.
    outside = BakeIntent("channel-performance", "7d")
    baker.bake(outside)
    assert fallback.calls == [outside]


def test_the_registered_baker_has_no_fixed_page_fields() -> None:
    names = {item.name for item in fields(RegisteredPresetArtifactBaker)}
    assert names == {"bakers", "fallback", "roster_pages", "admission"}
    assert not names & {"campaign", "retail_health", "scorecard"}


# --- A1.2: the declaration and the roster -----------------------------------------------------


def test_every_roster_row_is_declared_and_the_rest_are_the_not_graph_baked_pages() -> None:
    declared = load_declared_bake_intents(contracts_root())
    require_roster_declared(declared, ROSTER, NOT_GRAPH_BAKED_PAGES)
    assert len(ROSTER.rows) == 20
    closed = {"channel-performance", "workspace", "data-health", "media-efficiency"}
    assert closed == NOT_GRAPH_BAKED_PAGES


def test_control_a_newly_declared_page_fails_by_name() -> None:
    declared = load_declared_bake_intents(contracts_root())
    extra = DeclaredBakeIntents((*declared.intents, BakeIntent("new-page", "7d")))
    with pytest.raises(AssertionError, match=r"declared pages without roster rows: \['new-page'\]"):
        require_roster_declared(extra, ROSTER, NOT_GRAPH_BAKED_PAGES)


def test_control_a_roster_variant_outside_candidate_presets_fails() -> None:
    declared = load_declared_bake_intents(contracts_root())
    outside = BakeRoster((*ROSTER.rows, BakeRosterRow("campaigns", "2d", 240, 16.6, 123, 26)))
    assert BakeIntent("campaigns", "2d") not in declared.intents
    with pytest.raises(AssertionError, match="undeclared row campaigns#2d"):
        require_roster_declared(declared, outside, NOT_GRAPH_BAKED_PAGES)
