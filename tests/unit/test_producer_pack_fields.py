# SPDX-License-Identifier: MIT-0
"""ADR-110 query-pack fields: defaults, digests, the producer planner and the Finalizer checks.

Every pack here is synthetic, derived in memory from an installed pack; nothing is added to
`contracts/`. Each refusal names its exact finding code or message and the layer that raised it,
beside a paired positive control.
"""

from __future__ import annotations

# The adapter's definition readers are private seams, exercised directly so a malformed
# definition is refused at the layer that owns it.
# pyright: reportPrivateUsage=false
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.producer import packs as pack_loader
from adapters.producer.packs import load_query_packs, pack_as_dict, pack_by_id
from domain.producer.models import (
    AccessTier,
    GovernedQueryPack,
    InputParameter,
    ParameterDataType,
    PeriodGrain,
    PeriodPolicy,
    ScheduleKind,
    SourceRegistration,
    SuppressionPolicy,
    digest_bytes,
)
from domain.producer.parameters import (
    CommerceIdentityRequest,
    ParameterResolutionError,
    resolve_commerce_identities,
)
from domain.producer.periods import is_aligned
from domain.producer.planning import PlanLandingRequest, plan_landing
from domain.producer.validation import validate_pack, validate_registration
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.identity import IdentityMapping, MarketplaceKey, SourceIdentifier

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-110", "ADR-050")]

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = load_commerce_identity_bridge(ROOT)
COUNTRY = InputParameter("dsp_campaign_country", ParameterDataType.STRING, "amc.campaign_country")
MARKETPLACE_NAME = InputParameter(
    "sponsored_marketplace_name", ParameterDataType.STRING, "amc.marketplace_name"
)


_DSP_IMPRESSIONS = pack_by_id(ROOT, "amc-dsp-impressions")


def _base() -> GovernedQueryPack:
    return _DSP_IMPRESSIONS


def _with_sql(pack: GovernedQueryPack, sql: str) -> GovernedQueryPack:
    return replace(pack, sql=sql, query_digest=digest_bytes(sql.encode()))


def _parameterized(*parameters: InputParameter) -> GovernedQueryPack:
    """Filter the DSP impressions pack on one CUSTOM_PARAMETER per declared parameter."""
    base = _base()
    where = " AND ".join(
        f"campaign_country = CUSTOM_PARAMETER('{item.name}')" for item in parameters
    )
    sql = base.sql.replace(" GROUP BY", f" WHERE {where} GROUP BY")
    return replace(_with_sql(base, sql), input_parameters=parameters)


def _request(
    pack: GovernedQueryPack, period: tuple[date, date], **changes: Any
) -> PlanLandingRequest:
    """Build a request for `pack` over `period`; `replace` re-runs the request's own checks."""
    start, end = period
    daily = PlanLandingRequest(
        pack=_base(),
        execution_id="synthetic_execution_001",
        workflow_id="adlytics_synthetic_v1",
        schedule_kind=ScheduleKind.ADHOC,
        schedule_id=None,
        source_scope=SourceIdentifier("amc.entity", "fictional-producer-entity"),
        marketplace="US",
        period_start=start,
        period_end=end,
        time_window_start=datetime(start.year, start.month, start.day, tzinfo=UTC),
        time_window_end=datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1),
        source_watermark=date(2026, 10, 5),
        retrieved_at=datetime(2026, 10, 5, 6, tzinfo=UTC),
        object_version_id="incoming-version-001",
    )
    return replace(daily, pack=pack, **changes)


def _codes(
    pack: GovernedQueryPack,
    registration: SourceRegistration,
    bridge: CommerceIdentityBridge | None = BRIDGE,
) -> set[str]:
    report = validate_registration(pack, registration, bridge)
    return {finding.code for finding in report.findings}


SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
WEEK = (date(2026, 8, 10), date(2026, 8, 16))


# --- Defaults: existing packs declare nothing new and keep their digests (C-14) -----------------


