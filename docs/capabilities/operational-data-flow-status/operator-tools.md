# Operator tools for the execution-graph authority

Companion to the [Operational Data-Flow Status](README.md) package. It documents the command-line
tools an operator uses to run, inspect and validate the execution-graph authority and the
ingestion paths it governs, in any environment you deploy this repository to.

Every tool takes its target as explicit inputs. None of them carries a baked profile, account,
region or environment, and each refuses to act when its inputs are missing or disagree.

## Common inputs

| Input | Meaning |
|---|---|
| `--profile <PROFILE>` | A named AWS CLI profile for your own operator identity. Never `default`. |
| `--region <REGION>` | The Region the stacks are deployed in. |
| `--expected-account <ACCOUNT_ID>` | Your 12-digit account. The tool reads the caller identity first and stops on a mismatch. |
| `--env <ENV>` | The deployment environment name the stacks were synthesized with (for example a development environment). |

Run every tool from the repository root with `uv run python -m ...` (or `uv run python validation/...` for the probe runner), after `make install`.

## Execution-graph operator CLI

**Purpose.** Human-only commands against the retained execution-graph table: the authority mode,
run-request admission on demand, and run and hold commands. Each command writes exactly one
immutable command receipt. An agent principal is refused with `AGENT_MUTATION_NOT_ENABLED`.

**Prerequisites.** The execution-graph store stack is deployed, and your profile is the bound
human operator identity (no Lambda or agent role holds these permissions).

```bash
uv run python -m adapters.jobs.execution_graph.operator \
  --profile <PROFILE> --region <REGION> --expected-account <ACCOUNT_ID> --env <ENV> \
  --store-stack <STORE_STACK> show-mode

uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  set-mode --command-id <COMMAND_ID> --to SHADOW --expected-revision <REVISION>

uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  admit --request-id <REQUEST_ID> --manifest-key <KEY> --version-id <VERSION_ID> \
  --reason operator_rerun --raw-stack <RAW_STACK> --bucket <RAW_BUCKET>

uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  pause|resume|cancel --command-id <COMMAND_ID> --run-id <RUN_ID>

uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  set-hold|clear-hold --command-id <COMMAND_ID>
```

**Expected output.** One JSON document: the mode item, or the decision or command receipt the
command wrote (or returned on an exact replay).

**Safety notes.**

- `set-mode` is a compare-and-swap on `--expected-revision`; read it with `show-mode` first.
- A reused `--command-id` that names a different command or run is refused with
  `COMMAND_ID_CONFLICT` and writes nothing. An exact replay returns the first receipt.
- `pause` and `cancel` are refused with `RUN_GRANT_PENDING_APPLY` while a run's release grant has
  committed and not yet applied; retry once the run reads `GRANTED`.
- The hold commands are accepted only when the mode reads `GRAPH`.

### Dead letters: `dlq-status` and `redrive`

**Purpose.** Inspect and redrive the dead-letter queues of the four graph routes. `dlq-status`
reads each queue's counts. `redrive` moves one route's dead letters back to its source queue,
so a stalled graph node resumes. A redriven envelope needs no new graph state: an expired claim
on a live run is reclaimed, a settled node acknowledges it, and a fenced run's node takes the
takeover path.

| `--queue` | Source queue | Dead-letter queue |
|---|---|---|
| `delivery` | `adlytics-<ENV>-ingestion` | `adlytics-<ENV>-ingestion-dlq` |
| `semantic-write` | `adlytics-<ENV>-semantic-write.fifo` | `adlytics-<ENV>-semantic-write-dlq.fifo` |
| `bake` | `adlytics-<ENV>-preset-bake.fifo` | `adlytics-<ENV>-preset-bake-dlq.fifo` |
| `precompute` | `adlytics-<ENV>-insight-precompute.fifo` | `adlytics-<ENV>-insight-precompute-dlq.fifo` |

**Prerequisites.** Your operator profile, not any Lambda or agent role, holds these SQS actions
on the four dead-letter queues and their source queues: `sqs:GetQueueUrl` and
`sqs:GetQueueAttributes` (both commands), `sqs:ListMessageMoveTasks` (`dlq-status`) and
`sqs:StartMessageMoveTask` (`redrive`). The CLI's shared inputs are required, including
`--store-stack`, but neither command reads the graph table. `redrive` also needs the human
operator identity.

```bash
uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  dlq-status

uv run python -m adapters.jobs.execution_graph.operator <COMMON INPUTS> --store-stack <STORE_STACK> \
  redrive --queue <delivery|semantic-write|bake|precompute> --expected-count <N>
```

**Expected output.** One `RESULT {json}` line.

- `dlq-status` prints `queues`, keyed by `--queue` name. Each entry has the dead-letter and
  source queue names, the `visible` and `in_flight` counts, and `latest_move_task` (its status
  and moved counts, or `null`).
- `redrive` prints the queue names, `visible`, `in_flight`, `latest_move_task`, `started` and
  `task_handle`. A queue that's empty at `--expected-count 0` prints `started: false` and moves
  nothing.
- A refusal prints `{"command": "redrive", "refused": <SIGNAL>}` and exits 3:
  - `REDRIVE_QUEUE_UNKNOWN` (`ADLY-EXEC_GRAPH-0031`): the name isn't one of the four;
  - `REDRIVE_COUNT_MISMATCH` (`ADLY-EXEC_GRAPH-0032`): the visible count isn't `<N>`;
  - `REDRIVE_IN_FLIGHT` (`ADLY-EXEC_GRAPH-0033`): the dead-letter queue has in-flight messages;
  - `REDRIVE_MOVE_ACTIVE` (`ADLY-EXEC_GRAPH-0034`): its latest move task is still `RUNNING` or
    `CANCELLING`;
  - `AGENT_MUTATION_NOT_ENABLED`: the caller isn't the human operator.

**Safety notes.**

- `dlq-status` is read-only. Run it first, and pass the `visible` count you read as
  `--expected-count`. `redrive` moves only from a quiescent queue: that visible count, nothing
  in flight, and no active move task.
- The counts are SQS's approximate attributes, read just before the move, and the move carries
  no inspection token. `--expected-count` is a precondition, not a compare-and-swap: a message
  that dead-letters between the read and the move is moved with the rest.
- `redrive` starts one asynchronous SQS message-move task (`StartMessageMoveTask`). SQS supports
  it for FIFO queues too, and gives each moved message a new message ID and enqueue time. Follow
  progress with `dlq-status`.
- Inspect a dead letter before you redrive it. A message that fails for a lasting reason fails
  again and returns to the dead-letter queue after the route's receive bound.

## Probe-input producer: `land_fixtures`

**Purpose.** Lands the synthetic fixture plan into the raw zone, by delivery class and run day.
Its probe mode copies one named phase-A delivery to a new retrieval, so a validation probe has a
fresh input whose rows equal the original's.

**Prerequisites.** The raw-zone stack is deployed; the fixtures are generated (`make generate-fixtures`).

