# ADR-074: Define authority-backed data-flow completion and service-level objectives

Date: 2026-09-19
Status: Accepted — authority-backed semantics approved; development observation-only rollout authorized; evaluated SLO activation remains gated

> **Proposed extension, 2026-10-07.** [ADR-095](adr-095-expect-producer-deliveries-on-a-declared-schedule.md) (Proposed) would add an expected-delivery slot per producer dataset, source scope and period: a precursor clock before point 2's root, which never gates readiness. Point 2 is refined, not reversed. This decision stays in force, unchanged, until ADR-095 is accepted.

## Context

The platform has strong local authorities but no end-to-end definition of completion. A manifest proves
immutable landing; attempts and fenced `CURRENT` prove semantic publication; artifact pointers prove
servable deterministic revisions; insight-cache records prove model-slot outcomes; and route responses
prove synchronous delivery. Queues, Lambda results, logs, traces, and the proposed status projection
observe those authorities but cannot replace them.

The 2026-09-19 development investigation exposed the ambiguity. An isolated file reached semantic
`CURRENT` while every downstream preset attempt was unavailable because the page's complete source
cohort did not exist. Last-good pages remained usable. Queue drain, semantic completion, artifact
freshness, optional AI settlement, and serving continuity were therefore different facts, but no common
contract named their clocks, required children, SLO denominator, or evidence.

Existing route budgets are page-specific and remain useful. Source freshness values remain in source
contracts. Infrastructure timeout, retention, concurrency, and retry values remain operational ceilings.
None defines how one logical data flow is evaluated across retries, dependency waits, supersession, and
last-good serving.

## Decision

**Adopt [Data-Flow Completion and Service Levels](../concepts/data-flow-completion-and-service-levels.md)
as the single reusable contract for authority-backed completion, clocks, child obligations, target-free
baseline measurements, SLI arithmetic, and immutable SLO results. Concrete per-surface objective values
belong to the governed flow registry.**

1. **Lifecycle, completion outcome, SLO result, and serving continuity are separate.** A flow can
   complete after missing its SLO; a failed new generation can coexist with healthy last-good serving;
   and unknown evidence can never become attainment.
2. **The common data-to-serving root begins at repository authority.** Its clock starts when the exact
   canonical manifest version is first observed. Producer registration has its own precursor objective.
   External extraction is excluded unless a source contract supplies an accepted immutable timestamp.
3. **Clocks never reset or pause.** Queueing, retry, throttling, dependency wait, and owner-controlled
   delay remain part of elapsed time. Retry creates execution lineage under the same logical flow.
4. **Completion is authority-backed.** The owner re-attests its durable terminal and every immutable
   required child. Counts, submission, heartbeat, process exit, message deletion, provider acceptance,
   and status projection are insufficient.
5. **Required children need successful outcomes.** `completed_refused`, `exhausted`, and failed or
   blocked required children do not permit parent success. Optional children remain visible but do not
   gate deterministic completion.
6. **Page freshness is cohort-based.** One generated page/window dependency contract owns every metric
   and non-visual artifact input. The operations registry references its version. A page generation and
   customer-freshness clock start on the first applied relevant semantic `CURRENT` transition; a
   separate cohort-processing clock starts when the last required child is ready.
7. **Deterministic artifact completion ends at pointer authority.** Required body, custody, and sibling
   evidence is written and attested before the forward-only pointer advances. Last-good serving satisfies
   continuity, not freshness of the new flow.
8. **AI enhancement is optional by default.** Filled and verified-silent slots are successful AI
   outcomes. Unavailable is terminal and observable but does not fail the deterministic page. A future
   page may promote a slot only through an explicit contract change.
9. **Baseline observation precedes objective activation.** An `observation_only` registry surface may
   collect authority-backed lifecycle, clocks, child outcomes, evidence, descriptive latency
   distributions, and raw usage through a versioned measurement contract with no target. It emits no
   objective sample, SLO-window result, error budget, burn rate, threshold alarm, or healthy/unhealthy
   claim. Baseline history is evidence for choosing targets and is never retroactively scored.
