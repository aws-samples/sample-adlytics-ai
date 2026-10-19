# Page absence copy

| | |
|---|---|
| **Type** | Shared UI copy catalog |
| **Status** | Current development implementation under [ADR-139](../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md); the wording awaits owner approval before any deploy |
| **Owns** | Every server-owned page-level absence text, the serving-refusal text, the one browser fallback for a `domain` refusal, and the browser-owned Campaigns and Retail health absence text |
| **Parent** | [Decision Workspace UI](README.md) |

Each v2 `window_unavailable.message` is chosen from this catalog by an explicit trigger
(`src/domain/presentation/absence_copy.py`, `AbsenceTrigger`, whose values are the IDs below). The
server rows equal `ABSENCE_COPY`, `OTHER_WINDOWS_READY` and `SERVING_CHECK_TEXT`, and PI-DOMAIN equals
the browser's `PI_DOMAIN_FALLBACK` (`frontend/components/PresentationV2RouteController.tsx`). ABS-CAMP-1,
ABS-CAMP-2, ABS-RH-1 and ABS-RH-2 equal the `CAMPAIGN_ABSENCE_…` and `RETAIL_HEALTH_ABSENCE_…` constants
(`frontend/components/CampaignRouteAdapter.tsx`, `frontend/components/RetailHealthRouteAdapter.tsx`),
whose v1 contract carries no server text. Equality tests on both sides read this file. `{period_label}` is the served period's label. Each page LLD's
"Designed states" table links its row here and restates no text.

**Truth rule for the "no `CURRENT` pointer" rows.** That trigger fires when the source has never
delivered, when the data has landed but this preset's bake has not committed yet, or when the source
returned nothing for this one period, and the server cannot tell them apart. So no row says the data
"hasn't arrived" or is "being prepared". Each says only that the page isn't ready to show, names what
the page is built from, and says what it will show. The closing "you'll see" clause holds for every
outcome the page's artifact declares (`empty`, `insufficient`) and every preset the trigger covers, so
it is conditioned on the data and never promises a section some covered case doesn't render.

