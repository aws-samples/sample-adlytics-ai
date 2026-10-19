# SPDX-License-Identifier: MIT-0
"""Immutable prompt-registry lifecycle request construction and conditional outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

import adapters.outbound.dynamodb.insight_prompt_registry as registry_module
from adapters.outbound.dynamodb.insight_prompt_registry import (
    PROMPT_REGISTRY_MALFORMED,
    DynamoInsightPromptRegistry,
)
from domain.presentation.insights import (
    PROMPT_ACTIVATION_CALIBRATION_REQUIRED,
    InsightCalibrationCaseResult,
    InsightCalibrationClaimDisposition,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightCalibrationResult,
    InsightClaimedPromptCalibrationRequest,
    InsightModelRoute,
    InsightPendingPromptVersion,
    InsightPromptActivationRequest,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationRequest,
    InsightPromptRetirementRequest,
    InsightPromptSeedRequest,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightSlotId,
)
from domain.presentation.prompt_authorship import OperatorPromptAuthor, SystemPromptAuthor
from domain.presentation.prompt_composition import PromptFragmentDigests
from domain.shared_kernel.errors import InvariantError

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068", "ADR-069")]

_SUBJECT = "fictional-operator-subject-01"
_CREATED = datetime(2026, 10, 1, 15, 25, tzinfo=UTC)
_SEEDED = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


@guard_reserved_words
@dataclass
class PromptRegistryClient:
    items: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    transactions: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    updates: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    deletes: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    transaction_error: ClientError | None = None
    update_error: ClientError | None = None
    delete_error: ClientError | None = None
    update_attributes: dict[str, object] | None = None

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = cast("dict[str, dict[str, str]]", kwargs["Key"])
        record_key = key["record_key"]["S"]
        return {"Item": self.items.get(record_key)} if record_key in self.items else {}

    def transact_write_items(self, **kwargs: object) -> None:
        self.transactions.append(kwargs)
        if self.transaction_error is not None:
            raise self.transaction_error

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.updates.append(kwargs)
        if self.update_error is not None:
            raise self.update_error
        return {"Attributes": self.update_attributes or _version()}

    def delete_item(self, **kwargs: object) -> None:
        self.deletes.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error

    def batch_get_item(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Responses": {"fictional-prompt-registry": list(self.items.values())}}

    def query(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Items": list(self.items.values())}


def _registry(client: PromptRegistryClient) -> DynamoInsightPromptRegistry:
    return DynamoInsightPromptRegistry("fictional-prompt-registry", cast("DynamoDBClient", client))


def _counter() -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_COUNTER"},
        "version": {"N": "1"},
    }


def _version() -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_VERSION"},
        "slot_id": {"S": "contribution"},
        "version": {"N": "1"},
        "prompt": {"S": "A reviewed fictional prompt."},
        "model_route": {"S": "deep"},
        "calibration": {"S": "pending"},
    }


def _calibration_result(outcome: str = "passed") -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_CALIBRATION_RESULT"},
        "slot_id": {"S": "contribution"},
        "version": {"N": "1"},
        "outcome": {"S": outcome},
    }


def _pointer() -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_POINTER"},
        "slot_id": {"S": "contribution"},
        "active_version": {"N": "2"},
        "prompt": {"S": "An active fictional prompt."},
        "model_route": {"S": "quick"},
    }


def _passed_result() -> InsightCalibrationResult:
    return InsightCalibrationResult(
        tuple(
            InsightCalibrationCaseResult(kind, InsightCalibrationOutcome.PASSED)
            for kind in InsightCalibrationKind
        )
    )


def test_create_version_fences_the_counter_and_persists_the_allowlisted_route() -> None:
    client = PromptRegistryClient(items={"COUNTER": _counter()})

    version = _registry(client).create_version(
        InsightPromptVersionCreateRequest(
            InsightSlotId.CONTRIBUTION,
            "A reviewed fictional prompt.",
            InsightModelRoute.DEEP,
            author=OperatorPromptAuthor(_SUBJECT),
            created_at=_CREATED,
        )
    )

    transaction = client.transactions[0]
    writes = cast("list[dict[str, object]]", transaction["TransactItems"])
    version_put = cast("dict[str, object]", writes[0]["Put"])
    version_item = cast("dict[str, dict[str, str]]", version_put["Item"])
    # The counter's optimistic check lives in its own conditional Update: a transaction
    # cannot include two operations on one item, so a separate ConditionCheck is invalid.
    counter_update = cast("dict[str, object]", writes[2]["Update"])
    outbox_put = cast("dict[str, object]", writes[1]["Put"])
    outbox_item = cast("dict[str, dict[str, str]]", outbox_put["Item"])
    assert version == 2
    assert len(writes) == 3
    assert counter_update["ConditionExpression"] == "#version = :expected"
    assert counter_update["UpdateExpression"] == "SET #version = :next"
    assert version_item["model_route"] == {"S": "deep"}
    assert version_item["calibration"] == {"S": "pending"}
    assert outbox_item["record_key"] == {"S": "CALIBRATION_OUTBOX#2"}
    assert outbox_item["record_type"] == {"S": "PROMPT_CALIBRATION_OUTBOX"}


def test_record_calibration_persists_all_cases_only_for_a_pending_version() -> None:
    client = PromptRegistryClient()

    _registry(client).record_calibration(
        InsightPromptCalibrationRequest(InsightSlotId.CONTRIBUTION, 2, _passed_result())
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    version_update = cast("dict[str, object]", writes[0]["Update"])
    persisted = [cast("dict[str, object]", entry["Put"])["Item"] for entry in writes[1:]]
    records = [cast("dict[str, dict[str, str]]", item) for item in persisted]
    assert version_update["UpdateExpression"] == "SET #calibration = :outcome"
    assert version_update["ConditionExpression"] == (
        "#record_type = :record_type AND #slot_id = :slot_id "
        "AND #version = :version AND #calibration = :pending"
    )
    assert cast("dict[str, dict[str, str]]", version_update["ExpressionAttributeValues"])[
        ":outcome"
    ] == {"S": "passed"}
    assert len(records) == 8
    assert records[0]["outcome"] == {"S": "passed"}
    assert {record["calibration_kind"]["S"] for record in records[1:]} == {
        "known-correct",
        "exploratory",
        "contradiction",
        "fluent-unsupported",
        "derived-figure",
        "miscomputed-derivation",
        "incompatible-basis",
    }


@pytest.mark.covers("ADR-076")
def test_a_retained_three_case_version_row_stays_readable_beside_the_exploratory_kind() -> None:
    """Versions calibrated before the exploratory family are durable.

    A full-set read requirement would make every retained row an InvariantError
    after the kind ships.
    """
    client = PromptRegistryClient(
        items={
            "VERSION#1": {**_version(), "calibration": {"S": "passed"}},
            **{
                f"CASE#{kind}": {
                    "record_type": {"S": "PROMPT_CALIBRATION_CASE"},
                    "slot_id": {"S": "contribution"},
                    "version": {"N": "1"},
                    "calibration_kind": {"S": kind},
                    "outcome": {"S": "passed"},
                }
                for kind in ("known-correct", "contradiction", "fluent-unsupported")
            },
        }
    )

    records = _registry(client).versions(InsightSlotId.CONTRIBUTION)

    assert [record.version for record in records] == [1]
    assert {case.kind for case in records[0].calibration_cases} == {
        InsightCalibrationKind.KNOWN_CORRECT,
        InsightCalibrationKind.CONTRADICTION,
        InsightCalibrationKind.FLUENT_UNSUPPORTED,
    }


def test_pending_version_returns_only_the_stored_pending_prompt_and_allowed_route() -> None:
    client = PromptRegistryClient(items={"VERSION#1": _version()})

    pending = _registry(client).pending_version(InsightSlotId.CONTRIBUTION, 1)

    assert pending == InsightPendingPromptVersion(
        InsightSlotId.CONTRIBUTION,
        1,
        "A reviewed fictional prompt.",
        InsightModelRoute.DEEP,
        InsightPromptTier.CARD,
    )


@pytest.mark.covers("ADR-076")
def test_pending_deep_version_retains_its_tier_for_production_envelope_calibration() -> None:
    version = {**_version(), "tier": {"S": "deep"}}

    pending = _registry(PromptRegistryClient(items={"VERSION#1": version})).pending_version(
        InsightSlotId.CONTRIBUTION, 1
    )

    assert pending.tier is InsightPromptTier.DEEP


def test_active_activation_exposes_the_pointer_route() -> None:
    """A retained pre-tier POINTER item (no tier attribute) reads as the card pointer."""
    activation = _registry(PromptRegistryClient(items={"POINTER": _pointer()})).active(
        InsightSlotId.CONTRIBUTION, InsightPromptTier.CARD
    )

    assert activation is not None
    assert activation.version == "v2"
    assert activation.model_route is InsightModelRoute.QUICK
    # Retained records carry no digest marker: they read as unsourced.
    assert activation.fragment_digest is None


def _deep_pointer() -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_POINTER"},
        "slot_id": {"S": "contribution"},
        "tier": {"S": "deep"},
        "active_version": {"N": "3"},
        "prompt": {"S": "An active fictional deep prompt."},
        "model_route": {"S": "deep"},
        "fragment_digest": {"S": "c" * 64},
    }


def _composed_deep_version() -> dict[str, object]:
    return {
        "record_type": {"S": "PROMPT_VERSION"},
        "slot_id": {"S": "contribution"},
        "tier": {"S": "deep"},
        "version": {"N": "1"},
        "prompt": {"S": "A reviewed fictional composed deep prompt."},
        "model_route": {"S": "deep"},
        "calibration": {"S": "passed"},
        "fragment_digests": {
            "M": {
                "foundation": {"S": "a" * 64},
                "tier_core": {"S": "b" * 64},
                "slot_block": {"S": "d" * 64},
            }
        },
    }


@pytest.mark.covers("ADR-076")
def test_each_tier_reads_only_its_own_pointer_key() -> None:
    """One activation pointer per slot per tier: the card key is the retained POINTER."""
    client = PromptRegistryClient(items={"POINTER#deep": _deep_pointer()})

    deep = _registry(client).active(InsightSlotId.CONTRIBUTION, InsightPromptTier.DEEP)
    card = _registry(client).active(InsightSlotId.CONTRIBUTION, InsightPromptTier.CARD)

    assert deep is not None
    assert deep.version == "v3"
    assert deep.model_route is InsightModelRoute.DEEP
    assert deep.fragment_digest == "c" * 64
    assert card is None


@pytest.mark.covers("ADR-076")
def test_create_version_persists_the_tier_and_composed_fragment_digests() -> None:
    client = PromptRegistryClient(items={"COUNTER": _counter()})

    _registry(client).create_version(
        InsightPromptVersionCreateRequest(
            InsightSlotId.CONTRIBUTION,
            "A reviewed fictional composed prompt.",
            InsightModelRoute.DEEP,
            tier=InsightPromptTier.DEEP,
            fragment_digests=PromptFragmentDigests("a" * 64, "b" * 64, "d" * 64),
            author=OperatorPromptAuthor(_SUBJECT),
            created_at=_CREATED,
        )
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    version_item = cast("dict[str, object]", cast("dict[str, object]", writes[0]["Put"])["Item"])
    assert version_item["tier"] == {"S": "deep"}
    assert version_item["fragment_digests"] == {
        "M": {
            "foundation": {"S": "a" * 64},
            "tier_core": {"S": "b" * 64},
            "slot_block": {"S": "d" * 64},
        }
    }


@pytest.mark.covers("ADR-076")
def test_activation_derives_the_tier_from_the_version_record_and_replicates_the_digest() -> None:
    """An activation can only ever move the pointer of the tier the version belongs to."""
    client = PromptRegistryClient(
        items={
            "VERSION#1": _composed_deep_version(),
            "CALIBRATION#1": _calibration_result(),
        }
    )

    _registry(client).activate(
        InsightPromptActivationRequest(
            InsightSlotId.CONTRIBUTION,
            1,
            None,
            "reviewer-fictional",
            "event-fictional",
            datetime.now(UTC),
        )
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    pointer_put = cast("dict[str, object]", writes[1]["Put"])
    pointer_item = cast("dict[str, dict[str, str]]", pointer_put["Item"])
    expected_combined = PromptFragmentDigests("a" * 64, "b" * 64, "d" * 64).combined
    assert pointer_item["record_key"] == {"S": "POINTER#deep"}
    assert pointer_item["tier"] == {"S": "deep"}
    assert pointer_item["fragment_digest"] == {"S": expected_combined}


@pytest.mark.covers("ADR-076")
def test_retirement_deletes_only_the_named_tiers_pointer() -> None:
    client = PromptRegistryClient()

    _registry(client).retire(
        InsightPromptRetirementRequest(
            InsightSlotId.CONTRIBUTION,
            3,
            "reviewer-fictional",
            "event-fictional",
            datetime.now(UTC),
            tier=InsightPromptTier.DEEP,
        )
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    delete = cast("dict[str, object]", writes[0]["Delete"])
    key = cast("dict[str, dict[str, str]]", delete["Key"])
    assert key["record_key"] == {"S": "POINTER#deep"}


def test_activation_rollback_requires_the_persisted_passed_result_not_version_status() -> None:
    client = PromptRegistryClient(
        items={"VERSION#1": _version(), "CALIBRATION#1": _calibration_result()}
    )

    _registry(client).activate(
        InsightPromptActivationRequest(
            InsightSlotId.CONTRIBUTION,
            1,
            2,
            "reviewer-fictional",
            "event-rollback-fictional",
            datetime(2026, 9, 20, tzinfo=UTC),
        )
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    passed_result = cast("dict[str, object]", writes[0]["ConditionCheck"])
    pointer = cast("dict[str, object]", writes[1]["Put"])
    event_put = cast("dict[str, object]", writes[2]["Put"])
    event = cast("dict[str, dict[str, str]]", event_put["Item"])
    assert cast("dict[str, dict[str, str]]", passed_result["Key"])["record_key"] == {
        "S": "CALIBRATION#1"
    }
    assert passed_result["ConditionExpression"] == "outcome = :passed"
    assert cast("dict[str, dict[str, str]]", pointer["Item"])["model_route"] == {"S": "deep"}
    assert event_put["ConditionExpression"] == (
        "attribute_not_exists(slot_key) AND attribute_not_exists(record_key)"
    )
    assert event["event_action"] == {"S": "activated"}
    assert event["from_version"] == {"N": "2"}
    assert event["to_version"] == {"N": "1"}


def test_retirement_conditionally_clears_the_pointer_and_maps_stale_state_to_conflict() -> None:
    client = PromptRegistryClient(
        transaction_error=ClientError(
            {"Error": {"Code": "TransactionCanceledException", "Message": "stale"}},
            "TransactWriteItems",
        )
    )

    with pytest.raises(InvariantError, match="retirement conflicted"):
        _registry(client).retire(
            InsightPromptRetirementRequest(
                InsightSlotId.CONTRIBUTION,
                2,
                "reviewer-fictional",
                "event-retire-fictional",
                datetime(2026, 9, 20, tzinfo=UTC),
            )
        )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    delete = cast("dict[str, object]", writes[0]["Delete"])
    event = cast("dict[str, dict[str, str]]", cast("dict[str, object]", writes[1]["Put"])["Item"])
    assert delete["ConditionExpression"] == "active_version = :expected"
    assert event["event_action"] == {"S": "retired"}
    assert event["to_version"] == {"NULL": True}


def test_immutable_version_lookup_uses_the_exact_prompt_identity_not_active_pointer() -> None:
    activation = _registry(PromptRegistryClient(items={"VERSION#1": _version()})).version(
        InsightSlotId.CONTRIBUTION, "v1"
    )

    assert activation is not None
    assert activation.version == "v1"
    assert activation.prompt == "A reviewed fictional prompt."


@pytest.mark.parametrize(
    ("items", "reported_codes"),
    [
        pytest.param({}, [], id="absent-version-is-a-typed-miss"),
        pytest.param(
            {"VERSION#1": {**_version(), "version": {"N": "2"}}},
            [PROMPT_REGISTRY_MALFORMED],
            id="malformed-version-is-reported",
        ),
    ],
)
def test_an_unreadable_immutable_version_is_none_and_only_a_malformed_one_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    items: dict[str, dict[str, object]],
    reported_codes: list[object],
) -> None:
    """Both degrade the continuation the same way; a malformed record must also alarm."""
    reported: list[InvariantError] = []

    def capture(error: BaseException, _report: object) -> None:
        assert isinstance(error, InvariantError)
        reported.append(error)

    monkeypatch.setattr(registry_module, "report_error", capture)
    activation = _registry(PromptRegistryClient(items=items)).version(
        InsightSlotId.CONTRIBUTION, "v1"
    )

    assert activation is None
    assert [error.code for error in reported] == reported_codes


def test_activation_without_a_passed_calibration_is_a_typed_refusal() -> None:
    client = PromptRegistryClient(
        items={"VERSION#1": _version(), "CALIBRATION#1": _calibration_result("failed")}
    )

    with pytest.raises(InvariantError) as raised:
        _registry(client).activate(
            InsightPromptActivationRequest(
                InsightSlotId.CONTRIBUTION,
                1,
                2,
                "reviewer-fictional",
                "event-refusal-fictional",
                datetime(2026, 9, 20, tzinfo=UTC),
            )
        )

    assert raised.value.code == PROMPT_ACTIVATION_CALIBRATION_REQUIRED
    assert client.transactions == []


def _claim(now: int = 100) -> InsightPromptCalibrationClaimRequest:
    return InsightPromptCalibrationClaimRequest(
        InsightSlotId.CONTRIBUTION, 1, "message-fictional", now, now + 90
    )


def test_calibration_work_state_distinguishes_available_active_and_terminal() -> None:
    available = _registry(PromptRegistryClient(items={"VERSION#1": _version()}))
    active_version = {
        **_version(),
        "calibration_lease_ref": {"S": "message-fictional"},
        "calibration_lease_expires_at": {"N": "190"},
    }
    active = _registry(PromptRegistryClient(items={"VERSION#1": active_version}))
    terminal = _registry(
        PromptRegistryClient(items={"VERSION#1": {**_version(), "calibration": {"S": "passed"}}})
    )

    assert available.calibration_work_state(InsightSlotId.CONTRIBUTION, 1, 100).should_enqueue
    assert active.calibration_work_state(InsightSlotId.CONTRIBUTION, 1, 100).lease_active
    terminal_state = terminal.calibration_work_state(InsightSlotId.CONTRIBUTION, 1, 100)
    assert terminal_state.calibration == "passed"
    assert not terminal_state.should_enqueue


def test_claim_calibration_conditionally_leases_and_returns_the_stored_prompt() -> None:
    claimed = {
        **_version(),
        "calibration_lease_ref": {"S": "message-fictional"},
        "calibration_lease_expires_at": {"N": "190"},
    }
    client = PromptRegistryClient(update_attributes=claimed)

    result = _registry(client).claim_calibration(_claim())

    assert result.disposition is InsightCalibrationClaimDisposition.CLAIMED
    assert result.pending is not None
    assert result.pending.prompt == "A reviewed fictional prompt."
    update = client.updates[0]
    assert update["ConditionExpression"] == (
        "#record_type = :record_type AND #slot_id = :slot_id "
        "AND #version = :version AND #calibration = :pending "
        "AND (attribute_not_exists(#lease) OR #lease_expires <= :now)"
    )
    assert update["ReturnValues"] == "ALL_NEW"


@pytest.mark.parametrize(
    ("calibration", "lease_expires", "expected"),
    [
        ("pending", 190, InsightCalibrationClaimDisposition.ACTIVE_LEASE),
        ("passed", None, InsightCalibrationClaimDisposition.TERMINAL),
    ],
)
def test_claim_calibration_maps_a_failed_condition_to_current_state(
    calibration: str,
    lease_expires: int | None,
    expected: InsightCalibrationClaimDisposition,
) -> None:
    version = {**_version(), "calibration": {"S": calibration}}
    if lease_expires is not None:
        version.update(
            {
                "calibration_lease_ref": {"S": "other-message"},
                "calibration_lease_expires_at": {"N": str(lease_expires)},
            }
        )
    client = PromptRegistryClient(
        items={"VERSION#1": version},
        update_error=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "race"}},
            "UpdateItem",
        ),
    )

    result = _registry(client).claim_calibration(_claim())

    assert result.disposition is expected
    assert result.pending is None


def test_claimed_completion_requires_the_exact_lease_and_clears_it_atomically() -> None:
    client = PromptRegistryClient()

    _registry(client).complete_claimed_calibration(
        InsightClaimedPromptCalibrationRequest(
            InsightSlotId.CONTRIBUTION,
            1,
            "message-fictional",
            150,
            _passed_result(),
        )
    )

    writes = cast("list[dict[str, object]]", client.transactions[0]["TransactItems"])
    update = cast("dict[str, object]", writes[0]["Update"])
    assert update["UpdateExpression"] == (
        "SET #calibration = :outcome REMOVE #lease, #lease_expires"
    )
    assert "#lease = :lease AND #lease_expires > :completed_at" in cast(
        "str", update["ConditionExpression"]
    )
    values = cast("dict[str, object]", update["ExpressionAttributeValues"])
    assert values[":lease"] == {"S": "message-fictional"}
    assert values[":completed_at"] == {"N": "150"}
    assert len(writes) == 9


def test_pending_outbox_reads_only_content_free_version_identities() -> None:
    client = PromptRegistryClient(
        items={
            "CALIBRATION_OUTBOX#2": {
                "record_type": {"S": "PROMPT_CALIBRATION_OUTBOX"},
                "slot_id": {"S": "contribution"},
                "version": {"N": "2"},
            }
        }
    )

    work = _registry(client).pending_calibration_outbox((InsightSlotId.CONTRIBUTION,))

    assert work[0].slot_id is InsightSlotId.CONTRIBUTION
    assert work[0].version == 2


def test_outbox_acknowledgement_conditionally_deletes_the_exact_identity() -> None:
    client = PromptRegistryClient()

    _registry(client).acknowledge_calibration_outbox(InsightSlotId.CONTRIBUTION, 2)

    deletion = client.deletes[0]
    assert deletion["ConditionExpression"] == (
        "#record_type = :record_type AND #slot_id = :slot_id AND #version = :version"
    )
    assert deletion["Key"] == {
        "slot_key": {"S": "SLOT#contribution"},
        "record_key": {"S": "CALIBRATION_OUTBOX#2"},
    }


def test_expired_or_superseded_worker_cannot_complete_calibration() -> None:
    client = PromptRegistryClient(
        transaction_error=ClientError(
            {"Error": {"Code": "TransactionCanceledException", "Message": "lost lease"}},
            "TransactWriteItems",
        )
    )

    with pytest.raises(InvariantError, match="lease was lost"):
        _registry(client).complete_claimed_calibration(
            InsightClaimedPromptCalibrationRequest(
                InsightSlotId.CONTRIBUTION,
                1,
                "stale-attempt",
                191,
                _passed_result(),
            )
        )


def _written_version(client: PromptRegistryClient) -> dict[str, object]:
    """Return the version row the last transaction wrote, as the double's Put carried it."""
    writes = cast("list[dict[str, object]]", client.transactions[-1]["TransactItems"])
    item = cast("dict[str, object]", writes[0]["Put"])["Item"]
    return cast("dict[str, object]", item)


