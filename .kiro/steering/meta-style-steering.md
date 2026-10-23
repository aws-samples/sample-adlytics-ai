---
inclusion: fileMatch
fileMatchPattern: ["**/.kiro/steering/**/*.md"]
description: Governs steering-file descriptions, per-rule actionability, canonical ownership, tracked-versus-local content boundaries, scope, and inclusion-mode selection when editing workspace steering files.
---

# Authoring steering files

Front matter and plain-text format are enforced by make check-steering, not
stated here. Run it after editing; it will tell you what is missing.

## Description

Every file needs one; make check-steering enforces its shape. Name what the file
governs and when it applies, so someone scanning the set can tell whether this is the file they need without opening it. For a
manual file this is the only way anyone discovers it exists.

## Every rule must be actionable

A rule earns its place only if it passes all of these. Apply the test per rule,
not per file; there is no rule quota.

- It is specific enough to act on without interpretation. "Divide total_cost by
  100,000" passes. "Handle money carefully" does not.
- It changes what would otherwise happen. If the obvious action already complies,
  the rule is noise that dilutes the rules around it.
- It is a prohibition or a precise instruction, never a preference. "Never
  execute model-authored SQL" passes. "Prefer safe SQL" does not.
- It cannot be discovered from the code or inferred from existing patterns.
- No script can enforce it. If one can, write the script and delete the rule.
- Exactly one steering file states it. Never duplicate a rule across two files.
- It states a rule, not rationale. Rationale lives in docs/ and steering points
  to it by path.
- It states no value that something else owns. A version, a service quota, a
  threshold, a divisor, a character cap, a column list, a module list: each has an
  owner, which is a manifest, a config module, a contract, a source doc or a gate.
  Name the constraint and name the owner. Never copy the value.

- It states an invariant, never the system's current state. "X is not yet
  supported" describes a moment and expires silently; write the rule that
  stays true when X ships, or name the contract that owns X's status.

The last test is the one that fails silently, which is why it is worth applying
hardest: a redundant rule only wastes tokens, while a copied value goes stale with
nothing reporting it and then instructs the agent to do what the gate now rejects.
If a rule cannot be written without a number, that number is the thing being ruled
on and belongs here. If anything else already records it, point there instead.

Never state code style. Encode it in ruff, eslint or prettier config.

## Tracked and local content

- Tracked content never depends on the content of an untracked or gitignored
  path — local steering, local working documents, local specs. The dependency
  is one-way: local files may cite committed files, never the reverse. Naming
  where local custody lives (a glob, a directory's role) is designation, not
  dependency, and stays legal. make check-links enforces the link half; apply
  the same test to prose mentions.
- Operator-specific rules (identities, remotes, personal workflow bindings)
  live in gitignored `local-*.md` steering files, which load and bind exactly
  like committed steering. A committed rule states the invariant; the local
  file binds the operator's value.

## Scope and loading

- One concern per file. If a file governs two concerns, it is two files.
- Prefer fileMatch or manual over always. Put a rule in an always-on file only if
  it applies to every turn.
- Never nest fileMatch patterns without checking what co-loads. When nesting is
  unavoidable, the narrow file must never restate the broad one.
