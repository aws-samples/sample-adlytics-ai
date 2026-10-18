# SPDX-License-Identifier: MIT-0
"""Deduplicated temporal ROAS models through the deployed semantic read path."""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime

import pytest

from adapters.config import Settings
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    handle,
)
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import (
    INTERACTIVE_WAIT,
    AthenaQueryAdapter,
    AthenaQueryTarget,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.source_pack import load_source_packs
from domain.data_contracts.contract import DataContract
from domain.narrative.shaped_response import ShapedResponse, UnitKind
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-035", "ADR-055", "G-30")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEEK = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 16))
MODELS = ("first_touch_v1", "last_touch_v1", "linear_v1")


@pytest.fixture(scope="module")
def contracts() -> tuple[DataContract, ...]:
    return tuple(contract for _, contract in load_contracts(ROOT))


@pytest.fixture(scope="module")
def tooling(settings: Settings) -> MeasurementTooling:
    return MeasurementTooling(
        queries=AthenaQueryAdapter(
            AthenaQueryTarget(
                workgroup=settings.athena_workgroup,
                database=settings.semantic_v2_current_database,
                reuse_minutes=settings.athena_result_reuse_max_age_minutes,
                wait=INTERACTIVE_WAIT,
            )
        ),
        publications=PublicationSelector(
            DynamoCurrentPublications(settings.semantic_publications_table),
            load_source_packs(ROOT),
            load_commerce_identity_bridge(ROOT),
        ),
    )


@pytest.fixture(scope="module")
def answers(
    contracts: tuple[DataContract, ...],
    tooling: MeasurementTooling,
) -> dict[str, ShapedResponse]:
    """Run each exact attribution model once against the governed deployed week."""
    return {
        model: handle(
            MeasurementToolRequest(
                tool=MeasurementTool.METRIC_BY_GRAIN,
                catalog=load_catalog(ROOT),
                contracts=contracts,
                metric_name="roas_deduplicated_incl_halo_14d",
                grains=("channel", "marketplace"),
                window=WEEK,
                computed_at=datetime.now(UTC),
                correlation_id=f"integration-deduplicated-roas-{model}",
                filters={"attribution_model_key": model},
            ),
            tooling,
        )
        for model in MODELS
    }


def test_every_exact_model_returns_deployed_ratio_rows(
    answers: dict[str, ShapedResponse],
) -> None:
    assert tuple(answers) == MODELS
    for answer in answers.values():
        assert answer.data.values
        assert answer.metadata.row_count == len(answer.data.values)
        assert answer.data.grain.dimensions == ("channel_key", "marketplace_key")
        assert answer.data.units is not None
        assert answer.data.units.kind is UnitKind.RATIO
        assert all(
            set(row) == {"channel_key", "marketplace_key", "value"} for row in answer.data.values
        )


def test_every_exact_model_carries_complete_unsuppressed_coverage(
    answers: dict[str, ShapedResponse],
) -> None:
    for answer in answers.values():
        assert answer.metadata.requested_coverage_intervals == (WEEK,)
        assert answer.metadata.actual_coverage_intervals == (WEEK,)
        assert answer.metadata.coverage_holes == ()
        assert answer.metadata.loaded_at is not None
        assert answer.metadata.loaded_at.tzinfo is not None
        assert answer.metadata.suppression.rows_suppressed is False
        assert answer.metadata.suppression.reason is None


def test_model_selection_remains_exact_and_numerator_only(
    answers: dict[str, ShapedResponse],
) -> None:
    for model, answer in answers.items():
        source_options = {
            difference.left
            for difference in answer.metadata.semantic_qualifiers
            if difference.field == "source_option"
        }
        assert source_options == {model}
        assert set(answer.metadata.facts_read) == {
            "fct_deduplicated_temporal_attribution",
            "fct_ad_exposure",
        }
