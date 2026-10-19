# SPDX-License-Identifier: MIT-0
"""Verification of the issuer's own tokens presented back to the agent exchange (ADR-077 S4).

Every token is signed with an RSA key generated here, exactly as KMS would sign the domain's
signing input, and verified against the JWK the domain derives from its DER public key. Each
hostile case first shows the known-good token verifies with the same key set and clock, then
varies one attribute, so the refusal is attributable to that attribute alone.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from conftest import TokenMinter
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from domain.identity.issuance import (
    IssueTokenRequest,
    compact_jws,
    jwk_from_der_public_key,
    unsigned_token,
)
from domain.identity.issued_tokens import (
    IssuedTokenSubject,
    VerifyIssuedTokenRequest,
    verify_issued_token,
)
from domain.identity.session import IdentityError
from domain.identity.tokens import JsonWebKeySet

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

ISSUER = "https://abc123.execute-api.eu-west-1.amazonaws.com/v1"
KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
HARNESS_AUDIENCE = "adlytics-dev-harness"
ROUTE_CLIENT = "adlytics-dev-conversation-route"
SUBJECT = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EXP = int(NOW.timestamp()) + 240
"""Epoch seconds: the subject Harness token's expiry, 240 s after NOW."""


@dataclass(frozen=True)
class _Keys:
    """The issuer's key, and a foreign key that will be presented under the issuer's kid."""

    issuer: rsa.RSAPrivateKey
    foreign: rsa.RSAPrivateKey


@pytest.fixture(scope="module")
def keys() -> _Keys:
    return _Keys(
        issuer=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        foreign=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def _key_set(key: rsa.RSAPrivateKey) -> JsonWebKeySet:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return JsonWebKeySet(keys=(jwk_from_der_public_key(der, KEY_ID),))


def _claims(**overrides: Any) -> dict[str, Any]:
    """Build a Harness claim set that verifies; an override of None removes the claim."""
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HARNESS_AUDIENCE,
        "client_id": ROUTE_CLIENT,
        "act": {"sub": ROUTE_CLIENT},
        "iat": int(NOW.timestamp()) - 10,
        "exp": EXP,
        "jti": "0" * 31 + "1",
        **overrides,
    }
    return {name: value for name, value in claims.items() if value is not None}


def _signed(key: rsa.RSAPrivateKey, *, kid: str = KEY_ID, **overrides: Any) -> str:
    return jwt.encode(_claims(**overrides), key=key, algorithm="RS256", headers={"kid": kid})


def _unsigned_none(**overrides: Any) -> str:
    """`alg: none` with an empty signature, built by hand because no compliant library emits it."""
    parts = [{"alg": "none", "typ": "JWT", "kid": KEY_ID}, _claims(**overrides)]
    encoded = [
        base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode() for part in parts
    ]
    return ".".join([*encoded, ""])


def _minted_by_the_domain(key: rsa.RSAPrivateKey) -> str:
    """Mint by the route exchange's real path: the domain signing input, RS256 over it."""
    unsigned = unsigned_token(
        IssueTokenRequest(
            issuer=ISSUER,
            subject=SUBJECT,
            audience=HARNESS_AUDIENCE,
            client_id=ROUTE_CLIENT,
            key_id=KEY_ID,
            issued_at=NOW - timedelta(seconds=10),
            lifetime_seconds=250,
            token_id="0" * 31 + "1",
        )
    )
    signature = key.sign(unsigned.signing_input, padding.PKCS1v15(), hashes.SHA256())
    return compact_jws(unsigned, signature)


def _request(token: str, keys: _Keys, **overrides: Any) -> VerifyIssuedTokenRequest:
    base = VerifyIssuedTokenRequest(
        token=token,
        keys=_key_set(keys.issuer),
        expected_issuer=ISSUER,
        expected_key_id=KEY_ID,
        expected_audience=HARNESS_AUDIENCE,
        expected_client_id=ROUTE_CLIENT,
        now=NOW,
    )
    return replace(base, **overrides)


