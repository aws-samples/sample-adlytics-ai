# SPDX-License-Identifier: MIT-0
"""The operator directory against the deployed user pool: the only coverage of its SDK binding.

A never-issued subject resolves as not found, and a provisioned synthetic persona resolves to
its own username, which is its email address. Values are asserted, never printed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from probe_identity import probe_key

from adapters.config import Settings
from adapters.outbound.cognito.operator_directory import (
    CognitoOperatorDirectory,
    OperatorDirectoryRequest,
)
from adapters.outbound.telemetry.reporting import Report
from domain.identity.operator_directory import OperatorEmailStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from validation.cognito_personas import persona_username

if TYPE_CHECKING:
    from types_boto3_cognito_idp.client import CognitoIdentityProviderClient

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-069", "ADR-014")]

_PERSONA = "probe-a"
_CORRELATION_ID = "5d1c8f0e-2a4b-4c6d-8e9f-0a1b2c3d4e5f"


def _report() -> Report:
    return Report(
        operation="prompt_admin.versions",
        correlation_id=_CORRELATION_ID,
        logger=Logger(service="adlytics-integration-operator-directory"),
    )


@pytest.fixture
def directory(settings: Settings) -> CognitoOperatorDirectory:
    return CognitoOperatorDirectory(
        OperatorDirectoryRequest(settings.cognito_user_pool_id, settings.aws_region, 2),
        _report,
    )


def test_a_never_issued_subject_is_not_found(directory: CognitoOperatorDirectory) -> None:
    subject = probe_key("never-issued-operator")

    resolutions = directory.email_addresses(frozenset({subject}))

    assert resolutions[subject].status is OperatorEmailStatus.NOT_FOUND


def _persona_subject(settings: Settings, username: str) -> str:
    """Read the persona's subject with the test's own credentials, or skip when it is absent."""
    client: CognitoIdentityProviderClient = boto3.client(
        "cognito-idp", region_name=settings.aws_region
    )
    pool = settings.cognito_user_pool_id
    try:
        user = client.admin_get_user(UserPoolId=pool, Username=username)
        return user["Username"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "UserNotFoundException":
            pytest.skip(f"synthetic persona {_PERSONA} is not provisioned")
    # Email is the pool's username attribute; if the alias lookup is refused, filter instead.
    users = client.list_users(UserPoolId=pool, Filter=f'email = "{username}"', Limit=1)["Users"]
    if not users:
        pytest.skip(f"synthetic persona {_PERSONA} is not provisioned")
    username_value = users[0].get("Username")
    if username_value is None:
        pytest.skip(f"synthetic persona {_PERSONA} has no username")
    return username_value


def test_a_provisioned_persona_resolves_to_its_own_email(
    directory: CognitoOperatorDirectory, settings: Settings
) -> None:
    username = persona_username(_PERSONA)
    subject = _persona_subject(settings, username)

    resolution = directory.email_addresses(frozenset({subject}))[subject]

    assert resolution.status is OperatorEmailStatus.RESOLVED
    assert resolution.email == username
