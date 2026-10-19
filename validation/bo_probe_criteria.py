# SPDX-License-Identifier: MIT-0
"""P-BO-1 step A pass criteria and pack decisions, as design §8 states them. Pure.

Every function reads parsed query results and returns a `CriterionResult` whose `detail` holds
counts of rows checked and mismatched, never row values. "Exercised" follows AC-P.2: a criterion
whose condition never occurred is `inconclusive`, never `pass`.

The owning statement of each criterion is design §8 (P-BO-1); this module only implements it.
Criterion 10 reads only rows with `event_month > cohort_month` (plan decision 6, pending the
owner's confirmation): pack 14's `repeats` CTE filters `DATE_TRUNC('MONTH', event_date_utc) >
cohort_month`, and `cells` takes `MONTH_INDEX` only from the cohort month on, so the shipped
expression never reads a pre-cohort pair.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from bo_probe_sql import PACK5_DIAG_LABELS
from probe_amc_grain import SUPPRESSION

SENSITIVE_HEADERS = frozenset({"conversion_id", "user_id", "request_tag", "purchase_id"})
PASS, FAIL, INCONCLUSIVE, PENDING = "pass", "fail", "inconclusive", "pending"
LOW_BAR, ESCALATED_BAR = 2, 100
SIGN_MAGNITUDE = 3600
CREDIT_TOLERANCE = Decimal("0.000001")
MODELS = ("first_touch_v1", "last_touch_v1", "linear_v1")
YEAR_CROSSING_K = 6
MONTHS_PER_YEAR = 12

Row = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Parsed:
    """One query's rows plus the sanity defects found in its header and window."""

    rows: tuple[Row, ...]
    defects: tuple[str, ...]
    suppression_columns: bool

    @property
    def suppressed_count(self) -> int:
        """Rows AMC flagged as suppressed."""
        return sum(1 for row in self.rows if suppressed(row))


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """The outcome of one criterion; `detail` carries counts only."""

    id: str
    outcome: str
    exercised: bool
    detail: Mapping[str, object] = field(default_factory=dict[str, object])


def suppressed(row: Row) -> bool:
    """Whether AMC flagged the row as suppressed."""
    return row.get(SUPPRESSION[0], "").strip().lower() not in {"", "false", "0", "no"}


def number(row: Row, column: str) -> Decimal | None:
    """Read a numeric cell; empty is None."""
    value = row.get(column, "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{column} is not numeric") from error


def _instant(value: str) -> str:
    return value.strip().replace(" ", "T")[:19]


def parse_result(
    csv_text: str,
    columns: Sequence[str],
    *,
    window: tuple[str, str],
    controls_off: bool,
    escapechar: str | None = None,
) -> Parsed:
    """Parse one CSV and apply the sanity checks every query gets.

    Header: the analytic columns in order, then the three suppression columns. With controls
    off the suppression columns may be absent, which is recorded rather than refused.
    A backslash `escapechar` reads AMC's backslash-escaped quotes inside free text (observed in
    segment descriptions on STANDARD, 2026-10-09), which RFC 4180 parsing splits into extra fields.
    """
    reader = csv.DictReader(io.StringIO(csv_text), escapechar=escapechar)
    header = tuple(reader.fieldnames or ())
    rows = tuple(dict(row) for row in reader)
    defects: list[str] = []
    prefix = header[: len(columns)]
    rest = header[len(columns) :]
    has_suppression = set(rest) == set(SUPPRESSION) and len(rest) == len(SUPPRESSION)
    if tuple(prefix) != tuple(columns):
        defects.append("analytic header differs from the expected columns")
    if not has_suppression and (rest or not controls_off):
        defects.append("suppression columns missing or unexpected")
    if SENSITIVE_HEADERS.intersection(header):
        defects.append("sensitive identifier in the output header")
    for column, expected in zip(("time_window_start", "time_window_end"), window, strict=True):
        values = {_instant(row.get(column, "")) for row in rows if row.get(column, "").strip()}
        if values and values != {_instant(expected)}:
            defects.append(f"{column} differs from the requested bound")
    return Parsed(rows, tuple(defects), has_suppression)


def _result(
    criterion: str, *, exercised: bool, violations: int, **detail: object
) -> CriterionResult:
    if violations:
        return CriterionResult(criterion, FAIL, exercised, {"violations": violations, **detail})
    outcome = PASS if exercised else INCONCLUSIVE
    return CriterionResult(criterion, outcome, exercised, {"violations": 0, **detail})


