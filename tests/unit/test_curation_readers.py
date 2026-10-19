# SPDX-License-Identifier: MIT-0
"""Reading a delivered file and mapping it into canonical rows.

The two halves of curation that exist so far, tested together at the end because the pair is
what matters: a reader that parses and a mapper that maps are only correct if a real delivered
file survives both and satisfies the contract's own row model.

Three properties are load-bearing and each has a specific failure it prevents.

**JSON numbers become `Decimal`, never `float`.** A vendor `Amount.amount` may arrive as a bare
number, and `11.37` has no exact binary representation — so a float would carry an error before
any divisor is applied, and every later sum would keep it.

**The divisor is applied in decimal arithmetic and rounded with a stated mode.** A wrong or
missing divisor is a silent order-of-magnitude error that still reconciles at the headline.

**`reportOptions` becomes a stable hash.** It is dataset identity rather than a runtime flag: a
MANUFACTURING pull summed with a SOURCING pull double-counts, and the hash is what keeps them
in separate partitions.
"""

import json
import pathlib
from decimal import Decimal

import pytest

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.readers import DeliveredFile, ReaderError, read
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.schema_generation.models import class_name_for
from domain.curation.ingestion import split_discarded
from domain.curation.mapping import (
    MappingError,
    MappingRequest,
    SourceDocument,
    map_rows,
    options_hash,
    scaled,
    suppression_flag,
)
from domain.data_contracts import generated_rows
from domain.data_contracts.contract import (
    ContractError,
    DataContract,
    FieldMapping,
    SourceFormat,
)
from domain.data_contracts.schema import BASIS_KEY, SOURCE_TIMEZONE
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests/fixtures/generated"

EXTENSIONS = {
    SourceFormat.CSV: "csv",
    SourceFormat.JSON_DUAL_ARRAY: "json",
    SourceFormat.JSONL: "jsonl",
}

# The SCD2 merge assigns these; the mapper cannot know them. Supplied here so a dimension row
# can be validated as it will actually be written.
LOAD_ASSIGNED = {
    "valid_from": "2026-08-19T00:00:00Z",
    "valid_to": "2999-12-31T00:00:00Z",
    "campaign_key": "campaign:synthetic",
    "advertiser_key": "advertiser:synthetic",
    "commerce_scope_key": "amazon_retail_us",
    "ecosystem_key": "amazon",
    "platform_key": "amazon_ads",
    "source_system_key": "amc",
    "source_scope_namespace": "amc.entity",
    "source_scope_value": "fictional-fixture-amc-entity",
    "channel_group_key": "display",
    "channel_key": "amazon_dsp",
    "ad_product_key": "amazon_dsp",
    # A periodic shopper aggregate's period comes from the row's measurement bucket, and its
    # cell_state from the load's closed enumeration (amc-new-to-brand-buyers-platform).
    "period_start": "2026-08-03",
    "period_end": "2026-08-09",
    "time_grain": "week",
    "cell_state": "returned",
    # Pack 5's canonical exposure group, resolved by the load from exposure_source_set through
    # the taxonomy (amc-exposure-groups, ADR-101).
    "exposure_group": "dsp_only",
    # Packs 14 and 15's vintage month comes from the period, and the acquisition channel group
    # from acquisition_source_set through the same taxonomy section (ADR-104, ADR-101).
    "as_of_month": "2026-09-01",
    "acquisition_channel_group": "dsp_only",
    # Packs 6 and 7 carry attribution_model_key for a credit branch not built (plan D11, D12):
    # NULL on every window row.
    "attribution_model_key": None,
    # Pack 9's DSP segments carry no categories (ASI only), and every segment's provider is
    # stamped by the load from the governed classification, NULL where no rule covers it.
    "category_level_1": None,
    "category_level_2": None,
    "category_path": None,
    "provider": None,
}


