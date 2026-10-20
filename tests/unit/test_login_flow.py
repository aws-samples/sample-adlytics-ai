# SPDX-License-Identifier: MIT-0
"""Tests for the PKCE and state values that survive the login redirect.

Both exist to defeat a specific attack. The verifier stops an intercepted
authorization code from being redeemed by whoever intercepted it; the state stops
a callback nobody started from establishing a session. A weakness in either is
invisible in a working login.
"""

import base64
import hashlib
from dataclasses import FrozenInstanceError

import pytest

from domain.identity.login_flow import (
    CHALLENGE_METHOD,
    MIN_VERIFIER_LENGTH,
    LoginTransaction,
    new_login_transaction,
    state_matches,
)
from domain.identity.session import IdentityError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007")]

VERIFIER = "a" * 43


class TestChallengeMethod:
    def test_the_method_is_s256(self) -> None:
        # Pinned by value. With `plain` the challenge *is* the verifier, so anyone
        # who sees the authorize request can complete the exchange — which removes
        # the only thing PKCE contributes.
        assert CHALLENGE_METHOD == "S256"


class TestNewLoginTransaction:
    def test_the_verifier_meets_the_rfc_floor(self) -> None:
        assert len(new_login_transaction().code_verifier) >= MIN_VERIFIER_LENGTH

    def test_each_login_gets_fresh_values(self) -> None:
        first, second = new_login_transaction(), new_login_transaction()
        assert first.state != second.state
        assert first.code_verifier != second.code_verifier

    def test_values_are_url_safe(self) -> None:
        # Both travel on a URL. Padding and non-URL-safe characters get mangled by
        # some providers, and the failure appears only at the exchange.
        transaction = new_login_transaction()
        for value in (transaction.state, transaction.code_verifier):
            assert "=" not in value
            assert "+" not in value
            assert "/" not in value


class TestCodeChallenge:
    def test_it_is_the_sha256_of_the_verifier(self) -> None:
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert LoginTransaction(state="s", code_verifier=VERIFIER).code_challenge == expected

    def test_it_never_equals_the_verifier(self) -> None:
        transaction = new_login_transaction()
        assert transaction.code_challenge != transaction.code_verifier

    def test_it_is_stable_for_one_verifier(self) -> None:
        transaction = LoginTransaction(state="s", code_verifier=VERIFIER)
        assert transaction.code_challenge == transaction.code_challenge


class TestValidation:
    def test_a_short_verifier_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="43 characters"):
            LoginTransaction(state="s", code_verifier="a" * 42)

    def test_an_empty_state_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="state"):
            LoginTransaction(state="", code_verifier=VERIFIER)

    def test_a_separator_in_either_value_is_rejected(self) -> None:
        # The encoding splits on the first separator, so a value containing one
        # would decode into something other than what was encoded.
        with pytest.raises(IdentityError, match="separator"):
            LoginTransaction(state="a.b", code_verifier=VERIFIER)


class TestEncoding:
    def test_a_round_trip_preserves_both_values(self) -> None:
        original = new_login_transaction()
        restored = LoginTransaction.decode(original.encode())
        assert restored == original

    def test_a_malformed_value_is_rejected(self) -> None:
        # A callback with no decodable transaction is a callback for a login this
        # browser never started.
        with pytest.raises(IdentityError, match="malformed"):
            LoginTransaction.decode("no-separator-here")

    def test_an_empty_value_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="malformed"):
            LoginTransaction.decode("")

    def test_a_truncated_value_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="43 characters"):
            LoginTransaction.decode("state.tooshort")


class TestStateMatches:
    def test_the_matching_state_is_accepted(self) -> None:
        transaction = new_login_transaction()
        assert state_matches(transaction.state, transaction) is True

    def test_a_different_state_is_rejected(self) -> None:
        assert state_matches("someone-elses-state", new_login_transaction()) is False

    def test_an_absent_state_is_rejected(self) -> None:
        # Absent must never mean nothing-to-check. This is the entire defence
        # against a forged callback.
        assert state_matches("", new_login_transaction()) is False

    def test_a_prefix_of_the_state_is_rejected(self) -> None:
        transaction = new_login_transaction()
        assert state_matches(transaction.state[:-1], transaction) is False


class TestGapsFoundByMutationTesting:
    """Cases whose absence let a mutant survive (mutation review, ADR-013)."""

    def test_a_verifier_longer_than_the_rfc_floor_is_accepted(self) -> None:
        # The check is a floor, not an equality: RFC 7636 permits 43 to 128
        # characters. Every existing test used exactly 43, so a comparison mutated
        # from `<` to `!=` behaved identically while rejecting every legal longer
        # verifier — raising VERIFIER_BYTES would then stop every login from
        # starting, and no test would say why.
        longer = "a" * 64
        assert LoginTransaction(state="s", code_verifier=longer).code_verifier == longer

    def test_the_state_carries_at_least_128_bits_of_entropy(self) -> None:
        # Nothing asserted the state's length at all, so shrinking STATE_BYTES was
        # invisible. Being unguessable is the state's only property, and it is the
        # whole defence against a forged callback. Unpadded base64url packs 6 bits
        # per character, so 128 bits needs 22 of them.
        minimum_characters = 128 // 6 + 1
        assert len(new_login_transaction().state) >= minimum_characters

    def test_a_transaction_cannot_be_mutated_after_it_has_been_validated(self) -> None:
        # __post_init__ is the only place the state, the verifier length and the
        # separator are checked. Were the dataclass mutable, a caller could pass
        # validation and then rewrite state — and state_matches compares a returned
        # value against exactly that field.
        transaction = new_login_transaction()
        with pytest.raises(FrozenInstanceError):
            # The static error is the invariant: suppressed for this rule only, so
            # the runtime behaviour behind it still gets asserted.
            transaction.state = "forged"  # pyright: ignore[reportAttributeAccessIssue]
