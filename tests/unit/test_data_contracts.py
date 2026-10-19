# SPDX-License-Identifier: MIT-0
"""Contract rules: the checks that run before any data exists.

Two tiers of protection are pinned here. `contract.py` raises at construction for what cannot be
anything other than wrong, and `validation.py` reports findings for what needs the AMC column
registry to judge. The split matters: a contract with four problems must report four, so the
tests assert that every rule ran rather than that the first one fired. The registry rules carry
the most weight in this module. A wrong scale divisor and a missing suppression discriminator are
the two failures that produce entirely plausible numbers — one is a silent 1000x on a fee, the
other silently deletes rows so totals stop adding up — and neither is detectable once the data
has landed.
"""

from dataclasses import replace

import pytest

from domain.data_contracts.contract import (
    ContractError,
    DataContract,
    FieldMapping,
    SourceFormat,
    SuppressionColumns,
)
from domain.data_contracts.validation import (
    ColumnRegistry,
    RegisteredColumn,
    Severity,
    check_shared_measures_are_separable,
    has_errors,
    validate,
)
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit]


def _registry(*columns: RegisteredColumn, inherits: dict[str, str] | None = None) -> ColumnRegistry:
    """Build a registry from the columns a single test cares about.

    Supplies `source_type` where a test did not, because every real entry carries
    AMC's published type and without one the type-resolution rule fires for a reason
    unrelated to whatever is under test.
    """
    typed = tuple(c if c.source_type else replace(c, source_type="STRING") for c in columns)
    return ColumnRegistry(
        columns={(c.table, c.name): c for c in typed},
        known_tables=frozenset(c.table for c in typed),
        inherits=inherits or {},
    )


EMPTY_REGISTRY = ColumnRegistry(columns={}, known_tables=frozenset(), inherits={})


def _contract(**overrides: object) -> DataContract:
    """Build a minimal valid contract, so a test states only what it breaks."""
    defaults: dict[str, object] = {
        "contract_id": "test-contract",
        "contract_version": 1,
        "source_system": "vendor_central",
        "source_dataset": "GET_VENDOR_SALES_REPORT",
        "source_format": SourceFormat.JSON_DUAL_ARRAY,
        "target_table": "fct_retail_sales",
        "grain": ("product_key",),
        # Deliberately the same single column the default field list maps, so a test
        # overriding `fields` does not trip keys-are-mapped by accident and every
        # failure below is the rule under test rather than the fixture.
        "partition_keys": ("product_key",),
        "measurement_basis": MeasurementBasis.RETAIL_ACTUAL,
        "source_timezone": "PST",
        "ecosystem_key": "amazon",
        "platform_key": "amazon_retail",
        "fields": (
            FieldMapping(
                source_path="salesByAsin[].asin",
                target_column="product_key",
                target_type="string",
            ),
        ),
        "restatement_window_days": 45,
        "staleness_threshold_hours": 96,
    }
    return DataContract(**(defaults | overrides))  # type: ignore[arg-type]


def _errors(contract: DataContract, registry: ColumnRegistry) -> set[str]:
    """Collect the rule names that produced a blocking error."""
    return {f.rule for f in validate(contract, registry) if f.severity is Severity.ERROR}


class TestConstructionInvariants:
    """What `__post_init__` refuses outright, because it cannot be anything but wrong."""

    def test_nullable_column_must_say_which_null_it_means(self) -> None:
        """Collapsing the null meanings is how a privacy withholding becomes a zero."""
        with pytest.raises(ContractError, match="must declare which meaning of null applies"):
            FieldMapping(
                source_path="salesByAsin[].orderedUnits",
                target_column="units_ordered",
                nullable=True,
            )

    def test_a_divisor_below_one_is_refused(self) -> None:
        """A divisor of 0 would raise; a fractional one silently inflates money."""
        with pytest.raises(ContractError, match="at least 1"):
            FieldMapping(source_path="impression_cost", target_column="spend", scale_divisor=0)

    def test_divisor_of_one_is_allowed_and_means_already_local(self) -> None:
        """1 records that scale was considered; absent records that nobody looked."""
        mapping = FieldMapping(
            source_path="salesByAsin[].shippedRevenue.amount",
            target_column="revenue_shipped",
            scale_divisor=1,
            currency_column="currency_code",
        )
        assert mapping.monetary is True

    def test_grain_is_required(self) -> None:
        """A correct figure at an unstated grain is the failure mode to avoid."""
        with pytest.raises(ContractError, match="grain is required"):
            _contract(grain=())

    def test_two_fields_cannot_write_the_same_column(self) -> None:
        """Whichever wins is arbitrary, and the loser's source is silently discarded."""
        with pytest.raises(ContractError, match="same target column"):
            _contract(
                fields=(
                    FieldMapping(source_path="a.asin", target_column="product_key"),
                    FieldMapping(source_path="b.asin", target_column="product_key"),
                )
            )

    def test_source_timezone_is_required(self) -> None:
        """A pre-aggregated day bucket cannot be re-bucketed later."""
        with pytest.raises(ContractError, match="source_timezone"):
            _contract(source_timezone="")


