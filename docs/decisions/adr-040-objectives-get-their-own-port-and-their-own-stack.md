# ADR-040: Objectives get their own port and their own stack, and resolution reports its scope

Date: 2026-08-23
Status: Retired — the business-objectives feature was removed
**Amends [ADR-011](adr-011-strategy-layer.md)** in one respect — where objectives are read *through* —
and adds three decisions it left open. Every other property of ADR-011 stands: scoped resolution,
narrowest-first precedence, advisory by default, DynamoDB rather than the lake, attachment inside the
tool, and `mixes_objective` as the highest-value part of the feature.

## Context

ADR-011 settled the design and named the storage. Implementing it surfaced one contradiction in its own
text and three questions it did not reach.

**The contradiction.** ADR-011 says *"Read through the existing `KeyValuePort` with a new namespace"*
and, eleven lines later, describes the read path as *"DynamoDB → `BatchGetItem` objectives for those
`campaign_keys`"*. Those cannot both be true. `KeyValuePort` exposes `get(namespace, key)` and
`put(namespace, key, record)` — single-key, and `StoredValue` is a flat
`dict[str, str | int | Decimal | bool | None]` with no list or nested type. There is no batch read to
issue and no shape to put a set of assignments in.

**And that port has already refused this once, for a reason that applies here.** `key_value.py`'s own
docstring records that a `FRESHNESS` namespace was declared there in anticipation and then **removed**,
because `put` replaces whatever is at a key and the registry's central rule is that a write moving a
partition backwards must be refused. The precedent is not "the port was inconvenient"; it is that
overloading an artefact store with something whose access pattern it cannot express is how a rule stops
being enforceable.

Three further questions, none of which ADR-011 answers:

- Which stack owns the table.
- Whether the layer can be turned off, and at what granularity.
- Whether a caller can tell *why* an objective applied.

That third one is not cosmetic. `ObjectiveSet.resolve` returns a bare `Objective`, so "this campaign was
deliberately set to defend share" and "this campaign is unconfigured and inherited the account posture"
are indistinguishable at every call site. ADR-011's own mitigation for the expectation problem is to
*surface when strategy influenced an answer*, and that mitigation is unwritable against a return value
that does not say.

## Decision

### Objectives get their own port

`ports/outbound/objectives.py`, two methods, matching the two read paths ADR-011 already separated:

```python
def posture(self) -> ObjectiveSet | None:      # account + channel + notes. For the prompt.
def including(self, campaign_keys) -> ObjectiveSet | None:   # posture plus the campaigns that differ
```

**`including` returns one `ObjectiveSet`, not a mapping of campaign to objective**, and that is the
choice worth explaining. `ObjectiveSet` already models assignments at all three scopes and already
implements narrowest-first precedence. Handing the tool a posture *plus* a separate mapping would make
the tool combine them, which is a second implementation of `resolve` in the place least suited to
holding it. Returning one set means the domain resolves, as designed, and the adapter only decides which
assignments to fetch.

It is also **one round trip**. `BatchGetItem` takes the posture key alongside the campaign keys, so
ADR-011's "one `BatchGetItem` per tool call" is satisfied literally rather than becoming a `GetItem`
plus a batch.

**Why the posture is needed inside the tool at all**, since ADR-011 puts it in the prompt: because
`mixes_objective` is wrong without it. ADR-011 says only exceptions are configured, so a response with
five campaigns where one is `defend_share` and four inherit `maximise_roas` must report five objectives.
Attaching only the configured ones would report one, `mixes_objective` would be `False`, and the
comparison warning that ADR-011 calls the highest-value part of the feature would never fire. The bug
would be invisible: an answer that should carry a caveat simply would not.

### Objectives get their own stack

`StrategyStack`, RETAIN, with point-in-time recovery. [ADR-012](adr-012-stack-composition-and-config.md)
cuts by destroy-together first, rate of change second, logical unit third, and applied in that order
this is not `DataStack` and not `AgentStack`:

| Test | Result |
|---|---|
| Destroy together | RETAIN, like `DataStack`'s two load-state tables. So not `AgentStack`, which is DESTROY — and ADR-012 forbids a RETAIN resource sharing a stack with a DESTROY one |
| Rate of change | Changes when a human edits a form. `DataStack` changes on a schema or contract edit. Nothing else in the platform changes on this trigger |
| Logical unit | A distinct unit read by two consumers, the measurement tool and the conversation route |

It is the **only customer-authored** state in the platform, and that is the sharpest argument for its own
blast radius: everything else in a RETAIN stack can be rebuilt from a source delivery, and this cannot
be rebuilt from anything. A wrong `cdk destroy` naming `DataStack` costs a reload; one naming this costs
the customer's own configuration.

### The layer defaults on, with one global kill switch

`ADL_STRATEGY_ENABLED`, deploy-time, defaulting to **on** — a configured value like every other, resolved
through `adapters/config.py`.

