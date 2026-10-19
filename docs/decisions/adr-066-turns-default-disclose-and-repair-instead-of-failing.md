# ADR-066: Turns default, disclose, and repair instead of failing

Date: 2026-09-14
Status: Accepted — implemented; dev acceptance recorded in `validation/RESULTS.md`; the first review trigger fired and ADR-067 wired the label projection, removing the undisplayable-grain gate; §5 (the server repair round) and §7 (discarding narration between tool rounds) amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md), which has no repair round and streams that narration (deployment state in ADR-083's status line)

## Context

A live turn asking for campaign ROAS ended in the generic "temporarily unavailable" sentence.
The diagnosis found a chain of compounding rules, each defensible alone, that together made a
healthy adaptive turn unpublishable:

- A question naming no reporting window was refused at semantic resolution, although the
  platform knows exactly which windows are published.
- A window the platform has not published raised a `DependencyError` — an alarm-class failure —
  although "no data for that window" is an answer.
- Every refused exploratory tool call added a mandatory refusal rendering to the answer's
  obligations, so the more dead ends the model adapted around, the higher the bar for its final
  plan.
- The Gateway finalizer's plan check is a custody-blind subset of the API's verifier
  ([ADR-042](adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md)), so the model
  could be told "accepted" for a plan the API rejected, stop trying, and end the turn plan-less
  — which was then also misclassified as unavailability.
- A notice block admitted only qualifier and refusal renderings, so a server-authored context
  disclosure could not live where the model naturally placed it.
- A campaign breakdown fetched real figures and then failed public-text validation, because its
  rows are identified by protected canonical keys and the governed label projection
  (`AnalyticalEntityCatalogPort.public_labels`) exists but is wired to nothing.

## Decision

**A turn that can be answered with published data is answered, every server-made choice is
disclosed in the released text, and the one failure the model cannot see is repaired by the
server once — so the generic failure sentence is reserved for genuine outages.**

1. An unnamed reporting window resolves to the newest published coverage of the metric's own
   sources, recorded as `FieldOrigin.GOVERNED_DEFAULT`. That origin flows through resolution
   provenance into a mandatory context rendering, so the released answer cannot omit the
   sentence naming the window it chose. Only an unambiguous metric reference defaults; a
   comparison's missing prior period still refuses.
2. A requested window the platform has not published is a `DomainError` refusal whose reason
   names the covered days inside the request and the next step. It never alarms.
3. A refused tool call superseded by a later success of the same tool in the same turn keeps
   its renderings selectable but stops being an obligation. A standing refusal — one the model
   never got past — remains fully mandatory, as do all qualifiers (ADR-062: a refusal blocks
   only the claims that depended on it).
4. A notice may carry a server-authored `CONTEXT` rendering beside qualifiers and refusals.
   Claims and recommendations remain barred from notices.
5. When a Harness turn ends without an API-accepted plan, the adapter sends exactly one
   server-authored repair message on the same session, carrying the API's violation codes and
   the missing content-free reference IDs, and seeds the repair round with the prior round's
   custody and trajectory so a corrected plan may cite evidence already gathered. A turn whose
   repair also fails is a governed publication refusal, never an availability incident.
6. A breakdown grain whose row values are protected canonical identifiers with no wired label
   projection (campaign today) refuses before the query, naming the breakdowns that display.
   Wiring `public_labels` into row shaping is the recorded follow-up that removes the gate.
7. Assistant narration between tool rounds is discarded, never fatal; an unpublishable pre-tool
   narration drops the preface rather than the turn.

## Consequences

The four layered dev validation questions — the originally failing campaign-ROAS question, a
three-ask question mixing an unavailable breakdown with a deferred metric and general
explanation, an explicitly uncovered window with a fallback ask, and an unsupported ranking
with a "whatever period you have" ask — all publish, each constraint narrated in the released
text with its alternatives. The released disclosure sentence for a defaulted window was
observed verbatim beside the figure it qualifies.

The repair round costs one extra Harness invocation only on turns that would previously have
died, inside the existing Lambda budget, and the heartbeat covers its idle gap. The finalizer's
subset verdict remains advisory; the API's custody-bound verdict is the only one that releases.

A campaign breakdown is refused rather than answered until the label projection ships; the
refusal names every displayable breakdown, so the model reroutes within the same turn.

Superseded-refusal semantics weaken the guarantee that every dead end is disclosed: an
exploration the model recovered from may go unmentioned. The trajectory still records every
step for the audit, which is where an investigator looks; the released answer discloses what
shaped it.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Raise the Harness iteration budget | The model stopped because it was told "accepted", not because it ran out of turns; more budget repeats the same stop. |
| Give the Gateway finalizer the custody-bound obligations | ADR-042: no trusted per-turn channel reaches a Gateway target; model-supplied custody would be authority from the party being checked. |
| Publish campaign rows with their canonical keys | A protected internal identifier in public text, and the exact leak the public-text validator exists to refuse. |
| Let the model narrate its own window choice without a mandatory rendering | Prompt-only transparency is unenforceable; the verifier cannot prove a sentence the server did not author (ADR-061). |
| Keep coverage failures dependency-class | "No data for that window" pages an operator for a data state the refusal already explains to the user. |

## Review trigger

- The `public_labels` projection is wired into measurement row shaping, which removes the
  undisplayable-grain gate and reopens campaign breakdowns.
- Repair rounds appear in more than a small fraction of turns, which means the model is
  systematically failing obligations and the prompt or finalizer feedback needs work instead.
- A second grain joins the undisplayable set, which argues for declaring displayability in the
  metric dictionary rather than in the adapter.
