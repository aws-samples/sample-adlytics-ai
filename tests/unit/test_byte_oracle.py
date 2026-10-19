# SPDX-License-Identifier: MIT-0
"""ADR-075 §3.2 shared DynamoDB byte oracle: calibration vectors, envelope, fixtures.

The reference checker in ``byte_oracle_support`` recomputes every frozen fixture
expectation with only the standard library; these tests additionally require the
production builder and ``Stage1DynamoByteOracle`` to match the same frozen maps.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any, cast

import fixed_member_byte_fixture_support as fixed_member_support
import pytest
from boto3.dynamodb.types import TypeSerializer
from byte_oracle_support import (
    FixtureCheckError,
    FixtureTreeError,
    check_fixed_member_fixture,
    check_fixture,
    load_fixture_tree,
)
from dynamodb_reserved_words import guard_reserved_words

from adapters.outbound.dynamodb.byte_oracle import (
    ACTION_COUNT_EXCEEDED,
    AGGREGATE_BYTES_EXCEEDED,
    DUPLICATE_TARGET,
    SERVICE_ITEM_BYTES_EXCEEDED,
    UNSUPPORTED_VALUE,
    ConditionCheckAction,
    DeleteAction,
    ExactPreState,
    PutAction,
    Stage1DynamoByteOracle,
    TransactionEnvelopeRequest,
    TransactionEnvelopeResult,
    UpdateAction,
    calibration_item_v1,
    calibration_transaction_v1,
    canonical_wire_text,
)
from adapters.outbound.dynamodb.table_attempt import DynamoTableAttemptStore
from domain.operations.table_attempt import ProviderErrorClass, SanitizedStartEvidence
from domain.shared_kernel.errors import ValidationError
from ports.outbound.table_attempt import (
    RecordSubmittedAttemptRequest,
    RecordSubmittedAttemptResult,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

ORACLE = Stage1DynamoByteOracle()
OPERATION = "RecordSubmittedAttemptRequest"


def _plain(value: dict[str, Any]) -> Any:
    """Convert one frozen low-level attribute value back to its plain form."""
    tag, member = next(iter(value.items()))
    if tag == "S":
        return member
    if tag == "N":
        return int(member)
    if tag == "BOOL":
        return member
    if tag == "NULL":
        return None
    if tag == "M":
        return {name: _plain(child) for name, child in member.items()}
    if tag == "L":
        return [_plain(child) for child in member]
    raise AssertionError(f"fixture uses an unexpected tag {tag!r}.")


def _plain_item(low_level: dict[str, Any]) -> dict[str, Any]:
    return {name: _plain(value) for name, value in low_level.items()}


def _sized_put(ordinal: int, conservative_bytes: int) -> PutAction:
    """Build a put whose item measures exactly the requested conservative bytes."""
    key = {"partition_key": f"P#{ordinal:03d}", "record_key": "R"}
    padding = conservative_bytes - ORACLE.conservative_service_item_bytes({**key, "payload": ""})
    item = {**key, "payload": "x" * padding}
    assert ORACLE.conservative_service_item_bytes(item) == conservative_bytes
    return PutAction(table_name="T", key=key, item=item, pre_state=ExactPreState(None))


def _tiny_put(ordinal: int, table_name: str = "T") -> PutAction:
    key = {"partition_key": f"P#{ordinal:03d}", "record_key": "R"}
    return PutAction(table_name=table_name, key=key, item=dict(key), pre_state=ExactPreState(None))


# --- Calibration vectors (wire fixtures only; never service-size evidence) ------------------


def test_calibration_item_v1_reproduces_the_pinned_wire_totals() -> None:
    assert ORACLE.wire_json_bytes(calibration_item_v1(399913)) == 400000
    assert ORACLE.wire_json_bytes(calibration_item_v1(399914)) == 400001


def test_calibration_transaction_v1_reproduces_the_pinned_wire_totals() -> None:
    assert ORACLE.wire_json_bytes(calibration_transaction_v1(366632)) == 4000000
    assert ORACLE.wire_json_bytes(calibration_transaction_v1(366633)) == 4000001


def test_canonical_wire_text_is_identical_for_both_map_insertion_orders() -> None:
    forward = canonical_wire_text({"a": {"S": "1"}, "b": {"N": "2"}})
    reversed_order = canonical_wire_text({"b": {"N": "2"}, "a": {"S": "1"}})
    assert forward == reversed_order
    assert len(forward.encode("utf-8")) == ORACLE.wire_json_bytes(
        {"b": {"N": "2"}, "a": {"S": "1"}}
    )


# --- Action counting ------------------------------------------------------------------------


def test_action_count_counts_each_distinct_item_action_once() -> None:
    request = TransactionEnvelopeRequest(actions=tuple(_tiny_put(n) for n in range(3)))
    assert ORACLE.action_count(request) == 3


def test_two_actions_targeting_one_item_refuse_even_with_reordered_keys() -> None:
    first = _tiny_put(0)
    key_reordered = {"record_key": "R", "partition_key": "P#000"}
    second = DeleteAction(table_name="T", key=key_reordered, pre_state=ExactPreState(None))
    with pytest.raises(ValidationError) as refusal:
        ORACLE.action_count(TransactionEnvelopeRequest(actions=(first, second)))
    assert refusal.value.code == DUPLICATE_TARGET


def test_the_same_key_in_two_tables_is_two_distinct_targets() -> None:
    request = TransactionEnvelopeRequest(actions=(_tiny_put(0, "T"), _tiny_put(0, "U")))
    assert ORACLE.action_count(request) == 2


# --- Envelope acceptance and refusal at each boundary -----------------------------------------


def test_a_result_at_exactly_400000_conservative_bytes_is_accepted() -> None:
    result = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(_sized_put(0, 400000),)))
    assert result.action_count == 1
    assert result.targets[0].result_bytes == 400000
    assert result.aggregate_upper_bound == 400000


def test_a_result_one_beyond_400000_conservative_bytes_refuses() -> None:
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(_sized_put(0, 400001),)))
    assert refusal.value.code == SERVICE_ITEM_BYTES_EXCEEDED


def test_a_pre_state_one_beyond_400000_refuses_even_when_deleted() -> None:
    """A deleted item is not size-free: its exact pre-state still counts (§3.2)."""
    oversized = _sized_put(0, 400001)
    action = DeleteAction(
        table_name="T", key=dict(oversized.key), pre_state=ExactPreState(oversized.item)
    )
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(action,)))
    assert refusal.value.code == SERVICE_ITEM_BYTES_EXCEEDED


def test_a_delete_result_is_absence_with_exact_size_zero() -> None:
    boundary = _sized_put(0, 400000)
    action = DeleteAction(
        table_name="T", key=dict(boundary.key), pre_state=ExactPreState(boundary.item)
    )
    result = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(action,)))
    assert result.targets[0].pre_state_bytes == 400000
    assert result.targets[0].result_bytes == 0
    assert result.targets[0].target_upper_bound == 400000


def test_a_condition_check_result_is_the_unchanged_pre_state() -> None:
    boundary = _sized_put(0, 400000)
    action = ConditionCheckAction(
        table_name="T", key=dict(boundary.key), pre_state=ExactPreState(boundary.item)
    )
    result = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(action,)))
    assert result.targets[0].pre_state_bytes == result.targets[0].result_bytes == 400000


def test_an_aggregate_at_exactly_4000000_summed_maxima_is_accepted() -> None:
    actions = tuple(_sized_put(n, 400000) for n in range(10))
    result = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=actions))
    assert result.action_count == 10
    assert result.aggregate_upper_bound == 4000000


def test_an_aggregate_one_beyond_4000000_refuses_with_no_split_fallback() -> None:
    actions = (*tuple(_sized_put(n, 400000) for n in range(10)), _tiny_put(10))
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=actions))
    assert refusal.value.code == AGGREGATE_BYTES_EXCEEDED


def test_100_distinct_item_actions_are_accepted_and_101_refuse() -> None:
    hundred = tuple(_tiny_put(n) for n in range(100))
    assert ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=hundred)).action_count == 100
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(*hundred, _tiny_put(100))))
    assert refusal.value.code == ACTION_COUNT_EXCEEDED


def test_an_update_result_is_the_expression_applied_pre_state() -> None:
    key = {"partition_key": "P#000", "record_key": "R"}
    pre_state = {**key, "state": "intent", "payload": "x" * 10}
    action = UpdateAction(
        table_name="T",
        key=key,
        set_attributes={"state": "submitted", "note": "y" * 5},
        pre_state=ExactPreState(pre_state),
    )
    result = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(action,)))
    merged = {**pre_state, "state": "submitted", "note": "y" * 5}
    assert result.targets[0].result_bytes == ORACLE.conservative_service_item_bytes(merged)
    assert result.targets[0].pre_state_bytes == ORACLE.conservative_service_item_bytes(pre_state)


# --- Wire bytes and conservative bytes stay separate quantities -------------------------------


def test_wire_bytes_and_conservative_bytes_are_separate_quantities() -> None:
    """Keep the two quantities apart, as §3.2 requires.

    The 4000000-wire-byte transaction-v1 content is conservatively well inside
    the envelope: complete request wire bytes are never the service aggregate.
    """
    serializer = TypeSerializer()
    actions: list[PutAction] = []
    request_map = calibration_transaction_v1(366632)
    for member in cast("list[dict[str, Any]]", request_map["TransactItems"]):
        item = _plain_item(member["Put"]["Item"])
        key = {"partition_key": item["partition_key"], "record_key": item["record_key"]}
        actions.append(PutAction(table_name="T", key=key, item=item, pre_state=ExactPreState(None)))
        assert {name: serializer.serialize(value) for name, value in item.items()} == member["Put"][
            "Item"
        ]
    assert ORACLE.wire_json_bytes(request_map) == 4000000
    envelope = ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=tuple(actions)))
    assert all(target.target_upper_bound < 400000 for target in envelope.targets)
    assert envelope.aggregate_upper_bound < 4000000
    assert not hasattr(TransactionEnvelopeResult, "wire_json_bytes")


# --- Unsupported and indeterminate values refuse before mutation ------------------------------


@pytest.mark.parametrize(
    "value",
    [1.5, Decimal("NaN"), Decimal("Infinity"), set[str](), object()],
    ids=["float", "nan", "infinity", "empty-set", "unclassifiable"],
)
def test_a_value_the_serializer_cannot_classify_refuses(value: object) -> None:
    with pytest.raises(ValidationError) as refusal:
        ORACLE.conservative_service_item_bytes({"partition_key": "P", "payload": value})
    assert refusal.value.code == UNSUPPORTED_VALUE


def test_a_floating_point_wire_value_is_indeterminate_and_refuses() -> None:
    with pytest.raises(ValidationError) as refusal:
        ORACLE.wire_json_bytes({"N": 1.5})
    assert refusal.value.code == UNSUPPORTED_VALUE


# --- Typed action shape ------------------------------------------------------------------------


def test_a_put_item_must_carry_its_key_unchanged() -> None:
    with pytest.raises(ValueError, match="key unchanged"):
        PutAction(
            table_name="T",
            key={"partition_key": "P"},
            item={"partition_key": "Q"},
            pre_state=ExactPreState(None),
        )


def test_a_pre_state_must_carry_the_action_key_unchanged() -> None:
    with pytest.raises(ValueError, match="key unchanged"):
        DeleteAction(
            table_name="T",
            key={"partition_key": "P"},
            pre_state=ExactPreState({"partition_key": "Q"}),
        )


def test_an_update_may_never_rewrite_a_key_attribute() -> None:
    with pytest.raises(ValueError, match="key attribute"):
        UpdateAction(
            table_name="T",
            key={"partition_key": "P"},
            set_attributes={"partition_key": "Q"},
            pre_state=ExactPreState({"partition_key": "P"}),
        )


def test_an_envelope_requires_at_least_one_action() -> None:
    with pytest.raises(ValueError, match="at least one action"):
        TransactionEnvelopeRequest(actions=())


# --- Committed RecordSubmittedAttemptRequest fixtures ------------------------------------------


@pytest.fixture(scope="module")
def fixture_tree() -> dict[str, dict[str, dict[str, Any]]]:
    return load_fixture_tree()


def test_every_committed_fixture_passes_the_reference_checker(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    assert OPERATION in fixture_tree
    for branches in fixture_tree.values():
        for fixture in branches.values():
            check_fixture(fixture)


def _typed_request(fixture: dict[str, Any]) -> RecordSubmittedAttemptRequest:
    typed_input = fixture["typed_input"]
    evidence = typed_input["sanitized_start_evidence"]
    return RecordSubmittedAttemptRequest(
        physical_table_id=typed_input["physical_table_id"],
        attempt_id=typed_input["attempt_id"],
        envelope_digest=typed_input["envelope_digest"],
        client_request_token=typed_input["client_request_token"],
        query_execution_id=typed_input["query_execution_id"],
        submitted_at=typed_input["submitted_at"],
        sanitized_start_evidence=SanitizedStartEvidence(
            provider_status_code=evidence["provider_status_code"],
            provider_error_class=ProviderErrorClass(evidence["provider_error_class"]),
        ),
    )


def _update_action(fixture: dict[str, Any]) -> UpdateAction:
    target = fixture["targets"][0]
    pre_state = _plain_item(target["expected_pre_state_map"])
    result = _plain_item(target["expected_result_map"])
    key = {name: pre_state[name] for name in ("partition_key", "record_key")}
    set_attributes = {
        name: result[name]
        for name in ("state", "query_execution_id", "submitted_at", "sanitized_start_evidence")
    }
    return UpdateAction(
        table_name=fixture["table_name"],
        key=key,
        set_attributes=set_attributes,
        pre_state=ExactPreState(pre_state),
    )


def test_the_maximum_fixture_matches_the_production_builder_and_the_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[OPERATION]["maximum"]
    target = fixture["targets"][0]

    captured: dict[str, Any] = {}

    @guard_reserved_words
    class _Recording:
        def update_item(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"Attributes": target["expected_pre_state_map"]}

    store = DynamoTableAttemptStore(fixture["table_name"], cast("Any", _Recording()))
    outcome = store.record_submitted_attempt(_typed_request(fixture))
    assert outcome is RecordSubmittedAttemptResult.RECORDED
    assert captured == fixture["expected_request"]
    assert ORACLE.wire_json_bytes(captured) == fixture["expected"]["wire_json_bytes"]

    serializer = TypeSerializer()
    pre_state = _plain_item(target["expected_pre_state_map"])
    result = _plain_item(target["expected_result_map"])
    assert {name: serializer.serialize(value) for name, value in result.items()} == target[
        "expected_result_map"
    ]
    assert (
        ORACLE.conservative_service_item_bytes(pre_state) == target["pre_state_conservative_bytes"]
    )
    assert ORACLE.conservative_service_item_bytes(result) == 400000

    envelope = ORACLE.validate_envelope(
        TransactionEnvelopeRequest(actions=(_update_action(fixture),))
    )
    assert envelope.action_count == fixture["expected"]["action_count"]
    assert envelope.targets[0].pre_state_bytes == target["pre_state_conservative_bytes"]
    assert envelope.targets[0].result_bytes == target["result_conservative_bytes"]
    assert envelope.aggregate_upper_bound == fixture["expected"]["aggregate_upper_bound"]


def test_the_one_beyond_fixture_refuses_in_the_oracle(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[OPERATION]["one-beyond"]
    assert fixture["expected"]["outcome"] == "refused"
    assert fixture["targets"][0]["result_conservative_bytes"] == 400001
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(actions=(_update_action(fixture),)))
    assert refusal.value.code == SERVICE_ITEM_BYTES_EXCEEDED


# --- Common-mode omission --------------------------------------------------------------------


def _altered(value: Any) -> Any:
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, dict):
        return {**cast("dict[str, Any]", value), "altered_member": {"S": "x"}}
    return str(value) + "x"


def test_omitting_or_altering_every_request_member_fails_the_checker(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[OPERATION]["maximum"]
    for member in fixture["expected_request"]:
        omitted = copy.deepcopy(fixture)
        del omitted["expected_request"][member]
        with pytest.raises(FixtureCheckError):
            check_fixture(omitted)
        altered = copy.deepcopy(fixture)
        altered["expected_request"][member] = _altered(altered["expected_request"][member])
        with pytest.raises(FixtureCheckError):
            check_fixture(altered)


def test_omitting_every_pre_state_and_result_member_fails_the_checker(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[OPERATION]["maximum"]
    for map_name in ("expected_pre_state_map", "expected_result_map"):
        for member in fixture["targets"][0][map_name]:
            omitted = copy.deepcopy(fixture)
            del omitted["targets"][0][map_name][member]
            with pytest.raises(FixtureCheckError):
                check_fixture(omitted)


def test_a_fixture_version_change_without_review_fails_the_checker(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = copy.deepcopy(fixture_tree[OPERATION]["maximum"])
    fixture["fixture_version"] = "v2"
    with pytest.raises(FixtureCheckError):
        check_fixture(fixture)


def test_a_fixture_set_missing_a_required_branch_fails_the_loader(tmp_path: Any) -> None:
    operation_dir = tmp_path / "SomeOperationRequest"
    operation_dir.mkdir()
    stub = {
        "fixture_version": "v1",
        "typed_operation": "SomeOperationRequest",
        "branch": "maximum",
    }
    (operation_dir / "maximum-v1.json").write_text(json.dumps(stub), encoding="utf-8")
    with pytest.raises(FixtureTreeError, match="one-beyond"):
        load_fixture_tree(tmp_path)


# --- FixedMemberDrainActivationByteFixtureV1 (rolling final drain and activation) ------------

FIXED = "FixedMemberDrainActivation"
_PAD = "fixture_padding"


def _fixed_actions(fixture: dict[str, Any]) -> tuple[Any, ...]:
    """Rebuild the oracle actions from the frozen request and its frozen exact images."""
    targets = fixture["targets"]
    return fixed_member_support.envelope_actions(
        fixture["expected_request"],
        tuple(target["expected_pre_state_map"] for target in targets),
        tuple(target["expected_result_map"] for target in targets),
    )


def _result_bytes(action: PutAction | UpdateAction) -> int:
    if isinstance(action, PutAction):
        return ORACLE.conservative_service_item_bytes(action.item)
    merged = {**action.key, **(action.pre_state.item or {}), **action.set_attributes}
    return ORACLE.conservative_service_item_bytes(merged)


def _padded(action: PutAction | UpdateAction, size: int) -> PutAction | UpdateAction:
    """Return the action with its resulting item grown to exactly `size` conservative bytes."""

    def grown(length: int) -> PutAction | UpdateAction:
        if isinstance(action, PutAction):
            return PutAction(
                action.table_name, action.key, {**action.item, _PAD: "x" * length}, action.pre_state
            )
        return UpdateAction(
            action.table_name,
            action.key,
            {**action.set_attributes, _PAD: "x" * length},
            action.pre_state,
        )

    length = size - _result_bytes(grown(0))
    assert length >= 0
    sized = grown(length)
    assert _result_bytes(sized) == size
    return sized


def test_fixed_member_profiles_pass_the_independent_checker_with_exact_action_counts(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    branches = fixture_tree[FIXED]
    assert set(branches) == {"maximum", "candidates-98", "no-eligible-98", "one-beyond"}
    for fixture in branches.values():
        check_fixed_member_fixture(fixture)
    counts = {name: branch["expected"]["action_count"] for name, branch in branches.items()}
    assert counts == {"maximum": 51, "candidates-98": 51, "no-eligible-98": 42, "one-beyond": 101}
    assert branches["maximum"]["typed_input"]["b_candidates"] == 512
    assert branches["candidates-98"]["typed_input"]["b_candidates"] == 98


def test_fixed_member_oracle_reproduces_every_frozen_total_within_every_limit(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for branch in ("maximum", "candidates-98", "no-eligible-98"):
        fixture = fixture_tree[FIXED][branch]
        envelope = ORACLE.validate_envelope(TransactionEnvelopeRequest(_fixed_actions(fixture)))
        expected = fixture["expected"]
        assert envelope.action_count == expected["action_count"]
        assert envelope.aggregate_upper_bound == expected["aggregate_upper_bound"]
        uppers = [target.target_upper_bound for target in envelope.targets]
        assert max(uppers) == expected["largest_target_upper_bound"]
        for measured, frozen in zip(envelope.targets, fixture["targets"], strict=True):
            assert measured.pre_state_bytes == frozen["pre_state_conservative_bytes"]
            assert measured.result_bytes == frozen["result_conservative_bytes"]
        assert expected["outcome"] == "accepted"
        assert envelope.action_count <= 100
        assert max(uppers) <= 400_000
        assert envelope.aggregate_upper_bound <= 4_000_000


def test_fixed_member_98_profiles_reproduce_from_the_production_builder(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    # The 512 profile runs the same builder with longer rosters; its drift check is the
    # committed generator's `--check` mode (`fixed_member_byte_fixture_support.py`).
    for branch in ("candidates-98", "no-eligible-98"):
        rebuilt = fixed_member_support.fixture_text(fixed_member_support.fixture(branch))
        committed = fixed_member_support.fixture_path(branch).read_text(encoding="utf-8")
        assert rebuilt == committed
        assert json.loads(committed) == fixture_tree[FIXED][branch]


def test_fixed_member_one_action_beyond_refuses_with_no_split_fallback(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[FIXED]["one-beyond"]
    assert fixture["expected"]["outcome"] == "refused"
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(_fixed_actions(fixture)))
    assert refusal.value.code == ACTION_COUNT_EXCEEDED
    assert refusal.value.context["action_count"] == 101


def test_fixed_member_target_one_byte_beyond_400000_refuses_with_no_split_fallback(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    actions = list(_fixed_actions(fixture_tree[FIXED]["maximum"]))
    index = next(
        position
        for position, action in enumerate(actions)
        if isinstance(action, PutAction) and str(action.key["pk"]).startswith("SERVING_TRANSITION#")
    )
    at_limit = [*actions[:index], _padded(actions[index], 400_000), *actions[index + 1 :]]
    accepted = ORACLE.validate_envelope(TransactionEnvelopeRequest(tuple(at_limit)))
    assert accepted.targets[index].result_bytes == 400_000
    beyond = [*actions[:index], _padded(actions[index], 400_001), *actions[index + 1 :]]
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(TransactionEnvelopeRequest(tuple(beyond)))
    assert refusal.value.code == SERVICE_ITEM_BYTES_EXCEEDED
    assert refusal.value.context["result_bytes"] == 400_001


def _grown_to_aggregate(actions: list[Any], aggregate: int) -> list[Any]:
    """Grow put and update results, each at most 400000 bytes, to exactly `aggregate`."""
    measured = ORACLE.validate_envelope(TransactionEnvelopeRequest(tuple(actions)))
    remaining = aggregate - measured.aggregate_upper_bound
    grown = list(actions)
    for position, (action, target) in enumerate(zip(actions, measured.targets, strict=True)):
        if remaining == 0:
            break
        if not isinstance(action, PutAction | UpdateAction):
            continue
        if target.pre_state_bytes > target.result_bytes:
            continue
        room = 400_000 - target.target_upper_bound
        step = min(room, remaining)
        if step < 64:  # leave room for the padding attribute's own overhead
            continue
        grown[position] = _padded(action, target.target_upper_bound + step)
        remaining -= step
    assert remaining == 0
    return grown


def test_fixed_member_aggregate_one_byte_beyond_4000000_refuses_with_no_split_fallback(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    actions = list(_fixed_actions(fixture_tree[FIXED]["maximum"]))
    at_limit = ORACLE.validate_envelope(
        TransactionEnvelopeRequest(tuple(_grown_to_aggregate(actions, 4_000_000)))
    )
    assert at_limit.aggregate_upper_bound == 4_000_000
    assert at_limit.action_count == 51
    with pytest.raises(ValidationError) as refusal:
        ORACLE.validate_envelope(
            TransactionEnvelopeRequest(tuple(_grown_to_aggregate(actions, 4_000_001)))
        )
    assert refusal.value.code == AGGREGATE_BYTES_EXCEEDED
    assert refusal.value.context["aggregate_upper_bound"] == 4_000_001


def test_fixed_member_checker_refuses_a_drifted_action_count_or_largest_target(
    fixture_tree: dict[str, dict[str, dict[str, Any]]],
) -> None:
    fixture = fixture_tree[FIXED]["candidates-98"]
    drifted = copy.deepcopy(fixture)
    drifted["typed_input"]["elected"] = False
    with pytest.raises(FixtureCheckError):
        check_fixed_member_fixture(drifted)
    largest = copy.deepcopy(fixture)
    largest["expected"]["largest_target_upper_bound"] += 1
    with pytest.raises(FixtureCheckError):
        check_fixed_member_fixture(largest)
    rekeyed = copy.deepcopy(fixture)
    rekeyed["targets"][40]["expected_pre_state_map"]["pk"] = {"S": "RUN#another"}
    with pytest.raises(FixtureCheckError):
        check_fixed_member_fixture(rekeyed)
