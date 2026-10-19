# SPDX-License-Identifier: MIT-0
"""Window totals and campaign artifacts built in-process from the committed generated fixtures.

The shared fixture-artifact helper of design §4.3 and plan D2. It maps every generated AMC fact
row onto its contract's canonical columns, applies the bundle's own `_mature_attribution_row`
as of `last_day` (the identity under the committed `{1, 1}`), and answers the publisher's
governed campaign queries from those rows, so the portfolio, detail and delivery artifacts come
out of the publisher's own renderer rather than a parallel builder.

Identity is simplified on purpose, and only here: a campaign's canonical key is its catalog key
on both the fact rows and the aliases, and its channel is the catalog's channel. The renderer
never reads a key's form, only that the two sides agree.
"""

from __future__ import annotations

import csv
import functools
import io
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import cast

from publication_support import StubPublications

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderer,
    CampaignArtifactRenderInput,
    CampaignArtifactRenderResult,
    retained_campaign_selections,
    retained_capabilities,
)
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
)
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.fixture_generation.catalog import (
    SyntheticCampaign,
    SyntheticEntityCatalog,
    load_synthetic_entity_catalog,
)
from adapters.jobs.fixture_generation.daily import DAILY_BOUNDS
from adapters.jobs.land_fixtures import bundle as bundle_module
from adapters.jobs.land_fixtures.bundle import (
    AMC_DAILY_DATE_COLUMNS,
    AMC_DAY_DELIVERY_FIXTURES,
    AMC_FIXTURE_CONTRACTS,
    LandedDelivery,
    _mature_attribution_row,  # pyright: ignore[reportPrivateUsage]
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.planning import measure_alias
from domain.presentation.analytical_pages import (
    campaign_platform_labels,
    presentation_dimension_labels,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
)
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
from ports.outbound.query import QueryPort, QueryRequest, QueryResult, QueryRow

ROOT = pathlib.Path(__file__).resolve().parents[2]

_KEY_TARGETS = frozenset(
    {
        "date_key",
        "campaign_key",
        "marketplace_key",
        "channel_key",
        "ad_product_key",
        "channel_group_key",
        "amazon_asin",
        "currency_code",
        "rows_suppressed",
        "suppression_reason",
        "distinct_user_count",
    }
)
"""Targets that are grain, provenance or suppression evidence, never a summed measure."""


@functools.cache
def fixture_catalog() -> SyntheticEntityCatalog:
    """Load the committed synthetic entity catalog once."""
    return load_synthetic_entity_catalog(ROOT)


def _campaigns_by_source_id() -> dict[str, SyntheticCampaign]:
    return {
        source_id: campaign
        for campaign in fixture_catalog().campaigns
        for source_id in campaign.source_aliases.values()
    }


@functools.cache
def fact_rows(fact: str) -> tuple[dict[str, object], ...]:
    """Every canonical row of one fact, mapped from each generated AMC fixture that writes it.

    Measures are Decimal after each field's `scale_divisor`; `date_key` is a date. Conversion
    rows are matured as of `last_day` by the bundle's own function.
    """
    catalog = fixture_catalog()
    shape = catalog.shape
    contracts = {contract.contract_id: contract for _, contract in load_contracts(ROOT)}
    campaigns = _campaigns_by_source_id()
    marketplaces = set(catalog.marketplace_aliases.values()) | {catalog.marketplace_key}
    found: list[dict[str, object]] = []
    for fixture, contract_id in AMC_FIXTURE_CONTRACTS.items():
        contract = contracts[contract_id]
        if not fixture.startswith("generated/") or contract.target_table != fact:
            continue
        text = (ROOT / "tests" / "fixtures" / fixture).read_text()
        raws = tuple(csv.DictReader(io.StringIO(text)))
        if fixture in AMC_DAY_DELIVERY_FIXTURES:
            # One delivery per day, and the bundle never lands a day holding a suppressed row.
            date_column = DAILY_BOUNDS[contract_id].date_column
            refused = {
                item["time_window_start"] for item in raws if item["rows_suppressed"] != "false"
            }
            raws = tuple(item for item in raws if item["time_window_start"] not in refused)
        else:
            date_column = AMC_DAILY_DATE_COLUMNS[fixture]
        for raw in raws:
            row = (
                _mature_attribution_row(dict(raw), date_column, shape.fixture_span.last_day, shape)
                if fixture == "generated/amc-attributed-conversion.csv"
                else dict(raw)
            )
            campaign = campaigns[row["campaign_id_string"]]
            canonical: dict[str, object] = {
                "date_key": date.fromisoformat(row[date_column]),
                "campaign_key": campaign.key,
                "channel_key": campaign.channel,
                "marketplace_key": catalog.marketplace_key,
            }
            for item in contract.fields:
                if item.target_column == "marketplace_key" and row[item.source_path] not in (
                    marketplaces
                ):
                    raise AssertionError(f"{fixture}: unknown marketplace {row[item.source_path]}")
                if item.target_column in _KEY_TARGETS or item.source_path not in row:
                    continue
                value = Decimal(row[item.source_path])
                canonical[item.target_column] = (
                    value if item.scale_divisor is None else value / item.scale_divisor
                )
            found.append(canonical)
    return tuple(found)


@functools.cache
def vendor_served_deliveries(fixture: str) -> dict[date, LandedDelivery]:
    """Each day's served Vendor delivery: its restating report if one is due, else its first.

    Built by the bundle itself, so the last two days carry `_vendor_initial_sales` under the
    committed `unrestated_returns`, as the lake serves them.
    """
    context = bundle_module._plan_context(ROOT, fixture_catalog().shape)  # pyright: ignore[reportPrivateUsage]
    served: dict[date, LandedDelivery] = {}
    for delivery in bundle_module._plan_fixture(context, fixture):  # pyright: ignore[reportPrivateUsage]
        day = delivery.manifest.period_start
        previous = served.get(day)
        if previous is None or delivery.manifest.retrieved_at > previous.manifest.retrieved_at:
            served[day] = delivery
    return served


def window_total(  # noqa: PLR0913, PLR0917 -- one fact, measure, campaign and window
    fact: str, measure: str, campaign: str, window: DateRange
) -> Decimal:
    """Sum one measure for one campaign over an inclusive window (zero when no row exists)."""
    return sum(
        (
            cast("Decimal", row.get(measure, Decimal(0)))
            for row in fact_rows(fact)
            if row["campaign_key"] == campaign
            and window.start <= cast("date", row["date_key"]) <= window.end
        ),
        Decimal(0),
    )


def _aggregate(  # noqa: PLR0913, PLR0917 -- the plan's five query parts
    fact: str,
    measures: tuple[str, ...],
    group_by: tuple[str, ...],
    filters: dict[str, str],
    window: DateRange,
) -> dict[tuple[str, ...], tuple[Decimal, ...]]:
    groups: dict[tuple[str, ...], list[Decimal]] = {}
    for row in fact_rows(fact):
        day = cast("date", row["date_key"])
        if not window.start <= day <= window.end:
            continue
        if any(str(row.get(name)) != value for name, value in filters.items()):
            continue
        key = tuple(day.isoformat() if name == "date_key" else str(row[name]) for name in group_by)
        sums = groups.setdefault(key, [Decimal(0)] * len(measures))
        for index, measure in enumerate(measures):
            sums[index] += cast("Decimal", row.get(measure, Decimal(0)))
    return {key: tuple(values) for key, values in groups.items()}


@dataclass
class FixtureQueries:
    """A query port answering governed campaign plans from the fixtures' canonical rows.

    A single-fact plan returns one row per group present; a drill-across returns the full join
    of its two sides, with a missing side's aggregate null, as the Athena composition does.
    """

    calls: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.calls.append(request)
        plan = request.plan.logical
        carried = dict.fromkeys(plan.carried_columns, "USD")
        join = plan.join
        rows: list[dict[str, object]] = []
        if join is None:
            for key, values in sorted(
                _aggregate(
                    plan.fact, plan.measures, plan.group_by, dict(plan.filters), plan.window
                ).items()
            ):
                rows.append(
                    {
                        **dict(zip(plan.group_by, key, strict=True)),
                        **carried,
                        **{measure_alias(index): value for index, value in enumerate(values)},
                    }
                )
        else:
            numerator = _aggregate(
                plan.fact, plan.measures[:1], plan.group_by, dict(plan.filters), plan.window
            )
            denominator = _aggregate(
                join.fact, (join.measure,), plan.group_by, dict(join.filters), plan.window
            )
            for key in sorted(set(numerator) | set(denominator)):
                rows.append(
                    {
                        **dict(zip(plan.group_by, key, strict=True)),
                        **carried,
                        measure_alias(0): numerator[key][0] if key in numerator else None,
                        measure_alias(1): denominator[key][0] if key in denominator else None,
                    }
                )
        return QueryResult(
            cast("tuple[QueryRow, ...]", tuple(rows)), 1024, False, Suppression(False)
        )


def campaign_aliases(channel: str | None = None) -> tuple[EntityAlias, ...]:
    """Governed-shape aliases for the visible campaigns, optionally on one channel."""
    catalog = fixture_catalog()
    scope = load_commerce_identity_bridge(ROOT).commerce_scope
    aliases: list[EntityAlias] = []
    for campaign in catalog.campaigns:
        if channel is not None and campaign.channel != channel:
            continue
        aliases.append(
            EntityAlias(
                kind=EntityKind.CAMPAIGN,
                key=CampaignKey(campaign.key, scope, PlatformKey(campaign.platform)),
                commerce_scope=scope,
                labels=tuple(
                    EffectiveLabel(name.value, name.valid_from, name.valid_to)
                    for name in campaign.names
                ),
                source_identifiers=tuple(
                    SourceIdentifier(namespace, value)
                    for namespace, value in campaign.source_aliases.items()
                ),
                evidence=MappingEvidence("fixture-artifacts-support", 1),
                valid_from=fixture_catalog().shape.fixture_span.first_day,
                platform=PlatformKey(campaign.platform),
                channel=ChannelKey(campaign.channel),
                marketplace=MarketplaceKey(campaign.marketplace),
            )
        )
    return tuple(aliases)


def render_campaign_artifacts(  # noqa: PLR0913 -- two windows and three keyword options
    period: DateRange,
    prior: DateRange,
    *,
    channel: str | None = "amazon_dsp",
    evidence: PresentationEvidenceInspectionPort | None = None,
    queries: QueryPort | None = None,
) -> CampaignArtifactRenderResult | None:
    """Render the portfolio, detail and delivery artifacts through the publisher's renderer.

    `evidence` replaces the binding stub with a caller's inspection port, such as the real
    `PublicationSelector`, so pack binding runs as it does in the deployed bake.
    `queries` replaces the fixture query port, such as one returning a writer split.
    """
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    bridge = load_commerce_identity_bridge(ROOT)
    taxonomy = load_media_taxonomy(ROOT)
    scope = AuthorizedAnalyticalScope(frozenset({bridge.commerce_scope}))
    labels = (
        *presentation_dimension_labels(taxonomy, bridge),
        *campaign_platform_labels(taxonomy),
    )
    selections = retained_campaign_selections(campaign_aliases(channel), period, labels)
    now = datetime.combine(period.end, time.max, tzinfo=UTC)
    return CampaignArtifactRenderer(
        measurements=CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(scope, load_catalog(ROOT), contracts, lambda: now)
        ),
        evidence=StubPublications() if evidence is None else evidence,
        tooling=MeasurementTooling(
            queries=FixtureQueries() if queries is None else queries,
            publications=StubPublications(),
            query_batch_max_concurrency=8,
        ),
    ).render(
        CampaignArtifactRenderInput(
            selections=selections,
            period=period,
            prior_period=prior,
            sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
            capabilities=retained_capabilities(selections),
        )
    )
