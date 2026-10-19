---
inclusion: auto
name: meta-documentation
description: Governs architecture, capability, concept, ADR, C4, arc42, and SVG diagram or call-flow Mermaid documentation when creating or revising it.
---

# Design doc rules

Templates, the notation legend and the review checklist: docs/conventions.md.
Never restate them here or in a design doc; point at that file.

## Ownership

Every fact has exactly one owning document. Parent docs describe children as
black boxes — responsibility and interface only — and point down. Children never
restate the parent. When a pointer feels insufficient, improve the owning doc
rather than duplicating into this one.

ADRs own why and history. Design docs own what is true now. Never copy an ADR's
reasoning into a design doc.

An ADR may be historical; it may never mislead. Refining, amending, or
superseding an accepted decision adds a dated marker at the top of the affected
ADR in the same change, pointing to the decision that changed it. A fully
reversed decision names its successor in its Status line. Never state current
behavior only in an ADR: the change that alters behavior updates the owning
design doc in the same change, and a reader learns current state from that doc,
never from an ADR.

## Structure

- arc42 decides what sections a doc has. C4 decides what a diagram shows and at
  which zoom level. Never invent a third scheme.
- Use the template for the doc type being edited, from docs/conventions.md §7,
  and keep its section order.
- Capability docs put the black-box contract before internal structure, so a
  reader who only consumes the capability can stop early.
- A deployable thing with its own interfaces is a capability. A pattern applied
  inside other capabilities is a concept. Filing a pattern as a capability is
  what makes six docs re-explain it.

## Diagrams

- Write the caption first, then build the diagram that delivers it. Every
  diagram carries a title stating type and scope, and a one-sentence takeaway
  beneath it.
- One level of abstraction per diagram. Never put components on a container
  diagram.
- One paragraph of explanation per diagram, maximum. Needing more than five
  bullets means it is two diagrams.
- Every element carries an explicit type and technology. Every line is
  unidirectional and specifically labelled. Never label a line "uses".
- Never introduce a colour, shape or border that is not already in the
  docs/conventions.md §6 legend.
- Colour is never the only carrier of meaning. Diagrams must survive greyscale.
- Refine to component level only when a capability is complex, risky or volatile. A black-box row in the capability index is a complete answer.
- Focused module diagrams are allowed only to clarify one bounded implementation mechanism inside a named component. Never diagram arbitrary classes or functions.
- Use Mermaid only for a genuinely non-obvious sequence or call flow, titled explicitly as a Sequence diagram or Call-flow diagram.
- Structural diagrams are hand-authored, self-contained SVG files stored in a `diagrams/` directory beside the one doc that owns them, and referenced from it as a Markdown image. Never generate, export, or rasterize them, and never reference an SVG outside the repository. Follow docs/conventions.md §6.

## Prose

Prose disambiguates a diagram; it never narrates one. If a sentence has to
explain which part of a diagram you mean, label the diagram instead.

Before finishing, run the review checklist in docs/conventions.md §9.
