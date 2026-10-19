# SPDX-License-Identifier: MIT-0
"""Pack 5, `amc-exposure-groups`: declared cells, load checks, residual and the dictionary.

Every delivery here is fictional. The load runs the real ingestion path (assessment, then the
sidecar) on in-memory tooling, as the pack 1 tests do.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

import pytest
from publication_support import bound_plan
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
)
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarOutcome
from adapters.outbound.athena.composition import compose
from adapters.outbound.metric_dictionary import load_catalog
from adapters.producer.packs import (
    discard_policies,
    installed_governance,
    load_query_packs,
    pack_by_id,
)
from domain.catalog.capability import MetricKind
from domain.curation.landing import GOVERNED_MANIFEST_LAYOUT, LandedObject
from domain.measurement.planning import (
    FactShape,
    PlanRequest,
    QueryPlan,
    _currency_source,  # pyright: ignore[reportPrivateUsage]
    plan,
)
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange
from ports.outbound.canonical import CanonicalDestination

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099", "ADR-101", "ADR-102")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/generated"
PACK_ID = "amc-exposure-groups"
FACT = "fct_exposure_group_outcome"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
WEEK = (date(2026, 9, 14), date(2026, 9, 20))
WINDOW = "2026-09-14T00:00:00.000Z,2026-09-21T00:00:00.000Z"
HEADER = (
    "exposure_source_set,identity_space,row_kind,shoppers_reached,impressions,dsp_window_spend,"
    "sponsored_window_spend,dsp_currency,dsp_currency_min,sponsored_currency,"
    "sponsored_currency_min,search_shoppers,detail_page_view_shoppers,purchasers,ntb_purchasers,"
    "purchases,revenue_incl_halo,currency_code,purchase_currency_min,marketplace_name,"
    "time_window_start,time_window_end,distinct_user_count,suppression_reason,rows_suppressed"
)


def _row(  # noqa: PLR0913 -- one fictional cell, by keyword
    source_set: str,
    space: str,
    *,
    shoppers: int = 1000,
    purchasers: int = 50,
    sponsored_min: str = "USD",
    row_kind: str = "combined",
) -> str:
    dsp = "USD" if "dsp" in source_set else ""
    sponsored = "USD" if "sponsored" in source_set else ""
    return (
        f"{source_set},{space},{row_kind},{shoppers},{shoppers * 6},"
        f"{'600000' if dsp else '0'},{'120000000' if sponsored else '0'},"
        f"{dsp},{dsp},{sponsored},{sponsored_min if sponsored else ''},"
        f"100,300,{purchasers},20,60,1950.00,USD,USD,AMAZON.COM,{WINDOW},2,,false"
    )


WITHHELD = (
    f"sponsored,,combined,3,18,0,137156329,,,,,2,2,2,1,,,,,,{WINDOW},1,"
    "1 'sponsored_ads_traffic' (path 2 of 3) users < 2 required,true"
)
"""A per-path withheld row with three shoppers (plan D19): the sponsored path held fewer than two
users. Its distinct counts and exposure-side sums survive; purchase sums and currency are NULL."""
GRID = tuple(
    (group, space)
    for group in ("dsp", "sponsored", "dsp+sponsored")
    for space in ("account", "device_or_browser")
)


def _csv(*lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADER, *lines))


def _full(**overrides: Mapping[str, object]) -> list[str]:
    return [_row(group, space, **overrides.get(f"{group}/{space}", {})) for group, space in GRID]  # type: ignore[arg-type]


def _delivery(text: str, *, variant: str | None = None, named: str | None = None) -> LoadedDelivery:
    """Land one pack 5 delivery over the week, governed by the base pack or a named variant.

    `named` is the variant its options name, which is `variant` unless a test makes them differ.
    """
    body = text.encode()
    declaration = json.loads((FIXTURES / f"{PACK_ID}.manifest.json").read_text())
    pack = pack_by_id(ROOT, PACK_ID)
    binding = pack if variant is None else pack.variant_binding(variant)
    option = variant if named is None else named
    options = {"period_grain": "week", **({} if option is None else {"pack_variant": option})}
    manifest = build_manifest(
        BuildManifestRequest(
            declaration={
                **declaration,
                "option_set": options,
                "period_start": WEEK[0].isoformat(),
                "period_end": WEEK[1].isoformat(),
                "source_watermark": (WEEK[1] + timedelta(days=16)).isoformat(),
            },
            objects=(LandedObject("data-0001.csv", len(body), checksum_of(body)),),
            declared_columns=declared_columns_of(text, source_format="csv"),
        )
    )
    manifest = replace(
        manifest, governance=binding.governance, layout_version=GOVERNED_MANIFEST_LAYOUT
    )
    return LoadedDelivery(
        manifest=manifest,
        manifest_key=manifest.manifest_key,
        text=text,
        landed_ref=LandedDeliveryRef(
            manifest.manifest_key,
            "manifest-v1",
            "sha256:manifest",
            manifest.object_keys()[0],
            "object-v1",
            manifest.objects[0].checksum,
        ),
    )


@dataclass
class _Loaded:
    outcome: SemanticSidecarOutcome
    detail: str
    rows: tuple[dict[str, object], ...]
    residual: tuple[dict[str, object], ...]
    loss: str | None
    contract_rule: str = ""
    """The candidate's contract-assertion detail, as its evidence records it."""


