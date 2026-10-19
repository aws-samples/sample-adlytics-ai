# SPDX-License-Identifier: MIT-0
"""Install the OTel SDK, or deliberately do not. One place, called once per container.

Closes G-39 (docs/decision-coverage-registry.md). The spans in `tracing.py` have existed and
exported nothing, because the repository depended on the OTel *API* only and an API with no SDK
produces `NonRecordingSpan`s. This is the SDK half.

**Collector-less, which is what makes it possible at all.** The obvious route was an AWS-managed
ADOT Lambda layer, and it fails here twice: every managed Python layer bundles the ADOT Collector,
and the layer activates through `AWS_LAMBDA_EXEC_WRAPPER` — a variable the Web Adapter has already
claimed on the conversation function, where Lambda permits exactly one wrapper. AWS now
documents sending spans straight to the X-Ray OTLP endpoint with no collector in between
[[CloudWatch-OTLP-UsingADOT](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLP-UsingADOT.html)],
so the SDK ships in the layer this repository already builds and is initialised in-process. One
mechanism for every function, including the one that could not have used the layer.

**Off unless configured, and the gate is Transaction Search.** Reaching the X-Ray OTLP endpoint
requires Transaction Search to be enabled on the account, so exporting without it would emit spans
that are billed and discarded. The same flag therefore governs both, which is why this reads
`tracing_enabled` rather than owning a second switch that could disagree with the infrastructure.

**Nothing here runs during a unit test.** `configure_tracing` returns `False` immediately when the
flag is off, which is the state of every test process, so no provider is installed and no exporter
is constructed. That is what keeps `make verify` free of network and credentials, and it is asserted
rather than assumed.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace

if TYPE_CHECKING:  # pragma: no cover — typing only
    from botocore.session import Session as BotocoreSession

    from adapters.config import TracingSettings

__all__ = ["configure_tracing", "flush_traces", "flushes_traces", "otlp_traces_endpoint"]


_LOCK = threading.Lock()
_configured = False
"""Whether this process has installed a provider. Lower-case because it is state, not a constant.

Module state rather than a parameter because the operation is process-wide and not idempotent in
OTel: `set_tracer_provider` warns and does nothing on a second call, so a second attempt would look
like it worked while exporting through the first provider's pipeline.
"""

_FLUSH_TIMEOUT_MILLIS = 2_000
_EXPORT_TIMEOUT_SECONDS = 2
"""The OTLP exporter's own request deadline. It is where a flush's bound actually binds: the
batch processor does not enforce `force_flush`'s timeout on an export already in flight."""
_FLUSH_LOGGER = logging.getLogger("adlytics.telemetry.flush")
"""How long a flush may block before the invocation continues regardless.

A ceiling rather than a target. An unflushed span is a gap in a trace; an invocation held open by a
telemetry export is a slower answer for the user, and the second is worse. Two seconds is far above
the observed export and still under any caller's patience.
"""


def otlp_traces_endpoint(region: str) -> str:
    """Build the X-Ray OTLP endpoint for a region.

    Derived rather than configured, for the reason `config.py` derives every other computable value:
    two keys that must agree eventually will not, and a mismatched endpoint fails as spans that
    silently go nowhere.
    """
    return f"https://xray.{region}.amazonaws.com/v1/traces"


