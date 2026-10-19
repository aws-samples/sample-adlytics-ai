# Mutation review — src/adapters/inbound/conversation/withholding.py
> **Module retired 2026-09-04.** `src/adapters/inbound/conversation/withholding.py` no longer exists; this review is retained as history.

- Commit: f1c1dddbda9a824547088f0aeae4bd404deff734
- Date: 2026-08-24
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/conversation/withholding.py TESTS=tests/unit/test_route_guardrails.py`
- Mutants: 29 generated, 25 killed, 4 survived
- Survivors: 0 open, 2 killed since, 2 equivalent

Layer 4 of ADR-041, as amended by ADR-042: the generator that holds the block carrying an answer until
it has been assessed. The first run left 8 survivors from 32 mutants and **two were genuine defects
rather than missing tests** — which is the outcome this review exists for, because both were invisible
to a reading of the code and to every test that passed.

## S-01 · KILLED · A refusal did not end the stream

- Track: mechanical
- Location: `src/adapters/inbound/conversation/withholding.py:78`, `:81`, `:86`
- Mutants: 3 surviving at these locations
- Mutation: `continue` becomes `break`
- Survives because: nothing asserted what happens after a terminal event, so continuing and stopping
  were indistinguishable.
- Verdict: KILLED by `tests/unit/test_route_guardrails.py::TestAnUpstreamRefusal::test_nothing_follows_a_refusal`
  and `::test_nothing_follows_a_completed_turn`

**The mutant was pointing at a real defect, not at a missing assertion.** `ports.outbound.conversation`
documents `DONE` and `REFUSED` as the last event of a turn, and `AgentCoreHarness._translate` does not
enforce it: it yields a `REFUSED` for an error *frame* and then keeps iterating the stream. So a
refusal followed by more text is reachable, and with `continue` that text would accumulate in the
buffer and be released after the refusal — an answer arriving after the platform had said it stopped.

Resolved by making both terminal branches `return`, which makes the port's stated contract true at the
point it is relied upon, plus the two tests above. Recorded here rather than as an equivalence because
the source changed: this is the one class of survivor where editing the source is the correct
resolution, since the mutation revealed behaviour that was wrong rather than untested.

## S-02 · KILLED · Metadata discarded held text instead of releasing it

- Track: mechanical
- Location: `src/adapters/inbound/conversation/withholding.py:83`
- Mutants: 1 surviving at this location
- Mutation: `event.kind is TurnEventKind.REFUSED` becomes `event.kind <= TurnEventKind.REFUSED`
- Survives because: every test placed `METADATA` before any text, so the held buffer was empty and
  clearing it was indistinguishable from releasing it.
- Verdict: KILLED by `tests/unit/test_route_guardrails.py::TestTheHeldTail::test_releases_held_text_when_metadata_arrives`

`TurnEventKind` is a `StrEnum`, so `<=` compares the underlying strings. `"metadata" <= "refused"` is
true, which sent a `METADATA` event down the refusal branch — and that branch *discards* the buffer
rather than releasing it. A turn whose model narrated before a tool result would have lost that
narration silently.

The gap was in the test data rather than in the assertions: metadata was always first. Killed by a
case where text precedes it.

## S-03 · EQUIVALENT · Identity against equality on an enum member

- Track: mechanical
- Location: `src/adapters/inbound/conversation/withholding.py:61`, `:65`, `:83`
- Mutants: 3 surviving across these locations
- Mutation: `event.kind is TurnEventKind.X` becomes `event.kind == TurnEventKind.X`
- Survives because: enum members are singletons, so identity and equality agree for every value of
  `TurnEventKind`.
- Verdict: EQUIVALENT

The known class named in the mutation steering. `is` is kept because it is the stronger statement and
because `==` on a `StrEnum` would also match a bare string, which is a comparison this module should
never make.

## S-04 · EQUIVALENT · An ordering comparison that agrees by alphabet

- Track: mechanical
- Location: `src/adapters/inbound/conversation/withholding.py:65`
- Mutants: 1 surviving at this location
- Mutation: `event.kind is TurnEventKind.DONE` becomes `event.kind <= TurnEventKind.DONE`
- Survives because: `"done"` sorts first among the five `TurnEventKind` values — `done`, `metadata`,
  `refused`, `text`, `tool_call` — so `x <= "done"` is true only when `x` is `"done"`.
- Verdict: EQUIVALENT

**Equivalent for an accidental reason, which is worth stating rather than glossing.** The same mutation
at line 83 was a genuine defect (S-02) purely because `"refused"` does not sort first. If a sixth kind
were added whose name sorted before `done`, this would stop being equivalent — so the equivalence
belongs to the current value set rather than to the code.

## Semantic pass

Four mutations no operator expresses were considered against this module. None survived.

- **Release the held text *and* the refusal on an intervention.** Killed by
  `TestAnIntervention::test_never_releases_the_refused_text` — this is the whole point of the layer,
  and a masked or partially released answer is a corrupted answer presented as a good one.
- **Join held fragments with a space.** Killed by `TestTheHeldTail::test_releases_the_tail_once_it_passes`,
  which asserts the exact reassembled string. The fragments are `contentBlockDelta` deltas of one
  string, so any separator inserts characters the model did not write.
- **Assess before appending rather than at `DONE`.** Would assess each fragment separately, costing one
  call per delta and assessing text no reader ever sees as a unit. Killed by
  `TestTheHeldTail::test_releases_prose_before_a_tool_call_unassessed`, which counts the assessments.
- **Return a passing assessment when the guardrail call raises.** Killed by
  `TestAFailedAssessment::test_propagates_rather_than_releasing_unassessed_text`. This is the mutation
  that matters most: it would make an outage in Bedrock silently remove the only content control on
  the answer path, with every turn reporting healthy.