NUMERIC_KEYS = frozenset({"months_since_acquisition", "k"})
"""Integer grain columns AMC may widen to a decimal: a `UNION ALL` branch carrying `NULL` in the
column turns `1` into `1.0000000000` (observed in Q8, 2026-10-08). Compared by value, not text."""


def _key_value(row: Row, key: str) -> str:
    value = row.get(key, "").strip()
    if key in NUMERIC_KEYS and value:
        try:
            return str(Decimal(value).normalize())
        except InvalidOperation:
            return value
    return value


def _key(row: Row, keys: Sequence[str]) -> tuple[str, ...]:
    return tuple(_key_value(row, key) for key in keys)


@dataclass(frozen=True, slots=True)
class Exactness:
    """Counts behind criteria 1, 2 and 4 for one controls-on / controls-off pair."""

    checked: int
    mismatched: int
    suppressed: int
    suppressed_with_dimensions: int
    absent: int
    duplicate_reference_keys: int


def exactness(
    on_rows: Iterable[Row], off_rows: Iterable[Row], keys: Sequence[str], *, counts: Sequence[str]
) -> Exactness:
    """Compare every unsuppressed controls-on row with the controls-off reference."""
    reference: dict[tuple[str, ...], Row] = {}
    duplicates = 0
    for row in off_rows:
        key = _key(row, keys)
        duplicates += key in reference
        reference[key] = row
    checked = mismatched = suppressed_rows = with_dims = absent = 0
    for row in on_rows:
        if suppressed(row):
            suppressed_rows += 1
            with_dims += any(row.get(key, "").strip() for key in keys)
            continue
        truth = reference.get(_key(row, keys))
        if truth is None:
            absent += 1
            continue
        checked += 1
        mismatched += any(number(row, name) != number(truth, name) for name in counts)
    return Exactness(checked, mismatched, suppressed_rows, with_dims, absent, duplicates)


def exactness_criteria(prefix: str, result: Exactness) -> tuple[CriterionResult, ...]:
    """Criteria 1, 2 and 4 (or their derived-half copies) from one comparison."""
    counts = {
        "checked": result.checked,
        "duplicate_reference_keys": result.duplicate_reference_keys,
    }
    return (
        _result(f"{prefix}1", exercised=result.checked > 0, violations=result.mismatched, **counts),
        _result(
            f"{prefix}2",
            exercised=result.suppressed > 0,
            violations=result.suppressed_with_dimensions,
            suppressed=result.suppressed,
        ),
        _result(f"{prefix}4", exercised=result.checked > 0, violations=result.absent, **counts),
    )


Q1_KEYS = ("campaign_id_string", "event_date_utc")
Q1R_KEYS = (*Q1_KEYS, "event_dt_hour_utc")
Q1_COUNTS = ("reach_users",)
PACK5_KEYS = ("exposure_source_set", "identity_space")
PACK5_COUNTS = (
    "shoppers_reached",
    "branded_search_shoppers",
    "detail_page_view_shoppers",
    "purchasers",
    "ntb_purchasers",
)
PACK6_KEYS = ("frequency_bucket", "identity_space")
PACK7_KEYS = ("ad_product_type", "frequency_bucket", "identity_space")
FREQUENCY_COUNTS = ("shoppers", "purchasers", "ntb_purchasers")
PACK14_KEYS = ("cohort_month", "months_since_acquisition", "acquisition_source_set")
PACK14_COUNTS = (
    "cohort_buyers_new_to_brand_12mo",
    "cohort_buyers_account_ids",
    "repeat_buyers",
    "repeat_buyers_account_ids",
    "first_repeat_buyers",
    "cumulative_repeat_buyers",
    "cumulative_repeat_buyers_account_ids",
)


def criterion_0(on: Parsed, off: Parsed, *, label: str = "0") -> CriterionResult:
    """Exercised: a suppressed controls-on row and a one-user reference cell."""
    one_user = sum(1 for row in off.rows if number(row, "reach_users") == 1)
    exercised = on.suppressed_count > 0 and one_user > 0
    return _result(
        label, exercised=exercised, violations=0, suppressed=on.suppressed_count, one_user=one_user
    )


def criterion_3(
    on: Parsed, off: Parsed, keys: Sequence[str], *, reach: str = "reach_users"
) -> CriterionResult:
    """Every reference cell is returned equal, or has fewer than two users (`reach` counts them)."""
    returned = {_key(row, keys): row for row in on.rows if not suppressed(row)}
    violations = 0
    for row in off.rows:
        match = returned.get(_key(row, keys))
        if match is not None and number(match, reach) == number(row, reach):
            continue
        users = number(row, reach)
        violations += users is None or users >= LOW_BAR
    return _result("3", exercised=bool(off.rows), violations=violations, reference=len(off.rows))


