# SPDX-License-Identifier: MIT-0
"""The one-time record-expiry backfill: exact requests, per-kind expiry, and counts-only output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from adapters.jobs.backfill_record_expiry.__main__ import (
    BackfillPlan,
    BackfillTables,
    build_parser,
    main,
    run,
)
from domain.conversation.audit import RecordRetention

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-080", "ADR-081")]

_TABLES = BackfillTables(
    receipts="receipts-test",
    findings="findings-test",
    deliveries="deliveries-test",
    writer_receipts="writer-receipts-test",
    verdicts="verdicts-test",
)
_AT = "2026-09-02T12:00:00+00:00"
_RECEIPT_EXPIRY = "1796126400"
"""`_AT` plus 90 days in whole epoch seconds: 2026-12-01T12:00:00Z."""
_BACKFILL_AT = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)
_ORPHAN_EXPIRY = "1798363800"
"""The backfill time plus 90 days: 2026-12-27T09:30:00Z."""
_LEGACY_EXPIRY = "1790760600"
"""The backfill time plus a 48-hour margin: 2026-09-30T09:30:00Z."""
_CONDITION = "attribute_exists(#key) AND attribute_not_exists(#expiry)"


def _plan(*, dry_run: bool = False) -> BackfillPlan:
    return BackfillPlan(
        retention=RecordRetention(90),
        legacy_margin_hours=48,
        backfill_at=_BACKFILL_AT,
        dry_run=dry_run,
    )


def _item(audit_id: str, item_key: str, **extra: dict[str, str]) -> dict[str, Any]:
    return {"audit_id": {"S": audit_id}, "item_key": {"S": item_key}, **extra}


def _receipt(audit_id: str, payload: object) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return _item(audit_id, "RECORD", payload={"S": text})


def _conditional_failure(old: dict[str, Any] | None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": "ConditionalCheckFailedException"}}
    if old is not None:
        response["Item"] = old
    return ClientError(cast("Any", response), "UpdateItem")


@guard_reserved_words
@dataclass
class _RecordingDynamo:
    """Records every low-level request; never a fake of DynamoDB semantics."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, list[dict[str, Any]]])
    page_size: int = 2
    update_failures: dict[tuple[str, str], Exception] = field(
        default_factory=dict[tuple[str, str], Exception]
    )
    requests: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def of(self, operation: str) -> list[dict[str, Any]]:
        return [request for name, request in self.requests if name == operation]

    def scan(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("Scan", request))
        items = self.tables.get(request["TableName"], [])
        start = int(request.get("ExclusiveStartKey", {}).get("offset", {}).get("N", "0"))
        page = items[start : start + self.page_size]
        response: dict[str, Any] = {"Items": [dict(item) for item in page]}
        if start + self.page_size < len(items):
            response["LastEvaluatedKey"] = {"offset": {"N": str(start + self.page_size)}}
        return response

    def get_item(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("GetItem", request))
        key = request["Key"]
        for item in self.tables.get(request["TableName"], []):
            if item["audit_id"] == key["audit_id"] and item["item_key"] == key["item_key"]:
                return {"Item": {"payload": item["payload"]}} if "payload" in item else {"Item": {}}
        return {}

    def update_item(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("UpdateItem", request))
        key = (request["Key"]["audit_id"]["S"], request["Key"]["item_key"]["S"])
        failure = self.update_failures.get(key)
        if failure is not None:
            raise failure
        return {}


def _expiries(client: _RecordingDynamo) -> dict[tuple[str, str, str], str]:
    return {
        (
            request["TableName"],
            request["Key"]["audit_id"]["S"],
            request["Key"]["item_key"]["S"],
        ): request["ExpressionAttributeValues"][":expiry"]["N"]
        for request in client.of("UpdateItem")
    }


def _counts(client: _RecordingDynamo, plan: BackfillPlan) -> dict[str, dict[str, int]]:
    document = run(cast("Any", client), _TABLES, plan).document(plan)
    return cast("dict[str, dict[str, int]]", document["counts"])


