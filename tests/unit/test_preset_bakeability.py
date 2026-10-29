# SPDX-License-Identifier: MIT-0
"""Build-time bakeability gate for declared artifact-served preset windows."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher import bakeability
from adapters.jobs.preset_artifact_publisher.bakeability import (
    BAKER_LAMBDA_TIMEOUT_SECONDS,
    BAKER_MEMORY_BYTES,
    BAKER_TIMEOUT_SECONDS,
    CAMPAIGN_GOVERNED_CHANNEL_COUNT,
    CAMPAIGN_MAX_FILTER_SIBLINGS,
    CERTIFIED_QUERY_CAP,
    MAX_PRESET_SNAPSHOT_PARTITIONS,
    PER_OBJECT_WRITE_MS,
    PER_QUERY_SECONDS,
    ArtifactServedPage,
    BakeStorageEstimate,
    artifact_served_pages,
    audiences_phases,
    campaign_bake_object_count,
    certify_roster_tasks,
    evaluate_bakeability,
    roster_plan_counts,
    sibling_index_shard_count,
    write_term_object_count,
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
from domain.operations.bake_memory import (
    WRITES_IN_FLIGHT_BYTES,
    bake_max_rows,
    write_buffer_bytes,
)
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
        write_term_object_count("campaigns"),
        256_000,
        write_term_object_count("campaigns") * 256_000,
    ),
    "retail-health": BakeStorageEstimate(2, 256_000, 512_000),
    "scorecard": BakeStorageEstimate(1, 256_000, 256_000),
    "new-customers": BakeStorageEstimate(1, 256_000, 256_000),
    "reach": BakeStorageEstimate(1, 256_000, 256_000),
    "retention": BakeStorageEstimate(
        1, retention_bake_body_bytes(ROOT), retention_bake_body_bytes(ROOT)
    ),
    "audiences": BakeStorageEstimate(
        write_term_object_count("audiences"),
        256_000,
        write_term_object_count("audiences") * 256_000,
    ),
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
    # ADR-070 2026-10-25: the bake policy is the chunked two-pass snapshot's bound.
    assert MAX_PRESET_SNAPSHOT_PARTITIONS == MAX_EXACT_CURRENT_PARTITIONS == 1_000
    findings = evaluate_bakeability(
        pages,
        {
            "campaigns": campaign_portfolio_bake_plan_count(ROOT, 30),
            "retail-health": retail_health_bake_plan_count(ROOT, 14),
            "scorecard": scorecard_bake_plan_count(ROOT),
            "new-customers": new_customers_bake_plan_count(ROOT),
            "reach": reach_bake_plan_count(ROOT),
            "retention": len(STANDARD_METRICS),
            "audiences": sum(audiences_phases(1)),
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
    # 11 compares, each one breakdown per window, and one daily read per trend operand: 26 at
    # every preset (ADR-152). Delivery per channel adds clicks and the DSP viewability operands.
    assert campaign_portfolio_bake_plan_count(ROOT, 30) == 26
    # Up to seven filter siblings per preset, each with its own segments (ADR-127).
    assert CAMPAIGN_MAX_FILTER_SIBLINGS == 7


@pytest.mark.covers("ADR-141")
def test_audiences_is_certified_at_24_plans_whatever_its_campaigns() -> None:
    """Design 01 §6: every campaign read is grouped, so no phase grows with the seller."""
    from domain.operations.execution_graph_release import BakeRoster  # noqa: PLC0415

    # Phase 1: 3 profile rosters + the count pass's 3 figures and R1; phase 2: 15 profile
    # figures + the history walk's older months; phase 3: its withheld part.
    assert audiences_phases(1) == (7, 16, 1)
    assert sum(audiences_phases(1)) == 24
    certified = {row.key: row.certified_plan_count for row in BakeRoster.from_contract().rows}
    assert certified["audiences#last_complete_month"] == 24
    # Its write term is the whole allowance, so its budget is the counter's, not a fixed 300 s.
    write = bakeability.page_write_seconds("audiences")
    assert (write.seconds, write.marker) == (450.0, "derived")
    assert bakeability.phased_task_seconds(audiences_phases(1), CERTIFIED_QUERY_CAP, 450.0) == 600
    assert BAKE_TASKS["budget_seconds"]["audiences#last_complete_month"] == 600
    # The write term writes 4,500 objects one after another at 100 ms; the stage writes more.
    assert write_term_object_count("audiences") == 4_500
    page = ArtifactServedPage("audiences", ())
    storage = {
        "audiences": BakeStorageEstimate(4_500, 256_000, 4_500 * 256_000),
    }
    assert evaluate_bakeability((page,), {"audiences": 24}, storage) == ()
    # Paired control: one more sequential object than the term holds overruns the 600 s budget.
    over = {"audiences": BakeStorageEstimate(4_801, 256_000, 4_801 * 256_000)}
    assert {item.rule for item in evaluate_bakeability((page,), {"audiences": 24}, over)} == {
        "bake-write-budget"
    }


@pytest.mark.covers("ADR-141")
def test_the_gate_prints_audiences_from_its_one_certificate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LOW-1: `main()` prints the page and task counts from the one model, with no override."""
    assert bakeability.main() == 0
    out = capsys.readouterr().out
    assert "  audiences: presets [last_complete_month], 24 plans" in out
    assert bakeability.d2_main() == 0
    d2 = capsys.readouterr().out
    assert "  audiences#last_complete_month: count 24, phases [7, 16, 1], write 450.0 s" in d2


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
    fits = BakeStorageEstimate(fitting, 256_000, fitting * 256_000)
    assert evaluate_bakeability((page,), {"synthetic": measured}, {"synthetic": fits}) == ()
    over = BakeStorageEstimate(fitting + 1, 256_001, (fitting + 1) * 256_001)
    findings = evaluate_bakeability((page,), {"synthetic": measured}, {"synthetic": over})
    # Bodies stream (ADR-145): what a bake holds is the memory model's, so the only storage
    # findings are the per-body cap and the writes' time.
    assert [finding.rule for finding in findings] == [
        "artifact-storage-bound",
        "bake-write-budget",
    ]
    assert "256001" in findings[0].message
    assert str(fitting + 1) in findings[1].message


