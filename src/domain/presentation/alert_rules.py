# SPDX-License-Identifier: MIT-0
"""Governed alert rules and the "Also changed" rows they select (ADR-108, alerts-declined path).

The owner deferred the alert evaluator (OD-7), so a page renders the alerts-declined path: it
lists the baked deterministic findings that meet a governed rule for its current window. A
rule names one insight contract and finding, a scope, a direction, a threshold and the horizon
it reads. Its threshold is at or above the contract's own materiality at that horizon, so a
rule can never list a finding the contract itself would call noise. Rows are direction only:
no figure, so no row can state a cross-channel attributed total or its change.

Pure domain. The rules file is read and validated by an adapter; no clock is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from domain.presentation.insights import (
    FindingUnit,
    InsightContract,
    InsightFindingKind,
    InsightHorizonRole,
    OperandBasis,
)

__all__ = [
    "ALSO_CHANGED_LIMIT",
    "AlertDirection",
    "AlertRule",
    "AlertRuleError",
    "AlertRuleFinding",
    "AlertRuleScope",
    "validate_alert_rules",
]

ALSO_CHANGED_LIMIT: Final = 5
"""At most five "Also changed" rows per window (Scorecard LLD)."""
_CHANGE_KINDS: Final = frozenset(
    {
        InsightFindingKind.ABSOLUTE_CHANGE,
        InsightFindingKind.PERCENTAGE_CHANGE,
        InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
    }
)
"""The finding kinds that compare a window with its prior. A rule's row states a direction
("Up", "Down") against the prior window, so only a change can carry one: a level such as a
share of spend has a sign but was never compared."""


class AlertRuleError(ValueError):
    """A governed alert rule is malformed or contradicts its insight contract."""


class AlertDirection(StrEnum):
    """Which way a finding must move for its rule to list it."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class AlertRuleScope:
    """Where a rule applies: one ecosystem and platform, and a channel on an ad platform."""

    ecosystem: str
    platform: str
    channel: str | None

    def __post_init__(self) -> None:
        if not self.ecosystem.strip() or not self.platform.strip():
            raise AlertRuleError("an alert rule scope names its ecosystem and platform.")
        if self.channel is not None and not self.channel.strip():
            raise AlertRuleError("an alert rule's channel is a governed channel key.")


@dataclass(frozen=True, slots=True)
class AlertRuleFinding:
    """The finding a rule reads: its kind, and the unit and basis its contract sizes it on."""

    finding_id: str
    kind: InsightFindingKind
    unit: FindingUnit
    basis: OperandBasis
    metric_id: str


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One governed rule: contract, finding, scope, direction, threshold and horizon."""

    rule_id: str
    insight_contract: str
    finding_id: str
    scope: AlertRuleScope
    direction: AlertDirection
    threshold: Decimal
    horizon: InsightHorizonRole

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.insight_contract.strip():
            raise AlertRuleError("an alert rule names itself and its insight contract.")
        if not self.threshold.is_finite() or self.threshold <= 0:
            raise AlertRuleError(f"{self.rule_id}: a threshold is a positive finite number.")

    def met_by(self, value: Decimal, horizon: InsightHorizonRole) -> bool:
        """Whether a signed finding value meets this rule at the window's horizon."""
        if horizon is not self.horizon:
            return False
        moved = value > 0 if self.direction is AlertDirection.UP else value < 0
        return moved and abs(value) >= self.threshold


def validate_alert_rules(  # noqa: PLR0913, PLR0917 -- the rules and the three facts they're checked on
    rules: tuple[AlertRule, ...],
    contracts: tuple[InsightContract, ...],
    findings: dict[str, tuple[AlertRuleFinding, ...]],
    channels: frozenset[str],
) -> tuple[AlertRule, ...]:
    """Refuse a rule set that contradicts its contracts; return it unchanged when it holds.

    `findings` names, per insight contract id, the findings a page bakes for it, with the
    unit and basis its materiality sizes them on. A rule's threshold must be at or above that
    materiality at its horizon, its finding must exist and be a change (a row states a direction
    against the prior window), its channel must be governed, and no two rules may share an id.
    Every error is named at once.
    """
    errors: list[str] = []
    seen: set[str] = set()
    by_id = {contract.id.value: contract for contract in contracts}
    for rule in rules:
        if rule.rule_id in seen:
            errors.append(f"{rule.rule_id}: rule ids are unique.")
        seen.add(rule.rule_id)
        contract = by_id.get(rule.insight_contract)
        declared = {item.finding_id: item for item in findings.get(rule.insight_contract, ())}
        finding = declared.get(rule.finding_id)
        if contract is None or finding is None:
            errors.append(
                f"{rule.rule_id}: {rule.insight_contract} doesn't bake {rule.finding_id}."
            )
            continue
        if finding.kind not in _CHANGE_KINDS:
            errors.append(
                f"{rule.rule_id}: {rule.finding_id} is a {finding.kind.value} level, not a "
                "change, so it has no direction to list."
            )
        if rule.scope.channel is not None and rule.scope.channel not in channels:
            errors.append(f"{rule.rule_id}: {rule.scope.channel} isn't a governed channel.")
        floor = contract.predicate.materiality.threshold_for(
            rule.horizon, finding.basis, unit=finding.unit, metric_id=finding.metric_id
        )
        if rule.threshold < floor:
            errors.append(
                f"{rule.rule_id}: threshold {rule.threshold} is below the contract's "
                f"{rule.horizon.value} materiality {floor}."
            )
    if errors:
        raise AlertRuleError(" ".join(errors))
    return rules
