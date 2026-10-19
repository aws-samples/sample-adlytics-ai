---
inclusion: always
description: Governs Git safety, AWS identity boundaries, evidence and completion claims, scope control, and platform invariants on every turn.
---

# Working agreement

## Git

- Read-only, non-destructive Git commands may run at all times without approval when they cannot
  modify the working tree, index, refs, history, remotes, configuration, or repository metadata.
  This includes status, diff, log, show, branch listing, remote listing, and tracked/ignored-file
  inspection.
- Create or switch to a feature branch, stage specific files (never git add . or
  -A), and create a new
  commit without per-command approval when needed to complete approved work.
  Never commit directly on `main`; keep each commit focused and preserve hooks.
- Pushing, merging, rebasing, amending, tagging, deleting a branch, rewriting
  history, discarding worktree changes, and destructive cleanup always require
  explicit approval. Never force push or bypass hooks.

## AWS identity boundary

- Local AWS operator actions and deployed workload identity are different contexts.
  Never configure a developer profile in application or infrastructure runtime code.
- Local AWS operator actions follow meta-aws-operations.

## Evidence

- Never present an assumption as a finding. Claims about an external system carry a
  marker: [verified] fetched and read, [snippet] search result only, [unverified]
  could not confirm.
- Never upgrade a marker without reading the source and citing it.
- Before using a belief about an external system to make a finding, reject a
  design, or propose a workaround, read applicable repository evidence and an
  authoritative source. If it remains unresolved, mark it [unverified].
- When evidence appears to disagree, first align the exact claim, target, scope,
  version, date, and authority boundary. Never choose a result by convenience or
  a generic source ranking. If the same decision-relevant claim remains unresolved,
  name the conflicting evidence and stop the dependent work; research tasks follow
  meta-research.
- Date any claim that depends on a service's current state.
- A command exiting zero is not evidence the goal was met. Never describe
  unverified behaviour in the past tense.

## Completion

- The AWS identity check above precedes every mutation. Production changes, destructive actions,
  and safety-protection changes each require their own explicit approval, whatever broader
  authorization the user gave.
- Mark deployed or external behavior complete only after the corresponding execution finishes and
  its returned state, output, or recorded evidence has been read. A successful command, local test,
  synthetic substitute, or CloudFormation completion cannot stand in for the live acceptance that
  the owning validation document requires.
- If completion reaches a genuinely human-only action or a missing secret or target value, finish
  every independent automatable step first, then request only that narrow input. Record the blocked
  criterion as pending rather than ending at preparation or describing it as accepted.

## Scope

- Solve only what was asked. No extra abstractions, no adjacent cleanup, no
  unrequested files.
- One fact, one home. Improve the doc that owns a fact; never restate it.
- Ask when the answer changes the design and cannot be inferred. Choose and note
  when it is a naming or formatting matter.
- Correct the user when they are wrong. No agreement preamble.
- When a decision reverses an earlier one, update every affected doc in the same
  change.
- Name the business outcome every requirement, gate, acceptance criterion and
  review finding protects: a user-visible behaviour, data integrity, security,
  or cost. A review finding that cannot name one is LOW and never blocks.
- Never build tests, controls or tooling for code the same change deletes.
  Prove the deletion instead.
- Decide verification depth yourself, in proportion to the consequence of being
  wrong. Record each such call and its reason in the change's commit message or
  pull request. Ask the user only when the choice changes a delivered outcome,
  scope, cost, or an action the user asked to approve first.
- A doubt about how a test or probe collected its own evidence blocks only
  when that evidence decides a production, irreversible or user-approved
  action. Otherwise record it as tech debt and continue.

## Invariants

- The agent never fabricates a figure. Every number it states is returned by a
  governed tool this turn or validly inferred from those numbers: arithmetic,
  comparison, ranking, or a projection labelled as one, as an analyst works
  from a report. An inferred figure carries the qualifiers of the figures it
  came from and never combines figures of incompatible basis, grain, window,
  or currency. Evaluation and audit measure this; publication does not block
  on it.
- Customer data never leaves the customer's AWS account.
