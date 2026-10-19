# Mutation review — src/domain/identity/authorization.py

- Commit: PENDING — needs `git rev-parse HEAD`, which requires approval
- Date: 2026-08-18
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/domain/identity/authorization.py TESTS=tests/unit/test_authorization.py`
- Mutants: 24 generated, 23 killed, 1 survived
- Survivors: 0 open, 3 killed since, 1 equivalent

First review of the authorization gate. The initial run left 4 survivors, of which 3 were genuine and one was a bypass. Tests for this module went from 33 to 36.

## S-01 · KILLED · The origin check accepted anything sorting before our origin

- Track: mechanical
- Location: src/domain/identity/authorization.py:124
- Mutants: 1 at this location
- Mutation: `request.origin == request.allowed_origin` becomes `request.origin <= request.allowed_origin`
- Survived because: the only foreign origin under test was `https://attacker.example`, which sorts *after* `https://app.customer.com`, so an ordering comparison rejected it exactly as equality would. Any origin sorting before ours — `https://aaa.example` — would have been accepted, and the suite would not have noticed.
- Verdict: KILLED by `tests/unit/test_authorization.py::TestOriginComparisonIsExact::test_an_origin_sorting_before_ours_is_still_rejected`

This is the most valuable finding of either review so far. It is a CSRF-defence bypass that no test failure would have revealed, and it survived because the fixture value happened to fall on the safe side of an ordering the test was never written to consider.

## S-02 · KILLED · The origin check would have compared by identity

- Track: mechanical
- Location: src/domain/identity/authorization.py:124
- Mutants: 1 at this location
- Mutation: `==` becomes `is`
- Survived because: the test supplied the same string literal for both sides, and equal literals in one module are the same object. A header value arriving over the wire never is, so this mutation would have denied every mutating request in production while every test passed — the inverse failure of S-01, and equally invisible.
- Verdict: KILLED by `tests/unit/test_authorization.py::TestOriginComparisonIsExact::test_an_equal_but_distinct_string_is_accepted`

The new test builds the origin with `"".join(list(...))` and asserts it is not the same object before using it, so the test cannot silently regress to comparing a literal with itself.

## S-03 · KILLED · AuthorizationRequest immutability was unasserted

- Track: mechanical
- Location: src/domain/identity/authorization.py:51
- Mutants: 1 at this location
- Mutation: `frozen=True` becomes `frozen=False`
- Survived because: no test attempted to mutate a request. A mutable request would let a caller change the method or the origin after `__post_init__` had validated them, which is a validated object that no longer describes the decision being made.
- Verdict: KILLED by `tests/unit/test_authorization.py::TestAuthorizationRequestIsFrozen::test_the_request_cannot_be_mutated`

## S-04 · EQUIVALENT · `slots=True` flip

- Track: mechanical
- Location: src/domain/identity/authorization.py:51
- Mutants: 1 surviving at this location
- Mutation: `slots=True` becomes `slots=False`
- Survives because: `slots` changes attribute storage, not behaviour. Nothing here depends on the absence of `__dict__`.
- Verdict: EQUIVALENT — known class: dataclass slots argument is inert

## Not yet reviewed

`tokens.py`, `cookies.py` and `login_flow.py` carry comparable risk and have not been through a mechanical run. `tokens.py` is the highest priority of the three: it holds the signature and claim checks, and S-01 above shows that a comparison operator in this area can be wrong in a way that reads as working.
