# ADR-067: Release governed display names, never canonical keys

Date: 2026-09-14
Status: Accepted — implemented; dev acceptance recorded in `validation/RESULTS.md`

## Context

A released measurement answer read "ROAS (attributed, 14 day) was 0.9420 for channel
amazon_sponsored_brands, marketplace amazon_us, at the channel key and marketplace key grain."
Every value in that sentence is correct and none of it reads as a product. The canonical media
and marketplace keys are declared public vocabulary, so they were legal in public text — just
unreadable. Campaign keys are worse: they are protected identifiers, so
[ADR-066](adr-066-turns-default-disclose-and-repair-instead-of-failing.md) gated campaign
breakdowns behind a pre-query refusal until the governed label projection was wired in.

Every dimension already has a governed display name. The media taxonomy declares names for
ecosystems, platforms, channel groups, channels, and ad products; commerce identity declares
marketplace display names; the entity catalog's `public_labels` projection resolves campaign
keys to campaign names. The dashboard pages already render all of them; conversation was the
only surface still reading canonical keys aloud.

## Decision

**A released measurement row shows the governed display name for every dimension value that has
one, a protected identifier never reaches public text even as a fallback, and no answer is ever
refused for want of a label.**

1. After the query and before the answer is built, the measurement tool resolves the distinct
   dimension values its rows carry through `AnalyticalEntityCatalogPort.public_labels` and hands
   the answer a typed label mapping. The tool resolves labels itself, in-process, for the same
   reason business objectives are resolved in-tool: a second model-invoked call could be skipped
   or paired with the wrong rows.
2. Labels are applied to answer rows strictly *after* grouping. Grouping happens on canonical
   values; two campaigns whose labels collide (or share a placeholder) must remain two rows
   rather than merging into one fabricated figure.
3. One projection call per dimension column. The public-label contract downgrades duplicate
   resolved labels within a batch to ambiguous, and a channel and its ad product legitimately
   share a name ("Amazon DSP"), so a mixed batch would erase correct labels. Within one column a
   duplicate is a genuine ambiguity.
4. A protected identifier column (campaign today) is total: every value gets its resolved name
   or a deterministic placeholder ("Unnamed campaign 1", numbered in sorted canonical-value
   order). A resolved name that fails the public-text rule or exceeds the label bound also
   becomes a placeholder, at resolution time — never a post-query turn death, which is the
   failure ADR-066 diagnosed.
5. A declared public-vocabulary value with no label passes through unchanged; it is readable as
   itself and refusing or masking it would trade a real answer for polish.
6. Grain wording in claims and narrative uses customer words: physical columns lose their
   `_key` suffix, so "at the channel key and marketplace key grain" becomes "at the channel and
   marketplace grain" and "broken down by campaign_key" becomes "broken down by campaign".
7. Campaign public-label resolution no longer requires a platform context. A campaign is matched
   by its exact canonical key, unique within the authorized commerce scope; an impossible
   collision surfaces as ambiguous, never as a wrong label. An ASIN's marketplace context stays
   mandatory because its label is a per-marketplace title for the same key value.
8. The presentation-page path is untouched: its tooling carries no label resolver, because page
   projection substitutes its own labels against canonical selector values and rows already
   carrying display text would break that exact-match projection.

With the projection wired, ADR-066's undisplayable-grain gate is removed and campaign
breakdowns answer.

## Consequences

The conversational answer for the motivating question now reads "for channel Sponsored Brands,
marketplace United States, at the channel and marketplace grain", and a campaign breakdown
returns campaign names beside their figures. The claim renderings, the row values behind
governed tables, and the narrative scaffolding all read the same labels, because the
substitution happens once where the rows are built.

Label resolution adds one catalog projection per dimension column per answer, reading the same
published dimension state the query path already depends on. A projection failure is a genuine
dependency failure and is surfaced as one.

Two campaigns sharing one name both render as placeholders rather than as each other. The
honest fix is a disambiguated label, which needs the projection to return labels with
qualifiers rather than a bare ambiguous outcome; that is a recorded review trigger, not this
change.

> **Amended 2026-10-05 (W14; the duplicate-name review trigger is exercised).** A campaign
> label that repeats in one `public_labels` batch is now qualified with its channel's governed
> name: "<campaign name> (<channel name>)". The channel comes from the campaign alias and its
> name from a second projection over the channel column only (item 3 holds: one projection per
> column), so a canonical channel key never reaches public text. The campaign stays ambiguous,
> and renders as "Unnamed campaign N", when its channel has no single public name at the window
> end, when that name is its canonical key, or when the qualified labels still repeat (two
> same-named campaigns on one channel, or on two channels with the same name). A label that
> does not repeat is never qualified. Only the chat path reads `public_labels`, so item 8 is
> unchanged. Tests: `tests/unit/test_entity_catalog.py` (`test_w14_*`).

`amazon_asin` stays a passthrough: an ASIN is a readable public product identifier, and its
title labels need marketplace-context plumbing this change does not add.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Substitute labels before grouping | Two values sharing a label or placeholder would merge into one row, a fabricated aggregate at the exact point the platform promises never to fabricate. |
| Let the model fetch labels with a second tool call | It could skip the call or pair labels with the wrong rows; the same reasoning already keeps business objectives in-tool (ADR-011). |
| Pass campaign keys through and rely on the public-text validator | The validator rightly rejects them, but only after the query was paid for and the plan composed — the post-query turn death ADR-066 removed. |
| Truncate an overlong campaign name instead of placeholding | A truncated name is an altered claim about what the campaign is called; a placeholder is visibly no claim at all. |
| Keep the campaign platform-context requirement and thread platform through measurement | The exact canonical key already identifies the campaign within the authorized scope; the requirement added a refusal path, not safety, and threading context through every grain combination adds surface for no discrimination gain. |

## Review trigger

- ASIN title labelling ships, which needs per-marketplace context plumbing through measurement
  label resolution.
- Duplicate campaign names appear in real use, which argues for the projection returning
  disambiguated labels instead of a bare ambiguous outcome.
- A second protected-identifier grain appears, which (carried from ADR-066) argues for
  declaring displayability in the metric dictionary rather than in adapter code.
