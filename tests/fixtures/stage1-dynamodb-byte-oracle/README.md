# Stage-1 DynamoDB byte-oracle fixtures (ADR-075 §3.2 / §13.1)

Static committed v1 fixtures for the shared `Stage1DynamoByteOracle` seam
(`src/adapters/outbound/dynamodb/byte_oracle.py`). The authoritative design is
`docs/capabilities/load-path/write-discipline.md` §3.2 ("Shared DynamoDB byte
oracle") and §13.1.

## Layout

```
stage1-dynamodb-byte-oracle/<typed-operation>/<branch>-v1.json
```

Every typed-operation directory carries both required branches: `maximum`
(the largest accepted request for that branch) and `one-beyond` (one byte or
one action past it, refused). An operation with additional real branches adds
them beside the required pair under the same naming rule: `ClaimUnitRequest`
adds `redrive` (the failed-unit one-time decrement) and `reclaim` (the
8-action `writing→writing` dead-writer reclaim with its one leases-table
`ConditionCheck`), `FailUnitRequest` adds
`attempt-33` (the 42-action successor-intent expansion whose trigger and
maximum successor `META`/`UNIT#` puts are pinned independently of the ordinary
26-action `maximum`), and `PersistPlanRequest` adds `replacement-maximum` /
`replacement-one-beyond` (the 100-action planned-plan replacement map with
exactly one owner update). Maximum branches are captured byte-for-byte from
the production builders; each one-beyond pins an envelope-limit refusal.

`FixedMemberDrainActivation` holds the `FixedMemberDrainActivationByteFixtureV1` profiles of
the rolling final-alias drain and successor activation: `maximum` (512 candidates in each
cohort, 51 actions), `candidates-98` (51 actions), `no-eligible-98` (42 actions), and
`one-beyond` (the maximum plus 50 distinct checks, 101 actions, refused). They are captured
from the real promoter pass over `FakeDynamo` by
`tests/unit/fixed_member_byte_fixture_support.py`. Its `--write` mode is the only regeneration
path, and that run must be reviewed. Its `--check` mode fails on any drift. Each frozen file
also passes `check_fixed_member_fixture`.

## What each fixture owns

- `typed_input` — the exact typed request fields.
- `expected_request` — the independently reviewed complete low-level SDK
  request map the production builder must emit; only transport headers and
  credentials are outside it.
- `targets[*].expected_pre_state_map` / `expected_result_map` — the exact
  strong pre-state and reconstructed resulting item as low-level maps; `null`
  is proven absence, whose exact size is zero.
- Frozen totals: `wire_json_bytes` (complete request wire bytes;
  serialization/request-completeness calibration only, never AWS item-size or
  4 MB aggregate evidence), per-target `conservative_service_item_bytes`,
  `target_upper_bound`, `aggregate_upper_bound`, `action_count`, and the
  accepted/refused `outcome`.

The `RecordSubmittedAttemptRequest` maximum pins its resulting item at exactly
400000 conservative service-item bytes via `start_envelope_padding`; the
one-beyond adds one byte and must refuse.

## Rules

- Fixtures are never regenerated during tests. Any change requires an explicit
  reviewed fixture-version and design update; `check_fixture` fails on any
  drift between the frozen maps and the frozen totals.
- The reference checker (`tests/unit/byte_oracle_support.py`) uses only the
  standard library and imports no production oracle or builder; production
  builders and `Stage1DynamoByteOracle` must independently match the same
  frozen maps and expectations.
- Every identity in these fixtures is fictional; none is copied from a live
  system.
