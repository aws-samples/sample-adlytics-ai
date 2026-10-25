# ADR-146: Publish the Reach quarter, or retire it, on measured partitions
Date: 2026-10-18
Status: Proposed (2026-10-18). On 2026-10-18 (UTC) the owner approved measuring first:

- fix the pin if the fix is small;
- otherwise remove the quarter option until quarterly data exists, with no dual path.

The decision rule below is fixed. The branch taken is recorded here, from the read-only probe's
evidence, before acceptance. Under branch (d) the owner chooses how to attest the quarter or
retires the option. Under branches (a) or (d) it amends ADR-070 (marker added there).
> **Amendment, 2026-10-25.** The branch taken moves from (d) to (a′), attest by ADR-070's chunked
> two-pass snapshot (owner decision 2026-10-23); see "Branch taken" below.

## Context
The Reach page offers week, month and quarter. The quarter variant was unavailable in 8 of 8 dev
bake runs on 2026-10-17, and again on 2026-10-18, with no queries. It's refused at pin because one
selection resolved to more than 100 exact partitions (`SELECTION_TOO_LARGE`).

The 100 is DynamoDB `TransactGetItems`' limit for one transaction. The pin applies it to a whole
selection, which is stricter than ADR-070 decision point 5: per-pack attestation, no cross-pack
claim. The build gate models Reach as periodic snapshots, about 4 partitions for the quarter.

Unknown until measured: whether the packs resolve day partitions, period publications or extra
scopes; whether the quarter's distinct-count figures can be computed from what they resolve; and
whether chat's quarterly reach question, which binds through the same selection-wide bound, is
refused too. The pin's log line carried only the error code, so the count was never recorded.

## Decision
A read-only probe composes the quarter render as the bake does, against dev's current publication
state, and runs chat's quarterly reach reads the same way. It records partitions per pack and in
total, each partition's grain, the raising call, and whether chat raises. Then:

| Measured | Branch | Change |
|---|---|---|
| Day partitions; each pack within 100; total over 100 | (a) Attest per pack at pin | The bake applies the 100 bound per pack under one pinned instant, in the reader both bake entries share. Chat's selection-wide bound is unchanged |
| Period publications exist, but the pin resolves days or extra scopes | (c) Resolve the pin as the gate models it | The pin reads the period publications, and the gate and pin counts are held equal by a test |
| Day partitions over 100 in a pack, and the quarter's figures can be computed from them | (d) Owner's choice: attest the quarter's day partitions, or retire | (a′) Attest them by ADR-070's chunked two-pass snapshot: the exact read takes each pass in chunks of 100 and verifies the heads twice, so the bound rises to a cost bound. Or retire, as (b) |
| The quarter needs data no publication carries | (b) Retire the quarter preset | Removed from the page declaration, the bake roster, the source, the gate and the docs in one change |
| Anything else | Stop | Written up for the owner |

No doc states chat's quarterly behaviour until the probe has measured it. The pin's refusal line
carries the required and maximum partition counts (ADR-142).

**Branch taken: (a′), attest by the chunked two-pass snapshot (2026-10-25).** The probe ran
read-only against dev on 2026-10-18 (UTC). The quarter's bake pin resolved 184 day partitions in one
dataset (`fct_ad_exposure`, 92 days × 2 lenses), over the bound of 100, and `select` raised
`ADLY-PUBLICATION_SELECTION-0002`. The last-complete-month control resolved 60 and passed. Chat's
"reach last quarter" answered from `fct_exposure_frequency`'s one quarter-bucket partition; "reach
by channel last quarter" was refused with `ADLY-PLAN-0008`. On 2026-10-18 the owner chose a
precomputed quarter-level item over retiring the option. It was never built: none of the three
publication paths moves the servable head in one transaction the item could share. On 2026-10-23 the owner approved ADR-070's
own mechanism instead: the exact read already reads in chunks of 100 and verifies the heads twice,
so its bound rises to 1,000 ([ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)
amendment, 2026-10-25) and the quarter's 184 partitions bind in one verified read per pack. Chat's
`ADLY-PLAN-0008` is a planner refusal this doesn't change.
Decided by rubric item (1) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a declared view that
never fills is a broken promise on the page.

## Will be enforced by
- Branch (a) or (c): `tests/unit/test_publication_selection.py` with paired per-pack and
  selection-wide cases, or the gate-equals-pin test.
- Branch (a′): `tests/unit/test_exact_snapshot_chunks.py`, every interleaving of head advances
  between chunked passes at 1, 100, 101, 184 and 1,000 partitions; `test_publication_selection.py`'s
  184-partition selection paired with the 1,001 refusal; the probe recording a bound quarter.
- Any branch: the quarter run through both bake entries, the graph task and the legacy entry
  every deployment outside dev runs.
- Branch (b): the page-schema validation and the roster contract tests, with no quarter preset
  declared.
- The probe's unit test on fictional publications: per-pack counts, the raising call, chat's
  outcome, and no identifiers.

## Consequences
- Branch (a) or (c): the quarter view publishes like week and month, with one more bake row's work.
  Chat stays as the probe found it.
- Branch (a′): the quarter publishes like week and month, and chat's long-window day selections
  bind too, up to 1,000 partitions. An exact read costs one more DynamoDB transaction per 100
  partitions per pass.
- Branch (b): the roster drops to 19 rows, users see week and month only, and the README's known
  limits say so.
- In every branch, a future refusal of this kind names its partition count.

## Alternatives considered
- **Chunk one selection's attestation across reads regardless of pack.** Rejected: it splits a pack
  across reads, which the per-pack snapshot rule forbids.
- **Retire without measuring.** Rejected by the owner's decision 8a: measure first.

## Review trigger
Quarterly reach publications land, which reopens branch (b) as a build decision. Or a second page
whose fixed window resolves more partitions per pack than the exact-read bound.
