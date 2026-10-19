# SPDX-License-Identifier: MIT-0
r"""``adapters.jobs.backfill_record_expiry``: give every retained record of a turn an expiry.

The one-time step 3 backfill of ADR-080 §5 and ADR-081 §5, run after TTL is enabled on the five
record tables and before their resource policies land. Against a table that already carries the
ADR-080 policy it is refused, dry run included: statement 2 denies its updates to everyone, and the
receipts table denies its scan to every principal but the named readers. It scans the receipts
(audit) table and the four family tables and sets ``expires_at`` on each item that has none:
- a receipt (``RECORD``): its payload's ``created_at`` plus the retention. The payload is read as
  JSON for that one field, never through the full decoder, which some legacy receipts fail;
- a delivery disposition or evaluation admission in the receipts table, and every item in the
  four family tables: its partition receipt's ``created_at`` plus the retention. A partition with
  no readable receipt is an orphan and expires at the backfill time plus the retention;
- a legacy item of a family that has moved out of the receipts table (``FINDING#``,
  ``EVALUATION_DELIVERY#``, ``EVALUATION_WRITER#``, ``EVALUATION_RESULT#``): the backfill time plus
  the declared margin, so TTL removes it only once the resource policy is in force.

Each write is one ``UpdateItem`` conditioned on the item existing and having no expiry, so a rerun
changes nothing and the job can never overwrite an expiry a writer stamped. The caller account is
proved with STS, and ``--retention-days`` is compared with the ``ADL_RECEIPT_RETENTION_DAYS`` the
deployed conversation function holds, before any table is resolved or written; ``--dry-run``
writes nothing. Output is per-kind counts and the moved-family expiry instant only: never an
identifier, a key, or a payload. Any ``failed`` count exits nonzero, and the agent-quality-loop
LLD's upgrade order does not deploy the resource policies until a run reports none.

Usage:
    uv run python -m adapters.jobs.backfill_record_expiry \
        --profile <PROFILE> --environment <ENV> --region <REGION> \
        --expected-account <ACCOUNT_ID> --retention-days <DAYS> \
        --legacy-margin-hours <HOURS> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

import boto3
from botocore.exceptions import ClientError

from adapters.jobs.stage1_operator_bootstrap import parameter_path
from adapters.outbound.dynamodb.audit import (
    AUDIT_ID,
    DELIVERY,
    EVALUATION_ADMISSION_PREFIX,
    EVALUATION_DELIVERY_PREFIX,
    EVALUATION_RESULT_PREFIX,
    EVALUATION_WRITER_PREFIX,
    EXPIRES_AT,
    FINDING_PREFIX,
    ITEM_KEY,
    PAYLOAD,
    RECORD,
)
from domain.conversation.audit import MAX_RECORD_RETENTION_DAYS, RecordRetention

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

__all__ = ["BackfillPlan", "BackfillTables", "Kind", "Outcome", "build_parser", "main", "run"]

_ACCOUNT: Final = re.compile(r"\A\d{12}\Z")
_MAX_LEGACY_MARGIN_HOURS: Final = 24 * 90
_MOVED_FAMILY_PREFIXES: Final = (
    FINDING_PREFIX,
    EVALUATION_DELIVERY_PREFIX,
    EVALUATION_WRITER_PREFIX,
    EVALUATION_RESULT_PREFIX,
)
_TABLE_KEYS: Final = {
    "receipts": "agentQualityAuditTableName",
    "findings": "agentQualityFindingsTableName",
    "deliveries": "evaluationDeliveriesTableName",
    "writer_receipts": "evaluationWriterReceiptsTableName",
    "verdicts": "evaluationVerdictsTableName",
}
_NAMES: Final = {"#audit": AUDIT_ID, "#key": ITEM_KEY, "#expiry": EXPIRES_AT}
RETENTION_VARIABLE: Final = "ADL_RECEIPT_RETENTION_DAYS"


class Kind(StrEnum):
    """What an item is, which decides where its expiry comes from."""

    RECEIPT = "receipt"
    RECEIPT_COMPANION = "receipt_companion"
    MOVED_FAMILY_LEGACY = "moved_family_legacy"
    FAMILY_RECORD = "family_record"
    UNRECOGNIZED = "unrecognized"


class Outcome(StrEnum):
    """The closed result for one item; an orphan is applied with the backfill-time expiry."""

    APPLIED = "applied"
    ALREADY_HAD_EXPIRY = "already_had_expiry"
    ORPHAN = "orphan"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BackfillTables:
    """The five record tables, resolved from the deployment's published parameters."""

    receipts: str
    findings: str
    deliveries: str
    writer_receipts: str
    verdicts: str


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """Everything that decides an expiry, fixed once for the whole run."""

    retention: RecordRetention
    legacy_margin_hours: int
    backfill_at: datetime
    dry_run: bool

    def __post_init__(self) -> None:
        if not 1 <= self.legacy_margin_hours <= _MAX_LEGACY_MARGIN_HOURS:
            raise ValueError("legacy margin hours is out of range.")
        if self.backfill_at.tzinfo is None or self.backfill_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("backfill time must be a UTC instant.")

    @property
    def orphan_expiry(self) -> int:
        """The backfill time plus the retention, for a record whose receipt is unreadable."""
        return self.retention.expires_at(self.backfill_at.isoformat())

    @property
    def legacy_expiry(self) -> int:
        """The backfill time plus the declared margin, in whole epoch seconds."""
        return int(self.backfill_at.timestamp()) + self.legacy_margin_hours * 3600

    @property
    def legacy_expiry_instant(self) -> str:
        """`legacy_expiry` as a UTC ISO 8601 instant."""
        return datetime.fromtimestamp(self.legacy_expiry, UTC).isoformat()


