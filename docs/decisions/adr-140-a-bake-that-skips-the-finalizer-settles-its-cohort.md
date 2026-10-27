# ADR-140: A bake that skips the finalizer still settles its cohort
Date: 2026-10-17
Status: Accepted (2026-10-17). Option 1 was chosen on 2026-10-17 (UTC) with the owner informed, and
the owner approved its alarm-only `infra/` change and its dev deploy the same day.

> **Proposed amendment, 2026-10-18.** [ADR-142](adr-142-isolate-a-bake-refusal-to-its-own-scope-and-alarm-on-defects.md) (Proposed) would extend decision 6: a typed isolated refusal (a scale limit, a data-history gap, any other source refusal, or a dependency still failing after its retries) is a designed absence at the smallest scope its read feeds. A bake-policy statement deadline timeout, on any receive, and any other dependency error on a task's final receive are the page's designed absence, not `FAILED`; a dependency error in the pin pass pins that row as refused after a bounded retry instead of failing the cohort. Every isolated refusal is reported once per settled outcome, by the page execution both bake entries share, with one failed span, one bounded line and one metric datapoint; a settlement whose response was lost replays its stored refusals once. Implemented 2026-10-26 by the scale-limits change (PR #20) and live-validated on dev: where it differs from this decision, the amendment describes the code. ADR-142's acceptance is the owner's review.

## Context
Only the V3 finalizer arms precompute. A bake fan-out that ends without a finalizer therefore left
precompute `WAITING_INPUT` forever: U1, no curation to bake (`NO_ELIGIBLE`), and U2, a consumer
invariant (`UNAVAILABLE`, every key `post_grant_invariant`). `settle_cohort` needs both members, so
the cohort never bound, the serving-domain head stayed `ACTIVE`, and the promoter skipped the domain.
On 2026-10-16 (UTC) a winner with zero publications took this path and no dev page rebuilt after
23:21Z. Both bake gauges missed it, because they read only a `COMPLETE#` stamped with
`bake_readiness_at`, which only the finalizer writes. Only the protocol-ceiling alarm fired, about
1 h 49 min late.

## Decision
1. **The Release settles U1 and U2.** On its `COMPLETE#` loop, before completion, the Release reads
   the stored items. A `GRANTED` winner whose precompute is `WAITING_INPUT`, whose bake node is
   terminal and whose bake member is stored is checked by one closed domain rule,
   `unarmed_precompute_contribution` (`src/domain/operations/execution_graph_release.py`). With no
   fan-out node and no precompute member, exactly U1 (bake `SUCCEEDED`) and U2 (bake `FAILED`), each
   naming no revision, settle precompute `NO_ELIGIBLE`. One transaction
   (`settle_unarmed_precompute`) checks the run, the `ACTIVE` head and the bake member as read. It
   writes the precompute member, moves the node to `SUCCEEDED` with the consumer's own effect record,
   and writes the direct binding. Completion then drains the head as for any bound run. Every other
   shape raises `GRAPH_PRECOMPUTE/7` on every pass and pages. A bake with a revision and no finalizer
   would bind a generation no pointer serves. The rule reads only stored items, so it works whichever
   Baker version wrote them: a cohort wedged before the deploy, or elected during it, settles on the
   new Release's next pass.
2. **Eligibility is the cohort's.** A `NO_ELIGIBLE` direct binding becomes every loser's alias. The
   shared consumer's step 3 therefore asks whether any cohort member advanced curation. It reads each
   other member's frozen `publish` effect on that member's own grant, and only when the winner's own
   effect is not eligible. An unverifiable member is `GRAPH_POST_GRANT/8`, a consumer invariant
   (U2). Bake and precompute share the step, so they agree.
3. **A stale precompute settles withheld.** A precompute member whose digest or roster does not
   match the readiness no longer raises in the finalizer. The finalizer logs WARNING
   `finalize stale_precompute`, records `withheld_bake_contribution` (every roster key
   `post_grant_invariant`) and commits its existing final transaction with no page pointer, so
   `GraphServingWithheld` pages. This covers a new Baker beside an old precompute and a precompute
   invariant, which used to dead-letter the finalizer.
4. **The blocked gauges get their anchor.** An unstamped `COMPLETE#` is counted from its grant's
   stored bake member: `GraphBakePrecomputeBlocked` past `PRECOMPUTE_BLOCKED_AFTER` with no
   precompute member, `GraphBakeFinalizerBlocked` past `FINALIZER_BLOCKED_AFTER` with both. The
   alarm descriptions derive both windows from `BAKE_TASKS` and name the Release's failure lines.
5. **The Release signals a withheld completion.** When the Release completes a withheld binding (U2)
   it emits `GraphServingWithheld` = 1 and a WARNING `serving_withheld` line (`phase=complete`), at
   least once. It never emits `0`.
6. **A source refusal is one row's designed absence; a failure settles withheld** (D2 hardening,
   2026-10-17). A source's `DomainError` or `ValidationError` in a task's plan pass or render is
   that page's `UNAVAILABLE` effect with its generated reason, so siblings advance and the page
   keeps its last-good pointer; invariants stay fatal. The fan-out isolates each row: a taxonomy
   error other than a dependency pins that row with zero requests, and its task replays the same
   prefetch-then-bind pass. A `FAILED` roster task arms `bake_finalize` once every task is
   terminal, and the finalizer then abandons: it records `withheld_bake_contribution` unless a bake
   member is stored, binds `stale` when the cohort can bind (`bind_once` adopts a sibling's
   binding), settles `FAILED` `post_grant_invariant` and logs WARNING `finalize abandoned`. It does
   the same on its own non-dependency error on its final receive, except under a stored or adopted
   binding that authorizes serving (WARNING `finalize abandon_refused`, retried). An abandon before
   precompute armed is the U3 shape: the Release settles precompute `NO_ELIGIBLE` and completes the
   run withheld, deleting each `FAILED` task's `BAKE_RECOVERY#`. Precompute settles
   `post_grant_invariant` on any error on its final receive other than a dependency error, a guard
   or a transaction conflict, which keep retrying.

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a wedged domain stops
every page from rebuilding, and the fix fails loud on any shape it does not settle.