PERIODIC_PACK = "amc-new-to-brand-buyers-platform"
CHANNEL_PACK = "amc-new-to-brand-buyers-channel"
"""Pack 2 (FEAT-004 of wave 2b) declares exactly pack 1's fields."""
CAMPAIGN_PACK = "amc-new-to-brand-buyers-campaign"
"""Pack 3 (FEAT-005 of wave 2b) declares pack 1's fields at calendar months only."""
SEGMENT_PACKS = ("amc-dsp-segment-names", "amc-segment-metadata", "amc-audience-performance")
"""Packs 9, 13 and 8 (FEAT-008 of wave 2b): monthly, discard, governed parameters."""
COHORT_PACKS = ("amc-acquisition-cohorts", "amc-acquisition-cohorts-arp")
"""Packs 14 and 15 (FEAT-010 of wave 2b): cohort vintages, discard, a period-derived parameter."""
PROFILE_PACKS = (
    "amc-segment-buyer-profile-inmarket",
    "amc-segment-buyer-profile-lifestyle",
    "amc-segment-buyer-profile-dsp-exposed",
)
"""Packs 10-12 (FEAT-009 of wave 2b): monthly, discard, governed and snapshot-range parameters."""
DSP_REACH_PACK = "amc-dsp-reach"
"""The first pack declaring non-default ADR-110 fields (FEAT-003); every other keeps them all."""


def test_every_installed_pack_takes_the_defaults_and_renders_as_before() -> None:
    generated = json.loads((ROOT / "contracts/generated/producer-packs.json").read_text())
    digests = {item["packId"]: item["packDigest"] for item in generated["packs"]}
    for pack in load_query_packs(ROOT):
        if pack.pack_id in {
            PERIODIC_PACK,
            CHANNEL_PACK,
            CAMPAIGN_PACK,
            DSP_REACH_PACK,
            "amc-exposure-groups",
            "amc-exposure-frequency-platform",
            "amc-exposure-frequency-channel",
            *SEGMENT_PACKS,
            *COHORT_PACKS,
            *PROFILE_PACKS,
        }:
            continue
        assert pack.period_policy is None
        assert pack.input_parameters == ()
        assert pack.expected_bar == 2
        assert pack.effective_cadence_lag_days == 2
        assert pack.declared_fields in ({}, {"rejectSuppressedRows": True})
        assert digests[pack.pack_id] == pack.digest
        assert pack_as_dict(pack)["packDigest"] == pack.digest


@pytest.mark.parametrize(
    ("pack_id", "grains"),
    [
        (PERIODIC_PACK, ["week", "month"]),
        (CHANNEL_PACK, ["week", "month"]),
        (CAMPAIGN_PACK, ["month"]),
    ],
)
def test_the_periodic_pack_renders_exactly_its_declared_fields(
    pack_id: str, grains: list[str]
) -> None:
    generated = json.loads((ROOT / "contracts/generated/producer-packs.json").read_text())
    assert generated["schemaVersion"] == 2
    pack = pack_by_id(ROOT, pack_id)
    assert pack.declared_fields == {
        "inputParameters": {
            "sponsored_marketplace_name": {
                "commerceIdentity": "amc.marketplace_name",
                "dataType": "STRING",
            }
        },
        "periodPolicy": {"grains": grains},
        "suppressionPolicy": "discard_suppressed_rows",
    }
    # expectedBar 2 and cadenceLagDays 2 are declared and are the defaults, so they don't render.
    assert (pack.expected_bar, pack.effective_cadence_lag_days) == (2, 2)
    rendered = next(item for item in generated["packs"] if item["packId"] == pack_id)
    assert rendered["packDigest"] == pack.digest


def test_the_dsp_reach_pack_renders_exactly_its_declared_fields() -> None:
    """Pack 4: daily, reject_delivery, one governed parameter; the bar and lag are defaults."""
    generated = json.loads((ROOT / "contracts/generated/producer-packs.json").read_text())
    pack = pack_by_id(ROOT, DSP_REACH_PACK)
    assert pack.period_policy is None
    assert pack.declared_fields == {
        "inputParameters": {
            "dsp_campaign_country": {
                "commerceIdentity": "amc.campaign_country",
                "dataType": "STRING",
            }
        },
        "rejectSuppressedRows": True,
    }
    assert (pack.expected_bar, pack.effective_cadence_lag_days) == (2, 2)
    rendered = next(item for item in generated["packs"] if item["packId"] == DSP_REACH_PACK)
    assert rendered["packDigest"] == pack.digest


def test_reject_suppressed_rows_reads_as_reject_delivery_with_the_same_digest() -> None:
    reach = pack_by_id(ROOT, "amc-reach-frequency")
    assert reach.suppression_policy is SuppressionPolicy.REJECT_DELIVERY
    assert reach.reject_suppressed_rows
    unchanged = replace(reach, suppression_policy=SuppressionPolicy.REJECT_DELIVERY)
    assert unchanged.digest == reach.digest
    discard = replace(reach, suppression_policy=SuppressionPolicy.DISCARD_SUPPRESSED_ROWS)
    assert not discard.reject_suppressed_rows
    assert discard.declared_fields == {"suppressionPolicy": "discard_suppressed_rows"}
    assert discard.digest != reach.digest


