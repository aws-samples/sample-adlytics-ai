# Query Path — architecture

## 4. Internal structure

A semantic planner resolves declared metric semantics, axes, filters, and required publication scopes. Publication State then returns one exact successful set with coverage. Only then does the SQL composer use governed identifiers and bound values to query selected immutable publications; deterministic shaping applies arithmetic and mandatory qualifiers.

## 5. Key runtime flows

1. Validate the request against declared metrics, grain, semantic compatibility, and supported operation shape.
2. Derive every required publication scope and requested interval.
3. Strongly resolve `CURRENT` and exact coverage before composing SQL.
4. Refuse missing, invalid, incomplete, uncovered, incompatible, or non-additive requests before Athena.
5. Compose SQL from governed identifiers only; bind all caller values and publication IDs.
6. Shape returned rows with decimal arithmetic, typed value states, qualifications, and exact context.

Target atomic operations remain server-owned contracts; independent calls and browser/model joins never simulate an atomic result.

## Statement wait policies

Every `AthenaQueryTarget` names its wait policy (`StatementWait`, `src/adapters/outbound/athena/query.py`); the field has no default, so no composition gets another caller's wait ([ADR-144](../../decisions/adr-144-wait-for-a-bake-statement-within-its-invocation.md)).

| Policy | Callers | Statement wait | Throttled start (`TooManyRequestsException`) |
|---|---|---|---|
| `interactive` | chat's measurement tool, presentation, insight precompute | `MAX_WAIT_SECONDS` (30 s) from the start, below the API Gateway integration timeout | retried for up to 10 s inside the 30 s, so at least 20 s remain for the statement |
| `bake_invocation` | the Baker, on both bake entries | until the page's statement deadline: the invocation's remaining time less the row's write term and the contract's 30 s tail, set by `page_bake.render_page` | retried for up to 60 s, never past the deadline |

A throttled start backs off with full jitter (`uniform(0, min(8, 0.5 × 2^attempt))` seconds); past its window, or on any other start error, the statement fails as before (`ATHENA/1`, with `throttled_attempts`). A bake statement outside `render_page` has no deadline and is an invariant (`ATHENA/5`). On a timeout the error carries `wait_policy`, `waited_seconds` and `wait_limit_seconds`. `BATCH_MAX_WAIT_SECONDS` (600 s) stays the semantic writer's wait; the bake doesn't use it.

## Athena workgroups and the batch start budget

Each execution class runs in its own Athena workgroup, so a burst in one class never queues another's queries or reads its results. `DataV2Stack` owns all of them; consumers derive the physical names with `semanticV2AthenaWorkgroupName` and never configure one by hand.

| Role | Physical name | Result prefix | Callers |
|---|---|---|---|
| CHAT | `adlytics-<env>-v2-interactive` | `v2-results/interactive/` | API presentation, the Gateway measurement tool, the agent, and ingestion's interactive reads (`ADL_ATHENA_WORKGROUP`) |
| BAKE | `adlytics-<env>-v2-bake` | `v2-results/bake/` | Baker, inline load, TableWriter, ProviderAttemptReconciler, the `ingest_delivery` and `reprocess_semantic_v2` operators, and the operator validation probes that query the current model |
| INSIGHT_PRECOMPUTE | `adlytics-<env>-v2-insight-precompute` | `v2-results/insight-precompute/` | The insight precompute worker |

Every non-interactive workgroup carries the batch scan cutoff; every workgroup enforces its configuration, pins the engine, publishes CloudWatch metrics and encrypts results with SSE-S3. Each principal may start queries only in its own workgroup and write only its own result prefix; no principal holds `s3:DeleteObject` on any result prefix, and results expire bucket-wide.

The earlier shared `adlytics-<env>-v2-batch` workgroup is retained so executions started before the cutover stay readable: the load, TableWriter, reconciler and operator principals keep get, results and stop on it, plus a read on `v2-results/batch/*`. Only ProviderAttemptReconciler may still start there, through one IAM Sid group (`ReplayPersistedBatchEnvelopes`), because it alone replays persisted start envelopes and an unsubmitted one may still name `batch`. That residual is removed once `validation/probe_athena_workgroup_migration.py` reports `removal_trigger_met` in every deployed environment: its exhaustive, strongly consistent census of persisted attempts counts zero nonterminal envelopes naming `batch`.

### The static batch start budget

Every non-CHAT Athena caller has a fixed maximum of concurrent executions: its Lambda reserved concurrency times its in-process start cap. The sum is bounded by construction, with no runtime admission machinery.

| Row | Reserved concurrency | Start cap | Maximum | What it bounds |
|---|---|---|---|---|
| `baker` | 2 (one per bake lane) | `athenaStartBudget.bakerStartCap` = 10 | 20 | The invocation-shared `AthenaBatchAdmission`, also every bake source's fan-out width |
| `table_writer` | `ingestion.tableWriterConcurrency` = 8 | 1 | 8 | Sequential statements, one in flight per invocation |
| `inline_ingestion` | 2 (0 in a quiesced or drain handoff state) | 1 | 2 | The load function's sequential statements |
| `provider_attempt_reconciler` | 1 | 1 | 1 | Replay of persisted attempts, in BAKE or the retained `batch` |
| `ingest_operator` | 1 process, by the runbook rule | 1 | 1 | One `ingest_delivery` run |
| `reprocess_operator` | 1 process, by the runbook rule | 1 | 1 | One `reprocess_semantic_v2` run or one of the three BAKE validation probes |
| `insight_precompute` | 2 | `athenaStartBudget.insightStartCap` = 4 | 8 | The precompute measurement fan-out |
| **Total** | | | **41** | |

The rule is `Σ(reserved × cap) ≤ batch_athena_budget ≤ active_dml_quota − chat_reserve_active_dml`, which at the initial values is 41 ≤ 41 ≤ 180. Every row is present exactly once, every value is an integer, a reserved concurrency of 0 is a quiesced row and an absent one (unreserved, unbounded) is refused, and the Baker cap may not fall below the certified query cap of 10 the bake budgets are proven at.

The three limits live only in the `athena_start_budget` block of `contracts/operations/execution-graphs.yaml`: `active_dml_quota: 200` is this account's Active DML quota as read back on 2026-10-13, and `chat_reserve_active_dml: 20` is design-proposed headroom for CHAT, not a service requirement. A customer with a different quota changes the contract constant. One rule is implemented twice: `cdk synth` runs `validateAthenaStartBudget` over the configured maximum each stack can render and throws on violation, and `validation/probe_athena_workgroup_migration.py` runs the Python twin over the deployed reserved concurrency, so console drift is caught. Neither accepts a configured or supplied limit that differs from the contract.

Raising Q is a config change that also raises the contract budget. As projections, not certified values: Q16 makes the total 53 and Q24 makes it 69.
