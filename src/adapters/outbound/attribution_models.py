# SPDX-License-Identifier: MIT-0
"""Load the governed temporal attribution-model registry from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from domain.semantics.attribution import (
    AttributionMethod,
    AttributionModel,
    AttributionModelError,
    AttributionModelRegistry,
)

REGISTRY_PATH = Path("contracts/shared/attribution-models.yaml")
type Mapping = dict[str, object]


def _mapping(value: object, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise AttributionModelError(f"{where}: expected a mapping.")
    return cast("Mapping", value)


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise AttributionModelError(f"{where}: expected a list.")
    return cast("list[object]", value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise AttributionModelError(f"{where}: expected a string.")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    return tuple(_text(item, f"{where}[]") for item in _list(value, where))


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttributionModelError(f"{where}: expected an integer.")
    return value


def _exact(value: Mapping, expected: set[str], where: str) -> None:
    if set(value) != expected:
        raise AttributionModelError(
            f"{where}: fields must be exactly {', '.join(sorted(expected))}."
        )


def _reject_duplicate_keys(node: yaml.Node | None, where: str) -> None:
    if node is None:
        return
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                raise AttributionModelError(f"{where}: duplicate YAML key {key!r}.")
            seen.add(key)
            _reject_duplicate_keys(value_node, where)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _reject_duplicate_keys(child, where)


def _model(value: object, where: str) -> AttributionModel:
    raw = _mapping(value, where)
    _exact(
        raw,
        {
            "key",
            "label",
            "method",
            "lookback_days",
            "time_basis",
            "eligible_sources",
            "tie_break",
            "population",
            "revenue_scope",
        },
        where,
    )
    method_name = _text(raw["method"], f"{where}.method")
    try:
        method = AttributionMethod(method_name)
    except ValueError as error:
        raise AttributionModelError(f"{where}: unknown method {method_name!r}.") from error
    return AttributionModel(
        key=_text(raw["key"], f"{where}.key"),
        label=_text(raw["label"], f"{where}.label"),
        method=method,
        lookback_days=_integer(raw["lookback_days"], f"{where}.lookback_days"),
        time_basis=_text(raw["time_basis"], f"{where}.time_basis"),
        eligible_sources=_strings(raw["eligible_sources"], f"{where}.eligible_sources"),
        tie_break=_strings(raw["tie_break"], f"{where}.tie_break"),
        population=_text(raw["population"], f"{where}.population"),
        revenue_scope=_text(raw["revenue_scope"], f"{where}.revenue_scope"),
    )


def load_attribution_models(root: Path) -> AttributionModelRegistry:
    """Load the exact active attribution-model registry."""
    path = root / REGISTRY_PATH
    if not path.is_file():
        raise AttributionModelError(f"attribution model registry is missing at {path}.")
    text = path.read_text()
    composed = cast(
        "object",
        yaml.compose(text, Loader=yaml.SafeLoader),  # type: ignore[reportUnknownMemberType]
    )
    if composed is not None and not isinstance(composed, yaml.Node):
        raise AttributionModelError(f"{path}: parser returned an invalid YAML node.")
    _reject_duplicate_keys(composed, str(path))
    raw = _mapping(cast("object", yaml.safe_load(text)), str(path))
    _exact(raw, {"version", "models"}, str(path))
    return AttributionModelRegistry(
        version=_integer(raw["version"], f"{path}.version"),
        models=tuple(
            _model(item, f"{path}.models[{index}]")
            for index, item in enumerate(_list(raw["models"], f"{path}.models"))
        ),
    )