def _contract(**overrides: object) -> DataContract:
    """Build a flat CSV contract with one column, for overriding a field at a time."""
    defaults: dict[str, object] = {
        "contract_id": "test-contract",
        "contract_version": 1,
        "source_system": "amc",
        "source_dataset": "dsp_impressions",
        "source_format": SourceFormat.CSV,
        "target_table": "fct_ad_exposure",
        "grain": ("date_key",),
        "partition_keys": ("date_key",),
        "measurement_basis": MeasurementBasis.AD_ATTRIBUTED,
        "source_timezone": "UTC",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "restatement_window_days": 30,
        "staleness_threshold_hours": 72,
        "source_tables": (),
        "fields": (
            FieldMapping(source_path="date_key", target_column="date_key", target_type="date"),
        ),
    }
    return DataContract(**(defaults | overrides))  # type: ignore[arg-type]


def _mapped(contract: DataContract, source: SourceDocument) -> tuple[dict[str, object], ...]:
    return map_rows(MappingRequest(contract=contract, source=source))


class TestReadingCsv:
    def test_a_quoted_value_containing_a_comma_survives(self) -> None:
        """A product title routinely contains one, and splitting on commas would shear it."""
        text = 'asin,product_title\nB0TEST0001,"Widget, large"\n'
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.CSV)).rows

        assert rows[0]["product_title"] == "Widget, large"

    def test_a_file_with_no_header_is_refused(self) -> None:
        """Without a header no column can be identified, so every value is anonymous."""
        with pytest.raises(ReaderError, match="no header row"):
            read(DeliveredFile(text="", source_format=SourceFormat.CSV))

    def test_a_short_row_keeps_none_so_the_row_model_names_the_column(self) -> None:
        """Better than the reader guessing: the model reports which column is missing."""
        text = "asin,product_title\nB0TEST0001\n"
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.CSV)).rows

        assert rows[0]["product_title"] is None

    def test_surplus_values_are_dropped_rather_than_rejected(self) -> None:
        """Additive tolerance: a delivered file that gained a column must still load."""
        text = "asin\nB0TEST0001,surplus,values\n"
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.CSV)).rows

        assert rows[0] == {"asin": "B0TEST0001"}

    def test_every_csv_value_stays_a_string(self) -> None:
        """The generated row model is what types them, and it needs the raw text to do it."""
        text = "impressions\n137\n"
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.CSV)).rows

        assert rows[0]["impressions"] == "137"


class TestReadingJsonl:
    def test_each_line_is_one_record(self) -> None:
        text = '{"asin":"B0A"}\n{"asin":"B0B"}\n'
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.JSONL)).rows

        assert [row["asin"] for row in rows] == ["B0A", "B0B"]

    def test_a_blank_line_is_not_a_record(self) -> None:
        """A trailing newline is normal; an empty record is not."""
        text = '{"asin":"B0A"}\n\n{"asin":"B0B"}\n\n'
        rows = read(DeliveredFile(text=text, source_format=SourceFormat.JSONL)).rows

        assert len(rows) == 2

    def test_a_line_that_is_not_an_object_names_its_line_number(self) -> None:
        text = '{"asin":"B0A"}\n["not","an","object"]\n'
        with pytest.raises(ReaderError, match="line 2"):
            read(DeliveredFile(text=text, source_format=SourceFormat.JSONL))


class TestReadingJson:
    def test_a_number_becomes_a_decimal_and_never_a_float(self) -> None:
        """The single most important line in the reader.

        `11.37` has no exact binary representation, so a float would already carry an error
        before any divisor was applied, and every later sum would preserve it.
        """
        text = '{"salesByAsin":[{"shippedRevenue":{"amount":11.37}}]}'
        document = read(
            DeliveredFile(text=text, source_format=SourceFormat.JSON_DUAL_ARRAY)
        ).document
        assert document is not None
        amount = document["salesByAsin"][0]["shippedRevenue"]["amount"]

        assert isinstance(amount, Decimal)
        assert amount == Decimal("11.37")
        assert not isinstance(amount, float)

    def test_an_integer_stays_an_integer(self) -> None:
        """A scaled monetary value arrives as an integer, and dividing it is the mapper's job."""
        text = '{"salesByAsin":[{"shippedUnits":137}]}'
        document = read(
            DeliveredFile(text=text, source_format=SourceFormat.JSON_DUAL_ARRAY)
        ).document
        assert document is not None
        assert document["salesByAsin"][0]["shippedUnits"] == 137

    def test_malformed_json_says_what_could_not_be_parsed(self) -> None:
        with pytest.raises(ReaderError, match="report document is not valid JSON"):
            read(DeliveredFile(text="{oops", source_format=SourceFormat.JSON_DUAL_ARRAY))

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(ReaderError, match="must be a JSON object"):
            read(DeliveredFile(text="[1,2,3]", source_format=SourceFormat.JSON_DUAL_ARRAY))


