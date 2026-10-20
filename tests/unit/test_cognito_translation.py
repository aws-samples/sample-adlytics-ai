# SPDX-License-Identifier: MIT-0
"""Tests for the Cognito adapter's response translation.

Only the pure half is here: shape validation, header construction, cache expiry.
The network behaviour — retries, timeouts, real JWKS contents, a real code
exchange — is verified against an actual user pool in the integration tier,
because a fake HTTP layer would only confirm that our fake behaves. See ADR-014.
"""

from datetime import UTC, datetime, timedelta

import pytest

from adapters.outbound.cognito.identity_provider import (
    JWKS_CACHE_TTL,
    TOKEN_EXCHANGE_FAILED,
    basic_auth_header,
    decode_json_object,
    is_cache_fresh,
    require_https,
    to_key_set,
    to_token_set,
)
from domain.identity.session import IdentityError
from domain.shared_kernel.errors import DependencyError, ErrorClass

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007", "ADR-014")]

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CODE = TOKEN_EXCHANGE_FAILED


class TestRequireHttps:
    def test_https_passes_through(self) -> None:
        assert require_https("https://auth.customer.com/oauth2/token").startswith("https://")

    @pytest.mark.parametrize(
        "url",
        [
            "http://auth.customer.com/oauth2/token",
            "file:///etc/passwd",
            "ftp://auth.customer.com",
            "//auth.customer.com/oauth2/token",
        ],
    )
    def test_anything_else_is_refused(self, url: str) -> None:
        # The secret and the refresh token both cross this call. Plaintext or a
        # non-network scheme must never be attempted at all.
        with pytest.raises(DependencyError, match="non-HTTPS"):
            require_https(url)


class TestBasicAuthHeader:
    def test_encodes_id_and_secret(self) -> None:
        # Cognito expects the confidential client credentials here rather than in
        # the form body, which also keeps the secret out of request-body logging.
        assert basic_auth_header("client", "secret") == "Basic Y2xpZW50OnNlY3JldA=="

    def test_a_colon_in_the_secret_survives_encoding(self) -> None:
        header = basic_auth_header("client", "sec:ret")
        assert header.startswith("Basic ")
        assert header != basic_auth_header("client", "secret")


class TestDecodeJsonObject:
    def test_parses_an_object(self) -> None:
        assert decode_json_object(b'{"a": 1}', code=CODE) == {"a": 1}

    def test_an_empty_body_is_a_success(self) -> None:
        # The revocation endpoint answers with nothing. Treating that as malformed
        # would turn a successful logout into an error.
        assert decode_json_object(b"", code=CODE) == {}

    def test_unparseable_json_becomes_a_dependency_error(self) -> None:
        with pytest.raises(DependencyError, match="unparseable"):
            decode_json_object(b"{not json", code=CODE)

    def test_a_json_array_is_rejected(self) -> None:
        with pytest.raises(DependencyError, match="unexpected JSON shape"):
            decode_json_object(b"[1, 2]", code=CODE)

    def test_the_failure_alarms(self) -> None:
        with pytest.raises(DependencyError) as caught:
            decode_json_object(b"[]", code=CODE)
        assert caught.value.error_class is ErrorClass.DEPENDENCY
        assert caught.value.alarms is True