## Enforced by
- `tests/unit/test_execution_graph_release.py`: the rule's two settled shapes, every wait and every
  refusal reason (D1); one Release pass settles, binds and completes U1, with zero-write controls
  (R1); the U2 withheld signal, at least once (R2); a B8 image fails loud every pass with zero writes
  (R3).
- `tests/unit/test_execution_graph_release_store.py`: the six-member transaction, its replay, each
  moved condition and the mixed image (S1).
- `tests/unit/test_graph_bake.py` and `tests/unit/test_graph_precompute.py`: cohort eligibility,
  member reads on each grant, the unverifiable member, and Z3 with the digest-free final conditions
  (C1, S1).
- `tests/unit/test_graph_whole_chain.py`: the live replay and its control (W1), the old-Baker
  mixed-version cohort (W2), cohort eligibility end to end (W3), and Z3 for an old precompute and a
  precompute invariant (W4).
- `tests/unit/test_delivery_cohort.py`: ADR-139's predicates unchanged for U1 and U2 (D2).
- `tests/unit/test_stuck_flow_detector.py`: the live image counts, both windows from the constants,
  and an unread or grantless `COMPLETE#` yields no value (G1-G3).
- `infra/test/ingestion-stack.test.ts`: both windows derive from `BAKE_TASKS` and appear in the
  synthesized descriptions (I1).
- Decision 6: `tests/unit/test_graph_whole_chain.py` (a refusal per contract roster row and phase,
  H1; a genuine task failure ends terminal and withheld, H3), `tests/unit/test_graph_bake.py` (the
  refused pin's replay, H2; the finalizer's abandon and its refusal under an authorizing binding,
  H5), `tests/unit/test_graph_precompute.py` (H4), `tests/unit/test_execution_graph_release.py`
  (U3 and `bind_once`, H6), `tests/unit/test_execution_graph_release_store.py` (H7) and
  `tests/unit/test_execution_graph_dispatch.py` (the arm rule, H9).

## Consequences
- During one deploy-all the new Release runs before the new Baker (`IngestionStack` deploys before
  `PublisherStack`). Old U1 and U2 items are byte-identical, so they settle on the next Release
  pass. Until `PublisherStack` deploys, an old Baker still decides eligibility from the winner alone.
  A curated loser behind a zero-curation winner then settles U1 and its curation reaches pages
  through a later cohort. That window is silent by decision: no backfill and no signal.
- Between `PublisherStack` and `InsightPrecomputeStack` a cohort-eligible, zero-curation winner gets
  an old precompute's digest-free `NO_ELIGIBLE`. The finalizer then settles it withheld (Z3): pages
  stay on last-good, `GraphServingWithheld` pages, and the next cohort clears it.
- The detector reads one bake `GetItem` per unstamped `COMPLETE#` and a second only for a U1 or U2
  winner. Its existing `COHORT#*` grant covers both. No IAM, route, authorizer or storage change.
- A roster task that is `FAILED` no longer blocks finalization (decision 6): the cohort settles
  withheld, pages stay on last-good and `GraphServingWithheld` pages. A precompute-bound
  authorizing binding still makes the finalizer retry and dead-letter (`BakeDeadLettered`), since
  settling it `FAILED` would be silent; that is pre-existing and stays a follow-up.

## Alternatives considered
- **Exclude zero-publication runs from serving candidacy** (Option 2). Kept in reserve, for use only
  if correctness required it. It does not cover U2, and a wedged cohort elected before the deploy
  would stay wedged.
- **Keep winner-only eligibility.** Rejected by (1): the Release would then bind "nothing to bake"
  over a loser's curation, and pages would go stale with no signal.

## Review trigger
A second release domain, or a bake shape that settles without a finalizer and names a revision.