class TestTheManifest:
    def test_it_is_parsed_alongside_the_data(self) -> None:
        payload = DeliveredFile(
            text="asin\nB0A\n",
            source_format=SourceFormat.CSV,
            manifest_text='{"retrieved_at":"2026-08-19T06:00:00Z"}',
        )
        assert read(payload).manifest["retrieved_at"] == "2026-08-19T06:00:00Z"

    def test_an_absent_manifest_reads_as_empty_rather_than_failing(self) -> None:
        """Refusing a load with no manifest is a load assertion's job, not a parser's.

        Keeping the reader usable without one is what lets a reader be tested alone.
        """
        payload = DeliveredFile(text="asin\nB0A\n", source_format=SourceFormat.CSV)
        assert read(payload).manifest == {}

    def test_a_manifest_that_is_not_an_object_is_refused(self) -> None:
        payload = DeliveredFile(
            text="asin\nB0A\n", source_format=SourceFormat.CSV, manifest_text="[]"
        )
        with pytest.raises(ReaderError, match="manifest must be a JSON object"):
            read(payload)


class TestScalingMoney:
    def test_a_millicent_integer_becomes_a_currency_decimal(self) -> None:
        assert scaled(897_000, 100_000) == Decimal("8.9700")

    def test_the_result_is_a_decimal_and_never_a_float(self) -> None:
        """A float divisor would reintroduce exactly the error the Decimal parse avoided."""
        value = scaled("1137000", 100_000)
        assert isinstance(value, Decimal)
        assert value == Decimal("11.37")

    def test_a_divisor_of_one_leaves_the_amount_alone(self) -> None:
        """Vendor money arrives already in currency, so its divisor is 1 rather than absent."""
        assert scaled(Decimal("11.37"), 1) == Decimal("11.3700")

    def test_more_precision_than_the_column_holds_is_rounded_half_even(self) -> None:
        """`decimal(38,4)` cannot hold five places, so something must round.

        Doing it here with a stated mode makes the rule visible; half-even avoids the upward
        drift half-up would accumulate across the thousands of rows spend is summed over, and
        spend biased upward deflates every ROAS built on it.
        """
        # 12.34565 and 12.34575 both sit exactly halfway, and half-even goes to the even digit.
        assert scaled(1_234_565, 100_000) == Decimal("12.3456")
        assert scaled(1_234_575, 100_000) == Decimal("12.3458")

    def test_scaling_nothing_is_an_error_rather_than_a_zero(self) -> None:
        """A zero would be indistinguishable from a real zero amount."""
        with pytest.raises(MappingError, match="cannot be scaled from nothing"):
            scaled(None, 100_000)


class TestHashingAnOptionSet:
    def test_the_same_options_hash_the_same_whatever_their_key_order(self) -> None:
        """A digest depending on key order would change without the options changing."""
        assert options_hash({"a": 1, "b": 2}) == options_hash({"b": 2, "a": 1})

    def test_different_options_hash_differently(self) -> None:
        """This is what keeps a MANUFACTURING pull from being summed with a SOURCING one."""
        manufacturing = {"distributorView": "MANUFACTURING", "reportPeriod": "DAY"}
        sourcing = {"distributorView": "SOURCING", "reportPeriod": "DAY"}
        assert options_hash(manufacturing) != options_hash(sourcing)

    def test_a_decimal_in_the_options_does_not_break_the_digest(self) -> None:
        """The reader parses JSON numbers as Decimal, which json.dumps cannot serialise."""
        assert options_hash({"threshold": Decimal("1.5")})

    def test_the_digest_is_short_enough_to_be_a_partition_directory(self) -> None:
        assert len(options_hash({"a": 1})) == 16


