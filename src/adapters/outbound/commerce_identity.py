# SPDX-License-Identifier: MIT-0
"""Strict filesystem adapter for the governed commerce identity bridge."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import cast

import yaml

from adapters.outbound import repository_yaml
from domain.semantics.commerce_identity import (
    CommerceIdentityBridge,
    MarketplaceDisplayName,
    ProductIdentifierRule,
)
from domain.semantics.identity import (
    CommerceScopeKey,
    IdentityMapping,
    MarketplaceKey,
    ProductKey,
    SourceIdentifier,
)

__all__ = ["CommerceIdentityLoadError", "load_commerce_identity_bridge"]

BRIDGE_PATH = Path("contracts/shared/commerce-identities.yaml")


class CommerceIdentityLoadError(ValueError):
    """The declarative bridge is malformed or contains undeclared fields."""


def load_commerce_identity_bridge(root: Path) -> CommerceIdentityBridge:
    """Load marketplace aliases and product namespace rules into strict domain values."""
    path = root / BRIDGE_PATH
    text = path.read_text()
    composed = repository_yaml.compose(text)
    if composed is not None and not isinstance(composed, yaml.Node):
        raise CommerceIdentityLoadError(f"{path}: parser returned an invalid node.")
    _reject_duplicate_keys(composed, str(path))
    document = _mapping(repository_yaml.safe_load(text), str(path))
    _keys(
        document,
        (
            {
                "version",
                "commerce_scope",
                "marketplace_aliases",
                "marketplace_display_names",
                "product_mappings",
                "product_identifier_rules",
            },
            set(),
        ),
        str(path),
    )
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise CommerceIdentityLoadError(f"{path}: version must be 1.")
    scope = CommerceScopeKey(_text(document.get("commerce_scope"), "commerce_scope"))
    aliases = tuple(
        _marketplace(item, index)
        for index, item in enumerate(
            _list(document.get("marketplace_aliases"), "marketplace_aliases")
        )
    )
    display_names = tuple(
        _marketplace_display_name(item, index)
        for index, item in enumerate(
            _list(document.get("marketplace_display_names"), "marketplace_display_names")
        )
    )
    products = tuple(
        _product_mapping(item, index, scope)
        for index, item in enumerate(_list(document.get("product_mappings"), "product_mappings"))
    )
    rules = tuple(
        _product_rule(item, index)
        for index, item in enumerate(
            _list(document.get("product_identifier_rules"), "product_identifier_rules")
        )
    )
    return CommerceIdentityBridge(
        scope,
        aliases,
        marketplace_display_names=display_names,
        product_mappings=products,
        product_rules=rules,
    )


def _marketplace_display_name(value: object, index: int) -> MarketplaceDisplayName:
    where = f"marketplace_display_names[{index}]"
    item = _mapping(value, where)
    _keys(item, ({"target", "value"}, set()), where)
    return MarketplaceDisplayName(
        MarketplaceKey(_text(item.get("target"), "target")),
        _text(item.get("value"), "value"),
    )


def _marketplace(value: object, index: int) -> IdentityMapping[MarketplaceKey]:
    where = f"marketplace_aliases[{index}]"
    item = _mapping(value, where)
    _keys(
        item,
        (
            {"namespace", "value", "target", "mapping_version", "valid_from", "valid_to"},
            {"valid_to"},
        ),
        where,
    )
    return IdentityMapping(
        source=SourceIdentifier(
            _text(item.get("namespace"), "namespace"), _text(item.get("value"), "value")
        ),
        target=MarketplaceKey(_text(item.get("target"), "target")),
        mapping_version=_integer(item.get("mapping_version"), "mapping_version"),
        valid_from=_date(item.get("valid_from"), "valid_from"),
        valid_to=None if item.get("valid_to") is None else _date(item.get("valid_to"), "valid_to"),
    )


def _product_rule(value: object, index: int) -> ProductIdentifierRule:
    where = f"product_identifier_rules[{index}]"
    item = _mapping(value, where)
    _keys(
        item,
        (
            {
                "source_namespace",
                "canonical_namespace",
                "value_pattern",
                "normalization",
                "mapping_version",
                "valid_from",
                "valid_to",
            },
            {"valid_to"},
        ),
        where,
    )
    return ProductIdentifierRule(
        source_namespace=_text(item.get("source_namespace"), "source_namespace"),
        canonical_namespace=_text(item.get("canonical_namespace"), "canonical_namespace"),
        value_pattern=_text(item.get("value_pattern"), "value_pattern"),
        normalization=_text(item.get("normalization"), "normalization"),
        mapping_version=_integer(item.get("mapping_version"), "mapping_version"),
        valid_from=_date(item.get("valid_from"), "valid_from"),
        valid_to=None if item.get("valid_to") is None else _date(item.get("valid_to"), "valid_to"),
    )


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CommerceIdentityLoadError(f"{where}: expected a mapping.")
    return cast("dict[str, object]", value)


def _list(value: object, key: str) -> list[object]:
    if not isinstance(value, list):
        raise CommerceIdentityLoadError(f"{key}: expected a list.")
    return cast("list[object]", value)


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommerceIdentityLoadError(f"{key}: expected a non-empty string.")
    return value


def _integer(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommerceIdentityLoadError(f"{key}: expected an integer.")
    return value


def _date(value: object, key: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CommerceIdentityLoadError(f"{key}: expected an ISO date.")


def _keys(
    value: dict[str, object],
    fields: tuple[set[str], set[str]],
    where: str,
) -> None:
    allowed, optional = fields
    unknown = set(value) - allowed
    missing = allowed - optional - set(value)
    if unknown or missing:
        raise CommerceIdentityLoadError(
            f"{where}: unknown={sorted(unknown)}, missing={sorted(missing)}."
        )


def _product_mapping(
    value: object, index: int, scope: CommerceScopeKey
) -> IdentityMapping[ProductKey]:
    where = f"product_mappings[{index}]"
    item = _mapping(value, where)
    allowed = {
        "namespace",
        "value",
        "target",
        "marketplace",
        "mapping_version",
        "valid_from",
        "valid_to",
    }
    _keys(item, (allowed, {"valid_to"}), where)
    return IdentityMapping(
        source=SourceIdentifier(
            _text(item.get("namespace"), "namespace"), _text(item.get("value"), "value")
        ),
        target=ProductKey(
            _text(item.get("target"), "target"),
            scope,
            MarketplaceKey(_text(item.get("marketplace"), "marketplace")),
        ),
        mapping_version=_integer(item.get("mapping_version"), "mapping_version"),
        valid_from=_date(item.get("valid_from"), "valid_from"),
        valid_to=None if item.get("valid_to") is None else _date(item.get("valid_to"), "valid_to"),
    )


def _reject_duplicate_keys(node: yaml.Node | None, where: str) -> None:
    if node is None:
        return
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                raise CommerceIdentityLoadError(f"{where}: duplicate YAML key {key!r}.")
            seen.add(key)
            _reject_duplicate_keys(value_node, where)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _reject_duplicate_keys(child, where)
