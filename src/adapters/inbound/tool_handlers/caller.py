# SPDX-License-Identifier: MIT-0
"""Tool-side caller verification: the one identity a Gateway tool accepts (ADR-077 §5).

The Gateway's `CUSTOM_JWT` authorizer admits only a Gateway-audience token the token service
minted for the agent client, and forwards that bearer to each Lambda target inside
`client_context.custom["bedrockAgentCorePropagatedHeaders"]` (observed live 2026-09-26). A tool
does not rely on the Gateway having checked it: a detached interceptor, a misconfigured
authorizer, or a direct `lambda:Invoke` with a fabricated context must all fail closed here, so
every tool verifies the signature, `alg`, `kid`, `exp`, `iss`, `aud`, `client_id`, and `act.sub`
itself before it reads anything else about the call.

**The verification key comes from KMS, never from the public issuer.** The public key of the
signing key is read with `kms:GetPublicKey` once per container, on its first call, and cached for
the container's life. The `kid` is pinned to the key id, so an unknown `kid` is a refusal, never
a refresh: a flood on the public key-set route cannot reach a tool's cold start, and a rotation is
a new key, a new `kid`, and a redeploy. A key that cannot be read is not cached, so the next call
retries it; every call until then is refused.

**A second credential class, only when configured (ADR-150).** With `ADL_MCP_CLIENT_ID` set, the
external MCP gateway forwards an Amazon Quick user's own Cognito access token through the same
header channel. A token whose `kid` is not the token service's is then verified against the user
pool's AWS-managed key set (`exp`, `iss`, `client_id`, `token_use`), and its user must hold
`mcp_tool_use`. Unset, such a token is refused like any other foreign token.

Nothing here logs the bearer or any claim. A refusal is reported with a closed cause, and the
terminal record carries only whether the caller verified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, cast

from adapters.config import ToolCallerSettings
from adapters.inbound.tool_handlers import gateway
from adapters.outbound.cognito.identity_provider import CognitoSigningKeys
from adapters.outbound.kms.token_signer import KmsTokenSigner
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.identity.caller import CallerPrincipal, verify_caller
from domain.identity.issuance import jwk_from_der_public_key
from domain.identity.issued_tokens import VerifyIssuedTokenRequest
from domain.identity.session import IdentityError, ReviewAccessLevel, utc_now
from domain.identity.tokens import (
    JsonWebKeySet,
    UnknownSigningKeyError,
    VerifyAccessTokenRequest,
    signing_key_id,
    verify_access_token,
)
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    ErrorCode,
    InvariantError,
    ValidationError,
)

__all__ = [
    "CALLER_KEY_SET_UNAVAILABLE",
    "CALLER_UNVERIFIED",
    "CALLER_UNVERIFIED_DESCRIPTION",
    "CallerAdmission",
    "CallerRefusalCause",
    "CallerUnverifiedError",
    "CallerVerifier",
    "PublicKeySource",
    "SigningKeySource",
    "bearer_token",
    "kms_caller_verifier",
]

CALLER_UNVERIFIED = ErrorCode("CALLER", 1)
"""The refusal every tool returns when its caller does not verify. A `ValidationError`: the call
is outside the tool's contract, so it is a product signal, not a page."""

CALLER_KEY_SET_UNAVAILABLE = ErrorCode("CALLER", 2)
"""The signing key's public half was unusable. An `InvariantError`: the deployment is wrong."""

CALLER_UNVERIFIED_DESCRIPTION: Final = (
    "This tool call could not be attributed to a verified user, so it was not run."
)
"""Fixed text: it names no check, so a caller cannot learn which one it failed."""

_AUTHORIZATION: Final = "authorization"
_BEARER_SCHEME: Final = "bearer"
_MAX_BEARER_CHARACTERS: Final = 8_192
"""Far above one RS256 token; anything longer is not a bearer this issuer minted."""


class CallerRefusalCause(StrEnum):
    """Why a caller did not verify, as a closed label that is safe to log."""

    BEARER_MISSING = "bearer_missing"
    BEARER_MALFORMED = "bearer_malformed"
    ASSERTION_REFUSED = "assertion_refused"
    """The bearer parsed but did not verify. Not named `token_*`: the shared diagnostic redactor
    erases any word containing `token`, which would erase the cause."""
    KEY_SET_UNAVAILABLE = "key_set_unavailable"
    ENTITLEMENT_MISSING = "entitlement_missing"
    """A verified external MCP client caller whose user is not in `mcp-tool-users` (ADR-150)."""


