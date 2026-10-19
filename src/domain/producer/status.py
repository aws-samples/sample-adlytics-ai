# SPDX-License-Identifier: MIT-0
"""Operational registration state, separate from analytical publication authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.producer.models import digest_bytes


class RegistrationState(StrEnum):
    """Lifecycle of one exact registration object version."""

    PROCESSING = "processing"
    FINALIZED = "finalized"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RegistrationAttempt:
    """Identity selected solely from an immutable registration key and VersionId."""

    registration_key: str
    registration_version_id: str

    def __post_init__(self) -> None:
        if not self.registration_key.endswith("/registration.json"):
            raise ValueError("registration attempt key must end in /registration.json.")
        if not self.registration_version_id.strip():
            raise ValueError("registration attempt requires a nonblank VersionId.")

    @property
    def attempt_id(self) -> str:
        """Return a bounded deterministic DynamoDB key for this exact version."""
        payload = f"{self.registration_key}|{self.registration_version_id}".encode()
        return f"registration-{digest_bytes(payload).removeprefix('sha256:')[:32]}"


@dataclass(frozen=True, slots=True)
class FinalizedRegistration:
    """Exact canonical result and manifest versions created by Finalizer."""

    result_key: str
    result_version_id: str
    manifest_key: str
    manifest_version_id: str

    def __post_init__(self) -> None:
        if not self.result_key.strip() or not self.manifest_key.endswith("/manifest.json"):
            raise ValueError("finalized registration requires canonical result and manifest keys.")
        if not self.result_version_id.strip() or not self.manifest_version_id.strip():
            raise ValueError(
                "finalized registration requires exact result and manifest VersionIds."
            )


@dataclass(frozen=True, slots=True)
class RegistrationRejection:
    """Stable terminal content refusal stored for producer diagnosis."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.detail.strip():
            raise ValueError("registration rejection requires code and detail.")


@dataclass(frozen=True, slots=True)
class RegistrationStatus:
    """One operational decision about one exact registration attempt."""

    attempt: RegistrationAttempt
    state: RegistrationState
    updated_at: datetime
    output: FinalizedRegistration | None = None
    rejection_code: str | None = None
    rejection_detail: str | None = None

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("registration status time must be timezone-aware.")
        if self.state is RegistrationState.PROCESSING:
            if (
                self.output is not None
                or self.rejection_code is not None
                or self.rejection_detail is not None
            ):
                raise ValueError("processing registration status cannot carry a terminal result.")
        elif self.state is RegistrationState.FINALIZED:
            if (
                self.output is None
                or self.rejection_code is not None
                or self.rejection_detail is not None
            ):
                raise ValueError(
                    "finalized registration status requires only exact output evidence."
                )
        elif self.output is not None or not self.rejection_code or not self.rejection_detail:
            raise ValueError("rejected registration status requires only code and detail.")

    @classmethod
    def processing(cls, attempt: RegistrationAttempt, *, at: datetime) -> RegistrationStatus:
        """Create the nonterminal claim recorded before external reads."""
        return cls(attempt=attempt, state=RegistrationState.PROCESSING, updated_at=at)

    @classmethod
    def finalized(
        cls,
        attempt: RegistrationAttempt,
        output: FinalizedRegistration,
        *,
        at: datetime,
    ) -> RegistrationStatus:
        """Create one exact successful finalization decision."""
        return cls(
            attempt=attempt,
            state=RegistrationState.FINALIZED,
            updated_at=at,
            output=output,
        )

    @classmethod
    def rejected(
        cls,
        attempt: RegistrationAttempt,
        rejection: RegistrationRejection,
        *,
        at: datetime,
    ) -> RegistrationStatus:
        """Create a terminal content refusal that queue retry cannot repair."""
        return cls(
            attempt=attempt,
            state=RegistrationState.REJECTED,
            updated_at=at,
            rejection_code=rejection.code,
            rejection_detail=rejection.detail,
        )


@dataclass(frozen=True, slots=True)
class RegistrationWriteOutcome:
    """Whether a conditional status transition was accepted."""

    accepted: bool
    status: RegistrationStatus
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.accepted and self.reason is not None:
            raise ValueError("an accepted registration write cannot carry a refusal reason.")
        if not self.accepted and not self.reason:
            raise ValueError("a refused registration write must state why.")
