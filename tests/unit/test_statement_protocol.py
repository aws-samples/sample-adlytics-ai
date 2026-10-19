# SPDX-License-Identifier: MIT-0
"""ADR-075 §5/§6 statement protocol: deadlines, the step-2 validator, and consolidation."""

from __future__ import annotations

import pytest

from domain.operations.statement_protocol import (
    AcceptedStatementStart,
    CanonicalObjectCandidate,
    InsufficientStatementBudget,
    LocalStatementRefusal,
    StatementDeadlines,
    StatementKind,
    StatementProtocolError,
    StatementRefusalReason,
    StatementStartBounds,
    ValidateStatementStartRequest,
    consolidate_canonical_objects,
    derive_statement_deadlines,
    unit_statement_kinds,
    validate_statement_start,
)
from domain.operations.table_attempt import (
    AttemptIdentityRequest,
    AttemptOwnerKind,
    attempt_identity,
    attempt_reservation_identity,
)
from domain.operations.write_plan import CanonicalObjectEntry

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
TABLE = "adlytics_dev.fact_semantic_rows"
HOLDER = "ab01" * 16
PLAN_KEY = "WRITE_PLAN#" + "b" * 64 + "#" + "c" * 64
ENVELOPE = '{"Database":"db_fixture","QueryString":"SELECT 1","WorkGroup":"wg-fixture"}'
CHECKSUM = "d" * 64


def _request(**overrides: object) -> ValidateStatementStartRequest:
    base: dict[str, object] = {
        "statement_kind": "merge",
        "owner_kind": "queued_unit",
        "owner_ref": PLAN_KEY,
        "physical_table_id": TABLE,
        "lease_holder": HOLDER,
        "fencing_token": 7,
        "ordinal": 3,
        "start_envelope": ENVELOPE,
        "rendered_sql": "MERGE INTO fixture",
        "parameters": ("alpha", "beta"),
    }
    base.update(overrides)
    return ValidateStatementStartRequest(**base)  # type: ignore[arg-type]


# --- §5 step 1: deadlines -------------------------------------------------------------------------


def test_a_full_900000_ms_budget_derives_q_s_i_at_780000_840000_900000_from_m0() -> None:
    derived = derive_statement_deadlines(5_000, 900_000)
    assert isinstance(derived, StatementDeadlines)
    assert derived.cancellation_deadline_ms == 5_000 + 780_000
    assert derived.safety_deadline_ms == 5_000 + 840_000
    assert derived.invocation_deadline_ms == 5_000 + 900_000


def test_a_budget_of_exactly_120000_ms_starts_no_attempt_and_is_retryable() -> None:
    refusal = derive_statement_deadlines(5_000, 120_000)
    assert isinstance(refusal, InsufficientStatementBudget)
    assert refusal.remaining_budget_ms == 120_000
    assert refusal.retryable is True


def test_a_budget_of_120001_ms_accepts_with_the_cancellation_deadline_at_m0_plus_one() -> None:
    derived = derive_statement_deadlines(5_000, 120_001)
    assert isinstance(derived, StatementDeadlines)
    assert derived.cancellation_deadline_ms == 5_001
    assert derived.safety_deadline_ms == 5_000 + 60_001
    assert derived.invocation_deadline_ms == 5_000 + 120_001


def test_a_boolean_or_negative_budget_is_rejected_before_any_derivation() -> None:
    with pytest.raises(StatementProtocolError):
        derive_statement_deadlines(5_000, True)
    with pytest.raises(StatementProtocolError):
        derive_statement_deadlines(5_000, -1)
    with pytest.raises(StatementProtocolError):
        derive_statement_deadlines(True, 900_000)


# --- §5 step 2: the closed local typed validator --------------------------------------------------


