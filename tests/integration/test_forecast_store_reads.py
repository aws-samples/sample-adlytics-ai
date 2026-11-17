# SPDX-License-Identifier: MIT-0
"""The read API's readers against the deployed dev forecast table, read-only (ADR-163).

What no unit fake can establish: that the items the real publisher wrote parse through the
readers, that `META` names every closed filter, and that each product's stored list row equals
its ranked first-page entry. A failure here means the page or the tools would refuse or misread a
real generation. Dev only, synthetic data; nothing is written.
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from adapters.outbound.demand_forecast_policy import load_policy
from adapters.outbound.dynamodb.demand_forecast_store import (
    READER_CLIENT_CONFIG,
    DynamoDbForecastStore,
)
from domain.forecasting.boundary import ForecastSetV1
from domain.forecasting.list_pages import CLOSED_FILTERS
from domain.forecasting.publish import forecast_set_id
from ports.outbound.forecast_store import ReadListPageRequest

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-163")]

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")
ITEM_MAX_BYTES = 350_000
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def reader() -> tuple[DynamoDbForecastStore, str]:
    """Return the deployed table's reader and the dev set ID, or skip naming what is missing."""
    if ENV_NAME != "dev":
        pytest.skip("demand-forecast reader probes run only against dev")
    marketplace = os.environ.get("ADL_DEMAND_FORECAST_MARKETPLACE_KEY", "")
    if not marketplace:
        pytest.skip("set ADL_DEMAND_FORECAST_MARKETPLACE_KEY to the dev forecast set's key")
    try:
        parameter = boto3.client("ssm").get_parameter(
            Name=f"/adlytics/{ENV_NAME}/data/demand-forecast-table-name"
        )["Parameter"]
    except NoCredentialsError:
        pytest.skip("integration tier needs AWS credentials")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        pytest.skip(f"integration tier needs the deployed {ENV_NAME} forecast table")
    client = boto3.client("dynamodb", config=READER_CLIENT_CONFIG)
    store = DynamoDbForecastStore(str(parameter.get("Value", "")), ITEM_MAX_BYTES, client)
    declared = load_policy(ROOT / "contracts/forecasting/demand-forecast.yaml").policy.input
    forecast_set = ForecastSetV1(
        marketplace_key=marketplace,
        distributor_view=declared.distributor_view,
        selling_program=declared.selling_program,
    )
    return store, forecast_set_id(forecast_set)


def test_the_current_generation_parses_through_every_reader(
    reader: tuple[DynamoDbForecastStore, str],
) -> None:
    store, set_id = reader
    current = store.get_current(set_id)
    if current is None or current.generation_id is None:
        pytest.skip("dev has no published forecast generation")
    meta = store.read_generation(current.generation_id)
    assert meta is not None
    assert meta.generation_id == current.generation_id
    assert set(meta.pages) == set(CLOSED_FILTERS)
    page = store.read_list_page(ReadListPageRequest(current.generation_id, "all", 1))
    assert page.entries == meta.first_page
    assert meta.first_page, "a published generation has at least one product"
    first = meta.first_page[0]
    stored = store.read_asin(current.generation_id, first.display_asin)
    assert stored is not None
    assert stored.roster == first.model_copy(update={"rank": None})