class CallerUnverifiedError(IdentityError):
    """The caller did not verify; `cause` says why without repeating anything presented.

    `dependency` is set only when the key set could not be read, so the failure that should
    alarm is reported as itself rather than as a caller's mistake.
    """

    def __init__(
        self, cause: CallerRefusalCause, *, dependency: AdlyticsError | None = None
    ) -> None:
        super().__init__(f"The caller was not verified: {cause.value}.")
        self.cause = cause
        self.dependency = dependency


class PublicKeySource(Protocol):
    """Where the signing key's DER public key comes from: KMS in production, a test key here."""

    def public_key_der(self) -> bytes:
        """Return the DER SubjectPublicKeyInfo of the token signing key."""
        ...


class SigningKeySource(Protocol):
    """The user pool's published key set, cached: `CognitoSigningKeys` in production (ADR-150)."""

    def signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, from cache while fresh."""
        ...

    def refresh_signing_keys(self) -> JsonWebKeySet:
        """Return the pool's public keys, bypassing the cache."""
        ...


def bearer_token(context: object) -> str:
    """Return the one bearer token the Gateway propagated, or raise `CallerUnverifiedError`.

    Exactly one header named `Authorization` in any case, whose value is `Bearer <token>` with a
    case-insensitive scheme and a token containing no whitespace. Two such headers, differing only
    in case or duplicated in JSON, are refused rather than choosing one.
    """
    values = [
        value
        for name, value in gateway.propagated_header_pairs(context)
        if name.lower() == _AUTHORIZATION
    ]
    if not values:
        raise CallerUnverifiedError(CallerRefusalCause.BEARER_MISSING)
    value = values[0] if len(values) == 1 else None
    if isinstance(value, list):
        items = cast("list[object]", value)
        value = items[0] if len(items) == 1 else None
    if not isinstance(value, str) or len(value) > _MAX_BEARER_CHARACTERS:
        raise CallerUnverifiedError(CallerRefusalCause.BEARER_MALFORMED)
    scheme, _, token = value.partition(" ")
    if scheme.lower() != _BEARER_SCHEME or not token or token != "".join(token.split()):
        raise CallerUnverifiedError(CallerRefusalCause.BEARER_MALFORMED)
    return token


@dataclass(frozen=True, slots=True)
class CallerAdmission:
    """The outcome of checking one call's caller: a principal, or the refusal to return."""

    principal: CallerPrincipal | None
    refusal: ValidationError | None = None

    def __post_init__(self) -> None:
        if (self.principal is None) == (self.refusal is None):
            raise ValueError("An admission carries exactly one of a principal and a refusal.")


