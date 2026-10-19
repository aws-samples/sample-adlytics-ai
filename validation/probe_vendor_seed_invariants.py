# SPDX-License-Identifier: MIT-0
"""Attest the served fictional Vendor retail state against the declared shape (design §2.5).

Read-only: CURRENT pointers from DynamoDB and publication-fenced Athena projections over the
span and the visible products only. Every invariant is a pure function evaluated in Python,
and no check passes on absence: each reports its denominator and fails below expectation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

_VALIDATION: Final = Path(__file__).resolve().parent
if str(_VALIDATION) not in sys.path:  # pragma: no cover - script entry
    sys.path.insert(0, str(_VALIDATION))

from probe_amc_current_invariants import (  # noqa: E402
    CheckReport,
    ProbeError,
    ProbeRequest,
    QueryExecutionRequest,
    QueryTarget,
    catalog_span,
    decimal_value,
    fences,
    publication_clause,
    publication_scope,
    query,
    row_day,
)

from adapters.jobs.fixture_generation.catalog import (  # noqa: E402
    SyntheticEntityCatalog,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.shape import RetailShape  # noqa: E402
from adapters.jobs.reprocess_semantic_v2.resources import (  # noqa: E402
    resolve_named_semantic_resources,
)
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications  # noqa: E402
from domain.semantics.publication import PartitionIdentity  # noqa: E402
from domain.semantics.time import MeasurementBucket  # noqa: E402
from domain.shared_kernel.values import DateRange  # noqa: E402

_ROOT: Final = _VALIDATION.parent
_FIXTURES: Final = _ROOT / "tests" / "fixtures" / "generated"
_SALES_CONTRACT: Final = "vendor-sales-by-asin"
_TRAFFIC_CONTRACT: Final = "vendor-traffic-by-asin"
_FIRST_REPORT_DAYS: Final = 2
"""The last two days carry only their first report (no restating report has fallen due)."""


@dataclass(frozen=True, slots=True)
class ProductDay:
    """One product-day's served retail figures."""

    revenue: Decimal
    cogs: Decimal
    units: Decimal
    returns: Decimal
    glance_views: Decimal | None = None


type ProductDays = Mapping[tuple[str, date], ProductDay]


@dataclass(frozen=True, slots=True)
class VendorExpectations:
    """The span, the visible products, the retail declarations and the committed values."""

    span: DateRange
    asins: tuple[str, ...]
    retail: RetailShape
    final: ProductDays


def _amount(value: object) -> Decimal:
    return Decimal(str(cast("dict[str, object]", value)["amount"]))


def expected_vendor(catalog: SyntheticEntityCatalog) -> VendorExpectations:
    """Read the committed final Vendor values (the restated report for every day)."""
    sales = json.loads((_FIXTURES / "vendor-sales-by-asin.json").read_text())
    traffic = json.loads((_FIXTURES / "vendor-traffic-by-asin.json").read_text())
    views = {
        (str(row["asin"]), date.fromisoformat(str(row["startDate"]))): Decimal(
            str(row["glanceViews"])
        )
        for row in cast("list[dict[str, object]]", traffic["trafficByAsin"])
    }
    final: dict[tuple[str, date], ProductDay] = {}
    for row in cast("list[dict[str, object]]", sales["salesByAsin"]):
        key = (str(row["asin"]), date.fromisoformat(str(row["startDate"])))
        final[key] = ProductDay(
            _amount(row["shippedRevenue"]),
            _amount(row["shippedCogs"]),
            Decimal(str(row["shippedUnits"])),
            Decimal(str(row["customerReturns"])),
            views.get(key),
        )
    return VendorExpectations(
        span=catalog_span(catalog),
        asins=tuple(product.asin for product in catalog.products),
        retail=catalog.shape.retail,
        final=final,
    )


def _span_days(span: DateRange) -> tuple[date, ...]:
    return tuple(span.start + timedelta(days=offset) for offset in range(span.days))


# --- Pure invariants ----------------------------------------------------------------------


def margin_report(served: ProductDays, expected: VendorExpectations) -> CheckReport:
    """Check COGS below revenue on every served product-day."""
    violations = tuple(
        f"{asin} {day}: COGS {item.cogs} >= revenue {item.revenue}"
        for (asin, day), item in sorted(served.items())
        if item.cogs >= item.revenue
    )
    minimum = len(expected.asins) * expected.span.days
    return CheckReport("COGS < revenue", len(served), minimum, violations)


