# SPDX-License-Identifier: MIT-0
"""Curated schema resolution and the merge of contracts writing one table.

Two rules here are correctness properties rather than conveniences, and both are the kind
that fail silently if wrong.

A monetary column's target type is never its source type, because the divisor changes it —
emitting `impression_cost` as the `LONG` AMC publishes would truncate every fee to zero
while the totals still looked plausible.

And a type conflict between two contracts writing one column has no safe resolution, so it
must stop the generator rather than pick one.
"""

import datetime
import json
import pathlib
from dataclasses import replace
from decimal import Decimal
from typing import ClassVar, NotRequired, TypedDict, cast

import pytest
from pydantic import ValidationError

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.schema_generation.dq import render_ruleset
from adapters.jobs.schema_generation.merge import (
    AbsentFill,
    MergedColumn,
    MergedTable,
    MergeError,
    merge_tables,
    withheld_tables,
)
from adapters.jobs.schema_generation.models import LINE_LIMIT, class_name_for, render_models
from adapters.jobs.schema_generation.physical_schema import (
    PhysicalColumn,
    PhysicalSchemaCompatibility,
    PhysicalSchemaError,
    compatible_column_order,
    load_physical_schema_compatibility,
    require_append_only_physical_columns,
)
from adapters.jobs.schema_generation.publisher_graph import load_publication_graph
from adapters.jobs.schema_generation.v2_storage import V2StorageRequest, v2_storage_json
from adapters.outbound.source_pack import load_source_packs
from domain.data_contracts import generated_rows
from domain.data_contracts.contract import (
    DataContract,
    FieldMapping,
    NullMeaning,
    SourceFormat,
)
from domain.data_contracts.schema import (
    BASIS_KEY,
    CONFORMED_PROVENANCE_COLUMNS,
    ECOSYSTEM_KEY,
    PLATFORM_KEY,
    SOURCE_TIMEZONE,
    CanonicalType,
    ColumnRegistry,
    RegisteredColumn,
    SchemaError,
    conformed_provenance,
    conformed_provenance_values,
    resolve_target_type,
    target_schema,
)
from domain.semantics.publication import CanonicalDatasetKey
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit]


class _StorageColumn(TypedDict):
    fieldId: int
    name: str
    type: str
    nullable: bool
    comment: str
    nullMeaning: NotRequired[str]


class _StorageTable(TypedDict):
    name: str
    writtenBy: list[str]
    canonicalPublisher: str
    readiness: str
    grain: list[str]
    columns: list[_StorageColumn]


def _storage_tables(value: object) -> list[_StorageTable]:
    assert isinstance(value, list)
    return cast("list[_StorageTable]", value)


def _generated_tables(path: pathlib.Path) -> list[_StorageTable]:
    payload = cast("dict[str, object]", json.loads(path.read_text()))
    return _storage_tables(payload["tables"])


def _registry(*columns: RegisteredColumn) -> ColumnRegistry:
    return ColumnRegistry(
        columns={(c.table, c.name): c for c in columns},
        known_tables=frozenset(c.table for c in columns),
        inherits={},
    )


def _contract(**overrides: object) -> DataContract:
    defaults: dict[str, object] = {
        "contract_id": "test-contract",
        "contract_version": 1,
        "source_system": "amc",
        "source_dataset": "dsp_impressions",
        "source_format": SourceFormat.CSV,
        "target_table": "fct_ad_exposure",
        "grain": ("product_key",),
        "partition_keys": ("product_key",),
        "measurement_basis": MeasurementBasis.AD_ATTRIBUTED,
        "source_timezone": "UTC",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
        "restatement_window_days": 30,
        "staleness_threshold_hours": 72,
        "source_tables": ("dsp_impressions",),
        "grain_feasibility_note": "every output column is NONE or LOW.",
        "fields": (
            FieldMapping(
                source_path="impressions", target_column="product_key", target_type="bigint"
            ),
        ),
    }
    return DataContract(**(defaults | overrides))  # type: ignore[arg-type]


class TestTypeResolution:
    def test_a_monetary_column_is_decimal_whatever_its_source_type(self) -> None:
        """The divisor makes it a decimal, so the source type is the wrong answer.

        `impression_cost` is a LONG and is divided by 100,000. Stored as a long, every fee
        truncates to zero and nothing errors.
        """
        mapping = FieldMapping(
            source_path="impression_cost",
            target_column="spend_media_only",
            scale_divisor=100_000,
            currency_column="currency_code",
        )
        registered = RegisteredColumn(
            name="impression_cost",
            table="dsp_impressions",
            threshold="NONE",
            source_type="LONG",
            monetary=True,
            scale_divisor=100_000,
        )
        assert resolve_target_type(mapping, registered) is CanonicalType.DECIMAL

    def test_a_declared_type_wins_over_the_registry(self) -> None:
        mapping = FieldMapping(
            source_path="impressions", target_column="impressions", target_type="string"
        )
        registered = RegisteredColumn(
            name="impressions", table="dsp_impressions", threshold="NONE", source_type="LONG"
        )
        assert resolve_target_type(mapping, registered) is CanonicalType.STRING

    @pytest.mark.parametrize(
        ("published", "expected"),
        [
            ("LONG", CanonicalType.BIGINT),
            ("STRING", CanonicalType.STRING),
            ("DATE", CanonicalType.DATE),
            ("TIMESTAMP", CanonicalType.TIMESTAMP),
            ("BOOLEAN", CanonicalType.BOOLEAN),
            ("INTEGER", CanonicalType.INT),
            ("DOUBLE", CanonicalType.DOUBLE),
            ("DECIMAL(38,4)", CanonicalType.DECIMAL),
        ],
    )
    def test_amc_published_types_map_to_canonical_ones(
        self, published: str, expected: CanonicalType
    ) -> None:
        """Read from the registry rather than re-declared, so the two cannot drift."""
        mapping = FieldMapping(source_path="c", target_column="c")
        registered = RegisteredColumn(name="c", table="t", threshold="LOW", source_type=published)
        assert resolve_target_type(mapping, registered) is expected

    def test_an_unmapped_source_type_resolves_to_nothing_rather_than_a_default(self) -> None:
        """Defaulting to STRING would store an array as text and lose it silently."""
        mapping = FieldMapping(source_path="segments", target_column="segments")
        registered = RegisteredColumn(
            name="segments", table="t", threshold="LOW", source_type="Array[LONG]"
        )
        assert resolve_target_type(mapping, registered) is None

    def test_a_field_with_no_source_and_no_declaration_resolves_to_nothing(self) -> None:
        mapping = FieldMapping(source_path="salesByAsin[].asin", target_column="product_key")
        assert resolve_target_type(mapping, None) is None

    def test_target_schema_names_every_unresolvable_column_at_once(self) -> None:
        """Not the first one. Naming all of them is one correction instead of several."""
        contract = _contract(
            source_tables=(),
            source_system="vendor_central",
            fields=(
                FieldMapping(source_path="a", target_column="product_key"),
                FieldMapping(source_path="b", target_column="other_key"),
            ),
        )
        with pytest.raises(SchemaError) as raised:
            target_schema(contract, _registry())
        assert "product_key" in str(raised.value)
        assert "other_key" in str(raised.value)


