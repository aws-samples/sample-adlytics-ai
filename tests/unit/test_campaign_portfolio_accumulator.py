"""Campaigns' compact record renders exactly what the held compares render (ADR-152).

The bake folds its 22 compares into one record per campaign and its trend into per-filter daily
sums. A difference from the held answers would publish a wrong figure, row order or roster. So
both shapes measure one fictional estate through one query port, and every filter's every
ordering and segment must encode to the same bytes.
"""

from __future__ import annotations

import pytest
from campaign_estate_support import (
    PERIOD,
    PRIOR,
    EstateQueries,
    estate,
    render_input,
    renderer,
)
from publication_support import StubPublications

from adapters.campaign_artifact_renderer import filter_subsets
from adapters.campaign_measurement import CampaignPortfolioMeasurementInput
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    execute_presentation_batch,
)
from domain.measurement.folds import KeyedColumn
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_payload,
    campaign_portfolio_segment,
)
from domain.presentation.preset_artifacts import canonical_preset_artifact_json
from domain.shared_kernel.errors import InvariantError
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import RowCeiling

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-152", "ADR-071")]


def _held(queries: EstateQueries) -> tuple[CampaignPortfolioArtifact, ...] | None:
    """Render the request-time shape: held compares and the day-chunked trend, by filter."""
    subject = renderer(queries)
    request = render_input(queries.selections)
    batch = subject.measurements.portfolio_batch(
        CampaignPortfolioMeasurementInput(request.selections, PERIOD, PRIOR, request.sort)
    )
    prepared = batch.prepared
    inspection = StubPublications().inspect(
        PresentationEvidenceInspectionRequest(
            plans=tuple(
                PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                for plan in prepared.plans
            ),
            contracts=prepared.contracts,
            now=prepared.now,
        )
    )
    executed = execute_presentation_batch(
        prepared, inspection, MeasurementExecutionTooling(queries=queries)
    )
    return subject.reduce_filters(batch, executed, request)


def _baked(queries: EstateQueries) -> tuple[CampaignPortfolioArtifact, ...] | None:
    """Render the bake: `render_portfolio`, whose batch folds into the compact record."""
    rendered = renderer(queries).render_portfolio(render_input(queries.selections))
    if rendered.portfolio is None:
        return None
    return (rendered.portfolio, *rendered.siblings)


def _encoded(artifact: CampaignPortfolioArtifact) -> list[str]:
    """Every segment of every ordering, as the bake writes its bytes."""
    assert artifact.pagination is not None
    encoded = [canonical_preset_artifact_json(campaign_portfolio_artifact_payload(artifact))]
    for field in CampaignPortfolioSortField:
        for direction in ("asc", "desc"):
            sort = CampaignPortfolioSort(field, direction)
            for index in range(artifact.pagination.segment_count):
                segment = campaign_portfolio_segment(artifact, sort, index)
                assert segment is not None
                encoded.append(
                    canonical_preset_artifact_json(campaign_portfolio_artifact_payload(segment))
                )
    return encoded


def test_the_compact_record_renders_the_held_compares_bytes_on_a_1000_campaign_estate() -> None:
    selections = estate(1_000)
    held_queries, baked_queries = EstateQueries(selections), EstateQueries(selections)
    held, baked = _held(held_queries), _baked(baked_queries)
    assert held is not None and baked is not None
    # All channels, Sponsored ads and the five channels, in option order.
    assert [item.filter_id for item in baked] == [item.filter_id for item in held]
    assert len(baked) == 7
    for expected, actual in zip(held, baked, strict=True):
        assert _encoded(actual) == _encoded(expected), actual.filter_id
        assert actual.roster == expected.roster
    everything = baked[0]
    assert everything.total_count == 1_000
    # The population is every authorized campaign; the reads' unauthorized keys reach nothing.
    assert {row.signature.effective_name for row in everything.rows} <= {
        item.signature.effective_name for item in selections
    }
    # The held shape chunks the 30-day trend; the bake reads each operand once, folded.
    assert sum(item.startswith("run:") for item in held_queries.statements) > 22
    assert not [item for item in baked_queries.statements if item.startswith("run:")]


def test_a_negative_operand_the_held_parse_refuses_refuses_the_compact_record_too() -> None:
    """Paired with the parity above: a negative operand withholds both shapes' portfolio."""
    selections = estate(40)
    assert _held(EstateQueries(selections)) is not None
    assert _baked(EstateQueries(selections)) is not None
    assert _held(EstateQueries(selections, negative="ad_spend")) is None
    assert _baked(EstateQueries(selections, negative="ad_spend")) is None


def test_a_currency_mix_is_the_same_defect_in_both_shapes() -> None:
    """One currency rule (`CurrencyAccumulator`) reads a held and a folded answer alike."""
    selections = estate(40)
    codes: list[str] = []
    for render in (_held, _baked):
        with pytest.raises(InvariantError) as raised:
            render(EstateQueries(selections, mixed_currency="ad_spend"))
        codes.append(str(raised.value.code))
    assert codes == ["ADLY-MEASURE-0003", "ADLY-MEASURE-0003"]


def test_the_bake_reads_have_no_row_limit_and_share_one_record() -> None:
    """26 reads, each folded with no row `LIMIT`; the 22 compares write one store."""
    selections = estate(40)
    subject = renderer(EstateQueries(selections))
    request = render_input(selections)
    batch = subject.measurements.portfolio_batch(
        CampaignPortfolioMeasurementInput(
            selections, PERIOD, PRIOR, request.sort, bake_subsets=filter_subsets(request)
        )
    )
    requests = tuple(operation.request for operation in batch.prepared.operations)
    assert len(batch.prepared.plans) == 26
    assert {item.row_ceiling for item in requests} == {RowCeiling.BAKE_GROUPED}
    columns = [item.reduction for item in requests if isinstance(item.reduction, KeyedColumn)]
    assert len(columns) == 22
    assert {id(item.store) for item in columns} == {id(batch.figures)}
