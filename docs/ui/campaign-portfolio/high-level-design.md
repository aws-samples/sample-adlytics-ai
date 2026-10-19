# Campaign Portfolio — high-level design

This document owns component topology, request-to-render flow, refresh lifecycle, and V1 → V2 cutover shape. The [README](README.md) owns business scope; [low-level design](low-level-design.md) owns exact wire schemas, artifact fields, custody checks, and tests.

## 4. Presentation topology

**Component diagram — Campaign Portfolio presentation topology, component viewpoint.** The browser supplies user intent only; the Presentation API validates opaque capability custody and produces a typed artifact from governed measurement evidence before the frontend renders it.

![Component diagram: Campaign Portfolio presentation topology. A React campaign route calls an authorized presentation API. The API issues and validates opaque campaign capabilities, reads authorized catalog and measurement evidence, persists artifacts, and returns a typed public artifact to a strict frontend decoder and renderer.](diagrams/campaign-topology.svg)

> **Takeaway.** The browser renders a strictly decoded artifact, while identity, selection, measurement planning, evidence qualification, and artifact lineage remain server-owned.

### Presentation API roles

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| List | `POST /api/presentation/campaigns` | Session, optional preset `TemporalSelection.v1`, optional declared sort. | Resolve the shared control (preset-only), serve the revision-current baked artifact for the resolved variant ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)), order rows, mint opaque capabilities. |
| Detail | `POST /api/presentation/campaign-detail` | Session and opaque selection reference. | Revalidate capability/parent/catalog custody, derive comparator, plan evidence, reduce artifact with insight slots. |
| Metric focus | `POST /api/presentation/campaign-metric-focus` | Session, opaque selection reference, one declared metric slot. | Revalidate the same custody, serve the metric-focus insight from cache or generate it for the capability's resolved window. |

The target `/campaigns/` frontend route may replace `/campaign-detail/` in the clean workspace cutover while retaining these API paths. Any API route change is a separate explicit contract migration.

## 5. Request-to-render and refresh lifecycle

**Call-flow diagram — Campaign list, capability redemption, and render lifecycle.** The browser cannot carry a campaign selector from the list to detail; it redeems one opaque capability that the server binds to owner, session, expiry, parent artifact revision, and current catalog identity.

![Call-flow diagram: Campaign Portfolio list and detail lifecycle. The browser requests campaign choices, receives safe labels and capabilities, redeems one selection reference, receives a typed detail artifact, and refreshes choices when the capability expires or validation fails.](diagrams/campaign-flow.svg)

> **Takeaway.** Detail is reachable only through a currently valid opaque capability; expiry or failed revalidation returns the UI to a newly issued list rather than exposing stale scope.

### Browser refresh and rendering lifecycle

1. The controller loads campaign choices on route, session, or temporal-selection change.
2. Strict decoding determines whether the UI renders loading, choices, empty, ambiguous, insufficient, or classified issue state.
3. An expiry timer disables locally expired capabilities. Manual refresh replaces the complete capability set.
4. Selection retains only the safe signature while detail loads. A generation ticket discards stale async responses, and returned detail identity must match that signature.
5. A capability validation failure refreshes the list. HTTP 401 begins login; HTTP 403 refreshes the session once and retries with rebuilt CSRF/header state.
6. V2 adds delivery modules only after strict V2 decode. DSP quality may render available; Sponsored quality renders unavailable, never zero.

### Atomic V2 cutover

V2 is one compatible release: V2 list issuance, V2 parent artifact, V2 capability redemption, V2 detail artifact, strict decoder, and renderer deploy together. A V1 capability found after cutover is a refresh-list condition, not a compatibility bypass. V1 code is removed only after contract, browser, and non-production acceptance validate V2.

## 5b. Measurement batch execution

The portfolio parent, custody, and ordered-segment artifacts are assembled from one prepared
measurement batch executed **concurrently** under the bound the query configuration owns
(`.kiro/steering/backend-concurrent-execution.md` states the rule; the campaign-detail timeout finding
in `validation/RESULTS.md` records why). The batch is planned and publication-bound up front, and
results join by position.

That portfolio batch runs **at publish time in the Preset Artifact Publisher, never on the portfolio
request path** ([ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md)).
Evidence snapshots are taken per source pack with both comparison windows in one transaction; the
portfolio route serves the persisted artifact and performs no measurement. Displayed-cohort detail
artifact publishing is still a mandated target, not current behavior. Detail redemption currently uses
the live concurrent measurement path: temporarily for displayed campaigns until that target ships, and
as the declared permanent tier for the searchable tail. Campaign Insight Precompute is a separate
stream consumer that independently remeasures evidence to warm model slots; it neither persists page
artifacts nor participates in the portfolio artifact batch.

## 5c. Insight generation topology

Insight slots are revision-bound optional enhancements over the deterministic artifact, not part of the
artifact baker ([ADR-068](../../decisions/adr-068-fill-declared-insight-slots-with-direct-structured-model-calls.md)):

1. A separate publications-stream Insight Precompute worker derives each declared slot's deterministic
   findings from independently measured Campaign evidence and may warm the revision-keyed cache.
2. After deterministic artifact serving resolves the exact revision, the server reads each slot by
   (subject, resolved window, artifact revision, prompt version). A hit fills from cache. An eligible
   miss may issue one bounded Bedrock Converse call or admit continuation; verified results are cached,
   and deterministic materiality may record the slot silent.
3. Neither precompute nor serving guarantees that every preset slot is warm. Pending, unavailable,
   dependency-failed, or generation-failed slots render their designed absence state and never fail or
   delay deterministic page readiness.
4. The prompt each slot runs is resolved through the activation pointer of the prompt registry
   ([ADR-069](../../decisions/adr-069-version-insight-prompts-in-a-calibration-gated-registry.md))
   and the version used is stamped into the cached record. Model routing defaults to the
   configured quick model; a prompt version may carry a routing hint that overrides it.

The metric focus modal introduces no deterministic measurement topology: it renders from the
already-delivered detail artifact, and its single "why it moved" slot follows the same revision-keyed
cache/first-fill/continuation path keyed by metric.
