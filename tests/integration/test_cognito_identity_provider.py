# SPDX-License-Identifier: MIT-0
"""The Cognito adapter against a real user pool.

These cover what a fake cannot: that the JWKS document has the shape our verifier
expects, that the token endpoint accepts our confidential-client header, and that
a rejected request translates to a DependencyError rather than escaping as a
urllib exception.

Deliberately no test needs a signed-in user. A rejected code exercises the same
request construction, authentication header and error path as a real exchange,
which is most of the integration risk without any fixture user to maintain.
"""

from __future__ import annotations

import boto3
import pytest

from adapters.config import Settings
from adapters.outbound.cognito.identity_provider import (
    CognitoIdentityProvider,
    CognitoSigningKeys,
)
from domain.identity.tokens import REQUIRED_ALGORITHM
from domain.shared_kernel.errors import DependencyError
from ports.outbound.identity_provider import ExchangeCodeRequest, IdentityProviderPort

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-007", "ADR-014")]


@pytest.fixture
def provider(settings: Settings) -> CognitoIdentityProvider:
    return CognitoIdentityProvider(settings)


class TestPortConformance:
    def test_the_adapter_satisfies_the_port(self, provider: CognitoIdentityProvider) -> None:
        assert isinstance(provider, IdentityProviderPort)


class TestSigningKeys:
    def test_the_pool_publishes_usable_keys(self, provider: CognitoIdentityProvider) -> None:
        assert provider.signing_keys().keys

    def test_every_key_carries_a_kid(self, provider: CognitoIdentityProvider) -> None:
        # Selection is by kid and nothing else, so a key without one is unusable.
        assert all(key.get("kid") for key in provider.signing_keys().keys)

    def test_every_key_is_rs256(self, provider: CognitoIdentityProvider) -> None:
        keys = provider.signing_keys().keys
        assert all(key.get("alg") == REQUIRED_ALGORITHM for key in keys)

    def test_the_pool_has_two_key_pairs(self, provider: CognitoIdentityProvider) -> None:
        """One signs access tokens, the other signs ID tokens.

        Documented by AWS and easy to forget, because a verifier that pins "the"
        pool key works until it meets a token of the other kind.
        """
        assert len(provider.signing_keys().keys) >= 2

    def test_a_second_call_returns_the_same_set(self, provider: CognitoIdentityProvider) -> None:
        assert provider.signing_keys() == provider.signing_keys()

    def test_a_forced_refresh_still_returns_keys(self, provider: CognitoIdentityProvider) -> None:
        # The path taken after an unrecognised kid. It must reach the pool rather
        # than returning the cached set it was called to bypass.
        assert provider.refresh_signing_keys().keys

    @pytest.mark.covers("ADR-077")
    def test_the_settings_free_key_source_serves_the_same_pool_keys(
        self, provider: CognitoIdentityProvider, settings: Settings
    ) -> None:
        """The route exchange verifies with this source; it must see exactly the route's keys."""
        source = CognitoSigningKeys(settings.cognito_jwks_url)

        assert source.signing_keys() == provider.signing_keys()
        assert source.refresh_signing_keys() == provider.signing_keys()


class TestDeployedPoolPolicy:
    def test_self_signup_is_disabled_and_recovery_is_email_only(
        self, deployed_env: dict[str, str]
    ) -> None:
        client = boto3.client("cognito-idp")
        response = client.describe_user_pool(UserPoolId=deployed_env["ADL_COGNITO_USER_POOL_ID"])
        pool = response["UserPool"]
        admin = pool.get("AdminCreateUserConfig", {})
        recovery = pool.get("AccountRecoverySetting", {})

        assert admin.get("AllowAdminCreateUserOnly") is True
        assert recovery.get("RecoveryMechanisms") == [{"Priority": 1, "Name": "verified_email"}]


class TestRejectedRequests:
    def test_a_spent_code_becomes_a_dependency_error(
        self, provider: CognitoIdentityProvider, settings: Settings
    ) -> None:
        # Exercises URL construction, the Basic header and 4xx translation. A
        # urllib.error.HTTPError escaping here would reach the domain.
        with pytest.raises(DependencyError) as caught:
            provider.exchange_code(
                ExchangeCodeRequest(
                    code="not-a-real-authorization-code",
                    redirect_uri=settings.callback_redirect_uri,
                    code_verifier="a" * 43,
                )
            )
        assert caught.value.alarms is True

    def test_an_invalid_refresh_token_becomes_a_dependency_error(
        self, provider: CognitoIdentityProvider
    ) -> None:
        with pytest.raises(DependencyError):
            provider.refresh("not-a-real-refresh-token")

    def test_no_cognito_detail_reaches_a_caller(self, provider: CognitoIdentityProvider) -> None:
        """The pool ID and client ID must not appear in a user-visible message.

        Both are in the request. A generic user-facing description is what keeps
        them in the logs where they belong.
        """
        with pytest.raises(DependencyError) as caught:
            provider.refresh("not-a-real-refresh-token")
        visible = caught.value.user_visible_description
        assert "cognito" not in visible.lower()
        assert "client" not in visible.lower()