class TestMappingProvenance:
    @pytest.mark.parametrize("value", (True, "true", "values_removed"))
    def test_governed_suppression_values_normalize_to_true(self, value: object) -> None:
        assert suppression_flag(value) is True

    @pytest.mark.parametrize("value", (False, "false", ""))
    def test_governed_clear_values_normalize_to_false(self, value: object) -> None:
        assert suppression_flag(value) is False

    def test_unknown_suppression_value_is_refused(self) -> None:
        with pytest.raises(MappingError, match="unknown value"):
            suppression_flag("unexpected")

    def test_a_literal_is_stamped_rather_than_read(self) -> None:
        """DSP tables carry no ad_product_type, so the channel is knowledge we hold."""
        contract = _contract(
            fields=(
                FieldMapping(
                    target_column="channel_key", literal_value="amazon_dsp", target_type="string"
                ),
            )
        )
        rows = _mapped(contract, SourceDocument(rows=({"anything": "x"},)))

        assert rows[0]["channel_key"] == "amazon_dsp"

    def test_a_load_assigned_column_is_absent_from_the_mapped_row(self) -> None:
        """The merge computes it by comparing loads, so the mapper must not invent it."""
        contract = _contract(
            fields=(
                FieldMapping(source_path="asin", target_column="amazon_asin", target_type="string"),
                FieldMapping(
                    target_column="valid_from", assigned_by_load=True, target_type="timestamp"
                ),
            )
        )
        rows = _mapped(contract, SourceDocument(rows=({"asin": "B0A"},)))

        assert "valid_from" not in rows[0]
        assert rows[0]["amazon_asin"] == "B0A"

    def test_a_manifest_path_reads_from_the_manifest(self) -> None:
        """The manifest is the only statement of what actually arrived."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="manifest.retrieved_at",
                    target_column="retrieved_at",
                    target_type="timestamp",
                ),
            )
        )
        rows = _mapped(
            contract,
            SourceDocument(rows=({},), manifest={"retrieved_at": "2026-08-19T06:00:00Z"}),
        )

        assert rows[0]["retrieved_at"] == "2026-08-19T06:00:00Z"

    def test_a_derived_column_is_read_under_its_canonical_name(self) -> None:
        """`COUNT(DISTINCT user_id)` is not a column name, so the extraction aliases it."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="COUNT(DISTINCT user_id)",
                    target_column="reach_users",
                    derived_from=("user_id",),
                    target_type="bigint",
                ),
            )
        )
        rows = _mapped(contract, SourceDocument(rows=({"reach_users": "57"},)))

        assert rows[0]["reach_users"] == "57"

    def test_an_undeclared_source_field_is_ignored(self) -> None:
        """Both sources add fields without notice, so rejecting one would break a load."""
        contract = _contract()
        rows = _mapped(
            contract, SourceDocument(rows=({"date_key": "2026-08-13", "brand_new": "x"},))
        )

        assert "brand_new" not in rows[0]
        assert rows[0]["date_key"] == "2026-08-13"

    def test_a_declared_field_the_document_omits_becomes_none(self) -> None:
        """Whether that is legitimate is already answered by the contract's nullability."""
        contract = _contract()
        rows = _mapped(contract, SourceDocument(rows=({},)))

        assert rows[0]["date_key"] is None


