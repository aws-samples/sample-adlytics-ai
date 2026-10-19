# SPDX-License-Identifier: MIT-0
"""`ApplyGuardrail` as a `GuardrailPort`.

Layer 1 of ADR-041, as amended by ADR-042 and on 2026-09-30.

**Why this exists in our own code rather than on a model call.** The conversation route owns the
customer question, so `ApplyGuardrail` is a standalone runtime API applied to it before the
Harness runs; no model request may weaken, remove, or redirect that control. The outbound check on
the released answer was retired by ADR-083: the answer streams, and evaluation measures it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.guardrail import Assessment, AssessmentRequest, Direction

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from types_boto3_bedrock_runtime.literals import GuardrailContentSourceType
    from types_boto3_bedrock_runtime.type_defs import ApplyGuardrailResponseTypeDef

__all__ = ["GUARDRAIL_UNREACHABLE", "BedrockGuardrail"]

GUARDRAIL_UNREACHABLE = ErrorCode(area="GUARDRAIL", number=1)
"""The assessment could not be made. A dependency failure, so it alarms."""

_SOURCE: dict[Direction, GuardrailContentSourceType] = {
    Direction.INBOUND: "INPUT",
    Direction.OUTBOUND: "OUTPUT",
}
"""The port's direction as the API's `source`.

An explicit map rather than an uppercased name. The port says `inbound`/`outbound` and the API says
`INPUT`/`OUTPUT`, so the two only coincide by accident — deriving one from the other would break
silently if either were renamed.

Annotated with the SDK's own `GuardrailContentSourceType` rather than `str`, which is what makes
this a checked map: a value AWS stops accepting becomes a type error here rather than a runtime
`ValidationException`, and the checker also proves the map is total over `Direction`.
"""

_INTERVENED = "GUARDRAIL_INTERVENED"
"""The one `action` value that means the text must not be released.

`GuardrailActionType` is `Literal["GUARDRAIL_INTERVENED", "NONE"]`. Compared against the
intervening value rather than against `NONE`, so a third value AWS adds later is treated as *not*
an intervention. That direction is deliberate and worth naming, because it is the risky one: an
action this code does not recognise releases text. The alternative refuses every answer the moment
the API gains a value, which is a worse failure for a control that sits on every turn.
"""


class BedrockGuardrail:
    """A `GuardrailPort` over `bedrock-runtime:ApplyGuardrail`.

    **SigV4 under this function's own role.** The direct Runtime V2 invocation is authorized only
    from the Conversation Function role, while a guardrail is a deployment control rather than a
    caller capability. It is therefore assessed under the function identity, never a browser token.

    Built once per container rather than per request. It holds no credential or caller identity, so
    no caller-specific state can leak between Runtime V2 turns.

    **`outputScope` is deliberately unset.** `GuardrailOutputScopeType` offers `FULL` and
    `INTERVENTIONS`, and the default returns only what intervened — which is all this adapter reads.
    Asking for `FULL` would return the assessed text back to us, putting a copy of the narration in
    a response this code would then have to be careful never to log.
    """

    def __init__(
        self,
        guardrail_id: str,
        guardrail_version: str,
        client: BedrockRuntimeClient | None = None,
    ) -> None:
        if not guardrail_id.strip():
            raise ValueError(
                "A guardrail identifier is required. Constructing this adapter without one would "
                "produce a port that fails on the first turn rather than at container start."
            )
        if not guardrail_version.strip():
            raise ValueError(
                "A guardrail version is required. `DRAFT` is a legal value for ApplyGuardrail and "
                "is what this deployment passes; an empty string is not."
            )
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._client = client or boto3.client("bedrock-runtime")

    def assess(self, request: AssessmentRequest) -> Assessment:
        """Assess the text, or raise if the assessment could not be made."""
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._guardrail_id,
                guardrailVersion=self._guardrail_version,
                source=_SOURCE[request.direction],
                content=[{"text": {"text": request.text}}],
            )
        except (ClientError, BotoCoreError) as exc:
            # Raised, never swallowed into a passing assessment. A guardrail that could not be
            # reached has assessed nothing, and reporting that as "no intervention" would turn an
            # outage into a silent removal of the control.
            raise DependencyError(
                GUARDRAIL_UNREACHABLE,
                "The question could not be checked before it ran.",
                context={
                    "cause": type(exc).__name__,
                    "direction": str(request.direction),
                    "correlation_id": request.correlation_id,
                },
            ) from exc

        return self._assessment(response, request.direction)

    @staticmethod
    def _assessment(response: ApplyGuardrailResponseTypeDef, direction: Direction) -> Assessment:
        """Read the verdict and which checks fired, and never the matched value.

        **The matched value is never read, let alone logged.** An assessment carries the offending
        text back by design, and the observability rules forbid a guardrail match value anywhere,
        so only the named fields below are read: a filter's type and confidence, an entity's type.
        Without them a false positive cannot be told from a real attack except by replaying the
        user's text.
        """
        if response.get("action") != _INTERVENED:
            return Assessment(intervened=False)
        return Assessment(intervened=True, reason=_reason(direction), signals=_signals(response))

    def __repr__(self) -> str:
        """Name the guardrail, never a credential.

        There is nothing secret here — a guardrail id is not a credential — but the default `repr`
        of an object holding a botocore client is noise in a traceback, and this is what a reader
        actually wants to know.
        """
        return f"BedrockGuardrail(guardrail_id={self._guardrail_id!r})"


_MAX_SIGNALS = 16


def _signals(response: ApplyGuardrailResponseTypeDef) -> tuple[str, ...]:
    """Label every check that blocked: `content:<filter>:<confidence>` or `pii:<entity>`."""
    labels: list[str] = []
    for assessment in response.get("assessments", []):
        for item in assessment.get("contentPolicy", {}).get("filters", []):
            if item.get("action") == "BLOCKED":
                labels.append(f"content:{item.get('type')}:{item.get('confidence')}")
        for entity in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            if entity.get("action") == "BLOCKED":
                labels.append(f"pii:{entity.get('type')}")
        for regex in assessment.get("sensitiveInformationPolicy", {}).get("regexes", []):
            if regex.get("action") == "BLOCKED":
                labels.append(f"regex:{regex.get('name')}")
        for topic in assessment.get("topicPolicy", {}).get("topics", []):
            if topic.get("action") == "BLOCKED":
                labels.append(f"topic:{topic.get('name')}")
        words = assessment.get("wordPolicy", {})
        if any(
            word.get("action") == "BLOCKED"
            for word in [*words.get("customWords", []), *words.get("managedWordLists", [])]
        ):
            labels.append("word")
    return tuple(dict.fromkeys(labels))[:_MAX_SIGNALS]


def _reason(direction: Direction) -> str:
    """Give a reason safe to show, which depends only on the direction.

    **Never the category that fired, and never the matched text.** Which filter intervened helps an
    attacker calibrate and helps a user not at all; it belongs on the span. What a reader needs is
    what happened and what they can do, and that differs by direction: a declined question can be
    rephrased, while a withheld answer cannot be, because there is nothing for the user to fix. So
    the outbound wording says the answer was withheld rather than implying they asked badly.
    """
    if direction is Direction.INBOUND:
        return (
            "That question was declined before it ran. Rephrase it and ask again, or ask about "
            "campaign, product or marketplace performance directly."
        )
    return (
        "The answer was withheld because it did not pass a content check. Nothing was wrong with "
        "the question — ask again, or narrow it to a specific metric and window."
    )
