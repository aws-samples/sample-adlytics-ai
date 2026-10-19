# SPDX-License-Identifier: MIT-0
"""Validate privacy-safe conversion deduplication structures inside AMC.

This is a source-validation probe, not a governed producer. It deliberately does not claim that
campaign-id ordering is first- or last-touch attribution. The model-reconciliation case uses that
ordering only after the sensitive-ID CTE and window syntax cases pass, proving that three credit
slots reconcile structurally without emitting conversion or campaign identifiers.

Run one case at a time, in order, after sourcing validation/.env.local:

    uv run python validation/probe_deduplicated_attribution.py \
      --instance-type SANDBOX --case conversion-id-cte \
      --from <YYYY-MM-DDT00:00:00> --to <YYYY-MM-DDT00:00:00>
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from amc_client import AdsApi, AmcError, credentials_from_environment, fetch_csv
from probe_amc_grain import SUPPRESSION, discover

FINDINGS = pathlib.Path(__file__).parent / "findings"
ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCTION_SQL_PATH = ROOT / "contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql"
TERMINAL_WAIT_SECONDS = 21_600
RESULT_BYTE_LIMIT = 2_000_000
PRODUCTION_RESULT_BYTE_LIMIT = 20_000_000
LINEAR_TOLERANCE = Decimal("0.000001")
REVENUE_TOLERANCE = Decimal("0.0001")
SENSITIVE_HEADERS = frozenset({"conversion_id", "user_id", "request_tag", "purchase_id"})
MODEL_KEYS = frozenset({"first_touch_v1", "last_touch_v1", "linear_v1"})

CONVERSION_ID_CTE_SQL = """
WITH conversion_groups AS (
  SELECT conversion_id, COUNT(*) AS relevance_rows
  FROM conversions_with_relevance
  WHERE event_category = 'purchase'
  GROUP BY conversion_id
)
SELECT COUNT(*) AS conversion_count,
       SUM(relevance_rows) AS relevance_row_count
FROM conversion_groups
"""

ROW_NUMBER_SQL = """
WITH campaigns AS (
  SELECT campaign_id_string
  FROM conversions_with_relevance
  WHERE event_category = 'purchase'
    AND campaign_id_string IS NOT NULL
  GROUP BY campaign_id_string
), ranked AS (
  SELECT campaign_id_string,
         ROW_NUMBER() OVER (ORDER BY campaign_id_string) AS structural_rank
  FROM campaigns
)
SELECT COUNT(*) AS ranked_campaign_count,
       MIN(structural_rank) AS minimum_rank,
       MAX(structural_rank) AS maximum_rank
FROM ranked
"""

MODEL_RECONCILIATION_SQL = """
WITH distinct_campaigns AS (
  SELECT conversion_id, campaign_id_string
  FROM conversions_with_relevance
  WHERE event_category = 'purchase'
    AND campaign_id_string IS NOT NULL
  GROUP BY conversion_id, campaign_id_string
), ranked AS (
  SELECT conversion_id,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id ORDER BY campaign_id_string
         ) AS first_rank,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id ORDER BY campaign_id_string DESC
         ) AS last_rank,
         COUNT(campaign_id_string) OVER (PARTITION BY conversion_id) AS campaign_count
  FROM distinct_campaigns
), per_conversion AS (
  SELECT conversion_id,
         SUM(CASE WHEN first_rank = 1 THEN 1.0 ELSE 0.0 END) AS first_credit,
         SUM(CASE WHEN last_rank = 1 THEN 1.0 ELSE 0.0 END) AS last_credit,
         SUM(1.0 / campaign_count) AS linear_credit
  FROM ranked
  GROUP BY conversion_id
)
SELECT COUNT(*) AS conversion_count,
       SUM(first_credit) AS first_credit_total,
       SUM(last_credit) AS last_credit_total,
       SUM(linear_credit) AS linear_credit_total
