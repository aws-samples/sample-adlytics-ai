# Demand forecast slice 3: delivery plan

| | |
|---|---|
| **Level** | Implementation plan for the [Read API and page](low-level-design.md#read-api) of the weekly retail demand forecast |
| **Status** | Built locally, 2026-11-17; not deployed or live-accepted. Deploy and dev acceptance follow slice 2's merge |
| **Owns** | Slice 3's part order, files, gates and commit subjects, and why each part departs from the reviewed design |
| **Parent** | [Forecasting](README.md); decision [ADR-163](../../decisions/adr-163-serve-demand-forecasts-and-status-through-a-read-api.md) |

## 1. Rules

- **Contracts first.** Each change starts at its contract, then runs that contract's generator; a
  generated file is never hand-edited.
- **One commit per part**, after the part's gate passes. `make check` is green after every part.
- **No figure computed at read time.** Readers return stored values; the only arithmetic is server
  display rounding of one stored value (ADR-084).
- **Every failure path has a specific code** from `ADLY-FORECAST_READ-0001` to `-0011`.
- **No AWS call in the build.** Deploy, live denial checks and dev acceptance run after slice 2
  merges.

## 2. Parts

| # | Part | Main files | Gate | Commit subject |
|---|---|---|---|---|
| 1 | Record the design and this plan | ADR-163, ADR-115 amendment, forecasting LLD and architecture | `make check-links check-diagrams check-design-manifest`, `make check` | `docs: record the demand forecast read API, page design and slice 3 plan` |
| 2 | Read contract and generated schema | `src/domain/forecasting/read_api.py`, `summaries.py`, `list_pages.py`, the schema job, wire fixtures | `make check-forecast-schemas`, the read-contract tests, `make check` | `feat: add the demand forecast read contract and its generated schema (slice 3)` |
| 3 | Store readers and the stored list row | `ports/outbound/forecast_store.py`, `demand_forecast_store.py`, the publisher's shard step | store read tests, `make check-aws-pagination`, `make check` | `feat: store each product's list row and add the forecast store readers (slice 3)` |
| 4 | Reader settings and the shared read service | `adapters/config.py`, `adapters/inbound/forecast_read/service.py` | config and service tests, `make check-config-access`, `make check` | `feat: add the shared demand forecast read service with request deadlines (slice 3)` |
| 5 | Spans, the forecast qualifier and the HTTP handler | `spans.yaml`, `publication.py`, `forecast_read/handler.py`, the deep-input revision | `make check-trace-contract`, handler tests, `make check` | `feat: serve the demand forecast and its status on two read routes (slice 3)` |
| 6 | The four chat tools | `tool_handlers/schemas.py`, `forecast.py`, `forecast_entrypoint.py`, the prompt, regression set v3, the tool step labels | `make check-tool-schemas check-prompt-text`, tool tests, frontend and infra tests, `make check` | `feat: add the four demand forecast chat tools over the read service (slice 3)` |
| 7 | Navigation and the page | `navigation.yaml`, `navigation.py`, `frontend/lib/demand-forecast.ts`, `DemandForecastView.tsx`, `DemandForecastAsinDetail.tsx` | `make validate-page-schemas`, frontend tests, lint and build, infra tests, `make check` | `feat: add the Demand forecast page in the Retail hub (slice 3)` |
| 8 | Infrastructure | `api-stack.ts`, `agent-stack.ts`, `harness-proof-stack.ts` | `make infra-test`, `make check-alarm-coverage`, `make check` | `feat: deploy-ready read route, forecast tool target and Harness tool list (slice 3)` |
| 9 | OQ-29: derive the Retail health body cap | `page_context.py`, `artifacts.py`, `preset_artifacts.py` | Retail health and page-context tests, `make check-bakeability`, `make check` | `fix: derive the Retail health body cap and fit the served parent in one item (OQ-29)` |
| 10 | Remaining docs and dispositions | forecasting ADRs, capability and runtime docs, security dispositions | `make check-links check-diagrams check-design-manifest check-coverage-matrix`, `make check` | `docs: forecasting ADRs, capability, runtime and disposition updates for slice 3` |
| 11 | Full gates on the final tip | none | `make verify-clean`, `make release-gate`, the slice's named checks | none unless a fix is needed |

## 3. Where the build departs from the reviewed design

| Topic | Built | Why |
|---|---|---|
| Latest outcome on `CURRENT` (`last_outcome`) | Not built. Status reports `CURRENT.last_attempt` as "Last forecast run"; `hold` is `quality` or `none` | It would change the publisher's outcome transaction, which slice 2 owns |
| Reader client retries | One attempt per call; each round runs in its own short-lived thread pool | A blocked read can't hold a later request on a warm function, with no busy-state tracking |
| Instants | Whole-second UTC with `Z` | The page's existing instant formatter reads them unchanged |
| Money display | Two places (half up) for money targets, whole numbers for units | ADR-084 renders money at currency scale |
| Currency refusal | Only when the selected rows carry more than one currency | Tools don't read `META` for currency, so reads stay at the table above |
| Each item's list row | A read-only `GenerationSeries.roster` property used for both writes | One build, two writes, with no constructor change |
| Retail health served bound (OQ-29) | The 256,000 B body cap stays; the served parent bound is the cap plus its derived growth, and the item check counts UTF-8 bytes | Lowering the cap would refuse bodies that serve today |
| OQ-31 return-rate threshold | Unchanged on this branch; an owner question | Every working fixture change needs a dev re-seed |
| README and solution README | Carried to the run after the rebase onto `main` | The solution README lives on `main`, not on this branch's base |

## 4. Deferred to the deploy run

- Deploy the API, agent, Harness, forecast, frontend and presentation stacks, diff first.
- Publish a slice-3 generation before acceptance, so every `ASIN#` item carries its list row.
- Live anonymous denial on both routes, then slice 3's acceptance on dev with synthetic data.
- The latency budgets at 1,000, 10,000 and 50,000 ASINs (P3-1).
- The store reader integration tests against dev.
