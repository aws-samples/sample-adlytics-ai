# SPDX-License-Identifier: MIT-0
"""Token service entry points (ADR-077 §3): the route exchange and the public issuer.

The route exchange verifies real RS256 Cognito-shaped tokens signed by a key made in the test
and signs through an injected local signer, so every hostile subject is refused by the real
verifier, never by a stub of it. Each refusal first asserts the known-good request succeeds,
then varies one attribute, and shows the refusal signed nothing.

The issuer's agent exchange (`POST /token`, ADR-077 S4) is exercised the same way: subjects are
Harness tokens signed by the test's issuer key, the key set is derived from the local signer's
public key, and the client secret is held by a local port, so no AWS client is stubbed.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import re
import sys
import urllib.parse
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import timedelta
from types import ModuleType
from typing import Any, cast

import jwt
import pytest
from conftest import CLIENT_ID, KID, NOW, SUBJECT, TokenMinter
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fictional_tokens import fictional_jwt
from pydantic import SecretStr

from adapters.config import token_service_settings, tracing_settings
from domain.identity.issuance import jwk_from_der_public_key
from domain.identity.tokens import JsonWebKeySet
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.client_secret import ClientSecret

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

TOKEN_ISSUER = "https://abc123.execute-api.eu-west-1.amazonaws.com/v1"
SIGNING_KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
HARNESS_AUDIENCE = "adlytics-dev-harness"
ROUTE_CLIENT = "adlytics-dev-conversation-route"
LIFETIME_SECONDS = 240
GATEWAY_AUDIENCE = "adlytics-dev-gateway"
AGENT_CLIENT = "adlytics-dev-agent"
# pragma: allowlist nextline secret (A-TEST-LITERALS-001)
AGENT_SECRET = "Fictional-Agent-Secret-For-Tests"
INVALID_GRANT = {"error": "invalid_grant"}
_UNAVAILABLE = ErrorCode("TEST", 1)

_ENV = {
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_EC2_METADATA_DISABLED": "true",
    "ADL_ENVIRONMENT": "dev",
    # The conftest pool issuer is in eu-west-1 with this pool id.
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": CLIENT_ID,
    "ADL_TOKEN_ISSUER": TOKEN_ISSUER,
    "ADL_TOKEN_SIGNING_KEY_ID": SIGNING_KEY_ID,
    "ADL_HARNESS_TOKEN_AUDIENCE": HARNESS_AUDIENCE,
    "ADL_ROUTE_CLIENT_ID": ROUTE_CLIENT,
    "ADL_HARNESS_TOKEN_LIFETIME_SECONDS": str(LIFETIME_SECONDS),
    "ADL_GATEWAY_TOKEN_AUDIENCE": GATEWAY_AUDIENCE,
    "ADL_AGENT_CLIENT_ID": AGENT_CLIENT,
    "ADL_AGENT_CLIENT_SECRET_ID": (
        "arn:aws:secretsmanager:eu-west-1:111122223333:secret:"
        "adlytics/dev/identity/agent-client-secret-AbCdEf"
    ),
}


@dataclass
class _LocalSigner:
    """TokenSigningPort over a key held in the test: what KMS computes, without KMS."""

    key: rsa.RSAPrivateKey
    signed: list[bytes] = field(default_factory=list[bytes])
    public_key_reads: int = 0
    failure: Exception | None = None

    def sign(self, message: bytes) -> bytes:
        if self.failure is not None:
            raise self.failure
        self.signed.append(message)
        return self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())

    def public_key_der(self) -> bytes:
        self.public_key_reads += 1
        if self.failure is not None:
            raise self.failure
        return self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )


@dataclass
class _KeySource:
    """The Cognito key set, as the cached key source returns it, built from the test key."""

    current: JsonWebKeySet
    refreshed: JsonWebKeySet
    refreshes: int = 0
    failure: Exception | None = None

    def signing_keys(self) -> JsonWebKeySet:
        if self.failure is not None:
            raise self.failure
        return self.current

    def refresh_signing_keys(self) -> JsonWebKeySet:
        self.refreshes += 1
        return self.refreshed


@dataclass
class _LocalClientSecret:
    """ClientSecretPort over a value held in the test: the store, and the cache in front of it."""

    stored: str = AGENT_SECRET
    cached: str | None = None
    reads: int = 0
    failure: Exception | None = None

    def current(self) -> ClientSecret:
        if self.cached is None:
            return self.refreshed()
        return ClientSecret(SecretStr(self.cached))

    def refreshed(self) -> ClientSecret:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        self.cached = self.stored
        return ClientSecret(SecretStr(self.cached))


@pytest.fixture(scope="module")
def harness_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _import(monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    token_service_settings.cache_clear()
    tracing_settings.cache_clear()
    sys.modules.pop(name, None)
    return cast("Any", importlib.import_module(name))


@pytest.fixture
def exchange(
    monkeypatch: pytest.MonkeyPatch, minter: TokenMinter, harness_key: rsa.RSAPrivateKey
) -> Generator[Any]:
    name = "adapters.inbound.token_service.route_exchange"
    module = _import(monkeypatch, name)
    monkeypatch.setattr(module, "SIGNER", _LocalSigner(harness_key))
    monkeypatch.setattr(module, "KEYS", _KeySource(minter.jwks(), minter.jwks()))
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    yield module
    sys.modules.pop(name, None)
    token_service_settings.cache_clear()
    tracing_settings.cache_clear()


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch, harness_key: rsa.RSAPrivateKey) -> Generator[Any]:
    name = "adapters.inbound.token_service.issuer"
    module = _import(monkeypatch, name)
    monkeypatch.setattr(module, "SIGNER", _LocalSigner(harness_key))
    monkeypatch.setattr(module, "CLIENT_SECRET", _LocalClientSecret())
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    module._published_jwk.cache_clear()
    yield module
    sys.modules.pop(name, None)
    token_service_settings.cache_clear()
    tracing_settings.cache_clear()


def _call(module: ModuleType, event: object) -> dict[str, Any]:
    handler = cast("Callable[[object, object], dict[str, Any]]", inspect.unwrap(module.handler))
    return handler(event, object())


def _published_key(harness_key: rsa.RSAPrivateKey) -> Any:
    der = harness_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return jwt.PyJWK.from_dict(jwk_from_der_public_key(der, SIGNING_KEY_ID)).key


def _harness_claims(token: str, harness_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """Verify the minted token as the Harness authorizer would: signature, iss, aud, alg."""
    return jwt.decode(
        token,
        key=_published_key(harness_key),
        algorithms=["RS256"],
        audience=HARNESS_AUDIENCE,
        issuer=TOKEN_ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )


class TestRouteExchangeMintsForAVerifiedSubject:
    def test_the_response_is_exactly_the_contracted_success_document(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        response = _call(exchange, {"subject_token": minter.token()})

        assert set(response) == {"access_token", "token_type", "expires_in"}
        assert response["token_type"] == "Bearer"
        # Seconds: the configured single-turn lifetime, not the subject's remaining life.
        assert response["expires_in"] == LIFETIME_SECONDS

    def test_the_minted_token_verifies_as_a_harness_token_bound_to_the_subject(
        self, exchange: Any, minter: TokenMinter, harness_key: rsa.RSAPrivateKey
    ) -> None:
        response = _call(exchange, {"subject_token": minter.token()})
        claims = _harness_claims(response["access_token"], harness_key)

        assert jwt.get_unverified_header(response["access_token"]) == {
            "alg": "RS256",
            "typ": "JWT",
            "kid": SIGNING_KEY_ID,
        }
        assert {k: v for k, v in claims.items() if k != "jti"} == {
            "iss": TOKEN_ISSUER,
            "sub": SUBJECT,
            "aud": HARNESS_AUDIENCE,
            "client_id": ROUTE_CLIENT,
            "act": {"sub": ROUTE_CLIENT},
            "iat": int(NOW.timestamp()),
            "exp": int(NOW.timestamp()) + LIFETIME_SECONDS,
        }
        assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])

    @pytest.mark.parametrize("subject_seconds_left", [10, 900])
    def test_the_harness_lifetime_is_independent_of_the_subject_expiry(
        self, exchange: Any, minter: TokenMinter, subject_seconds_left: int
    ) -> None:
        subject = minter.token(exp=int(NOW.timestamp()) + subject_seconds_left)
        harness_key = cast("_LocalSigner", exchange.SIGNER).key

        claims = _harness_claims(
            _call(exchange, {"subject_token": subject})["access_token"], harness_key
        )

        assert claims["exp"] - claims["iat"] == LIFETIME_SECONDS

    def test_every_exchange_mints_a_distinct_token_id(
        self, exchange: Any, minter: TokenMinter, harness_key: rsa.RSAPrivateKey
    ) -> None:
        subject = minter.token()
        first = _harness_claims(
            _call(exchange, {"subject_token": subject})["access_token"], harness_key
        )
        second = _harness_claims(
            _call(exchange, {"subject_token": subject})["access_token"], harness_key
        )

        assert first["jti"] != second["jti"]

    def test_the_minted_token_is_signed_by_the_published_issuer_key_set(
        self, exchange: Any, issuer: Any, minter: TokenMinter
    ) -> None:
        """What the Harness authorizer fetches is what verifies what the route presents."""
        minted = _call(exchange, {"subject_token": minter.token()})["access_token"]
        published = json.loads(_call(issuer, _get("/jwks.json"))["body"])

        key = jwt.PyJWK.from_dict(published["keys"][0]).key
        claims = jwt.decode(
            minted,
            key=key,
            algorithms=["RS256"],
            audience=HARNESS_AUDIENCE,
            issuer=TOKEN_ISSUER,
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["sub"] == SUBJECT
        assert published["keys"][0]["kid"] == jwt.get_unverified_header(minted)["kid"]


_HOSTILE_SUBJECTS: dict[str, Callable[[TokenMinter], str]] = {
    "expired at exactly exp": lambda m: m.token(exp=int(NOW.timestamp())),
    "another app client": lambda m: m.token(client_id="another-client"),
    "another user pool": lambda m: m.token(
        iss="https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_other"
    ),
    "an id token": lambda m: m.token(token_use="id"),
    "no subject": lambda m: m.token(sub=None),
    "alg none": lambda m: m.unsigned_token(),
    "hs256 substituted": lambda m: m.token(algorithm="HS256"),
    "signed by a foreign key": lambda m: m.token(wrong_key=True),
    "not a jwt": lambda _m: "not.a.jwt",
}


class TestRouteExchangeRefusesEveryHostileSubject:
    @pytest.mark.parametrize("case", sorted(_HOSTILE_SUBJECTS))
    def test_the_refusal_is_exactly_invalid_grant_and_signs_nothing(
        self, exchange: Any, minter: TokenMinter, case: str
    ) -> None:
        assert set(_call(exchange, {"subject_token": minter.token()})) == {
            "access_token",
            "token_type",
            "expires_in",
        }
        signer = cast("_LocalSigner", exchange.SIGNER)
        signed_before = len(signer.signed)

        response = _call(exchange, {"subject_token": _HOSTILE_SUBJECTS[case](minter)})

        assert response == INVALID_GRANT
        assert len(signer.signed) == signed_before

    @pytest.mark.parametrize(
        "event",
        [
            {},
            {"subject_token": ""},
            {"subject_token": 7},
            {"subject_token": None},
            "a bare string",
            ["subject_token"],
        ],
    )
    def test_a_request_without_exactly_one_usable_subject_token_is_invalid_grant(
        self, exchange: Any, event: object
    ) -> None:
        assert _call(exchange, event) == INVALID_GRANT
        assert cast("_LocalSigner", exchange.SIGNER).signed == []

    def test_an_extra_request_field_is_refused_rather_than_ignored(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        """The request carries the subject token only; nothing else may steer the mint."""
        token = minter.token()
        assert "access_token" in _call(exchange, {"subject_token": token})

        response = _call(exchange, {"subject_token": token, "audience": "adlytics-dev-gateway"})

        assert response == INVALID_GRANT

    def test_the_sdk_trace_carrier_is_tolerated_and_does_not_change_the_minted_claims(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        """The caller's botocore instrumentation adds `headers` to every invoke payload."""
        token = minter.token()
        carrier = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}

        response = _call(exchange, {"subject_token": token, "headers": carrier})

        assert response["token_type"] == "Bearer"
        claims = jwt.decode(response["access_token"], options={"verify_signature": False})
        assert set(claims) == {"iss", "sub", "aud", "client_id", "act", "iat", "exp", "jti"}

    @pytest.mark.parametrize(
        "carrier",
        [
            "traceparent=00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            ["traceparent"],
            {"traceparent": {"nested": "value"}},
            {"traceparent": 7},
        ],
    )
    def test_a_trace_carrier_that_is_not_a_flat_string_map_is_invalid_grant(
        self, exchange: Any, minter: TokenMinter, carrier: object
    ) -> None:
        response = _call(exchange, {"subject_token": minter.token(), "headers": carrier})

        assert response == INVALID_GRANT
        assert cast("_LocalSigner", exchange.SIGNER).signed == []