def criterion_2_restated(
    rows: Iterable[Row], *, source_keys: Sequence[str], derived_keys: Sequence[str], label: str
) -> CriterionResult:
    """Criterion 2 as restated by the owner, 2026-10-08 (AMC-V decision 3).

    Source-column keys are NULL on every suppressed row; CTE-derived labels may survive; and
    `rows_suppressed` is what identifies a suppressed row. A grain with no source-column key
    therefore reduces to identification by `rows_suppressed`. Exercised only by a suppressed row.
    """
    held = [row for row in rows if suppressed(row)]
    with_source = sum(any(row.get(key, "").strip() for key in source_keys) for row in held)
    surviving = sum(all(row.get(key, "").strip() for key in derived_keys) for row in held)
    return _result(
        label,
        exercised=bool(held),
        violations=with_source,
        suppressed=len(held),
        derived_labels_surviving=surviving,
        source_keys=len(source_keys),
    )


def withheld_labels_matched(on: Parsed, off: Iterable[Row], keys: Sequence[str]) -> dict[str, int]:
    """Count suppressed rows whose surviving labels name a reference cell the result withheld."""
    returned = {_key(row, keys) for row in on.rows if not suppressed(row)}
    withheld = {_key(row, keys) for row in off} - returned
    held = [row for row in on.rows if suppressed(row)]
    labelled = [row for row in held if all(row.get(key, "").strip() for key in keys)]
    return {
        "suppressed": len(held),
        "fully_labelled": len(labelled),
        "labels_name_a_withheld_cell": sum(_key(row, keys) in withheld for row in labelled),
        "withheld_reference_cells": len(withheld),
    }


def _gate(criteria: Sequence[CriterionResult], exercise: CriterionResult) -> str:
    if any(item.outcome == FAIL for item in criteria):
        return FAIL
    if exercise.outcome == PASS and all(item.outcome == PASS for item in criteria):
        return PASS
    return INCONCLUSIVE


def _not_run(criterion: str, *, mismatch: bool = False) -> CriterionResult:
    detail: dict[str, object] = {"not_run": True}
    if mismatch:
        detail["instance_mismatch"] = True
    return CriterionResult(criterion, INCONCLUSIVE, exercised=False, detail=detail)


def _unexercised(result: CriterionResult, exercise: CriterionResult) -> CriterionResult:
    """AC-P.2: with no suppressed row, matching unsuppressed rows isn't exactness under it."""
    if result.outcome != PASS or exercise.outcome == PASS:
        return result
    detail = {**result.detail, "unsuppressed_rows_matched": True}
    return CriterionResult(result.id, INCONCLUSIVE, exercised=False, detail=detail)


@dataclass(frozen=True, slots=True)
class Inputs:
    """The parsed results available to one evaluation; a query not run is None.

    `instances` names the instance (type and ID) each result came from. A controls-on result is
    compared with its controls-off reference only when both came from the same instance.
    """

    results: Mapping[str, Parsed | None]
    rejected: frozenset[str] = frozenset()
    instances: Mapping[str, str] = field(default_factory=dict[str, str])

    def get(self, name: str) -> Parsed | None:
        """Return one parsed result or None."""
        return self.results.get(name)

    def mismatched(self, on: str, off: str) -> bool:
        """Whether both results exist but came from different instances."""
        present = self.get(on) is not None and self.get(off) is not None
        return present and self.instances.get(on) != self.instances.get(off)

    def pair(self, on: str, off: str) -> tuple[Parsed, Parsed] | None:
        """Return a controls-on result and its reference, only when both ran on one instance."""
        first, second = self.get(on), self.get(off)
        if first is None or second is None or self.mismatched(on, off):
            return None
        return first, second


def source_half(inputs: Inputs) -> tuple[str, tuple[CriterionResult, ...]]:
    """Criteria 0, 1, 2, 4 and 3 for packs 1-3, 9 and 13, escalating to Q1r/Q2r."""
    base = inputs.pair("Q1", "Q2")
    if base is None:
        mismatch = inputs.mismatched("Q1", "Q2")
        missing = tuple(_not_run(c, mismatch=mismatch) for c in ("0", "1", "2", "4", "3"))
        return INCONCLUSIVE, missing
    q1, q2 = base
    zero = criterion_0(q1, q2)
    on, off, keys = q1, q2, Q1_KEYS
    rerun = inputs.pair("Q1r", "Q2r")
    if zero.outcome != PASS and rerun is not None:
        zero = criterion_0(*rerun)
        zero = CriterionResult(zero.id, zero.outcome, zero.exercised, {**zero.detail, "rerun": 1})
        on, off, keys = (*rerun, Q1R_KEYS)
    exact = exactness_criteria("", exactness(on.rows, off.rows, keys, counts=Q1_COUNTS))
    core = tuple(item if item.id == "2" else _unexercised(item, zero) for item in exact)
    three = criterion_3(on, off, keys)
    return _gate(core, zero), (zero, *core, three)