FROM per_conversion
"""

TEMPORAL_TOUCH_JOIN_SQL = """
WITH conversion_events AS (
  SELECT conversion_id, user_id, event_dt_utc AS conversion_time
  FROM conversions
  WHERE event_category = 'purchase'
    AND user_id IS NOT NULL
  GROUP BY conversion_id, user_id, event_dt_utc
), traffic_events AS (
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         impression_dt_utc AS touch_time, 'impression' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         click_dt_utc AS touch_time, 'click' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_clicks', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, ad_product_type AS channel_key,
         event_dt_utc AS touch_time,
         CASE WHEN clicks > 0 THEN 'click' ELSE 'impression' END AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('sponsored_ads_traffic', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
    AND ad_product_type IS NOT NULL
    AND (clicks > 0 OR impressions > 0)
), matched AS (
  SELECT c.conversion_id, t.campaign_id_string, t.channel_key,
         t.touch_time, t.touch_type
  FROM conversion_events c
  JOIN traffic_events t ON c.user_id = t.user_id
  WHERE SECONDS_BETWEEN(t.touch_time, c.conversion_time)
        BETWEEN 0 AND 14 * 24 * 60 * 60
), per_conversion AS (
  SELECT conversion_id, COUNT(campaign_id_string) AS touch_count
  FROM matched
  GROUP BY conversion_id
)
SELECT COUNT(*) AS matched_conversion_count,
       SUM(touch_count) AS matched_touch_count
FROM per_conversion
"""

TEMPORAL_MODEL_RECONCILIATION_SQL = """
WITH conversion_events AS (
  SELECT conversion_id, user_id, event_dt_utc AS conversion_time
  FROM conversions
  WHERE event_category = 'purchase'
    AND user_id IS NOT NULL
  GROUP BY conversion_id, user_id, event_dt_utc
), traffic_events AS (
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         impression_dt_utc AS touch_time, 'impression' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         click_dt_utc AS touch_time, 'click' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_clicks', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, ad_product_type AS channel_key,
         event_dt_utc AS touch_time,
         CASE WHEN clicks > 0 THEN 'click' ELSE 'impression' END AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('sponsored_ads_traffic', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
    AND ad_product_type IS NOT NULL
    AND (clicks > 0 OR impressions > 0)
), matched AS (
  SELECT c.conversion_id, t.campaign_id_string, t.channel_key,
         t.touch_time, t.touch_type
  FROM conversion_events c
  JOIN traffic_events t ON c.user_id = t.user_id
  WHERE SECONDS_BETWEEN(t.touch_time, c.conversion_time)
        BETWEEN 0 AND 14 * 24 * 60 * 60
), ranked AS (
  SELECT conversion_id,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_time ASC, touch_type ASC, campaign_id_string ASC
         ) AS first_rank,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_time DESC, touch_type ASC, campaign_id_string ASC
         ) AS last_rank,
         COUNT(campaign_id_string) OVER (PARTITION BY conversion_id) AS touch_count
  FROM matched
), per_conversion AS (
  SELECT conversion_id,
         SUM(CASE WHEN first_rank = 1 THEN 1.0 ELSE 0.0 END) AS first_credit,
         SUM(CASE WHEN last_rank = 1 THEN 1.0 ELSE 0.0 END) AS last_credit,
         SUM(1.0 / touch_count) AS linear_credit
  FROM ranked
  GROUP BY conversion_id
)
SELECT COUNT(*) AS matched_conversion_count,
       SUM(first_credit) AS first_credit_total,
       SUM(last_credit) AS last_credit_total,
       SUM(linear_credit) AS linear_credit_total
