# SPDX-License-Identifier: MIT-0
"""The agent client secret adapter against the deployed secret (ADR-077 S4).

Since the secret's resource policy admits only the readers probe P2 observed, the operator
running this tier cannot read it, so the adapter is exercised against the real secret on its
refusal path. No value is ever returned, printed, or compared.

Skips, never fails, when there are no credentials or the secret's parameter is not published.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from conftest import ENV_NAME

from adapters.outbound.secretsmanager.client_secret import (
    CLIENT_SECRET_UNAVAILABLE,
    SecretsManagerClientSecret,
)
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-077")]

SECRET_ARN_PARAMETER = f"/adlytics/{ENV_NAME}/identity/agent-client-secret-arn"


@pytest.fixture(scope="module")
def secret_arn() -> tuple[str, str]:
    """Resolve the deployed secret's ARN and region from its published parameter, or skip."""
    try:
        client: Any = boto3.client("ssm")
        value = client.get_parameter(Name=SECRET_ARN_PARAMETER)["Parameter"]["Value"]
    except ClientError as error:
        pytest.skip(f"needs a published {SECRET_ARN_PARAMETER}: {type(error).__name__}")
    except BotoCoreError as error:
        pytest.skip(f"needs AWS credentials: {type(error).__name__}")
    return str(value), str(client.meta.region_name)


def test_the_adapter_reports_the_operators_read_as_the_resource_policy_deny(
    secret_arn: tuple[str, str],
) -> None:
    """The adapter's real Secrets Manager call, on the only path the operator can reach.

    The explicit deny arrives as the adapter's typed dependency failure with the provider's
    reason kept, and no value, since none was returned.
    """
    arn, region = secret_arn
    store = SecretsManagerClientSecret(arn, region=region)

    with pytest.raises(DependencyError) as caught:
        store.current()

    assert caught.value.code == CLIENT_SECRET_UNAVAILABLE
    assert caught.value.context["service_code"] == "AccessDeniedException"
    assert "explicit deny in a resource-based policy" in str(
        caught.value.context["provider_message"]
    )