class TestMappingANestedDocument:
    def _vendor(self, *fields: FieldMapping) -> DataContract:
        return _contract(
            source_format=SourceFormat.JSON_DUAL_ARRAY,
            target_table="fct_retail_sales",
            measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
            source_timezone="America/Los_Angeles",
            fields=fields,
        )

    def test_an_array_path_produces_one_row_per_element(self) -> None:
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            )
        )
        document = {"salesByAsin": [{"asin": "B0A"}, {"asin": "B0B"}]}
        rows = _mapped(contract, SourceDocument(document=document))

        assert [row["amazon_asin"] for row in rows] == ["B0A", "B0B"]

    def test_a_document_level_path_is_stamped_onto_every_row(self) -> None:
        """Required by the vendor steering.

        Without it a MANUFACTURING pull and a SOURCING pull are indistinguishable in the
        curated table and summing them double-counts.
        """
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            ),
            FieldMapping(
                source_path="reportSpecification.reportOptions.distributorView",
                target_column="distributor_view",
                target_type="string",
            ),
        )
        document = {
            "salesByAsin": [{"asin": "B0A"}, {"asin": "B0B"}],
            "reportSpecification": {"reportOptions": {"distributorView": "MANUFACTURING"}},
        }
        rows = _mapped(contract, SourceDocument(document=document))

        assert [row["distributor_view"] for row in rows] == ["MANUFACTURING", "MANUFACTURING"]

    def test_an_indexed_path_reads_that_element(self) -> None:
        """`marketplaceIds[0]`: the list exists but one report is one marketplace."""
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            ),
            FieldMapping(
                source_path="reportSpecification.marketplaceIds[0]",
                target_column="marketplace_key",
                target_type="string",
            ),
        )
        document = {
            "salesByAsin": [{"asin": "B0A"}],
            "reportSpecification": {"marketplaceIds": ["ATVPDKIKX0DER"]},
        }
        rows = _mapped(contract, SourceDocument(document=document))

        assert rows[0]["marketplace_key"] == "ATVPDKIKX0DER"

    def test_a_path_naming_a_whole_object_becomes_its_hash(self) -> None:
        """`reportSpecification.reportOptions` is dataset identity, not a scalar.

        Reading it as a value would store a dict's repr; hashing it is what makes two pulls
        with different options land in different partitions.
        """
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            ),
            FieldMapping(
                source_path="reportSpecification.reportOptions.reportPeriod",
                target_column="report_period",
                target_type="string",
            ),
            FieldMapping(
                source_path="reportSpecification.reportOptions",
                target_column="report_options_hash",
                target_type="string",
            ),
        )
        document = {
            "salesByAsin": [{"asin": "B0A"}],
            "reportSpecification": {"reportOptions": {"reportPeriod": "DAY"}},
        }
        rows = _mapped(contract, SourceDocument(document=document))

        assert rows[0]["report_period"] == "DAY"
        assert rows[0]["report_options_hash"] == options_hash({"reportPeriod": "DAY"})

    def test_a_missing_array_is_not_an_empty_one(self) -> None:
        """A report whose array is absent is a different event from a quiet day."""
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            )
        )
        with pytest.raises(MappingError, match="no `salesByAsin` array"):
            _mapped(contract, SourceDocument(document={"reportSpecification": {}}))

    def test_two_repeating_arrays_mean_the_contract_is_wrong(self) -> None:
        """One contract is one dataset at one grain, so two grains is two contracts."""
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            ),
            FieldMapping(
                source_path="trafficByAsin[].glanceViews",
                target_column="glance_views",
                target_type="bigint",
            ),
        )
        with pytest.raises(MappingError, match="exactly one repeating array"):
            _mapped(contract, SourceDocument(document={"salesByAsin": []}))

    def test_a_nested_contract_given_flat_rows_says_the_formats_disagree(self) -> None:
        contract = self._vendor(
            FieldMapping(
                source_path="salesByAsin[].asin", target_column="amazon_asin", target_type="string"
            )
        )
        with pytest.raises(MappingError, match="source_format and the delivered file disagree"):
            _mapped(contract, SourceDocument(rows=({"asin": "B0A"},)))


class TestTheLoadAssignedProvenance:
    """The fifth provenance, added because a contract could not otherwise be read.

    `valid_from` and `valid_to` were declared as literals whose value was prose describing who
    would fill them in, so the mapper stamped that prose into a timestamp column.
    """

    def test_it_may_not_declare_a_source_path(self) -> None:
        with pytest.raises(ContractError, match="must not declare a source_path"):
            FieldMapping(
                source_path="valid_from",
                target_column="valid_from",
                assigned_by_load=True,
                target_type="timestamp",
            )

    def test_it_requires_a_target_type_because_no_source_states_one(self) -> None:
        with pytest.raises(ContractError, match="target_type is required"):
            FieldMapping(target_column="valid_from", assigned_by_load=True)

    def test_it_cannot_also_be_a_literal(self) -> None:
        with pytest.raises(ContractError, match="more than one provenance"):
            FieldMapping(
                target_column="valid_from",
                assigned_by_load=True,
                literal_value="now",
                target_type="timestamp",
            )

    def test_it_is_never_looked_up_in_the_amc_registry(self) -> None:
        """There is no source column to look up, so a registry check would reject it."""
        mapping = FieldMapping(
            target_column="valid_from", assigned_by_load=True, target_type="timestamp"
        )
        assert mapping.resolves_against_registry is False
        assert mapping.has_no_delivered_value is True


