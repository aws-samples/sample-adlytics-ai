# SPDX-License-Identifier: MIT-0
"""Page-level absence copy: the one home of every server-owned no-pointer message (ADR-139).

Temporal contract rule 3: the page shows the server's text on every unavailable preset. Each v2
`window_unavailable.message` is chosen from `ABSENCE_COPY` by an explicit trigger whose value is
its catalog ID in `docs/ui/page-absence-copy.md`, which an equality test keeps identical.

Truth rule for the no-`CURRENT`-pointer rows: that trigger fires when the source has never
delivered, when this preset's bake has not committed yet, or when the source returned nothing for
this period, so no row says the data "hasn't arrived" or is "being prepared". Each says only that
the page isn't ready to show, names what it is built from and what it will show, conditioned on
the data. Pure domain: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "ABSENCE_COPY",
    "OTHER_WINDOWS_READY",
    "SERVING_CHECK_TEXT",
    "AbsenceTrigger",
    "absence_message",
]

_PERIOD_LABEL: Final = "{period_label}"


class AbsenceTrigger(StrEnum):
    """The exact condition that selects one catalog row; each value is its catalog ID."""

    REACH_PERIOD_NOT_PUBLISHED = "ABS-REACH-1"
    AUDIENCES_MONTH_NOT_PUBLISHED = "ABS-AUD-1"
    AUDIENCES_MONTH_OVER_BOUND = "ABS-AUD-2"
    RETENTION_VINTAGE_NOT_PUBLISHED = "ABS-RET-1"
    SCORECARD_WINDOW_NOT_PUBLISHED = "ABS-SC-1"
    SCORECARD_PERIOD_NOT_PUBLISHED = "ABS-SC-2"
    NEW_CUSTOMERS_WINDOW_NOT_PUBLISHED = "ABS-NC-1"
    NEW_CUSTOMERS_PERIOD_NOT_PUBLISHED = "ABS-NC-2"


ABSENCE_COPY: Final[Mapping[AbsenceTrigger, str]] = MappingProxyType(
    {
        AbsenceTrigger.REACH_PERIOD_NOT_PUBLISHED: (
            "Reach for {period_label} isn't ready to show yet. It's built from your Amazon "
            "Marketing Cloud sponsored ads data, plus Amazon DSP where you run it. When it's "
            "ready, you'll see the reach, frequency and full-funnel figures your data supports "
            "for this period."
        ),
        AbsenceTrigger.AUDIENCES_MONTH_NOT_PUBLISHED: (
            "Audiences for {period_label} isn't ready to show yet. It's built from your Amazon "
            "Marketing Cloud audience segment data. When it's ready, you'll see which audience "
            "segments your buyers belong to, where there are enough shoppers to show them."
        ),
        AbsenceTrigger.AUDIENCES_MONTH_OVER_BOUND: (
            "Audiences for {period_label} has more segment and campaign detail than this page "
            "can show. Ask the assistant about a DSP campaign to see its figures."
        ),
        AbsenceTrigger.RETENTION_VINTAGE_NOT_PUBLISHED: (
            "Retention isn't ready to show yet. It's built from your Amazon Marketing Cloud "
            "new-to-brand data. When it's ready, you'll see how many of the new customers your "
            "ads won come back to buy again, where there's enough evidence to show it."
        ),
        AbsenceTrigger.SCORECARD_WINDOW_NOT_PUBLISHED: (
            "The Scorecard for this window isn't ready to show yet. It's built from your Amazon "
            "Ads data, plus Vendor Central sales where you have them. When it's ready, you'll "
            "see the return on ad spend by network and channel your data supports for this "
            "window."
        ),
        AbsenceTrigger.SCORECARD_PERIOD_NOT_PUBLISHED: (
            "The Scorecard for {period_label} isn't ready to show yet. It's built from your "
            "Amazon Ads data, plus Vendor Central sales where you have them. When it's ready, "
            "you'll see the return on ad spend by network and channel your data supports for "
            "{period_label}."
        ),
        AbsenceTrigger.NEW_CUSTOMERS_WINDOW_NOT_PUBLISHED: (
            "New customers for this window isn't ready to show yet. It's built from your Amazon "
            "Marketing Cloud new-to-brand data and your Amazon Ads spend. When it's ready, "
            "you'll see the new-to-brand purchases your data supports for this window. New "
            "buyers are measured for complete weeks and months."
        ),
        AbsenceTrigger.NEW_CUSTOMERS_PERIOD_NOT_PUBLISHED: (
            "New customers for {period_label} isn't ready to show yet. It's built from your "
            "Amazon Marketing Cloud new-to-brand data and your Amazon Ads spend. When it's "
            "ready, you'll see the new-to-brand purchases and new buyers your data supports for "
            "{period_label}."
        ),
    }
)
"""Catalog rows ABS-REACH-1 through ABS-NC-2, keyed by their trigger."""

OTHER_WINDOWS_READY: Final = "Other windows above are ready to view."
"""ABS-ALT: appended when the served control lists a published preset other than the selected."""

SERVING_CHECK_TEXT: Final = (
    "This page's latest data is still being checked before it's shown. Try again later."
)
"""API-0002: the `error` text of an `ungranted_generation` or `serving_refresh_pending` refusal."""


def absence_message(
    trigger: AbsenceTrigger, *, period_label: str | None, other_window_ready: bool
) -> str:
    """Render one trigger's catalog text, with ABS-ALT appended when another window is ready.

    A template naming `{period_label}` given no label is a programming error (`ValueError`).
    """
    template = ABSENCE_COPY[trigger]
    if _PERIOD_LABEL in template:
        if period_label is None:
            raise ValueError(f"absence copy {trigger.value} needs a period label.")
        template = template.replace(_PERIOD_LABEL, period_label)
    return f"{template} {OTHER_WINDOWS_READY}" if other_window_ready else template