def test_the_object_count_follows_each_filters_segments_and_roster_shards() -> None:
    """Each filter writes 2 x (orderings x segments + roster shards), plus index shards.

    ADR-145; the estate acceptance bakes the same count.
    """
    # All 1,000, Sponsored 999, then 1, 251, 251, 251 and 246: 64 segments and 9 roster
    # shards, as baked locally, and the one shard indexing their 648 siblings.
    assert campaign_bake_object_count((1000, 999, 1, 251, 251, 251, 246)) == 1299
    # 19 segment pairs and one roster shard pair, the parent and its custody, one index shard.
    assert campaign_bake_object_count((61,)) == 43
    # A shard holds 800 descriptors: 800 siblings need one, 801 two, none none.
    assert [sibling_index_shard_count(count) for count in (0, 1, 800, 801)] == [0, 1, 1, 2]


@pytest.mark.covers("ADR-152")
def test_campaigns_is_certified_at_26_plans_and_the_whole_write_allowance() -> None:
    """Campaigns' segments grow with its campaigns, as Audiences' parts do (ADR-152).

    So its write term is the whole allowance and the stage's runtime deadline its one write
    limit; no campaign count is certified as a bound.
    """
    write = bakeability.page_write_seconds("campaigns")
    assert (write.seconds, write.marker) == (450.0, "derived")
    # 3 waves of 10 at 30 s, the 30 s tail and the 450 s write term.
    assert bakeability.task_seconds(26, CERTIFIED_QUERY_CAP, 450.0) == 570
    for variant in ("1d", "7d", "14d", "30d"):
        assert BAKE_TASKS["certified_plan_counts"][f"campaigns#{variant}"] == 26
        assert BAKE_TASKS["budget_seconds"][f"campaigns#{variant}"] == 570
    # Every other page writes a fixed set of bodies and keeps its own 16.6 s.
    assert bakeability.page_write_seconds("retail-health").seconds == 16.6
    # Its memory: one record store at the ceiling, and seven filters' sums for each of 30 days
    # per trend operand; nothing is held (ADR-152).
    certificate = dict(bakeability.roster_certificates(ROOT))["campaigns#30d"]
    assert certificate.records == (bakeability.BAKE_MAX_ROWS,)
    assert certificate.kept == (7 * 30,) * 4
    assert certificate.widths == ()