def _persisted(client: PromptRegistryClient) -> dict[str, dict[str, object]]:
    """Every row the last transaction put, keyed by record key, as a table would hold them."""
    writes = cast("list[dict[str, object]]", client.transactions[-1]["TransactItems"])
    items = [cast("dict[str, object]", write["Put"])["Item"] for write in writes if "Put" in write]
    rows = [cast("dict[str, dict[str, str]]", item) for item in items]
    return {row["record_key"]["S"]: cast("dict[str, object]", row) for row in rows}


def _create(client: PromptRegistryClient) -> None:
    _registry(client).create_version(
        InsightPromptVersionCreateRequest(
            InsightSlotId.CONTRIBUTION,
            "A reviewed fictional prompt.",
            InsightModelRoute.QUICK,
            author=OperatorPromptAuthor(_SUBJECT),
            created_at=_CREATED,
        )
    )


def _seed(client: PromptRegistryClient) -> None:
    _registry(client).seed_v1(
        InsightPromptSeedRequest(
            InsightSlotId.CONTRIBUTION, "A reviewed fictional seed prompt.", _SEEDED
        )
    )


def test_a_created_version_records_its_operator_subject_and_creation_time() -> None:
    client = PromptRegistryClient(items={"COUNTER": _counter()})

    _create(client)

    item = _written_version(client)
    assert item["author_kind"] == {"S": "operator"}
    assert item["operator"] == {"S": _SUBJECT}
    assert item["created_at"] == {"S": "2026-10-01T15:25:00+00:00"}
    assert "system_author" not in item


