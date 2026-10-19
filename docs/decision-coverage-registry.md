# Decision coverage registry — gap and finding identifiers

| | |
|---|---|
| **Level** | L2 — quality register |
| **Status** | Live register; grows as gaps and findings are recorded, entries never renumber |
| **Owns** | the G-NN, F-NN, and Q-NN identifier inventory and titles: G/F are cited by `@pytest.mark.covers` markers and verified by `make check-coverage-matrix`; Q is citable by ADRs and carries no coverage expectation. Also the ADR → test map for an ADR whose approved design registers it here. |
| **Conventions** | [Documentation conventions](conventions.md) |

Tests cite three decision families with `@pytest.mark.covers`: `ADR-NNN` (owned
by [decisions/](decisions/)), and the `G-NN` gaps and `F-NN` findings below.
This register owns the identifiers and their titles — published because the
recorded gaps and findings ARE the quality posture, as the reviewed scanner
dispositions in [security dispositions](security-dispositions.md) are. An entry marked CLOSED or
RESOLVED keeps its identifier forever; `make check-coverage-matrix` reports
which entries no test exercises.

## Gap register (G-NN)

### G-03 · An added upstream column is tolerated silently, and no contract version is readable beside another

### G-04 · No WAF, rate limiting, or per-user usage admission

### G-05 · No prompt injection defence for data-borne content

### G-07 · No secrets rotation or key management detail

### G-08 · No audit trail for questions asked

### G-10 · No test-data strategy, and no owner for tearing an environment down

### G-11 · No deployment cost model or budget alarming — cost model added 2026-09-04 (README); budget alarming still open

### G-12 · No disaster recovery or backup position

### G-13 · No deployment safety or rollback story

### G-15 · Online evaluation observes the pre-release control envelope

### G-16 · No conversation continuity across session expiry

### G-18 · No model versioning or reproducibility

### G-19 · No cold-start position for new campaigns or ASINs

### G-20 · No accessibility or i18n position

### G-21 · No answer-provenance UI — PARTLY RESOLVED 2026-09-25: a customer-word method strip ships on the insight and metric deep-dive dialogs; chat answers still show none

### G-22 · Seven of the eight dimensions do not exist — count stale: two dimensions exist as of 2026-09

### G-24 · Generated fixtures are not mutually consistent

### G-25 · Mutation review covers fourteen modules of roughly sixty — count stale: 32 reports of 312 modules as of 2026-09-25

### G-29 · A withdrawn grain can leave a row with every measure null

### G-34 · An absent drill-across side has two indistinguishable meanings on a union table

### G-35 · A cross-fact monetary metric cannot state its currency

### G-37 · A catalog answer still reports an empty `contract_ids` — measurement half RESOLVED 2026-08-22

### G-38 · How G-06 is implemented decides whether the platform has any query caching at all

### G-44 · A corrected registry does not correct what the agent already remembers

### G-51 · The agent and its tools act under AWS service roles, not the signed-in user's identity — CLOSED in dev 2026-09-26 (ADR-077): every hop carries the verified user, and each turn's audit receipt names it

### G-01 · No idempotency or replay story for ingestion — CLOSED 2026-08-19

### G-02 · No late-arriving and out-of-order handling beyond restatement — CLOSED 2026-08-19

### G-06 · No tenant or user-scoped data authorization

### G-09 · Transaction Search is an account-wide decision nobody has made

### G-14 · No conversation-level error handling — RESOLVED 2026-09-14

### G-17 · No handling for "the answer is that we cannot answer" — RESOLVED 2026-09-14

### G-48 · The envelope's qualifying metadata never reaches the browser, so the model is its only narrator — RESOLVED 2026-09-14

### G-49 · A fact partition is deleted by the manifest's marketplace and written with the rows' — RESOLVED 2026-09-02

### G-50 · Serialising the load is a configuration value guarding a data invariant — RESOLVED 2026-09-02

### G-23 · `check-lineage` silently skips a ratio over a raw fact column — CLOSED 2026-08-21

### G-26 · Nothing can reset a dev environment's load state — CLOSED for dev 2026-08-20

### G-27 · Two write paths targeted one dev table — CLOSED 2026-08-20

### G-28 · One suppression column, three deliveries — CLOSED 2026-08-21

### G-30 · ROAS cannot be answered until suppression folds over a group — CLOSED 2026-08-21

### G-31 · A suppressed row nulls the grain columns — RESOLVED 2026-08-21, pending deploy

### G-32 · Glue assigns ordinal Iceberg field ids, so physical schema evolution must be append-only — RESOLVED LOCALLY 2026-08-24

### G-33 · An observation records one window per contract

### G-36 · A Gateway Lambda target receives no correlation or session context

### G-39 · No ADOT SDK, so no spans — CLOSED 2026-08-23

### G-40 · A bare `ValueError` from a domain request object reaches Gateway as a broken tool — CLOSED 2026-08-22

### G-41 · The agent cannot be tested end to end without a human completing a browser login — CLOSED 2026-08-23

### G-42 · The model had a working bash shell, because `allowedTools` was never narrowed — CLOSED 2026-08-23

### G-43 · An integration test writes an irreversible freshness observation under a real contract id — CLOSED 2026-08-23

### G-45 · The model adds up rows, and the prompt is the only thing telling it not to — RESOLVED (ADR-049 controls)

### G-46 · A ratio denominated in currency was summed — RESOLVED

### G-47 · A grand total was claimed to cover a population Amazon had withheld part of — RESOLVED

## Finding register (F-NN)

### F-01 · BLOCKER · Vendor Central reports are JSON, not CSV

### F-09 · MEDIUM · Guardrails do not inspect tool output parameters