def test_an_accepted_start_derives_the_pinned_identity_and_reservation() -> None:
    accepted = validate_statement_start(_request())
    assert isinstance(accepted, AcceptedStatementStart)
    expected = attempt_identity(
        AttemptIdentityRequest(
            owner_kind=AttemptOwnerKind.QUEUED_UNIT,
            owner_ref=PLAN_KEY,
            physical_table_id=TABLE,
            lease_holder=HOLDER,
            fencing_token=7,
            ordinal=3,
            start_envelope=ENVELOPE,
        )
    )
    assert accepted.identity == expected
    assert accepted.identity.client_request_token == accepted.identity.attempt_id
    assert accepted.reservation_identity == attempt_reservation_identity(expected.attempt_id)
    assert accepted.statement_kind is StatementKind.MERGE


def test_a_supplied_token_byte_equal_to_the_derived_attempt_id_is_accepted() -> None:
    derived = validate_statement_start(_request())
    assert isinstance(derived, AcceptedStatementStart)
    accepted = validate_statement_start(_request(client_request_token=derived.identity.attempt_id))
    assert isinstance(accepted, AcceptedStatementStart)


def test_a_supplied_token_that_differs_from_the_derived_attempt_id_refuses() -> None:
    refusal = validate_statement_start(_request(client_request_token="e" * 64))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.CLIENT_REQUEST_TOKEN


def test_a_statement_kind_outside_merge_or_verification_refuses() -> None:
    refusal = validate_statement_start(_request(statement_kind="insert"))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.STATEMENT_KIND


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_kind": "operator"},
        {"owner_ref": "   "},
        {"physical_table_id": "no-database-segment"},
        {"lease_holder": "not-a-digest"},
        {"lease_holder": HOLDER.upper()},
        {"fencing_token": 0},
        {"fencing_token": True},
        {"ordinal": 0},
    ],
)
def test_every_malformed_owner_lease_tuple_member_refuses(overrides: dict[str, object]) -> None:
    refusal = validate_statement_start(_request(**overrides))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.OWNER_LEASE_TUPLE


def test_a_blank_start_envelope_refuses() -> None:
    refusal = validate_statement_start(_request(start_envelope="   "))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.START_ENVELOPE


def test_an_envelope_at_exactly_200000_utf8_bytes_is_accepted_and_200001_refuses() -> None:
    """The §11 bound is measured on the canonical start envelope: SQL plus every parameter."""
    at_bound = validate_statement_start(_request(start_envelope="x" * 200_000))
    assert isinstance(at_bound, AcceptedStatementStart)
    beyond = validate_statement_start(_request(start_envelope="x" * 200_001))
    assert isinstance(beyond, LocalStatementRefusal)
    assert beyond.reason is StatementRefusalReason.START_ENVELOPE
    assert "200001 UTF-8 bytes" in beyond.description


def test_the_envelope_bound_counts_utf8_bytes_not_characters() -> None:
    # "é" is two UTF-8 bytes, so 100001 of them exceed the bound in half the characters.
    refusal = validate_statement_start(_request(start_envelope="é" * 100_001))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.START_ENVELOPE
    accepted = validate_statement_start(_request(start_envelope="é" * 100_000))
    assert isinstance(accepted, AcceptedStatementStart)


def test_rendered_sql_is_not_measured_apart_from_its_envelope() -> None:
    """Large SQL inside a within-bound envelope is the envelope's measure, not a second one."""
    accepted = validate_statement_start(_request(rendered_sql="x" * 200_001))
    assert isinstance(accepted, AcceptedStatementStart)


def test_blank_rendered_sql_refuses() -> None:
    refusal = validate_statement_start(_request(rendered_sql="  "))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.RENDERED_SQL


def test_there_is_no_parameter_count_bound_only_the_envelope_byte_bound() -> None:
    """The 900-parameter cap split one unit into ~3 Iceberg commits (§11, 2026-10-10)."""
    accepted = validate_statement_start(_request(parameters=("p",) * 5_000))
    assert isinstance(accepted, AcceptedStatementStart)


