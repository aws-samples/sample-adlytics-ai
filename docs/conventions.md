# Design Doc Conventions

| | |
|---|---|
| **Status** | Active — this document governs every doc under `docs/` |
| **Last updated** | 2026-09-04 |
| **Enforced by** | `.kiro/steering/meta-documentation.md` (auto-loads when editing `docs/`) |

This is the notation key and the document schema. Every diagram in this repo is read against the legend in §6, and every document follows one of the templates in §7.

## 1. The one rule

> **Every fact has exactly one owning document.**
> Parent docs describe children as black boxes — responsibility and interface only — and link down. Children never restate the parent.

This exists because the failure we actually had was not "too little visual, too much prose". It was that no document owned any particular fact, so all of them re-explained context in order to stand alone. That produces prose by necessity. Fixing ownership is what makes the docs short; diagrams are how the remaining content is best expressed.

Practical consequences:

- A capability doc never re-draws the system context. It links up.
- A shared pattern (auth, tracing, error handling) is documented **once** as a concept and referenced, never re-explained per capability.
- An ADR owns the *why* and the history. A design doc owns *what is true now* and links to the ADR. Never restate an ADR's reasoning.
- If you are about to explain something already explained elsewhere, link instead. If linking feels insufficient, the owning doc is the one that needs improving.

## 2. Frameworks — arc42 for structure, C4 for diagrams

These are complementary by design and are used in their intended division of labour. We are not inventing a third scheme.

