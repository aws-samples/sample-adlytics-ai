# ADR-075: Generate page artifacts once per dependency cohort

Date: 2026-09-19
Status: Accepted 2026-09-19 — Stage 0 observation baseline dev-accepted 2026-09-19; Stage 1 write discipline (§6) implemented and dev-accepted 2026-09-21; Stages 2–5 (§1–5, §7–8) pending

> **Withdrawn amendment, 2026-10-10, withdrawn 2026-10-11 — a bounded in-process wait for a
> busy partition lease (TD-61, FEAT-010).** The 2026-10-10 proposal let the GRAPH
> `materialize_raw` delivery wait in Lambda for a busy canonical-partition lease
> (`SIDECAR_BUSY`) before refusing, which would have accepted, for a bounded case, the "spin in
> Lambda" downside the alternatives table rejects. The wait was built and then removed before
> owner review, so this ADR's decision and its alternatives table stand unchanged. It was
> removed for three reasons. First, it covered only a delivery that publishes one publication
> group, with the busy member the first that group meets, so a later member or any multi-group
> delivery got nothing from it. Those multi-bucket and multi-dataset deliveries are the ones
> most exposed to cross-window contention on a shared partition. Second, it was the only new
> availability risk in FEAT-010: two final-receive waiters could hold both of the delivery
> route's slots for up to about 806 s. Third, TD-61 asked only for a short, bounded visibility.
> What ships is that bounded `ChangeMessageVisibility` override alone, owned by
> `src/adapters/inbound/ingestion_handlers/graph_materialize.py`. A `SIDECAR_BUSY` refusal
> redelivers after 220 s and then 440 s, capped at the queue's 960 s, and every receive still
> counts. On the final receive, a delivery that still meets a live holder settles `FAILED`
> (`dependency_exhausted`), as it did before FEAT-010. That failure is loud through the
> `ingest_delivery` errors alarm, and an operator can re-run it through the contract's
> `RETRY_IDENTITY` row. It is carried as residual TD-90 in the tech-debt register.
>
> **Amendment, 2026-09-25 — status reconciled with the shipped implementation.** The decision ships
> in six stages. Each stage retires the path it replaces in the same change, so old and new
> scheduling paths are never both operational (§9).
>
> | Stage | Scope | State (2026-09-25) |
> |---|---|---|
> | 0 | Observation-only baseline for `semantic_candidate_publication`, `preset_enqueue`, `preset_bake`, and `insight_precompute` (§9) | Accepted on dev 2026-09-19 ([validation results](../../validation/RESULTS.md), Stage 0 observation baseline entry) |
> | 1 | Ingestion write discipline (§6): authority-first publication, table-scoped FIFO write units, per-table lease and provider-attempt fence, merge consolidation, one verification per unit, servable projection pointer ([write discipline](../capabilities/load-path/write-discipline.md)) | Implemented (`infra/lib/stacks/ingestion-stage1.ts`, `infra/lib/stacks/operational-status-stage1.ts`, `infra/test/adr-075-foundations.test.ts`); live-accepted on dev 2026-09-21 for single-day deliveries ([validation results](../../validation/RESULTS.md), ADR-075 Stage 1 write discipline accepted on dev entry); wide-window acceptance closed and the queued writer topology switched on in dev 2026-09-22 UTC ([validation results](../../validation/RESULTS.md), Stage 1 queued writer topology switched on and wide-window acceptance closed entry). The servable-pointer slice is recorded in the Stage 0 validation entry |
> | 2 | Dependency router, generation coordinator, and dispatch queue with the existing baker as transitional consumer (§1–3, §7–8; [generation coordination](../ui/generation-coordination.md)) | Designed, not implemented |
> | 3 | Generation materialization store consumed by the renderer, ordered segments, and operational measurement, with declared preset reuse (§4–5; [generation materialization](../ui/generation-materialization.md)) | Designed, not implemented |
> | 4 | Insight generation reads materialization findings; Campaign Insight Precompute retired (§4) | Designed, not implemented |
> | 5 | Serving receipts record the selected pointer revision (§8) | No low-level design yet; not implemented |
>
> Until Stage 2 ships, the global declaration fan-out (`infra/lib/stacks/publisher-stack.ts`) and
> the Insight Precompute worker (`infra/lib/stacks/insight-precompute-stack.ts`) remain deployed.

