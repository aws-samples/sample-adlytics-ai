# SPDX-License-Identifier: MIT-0
"""Read one turn's receipt, findings, and evaluation through the Quality Review API (ADR-078 §5).

Acceptance probes read the protected record tables only through `POST /api/quality-review/turn`
with a reviewer bearer, because the receipts table denies every other reader. This module is
that client and the strict decoders from the `QualityTurnReview.v1` response to the values the
probes attest. It never prints, logs, or persists the bearer or a response body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from cognito_personas import admin_sign_in, load_persona, public_client, published_parameter

from adapters.outbound.https import HttpsSession, require_https
from domain.conversation.audit import (
    AuditFindingKindV2,
    AuditFindingSeverityV2,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditPublicConversationBlockKindV1,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
)
from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationNotDispatchedReasonV1,
    EvaluationResultStateV1,
)
from domain.evaluation.release import EvaluationFamilyV1, EvaluationReleaseEvaluatorV1

if TYPE_CHECKING:
    from collections.abc import Callable

    import boto3

__all__ = [
    "ATTRIBUTOR_PERSONA",
    "DEFAULT_REVIEWER_PERSONA",
    "PREDATES_GATE",
    "ReviewClient",
    "ReviewUnavailableError",
    "ReviewedDelivery",
    "ReviewedEvaluation",
    "ReviewedFinding",
    "ReviewedReceipt",
    "ReviewedVerdict",
    "reviewed_evaluation",
    "reviewed_findings",
    "reviewed_principal",
    "reviewed_receipt",
    "reviewer_client",
]

DEFAULT_REVIEWER_PERSONA = "reviewer"
"""The synthetic dev persona in `quality-reviewer` only, provisioned by the persona script."""

ATTRIBUTOR_PERSONA = "attributor"
"""The synthetic dev persona in both review groups, the only one that receives `attribution`."""
_TURN_PATH = "/api/quality-review/turn"
_HTTP_OK = 200
_HTTP_NOT_FOUND = 404
_REFUSED = frozenset({401, 403})
_TIMEOUT_SECONDS = 30


class ReviewUnavailableError(Exception):
    """The review API answered neither a review nor the non-disclosing absence."""


@dataclass(slots=True)
class ReviewClient:
    """A reviewer's bearer and the public origin its review requests go through.

    The bearer is obtained lazily and obtained once more when a request is refused, because an
    acceptance run can outlast one access token; a second refusal is raised, never retried.
    """

    origin: str
    sign_in: Callable[[], str] = field(repr=False)
    _bearer: str | None = field(default=None, repr=False)

    def turn(self, correlation_id: str) -> dict[str, object] | None:
        """Return one turn's review by correlation, or `None` while it reads as absent."""
        body = json.dumps(
            {"selector": {"correlation_id": correlation_id}}, separators=(",", ":")
        ).encode()
        status, text = self._post(body)
        if status in _REFUSED:
            self._bearer = None
            status, text = self._post(body)
        if status == _HTTP_NOT_FOUND:
            return None
        if status != _HTTP_OK:
            raise ReviewUnavailableError(f"review_status_{status}")
        document: object = json.loads(text)
        if not isinstance(document, dict):
            raise ReviewUnavailableError("review_body_malformed")
        review = cast("dict[str, object]", document)
        if review.get("schema_version") != "QualityTurnReview.v1":
            raise ReviewUnavailableError("review_schema_unexpected")
        return review

    def _post(self, body: bytes) -> tuple[int, str]:
        if self._bearer is None:
            self._bearer = self.sign_in()
        with HttpsSession().post(
            require_https(f"{self.origin.rstrip('/')}{_TURN_PATH}"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer}",
            },
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            status = response.status_code
            return status, response.text if status == _HTTP_OK else ""


