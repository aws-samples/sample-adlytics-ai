# SPDX-License-Identifier: MIT-0
"""Governed display labels for the dimension values a measurement answer releases."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from publication_support import StubPublications

from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.answers import (
    AnswerError,
    DimensionDisplayLabel,
    DimensionDisplayLabels,
)
from domain.measurement.planning import measure_alias
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
    EntityCatalogLookupRequest,
    EntityCatalogLookupResult,
    PublicEntityLabelOutcome,
    PublicEntityLabelRequest,
    PublicEntityLabelResolution,
    PublicEntityLabelResult,
)
from ports.outbound.query import QueryRequest, QueryResult

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-067")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
WINDOW = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 16))
SCOPE = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("amazon_retail_us")}))

type _Row = dict[str, str | int | Decimal | None]


@dataclass
class _LabelCatalog:
    """A stub of the public-label projection, recording each batch it was asked."""

    labels: dict[tuple[str, str], str | PublicEntityLabelOutcome]
    seen: list[PublicEntityLabelRequest] = field(default_factory=list[PublicEntityLabelRequest])

    def browse(
        self,
        request: EntityCatalogBrowseRequest,  # noqa: ARG002 - the port names this parameter
    ) -> EntityCatalogBrowseResult:
        raise AssertionError("label resolution must not browse the catalog.")

    def lookup(
        self,
        request: EntityCatalogLookupRequest,  # noqa: ARG002 - the port names this parameter
    ) -> EntityCatalogLookupResult:
        raise AssertionError("label resolution must not look up references.")

    def public_labels(self, request: PublicEntityLabelRequest) -> PublicEntityLabelResult:
        self.seen.append(request)
        resolutions: list[PublicEntityLabelResolution] = []
        for item in request.items:
            configured = self.labels.get(item.dimensions[0])
            if configured is None:
                resolutions.append(PublicEntityLabelResolution(PublicEntityLabelOutcome.NOT_FOUND))
            elif isinstance(configured, PublicEntityLabelOutcome):
                resolutions.append(PublicEntityLabelResolution(configured))
            else:
                resolutions.append(
                    PublicEntityLabelResolution(PublicEntityLabelOutcome.RESOLVED, configured)
                )
        return PublicEntityLabelResult(tuple(resolutions))


def _resolve(
    catalog: _LabelCatalog,
    rows: tuple[_Row, ...],
    columns: tuple[str, ...],
) -> DimensionDisplayLabels:
    return DimensionLabelResolver(catalog, SCOPE).resolve(rows=rows, columns=columns, window=WINDOW)


def test_media_and_marketplace_values_become_their_governed_names() -> None:
    catalog = _LabelCatalog(
        {
            ("channel_key", "amazon_dsp"): "Amazon DSP",
            ("marketplace_key", "amazon_us"): "United States",
        }
    )
    rows: tuple[_Row, ...] = (
        {"channel_key": "amazon_dsp", "marketplace_key": "amazon_us", "value_0": Decimal(1)},
    )

    labels = _resolve(catalog, rows, ("channel_key", "marketplace_key"))

    assert labels == DimensionDisplayLabels(
        (
            DimensionDisplayLabel("channel_key", "amazon_dsp", "Amazon DSP"),
            DimensionDisplayLabel("marketplace_key", "amazon_us", "United States"),
        )
    )


def test_each_column_is_resolved_in_its_own_batch() -> None:
    """A channel and its ad product legitimately share a display name.

    The public-label contract downgrades duplicate resolved labels in one batch to
    ambiguous, so mixing columns in one call would erase both correct labels.
    """
    catalog = _LabelCatalog(
        {
            ("channel_key", "amazon_dsp"): "Amazon DSP",
            ("ad_product_key", "amazon_dsp"): "Amazon DSP",
        }
    )
    rows: tuple[_Row, ...] = (
        {"channel_key": "amazon_dsp", "ad_product_key": "amazon_dsp", "value_0": Decimal(1)},
    )

    labels = _resolve(catalog, rows, ("channel_key", "ad_product_key"))

    assert len(catalog.seen) == 2
    assert {entry.label for entry in labels.entries} == {"Amazon DSP"}
    assert len(labels.entries) == 2


def test_an_unresolved_campaign_value_becomes_a_deterministic_placeholder() -> None:
    """A protected canonical key must never pass through to public text, and never refuse."""
    catalog = _LabelCatalog({("campaign_key", "campaign:bbb"): "Summer Launch"})
    rows: tuple[_Row, ...] = (
        {"campaign_key": "campaign:aaa", "value_0": Decimal(1)},
        {"campaign_key": "campaign:bbb", "value_0": Decimal(2)},
    )

    labels = _resolve(catalog, rows, ("campaign_key",))

    assert labels == DimensionDisplayLabels(
        (
            DimensionDisplayLabel("campaign_key", "campaign:aaa", "Unnamed campaign 1"),
            DimensionDisplayLabel("campaign_key", "campaign:bbb", "Summer Launch"),
        )
    )


@pytest.mark.covers("ADR-082")
def test_an_overlong_campaign_name_becomes_a_placeholder_and_any_wording_passes() -> None:
    """An overlong name would fail the public-text length bound post-query.

    That late failure is the exact turn-killing defect ADR-066 diagnosed, so the fallback
    happens where the label is resolved rather than where the block is released. The name's
    wording is never inspected, so a name that looks like a link is released as written.
    """
    catalog = _LabelCatalog(
        {
            ("campaign_key", "campaign:aaa"): "promo:summer",
            ("campaign_key", "campaign:bbb"): "x" * 130,
        }
    )
    rows: tuple[_Row, ...] = (
        {"campaign_key": "campaign:aaa", "value_0": Decimal(1)},
        {"campaign_key": "campaign:bbb", "value_0": Decimal(2)},
    )

    labels = _resolve(catalog, rows, ("campaign_key",))

    assert labels == DimensionDisplayLabels(
        (
            DimensionDisplayLabel("campaign_key", "campaign:aaa", "promo:summer"),
            DimensionDisplayLabel("campaign_key", "campaign:bbb", "Unnamed campaign 1"),
        )
    )


def test_an_unresolved_public_vocabulary_value_passes_through() -> None:
    """A declared media key is readable as itself, so no label means no substitution."""
    catalog = _LabelCatalog({})
    rows: tuple[_Row, ...] = ({"channel_key": "amazon_dsp", "value_0": Decimal(1)},)

    labels = _resolve(catalog, rows, ("channel_key",))

    assert labels == DimensionDisplayLabels(())


def test_only_labellable_grain_columns_are_consulted() -> None:
    """A date is already readable and an ASIN's title needs marketplace context it lacks."""
    catalog = _LabelCatalog({})
    rows: tuple[_Row, ...] = (
        {"date_key": "2026-08-10", "amazon_asin": "B0FAKE0001", "value_0": Decimal(1)},
    )

    labels = _resolve(catalog, rows, ("date", "product"))

    assert catalog.seen == []
    assert labels == DimensionDisplayLabels(())


