# SPDX-License-Identifier: MIT-0
"""Deterministic structural findings and aggregate measurements derived from one receipt.

Pure calculations: the agent-quality processor applies them to each new receipt, and validation
probes apply them to attest what the processor wrote, without importing a Lambda handler.
"""

from __future__ import annotations

import hashlib

from domain.conversation.audit import (
    AuditAggregateMetricV2,
    AuditFindingKindV2,
    AuditFindingSeverityV2,
    AuditFindingV3,
    AuditOperationOutcomeV2,
    AuditTerminalOutcomeV2,
    AuditToolStepDispositionV3,
    ConversationAuditRecordV6,
)

__all__ = ["aggregate_metric", "derive_findings"]


def _identity(prefix: str, audit_id: str, category: str) -> str:
    """Derive a deterministic opaque ID without embedding request/customer material."""
    return f"{prefix}-{hashlib.sha256(f'{audit_id}:{category}'.encode()).hexdigest()[:32]}"


def derive_findings(record: ConversationAuditRecordV6) -> tuple[AuditFindingV3, ...]:
    """Derive only closed structural findings from a content-minimized receipt.

    Turn-level findings name the terminal defect class. Step-level findings anchor to one
    trajectory ordinal: a finalizer attempt the server rejected (the model needed repair) and a
    governed result the accepted plan never used (a tool call that cost latency and returned
    nothing the user saw).
    """
    findings: list[tuple[AuditFindingKindV2, AuditFindingSeverityV2, int | None]] = []
    if (
        record.terminal_outcome is AuditTerminalOutcomeV2.DEPENDENCY_FAILURE
        or record.operation_outcome is AuditOperationOutcomeV2.FAILED
    ):
        findings.append((AuditFindingKindV2.DEPENDENCY_FAILURE, AuditFindingSeverityV2.ERROR, None))
    if record.terminal_outcome is AuditTerminalOutcomeV2.GOVERNED_REFUSAL:
        findings.append((AuditFindingKindV2.GOVERNED_REFUSAL, AuditFindingSeverityV2.INFO, None))
    if record.terminal_outcome is AuditTerminalOutcomeV2.OUTPUT_POLICY_INTERVENTION:
        findings.append(
            (
                AuditFindingKindV2.OUTPUT_POLICY_INTERVENTION,
                AuditFindingSeverityV2.WARNING,
                None,
            )
        )
    for step in record.trajectory:
        if step.disposition is AuditToolStepDispositionV3.REJECTED:
            findings.append(
                (AuditFindingKindV2.FINALIZER_REPAIR, AuditFindingSeverityV2.INFO, step.ordinal)
            )
        elif step.governed and step.selected_into_plan is False:
            findings.append(
                (
                    AuditFindingKindV2.UNSELECTED_GOVERNED_RESULT,
                    AuditFindingSeverityV2.WARNING,
                    step.ordinal,
                )
            )
    return tuple(
        AuditFindingV3(
            finding_id=_identity(
                "finding",
                record.audit_id,
                kind.value if ordinal is None else f"{kind.value}:{ordinal}",
            ),
            audit_id=record.audit_id,
            kind=kind,
            severity=severity,
            created_at=record.created_at,
            step_ordinal=ordinal,
        )
        for kind, severity, ordinal in findings
    )


def aggregate_metric(record: ConversationAuditRecordV6) -> AuditAggregateMetricV2:
    """Produce one immutable, aggregate-ready structural measurement per receipt."""
    return AuditAggregateMetricV2(
        metric_id=_identity("metric", record.audit_id, "terminal"),
        audit_id=record.audit_id,
        terminal_outcome=record.terminal_outcome,
        operation_outcome=record.operation_outcome,
        value=1,
        created_at=record.created_at,
    )
