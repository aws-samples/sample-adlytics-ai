# SPDX-License-Identifier: MIT-0
"""Rosters: the expected cells of a shopper-aggregate grain set with no closed enumeration.

Pack 2 (`amc-new-to-brand-buyers-channel`) returns one row per ad product, pack 3
(`amc-new-to-brand-buyers-campaign`) one per campaign, and AMC drops a key whose shoppers are
too few or none (design §4.5). Their expected cells aren't known at load; they're the keys with
`ad_spend > 0` in `fct_ad_exposure` for the same period, marketplace and platform: the roster
(plan D4). The planner requires the roster beside any
figure at such a grain set, so chat, page bakes and insights all read it the same way:

- every roster entry without a returned row is "not available" with the text "too few shoppers,
  or none". A suppressed row and a true zero look the same, so the text claims neither;
- a returned key that isn't on the roster (a row with no spend in the period) is kept;
- the loss is roster entries with no row over roster entries, graded with
  `LossScope.ROSTER`. Under 20 entries it is graded only, never withdrawn (design §4.4);
- with 20 or more entries, more than a fifth missing and a coarser served grain set (pack 3's
  campaigns point to new buyers per channel), the grain set is withdrawn: none of its figures is
  offered, and every rostered key reads "not available" with the pointer.

A roster lists the population the request measures: a request filtered to one ad product,
channel or channel group reads the keys with spend under the same filter (`roster_filters`), so
a campaign the filter excludes is never graded as missing. Withdrawal is decided per delivery: a
request pinning one rostered key is graded on the keys the whole delivery returned
(`Roster.returned`, `apply_pinned_roster`) and names only its own key.

A segment roster (pack 8, C1, design §6.5) is the campaign's segments returned the month
before, for the same marketplace: segments are an open domain with no spend of their own. It is
read under the request's campaign (`SEGMENT_ROSTER_SCOPE`), graded and never withdrawn, because
no coarser served view of segments exists; a campaign's first month has an empty roster and says
so (`FIRST_MONTH_NOTE`). A C2 segment roster (packs 10-12) is the same, per profile path
(`PROFILE_ROSTER_SCOPE`): the segments that path's delivery returned the month before.

The roster is read through `ports.outbound.rosters`; this module stays pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from domain.data_contracts.contract import DataContract
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from domain.shopper_aggregates.grain_sets import GrainSet, ServedGrainSets
from domain.shopper_aggregates.loss import (
    GrainSetLoss,
    LossGrade,
    LossRequest,
    LossScope,
    grade_loss,
)

__all__ = [
    "FIRST_MONTH_NOTE",
    "PERIODIC_CELL_COLUMNS",
    "PROFILE_ROSTER_SCOPE",
    "ROSTER_MEDIA_COLUMNS",
    "ROSTER_NOT_RETURNED_TEXT",
    "ROSTER_REFUSED",
    "ROSTER_UNAVAILABLE",
    "ROSTER_WITHDRAWN",
    "SEGMENT_NOT_RETURNED_TEXT",
    "SEGMENT_PROFILE_FACT",
    "SEGMENT_ROSTER_SCOPE",
    "SERVED_GRAIN_SETS",
    "PinnedKey",
    "Roster",
    "RosterEntry",
    "RosterKind",
    "RosterOutcome",
    "apply_pinned_roster",
    "apply_roster",
    "coarser_grain_set",
    "require_roster",
    "roster_column",
    "roster_filters",
    "roster_key_columns",
    "roster_kind_for",
    "roster_lines",
    "roster_scope_columns",
    "roster_scope_required",
]

ROSTER_UNAVAILABLE: Final = ErrorCode(area="PLAN", number=7)
"""A roster-graded grain set was planned without its roster, or with another period's."""
ROSTER_REFUSED: Final = ErrorCode(area="PLAN", number=8)
"""The roster can't be read for the period: a day lacks a spend publication, or no marketplace."""

ROSTER_WITHDRAWN: Final = ErrorCode(area="PLAN", number=9)
"""A comparison or ranking over a withdrawn grain set: none of its figures is offered."""

ROSTER_NOT_RETURNED_TEXT: Final = "too few shoppers, or none"
"""The reason a rostered key with no row reads "not available" (design §4.5)."""

PERIODIC_CELL_COLUMNS: Final = frozenset(
    {"period_start", "period_end", "time_grain", "grain_set", "marketplace_key"}
)
"""The grain every shopper-aggregate grain set shares: one delivery's period and marketplace."""

