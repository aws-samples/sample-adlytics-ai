# SPDX-License-Identifier: MIT-0
"""Tests for cookie construction and parsing.

The attribute assertions are the point. Every one of HttpOnly, Secure, SameSite
and Domain is load-bearing, and dropping any of them fails silently in a way no
functional test would notice — the browser simply stops sending the cookie, or
starts letting JavaScript read it.
"""

from dataclasses import FrozenInstanceError

import pytest

from domain.identity.cookies import (
    CSRF_COOKIE,
    REFRESH_COOKIE,
    SESSION_COOKIE,
    TRANSACTION_COOKIE,
    TRANSACTION_MAX_AGE,
    CookiePolicy,
    CookieScope,
    SessionCookieRequest,
    _CookieToSet,  # pyright: ignore[reportPrivateUsage] — frozen check has no public path
    build_cleared_cookies,
    build_session_cookies,
    build_transaction_cookie,
    clear_transaction_cookie,
    parse_cookie_header,
)
from domain.identity.session import IdentityError
from domain.identity.tokens import TokenSet

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]

POLICY = CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=".customer.com")
HOST_ONLY = CookiePolicy(scope=CookieScope.HOST_ONLY)
TOKENS = TokenSet(access_token="access.jwt.value", expires_in=900, refresh_token="refresh-value")
REFRESH_MAX_AGE = 2_592_000


def _headers(tokens: TokenSet = TOKENS, policy: CookiePolicy = POLICY) -> dict[str, str]:
    """Set-Cookie headers keyed by cookie name, for attribute assertions."""
    built = build_session_cookies(
        SessionCookieRequest(
            policy=policy,
            tokens=tokens,
            csrf_token="csrf-value",
            refresh_max_age=REFRESH_MAX_AGE,
        )
    )
    return {header.split("=", 1)[0]: header for header in built}


def _by_name(headers: tuple[str, ...]) -> dict[str, str]:
    return {header.split("=", 1)[0]: header for header in headers}


class TestCookiePolicy:
    def test_a_domain_is_required_for_parent_scope(self) -> None:
        with pytest.raises(IdentityError, match="no default"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain="")

    def test_host_only_takes_no_domain(self) -> None:
        # An empty domain must not be able to mean two different things.
        with pytest.raises(IdentityError, match="take no domain"):
            CookiePolicy(scope=CookieScope.HOST_ONLY, domain=".customer.com")

    def test_host_only_needs_nothing_else(self) -> None:
        assert CookiePolicy(scope=CookieScope.HOST_ONLY).domain == ""

    @pytest.mark.parametrize(
        "domain", [".cloudfront.net", ".d111abcdef8.cloudfront.net", ".amazonaws.com", ".on.aws"]
    )
    def test_a_public_suffix_is_refused(self, domain: str) -> None:
        """The failure that has no visible symptom.

        A browser drops a Set-Cookie naming a public suffix, so the login appears
        to succeed and every request afterwards is denied. Host-only is the only
        legal scope on an AWS-provided hostname.
        """
        with pytest.raises(IdentityError, match="public suffix"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=domain)

    def test_a_domain_without_a_leading_dot_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="leading dot"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain="customer.com")

    def test_a_url_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="looks like a URL"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=".customer.com/path")

    def test_a_domain_with_a_port_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="looks like a URL"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=".customer.com:443")

    def test_localhost_is_rejected(self) -> None:
        """The failure mode that has bitten this team on real deployments.

        A localhost value means configuration did not resolve. These cookies are
        Secure and scoped to a registrable parent, so emitting it would produce a
        login that appears to work locally and silently fails once deployed.
        """
        with pytest.raises(IdentityError, match="localhost"):
            CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=".localhost")


class TestSessionCookieRequest:
    def test_a_csrf_token_is_required(self) -> None:
        with pytest.raises(IdentityError, match="CSRF"):
            SessionCookieRequest(
                policy=POLICY, tokens=TOKENS, csrf_token="", refresh_max_age=REFRESH_MAX_AGE
            )

    def test_refresh_max_age_must_be_positive(self) -> None:
        with pytest.raises(IdentityError, match="refresh_max_age"):
            SessionCookieRequest(policy=POLICY, tokens=TOKENS, csrf_token="csrf", refresh_max_age=0)


