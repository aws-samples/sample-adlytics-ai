# SPDX-License-Identifier: MIT-0
"""Read-only deployment assertion for event-driven protected evaluation-result ingress."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import boto3
import pytest

from adapters.outbound.cloudformation_stack import stack_resource_summaries

if TYPE_CHECKING:
    from types_boto3_cloudformation.client import CloudFormationClient

pytestmark = pytest.mark.integration


def _stack_resources() -> list[str]:
    profile = os.environ.get("AWS_PROFILE", "").strip()
    stack_name = os.environ.get("ADL_TEST_EVALUATION_RESULT_LINK_STACK", "").strip()
    if not profile or not stack_name:
        pytest.skip(
            "evaluation result-link integration requires AWS_PROFILE and an exact deployed "
            "stack name"
        )
    try:
        session = boto3.Session(profile_name=profile)
        session.client("sts").get_caller_identity()
    except Exception as error:  # noqa: BLE001 — pytest.skip raises: no credentials is a skip
        pytest.skip(f"evaluation result-link credentials are unavailable: {type(error).__name__}")
    client: CloudFormationClient = session.client("cloudformation")
    return [resource["ResourceType"] for resource in stack_resource_summaries(client, stack_name)]


def test_result_link_stack_contains_one_source_subscription_and_safe_projection_boundary() -> None:
    resources = _stack_resources()

    assert resources.count("AWS::Lambda::Function") == 2
    assert resources.count("AWS::SQS::Queue") == 2
    assert resources.count("AWS::Logs::SubscriptionFilter") == 2
    assert resources.count("AWS::Lambda::EventSourceMapping") == 1
