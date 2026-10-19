# SPDX-License-Identifier: MIT-0
"""Prompt version authorship: the stored author, the email resolution, and the attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from domain.identity.operator_directory import OperatorEmailResolution, OperatorEmailStatus
from domain.presentation.insights import (
    InsightModelRoute,
    InsightPromptSeedRequest,
    InsightPromptVersionCreateRequest,
    InsightPromptVersionRecord,
    InsightSlotId,
)
from domain.presentation.prompt_authorship import (
    OperatorPromptAuthor,
    PromptAttributionKind,
    PromptVersionAttribution,
    SystemPromptAuthor,
    prompt_version_attribution,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]

_SUBJECT = "a1b2c3d4-fictional-operator-subject"
_EMAIL = "operator@example.com"


def _resolved(email: str = _EMAIL) -> OperatorEmailResolution:
    return OperatorEmailResolution(OperatorEmailStatus.RESOLVED, email)


@pytest.mark.parametrize(
    ("author", "emails", "expected"),
    [
        (None, {}, PromptVersionAttribution(PromptAttributionKind.NOT_RECORDED)),
        (
            SystemPromptAuthor.CONTRACT_SEED,
            {},
            PromptVersionAttribution(
                PromptAttributionKind.SYSTEM, system_author=SystemPromptAuthor.CONTRACT_SEED
            ),
        ),
        (
            OperatorPromptAuthor(_SUBJECT),
            {_SUBJECT: _resolved()},
            PromptVersionAttribution(PromptAttributionKind.OPERATOR_EMAIL, email=_EMAIL),
        ),
        (
            OperatorPromptAuthor(_SUBJECT),
            {_SUBJECT: OperatorEmailResolution(OperatorEmailStatus.NOT_FOUND)},
            PromptVersionAttribution(PromptAttributionKind.OPERATOR_NOT_FOUND),
        ),
        (
            OperatorPromptAuthor(_SUBJECT),
            {_SUBJECT: OperatorEmailResolution(OperatorEmailStatus.UNAVAILABLE)},
            PromptVersionAttribution(PromptAttributionKind.OPERATOR_UNAVAILABLE),
        ),
        (
            OperatorPromptAuthor(_SUBJECT),
            {},
            PromptVersionAttribution(PromptAttributionKind.OPERATOR_UNAVAILABLE),
        ),
    ],
    ids=["legacy", "system", "email", "not-found", "unavailable", "missing-entry"],
)
def test_every_author_maps_to_exactly_one_attribution_kind(
    author: OperatorPromptAuthor | SystemPromptAuthor | None,
    emails: dict[str, OperatorEmailResolution],
    expected: PromptVersionAttribution,
) -> None:
    assert prompt_version_attribution(author, emails) == expected


@pytest.mark.parametrize("subject", ["a", "s" * 128, "not-a-uuid_sub.value"])
def test_an_operator_author_accepts_any_bounded_subject(subject: str) -> None:
    assert OperatorPromptAuthor(subject).subject == subject


@pytest.mark.parametrize(
    "subject",
    ["", "s" * 129, "has space", "tab\tsubject", "control\x00subject", "line\nbreak"],
    ids=["empty", "129-chars", "space", "tab", "nul", "newline"],
)
def test_an_operator_author_refuses_an_unbounded_or_spaced_subject(subject: str) -> None:
    with pytest.raises(ValueError, match="operator author"):
        OperatorPromptAuthor(subject)


@pytest.mark.parametrize("email", ["a@b", _EMAIL, "x" * 120 + "@" + "y" * 133])
def test_a_resolution_accepts_a_well_formed_email(email: str) -> None:
    assert _resolved(email).email == email


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign.example.com",
        "two@at@example.com",
        "@example.com",
        "operator@",
        "a@",
        "x" * 120 + "@" + "y" * 134,
        "operator @example.com",
        "operator@example.com\n",
    ],
    ids=["no-at", "two-at", "empty-local", "empty-domain", "2-chars", "255-chars", "space", "nl"],
)
def test_a_resolution_refuses_a_malformed_email(email: str) -> None:
    with pytest.raises(ValueError, match="well-formed email"):
        _resolved(email)


@pytest.mark.parametrize("status", [OperatorEmailStatus.NOT_FOUND, OperatorEmailStatus.UNAVAILABLE])
def test_only_a_resolved_operator_carries_an_email(status: OperatorEmailStatus) -> None:
    assert OperatorEmailResolution(status).email is None
    with pytest.raises(ValueError, match="only a resolved operator"):
        OperatorEmailResolution(status, _EMAIL)


def test_a_resolved_operator_requires_an_email() -> None:
    with pytest.raises(ValueError, match="well-formed email"):
        OperatorEmailResolution(OperatorEmailStatus.RESOLVED)


@pytest.mark.parametrize(
    "kind",
    [
        PromptAttributionKind.OPERATOR_NOT_FOUND,
        PromptAttributionKind.OPERATOR_UNAVAILABLE,
        PromptAttributionKind.SYSTEM,
        PromptAttributionKind.NOT_RECORDED,
    ],
)
def test_an_attribution_refuses_an_email_on_a_non_email_kind(kind: PromptAttributionKind) -> None:
    with pytest.raises(ValueError, match="email is present exactly"):
        PromptVersionAttribution(
            kind,
            email=_EMAIL,
            system_author=SystemPromptAuthor.CONTRACT_SEED
            if kind is PromptAttributionKind.SYSTEM
            else None,
        )


def test_an_attribution_requires_an_email_for_the_email_kind() -> None:
    with pytest.raises(ValueError, match="email is present exactly"):
        PromptVersionAttribution(PromptAttributionKind.OPERATOR_EMAIL)


def test_a_system_attribution_requires_its_system_author() -> None:
    with pytest.raises(ValueError, match="system author is present exactly"):
        PromptVersionAttribution(PromptAttributionKind.SYSTEM)
    with pytest.raises(ValueError, match="system author is present exactly"):
        PromptVersionAttribution(
            PromptAttributionKind.NOT_RECORDED, system_author=SystemPromptAuthor.CONTRACT_SEED
        )


_AT = datetime(2026, 10, 1, 15, 25, tzinfo=UTC)


def _create(**overrides: object) -> InsightPromptVersionCreateRequest:
    fields: dict[str, Any] = {
        "author": OperatorPromptAuthor(_SUBJECT),
        "created_at": _AT,
        **overrides,
    }
    return InsightPromptVersionCreateRequest(
        InsightSlotId.CONTRIBUTION, "A fictional prompt.", InsightModelRoute.QUICK, **fields
    )


def test_a_create_request_carries_its_author_and_aware_time() -> None:
    request = _create()

    assert (request.author, request.created_at) == (OperatorPromptAuthor(_SUBJECT), _AT)


def test_a_create_request_refuses_a_naive_creation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _create(created_at=datetime(2026, 10, 1, 15, 25))  # noqa: DTZ001 - the naive case


@pytest.mark.parametrize("author", ["fictional-subject", None])
def test_a_create_request_refuses_an_author_that_is_not_typed(author: object) -> None:
    with pytest.raises(ValueError, match="operator or system author"):
        _create(author=author)


def test_a_create_request_has_no_default_author() -> None:
    with pytest.raises(TypeError, match="author"):
        InsightPromptVersionCreateRequest(  # type: ignore[call-arg]
            InsightSlotId.CONTRIBUTION,
            "A fictional prompt.",
            InsightModelRoute.QUICK,
            created_at=_AT,
        )


def _record(author: object, created_at: object) -> InsightPromptVersionRecord:
    return InsightPromptVersionRecord(
        InsightSlotId.CONTRIBUTION,
        2,
        "A fictional prompt.",
        InsightModelRoute.QUICK,
        cast("Any", author),
        cast("Any", created_at),
        "pending",
        (),
    )


def test_a_record_is_legacy_only_when_both_author_and_time_are_absent() -> None:
    assert _record(None, None).author is None
    assert _record(SystemPromptAuthor.CONTRACT_SEED, _AT).created_at == _AT


@pytest.mark.parametrize(
    ("author", "created_at"),
    [(None, _AT), (OperatorPromptAuthor(_SUBJECT), None)],
    ids=["time-without-author", "author-without-time"],
)
def test_a_record_refuses_a_half_legacy_authorship(author: object, created_at: object) -> None:
    with pytest.raises(ValueError, match="projection is invalid"):
        _record(author, created_at)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"prompt": "   "}, "seeded prompt"),
        ({"model_route": "fictional-route"}, "seeded prompt"),
        ({"created_at": datetime(2026, 10, 1)}, "timezone-aware"),  # noqa: DTZ001 - naive case
    ],
    ids=["blank-prompt", "bad-route", "naive-time"],
)
def test_a_seed_request_refuses_a_blank_prompt_a_bad_route_or_a_naive_time(
    overrides: dict[str, object], match: str
) -> None:
    fields: dict[str, Any] = {
        "slot_id": InsightSlotId.CONTRIBUTION,
        "prompt": "A fictional seed prompt.",
        "created_at": _AT,
        **overrides,
    }
    with pytest.raises(ValueError, match=match):
        InsightPromptSeedRequest(**fields)


def test_a_seed_request_does_not_refuse_the_movers_slot() -> None:
    """The adapter's early exit, not the request, keeps movers out of the registry."""
    request = InsightPromptSeedRequest(InsightSlotId.MOVERS, "Fictional movers text.", _AT)

    assert request.slot_id is InsightSlotId.MOVERS