> **Amendment, 2026-09-20 — Stage 1 deployment-gating and telemetry decisions closed.** These
> close the remaining decisions an implementer would otherwise have had to invent. Mechanics live
> only in [Load Path — write discipline](../capabilities/load-path/write-discipline.md) and
> [Operational Data-Flow Status](../capabilities/operational-data-flow-status/low-level-design.md).
>
> - `PlanIntentReconciler` is the sole post-persistence actor for queue send, admission, and
>   admission failure; the phase-1 delivery role only submits intents. Intent states are exactly
>   pending/claimed/admitted. A non-retryable reconciliation condition fails closed without
>   mutation. Intent scheduling fields and their per-origin seeds are named.
> - Byte-exact constructors are pinned for `plan_digest` and the source-delivery origin identity,
>   the `plan_attempt_budget` origin, `writer_execution_id`, the source-acknowledgment digest (over
>   the intent identity, proved outcome ordinal, and observed owner revision), and a versioned
>   per-family Stage-1 event `authority_digest` distinct from the whole-payload
>   `immutable_payload_digest`. The plan execution UUID input is the plan-materialization internal
>   execution identity. Operational Data-Flow Status owns the `STATUS_FLOW#`/`HEAD`/`EVENT#`/pointer
>   key constructors, the closed lifecycle-`HEAD` field set, and sequence/predecessor assignment.
> - One required typed boolean, `stage1RecoveryEnabled`, governs both reconciler schedules and both
>   producer triggers through a false-deploy → `verify-pre-switch` → true-redeploy contract; inline
>   parked-attempt recovery stays live in every later state.
> - One read-only operator module, `adapters.jobs.verify_stage1_preswitch`, under the dedicated
>   `Stage1PreswitchVerificationRole` (the sole named `Scan` exception), owns pre-switch all-page
>   verification, drain observation, and a `verify-edge` mode. Its forward-edge evidence includes a
>   statement-ledger proof that queued delivery composition ran zero Athena statements. Execution
>   evidence is exactly queue attributes, rendered function concurrency, and durable nonterminal
>   authority in the execution ledger. The module's ambient bootstrap resolves four named typed
>   parameter paths plus the publications-table parameter. The handoff provider keeps its narrow IAM
>   and validates only state-machine edges.
> - Operator and recovery surfaces are closed: the fence-recovery broker has versioned
>   `ProviderFenceRecoveryInvocation.v1`/`Result.v1` wire schemas with a closed error vocabulary;
>   the operator CLI is `adapters.jobs.requeue_provider_fence`; `semantic_unit_failed` has a closed
>   four-value reason vocabulary; plan-intent claim/outcome codes are closed; and
>   `sanitized_evidence` is one closed two-field form.
> - Telemetry and sizing: six span-free Stage-1 components are log-only through the shared reporting
>   seam; `USAGE#` outbox order uses the settled disposition's `observed_at`; semantic-write
>   queue/DLQ encryption, retention, and alarm plus the `Stage1BlockedOutboxCount` alarm are pinned;
>   table-writer, fence-recovery, handoff-provider, and outbox-recovery sizings are reviewed values.

