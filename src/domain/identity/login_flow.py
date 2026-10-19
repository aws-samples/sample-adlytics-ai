# SPDX-License-Identifier: MIT-0
"""What has to survive the redirect to the identity provider and back.

A login leaves our control at the redirect and returns as a bare `code` and
`state` on a URL. Two things must be remembered across that gap: the PKCE
verifier, so the code cannot be redeemed by whoever intercepts it, and the state,
so a callback nobody started is rejected.

Pure and stdlib only. Where the transaction is *kept* between the two requests is
an adapter decision; what it contains and how it is checked is decided here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from domain.identity.session import IdentityError

VERIFIER_BYTES = 32
"""Yields a 43-character verifier, the shortest RFC 7636 permits.

Below 43 characters a provider is entitled to reject the exchange, and the
failure appears at the end of a successful-looking login.
"""

MIN_VERIFIER_LENGTH = 43
"""RFC 7636's floor. A provider may reject anything shorter, and the rejection
lands at the exchange — after the login has appeared to succeed."""

STATE_BYTES = 16

CHALLENGE_METHOD = "S256"
"""Never `plain`.

With `plain` the challenge *is* the verifier, so anyone who sees the authorize
request can complete the exchange — which removes the only thing PKCE adds.
"""

_SEPARATOR = "."


@dataclass(frozen=True, slots=True)
class LoginTransaction:
    """The state and verifier belonging to one in-flight login."""

    state: str
    code_verifier: str

    def __post_init__(self) -> None:
        if not self.state:
            raise IdentityError("A login transaction requires a state value.")
        if len(self.code_verifier) < MIN_VERIFIER_LENGTH:
            raise IdentityError(
                "A PKCE verifier must be at least 43 characters; a shorter one is "
                "rejected at the exchange, after the login appears to have worked."
            )
        if _SEPARATOR in self.state or _SEPARATOR in self.code_verifier:
            raise IdentityError("Neither value may contain the encoding separator.")

    @property
    def code_challenge(self) -> str:
        """The S256 challenge sent to the authorize endpoint."""
        digest = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        return _b64url(digest)

    def encode(self) -> str:
        """Render for transport between the two requests.

        Not signed, and it does not need to be. It travels in an HttpOnly cookie
        on our own origin, which another site cannot set or read, and the security
        property comes from comparing the returned state against this copy rather
        than from the encoding.
        """
        return f"{self.state}{_SEPARATOR}{self.code_verifier}"

    @classmethod
    def decode(cls, raw: str) -> LoginTransaction:
        """Rebuild a transaction, rejecting anything malformed.

        A callback without a decodable transaction is a callback for a login this
        browser never started.
        """
        state, separator, verifier = raw.partition(_SEPARATOR)
        if not separator:
            raise IdentityError("The login transaction is malformed.")
        return cls(state=state, code_verifier=verifier)


def new_login_transaction() -> LoginTransaction:
    """Start a login. Both values come from a cryptographic source."""
    return LoginTransaction(
        state=_b64url(secrets.token_bytes(STATE_BYTES)),
        code_verifier=_b64url(secrets.token_bytes(VERIFIER_BYTES)),
    )


def state_matches(returned: str, transaction: LoginTransaction) -> bool:
    """Whether a callback belongs to the login this browser started.

    Constant-time, and an absent value is a failure rather than a pass. This is
    the whole defence against a forged callback, so "no state" must never mean
    "nothing to check".
    """
    if not returned or not transaction.state:
        return False
    return hmac.compare_digest(returned, transaction.state)


def _b64url(raw: bytes) -> str:
    """Unpadded base64url. Padding is not URL-safe and providers vary on it."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
