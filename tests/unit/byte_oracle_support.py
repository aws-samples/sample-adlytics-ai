# SPDX-License-Identifier: MIT-0
"""Standard-library reference loader/checker for the §3.2 byte-oracle fixture tree.

This module imports and calls no production oracle or builder. From each frozen
fixture it recomputes ``wire_json_bytes`` with only the standard library and
computes conservative totals directly from the frozen low-level maps by the
documented recursive vocabulary walk. Production builders and
``Stage1DynamoByteOracle`` must independently match these frozen expectations;
this checker is the second, independent leg of that agreement.

Fixture tree layout (ADR-075 §3.2 / §13.1):
``tests/fixtures/stage1-dynamodb-byte-oracle/<typed-operation>/<branch>-v1.json``
with both required branches — ``maximum`` and ``one-beyond`` — present for every
typed operation. Fixtures are never regenerated during tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, cast

__all__ = [
    "FIXTURE_TREE",
    "REQUIRED_BRANCHES",
    "FixtureCheckError",
    "FixtureTreeError",
    "canonical_json_bytes",
    "check_fixed_member_fixture",
    "check_fixture",
    "load_fixture_tree",
]

FIXTURE_TREE: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "stage1-dynamodb-byte-oracle"
)
REQUIRED_BRANCHES: Final = ("maximum", "one-beyond")
_FIXTURE_VERSION: Final = "v1"

_MAX_ACTIONS: Final = 100
_MAX_ITEM_BYTES: Final = 400000
_MAX_AGGREGATE_BYTES: Final = 4000000

_TEXT_TAGS: Final = frozenset({"S", "B"})
_SET_TAGS: Final = frozenset({"SS", "NS", "BS"})
_NUMBER_TEXT: Final = re.compile(r"\A-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z")


class FixtureTreeError(Exception):
    """The fixture tree is malformed: a required branch or member is absent."""


class FixtureCheckError(Exception):
    """A frozen fixture disagrees with its independently recomputed expectations."""


def canonical_json_bytes(value: object) -> int:
    """Measure the §3.2 canonical low-level JSON wire bytes of one frozen value."""
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _require_scalar(tag: str, member: object, where: str) -> None:
    if tag in _TEXT_TAGS:
        if not isinstance(member, str):
            raise FixtureCheckError(f"{where}: {tag} must carry text.")
    elif tag == "N":
        if not isinstance(member, str) or not _NUMBER_TEXT.match(member):
            raise FixtureCheckError(f"{where}: N must carry finite decimal text.")
    elif tag == "BOOL":
        if not isinstance(member, bool):
            raise FixtureCheckError(f"{where}: BOOL must carry a boolean.")
    elif member is not True:  # tag == "NULL"
        raise FixtureCheckError(f"{where}: NULL must carry true.")


def _require_set(tag: str, member: object, where: str) -> None:
    if not isinstance(member, list) or not member:
        raise FixtureCheckError(f"{where}: {tag} must carry a nonempty list.")
    for index, child in enumerate(cast("list[object]", member)):
        if not isinstance(child, str):
            raise FixtureCheckError(f"{where}: {tag}[{index}] must carry text.")
        if tag == "NS" and not _NUMBER_TEXT.match(child):
            raise FixtureCheckError(f"{where}: NS[{index}] must carry finite decimal text.")


def _require_attribute_value(value: object, where: str) -> None:
    """Recursively require one low-level attribute value in the admitted vocabulary."""
    if not isinstance(value, dict):
        raise FixtureCheckError(f"{where}: an attribute value must be a map.")
    value_map = cast("dict[str, object]", value)
    if len(value_map) != 1:
        raise FixtureCheckError(f"{where}: an attribute value must carry exactly one type tag.")
    tag, member = next(iter(value_map.items()))
    if tag in _TEXT_TAGS or tag in ("N", "BOOL", "NULL"):
        _require_scalar(tag, member, where)
    elif tag == "L":
        if not isinstance(member, list):
            raise FixtureCheckError(f"{where}: L must carry a list.")
        for index, child in enumerate(cast("list[object]", member)):
            _require_attribute_value(child, f"{where}.L[{index}]")
    elif tag == "M":
        _require_item_map(member, f"{where}.M")
    elif tag in _SET_TAGS:
        _require_set(tag, member, where)
    else:
        raise FixtureCheckError(f"{where}: unsupported type tag {tag!r}.")


def _require_item_map(value: object, where: str) -> None:
    if not isinstance(value, dict):
        raise FixtureCheckError(f"{where}: an item map must be a map.")
    for name, member in cast("dict[object, object]", value).items():
        if not isinstance(name, str) or not name:
            raise FixtureCheckError(f"{where}: an attribute name must be nonempty text.")
        _require_attribute_value(member, f"{where}.{name}")


def _item_bytes(value: object, where: str) -> int:
    """Conservative service-item bytes of one frozen low-level map; absence is zero."""
    if value is None:
        return 0
    _require_item_map(value, where)
    return canonical_json_bytes(value)


def _required_member(fixture: dict[str, Any], name: str) -> Any:
    if name not in fixture:
        raise FixtureCheckError(f"fixture member {name!r} is absent.")
    return fixture[name]


def _check_target(target: object, ordinal: int) -> tuple[int, int, int]:
    where = f"targets[{ordinal}]"
    if not isinstance(target, dict):
        raise FixtureCheckError(f"{where}: a target must be a map.")
    target = cast("dict[str, Any]", target)
    pre_state_bytes = _item_bytes(
        _required_member(target, "expected_pre_state_map"), f"{where}.expected_pre_state_map"
    )
    result_bytes = _item_bytes(
        _required_member(target, "expected_result_map"), f"{where}.expected_result_map"
    )
    if pre_state_bytes != _required_member(target, "pre_state_conservative_bytes"):
        raise FixtureCheckError(f"{where}: pre-state conservative bytes disagree.")
    if result_bytes != _required_member(target, "result_conservative_bytes"):
        raise FixtureCheckError(f"{where}: result conservative bytes disagree.")
    upper_bound = max(pre_state_bytes, result_bytes)
    if upper_bound != _required_member(target, "target_upper_bound"):
        raise FixtureCheckError(f"{where}: target upper bound disagrees.")
    return pre_state_bytes, result_bytes, upper_bound


def check_fixture(fixture: dict[str, Any]) -> None:
    """Recompute every frozen expectation; any disagreement is a typed failure."""
    if _required_member(fixture, "fixture_version") != _FIXTURE_VERSION:
        raise FixtureCheckError("fixture version is not the reviewed v1.")
    request = _required_member(fixture, "expected_request")
    expected = _required_member(fixture, "expected")
    targets_member = _required_member(fixture, "targets")
    if not isinstance(targets_member, list) or not targets_member:
        raise FixtureCheckError("a fixture requires at least one target.")
    targets = cast("list[object]", targets_member)

    wire = canonical_json_bytes(request)
    if wire != _required_member(expected, "wire_json_bytes"):
        raise FixtureCheckError("request wire_json_bytes disagrees with the frozen total.")

    per_item_violation = False
    aggregate = 0
    for ordinal, target in enumerate(targets):
        pre_state_bytes, result_bytes, upper_bound = _check_target(target, ordinal)
        if pre_state_bytes > _MAX_ITEM_BYTES or result_bytes > _MAX_ITEM_BYTES:
            per_item_violation = True
        aggregate += upper_bound

    if len(targets) != _required_member(expected, "action_count"):
        raise FixtureCheckError("action count disagrees with the frozen total.")
    if aggregate != _required_member(expected, "aggregate_upper_bound"):
        raise FixtureCheckError("aggregate upper bound disagrees with the frozen total.")

    violated = len(targets) > _MAX_ACTIONS or per_item_violation or aggregate > _MAX_AGGREGATE_BYTES
    outcome = _required_member(expected, "outcome")
    if outcome not in ("accepted", "refused"):
        raise FixtureCheckError("outcome must be accepted or refused.")
    if violated is not (outcome == "refused"):
        raise FixtureCheckError("the frozen outcome disagrees with the recomputed envelope.")


_FIXED_MEMBER_SCHEMA: Final = "FixedMemberDrainActivationByteFixtureV1"
_FRONTIER_SHARDS: Final = 32
_ELECTED_ACTIONS: Final = 51
_NO_ELIGIBLE_ACTIONS: Final = 42


def _member_target(member: object, ordinal: int) -> tuple[str, str, str]:
    where = f"expected_request.TransactItems[{ordinal}]"
    if not isinstance(member, dict) or len(cast("dict[str, object]", member)) != 1:
        raise FixtureCheckError(f"{where}: a member must carry exactly one action.")
    ((kind, body),) = cast("dict[str, Any]", member).items()
    if kind not in ("Put", "Update", "Delete", "ConditionCheck") or not isinstance(body, dict):
        raise FixtureCheckError(f"{where}: unsupported action {kind!r}.")
    key = cast("dict[str, Any]", body).get("Item" if kind == "Put" else "Key")
    if not isinstance(key, dict):
        raise FixtureCheckError(f"{where}: the action names no item.")
    names = cast("dict[str, Any]", key)
    try:
        return kind, str(names["pk"]["S"]), str(names["sk"]["S"])
    except KeyError, TypeError:
        raise FixtureCheckError(f"{where}: the action key is not a pk/sk string pair.") from None


def _check_target_keys(keyed: list[tuple[str, str, str]], targets: list[dict[str, Any]]) -> None:
    """Require every frozen pre-state and result image to carry its request member's key."""
    for ordinal, ((_kind, pk, sk), target) in enumerate(zip(keyed, targets, strict=True)):
        for name in ("expected_pre_state_map", "expected_result_map"):
            image = target[name]
            if image is not None and (image.get("pk") != {"S": pk} or image.get("sk") != {"S": sk}):
                raise FixtureCheckError(f"targets[{ordinal}].{name} carries another key.")


