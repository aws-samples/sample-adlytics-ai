# Delivery practice

| | |
|---|---|
| **Status** | Proposed with [ADR-137](decisions/adr-137-weigh-design-decisions-by-the-samples-rubric.md) and [ADR-138](decisions/adr-138-deliver-a-slice-complete-or-provably-dark.md) |
| **Last updated** | 2026-10-11 |
| **Enforced by** | Review. No gate checks this document; the gates a slice must pass are named in its acceptance |

This document owns how a design weighs its options, how much review a change gets, and when a
slice may merge. The two ADRs above own why.

## 1. How to read this

| Reader | Read | Stop after |
|---|---|---|
| Writing a design, plan or ADR | §2, §5 | §5 |
| Planning or reviewing a review loop | §2, §6 | §6 |
| Merging a slice | §2, §3, §7 | §7 |
| Checking how the rules apply to real work | everything | §8 |

## 2. The crux

A decision is weighed by one ordered rubric from its first round, so a later round can't reverse it
on a criterion nobody weighed. A merged slice either works end to end in dev or is provably
unreachable, so nothing half-built can show a customer a wrong number.

## 3. Vocabulary

- **Slice.** One user-visible capability, delivered across every layer it needs.
- **Complete.** The slice works end to end in dev, as §7 defines.
- **Entry point.** Any way a slice's code can run or be seen, or its data be produced: a page, a
  route it serves, a schedule it owns, a stream or queue event source mapping, an event-bus rule, a
  deploy-time custom resource, or an operator-invoked command.
- **Dark.** The slice is merged but unreachable: every entry point it has is closed, as §7 defines.
- **Positive control.** The same test, with the one switch flipped, shows the thing it proves absent
  is present. Without it, a test that proves "dark" can pass because the test is broken.
- **Dual path.** Two live implementations of one behaviour, such as a legacy reader kept beside its
  successor.
- **Live acceptance.** A check run against the deployed dev environment on real-shaped data, with
  its returned output read. A unit test, a synth or a successful command is not one.

## 4. Why not the obvious thing

The obvious practice is to judge each decision on its merits when it comes up, review every change
the same way, and merge each layer as it is ready. In practice that fails three ways. Without a
shared order of criteria, a reviewer in round 3 weighs something round 1 didn't, and the decision
moves. Uniform review spends the same effort on a heading change as on a data-integrity change, so
either docs wait or data correctness gets too little. And a layer merged on its own, such as a
backend with no page or a page with no data, is half a feature: it either shows nothing useful or
needs a second, transitional path that has to be retired later.

## 5. The decision-weighting rubric

Every design, plan and decision record weighs its options by these six criteria, in this order. A
higher criterion outranks every lower one. A decision states which numbered items decided it, for
example "decided by (1) and (3)".

1. **Correctness and data integrity.** No undercount, no silent overwrite, idempotent retries,
   fenced writers, and retained data stays retained. This is not negotiable: no lower criterion
   can trade against it.
2. **Strong but efficient patterns.** Use a well-known pattern, such as an outbox, a compare-and-set
   or fencing token, an idempotency key, a single writer or a typed contract, when it removes a real failure
   mode. Don't add one only for ceremony.
3. **Simplicity, judged as an aws-sample.** Prefer fewer moving parts, fewer stacks and less bespoke
   infrastructure. A customer must be able to read and run it. Reject complexity that only protects
   live users, multi-tenant scale or hypothetical futures.
4. **No dual paths.** Legacy is retired fully once its successor exists.
5. **Operability.** Typed spans on business boundaries, retrievable by trace ID and business key,
   plus actionable alarms and documented operator commands.
6. **Reversibility.** When options are close, prefer the one that is cheaper to change later.

## 6. Review effort scales with risk

| Change | Reviewers | Rounds |
|---|---|---|
| Data correctness, security, or the control plane | 2 | 2 |
| Only UI layout or documentation | 1 | stop at approval |
| Anything else, such as backend behaviour, an integration, operability or performance | 2 | 2 |

Every change has a row, and a change that fits more than one takes the highest-effort row that
applies. The second row is the only reduced tier. A change takes it only when every part of it is
UI layout or documentation and no part makes or changes a decision about data correctness,
security or the control plane. A decision record or design doc that does is reviewed under the
first row. A mixed change, such as a doc edit with a backend fix, takes the higher-effort row of
its parts.

A gate's passing run does not cover the residuals its source lists as review-owned, so a reviewer
of a change in that gate's scope checks them by hand. For example, `make check-aws-pagination`
counts a first-page call as part of its token's chain even when its receiver or request calls a
function inside the loop, such as `next_client()` or `Bucket=next_bucket()`, although each call
can return a different client or bucket. The "Accepted residuals" paragraph of
`scripts/check-aws-pagination.py` owns that list.

## 7. Complete or provably dark

A merged slice is one of two things.

- **Complete.** It works end to end in dev: contract, backend, data present, UI, navigation, alarms,
  spans, docs, and a passing live acceptance on real-shaped data.
- **Dark.** Every entry point it has is closed. A page has no navigation entry. A route it serves
  refuses. A schedule it owns is disabled. A stream or queue event source mapping is disabled or
  absent, or the consumer it would invoke is not deployed. An event-bus rule is disabled. A
  deploy-time custom resource is absent. An operator-invoked command is not deployed, or refuses.
  An entry point the slice does not have needs no proof,
  and the slice's plan names the entry points it has, so a reviewer can tell an absent proof from
  a missing one.

Anything else stays unmerged. Three rules bind both:

- A dark-proof with a positive control is required only where wrong data could be shown. Code with
  no caller needs only a unit test.
- No dual paths and no undercount.
- Unit-proven is never reported as live-validated.

## 8. Worked example

The Data pipeline status page is built dark while its serving waits for design approval
([delivery slices](ui/information-architecture/delivery-slices.md#3-slices), Slice 11). Its one
entry point is the page. The slice serves no route of its own yet: its two `/me` keys extend an
existing route and carry no status data, and its feed read is one of the serving fixes awaiting
approval. It owns no schedule. So only the page needs a dark-proof.
The page has no navigation entry, so neither the route manifest nor the route allowlist names
`/admin/status/` and no session reaches it. The test
`test_data_pipeline_status_is_dark_until_its_navigation_entry_is_added` in
`tests/unit/test_navigation_manifest.py` proves the absence. As its positive control, it adds the
entry to a copy of the navigation file and shows the route appear under its own entitlement. The
page could show wrong status data, so its dark-proof needs that control. When its feed route lands,
that route becomes a second entry point, and the slice proves it refuses a session without the
entitlement. It becomes complete when its serving, data, alarms, spans and live acceptance land
together.

## 9. Where to go next

| Question | Read |
|---|---|
| Why one rubric, and why from round 1? | [ADR-137](decisions/adr-137-weigh-design-decisions-by-the-samples-rubric.md) |
| Why complete or dark, and not layer by layer? | [ADR-138](decisions/adr-138-deliver-a-slice-complete-or-provably-dark.md) |
| How do I record a decision? | [Contributing: record a decision as an ADR](../CONTRIBUTING.md#record-a-decision-as-an-adr) |
| Which slices exist and where each stands? | [Delivery slices](ui/information-architecture/delivery-slices.md) |
