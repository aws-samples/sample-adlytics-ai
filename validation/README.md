# Validation

This directory owns two different evidence classes. **Source validation** probes external AMC
behavior to settle disputed source facts. **Deployed acceptance** probes this repository's deployed
Semantic V2 path against exact synthetic evidence. Reusable scripts and sanitized conclusions are
publication content; raw output, credentials, journals, and exact operational identifiers are local
only under ignored `findings/`, `evidence/`, `RESULTS.local.md`, and the repository's `.agents/`. Neither class is part of the
application runtime or `make verify`.

The direct-Runtime endpoint evaluator and its V4 scenario corpus were retired with the custom Runtime; [evaluation/README.md](../evaluation/README.md) and the superseded [ADR-057](../docs/decisions/adr-057-evaluate-endpoint-behavior-with-a-versioned-scenario-harness.md) record that. The deployed `Adlytics-EvaluationV4-<ENV>` stack is a different thing: it holds the online grounded-correctness, internal-consistency, and helpfulness evaluators that score managed Harness turns, last recorded as deployed in dev on 2026-09-30 in [RESULTS.md](RESULTS.md).

## Why this is a root directory and not part of the application

**The platform never calls AMC at runtime.** The producer that operates extraction, outside this repository, lands
files in S3; this repository's scope starts at the immutable landed object ([Data Layer](../docs/capabilities/data-layer/README.md)).
AMC API credentials are therefore a source-validation dependency only — no Lambda holds them, no
stack grants access to them, and nothing in `infra/lib/config` references them. Adding them to a
runtime stack "for later" would build a capability the architecture says we do not have.

**The deployed acceptance probe uses AWS, but not AMC.** It lands a synthetic Vendor Central traffic
delivery into an explicitly named deployed environment and observes the V2 publication boundary.
It requires an explicit AWS profile and never reads `validation/.env.local`.

**Both classes are outside every gate, deliberately.** `make verify` does not lint or typecheck this
directory: `ruff` and `pyright` are scoped to `src` and `tests`, and `check-config-access` scans `src`
only, which is why reading validation-only credentials from the environment is permissible here and
nowhere else. The trade is real: no lint or typecheck covers a change here, and there is no remote CI, so read it
before running it.

**The cost of that trade, stated plainly.** Findings are not traceable through the `covers()` marker,
and nobody will re-run these automatically. AMC publishes no changelog and its sitemap `lastmod` is
a uniform build stamp, so page drift is undetectable. Deployed acceptance is also manual because it
requires an explicit target environment.

## Inventory

