# SPDX-License-Identifier: MIT-0
"""BF-5: signed stateless cursors and the `CURSOR_CAPACITY_LIMITED` cursor-admission refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from execution_explanation_support import FakeMac

from domain.operations.execution_cursor import (
    CURSOR_CAPACITY_LIMITED,
    CURSOR_FIELDS,
    MAX_CURSOR_BYTES,
    CursorCapacityLimitedError,
    CursorInvalidError,
    CursorState,
    admit_cursor,
    issue_cursor,
)
from domain.operations.generated_execution_graphs import BOUNDS

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-073")]

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts/operations/execution-graphs.yaml"
ROUTE = "execution-explanations/receipts"
FILTER = {"root_flow_id": "6a1f0f51-3f0e-5b5c-9a43-1f2d3c4b5a69"}
REVISION = "status-v2/1"


def _state(last: str = "RECEIPT#2026-10-07T09:00:00+00:00#p1-first#RECEIPT") -> CursorState:
    return CursorState(ROUTE, FILTER, REVISION, {"sort_key": last})


def _admit(token: str, mac: FakeMac, **overrides: object) -> CursorState:
    values: dict[str, object] = {"route": ROUTE, "filter_values": FILTER, "revision": REVISION}
    values.update(overrides)
    return admit_cursor(token, signer=mac, **values)  # type: ignore[arg-type]


def test_the_cursor_fields_and_bound_are_the_contracts() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    assert tuple(contract["cursor"]["fields"]) == CURSOR_FIELDS
    assert contract["cursor"]["stateless"] is True
    assert MAX_CURSOR_BYTES == contract["bounds"]["cursor_max_bytes"] == BOUNDS["cursor_max_bytes"]


def test_a_retry_returns_byte_identical_cursor_bytes() -> None:
    mac = FakeMac()

    first, second = issue_cursor(_state(), mac), issue_cursor(_state(), mac)

    assert first == second
    assert _admit(first, mac) == _state()


def test_an_oversize_incoming_continuation_is_refused_before_any_mac_or_decode() -> None:
    mac = FakeMac()
    oversize = "A" * (MAX_CURSOR_BYTES + 1)

    with pytest.raises(CursorCapacityLimitedError) as refused:
        _admit(oversize, mac)

    assert refused.value.signal == CURSOR_CAPACITY_LIMITED
    assert mac.calls == 0


def test_exactly_at_the_bound_is_admitted_to_decoding_and_one_over_is_not() -> None:
    mac = FakeMac()
    at_bound = "A" * MAX_CURSOR_BYTES

    with pytest.raises(CursorInvalidError):
        _admit(at_bound, mac)
    with pytest.raises(CursorCapacityLimitedError):
        _admit(at_bound + "A", mac)


def test_a_continuation_that_cannot_fit_is_refused_at_issue_with_no_mac() -> None:
    mac = FakeMac()

    with pytest.raises(CursorCapacityLimitedError):
        issue_cursor(_state("RECEIPT#" + "x" * MAX_CURSOR_BYTES), mac)

    assert mac.calls == 0


@pytest.mark.parametrize(
    "override",
    [
        {"route": "execution-explanations/graphs"},
        {"filter_values": {"root_flow_id": "00000000-0000-5000-8000-000000000000"}},
        {"revision": "status-v2/2"},
    ],
)
def test_a_cursor_for_another_route_filter_or_revision_is_invalid(
    override: dict[str, object],
) -> None:
    mac = FakeMac()
    token = issue_cursor(_state(), mac)

    with pytest.raises(CursorInvalidError):
        _admit(token, mac, **override)


def test_a_tampered_or_foreign_cursor_is_invalid() -> None:
    mac = FakeMac()
    token = issue_cursor(_state(), mac)
    payload, _, signature = token.partition(".")
    tampered = issue_cursor(_state("RECEIPT#9999"), mac).partition(".")[0] + "." + signature

    with pytest.raises(CursorInvalidError):
        _admit(tampered, mac)
    with pytest.raises(CursorInvalidError):
        _admit(token, FakeMac(b"another-fictional-key"))
    with pytest.raises(CursorInvalidError):
        _admit(payload, mac)
    with pytest.raises(CursorInvalidError):
        _admit(f"{payload}.{'!' * 43}", mac)
