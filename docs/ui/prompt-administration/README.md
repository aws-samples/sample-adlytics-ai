# Prompt Administration

| | |
|---|---|
| **Level** | Delivery page package |
| **Status** | Built; live-accepted on dev 2026-09-15 ([dated result](../../../validation/RESULTS.md#command-center-frontend-rebuild--2026-09-15)). Version authorship was added on 2026-10-01 (ADR-069 amendment). The page still renders the card lifecycle only; the tier-aware composition is an open follow-up. Revision history: [design manifest](../design-manifest.yaml). |
| **Design revision** | `2026-10-09.2` |
| **Owns** | Operator lifecycle of insight-slot prompts: inspect, version, calibrate, activate, retire |
| **Parent** | [Decision Workspace UI](../README.md) |

## 1. Purpose and scope

Prompt Administration lets an authorized operator manage the versioned task prompts behind the
page insight slots (trend shape, efficiency drift, funnel, contribution, divergence, exposure,
next check). Each slot's prompt is an immutable versioned record; "editing" mints a new version,
activation is gated on that version's calibration cases passing, and retirement disables a slot
without destroying the history that cached insights and audit records still reference.

The page carries no customer data and no analytical evidence. It is an operator control surface
behind an explicit operator authorization boundary, following the precedent of
[ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md).
The registry semantics this page fronts — immutable versions, calibration-gated activation,
retirement without destruction — are decided by
[ADR-069](../../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md);
the insight slots whose prompts it manages are decided by
[ADR-068](../../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md).

## 2. Where this fits

Target route `/admin/prompts/`, inside the workspace shell but under a distinct **Admin**
navigation section that renders only for authorized operators. The insight slots this page
manages are declared by the insight-contract schema (owned by the insights feature package when
it lands); this page never defines a slot, only the prompt versions a declared slot runs.

## 3. Black-box contract

| Concern | Contract |
|---|---|
| Slot roster | Read-only projection of declared insight slots with purpose, output class, active version, calibration state. |
| Version lifecycle | Create version (immutable), activate (calibration-gated), rollback (activate a prior version), retire slot. Versions are never mutated or destroyed. |
| Calibration | Every version carries its calibration run result; an unpassed version cannot activate. The UI states failures in plain words with the failing case names. |
| Cache linkage | The prompt version is part of the insight cache key, so activation invalidates exactly one slot's cache. The page states this consequence before an operator confirms activation. |
| Authorization | Operator-only; the browser receives no admin capability when the caller is not an operator. |
| Provenance | Every activation records who, when, from-version, and to-version; the audit trail is append-only. |

## Delivery map

- [High-level design](high-level-design.md) — topology, registry API roles, activation lifecycle.
- [Operational design](operational-design.md) — latency, authorization, retention, failure containment, observability, and cost.
- [Low-level design](low-level-design.md) — wire contracts, states, storage shape, UI states, tests.
- [Mockup](mockup.html) — fictional visual acceptance reference in the command-center language.
- [Shared UI policy](../README.md) — cross-page design rules.