| Class | File | What it does |
|---|---|---|
| Synthetic contract fit | [conversation-flow-contract-fit.md](conversation-flow-contract-fit.md) | Approved fictional page and conversation specimens proving accepted contract fit only; never runtime, deployment, browser, or acceptance evidence |
| Synthetic method prototype | [response-curve-prototype.md](response-curve-prototype.md) | Tracked summary of the offline response-curve, Chronos-2 baseline and simulator prototype on generated fictional data, with the main criteria's results and the hosting projection; the prototype code and raw outputs stay local; never source, hosted-run, or acceptance evidence |
| Source validation | `amc_client.py` | Minimal Ads API / AMC client. Stdlib plus boto3, no new dependency. Built to be diagnostic: every failure prints its status and body |
| Source validation | `mint_token.py` | Interactive refresh-token mint. Needs a human at a consent screen, so it is a script and not a test |
| Source validation | `probe_amc.py` | Discovery, then external-source probes. Writes JSON to `findings/`, which is gitignored |
| Source validation | `probe_suppression_nulls.py` | Narrow live AMC probe for per-column suppression nulling |
| Source validation | `probe_amc_query_packs.py` | Durable recovery-first execution and exact CSV conformance for governed packs |
| Source validation | `probe_reach_suppression.py` | Distinct-reach behavior under suppression and duplicate nulled grain detection |
| Source validation | `probe_deduplicated_attribution.py` | Recovery-first conversion deduplication plus temporal first-touch, last-touch, linear-credit, and currency-scoped revenue reconciliation |
| Deployed Agent Quality chain | `probe_agent_quality_loop.py` | Controlled authenticated turns whose receipt, findings, and evaluation chain are read through `POST /api/quality-review/turn` under the `reviewer` persona's bearer, with deterministic processor-output and aggregate reads; raw SSE stays under ignored `evidence/` |
| Joined managed Harness acceptance | `accept_managed_harness.py` | Exact-stack deployment, custom-resource Harness and route-minted-token authorizer, and API-role attestation; every declared tool, one same-turn multi-tool question, and one no-tool turn through `/api/chat` as the `probe-a` persona with each tool call verified; BFF-session same-origin turns with bounded log/trace joins, receipt and evaluation reads through the review API, and distinct F-23 custody status; raw protected evidence stays ignored |
| Identity-chain probe (ADR-077) | `probe_identity_chain.py` | Case-table probe run as the `probe-a`/`probe-b` personas: `--case p1` proves the route-minted token, reached through `/api/chat`, is the only working Harness path (the persona's own Cognito access token, an ID token, a token signed by an unpublished key, and SigV4 on the Harness and its runtime refused, each after its positive control and with no Memory side effect), and that the route exchange refuses an administrator-class operator by its resource-policy Deny; `--case p5` proves cross-conversation Memory recall for one user and none for another; `--case s5` proves a persona tool turn's every tool call verified its caller, then that the Gateway authorizer refuses a missing, Cognito, foreign-key, or Harness-audience bearer with no tool invoked, that each tool Lambda invoked directly with a fabricated Gateway context refuses `ADLY-CALLER-0001` before routing, that the route exchange and the agent client secret refuse the operator by their resource-policy Denies, and that `/token` refuses a wrong client secret. Prints expected vs observed per case; sanitized evidence stays under ignored `evidence/identity-probes/` |
| Identity-chain latency (ADR-077 P4) | `probe_identity_latency.py` | Drives sequential persona tool turns through `/api/chat`, then reads the route exchange's, `/token`'s, and the Gateway interceptor's `REPORT` lines in the run window and judges warm p50 and cold max against the budgets the file declares; PASS/FAIL/NO-SAMPLE per budget, non-zero exit on any FAIL; sanitized evidence stays under ignored `evidence/identity-probes/` |
| Turn latency breakdown | `probe_turn_latency.py` | Runs the committed six-prompt `fixtures/chat-bake-acceptance-v2.json` manifest, filled from the execution's `--outside-selection-receipt` (new-conversation and follow-up turns, with and without a governed tool, plus the out-of-list campaign control) through `/api/chat`, spaced apart; joins each turn's client arrival times, route timing line, route-trace and tool-window spans, the Harness runtime's own spans from `aws/spans`, kept only when they carry the turn's trace ID and the Harness's service name, its metrics and session start-up lines, and Lambda `REPORT` lines into a per-hop breakdown; prints milestones, durations, and token counts only; correlations and released text stay under ignored `evidence/turn-latency.*/`. With `--phase`, it also writes a D8 six-row paired-chat draft that `--bind-draft` binds into `ChatLatencySampleV1` after the bake |
| Route Guardrail calibration and acceptance | `probe_guardrail_calibration.py` | Replays the fictional question set in `guardrail-calibration-cases.json` through `ApplyGuardrail` with `source=INPUT`. Default mode compares the deployed guardrail with a temporary Standard-tier copy it creates and deletes, deriving each case's verdict at every prompt-attack strength. `--accept` (`make accept-guardrail`) replays the deployed guardrail only and fails unless it is Standard tier, ran all 54 cases, blocks no benign case, and misses at most one attack, a jailbreak. Prints verdicts, filter names, confidences, and case families only; never the matched text |
| Reprocess telemetry acceptance | `probe_reprocess_telemetry.py` | Dev-only dry-plan/apply orchestration joined to exact lifecycle and exported business-span hierarchy acceptance; raw plans, process output, deployment records, and span documents stay under ignored `evidence/` |
| Deployed AMC cadence acceptance | `probe_amc_current_invariants.py` | Read-only dev acceptance fenced to transactionally attested exact `CURRENT` publication IDs: governed campaign population, daily exposure coverage, click/impression bounds, and conversion/zero-conversion identities |
| Deployed campaign artifact acceptance | `probe_campaign_artifact_store.py` | Read-only dev attestation of campaign parent, immutable tail segments, custody, pagination, and exact sibling reads for every declared window |
| Ingestion traceability and P6E-3 | `probe_ingestion_traceability_gate.py` | Read-only graph-authority acceptance; P6E-3 is decided only by the observed named cohort and its in-run queue metric, never by the retained serial estimate |
| D7 rolling-freshness control | `run_graph_rolling_freshness_control.py` | Dev-only driver for the mandatory single-root and A/B control over `fixtures/graph-rolling-freshness-control-v1.json`. It dry-plans each entry twice, reserves exact keys serially, and launches twin-only children of the real `land_fixtures` CLI as argument tuples. It writes one atomic `RollingFreshnessControlV1` receipt, `NOT_PROVEN` on any failure, under ignored `evidence/`. The optional 98-root plan is synthetic load characterization only |
| Ingestion drain observation | `probe_ingestion_drain.py` | Read-only exact-stack queue/lifecycle/performance observer with fail-closed error and DLQ detection; raw samples and aggregate output stay under ignored `evidence/` |
| Isolated ingestion latency diagnostic | `probe_ingestion_end_to_end_latency.py` | Dev-only single-delivery measurement from empty ingestion queue through exact semantic `CURRENT`, followed by explicitly non-authoritative bake-queue observation; raw cutover output stays under ignored `evidence/` |
| Deployed acceptance | `probe_ingestion_cutover.py` | V2-only synthetic ingestion acceptance: immutable landing, exact attempt success, exact `CURRENT` advancement, and unchanged DLQ depth |
| README screenshots | `capture_screenshots.mjs` | Drives a deployment loaded with synthetic data as a logged-in user in the light theme and writes the README gallery to `docs/images/`. Every chat image is a scripted fixture served in the browser, so no question reaches the deployed agent. Never mutates the deployment; fails on a missing surface or past the 6 MiB image budget; prints image names and sizes only |

## Running deployed AMC cadence acceptance

`probe_amc_current_invariants.py` is a read-only dev gate for the fictional customer-cadence
landing. It refuses any target other than the exact declared dev region, Data V2 stack,
marketplace, and fixture window before constructing an AWS session. Its first AWS call verifies the
explicit profile account. It resolves only exact stack-owned Semantic V2 parameters, derives every
daily partition through the same governed source-pack and publication-scope code used by serving,
and transactionally attests each `CURRENT` pointer with its immutable publication record.

Every Athena statement is fenced to those attested publication IDs. Acceptance requires every
governed campaign identity/channel pair in the append-only dimension history and reports retained
historical dimension aliases separately. Current exposure and conversion facts must contain no
extra identities, daily exposure must be complete except for the one declared gap, no campaign-day
may have clicks above impressions, conversion coverage must be complete for eligible campaigns,
and the declared zero-conversion cohort must remain exactly absent. Standard output contains only
bounded counts and classifications, never publication IDs, source identifiers, rows, or
measurements.

```bash
uv run python validation/probe_amc_current_invariants.py \
  --profile <profile> --environment dev --region us-east-1 \
  --expected-account <approved-dev-account> --data-stack Adlytics-DataV2-dev \
  --marketplace-key amazon_us \
  --window-start 2026-08-10 --window-end 2026-09-15
```

A passing command establishes only the exact deployed fictional cadence state it read. Record a
dated sanitized conclusion in [RESULTS.md](RESULTS.md); keep operational identifiers and raw query
evidence local and ignored.

## Running ingestion traceability P6E-3

P6E-3 is the approved rolling-freshness business acceptance, never a UTC calendar-day projection. The prior `47,258`-second result was a pre-coalescing serial estimate, not a deployed observation. The tracked provenance conclusion is that one root is one exact immutable manifest-version flow and the old 97-root gate came from one observed `2026-10-06` SHADOW cycle against 98 planned deliveries, not users or proven concurrent business traffic. The direct retained count artifact has SHA-256 `d3374dc50fb33b3699973e8ca4794ab8247da322862c5f5f8347db75ac6b09e7` and supports 98 roots on 53 of 58 listed days. Its compressed barrier replay is synthetic. Owner decision D7 removes day admission rather than repairing it, and the owner ratified the revised acceptance and named infrastructure scope on `2026-10-14` UTC. Live acceptance `graph-accept-20261016-x9` (dev, 2026-10-16 UTC, runtime `96cbd9e7`, probes `fa69b3df`) passed criteria 1-9, and the GRAPH hold was cleared at 06:55:43Z; its receipt is the deploy-tail `deployment-acceptance-receipt.md` in the local evidence tree. The paragraphs below state each approved rule and the result the live runs recorded; the outcome, the final hold state and the post-clear regression check are in "Rolling-freshness live acceptance and hold clear" below.

`validation/run_graph_rolling_freshness_control.py` requires explicit profile, dev environment, region, account, data stack, bucket, graph-store stack, acceptance execution ID, manifest, and ignored evidence directory. It invokes the real `adapters.jobs.land_fixtures` CLI with deterministic operation IDs and all required arguments, dry-plans and serially reserves every exact probe slot before launch, rejects duplicate labels/sources/windows/keys or exhausted slots, and parses exactly one admitted/replayed `PROBE_TWIN` run ID per child. Any missing, refused, duplicate, malformed, or plan-drifted child writes `NOT_PROVEN`. In x9 the mandatory hold-clear control ran one exact root end to end (664.9 s), launched wave A once that root held its grant, and launched wave B only after wave A's immutable snapshot was strongly `ACTIVE`; the control passed. The separate live 98-root barrier mode is optional, non-blocking, and labelled synthetic load characterization; the x9 receipt records no run of it.

The rolling publication implementation allows every B flip and settlement while A remains active by advancing only the live frontier; A's contributors, aliases, elected frontier and candidate digest remain immutable, and B never joins A. A's final alias and B's activation are one replay-safe fixed-member DynamoDB transaction: 51 actions at both the 98-candidate offline profile and the 512-candidate protocol maximum, each at most 1,000,000 aggregate oracle bytes (measured totals in the fixed-member table below), with a 42-action no-eligible branch. The offline gate proves that chronology plus transaction-conflict, timeout-before-commit, lost-response/equal-replay, no-successor, racing-arrival, and action/byte one-beyond controls. Live in x9, wave A elected as one cohort; wave B flipped and settled at 06:21:50.86–06:21:51.22Z while the head was `ACTIVE` v37, and A's final-alias drain at 06:22:45.37Z elected B. Every mandatory single-root/A/B root and the whole control cycle stayed at most 7,200 seconds from `root_clock_started_at` to `terminal_at`: the waves' maximum root was 1,656.5 s and the cycle 1,720.5 s.

The one-time dev migration ran on 2026-10-15 (UTC). Stage-1 pin `87af5efb` deployed a narrow fence check to every old release-store and TableWriter/dispatch serving writer (lock released 15:24:12Z, source key read back). `MIGRATION#GRAPH_ROLLING_FRESHNESS/FENCE` was set at 15:24:36Z while GRAPH revision 12 was held, and its strong readback matched. T0, the later of the deployment and readback timestamps, was 15:24:38Z; census A began at 15:45:44Z, 1,266 seconds later, so the 900-second wait held, and independent checks used the interval without shortening it. The complete queue, claim, head, and obligation idle proof passed at interval 0, at the boundary and after the census. Censuses A and B, with intervening strong authority reads, matched at SHA-256 `51c7386f…`, and only then was the no-day/no-wait runtime deployed; it does not read the fence as admission. The live delayed-writer control (receipt below) showed that a delayed pre-fence writer cannot mutate `SERVING_PUBLICATION#`, `PUBLISH_WAIT#`, `ARM#`, or siblings after the fence. Historical `SERVING_DAY#*/RESERVATION` and `CAPACITY_WAIT#*` rows remain inert and were not deleted, repaired, or mutated: the postdeploy census and x9's census are byte-identical to census B. `RuntimeAdmissionRetirementV1` keeps the complete broad runtime/generated/IAM/call inventory and adds `validation/probe_ingestion_traceability_gate.py` and `tests/unit/test_probe_ingestion_traceability_gate.py` as mandatory auxiliary AST roots with four function-qualified allowlists; every other legacy occurrence fails. At the accepted runtime `96cbd9e7` it scanned 625 files with 20 allowlisted hits and 0 forbidden.

The trace contract adds cohort revision to every cohort span, including source-pin, fan-out, precompute-plan and model-call, and generates one request-scoped promoter-pass span with one typed promoter-domain child per attempted cohort. `make check-trace-contract` validates the schema and generated projection; isolated failures mark the child before the single metric/log report. Criterion 6 passed on x9's retained evidence once the omission control targeted a pass that owns an attempt (`fa69b3df`): 25 of 25 controls were rejected.

General `athenaQueryBatchMaxConcurrency` remains configurable but must be explicitly 8 across deploy environment, synth, deploy and readback unless a digest-bound `AthenaAdmissionMeasurementV1` proves another value. This is separate from the accepted INSIGHT_PRECOMPUTE `2 × 4` bound; all seven static rows, CHAT reserve and separate CHAT/BAKE/INSIGHT_PRECOMPUTE workgroups remain unchanged. Missing, fallback, unproven non-8, or readback-mismatched configuration is `NOT_PROVEN`. Criterion 7 passed in x9 with the value 8.

Paired chat acceptance binds both six-row `ChatLatencySampleV1` artifacts to one unused acceptance execution ID and exact bake run/cohort, `bake.run` start, and final successful `bake.finalize` end. The baseline must finish after preflight and before first root launch; every during row must launch while the cohort is `ACTIVE`, and at least one request interval must overlap bake. The comparator uses exact nearest rank `sorted(values)[ceil(0.95*n)-1]` and passes only when during-bake p95 is at most 110% of paired no-bake p95 with the same persona/prompts/count/generation and zero errors, refusals, quota refusals, or throttles. Reused IDs, non-overlap, reversed timestamps, or unprovable telemetry are `NOT_PROVEN`. Criterion 8 passed in x9: during-bake p95 was 49,027 ms against a paired no-bake p95 of 49,924 ms, over the bake interval 06:16:50–06:21:54Z. The outside-list control is a real served campaign. `probe_turn_latency.py --select-outside-list-item` selects it by rule `lexically-last-served-campaign-v3`: a CHAT-workgroup query for campaigns with impressions in the target unit's window, plus the chat tool's own catalog browse. The selection writes one execution-scoped receipt. Both samples and the verdict bind to that receipt by digest, query ID and target unit, and the verdict re-reads the Athena query. `--accepted-generation` is the 64-hex `serving_generation(grant_id)` digest of the chat target unit's DIRECT binding. The bounded-list absence leg moved to D2 live acceptance (owner, 2026-10-15); GRAPH samples record it as not measured (`MOVED_TO_D2`). The earlier fixed `campaign:bulk-estate-1000` (fixture v1) is absent from the dev lake, so v2 replaces it (2026-10-15).

### Rolling-freshness live acceptance and hold clear (2026-10-16 UTC)

Dev only (the approved dev account, us-east-1). `main` and production were not touched. The canonical
receipt is the ignored `.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/deploy-tail/deployment-acceptance-receipt.md`.

- **Deployed.** Stage-1 pin `87af5efbb12581908233db041154e9273b80350c` (2026-10-15), then the
  no-day runtime, last redeployed as `96cbd9e713a9bb98077baae8a862ecf9ea420cfc` on 2026-10-16
  03:23:56–03:37:49Z with source key, census preservation, retirement and deployed-IAM readbacks
  passing. Probe tooling `fa69b3df` changes nothing under `src/`, `contracts/` or `infra/`. The
  criterion-2 control ran at the test-only stage-1 commit `7307f1b1`.
- **Accepted.** `graph-accept-20261016-x9` passed criteria 1–9. Criterion 2 passed under the
  owner-ratified amendment. Criterion 6 and the hold-clear matrix passed on re-evaluation of x9's
  byte-identical retained evidence at `fa69b3df`; the original write-once receipts are unchanged.
  The x8 control verdict was also re-evaluated, from NOT_PROVEN to PASS, on its own raw
  observations. x1–x8 stay recorded as NOT_PROVEN.
- **HIST-17 alarm scope (2026-10-16).** The final review found that the terminal dev-state check
  matched dispositions by name only. It accepted `adlytics-dev-evaluation-grounded-correctness-worst-label`
  in `ALARM`, where its disposition allowed only `INSUFFICIENT_DATA`. The alarm-window check also
  missed an alarm already in `ALARM` when the window opened. Both checks are now state-aware and
  fail closed. Under the dated amendment to HIST-17 in the local `acceptance.md`, the three
  evaluation worst-label alarms are out of GRAPH scope. They measure chat answer quality, and D2
  chat acceptance plus the evaluation loop own them. Every receipt and the HIST-17 row record each
  of them with its actual state. On re-evaluation, x9's HIST-17 and the hold-clear matrix pass with
  every GRAPH-scope alarm clean. The grounded-correctness alarm is in `ALARM` because the evaluator
  grades capability-only greetings Unsupported against empty evidence. That is a D2 follow-up; no
  figure fabrication was found.
- **Fresh-election guard re-proven at the deployed runtime (2026-10-16).** The 10,493 ms
  `next_election_min_remaining_ms` was measured at `c1742f14`, before `750a1697` stamped the root
  clock on two members of the 51-action transaction and before promoter tracing with its 2 s
  bounded flush. It stays unchanged: x9's own evidence at `96cbd9e7`, under the calibration's rule
  `ceil(2 * max)` plus the unmargined 2,000 ms flush bound, needs at most 7,733 ms. The bases are
  the slowest final-drain `promoter_domain` span (1,360.2 ms, which wraps the guard check through
  the transaction; 4,721 ms needed), the slowest of 49 `promoter_pass` spans (2,619.8 ms; 7,240 ms)
  and the slowest of 63 Lambda `REPORT` durations, flush included (2,866.3 ms; 7,733 ms). Two
  sequential 2 s exports would still need only 9,733 ms. x9 had two final drains, each one
  iteration; the calibration's 45 operator-host attempts (max 5,246.3 ms) remain the larger
  measurement. The receipt is the ignored
  `final-fix/fresh-election-guard-recheck-96cbd9e7.json` (SHA-256 `34fffbeb…bd1f`).
- **Final hold state.** GRAPH revision 12, `hold=false`, `hold_revision` 2, strongly read back at
  06:55:49Z after `clear-hold` committed at 06:55:43Z. The fence row is retained. No held waiter
  existed at the clear, so none was admitted. After the clear the graph table read head `IDLE`,
  0 flows, 0 waiters and 0 non-terminal runs (06:57:54Z), and `make dev-state` passed (06:58:51Z).
- **Post-clear Q10 bake regression check.** One run, `c50aee4e`, 07:08:17–07:19:08Z, used the
  frozen benchmark fixtures, phase definitions and extraction code, and was admitted on the
  normal path. Compared with the baseline `69e71e5d` and its reproduction `af23d37e`:

  | Metric | Baseline | Reproduced | Regression run |
  | --- | --- | --- | --- |
  | `bake.run` to FINALIZED | 353.8 s | 352.3 s | 352.6 s |
  | Seven-task makespan | 75.8 s | 65.0 s | 83.1 s |
  | Athena executions / peak in flight / admission wait | 302 / 20 / 0 | 302 / 20 / 0 | 302 / 20 / 0 |
  | Trace completeness, controls rejected | 302/302, 3/3 | 302/302, 3/3 | 302/302, 3/3 |

  The makespan is a LOW, non-blocking finding: total task busy time was 93.7 s, within the
  baselines' 88.4–109.4 s, and the extra time was Baker lane idle time while one container ran
  three tasks in series for about 47 s. The queue-to-Lambda configuration was unchanged (read
  2026-10-16). The cause of the dip is not established. Bake-to-serving time stayed flat because
  the fixed 180 s finalize redelivery absorbed it. The run was not reproduced, because it was
  healthy on every other measure.

### Rolling-freshness focused design-repair receipt

The focused repair was authored against clean pin `425cd4ac87350b0c831430f352b1fa7f553e5117`. Canonical ignored verification is `.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/design-repair-source-verification.md` (SHA-256 `fb57269716b21862bb0f784b31361b69737950f0c58f4ebbc6426bd1088fecc6`); check output is `design-repair-checks.md` beside it (SHA-256 `b1695a0873c610a4b8371782e9a7995beed346b821f78926ec2ad37ef0e95ece`). `make check-links`, `make check-trace-contract`, and `git diff --check` passed before that commit. Those are design/contract checks, not implementation, deployment, or live acceptance evidence.

The finalization repair began from clean `feat/graph-bake-delivery` tip `40a03d159418eaf85af786dde59ab9142afa9757` and records the `2026-10-14` informed owner ratification, fixed-member transaction/oracle proof, complete 900-second migration fence, auxiliary retirement roots, finalized infrastructure authority, finalize-field map, and acceptance criteria 1–9. Its current check output remains in ignored custody, and its completion receipt, `.agents/tasks/ingestion-traceability/graph-execution-chain-design.md`, remains in ignored operator-local custody. GRAPH revision 12 stayed held through that repair; the hold was cleared on 2026-10-16 after x9.

### Rolling-freshness GRAPH preflight receipt

Fresh run `2026-10-14T21:49:06Z`: **PASS**. Canonical raw evidence remains ignored at
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/preflight.md`
(SHA-256 `8ce42369f4f557840ef87fd5bc2e53db095f686e22d817752ee9151d3d6b7684`) and
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/preflight.json`
(SHA-256 `baf62fd99103803cce043e1f1362028ffa01cf83c574ce3c196607cf175d7a04`).

### Rolling-freshness FEAT-002 local commit receipt

Local verification only; nothing was deployed. Parent: original stage-1 commit
`a44951788d7a9cd4521564f2304ca2d160896b8a`. The stage-1 deploy pin, deployed 2026-10-15, is `87af5efb`: the
pyright typing fix plus the probe-name inventory row; see the stage-1 pin note after the
FEAT-002b receipt below. The verified staged tree was
`6805bd4ce0a041367bd5f02bb790b6825f01d821`; the commit adds only this receipt to it. Each command ran
once against a clean export of that tree. Raw logs remain ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/feat-002-commit/`.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| `make generate-execution-graph-protocol && make check-execution-graph-protocol` | pass; generated projections unchanged | `b612fb21ce87a1e010b7468a591cad035bb1c0b9b0cbf46c58a6308a6518d115` |
| FEAT-002 handoff pytest list plus operator, dispatch-store, table-writer and unit-batch-store suites | 1,012 passed | `112995565605eee53641eabce03372a33a073067228534783b38908c24054de2` |
| Jest `ingestion-stack`, `graph-call-inventory`, `control-plane-authority-stacks`, `publisher-stacks` | 92 passed (the pre-fix baseline had 7 inventory/IAM failures) | `5ddc36f18a4d8e27925cb61ca53222e7abf20f7946cd1abcfa5f74d3534342e7` |
| `npm run typecheck` | pass | `29e13a4e236649719a890bbfe69edae179312a03bd554db6a3bb13268d67db04` |
| `ruff check src tests`, `ruff format --check src tests`, `make lint-boundaries` | pass | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`, `2a31b861f353d42f14e50e34ae21ed01f792f7a58519d4fbebdb1f19675913b7`, `c489dc4fd5d3da3abb92cd8a02022d0273ce8b05e782f3f129fa78079ced3e6a` |
| `pyright` | 1 error, in stage-1 code unchanged since `be664a8d` (`execution_graph.py:318`) | `85d397fb7cff2c86f2addfd9eee9b31b46be90ec319788d183cc49d47c98d805` |

The final IAM and call inventory no longer grant or list the stage-1 fence or any `SERVING_DAY#`,
`SERVING_TELEMETRY#`, or publication-marker delete access. Each worker now holds only the rolling
domain, frontier, publication, binding and `SERVING_TRANSITION#` calls its code makes. All staged
hunks are attributed to FEAT-002. The FEAT-003 telemetry hunks stay uncommitted for the next commit.
FEAT-002 is not yet accepted. Still missing: the
`FixedMemberDrainActivationByteFixtureV1` 98/512 byte profiles and one-beyond refusals, behavioral
controls for `drain_final_alias_and_activate_next`, the A-to-B whole-chain election control, and
the live release integration updates. The FEAT-002b receipt below closes the first three.

### Rolling-freshness FEAT-002b local commit receipt

Local verification only; nothing was deployed. Parent: `2502a303`. The verified staged tree was
`939f89b803fca062abb38ebc7407bd574a5f8ab3`; the commit adds only this receipt to it. Each check
ran once against a clean export of that tree. Raw logs remain ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/feat-002b-commit/`.

The production builder now has committed `FixedMemberDrainActivationByteFixtureV1` profiles. The
committed generator `tests/unit/fixed_member_byte_fixture_support.py` captures them from the real
promoter pass. Every number below was measured from those profiles; none was typed by hand.

| Profile | Actions | Aggregate bytes (design estimate) | Largest target (estimate) |
| --- | --- | --- | --- |
| 98 candidates, elected | 51 | 55,129 (44,680) | 9,274 (8,127) |
| 512 candidates, elected | 51 | 176,162 (123,829) | 39,496 (38,349) |
| 98 candidates, no eligible | 42 | 33,729 | 9,176 |
| one beyond (512 plus 50 checks) | 101, refused | 176,162 | 39,496 |

Re-measured 2026-10-16 (UTC): both `COALESCE#` writers now stamp the cohort's earliest candidate root clock, and the elected winner's bake `OUTBOX#` stamps its run's clock, so the stuck-flow sweep no longer counts every live cohort as an unclocked flow (it tripped `open-flows-without-root-clock` during acceptance x5). That adds 58 bytes each to the new `COALESCE#` and `OUTBOX#` and 58 to the drained `COALESCE#`'s pre-state; action counts are unchanged. Measured at `750a1697`; the regenerated fixture SHA-256 values are `candidates-98` `2d8869d5583ca171295d391298a80d15c21ec7e41fe6a17593e1d4d86bd3c081`, `maximum` `37e26e67643033e67867d84ef1dc91c7d1b867de8a5c6e0bc5c95f20100a22ea`, `no-eligible-98` `fb06716185599a0a8777bd406e8232e7aded4ecdb1b375dcc216e58f9ea971d3`, `one-beyond` `f463f2743bc9111dea6d273615ec01de787ae0a84a9d651f45ad208614787dd8`. The fixture SHA-256 values below belong to this receipt's own tree, from before that change.

The measured totals exceed the estimates mainly because the estimate did not count the full
successor roster that activation stamps on the selected winner's bake node. The estimate's
calculation was not retained, so the remaining fixed-size difference cannot be itemised. Every profile stays below 100
actions, 400,000 bytes per item and 4,000,000 aggregate bytes. The design and acceptance numbers
were corrected in the same commit. Building the controls exposed two production defects, both
fixed:

- The final alias's own `COMPLETE#` counted as a lower-ordinal gap for the successor, so B never
  elected and A's final alias never drained.
- The selected winner's marker check compared `target_digest` with the publish node's effect
  digest, so every elected final transaction failed.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| Plan item 3 pytest list | 461 passed | `b90e9a1dee08cb7268fa6b7ee30b0c391f2eb455f8dccdbe131d303e0e9c64c0` |
| FEAT-002 handoff pytest list plus operator, dispatch-store, table-writer and unit-batch-store | 1,028 passed | `abce10d7a46f517f9b87837c56f4f57996c39816b47861f11bbf63295f09032c` |
| `fixed_member_byte_fixture_support.py --check` (all four profiles, including 512) | byte-identical | `294f54c9927307d157735b4f120843c47a1451d12fcc104683510ba5a0edc5ed` |
| Jest `ingestion-stack`, `graph-call-inventory`, `control-plane-authority-stacks`, `publisher-stacks` | 92 passed | `958775daab959904b74e6daaac3b6c2c497598327fcbb4c57b84b04af7db6dd8` |
| `pyright` (strict), `npm run typecheck` | 0 errors; pass | `f552d237ad0bd249c05f97eb62dbc65d2d0e4a5fe2346608e10fb7838dba9ed1`, `29e13a4e236649719a890bbfe69edae179312a03bd554db6a3bb13268d67db04` |
| `ruff check`, `ruff format --check`, `make lint-boundaries` | pass | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`, `48bc4859bfa243f69b0be8b1d866c74c7cc035f3e0aee9e4839e9dd1b0bd49ac`, `c489dc4fd5d3da3abb92cd8a02022d0273ce8b05e782f3f129fa78079ced3e6a` |

Fixture SHA-256: `candidates-98` `0c95b9910a42036a8483c67b9df2f48005c32968ff711ce885c58b307ade88d5`;
`maximum` `37077d2e32372f71e051d5889b6546abb282ce2ebc291687db8f16fed58fb89a`; `no-eligible-98`
`4b4f99fdd664b19a8c86429dc15c6fd685f8a8ac042f06ab882eb8b317403bfe`; `one-beyond`
`f94ceaa72868c183cfb0931e754d587822aa12522945c6b4a3f98742c4488405`.

The behavior tests cover these final-transaction cases:
- a 51-action A-to-B election, with B publishing while A is ACTIVE;
- a 42-action `NO_ELIGIBLE` branch;
- a conflict, and a timeout before commit; each leaves the alias and obligation unchanged, then a fresh retry commits;
- a lost response, which resolves by equal replay, while a different request ID is refused;
- a remaining-time reading below the guard, where the final alias and its `COALESCE#` stay durable and the next invocation elects;
- a racing publication, which fails the whole transaction and joins the fresh retry;
- a 52-action builder drift, which is refused before any write.

The live release integration updates remain open.

Stage-1 deploy pin: `87af5efbb12581908233db041154e9273b80350c` on `feat/graph-stage1-fence`
(2026-10-15), deployed from a detached checkout that day (lock 15:11:22Z, released 15:24:12Z;
`make check-source-key` passed). It supersedes `96b82c30f730be7f0d4bc0f338c1e170ef739636`, which itself superseded
`a44951788d7a9cd4521564f2304ca2d160896b8a`. `a4495178..87af5efb` is exactly two changes:

- the typing-only fix at `execution_graph.py:318` (`96b82c30`), so strict pyright passes;
- one `infra/test/support/probe-resource-inventory.ts` row, backported from the final tip.

The final tip already carries both. At `96b82c30`, `make infra-test` failed 1 of 1,682 on
pre-existing FEAT-001 probe-name drift: `validation/probe_ingestion_traceability_gate.py
"ingestion"`. At `87af5efb` every item-10 gate passed: `make check`, `make infra-typecheck`,
`make verify`, `make infra-test` (1,682 of 1,682), `make build-lambdas`, `make check-staged-tree`,
and dev synth. Its diff-only `make deploy-all-locked` summary is byte-identical to the reviewed
`96b82c30` diff, including the shared Lambda asset `5327d339…`, because the new row is test-only.
The evidence is under `tooling-build/item10-stage1/`. The implementation review and
the final adversarial review must also review `a4495178..87af5efb`, not only `4bd808d0..HEAD`.

### Rolling-freshness delayed-writer control receipt (criterion 2, amended)

Live against the dev probe table `adlytics-dev-execution-graph-probe` on 2026-10-15
(13:23-13:33Z), at `feat/graph-stage1-fence` `7307f1b1`. That commit is test-only; the stage-1
deploy pin stays `87af5efb`. All 13 writers passed in 16 runs, which include 3 paired positive
controls, and 0 probe rows remained after disposal. The evidence is under
`tooling-build/stage1-delayed-writer-live-iter3/`: the JUnit file and one disposition record per
run.

Criterion 2 was amended 2026-10-15 (UTC) by the orchestrator under the owner's standing
overnight delegation; ratified by the owner 2026-10-15, so criterion 2 is PASS under the amended
wording, no longer NOT_PROVEN. The amendment and its rationale are in the ignored, operator-local
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/acceptance.md`, criterion 2.
DynamoDB rejects the three reserved-word writers' own request before any condition runs. So each
one also replays its exact transaction with only `domain` aliased against the still-fenced
table. The alias is test-only: keys, values and condition meaning stay the same. That replay
proves each writer's own `MIGRATION_FENCE_SET` refusal, which the earlier `c1171550` run lacked.

| Writers | Required outcome | Live result |
| --- | --- | --- |
| The ten fence-refused writers | `MIGRATION_FENCE_SET`, only the fence member cancelled, zero change | 10 passed |
| `park_cohort_for_capacity`, `settle_capacity_unavailable`, the capacity branch of `drain_alias_binding` | `ValidationException` for reserved keyword `domain`, no condition evaluated, zero change. Then the alias-normalized replay leads with the production fence check, commits unfenced against an identical world, and only the fence member cancels it, as `MIGRATION_FENCE_SET` with zero change | 3 passed (aliased members: 2; 2, 3 and 9; 9) |
| `roll_capacity_wait`, run beside each of the three | `MIGRATION_FENCE_SET` in the same test | 3 passed |

Separate finding (pre-existing, latent): in the deployed pre-stage-1 lineage, these three writers
can never succeed, because `domain` is an unaliased reserved word in their condition expression.
Because `park_cohort_for_capacity` is the only writer that creates a `CAPACITY_WAIT#` row, no
wait can have been created through this path. That is consistent with the zero-wait census that
criterion 2 expects. The corrected `LegacyServingCensusV1`, taken later that day (censuses A and B,
15:45Z), found 0 waits in 11,377 scanned rows. The final
runtime deletes these writers, so stage 1 does not remediate them. The unit-tier `FakeDynamo` does not model reserved words, which
is why only the live control found it. The offline delayed-writer table now models the word in
condition and update expressions.

### Rolling-freshness FEAT-003 local commit receipt

Local verification only; nothing was deployed. Parent: `ad609c84`. This commit records the
FEAT-003 telemetry handoff (plan item 5) exactly as it was left in the worktree: 17 paths, with
binary diff SHA-256 `5dcf9dc945b2b7856b6afce2a3977696fba28044407b747b2299b063a42ed023`. It also
adds this receipt. The checks ran once, in the worktree, against that diff. Raw logs remain
ignored under `.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/feat-003-commit/`.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| `make generate-trace-contract` | wrote 71 spans; diff unchanged afterward (no generator drift) | `b209ee478727d5a4c9c8e98755f2f60e11359494a09c381a8fd5c5a11164f33d` |
| `make check-trace-contract` | passed; 71 static spans conform | `fc93fe7e84f7bf8d24808e21883f396a52c2bd4e1b0b027968c9f1364adf8c77` |
| Plan item 5 pytest list | 187 passed | `4ad0fe7a63a7ed80fcaf782166159647b5662b1d2748ed1c114d63806c672c6f` |
| Touched tests outside the list (`test_graph_precompute`, `test_insight_bedrock`) | 33 passed | `f1b5045ce8b761ce455a07695a73f230c809b3b1b8890fc7a5acf04f876222aa` |
| Every unit file that imports `AthenaQueryAdapter` | 73 passed | `a9e8539547d89517c13da719aa09045a8f6f907d6024182c089879f1209768a9` |
| `ruff format --check` on the 16 changed Python files | pass | `7ff2df5e4a19c344d6ea2dfc25c25d2370159894cbac570f392753b22180e09c` |
| `ruff check` on the 16 changed Python files | **fail**, 6 findings | `a3e997c2df5d916dcacbe925d0c1cc64d65d0adc5f010506eb1970b0a69e9518` |
| `pyright` (strict) on the 16 changed Python files | **fail**, 12 errors | `a95fc5bab1faa70adc6dff1498841b07c43117d2fa8519b395452bc29fb18ed6` |

The 187 count is three more than the earlier 184 handoff count. FEAT-002b `ad609c84` added
promoter controls to `test_execution_graph_promoter.py`, which is in the list.

FEAT-003 is not accepted. The handoff is incomplete in these ways (from reading the code, unless
a check above shows otherwise):

- `promoter.py::_promoter_domain` declares the `execution_graph.promoter_domain` child, but nothing
  calls it. No domain span is emitted, and nothing tests the isolated-failure acceptance (one
  errored child, one metric, one log, and the next domain still runs). Its `context.on_failure`
  call may also double-report inside the existing `isolated()` seam.
- `athena/query.py` now raises `ValueError` when there is no correlation. Serve-time callers
  never bind one: `presentation_handlers/handler.py:9146`, `tool_handlers/measurement_entrypoint.py:110`,
  `insight_precompute/handler.py:529`, and `preset_artifact_publisher/baker.py:911`. Their
  Athena queries would fail. The contract makes `run_id`, `task_id`, and `cohort_revision`
  required on `athena.query`, which conflicts with plan item 5's "serve-time chat handles absent
  correlation". That conflict needs a design decision; this commit does not resolve it.
- The trace contract has no conditional requirement on correlated model calls.
  `insight.generation` only allows `cohort_revision`. `trace_contract/contract.py`, `__main__.py`,
  `materializer.py`, and the tests `test_trace_contract`, `test_tracing`, `test_graph_bake`, and
  `test_execution_graph_promoter` have no FEAT-003 changes.
- Static findings. Ruff reports `PLR0913`/`PLR0917` on `_promoter_domain`, `I001` in `workers.py`
  and `test_preset_artifacts.py`, `F821` (`Any` undefined in a string `cast` in
  `graph_precompute.py`), and `S105` on the `EXECUTION_GRAPH_PROMOTER_PASS` constant. Pyright
  reports `_promoter_domain` and `_cohort_correlation` as unused, nine unknown-type errors on the
  `graph_precompute.py` serving-execution fallback, and an unnecessary `isinstance` in
  `SpanCorrelation`. `span_context.py`'s module docstring still shows the two-argument
  `correlated`.
- Bake task and finalize messages now require `owner.cohort_revision`. Messages enqueued by the
  pre-FEAT-003 runtime would be refused after deploy.

Every hunk is attributed to FEAT-003: no hunk is unattributed.

### Rolling-freshness FEAT-004 local commit receipt

Local verification only; nothing was deployed. Parent: `97ed7360`. This commit closes plan
item 4 (publication, bake finalization, status, and final IAM over the live frontier and
immutable elected snapshots). Most of item 4's runtime and final IAM landed in FEAT-002
`f9199945`. The audit found one runtime gap and no paired controls for item 4's Verify claims.
This commit adds them:

- `graph_publish._settle` now ends bounded settle CAS/frontier races the same way `_stage_and_flip`
  does: it releases the claim and returns the recoverable `frontier_conflict`. Before, it returned
  `graph_redecide_exhausted`.
- Publish controls: B flips and settles on the live frontier while A's head is `ACTIVE`. The test
  checks the exact visible/eligible/revision deltas and the stamped eligible coordinate, that the
  head is never read, that A's head is byte-identical, and that no `PUBLISH_WAIT#` row exists.
  Bounded settle races end in `frontier_conflict` with the claim released and the flip intact.
- Bake controls: finalization conditions on the head's `cohort_revision`, elected-frontier digest
  and elected-candidate digest. It commits after live-frontier movement, and its transaction names
  no `SERVING_FRONTIER#`, `SERVING_DAY#`, `CAPACITY_WAIT#` or `cycle_epoch`. If the snapshot
  changes between read and commit, the transaction is refused with `SERVING_HEAD_CHANGED`, writes
  nothing, and leaves the run `GRANTED`. The paired control then finalizes.
- Whole-chain control: B publishes while A is `ACTIVE` and never joins or mutates A. A finalizes on
  its frozen snapshot, and the next pass elects B as a new cohort.
- Status and worker controls: retired day/wait items and fields never project, explain or render.
  No runtime store or worker module exposes or calls a park/wake/day/wait method, or the fence
  read. `FLOW_OBLIGATION_KINDS` holds no wait kind.
- Inventory controls: no call row and no synthesized policy in any of the five GRAPH stacks names
  `MIGRATION#`, `SERVING_DAY#`, `CAPACITY_WAIT#`, `PUBLISH_WAIT#` or `SERVING_TELEMETRY#`. Only the
  promoter holds `SERVING_TRANSITION#` (`GetItem` and `PutItem`). A reinstated grant is reported
  by stack and Sid.

No infrastructure code under `infra/lib` changed. The final IAM from `f9199945` already met the
resolution rule; this commit only adds test controls for it.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| Plan item 4 Verify pytest list (six files) | 146 passed | `29caff9965e9cd58b1ce3babc7019cd551de078d2755e3dd77f1d683d882fcf3` |
| Plan item 4 Verify Jest `ingestion-stack`, `graph-call-inventory` | 67 passed | `74543ccbdfc5a7a756f7aac17656a6694c5dc39feae42ea99fcb6dcb2db5af55` |
| `ruff format --check`, `ruff check` on the 7 changed Python files | pass | `0321fffcb91606a42c0a19d8c1121d5c544379d663821fce616c71a7bb62f6b5`, `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `pyright` (strict) on the 7 changed Python files; `tsc --noEmit` in `infra` | 0 errors; pass | `f552d237ad0bd249c05f97eb62dbc65d2d0e4a5fe2346608e10fb7838dba9ed1`, `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

The Verify suite ran twice. On the first run, 1 pytest failed and 145 passed, and Jest passed 67.
The failure was a defect in a new test, not in the runtime: it built a stale finalize request whose
run revision had already moved. The test now injects the race at commit. The logs above are from
the second run, against working-diff SHA-256
`b83aa791834a3d16d7795fb083de5bc323aabf8877706edc5d748aa206ce8574`. Raw logs, including
`run1-*`, are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/feat-004-commit/`.

Open before implementation review:

- FEAT-003 `athena.query`: require `run_id`/`task_id`/`cohort_revision` only under bake or
  precompute correlation. When `correlation()` is `None` (serve-time chat), record them as
  absent and never raise. This is the same rule as `insight.model.call` (owner decision about
  04:25Z, 2026-10-15). This commit does not touch that contract.
- FEAT-003 `promoter_domain`: the child span is never emitted, and the isolated-failure control
  is missing.
- FEAT-003 static gates: ruff check (6 findings) and strict pyright (12 errors) on the FEAT-003
  files.
- `frontier_conflict` is not in `table_writer.UNIT_RECEIVE_OUTCOMES`, so `SemanticUnitReceives`
  counts it as `unclassified`. FEAT-002 introduced it for flip races. This commit routes settle
  exhaustion there too, which was formerly the classified `graph_redecide_exhausted`.
  `table_writer.py` is outside plan item 4's file list.
- The live release integration updates (`tests/integration/test_execution_graph_release_live.py`)
  are still open.

### Rolling-freshness FEAT-003 closure local commit receipt

Local verification only; nothing was deployed. Parent: `03dd0045`. This commit closes the five
items the FEAT-004 receipt left open before implementation review.

- `athena.query` follows the `insight.model.call` rule (orchestrator decision about 04:25Z,
  2026-10-15). Beneath bake or precompute correlation it carries `run_id`, `task_id` and
  `cohort_revision`. A serve-time query has no correlation, so it records them absent and still
  runs. `spans.yaml` keeps the three as allowed, not unconditionally required, and states the
  rule on both spans. Bake trace completeness still enforces the cohort case.
- `coalesce_serving` now isolates each domain. Each attempted cohort opens one
  `execution_graph.promoter_domain` child under the request's `promoter_pass` span, and records one
  closed outcome: a `ServingElectionOutcome` value, or `failed`. A failed attempt marks its child
  and is reported once through the pass's failure sink (`report_error`), and the next domain is
  still decided. Conflicts are not retried inside the pass, because only a wholly fresh iteration
  may retry one. An invalid Lambda clock still ends the pass. The final-transaction race control
  now asserts exactly one reported `GraphGuardError` and an unchanged head, alias and obligation.
- Static gates: ruff and strict pyright are clean across `src` and `tests`. `generated_spans.py`
  gets a scoped `S105` per-file ignore, because the `PASS` in `EXECUTION_GRAPH_PROMOTER_PASS` is a
  span name, not a credential.
- `frontier_conflict` joins `UNIT_RECEIVE_OUTCOMES` as a retried outcome. A test now requires every
  `graph_publish` outcome literal to be in that vocabulary.
- The live release integration file adds a domain-only bootstrap control: a first rolling grant
  creates an IDLE head and zeroed live-frontier shards, with no day field. An equal replay
  leaves both byte-identical. This test needs the dev probe role and has **not** been run.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| `make generate-trace-contract && make check-trace-contract` | 71 spans conform; no generator drift | `acfd19355172521ad8d3ebedca699a8de6ea41c0a7fcbeaf3ab9f24b8d5edf09` |
| Plan item 5 pytest list plus `test_table_writer`, `test_graph_precompute`, `test_insight_bedrock`, `test_graph_publish`, `test_graph_whole_chain` | 393 passed | `ceb176b65ab1f9da90c1d07b7f65809d967d5acafe2a50e36b783a4b02c4880a` |
| `ruff check src tests`, `ruff format --check src tests` | pass | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`, `48bc4859bfa243f69b0be8b1d866c74c7cc035f3e0aee9e4839e9dd1b0bd49ac` |
| `pyright` (strict, whole repository) | 0 errors | `f552d237ad0bd249c05f97eb62dbc65d2d0e4a5fe2346608e10fb7838dba9ed1` |
| `make lint-boundaries` | pass | `d33e0ff01baf3c39922fd108dc55412ce853849ec6a177d9aa5c1cdb78c5368f` |

Raw logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/commit-a/`.

### Rolling-freshness Athena admission (plan item 6) local commit receipt

Local verification only; nothing was synthesized against an account or deployed. Parent: `946be9cd`.

`athenaQueryBatchMaxConcurrency` now comes only from
`ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY`, read by
`envRequiredWholeNumberInRange(name, 1, ATHENA_START_BUDGET.chat_reserve_active_dml)`. It has no
numeric fallback:

- An absent, empty or blank value is reported as unset, and the error names the variable.
- A present value that is not a base-10 whole number from 1 to 20 fails synth with the name and
  the range. That covers a sign, fraction, exponent, leading zero, hex, words, 0, and 21.

The name is in `Makefile::DEPLOY_FLAG_NAMES`, and `.env.example` documents explicit 8. Both
independent `getConfig` maps carry it: `config.test.ts::CORE_ENVIRONMENT_SUPPLIED` and
`edge-security.test.ts::supplied`. `TEST_CONFIG` keeps its explicit field.

Stack controls at a legal non-8 value (3):

- Ingestion and its stage-1 construct propagate it.
- The Baker stays at its start-budget cap of 10.
- Insight precompute stays at `2 × 4`, with model calls at 8.

The static start budget (41/200/20) is unchanged. The operator's ignored `.env` is not touched
here; preflight sets it.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| `make infra-typecheck` | pass | `29e13a4e236649719a890bbfe69edae179312a03bd554db6a3bb13268d67db04` |
| Plan item 6 Jest list (8 suites) | 319 passed | `af94a32757f2ba024f32edd1135a55312880fd6f0e6c541dd7e6de6c1051172a` |

Raw logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/commit-b/`.

### Rolling-freshness retirement and acceptance verdicts (plan item 7) local commit receipt

Local verification only; no AWS call was made. Parent: `0ea2f82f`.
`probe_ingestion_traceability_gate.py` adds the criterion 1, 2, 5, 6, 7, 8 and 9 verdicts. Every
one fails closed.

- **Retirement (criterion 1).** `RuntimeAdmissionRetirementV1` (`--retirement-scan`, local)
  covers these roots:
  - every Python file under `src/`, by AST;
  - every TypeScript file under `infra/lib/`, by lexical scan with comments skipped;
  - both generated projections and `graph-call-inventory.ts`;
  - the two mandatory auxiliary AST roots.

  It follows aliased imports and calls, attribute calls, keywords, dataclass fields, and string
  constants. A legacy literal is accepted only inside the four function-qualified owners, and
  even there a retired call fails. Unknown files, parse failures, or a missing mandatory file
  are `NOT_PROVEN`. The retired vocabulary is the committed
  `validation/fixtures/runtime-admission-retirement-v1.json`, so neither auxiliary root holds a
  literal of its own. That fourth fixture is a placement choice this commit makes; the design
  names only the roster. On this tree, 625 files are scanned with 0 forbidden hits.
- **Census and inertness (criterion 2).** The read-only `--legacy-census` mode covers both the
  predeploy cutover and the postdeploy check.
  - `gather_legacy_serving_census` scans strongly, follows every page itself, and refuses a
    repeated token. It selects exactly by `pk.S`/`sk.S`, with waits on the sort key. It writes
    canonical JSON with recursively ordered keys and records the bytes, distributions, and
    SHA-256. A malformed key, token cycle, identity change, or scan error retains only an
    invalid partial.
  - `legacy_serving_inertness_verdict` strongly reads every enumerated head and obligation.
    `legacy_serving_cutover_verdict` binds census A, the inertness verdict, and census B
    together.
  - `legacy_serving_census_verdict` gates bytes and identity, not provenance.
  - `fence_readback_verdict` and `fence_wait_verdict` (900 s from the later of the two times)
    check the operator fence output.
- **Cohorts (criteria 3–5).** `ObservedCohortRun` and `ObservedCohortEvidence` are now
  day/capacity-free. Each run carries terminal time, publication, and binding facts, and each
  cohort carries its bake chain and promoter transition. `observed_cohort_verdict` requires one
  direct with one execution, exact aliases and contributors, one successful
  `bake.run`/`bake.finalize`, one transition, and a flip no later than settlement. Root and
  cycle durations come from `terminal_at`, never `bound_at`, and both are bounded by 7,200 s.
- **Remaining verdicts.**
  - `trace_completeness_verdict` (criterion 6) checks exact per-class cardinality, correlation,
    lexical parents, bytes, Athena statistics, and terminal spans. It also requires one
    promoter-domain child per attempt under its pass.
  - `athena_admission_config_verdict` (criterion 7) covers the absent, fallback, unproven
    non-8, proven non-8, mismatch, and static-row branches.
  - `paired_bake_chat_verdict` (criterion 8) uses exact nearest rank, the inclusive overlap
    endpoint, and the preflight/launch bracket.
  - `hold_clear_matrix_verdict` (criterion 9) reads the committed
    `validation/fixtures/graph-hold-clear-migration-v1.json`, which holds the 22 `acceptance.md`
    rows.

Every verdict has a paired passing control.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| Plan item 7 pytest list (`test_probe_ingestion_traceability_gate`, `_p6`) | 218 passed | `89f326ed2716479f3ebc592c01b7ec2afe615dcc581c0d51bb5aea5dc64f8a82` |
| `--retirement-scan` on this tree | passed: 625 files, 20 allowlisted hits, 0 forbidden | `2e194e77e13b4173975771e31fbdfb20ceda0844a338d17016153d74d26804d3` (receipt) |
| `ruff check`, `ruff format --check` (`src tests`) | pass | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`, `48bc4859bfa243f69b0be8b1d866c74c7cc035f3e0aee9e4839e9dd1b0bd49ac` |
| `pyright` (strict, whole repository) | 0 errors | `f552d237ad0bd249c05f97eb62dbc65d2d0e4a5fe2346608e10fb7838dba9ed1` |

Raw logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/commit-c/`.

### Rolling-freshness control driver and paired chat (plan item 8) local commit receipt

Local verification only; nothing was landed, sent, or read in AWS. Parent: `621a0551`. Nothing
below had run live at this commit. The driver and paired chat later ran live in the 2026-10-15/16
acceptance runs, the paired chat with the v2 prompt manifest; x9 passed.

- **`land_fixtures` (`--reserved-probe-key K` with `--no-twin` or `--twin-only`).** The two
  flags constrain each other and require `--probe-retrieval`. Neither accepts `--dry-run`, and
  `--twin-only` needs `--graph-store-stack`.
  - A reservation puts K only if K is still the planned key.
  - A twin-only launch puts nothing. K must be the first free slot once its own slot is
    excluded. The launch then strongly verifies one immutable version with exactly the planned
    manifest bytes, and sends only K's twin.
  - Drift is refused with zero puts. Ordinary landing is unchanged.
- **`validation/run_graph_rolling_freshness_control.py`.** Every input is validated before AWS
  access: dev only, an explicit profile, a 12-digit account, a readable manifest, a new evidence
  directory under `validation/evidence/`, and an unused execution ID. The run then:
  - parses the whole four-roster manifest, with labels and sources unique across all rosters;
  - checks every child argv with the real `_parse_args`;
  - dry-plans every entry twice, refusing drift, a repeated window or key, exhaustion, or
    malformed output;
  - reserves serially in `(window, key, label)` order;
  - launches twin-only children as argument tuples, each needing exactly one valid
    `PROBE_TWIN` line;
  - runs the single root to terminal, launches A, waits for A strongly `ACTIVE` with exactly
    its own runs, then launches B and requires B's settlement while A is unchanged. A's
    transition must elect B.

  `rolling_freshness_control_verdict` checks each of these facts and the 7,200 s bounds.
  `RollingFreshnessControlV1` is written atomically, `NOT_PROVEN` on any failure.
  - The committed `fixtures/graph-rolling-freshness-control-v1.json` has one single root, two
    A and two B roots from the 2026-10-06 daily run (all five previously landed in dev), and
    the full planned 98-delivery 2026-10-05 daily run as `synthetic_full_plan`. Every window
    is distinct.
- **`probe_turn_latency.py`.** It requires the committed six-prompt
  `fixtures/chat-bake-acceptance-v1.json`, so the implicit sequence is gone.
  - `--phase paired-no-bake|during-bake --output` writes a six-row draft. Each row carries the
    exact epoch interval and latency, the terminal class, the governed generation (strongly read
    before and after the row), and the quota, throttle, HTTP, and auth outcomes. Each
    during-bake row also carries a strong `ACTIVE` head receipt.
  - The outside-list item must be present in the accepted generation and absent from the
    bounded list.
  - The first-root launch, the target bake run, and the telemetry-derived bake interval exist
    only after the bake. So `--bind-draft` binds them into `ChatLatencySampleV1`. That bind
    step is a sequencing choice this commit makes; the design names only the inputs. A bound
    pair passes the gate's `paired_bake_chat_verdict`.

| Check | Result | Log SHA-256 |
| --- | --- | --- |
| Plan item 8 pytest list (`test_run_graph_rolling_freshness_control`, `test_land_fixtures`, `test_probe_turn_latency`, `test_graph_whole_chain`) | 185 passed | `a3390d2cb725125c2603a19587ef64e9b31ddc058908ea15a281c2f87137c986` |
| `ruff check`, `ruff format --check` (`src tests`) | pass | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`, `2854867803323f64a6c7723da2517180e382c49178faa2864373a330f3a7d3db` |
| `pyright` (strict, whole repository) | 0 errors | `f552d237ad0bd249c05f97eb62dbc65d2d0e4a5fe2346608e10fb7838dba9ed1` |
| `--retirement-scan` | passed: 625 files, 0 forbidden | `2e194e77e13b4173975771e31fbdfb20ceda0844a338d17016153d74d26804d3` (receipt) |

Fixture SHA-256: `graph-rolling-freshness-control-v1.json`
`2599e6195be2220d6a6bd1bdc33476ca27cdddfb56ad9920e4041b9f10818fe7`; `chat-bake-acceptance-v1.json`
`6703b639d758b4dd3ebd3145c290f4d1960e19ca9d08229ec870d1151cf48377`. Raw logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/commit-d/`.

### Rolling-freshness convergence gate fixes local commit receipt

Local verification only. Parent: `49f2fb9c`. The first full `make verify` at `49f2fb9c` had 9
failing unit tests, and `make infra-test` had 1 failing test. This commit fixes all four
causes:

- The fixture `promoter_pass` now re-raises the pass's one isolated failure. Four
  `test_execution_graph_release_store` controls expect a conflict, timeout, ambiguous result, or
  52-action error from a promoter pass, so their assertions are unchanged. They failed
  because per-domain isolation reports the failure instead of raising it.
- `artifact.write` follows the `athena.query` pattern. Beneath cohort correlation it carries run,
  task, and cohort. A legacy-intent write outside a cohort records them absent and still writes.
  FEAT-003 had made it raise, which broke the four `test_campaign_preset_source` persistence
  tests and the legacy write path. `spans.yaml` is unchanged; trace completeness enforces the
  cohort case.
- `DEEP_INPUT_REVISIONS` gets a new revision, `2026-10-15.1`, for the FEAT-003 span-context,
  Athena, and generated-span changes inside the deep-input closure.
- The committed probe-name inventory gains
  `validation/probe_ingestion_traceability_gate.py` `ingestion`. That is the FEAT-001 pre-existing
  `probe-resource-names` drift, a name already checked against the synthesized universe.

Gate results are in the final convergence report.

### Rolling-freshness domain mutation review local commit receipt

These are clean-tree `make mutate` runs at `4c3c7315`, one per changed domain module. Every
run left survivors; triage and remediation are pending.

| Module | Generated | Killed | Survived |
| --- | ---: | ---: | ---: |
| `bake_scheduling.py` | 476 | 383 | 93 |
| `stuck_flow.py` | 315 | 238 | 77 |
| `flow_explain.py` | 594 | 399 | 195 |
| `graph_projection.py` | 222 | 141 | 81 |
| `execution_graph_dispatch.py` | 780 | 319 | 461 |

The source tree was clean after every run. Raw sessions and logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/mutation/`.

### Rolling-freshness implementation-review fixes local commit receipt

Local verification only. Parent: `520204cf`. This commit closes the three material findings of
the rolling-freshness implementation review. No existing assertion changes.

- `drain_final_alias_and_activate_next` now proves a same-request-ID replay against the committed
  facts before it returns `REPLAY`. The checked transition facts are outcome, successor cohort
  revision, `drained_at`, and `elected_at`. The checked resulting-head facts depend on the
  outcome. An elected head must match the cohort and input revisions, winner, elected frontier
  and its digest, candidate digest, and run IDs. The winner's bake node must carry the same
  stamped authority. A `NO_ELIGIBLE` head must be `IDLE` at the next version. Any difference
  refuses before a write. New controls cover changed terminal time, outcome, successor cohort,
  input revision, candidate digest, frontier, winner, plan digest, and a rewritten head, each
  with zero writes. They also cover the equal and refused `NO_ELIGIBLE` replay.
- `test_execution_graph_dispatch.py` adds paired positive and negative controls for every
  rolling-election guard. On the frontier vector, they cover shard length, negative values, and
  `eligible <= visible`, plus the count properties. On the candidate roster, they cover the
  512 bound, single domain, ordinal 0 against -1, every non-`GRANTED` run state, missing ARM or
  COMPLETE authority, every unsettled publication state, every started worker-node state, and
  runtime or binding presence. On the gap rule, they cover same-domain-only gaps with an
  equal-but-not-identical domain, the duplicate-ordinal refusal, no gap at or above the winner
  ordinal, and the lowest-gap selection.
- The `SERVING_FRONTIER#` shard mapping is pinned with literal values for eight fixed run IDs.
  Further controls cover the blank-run-ID refusal, non-power-of-two shard counts 20 and 24
  configured independently, and literal results at shard counts 1 and 64. No expectation is
  derived through `serving_frontier_shard`.

Gate results are in the final convergence report.

### Rolling-freshness obligation-count control local commit receipt

Local verification only. Parent: `115764af`. The scoped mutation re-run at `115764af` left one
non-equivalent survivor: the `serving_gap` duplicate-ordinal count compared with `is not`. That
mutant is wrong for more than 256 open obligations, because those counts are distinct `int`
objects. This commit adds a 300-obligation control that requires the comparison by value.

### Rolling-freshness election mutation re-review local commit receipt

This is a clean-tree cosmic-ray run at `ced56e83`, using the `make mutate` configuration. It
covers `execution_graph_dispatch.py` lines 617-766, which hold survivors S-47 to S-71. It ran
against six test files:

- `test_execution_graph_dispatch.py`
- `test_execution_graph_unit_batching.py`
- `test_execution_graph_release_store.py`
- `test_execution_graph_dispatch_store.py`
- `test_graph_publish.py`
- `test_execution_graph_promoter.py`

The run had 237 mutants: 204 were killed and 33 survived. S-48 to S-71 recorded 127 mutants.
Of those, 96 are now killed by named tests, and 31 are equivalent with a stated reason. The
equivalent mutants are type annotations, small-int and enum identity, the redundant
`zip(strict=...)`, and set-size ordering. `domain-operations-execution_graph_dispatch.md` records
each verdict in place. S-47, the `@dataclass` flags on line 618, is outside the review scope and
stays OPEN. The source tree was clean after the run. The raw session, log, and triage script
are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/iter2/mutation/`.

### Rolling-freshness elected-replay head control local commit receipt

Local verification only. Parent: `c180d85b`. This commit closes implementation-review MED-1. A
same-request-ID elected replay now also requires the resulting head to be `ACTIVE` at
`request.head.version + 1`, as the `NO_ELIGIBLE` branch already requires `IDLE` at that version.
New controls move the head to `BOUND_DRAINING`, to `IDLE`, or past the next version, with every
other field unchanged. Each must refuse with zero writes. Without the new comparison, all three
controls fail. No existing assertion changes.

| Check | Result |
| --- | --- |
| `make generate-execution-graph-protocol && make generate-trace-contract` | passed, no diff |
| Nine release, promoter, dispatch, byte-oracle, publish, batching, and trace suites | 778 passed |
| `make check` | passed |
| `make lint typecheck lint-boundaries` | passed, 0 pyright errors, 5 contracts kept |

Raw logs are ignored under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/converge-fix/iter3/`.

### D2 spike S0 bake certification receipt (horizon-bounded pin)

Local verification only. No AWS call. Base: the D2 merge tip `24854c92`. `git status
--porcelain` was empty there before S0 measured (O1). The single committed authority is
`tests/fixtures/bake-certification/d2-certificate.json` (O4). It holds the 20-row table, the
commands with raw-output digests, the O3 scope figures, the pin certificate, the budget totals,
the root bound and the lane result. This commit also carries the pin-growth remedy (plan A5.1
I9, orchestrator decision 2026-10-15 ~14:05Z). That remedy adds horizon-bounded whole-scope
reads, generated `source_horizon_days` and the pre-write horizon and byte guard. It changes no
page or chat answer.

| Value | Figure |
| --- | --- |
| Rows certified (N) | 20, all within budget and 900 s |
| Sum of `budget_seconds` / root bound | 4,080 s / 4,140 s of 7,200 s |
| N = 20 lanes (2^19 Gray-code steps) | 1,379.4 s and 1,382.6 s, makespan 1,382.6 s |
| Audiences | C = 7, 61 requests, 286.6 s of 300 s (write 16.6 s, a projection) |
| Write seconds | campaigns measured 11.2 s, 16.6 s adopted; other pages 16.6 s (projection) |
| Source horizons (days) | campaigns 123, retail-health 106, scorecard 140, new-customers 125, reach 261, retention 832, audiences 218 |
| Pin, largest item (saturated) | `BAKE_SOURCE#scorecard#30d` 84,770 B (ceiling 400,000; first S0 run 93,533) |
| Pin aggregate (saturated) | 970,919 B (ceiling 4,000,000; first S0 run 1,214,959) |
| Invariance | byte-identical at 1,257 (saturation), 2,000, 3,650 and 7,300 days of history |
| Negative control (horizons removed) | aggregate over 4,000,000 at 353 days, item over 400,000 at 598 days |

| Checksum (SHA-256) | Of |
| --- | --- |
| `5d2a6a71104713871e46e05bd7f8300ac38d5b25b7d2a34147a06b60da945d1a` | `tests/fixtures/bake-certification/d2-certificate.json` |
| `31dae84d04b370aa7a23d77dbd19285a3a4b96480452a92a5f409afa32b4a93f` | the owning design, plan Addendum A5 with the remedy text and A5.9 (ignored custody) |
| `c867c23f3cfe30df503c4a977c4ae4bd09538f3607e34c1e9cda2910cbfd460c` | `pin-growth-remedy.md`, the reviewed remedy design (ignored custody) |
| `52346319cbe77f90ead4c7269347284190317f5b0fee0869d72e078adf7f068e` | `bakeability --d2-roster` output |
| `a2f4c1528fb2a97e7103303e4a9fe336300688a9af235ec927e33257177c936b` | `make check-bakeability` output |
| `70074f21cb1fbf5c60d3b78acb607a098da7271e6b7c6f3eb050fe1bfd5a25a5` | the measured pin section |
| `9fe387868c30d076085b204f8fba8e54274d16fbfa7888b5a46e88113e3e36db` | the run-69e71e5d `artifact.write` extract |

| Check | Result |
| --- | --- |
| `test_bake_pin_certificate.py`, `test_bake_source_horizons.py`, `test_preset_bakeability.py` | 32 passed |
| Graph bake, whole chain, precompute, release, protocol and registry suites | passed, except the two registry tests the merge receipt names as expected red until A5.3 adds the rows |
| `make check` | passed |
| `make lint typecheck lint-boundaries` | passed, 0 pyright errors, 5 contracts kept |

Raw outputs, the remedy design and the first S0 run's stop evidence are ignored under
`.agents/tasks/dev-integration/d2-merge/s0/`.

Long gates on the clean S0 commit `7cf556fb`:

| Gate | Result |
| --- | --- |
| Clean re-run of the four digested commands | all four outputs match the checksums above byte for byte |
| `make verify` | lint, typecheck, boundaries and contracts passed. Unit: 11,131 passed, 8 failed. Five are the expected red the merge receipt names: the two census tests, the two registry tests and the Reach B4 tripwire. Two are `test_dsp_reach` first-day tests, which also fail on a clean export of `24854c92` because their DynamoDB fake predates the servable projection pointer. One is `test_deep_input_revision`; the follow-up commit closes it |
| `make infra-test` | 60 suites, 1,712 tests passed |

The follow-up commit appends deep-input revision `2026-10-15.3`, because S0 changed the entity
catalog and segment-label reads inside the deep-input closure. The deep insight cache therefore
starts cold at the next deploy. `test_deep_input_revision.py` passes, 4 of 4.

Merge gate on `feat/d2-integration`, credential-free (every AWS credential source pointed at
nothing, `ADLYTICS_*` unset). The in-tree gates ran on `aeabd861`. Two fixes followed:
`32476554` stamps the servable projection pointer in the `test_dsp_reach` first-day fake, and
`2508b718` commits the verify-clean export's index so `HEAD` resolves there. `verify-clean`
then ran on `2508b718`.

| Gate | Commit | Result |
| --- | --- | --- |
| The S0 certificate checksum | `aeabd861` | matches the checksum above |
| Every `make generate-*` target, then `git diff --exit-code` | `aeabd861` | 11 generators, no diff, tree clean |
| Targeted pytest: census, bake registry and pin, `test_graph_bake`, preset artifacts, every presentation suite, the renumbered measurement-code suites and `test_audiences_runtime_bake.py` | `aeabd861` | 851 passed, 4 skipped. The 4 are the integration tests, which skip without credentials |
| `make check` | `aeabd861` | passed |
| `make verify` | `aeabd861` | lint, pyright (0 errors), boundaries (5 contracts kept) and contracts (2 warnings, no errors) passed. Unit: 11,205 passed, 2 failed, the two `test_dsp_reach` first-day tests. `32476554` fixes them, and `test_dsp_reach.py` passes 6 of 6 |
| `make infra-test` | `aeabd861` | 60 suites, 1,712 tests passed |
| Frontend typecheck, lint, test, build and e2e | `aeabd861` | passed: 55 files and 660 tests, a static build, and 13 e2e tests |
| `make synth ENV=dev PROFILE=…` | `aeabd861` | refused by design without credentials: "Configuration for dev is not filled in yet", because the account comes from the profile |
| `cdk synth -c env=dev`: the operator's dev configuration, with a placeholder account and `ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY=8` (the dev configuration leaves it unset) | `aeabd861` | 32 templates synthesized. 4 `CloudFormation-Validate::F3006` warnings say the local validator does not know `AWS::Lambda::ResourcePolicy` (TokenService, Harness, EvaluationResultLinkV3, EvaluationV4) |
| `make verify-clean` | `2508b718` | verify passed with 11,206 unit tests and 1 skip, a test that needs git history the export lacks. `make check` and `make infra-test` (1,712 tests) passed. In the frontend stage, 659 of 660 tests passed. The Reach axe test hit its 5,000 ms limit at 5,251 ms while another workspace's test run was loading the CPU. Run alone, it takes 1,229 ms |
| The verify-clean frontend stage, re-run on a clean export of HEAD | `2508b718` | `npm ci`, typecheck, lint, 660 tests and the build passed |

Only `tests/unit/test_dsp_reach.py` and `scripts/verify-clean.sh` change after `aeabd861`, and
`verify-clean` covers both. So the generators, e2e and the synth were not re-run.

Merge-gate review round 1 (F1 to F9):

| Finding | Disposition |
| --- | --- |
| F1 runtime test bypasses GRAPH | `test_audiences_runtime_bake.py` is now a non-authoritative render diagnostic: no pointer, body or custody write, and the sources get a pointer store that only reads. The GRAPH-path acceptance (20-row root, finalized grant, served generation) is the deploy step's |
| F2 live target unbound | Once opted in, the test refuses unless `ADL_TEST_ENV=dev` is explicit, `ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT` (12 digits, supplied by the operator, never committed) matches STS and the function ARN, and the function is exactly `adlytics-dev-PresetArtifactBaker`. The guard is the pure `tests/integration/runtime_bake_target.py`. Its paired positive and negative controls are in `tests/unit/test_runtime_bake_target.py`, because every integration test skips without credentials, so a control kept there would never run in a credential-free gate |
| F3 restore before evidence | Discharged by deletion: nothing is written, so there is no restore. The trace is written in `finally` with its pass or fail outcome |
| F4 pin-absent reclaim | `test_a_byte_envelope_refusal_whose_settlement_is_lost_reclaims_as_pin_absent`: the first delivery is never settled, and the reclaim is `durable_bake_source_pin_absent` with zero CURRENT reads and zero pin-family writes. It fails when the selection-marker guard is removed |
| F5 write-seconds provenance | The campaigns rows are now `projection`, "adopted (conservative) 16.6 s; measured 11.2 s". No number changes. Certificate SHA-256 `f0c40d24abcb5d110e24abffd56806c054ecf4488bb443a578354f723c321b71` supersedes `5d2a6a71…` above, and the S0 artifact returns to its blocking review |
| F6 accepted-tip drift receipt | Discharged by the forward-merge step, which records the A5 citation-set diff against the accepted GRAPH tip. That is the only tip that matters while GRAPH is still moving |
| F7 exact `make synth` | Discharged by the deploy step, which runs the exact target with dev credentials and a reviewed diff before any mutation |
| F8 (LOW) | Tech debt: document `pin_size.over_cap_plans` and its duplicated `reach#last_complete_quarter` entry |
| F9 (LOW) | Tech debt: the 20-row runtime bake, the real-Bedrock insight acceptance and the stage-1 rollback check stay open after merge, sequenced after F1 |

Merge gate after round 1, credential-free, with the same runner as above:

| Gate | Commit | Result |
| --- | --- | --- |
| Every `make generate-*` target, then `git diff --exit-code` | `f927ce13` | 11 generators, no diff, tree clean |
| Targeted pytest, the same module set | `f927ce13` | 852 passed, 5 skipped (the integration tests, which skip without credentials). The added pass is the F4 reclaim control |
| `make check` | `f927ce13` | passed |
| `make verify` | `f927ce13` | passed. Unit: 11,208 passed |
| `make verify-clean` | `1515fce7` | passed. Unit: 11,214 passed and 1 skipped (the git-history test). Pyright reported 0 errors and boundaries kept 5 contracts. `make check` passed, `make infra-test` passed 60 suites and 1,712 tests, and the frontend passed 55 files, 660 tests and the build |

`1515fce7` moves the F2 guard's controls to the unit tier and changes no behaviour. Nothing under
`frontend/` or `infra/` changed after `7d2be1c8`, so frontend e2e and the offline synth were not
re-run. The exact `make synth` stays with the deploy step (F7).

Merge-gate review round 2 (F10 to F16), on parent `7b053b45`:

| Finding | Disposition |
| --- | --- |
| F10 stale roster digest | Every S0 command was re-run on the round-3 tree. The raw outputs are in the ignored `.agents/tasks/dev-integration/d2-merge/s0/round3/`. The certificate now records their digests (table below). `test_the_recorded_roster_output_digest_reproduces_on_this_tree` re-runs `--d2-roster` in process and fails if the recorded digest or any row's counter output line is stale |
| F11 counter protocol | Every row now carries `counter`: `builder` (the real production builders the counter invoked, as `module.qualname`), `fixture` (the closed maximum-cardinality parameters: the reference instant, windows, marketplace, campaign, model and scope widths, the publication/coverage state, and for calendar rows the instance count it maximised over) and `output` (the row's verbatim `--d2-roster` line). `test_the_certified_rows_equal_the_committed_certificate` asserts all three against the counters. The certified counts, phases, seconds and budgets are unchanged. One label changed: the 13 D2 rows' `budget_basis` read `deployed contract budget`, but those budgets are in the contract and not yet deployed, so every contract row now reads `contract budget, re-certified` |
| F12 diagnostic bounds | `test_audiences_runtime_bake.py` binds every bound to the generated S0 row certificate, which it checks against the committed row. Audiences uses the five-phase ceilings `audiences_phases(M, C)` (list; roster rounds P + C, C, C; measurement). The `roster` barrier is bounded by the sum of the three rounds, the whole render by the certified count, and the time by the Q10 `phased_task_seconds` within the row budget. Retention asserts that the generated count is 6 and that the render ran at most that many queries. The legacy three-phase and Q8 helpers are no longer used |
| F13 GRAPH-path acceptance | New deploy-gated `tests/integration/test_d2_graph_root_acceptance.py`. It lands the control manifest's `single_root` through the real `land_fixtures` child (dry plan, reserve, `--twin-only`), as `run_graph_rolling_freshness_control.py` does. It writes no pointer, body or graph item itself. It waits for the run to reach a terminal state, then strongly reads back the run, its nodes, all 20 CURRENT pointers and the grant's serving binding. The pure verdict `tests/integration/d2_graph_acceptance.py` requires: a `SUCCEEDED` granted run; `bake_finalize` `SUCCEEDED`; one `SUCCEEDED` task per roster row whose effect names the grant; every available row's pointer grant-bearing, at the effect's revision, with its grant's serving generation; written rows under the run's own grant; and every available pointer served `built_at` its write instant under its grant's binding. The verdict's paired controls (one accept, seven refusals) are in `tests/unit/test_d2_graph_acceptance.py`. The test opts in only through `ADL_D2_GRAPH_ACCEPTANCE_ARGS` plus the F2 Baker guard. It runs only in D2's live-acceptance step, after the integrated dev deploy. It was not run here |
| F14 accepted-tip receipt | Below |
| F15 exact synth | Below |
| F16 (LOW) | Tech debt, unchanged: `pin_size.over_cap_plans` and its duplicated `reach#last_complete_quarter` entry |
| Worker composition marker | `tests/unit/test_execution_graph_worker_composition.py` had no `unit` marker, so `make verify` never collected its retirement fence for the day/wait writers and the publication park/wake pair. It now has `pytestmark = [pytest.mark.unit]`. `pytest -m unit` on the file: 3 passed. The fence fails as expected (`AssertionError`) when a retired method (`wake_parked_publish`) is re-added to `DynamoGraphReleaseStore` |

S0 command digests on the round-3 tree. They supersede the table above:

| SHA-256 | Output |
| --- | --- |
| `8ce35ae9a9cafb082597394388e76ead2eed24079ba628a045073d66ddfffa5a` | `bakeability --d2-roster` |
| `889a240899c1de6ae54a10ff54bfd26ca7dcd36d4681d9dd653027969a513fc2` | `make check-bakeability` (retail-health task seconds now use the 16.6 s write seconds `5dbf0d0e` adopted) |
| `70074f21cb1fbf5c60d3b78acb607a098da7271e6b7c6f3eb050fe1bfd5a25a5` | the measured pin section (unchanged) |
| `9fe387868c30d076085b204f8fba8e54274d16fbfa7888b5a46e88113e3e36db` | the run-69e71e5d `artifact.write` extract (unchanged) |
| `8f34cf4e881ab3bde47569b92dc6a8c25a44820ee7353dabdfc4672d5b2d6390` | `tests/fixtures/bake-certification/d2-certificate.json`. Supersedes `f0c40d24…` and returns to blocking review |

`owning_design_digest.sha256` is now `f8aee28d…`, the current `plan.md`. The remedy digest is
unchanged. The S0 suites (`test_bake_pin_certificate.py`, `test_bake_source_horizons.py`,
`test_preset_bakeability.py`) passed 33 of 33.

**F14: A5 citation-set reconciliation against the current GRAPH tip.** Read on 2026-10-15 with
`git rev-parse feat/graph-bake-delivery`, the tip is `ffc2309d7077aed579d213fffd15056b608560b7`.
`ca1dc7c9` (the integrated tip) is its ancestor, 18 commits behind. This tip is not yet accepted.
The forward merge refreshes this delta against the accepted tip.

`git diff --stat 4c3c7315 ffc2309d -- <A5 citation set>`:

```
 Makefile                                |   8 +-
 .../operations/execution-graphs.yaml    |   7 +-
 .../execution_graph_dispatch.py         |   4 +-
 .../dynamodb/execution_graph_release.py | 106 +++++++++++++-
 .../test_execution_graph_protocol.py    |   2 +-
 5 files changed, 115 insertions(+), 12 deletions(-)
```

`git diff --stat 041e0235 feat/ui-ia-impl -- <A5 citation set>`: empty. The UI tip is still `041e0235`, so there is no drift.

| File | Hunk | Commit | Disposition |
| --- | --- | --- | --- |
| `Makefile` | `synth`, `diff` and `deploy` gain `set -eu; $(load_deploy_env)` | `a07c79d1` (in `ca1dc7c9`) | No A5 symbol touched. A5 cites `check-bakeability`, which is unchanged |
| `execution_graph_release.py` | `_final_transition_replay_change` and `_elected_replay_facts` on the promoter's replayed transition | `b353218c`, `115764af` (in `ca1dc7c9`) | No A5 symbol touched. `pin_durable_bake_sources` and `finalize_bake` are unchanged |
| `execution_graph_release.py` | `_binding_member`: `domain = :domain` becomes `#domain = :domain` | `1995acac` | Expression-attribute alias for the reserved word `domain`. Same attribute, same value. `finalize_bake` reaches this through `_binding_member(binding, None, …)`, which takes the `put` branch. The edit is in the `identity is not None` `update` branch, so the finalize write is unchanged. No A5 behaviour change |
| `execution_graph_release.py` | `_selected_winner_members`: the same alias in the `COMPLETE#` ConditionCheck | `1995acac` | Promoter serving election, not cited by A5. Same condition. No A5 symbol touched |
| `execution_graph_dispatch.py` | `build_publication_flip_participant` and `settle_publish`: the same alias in two ConditionChecks | `1995acac` | The publication flip and settle, not cited by A5. Same condition. No A5 symbol touched |
| `execution-graphs.yaml` | `serving_coalescing.next_election_min_remaining_ms` changes from 30000 to the measured 10493 | `d3e8720d` | The Promoter's next-election guard. A5 cites `bake_tasks` and the bake bounds, and this key is outside them. No A5 symbol touched |
| `test_execution_graph_protocol.py` | the test pins the same constant at 10493 | `d3e8720d` | Test of the hunk above. No A5 symbol touched |

No hunk changes a cited symbol's behaviour, so the A5.6 stop rule does not fire. GRAPH's
reserved-word guard (`c1742f14`, "Run the reserved-word gate in make verify and guard every fake")
is outside the citation set and is not on this branch. It arrives with the forward merge of the
accepted GRAPH tip.

**F15: exact synth.** This step ran `make synth ENV=dev PROFILE=<dev profile>` on parent
`7b053b45`, after sourcing the dev env loader
(`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/dev-deploy/env.sh`, ignored,
operator-local; values never printed). The worktree's ignored `.env` supplies
`ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY`, copied from the GRAPH worktree's ignored `.env`,
the same preflight path GRAPH's dev deploys use. `.env.example` documents it as 8. The setting was
the only input the round-1 substitute injected. The deploy lock was not taken, and no diff or
deploy ran. Result: exit 0, "Successfully synthesized" for all 32 dev stacks. The only warnings
were the four known `CloudFormation-Validate::F3006` `AWS::Lambda::ResourcePolicy` warnings
(TokenService, Harness, EvaluationResultLinkV3, EvaluationV4). The ignored log's SHA-256 is
`87c2a95f…`. Round 3 changes nothing under `infra/` or `frontend/`.

Merge gate after round 2. It ran credential-free with the same runner. The ignored logs are under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/dev-deploy/merge-gate-r3/`:

| Gate | Result |
| --- | --- |
| Targeted pytest: the round-1 module set, plus `test_d2_graph_root_acceptance.py`, `test_d2_graph_acceptance.py`, `test_runtime_bake_target.py` and `test_execution_graph_worker_composition.py` | 872 passed, 5 skipped. The 5 are the integration tests, which skip without credentials |
| `make lint typecheck lint-boundaries` | passed: pyright 0 errors, 5 contracts kept |
| `make check` | passed. The first run refused this receipt for binding a literal AWS profile, which is now a placeholder |
| `make synth ENV=dev PROFILE=<dev profile>` (exact, dev env loader) | exit 0, 32 stacks (F15) |
| S0 suites | 33 passed |

The round-3 commit changes nothing under `frontend/` or `infra/`. It changes no generator input
and no production behaviour except one `budget_basis` label in the `--d2-roster` print. So the
generators, frontend gates, `make infra-test`, `make verify` and `make verify-clean` were not
re-run. The round-1 results for those gates stand. The two new unit modules and the marked worker
composition file join `make verify`'s unit tier from this commit.

Merge-gate review round 3 (M1, M2), on parent `a2ff0d5e`:

**M1: the certificate names the tree that reproduces it.** Round 3 recorded
`regenerated_on_parent = 7b053b45`, whose `_row_budget` prints a different `budget_basis` label,
so that tree cannot emit the retained roster output. This step re-ran the four S0 commands and the
S0 suites on the clean committed tip `a2ff0d5e037fbc1b32b5f75ae532ba88061d6273`.
`git status --porcelain` was empty before and after. The raw outputs are in the ignored
`.agents/tasks/dev-integration/d2-merge/s0/round4/`. Every output hashes to the digest the
certificate already records, byte-identical to round 3:

| SHA-256 | Output |
| --- | --- |
| `8ce35ae9a9cafb082597394388e76ead2eed24079ba628a045073d66ddfffa5a` | `bakeability --d2-roster` |
| `889a240899c1de6ae54a10ff54bfd26ca7dcd36d4681d9dd653027969a513fc2` | `make check-bakeability` |
| `70074f21cb1fbf5c60d3b78acb607a098da7271e6b7c6f3eb050fe1bfd5a25a5` | the measured pin section |
| `9fe387868c30d076085b204f8fba8e54274d16fbfa7888b5a46e88113e3e36db` | the run-69e71e5d `artifact.write` extract (input `21a790ed…`) |
| `bdbb1a7d21782a9223255524f370f2157cf16b3d0d0f5f8961d57c28e9eb9b8c` | `tests/fixtures/bake-certification/d2-certificate.json`. Supersedes `8f34cf4e…` |

The S0 suites passed 33 of 33. The certificate edit changes only `measured_tree.regenerated_on_parent`
(now `a2ff0d5e`), its `regeneration_note` and `raw_custody` (now `round4/`). No count, phase,
second, budget or digest changed. The commit carrying it has `a2ff0d5e` as its parent and changes
no counter code.

**Re-measure, 2026-10-17: the pin shrank under dc8374dc.** `dc8374dc` scopes source coverage to
the facts each metric reads, so the Campaigns, Scorecard and New customers rows pin fewer coverage
scopes. The committed `pin_size` no longer equalled the measured one. This step re-ran the three
S0 measurement commands with the round-4 `pin_section.py` on `ff36d6b8`; the raw outputs are in
the ignored `.agents/tasks/integration-publish-ready/s0-remeasure-2026-10-17/`. The roster and
`check-bakeability` outputs reproduce byte for byte; only the pin section changed:

| Figure | Before | After |
| --- | --- | --- |
| Largest item (`BAKE_SOURCE#scorecard#30d`) | 84,770 B | 71,062 B (ceiling 400,000) |
| Pin aggregate | 970,919 B | 838,395 B (ceiling 4,000,000) |
| Negative control: aggregate / item over the ceiling at | 353 / 598 days | 380 / 670 days |
| Pin section SHA-256 | `70074f21…` | `074dad6d06eaaf6060b9e06515f3fab22fea95b56cb487cd8ecd8337fe126e0b` |
| `d2-certificate.json` SHA-256 | `bdbb1a7d…` | `9395ccaab199f4c7a6f61683e445da08eb0e53bbe548b173a46086ef546f8dc7` |

`measured_tree.remeasure` records the same before and after. No count, phase, second or budget
changed. The S0 suites passed 33 of 33.

**M2: the over-bound Audiences month through GRAPH and the actual route.** Owner decision 1b: an
over-bound month shows the typed unavailable state, never a stale month labelled current. The
new deploy-gated `tests/integration/test_d2_audiences_over_bound_acceptance.py` runs the prep
sequence "A body, then B `audiences_body_over_bound`, served as `window_unavailable`" end to end.
It writes no pointer, body or graph item itself:

1. It strongly reads month A's Audiences CURRENT pointer and body.
2. It lands one reserved over-bound delivery through the real `land_fixtures` child (dry plan,
   reserve, `--twin-only`), as the GRAPH control lands its single root, and waits for the run's
   terminal state.
3. It strongly reads back the run, its nodes, the finalized pointer, its body and the grant's
   serving binding.
4. It POSTs the deployed `/api/presentation/audiences` for `last_complete_month` with an API
   session refreshed from `ADL_BASE_URL`, `ADL_REFRESH` and `ADL_CSRF`. No credential value is
   printed or recorded.

The pure `audiences_over_bound_verdict` (`tests/integration/d2_graph_acceptance.py`) accepts only
when all of these hold:

- the run `SUCCEEDED` with a grant and `bake_finalize` `SUCCEEDED`;
- the Audiences task `SUCCEEDED` and wrote under the run's grant;
- month A held a full Audiences body;
- the finalized pointer is the run's grant-bearing revision for a later month, with the grant's
  serving generation;
- its body is the typed `audiences_body_over_bound` marker for that month;
- it is served under the grant's binding `built_at` its write instant;
- the route answers 200 `window_unavailable` (`preset_unavailable`) resolved to month B, with no
  page body.

`tests/unit/test_d2_audiences_over_bound_verdict.py` holds 14 credential-free controls: the
route's reason, one accepted transition and twelve single-deviation refusals. One of the
refusals is the stale month A still served as current. The twenty-row root verdict is unchanged
for the other rows. Opt-in is `ADL_D2_AUDIENCES_OVER_BOUND_ARGS`: a JSON object holding the GRAPH
control's `control_argv` and the `over_bound_source` delivery name, plus the F2 Baker guard. It
runs only in D2's live-acceptance step, after the twenty-row root acceptance. It was not run
here, because GRAPH acceptance is live in dev.

The prerequisite is still open: the deterministic over-bound month B delivery is not yet in the
fixture corpus. The generated Audiences fixtures hold a few dozen rows a month. The bound is
`audiences_row_budget(1)` = (256,000 − 16,000) / 800 = 300 rows. The live step needs a month-B
delivery above that bound before it can pass. Until then the verdict refuses: the body is not
the marker.

Merge gate after round 3. It ran credential-free with the round-3 runner, plus the two new
modules. The ignored logs are under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/dev-deploy/merge-gate-r4/`:

| Gate | Result |
| --- | --- |
| Targeted pytest: the round-3 module set, plus `test_d2_audiences_over_bound_acceptance.py` and `test_d2_audiences_over_bound_verdict.py` | 886 passed, 6 skipped. The 6 are the integration tests, which skip without credentials |
| `make check` | passed |
| `ruff check`, `ruff format --check` and `pyright` on the three changed test modules | passed: pyright 0 errors |
| S0 suites on clean `a2ff0d5e` (M1) | 33 passed |

Round 4 changes only the certificate's provenance fields, three test modules and this receipt.
It changes nothing under `src/`, `infra/` or `frontend/`, and no generator input. So the
generators, frontend gates, `make infra-test`, `make synth`, `make verify` and `make verify-clean`
were not re-run, and their earlier results stand.

Merge-gate review round 4 (M1, M2), on parent `df37614d`:

**Month A must decode whole, for its pointer's window.** The verdict accepted month A when its
`type` was `AudienceProfileArtifact`, so a truncated body carrying that tag passed the
"A body, then B marker" transition. Month A is now decoded with
`require_audiences_artifact_payload`, the decoder the Audiences route applies before serving,
and its `month` must equal `before.resolved_window`. Two credential-free controls join
`tests/unit/test_d2_audiences_over_bound_verdict.py` (16 now): a correctly tagged but malformed
body (the tag alone, and a whole body without `markets`) is refused, and a whole body for another
month than its pointer is refused. The accepted control's month A is now a real September body
rendered by `test_segment_profile._bake_with`, not a bare tag.

**M1: `over_bound_source` binds to one tracked declaration.** The new tracked
`validation/fixtures/d2-audiences-over-bound-v1.json` declares the one month-B delivery the
acceptance accepts, its roster row, and a row floor of 301 (`audiences_row_budget(1)` + 1).
`declared_over_bound_source` in `tests/integration/d2_graph_acceptance.py` refuses, before any
AWS call, any other text (blank included) and a declaration whose floor the body bound admits.
Credential-free controls: the declared entry binds, four undeclared values refuse, and an
at-bound floor refuses.

Superseded in round 6: no such generator can reach the real loader unchanged, and the live leg is
deferred to tech debt TD-D2-OVERBOUND-LIVE below. As written in round 4: the producible over-bound
input is built at the D2 live-acceptance step by a validation-only
generator. It lands a probe-owned month-B delivery that matches the declaration. Nothing changes
under `src/` or in `land_fixtures/bundle.py`, and normal dev landing does not change: the
generated corpus cannot hold the month, because adding it there would make dev's Audiences
"last complete month" serve `window_unavailable` and force S0 re-certification (owner decision B).
So M1 is closed at merge scope (binding and declaration) and open only at deploy scope, like the
other deploy-gated executions. The merge does not discharge the live run. Tech debt carried, not
fixed: `pin_size.over_cap_plans` repeats one `reach#last_complete_quarter` tuple without plan
identity (F16), and the exact dev synth takes `ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY` from
an ignored `.env` until GRAPH FEAT-004 lands it in tracked config.

Merge gate after round 4, credential-free with the round-4 runner. The ignored logs are under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/dev-deploy/merge-gate-r5/`:

| Gate | Result |
| --- | --- |
| Targeted pytest: the round-4 module set, after M2 | 888 passed, 6 skipped. The 6 are the integration tests, which skip without credentials |
| `make check`, after M2 | passed |
| Targeted pytest: the round-4 module set, after M1 | 894 passed, 6 skipped. The 6 are the integration tests, which skip without credentials |
| `make check`, after M1 | passed |
| `ruff check`, `ruff format --check` and `pyright` on the three changed test modules | passed: pyright 0 errors |

This step changes three test modules, adds one validation declaration and edits this receipt. It
changes nothing under `src/`, `infra/` or `frontend/`, and no generator input. So the other gates
were not re-run; earlier results stand.

Merge-gate review round 6 (verdict F1-F9), on parent `5ee3b935`. Scope: F1 and F2 only.

**F2: the live acceptances measure the complete task, write phase included.** The no-write
diagnostic compared a render-only total with a budget that reserves the write, and neither GRAPH
acceptance read a task summary or a write span. `d2_task_timing_verdict` in
`tests/integration/d2_graph_acceptance.py` now decides each row from the accepted run's own
evidence: exactly one Baker `task_summary` naming that run, the row's one `SUCCEEDED` task node
and its effect kind; its complete `seconds` (plan, query, render, write and tail) within the
row's generated `budget_seconds`, with the phases inside it; for a written row, one
`artifact.write` span per written pair under the same run and task id, bytes summing to the
summary's written bytes, none outlasting the write phase; for an unwritten row, none. The
twenty-row root acceptance applies it to every row and the over-bound acceptance to the
Audiences row; each passes only when its existing verdict and the timing verdict both pass, and
both are written to the receipt. The live read (`read_task_timing_evidence`) reuses the
traceability acceptance's `task_summaries` and `aws/spans` readers, refuses an incomplete span
retrieval, and re-reads for up to ten minutes while Logs Insights ingests. Eight credential-free
controls join `tests/unit/test_d2_graph_acceptance.py`: accepted; a render consuming the write
allowance (over budget); missing and short write spans; a summary or spans from another run;
another task's or a second summary; a span outlasting its phase and a write on an unwritten row;
phases outlasting the task and a malformed summary; a row without a `SUCCEEDED` node. Live
execution stays deploy-gated. The nineteen `[projection]` write-seconds are replaced only from
the receipts these acceptances write once they run.

**F1: recorded as deploy-gated tech debt (orchestrator decision D, round 6).** No `src/`, corpus
or reviewed-tree change this round. `validation/fixtures/d2-audiences-over-bound-v1.json`,
`declared_over_bound_source` and their controls stay as committed. The declaration's `purpose`
text describes the follow-up producer below, which does not exist yet.

**TD-D2-OVERBOUND-LIVE: the over-bound Audiences live leg is deferred.**

- Blocker. `land_fixtures --probe-retrieval` plans only from `plan_with_backfill(ROOT)`, which is
  the same `tests/fixtures` corpus normal dev landing uses, and a probe copies the planned
  delivery's bytes unchanged. A month-B delivery above 300 rows can reach the dry plan only
  through a `src/` hook or a corpus change. A corpus change alters normal dev landing (round 4).
- Proven offline (credential-free, in the tree):
  - the Audiences over-bound refusal and its typed marker;
  - the certified bound+1 refusal (`test_graph_bake_bound.py`);
  - the strict source binding (`test_d2_audiences_over_bound_verdict.py`);
  - the over-bound verdict's 16 controls.
- Not proven live: one GRAPH root over a real over-bound month B, finalized, then served by the
  deployed Audiences route as `window_unavailable`. The live leg would add only end-to-end
  confirmation of the route integration. It is not claimed live, and D2's live-acceptance plan
  skips it.
- Resolve first: the month-completeness timing. The corpus ends at September 2026, so month B is
  probably October 2026, which is not a complete month until 2026-11-01. This is an inference
  from the corpus; dev CURRENT was not read.
- Preferred follow-up, A: an opt-in flag on `land_fixtures`, accepted only with
  `--probe-retrieval`, that adds a validation-only producer's month-B delivery to the probe plan.
  The default is unchanged, and the change gets its own review. Credential-free proof: generate
  the delivery, verify its manifest and a measured row count above 300, show the dry plan
  accepts it, and show it refuses without the flag.
- Alternative, B: a validation-only producer lands month B straight in the dev raw bucket and
  sends the twin itself. This bypasses the real `land_fixtures` child, which is why A is
  preferred.

**TD-D2-AUDIENCES-CAMPAIGN-LIMIT: Audiences shows at most 7 DSP campaigns (dev-only stopgap).**
- Decision. Owner-approved 2026-10-17 (option A): the runtime composes with the certifier's bound,
  `audiences_runtime_campaign_bound` (7 at one marketplace, 61 certified plans). This is the only
  authority for page behaviour, the gate, acceptance and tests.
- Why. Live cohort a6e5f457 refused query 62: the runtime ran 11 campaigns against a count certified
  for 7.
- `audiences_timeout_headroom_campaigns` (11) is the 900 s timeout estimator only. It is not a page
  limit.
- Deferred to the redesign:
  - repeated full label-publication reads in one attempt (review 2026-10-17 GPT #2);
  - the S3 read amplification from dropping the row cache (Opus).
  Correctness is unaffected.
- Resolve with: the outcome-first, constant-query Audiences redesign (designed, not yet built). The
  customer-facing statement is in the root README's [Known limits](../README.md#known-limits).
- Live result: [D2 live acceptance](RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18).

**D2 live-acceptance plan.** After the F3 forward merge and integrated dev deploy, the plan runs:

- the twenty-row root acceptance (`test_d2_graph_root_acceptance.py`), with its F2 timing verdict;
- the real-Bedrock insight path;
- the stage-1 rollback check;
- the exact deployment synth and readbacks.

The over-bound Audiences leg (`test_d2_audiences_over_bound_acceptance.py`) is deferred to
TD-D2-OVERBOUND-LIVE and is not claimed live. Its opt-in stays unset.

Outcome (2026-10-18): the dated per-leg verdicts are in
[D2 live acceptance](RESULTS.md#d2-pages-and-graph-control-plane-live-acceptance-on-dev--2026-10-18).

**F3 (forward merge onto the accepted GRAPH tip) is not done here.** It cannot be done until
GRAPH is accepted; the forward-merge step owns it, re-runs the full gate on the merged tree, and
is followed by the two-model delta review. F4-F9 stay recorded as tech debt.

Merge gate after round 6, credential-free with the round-4 runner. The ignored logs are under
`.agents/tasks/ingestion-traceability/evidence/graph-bake-delivery/dev-deploy/merge-gate-r6/`:

| Gate | Result |
| --- | --- |
| Targeted pytest: the round-4 module set, after F2 | 902 passed, 6 skipped. The 6 are the integration tests, which skip without credentials |
| `make check`, after F2 | passed |
| `ruff check`, `ruff format --check` and `pyright` on the four changed test modules | passed: pyright 0 errors |

Round 6 changes four test modules and this receipt, and nothing under `src/`, `infra/` or
`frontend/`, and no generator input. So the other gates were not re-run; earlier results stand.
The F1 decision-D commit after these gates changes only this receipt and one test module's
docstring. Its re-run is recorded in that commit's message.

## Running isolated ingestion latency diagnostics

`probe_ingestion_end_to_end_latency.py` requires an empty ingestion queue, an empty preset-bake queue,
the exact enabled ingestion mapping, and the three exact dev stacks before it lands one immutable
fictional Vendor traffic delivery. It runs the existing cutover acceptance and records the wall-clock
bracket to exact semantic `CURRENT`. It then records one post-`CURRENT` bake-queue and pointer snapshot
as diagnostic context only. That observation does not prove producer convergence, unchanged bake DLQ,
causally linked terminal outcomes, or fresh serving readiness; only an accepted flow-correlated status
contract can establish those boundaries.

```bash
uv run python validation/probe_ingestion_end_to_end_latency.py \
  --profile <profile> --environment dev --region us-east-1 \
  --expected-account <approved-dev-account> \
  --ingestion-stack Adlytics-Ingestion-dev \
  --mapping-uuid <exact-stack-owned-mapping-uuid> \
  --publisher-stack Adlytics-Publisher-dev \
  --store-stack Adlytics-PublisherArtifactStore-dev \
  --day <unused-fictional-day> --glance-views <fictional-value> \
  --max-seconds <bounded-window> \
  --evidence-dir validation/evidence/<new-local-directory>
```

The probe creates immutable dev data and therefore requires an unused fictional day. Its evidence
directory must not exist. Raw object, publication, attempt, and execution identities stay local and
ignored. Its `diagnostic_only` summary accepts exact semantic cutover but labels the post-`CURRENT`
snapshot as non-authoritative; pointer-token deltas are observations, not serving acceptance.

`probe_ingestion_drain.py` is the read-only companion for backlog observation. It requires explicit
profile, account, region, stack, mapping UUID, poll interval, deadline, and new ignored evidence path.
It never enables, disables, consumes, deletes, or purges messages. It reports queue/DLQ state,
structured lifecycle progress, measured throughput/ETA, Lambda latency, business-span latency, Athena
statement timing, and aggregate queue/compute metrics. Any new lifecycle error or DLQ-depth change
terminates the observation as failed.

## Retired planner-era probes

The response-composer, planner-contract, planner admission RCA, and initial-plan-assessor probes were
removed on 2026-09-07 with the managed Harness adoption; the planner-era contracts they exercised are
retired by [ADR-064](../docs/decisions/adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md).
Their earlier dated conclusions remain in `RESULTS.md` as history.

## Running Agent Quality audit-chain acceptance

The Agent Quality probe sends two fixed fictional authenticated turns through the deployed conversation
route. It refuses every target except explicit `dev`, verifies the selected profile account first, and
uses only the audit table's narrow correlation index followed by exact attested reads. It resolves the
conversation endpoint, audit tables, released evaluator identity/name, and service-owned result-log group
from the published SSM contract, then describes only the caller-supplied result-link CloudFormation stack.
It never enumerates account resources. It polls only exact deterministic finding, aggregate, admission,
delivery, writer, and result keys; the final lookup requires the receipt's `audit_id` and admission
reference. Standard output contains only allowlisted outcomes: no questions, answers, correlations,
audit/admission IDs, trace IDs, spans, evaluator explanations, resource IDs, or raw SSE.

```bash
set -a; source validation/.env.local; set +a
make accept-evaluation-link \
  PROFILE=<explicit-nonproduction-profile> ENV=dev REGION=us-east-1 \
  ACCOUNT=<approved-dev-account> \
  EVALUATION_RESULT_LINK_STACK=<exact-dev-result-link-stack>
```

`PROFILE`, `ENV`, `REGION`, `ACCOUNT`, and `EVALUATION_RESULT_LINK_STACK` have no defaults. The target
rejects every environment other than `dev`, every region other than `us-east-1`, an omitted target,
and invalid evidence-retention input before Python can call AWS. It creates protected raw custody in a
new ignored `validation/evidence/evaluation-link.*` directory, removes that directory after a successful
run, and retains it after a failure. Use `RETAIN_EVIDENCE=1` only when an otherwise successful run must
be retained for authorized review; it never changes stdout.

A completed aggregate chain alone does not accept Gate 3, and a writer receipt alone does not accept
protected evaluation linkage. Returned records must include one successful capability/conversation
terminal and one governed refusal or clarification terminal. For every selected released response, the
probe must attest the immutable receipt's nonempty admission reference, exact queue-delivery and writer
receipts, the deployed source-scoped result subscription, and the worker's exact `EvaluationResult.v1`
companion. Before accepting a result, the probe directly invokes only the ingress Lambda resolved
from the caller-supplied stack with one bounded fictional subscription envelope and requires the
persister's deterministic content-free companion; a pre-existing or mismatched result fails that
directed-ingress stage. The probe accepts semantic linkage only when that immutable companion is `completed` or
`evaluator_failed`. A missing companion is a pending/failed acceptance stage; it is never stored as a
score, an evaluator failure, or a synthetic terminal state. The protected subscription ingress receives
the CloudWatch gzip/base64 envelope only in memory, requires both the writer trace and audit correlation
to attest one target, and sends only canonical allowlisted `EvaluationResult.v1` bytes to its encrypted
queue. The persister has no CloudWatch read permission. Result state is never converted to zero or used
to invoke an evaluator again.

### Agent-mode regression

Run before and after a change to the agent prompt, a tool description, or the model, and record the
two reports with the change (agent-evaluation-validity steering). The question set is
`validation/agent-regression-v2.json`, so a baseline must be a report of set version 2; the runner,
its primed questions, and its report are defined in the Agent Quality Loop design's [regression path](../docs/capabilities/agent-quality-loop/low-level-design.md#regression-path-adr-079).

```bash
set -a; source validation/.env.local; set +a
make regress-agent \
  PROFILE=<explicit-nonproduction-profile> ENV=dev REGION=us-east-1 \
  ACCOUNT=<approved-dev-account> \
  EVALUATION_RESULT_LINK_STACK=<exact-dev-result-link-stack> \
  BASELINE=<report path printed by the earlier run>
```

`BASELINE` is optional. The report stays in ignored local evidence custody, because the next run
compares against it. A label mismatch does not fail the run; a chain that cannot be attested does.
Skipped, a prompt or model change ships with no evidence of how its answers were scored.

## Running joined managed Harness acceptance


`accept-managed-harness` is the single manual cutover gate for the deployed development path. It
requires an explicit nondefault profile, the literal `dev` environment and `us-east-1`, the expected
account, and every exact Harness, API, Agent, Agent Quality, evaluation, and observability stack name.
The target rejects omissions, aliases, other environments or regions, and invalid retention input
before Python starts. The runner then creates `boto3.Session(profile_name=..., region_name=...)`, makes
STS account attestation its first AWS call, and describes only those supplied CloudFormation stacks.
It never enumerates functions, roles, log groups, Harnesses, buckets, tables, or other account
resources; the two log-group configuration reads use exact ARNs derived from the attested account,
region, and named stack resource.

```bash
make accept-managed-harness \
  PROFILE=<explicit-nonproduction-profile> ENV=dev REGION=us-east-1 \
  ACCOUNT=<approved-dev-account> \
  HARNESS_STACK=Adlytics-Harness-dev \
  API_STACK=Adlytics-Api-dev AGENT_STACK=Adlytics-Agent-dev \
  AGENT_QUALITY_STACK=Adlytics-AgentQuality-dev \
  AGENT_QUALITY_PROCESSOR_STACK=Adlytics-AgentQualityProcessor-dev \
  EVALUATION_EVIDENCE_STACK=Adlytics-EvaluationEvidence-dev \
  EVALUATION_INPUT_STACK=Adlytics-EvaluationInput-dev \
  EVALUATION_OUTBOX_DISPATCH_STACK=Adlytics-EvaluationOutboxDispatch-dev \
  EVALUATION_STACK=Adlytics-EvaluationV4-dev \
  EVALUATION_RESULT_LINK_STACK=Adlytics-EvaluationResultLinkV3-dev \
  OBSERVABILITY_STACK=Adlytics-Observability-dev RETAIN_EVIDENCE=1
```

The runner resolves deployed values only through `contracts/generated/parameter-paths.json`. It
requires the Harness control record and generated `tools.json` inventory to agree, and the Harness to
admit only a route-minted token: the token service's issuer, the Harness audience, and the route
client. The Harness refuses a persona's own Cognito token, so every scenario runs through `/api/chat`.
Tool coverage asks each declared tool's fixed fictional question as the `probe-a` persona and requires
that tool's Gateway terminal record with a verified caller. The route offers the model every declared
tool, so coverage shows the intended tool ran, not that no other tool could. Acceptance requires a valid managed lifecycle, a legal
`ConversationStream.v4` stream whose `text` events concatenate to a non-blank answer, and a
receipt that names `conversation-stream.v4` and `answer.markdown.v1`, holds only governed
trajectory steps each with its reference, records no plan or blocks, and counts exactly the
released characters ([ADR-083](../docs/decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md)).
The declared-tool inventory and the Harness's allowed tools must name no finalizer. It covers a qualified
success, a typed limitation explained without semantic-intent substitution, a typed tool refusal,
safe no-tool social handling, a safe no-tool explanation that an unrecognized or unsupported metric
request cannot be mapped, and a representative same-turn multi-tool answer. No exact-render selector,
prose reconstruction, or paraphrase may intervene. A safe no-tool limitation must make no
customer-specific claim or figure, execute no substitute query or tool, preserve the user's intent,
and ask for clarification or offer next steps. It must audit as `publication=published` and
`terminal=complete` with no governed tool step that succeeded. The route records
`operation_outcome=succeeded` for a completed turn that called no governed tool and `refused` when
every governed call was refused, so the outcome label alone cannot show a limitation. Acceptance rejects substitute success, customer-specific claims, figures,
and weakened intent in either limitation path. Before invocation, source ignored `validation/.env.local`
with `ADL_REFRESH` and `ADL_CSRF` from an authenticated dev Managed Login browser session; the
collection and protection procedure is owned by `validation/env.example`. The runner refreshes that
ordinary BFF session through `/api/auth/refresh` before same-origin `/api/chat` requests. An
`ADL_SESSION` access token with `ADL_CSRF` is accepted instead, and the summary's `session_source`
says which was used; only `refresh` exercises the refresh path.

For each route turn, raw SSE and correlation remain local while the runner requires one exact bounded
Conversation API terminal log, one correlation-discovered and exact-read managed audit receipt, and
exact evaluation admission, delivery, writer, and result companions. The terminal log supplies the
only trace ID used for an exact bounded `aws/spans` Transaction Search query, which must hold the
route's `conversation.turn`, `conversation.route_exchange`, and `InvokeHarness` spans, none errored.
The Gateway tool boundary starts a new trace ([observability §3.3](../docs/concepts/observability.md#33-w3c-boundaries)),
and the Harness exported no internal span in dev on 2026-09-29, so the summary reports both rather
than requiring them. Missing service support,
missing trace topology, unavailable evaluation records, or an exact query that the current deployment
cannot perform returns a specific `pending` stage; none is converted into success. The API role must
hold no AgentCore Allow, carry the explicit AgentCore Deny, invoke only the route exchange, and hold
no direct measurement invocation, command, or turn-evidence authority.

F-23 telemetry custody is reported as a separate pre-customer-traffic result. The runner records exact
log-group retention, KMS association when present, class, and subscription-filter configuration only
when those values are discoverable from the named deployment or returned trace. It does not infer
protected reader authority, service encryption guarantees, or browser/application export absence from
a missing field. Unresolved custody remains `pending` and keeps customer traffic prohibited without
blocking fictional development acceptance or legacy replacement.

Raw deployment records, IAM policies, tokens if ever retained, Harness events, SSE, logs, spans, audit,
and evaluation material may exist only in the new ignored `validation/evidence/managed-harness.*`
directory. A complete pass removes it unless `RETAIN_EVIDENCE=1`; failed or pending runs retain it for
authorized review. Standard output is one bounded sanitized JSON object containing only closed stage,
status, reason, outcome, and count values. Read retained evidence before recording any dated sanitized
conclusion in `RESULTS.md`.

## Running route Guardrail acceptance

Run after every `Adlytics-Agent-dev` deploy, because a deploy is what changes the guardrail:

```bash
make accept-guardrail PROFILE=<profile> ENV=dev REGION=us-east-1 ACCOUNT=<account>
```

It sends the 54 fictional cases to the deployed guardrail at `DRAFT` and judges each by the `action`
the service returned. The bounds are the calibrated result recorded in `RESULTS.md` (Inbound
Guardrail calibration, 2026-09-30): Standard tier, all 38 benign and 16 attack cases replayed, no
benign case blocked, and at most one attack missed, from the jailbreak family the calibrated miss
came from. A failure prints which bound broke and, for a blocked benign case, its family and the checks
that fired. It creates nothing. Its 15 or 16 interventions in a few minutes raise the
`guardrail-intervention-rate` alarm (0.278 on 2026-10-01), and the default calibration mode does
the same because it also replays the deployed guardrail. The alarm notifies only on a change of
state, so for up to an hour after a replay a real surge sends no further notification: read the
`input_guardrail` lines directly in that window.

The guardrail profile's destination Regions are hard-coded in
`infra/lib/constructs/agent/route-guardrail.ts` from the AWS page it cites. A destination AWS adds
later is not in the Conversation function's grant, so a route assessment sent there would be
denied. Re-read that page when this acceptance is run after a long gap.

## Retired Runtime observability probes

The direct Runtime observability probe and its custom AgentCore Runtime stack were removed with the
managed Harness adoption (stack removed 2026-09-07, probe removed 2026-09-14); neither the
`probe-runtime-observability` nor the `probe-conversation-runtime-observability` Make target exists.
`probe_conversation_runtime_observability.py` still accepts only the retired `Adlytics-AgentRuntime-dev`
stack name, so it cannot run against the current deployment. Earlier dated conclusions remain in
`RESULTS.md` as history; current joined acceptance is
[Running joined managed Harness acceptance](#running-joined-managed-harness-acceptance).

## Running endpoint evaluation

The V4 endpoint evaluation runner is retired; see [evaluation/README.md](../evaluation/README.md).

## Running source validation

Source validation requires credentials for an AMC account the operator is authorized to inspect.
Keep credentials in ignored local configuration or an explicitly selected Secrets Manager secret;
never commit them or live probe output. The first-time path below is also the repeatable setup path
for a new maintainer or environment.

**One. Mint and store a refresh token.** A human must approve a consent screen while signed in as
an authorized user. The script resolves the explicit AWS profile first, displays the caller
identity and destination secret, and requires the operator to type `create`. It refuses an existing
secret, then sends the token and prompted client secret directly to Secrets Manager without printing
or writing either credential locally.

```
uv run python -m validation.mint_token \
    --client-id amzn1.application-oa2-client.xxxx \
    --region na \
    --redirect-uri https://amazon.com \
    --profile <profile> \
    --secret-name adlytics/<env>/amc/api-credentials
```

The `redirect_uri` must match one of the app's Allowed Return URLs byte-for-byte; a trailing slash
is a difference. The region follows the advertiser's marketplace, not an AWS region. A wrong region
returns `invalid_grant`.

**Two. Discover what the token reaches.** The new secret intentionally leaves marketplace, account,
and instance identity fields empty. Run discovery through that exact named secret:

```
uv run python validation/probe_amc.py \
    --secret adlytics/<env>/amc/api-credentials \
    --profile <profile> \
    --discover-only
```

`GET /amc/instances` returning empty means there is no AMC behind this app. Update the three blank
identity fields through the Secrets Manager console after reviewing discovery; do not retrieve or
copy the credential fields into a local file.

**Before spending a token, check request shapes.** A maintainer with existing ignored local
credentials may print redacted, curl-compatible request shapes without sending them:

```
set -a; source validation/.env.local; set +a
uv run python validation/probe_amc.py --from-env --print-requests
```

If token minting fails, `invalid_client` means the client ID or secret is wrong; `invalid_grant`
means the code is wrong or expired, the redirect differs, or the region is wrong.

**Three. Run the source probes.**

```
uv run python validation/probe_amc.py \
    --secret adlytics/<env>/amc/api-credentials --profile <profile>
```

Governed pack validation uses the already sourced local credentials and explicit half-open UTC bounds. It journals every execution before polling and recovers before submission:

```bash
uv run python validation/probe_amc_query_packs.py \
  --instance-type STANDARD \
  --from <YYYY-MM-DDT00:00:00> \
  --to <YYYY-MM-DDT00:00:00>
```

The non-additive reach policy is grounded separately:

```bash
uv run python validation/probe_reach_suppression.py \
  --from <YYYY-MM-DDT00:00:00> \
  --to <YYYY-MM-DDT00:00:00>
```

Deduplicated attribution viability is probed one case at a time, Sandbox before Standard. The
script journals every execution and recovers before submission:

```bash
uv run python validation/probe_deduplicated_attribution.py \
  --instance-type SANDBOX \
  --case temporal-model-reconciliation \
  --from <YYYY-MM-DDT00:00:00> \
  --to <YYYY-MM-DDT00:00:00>
```

### Business-outcome probes (P-BO-1 step A, P-BO-2)

These settle AMC source semantics for the business-outcomes design. Its §8 owns every query, pass
criterion and outcome; the probes implement it and don't restate it. Both scripts submit one query
per run, re-discover the instance, adopt a journaled or historical execution before they can
submit, journal immediately, and write the terminal record, CSV and a count-only summary to
`findings/`. Re-running a command adopts its journal; it never resubmits. Bounds are explicit,
half-open UTC midnights, inside the 13-month retention and at least 48 hours old. No operator
identity, instance, campaign or marketplace value is built in.

**P-BO-1 step A** (`probe_bo_suppression_exactness.py`) runs on SANDBOX, because Q2, Q4, Q6,
Q8 and Q9 need aggregation controls off; the shared module refuses controls off anywhere else.
The Sandbox has no `dsp_impressions.campaign_country`, so the controls-on fence queries (Q3, Q3m,
Q5, Q5r) may take `--instance-type STANDARD`, which is validation-instance evidence; their
controls-off halves have no step A path. That schema fact comes from its own one-column probe,
`probe_bo_campaign_country_schema.py`, not from Q3's broad rejection. It reads
`COUNT(campaign_country)` from plain `dsp_impressions` on the Sandbox, controls on, over Q3's week.
Only if that succeeds does it read the same column through `EXTEND_TIME_WINDOW`, to separate "absent
from the table" from "not resolvable through the table function". It writes
`findings/bo-cc-verdict-<stamp>.json`:

```bash
uv run python validation/probe_bo_campaign_country_schema.py \
  --from <Monday>T00:00:00 --to <next Monday>T00:00:00
```

```bash
uv run python validation/probe_bo_suppression_exactness.py run \
  --instance-type SANDBOX --query Q0 \
  --from <Monday>T00:00:00 --to <next Monday>T00:00:00
uv run python validation/probe_bo_suppression_exactness.py evaluate \
  --week-from <Monday>T00:00:00 \
  [--month-from <YYYY-MM-01T00:00:00>] \
  [--cohort-from <YYYY-MM-01T00:00:00> --cohort-to <YYYY-MM-01T00:00:00>]
```

- Windows: Q0–Q6 and their re-runs use one ISO week, shared by every week query in an evaluation;
  for Q3–Q6 the week must end 14 days plus 48 hours before today so the window extension is
  complete. Q3m/Q4m use one calendar month. Q7–Q9 use whole months, at most 13.
- Q3–Q9 also need `--marketplace-name`, `--dsp-campaign-country` (Q3–Q6) and `--as-of-month`
  (Q7, Q8: the month before `--to`, as `YYYY-MM-01`), taken from local evidence and never
  committed. They travel as `CUSTOM_PARAMETER` values, never templated into SQL. The ad hoc form
  was confirmed by `probe_bo_parameter_shape.py`, one Sandbox execution that passes only if the
  supplied value comes back:

  ```bash
  uv run python validation/probe_bo_parameter_shape.py \
    --from <Monday>T00:00:00 --to <next Monday>T00:00:00
  ```
- Run order, each to its terminal state first: Q0 (Q0alt only if Q0 is `REJECTED`); Q1, Q2; Q3, Q4;
  Q5, Q6, Q6r; Q7, Q8; Q9 (Q9alt only if criterion 10 fails); then `evaluate`. Re-runs fire only on
  their trigger: Q1r/Q2r if criterion 0 isn't exercised, Q3m/Q4m for criterion 8, Q5r for 0b.
  Budget: ten base executions plus at most seven re-runs. A differencing-guard or compile `REJECTED`
  is recorded, never worked around by varying a filter.
- `evaluate` makes no AMC call. It reads only summaries whose SQL digest matches the current text,
  and it refuses two instances of one type. It may read queries from both instance types, but it
  compares a controls-on result with its controls-off reference (Q1/Q2, Q1r/Q2r, Q3/Q4, Q5/Q6,
  Q5r/Q6r, Q7/Q8) only when both came from the same instance. A pair from two instances is
  recorded as not run (`instance_mismatch`). It writes `findings/bo-p1-verdict-<stamp>.json`.
  Exit 0 = all pass, 2 = some inconclusive, 1 = a fail or defect.
- History recovery looks back 7 days by default. To re-run a command whose execution is older and
  whose journal is lost, pass `--recover-since <YYYY-MM-DDTHH:MM:SS>` from before the original
  submission. Otherwise recovery misses it and the probe resubmits. This applies to every P-BO
  probe.

**P-BO-2** (`probe_bo_segment_expansion.py`) runs Form B (`EXPLODE`, the documented array
function) and the `dsp_impressions_by_matched_segments` cross-check over one calendar month, with
controls on, on SANDBOX first and then STANDARD:

```bash
uv run python validation/probe_bo_segment_expansion.py run \
  --instance-type SANDBOX --case expansion --form B \
  --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
uv run python validation/probe_bo_segment_expansion.py evaluate \
  --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
```

**Forced suppression** (`probe_bo_forced_suppression.py`) runs two deliberately sparse
`dsp_impressions` groupings over one day, so one-user cells must occur: `sparse_source` (six LOW
source columns, standing where Q1/Q2 stood) and `sparse_derived` (a per-shopper frequency label
derived in a CTE). Each runs controls on, and controls off on SANDBOX only as the same-instance
reference. `evaluate` applies criteria 0–4 to each pair through `bo_probe_criteria.py`, records the
suppression output shape, and writes `findings/bo-force-verdict-<stamp>.json`. Neither variant is
pack text, so neither settles 0b or 0c:

```bash
uv run python validation/probe_bo_forced_suppression.py run \
  --variant sparse_source --controls on --instance-type SANDBOX \
  --from <YYYY-MM-DD>T00:00:00 --to <next day>T00:00:00
uv run python validation/probe_bo_forced_suppression.py evaluate \
  --from <YYYY-MM-DD>T00:00:00 --to <next day>T00:00:00
```

**Paid-feature presence** (`probe_amc_paid_feature_schema.py`) reads, controls on, one aggregate
per paid table the contracts and packs use (ARP, the ASI snapshot tables, `segment_metadata`,
`conversions_all`). A `table` probe reads one column; a `columns` probe reads every column the packs
use. Each verdict is `resolves`, `table_unavailable` (rejected naming the table), `column_not_found`
(rejected naming a column) or `inconclusive`. An unavailable table means that customer isn't
enrolled, not a defect: the packs keep their standard-table path. It writes
`findings/paid-schema-verdict-<type>-<probe>-<stamp>.json`:

```bash
uv run python validation/probe_amc_paid_feature_schema.py \
  --probe arp_table --instance-type STANDARD \
  --from <YYYY-MM-01>T00:00:00 --to <next YYYY-MM-01>T00:00:00
```

**Supplementary checks** (`probe_bo_supplementary.py`, owner decisions 2026-10-09) run on SANDBOX
only, each as a controls-on query and its controls-off reference on the same instance:

- The packs 5–7 fence **structural check, not the exact pack text**: Q3s/Q4s are pack 5 and Q4
  with the fence's one `campaign_country` line removed, because the Sandbox schema lacks the
  column. Q4ms is Q4s over a calendar month, only if criterion 8 isn't exercised over the week.
  Every result carries that label, and none counts as the AC-E1.5 pass, which stays "pending Step B".
- Pack 14 at acquisition-date grain: Q7d/Q8d are pack 14 with one added grain key, each shopper's
  acquisition date, so cells are sparse enough to suppress (criteria 0c and 5c).

`evaluate` applies criterion 2 as the owner restated it on 2026-10-08 (source keys NULL, derived
labels may survive, `rows_suppressed` identifies the row) and writes
`findings/bo-supp-verdict-<stamp>.json`:

```bash
uv run python validation/probe_bo_supplementary.py run --query Q3s \
  --from <Monday>T00:00:00 --to <next Monday>T00:00:00 --marketplace-name <name>
uv run python validation/probe_bo_supplementary.py run --query Q7d \
  --from <YYYY-MM-01T00:00:00> --to <YYYY-MM-01T00:00:00> \
  --marketplace-name <name> --as-of-month <the month before --to, YYYY-MM-01>
uv run python validation/probe_bo_supplementary.py evaluate --week-from <Monday>T00:00:00 \
  [--month-from <YYYY-MM-01T00:00:00>] --cohort-from <...> --cohort-to <...>
```

**P-BO-3 and P-BO-4** (`probe_bo_audience_segments.py`) run on STANDARD with controls on. P-BO-3
and P-BO-4 step 1 (once per category table) use one calendar month; step 2 uses one ISO week that
holds the month's first Thursday, the ASI snapshot date. The module docstring records the three
departures from design §8's literal text (the `_snapshot` tables the packs read, the documented
`segment_marketplace_id = 1`, and no `CAST`). P-BO-3's regex candidates go to
`findings/bo-aud-pbo3-candidates-<stamp>.json`; a reviewer records the confirmed ones in
`findings/bo-aud-pbo3-review.json` before `evaluate` counts them. It writes
`findings/bo-aud-verdict-<stamp>.json`:

```bash
uv run python validation/probe_bo_audience_segments.py run --query pbo4-step1-inmarket \
  --from <YYYY-MM-01T00:00:00> --to <next YYYY-MM-01T00:00:00>
uv run python validation/probe_bo_audience_segments.py evaluate \
  --month-from <YYYY-MM-01T00:00:00> --week-from <Monday>T00:00:00
```

**Evidence limits.** A Sandbox result settles function semantics, suppression mechanics and SQL
construction only; it is `sandbox_provisional` for admissibility. A STANDARD run on the internal
demo instance is validation-instance evidence of admissibility, never customer volume. Neither
settles suppression rates, loss ratios, the differencing guard at schedule scale or timeouts at
volume; those belong to Step B on a customer instance under the customer deployment's identity.
Segment IDs and row values stay in `findings/`; `RESULTS.md` records outcomes and AMC facts only.

## Running the authenticated browser acceptance

`probe_frontend_browser.mjs` drives the deployed frontend as a logged-in user and fails on any
accessibility violation, unexpected request failure, console error, or missing composition.
Its last step opens an unknown path signed in and checks the not-found page, then opens
`/campaigns/index.html` and requires the Campaigns page. It then opens an unknown path in
a fresh signed-out context, checks that the served document is the not-found page without the
assistant, and checks that the visitor is sent to sign-in (ADR-123 decision 2). Both not-found
pages fail the probe on any page error. It prints the status
the edge returned (`not_found_status`, ADR-123 decision 3), and requires every route in
`contracts/generated/route-allowlist.json` to answer 200.
Finally it opens every allowlisted route the earlier steps don't walk in depth (today Reach,
Retention, Audiences, Scorecard and New customers). Each must have a page API answering 200 with
a page or its designed absence, no page issue, and one status region announcing any absence
message. Each must also be axe-clean in light and dark (`roster_pages=data:N+absence:M`).
The route list comes from `roster_routes.mjs`, which fails the probe when it would be empty or
when a route it excludes is no longer allowlisted; a unit test pins the allowlist's page routes
to the bake roster's pages.
It needs `ADL_BASE_URL` (the deployed origin), `ADL_REFRESH`, and `ADL_CSRF` from
`validation/.env.local` (template and cookie-harvest procedure in `env.example`), plus the
frontend's installed dev dependencies (Playwright and its Chromium: `npm ci` and
`npx playwright install chromium` in `frontend/`).

```bash
set -a; source validation/.env.local; set +a
node validation/probe_frontend_browser.mjs
```

It prints one sanitized acceptance line and never page values; record the dated conclusion in
[RESULTS.md](RESULTS.md).

## Regenerating README screenshots

`capture_screenshots.mjs` rewrites the images in `docs/images/` that the root README embeds. It
takes the same inputs and frontend prerequisites as
[the authenticated browser acceptance](#running-the-authenticated-browser-acceptance). Point
`ADL_BASE_URL` only at a deployment holding synthetic data, because the images are committed.

```bash
set -a; source validation/.env.local; set +a
node validation/capture_screenshots.mjs
```

In place of `ADL_REFRESH` and `ADL_CSRF`, `ADL_SESSION` may carry one access token for a synthetic
dev persona, signed in as `probe_footnote_readback.py` does (`cognito_personas.py`); the script
sends it as the `adl_session` cookie and never prints it.

It exits 0 when every image was captured, 2 when a data-dependent surface was not available and
was skipped (it prints a `SKIPPED` line naming the image, and the old file is left in place), and
1 on anything else, including a page that would show an email address other than the fixture
author's, or the deployment's host. A person views every image before committing it.

`ADL_CAPTURE_SKIP` takes a comma-separated list. `campaign-detail` leaves out campaign detail (the
Sponsored Products and Amazon DSP details and their close-ups) when that surface is broken on the
target. `prompt-administration` leaves out the two Prompt administration images when the
signed-in user is not in the prompt-admin group. Each is reported as `SKIPPED` and the run exits 2,
so a stale image is never mistaken for a fresh one. A detail whose delivery reads "Not enough data
for this window" fails the run.

**Last capture, 2026-10-23.** The images the root README embeds were captured from 02:50 to 02:53
UTC with `node validation/capture_screenshots.mjs`, signed in through `ADL_SESSION` as the
synthetic dev persona `probe-a`, with `ADL_CAPTURE_SKIP=prompt-administration`. The target was the
dev deployment's website origin, holding synthetic data and serving the UI refresh that `main`
carries at `5f48abb6`.
- Exit status: 2, for the two Prompt administration images, skipped because `probe-a` is in no
  Cognito group. Those two files are from the 2026-10-19 capture, whose sidebar has no Overview item.
- 31 images were written, `docs/images/` totals 5,473,441 bytes within the 6 MiB budget, and the run
  logged no browser failure. The metric and insight focus dialogs are now taken at CSS scale.
- Visual review: the operator agent that ran the capture viewed every image before commit. Each
  shows synthetic data, with no error, skeleton or "Not enough data" state, except the designed
  absences the README captions as such: Gateway products, New customers' missing new-buyer week,
  Reach "last quarter" and the per-channel frequency cells.
- **Re-capture of the UI refresh's third tier, 2026-10-23 20:50 UTC.** Same command, persona and
  skip, against the same dev origin, which then served the tier 3 UI (`feat/ui-refresh` with
  `public/main` `776b0ce6`). Exit 2 for the same two Prompt administration images. The script now
  waits on the Growth check's answer (the tab question moved to navigation), names Reach's
  renamed "When more ads stop paying" section, opens Audiences' per-campaign disclosure before
  its close-up, and writes `products-and-ads.png`. Committed: the 21 images of pages tier 3
  changed plus `products-and-ads.png`; the metric and insight dialogs, `insight-cards.png`,
  `campaign-delivery.png` and the three chat images were left from the 02:50 capture, because
  tier 3 didn't change those surfaces. The operator agent viewed each committed image: synthetic
  data only, no email address, host name or token, and no error or skeleton state beyond the
  designed absences the README captions. `docs/images/` totals 5,751,459 bytes within budget.
- `reach-absence-light.png` and `reach-absence-dark.png` belong to
  [page absence copy](../docs/ui/page-absence-copy.md), not the README, and this script does not
  write them.

## Producing the per-stage latency picture

`probe_stage_latency.py` runs the governed CloudWatch Logs Insights queries that turn the
deployed structured events into one aggregate per-stage latency picture — ingestion (per
delivery, Lambda report duration), insight precompute (per run), bake (the baker's
`preset artifact bake finished` event splits `source_ms` — window resolve, per-pack
snapshot, Athena measurement batch, render — from `persist_ms` — body/custody writes and
the pointer advance), and the serve path (the presentation lifecycle `duration_ms` per
operation). Reprocessing telemetry has its own lifecycle-to-trace acceptance probe in
§"Running retained Semantic V2 reprocessing acceptance".

```
uv run python validation/probe_stage_latency.py --profile <profile> --env dev --hours 24
```

Every target-affecting input is an argument. The output is aggregate counts and percentile
durations only — no identifiers, names, or measured values — so a scale-seeding run has a
pre/post baseline without ad-hoc log spelunking. Record dated conclusions (for example the
pre-scale bake baseline) in [RESULTS.md](RESULTS.md); the bake envelope itself is owned by
the campaign-portfolio operational design §1.

## Shipping an insight-slot prompt through the deployed registry

`probe_prompt_registry.py` is the only way a prompt reaches an environment (ADR-069): it
composes create-version → calibration → activate from the route grammar in the
[prompt-administration LLD](../docs/ui/prompt-administration/low-level-design.md) §1. It reads
the same `ADL_BASE_URL`, `ADL_REFRESH`, and `ADL_CSRF` as the browser probe (the logged-in
session must hold the `prompt-admin` entitlement), takes the slot, the prompt file, and the
expected active version explicitly, and refuses to activate unless every fictional calibration
case passed. Author the prompt file under `validation/evidence/` (ignored custody); it prints
no prompt text or cookie value.

```bash
set -a; source validation/.env.local; set +a
uv run python validation/probe_prompt_registry.py --slot retail-metric-focus \
  --prompt-file validation/evidence/prompt-retail-metric-focus-v2.txt --expected-active 1
```

A new slot's seeded v1 must exist first: deploy `Adlytics-InsightPromptRegistrySeed-<env>`
after adding the contract, since its custom resource re-fires on the contract-set digest.

## Running retained Semantic V2 reprocessing acceptance

`probe_reprocess_telemetry.py` is the reusable dev-only operator path. It refuses any environment
other than `dev`, any region other than `us-east-1`, and any stack names other than the exact Data
V2 and Observability dev stacks before constructing an AWS session. It verifies the expected STS
account, exact stack ownership of the raw bucket and Transaction Search controls, and the active
CloudWatch Logs trace-segment destination before running reprocessing.

The probe runs the dry plan itself, requires exactly one digest and at least one exact delivery,
then runs the digest-bound apply with tracing through the same Python executable. It accepts only a
completed, monotonic structured lifecycle joined to one exported `aws/spans` trace whose business
span hierarchy and attributes conform to `contracts/observability/spans.yaml`. Athena statement
spans must include queue, planning, engine, and total timings plus the query reference, but no SQL,
parameters, or rows. The console result contains only classifications and counts; raw plans,
subprocess streams, stack responses, and span documents remain under ignored
`validation/evidence/`.

Supply every target and replay-scope value explicitly. `RETRIEVED_TOKEN` is required so acceptance
cannot silently widen to all retained history. Repeat a run only with the same operation and scope;
when continuing a failed execution, add its exact lineage as `PREDECESSOR_EXECUTION_ID`.

```bash
make accept-reprocess-telemetry \
  PROFILE=<profile> ENV=dev REGION=us-east-1 ACCOUNT=<approved-dev-account> \
  DATA_STACK=Adlytics-DataV2-dev \
  OBSERVABILITY_STACK=Adlytics-Observability-dev \
  BUCKET=<exact-raw-bucket> \
  WINDOW_START=<YYYY-MM-DD> WINDOW_END=<YYYY-MM-DD> \
  OPERATION_ID=<stable-operation-id> CONTRACT=<contract-id> \
  RETRIEVED_TOKEN=<YYYYMMDDTHHMMSSZ> \
  PREDECESSOR_EXECUTION_ID=<optional-prior-execution-id>
```

The Make target creates a unique ignored evidence directory and never removes it. A failed or
pending result retains the same evidence but does not claim trace acceptance. Review the local
files for diagnosis; publish only a sanitized, dated conclusion after an actual acceptance run.

## Running deployed Semantic V2 ingestion acceptance

This probe is **V2-only**. It has no `--plane` switch and cannot exercise V1 or rollback. Supply all
four inputs explicitly; none has a default:

```
make accept-ingestion-v2 \
    PROFILE=<profile> ENV=dev DAY=2026-08-23 GLANCE_VIEWS=85
```

The target invokes:

```
uv run python validation/probe_ingestion_cutover.py \
    --profile <profile> --env dev --day 2026-08-23 --glance-views 85
```

`--profile` selects the named AWS profile, `--env` selects the deployed environment (the probe
currently accepts only `dev`), `--day` gives the synthetic delivery its deterministic day, and
`--glance-views` supplies the detail and reported-total value. The Make target refuses before
invoking Python if any input is absent.

The probe's first AWS API call is STS `GetCallerIdentity` through the explicit profile. Only after
printing that identity does it resolve the exact V2 SSM parameters. It never lists account
resources.

Acceptance requires all of the following:

- the data object is created immutably before the manifest marker, both writes return a `VersionId`,
  and an existing key is accepted only when its bytes are identical;
- the manifest binds the exact data-object version and checksums;
- the deterministically derived attempt for each traffic contract reaches `SUCCEEDED` for the exact
  expected partition;
- each exact `CURRENT` pointer changes from its pre-landing snapshot and names that attempt and
  partition; and
- the exact ingestion DLQ is never consumed or cleared and its total visible, in-flight, and delayed
  depth remains unchanged when readable. Historical messages are allowed.

This is deployed acceptance against synthetic Vendor Central input, **not a real AMC probe**. Exact
recorded runs belong in [RESULTS.md](RESULTS.md).

## The source probes, and what each one changes

Aggregation controls are left **on** for all of them. A question about whether a column
may be selected only means something under the rules a real advertiser instance
enforces; turning them off answers a different question.

| Probe | Settles |
|---|---|
| `name_columns_outputtable` | **15 disputed entries.** Whether `advertiser` and `campaign` are `INTERNAL` (amc.md §4) or `LOW` (seven data-source pages). If `LOW`, amc.md §9 is wrong and a third-party campaign-metadata source becomes unnecessary. **Ran 2026-08-19: they are `LOW`.** The count read 20 before transcription finished; the registry holds 15 |
| `dsp_views_event_type_values` | `VIEWABLE_IMP` (amc.md §3) versus `VIEW` (the page). A filter on the wrong literal returns zero rows rather than erroring |
| `conversions_all_money_scale` | **3 disputed entries.** Whether `combined_sales` and friends are scaled integers. Compare against `total_product_sales`, which every page types as a decimal. Blocks `amc-organic-vs-ad` |
| `winning_bid_cost_exists` | Absent from the schema page, used by a live playbook. Success removes it from `do_not_model` |
| `arp_available_in_sandbox` | The sandbox coverage table omits ARP entirely. Success means the 26-vs-27 column count and the missing-revenue-column finding are settleable without production |

## Two things the sandbox cannot answer

**SQL admissibility.** Sandbox accepts non-aggregated queries that a real advertiser
instance rejects, so a query passing here is not evidence it is production-legal.

**Volumetric grain feasibility.** Synthetic data has no realistic distribution and
Amazon states outliers are not present, so how many rows a grain loses at this
advertiser's volume stays unknown.

## When a probe answers something

Fold it into `contracts/shared/amc-columns.yaml` and `docs/sources/amc.md` in the same
change, and drop the `status: disputed` marker. A probe result sitting only in
`findings/` has not been recorded — that directory is gitignored, so it is scratch.

## Running the local campaign-estate scale acceptance

Run `make accept-campaign-estate-local`. The target expands the governed bulk catalog, renders
one 1,000-campaign segmented publication through the production source, and attests the complete
parent/sibling descriptor fan-out and per-body byte cap. It prints the measured duration, body
count, and maximum encoded body size, and retains the raw JSON only beneath the ignored
`validation/evidence/campaign-estate.*` directory. This local operation performs no AWS calls and
does not establish deployed acceptance.
