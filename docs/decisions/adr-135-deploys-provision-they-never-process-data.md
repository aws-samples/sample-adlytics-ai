# ADR-135: Deploys provision; they never process data
Date: 2026-10-11
Status: Proposed (2026-10-09). It restates and generalizes owner decision D11 of 2026-10-09, that a
re-bake not caused by new data is only ever triggered declaratively, and rests on the owner's
approval, on 2026-10-09, of the workflow retrospective's recommendations. It is not accepted:
acceptance is the owner's review of this file, including the open question below. Not implemented:
the one violation is named under Consequences.
## Context
A deploy that also processes data does work nobody asked for, at a time nobody chose, and makes
every deploy's effect depend on what data happened to be there.
- The Publisher's `DeployBackfill` custom resource enqueues the complete declared page and variant
  set whenever the declared-input generation changes
  [repo: `infra/lib/stacks/publisher-stack.ts:372`]; a code-only deploy does not fire it. The owner
  decided on 2026-10-09 that, while several deploys a day are still landing, automatic re-bakes
  would cause confusion and unneeded processing, so a re-bake not caused by new data runs only from
  an operator command.
- The prompt-registry seed is also a custom resource that re-runs on a deploy: its
  `contractDigest` property changes with the contract bytes, so any contract change re-fires the
  idempotent seed handler
  [repo: `infra/lib/stacks/insight-prompt-registry-seed-stack.ts:95-100`]. A text mismatch raises,
  and the stack rolls back.
## Decision
**No custom resource or deploy hook starts a re-bake, reprocess or backfill.**
1. Reprocessing is a declarative, human-run operator command. It takes an explicit scope and a dry
   run that lists exactly what it would do. It records who ran it and why, emits a span, and is
   idempotent.
2. A granted run's own bakes stay automatic. They are the normal flow of new data, which would
   otherwise never appear.
3. Provisioning seeds are excluded, as proposed below: a seed that writes declared configuration
   and fails closed provisions the stack it belongs to.

Decided by rubric items (4) and (5) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a re-bake has one
trigger, the operator command, rather than that command beside a deploy hook, and that command is
scoped, recorded and traced. Item (1) is not traded: a granted run's own bakes stay automatic, so
new data still appears. The proposed seed exclusion is decided by (3): a seed that fails closed is
part of provisioning, and moving it to a command adds a step without protecting any data.
## Open questions
Whether the rule extends to provisioning seeds is for the owner. This ADR proposes to exclude them,
because a seed that fails closed changes no customer data and its failure rolls the deploy back. If
the owner extends the rule, the prompt-registry seed moves to an operator command too.
## Enforced by
Nothing today. The infra-test assertion "no custom resource on the Publisher or ingestion path
enqueues work" lands with the removal of `DeployBackfill`, because it would fail on that resource
until then.
## Consequences
- `DeployBackfill` violates this decision until it is removed. Its removal, and the declarative
  `rebake` command that replaces it, are scheduled outside this branch under D11.
- Until then, a deploy that changes a declared input still starts a full re-bake.
- After the removal, a declared-input change shows in baked pages only after an operator runs a
  re-bake, so a deploy's acceptance names that run when it needs one.
## Alternatives considered
- **Keep the deploy-time trigger, firing only on a declared-input change.** Rejected by the owner,
  and by (4) and (5): it still re-bakes on deploys nobody ran as a re-bake, as a second trigger
  beside the command.
- **Make new-data bakes manual too.** Not taken: new data would wait for an operator. It is a
  separate owner decision that has not been made.
## Review trigger
A second environment with live users, where a deploy and the re-bake it needs would have to land
together, or the owner's answer to the open question.