def _groups(parsed: Parsed, label_column: str, probe_labels: Iterable[str]) -> tuple[Row, ...]:
    labels = set(probe_labels)
    return tuple(row for row in parsed.rows if row.get(label_column, "").strip() not in labels)


def _rows_of_kind(parsed: Parsed, kind: str) -> tuple[Row, ...]:
    return tuple(row for row in parsed.rows if row.get("row_kind", "").strip() == kind)


def _suppressed_pack7_window(parsed: Parsed) -> int:
    # A suppressed row loses its LOW labels, row_kind included, so count every suppressed row.
    return parsed.suppressed_count


def derived_sub_gate_1(inputs: Inputs) -> tuple[str, tuple[CriterionResult, ...]]:
    """Criteria 0b, 5 and 5b (packs 5-8, 10-12)."""
    q5, q5r = inputs.get("Q5"), inputs.get("Q5r")
    suppressed_q5 = q5.suppressed_count if q5 is not None else 0
    suppressed_q5r = _suppressed_pack7_window(q5r) if q5r is not None else 0
    zero_b = _result(
        "0b",
        exercised=suppressed_q5 > 0 or suppressed_q5r > 0,
        violations=0,
        suppressed_q5=suppressed_q5,
        suppressed_q5r=suppressed_q5r,
    )
    fence = inputs.pair("Q3", "Q4")
    if fence is None:
        five: tuple[CriterionResult, ...] = (_not_run("5", mismatch=inputs.mismatched("Q3", "Q4")),)
    else:
        on, off = fence
        groups = _groups(off, "exposure_source_set", PACK5_DIAG_LABELS)
        five = _combine(
            "5",
            exactness_criteria("5.", exactness(on.rows, groups, PACK5_KEYS, counts=PACK5_COUNTS)),
            zero_b,
        )
    parts: list[CriterionResult] = []
    pack6 = inputs.pair("Q5", "Q6")
    if pack6 is not None:
        parts.extend(
            exactness_criteria(
                "5b.", exactness(pack6[0].rows, pack6[1].rows, PACK6_KEYS, counts=FREQUENCY_COUNTS)
            )
        )
    pack7 = inputs.pair("Q5r", "Q6r")
    if pack7 is not None:
        on7, off7 = pack7
        on_rows = (*_rows_of_kind(on7, "window"), *(r for r in on7.rows if suppressed(r)))
        parts.extend(
            exactness_criteria(
                "5b.r",
                exactness(
                    on_rows, _rows_of_kind(off7, "window"), PACK7_KEYS, counts=FREQUENCY_COUNTS
                ),
            )
        )
    mismatch = inputs.mismatched("Q5", "Q6") or inputs.mismatched("Q5r", "Q6r")
    five_b = _combine("5b", tuple(parts), zero_b) if parts else (_not_run("5b", mismatch=mismatch),)
    criteria = (*five, *five_b)
    return _gate(criteria, zero_b), (zero_b, *criteria)


def _combine(
    criterion: str, parts: tuple[CriterionResult, ...], exercise: CriterionResult
) -> tuple[CriterionResult, ...]:
    """Fold sub-results (1, 2, 4 of one comparison) into one criterion outcome.

    Design §8: without the exercising suppressed row (0b or 0c), the criterion is inconclusive,
    even when every unsuppressed row matched (AC-P.2).
    """
    core = [p for p in parts if not p.id.endswith("2")]
    matched = bool(core) and all(p.outcome == PASS for p in core)
    detail: dict[str, object] = {part.id: part.outcome for part in parts}
    if any(part.outcome == FAIL for part in parts):
        outcome = FAIL
    elif exercise.outcome != PASS:
        outcome = INCONCLUSIVE
        detail["unsuppressed_rows_matched"] = matched
    else:
        # criterion 2 is only exercised by a suppressed row; 1 and 4 carry exactness.
        outcome = PASS if matched else INCONCLUSIVE
    return (CriterionResult(criterion, outcome, outcome != INCONCLUSIVE, detail),)