> **Amendment, 2026-09-19 — Stage 1 write-discipline design refined before implementation.** These
> refine rather than reverse table-scoped serialization (§6). Mechanics and acceptance live only in
> [Load Path — write discipline](../capabilities/load-path/write-discipline.md),
> [Operational Data-Flow Status](../capabilities/operational-data-flow-status/low-level-design.md),
> and [Data-Flow Completion and Service Levels](../concepts/data-flow-completion-and-service-levels.md#stage-1-usage-attribution).
>
> - **Authority before transport; status only asynchronously.** Lifecycle event/head authority
>   commits atomically with plan persistence before any transport. Status is delivered only
>   asynchronously, through one `Stage1LifecycleProducer` for both lifecycle and usage authority,
>   over the table stream and a sparse pending-outbox index. A persisted-cursor recovery design was
>   replaced by that index, and no path scans the publications table.
> - **Closed, typed mutation boundaries.** Every cross-family lease/attempt/owner/lifecycle
>   mutation is one closed typed coordinator request with an exact principal/action/state matrix,
>   including inline-run persistence, refusal recording, and parked-submission recovery.
>   Planned-predecessor replacement is one compound lifecycle-head transaction. DynamoDB
>   transaction IAM is expressed through the underlying item actions and enclosing-operation
>   conditions. Runtime Lambdas receive direct builder-scoped grants; local operators and
>   credentialed integration assume one same-account least-privilege role resolved from typed SSM
>   configuration, with ambient verification clients split from the semantic session. Direct
>   attempt submission has its own least-privilege write statement, and the manual ingest operator
>   accepts only one explicit retained manifest key/version pair.
> - **Plan intent and generations.** One byte-closed intent authority binds the complete roster,
>   carried by one pre-claim FIFO identity. Pending intent identity and state are
>   generation-independent: the serialized owner/intent claim assigns the then-current next
>   generation, and admission failure returns the same intent for a fresh assignment. The roster is
>   proven feasible before any authority mutation through one shared deterministic pack/byte
>   contract, one canonical authority encoder, the existing repeated-grain rejection rule, and a
>   conservative DynamoDB item admission kept separate from wire serialization. An admitted plan
>   that exhausts its attempt budget hands off to one immutable successor identity with one
>   retained responsibility and no second recovery path; a concurrent retry-reschedule loser honors
>   the newer schedule and resumes only through scheduled recovery.
> - **Provider-attempt fence.** The fence has no timeout escape. Every `Begin` reservation counts
>   permanently; ambiguous acceptance after `Begin` fails closed while retaining the reservation,
>   and only a valid query execution identity plus provider-terminal evidence may progress.
>   Immutable provider core cannot be constructed from repairable discovery metadata, which alone
>   may block. Timing, refusal visibility, and the jointly proven workload envelope are
>   deterministic and replay-safe, and one typed per-environment ingestion configuration owns every
>   correctness-bearing bound.
> - **Usage and retention.** Stage-1 categorized usage is Athena-only and is recorded once,
>   atomically with final owner disposition; an unavailable disposition is final for that attempt,
>   with no late delta. Plan completion is metadata-only and never settles usage. Stage 1 activates
>   no TTL or sweeper; all of its authority is retained indefinitely.
> - **Poison and recovery.** Sparse outbox recovery closes durable poison as `blocked` after an
>   invariant/validation failure or a typed consecutive dependency-failure threshold, continues
>   later candidates, alarms, and projects aggregate evidence without leaking status accounting on
>   repeated poison. A separate least-privilege, service-side recovery boundary requeues the exact
>   retained authority through opaque case references and closed outcomes; case membership,
>   producer aggregate reads, and recovery bootstrap are each exactly authorized.
> - **Identity and topology.** Operational Data-Flow Status owns the versioned status-contract
>   tuples and UUID constructors (§8); each Stage-1 family has one fixed registry surface; queued
>   and inline trace identities are the persisted public status UUIDs; unit attestation telemetry
>   is separate from plan-terminal identity. A versioned constructive topology source distinguishes
>   proved roots from required joins. Only plan roots declare required queued-unit children: one
>   immutable required-unit history plus generation-addressable completion authority lets a plan
>   resolve its exact expected unit generation in either overtaking order, and a separate latest
>   selector describes only current visibility, never completion. Queued and inline pointer
>   dispositions are independent headless flows linked by same-transaction parent/causation
>   topology; they are never child outcomes and never enter child history, counts, measurements,
>   release, or migration.
> - **Writer handoff.** The inline→queued writer handoff is a typed, deploy-time CDK state sequence
>   (six states covering forward, drain, rollback, and emergency park) over one retained
>   compare-and-swap authority. A token-propagating three-resource CloudFormation callback protocol
>   keeps one committed identity across replay, a token-bound bootstrap rollback restores prior
>   absence, and stack deletion leaves truthful retained decommission history. This replaced an
>   imperative operator procedure.

## Context

The 2026-09-19 development investigation ([validation results](../../validation/RESULTS.md))
measured the current data-to-page path under no traffic. One small isolated delivery took tens of
seconds, and Athena-backed semantic materialization dominated the ingestion Lambda through repeated
select and merge statements. Every generated preset intent from that delivery terminated
`unavailable`, no serving pointer advanced, and a bounded drain at concurrency two produced another
Iceberg commit collision on a source-aligned table. Traffic was not the constraint; the topology was.

Repository evidence identifies four structural causes, all in code that works correctly today:

1. **Semantic ingestion serializes and duplicates Athena work.** The sidecar materializes each
   source-aligned projection, then each candidate projection, then each successful projection, one
   after another; a projection whose readback does not already match runs a read, one or more
   batched merges, and a verifying re-read, with Athena result reuse disabled. Every partition that
   maps to one physical Iceberg table — canonical tables per dataset, source-aligned tables per
   interpreter — merges into that table under a lease that protects a partition, not the table, so
   concurrent deliveries collide on the table commit.
2. **Publisher scheduling fans out globally.** Every canonical `CURRENT` transition enqueues the
   complete declared page/variant roster regardless of which dataset changed or whether the page's
   cohort can be satisfied. Only two pages have materializers; every other intent terminates
   `unavailable` after consuming a bake invocation. The bake message carries only `(page, variant)`,
   so transport deduplication cannot recognize a publication cohort.
3. **Bakes measure before they know whether anything changed.** A materializer resolves windows,
   browses the population, executes the full measurement batch, derives the revision, and only then
   compares it with the current pointer. A no-change revision has already paid the full cost.
4. **Campaign evidence is measured twice.** The artifact baker runs the portfolio batch; the
   separate Campaign Insight Precompute worker re-runs the portfolio batch plus per-campaign detail
   and delivery batches for every candidate preset. The two paths share code and Athena result
   reuse, but no persisted materialization.

[ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md) already requires
dependency-aware scheduling, and the [preset publisher](../ui/preset-artifact-publisher.md) lists
shared materialization as an unresolved control. Neither states an architecture. [ADR-074](adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
defines how completion and cost are measured; without this decision, it would measure an
inefficient topology precisely.

## Decision

**A source change produces exactly one deterministic materialization per affected page generation,
every consumer of that generation reads the materialization instead of remeasuring, and no work is
scheduled for a page that cannot yet be satisfied.**

1. **Route by dependency, not by declaration.** A publication transition is mapped through the
   generated page dependency contract to the affected pages and presets only. A page without a
   registered materializer, or whose dependency cohort names the changed dataset in no role,
   receives no work. Global declaration fan-out is retired in the same change that ships routing.
2. **Coalesce into generations.** Generation identity, open/frozen state, join, supersession, and
   predecessor linkage are owned by [Data-Flow Completion and Service Levels](../concepts/data-flow-completion-and-service-levels.md).
   This decision adds one rule: at most one materialization runs per generation, and a superseded
   generation's materialization is abandoned, not completed.
3. **Decide no-change before measuring.** The expected revision is derived from the cohort's exact
   publication identities and render-format version before any query runs. A generation whose
   expected revision equals the current pointer's revision terminates `completed_no_change` without
   measurement. Measurement is the last step, not the first.
4. **Materialize once, consume many.** An eligible generation executes the page's measurement
   batch once and persists an immutable, revision-bound generation materialization: exact source
   roster, resolved windows, deterministic metric results, findings and decompositions, and custody
   inputs. The artifact renderer, the ordered-segment renderer, optional insight generation, and the
   operational measurement all read that record. Insight generation consumes deterministic findings
   from it and never executes Athena. Campaign Insight Precompute as a separate remeasuring consumer
   is retired in the same change.
5. **Reuse evidence across presets only where declared.** A materialization may retrieve the widest
   required daily-grain evidence range once and derive narrower presets from it only for metrics the
   dictionary explicitly declares preset-reusable through a new field. The existing `time_class` and
   `aggregation` fields are not that declaration: ratio metrics carry `daily_additive` because their
   operands do, and must be recomputed from those operands per preset; exact-distinct metrics have no
   operands and are re-queried per exact window; suppressed cells and control-total reconciliations
   are never derived. A derived preset's revision is built from that variant's own exact publication
   identities, not from the wider retrieval roster. The materializer refuses to derive an undeclared
   metric.
6. **Consolidate within each table; parallelize only across tables.** Semantic ingestion combines
   compatible immutable-record merges for one physical table into the fewest statements the Load Path-
   owned `IngestionConfig` permits and verifies that table once. All mutations of a physical table are serialized; only
   independently scheduled table groups may use bounded concurrency. FIFO provides queued scheduling,
   while the table lease and durable nonterminal-provider-attempt fence prevent queued and inline
   writers from overlapping after cancellation or worker exit. That fence remains active until a
   provider terminal is observed; no lease expiry, provider/workgroup timeout, or quarantine duration
   can release it. A lease alone was rejected because it excludes without scheduling, and a
   single-writer function because it serializes unrelated tables. Raising a timeout or concurrency
   ceiling is not an accepted remedy. The current fence, feasibility, reconciliation, finalization, and
   rollout protocols are owned by the linked write-discipline design.
7. **Bound amplification.** One source change may cause at most one generation per affected
   page/variant and one materialization per generation. Accepted Stage-1 Athena attempts are recorded in
   ADR-074's usage categories only after final owner disposition. Lambda, queue, and storage performance
   remains the ADR-074 companion/CloudWatch before-and-after comparison, not a fabricated per-flow
   Stage-1 contribution. Retries, duplicate deliveries, no-change decisions, refusals, unavailable work,
   and supersession therefore remain visible through their actual accepted evidence rather than being
   averaged away.
8. **Preserve identity at each authority boundary.** Internal operation, plan, publication, attempt,
   and event-key digests remain owner selectors and are never public status identifiers. New owner
   interfaces normally allocate and atomically persist UUIDv4 identities. The explicitly named Stage-1
   `DynamoStage1LifecycleHeadStore` exception derives UUIDv5 flow, execution, and event identities only
   from status-contract-owned versioned tuples/namespace and persists them in the owner transaction;
   the status projector only recomputes and verifies them. Retries preserve the public flow while
   creating predecessor-linked public executions. Generation records carry those public joins from the
   semantic transition; pointer transactions record the generation, exact source-roster digest, advance
   instant, and typed outcome; serving receipts record the selected pointer revision. These are the
   joins ADR-073 and ADR-074 need without exposing internal selectors.
9. **Measure before and after with the same clocks.** The observation-only baseline authorized by
   ADR-074 records the current topology first for every surface this decision replaces — semantic
   attempts, preset enqueue, preset bake, and insight precompute — before any stage retires it; the
   same measurement contracts then record the replaced topology. Acceptance compares the two under
   identical clock, eligibility, and usage definitions. A row with no before-measurement is reported
   as uncompared, never as improved. The old and new scheduling paths are never both operational in
   one deployment.

## Consequences

Work per source change becomes proportional to the pages that actually depend on it, not to the
number of pages declared. Athena statements per delivery fall from a per-projection
read-merge-read cycle to consolidated merges with one verification. A Campaign preset costs one
measurement per generation instead of two independent measurement families. No-change revisions
cost a revision comparison instead of a full bake. Iceberg commit collisions become a design
violation rather than a load-dependent surprise.

The price is new coordination state: a generation record with dependency-revision tracking and
supersession, a persisted materialization store with revision-bound retention, dictionary metadata
declaring preset reusability, and table-scoped write ownership in ingestion. Stage-1 plans, units, runs,
attempts, lifecycle events/heads, Athena usage/evidence, and fences are retained indefinitely in this
stage with no TTL or sweeper; that includes delivered and poison-blocked outboxes and their immutable
payloads. Missing Athena quantities remain unavailable for that attempt rather than authorizing a late
completion path. Poison recovery adds a typed failure threshold, aggregate evidence/alarm visibility,
and a service-side typed recovery boundary whose caller sees only opaque case references and closed
outcomes; it prevents invalid authority from starving unrelated delivery without exposing protected
selectors.
Stage 1 deploys no retention mechanism, including for disposable exploratory plans. Retention
tuning is a future review trigger, not a Stage-1 acceptance condition. Other stages
retain their own explicit owner/removal policies. Baking gains one deterministic stage before
measurement, so the bakeability gate must budget the materialization step separately from rendering.

Lifecycle observability under ADR-073 becomes more valuable, not merely more precise: every stage
it records is now a stage the platform intends to keep. ADR-074's usage ledger becomes the
acceptance instrument for this decision rather than a report on waste the design tolerates.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Raise ingestion Lambda concurrency or timeout | Concurrency already reproduced Iceberg commit collisions; timeout headroom hides the serial statement pattern. |
| Keep global fan-out and let the baker refuse fast | Every refusal still costs an invocation, a window resolution, and a population browse; unavailable work is still work. |
| Filter enqueue by dataset only, without generations | Removes unrelated pages but still bakes once per transition, so a multi-source cohort bakes N times for one generation. |
| Let precompute persist artifacts as a side effect | Couples optional AI to deterministic artifact authority; a model failure would block page freshness. |
| Derive every preset from one wide window unconditionally | Silently changes ratio, distinct-count, and suppression semantics; reuse must be declared per metric. |
| Rely on Athena result reuse for cross-consumer sharing | Reuse hits only on byte-identical statements within a time window and is disabled on the ingestion path; it is a cache, not a materialization. |
| Table-level Iceberg locking through longer partition leases | A partition lease cannot express table ownership; conflicting writers would still race on commit. |
| Per-table lease as the sole serializer | A lease excludes but does not schedule: contenders fail the invocation or spin in Lambda, every retry re-parses the delivery, and lease duration becomes a correctness parameter that already caused one incident. |
| One ingestion writer function with reserved concurrency one | Serializes every table behind every other; throughput is one delivery at a time regardless of dataset count, and it cannot scale without changing mechanism. |
| Defer until SLO thresholds are chosen | Thresholds chosen against an inefficient topology would be renegotiated after every optimization. |

## Review trigger

Revisit when a page declares a dependency the generation router cannot express, a metric requires
a reuse rule the dictionary vocabulary lacks, a materialization store approaches its retention or
size bound, measured Stage-1 authority growth justifies a separately reviewed retention design, a
second ingestion writer per table becomes necessary, or before-and-after measurement shows the
replaced topology did not reduce statements, materializations, or amplification per source change.
A future retention review cannot retroactively satisfy this stage's acceptance or authorize deletion.
