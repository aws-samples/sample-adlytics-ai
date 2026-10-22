# SPDX-License-Identifier: MIT-0
"""ADR-139 layer 3: the page absence catalog is one home, and each trigger serves its exact row.

`docs/ui/page-absence-copy.md` owns the wording; `domain.presentation.absence_copy` must equal it.
Every identity is fictional.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from domain.presentation.absence_copy import (
    ABSENCE_COPY,
    OTHER_WINDOWS_READY,
    SERVING_CHECK_TEXT,
    AbsenceTrigger,
    absence_message,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-139")]

_CATALOG = Path(__file__).resolve().parents[2] / "docs/ui/page-absence-copy.md"
_SECTION = re.compile(r"^### (?P<id>[A-Z0-9-]+)\n(?P<body>.*?)(?=^#{2,3} |\Z)", re.M | re.S)
_TEXT = re.compile(r'^- Text: "(?P<text>[^"]+)"$', re.M)


def _catalog() -> dict[str, str]:
    rows: dict[str, str] = {}
    for section in _SECTION.finditer(_CATALOG.read_text()):
        text = _TEXT.search(section["body"])
        if text is not None:
            rows[section["id"]] = text["text"]
    return rows


def test_the_catalog_doc_and_the_domain_catalog_are_equal() -> None:
    rows = _catalog()
    assert {trigger.value: rows[trigger.value] for trigger in AbsenceTrigger} == {
        trigger.value: text for trigger, text in ABSENCE_COPY.items()
    }
    assert rows["ABS-ALT"] == OTHER_WINDOWS_READY
    assert rows["API-0002"] == SERVING_CHECK_TEXT
    # Every quoted catalog row is a domain row, ABS-ALT, API-0002 or a browser row.
    assert set(rows) == {
        *(trigger.value for trigger in AbsenceTrigger),
        "ABS-ALT",
        "API-0002",
        "PI-DOMAIN",
        "ABS-CAMP-1",
        "ABS-CAMP-2",
        "ABS-RH-1",
        "ABS-RH-2",
        # Retail health empty and insufficient bodies (OD-UX-9 E1), browser-owned.
        "ABS-RH-3",
        "ABS-RH-4",
        # Overview band absences (ADR-151), browser-owned.
        "ABS-OV-1",
        "ABS-OV-2",
    }


@pytest.mark.parametrize("trigger", list(AbsenceTrigger))
def test_each_trigger_renders_its_row_with_and_without_abs_alt(trigger: AbsenceTrigger) -> None:
    label = "September 2026"
    plain = absence_message(trigger, period_label=label, other_window_ready=False)
    assert plain == ABSENCE_COPY[trigger].replace("{period_label}", label)
    assert "{" not in plain
    alternative = absence_message(trigger, period_label=label, other_window_ready=True)
    assert alternative == f"{plain} {OTHER_WINDOWS_READY}"
    if "{period_label}" in ABSENCE_COPY[trigger]:
        with pytest.raises(ValueError, match=trigger.value):
            absence_message(trigger, period_label=None, other_window_ready=False)
    else:
        assert absence_message(trigger, period_label=None, other_window_ready=False) == plain


def test_no_row_claims_the_data_has_not_arrived_or_is_being_prepared() -> None:
    for text in ABSENCE_COPY.values():
        assert "hasn't arrived" not in text
        assert "being prepared" not in text
