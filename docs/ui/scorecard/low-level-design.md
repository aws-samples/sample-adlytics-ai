# Scorecard — low-level design

## 6. Wire, artifact, and renderer contracts

Marker key: [repo] is read in this worktree on 2026-10-08 with `file:line`; [proposal] is a choice made in
this package that the build slice may still change through review.

### Exact API inputs

| Route | Method and auth | Accepted body | Explicitly forbidden |
|---|---|---|---|
| `/api/presentation/scorecard` [proposal] | `POST`, the custom request authorizer | `{session_id}`, optionally `temporal_selection: TemporalSelection.v2` restricted to `omitted` or a declared preset | custom ranges, a caller comparator, raw windows, channel or campaign selectors, an attribution model (the artifact carries every model), unknown fields |
| `/api/presentation/scorecard-metric-focus` [proposal] | `POST`, same authorizer | `{session_id, metric, temporal_selection}`; for a ranking row also `channel` (a governed channel key on the card) and `model` (a registered model) | any grain or window field beyond the preset, campaign selectors, unknown fields |

The page follows the shared [temporal-control contract](../temporal-control-contract.md):
`PresentationResult.v2` with the control and either the artifact or `window_unavailable`.

### Page declarations

New page id `scorecard`, route `/scorecard/`, governed label "Scorecard", in the Performance hub with tab
order 1 ([ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)).
A new page carries a v1 and a v2 entry.

| File | Entry |
|---|---|
| `presentation/pages/v1/pages.yaml` | `mode: parallel`, because the page shows two bases: Amazon-attributed returns and the mixed-basis TACoS. A `parallel` page needs at least two bases and a boundary section [repo: `src/domain/presentation/page_schemas.py:727`, `:731`]. `grain.time: period`, dimensions `[marketplace]`. Metric slots for the network card, the ranking, the channel table and the TACoS and spend rows. Every slot is daily-additive, so all three v1 period checks run as today |
| `presentation/pages/v2/pages.yaml` | `mode: selectable`, `boundary_required: true`, `navigation: {hub_id: performance, tab_label: Scorecard, tab_description: Return on ad spend per network and channel, order: 1}`. `temporal_policy`: `candidate_presets: [7d, 30d, last_complete_week, last_complete_month]`, `default_preset: 7d`, custom disabled, `comparison_policy: optional_equal_prior` (rolling presets only), `display_grains: [period]`. Artifact `NetworkScorecardArtifact` v1, outcomes `[complete, empty]`. The materializer bakes `empty` when no channel delivers and nothing when a channel's delivery is partial or withheld, so no input yields an `insufficient` page (slice 9a, 2026-10-11) |