def test_a_receipt_expires_at_its_payload_creation_time_plus_the_retention() -> None:
    client = _RecordingDynamo(
        tables={_TABLES.receipts: [_receipt("audit-01", {"created_at": _AT})]}
    )

    counts = _counts(client, _plan())

    assert _expiries(client) == {(_TABLES.receipts, "audit-01", "RECORD"): _RECEIPT_EXPIRY}
    assert counts["receipt"]["applied"] == 1
    (update,) = client.of("UpdateItem")
    assert update == {
        "TableName": _TABLES.receipts,
        "Key": {"audit_id": {"S": "audit-01"}, "item_key": {"S": "RECORD"}},
        "UpdateExpression": "SET #expiry = :expiry",
        "ConditionExpression": _CONDITION,
        "ExpressionAttributeNames": {"#key": "item_key", "#expiry": "expires_at"},
        "ExpressionAttributeValues": {":expiry": {"N": _RECEIPT_EXPIRY}},
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }


def test_a_legacy_receipt_the_full_decoder_refuses_is_still_given_its_expiry() -> None:
    """Only `created_at` is read, so an old schema version never blocks its own expiry."""
    legacy = {"schema_version": "ConversationAuditRecord.v0", "created_at": _AT, "extra": [1]}
    client = _RecordingDynamo(tables={_TABLES.receipts: [_receipt("audit-01", legacy)]})

    _counts(client, _plan())

    assert _expiries(client) == {(_TABLES.receipts, "audit-01", "RECORD"): _RECEIPT_EXPIRY}


@pytest.mark.parametrize(
    "payload",
    ["not json", json.dumps(["created_at"]), json.dumps({}), json.dumps({"created_at": 1})],
    ids=["not-json", "not-object", "no-created-at", "not-text"],
)
def test_a_receipt_whose_creation_time_cannot_be_read_fails_and_is_not_written(
    payload: str,
) -> None:
    client = _RecordingDynamo(tables={_TABLES.receipts: [_receipt("audit-01", payload)]})

    counts = _counts(client, _plan())

    assert client.of("UpdateItem") == []
    assert counts["receipt"]["failed"] == 1


def test_a_receipt_whose_creation_time_is_not_utc_fails_and_is_not_written() -> None:
    client = _RecordingDynamo(
        tables={_TABLES.receipts: [_receipt("audit-01", {"created_at": "2026-09-02T12:00:00"})]}
    )

    assert _counts(client, _plan())["receipt"]["failed"] == 1
    assert client.of("UpdateItem") == []


def test_receipt_companions_expire_with_their_partition_receipt() -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                _item("audit-01", "DELIVERY"),
                _receipt("audit-01", {"created_at": _AT}),
                _item("audit-01", "EVALUATION_ADMISSION#admission-01"),
            ]
        }
    )

    counts = _counts(client, _plan())

    assert _expiries(client) == {
        (_TABLES.receipts, "audit-01", "DELIVERY"): _RECEIPT_EXPIRY,
        (_TABLES.receipts, "audit-01", "RECORD"): _RECEIPT_EXPIRY,
        (_TABLES.receipts, "audit-01", "EVALUATION_ADMISSION#admission-01"): _RECEIPT_EXPIRY,
    }
    assert counts["receipt_companion"]["applied"] == 2
    (read,) = client.of("GetItem")
    assert read == {
        "TableName": _TABLES.receipts,
        "Key": {"audit_id": {"S": "audit-01"}, "item_key": {"S": "RECORD"}},
        "ProjectionExpression": "#payload",
        "ExpressionAttributeNames": {"#payload": "payload"},
        "ConsistentRead": True,
    }


@pytest.mark.parametrize(
    "receipts",
    [[], [_receipt("audit-01", {"schema_version": "x"})]],
    ids=["no-receipt", "no-created-at"],
)
def test_a_companion_without_a_readable_receipt_is_an_orphan_at_backfill_time_plus_retention(
    receipts: list[dict[str, Any]],
) -> None:
    client = _RecordingDynamo(tables={_TABLES.receipts: [*receipts, _item("audit-01", "DELIVERY")]})

    counts = _counts(client, _plan())

    assert _expiries(client)[_TABLES.receipts, "audit-01", "DELIVERY"] == _ORPHAN_EXPIRY
    assert counts["receipt_companion"]["orphan"] == 1


@pytest.mark.parametrize(
    "item_key",
    [
        "FINDING#finding-01",
        "EVALUATION_DELIVERY#admission-01",
        "EVALUATION_WRITER#admission-01",
        "EVALUATION_RESULT#admission-01#evaluator-01",
    ],
)
def test_a_moved_family_legacy_item_expires_the_declared_margin_after_the_backfill(
    item_key: str,
) -> None:
    """Its receipt's expiry would not wait for the policy deploy that its removal must prove."""
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                _receipt("audit-01", {"created_at": _AT}),
                _item("audit-01", item_key),
            ]
        }
    )

    counts = _counts(client, _plan())

    assert _expiries(client)[_TABLES.receipts, "audit-01", item_key] == _LEGACY_EXPIRY
    assert counts["moved_family_legacy"]["applied"] == 1


