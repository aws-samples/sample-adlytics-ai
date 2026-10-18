# SPDX-License-Identifier: MIT-0
"""Build-time bakeability gate for declared artifact-served preset windows."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import bakeability
from adapters.jobs.preset_artifact_publisher.audiences_source import (
    audiences_bake_plan_count,
    audiences_marketplace_count,
)
from adapters.jobs.preset_artifact_publisher.bakeability import (
    BAKE_RESIDENT_SHARE,
    BAKER_LAMBDA_TIMEOUT_SECONDS,
    BAKER_MEMORY_BYTES,
    BAKER_TIMEOUT_SECONDS,
    CAMPAIGN_GOVERNED_CHANNEL_COUNT,
    CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE,
    CAMPAIGN_MAX_FILTER_SIBLINGS,
    CERTIFIED_QUERY_CAP,
    MAX_BAKE_BODY_COUNT,
    MAX_BAKE_TOTAL_BYTES,
    MAX_PRESET_SNAPSHOT_PARTITIONS,
    PER_OBJECT_WRITE_MS,
    PER_QUERY_SECONDS,
    ArtifactServedPage,
    BakeStorageEstimate,
    artifact_served_pages,
    audiences_bake_duration_ms,
    audiences_page_plan_count,
    audiences_row_budget,
    audiences_runtime_campaign_bound,
    audiences_storage_estimate,
    audiences_timeout_headroom_campaigns,
    campaign_bake_object_count,
    certify_roster_tasks,
    evaluate_bakeability,
    roster_plan_counts,
)
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    campaign_portfolio_bake_plan_count,
)
from adapters.jobs.preset_artifact_publisher.new_customers_source import (
    new_customers_bake_plan_count,
)
from adapters.jobs.preset_artifact_publisher.reach_source import reach_bake_plan_count
from adapters.jobs.preset_artifact_publisher.retail_source import retail_health_bake_plan_count
from adapters.jobs.preset_artifact_publisher.retention_source import (
    RETENTION_MARKET_MAX_ENCODED_BYTES,
    STANDARD_METRICS,
    retention_bake_body_bytes,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import scorecard_bake_plan_count
from adapters.outbound.media_taxonomy import load_media_taxonomy
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.page_schemas import TemporalPresetId
from domain.semantics.identity import ChannelKey
from ports.outbound.semantic_state import MAX_EXACT_CURRENT_PARTITIONS

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-070")]

ROOT = Path(__file__).resolve().parents[2]

# The largest count whose waves and tail still fit the baker timeout; one more plan adds a wave.
_TAIL_SECONDS = 30
_LARGEST_FITTING_PLAN_COUNT = (
    (BAKER_TIMEOUT_SECONDS - _TAIL_SECONDS) // PER_QUERY_SECONDS
) * CERTIFIED_QUERY_CAP

_STORAGE = {
    "campaigns": BakeStorageEstimate(
        MAX_BAKE_BODY_COUNT,
        CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE,
        MAX_BAKE_BODY_COUNT * CAMPAIGN_MAX_ENCODED_BODY_ESTIMATE,
    ),
    "retail-health": BakeStorageEstimate(2, 256_000, 512_000),
    "scorecard": BakeStorageEstimate(1, 256_000, 256_000),
    "new-customers": BakeStorageEstimate(1, 256_000, 256_000),
    "reach": BakeStorageEstimate(1, 256_000, 256_000),
    "retention": BakeStorageEstimate(
        1, retention_bake_body_bytes(ROOT), retention_bake_body_bytes(ROOT)
    ),
    "audiences": audiences_storage_estimate(1, audiences_row_budget(1)),
    "synthetic": BakeStorageEstimate(2, 256_000, 512_000),
    "over-declared": BakeStorageEstimate(2, 256_000, 512_000),
    "unregistered": BakeStorageEstimate(2, 256_000, 512_000),
}


def test_todays_declared_presets_and_real_bake_batches_pass_the_gate() -> None:
    """The committed declarations and the real batch builders satisfy ADR-070 point 8."""
    pages = artifact_served_pages(load_v2_page_schemas(ROOT))

    assert {page.page_id for page in pages} == {
        "campaigns",
        "retail-health",
        "scorecard",
        "new-customers",
        "reach",
        "retention",
        "audiences",
    }
    # ADR-127 declares 30d on Campaigns: 2 x 30 day-partitions per pack fit one verified read.
    campaigns = next(page for page in pages if page.page_id == "campaigns")
    assert TemporalPresetId.THIRTY_DAYS in campaigns.candidate_presets
    assert MAX_PRESET_SNAPSHOT_PARTITIONS == MAX_EXACT_CURRENT_PARTITIONS == 100
    findings = evaluate_bakeability(
        pages,
        {
            "campaigns": campaign_portfolio_bake_plan_count(ROOT, 30),
            "retail-health": retail_health_bake_plan_count(ROOT, 14),
            "scorecard": scorecard_bake_plan_count(ROOT),
            "new-customers": new_customers_bake_plan_count(ROOT),
            "reach": reach_bake_plan_count(ROOT),
            "retention": len(STANDARD_METRICS),
            "audiences": audiences_bake_plan_count(
                audiences_marketplace_count(ROOT),
                audiences_runtime_campaign_bound(audiences_marketplace_count(ROOT)),
            ),
        },
        _STORAGE,
    )
    assert findings == ()
    # One marketplace: 4 channel figures x 2 windows, 3 models x (headline x 2 + ranking),
    # TACoS x 2 (UI slice 9a).
    assert scorecard_bake_plan_count(ROOT) == 19
    # One marketplace on a calendar preset: five step-1 figures, the share, gateway products
    # and their channel total, the two tiles and their spend, and the two per-channel figures.
    assert new_customers_bake_plan_count(ROOT) == 13
    # One marketplace: pack 5 current/prior has four metrics each; frequency has five
    # platform metrics and one channel-roster read.
    assert reach_bake_plan_count(ROOT) == 14
    # The 30-day trend chunks into three windows, and Delivery per channel adds clicks and
    # the DSP viewability operands to the one batch.
    assert campaign_portfolio_bake_plan_count(ROOT, 30) == 34
    # Up to seven filter siblings per preset, each with its own segments (ADR-127).
    assert CAMPAIGN_MAX_FILTER_SIBLINGS == 7
    assert MAX_BAKE_BODY_COUNT == 1300
    assert _STORAGE["campaigns"].maximum_body_bytes < 256_000


def test_the_audiences_bounds_fit_the_gate_and_one_more_fires_it() -> None:
    """The timeout estimator and the row budget are the gate's own boundaries.

    The estimator is not the page limit (TD-D2-AUDIENCES-CAMPAIGN-LIMIT).
    """
    page = ArtifactServedPage("audiences", ())
    bound, rows = audiences_timeout_headroom_campaigns(1), audiences_row_budget(1)
    # The renderer executes list, roster and measurement barriers separately. Eleven campaigns
    # fit nine waves with render, write and positive tail margin; twelve need ten query waves.
    assert (bound, audiences_bake_plan_count(1, bound)) == (11, 63)
    assert audiences_bake_duration_ms(1, bound) < BAKER_TIMEOUT_SECONDS * 1_000
    assert audiences_bake_duration_ms(1, bound + 1) > BAKER_TIMEOUT_SECONDS * 1_000
    # Paired pre-fix control: the former aggregate N=13 choice also exceeds the phase budget.
    assert audiences_bake_duration_ms(1, 13) > BAKER_TIMEOUT_SECONDS * 1_000
    assert rows == 300

    def findings(campaigns: int, body_rows: int) -> set[str]:
        return {
            finding.rule
            for finding in evaluate_bakeability(
                (page,),
                {"audiences": audiences_bake_plan_count(1, campaigns)},
                {"audiences": audiences_storage_estimate(1, body_rows)},
            )
        }

    assert findings(bound, rows) == set()
    # The generic aggregate check remains an independent backstop after the phase-aware bound.
    # It waves at the certified Q10, so 91 plans (bound + 7) are the first over 300 s, and
    # 83 plans (bound + 5) still fit their waves but leave no time for the write.
    assert findings(bound + 5, rows) == {"bake-write-budget"}
    assert findings(bound + 7, rows) == {"bake-budget"}
    assert findings(bound, rows + 1) == {"artifact-storage-bound"}


def test_the_retention_body_bound_fits_one_marketplace_and_refuses_two() -> None:
    """The registered Retention body is derived per marketplace, so the cap can fire."""
    page = ArtifactServedPage("retention", ())

    def storage(markets: int) -> dict[str, BakeStorageEstimate]:
        body = markets * RETENTION_MARKET_MAX_ENCODED_BYTES
        return {"retention": BakeStorageEstimate(1, body, body)}

    # The committed bridge governs one marketplace.
    assert retention_bake_body_bytes(ROOT) == RETENTION_MARKET_MAX_ENCODED_BYTES
    assert evaluate_bakeability((page,), {"retention": 1}, storage(1)) == ()
    (finding,) = evaluate_bakeability((page,), {"retention": 1}, storage(2))
    assert (finding.page_id, finding.rule) == ("retention", "artifact-storage-bound")


def test_an_over_bound_preset_yields_a_named_snapshot_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the former 50-partition policy, 30d (60 per pack) is the over-bound preset."""
    monkeypatch.setattr(bakeability, "MAX_PRESET_SNAPSHOT_PARTITIONS", 50)
    page = ArtifactServedPage("synthetic", (TemporalPresetId.THIRTY_DAYS,))

    findings = evaluate_bakeability((page,), {"synthetic": 8}, _STORAGE)

    assert [finding.rule for finding in findings] == ["preset-snapshot-bound"]
    assert findings[0].page_id == "synthetic"
    assert "30d" in findings[0].message
    assert str(2 * 30) in findings[0].message
    assert "50" in findings[0].message


