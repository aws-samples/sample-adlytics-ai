# SPDX-License-Identifier: MIT-0
"""Shared DynamoDB byte oracle for Stage 1 delivery (ADR-075 §3.2).

``Stage1DynamoByteOracle`` is the one shared pure encoder/measurement helper
invoked by feasibility and by every coordinator/store builder immediately before
its SDK call; no builder owns a second estimator. It keeps three quantities
distinct:

1. ``action_count`` — the number of distinct-item ``Put``, ``Update``, ``Delete``,
   and ``ConditionCheck`` actions; two actions targeting one item are rejected.
2. ``wire_json_bytes(value)`` — exactly the length of the canonical low-level JSON
   map after DynamoDB's ordinary low-level wire conversion (standard base64 text
   for ``B``/``BS``): ``len(json.dumps(value, ensure_ascii=True, sort_keys=True,
   separators=(",", ":"), allow_nan=False).encode("utf-8"))``. It pins builder
   serialization, request completeness, and calibration only. It is never called
   the AWS service item size, and complete request wire bytes are never used as
   proof of the service's 4 MB item aggregate.
3. ``conservative_service_item_bytes(item)`` — ``wire_json_bytes`` of the map
   produced by applying the production ``TypeSerializer`` to every attribute
   value, with the same low-level wire conversion. By the §3.2 type-induction
   proof this is greater than or equal to the service-counted item size for every
   admitted finite nesting of ``S``, ``N``, ``B``, ``BOOL``, ``NULL``, ``L``,
   ``M``, ``SS``, ``NS``, and ``BS``.

The envelope validator admits at most 100 distinct-item actions, at most 400000
conservative service-item bytes for every exact pre-state and resulting target
item, and at most 4000000 summed per-target maxima — deliberately below the
binary service limits of 409600 and 4194304 bytes. An infeasible request is
refused with a typed error before mutation, never split into a second
transaction. Unsupported tags, floating-point values, non-finite or otherwise
indeterminate numbers, empty sets, invalid nesting, and any value the production
serializer cannot classify are likewise rejected before mutation.

The ``item-v1`` and ``transaction-v1`` calibration builders reproduce the four
pinned §3.2 wire totals (400000/400001 and 4000000/4000001). They pin only the
wire serializer; neither is service-size or transaction-aggregate evidence.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from boto3.dynamodb.types import Binary, TypeSerializer

from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "ACTION_COUNT_EXCEEDED",
    "AGGREGATE_BYTES_EXCEEDED",
    "CALIBRATION_ITEM_V1_MAX_PAYLOAD",
    "CALIBRATION_TRANSACTION_V1_MAX_FINAL_PAYLOAD",
    "DUPLICATE_TARGET",
    "MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES",
    "MAX_CONSERVATIVE_SERVICE_ITEM_BYTES",
    "MAX_DISTINCT_ITEM_ACTIONS",
    "SERVICE_ITEM_BYTES_EXCEEDED",
    "UNSUPPORTED_VALUE",
    "ConditionCheckAction",
    "DeleteAction",
    "ExactPreState",
    "PutAction",
    "Stage1DynamoByteOracle",
    "TargetMeasurement",
    "TransactionAction",
    "TransactionEnvelopeRequest",
    "TransactionEnvelopeResult",
    "UpdateAction",
    "calibration_item_v1",
    "calibration_transaction_v1",
    "canonical_wire_text",
]

# Reviewed Stage-1 application envelope; deliberately below the 409600/4194304
# binary service limits. No cap is independently combinable with a different
# simultaneous profile (§11).
MAX_DISTINCT_ITEM_ACTIONS: Final = 100
MAX_CONSERVATIVE_SERVICE_ITEM_BYTES: Final = 400000
MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES: Final = 4000000

# Stable refusal codes. A refusal is typed and final: there is no split fallback.
UNSUPPORTED_VALUE: Final = ErrorCode("BYTE_ORACLE", 1)
DUPLICATE_TARGET: Final = ErrorCode("BYTE_ORACLE", 2)
ACTION_COUNT_EXCEEDED: Final = ErrorCode("BYTE_ORACLE", 3)
SERVICE_ITEM_BYTES_EXCEEDED: Final = ErrorCode("BYTE_ORACLE", 4)
AGGREGATE_BYTES_EXCEEDED: Final = ErrorCode("BYTE_ORACLE", 5)

# §3.2 item-v1 / transaction-v1 calibration constants; the four wire totals they
# reproduce are 400000/400001 and 4000000/4000001.
CALIBRATION_ITEM_V1_MAX_PAYLOAD: Final = 399913
CALIBRATION_TRANSACTION_V1_MAX_FINAL_PAYLOAD: Final = 366632
_CALIBRATION_ITEM_PARTITION_KEY: Final = "BYTE_ORACLE_ITEM_V1"
_CALIBRATION_TABLE_NAME: Final = "T" * 193
# The §3.2-pinned ClientRequestToken fixture label — an idempotency token, not a credential.
_CALIBRATION_TOKEN: Final = "byte-oracle-transaction-v1"  # noqa: S105
_CALIBRATION_ORDINAL_PAYLOAD: Final = 363000
_CALIBRATION_FINAL_PARTITION_KEY: Final = "TX#10#123456789"


def _unsupported(description: str, *, context: dict[str, object] | None = None) -> ValidationError:
    return ValidationError(
        UNSUPPORTED_VALUE,
        f"{description} The value is rejected before mutation.",
        context=dict(context or {}),
    )


def _wire_converted(value: object) -> object:
    """Apply DynamoDB's ordinary low-level wire conversion to one value.

    Standard base64 text replaces raw ``B``/``BS`` bytes; every other admitted
    leaf is already its wire form. Floating-point and unclassifiable values are
    indeterminate on the wire and refuse.
    """
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, float):
        raise _unsupported("A floating-point value is indeterminate on the wire.")
    if isinstance(value, int):
        return value
    if isinstance(value, Binary):
        raw = cast("Any", value).value
        return base64.b64encode(bytes(raw)).decode("ascii")
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        members = cast("Mapping[object, object]", value)
        converted: dict[str, object] = {}
        for name, member in members.items():
            if not isinstance(name, str):
                raise _unsupported("A wire map key must be a string.")
            converted[name] = _wire_converted(member)
        return converted
    if isinstance(value, list | tuple):
        return [_wire_converted(member) for member in cast("Sequence[object]", value)]
    raise _unsupported(
        "A value outside the admitted wire vocabulary cannot be classified.",
        context={"value_type": type(value).__name__},
    )


def _require_nonempty_sets(serialized: object, attribute_name: str) -> None:
    """Refuse the empty ``SS``/``NS``/``BS`` the production serializer vacuously admits.

    The §3.2 proof rejects empty sets before mutation; DynamoDB itself refuses them,
    so admitting one here would make an unconstructible request look feasible.
    """
    if not isinstance(serialized, Mapping):
        return
    for tag, member in cast("Mapping[object, object]", serialized).items():
        if tag in ("SS", "NS", "BS") and isinstance(member, list | tuple) and not member:
            raise _unsupported(
                "An empty set is rejected before mutation.",
                context={"attribute_name": attribute_name},
            )
        if isinstance(member, Mapping):
            for child in cast("Mapping[object, object]", member).values():
                _require_nonempty_sets(child, attribute_name)
        elif isinstance(member, list | tuple):
            for child in cast("Sequence[object]", member):
                _require_nonempty_sets(child, attribute_name)


def canonical_wire_text(value: object) -> str:
    """Return the canonical low-level JSON text for a wire-converted value.

    This is the exact §3.2 canonical form: ASCII-escaped, key-sorted, minimal
    separators, no NaN. Its UTF-8 length is ``wire_json_bytes``.
    """
    return json.dumps(
        _wire_converted(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class ExactPreState:
    """The strongly read exact pre-state of one target; ``None`` is proven absence.

    Absence is the exact size zero. A caller without an exact strong pre-state
    must refuse before mutation rather than construct an action.
    """

    item: Mapping[str, object] | None


def _require_target_shape(
    table_name: str,
    key: Mapping[str, object],
    pre_state: ExactPreState,
) -> None:
    if not table_name.strip():
        raise ValueError("an action requires a table name.")
    if not key:
        raise ValueError("an action requires a nonempty key.")
    if pre_state.item is not None:
        for name, value in key.items():
            if name not in pre_state.item or pre_state.item[name] != value:
                raise ValueError("an exact pre-state must carry the action's key unchanged.")


@dataclass(frozen=True, slots=True)
class PutAction:
    """One ``Put`` of a complete item; the result is exactly the put item."""

    table_name: str
    key: Mapping[str, object]
    item: Mapping[str, object]
    pre_state: ExactPreState

    def __post_init__(self) -> None:
        _require_target_shape(self.table_name, self.key, self.pre_state)
        for name, value in self.key.items():
            if name not in self.item or self.item[name] != value:
                raise ValueError("a put item must carry the action's key unchanged.")


@dataclass(frozen=True, slots=True)
class UpdateAction:
    """One ``SET``-form ``Update``; the result is the expression-applied item.

    The reconstructed result is the exact pre-state (or, when proven absent, the
    key alone) with every set attribute applied.
    """

    table_name: str
    key: Mapping[str, object]
    set_attributes: Mapping[str, object]
    pre_state: ExactPreState

    def __post_init__(self) -> None:
        _require_target_shape(self.table_name, self.key, self.pre_state)
        if not self.set_attributes:
            raise ValueError("an update must set at least one attribute.")
        if any(name in self.key for name in self.set_attributes):
            raise ValueError("an update may never rewrite a key attribute.")


@dataclass(frozen=True, slots=True)
class DeleteAction:
    """One ``Delete``; the result is absence, whose exact size is zero."""

    table_name: str
    key: Mapping[str, object]
    pre_state: ExactPreState

    def __post_init__(self) -> None:
        _require_target_shape(self.table_name, self.key, self.pre_state)


@dataclass(frozen=True, slots=True)
class ConditionCheckAction:
    """One ``ConditionCheck``; the result is the unchanged exact pre-state."""

    table_name: str
    key: Mapping[str, object]
    pre_state: ExactPreState

    def __post_init__(self) -> None:
        _require_target_shape(self.table_name, self.key, self.pre_state)


type TransactionAction = PutAction | UpdateAction | DeleteAction | ConditionCheckAction


@dataclass(frozen=True, slots=True)
class TransactionEnvelopeRequest:
    """The distinct-item actions of one transaction, in emission order."""

    actions: tuple[TransactionAction, ...]

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError("an envelope requires at least one action.")


@dataclass(frozen=True, slots=True)
class TargetMeasurement:
    """Conservative service-item bytes for one target's pre-state and result."""

    table_name: str
    canonical_key: str
    pre_state_bytes: int
    result_bytes: int

    @property
    def target_upper_bound(self) -> int:
        """The admitted upper bound covering either interpretation of the target."""
        return max(self.pre_state_bytes, self.result_bytes)


