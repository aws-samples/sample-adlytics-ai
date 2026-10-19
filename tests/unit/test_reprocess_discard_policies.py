# SPDX-License-Identifier: MIT-0
"""The reprocess job assesses a delivery under its installed discard policy (P-1, 2026-10-16).

D2 live acceptance found the GRAPH materialize path omitting the installed discard policies;
the parity audit found the operator reprocess job building its `AssessmentRequest` without
them too, so a reprocessed `records_cell_state` delivery was refused CLOSED_ENUMERATION. Each
control runs the real reprocess apply on in-memory tooling, beside the delivery handler's
assessment of the same bytes.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from test_new_to_brand_buyers import (
    FACT,
    KEPT,
    NOW,
    PACK_ID,
    ROOT,
    WEEK,
    WITHHELD,
    _csv,  # pyright: ignore[reportPrivateUsage]
    _delivery,  # pyright: ignore[reportPrivateUsage]
)
from test_reprocess_semantic_v2 import MemoryRaw
from test_semantic_sidecar import (
    MemoryCanonical,
    _case,  # pyright: ignore[reportPrivateUsage]
)

from adapters.jobs.contract_validation.loader import load_contracts, load_registry
from adapters.jobs.curation.manifests import checksum_of, write_manifest
from adapters.jobs.ingest_delivery import semantic_ingestion
from adapters.jobs.ingest_delivery.landing import LoadedDelivery
from adapters.jobs.ingest_delivery.semantic_ingestion import (
    SemanticIngestionRequest,
    ingest_semantic_delivery,
)
from adapters.jobs.reprocess_semantic_v2 import __main__ as reprocess_cli
from adapters.jobs.reprocess_semantic_v2 import job as reprocess_job
from adapters.jobs.reprocess_semantic_v2.job import ApplyReprocessRequest, apply_reprocess
from adapters.producer.packs import discard_policies, installed_governance, load_query_packs
from domain.curation.ingestion import AssessmentRequest, DiscardPolicy
from domain.curation.landing import dataset_prefix
from domain.semantics.reprocessing import (
    ContractArtifact,
    CreateReprocessPlanRequest,
    RawDeliveryBinding,
    RawObjectBinding,
    ReprocessOperationId,
    ReprocessPlan,
    ReprocessRequest,
)
from ports.outbound.canonical import CanonicalDestination

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-099")]

INSTALLED = discard_policies(load_query_packs(ROOT))


class _StopError(Exception):
    """Raised by an assessment spy once it has recorded its request."""


def _apply(delivery: LoadedDelivery, policies: tuple[DiscardPolicy, ...], tooling: Any) -> None:
    """Apply one exact reprocess of `delivery` through the real job on `tooling`."""
    manifest = delivery.manifest
    manifest_body = write_manifest(manifest).encode()
    body = delivery.text.encode()
    loaded = load_contracts(ROOT)
    artifacts = tuple(
        ContractArtifact(contract.contract_id, checksum_of(path.read_bytes()))
        for path, contract in loaded
    )
    contract = next(item for _, item in loaded if item.contract_id == PACK_ID)
    binding = RawDeliveryBinding(
        contract_artifacts=tuple(item for item in artifacts if item.contract_id == PACK_ID),
        manifest_key=manifest.manifest_key,
        manifest_version_id="manifest-v1",
        manifest_checksum=checksum_of(manifest_body),
        first_observed_at=manifest.retrieved_at,
        object=RawObjectBinding(
            key=manifest.object_keys()[0],
            version_id="object-v1",
            name="data-0001.csv",
            declared_byte_count=len(body),
            actual_byte_count=len(body),
            declared_checksum=checksum_of(body),
            actual_checksum=checksum_of(body),
        ),
        period=WEEK,
    )
    authority = reprocess_cli._semantic_authority_digest()  # pyright: ignore[reportPrivateUsage]
    plan = ReprocessPlan.create(
        CreateReprocessPlanRequest(
            request=ReprocessRequest(
                environment="dev",
                stack_name="Adlytics-Foundation-dev",
                bucket="explicit-versioned-raw",
                contracts=(PACK_ID,),
                window=WEEK,
                operation_id=ReprocessOperationId("ntb-discard-replay"),
            ),
            prefixes=(dataset_prefix(contract),),
            contract_artifacts=binding.contract_artifacts,
            semantic_authority_digest=authority,
            deliveries=(binding,),
        )
    )
    apply_reprocess(
        ApplyReprocessRequest(
            plan=plan,
            contracts=tuple(item for _, item in loaded),
            registry=load_registry(ROOT),
            raw=MemoryRaw(
                (),
                {
                    (manifest.manifest_key, "manifest-v1"): manifest_body,
                    (manifest.object_keys()[0], "object-v1"): body,
                },
            ),
            semantic=tooling,
            execution_id="ntb-discard-reprocess",
            applied_at=NOW,
            installed_governance=installed_governance(load_query_packs(ROOT)),
            discard_policies=policies,
            contract_artifacts=artifacts,
            semantic_authority_digest=authority,
        )
    )


def _fact_requests(tooling: Any) -> tuple[Any, ...]:
    canonical = tooling.canonical_writer
    assert isinstance(canonical, MemoryCanonical)
    return tuple(
        item
        for item in canonical.requests
        if item.publication.partition.scope.dataset.value == FACT
    )


def test_reprocess_and_the_delivery_handler_assess_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `AssessmentRequest` field the reprocess job builds equals the handler's own."""
    delivery = _delivery(_csv(KEPT, WITHHELD))
    seen: list[AssessmentRequest] = []

    def spy(request: AssessmentRequest) -> Any:
        seen.append(request)
        raise _StopError

    monkeypatch.setattr(semantic_ingestion, "assess_source_delivery", spy)
    monkeypatch.setattr(reprocess_job, "assess_source_delivery", spy)
    _request, tooling, _order, _attempts = _case()
    with pytest.raises(_StopError):
        ingest_semantic_delivery(
            SemanticIngestionRequest(
                delivery=delivery,
                contracts=tuple(
                    contract
                    for _, contract in load_contracts(ROOT)
                    if contract.contract_id == PACK_ID
                ),
                columns=load_registry(ROOT),
                now=NOW,
                operation_id="ntb-parity",
                execution_id="ntb-parity-execution",
                plan_digest="sha256:ntb-parity-plan",
                installed_governance=installed_governance(load_query_packs(ROOT)),
                discard_policies=INSTALLED,
            ),
            tooling,
        )
    with pytest.raises(_StopError):
        _apply(delivery, INSTALLED, tooling)
    handler, reprocess = seen
    names = tuple(item.name for item in dataclasses.fields(AssessmentRequest))
    assert names == (
        "manifest",
        "document",
        "loads",
        "registry",
        "discard_policies",
        "variant_evidence",
    )
    assert {name: getattr(reprocess, name) for name in names} == {
        name: getattr(handler, name) for name in names
    }
    assert reprocess.discard_policies == INSTALLED