FROM per_conversion
"""

TEMPORAL_REVENUE_RECONCILIATION_SQL = """
WITH conversion_events AS (
  SELECT conversion_id, user_id, event_dt_utc AS conversion_time,
         total_product_sales AS conversion_revenue,
         purchase_currency AS currency_code
  FROM conversions
  WHERE event_category = 'purchase'
    AND user_id IS NOT NULL
    AND total_product_sales IS NOT NULL
    AND purchase_currency IS NOT NULL
  GROUP BY conversion_id, user_id, event_dt_utc,
           total_product_sales, purchase_currency
), traffic_events AS (
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         impression_dt_utc AS touch_time, 'impression' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, 'amazon_dsp' AS channel_key,
         click_dt_utc AS touch_time, 'click' AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_clicks', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
  UNION ALL
  SELECT user_id, campaign_id_string, ad_product_type AS channel_key,
         event_dt_utc AS touch_time,
         CASE WHEN clicks > 0 THEN 'click' ELSE 'impression' END AS touch_type
  FROM TABLE(EXTEND_TIME_WINDOW('sponsored_ads_traffic', 'P14D', 'P0D'))
  WHERE user_id IS NOT NULL AND campaign_id_string IS NOT NULL
    AND ad_product_type IS NOT NULL
    AND (clicks > 0 OR impressions > 0)
), matched AS (
  SELECT c.conversion_id, c.conversion_revenue, c.currency_code,
         t.campaign_id_string, t.touch_time, t.touch_type
  FROM conversion_events c
  JOIN traffic_events t ON c.user_id = t.user_id
  WHERE SECONDS_BETWEEN(t.touch_time, c.conversion_time)
        BETWEEN 0 AND 14 * 24 * 60 * 60
), ranked AS (
  SELECT conversion_id, conversion_revenue, currency_code,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_time ASC, touch_type ASC, campaign_id_string ASC
         ) AS first_rank,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_time DESC, touch_type ASC, campaign_id_string ASC
         ) AS last_rank,
         COUNT(campaign_id_string) OVER (PARTITION BY conversion_id) AS touch_count
  FROM matched
), per_conversion AS (
  SELECT conversion_id, currency_code,
         MAX(conversion_revenue) AS conversion_revenue,
         SUM(CASE WHEN first_rank = 1 THEN 1.0 ELSE 0.0 END) AS first_credit,
         SUM(CASE WHEN last_rank = 1 THEN 1.0 ELSE 0.0 END) AS last_credit,
         SUM(1.0 / touch_count) AS linear_credit
  FROM ranked
  GROUP BY conversion_id, currency_code
)
SELECT currency_code,
       COUNT(*) AS matched_conversion_count,
       SUM(conversion_revenue) AS portfolio_revenue,
       SUM(conversion_revenue * first_credit) AS first_revenue_total,
       SUM(conversion_revenue * last_credit) AS last_revenue_total,
       SUM(conversion_revenue * linear_credit) AS linear_revenue_total
