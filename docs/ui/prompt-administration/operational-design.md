# Prompt Administration — operational design

Cross-cutting qualities for the registry surface. The [HLD](high-level-design.md) owns
topology; the [LLD](low-level-design.md) owns wire contracts and points here for budgets.

## 1. Latency and capacity

- Registry reads and lifecycle writes: 2 s p95 server time. Browser volumes are operator-scale
  (single-digit slots, tens of versions). The one-minute retained-outbox dispatcher queries each
  closed-roster slot partition once; it never scans the table.
- Calibration is asynchronous. The API returns after atomic retained-outbox admission and attempts
  immediate queue delivery; one worker handles
  one version per receive, with two candidate generations in one bounded parallel wave followed by
  independently judged cases in a second bounded wave. Worker concurrency is two versions. Normal
  clients poll the version projection at 2, 3, 5, 8, then 13-second intervals capped at 13 seconds;
  each client has a finite attempt budget and stops immediately on `passed` or `failed`.
- The generation path's activation-pointer read is one cached `GetItem` and must not add a
  measurable step to any page budget.
- The versions read resolves each distinct author through Cognito `AdminGetUser` inside the same
  2 s budget (amended 2026-10-01): at most 25 lookups per response, 4 at a time by default
  (`operator_directory_max_concurrency`, 1-8), waited on for at most 1.5 s, and cached per
  container for 15 minutes. A lookup past the budget leaves that author unavailable for this
  response and resolves on a later poll.

## 2. Caching and precompute

- The activation pointer is read through the same cached-configuration discipline as other
  governed configuration; there is nothing else to cache. Version records are immutable, so
  any caching of them is trivially safe.

## 3. Authorization and custody boundary

- Every route sits behind the operator authorization boundary (ADR-060 precedent), gated by
  the `prompt-admin` Cognito group. The API enforces it independently of any UI; a
  non-operator caller receives the same nondisclosing response as an unknown route.
- Operator identity comes only from the authorizer and is forbidden in request bodies. It is
  recorded as the opaque Cognito subject on every version creation and every lifecycle event
  (amended 2026-10-01).
- The versions route resolves a version's subject to the operator's email address at read time,
  on its own role (the only one holding `cognito-idp:AdminGetUser`), and returns it only to
  prompt-admin operators. The email address is never stored and never logged.
- The registry carries no customer data, so its risk surface is integrity, not disclosure:
  write-once version records and the transactional pointer+event write are the controls. The
  one disclosure is that read-time email address: prompt-admin operators see their
  co-operators' addresses.

## 4. Data protection and retention

- Prompt templates and calibration cases contain no customer content (ADR-069; calibration
  cases are fictional by the evaluation-validity contract). Version and event history is
  retained indefinitely — cached insights and audit records reference version identities, so
  destruction is not a capability. Storage: managed encryption, on-demand billing, PITR,
  RETAIN policy.

## 5. Failure containment and degraded states

| Failure | Result |
|---|---|
| Immediate queue admission fails | The API still returns the committed version identity as `pending`; its atomically created retained outbox row is dispatched by the scheduled worker. |
| Calibration runner unavailable | The message retries through three receives, then dead-letters and alarms; the retained version stays `pending`, activation stays refused, and live generation is unaffected. |
| Duplicate or acknowledgement-loss delivery | A conditional lease admits one worker; terminal redelivery acknowledges without a model call. |
| Worker loses or outlives its lease | Completion is refused because it requires the exact per-attempt reference and an unexpired lease; queue retry may reclaim only after expiry. |
| Registry store unavailable | Admin surface renders its page-issue state; insight pages degrade per the campaign package's operational design, never blocking on the registry. |
| Concurrent operators | Optimistic-lock conflict returns a typed refusal; the UI prompts reload; no half-applied activation can exist (single transaction). |
| Operator directory unavailable | The versions read still answers 200; each affected version reads "operator unavailable", and one ERROR per response carries the failed-lookup count. |
| Malformed version record | 503 `unavailable` with an ERROR report and the error metric, so it alarms; never a conflict. |

## 6. Observability and alarms

- Every lifecycle write emits a structured log beside the append-only activation events. A
  completed calibration logs slot, version, tier, receive count, and case count; provider failures
  use the shared bounded AWS failure projection, never prompt, narration, judge working, or raw
  request/response. Each batch records acknowledged/failed counts. Model calls retain the worker
  trace across thread fan-out.
- The versions read's terminal log carries `versions` and `authors_unavailable` counts, never a
  subject or an email address (amended 2026-10-01).
- `report_error` emits the shared error metric for retryable failures. Queue age and DLQ depth are
  the workflow signals: either alarm notifies the shared alarm topic, and one dead-lettered message
  breaches immediately.

## 7. Cost envelope

- Operator-scale traffic on one on-demand table, one content-free queue, and calibration runs.
- Each Cognito `AdminGetUser` lookup counts toward the pool's monthly active users for billing,
  so an author looked up in a month they did not sign in may count as active. The per-container
  cache keeps polling near zero lookups after the first.
  Retry cost is bounded by the queue's three receives. Polling reads only one slot's version
  projection and uses increasing intervals; it never causes a model invocation.

## 8. Deployed acceptance of read-time authorship

- The deployed probe (`validation/probe_prompt_registry.py` with `ADL_EXPECTED_AUTHOR_EMAIL`)
  asserts the version it creates reads `operator_email` with the expected address and prints
  only `author=matched`, the response's correlation ID, and the UTC window of the call.
- The trace of that versions request, read from the exported spans by its correlation ID, must
  carry no `@` and no operator subject in any span attribute or event value. If it does,
  authorship acceptance **fails**: it is recorded as pending, the publication push does not
  proceed, and the remedy returns to the owner as a design revision. It is never fixed in place
  during acceptance.