def check_fixed_member_fixture(fixture: dict[str, Any]) -> None:
    """Recompute a `FixedMemberDrainActivationByteFixtureV1` fixture independently.

    Beyond `check_fixture`'s totals: every target's frozen images carry its request member's
    key; the request starts with exactly the 32 live-frontier ConditionChecks and puts one
    `SERVING_TRANSITION#` record; an elected profile updates the domain head and puts a
    successor `COALESCE#`; the action count is exactly 51 elected or 42 `NO_ELIGIBLE` plus any
    declared one-beyond growth; and the largest target upper bound is recomputed.
    """
    check_fixture(fixture)
    if _required_member(fixture, "fixture_schema") != _FIXED_MEMBER_SCHEMA:
        raise FixtureCheckError("the fixture is not a fixed-member drain/activation fixture.")
    typed_input = _required_member(fixture, "typed_input")
    expected = _required_member(fixture, "expected")
    elected = _required_member(typed_input, "elected")
    extra = _required_member(typed_input, "extra_condition_checks")
    if not isinstance(elected, bool) or not isinstance(extra, int) or extra < 0:
        raise FixtureCheckError("typed_input must declare elected and extra_condition_checks.")
    members = cast("list[object]", _required_member(fixture["expected_request"], "TransactItems"))
    targets = cast("list[dict[str, Any]]", fixture["targets"])
    if len(members) != len(targets):
        raise FixtureCheckError("every request member needs exactly one frozen target.")
    keyed = [_member_target(member, ordinal) for ordinal, member in enumerate(members)]
    _check_target_keys(keyed, targets)
    frontier = keyed[:_FRONTIER_SHARDS]
    if any(
        kind != "ConditionCheck" or not pk.startswith("SERVING_FRONTIER#")
        for kind, pk, _ in frontier
    ):
        raise FixtureCheckError("the request must open with the 32 live-frontier checks.")
    if len({pk for _kind, pk, _sk in frontier}) != _FRONTIER_SHARDS:
        raise FixtureCheckError("the 32 live-frontier checks must name distinct shards.")
    transitions = [item for item in keyed if item[1].startswith("SERVING_TRANSITION#")]
    if [kind for kind, _pk, _sk in transitions] != ["Put"]:
        raise FixtureCheckError("the request must put exactly one transition record.")
    heads = [item for item in keyed if item[1].startswith("SERVING_DOMAIN#")]
    if [kind for kind, _pk, _sk in heads] != ["Update"]:
        raise FixtureCheckError("the request must update the domain head exactly once.")
    coalesce_puts = [item for item in keyed if item[0] == "Put" and item[2].startswith("COALESCE#")]
    if len(coalesce_puts) != (1 if elected else 0):
        raise FixtureCheckError("only an elected profile puts a successor COALESCE#.")
    base = _ELECTED_ACTIONS if elected else _NO_ELIGIBLE_ACTIONS
    if len(members) != base + extra:
        raise FixtureCheckError("the action count is not the fixed-member formula.")
    largest = max(int(target["target_upper_bound"]) for target in targets)
    if largest != _required_member(expected, "largest_target_upper_bound"):
        raise FixtureCheckError("the largest target upper bound disagrees.")


