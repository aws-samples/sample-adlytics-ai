# ADR-095: Expect producer deliveries on a declared schedule
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner chose to have it on 2026-10-07, changed from "not in v1". The rest of the design was approved on the owner's behalf the same day, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would extend [ADR-074](adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md), which stays in force, unchanged, until this is accepted. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
The owner requires end-to-end latency to be measurable, from source data landing through to publication. ADR-074 starts the data-to-serving clock at repository authority: a registration that never arrives creates no flow. So a delivery that is late or never comes is silent. A curve run then just waits, as `not_ready`, and nobody is told why.
## Decision
**Each producer dataset declares when it is expected. The status plane projects one slot per dataset, source scope and period. A slot is a precursor clock and never an authority.**
1. **Schedule as contract data.** Each dataset on a `producer_registration` surface in `contracts/operations/data-flows.yaml` declares `expected_delivery`:
   - cadence;
   - anchor timezone;
   - `expected_by`, an offset from the end of the covered period;
   - `grace` and `missing_after`;
   - the source-scope kind (profile, advertiser account, AMC instance or deployment);
   - the readiness it feeds.
2. **Slots.** A scheduled evaluator materializes a slot for every registered source scope, so one scope's missing delivery is never hidden by others. A slot reads `pending`, `on_time`, `late` or `missing`. Lateness is `landed_at − source_expected_at`.
3. **Heartbeat.** The evaluator emits a heartbeat every period, and its absence is alarmed (`ExpectedDeliveryEvaluatorStalled`).
4. **What a slot feeds:**
   - the `source_expected_at` latency clock;
   - `not_ready` receipts that name the late dataset, scope and slot;
   - fallback request schedules;
   - a `ProducerDeliveryMissing` alarm.
5. **What it never does.** It never gates readiness, and never guesses a cause.
6. **ADR-074 point 2 is refined, not reversed.** The data-to-serving root still starts at repository authority. The slot is a separate clock that runs before it.
## Consequences
- Every connector and registered bring-your-own dataset needs an `expected_delivery` entry. Its offsets come from the connector's capture schedule or the source's published availability.
- A late or missing delivery is visible per scope and period, and the latency report can start from an expectation where no source timestamp exists.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Keep it silent; surface missing data only as stale artifacts | Fails the owner's latency requirement, and hides which source and scope is missing |
| Gate readiness on the schedule | A schedule is an expectation, not evidence. Readiness must gate on exact coverage |
| **Declare an expected delivery per dataset and project slots on the status plane** | **Chosen** (owner, 2026-10-07) |
## Open questions
- Starting offsets, grace and `missing_after` per dataset are proposals until the first connector runs on a schedule.
## Evidence
- ADR-074 point 2 starts data-flow clocks at repository authority and excludes external extraction without an accepted timestamp [verified 2026-10-06, repository file `docs/decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md`].
- The `producer_registration` surface is triggered by a registration [verified 2026-10-06, repository file `contracts/operations/data-flows.yaml`, lines 32 to 52].
- AMC states a data availability window that sets the proposed AMC offset [verified 2026-10-06, repository file `docs/sources/amc.md`, line 300].
## Review trigger
A source that publishes an accepted immutable delivery timestamp, so the observed clock can replace the expected one.