def test_a_reprocess_discards_the_withheld_row_and_publishes_the_kept_one() -> None:
    """Positive control: the candidate holds only the kept row, and it reaches successful."""
    _request, tooling, _order, _attempts = _case()
    _apply(_delivery(_csv(KEPT, WITHHELD)), INSTALLED, tooling)
    requests = _fact_requests(tooling)
    assert tuple(item.destination for item in requests) == (
        CanonicalDestination.CANDIDATE,
        CanonicalDestination.SUCCESSFUL,
    )
    candidate = requests[0]
    assert tuple(dict(row) for row in candidate.rows) == (
        {
            "ad_product_key": None,
            "basis_key": "ad_attributed",
            "buyers_attributed": 9000,
            "buyers_new_to_brand_12mo": 4400,
            "campaign_key": None,
            "cell_state": "returned",
            "channel_group_key": None,
            "channel_key": None,
            "ecosystem_key": "amazon",
            "grain_set": "platform",
            "identity_unit": "amazon_user_id",
            "marketplace_key": "amazon_us",
            "period_end": WEEK.end,
            "period_start": WEEK.start,
            "platform_key": "amazon_ads",
            "source_timezone": "UTC",
            "time_grain": "week",
        },
    )
    evidence = {item.rule_id: item for item in candidate.publication.evidence.results}
    assert evidence[f"contract.{PACK_ID}.v1"].outcome.value == "pass"
    assert evidence[f"suppression:{PACK_ID}.cell-loss.v1"].detail == (
        '{"discarded_rows":1,"expected_cells":1,"grade":"not_degraded",'
        '"loss_ratio":"0","not_returned_cells":0}'
    )


def test_a_reprocess_without_the_policies_refuses_closed_enumeration_at_assessment() -> None:
    """Negative control: the pre-fix input quarantines at the contract assertion, nothing else."""
    _request, tooling, _order, _attempts = _case()
    _apply(_delivery(_csv(KEPT, WITHHELD)), (), tooling)
    requests = _fact_requests(tooling)
    assert tuple(item.destination for item in requests) == (CanonicalDestination.CANDIDATE,)
    failed = [
        (item.rule_id, item.family.value, item.outcome.value, item.detail)
        for item in requests[0].publication.evidence.results
        if item.outcome.value != "pass"
    ]
    assert failed == [
        (
            f"contract.{PACK_ID}.v1",
            "contract_assertions",
            "fail",
            f"{PACK_ID}: records cell_state, so its delivery must come from a governed pack "
            "that declares discard_suppressed_rows; this one doesn't, so its withheld rows "
            "can't be discarded or its missing cells written not_returned.",
        )
    ]


class _NoReads:
    """Semantic tooling that refuses every attribute read."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the reprocess job read semantic tooling .{name}")


def test_without_policies_the_reprocess_reads_no_variant_evidence_and_the_handler_reads_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The short-circuit is the handler's own result: no policy, no automatic variant, `{}`."""
    from adapters.jobs.ingest_delivery.variant_evidence import (  # noqa: PLC0415
        VariantEvidenceRequest,
        read_variant_evidence,
    )

    delivery = _delivery(_csv(KEPT, WITHHELD))
    seen: list[AssessmentRequest] = []

    def spy(request: AssessmentRequest) -> Any:
        seen.append(request)
        raise _StopError

    monkeypatch.setattr(reprocess_job, "assess_source_delivery", spy)
    with pytest.raises(_StopError):
        _apply(delivery, (), _NoReads())
    (reprocess,) = seen
    assert reprocess.discard_policies == ()
    assert dict(reprocess.variant_evidence) == {}
    # The delivery handler's own read of the same manifest with no policy is also empty.
    _request, tooling, _order, _attempts = _case()
    contracts = tuple(c for _, c in load_contracts(ROOT) if c.contract_id == PACK_ID)
    assert (
        read_variant_evidence(
            VariantEvidenceRequest(
                manifest=delivery.manifest,
                contracts=contracts,
                discard_policies=(),
                packs=tooling.packs,
                bridge=tooling.bridge,
                publications=tooling.publications,
                governed=contracts,
            )
        )
        == {}
    )
