# SPDX-License-Identifier: MIT-0
"""The validation-side review client, its decoders, and the review-group migration (ADR-078).

The decoders are exercised against the review function's own projection code, so a drift between
what the API returns and what the probes read fails here rather than in a live run.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import provision_quality_review_test_users as provision  # noqa: E402
from validation import quality_review_client as review_client  # noqa: E402

from adapters.config import (  # noqa: E402
    quality_review_settings,
    settings_without_client_secret,
    tracing_settings,
)
from domain.conversation.audit import (  # noqa: E402
    ANSWER_STREAM_CONTRACT,
    MANAGED_HARNESS_RUNTIME_V3,
    AuditFindingKindV2,
    AuditOperationCategoryV2,
    AuditOperationOutcomeV2,
    AuditPublicationOutcomeV2,
    AuditPublicConversationBlockKindV1,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    AuditToolStepV3,
    ConversationAuditRecordV6,
)
from domain.conversation.audit_findings import derive_findings  # noqa: E402
from domain.evaluation.linkage import (  # noqa: E402
    EvaluationDeliveryFamilyV1,
    EvaluationDeliveryOutcomeV1,
    EvaluationDeliveryReceiptV2,
    EvaluationResultStateV1,
    EvaluationResultV3,
)
from domain.evaluation.outbox import evaluation_delivery_ref  # noqa: E402
from domain.evaluation.release import EvaluationFamilyV1, EvaluationReleaseEvaluatorV1  # noqa: E402
from domain.identity.session import ReviewAccessLevel  # noqa: E402
from ports.outbound.audit import StoredRecord  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-078")]

_CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_DIGEST = "sha256:" + "a" * 64
_PRINCIPAL = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
_AT = "2026-09-02T12:00:00+00:00"
_EXPIRES_AT = 1_796_126_400
_JUDGE = EvaluationReleaseEvaluatorV1(
    evaluator_id="fictional-judge-01",
    evaluator_name="fictional_judge",
    revision="r1",
    model_id="fictional.judge-model-v1:0",
    prompt_digest="b" * 64,
)
_FAMILY = EvaluationDeliveryFamilyV1(
    family=EvaluationFamilyV1.V4, config_id="fictional_released_v4-0001", evaluators=(_JUDGE,)
)


def _record() -> ConversationAuditRecordV6:
    return ConversationAuditRecordV6(
        audit_id="audit-01",
        correlation_id=_CORRELATION_ID,
        trace_id=_TRACE_ID,
        operation_category=AuditOperationCategoryV2.COMPOSITION,
        operation_outcome=AuditOperationOutcomeV2.SUCCEEDED,
        publication_outcome=AuditPublicationOutcomeV2.PUBLISHED,
        terminal_outcome=AuditTerminalOutcomeV2.COMPLETE,
        runtime_version=MANAGED_HARNESS_RUNTIME_V3,
        prompt_catalog_digest=_DIGEST,
        page_schema_version="presentation.v2",
        response_contract_version="public-block-stream.v3",
        evaluation_admission_ref="admission-01",
        created_at=_AT,
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="presentation_publish",
                disposition=AuditToolStepDispositionV3.REJECTED,
                step_ref="finalizer-message-0",
                violations=("unsupported_numeric_literal",),
            ),
            AuditToolStepV3(
                ordinal=2,
                tool_name="presentation_publish",
                disposition=AuditToolStepDispositionV3.ACCEPTED,
                step_ref="finalizer-message-1",
            ),
        ),
        tool_custody_digest=_DIGEST,
        presentation_plan_digest=_DIGEST,
        public_block_kinds=(AuditPublicConversationBlockKindV1.PARAGRAPH,),
        public_block_count=1,
        guardrail_version="3",
        principal=_PRINCIPAL,
    )


def _stored(value: object) -> StoredRecord[object]:
    return StoredRecord(record=value, expires_at=_EXPIRES_AT)


@dataclass
class _Tables:
    """Returns each family's record with the fictional stored expiry, whatever is asked."""

    record: ConversationAuditRecordV6

    def get_delivery_with_expiry(self, _audit_id: str) -> None:
        return None

    def get_findings_with_expiry(self, _audit_id: str) -> tuple[StoredRecord[object], ...]:
        return tuple(_stored(finding) for finding in derive_findings(self.record))

    def get_evaluation_admission_with_expiry(self, *_: str) -> StoredRecord[object]:
        return _stored(SimpleNamespace(selected_at=_AT))

    def get_evaluation_delivery_with_expiry(self, *_: str) -> StoredRecord[object]:
        return _stored(
            EvaluationDeliveryReceiptV2(
                audit_id="audit-01",
                admission_ref="admission-01",
                delivery_ref=evaluation_delivery_ref("audit-01", "admission-01"),
                decided_at=_AT,
                outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
                not_dispatched_reason=None,
                families=(_FAMILY,),
            )
        )

    def get_evaluation_writer_receipt_with_expiry(self, *_: str) -> StoredRecord[object]:
        return _stored(SimpleNamespace(emitted_at=_AT, trace_id=_TRACE_ID))

    def get_evaluation_results_with_expiry(self, *_: str) -> tuple[StoredRecord[object], ...]:
        return (
            _stored(
                EvaluationResultV3(
                    audit_id="audit-01",
                    admission_ref="admission-01",
                    evaluator_ref=_JUDGE.evaluator_id,
                    evaluator_name=_JUDGE.evaluator_name,
                    result_ref="result-01",
                    state=EvaluationResultStateV1.COMPLETED,
                    label="good",
                    score=0.75,
                    evaluated_at=_AT,
                    family=EvaluationFamilyV1.V4,
                    config_id=_FAMILY.config_id,
                    evaluator_revision=_JUDGE.revision,
                    model_id=_JUDGE.model_id,
                    prompt_digest=_JUDGE.prompt_digest,
                )
            ),
        )