def test_the_baker_constants_match_the_publisher_stack() -> None:
    stack = (ROOT / "infra/lib/stacks/publisher-stack.ts").read_text()
    assert f"const BAKE_TIMEOUT_SECONDS = {BAKER_LAMBDA_TIMEOUT_SECONDS};" in stack
    # One variant's budget never exceeds the invocation it runs in.
    assert BAKER_TIMEOUT_SECONDS <= BAKER_LAMBDA_TIMEOUT_SECONDS
    # The Baker's memory has one home, the contract: the stack sizes the function from the
    # generated value and the memory model reads the same value (ADR-152).
    baker = stack[stack.index("id: 'Baker'") :]
    memory = baker[baker.index("memorySize: ") + len("memorySize: ") :].split(",", 1)[0]
    assert memory == "BAKE_TASKS.baker_memory_mib"
    assert BAKER_MEMORY_BYTES == 2048 * 1024 * 1024


def test_the_sibling_envelope_counts_every_governed_channel() -> None:
    taxonomy = load_media_taxonomy(ROOT)
    channels = [term for term in taxonomy.terms if isinstance(term.key, ChannelKey)]
    assert len(channels) == CAMPAIGN_GOVERNED_CHANNEL_COUNT


def test_each_roster_task_is_counted_at_its_own_window_never_a_page_total() -> None:
    """Plan 4.2: each task at its own window; Retail ranks in the query, so 49 at any window."""
    counts = roster_plan_counts(ROOT)

    assert counts == {
        "campaigns#1d": 26,
        "campaigns#7d": 26,
        "campaigns#14d": 26,
        "campaigns#30d": 26,
        "retail-health#1d": 49,
        "retail-health#7d": 49,
        "retail-health#14d": 49,
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
        ("campaigns#1d", 570.0, 570),
        ("campaigns#7d", 570.0, 570),
        ("campaigns#14d", 570.0, 570),
        ("campaigns#30d", 570.0, 570),
        ("retail-health#1d", 196.6, 240),
        ("retail-health#7d", 196.6, 240),
        ("retail-health#14d", 196.6, 240),
    ]


def test_a_roster_task_over_its_budget_or_without_a_count_is_a_named_finding() -> None:
    """Paired control: one count past campaigns#1d's 570 s budget and one missing count trip."""
    counts = dict(roster_plan_counts(ROOT))
    # 4 waves of 10 at 30 s, the 450 s write term and the 30 s tail: 600 s > 570 s.
    counts["campaigns#1d"] = 31
    del counts["retail-health#14d"]

    _certificates, findings = certify_roster_tasks(counts)

    assert [(item.page_id, item.rule) for item in findings] == [
        ("campaigns", "roster-task-budget"),
        ("retail-health", "roster-task-budget"),
    ]
    assert "600.0" in findings[0].message
    assert "570" in findings[0].message
    assert "retail-health#14d has no true plan count" in findings[1].message
    # The boundary: 3 waves (30 plans) is 570 s and certifies.
    counts["campaigns#1d"] = 30
    counts["retail-health#14d"] = 47
    assert certify_roster_tasks(counts)[1] == ()


