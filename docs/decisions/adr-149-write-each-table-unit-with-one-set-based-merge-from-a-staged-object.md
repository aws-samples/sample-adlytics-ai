# ADR-149: Write each table unit with one set-based MERGE from a staged object
Date: 2026-10-18
Status: Proposed (2026-10-18). Amends [ADR-075](adr-075-generate-page-artifacts-once-per-dependency-cohort.md)
§6. The owner approved the approach (O-INGEST-UNIT-CAP-001, Option A) and moved it before
publication. On 2026-10-21 the owner approved the dev scratch probe, the staging infrastructure,
the IAM grants, a 7-day staging retention and the equal-or-both-NULL comparison of decision 2.
The probe ran on 2026-10-22 with synthetic rows: both writers' rows were identical on Athena and
the approved grants sufficed. The two row ceilings in decision 7 come from that probe's timings
and a local memory measurement. Implemented on the ingestion branch on 2026-10-22 (FEAT-010);
not deployed.

## Context
ADR-075 §6 consolidates a table's immutable-record merges "into the fewest statements" the
`IngestionConfig` permits. The writer carries every row inside the SQL text: a
`MERGE … USING (VALUES (CAST(? AS …), …))` with one parameter per value, and the start envelope is
stored whole for byte-identical replay. A statement holds 200,000 envelope bytes, so statements grow
with rows (about 80 to 170 rows each), and the 10-attempt unit cap refuses a unit of more than about
740 to 1,540 rows. A vendor with more than about 840 active ASINs can't load one day of sales, and
every slowly changing dimension crosses the cap as its history grows. The verification then reads
every row back into Lambda.

No guarantee needs statement count to grow with rows. Exactly-once insertion comes from the
insert-only merge on the grain and the attempt fence; byte-identical replay needs a bounded,
deterministic envelope; reader all-or-nothing comes from the servable pointer.

## Decision
1. **Two statements per unit.** The writer composes one `MERGE INTO <table> … USING (SELECT
   CAST(<column> AS <type>) … FROM <staging table> WHERE staging_key = '<sha256>')`, insert-only,
   then one verification. That holds for every writer: the legacy queued table writer, the legacy
   inline delivery path, GRAPH unit batches and the operator jobs. The `VALUES` renderer, its value
   renderer and its packer are deleted. `M(u)` is 1 by construction, and the 10, 32, 15, 11 and 37
   caps keep their values and meaning.
2. **The merge matches nulls.** The `ON` clause uses `(a = b OR (a IS NULL AND b IS NULL))` for a
   nullable grain column and `=` for the others, the same rule as the verification join, which also
   compares every nullable non-grain column that way. No statement uses `IS NOT DISTINCT FROM`: on
   Athena it dropped correct rows when every compared row held NULL in a nullable `varchar` column,
   so a correct unit would fail verification on every receive. A first merge inserts
   exactly the rows today's merge inserts; a re-run now inserts nothing for a grain that holds a
   null, where today's re-inserted the row and failed verification as a duplicate.
3. **The unit's records are staged once, content-addressed.** Each staged line is the unit's exact
   physical record. Every value is the same text today's renderer sends, as a parameter or as an
   inlined literal, or JSON `null`, so both writers put identical text through the same Athena
   cast. The object key is the
   SHA-256 of the bytes, computed by streaming over the rendered lines without keeping them. The
   writer renders the body only just before the merge's `Begin`, for that one unit, writes it with
   `If-None-Match` and a checksum, and releases it. A staging failure reserves no attempt. Phase 1
   computes the key and counts to prove feasibility but never writes, so phase 1 stays
   authority-only.
4. **Staging is its own bucket and database.** A dedicated `v2-staging` bucket holds the objects,
   and a generated external table per physical table name (all `string` columns plus an ordinal,
   with an injected partition projection on `staging_key`) reads them. Only writers, the attempt
   reconciler and the operator role can read it; no page, bake or chat role can. The bucket is
   unversioned because it holds no first copy, so it carries the same reviewed versioning
   disposition as the other unversioned buckets, recorded in the register and on its template.
5. **Staging is derived, so it expires.** It can be rebuilt from the immutable objects and has no
   reader after its unit's verification. A lifecycle rule expires it after 7 days, the retention
   the owner chose. This does not change ADR-075's rule that Stage-1 authority is
   retained indefinitely. A replay that meets an expired object merges nothing and fails
   verification. The queued table writer then retries and re-stages; the inline and GRAPH writers
   settle that mismatch as they settle any verification mismatch today.
6. **Verification is set-based.** One statement compares the table's rows with the staged rows
   inside Athena and returns, for each identity, the target count, the staged count and both
   matched counts. The unit is accepted only when all four equal the count the writer rendered.
7. **Two measured row ceilings replace the SQL-size ceiling.** `maxRowsPerUnit` and
   `maxRowsPerDelivery` join the reviewed `IngestionConfig` values. Feasibility refuses a unit or a
   delivery over them before authority (`unit_rows_exceed_cap`, `delivery_rows_exceed_cap`), and
   GRAPH set formation never unions past the unit ceiling. Their values, and the re-derived
   `statement_allowance_ms`, come from the dev probe's time measurements and the local Lambda-side
   memory and time measurements, by rules fixed before the probe ran. One record stays bounded as
   today: a record whose staged line exceeds the 200,000-byte statement bound is refused before authority with the same code. A staged line is
   never longer than that record's merge envelope today, so every record that loads today still
   loads.