combine = _combine
"""Public name for `_combine`, for the supplementary checks (`probe_bo_supplementary.py`)."""
group_rows = _groups
"""Public name for `_groups`: a reference's group rows, its probe-only rows removed."""
counted = _result
"""Public name for `_result`: pass, fail or inconclusive from an exercise flag and violations."""


def derived_sub_gate_2(inputs: Inputs) -> tuple[str, tuple[CriterionResult, ...]]:
    """Criteria 0c and 5c (packs 14-15)."""
    q7 = inputs.get("Q7")
    suppressed_q7 = q7.suppressed_count if q7 is not None else 0
    zero_c = _result("0c", exercised=suppressed_q7 > 0, violations=0, suppressed=suppressed_q7)
    cohort = inputs.pair("Q7", "Q8")
    if cohort is None:
        return INCONCLUSIVE, (zero_c, _not_run("5c", mismatch=inputs.mismatched("Q7", "Q8")))
    on, off = cohort
    groups = _groups(off, "acquisition_source_set", ("cohort_total",))
    five_c = _combine(
        "5c",
        exactness_criteria("5c.", exactness(on.rows, groups, PACK14_KEYS, counts=PACK14_COUNTS)),
        zero_c,
    )
    return _gate(five_c, zero_c), (zero_c, *five_c)


def criterion_11(parsed: Parsed | None, *, label: str) -> CriterionResult:
    """Derived-label bar: the max `distinct_user_count` over unsuppressed rows (2 or 100)."""
    if parsed is None:
        return _not_run(f"11.{label}")
    values = [number(row, SUPPRESSION[2]) for row in parsed.rows if not suppressed(row)]
    present = [value for value in values if value is not None]
    if not present:
        return CriterionResult(f"11.{label}", INCONCLUSIVE, exercised=False, detail={"bar": None})
    observed = max(present)
    if observed in {LOW_BAR, ESCALATED_BAR}:
        return CriterionResult(f"11.{label}", PASS, exercised=True, detail={"bar": int(observed)})
    return CriterionResult(f"11.{label}", INCONCLUSIVE, exercised=True, detail={"bar": None})


def bar_of(result: CriterionResult) -> int:
    """Return the bar criterion 7 reads: the recorded bar, or 100 when 11 is inconclusive."""
    bar = result.detail.get("bar")
    return bar if isinstance(bar, int) else ESCALATED_BAR


def criterion_7(
    inputs: Inputs,
    *,
    bar_pack5: int,
    bar_pack14: int,
    pack14_keys: Sequence[str] = PACK14_KEYS,
) -> CriterionResult:
    """Per-source check: large cells with 0-1 conditional users still return unsuppressed.

    The criterion is the conjunction of its pack 14 half (Q7/Q8) and its pack 5 half (Q3/Q4),
    each recorded separately. A withheld qualifying cell in a half that ran fails it. Otherwise a
    half that didn't run (or whose two sides came from different instances) leaves it
    inconclusive: a missing half is never read as zero cells.
    """
    halves = (
        ("pack14", "Q7", "Q8", pack14_keys, "acquisition_source_set", ("cohort_total",),
         "cohort_buyers_new_to_brand_12mo", "repeat_buyers", bar_pack14),
        ("pack5", "Q3", "Q4", PACK5_KEYS, "exposure_source_set", PACK5_DIAG_LABELS,
         "shoppers_reached", "purchasers", bar_pack5),
    )  # fmt: skip
    cells = violations = 0
    detail: dict[str, object] = {}
    missing = False
    for half, on_name, off_name, keys, label, probe_labels, base, conditional, bar in halves:
        pair = inputs.pair(on_name, off_name)
        if pair is None:
            missing = True
            mismatch = inputs.mismatched(on_name, off_name)
            detail[half] = "instance_mismatch" if mismatch else "not_run"
            continue
        on, off = pair
        returned = {_key(row, keys) for row in on.rows if not suppressed(row)}
        half_cells = half_violations = 0
        for row in _groups(off, label, probe_labels):
            size, cond = number(row, base), number(row, conditional)
            if size is None or size < bar or cond not in {0, 1}:
                continue
            half_cells += 1
            half_violations += _key(row, keys) not in returned
        detail[half] = {"cells": half_cells, "violations": half_violations, "bar": bar}
        cells += half_cells
        violations += half_violations
    if not violations and missing:
        detail.update({"violations": 0, "cells": cells, "not_run": True})
        return CriterionResult("7", INCONCLUSIVE, exercised=cells > 0, detail=detail)
    return _result("7", exercised=cells > 0, violations=violations, cells=cells, **detail)


