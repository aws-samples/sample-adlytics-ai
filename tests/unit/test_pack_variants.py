# SPDX-License-Identifier: MIT-0
"""Declared pack variants: which one a period expects, and the previous period it reads (D9, D10).

The pure rule (`select_pack_variant`) is tested with a positive control beside every refusal; the
adapter that reads the previous period's cell-loss evidence is tested on a fake publication store.
Every value here is fictional.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from test_exposure_groups import (
    _csv,  # pyright: ignore[reportPrivateUsage]
    _delivery,  # pyright: ignore[reportPrivateUsage]
    _full,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.ingest_delivery.variant_evidence import (
    VariantEvidenceRequest,
    read_variant_evidence,
)
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.publication_selection import (
    PublicationScopeRequest,
    PublicationSelector,
    publication_scopes_for,
)
from adapters.outbound.source_pack import load_source_packs
from domain.curation.ingestion import DiscardPolicy
from domain.measurement.periodic import VariantMismatchError, active_variant
from domain.producer.models import VariantTrigger
from domain.producer.variants import (
    UNESTABLISHED,
    UNKNOWN_VARIANT,
    DeclaredVariant,
    PreviousPeriodEvidence,
    VariantRequest,
    previous_period_evidence,
    select_pack_variant,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import DateRange, TimeGrain
from ports.outbound.served_periods import ServedPeriodsRequest

if TYPE_CHECKING:
    from domain.semantics.publication import (
        PartitionIdentity,
        PublicationLensKey,
        PublicationScope,
        SuccessfulPublication,
    )
    from domain.semantics.validation import PublicationEvidence
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-110")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACK_ID = "amc-exposure-groups"
RULE = f"suppression:{PACK_ID}.cell-loss.v1"
MANUAL = DeclaredVariant("touch_only", 2, VariantTrigger.MANUAL)
COARSE = DeclaredVariant("coarse", 3, VariantTrigger.DEGRADE_NEXT_PERIOD)
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def _select(
    *declared: DeclaredVariant,
    requested: str | None = None,
    delivered: str | None = None,
    previous: PreviousPeriodEvidence | None = None,
) -> tuple[bool, str | None]:
    selection = select_pack_variant(
        VariantRequest(
            declared=declared, requested=requested, delivered=delivered, previous=previous
        )
    )
    return selection.accepted, selection.expected


def test_the_base_pack_is_expected_when_nothing_switched_a_variant_on() -> None:
    assert _select(MANUAL, COARSE) == (True, None)
    # Refused: an automatic variant nobody's cell loss made due.
    assert _select(MANUAL, COARSE, requested="coarse", delivered="coarse") == (False, None)


def test_a_manual_variant_is_accepted_only_when_the_registration_names_it() -> None:
    assert _select(MANUAL, requested="touch_only", delivered="touch_only") == (True, "touch_only")
    # Refused: the governed pack version binds the variant but the registration doesn't name it.
    assert _select(MANUAL, requested=None, delivered="touch_only") == (False, None)
    # Refused: an undeclared variant.
    assert _select(MANUAL, requested="other", delivered="other") == (False, None)


def test_a_degraded_previous_period_makes_the_automatic_variant_the_only_one_accepted() -> None:
    degraded = PreviousPeriodEvidence(variant=None, degraded=True)
    assert _select(COARSE, requested="coarse", delivered="coarse", previous=degraded) == (
        True,
        "coarse",
    )
    # Refused, naming the expected variant: the base pack once the switch is due.
    assert _select(COARSE, previous=degraded) == (False, "coarse")
    # Refused: a manual variant while an automatic one is due.
    assert _select(
        MANUAL, COARSE, requested="touch_only", delivered="touch_only", previous=degraded
    ) == (False, "coarse")
    # Control: a whole previous period keeps the base pack.
    assert _select(COARSE, previous=PreviousPeriodEvidence(None, degraded=False)) == (True, None)


def test_the_switch_is_sticky_once_a_period_was_delivered_under_the_automatic_variant() -> None:
    whole = PreviousPeriodEvidence(variant="coarse", degraded=False)
    assert _select(COARSE, requested="coarse", delivered="coarse", previous=whole) == (
        True,
        "coarse",
    )
    assert _select(COARSE, previous=whole) == (False, "coarse")


def test_an_unreadable_switch_state_accepts_no_delivery_of_an_automatic_pack() -> None:
    # Refused whichever binding arrives: the base pack, the automatic variant, a manual one.
    assert _select(COARSE, previous=UNESTABLISHED) == (False, UNKNOWN_VARIANT)
    assert _select(COARSE, requested="coarse", delivered="coarse", previous=UNESTABLISHED) == (
        False,
        UNKNOWN_VARIANT,
    )
    assert _select(
        MANUAL, COARSE, requested="touch_only", delivered="touch_only", previous=UNESTABLISHED
    ) == (False, UNKNOWN_VARIANT)
    # Controls: an established state accepts its delivery; a manual-only pack never reads one.
    assert _select(COARSE, previous=PreviousPeriodEvidence(None, degraded=False)) == (True, None)
    assert _select(
        MANUAL, requested="touch_only", delivered="touch_only", previous=UNESTABLISHED
    ) == (True, "touch_only")


@dataclass(frozen=True)
class _Result:
    rule_id: str
    detail: str


def _evidence(*results: _Result) -> PublicationEvidence:
    return cast("PublicationEvidence", SimpleNamespace(results=results))


def _record(grade: str, *slices: str) -> str:
    return json.dumps({"grade": grade, "slices": [{"grade": item, "slice": {}} for item in slices]})


def test_the_previous_period_reads_degraded_from_its_delivery_or_any_slice() -> None:
    read = previous_period_evidence
    assert read(_evidence(_Result(RULE, _record("degraded"))), RULE, None).degraded is True
    assert read(
        _evidence(_Result(RULE, _record("not_degraded", "not_degraded", "degraded"))), RULE, "x"
    ) == PreviousPeriodEvidence("x", degraded=True)
    # Control: a whole record reads not degraded.
    assert read(_evidence(_Result(RULE, _record("not_degraded", "not_degraded"))), RULE, None) == (
        PreviousPeriodEvidence(None, degraded=False)
    )
    # Fail closed: no record of the rule, an unparseable one, or one without a grade.
    assert read(_evidence(_Result("other", _record("degraded"))), RULE, None) == UNESTABLISHED
    assert read(_evidence(_Result(RULE, "not json")), RULE, None) == UNESTABLISHED
    assert read(_evidence(_Result(RULE, json.dumps({"slices": []}))), RULE, None) == UNESTABLISHED


@dataclass
class _Current:
    """A publication store holding CURRENT publications by lens; records every scope read."""

    held: dict[PublicationLensKey, list[SuccessfulPublication]]
    asked: list[PublicationLensKey] = field(default_factory=lambda: [])

    def current(self, partition: PartitionIdentity) -> SuccessfulPublication | None:
        del partition
        raise AssertionError("the variant check reads each series' scope")

    def current_exact(
        self, partitions: tuple[PartitionIdentity, ...]
    ) -> tuple[SuccessfulPublication, ...] | None:
        del partitions
        raise AssertionError("the variant check reads each series' scope")

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        self.asked.append(scope.lens)
        return tuple(self.held.get(scope.lens, ()))


def _request(
    current: _Current, *variants: DeclaredVariant
) -> tuple[VariantEvidenceRequest, PublicationLensKey, PublicationLensKey]:
    delivery = _delivery(_csv(*_full()))
    contract = next(c for _, c in load_contracts(ROOT) if c.contract_id == PACK_ID)
    packs = load_source_packs(ROOT)
    bridge = load_commerce_identity_bridge(ROOT)
    pack = next(item for item in packs if item.contract_id == PACK_ID)

    def lens(variant: str | None) -> PublicationLensKey:
        (scope,) = publication_scopes_for(
            PublicationScopeRequest(
                pack=pack,
                contract=contract,
                bridge=bridge,
                filters={"marketplace_key": delivery.manifest.marketplace},
                period_grain=TimeGrain.WEEK,
                variant=variant,
            )
        )
        return scope.lens

    request = VariantEvidenceRequest(
        manifest=delivery.manifest,
        contracts=(contract,),
        discard_policies=(DiscardPolicy(PACK_ID, PACK_ID, 2, variants),),
        packs=packs,
        bridge=bridge,
        publications=current,
    )
    return request, lens(None), lens("coarse")


# The delivery under test is the week of 2026-09-14; P-2, P-1 and P+1 are the weeks around it.
P_MINUS_2 = date(2026, 8, 31)
P_MINUS_1 = date(2026, 9, 7)
P_PLUS_1 = date(2026, 9, 21)


def _published(grade: str, start: date = P_MINUS_1) -> SuccessfulPublication:
    window = DateRange(start, date.fromordinal(start.toordinal() + 6))
    return cast(
        "SuccessfulPublication",
        SimpleNamespace(
            evidence=_evidence(_Result(RULE, _record(grade))),
            partition=SimpleNamespace(bucket=SimpleNamespace(grain=TimeGrain.WEEK, window=window)),
        ),
    )


def test_the_load_reads_the_automatic_variant_series_first_then_the_base() -> None:
    current = _Current({})
    request, base, coarse = _request(current, MANUAL, COARSE)
    current.held[base] = [_published("degraded")]
    assert read_variant_evidence(request) == {PACK_ID: PreviousPeriodEvidence(None, True)}
    assert current.asked == [coarse, base]
    # Sticky: the automatic variant's own series wins once it holds an earlier week.
    current.held[coarse] = [_published("not_degraded")]
    assert read_variant_evidence(request) == {PACK_ID: PreviousPeriodEvidence("coarse", False)}


def test_the_switch_holds_across_a_week_with_no_publication() -> None:
    # The automatic variant at P-2, nothing at P-1: a base delivery at P is refused.
    current = _Current({})
    request, base, coarse = _request(current, COARSE)
    current.held[coarse] = [_published("not_degraded", P_MINUS_2)]
    current.held[base] = [_published("not_degraded", date(2026, 8, 24))]
    (state,) = read_variant_evidence(request).values()
    assert state == PreviousPeriodEvidence("coarse", degraded=False)
    assert _select(COARSE, previous=state) == (False, "coarse")
    # Positive control: the automatic variant delivered at P after the same gap is accepted.
    assert _select(COARSE, requested="coarse", delivered="coarse", previous=state) == (
        True,
        "coarse",
    )


def test_a_degraded_base_week_before_a_gap_still_makes_the_variant_due() -> None:
    current = _Current({})
    request, base, _coarse = _request(current, COARSE)
    current.held[base] = [
        _published("not_degraded", date(2026, 8, 24)),
        _published("degraded", P_MINUS_2),
    ]
    (state,) = read_variant_evidence(request).values()
    assert _select(COARSE, previous=state) == (False, "coarse")
    # Control: the latest earlier base week whole keeps the base pack.
    current.held[base] = [
        _published("degraded", date(2026, 8, 24)),
        _published("not_degraded", P_MINUS_2),
    ]
    (state,) = read_variant_evidence(request).values()
    assert _select(COARSE, previous=state) == (True, None)


def test_a_later_period_never_decides_an_earlier_one() -> None:
    # Restating P after the switch at P+1: nothing before P switched, so the base pack is expected.
    current = _Current({})
    request, base, coarse = _request(current, COARSE)
    current.held[coarse] = [_published("not_degraded", P_PLUS_1)]
    current.held[base] = [_published("not_degraded")]
    (state,) = read_variant_evidence(request).values()
    assert _select(COARSE, previous=state) == (True, None)
    # Control: the same switch one week earlier is sticky for P.
    current.held[coarse] = [_published("not_degraded", P_MINUS_1)]
    (state,) = read_variant_evidence(request).values()
    assert _select(COARSE, previous=state) == (False, "coarse")


def test_an_unreadable_earlier_record_fails_closed() -> None:
    current = _Current({})
    request, base, _coarse = _request(current, COARSE)
    unreadable = cast(
        "SuccessfulPublication",
        SimpleNamespace(
            evidence=_evidence(_Result("other", "{}")),
            partition=_published("degraded").partition,
        ),
    )
    current.held[base] = [unreadable]
    assert read_variant_evidence(request) == {PACK_ID: UNESTABLISHED}
    # Control: the same week with its record reads established.
    current.held[base] = [_published("not_degraded")]
    assert read_variant_evidence(request) == {PACK_ID: PreviousPeriodEvidence(None, False)}


def test_a_pack_with_only_a_manual_variant_reads_no_previous_period() -> None:
    current = _Current({})
    request, base, _coarse = _request(current, MANUAL)
    current.held[base] = [_published("degraded")]
    assert read_variant_evidence(request) == {}
    assert current.asked == []
    # Control: with nothing published before, an automatic pack finds no evidence either.
    empty = _Current({})
    automatic, _base, _ = _request(empty, COARSE)
    assert read_variant_evidence(automatic) == {}
    assert len(empty.asked) == 2


# --- serving: a variant's rows are read from its own series -------------------------------------


def _scope_lens(filters: dict[str, str], variant: str | None = None) -> PublicationLensKey:
    contract = next(c for _, c in load_contracts(ROOT) if c.contract_id == PACK_ID)
    pack = next(item for item in load_source_packs(ROOT) if item.contract_id == PACK_ID)
    (scope,) = publication_scopes_for(
        PublicationScopeRequest(
            pack=pack,
            contract=contract,
            bridge=load_commerce_identity_bridge(ROOT),
            filters={"marketplace_key": "amazon_us", **filters},
            period_grain=TimeGrain.WEEK,
            variant=variant,
        )
    )
    return scope.lens


def test_a_plan_pinning_a_variant_row_kind_binds_that_variant_series() -> None:
    touch_only = _scope_lens({}, "touch_only")
    assert _scope_lens({"row_kind": "exposure"}) == touch_only
    assert _scope_lens({"row_kind": "outcome"}) == touch_only
    # Controls: base rows bind the base lens, and a conflicting explicit variant is refused.
    base = _scope_lens({})
    assert _scope_lens({"row_kind": "combined"}) == base != touch_only
    with pytest.raises(ValueError, match="filters pin variant touch_only"):
        _scope_lens({"row_kind": "exposure"}, "coarse")


@dataclass
class _ByLens:
    """CURRENT publications by lens, each a week bucket."""

    held: dict[PublicationLensKey, tuple[date, ...]]

    def current_for_scope(
        self, scope: PublicationScope, horizon: object = None
    ) -> tuple[SuccessfulPublication, ...]:
        del horizon
        return tuple(_published("not_degraded", start) for start in self.held.get(scope.lens, ()))


def test_served_periods_name_the_series_holding_each_period() -> None:
    contract = next(c for _, c in load_contracts(ROOT) if c.contract_id == PACK_ID)
    base, touch_only = _scope_lens({}), _scope_lens({}, "touch_only")
    store = _ByLens({base: (P_MINUS_2, P_MINUS_1), touch_only: (P_MINUS_1, P_PLUS_1)})
    selector = PublicationSelector(
        cast("CurrentPublicationInspectionPort", store),
        load_source_packs(ROOT),
        load_commerce_identity_bridge(ROOT),
    )
    served = selector.served_periods(
        ServedPeriodsRequest((contract,), {"marketplace_key": "amazon_us"}, NOW)
    )

    def week(start: date) -> CalendarPeriod:
        return CalendarPeriod(start, date.fromordinal(start.toordinal() + 6), TimeGrain.WEEK)

    # Base only, mixed, and touch-only only: each period names its series.
    assert served.series_of(PACK_ID, week(P_MINUS_2)) == (None,)
    assert served.series_of(PACK_ID, week(P_MINUS_1)) == (None, "touch_only")
    assert served.series_of(PACK_ID, week(P_PLUS_1)) == ("touch_only",)
    assert {period for _contract, period in served.publications} == {
        week(P_MINUS_2),
        week(P_MINUS_1),
        week(P_PLUS_1),
    }
    assert active_variant(served.series_of(PACK_ID, week(P_PLUS_1)), "w") == "touch_only"
    with pytest.raises(VariantMismatchError, match="PACK_VARIANT_MISMATCH"):
        active_variant(served.series_of(PACK_ID, week(P_MINUS_1)), "w")
    # Control: a contract declaring no variant records no series, its periods are the base's.
    assert served.series_of("amc-new-to-brand-buyers-platform", week(P_MINUS_2)) == (None,)
