# SPDX-License-Identifier: MIT-0
"""Authenticate and complete one managed-Harness conversation request.

Identity, CSRF, origin, bounds, and browser authority are decided before Harness
work. The browser supplies only the schema tag, question, conversation session
and, from v3, one opaque page reference the server redeems (ADR-120). Verified
identity and correlation remain server-owned and never enter Harness messages or
tool arguments. The caller's verified access token travels
beside the turn, never inside it, as far as the route exchange; the Harness bearer is the
single-turn token that exchange mints (ADR-077 §2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from adapters.inbound.conversation.block_runtime import (
    ManagedConversationControls,
    ManagedConversationDependencies,
    ManagedConversationRuntime,
    PreHarnessTerminalRequest,
    record_pre_harness_terminal,
)
from adapters.inbound.conversation.contract_codec import decode_contract
from adapters.outbound.agentcore.harness import (
    AgentCoreManagedHarness,
    ManagedHarnessTarget,
    bearer_harness_client,
)
from adapters.outbound.bedrock.guardrail import BedrockGuardrail
from adapters.outbound.dynamodb.audit import DynamoAgentQualityAuditStore
from adapters.outbound.dynamodb.evaluation_outbox import DynamoEvaluationHandoffStore
from adapters.outbound.dynamodb.page_context import DynamoPageContextStore
from adapters.outbound.token_service.route_exchange_client import LambdaRouteTokenExchange
from domain.conversation.audit import (
    AuditTerminalOutcomeV2,
    ConversationAuditRecordV6,
    RecordRetention,
)
from domain.conversation.managed import HarnessTurnV2, ManagedConversationRequestV1
from domain.conversation.page_context import (
    ConversationContextStatusV1,
    RedeemedPageContextV1,
    context_status_for,
    redeemed_page_context,
)
from domain.identity.authorization import CSRF_HEADER, AuthorizationRequest, authorize_session
from domain.identity.session import utc_now
from domain.semantics.page_context import (
    PageContextRedemption,
    PageContextRedemptionOutcome,
    PageContextRedemptionRequest,
    PageContextReference,
)
from domain.shared_kernel.errors import ErrorCode, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from adapters.config import Settings
    from domain.identity.tokens import JsonWebKeySet
    from ports.outbound.audit import AgentQualityAuditStorePort
    from ports.outbound.evaluation_outbox import EvaluationHandoffPort
    from ports.outbound.guardrail import GuardrailPort
    from ports.outbound.harness_token import HarnessTokenPort
    from ports.outbound.page_context import PageContextStorePort

__all__ = [
    "CONVERSATION_REFUSED",
    "GUARDRAIL_DECLINED",
    "AdmittedTurn",
    "ConversationRequestV3",
    "ConversationTooling",
    "HarnessBearer",
    "RedeemedTurnContext",
    "TurnHttpRequest",
    "VerifiedTurn",
    "actor_id_for",
    "managed_request_from",
    "redemption_failed",
    "turn_from",
]

CONVERSATION_REFUSED = ErrorCode(area="CHAT", number=1)
GUARDRAIL_DECLINED = ErrorCode(area="GUARDRAIL", number=2)
MAX_QUESTION_BYTES = 8_000
_MAX_SESSION_ID_LENGTH = 200
_HARNESS_QUALIFIER = "DEFAULT"
_ACTOR_NAMESPACE = "user-memory-v1"
"""Versions the Memory actor derivation. ADR-077 §6 made Memory per user; records under the
former per-conversation namespace are deliberately not migrated."""


def _check_question_and_session(question: str, session_id: str) -> None:
    if not question.strip():
        raise ValueError("a conversation request needs a question.")
    if len(question.encode()) > MAX_QUESTION_BYTES:
        raise ValueError("a conversation request question is too long.")
    if not session_id.strip() or len(session_id) > _MAX_SESSION_ID_LENGTH:
        raise ValueError("a conversation request session ID is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRequestV3:
    """The strict browser-authored /api/chat request contract (ADR-120 decision 4).

    It carries the question, the opaque conversation session and an optional opaque page
    reference, the page's `artifact_revision`. A malformed reference refuses the request like
    any other contract field; a well-formed one that fails redemption never fails the turn.
    v3 is the only version accepted: `ConversationRequest.v2` retired in the integration
    change, because the API and the browser that sends v3 deploy together.
    """

    schema_version: Literal["ConversationRequest.v3"] = "ConversationRequest.v3"
    question: str
    session_id: str
    page_context_reference: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "ConversationRequest.v3":
            raise ValueError("conversation request schema version is not supported.")
        _check_question_and_session(self.question, self.session_id)
        if self.page_context_reference is not None:
            PageContextReference(self.page_context_reference)


@dataclass(frozen=True, slots=True)
class TurnHttpRequest:
    """One HTTP request reduced to fields required by the authorization boundary."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    correlation_id: str


