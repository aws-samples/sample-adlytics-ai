# ADR-148: Hold a page alarm until an observed success clears it
Date: 2026-10-18
Status: Proposed (2026-10-18). The owner approved fixing gap G1 as an alarm-only `infra/` change on
2026-10-18. This decision also enables route-level API Gateway metrics on the 17 page routes, a
monitoring setting that costs at most $30.60 a month per environment, plus $5.10 a month for the 17
three-metric alarms (Consequences). The owner's confirmation of that cost is pending and gates implementation; acceptance is the owner's review of
this file. Not yet implemented.

## Context
On 2026-10-18 (UTC) the Reach page returned 500 `ADLY-PAGE_CONTEXT-0004` to every caller from about
02:08Z until a fix deployed at 05:32Z. `adlytics-<env>-alarming-errors-by-operation` paged at 02:10Z
and 04:33Z, then returned to `OK` at 02:31Z and 04:48Z while the page was still broken: nobody
called it in between. The alarm has no OK action, so the false clear was not even announced.

Three platform facts make this a design gap, not a threshold choice:

- `Errors` is emitted only on failure (`report_error`, `src/adapters/outbound/telemetry/reporting.py`).
  A successful page request emits no metric that names its `Operation`: on dev the only metric
  carrying `Operation=presentation.reach` is `Errors` [verified 2026-10-18, `list-metrics`].
- In a Metrics Insights alarm that returns several time series, a series the query no longer returns
  "will transition to OK no matter the treat missing data treatment" [verified 2026-10-18,
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html,
  "Missing Data in CloudWatch Metrics Insights alarms"]. No `TreatMissingData` value can hold a
  contributor of the `GROUP BY Operation` catch-all in `ALARM` through silence.
- `ignore` keeps the current state while data is missing, and `notBreaching` treats missing data as
  within the threshold [verified 2026-10-18, same page]. A held state can only clear on a real
  non-breaching data point, which an error-only metric never produces.

An API Gateway HTTP API publishes `Count`, `4xx` and `5xx` per route when detailed metrics are
enabled, with the dimensions ApiId, Method, Resource and Stage [verified 2026-10-18,
https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-metrics.html]. Presentation
answers every `dependency` or `invariant` failure with 503 or 500, and a `validation` or `domain`
refusal with 400 or 409 (`_error_status`, `src/adapters/inbound/presentation_handlers/handler.py`);
the route's authorizer refuses with 401 or 403. A route's `5xx` alone is therefore not a recovery
signal: in a period that answered only refusals, such as one expired-session call, it reports no
server error although nothing succeeded.

## Decision
**A page's broken state is held by an alarm that only an observed success can clear.**

1. Each Presentation page route has its own alarm on its route-level `AWS/ApiGateway` metrics,
   one 5-minute period, at least 1, with missing data `ignore`, acting on the staffed destination
   on `ALARM` and on `OK`. The alarm's value is the route's `5xx` Sum, reported only for a period
   in which the route answered at least one request that it did not refuse with a 4xx
   (`IF((m_count - FILL(m_4xx, 0)) > 0, FILL(m_5xx, 0))`). Any other period yields no data point,
   so the state holds.
2. Only the page routes enable detailed route metrics.
3. The operation catch-all excludes each page operation, so that failure pages once, through the
   page alarm, and the catch-all's clear never speaks for a page.
4. Where a failure is a standing condition of a synchronous surface (it recurs on every call until
   fixed), its alarm holds until an observed success clears it. Where each failure is one item or
   delivery that is retried, dead-lettered or recorded, the alarm keeps arrival semantics
   ([ADR-133](adr-133-bound-every-asynchronous-retry-and-alarm-on-its-failure-destination.md)), and
   its clear means only that no new failure arrived.

Decided by rubric items (1) and (5) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): an operator told that
a broken page recovered stops working on it, and the observed-success clear is the actionable
signal. Item (2) chose built-in AWS metrics, a standard metric-math alarm and a built-in
missing-data treatment over a new emitter.

## Enforced by
Planned with the implementation: a stack test asserts each page route's alarm shape, its
route setting, and both actions; `infra/test/alarm-coverage.test.ts` counts a page alarm as its
operation's own alarm, so the catch-all must exclude exactly those operations. The live acceptance
on dev proves every page alarm reaches `OK` after one successful probe, so a wrong dimension cannot
leave one in `INSUFFICIENT_DATA` unnoticed. That probe runs after the page alarms deploy and before
the catch-all drops the page operations, so a page is never unalarmed if the route metrics are not
published as designed.

