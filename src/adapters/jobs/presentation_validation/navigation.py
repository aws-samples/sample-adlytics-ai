# SPDX-License-Identifier: MIT-0
"""Project the governed navigation file into the shell's route manifest and the 404 allowlist.

ADR-115: ``presentation/navigation.yaml`` lists hubs and operator static routes, and a v2 page
entry that is a tab carries a ``navigation`` block. One generator writes two committed
projections, one per consumer: ``frontend/generated/route-manifest.json`` for the shell, and
``contracts/generated/route-allowlist.json`` for the ``StaticRouteRewrite`` allowlist (ADR-123).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from adapters.jobs.presentation_validation.loader import (
    _exact,  # pyright: ignore[reportPrivateUsage]
    _load_yaml_mapping,  # pyright: ignore[reportPrivateUsage]
    _mapping,  # pyright: ignore[reportPrivateUsage]
    _positive_integer,  # pyright: ignore[reportPrivateUsage]
    _sequence,  # pyright: ignore[reportPrivateUsage]
    _string,  # pyright: ignore[reportPrivateUsage]
    load_v2_page_schemas,
)
from domain.presentation.page_schemas import PageSchemaError, PresentationPageSchemaSet

NAVIGATION_PATH = Path("presentation/navigation.yaml")
ROUTE_MANIFEST_PATH = Path("frontend/generated/route-manifest.json")
ROUTE_ALLOWLIST_PATH = Path("contracts/generated/route-allowlist.json")
ROOT_ROUTE = "/"
NOT_FOUND_PAGE = "404.html"

# ADR-115 decision 7: each entitlement is an existing Cognito group with exactly one session
# field. The API keeps enforcing each entitlement on its own; this only decides what is listed.
ENTITLEMENT_SESSION_FIELDS: dict[str, str] = {
    "prompt-admin": "can_manage_prompts",
    "operations-status-read": "can_read_operational_status",
}

type Audience = Literal["customer", "operator"]
_AUDIENCES: frozenset[str] = frozenset({"customer", "operator"})


@dataclass(frozen=True, slots=True)
class NavigationHub:
    """One hub or group, in navigation order."""

    hub_id: str
    label: str
    short_label: str
    audience: Audience


@dataclass(frozen=True, slots=True)
class NavigationTab:
    """One routed tab: a v2 presentation page or an operator static route."""

    tab_id: str
    kind: Literal["page", "static"]
    hub_id: str
    route: str
    label: str
    description: str
    order: int
    entitlement: str | None


@dataclass(frozen=True, slots=True)
class Navigation:
    """The validated navigation: hubs, every routed tab, the root owner and not-found outputs."""

    hubs: tuple[NavigationHub, ...]
    tabs: tuple[NavigationTab, ...]
    root_tab_id: str
    not_found_outputs: tuple[str, ...]


def _nonblank(value: object, where: str) -> str:
    text = _string(value, where)
    if not text.strip():
        raise PageSchemaError(f"{where}: expected nonblank text.")
    return text


def _canonical_route(value: object, where: str) -> str:
    route = _nonblank(value, where)
    if not route.startswith("/") or not route.endswith("/"):
        raise PageSchemaError(f"{where}: routes must be canonical slash-delimited paths.")
    return route


def _hubs(value: object, where: str) -> tuple[NavigationHub, ...]:
    hubs: list[NavigationHub] = []
    for index, item in enumerate(_sequence(value, where)):
        context = f"{where}[{index}]"
        raw = _mapping(item, context)
        _exact(raw, {"hub_id", "label", "short_label", "audience"}, context)
        audience = _nonblank(raw["audience"], f"{context}.audience")
        if audience not in _AUDIENCES:
            raise PageSchemaError(f"{context}.audience: unknown value {audience!r}.")
        hubs.append(
            NavigationHub(
                hub_id=_nonblank(raw["hub_id"], f"{context}.hub_id"),
                label=_nonblank(raw["label"], f"{context}.label"),
                short_label=_nonblank(raw["short_label"], f"{context}.short_label"),
                audience="customer" if audience == "customer" else "operator",
            )
        )
    if not hubs or len({hub.hub_id for hub in hubs}) != len(hubs):
        raise PageSchemaError(f"{where}: hubs must be nonempty with unique hub ids.")
    return tuple(hubs)


def _static_tabs(value: object, where: str) -> tuple[NavigationTab, ...]:
    tabs: list[NavigationTab] = []
    for index, item in enumerate(_sequence(value, where)):
        context = f"{where}[{index}]"
        raw = _mapping(item, context)
        _exact(
            raw,
            {"route", "hub_id", "tab_id", "tab_label", "tab_description", "order", "entitlement"},
            context,
        )
        entitlement = _nonblank(raw["entitlement"], f"{context}.entitlement")
        if entitlement not in ENTITLEMENT_SESSION_FIELDS:
            raise PageSchemaError(
                f"{context}.entitlement: {entitlement!r} has no session field mapping."
            )
        tabs.append(
            NavigationTab(
                tab_id=_nonblank(raw["tab_id"], f"{context}.tab_id"),
                kind="static",
                hub_id=_nonblank(raw["hub_id"], f"{context}.hub_id"),
                route=_canonical_route(raw["route"], f"{context}.route"),
                label=_nonblank(raw["tab_label"], f"{context}.tab_label"),
                description=_nonblank(raw["tab_description"], f"{context}.tab_description"),
                order=_positive_integer(raw["order"], f"{context}.order"),
                entitlement=entitlement,
            )
        )
    return tuple(tabs)


def _not_found_outputs(value: object, where: str) -> tuple[str, ...]:
    outputs = tuple(_nonblank(item, f"{where}[]") for item in _sequence(value, where))
    if len(set(outputs)) != len(outputs) or NOT_FOUND_PAGE not in outputs:
        raise PageSchemaError(f"{where}: must be unique and include {NOT_FOUND_PAGE}.")
    if any(item.startswith("/") or not item.endswith(".html") for item in outputs):
        raise PageSchemaError(f"{where}: entries are export-relative .html paths.")
    return outputs


def _page_tabs(pages: PresentationPageSchemaSet) -> tuple[NavigationTab, ...]:
    return tuple(
        NavigationTab(
            tab_id=page.page_id,
            kind="page",
            hub_id=page.navigation.hub_id,
            route=page.route,
            label=page.navigation.tab_label,
            description=page.navigation.tab_description,
            order=page.navigation.order,
            entitlement=None,
        )
        for page in pages.pages
        if page.navigation is not None
    )


def _root_tab_id(pages: PresentationPageSchemaSet, tabs: tuple[NavigationTab, ...]) -> str:
    routed = {tab.tab_id for tab in tabs if tab.kind == "page"}
    owners = tuple(
        page.page_id
        for page in pages.pages
        if page.page_id in routed
        and (page.route == ROOT_ROUTE or ROOT_ROUTE in page.compatibility_routes)
    )
    if len(owners) != 1:
        raise PageSchemaError("exactly one routed tab must serve the root route /.")
    return owners[0]


def load_navigation(root: Path) -> Navigation:
    """Load and cross-check the navigation file against the v2 page declaration."""
    path = root / NAVIGATION_PATH
    raw = _load_yaml_mapping(path)
    _exact(raw, {"version", "hubs", "static_routes", "not_found_outputs"}, str(path))
    if raw["version"] != 1:
        raise PageSchemaError(f"{path}.version: expected 1.")
    hubs = _hubs(raw["hubs"], f"{path}.hubs")
    pages = load_v2_page_schemas(root)
    tabs = _page_tabs(pages) + _static_tabs(raw["static_routes"], f"{path}.static_routes")
    audiences = {hub.hub_id: hub.audience for hub in hubs}
    for tab in tabs:
        audience = audiences.get(tab.hub_id)
        if audience is None:
            raise PageSchemaError(f"{tab.tab_id}: navigation names unknown hub {tab.hub_id!r}.")
        # A presentation page has no entitlement; an operator surface always has one.
        if (audience == "operator") != (tab.entitlement is not None):
            raise PageSchemaError(
                f"{tab.tab_id}: an operator hub lists only entitled static routes, "
                "and a customer hub only presentation pages."
            )
    for label, values in (
        ("tab ids", tuple(tab.tab_id for tab in tabs)),
        ("routes", tuple(tab.route for tab in tabs)),
        ("hub orders", tuple(f"{tab.hub_id}:{tab.order}" for tab in tabs)),
    ):
        if len(set(values)) != len(values):
            raise PageSchemaError(f"navigation {label} must be unique.")
    hub_order = {hub.hub_id: index for index, hub in enumerate(hubs)}
    ordered = tuple(sorted(tabs, key=lambda tab: (hub_order[tab.hub_id], tab.order)))
    return Navigation(
        hubs=hubs,
        tabs=ordered,
        root_tab_id=_root_tab_id(pages, ordered),
        not_found_outputs=_not_found_outputs(raw["not_found_outputs"], f"{path}.not_found_outputs"),
    )


def route_manifest_document(navigation: Navigation) -> dict[str, object]:
    """Build the shell's manifest: hubs with a shipped tab, their tabs and the root."""
    hubs: list[dict[str, object]] = []
    for hub in navigation.hubs:
        tabs = [tab for tab in navigation.tabs if tab.hub_id == hub.hub_id]
        if not tabs:
            continue
        hubs.append(
            {
                "audience": hub.audience,
                "hub_id": hub.hub_id,
                "label": hub.label,
                "short_label": hub.short_label,
                "tabs": [
                    {
                        "description": tab.description,
                        "entitlement": (
                            None
                            if tab.entitlement is None
                            else {
                                "group": tab.entitlement,
                                "session_field": ENTITLEMENT_SESSION_FIELDS[tab.entitlement],
                            }
                        ),
                        "kind": tab.kind,
                        "label": tab.label,
                        "order": tab.order,
                        "route": tab.route,
                        "tab_id": tab.tab_id,
                    }
                    for tab in tabs
                ],
            }
        )
    return {
        "hubs": hubs,
        "not_found_outputs": list(navigation.not_found_outputs),
        "root": {"route": ROOT_ROUTE, "tab_id": navigation.root_tab_id},
        "version": 1,
    }