v2 sections [proposal for the ids the business outcomes design didn't name]: `workspace-scope`,
`network-card`, `model-ranking`, `scorecard-insights`, `also-changed`, `channel-table`, `pointers`,
`ad-spend-and-retail-sales`, `evidence-trace`, `boundary`. The amendments of 2026-10-07 remove
`new-to-brand`, `gateway-products` and `reach-and-funnel-summary`, so no section declares
`declared_periods` and no slot is periodic.

### Pointer lines

The `pointers` section holds two governed lines, and each renders only when the session's navigation
manifest lists its target tab:

- "New-to-brand customers and gateway products are on Customers › New customers.", by tab id
  `new-customers`, the page id the information architecture gives New customers (ia.md §9,
  `NewCustomersArtifact`).
- "Try a budget move in Budget simulator.", by tab id `simulator` (ia.md §6.3, J11's `page_id: simulator`).

The browser's shared pointer component reads the manifest by tab id, and a page's tab id is its page id
(the rule is in the
[Retail health page action](../retail-health/low-level-design.md#page-action-repeat-purchase-by-product-added-2026-10-08-design-revision-2026-10-081)).
Neither line is a declared page action, because the schema would refuse an action whose target page isn't
declared [repo: `src/domain/presentation/page_schemas.py:314`], and neither target page exists when this
page ships. So the Scorecard ships in S1.1 without either line showing, and each line appears with no page
change when its target's page entry, with a `navigation` block, lands.

### Window policy

- Rolling presets `7d` and `30d` compare with the equal prior window. Calendar presets show no comparison,
  and the scope band says "No comparison for calendar periods" (reason `calendar_period_selected`).
- With no periodic anchor left, a calendar preset resolves on daily coverage alone: the latest complete
  ISO week or calendar month (UTC) whose every day of spend is covered
  [repo: `src/domain/presentation/calendar_presets.py:194`]. The whole page then shows that one period.
- Calendar presets need `temporal_control_version` 2. The server half exists; the browser half and the
  header flip are pending ([ADR-100](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)).
  Until they land, the build can ship the rolling presets only.
- A `last_complete_quarter` preset stays deferred.

### `NetworkScorecardArtifact` v1

| Section | Content | Rendering responsibility |
|---|---|---|
| Network card | One per ecosystem × marketplace, never converted across currencies. `roas_deduplicated_incl_halo_14d` under every registered model, with the server's default model named; "Ad spend, all channels" (`ad_spend`, additive across channels). Compare slots carry the server `change` block on rolling presets ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)) | Headline tile and spend tile; the model selector switches which server value shows, with no re-request, like the Retail health trend metric switcher |
| Model note | Server text: every model credits each deduplicated purchase once, so the headline is the same under each; the model changes how revenue splits across channels; channel rows don't depend on a model | Note under the selector |
| Model ranking | Per governed channel: `roas_deduplicated_incl_halo_14d`, its rank under each registered model (none for a model that credited it nothing, see Designed states), and spend; the outcome sentence from `robust_leader`, `model_sensitive` or `insufficient`; boundaries from the retiring module [repo: `src/domain/presentation/channel_robustness.py:21-45`] | "Does the ranking hold under every model?" table, with "Details ›" per row opening that channel's ROAS by day under the default model |
| Insights | Fixed roster `network-scorecard` v1 with slot states `filled`, `silent`, `unavailable` | Insights row |
| Also changed | At most five direction-only rows (below) | List; the section is skipped when empty |
| Channel rows | Per governed channel with delivery in the window, in the media taxonomy's channel order (`contracts/shared/media-taxonomy.yaml`; the ranking rows follow the same order): `ad_spend`, `attributed_revenue_14d`, `roas_attributed_14d`, `roas_attributed_incl_halo_14d`, each with its basis label, and the row's Campaigns link text | Table with no total row; the foot sentence below; one "No {channel} campaigns in this workspace." line per absent governed channel |
| TACoS | `tacos` at ecosystem × marketplace, or its typed absence | Row with the mixed-basis label and a link to Retail health |
| Spend across networks | `ad_spend` per `marketplace_key × currency_code` when two or more advertising platforms are loaded there; otherwise the single line "Networks loaded: Amazon" | Row; never one row across two marketplaces |
| Evidence status | `EvidenceStatus.v2` for every tile and row with a change chip ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)) | Coverage sheet |
| Page context and starter questions | `page_context_label`, `page_context_expires_at`, `starter_questions` ([ADR-120](../../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)) | Assistant chip and starter buttons |
| Boundary | Fixed statements: attributed returns stay inside one network and one channel; TACoS mixes bases | "About these figures" |

### Basis labels (server-built from the metric dictionary)

| Figure | Label |
|---|---|
| Channel attributed revenue and ROAS | "Amazon-attributed · 14-day · conversion date · excludes brand halo" |
| ROAS with brand halo | "Amazon-attributed · 14-day · includes brand halo" |
| Network headline | "Deduplicated across DSP and sponsored · {model} · 14-day · includes brand halo" |
| TACoS | "Mixed basis: ad spend ÷ Vendor shipped revenue" |

The channel table's foot reads "Attributed sales aren't added across channels, because one purchase can be
credited to more than one channel."

### Designed states

Every text is a server field. The browser never maps a reason code.

Browser-owned copy (slice 9a, 2026-10-10; matching rule revised in round 3). The browser writes
only these, because no response exists yet or the words belong to a shared component every page
uses. A vitest walk (`frontend/test/scorecard-view.test.tsx`) renders each served Scorecard
response through the route adapter: complete, month, absent cell, two marketplaces, Also changed,
empty, pending and no complete period. Every text node must be, whole and exact, a
string that same response served, a figure formatted from its served display value, the page's
`display_name`, a separator (`·`, `:` or `.`), or a row of this table. So a word cut from a served
sentence fails, and so does text another state served. The walk can't tell a hardcoded copy of
this response's own served string from the field that supplies it. That case is left to review.

| Text | Why the browser owns it |
|---|---|
| "Loading Scorecard" (status name) and "Loading Scorecard…" | Shown before the first response arrives |
| "Read more ›" and its name "Read more: {served card name}" | The shared insight card's dialog opener |
| "Up", "Down", "favourable", "unfavourable" (spoken before a change chip) | The shared change chip ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)), from the served `direction` and `sentiment` |
| The window control's "Window" legend and the insight dialog's chrome | Shared components ([temporal-control contract](../temporal-control-contract.md), [insight presentation contract](../insight-presentation-contract.md) §3) |
| [PI-DOMAIN](../page-absence-copy.md#pi-domain): "This page's latest data is still being checked before it's shown. Try again later." | The shared `PageIssue` fallback when a `domain` refusal carries no server text |

The page name in a page-level callout is the governed `display_name` from the page entry. "Not
available", the "Channel" row header, the "This campaign's channel" tag, the text for an
unpublished window, a `domain` refusal's own `error` text
([PI-DOMAIN-SERVER](../page-absence-copy.md#pi-domain-server)), the window control's preset labels and an Also changed row's dialog
narration are served fields. Money and multipliers render from their served two-place
`display_value` ("3.96×", plan S26). TACoS, a rate, goes through the shared percent formatter.

| Condition | What renders |
|---|---|
| Deduplicated delivery withheld | Headline "Not available" with the plain reason; the ranking reads "Not available" with its reason; channel rows still render (mockup state "Deduplicated ROAS suppressed") |
| No DSP campaigns | No DSP row, and the card says "No DSP delivery in this window" |
| No Vendor Central sales | TACoS "Needs Vendor Central sales" |
| Vendor Central days missing | TACoS "Not available" with "Vendor Central sales for {dates} didn't arrive. The latest available is {date}." and a link to Retail health at its latest window |
| One advertising network loaded | "Networks loaded: Amazon", with no figure |
| Two marketplaces | Two cards and two spend-across-networks rows, never one. Each TACoS and spend tile names its marketplace ("US marketplace", "US marketplace · USD"). So does each insight card (after its kind: "Channel mover · US marketplace") and each Also changed row (after its scope), and their served names ("{headline} · US marketplace") label their controls and dialogs. Campaigns filters by channel, not marketplace, so no insight card offers its Campaigns step. With one marketplace none of these names a marketplace, and the steps show |
| A delivering channel with no spend in the window | Its ROAS and ROAS with halo cells read "Not available" with "No ad spend on this channel in this window, so there's no spend to divide its return by." The ranking ranks no channel and reads "Not available: {channels} had no ad spend in this window, so channels can't be ranked under each model." |
| A channel that spent but a model credited nothing (owner decision 2026-10-22) | That model's ranking cell reads "No deduplicated credit" (served `credit_text`) and states no rank; the governed 0 stays the cell's served `value`. The other channels rank among themselves, and the outcome sentence is unchanged. It replaced "0.00× 2nd", a tie at zero that read like a broken figure beside the channel table's attributed ROAS |
| Complete headline values that differ by model | Each model credits each deduplicated purchase once, so the governed query can't produce this; malformed input can (the rotating dev feed found 2026-10-22). The bake still publishes, and the ranking is `insufficient` with "Not available: the attribution models credited different deduplicated totals for this window, so channels can't be ranked under each model." It never names a leader |
| Arrived from campaign detail | The named channel's row is outlined and tagged ("This campaign's channel"), from the `highlight_channel` step target ([ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)). The target names a channel, not a marketplace. With two marketplaces the tag names its row's marketplace ("This campaign's channel · US marketplace"). When that channel appears in more than one marketplace's card, no row is outlined, because the page can't tell which row is the campaign's |
| No complete calendar period | The `no_complete_period_measured` text from the server; no scope band ([information architecture](../information-architecture/README.md#6-shared-patterns)) |
| No published body for the preset (page-level) | Page-level absence: [ABS-SC-1](../page-absence-copy.md#abs-sc-1) (rolling) or [ABS-SC-2](../page-absence-copy.md#abs-sc-2) (calendar), with [ABS-ALT](../page-absence-copy.md#abs-alt) when another preset is published; no scope band |

### Channel row links

Each channel row opens `/campaigns/?preset={preset}&channel={channel key}` when Campaigns declares that
preset and that channel filter
([ADR-127](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md));
`preset` is left out when it equals the Campaigns default. Its text is built at serve time from the
Campaigns pointer, for example "Open · Sep 9–15" [fictional]. When the pointer is absent or the read fails,
the text names "latest {preset}" and a warning is logged; the page never fails for a link. On a preset
Campaigns doesn't declare, the link names Campaigns' default. One pointer read serves every row, because
filter siblings share one pointer per preset.

### Insight contract: `network-scorecard` v1

Output class `observed`. Deterministic findings `channel-share-of-network-spend` and `channel-roas-mover`,
each computed inside one network and one channel, with materiality per horizon following the
`portfolio-exposure` pattern. Calibration includes a rejection case that adds shares of two channel totals,
a rejection case that sums attributed revenue across networks, and a known-correct DSP share. Every card
sentence names its channel or "inside the Amazon network". The baked artifact carries the deterministic
findings, so "Also changed" and the Overview can read them through `ArtifactFindingsReadPort`.

On a calendar preset there's no comparison, so `channel-roas-mover` is silent. The approved month-state
mockup shows a "Channel return" card ("Sponsored Products had the highest ROAS"). That's a level reading
the two findings above don't produce. The build slice adds it to the contract as a third deterministic
finding, `channel-roas-leader` [proposal], or the card doesn't render.

### "Also changed"

The owner deferred the alert evaluator (OD-7, 2026-10-07), so this tab renders the alerts-declined path:

- It lists the artifact's baked deterministic findings at or above a governed alert threshold for the
  current window, at most five, one line each, direction-only, each opening its insight dialog.
- A finding that an insight card on this tab already narrates isn't repeated.
- Thresholds come from the governed alert rules: rule id, insight contract, finding kind, scope, direction,
  threshold and horizon, with each threshold at or above the contract's materiality. The default rules over
  `network-scorecard` are the DSP ROAS drop, the DSP spend spike and the Sponsored Products ROAS drop
  ([ADR-108](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md)).
- The rules file and its validator ship with this page's slice [proposal]. The evaluator, alert store,
  pointer stream, stacks and alert routes aren't built.
- With no qualifying finding, the section is skipped, never shown empty. No row states a cross-channel
  attributed total or its change.

### Metric focus

The network headline, "Ad spend, all channels", each channel's ROAS and each ranking row open the shared
focus dialog ([metric-focus contract](../metric-focus-contract.md)). A ranking row's focus shows that
channel's deduplicated ROAS by day under the default model (mockup state "Ranking: one channel by day"). The
payload derives from the served artifact; serving it opens no measurement path.

### Materializer

A new Scorecard materializer under the ADR-075 pattern. Per preset it bakes: the channel-grain attributed
batch, one deduplicated query at `ad_product` grain across the three registered models, the TACoS operands
and the spend rows. It also bakes the deterministic findings. Today only Campaigns and Retail health have
real materializers; other pages reach the typed unavailable fallback
([preset artifact publisher](../preset-artifact-publisher.md)).

## 7. Concepts applied

- [Business outcome measurement](../../concepts/business-outcome-measurement.md)
- [Preset artifact publisher](../preset-artifact-publisher.md)
- [Hybrid temporal-control contract](../temporal-control-contract.md)
- [Insight presentation contract](../insight-presentation-contract.md)
- [Theme preference and restrained glass](../theme-preference-contract.md)

## 8. Decisions

- [ADR-106: keep outcome metrics inside one network by type](../../decisions/adr-106-keep-outcome-metrics-inside-one-network-by-type.md)
- [ADR-100: serve periodic metrics only for complete measured periods](../../decisions/adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)
- [ADR-108: raise in-app alerts when page artifacts publish](../../decisions/adr-108-raise-in-app-alerts-when-page-artifacts-publish.md) (deferred; fallback path)
- [ADR-115](../../decisions/adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md), [ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md), [ADR-118](../../decisions/adr-118-render-every-page-from-one-set-of-shared-patterns-and-extended-tokens.md) to [ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)
- [ADR-126: render the Scorecard ranking under each attribution model](../../decisions/adr-126-render-the-scorecard-ranking-under-each-attribution-model.md)
- [ADR-127: the Campaigns channel filter](../../decisions/adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md)

## 9. Delivery prerequisites and acceptance

| Concern | Required proof |
|---|---|
| Page declaration | v1 and v2 entries pass `make validate-page-schemas`; the generated bindings and route manifest list `/scorecard/` |
| Artifact and decoders | `NetworkScorecardArtifact` v1 exact serializer and strict browser decoder: basis labels, typed absences, `change` blocks, `EvidenceStatus.v2`, ranking outcomes |
| No cross-channel attributed total | A page-schema or artifact test fails on an attributed total row, a channel-group subtotal row, or a starter question or "Also changed" row that states a cross-channel attributed total |
| Per-network rule | Spend across networks renders figures only with two or more advertising platforms in one marketplace and currency; two marketplaces give two rows |
| Model selector | Switching the model changes the headline's model label and value only from server values; channel rows and the ranking don't change |
| Insight contract | `network-scorecard` v1 with its calibration corpus, shipped through the registry path |
| Also changed | Unit fixtures: a finding above its threshold lists; one already narrated by a card doesn't; none renders no section |
| Channel links | A row links to the matching Campaigns filter with serve-time dates; a missing pointer falls back to "latest {preset}" text |
| Pointer lines | A frontend test: with no `new-customers` tab in the session's manifest, no New customers line renders; with it, the line links to `/new-customers/`. The same pair for the simulator line |
| Conformance | Rule 4 of the [delivery slices](../information-architecture/delivery-slices.md#2-rules-for-every-slice) against every mockup state in this package, on dev with real data where it exists |
| Live | Deployed dev bake observed end to end and recorded in `validation/RESULTS.md` |

## 10. Open items

| Item | What resolves it | Blocks |
|---|---|---|
| The model selector changes no figure today | The owner of that business outcomes requirement keeps it (current) or retires it through an amendment | Nothing |
| The month-state "Channel return" card needs a third finding | The build slice adds `channel-roas-leader` to `network-scorecard` v1, or the card doesn't render | That card only |
| Calendar presets need temporal transport v2 in the browser | The carried transport v2 slice | The two calendar presets |
| New-to-brand and gateway products render only on New customers (OD-4). Their page package is [New customers](../new-customers/README.md) (`NewCustomersArtifact` v2, revision `2026-10-09.2`); its server slice is business outcomes waves 2 and 2b. New buyers per channel are on that page; new buyers per campaign are answered in chat only. This page renders no new-to-brand figure | The New customers page entry, with its `navigation` block, landing in the UI sub-slice | The New customers pointer line only |
