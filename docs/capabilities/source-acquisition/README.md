# Source Acquisition

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Optional producer onboarding is disabled unless a deployment explicitly supplies the external role and external ID. |
| **Owns** | Governed producer packs, cross-account registration, manifest finalization, and landing status. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

> **Revision proposed, 2026-10-07.** [ADR-089](../../decisions/adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md), [ADR-097](../../decisions/adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md) and [ADR-098](../../decisions/adr-098-allow-same-account-connectors-with-one-landing-role-per-producer.md) (all Proposed) would add governed API operation packs beside query packs, ship first-party connectors in this repository, and allow same-account connectors with one landing role each. See [Response curves and budget simulation](../../concepts/response-curves-and-budget-simulation.md). This package changes when they are accepted and built.

## 1. Purpose and scope

Source Acquisition turns one registered external result into one immutable, manifest-bound delivery that Semantic V2 can consume. It keeps extraction credentials, workflow execution, and source-system access outside Adlytics.

It does not own AMC or Vendor Central authentication, source workflow scheduling, semantic mapping, publication authority, `CURRENT`, or frontend onboarding. Source-system facts remain in [sources](../../sources/); publication begins in [Load Path](../load-path/README.md).

## 2. Where this fits

This capability refines the Source Acquisition container between an external producer and the retained Semantic V2 source in the L1 [architecture](../../architecture.md#capability-index).

## 3. Black-box contract

| | |
|---|---|
| **Responsibility** | Admit a registered external result only when exact bytes, source identity, and governed producer artifacts agree; emit one canonical manifest-bound delivery. |
| **Inbound** | An explicitly enabled external role, deployment-specific external ID, immutable result object, final registration object, and governed query pack. |
| **Outbound** | Unchanged result bytes in the canonical raw zone, a manifest written last, and one exact finalization status. |
| **Implementation boundary** | `src/domain/producer`, `src/ports/outbound`, `src/adapters/producer`, and `IngestionStack`. |
| **Guarantees** | Least-privilege cross-account write, immutable VersionId binding, deterministic replay, one-object delivery, and no alternate publication authority. |

## Documentation map

- [Architecture](architecture.md) owns the enabled-producer components and finalization flow.
- [Low-level design](low-level-design.md) owns interfaces, governed pack bindings, and local risks.
- [Provenance Chain](../../concepts/provenance-chain.md) owns producer obligations and landing completion.
- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) owns registration/delivery clocks and objective results.
- [Validation results](../../../validation/RESULTS.md) own sanitized acceptance conclusions.