def test_the_bake_budget_boundary_is_exact_at_the_wave_arithmetic() -> None:
    # The generated certified cap, not a hard-coded eight: 9 waves of 10 and the tail fill 300 s.
    assert (CERTIFIED_QUERY_CAP, _LARGEST_FITTING_PLAN_COUNT) == (10, 90)
    page = ArtifactServedPage("synthetic", (TemporalPresetId.SEVEN_DAYS,))
    # A batch that fills the timeout leaves no time for even one write.
    at = evaluate_bakeability((page,), {"synthetic": _LARGEST_FITTING_PLAN_COUNT}, _STORAGE)
    assert [finding.rule for finding in at] == ["bake-write-budget"]
    over = evaluate_bakeability((page,), {"synthetic": _LARGEST_FITTING_PLAN_COUNT + 1}, _STORAGE)
    assert [finding.rule for finding in over] == ["bake-budget"]
    assert str(BAKER_TIMEOUT_SECONDS) in over[0].message


def test_every_finding_across_pages_and_rules_is_reported_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reports the complete defect list, never just the first refusal."""
    monkeypatch.setattr(bakeability, "MAX_PRESET_SNAPSHOT_PARTITIONS", 50)
    over_declared = ArtifactServedPage(
        "over-declared", (TemporalPresetId.FOURTEEN_DAYS, TemporalPresetId.THIRTY_DAYS)
    )
    unregistered = ArtifactServedPage("unregistered", (TemporalPresetId.SEVEN_DAYS,))

    findings = evaluate_bakeability(
        (over_declared, unregistered),
        {"over-declared": _LARGEST_FITTING_PLAN_COUNT + 1},
        _STORAGE,
    )

    assert [(finding.page_id, finding.rule) for finding in findings] == [
        ("over-declared", "preset-snapshot-bound"),
        ("over-declared", "bake-budget"),
        ("unregistered", "bake-budget"),
    ]


def test_the_storage_estimate_is_checked_against_the_bakers_own_memory_and_time() -> None:
    """The envelope is the baker's memory and timeout, never the page's own estimate (S8-4)."""
    page = ArtifactServedPage("synthetic", (TemporalPresetId.SEVEN_DAYS,))
    measured = 34  # Campaigns' 30d batch: 4 waves of 10 at 30 s and the 30 s tail, 150 s.
    budget_ms = BAKER_TIMEOUT_SECONDS * 1_000 - (4 * PER_QUERY_SECONDS + _TAIL_SECONDS) * 1_000
    fitting = budget_ms // PER_OBJECT_WRITE_MS
    resident = int(BAKER_MEMORY_BYTES * BAKE_RESIDENT_SHARE)
    fits = BakeStorageEstimate(fitting, 256_000, resident)
    assert evaluate_bakeability((page,), {"synthetic": measured}, {"synthetic": fits}) == ()
    over = BakeStorageEstimate(fitting + 1, 256_001, resident + 1)
    findings = evaluate_bakeability((page,), {"synthetic": measured}, {"synthetic": over})
    assert [finding.rule for finding in findings] == [
        "artifact-storage-bound",
        "bake-memory-bound",
        "bake-write-budget",
    ]
    assert "256001" in findings[0].message
    assert str(resident + 1) in findings[1].message
    assert str(fitting + 1) in findings[2].message
    # Campaigns at the estate bound with seven siblings: 1,300 objects, 130 s of writes after
    # 150 s of measurement, and at most 318.5 MB resident.
    assert MAX_BAKE_BODY_COUNT == 1300 <= fitting
    assert _STORAGE["campaigns"].total_bytes <= resident
    assert MAX_BAKE_TOTAL_BYTES == MAX_BAKE_BODY_COUNT * 256_000


def test_the_object_count_follows_each_filters_segments() -> None:
    """Each filter writes 2 x orderings x its segments; the estate acceptance bakes the same."""
    # All 1,000, Sponsored 999, then 1, 251, 251, 251 and 246: 64 segments, as baked locally.
    assert campaign_bake_object_count((1000, 999, 1, 251, 251, 251, 246)) == 1280
    assert campaign_bake_object_count((61,)) == 40  # the 19 sibling pairs, parent and custody
    # The closed form bounds every split of the population: one-channel segments sum to under
    # 20 + 5, so the split above (64 segments) is the most a real bake reaches.
    assert campaign_bake_object_count((1000, 999, 1, 251, 251, 251, 246)) < MAX_BAKE_BODY_COUNT


def test_the_baker_constants_match_the_publisher_stack() -> None:
    stack = (ROOT / "infra/lib/stacks/publisher-stack.ts").read_text()
    assert f"const BAKE_TIMEOUT_SECONDS = {BAKER_LAMBDA_TIMEOUT_SECONDS};" in stack
    # One variant's budget never exceeds the invocation it runs in.
    assert BAKER_TIMEOUT_SECONDS <= BAKER_LAMBDA_TIMEOUT_SECONDS
    baker = stack[stack.index("id: 'Baker'") :]
    memory = baker[baker.index("memorySize: ") + len("memorySize: ") :].split(",", 1)[0]
    assert int(memory) * 1024 * 1024 == BAKER_MEMORY_BYTES


def test_the_sibling_envelope_counts_every_governed_channel() -> None:
    taxonomy = load_media_taxonomy(ROOT)
    channels = [term for term in taxonomy.terms if isinstance(term.key, ChannelKey)]
    assert len(channels) == CAMPAIGN_GOVERNED_CHANNEL_COUNT


def test_each_roster_task_is_counted_at_its_own_window_never_a_page_total() -> None:
    """Plan 4.2: each task at its own window; Retail ranks in the query, so 47 at any window."""
    counts = roster_plan_counts(ROOT)

    assert counts == {
        "campaigns#1d": 26,
        "campaigns#7d": 26,
        "campaigns#14d": 30,
        "campaigns#30d": 34,
        "retail-health#1d": 47,
        "retail-health#7d": 47,
        "retail-health#14d": 47,
    }
    assert list(counts) == [
        f"{row['page']}#{row['variant']}"
        for row in BAKE_TASKS["roster"]
        if row["page"] in {"campaigns", "retail-health"}
    ]
    assert 136 not in counts.values()


def test_every_roster_task_is_certified_within_its_contract_budget() -> None:
    certificates, findings = certify_roster_tasks(roster_plan_counts(ROOT))

    assert findings == ()
    assert [(item.key, item.seconds, item.budget_seconds) for item in certificates] == [
        ("campaigns#1d", 136.6, 240),
        ("campaigns#7d", 136.6, 240),
        ("campaigns#14d", 136.6, 270),
        ("campaigns#30d", 166.6, 300),
        ("retail-health#1d", 196.6, 240),
        ("retail-health#7d", 196.6, 240),
        ("retail-health#14d", 196.6, 240),
    ]


def test_a_roster_task_over_its_budget_or_without_a_count_is_a_named_finding() -> None:
    """Paired control: one count past campaigns#1d's 240 s budget and one missing count trip."""
    counts = dict(roster_plan_counts(ROOT))
    # 7 waves of 10 at 30 s, 16.6 s of writes and the 30 s tail: 256.6 s > 240 s.
    counts["campaigns#1d"] = 70
    del counts["retail-health#14d"]

    _certificates, findings = certify_roster_tasks(counts)

    assert [(item.page_id, item.rule) for item in findings] == [
        ("campaigns", "roster-task-budget"),
        ("retail-health", "roster-task-budget"),
    ]
    assert "256.6" in findings[0].message
    assert "240" in findings[0].message
    assert "retail-health#14d has no true plan count" in findings[1].message
    # The boundary: 6 waves (60 plans) is 226.6 s and certifies.
    counts["campaigns#1d"] = 60
    counts["retail-health#14d"] = 47
    assert certify_roster_tasks(counts)[1] == ()


