# SPDX-License-Identifier: MIT-0
"""Conditional DynamoDB persistence for operational registration finalization state."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from domain.producer.status import (
    FinalizedRegistration,
    RegistrationAttempt,
    RegistrationRejection,
    RegistrationState,
    RegistrationStatus,
    RegistrationWriteOutcome,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

STATUS_UNREADABLE = ErrorCode("PRODUCER_STATUS", 1)
STATUS_UNWRITABLE = ErrorCode("PRODUCER_STATUS", 2)
CONDITIONAL = "ConditionalCheckFailedException"
ATTEMPT_ID = "attempt_id"


def _text(value: str) -> AttributeValueTypeDef:
    return {"S": value}


def _read(item: dict[str, AttributeValueTypeDef], key: str) -> str:
    value = item.get(key, {}).get("S")
    if not isinstance(value, str) or not value:
        raise ValueError(f"registration status item requires text attribute {key!r}.")
    return value


def _conditional(error: Exception) -> bool:
    return (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code") == CONDITIONAL
    )


def _status_from(item: dict[str, AttributeValueTypeDef]) -> RegistrationStatus:
    attempt = RegistrationAttempt(
        registration_key=_read(item, "registration_key"),
        registration_version_id=_read(item, "registration_version_id"),
    )
    state = RegistrationState(_read(item, "state"))
    updated_at = datetime.fromisoformat(_read(item, "updated_at"))
    if state is RegistrationState.PROCESSING:
        return RegistrationStatus.processing(attempt, at=updated_at)
    if state is RegistrationState.REJECTED:
        return RegistrationStatus.rejected(
            attempt,
            RegistrationRejection(
                _read(item, "rejection_code"),
                _read(item, "rejection_detail"),
            ),
            at=updated_at,
        )
    return RegistrationStatus.finalized(
        attempt,
        FinalizedRegistration(
            result_key=_read(item, "result_key"),
            result_version_id=_read(item, "result_version_id"),
            manifest_key=_read(item, "manifest_key"),
            manifest_version_id=_read(item, "manifest_version_id"),
        ),
        at=updated_at,
    )


class DynamoRegistrationStatus:
    """Strong exact-attempt reads and conditional processing-to-terminal transitions."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        if not table.strip():
            raise ValueError("registration status table cannot be blank.")
        self._table = table
        self._client = client or boto3.client("dynamodb")

    def get(self, attempt: RegistrationAttempt) -> RegistrationStatus | None:
        """Strongly read one exact registration attempt."""
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={ATTEMPT_ID: _text(attempt.attempt_id)},
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                STATUS_UNREADABLE,
                "Registration finalization state could not be read.",
                context={"cause": type(error).__name__, "attempt_id": attempt.attempt_id},
            ) from None
        item = response.get("Item")
        return None if item is None else _status_from(dict(item))

    def record(self, status: RegistrationStatus) -> RegistrationWriteOutcome:
        """Create processing once, or transition that exact attempt to a terminal decision."""
        try:
            if status.state is RegistrationState.PROCESSING:
                self._client.put_item(
                    TableName=self._table,
                    Item={
                        ATTEMPT_ID: _text(status.attempt.attempt_id),
                        "registration_key": _text(status.attempt.registration_key),
                        "registration_version_id": _text(status.attempt.registration_version_id),
                        "state": _text(status.state.value),
                        "updated_at": _text(status.updated_at.isoformat()),
                    },
                    ConditionExpression=f"attribute_not_exists({ATTEMPT_ID})",
                )
            else:
                names = {"#state": "state"}
                values: dict[str, AttributeValueTypeDef] = {
                    ":processing": _text(RegistrationState.PROCESSING.value),
                    ":terminal": _text(status.state.value),
                    ":updated": _text(status.updated_at.isoformat()),
                    ":registration_key": _text(status.attempt.registration_key),
                    ":registration_version": _text(status.attempt.registration_version_id),
                }
                assignments = ["#state = :terminal", "updated_at = :updated"]
                if status.state is RegistrationState.FINALIZED and status.output is not None:
                    for name, value in (
                        ("result_key", status.output.result_key),
                        ("result_version_id", status.output.result_version_id),
                        ("manifest_key", status.output.manifest_key),
                        ("manifest_version_id", status.output.manifest_version_id),
                    ):
                        values[f":{name}"] = _text(value)
                        assignments.append(f"{name} = :{name}")
                else:
                    values[":rejection_code"] = _text(status.rejection_code or "INVALID")
                    values[":rejection_detail"] = _text(
                        status.rejection_detail or "Invalid registration."
                    )
                    assignments.extend(
                        [
                            "rejection_code = :rejection_code",
                            "rejection_detail = :rejection_detail",
                        ]
                    )
                self._client.update_item(
                    TableName=self._table,
                    Key={ATTEMPT_ID: _text(status.attempt.attempt_id)},
                    UpdateExpression="SET " + ", ".join(assignments),
                    ConditionExpression=(
                        "#state = :processing AND registration_key = :registration_key AND "
                        "registration_version_id = :registration_version"
                    ),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
        except Exception as error:
            if _conditional(error):
                existing = self.get(status.attempt)
                if existing is not None:
                    return RegistrationWriteOutcome(
                        accepted=existing == status,
                        status=existing,
                        reason=(
                            None if existing == status else "attempt already has another state"
                        ),
                    )
            raise DependencyError(
                STATUS_UNWRITABLE,
                "Registration finalization state could not be written.",
                context={"cause": type(error).__name__, "attempt_id": status.attempt.attempt_id},
            ) from None
        return RegistrationWriteOutcome(accepted=True, status=status)
