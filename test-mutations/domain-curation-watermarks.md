# Mutation review — src/domain/curation/watermarks.py

- Commit: a31c162d05be5117c1ccdaf132dffc4e79e9c838
- Date: 2026-08-22
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/curation/watermarks.py TESTS=tests/unit/test_watermarks.py`
- Mutants: 86 generated, 76 killed, 10 survived
- Survivors: 0 open, 8 killed since, 10 equivalent

Second module reviewed on 2026-08-22, after `domain/measurement/figures`. Chosen because a survivor here means an out-of-order delivery silently reverts corrected data — [G-01](../docs/decision-coverage-registry.md#gap-register-g-nn) and G-02 both rest on `decide_load`.

The initial run left 18 survivors and the module was already in good shape: 20.9% survival against 78% for `figures.py`, and only 86 mutants because the module carries few union annotations. All 8 genuine survivors are now killed and the tests went from 24 to 32. **Every remaining survivor is equivalent with a stated mechanism, so this module has no open findings.**

**All three behavioural findings share one cause and it is worth naming: an existing test chose an input that could not distinguish the mutation.** Not a missing test — a test present, passing, and blind. That is a different and harder failure than the absences `figures.py` had, because coverage tooling reports these lines as covered.

## S-01 · KILLED · A checksum comparison that held only because every case sorted the same way

- Track: mechanical
- Location: src/domain/curation/watermarks.py:258
- Mutants: 1 at this location
- Mutation: `if previous.delivery.manifest_checksum == request.delivery.manifest_checksum:` — `==` becomes `>=`
- Survived because: every existing case used `sha-2222` against a stored `sha-1111`. With the request's checksum sorting *above* the stored one, `previous >= request` is false exactly where `previous == request` is false, so the mutation reached the same branch. A checksum sorting below is what tells them apart.
- Verdict: KILLED by `tests/unit/test_watermarks.py::TestARestatement::test_it_applies_even_when_the_new_checksum_sorts_below_the_stored_one`

The consequence is the one G-02 exists to prevent, arrived at from the other direction: under the mutation a genuine restatement with new bytes and a newer watermark is skipped as already-loaded. No error, no log line, and the corrected figures never land. A checksum is an identity and never an ordering, so any comparison other than equality is wrong on principle — but principle is not what a test asserts, and this one now fixes it by example.

## S-02 · KILLED · Comparing checksums by identity passes on interned literals and fails in production

- Track: mechanical
- Location: src/domain/curation/watermarks.py:258
- Mutants: 1 at this location
- Mutation: `==` becomes `is`
- Survived because: both sides were string literals, and CPython interns those, so `is` and `==` agreed in every test. They would stop agreeing in production, where the stored checksum arrives freshly constructed from DynamoDB rather than as a literal in this module.
- Verdict: KILLED by `tests/unit/test_watermarks.py::TestTheSameBytesArrivingAsADifferentObject::test_a_checksum_equal_by_value_is_the_same_delivery`

Recorded separately from S-01 despite sharing a location, because the causes differ: S-01 is an ordering that coincided, this is an identity that coincided. This is the more interesting of the two, because the test suite could not have caught it at any input built the way the others were — the fix is a test that constructs the value at runtime, which is what the ledger does.

## S-03 · KILLED · The reversed-period guard is one character from rejecting every daily load

- Track: mechanical
- Location: src/domain/curation/watermarks.py:80
- Mutants: 1 at this location
- Mutation: `if self.period_end < self.period_start:` — `<` becomes `<=`
- Survived because: no test constructed a partition whose window is a single day, so no case had `period_end == period_start` for the mutated boundary to reject.
- Verdict: KILLED by `tests/unit/test_watermarks.py::TestAPartitionIsIdentifiedBySixThings::test_a_single_day_window_is_a_valid_partition`

A single-day window is not an edge case here, it is the common case: `data-contract-vendor` makes `reportPeriod DAY` the source of truth and derives weeks and months from it. Under the mutation every daily partition raises `WatermarkError` and no load happens at all — loud rather than silent, but total.

## S-04 · KILLED · Immutability was unasserted on all five values

- Track: mechanical
- Location: src/domain/curation/watermarks.py:51, 105, 171, 185, 212
- Mutants: 5 across these 5 locations
- Mutation: `@dataclass(frozen=True, slots=True)` on `PartitionKey`, `DeliveryIdentity`, `LedgerEntry`, `WatermarkRequest` and `LoadOutcomeKind` — `frozen=True` becomes `False`
- Survived because: no test attempted to mutate any of them. A load decision and the identity it was made from are records of something already settled, and only the decorator was holding that.
- Verdict: KILLED by the five tests in `tests/unit/test_watermarks.py::TestTheseValuesAreFrozen`

Third module in three reviews to show this gap, after `domain-strategy-objectives.md` S-04 and `domain-measurement-figures.md` S-13. [G-25](../docs/decision-coverage-registry.md#gap-register-g-nn) now records it as a shape to grep for rather than one to rediscover per module: a frozen dataclass with no `FrozenInstanceError` test.

## S-05 · EQUIVALENT · `slots=True` on a dataclass

- Track: mechanical
- Location: src/domain/curation/watermarks.py:51, 105, 171, 185, 212
- Mutants: 5 across these 5 locations
- Mutation: `slots=True` becomes `False`
- Survives because: `slots` changes how attributes are stored, not what any of them are or what operations are permitted. The second of the two classes ADR-013 names. Separated from S-04 rather than grouped with it because the verdicts differ despite sharing location and operator.
- Verdict: EQUIVALENT

## S-06 · EQUIVALENT · An equality already narrowed by the branch above it

- Track: mechanical
- Location: src/domain/curation/watermarks.py:271
- Mutants: 1 at this location
- Mutation: `if request.source_watermark == previous.source_watermark:` — `==` becomes `>=`
- Survives because: line 261 returns `APPLY_RESTATEMENT` for every case where the request's watermark is strictly greater. Execution only reaches line 271 when it is not, so `>=` and `==` select the same dates. No input can distinguish them without removing the earlier branch.
- Verdict: EQUIVALENT

## S-07 · EQUIVALENT · Identity versus equality and ordering on a StrEnum

- Track: mechanical
- Location: src/domain/curation/watermarks.py:294, 300
- Mutants: 4 across these 2 locations
- Mutation: `previous.state is RunState.COMPLETED` and `is RunState.QUARANTINED` — `is` becomes `==` and `<=`
- Survives because: two reasons that both hold. Enum members are singletons, so `is` and `==` coincide for every possible operand. And `RunState` is a `StrEnum` whose values order as `completed < quarantined < started`, so `<= COMPLETED` matches only `COMPLETED` because it is the minimum; at line 300 the `COMPLETED` case has already returned, leaving only `STARTED` and `QUARANTINED`, and `started` does not sort at or below `quarantined`.
- Verdict: EQUIVALENT

The ordering half is an accident of the chosen strings rather than a property of enums — the same caveat `domain-measurement-figures.md` S-08 records for `Combination`. It stops holding if a member is added whose value sorts between them, and nothing would report that.
