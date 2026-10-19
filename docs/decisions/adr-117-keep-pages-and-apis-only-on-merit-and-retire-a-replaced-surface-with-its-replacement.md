# ADR-117: Keep pages and APIs only on merit, and retire a replaced surface with its replacement
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner reviewed and approved
its mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's
delegated overnight authorization, and the open choices it contains, including when only-retired
surfaces go, were taken as recommended, without owner objection. Not implemented. The proposed page set
is in [Information architecture](../ui/information-architecture/README.md#4-keep-rework-or-retire).

## Context

Several existing pages and APIs were built for earlier designs. Five v2 page entries are declared and
never rendered [repo: `presentation/pages/v2/pages.yaml:57-270`]. The Media Efficiency API has no
Amazon API Gateway route [repo: `infra/lib/stacks/api-stack.ts:1258-1271`], although the UI baseline
said its presentation APIs remained deployed for reuse. The owner directed that existing pages and
APIs are used only on merit, never kept because they exist. A replacement built beside its
predecessor would leave two paths to the same fact.

[Workspace implementation cutover](../ui/implementation-cutover.md) owns the clean-replacement rules
and the retirement inventory for frontend screens. It doesn't say when a backend route, handler path
or page entry retires relative to its replacement.

## Decision

**Every existing page and API is kept, reworked or retired on merit, and a replaced surface retires in
the build slice that ships its replacement.** Both parts decide one question, when an existing surface
stays or goes, so they're one decision.

1. **Keep, rework or retire on merit.** Each item is judged against the business outcomes
   requirements, the owner's DSP and sponsored coverage gap, the owner UX decisions and the users'
   jobs. Anything kept is rendered in the new design, not its old layout. The table, with evidence, is
   in [Information architecture](../ui/information-architecture/README.md#4-keep-rework-or-retire).
   [ADR-124](adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md) sets the test for
   retiring a surface that has governed data.
2. **No dual path.** A surface that's replaced retires in the slice that ships its replacement: its
   route, handler path, page entries, frontend bindings, domain module and every symbol that
   references them. Surfaces that are only retired go in one cleanup slice after the hubs ship, so no
   route outlives its only consumer.
3. **One retirement inventory.** The cutover rules and inventory stay in
   [Workspace implementation cutover](../ui/implementation-cutover.md). When this ADR is accepted,
   each slice's retirement scope from
   [Delivery slices](../ui/information-architecture/delivery-slices.md) is added there as inventory
   rows, and a row retires only after its replacement's acceptance evidence is read.

## Consequences

- Removing routes is an `infra/` change that needs approval in its slice.
- The UI baseline's statement that the retired pages' presentation APIs remain deployed for reuse
  [repo: `docs/ui/README.md:9`] is false for Media Efficiency. The design manifest pins that approval
  scope [repo: `docs/ui/design-manifest.yaml:8`], so it's corrected with a manifest revision in the
  cleanup slice.
- A replacement slice is larger, because it carries its predecessor's removal.

## Alternatives considered

- **Keep unrendered pages and APIs for later reuse.** Leaves request-time APIs outside the publish-time
  pattern of [ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md), and a
  route without a consumer.
- **Retire everything at once after the new pages ship.** Leaves two paths to product detail between
  slices.

## Review trigger

Reopen when a replacement can't ship in one slice with its predecessor's removal.
