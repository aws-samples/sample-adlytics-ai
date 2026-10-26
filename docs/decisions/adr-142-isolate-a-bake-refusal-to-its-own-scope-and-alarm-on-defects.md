# ADR-142: Isolate a bake refusal to its own scope, and alarm on defects
Date: 2026-10-18
Status: Proposed (2026-10-18). The owner approved the direction on 2026-10-18 (UTC): scale failures
become that page's own designed absence, pages still publish together, and true code defects still
hold every page. Every time budget and query count becomes a defect alarm derived from a query
shape that doesn't grow with the seller. Its two alarm-only `infra/` additions need their own
approval. When accepted, it amends ADR-139 and ADR-140 (markers added there).

> **Amendment, 2026-10-25.** [ADR-146](adr-146-publish-the-reach-quarter-or-retire-it-on-measured-partitions.md) took branch (a′), the chunked two-pass snapshot, so the Reach-quarter isolated refusal it would have needed is gone from decision 1 and from the closed table.
> **Proposed amendment, 2026-10-25.** [ADR-152](adr-152-read-page-sized-answers-in-sql-with-no-arbitrary-row-ceiling.md) (Proposed) would make a cut read on Retail health, Scorecard, Reach, Retention and New customers a defect instead of `rows_over_ceiling`, because their reads are bounded by their meaning or their governed axes; Audiences keeps it until its reads stream. Every `limit` reason's `isolated_refusal` line would name its operator action. Implemented 2026-10-26 by the scale-limits change (PR #20) and live-validated on dev: where it differs from this decision, the amendment describes the code. ADR-152's acceptance is the owner's review.
## Context
A source's refusal is already one page's designed absence (ADR-140 decision 6). But several ways
data scale or a passing outage reaches the cohort are fatal, so one page's problem stales every
page:

- a task over its certified query count;
- the body ceiling (D18);
- a query abandoned three times;
- a dependency error that outlasts a task's three receives;
- one transient throttle during the pin pass: the pin pass re-raises, and the redelivery finds the
  selection already begun and fails the whole cohort.

On dev, Audiences' over-count failure withheld the 2026-10-17 cohort (run `931a7065`).

A refusal also leaves little trace: one INFO line, with no metric and no failed span. A pin refusal
drops the figure that explains it, so the Reach quarter's partition count was never logged. Time
budgets don't alarm at runtime, and one of them sized a population: the Audiences campaign cap
(ADR-141).

**Two bake entries.** The execution-graph authority can be provisioned only in the project's `dev`
environment: synth refuses it anywhere else. Without it the baker composes no graph runtime, and
every bake runs the legacy entry, which renders one page per queue record and publishes that page's
legacy pointer when it finishes. So every adopter's deployment bakes through the legacy entry, with
no cohort, while dev bakes through the graph entry. The graph-only alarm set is built only with
the graph authority too. A fix designed only into the graph task would never reach an adopter.

## Decision
1. **Two classes, one table.** Every bake limit or failure is either an isolated refusal or a
   defect. A closed table in `src/domain/operations/bake_refusals.py` is the one home for which
   reason is which, and for each reason's allowed log context.
   - **Isolated refusals:** rows over a bake ceiling; a body over its cap before staging; bodies
     over the invocation's write bound, checked before staging and again at runtime by the
     stage's write deadline; a selection over an exact-partition bound; a
     statement reaching its invocation deadline; a population over its memory-sized guard; a
     dependency still failing after the pin's bounded retry or on the final receive; any other
     source refusal; a history gap; an incomplete answer.
   - **Defects:** everything ADR-139 and ADR-140 already make fatal, plus a killed invocation (out
     of memory or the hard timeout), plus, on the graph entry, a release-store read or write
     outside a task's effect that still fails: the task's claim, run, node and head reads, its pin
     read and its settlement, and the pin pass's selection mark, head read and pin write after
     their bounded retry. With the release store failing no node can settle, so there's no page to
     isolate.
