# SPDX-License-Identifier: MIT-0
"""The tool-side caller verifier (ADR-077 §5): the propagated bearer, the KMS key set, settings.

The verifier is the real one. Its key set is read from a local `PublicKeySource` over a key made in
the test session, never from a stubbed AWS client, and every hostile bearer is refused by the real
verification path. Each refusal first shows the known-good context verifies with the same verifier.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from aws_lambda_powertools import Logger
from tool_caller_support import (
    AGENT_CLIENT,
    CALLER_ENV,
    CALLER_KEY,
    COGNITO_ISSUER,
    COGNITO_KEY,
    COGNITO_KID,
    FOREIGN_KEY,
    MCP_CALLER_ENV,
    MCP_CLIENT,
    NOW,
    POOL_ID,
    POOL_REGION,
    PROPAGATED_HEADERS,
    SIGNING_KEY_ARN,
    SIGNING_KEY_ID,
    SUBJECT,
    LocalPoolKeys,
    LocalPublicKey,
    cognito_token,
    gateway_token,
    local_verifier,
    mcp_verifier,
)

from adapters.config import ToolCallerSettings, read_tool_caller_settings
from adapters.inbound.tool_handlers import caller
from adapters.inbound.tool_handlers.caller import (
    CALLER_KEY_SET_UNAVAILABLE,
    CALLER_UNVERIFIED,
    CALLER_UNVERIFIED_DESCRIPTION,
    CallerAdmission,
    CallerRefusalCause,
    CallerUnverifiedError,
    CallerVerifier,
    bearer_token,
)
from adapters.outbound.cognito.identity_provider import CognitoSigningKeys
from adapters.outbound.telemetry.reporting import Report
from domain.identity.caller import CallerPrincipal
from domain.shared_kernel.errors import (
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

GOOD = CallerPrincipal(subject=SUBJECT, actor=AGENT_CLIENT)


def _context(propagated: object = None, **custom: object) -> object:
    values: dict[str, object] = dict(custom)
    if propagated is not None:
        values[PROPAGATED_HEADERS] = propagated
    return SimpleNamespace(client_context=SimpleNamespace(custom=values))


def _bearer(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {gateway_token() if token is None else token}"}


_ACCEPTED: dict[str, Callable[[str], object]] = {
    "map": lambda token: {"Authorization": f"Bearer {token}"},
    "json": lambda token: json.dumps({"Authorization": f"Bearer {token}"}),
    "lowercase name": lambda token: {"authorization": f"Bearer {token}"},
    "any case": lambda token: {"AUTHORIZATION": f"bearer {token}"},
    "one-item list": lambda token: {"Authorization": [f"Bearer {token}"]},
    "with other headers": lambda token: {"x-probe": "1", "Authorization": f"Bearer {token}"},
}

_MALFORMED: dict[str, Callable[[str], object]] = {
    "basic scheme": lambda token: {"Authorization": f"Basic {token}"},
    "token scheme": lambda token: {"Authorization": f"Token {token}"},
    "no scheme": lambda token: {"Authorization": token},
    "empty token": lambda _token: {"Authorization": "Bearer "},
    "scheme only": lambda _token: {"Authorization": "Bearer"},
    "two spaces": lambda token: {"Authorization": f"Bearer  {token}"},
    "trailing part": lambda token: {"Authorization": f"Bearer {token} x"},
    "leading space": lambda token: {"Authorization": f" Bearer {token}"},
    "two names differing in case": lambda token: {
        "Authorization": f"Bearer {token}",
        "authorization": "Bearer x",
    },
    "duplicated in json": lambda token: (
        '{"Authorization": "Bearer ' + token + '", "Authorization": "Bearer x"}'
    ),
    "two-item list": lambda token: {"Authorization": [f"Bearer {token}", "Bearer x"]},
    "not a string": lambda _token: {"Authorization": 7},
    "oversized": lambda _token: {"Authorization": "Bearer " + "a" * 9_000},
}


class TestTheBearerIsReadFromThePropagatedHeaders:
    @pytest.mark.parametrize("case", sorted(_ACCEPTED))
    def test_one_authorization_header_yields_its_token(self, case: str) -> None:
        token = gateway_token()

        assert bearer_token(_context(_ACCEPTED[case](token))) == token

    @pytest.mark.parametrize(
        "context",
        [
            pytest.param(_context(), id="no-propagated-headers"),
            pytest.param(_context({"x-probe": "1"}), id="no-authorization"),
            pytest.param(_context({}), id="empty-map"),
            pytest.param(_context("not json"), id="unparseable-json"),
            pytest.param(_context(json.dumps(["Authorization", "Bearer x"])), id="json-array"),
            pytest.param(_context(7), id="not-a-map"),
            pytest.param(SimpleNamespace(client_context=None), id="no-client-context"),
            pytest.param(
                _context(None, Authorization="Bearer x"), id="authorization-outside-the-channel"
            ),
        ],
    )
    def test_no_propagated_authorization_is_a_missing_bearer(self, context: object) -> None:
        assert bearer_token(_context(_bearer())) == gateway_token()

        with pytest.raises(CallerUnverifiedError) as caught:
            bearer_token(context)
        assert caught.value.cause is CallerRefusalCause.BEARER_MISSING

    @pytest.mark.parametrize("case", sorted(_MALFORMED))
    def test_anything_but_exactly_one_bearer_value_is_malformed(self, case: str) -> None:
        token = gateway_token()
        assert bearer_token(_context(_bearer(token))) == token

        with pytest.raises(CallerUnverifiedError) as caught:
            bearer_token(_context(_MALFORMED[case](token)))
        assert caught.value.cause is CallerRefusalCause.BEARER_MALFORMED


class TestTheVerifier:
    def test_a_good_bearer_verifies_to_its_principal(self) -> None:
        assert local_verifier().verify(_context(_bearer())) == GOOD

    def test_a_bearer_the_domain_refuses_is_an_assertion_refusal(self) -> None:
        verifier = local_verifier()
        assert verifier.verify(_context(_bearer())) == GOOD

        with pytest.raises(CallerUnverifiedError) as caught:
            verifier.verify(_context(_bearer(gateway_token(aud="adlytics-dev-harness"))))
        assert caught.value.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert caught.value.dependency is None

    def test_the_key_set_is_read_once_per_verifier_and_an_unknown_kid_never_rereads_it(
        self,
    ) -> None:
        """The `kid` is pinned to the key id, so an unknown one is a refusal, not a refresh."""
        source = LocalPublicKey(CALLER_KEY)
        verifier = local_verifier(source)
        assert verifier.verify(_context(_bearer())) == GOOD
        assert verifier.verify(_context(_bearer())) == GOOD

        with pytest.raises(CallerUnverifiedError) as caught:
            verifier.verify(_context(_bearer(gateway_token(kid="rotated-key"))))
        assert caught.value.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert source.reads == 1

        assert verifier.verify(_context(_bearer())) == GOOD
        assert source.reads == 1

    def test_a_missing_bearer_reads_no_key(self) -> None:
        source = LocalPublicKey(CALLER_KEY)

        with pytest.raises(CallerUnverifiedError):
            local_verifier(source).verify(_context())
        assert source.reads == 0

    def test_an_unreadable_key_set_refuses_and_is_retried_on_the_next_call(self) -> None:
        source = LocalPublicKey(
            CALLER_KEY, failure=DependencyError(ErrorCode("TOKENSVC", 2), "unavailable")
        )
        verifier = local_verifier(source)

        with pytest.raises(CallerUnverifiedError) as caught:
            verifier.verify(_context(_bearer()))
        assert caught.value.cause is CallerRefusalCause.KEY_SET_UNAVAILABLE
        assert isinstance(caught.value.dependency, DependencyError)

        source.failure = None
        assert verifier.verify(_context(_bearer())) == GOOD
        assert source.reads == 2

    def test_a_public_key_that_cannot_verify_rs256_is_an_invariant_failure(self) -> None:
        class _NotRsa:
            def public_key_der(self) -> bytes:
                return b"not a key"

        verifier = local_verifier()
        assert verifier.verify(_context(_bearer())) == GOOD
        broken = type(verifier)(read_tool_caller_settings(CALLER_ENV), _NotRsa())

        with pytest.raises(CallerUnverifiedError) as caught:
            broken.verify(_context(_bearer()))
        assert isinstance(caught.value.dependency, InvariantError)
        assert caught.value.dependency.code == CALLER_KEY_SET_UNAVAILABLE


class TestAdmission:
    @staticmethod
    def _report(monkeypatch: pytest.MonkeyPatch) -> tuple[Report, list[BaseException]]:
        reported: list[BaseException] = []

        def capture(error: BaseException, _report: Report) -> None:
            reported.append(error)

        monkeypatch.setattr(caller, "report_error", capture)
        return Report(operation="unverified_caller", correlation_id="", logger=Logger()), reported

    def test_a_verified_caller_is_admitted_with_nothing_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, reported = self._report(monkeypatch)

        assert local_verifier().admit(_context(_bearer()), report) == CallerAdmission(GOOD)
        assert reported == []

    def test_a_refused_caller_becomes_one_reported_closed_validation_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, reported = self._report(monkeypatch)
        assert local_verifier().admit(_context(_bearer()), report).principal == GOOD

        admission = local_verifier().admit(_context({"Authorization": "Basic x"}), report)

        assert admission.principal is None
        assert isinstance(admission.refusal, ValidationError)
        assert admission.refusal.code == CALLER_UNVERIFIED
        assert str(admission.refusal.code) == "ADLY-CALLER-0001"
        assert admission.refusal.user_visible_description == CALLER_UNVERIFIED_DESCRIPTION
        assert admission.refusal.context == {"cause": "bearer_malformed"}
        assert reported == [admission.refusal]

    def test_an_unreadable_key_set_reports_the_dependency_failure_and_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, reported = self._report(monkeypatch)
        failure = DependencyError(ErrorCode("TOKENSVC", 2), "unavailable")

        admission = local_verifier(LocalPublicKey(CALLER_KEY, failure=failure)).admit(
            _context(_bearer()), report
        )

        assert admission.refusal is not None
        assert admission.refusal.context == {"cause": "key_set_unavailable"}
        assert reported == [failure]

    def test_an_admission_carries_exactly_one_outcome(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            CallerAdmission(principal=None)


MCP_GOOD = CallerPrincipal(subject=SUBJECT, actor=MCP_CLIENT)


def _refused(verifier: CallerVerifier, context: object) -> CallerUnverifiedError:
    with pytest.raises(CallerUnverifiedError) as caught:
        verifier.verify(context)
    return caught.value


@pytest.mark.covers("ADR-150")
class TestTheExternalMcpClientBranch:
    """A Quick user's own Cognito access token, verified at the tool (ADR-150).

    Every refusal sits beside a passing control on the same verifier, and asserts the exact closed
    cause the `ADLY-CALLER-0001` refusal carries.
    """

    def test_unconfigured_a_valid_pool_token_is_refused_as_any_foreign_token(self) -> None:
        verifier = local_verifier()
        assert verifier.verify(_context(_bearer())) == GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token())))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert refusal.dependency is None

    def test_configured_an_entitled_token_verifies_to_the_user_and_the_mcp_client(self) -> None:
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)

        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD
        assert keys.refreshes == 0

    def test_configured_the_token_service_branch_is_unchanged(self) -> None:
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)

        assert verifier.verify(_context(_bearer())) == GOOD
        refusal = _refused(verifier, _context(_bearer(gateway_token(aud="adlytics-dev-harness"))))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert keys.reads == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"client_id": "2publicexample345678"}, id="another-client"),
            pytest.param(
                {"iss": "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_other"},
                id="another-pool",
            ),
            pytest.param({"token_use": "id"}, id="an-id-token"),
            pytest.param({"exp": int(NOW.timestamp())}, id="expired-at-the-instant"),
            pytest.param({"exp": None}, id="no-expiry"),
            pytest.param({"quality_review_levels": ["mcp_tool_use", "retired"]}, id="bad-claim"),
        ],
    )
    def test_a_token_varying_one_attribute_is_an_assertion_refusal(
        self, overrides: dict[str, Any]
    ) -> None:
        verifier = mcp_verifier()
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token(**overrides))))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED

    def test_a_foreign_key_under_the_pool_kid_is_an_assertion_refusal(self) -> None:
        verifier = mcp_verifier()
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token(FOREIGN_KEY))))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED

    def test_an_unknown_kid_refreshes_the_key_set_exactly_once_then_refuses(self) -> None:
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token(FOREIGN_KEY, kid="unknown"))))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert keys.refreshes == 1

    def test_a_rotated_pool_key_is_found_by_the_one_refresh(self) -> None:
        """The positive control for the refresh: the pool published a key the cache predates."""
        keys = LocalPoolKeys(
            published=(("rotated-in", FOREIGN_KEY),), cached=((COGNITO_KID, COGNITO_KEY),)
        )
        verifier = mcp_verifier(keys)

        assert verifier.verify(_context(_bearer(cognito_token(FOREIGN_KEY, kid="rotated-in")))) == (
            MCP_GOOD
        )
        assert keys.refreshes == 1

    @pytest.mark.parametrize(
        "bearer", ["not-a-jwt", "a.b.c", "Zm9v.YmFy.YmF6"], ids=["opaque", "dotted", "undecodable"]
    )
    def test_a_bearer_that_is_not_a_jwt_is_an_assertion_refusal(self, bearer: str) -> None:
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(bearer)))
        assert refusal.cause is CallerRefusalCause.ASSERTION_REFUSED
        assert keys.refreshes == 0

    @pytest.mark.parametrize("case", ["basic scheme", "trailing part", "two spaces"])
    def test_a_wrong_scheme_or_embedded_whitespace_is_malformed(self, case: str) -> None:
        verifier = mcp_verifier()
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_MALFORMED[case](cognito_token())))
        assert refusal.cause is CallerRefusalCause.BEARER_MALFORMED

    def test_no_authorization_header_is_a_missing_bearer(self) -> None:
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context({"x-probe": "1"}))
        assert refusal.cause is CallerRefusalCause.BEARER_MISSING
        assert keys.reads == 1

    @pytest.mark.parametrize(
        "levels", [[], ["quality_reviewer"], ["quality_reviewer", "audit_attribution"]]
    )
    def test_a_verified_user_without_the_level_is_refused_as_entitlement_missing(
        self, levels: list[str]
    ) -> None:
        verifier = mcp_verifier()
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token(quality_review_levels=levels))))
        assert refusal.cause is CallerRefusalCause.ENTITLEMENT_MISSING
        assert refusal.dependency is None

    def test_an_absent_level_claim_is_refused_as_entitlement_missing(self) -> None:
        verifier = mcp_verifier()
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        refusal = _refused(verifier, _context(_bearer(cognito_token(quality_review_levels=None))))
        assert refusal.cause is CallerRefusalCause.ENTITLEMENT_MISSING

    def test_an_unreadable_pool_key_set_refuses_as_the_dependency_failure(self) -> None:
        failure = DependencyError(ErrorCode("COGNITO", 3), "unavailable")
        keys = LocalPoolKeys()
        verifier = mcp_verifier(keys)
        assert verifier.verify(_context(_bearer(cognito_token()))) == MCP_GOOD

        keys.failure = failure
        refusal = _refused(verifier, _context(_bearer(cognito_token())))
        assert refusal.cause is CallerRefusalCause.KEY_SET_UNAVAILABLE
        assert refusal.dependency is failure

    def test_admission_reports_the_closed_refusal_with_its_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, reported = TestAdmission._report(monkeypatch)  # pyright: ignore[reportPrivateUsage]
        verifier = mcp_verifier()
        assert verifier.admit(_context(_bearer(cognito_token())), report) == CallerAdmission(
            MCP_GOOD
        )
        assert reported == []

        admission = verifier.admit(
            _context(_bearer(cognito_token(quality_review_levels=[]))), report
        )
        assert admission.principal is None
        assert admission.refusal is not None
        assert str(admission.refusal.code) == "ADLY-CALLER-0001"
        assert admission.refusal.context == {"cause": "entitlement_missing"}
        assert reported == [admission.refusal]


class TestSettings:
    def test_the_kid_and_region_derive_from_the_key_arn(self) -> None:
        settings = read_tool_caller_settings(CALLER_ENV)

        assert settings.token_signing_key_arn == SIGNING_KEY_ARN
        assert settings.token_signing_key_id == SIGNING_KEY_ID
        # A-TEST-LITERALS-001: a Region name; see docs/security-dispositions.md
        assert settings.token_signing_key_region == "eu-west-1"  # nosec B105

    def test_every_missing_key_is_reported_at_once(self) -> None:
        with pytest.raises(InvariantError) as caught:
            read_tool_caller_settings({})

        assert caught.value.context["missing"] == sorted(CALLER_ENV)

    @pytest.mark.parametrize(
        ("name", "value", "problem"),
        [
            (
                "ADL_TOKEN_ISSUER",
                "http://abc123.execute-api.eu-west-1.amazonaws.com/v1",
                "ADL_TOKEN_ISSUER (must be https without a trailing slash)",
            ),
            (
                "ADL_TOKEN_ISSUER",
                "https://abc123.execute-api.eu-west-1.amazonaws.com/v1/",
                "ADL_TOKEN_ISSUER (must be https without a trailing slash)",
            ),
            (
                "ADL_TOKEN_SIGNING_KEY_ARN",
                SIGNING_KEY_ID,
                "ADL_TOKEN_SIGNING_KEY_ARN (must be a KMS key ARN)",
            ),
            (
                "ADL_TOKEN_SIGNING_KEY_ARN",
                "arn:aws:kms:eu-west-1:111122223333:alias/adlytics-dev-token-signing",
                "ADL_TOKEN_SIGNING_KEY_ARN (must be a KMS key ARN)",
            ),
        ],
    )
    def test_an_unusable_value_is_named_and_never_echoed(
        self, name: str, value: str, problem: str
    ) -> None:
        read_tool_caller_settings(CALLER_ENV)

        with pytest.raises(InvariantError) as caught:
            read_tool_caller_settings({**CALLER_ENV, name: value})

        assert caught.value.context["missing"] == [problem]
        assert value not in str(caught.value)
        assert value not in json.dumps(caught.value.context)

    @pytest.mark.covers("ADR-150")
    def test_without_an_mcp_client_the_settings_are_today_s(self) -> None:
        settings = read_tool_caller_settings(
            {**CALLER_ENV, "ADL_COGNITO_USER_POOL_ID": POOL_ID, "ADL_AWS_REGION": POOL_REGION}
        )

        assert settings == ToolCallerSettings(
            token_issuer=CALLER_ENV["ADL_TOKEN_ISSUER"],
            token_signing_key_arn=SIGNING_KEY_ARN,
            gateway_token_audience=CALLER_ENV["ADL_GATEWAY_TOKEN_AUDIENCE"],
            agent_client_id=AGENT_CLIENT,
        )
        assert settings.mcp_client_id == ""

    @pytest.mark.covers("ADR-150")
    def test_an_mcp_client_requires_its_pool_and_region_reporting_both_at_once(self) -> None:
        settings = read_tool_caller_settings(MCP_CALLER_ENV)
        assert settings.mcp_client_id == MCP_CLIENT
        assert settings.mcp_cognito_issuer == COGNITO_ISSUER
        assert settings.mcp_cognito_jwks_url == f"{COGNITO_ISSUER}/.well-known/jwks.json"

        with pytest.raises(InvariantError) as caught:
            read_tool_caller_settings({**CALLER_ENV, "ADL_MCP_CLIENT_ID": MCP_CLIENT})
        assert caught.value.context["missing"] == ["ADL_AWS_REGION", "ADL_COGNITO_USER_POOL_ID"]

    @pytest.mark.covers("ADR-150")
    def test_the_production_verifier_composes_the_pool_key_set_only_when_configured(self) -> None:
        unconfigured = caller.kms_caller_verifier(read_tool_caller_settings(CALLER_ENV))
        configured = caller.kms_caller_verifier(read_tool_caller_settings(MCP_CALLER_ENV))

        assert unconfigured._cognito_keys is None  # pyright: ignore[reportPrivateUsage]
        assert isinstance(configured._cognito_keys, CognitoSigningKeys)  # pyright: ignore[reportPrivateUsage]
