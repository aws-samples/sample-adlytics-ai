# Publication State — architecture

## 4. Internal structure

Publication State stores four independent records: append-only attempt events, expiring fenced
leases, immutable successful publications, and conditional `CURRENT` selectors. A resolver strongly
reads selectors and their referenced immutable successes before returning exact coverage.

One verified exact read supports at most **4,800 logical partitions** (`MAX_EXACT_CURRENT_PARTITIONS`,
a cost bound derived from latency measured on dev so the worst read fits chat's quick turn: ADR-070 2026-10-25); a scope read covers every GSI-discovered key, discovered once per call. Each phase
reads in ordered transactions of at most 100 items, so DynamoDB calls grow by one per 100 partitions
per phase. A serving read uses five ordered phases (an authority read skips P and P'):

1. **Discover heads (A).** Read every requested complete size-guarded `CURRENT` envelope and
   normalize its servable state through the closed decoder below.
2. **Read window pointers (P).** Read the generation pointer (`GENERATION#<conflict_unit>` /
   `POINTER`) of every window named by a `staged#<grant_id>` entry in A. With no staged entries this
   read is skipped, so a steady-state read costs A, B and C only.
3. **Attest immutable evidence (B).** Read every immutable publication named by the resolved heads,
   preserving request order and validating its full partition, publication, and fence identity.
4. **Verify heads (C).** Re-read every `CURRENT` selector.
5. **Verify window pointers (P').** Re-read the pointers of every window named in A or C, and require
   the resolved heads R(C, P') to equal R(A, P) before returning any result.

The decoder accepts exactly four states:

- absent `CURRENT` → `ABSENT`;
- all servable fields absent → authority `(publication_id, fencing_token)` for an authority read; a
  serving read refuses it as `PUBLICATION_UNREADABLE` (no pre-migration rule remains on serving reads);
- all servable fields present with `projected_publication_id = NONE` and projected fence `0` → `NONE`;
- all servable fields present with a non-`NONE` id and positive projected fence → projected
  `(publication_id, fencing_token)`.

A serving read then resolves each item: the candidates are the projected base and every staged entry
whose window pointer holds a grant ordinal at least the entry's; the largest fencing token wins, and the
head is `NONE` when the base is `NONE` and no entry is covered. A covered entry obeys the projected
pair's rule (its fence is at most authority's, and its id is authority's exactly when its fence is). An
uncovered entry is invisible, so one pointer flip makes a whole window visible at once.

In `GRAPH` the only writer of entries, pointers and (after the fold) `projected_*` is `publish`, in
three steps on `DynamoPublicationStore`:

- **Stage.** Each target is classified from stored `CURRENT` state: `published` (the base names it),
  `superseded` (the base holds a larger fence), `staged` (this grant's entry exists), or one direct
  `UpdateItem` sets `staged#<grant_id>` = `{publication_id, fencing_token, conflict_unit,
  grant_ordinal, staged_at}` under `attribute_exists(publication_id) AND attribute_not_exists(#s)`.
- **Flip.** One `TransactWriteItems` updates `GENERATION#<U>` / `POINTER` (grant, ordinal, run, root
  flow, the counts frozen at the flip, `target_count`, `target_digest`) under
  `attribute_not_exists(partition_key) OR grant_ordinal < :ordinal`, with the execution-graph
  table's `RUN#` grant fence and `EXEC_CLAIM#` lease as `ConditionCheck` members. A pointer that
  already names the grant makes the flip a no-op.
- **Fold.** Every target carrying the grant's entry gets `projected_*` from the entry and loses the
  entry, only when the entry beats the base; otherwise the entry alone is removed. The resolved head is
  never lowered, and a folded window reads with no extra pointer read.

Staging and the fold refuse a `CURRENT` without `projected_*` as `InvariantError`
`PUBLICATION_UNREADABLE`.

Every present `CURRENT` must also carry a complete valid authority envelope. Any partial servable field
set, conflicting sentinel/fence, missing authority field, malformed staged entry or pointer, or
malformed value in A or C is a typed pre-query refusal; states are never guessed or collapsed.

Each phase contains at most one item per logical partition. DynamoDB supports an ordered array of up to
100 items in one atomic `TransactGetItems`, with serializable isolation against writes and a 4 MB
aggregate response limit. A/C read complete `CURRENT` envelopes and B reads immutable publications;
both item forms share the existing per-item guard derived from
`(4 MB - 800,000 bytes headroom) / 100`, so a worst-case 100-item transaction remains below the
aggregate limit including request/response framing. A read over the partition bound is rejected
before I/O.

A=C bridges the phases, and every transaction within a phase, because the write authority is
monotonic:

- a missing `CURRENT` may become present but is never deleted/recreated, so `ABSENT` cannot recur;
- first creation may seed `NONE`; only a positive-fence projection can replace it, no writer can set
  `NONE` again, and `CURRENT` is never deleted/recreated, so `NONE` cannot recur;
- authority and projected tuples advance only to strictly higher fencing tokens and cannot return to an
  earlier `(publication_id, fencing_token)` pair;
- legacy-to-explicit projection of the same authority pair is the same normalized servable state;
- an authority advance while the projected predecessor stays selected does not change normalized
  servability and therefore remains available.

So a head that reads the same in A and C held that value throughout. Every A transaction precedes
every C transaction, so each partition's unchanged interval contains the instant between the last A
and the first C, and an accepted vector is the table at that instant however many transactions a
phase takes ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md),
2026-10-25; `tests/unit/test_exact_snapshot_chunks.py`).

A resolved move between A and C (a servable advance, or a window flip inside the read) changes the
resolved vector and refuses that attempt before any query starts. An authority-only commit, another
window's staging write or a fold leaves every resolved head unchanged, so the read is accepted. A
serving read repeats a refused attempt in full, up to 3 attempts in all with a jittered 50–150 ms pause,
then raises the "head vector changed" refusal with `attempts` in its context; an authority read makes
one attempt. An accepted read gives, for each window, all old or all new; the guarantee is per adapter
call, not per request. `CURRENT` is conditionally advanced but never deleted/recreated; immutable publication records
are never rewritten or deleted. Any out-of-band operation violating those rules is outside Publication
State authority and invalidates the no-ABA proof rather than becoming a state the reader guesses
through. See the AWS [`TransactGetItems` API](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactGetItems.html)
and [transaction isolation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html).

Consumers may declare a narrower policy bound than this generic read capability. The artifact
publisher's current product bound is owned only by
[Generation Coordination](../../ui/generation-coordination.md); widening Publication State never
widens a page declaration implicitly.

## 5. Key runtime flows

1. Load Path appends operational attempt evidence and acquires a monotonic fence for a publication scope
   or governed sibling set.
2. Only a fully validated immutable success can be submitted for authority.
3. One conditional transaction advances every governed sibling selector or none of them.
4. A resolver performs the verified A/P/B/C/P' protocol above and derives ordered exact coverage intervals
   and holes from one stable servable vector.
5. Missing state, malformed selector state, a changed head vector, divergent immutable state, an
   oversized roster, or a required coverage hole becomes a typed pre-query refusal; it never becomes
   zero, partial evidence, or inferred coverage.
