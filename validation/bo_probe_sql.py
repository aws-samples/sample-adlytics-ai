# SPDX-License-Identifier: MIT-0
# ruff: noqa: S608 -- static probe SQL assembled from fixed parts; no value is interpolated
"""Candidate pack SQL and the P-BO-1 step A query renderer.

Pure: no I/O beyond reading the probe's own SQL parts under `validation/sql/bo/`.

Packs 5, 6, 7 and 14 don't exist under `contracts/amc/query-packs/` yet. The probe owns their
candidate text here, built from shared parts, so Q3 is exactly pack 5, Q4 is pack 5 plus
probe-only rows, and so on. A published conclusion cites each candidate's normalized SQL digest.

Two macros are expanded at render time and never reach AMC:

- `GAP_SECONDS(a, b)`: the sign-independent absolute gap of design §5.5. The design's `CASE`
  text negates with a unary minus, which AMC rejects ("Unsupported scalar operation type
  MINUS_PREFIX", Sandbox, 2026-10-08), so it is `ABS(SECONDS_BETWEEN(a, b))`: `ABS` is in the
  AMC SQL function reference [verified 2026-10-08], the condition design §5.5 sets for using it.
- `MONTH_INDEX(a, b)`: the month count between two month starts, in one of two forms.

SQL comments are `/* */` only: the client joins lines with spaces, so a `--` comment would
swallow the rest of the query.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass

SQL_DIR = pathlib.Path(__file__).parent / "sql" / "bo"
WINDOW_COLUMNS = ("time_window_start", "time_window_end")
COUNTRY = "dsp_campaign_country"
MARKETPLACE = "sponsored_marketplace_name"
AS_OF = "as_of_month"

MONTH_FORMS = ("round", "calendar")
"""`round` is design §6.6's fallback, `ROUND(GAP_SECONDS(a, b) / 2629800.0)`. `calendar` is
`12 * (YEAR(b) - YEAR(a)) + (MONTH(b) - MONTH(a))`, from the documented `YEAR` and `MONTH`
functions. No month-difference function is documented, so pack 14 carries `round` and Q9alt
tries `calendar`."""


def _part(name: str) -> str:
    return (SQL_DIR / name).read_text().strip()


def _split_args(text: str, start: int) -> tuple[list[str], int]:
    """Split the comma-separated arguments of a call whose '(' is at `start`."""
    depth, args, current = 0, [], []
    index = start
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
            if depth > 1:
                current.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                return args, index + 1
            current.append(char)
        elif char == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    raise ValueError("unbalanced parentheses in SQL macro")


def _expand(sql: str, macro: str, render: Callable[[str, str], str]) -> str:
    token = f"{macro}("
    out: list[str] = []
    position = 0
    while (found := sql.find(token, position)) != -1:
        out.append(sql[position:found])
        args, end = _split_args(sql, found + len(macro))
        if len(args) != 2:  # noqa: PLR2004 -- both macros take two arguments
            raise ValueError(f"{macro} takes two arguments, got {len(args)}")
        out.append(render(_expand(args[0], macro, render), _expand(args[1], macro, render)))
        position = end
    out.append(sql[position:])
    return "".join(out)


def gap_seconds(a: str, b: str) -> str:
    """Return the absolute gap under all three `SECONDS_BETWEEN` conventions (design §5.5)."""
    return f"ABS(SECONDS_BETWEEN({a}, {b}))"


def month_index(a: str, b: str, form: str) -> str:
    """Return the month count from month start `a` to month start `b`."""
    if form == "round":
        return f"ROUND(GAP_SECONDS({a}, {b}) / 2629800.0)"
    if form == "calendar":
        return f"(12 * (YEAR({b}) - YEAR({a})) + (MONTH({b}) - MONTH({a})))"
    raise ValueError(f"unknown month-index form {form!r}")


def expand(sql: str, month_form: str = "round") -> str:
    """Expand both macros into AMC SQL."""
    with_months = _expand(sql, "MONTH_INDEX", lambda a, b: month_index(a, b, month_form))
    return _expand(with_months, "GAP_SECONDS", gap_seconds)


def fence() -> str:
    """Return the exposure fence, byte-identical in packs 5, 6 and 7."""
    return _part("exposure_fence.sql")


def _pack5_ctes() -> str:
    return ",\n".join((fence(), _part("window_ctes.sql"), _part("pack05.ctes.sql")))


def _pack6_ctes() -> str:
    return ",\n".join((_pack5_ctes(), _part("pack06.ctes.sql")))


def _pack7_ctes() -> str:
    return ",\n".join((fence(), _part("pack07.ctes.sql")))


def _pack14_ctes() -> str:
    return ",\n".join((_part("pack14.ctes.sql"), _part("pack14.tail.sql")))


def _with(ctes: str, body: str) -> str:
    return f"WITH {ctes}\n{body}"


def render_pack(number: int, month_form: str = "round") -> str:
    """Return the candidate pack text, macros expanded."""
    raw = {
        5: lambda: _with(_pack5_ctes(), _part("pack05.select.sql")),
        6: lambda: _with(_pack6_ctes(), _part("pack06.select.sql")),
        7: lambda: _with(_pack7_ctes(), _part("pack07.select.sql")),
        14: lambda: _with(_pack14_ctes(), _part("pack14.select.sql")),
    }
    if number not in raw:
        raise ValueError(f"no candidate text for pack {number}")
    return expand(raw[number](), month_form)


PACK5_COLUMNS = (
    "exposure_source_set",
    "identity_space",
    "shoppers_reached",
    "impressions",
    "dsp_cost_raw",
    "sponsored_spend_raw",
    "dsp_currency",
    "dsp_currency_min",
    "sponsored_currency",
    "sponsored_currency_min",
    "branded_search_shoppers",
    "detail_page_view_shoppers",
    "purchasers",
    "ntb_purchasers",
    "purchases",
    "revenue_incl_halo",
    "purchase_currency",
    "purchase_currency_min",
    "marketplace_name",
    *WINDOW_COLUMNS,
)
PACK6_COLUMNS = (
    "frequency_bucket",
    "identity_space",
    "shoppers",
    "impressions",
    "dsp_cost_raw",
    "sponsored_spend_raw",
    "dsp_currency",
    "dsp_currency_min",
    "sponsored_currency",
    "sponsored_currency_min",
    "purchasers",
    "ntb_purchasers",
    "purchases",
    "revenue_incl_halo",
    "purchase_currency",
    "purchase_currency_min",
    *WINDOW_COLUMNS,
)
PACK7_COLUMNS = (
    "row_kind",
    "ad_product_type",
    "frequency_bucket",
    "identity_space",
    "attribution_model_key",
    *PACK6_COLUMNS[2:-2],
    "credited_purchases",
    "credited_revenue_incl_halo",
    *WINDOW_COLUMNS,
)
PACK14_COLUMNS = (
    "cohort_month",
    "months_since_acquisition",
    "acquisition_source_set",
    "cohort_buyers_new_to_brand_12mo",
    "cohort_buyers_account_ids",
    "repeat_buyers",
    "repeat_buyers_account_ids",
    "first_repeat_buyers",
    "cumulative_repeat_buyers",
    "cumulative_repeat_buyers_account_ids",
    "repeat_purchases",
    "repeat_revenue",
    "purchase_currency",
    "purchase_currency_min",
    *WINDOW_COLUMNS,
)

PACK5_DIAG_LABELS = (
    "fence_total",
    "diag_pre_only_shoppers",
    "diag_pre_only_counted",
    "diag_late_dsp_shoppers",
    "diag_late_dsp_in_dsp_groups",
    "diag_late_dsp_in_sponsored",
    "diag_multi_hour_shoppers",
    "diag_sign_check",
    "diag_sign_check_mirror",
)

_T0 = "CAST('2026-01-01 00:00:00' AS TIMESTAMP)"
_T1 = "CAST('2026-01-01 01:00:00' AS TIMESTAMP)"
_WINDOW = (
    "BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,\n"
    "       BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end"
)

Q0_SQL = f"""
SELECT MIN(SECONDS_BETWEEN({_T0}, {_T1})) AS earlier_later,
       MIN(SECONDS_BETWEEN({_T1}, {_T0})) AS later_earlier,
       MIN(GAP_SECONDS({_T1}, {_T0})) AS gap,
       COUNT(DISTINCT user_id) AS users,
       {_WINDOW}
