# SPDX-License-Identifier: MIT-0
"""The Retention withheld-cell legend: one exact string on the server and in the mockups (E-B-3)."""

from __future__ import annotations

import pathlib

import pytest

from domain.presentation.retention import WITHHELD_CELL_LEGEND

pytestmark = [pytest.mark.unit]

ROOT = pathlib.Path(__file__).resolve().parents[2]
RETENTION = ROOT / "docs/ui/retention"
# The approved mockup's wording, which claimed every `not_returned` cell was suppressed.
REPLACED_LEGEND = "Amazon withheld this cell because too few shoppers bought again"


def test_server_legend_is_the_exact_string() -> None:
    assert WITHHELD_CELL_LEGEND == "Not available: too few shoppers bought again, or none"


@pytest.mark.parametrize("name", ["mockup.html", "mockup-light.html", "mockup-dialog-insight.html"])
def test_every_grid_mockup_shows_the_server_legend(name: str) -> None:
    text = (RETENTION / name).read_text()
    assert text.count(f"<span>{WITHHELD_CELL_LEGEND}</span>") == 1
    assert REPLACED_LEGEND not in text


def test_the_low_level_design_quotes_the_server_legend() -> None:
    text = (RETENTION / "low-level-design.md").read_text()
    assert f'"{WITHHELD_CELL_LEGEND}"' in text


def test_no_retention_document_keeps_the_replaced_legend() -> None:
    offenders = [
        path.name
        for path in sorted(RETENTION.iterdir())
        if path.suffix in {".md", ".html"} and REPLACED_LEGEND in path.read_text()
    ]
    assert offenders == []
