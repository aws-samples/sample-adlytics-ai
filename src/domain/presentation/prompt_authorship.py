# SPDX-License-Identifier: MIT-0
"""Who created a prompt version, and how that author is attributed on read (ADR-069).

A version record stores its author as the authorizer-verified Cognito subject, or as a declared
system author for contract seeds; records written before authorship was recorded carry none.
The display never stores an email address: it resolves the subject at read time, and this
module maps the author and that resolution to one closed attribution kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from domain.identity.operator_directory import OperatorEmailResolution, OperatorEmailStatus

_MAX_SUBJECT = 128
"""Cognito's `UsernameType` bound; a `sub` is not guaranteed to be a UUID."""


@dataclass(frozen=True, slots=True)
class OperatorPromptAuthor:
    """The authorizer-verified Cognito subject of the operator who created a version."""

    subject: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.subject) <= _MAX_SUBJECT or any(
            character.isspace() or not character.isprintable() for character in self.subject
        ):
            raise ValueError("an operator author is a 1-128 character subject with no spaces.")


class SystemPromptAuthor(StrEnum):
    """A declared non-operator author."""

    CONTRACT_SEED = "contract-seed"


type PromptVersionAuthor = OperatorPromptAuthor | SystemPromptAuthor


class PromptAttributionKind(StrEnum):
    """How one version's author is shown."""

    OPERATOR_EMAIL = "operator_email"
    OPERATOR_NOT_FOUND = "operator_not_found"
    OPERATOR_UNAVAILABLE = "operator_unavailable"
    SYSTEM = "system"
    NOT_RECORDED = "not_recorded"


@dataclass(frozen=True, slots=True)
class PromptVersionAttribution:
    """One version's attribution; `email` and `system_author` are present exactly for their kind."""

    kind: PromptAttributionKind
    email: str | None = None
    system_author: SystemPromptAuthor | None = None

    def __post_init__(self) -> None:
        if (self.email is not None) != (self.kind is PromptAttributionKind.OPERATOR_EMAIL):
            raise ValueError("an email is present exactly for an operator email attribution.")
        if (self.system_author is not None) != (self.kind is PromptAttributionKind.SYSTEM):
            raise ValueError("a system author is present exactly for a system attribution.")


_OPERATOR_KINDS = {
    OperatorEmailStatus.NOT_FOUND: PromptAttributionKind.OPERATOR_NOT_FOUND,
    OperatorEmailStatus.UNAVAILABLE: PromptAttributionKind.OPERATOR_UNAVAILABLE,
}


def prompt_version_attribution(
    author: PromptVersionAuthor | None, emails: Mapping[str, OperatorEmailResolution]
) -> PromptVersionAttribution:
    """Attribute one version from its stored author and the directory's resolutions."""
    if author is None:
        return PromptVersionAttribution(PromptAttributionKind.NOT_RECORDED)
    if isinstance(author, SystemPromptAuthor):
        return PromptVersionAttribution(PromptAttributionKind.SYSTEM, system_author=author)
    resolution = emails.get(author.subject)
    if resolution is None:
        return PromptVersionAttribution(PromptAttributionKind.OPERATOR_UNAVAILABLE)
    if resolution.status is OperatorEmailStatus.RESOLVED:
        return PromptVersionAttribution(
            PromptAttributionKind.OPERATOR_EMAIL, email=resolution.email
        )
    return PromptVersionAttribution(_OPERATOR_KINDS[resolution.status])