class TestMerge:
    """A table is the union of every contract writing it, not one contract's output."""

    def _exposure(self, contract_id: str, fields: tuple[FieldMapping, ...]) -> DataContract:
        return _contract(contract_id=contract_id, fields=fields)

    def _reg(self) -> ColumnRegistry:
        return _registry(
            RegisteredColumn(
                name="impressions", table="dsp_impressions", threshold="NONE", source_type="LONG"
            ),
            RegisteredColumn(
                name="clicks", table="dsp_impressions", threshold="NONE", source_type="LONG"
            ),
        )

    def test_a_column_only_some_contracts_write_becomes_nullable(self) -> None:
        """Each contract writes a subset, so requiring either rejects a valid write."""
        a = self._exposure(
            "a",
            (
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="bigint"
                ),
                FieldMapping(
                    source_path="impressions", target_column="impressions", target_type="bigint"
                ),
            ),
        )
        b = self._exposure(
            "b",
            (
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="bigint"
                ),
                FieldMapping(source_path="clicks", target_column="clicks", target_type="bigint"),
            ),
        )
        merged = merge_tables([a, b], self._reg())
        assert len(merged) == 1
        columns = {c.name: c for c in merged[0].columns}

        assert columns["product_key"].nullable is False, "written by both, so required"
        assert columns["impressions"].nullable is True, "written by one, so nullable"
        assert columns["clicks"].nullable is True
        assert columns["product_key"].written_by == ("a", "b")

    @pytest.mark.covers("ADR-109")
    def test_reach_is_null_filled_and_impressions_still_zero_filled(self) -> None:
        """Every writer of `reach_users` declares it non-additive, so a missing writer is NULL.

        The rows of the three DSP delivery contracts carry no reach, and reading them as 0
        would claim nobody was reached. `impressions` is additive, so its missing writers keep
        zero-fill: they contribute nothing to the sum. A column every writer supplies is filled
        by nobody.
        """
        root = pathlib.Path(__file__).resolve().parents[2]
        contracts = [contract for _, contract in load_contracts(root)]
        (exposure,) = (
            table
            for table in merge_tables(contracts, load_registry(root))
            if table.name == "fct_ad_exposure"
        )
        columns = {column.name: column for column in exposure.columns}
        assert exposure.written_by == (
            "amc-dsp-clicks",
            "amc-dsp-impressions",
            "amc-dsp-reach",
            "amc-dsp-views",
            "amc-reach-frequency",
        )
        assert columns["reach_users"].written_by == ("amc-dsp-reach", "amc-reach-frequency")
        assert columns["reach_users"].absent_fill is AbsentFill.NULL
        # Paired control: the additive measure the same three DSP rows lack stays zero-filled.
        assert columns["impressions"].absent_fill is AbsentFill.ZERO
        assert columns["clicks"].absent_fill is AbsentFill.ZERO
        assert columns["campaign_key"].absent_fill is None
        # Only the exception is rendered into the generated schema.
        generated = _generated_tables(root / "contracts/generated/tables.json")
        (table,) = (item for item in generated if item["name"] == "fct_ad_exposure")
        rendered = {
            column["name"]: cast("dict[str, object]", column).get("absentFill")
            for column in table["columns"]
        }
        assert {name: fill for name, fill in rendered.items() if fill is not None} == {
            "reach_users": "null"
        }

    @pytest.mark.covers("ADR-109")
    def test_one_additive_writer_keeps_a_column_zero_filled(self) -> None:
        """Null-fill needs every writer non-additive; one additive writer keeps zero-fill."""
        distinct = FieldMapping(
            source_path="impressions",
            target_column="impressions",
            target_type="bigint",
            non_additive=True,
        )
        summed = FieldMapping(
            source_path="impressions", target_column="impressions", target_type="bigint"
        )
        key = FieldMapping(
            source_path="impressions", target_column="product_key", target_type="bigint"
        )
        clicks = FieldMapping(source_path="clicks", target_column="clicks", target_type="bigint")

        def fill(first: FieldMapping) -> AbsentFill | None:
            merged = merge_tables(
                [
                    self._exposure("a", (key, first)),
                    self._exposure("b", (key, summed)),
                    self._exposure("c", (key, clicks)),
                ],
                self._reg(),
            )
            return {column.name: column for column in merged[0].columns}["impressions"].absent_fill

        assert fill(distinct) is AbsentFill.ZERO
        # Control: both writers non-additive, so the third writer's rows are NULL-filled.
        both = merge_tables(
            [
                self._exposure("a", (key, distinct)),
                self._exposure("b", (key, distinct)),
                self._exposure("c", (key, clicks)),
            ],
            self._reg(),
        )
        assert {column.name: column for column in both[0].columns}[
            "impressions"
        ].absent_fill is AbsentFill.NULL

    def test_two_contracts_disagreeing_about_a_type_is_fatal(self) -> None:
        """There is no safe default. Choosing either would make the other load coerce."""
        a = self._exposure(
            "a",
            (
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="bigint"
                ),
            ),
        )
        b = self._exposure(
            "b",
            (
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="string"
                ),
            ),
        )
        with pytest.raises(MergeError, match="no safe default"):
            merge_tables([a, b], self._reg())

    def test_a_dimension_is_marked_for_scd2_rather_than_overwrite(self) -> None:
        """A moved ASIN is a new version, so overwriting would rewrite history."""
        contract = _contract(
            target_table="dim_product",
            fields=(
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="string"
                ),
            ),
        )
        merged = merge_tables([contract], self._reg())
        assert merged[0].is_dimension is True

    def test_partition_keys_and_grain_union_across_contracts(self) -> None:
        a = self._exposure(
            "a",
            (
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="bigint"
                ),
            ),
        )
        b = _contract(
            contract_id="b",
            grain=("product_key", "date_key"),
            partition_keys=("date_key",),
            fields=(
                FieldMapping(
                    source_path="impressions", target_column="product_key", target_type="bigint"
                ),
                FieldMapping(source_path="clicks", target_column="date_key", target_type="date"),
            ),
        )
        merged = merge_tables([a, b], self._reg())
        assert merged[0].grain == ("date_key", "product_key")
        assert merged[0].partition_keys == ("date_key", "product_key")


