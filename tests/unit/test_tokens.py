# SPDX-License-Identifier: MIT-0
"""Tests for access-token verification.

Most of these inputs cannot be obtained from a real user pool — it will not issue
a token with a wrong audience, an unknown signing key or a substituted algorithm.
They are signed here instead, which is the whole reason ADR-014 puts this logic
in the domain rather than behind an adapter.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CLIENT_ID, ISSUER, KID, NOW, SUBJECT, TokenMinter

from domain.identity.session import IdentityError
from domain.identity.tokens import (
    JsonWebKeySet,
    TokenSet,
    UnknownSigningKeyError,
    VerifyAccessTokenRequest,
    verify_access_token,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007", "ADR-014")]


def _verify(minter: TokenMinter, token: str, **overrides: object) -> object:
    request = VerifyAccessTokenRequest(
        token=token,
        keys=overrides.get("keys") or minter.jwks(),  # type: ignore[arg-type]
        expected_issuer=str(overrides.get("issuer", ISSUER)),
        expected_client_id=str(overrides.get("client_id", CLIENT_ID)),
        now=overrides.get("now") or NOW,  # type: ignore[arg-type]
    )
    return verify_access_token(request)


class TestValidToken:
    def test_returns_the_subject_and_issuer(self, minter: TokenMinter) -> None:
        identity = _verify(minter, minter.token())
        assert identity.subject == SUBJECT  # type: ignore[attr-defined]
        assert identity.issuer == ISSUER  # type: ignore[attr-defined]


class TestSignature:
    def test_a_token_signed_by_another_key_is_rejected(self, minter: TokenMinter) -> None:
        # Same kid, different private key: the JWKS lookup succeeds and only the
        # signature check can catch it.
        with pytest.raises(IdentityError, match="signature"):
            _verify(minter, minter.token(wrong_key=True))

    def test_a_tampered_payload_is_rejected(self, minter: TokenMinter) -> None:
        header, payload, signature = minter.token().split(".")
        tampered = f"{header}.{payload[:-4]}AAAA.{signature}"
        with pytest.raises(IdentityError):
            _verify(minter, tampered)

    def test_alg_none_is_rejected(self, minter: TokenMinter) -> None:
        # The unsigned-token attack. Rejected on the declared algorithm, before
        # any key is selected and before any claim is read.
        with pytest.raises(IdentityError, match="not accepted"):
            _verify(minter, minter.unsigned_token())

    def test_a_symmetric_algorithm_is_rejected(self, minter: TokenMinter) -> None:
        # Algorithm confusion: HS256 signed with a value the attacker knows.
        with pytest.raises(IdentityError, match="not accepted"):
            _verify(minter, minter.token(algorithm="HS256"))

    def test_garbage_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="not decodable"):
            _verify(minter, "not-a-jwt")

    def test_an_empty_token_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="No token"):
            _verify(minter, "")


class TestSigningKeySelection:
    def test_an_unknown_kid_raises_the_retryable_error(self, minter: TokenMinter) -> None:
        # Distinct from every other failure: this is the one a caller should
        # respond to by refreshing the JWKS, because it usually means rotation.
        with pytest.raises(UnknownSigningKeyError):
            _verify(minter, minter.token(), keys=minter.jwks(kid="rotated-away"))

    def test_a_missing_kid_is_not_silently_matched(self, minter: TokenMinter) -> None:
        # An empty kid must never fall through to "the only key we have".
        with pytest.raises(UnknownSigningKeyError):
            _verify(minter, minter.token(kid=""))

    def test_an_empty_key_set_rejects_everything(self, minter: TokenMinter) -> None:
        with pytest.raises(UnknownSigningKeyError):
            _verify(minter, minter.token(), keys=JsonWebKeySet(keys=()))


class TestExpiry:
    def test_an_expired_token_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="expired"):
            _verify(minter, minter.token(exp=int((NOW - timedelta(seconds=1)).timestamp())))

    def test_a_token_expiring_exactly_now_is_rejected(self, minter: TokenMinter) -> None:
        # Inclusive on purpose. Valid-at-its-own-expiry is a replay window, and
        # it matches Session.is_expired rather than disagreeing with it.
        with pytest.raises(IdentityError, match="expired"):
            _verify(minter, minter.token(exp=int(NOW.timestamp())))

    def test_a_token_valid_one_second_longer_is_accepted(self, minter: TokenMinter) -> None:
        identity = _verify(minter, minter.token(exp=int((NOW + timedelta(seconds=1)).timestamp())))
        assert identity.subject == SUBJECT  # type: ignore[attr-defined]

    def test_a_missing_exp_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="exp"):
            _verify(minter, minter.token(exp=None))

    def test_a_non_numeric_exp_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="exp"):
            _verify(minter, minter.token(exp="soon"))


class TestAudienceClaim:
    def test_a_wrong_client_id_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="client_id"):
            _verify(minter, minter.token(client_id="9someoneelse876"))

    def test_an_absent_client_id_is_rejected_not_skipped(self, minter: TokenMinter) -> None:
        """The defect this module was written to prevent.

        An access token carries `client_id`, never `aud`. A verifier that checks
        `aud` finds nothing and, if it treats absence as nothing-to-check, allows
        any client. Absence must fail.
        """
        with pytest.raises(IdentityError, match="client_id"):
            _verify(minter, minter.token(client_id=None, aud=CLIENT_ID))


class TestIssuerClaim:
    def test_a_token_from_another_pool_is_rejected(self, minter: TokenMinter) -> None:
        other = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_attacker"
        with pytest.raises(IdentityError, match="iss"):
            _verify(minter, minter.token(iss=other))

    def test_a_missing_issuer_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="iss"):
            _verify(minter, minter.token(iss=None))


class TestTokenUseClaim:
    def test_an_id_token_is_rejected(self, minter: TokenMinter) -> None:
        # Signed by the same pool, so the signature is genuinely valid. Only
        # token_use distinguishes it.
        with pytest.raises(IdentityError, match="token_use"):
            # A-TEST-LITERALS-001: the token_use under test; see docs/security-dispositions.md
            _verify(minter, minter.token(token_use="id"))  # nosec B106

    def test_a_missing_token_use_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="token_use"):
            _verify(minter, minter.token(token_use=None))


class TestSubjectClaim:
    def test_a_missing_subject_is_rejected(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="sub"):
            _verify(minter, minter.token(sub=None))


class TestRequestValidation:
    def test_verification_without_an_expected_issuer_is_refused(self, minter: TokenMinter) -> None:
        # Fail closed on our own misconfiguration rather than skipping the check.
        with pytest.raises(IdentityError, match="expected_issuer"):
            VerifyAccessTokenRequest(
                token=minter.token(),
                keys=minter.jwks(),
                expected_issuer="",
                expected_client_id=CLIENT_ID,
                now=NOW,
            )

    def test_verification_without_an_expected_client_id_is_refused(
        self, minter: TokenMinter
    ) -> None:
        with pytest.raises(IdentityError, match="expected_client_id"):
            VerifyAccessTokenRequest(
                token=minter.token(),
                keys=minter.jwks(),
                expected_issuer=ISSUER,
                expected_client_id="",
                now=NOW,
            )

    def test_a_naive_now_is_refused(self, minter: TokenMinter) -> None:
        with pytest.raises(IdentityError, match="timezone-aware"):
            VerifyAccessTokenRequest(
                token=minter.token(),
                keys=minter.jwks(),
                expected_issuer=ISSUER,
                expected_client_id=CLIENT_ID,
                now=NOW.replace(tzinfo=None),
            )


class TestKeySetLookup:
    def test_key_for_selects_by_kid(self, minter: TokenMinter) -> None:
        assert minter.jwks().key_for(KID) is not None

    def test_key_for_returns_none_for_an_empty_kid(self, minter: TokenMinter) -> None:
        assert minter.jwks().key_for("") is None


class TestTokenSet:
    def test_an_access_token_is_required(self) -> None:
        with pytest.raises(IdentityError, match="access_token"):
            # A-TEST-LITERALS-001: the empty token under test; see docs/security-dispositions.md
            TokenSet(access_token="", expires_in=900)  # nosec B106

    def test_expires_in_must_be_positive(self) -> None:
        with pytest.raises(IdentityError, match="expires_in"):
            # A-TEST-LITERALS-001: a fictional token; see docs/security-dispositions.md
            TokenSet(access_token="a.b.c", expires_in=0)  # nosec B106

    def test_refresh_token_defaults_to_empty(self) -> None:
        # A refresh response omits it, and an empty value must not look like a
        # real token that could overwrite a good cookie.
        # A-TEST-LITERALS-001: an empty-token assertion; see docs/security-dispositions.md
        assert TokenSet(access_token="a.b.c", expires_in=900).refresh_token == ""  # nosec B105, B106


class TestGapsFoundByMutationTesting:
    """Cases whose absence let a mutant survive (mutation review, ADR-013).

    Each one corresponds to an invariant the module's own docstrings assert and that
    no test previously exercised, so the assertion was documentation only.
    """

    def test_key_selection_is_by_kid_and_not_by_ordering(self, minter: TokenMinter) -> None:
        # A pool signs access and ID tokens with two different key pairs, so key_for
        # must match on kid exactly. With a single-key set, a comparison mutated from
        # == to <= still returns the only key, so the previous tests could not tell
        # exact matching from "returns something". A decoy whose kid sorts below the
        # real one distinguishes them.
        decoy = {"kid": "0000-sorts-before", "kty": "RSA", "n": "unused", "e": "AQAB"}
        keys = JsonWebKeySet(keys=(decoy, *minter.jwks().keys))

        selected = keys.key_for(KID)

        assert selected is not None
        assert selected["kid"] == KID

    def test_expiry_is_judged_by_the_injected_now_not_the_wall_clock(
        self, minter: TokenMinter
    ) -> None:
        # verify_exp is disabled in the decoder precisely so `now` decides. Every
        # other expiry test uses NOW, which is close enough to the real clock that
        # enabling verify_exp would not fail them — so none of them could detect it.
        #
        # These timestamps are in the distant past deliberately: the token expired
        # long ago in wall-clock terms and is still valid relative to the injected
        # `now`, so it passes only while `now` is authoritative. That holds whenever
        # the suite runs, which a date near NOW would not.
        long_ago = datetime(2020, 1, 1, tzinfo=UTC)
        token = minter.token(exp=int((long_ago + timedelta(minutes=15)).timestamp()))

        identity = _verify(minter, token, now=long_ago)

        assert identity.subject == SUBJECT  # type: ignore[attr-defined]

    def test_an_empty_string_claim_is_rejected_like_a_missing_one(
        self, minter: TokenMinter
    ) -> None:
        # _require_str rejects on `not a string OR empty`. Mutated to AND, an empty
        # string passes both halves and is accepted, which would admit a token with a
        # blank subject — an identity of nobody.
        with pytest.raises(IdentityError):
            _verify(minter, minter.token(sub=""))

    def test_expires_in_of_exactly_one_second_is_accepted(self) -> None:
        # The boundary of `expires_in < 1`. Nothing previously sat on it, so shifting
        # the comparison to <= rejected a legitimate one-second token undetected.
        # A-TEST-LITERALS-001: a fictional token; see docs/security-dispositions.md
        assert TokenSet(access_token="t", expires_in=1).expires_in == 1  # nosec B106

    def test_a_verify_request_cannot_be_rewritten_after_validation(
        self, minter: TokenMinter
    ) -> None:
        # The one frozen dataclass here where mutability is exploitable.
        # __post_init__ is the only place expected_issuer is required to be
        # non-empty, so a mutable request could pass validation and then be
        # rewritten to expect a different issuer before verify_access_token reads it.
        request = VerifyAccessTokenRequest(
            token=minter.token(),
            keys=minter.jwks(),
            expected_issuer=ISSUER,
            expected_client_id=CLIENT_ID,
            now=NOW,
        )
        with pytest.raises(FrozenInstanceError):
            # The static error is the invariant; suppressed for this rule only so
            # the runtime behaviour behind it still gets asserted.
            request.expected_issuer = "https://evil.example"  # pyright: ignore[reportAttributeAccessIssue]

    def test_a_token_set_cannot_be_rewritten_after_validation(self) -> None:
        # __post_init__ rejects an empty access_token. Mutable, it could be emptied
        # afterwards and reach build_session_cookies as a valid-looking TokenSet.
        # A-TEST-LITERALS-001: a fictional token; see docs/security-dispositions.md
        tokens = TokenSet(access_token="a.b.c", expires_in=900)  # nosec B106
        with pytest.raises(FrozenInstanceError):
            # A-TEST-LITERALS-001: a frozen-field write; see docs/security-dispositions.md
            tokens.access_token = ""  # pyright: ignore[reportAttributeAccessIssue]  # nosec B105

    def test_a_key_set_cannot_be_rewritten_after_construction(self, minter: TokenMinter) -> None:
        # No validation to bypass here, but the keys a token is checked against must
        # not be swappable on an instance already handed to verify_access_token.
        keys = minter.jwks()
        with pytest.raises(FrozenInstanceError):
            keys.keys = ()  # pyright: ignore[reportAttributeAccessIssue]
