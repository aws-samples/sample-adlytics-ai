# SPDX-License-Identifier: MIT-0
"""Audiences' DSP campaigns, read one campaign at a time (design 01, ADR-141, ADR-152).

Pack 8's three campaign figures (impressions, spend, attributed sales) and the previous month's
segments (R1) are each read once for every campaign, grouped by campaign, and each is streamed
`ORDER BY` its grain through a `GroupSink` (`group_stream`), so the page never holds the
campaign population's rows.

- **Count pass**, in the render: the three figures and R1 are merged per campaign into one
  small tally: the segments its pages list, its largest impressions figure (its rank) and its
  roster grade (`grade_scope_rows`). The tallies are the campaign index, and they fix every
  body's count before the parent is built.
- **Page pass**, started in the render and drained by the stage: the same three figure requests
  (result reuse returns the count pass's results), merged again into each campaign's 50-row
  pages. Only the groups in flight are resident.

The reads carry no row limit (`RowCeiling.BAKE_GROUPED`). The tallies, the distinct segment set
and one campaign's segments are each bounded by the derived ceiling `BAKE_MAX_ROWS`, the one
memory-sized limit; past it the page is `rows_over_ceiling` (ADR-142, the Baker-memory action).
The page pass reading other than the count pass read is a defect.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from adapters.jobs.preset_artifact_publisher.group_stream import GroupStreams
from domain.measurement.rosters import (
    Roster,
    RosterEntry,
    RosterKind,
    coarser_grain_set,
    grade_scope_rows,
)
from domain.narrative.shaped_response import FreshnessStatus
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.presentation.audiences import (
    AUDIENCES_PAGE_ROWS,
    CampaignEntryState,
    CampaignIndexEntry,
    CampaignPage,
    CampaignRowRequest,
    CampaignSegment,
    campaign_segment,
    order_campaign_rows,
)
from domain.semantics.audience_segments import SEGMENT_ID_COLUMN, unnamed_segment_label
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.query import BAKE_MAX_ROWS

if TYPE_CHECKING:  # pragma: no cover
    from adapters.inbound.tool_handlers.measurement import PresentationMeasurementAnswerSlot
    from domain.measurement.figures import Figure
    from domain.semantics.audience_segments import SegmentLabel
    from domain.shared_kernel.values import DateRange

__all__ = [
    "CAMPAIGN_KEY_COLUMNS",
    "CAMPAIGN_PAGES_DIFFER",
    "CampaignCount",
    "CampaignCountScope",
    "CampaignPages",
    "CampaignTally",
    "count_campaigns",
]

CAMPAIGN_KEY_COLUMNS: Final = ("marketplace_key", "campaign_key")
"""A campaign group's key: the leading columns of `CAMPAIGN_GRAINS`, which the reads order by."""
CAMPAIGN_PAGES_DIFFER: Final = ErrorCode("AUDIENCES_CAMPAIGNS", 1)
"""The page pass read other than the count pass: a defect, never a limit."""
CAMPAIGN_FIGURE_READS: Final = ("impressions", "spend", "attributed sales")
"""The three figure reads, in the order every pass streams them; R1 follows in the count pass."""
_DELIVERY_FACT: Final = "fct_segment_delivery"
_CAMPAIGN_GRAIN_SET: Final = "campaign"


@dataclass(frozen=True, slots=True)
class CampaignTally:
    """One campaign's count-pass summary: all its pages need before they are rendered."""

    rows: int
    """The segments any of its three figures returned: its pages' rows."""
    largest: Decimal
    """Its largest matched-segment impressions figure: its rank, never a sum."""
    rostered: bool
    """Whether it returned any segment the month before (R1), so R1 graded it."""
    withdrawn_note: str | None
    """Its roster's withdrawal note, when the grade withdraws its grain set."""


@dataclass(frozen=True, slots=True)
class CampaignCount:
    """The count pass: each campaign's tally, the segments to name, and the reads' evidence."""

    tallies: dict[str, CampaignTally]
    segments: frozenset[str]
    complete: bool
    """Whether every read covered its month with nothing withheld (`grouped_answer_complete`)."""
    currencies: tuple[str | None, str | None]
    """The spend and attributed-sales currencies, each read's own."""


def _key(figure: Figure, column: str) -> str:
    return dict(figure.dimensions)[column]


def _complete(slot: PresentationMeasurementAnswerSlot, window: DateRange) -> bool:
    folded = slot.folded
    if folded is None:
        return False
    metadata = folded.metadata
    return (
        metadata.freshness_status is not FreshnessStatus.NEVER_LOADED
        and not metadata.coverage_holes
        and not metadata.suppression.rows_suppressed
        and metadata.actual_coverage_intervals == (window,)
    )


def _currency(slot: PresentationMeasurementAnswerSlot | None) -> str | None:
    folded = None if slot is None else slot.folded
    units = None if folded is None else folded.units
    return None if units is None else units.currency_code


def _refuse_cut(slots: tuple[PresentationMeasurementAnswerSlot, ...], *, over: bool) -> None:
    """Refuse the page when the index, or one campaign's segments, outgrew `BAKE_MAX_ROWS`.

    The reads have no row limit (`BAKE_GROUPED`), so a truncated slot is a group past its sink.
    """
    if over or any(slot.folded is not None and slot.folded.truncated for slot in slots):
        raise IsolatedRefusal(
            IsolatedRefusalReason.ROWS_OVER_CEILING, RefusalScope.PAGE, max_rows=BAKE_MAX_ROWS
        )