class TestModelGeneration:
    """The Pydantic projection: the row shape a reader must produce.

    Coercion is the load-bearing behaviour, not validation. A CSV reader hands back strings
    for every column, and the model is what turns them into typed values — via `Decimal`
    directly from the string, so no cent is lost to a float intermediary.
    """

    def test_generated_module_starts_with_exact_spdx_marker(self) -> None:
        rendered = render_models([_contract()], _registry())

        assert rendered.startswith('# SPDX-License-Identifier: MIT-0\n"""GENERATED FROM contracts/')
        assert rendered.count("SPDX-License-Identifier: MIT-0") == 1

    def test_a_class_name_comes_from_the_contract_not_the_table(self) -> None:
        """A table written by four contracts would otherwise give four identical names."""
        assert class_name_for("vendor-sales-by-asin") == "VendorSalesByAsinRow"
        assert class_name_for("amc-dsp-impressions") == "AmcDspImpressionsRow"

    def test_a_nullable_column_is_optional_and_defaults_to_none(self) -> None:
        """Required-but-nullable is not what the contract says.

        A column is nullable here because only some contracts writing the table write it, so
        it must be permitted to be absent rather than merely permitted to be null.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="filtered_reason",
                    target_column="suppression_reason",
                    from_extraction_option=True,
                    target_type="string",
                    nullable=True,
                    null_meaning=NullMeaning.SUPPRESSED,
                ),
            )
        )
        rendered = render_models([contract], _registry())
        # Asserted in two parts because a long description wraps the Field call across lines.
        assert "suppression_reason: str | None = Field(" in rendered
        assert "default=None" in rendered

    def test_a_monetary_column_carries_the_stored_precision(self) -> None:
        """Four places, asserted at the boundary rather than rounded into.

        A value arriving with more precision means a divisor was applied wrongly, and
        rounding it silently is how an order-of-magnitude error survives.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
            )
        )
        rendered = render_models([contract], _registry())
        assert "spend: Decimal = Field(" in rendered
        assert "max_digits=38" in rendered
        assert "decimal_places=4" in rendered

    def test_provenance_reaches_the_field_description(self) -> None:
        """So a validation error is traceable without opening the contract."""
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
        assert "derived from user_id" in render_models([contract], _registry())

    def test_only_the_imports_actually_used_are_emitted(self) -> None:
        """An unused import fails ruff, and generated code is linted like any other."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="impressions", target_column="impressions", target_type="bigint"
                ),
            )
        )
        rendered = render_models([contract], _registry())
        assert "from decimal import Decimal" not in rendered
        assert "from datetime import" not in rendered

    def test_no_generated_line_exceeds_the_lint_limit(self) -> None:
        """A long provenance string is wrapped rather than exempted from the line limit."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="a_source_path_long_enough_to_force_the_description_to_wrap",
                    target_column="wordy_column",
                    target_type="string",
                    nullable=True,
                    null_meaning=NullMeaning.NOT_APPLICABLE_UNDER_OPTIONS,
                ),
            )
        )
        for line in render_models([contract], _registry()).splitlines():
            assert len(line) <= LINE_LIMIT, line

    def test_a_wrapped_description_does_not_lose_the_spaces_it_wrapped_on(self) -> None:
        """`textwrap` breaks on whitespace and discards it.

        Without a trailing space per chunk the literals concatenate into one run-on word and
        the description silently changes meaning.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="some_column",
                    target_column="wordy_column",
                    derived_from=("alpha_column", "beta_column", "gamma_column", "delta_column"),
                    target_type="string",
                    nullable=True,
                    null_meaning=NullMeaning.NOT_YET_AVAILABLE,
                ),
            )
        )
        rendered = render_models([contract], _registry())
        # Reconstruct what Python would build from the concatenated literals.
        joined = "".join(
            line.strip().strip('"')
            for line in rendered.splitlines()
            if line.startswith('            "')
        )
        assert "derived from" in joined
        assert "columnbeta" not in joined, "a space was lost at a wrap point"


class TestTheCommittedModels:
    """The generated module as it stands in the repository.

    Every row is built with `model_validate` and a dict, because that is how a reader calls
    these — it maps a source document into a dict of canonical columns and hands the dict
    over. Keyword construction would also be *statically* wrong: the annotations say `int`
    and `Decimal`, so pyright rejects the strings a delivered file actually contains, and
    coercion is only reachable through `model_validate`.
    """

    IMPRESSIONS: ClassVar[dict[str, object]] = {
        "date_key": "2026-08-13",
        "channel_key": "amazon_dsp",
        "channel_group_key": "display",
        "campaign_key": "camp-1",
        "ad_product_key": "amazon_dsp",
        "marketplace_key": "ATVPDKIKX0DER",
        "impressions": "137",
        # 780000 millicents, divided by 1e5 by the reader.
        "spend": "7.8000",
        "currency_code": "USD",
        "rows_suppressed": "true",
        # No `suppression_reason` or `distinct_user_count`. Both arrive in the delivery and neither
        # is conformed onto a fact four contracts write — G-28. The model forbids extra inputs, so
        # their presence here would now be a failure rather than a harmless surplus.
        # Projected onto every fact from contract-level metadata, so a reader's dict carries
        # them even though no source field supplies either.
        "basis_key": "ad_attributed",
        "source_timezone": "UTC",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_ads",
    }

    HIERARCHY: ClassVar[dict[str, object]] = {
        "amazon_asin": "B0TEST0001",
        "marketplace_key": "ATVPDKIKX0DER",
        "amazon_parent_asin": None,
        "brand": None,
        "brand_code": None,
        "product_title": None,
        "valid_from": "2026-08-13T00:00:00Z",
        "valid_to": "2999-12-31T00:00:00Z",
        "is_current": "true",
    }

    def test_every_contract_has_a_model(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        contracts = [contract for _, contract in load_contracts(root)]
        for contract in contracts:
            assert hasattr(generated_rows, class_name_for(contract.contract_id))
        assert len(generated_rows.__all__) == len(contracts)

    def test_a_reader_may_hand_over_strings(self) -> None:
        """The reason this is Pydantic and not a dataclass.

        A delivered CSV is strings all the way down. The model is what types them.
        """
        row = generated_rows.AmcDspImpressionsRow.model_validate(self.IMPRESSIONS)

        assert row.impressions == 137
        assert isinstance(row.impressions, int)
        assert row.date_key == datetime.date(2026, 8, 13)
        assert row.rows_suppressed is True

    def test_money_arrives_exact_rather_than_through_a_float(self) -> None:
        """A source-scale integer becomes exact local currency at the declared grain."""
        row = generated_rows.AmcDspImpressionsRow.model_validate(self.IMPRESSIONS)

        assert row.spend == Decimal("7.8000")
        assert row.currency_code == "USD"
        assert row.date_key == datetime.date(2026, 8, 13)

    def test_more_precision_than_the_column_holds_is_rejected_not_rounded(self) -> None:
        """Silently rounding is how a wrong divisor survives to the curated table."""
        with pytest.raises(ValidationError, match="no more than 4 decimal places"):
            generated_rows.AmcDspImpressionsRow.model_validate(
                {**self.IMPRESSIONS, "spend": "7.800001"}
            )

    def test_a_column_the_contract_does_not_declare_is_rejected(self) -> None:
        """On a curated row an unexpected column is a reader bug.

        Additive tolerance governs parsing the *source* document, which happens earlier.
        """
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            generated_rows.VendorProductHierarchyRow.model_validate(
                {**self.HIERARCHY, "invented_column": "oops"}
            )

    def test_a_null_in_a_required_column_names_the_column(self) -> None:
        with pytest.raises(ValidationError, match="amazon_asin"):
            generated_rows.VendorProductHierarchyRow.model_validate(
                {**self.HIERARCHY, "amazon_asin": None}
            )

    def test_a_nullable_column_may_be_absent_entirely(self) -> None:
        """Nullable means "some contracts do not write it", so absent must be legal.

        A required-but-nullable field would force a reader to supply an explicit None for a
        column its own contract never mentions.
        """
        without = {k: v for k, v in self.HIERARCHY.items() if k != "brand"}
        assert generated_rows.VendorProductHierarchyRow.model_validate(without).brand is None

    def test_a_row_cannot_be_mutated_after_validation(self) -> None:
        """Frozen, so a validated row is evidence rather than a working buffer."""
        row = generated_rows.VendorProductHierarchyRow.model_validate(self.HIERARCHY)

        with pytest.raises(ValidationError):
            row.amazon_asin = "B0TEST0002"  # type: ignore[misc]


class TestDqdlGeneration:
    """The DQDL projection: the rules a curated table must satisfy.

    Two properties carry the weight. Uniqueness over the declared grain is the only thing that
    checks a *stated* grain was true. And a `ColumnValues` rule on a nullable column has to
    allow NULL explicitly, because DQDL fails NULL rows on every such rule — which on this
    platform would quarantine a partition for containing a legitimate privacy suppression.
    """

    def _table(self, *contracts: DataContract) -> MergedTable:
        return merge_tables(list(contracts), _registry())[0]

    def test_the_declared_grain_becomes_a_multi_column_uniqueness_rule(self) -> None:
        """A grain is stated, never inferred, so this is what checks the statement."""
        contract = _contract(
            grain=("date_key", "amazon_asin"),
            fields=(
                FieldMapping(source_path="a", target_column="date_key", target_type="date"),
                FieldMapping(source_path="b", target_column="amazon_asin", target_type="string"),
            ),
        )
        ruleset = render_ruleset(self._table(contract), [contract])

        assert 'Uniqueness "amazon_asin" "date_key" = 1.0' in ruleset

    def test_a_non_nullable_column_must_be_complete(self) -> None:
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="impressions", target_column="impressions", target_type="bigint"
                ),
            )
        )
        assert 'IsComplete "impressions"' in render_ruleset(self._table(contract), [contract])

    def test_a_nullable_column_is_not_required_to_be_complete(self) -> None:
        """Suppressed, not-applicable and not-yet-available are legitimate absences.

        Requiring completeness of one would quarantine a partition for being honest about a
        privacy withholding.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="filtered_reason",
                    target_column="suppression_reason",
                    from_extraction_option=True,
                    target_type="string",
                    nullable=True,
                    null_meaning=NullMeaning.SUPPRESSED,
                ),
            )
        )
        # Named rather than asserting `IsComplete` is absent altogether: every fact also
        # carries the non-nullable conformed provenance columns, which are legitimately
        # required to be complete.
        assert 'IsComplete "suppression_reason"' not in render_ruleset(
            self._table(contract), [contract]
        )

    def test_a_currency_rule_on_a_nullable_column_allows_null_explicitly(self) -> None:
        """DQDL fails NULL rows on every ColumnValues rule bar `!=` and `not in`.

        `fct_ad_exposure` needs this today: two of its four contracts carry no money, so its
        merged `currency_code` is nullable and every row from those two would fail.
        """
        money = _contract(
            contract_id="with-money",
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(
                    source_path="purchase_currency",
                    target_column="currency_code",
                    target_type="string",
                ),
            ),
        )
        countable = _contract(
            contract_id="without-money",
            fields=(
                FieldMapping(source_path="clicks", target_column="clicks", target_type="bigint"),
            ),
        )
        merged = merge_tables([money, countable], _registry())[0]
        ruleset = render_ruleset(merged, [money, countable])

        assert '(ColumnValues "currency_code" matches' in ruleset
        assert 'OR (ColumnValues "currency_code" = NULL)' in ruleset

    def test_a_currency_rule_on_a_required_column_is_not_wrapped(self) -> None:
        """Allowing NULL where the contract forbids it would weaken the rule for nothing."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(
                    source_path="purchase_currency",
                    target_column="currency_code",
                    target_type="string",
                ),
            )
        )
        ruleset = render_ruleset(self._table(contract), [contract])

        assert 'ColumnValues "currency_code" matches "^[A-Z]{3}$"' in ruleset
        assert "OR (ColumnValues" not in ruleset

    def test_the_currency_column_is_read_from_the_contract_not_matched_by_name(self) -> None:
        """A renamed currency column follows the contract rather than losing its rule."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="settlement_currency",
                ),
                FieldMapping(
                    source_path="purchase_currency",
                    target_column="settlement_currency",
                    target_type="string",
                ),
            )
        )
        assert 'ColumnValues "settlement_currency" matches' in render_ruleset(
            self._table(contract), [contract]
        )

    def test_a_key_valid_only_among_unsuppressed_rows_scopes_the_uniqueness_rule(self) -> None:
        """Otherwise the rule fails on rows that merged exactly as documented.

        No shipped contract sets this — every one puts the discriminator in its grain instead —
        but the flag is part of the contract model, and a ruleset ignoring it would quarantine
        correctly merged rows.
        """
        contract = _contract(
            key_unique_among_unsuppressed_only=True,
            grain=("date_key",),
            fields=(FieldMapping(source_path="a", target_column="date_key", target_type="date"),),
        )
        ruleset = render_ruleset(self._table(contract), [contract])

        assert 'where "rows_suppressed = false"' in ruleset

    def test_no_rule_asserts_a_count_is_non_negative(self) -> None:
        """The vendor documentation says cancellations are booked at cancellation time.

        So real-time ordered units legitimately go negative, no contract declares which
        columns may, and a blanket rule would assert what a source already contradicts.
        """
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="units_sold", target_column="units_ordered", target_type="bigint"
                ),
            )
        )
        ruleset = render_ruleset(self._table(contract), [contract])

        assert ">= 0" not in ruleset
        assert "> 0" not in ruleset

    def test_no_rule_counts_rows(self) -> None:
        """Arrival belongs to the freshness registry and completeness to the control total.

        A third home for the same question is how three checks come to disagree.
        """
        contract = _contract()
        assert "RowCount" not in render_ruleset(self._table(contract), [contract])

    def test_a_ruleset_is_a_well_formed_dqdl_document(self) -> None:
        """Bracket-delimited `Rules` list, comma separated, one rule per line."""
        contract = _contract(
            grain=("date_key",),
            fields=(FieldMapping(source_path="a", target_column="date_key", target_type="date"),),
        )
        lines = render_ruleset(self._table(contract), [contract]).splitlines()

        assert lines[0] == "Rules = ["
        assert lines[-1] == "]"
        for line in lines[1:-2]:
            assert line.endswith(","), line
        assert not lines[-2].endswith(",")