class TestTheGeneratedFixturesRoundTrip:
    """Read, map, validate — against every committed fixture and its own row model.

    The payoff for the fixtures being source-shaped and generated from the same contracts. It
    exercises the reader, the mapper, the divisor, the option hash and the row model in one
    pass, and it is the closest thing to a load that exists until task 7.

    What it cannot establish is anything about Amazon's behaviour: the fixtures are generated
    from our own declarations, so this proves our code does what we intended and nothing more.
    """

    def _payload(self, contract: DataContract) -> DeliveredFile:
        stem = contract.contract_id
        return DeliveredFile(
            text=(FIXTURES / f"{stem}.{EXTENSIONS[contract.source_format]}").read_text(),
            source_format=contract.source_format,
            manifest_text=(FIXTURES / f"{stem}.manifest.json").read_text(),
        )

    @pytest.mark.parametrize(
        "contract",
        [contract for _, contract in load_contracts(REPO_ROOT)],
        ids=lambda contract: contract.contract_id,
    )
    def test_a_fixture_maps_to_rows_its_own_model_accepts(self, contract: DataContract) -> None:
        source = read(self._payload(contract))
        rows = _mapped(contract, source)
        if contract.records_cell_state:
            # A shopper aggregate's suppressed rows never reach its fact: the load drops them
            # first (discard_suppressed_rows, ADR-099 point 7), so only the kept rows are
            # validated as written.
            measures = frozenset(f.target_column for f in contract.fields if f.non_additive)
            split = split_discarded(contract, source.rows, measures)
            assert split.discarded > 0, contract.contract_id
            # Packs 6 and 7's declared refused month holds a NULL bucket: the load refuses that
            # delivery (FREQUENCY_BUCKET_NULL), so none of its rows is written.
            windows = [
                (raw.get("time_window_start"), raw.get("time_window_end")) for raw in source.rows
            ]
            refused = {
                windows[index]
                for index, raw in enumerate(source.rows)
                if "frequency_bucket" in raw
                and not raw["frequency_bucket"]
                and raw.get("rows_suppressed") == "false"
            }
            rows = tuple(
                rows[index]
                for index in split.attributable_ordinals
                if windows[index] not in refused
            )
        elif contract.discards_into_history:
            # Packs 9 and 13's withheld name rows are dropped before the SCD2 fold (design
            # §5.4), so only the kept rows are validated as written.
            split = split_discarded(contract, source.rows, frozenset())
            assert split.discarded > 0 or contract.contract_id == "amc-segment-metadata"
            rows = tuple(rows[index] for index in split.attributable_ordinals)
        elif contract.contract_id == "amc-dsp-reach":
            # Pack 4's fixture holds its declared refused day: reject_delivery refuses that
            # delivery whole, so its suppressed row never reaches the fact (test_dsp_reach.py).
            assert sum(row["rows_suppressed"] is True for row in rows) == 1
            rows = tuple(row for row in rows if row["rows_suppressed"] is False)
        model = getattr(generated_rows, class_name_for(contract.contract_id))

        assert rows, contract.contract_id
        for row in rows:
            # The merge supplies what the mapper deliberately does not, so a dimension row is
            # validated as it will actually be written rather than as the mapper leaves it.
            assigned = {
                mapping.target_column: LOAD_ASSIGNED[mapping.target_column]
                for mapping in contract.fields
                if mapping.assigned_by_load
            }
            model.model_validate({**row, **assigned} if assigned else row)

    def test_the_divisor_reaches_the_curated_row(self) -> None:
        """Read straight off the fixture, so the assertion is on the whole path.

        The fixture holds millicents because a fixture in currency space would let a missing
        divisor pass unnoticed.
        """
        contract = _by_id("amc-dsp-impressions")
        raw = (FIXTURES / "amc-dsp-impressions.csv").read_text().splitlines()
        delivered = dict(zip(raw[0].split(","), raw[1].split(","), strict=True))
        rows = _mapped(contract, read(self._payload(contract)))

        spend = rows[0]["spend"]
        assert isinstance(spend, Decimal)
        assert spend == Decimal(delivered["total_cost"]) / Decimal(100_000)

    def test_the_option_hash_is_identical_across_a_document(self) -> None:
        """It describes the report, so a row-varying value would mean it read the wrong path."""
        contract = _by_id("vendor-sales-by-asin")
        rows = _mapped(contract, read(self._payload(contract)))

        assert len({row["report_options_hash"] for row in rows}) == 1

    def test_a_manifest_value_reaches_every_row(self) -> None:
        contract = _by_id("vendor-sales-by-asin")
        rows = _mapped(contract, read(self._payload(contract)))

        # The declaration's retrieved_at is a pure function of the declared span (design §2.2).
        last_day = load_synthetic_entity_catalog(REPO_ROOT).shape.fixture_span.last_day
        assert all(row["retrieved_at"] == f"{last_day}T00:00:00Z" for row in rows)


