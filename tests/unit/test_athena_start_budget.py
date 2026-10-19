# SPDX-License-Identifier: MIT-0
"""The static Athena start budget rule over the case table Jest also runs (plan S1, 3.5, F2).

Every case gives its expected verdict through `validate_athena_start_budget`, and every accepted
case runs beside its refused control, differing in one scalar, in the same test. The TypeScript
suite `infra/test/athena-start-budget.test.ts` runs the same file through its own validator; the
mirror proof is that both pass on it with no per-language exception.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from domain.operations.athena_start_budget import (
    ATHENA_START_BUDGET_ROW_IDS,
    AthenaStartBudgetRequest,
    AthenaStartBudgetRow,
    BudgetAccepted,
    BudgetRefusalReason,
    BudgetRefused,
    validate_athena_start_budget,
)

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
CASES: tuple[dict[str, Any], ...] = tuple(
    json.loads((ROOT / "tests/fixtures/athena-start-budget/cases.json").read_text())
)
BY_ID = {case["id"]: case for case in CASES}
ACCEPTED = tuple(case for case in CASES if case["expect"]["verdict"] == "accepted")
_TOP_LEVEL = (
    "batch_athena_budget",
    "chat_reserve_active_dml",
    "active_dml_quota",
    "certified_query_cap",
)


def _request(case: dict[str, Any]) -> AthenaStartBudgetRequest:
    return AthenaStartBudgetRequest(
        rows=tuple(
            AthenaStartBudgetRow(row["row_id"], row["reserved_concurrency"], row["start_cap"])
            for row in case["rows"]
        ),
        batch_athena_budget=case["batch_athena_budget"],
        chat_reserve_active_dml=case["chat_reserve_active_dml"],
        active_dml_quota=case["active_dml_quota"],
        certified_query_cap=case["certified_query_cap"],
    )


def _scalar_differences(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Return every scalar two cases disagree on: a row field or a top-level limit."""
    if len(left["rows"]) != len(right["rows"]):
        return ["rows.length"]
    differences = [
        f"rows[{index}].{name}"
        for index, (mine, theirs) in enumerate(zip(left["rows"], right["rows"], strict=True))
        for name in ("row_id", "reserved_concurrency", "start_cap")
        if not _same(mine[name], theirs[name])
    ]
    return differences + [name for name in _TOP_LEVEL if not _same(left[name], right[name])]


def _same(left: object, right: object) -> bool:
    """Compare as TypeScript's `===` does on parsed JSON: True never equals 1."""
    return left == right and isinstance(left, bool) == isinstance(right, bool)


def _expect_verdict(case: dict[str, Any]) -> None:
    verdict = validate_athena_start_budget(_request(case))
    expected = case["expect"]
    if expected["verdict"] == "accepted":
        assert verdict == BudgetAccepted(expected["total"]), case["id"]
    else:
        assert isinstance(verdict, BudgetRefused), case["id"]
        assert verdict.reason is BudgetRefusalReason(expected["reason"]), case["id"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_every_shared_case_gives_its_expected_verdict(case: dict[str, Any]) -> None:
    _expect_verdict(case)


@pytest.mark.parametrize("case", ACCEPTED, ids=[case["id"] for case in ACCEPTED])
def test_every_accepted_case_runs_beside_one_refused_control_one_scalar_apart(
    case: dict[str, Any],
) -> None:
    control = BY_ID[case["control"]]
    assert control["expect"]["verdict"] == "refused"
    assert "control" not in control
    assert len(_scalar_differences(case, control)) == 1
    # Both halves of the pair run here, through this suite's own validator.
    _expect_verdict(case)
    _expect_verdict(control)


def test_the_table_carries_the_required_cases_and_every_closed_reason() -> None:
    required = {
        "defaults-active",
        "baker-drift-3",
        "defaults-quiesced",
        "load-unreserved",
        "load-negative",
        "raised-q-16",
        "raised-q-16-budget-52",
        "operator-row-two-processes",
        "budget-over-headroom",
        "chat-reserve-zero",
        "start-cap-integral-float",
        "start-cap-fraction",
    }
    assert required <= set(BY_ID)
    reasons = {case["expect"].get("reason") for case in CASES}
    assert {reason.value for reason in BudgetRefusalReason} <= reasons
    assert len(ACCEPTED) >= 4


def test_control_the_pairing_check_counts_two_scalars_apart_as_two() -> None:
    base = BY_ID["defaults-active"]
    two_apart = {**base, "batch_athena_budget": 40, "chat_reserve_active_dml": 19}
    assert len(_scalar_differences(base, two_apart)) == 2
    assert _scalar_differences(base, base) == []


def test_refusals_report_the_violated_term_with_observed_and_limit() -> None:
    def verdict(case_id: str) -> BudgetAccepted | BudgetRefused:
        return validate_athena_start_budget(_request(BY_ID[case_id]))

    assert verdict("baker-drift-3") == BudgetRefused(BudgetRefusalReason.SUM_OVER_BUDGET, 51, 41)
    assert verdict("operator-row-two-processes") == BudgetRefused(
        BudgetRefusalReason.SUM_OVER_BUDGET, 42, 41
    )
    assert verdict("budget-over-headroom") == BudgetRefused(
        BudgetRefusalReason.BUDGET_OVER_CHAT_HEADROOM, 181, 180
    )
    assert verdict("load-unreserved") == BudgetRefused(
        BudgetRefusalReason.UNRESERVED, "inline_ingestion", None
    )
    # Paired positive control: the defaults themselves are accepted at exactly the budget.
    assert verdict("defaults-active") == BudgetAccepted(41)


def test_the_row_ids_and_refusal_order_equal_the_typescript_rule() -> None:
    source = (ROOT / "infra/lib/config/athena-start-budget.ts").read_text()

    def listed(name: str) -> list[str]:
        match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.DOTALL)
        assert match is not None, name
        return re.findall(r"'([A-Za-z_]+)'", match.group(1))

    assert listed("ATHENA_START_BUDGET_ROW_IDS") == list(ATHENA_START_BUDGET_ROW_IDS)
    assert listed("ATHENA_START_BUDGET_REFUSALS") == [r.value for r in BudgetRefusalReason]
    # Control: the extraction itself fails on a list the source does not declare.
    assert re.search(r"export const NOT_DECLARED = \[", source) is None