def test_a_calendar_preset_reads_its_longest_period_once_unless_the_page_compares_periods() -> None:
    """ADR-100: a month reads 31 day-partitions, 62 with a prior calendar period."""
    month = TemporalPresetId.LAST_COMPLETE_MONTH
    mixed = ArtifactServedPage("synthetic", (month,))
    assert bakeability.preset_partitions(mixed, month) == 31
    compared = ArtifactServedPage("synthetic", (month,), calendar_compares=True)
    assert bakeability.preset_partitions(compared, month) == 62
    periodic = ArtifactServedPage(
        "synthetic", (month,), calendar_compares=True, periodic_snapshots=True
    )
    assert bakeability.preset_partitions(periodic, month) == 2
    quarter = TemporalPresetId.LAST_COMPLETE_QUARTER
    # Paired: a compared quarter (184) is over the 100-partition read; an uncompared one fits.
    findings = evaluate_bakeability(
        (ArtifactServedPage("synthetic", (quarter,), calendar_compares=True),),
        {"synthetic": 8},
        _STORAGE,
    )
    assert [finding.rule for finding in findings] == ["preset-snapshot-bound"]
    assert (
        evaluate_bakeability(
            (ArtifactServedPage("synthetic", (quarter,)),), {"synthetic": 8}, _STORAGE
        )
        == ()
    )


