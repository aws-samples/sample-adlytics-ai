---
inclusion: always
description: Orients an assistant in an Adlytics AI checkout on every turn; governs where facts live, how contract-driven changes are made, and which local gates prove a change.
---

# Adlytics AI in this checkout

Adlytics AI is retail-media analytics for first-party Amazon vendors: it joins Amazon Marketing
Cloud and Vendor Central data in the customer's own AWS account and serves it through pages and a
chat assistant. README.md owns the product summary; docs/README.md indexes every design document;
docs/repo-structure.md owns the repository layout. Read the owning document before changing a
capability, and update that document in the same change.

## Changing behaviour

- Make a change at its declared contract under contracts/ or presentation/, then run the generator
  the README "Extend it" table names for that extension point. Never hand-edit a generated file;
  the next generator run overwrites it and make check refuses the drift.
- Never add a figure, ratio or total that a governed tool did not compute. The invariant is owned
  by meta-ai-collaboration; the semantic rules by agent-semantic-authority.
- Before claiming a change works, run the local gates CONTRIBUTING.md lists for the changed
  surface. There is no remote CI (docs/decisions/adr-015-no-remote-ci.md).

## Customer prompts

docs/kiro.md lists starter prompts and the order of work for common tasks.