The switch exists because `notes` is authenticated customer free text injected verbatim into a prompt
and [G-05](../decision-coverage-registry.md#g-05--no-prompt-injection-defence-for-data-borne-content) is open. That is
a real exposure and there has to be a way to remove it without a code change.

**It is not a true incident switch and the difference is worth stating.** A deploy-time value is read at
module scope, so flipping it needs a redeploy; a request-time switch would need `settings()` inside the
request path, which the configuration rules forbid for a reason — a container that cannot resolve its
configuration must fail before it becomes healthy. Making this one exception would be a separate
decision, and it is not taken here.

**No per-scope enable or disable.** Account scope cannot be disabled: it is the terminal fallback and
`ObjectiveSet.__post_init__` requires exactly one account assignment, so a set without it is
unconstructible. Channel and campaign scopes are already optional by not being configured, which is
ADR-011's *configure exceptions only*. A flag would be a second mechanism for a fact the data already
states.

### Resolution reports its source scope

`ObjectiveSet.resolve` returns a `Resolution` carrying the objective, the scope that supplied it and that
scope's key, rather than a bare `Objective`.

This is what makes ADR-011's expectation-management mitigation writable. "Defending share, set on this
campaign" and "defending share, inherited from the account" are different statements to a user, and only
the first is evidence the customer said something about that campaign. It also means a narration can say
*where* guidance came from, which is the difference between labelling something guidance and merely
calling it that.

Two parameters in, one result object out, per the repository's own signature rules. `account_objective`
keeps returning a bare `Objective`, because its scope is in its name.

## Consequences

**Resolved 2026-08-23.** `agent-strategy-objectives` had required objectives to live in DynamoDB and be
read through `KeyValuePort`, contrary to this ADR. Its Storage section was aligned with this ADR after the
approved steering update. The file's `fileMatchPattern` gained `src/ports/outbound/objectives.py` in the
same edit, because the rule naming that port did not load when the port itself was being edited — which is
where a second port would be wired.

**Historical deployment consequence.** The retired `StrategyStack` required an operator to deploy its
published table before the former consumers. Current manual sequencing is in the
[root README](../../README.md#deploy); no source-owned deploy-order symbol remains.

**Two consumers now need the same grant, and they are asymmetric.** The measurement function reads posture
plus campaigns (`GetItem` and `BatchGetItem`); the conversation function reads posture only (`GetItem`).
Both read-only: a tool or a route that could write an objective would be able to change what the platform
believes the customer wants.

**Notes reach a prompt verbatim, by design, and that is the residual risk.** ADR-011 requires it — the
2,000-character cap exists precisely so notes always fit and are therefore always present, rather than
being retrieved and sometimes missed. What this decision adds is the switch, not a defence. G-05 remains
open and is not narrowed by anything here.

**`resolve`'s new return type is a breaking change to a tested and mutation-reviewed module.**
Its mutation review ([ADR-013](adr-013-mutation-testing.md)) covered the old signature. The call sites are
updated in the same change; the review is re-run rather than assumed to still apply.

**One `BatchGetItem` is capped at 100 keys**, so a response with more campaigns than that is chunked. Not
mentioned in ADR-011, and the alternative to chunking is a silent partial read — which would omit
objectives for the campaigns past the boundary and, again, produce a wrongly absent caveat rather than an
error.

## Alternatives rejected

**Extend `KeyValuePort` with a batch read and a nested value type.** Honours ADR-011's wording. Rejected
because it makes one port serve two access patterns with different rules, which is exactly what the
`FRESHNESS` namespace removal established as the mistake — and because a nested `StoredValue` would widen
the type for every existing caller of a port whose whole value is that it is narrow.

**Store objectives in `DataStack` beside the load-state tables.** Same removal policy, one fewer stack.
Rejected on rate of change and on what the tables *are*: those two are written by the load path on a
schedule, this one by a human editing a form, and grouping them puts customer configuration behind a
changeset that a contract edit raises.

**One item per assignment, keyed on `(scope, scope_key)`.** The obvious normalisation. Rejected because
the prompt path then cannot read the posture in one request — it would need a `Query` per scope, or a
scan, or a secondary index over a table with a handful of rows in it. The posture is small and always read
whole, so it is one item; campaigns are numerous and read selectively, so they are one item each. The key
design follows the access pattern rather than the shape of the domain object.

**Store the `binding` flag.** It is modelled on `Objective` and unused in V1. Rejected because a stored
field that nothing reads is an invitation to start honouring it, and ADR-011 is explicit that enforcement
is a decision rather than an implementation detail. It defaults to `False` on read, and the day binding is
wanted the storage change is one attribute.

**Make the kill switch request-time.** Genuinely better for an incident. Rejected here rather than
dismissed: it conflicts with `settings()` at module scope, and resolving that conflict properly is its own
decision. Recorded as a review trigger.

## Review trigger

Any of these reopens it:

- An incident requires the strategy layer off in minutes rather than in a deploy, which makes the
  request-time switch worth its own decision.
- G-05 gains a defence for data-borne prompt content, which changes what the switch is for.
- A fourth scope is wanted — ADR-011 already names product tier as a deliberate omission, and a scope
  change is that ADR's to make, not this one's.
- Objectives need history, which changes the key design: this table holds current state per scope, and a
  history would need a sort key and a different thing entirely.
- More campaigns carry their own objective than fit in a response, which is ADR-011's own review trigger
  about aggregation and would reopen the read path here with it.
