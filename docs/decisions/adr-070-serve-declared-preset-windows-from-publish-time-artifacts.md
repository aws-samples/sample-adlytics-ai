# ADR-070: Serve declared preset windows from publish-time artifacts

Date: 2026-09-15
Status: Accepted — parent artifact serving implemented in development; displayed-detail bake remains pending
> **Amendment, 2026-10-16.** [ADR-139](adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md): a GRAPH cohort moves `CURRENT` only under a binding that authorizes serving, and a page's designed absence serves its typed absence or last-bound body. Current serve semantics: [preset artifact publisher, "The serve path"](../ui/preset-artifact-publisher.md#the-serve-path).

> **Proposed amendments, 2026-10-07.** [ADR-116](adr-116-give-every-tab-one-time-model-and-one-window.md) (Proposed) would add `resolved_window` and `comparison_window` to the pointer, so drill links name the target's dates. [ADR-127](adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md) (Proposed) would bind render-only channel filter siblings under one pointer. This decision stays in force, unchanged, until they're accepted.
> **Proposed amendment, 2026-10-06.** [ADR-100](adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md) (Proposed) would add calendar presets that resolve to the latest measured period, and an as-of-vintage page time. This decision stays in force, unchanged, until ADR-100 is accepted.

> **Proposed amendment, 2026-10-06.** [ADR-100](adr-100-serve-periodic-metrics-only-for-complete-measured-periods.md) (Proposed) would add calendar presets that resolve to the latest measured period, and an as-of-vintage page time. This decision stays in force, unchanged, until ADR-100 is accepted.
> **Amendment, 2026-10-08 — `30d` returns to Campaigns at the verified 100-partition read.** This
> reverses the 2026-09-16 withdrawal for Campaigns only, under
> [ADR-127](adr-127-serve-the-campaigns-channel-filter-from-render-only-siblings-of-one-bake.md).
> The withdrawal rested on the old attestation, two items per partition against the 100-item
> transactional-read cap, so 2 × window-days ≤ 50. Each bake-time per-pack snapshot is now taken by
> `inspect_exact`, the verified A/B/C protocol of the 2026-09-22 amendment below, which reads one
> item per partition in each transaction up to `MAX_EXACT_CURRENT_PARTITIONS` (100). The bake policy
> `MAX_PRESET_SNAPSHOT_PARTITIONS` takes that bound
> [repo: `src/adapters/outbound/publication_selection.py`], so `30d` (60 day-partitions per pack)
> fits one verified read and no snapshot is sub-chunked; the largest bake-able preset is 50 days.
> The two-pass mechanism named in 2026-09-16 isn't needed. Retail health keeps `14d` as its largest
> preset; declaring `30d` there is its own decision. The bakeability gate holds the policy equal to
> the protocol bound [repo: `tests/unit/test_preset_bakeability.py`].

> **Amendment, 2026-09-22 — live campaign detail adopts a verified 100-partition servable
> snapshot.** A deployed browser reproduction selected the 14-day portfolio, clicked a newly minted
> campaign capability, and received `ADLY-PUBLICATION_SELECTION-0002`: the detail cohort requires 84
> canonical day partitions while the shared reader rejects above 50. The browser and hydration probe
> send the same route, exact body fields, and V2 media type; the earlier source-delivery chunking changed
> delivery size but did not change canonical day partition identity. The 14-day user path is therefore
> genuinely blocked, not a probe artifact.
>
> Publication State now owns the smallest consistency-preserving extension: A reads up to 100
> normalized servable `CURRENT` heads atomically; B atomically attests the immutable publications A
> named; C atomically re-reads all heads and the adapter returns only when normalized A=C. Forward-only
> fencing prevents ABA, and an authority advance that leaves the projected predecessor servable does
> not cause false refusal. Missing, `NONE`, legacy, and projected states are distinct normalized values;
> any change refuses before query execution. Publication State owns the closed decoder and
> monotonic/no-delete premises.
> AWS documents `TransactGetItems` as an ordered atomic read of up to 100 items with serializable
> isolation against writes ([API](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactGetItems.html),
> [isolation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html));
> monotonic A=C verification, not an assumption that three calls form one transaction, establishes the
> stable vector. This also replaces the serving adapter's prior per-partition loop, which did not provide
> a whole-roster instant even under the 50-partition bound.
>
> The generic serving-read capability and the artifact-bake product policy were decoupled so a
> transport extension cannot silently widen a page contract. Publication State owns the current closed
> decoder, monotonic/no-ABA proof, item/byte bounds, and typed refusal behavior; Generation Coordination
> owns the current artifact-bake policy. The previously rejected constant-only increase remains rejected:
> the bound is safe only with the verified protocol.

