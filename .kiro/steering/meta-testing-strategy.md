---
inclusion: fileMatch
fileMatchPattern: ["tests/**"]
description: Governs test-tier placement, real-AWS integration boundaries, synthetic fixture evidence, traceability, assertions, and coverage interpretation when editing tests.
---

# Testing strategy

Markers are declared in pyproject.toml. Every test carries exactly one.

## The tiers

The tier markers and what each means are declared in pyproject.toml, which is also
where a new one is added. Never restate that list. What the declarations cannot
say:

- A pure adapter module with no I/O, such as configuration validation, belongs in
  the unit tier rather than the integration one.
- When a unit test appears to need an AWS mock, either extract the logic from the
  adapter or move the test to the integration tier. Never add the mock.
- Never let an integration test gate a unit-test loop.

## AWS boundaries

Where the seam sits, the AWS guidance behind it and the rejected options:
docs/decisions/adr-014-aws-testing-seam.md.

- Never introduce an AWS emulator or an in-process AWS fake in any tier. Not
  LocalStack, not moto. A test that passes against a fake has established that
  the fake works.
- An adapter covered only by unit tests is not covered. Every SDK call needs an
  integration test against a real resource before that adapter is finished.
- Skip integration tests when credentials are absent, never fail them. The unit
  loop runs with no AWS profile and no network, and make verify must stay that way.
- A unit-tier fake never accepts a request the real service rejects. Every fake
  of a provider applies that provider's request validation.
- Synthesise hostile inputs the real service will never issue — an expired token,
  a wrong audience, an unknown signing key, a substituted algorithm — using real
  cryptography and a keypair generated inside the test. Never reach them by
  stubbing the verifier, which removes the code under test.

## Fixtures

- Never describe a generated fixture as source-derived or as evidence of source
  volume. It proves only the declared structure, types, scaling, and deterministic
  reader behavior because it is generated from this repository's own contracts.
- A source-invariant synthetic fixture uses fictional values to represent one exact
  invariant established by a cited source document or narrow recorded probe. Never
  generalize an observed value, relationship, lexical form, distribution, or query
  result beyond the exact invariant that evidence settled. Never copy a live
  identifier, name, ASIN, account value, or measurement into a committed fixture.
- Hand-build the traps. These are known and specific, so each is a few rows and
  each is worth more than a hundred generated ones (the exact null and sign
  semantics behind them are owned by docs/sources/vendor-central.md). The trap
  roster and what each trap exercises are owned by tests/fixtures/traps/README.md;
  add a trap there, never here.
- Never commit a fixture containing real customer data, account IDs or ASINs
  tied to a real advertiser.
- Every identifier a test invents in a shared store carries the prefix in
  tests/integration/probe_identity.py, and the tier sweeps by that prefix before
  and after the session. Never write a test record under a real contract id: both
  load-state stores refuse a write that moves a key backwards, so such a write
  cannot be undone through the port and corrupts what the platform reports about
  real data.

## Identity and traceability

- Never invent a test ID. The pytest node ID is the identifier, it is derived
  from the code, and it cannot drift. A hand-assigned ID is a second identifier
  for the same thing and will eventually disagree with it.
- Tag a test with the decisions it verifies using the covers marker, whose
  arguments are IDs that already exist in docs/: ADR-NNN, G-NN, F-NN.
  For example: pytest.mark.covers("ADR-010", "G-14").
- Never pass an ID to covers that is not declared in docs/. make
  check-coverage-matrix fails on an unknown ID and reports which decisions have
  no test at all.
- Never tag a test with a decision it does not actually exercise. An inflated
  matrix is worse than an empty one, because it reports confidence that is not
  there.

## What justifies a test

- Before adding a test to a module a mutation report already covers, name the
  surviving mutant it kills. A test that kills none adds no signal; do not write it.
  A regression test that reproduces a fixed defect is exempt.
- When a property holds for every member of a set, assert it once over the set.
  tests/unit/test_domain_invariants.py is the worked example.
- Parametrise over the cases the code distinguishes. Never parametrise over a
  seed, index or identifier the code does not branch on.
- Never put randomised inputs or orderings in the unit tier. Where they run, their
  parametrisation states the size of the space sampled.
- Never write a test for code the same change deletes. Prove the deletion instead.

## Docstrings

- The test name states what is asserted. A docstring that restates the name is
  duplication that rots — write the name as a sentence instead.
- Use a docstring only for why: the consequence being prevented, or the reason a
  boundary case matters. Skip it when the name is sufficient.
- Every test module has a docstring naming the area it covers and the
  consequence a failure there prevents, in the vocabulary of
  meta-ai-collaboration's Scope rules. Ruff enforces presence; no gate checks
  the consequence, so review does. Individual test functions are exempt.
- Never write a docstring template with Args and Returns sections in a test. The
  signature carries that.

## Assertions

- Never assert on a bare number. Assert its units and its grain too, because a
  correct figure at the wrong grain is the failure mode this product must not have.
- Assert that a suppressed or truncated result is labelled as such, not merely
  that it returned.
- For anything touching money, assert the scale factor was applied — a wrong
  divisor is a silent order-of-magnitude error.
- Never compute an expected value with the code under test, or derive a boundary
  input from the constant being checked. Pin the literal.
- Never assert only that a collaborator was called or how many times. Assert the
  effect.
- Never assert what a linter, formatter or make check gate already owns.
- A refusal test asserts the exact refusal signal and the layer that refused it,
  with a positive control in the same module, wherever a silent pass would breach
  security, lose a write or publish a wrong figure.

## Coverage

Coverage is a signal, never a gate. A domain module with untested branches is a
finding; a percentage target is an invitation to test getters.

## Cost

- A per-change run reads each fixture or contract set at most once, through a
  session-scoped fixture.
- A unit test slow enough to stretch the per-change loop either moves to the
  integration tier or loses its I/O.
- Gate evidence is a make target's run (make verify, make test). Never cite a
  bare pytest invocation as gate evidence.
- Never let a test share mutable state with another test. Never fix an
  order-dependent failure by retrying it.
