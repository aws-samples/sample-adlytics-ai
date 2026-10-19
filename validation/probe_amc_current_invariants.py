# SPDX-License-Identifier: MIT-0
"""Attest the exact deployed CURRENT publications for the fictional AMC cadence."""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from types_boto3_athena.client import AthenaClient

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.fixture_generation.catalog import (
    SyntheticEntityCatalog,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.shape import CampaignChecks, CampaignDay, Pause
from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
from adapters.outbound.athena.execution import (
    StatementRequest,
    await_completion,
    fetch_rows,
    start,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.entity_catalog import publication_order
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from domain.semantics.identity import (
    CanonicalCampaignKeyRequest,
    ChannelKey,
    PlatformKey,
    SourceIdentifier,
    SourceSystemKey,
    canonical_campaign_key,
)
from domain.semantics.publication import PartitionIdentity, PublicationScope
from domain.semantics.source_pack import SourcePack
from domain.semantics.time import MeasurementBucket, TimeGrain
from domain.shared_kernel.values import DateRange

_ROOT: Final = Path(__file__).resolve().parents[1]
_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_DATA_STACK: Final = "Adlytics-DataV2-dev"
_MARKETPLACE: Final = "amazon_us"
_ACCOUNT_DIGITS: Final = 12
_VISIBLE_CAMPAIGNS: Final = 60
_QUERY_TIMEOUT_SECONDS: Final = 180.0
_SOURCE_SCOPE: Final = SourceIdentifier("amc.entity", "fictional-fixture-amc-entity")
_SOURCE_SYSTEM: Final = SourceSystemKey("amc")
_DIMENSION_CONTRACTS: Final = ("amc-campaign-dsp", "amc-campaign-sponsored")
_EXPOSURE_CONTRACTS: Final = (
    "amc-dsp-clicks",
    "amc-dsp-impressions",
    "amc-dsp-views",
    "amc-reach-frequency",
)
_CONVERSION_CONTRACT: Final = "amc-attributed-conversion"
_CONTRACTS: Final = (*_DIMENSION_CONTRACTS, *_EXPOSURE_CONTRACTS, _CONVERSION_CONTRACT)
_FIXTURES: Final = _ROOT / "tests" / "fixtures" / "generated"
_EXPOSURE_FIXTURES: Final = (
    ("amc-dsp-impressions.csv", "impression_date_utc", "amc-dsp-impressions"),
    ("amc-dsp-clicks.csv", "impression_date_utc", "amc-dsp-clicks"),
    ("amc-dsp-views.csv", "event_date_utc", "amc-dsp-views"),
    ("amc-reach-frequency.csv", "event_date_utc", "amc-reach-frequency"),
)
_CONVERSION_FIXTURE: Final = ("amc-attributed-conversion.csv", "conversion_event_date_utc")
_DIMENSION_FIXTURES: Final = ("amc-campaign-dsp.csv", "amc-campaign-sponsored.csv")
_WINDOW_DAYS: Final = 7
_DIMENSION_WINDOW_DAYS: Final = 7
_FENCE_DAYS: Final = 90
"""Days per publication-fenced query, so one statement binds at most 90 publication IDs."""
_CONTINUITY_OFFSETS: Final = (56, 55)
"""Phase boundary days `last_day - 56` and `last_day - 55` (design §2.5)."""

type Pair = tuple[str, str]


class ProbeError(Exception):
    """A bounded deployed-acceptance failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProbeError("invalid explicit target arguments")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Every explicit operator target needed by the read-only acceptance."""

    profile: str
    environment: str
    region: str
    expected_account: str
    data_stack: str
    marketplace_key: str
    window: DateRange
    dimension_history: bool = False
    continuity_days: tuple[date, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryTarget:
    """The exact stack-owned Athena workgroup and database."""

    workgroup: str
    database: str


@dataclass(frozen=True, slots=True)
class QueryExecutionRequest:
    """One parameterized statement against the exact acceptance target."""

    client: AthenaClient
    target: QueryTarget
    sql: str
    parameters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CurrentFactRequest:
    """Shared target, cadence, and exact publication fences for one fact read."""

    client: AthenaClient
    target: QueryTarget
    probe: ProbeRequest
    publication_ids: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Exposure:
    """One campaign-day's exposure measures summed over the exposure contracts."""

    clicks: Decimal = Decimal(0)
    impressions: Decimal = Decimal(0)
    spend: Decimal = Decimal(0)
    reach_users: Decimal | None = None

    def plus(self, other: Exposure) -> Exposure:
        """Add another contract's measures for the same campaign-day."""
        reach = (
            self.reach_users
            if other.reach_users is None
            else other.reach_users + (self.reach_users or Decimal(0))
        )
        return Exposure(
            self.clicks + other.clicks,
            self.impressions + other.impressions,
            self.spend + other.spend,
            reach,
        )


@dataclass(frozen=True, slots=True)
class Conversion:
    """One campaign-day's attributed purchases and revenue, summed over ASINs."""

    purchases: Decimal = Decimal(0)
    revenue: Decimal = Decimal(0)

    def plus(self, other: Conversion) -> Conversion:
        """Add another conversion row for the same campaign-day."""
        return Conversion(self.purchases + other.purchases, self.revenue + other.revenue)


type ExposureDays = Mapping[tuple[Pair, date], Exposure]
type ConversionDays = Mapping[tuple[Pair, date], Conversion]


@dataclass(frozen=True, slots=True)
class DimensionExpectation:
    """One named visible campaign's first observed day `d_c` and its catalog name then."""

    first_observed: date
    name: str


@dataclass(frozen=True, slots=True)
class RenameControl:
    """The renamed campaign: its old and new names and the boundary between them."""

    campaign_key: str
    old_name: str
    new_name: str
    boundary: date


@dataclass(frozen=True, slots=True)
class DimensionVersion:
    """One served `dim_campaign` version."""

    campaign_key: str
    name: str | None
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class CampaignExpectations:
    """Every expectation, derived from the governed catalog and the committed fixtures."""

    span: DateRange
    all_pairs: frozenset[Pair]
    zero_conversion_pairs: frozenset[Pair]
    exposure_days: Mapping[Pair, frozenset[date]]
    conversion_days: Mapping[Pair, frozenset[date]]
    exposure: ExposureDays
    conversion: ConversionDays
    coverage_gap: tuple[Pair, date]
    launch: tuple[Pair, date]
    pause: tuple[Pair, date, date]
    checks: CampaignChecks
    dimension: Mapping[str, DimensionExpectation]
    rename: RenameControl | None


@dataclass(frozen=True, slots=True)
class CheckReport:
    """One invariant's denominator and violations (design §2.5: no pass on absence)."""

    name: str
    evaluated: int
    expected_minimum: int
    violations: tuple[str, ...]

    @property
    def failed(self) -> bool:
        """Fail on any violation, or on a denominator of 0 or below its expectation."""
        return (
            bool(self.violations) or self.evaluated == 0 or self.evaluated < self.expected_minimum
        )

    def line(self) -> str:
        """Render the bounded evidence line."""
        first = f" first={self.violations[0]}" if self.violations else ""
        return (
            f"{self.name}: evaluated={self.evaluated} expected_minimum={self.expected_minimum} "
            f"violations={len(self.violations)}{first}"
        )


def _non_blank(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise argparse.ArgumentTypeError("value must not be blank")
    return selected


def _profile(value: str) -> str:
    selected = _non_blank(value)
    if selected.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault profile is required")
    return selected


def _account(value: str) -> str:
    selected = _non_blank(value)
    if len(selected) != _ACCOUNT_DIGITS or not selected.isdigit():
        raise argparse.ArgumentTypeError("expected account must be 12 digits")
    return selected


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("day must be ISO-8601") from error


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--data-stack", required=True, type=_non_blank)
    parser.add_argument("--marketplace-key", required=True, type=_non_blank)
    parser.add_argument("--window-start", required=True, type=_day)
    parser.add_argument("--window-end", required=True, type=_day)
    parser.add_argument(
        "--dimension-history",
        action="store_true",
        help="check only the served campaign and product dimension history (design §2.5)",
    )
    parser.add_argument(
        "--continuity-day",
        action="append",
        type=_day,
        default=[],
        help="a re-landing boundary day to compare with the committed fixture; repeatable. "
        "The phase boundary days last_day - 56 and last_day - 55 are always checked",
    )
    return parser


def _arguments() -> ProbeRequest:
    args = _parser().parse_args()
    try:
        window = DateRange(args.window_start, args.window_end)
    except ValueError as error:
        raise ProbeError("invalid cadence window") from error
    return ProbeRequest(
        profile=args.profile,
        environment=args.environment,
        region=args.region,
        expected_account=args.expected_account,
        data_stack=args.data_stack,
        marketplace_key=args.marketplace_key,
        window=window,
        dimension_history=bool(args.dimension_history),
        continuity_days=tuple(args.continuity_day),
    )


def catalog_span(catalog: SyntheticEntityCatalog) -> DateRange:
    """Return the declared fixture span the lake must hold."""
    span = catalog.shape.fixture_span
    return DateRange(span.first_day, span.last_day)


def catalog_coverage_gap(catalog: SyntheticEntityCatalog) -> tuple[str, date]:
    """Return the declared `coverage_gap` event's campaign and day."""
    gaps = [event for event in catalog.shape.events if isinstance(event, CampaignDay)]
    if len(gaps) != 1:
        raise ProbeError("the catalog must declare exactly one coverage gap")
    return gaps[0].campaign, gaps[0].on


def _validate_target(request: ProbeRequest, catalog: SyntheticEntityCatalog) -> None:
    if request.environment != _ENVIRONMENT:
        raise ProbeError("probe accepts only the dev environment")
    if request.region != _REGION:
        raise ProbeError("probe accepts only the governed dev region")
    if request.data_stack != _DATA_STACK:
        raise ProbeError("probe requires the exact Data V2 dev stack")
    if request.marketplace_key != _MARKETPLACE:
        raise ProbeError("probe requires the governed fictional marketplace")
    if request.window != catalog_span(catalog):
        raise ProbeError("probe requires the declared fictional cadence window")


def _days(window: DateRange) -> tuple[date, ...]:
    return tuple(window.start + timedelta(days=index) for index in range(window.days))


def _fixture_rows(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO((_FIXTURES / name).read_text())))


def _contract_scales(contract_id: str) -> dict[str, tuple[str, int | None]]:
    """Map each target to its source column and scale divisor."""
    contract = next(item for _, item in load_contracts(_ROOT) if item.contract_id == contract_id)
    return {
        field.target_column: (field.source_path, field.scale_divisor)
        for field in contract.fields
        if field.source_path
    }


def _measure(
    row: Mapping[str, str], scales: Mapping[str, tuple[str, int | None]], target: str
) -> Decimal | None:
    """Read one target from a fixture row: its source column, else a column named after it."""
    entry = scales.get(target)
    if entry is None:
        return None
    source, divisor = entry
    column = source if source in row else target if target in row else None
    if column is None:
        return None
    value = Decimal(row[column] or "0")
    return value if divisor is None else value / divisor


def _fixture_exposure(pairs: Mapping[str, Pair]) -> dict[tuple[Pair, date], Exposure]:
    found: dict[tuple[Pair, date], Exposure] = {}
    for name, date_column, contract_id in _EXPOSURE_FIXTURES:
        scales = _contract_scales(contract_id)
        for row in _fixture_rows(name):
            item = Exposure(
                _measure(row, scales, "clicks") or Decimal(0),
                _measure(row, scales, "impressions") or Decimal(0),
                _measure(row, scales, "spend") or Decimal(0),
                _measure(row, scales, "reach_users"),
            )
            key = (pairs[row["campaign_id_string"]], date.fromisoformat(row[date_column]))
            found[key] = found[key].plus(item) if key in found else item
    return found


def _fixture_conversion(pairs: Mapping[str, Pair]) -> dict[tuple[Pair, date], Conversion]:
    name, date_column = _CONVERSION_FIXTURE
    scales = _contract_scales(_CONVERSION_CONTRACT)
    found: dict[tuple[Pair, date], Conversion] = {}
    for row in _fixture_rows(name):
        item = Conversion(
            _measure(row, scales, "purchases") or Decimal(0),
            _measure(row, scales, "revenue_attributed") or Decimal(0),
        )
        key = (pairs[row["campaign_id_string"]], date.fromisoformat(row[date_column]))
        found[key] = found[key].plus(item) if key in found else item
    return found


def days_by_pair(keys: Iterable[tuple[Pair, date]]) -> dict[Pair, frozenset[date]]:
    """Group served campaign-day keys into each campaign's set of days."""
    found: dict[Pair, set[date]] = defaultdict(set)
    for pair, day in keys:
        found[pair].add(day)
    return {pair: frozenset(days) for pair, days in found.items()}


def _dimension_expectations(
    catalog: SyntheticEntityCatalog, pairs: Mapping[str, Pair]
) -> tuple[dict[str, DimensionExpectation], RenameControl | None]:
    """`d_c` and the name valid on it, for every named visible campaign (design §2.5)."""
    first: dict[str, date] = {}
    for name in _DIMENSION_FIXTURES:
        for row in _fixture_rows(name):
            day = date.fromisoformat(row["source_observed_on"])
            source = row["campaign_id_string"]
            first[source] = min(first.get(source, day), day)
    expected: dict[str, DimensionExpectation] = {}
    rename: RenameControl | None = None
    for campaign in catalog.campaigns:
        source = next(iter(campaign.source_aliases.values()))
        key = pairs[source][0]
        if source not in first:
            raise ProbeError("a visible campaign is absent from the committed dimension fixture")
        observed = first[source]
        name = campaign.name_on(observed)
        if name is not None:
            expected[key] = DimensionExpectation(observed, name)
        if len(campaign.names) == 2:  # noqa: PLR2004 -- the declared single rename
            rename = RenameControl(
                key, campaign.names[0].value, campaign.names[1].value, campaign.names[1].valid_from
            )
    return expected, rename


def expected_campaigns(catalog: SyntheticEntityCatalog) -> CampaignExpectations:
    """Derive every expectation from the governed catalog and the committed fixtures."""
    bridge = load_commerce_identity_bridge(_ROOT)
    pairs: dict[str, Pair] = {}
    by_key: dict[str, Pair] = {}
    zero: set[Pair] = set()
    for campaign in catalog.campaigns:
        source_value = next(iter(campaign.source_aliases.values()))
        key = canonical_campaign_key(
            CanonicalCampaignKeyRequest(
                commerce_scope=bridge.commerce_scope,
                platform=PlatformKey(campaign.platform),
                channel=ChannelKey(campaign.channel),
                source_system=_SOURCE_SYSTEM,
                source_scope=_SOURCE_SCOPE,
                identifier_kind="campaign_id_string",
                source_value=source_value,
            )
        ).value
        pair = (key, campaign.channel)
        pairs[source_value] = pair
        by_key[campaign.key] = pair
        if campaign.scenario == "zero_conversion":
            zero.add(pair)
    if len(pairs) != _VISIBLE_CAMPAIGNS or not zero:
        raise ProbeError("governed campaign expectations are malformed")
    exposure = _fixture_exposure(pairs)
    conversion = _fixture_conversion(pairs)
    gap_campaign, gap_day = catalog_coverage_gap(catalog)
    pause = next(event for event in catalog.shape.events if isinstance(event, Pause))
    dimension, rename = _dimension_expectations(catalog, pairs)
    return CampaignExpectations(
        span=catalog_span(catalog),
        all_pairs=frozenset(pairs.values()),
        zero_conversion_pairs=frozenset(zero),
        exposure_days=days_by_pair(exposure),
        conversion_days=days_by_pair(conversion),
        exposure=exposure,
        conversion=conversion,
        coverage_gap=(by_key[gap_campaign], gap_day),
        launch=(by_key[catalog.shape.launch.campaign], catalog.shape.launch.first_delivery),
        pause=(by_key[pause.campaign], pause.first, pause.last),
        checks=catalog.shape.campaign_checks,
        dimension=dimension,
        rename=rename,
    )


# --- Pure invariants (each unit-tested over one seeded violating row and one clean row) ----


def coverage_report(
    name: str,
    observed: Mapping[Pair, Iterable[date]],
    expected: Mapping[Pair, frozenset[date]],
) -> CheckReport:
    """Every campaign's served days equal the committed fixture's days (and no extra pair)."""
    violations = [
        f"{pair[0]}: missing={len(expected.get(pair, frozenset()) - set(days))} "
        f"extra={len(set(days) - expected.get(pair, frozenset()))}"
        for pair, days in sorted(observed.items())
        if frozenset(days) != expected.get(pair, frozenset())
    ] + [f"{pair[0]}: no rows" for pair in sorted(set(expected) - set(observed))]
    evaluated = sum(len(frozenset(days)) for days in observed.values())
    return CheckReport(
        name, evaluated, sum(len(days) for days in expected.values()), tuple(violations)
    )


def absence_report(
    exposure_days: Mapping[Pair, Iterable[date]],
    conversion_days: Mapping[Pair, Iterable[date]],
    expected: CampaignExpectations,
) -> CheckReport:
    """Check the three declared row absences, each with its in-query positive control."""
    violations: list[str] = []
    launch, first_delivery = expected.launch
    launch_days = set(conversion_days.get(launch, ()))
    if any(day < first_delivery for day in launch_days):
        violations.append(f"{launch[0]}: conversion rows before its first delivery")
    if first_delivery not in launch_days:
        violations.append(f"{launch[0]}: no conversion row on its first delivery (control)")
    pause, first, last = expected.pause
    pause_days = set(conversion_days.get(pause, ()))
    if any(first <= day <= last for day in pause_days):
        violations.append(f"{pause[0]}: conversion rows on its pause days")
    for control in (first - timedelta(days=1), last + timedelta(days=1)):
        if expected.span.start <= control <= expected.span.end and control not in pause_days:
            violations.append(f"{pause[0]}: no conversion row on {control} (control)")
    gap, gap_day = expected.coverage_gap
    gap_days = set(exposure_days.get(gap, ()))
    if gap_day in gap_days:
        violations.append(f"{gap[0]}: exposure rows on its coverage-gap day")
    for control in (gap_day - timedelta(days=1), gap_day + timedelta(days=1)):
        if expected.span.start <= control <= expected.span.end and control not in gap_days:
            violations.append(f"{gap[0]}: no exposure row on {control} (control)")
    evaluated = len(launch_days) + len(pause_days) + len(gap_days)
    return CheckReport("row absences", evaluated, 1, tuple(violations))


def reach_report(exposure: ExposureDays) -> CheckReport:
    """`reach_users <= impressions` on every campaign-day that reports reach (R-20).

    `distinct_user_count` is a suppression column on the exposure contracts and is not stored
    on `fct_ad_exposure`, so the lake holds the upper half of the design's chain only.
    """
    checked = [
        (key, item) for key, item in sorted(exposure.items()) if item.reach_users is not None
    ]
    violations = tuple(
        f"{pair[0]} {day}: reach {item.reach_users} > impressions {item.impressions}"
        for (pair, day), item in checked
        if item.reach_users is not None and item.reach_users > item.impressions
    )
    return CheckReport("reach <= impressions", len(checked), 1, violations)


def funnel_report(exposure: ExposureDays, conversion: ConversionDays) -> CheckReport:
    """`purchases <= clicks` on every served campaign-day."""
    violations = tuple(
        f"{pair[0]} {day}: purchases {item.purchases} > clicks "
        f"{exposure.get((pair, day), Exposure()).clicks}"
        for (pair, day), item in sorted(conversion.items())
        if item.purchases > exposure.get((pair, day), Exposure()).clicks
    )
    return CheckReport("purchases <= clicks", len(conversion), 1, violations)


def _band_windows(
    exposure: ExposureDays, conversion: ConversionDays, expected: CampaignExpectations
) -> tuple[int, list[str]]:
    days = _days(expected.span)
    violations: list[str] = []
    evaluated = 0
    cvr, roas = expected.checks.window_cvr_band, expected.checks.window_roas_band
    for pair in sorted(expected.all_pairs - expected.zero_conversion_pairs):
        for end in range(_WINDOW_DAYS - 1, len(days)):
            window = days[end - _WINDOW_DAYS + 1 : end + 1]
            served = [exposure.get((pair, day), Exposure()) for day in window]
            converted = [conversion.get((pair, day), Conversion()) for day in window]
            clicks = sum((item.clicks for item in served), Decimal(0))
            spend = sum((item.spend for item in served), Decimal(0))
            if clicks == 0 or spend == 0:
                continue
            evaluated += 1
            purchases = sum((item.purchases for item in converted), Decimal(0))
            revenue = sum((item.revenue for item in converted), Decimal(0))
            if not cvr.low <= purchases / clicks <= cvr.high:
                violations.append(
                    f"{pair[0]} 7D ending {days[end]}: purchases / clicks out of band"
                )
            if not roas.low <= revenue / spend <= roas.high:
                violations.append(f"{pair[0]} 7D ending {days[end]}: ROAS out of band")
    return evaluated, violations


def window_band_report(
    exposure: ExposureDays, conversion: ConversionDays, expected: CampaignExpectations
) -> CheckReport:
    """Check 7D purchases / clicks and ROAS inside the declared bands, as the generator does.

    The denominator must reach the window count the committed fixture itself yields.
    """
    evaluated, violations = _band_windows(exposure, conversion, expected)
    minimum, _ = _band_windows(expected.exposure, expected.conversion, expected)
    return CheckReport("7D funnel and ROAS bands", evaluated, minimum, tuple(violations))


def continuity_report(  # noqa: PLR0917 -- the two lake reads, the expectations, the days
    lake_exposure: ExposureDays,
    lake_conversion: ConversionDays,
    expected: CampaignExpectations,
    days: Iterable[date],
) -> CheckReport:
    """On each boundary day, every campaign's served measures equal the committed fixture."""
    violations: list[str] = []
    evaluated = 0
    for day in sorted(set(days)):
        for pair in sorted(expected.all_pairs):
            key = (pair, day)
            served = (lake_exposure.get(key), lake_conversion.get(key))
            committed = (expected.exposure.get(key), expected.conversion.get(key))
            evaluated += 1
            if (
                _exposure_tuple(served[0]) != _exposure_tuple(committed[0])
                or served[1] != committed[1]
            ):
                violations.append(f"{pair[0]} {day}: served measures differ from the fixture")
    return CheckReport("boundary continuity", evaluated, len(expected.all_pairs), tuple(violations))


def _exposure_tuple(item: Exposure | None) -> tuple[Decimal, Decimal, Decimal] | None:
    return None if item is None else (item.clicks, item.impressions, item.spend)


def _effective(version: DimensionVersion, start: date, end: date) -> bool:
    return version.valid_from <= end and (version.valid_to is None or start < version.valid_to)


def dimension_history_report(
    versions: Iterable[DimensionVersion],
    expected: Mapping[str, DimensionExpectation],
    rename: RenameControl | None,
) -> CheckReport:
    """Every named visible campaign keeps its history from `d_c` (design §2.5, R7-1)."""
    by_key: dict[str, list[DimensionVersion]] = defaultdict(list)
    for version in versions:
        by_key[version.campaign_key].append(version)
    violations: list[str] = []
    evaluated = 0
    for key, item in sorted(expected.items()):
        found = by_key.get(key, [])
        if not found:
            violations.append(f"{key}: no served version")
            continue
        evaluated += 1
        end = item.first_observed + timedelta(days=_DIMENSION_WINDOW_DAYS - 1)
        effective = [version for version in found if _effective(version, item.first_observed, end)]
        earliest = min(version.valid_from for version in found)
        if not effective:
            violations.append(
                f"{key}: no version effective from {item.first_observed}; earliest {earliest}"
            )
        elif any(version.name != item.name for version in effective):
            violations.append(
                f"{key}: the version effective on {item.first_observed} has another name"
            )
        if earliest > item.first_observed:
            violations.append(
                f"{key}: earliest valid_from {earliest} is after {item.first_observed}"
            )
    if rename is not None:
        found = by_key.get(rename.campaign_key, [])
        before = [
            version.name
            for version in found
            if _effective(
                version, rename.boundary - timedelta(days=7), rename.boundary - timedelta(days=1)
            )
        ]
        after = [
            version.name
            for version in found
            if _effective(version, rename.boundary, rename.boundary + timedelta(days=6))
        ]
        if before != [rename.old_name] or after != [rename.new_name]:
            violations.append(
                f"{rename.campaign_key}: the {rename.boundary} rename is not versioned"
            )
    return CheckReport("dimension history", evaluated, len(expected), tuple(violations))


def product_report(
    rows: Iterable[tuple[str, bool, date]], asins: Iterable[str]
) -> tuple[CheckReport, date | None]:
    """Every visible product present and current; the earliest `valid_from` is recorded."""
    current = {asin for asin, is_current, _ in rows if is_current}
    starts = [valid_from for _, _, valid_from in rows]
    expected = sorted(set(asins))
    missing = tuple(f"{asin}: not current" for asin in expected if asin not in current)
    return (
        CheckReport("product dimension", len(current & set(expected)), len(expected), missing),
        min(starts) if starts else None,
    )


# --- Lake reads (read-only: DynamoDB CURRENT pointers and publication-fenced Athena) -------


def query(request: QueryExecutionRequest) -> list[list[str | None]]:
    """Run one parameterized read-only statement and return its rows."""
    execution_id = start(
        request.client,
        StatementRequest(
            sql=request.sql,
            parameters=request.parameters,
            workgroup=request.target.workgroup,
            database=request.target.database,
            reuse_minutes=0,
        ),
    )
    await_completion(request.client, execution_id, limit=_QUERY_TIMEOUT_SECONDS)
    return fetch_rows(request.client, execution_id)


def publication_clause(publication_ids: tuple[str, ...]) -> str:
    """Render one `?` placeholder per fenced publication ID."""
    if not publication_ids:
        raise ProbeError("publication fence is empty")
    return ",".join("?" for _ in publication_ids)


def publication_scope(
    contract_id: str, marketplace_key: str
) -> tuple[PublicationScope, SourcePack]:
    """Resolve a contract's one governed publication scope for a marketplace."""
    contracts = {contract.contract_id: contract for _, contract in load_contracts(_ROOT)}
    pack = {item.contract_id: item for item in load_source_packs(_ROOT)}[contract_id]
    scopes = publication_scopes_for(
        PublicationScopeRequest(
            pack=pack,
            contract=contracts[contract_id],
            bridge=load_commerce_identity_bridge(_ROOT),
            filters={"marketplace_key": marketplace_key},
        )
    )
    if len(scopes) != 1:
        raise ProbeError("a governed publication scope is ambiguous")
    return scopes[0], pack


def _current_publication_ids(
    request: ProbeRequest, current: DynamoCurrentPublications
) -> dict[str, tuple[str, ...]]:
    """Every fact contract's exact daily CURRENT publication, one per span day."""
    days = _days(request.window)
    found: dict[str, tuple[str, ...]] = {}
    for contract_id in (*_EXPOSURE_CONTRACTS, _CONVERSION_CONTRACT):
        scope, pack = publication_scope(contract_id, request.marketplace_key)
        if pack.time.grain is not TimeGrain.DAY:
            raise ProbeError("a governed AMC fact is not daily")
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
            for day in days
        )
        publications = current.current_exact(partitions)
        if publications is None or len(publications) != len(days):
            raise ProbeError("an exact daily AMC CURRENT publication is absent")
        found[contract_id] = tuple(item.publication_id.value for item in publications)
    return found


def fences(
    window: DateRange, publication_ids: tuple[str, ...]
) -> Iterable[tuple[DateRange, tuple[str, ...]]]:
    """Split a daily fence into windows of at most `_FENCE_DAYS` days and their IDs."""
    for offset in range(0, window.days, _FENCE_DAYS):
        first = window.start + timedelta(days=offset)
        last = min(window.end, first + timedelta(days=_FENCE_DAYS - 1))
        yield DateRange(first, last), publication_ids[offset : offset + (last - first).days + 1]


def _pair(campaign: str | None, channel: str | None) -> Pair:
    if campaign is None or channel is None:
        raise ProbeError("canonical AMC query returned a malformed identity row")
    return campaign, channel


def decimal_value(value: str | None) -> Decimal:
    """Parse an Athena measure; NULL reads as 0."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ProbeError("canonical AMC query returned a malformed measure") from error


def row_day(value: str | None) -> date:
    """Parse an Athena date or timestamp rendering into its day."""
    if value is None:
        raise ProbeError("canonical AMC query returned a missing date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as error:
        raise ProbeError("canonical AMC query returned a malformed date") from error


_EXPOSURE_SQL: Final = (
    # A-SQL-001: literal names, values bind as ?; see docs/security-dispositions.md
    "SELECT campaign_key, channel_key, CAST(date_key AS varchar), "
    "SUM(COALESCE(clicks, 0)), SUM(COALESCE(impressions, 0)), SUM(COALESCE(spend, 0)), "
    "SUM(reach_users) FROM fct_ad_exposure "
    "WHERE date_key BETWEEN CAST(? AS date) AND CAST(? AS date) "
    "AND publication_id IN ({fence}) GROUP BY campaign_key, channel_key, date_key"
)
_CONVERSION_SQL: Final = (
    # A-SQL-001: literal names, values bind as ?; see docs/security-dispositions.md
    "SELECT campaign_key, channel_key, CAST(date_key AS varchar), "
    "SUM(COALESCE(purchases, 0)), SUM(COALESCE(revenue_attributed, 0)) "
    "FROM fct_attributed_conversion "
    "WHERE date_key BETWEEN CAST(? AS date) AND CAST(? AS date) "
    "AND publication_id IN ({fence}) GROUP BY campaign_key, channel_key, date_key"
)


def _fenced_rows(
    request: CurrentFactRequest, contract_id: str, template: str
) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    for window, ids in fences(request.probe.window, request.publication_ids[contract_id]):
        sql = template.format(fence=publication_clause(ids))
        parameters = (window.start.isoformat(), window.end.isoformat(), *ids)
        rows.extend(query(QueryExecutionRequest(request.client, request.target, sql, parameters)))
    return rows


def _exposure(request: CurrentFactRequest) -> dict[tuple[Pair, date], Exposure]:
    found: dict[tuple[Pair, date], Exposure] = {}
    for contract_id in _EXPOSURE_CONTRACTS:
        for row in _fenced_rows(request, contract_id, _EXPOSURE_SQL):
            try:
                campaign, channel, day, clicks, impressions, spend, reach = row
            except ValueError as error:
                raise ProbeError("canonical exposure query returned a malformed row") from error
            item = Exposure(
                decimal_value(clicks),
                decimal_value(impressions),
                decimal_value(spend),
                None if reach is None else decimal_value(reach),
            )
            key = (_pair(campaign, channel), row_day(day))
            found[key] = found[key].plus(item) if key in found else item
    return found


def _conversion(request: CurrentFactRequest) -> dict[tuple[Pair, date], Conversion]:
    found: dict[tuple[Pair, date], Conversion] = {}
    for row in _fenced_rows(request, _CONVERSION_CONTRACT, _CONVERSION_SQL):
        try:
            campaign, channel, day, purchases, revenue = row
        except ValueError as error:
            raise ProbeError("canonical conversion query returned a malformed row") from error
        key = (_pair(campaign, channel), row_day(day))
        item = Conversion(decimal_value(purchases), decimal_value(revenue))
        found[key] = found[key].plus(item) if key in found else item
    return found


def _latest_publication_id(
    current: DynamoCurrentPublications, contract_id: str, marketplace_key: str
) -> str:
    """Return the partition the entity catalog serves for a dimension scope (its order rule)."""
    scope, _pack = publication_scope(contract_id, marketplace_key)
    publications = current.current_for_scope(scope)
    if not publications:
        raise ProbeError("a governed dimension scope has no CURRENT partition")
    return max(publications, key=publication_order).publication_id.value


def _history_day(value: str | None) -> date | None:
    day = row_day(value)
    return None if day.year >= _OPEN_ENDED_YEAR else day


_OPEN_ENDED_YEAR: Final = 2999


def _dimension_versions(
    client: AthenaClient, target: QueryTarget, publication_id: str
) -> list[DimensionVersion]:
    sql = (
        # A-SQL-001: literal names, the ID binds as ?; see docs/security-dispositions.md
        "SELECT campaign_key, campaign_name, CAST(valid_from AS varchar), "
        "CAST(valid_to AS varchar) FROM dim_campaign WHERE publication_id = ?"
    )
    versions: list[DimensionVersion] = []
    for row in query(QueryExecutionRequest(client, target, sql, (publication_id,))):
        try:
            campaign, name, valid_from, valid_to = row
        except ValueError as error:
            raise ProbeError("canonical dimension query returned a malformed row") from error
        if campaign is None:
            raise ProbeError("canonical dimension query returned a malformed row")
        versions.append(
            DimensionVersion(campaign, name, row_day(valid_from), _history_day(valid_to))
        )
    return versions


def _product_rows(
    client: AthenaClient, target: QueryTarget, publication_id: str
) -> list[tuple[str, bool, date]]:
    sql = (
        # A-SQL-001: literal names, the ID binds as ?; see docs/security-dispositions.md
        "SELECT amazon_asin, is_current, CAST(valid_from AS varchar) "
        "FROM dim_product WHERE publication_id = ?"
    )
    found: list[tuple[str, bool, date]] = []
    for row in query(QueryExecutionRequest(client, target, sql, (publication_id,))):
        try:
            asin, is_current, valid_from = row
        except ValueError as error:
            raise ProbeError("canonical product query returned a malformed row") from error
        if asin is None:
            raise ProbeError("canonical product query returned a malformed row")
        found.append((asin, is_current == "true", row_day(valid_from)))
    return found


def continuity_days(request: ProbeRequest) -> tuple[date, ...]:
    """Return the phase boundary days, plus every requested re-landing boundary day."""
    boundary = tuple(request.window.end - timedelta(days=offset) for offset in _CONTINUITY_OFFSETS)
    return tuple(sorted({*boundary, *request.continuity_days}))


def _reports(request: ProbeRequest, session: boto3.Session) -> tuple[CheckReport, ...]:
    catalog = load_synthetic_entity_catalog(_ROOT)
    expected = expected_campaigns(catalog)
    resources = resolve_named_semantic_resources(
        request.environment,
        session.client("cloudformation"),
        session.client("ssm"),
    )
    current = DynamoCurrentPublications(resources["publications"], session.client("dynamodb"))
    target = QueryTarget(resources["bake_workgroup"], resources["current_database"])
    athena = session.client("athena")
    if request.dimension_history:
        versions = [
            version
            for contract_id in _DIMENSION_CONTRACTS
            for version in _dimension_versions(
                athena,
                target,
                _latest_publication_id(current, contract_id, request.marketplace_key),
            )
        ]
        products, earliest = product_report(
            _product_rows(
                athena,
                target,
                _latest_publication_id(current, _PRODUCT_CONTRACT, request.marketplace_key),
            ),
            (product.asin for product in catalog.products),
        )
        print(f"  product_names_valid_from={earliest}")
        return (dimension_history_report(versions, expected.dimension, expected.rename), products)
    fact_request = CurrentFactRequest(
        athena, target, request, _current_publication_ids(request, current)
    )
    exposure = _exposure(fact_request)
    conversion = _conversion(fact_request)
    return (
        coverage_report("exposure coverage", days_by_pair(exposure), expected.exposure_days),
        coverage_report("conversion coverage", days_by_pair(conversion), expected.conversion_days),
        absence_report(days_by_pair(exposure), days_by_pair(conversion), expected),
        reach_report(exposure),
        funnel_report(exposure, conversion),
        window_band_report(exposure, conversion, expected),
        continuity_report(exposure, conversion, expected, continuity_days(request)),
    )


_PRODUCT_CONTRACT: Final = "vendor-product-hierarchy"


def _run(request: ProbeRequest) -> tuple[CheckReport, ...]:
    _validate_target(request, load_synthetic_entity_catalog(_ROOT))
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    # aws-pagination: an exact StackName returns one stack; the count is checked below.
    stack = session.client("cloudformation").describe_stacks(StackName=request.data_stack)
    if len(stack.get("Stacks", [])) != 1:
        raise ProbeError("exact Data V2 stack is unavailable")
    return _reports(request, session)


def main() -> int:
    """Run the bounded probe and return nonzero without exposing live evidence."""
    try:
        reports = _run(_arguments())
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
    for report in reports:
        print(f"  {report.line()}")
    if any(report.failed for report in reports):
        print("FAILED: an invariant failed or its denominator fell short", file=sys.stderr)
        return 1
    print("PASS: exact-current fictional AMC state accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