class CallerVerifier:
    """Verify each Gateway call's caller against the configured issuer. One per container."""

    def __init__(  # noqa: PLR0913 - two key sources and a clock, each keyword-only beyond the first
        self,
        settings: ToolCallerSettings,
        public_key: PublicKeySource,
        *,
        cognito_keys: SigningKeySource | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._public_key = public_key
        self._cognito_keys = cognito_keys
        self._now = now
        self._keys: JsonWebKeySet | None = None

    def verify(self, context: object) -> CallerPrincipal:
        """Return the verified principal, or raise `CallerUnverifiedError`.

        Without a configured external MCP client this is exactly the token-service check. With
        one (ADR-150), the unverified header's `kid` only selects which check runs: the token
        service's pinned key id runs that check, and any other `kid` runs the Cognito check.
        Either check fully verifies before anything about the call is read.
        """
        token = bearer_token(context)
        if self._cognito_keys is None or not self._settings.mcp_client_id:
            return self._verify_issued(token)
        try:
            kid = signing_key_id(token)
        except IdentityError:
            raise CallerUnverifiedError(CallerRefusalCause.ASSERTION_REFUSED) from None
        if kid == self._settings.token_signing_key_id:
            return self._verify_issued(token)
        return self._verify_mcp_client(token, self._cognito_keys)

    def _verify_mcp_client(self, token: str, cognito_keys: SigningKeySource) -> CallerPrincipal:
        """Verify an external MCP client's Cognito access token and its entitlement (ADR-150).

        Signature against the pool's key set, `exp`, `iss`, `client_id` and `token_use=access`,
        then the `mcp_tool_use` level. An unknown `kid` refreshes the key set once, because the
        pool rotates its keys on its own schedule. The actor is the configured client, which the
        signed `client_id` was just checked to equal; no `act` claim exists on this path.
        """
        try:
            request = VerifyAccessTokenRequest(
                token=token,
                keys=cognito_keys.signing_keys(),
                expected_issuer=self._settings.mcp_cognito_issuer,
                expected_client_id=self._settings.mcp_client_id,
                now=self._now(),
            )
            try:
                identity = verify_access_token(request)
            except UnknownSigningKeyError:
                identity = verify_access_token(
                    replace(request, keys=cognito_keys.refresh_signing_keys())
                )
        except DependencyError as error:
            raise CallerUnverifiedError(
                CallerRefusalCause.KEY_SET_UNAVAILABLE, dependency=error
            ) from None
        except IdentityError:
            raise CallerUnverifiedError(CallerRefusalCause.ASSERTION_REFUSED) from None
        if ReviewAccessLevel.MCP_TOOL_USE not in identity.review_levels:
            raise CallerUnverifiedError(CallerRefusalCause.ENTITLEMENT_MISSING)
        return CallerPrincipal(subject=identity.subject, actor=self._settings.mcp_client_id)

    def _verify_issued(self, token: str) -> CallerPrincipal:
        """Verify a token-service Gateway token, exactly as before ADR-150."""
        keys = self._key_set()
        try:
            return verify_caller(
                VerifyIssuedTokenRequest(
                    token=token,
                    keys=keys,
                    expected_issuer=self._settings.token_issuer,
                    expected_key_id=self._settings.token_signing_key_id,
                    expected_audience=self._settings.gateway_token_audience,
                    expected_client_id=self._settings.agent_client_id,
                    now=self._now(),
                )
            )
        except IdentityError:
            # The domain's message names the check that failed; the cause is all that is kept.
            raise CallerUnverifiedError(CallerRefusalCause.ASSERTION_REFUSED) from None

    def admit(self, context: object, report: Report) -> CallerAdmission:
        """Verify the caller, reporting a refusal once through the shared error seam.

        A caller's failure is reported as the `ValidationError` it becomes. An unreadable key set
        is reported as its own dependency or invariant failure, which alarms, and the call is
        still refused: a tool never runs for a caller it could not check.
        """
        try:
            return CallerAdmission(principal=self.verify(context))
        except CallerUnverifiedError as error:
            refusal = ValidationError(
                CALLER_UNVERIFIED,
                CALLER_UNVERIFIED_DESCRIPTION,
                context={"cause": error.cause.value},
            )
            report_error(refusal if error.dependency is None else error.dependency, report)
            return CallerAdmission(principal=None, refusal=refusal)

    def _key_set(self) -> JsonWebKeySet:
        """Return the cached key set, reading the public key on first use."""
        if self._keys is None:
            try:
                der = self._public_key.public_key_der()
                jwk = jwk_from_der_public_key(der, self._settings.token_signing_key_id)
            except DependencyError as error:
                raise CallerUnverifiedError(
                    CallerRefusalCause.KEY_SET_UNAVAILABLE, dependency=error
                ) from None
            except ValueError:
                raise CallerUnverifiedError(
                    CallerRefusalCause.KEY_SET_UNAVAILABLE,
                    dependency=InvariantError(
                        CALLER_KEY_SET_UNAVAILABLE,
                        "The tool's caller verification key is unusable.",
                        context={"cause": "public_key_not_rs256"},
                    ),
                ) from None
            self._keys = JsonWebKeySet(keys=(jwk,))
        return self._keys


def kms_caller_verifier(settings: ToolCallerSettings) -> CallerVerifier:
    """Compose the production verifier: the key set is read from the signing key in KMS.

    Constructing the client makes no call; the first verification does. The key is addressed by
    its ARN, which is the one resource the tool role may call `kms:GetPublicKey` on. The user
    pool's key set is composed only when an external MCP client is configured (ADR-150); it is
    public HTTPS, so the tool role gains no grant for it.
    """
    return CallerVerifier(
        settings,
        KmsTokenSigner(settings.token_signing_key_arn, region=settings.token_signing_key_region),
        cognito_keys=(
            CognitoSigningKeys(settings.mcp_cognito_jwks_url) if settings.mcp_client_id else None
        ),
    )
