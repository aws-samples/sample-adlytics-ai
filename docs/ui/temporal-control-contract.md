# Hybrid temporal-control contract

| | |
|---|---|
| **Type** | Shared UI interface concept |
| **Status** | Design baseline for the implementation specification; not implemented by the current V1 transport. Amended 2026-09-15: Campaign Portfolio withdraws Custom ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)); the Custom grammar remains for pages that declare it. |
| **Owns** | Server-declared preset/Custom request and result grammar, temporal result states, capability invalidation, and shared acceptance |
| **Parent** | [Decision Workspace UI](README.md) |

## 1. Purpose and boundary

This contract defines the shared hybrid reporting-window behavior for Decision Workspace pages. It gives a browser a small, server-declared set of choices—`1D`, `7D`, `14D`, `30D`, and bounded **Custom**—without granting the browser authority to derive periods, comparisons, grain, availability, or analytical values.

This is a transport/control contract, not an analytical artifact contract. Each page LLD owns its page ID, exact route, declared candidate choices, comparison policy, display-grain policy, artifact binding, and typed evidence outcomes. A page may expose a fixed server-resolved window by returning no selectable controls. A page that exposes a control implements this contract in full.

## 2. Request union

A list, parent, or scope-discovery request accepts exactly one `TemporalSelection.v1` form:

```text
TemporalSelection.v1 =
  omitted
  | { kind: "preset", preset_id: "1d" | "7d" | "14d" | "30d" }
  | { kind: "custom", start: "YYYY-MM-DD", end: "YYYY-MM-DD" }
```

`omitted` asks the server to resolve the page’s configured default. It preserves the existing `preferred_default` or `partial_default` reporting-window provenance. A preset or Custom selection resolves as `user_selected`; that provenance does not replace the more specific selection kind below.

The browser must not send `visible_window`, `comparison_window`, a client-derived prior period, a grain, a metric list, a canonical selector, or unknown fields. Detail/child redemption endpoints remain capability-only: a window change is always a new parent/list request and never a field added to a detail request.

## 3. Result union

The new presentation transport profile is `PresentationResult.v2`. It preserves each page artifact’s version and meaning while making the temporal control explicit. The exact page wrapper remains page-owned.

```text
PresentationResult.v2 =
  | {
      envelope_version: 2,
      kind: "page",
      session_id: string,
      temporal: TemporalControl.v1,
      page: PagePayload,
      route_specific_capabilities?: object
    }
  | {
      envelope_version: 2,
      kind: "window_unavailable",
      session_id: string,
      temporal: TemporalControl.v1,
      reason_code: TemporalUnavailableReason,
      recovery: TemporalRecovery
    }
```

`window_unavailable` is deliberately distinct from an artifact’s `empty`, `insufficient`, `no_coverage`, `qualified`, or page-issue states. It means the requested temporal selection cannot yield a supported page result. It contains no artifact and never causes the browser to reuse stale evidence.

### Capability custody

Capability-only child/detail requests are available only after their owning page has minted the corresponding V2 parent-bound capability. They accept the page-owned opaque reference grammar and restore the immutable server-held parent temporal resolution. A request that includes raw windows, comparators, selectors, revisions, or unknown fields is refused before custody or query work.

```text
TemporalControl.v1 = {
  version: 1,
  mode: "fixed" | "selectable",
  resolved: {
    window: { start: "YYYY-MM-DD", end: "YYYY-MM-DD" },
    reporting_window_status: "preferred_default" | "partial_default" | "user_selected",
    selection: {
      kind: "default" | "preset" | "custom",
      preset_id: "1d" | "7d" | "14d" | "30d" | null
    },
    display_grain: "day" | "week" | "period"
  },
  comparison: {
    policy: "required_equal_prior" | "optional_equal_prior" | "none",
    status: "available" | "not_requested" | "not_applicable" | "unavailable",
    reason_code: ComparisonUnavailableReason | null
  },
  presets: [{
    id: "1d" | "7d" | "14d" | "30d",
    label: "1D" | "7D" | "14D" | "30D",
    availability: "available" | "unavailable",
    reason_code: TemporalUnavailableReason | null
  }],
  custom: {
    availability: "available" | "unavailable",
    bounds: {
      earliest_start: "YYYY-MM-DD",
      latest_end: "YYYY-MM-DD",
      min_duration_days: positive-integer,
      max_duration_days: positive-integer
    } | null,
    reason_code: TemporalUnavailableReason | null
  }
}
```

