# SPDX-License-Identifier: MIT-0
"""The Lambda entry point for the measurement domain's tools.

The absence this closes was recorded as a decision rather than an oversight: a tool reaches a
handler through an AgentCore Gateway target, so fixing an event shape before that contract was
verified would have been guessing. It is verified now — the Gateway Lambda target contract
(verified 2026-08-19) — and `gateway.py` owns reading it.

**One function for the measurement domain, and a second one for catalog rather than one for both.**
The collocation rule is one Lambda per domain, several Gateway targets routing to it, and it
carries an explicit exception: never collocate tools whose IAM permissions differ materially,
because the function holds the union. `catalog` reads only bundled declarations; this function
additionally needs Athena, Glue, S3, objectives and exact V2 CURRENT publication state. Sharing a
function would hand capability introspection all of those read grants, which is the widening that
rule exists to prevent.

**Everything expensive happens at import, not per request.** Configuration, the metric dictionary,
the contracts and both outbound adapters are resolved once. A container that cannot resolve its
configuration must fail before it is considered healthy rather than on whichever request arrives
first, which would present as a bug in that request.

Configuration comes from `settings_without_client_secret`, not `settings`: this function is not the
BFF and has no business holding the credential that exchanges an authorization code. That accessor
documents why the distinction is a least-privilege boundary rather than a convenience.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import (
    settings_without_client_secret,
    tool_caller_settings,
    tracing_settings,
)
from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.caller import CallerVerifier, kms_caller_verifier
from adapters.inbound.tool_handlers.dimension_labels import DimensionLabelResolver
from adapters.inbound.tool_handlers.measurement import MeasurementTooling, handle
from adapters.inbound.tool_handlers.measurement_dispatch import MeasurementDispatch
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.query import AthenaQueryAdapter, AthenaQueryTarget
from adapters.outbound.catalog import MetricWindowDefaults, PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.entity_catalog import CanonicalEntityCatalog, EntityCatalogTooling
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import PublicationSelector
from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter
from adapters.outbound.segment_labels import CanonicalSegmentLabels
from adapters.outbound.segment_roster import RosterReaders, SegmentRosterReader
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.spend_roster import SpendRosterReader, ad_product_labels
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    INVALID_TOOL,
    METRICS,
    UNIDENTIFIED_TOOL,
    UNVERIFIED_CALLER,
    GatewayInvestigationNext,
    GatewayResultClass,
    GatewayTerminalLog,
    Outcome,
    Report,
    normalize_correlation_id,
    report_error,
    report_invocation,
    report_lifecycle,
)
from adapters.outbound.telemetry.tracing import (
    MEASUREMENT_GATEWAY,
    span,
    tool_span,
    trace_boundary,
)
from adapters.producer.packs import load_query_packs
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import AdlyticsError, ValidationError

__all__ = ["handler"]

ROOT = gateway.contracts_root()
SETTINGS = settings_without_client_secret()
TRACING = configure_tracing(tracing_settings())
"""Whether spans leave this container, decided once at init.

