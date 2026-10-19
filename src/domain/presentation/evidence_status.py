# SPDX-License-Identifier: MIT-0
"""One server-written coverage block for every page that shows a tile or a change chip (ADR-119).

`EvidenceStatus.v2` lists, per metric slot, the display label, the role label, the missing
dates, the reason and the recovery text. The "Coverage for this window" sheet renders only this
block, so the browser keeps no code-to-label map and writes no coverage copy.

The block also carries the typed-absence rule of ADR-118 decision 5, so every surface reads one
rule from one place:

- **Missing day.** The source hasn't sent a day. The figure shows over the days that arrived,
  its basis names them ("6 of 7 days"), a ratio leaves the day out of both parts, and the
  change is withheld. The chip's place reads "{n} day(s) not yet sent.".
- **Partial day.** The source sent the day for some entities only. The chip's place reads
  "{n} partial day(s).". A count includes the entities that reported and says so; a ratio
  leaves the day out of both parts and names the covered days. The change is withheld.
- **Unavailable.** The figure isn't served at all, and the entry says why and what fills it.

A comparison whose slot is missing or partial carries no change; the artifact decoders refuse a
change beside such a slot, and refuse a withheld change beside a complete one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final, cast

from domain.shared_kernel.values import DateRange

__all__ = [
    "EVIDENCE_STATUS_KEY",
    "EVIDENCE_STATUS_VERSION",
    "CoverageFact",
    "CoverageKind",
    "CoverageState",
    "EvidenceStatus",
    "EvidenceStatusEntry",
    "evidence_status_from_payload",
    "evidence_status_payload",
    "labels_for",
    "require_entries",
    "withheld_slots",
    "write_evidence_status",
]

EVIDENCE_STATUS_KEY: Final = "evidence_status"
EVIDENCE_STATUS_VERSION: Final = 2

_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_DASH: Final = "\u2013"
_MAX_LISTED_FIGURES: Final = 3
_PAYLOAD_KEYS: Final = frozenset({"entries", "summary", "version", "window"})
_ENTRY_KEYS: Final = frozenset(
    {
        "absence_text",
        "coverage_text",
        "label",
        "missing_dates",
        "missing_text",
        "reason",
        "recovery",
        "role",
        "role_label",
        "slot",
        "state",
    }
)
_PRIOR_ROLES: Final = frozenset({"prior", "prior_daily"})


class CoverageState(StrEnum):
    """How much of the window one figure covers."""

    COMPLETE = "complete"
    MISSING_DAYS = "missing_days"
    PARTIAL_DAYS = "partial_days"
    UNAVAILABLE = "unavailable"


class CoverageKind(StrEnum):
    """Whether a figure adds up over days or is a ratio of two parts."""

    ADDITIVE = "additive"
    RATIO = "ratio"


@dataclass(frozen=True, slots=True)
class EvidenceStatusEntry:
    """One figure's served coverage: every string is the server's."""

    slot: str
    role: str
    label: str
    role_label: str
    state: CoverageState
    missing_dates: tuple[date, ...]
    missing_text: str
    reason: str
    recovery: str
    absence_text: str | None
    coverage_text: str | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.slot, "slot"),
            (self.role, "role"),
            (self.label, "label"),
            (self.role_label, "role label"),
            (self.missing_text, "missing text"),
            (self.reason, "reason"),
            (self.recovery, "recovery"),
        ):
            if not value.strip():
                raise ValueError(f"an evidence status entry requires its {name}.")
        if tuple(sorted(set(self.missing_dates))) != self.missing_dates:
            raise ValueError("evidence status missing dates must be ordered and unique.")
        withheld = self.state in {CoverageState.MISSING_DAYS, CoverageState.PARTIAL_DAYS}
        if withheld != (self.absence_text is not None) or withheld != (
            self.coverage_text is not None
        ):
            raise ValueError("only a missing or partial figure states its absence and coverage.")
        if withheld and not self.missing_dates:
            raise ValueError("a missing or partial figure names its missing dates.")
        if self.state is CoverageState.COMPLETE and self.missing_dates:
            raise ValueError("a complete figure has no missing dates.")
        for text in (self.absence_text, self.coverage_text):
            if text is not None and not text.strip():
                raise ValueError("evidence status absence and coverage text must be nonblank.")

    @property
    def withheld(self) -> bool:
        """Whether this figure's change is withheld (a missing or partial day)."""
        return self.state in {CoverageState.MISSING_DAYS, CoverageState.PARTIAL_DAYS}


