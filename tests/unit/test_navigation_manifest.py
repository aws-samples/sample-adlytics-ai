# SPDX-License-Identifier: MIT-0
"""ADR-115 and ADR-123: one navigation file, two committed projections, both kept current."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.jobs.presentation_validation.loader import (
    PRESENTATION_PAGE_SCHEMAS_PATH,
    load_v2_page_schemas,
)
from adapters.jobs.presentation_validation.navigation import (
    ENTITLEMENT_SESSION_FIELDS,
    NAVIGATION_PATH,
    ROUTE_ALLOWLIST_PATH,
    ROUTE_MANIFEST_PATH,
    load_navigation,
    rendered_projections,
    stale_projections,
)
from domain.presentation.page_schemas import PageSchemaError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _workspace(tmp_path: Path, *, navigation: str | None = None, pages: str | None = None) -> Path:
    for relative, text in (
        (NAVIGATION_PATH, navigation),
        (PRESENTATION_PAGE_SCHEMAS_PATH, pages),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text() if text is None else text)
    return tmp_path


def _navigation_text() -> str:
    return (ROOT / NAVIGATION_PATH).read_text()


def _pages_text() -> str:
    return (ROOT / PRESENTATION_PAGE_SCHEMAS_PATH).read_text()


def test_committed_route_manifest_and_allowlist_match_the_navigation_file() -> None:
    assert stale_projections(ROOT) == ()
    for path, text in rendered_projections(ROOT).items():
        assert (ROOT / path).read_text() == text


def test_a_stale_projection_is_reported_by_path(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    for path, text in rendered_projections(root).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    assert stale_projections(root) == ()

    manifest = root / ROUTE_MANIFEST_PATH
    manifest.write_text(manifest.read_text().replace("Campaigns", "Campaign Portfolio"))
    (root / ROUTE_ALLOWLIST_PATH).unlink()

    assert set(stale_projections(root)) == {ROUTE_MANIFEST_PATH, ROUTE_ALLOWLIST_PATH}


def test_only_hubs_with_a_shipped_tab_are_listed_in_navigation_order() -> None:
    manifest = json.loads((ROOT / ROUTE_MANIFEST_PATH).read_text())

    assert [hub["hub_id"] for hub in manifest["hubs"]] == [
        "overview",
        "performance",
        "reach",
        "customers",
        "retail",
        "operations",
    ]
    assert [tab["route"] for hub in manifest["hubs"] for tab in hub["tabs"]] == [
        "/",
        "/scorecard/",
        "/campaigns/",
        "/reach/",
        "/audiences/",
        "/new-customers/",
        "/retention/",
        "/retail-health/",
        "/products-and-ads/",
        "/admin/prompts/",
    ]
    assert manifest["root"] == {"route": "/", "tab_id": "overview"}


@pytest.mark.covers("ADR-151")
def test_the_composed_overview_owns_the_root_over_routed_source_pages() -> None:
    navigation = load_navigation(ROOT)
    overview = next(tab for tab in navigation.tabs if tab.tab_id == "overview")

    assert (overview.kind, overview.route, overview.hub_id, overview.entitlement) == (
        "composed",
        "/",
        "overview",
        None,
    )
    assert overview.sources == ("scorecard", "retail-health", "new-customers", "campaigns")
    assert navigation.root_tab_id == "overview"
    manifest = json.loads((ROOT / ROUTE_MANIFEST_PATH).read_text())
    hub = next(hub for hub in manifest["hubs"] if hub["hub_id"] == "overview")
    # The manifest carries the kind, never the sources: the browser reads each source itself.
    assert hub["tabs"] == [
        {
            "description": overview.description,
            "entitlement": None,
            "kind": "composed",
            "label": "Growth check",
            "order": 1,
            "question": "Is your advertising growing your business?",
            "route": "/",
            "tab_id": "overview",
        }
    ]


def test_every_customer_page_tab_carries_its_question_and_a_blank_one_is_refused(
    tmp_path: Path,
) -> None:
    """UI refresh 3.8: the question is navigation text, optional, and never blank."""
    manifest = json.loads((ROOT / ROUTE_MANIFEST_PATH).read_text())
    questions = {tab["tab_id"]: tab["question"] for hub in manifest["hubs"] for tab in hub["tabs"]}
    assert questions["scorecard"] == "Is my ad spend paying back?"
    assert questions["products-and-ads"] is None
    assert questions["prompt-admin"] is None
    root = _workspace(
        tmp_path,
        navigation=_navigation_text().replace(
            "question: Is your advertising growing your business?", 'question: " "'
        ),
    )

    with pytest.raises(PageSchemaError, match="question"):
        load_navigation(root)
    pages = (ROOT / PRESENTATION_PAGE_SCHEMAS_PATH).read_text()
    blank_page = _workspace(
        tmp_path / "pages",
        pages=pages.replace("question: Is my ad spend paying back?", 'question: " "'),
    )
    with pytest.raises(PageSchemaError, match="question"):
        load_navigation(blank_page)
    unknown_key = _workspace(
        tmp_path / "unknown",
        pages=pages.replace(
            "question: Is my ad spend paying back?", "subtitle: Is my ad spend paying back?"
        ),
    )
    with pytest.raises(PageSchemaError, match="unknown subtitle"):
        load_navigation(unknown_key)


def test_a_composed_source_that_is_not_a_routed_page_tab_is_refused(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        navigation=_navigation_text().replace(
            "sources: [scorecard, retail-health, new-customers, campaigns]",
            "sources: [scorecard, channel-performance]",
        ),
    )

    with pytest.raises(PageSchemaError, match="composed sources must be routed v2 page tabs"):
        load_navigation(root)


def test_a_composed_tab_cannot_sit_in_the_operator_group(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        navigation=_navigation_text().replace(
            "    hub_id: overview\n    tab_id: overview",
            "    hub_id: operations\n    tab_id: overview",
        ),
    )

    with pytest.raises(PageSchemaError, match="composed tab belongs to a customer hub"):
        load_navigation(root)


def test_two_root_owners_are_refused(tmp_path: Path) -> None:
    # A second composed tab at `/` beside the Overview: the root must have exactly one owner.
    second = """  - route: /
    hub_id: plan
    tab_id: plan-overview
    tab_label: Plan
    tab_description: A second owner of the root
    order: 1
    sources: [scorecard]
