# SPDX-License-Identifier: MIT-0
"""The governed alert rules file and its validator (ADR-108 decision 3; UI slice 9a A5)."""

from __future__ import annotations

import pathlib
from dataclasses import replace
from decimal import Decimal

import pytest

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.preset_artifact_publisher.scorecard_source import scorecard_alert_rules
from adapters.outbound.alert_rules import load_alert_rules
from domain.presentation.alert_rules import AlertRuleError, validate_alert_rules
from domain.presentation.insights import InsightHorizonRole
from domain.presentation.scorecard import SCORECARD_ALERT_FINDINGS

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-108")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = tuple(contract for _, contract in load_insight_contracts(ROOT))
FINDINGS = {"network-scorecard": SCORECARD_ALERT_FINDINGS}
CHANNELS = frozenset({"amazon_dsp", "amazon_sponsored_products"})


def test_the_committed_rules_are_the_three_scorecard_defaults_and_hold() -> None:
    rules = scorecard_alert_rules(ROOT)
    assert [rule.rule_id for rule in rules] == [
        "dsp-roas-drop",
        "dsp-spend-spike",
        "sponsored-products-roas-drop",
    ]
    assert {rule.horizon for rule in rules} == {InsightHorizonRole.TACTICAL_REVIEW}


def test_a_threshold_below_the_contracts_materiality_fails_loading() -> None:
    rules = load_alert_rules(ROOT)
    assert validate_alert_rules(rules, CONTRACTS, FINDINGS, CHANNELS) == rules
    # network-scorecard sizes a percentage move at 5 at tactical-review; 4.99 is noise.
    below = (replace(rules[0], threshold=Decimal("4.99")), *rules[1:])
    with pytest.raises(AlertRuleError, match="below the contract's tactical-review materiality"):
        validate_alert_rules(below, CONTRACTS, FINDINGS, CHANNELS)


def test_an_unknown_finding_an_ungoverned_channel_and_a_repeated_id_are_refused() -> None:
    rules = load_alert_rules(ROOT)
    unknown = (replace(rules[0], finding_id="channel-roas-leader"),)
    with pytest.raises(AlertRuleError, match="doesn't bake channel-roas-leader"):
        validate_alert_rules(unknown, CONTRACTS, FINDINGS, CHANNELS)
    ungoverned = (replace(rules[0], scope=replace(rules[0].scope, channel="amazon_radio")),)
    with pytest.raises(AlertRuleError, match="amazon_radio isn't a governed channel"):
        validate_alert_rules(ungoverned, CONTRACTS, FINDINGS, CHANNELS)
    with pytest.raises(AlertRuleError, match="rule ids are unique"):
        validate_alert_rules((rules[0], rules[0]), CONTRACTS, FINDINGS, CHANNELS)


def test_a_rule_over_a_level_finding_fails_loading_because_a_level_has_no_direction() -> None:
    rules = load_alert_rules(ROOT)
    # Paired: the committed rule over the ROAS change loads.
    assert validate_alert_rules(rules[:1], CONTRACTS, FINDINGS, CHANNELS) == rules[:1]
    # A share of spend is a level: always positive, never compared, so "Up" would be false.
    share = (replace(rules[0], finding_id="channel-share-of-network-spend", threshold=Decimal(1)),)
    with pytest.raises(AlertRuleError, match="is a share-of-total level, not a change"):
        validate_alert_rules(share, CONTRACTS, FINDINGS, CHANNELS)