def _by_label(parsed: Parsed, column: str = "exposure_source_set") -> dict[str, Decimal | None]:
    return {
        row.get(column, "").strip(): number(row, "shoppers_reached")
        for row in parsed.rows
        if row.get(column, "").strip() in PACK5_DIAG_LABELS
    }


def criterion_6(q4: Parsed | None) -> CriterionResult:
    """Check that the six group x space reach values sum to `fence_total` (AC-D1.2)."""
    if q4 is None:
        return _not_run("6")
    total = _by_label(q4).get("fence_total")
    groups = _groups(q4, "exposure_source_set", PACK5_DIAG_LABELS)
    summed = sum((number(row, "shoppers_reached") or Decimal(0)) for row in groups)
    exercised = total is not None and total >= 1
    return _result("6", exercised=exercised, violations=int(exercised and summed != total))


def criterion_6b(q6r: Parsed | None) -> CriterionResult:
    """Pack 7 credit never double counts; the three model sums agree."""
    if q6r is None:
        return _not_run("6b")
    totals = [number(row, "credited_purchases") for row in _rows_of_kind(q6r, "credit_total")]
    credit_total = totals[0] if totals else None
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for row in _rows_of_kind(q6r, "credit"):
        sums[row.get("attribution_model_key", "").strip()] += number(row, "credited_purchases") or 0
    exercised = credit_total is not None and credit_total >= 1
    if not exercised or credit_total is None:
        return _result("6b", exercised=False, violations=0)
    values = [sums.get(model, Decimal(0)) for model in MODELS]
    over = sum(value > credit_total + CREDIT_TOLERANCE for value in values)
    spread = max(values) - min(values) > CREDIT_TOLERANCE
    missing = sum(model not in sums for model in MODELS)
    return _result("6b", exercised=True, violations=over + int(spread) + missing)


def criterion_8(q4: Parsed | None, *, label: str = "8") -> CriterionResult:
    """AC-E1.5: pre-exposure purchases and post-window DSP touches are excluded."""
    if q4 is None:
        return _not_run(label)
    diag = _by_label(q4)

    def value(name: str) -> Decimal:
        return diag.get(name) or Decimal(0)

    violations = int(value("diag_pre_only_counted") != 0)
    violations += int(value("diag_late_dsp_in_dsp_groups") != 0)
    violations += int(value("diag_late_dsp_in_sponsored") != value("diag_late_dsp_shoppers"))
    exercised = value("diag_pre_only_shoppers") >= 1 and value("diag_late_dsp_shoppers") >= 1
    return _result(label, exercised=exercised, violations=violations)


CONVENTIONS = {
    (1, -1): "t2_minus_t1",
    (-1, 1): "t1_minus_t2",
    (1, 1): "absolute",
}
CROSS_CHECK = {"t2_minus_t1": (1, 0), "t1_minus_t2": (0, 1), "absolute": (0, 0)}


def criterion_8b(
    q0: Parsed | None, q4: Parsed | None, *, magnitude: int | None = SIGN_MAGNITUDE
) -> CriterionResult:
    """Read the `SECONDS_BETWEEN(t1, t2)` convention from Q0, cross-checked from Q4's sign rows.

    `magnitude` is 3600 for Q0's fixed timestamps; None for Q0alt, which reads it from the row.
    """
    if q0 is None or len(q0.rows) != 1:
        return CriterionResult("8b", INCONCLUSIVE, exercised=False, detail={"q0": "not run"})
    row = q0.rows[0]
    first, second, gap = (number(row, name) for name in ("earlier_later", "later_earlier", "gap"))
    if first is None or second is None or gap is None:
        return _result("8b", exercised=True, violations=1, reason="null reading")
    size = Decimal(magnitude) if magnitude is not None else abs(first)
    if size == 0:
        return _result("8b", exercised=True, violations=1, reason="zero magnitude")
    signs = (
        int(first / size) if abs(first) == size else 0,
        int(second / size) if abs(second) == size else 0,
    )
    convention = CONVENTIONS.get(signs)
    detail: dict[str, object] = {"convention": convention, "gap_ok": gap == size}
    if convention is None or gap != size:
        return CriterionResult("8b", FAIL, exercised=True, detail=detail)
    if q4 is not None:
        diag = _by_label(q4)
        multi = diag.get("diag_multi_hour_shoppers") or Decimal(0)
        if multi >= 1:
            sign = diag.get("diag_sign_check") or Decimal(0)
            mirror = diag.get("diag_sign_check_mirror") or Decimal(0)
            want = CROSS_CHECK[convention]
            agrees = (sign, mirror) == (want[0] * multi, want[1] * multi)
            detail["cross_check"] = "agree" if agrees else "disagree"
            if not agrees:
                return CriterionResult(
                    "8b", INCONCLUSIVE, exercised=True, detail={**detail, "defect": True}
                )
    return CriterionResult("8b", PASS, exercised=True, detail=detail)


