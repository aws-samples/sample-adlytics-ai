# SPDX-License-Identifier: MIT-0
"""The Harness control-plane adapter against the deployed Harness (ADR-077, amended 2026-09-26).

Read-only by design: it calls only GetHarness, on the Harness the stack publishes, so running the
integration tier can never create, patch or delete it. Create, Update and Delete are exercised by
the stack deploy itself, whose custom-resource response names any refusal.

Skips, never fails, when there are no credentials or the Harness parameter is not published.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from conftest import ENV_NAME

from adapters.outbound.agentcore.harness_control import AgentCoreHarnessControl
from ports.outbound.harness_control import HarnessStatus

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-077")]

HARNESS_ARN_PARAMETER = f"/adlytics/{ENV_NAME}/agent/harness-arn"


@pytest.fixture(scope="module")
def harness() -> tuple[str, str]:
    """Resolve the deployed Harness ARN and region from its published parameter, or skip."""
    try:
        client: Any = boto3.client("ssm")
        value = client.get_parameter(Name=HARNESS_ARN_PARAMETER)["Parameter"]["Value"]
    except ClientError as error:
        pytest.skip(f"needs a published {HARNESS_ARN_PARAMETER}: {type(error).__name__}")
    except BotoCoreError as error:
        pytest.skip(f"needs AWS credentials: {type(error).__name__}")
    return str(value), str(client.meta.region_name)


def test_get_reads_the_published_harness_by_its_id(harness: tuple[str, str]) -> None:
    arn, region = harness
    harness_id = arn.rsplit("/", 1)[-1]
    control = AgentCoreHarnessControl(boto3.client("bedrock-agentcore-control", region_name=region))

    state = control.get(harness_id)

    assert state.harness_id == harness_id
    assert state.arn == arn
    assert harness_id.startswith(f"{state.harness_name}-")
    assert isinstance(state.status, HarnessStatus)
