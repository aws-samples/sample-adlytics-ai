# Mutation review — domain/identity/cookies.py

Run: `make mutate MODULE=src/domain/identity/cookies.py TESTS=tests/unit/test_cookies.py`
Tool and rationale: ADR-013. Reviewed 2026-08-18.

| | before | after |
|---|---|---|
| mutants | 74 | 74 |
| survivors | 18 (24.3%) | 11 (14.9%) |

A later change took it to 8 — see the follow-up at the end.

Seven killed by four tests, and every one of the four killed something — unusual
enough to be worth stating, since the tokens.py review had two that killed
nothing. Of the eleven left, six are the `frozen`/`slots` pair on three
dataclasses and five are enum comparisons, all reasoned about below.

The single most useful finding is that `CookieScope` subclasses `StrEnum`. That
makes every member a `str`, so a comparison operator mutated from `is` to `<=`
does not raise — it silently compares the member *values*. Two of the six enum
mutants on those two branches survive only because `"host_only"` sorts below
`"parent_domain"`; the sixth changed behaviour and was the one real gap here.

## Killed

**A host-only policy was emitting `Domain=`.** `Is_LtE` on
`policy.scope is CookieScope.PARENT_DOMAIN` in `_set_cookie` (line 260).
Mutated to `scope <= CookieScope.PARENT_DOMAIN`, both members compare True, so a
host-only policy takes the parent-domain branch and appends `Domain=` with the
empty domain behind it. `_set_cookie`'s own docstring says this must never happen
— "an empty Domain attribute is not the same thing and browsers vary on it" — and
nothing tested it. The module-level `HOST_ONLY` policy constant existed in the
test file and was never used to render a header; every attribute assertion ran
under `PARENT_DOMAIN`.

This is the same failure class the `PUBLIC_SUFFIXES` guard exists to prevent: a
`Set-Cookie` the browser mishandles, a login that appears to succeed, and every
request after it denied. Closed by asserting `Domain` is absent from all three
session headers under the host-only policy.

**`refresh_max_age` at exactly 1.** `Lt_LtE` and `NumberReplacer` both survived on
`if self.refresh_max_age < 1`. Nothing sat on the boundary, so shifting the
comparison or the constant silently rejected a legitimate one-second lifetime.
The same gap tokens.py had on `expires_in`, in the same shape. One assertion on
the rendered `Max-Age=1;` covers both and pins the unit at the same time.

**HttpOnly on the two clear paths.** Four mutants: `build_cleared_cookies` lines
204-206 and `clear_transaction_cookie` line 233. These paths hard-code their own
HttpOnly table, independent of the one in `build_session_cookies`, and nothing
read it back.

Worth being precise about the consequence, because it is smaller than it looks. A
browser matches a deletion on name, Domain and Path — not on HttpOnly — so
flipping the flag on a `Max-Age=0` cookie changes nothing a browser does. These
are genuinely killable mutants over untested code, not equivalent ones, but the
tests pin consistency between two duplicated tables rather than close a live
weakness. Recorded that way in the test comment too.

## Survivors, and why they stay

**2 × `Is_Eq`, lines 92 and 260.** `scope is CookieScope.HOST_ONLY` becomes
`scope == ...`. For enum members these agree: members are singletons, and two
distinct members are never equal. The only input that separates them is a raw
`str` such as `"host_only"` in place of a member — with `is` it falls through to
the parent-domain branch, with `==` it does not. That input violates the
annotation, pyright rejects it, and no caller produces it. Killing it would mean
writing a test that pins accidental behaviour of a type-invalid argument, so it
stays open deliberately.

**`Is_LtE` line 92 and `Is_GtE` line 260.** Both execute, because `StrEnum` makes
the operands strings, and both happen to partition the two members exactly as `is`
does: `"host_only" <= "host_only"` and `"parent_domain" > "host_only"` at line 92;
`"host_only" < "parent_domain"` at line 260. Equivalent today, but by alphabetical
accident rather than by design. A third member whose value sorted differently —
`"cross_site"`, say — would make both killable and would mean the source was
wrong. Verified against the actual member values rather than assumed.

Note the contrast with `Is_GtE` at line 92 and `Is_LtE` at line 260, the same
operator on the same two lines the other way round. Those two *were* already dead:
they flip the branch for one member, which several existing tests notice. The
asymmetry is entirely down to string ordering.

**`Eq_Is` line 118.** `self.domain == suffix or self.domain.endswith(suffix)` with
the first comparison mutated to `is`. Unkillable, because a string ends with
itself: `".on.aws".endswith(".on.aws")` is True, so the second disjunct already
covers every case the first one did. The `==` half is defence in depth behind a
guard that always holds — the same shape as the `_require_str` finding in the
tokens.py review. Left in source untouched; this review does not change `src/`.

**6 × `ReplaceTrueWithFalse`.** `frozen=True` and `slots=True` on `CookiePolicy`,
`SessionCookieRequest` and `_CookieToSet`. `slots` has no behaviour to assert.
`frozen` does, and both `CookiePolicy` and `SessionCookieRequest` validate in
`__post_init__`, so a mutable instance would let a caller pass validation and then
rewrite `domain` before the header is rendered. Closed in the same change as the
tokens.py `frozen` gap — see the update below.

## Follow-up: frozen=True, now closed

Done in the same change as the tokens.py and login_flow.py frozen gaps.

| | before | after gap tests | after frozen tests |
|---|---|---|---|
| survivors | 18 (24.3%) | 11 (14.9%) | 8 (10.8%) |

Three mutants killed, one per dataclass. `CookiePolicy` is the one that matters:
`__post_init__` is the only place the domain is checked against the public suffix
list, the leading dot and localhost, so a mutable policy could clear every one of
those checks and then have its domain replaced before `_set_cookie` rendered it.

`_CookieToSet` cost something to close. It is private, and pyright strict is right
to flag a test importing it, so that import carries a `reportPrivateUsage`
suppression. The invariant is worth little — nothing validates the type and no
caller outside the module constructs one — so this is the one assertion here a
reviewer could reasonably delete. Recorded rather than buried.

The eight remaining are the three `slots=True` mutants and the five enum
comparisons. Every one is argued above; none is a coverage gap.