class TestSessionCookies:
    def test_all_three_are_issued_when_a_refresh_token_is_present(self) -> None:
        assert set(_headers()) == {SESSION_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}

    def test_the_refresh_cookie_is_omitted_when_not_reissued(self) -> None:
        # A refresh response carries no refresh token. Emitting an empty one
        # would overwrite a valid cookie and log the user out at the next hop.
        rotated = TokenSet(access_token="new.jwt.value", expires_in=900)
        assert set(_headers(rotated)) == {SESSION_COOKIE, CSRF_COOKIE}

    def test_the_session_cookie_is_http_only(self) -> None:
        assert "HttpOnly" in _headers()[SESSION_COOKIE]

    def test_the_refresh_cookie_is_http_only(self) -> None:
        assert "HttpOnly" in _headers()[REFRESH_COOKIE]

    def test_the_csrf_cookie_is_not_http_only(self) -> None:
        # Deliberately readable: JavaScript must echo it back as a header. This
        # is the one cookie where HttpOnly would break the design.
        assert "HttpOnly" not in _headers()[CSRF_COOKIE]

    @pytest.mark.parametrize("name", [SESSION_COOKIE, REFRESH_COOKIE, CSRF_COOKIE])
    def test_every_cookie_is_secure_lax_and_root_scoped(self, name: str) -> None:
        header = _headers()[name]
        assert "Secure" in header
        assert "SameSite=Lax" in header
        assert "SameSite=Strict" not in header
        assert "Path=/" in header
        assert "Domain=.customer.com" in header

    def test_the_session_lifetime_matches_the_token(self) -> None:
        assert "Max-Age=900" in _headers()[SESSION_COOKIE]

    def test_the_refresh_lifetime_comes_from_configuration(self) -> None:
        assert f"Max-Age={REFRESH_MAX_AGE}" in _headers()[REFRESH_COOKIE]

    def test_the_token_value_is_carried_verbatim(self) -> None:
        assert _headers()[SESSION_COOKIE].startswith(f"{SESSION_COOKIE}=access.jwt.value;")


class TestClearedCookies:
    def test_all_three_are_cleared(self) -> None:
        cleared = build_cleared_cookies(POLICY)
        assert {header.split("=", 1)[0] for header in cleared} == {
            SESSION_COOKIE,
            REFRESH_COOKIE,
            CSRF_COOKIE,
        }

    @pytest.mark.parametrize("header", build_cleared_cookies(POLICY))
    def test_each_expires_immediately_and_keeps_its_scope(self, header: str) -> None:
        # Scope must match the issued cookie exactly or the browser keeps the
        # original alongside the cleared one.
        assert "Max-Age=0" in header
        assert "Domain=.customer.com" in header
        assert "Path=/" in header


class TestParseCookieHeader:
    def test_parses_multiple_cookies(self) -> None:
        parsed = parse_cookie_header("adl_session=abc; adl_csrf=def")
        assert parsed == {"adl_session": "abc", "adl_csrf": "def"}

    def test_tolerates_missing_spaces(self) -> None:
        assert parse_cookie_header("a=1;b=2") == {"a": "1", "b": "2"}

    def test_an_empty_header_yields_nothing(self) -> None:
        assert parse_cookie_header("") == {}

    def test_a_value_containing_equals_is_preserved(self) -> None:
        # Base64 padding. Splitting on every `=` would truncate the token.
        assert parse_cookie_header("adl_session=ab==")["adl_session"] == "ab=="

    def test_malformed_pairs_are_dropped_not_raised(self) -> None:
        # An unrelated third-party cookie must not be able to fail the authorizer.
        assert parse_cookie_header("broken; adl_session=abc") == {"adl_session": "abc"}

    def test_a_valueless_cookie_is_kept_as_empty(self) -> None:
        assert parse_cookie_header("adl_session=") == {"adl_session": ""}


class TestTransactionCookie:
    """The fourth cookie, alive only between /auth/login and /auth/callback.

    It carries the PKCE verifier, so HttpOnly is not optional here: a script that
    could read it could complete an intercepted code exchange.
    """

    def test_it_is_http_only(self) -> None:
        assert "HttpOnly" in build_transaction_cookie(POLICY, "state.verifier")

    def test_it_is_secure_lax_and_root_scoped(self) -> None:
        header = build_transaction_cookie(POLICY, "state.verifier")
        assert "Secure" in header
        assert "SameSite=Lax" in header
        assert "Path=/" in header
        assert "Domain=.customer.com" in header

    def test_lax_rather_than_strict_is_what_makes_the_callback_work(self) -> None:
        # The return from the provider is a top-level navigation, which sends Lax
        # cookies but not Strict ones. Strict here would break every login.
        assert "SameSite=Strict" not in build_transaction_cookie(POLICY, "state.verifier")

    def test_it_expires_in_five_minutes(self) -> None:
        assert f"Max-Age={TRANSACTION_MAX_AGE}" in build_transaction_cookie(POLICY, "s.v")
        assert TRANSACTION_MAX_AGE == 300

    def test_an_empty_value_is_refused(self) -> None:
        # Setting an empty transaction cookie would make the next callback look
        # like a login that had started when it had not.
        with pytest.raises(IdentityError, match="empty"):
            build_transaction_cookie(POLICY, "")

    def test_clearing_expires_it_immediately(self) -> None:
        cleared = clear_transaction_cookie(POLICY)
        assert cleared.startswith(f"{TRANSACTION_COOKIE}=;")
        assert "Max-Age=0" in cleared

    def test_clearing_keeps_the_same_scope(self) -> None:
        # A different scope leaves the original cookie in place alongside the
        # cleared one, and the stale verifier stays usable.
        cleared = clear_transaction_cookie(POLICY)
        assert "Domain=.customer.com" in cleared
        assert "Path=/" in cleared

    def test_it_is_not_part_of_the_session_set(self) -> None:
        # Session cookies are issued by build_session_cookies; the transaction
        # cookie is deliberately not one of them.
        assert TRANSACTION_COOKIE not in _headers()