def test_a_seeded_v1_records_the_deployment_seed_and_the_run_time() -> None:
    client = PromptRegistryClient()

    _seed(client)

    item = _written_version(client)
    assert item["author_kind"] == {"S": "system"}
    assert item["system_author"] == {"S": "contract-seed"}
    assert item["created_at"] == {"S": "2026-09-30T08:00:00+00:00"}
    assert "operator" not in item


def test_a_movers_seed_writes_nothing() -> None:
    client = PromptRegistryClient()

    _registry(client).seed_v1(
        InsightPromptSeedRequest(InsightSlotId.MOVERS, "Fictional movers prompt.", _SEEDED)
    )

    assert client.transactions == []


def test_authorship_round_trips_through_the_registry() -> None:
    """The RCA guard: what create and seed write is what the versions read returns."""
    client = PromptRegistryClient(items={"COUNTER": _counter()})
    _seed(client)
    seeded = _persisted(client)
    _create(client)
    created = _persisted(client)
    client.items = {**seeded, **created}

    newest, first = _registry(client).versions(InsightSlotId.CONTRIBUTION)

    assert (newest.author, newest.created_at) == (OperatorPromptAuthor(_SUBJECT), _CREATED)
    assert (first.author, first.created_at) == (SystemPromptAuthor.CONTRACT_SEED, _SEEDED)
    assert "not-recorded" not in repr((newest, first))


