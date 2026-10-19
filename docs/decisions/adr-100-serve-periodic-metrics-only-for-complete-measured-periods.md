# ADR-100: Serve periodic metrics only for declared complete periods, resolved against data latency

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Implemented in the
repository on the server side (2026-10-08): points 1 to 7 and 9, and the server half of point 8. The
browser half of point 8 landed with UI slice 9a (2026-10-10): the committed declaration is at
`temporal_control_version` 2, and a page that declares a calendar preset serves the version-2 control
([Temporal control contract §8](../ui/temporal-control-contract.md#8-version-2-adr-100-point-8-implemented-in-ui-slice-9a)).
The first periodic metrics are `buyers_new_to_brand_12mo` and `buyers_attributed` (2026-10-09): the
dictionary declares `period_grains` and the `time_grain` selector, and the measurement tool answers
chat at a complete measured period through `resolve_chat_period`. Not deployed.
Amends, once accepted: the closed preset model of
[ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md). The current design is
in [Business outcome measurement](../concepts/business-outcome-measurement.md); the temporal wire
contract stays owned by [Temporal control contract](../ui/temporal-control-contract.md).

## Context

A shopper count exists only for the exact period AMC counted it over
([ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)). The packs count complete
ISO weeks (Monday to Sunday), calendar months and calendar quarters, all in UTC. A rolling 7-day window
can't be built from them.

Today's serving can't express that:

- v1 `PageTime` is `period`, `day` or `exact_source` (`src/domain/presentation/page_schemas.py:67-72`), and
  a v1 `period` page requires daily-additive metrics.
- v2 presets are closed to `1d`, `7d`, `14d` and `30d` (`page_schemas.py:616-622`), comparison policies
  are `required_equal_prior`, `optional_equal_prior` and `none`, and there is no as-of mode.
- The temporal wire contract accepts only rolling presets or a custom range, and has no vintage selection
  ([Temporal control contract](../ui/temporal-control-contract.md)).
- The periodic fact class is declared and the answer layer already adds a "periodic" qualifier
  (`src/domain/measurement/answers.py:520-527`), but no metric uses it yet.

Data latency adds a second problem. The exposure and frequency packs land 16 days after a period ends,
because each shopper's 14-day window runs past period end
([ADR-101](adr-101-model-shopper-groupings-as-disjoint-axes-with-one-window-per-shopper.md)). So the
calendar's last complete week is never published yet when it would first be shown.

## Decision

**A periodic metric is served only for an exact declared period. A calendar preset resolves to the newest
period whose data has actually landed, and the page names that period.**

1. **Period grain is an exact selector.** A request pins one `time_grain`, like an attribution model
   ([ADR-055](adr-055-govern-temporal-attribution-as-an-exact-selector.md)). The planner selects rows by
   exact `period_start`, `period_end`, `time_grain` and `grain_set`, and refuses anything else with the
   nearest served period as the alternative.
2. **Daily numerators align to the period.** A daily-additive numerator over a periodic denominator (for
   example spend per new buyer) sums exactly the days of the period. If any day lacks a current
   publication, the ratio is refused.
3. **Rolling windows refuse shopper counts**, with the complete-period alternative.
4. **Calendar presets.** `last_complete_week`, `last_complete_month` and `last_complete_quarter` join the
   closed preset enum. Their server labels are "Latest measured week", "Latest measured month" and
   "Latest measured quarter". The comparison `prior_calendar_period` is allowed only when every candidate
   preset of a page is a calendar preset.
5. **Resolution against latency.** A pure domain function, `resolve_calendar_preset`, takes `as_of: date`
   and the current publications. It returns the latest complete period of the preset's grain, ending on or
   before `as_of`, for which every periodic contract read by the anchor sections has a current publication
   and every daily operand covers every day. Anchor sections with no periodic contract resolve on daily
   coverage alone. The search looks back at most 13 months. If nothing qualifies, the result is `none` and
   the page says when the first period is expected.
6. **One period per page.** Every section uses the resolved period. Its label is built on the server (for
   example "September 2026"). A non-anchor section with no publication for that period shows pending or
   missing text, resolved per request ([ADR-107](adr-107-derive-paid-feature-availability-from-delivery-evidence.md)).
7. **Page-schema additions.** v1 gains `PageTime` values `declared_period` and `as_of_vintage`. v2 gains
   `vintage_policy` (selector `as_of_month`) as an alternative to `temporal_policy`. A v2 section that binds
   a periodic metric declares `declared_periods` and `period_anchor`. On a v1 `period` page, a metric slot
   with `declared_periods` is left out of the three existing period checks only when it's bound in matching
   v2 sections, declares covering `period_grains` and carries the `time_grain` selector. A new page that
   binds periodic metrics carries a v1 entry as well as a v2 entry, so those slot checks reach it.
8. **Temporal transport v2.** The wire contract moves to version 2. `TemporalSelection.v2` adds the three
   calendar preset ids and a `vintage` kind (`as_of_month`). `TemporalControl.v2` adds mode `vintage`,
   `resolved.as_of_month`, a server-built `resolved.period_label`, display grains `month`, `quarter` and
   `as_of_vintage`, a newest-first list of at most 13 vintages, comparison status `not_applicable` with
   reason `calendar_period_selected`, and the unavailable reason `no_complete_period_measured` with
   `expected_after`. The forecasting design's horizon selection joins the same union, so there is one
   version bump for both.
9. **Chat** resolves "last month" through the same function and names the period it used.

This ADR decides time contracts only. Which pages use which presets, and how pages are laid out, is owned
by each page package in the [Decision Workspace UI](../ui/README.md#2-approved-page-roster) (manifest
revision `2026-10-08.1`, approved under the owner's UI/IA approval of 2026-10-07).

## Consequences

- "Latest measured week" for reach and frequency shows a week that ended 16 to 22 days ago, an arithmetic
  consequence of the 16-day lag. The label names the dates, so it's never mistaken for last week.
- Artifacts bake only on publication transitions (ADR-070). Between transitions a bake may show an older
  period than a fresh bake would, but it's labelled with the period it shows.
- `docs/ui/temporal-control-contract.md` is updated in the implementing slice, not here, together with the
  decoder refusal tests. That contract is an approved UI design document under the design manifest, so its
  version-2 section lands with the browser half, through the UI design approval; until then point 8 and
  `src/domain/presentation/temporal_v2.py` state the server half.
- The `none` result's date is the newest complete period's end plus the slowest lag the anchors depend
  on. A period qualifies only when every anchor has it, so the slowest anchor decides when the first can
  appear.
- The forecasting design shares the v2 bump. Whichever design lands second rebases and keeps version 2, and
  its "a page declares exactly one of" wording names all three policies.

## Alternatives considered

- **Serve the calendar's last complete period.** It's usually not landed yet, so pages would be blank for
  up to 16 days.
- **Let each section pick its own latest period.** One page would show two windows, which the frontend
  rule of one resolved window per page forbids.
- **Rolling windows for shopper counts**, for example a trailing 90 days re-run each period. Amazon's
  optimal-frequency playbook counts impressions over 90 days [verified 2026-10-05, AMC optimal frequency
  playbook]; whether Amazon intends that window to roll is [unverified]. Distinct counts can't be rolled
  from smaller periods, and overlapping periods in one option set trip the load's overlap check
  (`src/domain/curation/assertions.py:574`). A calendar quarter is the nearest complete period.

## Review trigger

Reopen when a source supplies distinct counts for rolling windows, or when the 16-day lag changes.
