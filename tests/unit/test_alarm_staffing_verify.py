# SPDX-License-Identifier: MIT-0
"""FR-10 (owner-gate Item 20): the destination verifier's checks (a) and (b).

At least one confirmed email subscription matching the endpoint digest passes, and extra
confirmed subscribers are accepted. A pending-only or missing match, or an inexact alarm action,
is a failure.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from adapters.jobs.alarm_staffing.__main__ import destination_verdict

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

TOPIC = "arn:aws:sns:us-east-1:000000000000:adlytics-dev-ingestion-alarms"
DIGEST = hashlib.sha256(b"owner@example.com").hexdigest()


def _subscription(
    endpoint: str, protocol: str = "email", *, pending: bool = False
) -> dict[str, str]:
    return {
        "Protocol": protocol,
        "TopicArn": TOPIC,
        "Endpoint": endpoint,
        "SubscriptionArn": "PendingConfirmation" if pending else f"{TOPIC}:{endpoint}",
    }


def _alarm(name: str, actions: list[str] | None = None) -> dict[str, Any]:
    return {
        "AlarmName": name,
        "ActionsEnabled": True,
        "AlarmActions": [TOPIC] if actions is None else actions,
        "OKActions": [TOPIC],
    }


def _verdict(subscriptions: list[dict[str, str]], alarms: list[dict[str, Any]]) -> dict[str, Any]:
    return destination_verdict(
        topic_arn=TOPIC,
        expected_account="000000000000",
        region="us-east-1",
        topic_attributes={"TopicArn": TOPIC},
        subscriptions=subscriptions,
        listed=[str(alarm["AlarmName"]) for alarm in alarms],
        alarms=alarms,
        endpoint_sha256=DIGEST,
    )


def test_a_confirmed_matching_email_with_extra_confirmed_subscribers_passes() -> None:
    verdict = _verdict(
        [_subscription("owner@example.com"), _subscription("+15550100", "sms")],
        [_alarm("a"), _alarm("b")],
    )
    assert verdict["passed"], verdict["failures"]
    assert verdict["subscription_count"] == 2


@pytest.mark.parametrize(
    "subscriptions",
    [
        [],
        [_subscription("owner@example.com", pending=True)],
        [_subscription("someone-else@example.com")],
    ],
)
def test_no_confirmed_matching_subscription_fails(subscriptions: list[dict[str, str]]) -> None:
    verdict = _verdict(subscriptions, [_alarm("a")])
    assert not verdict["passed"]


def test_an_alarm_acting_on_another_destination_fails() -> None:
    verdict = _verdict(
        [_subscription("owner@example.com")],
        [_alarm("a", [TOPIC, "arn:aws:sns:us-east-1:000000000000:other"])],
    )
    assert not verdict["passed"]
    assert any("a: actions" in failure for failure in verdict["failures"])