@dataclass(frozen=True, slots=True)
class TransactionEnvelopeResult:
    """An accepted envelope: action count, per-target maxima, and their sum."""

    action_count: int
    targets: tuple[TargetMeasurement, ...]
    aggregate_upper_bound: int


def _resulting_item(action: TransactionAction) -> Mapping[str, object] | None:
    """Reconstruct the exact resulting item; ``None`` is absence after ``Delete``."""
    if isinstance(action, PutAction):
        return action.item
    if isinstance(action, DeleteAction):
        return None
    if isinstance(action, ConditionCheckAction):
        return action.pre_state.item
    merged: dict[str, object] = dict(action.key)
    if action.pre_state.item is not None:
        merged.update(action.pre_state.item)
    merged.update(action.set_attributes)
    return merged


class Stage1DynamoByteOracle:
    """The shared production serialization and feasibility seam (§3.2).

    Pure computation over typed request maps: no client, no network, no clock.
    Preflight calls it on built requests; every runtime builder calls it on its
    final request immediately before dispatch. Neither may reconstruct, trim, or
    independently estimate a map.
    """

    def __init__(self) -> None:
        self._serializer = TypeSerializer()

    def wire_json_bytes(self, value: object) -> int:
        """Measure the canonical low-level JSON bytes of a converted wire value.

        For an item, pass the low-level map produced by the production
        ``TypeSerializer``; for a request, pass the complete converted low-level
        SDK request map. Serialization/request-completeness calibration only —
        never proof of the service's 4 MB item aggregate.
        """
        return len(canonical_wire_text(value).encode("utf-8"))

    def conservative_service_item_bytes(self, item: Mapping[str, object]) -> int:
        """Measure the admitted upper bound for the item's service-counted bytes."""
        return self.wire_json_bytes(self._serialized(item))

    def action_count(self, request: TransactionEnvelopeRequest) -> int:
        """Count distinct-item actions, refusing two actions that target one item."""
        return len(self._measured_targets(request))

    def validate_envelope(self, request: TransactionEnvelopeRequest) -> TransactionEnvelopeResult:
        """Admit the transaction envelope or refuse with a typed error.

        Refusal is final: an over-envelope request is never split into a second
        transaction.
        """
        targets = self._measured_targets(request)
        if len(targets) > MAX_DISTINCT_ITEM_ACTIONS:
            raise ValidationError(
                ACTION_COUNT_EXCEEDED,
                "The transaction exceeds 100 distinct-item actions and is refused "
                "before mutation; there is no split fallback.",
                context={"action_count": len(targets)},
            )
        for measurement in targets:
            if measurement.target_upper_bound > MAX_CONSERVATIVE_SERVICE_ITEM_BYTES:
                raise ValidationError(
                    SERVICE_ITEM_BYTES_EXCEEDED,
                    "A pre-state or resulting item exceeds 400000 conservative "
                    "service-item bytes and is refused before mutation; there is "
                    "no split fallback.",
                    context={
                        "table_name": measurement.table_name,
                        "pre_state_bytes": measurement.pre_state_bytes,
                        "result_bytes": measurement.result_bytes,
                    },
                )
        aggregate = sum(measurement.target_upper_bound for measurement in targets)
        if aggregate > MAX_AGGREGATE_TARGET_UPPER_BOUND_BYTES:
            raise ValidationError(
                AGGREGATE_BYTES_EXCEEDED,
                "The summed per-target maxima exceed 4000000 bytes and the "
                "transaction is refused before mutation; there is no split fallback.",
                context={"aggregate_upper_bound": aggregate},
            )
        return TransactionEnvelopeResult(
            action_count=len(targets),
            targets=targets,
            aggregate_upper_bound=aggregate,
        )

    def _serialized(self, item: Mapping[str, object]) -> dict[str, object]:
        """Apply the production serializer per attribute, refusing what it cannot classify."""
        encoded: dict[str, object] = {}
        for name, value in item.items():
            if not name:
                raise _unsupported("An attribute name must be a nonempty string.")
            try:
                serialized = self._serializer.serialize(value)
            except (ArithmeticError, TypeError, ValueError) as error:
                raise _unsupported(
                    "The production serializer cannot classify an attribute value.",
                    context={"attribute_name": name, "reason": str(error)},
                ) from error
            _require_nonempty_sets(serialized, name)
            encoded[name] = serialized
        return encoded

    def _measured_targets(
        self, request: TransactionEnvelopeRequest
    ) -> tuple[TargetMeasurement, ...]:
        measured: list[TargetMeasurement] = []
        seen: set[tuple[str, str]] = set()
        for action in request.actions:
            canonical_key = canonical_wire_text(self._serialized(action.key))
            target = (action.table_name, canonical_key)
            if target in seen:
                raise ValidationError(
                    DUPLICATE_TARGET,
                    "Two actions target one item; the transaction is refused before mutation.",
                    context={"table_name": action.table_name},
                )
            seen.add(target)
            pre_state = action.pre_state.item
            pre_state_bytes = (
                0 if pre_state is None else self.conservative_service_item_bytes(pre_state)
            )
            result = _resulting_item(action)
            result_bytes = 0 if result is None else self.conservative_service_item_bytes(result)
            measured.append(
                TargetMeasurement(
                    table_name=action.table_name,
                    canonical_key=canonical_key,
                    pre_state_bytes=pre_state_bytes,
                    result_bytes=result_bytes,
                )
            )
        return tuple(measured)


