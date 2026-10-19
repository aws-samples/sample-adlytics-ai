# SPDX-License-Identifier: MIT-0
"""The load assertions: what must be true before a single row is written.

Each of these catches a condition where the data is *plausible*. That is the whole point — a
malformed file fails on its own, and these exist for the files that parse cleanly and are
wrong. So every test here asserts a quarantine on input that a reader accepts without
complaint.

They all fail the same way, and the way matters more than the checks: quarantine the
partition and alarm, never drop a row, never coerce a value, never touch the last good
partition. Dimension resolution needs `dim_product` loaded and is tested with the merge.

Period overlap is the newest and the sharpest example of the module's premise. Every other
assertion here catches a delivery that is internally inconsistent; this one catches two
deliveries that are each perfectly correct and cannot both be live.
"""

import pathlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.readers import DeliveredFile, read
from domain.curation.assertions import (
    AssertionName,
    ControlTotalRequest,
    CurrencyAgreementRequest,
    HeaderRequest,
    LoadOutcome,
    NonAdditiveSuppressionRequest,
    PeriodOverlapRequest,
    Violation,
    check_control_total,
    check_currency_agreement,
    check_header,
    check_non_additive_suppression,
    check_period_overlap,
    detail_and_total,
    measure_columns,
)
from domain.curation.mapping import MappingRequest, SourceDocument, map_rows
from domain.data_contracts.contract import DataContract
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATED = REPO_ROOT / "tests/fixtures/generated"
TRAPS = REPO_ROOT / "tests/fixtures/traps"

_REGISTRY = load_registry(REPO_ROOT)
_CONTRACTS = {contract.contract_id: contract for _, contract in load_contracts(REPO_ROOT)}


def _read(path: pathlib.Path, contract: DataContract) -> SourceDocument:
    return read(
        DeliveredFile(
            text=path.read_text(), source_format=contract.source_format, manifest_text="{}"
        )
    )


def _rows(path: pathlib.Path, contract: DataContract) -> tuple[dict[str, object], ...]:
    return map_rows(MappingRequest(contract=contract, source=_read(path, contract)))


def _delivered_header(name: str) -> frozenset[str]:
    return frozenset((GENERATED / f"{name}.csv").read_text().splitlines()[0].split(","))


class TestHeaderAssertion:
    """The one assertion whose failure cannot be recovered from downstream."""

    def test_a_delivered_file_with_every_suppression_column_passes(self) -> None:
        contract = _CONTRACTS["amc-dsp-impressions"]
        outcome = check_header(
            HeaderRequest(
                contract=contract, delivered_columns=_delivered_header("amc-dsp-impressions")
            )
        )

        assert outcome.quarantines is False

    def test_a_missing_discriminator_quarantines_and_says_why(self) -> None:
        """Without the flag, privacy-nulled values look like ordinary nulls forever."""
        contract = _CONTRACTS["amc-dsp-impressions"]
        without = _delivered_header("amc-dsp-impressions") - {"rows_suppressed"}
        outcome = check_header(HeaderRequest(contract=contract, delivered_columns=without))

        assert outcome.quarantines is True
        violation = outcome.violations[0]
        assert violation.assertion is AssertionName.HEADER
        assert "rows_suppressed" in violation.detail
        assert "privacy-null values" in violation.detail

    def test_every_missing_column_is_named_at_once(self) -> None:
        """Discovering the next one on the next load turns one fix into a week of them."""
        contract = _CONTRACTS["amc-dsp-impressions"]
        without = _delivered_header("amc-dsp-impressions") - {
            "rows_suppressed",
            "suppression_reason",
            "distinct_user_count",
        }
        outcome = check_header(HeaderRequest(contract=contract, delivered_columns=without))

        detail = outcome.violations[0].detail
        for column in ("rows_suppressed", "suppression_reason", "distinct_user_count"):
            assert column in detail

    def test_a_source_with_no_threshold_mechanism_is_not_checked(self) -> None:
        """A vendor report has no privacy threshold, so there is nothing to assert."""
        contract = _CONTRACTS["vendor-sales-by-asin"]
        assert contract.suppression is None

        outcome = check_header(HeaderRequest(contract=contract, delivered_columns=frozenset()))
        assert outcome.quarantines is False

    def test_it_checks_the_delivered_names_not_the_source_names(self) -> None:
        """An extraction-option column is aliased to its canonical name by the extraction.

        Checking `values_removed` would fail against every file our own extraction produces.
        """
        contract = _CONTRACTS["amc-dsp-impressions"]
        outcome = check_header(
            HeaderRequest(
                contract=contract,
                delivered_columns=frozenset(
                    {"rows_suppressed", "suppression_reason", "distinct_user_count"}
                ),
            )
        )

        assert outcome.quarantines is False


