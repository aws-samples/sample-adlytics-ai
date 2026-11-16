# SPDX-License-Identifier: MIT-0
"""The demand-forecast table: generation items, the fenced `CURRENT` pointer, receipts (§6b).

Key layout (one partition key per `ASIN#` and `PAGE#` item, R5-08):

| PK | SK |
|---|---|
| `SET#<set_id>` | `CURRENT`, `RUN#<run_id>#r<n>#<step>` |
| `GEN#<generation_id>` | `META` |
| `GEN#<generation_id>#ASIN#<display_asin>` | `ITEM` |
| `GEN#<generation_id>#PAGE#<filter>#<page, 5 digits>` | `ITEM` |

Choices worth stating:
- Nested content (an `ASIN#` item's candidate, a page's entries, `META.first_page`) is stored as
  DynamoDB maps and lists of the boundary model's JSON form, never as a JSON string, as
  `items.py` argues. Every generation item carries `item_sha256`, the SHA-256 of its canonical
  JSON, so a replay is recognised by one attribute.
- `item_size()` is the shared byte oracle's conservative figure (at or above the service's
  count), checked against `item_max_bytes` for every item of a call before any is written.
- Generation items are written in `WRITE_LANES` parallel conditional puts. Lanes 4 is policy,
  not a derived limit: INTERIM, removal item RB-1 re-derives it from P2-2's measured throttles.
  It limits throughput only.
- After a cancelled transaction the adapter re-reads with `ConsistentRead`, rather than relying
  on `ReturnValuesOnConditionCheckFailure`, so one read path serves every case.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from adapters.outbound.dynamodb.byte_oracle import Stage1DynamoByteOracle
from adapters.outbound.dynamodb.items import ItemError, read_int, read_text
from domain.forecasting.list_pages import ListPage
from domain.forecasting.publish import (
    CurrentPointer,
    Disposition,
    GenerationRecord,
    LastAttempt,
    ReceiptRecord,
    RunOutcome,
)
from ports.outbound.forecast_store import (
    Attestation,
    CommitDisposition,
    ForecastItemTooLargeError,
    ForecastStoreConflictError,
    ForecastStorePort,
    ForecastStoreThrottled,
    GenerationMeta,
    GenerationSeries,
    OutcomeCommitRequest,
    OutcomeCommitResult,
    RunReceipt,
    StoreWriteResult,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

PK: Final = "PK"
SK: Final = "SK"
CURRENT_SK: Final = "CURRENT"
META_SK: Final = "META"
ITEM_SK: Final = "ITEM"
ITEM_SHA256: Final = "item_sha256"
PAGE_DIGITS: Final = 5
MAX_PAGE: Final = 10**PAGE_DIGITS - 1

WRITE_LANES: Final = 4
"""INTERIM (RB-1): parallel conditional puts per call; limits throughput, refuses no data."""

STORE_CLIENT_CONFIG: Final = Config(retries={"mode": "adaptive", "max_attempts": 10})
"""Adaptive client retries (§6b); a throttle past them raises `ForecastStoreThrottled`."""

TOKEN_HEX_CHARS: Final = 36
"""`ClientRequestToken`'s maximum length (API_TransactWriteItems)."""

_THROTTLE_CODES: Final = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TransactionInProgressException",
    }
)
_THROTTLE_REASONS: Final = frozenset(
    {"ThrottlingError", "ProvisionedThroughputExceeded", "TransactionConflict"}
)
"""Cancellation reasons that a later attempt can clear (API_TransactWriteItems, read 2026-11-16)."""
_CONDITION_FAILED: Final = "ConditionalCheckFailed"

_ORACLE: Final = Stage1DynamoByteOracle()
_SERIALIZER: Final = TypeSerializer()

Item = dict[str, "AttributeValueTypeDef"]


# --- Keys and items (pure) -------------------------------------------------------------------


def set_key(set_id: str) -> str:
    """Return the set's partition key."""
    return f"SET#{set_id}"


def meta_key(generation: str) -> str:
    """Return `META`'s partition key."""
    return f"GEN#{generation}"


def asin_key(generation: str, display_asin: str) -> str:
    """Return one `ASIN#` item's partition key."""
    return f"GEN#{generation}#ASIN#{display_asin}"


