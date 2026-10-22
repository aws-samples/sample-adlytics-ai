# SPDX-License-Identifier: MIT-0
"""Landing fixtures as conforming deliveries, and reading a manifest back.

Two properties do the real work.

- **Data objects are written before the marker.** That ordering *is* the trigger contract: the
  manifest arriving is what says a delivery is complete, so a marker written first fires the
  load against objects that are not there. Nothing downstream can distinguish that from a
  delivery whose data never arrived.
- **`declared_columns` is measured from the bytes, never taken from the contract.** Derived from
  the contract it would make the header assertion compare a contract against itself and pass
  whatever arrived — which is the one assertion whose failure cannot be recovered downstream.
"""

from __future__ import annotations

import csv
import dataclasses
import functools
import itertools
import json
import pathlib
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

from adapters.amc_csv import dict_rows
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.curation.manifests import (
    checksum_of,
    declared_columns_of,
    read_manifest,
    write_manifest,
)
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog
from adapters.jobs.fixture_generation.shape import AttributionMaturity, CampaignDay
from adapters.jobs.fixture_generation.shapes import WINDOW_COLUMNS
from adapters.jobs.ingest_delivery import semantic_sidecar
from adapters.jobs.land_fixtures import __main__ as land_cli
from adapters.jobs.land_fixtures import bundle as bundle_module
from adapters.jobs.land_fixtures import job as land_job
from adapters.jobs.land_fixtures.bundle import (
    _RETIRED_GENERATION_PARTITIONS,  # pyright: ignore[reportPrivateUsage]
    _RETIRED_TRAP_PARTITIONS,  # pyright: ignore[reportPrivateUsage]
    AMC_DAY_DELIVERY_FIXTURES,
    AMC_FIXTURE_CONTRACTS,
    AMC_PERIODIC_FIXTURES,
    CAMPAIGN_DIMENSION_FIXTURES,
    LANDABLE,
    MAX_POPULATED_BUCKETS_PER_DELIVERY,
    MAX_SEAL_DELIVERIES_PER_FIXTURE,
    DeliveryClass,
    LandedDelivery,
    PlannedObject,
    PlanSelection,
    _mature_attribution_row,  # pyright: ignore[reportPrivateUsage]
    plan_landings,
    plan_with_backfill,
)
from domain.curation.landing import MANIFEST_NAME, LandingError
from domain.data_contracts.contract import DataContract
from domain.operations.generated_execution_graphs import ROUTES
from domain.operations.run_request import HistoricalPolicy, is_historical, probe_slot_instants
from domain.producer.models import PERIOD_GRAIN_OPTION
from domain.semantics.calendar_periods import vintage_ending
from domain.semantics.canonical_compiler import CanonicalRow
from domain.semantics.dimension_history import (
    OPEN_ENDED,
    DimensionHistoryRequest,
    build_dimension_history,
)
from domain.semantics.publication import SuccessfulPublication
from domain.semantics.revision import RevisionSchemeKey, SourceRevision
from domain.shared_kernel.errors import DependencyError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-030", "F-01")]

ROOT = pathlib.Path(__file__).resolve().parents[2]


@functools.cache
def _plan() -> tuple[LandedDelivery, ...]:
    """Return the committed plan, built once per session (a pure function of the tree)."""
    return plan_landings(ROOT)


_AMC_DAILY_DELIVERIES = 4_495
"""Chunked daily runs per generated AMC fixture over the 463-day span: runs 1..30 populate
1..30 days (ceil(n / 3) chunks each, 165 in all) and the other 433 runs populate 30 days (10
chunks each, 4,330), so 165 + 4,330 = 4,495."""


class _Recording:
    def __init__(self, recorded: dict[str, object] | None = None) -> None:
        self.recorded = recorded if recorded is not None else {}

    def record(self, **values: object) -> None:
        self.recorded.update(values)


# ---------------------------------------------------------------------------
# Ordering: the trigger contract
# ---------------------------------------------------------------------------


def test_every_delivery_writes_its_marker_last() -> None:
    for delivery in _plan():
        assert delivery.ordered[-1] is delivery.marker
        assert delivery.marker.key.endswith(f"/{MANIFEST_NAME}")


def test_every_data_object_precedes_the_marker() -> None:
    for delivery in _plan():
        marker_at = delivery.ordered.index(delivery.marker)
        assert all(delivery.ordered.index(item) < marker_at for item in delivery.data)


def test_fixture_plan_digest_binds_ordered_keys_and_exact_bodies() -> None:
    first = _plan()
    second = plan_landings(ROOT)

    assert land_cli._plan_digest(first) == land_cli._plan_digest(second)  # pyright: ignore[reportPrivateUsage]
    changed = replace(
        first[0],
        marker=replace(first[0].marker, body=first[0].marker.body + b"\n"),
    )
    assert land_cli._plan_digest((changed, *first[1:])) != land_cli._plan_digest(first)  # pyright: ignore[reportPrivateUsage]


def test_fixture_dry_run_root_carries_exact_plan_lineage_and_keeps_console_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deliveries = (_plan()[0],)
    opened: list[dict[str, object]] = []

    @contextmanager
    def capture_span(_name: str, **attributes: object) -> Generator[_Recording]:
        opened.append(attributes)
        yield _Recording()

    def load_catalog(_root: pathlib.Path) -> object:
        return object()

    def load_manifest(_root: pathlib.Path) -> object:
        return object()

    def ignore_validation(*_args: object) -> None:
        return None

    def selected_landings(
        _root: pathlib.Path, _selection: object = None
    ) -> tuple[LandedDelivery, ...]:
        return deliveries

    class Session:
        _session = object()

        @staticmethod
        def client(service: str) -> object:
            if service == "sts":
                return SimpleNamespace(
                    get_caller_identity=lambda: {"Account": "000000000000", "Arn": "arn:test"}
                )
            return object()

    def session_factory(**_kwargs: object) -> Session:
        return Session()

    def configure(*_args: object, **_kwargs: object) -> bool:
        return False

    def ignore_operation(*_args: object) -> None:
        return None

    def flush() -> None:
        return None

    monkeypatch.setattr(land_cli, "load_synthetic_entity_catalog", load_catalog)
    monkeypatch.setattr(land_cli, "load_fixture_evidence_manifest", load_manifest)
    monkeypatch.setattr(land_cli, "validate_fixture_provenance", ignore_validation)
    monkeypatch.setattr(land_cli, "plan_landings", selected_landings)
    monkeypatch.setattr(land_cli.boto3, "Session", session_factory)
    monkeypatch.setattr(land_cli, "configure_tracing", configure)
    monkeypatch.setattr(land_cli, "verify_named_raw_bucket", ignore_validation)
    monkeypatch.setattr(land_cli, "span", capture_span)
    monkeypatch.setattr(land_cli, "report_operation", ignore_operation)
    monkeypatch.setattr(land_cli, "flush_metrics", flush)
    monkeypatch.setattr(land_cli, "flush_traces", flush)

    result = land_cli.main(
        (
            "--profile",
            "explicit-profile",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
            "--operation-id",
            "land-unit",
            "--stack",
            "Adlytics-DataV2-dev",
            "--bucket",
            "explicit-versioned-raw",
            "--dry-run",
        )
    )

    digest = land_cli._plan_digest(deliveries)  # pyright: ignore[reportPrivateUsage]
    assert result == 0
    assert opened == [
        {
            "operation_id": "land-unit",
            "execution_id": opened[0]["execution_id"],
            "predecessor_execution_id": "none",
            "plan_digest": digest,
            "stage": "start",
            "completed": 0,
            "total": len(deliveries),
        }
    ]
    output = capsys.readouterr().out
    assert "LAND FIXTURES INTO THE V2 RAW ZONE (dev only)" in output
    assert output.endswith("Dry run. Nothing was written.\n")


def test_fixture_delivery_span_carries_exact_operator_lineage_and_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = _plan()[0]
    spans: list[tuple[str, dict[str, object], dict[str, object]]] = []
    progress: list[land_job.LandFixturesProgress] = []

    @contextmanager
    def capture_span(name: str, **attributes: object) -> Generator[_Recording]:
        recorded: dict[str, object] = {}
        spans.append((name, attributes, recorded))
        yield _Recording(recorded)

    def immutable_write(
        _client: object,
        _bucket: str,
        _planned: PlannedObject,
    ) -> tuple[str, bool]:
        return "version-001", True

    monkeypatch.setattr(land_job, "span", capture_span)
    monkeypatch.setattr(land_job, "_put_immutable", immutable_write)

    created = land_job.apply_immutable(
        land_job.ApplyLandFixturesRequest(
            client=object(),  # type: ignore[arg-type]
            bucket="explicit-versioned-raw",
            deliveries=(delivery,),
            operation_id="land-unit",
            execution_id="execution-unit",
            predecessor_execution_id=None,
            plan_digest="sha256:reviewed",
            progress=progress.append,
            cancelled=lambda: False,
            historical_policy=_with_backfill()[1],
        )
    )

    assert created == len(delivery.ordered)
    assert spans == [
        (
            land_job.LAND_FIXTURES_DELIVERY,
            {
                "operation_id": "land-unit",
                "execution_id": "execution-unit",
                "predecessor_execution_id": "none",
                "plan_digest": "sha256:reviewed",
                "stage": "apply",
                "completed": 0,
                "total": 1,
            },
            {"completed": 1},
        )
    ]
    assert [(item.stage, item.completed, item.total) for item in progress] == [("apply", 1, 1)]


def test_fixture_delivery_cancellation_does_not_advance_before_marker_is_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = _plan()[0]
    cancelled = False
    writes: list[PlannedObject] = []
    progress: list[land_job.LandFixturesProgress] = []

    def immutable_write(
        _client: object,
        _bucket: str,
        planned: PlannedObject,
    ) -> tuple[str, bool]:
        nonlocal cancelled
        writes.append(planned)
        cancelled = True
        return "version-001", True

    monkeypatch.setattr(land_job, "_put_immutable", immutable_write)

    with pytest.raises(land_job.LandFixturesCancelledError):
        land_job.apply_immutable(
            land_job.ApplyLandFixturesRequest(
                client=object(),  # type: ignore[arg-type]
                bucket="explicit-versioned-raw",
                deliveries=(delivery,),
                operation_id="land-unit",
                execution_id="execution-unit",
                predecessor_execution_id=None,
                plan_digest="sha256:reviewed",
                progress=progress.append,
                cancelled=lambda: cancelled,
                historical_policy=_with_backfill()[1],
            )
        )

    assert writes == [delivery.data[0]]
    assert progress == []


def test_every_object_sits_inside_its_own_manifests_derived_prefix() -> None:
    """If a key and its manifest disagree, the load refuses the landing — so they must not."""
    for delivery in _plan():
        for planned in delivery.ordered:
            assert planned.key.startswith(delivery.manifest.prefix)


def test_each_fixture_lands_at_a_distinct_prefix() -> None:
    """Two deliveries at one prefix means the second silently overwrites the first."""
    prefixes = [delivery.manifest.prefix for delivery in _plan()]
    assert len(set(prefixes)) == len(prefixes)


def test_the_restatement_pair_differs_only_in_its_retrieval_segment() -> None:
    """The pair exists to exercise the watermark decision, so it must share window and options."""
    planned = {d.fixture: d.manifest for d in _plan()}
    earlier = planned["traps/vendor-restatement-earlier.json"]
    later = planned["traps/vendor-restatement-later.json"]

    assert earlier.period_start == later.period_start
    assert earlier.option_set_hash == later.option_set_hash
    assert earlier.source_watermark is not None
    assert later.source_watermark is not None
    assert earlier.source_watermark < later.source_watermark
    assert earlier.prefix != later.prefix


def test_every_amc_fixture_binds_its_exact_governed_contract() -> None:
    planned = _plan()
    planned_bases = {delivery.fixture.split("#", maxsplit=1)[0] for delivery in planned}

    assert set(AMC_FIXTURE_CONTRACTS) <= planned_bases
    for delivery in planned:
        fixture = delivery.fixture.split("#", maxsplit=1)[0]
        contract_id = AMC_FIXTURE_CONTRACTS.get(fixture)
        if contract_id is None:
            continue
        governance = delivery.manifest.governance
        assert governance is not None
        assert governance.data_contract_id == contract_id
        assert governance.source_pack_id == contract_id
        assert governance.query_pack_id == contract_id


def test_campaign_and_fact_outputs_over_one_dataset_have_distinct_raw_identities() -> None:
    planned = {delivery.fixture: delivery.manifest for delivery in _plan()}
    run = "#daily-run=2026-09-15#chunk=2026-08-17"
    pairs = (
        ("generated/amc-dsp-impressions.csv", "generated/amc-campaign-dsp.csv"),
        ("generated/amc-reach-frequency.csv", "generated/amc-campaign-sponsored.csv"),
    )
    for fact_name, campaign_name in pairs:
        fact = planned[f"{fact_name}{run}"]
        campaign = planned[f"{campaign_name}{run}"]
        assert fact.source_dataset == campaign.source_dataset
        assert fact.period_start == campaign.period_start
        assert fact.option_set_hash == campaign.option_set_hash
        assert fact.retrieved_at == campaign.retrieved_at
        assert fact.governance is not None
        assert campaign.governance is not None
        assert fact.governance.query_pack_id != campaign.governance.query_pack_id
        assert fact.prefix != campaign.prefix


def test_every_campaign_and_advertiser_fixture_identity_is_governed_fictional() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    campaign_ids = {
        value for campaign in catalog.campaigns for value in campaign.source_aliases.values()
    } | set(catalog.trap_campaign_ids)
    campaign_names = {
        name.value for campaign in catalog.campaigns for name in campaign.names
    } | set(catalog.unnamed_campaign_names)
    advertiser_ids = {
        value for _name, aliases in catalog.advertiser_names.values() for value in aliases.values()
    }
    advertiser_names = {name for name, _aliases in catalog.advertiser_names.values()}
    fixture_paths = tuple(
        path
        for directory in (ROOT / "tests/fixtures/generated", ROOT / "tests/fixtures/traps")
        for path in sorted(directory.glob("*.csv"))
    )

    assert fixture_paths
    for path in fixture_paths:
        # AMC's dialect: a name holding a quote is backslash-escaped (adapters.amc_csv).
        for row in dict_rows(path.read_text())[1]:
            campaign_id = row.get("campaign_id_string", "")
            campaign_name = row.get("campaign", "")
            advertiser_id = row.get("advertiser_id", "")
            advertiser_name = row.get("advertiser", "")
            assert not campaign_id or campaign_id in campaign_ids, (
                path.name,
                campaign_id,
            )
            assert not campaign_name or campaign_name in campaign_names, (
                path.name,
                campaign_name,
            )
            assert not advertiser_id or advertiser_id in advertiser_ids, (
                path.name,
                advertiser_id,
            )
            assert not advertiser_name or advertiser_name in advertiser_names, (
                path.name,
                advertiser_name,
            )


def test_working_bundle_uses_unsuppressed_attributed_conversion_fixture() -> None:
    assert "generated/amc-attributed-conversion.csv" in LANDABLE
    assert "traps/amc-attributed-conversion-governed.csv" not in LANDABLE
    deliveries = tuple(
        item
        for item in _plan()
        if item.fixture.startswith("generated/amc-attributed-conversion.csv#daily-run=")
    )
    assert len(deliveries) == _AMC_DAILY_DELIVERIES
    rows = tuple(
        row
        for delivery in deliveries
        for row in csv.DictReader(StringIO(delivery.data[0].body.decode()))
    )
    assert rows
    assert {row["rows_suppressed"] for row in rows} == {"false"}
    assert {row["suppression_reason"] for row in rows} == {""}


def _chunked_amc_run_names(fixture: str, days: tuple[date, ...]) -> tuple[str, ...]:
    """Return the oldest-first chunked delivery names per write-discipline §2.1 rule 2."""
    restatement = {
        contract.contract_id: contract.restatement_window_days
        for _, contract in load_contracts(ROOT)
    }[AMC_FIXTURE_CONTRACTS[fixture]]
    names: list[str] = []
    for run_day in days:
        first = max(days[0], run_day - timedelta(days=restatement - 1))
        populated = tuple(day for day in days if first <= day <= run_day)
        for start in range(0, len(populated), MAX_POPULATED_BUCKETS_PER_DELIVERY):
            names.append(
                f"{fixture}#daily-run={run_day.isoformat()}#chunk={populated[start].isoformat()}"
            )
    return tuple(names)


MONTHLY_FIXTURES = frozenset({"generated/amc-new-to-brand-buyers-campaign.csv"})
"""Pack 3 (FEAT-005) declares calendar months only; the other periodic packs weeks too."""