def criterion_9(q8: Parsed | None) -> CriterionResult:
    """AC-B3.1: each cohort month's groups (repeated totals equal) sum to `cohort_total`."""
    if q8 is None:
        return _not_run("9")
    totals: dict[str, Decimal | None] = {}
    groups: dict[str, dict[str, set[Decimal | None]]] = defaultdict(lambda: defaultdict(set))
    for row in q8.rows:
        month = row.get("cohort_month", "").strip()
        label = row.get("acquisition_source_set", "").strip()
        value = number(row, "cohort_buyers_new_to_brand_12mo")
        if label == "cohort_total":
            totals[month] = value
        else:
            groups[month][label].add(value)
    violations = months = 0
    for month, by_group in groups.items():
        months += 1
        repeated = sum(len(values) != 1 for values in by_group.values())
        violations += repeated
        if repeated:
            continue
        summed = sum((next(iter(values)) or Decimal(0)) for values in by_group.values())
        violations += totals.get(month) != summed
    return _result("9", exercised=months > 0, violations=violations, months=months)


def _year_month(value: str) -> tuple[int, int]:
    text = value.strip()
    return int(text[0:4]), int(text[5:7])


def criterion_10(q9: Parsed | None, *, label: str = "10") -> CriterionResult:
    """`MONTH_INDEX`: k = 12 x year difference + month difference, on post-cohort rows."""
    if q9 is None:
        return _not_run(label)
    mismatched = checked = pre_cohort = 0
    crosses_year = False
    covered: set[int] = set()
    for row in q9.rows:
        if suppressed(row) or not row.get("cohort_month", "").strip():
            continue
        cy, cm = _year_month(row["cohort_month"])
        ey, em = _year_month(row["event_month"])
        expected = MONTHS_PER_YEAR * (ey - cy) + (em - cm)
        if expected <= 0:
            pre_cohort += 1
            continue
        checked += 1
        k = number(row, "k")
        mismatched += k is None or k != expected
        crosses_year = crosses_year or ey > cy
        covered.add(expected)
    exercised = crosses_year and set(range(1, YEAR_CROSSING_K + 1)) <= covered
    return _result(
        label, exercised=exercised, violations=mismatched, checked=checked, pre_cohort=pre_cohort
    )


@dataclass(frozen=True, slots=True)
class PackDecision:
    """Design §8's outcome for one pack."""

    policy: str
    expected_bar: str
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


SOURCE_PACKS = (1, 2, 3, 9, 13)
SUB_GATE_1_PACKS = (5, 6, 7, 8, 10, 11, 12)
PACK_5 = 5
PACK_7 = 7
WINDOW_PACKS = frozenset({5, 6, 7})
SUB_GATE_2_PACKS = (14, 15)
DISCARD, REJECT = "discard_suppressed_rows", "reject_delivery_at_narrowed_grains"


@dataclass(frozen=True, slots=True)
class Gates:
    """Gate outcomes and the criteria behind them, as `decide_packs` reads them."""

    source: str
    sub_gate_1: str
    sub_gate_2: str
    criteria: Mapping[str, CriterionResult]

    def outcome(self, criterion: str) -> str:
        """Return one criterion's outcome, inconclusive when absent."""
        result = self.criteria.get(criterion)
        return result.outcome if result is not None else INCONCLUSIVE


