# SPDX-License-Identifier: MIT-0
"""Who called a Gateway tool, as a verified issued token names them (ADR-077 §5).

The Gateway forwards the caller's bearer, a Gateway-audience token the token service minted for
the agent client, to every tool. A tool accepts no other identity. This module turns that token,
once `verify_issued_token` has checked its header, signature, expiry, issuer, audience, client, and
actor, into the principal the tool attributes its work to. Pure computation: the key set and the
clock arrive in the request.

Identity is attribution here, not entitlement. ADR-054's deployment-wide scope still decides what a
tool may read, and no rule in this module restricts what a verified subject may ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.identity.issued_tokens import VerifyIssuedTokenRequest, verify_issued_token

__all__ = ["CallerPrincipal", "verify_caller"]


@dataclass(frozen=True, slots=True)
class CallerPrincipal:
    """The verified end user and the client acting for them.

    `subject` is the Cognito `sub`, which ADR-077 §6 stamps on audit records as the principal. It
    is kept out of `repr` so a principal logged by accident reveals only that one exists.
    """

    subject: str = field(repr=False)
    actor: str

    def __post_init__(self) -> None:
        for label, value in (("subject", self.subject), ("actor", self.actor)):
            if not value.strip():
                raise ValueError(f"A caller principal needs a nonblank {label}.")


def verify_caller(request: VerifyIssuedTokenRequest) -> CallerPrincipal:
    """Verify the presented token as a caller of the tools, or raise `IdentityError`.

    `request.expected_client_id` is the agent client: the token's `client_id` and `act.sub` must
    both equal it, so the actor returned is that verified value and never a claim read unchecked.
    """
    verified = verify_issued_token(request)
    return CallerPrincipal(subject=verified.subject, actor=verified.client_id)