def test_a_declared_default_keeps_the_digest_and_a_non_default_moves_it() -> None:
    base = _base()
    assert replace(base, cadence_lag_days=2).digest == base.digest
    assert replace(base, expected_bar=2).digest == base.digest
    for changed in (
        replace(base, cadence_lag_days=16),
        replace(base, expected_bar=100),
        replace(base, period_policy=PeriodPolicy((PeriodGrain.MONTH,))),
        _parameterized(COUNTRY),
    ):
        assert changed.digest != base.digest


def test_a_paid_pack_must_declare_its_cadence_lag() -> None:
    # Positive control: the installed paid pack declares 2, the default, so its digest is the
    # one it had before the field existed.
    paid = pack_by_id(ROOT, "amc-retail-purchases")
    assert paid.access_tier is AccessTier.PAID_FEATURE
    assert paid.cadence_lag_days == 2
    with pytest.raises(
        ValueError, match=r"amc-retail-purchases: a paid-feature pack must declare cadenceLagDays\."
    ):
        replace(paid, cadence_lag_days=None)
    # A standard pack takes the default without declaring it.
    assert replace(_base(), cadence_lag_days=None).effective_cadence_lag_days == 2


def test_expected_bar_is_two_or_one_hundred() -> None:
    assert replace(_base(), expected_bar=100).expected_bar == 100
    with pytest.raises(ValueError, match=r"amc-dsp-impressions: expectedBar must be 2 or 100\."):
        replace(_base(), expected_bar=3)


# --- The definition adapter ---------------------------------------------------------------------


def _definition(tmp_path: Path, **extra: object) -> dict[str, object]:
    source = json.loads((ROOT / "contracts/amc/query-packs/amc-dsp-impressions.json").read_text())
    path = tmp_path / "pack.json"
    path.write_text(json.dumps({**source, **extra}))
    return pack_loader._definition(path)


def test_the_adapter_reads_every_new_field(tmp_path: Path) -> None:
    raw = _definition(
        tmp_path,
        suppressionPolicy="discard_suppressed_rows",
        periodPolicy={"grains": ["week", "month"]},
        inputParameters={
            "sponsored_marketplace_name": {
                "dataType": "STRING",
                "commerceIdentity": "amc.marketplace_name",
            },
            "dsp_campaign_country": {
                "dataType": "STRING",
                "commerceIdentity": "amc.campaign_country",
            },
        },
    )
    assert pack_loader._suppression_policy(raw) is SuppressionPolicy.DISCARD_SUPPRESSED_ROWS
    assert pack_loader._period_policy(raw) == PeriodPolicy((PeriodGrain.WEEK, PeriodGrain.MONTH))
    assert pack_loader._input_parameters(raw) == (COUNTRY, MARKETPLACE_NAME)


def test_the_adapter_refuses_both_suppression_declarations(tmp_path: Path) -> None:
    # Positive control: the boolean alone still reads as reject_delivery.
    assert (
        pack_loader._suppression_policy(_definition(tmp_path, rejectSuppressedRows=True))
        is SuppressionPolicy.REJECT_DELIVERY
    )
    with pytest.raises(
        ValueError, match="declare suppressionPolicy or rejectSuppressedRows, not both"
    ):
        _definition(tmp_path, rejectSuppressedRows=True, suppressionPolicy="reject_delivery")


def test_the_adapter_refuses_an_unknown_key_in_a_new_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="periodPolicy takes exactly one key, grains"):
        pack_loader._period_policy(
            _definition(tmp_path, periodPolicy={"grains": ["month"], "alignment": "calendar_month"})
        )
    with pytest.raises(
        ValueError, match="takes dataType and exactly one of commerceIdentity and periodValue"
    ):
        pack_loader._input_parameters(
            _definition(
                tmp_path,
                inputParameters={"dsp_campaign_country": {"dataType": "ARRAY", "value": "US"}},
            )
        )


# --- validate_pack: parameters bind to the SQL both ways ---------------------------------------


def test_every_declared_parameter_is_read_by_the_sql_and_every_read_one_is_declared() -> None:
    assert validate_pack(_parameterized(COUNTRY)).valid
    undeclared = replace(_parameterized(COUNTRY), input_parameters=())
    report = validate_pack(undeclared)
    assert [(f.code, f.detail) for f in report.findings] == [
        (
            "PACK_PARAMETER_UNDECLARED",
            "SQL reads CUSTOM_PARAMETER dsp_campaign_country with no inputParameters entry.",
        )
    ]
    unused = replace(_base(), input_parameters=(COUNTRY,))
    assert [(f.code, f.detail) for f in validate_pack(unused).findings] == [
        (
            "PACK_PARAMETER_UNUSED",
            "inputParameters dsp_campaign_country never appear as CUSTOM_PARAMETER in the SQL.",
        )
    ]