@pytest.mark.covers("ADR-004")
class TestMoneyRules:
    def test_a_bare_monetary_amount_is_an_error(self) -> None:
        """No source supplies FX, so an amount without its currency is unconvertible."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.shippedRevenue.amount",
                    target_column="revenue_shipped",
                    scale_divisor=1,
                ),
            )
        )
        assert "money-carries-currency" in _errors(contract, EMPTY_REGISTRY)

    def test_currency_column_must_be_mapped_by_this_contract(self) -> None:
        """Naming a column nothing produces leaves the amount just as bare."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.shippedRevenue.amount",
                    target_column="revenue_shipped",
                    scale_divisor=1,
                    currency_column="currency_code",
                ),
            )
        )
        assert "money-carries-currency" in _errors(contract, EMPTY_REGISTRY)

    def test_money_with_a_mapped_currency_column_passes(self) -> None:
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.shippedRevenue.amount",
                    target_column="revenue_shipped",
                    scale_divisor=1,
                    currency_column="currency_code",
                ),
                FieldMapping(
                    source_path="x.shippedRevenue.currencyCode",
                    target_column="currency_code",
                    target_type="string",
                ),
            )
        )
        assert not has_errors(validate(contract, EMPTY_REGISTRY))


@pytest.mark.covers("ADR-021")
class TestCurrencyAgreement:
    """One currency column per fact is only safe if the discarded values are asserted.

    The vendor source gives one `currencyCode` per `Amount` and the fact keeps one, so
    two values are dropped. Nothing downstream can then show they ever disagreed — the
    same shape of problem as AMC suppression without the discriminator, where the
    information is destroyed before any check can look at it.
    """

    def _money(self, path: str, column: str) -> FieldMapping:
        return FieldMapping(
            source_path=path,
            target_column=column,
            scale_divisor=1,
            currency_column="currency_code",
        )

    def _contract_with(self, *, agreement: tuple[str, ...]) -> DataContract:
        return _contract(
            currency_agreement=agreement,
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                self._money("salesByAsin[].shippedRevenue.amount", "revenue_shipped"),
                self._money("salesByAsin[].shippedCogs.amount", "cogs_shipped"),
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.currencyCode",
                    target_column="currency_code",
                    target_type="string",
                ),
            ),
        )

    def test_sharing_a_currency_column_without_the_assertion_is_an_error(self) -> None:
        assert "currency-agreement-declared" in _errors(
            self._contract_with(agreement=()), EMPTY_REGISTRY
        )

    def test_a_short_list_cannot_satisfy_the_rule(self) -> None:
        """The expected set is derived from the mappings, not read from the list."""
        findings = [
            f
            for f in validate(
                self._contract_with(agreement=("salesByAsin[].shippedRevenue.currencyCode",)),
                EMPTY_REGISTRY,
            )
            if f.rule == "currency-agreement-declared"
        ]
        assert findings
        assert "shippedCogs.currencyCode" in findings[0].message

    def test_declaring_every_sibling_satisfies_the_rule(self) -> None:
        contract = self._contract_with(
            agreement=(
                "salesByAsin[].shippedRevenue.currencyCode",
                "salesByAsin[].shippedCogs.currencyCode",
            )
        )
        assert not has_errors(validate(contract, EMPTY_REGISTRY))

    def test_a_single_money_column_needs_no_assertion(self) -> None:
        """With one amount there is no second currency to disagree with."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                self._money("salesByAsin[].shippedRevenue.amount", "revenue_shipped"),
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.currencyCode",
                    target_column="currency_code",
                    target_type="string",
                ),
            )
        )
        assert not has_errors(validate(contract, EMPTY_REGISTRY))

    def test_per_field_currency_columns_need_no_assertion(self) -> None:
        """Per-field currency has nothing to assert, so that shape stays available."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.amount",
                    target_column="revenue_shipped",
                    scale_divisor=1,
                    currency_column="revenue_shipped_currency",
                ),
                FieldMapping(
                    source_path="salesByAsin[].shippedRevenue.currencyCode",
                    target_column="revenue_shipped_currency",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="salesByAsin[].shippedCogs.amount",
                    target_column="cogs_shipped",
                    scale_divisor=1,
                    currency_column="cogs_shipped_currency",
                ),
                FieldMapping(
                    source_path="salesByAsin[].shippedCogs.currencyCode",
                    target_column="cogs_shipped_currency",
                    target_type="string",
                ),
            )
        )
        assert not has_errors(validate(contract, EMPTY_REGISTRY))

    def test_amc_scalar_money_needs_no_assertion(self) -> None:
        """AMC gives one currency per table, so there is no sibling path to compare."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(
                FieldMapping(source_path="campaign", target_column="product_key"),
                FieldMapping(
                    source_path="total_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(
                    source_path="impression_cost",
                    target_column="spend_media_only",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(source_path="currency_iso_code", target_column="currency_code"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="campaign", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(
                name="total_cost",
                table="dsp_impressions",
                threshold="LOW",
                monetary=True,
                scale_divisor=100_000,
            ),
            RegisteredColumn(
                name="impression_cost",
                table="dsp_impressions",
                threshold="LOW",
                monetary=True,
                scale_divisor=100_000,
            ),
            RegisteredColumn(name="currency_iso_code", table="dsp_impressions", threshold="LOW"),
        )
        assert "currency-agreement-declared" not in _errors(contract, registry)


@pytest.mark.covers("ADR-017")
class TestRatiosAreNeverStored:
    @pytest.mark.parametrize(
        "column", ["roas_blended", "ctr", "conversion_cvr", "cpc_average", "acos"]
    )
    def test_a_stored_ratio_is_an_error(self, column: str) -> None:
        """A stored ratio is wrong the moment two rows are summed."""
        contract = _contract(
            grain=("product_key",),
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.computed",
                    target_column=column,
                    target_type="string",
                ),
            ),
        )
        assert "no-stored-ratios" in _errors(contract, EMPTY_REGISTRY)

    def test_a_column_merely_containing_a_ratio_substring_is_allowed(self) -> None:
        """Matching is on whole tokens, so an embedded sequence is not a match."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.concept",
                    target_column="acostic_label",
                    target_type="string",
                ),
            )
        )
        assert "no-stored-ratios" not in _errors(contract, EMPTY_REGISTRY)


