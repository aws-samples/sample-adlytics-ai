# SPDX-License-Identifier: MIT-0
"""Tests for the composed authorization decision.

The ordering tests matter as much as the outcome tests. Signature and claims are
verified before CSRF, so a request bearing a forged token never reaches the CSRF
comparison — and the failure it reports is the token, not the CSRF token.
"""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest
from conftest import CLIENT_ID, ISSUER, NOW, SUBJECT, TokenMinter

from domain.identity.authorization import (
    SAFE_METHODS,
    AuthorizationRequest,
    AuthorizedSession,
    authorize_session,
    requires_csrf,
)
from domain.identity.session import Identity, IdentityError, SessionAbsentError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]

CSRF = "csrf-token-value"
APP_ORIGIN = "https://app.customer.com"


def _request(minter: TokenMinter, *, method: str = "GET", **kwargs: object) -> AuthorizationRequest:
    token = kwargs.get("token", minter.token())
    cookie_header = kwargs.get("cookie_header", f"adl_session={token}; adl_csrf={CSRF}")
    return AuthorizationRequest(
        cookie_header=str(cookie_header),
        method=method,
        keys=minter.jwks(),
        expected_issuer=ISSUER,
        expected_client_id=CLIENT_ID,
        now=NOW,
        csrf_header=str(kwargs.get("csrf_header", "")),
        allowed_origin=str(kwargs.get("allowed_origin", APP_ORIGIN)),
        origin=str(kwargs.get("origin", "")),
        sec_fetch_site=str(kwargs.get("sec_fetch_site", "same-origin")),
    )


def _identity(request: AuthorizationRequest) -> Identity:
    return authorize_session(request).identity


class TestRequiresCsrf:
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods_are_exempt(self, method: str) -> None:
        assert requires_csrf(method) is False

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_mutating_methods_require_it(self, method: str) -> None:
        assert requires_csrf(method) is True

    def test_an_unrecognised_method_requires_it(self) -> None:
        # Fail closed. Defined as an allow-list precisely so a method nobody
        # anticipated needs CSRF rather than bypassing it.
        assert requires_csrf("QUERY") is True

    def test_case_and_whitespace_do_not_bypass_it(self) -> None:
        assert requires_csrf(" post ") is True
        assert requires_csrf("get") is False

    def test_the_safe_set_is_exactly_the_three_read_methods(self) -> None:
        # Pinned by value: adding a method here silently disables CSRF for it.
        assert frozenset({"GET", "HEAD", "OPTIONS"}) == SAFE_METHODS


class TestAuthorizeReadRequests:
    def test_a_valid_token_authorizes_a_read(self, minter: TokenMinter) -> None:
        assert _identity(_request(minter)).subject == SUBJECT

    def test_a_read_needs_no_csrf_header(self, minter: TokenMinter) -> None:
        identity = _identity(_request(minter, cookie_header=f"adl_session={minter.token()}"))
        assert identity.subject == SUBJECT


class TestAuthorizeMutatingRequests:
    def test_a_matching_csrf_token_authorizes(self, minter: TokenMinter) -> None:
        identity = _identity(_request(minter, method="POST", csrf_header=CSRF))
        assert identity.subject == SUBJECT

    def test_a_mismatched_csrf_token_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="CSRF"):
            _identity(_request(minter, method="POST", csrf_header="not-the-token"))

    def test_an_absent_csrf_header_is_rejected(self, minter: TokenMinter) -> None:
        # Absent is a failure, not a default. An attacker who can omit the header
        # must not thereby succeed.
        with pytest.raises(IdentityError, match="CSRF"):
            _identity(_request(minter, method="POST"))

    def test_an_absent_csrf_cookie_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="CSRF"):
            _identity(
                _request(
                    minter,
                    method="POST",
                    cookie_header=f"adl_session={minter.token()}",
                    csrf_header=CSRF,
                )
            )