_ENVIRONMENT = {
    "ADL_ENVIRONMENT": "test",
    "ADL_AWS_REGION": "us-east-1",
    "ADL_COGNITO_USER_POOL_ID": "unused-quality-review",
    "ADL_COGNITO_CLIENT_ID": "unused-quality-review",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
    "ADL_APP_BASE_URL": "https://app.test.invalid",
    "ADL_API_BASE_URL": "https://api.test.invalid",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
    "ADL_ATHENA_WORKGROUP": "unused-quality-review",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "unused-quality-review",
    "ADL_SEMANTIC_LEASES_TABLE": "unused-quality-review",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "unused-quality-review",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "agent-quality-audit-test",
    "ADL_AGENT_QUALITY_FINDINGS_TABLE": "agent-quality-findings-test",
    "ADL_EVALUATION_DELIVERIES_TABLE": "evaluation-deliveries-test",
    "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "evaluation-writer-receipts-test",
    "ADL_EVALUATION_VERDICTS_TABLE": "evaluation-verdicts-test",
    "ADL_EVALUATION_RESULT_DEADLINE_SECONDS": "3600",
}


def _clear() -> None:
    sys.modules.pop("adapters.inbound.quality_review.handler", None)
    settings_without_client_secret.cache_clear()
    quality_review_settings.cache_clear()
    tracing_settings.cache_clear()


def _rendered(
    monkeypatch: pytest.MonkeyPatch, record: ConversationAuditRecordV6
) -> dict[str, object]:
    """One `QualityTurnReview.v1` document as the review function renders it, via JSON."""
    for key, value in _ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    _clear()
    try:
        module = cast("Any", importlib.import_module("adapters.inbound.quality_review.handler"))
        tables = _Tables(record)
        monkeypatch.setattr(module, "STORE", tables)
        monkeypatch.setattr(module, "FINDINGS", tables)
        monkeypatch.setattr(
            module,
            "EVALUATION",
            SimpleNamespace(deliveries=tables, writer_receipts=tables, verdicts=tables),
        )
        review = module._turn_review(
            StoredRecord(record=record, expires_at=_EXPIRES_AT),
            frozenset({ReviewAccessLevel.QUALITY_REVIEWER}),
        )
        return cast("dict[str, object]", json.loads(json.dumps(review)))
    finally:
        _clear()


@pytest.fixture
def api_review(monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, object]]:
    """Render a stored block-stream receipt, which still reads after ADR-083."""
    yield _rendered(monkeypatch, _record())


def _streamed_record() -> ConversationAuditRecordV6:
    """Build a streamed-answer receipt (ADR-083): governed steps, no plan, counted characters."""
    return replace(
        _record(),
        page_schema_version="answer.markdown.v1",
        response_contract_version=ANSWER_STREAM_CONTRACT,
        trajectory=(
            AuditToolStepV3(
                ordinal=1,
                tool_name="catalog_list_metrics",
                disposition=AuditToolStepDispositionV3.SUCCEEDED,
                step_ref="7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b",
            ),
        ),
        presentation_plan_digest=None,
        public_block_kinds=(),
        public_block_count=0,
        released_character_count=512,
    )


