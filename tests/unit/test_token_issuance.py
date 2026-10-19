# SPDX-License-Identifier: MIT-0
"""Token-service issuance shapes (ADR-077 §3), verified with real RSA keys made in the test.

The signature is produced locally over the domain's signing input exactly as KMS would, and
the assembled token is verified by PyJWT against the JWK the domain derives from the DER
public key. A test that stubbed either half would prove only that the stub agrees with itself.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from jwt.algorithms import RSAAlgorithm

from domain.identity.issuance import (
    DiscoveryDocument,
    IssueTokenRequest,
    compact_jws,
    jwk_from_der_public_key,
    unsigned_token,
)
from domain.identity.session import IdentityError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

ISSUER = "https://abc123.execute-api.eu-west-1.amazonaws.com/v1"
AUDIENCE = "adlytics-dev-harness"
ROUTE_CLIENT = "adlytics-dev-conversation-route"
KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
SUBJECT = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
ISSUED_AT = datetime(2026, 9, 26, 12, 0, 0, 750_000, tzinfo=UTC)
LIFETIME_SECONDS = 240


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _request(**overrides: object) -> IssueTokenRequest:
    base = IssueTokenRequest(
        issuer=ISSUER,
        subject=SUBJECT,
        audience=AUDIENCE,
        client_id=ROUTE_CLIENT,
        key_id=KEY_ID,
        issued_at=ISSUED_AT,
        lifetime_seconds=LIFETIME_SECONDS,
        token_id="0" * 31 + "1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _der(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _sign(key: rsa.RSAPrivateKey, message: bytes) -> bytes:
    """Compute what KMS Sign(MessageType=RAW, RSASSA_PKCS1_V1_5_SHA_256) returns."""
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _minted(key: rsa.RSAPrivateKey, request: IssueTokenRequest | None = None) -> str:
    unsigned = unsigned_token(request or _request())
    return compact_jws(unsigned, _sign(key, unsigned.signing_input))


def _segment(token: str, index: int) -> dict[str, object]:
    raw = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


class TestTheAssembledTokenVerifiesAgainstThePublishedKey:
    def test_pyjwt_verifies_signature_issuer_and_audience_with_the_derived_jwk(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        token = _minted(signing_key)
        jwk = jwk_from_der_public_key(_der(signing_key), KEY_ID)

        claims = jwt.decode(
            token,
            key=jwt.PyJWK.from_dict(jwk).key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )

        assert claims == {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "client_id": ROUTE_CLIENT,
            "act": {"sub": ROUTE_CLIENT},
            "iat": 1_790_424_000,
            "exp": 1_790_424_000 + LIFETIME_SECONDS,
            "jti": "0" * 31 + "1",
        }

    def test_the_header_names_rs256_jwt_and_the_signing_key_id(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        assert _segment(_minted(signing_key), 0) == {"alg": "RS256", "typ": "JWT", "kid": KEY_ID}

    def test_a_token_signed_by_another_key_does_not_verify_against_the_published_key(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        published = jwt.PyJWK.from_dict(jwk_from_der_public_key(_der(signing_key), KEY_ID))
        jwt.decode(
            _minted(signing_key),
            key=published.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            options={"verify_exp": False, "verify_iat": False},
        )

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                _minted(foreign),
                key=published.key,
                algorithms=["RS256"],
                audience=AUDIENCE,
                options={"verify_exp": False, "verify_iat": False},
            )

    def test_the_signature_covers_the_claims_so_a_substituted_subject_fails(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        header, _claims, signature = _minted(signing_key).split(".")
        other = unsigned_token(_request(subject="another-subject")).signing_input.decode()
        forged = f"{header}.{other.split('.')[1]}.{signature}"
        key = jwt.PyJWK.from_dict(jwk_from_der_public_key(_der(signing_key), KEY_ID)).key

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(forged, key=key, algorithms=["RS256"], options={"verify_aud": False})

    def test_the_signing_input_is_exactly_the_first_two_compact_segments(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        unsigned = unsigned_token(_request())
        token = compact_jws(unsigned, _sign(signing_key, unsigned.signing_input))

        assert token.rsplit(".", 1)[0].encode() == unsigned.signing_input
        assert "=" not in token


class TestClaimConstruction:
    def test_exp_is_iat_plus_the_configured_lifetime_in_whole_seconds(self) -> None:
        claims = unsigned_token(_request()).claims

        # Whole seconds since the epoch; the 0.75 s fraction of issued_at is floored, never
        # rounded up into a token that claims to be issued in the future.
        assert claims.iat == int(datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC).timestamp())
        assert claims.exp - claims.iat == LIFETIME_SECONDS

    def test_the_lifetime_is_the_requests_own_whatever_the_subject_token_had_left(self) -> None:
        """No subject expiry is a field: each exchange states its lifetime explicitly.

        The route exchange passes its configured one (ADR-077 §2); the agent exchange passes
        the subject's remaining seconds, covered in test_issued_tokens.
        """
        assert "subject_expires_at" not in IssueTokenRequest.__dataclass_fields__
        short = unsigned_token(_request(lifetime_seconds=1)).claims
        long = unsigned_token(_request(lifetime_seconds=3_600)).claims

        assert (short.exp - short.iat, long.exp - long.iat) == (1, 3_600)

    def test_the_route_client_is_both_client_id_and_the_rfc_8693_actor(self) -> None:
        claims = unsigned_token(_request()).claims.as_json()

        assert claims["client_id"] == ROUTE_CLIENT
        assert claims["act"] == {"sub": ROUTE_CLIENT}

    def test_the_claim_set_is_exactly_the_contracted_claims(self) -> None:
        assert list(unsigned_token(_request()).claims.as_json()) == [
            "iss",
            "sub",
            "aud",
            "client_id",
            "act",
            "iat",
            "exp",
            "jti",
        ]


class TestTheIssueRequestRefusesWhatCannotBeMinted:
    @pytest.mark.parametrize("field", ["subject", "audience", "client_id", "key_id", "token_id"])
    @pytest.mark.parametrize("value", ["", "   ", " padded"])
    def test_a_blank_or_padded_identity_field_is_refused(self, field: str, value: str) -> None:
        _request()

        with pytest.raises(ValueError, match=field):
            _request(**{field: value})

    @pytest.mark.parametrize(
        "issuer", ["http://abc123.example/v1", "https://abc123.example/v1/", ""]
    )
    def test_an_issuer_that_is_not_bare_https_is_refused(self, issuer: str) -> None:
        with pytest.raises(ValueError, match="issuer"):
            _request(issuer=issuer)

    @pytest.mark.parametrize("lifetime", [0, -1, True])
    def test_a_lifetime_that_is_not_a_positive_integer_is_refused(self, lifetime: int) -> None:
        with pytest.raises(ValueError, match="lifetime_seconds"):
            _request(lifetime_seconds=lifetime)

    def test_a_naive_issue_time_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _request(issued_at=ISSUED_AT.replace(tzinfo=None))

    def test_an_invalid_request_is_never_an_identity_refusal(self) -> None:
        """An IdentityError is answered `invalid_grant`; a composition fault must not be."""
        with pytest.raises(ValueError) as caught:
            _request(audience="")
        assert not isinstance(caught.value, IdentityError)

    def test_a_token_cannot_be_assembled_without_a_signature(self) -> None:
        with pytest.raises(ValueError, match="signature"):
            compact_jws(unsigned_token(_request()), b"")


class TestPublishedKey:
    def test_the_jwk_matches_an_independent_derivation_of_the_same_key(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        independent = RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)

        assert jwk_from_der_public_key(_der(signing_key), KEY_ID) == {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": KEY_ID,
            "n": independent["n"],
            "e": "AQAB",
        }

    def test_an_rsa_key_below_2048_bits_is_refused(self) -> None:
        # A-WEAK-KEY-TEST-001: the weak key is under test; see docs/security-dispositions.md
        # Deliberately weak: the refusal of a breakable key is what is under test.
        # nosemgrep: python.cryptography.security.insufficient-rsa-key-size
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505  # nosec B505

        with pytest.raises(ValueError, match="RS256 minimum"):
            jwk_from_der_public_key(_der(weak), KEY_ID)

    def test_a_non_rsa_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an RSA key"):
            jwk_from_der_public_key(_der(ec.generate_private_key(ec.SECP256R1())), KEY_ID)

    def test_bytes_that_are_not_der_are_refused(self) -> None:
        with pytest.raises(ValueError, match="DER"):
            jwk_from_der_public_key(b"not-a-key", KEY_ID)

    def test_a_published_key_needs_a_kid(self, signing_key: rsa.RSAPrivateKey) -> None:
        with pytest.raises(ValueError, match="kid"):
            jwk_from_der_public_key(_der(signing_key), " ")


class TestDiscoveryDocument:
    def test_the_document_is_exactly_the_contracted_one(self) -> None:
        assert DiscoveryDocument(ISSUER).as_json() == {
            "issuer": ISSUER,
            "jwks_uri": f"{ISSUER}/jwks.json",
            "token_endpoint": f"{ISSUER}/token",
            "grant_types_supported": ["urn:ietf:params:oauth:grant-type:token-exchange"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def test_a_trailing_slash_issuer_is_refused_rather_than_doubling_the_jwks_path(self) -> None:
        with pytest.raises(ValueError, match="issuer"):
            DiscoveryDocument(f"{ISSUER}/")
