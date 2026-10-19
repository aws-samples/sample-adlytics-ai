# SPDX-License-Identifier: MIT-0
"""PB-2: the SCD2 merge orders by observed date, never by load order (S4).

The risk named at HEAD is `valid_from` assigned "by comparing loads"
(`src/domain/data_contracts/contract.py`, `assigned_by_load`). The merge that assigns it is
`build_dimension_history` (`src/domain/semantics/dimension_history.py`), driven by the sidecar's
fold (`semantic_sidecar.py`, `_folded_dimension_history`):

- every version opens at the observation's bucket boundary, the start of its observed window,
  and a changed observation at or before the current version's `valid_from` is refused, so an
  older observation merged after a newer one never becomes current;
- the fold's base is the scope's published history at or before the first folded bucket
  (`_previous_history_rows`), chosen by observed window, not by which partition loaded last;
- the only dimension phase B lands is `dim_campaign`, whose packs bucket by the source business
  date. `dim_product` buckets by the raw manifest's first-observed instant, and phase B never
  lands it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from phase_b_support import ROOT, phase_b

from adapters.jobs.ingest_delivery import semantic_sidecar
from domain.semantics.dimension_history import (
    OPEN_ENDED,
    DimensionHistoryRequest,
    build_dimension_history,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

OLD = datetime(2026, 8, 1, tzinfo=UTC)
NEW = datetime(2026, 9, 1, tzinfo=UTC)


def _merge(previous: tuple[Any, ...], name: str, boundary: datetime) -> tuple[Any, ...]:
    observation = (("campaign", name), ("campaign_key", "c-fictional-1"))
    return build_dimension_history(
        DimensionHistoryRequest(
            previous=previous,
            observations=(observation,),
            business_key=("campaign_key",),
            tracked_attributes=("campaign",),
            boundary=boundary,
        )
    ).rows


def _current(rows: tuple[Any, ...]) -> str:
    current = [dict(row) for row in rows if dict(row)["is_current"] is True]
    assert len(current) == 1
    assert current[0]["valid_to"] == OPEN_ENDED
    return str(current[0]["campaign"])


def test_pb2_the_newer_observation_stays_current() -> None:
    history = _merge((), "Old name", OLD)
    history = _merge(history, "New name", NEW)
    assert _current(history) == "New name"


def test_pb2_an_older_observation_merged_after_a_newer_one_does_not_become_current() -> None:
    history = _merge(_merge((), "Old name", OLD), "New name", NEW)
    with pytest.raises(ValueError, match="must follow the current version"):
        _merge(history, "Old name again", OLD)
    assert _current(history) == "New name"


def test_pb2_the_fold_base_is_chosen_by_observed_window_not_by_load_order() -> None:
    def published(end: date, loaded: int) -> Any:
        return SimpleNamespace(
            partition=SimpleNamespace(bucket=SimpleNamespace(window=SimpleNamespace(end=end))),
            loaded=loaded,
        )

    # The September partition loaded first and the August one last.
    september, august = published(date(2026, 9, 30), 1), published(date(2026, 8, 31), 2)
    reads: list[Any] = []

    def current_for_scope(scope: object) -> tuple[Any, ...]:
        del scope
        return (september, august)

    def rows(item: Any) -> tuple[()]:
        reads.append(item)
        return ()

    operation = SimpleNamespace(
        item=SimpleNamespace(load=SimpleNamespace(contract=SimpleNamespace(contract_id="c"))),
        partition=SimpleNamespace(scope="scope"),
        tooling=SimpleNamespace(
            publications=SimpleNamespace(current_for_scope=current_for_scope),
            canonical_reader=SimpleNamespace(rows=rows),
        ),
    )
    bucket = SimpleNamespace(window=SimpleNamespace(end=date(2026, 9, 5)))

    semantic_sidecar._previous_history_rows(operation, bucket)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]

    assert reads == [august]


def _packs() -> dict[str, dict[str, Any]]:
    document = json.loads((ROOT / "contracts" / "v2" / "source-packs.json").read_text())
    return {str(pack["contractId"]): pack for pack in document["sourcePacks"]}


def test_pb2_every_dimension_phase_b_lands_buckets_by_its_source_business_date() -> None:
    packs = _packs()
    dimensions = {
        family.split("/")[-1].removesuffix(".csv")
        for family in {item.fixture.split("#", 1)[0] for item in phase_b()}
    } & {cid for cid, pack in packs.items() if pack["role"] == "dimension"}
    assert dimensions == {"amc-campaign-dsp", "amc-campaign-sponsored"}
    for contract in dimensions:
        assert packs[contract]["time"]["observationBasis"] == "source_business_date"
    first_observed = {
        cid
        for cid, pack in packs.items()
        if pack["time"]["observationBasis"] == "system_first_observed"
    }
    assert first_observed == {"vendor-product-hierarchy"}
    assert not any("vendor-product-hierarchy" in item.fixture for item in phase_b())
