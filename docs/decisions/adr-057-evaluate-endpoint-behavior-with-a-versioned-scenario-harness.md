# ADR-057: Evaluate endpoint behavior with a versioned scenario harness

Date: 2026-08-31
Status: Superseded — historical V4 endpoint-harness decision; the implementation was retired

> **Current-state note (2026-09-07).** The custom Runtime V2 endpoint harness, V4 corpus, prompt-use linkage, and scorecard path are retired. No current endpoint capability document or interface remains. Current managed-Harness release, immutable audit and V3 evaluation linkage are owned by [Conversational Runtime](../capabilities/conversational-runtime/README.md), [Agent Quality Loop](../capabilities/agent-quality-loop/README.md), and [Observability](../concepts/observability.md). [Repo structure](../repo-structure.md#current-stack-layout) owns the current physical stack inventory. The decision record below is historical and does not specify a current implementation.

> **Amended 2026-09-26 — the regression path (evaluator mode implemented in the repository
> 2026-09-27; agent mode implemented in the repository 2026-09-27).** The scenario-evidence
> role this harness held belongs to the regression path that
> [ADR-059](adr-059-create-a-bounded-agent-quality-audit-loop.md) names: the runner of
> [ADR-079](adr-079-enable-online-evaluation-only-through-a-verifying-gate.md) §5, which calibrates
> evaluators and runs a fictional question set through the agent before and after a change, plus the
> live acceptance probes.

## Context

Agent behavior cannot be validated by comparing every response with one exact string. Several useful
answers can satisfy the same analytical request, and model, prompt, context, and tool behavior can vary
across runs. Unit and contract tests still prove deterministic calculations, schemas, custody, and
failure boundaries, but they do not establish whether a deployed response is useful, direct,
appropriately qualified, or complete for the customer's objective.

The repository already has two accepted evaluation surfaces. A customer-owned post-release source
feeds a custom evaluator that judges the exact answer released after grounding and final content
assessment. A separate code-based evaluator checks the managed Harness pre-release protocol. Online
evaluation monitors ordinary traffic, but sampling and traffic mix do not provide a repeatable corpus,
controlled repetitions, or a durable before/after report.

A repeatable harness must preserve the distinction between those surfaces, invoke one endpoint attempt
rather than compare two stochastic attempts, retain raw evidence without publishing it, and report
semantic score distributions without treating one run as universal proof. Page-bound scenarios must
obtain real opaque custody from Presentation rather than inject selectors. Current and accepted-target
scenarios must remain distinct because the target structured response and adaptive loop are not yet
implemented or authorized.

Amazon Bedrock AgentCore Dataset Evaluation can orchestrate scenario invocation, telemetry waiting,
span collection, custom evaluator requests, and structured results. It is currently public preview,
so making its types the repository's scenario or result contract would couple a durable quality model
to a changing SDK surface.

An isolated Python 3.14 compatibility spike on 2026-08-31 imported
`bedrock-agentcore==1.22.0` without changing `pyproject.toml` or `uv.lock`. It confirmed the on-demand
and batch runners, file datasets, custom endpoint invokers, custom `AgentSpanCollector`, custom
evaluator IDs, KMS-aware batch configuration, structured results, generated
`<scenario-id>-<uuid>` runtime sessions, and sequential scenario turns. The installed botocore service
model also exposed `Evaluate`, `StartBatchEvaluation`, `GetBatchEvaluation`, and the related batch
lifecycle operations. This established design viability, not deployed acceptance.

## Decision

**Build a versioned, evaluation-first endpoint scenario harness and use the AgentCore Dataset
Evaluation SDK behind a repository-owned adapter when it preserves one-invocation evidence.**

The harness owns versioned scenario, attempt, execution-verdict, scorecard, baseline, and report
contracts. Scenario files reference existing flow/page/contract owners and contain synthetic prompts,
setup profiles, semantic assertions, readiness, evaluator applicability, and repetitions. They contain
no golden response prose or copied target JSON.

The initial execution mode uses `OnDemandEvaluationDatasetRunner` for scenario scheduling, stable
runtime sessions, sequential turns, telemetry waiting, custom released-response span collection, and
per-scenario quality evaluation. A custom authenticated endpoint invoker records a separate canonical
UUIDv4 correlation and the SDK runtime session in an ignored attempt journal. A custom span collector
maps the SDK session to the released-response correlations. The managed Harness protocol trace from
that same endpoint invocation is collected by runtime session and evaluated through a secondary direct
`Evaluate` call. The harness never runs the endpoint a second time merely to evaluate another surface.

The SDK remains an adapter. Repository contracts contain no SDK types, and direct boto3 `Evaluate`
remains the fallback for unsupported orchestration. An exact SDK version is pinned only after an
isolated Python 3.14 compatibility spike passes and implementation is authorized. SDK upgrades require
adapter, corpus, evaluator, and baseline review.

Evaluation scores are the semantic quality signal. Deterministic harness checks decide only whether
the endpoint/setup/stream/evidence collection was valid enough to evaluate; they do not require exact
wording or substitute for an evaluator. Missing telemetry or results remain missing evidence rather
than pass or fail quality.

Online evaluation remains ambient deployed monitoring. Service-side batch evaluation is a later scale
mode for large baselines and pre/post comparisons. Batch may be enabled only after a preflight proves
that its service-written result group satisfies the evaluation-locality contract for KMS, retention,
reader access, lifecycle, and no public export. One external invocation phase supplies separate
released-correlation and runtime-session lists to two batch jobs; a batch runner must not reinvoke the
corpus per source. Because that invocation phase and report read-back are local, this harness batch
mode is workstation-copying even though AgentCore discovers spans and scores them service-side. A
no-workstation-copy batch requires a separate AWS-resident invocation and reporting path.

Publication-eligible artifacts live under `evaluation/`: versioned synthetic scenarios, immutable
baselines, sanitized JSON scorecards, and generated Markdown reports. Raw endpoint output, spans,
evaluator explanations, capabilities, and operational identifiers live under `evaluation/runs/`, which
is ignored by Git and excluded from security-scan uploads. Reports fail closed if forbidden content or identifiers are present. Actual deployed conclusions
remain in `validation/RESULTS.md`; operational commands and credentials remain owned by
`validation/README.md`.

Current and target corpora are versioned separately. Target-only scenarios remain `PENDING_TARGET` and
outside denominators until their contracts are implemented and the conversation-flow authorization
gate is resolved. Evaluator, endpoint-contract, model, prompt, configuration, or fixture changes that
can alter a decision produce a new baseline identity rather than overwriting history. Repeated runs
produce per-scenario distributions; aggregate improvement cannot hide an individual regression.

## Consequences

A developer can execute one declared corpus repeatedly and receive raw evidence, per-scenario
evaluator results, a sanitized scorecard, and a readable Markdown report. The same corpus can measure a
baseline before a model, prompt, tool, or configuration change and compare the resulting distributions
afterward without requiring exact prose.

The harness adds a deliberate evaluation tier rather than placing network or credentials in the unit
loop. Pure contract/report tests remain credential-free. Collected endpoint runs use an explicitly
selected non-production environment, skip when credentials or approved synthetic setup are absent, and
never use an AWS emulator.

Distinct runtime session, released correlation, prompt-use, and local attempt identities add mapping
work but prevent diagnostic correlation from becoming functional identity. The released evaluation
span contract must be versioned before its `session.id` changes from prompt-use identity to UUIDv4
correlation. Correlation normalization must require UUIDv4 so evaluation admission cannot raise after
SSE completion for another canonical UUID version.

The public-preview SDK reduces orchestration code but introduces version and dependency risk. Keeping it
behind an adapter, retaining direct APIs as fallback, and storing repository-owned run contracts limits
that risk. Multi-turn released-quality evaluation remains deferred until a versioned released-session
contract can group turns without exposing the functional Runtime session.

Raw runs may contain complete synthetic prompts, responses, spans, evaluator explanations, and
operational identifiers. They are local evidence and cannot be committed or scanned as publication
content. Sanitized reports lose detailed prose by design; an authorized reviewer uses the ignored run
to investigate a score and records only the bounded conclusion.

Batch evaluation cannot be assumed safe merely because `kmsKeyArn` exists on the API. Its fixed
CloudWatch output group and per-session explanations require a non-production control probe before
batch becomes an accepted mode. The proposed local invocation and report read-back also make harness
batch a workstation-copying path; service-side span discovery and scoring do not change that
classification. Until a separately governed AWS-resident invocation and reporting path exists,
customers that prohibit workstation copies use online evaluation. Until the output controls pass,
synthetic on-demand evaluation is the supported corpus path and online evaluation remains the only
continuous mode.

This decision authorized the then-current single-turn on-demand harness, its operator-only dependency,
scenario manifests, and the published deployment interfaces it consumed. Those components are retired;
the current managed-Harness pathway is owned by the current-state documents named above. It did not
authorize executable evaluation of target conversation flows; those remained owned by the flow tracker's
explicit gate. Batch remained separately gated by its managed-sink preflight.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Exact golden response strings | Multiple useful answers are valid; wording equality rewards imitation and creates false regressions |
| LLM evaluation with no scenario contract | Results cannot be attributed to an objective, setup, evaluator version, or accepted behavior boundary |
| Online evaluation only | Sampling and live traffic do not provide repeatable scenario coverage, controlled repetitions, or pre/post baselines |
| Dataset Runner types as domain contracts | Public-preview SDK changes would redefine durable scenario, result, and baseline semantics |
| Invoke once per evaluator/source | Two stochastic invocations do not evaluate the same behavior and can produce contradictory evidence |
| Direct boto3 orchestration only | Reimplements useful SDK scenario scheduling, turn sequencing, waiting, reference mapping, and result models without a demonstrated need |
| Batch evaluation as the first mode | Per-session detail goes to a service-written result group whose controls must be proven before content writes |
| Commit raw runs for auditability | Raw spans and explanations contain content and operational identifiers that are outside the publication boundary |
| Treat missing evaluation as a failed answer | Conflates endpoint/model quality with telemetry ingestion or evaluator availability; missing evidence must remain explicit |
| Run target scenarios before target implementation | Converts known unsupported behavior into misleading quality regressions and bypasses the flow-authorization gate |

## Review trigger

Revisit when any of these becomes true:

- AgentCore Dataset Evaluation becomes generally available or changes its scenario/session/result APIs;
- the SDK cannot preserve one endpoint invocation across required evaluator surfaces;
- a multi-turn quality contract is accepted;
- batch result-group controls can or cannot satisfy the managed-sink boundary;
- evaluator variance prevents stable per-scenario distributions;
- a corpus must include customer-backed rather than synthetic endpoint data;
- report sanitization cannot preserve enough evidence for useful regression review; or
- an external client or production rollout requires N/N+1 corpus or endpoint-contract coexistence.

[AgentCore Dataset Evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations.html),
[on-demand runner](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations-on-demand.html),
[batch evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations.html),
and [`Evaluate` API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_Evaluate.html)
were read on 2026-08-31. Content was paraphrased for licensing compliance.