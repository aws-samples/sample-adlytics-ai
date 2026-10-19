# Mutation sweep status — production Python

- Commit: dcc9ef2c616512637733909738b323935e00b166
- Started: 2026-09-11
- Tool: cosmic-ray 8.7.0
- Mode: no remediation
- Source scope: production Python under `src/`; frontend TypeScript excluded because no JavaScript mutation tool is configured

## Inventory

- 242 production Python modules found.
- 199 modules have a direct unit-test import reference.
- 17 source-module reports existed before this sweep; 188 directly testable modules remained unreviewed after report-name reconciliation.
- 44 modules have no direct unit-test owner and cannot yield meaningful mutation evidence without adding tests or approving an integration-test mutation strategy.

## Completed reports

| Module | Mutants | Killed | Survived | Report |
|---|---:|---:|---:|---|
| `src/adapters/config.py` | 303 | 106 | 197 | `adapters-config.md` |
| `src/adapters/inbound/conversation/block_runtime.py` | 215 | 115 | 100 | `adapters-inbound-conversation-block_runtime.md` |
| `src/adapters/inbound/conversation/contract_codec.py` | 314 | 216 | 98 | `adapters-inbound-conversation-contract_codec.md` |
| `src/adapters/inbound/conversation/turns.py` | 72 | 12 | 60 | `adapters-inbound-conversation-turns.md` |

Each completed report contains its exact test command and line-level survivor/operator inventory. No source or test remediation was made.

## Interrupted module

`src/adapters/inbound/presentation_handlers/handler.py` generated 4,686 mutants. After 20 minutes, 1,238 outcomes had completed; the full per-module run was stopped before completion because the literal 188-module sequential queue would hold the primary workspace for multiple days. Its source was restored immediately; no report was emitted because partial outcome counts are not a valid mutation result.

## Decision needed before resuming

The sweep can continue only under one of these execution policies:

1. **Exhaustive:** run every mutant for every directly testable module sequentially. Highest per-module depth, but multi-day wall time and continuous source mutation in the workspace.
2. **Bounded representative sample:** cap work per module and produce one report for every directly testable module. Highest project breadth, but not exhaustive per-module mutation coverage.
3. **Risk-prioritized exhaustive:** fully mutate the domain and conversation boundaries first, then adapters and ports in later batches. Strongest business-risk signal per unit time, but not immediate whole-project breadth.

No further mutation process is active while this decision is pending.


## Risk-prioritized agent correctness batch

- Commit: `3d5d6eafc55a820911669de5a4c5d67cdc6e5c25`
- Mode: exhaustive per selected module; no remediation
- Status: completed 2026-09-11

| Module | Mutants | Killed | Survived | Report |
|---|---:|---:|---:|---|
| `src/adapters/outbound/agentcore/harness.py` | 778 | 466 | 312 | `adapters-outbound-agentcore-harness.md` |
| `src/adapters/inbound/tool_handlers/catalog_entrypoint.py` | 37 | 32 | 5 | `adapters-inbound-tool_handlers-catalog_entrypoint.md` |
| `src/adapters/inbound/tool_handlers/presentation_entrypoint.py` | 50 | 17 | 33 | `adapters-inbound-tool_handlers-presentation_entrypoint.md` |
| `src/adapters/inbound/conversation/sse.py` | 35 | 28 | 7 | `adapters-inbound-conversation-sse.md` |
| `src/domain/semantics/analytical_request.py` | 680 | 396 | 284 | `domain-semantics-analytical_request.md` |
| `src/domain/measurement/planning.py` | 711 | 489 | 222 | `domain-measurement-planning.md` |
| `src/adapters/outbound/metric_dictionary.py` | 497 | 371 | 126 | `adapters-outbound-metric_dictionary.md` |
| `src/domain/evaluation/envelope.py` | 71 | 31 | 40 | `domain-evaluation-envelope.md` |
| `src/adapters/jobs/evaluation_writer/span.py` | 50 | 22 | 28 | `adapters-jobs-evaluation_writer-span.md` |

All reports retain the exact unit-test command and line-level survivor/operator inventory. Raw survivors are not aggregate quality scores: they include untriaged equivalent mutations and behavior-changing candidates.

### High-risk modules not run

- `src/adapters/inbound/tool_handlers/measurement_entrypoint.py`: its handler constructs Athena and other AWS dependencies at import. It has an integration-only bundle test; mutation would require explicit non-production AWS integration authorization and a dedicated retained integration mutation strategy.
- `src/adapters/inbound/conversation/server.py`: no focused unit or integration test owner was found. Mutation without an owning test would create a meaningless report. Add a focused HTTP/server test owner before mutation.


## Wave 0 test ownership