FROM per_conversion
GROUP BY currency_code
"""

PRODUCTION_SHAPE_SQL = PRODUCTION_SQL_PATH.read_text()


@dataclass(frozen=True, slots=True)
class ProbeCase:
    """One narrow SQL admissibility question and its exact aggregate output."""

    name: str
    sql: str
    columns: tuple[str, ...]


CASES = {
    case.name: case
    for case in (
        ProbeCase(
            "conversion-id-cte",
            CONVERSION_ID_CTE_SQL,
            ("conversion_count", "relevance_row_count"),
        ),
        ProbeCase(
            "row-number",
            ROW_NUMBER_SQL,
            ("ranked_campaign_count", "minimum_rank", "maximum_rank"),
        ),
        ProbeCase(
            "model-reconciliation",
            MODEL_RECONCILIATION_SQL,
            (
                "conversion_count",
                "first_credit_total",
                "last_credit_total",
                "linear_credit_total",
            ),
        ),
        ProbeCase(
            "temporal-touch-join",
            TEMPORAL_TOUCH_JOIN_SQL,
            ("matched_conversion_count", "matched_touch_count"),
        ),
        ProbeCase(
            "temporal-model-reconciliation",
            TEMPORAL_MODEL_RECONCILIATION_SQL,
            (
                "matched_conversion_count",
                "first_credit_total",
                "last_credit_total",
                "linear_credit_total",
            ),
        ),
        ProbeCase(
            "temporal-revenue-reconciliation",
            TEMPORAL_REVENUE_RECONCILIATION_SQL,
            (
                "currency_code",
                "matched_conversion_count",
                "portfolio_revenue",
                "first_revenue_total",
                "last_revenue_total",
                "linear_revenue_total",
            ),
        ),
        ProbeCase(
            "production-shape",
            PRODUCTION_SHAPE_SQL,
            (
                "event_date_utc",
                "ad_product_type",
                "channel_group_key",
                "marketplace_name",
                "attribution_model_key",
                "matched_purchase_credit",
                "revenue_attributed_deduplicated_incl_halo",
                "purchase_currency",
                "time_window_start",
                "time_window_end",
            ),
        ),
    )
}


class ProbeApi(Protocol):
    """AMC execution surface used by recovery-first orchestration."""

    def executions_since(
        self, *, instance_id: str, min_creation_time: str
    ) -> list[dict[str, object]]:
        """List executions created since an exact AMC timestamp."""
        ...

    def execute_adhoc(
        self,
        *,
        instance_id: str,
        workflow_id: str,
        sql: str,
        window_start: str,
        window_end: str,
        require_synthetic_data: bool | None,
        disable_aggregation_controls: bool | None,
        suppression_columns: tuple[str, str, str],
    ) -> str:
        """Submit one exact ad hoc workflow."""
        ...

    def wait_for_execution(
        self, *, instance_id: str, execution_id: str, timeout_seconds: int
    ) -> dict[str, object]:
        """Wait for one execution to reach terminal state."""
        ...

    def download_urls(self, *, instance_id: str, execution_id: str) -> list[str]:
        """Return presigned downloads for one successful execution."""
        ...


def parser() -> argparse.ArgumentParser:
    """Build the one-case-at-a-time viability command line."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--instance-type", choices=("SANDBOX", "STANDARD"), required=True)
    result.add_argument("--case", choices=tuple(CASES), required=True)
    result.add_argument("--window-start", "--from", dest="window_start", required=True)
    result.add_argument("--window-end", "--to", dest="window_end", required=True)
    result.add_argument("--recover-since")
    return result


def normalized_sql(sql: str) -> str:
    """Render exactly what AdsApi submits and hashes into workflow identity."""
    return " ".join(sql.split())


def query_digest(sql: str) -> str:
    """Bind recovery to exact SQL bytes after submission normalization."""
    return f"sha256:{hashlib.sha256(normalized_sql(sql).encode()).hexdigest()}"


def _execution_id(execution: dict[str, object]) -> str:
    value = execution.get("workflowExecutionId")
    return value if isinstance(value, str) else ""