Installed here beside `settings()` for the same reason: a container that cannot resolve its
telemetry should fail at start rather than on a request. Returns `False` when tracing is off, which
is every test process and any deployment where Transaction Search has not been enabled — the spans
are still authored, they simply stay in-process.
"""

CATALOG = load_catalog(ROOT)
CONTRACTS = tuple(contract for _, contract in load_contracts(ROOT))
SOURCE_PACKS = load_source_packs(ROOT)
COMMERCE_BRIDGE = load_commerce_identity_bridge(ROOT)
DEPLOYMENT_SCOPE = AuthorizedAnalyticalScope(frozenset({COMMERCE_BRIDGE.commerce_scope}))
CURRENT_PUBLICATIONS = DynamoCurrentPublications(SETTINGS.semantic_publications_table)
ENTITY_CATALOG = CanonicalEntityCatalog(
    EntityCatalogTooling(
        current=CURRENT_PUBLICATIONS,
        canonical=S3CanonicalAdapter(SETTINGS.semantic_v2_bucket),
        packs=SOURCE_PACKS,
        contracts=CONTRACTS,
        bridge=COMMERCE_BRIDGE,
        taxonomy=load_media_taxonomy(ROOT),
    )
)
PUBLICATION_SELECTOR = PublicationSelector(
    CURRENT_PUBLICATIONS,
    SOURCE_PACKS,
    COMMERCE_BRIDGE,
    # Each governed pack's declared lag after period end, for a periodic answer's "expected
    # after" (ADR-100); the same files every runtime reads its installed governance from.
    cadence_lag_days={
        pack.contract_id: pack.effective_cadence_lag_days for pack in load_query_packs(ROOT)
    },
    # Chat's pins are the customer's, so a pin no writer serves is a classified refusal.
    taxonomy=load_media_taxonomy(ROOT),
)
QUERIES = AthenaQueryAdapter(
    AthenaQueryTarget(
        workgroup=SETTINGS.athena_workgroup,
        database=SETTINGS.semantic_v2_current_database,
        reuse_minutes=SETTINGS.athena_result_reuse_max_age_minutes,
    )
)
CANONICAL = S3CanonicalAdapter(SETTINGS.semantic_v2_bucket)
# Segment names and providers from dim_audience_segment, each namespace from its own writer
# (ADR-103), so a C1 answer names every segment with its provider and never a guessed one.
SEGMENT_LABELS = CanonicalSegmentLabels(
    CURRENT_PUBLICATIONS, CANONICAL, SOURCE_PACKS, CONTRACTS, COMMERCE_BRIDGE
)
LABELS = DimensionLabelResolver(ENTITY_CATALOG, DEPLOYMENT_SCOPE, segments=SEGMENT_LABELS)
TOOLING = MeasurementTooling(
    queries=QUERIES,
    publications=PUBLICATION_SELECTOR,
    labels=LABELS,
    query_batch_max_concurrency=SETTINGS.athena_query_batch_max_concurrency,
    periods=PUBLICATION_SELECTOR,
    pack_coverage=PUBLICATION_SELECTOR,
    # The ad products or campaigns with spend in a period, so a per-channel or per-campaign
    # answer names each rostered key with no row "not available" (plan D4). Campaigns are named
    # by the same public labels the answer's rows carry, never by their protected keys. A
    # campaign's segments are rostered against its previous month (design §6.5).
    rosters=RosterReaders(
        spend=SpendRosterReader(
            catalog=CATALOG,
            contracts=CONTRACTS,
            publications=PUBLICATION_SELECTOR,
            queries=QUERIES,
            labels=ad_product_labels(load_media_taxonomy(ROOT)),
            clock=lambda: datetime.now(UTC),
            names=LABELS,
        ),
        segments=SegmentRosterReader(
            catalog=CATALOG,
            contracts=CONTRACTS,
            publications=PUBLICATION_SELECTOR,
            periods=PUBLICATION_SELECTOR,
            queries=QUERIES,
            labels=SEGMENT_LABELS,
            clock=lambda: datetime.now(UTC),
        ),
    ),
)
DISPATCH = MeasurementDispatch(
    catalog=CATALOG,
    contracts=CONTRACTS,
    entity_catalog=ENTITY_CATALOG,
    scope=DEPLOYMENT_SCOPE,
    tooling=TOOLING,
    now=lambda: datetime.now(UTC),
    window_defaults=MetricWindowDefaults(
        PageWindowCatalog(CURRENT_PUBLICATIONS, SOURCE_PACKS, CONTRACTS, COMMERCE_BRIDGE),
        CATALOG,
    ),
)
LOGGER = Logger(service="adlytics-measurement")
CALLER: CallerVerifier = kms_caller_verifier(tool_caller_settings())
"""Verifies every call's caller before anything else is read (ADR-077 §5).