### F-20 · LOW · Python runtime and other version pins

### F-21 · COMMERCIAL · Three data contracts depend on a free window closing 2026-12-31

### F-22 · HIGH · Ten AgentCore Policy constraints that only a deploy reveals

### F-23 · HIGH · Managed Harness observability retains payload details

### F-02 · BLOCKER · Cookie auth does not work on a browser WebSocket handshake

### F-03 · ~~BLOCKER~~ RESOLVED 2026-08-18 · `SameSite` and default AWS hostnames

### F-04 · HIGH · Powertools Tracer is built on a library losing support

### F-05 · HIGH · Plain Parquet cannot serve restating sources

### F-06 · HIGH · Join keys do not line up between AMC and Vendor Central

### F-07 · HIGH · AMC privacy thresholds forbid row-level curated tables

### F-08 · HIGH · Chronos serverless cold start is ~30s, not 2–5s

### F-10 · MEDIUM · Bedrock Agents Classic is closed to new customers

### F-11 · MEDIUM · AgentCore is larger than the brief assumes, and Harness may replace hand-written orchestration

### F-12 · MEDIUM · Gateway caps tool responses at 6 MB

### F-13 · MEDIUM · Long-term memory extraction is asynchronous

### F-14 · MEDIUM · Athena needs a cost circuit breaker

### F-15 · MEDIUM · Transaction Search is an account-wide change

### F-16 · MEDIUM · Correlation ID alone is not distributed tracing

### F-17 · LOW · EMF is now the "classic" metrics path

### F-18 · LOW · Amplify Hosting is a weaker fit than CloudFront + S3 here

### F-19 · LOW · Lake Formation adds little for a single-tenant deployment

## Question register (Q-NN)

Open questions carry no decision and therefore no coverage expectation (an
unresolved question has nothing to test); their identifiers are registered so
ADRs can cite the question that shaped them. A question that resolves becomes
an ADR or a documented default.

### Q-01 · Which AWS region, and is it fixed per customer? — DECIDED 2026-08-19

### Q-02 · AgentCore Harness, or a repository-owned agent loop? — REVISED 2026-09-01

### Q-03 · Is mid-generation interruption a product requirement?

### Q-04 · Are what-if scenarios in V1 scope?

### Q-05 · Reports API or Data Kiosk for the vendor side? — SETTLED

### Q-06 · What exactly is the Context Engine?

### Q-07 · Which capabilities require a customer-side paid subscription? — REFRAMED

### Q-08 · Single-user or multi-user per customer account? — DECIDED 2026-08-24

### Q-09 · Where do we develop, and against what data?

### Q-10 · Who owns the Transaction Search decision? — SETTLED FOR OUR ENVIRONMENTS 2026-08-19

### Q-11 · What are the O1–O20 business outcomes?

### Q-12 · What latency is actually acceptable?

### Q-13 · The six canonical metric definitions

### Q-14 · What is the presentation layer built from?

### Q-15 · Managed login, or our own login UI? — SETTLED

### Q-16 · Can the same ASIN string appear under two marketplaces?

### Q-17 · How long do we keep Iceberg snapshots, and who deletes orphaned files?

## ADR test map (ADR-NNN)

The ADR owns its decision and what each test proves (its "Enforced by"
section); this map names only where the tests live. Python tests also carry
`@pytest.mark.covers("ADR-NNN")`, so `make check-coverage-matrix` reports them.

### ADR-139 · Serve designed absence, and advance pages only with a grantable cohort

[ADR-139](decisions/adr-139-serve-designed-absence-and-advance-pages-only-with-a-grantable-cohort.md)

- Layer 1, designed absence grants and failures withhold: `tests/unit/test_delivery_cohort.py`,
  `tests/unit/test_presentation_handler.py`
- Layer 2, last good by construction, including the crash-after-commit redelivery (M3):
  `tests/unit/test_graph_bake.py`, `tests/unit/test_presentation_handler.py`
- Layer 3, absence copy and its rendering (M1, M4, M5): `tests/unit/test_page_absence_copy.py`,
  `tests/unit/test_audiences_presentation.py`, `frontend/test/page-absence-copy.test.ts`,
  `frontend/test/presentation-v2-route-controller.test.tsx`, `frontend/test/presentation-v2.test.ts`,
  `frontend/e2e/static-export.spec.ts`
- Layer 4, refusal and withheld alarms and ops check O3: `tests/unit/test_serving_refusal_metric.py`,
  `tests/unit/test_graph_metrics.py`, `tests/unit/test_dev_operations.py`,
  `infra/test/serving-refusal-alarm.test.ts`, `infra/test/ingestion-stack.test.ts`
### ADR-140 · A bake that skips the finalizer still settles its cohort
[ADR-140](decisions/adr-140-a-bake-that-skips-the-finalizer-settles-its-cohort.md)
- The Release's unarmed settlement and its transaction: `tests/unit/test_execution_graph_release.py`,
  `tests/unit/test_execution_graph_release_store.py`
- Cohort eligibility and the withheld stale precompute: `tests/unit/test_graph_bake.py`,
  `tests/unit/test_graph_precompute.py`, `tests/unit/test_graph_whole_chain.py`,
  `tests/unit/test_delivery_cohort.py`
- The blocked gauges' bake anchor and derived alarm windows: `tests/unit/test_stuck_flow_detector.py`,
  `infra/test/ingestion-stack.test.ts`
- A source refusal is designed absence and a failed task or finalizer settles withheld:
  `tests/unit/test_graph_whole_chain.py`, `tests/unit/test_graph_bake.py`,
  `tests/unit/test_graph_precompute.py`, `tests/unit/test_execution_graph_dispatch.py`
