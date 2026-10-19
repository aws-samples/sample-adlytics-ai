---
inclusion: fileMatch
fileMatchPattern: ["README.md"]
description: Governs root README commands, delegation to canonical owners, unenforced sequences, and manual-versus-automatic wording when editing the repository README.
---

# Root README rules

The README owns the tutorial: what this is, what it costs, and the shortest
honest path from clone to a signed-in page. Everything else points into docs/.
Why there is no CI to appeal to: docs/decisions/adr-015-no-remote-ci.md.

## Commands

Never show a command that reaches AWS without PROFILE. Every such target
requires it and has no default, so a command written without it does not fail
later, it fails immediately and reads as a broken instruction. ENV carries the
same requirement.

Never substitute a real profile or environment name for the placeholder. A
copyable command naming this project's own values is how a reader's first deploy
lands somewhere nobody chose.

## Delegation

Never restate what a make target does. Name the target and let it be the
definition. Prose that describes a build step is a second definition that goes
stale silently, which is the drift ADR-015 removed once already.

Never explain why a design is the way it is. State what a reader must do and
link the ADR that owns the reasoning. A rationale paragraph here becomes the
version people read and the ADR becomes the version nobody updates.

## Sequences

Never present a sequence as enforced unless a make target enforces it; then
name the target instead of listing its steps. For every step no target
checks, such as gate results and teardown completeness, write the step, then
say what fails and how it presents when the step is skipped.

Never describe a step a human performs as automatic. Sign-in verification and
deleting retained resources are both manual, and a README that reads as though
they happen on their own is how a retained secret bills for months.
