# SPDX-License-Identifier: MIT-0
"""The deployed API surface, through CloudFront.

Everything here runs without a user and without a session, which is not a
limitation to work around — it is the consequence of a deliberate decision. No
deployment enables a password flow on the app client, so nothing can mint a token
headlessly, so no automated test can hold a valid session. See
docs/decisions/adr-007-auth-cookie-bff.md for why that trade was taken.

What these do cover is every assumption the unit tests cannot reach: that the
routes are live at the paths the code expects, that a cookie can be set at all on
an AWS-provided hostname, that the authorizer is attached and denies with 403, and
that CloudFront is not rewriting our error statuses.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import pytest

from adapters.config import Settings
from adapters.inbound.auth_handlers.routing import API_PREFIX
from adapters.outbound.https import HttpsSession, require_https

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-007", "ADR-014")]

TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class Reply:
    """A response reduced to what these assertions need."""

    status: int
    headers: dict[str, str]
    set_cookies: tuple[str, ...]

    def cookie(self, name: str) -> str:
        """Return the Set-Cookie header for one cookie, or empty if it was not set."""
        return next((c for c in self.set_cookies if c.startswith(f"{name}=")), "")


def read_body(url: str) -> str:
    """Fetch a page's body as text, following only HTTPS redirects.

    Separate from `call`, which deliberately reports only status, headers and
    cookies — the auth assertions have no business reading response bodies.
    A 4xx or 5xx raises, as an error status must fail the test that reads it.

    `trust_env` is off before any request is sent, so no `.netrc` credential,
    proxy or CA-bundle setting from the operator's environment applies, on the
    first request or on any redirect hop. These tests therefore need a direct
    network: behind a proxy or a TLS-intercepting endpoint they fail with a
    connection or certificate error, never a silent pass (O-TEST-TRANSPORT-001).
    """
    with HttpsSession() as http:
        http.trust_env = False
        with http.get(require_https(url), timeout=TIMEOUT, allow_redirects=True) as response:
            response.raise_for_status()
            return response.text


def cookie_header(**pairs: str) -> dict[str, str]:
    """Build a Cookie header.

    Whether the header is present at all is what decides 401 versus 403, so it is
    assembled in one place rather than spelled out per call site.
    """
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in pairs.items())}


def call(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> Reply:
    """Fetch over HTTPS without following redirects.

    Redirects must not be followed: the 302 and its Set-Cookie headers are the
    thing under test, and following them would swallow them and chase Cognito.
    The URL must be HTTPS before any Cookie header is sent, the same boundary
    the validation probes use.

    Each call gets its own session. A shared one would keep every Set-Cookie in
    its jar and replay it, so a test that sends no Cookie header would send one.
    `trust_env` is off before the request, as in `read_body`.
    """
    target = require_https(url)
    with HttpsSession() as http:
        http.trust_env = False
        with http.request(
            method,
            target,
            headers=headers or {},
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            return Reply(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                # requests folds repeated headers; the raw headers keep each Set-Cookie.
                set_cookies=tuple(response.raw.headers.getlist("Set-Cookie")),
            )


@pytest.fixture(scope="module")
def base(settings: Settings) -> str:
    """Return the public origin. Everything goes through CloudFront, never execute-api."""
    return settings.app_base_url


class TestLoginRedirect:
    def test_it_redirects_to_the_provider(self, base: str, settings: Settings) -> None:
        reply = call(f"{base}{API_PREFIX}/auth/login")
        assert reply.status == 302
        assert reply.headers["location"].startswith(settings.cognito_authorize_url)

    def test_it_asks_for_a_code_with_pkce(self, base: str) -> None:
        # An implicit grant would return tokens in a URL fragment, which is the
        # exact thing this architecture exists to prevent.
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(call(f"{base}{API_PREFIX}/auth/login").headers["location"]).query
        )
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0]

    def test_the_redirect_uri_matches_what_the_client_registered(
        self, base: str, settings: Settings
    ) -> None:
        # Cognito compares this string exactly. A one-character difference is a
        # rejected login rather than a warning, and it is the kind of mismatch that
        # only ever shows up here.
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(call(f"{base}{API_PREFIX}/auth/login").headers["location"]).query
        )
        assert query["redirect_uri"] == [settings.callback_redirect_uri]

    def test_it_sets_the_transaction_cookie(self, base: str) -> None:
        """The single most important assertion in this file.

        `cloudfront.net` is on the Public Suffix List, so a cookie naming it as a
        Domain is dropped silently. This proves a cookie can be set at all on the
        deployed hostname — reasoned from the PSL up to now, never observed.
        """
        cookie = call(f"{base}{API_PREFIX}/auth/login").cookie("adl_txn")
        assert cookie, "no adl_txn cookie was set on the login redirect"
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie
        # Host-only: no Domain attribute at all.
        assert "Domain=" not in cookie

    def test_the_response_is_not_cacheable(self, base: str) -> None:
        assert "no-store" in call(f"{base}{API_PREFIX}/auth/login").headers.get("cache-control", "")


class TestTheStaticSiteCachesCorrectly:
    """HTML must revalidate; hashed assets must not.

    With no Cache-Control the browser applies heuristic freshness, so it can hold HTML
    that points at chunk names the next deploy has already deleted. The scripts 404,
    React never hydrates, and the page sits on its pre-render — which presents as a
    redirect bug after login, not as a caching bug.
    """

    def test_html_is_revalidated_on_every_load(self, base: str) -> None:
        directive = call(f"{base}/").headers.get("cache-control", "")
        # no-cache means "revalidate", not "do not store". The ETag makes it a 304.
        assert "no-cache" in directive or "max-age=0" in directive

    def test_hashed_assets_are_immutable(self, base: str) -> None:
        # Safe only because the filename contains a content hash: a changed file is a
        # different URL, so a year-long TTL can never serve the wrong bytes.
        body = read_body(f"{base}/")
        match = re.search(r'src="(/_next/static/chunks/[^"]+\.js)"', body)
        assert match is not None
        directive = call(f"{base}{match.group(1)}").headers.get("cache-control", "")
        assert "immutable" in directive


class TestManagedLoginActuallyRenders:
    """The one hop that is entirely Cognito's, and the one that failed silently.

    A pool using managed login v2 without a branding style serves 403 from /login
    while /oauth2/authorize keeps redirecting, so the pool, client and domain all
    report healthy. Nothing short of fetching the page catches it.
    """

    def test_the_login_page_renders_a_sign_in_form(self, base: str) -> None:
        location = call(f"{base}{API_PREFIX}/auth/login").headers.get("location", "")
        assert location.startswith("https://"), "login did not redirect to the provider"

        page = read_body(location)
        # A password field is the proof it rendered. Asserting on the absence of an
        # error string would pass on any unrelated failure page.
        assert 'type="password"' in page

    def test_the_login_page_is_not_the_branding_error(self, base: str) -> None:
        # "Login pages unavailable" also appears in the page's bundled localisation
        # strings even when it renders correctly, so its presence proves nothing. The
        # rendered error page has no form to post to.
        location = call(f"{base}{API_PREFIX}/auth/login").headers.get("location", "")
        page = read_body(location)
        assert "<form" in page


class TestTheServedPageSatisfiesItsOwnCsp:
    """The CSP is delivered by CloudFront; the HTML is produced by a separate build.

    Nothing else checks that the two agree, and a disagreement is invisible to every
    gate: the deploy succeeds, the page returns 200, and the app is dead in the
    browser. That happened once already — Next.js inlines its Flight payload as
    `<script>self.__next_f.push(...)`, `script-src 'self'` blocked it, and React
    failed to hydrate with error #412.
    """

    def test_the_csp_forbids_inline_script(self, base: str) -> None:
        # If this ever gains 'unsafe-inline', the test below stops meaning anything,
        # so the premise is asserted rather than assumed.
        policy = call(base).headers.get("content-security-policy", "")
        assert "script-src 'self'" in policy
        assert "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]

    def test_the_served_html_carries_no_source_less_script(self, base: str) -> None:
        # Any source-less executable script is blocked by the CSP, including the
        # async serialization emitted by current Next exports.
        body = read_body(f"{base}/")
        assert (
            re.search(
                r"<script\b(?![^>]*\bsrc\s*=)[^>]*>",
                body,
                flags=re.IGNORECASE,
            )
            is None
        ), "served HTML contains a source-less script CSP will block"

    def test_the_externalised_bootstrap_is_reachable(self, base: str) -> None:
        # Externalising is only a fix if the file it points at actually serves. A
        # dangling reference is the same blank page by a different route.
        body = read_body(f"{base}/")
        match = re.search(r'src="(/_next/static/chunks/inline\.[a-f0-9]+\.js)"', body)
        assert match is not None, "no externalised bootstrap chunk referenced"
        assert call(f"{base}{match.group(1)}").status == 200


class TestAuthorizerIsAttached:
    def test_a_protected_route_denies_when_no_cookie_is_sent_at_all(self, base: str) -> None:
        # 403 until the edge slice (ADR-007 2026-09-28 amendment): the authorizer has no
        # identity source, because a review-route bearer carries no cookie, so a request
        # with no credential reaches it and is refused. The frontend refreshes once, which
        # fails without cookies, then begins login.
        assert call(f"{base}{API_PREFIX}/me").status == 403

    def test_a_rejected_cookie_denies_with_403_so_refresh_can_fire(self, base: str) -> None:
        # 403 is the frontend's refresh trigger. A cookie that arrives and fails
        # validation must produce it, or an expired session becomes a hard failure
        # instead of a retry.
        reply = call(f"{base}{API_PREFIX}/me", headers=cookie_header(adl_session="not-a-jwt"))
        assert reply.status == 403

    def test_any_cookie_at_all_reaches_the_authorizer(self, base: str) -> None:
        # Why an expired session still refreshes rather than bouncing to login:
        # adl_session expires with its access token, but adl_refresh outlives it, so
        # a Cookie header is still present and the request reaches the authorizer.
        # That ordering is load-bearing and nothing in the cookie code enforces it,
        # so it is asserted here — a cookie unrelated to the session is enough.
        reply = call(f"{base}{API_PREFIX}/me", headers=cookie_header(adl_csrf="irrelevant"))
        assert reply.status == 403

    def test_a_forged_session_cookie_is_also_denied(self, base: str) -> None:
        # Cannot distinguish "cookie arrived and was rejected" from "cookie never
        # arrived" by status alone — both deny identically, on purpose. The
        # difference is in the authorizer's logged reason. See §4a1.
        reply = call(
            f"{base}{API_PREFIX}/me",
            headers={"Cookie": "adl_session=not.a.real.token"},
        )
        assert reply.status == 403

    def test_the_denial_reveals_nothing(self, base: str) -> None:
        reply = call(f"{base}{API_PREFIX}/me")
        body = " ".join(reply.headers.values()).lower()
        for leak in ("cognito", "userpool", "client_id", "jwks"):
            assert leak not in body


class TestRouteTable:
    def test_an_unmapped_path_is_a_404(self, base: str) -> None:
        # Proves the live route table matches ROUTES rather than a prefix match
        # quietly forwarding everything to one handler.
        assert call(f"{base}{API_PREFIX}/auth/nonexistent").status == 404

    def test_the_wrong_method_does_not_reach_the_handler(self, base: str) -> None:
        assert call(f"{base}{API_PREFIX}/auth/login", method="POST").status in (403, 404, 405)

    def test_refresh_without_csrf_is_refused(self, base: str) -> None:
        assert call(f"{base}{API_PREFIX}/auth/refresh", method="POST").status == 403

    def test_logout_without_csrf_is_refused(self, base: str) -> None:
        assert call(f"{base}{API_PREFIX}/auth/logout", method="POST").status == 403


class TestFrontendIsServed:
    def test_the_root_serves_the_app(self, base: str) -> None:
        assert call(f"{base}/").status == 200

    def test_a_missing_page_is_not_rewritten_to_a_200(self, base: str) -> None:
        """The trap this whole edge design exists to avoid.

        A distribution-level error mapping would rewrite 403 and 404 to index.html
        with a 200 — and 403 is our refresh trigger. If this returns 200, the
        mapping is present and every expired session silently becomes a page of
        HTML instead of a signal to refresh.
        """
        assert call(f"{base}/no-such-page/").status != 200