def test_a_calendar_preset_reads_its_longest_period_once_unless_the_page_compares_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    compared_quarter = ArtifactServedPage("synthetic", (quarter,), calendar_compares=True)
    assert bakeability.preset_partitions(compared_quarter, quarter) == 184
    # ADR-070 2026-10-25: a compared quarter's 184 day-partitions fit the chunked snapshot.
    assert evaluate_bakeability((compared_quarter,), {"synthetic": 8}, _STORAGE) == ()
    # Paired: under the former 100-partition read the same quarter was the snapshot finding.
    monkeypatch.setattr(bakeability, "MAX_PRESET_SNAPSHOT_PARTITIONS", 100)
    findings = evaluate_bakeability((compared_quarter,), {"synthetic": 8}, _STORAGE)
    assert [finding.rule for finding in findings] == ["preset-snapshot-bound"]


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
        # Each plan is a held width or a fold's bound; a record store's columns are one term.
        if not certificate.records:
            assert len(certificate.widths) + len(certificate.kept) == len(certificate.plans)
        held = max(
            (
                bakeability._certified_width(  # pyright: ignore[reportPrivateUsage]
                    plan, width, bakeability.fixed_axes(ROOT)
                )
                for plan, width in zip(certificate.plans, certificate.widths, strict=False)
            ),
            default=0,
        )
        assert model.writes == bakeability.WRITES_IN_FLIGHT_BYTES
        assert model.raw == bakeability.CERTIFIED_QUERY_CAP * held * terms.raw
        assert model.render == held * terms.render
        assert model.records == sum(
            terms.record * (BAKE_MAX_ROWS_FOR_TEST if item == BAKE_MAX_ROWS_FOR_TEST else item)
            for item in certificate.records
        )
        assert model.total == (
            model.writes + model.raw + model.shaped + model.kept + model.render + model.records
        )
        assert f"= {model.total} B <= {model.limit} B" in model.terms()
        assert model.certified


@pytest.mark.covers("ADR-143")
def test_a_row_past_its_share_fails_and_its_terms_name_why() -> None:
    """Paired with the certified rows above: the binding row one row past V fails."""
    binding = "audiences#last_complete_month"
    certificate = dict(bakeability.roster_certificates(ROOT))[binding]
    axes = bakeability.fixed_axes(ROOT)
    fits = bakeability.resident(certificate, binding, axes=axes)
    assert fits.certified
    one_past = BAKE_MAX_ROWS_FOR_TEST + 1
    over = bakeability.resident(certificate, binding, ceiling=one_past, axes=axes)
    assert not over.certified and over.shaped > fits.shaped


@pytest.mark.covers("ADR-143", "ADR-152")
def test_the_widths_are_read_from_the_batches_the_sources_build() -> None:
    """Retail's widths: the movers' 3, the ranked 50, 50 x 5 ad rows, 50 x 31 series, totals."""
    widths = dict(bakeability.roster_certificates(ROOT))["retail-health#7d"].widths
    assert set(widths) == {
        3,
        50,
        50 * 5,
        50 * bakeability.BAKE_LONGEST_WINDOW_DAYS,
        BAKE_MAX_ROWS_FOR_TEST,
    }
    # Only the totals and trends ask for the ceiling, and they group by fixed axes alone.
    axes = bakeability.fixed_axes(ROOT)
    certificate = dict(bakeability.roster_certificates(ROOT))["retail-health#7d"]
    assert all(
        bakeability._certified_width(plan, width, axes) < width  # pyright: ignore[reportPrivateUsage]
        for plan, width in zip(certificate.plans, certificate.widths, strict=True)
        if width == BAKE_MAX_ROWS_FOR_TEST
    )


@pytest.mark.covers("ADR-152")
def test_the_ceiling_is_derived_from_the_bakers_memory_by_the_binding_row() -> None:
    """V at the deployed 2,048 MiB is 44,937 rows; the gate recomputes the intercept and slope."""
    assert BAKE_MAX_ROWS_FOR_TEST == bake_max_rows(2048 * 1024 * 1024) == 44_937
    # At the deployed width's W; a 1,024 MiB Baker derives its own width (the sizing line).
    assert bake_max_rows(1024 * 1024 * 1024) == 22_219
    pairs = tuple(
        zip(
            bakeability.memory_models(ROOT),
            bakeability.memory_models(ROOT, ceiling=BAKE_MAX_ROWS_FOR_TEST + 1),
            strict=True,
        )
    )
    bound = bakeability.binding_row(pairs)
    assert (bound.key, bound.fixed_bytes, bound.bytes_per_row, bound.ceiling) == (
        "audiences#last_complete_month",
        11_779_720,
        23_632,
        44_937,
    )
    assert not [
        item for item in bakeability.memory_findings(ROOT) if item.rule == "bake-memory-derivation"
    ]


