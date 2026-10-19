# SPDX-License-Identifier: MIT-0
"""ADR-080 §4 tamper-detection patterns judged by EventBridge itself, not the offline matcher.

The infra unit tests match these patterns with `infra/test/support/event-pattern.ts`, which only
proves the patterns agree with that matcher. Here every rule pattern and every pipe filter in the
synthesized `RecordIntegrityStack` template is sent to `events:TestEventPattern`, a read-only call
that evaluates a pattern against a sample event and changes nothing in the account.

The patterns come from `infra/cdk.out/Adlytics-RecordIntegrity-<env>.template.json`, the template a
deploy uploads, so run `cdk synth` for the environment first, and use a profile in the account and
Region it was synthesized for: the table-ARN patterns carry both literally. The test skips without
AWS credentials or without that template. Samples are fictional and follow each API's CloudTrail
shape.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest

if TYPE_CHECKING:
    from types_boto3_events.client import EventBridgeClient

pytestmark = pytest.mark.integration

ENV_NAME = os.environ.get("ADL_TEST_ENV", "dev")
_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "cdk.out"
    / f"Adlytics-RecordIntegrity-{ENV_NAME}.template.json"
)
_CLOUDTRAIL = "AWS API Call via CloudTrail"
_BACKUP_JOB = "Backup Job State Change"
_TABLE_SUFFIX = {
    "receipts": "agent-quality-audit",
    "findings": "agent-quality-findings",
    "deliveries": "evaluation-deliveries",
    "writer-receipts": "evaluation-writer-receipts",
    "verdicts": "evaluation-verdicts",
}
_EVALUATION_FAMILIES = ("deliveries", "writer-receipts", "verdicts")
# CloudTrail's `eventSource` per service; CloudWatch records as `monitoring`.
_EVENT_SOURCE = {
    "dynamodb": "dynamodb.amazonaws.com",
    "backup": "backup.amazonaws.com",
    "lambda": "lambda.amazonaws.com",
    "iam": "iam.amazonaws.com",
    "events": "events.amazonaws.com",
    "pipes": "pipes.amazonaws.com",
    "cloudwatch": "monitoring.amazonaws.com",
    "logs": "logs.amazonaws.com",
    "ssm": "ssm.amazonaws.com",
}
_PREFIX = f"adlytics-{ENV_NAME}-record-"
_FUNCTION = f"adlytics-{ENV_NAME}-ConversationFunction"
_ROLE = f"adlytics-{ENV_NAME}-conversation-function"

Event = dict[str, object]
Samples = tuple[list[Event], list[Event]]


class _Target:
    """The caller's account and Region, and the EventBridge client that judges the samples."""

    def __init__(self) -> None:
        profile = os.environ.get("AWS_PROFILE", "").strip()
        if not profile:
            pytest.skip("record-integrity pattern validation requires AWS_PROFILE")
        try:
            session = boto3.Session(profile_name=profile)
            self.account = str(session.client("sts").get_caller_identity()["Account"])
        except Exception as error:  # noqa: BLE001 — pytest.skip raises: no credentials
            pytest.skip(
                f"record-integrity pattern credentials are unavailable: {type(error).__name__}"
            )
        region = session.region_name
        if not region:
            pytest.skip("record-integrity pattern validation requires a profile Region")
        self.region = region
        self.events: EventBridgeClient = session.client("events")

    def matches(self, pattern: str, event: Event) -> bool:
        envelope: Event = {
            "version": "0",
            "id": "00000000-0000-4000-8000-000000000000",
            "account": self.account,
            "time": "2026-09-27T00:00:00Z",
            "region": self.region,
            "resources": [],
            **event,
        }
        response = self.events.test_event_pattern(EventPattern=pattern, Event=json.dumps(envelope))
        return bool(response["Result"])

    @property
    def table_base(self) -> str:
        return f"arn:aws:dynamodb:{self.region}:{self.account}:table/"


@pytest.fixture(scope="module")
def target() -> _Target:
    return _Target()


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    if not _TEMPLATE.is_file():
        pytest.skip(f"run cdk synth for {ENV_NAME} first; {_TEMPLATE.name} is absent")
    return cast("dict[str, Any]", json.loads(_TEMPLATE.read_text(encoding="utf-8")))


def _resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    resources = cast("dict[str, dict[str, Any]]", template.get("Resources", {}))
    return [
        cast("dict[str, Any]", resource["Properties"])
        for resource in resources.values()
        if resource.get("Type") == resource_type
    ]