8. **Wide windows land in chunks of at most 30 canonical partitions per derived table per
   delivery.** This replaces the 2026-09-22 three-bucket landing rule, whose arithmetic assumed
   merges per bucket. The 37-object unit bound still refuses anything wider, typed and before
   authority.

Decided by rubric items (1), (3) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): every guarantee holds
and each unit now commits once; one external table per physical table is simpler than any in-run
slicing; and one renderer serves every writer.

## Will be enforced by
- `tests/unit/test_table_attempt_statements.py`: every unit composes exactly two statements at any
  admitted size, the envelopes stay under the byte bound for every catalog table, the `ON`
  clause and the verification match nulls with the equal-or-both-NULL form and never use
  `IS NOT DISTINCT FROM`, and a record one byte over the record
  bound is refused before authority while a record at it composes.
- `tests/unit/test_athena_staging.py`: the staged bytes and key are deterministic, an identity
  without records is refused, and every staged cell equals a pinned golden of the text the deleted
  renderer sent.
- `tests/unit/test_athena_semantic_storage.py`: the verification accepts an exact unit and refuses
  a missing, extra, duplicated, altered or misplaced row and an empty staged object.
- `tests/unit/test_unit_staging.py`: the write is once-only, and an existing object with different
  bytes is an invariant failure.
- The table writer, inline run, unit batching and phase-one tests: staging precedes the merge's
  `Begin` in every writer, a staging outage retries without reserving an attempt or splitting a
  GRAPH set, both row ceilings refuse before authority, GRAPH composes one set at a time just
  before it runs and settles every member of a split, and both the queued attested record and the
  inline run terminal report their accepted attempts.
- `tests/unit/test_config.py`: the shared settings resolve without the staging settings, which only
  the table writer and the delivery function require.
- `tests/unit/test_projection_coordinator_fence.py`: an inline run re-persisted with a different
  statement roster collides, which is why an upgrade deploy waits for in-flight GRAPH sets.
- `infra/test/data-v2-stack.test.ts` and the ingestion stack tests: the staging bucket, database
  and tables, and the exact grants per principal.
- `infra/test/scan-dispositions.test.ts`: the unversioned staging bucket carries its versioning
  disposition, and a versioned bucket does not.
- The dev scratch probe's recorded results: rows written by the old and new writers are identical
  on Athena, a record at the record bound is written byte-identically, and the approved grants
  suffice under a scratch role holding only them.

## Consequences
- A unit of any admitted size costs two Athena commits and two reservations per receive instead of
  up to ten. The large-seller tier loads, and a wide re-pull needs one or two deliveries instead of
  ten or more.
- The remaining row cost sits where the platform limits are: Lambda memory and invocation time,
  bounded by the measured ceilings, and Athena execution time.
- Each unit's rows are held twice for the retention period: once in the table and once staged.
  Both copies stay in the deploying account.
- A staging write adds one S3 PUT per unit per receive, and one HEAD when the object already exists.
- Verification no longer returns rows, so the result bucket no longer holds a 30-day copy of every
  verified row.
- A re-run merge after a park or a budget return no longer turns a null-grain row into a permanent
  verification failure.
- Upgrading an environment that has run needs drained delivery and semantic-write queues and no
  in-flight GRAPH write set. A persisted run's statement roster is part of its identity, and the
  roster shrinks to two statements.
- A GRAPH batch re-reads each set's immutable objects just before that set runs. It trades one
  more S3 read per member for holding one set's rows in memory instead of the whole batch's.
- Only the table writer and the delivery function need the staging bucket and database settings;
  every other function starts without them.
- In GRAPH, a transient staging failure happens after the set's inline run is persisted, so it
  becomes one more trigger of the pre-existing re-persist collision on the next receive, beside a
  park and a budget return, until the GRAPH track fixes that collision.

## Alternatives considered
| Alternative | Why rejected |
|---|---|
| Compact the `VALUES` rendering | 1.4 to 1.8 times more rows per statement, still linear in rows. |
| Slice a unit across several units in one run | Every slice inherits the receive, time and packing protocol; three design rounds left five blocking findings. |
| Producer-side chunking plus delivery-group publication | A new group authority and per-connector, per-table chunk sizes, and the per-delivery ceiling stays. |
| Direct Iceberg append outside Athena | A new dependency, and append retries duplicate rows without a new guard. |
| Raise the caps | Still linear: any wider window outgrows any cap. |
| Parquet staging | A new dependency and a second type mapping, where fidelity can drift. |
| Stage under a prefix of the authority bucket | A lifecycle filter would sit one prefix away from expiring authority. |
| Keep the rendered body in the roster | The delivery function would hold every unit's body at once, and run out of memory after authority. |

## Review trigger
A probe result that sets either ceiling below one large-seller day. A measured merge time that
grows with table size rather than unit size. A staged-object read failure in production. Or a move
of the table writer off a 900 s invocation.
