# SPDX-License-Identifier: MIT-0
"""Every flow obligation is written with its root clock, so no live flow reads as unclocked.

The stuck-flow sweep (`domain.operations.stuck_flow.sweep`) counts a `FLOW_OBLIGATION_KINDS` item
without `root_clock_started_at` as `OpenFlowsWithoutRootClock`, which alarms. Acceptance x5
(2026-10-15) tripped that alarm on every live cohort because neither `COALESCE#` writer stamped a
clock. Two checks keep any writer from reopening the gap:

- a static inventory of every literal flow-obligation item in `src/`, which must stamp the clock
  (with a paired control that removes the stamp from each `COALESCE#` write and must fail);
- a behavioural pass through the real promoter election, alias drains and the fixed-member final
  drain on `FakeDynamo`, after which every due item of a flow kind carries a clock and the sweep
  reports zero unclocked flows (with a positive control that strips one and must report it).

Fictional data only; nothing here reaches AWS.
"""

from __future__ import annotations

import ast
import copy
import pathlib
from typing import Any

import pytest
from execution_graph_dispatch_support import NOW, ROOT_CLOCK, write_text
from fixed_member_byte_fixture_support import (
    DrainActivationWorld,
    FakeDynamo,
    capture_final_transaction,
    complete_direct_winner,
    drain_nonfinal_aliases,
    grant_and_publish,
    promoter_pass,
)

