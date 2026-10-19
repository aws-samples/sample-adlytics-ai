# SPDX-License-Identifier: MIT-0
"""FR-10: the AlarmStaffingPolicy fails generation when incomplete, widened or stale."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from adapters.jobs.data_flow_registry.alarm_destinations import (
    AlarmDestinationContractError,
    load_contract,
    main,
    validate,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _contract() -> dict[str, Any]:
    return load_contract(ROOT)


def test_the_committed_projections_are_current() -> None:
    assert main(["--check"]) == 0


@pytest.mark.parametrize(
    ("section", "name", "value"),
    [
        ("staffing_policy", "responsible_owner_id", " "),
        ("staffing_policy", "responsible_owner_id", None),
        ("staffing_policy", "policy_revision", 0),
        ("staffing_policy", "extra", "x"),
        # Owner-gate Item 20: the retired acknowledgement fields are undeclared now.
        ("staffing_policy", "allowed_ack_group", "operations-status-read"),
        ("staffing_policy", "challenge_ttl_seconds", 3600),
        (None, "staffing_policy", None),
        ("destination_arms", "ExistingConfirmedSnsTopic", None),
        ("selected_arms", "dev", "SlackChannel"),
        (None, "selected_arms", {}),
    ],
)
def test_an_incomplete_or_widened_policy_fails_generation(
    section: str | None, name: str, value: object
) -> None:
    contract = copy.deepcopy(_contract())
    target: dict[str, Any] = contract if section is None else contract[section]
    if value is None:
        del target[name]
    else:
        target[name] = value
    with pytest.raises(AlarmDestinationContractError):
        validate(contract)


def test_the_committed_policy_is_valid() -> None:
    validate(_contract())