class TestRouteExchangeKeyRotation:
    def test_an_unknown_kid_refreshes_the_key_set_once_and_then_mints(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        keys = _KeySource(current=minter.jwks(kid="retired-kid"), refreshed=minter.jwks())
        exchange.KEYS = keys

        response = _call(exchange, {"subject_token": minter.token(kid=KID)})

        assert "access_token" in response
        assert keys.refreshes == 1

    def test_a_kid_still_unknown_after_one_refresh_is_invalid_grant(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        keys = _KeySource(current=minter.jwks(kid="retired-kid"), refreshed=minter.jwks())
        exchange.KEYS = keys
        assert "access_token" in _call(exchange, {"subject_token": minter.token(kid=KID)})

        response = _call(exchange, {"subject_token": minter.token(kid="never-published")})

        assert response == INVALID_GRANT
        assert keys.refreshes == 2
        assert len(cast("_LocalSigner", exchange.SIGNER).signed) == 1


class TestRouteExchangeInternalFailuresRaise:
    def test_a_signing_failure_raises_rather_than_answering_invalid_grant(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        """A refusal would send a healthy session back to refresh for a broken key."""
        cast("_LocalSigner", exchange.SIGNER).failure = DependencyError(_UNAVAILABLE, "down")

        with pytest.raises(DependencyError, match="down"):
            _call(exchange, {"subject_token": minter.token()})

    def test_a_key_set_fetch_failure_raises_rather_than_answering_invalid_grant(
        self, exchange: Any, minter: TokenMinter
    ) -> None:
        cast("_KeySource", exchange.KEYS).failure = DependencyError(_UNAVAILABLE, "jwks down")

        with pytest.raises(DependencyError, match="jwks down"):
            _call(exchange, {"subject_token": minter.token()})


def test_route_exchange_logs_neither_the_subject_nor_the_minted_token(
    exchange: Any, minter: TokenMinter, caplog: pytest.LogCaptureFixture
) -> None:
    subject = minter.token()
    minted = _call(exchange, {"subject_token": subject})["access_token"]
    _call(exchange, {"subject_token": minter.token(client_id="another-client")})

    logged = "".join(repr(vars(record)) for record in caplog.records)
    assert "authorization denied" in logged
    assert subject not in logged
    assert minted not in logged
    assert minted.split(".")[2] not in logged


def _get(resource: str, method: str = "GET") -> dict[str, object]:
    return {"httpMethod": method, "resource": resource, "path": resource}


class TestIssuer:
    def test_discovery_is_the_exact_contracted_document_and_headers(self, issuer: Any) -> None:
        response = _call(issuer, _get("/.well-known/openid-configuration"))

        assert response["statusCode"] == 200
        assert response["headers"] == {
            "Content-Type": "application/json",
            "Cache-Control": "public, max-age=300",
        }
        assert json.loads(response["body"]) == {
            "issuer": TOKEN_ISSUER,
            "jwks_uri": f"{TOKEN_ISSUER}/jwks.json",
            "token_endpoint": f"{TOKEN_ISSUER}/token",
            "grant_types_supported": ["urn:ietf:params:oauth:grant-type:token-exchange"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def test_the_key_set_publishes_exactly_the_signing_keys_public_half(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        response = _call(issuer, _get("/jwks.json"))

        assert response["statusCode"] == 200
        assert response["headers"]["Cache-Control"] == "public, max-age=300"
        (key,) = json.loads(response["body"])["keys"]
        public = harness_key.public_key().public_numbers()
        assert {k: v for k, v in key.items() if k not in {"n", "e"}} == {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": SIGNING_KEY_ID,
        }
        assert jwt.PyJWK.from_dict(key).key.public_numbers() == public
        assert set(key) == {"kty", "use", "alg", "kid", "n", "e"}

    def test_the_key_set_is_read_from_the_key_store_once_per_container(self, issuer: Any) -> None:
        first = _call(issuer, _get("/jwks.json"))
        second = _call(issuer, _get("/jwks.json"))

        assert first["body"] == second["body"]
        assert cast("_LocalSigner", issuer.SIGNER).public_key_reads == 1

    def test_a_key_store_failure_raises_and_is_not_cached(self, issuer: Any) -> None:
        signer = cast("_LocalSigner", issuer.SIGNER)
        signer.failure = DependencyError(_UNAVAILABLE, "kms down")
        with pytest.raises(DependencyError, match="kms down"):
            _call(issuer, _get("/jwks.json"))

        signer.failure = None
        assert _call(issuer, _get("/jwks.json"))["statusCode"] == 200
        assert signer.public_key_reads == 2

    @pytest.mark.parametrize(
        "event",
        [
            _get("/jwks.json", method="POST"),
            _get("/.well-known/openid-configuration", method="HEAD"),
            _get("/token"),
            _get("/v1/jwks.json"),
            {"httpMethod": "GET"},
            {},
        ],
    )
    def test_anything_but_get_on_the_two_resources_is_404_without_a_key_read(
        self, issuer: Any, event: dict[str, object]
    ) -> None:
        response = _call(issuer, event)

        assert response == {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "not_found"}),
        }
        assert cast("_LocalSigner", issuer.SIGNER).public_key_reads == 0


def test_an_unusable_issuer_fails_the_container_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing-slash issuer would publish a jwks_uri the authorizer cannot fetch."""
    for key, value in {**_ENV, "ADL_TOKEN_ISSUER": f"{TOKEN_ISSUER}/"}.items():
        monkeypatch.setenv(key, value)
    token_service_settings.cache_clear()
    name = "adapters.inbound.token_service.issuer"
    sys.modules.pop(name, None)
    try:
        with pytest.raises(InvariantError, match="Token service configuration is incomplete"):
            importlib.import_module(name)
    finally:
        sys.modules.pop(name, None)
        token_service_settings.cache_clear()


def test_a_harness_token_presented_as_the_subject_is_refused(
    exchange: Any, minter: TokenMinter
) -> None:
    """The route exchange accepts only Cognito tokens; its own output is not one."""
    minted = _call(exchange, {"subject_token": minter.token()})["access_token"]

    assert _call(exchange, {"subject_token": minted}) == INVALID_GRANT


# --- The agent exchange: POST /token (RFC 8693, ADR-077 S4) ------------------------------------

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TYPE = "urn:ietf:params:oauth:token-type:jwt"
FORM = "application/x-www-form-urlencoded"
SUBJECT_EXP = int(NOW.timestamp()) + LIFETIME_SECONDS
"""Epoch seconds: the subject Harness token's expiry."""
EXCHANGE_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _harness_subject(key: rsa.RSAPrivateKey, *, kid: str = SIGNING_KEY_ID, **overrides: Any) -> str:
    """Sign a Harness token as the route exchange mints it; an override of None removes a claim."""
    claims = {
        "iss": TOKEN_ISSUER,
        "sub": SUBJECT,
        "aud": HARNESS_AUDIENCE,
        "client_id": ROUTE_CLIENT,
        "act": {"sub": ROUTE_CLIENT},
        "iat": int(NOW.timestamp()),
        "exp": SUBJECT_EXP,
        "jti": "0" * 31 + "1",
        **overrides,
    }
    return jwt.encode(
        {k: v for k, v in claims.items() if v is not None},
        key=key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _fields(subject: str, **overrides: str | None) -> dict[str, str]:
    """Build the form the credential provider is configured to send; None removes a field."""
    fields: dict[str, str | None] = {
        "grant_type": TOKEN_EXCHANGE,
        "client_id": AGENT_CLIENT,
        "client_secret": AGENT_SECRET,
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        **overrides,
    }
    return {k: v for k, v in fields.items() if v is not None}


def _post(
    fields: dict[str, str] | list[tuple[str, str]] | str,
    *,
    content_type: str | None = FORM,
    base64_body: bool = False,
) -> dict[str, object]:
    body = fields if isinstance(fields, str) else urllib.parse.urlencode(fields)
    headers = {} if content_type is None else {"Content-Type": content_type}
    return {
        "httpMethod": "POST",
        "resource": "/token",
        "path": "/token",
        "headers": headers,
        "body": base64.b64encode(body.encode()).decode() if base64_body else body,
        "isBase64Encoded": base64_body,
    }


def _exchange(module: ModuleType, event: object) -> tuple[int, dict[str, Any]]:
    response = _call(module, event)
    assert response["headers"] == EXCHANGE_HEADERS
    return response["statusCode"], json.loads(response["body"])


def _gateway_claims(token: str, harness_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """Verify an exchanged token as the Gateway authorizer would: signature, iss, aud, alg."""
    return jwt.decode(
        token,
        key=_published_key(harness_key),
        algorithms=["RS256"],
        audience=GATEWAY_AUDIENCE,
        issuer=TOKEN_ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )


def _signer(module: Any) -> _LocalSigner:
    return cast("_LocalSigner", module.SIGNER)


def _secret(module: Any) -> _LocalClientSecret:
    return cast("_LocalClientSecret", module.CLIENT_SECRET)


def _assert_issued(module: Any, harness_key: rsa.RSAPrivateKey) -> None:
    """Assert the known-good exchange that each refusal here is measured against."""
    status, body = _exchange(module, _post(_fields(_harness_subject(harness_key))))
    assert status == 200
    assert set(body) == {"access_token", "issued_token_type", "token_type", "expires_in"}


type _EventFor = Callable[[dict[str, str]], dict[str, object]]

_ACCEPTED_SHAPES: list[tuple[str, _EventFor]] = [
    ("base64 body", lambda f: _post(f, base64_body=True)),
    ("charset parameter", lambda f: _post(f, content_type=f"{FORM}; charset=UTF-8")),
    ("lower-case header name", lambda f: {**_post(f), "headers": {"content-type": FORM}}),
    ("jwt subject type", lambda f: _post({**f, "subject_token_type": JWT_TYPE})),
    ("matching audience", lambda f: _post({**f, "audience": GATEWAY_AUDIENCE})),
    ("matching resource", lambda f: _post({**f, "resource": GATEWAY_AUDIENCE})),
    ("a scope, ignored", lambda f: _post({**f, "scope": "adlytics/tools"})),
]


class TestTheAgentExchangeIssuesAGatewayToken:
    def test_the_response_is_exactly_the_rfc_8693_success_document(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        response = _call(issuer, _post(_fields(_harness_subject(harness_key))))
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert response["headers"] == EXCHANGE_HEADERS
        assert set(body) == {"access_token", "issued_token_type", "token_type", "expires_in"}
        assert body["issued_token_type"] == ACCESS_TOKEN_TYPE
        assert body["token_type"] == "Bearer"
        # Seconds: the subject's remaining life at NOW, never a configured lifetime.
        assert body["expires_in"] == LIFETIME_SECONDS

    def test_the_exchanged_claims_are_the_gateway_contract_bound_to_the_subject(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key))))
        claims = _gateway_claims(body["access_token"], harness_key)

        assert jwt.get_unverified_header(body["access_token"]) == {
            "alg": "RS256",
            "typ": "JWT",
            "kid": SIGNING_KEY_ID,
        }
        assert {k: v for k, v in claims.items() if k != "jti"} == {
            "iss": TOKEN_ISSUER,
            "sub": SUBJECT,
            "aud": GATEWAY_AUDIENCE,
            "client_id": AGENT_CLIENT,
            "act": {"sub": AGENT_CLIENT},
            "iat": int(NOW.timestamp()),
            "exp": SUBJECT_EXP,
        }
        assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])

    def test_a_later_exchange_still_expires_exactly_with_its_subject(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        later = NOW + timedelta(seconds=100, microseconds=400_000)
        monkeypatch.setattr(issuer, "utc_now", lambda: later)

        _, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key))))
        claims = _gateway_claims(body["access_token"], harness_key)

        assert claims["exp"] == SUBJECT_EXP
        assert body["expires_in"] == LIFETIME_SECONDS - 100  # whole seconds left
        assert claims["exp"] - claims["iat"] == body["expires_in"]

    def test_the_route_exchanges_real_output_is_an_accepted_subject(
        self, exchange: Any, issuer: Any, minter: TokenMinter
    ) -> None:
        harness = _call(exchange, {"subject_token": minter.token()})["access_token"]

        status, body = _exchange(issuer, _post(_fields(harness)))

        assert status == 200
        assert _gateway_claims(body["access_token"], _signer(issuer).key)["sub"] == SUBJECT

    @pytest.mark.parametrize(
        "event_for", [pytest.param(shape, id=name) for name, shape in _ACCEPTED_SHAPES]
    )
    def test_each_accepted_request_shape_issues(
        self,
        issuer: Any,
        harness_key: rsa.RSAPrivateKey,
        event_for: _EventFor,
    ) -> None:
        status, body = _exchange(issuer, event_for(_fields(_harness_subject(harness_key))))

        assert status == 200
        assert _gateway_claims(body["access_token"], harness_key)["exp"] == SUBJECT_EXP


