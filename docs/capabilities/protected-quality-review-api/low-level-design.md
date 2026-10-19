# Protected Quality Review API — low-level design

This companion owns the route contracts, their entitlements, and their implementation constraints.
The [capability overview](README.md) summarizes them as a black box;
[ADR-078](../../decisions/adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)
and [ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) own the
rationale.

## 6. Interfaces and contracts

### Authorization contract (ADR-078)

This section and the ones below are the only home of the review routes, their sections, and their
storage access
([ADR-078](../../decisions/adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md),
[ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md)). The edge entry,
the credential each route admits, and its verification are owned by
[authentication](../../concepts/authentication.md#credential-binding-adr-078).
Every check fails closed.

- Cognito groups `quality-reviewer` and `audit-attribution` replace the three per-level groups. The
  pre-token trigger projects them into the review-level claim.
- The authorizer chooses the required entitlement from the matched route key, not the raw path. A
  path under a protected prefix that maps to no entitlement is denied.

```text
POST /api/quality-review/turn   -> quality-reviewer; with audit-attribution as well, adds the attribution section
POST /api/quality-review/turns  -> quality-reviewer
```

- `audit-attribution` alone grants nothing. A caller in it but not in `quality-reviewer` is denied
  on both routes.
- The verified review levels travel in the authorizer context. The function rechecks every section
  against them before returning it. The `attribution` omission has unit and live tests of its own.
  A request with no verified subject or levels in that context can only mean a route detached
  from its authorizer; the function answers it 503 (`ADLY-AGENT_QUALITY-0005`), never 400.
- **Level vocabulary, expand before contract:** every verifier (the route authorizer, the
  conversation route, and the token service's route exchange) first learns `quality_reviewer` and
  `audit_attribution` as levels no route maps, and that change reaches TokenServiceStack and
  ApiStack before AuthStack's pre-token trigger issues them. Skipping it refuses every review-group
  member on every authorized route, including chat, for the whole deploy. During commit A the three
  old levels stayed recognized values that granted nothing, and the pre-token trigger issued an
  old-group member its retired level beside `quality_reviewer`, so the per-level routes an earlier
  ApiStack still served kept admitting them. Commit B closed the claim again: the old levels, groups,
  and trigger mapping are gone, and a token naming a retired level is refused everywhere.
- **Rollback limit.** The receipt writer stamps the turns-index attributes from this change on,
  and the earlier receipt decoder refuses a receipt that carries them. Rolling the receipt readers
  back past this change after any new receipt exists therefore turns those receipts into invariant
  conflicts in every old reader, and a writer retry that spans the deploy reports a conflict rather
  than an exact replay. Roll forward instead. The reviewer access log group is retained,
  deletion protected, and fixed-name: reverting AgentQualityStack orphans it, and a later roll
  forward fails on the existing name until the group is brought back with `cdk import`.
- **Group migration, in order inside the slice:** create the two new groups; run
  `make migrate-review-groups` (first with `DRY_RUN=1`) in every environment, which proves the
  account, adds each member of the three old groups to `quality-reviewer` before removing it from
  the old one, and exits nonzero unless the old groups read back empty; then remove the groups and
  their trigger mapping. The three retired level values leave the verifiers only once one
  access-token lifetime has passed since that clean run, because a token issued before it still
  carries one and an unknown level is refused everywhere. A retired level never implied attribution,
  so no member is added to `audit-attribution`. Skipping the migration removes review access from
  every member of the old groups when they are deleted, with no error at the time.
- The dev persona set is a reviewer in `quality-reviewer`, an attributor in both groups, an
  attributor in `audit-attribution` only, and a denied persona in neither.

### Selector request

`turn` accepts one JSON selector envelope:

```json
{"selector":{"audit_id":"opaque-server-issued-id"}}
```

or:

```json
{"selector":{"correlation_id":"normalized-uuid4"}}
```

The selector contains exactly one member. Missing, both, malformed, list/time-range/free-text, and
caller-provided record-reference selectors are invalid requests. A correlation lookup is bounded; zero
or multiple attested receipts yield the non-disclosing response rather than a list.

`turns` accepts a bounded creation-time window, closed-value filters, a page limit, and an optional
cursor. Both routes are POST, so no selector reaches URL or query telemetry.

`turns` paging: each page is one bounded read of the [turns index](#refusal-storage-access-and-logging-adr-078-adr-080-adr-081).
Filters and the expired-record rule apply after that read, so a page may hold fewer rows than the
limit, or none, while a cursor is still returned. The caller continues until no cursor is returned.

The `turns` request is exactly these members:

```json
{"window":{"from":"2026-09-20T00:00:00+00:00","to":"2026-09-27T00:00:00+00:00"},
 "filters":{"terminal_outcome":["complete"]},
 "limit":50,
 "cursor":"opaque-server-issued"}
```

- `window` is required. `from` and `to` are UTC instants with `from` before `to`, spanning at most
  31 days. A receipt is in the window when `from <= created_at < to`.
- `filters` is optional and has one member, `terminal_outcome`: one or more distinct values of the
  receipt's closed terminal outcome.
- `limit` is optional, 1 to 100, default 50.
- `cursor` is optional. It is opaque, valid only with the window and filters that produced it, and
  refused as an invalid request otherwise.
- The response is `{"schema_version":"QualityTurnsPage.v1","turns":[...],"cursor":...}`, newest
  first. `cursor` is `null` on the last page. A row carries `audit_id`, `correlation_id`,
  `created_at`, `terminal_outcome`, and `expires_at`.

The `turn` response is `{"schema_version":"QualityTurnReview.v1", ...}` with one member per
section the caller may receive: `audit`, `findings`, `evaluation`, and `attribution`. Each
section's record carries its `expires_at`, read from the stored item.

### Response sections (ADR-078, ADR-081)

| Section | Returned to | Content |
|---|---|---|
| `audit` | `quality-reviewer` | The `audit` fields below. |
| `findings` | `quality-reviewer` | The `findings` fields below. |
| `evaluation` | `quality-reviewer` | One result per evaluator, with the turn's evaluation state and each verdict's provenance as the [Agent Quality Loop](../agent-quality-loop/low-level-design.md#evaluation-states-dispatch-and-provenance-adr-079) defines them. |
| `attribution` | Callers in both `quality-reviewer` and `audit-attribution` | The receipt's principal. Any other caller receives the turn without this section. |

Every record the `turn` route returns carries its expiry, so live acceptance of retention reads
through this API rather than a table.

A `turns` row carries `audit_id`, `correlation_id`, `created_at`, and the terminal outcome, and
never the principal. The principal appears in no other section, log, metric, trace, or index
attribute.

The `audit`, `findings`, and `evaluation` sections carry these fields:

| Section | Fields |
|---|---|
| `audit` | Schema version; terminal/audit outcome enums; context/authority/operation/publication summaries, the publication summary including the released character count of a streamed turn ([ADR-083](../../decisions/adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md)); deployed version identities; admitted-objective count; creation time. |
| `findings` | Schema version; audit terminal identifier represented only within the response envelope; zero or more closed finding kind/severity/creation-time values. |
| `evaluation` | Schema version; the turn outcome (terminal and publication outcomes, public block count, released character count); admission/delivery/writer presence states; completed/evaluator-failed result state, a valid label with a score only when the evaluator's scale is numerical (null for the categorical grounded-correctness scale), and evaluation time when a result exists. |

`audit` carries the turn's `audit_id`, `correlation_id`,
and `trace_id`, the identifiers a reviewer selects and investigates by. No section includes
question/answer/prompt/model text, page/evidence values, tool arguments/results, digest or opaque
reference values, evaluator explanation/error, CloudWatch message/span/pointer, queue record, or raw
protected input. `completed` and `evaluator_failed` remain distinct; absent linkage is represented as
a bounded pending state, not an invented score or evaluator failure.

### Refusal, storage access, and logging (ADR-078, ADR-080, ADR-081)

- Invalid requests receive a bounded client-visible validation outcome. The external response for
  absent audit data, non-unique correlation, a caller denied discovery, and a caller in
  `audit-attribution` alone is indistinguishable. Internal errors map to bounded dependency or
  invariant outcomes without logging raw requests, claims, subject, selector, or record values. The
  cookie credential's CSRF and Origin checks apply.
- One function and one role serve both routes. The role holds exact key reads and key-bounded
  queries on each of the five record tables the
  [Agent Quality Loop](../agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080)
  names, `Query` on the existing `correlation-audit` index that `turn` needs for correlation
  selectors, and `Query` on the turns index. It is one of the receipt table's named readers, so its
  role has a fixed name. It holds no `Scan`, write, Logs, SQS, or protected evaluation input access.
- The turns index, `turns-by-day`, is a sparse global secondary index on the receipt table, keyed by
  the receipt's UTC creation day (`turns_day`, `YYYY-MM-DD`) and its creation time
  (`turns_created_at`). The receipt writer sets both, and a top-level `terminal_outcome`, on every
  receipt it writes. Only receipts written from slice 6 on (2026-09-28) carry the index
  attributes, so no other item enters it. Earlier turns are not listed; `turn` still reads them by
  selector. It projects the non-identity row fields (`correlation_id`, terminal
  outcome, and `created_at`) and the record's expiry, so a row costs no second read and the
  expired-record rule needs nothing beyond the index. The principal is never projected.
- Every read applies the Agent Quality Loop's
  [expired-record rule](../agent-quality-loop/low-level-design.md#terminal-coverage-and-retention-mandated-by-adr-081).
- The function still never logs requests, claims, subject, selectors, or record values. Reviewer
  access is recorded by the
  [reviewer access log](../../concepts/observability.md#38-reviewer-access-log-adr-081).
- **Retired by slice 6:** the three per-level routes and functions with their roles, log groups,
  and alarms; the authorizer's path map; the authorizer cache setting ([edge entry](../../concepts/authentication.md#edge-entry-adr-078));
  and the probes that exercise the old routes or read review tables directly. Commit B
  (2026-09-29) removed the three old groups, their trigger mapping, and the three retired level
  values, in the order the group migration bullet above states.

## 7. Concepts applied

- [Agent Quality Loop](../agent-quality-loop/README.md) owns immutable records and exact storage
  invariants.
- [Authentication flow](../../concepts/authentication.md) owns cookie sessions, CSRF transport
  protections, and the mandated credential binding and edge entry.
- [Observability](../../concepts/observability.md) owns protected
  evaluation transport and raw result custody.

## 8. Decisions

- [ADR-059](../../decisions/adr-059-create-a-bounded-agent-quality-audit-loop.md) — immutable audit and
  protected evaluation linkage.
- [ADR-060](../../decisions/adr-060-protect-quality-review-retrieval-with-an-explicit-operator-authorization-boundary.md)
  — Cognito group-derived route entitlement and dedicated route roles.
- [ADR-064](../../decisions/adr-064-retire-planner-era-contracts-and-anchor-the-audit-trajectory.md)
  — the v3 audit and findings projections, the per-evaluator evaluation projection, and the
  table `Query` the last two require.
- [ADR-078](../../decisions/adr-078-serve-quality-review-to-programmatic-callers-through-the-cloudfront-edge.md)
  — two routes, two groups, one role, the bearer credential, and the CloudFront edge (the
  command-line client not yet implemented).
- [ADR-080](../../decisions/adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md)
  — the five record tables the review role reads (current state in the Agent Quality Loop's
  [implementation status](../agent-quality-loop/low-level-design.md#record-family-storage-mandated-by-adr-080)).
- [ADR-081](../../decisions/adr-081-make-every-turn-traceable-and-attributable.md) — the
  attribution section and the reviewer access log.

## 9. Acceptance conditions

Returned acceptance evidence contains only route/outcome counts and schema facts, never protected
content or identity values.

### Acceptance (ADR-078, ADR-081)

Live dev acceptance reads, through the CloudFront URL, the items below. All eight passed in dev on 2026-09-28
([validation results](../../../validation/RESULTS.md#review-api-slice-6-commit-a--2026-09-28)),
except item 5's direct-endpoint half, which waits for edge entry. Item 7's absent groups read back
after commit B (2026-09-29); its old-level refusal is a unit test, because once the trigger mapping
was removed Cognito can no longer issue such a token to test live.

1. A `quality-reviewer` caller succeeds on `turn` and `turns` with each credential authentication
   admits there. Credential, crossing, and direct-endpoint refusals are
   [authentication's acceptance](../../concepts/authentication.md#acceptance-adr-078).
2. `attribution` is returned to a caller in both `quality-reviewer` and `audit-attribution`, and
   omitted for a `quality-reviewer`-only caller. A caller in `audit-attribution` alone is refused on
   both routes with the non-disclosing outcome.
3. No `turns` row carries the principal. Following the cursor across a window with filters returns
   every matching receipt once, including after a page shorter than its limit.
4. The retired `audit`, `findings`, and `evaluation` routes return 404.
5. Every non-canonical path variant the permanent probe sends through the distribution is refused.
   The same variants sent straight to `execute-api` are refused by the origin-verify header, which
   is [edge entry acceptance](../../concepts/authentication.md#acceptance-adr-078).
6. The rendered review role holds no `Scan`, write, Logs, SQS, or protected-input action.
7. After the group migration the three old groups read back empty and then absent, and a token
   carrying an old review level is refused
   (`tests/unit/test_route_authorization.py::test_a_retired_level_is_refused_on_every_route`).
8. Every record a `turn` response returns carries its expiry. A fictional turn written just before
   the slice deploy, so its backfilled receipt is still inside dev retention, is returned by `turn`
   and absent from `turns`.

Reviewer access-log acceptance is owned by
[observability](../../concepts/observability.md#38-reviewer-access-log-adr-081).
