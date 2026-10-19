# Mutation review — src/adapters/outbound/bedrock/guardrail.py

- Commit: f1c1dddbda9a824547088f0aeae4bd404deff734
- Date: 2026-08-24
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/outbound/bedrock/guardrail.py TESTS=tests/unit/test_route_guardrails.py`
- Mutants: 40 generated, 23 killed, 17 survived
- Survivors: 4 open, 3 killed since, 13 equivalent

`ApplyGuardrail` as a `GuardrailPort` — layers 1 and 4 of ADR-041, as amended by ADR-042. The first run
left 21 survivors; three were genuine and are now killed, and **one of the three was a latent hazard
that would have disabled the control while every turn reported healthy.**

The four open survivors are all one cause: the `assess()` path makes an SDK call, so it belongs to the
integration tier. `meta-testing-strategy` states that an adapter covered only by unit tests is not
covered, and forbids adding an AWS mock to close the gap — a test that passed against a mock would have
established that the mock works. They are recorded as open rather than equivalent because that is what
they are: real behaviour with no test behind it yet.

## S-01 · KILLED · The verdict was comparable by identity

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:133`
- Mutants: 1 surviving at this location
- Mutation: `response.get("action") != _INTERVENED` becomes `response.get("action") is not _INTERVENED`
- Survives because: the test passed a string *literal*, which CPython interns to the same object as the
  module constant — so identity and equality agreed.
- Verdict: KILLED by `tests/unit/test_route_guardrails.py::TestTheAdapter::test_recognises_an_action_string_it_did_not_intern`

**The most serious finding in either module.** A string parsed out of a JSON response is not interned,
so `is not` would be true for *every* response — including an intervening one. The control would be
silently off: no answer would ever be withheld, no error would be raised, and every turn would look
healthy. The test now builds the value at runtime with `"".join([...])` so it cannot be interned, and
asserts that it is a different object before asserting the verdict.

## S-02 · KILLED · Ordering stood in for equality

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:133`
- Mutants: 1 surviving at this location
- Mutation: `!=` becomes `>`
- Survives because: every action value asserted — `NONE`, `SOMETHING_NEW`, `GUARDRAIL_INTERVENED` —
  happens to sort at or after `GUARDRAIL_INTERVENED`, so `>` and `!=` agreed on all of them.
- Verdict: KILLED by `tests/unit/test_route_guardrails.py::TestTheAdapter::test_an_unknown_action_sorting_before_the_verdict_is_not_an_intervention`

A future action sorting *before* the verdict would have been treated as an intervention and would have
withheld a clean answer. `ANONYMIZED` is the plausible candidate, given the guardrail API already uses
that word for a PII action, and it is what the new test asserts.

## S-03 · KILLED · The error codes were not pinned

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:29`
- Mutants: 2 surviving at this location
- Mutation: `ErrorCode(area="GUARDRAIL", number=1)` becomes `number=2` and `number=0`
- Survives because: tests asserted the error *class* and its alarm behaviour, never its rendered code.
- Verdict: KILLED by `tests/unit/test_route_guardrails.py::TestTheErrorCodes::test_are_the_strings_operators_have_filtered_on`

A code reaches a log line and an EMF dimension, so a silently changed one stops matching the alarm and
the query built on it. `errors.py` states the rule — never change what an existing code means — and
nothing held it for this area. The `number=2` mutant is the sharper one: it collided with
`GUARDRAIL_DECLINED`, which would have merged a dependency failure and a product signal in every
dashboard. A second test now asserts the two are distinct.