class TestTheCommittedRulesets:
    """The generated rulesets as they stand in the repository."""

    def _rulesets(self) -> dict[str, dict[str, object]]:
        root = pathlib.Path(__file__).resolve().parents[2]
        raw = json.loads((root / "contracts/generated/dq-rulesets.json").read_text())
        return {entry["table"]: entry for entry in raw["rulesets"]}

    def test_every_generated_table_has_a_ruleset(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        tables = json.loads((root / "contracts/generated/tables.json").read_text())["tables"]
        rulesets = self._rulesets()

        assert {table["name"] for table in tables} == set(rulesets)

    def test_every_ruleset_checks_its_grain(self) -> None:
        """A table whose grain is unchecked is one whose stated grain is only a claim."""
        for table, entry in self._rulesets().items():
            assert "Uniqueness" in str(entry["ruleset"]), table

    def test_the_nullable_currency_on_fct_ad_exposure_allows_null(self) -> None:
        """The live instance of the NULL trap, asserted against the committed artefact.

        Two of the four contracts writing this table carry no money, so its `currency_code` is
        nullable and an unwrapped rule would fail every row they produce.
        """
        ruleset = str(self._rulesets()["fct_ad_exposure"]["ruleset"])
        assert 'OR (ColumnValues "currency_code" = NULL)' in ruleset


class TestConformedProvenanceColumns:
    """`basis_key` and `source_timezone`, projected onto every fact from contract metadata.

    These exist because the canonical model declared `basis_key` on every fact, its ERD
    joined `dim_measurement_basis` on it, and no contract wrote it — the column was in the
    diagram and absent from the table. `source_timezone` was the same defect, and ADR-004
    puts a deadline on it: mandatory from the first load, because a later backfill needs a
    value that may no longer be knowable.
    """

    @staticmethod
    def _names(contract: DataContract) -> set[str]:
        return {column.name for column in target_schema(contract, _registry())}

    def test_a_fact_carries_all_contract_level_columns(self) -> None:
        assert CONFORMED_PROVENANCE_COLUMNS == (
            BASIS_KEY,
            SOURCE_TIMEZONE,
            ECOSYSTEM_KEY,
            PLATFORM_KEY,
        )
        assert set(CONFORMED_PROVENANCE_COLUMNS) <= self._names(_contract())

    def test_a_dimension_carries_neither(self) -> None:
        """A product has no measurement basis and no day bucket it was aggregated into.

        Excluded deliberately, not incidentally: `dim_product`'s contract declares a
        `measurement_basis` because every contract must, and projecting it would invite a
        join to `dim_measurement_basis` that means nothing.
        """
        dimension = _contract(target_table="dim_product")

        assert not set(CONFORMED_PROVENANCE_COLUMNS) & self._names(dimension)

    def test_both_are_non_nullable_strings(self) -> None:
        """A fact whose basis is unknown can be summed with a fact of another basis."""
        columns = {
            column.name: column
            for column in target_schema(_contract(), _registry())
            if column.name in CONFORMED_PROVENANCE_COLUMNS
        }

        for column in columns.values():
            assert column.type is CanonicalType.STRING
            assert column.nullable is False

    def test_the_values_come_from_the_contract_and_vary_independently(self) -> None:
        """The reason there are two columns rather than one.

        `amc-retail-purchases` is genuinely `retail_actual` on `UTC` while
        `vendor-sales-by-asin` is `retail_actual` on `PST`, so neither column is derivable
        from the other, from the source system, or from the table name.
        """
        amc = _contract(measurement_basis=MeasurementBasis.RETAIL_ACTUAL, source_timezone="UTC")
        vendor = _contract(measurement_basis=MeasurementBasis.RETAIL_ACTUAL, source_timezone="PST")

        assert conformed_provenance_values(amc) == {
            BASIS_KEY: "retail_actual",
            SOURCE_TIMEZONE: "UTC",
            ECOSYSTEM_KEY: "amazon",
            PLATFORM_KEY: "amazon_ads",
        }
        assert conformed_provenance_values(vendor)[SOURCE_TIMEZONE] == "PST"

    def test_a_dimension_stamps_no_values(self) -> None:
        assert conformed_provenance_values(_contract(target_table="dim_product")) == {}

    def test_a_contract_declaring_one_itself_is_refused(self) -> None:
        """Two columns of one name is not a table.

        Refused rather than silently overridden, because an author who mapped `basis_key`
        believed they were supplying it and needs telling that they are not.
        """
        colliding = _contract(
            fields=(
                FieldMapping(source_path="whatever", target_column=BASIS_KEY, target_type="string"),
            )
        )

        with pytest.raises(SchemaError, match=BASIS_KEY):
            target_schema(colliding, _registry())

    def test_every_real_fact_carries_both_and_no_real_dimension_does(self) -> None:
        """The gate this pair exists to satisfy, against the committed contracts.

        Parametrising over the real contracts rather than a synthetic one because the defect
        was that eleven real contracts each declared the values and none produced the column.
        """
        root = pathlib.Path(__file__).resolve().parent.parent.parent
        seen_a_fact = False
        seen_a_dimension = False

        for _, contract in load_contracts(root):
            names = {column.name for column in conformed_provenance(contract)}
            if contract.targets_a_fact:
                seen_a_fact = True
                assert names == set(CONFORMED_PROVENANCE_COLUMNS), contract.contract_id
                assert conformed_provenance_values(contract).keys() == names
            else:
                seen_a_dimension = True
                assert not names, contract.contract_id

        # Otherwise a loader returning nothing would make this pass vacuously.
        assert seen_a_fact
        assert seen_a_dimension


class TestCanonicalPublisherGraph:
    @pytest.mark.covers("ADR-044")
    def test_every_generated_dataset_has_exactly_one_canonical_publisher(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        generated = _generated_tables(root / "contracts/generated/tables.json")
        expected = frozenset(CanonicalDatasetKey(table["name"]) for table in generated)
        graph = load_publication_graph(root, expected)

        assert {binding.dataset for binding in graph.bindings} == expected
        assert {binding.publisher.value for binding in graph.bindings} == {"canonical-curation"}

    @pytest.mark.covers("ADR-044")
    def test_shared_fact_has_many_source_contributors_and_one_publisher(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        generated = _generated_tables(root / "contracts/generated/tables.json")
        exposure = next(table for table in generated if table["name"] == "fct_ad_exposure")

        assert len(exposure["writtenBy"]) == 5
        assert exposure["canonicalPublisher"] == "canonical-curation"


class TestPhysicalIcebergCompatibility:
    @staticmethod
    def _column(
        name: str,
        column_type: CanonicalType = CanonicalType.STRING,
        *,
        nullable: bool = False,
    ) -> MergedColumn:
        return MergedColumn(
            name=name,
            type=column_type,
            nullable=nullable,
            written_by=("contract",),
            provenance=("test",),
            null_meaning=None,
        )

    def test_existing_identity_stays_first_and_new_fields_append_nullable(self) -> None:
        compatibility = PhysicalSchemaCompatibility(
            envelope=(),
            source_envelope=(),
            tables={"fact": (PhysicalColumn("existing", "string", False),)},
        )
        ordered = compatible_column_order(
            "fact",
            (self._column("new"), self._column("existing")),
            compatibility,
        )

        assert [column.name for column in ordered] == ["existing", "new"]
        assert ordered[0].nullable is False
        assert ordered[1].nullable is True
        assert ordered[1].null_meaning == "not_yet_available"

    @pytest.mark.parametrize("defect", ["removed", "retyped", "tightened"])
    def test_breaking_historical_identity_is_refused(self, defect: str) -> None:
        baseline = PhysicalColumn("existing", "string", defect == "tightened")
        compatibility = PhysicalSchemaCompatibility(
            envelope=(), source_envelope=(), tables={"fact": (baseline,)}
        )
        current = {
            "removed": (),
            "retyped": (self._column("existing", CanonicalType.BIGINT),),
            "tightened": (self._column("existing", nullable=False),),
        }[defect]

        with pytest.raises(PhysicalSchemaError):
            compatible_column_order("fact", current, compatibility)

    @pytest.mark.parametrize(
        "current",
        [
            (PhysicalColumn("first", "string", False),),
            (
                PhysicalColumn("second", "string", True),
                PhysicalColumn("first", "string", False),
            ),
            (
                PhysicalColumn("first", "bigint", False),
                PhysicalColumn("second", "string", True),
            ),
            (
                PhysicalColumn("first", "string", False),
                PhysicalColumn("second", "string", False),
            ),
            (
                PhysicalColumn("first", "string", False),
                PhysicalColumn("second", "string", True),
                PhysicalColumn("new_required", "string", False),
            ),
        ],
    )
    def test_source_envelope_rejects_removal_reorder_retype_tightening_and_required_addition(
        self,
        current: tuple[PhysicalColumn, ...],
    ) -> None:
        baseline = (
            PhysicalColumn("first", "string", False),
            PhysicalColumn("second", "string", True),
        )

        with pytest.raises(PhysicalSchemaError):
            require_append_only_physical_columns("sourceEnvelope", current, baseline)


class TestSemanticV2StorageProjection:
    @staticmethod
    def _projection() -> dict[str, object]:
        root = pathlib.Path(__file__).resolve().parents[2]
        contracts = [contract for _, contract in load_contracts(root)]
        merged = merge_tables(contracts, load_registry(root))
        tables = tuple(
            sorted((*merged, *withheld_tables(merged, contracts)), key=lambda item: item.name)
        )
        graph = load_publication_graph(
            root,
            frozenset(CanonicalDatasetKey(table.name) for table in tables),
        )
        return v2_storage_json(
            V2StorageRequest(
                tables,
                load_source_packs(root),
                graph,
                load_physical_schema_compatibility(root),
            )
        )

    @pytest.mark.covers("ADR-044")
    def test_projection_declares_all_three_isolated_layers(self) -> None:
        projection = self._projection()
        source = _storage_tables(projection["sourceAlignedTables"])
        candidate = _storage_tables(projection["canonicalCandidateTables"])
        current = _storage_tables(projection["canonicalCurrentTables"])

        # Pack 5 adds sa_amc_exposure_groups and fct_exposure_group_outcome (+ _withheld); packs 6
        # and 7 add two source-aligned tables and fct_exposure_frequency (+ _withheld); packs 14
        # and 15 add two source-aligned tables and fct_shopper_acquisition (+ _withheld); packs
        # 10-12 add three source-aligned tables and fct_segment_profile (no residual).
        assert len(source) == 29
        assert len(candidate) == 25
        assert len(current) == 25
        assert {table["name"] for table in candidate} == {table["name"] for table in current}
        assert candidate == current
        assert candidate is not current

    @pytest.mark.covers("ADR-029", "ADR-044")
    def test_physical_field_ids_are_explicit_contiguous_and_append_only(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        compatibility = load_physical_schema_compatibility(root)
        projection = self._projection()
        source = _storage_tables(projection["sourceAlignedTables"])
        candidate = _storage_tables(projection["canonicalCandidateTables"])

        for table in (*source, *candidate):
            assert [column["fieldId"] for column in table["columns"]] == list(
                range(1, len(table["columns"]) + 1)
            )
        source_baseline = {
            column.name: field_id
            for field_id, column in enumerate(compatibility.source_envelope, start=1)
        }
        for table in source:
            by_name = {column["name"]: column for column in table["columns"]}
            assert {name: by_name[name]["fieldId"] for name in source_baseline} == source_baseline
            additions = [
                column for column in table["columns"] if column["name"] not in source_baseline
            ]
            assert all(column["fieldId"] > len(source_baseline) for column in additions)
            assert all(column["nullable"] is True for column in additions)
        for table in candidate:
            baseline = compatibility.tables.get(table["name"])
            if baseline is None:
                continue
            by_name = {column["name"]: column for column in table["columns"]}
            for offset, historical in enumerate(baseline, start=11):
                assert by_name[historical.name]["fieldId"] == offset
            appended = [
                column
                for column in table["columns"]
                if column["name"]
                not in {item.name for item in compatibility.envelope}
                | {item.name for item in baseline}
            ]
            assert all(column["fieldId"] > 10 + len(baseline) for column in appended)
            assert all(column["nullable"] is True for column in appended)

    def test_new_campaign_table_keeps_required_identity_and_typed_cross_channel_nulls(
        self,
    ) -> None:
        candidate = {
            table["name"]: table
            for table in _storage_tables(self._projection()["canonicalCandidateTables"])
        }
        campaign = candidate["dim_campaign"]
        by_name = {column["name"]: column for column in campaign["columns"]}
        for name in (
            "ecosystem_key",
            "platform_key",
            "channel_group_key",
            "channel_key",
            "campaign_key",
        ):
            assert by_name[name]["nullable"] is False
        for name in ("source_ad_product_type", "source_advertiser_id"):
            assert by_name[name]["nullable"] is True
            assert by_name[name].get("nullMeaning") == "unsupported_by_channel"

    @pytest.mark.covers("ADR-044")
    def test_every_resolved_pack_has_a_ready_physical_source_table(self) -> None:
        projection = self._projection()
        source = _storage_tables(projection["sourceAlignedTables"])
        assert source
        assert {table["readiness"] for table in source} == {"ready"}

    @pytest.mark.covers("ADR-044")
    def test_source_envelope_can_represent_an_empty_publication(self) -> None:
        projection = self._projection()
        source = _storage_tables(projection["sourceAlignedTables"])
        columns = {column["name"]: column for column in source[0]["columns"]}

        assert source[0]["grain"] == [
            "source_publication_id",
            "record_kind",
            "source_row_ordinal",
        ]
        assert columns["record_kind"]["nullable"] is False
        assert columns["source_row_ordinal"]["nullable"] is False
        assert columns["delivery_scope_json"]["nullable"] is False
        assert columns["source_options_json"]["nullable"] is False
        assert columns["source_payload_json"]["nullable"] is True
        assert columns["source_payload_json"].get("nullMeaning") == (
            "not_applicable_to_publication_envelope"
        )

    @pytest.mark.covers("ADR-044")
    def test_canonical_projection_preserves_logical_columns_and_migration_nulls(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        logical = _generated_tables(root / "contracts/generated/tables.json")
        projection = self._projection()
        candidate = _storage_tables(projection["canonicalCandidateTables"])
        logical_by_name = {table["name"]: table for table in logical}
        compatibility = load_physical_schema_compatibility(root)

        for table in candidate:
            projected = {
                column["name"]: column
                for column in table["columns"]
                if column["name"] not in {item.name for item in compatibility.envelope}
            }
            expected = {
                column["name"]: column for column in logical_by_name[table["name"]]["columns"]
            }
            assert set(projected) == set(expected), table["name"]
            baseline = {column.name for column in compatibility.tables.get(table["name"], ())}
            for name, declared in expected.items():
                physical = projected[name]
                assert physical["type"] == declared["type"]
                assert physical["comment"] == declared["comment"]
                if name in baseline or table["name"] not in compatibility.tables:
                    assert physical["nullable"] == declared["nullable"]
                    assert physical.get("nullMeaning") == declared.get("nullMeaning")
                else:
                    # An appended column is physically nullable. It keeps a declared null
                    # meaning (a pack-versioned column's rows before that version are
                    # not_applicable_under_options); an undeclared one reads not_yet_available.
                    assert physical["nullable"] is True
                    assert physical.get("nullMeaning") == (
                        declared.get("nullMeaning") or "not_yet_available"
                    )

    @pytest.mark.covers("ADR-044")
    def test_normalized_source_name_collision_is_refused(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        packs = load_source_packs(root)
        colliding = (
            replace(packs[0], contract_id="collision-a-b"),
            replace(packs[1], contract_id="collision-a_b"),
        )

        contracts = [contract for _, contract in load_contracts(root)]
        merged = merge_tables(contracts, load_registry(root))
        tables = tuple(
            sorted((*merged, *withheld_tables(merged, contracts)), key=lambda item: item.name)
        )
        graph = load_publication_graph(
            root,
            frozenset(CanonicalDatasetKey(table.name) for table in tables),
        )
        with pytest.raises(ValueError, match="collide after physical normalization"):
            v2_storage_json(
                V2StorageRequest(
                    tables,
                    colliding,
                    graph,
                    load_physical_schema_compatibility(root),
                )
            )


def test_an_identity_column_outside_the_grain_never_earns_a_residual_table() -> None:
    """Pack 3 delivers its campaign's ad product outside its grain: a label, not a figure.

    Review FEAT-005-A1-6 pins both sides of the rule: that contract gets no `_withheld` residual,
    while a suppressible contract delivering an additive measure outside its grain keeps one.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    contracts = [contract for _, contract in load_contracts(root)]
    merged = merge_tables(contracts, load_registry(root))
    campaign = next(
        contract
        for contract in contracts
        if contract.contract_id == "amc-new-to-brand-buyers-campaign"
    )
    delivered = {
        field.target_column
        for field in campaign.fields
        if not field.is_literal and not field.assigned_by_load
    }
    assert "ad_product_key" in delivered - set(campaign.grain)
    assert withheld_tables(merged, [campaign]) == ()
    # Control: a suppressible spend writer's additive measure keeps its residual.
    spend = next(
        contract
        for contract in contracts
        if contract.target_table == "fct_ad_exposure" and contract.suppression is not None
    )
    assert [table.name for table in withheld_tables(merged, [spend])] == [
        "fct_ad_exposure_withheld"
    ]
