# Insight prompt fragments (ADR-076 decision 6)

One shared foundation, one core per delivery tier, and one block per model slot.
A deterministic renderer (`domain/presentation/prompt_composition.py`) composes
`foundation + tier core + slot block` into the complete prompt text, which ships
through the registry's create → calibrate → activate path via
`validation/ship_prompts.py`; the registry remains the only store of the rendered,
immutable text that runs, and rendered prompts are never committed.

Every composed registry version records the sha256 digests of the exact normalized
fragments that rendered it, so a served narration traces narration → prompt version
→ these files at a specific commit.

**Status: production content (ADR-076 slice H, 2026-09-25).** Authored against the
low-level design's §6 standard: card tier conservative per the ADR's 2026-09-24
evidence limits, deep tier full-context with the owner-approved labeled-liberty
frame (owner input 4: clearly-labeled external hypotheses are welcome in the deep
tier; unlabeled external facts stay hard-refused). Since ADR-082 (2026-09-27) no text
check refuses a narration: the foundation's figure rule admits figures validly derived
from the supplied findings, and calibration and evaluation judge them. Every change to these files composes new registry versions through
create → calibrate → activate; the slot corpora gate each version before any
pointer moves.

Layout:

- `foundation.md` — the shared analyst framing, input contract, grounding rules,
  and silence rule (authoring standard items 1, 3, 4, 5 in
  `docs/ui/prompt-administration/low-level-design.md` §6).
- `tiers/card.md` — the card tier's output core (two-sentence budget, narration
  schema, output classes).
- `tiers/deep.md` — the deep tier's output core (structured exploratory analysis).
- `slots/<slot-id>.md` — one intent block per model slot (standard item 2).