def _load(delivery: LoadedDelivery) -> _Loaded:
    _request, tooling, _order, _attempts = _case()
    packs = load_query_packs(ROOT)
    results = ingest_semantic_delivery(
        SemanticIngestionRequest(
            delivery=delivery,
            contracts=tuple(
                contract for _, contract in load_contracts(ROOT) if contract.contract_id == PACK_ID
            ),
            columns=load_registry(ROOT),
            now=NOW,
            operation_id="exposure-groups-unit",
            execution_id="exposure-groups-execution",
            plan_digest="sha256:exposure-groups-plan",
            installed_governance=installed_governance(packs),
            discard_policies=discard_policies(packs),
        ),
        tooling,
    )
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    candidates = tuple(
        item for item in canonical.requests if item.destination is CanonicalDestination.CANDIDATE
    )

    def rows(dataset: str) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(row)
            for item in candidates
            if item.publication.partition.scope.dataset.value == dataset
            for row in item.rows
        )

    loss = next(
        (
            result.detail
            for item in candidates
            for result in item.publication.evidence.results
            if result.rule_id == f"suppression:{PACK_ID}.cell-loss.v1"
        ),
        None,
    )
    contract_rule = next(
        (
            result.detail
            for item in candidates
            for result in item.publication.evidence.results
            if result.rule_id == f"contract.{PACK_ID}.v1"
        ),
        "",
    )
    return _Loaded(
        results[0].outcome,
        results[0].detail,
        rows(FACT),
        rows(f"{FACT}_withheld"),
        loss,
        contract_rule,
    )


def _cell(rows: tuple[dict[str, object], ...], group: str, space: str) -> dict[str, object]:
    (row,) = (
        row for row in rows if row["exposure_group"] == group and row["identity_space"] == space
    )
    return row


def test_a_full_grid_publishes_six_cells_with_canonical_exposure_groups() -> None:
    loaded = _load(_delivery(_csv(*_full())))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert len(loaded.rows) == 6
    both = _cell(loaded.rows, "dsp_and_sponsored", "account")
    assert (both["exposure_source_set"], both["row_kind"], both["cell_state"]) == (
        "dsp+sponsored",
        "combined",
        "returned",
    )
    assert (both["shoppers_reached"], both["impressions"], both["purchasers"]) == (1000, 6000, 50)
    # Each raw cost scaled by its own column's divisor, with its own currency (design §5.5).
    assert str(both["dsp_window_spend"]) == "6.0000"
    assert str(both["sponsored_window_spend"]) == "1.2000"
    assert (both["dsp_currency"], both["sponsored_currency"], both["currency_code"]) == (
        "USD",
        "USD",
        "USD",
    )
    assert loaded.residual == ()


def test_a_per_path_withheld_cell_is_not_returned_its_slice_graded_and_its_sums_withheld() -> None:
    """Plan D19: a withheld row of three shoppers is discarded like any withheld row."""
    lines = [line for line in _full() if not line.startswith("sponsored,device_or_browser")]
    loaded = _load(_delivery(_csv(*lines, WITHHELD)))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    gap = _cell(loaded.rows, "sponsored_only", "device_or_browser")
    assert gap["cell_state"] == "not_returned"
    assert all(
        gap[column] is None
        for column in ("shoppers_reached", "impressions", "purchasers", "revenue_incl_halo")
    )
    # Graded per identity space, never withdrawn: the account slice is whole.
    record = json.loads(loaded.loss or "{}")
    assert record["discarded_rows"] == 1
    assert {
        item["slice"]["identity_space"]: (item["not_returned_cells"], item["grade"])
        for item in record["slices"]
    } == {"account": (0, "not_degraded"), "device_or_browser": (1, "degraded")}
    # Its additive exposure-side sums go to the residual; its money came back without a
    # currency, so it isn't summed and reads withheld (FEAT-002 rule); distinct counts never.
    (residual,) = loaded.residual
    assert residual["row_kind"] == "combined"
    assert residual["impressions"] == 18
    assert residual.get("sponsored_window_spend") is None
    assert residual.get("purchases") is None
    assert residual.get("shoppers_reached") is None
    # Paired control: the same withheld row keeping its sponsored currency is summed.
    kept = WITHHELD.replace("137156329,,,,,", "137156329,,,USD,USD,", 1)
    control = _load(_delivery(_csv(*lines, kept)))
    assert control.outcome is SemanticSidecarOutcome.PUBLISHED, control.detail
    (summed,) = control.residual
    assert summed["sponsored_window_spend"] is not None
    assert summed["impressions"] == 18