def _periodic_run_names(fixture: str, first: date, last: date) -> tuple[str, ...]:
    """Return one delivery per complete declared period, by period end then start."""
    periods: list[tuple[date, date]] = []
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    while fixture not in MONTHLY_FIXTURES and monday + timedelta(days=6) <= last:
        periods.append((monday, monday + timedelta(days=6)))
        monday += timedelta(days=7)
    month = (
        first if first.day == 1 else date(first.year + first.month // 12, first.month % 12 + 1, 1)
    )
    while True:
        following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        if following - timedelta(days=1) > last:
            break
        periods.append((month, following - timedelta(days=1)))
        month = following
    return tuple(
        f"{fixture}#period={start.isoformat()}..{end.isoformat()}"
        for start, end in sorted(periods, key=lambda period: (period[1], period[0]))
    )


_D2_CORPORA: dict[str, str] = {
    "generated/amc-exposure-groups.csv": "amc-exposure-groups",
    "generated/amc-exposure-frequency-platform.csv": "amc-exposure-frequency-platform",
    "generated/amc-exposure-frequency-channel.csv": "amc-exposure-frequency-channel",
    "generated/amc-dsp-segment-names.csv": "amc-dsp-segment-names",
    "generated/amc-segment-metadata.csv": "amc-segment-metadata",
    "generated/amc-audience-performance.csv": "amc-audience-performance",
    "generated/amc-segment-buyer-profile-inmarket.csv": "amc-segment-buyer-profile-inmarket",
    "generated/amc-segment-buyer-profile-lifestyle.csv": "amc-segment-buyer-profile-lifestyle",
    "generated/amc-segment-buyer-profile-dsp-exposed.csv": "amc-segment-buyer-profile-dsp-exposed",
    "generated/amc-acquisition-cohorts.csv": "amc-acquisition-cohorts",
    "generated/amc-acquisition-cohorts-arp.csv": "amc-acquisition-cohorts-arp",
}
"""The D2 periodic corpora (packs 5-15) and their governed query packs, stated independently."""


def _declared_refused_period(fixture: str) -> tuple[date, date] | None:
    """Return the one period the catalog declares the load refuses for a D2 corpus."""
    shape = load_synthetic_entity_catalog(ROOT).shape
    if fixture == "generated/amc-exposure-groups.csv":
        week = shape.exposure_groups.currency_disagreement_week
        return (week, week + timedelta(days=6))
    if fixture.startswith("generated/amc-exposure-frequency-"):
        month = shape.exposure_frequency.null_bucket_month
        following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        return (month, following - timedelta(days=1))
    if fixture == "generated/amc-acquisition-cohorts.csv":
        vintage = vintage_ending(shape.acquisition_cohorts.refused_vintage)
        return (vintage.start, vintage.end)
    return None


def _corpus_windows(fixture: str) -> list[tuple[date, date]]:
    """Every distinct AMC window the corpus file itself carries, as inclusive days."""
    start, end = WINDOW_COLUMNS
    _, rows = dict_rows((FIXTURES / fixture).read_text())
    return sorted(
        {
            (
                date.fromisoformat(str(row[start])[:10]),
                date.fromisoformat(str(row[end])[:10]) - timedelta(days=1),
            )
            for row in rows
        },
        key=lambda window: (window[1], window[0]),
    )


def _pinned_unencoded(fixture: str) -> bool:
    """Whether admission's pinned band gives the corpus's dataset no retrieval encoding."""
    from adapters.inbound.run_requests.service import contract_policy  # noqa: PLC0415

    declaration = json.loads(
        (FIXTURES / fixture).with_suffix("").with_suffix(".manifest.json").read_text()
    )
    key = (str(declaration["source_system"]), str(declaration["source_dataset"]))
    return key not in contract_policy().encodings


def _lands_after_cut(fixture: str, end: date) -> bool:
    """Whether a period's landing day (end + the pack's cadence lag, capped) is after the cut."""
    from adapters.inbound.run_requests.service import contract_policy  # noqa: PLC0415

    pack = json.loads((ROOT / f"contracts/amc/query-packs/{_D2_CORPORA[fixture]}.json").read_text())
    last_day = load_synthetic_entity_catalog(ROOT).shape.fixture_span.last_day
    run_day = min(end + timedelta(days=int(pack["cadenceLagDays"])), last_day)
    return run_day > contract_policy().cut


def _corpus_period_names(fixture: str) -> tuple[str, ...]:
    """One delivery per window the corpus carries, minus its declared refused period.

    A corpus whose dataset the pinned band doesn't encode lands only periods after the cut.
    """
    refused = _declared_refused_period(fixture)
    unencoded = _pinned_unencoded(fixture)
    return tuple(
        f"{fixture}#period={start.isoformat()}..{end.isoformat()}"
        for start, end in _corpus_windows(fixture)
        if (start, end) != refused and (not unencoded or _lands_after_cut(fixture, end))
    )


@pytest.mark.covers("ADR-100")
def test_the_d2_corpora_land_under_their_governed_packs_one_delivery_per_period() -> None:
    """Reach, Audiences and Retention need their D2 feeds landed in dev (visual leg, 2026-10-16).

    Every D2 corpus is enumerated, mapped to its own governed pack, and splits into one cadence
    delivery per window it carries, each window aligned to one declared period grain.
    """
    plan = _plan()
    for fixture, pack in _D2_CORPORA.items():
        assert fixture in LANDABLE
        assert fixture in AMC_PERIODIC_FIXTURES
        assert AMC_FIXTURE_CONTRACTS[fixture] == pack
        deliveries = [item for item in plan if item.fixture.split("#", 1)[0] == fixture]
        # Non-empty in the shipped catalog: the empty-corpus rule never hides a broken corpus.
        assert deliveries, fixture
        assert [item.fixture for item in deliveries] == list(_corpus_period_names(fixture))
        assert all(item.delivery_class is DeliveryClass.CADENCE for item in deliveries)
        assert all(item.manifest.option_set.get(PERIOD_GRAIN_OPTION) for item in deliveries)
    # Dimensions are enumerated before the facts that name their segments.
    order = [LANDABLE.index(fixture) for fixture in _D2_CORPORA]
    assert LANDABLE.index("generated/amc-dsp-segment-names.csv") < LANDABLE.index(
        "generated/amc-audience-performance.csv"
    )
    assert order == sorted(order)


@pytest.mark.covers("ADR-100")
def test_the_d2_declared_refused_periods_are_never_landed() -> None:
    """Each D2 corpus with a declared refused period carries it, and the plan omits it."""
    names = {item.fixture for item in _plan()}
    refusing = [fixture for fixture in _D2_CORPORA if _declared_refused_period(fixture) is not None]
    assert len(refusing) == 4
    for fixture in refusing:
        start, end = cast("tuple[date, date]", _declared_refused_period(fixture))
        assert (start, end) in _corpus_windows(fixture)  # positive control: the corpus has it
        assert f"{fixture}#period={start.isoformat()}..{end.isoformat()}" not in names


def _sa_term(contract_id: str, body: str) -> int:
    """`M(u) + 1` of a body's source-aligned unit, every column a 64-byte parameter."""
    from adapters.outbound.athena.semantic_storage import (  # noqa: PLC0415
        PhysicalRecord,
        merge_batches,
    )
    from adapters.outbound.athena.start_envelope import envelope_fits  # noqa: PLC0415
    from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog  # noqa: PLC0415
    from domain.operations.statement_protocol import MAX_STATEMENT_BYTES  # noqa: PLC0415

    table = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json").source_by_contract[
        contract_id
    ]
    row: PhysicalRecord = {column.name: "x" * 64 for column in table.columns}
    _, rows = dict_rows(body)
    records = tuple(dict(row) for _ in rows)
    fits = envelope_fits("adlytics-dev-v2-batch", "adlytics_dev_source_v2", MAX_STATEMENT_BYTES)
    return len(merge_batches(table, records, fits)) + 1


@pytest.mark.covers("ADR-086")
def test_every_d2_delivery_fits_the_unit_cap_and_the_whole_corpus_does_not() -> None:
    """No D2 delivery exceeds the reviewed 10-attempt unit cap; the cap still bites unsplit."""
    from domain.operations.projection_feasibility import (  # noqa: PLC0415
        MAX_ACCEPTED_ATTEMPTS_PER_UNIT,
    )

    for item in _plan():
        fixture = item.fixture.split("#", 1)[0]
        if fixture in _D2_CORPORA:
            body = item.data[0].body.decode()
            assert _sa_term(_D2_CORPORA[fixture], body) <= MAX_ACCEPTED_ATTEMPTS_PER_UNIT
    # Negative control: the widest corpus landed whole would not fit one unit.
    whole = (FIXTURES / "generated/amc-audience-performance.csv").read_text()
    assert _sa_term("amc-audience-performance", whole) > MAX_ACCEPTED_ATTEMPTS_PER_UNIT


@pytest.mark.covers("ADR-100")
def test_only_a_cohort_corpus_on_a_span_without_a_vintage_may_be_empty() -> None:
    """A short span's empty cohort corpus plans nothing; every other empty corpus is refused."""
    shape = load_synthetic_entity_catalog(ROOT).shape
    short = replace(
        shape,
        fixture_span=replace(
            shape.fixture_span, first_day=shape.fixture_span.last_day - timedelta(days=56)
        ),
    )

    def runs(fixture: str, text: str, catalog: Any) -> tuple[Any, ...]:
        return bundle_module._periodic_runs(  # pyright: ignore[reportPrivateUsage]
            bundle_module._SourceRunsRequest(  # pyright: ignore[reportPrivateUsage]
                fixture,
                text,
                {"retrieved_at": "2026-10-06T00:00:00Z"},
                None,
                catalog,
                cadence_lag_days=2,
            )
        )

    cohorts = "generated/amc-acquisition-cohorts.csv"
    assert runs(cohorts, "\n", short) == ()
    assert runs("generated/amc-acquisition-cohorts-arp.csv", "\n", short) == ()
    # The shipped span holds whole vintages, so an empty cohort corpus there is a broken file.
    with pytest.raises(LandingError, match="carries no AMC window"):
        runs(cohorts, "\n", shape)
    # Any other periodic corpus is refused empty, even on the short span.
    with pytest.raises(LandingError, match="carries no AMC window"):
        runs("generated/amc-segment-metadata.csv", "\n", short)
    with pytest.raises(LandingError, match="carries no AMC window"):
        runs(cohorts, "cohort_month,cohort_buyers\n2025-09-01,10\n", short)
    header = (FIXTURES / cohorts).read_text().splitlines()[0]
    with pytest.raises(LandingError, match="carries no AMC window"):
        runs(cohorts, f"{header}\n", short)


_COHORTS = "generated/amc-acquisition-cohorts.csv"


@pytest.mark.covers("ADR-100", "ADR-104")
def test_the_cohort_corpora_plan_every_whole_vintage_the_span_holds_less_the_refused_one() -> None:
    """Packs 14-15 plan one delivery per 13-month vintage in the span, from the catalog alone."""
    shape = load_synthetic_entity_catalog(ROOT).shape
    span = shape.fixture_span
    first = (
        span.first_day
        if span.first_day.day == 1
        else date(
            span.first_day.year + span.first_day.month // 12, span.first_day.month % 12 + 1, 1
        )
    )
    vintages: list[tuple[date, date]] = []
    start = first
    while True:
        index = start.year * 12 + start.month - 1 + 13
        end = date(index // 12, index % 12 + 1, 1) - timedelta(days=1)
        if end > span.last_day:
            break
        vintages.append((start, end))
        start = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    refused = vintage_ending(shape.acquisition_cohorts.refused_vintage)
    planned = _plan()
    for fixture in (
        "generated/amc-acquisition-cohorts.csv",
        "generated/amc-acquisition-cohorts-arp.csv",
    ):
        names = [item.fixture for item in planned if item.fixture.startswith(f"{fixture}#")]
        expected = [
            f"{fixture}#period={a.isoformat()}..{b.isoformat()}"
            for a, b in vintages
            if fixture.endswith("-arp.csv") or (a, b) != (refused.start, refused.end)
        ]
        # Each corpus plans the newest whole vintages it carries, in vintage order.
        assert names == expected[-len(names) :] and names, fixture
    pack_14 = [item for item in planned if item.fixture.startswith(f"{_COHORTS}#")]
    assert len(pack_14) == len(vintages) - 1  # every whole vintage but the refused one


@pytest.mark.covers("ADR-086", "ADR-100")
def test_an_unencoded_periodic_dataset_lands_only_after_the_cut_and_never_historical() -> None:
    """The segment packs' datasets have no retrieval encoding in the pinned band.

    So only their periods landing after the cut are planned, each classified non-historical by
    admission's own predicate; the corpora do carry earlier periods (positive control).
    """
    from adapters.inbound.run_requests.service import contract_policy  # noqa: PLC0415

    policy = contract_policy()
    # The planner's view of the encoded datasets is admission's pinned one, so the flag can't
    # drift from the gap it covers when an encoding is added.
    encoded = bundle_module._encodings(FIXTURES)  # pyright: ignore[reportPrivateUsage]
    assert frozenset(encoded) == frozenset(policy.encodings)
    unencoded = [fixture for fixture in _D2_CORPORA if _pinned_unencoded(fixture)]
    assert len(unencoded) == 6
    planned = _plan()
    for fixture in unencoded:
        assert any(not _lands_after_cut(fixture, end) for _, end in _corpus_windows(fixture))
        deliveries = [item for item in planned if item.fixture.startswith(f"{fixture}#")]
        assert deliveries, fixture
        for item in deliveries:
            assert item.run_day is not None and item.run_day > policy.cut
            assert not is_historical(bundle_module.manifest_facts(item.manifest), policy)


@pytest.mark.covers("ADR-100")
def test_a_skipped_pre_cut_window_is_still_validated() -> None:
    """A phase-A-only corpus refuses a malformed earlier window rather than skipping it."""
    fixture = "generated/amc-segment-metadata.csv"
    text = (FIXTURES / fixture).read_text()
    shape = load_synthetic_entity_catalog(ROOT).shape

    def runs(body: str) -> tuple[Any, ...]:
        return bundle_module._periodic_runs(  # pyright: ignore[reportPrivateUsage]
            bundle_module._SourceRunsRequest(  # pyright: ignore[reportPrivateUsage]
                fixture,
                body,
                {"retrieved_at": "2026-10-06T00:00:00Z"},
                None,
                shape,
                cadence_lag_days=2,
                phase_a_only=True,
            )
        )

    kept = runs(text)
    assert kept  # positive control: the shipped corpus plans its post-cut months
    # The same corpus with its first (pre-cut) month's window start moved off the month's first
    # day: 2025-07-02..2025-07-31 is no declared period, and it lands before the cut.
    first = "2025-07-01T00:00:00.000Z"
    assert first in text
    with pytest.raises(LandingError, match="is no declared period"):
        runs(text.replace(first, "2025-07-02T00:00:00.000Z"))


@pytest.mark.covers("ADR-100")
def test_the_d2_corpora_leave_the_pinned_replay_band_unchanged() -> None:
    """Admission's pinned band still equals the planner's with the D2 deliveries planned."""
    from adapters.inbound.run_requests.service import contract_policy  # noqa: PLC0415

    _, policy = plan_with_backfill(ROOT)
    assert policy == contract_policy()


def test_every_landable_fixture_is_planned_in_declared_and_cadence_order() -> None:
    expected: list[str] = []
    shape = load_synthetic_entity_catalog(ROOT).shape
    span, seal_generation = shape.fixture_span, shape.seal_generation
    day_values = tuple(span.first_day + timedelta(days=offset) for offset in range(span.span_days))
    days = tuple(day.isoformat() for day in day_values)
    for fixture in LANDABLE:
        if fixture in _D2_CORPORA:
            expected.extend(_corpus_period_names(fixture))
        elif fixture in AMC_PERIODIC_FIXTURES:
            expected.extend(_periodic_run_names(fixture, span.first_day, span.last_day))
        elif fixture in AMC_DAY_DELIVERY_FIXTURES:
            # Pack 4: one delivery per declared clean day; the suppressed day isn't landed.
            reach = shape.dsp_reach
            expected.extend(
                f"{fixture}#day={day.isoformat()}"
                for day in reach.days
                if day != reach.suppressed_day
            )
        elif fixture.startswith("generated/amc-"):
            expected.extend(_chunked_amc_run_names(fixture, day_values))
            if fixture in CAMPAIGN_DIMENSION_FIXTURES:
                expected.extend(
                    f"{fixture}#seal-generation={seal_generation}#chunk={days[0]}..{days[-1]}"
                    for days in _expected_seal_chunks(fixture)
                )
        elif fixture == "generated/vendor-sales-by-asin.json":
            for day in days:
                expected.append(f"{fixture}#daily-run={day}")
                # A restating report lands only once due: none for the last two days (§2.2).
                if date.fromisoformat(day) + timedelta(days=2) <= span.last_day:
                    expected.append(f"{fixture}#restatement={day}")
        elif fixture == "generated/vendor-traffic-by-asin.json":
            expected.extend(f"{fixture}#daily-run={day}" for day in days)
        elif fixture == "generated/vendor-product-hierarchy.jsonl":
            # 238 rows (one per span day) per delivery, ADR-086 amendment 2026-10-06.
            expected.extend(
                f"{fixture}#chunk={days[start]}..{days[min(start + 238, len(days)) - 1]}"
                for start in range(0, len(days), 238)
            )
        else:
            expected.append(fixture)
    assert tuple(delivery.fixture for delivery in _plan()) == tuple(expected)


@pytest.mark.covers("ADR-030", "ADR-051")
def test_generated_amc_results_expand_into_chunked_daily_lookback_runs() -> None:
    """A run's trailing window lands as an ordered oldest-first ≤3-bucket chunk series.

    Write-discipline §2.1 (2026-09-22 amendment) rule 2: the wide single delivery per
    run is retired; 462 runs over a 30-day restatement window plan as 4,485 deliveries
    (was 37 runs and 235 before the 2025-07-01..2026-10-05 span, design §2.2).
    """
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    final_start = span.last_day - timedelta(days=29)
    deliveries = tuple(
        delivery
        for delivery in _plan()
        if delivery.fixture.startswith("generated/amc-dsp-impressions.csv#daily-run=")
    )
    assert len(deliveries) == _AMC_DAILY_DELIVERIES
    assert deliveries[0].manifest.period_start == deliveries[0].manifest.period_end
    for delivery in deliveries:
        manifest = delivery.manifest
        rows = tuple(csv.DictReader(StringIO(delivery.data[0].body.decode())))
        populated = {date.fromisoformat(row["impression_date_utc"]) for row in rows}
        assert len(populated) <= MAX_POPULATED_BUCKETS_PER_DELIVERY
        assert min(populated) == manifest.period_start
        assert max(populated) == manifest.period_end
    final_run = tuple(
        delivery
        for delivery in deliveries
        if delivery.fixture.startswith(
            f"generated/amc-dsp-impressions.csv#daily-run={span.last_day.isoformat()}#chunk="
        )
    )
    assert len(final_run) == 10
    assert final_run[0].manifest.period_start == final_start
    assert final_run[-1].manifest.period_end == span.last_day
    covered = {
        day
        for delivery in final_run
        for row in csv.DictReader(StringIO(delivery.data[0].body.decode()))
        for day in (date.fromisoformat(row["impression_date_utc"]),)
    }
    assert covered == {final_start + timedelta(days=offset) for offset in range(30)}
    retrievals = tuple(delivery.manifest.retrieved_at for delivery in final_run)
    assert retrievals == tuple(sorted(retrievals))
    assert len(set(retrievals)) == len(retrievals)


_DEDUPLICATED = bundle_module.DEDUPLICATED_ATTRIBUTION_FIXTURE


@functools.cache
def _generation_plan(fixture: str, generation: int) -> tuple[LandedDelivery, ...]:
    shape = replace(
        load_synthetic_entity_catalog(ROOT).shape, deduplicated_retrieval_generation=generation
    )
    context = bundle_module._plan_context(ROOT, shape)  # pyright: ignore[reportPrivateUsage]
    return tuple(bundle_module._plan_fixture(context, fixture))  # pyright: ignore[reportPrivateUsage]


@pytest.mark.covers("ADR-086")
def test_the_deduplicated_feed_lands_one_generation_up_at_fresh_keys_on_its_run_slots() -> None:
    # 2026-10-22: dev holds generation 0's keys with the rotating one-row-per-day rows, and a
    # landed key is immutable, so the corrected corpus lands under generation 1 (dev-seeding §1):
    # the same rows, windows and watermarks, each retrieval 16 s higher on its run's stride slot,
    # so the replay band classifies every delivery as before and no key equals a generation-0 key.
    assert load_synthetic_entity_catalog(ROOT).shape.deduplicated_retrieval_generation == 1
    committed = tuple(item for item in _plan() if item.fixture.startswith(f"{_DEDUPLICATED}#"))
    old, new = _generation_plan(_DEDUPLICATED, 0), _generation_plan(_DEDUPLICATED, 1)
    assert new == committed
    assert len(old) == len(new) == _AMC_DAILY_DELIVERIES
    _, policy = plan_with_backfill(ROOT)

    def slot(item: LandedDelivery) -> int:
        return (item.manifest.retrieved_at - policy.replay_base) // timedelta(seconds=64)

    for before, after in zip(old, new, strict=True):
        assert (after.fixture, after.run_day, after.delivery_class) == (
            before.fixture,
            before.run_day,
            before.delivery_class,
        )
        assert after.data[0].body == before.data[0].body
        assert after.manifest.retrieved_at == before.manifest.retrieved_at + timedelta(seconds=16)
        assert slot(after) == slot(before)
        assert replace(after.manifest, retrieved_at=before.manifest.retrieved_at).prefix == (
            before.manifest.prefix
        )
        assert bundle_module.window_prefix(after.manifest) == bundle_module.window_prefix(
            before.manifest
        )
        assert (after.manifest.period_start, after.manifest.period_end) == (
            before.manifest.period_start,
            before.manifest.period_end,
        )
        assert after.manifest.source_watermark == before.manifest.source_watermark
        assert is_historical(bundle_module.manifest_facts(after.manifest), policy) == (
            is_historical(bundle_module.manifest_facts(before.manifest), policy)
        )
    old_keys = {key.key for item in old for key in item.ordered}
    assert old_keys.isdisjoint(key.key for item in new for key in item.ordered)
    # Positive control: the generation moves only its own feed.
    other = "generated/amc-attributed-conversion.csv"
    assert _generation_plan(other, 0) == _generation_plan(other, 1)


def test_a_fixtures_selection_lands_only_the_named_feed() -> None:
    selection = PlanSelection(
        frozenset({DeliveryClass.CADENCE}), date(2026, 8, 12), None, frozenset({_DEDUPLICATED})
    )
    selected = plan_landings(ROOT, selection)
    assert selected == tuple(
        item
        for item in _plan()
        if item.fixture.startswith(f"{_DEDUPLICATED}#")
        and item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day >= date(2026, 8, 12)
    )
    assert len(selected) == 560  # runs 2026-08-12..2026-10-06, 10 chunks each
    with pytest.raises(LandingError, match="enumerated landable fixtures"):
        PlanSelection(frozenset({DeliveryClass.CADENCE}), fixtures=frozenset({"traps/none.csv"}))
    parsed = land_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
        _cli_arguments("--classes", "cadence", "--fixtures", _DEDUPLICATED)
    )
    assert parsed.selection == PlanSelection(
        frozenset({DeliveryClass.CADENCE}), fixtures=frozenset({_DEDUPLICATED})
    )


