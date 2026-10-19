# ADR-122: Declare next-step targets as a typed union checked against navigation
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design, applying the owner's decision of
2026-10-05 that the server declares next-step action targets. The owner reviewed and approved the
mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's
delegated overnight authorization. The grammar, codecs, target check and browser decoder are
in code (Slice 5), not deployed, and no card serves a step yet. Amends
[ADR-076](adr-076-tier-insight-narration-across-card-and-deep-dive-models.md) (the next-step wire
grammar of insight cards) when accepted.

> **Amended 2026-10-07 (Slice 5 implementation), decisions 5 and 7 refined.** The routed tabs a
> target is checked against are the v2 page entries with a `navigation` block, which is how
> `presentation/navigation.yaml` lists presentation tabs; the Lambda bundle carries those entries,
> not the navigation file. `open_page` steps are declared by the page that serves them, so a card
> contract can't list the kind. `open_product_detail` is in the browser decoder; its server step
> ships with Retail product detail. Cross-tab date labels (decision 6) ship with the first
> `open_page` step. The wire shape is in the
> [insight presentation contract](../ui/insight-presentation-contract.md) §2.3.

## Context

Insight cards end with a next step. The decoder that the insight and data UX refresh branch ships
accepts five kinds with a string or null `target`, and for `ask_assistant` the string is the question
itself. That branch isn't merged, so it isn't cited here by line. A target that's a bare string can't
name a page, preset, channel filter or section, so the new hubs have no way to say "open the Scorecard
for this channel". Insight slots are cached by the baked artifact's revision
[repo: `src/adapters/inbound/presentation_handlers/handler.py:3704-3705`], while campaign capabilities
are owner- and session-bound and minted per serve
[repo: `src/adapters/outbound/dynamodb/page_context.py:375-379`], so a cached step can't hold a
capability.

## Decision

**`NextStep.v2` is a union keyed by `kind`, each kind with a closed key set and an object target.
Targets are checked against the navigation file when declarations load, and a chip renders only when
its target exists for the session.**

1. **Grammar.**

   ```text
   NextStep.v2 =
     | { kind: open_page,            label, reason, target: { page_id, preset_id?, filter_id?, section_id?, highlight_channel?, focus_campaign_ref? } }
     | { kind: open_preset,          label, reason, target: { page_id, preset_id } }
     | { kind: open_campaign_detail, label, reason, target: { page_id, campaign_ref } }
     | { kind: open_product_detail,  label, reason, target: { selection_reference } }
     | { kind: open_metric_focus,    label, reason, target: { page_id, metric, filter_id? } }
     | { kind: open_evidence_status, label, reason, target: null }
     | { kind: ask_assistant,        label, reason, target: { question } }
   ```

   `reason` and `question` are 1 to 240 characters. `preset_id`, `filter_id` and `highlight_channel`
   use the URL's governed ids.
2. **Questions are server-written and dated** from the insight's validated output and the artifact's
   window label. A question that states a cross-channel attributed total, or uses a causal verb about a
   full-funnel or frequency figure, drops the step.
3. **"Ask the assistant" never sends.** It closes a dialog it sits in, opens the assistant if it's
   closed, puts the question in the composer verbatim and moves focus there. The user presses Ask.
4. **Cached steps hold keys; served steps hold capabilities.** The cached step stores a server-only
   `campaign_key` or product key. At serve time the handler maps it to the capability issued in that
   response, or drops the step.
5. **Validation.** Targets are checked against `presentation/navigation.yaml` and the target page's
   declared presets and filters when the options are built; an undeclared target is a
   declaration-load failure. At serve time, a step whose `filter_id` isn't offered in this workspace is
   dropped, so no chip opens a fallback. The browser hides a chip whose target isn't in the session's
   manifest.
6. **Cross-tab chips name the target's dates** from its current pointer, under
   [ADR-116](adr-116-give-every-tab-one-time-model-and-one-window.md) decision 4.
7. **Campaign detail's channel step.** `inspect_channel_context` leaves `CampaignRecoveryAction`
   [repo: `src/domain/presentation/campaign_attribution.py:95-103`], whose target page retires
   [repo: `presentation/pages/v2/pages.yaml:53-55`]. Detail serves an `open_page` step to the
   Scorecard with `highlight_channel`, and the `campaigns` page's `actions` block is removed with its
   bindings regenerated, in the Campaigns slice. The step renders only once the Scorecard is in the
   manifest.
8. **Versioning.** A card wire-grammar version with a render-format bump, so every cached slot
   re-fills and an old-shape step never meets the new decoder.

## Consequences

- Every navigation target in an insight is declared data, so a dead chip is a load-time failure, not a
  runtime surprise.
- The campaign curve step to the simulator waits for a forecasting amendment that lets the simulator
  list redeem a Campaigns capability (`focus_campaign_ref`). Until then detail serves no such step.
- Model calls don't grow: steps are built from validated output already produced.

## Alternatives considered

- **A top-level `question` field.** Gives `ask_assistant` two fields where every other kind has one.
- **Keep string targets and parse them in the browser.** The browser would interpret routing data.
- **Cache capabilities in slots.** They're session-bound and would leak across sessions or expire.

## Review trigger

Reopen when a next step needs to act, not navigate. The platform is read-only.