class TestNonAdditiveSuppression:
    def test_suppressed_reach_quarantines_the_delivery(self) -> None:
        contract = _CONTRACTS["amc-reach-frequency"]
        outcome = check_non_additive_suppression(
            NonAdditiveSuppressionRequest(
                contract,
                ({"rows_suppressed": "true", "reach_users": 1},),
            )
        )
        assert outcome.quarantines
        assert outcome.violations[0].assertion is AssertionName.NON_ADDITIVE_SUPPRESSION
        assert "reach_users" in outcome.violations[0].detail

    def test_clean_reach_and_additive_suppression_remain_usable(self) -> None:
        reach = _CONTRACTS["amc-reach-frequency"]
        impressions = _CONTRACTS["amc-dsp-impressions"]
        clean = check_non_additive_suppression(
            NonAdditiveSuppressionRequest(reach, ({"rows_suppressed": False},))
        )
        additive = check_non_additive_suppression(
            NonAdditiveSuppressionRequest(impressions, ({"rows_suppressed": True},))
        )
        assert not clean.quarantines
        assert not additive.quarantines

    def test_unknown_suppression_spelling_cannot_fail_open(self) -> None:
        reach = _CONTRACTS["amc-reach-frequency"]
        outcome = check_non_additive_suppression(
            NonAdditiveSuppressionRequest(reach, ({"rows_suppressed": "yes"},))
        )
        assert outcome.quarantines
        assert "invalid rows_suppressed" in outcome.violations[0].detail


class TestCurrencyAgreement:
    """One `currency_code` per fact (ADR-021), so the narrowing must be checked not assumed."""

    def _vendor(self) -> DataContract:
        return _CONTRACTS["vendor-sales-by-asin"]

    def _document(self, rows: list[dict[str, Any]]) -> SourceDocument:
        return SourceDocument(
            document={
                "reportSpecification": {
                    "marketplaceIds": ["ATVPDKIKX0DER"],
                    "reportOptions": {"reportPeriod": "DAY"},
                },
                "salesByAsin": rows,
            }
        )

    def test_a_document_whose_currencies_agree_passes(self) -> None:
        contract = self._vendor()
        source = _read(GENERATED / "vendor-sales-by-asin.json", contract)
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert outcome.quarantines is False

    def test_two_amounts_on_one_row_disagreeing_quarantines(self) -> None:
        """The case ADR-021 names: revenue in USD beside COGS in EUR on the same ASIN and day.

        No single `currency_code` is correct, so storing either misreports the other amount.
        """
        contract = self._vendor()
        source = self._document(
            [
                {
                    "asin": "B0TEST0001",
                    "shippedRevenue": {"amount": 100, "currencyCode": "USD"},
                    "shippedCogs": {"amount": 60, "currencyCode": "EUR"},
                    "orderedRevenue": {"amount": 110, "currencyCode": "USD"},
                }
            ]
        )
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert outcome.quarantines is True
        assert "row 0" in outcome.violations[0].detail
        assert "EUR and USD" in outcome.violations[0].detail

    def test_two_rows_disagreeing_quarantines_the_rollup(self) -> None:
        """One report is one marketplace, so a second currency contradicts the document.

        Read straight off the trap fixture: every row is internally consistent, which is what
        makes this the *other* half of the check rather than a repeat of the first.
        """
        contract = self._vendor()
        source = _read(TRAPS / "vendor-mixed-currency.json", contract)
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert outcome.quarantines is True
        assert len(outcome.violations) == 1, "each row is internally consistent"
        assert "across its rows" in outcome.violations[0].detail

    def test_an_absent_currency_is_not_a_disagreement(self) -> None:
        """A SOURCING pull omits `orderedRevenue` entirely, so its currency is legitimately gone.

        Treating absence as disagreement would quarantine every SOURCING load.
        """
        contract = self._vendor()
        source = _read(TRAPS / "vendor-sourcing-nulls.json", contract)
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert outcome.quarantines is False

    def test_both_kinds_of_disagreement_are_reported_together(self) -> None:
        contract = self._vendor()
        source = self._document(
            [
                {
                    "asin": "B0TEST0001",
                    "shippedRevenue": {"amount": 100, "currencyCode": "USD"},
                    "shippedCogs": {"amount": 60, "currencyCode": "EUR"},
                },
                {
                    "asin": "B0TEST0002",
                    "shippedRevenue": {"amount": 90, "currencyCode": "GBP"},
                    "shippedCogs": {"amount": 50, "currencyCode": "GBP"},
                },
            ]
        )
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert len(outcome.violations) == 2

    def test_a_flat_format_has_nothing_to_compare(self) -> None:
        """An AMC contract supplies one currency column, so there is no collapse to assert."""
        contract = _CONTRACTS["amc-dsp-impressions"]
        assert contract.currency_agreement == ()

        source = SourceDocument(rows=({"currency_iso_code": "USD"},))
        rows = map_rows(MappingRequest(contract=contract, source=source))
        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )

        assert outcome.quarantines is False


