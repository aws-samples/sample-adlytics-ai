# Saturation

| | |
|---|---|
| **Level** | L2 — proposed capability overview |
| **Status** | Proposed; no implementation or acceptance evidence. |
| **Owns** | Offline channel-response fitting, validated saturation artifacts, and bounded non-causal serving semantics. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** The campaign-level design in [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md), with [ADR-112](../../decisions/adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md), [ADR-113](../../decisions/adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md), [ADR-090](../../decisions/adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) and [ADR-093](../../decisions/adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md) (all Proposed), would replace this package's channel-level proposal. This package is rewritten when that design's first slice is built; until then, the concept holds the current proposal.

## 1. Purpose and scope

Saturation would fit declared channel-response curves from governed historical evidence and serve an explicitly uncertain, non-causal saturation view. It is separate from Forecasting because its target, fit, artifact, and customer claim differ.

## 2. Where this fits

This proposed capability refines the planned ML boundary in the L1 [architecture](../../architecture.md#capability-index).

## 3. Black-box contract

| | |
|---|---|
| **Inbound** | Exact selected governed history and a declared fit configuration. |
| **Outbound** | Validated response-curve parameters, credible intervals, fit diagnostics, and typed unavailable/held states. |
| **Guarantee** | A non-converged or insufficient fit never becomes customer-facing advice or causal allocation guidance. |

## Documentation map

- [Architecture](architecture.md) owns proposed offline topology and publish-or-hold behavior.
- [Low-level design](low-level-design.md) owns artifacts, validation, and risks.
