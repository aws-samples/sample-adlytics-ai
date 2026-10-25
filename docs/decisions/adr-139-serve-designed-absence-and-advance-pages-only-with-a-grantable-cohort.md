# ADR-139: Serve designed absence, and advance pages only with a grantable cohort
Date: 2026-10-16
Status: Accepted (2026-10-16). The owner approved all four layers on 2026-10-16 ("yes go with all
four"). On 2026-10-17 (UTC) the owner approved `insight_slot_unavailable` as a designed absence, the
`GraphServingWithheld` alarm, and last-good recovery for a withheld cohort with no re-drive (below,
"Consequences"). Deploy waits on one owner approval: the absence copy in
[page absence copy](../ui/page-absence-copy.md).

> **Deployment marker, 2026-10-18 (UTC).** Deployed to dev from 2026-10-17 and live-accepted there:
> a cohort with only designed absences authorized serving, `GraphServingWithheld` fired on a
> withheld cohort and cleared on the next serving one ([D2 live acceptance](../../validation/RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18)).
> The decision is unchanged.

> **Proposed amendment, 2026-10-18.** [ADR-142](adr-142-isolate-a-bake-refusal-to-its-own-scope-and-alarm-on-defects.md) (Proposed) would give designed absence two more scopes inside a published body (one item, one non-anchor section), add a closed isolated-refusal class with one failed span, one bounded log line and one metric per refusal group, and make an over-budget task an alarm. [ADR-141](adr-141-reach-every-audiences-campaign-through-grouped-reads-and-per-campaign-pages.md) (Proposed) would retire the Audiences page-level over-bound absence. This decision stays in force, unchanged, until they are accepted.

> **Marker, 2026-10-25 (UTC).** The Audiences page-level over-bound absence row and the body it
> served are deleted ([ADR-141](adr-141-reach-every-audiences-campaign-through-grouped-reads-and-per-campaign-pages.md) amendment of 2026-10-25).

## Context
In dev every grant-bearing page returned 409 `ungranted_generation` because one empty source made
every cohort ungrantable. Reach, Audiences and Retention had no Amazon Marketing Cloud data yet, so
their roster rows correctly baked `<page>_materialization_unavailable`. That made the cohort's
binding `PARTIAL`, and presentation served a typed direct binding only when `COMPLETE`. The finalizer
had already moved `CURRENT` onto that refused generation in the same transaction that committed the
binding, so no older generation was left to serve. The refusal reached only a log line: its metric is
a domain error, which never alarms.

## Decision
1. **Designed absence is a valid result.** One closed domain rule
   (`unavailable_class`, `src/domain/operations/execution_graph_release.py`) classifies each
   `unavailable` entry. A page's own generated `<page>_materialization_unavailable` on that page's key,
   and `insight_slot_unavailable` on any key, are designed absences; every other reason, an unknown
   page or a malformed key is a failure. One predicate, `binding_authorizes_serving`, says whether a
   binding serves: direct, executed, not stale, names a revision, and has no failure entry. The
   finalizer gate, the store's final-transaction shape check, presentation's
   `require_granted_generation`, the governed-generation probe and ops check O3 all call it.
   Exact-input reuse stays strict (`COMPLETE` only).
2. **Last good by construction.** The finalizer commits page-pointer members only when the binding it
   commits authorizes serving, so `CURRENT` always names a generation presentation accepts and its one
   `CURRENT` read is the last-good read. A withheld cohort (`serving_withheld`: a failure entry, or a
   non-authorizing binding that names a revision) still completes and stores its binding for audit.
3. **Server-owned absence copy.** Each page-level absence message is chosen by trigger from one
   catalog, rendered by one shared `PageAbsence` callout and announced by one persistent status region.
   A `domain` refusal shows the server's own `error` text.
4. **Refusals and withheld cohorts page.** `PresentationServingRefused` counts every grant-serving
   refusal with its settle class against `FINALIZER_BLOCKED_AFTER` (270 s); its alarm pages
   `past_settle`. `GraphServingWithheld` (0/1, missing data ignored) pages a withheld cohort until a
   later cohort finalizes without being withheld. `make check-serving-refusals` (O3) proves both live.

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a page that refuses
every generation shows no data at all, and classifying by reason keeps a genuinely failed cohort off
every page while a designed absence serves.

## Enforced by
- `tests/unit/test_delivery_cohort.py`: the closed classification, the one predicate, `serving_withheld`
  and their paired failures.
- `tests/unit/test_graph_bake.py`: a withheld cohort moves no page and signals once; a crash after the
  commit is repaired by the redelivery (claim path, terminal-node path, `ALREADY_FINALIZED`, failed
  re-read retried); an all-designed-absence cohort is not withheld; the store refuses pointer members
  under a non-authorizing binding.
- `tests/unit/test_presentation_handler.py`: a designed-absence binding serves with `built_at`, last
  good serves the prior grant, and each refusal is counted with its settle class.
- `tests/unit/test_page_absence_copy.py` and `frontend/test/page-absence-copy.test.ts`: the catalog
  doc equals the code.
- `frontend/test/presentation-v2-route-controller.test.tsx` and `frontend/e2e/static-export.spec.ts`:
  the absence renders through `PageAbsence` for every adapter, the status region keeps one instance,
  and refusal text is the server's.
- `infra/test/serving-refusal-alarm.test.ts` and `infra/test/ingestion-stack.test.ts`: both alarms.
- `tests/unit/test_dev_operations.py`: the O3 classifier, and O3 reading `past_settle` with exactly
  the synthesized alarm's Metrics Insights query.

## Consequences
- Every dev page serves once presentation deploys: the live bindings carry only designed absences.
- A withheld cohort's pages stay on their last-good generation until the next publication's cohort
  finalizes without being withheld. No operator re-drive produces a new cohort for the same input, so
  `GraphServingWithheld` stays in `ALARM` for that whole time. The owner approved this on 2026-10-17
  (UTC).
- A grant-serving refusal now means the write-side invariant broke, so it pages.
- A cohort of designed absences only completes with no pointer members: each page keeps its last-bound
  body or serves its typed absence.

## Alternatives considered
- **Read-side fallback** (walk `REVISION#` history to the newest grantable predecessor). Rejected by
  (1) and (4): one to N strong reads per refused request, a dependency on 30-day history, a reopened
  D7 Q1, and a `CURRENT` that names a generation never served.
- **Accept any `PARTIAL` direct binding.** Rejected by (1): it would serve a cohort whose precompute
  ran out of budget or hit a consumer invariant.
- **Publish an absence pointer** (D7 Q2 body N). Rejected: the owner chose body K
  (`KEEP_LAST_BOUND`, 2026-10-09), and it would replace a page's last-good body with an absence.

## Review trigger
A second release domain, which would break the rule that a fresh `FINALIZED` commit is the newest bound
cohort, or a need to withhold per page rather than per cohort.
