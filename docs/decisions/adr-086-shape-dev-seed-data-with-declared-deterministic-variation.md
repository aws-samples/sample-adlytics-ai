# ADR-086: Shape dev seed data with declared deterministic variation

Date: 2026-10-05
Status: Accepted 2026-10-05 (owner direction of 2026-10-04, design review rounds 1–8). Implemented
in the repository on `fix/pre-publication`; landing in dev is pending. The seeding strategy is
owned by [dev seeding](../concepts/dev-seeding.md); this ADR records the decisions and their
reasons.

## Context

The fictional dev estate was near-linear: values grew by a fixed step per day, many campaigns
moved by identical amounts, purchases did not follow clicks, returns exceeded shipments, and a
stale earlier delivery broke the retail series on 2026-09-13. Insights read those artifacts as
findings. The owner asked for history as long as is practical (at least 13 months), current
through the run date, with richer, realistic shape: seasonality, day-to-day noise, dated events,
differences between campaigns, returns that lag purchases, and ordered-versus-shipped divergence.

## Decision

**Every generated value is a pure function of a declared `shape` section in the synthetic entity
catalog and its seed. The generator reads no clock; landing reads it once and refuses before
the selection's latest `retrieved_at`.**

1. **Seed and noise.** Values come from SHA-256 draws keyed by what they draw: one draw per
   (entity, catalog field) for constants, and per (entity, date or ISO week, measure) for noise.
   A part containing `|` is refused, so the join is injective. There is no RNG object and no
   global state. Arithmetic runs at Decimal precision 28 with pinned rounding.
2. **Span and horizon.** History starts 2025-07-01, so a year-over-year window exists before the
   recent story. `last_day` is a literal in each landing commit. No value depends on `first_day`
   or `last_day`, so a longer span adds days without changing an earlier one.
3. **Two anchors.** `shape_anchor` keys the long-run trend, the 56-day story, the campaign slope,
   the conversion shape, the spike and the launch, and is never moved after the first landing.
   `event_anchor` keys the short dated events and may move by at most 14 days per recorded move,
   within a slack of 2 days to `last_day`.
4. **Events.** A promotion spike, a budget shift (held after), a launch, a pause, a coverage gap,
   ordered-versus-shipped divergence and a return-rate shift on one product (both held after
   their ramp), and a price shift (held after), each declared as an offset from its anchor. A
   reconciliation mismatch was declared and then removed after local proof (RC-5): a reported
   total that differs from its detail rows quarantines the whole day's Vendor delivery under the
   control-total assertion, so the day's retail rows would leave every window. The state is
   unreachable under that rule, and no governed rule changes to admit it.
5. **Funnel.** Purchases follow clicks through a per-campaign conversion rate and a declared
   per-scenario conversion shape; attributed revenue is purchases times a per-campaign order
   value with daily noise. Generator checks keep daily and 7-day conversion and ROAS inside fixed
   bands. A breach is fixed by a recorded seed or source-band change, never by widening a band.
   The first seed breached the ROAS band seven days past the anchor, so the committed seed is
   `adlytics-dev-shape-2026-10-04-r1`.
6. **Net-of-returns revenue.** Vendor revenue is net of returns on both reports; returns lag
   shipments by a declared lag profile and never reach shipped units.
7. **Zero rows.** On a launch-before or pause day the exposure row is present with every measure
   0, and no attributed-conversion row exists, as a grouped conversion query emits none.
8. **Scenario reassignment.** Ten of the twelve zero-conversion campaigns are reassigned; two stay
   zero-conversion. Visible campaigns may carry `stable_growth`; the bulk tier keeps the
   six-scenario cycle.
9. **Names before the span.** Campaign names are valid from 2025-01-01, so every named campaign
   has a name on `first_day`; the 2026-08-12 rename is kept.
10. **Attribution maturity `{1, 1}` in dev.** A maturation step on the last two days would be a
    common-mode drop on every campaign card that thresholds would be sized to. Dev data therefore
    does not mirror real late attribution (RC-12).