def test_a_currency_disagreement_refuses_the_delivery() -> None:
    refused = _load(_delivery(_csv(*_full(**{"dsp+sponsored/account": {"sponsored_min": "CAD"}}))))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "CURRENCY_DISAGREEMENT" in refused.contract_rule
    assert "sponsored_currency 'USD' != sponsored_currency_min 'CAD'" in refused.contract_rule
    # Paired control: the same grid in one currency publishes.
    assert _load(_delivery(_csv(*_full()))).outcome is SemanticSidecarOutcome.PUBLISHED


def test_every_reached_account_row_with_no_purchaser_refuses_the_delivery() -> None:
    groups = ("dsp", "sponsored", "dsp+sponsored")
    nobody = {f"{group}/account": {"purchasers": 0} for group in groups}
    refused = _load(_delivery(_csv(*_full(**nobody))))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "MARKETPLACE_FILTER_SUSPECTED" in refused.contract_rule
    # Paired control: one account row with a purchaser publishes.
    some = dict(list(nobody.items())[:2])
    assert _load(_delivery(_csv(*_full(**some)))).outcome is SemanticSidecarOutcome.PUBLISHED


def test_a_variant_delivery_the_registration_does_not_name_is_refused() -> None:
    """Plan D10: a base-pack delivery naming a variant in its options is a mismatch."""
    refused = _load(_delivery(_csv(*_full()), named="touch_only"))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "PACK_VARIANT_MISMATCH" in refused.contract_rule
    assert "the registration names variant touch_only" in refused.contract_rule


def test_a_named_manual_touch_only_delivery_fills_its_own_cells() -> None:
    exposure = [_row(group, space, row_kind="exposure", purchasers=50) for group, space in GRID]
    groups = ("dsp", "sponsored", "dsp+sponsored")
    outcome = [_row(group, "account", row_kind="outcome") for group in groups]
    loaded = _load(_delivery(_csv(*exposure, *outcome), variant="touch_only"))
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    assert sorted({str(row["row_kind"]) for row in loaded.rows}) == ["exposure", "outcome"]
    assert len(loaded.rows) == 9
    # Control: an outcome row on device or browser IDs isn't a declared cell of the variant.
    stray = _row("dsp", "device_or_browser", row_kind="outcome")
    refused = _load(_delivery(_csv(*exposure, *outcome, stray), variant="touch_only"))
    assert refused.outcome is SemanticSidecarOutcome.BLOCKED
    assert "CELL_VALUE_UNDECLARED" in refused.contract_rule


def test_a_touch_only_delivery_keeps_withheld_exposure_and_outcome_rows_apart() -> None:
    """Plan D11: the residual carries row_kind, one row per withheld branch of one delivery."""
    exposure = [
        _row(group, space, row_kind="exposure")
        for group, space in GRID
        if (group, space) != ("sponsored", "device_or_browser")
    ]
    outcome = [_row(group, "account", row_kind="outcome") for group in ("dsp", "dsp+sponsored")]
    withheld_exposure = WITHHELD.replace(",combined,", ",exposure,", 1)
    withheld_outcome = (
        f"sponsored,,outcome,3,,,,,,,,2,2,2,1,4,80.00,USD,USD,,{WINDOW},1,"
        "1 'sponsored_ads_traffic' (path 2 of 3) users < 2 required,true"
    )
    loaded = _load(
        _delivery(
            _csv(*exposure, *outcome, withheld_exposure, withheld_outcome), variant="touch_only"
        )
    )
    assert loaded.outcome is SemanticSidecarOutcome.PUBLISHED, loaded.detail
    by_kind = {str(row["row_kind"]): row for row in loaded.residual}
    assert sorted(by_kind) == ["exposure", "outcome"]
    assert (by_kind["exposure"]["impressions"], by_kind["exposure"].get("purchases")) == (18, None)
    assert by_kind["outcome"].get("impressions") is None
    assert by_kind["outcome"]["purchases"] == 4
    assert str(by_kind["outcome"]["revenue_incl_halo"]) == "80.0000"
    assert by_kind["outcome"]["currency_code"] == "USD"
    # Control: only the exposure branch withheld leaves one residual row, the exposure one.
    only = _load(_delivery(_csv(*exposure, *outcome, withheld_exposure), variant="touch_only"))
    assert only.outcome is SemanticSidecarOutcome.PUBLISHED, only.detail
    assert [row["row_kind"] for row in only.residual] == ["exposure"]