@dataclass(frozen=True, slots=True)
class CampaignCountScope:
    """What one marketplace's count pass grades against."""

    market: str
    platform: str
    windows: tuple[DateRange, DateRange | None]
    """The month, and the month before when R1 is read."""


def count_campaigns(streams: GroupStreams, scope: CampaignCountScope) -> CampaignCount:
    """Merge the three figures and R1 per campaign into one tally each (the count pass).

    `streams` reads impressions, spend and attributed sales, then R1 when the month before is
    published. A campaign only R1 returned isn't listed: the list is this month's.
    """
    window, prior = scope.windows
    coarser = coarser_grain_set(_DELIVERY_FACT, _CAMPAIGN_GRAIN_SET)
    tallies: dict[str, CampaignTally] = {}
    segments: set[str] = set()
    over = False
    streams.start()
    for merged in streams.groups():
        market, campaign = merged.key
        if market != scope.market:
            # As a held answer is refused: a row answered for another marketplace is never
            # attributed to this one.
            raise RuntimeError("pack 8's campaigns carry another marketplace's rows.")
        figures, prior_rows = merged.groups[:3], merged.groups[3:]
        returned = tuple(
            dict.fromkeys(_key(item, SEGMENT_ID_COLUMN) for group in figures for item in group)
        )
        if not returned:
            continue
        if (
            len(tallies) >= BAKE_MAX_ROWS
            or len(segments) + len(set(returned) - segments) > BAKE_MAX_ROWS
        ):
            over = True
            continue
        segments.update(returned)
        rostered = bool(prior_rows and prior_rows[0])
        note = None
        if rostered and prior is not None:
            keys = sorted({_key(item, SEGMENT_ID_COLUMN) for item in prior_rows[0]})
            roster = Roster(
                RosterKind.SEGMENT,
                window,
                market,
                scope.platform,
                tuple(RosterEntry(key, unnamed_segment_label(key)) for key in keys),
            )
            outcome = grade_scope_rows(roster, returned, coarser).outcome
            note = outcome.note if outcome.withdrawn else None
        largest = max(
            (Decimal(item.value) for item in figures[0]),
            default=Decimal(0),
        )
        tallies[campaign] = CampaignTally(len(returned), largest, rostered, note)
    slots = streams.slots()
    _refuse_cut(slots, over=over)
    windows = (window,) * len(CAMPAIGN_FIGURE_READS) + ((prior,) if prior is not None else ())
    complete = len(slots) == len(windows) and all(
        _complete(slot, month) for slot, month in zip(slots, windows, strict=True)
    )
    spend, revenue = slots[1:3] if len(slots) >= len(CAMPAIGN_FIGURE_READS) else (None, None)
    return CampaignCount(
        tallies,
        frozenset(segments),
        complete,
        (_currency(spend), _currency(revenue)),
    )


@dataclass(frozen=True, slots=True)
class CampaignPages:
    """The page pass: every listed campaign's 50-row pages, rendered as the stage draws them."""

    streams: GroupStreams
    entries: dict[str, CampaignIndexEntry]
    """Each listed campaign's index entry, by campaign key."""
    count: CampaignCount
    names: Callable[[str], SegmentLabel | None]
    """The DSP segment label for one segment, from the one label read."""

    @property
    def page_count(self) -> int:
        """Every campaign's pages: what the stream declares before it is drawn."""
        return sum(entry.page_count for entry in self.entries.values())

    def pages(self) -> Iterator[CampaignPage]:
        """Yield each campaign's pages in key order; refused campaigns have none."""
        spend_currency, revenue_currency = self.count.currencies
        drawn = 0
        for merged in self.streams.groups():
            _market, campaign = merged.key
            entry = self.entries.get(campaign)
            impressions, spend, revenue = (
                {_key(item, SEGMENT_ID_COLUMN): Decimal(item.value) for item in group}
                for group in merged.groups
            )
            returned = tuple(dict.fromkeys((*impressions, *spend, *revenue)))
            if not returned:
                continue
            tally = self.count.tallies.get(campaign)
            if entry is None or tally is None or tally.rows != len(returned):
                raise InvariantError(
                    CAMPAIGN_PAGES_DIFFER,
                    "The Audiences page pass read other campaigns than its count pass.",
                )
            if entry.state is CampaignEntryState.REFUSED:
                continue
            drawn += 1
            rows: tuple[CampaignSegment, ...] = ()
            if entry.state is CampaignEntryState.AVAILABLE:
                rows = order_campaign_rows(
                    tuple(
                        campaign_segment(
                            CampaignRowRequest(
                                (campaign, entry.label),
                                segment,
                                self.names(segment),
                                impressions.get(segment),
                                (spend.get(segment), spend_currency),
                                (revenue.get(segment), revenue_currency),
                            )
                        )
                        for segment in returned
                    )
                )
            for page in range(entry.page_count):
                start = page * AUDIENCES_PAGE_ROWS
                yield CampaignPage(
                    entry,
                    page,
                    rows[start : start + AUDIENCES_PAGE_ROWS],
                )
        if drawn != sum(
            1 for entry in self.entries.values() if entry.state is not CampaignEntryState.REFUSED
        ) or any(slot.folded is None or slot.folded.truncated for slot in self.streams.slots()):
            raise InvariantError(
                CAMPAIGN_PAGES_DIFFER,
                "The Audiences page pass read other campaigns than its count pass.",
            )