def _safe_http_url(url: str) -> str:
    """Remove userinfo, query credentials, and fragments before HTTP span projection."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def configure_tracing(
    settings: TracingSettings,
    session: BotocoreSession | None = None,
) -> bool:
    """Install a tracer provider and instrument the SDK clients. Returns whether tracing exports.

    An operator may supply the botocore session already bound to its explicit profile and region.
    Runtime callers omit it and retain the deployed-role behavior. The disabled path returns before
    importing an SDK/exporter module or inspecting the optional session.

    Called at module scope by every entry point, beside `settings()`. Safe to call more than once
    and safe to call when disabled — both are no-ops rather than errors, because an entry point
    should not have to know whether another module already did it.

    The return value is deliberately a `bool` rather than nothing, so a caller can log which mode it
    started in. A container that believes it is tracing and is not is a bad afternoon.
    """
    global _configured  # noqa: PLW0603 — process-wide state, guarded by the lock below

    if not settings.enabled:
        return False

    with _LOCK:
        if _configured:
            return True

        # Imported here rather than at module scope, and this is the one place in the repository
        # where a local import is load-bearing. These modules pull in the SDK, the OTLP exporter and
        # botocore's instrumentation; importing them at module scope would put that cost — and an
        # exporter class — into every unit test that touches telemetry, which is exactly what
        # depending on the API alone was protecting.
        from amazon.opentelemetry.distro.exporter.otlp.aws.traces.otlp_aws_span_exporter import (  # noqa: PLC0415
            OTLPAwsSpanExporter,
        )
        from botocore.session import Session  # noqa: PLC0415
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor  # noqa: PLC0415
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
        from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415
            ParentBased,
            TraceIdRatioBased,
        )

        provider = TracerProvider(
            resource=Resource.create(
                {
                    # `service.name` is what the service map and every trace query group by. Taken
                    # from configuration so one deployment's spans never merge with another's.
                    "service.name": settings.service_name,
                    "deployment.environment": settings.environment,
                }
            ),
            # Parent-based, which is the half that matters for an end-to-end trace. A child hop must
            # honour the decision its caller already made, or a sampled-in request loses whichever
            # hops independently sampled themselves out — leaving a trace with a hole where the
            # missing span looks like a fast one.
            sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio)),
        )
        provider.add_span_processor(
            # Batched, not simple. A simple processor exports one span per HTTP request to X-Ray on
            # the calling thread, which on a turn emitting dozens of spans would add its own latency
            # to the thing being measured. The cost is that spans sit in a buffer, which is why
            # `flush_traces` exists and why every entry point calls it.
            BatchSpanProcessor(
                OTLPAwsSpanExporter(
                    aws_region=settings.aws_region,
                    session=session if session is not None else Session(),
                    endpoint=otlp_traces_endpoint(settings.aws_region),
                    timeout=_EXPORT_TIMEOUT_SECONDS,
                )
            )
        )
        trace.set_tracer_provider(provider)

        # The "patching" half. Hand-authored spans explain the operations we chose to name;
        # these explain every AWS call and every outbound HTTP request without a call site knowing
        # about it — Athena, DynamoDB, AgentCore, the JWKS fetch. Without them a trace shows a span
        # of four seconds and nothing about what it was waiting on.
        BotocoreInstrumentor().instrument()
        URLLib3Instrumentor().instrument(url_filter=_safe_http_url)

        _configured = True
        return True


def flushes_traces[H: Callable[..., object]](handler: H) -> H:
    """Wrap a Lambda handler so buffered spans are exported before it returns.

    A decorator rather than a line in each handler body, because the requirement is "always, on
    every path" and a `finally` written by hand is one that an early return will eventually skip.
    Stacked below `METRICS.log_metrics` for the same reason it exists: both flush a buffer Lambda
    would otherwise freeze, and they should be impossible to remember separately.
    """

    @functools.wraps(handler)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return handler(*args, **kwargs)
        finally:
            flush_traces()

    return cast("H", wrapper)


def flush_traces(timeout_millis: int = _FLUSH_TIMEOUT_MILLIS) -> None:
    """Export whatever is buffered, before the process may be frozen.

    **Required, not defensive, and this is the Lambda-specific trap.** A batching processor exports
    on a background thread, and Lambda freezes the execution environment when a handler returns —
    so a thread that has not run yet does not run later, it runs on the *next* invocation or never.
    The spans that go missing are the ones at the end of a turn, which is where failures are.

    A no-op when nothing is configured, so a caller never has to check.
    """
    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None:
        return
    try:
        force_flush(timeout_millis)
    # error-propagation: logged once; a lost trace never changes the handler's outcome
    except Exception as error:
        _FLUSH_LOGGER.warning("trace flush failed: %s", type(error).__name__)