The `presets` array is always canonical-order `1d`, `7d`, `14d`, `30d`; each item may be unavailable at runtime. Runtime availability and Custom bounds are server-returned because they depend on authorized scope, source coverage, retention, compatible comparison availability, and display-grain support. The page schema may declare candidate presets and policy, but it never replaces the runtime declaration.

`TemporalUnavailableReason`, `ComparisonUnavailableReason`, and `TemporalRecovery` are closed page-schema vocabularies. A renderer must not turn arbitrary text into an action. At minimum the page schema distinguishes invalid/out-of-bounds Custom input, unavailable preset, no common/default interval, incompatible source coverage, no valid required comparison, and expired/stale temporal declaration. The page may map a reason to an explicit no-self-service disposition or to one returned generated recovery action.

## 4. Resolution, custody, and invalidation

1. The server validates a selection against the returned declaration, then resolves the exact current window, comparison status, and display grain.
2. The emitted `temporal.resolved.window` must equal the current period carried by the returned parent artifact. A child/detail artifact must preserve the parent resolution exactly in meaning.
3. Custom dates are inclusive and exact. The maximum requested duration is 90 calendar days inclusive; Campaign requires at least two days and every other page requires at least one day. Runtime source coverage may narrow those returned bounds but may not widen them. The server may re-grain a supported Custom window only when that returned grain, its exact rows, visual eligibility, comparison status, and qualification are represented in the artifact. It never fills gaps, aggregates client-side, or silently substitutes a different range.
4. A changed window invalidates page-local selections, child artifacts, capability references, visual narration references, and in-flight requests. The controller starts a new generation and discards stale results.
5. An expired or forged scope/window/campaign/product/channel capability refreshes the appropriate parent/list declaration. The browser never retries it with weaker custody.
6. A valid temporal window that returns missing or insufficient analytical evidence remains an artifact-owned outcome. A window that cannot be supported at all returns `window_unavailable`.

## 5. Page profiles

| Page | Mode | Candidate presets and Custom behavior | Comparison policy | Additional rule |
|---|---|---|---|---|
| [Campaign Portfolio](campaign-portfolio/README.md) | Selectable | `1D` is declared unavailable until a single-period, non-trend detail outcome is explicitly added. `7D`, `14D` are runtime-declared. `30D` is not a candidate (per-pack bake snapshot bound, ADR-070 amendment 2026-09-16) and returns unavailable. Custom is withdrawn ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)): `custom.availability` is always `"unavailable"` for this page. | Required equal-length prior. | A changed window creates a new list/capability set; detail accepts only the resulting opaque capability. Every window variant is served from a publish-time artifact. |
| [Retail health](retail-health/README.md) | Selectable | `1D`, `7D`, `14D` runtime-declared. `30D` is not a candidate (per-pack bake snapshot bound, ADR-070 amendment 2026-09-16) and returns unavailable. Custom is not declared: `custom.availability` is always `"unavailable"` ([ADR-070](../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)). | Required equal-length prior. | No child redemption exists; a changed window is simply a new parent artifact. Every window variant is served from a publish-time artifact. |

The page profile names candidate behavior, not live source availability. The returned `TemporalControl.v1` remains authoritative in every case.

## 6. Browser rendering and accessibility

The browser renders only the returned choices, selected state, resolved dates, display grain, comparison status, bounds, reason, and returned recovery. It must:

- expose a labelled keyboard-operable preset group and Custom entry point;
- keep unavailable choices visibly unavailable with programmatically associated reason text;
- retain focus in Custom validation/refusal state and announce the result;
- show the server-resolved period and grain after any selection;
- clear stale detail/child/narration content before presenting a new result;
- render an exact-data alternative for every eligible visual; and
- preserve equivalent controls, evidence, labels, and focus behavior in wide, regular, and compact layouts.

A page-level absence (`window_unavailable`) renders the adapter's required `absence` (the page's
title and the server's message) in the shared `PageAbsence` callout, which carries no live role, and
never calls the adapter's body render. The route controller owns one persistent status region: one
keyed `p.sr-only[role="status"]` element, the first child of its top-level fragment in every return
(loading, absence, page issue, page), so it exists, empty, before an absence message arrives and is
never remounted (WCAG 4.1.3). It holds the absence's title and message, and is empty otherwise; the
loading note and the page error keep their own roles. See
[page absence copy](page-absence-copy.md) ([ADR-139](../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md)).

## 7. Shared acceptance