class TestPairingTheTwoGrains:
    def test_both_detail_and_total_pairs_are_found_from_the_contracts(self) -> None:
        """Derived, not configured: two contracts on one dataset writing different tables.

        The total is the one whose grain is a strict subset — the by-ASIN contract's extra key
        *is* the difference between the two grains.
        """
        pairs = detail_and_total(list(_CONTRACTS.values()))
        named = {(detail.contract_id, total.contract_id) for detail, total in pairs}

        assert named == {
            ("vendor-sales-by-asin", "vendor-sales-reported-total"),
            ("vendor-traffic-by-asin", "vendor-traffic-reported-total"),
        }

    def test_a_measure_is_numeric_and_outside_the_grain(self) -> None:
        """`currency_code` and `source_watermark` are on both contracts and are not quantities.

        Summing a watermark would be meaningless and summing a currency code impossible, so the
        set is derived from type and structural role rather than listed by hand.
        """
        measures = measure_columns(_CONTRACTS["vendor-sales-by-asin"], _REGISTRY)

        assert "revenue_shipped" in measures
        assert "units_shipped" in measures
        assert "currency_code" not in measures
        assert "source_watermark" not in measures
        assert "date_key" not in measures
        assert "marketplace_key" not in measures


class TestControlTotal:
    """The detail rows must sum to the total the same document reports."""

    def _pair(self, fixture: str) -> ControlTotalRequest:
        detail = _CONTRACTS["vendor-sales-by-asin"]
        total = _CONTRACTS["vendor-sales-reported-total"]
        path = TRAPS / f"{fixture}.json"
        return ControlTotalRequest(
            detail_rows=_rows(path, detail),
            total_rows=_rows(path, total),
            measures=measure_columns(detail, _REGISTRY) & measure_columns(total, _REGISTRY),
            label=fixture,
        )

    def test_a_document_whose_grains_agree_passes(self) -> None:
        """Both arrays in one document, which is what a real vendor report carries.

        The generated fixtures cannot exercise this: the generator emits one array per
        contract, because a contract is one grain. This is hand-built for that reason.
        """
        outcome = check_control_total(self._pair("vendor-control-total-consistent"))

        assert outcome.quarantines is False

    def test_a_truncated_array_quarantines_although_every_row_is_correct(self) -> None:
        """The failure this exists for. Nothing is null and nothing raises.

        One ASIN is missing from the detail array. The two surviving rows are individually
        right, the reported total is right, and the sum of the parts is quietly short.
        """
        outcome = check_control_total(self._pair("vendor-control-total-short"))

        assert outcome.quarantines is True
        detail = " ".join(violation.detail for violation in outcome.violations)
        assert "revenue_shipped sums to 75.0000" in detail
        assert "reports 100.0000" in detail

    def test_every_disagreeing_measure_is_reported_not_just_the_first(self) -> None:
        outcome = check_control_total(self._pair("vendor-control-total-short"))
        columns = {dict(violation.context)["column"] for violation in outcome.violations}

        assert columns == {
            "revenue_shipped",
            "revenue_ordered",
            "cogs_shipped",
            "units_shipped",
            "units_ordered",
            "customer_returns",
        }

    def test_a_document_with_no_aggregate_row_cannot_be_checked_and_says_so(self) -> None:
        """Silently passing would make the assertion optional in practice."""
        outcome = check_control_total(
            ControlTotalRequest(
                detail_rows=({"revenue_shipped": Decimal("10")},),
                total_rows=(),
                measures=frozenset({"revenue_shipped"}),
            )
        )

        assert outcome.quarantines is True
        assert "no aggregate row" in outcome.violations[0].detail

    def test_a_measure_absent_from_both_grains_agrees(self) -> None:
        """A SOURCING pull omits `orderedRevenue` from the detail *and* the aggregate.

        Summing absent values to zero would make that look like a disagreement with a reported
        zero, so absence has to compare equal to absence.
        """
        outcome = check_control_total(
            ControlTotalRequest(
                detail_rows=({"revenue_ordered": None}, {"revenue_ordered": None}),
                total_rows=({"revenue_ordered": None},),
                measures=frozenset({"revenue_ordered"}),
            )
        )

        assert outcome.quarantines is False

    def test_an_absent_detail_against_a_reported_total_is_a_disagreement(self) -> None:
        """The report cannot both omit a measure and report it."""
        outcome = check_control_total(
            ControlTotalRequest(
                detail_rows=({"revenue_shipped": None},),
                total_rows=({"revenue_shipped": Decimal("100")},),
                measures=frozenset({"revenue_shipped"}),
            )
        )

        assert outcome.quarantines is True
        assert "sums to nothing" in outcome.violations[0].detail

    def test_equality_is_exact_and_that_is_deliberate(self) -> None:
        """A tolerance chosen now would be a guess, and the guarded condition is not subtle.

        A dropped ASIN is off by far more than rounding, so quarantining on a rounding
        difference is the safe direction to be wrong in. Revisit against a real delivered file.
        """
        outcome = check_control_total(
            ControlTotalRequest(
                detail_rows=({"revenue_shipped": Decimal("99.9999")},),
                total_rows=({"revenue_shipped": Decimal("100.0000")},),
                measures=frozenset({"revenue_shipped"}),
            )
        )

        assert outcome.quarantines is True


