# SPDX-License-Identifier: MIT-0
"""The browser probe's roster-page routes are the bake roster's pages (D2 seam sweep finding 11).

The browser probe (`validation/probe_frontend_browser.mjs`) reads only the generated route
allowlist; the bake roster is a Python projection (`BAKE_TASKS`). This pins the two together, so
an allowlist whose pages drift from the roster fails here, before any probe runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from domain.operations.execution_graph_release import BakeRoster

pytestmark = [pytest.mark.unit]

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "validation" / "roster_routes.mjs"
_ALLOWLIST = _ROOT / "contracts" / "generated" / "route-allowlist.json"
_IN_DEPTH = {"/campaigns/", "/retail-health/"}
"""The roster pages the browser probe's earlier blocks walk in depth."""
_NOT_ROSTER_PAGES = {"/", "/admin/prompts/"}
"""Allowlisted routes that are not roster pages: the root and the prompt admin."""


def _derive(routes: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the probe's own derivation in node over `routes`."""
    node = shutil.which("node")
    assert node is not None, "node is required (the frontend and `make check` use it too)."
    script = (
        f"import {{ rosterRoutes }} from {json.dumps(_MODULE.as_uri())};"
        "process.stdout.write(JSON.stringify(rosterRoutes(JSON.parse(process.env.ADL_ROUTES))));"
    )
    # A-SUBPROCESS-001: node and an inline test script, no shell; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(  # noqa: S603 -- a fixed argument list, no shell
        [node, "--input-type=module", "-e", script],
        env={**os.environ, "ADL_ROUTES": json.dumps(routes)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _allowlist() -> list[str]:
    return list(json.loads(_ALLOWLIST.read_text(encoding="utf-8"))["routes"])


def test_the_allowlisted_page_routes_are_exactly_the_bake_rosters_pages() -> None:
    roster_routes = {f"/{page}/" for page in BakeRoster.from_contract().pages}
    assert set(_allowlist()) - _NOT_ROSTER_PAGES == roster_routes


def test_the_browser_walks_every_roster_page_route_not_walked_in_depth() -> None:
    derived = _derive(_allowlist())
    assert derived.returncode == 0, derived.stderr
    routes = json.loads(derived.stdout)
    expected = {f"/{page}/" for page in BakeRoster.from_contract().pages} - _IN_DEPTH
    assert set(routes) == expected
    assert len(routes) == len(expected) == 5


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        (["/", "/admin/prompts/", "/campaigns/", "/retail-health/"], "leaves no page"),
        (["/", "/admin/prompts/", "/campaigns/", "/reach/"], "no longer carries /retail-health/"),
        ([], "no longer carries /"),
    ],
    ids=["empty_after_exclusion", "stale_in_depth_route", "empty_allowlist"],
)
def test_the_browser_derivation_refuses_an_empty_or_stale_route_list(
    routes: list[str], message: str
) -> None:
    derived = _derive(routes)
    assert derived.returncode != 0
    assert message in derived.stderr
