# ADR-126: Render the Scorecard ranking under each attribution model
Date: 2026-10-07
Status: Proposed. The owner directed on 2026-10-07 that model sensitivity renders if its data exists,
and the re-audit found governed data for it (Context). Needs a business outcomes design amendment, a
new requirement for this ranking, which that design's owner applies. Not implemented.

## Context

The Channel Performance page answered "does the channel ranking hold under every attribution model?"
at request time. Its route is deployed [repo: `infra/lib/stacks/api-stack.ts:1260`], its page entry was
never rendered [repo: `presentation/pages/v2/pages.yaml:57-101`], and its module measures
`roas_deduplicated_incl_halo_14d` under `first_touch_v1`, `last_touch_v1` and `linear_v1` with closed
outcomes `robust_leader`, `model_sensitive` and `insufficient`
[repo: `src/domain/presentation/channel_robustness.py:21-45`]. The metric is valid at `ad_product`
grain and requires a registered model [repo: `contracts/shared/metric-dictionary.yaml:1028-1046`]. The
Scorecard already owns a model selector, a business outcomes requirement. Every model credits each
deduplicated purchase once, so the network total is the same under each model; only its split across
channels changes [repo: `contracts/amc/query-packs/amc-deduplicated-temporal-attribution.sql:110-150`].

## Decision

**The Scorecard's network card gains "Does the ranking hold under every model?", baked with the
Scorecard at publish time. The request-time Channel Performance API and page entry retire.**

1. **Content.** Per governed channel, the deduplicated ROAS and rank under each registered model, and
   the channel's spend; a server outcome sentence from the closed outcomes; boundaries from the
   module's boundary text; and a "Details ›" per row that opens that channel's deduplicated ROAS by day
   under the default model (`scorecard-metric-focus` gains `channel` and `model`).
2. **Per channel only.** Never totalled across channels. Suppressed deduplicated sales, or a missing
   model, read "Not available" in place.
3. **Measured with the Scorecard.** One deduplicated query per preset at `ad_product` grain across the
   three models, in the Scorecard's bake.
4. **The model selector stays**, labelled "Attribution model for deduplicated ROAS". Its server note
   states that the network total is the same under every model and that the ranking shows how each
   splits it. The ranking shows all models at once, so it doesn't follow the selector. Whether to
   retire the selector is logged for the owner of the business outcomes requirement
   ([ADR-124](adr-124-render-governed-data-and-retire-a-surface-only-on-evidence.md) decision 4).
5. **Retirement.** The route, handler path and page entries retire in the cleanup slice. The module's
   outcome rules and boundaries move into the Scorecard materializer first, then
   `channel_robustness.py` goes.

## Consequences

- A media buyer sees in one place whether a channel's lead depends on the model.
- One more deduplicated query per Scorecard preset.
- The business outcomes design gains a requirement for the ranking, and its out-of-scope line, which
  names the retired page rather than this reading, narrows.
- A channel's deduplicated revenue never exceeds its Amazon-attributed revenue with halo, because
  deduplication only removes double credit, so the ranking can't contradict the channel table.

## Alternatives considered

- **Keep the request-time API and render its page.** Outside the publish-time pattern of
  [ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md), and a second home
  for the Scorecard's channels.
- **Make the selector re-rank the channel table.** The channel rows are attributed figures that don't
  depend on a model.

## Review trigger

Reopen when a fourth attribution model is registered, or the owner of the business outcomes
requirement retires the selector.