class TestGrainRules:
    @pytest.mark.parametrize(
        "table", ["fct_retail_sales_daily", "fct_ad_events_hourly", "fct_sales_by_week"]
    )
    def test_grain_in_the_table_name_is_an_error(self, table: str) -> None:
        """Encoding grain in a name makes every regrain a rename."""
        contract = _contract(target_table=table)
        assert "grain-not-in-table-name" in _errors(contract, EMPTY_REGISTRY)

    def test_a_grain_column_no_field_produces_is_an_error(self) -> None:
        contract = _contract(grain=("product_key", "never_mapped"))
        assert "keys-are-mapped" in _errors(contract, EMPTY_REGISTRY)

    def test_a_partition_column_no_field_produces_is_an_error(self) -> None:
        contract = _contract(partition_keys=("never_mapped",))
        assert "keys-are-mapped" in _errors(contract, EMPTY_REGISTRY)

    def test_suppression_columns_satisfy_the_key_requirement(self) -> None:
        """The discriminator comes from the suppression block, not a field mapping."""
        contract = _contract(
            grain=("product_key", "is_suppressed"),
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
        )
        assert "keys-are-mapped" not in _errors(contract, EMPTY_REGISTRY)


class TestRegistryIsAnAllowlist:
    """An unrecognised AMC column is unverified, not absent, and may not be used."""

    def test_an_unregistered_column_is_an_error(self) -> None:
        """Threshold cannot be inferred from a name: event_hour_utc LOW, event_dt_utc MEDIUM."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="invented_column", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW")
        )
        assert "amc-column-verified" in _errors(contract, registry)

    def test_an_untranscribed_source_table_is_an_error(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("not_transcribed",),
            fields=(FieldMapping(source_path="advertiser", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW")
        )
        assert "amc-table-transcribed" in _errors(contract, registry)

    def test_a_disputed_column_is_an_error(self) -> None:
        """Disputed means our source doc and the live page disagree.

        Using one asserts a fact no document supports.
        """
        contract = _contract(
            source_system="amc",
            source_tables=("conversions_all",),
            fields=(FieldMapping(source_path="combined_sales", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(
                name="combined_sales",
                table="conversions_all",
                threshold="LOW",
                disputed=True,
            )
        )
        assert "amc-column-disputed" in _errors(contract, registry)

    @pytest.mark.parametrize("threshold", ["VERY_HIGH", "INTERNAL"])
    def test_a_non_outputtable_column_is_an_error(self, threshold: str) -> None:
        """These are legal inside a CTE and never in a final SELECT."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="user_id", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(name="user_id", table="dsp_impressions", threshold=threshold)
        )
        assert "amc-column-outputtable" in _errors(contract, registry)

    def test_a_deprecated_column_names_its_replacement(self) -> None:
        """campaign_id truncates silently; campaign_id_string does not.

        The wrong one drops rows instead of erroring.
        """
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="campaign_id", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(
                name="campaign_id",
                table="dsp_impressions",
                threshold="LOW",
                deprecated_for_use=True,
                preferred_alternative="campaign_id_string",
            )
        )
        findings = [f for f in validate(contract, registry) if f.rule == "amc-preferred-column"]
        assert findings and "campaign_id_string" in findings[0].message

    def test_inheritance_resolves_a_column_from_the_parent_table(self) -> None:
        """dsp_video_events_feed documents 15 columns but carries all of dsp_impressions."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_video_events_feed",),
            fields=(FieldMapping(source_path="advertiser", target_column="product_key"),),
        )
        registry = ColumnRegistry(
            columns={
                ("dsp_impressions", "advertiser"): RegisteredColumn(
                    name="advertiser", table="dsp_impressions", threshold="LOW"
                )
            },
            known_tables=frozenset({"dsp_impressions", "dsp_video_events_feed"}),
            inherits={"dsp_video_events_feed": "dsp_impressions"},
        )
        assert "amc-column-verified" not in _errors(contract, registry)

    def test_vendor_contracts_are_exempt_from_registry_rules(self) -> None:
        """The registry describes AMC. A vendor path must not be looked up in it."""
        contract = _contract(
            fields=(
                FieldMapping(
                    source_path="salesByAsin[].asin",
                    target_column="product_key",
                    target_type="string",
                ),
            )
        )
        assert not has_errors(validate(contract, EMPTY_REGISTRY))


class TestScaleDivisors:
    """The highest-value rules here: a wrong divisor is a plausible-looking number."""

    def test_a_monetary_column_with_no_declared_divisor_is_an_error(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="impression_cost", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(
                name="impression_cost",
                table="dsp_impressions",
                threshold="LOW",
                monetary=True,
                scale_divisor=100_000,
            )
        )
        assert "divisor-declared" in _errors(contract, registry)

    def test_a_divisor_disagreeing_with_the_registry_is_an_error(self) -> None:
        """dsp_impressions mixes millicents and microcents, so a per-table divisor is 1000x off."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(
                FieldMapping(
                    source_path="impression_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(source_path="advertiser", target_column="currency_code"),
            ),
        )
        registry = _registry(
            RegisteredColumn(
                name="impression_cost",
                table="dsp_impressions",
                threshold="LOW",
                monetary=True,
                scale_divisor=100_000_000,
            ),
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW"),
        )
        findings = [f for f in validate(contract, registry) if f.rule == "divisor-matches-registry"]
        assert findings and findings[0].severity is Severity.ERROR
        assert "100000000" in findings[0].message

    def test_a_matching_divisor_passes(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(
                FieldMapping(source_path="campaign", target_column="product_key"),
                FieldMapping(
                    source_path="impression_cost",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(source_path="advertiser", target_column="currency_code"),
            ),
        )
        registry = _registry(
            RegisteredColumn(
                name="impression_cost",
                table="dsp_impressions",
                threshold="LOW",
                monetary=True,
                scale_divisor=100_000,
            ),
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="dsp_impressions", threshold="LOW"),
        )
        assert not has_errors(validate(contract, registry))

    def test_a_divisor_on_a_non_monetary_column_warns_rather_than_blocks(self) -> None:
        """The registry may just not record it as monetary, so this cannot be certain."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(
                FieldMapping(source_path="campaign_id_string", target_column="product_key"),
                FieldMapping(
                    source_path="advertiser",
                    target_column="spend",
                    scale_divisor=100_000,
                    currency_column="currency_code",
                ),
                FieldMapping(source_path="campaign", target_column="currency_code"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(name="campaign_id_string", table="dsp_impressions", threshold="LOW"),
        )
        findings = [f for f in validate(contract, registry) if f.rule == "divisor-matches-registry"]
        assert findings and findings[0].severity is Severity.WARNING
        assert not has_errors(validate(contract, registry))


@pytest.mark.covers("ADR-019")
class TestSuppressionMustBeVisible:
    """The one rule whose violation can never be detected downstream.

    Without the discriminator AMC deletes sub-threshold rows rather than flagging them. No gap,
    no marker, no error, and totals that quietly do not add up — and the information is destroyed
    inside AMC before delivery.
    """

    def _escalating_registry(self) -> ColumnRegistry:
        return _registry(
            RegisteredColumn(name="event_dt_utc", table="conversions", threshold="MEDIUM"),
            RegisteredColumn(name="advertiser", table="conversions", threshold="LOW"),
        )

    def test_an_escalating_column_without_suppression_columns_is_an_error(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            fields=(FieldMapping(source_path="event_dt_utc", target_column="product_key"),),
        )
        assert "suppression-declared" in _errors(contract, self._escalating_registry())

    def test_a_low_threshold_contract_needs_no_suppression_columns(self) -> None:
        """Every planned V1 column is NONE or LOW, so this rule must not fire on them."""
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            fields=(FieldMapping(source_path="advertiser", target_column="product_key"),),
        )
        assert "suppression-declared" not in _errors(contract, self._escalating_registry())

    def test_declared_suppression_columns_must_reach_the_target_schema(self) -> None:
        """All three are schema: without them zero is indistinguishable from suppressed."""
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            grain=("product_key", "is_suppressed"),
            grain_feasibility_note="every output column is LOW except event_dt_utc.",
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(FieldMapping(source_path="event_dt_utc", target_column="product_key"),),
        )
        assert "suppression-in-schema" in _errors(contract, self._escalating_registry())

    def test_suppression_must_not_silently_collapse_the_declared_grain(self) -> None:
        """Nulled rows re-aggregate with SUM, so a merged row duplicates the declared key."""
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            grain=("product_key",),
            grain_feasibility_note="event_dt_utc is MEDIUM; the rest are LOW.",
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(
                FieldMapping(source_path="event_dt_utc", target_column="product_key"),
                FieldMapping(source_path="advertiser", target_column="is_suppressed"),
                FieldMapping(source_path="advertiser_id", target_column="suppression_reason"),
                FieldMapping(source_path="campaign", target_column="distinct_users"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="event_dt_utc", table="conversions", threshold="MEDIUM"),
            RegisteredColumn(name="advertiser", table="conversions", threshold="LOW"),
            RegisteredColumn(name="advertiser_id", table="conversions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="conversions", threshold="LOW"),
        )
        assert "suppression-collapses-key" in _errors(contract, registry)

    def test_accepting_the_collapse_explicitly_satisfies_the_rule(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            grain=("product_key",),
            grain_feasibility_note="event_dt_utc is MEDIUM; the rest are LOW.",
            key_unique_among_unsuppressed_only=True,
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(
                FieldMapping(source_path="event_dt_utc", target_column="product_key"),
                FieldMapping(source_path="advertiser", target_column="is_suppressed"),
                FieldMapping(source_path="advertiser_id", target_column="suppression_reason"),
                FieldMapping(source_path="campaign", target_column="distinct_users"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="event_dt_utc", table="conversions", threshold="MEDIUM"),
            RegisteredColumn(name="advertiser", table="conversions", threshold="LOW"),
            RegisteredColumn(name="advertiser_id", table="conversions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="conversions", threshold="LOW"),
        )
        assert "suppression-collapses-key" not in _errors(contract, registry)

    def test_an_escalating_grain_requires_a_feasibility_note(self) -> None:
        """A human must state why this grain is emittable given every threshold class."""
        contract = _contract(
            source_system="amc",
            source_tables=("conversions",),
            grain=("product_key", "is_suppressed"),
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(
                FieldMapping(source_path="event_dt_utc", target_column="product_key"),
                FieldMapping(source_path="advertiser", target_column="is_suppressed"),
                FieldMapping(source_path="advertiser_id", target_column="suppression_reason"),
                FieldMapping(source_path="campaign", target_column="distinct_users"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="event_dt_utc", table="conversions", threshold="MEDIUM"),
            RegisteredColumn(name="advertiser", table="conversions", threshold="LOW"),
            RegisteredColumn(name="advertiser_id", table="conversions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="conversions", threshold="LOW"),
        )
        assert "grain-feasibility-note" in _errors(contract, registry)


class TestDerivedColumns:
    """Reach is why `derived_from` exists.

    `COUNT(DISTINCT user_id)` cannot be a field mapping, because `user_id` is VERY_HIGH
    and may never appear in output. Naming the inputs keeps them verifiable while the
    output column stays something no registry entry describes.
    """

    def _reach(self) -> FieldMapping:
        return FieldMapping(
            source_path="COUNT(DISTINCT user_id)",
            target_column="reach_users",
            derived_from=("user_id",),
            non_additive=True,
        )

    def _registry(self) -> ColumnRegistry:
        return _registry(
            RegisteredColumn(name="user_id", table="sponsored_ads_traffic", threshold="VERY_HIGH"),
            RegisteredColumn(name="impressions", table="sponsored_ads_traffic", threshold="NONE"),
            RegisteredColumn(
                name="event_dt_utc", table="sponsored_ads_traffic", threshold="MEDIUM"
            ),
            RegisteredColumn(
                name="marketplace_id", table="sponsored_ads_traffic", threshold="INTERNAL"
            ),
        )

    def _contract(self, **overrides: object) -> DataContract:
        base: dict[str, object] = {
            "source_system": "amc",
            "source_tables": ("sponsored_ads_traffic",),
            "grain_feasibility_note": "every output column is NONE or LOW; user_id is CTE-only.",
            "fields": (
                FieldMapping(source_path="impressions", target_column="product_key"),
                self._reach(),
            ),
        }
        return _contract(**(base | overrides))

    def test_a_very_high_input_is_legal_because_it_stays_in_a_cte(self) -> None:
        """The whole point: reach needs user_id, which can never be output."""
        assert "amc-column-outputtable" not in _errors(self._contract(), self._registry())

    def test_a_derived_input_must_still_exist_in_the_registry(self) -> None:
        contract = self._contract(
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    source_path="COUNT(DISTINCT invented)",
                    target_column="reach_users",
                    derived_from=("invented",),
                ),
            )
        )
        assert "amc-column-verified" in _errors(contract, self._registry())

    def test_an_internal_input_is_refused_even_in_a_cte(self) -> None:
        """VERY_HIGH is CTE-legal; INTERNAL is not usable at all."""
        contract = self._contract(
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    source_path="MAX(marketplace_id)",
                    target_column="marketplace_key",
                    derived_from=("marketplace_id",),
                ),
            )
        )
        assert "amc-derived-input-usable" in _errors(contract, self._registry())

    def test_a_derived_input_does_not_escalate_the_threshold(self) -> None:
        """A CTE column is not in the output, so it does not lift the bar to 100."""
        contract = self._contract(
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    source_path="COUNT(DISTINCT event_dt_utc)",
                    target_column="active_hours",
                    derived_from=("event_dt_utc",),
                ),
            )
        )
        assert "suppression-declared" not in _errors(contract, self._registry())

    def test_an_escalating_output_column_still_escalates(self) -> None:
        """The exemption is for derived inputs only, not a way to launder a MEDIUM column."""
        contract = self._contract(
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(source_path="event_dt_utc", target_column="event_hour"),
            )
        )
        assert "suppression-declared" in _errors(contract, self._registry())

    def test_a_derived_column_is_exempt_from_the_divisor_rules(self) -> None:
        """A derived value has no registry entry, so there is no divisor to compare."""
        assert "divisor-declared" not in _errors(self._contract(), self._registry())


class TestExtractionOptionColumns:
    """The suppression trio are workflow options, not columns on any AMC table.

    AMC synthesises them into the delivered output because the workflow asked for them,
    and the names are ours to choose. A contract declaring them as source columns would
    assert they exist in a schema where they do not.
    """

    def test_an_extraction_option_column_is_not_looked_up_in_the_registry(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("sponsored_ads_traffic",),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    source_path="values_removed",
                    target_column="is_suppressed",
                    from_extraction_option=True,
                ),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="sponsored_ads_traffic", threshold="NONE")
        )
        assert "amc-column-verified" not in _errors(contract, registry)

    def test_a_field_cannot_be_both_derived_and_an_extraction_option(self) -> None:
        """One our SQL computes, one the platform supplies. Exactly one must apply."""
        with pytest.raises(ContractError, match="more than one provenance"):
            FieldMapping(
                source_path="values_removed",
                target_column="is_suppressed",
                derived_from=("user_id",),
                from_extraction_option=True,
            )

    def test_an_ordinary_column_is_still_looked_up(self) -> None:
        """The exemption must not become a way to bypass the allowlist."""
        contract = _contract(
            source_system="amc",
            source_tables=("sponsored_ads_traffic",),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(FieldMapping(source_path="invented_column", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="sponsored_ads_traffic", threshold="NONE")
        )
        assert "amc-column-verified" in _errors(contract, registry)


@pytest.mark.covers("ADR-022")
class TestLiteralColumns:
    """A literal is knowledge we hold, not data we received.

    `channel_key` on a DSP contract is the case: DSP tables carry no `ad_product_type`,
    because AMC's convention is that a NULL there *means* DSP. There is nothing to map.
    """

    def test_a_literal_needs_no_source_and_is_not_looked_up(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    target_column="channel_key",
                    literal_value="amazon_dsp",
                    target_type="string",
                ),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="dsp_impressions", threshold="NONE")
        )
        assert not has_errors(validate(contract, registry))

    def test_a_literal_cannot_also_declare_a_source_path(self) -> None:
        """Inventing a path would make a constant look like a field we read."""
        with pytest.raises(ContractError, match="not a field we read"):
            FieldMapping(
                source_path="ad_product_type",
                target_column="channel_key",
                literal_value="amazon_dsp",
            )

    def test_a_field_declaring_no_provenance_at_all_is_refused(self) -> None:
        """Five provenances exist and a column must have exactly one of them.

        The message names all three of the sourceless ones, because a field reaching this has
        no `source_path` and the author needs to know what the alternatives are.
        """
        with pytest.raises(ContractError, match="nothing says where its value comes from"):
            FieldMapping(target_column="channel_key")

    def test_a_literal_satisfies_a_grain_column(self) -> None:
        """DSP has no ad_product_type, so only a literal can satisfy that grain column."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            grain=("product_key", "channel_key"),
            grain_feasibility_note="every output column is NONE or LOW.",
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(
                    target_column="channel_key",
                    literal_value="amazon_dsp",
                    target_type="string",
                ),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="dsp_impressions", threshold="NONE")
        )
        assert "keys-are-mapped" not in _errors(contract, registry)