## S-04 · OPEN · The exception clause is untested

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:104`
- Mutants: 2 surviving at this location
- Mutation: `except (ClientError, BotoCoreError)` becomes a clause catching a different exception
- Survives because: no test in this tier makes the SDK call, so the handler never runs.
- Verdict: OPEN

Belongs to the integration tier. The mapping it guards — an SDK failure becoming a `DependencyError`
that alarms and carries no infrastructure identifier into a user-facing string — is asserted for a
hand-constructed error by `TestAFailedAssessment`, but that the *real* failure is caught and mapped is
not. Closing this needs `tests/integration/` calling `apply_guardrail` against the deployed guardrail
with a deliberately wrong identifier. Not closed with a mock, per `meta-testing-strategy`.

## S-05 · OPEN · `@staticmethod` removal is only reachable through `assess()`

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:124`
- Mutants: 1 surviving at this location
- Mutation: the `@staticmethod` decorator is removed
- Survives because: the tests call `BedrockGuardrail._assessment(...)` through the class, where a plain
  function and a static method behave identically. The one call that would break is
  `self._assessment(response, request.direction)` inside `assess()`, which would pass three arguments
  to a two-parameter function — and `assess()` needs AWS.
- Verdict: OPEN

Same cause and same resolution as S-04.

## S-06 · OPEN · Inverting the `TYPE_CHECKING` guard

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:22`
- Mutants: 1 surviving at this location
- Mutation: `if TYPE_CHECKING:` becomes `if not TYPE_CHECKING:`
- Survives because: the stubs packages are installed in the development environment, so importing them
  at runtime succeeds and changes nothing observable.
- Verdict: OPEN

Not reproducible in a tier that has the dev dependencies installed, and it is genuinely detected — by
the deployed readiness probe rather than by a test. `scripts/build-lambdas.sh` ships only what the lock
file resolved for the runtime, so the mutated import would fail at function init, which
`validation/probe_conversation_route.py` check 1 reports as an unhealthy container on the first run
after a deploy. Recorded as open rather than equivalent because the behaviour differs; what is missing
is a test in this repository that reaches it.

## S-07 · EQUIVALENT · Arithmetic on a type annotation

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:79`
- Mutants: 11 surviving at this location
- Mutation: the `|` in `BedrockRuntimeClient | None` becomes `+`, `-`, `*`, `/`, `//`, `%`, `**`, `>>`,
  `<<`, `&`, `^`
- Survives because: `from __future__ import annotations` makes every annotation a string that is never
  evaluated at runtime.
- Verdict: EQUIVALENT

One of the two classes the mutation steering names. Eleven operator substitutions at one location are
one finding; listing them separately would bury the ones that differ.

## S-08 · EQUIVALENT · Identity against equality on an enum member

- Track: mechanical
- Location: `src/adapters/outbound/bedrock/guardrail.py:156`
- Mutants: 2 surviving at this location
- Mutation: `direction is Direction.INBOUND` becomes `== Direction.INBOUND` and `<= Direction.INBOUND`
- Survives because: enum members are singletons, so `==` agrees with `is`; and `Direction` has two
  values whose strings sort `inbound` before `outbound`, so `x <= "inbound"` is true only for
  `inbound`.
- Verdict: EQUIVALENT

The `<=` half is equivalent by alphabet rather than by construction, the same accident recorded as S-04
in the `withholding` review. A third direction whose name sorted before `inbound` would break it, so
the equivalence belongs to the current value set rather than to the code.

## Semantic pass

Five mutations no operator expresses were considered. Three are killed; two are the same integration
gap as S-04.

- **Swap `INPUT` and `OUTPUT` in the source map.** Killed by
  `TestTheAdapter::test_the_two_directions_read_differently_to_a_user`, which asserts the two
  directions produce different reader-facing reasons. A swapped map would assess a question under the
  rules for an answer.
- **Return the matched text as the reason.** Killed by
  `TestTheAdapter::test_reads_the_verdict_and_nothing_else`, which feeds a response containing
  `a-secret@example.com` and asserts it does not appear in the assessment. This is the observability
  rule about a guardrail match value, enforced by not reading the field at all.
- **Give both directions the same reason.** Killed by the same test as the first item. A declined
  question can be rephrased and a withheld answer cannot, so one wording would tell half of readers to
  fix something they did not do.
- **Pass `outputScope: FULL`.** Not expressible as a test in this tier — it changes what the service
  returns, not what this code does with it. Deliberately unset so the response never carries a copy of
  the narration back; belongs with S-04 in the integration tier.
- **Send the guardrail identifier without the version.** Refused at construction by
  `TestTheAdapter::test_refuses_construction_without_a_version`, so it cannot reach the call.