@dataclass(frozen=True, slots=True)
class VerifiedTurn:
    """Exact browser request after authorization and server-owned identity completion."""

    question: str
    verified_owner_id: str
    conversation_id: str
    custody_session_id: str
    correlation_id: str
    page_context_reference: PageContextReference | None = None


@dataclass(frozen=True, slots=True)
class RedeemedTurnContext:
    """What redemption made of a sent reference: the status, and the labels when used."""

    status: ConversationContextStatusV1
    page_context: RedeemedPageContextV1 | None = None


@dataclass(frozen=True, slots=True)
class HarnessBearer:
    """The route-minted Harness token for one turn, carried only as far as the Harness client.

    Adapter-owned and never part of `VerifiedTurn`, `HarnessTurnV2`, or any domain type
    (ADR-077 §2). Excluded from repr so it cannot reach a log line or a traceback.
    """

    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AdmittedTurn:
    """An authorized, decoded turn and the verified Cognito token the exchange will present.

    `subject_token` is the cookie token whose signature and claims admission just verified.
    It goes only to the route exchange, never to the Harness, and never reaches repr.
    """

    turn: VerifiedTurn
    subject_token: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationTooling:
    """Process-scope conversation composition; the Harness client is built per request.

    Everything here is shared across callers, including the exchange client, which carries
    no token of its own. The Harness is not, because its client presents one turn's minted
    token and caching it would present that token for another caller's turn.
    """

    target: ManagedHarnessTarget
    token_exchange: HarnessTokenPort
    audit_store: AgentQualityAuditStorePort
    evaluation_handoff: EvaluationHandoffPort | None
    guardrail: GuardrailPort | None
    controls: ManagedConversationControls
    page_context: PageContextStorePort

    def redeem(self, verified: VerifiedTurn) -> RedeemedTurnContext | None:
        """Redeem the turn's page reference at the request boundary (ADR-120 decision 5).

        `None` when no reference was sent. A storage failure raises, and the caller folds it
        to `invalid` with `redemption_failed`, so a failed redemption never fails the turn.
        """
        reference = verified.page_context_reference
        if reference is None:
            return None
        redemption = self.page_context.read_page_context(
            PageContextRedemptionRequest(
                reference,
                verified.verified_owner_id,
                verified.custody_session_id,
                utc_now(),
            )
        )
        return RedeemedTurnContext(
            status=context_status_for(redemption),
            page_context=(
                None
                if redemption.labels is None
                else redeemed_page_context(redemption.labels.display_labels)
            ),
        )

    def record_pre_harness(
        self, request: ManagedConversationRequestV1, outcome: AuditTerminalOutcomeV2
    ) -> ConversationAuditRecordV6 | None:
        """Write the receipt of a turn that ended before the Harness ran (ADR-081 §1)."""
        return record_pre_harness_terminal(
            PreHarnessTerminalRequest(
                request=request,
                terminal_outcome=outcome,
                audit_store=self.audit_store,
                controls=self.controls,
            )
        )

    def runtime_for(self, bearer: HarnessBearer) -> ManagedConversationRuntime:
        """Compose the managed runtime around a Harness that carries this turn's minted bearer."""
        harness = AgentCoreManagedHarness(
            self.target,
            _harness_session_id,
            client=bearer_harness_client(self.target, bearer.token),
        )
        return ManagedConversationRuntime(
            ManagedConversationDependencies(
                harness=harness,
                audit_store=self.audit_store,
                evaluation_handoff=self.evaluation_handoff,
            ),
            self.controls,
        )

    @classmethod
    def of(cls, settings: Settings) -> ConversationTooling:
        """Compose the only primary managed conversation path before readiness."""
        required = [
            ("ADL_HARNESS_ARN", settings.harness_arn),
            ("ADL_AGENT_QUALITY_AUDIT_TABLE", settings.agent_quality_audit_table),
            ("ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION", settings.token_route_exchange_function),
            # ADR-120 decision 9: the route reads `page#` records only, with `GetItem`.
            ("ADL_PAGE_CONTEXT_TABLE", settings.page_context_table),
        ]
        if settings.guardrails_enabled:
            required += [
                ("ADL_GUARDRAIL_ID", settings.guardrail_id),
                ("ADL_GUARDRAIL_VERSION", settings.guardrail_version),
            ]
        missing = [name for name, value in required if not value]
        if settings.receipt_retention_days is None:
            missing.append("ADL_RECEIPT_RETENTION_DAYS")
        if missing:
            raise ValidationError(
                code=CONVERSATION_REFUSED,
                description="The conversation route is not configured.",
                context={"missing": missing},
            )
        retention = RecordRetention(cast("int", settings.receipt_retention_days))
        guardrail = (
            BedrockGuardrail(settings.guardrail_id, settings.guardrail_version)
            if settings.guardrails_enabled
            else None
        )
        return cls(
            target=ManagedHarnessTarget(
                settings.harness_arn,
                _HARNESS_QUALIFIER,
                settings.harness_read_timeout_seconds,
            ),
            token_exchange=LambdaRouteTokenExchange(
                settings.token_route_exchange_function, region=settings.aws_region
            ),
            audit_store=DynamoAgentQualityAuditStore(
                settings.agent_quality_audit_table, retention=retention
            ),
            evaluation_handoff=(
                DynamoEvaluationHandoffStore(
                    settings.agent_quality_audit_table,
                    settings.evaluation_outbox_table,
                    retention=retention,
                )
                if settings.evaluation_outbox_table
                else None
            ),
            guardrail=guardrail,
            controls=ManagedConversationControls(
                prompt_digest=settings.harness_prompt_digest or None,
                guardrail_version=(
                    settings.guardrail_version if settings.guardrails_enabled else None
                ),
            ),
            page_context=DynamoPageContextStore(settings.page_context_table),
        )