def route_allowlist_document(navigation: Navigation) -> dict[str, object]:
    """ADR-123 decision 1: the routes that rewrite to their index.html; all else is the 404."""
    routes = sorted({ROOT_ROUTE, *(tab.route for tab in navigation.tabs)})
    return {"not_found_page": f"/{NOT_FOUND_PAGE}", "routes": routes, "version": 1}


def _json(document: dict[str, object]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def rendered_projections(root: Path) -> dict[Path, str]:
    """Render both committed projections, keyed by their repository-relative path."""
    navigation = load_navigation(root)
    return {
        ROUTE_MANIFEST_PATH: _json(route_manifest_document(navigation)),
        ROUTE_ALLOWLIST_PATH: _json(route_allowlist_document(navigation)),
    }


def stale_projections(root: Path) -> tuple[Path, ...]:
    """Return the committed projections that are missing or differ from their sources."""
    return tuple(
        path
        for path, text in rendered_projections(root).items()
        if not (root / path).is_file() or (root / path).read_text() != text
    )


def main() -> int:
    """Write both projections, or fail when --check detects drift."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    if args.check:
        stale = stale_projections(root)
        for path in stale:
            print(f"navigation projection is stale: {path}")  # noqa: T201
        return 1 if stale else 0
    for path, text in rendered_projections(root).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        print(f"wrote {path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
