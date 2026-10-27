# SPDX-License-Identifier: MIT-0
"""Load the governed alert rules from YAML (ADR-108 decision 3; Scorecard "Also changed")."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from adapters.outbound import repository_yaml
from domain.presentation.alert_rules import (
    AlertDirection,
    AlertRule,
    AlertRuleError,
    AlertRuleScope,
)
from domain.presentation.insights import InsightHorizonRole

__all__ = ["ALERT_RULES_PATH", "load_alert_rules"]

ALERT_RULES_PATH = Path("contracts/alerts/alert-rules.yaml")
_RULE_KEYS = frozenset(
    {"id", "insight_contract", "finding", "scope", "direction", "threshold", "horizon"}
)
_SCOPE_KEYS = frozenset({"ecosystem", "platform", "channel"})

type _Mapping = dict[str, object]


def _mapping(value: object, where: str, keys: frozenset[str]) -> _Mapping:
    if not isinstance(value, dict):
        raise AlertRuleError(f"{where}: expected a mapping.")
    item = cast("_Mapping", value)
    if frozenset(item) != keys:
        raise AlertRuleError(f"{where}: expected exactly {', '.join(sorted(keys))}.")
    return item


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlertRuleError(f"{where}: expected nonblank text.")
    return value


def _rule(value: object, index: int) -> AlertRule:
    where = f"rules[{index}]"
    item = _mapping(value, where, _RULE_KEYS)
    scope = _mapping(item["scope"], f"{where}.scope", _SCOPE_KEYS)
    try:
        threshold = Decimal(_text(item["threshold"], f"{where}.threshold"))
        direction = AlertDirection(_text(item["direction"], f"{where}.direction"))
        horizon = InsightHorizonRole(_text(item["horizon"], f"{where}.horizon"))
    except (InvalidOperation, ValueError) as error:
        raise AlertRuleError(f"{where}: {error}") from None
    return AlertRule(
        rule_id=_text(item["id"], f"{where}.id"),
        insight_contract=_text(item["insight_contract"], f"{where}.insight_contract"),
        finding_id=_text(item["finding"], f"{where}.finding"),
        scope=AlertRuleScope(
            _text(scope["ecosystem"], f"{where}.scope.ecosystem"),
            _text(scope["platform"], f"{where}.scope.platform"),
            None if scope["channel"] is None else _text(scope["channel"], f"{where}.scope.channel"),
        ),
        direction=direction,
        threshold=threshold,
        horizon=horizon,
    )


def load_alert_rules(root: Path) -> tuple[AlertRule, ...]:
    """Read every governed rule; contract consistency is checked by `validate_alert_rules`."""
    document = repository_yaml.safe_load((root / ALERT_RULES_PATH).read_text())
    item = _mapping(document, str(ALERT_RULES_PATH), frozenset({"version", "rules"}))
    if item["version"] != 1:
        raise AlertRuleError(f"{ALERT_RULES_PATH}: version must be 1.")
    rules = item["rules"]
    if not isinstance(rules, list):
        raise AlertRuleError(f"{ALERT_RULES_PATH}: rules must be a list.")
    return tuple(_rule(value, index) for index, value in enumerate(cast("list[object]", rules)))
