# Campaign Portfolio — operational design

This document owns the page's cross-cutting qualities: latency, caching, authorization, data
protection, failure containment, observability, and cost. The [HLD](high-level-design.md) owns
topology; the [LLD](low-level-design.md) owns wire contracts and points here for every budget.

## 1. Latency and capacity

Initial p95 server-time engineering budgets, with current/target status stated explicitly. These are
measurement targets, not request cutoffs:

| Path | Budget | What meets it |
|---|---|---|
| `POST /api/presentation/campaigns` | 1 s cached · 8 s first generation | Baked-artifact serve path (ADR-070/071): pointer and parent/custody reads; canonical segment zero needs no sibling read, while any other segment reads exactly one public/private sibling pair; bounded capability minting and exact-revision insight-cache reads. On a cache miss, every Gate-1-admitted slot generates concurrently and the response joins all results. No request-time measurement or population reorder exists. |
| `POST /api/presentation/campaign-detail` — displayed cohort | 1 s cached target · 8 s first generation | Pending baked detail artifact target (ADR-070 detail-tiers amendment): pointer read, artifact fetch, capability minting, then parallel on-demand insight generation on a miss. Until it ships, displayed detail uses the live concurrent measurement path and does not claim the cached target. |
| `POST /api/presentation/campaign-detail` — tail (live tier) | 8 s measurement target; first-generation attainment measured separately | Current concurrent measurement batch (the serial batch is the recorded 2026-09-14 timeout defect), followed by parallel on-demand insight generation from the assembled artifact. Acceptance reports measurement and slot-generation durations separately; a combined target is not claimed until deployed samples establish it. |
| `POST /api/presentation/campaign-metric-focus` | 300 ms cached · 8 s first generation | Revision-keyed cache; one direct Converse call on a miss. |

- The measurement and insight fan-out bounds come from configuration and respect the downstream
  quotas (`.kiro/steering/backend-concurrent-execution.md`). Independent slots start concurrently,
  join by declared slot identity, and never wait on one another serially.
- There is **no application-enforced insight response deadline**. Every admitted slot runs to success,
  verified silence, or a classified failure; the page then returns all independently completed slots.
  This does not remove transport safety: Bedrock connect/read limits and the Lambda/API Gateway request
  envelope remain hard external ceilings. The direct 2026-09-22 dev diagnostic used the exact 14-day
  segment-zero artifact and measured two admitted calls launched together: exposure completed as
  verified silence in **1,226 ms**; contribution failed the narration invariant on duplicate cited
  figure strings in **3,242 ms**; parallel batch wall time was **3,417 ms**. Bedrock's supported
  structured-output subset does not accept array uniqueness or string constraints, so the adapter
  canonicalizes duplicate/blank citations after native exact-field/type validation and before domain
  verification. This is one observation, not p95 attainment.
- **Current bake envelope.** The Campaign materializer executes one grouped 26-plan measurement batch
  per variant at the declared estate bound; it does not issue one batch per displayed campaign. The
  older approximately 1,300-execution estimate is superseded by the grouped renderer now asserted by
  the bakeability gate. The deployed baker ceiling is five minutes in `PublisherStack`, not the former
  fifteen-minute prose value, and remains an execution limit rather than an SLO. Distinct variants may
  run concurrently, while ordered segments and the name roster add in-memory sorting and immutable
  writes rather than Athena plans. At the 1,000-campaign bound, 20 segments × 10 orderings produce 199
  non-parent public/private sibling pairs; with parent public/custody the variant contains 400 immutable
  bodies. Every body independently enforces the encoded-byte artifact cap, and the build gate checks
  plan count, body count, maximum body, and total bytes. ADR-074 keeps the concrete bake-completion
  target in the governed objective registry instead of treating timeout headroom as attainment.