2. **The smallest owning scope.** A refusal lands on the smallest grain its failed read alone feeds:
   one item (an Audiences campaign or profile path), a section that declares itself non-anchor, or
   the page.
   - A section that doesn't declare `period_anchor: false` counts as anchor.
   - An item or section renders its typed absence inside a body that still publishes. An anchor
     section with at least one rendered item still publishes.
   - A page settles the designed absence ADR-140 decision 6 defines on the graph entry, and writes
     nothing on the legacy entry; either way it keeps its last good version.
   - On the graph entry the cohort binds unless a defect occurred, so pages still publish together.
     The legacy entry has no cohort: each page publishes when its bake finishes, and a defect holds
     only its own page, as before this decision.
3. **Defects keep holding everything the protocol holds.** Over-count stays fatal, on both entries.
   Every row's query count is certified from a shape with no seller term, so exceeding it is a
   code defect by construction. Writes are certified at each row's body bound, and memory at each
   row's held widths.
4. **The pin pass doesn't fail the cohort on a passing dependency error.** It retries a row's
   dependency error a bounded number of times within the invocation. Still failing, the row is
   pinned with a typed refusal and no requests, and its task settles that page's absence. Its
   selection mark, head read and pin write get the same bounded retry; each is safe to repeat.
   The refusal is an optional field of the pinned slice, absent for every slice without one, so
   a cohort pinned before this change re-hashes unchanged.
5. **Nothing fails silently, and each refusal reports once per settled outcome.** One emitter, which
   both entries call, emits per `(scope, reason)`:
   - one `bake.isolated_refusal` span marked failed;
   - one bounded `report_operation` WARNING line carrying only the entry, its lineage (run and
     task, or the invocation), page, variant, scope, class, count, whether it's a replay, and the
     reason's allowlisted context: no campaign key, segment ID, name or other customer value;
   - one datapoint, alarmed for the limit and dependency classes and counted for the data class,
     dimensioned only on the protocol and authority scope.

   The allowlist is closed: a key outside it raises at construction, never drops silently, and a
   static check of every construction site enforces it before release.

   When it emits:
   - **Graph entry:** on the receive whose own settlement wrote the node `SUCCEEDED`; never on a
     retryable receive, a `FAILED` settlement or a lost claim. The groups are stored with the
     settlement, so a settlement that lands but whose response is lost isn't lost: the redelivery
     that finds the node settled replays the stored groups once, marked as a replay. Delivery is
     at least once; a duplicate can't change an alarm whose threshold is one.
   - **Legacy entry:** on the receive whose outcome is final (published, unchanged or the page's
     absence); a receive that raises emits nothing and its redelivery renders again.

   The pin pass only logs, with the same allowlist, including a partition refusal's counts.
6. **Budgets are alarms, never cuts.** A page's bake over its `budget_seconds` runs to completion
   and emits `BakeTaskOverBudget` and one WARNING line, on either entry. No budget sizes a
   population, a row ceiling or a binder.
7. **One page execution for both entries.** The statement deadline, the stage's write deadline,
   the certified query count, classification and emission run in one function both entries call.
   The entries differ only in their trigger, their pointer protocol, and the graph-only pin and
   settlement. The two new alarms live with the bake queue's existing dead-letter alarm, which
   every deployment has.
   - Not every declared page is a bake-roster row: some declared pages have no bake source at all.
     On the legacy entry such a page keeps today's behaviour. It runs no statement, so it takes no
     time window and enters no page execution. It is reported unavailable and acknowledged, and it
     is neither a refusal nor a defect. The legacy entry checks roster membership before it looks
     up a row, so such a page can never raise the roster's invariant and dead-letter.
   - Each page execution reports which process ran it and that process's bake ordinal, beside its
     memory peak and the process's memory on entry. A warm process's peak covers every page it
     baked, so the live memory check compares a page's growth over its process's baseline.

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): one page's size or
one passing outage must not stale the others, and a defect must still stop what it protects and
page someone.

## Will be enforced by
- `tests/unit/test_bake_refusals.py`: the closed table; an unknown context key raising at
  construction, paired with an allowed key; each error classification; a section claim on an
  anchor or undeclared section refused; a profile-path item accepted; a limit reason going to the
  alarmed metric.
- `tests/unit/test_bake_refusal_sites.py`: a static scan failing any construction site whose keys
  aren't allowlisted, with a mis-keyed control; each source's refusals constructed with the keys it
  really passes.
