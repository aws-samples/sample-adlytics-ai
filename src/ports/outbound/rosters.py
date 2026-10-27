# SPDX-License-Identifier: MIT-0
"""Which ad products (or campaigns) had spend in a period: a grain set's roster (plan D4).

A segment roster (pack 8) is instead the campaign's segments returned the month before. A bake
reads those grouped by campaign, as a figure read, and asks here only for the earlier history of
the campaigns that returned none (`CampaignHistoryPort`).

A shopper-aggregate grain set that comes back one row per key has no closed enumeration at load:
AMC drops a key whose shoppers are too few or none. Its expected cells are the keys with
`ad_spend > 0` summed over exactly the period's days in CURRENT `fct_ad_exposure` publications,
for one marketplace and platform (design §4.5), under the figure's own media filters. This port
reads them; the domain applies them
(`domain.measurement.rosters`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.measurement.rosters import Roster, RosterKind, roster_scope_columns
from domain.shared_kernel.values import DateRange
from ports.outbound.query import RowCeiling

__all__ = [
    "CampaignHistories",
    "CampaignHistoriesRequest",
    "CampaignHistory",
    "CampaignHistoryPort",
    "RosterPort",
    "RosterRequest",
]


@dataclass(frozen=True, slots=True)
class RosterRequest:
    """What a roster lists, over which complete period, for one marketplace and platform."""

    kind: RosterKind
    period: DateRange
    marketplace_key: str
    platform_key: str
    filters: tuple[tuple[str, str], ...] = ()
    """The request's media filters (`roster_filters`): spend is read under the same ad product,
    channel or channel group the figure is, so the roster lists only keys it can return. A
    segment roster is read under the request's campaign (C1, `SEGMENT_ROSTER_SCOPE`) or profile
    path (C2, `PROFILE_ROSTER_SCOPE`) instead."""
    fact: str | None = None
    """The fact the graded figure reads; a segment roster's reader follows it (C1 or C2)."""
    ceiling: RowCeiling = RowCeiling.NARRATION
    """Which caller kind's row ceiling the reader's own reads ask for (ADR-143): a bake source's
    roster reads at the bake ceiling, chat's at narration's. Never a tool input."""

    def __post_init__(self) -> None:
        if not self.marketplace_key.strip() or not self.platform_key.strip():
            raise ValueError("a roster is read for one marketplace and one platform.")
        if any(column not in roster_scope_columns(self.kind) for column, _ in self.filters):
            raise ValueError("a roster is filtered by its own scope columns only.")


class CampaignHistory(StrEnum):
    """What a campaign's earlier months say when it returned no segment the month before."""

    FIRST_MONTH = "first_month"
    """Never observed in an earlier published month: an empty roster that says so."""
    OBSERVED = "observed"
    """Observed earlier while the previous month is published: an empty roster, not a first."""
    HISTORY_GAP = "history_gap"
    """Observed before an unpublished month, or unknowable past a withheld month: refused."""


@dataclass(frozen=True, slots=True)
class CampaignHistoriesRequest:
    """A bake's listed DSP campaigns with no segments the month before, graded at once (C1).

    `campaigns` is sorted and may be empty: the walk over older months is read whenever they
    exist, so a bake's requests depend only on its served periods.
    """

    period: DateRange
    marketplace_key: str
    platform_key: str
    campaigns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.marketplace_key.strip() or not self.platform_key.strip():
            raise ValueError("a campaign history is read for one marketplace and one platform.")
        if self.campaigns != tuple(sorted(set(self.campaigns))):
            raise ValueError("campaign histories are requested once each, sorted.")


@dataclass(frozen=True, slots=True)
class CampaignHistories:
    """Each requested campaign's history grade."""

    histories: Mapping[str, CampaignHistory]


@runtime_checkable
class CampaignHistoryPort(Protocol):
    """Reads every listed campaign's earlier segment history in one grouped walk (01 §3.2)."""

    def campaign_histories(self, request: CampaignHistoriesRequest) -> CampaignHistories:
        """Return each requested campaign's grade.

        Raises `DomainError` (`ROSTER_REFUSED`) when an older month can't be read in full.
        """
        ...


@runtime_checkable
class RosterPort(Protocol):
    """Reads a period's roster from spend publications, never from the graded fact."""

    def roster(self, request: RosterRequest) -> Roster:
        """Return the keys with spend over every day of the period.

        Raises `DomainError` (`ROSTER_REFUSED`) when any day of the period lacks a CURRENT
        spend publication, so a roster is never partial.
        """
        ...