@pytest.mark.covers("ADR-030", "ADR-051")
def test_daily_attribution_runs_restate_recent_dates_as_the_lookback_matures() -> None:
    # The maturation mechanism, under a fictional {0.70, 0.90} declaration (design §3.3): the
    # committed dev catalog carries {1, 1}, so the committed plan no longer steps; the identity
    # case is `test_committed_attribution_maturity_is_the_identity`.
    shape = dataclasses.replace(
        load_synthetic_entity_catalog(ROOT).shape,
        attribution_maturity=AttributionMaturity(Decimal("0.70"), Decimal("0.90")),
    )
    row = {
        "conversion_event_date_utc": "2025-07-01",
        "purchases": "20",
        "product_sales": "560.0000",
    }
    column = "conversion_event_date_utc"
    first = _mature_attribution_row(row, column, date(2025, 7, 1), shape)
    next_day = _mature_attribution_row(row, column, date(2025, 7, 2), shape)
    third_day = _mature_attribution_row(row, column, date(2025, 7, 3), shape)
    assert (first["purchases"], next_day["purchases"], third_day["purchases"]) == ("14", "18", "20")
    assert (first["product_sales"], next_day["product_sales"], third_day["product_sales"]) == (
        "392.0000",
        "504.0000",
        "560.0000",
    )
    deliveries = {
        delivery.fixture.split("#daily-run=", maxsplit=1)[-1]: delivery
        for delivery in _plan()
        if delivery.fixture.startswith("generated/amc-attributed-conversion.csv#daily-run=")
    }
    d0 = load_synthetic_entity_catalog(ROOT).shape.fixture_span.first_day
    d1 = d0 + timedelta(days=1)
    assert (
        deliveries[f"{d0}#chunk={d0}"].manifest.retrieved_at
        < deliveries[f"{d1}#chunk={d0}"].manifest.retrieved_at
    )


@pytest.mark.covers("ADR-086")
def test_committed_attribution_maturity_is_the_identity() -> None:
    shape = load_synthetic_entity_catalog(ROOT).shape
    assert (shape.attribution_maturity.age_0, shape.attribution_maturity.age_1) == (1, 1)
    row = {
        "conversion_event_date_utc": "2026-10-05",
        "purchases": "19",
        "product_sales": "531.9000",
    }
    for run_day in (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)):
        assert _mature_attribution_row(row, "conversion_event_date_utc", run_day, shape) == row


def test_no_fixture_that_must_quarantine_is_landed() -> None:
    """A bundle whose purpose is a working environment must not include a poisoned delivery."""
    assert "traps/vendor-control-total-short.json" not in LANDABLE
    assert "traps/vendor-mixed-currency.json" not in LANDABLE


def test_generated_vendor_deliveries_land_under_serving_lens_options() -> None:
    """A generated Vendor delivery at placeholder options would be unservable evidence."""
    planned = {delivery.fixture: delivery for delivery in _plan()}
    sales = planned["generated/vendor-sales-by-asin.json#restatement=2026-09-15"]
    traffic = planned["generated/vendor-traffic-by-asin.json#daily-run=2026-09-15"]
    assert sales.manifest.option_set == {
        "distributorView": "MANUFACTURING",
        "sellingProgram": "RETAIL",
        "reportPeriod": "DAY",
    }
    assert traffic.manifest.option_set == {"reportPeriod": "DAY"}
    assert set(json.loads(sales.data[0].body)) >= {"salesAggregate", "salesByAsin"}
    assert set(json.loads(traffic.data[0].body)) >= {"trafficAggregate", "trafficByAsin"}


# ---------------------------------------------------------------------------
# What is measured rather than declared
# ---------------------------------------------------------------------------


def test_each_objects_checksum_digests_the_bytes_that_are_written() -> None:
    """Digesting anything other than the uploaded bytes makes the checksum describe nothing."""
    for delivery in _plan():
        landed = delivery.manifest.objects[0]
        assert landed.checksum == checksum_of(delivery.data[0].body)
        assert landed.byte_count == len(delivery.data[0].body)


def test_declared_columns_come_from_the_document_not_from_the_contract() -> None:
    """`marketplaceId` is in the delivered hierarchy file and is not a canonical column."""
    hierarchy = next(d for d in _plan() if d.fixture.endswith("matching-control-total.jsonl"))
    assert "marketplaceId" in hierarchy.manifest.declared_columns
    assert "amazon_asin" not in hierarchy.manifest.declared_columns


def test_a_csv_header_becomes_its_declared_columns() -> None:
    columns = declared_columns_of("a,b,c\n1,2,3\n", source_format="csv")
    assert columns == ("a", "b", "c")


def test_jsonl_columns_are_the_union_across_records() -> None:
    """A record omitting a column is normal, so one row's keys are not the file's columns."""
    text = '{"a": 1}\n{"b": 2}\n'
    assert declared_columns_of(text, source_format="jsonl") == ("a", "b")


def test_a_dual_array_documents_columns_come_from_every_repeating_array() -> None:
    """Found by shape, so naming the array here cannot drift from the contract that reads it."""
    text = json.dumps(
        {"reportSpecification": {"reportType": "X"}, "salesByAsin": [{"asin": "A", "units": 1}]}
    )
    assert declared_columns_of(text, source_format="json_dual_array") == ("asin", "units")


# ---------------------------------------------------------------------------
# The manifest round-trips, and refuses what a producer got wrong
# ---------------------------------------------------------------------------


def test_a_written_manifest_reads_back_identically() -> None:
    for delivery in _plan():
        assert read_manifest(write_manifest(delivery.manifest)) == delivery.manifest


def test_a_landed_manifest_is_byte_identical_across_two_plannings() -> None:
    """Landing the same delivery twice must produce the same bytes, or a diff means nothing."""
    first = {d.fixture: d.marker.body for d in _plan()}
    second = {d.fixture: d.marker.body for d in plan_landings(ROOT)}
    assert first == second


def test_a_manifest_missing_a_required_key_names_the_key() -> None:
    payload = json.loads(write_manifest(_plan()[0].manifest))
    del payload["source_watermark"]
    with pytest.raises(LandingError, match="source_watermark"):
        read_manifest(json.dumps(payload))


def test_a_manifest_whose_timestamp_carries_no_offset_is_refused() -> None:
    """A provenance instant without a zone is ambiguous by up to a day at a boundary."""
    delivery = _plan()[0]
    payload = json.loads(write_manifest(delivery.manifest))
    payload["retrieved_at"] = "2026-08-19T06:00:00"
    with pytest.raises(LandingError, match="no offset"):
        read_manifest(json.dumps(payload))


def test_a_manifest_whose_hash_does_not_match_its_options_is_refused_on_read() -> None:
    """Catches a producer that edited the option set without rehashing it."""
    delivery = _plan()[0]
    payload = json.loads(write_manifest(delivery.manifest))
    payload["option_set"] = dict(payload["option_set"]) | {"distributorView": "SOURCING"}
    with pytest.raises(LandingError, match="disagree"):
        read_manifest(json.dumps(payload))


def test_a_manifest_that_is_not_json_is_refused_with_the_parse_error() -> None:
    with pytest.raises(LandingError, match="not valid JSON"):
        read_manifest("{not json")


def test_a_byte_count_given_as_a_boolean_is_refused() -> None:
    """`bool` is a subclass of `int`, so `byte_count: true` would otherwise read as 1."""
    delivery = _plan()[0]
    payload = json.loads(write_manifest(delivery.manifest))
    payload["objects"][0]["byte_count"] = True
    with pytest.raises(LandingError, match="byte_count"):
        read_manifest(json.dumps(payload))


def test_every_amc_dataset_is_landed() -> None:
    """All six, because the advertising half of the model was empty without them.

    Until these landed, every ROAS, both cost-per-conversion metrics and both viewability
    measures had no data to read — the planner could build their plans and the queries returned
    nothing. Asserted as a set of datasets rather than filenames so a renamed fixture does not
    quietly drop one.
    """
    landed = {delivery.manifest.source_dataset for delivery in _plan()}
    assert {
        "dsp_impressions",
        "dsp_clicks",
        "dsp_views",
        "sponsored_ads_traffic",
        "amazon_attributed_events_by_conversion_time",
        "amazon_retail_purchases",
    } <= landed


def test_the_four_exposure_datasets_land_at_four_prefixes() -> None:
    """`fct_ad_exposure` is written by four contracts, and each delivery is its own object.

    Within one window they share a marketplace and an empty option set, so the *dataset* segment
    is the only thing keeping their prefixes apart. A collision would have one delivery overwrite
    another and the fact would silently hold one contract's measures instead of four.

    Grouped by window rather than counted outright, because the partial-overlap trap lands three
    of these datasets again at a second window — which is the same property holding twice, not a
    weaker one.
    """
    exposure_contracts = {
        "amc-dsp-impressions",
        "amc-dsp-clicks",
        "amc-dsp-views",
        "amc-reach-frequency",
    }
    by_window: dict[tuple[object, object, object], set[str]] = {}
    datasets: dict[tuple[object, object, object], list[str]] = {}
    for delivery in _plan():
        manifest = delivery.manifest
        governance = manifest.governance
        if governance is None or governance.data_contract_id not in exposure_contracts:
            continue
        # Chunking re-lands one window across many runs; the retrieval instant is what
        # separates those recurrences, so the property groups per (window, retrieval).
        window = (manifest.period_start, manifest.period_end, manifest.retrieved_at)
        by_window.setdefault(window, set()).add(manifest.prefix)
        datasets.setdefault(window, []).append(manifest.source_dataset)

    assert by_window, "no exposure delivery is landed at all"
    for window, prefixes in by_window.items():
        assert len(prefixes) == len(datasets[window]) == len(set(datasets[window])), window


def _expected_periodic_options(delivery: LandedDelivery) -> dict[str, object]:
    """Derive a periodic delivery's producer options from its window and rows alone.

    The grain is the calendar period the window is (week, month, quarter), or `custom` for a
    13-month cohort vintage, which also names its `as_of_month`. A delivery bucketed under the
    coarse frequency scheme or grouped under quarterly cohorts names its pack variant.
    """
    start, end = delivery.manifest.period_start, delivery.manifest.period_end
    following = end + timedelta(days=1)
    if (end - start).days == 6 and start.weekday() == 0:
        grain = "week"
    elif (
        start.day == 1
        and following.day == 1
        and (following.year * 12 + following.month - (start.year * 12 + start.month) == 1)
    ):
        grain = "month"
    elif (
        start.day == 1
        and following.day == 1
        and start.month % 3 == 1
        and following.year * 12 + following.month - (start.year * 12 + start.month) == 3
    ):
        grain = "quarter"
    else:
        grain = "custom"
    options: dict[str, object] = {"period_grain": grain}
    rows = list(csv.DictReader(StringIO(delivery.data[0].body.decode())))
    if rows and rows[0].get("bucket_scheme") == "coarse":
        options["pack_variant"] = "coarse"
    if rows and rows[0].get("cohort_grain") == "quarter":
        options["pack_variant"] = "quarterly"
    if grain == "custom":
        options["as_of_month"] = f"{date(end.year, end.month, 1).isoformat()} 00:00:00"
    return options


def test_an_amc_delivery_declares_only_its_period_grain() -> None:
    """Empty for a daily delivery, and exactly the producer's `period_grain` for a periodic one.

    No AMC contract carries `report_options_hash` in its partition keys. The one option that
    names a publication family is a periodic delivery's grain, which a producer registration
    names (`plan_landing`) and the publication lens keeps (`lens_source_options`), so a
    fixture-landed week and month are found under the scope serving rebuilds for that grain.
    """
    periodic = 0
    for delivery in _plan():
        if delivery.manifest.source_system != "amc":
            continue
        if delivery.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES:
            periodic += 1
            assert delivery.manifest.option_set == _expected_periodic_options(delivery)
        else:
            assert delivery.manifest.option_set == {}
    assert periodic


def test_v2_manifest_round_trips_exact_data_object_version_id() -> None:
    delivery = _plan()[0]
    bound = replace(
        delivery.manifest,
        objects=(replace(delivery.manifest.objects[0], version_id="data-version-001"),),
    )
    payload = json.loads(write_manifest(bound))
    assert payload["objects"][0]["version_id"] == "data-version-001"
    assert read_manifest(json.dumps(payload)) == bound


def test_blank_v2_object_version_is_refused() -> None:
    delivery = _plan()[0]
    payload = json.loads(write_manifest(delivery.manifest))
    payload["objects"][0]["version_id"] = ""
    with pytest.raises(LandingError, match="version_id"):
        read_manifest(json.dumps(payload))


@pytest.mark.covers("ADR-030", "ADR-051")
def test_vendor_day_reports_are_source_valid_and_sales_restate_after_return_lag() -> None:
    planned = {delivery.fixture: delivery for delivery in _plan()}
    # A day outside the retired-generation span, where the restating watermark is run day + 2
    # (was 2026-08-13, which now sits inside the retired span; see the retired-span test).
    day = "2025-08-13"
    initial = planned[f"generated/vendor-sales-by-asin.json#daily-run={day}"]
    restated = planned[f"generated/vendor-sales-by-asin.json#restatement={day}"]
    first_doc = json.loads(initial.data[0].body)
    final_doc = json.loads(restated.data[0].body)

    assert initial.manifest.period_start == initial.manifest.period_end == date.fromisoformat(day)
    assert initial.manifest.source_watermark is not None
    assert restated.manifest.source_watermark is not None
    assert restated.manifest.source_watermark > initial.manifest.source_watermark
    assert restated.manifest.retrieved_at > initial.manifest.retrieved_at
    assert initial.manifest.prefix != restated.manifest.prefix
    for array_name in ("salesByAsin", "salesAggregate"):
        assert first_doc[array_name]
        assert final_doc[array_name]
        for row in final_doc[array_name]:
            assert row["startDate"] == row["endDate"] == day
            for amount_name in ("shippedRevenue", "shippedCogs", "orderedRevenue"):
                assert isinstance(row[amount_name]["amount"], (int, float))
                assert row[amount_name]["currencyCode"] == "USD"
        assert (
            first_doc[array_name][0]["customerReturns"]
            < final_doc[array_name][0]["customerReturns"]
        )
        assert (
            first_doc[array_name][0]["shippedRevenue"]["amount"]
            > final_doc[array_name][0]["shippedRevenue"]["amount"]
        )
    for document in (first_doc, final_doc):
        detail = document["salesByAsin"]
        aggregate = document["salesAggregate"][0]
        for field in ("shippedUnits", "orderedUnits", "customerReturns"):
            assert aggregate[field] == sum(row[field] for row in detail)
        for field in ("shippedRevenue", "shippedCogs", "orderedRevenue"):
            assert Decimal(str(aggregate[field]["amount"])) == sum(
                (Decimal(str(row[field]["amount"])) for row in detail),
                Decimal(0),
            )


@pytest.mark.covers("ADR-030", "ADR-051")
def test_every_daily_amc_run_is_complete_for_every_day_and_eligible_campaign() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    expected = {
        "generated/amc-dsp-impressions.csv": {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.channel == "amazon_dsp"
        },
        "generated/amc-reach-frequency.csv": {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.channel != "amazon_dsp"
        },
        "generated/amc-attributed-conversion.csv": {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if campaign.scenario != "zero_conversion"
        },
    }
    date_columns = {
        "generated/amc-dsp-impressions.csv": "impression_date_utc",
        "generated/amc-reach-frequency.csv": "event_date_utc",
        "generated/amc-attributed-conversion.csv": "conversion_event_date_utc",
    }
    # The declared coverage_gap event (was day index 33 of the scenario, 2026-09-12).
    gap = next(
        event
        for event in catalog.shape.events
        if isinstance(event, CampaignDay) and event.kind == "coverage_gap"
    )
    gap_ids = {
        next(iter(campaign.source_aliases.values()))
        for campaign in catalog.campaigns
        if campaign.key == gap.campaign
    }
    gap_day = gap.on.isoformat()

    def silent(day: str) -> set[str]:
        """Campaigns with no delivery on the day: no conversion row (design §3.3)."""
        return {
            next(iter(campaign.source_aliases.values()))
            for campaign in catalog.campaigns
            if not catalog.values.delivering(campaign.key, date.fromisoformat(day))
        }

    for fixture, expected_ids in expected.items():
        for delivery in (item for item in _plan() if item.fixture.startswith(f"{fixture}#")):
            rows = tuple(csv.DictReader(StringIO(delivery.data[0].body.decode())))
            column = date_columns[fixture]
            delivered_days = {row[column] for row in rows}
            for day in delivered_days:
                expected_for_day = (
                    expected_ids - gap_ids
                    if fixture == "generated/amc-reach-frequency.csv" and day == gap_day
                    # The coverage gap removes the campaign from every contract (design §2.2).
                    else expected_ids - silent(day) - (gap_ids if day == gap_day else set())
                    if fixture == "generated/amc-attributed-conversion.csv"
                    else expected_ids
                )
                assert {
                    row["campaign_id_string"] for row in rows if row[column] == day
                } == expected_for_day


@pytest.mark.covers("ADR-030", "ADR-051")
def test_every_vendor_traffic_day_reconciles_to_its_reported_total() -> None:
    deliveries = (
        delivery
        for delivery in _plan()
        if delivery.fixture.startswith("generated/vendor-traffic-by-asin.json#daily-run=")
    )
    for delivery in deliveries:
        document = json.loads(delivery.data[0].body)
        assert document["trafficAggregate"][0]["glanceViews"] == sum(
            row["glanceViews"] for row in document["trafficByAsin"]
        )


# --- Vendor landing rules, trap re-dating and the retired spans (design §2.2-§2.3, item 10) ---

_SALES = "generated/vendor-sales-by-asin.json"
_TRAFFIC = "generated/vendor-traffic-by-asin.json"


def _vendor(fixture: str, kind: str) -> dict[date, LandedDelivery]:
    prefix = f"{fixture}#{kind}="
    return {
        date.fromisoformat(item.fixture.removeprefix(prefix)): item
        for item in _plan()
        if item.fixture.startswith(prefix)
    }