from adapters.outbound.dynamodb.due_shards import (
    _observation,  # pyright: ignore[reportPrivateUsage]
)
from domain.operations.execution_graph_dispatch import ServingCandidate, cohort_root_clock
from domain.operations.stuck_flow import (
    FLOW_OBLIGATION_KINDS,
    HoldState,
    sweep,
    unarmed_contribution_candidates,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_RELEASE = _SRC / "adapters" / "outbound" / "dynamodb" / "execution_graph_release.py"


def _prefix(node: ast.expr) -> str | None:
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def flow_obligation_writes(source: str) -> list[tuple[int, str, bool]]:
    """Each literal item keyed `**_key(<shard>, "<KIND>#...")` with a flow kind, and its stamp."""
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        kind: str | None = None
        stamped = False
        for key, value in zip(node.keys, node.values, strict=True):
            call = value if key is None and isinstance(value, ast.Call) else None
            name = getattr(call.func, "id", None) if call is not None else None
            if call is not None and name == "_key" and len(call.args) == 2:
                text = _prefix(call.args[1])
                if text and "#" in text and text.split("#")[0] in FLOW_OBLIGATION_KINDS:
                    kind = text.split("#")[0]
            if name == "root_clock_attribute":
                stamped = True
            if isinstance(key, ast.Constant) and key.value == "root_clock_started_at":
                stamped = True
        if kind is not None:
            found.append((node.lineno, kind, stamped))
    return found


def _inventory() -> dict[str, list[tuple[int, str, bool]]]:
    return {
        str(path.relative_to(_ROOT)): writes
        for path in sorted(_SRC.rglob("*.py"))
        if (writes := flow_obligation_writes(path.read_text(encoding="utf-8")))
    }


def test_every_literal_flow_obligation_writer_stamps_its_root_clock() -> None:
    inventory = _inventory()
    unstamped = [
        f"{path}:{line} {kind}#"
        for path, writes in inventory.items()
        for line, kind, stamped in writes
        if not stamped
    ]
    assert unstamped == []
    # The exact literal writers, so a writer that leaves the inventory fails here, not silently.
    # Helper- and update-expression writers (`due_obligation_item`'s ARM#/COMPLETE#/RELEASE#,
    # the ARM# update) are covered by the behavioural FakeDynamo pass below.
    kinds = {path: sorted(kind for _, kind, _ in writes) for path, writes in inventory.items()}
    dynamodb = "src/adapters/outbound/dynamodb/"
    assert kinds == {
        f"{dynamodb}execution_graph.py": ["FENCE", "INSTANTIATE", "WAITER"],
        f"{dynamodb}execution_graph_dispatch.py": [
            "FENCE",
            "INSTANTIATE",
            "OUTBOX",
            "OUTBOX",
            "VERIFY",
        ],
        f"{dynamodb}execution_graph_release.py": [
            "APPLY",
            "BAKE_RECOVERY",
            "COALESCE",
            "COALESCE",
            "OUTBOX",
            "OUTBOX",
            "OUTBOX",
        ],
    }


@pytest.mark.parametrize("which", [0, 1])
def test_the_inventory_fails_when_either_coalesce_stamp_is_removed(which: int) -> None:
    source = _RELEASE.read_text(encoding="utf-8")
    stamp = "**root_clock_attribute(cohort_root_clock("
    lines = source.splitlines(keepends=True)
    stamped = [index for index, line in enumerate(lines) if stamp in line]
    assert len(stamped) == 2
    del lines[stamped[which]]
    writes = flow_obligation_writes("".join(lines))
    missing = [kind for _, kind, ok in writes if not ok]
    assert missing == ["COALESCE"]


def _candidate(run: str, clock: str | None) -> ServingCandidate:
    return ServingCandidate(run, f"g-{run}", f"r-{run}", "d", 1, "e", root_clock_started_at=clock)


def test_the_cohort_root_clock_is_the_earliest_and_unknown_when_any_is_missing() -> None:
    later = _candidate("a", "2026-10-10T00:05:00+00:00")
    earlier = _candidate("b", "2026-10-10T00:01:00Z")
    assert cohort_root_clock((later, earlier)) == "2026-10-10T00:01:00Z"
    assert cohort_root_clock((later, _candidate("c", None))) is None
    assert cohort_root_clock(()) is None


_A_CLOCKS = (
    "2026-10-07T08:10:00+00:00",
    "2026-10-07T08:20:00+00:00",
    "2026-10-07T08:30:00+00:00",
)
_B_CLOCKS = ("2026-10-07T08:35:00+00:00", "2026-10-07T08:40:00+00:00")
"""Distinct clocks inside the ceiling; each cohort's earliest is not its winner's (the newest)."""


def _clocked_world() -> DrainActivationWorld:
    """`build_world(3, 2)` with distinct per-run root clocks set before each election."""
    fake = FakeDynamo()

    def grant(cohort: str, clocks: tuple[str, ...]) -> tuple[str, ...]:
        runs: list[str] = []
        for index, clock in enumerate(clocks):
            run = grant_and_publish(fake, cohort, index)
            fake.items[(f"RUN#{run}", "RUN")]["root_clock_started_at"] = write_text(clock)
            runs.append(run)
        return tuple(runs)

    a_runs = grant("a", _A_CLOCKS)
    promoter_pass(fake, "clocked-elect-a")
    b_runs = grant("b", _B_CLOCKS)
    complete_direct_winner(fake, a_runs[-1])
    drain_nonfinal_aliases(fake)
    return DrainActivationWorld(fake, a_runs, b_runs)


def _due_items(fake: Any) -> dict[str, list[dict[str, Any]]]:
    shards: dict[str, list[dict[str, Any]]] = {f"DUE#{index}": [] for index in range(32)}
    for (pk, _sk), item in fake.items.items():
        if pk in shards:
            shards[pk].append(item)
    return shards


def _reading(shards: dict[str, list[dict[str, Any]]]) -> Any:
    observed = {
        shard: tuple(_observation(shard, item) for item in items) for shard, items in shards.items()
    }
    # ADR-140: each unstamped COMPLETE#'s bake member is read; these tests measure root clocks
    # only, so every read is recorded as absent (no bake gauge is asserted here).
    every = [item for items in observed.values() for item in items]
    unarmed = dict.fromkeys(unarmed_contribution_candidates(every), (None, None))
    return sweep(observed, tuple(shards), HoldState(set=True), NOW, None, unarmed)


def _flow_items(shards: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        item
        for items in shards.values()
        for item in items
        if str(item["sk"]["S"]).split("#")[0] in FLOW_OBLIGATION_KINDS
    ]


def _coalesce(shards: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    (item,) = [i for i in _flow_items(shards) if i["sk"]["S"].startswith("COALESCE#")]
    return item


def _clock(item: dict[str, Any]) -> str:
    return str(item["root_clock_started_at"]["S"])


def _assert_exact_reading(shards: dict[str, list[dict[str, Any]]]) -> None:
    """Zero unclocked; past ceiling holds exactly the items still on the stale fixture clock."""
    reading = _reading(shards)
    assert reading.without_root_clock == ()
    stale = {i["sk"]["S"] for i in _flow_items(shards) if _clock(i) == ROOT_CLOCK}
    assert {observation.sk for observation in reading.past_ceiling} == stale
    assert _coalesce(shards)["sk"]["S"] not in stale


def test_a_live_elected_cohort_and_its_successor_carry_their_earliest_root_clock() -> None:
    world = _clocked_world()
    # A is BOUND_DRAINING at its final alias: its COALESCE# carries A's earliest clock, not the
    # winner's (`run.root_clock_started_at` of the newest member would be 08:30).
    during = _due_items(world.fake)
    assert _clock(_coalesce(during)) == min(_A_CLOCKS) != _A_CLOCKS[-1]
    assert all("root_clock_started_at" in item for item in _flow_items(during))
    _assert_exact_reading(during)
    # The fixed-member final drain elects B; B's COALESCE# carries B's earliest clock (08:35),
    # and B's winner bake OUTBOX# its own run's clock.
    assert capture_final_transaction(world) is not None
    after = _due_items(world.fake)
    assert _clock(_coalesce(after)) == min(_B_CLOCKS)
    winner = str(world.fake.items[(_head_key(), "HEAD")]["winner_run_id"]["S"])
    winner_clock = _clock(world.fake.items[(f"RUN#{winner}", "RUN")])
    assert winner_clock != min(_B_CLOCKS)
    bake = [
        i
        for i in _flow_items(after)
        if i["sk"]["S"].startswith("OUTBOX#")
        and i.get("run_id", {}).get("S") == winner
        and i.get("node", {}).get("S") == "bake"
    ]
    assert len(bake) == 1 and _clock(bake[0]) == winner_clock
    assert all("root_clock_started_at" in item for item in _flow_items(after))
    _assert_exact_reading(after)


def _head_key() -> str:
    from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN  # noqa: PLC0415

    return f"SERVING_DOMAIN#{INGESTION_RELEASE_DOMAIN}"


def test_an_unclocked_coalesce_obligation_is_still_reported() -> None:
    world = _clocked_world()
    shards = copy.deepcopy(_due_items(world.fake))
    (item,) = [i for i in _flow_items(shards) if i["sk"]["S"].startswith("COALESCE#")]
    del item["root_clock_started_at"]
    unclocked = _reading(shards).without_root_clock
    assert [observation.sk for observation in unclocked] == [item["sk"]["S"]]


def test_a_coalesce_keyed_to_the_winners_own_clock_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Paired control: stamping the winner's own clock (the newest member) instead of the
    # cohort's earliest passes the inventory and the unclocked check, but not the value check.
    from adapters.outbound.dynamodb import execution_graph_release as release  # noqa: PLC0415

    def winner_clock(candidates: Any) -> str | None:
        return max(candidate.root_clock_started_at for candidate in candidates)

    monkeypatch.setattr(release, "cohort_root_clock", winner_clock)
    world = _clocked_world()
    during = _due_items(world.fake)
    assert _reading(during).without_root_clock == ()
    assert _clock(_coalesce(during)) == _A_CLOCKS[-1] != min(_A_CLOCKS)