ROSTER_MEDIA_COLUMNS: Final = ("channel_group_key", "channel_key", "ad_product_key")
"""Media filters a roster is read under, coarsest first; `fct_ad_exposure` carries each."""

SEGMENT_ROSTER_SCOPE: Final = ("campaign_key",)
"""What a C1 segment roster is read under: the campaign whose segments it lists (design §6.5)."""

PROFILE_ROSTER_SCOPE: Final = ("profile_path",)
"""What a C2 segment roster is read under: the profile path whose delivery it lists (packs
10-12, design §6.5), the same marketplace and path the month before."""

SEGMENT_PROFILE_FACT: Final = "fct_segment_profile"
"""The C2 fact; its segment roster is per profile path, not per campaign."""

_SEGMENT_SCOPES: Final = (*SEGMENT_ROSTER_SCOPE, *PROFILE_ROSTER_SCOPE)

SEGMENT_NOT_RETURNED_TEXT: Final = "too few shoppers this month, and it was listed last month"
"""The reason a rostered segment with no row reads "not available", never index or count 0."""

FIRST_MONTH_NOTE: Final = "First month: segments with too few shoppers aren't listed."
"""A segment roster with no earlier month to read: nothing can be named as missing."""


class RosterKind(StrEnum):
    """What a roster lists: ad products or campaigns with spend, or last month's segments."""

    AD_PRODUCT = "ad_product"
    CAMPAIGN = "campaign"
    SEGMENT = "audience_segment"
    """A campaign's audience segments; the value is the grain name, as the other two are."""


_ROSTER_COLUMNS: Final[Mapping[str, RosterKind]] = {
    "segment_id": RosterKind.SEGMENT,
    "campaign_key": RosterKind.CAMPAIGN,
    "ad_product_key": RosterKind.AD_PRODUCT,
}
"""Finest first: a campaign grain set is graded against campaigns, not ad products, and a
campaign-by-segment grain set (pack 8) against the campaign's segments."""

SERVED_GRAIN_SETS: Final[Mapping[str, ServedGrainSets]] = {
    "fct_new_to_brand_buyers": ServedGrainSets(
        "fct_new_to_brand_buyers",
        (
            GrainSet("platform", "new buyers for the whole Amazon Ads network"),
            GrainSet("channel", "new buyers per channel"),
            GrainSet("campaign", "new buyers per campaign"),
        ),
    ),
    "fct_exposure_frequency": ServedGrainSets(
        "fct_exposure_frequency",
        (
            GrainSet("platform", "frequency for the whole Amazon Ads network"),
            GrainSet("channel", "frequency per channel"),
        ),
    ),
}
"""Each shopper-aggregate fact's served grain sets, coarsest first (ADR-099 point 2)."""


def roster_key_columns(contract: DataContract) -> tuple[str, ...]:
    """Return a shopper-aggregate contract's grain columns finer than its period and marketplace.

    Empty for a platform grain set, which expects exactly its marketplace cell. Non-empty for a
    grain set whose cells come back one row per key (`ad_product_key` on pack 2): those have no
    closed enumeration at load, so the load writes no `not_returned` cell for them.
    """
    if not contract.records_cell_state:
        return ()
    return tuple(column for column in contract.grain if column not in PERIODIC_CELL_COLUMNS)


def roster_kind_for(contract: DataContract) -> RosterKind | None:
    """Return the roster a contract's grain set is graded against, or None for none."""
    columns = roster_key_columns(contract)
    return next((kind for column, kind in _ROSTER_COLUMNS.items() if column in columns), None)


def roster_scope_required(kind: RosterKind, fact: str | None = None) -> tuple[str, ...]:
    """Return the filters a roster of `kind` can't be read without.

    A C1 segment roster (`fct_segment_delivery`) needs its campaign, a C2 one
    (`fct_segment_profile`) its profile path; spend rosters need nothing.
    """
    if kind is not RosterKind.SEGMENT:
        return ()
    return PROFILE_ROSTER_SCOPE if fact == SEGMENT_PROFILE_FACT else SEGMENT_ROSTER_SCOPE


