# SPDX-License-Identifier: MIT-0
"""Versioned canonical field projections for durable digests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Mapping, Set
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

_MEMO: ContextVar[dict[int, tuple[object, object]] | None] = ContextVar(
    "canonical_projection_memo", default=None
)
"""Inside `projection_memo`: each immutable dataclass's projection by identity, with the object."""


@contextmanager
def projection_memo() -> Generator[None]:
    """Project each immutable dataclass once for every digest taken inside the scope.

    A bake task digests every bound request in each pass, and the requests share the same
    publication objects: at 14 days, 49 requests re-projected 84 publications 3,892 times per pass
    (dev, 2026-10-25, D4). Inside the scope a frozen dataclass holding only immutable values is
    projected once, by identity; the memo holds the object, so its id can't be reused. One holding
    a list, set, mapping or non-frozen dataclass anywhere is projected every time. Every digest
    equals the unscoped one. Worker threads started with `copy_context()` share the memo.
    """
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def _projection(value: object) -> object:
    """Project typed values to deterministic JSON, sorting every unordered container."""
    return _project(value, _MEMO.get())[0]


def _project(  # noqa: PLR0911 -- one closed type projection
    value: object, memo: dict[int, tuple[object, object]] | None
) -> tuple[object, bool]:
    """Return the projection and whether the value is immutable all the way down."""
    if value is None or isinstance(value, bool | int | float | str):
        return value, True
    if isinstance(value, Decimal):
        return ["decimal", str(value)], True
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()], True
    if isinstance(value, date):
        return ["date", value.isoformat()], True
    if isinstance(value, Enum):
        inner, immutable = _project(value.value, memo)
        return ["enum", f"{type(value).__module__}.{type(value).__qualname__}", inner], immutable
    if is_dataclass(value) and not isinstance(value, type):
        return _project_dataclass(value, memo)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        pairs = [(_project(key, memo)[0], _project(item, memo)[0]) for key, item in mapping.items()]
        return ["mapping", sorted(pairs, key=lambda pair: _encoded(pair[0]))], False
    if isinstance(value, tuple | list):
        items = [_project(item, memo) for item in cast("tuple[object, ...]", value)]
        kind = "tuple" if isinstance(value, tuple) else "list"
        immutable = isinstance(value, tuple) and all(flag for _, flag in items)
        return [kind, [item for item, _ in items]], immutable
    if isinstance(value, Set):
        items = [_project(item, memo) for item in cast("Set[object]", value)]
        immutable = isinstance(value, frozenset) and all(flag for _, flag in items)
        return ["set", sorted((item for item, _ in items), key=_encoded)], immutable
    raise TypeError(
        f"{type(value).__module__}.{type(value).__qualname__} has no canonical projection"
    )


def _project_dataclass(
    value: DataclassInstance, memo: dict[int, tuple[object, object]] | None
) -> tuple[object, bool]:
    """Project a dataclass instance; inside a memo, an immutable one is projected once."""
    if memo is not None:
        held = memo.get(id(value))
        if held is not None and held[0] is value:
            return held[1], True
    declared = fields(value)
    projected = [_project(getattr(value, item.name), memo) for item in declared]
    immutable = getattr(type(value), "__dataclass_params__").frozen and all(  # noqa: B009
        flag for _, flag in projected
    )
    result = [
        "dataclass",
        f"{type(value).__module__}.{type(value).__qualname__}",
        [[item.name, field[0]] for item, field in zip(declared, projected, strict=True)],
    ]
    if memo is not None and immutable:
        memo[id(value)] = (value, result)
    return result, immutable


def _encoded(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_model_digest(namespace: str, value: object) -> str:
    """Hash every field of a model under an explicit format namespace."""
    if not namespace.strip():
        raise ValueError("a canonical model digest needs a nonblank namespace")
    projected: list[Any] = [namespace, _projection(value)]
    return hashlib.sha256(_encoded(projected).encode("utf-8")).hexdigest()