## Consequences
- The current page-alarm rule, the page alarms and the catch-all's exclusions are owned by
  [observability §3.2](../concepts/observability.md#32-application-spans-and-metrics).
- An operator learns a page recovered from the alarm's `OK` notification, which follows the first
  5-minute period in which the page answered at least one request without a 4xx refusal and
  returned no 5xx, subject to the publication-skew residual below. A broken page with no callers,
  or whose only callers were refused, stays in `ALARM`.
- Accepted residual: if, within one period, a route's `Count` point is published before its `5xx`
  or `4xx` point, one evaluation can read a served, clean period and notify `OK`; the next
  evaluation returns to `ALARM` and pages if the late point is a 5xx, and if it is not re-evaluated
  the false `OK` stands until the page next fails. No AWS page states the relative timing of HTTP
  API route metrics [unverified], and dev cannot test it without breaking a page. A served-success
  emitter from `src/` removes it.
- A page alarm starts in `INSUFFICIENT_DATA` and stays there until its page first serves a request
  it does not refuse. That is truthful, never observed. Its disposition under
  [ADR-133](adr-133-bound-every-asynchronous-retry-and-alarm-on-its-failure-destination.md) clause 5
  is its route's acceptance probe: a new page route's acceptance calls it once, and until then the
  operator-tools page-alarm entry names it as never called.
- Recovery is proven per route, not per request variant. A page broken only for one selection, such
  as one window, can read `OK` in a period that served only other selections. It returns to `ALARM`
  and pages on the next failing request.
- An intermittently failing page alternates `ALARM` and `OK`, each notified.
- A page whose function fails outright pages through its function `Errors` alarm and through each
  called page's alarm.
- The catch-all, the per-operation `Errors` alarms and every other alarm keep their current
  semantics. The other synchronous surfaces, the non-page HTTP routes, the Gateway tools and the
  conversation turn, still clear on silence; extending the hold to them is open.
- `presentation-serving-refused` is a standing condition that still clears, and announces `OK`, on
  silence. It surfaces as 409, so route `5xx` cannot prove its recovery. Holding it needs a
  served-success signal from `src/` (open).
- Detailed metrics add six route-level metrics for each page route, 102 per environment. At $0.30
  per metric per full month (first tier, US East), that is at most $30.60 a month. Metrics are
  prorated by the hour and billed only for hours with traffic. Each page alarm lists three metrics
  and metric alarms are billed per metric listed, so the 17 alarms cost 17 × 3 × $0.10 = $5.10 a
  month [verified 2026-10-18, https://aws.amazon.com/cloudwatch/pricing/].
- The page operations add 803 characters to the catch-all's query, 1,483 of the 2,048 an alarm
  `Expression` allows [verified 2026-10-18, CloudWatch `MetricDataQuery` API reference]. Excluding
  more operations from it must fit the remainder.

## Alternatives considered
- **`missing` or `ignore` on the catch-all.** Rejected by (1): a contributor that stops being
  returned goes to `OK` whatever the setting.
- **`ignore` on a per-operation `Errors` alarm.** Rejected by (1) and (5): `Errors` never reports
  `0`, so the alarm stays in `ALARM` until a manual reset and gives no recovery signal.
- **A single-metric alarm on route `5xx` with `ignore`.** One alarm metric, $1.70 a month for 17.
  Rejected by (1): a period that answered only 4xx refusals reports `5xx` as `0` and clears a page
  on which nothing succeeded.
- **One Metrics Insights alarm over route `5xx` `GROUP BY Resource`.** Rejected by (1): like the
  catch-all, a route the query stops returning goes to `OK` whatever the setting.
- **Detailed metrics on every route through the stage's `DefaultRouteSettings`.** Simpler by (3):
  it removes the per-route settings and their dependency. Not chosen on cost, which the rubric does
  not weigh: it bills metrics for 20 routes that have no alarm and no reader, up to $66.60 a month
  per environment against $30.60. The owner's cost confirmation may choose it instead.
- **An OK action and a longer hold on the current alarm.** Rejected by (1): once the hold passes, it
  announces a recovery that did not happen.
- **A new per-operation success metric from `src/`.** Not chosen by (2): it needs a new emitter
  outside the approved scope, and the gateway already publishes the pairing for HTTP pages. It is the
  path for surfaces that have no gateway route.

## Review trigger
A page served other than through the HTTP API, a page whose server failures do not surface as 5xx,
a page whose success is answered with a 4xx, or a need to prove recovery per request variant.