def views_report(served: ProductDays, expected: VendorExpectations) -> CheckReport:
    """Check glance views above zero on every served product-day."""
    checked = [(key, item) for key, item in sorted(served.items()) if item.glance_views is not None]
    violations = tuple(
        f"{asin} {day}: glance views {item.glance_views}"
        for (asin, day), item in checked
        if item.glance_views is not None and item.glance_views <= 0
    )
    minimum = len(expected.asins) * expected.span.days
    return CheckReport("glance views > 0", len(checked), minimum, violations)


def returns_window_report(served: ProductDays, expected: VendorExpectations) -> CheckReport:
    """Check returns at or below shipped units over every 7D and 14D window.

    Per product and in total; a window with a missing product-day is itself a violation.
    """
    days = _span_days(expected.span)
    violations: list[str] = []
    evaluated = 0
    for width in (7, 14):
        for label, asins in (
            *((asin, (asin,)) for asin in expected.asins),
            ("all", expected.asins),
        ):
            for end in range(width - 1, len(days)):
                window = days[end - width + 1 : end + 1]
                items = [served.get((asin, day)) for asin in asins for day in window]
                present = [item for item in items if item is not None]
                if len(present) != len(items):
                    violations.append(f"{label} {width}D ending {days[end]}: missing product-days")
                    continue
                evaluated += 1
                returns = sum((item.returns for item in present), Decimal(0))
                units = sum((item.units for item in present), Decimal(0))
                if returns > units:
                    violations.append(f"{label} {width}D ending {days[end]}: returns > shipped")
    products = len(expected.asins) + 1
    minimum = products * (len(days) - 6) + products * (len(days) - 13)
    return CheckReport("window returns <= shipped", evaluated, minimum, tuple(violations))


def served_rpu_report(served: ProductDays, expected: VendorExpectations) -> CheckReport:
    """Check the served revenue per unit shipped inside `served_rpu_band` (R2-1)."""
    band = expected.retail.served_rpu_band
    violations = tuple(
        f"{asin} {day}: revenue per unit {item.revenue / item.units:.4f} out of band"
        for (asin, day), item in sorted(served.items())
        if item.units <= 0 or not band.low <= item.revenue / item.units <= band.high
    )
    minimum = len(expected.asins) * expected.span.days
    return CheckReport("served revenue per unit", len(served), minimum, violations)


def first_report_report(served: ProductDays, expected: VendorExpectations) -> CheckReport:
    """Check the last two days, which carry only their first report (R7-6).

    Under `unrestated_returns: final` the first report equals the final report, so the
    per-unit step is 0 and the return rate equals the final rate: any difference is a
    violation. Under `first_report` the per-unit basis is bounded by
    `first_report_step_max_pct` against the product's previous 7 final days, and returns by
    `first_report_returns_share` of the final returns plus one unit of rounding.
    """
    retail = expected.retail
    days = _span_days(expected.span)[-_FIRST_REPORT_DAYS:]
    violations: list[str] = []
    evaluated = 0
    for asin in expected.asins:
        for day in days:
            item, final = served.get((asin, day)), expected.final.get((asin, day))
            if item is None or final is None:
                violations.append(f"{asin} {day}: first-report day not served")
                continue
            evaluated += 1
            if retail.unrestated_returns == "final":
                if (item.revenue, item.units, item.returns) != (
                    final.revenue,
                    final.units,
                    final.returns,
                ):
                    violations.append(f"{asin} {day}: first report differs from the final report")
                continue
            prior = [
                expected.final[(asin, day - timedelta(days=offset))]
                for offset in range(1, 8)
                if (asin, day - timedelta(days=offset)) in expected.final
            ]
            mean = sum((p.revenue / p.units for p in prior), Decimal(0)) / len(prior)
            step = abs(item.revenue / item.units - mean) / mean * 100
            if step > retail.first_report_step_max_pct:
                violations.append(f"{asin} {day}: per-unit step {step:.2f}% above the bound")
            if item.returns > final.returns * retail.first_report_returns_share + 1:
                violations.append(f"{asin} {day}: first-report returns above the declared share")
    minimum = len(expected.asins) * _FIRST_REPORT_DAYS
    return CheckReport("first-report days", evaluated, minimum, tuple(violations))


def reports(served: ProductDays, expected: VendorExpectations) -> tuple[CheckReport, ...]:
    """Every Vendor invariant over one served read."""
    return (
        margin_report(served, expected),
        views_report(served, expected),
        returns_window_report(served, expected),
        served_rpu_report(served, expected),
        first_report_report(served, expected),
    )


# --- Lake reads ---------------------------------------------------------------------------


