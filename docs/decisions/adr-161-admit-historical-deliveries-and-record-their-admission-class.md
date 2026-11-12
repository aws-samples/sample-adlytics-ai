# ADR-161: Admit historical deliveries and record their admission class
Date: 2026-11-12
Status: Accepted (owner decision D1, 2026-11-12). Execution-graph contract revision 3.
## Context
The historical predicate (V-8) decodes a delivery's `retrieved_at` against the dev fixture planner's replay band and calls it `historical` when its period ends on or before the backfill cut, 2026-08-11. For any retrieval after the band, which means every real delivery, the period alone decides.

Until revision 3, four places refused a historical delivery, in every environment:
- decision-table row 4, `class_historical → REFUSED FULL_HISTORY_NOT_ENABLED`;
- the caller label `full_history`, refused before any read;
- the governed materialization port, which raised `FullHistoryRefusedError` (EXEC_GRAPH-41, an invariant that pages) before any data read. The legacy SQS path that customers run calls this port, so the refusal reached customer deployments;
- the dev fixture lander.

The only stated reason was a module docstring: "v1 has no legitimate historical producer". No ADR recorded it. Its cost: a customer could not backfill any period before 2026-08-11, the documented dev phase-B landing could not run, and dev served by-ASIN sales as CURRENT only from 2026-08-10.
## Decision
**Admit a historical delivery like any other. Derive its admission class as before, record it on the receipt, and log it. No decision-table row, label or port check refuses on the class.**
1. Row 4 is retired. The table keeps rows 1-3 and 5-20 (19 rows) with their numbers, because receipts store `row` and documents cite rows by number. `RETIRED_ROWS` in `src/domain/operations/run_request.py` holds 4, and the totality proof refuses a table that restores it.
2. The `full_history` label is no longer refused. `label` stays an optional field bound into the request digest.
3. `RunRequestReceipt.v1` gains `admission_class`, recorded whenever admission read the manifest. A receipt written before that read (rows 1-3) carries none, because no class was derived.
4. The governed materialization port reads the exact manifest version first, as before, then re-attests a graph claim, then, for a historical manifest, emits one `historical_admitted` operation log with `source_system`, `source_dataset`, `period_start` and `period_end`, and fetches. The legacy path writes no receipt, so there the log is the record.
5. `FULL_HISTORY_NOT_ENABLED` leaves the contract's refusal classes. EXEC_GRAPH-41 and execution-graph signal code 7 are reserved permanently.
## What still protects integrity
- **Decision-table rows that read no class.** Row 5 refuses a rewritten key, row 10 a correction without new input, and rows 13-15 an input older than the current, waiting or active one. `test_the_admission_class_never_decides_so_rows_5_10_and_13_to_15_refuse_history_alike` proves over the closed input domain that a historical input gets exactly the decision a cadence one gets.
- **Immutable raw keys** ([ADR-030](adr-030-the-raw-zone-landing-contract.md)): `retrieved=` is the key's last segment, so a re-pull is a new key and never an overwrite.
- **The legacy path's revision ordering** ([ADR-047](adr-047-separate-attempts-leases-publications-and-coverage.md)): a candidate strictly older than its partition's CURRENT never publishes (`_split_stale_stages`, `src/adapters/jobs/ingest_delivery/semantic_sidecar.py`).
- **Dev phase B lands final values.** Each backfilled day carries the values the full replay leaves CURRENT ([ADR-086](adr-086-shape-dev-seed-data-with-declared-deterministic-variation.md); `test_backfill_values_equal_what_the_full_replay_leaves_current` pins all 3,663 AMC fixture-days).
## Consequences
- `contract_revision` 3 re-keys every input fingerprint (`manifest_fingerprint` keys on the revision). A revision-2 input delivered again is decided afresh, not JOINED at row 9.
- The class is still decoded from the dev replay band, so for a real delivery `historical` means "period ends on or before 2026-08-11". It is a recorded attribute only; no behaviour depends on it.
- A large customer backfill now runs, and costs Athena queries and bake fan-out on the legacy entry in proportion to the days it covers.
## Reversibility
Restore row 4, the label refusal and the port check under a new `contract_revision`. Receipts keep their row numbers and no stored item changes, so a reversal needs no migration.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| A deploy setting `historical_admission`, default allow | Adds a setting nobody needs |
| Fix only the out-of-band date check | Helps customers, but dev phase B stays refused |
| Keep the refusal and design a slice-4 path | The slowest option; every customer stays without history until then |
| **Remove the refusal; record and log the class** | **Chosen** (owner D1, 2026-11-12) |
## Evidence
- The four refusal sites and the docstring reason [verified 2026-11-12, repository at public/main cb8a6009: `contracts/operations/execution-graphs.yaml` row 4 and `caller_labels_refused`; `src/adapters/inbound/run_requests/service.py` `_pre_domain`; `src/adapters/outbound/governed_materialization.py` module docstring and `require_not_historical`; `src/adapters/jobs/land_fixtures/__main__.py` and `job.py`].
- The legacy SQS handler fetches through the governed port [verified 2026-11-12, `src/adapters/inbound/ingestion_handlers/load.py`, the `fetch` stage].
- The owner approved D1 (admit by default, record the class) on 2026-11-12 after a review of the controls that blocked history.
## Review trigger
A delivery path that admits by class; a need to refuse history for a specific source; or a real producer whose retrievals fall inside the dev replay band.