class TestToTokenSet:
    def test_builds_from_a_full_response(self) -> None:
        tokens = to_token_set(
            {"access_token": "a.b.c", "expires_in": 900, "refresh_token": "r"}, code=CODE
        )
        assert (tokens.access_token, tokens.expires_in, tokens.refresh_token) == ("a.b.c", 900, "r")

    def test_a_missing_access_token_fails_loudly(self) -> None:
        """A 200 with no access token must not become an empty cookie.

        Defaulting to "" here would produce a session cookie containing nothing
        and a login that appears to have succeeded.
        """
        with pytest.raises(DependencyError, match="no access_token"):
            to_token_set({"expires_in": 900}, code=CODE)

    def test_an_empty_access_token_fails_loudly(self) -> None:
        with pytest.raises(DependencyError, match="no access_token"):
            to_token_set({"access_token": "", "expires_in": 900}, code=CODE)

    def test_a_missing_expires_in_fails(self) -> None:
        with pytest.raises(DependencyError, match="expires_in"):
            to_token_set({"access_token": "a.b.c"}, code=CODE)

    def test_a_string_expires_in_fails(self) -> None:
        with pytest.raises(DependencyError, match="expires_in"):
            to_token_set({"access_token": "a.b.c", "expires_in": "900"}, code=CODE)

    def test_a_boolean_expires_in_fails(self) -> None:
        # bool is a subclass of int, so a naive isinstance check would accept
        # True and produce a one-second session.
        with pytest.raises(DependencyError, match="expires_in"):
            to_token_set({"access_token": "a.b.c", "expires_in": True}, code=CODE)

    def test_a_refresh_response_ignores_any_refresh_token(self) -> None:
        # Cognito does not reissue one on refresh. Carrying it through would let a
        # stale or unexpected value overwrite a working cookie.
        tokens = to_token_set(
            {"access_token": "a.b.c", "expires_in": 900, "refresh_token": "surprise"},
            code=CODE,
            expect_refresh_token=False,
        )
        # A-TEST-LITERALS-001: an empty-token assertion; see docs/security-dispositions.md
        assert tokens.refresh_token == ""  # nosec B105

    def test_an_absent_refresh_token_becomes_empty_not_missing(self) -> None:
        assert (
            # A-TEST-LITERALS-001: an empty-token assertion; see docs/security-dispositions.md
            to_token_set({"access_token": "a.b.c", "expires_in": 900}, code=CODE).refresh_token  # nosec B105
            == ""
        )

    def test_a_zero_expiry_is_rejected_by_the_domain(self) -> None:
        # TokenSet owns that rule; this confirms the adapter does not bypass it.
        with pytest.raises(IdentityError, match="expires_in"):
            to_token_set({"access_token": "a.b.c", "expires_in": 0}, code=CODE)


class TestToKeySet:
    def test_builds_from_a_jwks_document(self) -> None:
        keys = to_key_set({"keys": [{"kid": "k1", "kty": "RSA"}, {"kid": "k2", "kty": "RSA"}]})
        assert keys.key_for("k1") is not None
        assert keys.key_for("k2") is not None

    def test_values_are_stringified(self) -> None:
        keys = to_key_set({"keys": [{"kid": "k1", "n": 12345}]})
        key = keys.key_for("k1")
        assert key is not None
        assert key["n"] == "12345"

    def test_a_missing_keys_field_fails(self) -> None:
        with pytest.raises(DependencyError, match="no keys"):
            to_key_set({})

    def test_an_empty_key_list_fails(self) -> None:
        # An empty set would reject every token as an unknown kid, which reads as
        # a rotation and drives a refresh on every request.
        with pytest.raises(DependencyError, match="no keys"):
            to_key_set({"keys": []})

    def test_a_non_list_keys_field_fails(self) -> None:
        with pytest.raises(DependencyError, match="no keys"):
            to_key_set({"keys": {"kid": "k1"}})

    def test_a_malformed_entry_fails_rather_than_being_skipped(self) -> None:
        with pytest.raises(DependencyError, match="not a JSON object"):
            to_key_set({"keys": [{"kid": "k1"}, "not-an-object"]})


class TestCacheFreshness:
    def test_a_just_fetched_set_is_fresh(self) -> None:
        assert is_cache_fresh(NOW, NOW) is True

    def test_a_set_within_the_ttl_is_fresh(self) -> None:
        assert is_cache_fresh(NOW, NOW + JWKS_CACHE_TTL - timedelta(seconds=1)) is True

    def test_a_set_at_the_ttl_is_stale(self) -> None:
        # Exclusive at the boundary. The TTL bounds how long a withdrawn signing
        # key stays trusted, so erring long is the wrong direction.
        assert is_cache_fresh(NOW, NOW + JWKS_CACHE_TTL) is False

    def test_a_set_past_the_ttl_is_stale(self) -> None:
        assert is_cache_fresh(NOW, NOW + JWKS_CACHE_TTL + timedelta(minutes=1)) is False