@pytest.mark.covers("ADR-043")
class TestPeriodOverlap:
    """The assertion whose absence produces a plausible number rather than a missing one.

    A fact partition is identified by its period bounds, so two intersecting deliveries stay live
    together and every shared day is counted once per partition. Nothing errors and nothing is
    null, which is why this needs an assertion rather than a reader.
    """

    @staticmethod
    def _contract() -> DataContract:
        return _CONTRACTS["amc-reach-frequency"]

    def test_a_first_load_with_nothing_beside_it_passes(self) -> None:
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 1), end=date(2026, 8, 1)),
            )
        )
        assert outcome.quarantines is False

    def test_adjacent_periods_do_not_overlap(self) -> None:
        """Consecutive days must load cleanly, or a daily schedule quarantines from day two."""
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 2), end=date(2026, 8, 2)),
                loaded=(DateRange(start=date(2026, 8, 1), end=date(2026, 8, 1)),),
            )
        )
        assert outcome.quarantines is False

    def test_an_identical_period_is_a_restatement_and_not_a_violation(self) -> None:
        """A re-pull of the same window replaces its partition wholesale.

        This is the mechanism a maturing attribution window depends on, so refusing it would
        break the one re-pull pattern the watermark was built for.
        """
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 1), end=date(2026, 8, 7)),
                loaded=(DateRange(start=date(2026, 8, 1), end=date(2026, 8, 7)),),
            )
        )
        assert outcome.quarantines is False

    def test_a_slid_window_quarantines(self) -> None:
        """The failure a scheduled rolling pull produces on its second run."""
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 2), end=date(2026, 8, 15)),
                loaded=(DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14)),),
            )
        )
        assert outcome.quarantines is True
        assert outcome.violations[0].assertion is AssertionName.PERIOD_OVERLAP
        assert "2026-08-01..2026-08-14" in outcome.violations[0].detail

    def test_a_period_containing_a_loaded_one_quarantines(self) -> None:
        """A widened window is the other way to reach the same double count."""
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14)),
                loaded=(DateRange(start=date(2026, 8, 3), end=date(2026, 8, 3)),),
            )
        )
        assert outcome.quarantines is True

    def test_every_overlapping_period_is_named_not_just_the_first(self) -> None:
        """One correction per load is the rule the other assertions follow too."""
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14)),
                loaded=(
                    DateRange(start=date(2026, 8, 3), end=date(2026, 8, 3)),
                    DateRange(start=date(2026, 8, 9), end=date(2026, 8, 9)),
                    DateRange(start=date(2026, 9, 1), end=date(2026, 9, 1)),
                ),
            )
        )
        detail = outcome.violations[0].detail
        assert "2026-08-03..2026-08-03" in detail
        assert "2026-08-09..2026-08-09" in detail
        assert "2026-09-01" not in detail

    def test_the_violation_says_what_to_do_instead(self) -> None:
        """The message has to name the fix rather than only the fault.

        The correct reading of "rolling fourteen days" is not the obvious one, so a violation
        reporting only the clash would leave the reader to guess at the remedy.
        """
        outcome = check_period_overlap(
            PeriodOverlapRequest(
                contract=self._contract(),
                period=DateRange(start=date(2026, 8, 2), end=date(2026, 8, 15)),
                loaded=(DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14)),),
            )
        )
        assert "supersede" in outcome.violations[0].detail