class TestIdentitySource:
    def test_a_request_without_the_session_cookie_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="adl_session"):
            _identity(_request(minter, cookie_header="adl_csrf=only"))

    def test_an_empty_cookie_header_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="adl_session"):
            _identity(_request(minter, cookie_header=""))

    def test_an_empty_session_cookie_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="adl_session"):
            _identity(_request(minter, cookie_header="adl_session=; adl_csrf=x"))

    @pytest.mark.covers("ADR-077")
    @pytest.mark.parametrize("cookie_header", ["", "adl_csrf=only", "adl_session=; adl_csrf=x"])
    def test_only_an_absent_session_cookie_is_the_session_absent_denial(
        self, minter: TokenMinter, cookie_header: str
    ) -> None:
        """ADR-007: only this denial means "no session to refresh" and may answer 401."""
        with pytest.raises(SessionAbsentError, match="adl_session"):
            _identity(_request(minter, cookie_header=cookie_header))

    @pytest.mark.covers("ADR-077")
    def test_a_present_but_expired_session_cookie_is_not_the_session_absent_denial(
        self, minter: TokenMinter
    ) -> None:
        """An expired session must be refreshed, so it must never read as an absent one."""
        expired = minter.token(exp=int(NOW.timestamp()))
        assert _identity(_request(minter)).subject == SUBJECT

        with pytest.raises(IdentityError, match="expired") as caught:
            _identity(_request(minter, token=expired))
        assert not isinstance(caught.value, SessionAbsentError)


class TestOrdering:
    def test_a_forged_token_fails_on_the_token_not_the_csrf(self, minter: TokenMinter) -> None:
        """Proves claims are validated before CSRF.

        If the order were reversed, a request with a valid CSRF pair and a forged
        token would report a CSRF failure — sending the next reader to entirely
        the wrong problem.
        """
        with pytest.raises(IdentityError, match="signature"):
            _identity(
                _request(
                    minter,
                    method="POST",
                    token=minter.token(wrong_key=True),
                    cookie_header=f"adl_session={minter.token(wrong_key=True)}; adl_csrf={CSRF}",
                    csrf_header=CSRF,
                )
            )

    def test_an_expired_token_fails_before_csrf_is_considered(self, minter: TokenMinter) -> None:
        expired = minter.token(exp=int((NOW - timedelta(seconds=1)).timestamp()))
        with pytest.raises(IdentityError, match="expired"):
            _identity(
                _request(
                    minter,
                    method="POST",
                    cookie_header=f"adl_session={expired}; adl_csrf={CSRF}",
                    csrf_header="wrong-on-purpose",
                )
            )


class TestRequestValidation:
    def test_a_method_is_required(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="method"):
            AuthorizationRequest(
                cookie_header="adl_session=x",
                method="",
                keys=minter.jwks(),
                expected_issuer=ISSUER,
                expected_client_id=CLIENT_ID,
                now=NOW,
            )


class TestOriginCheck:
    """The second CSRF defence, independent of the double-submit token.

    Both must pass on a mutating request. The double-submit token is ours to get
    right; Sec-Fetch-Site is set by the browser and a page cannot forge it.
    """

    @pytest.mark.parametrize("value", ["same-origin", "same-site", "Same-Origin", " same-site "])
    def test_a_same_site_fetch_is_accepted(self, minter: TokenMinter, value: str) -> None:
        identity = _identity(
            _request(minter, method="POST", csrf_header=CSRF, sec_fetch_site=value)
        )
        assert identity.subject == SUBJECT

    @pytest.mark.parametrize("value", ["cross-site", "none"])
    def test_a_cross_site_fetch_is_rejected(self, minter: TokenMinter, value: str) -> None:
        # `none` means the user navigated directly, which is not a legitimate way
        # to reach a mutating endpoint even though it is not another site.
        with pytest.raises(IdentityError, match="did not originate"):
            _identity(_request(minter, method="POST", csrf_header=CSRF, sec_fetch_site=value))

    def test_a_matching_origin_is_accepted_when_fetch_metadata_is_absent(
        self, minter: TokenMinter
    ) -> None:
        identity = _identity(
            _request(minter, method="POST", csrf_header=CSRF, sec_fetch_site="", origin=APP_ORIGIN)
        )
        assert identity.subject == SUBJECT

    def test_a_foreign_origin_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="did not originate"):
            _identity(
                _request(
                    minter,
                    method="POST",
                    csrf_header=CSRF,
                    sec_fetch_site="",
                    origin="https://attacker.example",
                )
            )

    def test_neither_signal_present_is_rejected(self, minter: TokenMinter) -> None:
        # Fail closed. A request with no origin signals is not a browser request
        # from our application, and this platform has no other kind of client.
        with pytest.raises(IdentityError, match="did not originate"):
            _identity(_request(minter, method="POST", csrf_header=CSRF, sec_fetch_site=""))

    def test_a_read_is_unaffected(self, minter: TokenMinter) -> None:
        identity = _identity(_request(minter, sec_fetch_site="cross-site"))
        assert identity.subject == SUBJECT

    def test_a_mutating_request_without_an_allowed_origin_is_refused(
        self, minter: TokenMinter
    ) -> None:
        """Fail closed on our own misconfiguration.

        With no configured origin the comparison would pass for any value, so the
        check would look present while doing nothing.
        """
        with pytest.raises(IdentityError, match="allowed_origin"):
            _request(minter, method="POST", csrf_header=CSRF, allowed_origin="")


