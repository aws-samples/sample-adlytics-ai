"""A fictional campaign estate measured end to end: real batches over a folding query port.

`EstateQueries` answers every plan the Campaigns portfolio batch prepares, held (`run`) or
folded (`fold`), with the same deterministic rows for the same plan, so the bake's compact
record and the request-time held compares can be compared figure for figure. Every value is
fictional and derived from the campaign's position; no row resembles a live one.
"""

# ruff: noqa: PLR0913, PLR0917
from __future__ import annotations

import pathlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache

from publication_support import StubPublications

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderer,
    CampaignArtifactRenderInput,
)
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
    CampaignMeasurementSelection,
)
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import (
    _suppression_from,  # pyright: ignore[reportPrivateUsage]
    _SuppressionAccumulator,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import DataContract
from domain.measurement.planning import (
    CURRENCY_COLUMN,
    QueryPlan,
    measure_alias,
    withheld_alias,
    withheld_rows_alias,
)
from domain.presentation.campaign_attribution import (
    CampaignSelectionCapability,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import CampaignPortfolioSort, CampaignPortfolioSortField
from domain.presentation.campaigns import EffectiveNameStatus
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.query import (
    FoldRead,
    PeriodSeriesRequest,
    QueryRequest,
    QueryResult,
    QueryRow,
    RowSink,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
MARKETPLACE = MarketplaceKey("amazon_us")
NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
PERIOD = DateRange(date(2026, 7, 23), date(2026, 8, 21))
PRIOR = DateRange(date(2026, 6, 23), date(2026, 7, 22))
CHANNEL_ORDER = (
    "amazon_dsp",
    "amazon_sponsored_products",
    "amazon_sponsored_brands",
    "amazon_sponsored_display",
    "amazon_sponsored_television",
)
CHANNEL_LABELS = {
    "amazon_dsp": "Amazon DSP",
    "amazon_sponsored_products": "Sponsored Products",
    "amazon_sponsored_brands": "Sponsored Brands",
    "amazon_sponsored_display": "Sponsored Display",
    "amazon_sponsored_television": "Sponsored Television",
}
OUTSIDE = 3
"""Campaigns the reads return that the catalog doesn't authorize: never part of any figure."""


@cache
def catalog_and_contracts() -> tuple[Catalog, tuple[DataContract, ...]]:
    """Return the committed metric dictionary and every contract, read once."""
    return load_catalog(ROOT), tuple(contract for _, contract in load_contracts(ROOT))


def _selection(index: int, channel: str) -> CampaignMeasurementSelection:
    name = f"Fictional Estate {index:06d}"
    return CampaignMeasurementSelection(
        CampaignKey(f"campaign:fictional-estate-{index:06d}", SCOPE, PLATFORM),
        ChannelKey(channel),
        MARKETPLACE,
        SafeCampaignDisplaySignature(
            effective_name=name,
            channel=CHANNEL_LABELS[channel],
            marketplace="United States",
            platform="Amazon Ads",
            effective_name_status=EffectiveNameStatus.AVAILABLE,
            effective_name_valid_from=date(2026, 1, 1),
        ),
    )


def estate(count: int) -> tuple[CampaignMeasurementSelection, ...]:
    """Return `count` fictional campaigns spread over the five governed channels."""
    return tuple(
        _selection(index, CHANNEL_ORDER[index % len(CHANNEL_ORDER)]) for index in range(count)
    )


def _value(metric: str, measure: int, position: int, when: date) -> int | None:
    """One fictional operand; None where the campaign delivered nothing on that read."""
    salt = sum(ord(character) for character in metric) + 7 * measure
    if (position + salt) % 13 == 0:
        return None
    if measure == 1 and (position + when.toordinal()) % 11 == 0:
        return 0  # a ratio's zero denominator: the figure is typed absent
    return 1 + (position * 37 + salt * 11 + when.toordinal()) % 9_973


@dataclass
class EstateQueries:
    """A folding query port answering every Campaigns plan over a fictional estate."""

    selections: tuple[CampaignMeasurementSelection, ...]
    statements: list[str] = field(default_factory=list[str])
    negative: str | None = None
    """A metric whose first row is negative: a held parse refuses it, and so must the record."""
    mixed_currency: str | None = None
    """A metric whose first row is in another currency: refused by the currency rule."""

    def rows(self, plan: QueryPlan) -> tuple[QueryRow, ...]:
        """Every row the plan's read returns: one per campaign (and day, for a daily read)."""
        return tuple(self.stream(plan))

    def stream(self, plan: QueryPlan) -> Iterator[QueryRow]:
        """Yield the plan's rows one at a time, as a streamed read hands them on."""
        keys = [(item.campaign.value, item.marketplace.value) for item in self.selections]
        keys.extend(
            (f"campaign:fictional-outside-{index}", MARKETPLACE.value) for index in range(OUTSIDE)
        )
        daily = "date_key" in plan.group_by
        days = (
            tuple(plan.window.start + timedelta(days=offset) for offset in range(plan.window.days))
            if daily
            else (plan.window.start,)
        )
        first = True
        for position, (campaign, marketplace) in enumerate(keys):
            for day in days:
                values = [
                    _value(plan.metric.name, measure, position, day)
                    for measure in range(max(1, len(plan.measures)))
                ]
                if values[0] is None:
                    continue
                row: dict[str, str | int | None] = {
                    "campaign_key": campaign,
                    "marketplace_key": marketplace,
                }
                if daily:
                    row["date_key"] = day.isoformat()
                if any(column not in row for column in plan.group_by):
                    raise AssertionError(f"the estate doesn't group by {plan.group_by}")
                trap, first = first, False
                for column in plan.carried_columns:
                    euro = trap and plan.metric.name == self.mixed_currency
                    currency = "EUR" if euro else "USD"
                    row[column] = currency if column == CURRENCY_COLUMN else f"{column}-value"
                for measure, value in enumerate(values):
                    negative = trap and measure == 0 and plan.metric.name == self.negative
                    row[measure_alias(measure)] = -value if negative and value else value
                    if plan.suppression_columns:
                        row[withheld_alias(measure)] = 0
                        row[withheld_rows_alias(measure)] = 0
                yield row

    def run(self, request: QueryRequest) -> QueryResult:
        plan = request.plan.logical
        self.statements.append(f"run:{plan.metric.name}")
        rows = self.rows(plan)
        truncated = len(rows) > request.max_rows
        kept = rows[: request.max_rows]
        return QueryResult(
            rows=kept,
            bytes_scanned=0,
            truncated=truncated,
            suppression=_suppression_from(plan, kept),
        )

    def run_series(self, request: PeriodSeriesRequest) -> QueryResult:
        raise AssertionError(f"the Campaigns portfolio reads no series: {request}")

    def fold(self, request: QueryRequest, sink: RowSink) -> FoldRead:
        plan = request.plan.logical
        self.statements.append(f"fold:{plan.metric.name}")
        accumulator = _SuppressionAccumulator(plan)
        count = 0
        for row in self.stream(plan):
            count += 1
            accumulator.add(row)
            sink.add(row)
        return FoldRead(
            rows_read=count, truncated=False, bytes_scanned=0, suppression=accumulator.result()
        )

    def fold_series(self, request: PeriodSeriesRequest, sink: RowSink) -> FoldRead:
        raise AssertionError(f"the Campaigns portfolio reads no series: {request, sink}")


def renderer(queries: EstateQueries) -> CampaignArtifactRenderer:
    """Return the real renderer over the committed dictionary, measuring through `queries`."""
    catalog, contracts = catalog_and_contracts()
    publications = StubPublications()
    return CampaignArtifactRenderer(
        measurements=CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(
                scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
                catalog=catalog,
                contracts=contracts,
                clock=lambda: NOW,
            )
        ),
        evidence=publications,
        tooling=MeasurementTooling(
            queries=queries, publications=publications, query_batch_max_concurrency=4
        ),
    )


def render_input(
    selections: tuple[CampaignMeasurementSelection, ...],
) -> CampaignArtifactRenderInput:
    """Return the bake's render input for `selections` over the fixed window."""
    expiry = datetime.max.replace(tzinfo=UTC)
    return CampaignArtifactRenderInput(
        selections=selections,
        period=PERIOD,
        prior_period=PRIOR,
        sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
        capabilities=tuple(
            CampaignSelectionCapability(f"baked:{index:06d}", expiry)
            for index in range(len(selections))
        ),
        channel_order=CHANNEL_ORDER,
    )
