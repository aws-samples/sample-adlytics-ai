# SPDX-License-Identifier: MIT-0
"""The Gateway tool caller principal, from a verified Gateway-audience token (ADR-077 §5).

Every token is signed with an RSA key generated inside the test session and verified against the
JWK the domain derives from its DER public key, exactly as a tool's KMS-read key set is built. Each
hostile case first shows the known-good token verifies with the same key set and clock, then varies
one attribute, so the refusal is attributable to that attribute alone.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import jwt
import pytest
from conftest import TokenMinter
from cryptography.hazmat.primitives import serialization
from tool_caller_support import (
    AGENT_CLIENT,
    CALLER_KEY,
    EXP,
    FOREIGN_KEY,
    GATEWAY_AUDIENCE,
    HARNESS_AUDIENCE,
    NOW,
    ROUTE_CLIENT,
    SIGNING_KEY_ID,
    SUBJECT,
    TOKEN_ISSUER,
    gateway_claims,
    gateway_token,
)

from domain.identity.caller import CallerPrincipal, verify_caller
from domain.identity.issuance import jwk_from_der_public_key
from domain.identity.issued_tokens import VerifyIssuedTokenRequest
from domain.identity.session import IdentityError
from domain.identity.tokens import JsonWebKeySet

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]


def _key_set() -> JsonWebKeySet:
    der = CALLER_KEY.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return JsonWebKeySet(keys=(jwk_from_der_public_key(der, SIGNING_KEY_ID),))


def _request(token: str, **overrides: Any) -> VerifyIssuedTokenRequest:
    base = VerifyIssuedTokenRequest(
        token=token,
        keys=_key_set(),
        expected_issuer=TOKEN_ISSUER,
        expected_key_id=SIGNING_KEY_ID,
        expected_audience=GATEWAY_AUDIENCE,
        expected_client_id=AGENT_CLIENT,
        now=NOW,
    )
    return replace(base, **overrides)


def _verify(token: str, **overrides: Any) -> CallerPrincipal:
    return verify_caller(_request(token, **overrides))


def _unsigned_none() -> str:
    """`alg: none` with an empty signature, built by hand because no compliant library emits it."""
    parts = [{"alg": "none", "typ": "JWT", "kid": SIGNING_KEY_ID}, gateway_claims()]
    encoded = [
        base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode() for part in parts
    ]
    return ".".join([*encoded, ""])


class TestAKnownGoodGatewayToken:
    def test_verifies_to_the_cognito_subject_acted_for_by_the_agent_client(self) -> None:
        assert _verify(gateway_token()) == CallerPrincipal(subject=SUBJECT, actor=AGENT_CLIENT)

    def test_is_valid_one_second_before_its_exp(self) -> None:
        principal = _verify(gateway_token(), now=NOW + timedelta(seconds=239))

        assert principal.subject == SUBJECT


_HOSTILE: dict[str, Callable[[TokenMinter], str]] = {
    "no bearer at all": lambda _m: "",
    "wrong iss": lambda _m: gateway_token(iss="https://other.example/v1"),
    "wrong aud": lambda _m: gateway_token(aud="adlytics-prod-gateway"),
    "no aud": lambda _m: gateway_token(aud=None),
    "wrong client_id": lambda _m: gateway_token(
        client_id="adlytics-dev-other", act={"sub": "adlytics-dev-other"}
    ),
    "act.sub naming another client": lambda _m: gateway_token(act={"sub": ROUTE_CLIENT}),
    "act as a bare string": lambda _m: gateway_token(act=AGENT_CLIENT),
    "no act": lambda _m: gateway_token(act=None),
    "a harness-audience token for the route": lambda _m: gateway_token(
        aud=HARNESS_AUDIENCE, client_id=ROUTE_CLIENT, act={"sub": ROUTE_CLIENT}
    ),
    "a cognito access token": lambda m: m.token(),
    "cognito claims under the issuer key and kid": lambda m: jwt.encode(
        m.claims(), key=CALLER_KEY, algorithm="RS256", headers={"kid": SIGNING_KEY_ID}
    ),
    "alg none": lambda _m: _unsigned_none(),
    "hs256 substituted": lambda _m: jwt.encode(
        gateway_claims(), key="a" * 32, algorithm="HS256", headers={"kid": SIGNING_KEY_ID}
    ),
    "a foreign key under the published kid": lambda _m: gateway_token(FOREIGN_KEY),
    "another kid": lambda _m: gateway_token(kid="another-kid"),
    "expired at exactly exp": lambda _m: gateway_token(exp=int(NOW.timestamp())),
    "expired before now": lambda _m: gateway_token(exp=int(NOW.timestamp()) - 1),
    "missing sub": lambda _m: gateway_token(sub=None),
    "not a jwt": lambda _m: "not.a.jwt",
}


class TestEveryHostileCallerIsRefused:
    @pytest.mark.parametrize("case", sorted(_HOSTILE))
    def test_the_known_good_token_verifies_and_the_varied_one_is_an_identity_refusal(
        self, minter: TokenMinter, case: str
    ) -> None:
        assert _verify(gateway_token()).subject == SUBJECT

        with pytest.raises(IdentityError):
            _verify(_HOSTILE[case](minter))

    def test_a_substituted_subject_under_the_original_signature_is_refused(self) -> None:
        token = gateway_token()
        assert _verify(token).subject == SUBJECT
        header, _payload, signature = token.split(".")
        forged_payload = gateway_token(sub="another-subject").split(".")[1]

        with pytest.raises(IdentityError):
            _verify(f"{header}.{forged_payload}.{signature}")

    def test_a_token_verified_for_the_route_client_is_not_a_caller(self) -> None:
        """The same issuer's Harness token verifies for its own audience, and still is no caller."""
        harness = gateway_token(
            aud=HARNESS_AUDIENCE, client_id=ROUTE_CLIENT, act={"sub": ROUTE_CLIENT}
        )
        assert _verify(
            harness, expected_audience=HARNESS_AUDIENCE, expected_client_id=ROUTE_CLIENT
        ) == CallerPrincipal(subject=SUBJECT, actor=ROUTE_CLIENT)

        with pytest.raises(IdentityError):
            _verify(harness)


class TestThePrincipal:
    def test_its_repr_never_carries_the_subject(self) -> None:
        principal = _verify(gateway_token())

        assert principal.subject == SUBJECT
        assert SUBJECT not in repr(principal)
        assert AGENT_CLIENT in repr(principal)

    @pytest.mark.parametrize("field", ["subject", "actor"])
    def test_a_blank_member_is_a_construction_fault(self, field: str) -> None:
        CallerPrincipal(subject=SUBJECT, actor=AGENT_CLIENT)

        with pytest.raises(ValueError, match=field):
            CallerPrincipal(**{"subject": SUBJECT, "actor": AGENT_CLIENT, field: " "})

    def test_the_expiry_is_the_tokens_own_whole_second(self) -> None:
        """The inclusive rule is shared with the agent exchange; a second earlier verifies."""
        assert int(NOW.timestamp()) + 240 == EXP  # seconds since the epoch
        _verify(gateway_token(), now=NOW + timedelta(seconds=239))

        with pytest.raises(IdentityError):
            _verify(gateway_token(), now=NOW + timedelta(seconds=240))