class TestTheClientIsAuthenticatedFirst:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"client_id": "adlytics-dev-conversation-route"},
            {"client_id": None},
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            {"client_secret": "not-the-registered-value"},
            {"client_secret": ""},
            {"client_secret": None},
            {"client_secret": f"{AGENT_SECRET}\u00e9"},
        ],
    )
    def test_a_bad_client_is_401_invalid_client_without_a_key_read_or_a_signature(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, overrides: dict[str, str | None]
    ) -> None:
        subject = _harness_subject(harness_key)
        _secret(issuer).cached = AGENT_SECRET

        status, body = _exchange(issuer, _post(_fields(subject, **overrides)))

        assert (status, body) == (401, {"error": "invalid_client"})
        assert _signer(issuer).signed == []
        assert _signer(issuer).public_key_reads == 0
        _assert_issued(issuer, harness_key)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"grant_type": "authorization_code"},
            {"subject_token_type": None},
            {"actor_token": "x"},
            {"audience": "adlytics-dev-harness"},
            {"subject_token": "not.a.jwt"},
        ],
    )
    def test_a_bad_client_is_refused_before_any_other_defect_is_examined(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, overrides: dict[str, str | None]
    ) -> None:
        subject = _harness_subject(harness_key)

        status, body = _exchange(
            issuer,
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            _post(_fields(subject, client_secret="wrong", **overrides)),
        )

        assert (status, body) == (401, {"error": "invalid_client"})
        assert _signer(issuer).public_key_reads == 0

    def test_a_wrong_client_id_never_reads_the_secret_store(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _exchange(issuer, _post(_fields(_harness_subject(harness_key), client_id="someone")))

        assert _secret(issuer).reads == 0

    def test_the_secret_is_read_once_per_container_across_exchanges(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _assert_issued(issuer, harness_key)
        _assert_issued(issuer, harness_key)

        assert _secret(issuer).reads == 1

    def test_a_rotated_secret_is_accepted_after_exactly_one_refetch(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _assert_issued(issuer, harness_key)
        store = _secret(issuer)
        store.stored = "Fictional-Rotated-Secret-For-Tests"

        status, _ = _exchange(
            issuer,
            _post(_fields(_harness_subject(harness_key), client_secret=store.stored)),
        )

        assert status == 200
        assert store.reads == 2

    def test_a_mismatched_secret_refetches_once_and_still_refuses(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _assert_issued(issuer, harness_key)

        status, _ = _exchange(
            issuer,
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            _post(_fields(_harness_subject(harness_key), client_secret="wrong")),
        )

        assert status == 401
        assert _secret(issuer).reads == 2


class TestTheExchangeRequestIsChecked:
    @pytest.mark.parametrize(
        "overrides",
        [{"grant_type": "authorization_code"}, {"grant_type": "client_credentials"}],
    )
    def test_another_grant_is_unsupported_grant_type(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, overrides: dict[str, str]
    ) -> None:
        _assert_issued(issuer, harness_key)

        status, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key), **overrides)))

        assert (status, body) == (400, {"error": "unsupported_grant_type"})

    def test_a_missing_grant_is_unsupported_grant_type(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _assert_issued(issuer, harness_key)

        status, body = _exchange(
            issuer, _post(_fields(_harness_subject(harness_key), grant_type=None))
        )

        assert (status, body) == (400, {"error": "unsupported_grant_type"})

    @pytest.mark.parametrize(
        "overrides",
        [
            {"subject_token_type": None},
            {"subject_token_type": "urn:ietf:params:oauth:token-type:id_token"},
            {"subject_token_type": "urn:ietf:params:oauth:token-type:refresh_token"},
            {"actor_token": fictional_jwt({}, signature="c2ln")},
            {"actor_token_type": JWT_TYPE},
            {"subject_token": None},
            {"subject_token": ""},
        ],
    )
    def test_a_malformed_exchange_is_invalid_request_and_signs_nothing(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, overrides: dict[str, str | None]
    ) -> None:
        _assert_issued(issuer, harness_key)
        signed_before = len(_signer(issuer).signed)

        status, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key), **overrides)))

        assert (status, body) == (400, {"error": "invalid_request"})
        assert len(_signer(issuer).signed) == signed_before

    @pytest.mark.parametrize(
        "overrides",
        [
            {"audience": "adlytics-dev-harness"},
            {"audience": ""},
            {"resource": "https://gateway.example/mcp"},
            {"audience": GATEWAY_AUDIENCE, "resource": "adlytics-prod-gateway"},
        ],
    )
    def test_any_target_but_the_gateway_audience_is_invalid_target(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, overrides: dict[str, str]
    ) -> None:
        _assert_issued(issuer, harness_key)
        signed_before = len(_signer(issuer).signed)

        status, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key), **overrides)))

        assert (status, body) == (400, {"error": "invalid_target"})
        assert len(_signer(issuer).signed) == signed_before


