# Mutation review — domain/identity/login_flow.py

Run: `make mutate MODULE=src/domain/identity/login_flow.py TESTS=tests/unit/test_login_flow.py`
Tool and rationale: ADR-013. Reviewed 2026-08-18.

| | before | after |
|---|---|---|
| mutants | 30 | 30 |
| survivors | 8 (26.7%) | 4 (13.3%) |

Four killed by three tests. The smallest module of the three reviewed and the one
with the highest starting survivor rate, which turned out to be a size artefact:
30 mutants is few enough that two constants and one dataclass decorator account
for most of them.

The useful finding is the one the numbers do not show. `STATE_BYTES` had no
assertion of any kind against it — not a floor, not a length, nothing. The state
is the entire defence against a forged callback and its only property is being
unguessable, so that was the one real hole rather than the comparison operators
that looked more alarming in the report.

## Killed

**Only a verifier of exactly 43 characters was ever tried.** `Lt_NotEq` and
`Lt_IsNot` both survived on `len(self.code_verifier) < MIN_VERIFIER_LENGTH`.
Every test used either `"a" * 43` or `new_login_transaction()`, which produces
exactly 43, or `"a" * 42`, which is below the floor — so a check mutated to `!=`
partitioned the whole suite identically to `<`.

RFC 7636 permits 43 to 128 characters and the check is deliberately a floor. Under
the mutant every legal longer verifier is rejected, so raising `VERIFIER_BYTES` —
a plausible hardening change — would stop every login from starting and no test
would explain why. Closed with a 64-character verifier.

`Lt_IsNot` is worth a note: `len(...) is not 43` looks like it should be a type
error, but CPython caches small ints, so for any length under 257 the identity
comparison agrees with equality. It behaves exactly like the `!=` mutant, which is
why one test killed both.

**Nothing pinned the state's length.** `NumberReplacer` on `STATE_BYTES = 16`
survived in both directions. `test_each_login_gets_fresh_values` passes at any
size, because two random values of any length differ.

16 bytes is 128 bits and encodes to 22 unpadded base64url characters; 15 bytes is
120 bits and 20 characters. 120 bits is still not brute-forceable, so this specific
mutant is not a live weakness — the finding is the absence of any floor at all,
which would let the constant be reduced arbitrarily without a failing test. The
assertion derives its threshold from the property rather than hard-coding the
observed length: unpadded base64url packs 6 bits per character, so 128 bits needs
22. That killed the shrink and correctly left the growth alive.

**`frozen=True` on `LoginTransaction`.** `__post_init__` is the only place the
state, the verifier length and the separator are checked. A mutable instance would
let a caller pass validation and then rewrite `state`, which is precisely the field
`state_matches` compares a returned value against. Same gap as
`VerifyAccessTokenRequest` in the tokens.py review, closed here in the same change.

The exception was checked rather than assumed: a frozen dataclass raises
`dataclasses.FrozenInstanceError`, a subclass of `AttributeError`. Pyright rejects
the assignment statically, which is the invariant restated — suppressed for that
one rule on that one line so the runtime behaviour still gets asserted.

## Survivors, and why they stay

**`ReplaceOrWithAnd` in `state_matches`.** `if not returned or not transaction.state:`
mutated to `and`. Genuinely equivalent, and the mechanism is `hmac.compare_digest`
rather than the guard: it returns False for any mismatch, including against an
empty string. Enumerating all four quadrants of empty and non-empty gives the same
answer from both versions, so no input distinguishes them.

Two of those quadrants are unreachable anyway — `__post_init__` rejects an empty
state, so `not transaction.state` is never True for a validly constructed
transaction. The guard is defence in depth behind a check that already holds, the
same shape as the `_require_str` finding in the tokens.py review. Unkillable; left
alone.

**`NumberReplacer` on `VERIFIER_BYTES = 32`, upward only.** 33 bytes yields 44
characters, still inside the RFC's 43-to-128 range and carrying more entropy, not
less. The dangerous direction is already dead: 31 bytes yields 42 characters,
below the floor, and `new_login_transaction()` raises. Killing the surviving
direction means asserting the length is exactly 43, which pins a sizing choice
rather than a property and would fail on a change that is not a regression. Left
open deliberately.

**`NumberReplacer` on `STATE_BYTES = 16`, upward only.** Same argument. 17 bytes
is more entropy. The floor is now asserted; a ceiling would be inventing a
requirement.

**`ReplaceTrueWithFalse` on `slots=True`.** A footgun guard with no behaviour to
assert. Unkillable without testing that a typo'd attribute assignment fails, which
is testing the language.