@pytest.mark.covers("ADR-152")
def test_a_declared_slope_the_batches_dont_produce_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bakeability, "BAKE_BYTES_PER_ROW", 23_631)
    assert "bake-memory-derivation" in {item.rule for item in bakeability.memory_findings(ROOT)}


@pytest.mark.covers("ADR-152")
def test_governed_taxonomy_axes_are_fixed_and_a_catalog_axis_is_not() -> None:
    """Scorecard's channel read is bounded by the taxonomy; a campaign read by the ceiling."""
    axes = bakeability.fixed_axes(ROOT)
    assert {key: axes[key] for key in ("ecosystem_key", "platform_key", "channel_key")} == {
        "ecosystem_key": 1,
        "platform_key": 2,
        "channel_key": 5,
    }
    assert (axes["ad_product_key"], axes["exposure_group"], axes["acquisition_channel_group"]) == (
        5,
        3,
        3,
    )
    rows = dict(bakeability.roster_certificates(ROOT))
    scorecard = bakeability.resident(rows["scorecard#7d"], "scorecard#7d", axes=axes)
    assert scorecard.shaped < 1_000 * bakeability.BAKE_MEMORY_TERMS.shaped
    audiences = bakeability.resident(
        rows["audiences#last_complete_month"], "audiences#last_complete_month", axes=axes
    )
    assert audiences.shaped > BAKE_MAX_ROWS_FOR_TEST * bakeability.BAKE_MEMORY_TERMS.shaped


@pytest.mark.covers("ADR-143", "ADR-152")
def test_every_daily_series_fits_the_longest_bake_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Campaigns' 30-day trend is the longest; Reach's 92-day campaign count is no series."""
    rows = bakeability.roster_certificates(ROOT)
    assert bakeability.longest_daily_window_days(rows) == 30
    assert bakeability.BAKE_LONGEST_WINDOW_DAYS == 31
    assert not [
        item for item in bakeability.memory_findings(ROOT) if item.rule == "bake-longest-window"
    ]
    # Control: a constant one day shorter than the longest series is a finding.
    monkeypatch.setattr(bakeability, "BAKE_LONGEST_WINDOW_DAYS", 29)
    assert [item.rule for item in bakeability.memory_findings(ROOT)] == ["bake-longest-window"]


@pytest.mark.covers("ADR-143")
def test_the_gate_prints_the_memory_model_and_its_binding_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bakeability.main() == 0
    out = capsys.readouterr().out
    assert f"BAKE_MAX_ROWS={BAKE_MAX_ROWS_FOR_TEST}" in out
    assert "1024 MiB Baker: BAKE_MAX_ROWS=22457 (binding audiences#last_complete_month)" in out
    assert "10240 MiB Baker: BAKE_MAX_ROWS=225792" in out
    # ADR-145: each size's write width, derived with its own write buffer, printed in the open.
    assert "2048 MiB Baker: BAKE_MAX_ROWS=44937 (binding audiences#last_complete_month); " in out
    assert "write width 23: ceil(101620 audiences objects at BAKE_MAX_ROWS=44937" in out
    assert "capped at the widest measured width 64, whose 288000 objects" in out
    for row in BAKE_TASKS["roster"]:
        assert f"memory {row['page']}#{row['variant']}: W " in out


@pytest.mark.covers("ADR-143", "ADR-152")
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
    model = bakeability.resident(certificate, "retail-health#7d", axes=bakeability.fixed_axes(ROOT))
    assert peak <= model.total, model.terms()


@pytest.mark.covers("ADR-145")
def test_the_contract_write_width_is_the_one_the_deployed_memory_derives() -> None:
    """23 pairs write the 2,048 MiB ceiling's Audiences objects inside the 450 s allowance."""
    derived = bakeability.derive_write_width(ROOT, BAKER_MEMORY_BYTES)
    assert (derived.width, derived.page, derived.objects, derived.capped) == (
        23,
        "audiences",
        101_620,  # objects at 44,937 DSP campaigns, the at-most structural count
        False,
    )
    assert (derived.bound.key, derived.bound.ceiling) == ("audiences#last_complete_month", 44_937)
    # W counts the width it derives: 2 objects x 23 pairs x 256,000 B.
    assert write_buffer_bytes(23) == WRITES_IN_FLIGHT_BYTES == 11_776_000
    assert bakeability.write_width_findings(ROOT) == ()