def test_the_receipt_decodes_from_the_review_function_output(
    api_review: dict[str, object],
) -> None:
    receipt = review_client.reviewed_receipt(api_review)
    record = _record()
    assert (receipt.audit_id, receipt.correlation_id, receipt.trace_id) == (
        "audit-01",
        _CORRELATION_ID,
        _TRACE_ID,
    )
    assert receipt.trajectory == record.trajectory
    assert receipt.terminal_outcome is AuditTerminalOutcomeV2.COMPLETE
    assert receipt.operation_category is AuditOperationCategoryV2.COMPOSITION
    assert receipt.evaluation_admission_ref == "admission-01"
    assert receipt.expires_at == _EXPIRES_AT


@pytest.mark.covers("ADR-083")
def test_a_streamed_receipt_decodes_its_answer_format_and_released_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _rendered(monkeypatch, _streamed_record())
    publication = cast("dict[str, Any]", review["audit"])["publication"]
    assert publication["released_character_count"] == 512

    receipt = review_client.reviewed_receipt(review)
    assert receipt.response_contract_version == "conversation-stream.v4"
    assert receipt.page_schema_version == "answer.markdown.v1"
    assert receipt.released_character_count == 512
    assert receipt.presentation_plan_digest is None
    assert (receipt.public_block_kinds, receipt.public_block_count) == ((), 0)
    assert receipt.trajectory == _streamed_record().trajectory
    assert review_client.reviewed_findings(review) == ()


def test_a_block_stream_receipt_released_no_streamed_characters(
    api_review: dict[str, object],
) -> None:
    receipt = review_client.reviewed_receipt(api_review)
    assert receipt.released_character_count == 0
    assert receipt.page_schema_version == "presentation.v2"


def test_a_review_api_that_predates_the_character_count_reads_as_zero(
    api_review: dict[str, object],
) -> None:
    audit = cast("dict[str, Any]", api_review["audit"])
    publication = {
        key: value
        for key, value in audit["publication"].items()
        if key != "released_character_count"
    }
    older = {**api_review, "audit": {**audit, "publication": publication}}
    assert review_client.reviewed_receipt(older).released_character_count == 0
    malformed = {
        **api_review,
        "audit": {**audit, "publication": {**publication, "released_character_count": "12"}},
    }
    with pytest.raises(ValueError, match="released character count"):
        review_client.reviewed_receipt(malformed)


def test_the_findings_decode_to_what_the_processor_derives(api_review: dict[str, object]) -> None:
    findings = review_client.reviewed_findings(api_review)
    assert [(finding.kind, finding.step_ordinal) for finding in findings] == [
        (AuditFindingKindV2.FINALIZER_REPAIR, 1)
    ]
    assert {finding.expires_at for finding in findings} == {_EXPIRES_AT}


def test_the_evaluation_chain_decodes_its_delivery_writer_and_verdicts(
    api_review: dict[str, object],
) -> None:
    evaluation = review_client.reviewed_evaluation(api_review)
    assert evaluation is not None
    assert evaluation.admitted is True
    assert evaluation.writer_present is True
    assert evaluation.writer_trace_id == _TRACE_ID
    assert evaluation.delivery == review_client.ReviewedDelivery(
        outcome=EvaluationDeliveryOutcomeV1.DISPATCHED,
        not_dispatched_reason=None,
        families=(_FAMILY,),
    )
    (verdict,) = evaluation.verdicts
    assert (verdict.evaluator_ref, verdict.label, verdict.config_id) == (
        _JUDGE.evaluator_id,
        "good",
        _FAMILY.config_id,
    )


def test_no_decoded_section_carries_the_principal(api_review: dict[str, object]) -> None:
    assert _PRINCIPAL not in json.dumps(api_review)


def test_a_section_the_api_did_not_return_is_refused(api_review: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="section"):
        review_client.reviewed_receipt({**api_review, "audit": None})


@dataclass
class _Response:
    status_code: int
    text: str = ""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@dataclass