_MALFORMED_BODIES: list[tuple[str, _EventFor]] = [
    ("json content type", lambda f: _post(f, content_type="application/json")),
    ("no content type", lambda f: _post(f, content_type=None)),
    ("no headers at all", lambda f: {**_post(f), "headers": None}),
    ("invalid base64", lambda f: {**_post(f, base64_body=True), "body": "not base64!"}),
    (
        "base64 of non-utf-8 bytes",
        lambda _f: {**_post("", base64_body=True), "body": base64.b64encode(b"\xff\xfe").decode()},
    ),
    ("a bare key", lambda f: _post(urllib.parse.urlencode(f) + "&orphan")),
    ("invalid percent-encoding", lambda f: _post(urllib.parse.urlencode(f) + "&x=%ff")),
    ("a repeated parameter", lambda f: _post([*f.items(), ("client_id", AGENT_CLIENT)])),
    ("an oversized body", lambda f: _post({**f, "scope": "s" * 16_384})),
    ("too many fields", lambda f: _post([*f.items(), *((f"k{i}", "v") for i in range(40))])),
    ("a non-text body", lambda f: {**_post(f), "body": 7}),
]


class TestTheFormIsParsedStrictly:
    @pytest.mark.parametrize(
        "event_for", [pytest.param(shape, id=name) for name, shape in _MALFORMED_BODIES]
    )
    def test_each_malformed_body_is_invalid_request_before_the_client_is_consulted(
        self,
        issuer: Any,
        harness_key: rsa.RSAPrivateKey,
        event_for: _EventFor,
    ) -> None:
        status, body = _exchange(issuer, event_for(_fields(_harness_subject(harness_key))))

        assert (status, body) == (400, {"error": "invalid_request"})
        assert _secret(issuer).reads == 0
        assert _signer(issuer).public_key_reads == 0
        _assert_issued(issuer, harness_key)


