# SPDX-License-Identifier: MIT-0
"""What the operator directory says about one prompt author's email address (ADR-069).

The registry stores only the opaque Cognito subject; the versions read resolves it to an email
address at read time. A resolution is a value: its email is present exactly when it resolved,
and an address that is not shaped like one cannot be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_MIN_EMAIL = 3
_MAX_EMAIL = 254


class OperatorEmailStatus(StrEnum):
    """Whether the directory resolved a subject to an email address."""

    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OperatorEmailResolution:
    """One subject's resolution; `email` is present exactly when `status` is `resolved`."""

    status: OperatorEmailStatus
    email: str | None = None

    def __post_init__(self) -> None:
        if self.status is OperatorEmailStatus.RESOLVED:
            if self.email is None or not is_operator_email(self.email):
                raise ValueError("a resolved operator needs a well-formed email address.")
        elif self.email is not None:
            raise ValueError("only a resolved operator carries an email address.")


def is_operator_email(value: str) -> bool:
    """Return whether `value` is 3-254 characters, one `@` between non-empty sides, no spaces."""
    if not _MIN_EMAIL <= len(value) <= _MAX_EMAIL:
        return False
    if any(character.isspace() or not character.isprintable() for character in value):
        return False
    local, separator, domain = value.partition("@")
    return separator == "@" and bool(local) and bool(domain) and "@" not in domain
