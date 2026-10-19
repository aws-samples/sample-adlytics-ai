# SPDX-License-Identifier: MIT-0
"""The token service's SDK adapters against the deployed key and function (ADR-077 §3).

Run as the operator, who is not the route-exchange role and not the Conversation Function
role. That is the point: both policies are Denies naming one role by `aws:PrincipalArn`, so
the operator must be refused by those Denies even when an identity policy would allow the call.
The positive halves (GetPublicKey from the issuer's grant; Invoke from the Conversation role)
are exercised by the live identity-chain probe, not here.

Skips, never fails, when there are no credentials or the stack is not deployed.
"""

from __future__ import annotations

import re
from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from conftest import ENV_NAME

from adapters.outbound.kms.token_signer import SIGNING_UNAVAILABLE, KmsTokenSigner
from adapters.outbound.token_service.route_exchange_client import (
    ROUTE_EXCHANGE_UNAVAILABLE,
    LambdaRouteTokenExchange,
)
from domain.identity.issuance import jwk_from_der_public_key
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-077")]

SIGNING_KEY_ALIAS = f"alias/adlytics-{ENV_NAME}-token-signing"
ROUTE_EXCHANGE_PARAMETER = f"/adlytics/{ENV_NAME}/identity/token-route-exchange-function-arn"
_KEY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EXPLICIT_DENY = "explicit deny in a resource-based policy"
"""The provider's wording for a refusal by a resource policy Deny rather than a missing Allow.

[unverified] until this test first runs live against the deployed key and function.
"""


def _skip_without(action: str, error: Exception) -> None:
    pytest.skip(f"token service integration needs {action}: {type(error).__name__}")


@pytest.fixture(scope="module")
def signing_key() -> tuple[str, str]:
    """Resolve the deployed key's KeyId and region from its alias, or skip."""
    try:
        client: Any = boto3.client("kms")
        described = client.describe_key(KeyId=SIGNING_KEY_ALIAS)["KeyMetadata"]
    except ClientError as error:
        _skip_without(f"a deployed {SIGNING_KEY_ALIAS}", error)
        raise
    except BotoCoreError as error:
        _skip_without("AWS credentials", error)
        raise
    return str(described["KeyId"]), str(client.meta.region_name)


@pytest.fixture(scope="module")
def route_exchange_arn() -> tuple[str, str]:
    """Resolve the deployed route-exchange function from its published parameter, or skip."""
    try:
        client: Any = boto3.client("ssm")
        value = client.get_parameter(Name=ROUTE_EXCHANGE_PARAMETER)["Parameter"]["Value"]
    except ClientError as error:
        _skip_without(f"a published {ROUTE_EXCHANGE_PARAMETER}", error)
        raise
    except BotoCoreError as error:
        _skip_without("AWS credentials", error)
        raise
    return str(value), str(client.meta.region_name)


class TestTheSigningKey:
    def test_the_alias_names_an_rsa_sign_verify_key_whose_kid_is_its_uuid(
        self, signing_key: tuple[str, str]
    ) -> None:
        key_id, region = signing_key
        signer = KmsTokenSigner(key_id, region=region)

        jwk = jwk_from_der_public_key(signer.public_key_der(), key_id)

        assert _KEY_ID.fullmatch(key_id)
        assert (jwk["kty"], jwk["alg"], jwk["use"], jwk["kid"]) == ("RSA", "RS256", "sig", key_id)
        assert jwk["e"] == "AQAB"

    def test_the_operator_is_refused_sign_by_the_key_policy_deny(
        self, signing_key: tuple[str, str]
    ) -> None:
        """Only the route-exchange role may sign; an administrator holding kms:* may not."""
        key_id, region = signing_key
        signer = KmsTokenSigner(key_id, region=region)
        assert signer.public_key_der()

        with pytest.raises(DependencyError) as caught:
            signer.sign(b"fictional-signing-input")

        assert caught.value.code == SIGNING_UNAVAILABLE
        assert caught.value.context["service_code"] == "AccessDeniedException"
        assert caught.value.context["provider_operation"] == "Sign"
        assert _EXPLICIT_DENY in str(caught.value.context["provider_message"])


class TestTheRouteExchangeFunction:
    def test_the_operator_is_refused_invoke_by_the_resource_policy_deny(
        self, route_exchange_arn: tuple[str, str]
    ) -> None:
        """The function answers only the Conversation Function role; nothing is minted here."""
        arn, region = route_exchange_arn
        client = LambdaRouteTokenExchange(arn, region=region)

        with pytest.raises(DependencyError) as caught:
            client.exchange("not-a-cognito-token")

        assert caught.value.code == ROUTE_EXCHANGE_UNAVAILABLE
        assert caught.value.context["service_code"] == "AccessDeniedException"
        assert caught.value.context["provider_operation"] == "Invoke"
        assert _EXPLICIT_DENY in str(caught.value.context["provider_message"])