A page cannot mark temporal controls complete until its schema/generation, handler, strict decoder, controller, browser, and non-production evidence jointly prove:

1. canonical preset ordering and closed identifiers;
2. preferred-default and partial-default behavior on omitted selection;
3. every available preset resolves one exact server window;
4. bounded Custom accepts inclusive valid input and refuses invalid, out-of-bounds, stale, and unsupported input without normalization;
5. required, optional, and absent comparison policies remain distinct;
6. `window_unavailable` remains distinct from every analytical artifact outcome and page issue;
7. a resolved parent, capability snapshot, and redeemed child/detail agree on period, selection, grain, and allowed comparison semantics;
8. no client code derives dates, prior periods, display grain, values, aggregates, ratios, or visual points; and
9. cancellation, expiry refresh, keyboard/focus, screen-reader announcements, responsive layout, and mockup-state coverage behave as specified.

## 8. Version 2 (ADR-100 point 8, implemented in UI slice 9a)

The declaration header is `temporal_control_version: 2`. A page whose entry declares a calendar
preset (`last_complete_week`, `last_complete_month`, `last_complete_quarter`) speaks version 2. Every
other page keeps the version-1 selection and control in §2 and §3 byte for byte, so Campaigns and
Retail health read exactly as before.

```text
TemporalSelection.v2 = TemporalSelection.v1
  | { kind: "preset", preset_id: "last_complete_week" | "last_complete_month" | "last_complete_quarter" }

TemporalControl.v2 = TemporalControl.v1 with
  version: 2,
  resolved.display_grain: "day" | "week" | "month" | "quarter" | "period",
  resolved.period_label: string | null,         // server-built, calendar periods only
  resolved.as_of_month: null,                   // preset controls only
  presets: [{                                   // the page's declared candidates only, canonical order
    id, label,                                  // "7 days", "Latest measured month", …
    availability, reason_code,
    reason_text: string | null,                 // shown inside an unavailable option
    window: { start, end } | null,              // what the preset serves now, from its pointer
    period_label: string | null                 // a calendar preset's served period
  }],
  comparison: … | { status: "not_applicable", reason_code: "calendar_period_selected" }

PresentationResult.v2 window_unavailable, version 2 adds:
  message: string,                              // every reason: the text the page shows
  expected_after: "YYYY-MM-DD" | null           // no_complete_period_measured only
```

Rules the server and the strict browser decoder both hold:

1. A calendar preset the page doesn't declare is refused before any read; a version-1 page refuses
   every calendar preset.
2. A calendar period carries its period label and a calendar display grain; a rolling window carries
   neither. On an `optional_equal_prior` page a calendar period shows no comparison (reason
   `calendar_period_selected`), and only a calendar period does.
3. An unavailable preset carries its server text. The browser never maps a reason code. A page-level
   absence message is chosen by the server from the [page absence copy](page-absence-copy.md)
   catalog by its trigger (amended 2026-10-16, ADR-139).
4. Every version-2 `window_unavailable` carries the server's text (rule 3). Only
   `no_complete_period_measured` carries an expected date (amended 2026-10-10, slice 9a: a pending
   preset's text was browser-written before). A `domain` refusal shows the server's validated
   `error` text; the browser's PI-DOMAIN text is only a fallback for one that carries none (amended
   2026-10-16, ADR-139).
5. Custom is never offered on a version-2 page: every one is served from preset artifacts.

The browser renders a preset version-2 control as one segmented control (rolling presets, a divider,
then calendar presets), each option showing its label and the dates or period it serves now, and as a
labelled select below a 560 px container. A `vintage_policy` page instead uses this separate closed
shape; preset and Custom fields are absent. It serves only the latest vintage (owner decision
2026-10-14), so it is fixed and carries no roster:

```text
VintageControl.v2 = {
  version: 2,
  mode: "fixed",
  resolved: {
    as_of_month: "YYYY-MM",
    period_label: string,                       // server-built, rendered without formatting
    label_text: "Cohorts through {period_label}", // the page's static label, server-built
    window: { start, end }
  } | null
}
```

The request carries no temporal selection: the server refuses any, a `vintage` included, before a
retained-store read. When no vintage pointer is published, `window_unavailable` carries
`resolved: null` and the server-owned page message, and no label renders. The browser renders
`label_text` as served and performs no date, label, value, rate, or total calculation. A bounded
historical vintage roster is deferred work.
`prior_calendar_period` remains the preset-page comparison policy described above.