| Concern | Framework | What it gives us |
|---|---|---|
| What documents exist, and what sections they have | **[arc42](https://arc42.org/overview)** | a tailorable 12-section template; we use a subset |
| What the diagrams show, and at which zoom level | **[C4](https://c4model.com/)** | defined structural levels, with a focused module view only for a bounded implementation mechanism |

The mapping, per [C4's own FAQ](https://c4model.com/faq):

| arc42 section | C4 diagram | Lives in |
|---|---|---|
| §3 Context and scope | **Level 1 — System Context** | `architecture.md` |
| §5.1 Building block view, level 1 | **Level 2 — Container** | `architecture.md` |
| §5.2 Building block view, level 2 | **Level 3 — Component** | capability docs |
| §5.2 Building block view, focused implementation | **Module — bounded implementation mechanism** | the owning capability's low-level design |
| §7 Deployment view | Deployment | `architecture.md` §technology |
| §6 Runtime view | Sequence or Call-flow | sparingly — see §5 |

A module diagram is not a general C4 level or a code map. Use it only to clarify one bounded implementation mechanism inside a named component; do not diagram arbitrary classes or functions.

## 3. Zoom levels — which doc owns what

A reader should be able to enter at their preferred depth and go deeper only if they need to.

| Level | Document | Owns |
|---|---|---|
| **L0** | `README.md` | entry point and reading paths. No architecture content. |
| **L1** | `architecture.md` | system context, container view, quality goals, constraints, the **capability index**, trust boundaries, technology summary |
| **L2** | capability docs | one capability's internals. Black-box contract first, then structure. |
| **L2** | concept docs | a pattern that recurs across capabilities |
| **L2** | `sources/*.md` | **external source-system facts** — schemas, limits, quirks, citations. The owning docs for anything true about AMC or Vendor Central. |
| **L3** | `canonical-model.md` | **field-level reference** — every column of every dimension and fact, plus the source crosswalk |
| **L3** | `contracts/` | the data contracts — the mapping from a source into the canonical model |
| **M** | a capability's low-level design | one focused module or implementation mechanism inside a named component; never an arbitrary class or function map |
| **X** | `decisions/adr-*.md` | why, and the history of why |
| **X** | `glossary.md` | cross-cutting support |

These conventions govern this directory. Documents that track *current coordination work* rather than design — the active roadmap, gaps narrative, open-question notes — live in local, untracked custody outside the zoom levels above; the [decision coverage registry](decision-coverage-registry.md) publishes the identifier inventory they elaborate. The notation legend in §6 still applies to any diagram a local document carries.

**Capability vs concept — the test that decides.** If it is a deployable thing with its own interfaces, it is a capability. If it is a pattern applied *inside* other capabilities, it is a concept. Getting this wrong is the main source of duplication: six capability docs each re-explaining how auth works is the predictable outcome of filing auth as a capability.

## 4. Refine only what is worth refining

Do not produce a component-level diagram for every capability. C4 marks level 3 as *not* recommended by default, and arc42 says to refine only a few building blocks and to prefer relevance over completeness.

Refine when a capability is **complex, risky, or volatile**. Leave it as a black box in the capability index when it is standard or stable. A black-box row plus an interface list is a complete and legitimate answer.

A focused module view is a further refinement only when a specific implementation mechanism needs explanation. It names that mechanism's viewpoint and depth, remains inside one named component, and omits arbitrary class and function relationships.

## 5. Diagram rules

Enforceable, and checked in review.

1. **Caption first.** Write the takeaway sentence, *then* build the diagram that delivers it. If the diagram does not deliver the caption, the diagram is wrong.
2. **One paragraph of information per diagram, maximum.** If it needs more than five bullets to explain, split it.
3. **One level of abstraction per diagram.** No components on a container diagram. This is the most common violation and the easiest to spot in review.
4. **Every element** carries an explicit type and, for containers and components, an explicit technology.
5. **Every line** is unidirectional and specifically labelled. Not "uses". Cross-container lines name the protocol.
6. **Colour is never the only carrier of meaning.** Diagrams must survive greyscale and colour blindness.
7. **Sequence and Call-flow diagrams are used sparingly** — only where an interaction is genuinely non-obvious. Two per capability is already a lot.
8. **Focused module views are bounded implementation views, not code maps.** They may clarify one named mechanism inside a component; never diagram arbitrary classes or functions.
9. **Generate what rots fastest.** Deployment topology from CDK output and data contracts from schemas are both better generated than written.

A diagram should be *mostly* understandable standing alone. Prose disambiguates; it does not narrate the picture. If a paragraph explains which part of a diagram you mean, put a label in the diagram instead.

## 6. Notation legend — the canonical key

This legend lives here, once, and every diagram is read against it. Do not restate it per document.

### Diagram format and viewpoint

Structural **System Context, Container, Component, Deployment, Capability, and focused Module** diagrams are hand-authored, self-contained SVG files in a `diagrams/` directory beside the one Markdown document that owns them, referenced from it as a Markdown image. Never generate, export, or rasterize a diagram, never put `<svg>` inline in Markdown, and never reference an SVG outside the repository.

Mermaid is reserved for an interaction that is genuinely non-obvious and whose immediately preceding bold caption explicitly says **Sequence diagram** or **Call-flow diagram**. A Sequence diagram starts with `sequenceDiagram`; a Call-flow diagram starts with `flowchart` or `graph`. Structural diagrams are never Mermaid. A Call-flow diagram that must render where the host's Mermaid renderer may not load, such as the root README's, is an SVG file under the same rules as a structural diagram.

Each diagram title states its type, scope, particular viewpoint, and depth. The nonempty bold caption immediately precedes its diagram; the `> **Takeaway.` block immediately follows it and gives the one-sentence takeaway. A focused Module diagram may explain one named implementation mechanism inside a component, but is never an arbitrary class or function diagram. One diagram has one viewpoint and one depth; make another diagram rather than mixing perspectives.

### SVG file requirements

A diagram file is self-contained and safe to render as an image:

- The root declares `xmlns="http://www.w3.org/2000/svg"` and has a valid `viewBox` of four finite numbers with positive width and
  height, `role="img"`, `focusable="false"`, and `aria-labelledby`.
- `aria-labelledby` names a non-empty local `<title>` and `<desc>`; every `id` is unique within the file.
- The first drawn element is a `<rect>` covering the whole `viewBox` with `fill="#ffffff"` and no opacity, `style` or `transform`, so the diagram stays legible on a dark page.
- Keep labels as SVG text; do not use `script`, `foreignObject`, event-handler attributes, or external URL-bearing `href`, `xlink:href`, `image`, or `style` references.
- Define markers, gradients, and other reusable drawing details locally in the same file.

The owning document references the file as a Markdown image whose target is `diagrams/<name>.svg`, alone on its line with a blank line before it. The alt text is the `<title>` text, a full stop and a space, then the `<desc>` text, so a screen reader hears what the file's own label says; neither may contain a square bracket. Each file is referenced by exactly one document.

The checker validates these file, reference, and safety requirements; review still checks whether the picture says the right thing.

### Colour

| Colour | Meaning | SVG fill / stroke |
|---|---|---|
| **Slate / white** | actor, user-facing surface, or neutral step | `#ffffff` / `#9aa8bb` |
| **Red** | access control and security boundary | `#fff7f2` / `#b54545` |
| **Purple** | agent, and anything non-deterministic | `#f8f5ff` / `#6b4fbb` |
| **Green** | deterministic compute (tools), or a good terminal state | `#f3fbf6` / `#2e7d4f` |
| **Blue** | data, storage, and the pipeline that fills it | `#f2f8fe` / `#2f6fb2` |
| **Amber** | the canonical model, quality gates, scheduled work | `#fff4e0` / `#c77700` |
| **Grey, dashed** | planned, not yet built | `#ffffff` / `#9aa8bb` |

The purple/green split is load-bearing: it is how a reader sees at a glance where determinism ends and inference begins, which is the platform's core invariant.

### Shape

| Shape | Meaning | SVG equivalent |
|---|---|---|
| Rectangle | container or component | `<rect>` |
| Cylinder | data store | a cylinder silhouette built from `<path>` and `<ellipse>` |
| Diamond | decision or gate | `<polygon>` with four points |
| Dashed border | planned, not built | `stroke-dasharray="5 4"` |

### Lines

| Line | Meaning | SVG equivalent |
|---|---|---|
| Solid arrow | synchronous call, labelled with protocol or payload | solid `<path>` or `<line>` with a local `marker-end` |
| Dotted arrow | asynchronous, scheduled, or a validation relationship | `stroke-dasharray` plus a local `marker-end` |
| Thick border (3px) | the element the diagram is actually about | `stroke-width="3"` |

### Grouping and boundaries

| Construct | Meaning | SVG equivalent |
|---|---|---|
| Group | related drawing elements | `<g>` |
| Boundary | an account, a system, a deployment unit, or a trust zone — never a category or tidy-up | labelled `<g>` containing an unfilled boundary `<rect>` and its label `<text>` |

A boundary label names what kind of boundary it is and who owns it. Keep it unfilled: dashed is already reserved for planned work, and a fill would collide with element colours. `architecture.md` §4 is the reference use — three account boundaries on a context diagram.

### SVG layout notes

- Use the `viewBox` to keep diagrams legible in narrow viewers; do not rely on a fixed rendered width.
- Keep connector markers and definitions local to the SVG, and route edges to labels that remain readable without hover state.
- Use groups and labelled boundaries to contain related elements rather than drawing a category box around unrelated items.
- Keep a diagram small enough to understand without zooming into unrelated details; split a crowded view by viewpoint or depth.

## 7. Document templates

### Root — `architecture.md`

arc42 order, trimmed to what this system needs.

```
1. Purpose and scope          — and explicitly what this doc does NOT cover
2. Quality goals              — 3–5, as scenarios, not adjectives
3. Constraints                — technical and organisational
4. Context                    — C4 L1 diagram + external systems table
5. Solution strategy          — the few decisions that shape everything; link ADRs
6. Container view             — C4 L2 diagram + THE CAPABILITY INDEX
7. Trust boundaries           — security-relevant structure
8. Technology and IaC         — service inventory and construct maturity
9. Runtime view               — one Sequence or Call-flow diagram, only when the canonical request path is non-obvious
10. Crosscutting concepts     — index with links, no content
11. Decisions                 — ADR index, one line each
12. Open questions and risks  — link out
```

### Capability — `<capability>.md`

A capability package is the approved realization of this template: `README.md` carries §§1–3,
`architecture.md` carries §§4–5, and `low-level-design.md` carries §§6–9. Each companion links to the
preceding and following package document. A bounded internal component may be a named companion in the
same package; it is not a second capability unless it has its own deployable interface.

Black-box contract **before** internal structure, so a reader who only needs to *consume* the capability can stop after §3.

```
1. Purpose and scope          — 1–2 sentences; and what lives elsewhere
2. Where this fits            — one line + link UP; name the parent element refined
3. Black-box contract         — responsibility, inbound/outbound interfaces,
                                owner, code location, relevant qualities
4. Internal structure         — C4 L3 diagram + why it is decomposed this way
5. Key runtime flows          — 0–2 explicitly titled Sequence or Call-flow diagrams, only where non-obvious
6. Interfaces and contracts   — links to contracts/ and to specifying tests
7. Concepts applied           — links only; never re-explain a shared pattern
8. Decisions                  — links to ADRs only
9. Risks and open questions   — local to this capability
```

### Source reference — `sources/<source>.md`

For an external system we read from but do not control. These are the owning docs for source facts; no other doc restates them.

```
1. What this is                — and what depends on it
2. Provenance                  — when researched, how verified, confidence per claim
3. Available data              — tables/reports and their shape
4. Identifiers                 — join keys and their exact semantics
5. Constraints                 — limits, thresholds, quotas that shape design
6. Time, money, currency       — normalisation facts
7. Freshness and restatement   — what changes after first publication
8. Access mechanics            — how data is actually retrieved
9. What this source cannot supply
10. Open items                 — unverified, and what would resolve it
11. Citations
```

Every factual claim carries a verification marker: **[verified]** content was fetched and read, **[snippet]** search snippet only, **[unverified]** could not confirm. Stating confidence is not optional in these docs — they are the basis for schema decisions.

### Primer — `<topic>.md`, no subdirectory

```
1. How to read this            — an audience table with a stated stopping point
2. The crux                    — the one or two ideas everything follows from
3. Vocabulary                  — defined here, pointed at from everywhere else
4. Why not the obvious thing   — the naive design, and how it fails
5. …the mechanics              — for the reader who continues past the stopping point
N. Worked example              — one thing traced end to end
N+1. Where to go next          — a question-to-doc table
```

**A primer owns concepts and never values.** No column names, no scaling factors, no valid
grains — those live in the reference docs and are checked by `make check-lineage` and
`make check-model`. A primer is checked by neither, so anything in it that a gate would
otherwise verify goes stale silently. Concepts change on the timescale of ADRs; field values
change weekly.

Two rules keep it from becoming a second reference:

- **It absorbs, never duplicates.** When explanatory material already sits inside a design
  doc, move it and leave a pointer. `capabilities/data-layer/README.md` moved into
  `understanding-the-data-model.md` for exactly this reason.
- **It names its audience and its stopping point.** A primer serving two audiences at two
  depths is one document with a marked exit, not two documents — two would drift, because
  the concepts are identical and only the depth differs.

A primer may be **partial on purpose**, provided the gaps are marked as questions rather
than filled with plausible prose. `business-context.md` §6 is the pattern: what nobody has
decided, listed as questions with owners. This is the same discipline as the
`[verified]` / `[unverified]` markers in the source docs.

### Concept — `concepts/<concept>.md`

Every concept follows the Provenance Chain schema:

```
# Concept — <name>
metadata: Level · Status · Last updated · Owns · Applied by · Conventions
1. What this concept is        — ownership boundary and what lives elsewhere
2. The rule                    — one prohibition plus necessary invariants
3. How it works                — one titled diagram and bounded handoff/outcomes
4. Rejected alternatives       — compact alternative-to-ADR pointers
5. Decisions                   — Decision | Owner table
6. Open items                  — Item | What resolves it | Blocks table
```

A concept owns a reusable pattern, not a capability’s deployment topology, source facts, field reference,
ADR history, or acceptance ledger. Empty open items are stated explicitly rather than invented.

### ADR — `decisions/adr-NNN-<slug>.md`

Canonical five: **title, status, context, decision, consequences.** One decision per file. Add "alternatives considered" and a "review trigger" when useful. Never duplicate an ADR's reasoning into a design doc.

## 8. Tooling — hand-authored SVG files now, with a defined trigger to model

**Decision: hand-authored SVG files for structural diagrams, each beside the one document that owns it, Mermaid only for titled Sequence and Call-flow diagrams, plus the discipline in §1 and §9.**

The tradeoff is explicit: hand-authored SVG is owned beside its prose and can be reviewed as markup, but it is not a shared architecture model. The drift risk remains real; the first-order control is fact ownership (§1), which is tool-independent. The §6 file, reference, accessibility, and safety contract is enforced by `scripts/check-diagrams.mjs`.

Changed 2026-10-02: structural diagrams moved from inline SVG to SVG files. GitHub's Markdown renderer strips inline `<svg>` and runs its text together [verified 2026-10-02, GitHub Markdown API probe of a synthetic snippet], and GitLab documents the same sanitizer allowlist, which has no `svg` [verified 2026-10-01, https://docs.gitlab.com/user/markdown/]. Both keep a Markdown image. The file is still the hand-authored source, so nothing is generated and no freshness gate is needed. Native Mermaid stays for Sequence and Call-flow diagrams because it degrades to readable source when the host cannot render it.

**Move to Structurizr DSL when any of these becomes true:**

- the same element appears on more than three diagrams
- a rename causes a diagram to silently disagree with another
- more than one person is authoring diagrams concurrently
- capability count grows past roughly ten

## 9. Review checklist

For any PR touching `docs/`. Adapted from [C4's review checklist](https://c4model.com/diagrams/checklist).

- [ ] Does every diagram have a title stating type, scope, viewpoint, and depth?
- [ ] Does every diagram have a one-sentence takeaway beneath it?
- [ ] Is a structural diagram a self-contained SVG file in the owning document's `diagrams/` directory, referenced once with the §6 alt text, or is Mermaid an explicitly titled Sequence or Call-flow diagram?
- [ ] Is every element's type and technology clear?
- [ ] Is every line labelled specifically, and unidirectional?
- [ ] Is every colour, shape, and border style in the §6 legend?
- [ ] Is the diagram at exactly one level of abstraction and one viewpoint?
- [ ] Is a module view bounded to one named implementation mechanism rather than arbitrary classes or functions?
- [ ] Does this doc restate anything owned by another doc? (If yes, link instead.)
- [ ] Is every acronym either obvious or in the glossary?
- [ ] Does it survive greyscale?

## 10. Anti-patterns

Attested by the sources, and worth naming so they can be called out in review.

| Anti-pattern | Why it hurts |
|---|---|
| **Mixing abstraction levels or viewpoints** in one diagram | produces a picture that often makes no coherent sense |
| **Copy-pasting an element** onto another diagram | violates DRY with no model to keep copies in sync; the main drift mechanism |
| **One big diagram** | cognitive load too high; if nobody understands it, nobody looks at it |
| **Decorative diagrams** | any graphic makes readers *like* a doc more, but only instructive ones help them learn — so a visual-first rewrite can improve satisfaction and do nothing for comprehension |
| **Documenting for completeness** | prefer important, surprising, risky, complex or volatile; omit normal and boring |
| **Arbitrary code maps** | class and function diagrams rot fastest; a module view is useful only for one bounded implementation mechanism |
| **External SVG dependencies** | an external asset, script, or reference breaks ownership, reviewability, and safe rendering |
| **The melting-pot concepts doc** | a crosscutting section becomes a dump for anything that did not fit; prune it |
| **Restating an ADR** | two copies of a rationale, one of which will go stale |

## 11. Sources

[C4 model](https://c4model.com/) · [C4 notation](https://c4model.com/diagrams/notation) · [C4 review checklist](https://c4model.com/diagrams/checklist) · [C4 FAQ](https://c4model.com/faq) · [arc42](https://arc42.org/overview) · [arc42 §5 building block view](https://docs.arc42.org/section-5/) · [arc42 Tip 5-6 black boxes](https://docs.arc42.org/tips/5-6/) · [arc42 Tip 5-27 refine only a few](https://docs.arc42.org/tips/5-27/) · [arc42 minimal documentation](https://faq.arc42.org/questions/B-4/) · [Google — Illustrating](https://developers.google.com/tech-writing/two/illustrations) · [ADR hub](https://adr.github.io/) · [Simon Brown — diagrams as code](https://dev.to/simonbrown/diagrams-as-code-20eo)