FROM dsp_impressions
"""  # nosec B608 # A-SQL-001

Q0ALT_SQL = f"""
WITH bounds AS (
  SELECT MIN(impression_dt_hour_utc) AS earliest, MAX(impression_dt_hour_utc) AS latest,
         COUNT(DISTINCT user_id) AS users
  FROM dsp_impressions
)
SELECT MIN(SECONDS_BETWEEN(earliest, latest)) AS earlier_later,
       MIN(SECONDS_BETWEEN(latest, earliest)) AS later_earlier,
       MIN(GAP_SECONDS(latest, earliest)) AS gap,
       MIN(users) AS users,
       {_WINDOW}
FROM bounds
"""  # nosec B608 # A-SQL-001
Q0_COLUMNS = ("earlier_later", "later_earlier", "gap", "users", *WINDOW_COLUMNS)


def _q1(hourly: bool) -> str:
    hour = ", event_dt_hour_utc" if hourly else ""
    return f"""
SELECT campaign_id_string, event_date_utc{hour},
       COUNT(DISTINCT user_id) AS reach_users, SUM(impressions) AS impressions,
       {_WINDOW}
FROM sponsored_ads_traffic
WHERE ad_product_type IS NOT NULL
GROUP BY campaign_id_string, event_date_utc{hour}
"""  # nosec B608 # A-SQL-001


_Q1_COLUMNS = ("campaign_id_string", "event_date_utc", "reach_users", "impressions")
_PACK5_TAIL = (
    ", ".join(["NULL"] * 15)
    + ",\n  CUSTOM_PARAMETER('sponsored_marketplace_name'),"
    + " BUILT_IN_PARAMETER('TIME_WINDOW_START'), BUILT_IN_PARAMETER('TIME_WINDOW_END')"
)
_Q4_DIAG_CTES = """
diag_purchase_hours AS (
  SELECT g.user_id, g.first_exposure_hour, g.exposure_source_set,
         MAX(o.event_hour) AS last_purchase_hour
  FROM grouped g JOIN outcome_events o ON o.user_id = g.user_id
  WHERE o.event_category = 'purchase'
  GROUP BY g.user_id, g.first_exposure_hour, g.exposure_source_set
),
diag_dsp_offsets AS (
  SELECT t.user_id, MIN(GAP_SECONDS(f.first_exposure_hour, t.touch_hour)) AS first_dsp_offset
  FROM touches t JOIN firsts f ON f.user_id = t.user_id
  WHERE t.exposure_source = 'dsp'
  GROUP BY t.user_id
)"""
_Q4_BRANCHES = (
    ("fence_total", "f.user_id", "firsts f", ""),
    (
        "diag_pre_only_shoppers",
        "d.user_id",
        "diag_purchase_hours d",
        "WHERE d.last_purchase_hour < d.first_exposure_hour",
    ),
    (
        "diag_pre_only_counted",
        "d.user_id",
        "diag_purchase_hours d JOIN outcomes o ON o.user_id = d.user_id",
        "WHERE d.last_purchase_hour < d.first_exposure_hour AND o.purchased = 1",
    ),
    (
        "diag_late_dsp_shoppers",
        "x.user_id",
        "diag_dsp_offsets x",
        "WHERE x.first_dsp_offset >= 1209600",
    ),
    (
        "diag_late_dsp_in_dsp_groups",
        "x.user_id",
        "diag_dsp_offsets x JOIN grouped g ON g.user_id = x.user_id",
        "WHERE x.first_dsp_offset >= 1209600 AND g.exposure_source_set IN ('dsp', 'dsp+sponsored')",
    ),
    (
        "diag_late_dsp_in_sponsored",
        "x.user_id",
        "diag_dsp_offsets x JOIN grouped g ON g.user_id = x.user_id",
        "WHERE x.first_dsp_offset >= 1209600 AND g.exposure_source_set = 'sponsored'",
    ),
    (
        "diag_multi_hour_shoppers",
        "w.user_id",
        "w_touches w",
        "WHERE w.touch_hour > w.first_exposure_hour",
    ),
    (
        "diag_sign_check",
        "w.user_id",
        "w_touches w",
        "WHERE w.touch_hour > w.first_exposure_hour"
        " AND SECONDS_BETWEEN(w.touch_hour, w.first_exposure_hour) < 0",
    ),
    (
        "diag_sign_check_mirror",
        "w.user_id",
        "w_touches w",
        "WHERE w.touch_hour > w.first_exposure_hour"
        " AND SECONDS_BETWEEN(w.first_exposure_hour, w.touch_hour) < 0",
    ),
)


def _q4() -> str:
    branches = "\n".join(
        f"UNION ALL\nSELECT '{label}', 'all', COUNT(DISTINCT {counted}),\n  {_PACK5_TAIL}\n"  # nosec B608 # A-SQL-001
        f"FROM {source} {where}".rstrip()
        for label, counted, source, where in _Q4_BRANCHES
    )
    body = f"{_part('pack05.select.sql')}\n{branches}"
    return expand(_with(f"{_pack5_ctes()},{_Q4_DIAG_CTES}", body))


_PACK7_TAIL_NULLS = ", ".join(["NULL"] * 14)


def _q6r() -> str:
    credit_total = (
        f"UNION ALL\nSELECT 'credit_total', NULL, NULL, NULL, NULL,\n  {_PACK7_TAIL_NULLS},\n"  # nosec B608 # A-SQL-001
        "  COUNT(DISTINCT conversion_id), NULL,\n"
        "  BUILT_IN_PARAMETER('TIME_WINDOW_START'), BUILT_IN_PARAMETER('TIME_WINDOW_END')\n"
        "FROM matched"
    )
    return expand(_with(_pack7_ctes(), f"{_part('pack07.select.sql')}\n{credit_total}"))


def _q8() -> str:
    cohort_total = (
        "UNION ALL\nSELECT cohort_month, NULL, 'cohort_total', COUNT(DISTINCT user_id),\n  "  # nosec B608 # A-SQL-001
        + ", ".join(["NULL"] * 10)
        + ",\n  BUILT_IN_PARAMETER('TIME_WINDOW_START'), BUILT_IN_PARAMETER('TIME_WINDOW_END')\n"
        "FROM cohort\nGROUP BY cohort_month"
    )
    return expand(_with(_pack14_ctes(), f"{_part('pack14.select.sql')}\n{cohort_total}"))


_Q9_SELECT = f"""SELECT ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc) AS event_month,
       MONTH_INDEX(ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc)) AS k,
       COUNT(DISTINCT c.user_id) AS shoppers,
       {_WINDOW}