# --- Periods: the planner writes the grain, the Finalizer checks alignment ---------------------


@pytest.mark.parametrize(
    ("grain", "period", "aligned"),
    [
        (PeriodGrain.WEEK, (date(2026, 8, 10), date(2026, 8, 16)), True),
        (PeriodGrain.WEEK, (date(2026, 8, 11), date(2026, 8, 17)), False),
        (PeriodGrain.MONTH, (date(2026, 2, 1), date(2026, 2, 28)), True),
        (PeriodGrain.MONTH, (date(2026, 12, 1), date(2026, 12, 31)), True),
        (PeriodGrain.MONTH, (date(2026, 9, 1), date(2026, 9, 29)), False),
        (PeriodGrain.QUARTER, (date(2026, 7, 1), date(2026, 9, 30)), True),
        (PeriodGrain.QUARTER, (date(2026, 10, 1), date(2026, 12, 31)), True),
        (PeriodGrain.QUARTER, (date(2026, 8, 1), date(2026, 10, 31)), False),
    ],
)
def test_alignment(grain: PeriodGrain, period: tuple[date, date], aligned: bool) -> None:
    assert is_aligned(grain, period) is aligned


def test_a_periodic_registration_carries_its_grain_and_finalizes() -> None:
    pack = replace(_base(), period_policy=PeriodPolicy((PeriodGrain.WEEK, PeriodGrain.MONTH)))
    registration = plan_landing(_request(pack, SEPTEMBER, period_grain=PeriodGrain.MONTH))
    assert registration.option_set["period_grain"] == "month"
    assert validate_registration(pack, registration).valid
    weekly = plan_landing(_request(pack, WEEK, period_grain=PeriodGrain.WEEK))
    # Week and month deliveries of one pack are separate option sets (ADR-110 point 1).
    assert weekly.option_set["period_grain"] == "week"
    assert validate_registration(pack, weekly).valid


def test_the_planner_refuses_a_missing_wrong_or_misaligned_grain() -> None:
    periodic = replace(_base(), period_policy=PeriodPolicy((PeriodGrain.MONTH,)))
    with pytest.raises(ValueError, match="amc-dsp-impressions requires period_grain, one of month"):
        _request(periodic, SEPTEMBER)
    with pytest.raises(ValueError, match="requires period_grain, one of month"):
        _request(periodic, WEEK, period_grain=PeriodGrain.WEEK)
    with pytest.raises(ValueError, match=r"2026-08-10\.\.2026-08-16 is not one whole month"):
        _request(periodic, WEEK, period_grain=PeriodGrain.MONTH)
    with pytest.raises(ValueError, match="is a daily pack and takes no period_grain"):
        _request(_base(), WEEK, period_grain=PeriodGrain.WEEK)
    # Positive control: the daily pack plans with no grain, as before.
    assert "period_grain" not in plan_landing(_request(_base(), WEEK)).option_set


def test_the_finalizer_refuses_a_registration_whose_grain_or_period_disagrees() -> None:
    pack = replace(_base(), period_policy=PeriodPolicy((PeriodGrain.MONTH,)))
    registration = plan_landing(_request(pack, SEPTEMBER, period_grain=PeriodGrain.MONTH))
    assert _codes(pack, registration) == set()
    wrong_grain = replace(
        registration, option_set={**registration.option_set, "period_grain": "week"}
    )
    assert _codes(pack, wrong_grain) == {"REGISTRATION_PERIOD_GRAIN"}
    # A daily registration presented for a periodic pack: no grain option at all.
    daily = plan_landing(_request(_base(), WEEK))
    assert _codes(pack, daily) == {"REGISTRATION_OPTION_KEYS", "REGISTRATION_PERIOD_GRAIN"}
    # A week window with a month grain: the window check passes, the alignment check doesn't.
    misaligned_options = {**daily.option_set, "period_grain": "month"}
    misaligned = replace(daily, option_set=misaligned_options)
    report = validate_registration(pack, misaligned, BRIDGE)
    assert [(f.code, f.detail) for f in report.findings] == [
        (
            "REGISTRATION_PERIOD_ALIGNMENT",
            "period 2026-08-10..2026-08-16 is not one whole calendar month (UTC).",
        )
    ]
    # A periodic grain on a daily pack is an undeclared option.
    assert _codes(_base(), misaligned) == {"REGISTRATION_OPTION_KEYS"}


