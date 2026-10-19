# SPDX-License-Identifier: MIT-0
"""Tests for identity and session rules. No AWS, no network, no clock."""

from datetime import UTC, datetime, timedelta

import pytest

from domain.identity.session import (
    Identity,
    IdentityError,
    Session,
    csrf_token_matches,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-007", "ADR-004")]

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/pool"
T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def make_session(*, issued: datetime = T0, ttl: timedelta = timedelta(minutes=15)) -> Session:
    return Session(
        identity=Identity(subject="sub-abc", issuer=ISSUER),
        issued_at=issued,
        expires_at=issued + ttl,
    )


class TestIdentity:
    def test_requires_a_subject(self) -> None:
        with pytest.raises(IdentityError, match="subject"):
            Identity(subject="", issuer=ISSUER)

    def test_requires_an_issuer(self) -> None:
        # Without checking iss, a token from another user pool would validate.
        with pytest.raises(IdentityError, match="issuer"):
            Identity(subject="sub-abc", issuer="")


class TestSession:
    def test_rejects_naive_timestamps(self) -> None:
        # ADR-004 depends on naive datetimes never entering the model.
        with pytest.raises(IdentityError, match="timezone-aware"):
            Session(
                identity=Identity(subject="s", issuer=ISSUER),
                issued_at=datetime(2026, 8, 18, 12, 0),  # noqa: DTZ001
                expires_at=T0 + timedelta(minutes=15),
            )

    def test_rejects_expiry_before_issue(self) -> None:
        with pytest.raises(IdentityError, match="after issued_at"):
            Session(
                identity=Identity(subject="s", issuer=ISSUER),
                issued_at=T0,
                expires_at=T0 - timedelta(seconds=1),
            )

    def test_not_expired_before_expiry(self) -> None:
        assert make_session().is_expired(T0 + timedelta(minutes=14)) is False

    def test_expired_at_the_boundary(self) -> None:
        # Exactly at expiry counts as expired. A session valid at its own expiry
        # is a one-second replay window.
        assert make_session().is_expired(T0 + timedelta(minutes=15)) is True

    def test_expires_within_triggers_early_refresh(self) -> None:
        session = make_session()
        assert session.expires_within(T0 + timedelta(minutes=14), timedelta(minutes=2)) is True
        assert session.expires_within(T0 + timedelta(minutes=1), timedelta(minutes=2)) is False

    def test_naive_now_is_rejected(self) -> None:
        with pytest.raises(IdentityError, match="timezone-aware"):
            make_session().is_expired(datetime(2026, 8, 18, 12, 0))  # noqa: DTZ001


class TestCsrf:
    def test_matching_tokens_pass(self) -> None:
        assert csrf_token_matches("tok-abc123", "tok-abc123") is True

    def test_mismatched_tokens_fail(self) -> None:
        assert csrf_token_matches("tok-abc123", "tok-abc124") is False

    def test_absent_header_fails(self) -> None:
        # Absent must never pass. An attacker who can omit the header must not
        # thereby succeed.
        assert csrf_token_matches("tok-abc123", "") is False

    def test_absent_cookie_fails(self) -> None:
        assert csrf_token_matches("", "tok-abc123") is False

    def test_both_absent_fails(self) -> None:
        assert csrf_token_matches("", "") is False
