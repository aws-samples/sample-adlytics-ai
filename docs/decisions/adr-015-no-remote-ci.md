# ADR-015: The Makefile is the gate; there is no remote CI

Date: 2026-08-18
Status: Accepted. Amended 2026-10-01: `make deploy-all` deploys every stack in `DEPLOY_ORDER`
([ADR-012](adr-012-stack-composition-and-config.md), amendment of that date). A person still
runs it and still runs the gates; nothing runs either automatically. Amended 2026-10-20: public
pushes go through local gates in three tiers, and tracked hooks add an early warning
([amendment](#amendment-2026-10-20)).

## Context

The repository carried a GitHub Actions workflow that ran the documentation
checks, the Python gates, the CDK tests and the frontend build. It was never
agreed to, and it had never run — no commit had reached `main` and no pull
request had been opened, so it had gated nothing.

Meanwhile every gate it would have run already exists as a `make` target, and
those targets are what has actually been used throughout development:
`make check`, `make verify`, `make lint-boundaries`, `make dead-code`,
`make synth`, `make test-integration`, `make mutate`.

Two things about this project make a remote pipeline a weaker fit than it looks.
Customers deploy it into their own AWS accounts, so there is no shared
environment for a pipeline to deploy to and nothing it could usefully verify past
synth. And the integration tier deliberately reaches real AWS resources rather
than mocks (ADR-014), which means the tests that catch the most require
credentials a public repository must not hold.

## Decision

`make` is the only gate. No remote CI.

- Every gate is a `make` target, runnable with no credentials except the ones that
  reach AWS, which require an explicitly named profile.
- A human runs the gates before merging. Nothing enforces it.
- A person follows the [manual deployment sequence](../../README.md#deploy). Out-of-order
  deploys fail where a stack reads a parameter that does not exist yet.
- The `.github/` directory is removed rather than left holding a disabled
  workflow. A workflow present but not running is worse than none: it reads as
  coverage that does not exist.

## Consequences

**Accepted, and the reason to revisit this.** Nothing stops a change merging with
failing gates. Today the maintainer is the only contributor, so the check and the
enforcement are the same person and the gap is theoretical. That stops being true
with the first outside contributor, and that is the trigger to reopen this — not a
date, and not repository size.

**What improves.** No second definition of the gates to drift from the Makefile.
This mattered in practice: the workflow described the Lambda build as installing
against an arm64 Lambda base image, which `make build-lambdas` does not do — it
resolves `aarch64-manylinux2014` wheels with `uv` and verifies every `.so`. A
pipeline nobody ran had been describing a build nobody performed.

**What is genuinely lost.** A pipeline runs the gates on a clean checkout, which
catches the class of failure where something passes only because of local state —
an uncommitted file, a stale `build/`, a dependency installed but unlocked.
`uv sync --frozen` covers the dependency case. The rest is uncovered, and running
the gates from a fresh clone before a release is the manual substitute.

**Superseded in part.** ADR-005 and ADR-012 both assumed a pipeline: ADR-005 for
artifact building, ADR-012 for deploy order. Both carry a pointer here. Their
underlying decisions are unchanged — only where the work runs.

## Alternatives rejected

**Keep the workflow, disabled.** Leaves a file describing gates that do not run,
which is the drift this decision removes.

**Keep it running on pull requests only.** Would have worked and cost nothing per
push. Rejected because it duplicates the Makefile's definition of every gate, and
the duplicate had already gone stale before it ever executed.

**Local git hooks instead.** A pre-push hook running `make check` would enforce
without a remote. Rejected for now because hooks are per-clone and silently absent
for anyone who does not install them, so the guarantee is no stronger than
remembering to run `make` — with the added cost of appearing stronger. The 2026-10-20
amendment adds hooks as an early warning only, for that reason.

## Amendment (2026-10-20)

The repository is now published, so a push publishes history as well as code. The Makefile is
still the only gate and nothing remote runs it, but publication is gated locally in three tiers.
[CONTRIBUTING](../../CONTRIBUTING.md#run-the-gates) owns which gate runs before which action, and
[scripts/README.md](../../scripts/README.md) owns what each gate checks.

- **Commit tier: tracked hooks, as an early warning.** The rejection above still holds for the
  boundary: a hook is per-clone and `--no-verify` skips it. So the hooks only catch a secret, a
  denylisted literal, a forbidden path or a wrong identity before it is in history at all, and the
  push tier repeats every one of those checks. `make install-hooks` writes stubs into the
  repository's own hooks directory and never sets `core.hooksPath`, because a repository-level
  hooksPath would silently replace a system-level one set by the machine's security tooling.
- **Push tier: `make publication-gate` over the pushed range.** `make publish` requires it, so an
  uninstalled hook never bypasses it.
- **Release tier: `make release-gate`.** The release gate runs `make verify-clean`, the gates on
  a clean export of HEAD. That covers, for a release, the clean-checkout failure class listed
  above as lost. Its full ASH scan is the release security scan. A sealed external content
  review receipt for the release tree was also required until the owner retired it on
  2026-10-20; ASH in the release gate replaced it.

Every test and gate run writes a write-once [run record](../test-run-records.md), and a gate
accepts only a record of the exact tree it examines. That is what makes a local gate's result
citable without a pipeline, and lets the release gate reuse a recorded verify-clean rather than
re-run it. The trigger to reopen this decision is unchanged: the first outside contributor.
