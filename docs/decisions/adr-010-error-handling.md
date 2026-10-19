# ADR-010 — Error taxonomy, codes, and metrics on every error

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-18 |
| **Decision** | **Errors are typed by layer and carry a stable code plus a human description. Domain errors are defined in `src/domain` with no infrastructure detail; adapters translate infrastructure failures into them at the boundary. Every error emits an EMF metric through Powertools Metrics, dimensioned so application health is assessable in near real time.** |
| **Related** | [ADR-009](adr-009-observability-instrumentation.md) · [repo-structure](../repo-structure.md) §2 · [Conversational Runtime](../capabilities/conversational-runtime/README.md) · [gap register](../decision-coverage-registry.md#gap-register-g-nn) G-14 |

## Context

`G-14` records that no error-handling strategy exists. This ADR is therefore design work rather than a record of something already decided.

Two forces shape it. The hexagonal dependency rule says `src/domain` imports nothing outside itself — so a domain error cannot reference `botocore.exceptions.ClientError`, and a `ThrottlingException` cannot travel upward unmodified without dragging AWS into the domain. And the platform invariant says the model narrates rather than computes — so an error the model paraphrases freely becomes an error it can misrepresent.

## Decision

### Four error classes, distinguished by what a human should do about them

| Class | Raised in | Means | Surfaced to user | Alarms |
|---|---|---|---|---|
| **`DomainError`** | `src/domain` | the request is understood and the answer is legitimately unavailable — insufficient history, a grain the privacy thresholds forbid, a metric invalid at the requested grain | **yes**, with the reason | no |
| **`ValidationError`** | adapter boundary, inbound | the request is malformed or out of contract | yes | no |
| **`DependencyError`** | adapter boundary, outbound | an infrastructure call failed — Athena, DynamoDB, Bedrock, AgentCore | generic message only | **yes** |
| **`InvariantError`** | anywhere | a state we believe impossible — a fact grain that should not exist, a suppressed row that lost its discriminator | generic message only | **yes, urgently** |

> **`DomainError` is the class most systems get wrong.** "No data for that window" is not a failure, it is an answer, and it is the answer this product must give confidently rather than degrade into a plausible wrong number. It surfaces its reason to the user and must not alarm — otherwise the alarm that matters drowns.

### Codes are stable, namespaced, and never reworded in place

Format `ADLY-<AREA>-<NNNN>`, for example `ADLY-DATA-0104`. A code is an identifier, so **never change what an existing code means.** Superseding a meaning means a new code.

Every error carries a code, a human description, and structured context. The description explains what happened in terms a user or an on-call engineer can act on. **Never put an infrastructure identifier — ARN, table name, query ID, account ID — in a user-facing description.** It goes in the log and the span, not the response.

### Translation happens at the boundary, in one direction

```
outbound adapter    catches ClientError, ThrottlingException, timeouts
                    →  raises DependencyError with a code
domain              raises DomainError only; imports nothing outside domain
inbound adapter     catches everything, maps to a response and a metric
```

- **Never let an SDK exception type cross into `src/domain`.** `import-linter` already forbids importing `boto3` and `botocore` there, which makes this structurally enforced rather than a review comment.
- **Never catch broadly inside the domain.** A bare `except` in domain code hides the invariant violations that `InvariantError` exists to surface.
- Retry and backoff belong in the outbound adapter, not the domain. A `DependencyError` reaching the domain means retries were already exhausted.

### Every error emits an EMF metric

Powertools Metrics emits EMF natively, so there is no custom plumbing. Every error path increments a metric before the response is formed.

Dimensions: error class, code, and the operation. That combination is what makes health assessable in near real time — the class says how bad, the code says exactly what, and the operation says where.

- **Never emit a metric dimensioned on anything unbounded.** No user IDs, ASINs, campaign IDs or correlation IDs as dimensions; EMF cardinality is a cost and a usability problem. Those belong in the log line and the span.
- `DependencyError` and `InvariantError` rates drive alarms. `DomainError` rate is a **product** signal, not a health signal — a rising rate means users are asking questions the data cannot answer, which is a roadmap input.
- Every error is also recorded on the active span, so a failed turn is traceable end to end rather than only countable.

### What the model may say about an error

The tool envelope carries the code and the description. The narration layer may **paraphrase the description and must not contradict it**, exactly as with figures.

**Never let the model author an error explanation from scratch.** An invented cause is worse than a blunt one, because it is plausible and unfalsifiable.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **HTTP status codes as the taxonomy** | Cannot distinguish "legitimately no answer" from "dependency broke". Both become 500 or both become 200, and either way the alarm signal is lost |
| **Exception classes without codes** | Class names are refactored freely; a dashboard built on them breaks silently. A code is a contract |
| **Letting SDK exceptions propagate** | Puts `botocore` types in the domain, violates the dependency rule, and leaks infrastructure detail into user-facing text |
| **Logging errors without metrics** | Log-based alarming is slower and more expensive than EMF, and the requirement is near-real-time health |
| **One generic `AppError`** | Collapses the alarm decision. The whole value of the taxonomy is that two of four classes page someone and two do not |
| **Model-authored error messages** | Plausible invented causes. Same failure mode as model-authored figures |

## Consequences

- Every outbound adapter needs a translation layer. That is real, repetitive work and it is what keeps the hexagon intact under deadline.
- The code registry becomes an artifact to maintain, and a duplicated or reused code is a defect. Worth a check in `make check` once codes exist.
- Four classes is a judgement call at the boundaries. Some failures are arguably two classes — a malformed contract file could be `ValidationError` or `InvariantError` — so the classification rule needs to be written into steering rather than left to instinct.
- `DomainError` not alarming is correct and will feel wrong the first time a user reports "it said no data". The trace and the metric still record it.
- Retry policy per dependency is now a required decision, not an implicit one.

## Review trigger

- a fifth class is argued for more than once — likely a sign one of the four is doing two jobs
- `DomainError` volume becomes a product concern, meaning the answerable question set is too narrow
- EMF cardinality or cost becomes material, forcing dimensions to be reduced