Attribution, not entitlement: `DEPLOYMENT_SCOPE` above still decides what may be read (ADR-054).
"""


def _handle(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Answer one measurement tool call, or return a refusal the model can relay.

    The handler executes only finite registry authority and resolves every protected plan,
    population, snapshot, and presentation value from server-owned declarations and exact
    selected evidence.

    **A refusal is a result here, not an exception escaping.** `plan()` raises for a question
    outside its contract, and those raises carry the reason and the alternatives — which is exactly
    what a caller needs. Letting the exception propagate would surface as a Lambda failure, and
    Gateway would report a broken tool rather than an answered question. So every classified error
    becomes a structured refusal, and the model is told why rather than that something went wrong.

    Only `AdlyticsError` is translated. Anything else is genuinely unexpected, and is re-raised
    after being recorded so it reaches an alarm instead of being flattened into a polite refusal.

    The caller is verified first (ADR-077 §5): an unverified call is refused before the
    invocation is read, so no argument is parsed and no data port is reached.
    """
    channel = gateway.channel_keys(event, context)
    message_ref = gateway.gateway_message_ref(context)
    caller_report = Report(
        operation=UNVERIFIED_CALLER,
        correlation_id="",
        logger=LOGGER,
        gateway_message_ref=message_ref,
    )
    admission = CALLER.admit(context, caller_report)
    if admission.refusal is not None:
        report_invocation(caller_report, Outcome.REFUSED)
        report_lifecycle(
            GatewayTerminalLog(
                operation=UNVERIFIED_CALLER,
                outcome=Outcome.REFUSED,
                result_class=GatewayResultClass.CALLER_UNVERIFIED,
                investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                gateway_message_ref=message_ref,
                channel=channel,
                caller_verified=False,
            ),
            LOGGER,
        )
        return DISPATCH.refusal_response(admission.refusal, normalize_correlation_id(None))
    try:
        invocation = gateway.GatewayInvocation.read(event, context)
    except AdlyticsError as error:
        report = Report(
            operation=UNIDENTIFIED_TOOL,
            correlation_id="",
            logger=LOGGER,
            gateway_message_ref=message_ref,
        )
        report_error(error, report)
        report_invocation(report, Outcome.FAILED)
        report_lifecycle(
            GatewayTerminalLog(
                operation=UNIDENTIFIED_TOOL,
                outcome=Outcome.FAILED,
                result_class=GatewayResultClass.INVALID_INVOCATION,
                investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                gateway_message_ref=message_ref,
                channel=channel,
                caller_verified=True,
            ),
            LOGGER,
        )
        raise

    response_correlation_id = normalize_correlation_id(None)
    try:
        selected_tool = DISPATCH.tool(invocation.tool_name)
    except ValidationError as error:
        report = Report(
            operation=INVALID_TOOL,
            correlation_id="",
            logger=LOGGER,
            gateway_message_ref=invocation.gateway_message_ref,
        )
        with span(tool_span(INVALID_TOOL)) as recording:
            report_error(error, report)
            report_invocation(report, Outcome.REFUSED)
            recording.note_error(error)
            report_lifecycle(
                GatewayTerminalLog(
                    operation=INVALID_TOOL,
                    outcome=Outcome.REFUSED,
                    result_class=GatewayResultClass.INVALID_TOOL,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            return DISPATCH.refusal_response(error, response_correlation_id)

    report = Report(
        operation=selected_tool.value,
        correlation_id="",
        logger=LOGGER,
        gateway_message_ref=invocation.gateway_message_ref,
    )
    with span(tool_span(selected_tool.value)) as recording:
        # Both identifiers let the audit trajectory reach this Gateway-isolated trace: the
        # result reference travels inside the tool result, the message reference is Gateway's.
        recording.record(
            tool_result_ref=response_correlation_id,
            gateway_message_ref=invocation.gateway_message_ref,
        )
        try:
            request = DISPATCH.request_from(invocation, selected_tool, response_correlation_id)
            recording.record(metric=request.metric_name, grains=",".join(request.grains))
            answer = handle(request, DISPATCH.tooling)
        except AdlyticsError as error:
            report_error(error, report)
            report_invocation(report, Outcome.REFUSED)
            recording.note_error(error)
            report_lifecycle(
                GatewayTerminalLog(
                    operation=selected_tool.value,
                    outcome=Outcome.REFUSED,
                    result_class=GatewayResultClass.CLASSIFIED_REFUSAL,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            return DISPATCH.refusal_response(error, response_correlation_id)
        except Exception as error:
            report_error(error, report)
            report_invocation(report, Outcome.FAILED)
            report_lifecycle(
                GatewayTerminalLog(
                    operation=selected_tool.value,
                    outcome=Outcome.FAILED,
                    result_class=GatewayResultClass.UNEXPECTED_FAILURE,
                    investigation_next=GatewayInvestigationNext.TOOL_BOUNDARY,
                    gateway_message_ref=invocation.gateway_message_ref,
                    channel=channel,
                    caller_verified=True,
                ),
                LOGGER,
            )
            raise

        recording.record(
            rows_returned=answer.metadata.row_count,
            suppressed=answer.metadata.suppression.rows_suppressed,
            freshness=answer.metadata.freshness_status,
            truncated=answer.metadata.truncated,
            contracts=",".join(answer.metadata.contract_ids),
        )
        report_invocation(report, Outcome.ANSWERED)
        report_lifecycle(
            GatewayTerminalLog(
                operation=selected_tool.value,
                outcome=Outcome.ANSWERED,
                result_class=GatewayResultClass.ANSWER,
                investigation_next=GatewayInvestigationNext.MANAGED_TOOL_TRACE,
                gateway_message_ref=invocation.gateway_message_ref,
                channel=channel,
                caller_verified=True,
            ),
            LOGGER,
        )
        return gateway.to_json_safe(answer)


@trace_boundary(MEASUREMENT_GATEWAY)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Bound invocation parsing and the selected governed measurement-tool span."""
    with span(MEASUREMENT_GATEWAY, operation=MEASUREMENT_GATEWAY) as process_span:
        result = _handle(event, context)
        process_span.record(outcome="completed")
        return result
