# SPDX-License-Identifier: MIT-0
"""Deployment-seed authority over the insight prompt registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import adapters.jobs.prompt_registry_seed.handler as seed_handler
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.prompt_registry_seed.handler import seed_contracts
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import INSIGHT_PROMPT_REGISTRY_SEED
from domain.presentation.insights import (
    DETERMINISTIC_SLOT_IDS,
    InsightModelRoute,
    InsightPromptActivation,
    InsightPromptSeedRequest,
    InsightPromptTier,
    InsightSlotId,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-069")]

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RecordingRegistry:
    pointers: dict[InsightSlotId, InsightPromptActivation] = field(
        default_factory=dict[InsightSlotId, InsightPromptActivation]
    )
    seeded: list[tuple[InsightSlotId, str]] = field(default_factory=list[tuple[InsightSlotId, str]])
    requests: list[InsightPromptSeedRequest] = field(default_factory=list[InsightPromptSeedRequest])

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        assert tier is InsightPromptTier.CARD, "seed authority is card-tier only"
        return self.pointers.get(slot_id)

    def seed_v1(self, request: InsightPromptSeedRequest) -> None:
        self.requests.append(request)
        self.seeded.append((request.slot_id, request.prompt))


def _contracts() -> tuple[object, ...]:
    return tuple(contract for _, contract in load_insight_contracts(ROOT))


def test_seed_installs_every_prompted_slot_and_skips_declared_null_prompts() -> None:
    registry = RecordingRegistry()

    seed_contracts(_contracts(), registry)  # type: ignore[arg-type]

    seeded_ids = {slot_id for slot_id, _ in registry.seeded}
    assert InsightSlotId.MOVERS not in seeded_ids
    assert InsightSlotId.RETAIL_MOVERS not in seeded_ids
    assert InsightSlotId.NETWORK_SCORECARD not in seeded_ids
    assert seeded_ids.isdisjoint(DETERMINISTIC_SLOT_IDS)
    assert (
        len(registry.seeded) == len(seeded_ids) == len(InsightSlotId) - len(DETERMINISTIC_SLOT_IDS)
    )


def test_one_run_records_one_aware_seed_time_on_every_slot() -> None:
    registry = RecordingRegistry()

    seed_contracts(_contracts(), registry)  # type: ignore[arg-type]

    times = {request.created_at for request in registry.requests}
    [seeded_at] = times
    assert seeded_at.tzinfo is not None
    assert seeded_at.utcoffset() == timedelta(0)
    assert {request.model_route for request in registry.requests} == {InsightModelRoute.QUICK}


def test_seed_never_touches_an_operator_activated_later_version() -> None:
    """A v2+ pointer is registry-governed; re-firing the seed digest must not conflict."""
    contracts = _contracts()
    registry = RecordingRegistry(
        pointers={
            contract.id: InsightPromptActivation(  # type: ignore[union-attr]
                contract.id,  # type: ignore[union-attr]
                "v2",
                "An operator-authored rewrite.",
                InsightModelRoute.QUICK,
            )
            for contract in contracts
            if contract.initial_fictional_prompt is not None  # type: ignore[union-attr]
        }
    )

    seed_contracts(contracts, registry)  # type: ignore[arg-type]

    assert registry.seeded == []


def test_seed_refuses_a_v1_pointer_whose_text_drifted_from_the_contract() -> None:
    contracts = _contracts()
    prompted = next(
        contract
        for contract in contracts
        if contract.initial_fictional_prompt is not None  # type: ignore[union-attr]
    )
    registry = RecordingRegistry(
        pointers={
            prompted.id: InsightPromptActivation(  # type: ignore[union-attr]
                prompted.id,  # type: ignore[union-attr]
                "v1",
                "Text that no longer matches the governed contract.",
                InsightModelRoute.QUICK,
            )
        }
    )

    with pytest.raises(RuntimeError, match="conflicts with the governed contract"):
        seed_contracts(contracts, registry)  # type: ignore[arg-type]


class _Context:
    function_name = "prompt-registry-seed"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:prompt-seed"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


def test_handler_records_contract_count_without_prompt_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    contracts = _contracts()
    monkeypatch.setattr(seed_handler, "_composition", lambda: (contracts, RecordingRegistry()))

    result = seed_handler.handler({"RequestType": "Create"}, _Context())

    assert result == {"PhysicalResourceId": "insight-prompt-registry-v1"}
    (root,) = [
        item for item in exporter.get_finished_spans() if item.name == INSIGHT_PROMPT_REGISTRY_SEED
    ]
    assert dict(root.attributes or {}) == {
        "operation": INSIGHT_PROMPT_REGISTRY_SEED,
        "request_type": "Create",
        "outcome": "completed",
        "contract_count": len(contracts),
    }


def test_handler_reports_composition_failure_and_preserves_the_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("composition unavailable")
    reported: list[BaseException] = []

    def fail_composition() -> object:
        raise error

    monkeypatch.setattr(seed_handler, "_composition", fail_composition)

    def capture_error(caught: BaseException, _report: object) -> None:
        reported.append(caught)

    monkeypatch.setattr(seed_handler, "report_error", capture_error)

    with pytest.raises(RuntimeError, match="composition unavailable"):
        seed_handler.handler({"RequestType": "Update"}, _Context())

    assert reported == [error]