def _by_id(contract_id: str) -> DataContract:
    for _, contract in load_contracts(REPO_ROOT):
        if contract.contract_id == contract_id:
            return contract
    raise AssertionError(f"no contract {contract_id}")


def test_the_manifest_fixtures_are_valid_json() -> None:
    """A manifest that cannot be parsed would make every round-trip above fail obscurely."""
    for path in sorted(FIXTURES.glob("*.manifest.json")):
        assert isinstance(json.loads(path.read_text()), dict), path.name


class TestConformedProvenanceIsStamped:
    """The mapper's half of `basis_key` and `source_timezone`.

    The generator declares the columns; this stamps the values. Both halves are required and
    the split is where the original defect lived — `measurement_basis`'s own docstring said
    "stamped as a literal during mapping" while nothing stamped it.
    """

    def test_a_mapped_fact_row_carries_both(self) -> None:
        rows = _mapped(_contract(), SourceDocument(rows=({"date_key": "2026-08-13"},)))

        assert rows[0][BASIS_KEY] == "ad_attributed"
        assert rows[0][SOURCE_TIMEZONE] == "UTC"

    def test_the_values_belong_to_the_contract_and_are_not_a_default(self) -> None:
        """A default would make every fact look like whichever source was written first."""
        vendor = _contract(
            target_table="fct_retail_sales",
            measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
            source_timezone="PST",
        )
        rows = _mapped(vendor, SourceDocument(rows=({"date_key": "2026-08-13"},)))

        assert rows[0][BASIS_KEY] == "retail_actual"
        assert rows[0][SOURCE_TIMEZONE] == "PST"

    def test_a_dimension_row_carries_neither(self) -> None:
        rows = _mapped(
            _contract(target_table="dim_product"),
            SourceDocument(rows=({"date_key": "2026-08-13"},)),
        )

        assert BASIS_KEY not in rows[0]
        assert SOURCE_TIMEZONE not in rows[0]

    def test_no_source_field_can_override_them(self) -> None:
        """Stamped last, so a delivered column of the same name cannot win.

        A source that happened to emit `basis_key` would otherwise decide our measurement
        semantics, which is the one thing no source knows about itself.
        """
        rows = _mapped(
            _contract(),
            SourceDocument(rows=({"date_key": "2026-08-13", BASIS_KEY: "whatever_it_says"},)),
        )

        assert rows[0][BASIS_KEY] == "ad_attributed"

    def test_every_row_of_a_multi_row_document_is_stamped(self) -> None:
        """Constant per contract, but it is a column, so every row must carry it."""
        rows = _mapped(
            _contract(),
            SourceDocument(rows=({"date_key": "2026-08-13"}, {"date_key": "2026-08-14"})),
        )

        assert len(rows) == 2
        assert all(row[SOURCE_TIMEZONE] == "UTC" for row in rows)