def test_every_item_in_the_four_family_tables_expires_with_its_partition_receipt() -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [_receipt("audit-01", {"created_at": _AT})],
            _TABLES.findings: [_item("audit-01", "FINDING#finding-01")],
            _TABLES.deliveries: [_item("audit-01", "EVALUATION_DELIVERY#admission-01")],
            _TABLES.writer_receipts: [_item("audit-01", "EVALUATION_WRITER#admission-01")],
            _TABLES.verdicts: [
                _item("audit-01", "EVALUATION_RESULT#admission-01#evaluator-01"),
                _item("audit-02", "EVALUATION_RESULT#admission-02#evaluator-01"),
            ],
        }
    )

    counts = _counts(client, _plan())

    expiries = _expiries(client)
    assert expiries[_TABLES.findings, "audit-01", "FINDING#finding-01"] == _RECEIPT_EXPIRY
    assert (
        expiries[_TABLES.deliveries, "audit-01", "EVALUATION_DELIVERY#admission-01"]
        == _RECEIPT_EXPIRY
    )
    assert (
        expiries[_TABLES.writer_receipts, "audit-01", "EVALUATION_WRITER#admission-01"]
        == _RECEIPT_EXPIRY
    )
    assert (
        expiries[_TABLES.verdicts, "audit-01", "EVALUATION_RESULT#admission-01#evaluator-01"]
        == _RECEIPT_EXPIRY
    )
    assert (
        expiries[_TABLES.verdicts, "audit-02", "EVALUATION_RESULT#admission-02#evaluator-01"]
        == _ORPHAN_EXPIRY
    )
    assert counts["family_record"] == {
        "applied": 4,
        "already_had_expiry": 0,
        "orphan": 1,
        "failed": 0,
    }
    # One strong receipt read per partition, however many items share it.
    assert len(client.of("GetItem")) == 2


def test_an_item_that_already_has_an_expiry_is_counted_and_never_written() -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                {**_receipt("audit-01", {"created_at": _AT}), "expires_at": {"N": "1"}}
            ],
            _TABLES.findings: [
                _item("audit-01", "FINDING#finding-01", expires_at={"N": _RECEIPT_EXPIRY})
            ],
        }
    )

    counts = _counts(client, _plan())

    assert client.of("UpdateItem") == []
    assert counts["receipt"]["already_had_expiry"] == 1
    assert counts["family_record"]["already_had_expiry"] == 1


def test_a_condition_failure_on_an_item_a_writer_stamped_meanwhile_is_already_expiring() -> None:
    client = _RecordingDynamo(
        tables={_TABLES.receipts: [_receipt("audit-01", {"created_at": _AT})]},
        update_failures={
            ("audit-01", "RECORD"): _conditional_failure({"expires_at": {"N": _RECEIPT_EXPIRY}})
        },
    )

    assert _counts(client, _plan())["receipt"] == {
        "applied": 0,
        "already_had_expiry": 1,
        "orphan": 0,
        "failed": 0,
    }


@pytest.mark.parametrize(
    "failure",
    [
        _conditional_failure(None),
        ClientError(
            cast("Any", {"Error": {"Code": "ProvisionedThroughputExceededException"}}), "x"
        ),
    ],
    ids=["item-gone", "throttled"],
)
def test_any_other_update_failure_is_counted_failed_and_the_run_continues(
    failure: Exception,
) -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                _receipt("audit-01", {"created_at": _AT}),
                _receipt("audit-02", {"created_at": _AT}),
            ]
        },
        update_failures={("audit-01", "RECORD"): failure},
    )

    counts = _counts(client, _plan())

    assert counts["receipt"]["failed"] == 1
    assert counts["receipt"]["applied"] == 1


def test_an_unrecognized_receipts_table_key_is_failed_and_not_written() -> None:
    client = _RecordingDynamo(tables={_TABLES.receipts: [_item("audit-01", "SOMETHING#else")]})

    assert _counts(client, _plan())["unrecognized"]["failed"] == 1
    assert client.of("UpdateItem") == []