def page_key(generation: str, filter_name: str, page: int) -> str:
    """Return one `PAGE#` item's partition key; the page has at most five digits."""
    if not 1 <= page <= MAX_PAGE:
        raise ValueError(f"page {page} is outside 1 to {MAX_PAGE}")
    return f"GEN#{generation}#PAGE#{filter_name}#{page:0{PAGE_DIGITS}d}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sealed(item: dict[str, object]) -> dict[str, object]:
    """Add `item_sha256`, the digest an exact replay is recognised by."""
    digest = hashlib.sha256(_canonical(item).encode("utf-8")).hexdigest()
    return {**item, ITEM_SHA256: digest}


def asin_item(generation: str, series: GenerationSeries) -> dict[str, object]:
    """Return one product's `ASIN#` item: its candidate and display title."""
    candidate = series.candidate
    return _sealed(
        {
            PK: asin_key(generation, candidate.display_asin),
            SK: ITEM_SK,
            "generation_id": generation,
            "canonical_asin": candidate.canonical_asin,
            "title": series.title,
            "title_truncated": series.title_truncated,
            "series": candidate.model_dump(mode="json"),
        }
    )


def page_item(generation: str, page: ListPage) -> dict[str, object]:
    """Return one filter's page of ranked entries."""
    return _sealed(
        {
            PK: page_key(generation, page.filter, page.page),
            SK: ITEM_SK,
            "generation_id": generation,
            "filter": page.filter,
            "page": page.page,
            "entries": [entry.model_dump(mode="json") for entry in page.entries],
        }
    )


def meta_item(meta: GenerationMeta) -> dict[str, object]:
    """Return the generation's `META` item."""
    generation = meta.generation_id
    return _sealed(
        {
            PK: meta_key(generation),
            SK: META_SK,
            "generation_id": generation,
            "set_id": meta.set_id,
            "run_id": meta.run_id,
            "receipt_sk": meta.receipt_sk,
            "input_end": meta.input_end.isoformat(),
            "first_forecast_week": meta.first_forecast_week.isoformat(),
            "run_fingerprint": meta.run_fingerprint,
            "run_identity": meta.run_identity.model_dump(mode="json"),
            "attempts": [
                {
                    "shard": result.shard,
                    "attempt_id": result.attempt_id,
                    "series_sha256": result.series_sha256,
                    "item_count": result.item_count,
                    "roster_part_sha256": result.roster_part_sha256,
                }
                for result in sorted(meta.attempts, key=lambda result: result.shard)
            ],
            "series_sha256": meta.series_sha256,
            "item_count": meta.item_count,
            "currency_codes": sorted(meta.currency_codes),
            "counts_by_reason": dict(meta.counts_by_reason),
            "counts_by_model": dict(meta.counts_by_model),
            "first_page": [entry.model_dump(mode="json") for entry in meta.pages.first_page],
            "pages": {
                name: {"page_count": counts.page_count, "entry_count": counts.entry_count}
                for name, counts in meta.pages.index.items()
            },
        }
    )


def _last_attempt(attempt: LastAttempt) -> dict[str, object]:
    return {
        "run_id": attempt.run_id,
        "attempt_id": attempt.attempt_id,
        "run_fingerprint": attempt.run_fingerprint,
        "outcome": attempt.outcome.value,
        "failure_category": attempt.failure_category,
        "retry_disposition": attempt.retry_disposition.value,
        "transient_retries": attempt.transient_retries,
        "at": _moment(attempt.at),
    }


def receipt_item(set_id: str, receipt: RunReceipt) -> dict[str, object]:
    """Return one `RUN#` receipt; an absent optional fact is left out, never stored as null."""
    optional: dict[str, object | None] = {
        "generation_id": receipt.generation_id,
        "published_at": None if receipt.published_at is None else _moment(receipt.published_at),
        "failure_category": receipt.failure_category,
        "retry_disposition": (
            None if receipt.retry_disposition is None else receipt.retry_disposition.value
        ),
        "attempt_id": receipt.attempt_id,
        "operator_reason": receipt.operator_reason,
        "run_fingerprint": receipt.run_fingerprint,
        "run_identity": (
            None if receipt.run_identity is None else receipt.run_identity.model_dump(mode="json")
        ),
        "landing_to_publish_seconds": receipt.landing_to_publish_seconds,
    }
    return {
        PK: set_key(set_id),
        SK: receipt.sk,
        "set_id": set_id,
        "outcome": receipt.outcome.value,
        "recorded_at": _moment(receipt.recorded_at),
        "counts_by_reason": dict(receipt.counts_by_reason),
        "durations_ms": dict(receipt.durations_ms),
        **{name: value for name, value in optional.items() if value is not None},
    }