class TestGapsFoundByMutationTesting:
    """Cases whose absence let a mutant survive. See test-mutations/domain-identity-cookies.md.

    Each one pins an attribute this module's own docstrings call non-negotiable and
    that no test previously read back off a rendered header.
    """

    def test_a_host_only_policy_omits_the_domain_attribute_entirely(self) -> None:
        # `_set_cookie` branches on `scope is PARENT_DOMAIN`. CookieScope is a
        # StrEnum, so a mutated `<=` compares the member *values* as strings, and
        # "host_only" sorts below "parent_domain" — the branch is taken for a
        # host-only policy and every cookie goes out with a bare `Domain=`.
        # Browsers vary on what an empty Domain means, so this is the class of
        # failure that looks like a working login and denies every request after it.
        for header in _headers(policy=HOST_ONLY).values():
            assert "Domain" not in header

    def test_a_one_second_refresh_lifetime_is_accepted(self) -> None:
        # The boundary of `refresh_max_age < 1`. Nothing sat on it, so both a
        # shifted comparison and a changed constant rejected a legitimate
        # one-second lifetime undetected.
        built = build_session_cookies(
            SessionCookieRequest(
                policy=POLICY, tokens=TOKENS, csrf_token="csrf-value", refresh_max_age=1
            )
        )
        assert "Max-Age=1;" in _by_name(built)[CSRF_COOKIE]

    @pytest.mark.parametrize(
        ("name", "http_only"),
        [(SESSION_COOKIE, True), (REFRESH_COOKIE, True), (CSRF_COOKIE, False)],
    )
    def test_a_cleared_cookie_carries_the_http_only_flag_it_was_issued_with(
        self, name: str, http_only: bool
    ) -> None:
        # The clear path hard-codes its own HttpOnly table, independent of the one
        # in build_session_cookies, and nothing checked the two agree. A browser
        # matches a deletion on name, Domain and Path alone, so a mismatch changes
        # nothing it does today — what this prevents is the duplicated table
        # drifting away from the issued cookies unnoticed.
        assert ("HttpOnly" in _by_name(build_cleared_cookies(POLICY))[name]) is http_only

    def test_the_cleared_transaction_cookie_stays_http_only(self) -> None:
        # Same duplicated-flag risk on the transaction path: clear_transaction_cookie
        # states HttpOnly independently of build_transaction_cookie.
        assert "HttpOnly" in clear_transaction_cookie(POLICY)

    def test_a_policy_cannot_be_rewritten_after_validation(self) -> None:
        # __post_init__ is the only place the domain is checked against the public
        # suffix list, the leading dot and localhost. Mutable, a policy could pass
        # every one of those and then have its domain replaced before _set_cookie
        # renders it — validation that a caller can step around is not validation.
        policy = CookiePolicy(scope=CookieScope.PARENT_DOMAIN, domain=".customer.com")
        with pytest.raises(FrozenInstanceError):
            # The static error is the invariant; suppressed for this rule only so
            # the runtime behaviour behind it still gets asserted.
            policy.domain = ".cloudfront.net"  # pyright: ignore[reportAttributeAccessIssue]

    def test_a_session_request_cannot_be_rewritten_after_validation(self) -> None:
        # __post_init__ requires a CSRF token and a positive lifetime. Mutable, the
        # token could be emptied after the check and the cookie issued blank.
        request = SessionCookieRequest(
            policy=POLICY, tokens=TOKENS, csrf_token="csrf-value", refresh_max_age=REFRESH_MAX_AGE
        )
        with pytest.raises(FrozenInstanceError):
            request.csrf_token = ""  # pyright: ignore[reportAttributeAccessIssue]

    def test_a_cookie_to_set_cannot_have_its_flags_rewritten(self) -> None:
        # No validation to bypass, but http_only is decided by the caller that builds
        # it and must not be alterable between construction and rendering.
        cookie = _CookieToSet(name=SESSION_COOKIE, value="v", max_age=900, http_only=True)
        with pytest.raises(FrozenInstanceError):
            cookie.http_only = False  # pyright: ignore[reportAttributeAccessIssue]