def test_every_table_is_scanned_to_its_last_page_with_a_projection() -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                _receipt(f"audit-{index}", {"created_at": _AT}) for index in range(5)
            ]
        }
    )

    counts = _counts(client, _plan())

    assert counts["receipt"]["applied"] == 5
    scans = client.of("Scan")
    assert [scan["TableName"] for scan in scans] == [
        *[_TABLES.receipts] * 3,
        _TABLES.findings,
        _TABLES.deliveries,
        _TABLES.writer_receipts,
        _TABLES.verdicts,
    ]
    assert scans[0] == {
        "TableName": _TABLES.receipts,
        "ProjectionExpression": "#audit, #key, #expiry, #payload",
        "ExpressionAttributeNames": {
            "#audit": "audit_id",
            "#key": "item_key",
            "#expiry": "expires_at",
            "#payload": "payload",
        },
    }
    # Only a receipt's own payload is read; the family tables are scanned for keys and expiry.
    for scan in scans[3:]:
        assert scan == {
            "TableName": scan["TableName"],
            "ProjectionExpression": "#audit, #key, #expiry",
            "ExpressionAttributeNames": {
                "#audit": "audit_id",
                "#key": "item_key",
                "#expiry": "expires_at",
            },
        }


def test_a_dry_run_reads_and_counts_but_writes_nothing() -> None:
    client = _RecordingDynamo(
        tables={
            _TABLES.receipts: [
                _receipt("audit-01", {"created_at": _AT}),
                _item("audit-01", "DELIVERY"),
                _item("audit-01", "FINDING#finding-01"),
            ]
        }
    )

    counts = _counts(client, _plan(dry_run=True))

    assert client.of("UpdateItem") == []
    assert counts["receipt"]["applied"] == 1
    assert counts["receipt_companion"]["applied"] == 1
    assert counts["moved_family_legacy"]["applied"] == 1


# --- The operator entry point: explicit inputs, STS first, counts-only output ------------------

_ARGS = [
    "--profile",
    "operator-test",
    "--environment",
    "dev",
    "--region",
    "us-east-1",
    "--expected-account",
    "111122223333",
    "--retention-days",
    "90",
    "--legacy-margin-hours",
    "48",
]


@dataclass
class _Session:
    dynamodb: _RecordingDynamo
    account: str = "111122223333"
    created: list[str] = field(default_factory=list[str])
    parameters: list[str] = field(default_factory=list[str])
    functions: list[str] = field(default_factory=list[str])
    deployed_variables: dict[str, str] = field(
        default_factory=lambda: {"ADL_RECEIPT_RETENTION_DAYS": "90", "ADL_ENVIRONMENT": "dev"}
    )

    def client(self, service: str) -> Any:
        self.created.append(service)
        if service == "sts":
            return _Sts(self.account)
        if service == "lambda":
            return _Lambda(self.functions, self.deployed_variables)
        if service == "ssm":
            return _Ssm(self.parameters)
        assert service == "dynamodb"
        return self.dynamodb


@dataclass
class _Lambda:
    names: list[str]
    variables: dict[str, str]

    def get_function_configuration(self, **request: str) -> dict[str, object]:
        self.names.append(request["FunctionName"])
        return {
            "FunctionName": request["FunctionName"],
            "Environment": {"Variables": self.variables},
        }


@dataclass
class _Sts:
    account: str

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self.account}


@dataclass
class _Ssm:
    names: list[str]

    def get_parameter(self, **request: str) -> dict[str, dict[str, str]]:
        self.names.append(request["Name"])
        return {"Parameter": {"Value": request["Name"].rsplit("/", 1)[-1]}}


def _main(session: _Session, *extra: str) -> int:
    def factory(**kwargs: str) -> _Session:
        assert kwargs == {"profile_name": "operator-test", "region_name": "us-east-1"}
        return session

    return main([*_ARGS, *extra], session_factory=factory, clock=lambda: _BACKFILL_AT)


def test_the_entry_point_refuses_an_unexpected_account_before_any_table_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _Session(_RecordingDynamo(), account="444455556666")

    assert _main(session) == 1

    assert session.created == ["sts"]
    output = capsys.readouterr().out
    assert json.loads(output) == {"stage": "identity", "error_class": "UnexpectedAccount"}
    assert "444455556666" not in output


def test_the_entry_point_resolves_the_five_published_table_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _Session(_RecordingDynamo())

    assert _main(session) == 0

    assert session.created == ["sts", "lambda", "ssm", "dynamodb"]
    assert session.parameters == [
        "/adlytics/dev/agent_quality/audit-table",
        "/adlytics/dev/agent_quality/findings-table",
        "/adlytics/dev/evaluation/deliveries-table",
        "/adlytics/dev/evaluation/writer-receipts-table",
        "/adlytics/dev/evaluation/verdicts-table",
    ]
    assert json.loads(capsys.readouterr().out)["dry_run"] is False