def turn_from(request: TurnHttpRequest, *, keys: JsonWebKeySet, settings: Settings) -> AdmittedTurn:
    """Authorize, then strictly decode the body.

    Raises SessionAbsentError when no session cookie is present and IdentityError for every
    other denial, both before the body is read, so a caller who must refresh learns that
    first rather than after a body error a retry would repeat. There is no minimum-lifetime
    refusal: the Harness token's life is its own, not the cookie token's (ADR-077 §2).
    """
    session = authorize_session(
        AuthorizationRequest(
            cookie_header=request.headers.get("cookie", ""),
            method=request.method,
            keys=keys,
            expected_issuer=settings.cognito_issuer,
            expected_client_id=settings.cognito_client_id,
            now=utc_now(),
            csrf_header=request.headers.get(CSRF_HEADER, ""),
            allowed_origin=settings.app_base_url,
            origin=request.headers.get("origin", ""),
            sec_fetch_site=request.headers.get("sec-fetch-site", ""),
        )
    )
    browser = _conversation_request(request.body)
    subject = session.identity.subject
    return AdmittedTurn(
        turn=VerifiedTurn(
            question=browser.question,
            verified_owner_id=subject,
            conversation_id=_conversation_id(subject, browser.session_id),
            custody_session_id=browser.session_id,
            correlation_id=request.correlation_id,
            page_context_reference=(
                None
                if browser.page_context_reference is None
                else PageContextReference(browser.page_context_reference)
            ),
        ),
        subject_token=session.access_token,
    )


def redemption_failed() -> RedeemedTurnContext:
    """Return the context of a turn whose redemption failed: it runs without context."""
    return RedeemedTurnContext(
        status=context_status_for(PageContextRedemption(PageContextRedemptionOutcome.INVALID))
    )


def managed_request_from(
    verified: VerifiedTurn, context: RedeemedTurnContext | None = None
) -> ManagedConversationRequestV1:
    """Frame a closed execution request; page context is only what the server redeemed.

    The browser's reference names a record the presentation handler wrote. Its labels join the
    turn as quoted data, and the request keeps the reference and status for the receipt.
    """
    turn = HarnessTurnV2(
        question=verified.question,
        conversation_id=verified.conversation_id,
        actor_id=actor_id_for(verified.verified_owner_id),
        correlation_id=verified.correlation_id,
        page_context=None if context is None else context.page_context,
    )
    return ManagedConversationRequestV1(
        turn=turn,
        principal=verified.verified_owner_id,
        page_context_reference=verified.page_context_reference,
        context_status=None if context is None else context.status,
    )


def actor_id_for(owner_id: str) -> str:
    """Derive the per-user Memory actor from the verified `sub` alone (ADR-077 §6).

    The conversation is deliberately not an input: preferences and learned facts persist
    across one user's conversations, while the Harness session stays per conversation.
    """
    material = f"{_ACTOR_NAMESPACE}\0{owner_id}"
    return f"actor-{hashlib.sha256(material.encode()).hexdigest()}"


def _conversation_request(body: bytes) -> ConversationRequestV3:
    """Decode exactly `ConversationRequest.v3`; every other version is refused (ADR-120)."""
    refused = ValidationError(
        code=CONVERSATION_REFUSED,
        description="The request body must be a valid ConversationRequest.v3 object.",
    )
    if not body:
        raise refused
    try:
        text = body.decode("utf-8")
        raw: object = json.loads(text)
        # The version is stated, never defaulted: a body without one is not a v3 request.
        if not isinstance(raw, dict) or "schema_version" not in raw:
            raise refused
        return decode_contract(text, ConversationRequestV3)
    except TypeError, UnicodeDecodeError, ValueError:
        raise refused from None


def _conversation_id(owner_id: str, session_id: str) -> str:
    """Namespace browser continuity by verified owner without exposing either value."""
    digest = hashlib.sha256(f"{owner_id}\0{session_id}".encode()).hexdigest()
    return f"conversation-{digest}"


def _harness_session_id(conversation_id: str) -> str:
    """Map stable server conversation identity to a bounded managed Harness session."""
    return f"harness-{hashlib.sha256(conversation_id.encode()).hexdigest()}"
