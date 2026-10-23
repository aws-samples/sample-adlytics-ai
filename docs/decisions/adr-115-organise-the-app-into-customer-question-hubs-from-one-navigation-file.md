# ADR-115: Organise the app into customer-question hubs from one navigation file
Date: 2026-10-07
Status: Proposed. Part of the UI and information architecture design. The owner reviewed and approved
its mockups on 2026-10-07. The workflow orchestrator approved the design the same day under the owner's
delegated overnight authorization, and the open choices it contains were taken as recommended, without
owner objection. Not implemented. The proposed design is in
[Information architecture](../ui/information-architecture/README.md), and the build order is in
[Delivery slices](../ui/information-architecture/delivery-slices.md).

> **Amended 2026-10-22 by [ADR-151](adr-151-compose-a-customer-tab-from-served-page-presentations.md):**
> `navigation.yaml` also lists composed customer tabs (`composed_routes`), and the composed Overview
> owns `/`.

## Context

The app has three rendered pages, Campaign portfolio, Retail health and Prompt administration, and
five page entries that are declared but never rendered [repo: `presentation/pages/v2/pages.yaml:57-270`].
The business outcomes design adds about eight more pages and the forecasting design adds a simulator.
A page per outcome would give eight or more top-level items in an internal order. One overview with
drill-downs would put rolling windows, calendar periods, cohort vintages and forecast horizons on one
screen, which the window-scope rule forbids [repo: `.kiro/steering/frontend-ui-standards.md:14-18`].

Navigation is also split today. The shell links Prompt administration by hand
[repo: `frontend/components/AppShell.tsx:211-220`], `declaredPageIds` lists seven presentation pages
and no operator route [repo: `frontend/lib/page-bindings.ts:11-18`], and declared but unrendered
bindings such as `/channel-performance/` would be allowlisted although nothing serves them.

The owner chose to organise the app by customer question, as a small set of hubs with tabs rather than
a page per outcome, and set a slide-out menu below 881 px (owner UX decisions, 2026-10-05).

## Decision

**The app is a small set of hubs, each answering one customer question, plus one operator-only group.
Each hub or group has one or more tabs, and each tab is one page. One governed navigation file is the
only source of hubs, tabs and routes.**

1. **Hubs.** Overview, Performance, Reach and audiences, Customers, Retail health and Plan, plus the
   operator-only Operations group. The roster, tab names and one-line descriptions are in
   [Information architecture](../ui/information-architecture/README.md#2-hubs-and-tabs).
2. **A hub has no route of its own.** Its nav item opens its first tab the session can see, and it
   doesn't remember the last-visited tab. A hub with one visible tab has no tab strip.
3. **A hub is listed only when it has a shipped tab.** Plan's position, after Retail health, is
   recorded in the navigation file, so it appears in place when the simulator ships.
4. **One navigation file.** `presentation/navigation.yaml` lists hubs (`hub_id`, `label`,
   `short_label`, `audience`) and operator static routes with their entitlement. A v2 page entry that
   is a tab carries `navigation: { hub_id, tab_label, tab_description, order }`; an entry without it
   isn't routed.
5. **One generated manifest per consumer.** The generator that writes
   `frontend/generated/page-bindings.json` today [repo: `src/adapters/jobs/presentation_validation/bindings.py:25`]
   also writes `frontend/generated/route-manifest.json` for the shell, and the 404 allowlist
   projection under `contracts/generated/` for infrastructure, where generated contracts that
   infrastructure reads already live [repo: `docs/repo-structure.md:180`]. Both are committed and
   rejected when stale, like `page-bindings.json` [repo: `docs/repo-structure.md:29`].
6. **A build test checks the manifest both ways.** After the static export, the test fails when an
   exported `index.html` has no manifest entry or a manifest route has no exported `index.html`. A
   committed `not_found_outputs` list exempts the not-found outputs, and it's filled from the first
   real export listing.
7. **Entitlements map one to one to session fields.** `prompt-admin` maps to `can_manage_prompts` and
   `operations-status-read` to `can_read_operational_status`. The generator refuses an entitlement
   with no mapping. The API keeps enforcing each entitlement on its own.
8. **Data pipeline status is listed only where it's provisioned.** `/me` returns
   `can_read_operational_status` as true only for a member of the `operations-status-read` group in a
   deployment where the operational status API is provisioned, which is dev only today
   [repo: `infra/lib/config/index.ts:1001-1004`]. A direct visit elsewhere shows "Data pipeline status
   isn't set up in this deployment." with "Go to Overview". `/me` also returns
   `operational_status_provisioned`, which grants nothing, so a direct visit where it's provisioned
   but the session isn't in the group shows "You don't have access to this page" instead. Until
   Overview ships, both links name the root tab, as the 404 page does. Lifting the dev-only guard is
   a separate infrastructure decision with its own cost and retention review.
9. **Navigation by width.** A 232 px sidebar from 1420 px, an 88 px labelled rail from 881 to 1419 px,
   and a slide-out sheet below 881 px. The 881 px breakpoint is fixed by the owner.
10. **Nav links carry a preset only when the user chose it and the target declares it.** Otherwise the
    target opens at its default with no fallback sentence. The fallback sentence stays for explicit
    URL parameters, in-content drill links and next-step targets. The channel filter is never carried
    by nav links.

## Consequences

- One file drives the sidebar, rail, slide-out sheet, tab strips and the 404 allowlist
  ([ADR-123](adr-123-serve-a-real-404-through-a-manifest-generated-allowlist.md)), so no route can
  appear in navigation without a page behind it.
- Prompt administration moves from a hand-written shell link into the navigation file.
- The `/me` session decoder accepted exactly two keys before Slice 11. It now requires exactly
  `subject`, `can_manage_prompts`, `can_read_operational_status` and
  `operational_status_provisioned` [repo: `frontend/lib/session.ts`], and no reader accepts the
  shorter body. This replaces the two-or-three-key dual accept the plan first set, by orchestrator
  approval on 2026-10-08 under the owner's simplified-delivery approval, for the owner to ratify. So
  a two-key server meeting this bundle (an API rollback, a partial API deploy or the frontend
  published first) shows "Session status is unavailable" to every signed-in user on every page
  until the API is fixed forward. The bundle published before Slice 11 refuses the new body, so the
  frontend is published straight after the API, as
  [Delivery slices](../ui/information-architecture/delivery-slices.md#2-rules-for-every-slice) rule 1
  says.
- Hub labels are governed display names under
  [ADR-067](adr-067-release-governed-display-names-never-canonical-keys.md), not frontend strings.

## Alternatives considered

- **A page per business outcome.** Eight or more top-level items ordered by an internal taxonomy.
- **One overview with drill-downs.** Mixes time models on one screen.
- **Customer hubs from page bindings plus a hand-declared operator route list.** Hub facts would live in
  two files with two owners, and the shell would keep two navigation code paths.
- **List Data pipeline status everywhere.** A nav item would lead to a page with no API behind it.

## Review trigger

Reopen when a hub would need more than four tabs, or when a page needs a route outside the manifest.