class TestOriginComparisonIsExact:
    """Found by mutation review: the origin check must be equality, nothing weaker.

    Both cases below passed against an ordering or identity comparison, so neither
    was covered by the tests above. One of them is a bypass.
    """

    def test_an_origin_sorting_before_ours_is_still_rejected(self, minter: TokenMinter) -> None:
        # Replacing `==` with `<=` survived every other test, because the attacker
        # origin used above happens to sort *after* ours. One that sorts before it
        # would have been accepted.
        with pytest.raises(IdentityError, match="did not originate"):
            _identity(
                _request(
                    minter,
                    method="POST",
                    csrf_header=CSRF,
                    sec_fetch_site="",
                    origin="https://aaa.example",
                )
            )

    def test_an_equal_but_distinct_string_is_accepted(self, minter: TokenMinter) -> None:
        # Replacing `==` with `is` also survived, because a literal reused in a
        # test is the same object. A header arriving over the wire never is, so
        # that mutation would deny every mutating request in production while the
        # suite stayed green.
        from_the_wire = "".join(list(APP_ORIGIN))
        assert from_the_wire is not APP_ORIGIN
        identity = _identity(
            _request(
                minter,
                method="POST",
                csrf_header=CSRF,
                sec_fetch_site="",
                origin=from_the_wire,
            )
        )
        assert identity.subject == SUBJECT


class TestAuthorizationRequestIsFrozen:
    def test_the_request_cannot_be_mutated(self, minter: TokenMinter) -> None:
        # A request whose method or origin could be changed after validation would
        # let a caller re-point the decision after the checks in __post_init__.
        request = _request(minter)
        with pytest.raises(FrozenInstanceError):
            request.method = "POST"  # type: ignore[misc]


@pytest.mark.covers("ADR-077")
class TestAuthorizeSession:
    def test_returns_the_exact_cookie_token_that_was_verified(self, minter: TokenMinter) -> None:
        token = minter.token(exp=int((NOW + timedelta(minutes=9)).timestamp()))
        session = authorize_session(
            _request(
                minter,
                method="POST",
                token=token,
                csrf_header=CSRF,
            )
        )

        assert session.access_token == token
        assert session.identity.subject == SUBJECT
        assert session.identity.issuer == ISSUER

    def test_it_carries_no_expiry_because_the_route_forwards_no_cookie_token(self) -> None:
        """ADR-077 §2: the Harness token's life is its own, so no freshness gate reads one."""
        assert set(AuthorizedSession.__dataclass_fields__) == {"identity", "access_token"}

    def test_the_token_never_appears_in_its_repr(self, minter: TokenMinter) -> None:
        token = minter.token()
        session = authorize_session(_request(minter, token=token))

        assert session.access_token == token
        assert token not in repr(session)
        assert token not in str(session)
        assert "access_token" not in repr(session)

    def test_csrf_is_still_enforced_after_the_token_is_verified(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="CSRF"):
            authorize_session(_request(minter, method="POST", csrf_header="not-the-token"))

    def test_a_forged_token_fails_on_the_signature_before_csrf(self, minter: TokenMinter) -> None:
        forged = minter.token(wrong_key=True)
        with pytest.raises(IdentityError, match="signature"):
            authorize_session(
                _request(minter, method="POST", token=forged, csrf_header="not-the-token")
            )