11. **`unrestated_returns: final`.** No governed signal tells a first report from a restated one:
    the Vendor contract declares no restatement cadence, and the publications port exposes only
    CURRENT. The last two days therefore carry final returns, and FR-3.4 (an artifact returns-lag
    qualification) is **unmet** (owner-accepted, 2026-10-05). The governed AMC attribution rule
    (30-day restatement window) names every day of every served window, so a card qualifier would
    not discriminate; it is limited to deep analysis. Campaign deep (page slots, detail slots and
    the campaign metric focus) carries an "attribution timing" entry naming the window days within
    the contract's `restatement_window_days` of the latest served day
    (`deep_insight_inputs.attribution_timing_entry`); no card and no retail slot carries it.
12. **Dev-only `retrieved_at`.** It is the start of `last_day`, a pure function of the span. A
    real delivery could not report a day before it ended; producer validation compares only the
    watermark date with the retrieved date.
13. **Designed states dropped after local proof.** The suppressed-rows event and the generated
    withheld residual were declared and then removed: a suppressed row in a multi-day chunk fails
    the prepublication rule `suppression:exact-bucket`, and a residual that keeps its date with a
    blank identity is refused at ingestion as a partial withholding. The hand-built traps already
    exercise both paths; generating them again would need a single-day delivery seam, which is
    its own design decision.
14. **Delivery classes, phases and the campaign-dimension seal.** Every planned delivery has a
    class: `cadence` (an AMC daily-run chunk or a Vendor daily or restating report), `whole`,
    `trap`, `seal` or `backfill`. A landing selects by class, and `cadence`, `whole` and
    `backfill` also by run day, so a first landing runs in phases: phase 0 (traps, whole
    deliveries and the seal), phase A (the newest 56 run days, every daily run replayed), then
    phase B. A daily increment selects `cadence` and `whole` from its run day, never a trap, a
    seal or a backfill delivery.
    - *Phase B is a final-state backfill (amended 2026-10-06, owner decision).* Each day before
      the replayed 56 lands once, at the values the replay would leave CURRENT, through the same
      raw zone and event-driven load: AMC in chunks of at most 3 days from `first_day`, each row
      matured as of the last run that restates its day and carrying that run's watermark through
      the retired-floor rule; Vendor as the replay's own last report for the day. At 463 days that
      is 2,038 deliveries in place of 38,489. Plan tests pin the counts, every retired floor met
      (a first-report watermark is the refused control), and the equality of all 3,663 AMC
      fixture-days with the replay. Phase A and every increment still exercise the daily
      cadence and the 30-day restatement window. Rejected: replaying every old run, which costs
      about 19 times the deliveries and fan-out for identical final values.
    - *Why a seal.* The SCD2 fold takes its base from the latest CURRENT partition at or before
      a delivery's first bucket, and the dev lake holds the old generation's campaign-dimension
      partitions (2026-08-10..2026-09-15). Modelled over the planned order, both newest-first
      and oldest-first landings carry that generation's 2026-08-10 history into the served
      partition. The seal is the operator re-land that write-discipline §2.1 rule 3 names: per
      campaign-dimension fixture, chain deliveries of at most 3 buckets (overlap 1) through
      `first_day`, every first appearance or tracked change before `last_day − 29`, and
      `last_day − 29`, then 29 tail deliveries `[b − 1, b]` up to `last_day`, all at watermark
      `last_day`, with the generated rows unchanged. Today that is 30 deliveries per fixture; a
      seal above 63 is refused at plan time (the retrieval stride envelope).
    - *Why a sequence guard (R8-1).* The fold is order-dependent, and the pipeline processes
      deliveries concurrently, so two seal deliveries in flight can fold from the same stale
      base. Seal deliveries are therefore put one at a time: the landing job reads the
      delivery's CURRENT at its last bucket, puts it, then polls every 5 s until that partition
      carries watermark `last_day` from a newer publication, and stops with `LandingError`
      after 2,880 s with no further put (amended 2026-10-06 from 600 s: a write unit the
      writer retries waits out the 960 s queue visibility, up to twice, so 600 s stopped
      before a normal retry finished). A seal landing also refuses to start without a settled
      report from `probe_pipeline_settled.py` that is at most 15 minutes old. The guard lives
      in the dev landing job only; making the governed fold order-independent was rejected,
      because it would change the ingestion sidecar for a dev-seeding seam.
    - *Recovery.* After a diagnosis, a failed seal is re-landed whole under
      `shape.seal_generation + 1` with `--classes seal`. Seal retrieval offsets are
      `(span_days + seal_generation) × 64 + i` seconds, so each generation lands at fresh
      prefixes above every cadence prefix; a re-land under the same generation would re-put
      identical keys, which trigger nothing.
