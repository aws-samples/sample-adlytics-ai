# SPDX-License-Identifier: MIT-0
"""Prompt shipper state polling and terminal success semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import ship_prompts as ship  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]


class Registry:
    """Return one queued create followed by a still-pending status projection."""

    def post(self, route: str, _body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if route == "create-version":
            return 202, {"version": 3, "outcome": "pending"}
        if route == "versions":
            return 200, {"versions": [{"version": 3, "calibration": "pending"}]}
        raise AssertionError(f"unexpected route {route}")


def test_pending_after_poll_budget_is_not_reported_as_success_without_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digests = SimpleNamespace(
        foundation="a" * 64,
        tier_core="b" * 64,
        slot_block="c" * 64,
        combined="d" * 64,
    )

    def composed(_slot: str, _tier: str, _composition: Any = None) -> Any:
        return SimpleNamespace(text="x", digests=digests)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ship, "_composed", composed)
    monkeypatch.setattr(ship.time, "sleep", no_sleep)

    ok, line = ship._ship_one(  # pyright: ignore[reportPrivateUsage]
        cast("Any", Registry()),
        "contribution",
        "card",
        activate=False,
        max_retries=1,
    )

    assert not ok
    assert "calibration=pending" in line


_DIGESTS = {"foundation": "a" * 64, "tier_core": "b" * 64, "slot_block": "c" * 64}


class SettledRegistry:
    """A create that settles at once, a read-back with the stored digests, and a roster."""

    def __init__(self, stored: dict[str, str]) -> None:
        self.stored = stored
        self.routes: list[str] = []

    def post(self, route: str, _body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.routes.append(route)
        if route == "create-version":
            return 202, {"version": 7, "outcome": "pending"}
        if route == "versions":
            return 200, {
                "versions": [
                    {
                        "version": 7,
                        "calibration": "passed",
                        "fragment_digests": self.stored,
                        "calibration_cases": [{"kind": "known-correct", "outcome": "passed"}],
                    }
                ]
            }
        if route == "slots":
            return 200, {"slots": [{"slot_id": "contribution", "active_deep_version": 5}]}
        raise AssertionError(f"unexpected route {route}")


def _fake_composed(monkeypatch: pytest.MonkeyPatch) -> None:
    digests = SimpleNamespace(**_DIGESTS, combined="d" * 64)

    def composed(_slot: str, _tier: str, _composition: Any = None) -> Any:
        return SimpleNamespace(text="Fictional prompt text.", digests=digests)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ship, "_composed", composed)
    monkeypatch.setattr(ship.time, "sleep", no_sleep)


def test_the_record_names_the_create_and_its_read_back_and_never_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_composed(monkeypatch)
    entries: list[dict[str, object]] = []
    composition = ship.Composition("day-label", "1" * 40, "2" * 40, "2" * 40)
    ok, _ = ship._ship_one(  # pyright: ignore[reportPrivateUsage]
        cast("Any", SettledRegistry(dict(_DIGESTS))),
        "contribution",
        "deep",
        activate=False,
        max_retries=3,
        composition=composition,
        record=entries.append,
    )
    assert ok
    [entry] = entries
    assert entry["arm"] == "day-label"
    assert entry["version"] == 7
    assert entry["create_status"] == 202
    assert entry["outcome"] == "passed"
    assert entry["readback_fragment_digests_match"] is True
    assert entry["active_version_before"] == 5
    assert entry["calibration_cases"] == [{"kind": "known-correct", "outcome": "passed"}]
    composed = cast("dict[str, dict[str, str]]", entry["composition"])
    assert composed["foundation"] == {
        "path": "foundation.md",
        "revision": "1" * 40,
        "digest": "a" * 64,
    }
    assert composed["tier_core"]["path"] == "tiers/deep.md"
    assert composed["slot_block"]["path"] == "slots/contribution.md"
    body = {
        "slot_id": "contribution",
        "prompt_template": "Fictional prompt text.",
        "model_routing_hint": "deep",
        "tier": "deep",
        "fragment_digests": _DIGESTS,
    }
    assert entry["body_bytes"] == len(json.dumps(body).encode())
    assert "Fictional prompt text." not in json.dumps(entry)


def test_a_read_back_with_other_digests_is_recorded_as_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_composed(monkeypatch)
    entries: list[dict[str, object]] = []
    ship._ship_one(  # pyright: ignore[reportPrivateUsage]
        cast("Any", SettledRegistry({**_DIGESTS, "slot_block": "e" * 64})),
        "contribution",
        "deep",
        activate=False,
        max_retries=3,
        record=entries.append,
    )
    assert entries[0]["readback_fragment_digests_match"] is False


def test_a_compose_file_is_strict_and_resolves_every_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolved: list[str] = []

    def git(*args: str) -> str:
        resolved.append(args[-1])
        return "f" * 40 + "\n"

    monkeypatch.setattr(ship, "_git", git)
    path = tmp_path / "arm.json"
    revisions = {"foundation": "HEAD", "tier_core": "237df7c", "slot_block": "237df7c"}
    path.write_text(json.dumps({"arm": "day-label", "revisions": revisions}))
    composition = ship.load_composition(path)
    assert composition == ship.Composition("day-label", "f" * 40, "f" * 40, "f" * 40)
    assert resolved == ["HEAD^{commit}", "237df7c^{commit}", "237df7c^{commit}"]
    for bad in (
        {"arm": "x", "revisions": {"foundation": "HEAD"}},
        {"arm": "", "revisions": revisions},
        {"arm": "x", "revisions": {**revisions, "slot_block": "--output=/tmp/x"}},
        {"arm": "x", "revisions": revisions, "extra": 1},
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(SystemExit):
            ship.load_composition(path)


def test_a_composition_reads_each_fragment_from_its_own_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[str] = []

    def git(*args: str) -> str:
        shown.append(args[1])
        return f"text of {args[1]}"

    monkeypatch.setattr(ship, "_git", git)
    composition = ship.Composition("demand-per-unit", "old", "old", "new")
    composed = ship._composed("demand-shape", "card", composition)  # pyright: ignore[reportPrivateUsage]
    assert shown == [
        "old:contracts/prompts/insights/foundation.md",
        "old:contracts/prompts/insights/tiers/card.md",
        "new:contracts/prompts/insights/slots/demand-shape.md",
    ]
    assert "text of new:contracts/prompts/insights/slots/demand-shape.md" in composed.text
    # The working tree is the default and reads no revision.
    shown.clear()
    ship._composed("demand-shape", "card")  # pyright: ignore[reportPrivateUsage]
    assert shown == []


def test_the_registry_re_mints_its_session_before_the_access_token_expires() -> None:
    now = [0.0]
    minted: list[str] = []

    def remint() -> str:
        minted.append("x")
        return f"fictional-{len(minted)}"

    registry = ship.Registry(
        "https://example.invalid", "fictional-0", "csrf", remint=remint, clock=lambda: now[0]
    )
    assert registry._session_cookie().startswith("adl_session=fictional-0;")  # pyright: ignore[reportPrivateUsage]
    now[0] = ship.SESSION_REUSE_SECONDS - 1
    assert registry._session_cookie().startswith("adl_session=fictional-0;")  # pyright: ignore[reportPrivateUsage]
    now[0] = ship.SESSION_REUSE_SECONDS
    assert registry._session_cookie().startswith("adl_session=fictional-1;")  # pyright: ignore[reportPrivateUsage]
    assert ship.SESSION_REUSE_SECONDS < 15 * 60
