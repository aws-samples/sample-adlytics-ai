# SPDX-License-Identifier: MIT-0
"""An in-memory forecast table that evaluates the store's real expressions (design §6b).

`ForecastTableFake` keeps low-level items by (`PK`, `SK`) and evaluates the subset of the
DynamoDB expression grammar the store writes: `attribute_exists`, `attribute_not_exists`,
comparisons, `AND`, `OR`, parentheses, `SET` with `if_not_exists` and `+`, and a trailing
`REMOVE`. A transaction is all-or-nothing with per-action `CancellationReasons`, and a reused
`ClientRequestToken` is idempotent while `remember_tokens` holds, as the service is within its
10-minute window.

Faults model a dying Lambda (`crash_on_put`), a response lost after the write applied
(`lose_transaction_response`), and throttles (`throttle_puts`, `throttle_transaction`).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from botocore.exceptions import ClientError, ReadTimeoutError
from dynamodb_reserved_words import guard_reserved_words

Key = tuple[str, str]
Wire = dict[str, Any]

_TOKEN = re.compile(r"<=|>=|<>|[()=<>,+]|[#:][A-Za-z_][A-Za-z0-9_]*|[A-Za-z_]+")


class LambdaCrashed(Exception):  # noqa: N818 - names the simulated event, not an error kind
    """The simulated publisher died mid-call; nothing after this point ran."""


def _key(item: Mapping[str, Any]) -> Key:
    return (str(item["PK"]["S"]), str(item["SK"]["S"]))


def _number(value: Mapping[str, Any]) -> Decimal | None:
    return Decimal(value["N"]) if "N" in value else None


def _compare(
    left: Mapping[str, Any] | None, operator: str, right: Mapping[str, Any] | None
) -> bool:
    if left is None or right is None:
        return False
    lnum, rnum = _number(left), _number(right)
    lhs: Any = lnum if lnum is not None else left.get("S", left)
    rhs: Any = rnum if rnum is not None else right.get("S", right)
    if operator == "=":
        return lhs == rhs
    if operator == "<>":
        return lhs != rhs
    ordered = {"<": lhs < rhs, "<=": lhs <= rhs, ">": lhs > rhs, ">=": lhs >= rhs}
    return ordered[operator]


class _Expression:
    """A recursive-descent reading of one condition or update expression."""

    def __init__(self, text: str, names: Mapping[str, str], values: Mapping[str, Any]) -> None:
        self._tokens = _TOKEN.findall(text)
        self._at = 0
        self._names = names
        self._values = values

    def _peek(self) -> str | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._tokens[self._at]
        if expected is not None and token != expected:
            raise AssertionError(f"expected {expected!r}, found {token!r}")
        self._at += 1
        return token

    def _name(self, token: str) -> str:
        return self._names[token] if token.startswith("#") else token

    def _operand(self, item: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        token = self._take()
        if token.startswith(":"):
            return cast("Mapping[str, Any]", self._values[token])
        if token == "if_not_exists":
            self._take("(")
            name = self._name(self._take())
            self._take(",")
            fallback = self._operand(item)
            self._take(")")
            found = None if item is None else item.get(name)
            return found if found is not None else fallback
        return None if item is None else item.get(self._name(token))

    def _sum(self, item: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        value = self._operand(item)
        while self._peek() == "+":
            self._take()
            right = self._operand(item)
            assert value is not None and right is not None, "+ needs two numbers"
            value = {"N": str(Decimal(value["N"]) + Decimal(right["N"]))}
        return value

    def _atom(self, item: Mapping[str, Any] | None) -> bool:
        token = self._peek()
        if token == "(":
            self._take()
            result = self._or(item)
            self._take(")")
            return result
        if token in {"attribute_exists", "attribute_not_exists"}:
            self._take()
            self._take("(")
            name = self._name(self._take())
            present = item is not None and name in item
            self._take(")")
            return present if token == "attribute_exists" else not present
        left = self._operand(item)
        operator = self._take()
        return _compare(left, operator, self._operand(item))

    def _and(self, item: Mapping[str, Any] | None) -> bool:
        result = self._atom(item)
        while self._peek() == "AND":
            self._take()
            result = self._atom(item) and result
        return result

    def _or(self, item: Mapping[str, Any] | None) -> bool:
        result = self._and(item)
        while self._peek() == "OR":
            self._take()
            result = self._and(item) or result
        return result

    def holds(self, item: Mapping[str, Any] | None) -> bool:
        """Evaluate the whole expression as a condition."""
        result = self._or(item)
        assert self._peek() is None, f"unparsed {self._tokens[self._at :]}"
        return result

    def apply(self, item: Mapping[str, Any] | None) -> tuple[dict[str, Any], set[str]]:
        """Evaluate a `SET` expression with an optional `REMOVE` clause: new and removed names."""
        self._take("SET")
        updates: dict[str, Any] = {}
        while True:
            name = self._name(self._take())
            self._take("=")
            updates[name] = self._sum(item)
            if self._peek() != ",":
                break
            self._take()
        removed: set[str] = set()
        if self._peek() == "REMOVE":
            self._take()
            removed.add(self._name(self._take()))
            while self._peek() == ",":
                self._take()
                removed.add(self._name(self._take()))
        assert self._peek() is None, f"unparsed {self._tokens[self._at :]}"
        return updates, removed


def refuse_unmatched_placeholders(operation: str, body: Mapping[str, Any]) -> None:
    """Raise DynamoDB's `ValidationException` for an undefined or an unused placeholder."""
    text = " ".join(
        str(body[key]) for key in ("ConditionExpression", "UpdateExpression") if key in body
    )
    used = set(_TOKEN.findall(text))
    for prefix, given in (
        ("#", body.get("ExpressionAttributeNames", {})),
        (":", body.get("ExpressionAttributeValues", {})),
    ):
        wanted = {token for token in used if token.startswith(prefix)}
        if wanted != set(given):
            message = f"placeholders used {sorted(wanted)} differ from given {sorted(given)}"
            raise ClientError(
                cast("Any", {"Error": {"Code": "ValidationException", "Message": message}}),
                operation,
            )


