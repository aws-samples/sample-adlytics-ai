# SPDX-License-Identifier: MIT-0
"""Pack-version coverage: a column a later query pack adds is answered only where every day has it.

`amc-attributed-conversion` pack version 2 adds `new_to_brand_total_purchases`. Deliveries the
earlier version produced load with the column NULL (`FieldMapping.present_from_pack_version`),
so a sum over a window with any such day would undercount without saying so. A day counts as
covered by the query pack version of the delivery currently published for it, never by whether
it has rows: a covered day with no purchases counts zero.

The planner refuses a metric reading such a column over a window with any uncovered day, so
chat, page bakes and insights all inherit the rule. Its refusal names the first fully covered
date. The publication evidence records each partition's pack version at load
(`pack_version_evidence`), and an adapter reads it back through the existing
`CurrentPublicationPort`; this module stays pure.

The contract-coverage variant reuses the same evidence for a writer that joined a fact late
(`FieldMapping.served_where_published`, plan D6). `amc-dsp-reach` writes `reach_users` on DSP rows
that the DSP delivery contracts landed long before it existed, so those days carry no reach. Its
column is covered from version 1, which means "this contract has a CURRENT publication for the
day", and only for a request pinning a grain value the contract owns (`channel_key` or
`ad_product_key` `amazon_dsp`). A request that doesn't pin those rows is planned as before.
A window wholly before the writer's first publication is refused naming that first day, which
the adapter reads only when the window isn't covered (`PackDayVersions.first_published`).

A late writer's rows carry only its own columns. A figure that carries a column the writer
doesn't write (a currency figure's `currency_code`) groups that writer's rows apart from the
other operand's rows, so no row holds both operands. Such a figure is refused over the rows the
writer owns (`SPLIT_WRITER_REFUSED`): `cost_per_reach` over DSP rows, whose reach comes from
`amc-dsp-reach` and whose spend and currency come from `amc-dsp-impressions` (plan D7).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import cast

from domain.data_contracts.contract import DataContract
from domain.semantics.calendar_periods import day_label
from domain.semantics.validation import (
    PrepublicationRuleFamily,
    PrepublicationRuleResult,
    PublicationEvidence,
    RuleOutcome,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError, ValidationError
from domain.shared_kernel.values import DateRange

__all__ = [
    "CONTRACT_COVERAGE_REFUSED",
    "PACK_COVERAGE_RECOVERY",
    "PACK_COVERAGE_REFUSED",
    "PACK_COVERAGE_UNAVAILABLE",
    "SPLIT_WRITER_REFUSED",
    "PackCoverageRequest",
    "PackCoverageVerdict",
    "PackDayVersions",
    "PackVersionedColumn",
    "contract_coverage_text",
    "pack_coverage_text",
    "pack_coverage_verdict",
    "pack_version_evidence",
    "pack_version_from_evidence",
    "pack_version_rule_id",
    "pack_versioned_columns",
    "require_pack_coverage",
    "split_writer_text",
]

PACK_COVERAGE_REFUSED = ErrorCode(area="PLAN", number=3)
"""A window includes a day published from a pack version that doesn't output the column."""

PACK_COVERAGE_UNAVAILABLE = ErrorCode(area="PLAN", number=4)
"""A pack-versioned or publication-gated column was planned on a runtime with no coverage."""

CONTRACT_COVERAGE_REFUSED = ErrorCode(area="PLAN", number=5)
"""A window pinning a late writer's rows includes a day that writer has no publication for."""

SPLIT_WRITER_REFUSED = ErrorCode(area="PLAN", number=6)
"""A figure carries a column the late writer of one of its operands doesn't write."""

PACK_COVERAGE_RECOVERY = "They appear once every day of a window is measured."
"""The last sentence of every uncovered-window text: what brings the section back."""

_RULE_SUFFIX = "query-pack-version.v1"