def test_a_non_text_parameter_refuses_whatever_its_value() -> None:
    refusal = validate_statement_start(_request(parameters=("p", 7)))
    assert isinstance(refusal, LocalStatementRefusal)
    assert refusal.reason is StatementRefusalReason.PARAMETERS


def test_bounds_members_must_be_positive_plain_integers() -> None:
    with pytest.raises(StatementProtocolError):
        StatementStartBounds(max_statement_bytes=0)
    with pytest.raises(StatementProtocolError):
        StatementStartBounds(max_statement_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StatementStartBounds(max_parameters_per_statement=900)  # type: ignore[call-arg]


def test_a_refusal_starts_no_attempt_because_it_returns_before_identity_derivation() -> None:
    """A refused start must reserve nothing; the refusal value carries no identity at all."""
    refusal = validate_statement_start(_request(statement_kind="insert"))
    assert isinstance(refusal, LocalStatementRefusal)
    assert not hasattr(refusal, "identity")


# --- §6: exact-object deduplication and canonical order -------------------------------------------


def _entry(kind: str, reference: str, checksum: str = CHECKSUM) -> CanonicalObjectEntry:
    return CanonicalObjectEntry(
        kind=kind,
        reference=reference,
        content_checksum=checksum,
        publication_id="pub-fixture-0001",
        partition_key=None,
        fencing_token=7,
    )


def test_byte_identical_duplicate_objects_deduplicate_to_one_entry() -> None:
    candidate = CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"authority-bytes")
    twin = CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"authority-bytes")
    assert consolidate_canonical_objects((candidate, twin)) == (candidate.entry,)


def test_same_key_objects_with_different_checksums_reject() -> None:
    first = CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"authority-bytes")
    second = CanonicalObjectCandidate(
        _entry("canonical", "obj-a", checksum="e" * 64), b"authority-bytes"
    )
    with pytest.raises(StatementProtocolError):
        consolidate_canonical_objects((first, second))


def test_same_key_objects_with_different_encoded_authority_bytes_reject() -> None:
    first = CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"authority-bytes")
    second = CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"other-bytes")
    with pytest.raises(StatementProtocolError):
        consolidate_canonical_objects((first, second))


def test_canonical_order_is_unsigned_utf8_bytes_of_kind_then_reference() -> None:
    """Caller order never participates; the shorter byte string sorts before a longer prefix."""
    entries = (
        CanonicalObjectCandidate(_entry("source", "obj-a"), b"s-a"),
        CanonicalObjectCandidate(_entry("canonical", "ab0"), b"c-ab0"),
        CanonicalObjectCandidate(_entry("canonical", "ab"), b"c-ab"),
    )
    ordered = consolidate_canonical_objects(entries)
    assert [(entry.kind, entry.reference) for entry in ordered] == [
        ("canonical", "ab"),
        ("canonical", "ab0"),
        ("source", "obj-a"),
    ]


def test_an_empty_authority_byte_string_is_rejected_at_construction() -> None:
    with pytest.raises(StatementProtocolError):
        CanonicalObjectCandidate(_entry("canonical", "obj-a"), b"")


# --- §6/§2.1: the clean statement sequence --------------------------------------------------------


def test_the_clean_sequence_is_exactly_m_merges_followed_by_one_verification() -> None:
    assert unit_statement_kinds(3) == (
        StatementKind.MERGE,
        StatementKind.MERGE,
        StatementKind.MERGE,
        StatementKind.VERIFICATION,
    )


def test_nine_merges_plus_one_verification_fill_the_ten_attempt_unit_cap() -> None:
    kinds = unit_statement_kinds(9)
    assert len(kinds) == 10
    assert kinds[-1] is StatementKind.VERIFICATION


def test_zero_or_ten_merges_are_outside_the_clean_unit_sequence() -> None:
    with pytest.raises(StatementProtocolError):
        unit_statement_kinds(0)
    with pytest.raises(StatementProtocolError):
        unit_statement_kinds(10)
