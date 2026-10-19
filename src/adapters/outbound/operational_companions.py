# SPDX-License-Identifier: MIT-0
"""Best-effort companion emission that never gates the owning workload."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from aws_lambda_powertools import Logger

from adapters.config import operational_companion_settings
from adapters.outbound.sqs.operational_status import SqsOperationalStatusSink
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.operations.status import OperationalDataFlowEvent

_LOGGER = Logger(service="adlytics-operational-companion")


@dataclass(frozen=True, slots=True)
class CompanionEmitter:
    """Send observation-only companion events; failures are reported, never raised.

    The workload's own authority is the truth; a lost observation is recorded as an
    error and recovered by later authority-backed reconciliation, so emission must
    never fail or retry the workload that carried it.
    """

    sink: SqsOperationalStatusSink | None
    operation: str

    def emit(self, *events: OperationalDataFlowEvent) -> None:
        """Send each event in order, swallowing and reporting any transport failure."""
        if self.sink is None:
            return
        for event in events:
            try:
                self.sink.send(event)
            except Exception as error:
                report_error(
                    error,
                    Report(operation=self.operation, correlation_id=event.flow_id, logger=_LOGGER),
                )
                return


@lru_cache(maxsize=8)
def companion_emitter(operation: str) -> CompanionEmitter:
    """Resolve the optional status FIFO; an absent URL disables emission entirely."""
    queue_url = operational_companion_settings().queue_url
    return CompanionEmitter(
        sink=SqsOperationalStatusSink(queue_url) if queue_url else None,
        operation=operation,
    )
