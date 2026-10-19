# SPDX-License-Identifier: MIT-0
"""Strict canonical JSON codec for Runtime V2 structured conversation contracts."""

import contextlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import UnionType
from typing import TYPE_CHECKING, Literal, Union, cast, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

from domain.conversation.answer_stream import ConversationStreamEventV4
from domain.semantics.page_context import PageContextReference

_TUPLE_ANNOTATION_PARTS = 2


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"conversation contract JSON repeats field {key!r}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r} is not permitted.")


def _load(serialized: str) -> object:
    try:
        return json.loads(serialized, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except TypeError, ValueError:
        raise ValueError("conversation contract is not valid strict JSON.") from None


def _json_value(value: object, label: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number.")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{label} item") for item in cast("list[object]", value)]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast("dict[object, object]", value).items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{label} object keys must be nonblank strings.")
            result[key] = _json_value(item, f"{label}.{key}")
        return result
    raise ValueError(f"{label} is not a JSON value.")


def _decode_union(value: object, annotation: object, label: str) -> object:
    candidates = get_args(annotation)
    if value is None and type(None) in candidates:
        return None
    successes: list[object] = []
    for candidate in candidates:
        if candidate is type(None):
            continue
        with contextlib.suppress(TypeError, ValueError):
            successes.append(_decode(value, candidate, label))
    if len(successes) != 1:
        raise ValueError(f"{label} does not match exactly one permitted union variant.")
    return successes[0]


def _decode_dataclass(value: object, annotation: object, label: str) -> object:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    item = cast("dict[str, object]", value)
    contract_type = cast("type[DataclassInstance]", annotation)
    contract_fields = fields(contract_type)
    expected = frozenset(field.name for field in contract_fields)
    actual = frozenset(item)
    annotations = get_type_hints(
        contract_type,
        localns={"PageContextReference": PageContextReference},
    )
    required = frozenset(
        field.name
        for field in contract_fields
        if field.default is MISSING and field.default_factory is MISSING
    )
    if missing := required - actual:
        raise ValueError(
            f"{label} fields are not exact; "
            f"missing={sorted(missing)}, unknown={sorted(actual - expected)}."
        )
    if unknown := actual - expected:
        raise ValueError(f"{label} fields are not exact; missing=[], unknown={sorted(unknown)}.")
    kwargs = {
        field.name: _decode(item[field.name], annotations[field.name], f"{label}.{field.name}")
        for field in contract_fields
        if field.name in item
    }
    constructor = cast("Callable[..., object]", contract_type)
    return constructor(**kwargs)


def _decode(  # noqa: PLR0911, PLR0912, PLR0915 - closed reflection over contract annotations
    value: object, annotation: object, label: str
) -> object:
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return _decode_union(value, annotation, label)
    if origin is Literal:
        choices = get_args(annotation)
        for choice in choices:
            if isinstance(choice, StrEnum) and isinstance(value, str) and value == choice.value:
                return choice
            if type(value) is type(choice) and value == choice:
                return value
        raise ValueError(f"{label} is not a permitted literal.")
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError(f"{label} must be an array.")
        arguments = get_args(annotation)
        if len(arguments) != _TUPLE_ANNOTATION_PARTS or arguments[1] is not Ellipsis:
            raise TypeError(f"unsupported tuple annotation for {label}.")
        items = cast("list[object]", value)
        return tuple(_decode(item, arguments[0], f"{label} item") for item in items)
    if origin in {dict, Mapping}:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object.")
        key_type, value_type = get_args(annotation)
        if key_type is not str:
            raise TypeError(f"unsupported dictionary key type for {label}.")
        return {
            key: _decode(item, value_type, f"{label}.{key}")
            for key, item in cast("dict[str, object]", value).items()
        }
    if annotation is object:
        return _json_value(value, label)
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
        return value
    if annotation is datetime:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be an ISO datetime string.")
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{label} must be an ISO datetime string.") from None
    if annotation is date:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be an ISO date string.")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{label} must be an ISO date string.") from None
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be boolean.")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label} must be an integer, not boolean.")
        return value
    if annotation is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{label} must be numeric, not boolean.")
        return float(value)
    if annotation is Decimal:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be an exact decimal string.")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"{label} is not an exact decimal string.") from None
        if not decimal_value.is_finite():
            raise ValueError(f"{label} must be finite.")
        return decimal_value
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a closed enum string.")
        try:
            return annotation(value)
        except ValueError:
            raise ValueError(f"{label} is not a permitted enum value.") from None
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(value, annotation, label)
    raise TypeError(f"unsupported contract annotation for {label}: {annotation!r}.")


def _materialize(value: object) -> object:  # noqa: PLR0911 - closed reflected value kinds
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _materialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("contract decimal value must be finite.")
        return str(value)
    if isinstance(value, tuple):
        return [_materialize(item) for item in cast("tuple[object, ...]", value)]
    if isinstance(value, list):
        return [_materialize(item) for item in cast("list[object]", value)]
    if isinstance(value, Mapping):
        return {
            key: _materialize(item) for key, item in cast("Mapping[str, object]", value).items()
        }
    return _json_value(value, "contract value")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _materialize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def contract_to_json(value: object) -> str:
    """Encode one contract as canonical compact sorted UTF-8 JSON text."""
    return _canonical_json(value)


def decode_contract[T](serialized: str, expected: type[T]) -> T:
    """Decode one complete strict JSON object into a named dataclass contract."""
    return cast("T", _decode(_load(serialized), expected, expected.__name__))


def _decode_family(serialized: str, expected: object, label: str) -> object:
    return _decode(_load(serialized), expected, label)


def decode_public_conversation_stream_event(serialized: str) -> ConversationStreamEventV4:
    """Decode one exact canonical post-open ConversationStream.v4 variant."""
    return cast(
        "ConversationStreamEventV4",
        _decode_family(serialized, ConversationStreamEventV4, "public conversation stream event"),
    )
