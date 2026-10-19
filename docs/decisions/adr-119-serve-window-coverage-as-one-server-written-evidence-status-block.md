# ADR-119: Serve window coverage as one server-written evidence status block
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner reviewed and approved
its mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's
delegated overnight authorization. Not implemented. Amends
[ADR-072](adr-072-name-why-evidence-is-insufficient-with-governed-slot-conditions.md) (the per-slot
conditions become one shared block on every v2 artifact) when accepted.

> **Amended 2026-10-07 (Slice 4 implementation), decision 4 refined.** Campaigns and Retail
> health bodies take the block with render formats 8 and 6; the browser reads a body without the
> key as uncovered until those pointers move. Campaign detail writes its block at serve time, so
> the browser requires it in the same release. The wire shape is in the
> [insight presentation contract](../ui/insight-presentation-contract.md) §2.4.
> **Amended 2026-10-09 (owner revision of 2026-10-08, simplified delivery), decision 4.** The
> block-less read is retired: the API, a full re-bake and the browser deploy together. Both sides
> require the `evidence_status` key on Campaigns and Retail health bodies, `null` exactly on an
> empty or insufficient body, and refuse a format-7 or format-5 body.

## Context

[ADR-072](adr-072-name-why-evidence-is-insufficient-with-governed-slot-conditions.md) gives campaign
detail per-slot conditions and a sheet that shows them. That sheet is the only coverage view today. It
renders only campaign detail's `CampaignEvidenceStatus`
[repo: `frontend/components/EvidenceStatusSheet.tsx:15`], and its only consumer is campaign detail
[repo: `frontend/components/CampaignPortfolioV2View.tsx:36`]. It maps slot and role codes to labels in
the browser, through `statusSlotLabels` and `statusRoleLabels`
[repo: `frontend/components/EvidenceStatusSheet.tsx:18`, `:34`, used at `:49`], although display
names are governed on the server
([ADR-067](adr-067-release-governed-display-names-never-canonical-keys.md)).

Under the information architecture, every tab shows tiles and change chips, and every partly reported
figure links to "See why". Each new surface also needs an absence state for data that's coming.

## Decision

**Every v2 artifact that shows a tile or a change chip serves one shared `EvidenceStatus.v2` block. The
"Coverage for this window" sheet renders only that block, on every tab.**

1. **Contract.** Per metric slot: the server display label, the role label, the missing dates, the
   reason text and the recovery text. Every string is written by the server.
2. **One sheet.** The sheet is titled "Coverage for this window", serves every tab's "See why" links,
   and renders only the block. `statusSlotLabels` and `statusRoleLabels` are removed.
3. **Typed absence uses it.** A missing or partly reported figure
   ([ADR-118](adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md)
   decision 5) and a surface whose data hasn't landed take their reason from the block. When the data
   lands, the server serves the figure and the absence text goes.
4. **Versioning.** A render-format bump. The server ships the block first, and the frontend requires it
   a release later.

## Consequences

- Every v2 artifact changes on the server before the sheet can drop its label maps.
- Coverage reasons have one pattern app-wide, so a new page adds slots, not a new sheet.
- When accepted, the block's wire shape joins the
  [insight presentation contract](../ui/insight-presentation-contract.md).

## Alternatives considered

- **Keep the browser label maps and add codes per page.** The browser would keep interpreting codes,
  and each page would add its own map.
- **A coverage contract per page.** Several shapes for one sheet.

## Review trigger

Reopen when a coverage reason needs an action beyond its recovery text.
