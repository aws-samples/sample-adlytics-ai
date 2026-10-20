# SPDX-License-Identifier: MIT-0
"""Exact-version, non-erasing Semantic V2 reprocess planning."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import (
    BuildManifestRequest,
    build_manifest,
    checksum_of,
    declared_columns_of,
    read_manifest,
    write_manifest,
)
from adapters.jobs.curation.readers import ReaderError
from adapters.jobs.ingest_delivery.semantic_sidecar import SemanticSidecarTooling
from adapters.jobs.reprocess_semantic_v2 import __main__ as reprocess_cli
from adapters.jobs.reprocess_semantic_v2 import job as reprocess_job
from adapters.jobs.reprocess_semantic_v2.job import (
    ApplyReprocessRequest,
    PlanReprocessRequest,
    ReprocessCancelledError,
    ReprocessProgress,
    apply_reprocess,
    plan_reprocess,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.producer.packs import load_query_packs
from domain.curation.landing import (
    GOVERNED_MANIFEST_LAYOUT,
    LandedObject,
    LandingError,
    Manifest,
    dataset_prefix,
    options_hash,
)
from domain.data_contracts.validation import ColumnRegistry
from domain.semantics.identity import SourceIdentifier
from domain.semantics.reprocessing import (
    ContractArtifact,
    CreateReprocessPlanRequest,
    RawDeliveryBinding,
    RawObjectBinding,
    ReprocessOperationId,
    ReprocessPlan,
    ReprocessRequest,
)
from domain.shared_kernel.provenance import ProducerGovernance
from domain.shared_kernel.values import DateRange
from ports.outbound.versioned_raw import RawVersion, VersionedBody

pytestmark = [pytest.mark.unit]
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _set_attribute(value: object, name: str, replacement: object) -> None:
    setattr(value, name, replacement)


class MemoryRaw:
    def __init__(self, versions: tuple[RawVersion, ...], bodies: dict[tuple[str, str], bytes]):
        self._versions = versions
        self._bodies = bodies
        self.prefixes: list[str] = []

    def versions(self, prefix: str) -> tuple[RawVersion, ...]:
        self.prefixes.append(prefix)
        return tuple(item for item in self._versions if item.key.startswith(prefix))

    def body(self, key: str, version_id: str) -> VersionedBody:
        return VersionedBody(key, version_id, self._bodies[(key, version_id)])

    def version(self, version_id: str) -> RawVersion:
        return next(item for item in self._versions if item.version_id == version_id)

    def add_version(self, version: RawVersion, body: bytes | None = None) -> None:
        self._versions = (*self._versions, version)
        if body is not None:
            self._bodies[(version.key, version.version_id)] = body

    def remove_version(self, version: RawVersion) -> None:
        self._versions = tuple(item for item in self._versions if item != version)


def _case(
    contract_id: str = "vendor-sales-by-asin",
    *,
    governance: ProducerGovernance | None = None,
    installed_governance: tuple[ProducerGovernance, ...] = (),
) -> tuple[PlanReprocessRequest, MemoryRaw]:
    loaded_contracts = load_contracts(ROOT)
    contracts = tuple(contract for _, contract in loaded_contracts)
    contract_artifacts = tuple(
        ContractArtifact(contract.contract_id, checksum_of(path.read_bytes()))
        for path, contract in loaded_contracts
    )
    contract = next(item for item in contracts if item.contract_id == contract_id)
    body = b"{}"
    options = {"sellingProgram": "RETAIL"}
    manifest = Manifest(
        source_system=contract.source_system,
        source_dataset=contract.source_dataset,
        source_scope=SourceIdentifier("vendor_central.vendor_group", "fictional-vendor-group"),
        option_set=options,
        option_set_hash=options_hash(options),
        marketplace="ATVPDKIKX0DER",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 1),
        source_watermark=date(2026, 8, 2),
        retrieved_at=datetime(2026, 8, 3, tzinfo=UTC),
        declared_columns=("salesByAsin",),
        objects=(
            LandedObject(
                "delivery.json",
                len(body),
                checksum_of(body),
                version_id="object-v1",
            ),
        ),
        governance=governance,
        layout_version=(GOVERNED_MANIFEST_LAYOUT if governance is not None else 1),
    )
    manifest_bytes = write_manifest(manifest).encode()
    versions = (
        RawVersion(
            manifest.manifest_key,
            "manifest-v1",
            len(manifest_bytes),
            datetime(2026, 8, 3, tzinfo=UTC),
            False,
        ),
        RawVersion(
            manifest.object_keys()[0],
            "object-v1",
            len(body),
            datetime(2026, 8, 3, tzinfo=UTC),
            False,
        ),
    )
    raw = MemoryRaw(
        versions,
        {
            (manifest.manifest_key, "manifest-v1"): manifest_bytes,
            (manifest.object_keys()[0], "object-v1"): body,
        },
    )
    request = PlanReprocessRequest(
        request=ReprocessRequest(
            environment="dev",
            stack_name="Adlytics-Foundation-dev",
            bucket="explicit-versioned-raw",
            contracts=(contract_id,),
            window=DateRange(date(2026, 8, 1), date(2026, 8, 1)),
            operation_id=ReprocessOperationId("task8-replay-001"),
        ),
        contracts=contracts,
        packs=load_source_packs(ROOT),
        raw=raw,
        installed_governance=installed_governance,
        contract_artifacts=contract_artifacts,
        semantic_authority_digest=reprocess_cli._semantic_authority_digest(),  # pyright: ignore[reportPrivateUsage]
    )
    return request, raw


@pytest.mark.covers("ADR-044")
def test_plan_binds_exact_manifest_and_object_versions_under_contract_prefix() -> None:
    request, raw = _case()
    plan = plan_reprocess(request)

    assert raw.prefixes == [
        dataset_prefix(
            next(item for item in request.contracts if item.contract_id == "vendor-sales-by-asin")
        )
    ]
    assert len(plan.deliveries) == 1
    delivery = plan.deliveries[0]
    assert delivery.manifest_version_id == "manifest-v1"
    assert delivery.object.version_id == "object-v1"
    assert delivery.object.actual_checksum == delivery.object.declared_checksum
    assert plan.digest.startswith("sha256:")


@pytest.mark.covers("ADR-044")
def test_retrieved_token_narrows_before_parsing_and_is_digest_bound() -> None:
    request, _raw = _case()
    original = plan_reprocess(request)
    token = original.deliveries[0].manifest_key.split("/retrieved=", 1)[1].split("/", 1)[0]
    narrowed_request = replace(request.request, retrieved_tokens=(token,))
    narrowed = plan_reprocess(replace(request, request=narrowed_request))
    empty = plan_reprocess(
        replace(
            request,
            request=replace(request.request, retrieved_tokens=("20990101T000000Z",)),
        )
    )

    assert narrowed.deliveries == original.deliveries
    assert (
        ReprocessPlan.digest_for(
            CreateReprocessPlanRequest(
                request=replace(request.request, retrieved_tokens=()),
                prefixes=original.prefixes,
                contract_artifacts=original.contract_artifacts,
                semantic_authority_digest=original.semantic_authority_digest,
                deliveries=original.deliveries,
            )
        )
        == original.digest
    )
    assert narrowed.digest != original.digest
    assert empty.deliveries == ()
    with pytest.raises(ValueError, match="retrieved token"):
        replace(request.request, retrieved_tokens=("2026-08-03",))
    with pytest.raises(ValueError, match="repeats a retrieved token"):
        replace(request.request, retrieved_tokens=(token, token))


def test_cli_accumulates_repeatable_retrieved_tokens() -> None:
    parsed = reprocess_cli.build_parser().parse_args(
        [
            "--profile",
            "explicit",
            "--region",
            "us-east-1",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
            "--stack",
            "Data-dev",
            "--bucket",
            "raw-dev",
            "--window-start",
            "2026-08-01",
            "--window-end",
            "2026-08-02",
            "--operation-id",
            "vendor-recovery",
            "--contract",
            "vendor-sales-by-asin",
            "--retrieved-token",
            "20260803T000000Z",
            "--retrieved-token",
            "20260804T000000Z",
        ]
    )

    assert parsed.retrieved_tokens == ["20260803T000000Z", "20260804T000000Z"]


def test_repeatable_tokens_union_selected_manifests_before_parsing_unselected_bytes() -> None:
    request, raw = _case()
    first = plan_reprocess(request).deliveries[0]
    first_token = first.manifest_key.split("/retrieved=", 1)[1].split("/", 1)[0]
    # A-TEST-LITERALS-001: a timestamp; see docs/security-dispositions.md
    second_token = "20260804T000000Z"  # nosec B105
    manifest_body = raw.body(first.manifest_key, first.manifest_version_id).body
    first_manifest = read_manifest(manifest_body.decode())
    second_manifest = replace(
        first_manifest,
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        objects=tuple(
            replace(
                item,
                version_id="object-v2",
            )
            for item in first_manifest.objects
        ),
    )
    second_bytes = write_manifest(second_manifest).encode()
    raw.add_version(
        RawVersion(
            second_manifest.manifest_key,
            "manifest-v2",
            len(second_bytes),
            datetime(2026, 8, 4, tzinfo=UTC),
            False,
        ),
        second_bytes,
    )
    raw.add_version(
        RawVersion(
            second_manifest.object_keys()[0],
            "object-v2",
            2,
            datetime(2026, 8, 4, tzinfo=UTC),
            False,
        ),
        b"{}",
    )
    malformed_key = first.manifest_key.replace(first_token, "20260805T000000Z")
    raw.add_version(
        RawVersion(malformed_key, "manifest-bad", 1, datetime(2026, 8, 5, tzinfo=UTC), False),
        b"{",
    )

    selected = replace(
        request,
        request=replace(request.request, retrieved_tokens=(first_token, second_token)),
    )
    plan = plan_reprocess(selected)

    assert len(plan.deliveries) == 2
    assert {item.manifest_version_id for item in plan.deliveries} == {
        "manifest-v1",
        "manifest-v2",
    }


@pytest.mark.covers("ADR-044", "ADR-053")
def test_selected_subset_cannot_hide_full_registry_ambiguity() -> None:
    request, _raw = _case("amc-dsp-impressions")

    with pytest.raises(LandingError, match="requires exact producer governance"):
        plan_reprocess(request)


@pytest.mark.covers("ADR-044", "ADR-053")
def test_governed_sibling_pack_is_excluded_after_full_registry_resolution() -> None:
    packs = {
        pack.contract_id: pack
        for pack in load_query_packs(ROOT)
        if pack.contract_id in {"amc-dsp-impressions", "amc-campaign-dsp"}
    }
    installed = tuple(pack.governance for pack in packs.values())
    request, raw = _case(
        "amc-dsp-impressions",
        governance=packs["amc-dsp-impressions"].governance,
        installed_governance=installed,
    )
    _sibling_request, sibling_raw = _case(
        "amc-campaign-dsp",
        governance=packs["amc-campaign-dsp"].governance,
        installed_governance=installed,
    )
    for version in sibling_raw.versions("amc/dsp_impressions/"):
        raw.add_version(
            version,
            sibling_raw.body(version.key, version.version_id).body,
        )

    plan = plan_reprocess(request)

    assert len(plan.deliveries) == 1
    assert plan.deliveries[0].contract_ids == ("amc-dsp-impressions",)
    assert "query_pack=amc-dsp-impressions/" in plan.deliveries[0].manifest_key


@pytest.mark.covers("ADR-044", "ADR-053")
def test_reprocess_refuses_governance_digest_drift_from_installed_artifacts() -> None:
    pack = next(
        item for item in load_query_packs(ROOT) if item.contract_id == "amc-dsp-impressions"
    )
    request, _raw = _case(
        pack.contract_id,
        governance=pack.governance,
        installed_governance=(replace(pack.governance, query_digest="sha256:" + "b" * 64),),
    )

    with pytest.raises(LandingError, match="artifacts differ"):
        plan_reprocess(request)


@pytest.mark.covers("ADR-044")
def test_partial_window_overlap_is_refused() -> None:
    request, _ = _case()
    changed = ReprocessRequest(
        environment=request.request.environment,
        stack_name=request.request.stack_name,
        bucket=request.request.bucket,
        contracts=request.request.contracts,
        window=DateRange(date(2026, 7, 31), date(2026, 7, 31)),
        operation_id=request.request.operation_id,
    )
    assert (
        plan_reprocess(
            PlanReprocessRequest(
                changed,
                request.contracts,
                request.packs,
                request.raw,
                request.installed_governance,
                request.contract_artifacts,
                request.semantic_authority_digest,
            )
        ).deliveries
        == ()
    )


@pytest.mark.covers("ADR-044", "ADR-045")
def test_out_of_window_legacy_manifest_is_excluded_before_strict_identity_parsing() -> None:
    request, raw = _case()
    contract = next(
        item for item in request.contracts if item.contract_id == "vendor-sales-by-asin"
    )
    key = f"{dataset_prefix(contract)}legacy/manifest.json"
    body = json.dumps(
        {
            "period_start": "2026-07-01",
            "period_end": "2026-07-01",
        }
    ).encode()
    raw.add_version(
        RawVersion(
            key,
            "legacy-manifest-v1",
            len(body),
            datetime(2026, 7, 2, tzinfo=UTC),
            False,
        ),
        body,
    )

    plan = plan_reprocess(request)

    assert len(plan.deliveries) == 1


@pytest.mark.covers("ADR-044", "ADR-045")
def test_overlapping_legacy_manifest_still_fails_closed_on_missing_scope() -> None:
    request, raw = _case()
    contract = next(
        item for item in request.contracts if item.contract_id == "vendor-sales-by-asin"
    )
    key = f"{dataset_prefix(contract)}legacy/manifest.json"
    current = raw.version("manifest-v1")
    legacy = json.loads(raw.body(current.key, current.version_id).body)
    legacy.pop("source_scope")
    body = json.dumps(legacy).encode()
    raw.add_version(
        RawVersion(
            key,
            "legacy-manifest-v1",
            len(body),
            datetime(2026, 8, 2, tzinfo=UTC),
            False,
        ),
        body,
    )

    with pytest.raises(LandingError, match="source_scope"):
        plan_reprocess(request)


@pytest.mark.covers("ADR-044")
def test_other_object_versions_do_not_make_declared_retained_version_ambiguous() -> None:
    request, raw = _case()
    original = raw.version("object-v1")
    raw.add_version(
        RawVersion(
            original.key,
            "object-v2",
            original.byte_count,
            datetime(2026, 8, 4, tzinfo=UTC),
            False,
        )
    )
    raw.add_version(
        RawVersion(
            original.key,
            "delete-v3",
            0,
            datetime(2026, 8, 5, tzinfo=UTC),
            True,
        )
    )

    plan = plan_reprocess(request)

    assert plan.deliveries[0].object.version_id == "object-v1"


@pytest.mark.covers("ADR-044")
def test_missing_declared_object_version_is_refused() -> None:
    request, raw = _case()
    original = raw.version("object-v1")
    raw.remove_version(original)

    with pytest.raises(ValueError, match="declared retained VersionId"):
        plan_reprocess(request)


@pytest.mark.covers("ADR-044")
def test_resolved_dsp_source_pack_can_be_planned_for_exact_reprocessing() -> None:
    pack = next(item for item in load_query_packs(ROOT) if item.contract_id == "amc-dsp-clicks")
    request, _ = _case(
        pack.contract_id,
        governance=pack.governance,
        installed_governance=(pack.governance,),
    )
    plan = plan_reprocess(request)
    assert plan.deliveries
    assert plan.deliveries[0].contract_ids == ("amc-dsp-clicks",)


def _planned() -> ReprocessPlan:
    request, _ = _case()
    return plan_reprocess(request)


def _construct_plan(
    plan: ReprocessPlan,
    prefixes: tuple[str, ...],
    deliveries: tuple[RawDeliveryBinding, ...] | None = None,
) -> ReprocessPlan:
    chosen = plan.deliveries if deliveries is None else deliveries
    contents = CreateReprocessPlanRequest(
        request=plan.request,
        prefixes=prefixes,
        contract_artifacts=plan.contract_artifacts,
        semantic_authority_digest=plan.semantic_authority_digest,
        deliveries=chosen,
    )
    return ReprocessPlan(
        request=plan.request,
        prefixes=prefixes,
        contract_artifacts=plan.contract_artifacts,
        semantic_authority_digest=plan.semantic_authority_digest,
        deliveries=chosen,
        digest=ReprocessPlan.digest_for(contents),
    )


def _contents(plan: ReprocessPlan) -> CreateReprocessPlanRequest:
    return CreateReprocessPlanRequest(
        request=plan.request,
        prefixes=plan.prefixes,
        contract_artifacts=plan.contract_artifacts,
        semantic_authority_digest=plan.semantic_authority_digest,
        deliveries=plan.deliveries,
    )


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize("value", ("ab", "bad value", "bad/value", "x" * 129))
def test_operation_identity_rejects_unsafe_or_out_of_range_values(value: str) -> None:
    with pytest.raises(ValueError):
        ReprocessOperationId(value)


@pytest.mark.covers("ADR-044")
def test_operation_identity_accepts_both_length_boundaries() -> None:
    assert ReprocessOperationId(" abc ").value == "abc"
    assert ReprocessOperationId("x" * 128).value == "x" * 128


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize("field", ("environment", "stack_name", "bucket"))
def test_request_requires_each_named_resource(field: str) -> None:
    plan = _planned()
    with pytest.raises(ValueError):
        replace(plan.request, **{field: ""})


@pytest.mark.covers("ADR-044")
def test_request_requires_unique_nonblank_explicit_contract_scope() -> None:
    plan = _planned()
    for contracts in ((), ("",), ("same", "same")):
        with pytest.raises(ValueError):
            replace(plan.request, contracts=contracts)


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize("field", ("key", "version_id", "name"))
def test_raw_object_requires_every_identity_field(field: str) -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError):
        replace(binding, **{field: ""})


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize(
    ("field", "value"),
    (("declared_byte_count", 0), ("actual_byte_count", 0), ("declared_byte_count", -1)),
)
def test_raw_object_requires_positive_byte_counts(field: str, value: int) -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError):
        replace(binding, **{field: value})


@pytest.mark.covers("ADR-044")
def test_raw_object_refuses_mismatch_in_either_byte_count_direction() -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError, match="byte count"):
        replace(binding, actual_byte_count=binding.declared_byte_count + 1)
    with pytest.raises(ValueError, match="byte count"):
        replace(binding, declared_byte_count=binding.actual_byte_count + 1)


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize("field", ("declared_checksum", "actual_checksum"))
def test_raw_object_requires_matching_nonempty_checksums(field: str) -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError):
        replace(binding, **{field: "" if field == "declared_checksum" else "sha256:other"})


@pytest.mark.covers("ADR-044")
def test_raw_delivery_requires_contract_manifest_version_and_checksum() -> None:
    delivery = _planned().deliveries[0]
    for changes in (
        {"contract_artifacts": ()},
        {"manifest_key": ""},
        {"manifest_version_id": ""},
        {"manifest_checksum": ""},
    ):
        with pytest.raises(ValueError):
            replace(delivery, **changes)


@pytest.mark.covers("ADR-044")
def test_raw_delivery_identity_binds_every_exact_version_field_in_order() -> None:
    delivery = _planned().deliveries[0]
    assert delivery.identity.split("|") == [
        ",".join(artifact.identity for artifact in delivery.contract_artifacts),
        delivery.manifest_key,
        delivery.manifest_version_id,
        delivery.manifest_checksum,
        delivery.first_observed_at.isoformat(),
        delivery.object.key,
        delivery.object.version_id,
        delivery.object.actual_checksum,
    ]


@pytest.mark.covers("ADR-044")
@pytest.mark.parametrize("prefix", ("", "/", "/root", "../unsafe", "safe/../unsafe"))
def test_plan_refuses_unsafe_or_root_prefixes(prefix: str) -> None:
    plan = _planned()
    with pytest.raises(ValueError):
        _construct_plan(plan, (prefix,))


@pytest.mark.covers("ADR-044")
def test_plan_requires_sorted_unique_prefixes_and_deliveries() -> None:
    plan = _planned()
    with pytest.raises(ValueError, match="prefixes"):
        _construct_plan(plan, ("z/source/", "a/source/"))
    with pytest.raises(ValueError, match="prefixes"):
        _construct_plan(plan, (plan.prefixes[0], plan.prefixes[0]))
    delivery = plan.deliveries[0]
    later = replace(delivery, manifest_version_id="zz-version")
    with pytest.raises(ValueError, match="deliveries"):
        _construct_plan(plan, plan.prefixes, (later, delivery))
    with pytest.raises(ValueError, match="deliveries"):
        _construct_plan(plan, plan.prefixes, (delivery, delivery))


@pytest.mark.covers("ADR-044")
def test_plan_digest_binds_scope_prefixes_and_every_delivery_version() -> None:
    plan = _planned()
    with pytest.raises(ValueError, match="digest"):
        replace(plan, digest="sha256:wrong")
    changed_request = replace(plan.request, bucket="another-explicit-bucket")
    changed_delivery = replace(plan.deliveries[0], manifest_version_id="another-version")
    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), request=changed_request)) != plan.digest
    )
    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), deliveries=(changed_delivery,)))
        != plan.digest
    )
    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), prefixes=("another/source/",)))
        != plan.digest
    )
    assert (
        ReprocessPlan.digest_for(
            replace(_contents(plan), semantic_authority_digest="sha256:" + "b" * 64)
        )
        != plan.digest
    )


@pytest.mark.covers("ADR-044")
def test_plan_digest_binds_contract_selection_and_exact_artifact_bytes() -> None:
    plan = _planned()
    delivery = plan.deliveries[0]
    artifact = delivery.contract_artifacts[0]
    changed_id = replace(
        delivery,
        contract_artifacts=(replace(artifact, contract_id="different-contract"),),
    )
    changed_digest = replace(
        delivery,
        contract_artifacts=(replace(artifact, digest="sha256:" + "b" * 64),),
    )

    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), deliveries=(changed_id,))) != plan.digest
    )
    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), deliveries=(changed_digest,)))
        != plan.digest
    )


@pytest.mark.covers("ADR-044")
def test_plan_digest_binds_selected_contract_without_a_matching_delivery() -> None:
    request, _raw = _case()
    operation = replace(
        request.request,
        contracts=("vendor-sales-by-asin", "vendor-traffic-by-asin"),
    )
    plan = plan_reprocess(replace(request, request=operation))
    changed = tuple(
        replace(artifact, digest="sha256:" + "b" * 64)
        if artifact.contract_id == "vendor-traffic-by-asin"
        else artifact
        for artifact in plan.contract_artifacts
    )

    assert len(plan.deliveries) == 1
    assert {artifact.contract_id for artifact in plan.contract_artifacts} == set(
        operation.contracts
    )
    assert (
        ReprocessPlan.digest_for(replace(_contents(plan), contract_artifacts=changed))
        != plan.digest
    )


@pytest.mark.covers("ADR-044")
def test_create_sorts_and_deduplicates_prefixes_and_sorts_deliveries() -> None:
    plan = _planned()
    delivery = plan.deliveries[0]
    later = replace(delivery, manifest_version_id="zz-version")
    created = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=plan.request,
            prefixes=("z/source/", "a/source/", "z/source/"),
            contract_artifacts=plan.contract_artifacts,
            semantic_authority_digest=plan.semantic_authority_digest,
            deliveries=(later, delivery),
        )
    )
    assert created.prefixes == ("a/source/", "z/source/")
    assert created.deliveries == (delivery, later)


@pytest.mark.covers("ADR-044")
def test_reprocess_values_are_frozen_and_slotted() -> None:
    plan = _planned()
    values = (
        plan.request.operation_id,
        plan.request,
        plan.deliveries[0].object,
        plan.deliveries[0],
        plan,
    )
    for value in values:
        with pytest.raises(FrozenInstanceError):
            _set_attribute(value, "unexpected", "changed")
        assert not hasattr(value, "__dict__")


@pytest.mark.covers("ADR-044")
def test_request_accepts_multiple_distinct_explicit_contracts() -> None:
    plan = _planned()
    request = replace(plan.request, contracts=("contract-a", "contract-b"))
    assert request.contracts == ("contract-a", "contract-b")


@pytest.mark.covers("ADR-044")
def test_raw_object_accepts_equal_positive_counts_and_checksums() -> None:
    binding = _planned().deliveries[0].object
    assert binding.declared_byte_count == binding.actual_byte_count
    assert binding.declared_byte_count > 0
    assert binding.declared_checksum == binding.actual_checksum
    assert binding.declared_checksum


@pytest.mark.covers("ADR-044")
def test_empty_declared_checksum_is_refused_even_when_both_sides_are_empty() -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError, match="checksum"):
        replace(binding, declared_checksum="", actual_checksum="")


@pytest.mark.covers("ADR-044")
def test_checksum_mismatch_is_refused_in_both_lexical_directions() -> None:
    binding = _planned().deliveries[0].object
    with pytest.raises(ValueError, match="checksum"):
        replace(binding, declared_checksum="sha256:a", actual_checksum="sha256:z")
    with pytest.raises(ValueError, match="checksum"):
        replace(binding, declared_checksum="sha256:z", actual_checksum="sha256:a")


@pytest.mark.covers("ADR-044")
def test_plan_requires_at_least_one_prefix() -> None:
    with pytest.raises(ValueError, match="prefix"):
        _construct_plan(_planned(), ())


@pytest.mark.covers("ADR-044")
def test_digest_comparison_refuses_values_on_both_sides_of_expected() -> None:
    plan = _planned()
    for digest in ("sha256:0000", "sha256:zzzz"):
        with pytest.raises(ValueError, match="digest"):
            replace(plan, digest=digest)


@pytest.mark.covers("ADR-044")
def test_digest_has_a_stable_canonical_json_encoding() -> None:
    artifact = ContractArtifact("contract-a", "sha256:" + "a" * 64)
    window = DateRange(date(2026, 8, 1), date(2026, 8, 2))
    request = ReprocessRequest(
        environment="dev",
        stack_name="Stack-dev",
        bucket="raw-bucket",
        contracts=(artifact.contract_id,),
        window=window,
        operation_id=ReprocessOperationId("replay-001"),
    )
    object_binding = RawObjectBinding(
        key="source/window=2026-08-01_2026-08-02/delivery.json",
        version_id="object-v1",
        name="delivery.json",
        declared_byte_count=2,
        actual_byte_count=2,
        declared_checksum="sha256:" + "b" * 64,
        actual_checksum="sha256:" + "b" * 64,
    )
    delivery = RawDeliveryBinding(
        contract_artifacts=(artifact,),
        manifest_key="source/window=2026-08-01_2026-08-02/manifest.json",
        manifest_version_id="manifest-v1",
        manifest_checksum="sha256:" + "c" * 64,
        first_observed_at=datetime(2026, 8, 3, tzinfo=UTC),
        object=object_binding,
        period=window,
    )
    contents = CreateReprocessPlanRequest(
        request=request,
        prefixes=("source/",),
        contract_artifacts=(artifact,),
        semantic_authority_digest="sha256:" + "d" * 64,
        deliveries=(delivery,),
    )

    assert ReprocessPlan.digest_for(contents) == (
        "sha256:cdee79f8174c8d8e3f71eba06a6e6754724a00aaad932ad480904275f1f42357"
    )


@pytest.mark.covers("ADR-044")
def test_positive_byte_count_validation_is_independent_of_equality() -> None:
    binding = _planned().deliveries[0].object
    assert replace(binding, declared_byte_count=1, actual_byte_count=1).actual_byte_count == 1
    for value in (0, -1):
        with pytest.raises(ValueError, match=r"positive .* byte count"):
            replace(binding, declared_byte_count=value, actual_byte_count=value)


@pytest.mark.covers("ADR-044", "ADR-047")
def test_apply_preflights_every_delivery_before_first_semantic_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = json.dumps(
        {
            "reportSpecification": {
                "reportType": "GET_VENDOR_SALES_REPORT",
                "marketplaceIds": ["ATVPDKIKX0DER"],
                "dataStartTime": "2026-08-10",
                "dataEndTime": "2026-08-10",
                "lastUpdatedDate": "2026-08-11T00:00:00Z",
                "reportOptions": {
                    "reportPeriod": "DAY",
                    "distributorView": "MANUFACTURING",
                    "sellingProgram": "RETAIL",
                },
            },
            "salesByAsin": [
                {
                    "asin": "B0PREFLT01",
                    "startDate": "2026-08-10",
                    "shippedRevenue": {"amount": "1.0000", "currencyCode": "USD"},
                    "shippedCogs": {"amount": "0.5000"},
                    "orderedRevenue": {"amount": "1.0000"},
                    "shippedUnits": 1,
                    "orderedUnits": 1,
                    "customerReturns": 0,
                }
            ],
        }
    ).encode()
    malformed = b"{"
    columns = declared_columns_of(valid.decode(), source_format="json_dual_array")
    contract_path, contract = next(
        item for item in load_contracts(ROOT) if item[1].contract_id == "vendor-sales-by-asin"
    )
    contract_artifact = ContractArtifact(
        contract.contract_id,
        checksum_of(contract_path.read_bytes()),
    )

    def delivery(
        body: bytes,
        retrieved_at: datetime,
        suffix: str,
    ) -> tuple[RawDeliveryBinding, dict[tuple[str, str], bytes]]:
        object_version = f"object-{suffix}"
        manifest_version = f"manifest-{suffix}"
        manifest = build_manifest(
            BuildManifestRequest(
                declaration={
                    "source_system": "vendor_central",
                    "source_dataset": "GET_VENDOR_SALES_REPORT",
                    "source_scope": {
                        "namespace": "vendor_central.vendor_group",
                        "value": "fictional-preflight-vendor-group",
                    },
                    "option_set": {
                        "reportPeriod": "DAY",
                        "distributorView": "MANUFACTURING",
                        "sellingProgram": "RETAIL",
                    },
                    "marketplace": "ATVPDKIKX0DER",
                    "period_start": "2026-08-10",
                    "period_end": "2026-08-10",
                    "source_watermark": "2026-08-11",
                    "retrieved_at": retrieved_at.isoformat(),
                },
                objects=(
                    LandedObject(
                        "delivery.json",
                        len(body),
                        checksum_of(body),
                        version_id=object_version,
                    ),
                ),
                declared_columns=columns,
            )
        )
        manifest_body = write_manifest(manifest).encode()
        object_key = manifest.object_keys()[0]
        return (
            RawDeliveryBinding(
                contract_artifacts=(contract_artifact,),
                manifest_key=manifest.manifest_key,
                manifest_version_id=manifest_version,
                manifest_checksum=checksum_of(manifest_body),
                first_observed_at=retrieved_at,
                object=RawObjectBinding(
                    key=object_key,
                    version_id=object_version,
                    name="delivery.json",
                    declared_byte_count=len(body),
                    actual_byte_count=len(body),
                    declared_checksum=checksum_of(body),
                    actual_checksum=checksum_of(body),
                ),
                period=DateRange(date(2026, 8, 10), date(2026, 8, 10)),
            ),
            {
                (manifest.manifest_key, manifest_version): manifest_body,
                (object_key, object_version): body,
            },
        )

    first, first_bodies = delivery(valid, datetime(2026, 8, 12, tzinfo=UTC), "valid")
    second, second_bodies = delivery(
        malformed,
        datetime(2026, 8, 13, tzinfo=UTC),
        "malformed",
    )
    request, _unused = _case()
    operation = replace(
        request.request,
        window=DateRange(date(2026, 8, 10), date(2026, 8, 10)),
    )
    plan = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=operation,
            prefixes=("vendor_central/GET_VENDOR_SALES_REPORT/",),
            contract_artifacts=tuple(
                artifact
                for artifact in request.contract_artifacts
                if artifact.contract_id in operation.contracts
            ),
            semantic_authority_digest=request.semantic_authority_digest,
            deliveries=(first, second),
        )
    )
    calls: list[object] = []
    progress: list[ReprocessProgress] = []

    def record_sidecar(sidecar_request: object, tooling: object) -> tuple[()]:
        calls.append((sidecar_request, tooling))
        return ()

    monkeypatch.setattr(reprocess_job, "run_semantic_sidecar", record_sidecar)

    with pytest.raises(ReaderError, match="not valid JSON"):
        apply_reprocess(
            ApplyReprocessRequest(
                plan=plan,
                contracts=request.contracts,
                registry=load_registry(ROOT),
                raw=MemoryRaw((), {**first_bodies, **second_bodies}),
                semantic=cast("SemanticSidecarTooling", object()),
                execution_id="test-preflight-execution",
                applied_at=datetime(2026, 8, 24, tzinfo=UTC),
                contract_artifacts=request.contract_artifacts,
                semantic_authority_digest=request.semantic_authority_digest,
                progress=progress.append,
            )
        )

    assert calls == []
    assert [(item.stage, item.completed, item.total) for item in progress] == [("preflight", 1, 4)]


@pytest.mark.covers("ADR-044", "ADR-047")
def test_apply_refuses_contract_artifact_drift_before_any_write() -> None:
    request, raw = _case()
    operation = replace(
        request.request,
        contracts=("vendor-sales-by-asin", "vendor-traffic-by-asin"),
    )
    expanded = replace(request, request=operation)
    plan = plan_reprocess(expanded)
    changed = tuple(
        replace(artifact, digest="sha256:" + "b" * 64)
        if artifact.contract_id == "vendor-traffic-by-asin"
        else artifact
        for artifact in request.contract_artifacts
    )

    with pytest.raises(ValueError, match="contract artifacts differ"):
        apply_reprocess(
            ApplyReprocessRequest(
                plan=plan,
                contracts=request.contracts,
                registry=cast("ColumnRegistry", object()),
                raw=raw,
                semantic=cast("SemanticSidecarTooling", object()),
                execution_id="artifact-drift-execution",
                applied_at=datetime(2026, 8, 24, tzinfo=UTC),
                contract_artifacts=changed,
                semantic_authority_digest=request.semantic_authority_digest,
            )
        )


@pytest.mark.covers("ADR-044", "ADR-047")
def test_apply_refuses_semantic_authority_drift_before_any_write() -> None:
    request, raw = _case()
    plan = plan_reprocess(request)

    with pytest.raises(ValueError, match="semantic authority differs"):
        apply_reprocess(
            ApplyReprocessRequest(
                plan=plan,
                contracts=request.contracts,
                registry=cast("ColumnRegistry", object()),
                raw=raw,
                semantic=cast("SemanticSidecarTooling", object()),
                execution_id="semantic-authority-drift-execution",
                applied_at=datetime(2026, 8, 24, tzinfo=UTC),
                contract_artifacts=request.contract_artifacts,
                semantic_authority_digest="sha256:" + "b" * 64,
            )
        )


@pytest.mark.covers("ADR-044", "ADR-047")
def test_apply_empty_exact_plan_performs_no_rediscovery_or_legacy_write() -> None:
    request, raw = _case()
    planned = plan_reprocess(request)
    empty = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=planned.request,
            prefixes=planned.prefixes,
            contract_artifacts=planned.contract_artifacts,
            semantic_authority_digest=planned.semantic_authority_digest,
            deliveries=(),
        )
    )
    raw.prefixes.clear()
    result = apply_reprocess(
        ApplyReprocessRequest(
            plan=empty,
            contracts=request.contracts,
            registry=cast("ColumnRegistry", object()),
            raw=raw,
            semantic=cast("SemanticSidecarTooling", object()),
            execution_id="test-execution",
            applied_at=datetime(2026, 8, 24, tzinfo=UTC),
            contract_artifacts=request.contract_artifacts,
            semantic_authority_digest=request.semantic_authority_digest,
        )
    )
    assert result == ()
    assert raw.prefixes == []


@pytest.mark.covers("ADR-044", "ADR-047")
@pytest.mark.parametrize("predecessor", (" ", "test-execution"))
def test_apply_reprocess_refuses_invalid_predecessor_lineage(predecessor: str) -> None:
    request, raw = _case()
    planned = plan_reprocess(request)
    empty = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=planned.request,
            prefixes=planned.prefixes,
            contract_artifacts=planned.contract_artifacts,
            semantic_authority_digest=planned.semantic_authority_digest,
            deliveries=(),
        )
    )

    with pytest.raises(ValueError, match="predecessor"):
        ApplyReprocessRequest(
            plan=empty,
            contracts=request.contracts,
            registry=cast("ColumnRegistry", object()),
            raw=raw,
            semantic=cast("SemanticSidecarTooling", object()),
            execution_id="test-execution",
            applied_at=datetime(2026, 8, 24, tzinfo=UTC),
            predecessor_execution_id=predecessor,
            contract_artifacts=request.contract_artifacts,
            semantic_authority_digest=request.semantic_authority_digest,
        )


@pytest.mark.covers("ADR-044", "ADR-047")
def test_apply_plan_guard_requires_nonempty_reviewed_digest() -> None:
    planned = _planned()
    empty = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=planned.request,
            prefixes=planned.prefixes,
            contract_artifacts=planned.contract_artifacts,
            semantic_authority_digest=planned.semantic_authority_digest,
            deliveries=(),
        )
    )

    with pytest.raises(ValueError, match="empty plan"):
        reprocess_cli._require_expected_plan(  # pyright: ignore[reportPrivateUsage]
            empty,
            empty.digest,
        )
    with pytest.raises(ValueError, match="reviewed dry run"):
        reprocess_cli._require_expected_plan(  # pyright: ignore[reportPrivateUsage]
            planned,
            "sha256:different",
        )

    reprocess_cli._require_expected_plan(  # pyright: ignore[reportPrivateUsage]
        planned,
        planned.digest,
    )


@pytest.mark.covers("ADR-044")
def test_apply_progress_is_monotonic_across_preflight_and_durable_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, raw = _case()
    plan = plan_reprocess(request)
    progress: list[ReprocessProgress] = []
    sidecar_calls: list[object] = []
    preflight_calls: list[object] = []

    @dataclass(frozen=True)
    class PreparedDouble:
        contracts: tuple[object, ...] = ()
        progress: object | None = None

    def complete_preflight(
        _request: ApplyReprocessRequest,
        delivery: object,
        _contracts: object,
    ) -> object:
        preflight_calls.append(delivery)
        return PreparedDouble()

    def complete_sidecar(sidecar_request: object, _tooling: object) -> tuple[()]:
        sidecar_calls.append(sidecar_request)
        return ()

    monkeypatch.setattr(reprocess_job, "_prepare_reprocess_delivery", complete_preflight)
    monkeypatch.setattr(reprocess_job, "run_semantic_sidecar", complete_sidecar)

    outcomes = apply_reprocess(
        ApplyReprocessRequest(
            plan=plan,
            contracts=request.contracts,
            registry=load_registry(ROOT),
            raw=raw,
            semantic=cast("SemanticSidecarTooling", object()),
            execution_id="progress-execution",
            applied_at=datetime(2026, 8, 24, tzinfo=UTC),
            contract_artifacts=request.contract_artifacts,
            semantic_authority_digest=request.semantic_authority_digest,
            progress=progress.append,
        )
    )

    delivery_count = len(plan.deliveries)
    assert outcomes == ()
    assert len(preflight_calls) == delivery_count
    assert len(sidecar_calls) == delivery_count
    assert [item.completed for item in progress] == list(range(1, delivery_count * 2 + 1))
    assert [item.stage for item in progress] == ["preflight"] * delivery_count + [
        "apply"
    ] * delivery_count
    assert all(item.total == delivery_count * 2 for item in progress)


@pytest.mark.covers("ADR-044")
def test_apply_cancellation_refuses_before_preflight_or_write() -> None:
    request, raw = _case()
    plan = plan_reprocess(request)
    progress: list[ReprocessProgress] = []

    with pytest.raises(ReprocessCancelledError):
        apply_reprocess(
            ApplyReprocessRequest(
                plan=plan,
                contracts=request.contracts,
                registry=cast("ColumnRegistry", object()),
                raw=raw,
                semantic=cast("SemanticSidecarTooling", object()),
                execution_id="cancelled-execution",
                applied_at=datetime(2026, 8, 24, tzinfo=UTC),
                contract_artifacts=request.contract_artifacts,
                semantic_authority_digest=request.semantic_authority_digest,
                progress=progress.append,
                cancelled=lambda: True,
            )
        )

    assert progress == []


# --- S6 (D22): apply refuses in GRAPH before any write --------------------------------------------

_APPLY_ARGUMENTS = (
    "--profile",
    "explicit-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
    "--stack",
    "Adlytics-DataV2-dev",
    "--bucket",
    "explicit-versioned-raw",
    "--window-start",
    "2026-08-01",
    "--window-end",
    "2026-08-01",
    "--operation-id",
    "reprocess-unit",
    "--contract",
    "vendor-sales-by-asin",
    "--apply",
    "--expected-plan-digest",
    "sha256:reviewed",
    "--graph-store-stack",
    "Adlytics-ControlPlaneAuthorityStore-dev",
)


class _ApplySession:
    """An operator session whose only real answer is the caller identity."""

    def __init__(self) -> None:
        self._session = object()
        self.clients: list[str] = []

    def client(self, service: str) -> object:
        self.clients.append(service)
        if service == "sts":
            return _CallerAccount()
        return object()


class _CallerAccount:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "000000000000"}


@dataclass
class _ApplyWorld:
    session: _ApplySession
    mode_reads: list[str]
    resolved_tables: list[tuple[str, str]]
    write_path: list[str]
    errors: list[BaseException]


class _WritePathReachedError(Exception):
    """The apply passed its mode gate and reached the semantic write path."""


def _apply_world(monkeypatch: pytest.MonkeyPatch, mode: object) -> _ApplyWorld:
    from types import SimpleNamespace  # noqa: PLC0415

    from domain.operations.execution_graph import ModeState  # noqa: PLC0415

    world = _ApplyWorld(_ApplySession(), [], [], [], [])

    class _GraphStore:
        def __init__(self, table: str, _client: object) -> None:
            self.table = table

        def read_mode(self) -> ModeState:
            world.mode_reads.append(self.table)
            return ModeState(cast("Any", mode), 4)

    def resolve(_session: object, environment: str, stack: str) -> str:
        world.resolved_tables.append((environment, stack))
        return "fictional-execution-graph"

    def reached(*_args: object, **_kwargs: object) -> object:
        world.write_path.append("resolve_named_semantic_resources")
        raise _WritePathReachedError

    def record(error: BaseException, _report: object) -> None:
        world.errors.append(error)

    plan = SimpleNamespace(digest="sha256:reviewed", deliveries=(object(),))

    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    def answer(value: object) -> Callable[..., object]:
        def returns(*_args: object, **_kwargs: object) -> object:
            return value

        return returns

    def session(**_kwargs: object) -> _ApplySession:
        return world.session

    stubs: dict[str, object] = {
        "configure_tracing": answer(False),
        "verify_named_raw_bucket": nothing,
        "_installed_contracts": answer(SimpleNamespace(contracts=(), artifacts=())),
        "_semantic_authority_digest": answer("sha256:authority"),
        "load_source_packs": answer(()),
        "load_query_packs": answer(()),
        "S3VersionedRawReader": answer(object()),
        "PlanReprocessRequest": nothing,
        "plan_reprocess": answer(plan),
        "_report": nothing,
        "_lifecycle": nothing,
        "resolve_graph_table": resolve,
        "DynamoExecutionGraphStore": _GraphStore,
        "resolve_named_semantic_resources": reached,
        "report_error": record,
        "flush_metrics": nothing,
        "flush_traces": nothing,
    }
    for name, value in stubs.items():
        monkeypatch.setattr(reprocess_cli, name, value)
    monkeypatch.setattr(reprocess_cli.boto3, "Session", session)
    return world


def test_d22_reprocess_apply_refuses_in_graph_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.operations.execution_graph import AuthorityMode  # noqa: PLC0415
    from domain.shared_kernel.errors import ValidationError  # noqa: PLC0415

    world = _apply_world(monkeypatch, AuthorityMode.GRAPH)
    assert reprocess_cli.main(list(_APPLY_ARGUMENTS)) == 1
    (error,) = world.errors
    assert isinstance(error, ValidationError)
    assert str(error.code) == "ADLY-REPROCESS-0001"
    assert world.resolved_tables == [("dev", "Adlytics-ControlPlaneAuthorityStore-dev")]
    assert world.mode_reads == ["fictional-execution-graph"]  # one strong mode read
    assert world.write_path == []  # refused before any semantic resource or write


@pytest.mark.parametrize("mode_name", ["SHADOW", "LEGACY", "LEGACY_DRAINING"])
def test_d22_control_reprocess_apply_proceeds_outside_graph(
    monkeypatch: pytest.MonkeyPatch, mode_name: str
) -> None:
    from domain.operations.execution_graph import AuthorityMode  # noqa: PLC0415

    world = _apply_world(monkeypatch, AuthorityMode[mode_name])
    assert reprocess_cli.main(list(_APPLY_ARGUMENTS)) == 1
    (error,) = world.errors
    assert isinstance(error, _WritePathReachedError)
    assert world.mode_reads == ["fictional-execution-graph"]
    assert world.write_path == ["resolve_named_semantic_resources"]


def test_d22_apply_requires_the_graph_store_stack_before_aws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(**_kwargs: object) -> object:
        pytest.fail("no AWS session may be built before the arguments are complete")

    monkeypatch.setattr(reprocess_cli.boto3, "Session", unexpected)
    without = [item for item in _APPLY_ARGUMENTS if item != "--graph-store-stack"]
    without.remove("Adlytics-ControlPlaneAuthorityStore-dev")
    with pytest.raises(SystemExit) as refusal:
        reprocess_cli.main(without)
    assert refusal.value.code == 2


def test_d22_the_mode_gate_names_graph_only() -> None:
    from domain.operations.execution_graph import AuthorityMode  # noqa: PLC0415
    from domain.shared_kernel.errors import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError) as refused:
        reprocess_cli.require_reprocess_mode(AuthorityMode.GRAPH)
    assert str(refused.value.code) == "ADLY-REPROCESS-0001"
    for mode in (AuthorityMode.LEGACY, AuthorityMode.SHADOW, AuthorityMode.LEGACY_DRAINING):
        reprocess_cli.require_reprocess_mode(mode)
