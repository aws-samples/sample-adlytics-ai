# SPDX-License-Identifier: MIT-0
"""Cognito implementation of IdentityProviderPort.

Three of the four operations are plain OAuth2 endpoints on the login domain, not
SDK calls — there is no `cognito-idp` API for the authorization-code grant. They
use the shared HTTPS-only outbound boundary so redirects cannot downgrade a
credential-bearing request.

Every failure leaves here as a DependencyError. A transport exception must never
reach the domain, and a Cognito error body must never reach a caller: it can name
the pool and the app client.

Response translation is deliberately module-level and pure, so the shape handling
is unit tested while the network behaviour is verified against a real pool in the
integration tier. See ADR-014.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import requests

from adapters.config import Settings
from adapters.outbound.https import HttpsSession, InsecureUrlError
from adapters.outbound.https import require_https as require_transport_https
from domain.identity.session import utc_now
from domain.identity.tokens import JsonWebKey, JsonWebKeySet, TokenSet
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.identity_provider import ExchangeCodeRequest

TOKEN_EXCHANGE_FAILED = ErrorCode("COGNITO", 1)
REFRESH_FAILED = ErrorCode("COGNITO", 2)
REVOKE_FAILED = ErrorCode("COGNITO", 3)
JWKS_FETCH_FAILED = ErrorCode("COGNITO", 4)
INSECURE_URL = ErrorCode("COGNITO", 5)

REQUEST_TIMEOUT_SECONDS = 5.0
"""Short on purpose. This sits inside a user-facing login redirect, and a provider
that has not answered in five seconds will not answer usefully."""

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.2

SERVER_ERROR_FLOOR = 500
"""The boundary between "the provider is unwell" and "the provider said no".

Below it a retry cannot change the answer. Above it a retry is the whole point.
"""

REDIRECT_STATUS_FLOOR = 300

JWKS_CACHE_TTL = timedelta(hours=1)
"""An upper bound on how long a withdrawn signing key stays trusted.

