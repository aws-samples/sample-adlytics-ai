# Mutation review — domain/identity/tokens.py

Run: `make mutate MODULE=src/domain/identity/tokens.py TESTS=tests/unit/test_tokens.py`
Tool and rationale: ADR-013. Reviewed 2026-08-18.

| | before | after |
|---|---|---|
| mutants | 98 | 98 |
| survivors | 24 (24.5%) | 21 (21.4%) |

Three mutants killed. Two of the four tests added killed nothing, which is
recorded below rather than quietly dropped — a test that closes no mutant is not
worthless, but claiming otherwise would make the next review distrust this one.

## Killed

**`key_for` matched by ordering, not by `kid`.** `ReplaceComparisonOperator_Eq_LtE`
turned `k.get("kid") == kid` into `<=`, and every test passed. With a single-key
JWKS, a lexicographic comparison still returns the only key, so the suite could
not distinguish exact matching from "returns something". This is the invariant the
class docstring leads with — a pool signs access and ID tokens with different key
pairs, so selection is always by `kid` — and it was documentation only.

The fix is a decoy key whose `kid` sorts below the real one. Cheap, and it is the
difference between verifying a token against the right public key and the wrong
one.

**`expires_in` at exactly 1.** `Lt_LtE` and `NumberReplacer` both survived on
`if self.expires_in < 1`. Nothing sat on the boundary, so shifting the comparison
silently rejected a legitimate one-second token. A one-line assertion covers both.

## Survivors, and why they stay

**11 × `ReplaceBinaryOperator_BitOr_*`, one site.** The `JsonWebKey | None` return
annotation on `key_for`. `from __future__ import annotations` makes annotations
lazy strings, so the mutated operator is never evaluated. Equivalent mutants and
unkillable by any test. Note the contrast with `isinstance(raw, int | float)` in
`_check_not_expired`, which *is* evaluated — mutating that one raises `TypeError`
and is already killed.

**6 × `ReplaceTrueWithFalse`.** `frozen=True` and `slots=True` on the three
dataclasses. `slots` is a footgun guard with no behaviour to assert. `frozen` does
have behaviour and is arguably a real gap: `VerifyAccessTokenRequest` validates in
`__post_init__`, so if it were mutable a caller could pass validation and then
rewrite `expected_issuer` before verification. Left open deliberately — see below.

**2 × `ReplaceFalseWithTrue`.** `verify_iss` and `verify_sub` in the decoder
options. Enabling either changes nothing observable, because pyjwt performs those
checks only when given an `issuer` or `subject` to compare against, and this module
passes neither — it checks both itself, afterwards, with stricter rules. The two
options that *do* matter, `verify_exp` and `verify_aud`, were already killed by the
existing tests.

**1 × `ExceptionReplacer`.** A `raise IdentityError` swapped for another type on a
path where the test asserts `pytest.raises(IdentityError)` against a subclass
relationship that still holds. Low value to chase.

## Two tests that killed nothing

**Expiry judged by injected `now`.** Added on the theory that no test proved
`verify_exp: False` was load-bearing. It was already proven; the `verify_exp`
mutant was dead before this. Kept anyway, because it is the only test that holds
when the wall clock is far from `NOW` — every other expiry test uses timestamps
near today's date, so they would still pass if the decoder started reading the
clock. It guards a real property that the mutation score cannot see.

First version of it was unsound: it used `NOW + 5 minutes`, and since `NOW` is
close to the real date the token was valid against both clocks. Rewritten with
timestamps in 2020 so the two clocks disagree regardless of when the suite runs.

**Empty-string claim rejected.** Added expecting `ReplaceOrWithAnd` on
`_require_str` to die. It survived, and the reason is the finding: with the `or`
mutated to `and`, an empty `sub` reaches `Identity(subject="")`, which raises on
its own. So `not value` in `_require_str` is defence in depth behind a guard that
already holds, for every claim — the other three are compared to an expected value
immediately after, which an empty string also fails. An equivalent mutant, not a
gap. Kept as a regression guard on the composite behaviour.

## Follow-up: frozen=True, now closed

Was recorded here as deferred. Done in a later change, alongside the same gap in
cookies.py and login_flow.py.

| | before | after gap tests | after frozen tests |
|---|---|---|---|
| survivors | 24 (24.5%) | 21 (21.4%) | 18 (18.4%) |

One assertion per frozen dataclass, killing all three `ReplaceTrueWithFalse`
mutants on `frozen=True`. The exception was checked rather than assumed:
`dataclasses.FrozenInstanceError`, a subclass of `AttributeError`. Pyright rejects
the assignment statically — which is the invariant restated — so each is
suppressed for that one rule on that one line, leaving the runtime behaviour
asserted.

`VerifyAccessTokenRequest` is the one where mutability was exploitable, as
predicted above: `__post_init__` is the only place `expected_issuer` is required to
be non-empty, so a mutable request could pass validation and then be pointed at a
different issuer before `verify_access_token` read it. The other two pin
`access_token` against being emptied after its check and the key set against being
swapped on an instance already in flight.

The 18 remaining are the 11 lazy-annotation `BitOr` mutants, 3 × `slots=True`,
2 × `ReplaceFalseWithTrue` on the unused decoder options, 1 × `ReplaceOrWithAnd`
and 1 × `ExceptionReplacer`. Every one is argued above; none is a coverage gap.