class TestFeasibilityNote:
    def test_every_amc_contract_needs_one(self) -> None:
        """A free-zone conclusion is still one someone had to reach; write it down."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            fields=(FieldMapping(source_path="impressions", target_column="product_key"),),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="dsp_impressions", threshold="NONE")
        )
        assert "grain-feasibility-note" in _errors(contract, registry)

    def test_a_vendor_contract_does_not_need_one(self) -> None:
        """Vendor Central has no threshold mechanism, so there is nothing to reason about."""
        assert "grain-feasibility-note" not in _errors(_contract(), EMPTY_REGISTRY)


class TestSuppressionDeclarationIsCheckedEvenAtLow:
    """A voluntary declaration used to go entirely unchecked.

    The consistency checks were folded into the rule that fires only on escalation, so a
    contract declaring suppression at LOW — where the bar is 2 users and a single-user
    campaign-day still suppresses — had its declaration validated by nothing.
    """

    def _low_registry(self) -> ColumnRegistry:
        return _registry(
            RegisteredColumn(name="impressions", table="sponsored_ads_traffic", threshold="NONE"),
            RegisteredColumn(name="campaign", table="sponsored_ads_traffic", threshold="LOW"),
        )

    def test_unmapped_suppression_columns_are_caught_without_escalation(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("sponsored_ads_traffic",),
            grain=("product_key", "is_suppressed"),
            grain_feasibility_note="every output column is NONE or LOW.",
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(FieldMapping(source_path="impressions", target_column="product_key"),),
        )
        assert "suppression-in-schema" in _errors(contract, self._low_registry())

    def test_key_collapse_is_caught_without_escalation(self) -> None:
        contract = _contract(
            source_system="amc",
            source_tables=("sponsored_ads_traffic",),
            grain=("product_key",),
            grain_feasibility_note="every output column is NONE or LOW.",
            suppression=SuppressionColumns(
                discriminator_column="is_suppressed",
                reason_column="suppression_reason",
                distinct_user_count_column="distinct_users",
            ),
            fields=(
                FieldMapping(source_path="impressions", target_column="product_key"),
                FieldMapping(source_path="campaign", target_column="is_suppressed"),
                FieldMapping(source_path="campaign_id_string", target_column="suppression_reason"),
                FieldMapping(source_path="advertiser", target_column="distinct_users"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="impressions", table="sponsored_ads_traffic", threshold="NONE"),
            RegisteredColumn(name="campaign", table="sponsored_ads_traffic", threshold="LOW"),
            RegisteredColumn(
                name="campaign_id_string", table="sponsored_ads_traffic", threshold="LOW"
            ),
            RegisteredColumn(name="advertiser", table="sponsored_ads_traffic", threshold="LOW"),
        )
        assert "suppression-collapses-key" in _errors(contract, registry)


class TestEveryRuleRuns:
    def test_a_contract_with_several_problems_reports_all_of_them(self) -> None:
        """Finding the next problem on the next run turns one fix into an afternoon."""
        contract = _contract(
            target_table="fct_retail_sales_daily",
            grain=("product_key", "never_mapped"),
            fields=(
                FieldMapping(
                    source_path="x.asin",
                    target_column="product_key",
                    target_type="string",
                ),
                FieldMapping(
                    source_path="x.shippedRevenue.amount",
                    target_column="revenue_shipped",
                    scale_divisor=1,
                ),
                FieldMapping(
                    source_path="x.computed",
                    target_column="roas_blended",
                    target_type="string",
                ),
            ),
        )
        assert _errors(contract, EMPTY_REGISTRY) == {
            "grain-not-in-table-name",
            "keys-are-mapped",
            "money-carries-currency",
            "no-stored-ratios",
        }

    def test_errors_sort_before_warnings(self) -> None:
        """A human reads the blocking problems first."""
        contract = _contract(
            source_system="amc",
            source_tables=("dsp_impressions",),
            target_table="fct_sales_daily",
            fields=(
                FieldMapping(
                    source_path="advertiser",
                    target_column="spend",
                    scale_divisor=100,
                    currency_column="currency_code",
                ),
                FieldMapping(source_path="campaign", target_column="currency_code"),
                FieldMapping(source_path="campaign_id_string", target_column="product_key"),
            ),
        )
        registry = _registry(
            RegisteredColumn(name="advertiser", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(name="campaign", table="dsp_impressions", threshold="LOW"),
            RegisteredColumn(name="campaign_id_string", table="dsp_impressions", threshold="LOW"),
        )
        severities = [f.severity for f in validate(contract, registry)]
        assert Severity.ERROR in severities
        assert Severity.WARNING in severities
        assert severities == sorted(severities, key=lambda s: s is not Severity.ERROR)


class TestTheTableNamePrefix:
    """`fct_` or `dim_`, validated rather than trusted.

    Ordinarily a naming convention would be a steering rule. This one is checked because
    the prefix decides whether the conformed provenance columns are projected, so an
    unprefixed table would silently produce a fact with no `basis_key` — and a fact whose
    basis is unknown can be summed with a fact of another basis, which ADR-002 calls a bug
    with good branding.
    """

    def test_a_fact_prefix_is_accepted(self) -> None:
        assert _contract(target_table="fct_retail_sales").targets_a_fact is True

    def test_a_dimension_prefix_is_accepted_and_is_not_a_fact(self) -> None:
        assert _contract(target_table="dim_product").targets_a_fact is False

    @pytest.mark.parametrize("name", ["retail_sales", "sales_fct_", "FCT_RETAIL_SALES", "tbl_x"])
    def test_anything_else_is_refused_at_construction(self, name: str) -> None:
        with pytest.raises(ContractError, match="must start with"):
            _contract(target_table=name)

    def test_the_refusal_names_the_offending_table(self) -> None:
        """An author who typo'd a table name needs to see which name was rejected."""
        with pytest.raises(ContractError, match="retail_sales"):
            _contract(target_table="retail_sales")


