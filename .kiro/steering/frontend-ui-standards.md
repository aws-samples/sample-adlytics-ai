---
inclusion: fileMatch
fileMatchPattern: ["frontend/**"]
description: Governs window-scope integrity, temporal labels, designed absence states, and progressive loading when editing frontend surfaces.
---

# Frontend surface standards

Rationale and the mandated composition: docs/ui/README.md and the page packages under
docs/ui/. Accessibility and theme rules are owned there and are not restated here.

## Window-scope integrity

- Every displayed value derives from the page's single resolved window; the invalidation
  contract on a window change is owned by docs/ui/README.md. Never render values from two
  windows side by side. A deep-dive payload may carry a sibling preset's direction as a
  typed, direction-only finding per ADR-076 decision 5; no second-window figure ever
  renders.
- Never hardcode a grain, period length, or date word in a rendered label, title, or column
  header. Every temporal word derives from the resolved window and its server-declared display
  grain.

## Absence is a designed state

- Never render an internal reason code, error class, or enum token in UI text. Every
  unavailable, empty, or partially covered state names, in plain words, what is missing and
  the nearest available alternative or next step.
- A partially covered window renders its covered span normally beside a dated gap region;
  never an empty chart, never a broken page.

## Progressive loading

- A loading element is a skeleton that reserves the final position and size; data replaces it
  in place. Never a blocking full-page spinner, and never layout shift when data arrives.
- Render ready content immediately; never hold ready content back to batch it with slower
  content.
