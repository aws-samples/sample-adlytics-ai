---
inclusion: auto
name: meta-delivery-git
description: Use when creating a branch, commit, code review, merge, release, cherry-pick, or delivery plan, or when handling remotes, pushes, or publication boundaries.
---

# Git and delivery

## The model

One long-lived branch: main. It is always releasable, and it only ever receives
work that is coherent and green.

Feature work happens on a feature branch, never directly on main. The branch is
the isolation boundary: it may carry several commits while a feature comes
together, and main is protected from partial states.

- Branch from main. Never branch from another branch.
- One branch per feature, not per commit. Commit freely on the branch; the
  history there is a working log, not a published one.
- Branch names are type/slug, where type is feat, fix, docs, refactor, test or
  chore. For example feat/auth-cookie-flow.
- Rebase only when explicitly approved. Before an approved merge, inspect whether
  the branch is behind its intended integration base and present the required
  integration action.
- Squash merge into main, so one feature is one commit and revert stays a single
  operation.
- Merge when the slice is coherent, green and reviewable. Never wait for the
  whole feature to be finished if an earlier slice stands on its own.
- Never let a branch live past a week without merging something to main.
- Never merge a red branch. Every merge leaves main releasable, and that outranks
  shipping on time.

The cost of this model is integration risk, and it grows with branch age. That is
why the two techniques below are more important here, not less: they are how a
feature branch stays short without the feature being small.

## Commits

Subject: a conventional-commits type and optional scope, then imperative mood and
sentence case. Read git log for the types and scopes in use rather than inventing
one. Body states why, the alternative rejected, and the risk avoided — never what,
which the diff already shows.

- One logical change per commit. A rename and a behaviour change are two commits.
- Never commit secrets: no .env, credentials, tokens, private keys, or account
  IDs in fixtures. Check before staging.
- Never commit generated or vendored output other than cdk.context.json, which
  infra-style-cdk governs.

## Pre-merge gate

Nothing merges until every gate CONTRIBUTING.md lists for the changed surface
passes, and nothing re-runs them after a push: the gates are local. Remote-CI
status is owned by ADR-015.

## Keeping a feature branch short

Two techniques, in order of preference. Both let a large feature reach main in
slices rather than as one long-lived branch.

Branch by abstraction. Introduce an abstraction around the code being replaced
and commit it. Build the replacement behind that abstraction across as many
commits as needed, each merged to main, none breaking the build. Switch over in
one small commit, then delete the old path.

Feature flag. Merge incomplete work to main with the entry point disabled.
Record the removal condition in the commit that adds the flag, and remove the
flag in the same release cycle the feature launches. A flag with no owner and no
removal condition is worse than a branch.

## Releases

- Release from main while cadence is high and there is one deployment target.
- When a deployment must stay on a known version, cut a release branch just in time —
  days before, not weeks — and tag it.
- Fix forward on main, then cherry-pick to the release branch. Never the
  reverse: a fix living only on a release branch is a regression waiting for the
  next release.

## Additional prohibitions

- Never --amend after a pre-commit hook failure. Fix, re-stage, new commit.
- Never merge your own change without review while more than one person works
  in the repo.
- Never touch git config beyond managing the operator's declared remotes.

## Remotes and publication

- Remotes and push destinations are operator-local configuration, never repository content.
  Bind them per meta-style-steering, not here.
- The tracked tree is publication-appropriate at every commit: content unfit for public
  publication never enters source control. Private working material lives locally, gitignored.
- A commit reaches a public remote only through `make publish`, after `make publication-gate`
  and `make release-gate` have both passed on that exact tree. Never push to a public remote by
  any other command.
- Never push a feature branch, a tag or working history to a public remote. A public release is
  one commit whose parent is the current public main.
- Never rewrite or force-push public history without the owner's explicit approval. A leaked
  secret is rotated first, then removed from history; CONTRIBUTING.md "If a secret is pushed" owns the procedure.
