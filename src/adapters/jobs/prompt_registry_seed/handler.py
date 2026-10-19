# SPDX-License-Identifier: MIT-0
"""Deployment-time idempotent seed for reviewed v1 insight prompt contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

from aws_lambda_powertools import Logger

from adapters.config import prompt_registry_seed_settings, tracing_settings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    INSIGHT_PROMPT_REGISTRY_SEED,
    span,
    trace_boundary,
)
from domain.presentation.insights import InsightPromptSeedRequest, InsightPromptTier

if TYPE_CHECKING:  # pragma: no cover
    from domain.presentation.insights import (
        InsightContract,
        InsightPromptActivation,
        InsightSlotId,
    )

_PHYSICAL_RESOURCE_ID = "insight-prompt-registry-v1"
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-insight-prompt-registry-seed")
_OPERATION = INSIGHT_PROMPT_REGISTRY_SEED
_COMPONENT = "prompt_registry_seed"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


class SeedableRegistry(Protocol):
    """The two registry capabilities the deployment seed is allowed to use."""

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        """Read the active pointer whose existence ends the seed's authority."""
        ...

    def seed_v1(self, request: InsightPromptSeedRequest) -> None:
        """Idempotently install one reviewed v1 record and its pointer."""
        ...


def seed_contracts(
    contracts: tuple[InsightContract, ...],
    registry: SeedableRegistry,
) -> None:
    """Install reviewed v1 records where no pointer exists; never touch operator versions.

    Seed authority is card-tier only (ADR-076 slice D): the deep tier has no
    contract-declared prompt source, receives no seed, and its prompts enter the
    registry exclusively through create → calibrate → activate. Every v1 this run installs
    records the run's start as its creation time.
    """
    seeded_at = datetime.now(UTC)
    for contract in contracts:
        prompt = contract.initial_fictional_prompt
        if prompt is None:
            # A deterministic slot (movers, retail-movers) declares no prompt; the
            # declared null — not a hardcoded slot list — is what exempts it.
            continue
        existing = registry.active(contract.id, InsightPromptTier.CARD)
        if existing is not None:
            # An operator-activated later version (v2+) governs the slot through the registry
            # API; the seed's authority ends once any pointer exists. Only a v1 pointer whose
            # text drifted from the governed contract is a retained-state conflict.
            if existing.version == "v1" and existing.prompt != prompt:
                raise RuntimeError("retained v1 prompt conflicts with the governed contract.")
            continue
        registry.seed_v1(InsightPromptSeedRequest(contract.id, prompt, seeded_at))


@lru_cache(maxsize=1)
def _composition() -> tuple[tuple[InsightContract, ...], DynamoInsightPromptRegistry]:
    settings = prompt_registry_seed_settings()
    registry = DynamoInsightPromptRegistry(settings.prompt_registry_table)
    contracts = tuple(contract for _, contract in load_insight_contracts(contracts_root()))
    return contracts, registry


@trace_boundary(INSIGHT_PROMPT_REGISTRY_SEED)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Seed reviewed prompt v1 records on create/update; retain data on delete."""
    with span(
        INSIGHT_PROMPT_REGISTRY_SEED,
        operation=_OPERATION,
    ) as process_span:
        raw_request_type = event.get("RequestType")
        request_type = (
            raw_request_type if raw_request_type in {"Create", "Update", "Delete"} else "unknown"
        )
        process_span.record(request_type=request_type)
        try:
            if request_type == "unknown":
                raise ValueError("prompt registry seed request type is unsupported")
            if request_type == "Delete":
                process_span.record(outcome="completed")
                report_operation(
                    OperationLog(
                        operation=_OPERATION,
                        component=_COMPONENT,
                        phase="terminal",
                        outcome="completed",
                        severity=LogSeverity.INFO,
                        details={"request_type": request_type, "contract_count": 0},
                    ),
                    LOGGER,
                )
                return {"PhysicalResourceId": _PHYSICAL_RESOURCE_ID}
            contracts, registry = _composition()
            seed_contracts(contracts, registry)
            process_span.record(outcome="completed", contract_count=len(contracts))
            report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="terminal",
                    outcome="completed",
                    severity=LogSeverity.INFO,
                    details={"request_type": request_type, "contract_count": len(contracts)},
                ),
                LOGGER,
            )
            return {"PhysicalResourceId": _PHYSICAL_RESOURCE_ID}
        except Exception as error:
            process_span.record(outcome="failed")
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id="", logger=LOGGER),
            )
            raise