def test_a_contract_source_horizon_its_page_does_not_derive_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5.1 I9: `make check-bakeability` refuses a declared horizon the policy doesn't derive."""
    from adapters.jobs.preset_artifact_publisher import bakeability  # noqa: PLC0415

    assert bakeability.source_horizon_findings(ROOT) == ()
    declared = dict(bakeability.BAKE_TASKS["source_horizon_days"])
    monkeypatch.setitem(
        bakeability.BAKE_TASKS, "source_horizon_days", {**declared, "campaigns#1d": 122}
    )
    (finding,) = bakeability.source_horizon_findings(ROOT)
    assert (finding.page_id, finding.rule) == ("campaigns", "source-horizon")


def test_the_page_level_audiences_gate_uses_the_one_campaign_authority() -> None:
    """TD-D2-AUDIENCES-CAMPAIGN-LIMIT: the page-level count fits the certified row count."""
    from domain.operations.execution_graph_release import BakeRoster  # noqa: PLC0415

    bound = audiences_runtime_campaign_bound(1)
    certified = {row.key: row.certified_plan_count for row in BakeRoster.from_contract().rows}
    assert bound < audiences_timeout_headroom_campaigns(1)
    gated = audiences_page_plan_count(ROOT)
    assert gated == audiences_bake_plan_count(1, bound)
    assert gated <= certified["audiences#last_complete_month"]


