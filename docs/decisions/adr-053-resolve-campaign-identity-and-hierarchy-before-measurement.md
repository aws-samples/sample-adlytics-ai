# ADR-053: Resolve campaign identity and hierarchy before measurement

Date: 2026-08-24
Status: Accepted

> **Proposed refinement, 2026-10-07.** [ADR-089](adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md) (Proposed) would match Amazon Ads API campaign references to AMC campaigns at read time through a same-record AMC alias, only on two-sided evidence (the alias and the same profile's own campaign listing), with the profile recorded as the alias's source account and many-to-one matches flagged, never merged. This decision stays in force, unchanged, until it is accepted.

## Context

At adoption, campaign measurement was implemented against exact `campaign_key` values carried by
advertising facts, while natural-language campaign lookup was not. The then-current tool surface
accepted only exact canonical metric names, canonical grain names, and fact-column filters; treating
an unresolved phrase as a campaign key could execute successfully and return zero rows, concealing
that no entity was resolved.

[verified] AMC exposes readable campaign and advertiser names and recommends
`campaign_id_string` as the cross-DSP/sponsored identifier field. DSP numeric and string forms refer
to the same identifier, while sponsored `campaign_id` and `campaign_id_string` are distinct populated
identifiers from different systems. Names are outputtable at the LOW threshold. These source facts
are owned by [sources/AMC](../sources/amc.md) §4.

Names are not identities. Two campaigns may share a display name, a campaign may be renamed, and the
same phrase may denote a campaign, advertiser, brand, or product family. Campaign results and
objectives also retain channel context because a campaign represented under different channels can
inherit different objectives and expose different metrics.

[ADR-045](adr-045-separate-delivery-commerce-marketplace-and-product-identities.md) already requires
qualified identifiers, named resolvers, mapping evidence, and refusal of unresolved or ambiguous
identity. It does not define campaign identity or the advertising hierarchy.

## Decision

**A campaign name is an effective-dated attribute and search alias. Measurement begins only after an
authorized resolver returns one canonical campaign identity and its required channel, platform,
commerce-scope, and marketplace context.**

A canonical campaign identity is opaque and scoped by canonical commerce scope and platform. Every
source alias retains:

- source system and source account or advertiser namespace;
- source-native campaign identifier and identifier kind;
- canonical campaign identity;
- platform and channel relationship;
- effective interval and mapping version; and
- source evidence establishing the mapping.

For AMC, the preferred source alias uses `campaign_id_string`; the numeric field may be retained as a
separate source alias where available but never substitutes for the preferred identifier. String
equality across source accounts, platforms, channels, or identifier kinds is not resolution.

For a request with a time window, a name alias is eligible when its effective interval overlaps the
requested window. A point-in-time request uses the alias containing that date; a request with no
measurement window uses the current alias only for candidate display, never to rewrite historical
identity. Several in-window aliases mapping to the same canonical campaign are one candidate, not an
ambiguity: the result states that the campaign was renamed and presents each label with its effective
interval while measurement remains on one stable key. If the same name maps to different canonical
campaigns in any overlapping part of the requested window, the outcome is `ambiguous`. Reuse outside
the requested interval does not create ambiguity. A window spanning two different campaigns' reuse
periods requires clarification rather than combining them.

The advertising hierarchy has governed roles: advertiser, campaign, ad group or DSP line item, and
placement or creative. A source-specific role maps to a canonical role only where verified source
semantics support it. Similar-looking DSP and sponsored fields do not become one level by naming
convention alone. Unimplemented hierarchy levels remain unavailable rather than null placeholders
that appear queryable.

Before matching or candidate disclosure, the resolver authorizes the candidate set against the
server-owned deployment-wide analytical scope and returns one of:

| Outcome | Meaning |
|---|---|
| `resolved` | one authorized canonical identity matched by exact key, exact source alias, exact normalized name, or approved customer alias |
| `ambiguous` | more than one authorized candidate remains; return differentiating attributes and require selection |
| `not_found` | no authorized candidate matches; do not run measurement or report zero activity |
| `unauthorized_or_hidden` | no candidate may be disclosed; use the same external wording as no match |
| `invalid_context` | marketplace, platform, channel, or effective date conflicts with the candidate |

Normalization may retrieve candidates by case, spacing, and punctuation. It may not silently select a
partial or fuzzy match. Approved customer aliases are versioned mappings, not model memory. Candidate
display includes readable name, platform/channel, marketplace, effective status, and the stable key
only where useful for disambiguation.

A phrase that matches multiple entity types produces a typed entity-kind ambiguity. “Arm & Hammer”
therefore requires campaign-versus-brand clarification unless explicit context or one exact
authorized alias settles the kind.

After resolution, measurement continues to use exact canonical keys. Campaign and channel remain in
the output grain when objective inheritance or channel-specific capability must be preserved. The
model may choose among resolver candidates after the user clarifies; it never creates or edits a key.

Campaign and advertiser names are untrusted source text. They remain labelled data in model context
and cannot become instructions.

## Consequences

AMC supplies the initial campaign dimension without an external campaign-metadata source. The
governed producer projection, canonical dimension and alias history, and query resolver are
implemented locally; development deployment, live acceptance, and each deployment's required
successful current campaign-entity publication remain before name-based questions are available
there.

Duplicate names, renamed campaigns, cross-channel campaigns, missing names, and brand-versus-campaign
phrases become explicit evaluation cases. Zero-row measurement is no longer used as entity-not-found
evidence.

The campaign list, campaign detail route, conversational resolver, objectives lookup, and
presentation API share one canonical identity and alias catalog. Presentation fixtures may use
readable fictitious names while retaining at least one missing-name fallback.

After period resolution fixes the final measurement window, the exact resolved selector is
reauthorized for that window immediately before it becomes a canonical measurement request. A
denial discloses no selector and runs no measurement. Under ADR-054, both checks enforce the
deployment-wide analytical scope; per-subject restrictions are unsupported.

Source-private hierarchy levels can be added without changing campaign measurement, provided their
mapping is explicit and their grain is declared by the metric model.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Use campaign name as the key | Names are mutable, duplicated, unscoped, and untrusted text |
| Pass the unresolved phrase as `campaign_key` | A valid zero-row query conceals failed resolution |
| Let the model select the closest string | Similarity is not identity and can cross entity types, channels, or customer scopes |
| Put every source level into one universal hierarchy immediately | DSP and sponsored roles are not proven equivalent at every level |
| Resolve after querying all campaigns | Discloses unauthorized names, increases result width, and makes the model perform the identity join |

## Review trigger

Revisit when a source provides a globally stable campaign identity across platforms and source
accounts, or when a verified hierarchy level cannot be represented as an effective-dated source
alias to one governed canonical role.
