# SPDX-License-Identifier: MIT-0
"""DynamoDB reserved words: the official list and the one check both gates share.

DynamoDB rejects a request with `ValidationException` before any condition runs when an
expression uses a reserved word as a bare attribute name. The live rehearsal found the class twice
on 2026-10-15: in three stage-1 writers, and in the final runtime's
`drain_final_alias_and_activate_next`. The unit-tier fakes did not model the rule, so the suite
passed. Two gates now share this module:

- Every DynamoDB fake under `tests/` carries `@guard_reserved_words`, which refuses an unaliased
  reserved word before the fake runs anything, exactly as DynamoDB does. Every test that drives an
  adapter through a fake therefore runs the real expression builders against the rule, and
  `test_every_dynamodb_fake_is_guarded` keeps new fakes from opting out.
- `test_dynamodb_reserved_words.py` statically scans every expression fragment under `src/`.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from botocore.exceptions import ClientError

_ROOT: Final = Path(__file__).resolve().parents[2]
RESERVED_WORDS_PATH: Final = (
    _ROOT / "tests" / "fixtures" / "dynamodb-reserved-words" / "reserved-words.txt"
)
EXPRESSION_KEYS: Final = (
    "ConditionExpression",
    "UpdateExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)
_GRAMMAR: Final = frozenset({"AND", "OR", "NOT", "BETWEEN", "IN", "SET", "REMOVE", "ADD", "DELETE"})
"""Expression keywords DynamoDB parses as grammar, not as attribute names.

Deliberate exemption: an attribute literally named one of these (`set`, `in`, `or`, ...) would
still be rejected by DynamoDB, and neither half of this guard reports it, because telling an
operand from a keyword needs positional parsing. No such attribute exists in `src` (tech debt)."""
_TOKEN: Final = re.compile(r"[#:]?[A-Za-z_][A-Za-z0-9_]*|\S")
_HOLE: Final = "\x00"
"""Stands in for an f-string interpolation: never an identifier, always a token boundary."""


def reserved_words(path: Path = RESERVED_WORDS_PATH) -> frozenset[str]:
    """Return the committed official list, upper-cased (DynamoDB matches it case-insensitively)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().upper() for line in lines if line.strip() and not line.startswith("#")
    )


RESERVED_WORDS: Final = reserved_words()


def unaliased_reserved(expression: str, words: frozenset[str] = RESERVED_WORDS) -> list[str]:
    """Return every bare attribute name in `expression` that DynamoDB reserves, in order.

    Placeholders (`#name`, `:value`), grammar keywords and function names (an identifier
    followed by `(`) are not attribute names.
    """
    tokens = _TOKEN.findall(expression)
    found: list[str] = []
    for index, token in enumerate(tokens):
        if token[0] in "#:" or not (token[0].isalpha() or token[0] == "_"):
            continue
        if token.upper() in _GRAMMAR:
            continue
        if index + 1 < len(tokens) and tokens[index + 1] == "(":
            continue
        if token.upper() in words:
            found.append(token)
    return found


def request_refusal(request: Mapping[str, object]) -> str | None:
    """Return DynamoDB's refusal message for one request body, or None when it is accepted."""
    for key in EXPRESSION_KEYS:
        expression = request.get(key)
        if isinstance(expression, str):
            hits = unaliased_reserved(expression)
            if hits:
                return (
                    f"Invalid {key}: Attribute name is a reserved keyword; "
                    f"reserved keyword: {hits[0]}"
                )
    return None


# --- The guard every DynamoDB fake carries ------------------------------------------------------

GUARDED_METHODS: Final = (
    "transact_write_items",
    "transact_get_items",
    "batch_get_item",
    "put_item",
    "update_item",
    "delete_item",
    "get_item",
    "query",
    "scan",
)
_GUARD_MARK: Final = "__reserved_word_guard__"


def _request_bodies(operation: str, request: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield each body DynamoDB parses expressions from, for one operation's request."""
    if operation in {"transact_write_items", "transact_get_items"}:
        for member in request.get("TransactItems") or ():
            yield from (body for body in member.values() if isinstance(body, Mapping))
    elif operation == "batch_get_item":
        tables = cast("Mapping[str, object]", request.get("RequestItems") or {})
        for table in tables.values():
            if isinstance(table, Mapping):
                yield cast("Mapping[str, Any]", table)
    else:
        yield request


def refuse_reserved(operation: str, request: Mapping[str, Any]) -> None:
    """Raise DynamoDB's `ValidationException` for an unaliased reserved word, or return."""
    for body in _request_bodies(operation, request):
        message = request_refusal(body)
        if message is not None:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": message}}, operation
            )