@pytest.mark.covers("ADR-086")
def test_the_last_two_days_carry_only_the_first_report() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    daily, restated = _vendor(_SALES, "daily-run"), _vendor(_SALES, "restatement")
    assert len(daily) == span.span_days
    assert sorted(set(daily) - set(restated)) == [
        span.last_day - timedelta(days=1),
        span.last_day,
    ]
    assert len(restated) == span.span_days - 2


@pytest.mark.covers("ADR-086")
def test_the_first_report_keeps_the_final_net_per_unit_amounts() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    share = catalog.shape.retail.first_report_returns_share
    daily, restated = _vendor(_SALES, "daily-run"), _vendor(_SALES, "restatement")
    restated_day = catalog.shape.fixture_span.first_day + timedelta(days=40)
    first = json.loads(daily[restated_day].data[0].body)["salesByAsin"]
    final = json.loads(restated[restated_day].data[0].body)["salesByAsin"]
    for initial_row, final_row in zip(first, final, strict=True):
        returns = final_row["customerReturns"]
        assert initial_row["customerReturns"] == int(returns * share)
        for money, units in (
            ("shippedRevenue", "shippedUnits"),
            ("shippedCogs", "shippedUnits"),
            ("orderedRevenue", "orderedUnits"),
        ):
            per_unit = Decimal(str(final_row[money]["amount"])) / (final_row[units] - returns)
            expected = ((final_row[units] - initial_row["customerReturns"]) * per_unit).quantize(
                Decimal("0.0001")
            )
            assert Decimal(str(initial_row[money]["amount"])) == expected
    # Under `unrestated_returns: final`, an unrestated day's first report is the final row.
    assert catalog.shape.retail.unrestated_returns == "final"
    last = catalog.shape.fixture_span.last_day
    document = json.loads(daily[last].data[0].body)
    source = json.loads((ROOT / "tests/fixtures" / _SALES).read_text())
    expected_rows = [row for row in source["salesByAsin"] if row["startDate"] == last.isoformat()]
    assert document["salesByAsin"] == expected_rows


def _retired_generation_days(fixture: str) -> set[date]:
    return {
        item.first + timedelta(days=offset)
        for item in _RETIRED_GENERATION_PARTITIONS
        if item.fixture == fixture
        for offset in range((item.last - item.first).days + 1)
    }


def _fold_stale(plan: tuple[LandedDelivery, ...]) -> dict[tuple[str, date], date]:
    """Model today's lake: each retired-generation day CURRENT at its stale watermark.

    Then fold the plan through the sidecar's rule (`semantic_sidecar._split_stale_stages`): a
    candidate strictly older than the incumbent is skipped; an equal or newer one publishes.
    Return the (fixture, day) keys that still hold the stale partition.
    """
    current: dict[tuple[str, date], tuple[date, bool]] = {
        (item.fixture, item.first + timedelta(days=offset)): (item.watermark, True)
        for item in _RETIRED_GENERATION_PARTITIONS
        for offset in range((item.last - item.first).days + 1)
    }
    for delivery in plan:
        fixture = delivery.fixture.split("#", 1)[0]
        watermark = delivery.manifest.source_watermark
        if watermark is None:
            continue
        manifest = delivery.manifest
        for offset in range((manifest.period_end - manifest.period_start).days + 1):
            key = (fixture, manifest.period_start + timedelta(days=offset))
            incumbent = current.get(key)
            if incumbent is None or not incumbent[0] > watermark:
                current[key] = (watermark, False)
    return {key: watermark for key, (watermark, stale) in current.items() if stale}


@pytest.mark.covers("ADR-086")
def test_no_stale_generation_partition_survives_on_any_span_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = {item.fixture for item in _RETIRED_GENERATION_PARTITIONS}
    assert {_SALES, _TRAFFIC} <= fixtures
    assert {
        "generated/amc-dsp-impressions.csv",
        "generated/amc-dsp-clicks.csv",
        "generated/amc-dsp-views.csv",
        "generated/amc-reach-frequency.csv",
        "generated/amc-attributed-conversion.csv",
        "generated/amc-deduplicated-temporal-attribution.csv",
        "generated/amc-retail-purchases.csv",
    } <= fixtures
    assert _fold_stale(_plan()) == {}
    # Positive control: today's lake state is detected. Without the retired floors the cadence
    # watermarks are older than the stale ones, so stale partitions survive on span days.
    monkeypatch.setattr(bundle_module, "_RETIRED_GENERATION_PARTITIONS", ())
    survivors = _fold_stale(plan_landings(ROOT))
    # The retail purchases contract's longer restatement window lets its later runs reach the
    # stale watermark on their own; every other retired fixture keeps stale days.
    assert {_SALES, _TRAFFIC, "generated/amc-dsp-impressions.csv"} <= {
        fixture for fixture, _day in survivors
    }
    assert (_SALES, date(2026, 9, 12)) in survivors
    assert (_SALES, date(2026, 9, 13)) not in survivors


@pytest.mark.covers("ADR-086")
def test_days_outside_the_retired_spans_keep_their_cadence_watermarks() -> None:
    sales_days = _retired_generation_days(_SALES)
    traps = {date(2026, 8, 5), date(2026, 8, 10)}
    for day, delivery in _vendor(_SALES, "restatement").items():
        if day in sales_days or day in traps:
            assert delivery.manifest.source_watermark is not None
            assert delivery.manifest.source_watermark >= day + timedelta(days=2)
        else:
            assert delivery.manifest.source_watermark == day + timedelta(days=2)
    for day, delivery in _vendor(_SALES, "daily-run").items():
        if day not in sales_days and day not in traps:
            assert delivery.manifest.source_watermark == day
    impressions = "generated/amc-dsp-impressions.csv"
    amc_days = _retired_generation_days(impressions)
    for delivery in _plan():
        if not delivery.fixture.startswith(f"{impressions}#daily-run="):
            continue
        run_day = date.fromisoformat(delivery.fixture.split("#daily-run=")[1].split("#")[0])
        manifest = delivery.manifest
        touches = any(
            manifest.period_start + timedelta(days=offset) in amc_days
            for offset in range((manifest.period_end - manifest.period_start).days + 1)
        )
        assert manifest.source_watermark == (
            max(run_day, date(2026, 9, 16)) if touches else run_day
        )


@pytest.mark.covers("ADR-047", "ADR-086")
def test_the_production_ingestion_rule_is_unchanged_by_the_retired_spans() -> None:
    # The sidecar's own ordering (ADR-047): a strictly older candidate is refused, an equal one
    # publishes. The retired spans only choose landing watermarks; they never reach this rule.
    scheme = RevisionSchemeKey("vendor_central.last_updated_date")
    stale = SourceRevision.dated(scheme=scheme, source_id="stale", observed_on=date(2026, 9, 15))
    older = SourceRevision.dated(scheme=scheme, source_id="older", observed_on=date(2026, 9, 14))
    equal = SourceRevision.dated(scheme=scheme, source_id="equal", observed_on=date(2026, 9, 15))
    assert stale.is_newer_than(older)
    assert not stale.is_newer_than(equal)
    sidecar = (ROOT / "src/adapters/jobs/ingest_delivery/semantic_sidecar.py").read_text()
    assert "and current.source_revision.is_newer_than(stage.candidate.source_revision)" in sidecar
    # Only the dev landing planner names the lists.
    holders = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "_RETIRED_GENERATION_PARTITIONS" in path.read_text()
        or "_RETIRED_TRAP_PARTITIONS" in path.read_text()
    }
    assert holders == {"src/adapters/jobs/land_fixtures/bundle.py"}


def _trap_keys(trap_dir: pathlib.Path) -> list[tuple[str, str, str, date, date]]:
    """(dataset, marketplace, sorted options, day, watermark) for every landed Vendor trap day."""
    keys: list[tuple[str, str, str, date, date]] = []
    for fixture in LANDABLE:
        if not fixture.startswith("traps/vendor-") or fixture.endswith(".jsonl"):
            continue
        manifest = json.loads(
            (trap_dir / pathlib.Path(fixture).name).with_suffix(".manifest.json").read_text()
        )
        document = cast(
            "dict[str, object]", json.loads((trap_dir / pathlib.Path(fixture).name).read_text())
        )
        days: set[str] = set()
        for rows in document.values():
            if isinstance(rows, list):
                for row in cast("list[object]", rows):
                    if isinstance(row, dict) and "startDate" in row:
                        days.add(str(cast("dict[str, object]", row)["startDate"]))
        for day in days:
            keys.append(
                (
                    manifest["source_dataset"],
                    manifest["marketplace"],
                    json.dumps(manifest["option_set"], sort_keys=True),
                    date.fromisoformat(day),
                    date.fromisoformat(manifest["source_watermark"]),
                )
            )
    return keys


def _trap_violations(trap_dir: pathlib.Path) -> list[tuple[str, date]]:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    final: dict[tuple[str, str, str, date], date] = {}
    for delivery in _plan():
        if not delivery.fixture.startswith("generated/vendor-"):
            continue
        manifest = delivery.manifest
        if manifest.source_watermark is None:
            continue
        key = (
            manifest.source_dataset,
            manifest.marketplace,
            json.dumps(manifest.option_set, sort_keys=True),
            manifest.period_start,
        )
        final[key] = max(final.get(key, manifest.source_watermark), manifest.source_watermark)
    return [
        (dataset, day)
        for dataset, marketplace, options, day, watermark in _trap_keys(trap_dir)
        if span.first_day <= day <= span.last_day
        and not final.get((dataset, marketplace, options, day), date.min) > watermark
    ]


@pytest.mark.covers("ADR-086")
def test_11_no_trap_hides_a_span_day_and_every_retired_trap_is_superseded() -> None:
    assert _trap_violations(ROOT / "tests/fixtures/traps") == []
    restated = _vendor(_SALES, "restatement")
    for item in _RETIRED_TRAP_PARTITIONS:
        for offset in range((item.last - item.first).days + 1):
            day = item.first + timedelta(days=offset)
            plan = restated if item.fixture == _SALES else _vendor(_TRAFFIC, "daily-run")
            watermark = plan[day].manifest.source_watermark
            assert watermark is not None and watermark > item.watermark, (item.fixture, day)


# --- Delivery classes, phase selections and the landing clock (design §2.2-§2.3, item 11) -----


