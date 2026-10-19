# ADR-108: Raise in-app alerts when page artifacts publish

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06, but **not** the `infra/`
change this ADR needs (open decision 3). No `infra/` work starts before the owner approves it separately.
If it's declined, the fallback below applies. **The owner deferred the evaluator on 2026-10-07 (UI decision
OD-7), so v1 ships the fallback:** an "Also changed" section on each tab with qualifying findings and the
Overview's serve-time "What changed" block. Baked findings and `ArtifactFindingsReadPort` ship either way.
When the evaluator is added later, it comes in as a control-plane workload. Not implemented. The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The anomaly-alert outcome (G2) tells a user, inside the app, when something material changed: a DSP ROAS
drop, a spend spike, a retail mover. The deterministic findings that say so already exist inside baked page
artifacts, through the insight contracts
([ADR-068](adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md),
[ADR-075](adr-075-generate-page-artifacts-once-per-dependency-cohort.md)). Artifacts change only when a
publication changes. A scheduled evaluator would run when nothing changed, and could disagree with the page
if it recomputed metrics. The product is read-only: nothing is sent to Amazon or outside the app.

## Decision

**Alerts are raised by an event-driven evaluator over the deterministic findings already baked into page
artifacts, when an artifact pointer advances. Alerts are in-app only and show direction, not figures.**

1. **Trigger.** A DynamoDB stream on the existing artifact-pointer table feeds an evaluator Lambda. The event
   source filter admits only inserts and modifies of `CURRENT` pointer records, and the adapter skips a
   record whose revision didn't change.
2. **Pure evaluation.** A domain function takes the artifact's findings, the governed rules for that page
   and preset, and prior rule state, and returns alert events and new state. It never recomputes a metric
   and never imports the query port.
3. **Rules.** A governed rule file names one insight contract and finding kind, a scope (ecosystem and
   platform, with channel required for advertising platforms and forbidden for retail), a direction, a
   threshold at or above the contract's materiality, and the one preset it reads. Customer-editable
   thresholds are out of scope.
4. **Identity and re-arm.** An alert's identity is a hash of rule, page, preset, subject and finding window
   end, so a republish of the same window can't re-raise. A rule raises for a subject only when it's met now
   and wasn't at the previous evaluation, so a persistent drop raises once.
5. **Direction only.** An alert stores and shows the rule, subject, direction and the server-declared window
   label. No figures are stored. Opening an alert opens its source page at the rule's preset, where figures
   render inside that page's one resolved window.
6. **Store and stacks.** A RETAIN `AlertStoreStack` holds only the alerts table (it holds per-user seen
   state). A DESTROY `AlertStack` holds the evaluator, its dead-letter queue and the event source mapping.
   An `AlertPresentationFunction` on the existing ApiStack serves `GET /alerts` and
   `POST /alerts/{alert_id}/seen` on the existing authenticated cookie session, with the existing CSRF
   checks on the mutating route. Visibility is deployment-wide under
   [ADR-054](adr-054-scope-analytical-visibility-to-the-customer-deployment.md).

Where the alert list and its count appear in the app is designed in the
[information architecture](../ui/information-architecture/README.md): a top-bar badge and inbox. The owner
deferred the evaluator on 2026-10-07, so they aren't built in v1 (see Status).

## Consequences

- Alerts follow publication exactly; they never fire on data a page doesn't show.
- Adding a stream to the existing pointer table must be an in-place update. `cdk diff` in the infra slice
  must show that before any deploy; it's [unverified] today.
- IAM can't bind the seen-state key to the caller's Cognito subject, because one role serves every user. The
  handler builds that key only from the authenticated session, never from input.
- **If the owner declines the `infra/` change (the v1 path since 2026-10-07):** each baked tab gets an "Also
  changed" section listing findings above the alert thresholds for the current window, and the Overview's
  "What changed" card lists them across tabs. There's no stream, table, stack, function or route. The
  thresholds come from the governed rules above, which ship with the Scorecard slice as a contract and a
  validator only [proposal] ([Scorecard](../ui/scorecard/low-level-design.md)).

## Alternatives considered

- **A scheduled evaluator.** Runs when nothing changed, and either recomputes metrics or reads stale ones.
- **Email or push notifications.** Sends data outside the app; out of scope for a read-only product.
- **Figures in the alert list.** Alerts from different windows would sit side by side as numbers.

## Review trigger

Reopen when the owner decides open decision 3, or when customer-editable thresholds are requested.