def test_every_row_write_term_fits_the_derived_write_allowance() -> None:
    """ADR-144: half the contract's Lambda timeout, derived; every row fits today (16.6 s)."""
    assert bakeability.WRITE_ALLOWANCE_SECONDS == BAKE_TASKS["lambda_timeout_seconds"] / 2 == 450
    assert bakeability.write_allowance_findings() == ()
    assert all(
        float(BAKE_TASKS["write_seconds"][str(row["page"])]) <= 450 for row in BAKE_TASKS["roster"]
    )


def test_a_row_whose_write_term_exceeds_the_allowance_fails_the_gate() -> None:
    terms = {**BAKE_TASKS["write_seconds"], "audiences": 450.1}
    findings = bakeability.write_allowance_findings(terms)
    assert [(item.page_id, item.rule) for item in findings] == [("audiences", "write-allowance")]
    assert "WRITE_ALLOWANCE_SECONDS=450" in findings[0].message
    # Control: exactly at the allowance fits.
    assert bakeability.write_allowance_findings({**terms, "audiences": 450.0}) == ()


def test_the_gate_prints_the_allowance_and_every_row_write_term(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bakeability.main() == 0
    out = capsys.readouterr().out
    assert "WRITE_ALLOWANCE_SECONDS=450" in out
    for row in BAKE_TASKS["roster"]:
        assert f"write {row['page']}#{row['variant']}: write_seconds" in out


# --- ADR-143: the memory model ------------------------------------------------------------------

BAKE_MAX_ROWS_FOR_TEST = bakeability.BAKE_MAX_ROWS


@pytest.mark.covers("ADR-143")
def test_every_roster_rows_resident_bytes_are_derived_term_by_term() -> None:
    """Each term is computed from the row's own certified widths, never typed."""
    terms = bakeability.BAKE_MEMORY_TERMS
    for (key, certificate), model in zip(
        bakeability.roster_certificates(ROOT), bakeability.memory_models(ROOT), strict=True
    ):
        assert model.key == key
        assert len(certificate.widths) + len(certificate.kept) == len(certificate.plans)
        held = max(
            (
                bakeability._certified_width(  # pyright: ignore[reportPrivateUsage]
                    plan, width, bakeability.audiences_marketplace_count(ROOT)
                )
                for plan, width in zip(certificate.plans, certificate.widths, strict=False)
            ),
            default=0,
        )
        assert model.writes == bakeability.WRITES_IN_FLIGHT_BYTES
        assert model.raw == bakeability.CERTIFIED_QUERY_CAP * held * terms.raw
        assert model.render == held * terms.render
        assert model.total == model.writes + model.raw + model.shaped + model.kept + model.render
        assert f"= {model.total} B <= {model.limit} B" in model.terms()
        assert model.certified


@pytest.mark.covers("ADR-143")
def test_a_row_past_its_share_fails_and_its_terms_name_why() -> None:
    """Paired with the certified rows above: the same row at a far wider ceiling fails."""
    key, certificate = next(
        item for item in bakeability.roster_certificates(ROOT) if item[0] == "retail-health#14d"
    )
    fits = bakeability.resident(certificate, key)
    assert fits.certified
    over = bakeability.resident(certificate, key, ceiling=50 * BAKE_MAX_ROWS_FOR_TEST)
    assert not over.certified and over.shaped > fits.shaped


@pytest.mark.covers("ADR-143")
def test_the_widths_are_read_from_the_batches_the_sources_build() -> None:
    """Retail's widths: the ranked 50, its 50 x 31 series bound, and the bake ceiling."""
    widths = dict(bakeability.roster_certificates(ROOT))["retail-health#7d"].widths
    assert set(widths) == {50, 50 * bakeability.BAKE_LONGEST_WINDOW_DAYS, BAKE_MAX_ROWS_FOR_TEST}


@pytest.mark.covers("ADR-143")
def test_the_longest_bake_window_is_held_equal_to_the_longest_daily_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = bakeability.roster_certificates(ROOT)
    assert bakeability.longest_daily_window_days(rows) == bakeability.BAKE_LONGEST_WINDOW_DAYS == 31
    assert not [
        item for item in bakeability.memory_findings(ROOT) if item.rule == "bake-longest-window"
    ]
    # Control: a constant one day short is a finding.
    monkeypatch.setattr(bakeability, "BAKE_LONGEST_WINDOW_DAYS", 30)
    assert [item.rule for item in bakeability.memory_findings(ROOT)] == ["bake-longest-window"]


@pytest.mark.covers("ADR-143")
def test_the_gate_prints_the_memory_model_and_its_binding_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bakeability.main() == 0
    out = capsys.readouterr().out
    assert f"BAKE_MAX_ROWS={BAKE_MAX_ROWS_FOR_TEST}" in out
    assert "binding row: " in out
    for row in BAKE_TASKS["roster"]:
        assert f"memory {row['page']}#{row['variant']}: W " in out


@pytest.mark.covers("ADR-143")
def test_the_retail_estate_at_the_bake_ceiling_stays_within_its_modelled_bytes() -> None:
    """Local estate at the bound (03 §3.5): BAKE_MAX_ROWS products bake within `resident`.

    The real renderer and seam over the unit tier's fictional query fake, at the certified width.
    """
    import tracemalloc  # noqa: PLC0415 -- evidence-only instrumentation

    from test_retail_health_preset_source import (  # noqa: PLC0415
        PERIOD,
        PRIOR,
        FakeHierarchy,
        FakeReported,
        RetailQueries,
        RetailReportedTotals,
        _hierarchy_result,  # pyright: ignore[reportPrivateUsage]
        _products,  # pyright: ignore[reportPrivateUsage]
        _renderer,  # pyright: ignore[reportPrivateUsage]
    )

    from adapters.jobs.preset_artifact_publisher.retail_source import (  # noqa: PLC0415
        RetailHealthRenderInput,
    )

    products = _products(BAKE_MAX_ROWS_FOR_TEST)
    renderer = _renderer(
        RetailQueries(products, PERIOD),
        FakeHierarchy(_hierarchy_result(products)),
        FakeReported(
            RetailReportedTotals(Decimal(0), Decimal(0), ("reported-total-publication-1",))
        ),
    )
    tracemalloc.start()
    try:
        rendered = renderer.render_retail_health(RetailHealthRenderInput(PERIOD, PRIOR))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rendered.artifact is not None
    assert rendered.artifact.total_count == BAKE_MAX_ROWS_FOR_TEST
    certificate = dict(bakeability.roster_certificates(ROOT))["retail-health#7d"]
    model = bakeability.resident(
        certificate,
        "retail-health#7d",
        marketplaces=bakeability.audiences_marketplace_count(ROOT),
    )
    assert peak <= model.total, model.terms()
