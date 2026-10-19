# Analytics Discovery

| | |
|---|---|
| **Level** | L2 — capability overview |
| **Status** | Proposed — review required before implementation |
| **Owns** | The declared-capability discovery result and the purpose-built Explore experience that guides users to existing analytical pages. |
| **Conventions** | [Documentation conventions](../../conventions.md) |

## 1. Purpose and scope

Analytics Discovery helps an ads practitioner understand what the product can answer, where to begin,
and which existing analytical page fits a business question. It is a deliberately designed workspace
experience and a server-built orientation result for chat. It organizes declared capability around
business questions and existing page destinations; it is not a model-generated dashboard, raw metric
browser, customer-data query, or substitute for a typed analytical page.

Metric definitions remain owned by the governed metric catalog; page purpose, actions, and visual
surfaces remain owned by page declarations and [UI page intent validation](../../ui/README.md). Current
customer availability remains owned by selected coverage.

## 2. Where this fits

This capability refines the workspace discovery surface in the L1 [architecture](../../architecture.md#capability-index).
The [Conversational Runtime](../conversational-runtime/README.md) may use a separately approved future
orientation contract with this result, while the static Explore page renders the same declared
information through a predefined UI.

## 3. Black-box contract

| | |
|---|---|
| **Responsibility** | Materialize declared product capability as business-question guidance, page destinations, and honest availability distinctions. |
| **Inbound** | Active metric catalog, page declarations, declared actions, and optional bounded current-page context/availability state. |
| **Outbound** | A fixed Explore page model and a capability/orientation result that distinguish declaration from current customer availability. |
| **Owner** | Server-owned discovery assembler and page-owned Explore renderer. |
| **Guarantee** | Discovery never invents a metric, page, page action, current dataset, or customer result; it never lets the model select an ungoverned page or catalog. |

Workspace orientation is the default for platform-wide questions. Current-page orientation is explicit
rather than inferred from page presence. A request about another page resolves only through a declared
page destination; the model never authors a page ID or browser route.

## Documentation map

- [Architecture](architecture.md) owns components and discovery flow.
- [Low-level design](low-level-design.md) owns the discovery model, scope, and selection constraints.
- [Conversational Runtime](../conversational-runtime/README.md) owns the current managed-Harness input and response publication boundaries.
- [ADR-039](../../decisions/adr-039-assembled-context-replaces-the-declared-prompt.md) and [ADR-058](../../decisions/adr-058-plan-every-turn-and-treat-page-context-as-additive.md) retain superseded context-design rationale.
- [UI page intent validation](../../ui/README.md) owns the Explore page's visual purpose and interaction
  contract when that page is added.