```bash
uv run python -m adapters.jobs.land_fixtures <COMMON INPUTS> \
  --operation-id <OPERATION_ID> --stack <RAW_STACK> --bucket <RAW_BUCKET> \
  --probe-retrieval '<FIXTURE>#daily-run=<DAY>#chunk=<DAY>' --dry-run

uv run python -m adapters.jobs.land_fixtures <COMMON INPUTS> \
  --operation-id <OPERATION_ID> --stack <RAW_STACK> --bucket <RAW_BUCKET> \
  --probe-retrieval '<FIXTURE>#daily-run=<DAY>#chunk=<DAY>' [--no-twin]
```

**Expected output.** One JSON report naming the delivery, its key, its classification and, after
a real put, the keys written.

**Safety notes.**

- A historical (phase-B) delivery, a class or day selection with `--probe-retrieval`, or two names
  are refused with zero puts (`FULL_HISTORY_NOT_ENABLED` for historical input).
- `--no-twin` is accepted only with `--probe-retrieval`: the key is put and no `on_demand` twin
  request is sent.
- Always read the `--dry-run` report first; it must name exactly one delivery.
- A probe copy stays in the raw zone as the newest retrieval of its window. Copy only a delivery
  no later planned delivery restates.

## Producer CLI

**Purpose.** Describes governed packs, validates a producer result, and renders the landing keys
and the registration a scheduled producer needs.

```bash
uv run python -m adapters.jobs.producer pack-describe <PACK_ID>
uv run python -m adapters.jobs.producer validate-result <PACK_ID> <RESULT.csv> [--registration <REG.json>]
uv run python -m adapters.jobs.producer plan-landing <PACK_ID> --execution-id <ID> --workflow-id <ID> \
  --schedule-kind <KIND> --source-scope-namespace <NS> --source-scope-value <VALUE> \
  --marketplace <MARKETPLACE> --period-start <DAY> --period-end <DAY> \
  --time-window-start <INSTANT> --time-window-end <INSTANT> --source-watermark <DAY> \
  --retrieved-at <INSTANT> --object-version-id <VERSION_ID>
```

**Expected output.** The rendered pack, a validation verdict, or the planned keys and registration.

**Safety notes.** The CLI renders and validates; it does not upload. Use fictional identities in
any registration you create for testing.

## Stream failure queues: `attempts_failures`

**Purpose.** This is the human disposition of every stream consumer's failure queue. Every stream
event source mapping is bounded (`AsyncFailureBoundsAspect` fails synth otherwise).

- **What arrives.** When a consumer exhausts its bounded retries on a stream batch, Lambda sends
  that batch's stream metadata to the consumer's failure queue.
  - The bound is 100 retries or one hour of record age, whichever comes first
    (`asyncFailureBounds` in `infra/lib/config/index.ts`). A failing batch is bisected first.
  - The metadata is `DDBStreamBatchInfo`: shard, sequence range, arrival times and batch size.
  - It sends one message per failed batch, not one per record.
- **The alarm.** Each queue's alarm pages the staffed destination once for each period with an
  arrival. An arrival is a sent message or any rise in visible depth. Messages left in the queue
  don't hold the alarm in `ALARM`, so the next exhaustion pages again.
- **Who consumes it.** Nothing but this tool.

`--consumer` is required and selects exactly one queue; it is never defaulted. Queue and alarm
names below omit the `adlytics-<ENV>-` prefix.

| `--consumer` | Stream consumer | Owning stack | Failure queue | Alarm | Recovery after drain | 24 h accepted loss |
|---|---|---|---|---|---|---|
| `attempts` | Attempts producer | `Adlytics-OperationalStatusProcessor-<ENV>` | `operational-status-attempts-failures` | `operational-status-attempts-exhausted` | None: no replay tool | The batch's operational-status events |
| `graph-projector` | Execution-graph projector | `Adlytics-OperationalStatusProcessor-<ENV>` | `graph-projector-stream-failures` | `graph-projector-stream-exhausted` | `graph_projection_rebuild` | None once rebuilt; the graph is rebuilt from authority |
| `stage1-publications` | Stage-1 lifecycle producer | `Adlytics-OperationalStatusProcessor-<ENV>` | `stage1-publications-stream-failures` | `stage1-publications-stream-exhausted` | The one-minute recovery schedule re-reads pending outbox items | None while the outbox item stays pending |
| `agent-quality-audit` | Agent-quality audit processor | `Adlytics-AgentQualityProcessor-<ENV>` | `agent-quality-audit-stream-failures` | `agent-quality-audit-stream-exhausted` | None: no replay tool | The batch's findings and aggregate measurements |
| `insight-precompute` | Insight precompute worker | `Adlytics-InsightPrecompute-<ENV>` | `insight-precompute-stream-failures` | `insight-precompute-stream-exhausted` | A declarative re-bake | None once re-baked |
| `publisher-enqueue` | Preset-artifact enqueue | `Adlytics-Publisher-<ENV>` | `publisher-enqueue-stream-failures` | `publisher-enqueue-stream-exhausted` | A declarative re-bake | None once re-baked |

The 24 h column is what is lost once the stream's 24-hour retention passes without the recovery.

**Prerequisites.** The owning stack is deployed. The tool resolves the queue as the one
`AWS::SQS::Queue` resource of that stack with that name.

```bash
uv run python -m adapters.jobs.operational_status.attempts_failures status --consumer <CONSUMER> \
  <COMMON INPUTS>
uv run python -m adapters.jobs.operational_status.attempts_failures drain --consumer <CONSUMER> \
  <COMMON INPUTS> --expected-count <N> --evidence <NEW_EVIDENCE_FILE>
```

**Expected output.** One `RESULT {json}` line.
- `status` is read-only and returns `visible`, `in_flight` and `delayed`.
- `drain` returns `drained` plus the counts before and after.

**What `drain` does, in order:**
1. Refuses unless exactly `<N>` messages are visible and none is in flight or delayed
   (`EXPECTED_COUNT_MISMATCH`).
2. Receives exactly `<N>`.
3. If any message isn't a stream-batch failure record, releases them all and refuses
   (`MALFORMED_FAILURE_RECORD`).
4. Writes every record to the evidence file. It refuses an existing file (`EVIDENCE_EXISTS`).
5. Only then deletes them.

A refusal exits 2 and deletes nothing.

**Safety notes.**
- **No redrive.** A message holds the metadata of the records, not the records. The authority
  those records described (the attempt ledger, the publications, `CURRENT`) is unaffected; only
  the consumer's derived output is missing. Recovery is the table's per-consumer column.
- **Recovery window and accepted loss.** The stream keeps records for 24 hours. A batch exhausted
  by a dependency outage longer than the one-hour record-age bound loses the table's accepted-loss
  column once the window passes without its recovery. Before draining, read the evidence and name the attempts the batch covered, for
  example by reading the stream range while it's still retained.
- **Evidence content.** The evidence file names the stream ARN, so it contains the account ID.
  Keep it in ignored operator evidence, never in a tracked file.
- **Before draining**, run `status`, then pass the count it read as `--expected-count`.