class _Http:
    statuses: list[int]
    bearers: list[str] = field(default_factory=list[str])

    def post(self, _url: str, *, headers: dict[str, str], **_: object) -> _Response:
        self.bearers.append(headers["Authorization"])
        status = self.statuses.pop(0)
        body = json.dumps({"schema_version": "QualityTurnReview.v1"}) if status == 200 else ""
        return _Response(status, body)


def _client(monkeypatch: pytest.MonkeyPatch, *statuses: int) -> tuple[Any, _Http, list[int]]:
    http = _Http(list(statuses))
    monkeypatch.setattr(review_client, "HttpsSession", lambda: http)
    signed: list[int] = []

    def sign_in() -> str:
        signed.append(1)
        return f"token-{len(signed)}"

    return (
        review_client.ReviewClient(origin="https://app.test.invalid", sign_in=sign_in),
        http,
        signed,
    )


def test_a_refused_bearer_is_renewed_once_and_the_request_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, http, signed = _client(monkeypatch, 403, 200)
    assert client.turn(_CORRELATION_ID) == {"schema_version": "QualityTurnReview.v1"}
    assert http.bearers == ["Bearer token-1", "Bearer token-2"]
    assert len(signed) == 2


def test_a_second_refusal_is_raised_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client, http, _ = _client(monkeypatch, 403, 403)
    with pytest.raises(review_client.ReviewUnavailableError, match="review_status_403"):
        client.turn(_CORRELATION_ID)
    assert len(http.bearers) == 2


def test_the_non_disclosing_absence_reads_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch, 404)
    assert client.turn(_CORRELATION_ID) is None


def test_the_bearer_never_appears_in_the_client_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch, 200)
    client.turn(_CORRELATION_ID)
    assert "token-" not in repr(client)


def test_the_persona_set_binds_each_persona_to_its_review_groups() -> None:
    assert dict(provision.PERSONAS) == {
        "reviewer": ("quality-reviewer",),
        "attributor": ("quality-reviewer", "audit-attribution"),
        "attribution-only": ("audit-attribution",),
        "denied": (),
        "probe-a": (),
        "probe-b": (),
    }


@dataclass
class _Pool:
    """In-memory user-group memberships recording every write."""

    groups: dict[str, set[str]]
    users: set[str] = field(default_factory=set[str])
    calls: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    def admin_list_groups_for_user(self, **request: Any) -> dict[str, object]:
        username = request["Username"]
        names = sorted(group for group, members in self.groups.items() if username in members)
        return {"Groups": [{"GroupName": name} for name in names]}

    def admin_add_user_to_group(self, **request: str) -> None:
        self.calls.append(("add", request["Username"], request["GroupName"]))
        self.groups.setdefault(request["GroupName"], set()).add(request["Username"])

    def admin_remove_user_from_group(self, **request: str) -> None:
        self.calls.append(("remove", request["Username"], request["GroupName"]))
        self.groups[request["GroupName"]].discard(request["Username"])

    def admin_delete_user(self, **request: str) -> None:
        if request["Username"] not in self.users:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "absent"}},
                "AdminDeleteUser",
            )
        self.users.remove(request["Username"])
        self.calls.append(("delete", request["Username"], ""))


class _Parameters:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_parameter(self, *, Name: str) -> None:  # noqa: N803 - boto3 keyword
        self.deleted.append(Name)


def test_provisioning_reconciles_review_groups_exactly_and_keeps_other_groups() -> None:
    persona = provision.Persona(label="reviewer", groups=("quality-reviewer",))
    pool = _Pool(
        groups={
            "audit-attribution": {persona.username},
            "prompt-admin": {persona.username},
        }
    )

    provision._reconcile_groups(cognito=pool, pool_id="pool", persona=persona)  # pyright: ignore[reportPrivateUsage]

    member_of = sorted(
        group for group, members in pool.groups.items() if persona.username in members
    )
    assert member_of == ["prompt-admin", "quality-reviewer"]


def test_retired_synthetic_personas_are_deleted_with_their_credentials() -> None:
    audit = provision.Persona(label="audit", groups=())
    pool = _Pool(groups={}, users={audit.username})
    parameters = _Parameters()

    removed = provision._retire_personas(  # pyright: ignore[reportPrivateUsage]
        ssm=parameters, cognito=pool, pool_id="pool", environment="dev"
    )

    assert removed == 1
    assert pool.users == set()
    assert parameters.deleted == [
        provision.Persona(label=label, groups=()).parameter_name("dev")
        for label in provision.RETIRED_PERSONAS
    ]