def load_fixture_tree(root: Path = FIXTURE_TREE) -> dict[str, dict[str, dict[str, Any]]]:
    """Load the committed fixture tree, requiring every branch of every operation.

    Returns ``{typed_operation: {branch: fixture}}``. A missing required branch,
    a stray file, or a fixture whose identity disagrees with its path fails.
    """
    if not root.is_dir():
        raise FixtureTreeError(f"fixture tree {root} is absent.")
    tree: dict[str, dict[str, dict[str, Any]]] = {}
    for operation_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        branches: dict[str, dict[str, Any]] = {}
        for fixture_path in sorted(operation_dir.glob("*.json")):
            branch, separator, version = fixture_path.stem.rpartition("-")
            if not separator or version != _FIXTURE_VERSION or not branch:
                raise FixtureTreeError(f"{fixture_path.name} is not a <branch>-v1.json fixture.")
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            if fixture.get("typed_operation") != operation_dir.name:
                raise FixtureTreeError(f"{fixture_path} disagrees with its operation directory.")
            if fixture.get("branch") != branch:
                raise FixtureTreeError(f"{fixture_path} disagrees with its branch name.")
            branches[branch] = fixture
        for required in REQUIRED_BRANCHES:
            if required not in branches:
                raise FixtureTreeError(
                    f"{operation_dir.name} is missing its required {required!r} branch."
                )
        tree[operation_dir.name] = branches
    if not tree:
        raise FixtureTreeError("the fixture tree holds no typed operation.")
    return tree
