# Forecasting — architecture

This companion owns the proposed Forecasting capability's service topology and publish-or-hold flow.
The [overview](README.md) owns the consumer contract; detailed records and interfaces are in the
[low-level design](low-level-design.md). Diagram notation is owned by the
[documentation conventions](../../conventions.md).

## 4. Internal structure

**Component diagram — Forecast retained-evidence topology, operational lifecycle viewpoint, component depth.** A scheduled
workflow creates a candidate from exact selected history, but only a separate quality publisher can
advance the retained forecast pointer. The dedicated page route and governed read tool retrieve that
artifact; neither invokes a model.

![Component diagram: Forecast retained-evidence topology. Selected history enters a planned daily workflow inside the operational ML boundary. Retained data is isolated in ForecastDataStack, and narrow readers serve the page and governed conversational tool from its immutable current generation.](diagrams/forecast-architecture.svg)

> **Takeaway.** A daily run can fail or be held without changing the last accepted forecast; a page
> response and governed tool response each use an attested immutable generation through a narrow role.

### Stack and lifecycle boundaries

| Stack/boundary | Resources | Lifecycle and responsibility |
|---|---|---|
| **ForecastDataStack** | Forecast artifact DynamoDB table, private forecast-processing S3 bucket, and typed SSM publication parameters | RETAIN. Holds immutable accepted generations, current pointers, bounded run receipts, and processing input/candidate evidence under explicit lifecycle policy. It is never deleted with model operations. |
| **MlStack** | EventBridge Scheduler, Scheduler DLQ/retry policy, Standard Step Functions state machine, Processing definition/roles, coordinator/publisher Lambdas, explicit log groups, alarms, and model-package configuration | DESTROY. Owns daily work and operational failure handling, not durable forecast authority. |
| **ApiStack extension** | Dedicated `ForecastPresentationFunction`, exact protected route, and its narrow reader role | Reads forecast artifacts and returns only a closed page response. It receives no Athena, Glue, or model-execution authority. |
| **Agent forecast tool** | Dedicated read-only forecast tool function/role registered through the governed operation boundary | Reads an exact retained artifact for an admitted forecast objective. It cannot publish, start a workflow, or invoke SageMaker. |
| **Frontend extension** | Static `/forecast` route and generated page binding | Adds no new external origin. It uses the existing same-origin presentation API and renders fixed supplied values only. |

ForecastDataStack publishes only typed consumer-facing identifiers through the repository SSM parameter
contract. MlStack and ApiStack read them at deploy time; no cross-stack construct reference or
synth-time lookup is permitted. A forecast producer change requires intentional consumer redeployment,
consistent with the existing composition model.

The forecast-processing bucket is private, TLS-only, and uses the repository's established managed
server-side encryption default unless an explicit customer-managed-key design separately supplies every
required grant. Input, candidate, and diagnostic prefixes have explicit lifecycle/retention configuration;
raw aggregate history and model output never enter logs, metrics, browser responses, or an unbounded
queue. The final accepted artifact is stored in DynamoDB, not inferred from an S3 object name.

### Service and IAM boundaries

| Principal | Minimum authority |
|---|---|
| Scheduler execution role | Start only the named forecast state machine. |
| State-machine role | Invoke only the named coordinator/publisher functions and start/wait only the named Processing job. |
| History coordinator | Select exact CURRENT publications, run only the batch history query path, and write one run input manifest under its staging prefix. |
| Processing role | Read only its run input/model package and write only its candidate-output prefix. It has no publication-pointer or presentation authority. |
| Quality publisher | Read one candidate manifest, write immutable generation/run receipts, and conditionally advance only the forecast table pointer. |
| Presentation/tool reader | Strongly read only the exact forecast artifact/current pointer and return either a closed page response or qualified governed tool result. |

No role is reused from ingestion merely for convenience. Ingestion may materialize Semantic V2; forecast
compute may only consume selected history and publish forecast artifacts. CloudWatch log groups are
explicit, use typed retention, and record only bounded run state, counts, durations, model/config
identity, and typed quality outcome—not history values, forecast values, scope keys, or customer data.

## 5. Key runtime flow

1. **Schedule and non-overlap.** EventBridge Scheduler starts one Standard workflow on the configured
   daily schedule, with explicit retry and DLQ policy. The workflow acquires a forecast-run lease; an
   overlapping run exits as a typed no-op rather than racing a pointer advance.
2. **Readiness and history.** The History Coordinator binds required logical plans to exact selected
   current publications, checks coverage/freshness/suppression/restatement eligibility, and writes an
   immutable input manifest plus canonical series digest to the staging bucket. A failed eligibility
   result is a typed held run; it does not start inference.
3. **Offline inference.** The workflow starts `CreateProcessingJob.sync`. The Processing job receives
   only the staged manifest and pinned model/package configuration, performs inference and rolling-origin
   backtests, then writes one candidate manifest and output under its run prefix. It never downloads a
   model at customer request time or calls a browser/API surface.
4. **Quality gate and publish.** The Quality Publisher validates candidate identity, 90 contiguous
   future dates, p10/p50/p90 ordering, finite values, target/basis/scope/currency closure, and declared
   backtest policy. It conditionally creates an immutable generation and advances the exact series
   `CURRENT` pointer only after all checks pass.
5. **Hold on failure.** A rejected candidate or operational failure writes a bounded run/quality receipt
   and leaves the last accepted pointer unchanged. The workflow emits a typed alarm outcome. A reader
   receives either `unavailable` when no accepted generation exists or the last exact generation with
   a typed `stale` state.
6. **Serve.** The dedicated forecast route reads and attests the pointer-to-generation pair, then
   returns a closed page response. The fixed Forecast page renders history, interval, freshness,
   quality, and boundary. The governed forecast read tool returns qualified forecast evidence without
   deriving a new path or making causal allocation advice.

Amazon documents both the Scheduler-to-Step-Functions start pattern and synchronous Step Functions
integration for SageMaker Processing jobs. SageMaker JumpStart documents Chronos models for time-series
forecasting. The exact model/image/package and output schema remain a required implementation-time
spike, not an assumed deployment detail. [Scheduler](https://docs.aws.amazon.com/step-functions/latest/dg/using-eventbridge-scheduler.html)
· [SageMaker integration](https://docs.aws.amazon.com/step-functions/latest/dg/connect-sagemaker.html)
· [Chronos](https://docs.aws.amazon.com/sagemaker/latest/dg/jumpstart-foundation-models-example-notebooks.html)

Content from those sources was rephrased for compliance with licensing restrictions.