def test_the_entry_point_prints_counts_only_never_identifiers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _RecordingDynamo(
        tables={
            "audit-table": [
                _receipt("audit-secret-01", {"created_at": _AT}),
                _item("audit-secret-01", "EVALUATION_ADMISSION#admission-secret"),
            ]
        }
    )

    assert _main(_Session(client), "--dry-run") == 0

    output = capsys.readouterr().out
    assert client.of("UpdateItem") == []
    for secret in ("audit-secret-01", "admission-secret", _AT, "audit-table"):
        assert secret not in output
    document = json.loads(output)
    assert document["dry_run"] is True
    assert document["counts"]["receipt"] == {
        "applied": 1,
        "already_had_expiry": 0,
        "orphan": 0,
        "failed": 0,
    }


def test_the_entry_point_prints_the_moved_family_expiry_as_a_utc_instant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The resource policies must land before this instant; printing it makes that checkable."""
    assert _main(_Session(_RecordingDynamo()), "--dry-run") == 0

    document = json.loads(capsys.readouterr().out)
    # The backfill time plus the 48-hour margin, in UTC: the instant `_LEGACY_EXPIRY` encodes.
    assert document["moved_family_legacy_expires_at"] == "2026-09-30T09:30:00+00:00"
    assert datetime.fromisoformat(document["moved_family_legacy_expires_at"]).timestamp() == int(
        _LEGACY_EXPIRY
    )


def test_the_entry_point_reads_the_deployed_retention_from_the_conversation_function() -> None:
    session = _Session(_RecordingDynamo())

    assert _main(session, "--dry-run") == 0

    assert session.functions == ["adlytics-dev-ConversationFunction"]


@pytest.mark.parametrize(
    "variables",
    [
        {"ADL_RECEIPT_RETENTION_DAYS": "30"},
        {"ADL_RECEIPT_RETENTION_DAYS": "900"},
    ],
    ids=["shorter", "longer"],
)
def test_a_retention_that_differs_from_the_deployed_one_is_refused_before_any_table(
    variables: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the policies land, a receipt's expiry can never be corrected, so a typo must stop."""
    client = _RecordingDynamo(tables={"audit-table": [_receipt("audit-01", {"created_at": _AT})]})
    session = _Session(client, deployed_variables=variables)

    assert _main(session) == 1

    assert session.created == ["sts", "lambda"]
    assert client.of("Scan") == []
    assert client.of("UpdateItem") == []
    assert json.loads(capsys.readouterr().out) == {
        "stage": "retention",
        "error_class": "RetentionMismatch",
    }


@pytest.mark.parametrize(
    "variables",
    [{}, {"ADL_RECEIPT_RETENTION_DAYS": ""}, {"ADL_RECEIPT_RETENTION_DAYS": "90 days"}],
    ids=["absent", "blank", "malformed"],
)
def test_an_unreadable_deployed_retention_is_refused_before_any_table(
    variables: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    session = _Session(_RecordingDynamo(), deployed_variables=variables)

    assert _main(session) == 1

    assert session.created == ["sts", "lambda"]
    assert json.loads(capsys.readouterr().out) == {
        "stage": "retention",
        "error_class": "ValueError",
    }


def test_the_entry_point_exits_nonzero_when_any_item_failed() -> None:
    client = _RecordingDynamo(tables={"audit-table": [_receipt("audit-01", "not json")]})

    assert _main(_Session(client)) == 1


@pytest.mark.parametrize(
    "omitted",
    [
        "--profile",
        "--environment",
        "--region",
        "--expected-account",
        "--retention-days",
        "--legacy-margin-hours",
    ],
)
def test_every_target_affecting_input_is_required(omitted: str) -> None:
    index = _ARGS.index(omitted)
    argv = _ARGS[:index] + _ARGS[index + 2 :]

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--profile", "default"),
        ("--expected-account", "12345"),
        ("--retention-days", "0"),
        ("--retention-days", "90.5"),
        ("--legacy-margin-hours", "0"),
        ("--legacy-margin-hours", "2161"),
    ],
)
def test_a_malformed_input_is_refused_before_any_client(flag: str, value: str) -> None:
    argv = list(_ARGS)
    argv[argv.index(flag) + 1] = value

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