def item_size(item: Mapping[str, object]) -> int:
    """Measure the item by DynamoDB's rule, as the shared byte oracle's upper bound."""
    return _ORACLE.conservative_service_item_bytes(item)


def client_request_token(sk: str, outcome: RunOutcome) -> str:
    """Return the first 36 hex characters of SHA-256 of the receipt key and the outcome (§6b)."""
    return hashlib.sha256(f"{sk}\n{outcome.value}".encode()).hexdigest()[:TOKEN_HEX_CHARS]


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _wire(item: Mapping[str, object]) -> Item:
    return {
        name: cast("AttributeValueTypeDef", _SERIALIZER.serialize(v)) for name, v in item.items()
    }


def _value(value: object) -> AttributeValueTypeDef:
    return cast("AttributeValueTypeDef", _SERIALIZER.serialize(value))


# --- Reading items back ----------------------------------------------------------------------


def _optional_text(item: Mapping[str, Any], name: str) -> str | None:
    found = item.get(name)
    if found is None or "NULL" in found:
        return None
    text = found.get("S")
    if text is None:
        raise ItemError(f"{name} is not a string.")
    return cast("str", text)


def _optional_moment(item: Mapping[str, Any], name: str) -> datetime | None:
    text = _optional_text(item, name)
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ItemError(f"{name} was stored without a timezone offset.")
    return moment.astimezone(UTC)


def _optional_date(item: Mapping[str, Any], name: str) -> date | None:
    text = _optional_text(item, name)
    return None if text is None else date.fromisoformat(text)


def _read_last_attempt(item: Item) -> LastAttempt | None:
    found = item.get("last_attempt")
    if found is None:
        return None
    mapped = found.get("M")
    if mapped is None:
        raise ItemError("last_attempt is not a map.")
    stored = cast("Item", mapped)
    at = _optional_moment(stored, "at")
    if at is None:
        raise ItemError("last_attempt.at is missing.")
    try:
        return LastAttempt(
            run_id=read_text(stored, "run_id"),
            attempt_id=_optional_text(stored, "attempt_id"),
            run_fingerprint=_optional_text(stored, "run_fingerprint"),
            outcome=RunOutcome(read_text(stored, "outcome")),
            failure_category=_optional_text(stored, "failure_category"),
            retry_disposition=Disposition(read_text(stored, "retry_disposition")),
            transient_retries=read_int(stored, "transient_retries"),
            at=at,
        )
    except ValueError as error:
        raise ItemError(f"last_attempt: {error}") from error


def _read_current(item: Item) -> CurrentPointer:
    try:
        return CurrentPointer(
            fence=read_int(item, "fence"),
            generation_id=_optional_text(item, "generation_id"),
            input_end=_optional_date(item, "input_end"),
            published_at=_optional_moment(item, "published_at"),
            run_fingerprint=_optional_text(item, "run_fingerprint"),
            rolled_back_from=_optional_text(item, "rolled_back_from"),
            last_attempt=_read_last_attempt(item),
        )
    except ValueError as error:
        raise ItemError(f"CURRENT: {error}") from error


# --- Errors ----------------------------------------------------------------------------------


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _retries(response: Mapping[str, Any]) -> int:
    metadata = cast("Mapping[str, Any]", response.get("ResponseMetadata", {}))
    return int(metadata.get("RetryAttempts", 0))


def _raise_store_error(error: ClientError) -> NoReturn:
    """Raise `ForecastStoreThrottled` for a throttle past the client retries, else the error."""
    code = _code(error)
    if code in _THROTTLE_CODES:
        raise ForecastStoreThrottled(code, _retries(error.response)) from error
    raise error


@dataclass(frozen=True)
class _Put:
    replayed: bool
    retry_attempts: int


@dataclass(frozen=True)
class _Positions:
    """Where each action sits in the outcome transaction."""

    current: int | None
    receipt: int
    check: int | None


# --- The store -------------------------------------------------------------------------------