def _call(service: str, event_name: str, parameters: dict[str, object]) -> Event:
    """One CloudTrail management event as EventBridge delivers it."""
    return {
        "source": f"aws.{service}",
        "detail-type": _CLOUDTRAIL,
        "detail": {
            "eventVersion": "1.09",
            "userIdentity": {"type": "AssumedRole"},
            "eventSource": _EVENT_SOURCE[service],
            "eventName": event_name,
            "requestParameters": parameters,
            "eventType": "AwsApiCall",
            "managementEvent": True,
        },
    }


def _wrong_source(event: Event) -> Event:
    """Report the same call from another service; the rule must key on Lambda's source."""
    detail = dict(cast("dict[str, object]", event["detail"]))
    detail["eventSource"] = "fictional.amazonaws.com"
    return {**event, "detail": detail}


def _table(family: str) -> str:
    return f"adlytics-{ENV_NAME}-{_TABLE_SUFFIX[family]}"


def _control_plane(target: _Target, family: str) -> Samples:
    name = _table(family)
    arn = f"{target.table_base}{name}"
    return (
        [
            _call("dynamodb", "UpdateTimeToLive", {"tableName": name}),
            _call("dynamodb", "DeleteTable", {"tableName": arn}),
            _call("dynamodb", "ExportTableToPointInTime", {"tableArn": arn}),
            _call("dynamodb", "RestoreTableToPointInTime", {"sourceTableName": name}),
            _call("dynamodb", "PutResourcePolicy", {"resourceArn": arn}),
            _call(
                "dynamodb",
                "PutResourcePolicy",
                {"resourceArn": f"{arn}/stream/2026-09-27T00:00:00.000"},
            ),
        ],
        [
            _call("dynamodb", "UpdateTimeToLive", {"tableName": f"{name}-copy"}),
            _call("dynamodb", "PutItem", {"tableName": name}),
            _call("dynamodb", "RestoreTableToPointInTime", {"targetTableName": name}),
        ],
    )


def _backup_job(arn: str) -> Event:
    return {
        "source": "aws.backup",
        "detail-type": _BACKUP_JOB,
        "detail": {"backupJobId": "fictional-job", "state": "RUNNING", "resourceArn": arn},
    }


def _functions(target: _Target) -> dict[str, Samples]:
    function_arn = f"arn:aws:lambda:{target.region}:{target.account}:function:{_FUNCTION}"
    partial_arn = f"{target.account}:function:{_FUNCTION}"
    role_arn = f"arn:aws:iam::{target.account}:role/{_ROLE}"
    execution_role = f"cdk-hnb659fds-cfn-exec-role-{target.account}-{target.region}"
    return {
        f"{_PREFIX}functions-by-name": (
            [
                _call("lambda", "UpdateFunctionCode20150331v2", {"functionName": _FUNCTION}),
                _call(
                    "lambda",
                    "UpdateFunctionConfiguration20150331v2",
                    {"functionName": function_arn},
                ),
            ],
            [_call("lambda", "UpdateFunctionCode20150331v2", {"functionName": f"x{_FUNCTION}"})],
        ),
        f"{_PREFIX}functions-by-role": (
            [_call("lambda", "CreateFunction20150331", {"functionName": "x", "role": role_arn})],
            [
                _call(
                    "lambda",
                    "CreateFunction20150331",
                    {"functionName": "x", "role": f"{role_arn}-x"},
                )
            ],
        ),
        # Who may invoke a watched function: the permission calls name it in
        # functionName (a name or an ARN); the resource-policy calls in resourceArn.
        f"{_PREFIX}functions-by-permission": (
            [
                _call("lambda", "AddPermission20150331v2", {"functionName": _FUNCTION}),
                _call("lambda", "RemovePermission20150331v2", {"functionName": function_arn}),
                # The rule matches the verb by prefix, so an unsuffixed or differently
                # versioned name matches too, as does a partial ARN.
                _call("lambda", "AddPermission", {"functionName": partial_arn}),
                _call("lambda", "RemovePermission", {"functionName": _FUNCTION}),
            ],
            [
                _call("lambda", "AddPermission20150331v2", {"functionName": f"x{_FUNCTION}"}),
                _call("lambda", "GetPolicy20150331v2", {"functionName": _FUNCTION}),
                _wrong_source(
                    _call("lambda", "AddPermission20150331v2", {"functionName": _FUNCTION})
                ),
            ],
        ),
        f"{_PREFIX}functions-by-policy": (
            [
                _call("lambda", "PutResourcePolicy", {"resourceArn": function_arn}),
                _call("lambda", "DeleteResourcePolicy", {"resourceArn": function_arn}),
                _call("lambda", "PutResourcePolicy20240101", {"resourceArn": function_arn}),
            ],
            [
                _call("lambda", "PutResourcePolicy", {"resourceArn": f"{function_arn}-x"}),
                _call("lambda", "GetResourcePolicy", {"resourceArn": function_arn}),
                _wrong_source(_call("lambda", "PutResourcePolicy", {"resourceArn": function_arn})),
            ],
        ),
        f"{_PREFIX}role-changes": (
            [
                _call("iam", "UpdateAssumeRolePolicy", {"roleName": _ROLE}),
                _call("iam", "DeleteRole", {"roleName": f"adlytics-{ENV_NAME}-evaluation-writer"}),
                _call("iam", "PutRolePolicy", {"roleName": execution_role}),
            ],
            [
                _call("iam", "UpdateAssumeRolePolicy", {"roleName": "fictional-role"}),
                _call("iam", "GetRole", {"roleName": _ROLE}),
            ],
        ),
    }


