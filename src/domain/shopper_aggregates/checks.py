# SPDX-License-Identifier: MIT-0
"""Load checks every shopper-aggregate delivery passes: the privacy bar and repeated totals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from domain.shopper_aggregates.findings import ShopperAggregateFinding

SUPPRESSED_COLUMN = "rows_suppressed"
USER_COUNT_COLUMN = "distinct_user_count"
ESCALATED_BAR = 100
REPEATED_TOTAL_MISMATCH = "REPEATED_TOTAL_MISMATCH"
"""The typed finding for a repeated total whose copies differ (design §7a): a cohort's size
differs between two of its follow-up month rows, so the delivery is refused."""


class BarVerdict(StrEnum):
    """What the bar check decided (ADR-099 point 10; design §4.4, §7a)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    """An unsuppressed row reads above the bar: a MEDIUM or HIGH column slipped in."""
    ESCALATION = "escalation"
    """Rejected too, but every unsuppressed row reads exactly 100 under a bar of 2: a derived
    label raised AMC's bar. Recorded as an escalation, and the pack moves to a version with
    `expectedBar: 100` at narrower grains (review DS5-4)."""


@dataclass(frozen=True, slots=True)
class BarRequest:
    """Every delivered row, suppressed or not, and the pack's declared `expectedBar`."""

    rows: tuple[Mapping[str, object], ...]
    expected_bar: int


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """The verdict, the highest count read on an unsuppressed row, and why it was refused."""

    verdict: BarVerdict
    highest: int | None
    finding: ShopperAggregateFinding | None = None


def _suppressed(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def check_bar(request: BarRequest) -> BarOutcome:
    """Reject a delivery whose unsuppressed rows report `distinct_user_count` above the bar.

    AMC reports the real count up to its cap (`docs/sources/amc.md` §5), so under a bar of 2 an
    unsuppressed row reads at most 2. A row whose suppression flag or count can't be read is
    refused too: the check can't vouch for it.
    """
    counts: list[int] = []
    for index, row in enumerate(request.rows):
        suppressed = _suppressed(row.get(SUPPRESSED_COLUMN))
        count = _count(row.get(USER_COUNT_COLUMN))
        if suppressed is None or (not suppressed and count is None):
            return BarOutcome(
                BarVerdict.REJECTED,
                None,
                ShopperAggregateFinding(
                    "BAR_UNREADABLE",
                    f"row {index}: {SUPPRESSED_COLUMN} or {USER_COUNT_COLUMN} can't be read.",
                ),
            )
        if not suppressed and count is not None:
            counts.append(count)
    highest = max(counts, default=None)
    if highest is None or highest <= request.expected_bar:
        return BarOutcome(BarVerdict.ACCEPTED, highest)
    above = sum(1 for count in counts if count > request.expected_bar)
    if request.expected_bar < ESCALATED_BAR and all(count == ESCALATED_BAR for count in counts):
        return BarOutcome(
            BarVerdict.ESCALATION,
            highest,
            ShopperAggregateFinding(
                "BAR_ESCALATION",
                f"every unsuppressed row reads {ESCALATED_BAR} under expectedBar "
                f"{request.expected_bar}: a derived label raised the bar.",
            ),
        )
    return BarOutcome(
        BarVerdict.REJECTED,
        highest,
        ShopperAggregateFinding(
            "BAR_EXCEEDED",
            f"{above} unsuppressed row(s) read {USER_COUNT_COLUMN} above expectedBar "
            f"{request.expected_bar} (highest {highest}).",
        ),
    )


@dataclass(frozen=True, slots=True)
class RepeatedTotal:
    """A total the pack repeats on several rows, such as a cohort's size on each follow-up month.

    `keyed_by` names the columns that identify one copy group: every row with the same values
    there must carry the same total.
    """

    column: str
    keyed_by: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.column.strip() or not self.keyed_by:
            raise ValueError("a repeated total needs a column and the columns it repeats over.")


@dataclass(frozen=True, slots=True)
class RepeatedTotalRequest:
    """One delivery's rows and the totals it repeats."""

    rows: tuple[Mapping[str, object], ...]
    totals: tuple[RepeatedTotal, ...]


def check_repeated_totals(request: RepeatedTotalRequest) -> tuple[ShopperAggregateFinding, ...]:
    """Refuse a delivery where copies of one repeated total differ (design §7a).

    Copies come from one AMC result, so a difference means the pack computed the total twice
    over different populations. A null or blank copy (a `not_returned` row, or a withheld row's
    nulled count) is skipped: it isn't a copy.
    """
    findings: list[ShopperAggregateFinding] = []
    for total in request.totals:
        copies: dict[tuple[str, ...], set[str]] = {}
        for row in request.rows:
            value = row.get(total.column)
            if value is None or not str(value).strip():
                continue
            group = tuple(str(row.get(column)) for column in total.keyed_by)
            copies.setdefault(group, set()).add(str(value))
        for group, values in sorted(copies.items()):
            if len(values) > 1:
                where = ", ".join(
                    f"{column}={value}" for column, value in zip(total.keyed_by, group, strict=True)
                )
                findings.append(
                    ShopperAggregateFinding(
                        REPEATED_TOTAL_MISMATCH,
                        f"{total.column} differs across copies at {where}: "
                        f"{', '.join(sorted(values))}.",
                    )
                )
    return tuple(findings)
