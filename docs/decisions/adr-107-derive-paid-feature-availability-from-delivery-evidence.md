# ADR-107: Derive paid-feature availability from delivery evidence

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06, with open decision 1 on
its default: show "available with the paid feature" and never hide it. Partly implemented (2026-10-08):
points 1, 2 and 4 exist in the domain, and no paid section is served yet.
Closes the Q-07 sub-question of whether "not licensed here" and "impossible" read the same
([decision coverage registry](../decision-coverage-registry.md#question-register-q-nn)). The current design
is in [Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

Two outcomes have a full path through AMC paid features: audience profiles through Audience Segment
Insights, and all-purchase repeat through Amazon Retail Purchases. Each keeps a standard-table path. The
paid tables are queryable at no cost through 2026-12-31 in NA, FE, AU and EU, and carry a subscription after
that date ([verified 2026-10-05, AMC paid features overview page]; recorded in [AMC](../sources/amc.md)
"Paid features", `docs/sources/amc.md:111`).

Adlytics can't ask AMC whether a customer is subscribed. A configuration flag would drift from reality. A
missing paid delivery could mean "not enrolled", "not yet landed this period" or "the producer failed", and
each needs a different message. Artifacts bake only when a publication changes
([ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md),
[ADR-075](adr-075-generate-page-artifacts-once-per-dependency-cohort.md)), so if a delivery never arrives,
nothing rebakes, and a baked "expected after" date would go stale.

## Decision

**Paid-feature availability is derived from delivery evidence, never configured. The state is resolved per
request, so it can't go stale.**

1. **State.** `PaidFeatureState` is computed from Publication State through the existing
   `CurrentPublicationPort`: `feature_observed` (any successful publication of the paid contract in the
   contract's latest three declared periods, whichever period is requested, so it's a deployment-level
   fact), `period_published`, `expected_after` (period end plus the pack's
   `cadenceLagDays`) and `latest_available_period`.
2. **Bake values, resolve text per request.** Materializers bake those four values, never a resolved text.
   The presentation adapter calls a pure domain function, `resolve_period_state`, with its own clock's date
   as `as_of`, and gets `published`, `pending` (on or before `expected_after`) or `missing` (after it). The
   domain function never reads a clock.
3. **Show, never hide (decision 1).** With no delivery ever observed, the panel reads "Needs the Amazon
   Insights paid feature (Audience Segment Insights). No data has arrived for this workspace." It never claims
   the customer isn't subscribed, because a failing producer looks the same. Pending and missing have their
   own server-built texts.
4. **Catalog.** The catalog tool gains the refusal reason `not_observed_in_deployment`, distinct from
   `unsupported`.
5. **Standard packs reuse it.** The same pending and missing texts serve any non-anchor section whose
   standard pack hasn't landed for the resolved period
   ([ADR-100](adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md)).

## Consequences

- A subscribed workspace no longer flips to "needs the paid feature" on the first days of a month or after
  one failed run.
- No configuration and no `infra/` change.
- Early customers are asked to enrol in the paid features before 2026-12-31 (open decision 2 = yes, a
  customer action). The standard paths ship either way.

## Alternatives considered

- **A subscription flag in configuration.** Drifts from what AMC actually delivers.
- **Hide the paid panel when nothing is observed.** Rejected under the owner's stated preference for an
  explicit "not available" state over hiding (2026-10-06). Decision 1 stays listed as open, on its default.
  If the owner picks "hide", paid panels aren't rendered, the agent answers "not supported here", and the
  state and catalog change go away; the standard paths are unchanged.
- **Bake the resolved text.** Goes stale when a delivery never arrives.

## Review trigger

Reopen when AMC exposes a subscription or entitlement read, or when the paid features' commercial terms
change.