def _detection() -> dict[str, Samples]:
    rule = f"{_PREFIX}tables-receipts"
    alarm = f"{_PREFIX}item-changes"
    group = f"/adlytics/{ENV_NAME}/record-integrity/item-changes"
    return {
        f"{_PREFIX}self-events": (
            [
                _call("events", "DisableRule", {"name": rule}),
                _call("events", "RemoveTargets", {"rule": rule, "ids": ["Target0"]}),
            ],
            [_call("events", "DisableRule", {"name": "fictional"})],
        ),
        f"{_PREFIX}self-pipes": (
            [_call("pipes", "StopPipe", {"name": f"{_PREFIX}receipts"})],
            [_call("pipes", "StartPipe", {"name": f"{_PREFIX}receipts"})],
        ),
        f"{_PREFIX}self-cloudwatch": (
            [
                _call("cloudwatch", "DeleteAlarms", {"alarmNames": ["fictional", alarm]}),
                _call("cloudwatch", "PutMetricAlarm", {"alarmName": alarm}),
            ],
            [_call("cloudwatch", "EnableAlarmActions", {"alarmNames": [alarm]})],
        ),
        f"{_PREFIX}self-logs": (
            [_call("logs", "DeleteLogGroup", {"logGroupName": group})],
            [_call("logs", "DeleteLogGroup", {"logGroupName": "/aws/lambda/fictional"})],
        ),
    }


def _by(identity: dict[str, object], event: Event) -> Event:
    """Return `event` as called by `identity` instead of the default assumed role."""
    detail = cast("dict[str, object]", event["detail"])
    return {**event, "detail": {**detail, "userIdentity": identity}}


def _assumed(issuer: str) -> dict[str, object]:
    """Return an assumed-role session identity issued by the role `issuer`."""
    return {
        "type": "AssumedRole",
        "sessionContext": {"sessionIssuer": {"type": "Role", "arn": issuer}},
    }


def _evaluation(target: _Target) -> dict[str, Samples]:
    """ADR-079: release-record writes by non-gate principals, and result-group actions."""
    # The retired V3 family's record name still falls under the release-record prefix (ADR-079).
    release = f"/adlytics/{ENV_NAME}/evaluation/release-v3"
    gate = f"arn:aws:iam::{target.account}:role/adlytics-{ENV_NAME}-evaluation-gate-v4"
    retired_gate = gate.replace("-v4", "-v3")
    operator = _assumed(f"arn:aws:iam::{target.account}:role/fictional-operator")
    group = f"/adlytics/{ENV_NAME}/evaluation/results-v4"
    group_arn = f"arn:aws:logs:{target.region}:{target.account}:log-group:{group}"
    other_arn = f"arn:aws:logs:{target.region}:{target.account}:log-group:/aws/lambda/fictional"
    v4_release = release.replace("-v3", "-v4")
    return {
        f"{_PREFIX}release-records": (
            [
                _by(operator, _call("ssm", "PutParameter", {"name": release, "type": "String"})),
                _by(operator, _call("ssm", "DeleteParameters", {"names": ["/x", v4_release]})),
                _by({"type": "IAMUser"}, _call("ssm", "DeleteParameter", {"name": release})),
                # The retired V3 gate's role is no longer exempt.
                _by(_assumed(retired_gate), _call("ssm", "PutParameter", {"name": v4_release})),
            ],
            [
                _by(_assumed(gate), _call("ssm", "PutParameter", {"name": release})),
                _by(operator, _call("ssm", "PutParameter", {"name": "/fictional/other"})),
                _by(operator, _call("ssm", "GetParameters", {"names": [release]})),
            ],
        ),
        f"{_PREFIX}result-groups": (
            [
                _call("logs", "PutSubscriptionFilter", {"logGroupName": group, "filterName": "x"}),
                _call("logs", "DeleteLogGroup", {"logGroupName": group}),
                _call("logs", "StartLiveTail", {"logGroupIdentifiers": [group_arn]}),
                _call("logs", "PutDataProtectionPolicy", {"logGroupIdentifier": group}),
                _call("logs", "PutResourcePolicy", {"policyName": "x", "resourceArn": group_arn}),
                _call("logs", "PutAccountPolicy", {"policyName": "x", "policyType": "x"}),
                _call("logs", "PutResourcePolicy", {"policyName": "x", "policyDocument": "{}"}),
            ],
            [
                _call("logs", "DeleteLogGroup", {"logGroupName": "/aws/lambda/fictional"}),
                _call("logs", "DescribeSubscriptionFilters", {"logGroupName": group}),
                _call("logs", "PutResourcePolicy", {"policyName": "x", "resourceArn": other_arn}),
            ],
        ),
    }


