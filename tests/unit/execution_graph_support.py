# SPDX-License-Identifier: MIT-0
"""In-memory fakes for the execution-graph store tests (S1).

`FakeDynamo` is a transactional stand-in for the low-level DynamoDB client. It evaluates the
exact condition and update grammar the adapter emits, so the store tests drive the real adapter
code: either every transaction member's condition holds and all apply, or none applies and the
call raises `TransactionCanceledException` with per-member reasons, as DynamoDB does.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from botocore.exceptions import ClientError
from dynamodb_reserved_words import guard_reserved_words

from ports.outbound.execution_graph import ManifestRead

type Attr = dict[str, Any]
type Item = dict[str, Attr]

_TOKEN = re.compile(r"\s*(\(|\)|<>|<=|>=|=|<|>|,|\+|[:#]?[A-Za-z_][A-Za-z0-9_]*)")
_MAX_TRANSACTION_ITEMS = 100
"""DynamoDB's TransactWriteItems limit: at most 100 actions, no two on one item (slice 7)."""
_MAX_ITEM_BYTES = 400_000
_MAX_TRANSACTION_BYTES = 4_000_000
"""Slice 7 (F3): the byte oracle's admitted item and aggregate sizes, below the service's 400 KB
and 4 MB. The fake measures each target's actual pre-state and result with the oracle, so it is
stricter than DynamoDB, never looser."""


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            if text[position:].strip() == "":
                break
            raise ValueError(f"unparsed expression near {text[position:]!r}")
        found.append(match.group(1))
        position = match.end()
    return found


def _value(attr: Attr | None) -> object:
    if attr is None:
        return None
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        return int(attr["N"])
    if "BOOL" in attr:
        return attr["BOOL"]
    return attr


class _Condition:
    def __init__(self, text: str, names: dict[str, str], values: dict[str, Attr]) -> None:
        self.tokens = _tokens(text)
        self.position = 0
        self.names = names
        self.values = values

    def _name(self, token: str) -> str:
        return self.names.get(token, token)

    def _next(self) -> str:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def evaluate(self, item: Item | None) -> bool:
        result = self._or(item)
        if self.position != len(self.tokens):
            raise ValueError("trailing tokens in a condition")
        return result

    def _or(self, item: Item | None) -> bool:
        result = self._and(item)
        while self.position < len(self.tokens) and self.tokens[self.position] == "OR":
            self._next()
            right = self._and(item)
            result = result or right
        return result

    def _and(self, item: Item | None) -> bool:
        result = self._atom(item)
        while self.position < len(self.tokens) and self.tokens[self.position] == "AND":
            self._next()
            right = self._atom(item)
            result = result and right
        return result

    def _atom(self, item: Item | None) -> bool:  # noqa: PLR0911 -- one return per operator
        token = self._next()
        if token == "(":
            result = self._or(item)
            if self._next() != ")":
                raise ValueError("unbalanced condition")
            return result
        if token in {"attribute_not_exists", "attribute_exists"}:
            self._next()
            name = self._name(self._next())
            self._next()
            present = item is not None and name in item
            return present if token == "attribute_exists" else not present
        name = self._name(token)
        operator = self._next()
        operand = _value(self.values[self._next()])
        current = None if item is None else _value(item.get(name))
        if operator == "=":
            return current == operand
        if operator == "<>":
            return current != operand
        if operator == "<":
            return current is not None and current < operand  # type: ignore[operator]
        if operator == ">":
            return current is not None and current > operand  # type: ignore[operator]
        if operator == ">=":
            return current is not None and current >= operand  # type: ignore[operator]
        if operator == "<=":
            return current is not None and current <= operand  # type: ignore[operator]
        raise ValueError(f"unknown operator {operator}")


def _apply_update(  # noqa: PLR0913, PLR0917 -- one fake update applier
    item: Item, expression: str, names: dict[str, str], values: dict[str, Attr]
) -> None:
    clauses = re.split(r"\b(SET|ADD|REMOVE)\b", expression)
    kind = None
    for part in clauses:
        stripped = part.strip()
        if stripped in {"SET", "ADD", "REMOVE"}:
            kind = stripped
            continue
        if not stripped:
            continue
        for assignment in stripped.split(","):
            words = assignment.strip()
            if kind == "SET":
                left, right = (side.strip() for side in words.split("=", 1))
                name = names.get(left, left)
                if "+" in right:
                    base, increment = (side.strip() for side in right.split("+"))
                    current = int(item.get(names.get(base, base), {"N": "0"})["N"])
                    item[name] = {"N": str(current + int(values[increment]["N"]))}
                else:
                    item[name] = copy.deepcopy(values[right])
            elif kind == "REMOVE":
                item.pop(names.get(words, words), None)
            elif kind == "ADD":
                left, right = words.split()
                name = names.get(left, left)
                if "SS" in values[right]:
                    # S6 (D29): ADD of a string set creates an absent set or unions into it.
                    members = {*item.get(name, {"SS": []})["SS"], *values[right]["SS"]}
                    item[name] = {"SS": sorted(members)}
                    continue
                current = int(item.get(name, {"N": "0"})["N"])
                item[name] = {"N": str(current + int(values[right]["N"]))}


