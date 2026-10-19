# SPDX-License-Identifier: MIT-0
"""The six hand-built trap fixtures, and what each one must not be allowed to do.

Every case here is a documented behaviour of a real source, and each is the shape a generated
fixture cannot produce: a generated fixture is built from our own declarations, so it never
contains an absent value, a negative count or a second currency.

Two of these found defects before an assertion existed for them — `distinct_user_count`
declared required, and the CSV reader keeping `""` instead of null. Both would have quarantined
correct data. `tests/fixtures/traps/README.md` records them.

Traps 2 and 6 are only half-testable here. What a load *does* about a restatement or a currency
disagreement belongs to the load assertions and the merge; what this file checks is that the
information needed to decide survives the read, because a mapper that dropped the watermark or
collapsed the currencies would make the later check impossible rather than merely wrong.
"""

import json
import pathlib
import re
from decimal import Decimal

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.jobs.schema_generation.models import class_name_for
from domain.curation.mapping import MappingRequest, map_rows
from domain.data_contracts import generated_rows
from domain.data_contracts.contract import DataContract, NullMeaning

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TRAPS = REPO_ROOT / "tests/fixtures/traps"

_CONTRACTS = {contract.contract_id: contract for _, contract in load_contracts(REPO_ROOT)}


def _rows(fixture: str, contract_id: str, suffix: str = "json") -> tuple[dict[str, object], ...]:
    """Read one trap fixture through its contract's reader and mapper."""
    contract = _CONTRACTS[contract_id]
    source = read(
        DeliveredFile(
            text=(TRAPS / f"{fixture}.{suffix}").read_text(),
            source_format=contract.source_format,
            manifest_text=(TRAPS / f"{fixture}.manifest.json").read_text(),
        )
    )
    return map_rows(MappingRequest(contract=contract, source=source))


def _delivered(
    fixture: str, contract_id: str, suffix: str = "json"
) -> tuple[dict[str, object], ...]:
    """Read one trap fixture without mapping it, to assert on what the source actually sent.

    Needed because a conformed fact does not curate every column AMC delivers. The two suppression
    diagnostics are requested, arrive, and are deliberately not stored (G-28) — so their behaviour
    is a fact about the *delivery* and has to be trapped there. Asserting through the mapper would
    silently stop checking them.
    """
    contract = _CONTRACTS[contract_id]
    source = read(
        DeliveredFile(
            text=(TRAPS / f"{fixture}.{suffix}").read_text(),
            source_format=contract.source_format,
            manifest_text=(TRAPS / f"{fixture}.manifest.json").read_text(),
        )
    )
    return source.rows


def _validated(fixture: str, contract_id: str, suffix: str = "json") -> list[object]:
    """Map and then validate, so a trap proves the row is *acceptable* and not merely parsed."""
    model = getattr(generated_rows, class_name_for(contract_id))
    return [model.model_validate(row) for row in _rows(fixture, contract_id, suffix)]


