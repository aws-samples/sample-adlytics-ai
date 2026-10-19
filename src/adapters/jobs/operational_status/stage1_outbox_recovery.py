# SPDX-License-Identifier: MIT-0
"""Protected non-disclosing Stage-1 outbox recovery Lambda boundary."""

from __future__ import annotations

from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import stage1_outbox_recovery_settings
from adapters.outbound.dynamodb.stage1_operational_status import (
    DynamoStage1OperationalStatus,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.stage1_operational_status import (
    Stage1OutboxRecoveryInvocation,
    Stage1OutboxRecoveryResult,
)
from domain.operations.status import OperationalStatusError
from ports.outbound.stage1_operational_status import Stage1OutboxRecoveryPort

_SETTINGS = stage1_outbox_recovery_settings()
_LOGGER = Logger(service="adlytics-stage1-outbox-recovery")
_OPERATION = "stage1_operational_status.recovery"
_COMPONENT = "stage1_outbox_recovery"
_RECOVERY = DynamoStage1OperationalStatus(_SETTINGS.publications_table, _SETTINGS.status_table)
_INPUT_FIELDS = frozenset({"schema_version", "case_ref", "resolution_reason"})


def parse_invocation(value: object) -> Stage1OutboxRecoveryInvocation:
    """Accept only the closed non-disclosing recovery input contract."""
    if not isinstance(value, dict):
        raise OperationalStatusError("Stage-1 recovery invocation must be an object")
    document = cast("dict[object, object]", value)
    if any(not isinstance(key, str) for key in document):
        raise OperationalStatusError("Stage-1 recovery invocation keys must be text")
    typed = cast("dict[str, object]", document)
    if frozenset(typed) != _INPUT_FIELDS:
        raise OperationalStatusError("Stage-1 recovery invocation fields differ from v1")
    if any(not isinstance(typed[name], str) for name in _INPUT_FIELDS):
        raise OperationalStatusError("Stage-1 recovery invocation fields must be text")
    return Stage1OutboxRecoveryInvocation(
        case_ref=cast("str", typed["case_ref"]),
        resolution_reason=cast("str", typed["resolution_reason"]),
        schema_version=cast("str", typed["schema_version"]),
    )


def result_document(result: Stage1OutboxRecoveryResult) -> dict[str, str]:
    """Return only the opaque case identity and closed outcome."""
    return {
        "schema_version": result.schema_version,
        "case_ref": result.case_ref,
        "outcome": result.outcome.value,
    }


def recover(value: object, recovery: Stage1OutboxRecoveryPort) -> dict[str, str]:
    """Parse, invoke, and shape without accepting or returning internal selectors."""
    invocation = parse_invocation(value)
    return result_document(recovery.invoke(invocation))


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Perform one separately authorized protected requeue."""
    try:
        result = recover(event, _RECOVERY)
    except Exception as error:
        report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
        raise
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=result["outcome"],
            severity=LogSeverity.INFO,
            details={"case_ref": result["case_ref"]},
        ),
        _LOGGER,
    )
    return result