## Operation catch-all alarm: `alarming-errors-by-operation`

**Purpose.** `adlytics-<ENV>-alarming-errors-by-operation` pages when an operation without its own
alarm reports a `dependency` or `invariant` error ([observability](../../concepts/observability.md#32-application-spans-and-metrics)).
Each failing `Operation` is one contributor, and the alarm names at most 100 contributors in
`ALARM` [verified 2026-10-10, https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-limits.html].
Past 100 concurrently failing operations, the state reason reads `100+ time series evaluated to
ALARM`. The alarm stays in `ALARM` and has paged, but a further failing operation is not named in
a new notification.

**Whenever the state reason reads `100+`, run the query below** for the alarm's window. It reads
the same `Errors` metric and the same paging classes as the alarm. It lists every failing
`Operation`, one series each, including the operations with their own alarm, up to the 500 series
one query returns [verified 2026-10-10,
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-metrics-insights-limits.html].

```bash
aws cloudwatch get-metric-data --profile <PROFILE> --region <REGION> \
  --start-time <START_ISO8601> --end-time <END_ISO8601> \
  --metric-data-queries '[{"Id":"errors","Period":300,"Expression":"SELECT SUM(Errors) FROM SCHEMA(\"Adlytics\", ErrorClass, ErrorCode, Operation) WHERE ErrorClass != '"'"'domain'"'"' AND ErrorClass != '"'"'validation'"'"' GROUP BY Operation ORDER BY SUM() DESC LIMIT 500"}]' \
  --query 'MetricDataResults[].[Label, sum(Values)]' --output text
```

**Expected output.** One line per failing `Operation`: its label, which names the operation, and
its error count in the window. The exact label format has not been checked against a live failure
yet [unverified].

To see which `ErrorClass` and `ErrorCode` one listed operation failed with, run the drill-down
below with `<OPERATION>` set to it. The log record of each failure carries the same `ErrorCode`.

```bash
aws cloudwatch get-metric-data --profile <PROFILE> --region <REGION> \
  --start-time <START_ISO8601> --end-time <END_ISO8601> \
  --metric-data-queries '[{"Id":"codes","Period":300,"Expression":"SELECT SUM(Errors) FROM SCHEMA(\"Adlytics\", ErrorClass, ErrorCode, Operation) WHERE ErrorClass != '"'"'domain'"'"' AND ErrorClass != '"'"'validation'"'"' AND Operation = '"'"'<OPERATION>'"'"' GROUP BY ErrorClass, ErrorCode ORDER BY SUM() DESC LIMIT 500"}]' \
  --query 'MetricDataResults[].[Label, sum(Values)]' --output text
```

**Safety notes.**

- Read-only.
- The `Adlytics` namespace has no environment dimension, so the result covers every environment in
  the account, as the alarm does.
- 500 lines from the first query means more than 500 operations failed and the query was capped
  too. Narrow the window and run it again.

## Page alarms: `page-<page>-server-errors`

**Purpose.** `adlytics-<ENV>-page-<page>-server-errors` holds one Presentation page's broken state
until an observed success clears it
([ADR-148](../../decisions/adr-148-hold-a-page-alarm-until-an-observed-success-clears-it.md)). There
are 17, one per page route, and they act on the staffed destination on `ALARM` and `OK`. What each
state means is in [observability §3.2](../../concepts/observability.md#32-application-spans-and-metrics).
The catch-all excludes the page operations, so a page failure pages here, not there.

**What to do in each state.**

- **`ALARM`:** the page answered a request with a 5xx. Run the catch-all's drill-down query above
  with `<OPERATION>` set to the page's operation (named in the alarm description, for example
  `presentation.reach`) to read the `ErrorClass` and `ErrorCode`. The alarm stays in `ALARM` while
  nobody calls the page, so a quiet page is not a fixed one. After a fix deploys, serve the page
  once (below) and wait for `OK`.
- **`OK`:** a 5-minute period served at least one request without a 4xx refusal and with no 5xx.
  The `OK` notification is that recovery. Nothing to do.
- **`INSUFFICIENT_DATA`:** the page has not been served since its alarm was created. That is not
  a failure, and it is not a recovery. Its disposition is the route's acceptance probe, run once:
  `validation/probe_presentation_hydration.py`, or `validation/probe_insight_deep_dive.py` for
  `insight-deep-dive`. A new page route's acceptance calls it once. Until it has run, this entry
  names that page alarm as never called.

**Deploy order.** Never `deploy-all` this change onto an environment that already has the
catch-all. `deploy-all` deploys `IngestionStack` before `ApiStack`, so the catch-all would stop
counting the pages before their page alarms exist. Deploy `ApiStack` first, per stack under the
deploy lock ([Shared-environment deploy and state tools](#shared-environment-deploy-and-state-tools)).
Run the acceptance probes and confirm every page alarm reads `OK`. Then deploy `IngestionStack`.
On a new environment, `deploy-all` is safe.

**Safety notes.**

- The drill-down is read-only. The probes are read-only and run only against `dev` with
  synthetic data.

## Serving alarms: `GraphServingWithheld` and `PresentationServingRefused`

Both come from [ADR-139](../../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md);
their metrics are in [observability](../../concepts/observability.md#32-application-spans-and-metrics).

**`adlytics-<ENV>-graph-serving-withheld`.** A GRAPH cohort finalized with a failure entry (budget
exhaustion or a POST_GRANT invariant), so its pages kept their previous, last-good generation.
- Read the Baker's `finalize serving_withheld` WARNING line for the run and its `failure_reasons`.
  If `failure_reasons` is empty, its `binding_kind`, `execution_attempts` and `stale` fields name the
  cause.
- There is no re-drive for the same input. `admit --reason operator_rerun` joins the `SUCCEEDED`
  run (decision row 9) and `data_correction` of the same fingerprint is refused (row 10), so don't
  re-admit.
- The pages stay on last-good, and the alarm stays in `ALARM`, until the next publication's cohort
  finalizes without being withheld. A cohort of designed absences only never raises it.
- Fix the cause (the precompute budget, or the logged POST_GRANT invariant) so that next cohort is
  not withheld too.
- If `BakeDeadLettered` fires for a finalize message and the Baker logged
  `finalize serving_signal_unread`, the cohort is already finalized. Don't re-bake; once the store is
  readable, `redrive --queue bake` re-reads the binding and emits the withheld signal. (That alarm's
  own description says to re-bake, which is wrong for this cause.) A `cause` of `binding_missing`
  means the store read succeeded but held no binding for the succeeded, granted finalizer; the
  final transaction writes both together, so treat it as a store defect and explain the run
  before any redrive.

**`adlytics-<ENV>-presentation-serving-refused`.** A page refused a grant-bearing generation more
than the settle bound (270 s) after its bind. The finalizer moves `CURRENT` only under a binding
presentation accepts, so a refusal is a write-side defect, never a settling cohort. Read the
Presentation WARNING `operation refused` lines (`operation`, `error_context.reason`,
`error_context.refusal_age_seconds`), then explain the grant's run. Treat any `within_settle` count
in the O3 record as the same defect.

## Staffed alarm destination CLI: `alarm_staffing`

**Purpose.** Proves the alarm destination is staffed (FR-10, checks (a) and (b)). `verify` reads
the selected alarm topic, its subscriptions, its tags and every alarm in the ingestion and
status-processor stacks. The policy (`policy_revision`, `responsible_owner_id`) and each
environment's selected arm come from `contracts/operations/alarm-destinations.yaml`
(`make generate-alarm-destinations`). Check (c), one induced `ALARM -> OK` per gate run whose
delivery the owner confirms, belongs to the gate run, not to this CLI.

**Prerequisites.** The ingestion and status-processor stacks are deployed. For the email arm, a
human has confirmed the email subscription on the ingestion alarm topic.

```bash
uv run python -m adapters.jobs.alarm_staffing verify <COMMON INPUTS> \
  --endpoint-sha256 <SHA256_OF_THE_ENDPOINT>
```

To compute the digest without echoing the address: `printf %s '<ENDPOINT>' | shasum -a 256`.
For the topic arm, add `--topic-arn <TOPIC_ARN>`. Synth reads the same value from
`ADLYTICS_STAFFED_ALARM_TOPIC_ARN`.

**Expected output.** One `RESULT {json}` line with the endpoint digest, the subscription ARN, the
subscription count, each alarm's actions, the count of alarms the stacks list, and `passed`. It
passes with at least one confirmed subscription whose endpoint digest matches; other confirmed
subscribers on the topic are accepted. It fails if no matching subscription is confirmed, if any
alarm's actions are not exactly the selected topic, if any listed alarm is not described (for
example a composite alarm or a renamed one), or if an unlisted alarm is described.

**Safety notes.**

- Read-only: no command writes anything. It never prints, stores or logs an email address.
- The staffed-console challenge, its acknowledgement route and the `issue` and `bind` commands
  were retired on 2026-10-08 (the acknowledgement was not required).

## Span retrieval CLI

**Purpose.** Reads spans from the account's `aws/spans` log group (Transaction Search) over an
explicit window, by trace ID, by business key or by span name, and prints them as structured JSON.
Validation probes call the same module (`adapters.jobs.span_retrieval`) instead of embedding their
own Logs Insights queries.

**Prerequisites.** Transaction Search delivers spans to `aws/spans` in the target account, and
your profile may call `logs:StartQuery` and `logs:GetQueryResults` on that group. The CLI takes
`--profile`, `--region` and `--env` only, with no defaults. It doesn't take `--expected-account`,
because it reads one account-wide log group and writes nothing.

```bash
uv run python -m adapters.jobs.span_retrieval \
  --profile <PROFILE> --region <REGION> --env <ENV> \
  --window-start <ISO_INSTANT> --window-end <ISO_INSTANT> --trace-id <TRACE_ID>

uv run python -m adapters.jobs.span_retrieval --profile <PROFILE> --region <REGION> --env <ENV> \
  --window-start <ISO_INSTANT> --window-end <ISO_INSTANT> \
  --key <ATTRIBUTE>=<VALUE> [--key ...] [--span-name <NAME> ...] [--span-name-prefix <PREFIX> ...] \
  [--limit 1000] [--max-pages 8] [--lag-seconds 600]
```

| Input | Meaning |
|---|---|
| `--window-start`, `--window-end` | The half-open window `[start, end)`, ISO-8601 with a UTC offset, at most 14 days, at one-second resolution. |
| `--trace-id` | A W3C trace ID: 32 lower-case hex digits. |
| `--key` | An attribute some span declares in `contracts/observability/spans.yaml`, and its value. Repeatable; every key must match. |
| `--span-name`, `--span-name-prefix` | Exact span names, or name prefixes. Repeatable; a span matching any one is kept. |
| `--limit` | Rows per page, 1 to 10,000. |
| `--max-pages` | Queries per lookup, 1 to 64. A full page is split into two half-windows while the bound allows. |
| `--lag-seconds` | The ingestion lag of `aws/spans`, 0 to 3,600. The default is 600. |

A lookup needs at least one of `--trace-id`, `--key`, `--span-name` or `--span-name-prefix`.

**Expected output.** One `RESULT {json}` line. Its `status` is one of three states:

- `complete`: at least one matching span is in `spans`, in time order;
- `pending`: a page didn't finish within the poll bound (`reason: query_running`), the page bound
  stopped the read before any match (`page_bound_reached`), or nothing matched and the window ends
  less than `lag_seconds` ago (`inside_lag`). Re-run it later;
- `empty`: nothing matched in a window older than the lag bound.

The line also carries `window`, the echoed `query`, `pages`, `truncated` (a full page could not
be split) and `excluded`, the count of returned records that matched loosely but belong to another
trace, key or environment. A span whose resource names another `deployment.environment` is
excluded; a managed service's span carries no environment and is kept. Exit 0 for any of the three
states, 3 for a refused lookup (`{"refused": <reason>, "code": "ADLY-SPAN_RETRIEVAL-0001"}`), 1 for
a failed query or malformed span record (`ADLY-SPAN_RETRIEVAL-0002`, `-0003`), and 2 when a
required input is missing.

**Safety notes.**

- Read-only: it calls only `StartQuery` and `GetQueryResults`.
- An undeclared key, a malformed value or a quote in a span name is refused before any query, so
  no input can change the query's shape.
- `empty` is never returned for a window still inside the lag bound, or for a read the page bound
  cut short. Treat `pending` as "not yet known", never as a pass.
- Spans carry identifiers, never row content or credentials (the span contract forbids them).
  Keep printed results in your ignored evidence directory.

## Validation probe runner

**Purpose.** Runs the traceability slice probes (`--probe P0` to `--probe P5`), the shared-asset
import scan (`--asset-scan`), the assets record (`--record-assets`) and synthetic persona
provisioning (`--provision-persona`). It writes one evidence JSON per run and exits nonzero on any
failed check. Integration files it runs fail closed on any skipped test.

```bash
uv run python validation/probe_ingestion_traceability_gate.py --record-assets <STACK> \
  --evidence-dir <ABSOLUTE_EVIDENCE_DIR>

uv run python validation/probe_ingestion_traceability_gate.py --asset-scan --sha <COMMIT> \
  <COMMON INPUTS> --evidence-dir <ABSOLUTE_EVIDENCE_DIR> --set-stack <STACK> ...

uv run python validation/probe_ingestion_traceability_gate.py --provision-persona <LABEL> \
  --persona-group <GROUP> <COMMON INPUTS> --evidence-dir <ABSOLUTE_EVIDENCE_DIR>

uv run python validation/probe_ingestion_traceability_gate.py --probe P4 --sha <COMMIT> \
  <COMMON INPUTS> --evidence-dir <ABSOLUTE_EVIDENCE_DIR> --bucket <RAW_BUCKET> \
  --raw-stack <RAW_STACK> --operation-id <OPERATION_ID> --store-stack <STORE_STACK> \
  --p1-evidence <P1.json> --p2-evidence <P2.json> --p3-evidence <P3.json> \
  --baseline-graph <GRAPH_ID>=<DIGEST> --set-stack <STACK> ... --assets-file <ASSETS.json> ... \
  --diff-file <DIFF.txt> ...

uv run python validation/probe_ingestion_traceability_gate.py --probe P5 <P4 INPUTS> \
  --p4-evidence <P4.json> --staffed-endpoint-sha256 <SHA256_OF_THE_ENDPOINT>
```

P5 publishes probe-scope gauges and moves the probe stuck-flow alarm
(`adlytics-<ENV>-open-flows-past-ceiling-probe`) from `ALARM` to `OK`, so the staffed destination
receives two notifications for each run. It records that expected alarm before the control runs,
and it fails if any other alarm enters `ALARM` while it runs.

**Expected output.** `evidence: <path>` on stdout; the JSON's `passed` is the verdict and `checks`
names each assertion.

**Safety notes.**

- P4 writes only to the probe table, under the probe role, in a fresh namespace per test; it
  reads the raw zone and the coordinator table without writing.
- P5's fault controls and stuck-flow obligation write only to the probe table, under the probe
  role. Its one live write is a run-request receipt (`admit`).
- Persona provisioning is for development environments only and creates fictional users.
- Never run `make test-integration`; the runner runs one integration file at a time.

## Shared-environment deploy and state tools

**Purpose.** One deployer at a time changes a shared environment, from one declared source, and
each step around the deploy proves the environment's state
([ADR-136](../../decisions/adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md)). Every tool ends in one exit code, and
**a non-zero exit is the stop**: read the record it names before doing anything else.

**Inputs.** Every target takes `PROFILE`, `ENV`, `REGION` and `ACCOUNT` (the
[common inputs](#common-inputs) `--profile`, `--env`, `--region` and `--expected-account`) and
refuses to run when one is missing or `PROFILE` is `default`. `EVIDENCE_DIR`, `LOCK_FILE` and
`LOG_FILE` are paths you choose: keep them in an ignored, durable directory outside `/tmp`, so a
restart cannot remove the lock or its record. `EVIDENCE_DIR` must be an existing absolute
directory. The stack set is always the app's own `cdk list --long --json` under your deploy
configuration (`.env`, as `make deploy-all` reads it), saved beside the record, and each stack is
read in its own Region: the edge security stack is not in `REGION`. Alarms are read in `REGION`.
The deploy lock's targets add the CDK bootstrap stack in each of those Regions (see
[the lock](#make-deploy-all-locked-scriptsdeploy-lockedsh)). Every AWS read names one of those
stacks, a queue one of them owns, or the `adlytics-<ENV>-` alarm prefix; no tool lists the account.

### The deploy sequence

1. `make dev-state`: exits 0, every alarm not `OK` and every non-empty failure queue disposed.
2. `make check-staged-tree`: the staged Lambda tree is the committed one.
3. `make deploy-all-locked` from the declared source.
4. `make check-alarm-window` over the deploy window, from the time step 3 started.
5. `make check-source-key`.
6. `make dev-state` again.

```bash
make dev-state PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  EVIDENCE_DIR=<EVIDENCE_DIR> LOCK_FILE=<LOCK_FILE> [DISPOSITIONS=<DISPOSITIONS.json>]
make check-staged-tree
make deploy-all-locked PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  LOCK_FILE=<LOCK_FILE> LOG_FILE=<LOG_FILE> SOURCE_SHA=<COMMIT> [SOURCE_BRANCH=<BRANCH>] APPROVE=1
make check-alarm-window PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  SINCE=<START_ISO8601> RECOVER_MINUTES=<MINUTES> EXPECTED=<ALARM,ALARM|none> EVIDENCE_DIR=<EVIDENCE_DIR>
make check-source-key PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  EVIDENCE_DIR=<EVIDENCE_DIR>
```

### Exit codes

| Code | Name | Raised by |
|---|---|---|
| 2 | `USAGE` or `READ_FAILED` | every tool: a missing or refused input, a failed AWS or git read, an identity mismatch in the state checks |
| 3 | `UNDISPOSED_STATE` | `dev-state`: an alarm not `OK`, a non-empty failure queue with no disposition, or a stale lock |
| 3 | `UNEXPECTED_ALARM` | `check-alarm-window`: a transition into `ALARM` of an alarm not in `EXPECTED` |
| 3 | `NOT_ONE_SOURCE_KEY` | `check-source-key`: not exactly one source key, or a stack unequal to this synth or not `*_COMPLETE` |
| 4 | `EXPECTED_ALARM_NOT_RECOVERED` | `check-alarm-window`: an expected alarm still in `ALARM` more than `RECOVER_MINUTES` after entering it |
| 75 | `LOCK_HELD` | `scripts/deploy-locked.sh` |
| 76 | `IDENTITY_MISMATCH` | `scripts/deploy-locked.sh`, `scripts/diff-then-deploy.sh` |
| 77 | `STACK_IN_PROGRESS` | `scripts/deploy-locked.sh`: a target reads `*_IN_PROGRESS` at entry or at force-release |
| 78 | `DEPLOY_SOURCE_MISMATCH` | `scripts/deploy-locked.sh` (HEAD is not `SOURCE_SHA`, a dirty tree, or a branch other than `SOURCE_BRANCH`); `scripts/diff-then-deploy.sh` (a dirty tree) |
| 79 | `NOT_UNDER_LOCK` | `scripts/diff-then-deploy.sh`: `ADLYTICS_DEPLOY_LOCK_HELD` does not name a lock held by an ancestor process |
| 80 | `DIFF_CHANGED_SINCE_READ` | `scripts/diff-then-deploy.sh` |
| 81 | `STAGED_TREE_DIFFERS` | `scripts/staged-tree-check.sh` |
| 82 | `WORK_IN_FLIGHT` | `scripts/diff-then-deploy.sh`: a daily ingestion work queue holds a visible, in-flight or delayed message |
| 83 | `FORCE_RELEASE_REFUSED` | `scripts/deploy-locked.sh --force-release` |
| 84 | `LOCK_KEPT` | `scripts/deploy-locked.sh`: the release wait ended without proving every stack terminal |
| 85 | `LOG_WRITE_FAILED` | `scripts/deploy-locked.sh`: an append to `LOG_FILE` failed after the start check; overrides every other code |

A wrapped command's own non-zero code, or 128 plus the signal number after a cancel, also stops.

### `make dev-state`: `validation/dev_operations.py state`

**Purpose.** Lists every `adlytics-<ENV>-` alarm not `OK` (`ALARM` or `INSUFFICIENT_DATA`), every
queue of the listed stacks with its depth, every listed stack's status in its own Region, and the
deploy lock. A
failure queue is one whose name ends in `-dlq`, `Dlq` or `-failures` (`.fifo` aside).

**Inputs.** The four target inputs, `EVIDENCE_DIR`, `LOCK_FILE`, and optionally `DISPOSITIONS`: a
local JSON object from an alarm or queue name to the exact states in which it may stand and why,
for example
`{"adlytics-<ENV>-ingestion-dlq": {"allowed_states": ["visible=1 in_flight=0 delayed=0"], "reason": "<why this message may stay>"}}`.
An alarm's states are `ALARM` or `INSUFFICIENT_DATA`; a failure queue's state is its exact
`visible=<n> in_flight=<n> delayed=<n>` depth. The same name in an unlisted state is undisposed,
with the disposition it failed recorded as `refused_disposition`. A sentence-only entry is refused,
because it names no state. An alarm entry may add `"owner"`, which places the alarm out of this
check's scope and names who owns it. Such an alarm is recorded under `out_of_scope` in every read,
with its actual state (`OK` included), its reason and its owner. It is never folded into a clean
result, and in a state it does not list it is still undisposed. Keep the file with your evidence.

**Expected output.** One `RESULT {json}` line, also written to
`<EVIDENCE_DIR>/state-<utc>.json` with HEAD, tree, branch and porcelain. `undisposed` lists what
stops it, `disposed` what the file explained, `unused_dispositions` the entries nothing matched.
A lock whose owner process is gone is listed as `stale_lock`; no disposition clears it.

### `make check-staged-tree`: `scripts/staged-tree-check.sh`

**Purpose.** Every tracked `src/` and `contracts/` file is byte-equal in `build/src` to HEAD, and
no staged `.py` file lacks a HEAD path. Run `make build-lambdas` first. Local only; no AWS inputs.

**Expected output.** One line per difference, `checked=<n> mismatches=<n> extra_py=<n> head=<sha>`,
then `STAGED_EQUAL` (0) or `STAGED_DIFFERS` (81).

### `make deploy-all-locked`: `scripts/deploy-locked.sh`

**Purpose.** Runs `make deploy-all`'s `scripts/deploy-all.sh`, with `APPROVE` and `EMAIL` passed
through as `make deploy-all` passes them, under the single-deployer lock. Without `APPROVE=1` it
only diffs, still under the lock. `REGION` must be the profile's configured Region, which
`deploy-all.sh` deploys to.

**Targets.** The lock covers exact `(stack, Region)` targets, one `<stack> <region>` line each in
a targets file saved beside `LOG_FILE`: every app stack in its own Region (the edge security stack
is in `EDGE_SECURITY_REGION` from `infra/lib/config`, whatever `REGION` is) and `CDKToolkit` in each of those Regions, because
`deploy-all.sh` may bootstrap there. `validation/dev_operations.py lock-targets` writes it from
`cdk list --long --json`, and refuses a stack for another account or without a concrete Region.

**What the lock does, in order:**
1. Creates `LOCK_FILE` complete, with one atomic link (75 when it exists). It records the pid,
   label, source and command, the account, environment and Region, and every target with the
   targets' sha256.
2. Checks the caller identity (76), the source (78) and that no target is `*_IN_PROGRESS` (77). A
   refusal here releases at once: nothing ran.
3. Exports `ADLYTICS_DEPLOY_LOCK_HELD=<LOCK_FILE>` and forks the command's own process group,
   which waits until `LOCK_FILE` also records `command_group=<pgid>`; only then does the command
   start. A failure to record it stops the group before the command runs (2).
4. When the command ends, waits until no process of that group remains, then reads every target
   with `describe-stacks --stack-name` in its Region until none is `*_IN_PROGRESS`, then removes
   the lock. Each pass is one `release_poll` line in `LOG_FILE`: `command_running`, the target in
   progress as `<stack>@<region>=<status>`, or the read error. A read error never counts as
   terminal.
5. Past 60 minutes, or after a signal during that wait with a process or target still running,
   keeps the lock and logs `LOCK_KEPT` with the last result (84).

**Lock log.** `LOG_FILE` is the custody record, so every append is checked. A failed append
prints `LOG_WRITE_FAILED <utc> log=<LOG_FILE> record=<record>` on stderr and the run ends 85. The
lock stays, because it is the state the log can no longer show: a missing `taken`, `targets` or
`checks ok` record stops the command from starting, and once the lock is taken no failed record
lets the wrapper remove it (`LOCK_KEPT reason=log_write_failed` on stderr). Every removal follows
the durable record that authorizes it: `release_approved` (the command never started), the
terminal `release_poll`, or `FORCE_RELEASE_APPROVED`. Only the record written after a removal
(`released`, `FORCED_RELEASE`) can fail with the lock gone, and that is still 85. Make
`LOG_FILE` writable, then use the stale-lock path below.

**Cancel.** Send `SIGTERM` (or `SIGINT` from your terminal) to the wrapper: it forwards `SIGTERM` to
the command's whole process group (`deploy-all.sh` and every `npx`, `make` and `cdk` under it),
waits for the command, then runs step 4. A `SIGKILL` skips all of this and leaves a stale lock,
and the command's group, outside the wrapper's, can keep running.
The command's group is not the terminal's foreground group, so a read from the terminal fails
rather than prompting; `deploy-all.sh` with `APPROVE=1` reads none.

**Per stack.** `scripts/diff-then-deploy.sh` (`make diff-then-deploy`) diffs or deploys one stack
and runs only under the lock. `MODE=diff-only` writes the gate-logged diff and the stack's asset
record to `EVIDENCE_DIR`. Read that diff, record the read with
`shasum -a 256 <DIFF> > <DIFF>.read`, then deploy with `MODE=deploy READ_DIFF=<DIFF>`. It deploys
only if the diff taken now equals the one read (80) and the daily ingestion work queues are idle
(82), then saves the stack's `describe-stacks` and its events since the deploy began. It is not for
a `src/` change: that restages the shared Lambda source asset for this one stack only, so a
`src/` change goes through [the deploy sequence](#the-deploy-sequence) instead
([ADR-136](../../decisions/adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md)).
`make deploy` of one stack takes no lock and reads no `SOURCE_SHA`, so it is not for a `src/`
change either.

```bash
(cd infra && npx cdk list --long --json --profile <PROFILE> -c env=<ENV>) > <CDK_LIST_JSON>
uv run python validation/dev_operations.py lock-targets --cdk-list <CDK_LIST_JSON> --env <ENV> \
  --expected-account <ACCOUNT_ID> --out <TARGETS_FILE>
bash scripts/deploy-locked.sh --profile <PROFILE> --region <REGION> --expected-account <ACCOUNT_ID> \
  --env <ENV> --lock-file <LOCK_FILE> --log-file <LOG_FILE> --targets-file <TARGETS_FILE> \
  --source-sha <COMMIT> --label <LABEL> -- \
  make diff-then-deploy PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  STACK=<STACK> EVIDENCE_DIR=<EVIDENCE_DIR> MODE=diff-only
```

### Stale lock: `make deploy-lock-force-release`

A lock is never released automatically once its owner is gone. When `dev-state` lists a
`stale_lock`, confirm nothing that lock covered is still running, then:

```bash
make deploy-lock-force-release PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT_ID> \
  LOCK_FILE=<LOCK_FILE> LOG_FILE=<LOG_FILE> OWNER_PID=<PID_THE_LOCK_NAMES> REASON='<why it is stale>'
```

It removes the lock only when the lock names `OWNER_PID` and that process is gone (a check that
fails for any reason but "no such process", such as a process you may not signal, is 83
`owner_unknown`); no process of
the `command_group` the lock records is left (otherwise 83 `command_group_alive`: that is the
abandoned deploy, still running; a lock without one never started its command); the account,
environment, Region and targets the lock recorded equal `ACCOUNT`, `ENV`, `REGION` and the targets
written now from your source and `.env`; the caller identity matches; and no recorded target is
`*_IN_PROGRESS`. Run it from the source and configuration the abandoned deploy used: a different
target set (a stack added, or one omitted) is refused before any read. It logs
`FORCE_RELEASE_APPROVED` with those checks, removes the lock, then logs `FORCED_RELEASE`; an
approval it cannot write leaves the lock unchanged (85). Otherwise it changes nothing and exits
83, 76 or 77. A lock that records no
targets is refused (83).

### `make check-alarm-window`: `validation/dev_operations.py alarm-window`

**Purpose.** Reads the current state and the `StateUpdate` history since `SINCE` of every
`adlytics-<ENV>-` alarm, one alarm at a time. `EXPECTED` names the alarms the step is expected to
move (comma-separated, or `none`); a name the prefix does not list is refused (2, `USAGE`). An
expected alarm in `ALARM` now is timed from when it entered `ALARM`, which can be before `SINCE`; a
read that omits that time is `READ_FAILED` (2). One `OK` now has recovered, whatever its last
history row shows. An alarm not in `EXPECTED` that is in `ALARM` now stops the check (3,
`UNEXPECTED_ALARM`, listed under `standing_unexpected`) even when it entered `ALARM` before
`SINCE` and so has no row in the window. An optional `DISPOSITIONS` file (the `make dev-state`
one) contributes only its owned entries. Each is recorded under `out_of_scope` with its state
and its transitions in the window, and stops the check only in a state its entry does not list.
An owned name the prefix does not list, or one also in `EXPECTED`, is refused (2). These refusals
and the two above come from the current states alone,
before any history is read.

**Expected output.** One `RESULT {json}` line and `<EVIDENCE_DIR>/alarm-window-<utc>.json`, listing
every transition with the unexpected ones marked, and each expected alarm's current state.

### `make check-serving-refusals`: `validation/dev_operations.py serving-refusals`

**Purpose.** D2 ops check O3 ([ADR-139](../../decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md)),
run after the last bind of an acceptance window. Read-only, under the operator profile, STS-checked.
It resolves the pointer and graph tables from the Presentation function's environment, strongly reads
every roster row's `CURRENT` and each grant-bearing pointer's `SERVING#<grant>/BINDING`, and applies
the one serving predicate. It sums `PresentationServingRefused` since `SINCE` with the
`PresentationServingRefused` alarm's own Metrics Insights query (`past_settle`), and the same query for
`within_settle`, one paginated `GetMetricData` call each on the alarm's 60-second period. It reads both
serving alarms, and counts `operation refused` lines by operation and reason (diagnostic, at most 100
rows). `GetMetricData` runs a Metrics Insights query over at most the last two weeks, so a `SINCE`
older than that is refused (2) before any read, and any query result, on any page, whose status is
not `Complete` (missing included) or that carries a message is `NOT_PROVEN`. So is a read with no
`refused` result at all, or a result with any other id: an absent result is never read as zero.

```bash
make check-serving-refusals PROFILE=<PROFILE> ENV=<ENV> REGION=<REGION> ACCOUNT=<ACCOUNT> \
  SINCE=<ISO8601> EVIDENCE_DIR=<ABSOLUTE_DIR>
```

**Expected output.** One `RESULT {json}` line and `<EVIDENCE_DIR>/serving-refusals-<utc>.json` with
`verdict`. `PASS` (0) only when every grant-bearing `CURRENT` is served, the `past_settle` sum is 0
and neither alarm is in `ALARM` (a new alarm's `INSUFFICIENT_DATA` passes). `FAIL` (3) lists the
unserved keys with their failure entries, the sums and the alarm states. `NOT_PROVEN` (2) names the
failed read, or an alarm that does not exist. The `within_settle` sum is recorded; treat non-zero as a
defect.

**Safety notes.** Read-only: `lambda:GetFunctionConfiguration`, `dynamodb:GetItem`,
`dynamodb:TransactGetItems`, `cloudwatch:GetMetricData`, `cloudwatch:DescribeAlarms`,
`logs:StartQuery`, `logs:GetQueryResults`. No runtime IAM.

### `make check-source-key`: `validation/dev_operations.py source-key`

**Purpose.** Records every listed stack's synthesized function keys from `infra/cdk.out` (the
probe runner's `--record-assets` record, one file per stack in `EVIDENCE_DIR`) and compares them
with each deployed processed template, read in that stack's own Region. Run it from the source
that was deployed.

**Expected output.** One `RESULT {json}` line and `<EVIDENCE_DIR>/source-key-<utc>.json`, naming
the source keys and any unequal or incomplete stack.

**Safety notes.**

- Only `deploy-all-locked` and `diff-then-deploy` change anything; every other target is read-only.
- The records name stack, alarm and queue names and the account's caller identity. Keep them in
  ignored evidence, never in a tracked file.

## Appendix: fresh-environment deploy and destroy, 2026-10-19

A customer-zero check of the README's deploy path, run before publication: a throwaway environment
deployed with `make deploy-all` and removed with `make destroy-all`, following only the README and
the docs it links. It records each step once; the README owns the procedure. Times are UTC.
Evidence (logs, stack events, the page-walk record and screenshots) is operator-local and
unpublished, in ignored task custody.

**Limits of this account, stated honestly.** The account runs the development environment too, so
it was already CDK-bootstrapped (bootstrap version 32, equal to the CLI's) and already had Bedrock
access to the three model IDs `.env.example` ships. `deploy-all` reported "already bootstrapped".
Both steps are **[unverified-from-zero]**. The README gives both: the bootstrap is automatic in
`deploy-all`, and the model IDs and how to enable them are in Prerequisites. The page walk used an
empty data lake, as a new customer's is, so the legacy bake path that customer deployments run and
the DSP precompute admission are **[unit-proven, not live-validated]**: the bake ran to its terminal
outcomes (36 unavailable, 36 completed, 0 dead-lettered) but published no data.

### What earlier runs found

| Run | Environment, source | Result | Fixed by |
|---|---|---|---|
| 1 | `pubcheck1019`, `b5792efc` | `deploy-all` failed at `Adlytics-Ingestion`: the projection handoff refused to bootstrap a new environment to the default `QUEUED_ACTIVE` state, and its failed create then could not roll back or be deleted. Three untagged Glue databases were left behind unseen. | ABSENT → `QUEUED_ACTIVE` bootstrap and a no-op delete of a failed create ([write discipline](../load-path/write-discipline.md)); `destroy-all` lists the retained Glue databases; README Clean up row |
| 2 | `pubcheck1020`, `2dcb5894` | `deploy-all` passed (22 stacks, about 39 minutes). On the empty lake, Scorecard and New customers showed an error instead of their designed absence; chat answered a data question with a currency refusal; the chat panel failed axe heading-order; Campaigns and Retail health offered "another period" when none existed; one tamper alarm fired on the first deploy. The README's audit-table clean-up step could not be done with plain IAM. | Designed absence ABS-SC-1 and ABS-NC-1 on an empty lake, a governed nothing-published refusal, heading re-levelling and absence copy ([page absence copy](../../ui/page-absence-copy.md)); README alarm and audit-table rows |

### Run 3: `pubcheck1021`, accepted

1. **Source.** Fresh `git clone --branch release/publication-2026-10-19-r3` from GitHub into an
   empty directory: HEAD `876f2a38`, tree `aeb02706`, no ignored or untracked file. The branch was
   later fast-forwarded to `7892e875`; `git diff --stat 876f2a38 7892e875` is one file,
   `frontend/test/campaign-portfolio-view.test.tsx` (5 insertions, 1 deletion), so the deployed
   runtime equals the release tree's.
2. **Shell.** Every customer command ran under `env -i HOME="$HOME" PATH="$PATH" AWS_PROFILE=<profile>
   AWS_REGION=us-east-1`, with no `ADLYTICS_*` variable, no operator environment file and no
   operator `node_modules`, `.venv`, `cdk.out` or `validation/.env.local`. Tools: Node.js 24, uv,
   AWS CLI v2, `make`, `file`.
3. **Pre-deploy reads** (read-only, after `aws sts get-caller-identity`). Nothing existed under the
   environment name: 0 stacks, 0 SSM parameters, 0 Glue databases, Cognito domain prefix unused.
   Lambda concurrent executions 1000 (README minimum 135). CloudWatch Logs resource policies 2 of
   10. Athena "Active DML queries" 200, equal to `active_dml_quota`. Every SSM parameter the stacks
   read is under `/adlytics/<env>/`, written by a stack of the same deployment, so no
   operator-made parameter is needed.
4. **Configure.** `cp .env.example .env`, then the one blank, `ADLYTICS_LOGIN_DOMAIN_PREFIX`, set to
   a new prefix. Nothing else was edited.
5. **Diff.** Run 2 ran `make deploy-all PROFILE=<profile> ENV=<env>` without `APPROVE`: rc 0, 22
   stacks, 779 additions, nothing deployed, about 4 minutes. Run 3 changed no `infra/` file and
   skipped it.
6. **Deploy.** `make deploy-all PROFILE=<profile> ENV=pubcheck1021 APPROVE=1 EMAIL=<address>`,
   12:47:09–13:29:24 (42 minutes), **rc 0**. It took the first-deployment branch, created all 22
   stacks, attached the API origins in a second Frontend pass, published the frontend, invited
   the user and printed the URL. The handoff Prepare and Commit resources completed.
7. **Real deploy, not a diff.** The diff pass prints the same banners, so `make check-source-key`
   ran from the clone: rc 0, one source key across all 22 stacks, none unequal or incomplete.
8. **First user.** The invite was issued: Cognito shows the invited address in
   `FORCE_CHANGE_PASSWORD`. Reading the emailed temporary password and setting a new one is
   **[unverified]**: it needs a human inbox.
9. **Page walk (operator test-only sign-in, not a customer step).** `scripts/create-user.sh` never
   sets a password, which is correct for customers, so the walk used a separate synthetic user,
   created as `validation/provision_quality_review_test_users.py` does: invite suppressed,
   permanent random password held only in memory, in no group, like the invited user. It signed in
   through managed login (13:30–13:32).

   | Page | Designed state shown | Status regions | axe |
   |---|---|---|---|
   | Campaigns (and `/`) | Absence: "Campaign reporting isn't ready to show yet…" | 1 | 0, light and dark |
   | Scorecard | ABS-SC-1 | 1 | 0, light and dark |
   | New customers | ABS-NC-1 | 1 | 0, light and dark |
   | Reach and funnel | ABS-REACH-1 | 1 | 0, light and dark |
   | Audiences | ABS-AUD-1 | 1 | 0, light and dark |
   | Retention | ABS-RET-1 | 1 | 0, light and dark |
   | Retail health | Absence: "Retail Health isn't ready to show yet…" | 1 | 0, light and dark |
   | Prompt administration | "Not authorized" (the user has no group) | — | 0, light and dark |

   Every page returned 200 and settled in under 1.5 seconds. There was no 5xx, no page error, no
   `ADLY-UNEXPECTED`, no page issue and no lingering skeleton. axe is not full WCAG validation.
10. **Chat.** "What metrics can I ask about?" listed the metric catalog. "What was my total ad
    spend over the last 7 days?" answered "No ad spend data has been published yet. Figures will
    appear once your data producers deliver the source data and it's published to the platform."
    The measurement tool logged the governed refusal `ADLY-PUBLICATION_SELECTION-0001`. The panel
    had 0 axe violations.
11. **Queues and alarms.** All 9 failure and bake queues were 0, and the preset-bake dead-letter
    alarm was OK. `record-control-plane-changes`, `record-delivery-failures-self-events` and
    `record-function-changes` went to ALARM during the first deploy and back to OK within 15
    minutes on their own, as the README says. After 13:39 two of them fired again, matching a
    change the CloudFormation execution role made to the development environment's evaluation
    result group in the same account. That is the shared-account co-tenancy the README does not
    support, not a customer-deployment behaviour.
12. **Destroy.** `make destroy-all PROFILE=<profile> ENV=pubcheck1021 CONFIRM=pubcheck1021`,
    13:43:55–14:09:48 (26 minutes), **rc 0**, "Stacks of pubcheck1021 still present: none". It
    printed the retained, tagged resources and the three Glue databases by name.
13. **Retained resources, removed by hand** (14:10–14:11, AWS CLI, each after the identity check),
    as README Clean up describes: the Cognito domain and user pool, with both users; the client
    secret; the review-token function and its log group; the AgentCore memory; the agent runtime
    log group; 7 DynamoDB tables; 4 S3 buckets, emptied of every object version first; the 3 Glue
    databases.
14. **Left in place on purpose.**
    - The two tamper-protected audit tables, `adlytics-pubcheck1021-agent-quality-audit` and
      `-findings`. They hold 0 items, have deletion protection on, and cost close to nothing. README
      Clean up keeps them by design and gives the optional removal.
    - The audit log group `/adlytics/pubcheck1021/agent-quality/reviewer-access`, which has
      deletion protection on. README Clean up says to turn it off first; it was not turned off.
    - The token-signing KMS key, which CloudFormation scheduled for deletion on 2026-10-26 (the
      KMS waiting period).

    No other stack, table, bucket, Glue database, SSM parameter, user pool, secret or alarm of the
    environment remained at 14:11.