Refreshing on an unrecognised `kid` already covers rotation, which is the common
case. This covers the uncommon one: a key *removed* from the set would otherwise
stay cached and keep validating tokens until the container was replaced.
"""


@dataclass(frozen=True, slots=True)
class _CachedKeys:
    keys: JsonWebKeySet
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class _Call:
    """One outbound endpoint, and how to report its failure.

    Carrying the error code next to the URL means each call site names the failure
    it is describing, rather than sharing a code that says only that something
    Cognito-shaped went wrong.
    """

    url: str
    code: ErrorCode
    authorization: str | None = None


@dataclass(frozen=True, slots=True)
class _Request:
    """One fully prepared provider request and its failure classification."""

    method: str
    url: str
    code: ErrorCode
    data: bytes | None = None
    headers: dict[str, str] | None = None


class CognitoSigningKeys:
    """One user pool's public key set, cached per process.

    Needs only the key set URL, so a function that verifies Cognito tokens without holding
    the BFF client configuration (the token service's route exchange, ADR-077 §3) shares this
    fetch, parse, and cache rather than repeating it.
    """

    def __init__(self, jwks_url: str, http: HttpsSession | None = None) -> None:
        self._jwks_url = jwks_url
        self._cached: _CachedKeys | None = None
        self._http = http if http is not None else HttpsSession()

    def signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, from cache while it is still fresh."""
        cached = self._cached
        if cached is not None and is_cache_fresh(cached.fetched_at, utc_now()):
            return cached.keys
        return self.refresh_signing_keys()

    def refresh_signing_keys(self) -> JsonWebKeySet:
        """Fetch the key set, bypassing the cache.

        Called when a token names a `kid` we do not hold, which usually means the
        pool rotated. Never call this per request: an unauthenticated caller could
        otherwise drive an outbound fetch on every attempt.
        """
        body = _send(
            self._http,
            _Request(method="GET", url=require_https(self._jwks_url), code=JWKS_FETCH_FAILED),
        )
        fetched = to_key_set(body)
        self._cached = _CachedKeys(keys=fetched, fetched_at=utc_now())
        return fetched


class CognitoIdentityProvider:
    """Talks to one Cognito user pool. Satisfies IdentityProviderPort structurally."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = HttpsSession()
        self._keys: CognitoSigningKeys | None = None

    def exchange_code(self, request: ExchangeCodeRequest) -> TokenSet:
        """Redeem an authorization code for tokens."""
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": request.code,
                "redirect_uri": request.redirect_uri,
                "code_verifier": request.code_verifier,
            },
            code=TOKEN_EXCHANGE_FAILED,
        )
        return to_token_set(payload, code=TOKEN_EXCHANGE_FAILED)

    def refresh(self, refresh_token: str) -> TokenSet:
        """Exchange a refresh token for a new access token.

        Cognito does not reissue a refresh token here, so the result carries an
        empty one and the caller leaves the existing cookie alone.
        """
        payload = self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            code=REFRESH_FAILED,
        )
        return to_token_set(payload, code=REFRESH_FAILED, expect_refresh_token=False)

    def revoke(self, refresh_token: str) -> None:
        """Invalidate a refresh token and every access token derived from it."""
        url = require_https(self._settings.cognito_revoke_url)
        self._post_form(
            _Call(
                url=url,
                code=REVOKE_FAILED,
                authorization=self._basic_auth(),
            ),
            {"token": refresh_token, "client_id": self._settings.cognito_client_id},
        )

    def signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, from cache while it is still fresh."""
        return self._key_source().signing_keys()

    def refresh_signing_keys(self) -> JsonWebKeySet:
        """Fetch the key set, bypassing the cache; see `CognitoSigningKeys`."""
        return self._key_source().refresh_signing_keys()

    def _key_source(self) -> CognitoSigningKeys:
        """Build the key source on first use, reading the key set URL no earlier than before."""
        if self._keys is None:
            self._keys = CognitoSigningKeys(self._settings.cognito_jwks_url, self._http)
        return self._keys

    def _token_request(self, form: dict[str, str], *, code: ErrorCode) -> dict[str, Any]:
        """POST to the token endpoint with client credentials in the header.

        The secret goes in an Authorization header rather than the form body, which
        is what Cognito expects for a confidential client and keeps it out of
        anything that logs a request body.
        """
        url = require_https(self._settings.cognito_token_url)
        return self._post_form(
            _Call(
                url=url,
                code=code,
                authorization=self._basic_auth(),
            ),
            {**form, "client_id": self._settings.cognito_client_id},
        )

    def _basic_auth(self) -> str:
        return basic_auth_header(
            self._settings.cognito_client_id,
            self._settings.cognito_client_secret.get_secret_value(),
        )

    def _post_form(self, call: _Call, form: dict[str, str]) -> dict[str, Any]:
        url = require_https(call.url)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if call.authorization is not None:
            headers["Authorization"] = call.authorization
        return _send(
            self._http,
            _Request(
                method="POST",
                url=url,
                data=urllib.parse.urlencode(form).encode(),
                headers=headers,
                code=call.code,
            ),
        )


def _send(http: HttpsSession, request: _Request) -> dict[str, Any]:
    """Send with bounded retries, and translate every failure.

    Retries cover transport faults and 5xx only. A 4xx is a decision the
    provider has made — a spent code, a revoked token, a rejected client — and
    repeating it spends the user's latency budget without changing anything.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with http.request(
                request.method,
                request.url,
                data=request.data,
                headers=request.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                status = response.status_code
                if status >= SERVER_ERROR_FLOOR:
                    last = requests.HTTPError(response=response)
                elif status >= REDIRECT_STATUS_FLOOR:
                    raise DependencyError(
                        request.code,
                        f"The identity provider rejected the request with {status}.",
                        context={"status": status, "url": request.url},
                    ) from None
                else:
                    return decode_json_object(response.content, code=request.code)
        except InsecureUrlError:
            raise DependencyError(
                INSECURE_URL, "Refusing to call a non-HTTPS identity provider URL."
            ) from None
        except (requests.RequestException, TimeoutError, OSError) as exc:
            last = exc
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))

    raise DependencyError(
        request.code,
        f"The identity provider did not respond after {MAX_ATTEMPTS} attempts.",
        context={"url": request.url, "last_error": type(last).__name__},
    ) from None


def is_cache_fresh(fetched_at: datetime, now: datetime) -> bool:
    """Whether a cached key set may still be used.

    Pure and clock-injected so the expiry boundary is testable without waiting an
    hour or reaching the network.
    """
    return now - fetched_at < JWKS_CACHE_TTL


def basic_auth_header(client_id: str, client_secret: str) -> str:
    """Build the confidential-client Authorization header."""
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


def require_https(url: str) -> str:
    """Refuse anything that is not HTTPS.

    Configuration already validates the origins these URLs derive from, so this
    cannot normally fail. It exists because the failure it prevents — a client
    secret or refresh token crossing the network in plaintext — is severe enough
    to justify one assertion at the point of use.
    """
    try:
        return require_transport_https(url)
    except InsecureUrlError:
        raise DependencyError(
            INSECURE_URL, "Refusing to call a non-HTTPS identity provider URL."
        ) from None


def decode_json_object(body: bytes, *, code: ErrorCode) -> dict[str, Any]:
    """Parse a JSON object response, or fail.

    An empty body is a success: the revocation endpoint answers with nothing.
    """
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DependencyError(code, "The identity provider returned unparseable JSON.") from exc
    if not isinstance(parsed, dict):
        raise DependencyError(code, "The identity provider returned an unexpected JSON shape.")
    return cast("dict[str, Any]", parsed)


def to_token_set(
    payload: dict[str, Any], *, code: ErrorCode, expect_refresh_token: bool = True
) -> TokenSet:
    """Build a TokenSet, failing loudly on a shape we did not expect.

    A 200 response with no `access_token` is not something to paper over with an
    empty string. That would produce a session cookie containing nothing and a
    login that appears to have worked.
    """
    access = payload.get("access_token")
    expires = payload.get("expires_in")
    if not isinstance(access, str) or not access:
        raise DependencyError(code, "The token response carried no access_token.")
    if not isinstance(expires, int) or isinstance(expires, bool):
        raise DependencyError(code, "The token response carried no usable expires_in.")

    refresh = payload.get("refresh_token") if expect_refresh_token else None
    return TokenSet(
        access_token=access,
        expires_in=expires,
        refresh_token=refresh if isinstance(refresh, str) else "",
    )


def to_key_set(body: dict[str, Any]) -> JsonWebKeySet:
    """Build a JsonWebKeySet from a JWKS document.

    A malformed entry fails rather than being skipped. Skipping would leave us
    without a key we may need, which then looks like rotation on the next request
    and drives a refresh on every attempt.
    """
    raw = body.get("keys")
    if not isinstance(raw, list) or not raw:
        raise DependencyError(JWKS_FETCH_FAILED, "The JWKS response carried no keys.")

    keys: list[JsonWebKey] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            raise DependencyError(JWKS_FETCH_FAILED, "A JWKS entry was not a JSON object.")
        fields = cast("dict[str, object]", entry)
        keys.append({str(name): str(value) for name, value in fields.items()})
    return JsonWebKeySet(keys=tuple(keys))