"""
    anchor = "static_routes:\n"
    text = _navigation_text()
    assert text.count(anchor) == 1
    root = _workspace(tmp_path, navigation=text.replace(anchor, second + anchor))

    with pytest.raises(PageSchemaError, match=r"routes must be unique|exactly one routed tab"):
        load_navigation(root)


def test_a_page_entry_that_owns_the_root_is_refused(tmp_path: Path) -> None:
    pages = _pages_text()
    anchor = (
        "    compatibility_routes: []\n    navigation:\n"
        "      hub_id: performance\n      tab_label: Scorecard"
    )
    assert pages.count(anchor) == 1
    root = _workspace(tmp_path, pages=pages.replace(anchor, anchor.replace("[]", '["/"]')))

    with pytest.raises(PageSchemaError, match="must not own the root route"):
        load_navigation(root)


def test_a_page_without_a_navigation_block_is_not_routed() -> None:
    routed = {tab.tab_id for tab in load_navigation(ROOT).tabs}
    unrouted = {
        page.page_id for page in load_v2_page_schemas(ROOT).pages if page.navigation is None
    }

    assert unrouted
    assert routed.isdisjoint(unrouted)
    allowlist = json.loads((ROOT / ROUTE_ALLOWLIST_PATH).read_text())
    assert "/channel-performance/" not in allowlist["routes"]
    assert allowlist == {
        "not_found_page": "/404.html",
        "routes": [
            "/",
            "/admin/prompts/",
            "/audiences/",
            "/campaigns/",
            "/new-customers/",
            "/products-and-ads/",
            "/reach/",
            "/retail-health/",
            "/retention/",
            "/scorecard/",
        ],
        "version": 1,
    }


def test_each_operator_tab_names_its_entitlement_and_session_field() -> None:
    manifest = json.loads((ROOT / ROUTE_MANIFEST_PATH).read_text())
    operations = next(hub for hub in manifest["hubs"] if hub["hub_id"] == "operations")

    assert operations["audience"] == "operator"
    assert operations["tabs"][0]["entitlement"] == {
        "group": "prompt-admin",
        "session_field": "can_manage_prompts",
    }
    assert ENTITLEMENT_SESSION_FIELDS == {
        "prompt-admin": "can_manage_prompts",
        "operations-status-read": "can_read_operational_status",
    }


def test_an_entitlement_without_a_session_field_mapping_is_refused(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        navigation=_navigation_text().replace(
            "entitlement: prompt-admin", "entitlement: billing-admin"
        ),
    )

    with pytest.raises(PageSchemaError, match="no session field mapping"):
        load_navigation(root)


def test_a_tab_naming_an_unknown_hub_is_refused(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path, pages=_pages_text().replace("hub_id: performance", "hub_id: marketing", 1)
    )

    with pytest.raises(PageSchemaError, match="unknown hub 'marketing'"):
        load_navigation(root)


def test_a_presentation_page_cannot_sit_in_the_operator_group(tmp_path: Path) -> None:
    root = _workspace(tmp_path, pages=_pages_text().replace("hub_id: retail", "hub_id: operations"))

    with pytest.raises(PageSchemaError, match="operator hub lists only entitled static routes"):
        load_navigation(root)


def test_two_tabs_cannot_share_a_hub_order(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        pages=_pages_text()
        .replace(
            "sponsored ads only or one channel\n      order: 2",
            "sponsored ads only or one channel\n      order: 1",
        )
        .replace("hub_id: retail", "hub_id: performance"),
    )

    with pytest.raises(PageSchemaError, match="hub orders must be unique"):
        load_navigation(root)


def test_the_not_found_outputs_must_include_the_404_page(tmp_path: Path) -> None:
    root = _workspace(tmp_path, navigation=_navigation_text().replace("  - 404.html\n", ""))

    with pytest.raises(PageSchemaError, match=r"include 404\.html"):
        load_navigation(root)


def test_the_page_loader_refuses_an_unknown_navigation_field(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        pages=_pages_text().replace("      order: 1\n", "      order: 1\n      icon: x\n", 1),
    )

    with pytest.raises(PageSchemaError, match="unknown icon"):
        load_v2_page_schemas(root)


_PIPELINE_STATUS_ENTRY = """  - route: /admin/status/
    hub_id: operations
    tab_id: pipeline-status
    tab_label: Data pipeline status
    tab_description: Whether each data feed is landing, and which pages are current
    order: 2
    entitlement: operations-status-read