def roster_scope_columns(kind: RosterKind) -> tuple[str, ...]:
    """Return the columns a roster of `kind` may be read under: media, or a segment roster's scope.

    A segment roster is read under its campaign (C1) or its profile path (C2), never both: a
    figure on one fact carries only its own fact's scope column.
    """
    return _SEGMENT_SCOPES if kind is RosterKind.SEGMENT else ROSTER_MEDIA_COLUMNS


def roster_column(kind: RosterKind) -> str:
    """Return the fact column a roster's keys are values of."""
    return next(column for column, item in _ROSTER_COLUMNS.items() if item is kind)


def roster_filters(kind: RosterKind, filters: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Return the media filters a request's roster is read under, coarsest first.

    The roster's own column is never one: a request pinning one rostered key is graded on the
    whole roster (`apply_pinned_roster`), not on a roster of one.
    """
    if kind is RosterKind.SEGMENT:
        return tuple((column, filters[column]) for column in _SEGMENT_SCOPES if column in filters)
    own = roster_column(kind)
    return tuple(
        (column, filters[column])
        for column in ROSTER_MEDIA_COLUMNS
        if column != own and column in filters
    )


def coarser_grain_set(fact: str, grain_set: str) -> GrainSet | None:
    """Return the served grain set a withdrawn one points to, or None when there's none."""
    served = SERVED_GRAIN_SETS.get(fact)
    return None if served is None else served.coarser_served(grain_set)


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One key with spend in the period, and the governed label an answer names it by."""

    key: str
    label: str

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("a roster entry names its key and label.")


@dataclass(frozen=True, slots=True)
class Roster:
    """The keys with `ad_spend > 0` summed over exactly the period's days (plan D4).

    Read from CURRENT `fct_ad_exposure` publications for one marketplace and platform; the
    reader refuses when any day of the period lacks one, so a roster is never partial.
    """

    kind: RosterKind
    period: DateRange
    marketplace_key: str
    platform_key: str
    entries: tuple[RosterEntry, ...]
    filters: tuple[tuple[str, str], ...] = ()
    """The media filters the spend was read under (`roster_filters`): the measured population."""
    returned: tuple[str, ...] | None = None
    """The keys the graded delivery returned over the whole roster, read only for a request
    pinning one rostered key, so its grade is the delivery's (`apply_pinned_roster`)."""
    first_period: bool = False
    """A segment roster with no earlier month to read (`FIRST_MONTH_NOTE`): empty, not lost."""

    def __post_init__(self) -> None:
        if not self.marketplace_key.strip() or not self.platform_key.strip():
            raise ValueError("a roster names its marketplace and platform.")
        keys = tuple(entry.key for entry in self.entries)
        if len(set(keys)) != len(keys):
            raise ValueError("a roster lists each key once.")
        if any(column not in roster_scope_columns(self.kind) for column, _ in self.filters):
            raise ValueError("a roster is filtered by its own scope columns only.")
        if self.first_period and (self.entries or self.kind is not RosterKind.SEGMENT):
            raise ValueError("only an empty segment roster is a first period.")

    @property
    def keys(self) -> tuple[str, ...]:
        """Return the roster's keys in order."""
        return tuple(entry.key for entry in self.entries)


@dataclass(frozen=True, slots=True)
class RosterOutcome:
    """A roster applied to the keys an answer returned."""

    roster: Roster
    absent: tuple[RosterEntry, ...]
    """Roster entries with no returned row: each reads "not available"."""
    off_roster: tuple[str, ...]
    """Returned keys with no spend in the period: kept, in the order they came back."""
    loss: GrainSetLoss

    @property
    def note(self) -> str | None:
        """The server note a degraded or withdrawn roster shows, or None."""
        return self.loss.note

    @property
    def withdrawn(self) -> bool:
        """The grain set is withdrawn: no figure of it is offered (design §4.4)."""
        return self.loss.grade is LossGrade.WITHDRAWN


def apply_roster(
    roster: Roster, returned: Iterable[str], coarser: GrainSet | None
) -> RosterOutcome:
    """Grade a roster against the returned keys; a roster never withdraws under 20 entries.

    An empty roster has nothing to miss: 0 / 0 grades "not degraded" (`GrainSetLoss.ratio`).
    """
    seen = tuple(dict.fromkeys(returned))
    absent = tuple(entry for entry in roster.entries if entry.key not in seen)
    loss = grade_loss(
        LossRequest(
            expected=len(roster.entries),
            missing=len(absent),
            scope=LossScope.ROSTER,
            coarser_served=coarser,
        )
    )
    return RosterOutcome(
        roster=roster,
        absent=absent,
        off_roster=tuple(key for key in seen if key not in roster.keys),
        loss=loss,
    )


@dataclass(frozen=True, slots=True)
class PinnedKey:
    """The one rostered key a request filters to, and whether its figure came back."""

    key: str
    answered: bool


def apply_pinned_roster(
    roster: Roster, pinned: PinnedKey, coarser: GrainSet | None
) -> RosterOutcome:
    """Grade a request pinning one rostered key on its whole delivery; name only that key.

    Withdrawal is per delivery (design §4.4, "per delivery, at bake"): when the delivery's keys
    (`Roster.returned`) withdraw the grain set, the pinned key reads "not available" with the
    pointer, as it would in the breakdown. Otherwise the request reads as a roster of its one
    key. Raises `InvariantError` (`ROSTER_UNAVAILABLE`) when the delivery's keys weren't read.
    """
    if roster.returned is None:
        raise InvariantError(
            ROSTER_UNAVAILABLE,
            "A figure for one rostered key was planned without the keys its delivery returned.",
            context={"roster": roster.kind.value, "window": str(roster.period)},
        )
    delivery = apply_roster(roster, roster.returned, coarser)
    own = replace(
        roster, entries=tuple(entry for entry in roster.entries if entry.key == pinned.key)
    )
    returned = (pinned.key,) if pinned.answered else ()
    if not delivery.withdrawn:
        return apply_roster(own, returned, coarser)
    return RosterOutcome(
        roster=own,
        absent=own.entries,
        off_roster=tuple(key for key in returned if key not in own.keys),
        loss=delivery.loss,
    )


def roster_lines(outcome: RosterOutcome) -> tuple[str, ...]:
    """Name each rostered key with no row, then the grade's note, as answer sentences.

    A withdrawn grain set offers none of its figures, so every rostered key reads "not
    available" with the pointer to the coarser grain set (design §4.4: its cells still show,
    never silently dropped), and returned keys off the roster are counted, not named.
    """
    if outcome.withdrawn:
        hidden = len(outcome.off_roster)
        return (
            *(
                f"{entry.label} is not available: {outcome.note}."
                for entry in outcome.roster.entries
            ),
            *(() if not hidden else (_off_roster_hidden(outcome, hidden),)),
        )
    reason = (
        SEGMENT_NOT_RETURNED_TEXT
        if outcome.roster.kind is RosterKind.SEGMENT
        else ROSTER_NOT_RETURNED_TEXT
    )
    lines = tuple(f"{entry.label} is not available: {reason}." for entry in outcome.absent)
    if outcome.roster.first_period:
        lines = (*lines, FIRST_MONTH_NOTE)
    return lines if outcome.note is None else (*lines, outcome.note)


def _off_roster_hidden(outcome: RosterOutcome, hidden: int) -> str:
    """Count a withdrawn grain set's returned keys with no spend: shown neither as figures."""
    noun = "ad product" if outcome.roster.kind is RosterKind.AD_PRODUCT else "campaign"
    if hidden == 1:
        return f"1 more {noun} with no ad spend in the period isn't shown either: {outcome.note}."
    return (
        f"{hidden} more {noun}s with no ad spend in the period aren't shown either: {outcome.note}."
    )


def require_roster(
    request: tuple[RosterKind | None, Roster | None, DateRange, Mapping[str, str]],
) -> Roster | None:
    """Return the roster a grain set needs, or raise when it's absent or for another population.

    `request` is (the grain set's roster kind, the attached roster, the plan's window, the plan's
    filters). A roster read for another period, or under other media filters than the plan's,
    would grade keys the plan can't return. A grain set graded against no roster ignores any
    roster attached.
    """
    kind, roster, window, filters = request
    if kind is None:
        return None
    if roster is None or roster.kind is not kind or roster.period != window:
        raise InvariantError(
            ROSTER_UNAVAILABLE,
            "A figure graded against a roster was planned without the roster for its period.",
            context={"roster": kind.value, "window": str(window)},
        )
    if roster.filters != roster_filters(kind, filters):
        raise InvariantError(
            ROSTER_UNAVAILABLE,
            "A figure graded against a roster was planned with a roster read under other "
            "media filters.",
            context={"roster": kind.value, "window": str(window)},
        )
    return roster
