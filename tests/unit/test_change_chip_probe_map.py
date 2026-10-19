# SPDX-License-Identifier: MIT-0
"""The dev probe's declared tile directions match the metric dictionary (ADR-121)."""

from __future__ import annotations

import re

import pytest
from change_support import ROOT, SEMANTICS

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-121")]

_PROBE_MAP = ROOT / "validation" / "change_chip_checks.mjs"
_ENTRY = re.compile(r"^\s+'(?P<label>[^']+)': \['(?P<metric>\w+)', '(?P<favourable>\w+)'\],$")


def _declared() -> list[tuple[str, str, str]]:
    return [
        (match["label"], match["metric"], match["favourable"])
        for line in _PROBE_MAP.read_text(encoding="utf-8").splitlines()
        if (match := _ENTRY.match(line)) is not None
    ]


def test_every_declared_tile_direction_is_the_dictionary_direction() -> None:
    declared = _declared()
    # Four Campaigns tiles and eleven Retail health tiles.
    assert len(declared) == 4 + 11
    mismatched = [
        (label, metric, favourable)
        for label, metric, favourable in declared
        if SEMANTICS.for_metric(metric).favourable.value != favourable
    ]
    assert mismatched == []
