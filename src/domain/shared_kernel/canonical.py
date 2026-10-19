# SPDX-License-Identifier: MIT-0
"""Versioned canonical field projections for durable digests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Set
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast


def _projection(value: object) -> object:  # noqa: PLR0911 -- one closed type projection
    """Project typed values to deterministic JSON, sorting every unordered container."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, Enum):
        return [
            "enum",
            f"{type(value).__module__}.{type(value).__qualname__}",
            _projection(value.value),
        ]
    if is_dataclass(value) and not isinstance(value, type):
        return [
            "dataclass",
            f"{type(value).__module__}.{type(value).__qualname__}",
            [[item.name, _projection(getattr(value, item.name))] for item in fields(value)],
        ]
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        projected = [(_projection(key), _projection(item)) for key, item in mapping.items()]
        return ["mapping", sorted(projected, key=lambda pair: _encoded(pair[0]))]
    if isinstance(value, tuple):
        return ["tuple", [_projection(item) for item in cast("tuple[object, ...]", value)]]
    if isinstance(value, list):
        return ["list", [_projection(item) for item in cast("list[object]", value)]]
    if isinstance(value, Set):
        projected = [_projection(item) for item in cast("Set[object]", value)]
        return ["set", sorted(projected, key=_encoded)]
    raise TypeError(
        f"{type(value).__module__}.{type(value).__qualname__} has no canonical projection"
    )


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