def _samples(target: _Target) -> dict[str, Samples]:
    """Per rule name: events it must match, and near misses it must not."""
    receipts = f"{target.table_base}{_table('receipts')}"
    return {
        **{f"{_PREFIX}tables-{family}": _control_plane(target, family) for family in _TABLE_SUFFIX},
        f"{_PREFIX}backup-jobs": (
            [_call("backup", "StartBackupJob", {"resourceArn": receipts})],
            [_call("backup", "StartBackupJob", {"resourceArn": f"{receipts}-copy"})],
        ),
        f"{_PREFIX}backup-job-states": ([_backup_job(receipts)], [_backup_job(f"{receipts}-x")]),
        **_functions(target),
        **_detection(),
        **_evaluation(target),
    }


def test_every_rule_pattern_matches_its_events_and_refuses_its_near_misses(
    target: _Target, template: dict[str, Any]
) -> None:
    patterns = {
        str(rule["Name"]): json.dumps(rule["EventPattern"])
        for rule in _resources(template, "AWS::Events::Rule")
    }
    samples = _samples(target)
    # A rule with no samples would go unjudged. Evaluation-family rules exist only when
    # evaluation input is provisioned, so only their samples may lack a rule.
    assert set(patterns) <= set(samples)
    assert set(samples) - set(patterns) <= {
        f"{_PREFIX}tables-{family}" for family in _EVALUATION_FAMILIES
    }
    for name, pattern in patterns.items():
        matching, missing = samples[name]
        for event in matching:
            assert target.matches(pattern, event), f"{name} did not match {event}"
        for event in missing:
            assert not target.matches(pattern, event), f"{name} matched {event}"


def _stream_record(event_name: str, user_identity: dict[str, str] | None) -> Event:
    """Build a KEYS_ONLY stream record under the envelope fields TestEventPattern requires."""
    record: Event = {
        "source": "fictional.stream-record",
        "detail-type": "DynamoDB stream record",
        "eventID": "1",
        "eventVersion": "1.1",
        "eventSource": "aws:dynamodb",
        "eventName": event_name,
        "dynamodb": {
            "Keys": {"audit_id": {"S": "fictional-audit"}, "item_key": {"S": "RECORD"}},
            "StreamViewType": "KEYS_ONLY",
        },
    }
    if user_identity is not None:
        record["userIdentity"] = user_identity
    return record


_TTL = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}


@pytest.mark.parametrize(
    "case",
    [
        (_stream_record("MODIFY", None), True),
        (_stream_record("REMOVE", None), True),
        (_stream_record("REMOVE", {"type": "Service", "principalId": "replication.example"}), True),
        (
            _stream_record(
                "REMOVE", {"type": "AssumedRole", "principalId": "dynamodb.amazonaws.com"}
            ),
            True,
        ),
        (_stream_record("REMOVE", {"principalId": "dynamodb.amazonaws.com"}), True),
        (_stream_record("REMOVE", _TTL), False),
        (_stream_record("INSERT", None), False),
    ],
    ids=[
        "modify",
        "remove",
        "remove-by-another-service",
        "remove-claiming-dynamodb-without-service-type",
        "remove-with-only-a-principal",
        "ttl-remove",
        "insert",
    ],
)
def test_every_pipe_admits_non_ttl_changes_and_nothing_else(
    target: _Target, template: dict[str, Any], case: tuple[Event, bool]
) -> None:
    """A pipe admits a record when any one of its filters matches it."""
    record, admitted = case
    pipes = _resources(template, "AWS::Pipes::Pipe")
    assert pipes
    for pipe in pipes:
        filters = cast(
            "list[dict[str, str]]", pipe["SourceParameters"]["FilterCriteria"]["Filters"]
        )
        passed = any(target.matches(entry["Pattern"], record) for entry in filters)
        assert passed is admitted, f"{pipe.get('Name')} admitted={passed} for {record['eventName']}"
