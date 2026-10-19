# SPDX-License-Identifier: MIT-0
"""Pre-open terminal refusal contract returned before the SSE stream exists."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from domain.conversation.contract_common import require_text


class TerminalReasonCodeV2(StrEnum):
    """Pre-open terminal refusal reasons."""

    INPUT_INTERVENED = "input_intervened"
    CONTEXT_CUSTODY_REFUSED = "context_custody_refused"
    INVALID_REQUEST = "invalid_request"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationTerminalV2:
    """Exact refusal body used only before SSE opens."""

    schema_version: Literal["ConversationTerminal.v2"] = "ConversationTerminal.v2"
    kind: Literal["refused"] = "refused"
    reason_code: TerminalReasonCodeV2
    reason: str
    audit_id: str | None = None
    """The support reference of an inbound intervention's receipt (ADR-081 §2), when recorded."""

    def __post_init__(self) -> None:
        require_text(self.reason, "terminal reason", maximum=4000)
        if self.audit_id is not None:
            require_text(self.audit_id, "terminal support reference", maximum=128)