def pack_version_rule_id(contract_id: str) -> str:
    """Name the publication evidence rule that records a partition's query pack version.

    A contract-assertions result: it records the pack version the load's pack-version column
    assertion checked the delivery against (`check_pack_version_columns`). Provenance is a
    source-conformance family, which a candidate result can't carry.
    """
    return f"contract.{contract_id}.{_RULE_SUFFIX}"


def pack_version_evidence(contract_id: str, query_pack_version: int) -> PrepublicationRuleResult:
    """Build the result a load records on each partition of a contract with versioned columns."""
    if query_pack_version < 1:
        raise ValueError("a query pack version starts at 1.")
    return PrepublicationRuleResult(
        family=PrepublicationRuleFamily.CONTRACT_ASSERTIONS,
        rule_id=pack_version_rule_id(contract_id),
        outcome=RuleOutcome.PASSED,
        detail=json.dumps(
            {"query_pack_id": contract_id, "query_pack_version": query_pack_version},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def pack_version_from_evidence(evidence: PublicationEvidence, contract_id: str) -> int | None:
    """Read the recorded query pack version back, or None when the partition recorded none.

    A partition published before the rule existed records none, so it reads as uncovered: the
    gate fails closed rather than assuming the later version.
    """
    rule_id = pack_version_rule_id(contract_id)
    for result in evidence.results:
        if result.rule_id != rule_id or result.outcome is not RuleOutcome.PASSED:
            continue
        parsed = cast("object", json.loads(result.detail))
        if not isinstance(parsed, dict):
            return None
        version = cast("dict[str, object]", parsed).get("query_pack_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        return version
    return None


@dataclass(frozen=True, slots=True)
class PackVersionedColumn:
    """One fact column a contract's governed pack outputs only from `from_version` on."""

    contract_id: str
    fact: str
    column: str
    from_version: int
    owned: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Empty for a pack-versioned column, which every request reading it is gated on.

    For a publication-gated one, the grain values its contract owns: the gate applies only to a
    request pinning one of these columns to one of these values.
    """
    writes: tuple[str, ...] = ()
    """For a publication-gated column, every column its contract writes; empty otherwise.

    A figure carrying a column outside this set can't be answered over the rows the contract owns
    (`SPLIT_WRITER_REFUSED`).
    """

    @property
    def publication_gated(self) -> bool:
        """Whether this is the contract-coverage variant (`served_where_published`)."""
        return bool(self.owned)

    def applies_to(self, filters: Mapping[str, str]) -> bool:
        """Whether a request with these filters reads rows this column's coverage governs."""
        if not self.owned:
            return True
        return any(filters.get(column) in values for column, values in self.owned)


def pack_versioned_columns(contracts: tuple[DataContract, ...]) -> tuple[PackVersionedColumn, ...]:
    """Every pack-versioned and publication-gated column the contracts declare, in contract order.

    A publication-gated column is covered by any recorded pack version, so it reads from 1.
    """
    return tuple(
        column
        for contract in contracts
        for column in (
            *(
                PackVersionedColumn(
                    contract.contract_id,
                    contract.target_table,
                    mapping.target_column,
                    cast("int", mapping.present_from_pack_version),
                )
                for mapping in contract.pack_versioned_fields
            ),
            *(
                PackVersionedColumn(
                    contract.contract_id,
                    contract.target_table,
                    mapping.target_column,
                    1,
                    tuple(sorted(contract.owned_grain_values.items())),
                    tuple(sorted(contract.target_columns)),
                )
                for mapping in contract.publication_gated_fields
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class PackDayVersions:
    """The query pack version currently published for each day, for one contract.

    A day maps to None when it has no current publication in every selected population, or its
    publication recorded no version. With two populations a day takes the lower version, so a day
    is covered only when every population's delivery carries the column.
    """

    contract_id: str
    versions: Mapping[date, int | None] = field(default_factory=dict[date, int | None])
    first_published: date | None = None
    """For a publication-gated contract whose window isn't covered, its first published day.

    The day every selected population has started on: the latest of their earliest CURRENT
    publications recording a version, read only when some requested day isn't covered. None when
    some population has none, or when it wasn't read; the refusal then doesn't name a day.

    It is a start, not a guaranteed covered day. With one population (every commerce alias maps
    to `amazon_us` today) it is that population's first published day. With two, an earlier
    starter's gap on the named day would refuse the retry; a second population's onboarding
    re-decides this rule (known limitation).
    """


@dataclass(frozen=True, slots=True)
class PackCoverageVerdict:
    """Whether a window is covered and, when it isn't, from which day it would be."""

    window: DateRange
    covered: bool
    first_covered_date: date | None
    """The earliest day from which every day through the window's last day is covered.

    None when the window's last day isn't covered. Every uncovered day lies before it, so
    "this window starts earlier" stays true even when an earlier pack re-runs a day after the
    later version was published.
    """
    any_day_covered: bool


def pack_coverage_verdict(
    window: DateRange, from_version: int, versions: Mapping[date, int | None]
) -> PackCoverageVerdict:
    """Decide coverage of `window` from the per-day versions; a missing day isn't covered."""
    days = tuple(window.start + timedelta(days=offset) for offset in range(window.days))
    covered = {day: (versions.get(day) or 0) >= from_version for day in days}
    first: date | None = None
    for day in reversed(days):
        if not covered[day]:
            break
        first = day
    return PackCoverageVerdict(
        window=window,
        covered=all(covered.values()),
        first_covered_date=first,
        any_day_covered=any(covered.values()),
    )


def pack_coverage_text(subject: str, verdict: PackCoverageVerdict) -> str:
    """Build the absence text for an uncovered window (New customers LLD "Designed states").

    `subject` is plural, e.g. "Gateway products".
    """
    if verdict.covered:
        raise ValueError("a covered window has no coverage absence text.")
    tail = PACK_COVERAGE_RECOVERY
    if verdict.first_covered_date is not None:
        return (
            f"{subject} are measured from {day_label(verdict.first_covered_date)}. This window "
            f"starts earlier, so they aren't shown. {tail}"
        )
    if verdict.any_day_covered:
        return (
            f"{subject} aren't measured for every day of this window, so they aren't shown. {tail}"
        )
    return f"{subject} aren't measured yet. {tail}"


def _first_published_after(
    verdict: PackCoverageVerdict, first_published: date | None
) -> date | None:
    """Return the writer's first published day when the whole window lies before it."""
    if first_published is None or verdict.any_day_covered:
        return None
    return first_published if first_published > verdict.window.end else None


def contract_coverage_text(
    subject: str, verdict: PackCoverageVerdict, first_published: date | None = None
) -> str:
    """Build the refusal text for a window a late writer doesn't cover (plan D6).

    `subject` is the metric's label, e.g. "Reach (distinct users)". Reach is extracted per day,
    so the text names the first day it is measured from rather than a window: the window's
    covered tail, or the writer's first published day when the whole window is before it. Only a
    writer with no publication at all reads "yet"; an earlier day is never backfilled.
    """
    if verdict.covered:
        raise ValueError("a covered window has no coverage refusal text.")
    if verdict.first_covered_date is not None:
        first = day_label(verdict.first_covered_date)
        return (
            f"{subject} for this selection is measured from {first}. This window starts "
            f"earlier, so it isn't answered. Ask for days from {first} on."
        )
    later = _first_published_after(verdict, first_published)
    if later is not None:
        first = day_label(later)
        return (
            f"{subject} for this selection is measured from {first}. This window is earlier, "
            f"so it isn't answered. Ask for days from {first} on."
        )
    if verdict.any_day_covered:
        return (
            f"{subject} for this selection isn't measured on every day of this window, so it "
            "isn't answered."
        )
    if first_published is not None:
        return (
            f"{subject} for this selection isn't measured on any day of this window, so it "
            "isn't answered."
        )
    return f"{subject} for this selection isn't measured yet, so it isn't answered."


def split_writer_text(subject: str) -> str:
    """Build the refusal text for a figure whose operands no single row holds together.

    The text names the currency because the planner carries only `currency_code`
    (`PackCoverageRequest.carried`); carrying another column needs its own wording here.
    """
    return (
        f"{subject} isn't available for this selection. Its parts are measured by separate "
        "deliveries and only one of them carries the currency, so they can't be combined into "
        "one figure. Ask for each part on its own."
    )


@dataclass(frozen=True, slots=True)
class PackCoverageRequest:
    """One planned window, the versioned columns its metric reads, and the supplied evidence."""

    metric_name: str
    subject: str
    """The plural noun the refusal text names, e.g. the metric's label."""
    window: DateRange
    columns: tuple[PackVersionedColumn, ...]
    """The versioned columns the metric's definition reads; with none the request plans."""
    coverage: tuple[PackDayVersions, ...] | None
    filters: Mapping[str, str] = field(default_factory=dict[str, str])
    """The request's filters, which decide whether a publication-gated column applies."""
    carried: tuple[str, ...] = ()
    """The columns the plan groups its figure by beside the request's grain.

    Only `currency_code` today, which `split_writer_text` names as "the currency".
    """


def _refuse_split_writer(
    request: PackCoverageRequest, columns: tuple[PackVersionedColumn, ...]
) -> None:
    """Refuse a figure carrying a column that an applying gated column's writer doesn't write.

    Checked before coverage: no day's coverage makes it answerable, because the writer's rows
    group apart from the other operand's rows on the column it lacks.
    """
    for column in columns:
        missing = tuple(
            carried
            for carried in request.carried
            if column.publication_gated and carried not in column.writes
        )
        if missing:
            raise ValidationError(
                code=SPLIT_WRITER_REFUSED,
                description=split_writer_text(request.subject),
                context={
                    "metric": request.metric_name,
                    "contract": column.contract_id,
                    "column": column.column,
                    "carried_not_written": list(missing),
                },
            )


def require_pack_coverage(request: PackCoverageRequest) -> None:
    """Refuse a window not fully covered for any pack-versioned column the metric reads.

    Coverage evidence the runtime didn't supply is an invariant break, never a covered window.
    """
    metric_name, subject, window = request.metric_name, request.subject, request.window
    columns = tuple(column for column in request.columns if column.applies_to(request.filters))
    coverage = request.coverage
    if not columns:
        return
    _refuse_split_writer(request, columns)
    if coverage is None:
        raise InvariantError(
            PACK_COVERAGE_UNAVAILABLE,
            f"{metric_name} reads a column a later query pack or a later writer added, and this "
            "runtime supplied no pack coverage to check the window against.",
            context={"metric": metric_name},
        )
    by_contract = {item.contract_id: item for item in coverage}
    for column in columns:
        supplied = by_contract.get(column.contract_id)
        verdict = pack_coverage_verdict(
            window, column.from_version, {} if supplied is None else supplied.versions
        )
        if verdict.covered:
            continue
        gated = column.publication_gated
        first_published = None if supplied is None or not gated else supplied.first_published
        named = verdict.first_covered_date or _first_published_after(verdict, first_published)
        raise ValidationError(
            code=CONTRACT_COVERAGE_REFUSED if gated else PACK_COVERAGE_REFUSED,
            description=(
                contract_coverage_text(subject, verdict, first_published)
                if gated
                else pack_coverage_text(subject, verdict)
            ),
            context={
                "metric": metric_name,
                "contract": column.contract_id,
                "column": column.column,
                "present_from_pack_version": column.from_version,
                "first_covered_date": None if named is None else named.isoformat(),
                "any_day_covered": verdict.any_day_covered,
            },
        )
