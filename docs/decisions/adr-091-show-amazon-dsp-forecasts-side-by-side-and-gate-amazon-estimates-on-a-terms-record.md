# ADR-091: Show Amazon DSP forecasts side by side, and gate Amazon estimates on a terms record
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner chose the side-by-side display on 2026-10-06, and on 2026-10-07 accepted the recommendation to store no Amazon estimate before the legal read. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. Two rules below are proposed defaults adopted under that approval and still open for the owner: keeping campaigns without our own curve at today's spend (condition 5), and sending saved settings only (point 5). **Display stays off until the owner records the legal read of the Ads API terms.** The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
Amazon's ADSP Forecasting API returns, for an existing DSP campaign, forecasts with low, mean and high values. That includes Amazon's two spend-capacity metrics, `CAS` and `TAS` (a capped and a total spend capacity; `CAS` is never above `TAS`), and, optionally, spend-versus-outcome curves. It is useful context next to our own DSP curve.
But it comes with three problems:
- **Two measurement systems.** It's Amazon's model, on a basis Amazon doesn't fully publish. Its numbers may not agree with our curve.
- **Terms are unread.** Whether Amazon estimates may be stored, shown or combined is a legal question the owner is reading personally.
- **No history.** No past forecasts can be retrieved, so it can't be backtested unless stored from day one.

Sponsored budget recommendations raise the same terms question.

This ADR records two linked choices: the side-by-side display, and the terms record that gates it. They are accepted or reversed together. The display can't switch on without the record, and the record exists first to gate the display.
## Decision
**Amazon's DSP curves are display-only context. Every Amazon estimate is gated by one governed terms record.**
1. **The five conditions.**
   - A source chip and the basis on every Amazon curve: "Amazon DSP forecast · Amazon's model · not in total".
   - Never in a total: the reallocation total rejects any `display_only` curve.
   - Disagreement shown plainly, compared only at matched spend, period and currency, and never averaged. `sources_disagree` is shown only when Amazon's mean at current spend falls outside our 80% band and our curve can drive totals.
   - Every pair logged as an internal benchmark record.
   - Campaigns without our own curve are kept at today's spend and flagged (proposed default, open for the owner). The Amazon curve may still be shown on them.

   Only spend-axis curves are drawn, and only within the returned range.
2. **One terms record.** `contracts/amazon-ads/terms.yaml` holds, per estimate kind (DSP curves, `CAS`/`TAS`, in-flight figures, budget recommendations), `storage_allowed` and `display_allowed` with who decided and when. The record is:
   - shipped by digest to the Finalizer, the snapshot publisher and the connector;
   - evaluated wherever an estimate is used, never copied into rows.

   So a revocation takes effect at the next revision triple.
3. **Off until the legal read.** Until the owner records the legal read, both flags are false (owner, 2026-10-07). The connector doesn't call those operations, and the Finalizer refuses such a landing.
4. **Spend capacity as an input, later.** Amazon's `CAS` may set DSP expected spend only after a planned check of DSP spend against budget and its fee basis, and the owner's DSP scenario decision.
5. **Saved settings only (proposed default, open for the owner).** Replanning settings are never sent. Editing a campaign to forecast other settings would be a write, so it's out of scope.
## Consequences
- Until the legal read, no benchmark log builds up, and capped sponsored campaigns can't use a recommendation as a demand bound. Their increases stay frozen.
- A deletion runbook step covers stored vintages if the terms later forbid storage.
- Two answers on screen invite anchoring on Amazon's number. The chip, the label and a visually primary custom curve mitigate it.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Ignore Amazon's forecasts | Loses useful context for DSP, where our own curves are thin |
| Use Amazon's curve in totals, or blend it with ours | Mixes two measurement systems in one figure |
| **Show it side by side, display-only, under five conditions** | **Chosen** (owner, 2026-10-06) |
## Open questions
- **The legal read (owner, personally): may Amazon estimates be stored, shown or combined?** Each answer is one reviewed change to the terms record. Storage off: nothing is called or stored. Storage on, display off: estimates are stored for the internal benchmark log and as a hidden demand bound, with no overlay. Both on: the overlay appears on the detail view. This gate doesn't affect the custom curves.
- Other open items, such as DSP budget scenarios, are listed in [Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items).
- **Do pre-flight campaigns get curves?** A planned probe checks it. If not, the view covers in-flight campaigns only.
## Evidence
- The ADSP Forecasting API forecasts existing campaigns only, returns low, mean and high values, and offers curves with x = `SPEND`, `BID`, `TAS` or `CAS` [verified 2026-10-05, Amazon Ads DSP forecasting FAQ, capabilities overview and metrics pages]. One campaign per request [verified 2026-10-05, Amazon Ads API v1 reference].
- The guide calls low, mean and high the 10th, 50th and 90th percentiles; the reference doesn't [verified 2026-10-05, the same pages]. The design treats that meaning as guide-only.
- The API keeps no history of past forecasts, so none can be retrieved to backtest [verified 2026-10-05, Amazon Ads DSP forecasting capabilities overview and FAQ].
- The forecast operation accepts only the `advertising::campaign_management` OAuth scope, which the reference labels write access. It isn't the API's only scope: the v1 reference defines four, none of them read-only [verified 2026-10-05, Amazon Ads API v1 reference, `securitySchemes` and the operation's security requirement]. So the scope can't make a call read-only. Read-only rests on the customer's Viewer grant and the connector's operation allowlist ([ADR-097](adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md)), not on the scope.
## Review trigger
The owner's legal-read answer; a change in Amazon's forecasting terms or API; or users acting on Amazon's figure in place of the custom curve.