@dataclass(slots=True)
class BackfillCounts:
    """Per-kind outcome counts, the only thing the job reports."""

    counts: Counter[tuple[Kind, Outcome]] = field(default_factory=Counter[tuple[Kind, Outcome]])

    def add(self, kind: Kind, outcome: Outcome) -> None:
        self.counts[kind, outcome] += 1

    @property
    def failed(self) -> int:
        return sum(
            count for (_, outcome), count in self.counts.items() if outcome is Outcome.FAILED
        )

    def document(self, plan: BackfillPlan) -> dict[str, object]:
        """Render every kind with every outcome, zeros included, so absence is stated.

        The moved-family expiry is an instant, not an identifier: the resource policies must be
        deployed before it, and printing it is what lets that order be checked afterwards.
        """
        return {
            "dry_run": plan.dry_run,
            "moved_family_legacy_expires_at": plan.legacy_expiry_instant,
            "counts": {
                kind.value: {outcome.value: self.counts[kind, outcome] for outcome in Outcome}
                for kind in Kind
            },
        }


def _text(item: dict[str, AttributeValueTypeDef], name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return value if isinstance(value, str) else None


def receipt_created_at(item: dict[str, AttributeValueTypeDef]) -> str | None:
    """Read only ``created_at`` from a receipt payload; ``None`` when it cannot be read."""
    payload = _text(item, PAYLOAD)
    if payload is None:
        return None
    try:
        document: object = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    created_at = cast("dict[str, object]", document).get("created_at")
    return created_at if isinstance(created_at, str) else None


def receipts_kind(item_key: str) -> Kind:
    """Classify one receipts-table item by its key alone."""
    if item_key == RECORD:
        return Kind.RECEIPT
    if item_key == DELIVERY or item_key.startswith(EVALUATION_ADMISSION_PREFIX):
        return Kind.RECEIPT_COMPANION
    if item_key.startswith(_MOVED_FAMILY_PREFIXES):
        return Kind.MOVED_FAMILY_LEGACY
    return Kind.UNRECOGNIZED


@dataclass(slots=True)
class _Backfill:
    client: DynamoDBClient
    tables: BackfillTables
    plan: BackfillPlan
    counts: BackfillCounts = field(default_factory=BackfillCounts)
    partition_expiry: dict[str, int | None] = field(default_factory=dict[str, int | None])

    def _scan(
        self, table: str, *, with_payload: bool
    ) -> Iterator[dict[str, AttributeValueTypeDef]]:
        """Scan keys and expiry; only a receipt's own payload is ever read, for `created_at`."""
        start: dict[str, AttributeValueTypeDef] | None = None
        while True:
            request: dict[str, Any] = (
                {
                    "TableName": table,
                    "ProjectionExpression": "#audit, #key, #expiry, #payload",
                    "ExpressionAttributeNames": {**_NAMES, "#payload": PAYLOAD},
                }
                if with_payload
                else {
                    "TableName": table,
                    "ProjectionExpression": "#audit, #key, #expiry",
                    "ExpressionAttributeNames": dict(_NAMES),
                }
            )
            if start is not None:
                request["ExclusiveStartKey"] = start
            response = self.client.scan(**request)
            for item in response.get("Items", []):
                yield dict(item)
            last = response.get("LastEvaluatedKey")
            if last is None:
                return
            start = dict(last)

    def _receipt_expiry(self, audit_id: str) -> int | None:
        """Return the partition receipt's expiry, or ``None`` for an orphan; read it once."""
        if audit_id not in self.partition_expiry:
            response = self.client.get_item(
                TableName=self.tables.receipts,
                Key={AUDIT_ID: {"S": audit_id}, ITEM_KEY: {"S": RECORD}},
                ProjectionExpression="#payload",
                ExpressionAttributeNames={"#payload": PAYLOAD},
                ConsistentRead=True,
            )
            found = response.get("Item")
            created_at = None if found is None else receipt_created_at(dict(found))
            try:
                expiry = None if created_at is None else self.plan.retention.expires_at(created_at)
            except ValueError:
                expiry = None
            self.partition_expiry[audit_id] = expiry
        return self.partition_expiry[audit_id]

    def _update(self, table: str, item: dict[str, AttributeValueTypeDef], expiry: int) -> bool:
        """Set the expiry only on an existing item that has none; report whether it applied."""
        if self.plan.dry_run:
            return True
        try:
            self.client.update_item(
                TableName=table,
                Key={AUDIT_ID: item[AUDIT_ID], ITEM_KEY: item[ITEM_KEY]},
                UpdateExpression="SET #expiry = :expiry",
                ConditionExpression="attribute_exists(#key) AND attribute_not_exists(#expiry)",
                ExpressionAttributeNames={"#key": ITEM_KEY, "#expiry": EXPIRES_AT},
                ExpressionAttributeValues={":expiry": {"N": str(expiry)}},
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            old = cast("dict[str, object]", error.response).get("Item")
            if isinstance(old, dict) and EXPIRES_AT in old:
                return False
            raise
        return True

    def _apply(self, table: str, item: dict[str, AttributeValueTypeDef], kind: Kind) -> None:
        audit_id = _text(item, AUDIT_ID)
        item_key = _text(item, ITEM_KEY)
        if EXPIRES_AT in item:
            self.counts.add(kind, Outcome.ALREADY_HAD_EXPIRY)
            return
        if audit_id is None or item_key is None or kind is Kind.UNRECOGNIZED:
            self.counts.add(kind, Outcome.FAILED)
            return
        outcome = Outcome.APPLIED
        try:
            if kind is Kind.RECEIPT:
                created_at = receipt_created_at(item)
                if created_at is None:
                    self.counts.add(kind, Outcome.FAILED)
                    return
                expiry = self.plan.retention.expires_at(created_at)
            elif kind is Kind.MOVED_FAMILY_LEGACY:
                expiry = self.plan.legacy_expiry
            else:
                receipt_expiry = self._receipt_expiry(audit_id)
                if receipt_expiry is None:
                    outcome, expiry = Outcome.ORPHAN, self.plan.orphan_expiry
                else:
                    expiry = receipt_expiry
            applied = self._update(table, item, expiry)
        # error-propagation: operator CLI; counted FAILED in the printed counts, which exit 1
        except ClientError, ValueError:
            self.counts.add(kind, Outcome.FAILED)
            return
        self.counts.add(kind, outcome if applied else Outcome.ALREADY_HAD_EXPIRY)

    def run(self) -> BackfillCounts:
        for item in self._scan(self.tables.receipts, with_payload=True):
            self._apply(self.tables.receipts, item, receipts_kind(_text(item, ITEM_KEY) or ""))
        for table in (
            self.tables.findings,
            self.tables.deliveries,
            self.tables.writer_receipts,
            self.tables.verdicts,
        ):
            for item in self._scan(table, with_payload=False):
                self._apply(table, item, Kind.FAMILY_RECORD)
        return self.counts


def run(client: DynamoDBClient, tables: BackfillTables, plan: BackfillPlan) -> BackfillCounts:
    """Backfill every table in order: receipts first, then the four family tables."""
    return _Backfill(client, tables, plan).run()


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    normalized = _non_blank(value)
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value)
    if _ACCOUNT.match(normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _whole(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        if re.fullmatch(r"[1-9][0-9]*", value.strip()) is None:
            raise argparse.ArgumentTypeError("value must be a positive whole number")
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    """Every target-affecting input is required; nothing defaults."""
    parser = argparse.ArgumentParser(
        prog="python -m adapters.jobs.backfill_record_expiry",
        description="Give every retained record of a turn its expiry (ADR-080 §5, ADR-081 §5).",
    )
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument(
        "--retention-days",
        required=True,
        type=_whole(1, MAX_RECORD_RETENTION_DAYS),
        help="the deployed ADL_RECEIPT_RETENTION_DAYS; the job refuses any other value",
    )
    parser.add_argument(
        "--legacy-margin-hours",
        required=True,
        type=_whole(1, _MAX_LEGACY_MARGIN_HOURS),
        help="hours after the backfill that moved-family legacy items expire",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _tables(ssm: Any, environment: str) -> BackfillTables:
    values = {
        name: str(
            ssm.get_parameter(Name=parameter_path(environment, key))["Parameter"]["Value"]
        ).strip()
        for name, key in _TABLE_KEYS.items()
    }
    if not all(values.values()):
        raise ValueError("a record table parameter is empty.")
    return BackfillTables(**values)


def conversation_function_name(environment: str) -> str:
    """Name the receipts writer as `conversationFunctionName` in infra/lib/config fixes it."""
    return f"adlytics-{environment}-ConversationFunction"


def deployed_retention_days(lambda_client: Any, environment: str) -> int:
    """Read the retention the receipts writer stamps, from its deployed configuration.

    The receipts table's writer is the conversation function, so its value is the one every
    other writer's records must agree with. Absent or malformed refuses.
    """
    configuration = lambda_client.get_function_configuration(
        FunctionName=conversation_function_name(environment)
    )
    variables: object = configuration.get("Environment", {}).get("Variables", {})
    value = (
        cast("dict[str, object]", variables).get(RETENTION_VARIABLE)
        if isinstance(variables, dict)
        else None
    )
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("the deployed receipt retention is absent or malformed.")
    return int(value)


def _print(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True, indent=1))  # noqa: T201


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Validate, prove the account, resolve the five tables, backfill, and print counts."""
    args = build_parser().parse_args(argv)
    plan = BackfillPlan(
        retention=RecordRetention(args.retention_days),
        legacy_margin_hours=args.legacy_margin_hours,
        backfill_at=clock(),
        dry_run=args.dry_run,
    )
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    # error-propagation: operator CLI; the printed identity-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "identity", "error_class": type(error).__name__})
        return 1
    if account != args.expected_account:
        _print({"stage": "identity", "error_class": "UnexpectedAccount"})
        return 1
    # A typed retention that differs from the deployed one would give receipts an expiry that
    # disagrees with their companions', and once the policies land it can never be corrected.
    try:
        deployed = deployed_retention_days(session.client("lambda"), args.environment)
    # error-propagation: operator CLI; the printed retention-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "retention", "error_class": type(error).__name__})
        return 1
    if deployed != args.retention_days:
        _print({"stage": "retention", "error_class": "RetentionMismatch"})
        return 1
    try:
        tables = _tables(session.client("ssm"), args.environment)
        counts = run(session.client("dynamodb"), tables, plan)
    # error-propagation: operator CLI; the printed backfill-stage error_class and exit 1
    except Exception as error:
        _print({"stage": "backfill", "error_class": type(error).__name__})
        return 1
    _print(counts.document(plan))
    return 1 if counts.failed else 0


if __name__ == "__main__":
    sys.exit(main())