_HOSTILE_HARNESS_SUBJECTS: dict[str, Callable[[rsa.RSAPrivateKey, TokenMinter], str]] = {
    "a cognito access token": lambda _k, m: m.token(),
    "wrong iss": lambda k, _m: _harness_subject(k, iss="https://other.example/v1"),
    "wrong aud": lambda k, _m: _harness_subject(k, aud=GATEWAY_AUDIENCE),
    "wrong client_id": lambda k, _m: _harness_subject(k, client_id=AGENT_CLIENT),
    "expired at exactly exp": lambda k, _m: _harness_subject(k, exp=int(NOW.timestamp())),
    "missing sub": lambda k, _m: _harness_subject(k, sub=None),
    "another kid": lambda k, _m: _harness_subject(k, kid="another-kid"),
    "signed by a foreign key": lambda _k, m: _harness_subject(m.other_key),
    "hs256 substituted": lambda _k, _m: jwt.encode(
        {"sub": SUBJECT}, key="a" * 32, algorithm="HS256", headers={"kid": SIGNING_KEY_ID}
    ),
    "not a jwt": lambda _k, _m: "not.a.jwt",
}


class TestTheSubjectMustBeAHarnessTokenOfThisIssuer:
    @pytest.mark.parametrize("case", sorted(_HOSTILE_HARNESS_SUBJECTS))
    def test_a_hostile_subject_is_invalid_grant_and_signs_nothing(
        self, issuer: Any, minter: TokenMinter, case: str
    ) -> None:
        harness_key = _signer(issuer).key
        _assert_issued(issuer, harness_key)
        signed_before = len(_signer(issuer).signed)

        status, body = _exchange(
            issuer, _post(_fields(_HOSTILE_HARNESS_SUBJECTS[case](harness_key, minter)))
        )

        assert (status, body) == (400, INVALID_GRANT)
        assert len(_signer(issuer).signed) == signed_before

    def test_an_exchanged_gateway_token_presented_as_the_subject_is_invalid_grant(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _, body = _exchange(issuer, _post(_fields(_harness_subject(harness_key))))

        status, refused = _exchange(issuer, _post(_fields(body["access_token"])))

        assert (status, refused) == (400, INVALID_GRANT)

    def test_the_key_set_is_read_from_the_key_store_once_across_exchanges(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _assert_issued(issuer, harness_key)
        _assert_issued(issuer, harness_key)
        _call(issuer, _get("/jwks.json"))

        assert _signer(issuer).public_key_reads == 1


class TestExchangeInternalFailuresRaise:
    def test_a_secret_store_failure_raises_rather_than_answering_invalid_client(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        _secret(issuer).failure = DependencyError(_UNAVAILABLE, "secrets down")

        with pytest.raises(DependencyError, match="secrets down"):
            _call(issuer, _post(_fields(_harness_subject(harness_key))))

    def test_a_signing_failure_raises_rather_than_answering_a_refusal(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey
    ) -> None:
        subject = _harness_subject(harness_key)
        _call(issuer, _get("/jwks.json"))
        _signer(issuer).failure = DependencyError(_UNAVAILABLE, "kms down")

        with pytest.raises(DependencyError, match="kms down"):
            _call(issuer, _post(_fields(subject)))


def _terminal_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        vars(record)
        for record in caplog.records
        if getattr(record, "operation", None) == "token.issuer"
        and getattr(record, "phase", None) == "terminal"
    ]


_P2_FIELDS = (
    "form_keys",
    "subject_type",
    "audience_present",
    "resource_present",
    "scope_present",
    "target_matches",
)


class TestTheExchangeDiagnostics:
    def test_an_issued_exchange_records_exactly_the_labelled_request_shape(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
    ) -> None:
        fields = _fields(_harness_subject(harness_key), audience=GATEWAY_AUDIENCE, scope="s")
        _exchange(issuer, _post(fields))

        (record,) = _terminal_records(caplog)
        assert record["outcome"] == "issued"
        assert record["status"] == 200
        assert {name: record[name] for name in _P2_FIELDS} == {
            "form_keys": [
                "audience",
                "client_auth",
                "client_id",
                "grant_type",
                "scope",
                "subject",
                "subject_type",
            ],
            "subject_type": "access",
            "audience_present": True,
            "resource_present": False,
            "scope_present": True,
            "target_matches": True,
        }

    def test_an_unlisted_parameter_is_recorded_by_bounded_name(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
    ) -> None:
        fields = _fields(_harness_subject(harness_key), requested_token_type=ACCESS_TOKEN_TYPE)
        _exchange(issuer, _post({**fields, "x-trace": "1", "Weird Name!": "2"}))

        (record,) = _terminal_records(caplog)
        assert record["outcome"] == "issued"
        assert {"requested_type", "other:x-trace", "other"} <= set(record["form_keys"])

    @pytest.mark.parametrize(
        "refusal",
        [
            # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
            ({"client_secret": "wrong"}, "invalid_client", 401),
            ({"grant_type": "password"}, "unsupported_grant_type", 400),
            ({"actor_token": "x"}, "invalid_request", 400),
            ({"resource": "https://attacker.example/steal"}, "invalid_target", 400),
            ({"subject_token_type": JWT_TYPE, "subject_token": "not.a.jwt"}, "invalid_grant", 400),
        ],
    )
    def test_every_refusal_records_its_outcome_and_the_request_shape(
        self,
        issuer: Any,
        caplog: pytest.LogCaptureFixture,
        refusal: tuple[dict[str, str], str, int],
    ) -> None:
        overrides, outcome, status = refusal
        _exchange(issuer, _post(_fields(_harness_subject(_signer(issuer).key), **overrides)))

        (record,) = _terminal_records(caplog)
        assert (record["outcome"], record["status"]) == (outcome, status)
        assert set(_P2_FIELDS) <= set(record)
        assert "client_auth" in record["form_keys"]

    def test_a_mismatched_target_records_a_boolean_never_the_target(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
    ) -> None:
        foreign_target = "https://attacker.example/steal"
        _exchange(issuer, _post(_fields(_harness_subject(harness_key), resource=foreign_target)))

        (record,) = _terminal_records(caplog)
        assert (record["resource_present"], record["target_matches"]) == (True, False)
        assert foreign_target not in "".join(repr(vars(r)) for r in caplog.records)

    def test_an_internal_failure_still_records_the_request_shape(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
    ) -> None:
        _secret(issuer).failure = DependencyError(_UNAVAILABLE, "secrets down")

        with pytest.raises(DependencyError):
            _call(issuer, _post(_fields(_harness_subject(harness_key))))

        (record,) = _terminal_records(caplog)
        assert (record["outcome"], record["status"]) == ("failed", None)
        assert record["subject_type"] == "access"

    def test_no_secret_subject_or_minted_token_value_reaches_any_record(
        self, issuer: Any, harness_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
    ) -> None:
        subject = _harness_subject(harness_key)
        _, body = _exchange(issuer, _post(_fields(subject)))
        minted = body["access_token"]
        # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
        wrong_secret = "Fictional-Wrong-Secret-For-Tests"
        _exchange(issuer, _post(_fields(subject, client_secret=wrong_secret)))
        _exchange(issuer, _post(_fields(subject, actor_token=minted)))

        logged = "".join(repr(vars(record)) for record in caplog.records)
        assert len(_terminal_records(caplog)) == 3
        for value in (AGENT_SECRET, wrong_secret, subject, minted):
            assert value not in logged
        for signature in (subject.split(".")[2], minted.split(".")[2]):
            assert signature not in logged