# --- Parameters: resolved from the governed bridge, re-resolved by the Finalizer ---------------


def test_parameters_resolve_from_the_governed_bridge_for_the_registration_marketplace() -> None:
    pack = _parameterized(COUNTRY, MARKETPLACE_NAME)
    identities = resolve_commerce_identities(
        BRIDGE, CommerceIdentityRequest(pack=pack, marketplace="US", period=WEEK)
    )
    assert identities == {"amc.campaign_country": "US", "amc.marketplace_name": "AMAZON.COM"}
    registration = plan_landing(_request(pack, WEEK, commerce_identities=identities))
    assert registration.option_set["dsp_campaign_country"] == "US"
    assert registration.option_set["sponsored_marketplace_name"] == "AMAZON.COM"
    assert validate_registration(pack, registration, BRIDGE).valid


def test_the_finalizer_refuses_a_parameter_that_disagrees_with_the_bridge() -> None:
    pack = _parameterized(COUNTRY)
    identities = {"amc.campaign_country": "US"}
    registration = plan_landing(_request(pack, WEEK, commerce_identities=identities))
    assert _codes(pack, registration) == set()
    forged = replace(
        registration, option_set={**registration.option_set, "dsp_campaign_country": "GB"}
    )
    report = validate_registration(pack, forged, BRIDGE)
    assert [(f.code, f.detail) for f in report.findings] == [
        (
            "REGISTRATION_PARAMETER",
            "parameter dsp_campaign_country must equal the governed amc.campaign_country "
            "value 'US'.",
        )
    ]
    # Fails closed when no bridge was loaded.
    assert _codes(pack, registration, None) == {"REGISTRATION_PARAMETER_UNRESOLVED"}


def test_a_namespace_with_no_pinned_value_refuses_the_registration() -> None:
    unpinned = InputParameter(
        "segment_marketplace_id", ParameterDataType.LONG, "amc.segment_marketplace_id"
    )
    pack = _parameterized(unpinned)
    without_pin = CommerceIdentityBridge(
        BRIDGE.commerce_scope,
        tuple(
            mapping
            for mapping in BRIDGE.marketplace_mappings
            if mapping.source.namespace != "amc.segment_marketplace_id"
        ),
    )
    request = CommerceIdentityRequest(pack=pack, marketplace="US", period=WEEK)
    with pytest.raises(
        ParameterResolutionError,
        match=r"amc\.segment_marketplace_id has no value pinned for amazon_us on 2026-08-10",
    ):
        resolve_commerce_identities(without_pin, request)
    with pytest.raises(ValueError, match=r"no pinned value for amc\.segment_marketplace_id"):
        _request(pack, WEEK)
    # Positive control: the committed bridge pins the US value, "1".
    assert resolve_commerce_identities(BRIDGE, request) == {"amc.segment_marketplace_id": "1"}
    registration = plan_landing(
        _request(pack, WEEK, commerce_identities={"amc.segment_marketplace_id": "1"})
    )
    assert _codes(pack, registration, without_pin) == {"REGISTRATION_PARAMETER_UNRESOLVED"}


def test_a_bare_query_parameter_value_is_never_a_registration_marketplace() -> None:
    pack = _parameterized(COUNTRY)
    with pytest.raises(ParameterResolutionError, match="marketplace '1' has no single governed"):
        resolve_commerce_identities(
            BRIDGE, CommerceIdentityRequest(pack=pack, marketplace="1", period=WEEK)
        )


def test_a_mapping_that_changes_mid_period_is_refused() -> None:
    pack = _parameterized(COUNTRY)
    kept = tuple(
        mapping
        for mapping in BRIDGE.marketplace_mappings
        if mapping.source.namespace != "amc.campaign_country"
    )
    moved = (
        *kept,
        IdentityMapping(
            SourceIdentifier("amc.campaign_country", "US"),
            MarketplaceKey("amazon_us"),
            mapping_version=3,
            valid_from=date(2000, 1, 1),
            valid_to=date(2026, 8, 13),
        ),
        IdentityMapping(
            SourceIdentifier("amc.campaign_country", "USA"),
            MarketplaceKey("amazon_us"),
            mapping_version=4,
            valid_from=date(2026, 8, 13),
        ),
    )
    bridge = CommerceIdentityBridge(BRIDGE.commerce_scope, moved)
    with pytest.raises(ParameterResolutionError, match=r"amc\.campaign_country changes value"):
        resolve_commerce_identities(
            bridge, CommerceIdentityRequest(pack=pack, marketplace="AMAZON.COM", period=WEEK)
        )