FROM conversions c JOIN cohort ch ON ch.user_id = c.user_id
WHERE c.event_category = 'purchase'
  AND c.marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
GROUP BY ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc),
         MONTH_INDEX(ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc))"""  # nosec B608 # A-SQL-001
Q9_COLUMNS = ("cohort_month", "event_month", "k", "shoppers", *WINDOW_COLUMNS)


def _q9(form: str) -> str:
    return expand(_with(_part("pack14.ctes.sql"), _Q9_SELECT), form)


@dataclass(frozen=True, slots=True)
class Query:
    """One step A query: its SQL, expected header, controls and window rule."""

    name: str
    sql: str
    columns: tuple[str, ...]
    controls_off: bool
    parameters: tuple[str, ...]
    window: str
    """`week`, `month` or `cohort` (design §8 window rules)."""


_EXPOSURE_PARAMETERS = (COUNTRY, MARKETPLACE)
_COHORT_PARAMETERS = (MARKETPLACE, AS_OF)


def _queries() -> dict[str, Query]:
    q1 = (*_Q1_COLUMNS, *WINDOW_COLUMNS)
    q1r = (*_Q1_COLUMNS[:2], "event_dt_hour_utc", *_Q1_COLUMNS[2:], *WINDOW_COLUMNS)
    exp, coh, mkt = _EXPOSURE_PARAMETERS, _COHORT_PARAMETERS, (MARKETPLACE,)
    on, off = "on", "off"
    rows: tuple[tuple[str, str, tuple[str, ...], str, tuple[str, ...], str], ...] = (
        ("Q0", expand(Q0_SQL), Q0_COLUMNS, on, (), "week"),
        ("Q0alt", expand(Q0ALT_SQL), Q0_COLUMNS, on, (), "week"),
        ("Q1", _q1(hourly=False), q1, on, (), "week"),
        ("Q2", _q1(hourly=False), q1, off, (), "week"),
        ("Q1r", _q1(hourly=True), q1r, on, (), "week"),
        ("Q2r", _q1(hourly=True), q1r, off, (), "week"),
        ("Q3", render_pack(5), PACK5_COLUMNS, on, exp, "week"),
        ("Q4", _q4(), PACK5_COLUMNS, off, exp, "week"),
        ("Q3m", render_pack(5), PACK5_COLUMNS, on, exp, "month"),
        ("Q4m", _q4(), PACK5_COLUMNS, off, exp, "month"),
        ("Q5", render_pack(6), PACK6_COLUMNS, on, exp, "week"),
        ("Q6", render_pack(6), PACK6_COLUMNS, off, exp, "week"),
        ("Q5r", render_pack(7), PACK7_COLUMNS, on, exp, "week"),
        ("Q6r", _q6r(), PACK7_COLUMNS, off, exp, "week"),
        ("Q7", render_pack(14), PACK14_COLUMNS, on, coh, "cohort"),
        ("Q8", _q8(), PACK14_COLUMNS, off, coh, "cohort"),
        ("Q9", _q9("round"), Q9_COLUMNS, off, mkt, "cohort"),
        ("Q9alt", _q9("calendar"), Q9_COLUMNS, off, mkt, "cohort"),
    )
    return {
        name: Query(name, sql, cols, controls_off=controls == off, parameters=params, window=window)
        for name, sql, cols, controls, params, window in rows
    }


QUERIES = _queries()


# --- Supplementary checks (owner decisions 2026-10-09) -----------------------------------------
#
# Neither text below is a governed pack's text. Each is derived from the candidate parts above by
# one asserted, mechanical edit, so a reader can see exactly how far it sits from the pack.

STRUCTURAL_LABEL = "structural check, not the exact pack text"
"""Carried by every result of the packs 5-7 fence variant. It never counts as the AC-E1.5 pass."""

COUNTRY_FILTER = "    AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')\n"


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"expected exactly one occurrence of {old.strip()!r}")
    return text.replace(old, new)


def structural_fence() -> str:
    """Return the exposure fence without its `campaign_country` filter, and nothing else changed.

    The Sandbox schema has no `dsp_impressions.campaign_country` (2026-10-08), so the exact fence
    can't compile there. Removing that one line is the only edit.
    """
    return _replace_once(fence() + "\n", COUNTRY_FILTER, "").strip()


def _structural_pack5_ctes() -> str:
    return ",\n".join((structural_fence(), _part("window_ctes.sql"), _part("pack05.ctes.sql")))


def _structural_q4() -> str:
    branches = "\n".join(
        f"UNION ALL\nSELECT '{label}', 'all', COUNT(DISTINCT {counted}),\n  {_PACK5_TAIL}\n"  # nosec B608 # A-SQL-001
        f"FROM {source} {where}".rstrip()
        for label, counted, source, where in _Q4_BRANCHES
    )
    body = f"{_part('pack05.select.sql')}\n{branches}"
    return expand(_with(f"{_structural_pack5_ctes()},{_Q4_DIAG_CTES}", body))


ACQUISITION_DATE = "acquisition_date"
PACK14_DAILY_COLUMNS = (*PACK14_COLUMNS[:3], ACQUISITION_DATE, *PACK14_COLUMNS[3:])


def render_pack14_daily() -> str:
    """Return pack 14 with one added grain key: each shopper's acquisition date.

    Monthly cohorts, `MONTH_INDEX`, the two data sources and every measure are pack 14's own. The
    only change is that `cohort` and `cells` carry `f.acq_date` and the final `SELECT` groups by
    it, so a cell holds the shoppers acquired on one day: sparse enough to suppress (S-6).
    """
    ctes = _replace_once(
        _part("pack14.ctes.sql"),
        "SELECT f.user_id, MAX(n.user_id_type) AS user_id_type,",
        f"SELECT f.user_id, MAX(n.user_id_type) AS user_id_type, f.acq_date AS {ACQUISITION_DATE},",
    )
    tail = _replace_once(
        _part("pack14.tail.sql"),
        "SELECT ch.user_id, ch.user_id_type, ch.cohort_month, ch.acquisition_source_set, m.k",
        f"SELECT ch.user_id, ch.user_id_type, ch.cohort_month, ch.acquisition_source_set, "
        f"ch.{ACQUISITION_DATE}, m.k",
    )
    select = _replace_once(
        _part("pack14.select.sql"),
        "x.k AS months_since_acquisition, x.acquisition_source_set,",
        f"x.k AS months_since_acquisition, x.acquisition_source_set, x.{ACQUISITION_DATE},",
    )
    select = _replace_once(
        select,
        "GROUP BY x.cohort_month, x.k, x.acquisition_source_set",
        f"GROUP BY x.cohort_month, x.k, x.acquisition_source_set, x.{ACQUISITION_DATE}",
    )
    return expand(_with(",\n".join((ctes, tail)), select))


def _supplementary() -> dict[str, Query]:
    q3s, q4s, daily = (
        expand(_with(_structural_pack5_ctes(), _part("pack05.select.sql"))),
        _structural_q4(),
        render_pack14_daily(),
    )
    mkt, coh = (MARKETPLACE,), _COHORT_PARAMETERS
    rows: tuple[tuple[str, str, tuple[str, ...], bool, tuple[str, ...], str], ...] = (
        ("Q3s", q3s, PACK5_COLUMNS, False, mkt, "week"),
        ("Q4s", q4s, PACK5_COLUMNS, True, mkt, "week"),
        ("Q4ms", q4s, PACK5_COLUMNS, True, mkt, "month"),
        ("Q7d", daily, PACK14_DAILY_COLUMNS, False, coh, "cohort"),
        ("Q8d", daily, PACK14_DAILY_COLUMNS, True, coh, "cohort"),
    )
    return {
        name: Query(name, sql, cols, controls_off=off, parameters=params, window=window)
        for name, sql, cols, off, params, window in rows
    }


SUPPLEMENTARY = _supplementary()
"""Q3s/Q4s (Q4ms: Q4s over a calendar month) are the packs 5-7 fence structural check. Q7d/Q8d
are pack 14 at acquisition-date grain, the same SQL with controls on and off."""
