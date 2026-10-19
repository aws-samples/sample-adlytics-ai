# ADR-118: Render every page from one set of shared patterns and extended tokens
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner reviewed and approved
its mockups on 2026-10-07 and set the token, responsiveness and conformance rules below that day. The
workflow orchestrator approved the design the same day under the owner's delegated overnight
authorization. Not implemented. The pattern catalogue is in
[Information architecture](../ui/information-architecture/README.md#6-shared-patterns).

## Context

The UX review of the dev deployment (2026-10-05) found the same defects on several pages: charts that
don't start at zero, a prior series at 1.80:1 contrast, dark primary button text at 2.72:1, tables
that scroll sideways, text under 12 px, three provenance strips in one dialog, and an assistant that
overlays the page below 1420 px [repo: `frontend/components/ChatPanel.tsx:21-23`]. The owner set UX
decisions on 2026-10-05, approved the mockups on 2026-10-07, and required that the built UI uses the
project's design tokens, is production-ready and responsive at every verified width, and is checked
against the mockups on the deployed page with real data.

## Decision

**Every page is built from one set of shared components, styled only from the project's design tokens.
New tokens are added at the token source, and the deployed page is checked against the approved
mockups with real data.**

1. **Tokens.** The token source is `frontend/app/globals.css` [repo: `frontend/app/globals.css:10-99`].
   It gains `--stroke-strong`, `--chart-prior-strong`, `--skel` and `--pill-soft`, and dark `--on-ai`
   becomes `#070b13`. Components never hard-code a colour, size or shadow. Text is never under 12 px.
2. **Shell.** Navigation follows [ADR-115](adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md).
   The assistant is docked at every width and open by default. The floating launcher is removed, and
   the top-bar toggle is the only control. From 881 px its width is clamped so the main column never
   drops below 448 px, and "Narrower" and "Wider" buttons give a single-pointer alternative to
   dragging (WCAG 2.2 SC 2.5.7). Below 881 px it's fixed at 320 px.
3. **Landmarks and headings.** Skip link, then the header, then `nav` "Hubs", then `main`, then the
   labelled `aside` "Assistant", with a "Skip to assistant" link that shows on focus. The h1 is the tab
   name and takes focus on a route change. At 881 px and wider the top bar keeps the full crumb (hub,
   then tab, then the entity when one is open; the hub item is omitted when the tab has the hub's
   name); below 881 px the crumb shows only its last item, and the full path is at the top of the
   slide-out sheet. On a hub with more than one visible tab, the hub header keeps its eyebrow at
   every width, as the approved mockups show. A hub with one visible tab has no tab strip
   ([ADR-115](adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)
   decision 2) and no eyebrow.
4. **Section order.** Scope band, headline, insights, "Also changed" (only while alerts are deferred),
   the primary table or visuals, then "About these figures". A section a tab doesn't have is skipped,
   never shown empty.
5. **Typed absence.** A missing figure reads "Not available" with a plain server reason and next step,
   never zero and never hidden. One rule covers a partly reported day on every surface:
   - *Missing day.* The figure shows over the days that arrived and its basis names them ("6 of 7
     days"). A ratio leaves the day out of both parts. The change is withheld, and the chip's place
     reads "{n} day(s) not yet sent." with "See why: {figure}".
   - *Partial day* (the source sent the day for some entities only). The chip's place reads "{n}
     partial day(s)." with "See why". Counts include the entities that reported and say so, ratios
     leave the day out of both parts, and the change is withheld.
   - On a stacked table row whose cell is a typed absence, the basis line drops its coverage part, so
     "6 of 7 days" never sits beside "—". This rule overrides the approved mockup state that still
     shows both, and conformance checks the build against this rule for that state.
   The reasons and missing dates come from the coverage block in
   [ADR-119](adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md).
6. **Dialogs.** One shared modal primitive with a sticky header. A dialog opened from inside another
   replaces it, and closing returns focus to whatever opened the first, so there's one modal at a time.
   One "How this is calculated" disclosure per dialog; the basis label stays visible in the dialog
   body.
7. **Charts.** Zero-based for bars, counts and currency totals. A ratio or rate line may use a padded
   range only with a visible "Axis doesn't start at 0" note. The prior series is dashed in
   `--chart-prior-strong`, and every chart has a "Show values" table.
8. **Tables.** One data table component. Tables stack below an 800 px container (CSS container queries,
   no dependency); tiles and controls narrow below 560 px. Stackable tables carry explicit table roles,
   and the scroll region is named by the caption's title. A stacked table keeps 12 to 16 px of space
   above its caption, so its title never reads as a note on the block above.
9. **Other owner UX rules.** A "Copy reference" icon with an accessible label and a toast. Data is fixed
   before display; there's no data-quality notes feature. There's no marketplace selector: each tab
   stacks one card or row set per marketplace, never converted.
10. **Conformance.** A page is done when the deployed page, with real data, matches its approved mockup
    states at 1440, 1280, 1248, 1180, 1100, 1024, 1008, 881, 880 and 768 px in both themes, with zero
    axe violations, no sideways scrolling and no text under 12 px. Unit tests and mockup checks don't
    stand in for this.

## Consequences

- Every page changes visually, so each build slice carries a conformance check on dev
  ([Delivery slices](../ui/information-architecture/delivery-slices.md)).
- Phone width stays out of scope, as the owner decided on 2026-10-05.
- Screen reader output and the axe "incomplete" contrast results weren't checked on the mockups. The
  build checks stacked tables with VoiceOver in Safari. Full WCAG validation needs manual testing with
  assistive technologies and an expert review.

## Alternatives considered

- **Per-page styling from the mockup CSS.** The mockups are references; production code reimplements
  them through typed components [repo: `docs/ui/README.md:114`].
- **A component library dependency.** The owner ruled out new dependencies.
- **Stack tables at 560 px like tiles.** Tables scrolled sideways by up to 235 px between 560 and 799 px
  in the mockup measurements.

## Review trigger

Reopen when phone width comes into scope or a page needs a component outside this set.
