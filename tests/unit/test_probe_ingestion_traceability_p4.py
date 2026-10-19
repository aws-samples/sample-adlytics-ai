# SPDX-License-Identifier: MIT-0
"""P4's attribution invariant (R2-16), decided over fictional readbacks."""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Mapping

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_ingestion_traceability_p4 as p4  # noqa: E402

RELEASE_LIVE_TESTS = p4.RELEASE_LIVE_TESTS
attribution_verdict = p4.attribution_verdict

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]


def _semantic(publication: str, publisher: str = "canonical-curation") -> dict[str, object]:
    return {
        "partition_key": {"S": "fictional-partition"},
        "record_key": {"S": "CURRENT"},
        "publication": {"M": {"publisher": {"S": publisher}, "id": {"S": publication}}},
    }


def _artifact(revision: str) -> dict[str, object]:
    return {"page_variant_key": {"S": "page#7d"}, "revision": {"S": revision}}


def _verdict(  # noqa: PLR0913 -- one fixture builder
    before: Mapping[str, Mapping[str, object]],
    after: Mapping[str, Mapping[str, object]],
    *,
    census: dict[str, int] | None = None,
    backfill: float = 0,
) -> dict[str, object]:
    return attribution_verdict(
        {"semantic": before.get("semantic", {}), "artifact": before.get("artifact", {})},  # type: ignore[arg-type]
        {"semantic": after.get("semantic", {}), "artifact": after.get("artifact", {})},  # type: ignore[arg-type]
        census_before={"RECEIPT": 9},
        census_after=census or {"RECEIPT": 9},
        backfill_invocations=backfill,
    )


def test_a_legacy_publication_and_the_bake_it_triggers_are_attributed() -> None:
    verdict = _verdict(
        {"semantic": {"p": _semantic("a")}, "artifact": {"v": _artifact("r1")}},
        {"semantic": {"p": _semantic("b")}, "artifact": {"v": _artifact("r2")}},
    )
    assert verdict["passed"] is True
    assert verdict["changed_counts"] == {"semantic": 1, "artifact": 1}


def test_no_change_is_trivially_attributed() -> None:
    same = {"semantic": {"p": _semantic("a")}, "artifact": {"v": _artifact("r1")}}
    assert _verdict(same, same)["passed"] is True


def test_a_pointer_change_by_another_writer_fails() -> None:
    verdict = _verdict(
        {"semantic": {"p": _semantic("a")}}, {"semantic": {"p": _semantic("b", "x")}}
    )
    assert verdict["passed"] is False
    assert verdict["unattributed"] == ["semantic:p"]


def test_a_changed_pointer_carrying_probe_identity_fails() -> None:
    verdict = _verdict(
        {"semantic": {"p": _semantic("a")}},
        {"semantic": {"p": _semantic("integration-probe-b")}},
    )
    assert verdict["probe_identity"] == ["semantic:p"]
    assert verdict["passed"] is False


def test_an_artifact_change_without_a_publication_or_backfill_fails_and_with_backfill_passes() -> (
    None
):
    before = {"artifact": {"v": _artifact("r1")}}
    after = {"artifact": {"v": _artifact("r2")}}
    assert _verdict(before, after)["passed"] is False
    assert _verdict(before, after, backfill=1)["passed"] is True


def test_any_live_release_family_fails_before_graph() -> None:
    verdict = _verdict({}, {}, census={"RECEIPT": 9, "GRANT": 1})
    assert verdict["live_release_families"] == {"GRANT": 1}
    assert verdict["passed"] is False


def test_the_release_live_file_count_is_pinned() -> None:
    assert RELEASE_LIVE_TESTS == 6
