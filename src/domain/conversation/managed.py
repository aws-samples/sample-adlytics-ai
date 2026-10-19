# SPDX-License-Identifier: MIT-0
"""Managed-Harness turn, lifecycle, streamed text, and private tool custody contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from domain.conversation.contract_common import require_text
from domain.conversation.page_context import ConversationContextStatusV1, RedeemedPageContextV1
from domain.conversation.tool_custody import (
    HarnessStepDispositionV1,
    HarnessToolStepV1,
    ValidatedToolResultV1,
)
from domain.semantics.page_context import PageContextReference

_CORRELATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_QUESTION_BYTES = 8_000


@dataclass(frozen=True, slots=True, kw_only=True)
class HarnessTurnV2:
    """Server-owned turn framed only from the authenticated chat request.

    `HarnessTurn.v1` plus `page_context` (ADR-120 decision 6): the labels of the page the
    route redeemed, sent to the Harness as quoted data in a second text block, never as
    instructions. `None` when the browser sent no reference or redemption failed.
    """

    schema_version: Literal["HarnessTurn.v2"] = "HarnessTurn.v2"
    question: str
    conversation_id: str
    actor_id: str
    correlation_id: str
    page_context: RedeemedPageContextV1 | None = None

    def __post_init__(self) -> None:
        if not self.question.strip() or len(self.question.encode("utf-8")) > _MAX_QUESTION_BYTES:
            raise ValueError("managed Harness question is empty or too long.")
        if not self.conversation_id.strip():
            raise ValueError("managed Harness conversation identity is required.")
        if not self.actor_id.strip():
            raise ValueError("managed Harness actor identity is required.")
        if not _CORRELATION_ID.fullmatch(self.correlation_id):
            raise ValueError("managed Harness correlation must be a canonical UUID4.")


class ManagedHarnessLifecycleStageV1(StrEnum):
    """Private, non-disclosing projection of raw Harness lifecycle progress."""

    GOVERNED_DATA_REQUESTED = "governed_data_requested"
    GOVERNED_DATA_READY = "governed_data_ready"


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedHarnessActivityV1:
    """One raw-lifecycle projection with no tool custody data."""

    schema_version: Literal["ManagedHarnessActivity.v1"] = "ManagedHarnessActivity.v1"
    stage: ManagedHarnessLifecycleStageV1


class ManagedHarnessToolCallStatusV1(StrEnum):
    """Progress of one governed tool call as the Harness stream reveals it."""

    REQUESTED = "requested"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedHarnessToolActivityV1:
    """One governed tool call named by its declared logical tool name.

    Carries the server-declared name only — never arguments, results, or model wording — so
    the runtime can show the user which governed capability ran (ADR-065 transparency).
    """

    schema_version: Literal["ManagedHarnessToolActivity.v1"] = "ManagedHarnessToolActivity.v1"
    tool_name: str
    status: ManagedHarnessToolCallStatusV1

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("managed Harness tool activity requires its logical tool name.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedHarnessTextDeltaV1:
    """One assistant text delta, forwarded the moment the Harness delivers it (ADR-083).

    Reasoning content never becomes one; only the text the model writes for the user does.
    """

    schema_version: Literal["ManagedHarnessTextDelta.v1"] = "ManagedHarnessTextDelta.v1"
    text: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("a managed Harness text delta must be non-empty.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedHarnessToolStepV1:
    """One completed governed tool call: its trajectory step and, unless it errored, its custody.

    Emitted as each result closes, so a turn that fails later still records the steps it reached.
    """

    schema_version: Literal["ManagedHarnessToolStep.v1"] = "ManagedHarnessToolStep.v1"
    step: HarnessToolStepV1
    result: ValidatedToolResultV1 | None = None

    def __post_init__(self) -> None:
        errored = self.step.disposition is HarnessStepDispositionV1.ERRORED
        if errored != (self.result is None):
            raise ValueError("a tool step carries its custody exactly when it did not error.")
        if self.result is not None and self.step.step_ref != self.result.tool_result_ref:
            raise ValueError("a tool step and its custody must name the same result.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedHarnessTurnEndedV1:
    """The Harness reached `end_turn`."""

    schema_version: Literal["ManagedHarnessTurnEnded.v1"] = "ManagedHarnessTurnEnded.v1"


ManagedHarnessEventV1 = (
    ManagedHarnessActivityV1
    | ManagedHarnessToolActivityV1
    | ManagedHarnessTextDeltaV1
    | ManagedHarnessToolStepV1
    | ManagedHarnessTurnEndedV1
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedConversationRequestV1:
    """One server-owned request for the sole managed Harness execution path.

    `principal` is the verified Cognito `sub`, carried only as far as the audit receipt
    (ADR-077 §6). The Harness receives `turn` alone, whose actor is a one-way digest of it.
    """

    schema_version: Literal["ManagedConversationRequest.v1"] = "ManagedConversationRequest.v1"
    turn: HarnessTurnV2
    principal: str = field(repr=False)
    page_context_reference: PageContextReference | None = None
    """The opaque reference the browser sent; the receipt records it, never its labels."""
    context_status: ConversationContextStatusV1 | None = None
    """Whether the sent reference was used; `None` exactly when none was sent."""

    def __post_init__(self) -> None:
        require_text(self.principal, "managed conversation principal", maximum=128)
        if (self.page_context_reference is None) != (self.context_status is None):
            raise ValueError("a context status exists exactly when a page reference was sent.")
        if (self.context_status is not None and self.context_status.used) != (
            self.turn.page_context is not None
        ):
            raise ValueError("a turn carries page context exactly when its status says used.")