def test_projection_replaces_only_mapped_string_cells() -> None:
    labels = DimensionDisplayLabels(
        (DimensionDisplayLabel("channel_key", "amazon_dsp", "Amazon DSP"),)
    )
    rows: tuple[_Row, ...] = (
        {"channel_key": "amazon_dsp", "date_key": "2026-08-10", "value_0": Decimal(3)},
        {"channel_key": "amazon_sponsored_brands", "date_key": None, "value_0": Decimal(4)},
    )

    projected = labels.project(rows)

    assert projected == (
        {"channel_key": "Amazon DSP", "date_key": "2026-08-10", "value_0": Decimal(3)},
        {"channel_key": "amazon_sponsored_brands", "date_key": None, "value_0": Decimal(4)},
    )


def test_duplicate_column_value_pairs_are_refused() -> None:
    """Two labels for one value could silently disagree about what a reader sees."""
    entry = DimensionDisplayLabel("channel_key", "amazon_dsp", "Amazon DSP")
    other = DimensionDisplayLabel("channel_key", "amazon_dsp", "Amazon DSP Display")

    with pytest.raises(AnswerError, match="repeat a column and value pair"):
        DimensionDisplayLabels((entry, other))


def test_a_breakdown_answer_releases_display_names_and_readable_grain_words() -> None:
    """The released claim reads governed names and customer grain words end to end.

    The claim previously read "for channel amazon_dsp ... at the channel key grain" — a
    canonical key and a physical column name in public text.
    """
    catalog = _LabelCatalog(
        {
            ("channel_key", "amazon_dsp"): "Amazon DSP",
            ("channel_key", "amazon_sponsored_brands"): "Sponsored Brands",
        }
    )
    request = MeasurementToolRequest(
        tool=MeasurementTool.METRIC_BY_GRAIN,
        catalog=load_catalog(ROOT),
        contracts=tuple(c for _, c in load_contracts(ROOT)),
        metric_name="ad_spend",
        grains=("channel",),
        window=WINDOW,
        computed_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
        correlation_id="corr-labels-1",
        filters={"marketplace_key": "amazon_us"},
    )
    rows: tuple[_Row, ...] = (
        {"channel_key": "amazon_dsp", "currency_code": "USD", measure_alias(0): Decimal("400000")},
        {
            "channel_key": "amazon_sponsored_brands",
            "currency_code": "USD",
            measure_alias(0): Decimal("900000"),
        },
    )

    response = handle(
        request,
        MeasurementTooling(
            queries=_Queries(rows),
            publications=StubPublications(),
            labels=DimensionLabelResolver(catalog, SCOPE),
        ),
    )

    grounding = response.metadata.grounding
    assert grounding is not None
    claims = [item.text for item in grounding.renderings if "was" in item.text]
    assert any("for channel Amazon DSP, at the channel grain." in text for text in claims)
    assert any("for channel Sponsored Brands" in text for text in claims)
    assert all("amazon_dsp" not in text for text in claims)
    assert all("channel key" not in text for text in claims)
    assert {row["channel_key"] for row in response.data.values} == {
        "Amazon DSP",
        "Sponsored Brands",
    }
    assert response.data.units is not None
    assert response.data.grain.dimensions == ("channel_key",)


@dataclass
class _Queries:
    """A stub of `QueryPort` returning fixed rows."""

    rows: tuple[_Row, ...]

    def run(
        self,
        request: QueryRequest,  # noqa: ARG002 - the port names this parameter
    ) -> QueryResult:
        return QueryResult(
            rows=self.rows,
            bytes_scanned=1_024,
            truncated=False,
            suppression=Suppression(rows_suppressed=False),
        )