@pytest.mark.covers("ADR-145")
def test_a_width_chosen_without_its_own_write_buffer_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """22 pairs write 99,000 objects, short of the 101,670 at 22's own 44,959-row ceiling."""
    monkeypatch.setattr(bakeability, "BAKE_WRITE_WIDTH", 22)
    (finding,) = bakeability.write_width_findings(ROOT)
    assert (finding.page_id, finding.rule) == ("roster", "bake-write-width")
    assert finding.message.startswith(
        "bake_tasks.baker_write_width is 22; 2048 MiB derives width 23"
    )


@pytest.mark.covers("ADR-145")
def test_a_width_past_the_widest_measured_one_is_capped_with_its_write_ceiling() -> None:
    """At 10,240 MiB the ceiling's objects need 114 pairs; only 64 were measured still rising."""
    derived = bakeability.derive_write_width(ROOT, 10_240 * 1024 * 1024)
    assert (derived.width, derived.capped, derived.page) == (64, True, "audiences")
    assert bakeability.width_for_objects(derived.objects) == 114
    # 64 chains x 450,000 ms / 100 ms per object: the objects one invocation's writes certify.
    assert derived.writable_objects == 288_000


def test_the_width_covers_its_objects_exactly_at_the_allowance_boundary() -> None:
    """23 chains x 4,500 objects each fill the 450 s allowance; one object more needs a chain."""
    assert bakeability.width_for_objects(103_500) == 23
    assert bakeability.width_for_objects(103_501) == 24
    assert bakeability.width_for_objects(1) == 1


def test_only_the_pages_whose_objects_grow_with_campaigns_count_toward_the_width() -> None:
    assert bakeability.ceiling_objects("campaigns", 10_000) == 12_128  # objects per variant
    # 449 index parts + 3 paths x 1,797 profile parts (89,874 items each) + 44,937 campaign
    # pages = 50,777 sibling pairs, their 64 index shards and the parent pair.
    assert bakeability.ceiling_objects("audiences", 44_937) == 101_620  # objects per bake
    with pytest.raises(ValueError, match="fixed set of objects"):
        bakeability.ceiling_objects("scorecard", 10_000)


@pytest.mark.covers("ADR-145")
@pytest.mark.parametrize(
    ("shape", "objects"),
    [
        # One fresh dev bake (2026-10-27): 30 one-page campaigns, paths inside the parent.
        ((30, 30, (50, 50, 50)), 63),
        ((100, 100, (0, 0, 0)), 203),
        ((101, 100, (0, 0, 0)), 205),  # the index's 101st entry is a part pair
        ((1, 1, (50, 0, 0)), 5),
        ((1, 1, (51, 0, 0)), 7),  # a path's 51st item is a profile part pair
        ((1, 800, (0, 0, 0)), 1_603),
        ((1, 801, (0, 0, 0)), 1_606),  # the 801st sibling pair needs a second index shard
    ],
)
def test_an_audiences_bake_writes_its_parent_pair_its_part_pairs_and_their_index_shards(
    shape: tuple[int, int, tuple[int, ...]], objects: int
) -> None:
    """`shape` is (listed campaigns, campaign pages, each profile path's items)."""
    assert bakeability.audiences_bake_object_count(*shape) == objects