"""


@pytest.mark.covers("ADR-115")
def test_data_pipeline_status_is_dark_until_its_navigation_entry_is_added(tmp_path: Path) -> None:
    # Delivery slices, Slice 11: the page waits for its serving, so neither projection names it.
    projections = rendered_projections(ROOT)
    assert "/admin/status/" not in projections[ROUTE_MANIFEST_PATH]
    assert "/admin/status/" not in json.loads(projections[ROUTE_ALLOWLIST_PATH])["routes"]
    # Positive control: the same file with the entry routes it, under its own session field.
    text = _navigation_text()
    anchor = "    entitlement: prompt-admin\n"
    assert text.count(anchor) == 1
    lit = rendered_projections(
        _workspace(tmp_path, navigation=text.replace(anchor, anchor + _PIPELINE_STATUS_ENTRY))
    )
    assert "/admin/status/" in json.loads(lit[ROUTE_ALLOWLIST_PATH])["routes"]
    operations = next(
        hub for hub in json.loads(lit[ROUTE_MANIFEST_PATH])["hubs"] if hub["hub_id"] == "operations"
    )
    assert [(tab["route"], tab["entitlement"]) for tab in operations["tabs"]] == [
        ("/admin/prompts/", {"group": "prompt-admin", "session_field": "can_manage_prompts"}),
        (
            "/admin/status/",
            {"group": "operations-status-read", "session_field": "can_read_operational_status"},
        ),
    ]