@guard_reserved_words
@dataclass
class FakeDynamo:
    """A transactional in-memory table the adapter's own expressions run against."""

    items: dict[tuple[str, str], Item] = field(default_factory=dict[tuple[str, str], Item])
    fail_next: bool = False
    conflicts_next: int = 0
    """S3: cancel this many next transactions with a `TransactionConflict` (R2-7)."""
    conflicts_raised: int = 0
    transactions: list[list[dict[str, Any]]] = field(default_factory=list[list[dict[str, Any]]])
    before_transaction: Any = None
    fail_query: str | None = None
    """S5: fail every query whose arguments contain this text (a shard name, for example)."""
    key_names: tuple[str, str] = ("pk", "sk")
    """S5: the table's key attribute names (the status table uses partition_key/sort_key)."""
    ambiguous_next: bool = False
    """S5: commit the next transaction, then raise as if its response were lost."""
    update_calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    """S6: every single-item `UpdateItem` call (the lease release is one, not a transaction)."""
    queries: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """S6 (AGG4-14): every query's partition key and sort-key prefix, in call order."""
    after_query: Any = None
    """S6 (AGG4-14): called with (fake, pk, prefix) after each query answers."""

    def _key(self, key: Item) -> tuple[str, str]:
        pk, sk = self.key_names
        return (str(key[pk]["S"]), str(key[sk]["S"]))

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        """One conditional put, as its own single-member transaction."""
        put = {
            name: kwargs[name]
            for name in ("TableName", "Item", "ConditionExpression")
            if name in kwargs
        }
        try:
            self.transact_write_items(TransactItems=[{"Put": put}])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                    "PutItem",
                ) from None
            raise
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """S6: one conditional update, as its own single-member transaction (`UpdateItem`)."""
        update = {
            name: kwargs[name]
            for name in (
                "TableName",
                "Key",
                "UpdateExpression",
                "ConditionExpression",
                "ExpressionAttributeNames",
                "ExpressionAttributeValues",
            )
            if name in kwargs
        }
        self.update_calls.append(update)
        try:
            self.transact_write_items(TransactItems=[{"Update": update}])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}},
                    "UpdateItem",
                ) from None
            raise
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        found = self.items.get(self._key(kwargs["Key"]))
        return {} if found is None else {"Item": copy.deepcopy(found)}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        members: list[dict[str, Any]] = kwargs["TransactItems"]
        if self.before_transaction is not None:
            hook, self.before_transaction = self.before_transaction, None
            hook(self)
        if self.fail_next:
            self.fail_next = False
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "injected"}},
                "TransactWriteItems",
            )
        if self.conflicts_next:
            self.conflicts_next -= 1
            self.conflicts_raised += 1
            response_conflict: Any = {
                "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
                "CancellationReasons": [{"Code": "TransactionConflict"}] * len(members),
            }
            raise ClientError(response_conflict, "TransactWriteItems")
        self._validate(members)
        reasons: list[dict[str, Any]] = []
        for member in members:
            ((kind, body),) = member.items()
            key = self._key(body["Item"] if kind == "Put" else body["Key"])
            condition = body.get("ConditionExpression")
            ok = condition is None or _Condition(
                condition,
                body.get("ExpressionAttributeNames", {}),
                body.get("ExpressionAttributeValues", {}),
            ).evaluate(self.items.get(key))
            reason: dict[str, Any] = {"Code": "None" if ok else "ConditionalCheckFailed"}
            old = self.items.get(key)
            if not ok and body.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" and old:
                reason["Item"] = copy.deepcopy(old)
            reasons.append(reason)
        if any(reason["Code"] != "None" for reason in reasons):
            response: Any = {
                "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                "CancellationReasons": reasons,
            }
            raise ClientError(response, "TransactWriteItems")
        for member in members:
            ((kind, body),) = member.items()
            if kind == "Put":
                self.items[self._key(body["Item"])] = copy.deepcopy(body["Item"])
            elif kind == "Delete":
                self.items.pop(self._key(body["Key"]), None)
            elif kind == "Update":
                key = self._key(body["Key"])
                item = self.items.setdefault(key, copy.deepcopy(body["Key"]))
                _apply_update(
                    item,
                    body["UpdateExpression"],
                    body.get("ExpressionAttributeNames", {}),
                    body.get("ExpressionAttributeValues", {}),
                )
        self.transactions.append(members)
        if self.ambiguous_next:
            self.ambiguous_next = False
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "response lost"}},
                "TransactWriteItems",
            )
        return {}

    def _validate(self, members: list[dict[str, Any]]) -> None:
        """Refuse what DynamoDB refuses before evaluating any condition (slice 7, P7)."""
        keys: list[tuple[str, str]] = []
        for member in members:
            ((kind, body),) = member.items()
            keys.append(self._key(body["Item"] if kind == "Put" else body["Key"]))
        if len(members) > _MAX_TRANSACTION_ITEMS or len(set(keys)) != len(keys):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "more than 100 actions or two actions on one item",
                    }
                },
                "TransactWriteItems",
            )
        sizes = [self._target_bytes(member) for member in members]
        if max(sizes) > _MAX_ITEM_BYTES or sum(sizes) > _MAX_TRANSACTION_BYTES:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "an item over 400 KB or a transaction over 4 MB",
                    }
                },
                "TransactWriteItems",
            )

    def _target_bytes(self, member: dict[str, Any]) -> int:
        """Return the larger of one target's actual pre-state and result size (slice 7, F3)."""
        from adapters.outbound.dynamodb.graph_transaction_bytes import (  # noqa: PLC0415
            item_bytes,
        )

        ((kind, body),) = member.items()
        old = self.items.get(self._key(body["Item"] if kind == "Put" else body["Key"]))
        new: Item | None
        if kind == "Put":
            new = body["Item"]
        elif kind == "Update":
            updated: Item = copy.deepcopy(old) if old is not None else copy.deepcopy(body["Key"])
            _apply_update(
                updated,
                body["UpdateExpression"],
                body.get("ExpressionAttributeNames", {}),
                body.get("ExpressionAttributeValues", {}),
            )
            new = updated
        elif kind == "Delete":
            new = None
        else:
            new = old
        images = [cast("Any", image) for image in (old, new) if image is not None]
        return max((item_bytes(image) for image in images), default=0)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Answer `pk = :pk [AND begins_with(sk, :prefix)]` in sort-key order."""
        condition = str(kwargs["KeyConditionExpression"])
        aliases = cast("dict[str, str]", kwargs.get("ExpressionAttributeNames", {}))
        for alias in aliases:
            condition = condition.replace(alias, {"#pk": "pk", "#sk": "sk"}.get(alias, alias))
        if condition not in {"pk = :pk AND begins_with(sk, :prefix)", "pk = :pk"}:
            raise ValueError("unsupported key condition")
        if self.fail_query is not None and self.fail_query in str(condition) + str(kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "injected"}}, "Query"
            )
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]["S"]
        prefix = values.get(":prefix", {"S": ""})["S"]
        found = [
            copy.deepcopy(item)
            for (item_pk, item_sk), item in sorted(self.items.items())
            if item_pk == pk and item_sk.startswith(prefix)
        ]
        self.queries.append((pk, prefix))
        if self.after_query is not None:
            self.after_query(self, pk, prefix)
        return {"Items": found}

    def kinds(self) -> list[str]:
        """Every stored item's pk family, sorted."""
        return sorted(pk.split("#", 1)[0] + "/" + sk.split("#", 1)[0] for pk, sk in self.items)


