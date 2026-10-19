# ADR-136: Deploy a shared-asset change as one deploy-all from one declared source
Date: 2026-10-11
Status: Proposed (2026-10-09). It rests on owner decision D10 of 2026-10-08, to promote the deploy
lock wrapper as a documented tool, on the owner's simplified delivery of 2026-10-08, which replaced
per-slice stack subsets with one deploy-all, and on the owner's approval, on 2026-10-09, of the
workflow retrospective's recommendations. It is not accepted: acceptance is the owner's review of
this file. Implemented in code on `feat/hardening-no-silent-failure` (`374d5c11`) and not yet run
against dev.
## Context
- Every function-bearing stack deploys its Lambda code from one staged source asset
  [repo: `infra/bin/app.ts:105`], passed to each stack as `sourceAssetPath` and read with
  `lambda.Code.fromAsset(props.sourceAssetPath)`, for example
  [repo: `infra/lib/stacks/ingestion-stack.ts:470`]. Any `src/` change therefore reaches all of
  them, and a stack left out of a deploy runs older code beside the new.
- `cdk deploy --all` follows `DEPLOY_ORDER` [repo: `infra/lib/config/index.ts:2576`], which
  `orderStacksForDeployment` turns into stack dependencies [repo: `infra/lib/deploy-order.ts`].
- Deploying a per-slice subset needed a heuristic scan of which stacks a change reached, and that
  scan pulled in nearly every stack, and code skew between stacks still occurred. Each subset left
  a ledger of lagging stacks to catch up later.
- Several deployers shared one dev account with no exclusivity. Separately, a tool timeout killed a
  deploy and its exit trap released the lock while CloudFormation was still updating, so a second
  deploy could start against a stack in progress. The promoted wrapper keeps the lock until every
  target stack is out of `*_IN_PROGRESS`, or keeps it and exits 84
  [repo: `scripts/deploy-locked.sh:461-475`].
## Decision
**A shared-asset (`src/`) change reaches the shared dev stacks only through
`make deploy-all-locked`, from the declared source SHA, as the whole `DEPLOY_ORDER`.** A
one-source-key readback, `make check-source-key`, and `make dev-state` follow. Per-slice stack
subsets and lagging-stack ledgers are not used for such a change. The sequence and every exit code
are owned by
[operator-tools](../capabilities/operational-data-flow-status/operator-tools.md#the-deploy-sequence).

Two one-stack paths remain, and neither is for a shared-asset change, because each restages the
shared source asset for its one stack only:
- `make diff-then-deploy MODE=deploy` runs only under the lock; outside it, it exits 79.
- `make deploy` takes no lock and reads no `SOURCE_SHA`.

Decided by rubric items (1), (3) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a stack left on
older code beside the new is a dual path that can handle one record two ways, and one deploy-all
replaces a heuristic reach scan and a ledger of lagging stacks with a single command.
## Enforced by
Each command enforces its own invariant by a non-zero exit, listed in
[operator-tools](../capabilities/operational-data-flow-status/operator-tools.md#exit-codes):
- `make deploy-all-locked`, through `scripts/deploy-locked.sh`: one deployer holds the lock, HEAD
  is the declared clean `SOURCE_SHA`, and the lock is kept until every target stack is terminal.
- `make check-source-key`: every listed stack is `*_COMPLETE` with each deployed function key
  equal to this synth, and the stacks that carry the shared source asset carry one key between
  them. A listed stack whose synth stages no source asset is not refused, so the gate proves one
  key across the stacks that stage it, not a key on every listed stack.
- `make dev-state`: every alarm not `OK` and every non-empty failure queue carries a disposition.

The sequence is not enforced. The three are separate targets and `make deploy-all-locked` runs
neither check, so the operator runs both after every deploy and review accepts a deploy only on the
read output of all three. Nothing stops a direct `cdk deploy` by someone not using the wrapper,
or a `make deploy` of one stack after a `src/` change, which also holds no lock while another
deployer may; review owns both. `pytest.mark.covers("ADR-136")` marks the unit tests of
`scripts/deploy-locked.sh` and of `validation/dev_operations.py`, which serves the readback and
`make dev-state`. The `diff-then-deploy`, staged-tree and Make-target tests beside them do not
carry it.
## Consequences
- Every deploy of a shared-asset change takes the time of a full deploy-all, even for a one-line
  change.
- One deployer at a time: a second waits or stops with 75.
- A deploy's evidence is one source key read back across every stack that stages it, instead of a
  ledger per slice.
## Alternatives considered
- **Per-slice stack subsets with a reach scan.** Rejected by (1), (3) and (4): the scan was a
  heuristic, and the subsets left stacks lagging on different code.
- **Keep the lock wrapper as untracked task-local copies.** Rejected by (3) and (5): the copies
  carried literal environment values and read the whole account to decide when to release, and an
  untracked copy is no documented operator command.
## Review trigger
A second Lambda source asset, a deploy-all whose duration blocks normal delivery, or a shared
environment other than dev.