# --- dictionary --------------------------------------------------------------------------------

E1_METRICS = (
    "search_shoppers_in_window",
    "detail_page_view_shoppers_in_window",
    "purchasers_in_window",
    "ntb_purchasers_in_window",
    "purchases_in_window",
    "revenue_incl_halo_in_window",
    "search_rate",
    "purchaser_rate",
    "revenue_per_exposed_shopper",
    "purchaser_rate_index_vs_sponsored_only",
)


def test_every_pack_5_metric_is_causal_false_and_every_rate_reads_account_ids() -> None:
    catalog = load_catalog(ROOT)
    pack5 = (
        "shoppers_reached",
        "impressions_in_window",
        "overlap_share",
        "overlap_share_all_ids",
        "dsp_only_reach_share",
        *E1_METRICS,
    )
    for name in pack5:
        metric = catalog.metric(name)
        assert metric is not None, name
        assert metric.causal is False, name
    for name in ("search_rate", "purchaser_rate", "revenue_per_exposed_shopper"):
        metric = catalog.metric(name)
        assert metric is not None
        assert metric.requires == (("identity_space", "account"),)
        refused = catalog.refusal_for(
            name, grain="exposure_group", filters={"identity_space": "device_or_browser"}
        )
        assert refused is not None
        assert "reads Amazon account IDs only" in refused.explanation
    # Control: a metric with no causal flag declares none.
    roas = catalog.metric("roas_attributed_14d")
    assert roas is not None and roas.causal is None
    # The search metric is never called branded (plan D13).
    assert all("branded" not in (catalog.metric(name).label or "").lower() for name in pack5)  # type: ignore[union-attr]
    assert all(catalog.metric(name).kind in set(MetricKind) for name in pack5)  # type: ignore[union-attr]


# --- window spend, one metric per currency ------------------------------------------------------


def _window_plan(metric: str) -> QueryPlan:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    return plan(
        load_catalog(ROOT),
        PlanRequest(
            metric,
            ("marketplace", "exposure_group", "identity_space"),
            DateRange(*WEEK),
            {
                "marketplace_key": "amazon_us",
                "time_grain": "week",
                "grain_set": "platform",
                "row_kind": "combined",
            },
        ),
        FactShape.for_fact(FACT, contracts),
    )


def test_each_window_spend_is_labelled_from_its_own_currency_column() -> None:
    """The design's one window spend is split (owner decision B2-5): no figure adds currencies."""
    dsp = _window_plan("dsp_window_spend_on_shoppers")
    sponsored = _window_plan("sponsored_window_spend_on_shoppers")
    assert (dsp.measures, dsp.currency_source) == (("dsp_window_spend",), "dsp_currency")
    assert (sponsored.measures, sponsored.currency_source) == (
        ("sponsored_window_spend",),
        "sponsored_currency",
    )
    composed = compose(bound_plan(dsp, NOW), max_rows=10)
    assert '"dsp_currency" AS "currency_code"' in composed.sql
    assert 'GROUP BY "marketplace_key", "exposure_group", "identity_space", "dsp_currency"' in (
        composed.sql
    )
    # Control: revenue keeps the fact's own currency_code, selected and grouped as itself.
    revenue = _window_plan("revenue_incl_halo_in_window")
    assert revenue.currency_source == "currency_code"
    sql = compose(bound_plan(revenue, NOW), max_rows=10).sql
    assert '"currency_code"' in sql
    assert ' AS "currency_code"' not in sql
    # The design's summed metric no longer exists, so nothing can ask for the two added.
    assert load_catalog(ROOT).metric("window_spend_on_shoppers") is None


def test_a_plan_combining_measures_of_two_currency_columns_is_refused() -> None:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    shape = FactShape.for_fact(FACT, contracts)
    metric = load_catalog(ROOT).metric("dsp_window_spend_on_shoppers")
    assert metric is not None
    with pytest.raises(DomainError, match="carries its own currency column"):
        _currency_source(metric, shape, ("dsp_window_spend", "sponsored_window_spend"))
    # Control: one measure reads its own column; two default-currency measures read currency_code.
    assert _currency_source(metric, shape, ("dsp_window_spend",)) == "dsp_currency"
    assert _currency_source(metric, shape, ("revenue_incl_halo", "purchases")) == "currency_code"
