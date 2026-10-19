# SPDX-License-Identifier: MIT-0
"""Content assessment at the edge of a turn.

Layer 1 of ADR-041, as amended by ADR-042 and on 2026-09-30.

**One call per turn, on the question.** The question is assessed before the agent loop starts.
ADR-083 retired the outbound assessment of the narration, because the answer now streams and
evaluation measures it. The port keeps its direction because the operation is direction-aware:
an assessment of an answer applies different rules from one of a question, and a caller that
assesses outbound text again must say so rather than inherit the inbound rules.

**Deliberately not a "guardrail" abstraction.** Nothing here names Bedrock, a filter category, a
confidence score or a threshold. Those are the adapter's business, and a port carrying them would
make the route's own logic depend on one vendor's assessment shape — which is what happened to
layer 2, where a mechanism turned out not to fit the topology and the design had to move.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "Assessment",
    "AssessmentRequest",
    "Direction",
    "GuardrailPort",
]


class Direction(StrEnum):
    """Which end of the turn is being assessed.

    The two are not interchangeable even though the call is. An intervention on the way in means the
    question was declined and nothing has been spent; an intervention on the way out means an answer
    was produced and is being withheld, which is the more expensive outcome and the one a reader
    experiences as the platform refusing to say something.
    """

    INBOUND = "inbound"
    """The user's question, before the agent loop starts."""

    OUTBOUND = "outbound"
    """The narration, before the block carrying it is released."""


@dataclass(frozen=True, slots=True)
class AssessmentRequest:
    """One piece of text to assess, and which direction it is travelling.

    A request object rather than loose parameters because there is a third — the correlation id —
    and because a direction passed positionally beside a string is the kind of argument that gets
    swapped once and never noticed: the call still succeeds, and the only symptom is that a question
    was assessed under the rules for an answer.
    """

    text: str
    direction: Direction
    correlation_id: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            # Refused rather than sent. An empty assessment costs a round trip and returns nothing
            # actionable, and a caller reaching this with empty text has a bug the assessment would
            # hide — an unassessed answer would read as an assessed one.
            raise ValueError(
                "There is nothing to assess. An empty string is a defect in the caller rather "
                "than a turn with no content, and assessing it would report success."
            )
        if not self.correlation_id:
            raise ValueError("correlation_id is required, so an assessment is findable in a log.")


@dataclass(frozen=True, slots=True)
class Assessment:
    """What the guardrail concluded, reduced to what a caller can act on.

    **A verdict and a reason, and no score.** A caller's only decision is whether to release the
    text, so a threshold comparison belongs on the far side of this boundary: exposing a score would
    invite the route to second-guess it, and two places deciding what counts as an intervention is
    how a control ends up disagreeing with itself. `signals` is diagnosis, not a third input to that
    decision.

    `reason` is safe to show. It never carries the matched value, the category that fired or any
    service identifier — the observability rules keep a guardrail match value out of a log, and a
    user-facing string is a weaker place still.
    """

    intervened: bool
    """True when the text must not be released."""

    reason: str = ""
    """Why, in words a reader can act on. Empty when nothing intervened."""

    signals: tuple[str, ...] = ()
    """What the assessment fired on, as the adapter's own short labels, for diagnosis only.

    Never shown to a reader and never used to decide anything; the caller logs them so a false
    positive can be told from a real one without replaying the text. A label names a check and its
    confidence, never the matched value.
    """

    def __post_init__(self) -> None:
        if self.intervened and not self.reason.strip():
            # The same invariant `Suppression` holds for withheld rows, for the same reason: a
            # withholding with no stated reason is indistinguishable to a reader from a failure, and
            # this product's posture is that anything withheld says why.
            raise ValueError("An intervention must carry a reason a reader can be shown.")


@runtime_checkable
class GuardrailPort(Protocol):
    """Assess one piece of text, in one direction.

    Raises rather than returning a passing assessment when the call itself fails. A guardrail that
    could not be reached has assessed nothing, and reporting that as "no intervention" would turn an
    outage into a silent removal of the control — which is the failure mode this whole layer exists
    to prevent.
    """

    def assess(self, request: AssessmentRequest) -> Assessment:
        """Assess the text, or raise if the assessment could not be made."""
        ...