@dataclass
class FakeManifests:
    """A manifest reader with a fixed answer, or a dependency failure."""

    retrieved_at: datetime
    source_system: str = "amc"
    source_dataset: str = "dsp_impressions"
    period_end: str = "2026-10-06"
    exact_versions: int = 1
    window_retrievals: int = 1
    fail: bool = False
    reads: int = 0
    last_modified: datetime | None = None
    """S5-V-5: the root clock; None reads as the retrieval instant (admission requires one)."""
    unclocked: bool = False
    """Return no root clock at all, to prove admission refuses it."""

    def read(self, key: str, version_id: str) -> ManifestRead:
        del key, version_id
        self.reads += 1
        if self.fail:
            from domain.shared_kernel.errors import DependencyError, ErrorCode  # noqa: PLC0415

            raise DependencyError(ErrorCode("RUN_REQUEST", 14), "injected listing failure")
        return ManifestRead(
            retrieved_at=self.retrieved_at,
            source_system=self.source_system,
            source_dataset=self.source_dataset,
            period_end=self.period_end,
            exact_versions=self.exact_versions,
            window_retrievals=self.window_retrievals,
            last_modified=None if self.unclocked else (self.last_modified or self.retrieved_at),
        )


def fixed_now() -> datetime:
    """Return the fixed instant every store test records receipts at."""
    return datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