def evaluate_gates(inputs: Inputs) -> Gates:
    """Run every criterion in design order: 11 before 7, re-runs only where present."""
    source, source_criteria = source_half(inputs)
    gate_1, gate_1_criteria = derived_sub_gate_1(inputs)
    gate_2, gate_2_criteria = derived_sub_gate_2(inputs)
    pack7_on = inputs.get("Q5r") or inputs.get("Q5")
    bars = (
        criterion_11(inputs.get("Q3"), label="Q3"),
        criterion_11(pack7_on, label="Q5r" if inputs.get("Q5r") else "Q5"),
        criterion_11(inputs.get("Q7"), label="Q7"),
    )
    if inputs.get("Q5r") is not None:
        bars = (*bars, criterion_11(inputs.get("Q5"), label="Q5"))
    by_label = {result.id: result for result in bars}
    seven = criterion_7(
        inputs,
        bar_pack5=bar_of(by_label["11.Q3"]),
        bar_pack14=bar_of(by_label["11.Q7"]),
    )
    if "Q0" in inputs.rejected and inputs.get("Q0alt") is not None:
        sign = criterion_8b(inputs.get("Q0alt"), inputs.get("Q4"), magnitude=None)
    else:
        sign = criterion_8b(inputs.get("Q0"), inputs.get("Q4"))
    ac_e15 = criterion_8(inputs.get("Q4"))
    if ac_e15.outcome == INCONCLUSIVE and inputs.get("Q4m") is not None:
        ac_e15 = criterion_8(inputs.get("Q4m"))
    if ac_e15.outcome == INCONCLUSIVE and inputs.get("Q4m") is not None and sign.outcome == PASS:
        ac_e15 = CriterionResult("8", PENDING, exercised=False, detail=dict(ac_e15.detail))
    month = criterion_10(inputs.get("Q9"))
    if month.outcome == FAIL and inputs.get("Q9alt") is not None:
        alternative = criterion_10(inputs.get("Q9alt"), label="10")
        month = CriterionResult(
            "10",
            alternative.outcome,
            alternative.exercised,
            {**alternative.detail, "form": "calendar"},
        )
    elif month.outcome == PASS:
        month = CriterionResult(
            "10", PASS, exercised=True, detail={**month.detail, "form": "round"}
        )
    criteria = (
        *source_criteria,
        *gate_1_criteria,
        *gate_2_criteria,
        *bars,
        seven,
        criterion_6(inputs.get("Q4")),
        criterion_6b(inputs.get("Q6r")),
        ac_e15,
        sign,
        criterion_9(inputs.get("Q8")),
        month,
    )
    return Gates(source, gate_1, gate_2, {result.id: result for result in criteria})


def _bar(gates: Gates, label: str) -> str:
    result = gates.criteria.get(f"11.{label}")
    if result is None:
        return "first_live_run"
    bar = result.detail.get("bar")
    return str(bar) if isinstance(bar, int) else "100 (inconclusive -> escalation path)"


def decide_packs(gates: Gates, *, pack7_bar_from: str = "Q5") -> dict[int, PackDecision]:
    """Map gate and criterion outcomes onto every gated pack (design §8 outcomes table)."""
    decisions: dict[int, PackDecision] = {}
    sign_ok = gates.outcome("8b") == PASS
    ac_e15 = gates.outcome("8")
    seven_failed = gates.outcome("7") == FAIL
    for pack in SOURCE_PACKS:
        decisions[pack] = PackDecision(
            DISCARD if gates.source == PASS else REJECT, "first_live_run"
        )
    bars = {5: "Q3", 10: "Q3", 11: "Q3", 6: "Q5", 7: pack7_bar_from}
    for pack in SUB_GATE_1_PACKS:
        blockers: list[str] = []
        notes: list[str] = []
        if pack in WINDOW_PACKS:
            if not sign_ok:
                blockers.append("8b")
            # `pending` is design §8's end state, which `evaluate_gates` records only after the
            # Q4m calendar-month re-run stayed inconclusive with 8b passed. Anything short of
            # that keeps AC-E1.5 a blocker until the owner decides otherwise.
            if ac_e15 == PENDING and sign_ok:
                notes.append("AC-E1.5 pending: re-check on the first customer run")
            elif ac_e15 != PASS:
                blockers.append("8")
        if pack == PACK_5 and gates.outcome("6") != PASS:
            blockers.append("6")
        if pack == PACK_7 and gates.outcome("6b") != PASS:
            notes.append("credit branch ships as 'Not available per channel'")
        if seven_failed and pack in WINDOW_PACKS:
            notes.append("per-source alternative: pack 5 row_kind branches; coarse buckets 6-7")
        bar = _bar(gates, bars[pack]) if pack in bars else "first_live_run"
        policy = DISCARD if gates.sub_gate_1 == PASS else REJECT
        decisions[pack] = PackDecision(policy, bar, tuple(blockers), tuple(notes))
    for pack in SUB_GATE_2_PACKS:
        blockers = [c for c in ("8b", "9", "10") if gates.outcome(c) != PASS]
        notes = ["per-source alternative: quarterly cohorts"] if seven_failed else []
        policy = DISCARD if gates.sub_gate_2 == PASS else REJECT
        decisions[pack] = PackDecision(policy, _bar(gates, "Q7"), tuple(blockers), tuple(notes))
    return decisions