class TestRangeIntersection:
    """`DateRange.overlaps`, which is the opposite question to `covers` and needed separately."""

    def test_containment_implies_intersection(self) -> None:
        outer = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 31))
        inner = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 12))
        assert outer.covers(inner) is True
        assert outer.overlaps(inner) is True

    def test_intersection_does_not_imply_containment(self) -> None:
        """The asymmetry is the reason both exist.

        A half-covered window answers a question that looks whole; a half-shared window double
        counts.
        """
        first = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14))
        second = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 20))
        assert first.overlaps(second) is True
        assert first.covers(second) is False

    def test_a_shared_single_day_is_an_intersection(self) -> None:
        first = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 10))
        second = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 20))
        assert first.overlaps(second) is True

    def test_adjacent_ranges_do_not_intersect(self) -> None:
        first = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 9))
        second = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 20))
        assert first.overlaps(second) is False

    def test_intersection_is_symmetric(self) -> None:
        first = DateRange(start=date(2026, 8, 1), end=date(2026, 8, 14))
        second = DateRange(start=date(2026, 8, 10), end=date(2026, 8, 20))
        assert first.overlaps(second) == second.overlaps(first)


class TestTheOutcome:
    def test_no_violations_means_the_partition_is_written(self) -> None:
        assert LoadOutcome().quarantines is False

    def test_outcomes_combine_so_every_assertion_runs_before_the_decision(self) -> None:
        """Running them all first is what makes one quarantine report every reason."""
        first = LoadOutcome(violations=(Violation(assertion=AssertionName.HEADER, detail="a"),))
        second = LoadOutcome(
            violations=(Violation(assertion=AssertionName.CONTROL_TOTAL, detail="b"),)
        )
        combined = first.combined(second)

        assert len(combined.violations) == 2
        assert combined.quarantines is True

    def test_a_violation_must_say_what_failed(self) -> None:
        """An unexplained quarantine is indistinguishable from an outage."""
        with pytest.raises(ValueError, match="must say what failed"):
            Violation(assertion=AssertionName.HEADER, detail="")

    def test_context_is_separate_from_the_description(self) -> None:
        """Context is logged; a description can reach a caller. ADR-010 keeps them apart."""
        violation = Violation(
            assertion=AssertionName.CONTROL_TOTAL,
            detail="revenue_shipped is short",
            context=(("column", "revenue_shipped"),),
        )

        assert dict(violation.context)["column"] == "revenue_shipped"
        assert "revenue_shipped" in violation.detail


class TestEveryGeneratedFixturePassesEveryAssertion:
    """A generated fixture is what a well-behaved delivery looks like.

    If one of these quarantined, the assertion would be rejecting the shape the contracts
    themselves describe — which is a defect in the assertion rather than in the data.
    """

    @pytest.mark.parametrize(
        "contract_id",
        [contract.contract_id for contract in _CONTRACTS.values() if contract.suppression],
    )
    def test_the_header_assertion_passes(self, contract_id: str) -> None:
        contract = _CONTRACTS[contract_id]
        outcome = check_header(
            HeaderRequest(contract=contract, delivered_columns=_delivered_header(contract_id))
        )

        assert outcome.quarantines is False

    @pytest.mark.parametrize(
        "contract_id",
        [contract.contract_id for contract in _CONTRACTS.values() if contract.currency_agreement],
    )
    def test_the_currency_assertion_passes(self, contract_id: str) -> None:
        contract = _CONTRACTS[contract_id]
        source = _read(GENERATED / f"{contract_id}.json", contract)
        rows = map_rows(MappingRequest(contract=contract, source=source))

        outcome = check_currency_agreement(
            CurrencyAgreementRequest(contract=contract, source=source, rows=rows)
        )
        assert outcome.quarantines is False
