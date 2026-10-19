# Analytics Discovery — architecture

This companion owns the implementation-ready service topology and flow for the proposed Explore
experience and server-built chat orientation. The [overview](README.md) owns the consumer contract;
exact declaration, scope, and wire rules are in the [low-level design](low-level-design.md). Notation is
owned by the [documentation conventions](../../conventions.md).

## 4. Internal structure

**Component diagram — Analytics Discovery declaration topology, compilation viewpoint, component depth.** The model can select only a validated scope or server-issued destination reference; it never assembles catalog data, pages, or routes.

![Component diagram: Analytics Discovery declaration topology. Three reviewed declaration data inputs compile into a packaged manifest. The Explore API serves the fixed React page, while a future managed-Harness contract may consume bounded discovery data from the compilation assembly.](diagrams/analytics-discovery.svg)

> **Takeaway.** Analytics Discovery is a fixed product surface and a server-built chat result over the
>same reviewed declaration; neither relies on model-generated UI, alphabetical catalog dumping, or a
>claim that a customer's data is currently available.

### Service and IAM boundary

The proposed Discovery capability uses a dedicated `ExploreFunction` in ApiStack rather than extending
the Athena-capable Presentation Function. It serves one protected `POST /api/presentation/explore` route,
reads only packaged catalog/page/discovery declarations, and has logs plus session-authorizer invocation
context. It has no Athena, Glue, S3 semantic data, DynamoDB page-context, forecast, model, or write
authority. CloudFront needs no new behavior because existing `/api/*` forwarding covers the route.

The proposed static frontend adds `/explore` and a generated discovery binding. It renders only the
server `AnalyticsDiscovery.v1` model and performs local search/filter over that model; it does not query
a catalog, derive routes, create pages, or claim availability. A future managed-Harness orientation
contract may use the same compiled manifest directly; it is not permitted to call the Explore API.

## 5. Key runtime flow

1. **Compile declaration.** Build-time validation resolves every category metric against the active
   catalog and every destination against an active page/action. It rejects duplicates, dangling
   references, unbounded category contents, or an undisclosed destination.
2. **Serve Explore.** The authenticated Explore page sends only session identity. ExploreFunction returns
   one declaration-only `AnalyticsDiscovery.v1` response; no reporting window, page context, query,
   artifact, or selected-current read is created.
3. **Navigate.** The browser maps only a generated declared destination action identity to a committed
   static route. Opening a page then follows its normal presentation/page-context lifecycle.
4. **Orient in chat (future contract).** A future managed-Harness orientation contract must define its
   bounded public labels and opaque destination references, then verify `workspace`, `current_page`, or
   one server-issued declared destination before shared discovery assembly materializes a capability block.
5. **Keep availability honest.** V1 discovery states declared support and prerequisite tier only. It does
   not say a customer's data is loaded. A later availability-enabled discovery revision requires a
   separately typed selected-coverage snapshot and a new reviewed contract.

### Deployment and acceptance

The proposal places ExploreFunction and its protected route in ApiStack and publishes the `/explore`
bundle from FrontendStack after the API is available. Any managed-Harness orientation integration remains
a separately approved future contract. No new stack, table, queue, model, or data-reader role is required
for declaration-only V1.

Acceptance must authenticate, render every server-returned category/destination, follow each navigation
action, and verify that declaration-only discovery never reports customer values or current coverage.