15. **Dev-only watermark offset over retired partitions (design deviation, approved 2026-10-05).**
    The dev lake held one whole-span delivery per dataset (2026-08-10..2026-09-15; Vendor sales
    and traffic at watermark 2026-09-15, every AMC fact at 2026-09-16) that outranked the new
    per-day watermarks, so the ingestion path correctly refused the new days; that is the cause of
    the 2026-09-13 retail break. The landing planner therefore carries a closed, hard-coded list
    of those spans, and a generated delivery touching one lands with the larger of its own and
    the listed watermark; ties publish, so the newer retrieval wins. Vendor traps are re-dated 420
    days earlier, and trap partitions earlier landings left CURRENT get a watermark one day newer.
    Watermarks on those days are later than a real feed would send. Ingestion, supersession and
    publication semantics are unchanged; a plan test proves no stale partition survives on any
    span day and that a partition outside the list fails the test. The trap rules are owned by
    `tests/fixtures/traps/README.md`. Rejected: a dev-lake reset (destructive) and a later
    `first_day` (it would not fix the served windows).
16. **Product names (OQ-7).** The product hierarchy is a `system_first_observed` snapshot, so dev
    product names are valid only from the hierarchy's first dev observation, 2026-08-28 (read
    2026-10-05). Changing that needs a contract change outside this work.
17. **Data is fixed before display (owner decision #8, 2026-10-05).** The 2026-10-04 UX review
    found returns above shipped units (F16), an implausibly small prior retail week (F11) and one
    CTR on every campaign (F24). They were artifacts of the earlier dev generation, and the page
    shows no data-quality notes for them. The shaped seed removes them, and
    `tests/unit/test_display_data_invariants.py` pins each over the committed fixtures, with a
    control showing each check catches the review's capture. F16's narration scale is a separate,
    server-side fix: the returns card states every rate in its tile's unit.

18. **The product hierarchy lands in row chunks (amended 2026-10-06).** The generator writes one
    hierarchy row per span day, so at 463 days the single `whole` delivery needed 14 clean
    attempts on its source-aligned unit, and Stage-1 refused it on every receive because the cap
    is 10 (`projection_feasibility.MAX_ACCEPTED_ATTEMPTS_PER_UNIT`, observed live on 2026-10-06).
    The dev landing planner now splits that fixture into consecutive deliveries of
    `HIERARCHY_ROWS_PER_DELIVERY` (238) rows. Each chunk is still a `whole` delivery, with its
    rows' days as its window. 238 rows is 7 MERGE statements under the 900-parameter bound, a term
    of 8, and 34 whole cycles of the 7 products, so every chunk is a complete snapshot. A fixture
    at or under the chunk size stays one delivery. The ingestion caps, Stage-1 and the writer are
    unchanged. The plan tests prove every chunk fits the cap, that the unsplit fixture does not
    (the positive control), and that no ingestion module imports the planner. Rejected: skipping
    the hierarchy, because product validity on the new seed would then be unproven; and raising
    the cap, which is a reviewed ingestion value.
## Consequences

- The committed generated fixtures grow from 1.2 MB to about 15 MB for 463 days (2025-07-01 to 2026-10-06).
- Regeneration is deterministic and byte-identical; `make check-fixtures` enforces it.
- A later day lands as a small increment without moving any earlier final value.

## Alternatives considered

- **Random noise with a fixed RNG seed.** Rejected: draw order would couple unrelated values,
  and a code change could shift every later draw.
- **Hand-tuned per-campaign tables.** Rejected: not reviewable at 60 campaigns and 463 days.
- **One anchor for everything.** Rejected in design round 5: an anchor move changed values across
  the whole history while only recent days re-landed, leaving a seam.

## Review trigger

Revisit when a real AMC or Vendor feed replaces the fictional estate in any environment, or when
the served windows outgrow the 56-day story.