def test_a_legacy_version_reads_back_with_no_author_and_no_time() -> None:
    client = PromptRegistryClient(items={"VERSION#1": _version()})

    [record] = _registry(client).versions(InsightSlotId.CONTRIBUTION)

    assert (record.author, record.created_at) == (None, None)


@pytest.mark.parametrize(
    "authorship",
    [
        {"author_kind": {"S": "operator"}, "operator": {"S": _SUBJECT}},
        {"author_kind": {"S": "operator"}, "created_at": {"S": "2026-10-01T15:25:00+00:00"}},
        {"operator": {"S": _SUBJECT}, "created_at": {"S": "2026-10-01T15:25:00+00:00"}},
        {"created_at": {"S": "2026-10-01T15:25:00+00:00"}},
        {
            "author_kind": {"S": "operator"},
            "operator": {"S": _SUBJECT},
            "system_author": {"S": "contract-seed"},
            "created_at": {"S": "2026-10-01T15:25:00+00:00"},
        },
        {
            "author_kind": {"S": "system"},
            "system_author": {"S": "fictional-unknown-seed"},
            "created_at": {"S": "2026-10-01T15:25:00+00:00"},
        },
        {
            "author_kind": {"S": "fictional-kind"},
            "operator": {"S": _SUBJECT},
            "created_at": {"S": "2026-10-01T15:25:00+00:00"},
        },
        {
            "author_kind": {"S": "operator"},
            "operator": {"S": _SUBJECT},
            "created_at": {"S": "not-a-time"},
        },
        {
            "author_kind": {"S": "operator"},
            "operator": {"S": _SUBJECT},
            "created_at": {"S": "2026-10-01T15:25:00"},
        },
    ],
    ids=[
        "no-time",
        "no-subject",
        "no-kind",
        "time-only",
        "both-authors",
        "unknown-system",
        "unknown-kind",
        "bad-time",
        "naive-time",
    ],
)
def test_a_partial_or_unknown_authorship_row_is_malformed(authorship: dict[str, object]) -> None:
    client = PromptRegistryClient(items={"VERSION#1": {**_version(), **authorship}})

    with pytest.raises(InvariantError) as caught:
        _registry(client).versions(InsightSlotId.CONTRIBUTION)

    assert caught.value.code == PROMPT_REGISTRY_MALFORMED