def _calibration_low_level_item(partition_key: str, payload_length: int) -> dict[str, object]:
    serializer = TypeSerializer()
    item: dict[str, object] = {
        "partition_key": partition_key,
        "record_key": "R",
        "payload": "x" * payload_length,
    }
    return {name: serializer.serialize(value) for name, value in item.items()}


def calibration_item_v1(payload_length: int) -> dict[str, object]:
    """Build the §3.2 ``item-v1`` low-level calibration map for one payload length.

    ``wire_json_bytes`` is 400000 at payload length 399913 and 400001 at 399914.
    A wire fixture only — never service-size evidence.
    """
    return _calibration_low_level_item(_CALIBRATION_ITEM_PARTITION_KEY, payload_length)


def calibration_transaction_v1(final_payload_length: int) -> dict[str, object]:
    """Build the §3.2 ``transaction-v1`` low-level request map, exactly as pinned.

    Eleven ``Put`` members in ordinal order 00..10, the 193-character table name,
    ten 363000-byte payloads, and no other request or ``Put`` member.
    ``wire_json_bytes`` is 4000000 at final payload length 366632 and 4000001 at
    366633. A wire fixture only — never transaction-aggregate evidence.
    """
    members: list[dict[str, object]] = [
        {
            "Put": {
                "Item": _calibration_low_level_item(
                    f"TX#{ordinal:02d}", _CALIBRATION_ORDINAL_PAYLOAD
                ),
                "TableName": _CALIBRATION_TABLE_NAME,
            }
        }
        for ordinal in range(10)
    ]
    members.append(
        {
            "Put": {
                "Item": _calibration_low_level_item(
                    _CALIBRATION_FINAL_PARTITION_KEY, final_payload_length
                ),
                "TableName": _CALIBRATION_TABLE_NAME,
            }
        }
    )
    return {"ClientRequestToken": _CALIBRATION_TOKEN, "TransactItems": members}