def _verify(token: str, keys: _Keys) -> IssuedTokenSubject:
    return verify_issued_token(_request(token, keys))


class TestAKnownGoodIssuedToken:
    def test_verifies_to_its_subject_and_its_own_expiry_in_epoch_seconds(self, keys: _Keys) -> None:
        assert _verify(_signed(keys.issuer), keys) == IssuedTokenSubject(
            subject=SUBJECT, expires_at=EXP, client_id=ROUTE_CLIENT
        )

    def test_the_route_exchanges_real_mint_output_verifies(self, keys: _Keys) -> None:
        verified = _verify(_minted_by_the_domain(keys.issuer), keys)

        assert verified == IssuedTokenSubject(
            subject=SUBJECT, expires_at=EXP, client_id=ROUTE_CLIENT
        )

    def test_an_iat_ahead_of_any_clock_is_not_refused_because_no_rule_reads_it(
        self, keys: _Keys
    ) -> None:
        """The minting function floors iat; skew with this one's clock must not refuse it."""
        far_future = int(datetime(2100, 1, 1, tzinfo=UTC).timestamp())

        assert _verify(_signed(keys.issuer, iat=far_future), keys).expires_at == EXP

    def test_is_valid_one_second_before_its_exp(self, keys: _Keys) -> None:
        token = _signed(keys.issuer)

        verified = verify_issued_token(_request(token, keys, now=NOW + timedelta(seconds=239)))

        assert verified.expires_at == EXP


_HOSTILE: dict[str, Callable[[_Keys, TokenMinter], str]] = {
    "wrong iss": lambda k, _m: _signed(k.issuer, iss="https://other.example/v1"),
    "iss with a trailing slash": lambda k, _m: _signed(k.issuer, iss=f"{ISSUER}/"),
    "wrong aud (the Gateway audience)": lambda k, _m: _signed(k.issuer, aud="adlytics-dev-gateway"),
    "aud as a list containing the expected audience": lambda k, _m: _signed(
        k.issuer, aud=[HARNESS_AUDIENCE]
    ),
    "no aud": lambda k, _m: _signed(k.issuer, aud=None),
    "wrong client_id (the agent client)": lambda k, _m: _signed(
        k.issuer, client_id="adlytics-dev-agent"
    ),
    "no client_id": lambda k, _m: _signed(k.issuer, client_id=None),
    "act.sub naming another client": lambda k, _m: _signed(
        k.issuer, act={"sub": "adlytics-dev-agent"}
    ),
    "no act": lambda k, _m: _signed(k.issuer, act=None),
    "alg none": lambda _k, _m: _unsigned_none(),
    "hs256 substituted": lambda _k, _m: jwt.encode(
        _claims(), key="a" * 32, algorithm="HS256", headers={"kid": KEY_ID}
    ),
    "foreign key under the published kid": lambda k, _m: _signed(k.foreign),
    "another kid": lambda k, _m: _signed(k.issuer, kid="another-kid"),
    "expired at exactly exp": lambda k, _m: _signed(k.issuer, exp=int(NOW.timestamp())),
    "expired before now": lambda k, _m: _signed(k.issuer, exp=int(NOW.timestamp()) - 1),
    "fractional exp": lambda k, _m: _signed(k.issuer, exp=EXP + 0.5),
    "boolean exp": lambda k, _m: _signed(k.issuer, exp=True),
    "no exp": lambda k, _m: _signed(k.issuer, exp=None),
    "missing sub": lambda k, _m: _signed(k.issuer, sub=None),
    "empty sub": lambda k, _m: _signed(k.issuer, sub=""),
    "cognito access token": lambda _k, m: m.token(),
    "cognito claims under the issuer key and kid": lambda k, m: jwt.encode(
        m.claims(), key=k.issuer, algorithm="RS256", headers={"kid": KEY_ID}
    ),
    "not a jwt": lambda _k, _m: "not.a.jwt",
    "empty": lambda _k, _m: "",
}


