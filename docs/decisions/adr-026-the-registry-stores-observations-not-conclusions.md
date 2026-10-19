# ADR-026: The freshness registry stores observations, not conclusions

Date: 2026-08-19
Status: Accepted

Supersedes the registry schema sketched in an earlier revision of [data-layer](../capabilities/data-layer/README.md), which listed `status` and `staleness_threshold_hours` as stored columns. [Publication State](../capabilities/publication-state/README.md) now owns the store's shape.

## Context

Building the freshness registry meant implementing the schema §9 had sketched:

```
contract_freshness_registry
  contract_id
  last_successful_window_end
  last_successful_run_at
  staleness_threshold_hours
  status                         FRESH | STALE | QUARANTINED | NEVER_LOADED
  last_failure_reason
  suppression_rate_by_partition  suppressed rows / total rows, per partition
```

Three of those seven fields turn out to be conclusions rather than facts, and each one fails in a way the registry exists to prevent.

**`status` is a function of time, and storing it freezes the answer.** A load that finishes writes `FRESH`. Nothing writes over it until the next load. So a contract whose loads stopped three weeks ago still reports `FRESH` — the registry is at its most confident exactly when it is most wrong, and the thing it was built to detect is the thing it now conceals. There is no write that could fix this, because the value became wrong through the passage of time and nothing was running to notice.

**`staleness_threshold_hours` already lives on the contract.** `DataContract.staleness_threshold_hours` declares it and is validated there. A copy in the registry is a second home for one number, and the copy only refreshes when a load runs — so editing a contract's threshold would have no effect until the next successful load of that contract, which is the opposite of what an operator raising a threshold during an incident expects.

**`suppression_rate_by_partition` is a rate, and a rate has lost its denominator.** [ADR-019](adr-019-grain-viability-is-measured.md) turns measured suppression into the set of grains a customer is offered, against thresholds defaulting to 5% and 20%. But 50% of two rows and 50% of twenty thousand are not the same evidence, and only the second should withdraw a grain. A stored rate cannot tell them apart. A nested per-partition map also does not fit the key-value shape the serving store uses, which was the surface symptom that prompted looking at it.

## Decision

**The registry stores what a load observed. Everything derivable is derived at read time.**

Stored — an `Observation`, and every field is a fact that does not go out of date:

| Stored | Why it is a fact |
|---|---|
| `contract_id` | identity |
| `covered_from`, `covered_through` | what the data covers, which does not change after the load |
| `run_at` | when the load finished |
| `quarantined`, `failure_reason` | how the load ended |
| `total_rows`, `suppressed_rows` | **counts**, so the denominator survives |

Derived on every read, in `domain/catalog/freshness.py`:

- **`FreshnessStatus`** from the observation, the contract's declared threshold, and the current time. `QUARANTINED` outranks `STALE`, because it means the fact must not be answered from at all rather than that it is merely old.
- **`suppression_rate`** from the two counts, and `None` rather than zero when nothing was read — "no rows" and "no rows withheld" are different facts and only the second is evidence about suppression.
- **`NEVER_LOADED`** from the absence of an observation, which is now a state of `FreshnessStatus` rather than an absent record. It is distinct from "no such fact": conflating them tells a user their question is unanswerable when the truth is that it becomes answerable as soon as the first load runs.

**`Coverage.window` is `None` for a fact that has never loaded**, and an invariant ties the two together so they cannot disagree. A zero-width or invented range would imply a period was consulted; this is the same argument that leaves an empty monetary result with no currency.

## Consequences

**The registry cannot lie by standing still.** The failure this ADR exists to prevent — a stale fact reporting fresh — is now structurally impossible, because no stored field makes that claim.

**A contract's threshold takes effect immediately.** Raising `staleness_threshold_hours` changes every subsequent answer, with no load required.

**Every read costs a computation, and it is trivial.** A subtraction and a comparison per contract. The alternative bought nothing: a stored status would still have needed a read.

**`Coverage` grew two fields** — `loaded_at` and `failure_reason`. Both are things a user has to be told and neither had anywhere to live: without `loaded_at` a reader learns the data is stale but not by how much, and without `failure_reason` a quarantine arrives as an unexplained refusal.

**`FreshnessStatus` gained a fourth member.** It is a registry state that a *measurement* response can never carry, because a tool with no data refuses rather than answering. A capability response can, because describing what is not yet loaded is exactly its job.

**Per-partition suppression is not yet stored, and the grain derivation ADR-019 asks for is not yet built.** The counts have a home on `Observation` at contract level; the per-partition breakdown and the threshold-driven withdrawal of a grain arrive with the first AMC metric. Building the derivation now would mean choosing behaviour against thresholds nothing has measured, on a suppression mechanism no load has yet exercised — which is the guess ADR-019 was written to avoid.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **Store `status` and refresh it on a schedule** | A second writer whose only job is to correct the first, and it has the same failure mode one level up: if the refresher stops, the status freezes again. It also makes staleness detection depend on a cron rather than on the question being asked |
| **Store `status` and treat a stale record as stale** | The obvious fix — if the record itself is old, the fact is old. It works until a load runs and quarantines a partition, at which point the record is fresh and says `QUARANTINED`, and a *stale* record saying `FRESH` is indistinguishable from a fresh record saying `FRESH` without comparing timestamps. At which point the status is being derived anyway |
| **Keep `staleness_threshold_hours` in the registry as a snapshot** | Defensible if the intent were to record the threshold a decision was made under. It is not: the threshold is read to make a decision *now*, so the current value is the only relevant one |
| **Store the rate and the total separately** | Equivalent information and one more field that can disagree with the other two. Two counts and one division is fewer things that can be inconsistent |
| **Store a nested per-partition map on the contract item** | The shape §9 implies, and it does not fit: the serving store's value type is a flat mapping, an item has a size ceiling, and a per-partition conditional write cannot be expressed against one item that holds every partition. Per-partition state belongs in per-partition items |

## Review trigger

Revisit when the first AMC metric is planned. That is the point at which per-partition suppression counts must be written and ADR-019's grain derivation becomes real, and it will need a decision about where a per-partition item lives — the run ledger already keys on a partition and may be its natural home.