def _workflow_id(execution: dict[str, object]) -> str:
    direct = execution.get("workflowId")
    if isinstance(direct, str):
        return direct
    workflow = execution.get("workflow")
    if isinstance(workflow, dict):
        value = cast("dict[str, object]", workflow).get("workflowId")
        return value if isinstance(value, str) else ""
    return ""


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Exact execution identity and request needed for recovery or submission."""

    api: ProbeApi
    instance_id: str
    instance_type: str
    case: ProbeCase
    window_start: str
    window_end: str
    recover_since: str
    findings: pathlib.Path


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """One journaled or recovered server-side execution."""

    instance_id: str
    instance_type: str
    case: ProbeCase
    workflow_id: str
    execution_id: str
    digest: str
    stem: str
    findings: pathlib.Path


def _recover(request: ExecutionRequest, workflow_id: str) -> str | None:
    matches = [
        execution
        for execution in request.api.executions_since(
            instance_id=request.instance_id,
            min_creation_time=request.recover_since,
        )
        if _workflow_id(execution) == workflow_id and _execution_id(execution)
    ]
    if len(matches) > 1:
        ids = sorted(_execution_id(item) for item in matches)
        raise SystemExit(f"more than one execution matches {workflow_id}: {ids}")
    return None if not matches else _execution_id(matches[0])


def resolve_execution(request: ExecutionRequest) -> ExecutionPlan:
    """Adopt a journal/history execution before submitting an exact new workflow."""
    digest = query_digest(request.case.sql)
    request.findings.mkdir(parents=True, exist_ok=True)
    identity = digest.removeprefix("sha256:")[:10]
    start = request.window_start.partition("T")[0].replace("-", "")
    end = request.window_end.partition("T")[0].replace("-", "")
    workflow_id = f"probe-dedup-{request.case.name}-{identity}-{start}-{end}"
    stem = (
        f"deduplicated-attribution-{request.instance_type.lower()}-"
        f"{request.case.name}-{identity}-{start}-{end}"
    )
    journal = request.findings / f"{stem}.submission.json"
    execution_id: str | None = None
    source = "submission"
    if journal.exists():
        recorded = json.loads(journal.read_text())
        execution_id = str(recorded.get("execution_id", "")) or None
        source = "journal"
    if execution_id is None:
        execution_id = _recover(request, workflow_id)
        if execution_id is not None:
            source = "history"
    if execution_id is None:
        sandbox = request.instance_type == "SANDBOX"
        execution_id = request.api.execute_adhoc(
            instance_id=request.instance_id,
            workflow_id=workflow_id,
            sql=request.case.sql,
            window_start=request.window_start,
            window_end=request.window_end,
            require_synthetic_data=True if sandbox else None,
            disable_aggregation_controls=False if sandbox else None,
            suppression_columns=SUPPRESSION,
        )
        journal.write_text(
            json.dumps(
                {
                    "instance_id": request.instance_id,
                    "instance_type": request.instance_type,
                    "workflow_id": workflow_id,
                    "execution_id": execution_id,
                    "case": request.case.name,
                    "query_digest": digest,
                    "sql": normalized_sql(request.case.sql),
                    "window_start": request.window_start,
                    "window_end": request.window_end,
                    "require_synthetic_data": True if sandbox else None,
                    "disable_aggregation_controls": False if sandbox else None,
                    "suppression_columns": list(SUPPRESSION),
                },
                indent=2,
            )
        )
    print(f"  {request.case.name:24s} execution={execution_id} source={source}")
    return ExecutionPlan(
        request.instance_id,
        request.instance_type,
        request.case,
        workflow_id,
        execution_id,
        digest,
        stem,
        request.findings,
    )


def _decimal(row: dict[str, str], name: str) -> Decimal:
    value = row.get(name, "")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} is not a decimal: {value!r}") from error


def _suppressed(row: dict[str, str]) -> bool:
    value = row.get(SUPPRESSION[0], "").strip().lower()
    return value not in {"", "false", "0", "no"}


def _validate_revenue_rows(
    headers: tuple[str, ...], rows: list[dict[str, str]]
) -> dict[str, object]:
    if not rows:
        raise ValueError("revenue reconciliation returned no aggregate rows")
    if any(_suppressed(row) for row in rows):
        return {"outcome": "PRIVACY_BLOCKED", "header": list(headers)}
    evidence: list[dict[str, str]] = []
    positive = False
    for row in rows:
        currency = row.get("currency_code", "").strip()
        if not currency:
            raise ValueError("revenue reconciliation returned a bare monetary amount")
        values = {
            name: _decimal(row, name)
            for name in (
                "matched_conversion_count",
                "portfolio_revenue",
                "first_revenue_total",
                "last_revenue_total",
                "linear_revenue_total",
            )
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("revenue reconciliation values must be nonnegative")
        conversions = values["matched_conversion_count"]
        positive = positive or conversions > 0
        portfolio = values["portfolio_revenue"]
        for name in ("first_revenue_total", "last_revenue_total"):
            if abs(values[name] - portfolio) > REVENUE_TOLERANCE:
                raise ValueError(f"{name} does not reconcile to portfolio_revenue")
        if abs(values["linear_revenue_total"] - portfolio) > REVENUE_TOLERANCE:
            raise ValueError("linear_revenue_total does not reconcile to portfolio_revenue")
        evidence.append(
            {"currency_code": currency, **{name: str(value) for name, value in values.items()}}
        )
    return {
        "outcome": "VIABLE" if positive else "INCONCLUSIVE",
        "header": list(headers),
        "rows": evidence,
        "suppressed": False,
    }


def _validate_production_rows(
    headers: tuple[str, ...], rows: list[dict[str, str]]
) -> dict[str, object]:
    if not rows:
        raise ValueError("production shape returned no rows")
    if any(_suppressed(row) for row in rows):
        return {"outcome": "PRIVACY_BLOCKED", "header": list(headers)}
    required_dimensions = (
        "event_date_utc",
        "ad_product_type",
        "channel_group_key",
        "marketplace_name",
        "purchase_currency",
        "time_window_start",
        "time_window_end",
    )
    totals: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    for row in rows:
        missing = tuple(name for name in required_dimensions if not row.get(name, "").strip())
        if missing:
            raise ValueError(f"production row omits required dimensions {missing}")
        model = row.get("attribution_model_key", "").strip()
        if model not in MODEL_KEYS:
            raise ValueError(f"production row has unknown attribution model {model!r}")
        credit = _decimal(row, "matched_purchase_credit")
        revenue = _decimal(row, "revenue_attributed_deduplicated_incl_halo")
        if credit < 0 or revenue < 0:
            raise ValueError("production credit and revenue must be nonnegative")
        currency = row["purchase_currency"]
        by_model = totals.setdefault(currency, {})
        prior_credit, prior_revenue = by_model.get(model, (Decimal(0), Decimal(0)))
        by_model[model] = (prior_credit + credit, prior_revenue + revenue)
    for currency, by_model in totals.items():
        if set(by_model) != set(MODEL_KEYS):
            raise ValueError(f"{currency} does not contain every governed attribution model")
        reference_credit, reference_revenue = by_model["first_touch_v1"]
        for model, (credit, revenue) in by_model.items():
            if abs(credit - reference_credit) > LINEAR_TOLERANCE:
                raise ValueError(f"{currency}/{model} credit does not reconcile")
            allowed_rounding = max(reference_credit, credit) * REVENUE_TOLERANCE
            if abs(revenue - reference_revenue) > allowed_rounding:
                raise ValueError(f"{currency}/{model} revenue does not reconcile")
    return {
        "outcome": "VIABLE",
        "header": list(headers),
        "row_count": len(rows),
        "currencies": sorted(totals),
        "model_totals": {
            currency: {
                model: {"credit": str(values[0]), "revenue": str(values[1])}
                for model, values in sorted(by_model.items())
            }
            for currency, by_model in sorted(totals.items())
        },
        "suppressed": False,
    }


def validate_csv(  # noqa: PLR0912 -- validates each explicit probe contract
    case: ProbeCase, csv_text: str
) -> dict[str, object]:
    """Validate one aggregate row, exact headers, privacy state, and reconciliation."""
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = tuple(reader.fieldnames or ())
    suppression_start = len(case.columns)
    if (
        headers[:suppression_start] != case.columns
        or len(headers) != suppression_start + len(SUPPRESSION)
        or set(headers[suppression_start:]) != set(SUPPRESSION)
    ):
        raise ValueError(
            f"header is {headers!r}; expected analytical prefix {case.columns!r} "
            f"and suppression columns {SUPPRESSION!r}"
        )
    if SENSITIVE_HEADERS.intersection(headers):
        raise ValueError("sensitive identifiers appear in final output")
    rows = list(reader)
    if case.name == "temporal-revenue-reconciliation":
        return _validate_revenue_rows(headers, rows)
    if case.name == "production-shape":
        return _validate_production_rows(headers, rows)
    if len(rows) != 1:
        raise ValueError(f"expected one aggregate row, received {len(rows)}")
    row = rows[0]
    if _suppressed(row):
        return {"outcome": "PRIVACY_BLOCKED", "header": list(headers)}

    values = {name: _decimal(row, name) for name in case.columns}
    if any(value < 0 for value in values.values()):
        raise ValueError("aggregate values must be nonnegative")
    if case.name == "conversion-id-cte":
        conversions = values["conversion_count"]
        if values["relevance_row_count"] < conversions:
            raise ValueError("relevance rows are fewer than unique conversions")
        outcome = "INCONCLUSIVE" if conversions == 0 else "VIABLE"
    elif case.name == "row-number":
        count = values["ranked_campaign_count"]
        if count == 0:
            outcome = "INCONCLUSIVE"
        else:
            if values["minimum_rank"] != 1 or values["maximum_rank"] != count:
                raise ValueError("structural ranks do not cover one through campaign count")
            outcome = "VIABLE"
    elif case.name == "temporal-touch-join":
        conversions = values["matched_conversion_count"]
        if values["matched_touch_count"] < conversions:
            raise ValueError("matched touches are fewer than matched conversions")
        outcome = "INCONCLUSIVE" if conversions == 0 else "VIABLE"
    else:
        count_name = (
            "matched_conversion_count"
            if case.name == "temporal-model-reconciliation"
            else "conversion_count"
        )
        conversions = values[count_name]
        if conversions == 0:
            outcome = "INCONCLUSIVE"
        else:
            for name in ("first_credit_total", "last_credit_total"):
                if values[name] != conversions:
                    raise ValueError(f"{name} does not reconcile to {count_name}")
            if abs(values["linear_credit_total"] - conversions) > LINEAR_TOLERANCE:
                raise ValueError(f"linear_credit_total does not reconcile to {count_name}")
            outcome = "VIABLE"
    return {
        "outcome": outcome,
        "header": list(headers),
        "values": {name: str(value) for name, value in values.items()},
        "suppressed": False,
    }


def collect_terminal(api: ProbeApi, plan: ExecutionPlan) -> dict[str, object]:
    """Persist terminal and aggregate evidence for one exact execution."""
    terminal = api.wait_for_execution(
        instance_id=plan.instance_id,
        execution_id=plan.execution_id,
        timeout_seconds=TERMINAL_WAIT_SECONDS,
    )
    (plan.findings / f"{plan.stem}.terminal.json").write_text(json.dumps(terminal, indent=2))
    status = str(terminal.get("status", "")).upper()
    summary: dict[str, object] = {
        "instance_id": plan.instance_id,
        "instance_type": plan.instance_type,
        "case": plan.case.name,
        "workflow_id": plan.workflow_id,
        "execution_id": plan.execution_id,
        "query_digest": plan.digest,
        "terminal_status": status,
        "status_reason": str(terminal.get("statusReason") or terminal.get("statusMessage") or ""),
    }
    if status != "SUCCEEDED":
        summary["outcome"] = "NOT_VIABLE"
    else:
        urls = api.download_urls(instance_id=plan.instance_id, execution_id=plan.execution_id)
        if not urls:
            raise AmcError(200, "successful execution returned no download URL", plan.execution_id)
        byte_limit = (
            PRODUCTION_RESULT_BYTE_LIMIT
            if plan.case.name == "production-shape"
            else RESULT_BYTE_LIMIT
        )
        csv_text = fetch_csv(urls[0], max_bytes=byte_limit)
        (plan.findings / f"{plan.stem}.csv").write_text(csv_text)
        summary.update(validate_csv(plan.case, csv_text))
    (plan.findings / f"{plan.stem}.summary.json").write_text(json.dumps(summary, indent=2))
    print(f"  {plan.case.name:24s} status={status} outcome={summary['outcome']}")
    return summary


def main(argv: list[str] | None = None) -> int:
    """Recover, submit if absent, poll, and validate one narrow structural case."""
    args = parser().parse_args(argv)
    FINDINGS.mkdir(exist_ok=True)
    instance_type = str(args.instance_type)
    api = AdsApi(credentials_from_environment())
    instance_id = discover(api, instance_type)
    recover_since = str(
        args.recover_since
        or (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    )
    request = ExecutionRequest(
        api=api,
        instance_id=instance_id,
        instance_type=instance_type,
        case=CASES[str(args.case)],
        window_start=str(args.window_start),
        window_end=str(args.window_end),
        recover_since=recover_since,
        findings=FINDINGS,
    )
    print(f"instance: {instance_id} ({instance_type})")
    print(f"window  : {request.window_start} .. {request.window_end}")
    plan = resolve_execution(request)
    summary = collect_terminal(api, plan)
    return 0 if summary["outcome"] == "VIABLE" else 2 if summary["outcome"] == "INCONCLUSIVE" else 1


if __name__ == "__main__":
    sys.exit(main())
