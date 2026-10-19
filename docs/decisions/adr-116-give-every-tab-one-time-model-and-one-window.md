# ADR-116: Give every tab one time model and one window
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner reviewed and approved
its mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's
delegated overnight authorization, and the open choices it contains were taken as recommended, without
owner objection. Not implemented. Amends
[ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md) (the pointer gains its
windows, decision 4 below) when accepted.

## Context

Every v2 page declares exactly one temporal policy [repo: `presentation/pages/v2/pages.yaml:13-21`],
and the window-scope rule forbids values from two windows side by side
[repo: `.kiro/steering/frontend-ui-standards.md:14-18`]. The new pages use rolling presets, calendar
periods, cohort vintages and forecast horizons. Their sources land on different days, so the same
preset can end on a different day on two pages. A link from one page to another can't assume that the
target shows the same dates.

## Decision

**A tab is one page with one time model, one artifact and one window. A child view opens in place on
its parent's window, and a link to another tab names that tab's own dates.**

1. **One page, one window.** Each tab keeps its own `temporal_policy`, `vintage_policy` or
   `forecast_horizon_policy`. No tab shows figures from two windows.
2. **Children live in place.** Campaign detail opens on `/campaigns/` and product detail on
   `/retail-health/`, on the parent's route, window and freshness, with one history marker so Back
   closes them. The URL never holds a selection, because opaque selections never appear in URLs
   [repo: `docs/ui/README.md:120`].
3. **The Overview is its own baked page.** Page entry `overview` has one `7d` preset resolved on the
   common coverage of its figure cards, with `max_card_lag_days` (default 2) so one late source can't
   drag the landing page back. It's the only surface that repeats a home tab's figure, always for its
   own window and with a line naming the target's later data. It reuses the
   `/api/presentation/overview` path. The request-time handler and the v1 `decision-overview` entry
   retire in that slice, and `OVERVIEW_PATH` leaves `ANALYTICAL_PAGES`
   [repo: `src/adapters/inbound/presentation_handlers/handler.py:515-522`] as it joins
   `V2_PARENT_PAGE_IDS` (`:537-546`).
4. **In-content drill links name the target's own dates.** `PresetArtifactPointer` gains
   capability-free `resolved_window` and `comparison_window`, written when the pointer advances. Today
   it carries none [repo: `src/domain/presentation/preset_artifacts.py:422-431`]. A source page reads
   each target's `CURRENT` pointer when it's served and builds "Open Campaigns · Amazon DSP · Sep 9–15"
   [fictional]. A failed read gives "latest {preset}" text and a warning log; the page never fails for
   a link. Nav items and tab strips show labels only, never dates.

## Consequences

- The Overview costs one small extra bake per publication in its cohort, instead of composing hub
  artifacts whose windows can differ.
- A drill link can name later dates than the source page. The Overview states that per card.
- Campaign detail can't be linked or restored from a URL. A non-opaque campaign slug would need its
  own guardrail decision.

## Alternatives considered

- **Compose the Overview from the hubs' current artifacts.** Two windows side by side.
- **Give children their own routes.** A child would need a selection in the URL, or its own window.
- **Drill links without dates.** A reader would assume the target shows the source page's dates.

## Review trigger

Reopen when a page needs two time models on one screen, or a child needs its own URL.