- **Seven-sibling bake envelope (ADR-127, 2026-10-08).** Each filter writes 2 × 10 orderings ×
  its segments: All channels and Sponsored ads may each hold the whole 1,000-campaign population
  (20 segments) and the one-channel siblings partition it (at most 20 + 5 segments), so a variant
  writes at most 1,300 immutable objects. The 30-day batch is 34 plans, 5 waves of 8 at the 30 s
  budget, 150 s. `make check-bakeability` checks the page's estimate against the baker itself: the
  bodies held in memory against half of the 1,024 MB baker (318.5 MB at 245,000 bytes a body,
  against 536.9 MB), and measurement plus 100 ms per sequential object write against the 300 s
  timeout (150 s + 130 s). The 100 ms write is a budget, not a measurement. Since ADR-145
  (streamed writes) a variant also writes one index shard per 800 siblings (1,301 objects at the
  bound) and the all-bodies memory check is gone: the memory model's write term covers the pairs
  in flight ([publisher](../preset-artifact-publisher.md#bake-width-and-the-memory-model)). Local evidence
  (`accept_campaign_estate_local.py`, 2026-10-08) bakes 30d for 1,000 campaigns over all five
  channels: 7 filters, 64 segments, 1,280 objects (the gate's count for that split), 102.8 MB in
  total, the largest 228,495 bytes, render 3.3 s and a peak of 201 MB of traced Python allocation.
  No S3 write was timed; the deployed bake's duration is its live check.
- **Observed local bulk acceptance (2026-09-18):** the governed 1,000-campaign workflow
  completed in 31,827 ms, proved a bounded 26-plan grouped measurement batch at 1,000
  selections (the five daily series split into two window chunks under the 10,000-row query
  ceiling, rather than truncating or issuing per-campaign queries), produced 20 segments, 199 sibling
  pairs and 400 total public/private bodies, and walked every ordering, descriptor, digest and custody
  pair. The largest encoded body was 197,637 bytes; the maximum 50-row served form with bounded filled
  insights was 223,761 bytes, below the same 256,000-byte cap. This is local deterministic
  plan/render/attestation evidence, not deployed bake or route latency evidence. Re-measured
  2026-10-07 at render format 7, where every comparison carries its change block in place of
  `percentage_change` ([ADR-121](../../decisions/adr-121-declare-change-direction-and-sentiment-on-the-server.md)):
  largest body 225,773 bytes, maximum served form 251,529 bytes. Keeping the percentage beside
  the block measured 257,849 bytes, over the cap, which is why a comparison with a block drops it.
  Re-measured 2026-10-07 at render format 8, with the coverage block
  ([ADR-119](../../decisions/adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md)):
  largest body 225,796 bytes, maximum served form 253,383 bytes. Re-measured 2026-10-07 at
  render format 9, with a full-length `NextStep.v2` "Ask the assistant" step (240-character
  reason and question) on each model card
  ([ADR-122](../../decisions/adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)):
  largest body 225,796 bytes, maximum served form 255,129 bytes, 871 bytes under the cap.
- A change that cannot meet a budget splits the endpoint or precomputes; raising a Lambda timeout is
  never the first fix (`.kiro/steering/backend-concurrent-execution.md`). Slot generation is already
  parallel; the first-generation budget is re-evaluated from the per-slot timings introduced below.
- **Open: deployed detail latency is unattributed.** A dev hydration walk on 2026-10-02 recorded
  campaign-detail function durations of 13.2–19.8 s (median 16.5 s, 107 sequential requests), above
  both 8 s targets, without the measurement/generation split, cache state or tier per request
  ([validation results](../../../validation/RESULTS.md#security-pre-publication-fixes-on-dev--2026-10-02)).
  The next detail readout reports those three per request, so the figures can be judged against the
  table above.

## 2. Caching and precompute

- Complete insight cache identity: (analytical scope, subject, resolved window, artifact revision,
  prompt version, slot) — ADR-068. Exact content identity, not time, determines validity. A
  `superseded_at` value is asynchronous retention metadata, never evidence that an exact-key result is
  invalid; the exact artifact+prompt result stays readable until DynamoDB removes it. Successful
  generation never infers supersession from a different cache write: one page/window can have multiple
  simultaneously serveable ordered segments, each with its own artifact revision. Cleanup is driven
  only by the owning artifact lifecycle.
- Page artifacts are precomputed, not cached
  ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
  The publisher's artifact baker independently resolves and measures each Campaign preset, persists
  capability-free parent/custody/ordered-segment bytes, and advances one forward-only pointer per
  variant. The current publisher persists the portfolio parent, custody, and ordered segments only;
  displayed-cohort detail artifact persistence remains an unimplemented ADR-070 target, while the tail
  keeps its declared live tier. Bake-time evidence snapshots are taken per source pack with current and
  prior windows together.
- Optional prewarming may populate the same exact cache key before a viewer arrives, but it is not a
  correctness dependency. Serving always derives requests from the exact artifact being returned,
  reads that revision's cache entries, and generates every admitted miss directly. A warmer must never
  expire or invalidate a simultaneously serveable sibling revision. The separate remeasuring Insight
  Precompute worker remains scheduled for retirement by ADR-075; the target publisher materialization
  owns any future zero-measurement warm path. Until then its per-publication request bound
  (`INSIGHT_PRECOMPUTE_MAX_REQUESTS`) admits every preset's portfolio slots first, then whole
  complete campaign details in preset order and then campaign_key order, stopping at the first
  detail that does not fit. A detail left out is generated at request time like any cache miss, and
  one `admission` lifecycle event records the requested, admitted and skipped counts.
- Baked artifacts are capability-free; the handler mints owner/session-bound selection capabilities
  onto the served artifact at request time, so capability custody stays per-session while the artifact
  body is shared.
- Sort is baked, not served
  ([ADR-071](../../decisions/adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)):
  the baker sorts the full measured population once per declared sortable column and direction and
  stores ordered segments under the same revision; parent custody indexes every non-parent
  public/private digest pair. A sort or segment request is a pointer/parent read and exactly one
  sibling-pair fetch unless it selects canonical segment zero, never a reorder and never a measurement.
  Estate filtering is client-side over the parent roster.
- Athena result reuse stays enabled per the query adapter's existing configuration.

## 3. Authorization and custody boundary

- All three routes sit behind the existing custom request authorizer: the session cookie and CSRF
  header authenticate; the authorizer supplies owner identity. The browser never sends owner,
  canonical selectors, or window derivations (LLD exact-inputs tables).
- Campaign identity crosses the boundary only as an opaque capability bound to owner, session, expiry,
  parent artifact revision, and current catalog identity. The metric-focus route revalidates the same
  custody as detail and derives window and grain from the redeemed capability's parent — it adds no new
  authority surface.
- Bedrock invocation is a server-role permission of the presentation function and optional background
  recovery/warmers only; no model access is reachable from browser input beyond the declared slot id.

## 4. Data protection and retention

- Cached insights are customer-derived content: generated sentences citing campaign display names and
  figures. Declared purpose: rendering this page's insight slots. Access boundary: the same authorized
  deployment scope as the artifacts they derive from; the cache is never readable cross-scope and never
  leaves the account. Storage: managed encryption, on-demand billing, PITR.
- Cache entries are immutable by exact artifact+prompt revision. Supersession schedules retention
  cleanup but does not make an exact match malformed; a still-open page may truthfully request an older
  retained artifact revision. Current serving revisions and every simultaneously serveable ordered
  segment are never inferred obsolete from another segment's successful generation.
- Prompts themselves contain no customer content (registry rules, ADR-069); calibration cases are
  fictional by the evaluation-validity contract.

## 5. Failure containment and degraded states

| Failure | User-visible result |
|---|---|
| One measurement query fails | Typed insufficiency for the affected slots; the page renders every independently answerable element (per-claim unit rule). |
| One insight generation fails validation or dependency access | Other parallel slots complete independently; only the failed slot renders unavailable. A classified dependency failure may enter durable background recovery; provider request/schema validation and model-output invariant defects do not become blind retry loops. |
| Insight cache store unavailable | The page attempts no unversioned fallback. Independently available deterministic evidence renders; affected model slots render unavailable and the dependency failure is reported. |
| Prompt registry unreadable | Generation uses no guessed prompt; affected slots render unavailable and the dependency failure is reported. |
| Precompute lag or failure | No request-path correctness impact: the exact served artifact first-fills admitted misses on demand. |
| Bake failure | The last good artifact keeps serving and the failed FIFO delivery retries. A page whose variant has never baked returns `window_unavailable`; the current publisher has a DLQ but no implemented DLQ alarm. |
| Capability expiry / custody failure | Refresh-list behavior exactly as the LLD state contract already defines. |

## 6. Observability and alarms

- Every direct slot call emits one bounded structured terminal with slot id, prompt route/version,
  outcome (`filled`, `silent`, or classified failure), cache outcome, finding/context counts, provider
  duration, total slot duration, and start/end offsets relative to the batch start. Provider SDK
  failures use the shared AWS failure projection; no prompt, model output, raw artifact, customer
  identifier, or credential is logged. Batch terminals record admitted count, cache-hit count,
  generated count, failed count, configured concurrency, and parallel wall time, joined to the request
  correlation id.
- Acceptance reads those terminals and proves overlap directly: at least two generated slots' bounded
  `[start_offset_ms, end_offset_ms]` intervals intersect. As a secondary efficiency check, summed
  generated-slot durations must exceed batch wall time whenever overlap is claimed. Configuration or
  thread-pool construction alone is not concurrency evidence. Acceptance also verifies a repeated exact-
  revision request is a cache hit and invokes no model.
- Current structured evidence also includes ingestion delivery spans, precompute batch terminals,
  per-bake `source_ms`/`persist_ms`, and serving lifecycle duration. The repeatable aggregate view is
  `validation/probe_stage_latency.py`; it does not causally join one delivery to one served revision.
- The latency numbers above are page-owned engineering budgets. They are not SLO targets or attainment
  claims. The governed flow registry is the sole owner of accepted route SLO targets, percentile
  windows, sample floors, allowed miss/unknown fractions, and burn policy; an objective may reference a
  page budget as design context without transferring target ownership.

## 7. Cost envelope

- Serve-time generation is one direct call per Gate-1-admitted cache miss, bounded by the declared slot
  roster and configured concurrency. The exact-revision cache prevents repeat calls. Concurrent misses
  may still duplicate calls before the conditional write chooses one winner; acceptance measures this
  rather than assuming zero duplication.
- Optional prewarming and dependency-failure recovery may call the same idempotent generation path, but
  neither may invalidate another serveable revision. ADR-074 requires actual model calls/tokens per flow
  rather than converting roster bounds into an attainment claim.
- Athena and snapshot spend moves away from ordinary preset page views. Until ADR-075 retires the
  separate remeasuring precompute worker, its cost is reported separately from on-demand Bedrock cost.
