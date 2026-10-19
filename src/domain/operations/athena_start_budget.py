# SPDX-License-Identifier: MIT-0
"""The static Athena start budget rule (plan S1, F2), the Python twin of the TypeScript one.

Each batch caller's maximum concurrent Athena executions is reserved concurrency x in-process
start cap. `validate_athena_start_budget` checks the seven closed rows and the two limits in the
closed refusal order, the first failure winning:

    UNKNOWN_ROW, MISSING_ROW, DUPLICATE_ROW, NON_INTEGER, NEGATIVE_RESERVED_CONCURRENCY,
    UNRESERVED, NON_POSITIVE_CAP, BELOW_CERTIFIED_QUERY_CAP, NON_POSITIVE_CHAT_RESERVE,
    SUM_OVER_BUDGET, BUDGET_OVER_CHAT_HEADROOM

A reserved concurrency of 0 is a quiesced row (zero starts); `None` is unreserved, i.e. unbounded,
and is refused. The integer test is `type(v) is int` or an integral float, exactly TypeScript's
`Number.isInteger`; a boolean is never an integer. `infra/lib/config/athena-start-budget.ts`'s
`validateAthenaStartBudgetRequest` is the same rule; both suites run the shared case table
`tests/fixtures/athena-start-budget/cases.json`. Pure: no clock, no I/O, no AWS.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ATHENA_START_BUDGET_ROW_IDS",
    "AthenaStartBudgetRequest",
    "AthenaStartBudgetRow",
    "BudgetAccepted",
    "BudgetRefusalReason",
    "BudgetRefused",
    "validate_athena_start_budget",
]

ATHENA_START_BUDGET_ROW_IDS: Final = (
    "baker",
    "table_writer",
    "inline_ingestion",
    "provider_attempt_reconciler",
    "ingest_operator",
    "reprocess_operator",
    "insight_precompute",
)


class BudgetRefusalReason(StrEnum):
    """The closed refusal reasons, declared in the order they are checked."""

    UNKNOWN_ROW = "UNKNOWN_ROW"
    MISSING_ROW = "MISSING_ROW"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    NON_INTEGER = "NON_INTEGER"
    NEGATIVE_RESERVED_CONCURRENCY = "NEGATIVE_RESERVED_CONCURRENCY"
    UNRESERVED = "UNRESERVED"
    NON_POSITIVE_CAP = "NON_POSITIVE_CAP"
    BELOW_CERTIFIED_QUERY_CAP = "BELOW_CERTIFIED_QUERY_CAP"
    NON_POSITIVE_CHAT_RESERVE = "NON_POSITIVE_CHAT_RESERVE"
    SUM_OVER_BUDGET = "SUM_OVER_BUDGET"
    BUDGET_OVER_CHAT_HEADROOM = "BUDGET_OVER_CHAT_HEADROOM"


type Scalar = int | float | bool | str | None


@dataclass(frozen=True, slots=True)
class AthenaStartBudgetRow:
    """One budget row: `reserved_concurrency` None means unreserved; 0 means quiesced."""

    row_id: str
    reserved_concurrency: int | float | bool | None
    start_cap: int | float | bool


@dataclass(frozen=True, slots=True)
class AthenaStartBudgetRequest:
    """Explicit values to evaluate; the rule itself, not this type, refuses bad ones."""

    rows: tuple[AthenaStartBudgetRow, ...]
    batch_athena_budget: int | float | bool
    chat_reserve_active_dml: int | float | bool
    active_dml_quota: int | float | bool
    certified_query_cap: int | float | bool


@dataclass(frozen=True, slots=True)
class BudgetAccepted:
    """The rule holds; `total` is the sum of reserved concurrency x start cap."""

    total: int


@dataclass(frozen=True, slots=True)
class BudgetRefused:
    """The first violated term, with what was observed and the limit it broke."""

    reason: BudgetRefusalReason
    observed: Scalar
    limit: Scalar


def _integral(value: object) -> bool:
    """Return the shared integer test: an int or an integral float, never a boolean."""
    return type(value) is int or (type(value) is float and value.is_integer())


def validate_athena_start_budget(  # noqa: PLR0911, PLR0912 -- one ordered rule
    request: AthenaStartBudgetRequest,
) -> BudgetAccepted | BudgetRefused:
    """Return the S1 verdict for explicit rows and limits."""
    rows = request.rows
    unknown = next((row for row in rows if row.row_id not in ATHENA_START_BUDGET_ROW_IDS), None)
    if unknown is not None:
        return BudgetRefused(BudgetRefusalReason.UNKNOWN_ROW, unknown.row_id, None)
    present = [row.row_id for row in rows]
    missing = next(
        (row_id for row_id in ATHENA_START_BUDGET_ROW_IDS if row_id not in present), None
    )
    if missing is not None:
        return BudgetRefused(BudgetRefusalReason.MISSING_ROW, missing, None)
    duplicate = next(
        (row_id for index, row_id in enumerate(present) if row_id in present[:index]), None
    )
    if duplicate is not None:
        return BudgetRefused(BudgetRefusalReason.DUPLICATE_ROW, duplicate, None)
    for row in rows:
        reserved = row.reserved_concurrency
        if (reserved is not None and not _integral(reserved)) or not _integral(row.start_cap):
            return BudgetRefused(BudgetRefusalReason.NON_INTEGER, row.row_id, None)
    scalars = (
        ("batch_athena_budget", request.batch_athena_budget),
        ("chat_reserve_active_dml", request.chat_reserve_active_dml),
        ("active_dml_quota", request.active_dml_quota),
        ("certified_query_cap", request.certified_query_cap),
    )
    for name, value in scalars:
        if not _integral(value):
            return BudgetRefused(BudgetRefusalReason.NON_INTEGER, name, None)
    for row in rows:
        if row.reserved_concurrency is not None and row.reserved_concurrency < 0:
            return BudgetRefused(
                BudgetRefusalReason.NEGATIVE_RESERVED_CONCURRENCY, row.reserved_concurrency, 0
            )
    unreserved = next((row for row in rows if row.reserved_concurrency is None), None)
    if unreserved is not None:
        return BudgetRefused(BudgetRefusalReason.UNRESERVED, unreserved.row_id, None)
    uncapped = next((row for row in rows if row.start_cap < 1), None)
    if uncapped is not None:
        return BudgetRefused(BudgetRefusalReason.NON_POSITIVE_CAP, uncapped.start_cap, 1)
    baker = next(row for row in rows if row.row_id == "baker")
    if baker.start_cap < request.certified_query_cap:
        return BudgetRefused(
            BudgetRefusalReason.BELOW_CERTIFIED_QUERY_CAP,
            baker.start_cap,
            request.certified_query_cap,
        )
    if request.chat_reserve_active_dml < 1:
        return BudgetRefused(
            BudgetRefusalReason.NON_POSITIVE_CHAT_RESERVE, request.chat_reserve_active_dml, 1
        )
    total = int(sum((row.reserved_concurrency or 0) * row.start_cap for row in rows))
    if total > request.batch_athena_budget:
        return BudgetRefused(
            BudgetRefusalReason.SUM_OVER_BUDGET, total, int(request.batch_athena_budget)
        )
    headroom = int(request.active_dml_quota - request.chat_reserve_active_dml)
    if request.batch_athena_budget > headroom:
        return BudgetRefused(
            BudgetRefusalReason.BUDGET_OVER_CHAT_HEADROOM,
            int(request.batch_athena_budget),
            headroom,
        )
    return BudgetAccepted(total)