10. **SLO evaluation is immutable after activation.** An `evaluated` surface references an accepted
    measurement contract plus a versioned objective. Results are `met`, `missed`, `unknown`, or
    `not_applicable` and name the exact objective version. Recovery may improve lifecycle but cannot
    retroactively turn a miss into attainment.
11. **Eligibility does not hide waste.** Exact duplicate delivery reuses one measurement sample and,
    when evaluated, one objective sample. Equal-revision
    rule reprocessing is a new predecessor-linked eligible flow. Strictly older skips may satisfy a
    decision-latency objective but never freshness. Timely supersession before publication is
    `not_applicable`; a flow already late remains missed. Retry, refused, no-op, and superseded usage is
    reported separately from successful-revision efficiency.
12. **Concrete measurement and objective definitions are generated, not copied into prose.** The future
    `contracts/operations/data-flows.yaml` registry is the sole owner of target-free measurement
    references, clocks, measures/units, child policies, plus objective references, targets, evaluation
    windows, sample floors, miss/unknown fractions, burn windows, and freshness/cost policy references. The future generated page dependency contract under `presentation/pages/v2/` owns full
    cohort rosters; the operations registry references its version. Source contracts and page designs
    retain source facts and explanatory context, not duplicate objective values.
13. **Only evaluated activation remains gated.** Development may implement registered
    `observation_only` surfaces after this decision and ADR-073 acceptance. Switching any surface to
    `evaluated`, emitting attainment results, or enabling target-based alarms remains prohibited until
    that surface's objective values, owner, and required dependency contract are accepted. Baseline
    implementation may be incremental, but unimplemented surfaces remain explicitly incomplete and the
    platform cannot claim complete status coverage.

## Consequences

Operators and product owners can distinguish “data decided,” “cohort waiting,” “artifact fresh,” “AI
settled,” “last-good page available,” and “objective attained.” Retry and dependency wait stop
artificially improving reported latency. Unknown evidence becomes a visible quality problem rather than
a silent exclusion from the denominator.

Every movement owner must persist or deterministically derive flow identity, measurement version, clock
origin, complete child policy, terminal authority reference, and usage contributions. An owner persists
an objective reference/version only when its root binding is `evaluated`; observation-only bindings
forbid it. The status plane
must reconcile these facts, but remains a derived read model. Complete page dependency rosters become
first-class reviewed contracts; current page declarations are insufficient for non-visual dependencies.

Observation-only rollout adds implementation work before target selection: persist exact measurement
identity and clock origins, collect authority-backed terminals and usage, expose descriptive baseline
windows, and keep incomplete surfaces explicit. It deliberately creates no SLO result. Product owners
then choose objective values, reconcile publisher budgets with customer needs, declare Campaign and
Retail Health full cohorts, and decide cost attribution before activating evaluation. This sequence
measures current behavior without converting it into a promise or producing precise-looking but
non-comparable SLOs.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| One generic pipeline `completed` state | Collapses semantic completion, cohort readiness, artifact freshness, AI enhancement, and serving continuity. |
| Start the clock when a worker begins | Hides queueing, retries, and dependency wait—the time the customer experiences. |
| Reset the clock on retry or redrive | Rewards instability and makes the same logical obligation appear faster after failure. |
| Treat last-good serving as fresh completion | Reports availability while concealing that the changed source generation never published. |
| Require AI before publishing deterministic artifacts | Makes an optional nondeterministic dependency control page freshness and availability. |
| Let every capability define its own denominator | Produces incomparable attainment by silently excluding different failures, unknowns, and superseded work. |
| Store targets in design prose | Values drift without a generated contract or checker and cannot drive projection, alarms, or acceptance. |
| Derive full page cohorts only from visual metrics | Omits hierarchy, control-total, custody, comparison, and render dependencies required by the actual materializers. |
| Wait for numeric thresholds before collecting authority-backed measurements | Prevents evidence-based target selection. Observation-only measurement is allowed, but it cannot emit SLO judgments or alarms. |

## Review trigger

Revisit when a new source responsibility boundary, page/window generation model, required AI product
obligation, supersession policy, cost allocation method, or external extraction timestamp contract is
proposed, or when a measured flow shows that the objective population/clock no longer represents the
customer experience.