- Completed: 2026-09-11
- `tests/unit/test_conversation_server.py` now owns HTTP health, authorization denial, pre-open classified errors, inbound Guardrail intervention, runtime admission, chunked release, and terminalization using fictional settings and port fakes only.
- `src/adapters/inbound/tool_handlers/measurement_dispatch.py` now owns the locally testable measurement boundary: declared tool selection, grounded refusal rendering, and semantic request context construction. `measurement_entrypoint.py` remains an AWS-composed integration boundary and is not directly mutation-run without explicit non-production AWS authorization.


### Wave 0 mutation proof

| Module | Mutants | Killed | Survived | Report |
|---|---:|---:|---:|---|
| `src/adapters/inbound/conversation/server.py` | 144 | 59 | 85 | `adapters-inbound-conversation-server.md` |
| `src/adapters/inbound/tool_handlers/measurement_dispatch.py` | 30 | 26 | 4 | `adapters-inbound-tool_handlers-measurement_dispatch.md` |

The direct AWS-composed measurement entrypoint remains an explicit integration mutation gap; the dispatch review covers only the pure local seam.


## Wave 1 publication-path re-mutation

- Commit: `6b8dce22d67ced20b217373154c054a667012f13`
- Mode: retained per-module re-mutation after local adversarial test remediation

| Module | Before: killed/survived | After: killed/survived | Report |
|---|---:|---:|---|
| `src/adapters/outbound/agentcore/harness.py` | 466 / 312 | 481 / 299 | `adapters-outbound-agentcore-harness.md` |
| `src/adapters/inbound/tool_handlers/presentation_entrypoint.py` | 17 / 33 | 46 / 4 | `adapters-inbound-tool_handlers-presentation_entrypoint.md` |
| `src/adapters/inbound/conversation/sse.py` | 28 / 7 | 33 / 2 | `adapters-inbound-conversation-sse.md` |
| `src/adapters/inbound/tool_handlers/catalog_entrypoint.py` | 32 / 5 | 34 / 3 | `adapters-inbound-tool_handlers-catalog_entrypoint.md` |

The Harness timeout type defect was fixed: its target now rejects positive non-integer values as its contract promises. Remaining survivors require equivalence or semantic review; raw survivor counts are not release-quality scores.


### Harness retained survivor triage

- Retained run: 780 generated, 481 killed, 299 raw survivors.
- Classified equivalents: 45 annotation/import mutations.
- Open test gaps: 254 lifecycle, framing, finalizer-attestation, decoding, scalar-wire-validation, or structural API mutations.
- Confirmed defect killed: positive non-integer Harness timeout acceptance; Wave 1 now enforces the declared positive-integer target contract.


### Harness lifecycle-admission re-mutation

- Commit: `c85cc07`
- Before: 481 killed / 299 raw survivors.
- After: 501 killed / 279 raw survivors.
- Result: 20 additional mutants killed across `_stop`, `_message_is`, `_start`, `finish`, and `_index` lifecycle admission guards.


### Harness content-framing re-mutation

- Commit: `bef3ad1`
- Before: 501 killed / 279 raw survivors.
- After: 519 killed / 261 raw survivors.
- Result: 18 additional mutants killed across event cardinality, delta/result framing, finalizer input, scalar index, and session-bound validation.


### Wave 2/3 semantic and evaluation re-mutation

- Commit: `c27c836`

| Module | Before: killed/survived | After: killed/survived |
|---|---:|---:|
| `src/domain/semantics/analytical_request.py` | 396 / 284 | 404 / 276 |
| `src/domain/measurement/planning.py` | 489 / 222 | 489 / 222 |
| `src/adapters/outbound/metric_dictionary.py` | 371 / 126 | 372 / 125 |
| `src/domain/evaluation/envelope.py` | 31 / 40 | 42 / 29 |
| `src/adapters/jobs/evaluation_writer/span.py` | 22 / 28 | 22 / 28 |

The first local resolver/dictionary/evaluation boundary tests killed 20 additional mutants across the batch. Planning and span require retained semantic triage before further tests; raw survivor counts remain backlog signals, not release-quality scores.


### Evaluation fidelity re-mutation

- Commit: `ccdf3e3`
- Envelope: 42 killed / 29 raw survivors → 48 killed / 23 raw survivors.
- Writer span: 22 killed / 28 raw survivors → 42 killed / 8 raw survivors.
- Result: exact grounded-context boundary and OTel projection identity/timing tests killed 26 additional evaluation-plane mutants.


### Final dictionary/evaluation proof

- Commit: `770518a`
- Metric dictionary: 372 killed / 125 raw survivors → 373 killed / 124 raw survivors.
- Envelope: remains 48 killed / 23 raw survivors after exact size and aggregate boundary proof.
- Writer span: remains 42 killed / 8 raw survivors after exact projection contract proof.
- Remaining dictionary/planning survivors are explicitly semantic-triage backlog, not unverified test success.


### Harness finalizer/custody re-mutation

- Commit: `0047882`
- Before: 519 killed / 261 raw survivors.
- After: 526 killed / 254 raw survivors.
- Result: 7 additional mutations killed across finalizer disposition and success/refusal envelope decoding.
