# SPDX-License-Identifier: MIT-0
r"""The staffed alarm destination verifier (FR-10; owner-gate Item 20).

One read-only command under an explicit operator profile, printing one `RESULT {json}` line.
Every target-affecting input is explicit: profile, region, expected account, environment.

`verify --endpoint-sha256 <hex>` reads back `GetTopicAttributes`, `ListSubscriptionsByTopic`,
`ListTagsForResource` and `DescribeAlarms` for every alarm in the ingestion and status-processor
stacks. It passes with at least one confirmed (never `PendingConfirmation`) email subscription
whose endpoint digest is the given one, extra confirmed subscribers accepted, and every alarm's
actions enabled and exactly the selected topic: FR-10 checks (a) and (b). Check (c), one
induced `ALARM -> OK` per gate run, belongs to the gate run. Only the digest and the
subscription ARN are printed, never the address. Owner-gate Item 20 retired the challenge
`issue` and the acknowledgement `bind`.

    python -m adapters.jobs.alarm_staffing verify --profile <p> --region <r> \\
        --expected-account <account> --env <env> --endpoint-sha256 <hex>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import boto3

from adapters.outbound.cloudformation_stack import stack_resource_summaries
from domain.operations.generated_alarm_destinations import SELECTED_ARMS

__all__ = ["destination_verdict", "main"]

_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_PENDING: Final = "PendingConfirmation"
_ALARM_STACKS: Final = ("Ingestion", "OperationalStatusProcessor")


def endpoint_digest(endpoint: str) -> str:
    """Return the digest evidence stores in place of an endpoint."""
    return hashlib.sha256(endpoint.encode()).hexdigest()


def destination_verdict(  # noqa: PLR0913 -- one pure verdict over the five readbacks
    *,
    topic_arn: str,
    expected_account: str,
    region: str,
    topic_attributes: Mapping[str, str],
    subscriptions: Sequence[Mapping[str, str]],
    listed: Sequence[str],
    alarms: Sequence[Mapping[str, Any]],
    endpoint_sha256: str,
) -> dict[str, object]:
    """Judge the destination: account, region, a confirmed subscription, exact actions.

    `listed` is every alarm the stacks declare; `alarms` is what `DescribeAlarms` returned for
    them. That call silently drops a composite alarm or an unknown name, so every listed alarm
    must be described exactly once and nothing else may be.
    """
    failures: list[str] = []
    parts = topic_arn.split(":")
    if len(parts) != 6 or parts[3] != region or parts[4] != expected_account:  # noqa: PLR2004
        failures.append("the topic is not in the expected account and region")
    if topic_attributes.get("TopicArn") != topic_arn:
        failures.append("the topic attributes name another topic")
    matching = [
        item
        for item in subscriptions
        if item.get("Protocol") == "email"
        and item.get("TopicArn") == topic_arn
        and endpoint_digest(str(item.get("Endpoint", ""))) == endpoint_sha256
    ]
    confirmed = [item for item in matching if item.get("SubscriptionArn", _PENDING) != _PENDING]
    if not matching:
        failures.append("no email subscription matches the endpoint digest")
    elif not confirmed:
        failures.append("every matching subscription is PendingConfirmation")
    checked: dict[str, object] = {}
    for alarm in alarms:
        name = str(alarm["AlarmName"])
        actions = sorted(str(item) for item in alarm.get("AlarmActions", []))
        ok = sorted(str(item) for item in alarm.get("OKActions", []))
        enabled = bool(alarm.get("ActionsEnabled"))
        checked[name] = {"actions_enabled": enabled, "alarm_actions": actions, "ok_actions": ok}
        if not enabled or actions != [topic_arn] or any(item != topic_arn for item in ok):
            failures.append(f"{name}: actions are not exactly the selected topic")
    if not alarms:
        failures.append("no alarm was read")
    described = [str(alarm["AlarmName"]) for alarm in alarms]
    if sorted(described) != sorted(set(listed)) or len(set(listed)) != len(listed):
        missing = sorted(set(listed) - set(described))
        extra = sorted(set(described) - set(listed))
        failures.append(
            f"the described alarms differ from the listed: missing={missing} extra={extra}"
        )
    return {
        "topic_arn": topic_arn,
        "endpoint_sha256": endpoint_sha256,
        "subscription_arn": str(confirmed[0]["SubscriptionArn"]) if confirmed else None,
        "subscription_count": len(subscriptions),
        "alarms": checked,
        "listed_count": len(listed),
        "failures": failures,
        "passed": not failures,
    }


def _print(document: Mapping[str, object]) -> None:
    sys.stdout.write("RESULT " + json.dumps(dict(document), sort_keys=True, default=str) + "\n")


def _stack_resources(session: Any, stack: str, kind: str) -> list[str]:
    physical = [
        item.get("PhysicalResourceId", "")
        for item in stack_resource_summaries(session.client("cloudformation"), stack)
        if item["ResourceType"] == kind
    ]
    if not all(physical):
        raise PermissionError(f"a {kind} in {stack} has no physical id")
    return physical


def _selected_topic(session: Any, environment: str, topic_arn: str | None) -> str:
    arm = cast("dict[str, str]", SELECTED_ARMS).get(environment)
    if arm is None:
        raise PermissionError("no staffed destination arm is selected for this environment")
    if arm == "ExistingConfirmedSnsTopic":
        if not topic_arn:
            raise PermissionError("--topic-arn is required for the topic arm")
        return topic_arn
    if topic_arn:
        raise PermissionError("--topic-arn conflicts with the selected email arm")
    topics = [
        arn
        for arn in _stack_resources(session, f"Adlytics-Ingestion-{environment}", "AWS::SNS::Topic")
        if arn.endswith(f":adlytics-{environment}-ingestion-alarms")
    ]
    if len(topics) != 1:
        raise PermissionError("the ingestion alarm topic is not exactly one stack resource")
    return topics[0]


def _verify(session: Any, arguments: argparse.Namespace, topic: str) -> dict[str, object]:
    sns = session.client("sns")
    attributes = dict(sns.get_topic_attributes(TopicArn=topic)["Attributes"])
    subscriptions: list[dict[str, str]] = []
    for page in sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=topic):
        subscriptions.extend(dict(item) for item in page.get("Subscriptions", []))
    # aws-pagination: SNS ListTagsForResource returns every tag of one topic; it has no token.
    tags = sns.list_tags_for_resource(ResourceArn=topic).get("Tags", [])
    names: list[str] = []
    for stack in _ALARM_STACKS:
        names += _stack_resources(
            session, f"Adlytics-{stack}-{arguments.env}", "AWS::CloudWatch::Alarm"
        )
    alarms: list[dict[str, Any]] = []
    describe_alarms = session.client("cloudwatch").get_paginator("describe_alarms")
    # AlarmNames takes at most 100 names, and each request's results still page.
    for start in range(0, len(names), 100):
        for page in describe_alarms.paginate(AlarmNames=names[start : start + 100]):
            alarms += [dict(item) for item in page.get("MetricAlarms", [])]
    verdict = destination_verdict(
        topic_arn=topic,
        expected_account=str(arguments.expected_account),
        region=str(arguments.region),
        topic_attributes=attributes,
        subscriptions=subscriptions,
        listed=names,
        alarms=alarms,
        endpoint_sha256=str(arguments.endpoint_sha256),
    )
    verdict["tags"] = {str(item["Key"]): str(item["Value"]) for item in tags}
    return verdict


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify",))
    for name in ("--profile", "--region", "--expected-account", "--env"):
        parser.add_argument(name, required=True)
    parser.add_argument("--endpoint-sha256")
    parser.add_argument("--topic-arn")
    return parser


def _run(arguments: argparse.Namespace) -> int:
    if str(arguments.profile).casefold() == "default":
        raise PermissionError("an explicit nondefault profile is required")
    session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != str(arguments.expected_account):
        raise PermissionError("caller account does not match the explicitly expected account")
    topic = _selected_topic(session, str(arguments.env), arguments.topic_arn)
    if not arguments.endpoint_sha256 or _DIGEST.fullmatch(arguments.endpoint_sha256) is None:
        raise PermissionError("--endpoint-sha256 must be a 64-character hex digest")
    result: dict[str, object] = {"command": "verify", **_verify(session, arguments, topic)}
    _print(result)
    return 0 if result.get("passed") else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; a refused input exits 2 and prints why."""
    arguments = _parser().parse_args(argv)
    try:
        return _run(arguments)
    except PermissionError as error:
        _print({"command": arguments.command, "refused": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