@dataclass(frozen=True, slots=True)
class EvidenceStatus:
    """The page's coverage for its window: a summary line and one entry per figure."""

    window: DateRange
    summary: str
    entries: tuple[EvidenceStatusEntry, ...]

    def __post_init__(self) -> None:
        if not self.summary.strip() or not self.entries:
            raise ValueError("evidence status requires a summary and at least one entry.")
        keys = [(entry.slot, entry.role) for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("evidence status entries must be unique per slot and role.")
        prior = _prior(self.window)
        for entry in self.entries:
            period = prior if entry.role in _PRIOR_ROLES else self.window
            if any(not period.start <= day <= period.end for day in entry.missing_dates):
                raise ValueError("evidence status missing dates must fall inside their window.")

    def entry(self, slot: str, role: str = "current") -> EvidenceStatusEntry:
        """Return the entry for one figure; every declared figure has exactly one."""
        for item in self.entries:
            if item.slot == slot and item.role == role:
                return item
        raise ValueError(f"evidence status has no entry for {slot!r} ({role}).")


@dataclass(frozen=True, slots=True)
class CoverageFact:
    """What the server knows about one figure's coverage; the builder writes its words.

    `missing_dates` are days the source hasn't sent; `partial_dates` are days it sent for some
    entities only, and `partial_entities` names the entities still missing. `unavailable` is
    the served reason and recovery of a figure that isn't shown at all; `unsent` marks one whose
    source has sent nothing for its period, so its missing dates read "Not sent", not "None".
    """

    slot: str
    label: str
    role_label: str
    kind: CoverageKind = CoverageKind.ADDITIVE
    role: str = "current"
    missing_dates: tuple[date, ...] = ()
    partial_dates: tuple[date, ...] = ()
    partial_entities: tuple[str, ...] = ()
    entity_word: str = "entities"
    unavailable: tuple[str, str] | None = None
    unsent: bool = False

    def __post_init__(self) -> None:
        if self.unsent and (self.unavailable is None or self.missing_dates):
            raise ValueError("only an unavailable figure with no named days is unsent.")
        if self.missing_dates and self.partial_dates:
            raise ValueError("a figure has one coverage state: missing or partial days.")
        if self.partial_entities and not self.partial_dates:
            raise ValueError("only a partial day names the entities still missing.")
        if not self.entity_word.strip():
            raise ValueError("a coverage fact names its entities.")
        if self.unavailable is not None and (
            self.partial_dates or not all(text.strip() for text in self.unavailable)
        ):
            raise ValueError("an unavailable figure states its reason and recovery.")


def _prior(window: DateRange) -> DateRange:
    return DateRange(window.start - timedelta(days=window.days), window.start - timedelta(days=1))


def _day(value: date) -> str:
    return f"{_MONTHS[value.month - 1]} {value.day}"


def _span(start: date, end: date) -> str:
    if start == end:
        return _day(start)
    if start.month == end.month:
        return f"{_day(start)}{_DASH}{end.day}"
    return f"{_day(start)}{_DASH}{_day(end)}"


def _runs(days: Iterable[date]) -> tuple[tuple[date, date], ...]:
    ordered = sorted(set(days))
    runs: list[tuple[date, date]] = []
    for day in ordered:
        if runs and runs[-1][1] + timedelta(days=1) == day:
            runs[-1] = (runs[-1][0], day)
        else:
            runs.append((day, day))
    return tuple(runs)


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _days_text(days: Iterable[date]) -> str:
    """Name days as "Sep 13", "Sep 9-12 and 14-15" (with an en dash) or "Aug 31 and Sep 2"."""
    parts: list[str] = []
    previous_month: int | None = None
    for start, end in _runs(days):
        text = _span(start, end)
        if previous_month == start.month:
            text = text.removeprefix(f"{_MONTHS[start.month - 1]} ")
        parts.append(text)
        previous_month = end.month
    return _join(parts)


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _entry(fact: CoverageFact, window: DateRange) -> EvidenceStatusEntry:
    period = _prior(window) if fact.role in _PRIOR_ROLES else window
    if fact.unavailable is not None:
        reason, recovery = fact.unavailable
        return EvidenceStatusEntry(
            fact.slot,
            fact.role,
            fact.label,
            fact.role_label,
            CoverageState.UNAVAILABLE,
            tuple(sorted(set(fact.missing_dates))),
            _days_text(fact.missing_dates)
            if fact.missing_dates
            else "Not sent"
            if fact.unsent
            else "None",
            reason,
            recovery,
            None,
            None,
        )
    affected = tuple(sorted(set(fact.missing_dates or fact.partial_dates)))
    if not affected:
        return EvidenceStatusEntry(
            fact.slot,
            fact.role,
            fact.label,
            fact.role_label,
            CoverageState.COMPLETE,
            (),
            "None",
            "Every day of "
            + ("the prior window" if fact.role in _PRIOR_ROLES else "this window")
            + " has arrived.",
            "No action needed.",
            None,
            None,
        )
    count = len(affected)
    covered = tuple(
        day
        for offset in range(period.days)
        if (day := period.start + timedelta(days=offset)) not in affected
    )
    share = f"{len(covered)} of {period.days} days"
    covered_text = _days_text(covered) if covered else "no day"
    named = _days_text(affected)
    if fact.missing_dates:
        part = _plural(count, "the missing day is", "the missing days are")
        return EvidenceStatusEntry(
            fact.slot,
            fact.role,
            fact.label,
            fact.role_label,
            CoverageState.MISSING_DAYS,
            affected,
            named,
            f"Covers {covered_text} ({share})"
            + (f": {part} left out of both parts." if fact.kind is CoverageKind.RATIO else "."),
            "The figure and its change update by themselves when the "
            + _plural(count, "day arrives.", "days arrive."),
            f"{count} {_plural(count, 'day', 'days')} not yet sent.",
            share,
        )
    missing_text = (
        f"{named} ({_join(list(fact.partial_entities))})" if fact.partial_entities else named
    )
    absence = f"{count} partial {_plural(count, 'day', 'days')}."
    if fact.kind is CoverageKind.RATIO:
        part = _plural(count, "a partly reported day is", "partly reported days are")
        return EvidenceStatusEntry(
            fact.slot,
            fact.role,
            fact.label,
            fact.role_label,
            CoverageState.PARTIAL_DAYS,
            affected,
            missing_text,
            f"Covers {covered_text} ({share}): {part} left out of both parts.",
            "The day and the change show when the rest of the day arrives.",
            absence,
            f"{covered_text}, {share}",
        )
    return EvidenceStatusEntry(
        fact.slot,
        fact.role,
        fact.label,
        fact.role_label,
        CoverageState.PARTIAL_DAYS,
        affected,
        missing_text,
        f"Shows the {fact.entity_word} that reported on {named}.",
        "The figure and its change update by themselves when the rest of the day arrives.",
        absence,
        f"{named} {_plural(count, 'counts', 'count')} the {fact.entity_word} that reported",
    )


def _labels(entries: Iterable[EvidenceStatusEntry]) -> tuple[str, ...]:
    seen: list[str] = []
    for entry in entries:
        if entry.label not in seen:
            seen.append(entry.label)
    return tuple(seen)


def _figures(labels: tuple[str, ...]) -> str:
    if len(labels) > _MAX_LISTED_FIGURES:
        return f"{len(labels)} figures"
    return _join(list(labels))


def _summary(entries: tuple[EvidenceStatusEntry, ...]) -> str:
    partly = _labels(entry for entry in entries if entry.withheld)
    unavailable = _labels(entry for entry in entries if entry.state is CoverageState.UNAVAILABLE)
    if not partly and not unavailable:
        return "Every figure is complete for this window."
    sentences: list[str] = []
    if partly:
        verb = _plural(len(partly), "is", "are")
        shows = _plural(len(partly), "It shows", "They show")
        sentences.append(
            f"{_figures(partly)} {verb} partly reported for this window. {shows} what arrived, "
            "without a change, until the rest arrives."
        )
    if unavailable:
        sentences.append(
            f"{_figures(unavailable)} can't be shown for this window; each row says why."
        )
    if any(entry.state is CoverageState.COMPLETE for entry in entries):
        sentences.append("Every other figure is complete.")
    return " ".join(sentences)


def write_evidence_status(window: DateRange, facts: tuple[CoverageFact, ...]) -> EvidenceStatus:
    """Write one page's coverage block from what the server knows about each figure."""
    entries = tuple(_entry(fact, window) for fact in facts)
    return EvidenceStatus(window, _summary(entries), entries)


def withheld_slots(status: EvidenceStatus | None) -> frozenset[str]:
    """Return the current-window figures whose change is withheld; none without a block."""
    if status is None:
        return frozenset()
    return frozenset(
        entry.slot for entry in status.entries if entry.role == "current" and entry.withheld
    )


def evidence_status_payload(status: EvidenceStatus | None) -> dict[str, object] | None:
    """Encode the block, or `None` where a page serves none."""
    if status is None:
        return None
    return {
        "entries": [
            {
                "absence_text": entry.absence_text,
                "coverage_text": entry.coverage_text,
                "label": entry.label,
                "missing_dates": [day.isoformat() for day in entry.missing_dates],
                "missing_text": entry.missing_text,
                "reason": entry.reason,
                "recovery": entry.recovery,
                "role": entry.role,
                "role_label": entry.role_label,
                "slot": entry.slot,
                "state": entry.state.value,
            }
            for entry in status.entries
        ],
        "summary": status.summary,
        "version": EVIDENCE_STATUS_VERSION,
        "window": {"start": status.window.start.isoformat(), "end": status.window.end.isoformat()},
    }


def _object(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(cast("dict[str, object]", value)) != keys:
        raise ValueError(f"{label} fields are not exact.")
    return cast("dict[str, object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    parsed = date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be a canonical ISO date.")
    return parsed


def evidence_status_from_payload(value: object) -> EvidenceStatus | None:
    """Decode the block exactly; `None` decodes to `None`."""
    if value is None:
        return None
    item = _object(value, _PAYLOAD_KEYS, "evidence status")
    if item["version"] != EVIDENCE_STATUS_VERSION:
        raise ValueError("evidence status version is unsupported.")
    window = _object(item["window"], frozenset({"start", "end"}), "evidence status window")
    raw_entries = item["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError("evidence status entries must be a list.")
    entries: list[EvidenceStatusEntry] = []
    for raw in cast("list[object]", raw_entries):
        entry = _object(raw, _ENTRY_KEYS, "evidence status entry")
        dates = entry["missing_dates"]
        if not isinstance(dates, list):
            raise ValueError("evidence status missing dates must be a list.")
        try:
            state = CoverageState(_text(entry["state"], "evidence status state"))
        except ValueError:
            raise ValueError("evidence status state is unsupported.") from None
        entries.append(
            EvidenceStatusEntry(
                _text(entry["slot"], "evidence status slot"),
                _text(entry["role"], "evidence status role"),
                _text(entry["label"], "evidence status label"),
                _text(entry["role_label"], "evidence status role label"),
                state,
                tuple(
                    _date(day, "evidence status missing date")
                    for day in cast("list[object]", dates)
                ),
                _text(entry["missing_text"], "evidence status missing text"),
                _text(entry["reason"], "evidence status reason"),
                _text(entry["recovery"], "evidence status recovery"),
                _optional_text(entry["absence_text"], "evidence status absence text"),
                _optional_text(entry["coverage_text"], "evidence status coverage text"),
            )
        )
    return EvidenceStatus(
        DateRange(
            _date(window["start"], "evidence status window start"),
            _date(window["end"], "evidence status window end"),
        ),
        _text(item["summary"], "evidence status summary"),
        tuple(entries),
    )


def require_entries(  # noqa: PLR0913 -- a block, its roster, its window, and the caller
    status: EvidenceStatus | None,
    roster: tuple[tuple[str, str], ...],
    window: DateRange,
    *,
    label: str,
) -> None:
    """Refuse a block that doesn't list exactly the page's figures, in order, for its window."""
    if status is None:
        return
    if status.window != window:
        raise ValueError(f"{label} evidence status names another window.")
    if tuple((entry.slot, entry.role) for entry in status.entries) != roster:
        raise ValueError(f"{label} evidence status must list its declared figures in order.")


def labels_for(
    slots: Iterable[str], label_by_slot: Mapping[str, str], label: str
) -> dict[str, str]:
    """Return each slot's served label, refusing a slot the caller has no label for."""
    result: dict[str, str] = {}
    for slot in slots:
        value = label_by_slot.get(slot)
        if value is None or not value.strip():
            raise ValueError(f"{label} has no display label for {slot!r}.")
        result[slot] = value
    return result
