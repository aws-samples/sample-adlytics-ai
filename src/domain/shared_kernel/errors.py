# SPDX-License-Identifier: MIT-0
"""Error taxonomy. Four classes, split by what a human should do about them.

Specified in ADR-010. This module is pure domain: it must never import boto3,
botocore, or aws_lambda_powertools. `make lint-boundaries` enforces that.

Codes are identifiers, not descriptions. Never change what an existing code
means — supersede it with a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class ErrorClass(StrEnum):
    """What a human should do about the error.

    The split exists so alarms stay meaningful: two of these page someone and
    two do not.
    """

    DOMAIN = "domain"
    VALIDATION = "validation"
    DEPENDENCY = "dependency"
    INVARIANT = "invariant"


@dataclass(frozen=True, slots=True)
class ErrorCode:
    """A stable, namespaced identifier: ADLY-<AREA>-<NNNN>."""

    area: str
    number: int

    def __str__(self) -> str:
        return f"ADLY-{self.area.upper()}-{self.number:04d}"


REPROCESS_REQUIRES_GRAPH_RUN: Final = ErrorCode("REPROCESS", 1)
"""S6 §2.6 (D22): `reprocess_semantic_v2 --apply` refuses in `GRAPH`, before any write; in
`GRAPH` a reprocess is an `on_demand` `data_correction` run request instead."""

# `GRAPH_SERVING` 1 and `GRAPH_BAKE` 1 are retired: they were the D7 seam's undecided
# placeholders, removed when slice 5s built the owner's answer. Never reuse either number.
GRAPH_SERVING_ENTRY_MALFORMED: Final = ErrorCode("GRAPH_SERVING", 2)
"""S6 §2.9: presentation's mode read found `GRAPH` with a `graph_entered_at` it can't normalize
to a whole-second UTC instant (or none at all)."""

WRITE_UNIT_SETTLE_PATH_CODE: Final = ErrorCode("GRAPH_UNIT", 1)
"""S6 part 2 slice 7 (D34): `settle_node` refuses a `write_unit` `SUCCEEDED` before any write."""
GRAPH_UNIT_BATCH_UNSETTLED_CODE: Final = ErrorCode("GRAPH_UNIT", 2)
"""Slice 7 (§2.5 step 6): a success-set chunk's settlement guard was exhausted."""
GRAPH_UNIT_SET_COMPOSITION_CODE: Final = ErrorCode("GRAPH_UNIT", 3)
"""Slice 7 (§2.5 step 3, M3, N1): a set's union composition raised, or formation yielded none."""
GRAPH_UNIT_CLAIM_REFUSED_LIVE_CODE: Final = ErrorCode("GRAPH_UNIT", 4)
"""Slice 7 (§2.5 step 8, AR2): Begin refused a set while every re-read claim was still live."""
GRAPH_UNIT_EMPTY_CLAIMS_CODE: Final = ErrorCode("GRAPH_UNIT", 5)
"""Slice 7 (F5): a `GRAPH_UNIT` Begin's claim thunk returned no fence; refused before any write."""
GRAPH_UNIT_BATCH_ACCOUNTING_CODE: Final = ErrorCode("GRAPH_UNIT", 6)
"""Slice 7 (§2.5 step 8): a listed batch member landed in no class, or in more than one."""
WRITE_UNIT_BATCHING_PENDING_PROBE_CODE: Final = ErrorCode("GRAPH_UNIT", 7)
"""Slice 7 (D40): the time rule met a null `statement_allowance_ms` at run time."""
GRAPH_UNIT_BATCH_OVERSIZE_CODE: Final = ErrorCode("GRAPH_UNIT", 8)
"""Slice 7 (§2.2 "Oversize"): the provider rejected a batch envelope as oversized; a defect."""


class AdlyticsError(Exception):
    """Base for every error the platform raises deliberately.

    Carries a code, a description safe to show a user, and structured context
    that is logged but never surfaced.
    """

    error_class: ErrorClass

    def __init__(
        self,
        code: ErrorCode,
        description: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.context = context or {}

    @property
    def alarms(self) -> bool:
        """Whether this error should page someone.

        DomainError deliberately does not. "No data for that window" is an
        answer, not a failure, and alarming on it drowns the alarms that matter.
        """
        return self.error_class in (ErrorClass.DEPENDENCY, ErrorClass.INVARIANT)

    @property
    def user_visible_description(self) -> str:
        """What may reach the user.

        Dependency and invariant failures are generic on purpose: their real
        descriptions can name infrastructure, which must never leave the logs.
        """
        if self.error_class in (ErrorClass.DOMAIN, ErrorClass.VALIDATION):
            return self.description
        return "The request could not be completed. The issue has been recorded."


class DomainError(AdlyticsError):
    """The request is understood and the answer is legitimately unavailable.

    Insufficient history, a grain the privacy thresholds forbid, a metric that
    is not valid at the requested grain. Surfaced with its reason; never alarms.
    """

    error_class = ErrorClass.DOMAIN


class ValidationError(AdlyticsError):
    """The request is malformed or outside its contract."""

    error_class = ErrorClass.VALIDATION


class DependencyError(AdlyticsError):
    """An infrastructure call failed after retries were exhausted.

    Raised only by outbound adapters translating an SDK failure. An SDK
    exception type must never cross into the domain.
    """

    error_class = ErrorClass.DEPENDENCY


class InvariantError(AdlyticsError):
    """A state we believe impossible.

    A fact grain that should not exist, a suppressed row that lost its
    discriminator. Alarms urgently.
    """

    error_class = ErrorClass.INVARIANT


@dataclass(frozen=True, slots=True)
class ErrorMetric:
    """What gets emitted as EMF for every error.

    Dimensions are deliberately low-cardinality. Never dimension on a user id,
    ASIN, campaign id or correlation id — those belong in the log line and the
    span, not in a metric dimension, because EMF cardinality is both a cost and
    a usability problem.
    """

    error_class: ErrorClass
    code: str
    operation: str

    @property
    def dimensions(self) -> dict[str, str]:
        """The EMF dimension set.

        Derived rather than stored, so a dimension can never drift from the
        field it came from.
        """
        return {
            "ErrorClass": self.error_class.value,
            "ErrorCode": self.code,
            "Operation": self.operation,
        }

    @classmethod
    def from_error(cls, error: AdlyticsError, operation: str) -> ErrorMetric:
        """Build the metric for an error, deliberately dropping its context.

        Context can hold unbounded values, which must never become dimensions.
        """
        return cls(
            error_class=error.error_class,
            code=str(error.code),
            operation=operation,
        )