- `tests/unit/test_page_bake.py`: the deadline set and reset; the certified count enforced;
  classification to the page's absence; the final-receive rule; the stage's refusal caught.
- `tests/unit/test_graph_bake.py`:
  - page, item and plan-pass refusals;
  - exactly one span, line and datapoint per refusal across pin and task, and for every
    `UNAVAILABLE` settlement, with no identifiers;
  - no emission on a retryable receive that collected item refusals, then exactly one on the
    receive that settles; none for a task that settles `FAILED`;
  - a settlement that lands and then raises emits nothing on that receive, and its redelivery
    replays the stored groups once, paired with a settled node that stored none;
  - the stage's runtime write deadline settling the page's absence without writing its parent;
  - a deadline timeout settling on its own receive, paired with another wait policy that retries;
  - a final-receive dependency error settling the page, paired with an earlier receive;
  - over-budget emitting without changing the effect;
  - over-count staying fatal.
- `tests/unit/test_baker_legacy_entry.py`: on the legacy entry, a page refusal writes nothing and
  emits once; item refusals publish and emit; the stage's write deadline moves no pointer; a
  final-receive dependency error is the page's absence; over-count is a defect; a declared page
  with no bake source is acknowledged unavailable with no window and no statement, beside a roster
  page in the same event that bakes; every declared page is either a roster row or has no bake
  source, with a failing control; the process's bake ordinal advances across invocations and not
  for a page with no source.
- `tests/unit/test_graph_bake_pin.py`: a throttle retried in place; exhausted attempts pinning a
  typed refusal while the cohort proceeds; the selection mark, head read and pin write each
  retried in place; a pin stored before the change decoding and re-hashing unchanged.
- `tests/unit/test_graph_whole_chain.py`: a limit refusal on one row binds the cohort not withheld,
  and an invariant on the same row withholds.
- `infra/test/publisher-stacks.test.ts`: both alarms, present with and without the graph authority.

## Consequences
- One page's scale problem, history gap or passing outage no longer stales the others. A user sees
  a precise absence where it occurs and current data elsewhere, on every deployment.
- A page-scope refusal keeps that page on its last-good body, as any designed absence does
  (ADR-139). The item and section scopes avoid that for the cases they cover.
- Operators get one alarm per limit or dependency hit, with the figure that decided it, on every
  deployment. A real seller beyond the certified envelope trips the over-budget alarm without any
  user-visible cut.
- A sustained outage shows as alarmed page absences instead of a withheld cohort or a dead letter.
  Users see the same last-good pages either way.
- On the graph entry, release-store reads and writes outside a task's effect (claim, run, node and
  head reads, the pin, the selection mark and settlement) stay cohort-wide. Each still alarms, as a
  dead letter or a failed task, and users see every page on its last good version, as today.
- Outside dev, pages keep publishing one at a time after a landing, as they do today. Publishing
  them together everywhere would need the graph authority to be deployable everywhere, which is a
  separate decision.

## Alternatives considered
- **Finalize per page** on the graph entry (each page binds and advances on its own). Deferred: the
  owner chose pages publishing together. It changes cross-page consistency and the release
  protocol.
- **Run bakes only in the graph entry and delete the legacy render.** Rejected: the graph authority
  can be provisioned only in the project's dev environment, so every other deployment would bake no
  page at all.
- **Keep every final-receive dependency failure fatal.** Rejected by (1): an outage isn't a code
  defect, and the owner's rule holds everything only for defects. Treating it as a refusal hides
  nothing, because it alarms on the staffed topic.
- **Emit at the pin and the task.** Rejected: one refusal would count twice.
- **Emit at most once on the graph entry.** Rejected: a settlement can land and still raise, and
  the redelivery would then report nothing, an unalarmed absence.
- **Drop an unknown context key.** Rejected: it would lose the figure that decided the refusal.
- **Log refusals and add no metric.** Rejected: a log line alone has no alarm, so a limit hit at a
  real seller would go unnoticed.

## Review trigger
A need to publish pages from different cohorts. A refusal class that can't name its scope. A
second page with items. Or the graph authority becoming deployable outside dev, which retires the
legacy entry.