The page renders the text in the shared `PageAbsence` callout below the window control: a heading with
the page's governed `display_name`, the message, no live role and no focus move. The controller's one
persistent status region announces it ([temporal contract §6](temporal-control-contract.md#6-browser-rendering-and-accessibility)).

![Reach absence (ABS-REACH-1 with ABS-ALT), light theme](../images/reach-absence-light.png)

![Reach absence (ABS-REACH-1 with ABS-ALT), dark theme](../images/reach-absence-dark.png)

## Server rows

### ABS-REACH-1

- Surface: Reach
- Trigger: the selected calendar preset has no `CURRENT` pointer (`preset_unavailable`).
- Today: "This window isn't published yet. Choose another window."
- Text: "Reach for {period_label} isn't ready to show yet. It's built from your Amazon Marketing Cloud sponsored ads data, plus Amazon DSP where you run it. When it's ready, you'll see the reach, frequency and full-funnel figures your data supports for this period."

### ABS-AUD-1

- Surface: Audiences
- Trigger: the latest complete month has no `CURRENT` pointer.
- Today: "This window isn't published yet. Choose another window."
- Text: "Audiences for {period_label} isn't ready to show yet. It's built from your Amazon Marketing Cloud audience segment data. When it's ready, you'll see which audience segments your buyers belong to, where there are enough shoppers to show them."

### ABS-AUD-2

- Surface: Audiences
- Trigger: the month's body is `AudienceProfileUnavailable` with `audiences_body_over_bound`.
- Today: "This window isn't published yet. Choose another window."
- Text: "Audiences for {period_label} has more segment and campaign detail than this page can show. Ask the assistant about a DSP campaign to see its figures."

### ABS-RET-1

- Surface: Retention
- Trigger: no vintage `CURRENT` pointer (`vintage_unavailable`).
- Today: "No Retention vintage is published"
- Text: "Retention isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data. When it's ready, you'll see how many of the new customers your ads won come back to buy again, where there's enough evidence to show it."

### ABS-SC-1

- Surface: Scorecard
- Trigger: a rolling preset (7 or 30 days) has no `CURRENT` pointer.
- Today: "This window isn't published yet. Choose another window."
- Text: "The Scorecard for this window isn't ready to show yet. It's built from your Amazon Ads data, plus Vendor Central sales where you have them. When it's ready, you'll see the return on ad spend by network and channel your data supports for this window."

### ABS-SC-2

- Surface: Scorecard
- Trigger: a calendar preset whose period is measured has no `CURRENT` pointer.
- Today: "This window isn't published yet. Choose another window."
- Text: "The Scorecard for {period_label} isn't ready to show yet. It's built from your Amazon Ads data, plus Vendor Central sales where you have them. When it's ready, you'll see the return on ad spend by network and channel your data supports for {period_label}."

### ABS-SC-3

- Surface: Scorecard
- Trigger: a calendar preset with no complete period measured.
- Text: unchanged, the calendar `none_text` owned by `calendar_presets.py`.

### ABS-NC-1

- Surface: New customers
- Trigger: a rolling preset has no `CURRENT` pointer.
- Today: "This window isn't published yet. Choose another window."
- Text: "New customers for this window isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data and your Amazon Ads spend. When it's ready, you'll see the new-to-brand purchases your data supports for this window. New buyers are measured for complete weeks and months."

### ABS-NC-2

- Surface: New customers
- Trigger: a calendar preset whose period is measured has no `CURRENT` pointer.
- Today: "This window isn't published yet. Choose another window."
- Text: "New customers for {period_label} isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data and your Amazon Ads spend. When it's ready, you'll see the new-to-brand purchases and new buyers your data supports for {period_label}."

### ABS-NC-3

- Surface: New customers
- Trigger: no complete period measured.
- Text: unchanged, the calendar `none_text`.

### ABS-ALT

- Surface: Reach, Scorecard, New customers (never Audiences)
- Trigger: appended, after one space, when the served control lists a published preset other than the selected one.
- Text: "Other windows above are ready to view."

### API-0002

- Surface: the API error body's `error`, shown on the page by `PageIssue`
- Trigger: an `ungranted_generation` or `serving_refresh_pending` refusal.
- Today: "This reporting window's baked generation is not served."
- Text: "This page's latest data is still being checked before it's shown. Try again later."

## Browser rows

### PI-DOMAIN

- Surface: every v2 page (browser fallback)
- Trigger: a `domain` refusal that reaches `PageIssue` with no server `error` text. No current path takes it; every `domain` refusal today carries validated server text.
- Today: "This page cannot be shown safely right now."
- Text: "This page's latest data is still being checked before it's shown. Try again later."

### PI-DOMAIN-SERVER

- Surface: every v2 page
- Trigger: every other `domain` refusal (for example "No reporting period is available for this page.").
- Text: unchanged server text. `PageIssue` shows the refusal's own validated `error` text as sent; this catalog adds no text for these refusals, and each message's home stays its `raise DomainError` site.

### ABS-CAMP-1

- Surface: Campaigns
- Trigger: `window_unavailable` and the served control has at least one available preset.
- Title: "Campaign portfolio is unavailable for this period"
- Text: "Campaign reporting is unavailable for the selected period. Choose another available period to view campaign results."

### ABS-CAMP-2

- Surface: Campaigns
- Trigger: `window_unavailable` and the served control has no available preset (custom periods are off for this page, so presets are the only choosable periods).
- Title: "Campaign portfolio is unavailable for this period"
- Text: "Campaign reporting isn't ready to show yet. It's built from your Amazon Ads data. When it's ready, you'll see the campaign results your data supports for each published window."

### ABS-RH-1

- Surface: Retail health
- Trigger: `window_unavailable` and the served control has at least one available preset.
- Title: "Retail Health is unavailable for this period"
- Text: "Choose another published reporting period to view Retail Health."

### ABS-RH-2

- Surface: Retail health
- Trigger: `window_unavailable` and the served control has no available preset (custom periods are off for this page, so presets are the only choosable periods).
- Title: "Retail Health is unavailable for this period"
- Text: "Retail Health isn't ready to show yet. It's built from your Vendor Central data. When it's ready, you'll see the sales, traffic, conversion and returns your data supports, per product."

## Why the wording

Reach, Audiences and Retention are Amazon Marketing Cloud packs (each LLD's "Materializer"). A Reach
instance without DSP still renders sponsored-side frequency, hence "plus Amazon DSP where you run it",
and ABS-REACH-1 promises no single Reach section: overlap and the full funnel need DSP and sponsored
ads on one instance, and frequency is not shown for the week preset or with fewer than four active
campaigns ([Reach LLD](reach-and-funnel/low-level-design.md)). The Scorecard reads Vendor Central
sales only for TACoS, so an account without it still gets a Scorecard ([Scorecard
LLD](scorecard/low-level-design.md)). `audiences_body_over_bound` fires on too many profile or
campaign rows or an over-cap body, and only DSP campaigns have a governed assistant read ([Audiences
LLD](audiences/low-level-design.md)). New buyers are a calendar-period section, so ABS-NC-1 (rolling
presets) promises only new-to-brand purchases ([New customers LLD](new-customers/low-level-design.md)).
Every closing clause is conditioned on the data because a ready page can still bake an `empty` or
`insufficient` outcome.
