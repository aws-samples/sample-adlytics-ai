# SPDX-License-Identifier: MIT-0
"""Audiences decoder parity: one case table, judged by the server here and the browser in Vitest.

The table (frontend/test/fixtures/audiences-parity-cases.json) is built from this module's
server-encoded fixtures and the server's own encoders, so the two decoders can't drift: every
case must get the same verdict from `require_audiences_artifact_payload(served=True)` and from
`parseAudienceProfileArtifact` (frontend/test/audiences-parity.test.ts); a campaign page case
gets the same verdict from `require_audiences_part_payload` and `parseAudiencesPart`. The rule
inventory is .agents/tasks/ui-audiences-retention/audiences-parity.md.
"""

from __future__ import annotations

import json
import pathlib
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
import test_segment_profile as profile
from test_audiences_presentation import NOW, _payload  # pyright: ignore[reportPrivateUsage]

from domain.capabilities.paid_features import PaidFeatureDisplay, PaidFeatureState
from domain.presentation.audiences import (
    BOUNDARY_TEXT,
    CAMPAIGN_PENDING_TEXT,
    DELIVERY_LABELS,
    NO_DELIVERY_TEXT,
    PAID_VIEW,
    SEGMENT_IDS_SENTENCE,
    SEGMENT_PARTIAL_TEXT,
    TOTAL_NOT_AVAILABLE_TEXT,
    VIEW_LABELS,
    AudiencesPart,
    _decimal,  # pyright: ignore[reportPrivateUsage]
    _money,  # pyright: ignore[reportPrivateUsage]
    _multiplier,  # pyright: ignore[reportPrivateUsage]
    _paid_period_text,  # pyright: ignore[reportPrivateUsage]
    _share,  # pyright: ignore[reportPrivateUsage]
    audiences_artifact_payload,
    require_audiences_artifact_payload,
    require_audiences_part_payload,
    resolve_audiences_payload,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.query import QueryRequest, QueryResult, QueryRow

pytestmark = [pytest.mark.unit]
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "frontend/test/fixtures"
BASE_FILES = {
    "complete": "audiences-complete-v2.json",
    "in_roster_unavailable": "audiences-in-roster-unavailable-v2.json",
    "campaign_page": "audiences-campaign-page-v1.json",
}
CASES_FILE = FIXTURES / "audiences-parity-cases.json"
MONTH = "September 2026"
Path = list[str | int]


def _served(payload: dict[str, object]) -> dict[str, object]:
    """Resolve a baked body at NOW and bind it as the route does, with a fixed reference."""
    served = resolve_audiences_payload(payload, NOW.date())
    served["page_context_label"] = f"Audiences · {MONTH}"
    served["page_context_expires_at"] = "2099-10-16T12:00:00+00:00"
    for market in cast("list[dict[str, Any]]", served["markets"]):
        for view in market["views"]:
            for delivery in view["deliveries"]:
                for insight in delivery["insights"]:
                    insight["artifact_revision"] = "r" * 43
    return served


def _in_roster_unavailable() -> dict[str, object]:
    """Bake a complete month whose In-market total didn't come back: an in-roster not_available.

    Both paid profiles and the DSP profile are published; Lifestyle reads segment 41001 and
    In-market's `profiled_total` row is missing, so its row is listed not available in the roster.
    """
    published = frozenset(
        (contract, profile.SEPTEMBER)
        for contract in (profile.INMARKET, profile.LIFESTYLE, profile.DSP_EXPOSED)
    ) | {(profile.PERFORMANCE, profile.SEPTEMBER)}
    renderer, store = profile._renderer(published)  # pyright: ignore[reportPrivateUsage]
    labels = renderer.segment_labels

    class _OwnNamespace:
        def labels(self, request: Any) -> Any:
            found = labels.labels(request)
            return {key: item for key, item in found.items() if item.namespace is request.namespace}

    renderer = replace(renderer, segment_labels=_OwnNamespace())  # type: ignore[arg-type]
    original = store.run
    lifestyle = {
        "segment_exposed_shoppers": 600,
        "segment_buyers": 54,
        "segment_buyers_new_to_brand": 21,
    }

    def run(request: QueryRequest) -> QueryResult:
        logical = request.plan.logical
        path = logical.filters.get("profile_path")
        if logical.group_by == ("marketplace_key",) and path == "asi_in_market":
            return replace(original(request), rows=())
        if path == "asi_lifestyle" and logical.group_by != ("marketplace_key",):
            value = lifestyle[logical.measures[0]]
            row: QueryRow = {"marketplace_key": profile.US, "segment_id": "41001"}
            row[profile.measure_alias(0)] = value
            return QueryResult(
                rows=(row,),
                bytes_scanned=1,
                truncated=False,
                suppression=profile.Suppression(rows_suppressed=False),
            )
        return original(request)

    object.__setattr__(store, "run", run)
    result = renderer.render_audiences(profile.AudiencesRenderInput(profile.MONTH))
    assert result.artifact is not None
    return _served(audiences_artifact_payload(result.artifact))


def _bases() -> dict[str, dict[str, object]]:
    return {name: json.loads((FIXTURES / file).read_text()) for name, file in BASE_FILES.items()}


def _get(value: Any, path: Path) -> Any:
    for key in path:
        value = value[key]
    return value


def _apply(base: dict[str, object], sets: list[dict[str, Any]]) -> dict[str, object]:
    candidate = deepcopy(base)
    for change in sets:
        *parents, last = change["path"]
        _get(candidate, parents)[last] = deepcopy(change["value"])
    return candidate


MARKET: Path = ["markets", 0]
PAID: Path = [*MARKET, "views", 0]
DSP: Path = [*MARKET, "views", 1]
DSP_ROW: Path = [*DSP, "rows", 0]
DSP_BELOW: Path = [*DSP, "rows", 1]
DSP_GAP: Path = [*DSP, "not_available", 0]
PAID_DELIVERY: Path = [*PAID, "deliveries", 0]
CAMPAIGNS: Path = [*MARKET, "campaign_audiences"]
CAMPAIGN_ENTRY: Path = [*CAMPAIGNS, "index", 0]
CAMPAIGN_ROW: Path = ["rows", 0]
"""A campaign page's first row: the cases on it use the `campaign_page` base."""
NA_ROW: Path = [*PAID, "rows", 0]
NA_DELIVERY: Path = [*PAID, "deliveries", 0]
REPLACED_IDS = (
    "DSP behaviour segments and Amazon audience segments (paid) share IDs, so they can be "
    "compared as one list."
)


def _cases() -> list[dict[str, Any]]:  # noqa: PLR0915 -- one table, every rule
    cases: list[dict[str, Any]] = []
    bases = _bases()

    def case(  # noqa: PLR0913 -- a case's name, verdict, changes, base and control
        name: str,
        accept: bool,
        *sets: tuple[Path, object],
        base: str = "complete",
        control: str | None = None,
    ) -> None:
        cases.append(
            {
                "name": name,
                "base": base,
                "accept": accept,
                "control": control,
                "set": [{"path": path, "value": value} for path, value in sets],
            }
        )

    def refuse(name: str, control: str, *sets: tuple[Path, object]) -> None:
        base = next(item["base"] for item in cases if item["name"] == control)
        case(name, False, *sets, base=base, control=control)

    def index(raw: str | None, text: str | None) -> tuple[tuple[Path, object], ...]:
        return (([*DSP_ROW, "segment_index"], raw), ([*DSP_ROW, "segment_index_text"], text))

    def cpm(raw: str, text: str) -> tuple[Path, object]:
        return ([*CAMPAIGN_ROW, "segment_cpm"], {"currency": "USD", "text": text, "value": raw})

    def roas(raw: str, text: str) -> tuple[tuple[Path, object], ...]:
        return (
            ([*CAMPAIGN_ROW, "segment_roas_attributed"], raw),
            ([*CAMPAIGN_ROW, "segment_roas_attributed_text"], text),
        )

    def share(value: object) -> tuple[Path, object]:
        return ([*DSP_ROW, "segment_buyer_share"], value)

    def server_share(value: str) -> tuple[Path, object]:
        return share(_share(Decimal(value)))

    complete, na = "complete as served", "in-roster unavailable as served"
    case(complete, True)
    case(na, True, base="in_roster_unavailable")
    page = "campaign page as served"
    case(page, True, base="campaign_page")

    # Canonical fixed-two-decimal index, CPM and ROAS: the server's own encoders accept.
    two_fifty = Decimal("2.5")
    case(
        "index 2.50 as the server writes it",
        True,
        *index(_decimal(two_fifty), _multiplier(two_fifty)),
    )
    refuse("index 2.5/2.5x", "index 2.50 as the server writes it", *index("2.5", "2.5x"))
    for raw in ("1.333", "01.33", "+1.33", "1.33E+0", "-1.33", " 1.33"):
        refuse(f"index {raw!r}/{raw!r}x", complete, *index(raw, f"{raw}x"))
    refuse("index display disagrees with raw", complete, *index("1.33", "1.34x"))
    # 29 digits: one past the longest coefficient the serializer's quantize can write.
    long_index = f"1{'0' * 26}.00"
    refuse("index with an over-long coefficient", complete, *index(long_index, f"{long_index}x"))
    refuse("index display without raw", complete, *index(None, "1.33x"))
    money = Decimal("12.5")
    case(
        "CPM 12.50 USD as the server writes it",
        True,
        cpm(_decimal(money, Decimal("0.01")) or "", _money(money, "USD") or ""),
        base="campaign_page",
    )
    refuse("CPM 12.5/12.5 USD", "CPM 12.50 USD as the server writes it", cpm("12.5", "12.5 USD"))
    refuse("CPM 7/7 USD", page, cpm("7", "7 USD"))
    refuse("CPM 7.000/7.000 USD", page, cpm("7.000", "7.000 USD"))
    refuse("CPM display names another currency", page, cpm("7.00", "7.00 EUR"))
    three = Decimal(3)
    case(
        "ROAS 3.00 as the server writes it",
        True,
        *roas(_decimal(three) or "", _multiplier(three) or ""),
        base="campaign_page",
    )
    refuse("ROAS 3/3x", "ROAS 3.00 as the server writes it", *roas("3", "3x"))
    refuse("ROAS 0.590/0.590x", page, *roas("0.590", "0.590x"))

    # Canonical share: four places, at most one, the server's half-even percent text.
    for value, label in (("1", "one"), ("0", "zero"), ("0.0005", "0.0005"), ("0.0015", "0.0015")):
        case(f"share {label} as the server writes it", True, server_share(value))
    for value, text in (("1.0001", "100.0%"), ("1.9999", "200.0%")):
        refuse(
            f"above-one share {value}",
            "share one as the server writes it",
            share({"text": text, "value": value}),
        )
    refuse(
        "negative zero share",
        "share zero as the server writes it",
        share({"text": "-0.0%", "value": "-0.0000"}),
    )
    refuse(
        "share 0.0005 rounded half up",
        "share 0.0005 as the server writes it",
        share({"text": "0.1%", "value": "0.0005"}),
    )
    refuse(
        "share 0.0015 rounded half down",
        "share 0.0015 as the server writes it",
        share({"text": "0.1%", "value": "0.0015"}),
    )
    for value, text in (("0.4", "40.0%"), ("0.40000", "40.0%"), ("0.4000", "40%")):
        refuse(
            f"noncanonical share {value}/{text}", complete, share({"text": text, "value": value})
        )

    # Pinned sentences at every profile view, the campaign field and the two boundary slots.
    refuse(
        "paid view segment-ID sentence replaced",
        complete,
        ([*PAID, "segment_ids_sentence"], REPLACED_IDS),
    )
    refuse(
        "DSP view segment-ID sentence replaced",
        complete,
        ([*DSP, "segment_ids_sentence"], REPLACED_IDS),
    )
    refuse(
        "campaign segment-ID sentence replaced",
        complete,
        ([*CAMPAIGNS, "segment_ids_sentence"], REPLACED_IDS),
    )
    boundary_copy = "This describes an audience Adlytics can activate for you."
    refuse(
        "boundary slot 1 replaced", complete, (["boundary"], [boundary_copy, SEGMENT_IDS_SENTENCE])
    )
    refuse("boundary slot 2 replaced", complete, (["boundary"], [BOUNDARY_TEXT, REPLACED_IDS]))
    refuse(
        "boundary slots swapped", complete, (["boundary"], [SEGMENT_IDS_SENTENCE, BOUNDARY_TEXT])
    )
    refuse(
        "boundary carries a third slot",
        complete,
        (["boundary"], [BOUNDARY_TEXT, SEGMENT_IDS_SENTENCE, boundary_copy]),
    )
    other_total = "Not available: the total is late."
    refuse("missing-total delivery text replaced", na, ([*NA_DELIVERY, "total_text"], other_total))
    refuse("missing-total delivery text absent", na, ([*NA_DELIVERY, "total_text"], None))
    refuse("in-roster unavailable reason replaced", na, ([*NA_ROW, "text"], other_total))
    refuse(
        "missing-total insight reason replaced",
        na,
        ([*NA_DELIVERY, "insights", 0, "absence_reason"], other_total),
    )

    # State <-> text <-> null couplings: rows against their delivery, deliveries against totals.
    refuse("in-roster unavailable without its reason", na, ([*NA_ROW, "text"], None))
    refuse(
        "in-roster unavailable with a share",
        na,
        ([*NA_ROW, "segment_buyer_share"], _share(Decimal("0.35"))),
    )
    refuse(
        "in-roster unavailable with an index",
        na,
        ([*NA_ROW, "segment_index"], "1.10"),
        ([*NA_ROW, "segment_index_text"], "1.10x"),
    )
    refuse("in-roster unavailable relabelled value", na, ([*NA_ROW, "state"], "value"))
    refuse(
        "missing-total delivery with totals",
        na,
        ([*NA_DELIVERY, "profiled_buyers_total"], 120),
        ([*NA_DELIVERY, "profiled_buyers_total_text"], "120"),
    )
    lifestyle_insight = _get(bases["in_roster_unavailable"], [*PAID, "deliveries", 1, "insights"])
    refuse(
        "missing-total delivery with a filled insight",
        na,
        ([*NA_DELIVERY, "insights"], lifestyle_insight),
    )
    refuse(
        "missing-total delivery called complete",
        na,
        ([*NA_DELIVERY, "total_returned"], True),
        ([*NA_DELIVERY, "total_text"], None),
    )
    refuse(
        "unavailable row in a delivery whose total came back",
        complete,
        ([*DSP_ROW, "state"], "not_available"),
        ([*DSP_ROW, "text"], TOTAL_NOT_AVAILABLE_TEXT),
        ([*DSP_ROW, "segment_buyer_share"], None),
        ([*DSP_ROW, "segment_exposed_share"], None),
        *index(None, None),
    )
    refuse("value row with text", complete, ([*DSP_ROW, "text"], "Measured."))
    refuse("value row without its index", complete, *index(None, None))
    refuse(
        "below-minimum row with an index",
        complete,
        ([*DSP_BELOW, "segment_index"], "0.82"),
        ([*DSP_BELOW, "segment_index_text"], "0.82x"),
    )
    refuse("below-minimum row without its reason", complete, ([*DSP_BELOW, "text"], None))
    refuse("roster gap without its reason", complete, ([*DSP_GAP, "text"], None))
    partial = "roster gap with the partial-figures reason"
    case(partial, True, ([*DSP_GAP, "text"], SEGMENT_PARTIAL_TEXT))
    refuse(
        "roster gap reason replaced",
        partial,
        ([*DSP_GAP, "text"], "Not available: Amazon was late with this segment."),
    )
    gap = _get(bases["complete"], DSP_GAP)
    row = _get(bases["complete"], DSP_ROW)
    paid_gaps = [{**gap, "profile_path": path} for path in ("asi_in_market", "asi_lifestyle")]
    ordered_gaps = "paid roster gaps in delivery order"
    case(ordered_gaps, True, ([*PAID, "not_available"], paid_gaps), base="in_roster_unavailable")
    refuse(
        "paid roster gaps out of delivery order",
        ordered_gaps,
        ([*PAID, "not_available"], paid_gaps[::-1]),
    )
    na_rows = _get(bases["in_roster_unavailable"], [*PAID, "rows"])
    refuse("paid rows out of delivery order", na, ([*PAID, "rows"], na_rows[::-1]))
    refuse(
        "roster gap in a delivery that didn't come back",
        complete,
        ([*PAID, "not_available"], [{**gap, "profile_path": "asi_in_market"}]),
    )
    refuse(
        "row in a delivery that didn't come back",
        complete,
        ([*PAID, "rows"], [{**row, "profile_path": "asi_in_market"}]),
    )
    refuse(
        "value row without its buyer count",
        complete,
        ([*DSP_ROW, "segment_buyers"], None),
        ([*DSP_ROW, "segment_buyers_text"], None),
    )

    # Observed and not-observed paid states, the callout and the default view.
    refuse(
        "observed paid view with a callout", complete, ([*PAID, "callout"], "Paid feature notice.")
    )
    refuse("DSP view with a callout", complete, ([*DSP, "callout"], "Paid feature notice."))
    refuse("paid default with no paid delivery", complete, ([*MARKET, "default_view"], PAID_VIEW))
    refuse(
        "DSP default while the paid view is observed and came back",
        na,
        ([*MARKET, "default_view"], "dsp_exposed_only"),
    )
    baked = PaidFeatureState(
        True, False, date(2026, 10, 16), DateRange(date(2026, 8, 1), date(2026, 8, 31))
    )
    missing_text = _paid_period_text(PaidFeatureDisplay.MISSING, MONTH, baked)
    case(
        "missing period text as the server writes it",
        True,
        ([*PAID_DELIVERY, "period_text"], missing_text),
    )
    for name, text in (
        (
            "missing period text naming another latest month",
            f"Figures for {MONTH} didn't arrive. The latest available is July 2026.",
        ),
        (
            "missing period text with a replaced suffix",
            f"Figures for {MONTH} didn't arrive. Ask your account team.",
        ),
        (
            "pending period text with another day",
            f"Figures for {MONTH} are expected after 17 Oct 2026.",
        ),
        ("unpublished period without its text", None),
    ):
        refuse(
            name,
            "missing period text as the server writes it",
            ([*PAID_DELIVERY, "period_text"], text),
        )
    # `PaidFeatureState`: a published period always names its latest available period.
    na_latest: Path = [*NA_DELIVERY, "paid_state", "latest_available_period"]
    refuse("published paid period without its latest period", na, (na_latest, None))
    refuse(
        "latest available period runs backwards",
        na,
        (na_latest, {"end": "2026-09-01", "start": "2026-09-30"}),
    )

    # Outcome couplings.
    refuse(
        "market outcome differs from the artifact's",
        complete,
        ([*MARKET, "outcome"], "insufficient"),
    )
    refuse("complete outcome without starter questions", complete, (["starter_questions"], []))
    insufficient: tuple[tuple[Path, object], ...] = (
        (["outcome"], "insufficient"),
        ([*MARKET, "outcome"], "insufficient"),
        (["starter_questions"], []),
    )
    with_text = "insufficient outcome with its display text"
    case(with_text, True, *insufficient, base="in_roster_unavailable")
    refuse("insufficient outcome with starter questions", with_text, *insufficient[:2])
    # Every delivery filled leaves no display text: complete accepts it, insufficient can't.
    lifestyle_delivery = _get(bases["in_roster_unavailable"], [*PAID, "deliveries", 1])
    all_filled = (
        ([*NA_DELIVERY], {**lifestyle_delivery, "profile_path": "asi_in_market"}),
        ([*PAID, "rows"], [_get(bases["in_roster_unavailable"], [*PAID, "rows", 1])]),
        ([*DSP, "deliveries", 0, "insights"], lifestyle_insight),
    )
    filled = "every delivery filled in a complete month"
    case(filled, True, *all_filled, base="in_roster_unavailable")
    refuse("insufficient outcome without display text", filled, *all_filled, *insufficient)
    refuse(
        "delivery with two insights",
        complete,
        ([*PAID_DELIVERY, "insights"], _get(bases["complete"], [*PAID_DELIVERY, "insights"]) * 2),
    )
    refuse(
        "unreturned delivery with its total returned",
        complete,
        ([*PAID_DELIVERY, "total_returned"], True),
    )
    refuse(
        "campaign CPM with its absence text",
        page,
        ([*CAMPAIGN_ROW, "cpm_text"], "Not available: no spend."),
    )
    refuse(
        "campaign ROAS with its absence text",
        page,
        ([*CAMPAIGN_ROW, "roas_text"], "Not available: no sales."),
    )
    refuse("campaign page of another revision", page, (["parent_revision"], "b" * 64))
    refuse("campaign page past its last", page, (["page"], 1))
    refuse("campaign page withdrawn with rows", page, (["state"], "withdrawn"), (["text"], "Gone."))
    pending = CAMPAIGN_PENDING_TEXT.format(month=MONTH)
    case(
        "pending campaigns with no index",
        True,
        ([*CAMPAIGNS, "pending_text"], pending),
        ([*CAMPAIGNS, "campaign_count"], 0),
        ([*CAMPAIGNS, "index"], []),
    )
    refuse(
        "pending campaigns with an index",
        "pending campaigns with no index",
        ([*CAMPAIGNS, "pending_text"], pending),
    )

    # Numbers, dates, names and references the two decoders read alike.
    refuse("display minimum as a boolean", complete, (["display_minimum"], True))
    refuse(
        "count above the browser's exact integers",
        complete,
        ([*DSP_ROW, "segment_exposed_shoppers"], 2**53),
    )
    refuse(
        "basic-format expected-after date",
        complete,
        ([*PAID_DELIVERY, "paid_state", "expected_after"], "20261016"),
    )
    refuse("unpadded month start", complete, (["month", "start"], "2026-9-01"))
    # Cross-runtime primitives: ASCII digits only, year 1 onward, one shared blank rule.
    refuse("index with Arabic-Indic digits", complete, *index("1.\u0663\u0663", "1.33x"))
    refuse(
        "expected-after date with Arabic-Indic digits",
        complete,
        ([*PAID_DELIVERY, "paid_state", "expected_after"], "\u0662\u0660\u0662\u0666-10-16"),
    )
    refuse("year-zero month start", complete, (["month", "start"], "0000-09-01"))
    refuse("view label of a byte-order mark", complete, ([*DSP, "label"], "\ufeff"))
    refuse("view label of an information separator", complete, ([*DSP, "label"], "\x1c"))
    refuse("blank segment display name", complete, ([*DSP_ROW, "display_name"], " "))
    refuse(
        "unknown provider label", complete, ([*DSP_ROW, "provider_label"], "Provider not recorded")
    )
    refuse("blank campaign label", complete, ([*CAMPAIGN_ENTRY, "campaign_label"], " "))
    # Campaign identity: an opaque reference by rank, never the protected canonical key.
    first = _get(bases["complete"], CAMPAIGN_ENTRY)
    second = {**first, "campaign_label": "Unnamed campaign 1", "campaign_ref": "campaign-2"}
    two = "two campaigns as the server writes them"
    case(two, True, ([*CAMPAIGNS, "index"], [first, second]), ([*CAMPAIGNS, "campaign_count"], 2))
    refuse(
        "campaign references skip a rank",
        two,
        ([*CAMPAIGNS, "index"], [first, {**second, "campaign_ref": "campaign-3"}]),
        ([*CAMPAIGNS, "campaign_count"], 2),
    )
    refuse(
        "an index whose count isn't its entries",
        two,
        ([*CAMPAIGNS, "index"], [first, second]),
        ([*CAMPAIGNS, "campaign_count"], 3),
    )
    refuse(
        "an index entry whose pages don't match its segments",
        complete,
        ([*CAMPAIGN_ENTRY, "page_count"], 2),
    )
    refuse(
        "an available entry with a note",
        complete,
        ([*CAMPAIGN_ENTRY, "text"], "Not shown."),
    )
    case(
        "a refused entry as the server writes it",
        True,
        ([*CAMPAIGN_ENTRY, "state"], "refused"),
        ([*CAMPAIGN_ENTRY, "text"], "Its history isn't available."),
        ([*CAMPAIGN_ENTRY, "page_count"], 0),
    )
    refuse(
        "a refused entry with pages",
        "a refused entry as the server writes it",
        ([*CAMPAIGN_ENTRY, "state"], "refused"),
        ([*CAMPAIGN_ENTRY, "text"], "Its history isn't available."),
    )
    refuse(
        "campaign reference is its canonical key",
        complete,
        ([*CAMPAIGN_ENTRY, "campaign_ref"], "camp-1"),
    )
    refuse(
        "campaign entry carries its canonical key",
        complete,
        ([*CAMPAIGN_ENTRY], {**first, "campaign_key": "camp-1"}),
    )
    refuse(
        "market carries its canonical marketplace key",
        complete,
        ([*MARKET], {**_get(bases["complete"], MARKET), "marketplace_key": "amazon_us"}),
    )
    refuse("blank view label", complete, ([*DSP, "label"], ""))
    refuse(
        "served insight not bound to a page reference",
        complete,
        ([*PAID_DELIVERY, "insights", 0, "artifact_revision"], "r" * 42),
    )
    slot = {
        "missing_dates": [{"end": "2026-09-30", "start": "2026-09-01"}],
        "reason_text": NO_DELIVERY_TEXT,
        "recovery_text": None,
        "role_label": "This month",
        "slot_label": f"{VIEW_LABELS[PAID_VIEW]} · {DELIVERY_LABELS['asi_in_market']}",
    }
    case("evidence slot as the server writes it", True, (["evidence_status", "slots"], [slot]))
    refuse(
        "evidence slot without its reason",
        "evidence slot as the server writes it",
        (["evidence_status", "slots"], [{**slot, "reason_text": " "}]),
    )
    refuse(
        "evidence slot with an unpadded date",
        "evidence slot as the server writes it",
        (
            ["evidence_status", "slots"],
            [{**slot, "missing_dates": [{"end": "2026-09-30", "start": "2026-9-1"}]}],
        ),
    )
    refuse(
        "evidence slot with a reversed period",
        "evidence slot as the server writes it",
        (
            ["evidence_status", "slots"],
            [{**slot, "missing_dates": [{"end": "2026-09-01", "start": "2026-09-30"}]}],
        ),
    )
    refuse("month runs backwards", complete, (["month", "end"], "2026-08-31"))
    refuse("evidence status version 1", complete, (["evidence_status", "version"], 1))

    # Structure and identity: strict key rosters, cardinality, ordered rosters, served context.
    refuse("artifact carries an unknown field", complete, (["note"], "extra"))
    refuse("profile row carries an unknown field", complete, ([*DSP_ROW, "note"], "extra"))
    refuse("artifact type replaced", complete, (["type"], "AudienceProfile"))
    refuse("artifact version 1", complete, (["version"], 1))
    refuse("artifact naming no publication revision", complete, (["revision"], "r" * 43))
    refuse("artifact version as a boolean", complete, (["version"], True))
    # Scalar type and closed vocabularies, one representative each (audiences-parity §5).
    refuse(
        "DSP delivery returned flag as a number",
        complete,
        ([*DSP, "deliveries", 0, "returned"], 1),
    )
    refuse(
        "artifact outcome outside its vocabulary",
        complete,
        (["outcome"], "partial"),
        ([*MARKET, "outcome"], "partial"),
    )
    refuse(
        "DSP view with the paid view's observed state", complete, ([*DSP, "paid_state"], "observed")
    )
    market = _get(bases["complete"], MARKET)
    refuse("artifact serves two markets", complete, (["markets"], [market, market]))
    paid_view, dsp_view = _get(bases["complete"], [*MARKET, "views"])
    refuse("market views swapped", complete, ([*MARKET, "views"], [dsp_view, paid_view]))
    refuse("market carries one view", complete, ([*MARKET, "views"], [paid_view]))
    in_market, lifestyle = _get(bases["complete"], [*PAID, "deliveries"])
    refuse("paid deliveries reordered", complete, ([*PAID, "deliveries"], [lifestyle, in_market]))
    refuse("paid delivery missing", complete, ([*PAID, "deliveries"], [in_market]))
    insight: Path = [*PAID_DELIVERY, "insights", 0]
    refuse("insight id replaced", complete, ([*insight, "id"], "segment-under-index"))
    refuse("insight output class replaced", complete, ([*insight, "output_class"], "finding"))
    refuse("insight state replaced", complete, ([*insight, "state"], "pending"))
    refuse(
        "silent insight without its absence class", complete, ([*insight, "absence_class"], None)
    )
    refuse(
        "filled insight with an absence class",
        na,
        ([*PAID, "deliveries", 1, "insights", 0, "absence_class"], "below_minimum"),
    )
    filled_insight: Path = [*PAID, "deliveries", 1, "insights", 0]
    refuse("filled insight without its text", na, ([*filled_insight, "text"], None))
    refuse("served page context label blank", complete, (["page_context_label"], " "))
    refuse("served page context expiry blank", complete, (["page_context_expires_at"], ""))
    return cases


def test_in_roster_unavailable_fixture_is_the_servers_exact_served_payload() -> None:
    fixture = json.loads((FIXTURES / BASE_FILES["in_roster_unavailable"]).read_text())
    assert fixture == _in_roster_unavailable()
    paid = fixture["markets"][0]["views"][0]
    assert (fixture["outcome"], fixture["markets"][0]["default_view"]) == ("complete", PAID_VIEW)
    assert [(row["profile_path"], row["state"]) for row in paid["rows"]] == [
        ("asi_in_market", "not_available"),
        ("asi_lifestyle", "value"),
    ]


def test_parity_table_is_the_committed_browser_table() -> None:
    assert json.loads(CASES_FILE.read_text()) == _cases()


def test_every_accepting_case_has_a_refusing_control_and_every_refusal_names_one() -> None:
    cases = _cases()
    accepting = {item["name"] for item in cases if item["accept"]}
    controls = {item["control"] for item in cases if not item["accept"]}
    assert len({item["name"] for item in cases}) == len(cases)
    assert controls == accepting
    assert all(item["control"] is None for item in cases if item["accept"])


def _judge(base: str, candidate: dict[str, object]) -> None:
    if base == "campaign_page":
        parent = _bases()["complete"]["revision"]
        require_audiences_part_payload(candidate, AudiencesPart.CAMPAIGN, cast("str", parent))
        if (candidate["campaign_ref"], candidate["page"]) != ("campaign-1", 0):
            raise ValueError("the part is not the one asked for")
        return
    require_audiences_artifact_payload(candidate, served=True)


@pytest.mark.parametrize("item", _cases(), ids=lambda item: item["name"])
def test_server_served_validator_gives_each_parity_case_its_verdict(item: dict[str, Any]) -> None:
    candidate = _apply(_bases()[item["base"]], item["set"])
    if item["accept"]:
        _judge(item["base"], candidate)
    else:
        with pytest.raises(ValueError):
            _judge(item["base"], candidate)


def test_baked_complete_payload_is_the_served_fixtures_source() -> None:
    # Control for the table's first base: it's the route fixture, not a hand-written copy.
    assert _served(_payload()) == _bases()["complete"]