> **Amendment, 2026-09-19 — generation, not publication, is the bake unit.**
> [ADR-075](adr-075-generate-page-artifacts-once-per-dependency-cohort.md) refines decision point 1:
> "dependency-aware scheduling" becomes a dependency router and generation coordinator that bake one
> materialization per affected page generation, decide no-change before measuring, and retire global
> declaration fan-out. "Bake cost per publication is bounded by declared variants × pages" below
> describes the deployed development path, not the mandated one; the
> [preset publisher](../ui/preset-artifact-publisher.md) owns the target pipeline.

> **Amendment, 2026-09-19 — the artifact publisher, not Insight Precompute, persists preset artifacts.**
> The deployed publisher independently resolves, measures, renders, persists, and pointer-commits
> Campaign and Retail Health artifacts. Campaign Insight Precompute remains a separate stream consumer
> that warms model slots; it does not persist page artifacts, and the baker does not consume its cache.
> The shared [preset publisher](../ui/preset-artifact-publisher.md) owns current mechanics. The
> publish-time deterministic serving decision below remains; [ADR-074](adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
> owns completion clocks and optional AI obligations.

> **Amendment, 2026-09-15.** The first review trigger fired the same day: Retail health was
> mandated as the second artifact-serving page. The publisher and artifact store are therefore
> generalized from the start — mechanism owned by the shared
> [preset artifact publisher](../ui/preset-artifact-publisher.md) concept, consumed by both
> pages — rather than built campaign-specific and migrated later.

> **Amendment, 2026-09-16 — the per-pack snapshot bound caps a declared preset at 25 days;
> `30d` is withdrawn.** The bake-time per-pack snapshot (decision point 5) inherits the same
> transport arithmetic that retired live measurement: attestation reads two items per
> partition against DynamoDB's 100-item transactional-read cap, so one transaction attests at
> most 50 partitions. A per-pack snapshot attests both comparison windows — current plus the
> required equal prior — of a daily-grain pack in one transaction, so 2 × window-days ≤ 50:
> **the largest bake-able preset is 25 days.** `30d` needs 60 day-partitions per pack — 120
> items — and cannot be attested as one consistent per-pack snapshot. `30d` is therefore
> withdrawn from the campaigns and retail-health candidate presets; `14d` becomes the largest
> declared preset for artifact-served pages. The temporal control still returns the canonical
> four presets with `30d` unavailable, so no browser grammar changes. Reopening `30d` (or any
> window past 25 days) is a separate future decision with its mechanism named now: a
> **two-pass verified snapshot** exploiting the forward-only publication pointers — read the
> partitions in as many chunked transactions as needed, then re-read every `CURRENT` pointer;
> unchanged pointer identities across the two passes prove the chunks describe one consistent
> instant, because pointers are monotonic. Decision point 5's "sized well under the cap for
> every declared preset" holds only under this bound and is annotated in place.

> **Amendment, 2026-09-17 — campaign detail serves in two declared tiers at estate scale.**
> Decision point 1's "each complete campaign detail artifact" was reasoned at the portfolio
> row bound; at an ADR-071 estate population (~1,000 campaigns) baking every detail for every
> preset is ~26 measurements × population × variants per publish, spent mostly on campaigns
> nobody opens. Detail therefore serves in two tiers, both declared: the **displayed
> cohort** — the union of the first segment of every declared ordering per variant, known
> deterministically at bake time — gets its detail artifacts baked and its insight slots
> pre-seeded exactly as before; the **tail** serves detail through the existing
> capability-bound live measurement path on open, within the detail route's declared budget,
> with insight slots generating on first open into the revision-keyed cache (first-touch
> warming). The tail's live serve is a *declared path with its own budget and acceptance*,
> not a fallback from a missing artifact — decision point 4's no-fallback rule is about
> nondeterministic degradation of the preset page path and is untouched. A named future
> extension (build trigger: observed first-open latency on tail campaigns): re-warm
> recently-opened campaigns at publish from insight-cache telemetry. Acceptance and budgets
> are owned by the campaign operational design §1; the cohort rule and serve tiers by the
> campaign LLD.

> **Amendment, 2026-09-17 — the ranked-cohort and tail-reach review trigger fired; sort
> orderings are now baked ([ADR-071](adr-071-reach-the-full-campaign-estate-through-baked-ordered-segments.md)).**
> The enterprise estate requirement (full population discoverable, sortable, and pageable)
> reopened the trade this ADR's review trigger named. Decision point 6 is superseded in
> scope: the baker persists complete ordered row segments per declared sortable column and
> direction plus a compact name roster, and the handler serves segments — serve-time reorder
> is retired along with the two-regime sort split. The "bake all eight sort variants"
> rejection below was reasoned for populations that fit one artifact and does not survive
> populations that exceed it. Everything else in this ADR — per-pack snapshot arithmetic,
> preset bounds, pointer mechanics, the one-serving-path rule — is unchanged; ADR-071 owns
> the segment and roster decision.

> **Amendment, 2026-09-16 — one signature derivation, and the revision names its render
> format.** Live acceptance of the serve path caught a split derivation: the baker rendered
> public rows and custody signatures from raw canonical dimension keys, while capability
> redemption re-derives the current signature through the governed dimension-label
> projection, so every baked child selection was refused. The display signature has one
> derivation, owned by the domain and shared by the bake and redemption paths; it always
> projects governed display labels, and raw selector keys never appear in a baked body or
> custody manifest. Correcting the rendering exposed a gap in decision point 1: a revision
> derived from publication identities alone cannot supersede a body whose rendering was
> wrong, because identical sources reproduce the identical revision and the immutable body
> store refuses divergent bytes. The artifact revision is therefore derived from the exact
> publication identities **and the page source's declared render-format version**, bumped
> whenever a renderer changes its output for identical sources; a bump re-bakes and
> supersedes through the normal forward-only pointer advance. Decision point 1 is annotated
> in place.

## Context

The campaigns page measures its portfolio at request time: every page load resolves a window,
expands it to exact publication partitions, attests them in one DynamoDB `TransactGetItems`
snapshot, and executes an Athena measurement batch. The exact-read design permits at most 50
partitions per snapshot — not a tunable budget but transport arithmetic: attestation reads two
items per partition (the `CURRENT` pointer and the immutable publication it names), and
DynamoDB caps one transactional read at 100 items.

The portfolio's metrics touch four daily-grain source packs, so the fan-out is four partitions
per distinct calendar day. On 2026-09-15, extending dev's published coverage from 7 to 37
fictional days let the page resolve its full declared default for the first time — 7 days plus
the required equal prior — and the arithmetic fired: 14 days × 4 packs = 56 partitions > 50.
`POST /api/presentation/campaigns` returned `ADLY-PUBLICATION_SELECTION-0002` and the page was
down. The defect was latent from the page's design, masked only by fixture coverage too short
to make any declared preset available: with a required prior, 7D needs 56 partitions, 14D
needs 112, and 30D needs 240. The page's contract and its serving path have never been
mutually satisfiable.

The [high-level design](../ui/campaign-portfolio/high-level-design.md) reserved the exit: "a
batch that cannot fit the route's latency budget even in parallel splits the endpoint or moves
to precomputed artifacts — never a timeout raise." The performance steering states the general
rule: when a request's variants are a small declared set — preset windows — precompute them
when the underlying data publishes rather than at request time. The original decision assumed ADR-068's
publications-stream worker could persist page artifacts while warming insights. The deployed design
corrected that assumption: the Preset Artifact Publisher independently materializes deterministic page
artifacts, while Campaign Insight Precompute separately remeasures evidence and warms revision-keyed
model slots.

## Decision

**Declared preset windows are the campaigns page's only windows, and they are served
exclusively from artifacts rendered at publish time; the request path never measures a
preset.**

1. The Preset Artifact Publisher owns deterministic artifact persistence. When a relevant
   `CURRENT` publication changes, dependency-aware scheduling must resolve each affected declared
   preset, render the portfolio parent/custody/ordered segments, and store them behind one forward-only
   (page, variant) pointer. The displayed-cohort detail tier mandated by the 2026-09-17 amendment remains
   pending in the current implementation; the tail keeps its declared live-on-open tier. Campaign
   Insight Precompute is a separate optional cache warmer and never owns artifact persistence. Artifact
   revision derives from the exact publication identities plus the page render-format version, so a
   corrected renderer can supersede wrong bytes built from identical sources.
2. The resolved default is itself a baked variant. The default-resolution ladder — preferred
   preset with required prior, else the next smaller available preset with prior, else the
   deterministic partial window, else `window_unavailable` — resolves at bake time, so every
   non-selected page load is also served from the shelf.
3. Custom windows are withdrawn from the campaigns page contract. The `TemporalControl.v1`
   grammar keeps its `custom` block; the campaigns page declares it unavailable. Reintroducing
   custom windows is a separate future decision that must derive its duration bound from the
   snapshot arithmetic rather than declaring one.
4. One serving path. The request-time preset measurement path is retired in the same change
   that ships artifact serving; there is no fallback from a missing artifact to live
   measurement, because a fallback reintroduces the refused-selection failure
   nondeterministically. A missing artifact serves the page's designed precompute-lag or
   `window_unavailable` state. *(Annotated 2026-09-17: this rule governs the preset page
   path. The campaign detail tail's live serve is a declared tier with its own budget, not a
   fallback; see the detail-tiers amendment above.)*
5. Bake-time evidence snapshots are taken per source pack: each pack's current-plus-prior day
   partitions are attested in one transaction, sized well under the cap for every declared
   preset *(annotated 2026-09-16: true only because the declared set is bounded by the same
   arithmetic — 2 items × 2 windows × days ≤ 100 caps a preset at 25 days, which withdrew
   `30d`; see the amendment above)*. Cross-pack instant consistency is deliberately not claimed — independent sources
   deliver and restate on independent cadences, so a cross-pack single instant asserts nothing
   semantically real. Within a pack, both comparison windows are attested together, so a
   restatement can never fabricate a delta.
6. Sort stays server-owned but moves to serve time: the baker stores one canonical artifact
   and the handler reorders rows deterministically for a declared sort request. Sort variants
   are not baked *(superseded 2026-09-17: ADR-071 bakes complete ordered segments per
   declared sortable column and direction and retires serve-time reorder; see the amendment
   above)*.
7. Session custody stays request-scoped: baked artifacts are capability-free, and the handler
   mints owner/session-bound selection capabilities onto the served artifact exactly as the
   retained-artifact path already provides for.
8. A declared preset is a checked promise: a build-time check proves every declared preset of
   every page resolves within the per-pack snapshot bound and the bake budget, so a page can
   never again declare a window its serving path cannot honor.

## Consequences

The user path collapses to a pointer read, an artifact fetch, capability minting, and cached
insight reads — the transactional-read cap now constrains a background job that can retry,
not a page load. The revision chain extends end to end: the insight cache (ADR-068), the
child-capability parent-revision check, and now the served artifact all name the same exact
publication set. Freshness follows publications, never clocks: a restatement flips pointers,
the stream re-bakes, and the artifact pointer advances; readers mid-session keep one coherent
revision until refresh.

A page load can now be seconds newer than a publication (bake lag). The page states its
"loaded as of" stamp, and precompute lag beyond one publication cycle is alarm-worthy — both
already declared in the [operational design](../ui/campaign-portfolio/operational-design.md).
A fresh environment with no baked artifact serves `window_unavailable`, not an error.

Adding a preset becomes: declare it, let the build check prove it bakes, and the worker picks
it up. Bake cost per publication is bounded by declared variants × pages, and grows with
marketplaces — the review triggers below watch that product.

The campaigns page loses custom date ranges. That narrows the contract deliberately: presets
are the paved road, and the escape hatch returns only with a derived bound.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Chunk the request-time snapshot into per-pack transactions and keep live measurement | Buys headroom for 7D/14D but 30D with prior needs 60 day-partitions per pack — 120 items, over the 100-item cap even alone. Request cost also stays linear in window size and packs; the page contract remains unservable. Per-pack snapshots are adopted, but at bake time. |
| Raise `MAX_EXACT_CURRENT_PARTITIONS` without changing the read protocol | Pinned by transport: the old attestation used 2 items per partition against DynamoDB's 100-item transactional-read cap. A constant-only increase remains rejected. *(Superseded in scope 2026-09-22: the verified A/B/C servable-head protocol uses at most one item per partition in each transaction and safely raises the serving capability; [Generation Coordination](../ui/generation-coordination.md) retains a separate bake policy.)* |
| Shrink presets until live measurement fits | 6 days with prior is the largest fitting window. Surrenders the page contract to an implementation bound and still pays a wide snapshot plus a measurement batch per page view. |
| Keep both paths: serve baked artifacts, fall back to live measurement | The refused-selection failure returns as a nondeterministic tail, and two serving paths for one contract is standing tech debt. Explicitly rejected in favor of one path. |
| Bake all eight sort variants | Eightfold storage and bake work for a deterministic reordering of identical rows. *(Superseded 2026-09-17: valid only while the population fits one artifact; ADR-071 bakes orderings because a 50-row reorder cannot express a population ranking.)* |
| TTL-refreshed artifacts instead of stream-triggered re-bakes | The source is versioned; the caching steering already forbids TTLs over versioned data. The publications stream is the exact invalidation. |

## Review trigger

- A second page adopts artifact serving — generalize the artifact store rather than cloning it.
- Custom windows are demanded — a new decision that derives its duration bound from the
  per-pack snapshot arithmetic.
- Bake duration approaches the publication cadence, or multi-marketplace scope multiplies the
  bake fan-out past its budget.
- ~~A real workspace population exceeds the portfolio row bound — the "bake all sort variants"
  rejection above was reasoned for populations that fit the artifact; above the bound a sort
  field selects a different cohort, and the ranked-cohort and tail-reach slice
  reopens that trade.~~ *Fired 2026-09-17 as a product requirement; resolved by
  ADR-071.*
- The build-time preset check and the baker disagree about resolvability — the check's model
  of the arithmetic has drifted from the selector's.