@pytest.mark.covers("ADR-033")
class TestSharedMeasureClassification:
    @staticmethod
    def _writer(contract_id: str, measure: str) -> DataContract:
        return _contract(
            contract_id=contract_id,
            target_table="fct_ad_exposure",
            grain=("channel_key",),
            partition_keys=("channel_key",),
            fields=(
                FieldMapping(
                    target_column="channel_key",
                    literal_value="amazon_dsp",
                    target_type="string",
                ),
                FieldMapping(
                    target_column="channel_group_key",
                    literal_value="display",
                    target_type="string",
                ),
                FieldMapping(
                    source_path=measure,
                    target_column=measure,
                    target_type="long",
                ),
            ),
        )

    def test_shared_governed_channel_group_is_not_misclassified_as_a_measure(self) -> None:
        clicks = self._writer("click-writer", "clicks")
        views = self._writer("view-writer", "viewable_impressions")

        assert check_shared_measures_are_separable((clicks, views)) == ()

    def test_genuinely_shared_measure_still_requires_disjoint_grain_domains(self) -> None:
        first = self._writer("first-impression-writer", "impressions")
        second = self._writer("second-impression-writer", "impressions")

        findings = check_shared_measures_are_separable((first, second))

        assert len(findings) == 1
        assert findings[0].rule == "shared_measures_are_separable"
        assert "impressions" in findings[0].message