class TestAmcSuppression:
    """A privacy withholding must be labelled, never coalesced.

    The central property of the whole platform: suppressed is not zero, and a partition that
    contains a suppressed row is correct data rather than a defect.
    """

    def test_the_merged_row_is_flagged(self) -> None:
        rows = _rows("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        suppressed = [row for row in rows if row["rows_suppressed"] is True]

        assert len(suppressed) == 1

    def test_the_delivery_carries_a_reason_that_names_its_own_data_source(self) -> None:
        """Asserted on the delivery, because the reason is why a conformed fact cannot store it.

        The format names the data source it is about, so three deliveries assembling one row of
        `fct_ad_exposure` carry three different reasons and one column could hold only the last.
        That is the whole argument for G-28, and it rests on this string's shape.
        """
        delivered = _delivered("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        suppressed = next(row for row in delivered if row["rows_suppressed"] == "true")

        assert suppressed["suppression_reason"] == "1 dsp_impressions users < 2 required"
        assert "dsp_impressions" in str(suppressed["suppression_reason"])

    def test_a_blank_distinct_count_reads_as_null_and_never_zero(self) -> None:
        """Zero would say AMC saw no users, which is a different and false statement.

        One of the two defects this trap found was the CSV reader keeping an empty field as `""`.
        Still asserted, now at the reader, which is where that defect lived — and it must keep
        holding for every blank field whether or not the column is curated.
        """
        delivered = _delivered("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        suppressed = next(row for row in delivered if row["rows_suppressed"] == "true")

        assert suppressed["distinct_user_count"] is None
        assert suppressed["distinct_user_count"] != 0

    def test_a_clean_row_keeps_the_count_capped_at_the_threshold(self) -> None:
        """The clean rows report the threshold, not the true user count.

        Which is the second half of why the count cannot be conformed: it is a per-query artefact
        capped by the strictest output column, not a property of the row.
        """
        delivered = _delivered("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        clean = [row for row in delivered if row["rows_suppressed"] == "false"]

        assert [row["distinct_user_count"] for row in clean] == ["2", "2"]

    def test_neither_diagnostic_reaches_the_conformed_fact(self) -> None:
        """G-28. The delivery carries both; the curated row must carry neither.

        Both are per-delivery values, and `fct_ad_exposure` has four writers — so one column would
        hold whatever landed last, and each load's withdraw would erase the others rather than
        supersede them.
        """
        delivered = _delivered("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        curated = _rows("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")

        for column in ("suppression_reason", "distinct_user_count"):
            assert column in delivered[0], f"{column} must still arrive"
            assert column not in curated[0], f"{column} must not be conformed"
        assert "rows_suppressed" in curated[0], "the discriminator is a row fact and must remain"

    def test_the_metrics_survive_suppression(self) -> None:
        """AMC nulls the offending *dimension* and re-aggregates the measures with SUM.

        So a suppressed row still carries figures, and they still belong in the total. Treating
        the row as empty would lose them.
        """
        rows = _rows("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")
        suppressed = next(row for row in rows if row["rows_suppressed"] is True)

        assert suppressed["impressions"] == "457"
        assert suppressed["spend"] == Decimal("13.9400")

    def test_the_whole_file_is_acceptable_to_the_row_model(self) -> None:
        """The assertion the two defects failed. Parsing it was never the hard part."""
        validated = _validated("amc-suppression-merged", "amc-dsp-impressions", suffix="csv")

        assert len(validated) == 3

    def test_a_single_writer_fact_still_declares_which_null_applies(self) -> None:
        """A status column has to say *which* of the four absences it is.

        Moved to `amc-retail-purchases` because the rule is unchanged where it still applies. A
        fact with one writer conforms all three suppression columns, so its diagnostics are
        nullable and must name SUPPRESSED. Only a conformed fact drops them.
        """
        contract = _CONTRACTS["amc-retail-purchases"]
        assert contract.suppression is not None
        for column in (
            contract.suppression.reason_column,
            contract.suppression.distinct_user_count_column,
        ):
            mapping = contract.field_for(column)
            assert mapping is not None, column
            assert mapping.nullable is True, column
            assert mapping.null_meaning is NullMeaning.SUPPRESSED, column


class TestThreeDspDeliveriesCoveringDifferentGrains:
    """One partition, three deliveries, only partly overlapping. G-28 and ADR-033.

    The shape a generated fixture cannot produce. The generator derives every value from a column
    name, so its three DSP fixtures cover identical grains with identical values — and a load that
    erased one contract's columns would leave a table indistinguishable from a correct one. Here
    each delivery covers a grain the other two do not, so a withdraw that reaches beyond the rows
    its contract wrote is visible as a missing figure.
    """

    def test_each_delivery_covers_a_grain_the_others_do_not(self) -> None:
        campaigns = {
            stem: {
                str(row["campaign_key"])
                for row in _rows(f"amc-dsp-partial-overlap-{stem}", contract, suffix="csv")
            }
            for stem, contract in (
                ("impressions", "amc-dsp-impressions"),
                ("clicks", "amc-dsp-clicks"),
                ("views", "amc-dsp-views"),
            )
        }
        assert campaigns["impressions"] == {"TRAPDSP-SHARED", "TRAPDSP-IMPRONLY"}
        assert campaigns["clicks"] == {"TRAPDSP-SHARED", "TRAPDSP-CLICKONLY"}
        assert campaigns["views"] == {"TRAPDSP-SHARED"}

    def test_all_three_stamp_the_same_channel_so_nothing_but_columns_keeps_them_apart(
        self,
    ) -> None:
        """Which is why their withdrawable columns must be disjoint, asserted in the write tests."""
        for stem, contract in (
            ("impressions", "amc-dsp-impressions"),
            ("clicks", "amc-dsp-clicks"),
            ("views", "amc-dsp-views"),
        ):
            for row in _rows(f"amc-dsp-partial-overlap-{stem}", contract, suffix="csv"):
                assert row["channel_key"] == "amazon_dsp", stem
                assert row["ad_product_key"] == "amazon_dsp", stem

    def test_the_impressions_only_row_keeps_its_measures_and_its_scaled_money(self) -> None:
        """The divisor still has to be applied on the grain only impressions cover.

        100000 millicents ÷ 100,000 = 1.0000. A wrong divisor here is a five-order-of-magnitude
        error that still looks like money. The row is kept at the bar rather than suppressed:
        live, a suppressed row nulls its campaign, so it would cover no grain of its own.
        """
        rows = _rows("amc-dsp-partial-overlap-impressions", "amc-dsp-impressions", suffix="csv")
        only = next(row for row in rows if row["campaign_key"] == "TRAPDSP-IMPRONLY")

        assert only["rows_suppressed"] is False
        assert only["impressions"] == "2000"
        assert only["spend"] == Decimal("1.0000")
        assert only["currency_code"] == "USD"

    def test_every_row_of_all_three_is_acceptable_to_its_row_model(self) -> None:
        for stem, contract in (
            ("impressions", "amc-dsp-impressions"),
            ("clicks", "amc-dsp-clicks"),
            ("views", "amc-dsp-views"),
        ):
            validated = _validated(f"amc-dsp-partial-overlap-{stem}", contract, suffix="csv")
            assert validated, stem


class TestVendorRestatement:
    """The same period delivered twice. Revenue is net of returns and returns lag."""

    def test_both_deliveries_describe_the_same_grain(self) -> None:
        """Which is what makes this a restatement rather than two facts.

        A load keyed on the grain therefore collides, and must overwrite rather than append.
        """
        contract = _CONTRACTS["vendor-sales-by-asin"]
        earlier = _rows("vendor-restatement-earlier", "vendor-sales-by-asin")[0]
        later = _rows("vendor-restatement-later", "vendor-sales-by-asin")[0]

        key = tuple(contract.grain)
        assert tuple(earlier[column] for column in key) == tuple(later[column] for column in key)

    def test_the_watermark_distinguishes_them(self) -> None:
        """`lastUpdatedDate` is the restatement watermark and the only ordering signal.

        Without it in the curated row, an out-of-order delivery silently reverts corrected
        data and nothing downstream can tell.
        """
        earlier = _rows("vendor-restatement-earlier", "vendor-sales-by-asin")[0]
        later = _rows("vendor-restatement-later", "vendor-sales-by-asin")[0]

        assert earlier["source_watermark"] is not None
        assert later["source_watermark"] is not None
        assert str(later["source_watermark"]) > str(earlier["source_watermark"])

    def test_the_figures_actually_moved(self) -> None:
        """A trap where the two deliveries agreed would prove nothing about restatement."""
        earlier = _rows("vendor-restatement-earlier", "vendor-sales-by-asin")[0]
        later = _rows("vendor-restatement-later", "vendor-sales-by-asin")[0]

        assert earlier["revenue_shipped"] == Decimal("100.0000")
        assert later["revenue_shipped"] == Decimal("88.5000")

    def test_both_deliveries_are_acceptable(self) -> None:
        assert _validated("vendor-restatement-earlier", "vendor-sales-by-asin")
        assert _validated("vendor-restatement-later", "vendor-sales-by-asin")


class TestOptionDependentNulls:
    """`reportOptions` changes the effective schema, and the absences are by design."""

    def test_sourcing_omits_the_ordered_measures(self) -> None:
        """Absent, not zero. A SOURCING pull genuinely does not report them."""
        row = _rows("vendor-sourcing-nulls", "vendor-sales-by-asin")[0]

        assert row["distributor_view"] == "SOURCING"
        assert row["revenue_ordered"] is None
        assert row["units_ordered"] is None
        # The measures the option does report are unaffected.
        assert row["revenue_shipped"] == Decimal("100.0000")

    def test_fresh_omits_customer_returns(self) -> None:
        row = _rows("vendor-fresh-nulls", "vendor-sales-by-asin")[0]

        assert row["selling_program"] == "FRESH"
        assert row["customer_returns"] is None

    def test_both_are_acceptable_to_the_row_model(self) -> None:
        """Which requires the contract to have declared them nullable, and it does."""
        assert _validated("vendor-sourcing-nulls", "vendor-sales-by-asin")
        assert _validated("vendor-fresh-nulls", "vendor-sales-by-asin")

    def test_the_option_hash_separates_the_two_pulls(self) -> None:
        """A MANUFACTURING total summed with a SOURCING total double-counts.

        The hash is what keeps them in different partitions, so it must differ.
        """
        sourcing = _rows("vendor-sourcing-nulls", "vendor-sales-by-asin")[0]
        fresh = _rows("vendor-fresh-nulls", "vendor-sales-by-asin")[0]
        restated = _rows("vendor-restatement-later", "vendor-sales-by-asin")[0]

        hashes = {
            sourcing["report_options_hash"],
            fresh["report_options_hash"],
            restated["report_options_hash"],
        }
        assert len(hashes) == 3

    def test_the_option_dependent_columns_say_why_they_are_null(self) -> None:
        contract = _CONTRACTS["vendor-sales-by-asin"]
        for column in ("revenue_ordered", "units_ordered", "customer_returns"):
            mapping = contract.field_for(column)
            assert mapping is not None
            assert mapping.null_meaning is NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS, column


class TestNegativeUnits:
    """Cancellations are booked at cancellation time, so this is correct data."""

    def test_a_negative_ordered_unit_count_survives(self) -> None:
        row = _rows("vendor-negative-units", "vendor-sales-by-asin")[0]

        assert row["units_ordered"] == -3

    def test_a_negative_amount_survives_the_divisor(self) -> None:
        """Scaling must not lose the sign, and `abs()` anywhere would."""
        row = _rows("vendor-negative-units", "vendor-sales-by-asin")[0]

        assert row["revenue_ordered"] == Decimal("-45.5000")

    def test_the_row_is_acceptable(self) -> None:
        """The row model must not assume non-negativity, and nothing declares that it may."""
        assert _validated("vendor-negative-units", "vendor-sales-by-asin")

    def test_no_generated_dq_rule_would_reject_it(self) -> None:
        """Close the loop with the generated DQDL ruleset.

        It asserts no non-negativity anywhere, and this is the data that would fail if it did.
        """
        rulesets = json.loads((REPO_ROOT / "contracts/generated/dq-rulesets.json").read_text())[
            "rulesets"
        ]
        ruleset = next(r for r in rulesets if r["table"] == "fct_retail_sales")

        assert ">= 0" not in str(ruleset["ruleset"])


class TestMixedCurrency:
    """One `currency_code` per fact, so a document with two has no correct single value."""

    def test_the_two_rows_disagree_on_currency(self) -> None:
        """The disagreement must reach the curated rows, or nothing can detect it.

        A mapper that took the first currency and applied it to both would produce a document
        that looks internally consistent and reports EUR revenue as USD.
        """
        rows = _rows("vendor-mixed-currency", "vendor-sales-by-asin")

        assert {row["currency_code"] for row in rows} == {"USD", "EUR"}

    def test_the_contract_names_the_paths_that_must_agree(self) -> None:
        """ADR-021: three amounts collapse onto one column, so agreement is asserted at load.

        This is the declaration the load assertion reads. Without it the shared column would be
        a silent assumption rather than a checked one.
        """
        contract = _CONTRACTS["vendor-sales-by-asin"]

        assert len(contract.currency_agreement) == 3
        assert all(path.endswith(".currencyCode") for path in contract.currency_agreement)

    def test_each_row_is_individually_acceptable(self) -> None:
        """The rows are well formed; it is the *rollup* that has no single currency.

        Which is why this cannot be caught by a row model and needs a load assertion.
        """
        assert len(_validated("vendor-mixed-currency", "vendor-sales-by-asin")) == 2


class TestTheTrapsThemselves:
    def test_every_trap_has_a_manifest(self) -> None:
        """Upstream is an untrusted producer, so a fixture without one is not realistic."""
        data = [
            path
            for path in TRAPS.iterdir()
            if path.suffix in {".csv", ".json", ".jsonl"}
            and not path.name.endswith(".manifest.json")
        ]
        assert data
        for path in data:
            assert (TRAPS / f"{path.stem}.manifest.json").exists(), path.name

    def test_no_trap_names_a_real_advertiser(self) -> None:
        """Never commit a fixture containing real customer data or a real advertiser's ASIN."""
        for path in TRAPS.glob("*.json"):
            text = path.read_text()
            for asin in _asins(text):
                assert asin.startswith("B0TRAP"), f"{path.name}: {asin}"

    def test_no_trap_fixture_is_shadowed_in_the_generated_directory(self) -> None:
        """`make generate-fixtures` deletes anything it did not write, so a trap there is lost.

        Both directories have their own README, which is why this checks fixtures rather than
        every filename.
        """
        generated = REPO_ROOT / "tests/fixtures/generated"
        fixtures = [path for path in TRAPS.iterdir() if path.suffix in {".csv", ".json", ".jsonl"}]
        assert fixtures
        for path in fixtures:
            assert not (generated / path.name).exists(), path.name


def _asins(text: str) -> list[str]:
    """List every ASIN-shaped literal in a fixture, so the no-real-advertiser rule is checked."""
    return re.findall(r'"(B0[A-Z0-9]{8})"', text)


@pytest.mark.parametrize("contract_id", ["amc-dsp-impressions", "vendor-sales-by-asin"])
def test_a_trapped_contract_is_one_of_the_real_committed_contracts(contract_id: str) -> None:
    """A trap written against an invented contract would test the trap and nothing else."""
    assert isinstance(_CONTRACTS[contract_id], DataContract)
