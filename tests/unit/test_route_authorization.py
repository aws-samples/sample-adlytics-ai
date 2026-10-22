# SPDX-License-Identifier: MIT-0
"""The API authorizer's route decision: route-key entitlement and one bound credential.

Each credential is valid with exactly one app client and a fixed route set (ADR-078 §2), and the
entitlement is chosen from the route API Gateway matched, never the raw path (ADR-078 §3). The
crossings are synthesised with real signatures from the unit keypair, never by stubbing the
verifier.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import CLIENT_ID, ISSUER, NOW, SUBJECT, TokenMinter

from domain.identity.authorization import (
    PROTECTED_PREFIXES,
    QUALITY_REVIEW_TURN_ROUTE,
    QUALITY_REVIEW_TURNS_ROUTE,
    ROUTE_ENTITLEMENTS,
    CredentialKind,
    RouteAuthorizationRequest,
    authorize_route,
    bearer_token,
    required_route_level,
)
from domain.identity.session import IdentityError, ReviewAccessLevel, SessionAbsentError
from domain.identity.tokens import UnknownSigningKeyError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

PUBLIC_CLIENT_ID = "2publicexample345678"
CSRF = "csrf-token-value"
APP_ORIGIN = "https://app.customer.com"
REVIEWER = ["quality_reviewer"]
NON_REVIEW_ROUTE = "POST /api/presentation/overview"


def _route(  # noqa: PLR0913 - every credential slot of one request, each defaulted per case
    minter: TokenMinter,
    *,
    route_key: str = QUALITY_REVIEW_TURN_ROUTE,
    method: str = "POST",
    cookie_header: str = "",
    authorization: str | None = None,
    csrf_header: str = "",
) -> RouteAuthorizationRequest:
    return RouteAuthorizationRequest(
        route_key=route_key,
        method=method,
        cookie_header=cookie_header,
        authorization_header=authorization,
        keys=minter.jwks(),
        expected_issuer=ISSUER,
        session_client_id=CLIENT_ID,
        bearer_client_id=PUBLIC_CLIENT_ID,
        now=NOW,
        csrf_header=csrf_header,
        allowed_origin=APP_ORIGIN,
        sec_fetch_site="same-origin",
    )


def _bff(minter: TokenMinter, **claims: Any) -> str:
    return minter.token(quality_review_levels=REVIEWER, **claims)


def _public(minter: TokenMinter, **claims: Any) -> str:
    return minter.token(client_id=PUBLIC_CLIENT_ID, quality_review_levels=REVIEWER, **claims)


def _cookie(token: str) -> str:
    return f"adl_session={token}; adl_csrf={CSRF}"


class TestCredentialMatrix:
    """The 2x2 of credential and route class: only the bound combinations authorize."""

    @pytest.mark.parametrize(
        "route_key", [QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE, NON_REVIEW_ROUTE]
    )
    def test_a_bff_cookie_authorizes_review_and_non_review_routes(
        self, minter: TokenMinter, route_key: str
    ) -> None:
        decision = authorize_route(
            _route(
                minter, route_key=route_key, cookie_header=_cookie(_bff(minter)), csrf_header=CSRF
            )
        )
        assert decision.identity.subject == SUBJECT
        assert decision.credential is CredentialKind.SESSION_COOKIE
        assert decision.review_route is (route_key != NON_REVIEW_ROUTE)

    @pytest.mark.parametrize("route_key", [QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE])
    def test_a_public_bearer_authorizes_both_review_routes_without_csrf(
        self, minter: TokenMinter, route_key: str
    ) -> None:
        decision = authorize_route(
            _route(minter, route_key=route_key, authorization=f"Bearer {_public(minter)}")
        )
        assert decision.credential is CredentialKind.BEARER
        assert decision.review_route is True
        assert decision.identity.review_levels == (ReviewAccessLevel.QUALITY_REVIEWER,)

    def test_a_public_bearer_is_refused_on_a_non_review_route(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="bearer is not valid"):
            authorize_route(
                _route(
                    minter, route_key=NON_REVIEW_ROUTE, authorization=f"Bearer {_public(minter)}"
                )
            )


class TestCrossings:
    @pytest.mark.parametrize("route_key", [QUALITY_REVIEW_TURN_ROUTE, NON_REVIEW_ROUTE])
    def test_a_bff_token_presented_as_a_bearer_is_refused(
        self, minter: TokenMinter, route_key: str
    ) -> None:
        with pytest.raises(IdentityError):
            authorize_route(
                _route(minter, route_key=route_key, authorization=f"Bearer {_bff(minter)}")
            )

    def test_a_public_token_presented_as_the_session_cookie_is_refused(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(IdentityError, match="client_id"):
            authorize_route(
                _route(minter, cookie_header=_cookie(_public(minter)), csrf_header=CSRF)
            )

    def test_a_session_cookie_and_a_bearer_together_are_refused(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="both"):
            authorize_route(
                _route(
                    minter,
                    cookie_header=_cookie(_bff(minter)),
                    authorization=f"Bearer {_public(minter)}",
                    csrf_header=CSRF,
                )
            )

    def test_an_empty_session_cookie_with_a_bearer_still_counts_as_both(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(IdentityError, match="both"):
            authorize_route(
                _route(
                    minter, cookie_header="adl_session=", authorization=f"Bearer {_public(minter)}"
                )
            )

    def test_a_request_with_neither_credential_is_refused_as_absent(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(SessionAbsentError):
            authorize_route(_route(minter, cookie_header="adl_csrf=only"))

    def test_an_unrelated_cookie_beside_a_bearer_is_accepted(self, minter: TokenMinter) -> None:
        decision = authorize_route(
            _route(
                minter,
                cookie_header="theme=dark; adl_csrf=unrelated",
                authorization=f"Bearer {_public(minter)}",
            )
        )
        assert decision.credential is CredentialKind.BEARER

    def test_a_bearer_on_a_non_review_route_never_reaches_key_selection(
        self, minter: TokenMinter
    ) -> None:
        """An unknown `kid` would ask for a JWKS refresh; the route binding refuses first."""
        unknown_kid = minter.token(kid="rotated-away", client_id=PUBLIC_CLIENT_ID)
        with pytest.raises(IdentityError) as caught:
            authorize_route(
                _route(minter, route_key=NON_REVIEW_ROUTE, authorization=f"Bearer {unknown_kid}")
            )
        assert not isinstance(caught.value, UnknownSigningKeyError)

    def test_an_unknown_kid_on_a_bearer_is_the_retryable_failure(self, minter: TokenMinter) -> None:
        token = minter.token(kid="rotated-in", client_id=PUBLIC_CLIENT_ID)
        with pytest.raises(UnknownSigningKeyError):
            authorize_route(_route(minter, authorization=f"Bearer {token}"))


class TestBearerHeader:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
    def test_the_scheme_is_case_insensitive(self, minter: TokenMinter, scheme: str) -> None:
        token = _public(minter)
        assert bearer_token(f"{scheme} {token}") == token

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "Bearer",
            "Bearer ",
            "Basic dXNlcjpwYXNz",
            "Bearera.b.c",
            "Bearer a.b.",
            "Bearer a.b.c d.e.f",
            "Bearer a.b.c,Bearer d.e.f",
            "Token a.b.c",
        ],
    )
    def test_anything_but_exactly_one_compact_token_is_refused(self, header: str) -> None:
        with pytest.raises(IdentityError, match="exactly one bearer"):
            bearer_token(header)

    def test_an_unsigned_token_is_refused_at_the_header(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="exactly one bearer"):
            bearer_token(f"Bearer {minter.unsigned_token(client_id=PUBLIC_CLIENT_ID)}")

    def test_a_present_but_malformed_header_is_a_credential_not_an_absence(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(IdentityError, match="exactly one bearer") as caught:
            authorize_route(_route(minter, authorization="Basic dXNlcjpwYXNz"))
        assert not isinstance(caught.value, SessionAbsentError)


class TestRouteEntitlement:
    def test_every_mapped_route_key_is_under_a_protected_prefix(self) -> None:
        for route_key in ROUTE_ENTITLEMENTS:
            path = route_key.split(" ", 1)[1]
            assert path.startswith(PROTECTED_PREFIXES)

    @pytest.mark.parametrize(
        ("route_key", "level"),
        [
            (QUALITY_REVIEW_TURN_ROUTE, ReviewAccessLevel.QUALITY_REVIEWER),
            (QUALITY_REVIEW_TURNS_ROUTE, ReviewAccessLevel.QUALITY_REVIEWER),
            ("POST /api/prompt-registry/activate", ReviewAccessLevel.PROMPT_ADMIN),
            ("POST /api/prompt-registry/extensions/calibrate", ReviewAccessLevel.PROMPT_ADMIN),
            ("GET /api/operations/data-flows", ReviewAccessLevel.OPERATIONS_STATUS_READ),
            (
                "GET /api/operations/data-flows/{flow_id}/events",
                ReviewAccessLevel.OPERATIONS_STATUS_READ,
            ),
            (NON_REVIEW_ROUTE, None),
            ("GET /api/me", None),
        ],
    )
    def test_the_required_level_comes_from_the_route_key(
        self, route_key: str, level: ReviewAccessLevel | None
    ) -> None:
        assert required_route_level(route_key) is level

    @pytest.mark.parametrize(
        "route_key",
        [
            "POST /api/quality-review/audit",
            "POST /api/quality-review/findings",
            "POST /api/quality-review/evaluation",
            "GET /api/quality-review/turn",
            "POST /api/quality-review",
            "POST /api/prompt-registry/delete",
            "GET /api/operations/secrets",
            "ANY /api/operations/{proxy+}",
        ],
    )
    def test_an_unmapped_route_under_a_protected_prefix_is_denied(
        self, minter: TokenMinter, route_key: str
    ) -> None:
        with pytest.raises(IdentityError, match="no entitlement mapping"):
            authorize_route(
                _route(
                    minter,
                    route_key=route_key,
                    cookie_header=_cookie(
                        minter.token(
                            quality_review_levels=[level.value for level in ReviewAccessLevel]
                        )
                    ),
                    csrf_header=CSRF,
                )
            )

    @pytest.mark.parametrize("route_key", ["", "$default", "POST", "POST api/quality-review/turn"])
    def test_a_malformed_route_key_is_denied(self, route_key: str) -> None:
        with pytest.raises(IdentityError):
            required_route_level(route_key)

    @pytest.mark.parametrize(
        "levels", [[], ["audit_attribution"], ["prompt_admin", "operations_status_read"]]
    )
    @pytest.mark.parametrize("route_key", [QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE])
    def test_a_review_route_requires_quality_reviewer_on_either_credential(
        self, minter: TokenMinter, route_key: str, levels: list[str]
    ) -> None:
        """`audit_attribution` alone grants nothing (ADR-078 §3)."""
        public = minter.token(client_id=PUBLIC_CLIENT_ID, quality_review_levels=levels)
        with pytest.raises(IdentityError, match="entitlement"):
            authorize_route(_route(minter, route_key=route_key, authorization=f"Bearer {public}"))
        bff = minter.token(quality_review_levels=levels)
        with pytest.raises(IdentityError, match="entitlement"):
            authorize_route(
                _route(minter, route_key=route_key, cookie_header=_cookie(bff), csrf_header=CSRF)
            )

    def test_quality_review_access_does_not_broaden_into_prompt_administration(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(IdentityError, match="entitlement"):
            authorize_route(
                _route(
                    minter,
                    route_key="POST /api/prompt-registry/activate",
                    cookie_header=_cookie(_bff(minter)),
                    csrf_header=CSRF,
                )
            )

    @pytest.mark.covers("ADR-150")
    def test_no_route_requires_the_mcp_tool_use_level(self) -> None:
        assert ReviewAccessLevel.MCP_TOOL_USE not in set(ROUTE_ENTITLEMENTS.values())

    @pytest.mark.covers("ADR-150")
    @pytest.mark.parametrize("route_key", sorted(ROUTE_ENTITLEMENTS))
    def test_the_mcp_tool_use_level_alone_opens_no_entitled_route(
        self, minter: TokenMinter, route_key: str
    ) -> None:
        """ADR-150: the external MCP entitlement is admitted at that gateway only.

        Refused at the route's entitlement step with the same signal a missing level gets; the
        control in the same test is the route's own level on the same credential.
        """
        method = route_key.split(" ", 1)[0]
        required = ROUTE_ENTITLEMENTS[route_key]
        mcp_only = minter.token(quality_review_levels=[ReviewAccessLevel.MCP_TOOL_USE.value])
        with pytest.raises(IdentityError, match=r"^Route entitlement is absent\.$"):
            authorize_route(
                _route(
                    minter,
                    route_key=route_key,
                    method=method,
                    cookie_header=_cookie(mcp_only),
                    csrf_header=CSRF,
                )
            )
        entitled = minter.token(
            quality_review_levels=[ReviewAccessLevel.MCP_TOOL_USE.value, required.value]
        )
        decision = authorize_route(
            _route(
                minter,
                route_key=route_key,
                method=method,
                cookie_header=_cookie(entitled),
                csrf_header=CSRF,
            )
        )
        assert decision.identity.subject == SUBJECT

    @pytest.mark.parametrize("retired", ["audit", "findings", "evaluation"])
    def test_a_retired_level_is_refused_on_every_route(
        self, minter: TokenMinter, retired: str
    ) -> None:
        """The claim is closed: a token naming a retired per-level value verifies nowhere."""
        token = minter.token(quality_review_levels=["quality_reviewer", retired])
        with pytest.raises(IdentityError, match="malformed"):
            authorize_route(
                _route(minter, route_key="GET /api/me", method="GET", cookie_header=_cookie(token))
            )
        for route_key in (QUALITY_REVIEW_TURN_ROUTE, QUALITY_REVIEW_TURNS_ROUTE):
            with pytest.raises(IdentityError, match="malformed"):
                authorize_route(
                    _route(
                        minter,
                        route_key=route_key,
                        cookie_header=_cookie(token),
                        csrf_header=CSRF,
                    )
                )


class TestCsrfBinding:
    def test_the_cookie_on_a_review_route_still_needs_csrf(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="CSRF"):
            authorize_route(_route(minter, cookie_header=_cookie(_bff(minter)), csrf_header=""))

    def test_a_forged_cookie_fails_on_the_signature_before_csrf(self, minter: TokenMinter) -> None:
        forged = minter.token(wrong_key=True, quality_review_levels=REVIEWER)
        with pytest.raises(IdentityError, match="signature"):
            authorize_route(_route(minter, cookie_header=_cookie(forged), csrf_header="wrong"))

    def test_a_read_on_a_non_review_route_needs_no_csrf(self, minter: TokenMinter) -> None:
        decision = authorize_route(
            _route(
                minter,
                route_key="GET /api/me",
                method="GET",
                cookie_header=f"adl_session={_bff(minter)}",
            )
        )
        assert decision.credential is CredentialKind.SESSION_COOKIE


class TestRequestValidation:
    def test_both_client_ids_are_required(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="client ids"):
            RouteAuthorizationRequest(
                route_key=QUALITY_REVIEW_TURN_ROUTE,
                method="GET",
                cookie_header="",
                authorization_header=None,
                keys=minter.jwks(),
                expected_issuer=ISSUER,
                session_client_id=CLIENT_ID,
                bearer_client_id="",
                now=NOW,
            )

    def test_a_route_key_is_required(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="route_key"):
            RouteAuthorizationRequest(
                route_key="",
                method="GET",
                cookie_header="",
                authorization_header=None,
                keys=minter.jwks(),
                expected_issuer=ISSUER,
                session_client_id=CLIENT_ID,
                bearer_client_id=PUBLIC_CLIENT_ID,
                now=NOW,
            )