def _phases(span_days: int, last_day: date) -> list[PlanSelection]:
    """Phase 0, phase A and the phase-B batches K..0 of design §2.3, as selections."""
    selections = [
        PlanSelection(frozenset({DeliveryClass.TRAP, DeliveryClass.WHOLE, DeliveryClass.SEAL})),
        PlanSelection(
            frozenset({DeliveryClass.CADENCE}), run_days_from=last_day - timedelta(days=55)
        ),
    ]
    batches = -(-(span_days - 56) // 60) - 1
    for k in range(batches, -1, -1):
        selections.append(
            PlanSelection(
                frozenset({DeliveryClass.CADENCE}),
                run_days_from=None
                if k == batches
                else last_day - timedelta(days=56 + 60 * (k + 1) - 1),
                run_days_to=last_day - timedelta(days=56 + 60 * k),
            )
        )
    return selections


def _assert_partition(plan: tuple[LandedDelivery, ...], last_day: date, span_days: int) -> None:
    index = {id(delivery): position for position, delivery in enumerate(plan)}
    chosen: list[int] = []
    for selection in _phases(span_days, last_day):
        chosen.extend(index[id(delivery)] for delivery in plan if selection.selects(delivery))
    assert sorted(chosen) == list(range(len(plan)))


@pytest.mark.covers("ADR-086")
def test_the_phases_partition_the_full_plan() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    plan = _plan()
    assert len(_phases(span.span_days, span.last_day)) == 9  # phase 0, A and 7 batches
    _assert_partition(plan, span.last_day, span.span_days)
    selected = plan_landings(ROOT, PlanSelection(frozenset({DeliveryClass.TRAP})))
    assert tuple(item.fixture for item in selected) == tuple(
        item.fixture for item in plan if item.delivery_class is DeliveryClass.TRAP
    )


@pytest.mark.covers("ADR-086")
def test_traps_are_only_in_phase_0_and_the_hierarchy_leads_every_seal_and_fact() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    plan = _plan()
    phases = _phases(span.span_days, span.last_day)
    traps = [item for item in plan if item.fixture.startswith("traps/")]
    assert traps and all(item.delivery_class is DeliveryClass.TRAP for item in traps)
    for item in traps:
        assert [index for index, phase in enumerate(phases) if phase.selects(item)] == [0]
    order = [item.fixture for item in plan]
    chunks = [
        position
        for position, name in enumerate(order)
        if name.startswith("generated/vendor-product-hierarchy.jsonl#chunk=")
    ]
    assert chunks and all(
        plan[position].delivery_class is DeliveryClass.WHOLE for position in chunks
    )
    hierarchy = max(chunks)
    assert all(
        position > hierarchy
        for position, item in enumerate(plan)
        if item.delivery_class is DeliveryClass.SEAL
        or (item.fixture.startswith("generated/amc-") and "#daily-run=" in item.fixture)
    )


@pytest.mark.covers("ADR-086")
def test_an_increment_selects_no_trap_or_seal() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    increment = PlanSelection(
        frozenset({DeliveryClass.CADENCE, DeliveryClass.WHOLE}), run_days_from=span.last_day
    )
    chosen = [item for item in _plan() if increment.selects(item)]
    assert chosen
    assert {item.delivery_class for item in chosen} <= {DeliveryClass.CADENCE, DeliveryClass.WHOLE}
    assert all(item.run_day is not None and item.run_day >= span.last_day for item in chosen)
    # The day's restating reports that fall due are the ones for last_day - 2.
    restating = {item.fixture for item in chosen if "#restatement=" in item.fixture}
    assert restating == {
        f"generated/vendor-sales-by-asin.json#restatement={span.last_day - timedelta(days=2)}"
    }


def _horizon_root(tmp_path: pathlib.Path, first_day: date) -> pathlib.Path:
    """Copy the governed sources with a catalog that starts on `first_day`."""
    import shutil  # noqa: PLC0415

    root = tmp_path / first_day.isoformat()
    shutil.copytree(ROOT / "contracts", root / "contracts")
    shutil.copytree(ROOT / "tests/fixtures/traps", root / "tests/fixtures/traps")
    catalog = root / "contracts/shared/synthetic-entity-catalog.yaml"
    text = catalog.read_text()
    assert 'first_day: "2025-07-01"' in text
    catalog.write_text(text.replace('first_day: "2025-07-01"', f'first_day: "{first_day}"'))
    return root


def _plan_for(  # type: ignore[no-untyped-def]
    root: pathlib.Path, first_day: date, monkeypatch: pytest.MonkeyPatch
):
    """Regenerate the fixtures under `root` (generator steps, no provenance gate) and plan."""
    from adapters.jobs.fixture_generation import __main__ as generator  # noqa: PLC0415
    from adapters.jobs.fixture_generation import catalog as catalog_module  # noqa: PLC0415
    from adapters.jobs.fixture_generation import shapes as shapes_module  # noqa: PLC0415

    monkeypatch.setattr(catalog_module, "FIRST_DAY", first_day)
    monkeypatch.setattr(shapes_module, "FIRST_DAY", first_day)
    fixtures = generator.build_fixtures(root)
    target = root / "tests/fixtures/generated"
    target.mkdir(parents=True)
    for fixture in fixtures:
        for name, content in generator._files_for(fixture).items():  # pyright: ignore[reportPrivateUsage]
            (target / name).write_text(content)
    return plan_landings(root)


@pytest.mark.covers("ADR-086")
def test_a_shorter_horizon_keeps_phase_0_and_lands_kept_days_identically(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    full_plan = _plan()  # built before FIRST_DAY is patched for the shorter catalog
    later = span.last_day - timedelta(days=56)  # the shortest legal horizon: 57 days
    short_plan = _plan_for(_horizon_root(tmp_path, later), later, monkeypatch)
    _assert_partition(short_plan, span.last_day, 57)
    phase_0 = _phases(57, span.last_day)[0]
    full_traps = [
        item.manifest.prefix for item in full_plan if item.delivery_class is DeliveryClass.TRAP
    ]
    assert [
        item.manifest.prefix for item in short_plan if item.delivery_class is DeliveryClass.TRAP
    ] == full_traps
    assert any(phase_0.selects(item) for item in short_plan)
    # Test 8c's delivery assertions: run days >= F + W - 1 are byte-identical except
    # retrieved_at, and no prefix of the later-start plan equals a prefix of the full plan.
    window = 30
    full = {
        item.fixture: item for item in full_plan if item.delivery_class is DeliveryClass.CADENCE
    }
    for item in short_plan:
        if item.delivery_class is not DeliveryClass.CADENCE or item.run_day is None:
            continue
        if not item.fixture.startswith("generated/amc-dsp-impressions.csv"):
            continue
        if item.run_day < later + timedelta(days=window - 1):
            continue
        twin = full[item.fixture]
        assert item.data[0].body == twin.data[0].body
        assert replace(item.manifest, retrieved_at=twin.manifest.retrieved_at) == twin.manifest
    full_prefixes = {item.manifest.prefix for item in full_plan}
    cadence = [item for item in short_plan if item.delivery_class is DeliveryClass.CADENCE]
    assert not full_prefixes & {item.manifest.prefix for item in cadence}


def _ignore(*_args: object, **_kwargs: object) -> None:
    return None


def _cli_arguments(*extra: str) -> tuple[str, ...]:
    return (
        "--profile",
        "explicit-profile",
        "--region",
        "us-east-1",
        "--expected-account",
        "000000000000",
        "--env",
        "dev",
        "--operation-id",
        "land-unit",
        "--stack",
        "Adlytics-DataV2-dev",
        "--bucket",
        "explicit-versioned-raw",
        *extra,
    )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--classes", "cadence,nonsense"), "names an unknown delivery class: nonsense"),
        (("--classes", ""), "names an unknown delivery class"),
        (("--run-days-from", "2026-10-01"), "require --classes"),
        (("--run-days-to", "2026-10-01"), "require --classes"),
        (("--fixtures", "generated/amc-retail-purchases.csv"), "require --classes"),
        (
            ("--classes", "cadence", "--fixtures", "generated/amc-nonsense.csv"),
            "--fixtures names an unknown fixture: generated/amc-nonsense.csv",
        ),
        (("--classes", "cadence", "--fixtures", ","), "--fixtures names an unknown fixture"),
        (
            (
                "--classes",
                "cadence",
                "--run-days-from",
                "2026-10-02",
                "--run-days-to",
                "2026-10-01",
            ),
            "--run-days-from is after --run-days-to",
        ),
    ],
)
def test_each_selection_argument_error_is_refused_before_any_aws_call(
    extra: tuple[str, ...], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        land_cli._parse_args(_cli_arguments(*extra))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_an_empty_selection_is_refused_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(land_cli, "validate_fixture_provenance", _ignore)
    with pytest.raises(SystemExit, match="the plan selection is empty"):
        land_cli.main(
            _cli_arguments("--classes", "cadence", "--run-days-from", "2099-01-01", "--dry-run")
        )


def _real_run(  # noqa: PLR0913 -- the clock, and which class and flags the run selects
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    *,
    selected: DeliveryClass = DeliveryClass.WHOLE,
    extra: tuple[str, ...] = (),
) -> tuple[int, list[int], list[BaseException]]:
    """Run `main` for real with every AWS edge stubbed; `puts` counts deliveries applied.

    A seal selection records a negative count when `apply_immutable` was given no guard.
    """
    deliveries = tuple(item for item in _plan() if item.delivery_class is selected)
    puts: list[int] = []

    class Session:
        _session = object()

        @staticmethod
        def client(service: str) -> object:
            if service == "sts":
                return SimpleNamespace(
                    get_caller_identity=lambda: {"Account": "000000000000", "Arn": "arn:test"}
                )
            return object()

    def apply(request: land_cli.ApplyLandFixturesRequest) -> int:
        guarded = selected is not DeliveryClass.SEAL or request.seal_guard is not None
        puts.append(len(request.deliveries) if guarded else -len(request.deliveries))
        return len(request.deliveries)

    def seal_reader(_session: object, _environment: str) -> object:
        return _ignore

    monkeypatch.setattr(land_cli, "validate_fixture_provenance", _ignore)

    def planned(_root: object, _selection: object = None) -> tuple[LandedDelivery, ...]:
        return deliveries

    def session(**_kwargs: object) -> Session:
        return Session()

    def no_tracing(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(land_cli, "plan_landings", planned)

    def with_backfill(_root: object) -> object:
        return _with_backfill()

    monkeypatch.setattr(land_cli, "plan_with_backfill", with_backfill)
    monkeypatch.setattr(land_cli.boto3, "Session", session)
    monkeypatch.setattr(land_cli, "configure_tracing", no_tracing)
    monkeypatch.setattr(land_cli, "verify_named_raw_bucket", _ignore)
    monkeypatch.setattr(land_cli, "apply_immutable", apply)
    errors: list[BaseException] = []

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    monkeypatch.setattr(land_cli, "report_operation", _ignore)
    monkeypatch.setattr(land_cli, "report_error", record_error)
    monkeypatch.setattr(land_cli, "flush_metrics", _ignore)
    monkeypatch.setattr(land_cli, "flush_traces", _ignore)
    monkeypatch.setattr(land_cli, "_seal_current_reader", seal_reader)
    result = land_cli.main(_cli_arguments("--classes", selected.value, *extra), now=lambda: now)
    return result, puts, errors


def test_a_real_run_refuses_before_the_selection_s_latest_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deliveries = tuple(item for item in _plan() if item.delivery_class is DeliveryClass.WHOLE)
    earliest = max(item.manifest.retrieved_at for item in deliveries)
    result, puts, errors = _real_run(monkeypatch, earliest - timedelta(seconds=1))
    assert (result, puts) == (1, [])
    assert [type(error) for error in errors] == [LandingError]
    assert str(errors[0]) == (
        f"plan retrieved_at {earliest.isoformat()} is in the future; "
        f"land after {earliest.isoformat()}"
    )
    # Positive control: at the instant itself the run reaches its first put.
    assert _real_run(monkeypatch, earliest) == (0, [len(deliveries)], [])


# --- The dimension seal and its sequence guard (design §2.3 "The seal", plan D1, item 12) ----

_SEAL_NAMESPACES = {
    "generated/amc-campaign-dsp.csv": "amc.dsp.campaign_id_string",
    "generated/amc-campaign-sponsored.csv": "amc.sponsored.campaign_id_string",
}
_SEAL_DATE = "source_observed_on"
_SEAL_KEY = "campaign_id_string"
_OLD_LAKE = (date(2026, 8, 10), date(2026, 9, 15))
_OLD_LAKE_REVISION = date(2026, 9, 16)
_RENAME = date(2026, 8, 12)


def _seal_contract(fixture: str) -> DataContract:
    contract_id = AMC_FIXTURE_CONTRACTS[fixture]
    return next(item for _, item in load_contracts(ROOT) if item.contract_id == contract_id)


@functools.cache
def _dimension_rows(fixture: str) -> tuple[dict[str, str], ...]:
    return tuple(csv.DictReader(StringIO((ROOT / "tests/fixtures" / fixture).read_text())))


@functools.cache
def _first_observed(fixture: str) -> dict[str, date]:
    """`d_c` for every campaign the committed result observes (design §2.5)."""
    found: dict[str, date] = {}
    for row in _dimension_rows(fixture):
        day = date.fromisoformat(row[_SEAL_DATE])
        found[row[_SEAL_KEY]] = min(found.get(row[_SEAL_KEY], day), day)
    return found


def _named(fixture: str) -> frozenset[str]:
    return frozenset(row[_SEAL_KEY] for row in _dimension_rows(fixture) if row["campaign"])


def _chunks_from(points: tuple[date, ...], last_day: date) -> tuple[tuple[date, ...], ...]:
    chain = tuple(points[index : index + 3] for index in range(0, len(points) - 1, 2))
    tail = tuple(
        (last_day - timedelta(days=offset + 1), last_day - timedelta(days=offset))
        for offset in range(28, -1, -1)
    )
    return chain + tail


def _expected_seal_points(fixture: str) -> tuple[date, ...]:
    """Recompute the chain points from the catalog's names and validity (test 11b)."""
    catalog = load_synthetic_entity_catalog(ROOT)
    span = catalog.shape.fixture_span
    cut = span.last_day - timedelta(days=29)
    namespace = _SEAL_NAMESPACES[fixture]
    changes = {
        moment
        for campaign in catalog.campaigns
        if namespace in campaign.source_aliases
        for name in campaign.names
        for moment in (name.valid_from, name.valid_to)
        if moment is not None
    } | set(_first_observed(fixture).values())
    return (
        span.first_day,
        *sorted(day for day in changes if span.first_day < day < cut),
        cut,
    )


def _expected_seal_chunks(fixture: str) -> tuple[tuple[date, ...], ...]:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    return _chunks_from(_expected_seal_points(fixture), span.last_day)


def _seals(fixture: str) -> list[LandedDelivery]:
    return [
        item
        for item in _plan()
        if item.delivery_class is DeliveryClass.SEAL and item.fixture.startswith(f"{fixture}#")
    ]


@pytest.mark.covers("ADR-086")
def test_11b_every_seal_delivery_has_the_planned_shape_rows_and_chain() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    cut = span.last_day - timedelta(days=29)
    for fixture in CAMPAIGN_DIMENSION_FIXTURES:
        seals = _seals(fixture)
        expected = _expected_seal_chunks(fixture)
        points = _expected_seal_points(fixture)
        # The count pin: ceil((|P| - 1) / 2) + 29 per fixture, 30 for each fixture today.
        assert len(seals) == len(expected) == -(-(len(points) - 1) // 2) + 29 == 30
        lines = (ROOT / "tests/fixtures" / fixture).read_text().splitlines(keepends=True)
        declared = bundle_module._declaration(  # pyright: ignore[reportPrivateUsage]
            (ROOT / "tests/fixtures" / fixture).with_suffix("").with_suffix(".manifest.json")
        )
        retrieved = datetime.fromisoformat(str(declared["retrieved_at"]).replace("Z", "+00:00"))
        for index, (delivery, days) in enumerate(zip(seals, expected, strict=True)):
            manifest = delivery.manifest
            assert manifest.source_watermark == span.last_day
            assert (manifest.period_start, manifest.period_end) == (days[0], days[-1])
            generation = load_synthetic_entity_catalog(ROOT).shape.seal_generation
            assert manifest.retrieved_at == retrieved + timedelta(
                seconds=(span.span_days + generation) * 64 + index
            )
            assert delivery.run_day is None
            chosen = tuple(day.isoformat() for day in days)
            # The corpus's rows, each carrying this delivery's own AMC window.
            corpus_window = f"{span.first_day}T00:00:00.000Z,{span.last_day + timedelta(1)}T"
            own_window = f"{days[0]}T00:00:00.000Z,{days[-1] + timedelta(1)}T"
            assert delivery.data[0].body.decode() == lines[0] + "".join(
                line.replace(corpus_window, own_window, 1)
                for line in lines[1:]
                if line.startswith(tuple(f"{day}," for day in chosen))
            )
            populated = {
                row[_SEAL_DATE] for row in csv.DictReader(StringIO(delivery.data[0].body.decode()))
            }
            assert populated == set(chosen)
            assert len(populated) <= MAX_POPULATED_BUCKETS_PER_DELIVERY
            if index:
                assert days[0] == expected[index - 1][-1]
        ends = {days[-1] for days in expected}
        assert {cut + timedelta(days=offset) for offset in range(30)} <= ends
    assert _expected_seal_points("generated/amc-campaign-dsp.csv") == (
        span.first_day,
        _RENAME,
        cut,
    )


def _fictional_dimension(
    shape_days: int, name_for: Callable[[int], str], late_from: int | None = None
) -> str:
    header = (ROOT / "tests/fixtures/generated/amc-campaign-dsp.csv").read_text().splitlines()[0]
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header.split(","))
    for offset in range(shape_days):
        day = (span.first_day + timedelta(days=offset)).isoformat()
        writer.writerow([day, "1", name_for(offset), "9", "Adv", "US", "false", "", "100"])
        if late_from is not None and offset >= late_from:
            writer.writerow([day, "2", "Late", "9", "Adv", "US", "false", "", "100"])
    return buffer.getvalue()


def _fictional_seal(text: str) -> tuple[tuple[str, str, dict[str, object]], ...]:
    fixture = "generated/amc-campaign-dsp.csv"
    shape = load_synthetic_entity_catalog(ROOT).shape
    declaration = bundle_module._declaration(  # pyright: ignore[reportPrivateUsage]
        ROOT / "tests/fixtures/generated/amc-campaign-dsp.manifest.json"
    )
    return bundle_module._dimension_seal_runs(  # pyright: ignore[reportPrivateUsage]
        bundle_module._SealRequest(  # pyright: ignore[reportPrivateUsage]
            fixture, text, declaration, _seal_contract(fixture), shape
        )
    )


def _chain_days(runs: tuple[tuple[str, str, dict[str, object]], ...]) -> tuple[date, ...]:
    return tuple(
        sorted(
            {
                date.fromisoformat(row[_SEAL_DATE])
                for _, text, _ in runs[:-29]
                for row in csv.DictReader(StringIO(text))
            }
        )
    )


@pytest.mark.covers("ADR-086")
def test_11b_a_day_40_rename_and_a_day_100_first_observation_enter_the_chain() -> None:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    cut = span.last_day - timedelta(days=29)
    day_40 = span.first_day + timedelta(days=40)
    day_100 = span.first_day + timedelta(days=100)
    renamed = _fictional_seal(
        _fictional_dimension(span.span_days, lambda offset: "A" if offset < 40 else "A2", 100)
    )
    assert _chain_days(renamed) == (span.first_day, day_40, day_100, cut)
    assert len(renamed) == 2 + 29  # (first, 40, 100) then (100, cut)
    # Positive control: an unchanged campaign set keeps only first_day and the cut.
    steady = _fictional_seal(_fictional_dimension(span.span_days, lambda _offset: "A"))
    assert _chain_days(steady) == (span.first_day, cut)
    # The stride envelope: 67 daily renames give 69 points, ceil(68 / 2) + 29 = 63 deliveries,
    # which plan; 69 renames give 71 points and 64 deliveries, which the plan refuses before
    # any write.
    assert MAX_SEAL_DELIVERIES_PER_FIXTURE == 63
    edge = _fictional_seal(
        _fictional_dimension(span.span_days, lambda offset: f"A{min(offset, 67)}")
    )
    assert len(edge) == 63
    with pytest.raises(LandingError, match=r"needs 64 deliveries, above the stride envelope"):
        _fictional_seal(_fictional_dimension(span.span_days, lambda offset: f"A{min(offset, 69)}"))


@dataclasses.dataclass(slots=True)
class _Current:
    watermark: date
    rows: tuple[CanonicalRow, ...]
    publication: int


class _FoldStore:
    """A double of one campaign-dimension scope's CURRENT partitions (test 11c).

    It applies the two cited rules: the fold base is the latest CURRENT partition whose bucket
    ends at or before the delivery's first bucket (`_previous_history_rows`), and a candidate is
    skipped only when the incumbent's watermark is strictly newer (ties publish). The fold
    itself is the real `build_dimension_history`, one bucket at a time, ascending.
    """

    def __init__(self, fixture: str) -> None:
        contract = _seal_contract(fixture)
        key, tracked = bundle_module._seal_columns(contract)  # pyright: ignore[reportPrivateUsage]
        targets = {item.source_path: item.target_column for item in contract.fields}
        self._key = key
        self._tracked = tuple((source, targets[source]) for source in tracked)
        self.current: dict[date, _Current] = {}
        self.published = 0

    def observations(self, rows: Iterable[dict[str, str]]) -> dict[date, tuple[CanonicalRow, ...]]:
        found: dict[date, list[CanonicalRow]] = {}
        for row in rows:
            canonical: CanonicalRow = tuple(
                sorted(
                    (
                        ("campaign_key", row[self._key]),
                        *((target, row[source] or None) for source, target in self._tracked),
                    )
                )
            )
            found.setdefault(date.fromisoformat(row[_SEAL_DATE]), []).append(canonical)
        return {day: tuple(items) for day, items in found.items()}

    def base(self, first: date) -> tuple[CanonicalRow, ...]:
        eligible = [day for day in self.current if day <= first]
        return self.current[max(eligible)].rows if eligible else ()

    def prepare(self, buckets: dict[date, tuple[CanonicalRow, ...]]) -> tuple[CanonicalRow, ...]:
        days = sorted(buckets)
        rows = self.base(days[0])
        for day in days:
            rows = build_dimension_history(
                DimensionHistoryRequest(
                    previous=rows,
                    observations=buckets[day],
                    business_key=("campaign_key",),
                    tracked_attributes=tuple(target for _, target in self._tracked),
                    boundary=datetime.combine(day, time.min, tzinfo=UTC),
                )
            ).rows
        return rows

    def write(self, last: date, watermark: date, rows: tuple[CanonicalRow, ...]) -> bool:
        incumbent = self.current.get(last)
        if incumbent is not None and incumbent.watermark > watermark:
            return False
        self.published += 1
        self.current[last] = _Current(watermark, rows, self.published)
        return True

    def deliver(self, buckets: dict[date, tuple[CanonicalRow, ...]], watermark: date) -> bool:
        incumbent = self.current.get(max(buckets))
        if incumbent is not None and incumbent.watermark > watermark:
            return False  # skipped, so folding it first would change nothing
        return self.write(max(buckets), watermark, self.prepare(buckets))

    def buckets(self, delivery: LandedDelivery) -> dict[date, tuple[CanonicalRow, ...]]:
        return self.observations(csv.DictReader(StringIO(delivery.data[0].body.decode())))


def _seed_old_lake(store: _FoldStore, fixture: str) -> None:
    """Model the old generation's dimension partitions, the HEAD 237df7c landing's result.

    The 2026-10-05 lake read (task evidence state-before.md, step 6) found every bucket
    2026-08-10..09-15 CURRENT at revision 2026-09-16. The HEAD 237df7c rows on those days
    carry the same tracked values as the committed rows (compared when this test was
    written; recorded in the item 12 commit), so the committed rows stand in. Each bucket's
    history is folded from 2026-08-10, as that generation's oldest-first landing left it.
    """
    observed = store.observations(
        row
        for row in _dimension_rows(fixture)
        if _OLD_LAKE[0] <= date.fromisoformat(row[_SEAL_DATE]) <= _OLD_LAKE[1]
    )
    for day in sorted(observed):
        # One bucket at a time from the previous old partition, so history opens on 08-10.
        store.write(day, _OLD_LAKE_REVISION, store.prepare({day: observed[day]}))


def _land_increments(store: _FoldStore, fixture: str, last_day: date) -> None:
    """Three later daily runs; days after `last_day` re-observe `last_day`'s rows unchanged."""
    observed = store.observations(_dimension_rows(fixture))
    for k in (1, 2, 3):
        run_day = last_day + timedelta(days=k)
        days = [run_day - timedelta(days=offset) for offset in range(29, -1, -1)]
        for start in range(0, len(days), 3):
            store.deliver(
                {day: observed[min(day, last_day)] for day in days[start : start + 3]}, run_day
            )


def _value(row: CanonicalRow, column: str) -> object:
    return dict(row)[column]


def _earliest(rows: tuple[CanonicalRow, ...]) -> dict[str, date]:
    found: dict[str, date] = {}
    for row in rows:
        key = cast("str", _value(row, "campaign_key"))
        day = cast("datetime", _value(row, "valid_from")).date()
        found[key] = min(found.get(key, day), day)
    return found


def _d_c_violations(store: _FoldStore, fixture: str, bucket: date) -> list[str]:
    """Named campaigns observed by `bucket` whose earliest `valid_from` is not `d_c`."""
    earliest = _earliest(store.current[bucket].rows)
    first = _first_observed(fixture)
    return sorted(
        key for key in _named(fixture) if first[key] <= bucket and earliest.get(key) != first[key]
    )


def _dimension_order(*, sealed: bool) -> list[LandedDelivery]:
    """Phase 0, phase A, then the phase-B batches K..0: the campaign-dimension deliveries."""
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    plan = _plan()
    ordered: list[LandedDelivery] = []
    for phase in _phases(span.span_days, span.last_day):
        ordered.extend(
            item
            for item in plan
            if phase.selects(item)
            and item.fixture.split("#", 1)[0] in CAMPAIGN_DIMENSION_FIXTURES
            and (sealed or item.delivery_class is not DeliveryClass.SEAL)
        )
    return ordered


@pytest.mark.covers("ADR-086")
def test_11c_the_fold_double_takes_the_base_previous_history_rows_takes() -> None:
    contract = _seal_contract("generated/amc-campaign-dsp.csv")
    first = date(2026, 1, 10)
    for days, expected in (
        ((), None),
        ((date(2026, 1, 5),), date(2026, 1, 5)),
        ((date(2026, 1, 5), date(2026, 1, 12)), date(2026, 1, 5)),
    ):
        published = tuple(
            SimpleNamespace(
                partition=SimpleNamespace(bucket=SimpleNamespace(window=SimpleNamespace(end=day)))
            )
            for day in days
        )

        def current_for_scope(
            _scope: object, found: tuple[SimpleNamespace, ...] = published
        ) -> tuple[SimpleNamespace, ...]:
            return found

        def rows(selected: Any) -> tuple[CanonicalRow, ...]:
            return ((("bucket", cast("date", selected.partition.bucket.window.end)),),)

        operation = SimpleNamespace(
            item=SimpleNamespace(load=SimpleNamespace(contract=contract)),
            partition=SimpleNamespace(scope="dim_campaign"),
            tooling=SimpleNamespace(
                publications=SimpleNamespace(current_for_scope=current_for_scope),
                canonical_reader=SimpleNamespace(rows=rows),
            ),
        )
        real = semantic_sidecar._previous_history_rows(  # pyright: ignore[reportPrivateUsage]
            cast("Any", operation), cast("Any", SimpleNamespace(window=SimpleNamespace(end=first)))
        )
        store = _FoldStore("generated/amc-campaign-dsp.csv")
        store.current = {day: _Current(date.min, ((("bucket", day),),), 0) for day in days}
        assert store.base(first) == real
        assert real == (() if expected is None else ((("bucket", expected),),))


@pytest.mark.covers("ADR-086")
def test_11c_the_planned_order_keeps_full_history_and_the_unsealed_order_does_not() -> None:
    catalog = load_synthetic_entity_catalog(ROOT)
    span = catalog.shape.fixture_span
    latest = span.last_day + timedelta(days=3)
    renamed = next(
        campaign.source_aliases["amc.dsp.campaign_id_string"]
        for campaign in catalog.campaigns
        if len(campaign.names) == 2
    )
    for sealed in (True, False):
        order = _dimension_order(sealed=sealed)
        for fixture in CAMPAIGN_DIMENSION_FIXTURES:
            store = _FoldStore(fixture)
            _seed_old_lake(store, fixture)
            for delivery in order:
                if delivery.fixture.startswith(f"{fixture}#"):
                    watermark = delivery.manifest.source_watermark
                    assert watermark is not None
                    store.deliver(store.buckets(delivery), watermark)
            _land_increments(store, fixture, span.last_day)
            assert max(store.current) == latest
            if not sealed:
                # Positive control: without the seal the old generation's base survives.
                assert min(_earliest(store.current[latest].rows).values()) == _OLD_LAKE[0]
                assert _d_c_violations(store, fixture, latest)
                continue
            for bucket in sorted(store.current):
                if bucket >= latest - timedelta(days=29):
                    assert _d_c_violations(store, fixture, bucket) == [], (fixture, bucket)
            if fixture == "generated/amc-campaign-dsp.csv":
                versions = sorted(
                    (
                        cast("datetime", _value(row, "valid_from")).date(),
                        cast("datetime", _value(row, "valid_to")),
                    )
                    for row in store.current[latest].rows
                    if _value(row, "campaign_key") == renamed
                )
                assert versions == [
                    (span.first_day, datetime.combine(_RENAME, time.min, tzinfo=UTC)),
                    (_RENAME, OPEN_ENDED),
                ]


@dataclasses.dataclass(slots=True)
class _Job:
    delivery: LandedDelivery
    prepare_at: int
    write_at: int
    age: int = 0
    folded: tuple[CanonicalRow, ...] | None = None


class _Pipeline:
    """An asynchronous processor model of ingestion for seal deliveries (test 11d).

    A put manifest queues its delivery. Two workers run at once. A worker prepares (reads its
    fold base from the store and folds) at its scheduled step and writes at a later one; each
    step of the model is one guard poll. A failed delivery is dropped and never written.
    """

    def __init__(
        self,
        stores: dict[str, _FoldStore],
        schedule: Callable[[int], tuple[int, int]],
        failed: frozenset[int] = frozenset(),
    ) -> None:
        self.stores = stores
        self.schedule = schedule
        self.failed = failed
        self.queue: list[_Job] = []
        self.running: list[_Job] = []
        self.arrived = 0

    def arrive(self, delivery: LandedDelivery) -> None:
        prepare_at, write_at = self.schedule(self.arrived)
        if self.arrived not in self.failed:
            self.queue.append(_Job(delivery, prepare_at, write_at))
        self.arrived += 1

    def step(self) -> None:
        while len(self.running) < 2 and self.queue:
            self.running.append(self.queue.pop(0))
        for job in list(self.running):
            store = self.stores[job.delivery.fixture.split("#", 1)[0]]
            job.age += 1
            if job.age == job.prepare_at:
                job.folded = store.prepare(store.buckets(job.delivery))
            if job.age >= job.write_at and job.folded is not None:
                watermark = job.delivery.manifest.source_watermark
                assert watermark is not None
                store.write(job.delivery.manifest.period_end, watermark, job.folded)
                self.running.remove(job)

    def drain(self) -> None:
        while self.queue or self.running:
            self.step()

    def read_current(self, delivery: LandedDelivery) -> SuccessfulPublication | None:
        store = self.stores[delivery.fixture.split("#", 1)[0]]
        current = store.current.get(delivery.manifest.period_end)
        if current is None:
            return None
        return cast(
            "SuccessfulPublication",
            SimpleNamespace(
                source_revision=SimpleNamespace(
                    observed_at=datetime.combine(current.watermark, time.min, tzinfo=UTC)
                ),
                publication_id=SimpleNamespace(value=f"publication-{current.publication}"),
                published_at=datetime(2026, 1, 1, tzinfo=UTC)
                + timedelta(seconds=current.publication),
            ),
        )


def _old_lake_stores() -> dict[str, _FoldStore]:
    stores = {fixture: _FoldStore(fixture) for fixture in CAMPAIGN_DIMENSION_FIXTURES}
    for fixture, store in stores.items():
        _seed_old_lake(store, fixture)
    return stores


def _all_seals() -> tuple[LandedDelivery, ...]:
    return tuple(item for item in _plan() if item.delivery_class is DeliveryClass.SEAL)


def _seal_violations(stores: dict[str, _FoldStore]) -> list[str]:
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    return [
        f"{fixture}:{key}"
        for fixture, store in stores.items()
        for key in _d_c_violations(store, fixture, span.last_day)
    ]


class _FakeS3:
    def __init__(self, pipeline: _Pipeline, deliveries: tuple[LandedDelivery, ...]) -> None:
        self.pipeline = pipeline
        self.markers = {item.marker.key: item for item in deliveries}
        self.puts: list[str] = []

    def put_object(self, **request: object) -> dict[str, str]:
        key = cast("str", request["Key"])
        self.puts.append(key)
        if key in self.markers:
            self.pipeline.arrive(self.markers[key])
        return {"VersionId": f"version-{len(self.puts)}"}


class _FakeClock:
    def __init__(self, pipeline: _Pipeline) -> None:
        self.pipeline = pipeline
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.pipeline.step()

    def monotonic(self) -> float:
        return self.now


def _guarded_landing(
    pipeline: _Pipeline, deliveries: tuple[LandedDelivery, ...]
) -> tuple[_FakeS3, list[land_job.SealWait]]:
    """Land through the real `apply_immutable` and the real guard, with fakes at the edges."""
    s3 = _FakeS3(pipeline, deliveries)
    clock = _FakeClock(pipeline)
    waits: list[land_job.SealWait] = []
    land_job.apply_immutable(
        land_job.ApplyLandFixturesRequest(
            client=cast("Any", s3),
            bucket="explicit-versioned-raw",
            deliveries=deliveries,
            operation_id="land-unit",
            execution_id="execution",
            predecessor_execution_id=None,
            plan_digest="sha256:unit",
            progress=_ignore,
            cancelled=lambda: False,
            historical_policy=_with_backfill()[1],
            seal_guard=land_job.SealSequenceGuard(
                read_current=pipeline.read_current,
                report=waits.append,
                sleep=clock.sleep,
                clock=clock.monotonic,
            ),
        )
    )
    return s3, waits


@pytest.mark.covers("ADR-086")
def test_11d_unguarded_concurrent_or_reversed_seal_processing_breaks_the_history() -> None:
    seals = _all_seals()
    # (i) a concurrent same-base pair: the chain and the first tail both prepare on step 1,
    # before either writes, so the tail folds from the old generation's partition.
    stores = _old_lake_stores()
    concurrent = _Pipeline(stores, lambda _index: (1, 2))
    for delivery in seals:
        concurrent.arrive(delivery)
    concurrent.drain()
    assert _seal_violations(stores)
    # ... and a reversed pair: the first tail is processed before the chain.
    stores = _old_lake_stores()
    reversed_pair = _Pipeline(stores, lambda _index: (1, 1))
    for fixture in CAMPAIGN_DIMENSION_FIXTURES:
        own = [item for item in seals if item.fixture.startswith(f"{fixture}#")]
        for delivery in (own[1], own[0], *own[2:]):
            reversed_pair.arrive(delivery)
            reversed_pair.drain()
    assert _seal_violations(stores)
    # Control: the same model processing them one at a time, in order, keeps the history.
    stores = _old_lake_stores()
    serial = _Pipeline(stores, lambda _index: (1, 1))
    for delivery in seals:
        serial.arrive(delivery)
        serial.drain()
    assert _seal_violations(stores) == []


_SCHEDULES: tuple[Callable[[int], tuple[int, int]], ...] = (
    *(
        (lambda _index, prepare=prepare, write=write: (prepare, prepare + write))
        for prepare in (1, 2, 3)
        for write in (0, 1, 2)
    ),
    lambda index: (index % 3 + 1, index % 3 + 1 + index * 7 % 3),
    lambda index: (3 - index % 3, 3 - index % 3 + index % 2),
)
"""Every interleaving the model enumerates: fixed prepare and write delays, and two mixes."""


@pytest.mark.covers("ADR-086")
@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_11d_the_guarded_loop_keeps_the_history_under_every_interleaving(
    schedule: Callable[[int], tuple[int, int]],
) -> None:
    seals = _all_seals()
    stores = _old_lake_stores()
    pipeline = _Pipeline(stores, schedule)
    s3, waits = _guarded_landing(pipeline, seals)
    assert _seal_violations(stores) == []
    assert [wait.fixture for wait in waits] == [item.fixture for item in seals]
    assert all(wait.polls == schedule(index)[1] for index, wait in enumerate(waits))
    assert len(s3.puts) == 2 * len(seals)


@pytest.mark.covers("ADR-086")
def test_11d_a_timeout_stops_the_landing_with_no_further_put() -> None:
    seals = _all_seals()
    pipeline = _Pipeline(_old_lake_stores(), lambda _index: (1, 1), failed=frozenset({0}))
    s3 = _FakeS3(pipeline, seals)
    clock = _FakeClock(pipeline)
    with pytest.raises(LandingError, match=r"was not CURRENT with watermark .* after 2880 s"):
        land_job.apply_immutable(
            land_job.ApplyLandFixturesRequest(
                client=cast("Any", s3),
                bucket="explicit-versioned-raw",
                deliveries=seals,
                operation_id="land-unit",
                execution_id="execution",
                predecessor_execution_id=None,
                plan_digest="sha256:unit",
                progress=_ignore,
                cancelled=lambda: False,
                historical_policy=_with_backfill()[1],
                seal_guard=land_job.SealSequenceGuard(
                    read_current=pipeline.read_current,
                    report=_ignore,
                    sleep=clock.sleep,
                    clock=clock.monotonic,
                ),
            )
        )
    assert s3.puts == [seals[0].data[0].key, seals[0].marker.key]
    assert clock.now == land_job.SEAL_TIMEOUT_SECONDS == 2880.0


@functools.cache
def _seal_plan(generation: int) -> tuple[LandedDelivery, ...]:
    shape = replace(load_synthetic_entity_catalog(ROOT).shape, seal_generation=generation)
    context = bundle_module._plan_context(ROOT, shape)  # pyright: ignore[reportPrivateUsage]
    return tuple(
        item
        for fixture in CAMPAIGN_DIMENSION_FIXTURES
        for item in bundle_module._plan_fixture(context, fixture)  # pyright: ignore[reportPrivateUsage]
        if item.delivery_class is DeliveryClass.SEAL
    )


@pytest.mark.covers("ADR-086")
def test_11d_a_failed_seal_recovers_under_the_next_generation_at_fresh_prefixes() -> None:
    # The committed plan is generation 2 since the 2026-10-06 recoveries; the model replays 0 -> 1.
    assert _seal_plan(2) == _all_seals()
    failed_at = 10  # the tenth tail delivery of the DSP seal
    stores = _old_lake_stores()
    pipeline = _Pipeline(stores, lambda _index: (1, 2), failed=frozenset({failed_at}))
    with pytest.raises(LandingError, match="seal_generation"):
        _guarded_landing(pipeline, _seal_plan(0))
    assert pipeline.arrived == failed_at + 1
    recovery = _Pipeline(stores, lambda _index: (2, 3))
    s3, waits = _guarded_landing(recovery, _seal_plan(1))
    assert _seal_violations(stores) == []
    assert len(waits) == len(_seal_plan(1)) and len(s3.puts) == 2 * len(_seal_plan(1))
    prefixes = [
        {item.manifest.prefix for item in _seal_plan(generation)} for generation in (0, 1, 2)
    ]
    cadence = {
        item.manifest.prefix for item in _plan() if item.delivery_class is not DeliveryClass.SEAL
    }
    assert all(len(found) == len(_seal_plan(0)) for found in prefixes)
    for index, found in enumerate(prefixes):
        assert not found & cadence
        for other in prefixes[index + 1 :]:
            assert not found & other


def test_apply_refuses_seal_deliveries_without_the_guard_before_any_put() -> None:
    seals = _all_seals()
    pipeline = _Pipeline(_old_lake_stores(), lambda _index: (1, 1))
    s3 = _FakeS3(pipeline, seals)
    with pytest.raises(LandingError, match="only through the sequence guard"):
        land_job.apply_immutable(
            land_job.ApplyLandFixturesRequest(
                client=cast("Any", s3),
                bucket="explicit-versioned-raw",
                deliveries=seals[:1],
                operation_id="land-unit",
                execution_id="execution",
                predecessor_execution_id=None,
                plan_digest="sha256:unit",
                progress=_ignore,
                cancelled=lambda: False,
                historical_policy=_with_backfill()[1],
            )
        )
    assert s3.puts == []


_CHECKED = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _settled(**changes: object) -> dict[str, object]:
    return {
        "report": "pipeline-settled",
        "version": 1,
        "environment": "dev",
        "account": "000000000000",
        "checked_at": "2026-10-05T12:00:00Z",
        "settled": True,
        "findings": [],
        **changes,
    }


@pytest.mark.parametrize(
    ("document", "now", "message"),
    [
        (_settled(), _CHECKED + timedelta(minutes=15, seconds=1), "not within 15 minutes"),
        (_settled(), _CHECKED - timedelta(seconds=1), "not within 15 minutes"),
        (_settled(settled=False, findings=["bake queue holds 3"]), _CHECKED, "not settled"),
        (_settled(findings=["dlq rose"]), _CHECKED, "not settled"),
        (_settled(environment="prod"), _CHECKED, "another environment or account"),
        (_settled(account="111111111111"), _CHECKED, "another environment or account"),
        (_settled(version=2), _CHECKED, "not a pipeline-settled v1 report"),
        (_settled(checked_at="yesterday"), _CHECKED, "not an instant"),
        (["settled"], _CHECKED, "must be a JSON object"),
    ],
)
def test_a_stale_unsettled_or_foreign_settled_report_is_refused(
    document: object, now: datetime, message: str
) -> None:
    with pytest.raises(LandingError, match=message):
        land_job.check_settled_report(document, environment="dev", account="000000000000", now=now)
    # Positive control: a settled report from this account, 15 minutes old, is accepted.
    land_job.check_settled_report(
        _settled(), environment="dev", account="000000000000", now=_CHECKED + timedelta(minutes=15)
    )


def test_a_seal_landing_needs_a_fresh_settled_report_before_any_put(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    earliest = max(item.manifest.retrieved_at for item in _all_seals())
    result, puts, errors = _real_run(monkeypatch, earliest, selected=DeliveryClass.SEAL)
    assert (result, puts) == (1, [])
    assert [type(error) for error in errors] == [LandingError]
    assert "needs --settled-report" in str(errors[0])
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(_settled(checked_at=(earliest - timedelta(minutes=16)).isoformat()))
    )
    result, puts, errors = _real_run(
        monkeypatch, earliest, selected=DeliveryClass.SEAL, extra=("--settled-report", str(stale))
    )
    assert (result, puts) == (1, [])
    assert "not within 15 minutes" in str(errors[0])
    # Positive control: a fresh report reaches apply, with the sequence guard attached.
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(_settled(checked_at=(earliest - timedelta(minutes=1)).isoformat())))
    assert _real_run(
        monkeypatch, earliest, selected=DeliveryClass.SEAL, extra=("--settled-report", str(fresh))
    ) == (0, [len(_all_seals())], [])


# --- The product-hierarchy split (ADR-086 amendment 2026-10-06) -------------------------------

_HIERARCHY = "generated/vendor-product-hierarchy.jsonl"
FIXTURES = ROOT / "tests/fixtures"


def _hierarchy_term(text: str) -> int:
    """Measure the source-aligned unit's clean-attempt term `M(u) + 1` for one hierarchy body.

    `M(u)` is measured with the deployed packer and §11 byte bound over the deployed table,
    every column a 64-byte parameter, so the term does not under-count.
    """
    from adapters.outbound.athena.semantic_storage import (  # noqa: PLC0415
        PhysicalRecord,
        merge_batches,
    )
    from adapters.outbound.athena.start_envelope import envelope_fits  # noqa: PLC0415
    from adapters.outbound.v2_storage_catalog import load_v2_storage_catalog  # noqa: PLC0415
    from domain.operations.statement_protocol import MAX_STATEMENT_BYTES  # noqa: PLC0415

    table = load_v2_storage_catalog(ROOT / "contracts/generated/v2-tables.json").source_by_contract[
        "vendor-product-hierarchy"
    ]
    row: PhysicalRecord = {column.name: "x" * 64 for column in table.columns}
    records = tuple(dict(row) for _ in text.splitlines())
    fits = envelope_fits("adlytics-dev-v2-batch", "adlytics_dev_source_v2", MAX_STATEMENT_BYTES)
    return len(merge_batches(table, records, fits)) + 1


@pytest.mark.covers("ADR-086")
def test_every_hierarchy_chunk_fits_the_unit_cap_and_a_larger_body_does_not() -> None:
    from domain.operations.projection_feasibility import (  # noqa: PLC0415
        MAX_ACCEPTED_ATTEMPTS_PER_UNIT,
    )

    chunks = [item for item in _plan() if item.fixture.startswith(f"{_HIERARCHY}#chunk=")]
    assert len(chunks) == 2
    for chunk in chunks:
        assert _hierarchy_term(chunk.data[0].body.decode()) <= MAX_ACCEPTED_ATTEMPTS_PER_UNIT
    # Negative control: the cap still bites. The unsplit 463-row fixture was refused live on
    # 2026-10-06 under the since-removed 900-parameter bound; byte-bound packing fits it, so the
    # control is a body three times its size.
    whole = (FIXTURES / _HIERARCHY).read_text()
    assert _hierarchy_term(whole) <= MAX_ACCEPTED_ATTEMPTS_PER_UNIT
    assert _hierarchy_term(whole * 3) > MAX_ACCEPTED_ATTEMPTS_PER_UNIT


@pytest.mark.covers("ADR-086")
def test_the_hierarchy_chunks_are_whole_complete_snapshots_covering_every_row_once() -> None:
    plan = _plan()
    chunks = [item for item in plan if item.fixture.startswith(f"{_HIERARCHY}#chunk=")]
    assert all(item.delivery_class is DeliveryClass.WHOLE for item in chunks)
    assert all(item.run_day == item.manifest.period_end for item in chunks)
    assert "".join(item.data[0].body.decode() for item in chunks) == (
        (FIXTURES / _HIERARCHY).read_text()
    )
    span = load_synthetic_entity_catalog(ROOT).shape.fixture_span
    assert chunks[0].manifest.period_start == span.first_day
    assert chunks[-1].manifest.period_end == span.last_day
    for earlier, later in itertools.pairwise(chunks):
        assert later.manifest.period_start == earlier.manifest.period_end + timedelta(days=1)
    products = {
        json.loads(line)["asin"] for line in (FIXTURES / _HIERARCHY).read_text().splitlines()
    }
    for chunk in chunks:
        assert {
            json.loads(line)["asin"] for line in chunk.data[0].body.decode().splitlines()
        } == products
    assert len({item.manifest.prefix for item in chunks}) == len(chunks)
    # Both chunks land in phase 0, ahead of every seal and AMC fact; an increment takes the last.
    seals_and_facts = [
        position
        for position, item in enumerate(plan)
        if item.delivery_class is DeliveryClass.SEAL
        or (item.fixture.startswith("generated/amc-") and "#daily-run=" in item.fixture)
    ]
    assert max(plan.index(item) for item in chunks) < min(seals_and_facts)
    increment = PlanSelection(
        frozenset({DeliveryClass.CADENCE, DeliveryClass.WHOLE}), run_days_from=span.last_day
    )
    assert [item for item in chunks if increment.selects(item)] == [chunks[-1]]


@pytest.mark.covers("ADR-086")
def test_the_split_leaves_production_ingestion_unchanged() -> None:
    import ast  # noqa: PLC0415

    from domain.operations.projection_feasibility import (  # noqa: PLC0415
        MAX_ACCEPTED_ATTEMPTS_PER_PLAN,
        MAX_ACCEPTED_ATTEMPTS_PER_UNIT,
    )

    # The reviewed caps are what they were; the dev planner adapts to them, not the reverse.
    assert (MAX_ACCEPTED_ATTEMPTS_PER_UNIT, MAX_ACCEPTED_ATTEMPTS_PER_PLAN) == (10, 32)
    # No ingestion, Stage-1 or writer module imports the dev landing planner.
    roots = [
        ROOT / "src/adapters/inbound/ingestion_handlers",
        ROOT / "src/adapters/jobs/ingest_delivery",
    ]
    for path in (file for root in roots for file in root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("adapters.jobs.land_fixtures"), path
    # A fixture at or under the chunk size stays one delivery, as before the split.
    small = "".join(
        line + "\n"
        for line in (FIXTURES / _HIERARCHY)
        .read_text()
        .splitlines()[: bundle_module.HIERARCHY_ROWS_PER_DELIVERY]
    )
    runs = bundle_module._whole_chunk_runs(  # pyright: ignore[reportPrivateUsage]
        bundle_module._SourceRunsRequest(  # pyright: ignore[reportPrivateUsage]
            _HIERARCHY,
            small,
            {"period_start": "2025-07-01"},
            None,
            load_synthetic_entity_catalog(ROOT).shape,
        )
    )
    assert [run[0] for run in runs] == [_HIERARCHY]


@pytest.mark.covers("ADR-086")
def test_the_seal_timeout_outlasts_every_redelivery_the_semantic_write_queue_allows() -> None:
    import re  # noqa: PLC0415

    config = (ROOT / "infra/lib/config/index.ts").read_text()

    def reviewed(name: str) -> int:
        return int(re.findall(rf"{name}: (\d+),", config)[0])

    visibility = reviewed("tableWriterTimeoutSeconds") + reviewed(
        "semanticWriteVisibilityMarginSeconds"
    )
    # Revision 2 moved the queue's maxReceiveCount into the generated graph routes (design §2.1).
    receives = int(ROUTES["semantic_write"]["max_receive_count"])
    assert (
        visibility,
        receives,
    ) == (land_job.SEMANTIC_WRITE_VISIBILITY_SECONDS, land_job.SEMANTIC_WRITE_MAX_RECEIVES)
    assert reviewed("tableWriterTimeoutSeconds") == land_job.TABLE_WRITER_TIMEOUT_SECONDS
    last_receive_done = (receives - 1) * visibility + reviewed("tableWriterTimeoutSeconds")
    assert last_receive_done < land_job.SEAL_TIMEOUT_SECONDS
    # Positive control: the earlier 600 s ends before even one redelivery.
    assert visibility > 600


@pytest.mark.covers("ADR-086")
def test_the_seal_guard_rides_out_a_failed_current_read_and_still_times_out() -> None:
    from domain.shared_kernel.errors import ErrorCode  # noqa: PLC0415

    seal = _all_seals()[0]
    now = [0.0]
    sealed = SimpleNamespace(
        publication_id=SimpleNamespace(value="publication-new"),
        published_at=datetime(2026, 10, 6, 13, 0, tzinfo=UTC),
        source_revision=SimpleNamespace(
            observed_at=datetime.combine(
                cast("date", seal.manifest.source_watermark), time(), tzinfo=UTC
            )
        ),
    )
    answers: list[object] = []

    def read(_delivery: LandedDelivery) -> object:
        answer = answers.pop(0) if answers else sealed
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(seconds: float) -> None:
        now[0] += seconds

    guard = land_job.SealSequenceGuard(
        read_current=cast("Any", read), report=_ignore, sleep=sleep, clock=lambda: now[0]
    )
    failure = DependencyError(
        ErrorCode("SEMANTIC_STATE", 4), "The canonical publication could not be read."
    )
    # Two failed polls, then the publication: the wait succeeds.
    answers[:] = [failure, failure]
    wait = guard.await_current(seal, None, lambda: False)
    assert wait.polls == 3
    # Before the put, a failed read is retried; past the retry budget it still raises.
    answers[:] = [failure]
    assert guard.read_before(seal) is sealed
    now[0] = 0.0
    answers[:] = [failure] * 1000
    with pytest.raises(DependencyError):
        guard.read_before(seal)
    assert now[0] >= land_job.SEAL_READ_RETRY_SECONDS
    # Positive control: a read that keeps failing still ends at the guard timeout, never hangs.
    now[0] = 0.0
    answers[:] = [failure] * 10_000
    with pytest.raises(LandingError, match="was not CURRENT"):
        guard.await_current(seal, None, lambda: False)
    assert now[0] >= land_job.SEAL_TIMEOUT_SECONDS


# --- Phase B final-state backfill (owner, 2026-10-06; ADR-086 §14) ---------------------------

_BACKFILL = PlanSelection(frozenset({DeliveryClass.BACKFILL}))


@functools.cache
def _backfill_plan() -> tuple[LandedDelivery, ...]:
    return plan_landings(ROOT, _BACKFILL)


@pytest.mark.covers("ADR-086")
def test_every_generated_amc_delivery_carries_its_own_period_as_its_amc_window() -> None:
    """The producer's registration rule: the window is `[period_start, period_end + 1 day)`."""
    checked = 0
    for item in (*_plan(), *_backfill_plan()):
        if not item.fixture.startswith("generated/amc-"):
            continue
        rows = list(csv.DictReader(StringIO(item.data[0].body.decode())))
        windows = {(row[WINDOW_COLUMNS[0]], row[WINDOW_COLUMNS[1]]) for row in rows}
        end = item.manifest.period_end + timedelta(days=1)
        assert windows == {
            (f"{item.manifest.period_start}T00:00:00.000Z", f"{end}T00:00:00.000Z")
        }, item.fixture
        checked += 1
    # Positive control: at least every generated AMC fixture's cadence deliveries were read.
    assert checked >= len(bundle_module.AMC_DAILY_DATE_COLUMNS) * _AMC_DAILY_DELIVERIES


def _rows_by_day(delivery: LandedDelivery, column: str) -> dict[str, list[str]]:
    """Each day's row values; the AMC window is the delivery's own, so it isn't a value."""
    found: dict[str, list[str]] = {}
    for row in csv.DictReader(StringIO(delivery.data[0].body.decode())):
        values = {key: value for key, value in row.items() if key not in WINDOW_COLUMNS}
        found.setdefault(row[column], []).append(json.dumps(values, sort_keys=True))
    return found


@pytest.mark.covers("ADR-086")
def test_the_backfill_counts_are_the_evaluated_ones_and_replace_only_the_old_cadence() -> None:
    counts: dict[str, int] = {}
    for item in _backfill_plan():
        counts[item.fixture.partition("#")[0]] = counts.get(item.fixture.partition("#")[0], 0) + 1
    amc = dict.fromkeys(bundle_module.AMC_DAILY_DATE_COLUMNS, 136)
    assert counts == {
        **amc,
        "generated/vendor-sales-by-asin.json": 407,
        "generated/vendor-traffic-by-asin.json": 407,
        # Every complete week and month landing on or before the cut, unchanged (ADR-100), for
        # each grain set: pack 1 (platform) and pack 2 (channel) land the same periods, and
        # pack 3 (campaign) its calendar months only.
        "generated/amc-new-to-brand-buyers-platform.csv": 70,
        "generated/amc-new-to-brand-buyers-channel.csv": 70,
        "generated/amc-new-to-brand-buyers-campaign.csv": 13,
        # The D2 corpora's periods landing on or before the cut: pack 5 weeks and months less
        # its refused week, packs 6-7 months and quarters less the refused month. The segment
        # packs (8-13) plan no pre-cut period (their datasets are unencoded in the pinned band),
        # and no cohort vintage (packs 14-15) lands that early.
        "generated/amc-exposure-groups.csv": 67,
        "generated/amc-exposure-frequency-platform.csv": 15,
        "generated/amc-exposure-frequency-channel.csv": 15,
    }
    assert sum(counts.values()) == 2_288
    cut = bundle_module.backfill_last_day(load_synthetic_entity_catalog(ROOT).shape)
    replay = [
        item
        for item in _plan()
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day <= cut
    ]
    # Pack 2 (amc-new-to-brand-buyers-channel) adds its 70 cadence deliveries on or before the cut,
    # pack 3 (amc-new-to-brand-buyers-campaign) its 13 monthly ones, and packs 5-7 their
    # 97 periods on or before the cut.
    assert len(replay) == 38_739
    # The full plan, phase A and the increment do not change: the backfill is selected only.
    assert all(item.delivery_class is not DeliveryClass.BACKFILL for item in _plan())
    assert all(item.run_day is not None and item.run_day <= cut for item in _backfill_plan())
    for item in _backfill_plan():
        if item.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES:
            # One populated bucket: the whole complete period its window is (ADR-100).
            windows = {
                (row["time_window_start"], row["time_window_end"])
                for row in csv.DictReader(StringIO(item.data[0].body.decode()))
            }
            assert len(windows) == 1
            continue
        days = (item.manifest.period_end - item.manifest.period_start).days + 1
        assert days <= MAX_POPULATED_BUCKETS_PER_DELIVERY
    prefixes = {item.manifest.prefix for item in _plan()}
    amc_prefixes = [
        item.manifest.prefix for item in _backfill_plan() if "#backfill#" in item.fixture
    ]
    assert len(set(amc_prefixes)) == len(amc_prefixes) and not prefixes & set(amc_prefixes)


@pytest.mark.covers("ADR-086")
def test_every_backfill_watermark_meets_its_retired_floor_and_a_first_report_one_would_not() -> (
    None
):
    touched = 0
    for item in _backfill_plan():
        fixture = item.fixture.partition("#")[0]
        floor = bundle_module._retired_floor(  # pyright: ignore[reportPrivateUsage]
            fixture, item.manifest.period_start, item.manifest.period_end
        )
        if floor is None:
            continue
        touched += 1
        assert item.manifest.source_watermark is not None
        assert item.manifest.source_watermark >= floor
        # Positive control: a first-report watermark (the chunk's first day) is below the floor.
        assert item.manifest.period_start < floor
    assert touched >= 7


@pytest.mark.covers("ADR-086")
def test_backfill_values_equal_what_the_full_replay_leaves_current() -> None:
    cut = bundle_module.backfill_last_day(load_synthetic_entity_catalog(ROOT).shape)
    replay_last: dict[tuple[str, str], list[str]] = {}
    for item in _plan():  # plan order is run order: the last write per day wins
        fixture = item.fixture.partition("#")[0]
        column = bundle_module.AMC_DAILY_DATE_COLUMNS.get(fixture)
        if column is None or item.delivery_class is not DeliveryClass.CADENCE:
            continue
        for day, rows in _rows_by_day(item, column).items():
            if date.fromisoformat(day) <= cut:
                replay_last[(fixture, day)] = rows
    backfilled: dict[tuple[str, str], list[str]] = {}
    for item in _backfill_plan():
        fixture = item.fixture.partition("#")[0]
        column = bundle_module.AMC_DAILY_DATE_COLUMNS.get(fixture)
        if column is not None:
            for day, rows in _rows_by_day(item, column).items():
                backfilled[(fixture, day)] = rows
    assert len(backfilled) == len(replay_last) == 3_663
    mismatched = [
        key for key, rows in backfilled.items() if sorted(rows) != sorted(replay_last[key])
    ]
    assert mismatched == []
    # Vendor: each backfill report is byte-identical to the replay's last report for its day.
    replay_vendor = {
        item.marker.key: item.data[0].body
        for item in _plan()
        if item.fixture.partition("#")[0]
        in {
            "generated/vendor-sales-by-asin.json",
            "generated/vendor-traffic-by-asin.json",
        }
    }
    vendor = [
        item
        for item in _backfill_plan()
        if "#backfill#" not in item.fixture
        and item.fixture.partition("#")[0] not in AMC_PERIODIC_FIXTURES
    ]
    assert vendor and all(replay_vendor[item.marker.key] == item.data[0].body for item in vendor)
    # Periodic: each backfill delivery is the immutable cadence delivery for its period.
    replay_periodic = {
        item.marker.key: item.data[0].body
        for item in _plan()
        if item.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES
    }
    periodic = [
        item for item in _backfill_plan() if item.fixture.partition("#")[0] in AMC_PERIODIC_FIXTURES
    ]
    assert len(periodic) == 250  # 153 NTB periods and 97 from packs 5-7
    assert all(replay_periodic[item.marker.key] == item.data[0].body for item in periodic)
    assert all("#daily-run=" not in item.fixture for item in vendor if "sales" in item.fixture)


# --- V-24: the probe-input producer, `--probe-retrieval` with `--no-twin` (S1, P0 commit) -----

_PROBE_NAME = "generated/amc-dsp-impressions.csv#daily-run=2026-10-06#chunk=2026-10-04"
"""The probe runner's fixed phase-A delivery (validation/probe_ingestion_traceability_gate.py)."""


@functools.cache
def _with_backfill() -> tuple[tuple[LandedDelivery, ...], HistoricalPolicy]:
    return bundle_module.plan_with_backfill(ROOT)


def _planned_delivery(name: str) -> LandedDelivery:
    planned, _ = _with_backfill()
    return next(item for item in planned if item.fixture == name)


def test_v24_one_phase_a_delivery_gives_one_newer_key_in_the_same_window() -> None:
    planned, policy = _with_backfill()
    original = _planned_delivery(_PROBE_NAME)
    taken = frozenset({original.manifest.retrieved_at})
    result = bundle_module.plan_probe_retrieval(
        planned, policy, bundle_module.ProbeRetrievalRequest(_PROBE_NAME, taken)
    )
    probe = result.probe
    assert result.window == bundle_module.window_prefix(original.manifest)
    assert bundle_module.window_prefix(probe.manifest) == result.window
    assert probe.manifest.manifest_key != original.manifest.manifest_key
    assert probe.manifest.retrieved_at > original.manifest.retrieved_at
    assert probe.manifest.retrieved_at == policy.replay_base + timedelta(
        seconds=policy.probe_first_offset
    )
    assert [item.body for item in probe.data] == [item.body for item in original.data]
    assert replace(probe.manifest, retrieved_at=original.manifest.retrieved_at) == (
        original.manifest
    )
    assert read_manifest(probe.marker.body.decode()) == probe.manifest
    # The next free instant is taken when the first one is already in the window.
    second = bundle_module.plan_probe_retrieval(
        planned,
        policy,
        bundle_module.ProbeRetrievalRequest(_PROBE_NAME, taken | {probe.manifest.retrieved_at}),
    )
    assert second.probe.manifest.retrieved_at == probe.manifest.retrieved_at + timedelta(seconds=1)


def test_v24_a_historical_or_non_cadence_name_is_refused_before_any_key() -> None:
    planned, policy = _with_backfill()
    historical = next(
        item.fixture
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day <= policy.cut
        and item.fixture.startswith("generated/amc-")
    )
    seal = next(item.fixture for item in planned if item.delivery_class is DeliveryClass.SEAL)
    backfill = next(item.fixture for item in planned if "#backfill#" in item.fixture)
    for name, message in (
        (historical, "FULL_HISTORY_NOT_ENABLED"),
        (seal, "cadence delivery"),
        (backfill, "cadence delivery"),
        ("generated/no-such-fixture.csv", "exactly one is required"),
    ):
        with pytest.raises(LandingError, match=message):
            bundle_module.plan_probe_retrieval(
                planned, policy, bundle_module.ProbeRetrievalRequest(name, frozenset())
            )


def test_v24_every_probe_slot_taken_is_refused() -> None:
    planned, policy = _with_backfill()
    taken = frozenset(probe_slot_instants(policy))
    with pytest.raises(LandingError, match="every reserved probe retrieval slot"):
        bundle_module.plan_probe_retrieval(
            planned, policy, bundle_module.ProbeRetrievalRequest(_PROBE_NAME, taken)
        )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--no-twin",), "--no-twin is accepted only with --probe-retrieval"),
        (
            ("--probe-retrieval", _PROBE_NAME, "--probe-retrieval", _PROBE_NAME),
            "names exactly one delivery",
        ),
        (("--probe-retrieval", _PROBE_NAME, "--classes", "cadence"), "no class or run-day"),
        (
            ("--probe-retrieval", _PROBE_NAME, "--run-days-from", "2026-10-01"),
            "no class or run-day",
        ),
        (("--delivery", _PROBE_NAME, "--delivery", _PROBE_NAME), "names exactly one delivery"),
        (("--delivery", _PROBE_NAME, "--classes", "cadence"), "takes no other selection"),
    ],
)
def test_v24_selection_beside_a_probe_or_named_delivery_is_refused_before_any_aws_call(
    extra: tuple[str, ...], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        land_cli._parse_args(_cli_arguments(*extra))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


_TWINS: list[object] = []


def _probe_run(
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    existing: tuple[str, ...] = (),
    twin_decision: str = "ADMITTED",
) -> tuple[int, list[tuple[str, ...]], list[BaseException], str]:
    """Run `main` in probe mode with every AWS edge stubbed; `puts` lists the keys applied.

    S6: the twin is stubbed at `send_probe_twin`; `_TWINS` records each request it was sent.
    """
    puts: list[tuple[str, ...]] = []
    errors: list[BaseException] = []
    _TWINS.clear()
    extra = (*extra, "--graph-store-stack", "Adlytics-ControlPlaneAuthorityStore-dev")

    def twin(_session: object, request: object) -> dict[str, object]:
        _TWINS.append(request)
        return {"mode": "SHADOW", "sent": True, "decision": twin_decision}

    monkeypatch.setattr(land_cli, "send_probe_twin", twin)

    class Paginator:
        @staticmethod
        def paginate(**kwargs: object) -> list[dict[str, object]]:
            prefix = str(kwargs["Prefix"])
            return [{"Contents": [{"Key": f"{prefix}{item}/manifest.json"} for item in existing]}]

    class Session:
        _session = object()

        @staticmethod
        def client(service: str) -> object:
            if service == "sts":
                return SimpleNamespace(
                    get_caller_identity=lambda: {"Account": "000000000000", "Arn": "arn:test"}
                )
            if service == "s3":

                def paginator(_name: str) -> Paginator:
                    return Paginator()

                return SimpleNamespace(get_paginator=paginator)
            return object()

    def apply(request: land_cli.ApplyLandFixturesRequest) -> int:
        puts.append(tuple(item.marker.key for item in request.deliveries))
        return len(request.deliveries)

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    def session(**_kwargs: object) -> Session:
        return Session()

    def no_tracing(*_args: object, **_kwargs: object) -> bool:
        return False

    def planned(_root: object) -> object:
        return _with_backfill()

    monkeypatch.setattr(land_cli, "validate_fixture_provenance", _ignore)
    monkeypatch.setattr(land_cli, "plan_with_backfill", planned)
    monkeypatch.setattr(land_cli.boto3, "Session", session)
    monkeypatch.setattr(land_cli, "configure_tracing", no_tracing)
    monkeypatch.setattr(land_cli, "verify_named_raw_bucket", _ignore)
    monkeypatch.setattr(land_cli, "apply_immutable", apply)
    monkeypatch.setattr(land_cli, "report_operation", _ignore)
    monkeypatch.setattr(land_cli, "report_error", record_error)
    monkeypatch.setattr(land_cli, "flush_metrics", _ignore)
    monkeypatch.setattr(land_cli, "flush_traces", _ignore)
    output = StringIO()
    monkeypatch.setattr("sys.stdout", output)
    result = land_cli.main(_cli_arguments(*extra))
    return result, puts, errors, output.getvalue()


def test_v24_probe_dry_run_reports_exactly_one_non_historical_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, puts, errors, output = _probe_run(
        monkeypatch, "--probe-retrieval", _PROBE_NAME, "--dry-run"
    )
    assert (result, puts, errors) == (0, [], [])
    (line,) = [item for item in output.splitlines() if item.startswith("PROBE_RETRIEVAL ")]
    report = json.loads(line.removeprefix("PROBE_RETRIEVAL "))
    assert report["deliveries"] == 1
    assert report["historical"] is False
    assert report["original_historical"] is False
    assert report["body_sha256"] == report["original_body_sha256"]
    assert report["key"].startswith(report["window"])
    assert report["original_key"] == _planned_delivery(_PROBE_NAME).manifest.manifest_key


def test_v24_no_twin_probe_puts_exactly_one_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _, policy = _with_backfill()
    first = (policy.replay_base + timedelta(seconds=policy.probe_first_offset)).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    result, puts, errors, output = _probe_run(
        monkeypatch,
        "--probe-retrieval",
        _PROBE_NAME,
        "--no-twin",
        existing=(first,),
    )
    assert (result, errors) == (0, [])
    assert len(puts) == 1 and len(puts[0]) == 1
    report = json.loads(
        next(i for i in output.splitlines() if i.startswith("PROBE_RETRIEVAL ")).removeprefix(
            "PROBE_RETRIEVAL "
        )
    )
    assert report["no_twin"] is True
    assert puts[0][0] == report["key"]
    assert f"retrieved={first}" not in report["key"]  # the taken instant is skipped


def test_v24_probe_of_a_historical_delivery_makes_zero_puts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned, policy = _with_backfill()
    historical = next(
        item.fixture
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day <= policy.cut
    )
    with pytest.raises(SystemExit, match="FULL_HISTORY_NOT_ENABLED"):
        _probe_run(monkeypatch, "--probe-retrieval", historical)


def test_named_delivery_lands_exactly_one_planned_key(monkeypatch: pytest.MonkeyPatch) -> None:
    result, puts, errors, _ = _probe_run(monkeypatch, "--delivery", _PROBE_NAME)
    assert (result, errors) == (0, [])
    assert puts == [(_planned_delivery(_PROBE_NAME).manifest.manifest_key,)]
    planned, policy = _with_backfill()
    historical = next(
        item.fixture
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day <= policy.cut
    )
    with pytest.raises(SystemExit, match="FULL_HISTORY_NOT_ENABLED"):
        _probe_run(monkeypatch, "--delivery", historical)


# --- The S2 hard deny at the fixture-landing producer (BF "S2 hard deny" (c), S1) ------------


def test_hard_deny_a_selection_holding_a_historical_delivery_is_refused_before_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(land_cli, "validate_fixture_provenance", _ignore)

    def with_backfill(_root: object) -> object:
        return _with_backfill()

    monkeypatch.setattr(land_cli, "plan_with_backfill", with_backfill)
    _, policy = _with_backfill()
    with pytest.raises(SystemExit, match="FULL_HISTORY_NOT_ENABLED"):
        land_cli.main(
            _cli_arguments(
                "--classes", "cadence", "--run-days-to", policy.cut.isoformat(), "--dry-run"
            )
        )
    assert "LAND FIXTURES" not in capsys.readouterr().out
    with pytest.raises(SystemExit, match="FULL_HISTORY_NOT_ENABLED"):
        land_cli.main(_cli_arguments("--classes", "backfill", "--dry-run"))


def test_hard_deny_positive_control_a_phase_a_selection_plans_normally() -> None:
    planned, policy = _with_backfill()
    selection = PlanSelection(
        frozenset({DeliveryClass.CADENCE}), run_days_from=policy.cut + timedelta(days=1)
    )
    chosen = [item for item in planned if selection.selects(item)]
    assert chosen
    assert not any(
        is_historical(bundle_module.manifest_facts(item.manifest), policy) for item in chosen
    )


def test_hard_deny_the_job_refuses_a_historical_delivery_with_zero_puts() -> None:
    planned, policy = _with_backfill()
    historical = next(item for item in planned if "#backfill#" in item.fixture)
    puts: list[object] = []
    client = SimpleNamespace(put_object=lambda **kwargs: puts.append(kwargs))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    with pytest.raises(LandingError, match="FULL_HISTORY_NOT_ENABLED"):
        land_job.apply_immutable(
            land_job.ApplyLandFixturesRequest(
                client=cast("Any", client),
                bucket="explicit-versioned-raw",
                deliveries=(historical,),
                operation_id="land-unit",
                execution_id="exec",
                predecessor_execution_id=None,
                plan_digest="sha256:x",
                progress=_ignore,
                cancelled=lambda: False,
                historical_policy=policy,
            )
        )
    assert puts == []


# --- S6: the probe retrieval's on_demand twin (V-19) ---------------------------------------------


def test_v19_a_probe_retrieval_sends_one_twin_after_its_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, puts, errors, output = _probe_run(monkeypatch, "--probe-retrieval", _PROBE_NAME)
    assert result == 0, errors
    assert len(puts) == 1
    assert len(_TWINS) == 1
    assert "PROBE_TWIN" in output


def test_v19_a_refused_twin_exits_nonzero_after_the_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """LEGACY_DRAINING: the put lands, the twin gets REFUSED(RUN_ADMISSION_NOT_ACTIVE)."""
    result, puts, _errors, _ = _probe_run(
        monkeypatch, "--probe-retrieval", _PROBE_NAME, twin_decision="REFUSED"
    )
    assert result == 3
    assert len(puts) == 1


def test_v19_no_twin_puts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    result, puts, errors, _ = _probe_run(monkeypatch, "--probe-retrieval", _PROBE_NAME, "--no-twin")
    assert result == 0, errors
    assert len(puts) == 1
    assert _TWINS == []


# --- D7: exact reservation (`--reserved-probe-key --no-twin`) and `--twin-only` launch -----------


def _slot(index: int) -> str:
    _, policy = _with_backfill()
    instant = policy.replay_base + timedelta(seconds=policy.probe_first_offset + index)
    return instant.strftime("%Y%m%dT%H%M%SZ")


def _planned_probe_key(taken: tuple[str, ...]) -> str:
    planned, policy = _with_backfill()
    instants = frozenset(
        datetime.strptime(item, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) for item in taken
    )
    return bundle_module.plan_probe_retrieval(
        planned, policy, bundle_module.ProbeRetrievalRequest(_PROBE_NAME, instants)
    ).probe.manifest.manifest_key


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--probe-retrieval", _PROBE_NAME, "--twin-only"), "--twin-only requires"),
        (("--reserved-probe-key", "k", "--no-twin"), "only with --probe-retrieval"),
        (
            ("--probe-retrieval", _PROBE_NAME, "--reserved-probe-key", "k", "--dry-run"),
            "cannot be used with --dry-run",
        ),
        (
            ("--probe-retrieval", _PROBE_NAME, "--reserved-probe-key", "k"),
            "exactly one of --no-twin or --twin-only",
        ),
        (
            (
                "--probe-retrieval",
                _PROBE_NAME,
                "--reserved-probe-key",
                "k",
                "--no-twin",
                "--twin-only",
            ),
            "exactly one of --no-twin or --twin-only",
        ),
        (
            ("--probe-retrieval", _PROBE_NAME, "--reserved-probe-key", "k", "--twin-only"),
            "give --graph-store-stack",
        ),
    ],
)
def test_d7_reserved_key_and_twin_only_are_mutually_constrained(
    extra: tuple[str, ...], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        land_cli._parse_args(_cli_arguments(*extra))  # pyright: ignore[reportPrivateUsage]
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_d7_a_reservation_puts_only_its_still_planned_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _planned_probe_key(())
    result, puts, errors, _ = _probe_run(
        monkeypatch, "--probe-retrieval", _PROBE_NAME, "--reserved-probe-key", key, "--no-twin"
    )
    assert (result, errors, puts, _TWINS) == (0, [], [(key,)], [])


def test_d7_a_reservation_whose_slot_was_taken_is_refused_with_zero_puts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _planned_probe_key(())
    with pytest.raises(SystemExit, match="plan drift"):
        _probe_run(
            monkeypatch,
            "--probe-retrieval",
            _PROBE_NAME,
            "--reserved-probe-key",
            key,
            "--no-twin",
            existing=(_slot(0),),
        )
    assert _TWINS == []


def test_d7_twin_only_verifies_the_reserved_key_and_sends_only_its_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _planned_probe_key((_slot(0),))
    verified: list[str] = []

    def verify(_session: object, _bucket: str, delivery: Any) -> str:
        verified.append(delivery.marker.key)
        return "v-1"

    monkeypatch.setattr(land_cli, "verify_reserved_probe", verify)
    result, puts, errors, output = _probe_run(
        monkeypatch,
        "--probe-retrieval",
        _PROBE_NAME,
        "--reserved-probe-key",
        key,
        "--twin-only",
        existing=(_slot(0), _slot(1)),
    )
    assert (result, errors, puts) == (0, [], [])
    assert verified == [key]
    assert [cast("Any", item).manifest_key for item in _TWINS] == [key]
    assert len([line for line in output.splitlines() if line.startswith("PROBE_TWIN ")]) == 1


def test_d7_twin_only_for_a_key_that_is_not_its_planned_slot_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slot 1 is reserved, but slot 0 is free: slot 1 was never this entry's dry-planned key.
    key = _planned_probe_key((_slot(0),))
    with pytest.raises(SystemExit, match="plan drift"):
        _probe_run(
            monkeypatch,
            "--probe-retrieval",
            _PROBE_NAME,
            "--reserved-probe-key",
            key,
            "--twin-only",
            existing=(_slot(1),),
        )
    assert _TWINS == []


def _probe_delivery() -> LandedDelivery:
    planned, policy = _with_backfill()
    return bundle_module.plan_probe_retrieval(
        planned, policy, bundle_module.ProbeRetrievalRequest(_PROBE_NAME, frozenset())
    ).probe


@dataclass
class _VersionedS3:
    """A versioned bucket: each put of a new key gets the next version ID."""

    objects: dict[str, list[tuple[str, bytes]]] = dataclasses.field(
        default_factory=dict[str, list[tuple[str, bytes]]]
    )

    def put_object(self, **request: Any) -> dict[str, object]:
        versions = self.objects.setdefault(str(request["Key"]), [])
        if versions and request.get("IfNoneMatch") == "*":
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        versions.append((f"v-{sum(map(len, self.objects.values()))}", bytes(request["Body"])))
        return {"VersionId": versions[-1][0]}

    def get_object(self, **request: Any) -> dict[str, object]:
        versions = self.objects[str(request["Key"])]
        wanted = request.get("VersionId", versions[-1][0])
        body = next(body for version, body in versions if version == wanted)
        return {"Body": BytesIO(body), "VersionId": wanted}

    def get_paginator(self, _name: str) -> Any:
        objects = self.objects

        class Paginator:
            @staticmethod
            def paginate(**kwargs: object) -> list[dict[str, object]]:
                key = str(kwargs["Prefix"])
                found = [{"Key": key, "VersionId": v} for v, _ in objects.get(key, [])]
                return [{"Versions": found}]

        return Paginator()


def test_d7_a_reservation_landed_by_apply_immutable_verifies_for_its_launch() -> None:
    # x3 (2026-10-15): the reserved manifest is written bound to its data versions, so a launch
    # comparing against the unbound plan refused every real reservation.
    from adapters.jobs.land_fixtures.twin import verify_reserved_probe  # noqa: PLC0415

    delivery = _probe_delivery()
    s3 = _VersionedS3()

    def client(_name: str) -> _VersionedS3:
        return s3

    session = cast("Any", SimpleNamespace(client=client))
    land_job.apply_immutable(
        land_job.ApplyLandFixturesRequest(
            client=cast("Any", s3),
            bucket="raw",
            deliveries=(delivery,),
            operation_id="op",
            execution_id="exec",
            predecessor_execution_id=None,
            plan_digest="sha256:x",
            progress=lambda _snapshot: None,
            cancelled=lambda: False,
            historical_policy=_with_backfill()[1],
            seal_guard=None,
        )
    )
    assert delivery.marker.body != s3.objects[delivery.marker.key][0][1]  # bound, not planned
    assert verify_reserved_probe(session, "raw", delivery) == s3.objects[delivery.marker.key][0][0]
    # Paired controls: a second marker version, a missing marker, other marker bytes, and
    # another landing's data under the planned key are each refused.
    marker_key = delivery.marker.key
    landed = list(s3.objects[marker_key])
    s3.objects[marker_key] = [*landed, ("v-extra", landed[0][1])]
    with pytest.raises(PermissionError, match="2 versions"):
        verify_reserved_probe(session, "raw", delivery)
    s3.objects[marker_key] = []
    with pytest.raises(PermissionError, match="0 versions"):
        verify_reserved_probe(session, "raw", delivery)
    s3.objects[marker_key] = [(landed[0][0], delivery.marker.body)]
    with pytest.raises(PermissionError, match="reserved probe key holds different bytes"):
        verify_reserved_probe(session, "raw", delivery)
    s3.objects[marker_key] = landed
    data_key = delivery.data[0].key
    s3.objects[data_key] = [(s3.objects[data_key][0][0], b"other bytes")]
    with pytest.raises(PermissionError, match="data object holds different bytes"):
        verify_reserved_probe(session, "raw", delivery)


# --- A declared variant's delivery carries the variant's governance (D2 live, 2026-10-17) -------
# Live in dev, the September coarse frequency months and quarterly cohort vintages quarantined
# PACK_VARIANT_MISMATCH: the planner named the variant but stamped the base pack's governance.

_VARIANT_DELIVERIES = {
    "generated/amc-exposure-frequency-platform.csv#period=2026-09-01..2026-09-30": "coarse",
    "generated/amc-exposure-frequency-channel.csv#period=2026-09-01..2026-09-30": "coarse",
    "generated/amc-acquisition-cohorts.csv#period=2025-09-01..2026-09-30": "quarterly",
    "generated/amc-acquisition-cohorts-arp.csv#period=2025-09-01..2026-09-30": "quarterly",
}
"""Every planned delivery whose rows are a declared variant's shape, from the catalog's shapes."""


def _declared_pack(pack_id: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads((ROOT / f"contracts/amc/query-packs/{pack_id}.json").read_text()),
    )


@pytest.mark.covers("ADR-100")
def test_a_variant_delivery_carries_its_variant_governance_and_every_other_the_base() -> None:
    from adapters.producer.packs import installed_governance, load_query_packs  # noqa: PLC0415
    from domain.producer.models import digest_bytes  # noqa: PLC0415

    packs = {pack.pack_id: pack for pack in load_query_packs(ROOT)}
    installed = installed_governance(load_query_packs(ROOT))
    variants: dict[str, str] = {}
    base = 0
    for item in _plan():
        pack_id = AMC_FIXTURE_CONTRACTS.get(item.fixture.split("#", 1)[0])
        if pack_id is None:
            continue
        declared = _declared_pack(pack_id)
        governance = item.manifest.governance
        assert governance is not None
        assert governance in installed
        variant = item.manifest.option_set.get("pack_variant")
        if variant is None:
            # Base control: the base pack's exact identity and declared version.
            assert governance == packs[pack_id].governance
            assert governance.query_pack_version == declared["packVersion"]
            base += 1
            continue
        variants[item.fixture] = str(variant)
        (entry,) = [row for row in declared["variants"] if row["variant"] == variant]
        # The governed SQL text is whitespace-normalized, as the pack loader binds it.
        sql = " ".join(
            (ROOT / "contracts/amc/query-packs" / entry["queryFile"]).read_text().split()
        )
        assert governance == packs[pack_id].variant_binding(str(variant)).governance
        assert governance.query_pack_version == entry["packVersion"] != declared["packVersion"]
        assert governance.query_digest == digest_bytes(sql.encode())
        assert governance.query_pack_id == pack_id
    assert variants == _VARIANT_DELIVERIES
    assert base > 0


def _assess_planned(item: LandedDelivery, previous_degraded: bool) -> list[tuple[str, str]]:
    """Assess one planned delivery's exact bytes under the installed discard policies."""
    from adapters.jobs.contract_validation.loader import load_registry  # noqa: PLC0415
    from adapters.jobs.curation.manifests import write_manifest  # noqa: PLC0415
    from adapters.jobs.curation.readers import DeliveredFile, read  # noqa: PLC0415
    from adapters.producer.packs import discard_policies, load_query_packs  # noqa: PLC0415
    from domain.curation.ingestion import (  # noqa: PLC0415
        AssessmentRequest,
        ContractLoad,
        assess_source_delivery,
    )
    from domain.curation.landing import partition_for  # noqa: PLC0415
    from domain.curation.mapping import MappingRequest, map_rows  # noqa: PLC0415
    from domain.producer.variants import PreviousPeriodEvidence  # noqa: PLC0415

    pack_id = AMC_FIXTURE_CONTRACTS[item.fixture.split("#", 1)[0]]
    contract = next(c for _, c in load_contracts(ROOT) if c.contract_id == pack_id)
    text = item.data[0].body.decode()
    document = read(DeliveredFile(text, contract.source_format, write_manifest(item.manifest)))
    assessed = assess_source_delivery(
        AssessmentRequest(
            manifest=item.manifest,
            document=document,
            loads=(
                ContractLoad(
                    contract,
                    partition_for(item.manifest, contract),
                    map_rows(MappingRequest(contract, document)),
                ),
            ),
            registry=load_registry(ROOT),
            discard_policies=discard_policies(load_query_packs(ROOT)),
            variant_evidence={pack_id: PreviousPeriodEvidence(None, previous_degraded)},
        )
    )
    return [
        (violation.assertion.value, violation.detail)
        for violation in assessed.assessments[0].outcome.violations
    ]


@pytest.mark.covers("ADR-100")
@pytest.mark.parametrize(("fixture", "variant"), sorted(_VARIANT_DELIVERIES.items()))
def test_a_planned_variant_delivery_assesses_clean_and_its_base_governed_form_is_refused(
    fixture: str, variant: str
) -> None:
    from adapters.producer.packs import pack_by_id  # noqa: PLC0415

    (item,) = [planned for planned in _plan() if planned.fixture == fixture]
    pack_id = AMC_FIXTURE_CONTRACTS[fixture.split("#", 1)[0]]
    # Positive control: due from a degraded previous period, the variant delivery passes.
    assert _assess_planned(item, previous_degraded=True) == []
    # The pre-fix form: the same bytes and options under the base pack's governance.
    old = replace(
        item, manifest=replace(item.manifest, governance=pack_by_id(ROOT, pack_id).governance)
    )
    assert _assess_planned(old, previous_degraded=True) == [
        (
            "pack_variant",
            f"{pack_id}: PACK_VARIANT_MISMATCH: the registration names variant {variant} but "
            "its governed pack version binds the base pack.",
        )
    ]