class DynamoDbForecastStore(ForecastStorePort):
    """`ForecastStorePort` over one DynamoDB table with `PK` and `SK` string keys."""

    def __init__(
        self, table_name: str, item_max_bytes: int, client: DynamoDBClient | None = None
    ) -> None:
        if not table_name.strip():
            raise ValueError("the forecast table name is required.")
        if item_max_bytes < 1:
            raise ValueError("item_max_bytes is positive.")
        self._table = table_name
        self._item_max_bytes = item_max_bytes
        self._dynamodb = client or boto3.client("dynamodb", config=STORE_CLIENT_CONFIG)

    # Generation writes

    def put_generation_items(
        self, generation: str, series: tuple[GenerationSeries, ...]
    ) -> StoreWriteResult:
        """Write one shard's `ASIN#` items, `WRITE_LANES` conditional puts at a time."""
        return self._put_all([asin_item(generation, one) for one in series])

    def put_list_pages(self, generation: str, pages: tuple[ListPage, ...]) -> StoreWriteResult:
        """Write the generation's `PAGE#` items, `WRITE_LANES` conditional puts at a time."""
        return self._put_all([page_item(generation, page) for page in pages])

    def put_generation_meta(self, meta: GenerationMeta) -> StoreWriteResult:
        """Write `META`; the caller writes it after every page item."""
        return self._put_all([meta_item(meta)])

    def attest_generation(self, meta: GenerationMeta) -> Attestation:
        """Re-read `META` with a strongly consistent read and compare count and digest."""
        stored = self._get(meta_key(meta.generation_id), META_SK)
        if stored is None:
            return Attestation.META_MISSING
        same = (
            read_int(stored, "item_count", default=-1) == meta.item_count
            and _optional_text(stored, "series_sha256") == meta.series_sha256
        )
        return Attestation.ATTESTED if same else Attestation.META_DIFFERS

    # The outcome transaction

    def commit_outcome(self, request: OutcomeCommitRequest) -> OutcomeCommitResult:
        """Apply one outcome as one `TransactWriteItems` call with `ClientRequestToken` (§6b)."""
        receipt = request.receipt
        actions, positions = self._transaction(request)
        try:
            response = self._dynamodb.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=client_request_token(receipt.sk, receipt.outcome),
            )
        except ClientError as error:
            return self._cancelled(request, positions, error)
        return OutcomeCommitResult(
            CommitDisposition.APPLIED, receipt.sk, receipt.outcome, _retries(response)
        )

    # Reads

    def get_current(self, set_id: str) -> CurrentPointer | None:
        """Read `CURRENT` with a strongly consistent read."""
        stored = self._get(set_key(set_id), CURRENT_SK)
        return None if stored is None else _read_current(stored)

    def get_receipt(self, set_id: str, sk: str) -> ReceiptRecord | None:
        """Read one receipt with a strongly consistent read."""
        stored = self._get(set_key(set_id), sk)
        if stored is None:
            return None
        try:
            outcome = RunOutcome(read_text(stored, "outcome"))
        except ValueError as error:
            raise ItemError(f"{sk}: {error}") from error
        return ReceiptRecord(
            sk=sk,
            outcome=outcome,
            generation_id=_optional_text(stored, "generation_id"),
            published_at=_optional_moment(stored, "published_at"),
        )

    def get_generation(self, generation: str) -> GenerationRecord | None:
        """Read a generation's `META` identity with a strongly consistent read."""
        stored = self._get(meta_key(generation), META_SK)
        if stored is None:
            return None
        return GenerationRecord(
            generation_id=read_text(stored, "generation_id"),
            set_id=read_text(stored, "set_id"),
            receipt_sk=read_text(stored, "receipt_sk"),
            input_end=date.fromisoformat(read_text(stored, "input_end")),
            run_fingerprint=read_text(stored, "run_fingerprint"),
        )

    # Internals: generation puts

    def _sized(self, item: Mapping[str, object]) -> Item:
        size = item_size(item)
        if size > self._item_max_bytes:
            raise ForecastItemTooLargeError(str(item[PK]), size, self._item_max_bytes)
        return _wire(item)

    def _put_all(self, items: list[dict[str, object]]) -> StoreWriteResult:
        """Size every item first, then write in lanes; the first failure stops new puts."""
        wired = [self._sized(item) for item in items]
        outcomes: list[_Put] = []
        with ThreadPoolExecutor(max_workers=WRITE_LANES) as lanes:
            pending: set[Future[_Put]] = set()
            for item in wired:
                if len(pending) >= WRITE_LANES:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    outcomes.extend(future.result() for future in done)
                pending.add(lanes.submit(self._put_once, item))
            outcomes.extend(future.result() for future in wait(pending).done)
        replayed = sum(1 for outcome in outcomes if outcome.replayed)
        return StoreWriteResult(
            written=len(outcomes) - replayed,
            replayed=replayed,
            retry_attempts=sum(outcome.retry_attempts for outcome in outcomes),
        )

    def _put_once(self, item: Item) -> _Put:
        try:
            response = self._dynamodb.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": PK},
            )
        except ClientError as error:
            if _code(error) != "ConditionalCheckFailedException":
                _raise_store_error(error)
            return _Put(replayed=self._is_replay(item), retry_attempts=_retries(error.response))
        return _Put(replayed=False, retry_attempts=_retries(response))

    def _is_replay(self, item: Item) -> bool:
        key = cast("str", item[PK].get("S"))
        stored = self._get(key, cast("str", item[SK].get("S")))
        if stored is not None and stored.get(ITEM_SHA256) == item[ITEM_SHA256]:
            return True
        raise ForecastStoreConflictError(key, "the key already holds other bytes")

    def _get(self, pk: str, sk: str) -> Item | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._table,
                Key={PK: {"S": pk}, SK: {"S": sk}},
                ConsistentRead=True,
            )
        except ClientError as error:
            _raise_store_error(error)
        return response.get("Item")

    # Internals: the outcome transaction

    def _transaction(
        self, request: OutcomeCommitRequest
    ) -> tuple[list[TransactWriteItemTypeDef], _Positions]:
        receipt_put: TransactWriteItemTypeDef = {
            "Put": {
                "TableName": self._table,
                "Item": self._sized(receipt_item(request.set_id, request.receipt)),
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": PK},
            }
        }
        pointer = request.pointer
        if pointer is None:
            return [receipt_put], _Positions(current=None, receipt=0, check=None)
        actions: list[TransactWriteItemTypeDef] = [self._current_update(request, pointer)]
        actions.append(receipt_put)
        check = self._generation_check(request, pointer)
        if check is not None:
            actions.append(check)
        return actions, _Positions(current=0, receipt=1, check=2 if check is not None else None)

    def _current_update(
        self, request: OutcomeCommitRequest, pointer: CurrentPointer
    ) -> TransactWriteItemTypeDef:
        outcome = request.receipt.outcome
        attempt = cast("LastAttempt", pointer.last_attempt)
        values: dict[str, object] = {":last_attempt": _last_attempt(attempt)}
        assignments = ["#last_attempt = :last_attempt"]
        if outcome in {RunOutcome.PUBLISHED, RunOutcome.ROLLED_BACK}:
            values |= {
                ":generation_id": pointer.generation_id,
                ":input_end": cast("date", pointer.input_end).isoformat(),
                ":published_at": _moment(cast("datetime", pointer.published_at)),
                ":run_fingerprint": pointer.run_fingerprint,
            }
            assignments += [
                "#generation_id = :generation_id",
                "#input_end = :input_end",
                "#published_at = :published_at",
                "#run_fingerprint = :run_fingerprint",
            ]
        if outcome is RunOutcome.ROLLED_BACK:
            values |= {":rolled_back_from": pointer.rolled_back_from, ":one": 1}
            assignments += ["#rolled_back_from = :rolled_back_from", "#fence = #fence + :one"]
            condition = "#fence = :expected_fence"
        else:
            values[":zero"] = 0
            if outcome is RunOutcome.PUBLISHED:
                values[":one"] = 1
                assignments.append("#fence = if_not_exists(#fence, :zero) + :one")
            else:
                assignments.append("#fence = if_not_exists(#fence, :zero)")
            condition = self._fence_condition(request.expected_fence, outcome)
        if ":expected_fence" in condition:
            values[":expected_fence"] = request.expected_fence
        names = {
            f"#{name}": PK if name == "pk" else name
            for name in _names_in(" ".join([*assignments, condition]))
        }
        return {
            "Update": {
                "TableName": self._table,
                "Key": {PK: {"S": set_key(request.set_id)}, SK: {"S": CURRENT_SK}},
                "UpdateExpression": "SET " + ", ".join(assignments),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {name: _value(v) for name, v in values.items()},
            }
        }

    @staticmethod
    def _fence_condition(expected_fence: int, outcome: RunOutcome) -> str:
        """Pick the §6b fence condition; only a publish adds the input_end clause."""
        if expected_fence == 0:
            return "attribute_not_exists(#pk) OR #fence = :zero"
        if outcome is RunOutcome.PUBLISHED:
            return (
                "#fence = :expected_fence"
                " AND (attribute_not_exists(#generation_id) OR #input_end <= :input_end)"
            )
        return "#fence = :expected_fence"

    def _generation_check(
        self, request: OutcomeCommitRequest, pointer: CurrentPointer
    ) -> TransactWriteItemTypeDef | None:
        outcome = request.receipt.outcome
        if outcome is RunOutcome.PUBLISHED:
            condition = "#series_sha256 = :series_sha256"
            names = {"#series_sha256": "series_sha256"}
            values = {":series_sha256": _value(request.series_sha256)}
        elif outcome is RunOutcome.ROLLED_BACK:
            condition = "attribute_exists(#pk) AND #set_id = :set_id"
            names = {"#pk": PK, "#set_id": "set_id"}
            values = {":set_id": _value(request.set_id)}
        else:
            return None
        generation = cast("str", pointer.generation_id)
        return {
            "ConditionCheck": {
                "TableName": self._table,
                "Key": {PK: {"S": meta_key(generation)}, SK: {"S": META_SK}},
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }

    def _cancelled(
        self, request: OutcomeCommitRequest, positions: _Positions, error: ClientError
    ) -> OutcomeCommitResult:
        """Resolve a refused transaction: fence moved, replay, `META` mismatch or throttle."""
        code = _code(error)
        retried = _retries(error.response)
        if code == "IdempotentParameterMismatchException":
            return self._replayed(request, retried)
        if code != "TransactionCanceledException":
            _raise_store_error(error)
        reasons = cast("list[Mapping[str, Any]]", error.response.get("CancellationReasons", []))
        codes = [str(reason.get("Code", "None")) for reason in reasons]
        failed = {index for index, reason in enumerate(codes) if reason == _CONDITION_FAILED}
        if not failed:
            throttle = next((reason for reason in codes if reason in _THROTTLE_REASONS), None)
            if throttle is not None:
                raise ForecastStoreThrottled(throttle, retried) from error
            raise error
        if positions.current in failed:
            return self._fence_moved(request, retried)
        if positions.receipt in failed:
            return self._replayed(request, retried)
        generation = cast("CurrentPointer", request.pointer).generation_id
        detail = "META no longer matches the committed generation"
        raise ForecastStoreConflictError(meta_key(cast("str", generation)), detail) from error

    def _fence_moved(self, request: OutcomeCommitRequest, retried: int) -> OutcomeCommitResult:
        """Another run or a rollback moved the fence: already current, or write `superseded`."""
        receipt = request.receipt
        current = self.get_current(request.set_id)
        if (
            receipt.outcome is RunOutcome.PUBLISHED
            and current is not None
            and current.generation_id == receipt.generation_id
        ):
            return OutcomeCommitResult(
                CommitDisposition.ALREADY_CURRENT, receipt.sk, RunOutcome.PUBLISHED, retried
            )
        superseded = replace(
            receipt,
            outcome=RunOutcome.SUPERSEDED,
            generation_id=None,
            published_at=None,
            failure_category=None,
            retry_disposition=None,
        )
        result = self.commit_outcome(
            OutcomeCommitRequest(request.set_id, request.expected_fence, superseded)
        )
        disposition = (
            CommitDisposition.SUPERSEDED
            if result.disposition is CommitDisposition.APPLIED
            else result.disposition
        )
        return replace(
            result, disposition=disposition, retry_attempts=retried + result.retry_attempts
        )

    def _replayed(self, request: OutcomeCommitRequest, retried: int) -> OutcomeCommitResult:
        sk = request.receipt.sk
        recorded = self.get_receipt(request.set_id, sk)
        if recorded is None:
            raise ForecastStoreConflictError(sk, "the request token was reused for no receipt")
        return OutcomeCommitResult(CommitDisposition.REPLAYED, sk, recorded.outcome, retried)


def _names_in(expression: str) -> Iterable[str]:
    """Every `#name` placeholder in an expression, in order, once."""
    seen: dict[str, None] = {}
    for token in expression.replace("(", " ").replace(")", " ").replace(",", " ").split():
        if token.startswith("#"):
            seen.setdefault(token[1:], None)
    return seen
