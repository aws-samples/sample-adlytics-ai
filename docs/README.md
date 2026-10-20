# Adlytics AI — Design Documentation

This is the entry point for the current design. It contains navigation only: each linked document owns its own facts.

## Start here

| Reader | Read |
|---|---|
| New to the project | [Business Context](business-context.md) → [Understanding the Data Model](understanding-the-data-model.md) → [Architecture](architecture.md) |
| Building data capabilities | [Source Acquisition](capabilities/source-acquisition/README.md) → [Data Layer](capabilities/data-layer/README.md) → [Load Path](capabilities/load-path/README.md) → [Publication State](capabilities/publication-state/README.md) → [Query Path](capabilities/query-path/README.md) |
| Building conversation | [Conversational Runtime](capabilities/conversational-runtime/README.md) → [Agent Quality Loop](capabilities/agent-quality-loop/README.md) |
| Building analytical experiences | [Analytics Discovery](capabilities/analytics-discovery/README.md) → [UI page contracts](ui/README.md) |
| Building evaluation | [Agent Quality Loop](capabilities/agent-quality-loop/README.md) → [Protected Quality Review API](capabilities/protected-quality-review-api/README.md) |
| Building deferred ML | [Forecasting](capabilities/forecasting/README.md) → [Saturation](capabilities/saturation/README.md) |
| Operating a deployment | [Repository README](../README.md) → [Repo Structure](repo-structure.md) → [Validation results](../validation/RESULTS.md) |
| Working with an AI assistant | [Build with an AI assistant](kiro.md) → [AGENTS.md](../AGENTS.md) |

## Documentation levels

- [Architecture](architecture.md) is the L1 system view and capability index.
- [Capabilities](capabilities/README.md) are L2 deployable/interface-owning behaviors.
- [Concepts](concepts/) are reusable patterns applied by capabilities.
- [Sources](sources/) own external-system facts and citations.
- [Canonical Model](canonical-model.md) is the L3 field-level reference.
- [Decisions](decisions/) own rationale and history.
- [Decision coverage registry](decision-coverage-registry.md) owns the G-NN gap, F-NN finding, and Q-NN question identifiers.
- [Security dispositions](security-dispositions.md) own the register IDs that inline scanner suppressions name.
- [Validation results](../validation/RESULTS.md) own sanitized returned acceptance conclusions.
- [Test run records](test-run-records.md) own the local run-record format and which records a gate accepts.

## Primers

| Document | Audience |
|---|---|
| [Business Context](business-context.md) | Product purpose, boundaries, and vocabulary. |
| [Understanding the Data Model](understanding-the-data-model.md) | Canonical analytical model from first principles. |
| [Answerable Questions](answerable-questions.md) | Customer-facing supported and refused question boundaries. |

## Governance

[Conventions](conventions.md) define fact ownership, document templates, and diagram notation. [Delivery practice](delivery-practice.md) defines the decision-weighting rubric, review effort by risk, and when a slice may merge. [Glossary](glossary.md) defines shared vocabulary. Deployment, cost, and teardown belong to the [repository README](../README.md).