_SALES_SQL: Final = (
    # A-SQL-001: literal names, values bind as ?; see docs/security-dispositions.md
    "SELECT amazon_asin, CAST(date_key AS varchar), SUM(revenue_shipped), SUM(cogs_shipped), "
    "SUM(units_shipped), SUM(customer_returns) FROM fct_retail_sales "
    "WHERE date_key BETWEEN CAST(? AS date) AND CAST(? AS date) "
    "AND publication_id IN ({fence}) GROUP BY amazon_asin, date_key"
)
_VIEWS_SQL: Final = (
    # A-SQL-001: literal names, values bind as ?; see docs/security-dispositions.md
    "SELECT amazon_asin, CAST(date_key AS varchar), SUM(glance_views) "
    "FROM fct_product_page_views "
    "WHERE date_key BETWEEN CAST(? AS date) AND CAST(? AS date) "
    "AND publication_id IN ({fence}) GROUP BY amazon_asin, date_key"
)


def _daily_ids(
    request: ProbeRequest, current: DynamoCurrentPublications, contract_id: str
) -> tuple[str, ...]:
    scope, pack = publication_scope(contract_id, request.marketplace_key)
    partitions = tuple(
        PartitionIdentity(
            scope,
            MeasurementBucket(
                DateRange(day, day),
                pack.time.grain,
                scope.business_calendar,
                scope.source_timezone,
                pack.time.observation_basis,
            ),
        )
        for day in _span_days(request.window)
    )
    publications = current.current_exact(partitions)
    if publications is None or len(publications) != request.window.days:
        raise ProbeError("an exact daily Vendor CURRENT publication is absent")
    return tuple(item.publication_id.value for item in publications)


def _rows(  # noqa: PLR0917 -- the client, target, window, fence and statement
    client: AthenaClient, target: QueryTarget, window: DateRange, ids: tuple[str, ...], sql: str
) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    for part, fence in fences(window, ids):
        statement = sql.format(fence=publication_clause(fence))
        parameters = (part.start.isoformat(), part.end.isoformat(), *fence)
        rows.extend(query(QueryExecutionRequest(client, target, statement, parameters)))
    return rows


def _served(request: ProbeRequest, session: boto3.Session) -> dict[tuple[str, date], ProductDay]:
    resources = resolve_named_semantic_resources(
        request.environment, session.client("cloudformation"), session.client("ssm")
    )
    current = DynamoCurrentPublications(resources["publications"], session.client("dynamodb"))
    target = QueryTarget(resources["bake_workgroup"], resources["current_database"])
    athena = session.client("athena")
    views = {
        (str(asin), row_day(day)): decimal_value(value)
        for asin, day, value in _rows(
            athena,
            target,
            request.window,
            _daily_ids(request, current, _TRAFFIC_CONTRACT),
            _VIEWS_SQL,
        )
    }
    served: dict[tuple[str, date], ProductDay] = {}
    for asin, day, revenue, cogs, units, returns in _rows(
        athena, target, request.window, _daily_ids(request, current, _SALES_CONTRACT), _SALES_SQL
    ):
        key = (str(asin), row_day(day))
        served[key] = ProductDay(
            decimal_value(revenue),
            decimal_value(cogs),
            decimal_value(units),
            decimal_value(returns),
            views.get(key),
        )
    return served


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--profile", "--environment", "--region", "--expected-account", "--data-stack"):
        parser.add_argument(name, required=True)
    parser.add_argument("--marketplace-key", required=True)
    parser.add_argument("--window-start", required=True, type=date.fromisoformat)
    parser.add_argument("--window-end", required=True, type=date.fromisoformat)
    return parser


def _run(arguments: Iterable[str] | None = None) -> tuple[CheckReport, ...]:
    args = _parser().parse_args(None if arguments is None else list(arguments))
    catalog = load_synthetic_entity_catalog(_ROOT)
    request = ProbeRequest(
        args.profile,
        args.environment,
        args.region,
        args.expected_account,
        args.data_stack,
        args.marketplace_key,
        DateRange(args.window_start, args.window_end),
    )
    if request.environment != "dev" or request.data_stack != "Adlytics-DataV2-dev":
        raise ProbeError("probe accepts only the dev environment and its Data V2 stack")
    if request.profile.casefold() == "default":
        raise ProbeError("an explicit nondefault profile is required")
    if request.window != catalog_span(catalog):
        raise ProbeError("probe requires the declared fictional span")
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    return reports(_served(request, session), expected_vendor(catalog))


def main() -> int:
    """Run the bounded probe and return nonzero without exposing live evidence."""
    try:
        result = _run()
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1
    for report in result:
        print(f"  {report.line()}")
    if any(report.failed for report in result):
        print("FAILED: an invariant failed or its denominator fell short", file=sys.stderr)
        return 1
    print("PASS: served fictional Vendor state accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
