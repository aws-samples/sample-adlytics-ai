# Analytics Discovery — low-level design

This companion owns the concrete declaration, domain, API, page, future orientation, test, and
acceptance contracts for Analytics Discovery. It does not own metric definitions, current customer
coverage, analytical page artifacts, or general conversational publication.

## 6. Interfaces and contracts

### Reviewed taxonomy declaration

A new product-owned declaration at `presentation/discovery/v1/discovery.yaml` defines business-question
categories without copying metric or page truth:

```text
AnalyticsDiscoveryDeclaration.v1 = {
  version,
  categories: DiscoveryCategoryDeclaration.v1[]
}
DiscoveryCategoryDeclaration.v1 = {
  category_id, label, description, order,
  pages: declared destination IDs[],
  surfaced_metrics: catalog metric IDs[],
  prerequisite_tier: "retail" | "advertising" | "paid_feature" | null
}
```

V1 categories are fixed for implementation review:

| Category | Existing destination focus |
|---|---|
| **Advertising efficiency** | Amazon channel performance and campaign attribution detail |
| **Attribution and acquisition** | Channel attribution interpretation and campaign outcomes |
| **Product and retail demand** | Retail product detail |
| **Marketplace comparison** | Marketplace comparison |
| **Data readiness and limits** | Data health detail |

The declaration may surface a short reviewed metric subset for orientation, but it is not a metric
browser. Catalog owns definition/grain/caveat; page declarations own purpose/actions; this declaration
owns only business-question grouping, customer wording, ordering, and direct-destination eligibility.

A build-time compiler validates every reference and emits a packaged manifest plus generated frontend
binding. It rejects duplicate category/page/destination IDs, unknown/deferred metrics, duplicate page
membership unless explicitly allowed, empty categories, invalid prerequisite tiers, and destinations
without a declared static route.

### Pure domain model and assembler

`src/domain/discovery/` introduces pure values:

```text
AnalyticsDiscovery.v1 = { categories: DiscoveryCategory.v1[], declaration_version }
DiscoveryCategory.v1 = { label, description, prerequisite_tier, pages, metrics }
DiscoveryPage.v1 = { destination_ref, action_id, title, purpose }
DiscoveryMetric.v1 = { label, definition, caveat }
DiscoveryScope.v1 = workspace | current_page | declared_destination(destination_ref)
```

The assembler accepts compiled declaration, active Catalog, PageSchemaSet, one attested scope, and an
optional typed availability snapshot. It returns only public labels/text and server-issued destination
references. It rejects raw catalog configuration, canonical IDs, selectors, values, coverage internals,
page routes, model input, and browser input.

V1 is declaration-only: `prerequisite_tier` describes required source capability, not customer-current
availability. It deliberately does not call a selected-current/coverage reader. A future V2 availability
field requires a dedicated `DiscoveryAvailabilityPort` and a separate review of freshness/cache language.

### Discovery destination and future orientation scope

`DiscoveryDestinationRef.v1` is a server-issued opaque token bound in the compiled declaration to one
public action identity and page declaration. It is not a route or page ID. The frontend maps only the
returned action identity through its generated binding.

A separately approved future managed-Harness orientation contract may request a `DiscoveryScope.v1`:

- `workspace` selects the complete discovery declaration and ignores valid page context;
- `current_page` selects exactly the custody-validated page declaration;
- `declared_destination` requires one verified opaque destination reference.

The contract may not accept a page ID, route, catalog selector, availability state, metric list, or action
argument from the model or browser. It must verify scope/ref before shared discovery assembly and return
typed clarification or unavailability on context failure; it must never substitute workspace scope.

### API, frontend, and presentation contract

The proposed ExploreFunction accepts exact `{ "session_id": "…" }` input and returns
`AnalyticsDiscovery.v1`. It does not resolve reporting windows, mint page context, query Athena, or write
state. Its proposed route is `POST /api/presentation/explore` under the existing custom cookie authorizer.

The proposed frontend adds a dedicated `/explore` static route and `ExploreApp` renderer. It consumes a
strict `AnalyticsDiscovery.v1` decoder and generated destination binding. Search/filter is local to the
received model. Every clickable destination is a declared action; no dynamic path construction, raw
route, metric query, or capability request appears in browser code.

A future orientation integration consumes the same compiled manifest directly; it does not call the
Explore API or expose arbitrary navigation strings. The managed-Harness current path does not imply this
future contract exists.

### Telemetry, code, and test plan

Explore telemetry allows operation, declaration version, bounded category/destination counts, action
outcome, and typed error category. It excludes search text, session/owner, destination reference, page
ID/route, metric IDs, customer values, coverage, and request body.

| Boundary | Implementation target |
|---|---|
| Declaration | Discovery YAML, schema, compiler, generated API/frontend manifest, and generation checks. |
| Domain | `src/domain/discovery/` assembler/types and `src/ports/outbound/discovery.py` only if a future availability reader is added. |
| Orientation | A separately approved managed-Harness contract with verified scope/ref and bounded public results; no retired Runtime, prompt, or endpoint package is a dependency. |
| API | ExploreFunction handler, ApiStack route/least-privilege role, strict response codec. |
| Frontend | `/explore`, strict decoder, generated bindings, fixed renderer, safe navigation/actions. |
| Evaluation | No endpoint scenario path is current. Any discovery-specific evaluation requires a separate approved contract. |

Unit tests cover declaration closure, ordering, bounds, assembler scope, no customer payload leak,
opaque destination validation, and V1 declaration-only result. API tests cover exact payload rejection,
no query/window/page-context side effect, and telemetry allowlist. Frontend tests cover strict decoding,
search/filter, action navigation, accessibility, and no dynamic route derivation. Acceptance must cover
authenticated Explore rendering, every declared navigation, and proof that declaration-only discovery
never reports customer values or current coverage.

## 7. Concepts applied

- [Conversational Runtime](../conversational-runtime/README.md) owns the current managed-Harness input and response-publication boundaries.
- [ADR-039](../../decisions/adr-039-assembled-context-replaces-the-declared-prompt.md) and [ADR-058](../../decisions/adr-058-plan-every-turn-and-treat-page-context-as-additive.md) retain superseded context-design rationale.
- [UI page intent validation](../../ui/README.md) owns analytical page purpose and fixed visuals.
- [Agent Quality Loop](../agent-quality-loop/README.md) owns the current immutable audit and protected evaluation linkage.

## 8. Decisions

- Explore is a sibling declaration-only page, not an `AnalyticalPageSchema` with fake metrics.
- ExploreFunction is separate from the Athena-capable Presentation Function to preserve least privilege.
- V1 discovery is declaration-only; current availability is deferred until a typed coverage contract is
  approved.
- Named-page orientation uses server-issued destination references, never model-authored routes/IDs.

## 9. Risks and open questions

- Product review must approve final category wording, surfaced metric subset, and destination placement.
- A direct destination from chat requires a separately approved managed-Harness orientation contract; before then chat may describe a declared page but cannot expose an unsafe route string.
- Discovery availability is intentionally not implemented in V1; marketing language must not imply it.
- The proposal does not select a runtime implementation for orientation; any implementation must preserve the bounded scope and destination rules above.
