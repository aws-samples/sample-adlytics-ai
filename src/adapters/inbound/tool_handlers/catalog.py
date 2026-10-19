# SPDX-License-Identifier: MIT-0
"""The catalog tool: capability introspection.

The first tool in the repository, and deliberately so — docs/answerable-questions.md §8 says
`catalog` should be built first, because without it the agent guesses at its own capabilities
and that is where confident wrong answers come from. Building it now also exercises the
`ShapedResponse` envelope while changing the envelope is still cheap (build-order 2.6a).

This is the **capability** half only: what can be asked, at what grain, what is refused and
why. The coverage half — what period is loaded and how fresh it is — is 2.6b. The freshness
registry it needs now exists (`ports/outbound/freshness.py`), so what is missing is the wiring
rather than the store, and `CatalogPort` is still not implemented here.

Thin by design. It maps a tool name to a domain call and returns the envelope; every
decision, refusal and narrative sentence is in `domain/catalog`, so all of it is unit tested
without reading a file.

**Two things are deliberately absent, and both are decisions rather than omissions.**

*No Lambda entry point and no Powertools wiring.* A tool reaches this through an AgentCore
Gateway target, and `AgentStack` does not exist. Adding a handler signature now would fix the
event shape against a Gateway contract nobody has configured.

*No module-scope catalogue load.* The configuration steering's reasoning applies — a
container that cannot resolve what it needs must fail before it becomes healthy, not on the
first request — but acting on it means deciding how `contracts/` reaches the Lambda bundle,
and that packaging question belongs with `AgentStack`. Until then the catalogue is passed in,
which is also what lets a test supply one without a file. Recorded in the docstring rather
than left implicit so the eventual handler does not have to rediscover the requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.catalog.answers import (
    AnswerContext,
    DescribeMetricRequest,
    ListMetricsRequest,
    describe_metric,
    list_metrics,
)
from domain.catalog.capability import Catalog
from domain.narrative.shaped_response import ShapedResponse
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = ["CatalogTool", "CatalogToolRequest", "handle"]

UNKNOWN_TOOL = ErrorCode(area="CATALOG", number=1)
BAD_REQUEST = ErrorCode(area="CATALOG", number=2)


class CatalogTool(StrEnum):
    """The tool names this handler answers to.

    An enum rather than free strings so a name reaching the Gateway target registration and a
    name handled here cannot drift apart silently.
    """

    LIST_METRICS = "catalog_list_metrics"
    """Discover broad declared metric capability dynamically."""

    DESCRIBE_METRIC = "catalog_describe_metric"
    """What one metric means, and optionally whether a grain is supported."""


@dataclass(frozen=True, slots=True)
class CatalogToolRequest:
    """One catalog tool invocation.

    Kept local to this module rather than promoted to `ports/inbound`. A shared tool-request
    port should exist, but deriving its shape from the only tool that exists would encode one
    caller's needs as the interface — extract it when a second tool arrives and there is
    something to generalise from.
    """

    tool: CatalogTool
    catalog: Catalog
    computed_at: datetime
    correlation_id: str
    metric_name: str = ""
    grain: str = ""
    source_reference: str = ""

    @property
    def context(self) -> AnswerContext:
        """The per-request values every answer carries. Validated by `AnswerContext`."""
        return AnswerContext(computed_at=self.computed_at, correlation_id=self.correlation_id)


def handle(request: CatalogToolRequest) -> ShapedResponse:
    """Answer a catalog invocation with the envelope, and nothing else.

    Refusals reach the caller as raised errors rather than empty responses — the reasoning is
    on `Refusal.to_error`, and the short version is that a refusal ran no query, so a
    response would have to invent the basis and coverage window `Metadata` requires. Both
    error classes carry a user-visible description, so the reason and the alternatives are
    not lost.
    """
    if request.tool is CatalogTool.LIST_METRICS:
        if request.source_reference.strip():
            raise ValidationError(
                code=BAD_REQUEST,
                description=(
                    "This workspace cannot confirm metric availability for the requested data "
                    "source. Ask about the workspace's declared metrics instead."
                ),
            )
        return list_metrics(ListMetricsRequest(catalog=request.catalog, context=request.context))

    if request.tool is CatalogTool.DESCRIBE_METRIC:
        if not request.metric_name:
            raise ValidationError(
                code=UNKNOWN_TOOL,
                description=(
                    f"{CatalogTool.DESCRIBE_METRIC.value} requires a metric name. To list "
                    f"what is available, call {CatalogTool.LIST_METRICS.value}."
                ),
            )
        return describe_metric(
            DescribeMetricRequest(
                catalog=request.catalog,
                context=request.context,
                metric_name=request.metric_name,
                grain=request.grain,
            )
        )

    # Unreachable while CatalogTool has exactly the members handled above, and kept so that
    # adding a member without handling it fails loudly rather than returning None.
    raise ValidationError(
        code=UNKNOWN_TOOL,
        description=f"{request.tool} is not a catalog tool.",
    )
