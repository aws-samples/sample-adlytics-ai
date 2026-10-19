# Retention — high-level design

## 4. Solution strategy

One tab, one baked artifact per vintage, served from publish-time artifacts through the
[preset artifact publisher](../preset-artifact-publisher.md). This tab is the only one on the `as_of_vintage`
page time. It serves only the latest vintage and shows the server's static label "Cohorts through {vintage}"
instead of a select (owner decision 2026-10-14; a historical roster is deferred) ([ADR-116](../../decisions/adr-116-give-every-tab-one-time-model-and-one-window.md)). A cohort cell measures
a period, not a state at an instant, so a vintage is a selector on an explicit periodic fact, not a snapshot.
This document records only what's specific to this tab.

### Presentation API role

| Operation | Route | Browser intent | Server responsibility |
|---|---|---|---|
| Retention | `POST /api/presentation/retention` [proposal] | Session only | Serve the latest vintage's baked artifact: the latest `as_of_month` with a current standard-path publication |
| Retention metric focus | `POST /api/presentation/retention-metric-focus` [proposal] | Session, a declared focusable metric, the same selection | Derive the focus payload from the baked artifact |

The URL holds no Retention state; any query string is refused before a fetch. Moving to another tab
carries nothing. Both routes are new API Gateway routes and need their own `infra/` approval.

### Composition (top to bottom, per the mockup)

1. Hub header: "Customers" eyebrow, tab strip (New customers, Retention, Repeat purchase by product), h1
   "Retention", the description "Cohorts of new customers your ads brought in, and when they buy again", the
   static "Cohorts through {vintage}" label, then the scope band naming the cohort months and the repeat months.
2. **Repeat purchase by cohort**: the acquisition-group selector "Acquired through", then the cohort grid as a
   heat map with its table, with the cohort size under each cohort month.
3. The paid path: a path selector when Amazon Retail Purchases has delivered, otherwise the "Available with
   the paid feature" callout.
4. **Repeat curve** (cross-group, account IDs).
5. **Insights row**: the `retention-shift` card.
6. One pointer line: "For all repeat purchases per product, reported by Amazon Brand Analytics, see Repeat
   purchase by product."
7. **About these figures**.

### Data lineage

`fct_shopper_acquisition` (as-of month × cohort month × months since acquisition × acquisition group × cohort
path). The standard pack (pack 14) is its first writer and the ARP pack (pack 15) an optional second, so the
canonical model's "blocked on paid features" row changes with ADR-104 in the S4.1 change. The ARP rates
divide pack 15 repeat buyers by pack 14's account-ID cohort size for the same vintage and cell.

Each vintage carries its cohort grain, month or quarter. Packs 14 and 15 each have a pre-built
calendar-quarter variant that the load switches on per marketplace from the next vintage after any slice of
the pack 14 grid passes the degrade threshold, sticky until Step B. Pack 15 follows pack 14's grain, so an
ARP rate's two operands share it, and a rate whose operands differ in grain is withheld; a quarterly vintage renders its own designed
state ([low-level design](low-level-design.md#quarterly-cohorts-added-at-2026-10-092)).

### Build slices and dependencies

| Needs | Why |
|---|---|
| S4.1 (pack 14, standard path) after S2.1 | The grid, curve and insight on the standard path |
| S4.2 (pack 15, paid) | The paid path |
| P-BO-1 criteria 0c, 5c, 8b, 9, 10 and 11 | The pack's month index, cell thresholds and window checks on a live instance |
| Temporal transport v2 in the browser | The vintage kind of the date control |
| A route approval | The two new routes |