class TestEveryHostileSubjectIsRefused:
    @pytest.mark.parametrize("case", sorted(_HOSTILE))
    def test_the_known_good_token_verifies_and_the_varied_one_is_an_identity_refusal(
        self, keys: _Keys, minter: TokenMinter, case: str
    ) -> None:
        assert _verify(_signed(keys.issuer), keys).subject == SUBJECT

        with pytest.raises(IdentityError):
            _verify(_HOSTILE[case](keys, minter), keys)

    def test_a_substituted_subject_under_the_original_signature_is_refused(
        self, keys: _Keys
    ) -> None:
        token = _signed(keys.issuer)
        assert _verify(token, keys).subject == SUBJECT
        header, _payload, signature = token.split(".")
        forged_payload = _signed(keys.issuer, sub="another-subject").split(".")[1]

        with pytest.raises(IdentityError):
            _verify(f"{header}.{forged_payload}.{signature}", keys)

    def test_a_key_set_without_the_published_kid_refuses_rather_than_guessing(
        self, keys: _Keys
    ) -> None:
        token = _signed(keys.issuer)
        assert _verify(token, keys).subject == SUBJECT

        with pytest.raises(IdentityError):
            verify_issued_token(replace(_request(token, keys), keys=JsonWebKeySet(keys=())))


class TestTheRequestRefusesWhatCannotBeVerified:
    @pytest.mark.parametrize(
        "field",
        ["expected_issuer", "expected_key_id", "expected_audience", "expected_client_id"],
    )
    def test_a_blank_expectation_is_a_composition_fault_not_a_refusal(
        self, keys: _Keys, field: str
    ) -> None:
        _request("t", keys)

        with pytest.raises(ValueError, match=field) as caught:
            _request("t", keys, **{field: " "})
        assert not isinstance(caught.value, IdentityError)

    def test_a_naive_clock_is_refused(self, keys: _Keys) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _request("t", keys, now=NOW.replace(tzinfo=None))

    def test_the_presented_token_never_reaches_repr(self, keys: _Keys) -> None:
        token = _signed(keys.issuer)

        assert token not in repr(_request(token, keys))


class TestTheExchangedLifetimeEndsWithTheSubject:
    def test_a_fractional_issue_time_still_yields_exp_equal_to_the_subjects(self) -> None:
        """`unsigned_token` floors iat; the lifetime is computed against the floored second."""
        subject = IssuedTokenSubject(subject=SUBJECT, expires_at=EXP, client_id=ROUTE_CLIENT)
        issued_at = NOW + timedelta(seconds=17, microseconds=900_000)

        lifetime = subject.remaining_lifetime_seconds(issued_at)
        claims = unsigned_token(
            IssueTokenRequest(
                issuer=ISSUER,
                subject=SUBJECT,
                audience="adlytics-dev-gateway",
                client_id="adlytics-dev-agent",
                key_id=KEY_ID,
                issued_at=issued_at,
                lifetime_seconds=lifetime,
                token_id="0" * 31 + "2",
            )
        ).claims

        assert lifetime == 240 - 17  # seconds
        assert claims.exp == EXP

    def test_one_whole_second_left_is_a_one_second_lifetime(self) -> None:
        subject = IssuedTokenSubject(subject=SUBJECT, expires_at=EXP, client_id=ROUTE_CLIENT)

        assert subject.remaining_lifetime_seconds(NOW + timedelta(seconds=239.5)) == 1

    @pytest.mark.parametrize("seconds_after_now", [240, 241])
    def test_no_whole_second_left_is_an_identity_refusal(self, seconds_after_now: int) -> None:
        subject = IssuedTokenSubject(subject=SUBJECT, expires_at=EXP, client_id=ROUTE_CLIENT)
        assert subject.remaining_lifetime_seconds(NOW) == 240

        with pytest.raises(IdentityError):
            subject.remaining_lifetime_seconds(NOW + timedelta(seconds=seconds_after_now))

    def test_a_naive_issue_time_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            IssuedTokenSubject(SUBJECT, EXP, ROUTE_CLIENT).remaining_lifetime_seconds(
                NOW.replace(tzinfo=None)
            )
