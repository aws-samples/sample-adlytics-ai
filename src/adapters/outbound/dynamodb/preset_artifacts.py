# SPDX-License-Identifier: MIT-0
"""DynamoDB history and forward-only pointers for shared preset artifacts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

import boto3
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.items import ItemError, read_int, read_text, write_int, write_text
from domain.operations.generated_execution_graphs import STORE
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceOutcome,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPointerAdvanceResult,
    PresetArtifactPointerRosterRequest,
    PresetArtifactPointerRosterResult,
    PresetArtifactRevision,
    PresetArtifactVariant,
    classify_conditional_pointer_failure,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.preset_artifacts import (
    LegacyPointerAfterCutoverError,
    PresetArtifactPointerStorePort,
)

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )

__all__ = [
    "DynamoPresetArtifactPointerStore",
    "pointer_candidate_item",
    "pointer_from_candidate_item",
    "pointer_transaction_items",
]

PARTITION_KEY = "page_variant_key"
RECORD_KEY = "record_key"
RECORD_TYPE = "record_type"
CURRENT_RECORD = "CURRENT"
REVISION_PREFIX = "REVISION#"
POINTER_RECORD_TYPE = "preset_artifact_pointer"
PAGE = "page"
VARIANT = "variant"
REVISION = "revision"
BODY_KEY = "body_key"
BODY_DIGEST = "body_digest"
CUSTODY_DIGEST = "custody_digest"
# A-PROTOCOL-CONSTANT-001: a DynamoDB attribute name; see docs/security-dispositions.md
FORWARD_TOKEN = "forward_token"  # noqa: S105  # nosec B105
SUPERSEDES = "supersedes"
RESOLVED_START = "resolved_window_start"
RESOLVED_END = "resolved_window_end"
COMPARISON_START = "comparison_window_start"
COMPARISON_END = "comparison_window_end"
EXPIRES_AT = "expires_at"
GRANT_ID = "grant_id"
SERVING_GENERATION = "serving_generation"
WRITTEN_AT = "written_at"
_GRAPH_FIELDS = (GRANT_ID, SERVING_GENERATION, WRITTEN_AT)
CONDITIONAL = "ConditionalCheckFailed"
TRANSACTION_CANCELLED = "TransactionCanceledException"
EXPECTED_ATTESTATION_ITEMS = 2
_GRAPH_MODE_KEY = {
    "pk": {"S": str(STORE["mode_key"]["pk"])},
    "sk": {"S": str(STORE["mode_key"]["sk"])},
}

PRESET_ARTIFACT_POINTER_UNREADABLE = ErrorCode("PRESET_ARTIFACT_POINTER", 1)
PRESET_ARTIFACT_POINTER_UNWRITABLE = ErrorCode("PRESET_ARTIFACT_POINTER", 2)
PRESET_ARTIFACT_POINTER_MALFORMED = ErrorCode("PRESET_ARTIFACT_POINTER", 3)


def pointer_transaction_items(
    table: str,
    request: PresetArtifactPointerAdvanceRequest,
    graph_table: str | None = None,
) -> tuple[TransactWriteItemTypeDef, ...]:
    """Build the history/current transaction whose conditions enforce monotonicity.

    S6 §2.9 (AGG3-6): where the graph authority is provisioned, every grant-less (legacy)
    pointer write also ConditionChecks the graph mode item `mode <> GRAPH`, as its last member,
    so a legacy bake that read the mode before the `GRAPH` CAS cannot write after it. A
    grant-bearing write carries no such member: it exists only in `GRAPH`.
    """
    pointer = request.pointer
    history = _pointer_item(pointer, current=False)
    current = _pointer_item(pointer, current=True)
    current_write: TransactWriteItemTypeDef
    if pointer.supersedes is None:
        current_write = {
            "Put": {
                "TableName": table,
                "Item": current,
                "ConditionExpression": f"attribute_not_exists({RECORD_KEY})",
            }
        }
    else:
        current_write = {
            "Put": {
                "TableName": table,
                "Item": current,
                "ConditionExpression": f"{REVISION} = :supersedes AND {FORWARD_TOKEN} < :token",
                "ExpressionAttributeValues": {
                    ":supersedes": write_text(pointer.supersedes.value),
                    ":token": write_int(pointer.forward_token),
                },
            }
        }
    items: list[TransactWriteItemTypeDef] = [
        {
            "Put": {
                "TableName": table,
                "Item": history,
                "ConditionExpression": (
                    f"attribute_not_exists({PARTITION_KEY}) AND attribute_not_exists({RECORD_KEY})"
                ),
            }
        },
        current_write,
    ]
    if pointer.supersedes is not None:
        expires_at = cast("datetime", request.superseded_history_expires_at)
        items.append(
            {
                "Update": {
                    "TableName": table,
                    "Key": {
                        PARTITION_KEY: write_text(pointer.partition_key),
                        RECORD_KEY: write_text(f"{REVISION_PREFIX}{pointer.supersedes.value}"),
                    },
                    "UpdateExpression": f"SET {EXPIRES_AT} = :expires",
                    "ConditionExpression": f"{REVISION} = :supersedes",
                    "ExpressionAttributeValues": {
                        ":expires": write_int(int(expires_at.astimezone(UTC).timestamp())),
                        ":supersedes": write_text(pointer.supersedes.value),
                    },
                }
            }
        )
    if graph_table is not None and pointer.grant_id is None:
        items.append(
            {
                "ConditionCheck": {
                    "TableName": graph_table,
                    "Key": cast("dict[str, AttributeValueTypeDef]", dict(_GRAPH_MODE_KEY)),
                    "ConditionExpression": "attribute_not_exists(pk) OR #mode <> :graph",
                    "ExpressionAttributeNames": {"#mode": "mode"},
                    "ExpressionAttributeValues": {":graph": write_text("GRAPH")},
                }
            }
        )
    return tuple(items)


class DynamoPresetArtifactPointerStore(PresetArtifactPointerStorePort):
    """Strongly read shared pointers and atomically preserve immutable revision history."""

    def __init__(
        self,
        table: str,
        client: DynamoDBClient | None = None,
        *,
        graph_table: str | None = None,
    ) -> None:
        if not table.strip():
            raise ValueError("preset artifact pointer table cannot be blank.")
        self._table = table
        self._client = client or boto3.client("dynamodb")
        self._graph_table = graph_table or None
        """S6 §2.9: the graph table whose mode item a legacy pointer write ConditionChecks."""

    def current(
        self, page: PresetArtifactPage, variant: PresetArtifactVariant
    ) -> PresetArtifactPointer | None:
        """Return one strongly consistent current pointer, or its stable absence."""
        request = PresetArtifactPointerRosterRequest(page, (variant,))
        return self.roster(request).pointers[0]

    def roster(
        self, request: PresetArtifactPointerRosterRequest
    ) -> PresetArtifactPointerRosterResult:
        """Strongly read a bounded declared CURRENT roster in one transaction."""
        try:
            response = self._client.transact_get_items(
                TransactItems=[
                    _get_item(
                        self._table,
                        f"{request.page.value}#{variant.value}",
                        CURRENT_RECORD,
                    )
                    for variant in request.variants
                ]
            )
            responses = response.get("Responses", [])
            if len(responses) != len(request.variants):
                raise ValueError("preset artifact pointer roster response is incomplete.")
            items = [response_item.get("Item") for response_item in responses]
            pointers = tuple(
                None if item is None else _pointer_from_item(dict(item), current=True)
                for item in items
            )
            return PresetArtifactPointerRosterResult(request, pointers)
        except InvariantError:
            raise
        except (ItemError, KeyError, TypeError, ValueError) as error:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_MALFORMED,
                "Persisted preset artifact pointer roster is malformed.",
                context={"cause": type(error).__name__},
            ) from None
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_POINTER_UNREADABLE,
                "Preset artifact pointer roster could not be read.",
                context={"cause": type(error).__name__},
            ) from None

    def advance(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        """Write body-attesting history and advance CURRENT only through its exact predecessor."""
        items = pointer_transaction_items(self._table, request, self._graph_table)
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as error:
            if _mode_check_refused(error, items):
                raise LegacyPointerAfterCutoverError(request.pointer.partition_key) from None
            if not _is_conditional_transaction(error):
                raise DependencyError(
                    PRESET_ARTIFACT_POINTER_UNWRITABLE,
                    "Preset artifact pointer could not be advanced.",
                    context={"cause": str(error.response.get("Error", {}).get("Code", ""))},
                ) from None
            return self._attest_conditional_failure(request)
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_POINTER_UNWRITABLE,
                "Preset artifact pointer could not be advanced.",
                context={"cause": type(error).__name__},
            ) from None
        return PresetArtifactPointerAdvanceResult(PresetArtifactPointerAdvanceOutcome.ADVANCED)

    def _attest_conditional_failure(
        self, request: PresetArtifactPointerAdvanceRequest
    ) -> PresetArtifactPointerAdvanceResult:
        desired = request.pointer
        revision_record = f"{REVISION_PREFIX}{desired.revision.value}"
        try:
            response = self._client.transact_get_items(
                TransactItems=[
                    _get_item(self._table, desired.partition_key, revision_record),
                    _get_item(self._table, desired.partition_key, CURRENT_RECORD),
                ]
            )
            responses = response.get("Responses", [])
            if len(responses) != EXPECTED_ATTESTATION_ITEMS:
                raise ValueError("conditional attestation response is malformed.")
            history_item = responses[0].get("Item")
            current_item = responses[1].get("Item")
            history = (
                None
                if history_item is None
                else _pointer_from_item(dict(history_item), current=False)
            )
            current = (
                None
                if current_item is None
                else _pointer_from_item(dict(current_item), current=True)
            )
        except InvariantError:
            raise
        except (ItemError, TypeError, ValueError, KeyError) as error:
            raise InvariantError(
                PRESET_ARTIFACT_POINTER_MALFORMED,
                "Persisted preset artifact pointer bytes are malformed.",
                context={"cause": type(error).__name__},
            ) from None
        except Exception as error:
            raise DependencyError(
                PRESET_ARTIFACT_POINTER_UNREADABLE,
                "Conditional preset artifact pointer state could not be attested.",
                context={"cause": type(error).__name__},
            ) from None
        return classify_conditional_pointer_failure(request, history, current)


def _pointer_item(
    pointer: PresetArtifactPointer, *, current: bool
) -> dict[str, AttributeValueTypeDef]:
    record_key = CURRENT_RECORD if current else f"{REVISION_PREFIX}{pointer.revision.value}"
    item: dict[str, AttributeValueTypeDef] = {
        PARTITION_KEY: write_text(pointer.partition_key),
        RECORD_KEY: write_text(record_key),
        RECORD_TYPE: write_text(POINTER_RECORD_TYPE),
        PAGE: write_text(pointer.page.value),
        VARIANT: write_text(pointer.variant.value),
        REVISION: write_text(pointer.revision.value),
        BODY_KEY: write_text(pointer.body_key),
        BODY_DIGEST: write_text(pointer.body_digest),
        FORWARD_TOKEN: write_int(pointer.forward_token),
    }
    if pointer.supersedes is not None:
        item[SUPERSEDES] = write_text(pointer.supersedes.value)
    if pointer.custody_digest is not None:
        item[CUSTODY_DIGEST] = write_text(pointer.custody_digest)
    # S6 §2.8 (D28(a)): each graph field only when set, so a legacy item stays today's envelope.
    for name, value in (
        (GRANT_ID, pointer.grant_id),
        (SERVING_GENERATION, pointer.serving_generation),
        (WRITTEN_AT, pointer.written_at),
    ):
        if value is not None:
            item[name] = write_text(value)
    # ADR-116 decision 4: the served windows, capability-free; absent on an older pointer.
    for window, (start, end) in (
        (pointer.resolved_window, (RESOLVED_START, RESOLVED_END)),
        (pointer.comparison_window, (COMPARISON_START, COMPARISON_END)),
    ):
        if window is not None:
            item[start] = write_text(window.start.isoformat())
            item[end] = write_text(window.end.isoformat())
    return item


def _window_from_item(
    item: dict[str, AttributeValueTypeDef], start: str, end: str
) -> DateRange | None:
    if item.get(start) is None and item.get(end) is None:
        return None
    return DateRange(
        date.fromisoformat(read_text(item, start)), date.fromisoformat(read_text(item, end))
    )


def _pointer_from_item(
    item: dict[str, AttributeValueTypeDef], *, current: bool
) -> PresetArtifactPointer:
    if read_text(item, RECORD_TYPE) != POINTER_RECORD_TYPE:
        raise ValueError("pointer record type is invalid.")
    page = PresetArtifactPage(read_text(item, PAGE))
    variant = PresetArtifactVariant(read_text(item, VARIANT))
    revision = PresetArtifactRevision(read_text(item, REVISION))
    supersedes_raw = item.get(SUPERSEDES)
    supersedes = (
        None if supersedes_raw is None else PresetArtifactRevision(read_text(item, SUPERSEDES))
    )
    custody_digest = None if item.get(CUSTODY_DIGEST) is None else read_text(item, CUSTODY_DIGEST)
    pointer = PresetArtifactPointer(
        page,
        variant,
        revision,
        read_text(item, BODY_DIGEST),
        read_int(item, FORWARD_TOKEN),
        supersedes,
        custody_digest,
        *(None if item.get(name) is None else read_text(item, name) for name in _GRAPH_FIELDS),
        resolved_window=_window_from_item(item, RESOLVED_START, RESOLVED_END),
        comparison_window=_window_from_item(item, COMPARISON_START, COMPARISON_END),
    )
    expected = _pointer_item(pointer, current=current)
    if not current and EXPIRES_AT in item:
        expected[EXPIRES_AT] = write_int(read_int(item, EXPIRES_AT))
    if item != expected:
        raise ValueError("pointer envelope differs from its immutable payload.")
    return pointer


def _get_item(table: str, partition_key: str, record_key: str) -> TransactGetItemTypeDef:
    return {
        "Get": {
            "TableName": table,
            "Key": {
                PARTITION_KEY: write_text(partition_key),
                RECORD_KEY: write_text(record_key),
            },
        }
    }


def _mode_check_refused(error: ClientError, items: tuple[TransactWriteItemTypeDef, ...]) -> bool:
    """Whether the cancellation's last reason is the graph mode `ConditionCheck`'s (AGG3-6)."""
    if not items or "ConditionCheck" not in items[-1]:
        return False
    reasons = error.response.get("CancellationReasons", [])
    return len(reasons) == len(items) and reasons[-1].get("Code") == CONDITIONAL


def _is_conditional_transaction(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == TRANSACTION_CANCELLED and any(
        reason.get("Code") == CONDITIONAL
        for reason in error.response.get("CancellationReasons", [])
    )


def pointer_candidate_item(pointer: PresetArtifactPointer) -> dict[str, AttributeValueTypeDef]:
    """Plan 4.6: the CURRENT envelope a V3 task stores as its pointer candidate (JSON-safe)."""
    return _pointer_item(pointer, current=True)


def pointer_from_candidate_item(item: dict[str, AttributeValueTypeDef]) -> PresetArtifactPointer:
    """Parse a stored pointer candidate; any envelope drift is refused (`ValueError`)."""
    return _pointer_from_item(item, current=True)