def reviewer_client(
    session: boto3.Session, environment: str, persona: str = DEFAULT_REVIEWER_PERSONA
) -> ReviewClient:
    """Bind the published public origin and the reviewer persona's bearer sign-in."""
    ssm = session.client("ssm")
    credential = load_persona(ssm, environment, persona)
    client = public_client(session, environment)
    cognito = session.client("cognito-idp")

    def sign_in() -> str:
        return admin_sign_in(cognito, credential, client).access_token

    return ReviewClient(
        origin=published_parameter(ssm, environment, "publicBaseUrl"), sign_in=sign_in
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"review {label} is not an object")
    return cast("dict[str, object]", value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"review {label} is not a list")
    return cast("list[object]", value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"review {label} is not a string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _whole(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"review {label} is not a whole number")
    return value


def _section(review: dict[str, object], name: str) -> dict[str, object]:
    return _object(review.get(name), f"{name} section")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedReceipt:
    """The receipt fields a probe attests, decoded from the `audit` section.

    Carries what `derive_findings` and `aggregate_metric` read, so a probe recomputes the
    processor's outputs with the domain's own calculations rather than a copy of them.
    """

    audit_id: str
    correlation_id: str
    trace_id: str | None
    created_at: str
    runtime_version: str
    prompt_catalog_digest: str | None
    response_contract_version: str
    operation_category: AuditOperationCategoryV2
    operation_outcome: AuditOperationOutcomeV2
    publication_outcome: AuditPublicationOutcomeV2
    terminal_outcome: AuditTerminalOutcomeV2
    failure_error_code: str | None
    completion_refusal_stage: str | None
    trajectory: tuple[AuditToolStepV3, ...]
    tool_custody_digest: str | None
    presentation_plan_digest: str | None
    public_block_kinds: tuple[AuditPublicConversationBlockKindV1, ...]
    public_block_count: int
    evaluation_admission_ref: str | None
    expires_at: int | None
    page_schema_version: str | None = None
    released_character_count: int = 0
    """Streamed answer characters (ADR-083); an API that predates the field reads as zero."""


def reviewed_principal(review: dict[str, object]) -> str | None:
    """Return the receipt's principal from `attribution`, or `None` when the section is absent.

    Only a caller in both review groups receives the section (ADR-081 §3). A probe compares the
    value with its own persona's subject and never records it.
    """
    attribution = review.get("attribution")
    if attribution is None:
        return None
    return _text(_object(attribution, "attribution").get("principal"), "attribution principal")


def reviewed_receipt(review: dict[str, object]) -> ReviewedReceipt:
    """Strictly decode the `audit` section; any shape the API does not return is refused."""
    audit = _section(review, "audit")
    if audit.get("schema_version") != "QualityAuditReview.v3":
        raise ValueError("review audit section schema is unexpected")
    identity = _object(audit.get("identity"), "audit identity")
    provenance = _object(audit.get("provenance"), "audit provenance")
    outcome = _object(audit.get("outcome"), "audit outcome")
    publication = _object(audit.get("publication"), "audit publication")
    expires_at = audit.get("expires_at")
    return ReviewedReceipt(
        audit_id=_text(identity.get("audit_id"), "audit id"),
        correlation_id=_text(identity.get("correlation_id"), "correlation id"),
        trace_id=_optional_text(identity.get("trace_id"), "trace id"),
        created_at=_text(identity.get("created_at"), "creation time"),
        runtime_version=_text(provenance.get("runtime_version"), "runtime version"),
        prompt_catalog_digest=_optional_text(
            provenance.get("prompt_catalog_digest"), "prompt digest"
        ),
        response_contract_version=_text(
            provenance.get("response_contract_version"), "response contract"
        ),
        operation_category=AuditOperationCategoryV2(
            _text(outcome.get("operation_category"), "operation category")
        ),
        operation_outcome=AuditOperationOutcomeV2(
            _text(outcome.get("operation_outcome"), "operation outcome")
        ),
        publication_outcome=AuditPublicationOutcomeV2(
            _text(outcome.get("publication_outcome"), "publication outcome")
        ),
        terminal_outcome=AuditTerminalOutcomeV2(
            _text(outcome.get("terminal_outcome"), "terminal outcome")
        ),
        failure_error_code=_optional_text(outcome.get("failure_error_code"), "failure code"),
        completion_refusal_stage=_optional_text(
            outcome.get("completion_refusal_stage"), "completion refusal stage"
        ),
        trajectory=tuple(
            _step(_object(step, "trajectory step"))
            for step in _list(audit.get("trajectory"), "trajectory")
        ),
        tool_custody_digest=_optional_text(
            publication.get("tool_custody_digest"), "tool custody digest"
        ),
        presentation_plan_digest=_optional_text(
            publication.get("presentation_plan_digest"), "plan digest"
        ),
        public_block_kinds=tuple(
            AuditPublicConversationBlockKindV1(_text(kind, "block kind"))
            for kind in _list(publication.get("public_block_kinds"), "block kinds")
        ),
        public_block_count=_whole(publication.get("public_block_count"), "block count"),
        evaluation_admission_ref=_optional_text(
            publication.get("evaluation_admission_ref"), "admission ref"
        ),
        expires_at=None if expires_at is None else _whole(expires_at, "receipt expiry"),
        page_schema_version=_optional_text(
            provenance.get("page_schema_version"), "page schema version"
        ),
        released_character_count=_whole(
            publication.get("released_character_count", 0), "released character count"
        ),
    )


def _step(step: dict[str, object]) -> AuditToolStepV3:
    selected = step.get("selected_into_plan")
    if selected is not None and not isinstance(selected, bool):
        raise ValueError("review trajectory selection is malformed")
    return AuditToolStepV3(
        ordinal=_whole(step.get("ordinal"), "step ordinal"),
        tool_name=_text(step.get("tool_name"), "tool name"),
        disposition=AuditToolStepDispositionV3(_text(step.get("disposition"), "disposition")),
        step_ref=_optional_text(step.get("step_ref"), "step ref"),
        violations=tuple(
            _text(item, "violation") for item in _list(step.get("violations"), "violations")
        ),
        selected_into_plan=selected,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedFinding:
    """One finding as the `findings` section returns it: closed kind, severity, and anchor."""

    kind: AuditFindingKindV2
    severity: AuditFindingSeverityV2
    created_at: str
    step_ordinal: int | None
    expires_at: int | None


def reviewed_findings(review: dict[str, object]) -> tuple[ReviewedFinding, ...]:
    """Strictly decode the `findings` section."""
    section = _section(review, "findings")
    if section.get("schema_version") != "QualityFindingsReview.v3":
        raise ValueError("review findings section schema is unexpected")
    findings: list[ReviewedFinding] = []
    for raw in _list(section.get("findings"), "findings"):
        finding = _object(raw, "finding")
        step = finding.get("step")
        expires_at = finding.get("expires_at")
        findings.append(
            ReviewedFinding(
                kind=AuditFindingKindV2(_text(finding.get("kind"), "finding kind")),
                severity=AuditFindingSeverityV2(_text(finding.get("severity"), "severity")),
                created_at=_text(finding.get("created_at"), "finding time"),
                step_ordinal=(
                    None
                    if step is None
                    else _whole(_object(step, "finding step").get("ordinal"), "step ordinal")
                ),
                expires_at=None if expires_at is None else _whole(expires_at, "finding expiry"),
            )
        )
    return tuple(findings)


PREDATES_GATE = "predates_evaluation_gate"
"""The closed reason the API gives a delivery receipt written before the ADR-079 gate."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedDelivery:
    """The dispatcher's decision as the `evaluation` chain returns it."""

    outcome: EvaluationDeliveryOutcomeV1
    not_dispatched_reason: EvaluationNotDispatchedReasonV1 | None
    families: tuple[EvaluationDeliveryFamilyV1, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedVerdict:
    """One evaluator's persisted verdict and the provenance it was produced under."""

    evaluator_ref: str
    evaluator_name: str
    state: EvaluationResultStateV1
    label: str | None
    family: EvaluationFamilyV1
    config_id: str
    evaluator_revision: str
    model_id: str
    prompt_digest: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedEvaluation:
    """One admitted turn's evaluation chain as the API returns it.

    `delivery` is `None` while no receipt exists, and `PREDATES_GATE` for a receipt written
    before the gate, which is never current dispatch evidence.
    """

    state: str
    admitted: bool
    delivery: ReviewedDelivery | str | None
    writer_present: bool
    writer_trace_id: str | None
    verdicts: tuple[ReviewedVerdict, ...]
    results: tuple[object, ...]
    """The raw result projections, for a probe's content-free check; never printed."""


def reviewed_evaluation(review: dict[str, object]) -> ReviewedEvaluation | None:
    """Strictly decode the `evaluation` section; `None` when the turn was never admitted."""
    section = _section(review, "evaluation")
    if section.get("schema_version") != "QualityEvaluationReview.v3":
        raise ValueError("review evaluation section schema is unexpected")
    state = _text(section.get("state"), "evaluation state")
    reason = section.get("reason")
    chain_value = section.get("chain")
    if chain_value is None:
        if (state, reason) != ("not_selected", "not_admitted"):
            raise ValueError("review evaluation without a chain must be not admitted")
        return None
    chain = _object(chain_value, "evaluation chain")
    outcome = chain.get("delivery_outcome")
    delivery: ReviewedDelivery | str | None
    if outcome is not None:
        dispatched = EvaluationDeliveryOutcomeV1(_text(outcome, "delivery outcome"))
        delivery = ReviewedDelivery(
            outcome=dispatched,
            not_dispatched_reason=(
                EvaluationNotDispatchedReasonV1(_text(reason, "not dispatched reason"))
                if dispatched is EvaluationDeliveryOutcomeV1.NOT_DISPATCHED
                else None
            ),
            families=tuple(
                _family(_object(family, "delivery family"))
                for family in _list(chain.get("families"), "delivery families")
            ),
        )
    elif reason == PREDATES_GATE:
        delivery = PREDATES_GATE
    else:
        delivery = None
    results = tuple(_list(section.get("results"), "evaluation results"))
    return ReviewedEvaluation(
        state=state,
        admitted=chain.get("admitted") is not None,
        delivery=delivery,
        writer_present=chain.get("input_emitted") is not None,
        writer_trace_id=_optional_text(chain.get("input_trace_id"), "writer trace id"),
        verdicts=tuple(_verdict(_object(result, "evaluation result")) for result in results),
        results=results,
    )


def _family(family: dict[str, object]) -> EvaluationDeliveryFamilyV1:
    return EvaluationDeliveryFamilyV1(
        family=EvaluationFamilyV1(_text(family.get("family"), "family")),
        config_id=_text(family.get("config_id"), "config id"),
        evaluators=tuple(
            EvaluationReleaseEvaluatorV1(
                evaluator_id=_text(item.get("evaluator_id"), "evaluator id"),
                evaluator_name=_text(item.get("evaluator_name"), "evaluator name"),
                revision=_text(item.get("revision"), "evaluator revision"),
                model_id=_text(item.get("model_id"), "evaluator model"),
                prompt_digest=_text(item.get("prompt_digest"), "evaluator prompt digest"),
            )
            for item in (
                _object(raw, "family evaluator")
                for raw in _list(family.get("evaluators"), "family evaluators")
            )
        ),
    )


def _verdict(result: dict[str, object]) -> ReviewedVerdict:
    return ReviewedVerdict(
        evaluator_ref=_text(result.get("evaluator_ref"), "evaluator ref"),
        evaluator_name=_text(result.get("evaluator_name"), "evaluator name"),
        state=EvaluationResultStateV1(_text(result.get("state"), "result state")),
        label=_optional_text(result.get("label"), "result label"),
        family=EvaluationFamilyV1(_text(result.get("family"), "result family")),
        config_id=_text(result.get("config_id"), "result config"),
        evaluator_revision=_text(result.get("evaluator_revision"), "result revision"),
        model_id=_text(result.get("model_id"), "result model"),
        prompt_digest=_text(result.get("prompt_digest"), "result prompt digest"),
    )
