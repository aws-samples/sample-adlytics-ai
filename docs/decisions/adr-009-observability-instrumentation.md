# ADR-009 — ADOT for traces, Powertools for logs and metrics

| | |
|---|---|
| **Status** | Accepted — amended 2026-10-02: the agent execution role no longer holds `logs:PutResourcePolicy`, and the agent's spans reach the account's Transaction Search group through its existing X-Ray grant, which exists only with `ADLYTICS_TRANSACTION_SEARCH` on (H-EVALUATION-RESULT-001, residual O-HARNESS-SPANS-001; [dev acceptance](../../validation/RESULTS.md#security-pre-publication-fixes-on-dev--2026-10-02)) |
| **Date** | 2026-08-18 |
| **Decision** | **ADOT SDK for distributed tracing, Powertools Logger for structured logs, Powertools Metrics for EMF. Powertools Tracer is prohibited.** Every request carries a W3C `traceparent` and a correlation ID; hand-authored spans wrap the operations whose latency we need attributed. |
| **Related** | [observability](../concepts/observability.md) · [ADR-005](adr-005-compute-runtime-baseline.md) · [ADR-010](adr-010-error-handling.md) · [Q-10](../decision-coverage-registry.md#question-register-q-nn) |

## Context

The brief specified ADOT for tracing, Powertools for structured logging, and EMF via Powertools for metrics. Two parts of that needed correcting before it could be recorded, and neither correction had an ADR. This promotes `D-04`.

The operational requirement is latency attribution: when a conversational turn is slow, we need to know whether the time went to the model, the Athena scan, a DynamoDB lookup, or context assembly. A log line with a duration cannot answer that. Parent-child spans can.

## Decision

### Powertools Tracer is prohibited

It is a thin wrapper over the AWS X-Ray SDK. **The X-Ray SDKs and daemon entered maintenance mode on 2026-02-25, with support ending 2027-02-25**, and AWS recommends migrating to OpenTelemetry. Powertools has acknowledged this and has not shipped an OTel-backed Tracer.

Adopting it would mean adopting a component with a known expiry and no published successor. Powertools **Logger** and **Metrics** are unaffected and are both used.

### ADOT SDK, collector-less, initialised in-process — settled 2026-08-23

Python uses `aws-opentelemetry-distro`, minimum 0.10.0 and **0.18.0 or later** for spans in the per-agent log group. It ships in this repository's own Lambda layer and is initialised programmatically, exporting SigV4-signed OTLP directly to `https://xray.<region>.amazonaws.com/v1/traces`.

**This replaces an open measurement, and the answer was forced rather than chosen.** The question was whether to use an AWS-managed ADOT layer or the collector-less SDK. The layer cannot be used on the conversation function at all: it activates through `AWS_LAMBDA_EXEC_WRAPPER`, the Lambda Web Adapter has already claimed that variable, and Lambda supports exactly one exec wrapper. Since collector-less export was required for that function regardless, using a layer anywhere else would have been a second mechanism — two ADOT versions drifting apart, and a Collector process adding cold start to functions a user waits on — for no gain. Verified working end to end on 2026-08-23; see [observability](../concepts/observability.md) §3.3.

**The original reason given for avoiding the Collector was wrong, and the decision survives without it.** This ADR said "AgentCore documentation states the Collector is unsupported for agent observability." That could not be substantiated: AWS documents the SDK as the supported path, which is a different claim. The falsifier below was listed from the start, so the weakness was known. What the decision now rests on is the exec-wrapper conflict and the cost of two mechanisms, both of which are checkable.

> **Reverified 2026-09-17.** [AWS's collector-less ADOT guidance](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLP-UsingADOT.html) explicitly supports the ADOT SDK sending traces directly to the X-Ray OTLP endpoint when Transaction Search is enabled. [AWS's X-Ray migration guidance](https://docs.aws.amazon.com/xray/latest/devguide/xray-sdk-migration.html) identifies OpenTelemetry as the strategic instrumentation direction and the Lambda layer as a convenience option, not a different tracing standard. The repository therefore keeps one in-process ADOT bootstrap across Lambda, the Web Adapter conversation process, and operator CLIs. The bootstrap strips URL userinfo, query strings, and fragments before automatic HTTP span projection; presigned download credentials must never enter a span.

Per [ADR-005](adr-005-compute-runtime-baseline.md) every Lambda is arm64. That still matters for the *Web Adapter* layer, whose `arm64` variant is the one attached — and AWS publishes no arm64 adapter layer in China, which `lambdaWebAdapterLayerArn` refuses at synth rather than discovering at function init.

### Correlation ID and traceparent are different things, and we need both

A custom `x-correlation-id` header is **log correlation**, not distributed tracing. It produces no spans, so no parent-child relationships, no waterfall, no per-hop latency attribution, no service map.

| Carries | Purpose |
|---|---|
| W3C `traceparent` | span topology — the waterfall, per-hop latency, service map |
| `x-correlation-id` | one human-quotable identifier across every log line for one user request, minted by the server and returned in the response header, never read from the client ([ADR-081](adr-081-make-every-turn-traceable-and-attributable.md) §2) |

Only W3C `traceparent` is propagated. **OTel baggage is prohibited**: session, identity, and request state remain functional data and must never become telemetry attributes.

### Runtime process amendment — settled 2026-09-05, superseded 2026-09-14

> The custom AgentCore Runtime process this amendment instrumented was replaced by the managed
> Harness ([ADR-006](adr-006-agentcore-agentic-layer.md)) and its remaining code deleted by
> [ADR-064](adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md). No deployed
> handler opens a `runtime.*` span or wraps a `RuntimeOperationEnvelope.v1`, and the span
> constants are gone. The paragraphs below are kept as the record of what was decided; the
> managed Harness's own traces and the API root trace ID are described in
> [observability](../concepts/observability.md).

The AgentCore Runtime process used the same collector-less in-process ADOT bootstrap as the Lambda
handlers. It initializes before serving traffic, emits a bounded structured startup record, opens a
turn span plus bounded model/execution/investigation/publication child spans, and flushes EMF and
spans whenever the terminal stream completes or the process stops. Error reporting stays
error-focused: classified dependency and invariant failures emit the existing Powertools EMF error
metric before the governed refusal is formed; ordinary product refusals do not become alarms.

The direct Runtime-to-measurement Lambda hop is the one extra propagation boundary. After Runtime
admission, its outbound adapter wraps the canonical server-owned `RuntimeOperationRequest.v1` in a
private `RuntimeOperationEnvelope.v1` containing only optional W3C `traceparent`. The measurement
adapter extracts that parent before opening `tool.runtime_operation`. This envelope is not a Gateway
schema, model input, model output, or public Lambda contract.

### Spans are hand-authored where they answer a question


Auto-instrumentation covers HTTP and SDK calls. That is not sufficient for latency attribution, because the spans that matter are domain-level. These are the X-Ray subsegment equivalent, and they are explicit. `contracts/observability/spans.yaml` is the single complete roster: each declaration owns its fixed name, parent policy, purpose, and closed metadata projection. The generated constants are imported through `src/adapters/outbound/telemetry/tracing.py`; `@trace_boundary(...)` colocates each declaration with exactly one implementing function. The conformance gate requires every tracing-enabled handler to own a typed root and every required attribute to be supplied when its span opens. Model phases inside the managed Harness are the service's own spans, not ours.

> **A span without attributes answers "how long", never "why".** Every hand-authored span carries the attributes needed to explain its own duration — bytes scanned, rows returned, tokens consumed. Duration alone cannot distinguish a slow query from a large one.

### Log levels have assigned meanings

Powertools Logger, JSON, with `info`, `warn`, `error`, `debug` used deliberately rather than by feel:

| Level | Used for |
|---|---|
| `debug` | off in production; developer detail |
| `info` | request lifecycle, decisions taken, coverage and suppression outcomes |
| `warn` | degraded but served — stale data, truncation, suppressed rows, a retried call |
| `error` | the request failed, or produced a number we would not stand behind |

**Never log a token, cookie, credential, or Guardrail `match` value.** Never log raw model output at `info`.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Powertools Tracer** | Wraps an SDK in maintenance mode with support ending 2027-02-25 |
| **X-Ray SDK directly** | Same expiry, without the ergonomics |
| **Correlation ID only, no tracing** | Cannot attribute latency across hops, which is the actual operational requirement |
| **Auto-instrumentation only** | Produces HTTP and SDK spans, none of which explain where a conversational turn spent its time |
| **ADOT Collector** | Unsupported for AgentCore agent observability |

## Consequences

- **Transaction Search must be enabled** for the agent's spans to be exported. The execution role holds no `logs:PutResourcePolicy`, because with it the role could open any log group to X-Ray (H-EVALUATION-RESULT-001), and the stack declares no Logs resource policy for the agent's runtime groups. Where the agent's spans land, and who can read them, is owned by [observability §3.4](../concepts/observability.md#34-protected-harness-diagnostics). Span destination cannot be migrated after creation, so it must be set correctly at creation. Ownership of that decision is [Q-10](../decision-coverage-registry.md#question-register-q-nn).
- **Application Signals must be decided together with Transaction Search.** Application Signals auto-instruments Lambda through its own enhanced ADOT layer, and AWS's guidance is to keep `OTEL_AWS_APPLICATION_SIGNALS_ENABLED` at its `false` default when using the ADOT SDK directly, to avoid paying twice for the same spans.
- The ADOT cold-start cost on Python is **unmeasured and unpublished**. For the interactive path it decides layer versus collector-less SDK. Measure in the target account before committing; recorded as an explicit unknown.
- Hand-authored spans are code that can rot. A renamed operation with a stale span name produces a dashboard that quietly measures the wrong thing.
- Powertools Metrics gives EMF natively, so [ADR-010](adr-010-error-handling.md) needs no custom metric plumbing.

## Review trigger

- measured ADOT layer cold start exceeds the interactive latency budget in [Q-12](../decision-coverage-registry.md#question-register-q-nn)
- Powertools ships an OTel-backed Tracer, which would make the prohibition worth revisiting
- ~~AgentCore supports the ADOT Collector for agent observability~~ — **fired 2026-08-23**, in the sense that the claimed prohibition could not be substantiated. The decision was re-argued on the exec-wrapper conflict and the cost of two instrumentation stacks, and stands
- a future CDK or ADOT release makes the managed layer usable alongside another exec wrapper, which would reopen the layer-versus-in-process question
- span ingestion cost at 100% recording exceeds what the indexing dial can absorb, which would make `traceSampleRatio` a real decision rather than a formality