def _guarded(operation: str, method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def call(self: object, *args: Any, **kwargs: Any) -> Any:
        refuse_reserved(operation, kwargs)  # before any hook, injected fault or state change
        return method(self, *args, **kwargs)

    return call


def guard_reserved_words[C: type](cls: C) -> C:
    """Make one DynamoDB fake refuse unaliased reserved words in every request it defines."""
    for operation in GUARDED_METHODS:
        method = cls.__dict__.get(operation)
        if method is not None:
            setattr(cls, operation, _guarded(operation, method))
    setattr(cls, _GUARD_MARK, True)
    return cls


# --- Static scan over src/ -----------------------------------------------------------------------
#
# Discovery covers (1) every string literal and f-string, (2) `+` chains of them, joined, and
# (3) continuation fragments that open with `AND`/`OR`/`,` (what a helper appends to a condition
# or an update). Projection lists (the value of a `ProjectionExpression` keyword or dict entry)
# carry no placeholder, so they are scanned as projections. A fragment a helper computes at run
# time from non-literal parts is outside static reach; `FakeDynamo` refuses it on every request
# a test drives, which is the second, required half of this guard.

_FRAGMENT: Final = re.compile(
    r"(^|[\s(,])[:#][A-Za-z_]|attribute_(not_)?exists\(|begins_with\(|if_not_exists\("
    r"|^\s*(SET|REMOVE|ADD|DELETE)\s|^\s*(AND|OR)\s|^\s*,\s*[A-Za-z_#]"
)
"""A string that is (part of) a DynamoDB expression: it names a placeholder or a DynamoDB
function, opens an update clause, or continues one (`AND …`, `OR …`, `, name = …`)."""


@dataclass(frozen=True, slots=True)
class Hit:
    """One unaliased reserved word in one expression fragment."""

    path: str
    line: int
    word: str
    fragment: str


def _docstrings(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def _text(node: ast.expr) -> str | None:
    """Return the literal text of a string, an f-string (holes marked) or a `+` chain."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else _HOLE
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is not None or right is not None:
            return (left if left is not None else _HOLE) + (right if right is not None else _HOLE)
    return None


def _projections(tree: ast.Module) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "ProjectionExpression":
            yield node.value
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "ProjectionExpression":
                    yield value


def _literals(tree: ast.Module) -> Iterator[tuple[int, str, bool]]:
    """Yield `(line, text, is_projection)` for every candidate; each node once, outermost first."""
    skipped = _docstrings(tree)
    projections = {id(node) for node in _projections(tree)}
    covered: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in covered or id(node) in skipped or not isinstance(node, ast.expr):
            continue
        text = _text(node)
        if text is None:
            continue
        for inner in ast.walk(node):
            covered.add(id(inner))
        yield node.lineno, text, id(node) in projections


def scan_source(path: str, source: str) -> list[Hit]:
    """Return every unaliased reserved word in the expression fragments of one module."""
    hits: list[Hit] = []
    for line, text, projection in _literals(ast.parse(source)):
        if not projection and _FRAGMENT.search(text) is None:
            continue
        hits.extend(
            Hit(path, line, word, text.replace(_HOLE, "{…}")) for word in unaliased_reserved(text)
        )
    return hits


UNGUARDED_FAKES: Final = {
    (
        "tests/integration/stage1_probe_world.py",
        "RecordingDynamo",
    ): "wraps the real DynamoDB client",
    ("tests/integration/test_execution_graph_store_live.py", "_Hooked"): "wraps the real client",
}
"""Request-method classes that forward to real DynamoDB, which enforces the rule itself."""


def unguarded_fakes(root: Path) -> list[str]:
    """Return every class under `tests/` that defines a DynamoDB request method unguarded."""
    found: list[str] = []
    for module in sorted((root / "tests").rglob("*.py")):
        relative = module.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or (relative, node.name) in UNGUARDED_FAKES:
                continue
            defines = any(
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name in GUARDED_METHODS
                for item in node.body
            )
            guarded = any(
                ast.unparse(item) == "guard_reserved_words" for item in node.decorator_list
            )
            if defines and not guarded:
                found.append(f"{relative}:{node.lineno} {node.name}")
    return found


def scan_tree(root: Path) -> list[Hit]:
    """Scan every Python module under `root/src`."""
    hits: list[Hit] = []
    for module in sorted((root / "src").rglob("*.py")):
        relative = module.relative_to(root).as_posix()
        hits.extend(scan_source(relative, module.read_text(encoding="utf-8")))
    return hits