def _condition_holds(request: Mapping[str, Any], item: Mapping[str, Any] | None) -> bool:
    text = request.get("ConditionExpression")
    if text is None:
        return True
    names = request.get("ExpressionAttributeNames", {})
    values = request.get("ExpressionAttributeValues", {})
    return _Expression(text, names, values).holds(item)


def _throttled(operation: str, code: str) -> ClientError:
    response: Any = {"Error": {"Code": code}, "ResponseMetadata": {"RetryAttempts": 9}}
    return ClientError(response, operation)


@guard_reserved_words
@dataclass
class ForecastTableFake:
    """The forecast table, its request log and its injected faults."""

    items: dict[Key, Wire] = field(default_factory=dict[Key, Wire])
    puts: list[Wire] = field(default_factory=list[Wire])
    gets: list[Wire] = field(default_factory=list[Wire])
    transactions: list[Wire] = field(default_factory=list[Wire])
    crash_on_put: int | None = None
    """The 1-based `put_item` call at which the simulated Lambda dies, before writing."""
    throttle_puts: bool = False
    throttle_transaction: str | None = None
    """A cancellation reason code every transaction is refused with."""
    lose_transaction_response: bool = False
    remember_tokens: bool = True
    retry_attempts: int = 0
    _tokens: dict[str, Wire] = field(default_factory=dict[str, Wire])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _metadata(self) -> Wire:
        return {"ResponseMetadata": {"RetryAttempts": self.retry_attempts}}

    def put_item(self, **request: Any) -> Wire:
        with self._lock:
            self.puts.append(request)
            refuse_unmatched_placeholders("PutItem", request)
            if self.crash_on_put is not None and len(self.puts) >= self.crash_on_put:
                raise LambdaCrashed(f"put {len(self.puts)}")
            if self.throttle_puts:
                raise _throttled("PutItem", "ProvisionedThroughputExceededException")
            item = cast("Wire", request["Item"])
            if not _condition_holds(request, self.items.get(_key(item))):
                error: Any = {
                    "Error": {"Code": "ConditionalCheckFailedException"},
                    **self._metadata(),
                }
                raise ClientError(error, "PutItem")
            self.items[_key(item)] = item
            return self._metadata()

    def get_item(self, **request: Any) -> Wire:
        with self._lock:
            self.gets.append(request)
            found = self.items.get(_key(request["Key"]))
            return {**self._metadata(), **({} if found is None else {"Item": found})}

    def _reason(self, action: Mapping[str, Any]) -> str:
        (kind, body), *_ = action.items()
        key = _key(body["Item"] if kind == "Put" else body["Key"])
        return "None" if _condition_holds(body, self.items.get(key)) else "ConditionalCheckFailed"

    def _apply(self, action: Mapping[str, Any]) -> None:
        (kind, body), *_ = action.items()
        if kind == "Put":
            self.items[_key(body["Item"])] = body["Item"]
        elif kind == "Update":
            key = _key(body["Key"])
            current = self.items.get(key)
            expression = _Expression(
                body["UpdateExpression"],
                body.get("ExpressionAttributeNames", {}),
                body.get("ExpressionAttributeValues", {}),
            )
            updates, removed = expression.apply(current)
            merged = {**(current or body["Key"]), **updates}
            self.items[key] = {name: v for name, v in merged.items() if name not in removed}

    def transact_write_items(self, **request: Any) -> Wire:
        with self._lock:
            self.transactions.append(request)
            token = request.get("ClientRequestToken")
            if token is not None and token in self._tokens:
                if self._tokens[token] != request:
                    raise ClientError(
                        cast("Any", {"Error": {"Code": "IdempotentParameterMismatchException"}}),
                        "TransactWriteItems",
                    )
                return self._metadata()
            actions = cast("list[Wire]", request["TransactItems"])
            for action in actions:
                (body,) = action.values()
                refuse_unmatched_placeholders("TransactWriteItems", body)
            if self.throttle_transaction is not None:
                reasons = [{"Code": self.throttle_transaction} for _ in actions]
                raise ClientError(
                    cast(
                        "Any",
                        {
                            "Error": {"Code": "TransactionCanceledException"},
                            "CancellationReasons": reasons,
                            "ResponseMetadata": {"RetryAttempts": 9},
                        },
                    ),
                    "TransactWriteItems",
                )
            codes = [self._reason(action) for action in actions]
            if any(code != "None" for code in codes):
                raise ClientError(
                    cast(
                        "Any",
                        {
                            "Error": {"Code": "TransactionCanceledException"},
                            "CancellationReasons": [{"Code": code} for code in codes],
                            **self._metadata(),
                        },
                    ),
                    "TransactWriteItems",
                )
            for action in actions:
                self._apply(action)
            if token is not None and self.remember_tokens:
                self._tokens[token] = request
            if self.lose_transaction_response:
                self.lose_transaction_response = False
                raise ReadTimeoutError(endpoint_url="https://dynamodb.invalid")
            return self._metadata()

    def forget_tokens(self) -> None:
        """Model a retry after the service's 10-minute idempotency window."""
        self._tokens.clear()

    def item(self, pk: str, sk: str) -> Wire | None:
        """Return one stored item, low-level."""
        return self.items.get((pk, sk))

    def keys(self, prefix: str = "") -> Iterator[Key]:
        """Yield every stored key whose PK starts with `prefix`."""
        return (key for key in self.items if key[0].startswith(prefix))
