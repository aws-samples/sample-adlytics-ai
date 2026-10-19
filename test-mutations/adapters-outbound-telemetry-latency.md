# Mutation review — src/adapters/outbound/telemetry/latency.py
> **Module retired 2026-09-14.** `src/adapters/outbound/telemetry/latency.py` no longer exists; this review is retained as history.

- Commit: 142d191fc579c8f8e3616f0cffac3fdd3df9b2c1
- Date: 2026-08-23
- Tool: cosmic-ray 8.7.0
- Command: `uv run cosmic-ray init /tmp/cr-latency.toml session.sqlite && uv run cosmic-ray exec /tmp/cr-latency.toml session.sqlite` with `module-path = "src/adapters/outbound/telemetry/latency.py"`, `test-command = "python -m pytest tests/unit/test_latency.py -q -x"`, `timeout = 20.0`
- Mutants: 123 generated, 91 killed, 32 survived
- Survivors: 4 open, 0 killed since, 28 equivalent

The first pass on this module surveyed 45 survivors. Seven were real blind spots and are recorded
below with KILLED verdicts and the node IDs that now close them; the remainder resolved to two
equivalence classes plus a third this module introduces. The module is a latency calculator whose
figures are read during incident triage, so a surviving arithmetic mutant here is a wrong number in
the one place nobody would think to distrust.

**One methodological note that cost time and is worth not repeating.** Verifying a survivor by
hand-writing what the mutation "probably" was gave three wrong verdicts: cosmic-ray applies
`max(0, …)` → `max(-1, …)`, and the hand-written approximation `max(1, …)` is killed while the real
mutant survives. Always confirm with `cosmic-ray apply <module> <operator> <occurrence>` and diff.
Separately, when re-running a mutant by hand, clear `src/**/__pycache__` first: several mutations are
byte-length preserving (`max(0,` → `max(1,`, `-` → `+`), and a stale `.pyc` with an unchanged size and
mtime is reused, which reports a killed mutant as a survivor.

## S-01 · KILLED · The start offset is never subtracted, because the test clock starts at zero

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:148
- Mutants: 1 surviving at this location
- Mutation: `_ms(instant - self._started)` becomes `_ms(instant + self._started)`
- Survives because: the test clock began at `0.0`, so `instant - 0.0` and `instant + 0.0` are the
  same number for every assertion in the file. `time.monotonic` returns seconds from an arbitrary
  origin and never from zero, so the production path always subtracts a large offset that no test
  exercised. Every `first_event_ms` and `first_text_ms` figure was unverified against a realistic
  clock.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheStartOffset::test_time_to_first_event_is_also_relative`

## S-02 · KILLED · The same offset defect on the total

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:154
- Mutants: 1 surviving at this location
- Mutation: `total_ms=_ms(ended - self._started)` becomes `total_ms=_ms(ended + self._started)`
- Survives because: the same zero-based clock. Recorded separately from S-01 rather than grouped,
  because they are different locations and a fix could plausibly reach one and not the other.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheStartOffset::test_a_duration_is_relative_to_the_start_not_the_clock_origin`

## S-03 · KILLED · A tie in the longest gap re-attributes the stall

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:126
- Mutants: 1 surviving at this location
- Mutation: `if gap > self._longest_gap:` becomes `if gap >= self._longest_gap:`
- Survives because: no test had two gaps of exactly equal length with different preceding events, and
  that is the only case where the two operators differ. `>=` keeps the *later* event, so the reported
  `longest_gap_after` would silently blame the wrong thing — and that field is the module's entire
  claim to being a diagnosis rather than a measurement. The most valuable finding in this pass.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheContentionFigure::test_the_earliest_of_two_equal_gaps_keeps_the_attribution`

## S-04 · KILLED · The zero floor is never exercised at its boundary

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:171
- Mutants: 1 surviving at this location
- Mutation: `return max(0, round(seconds * 1000))` becomes `return max(-1, round(seconds * 1000))`
- Survives because: no test ever passed `_ms` a negative value, so the clamp existed and was never
  reached. The clamp is there for a clock anomaly, and a negative latency is worse than a wrong one —
  it is invisible among real figures and drags a minimum below zero.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheTotal::test_a_backwards_clock_cannot_produce_a_negative_duration`

## S-05 · KILLED · Private accumulators become constructor arguments

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:100-108
- Mutants: 9 surviving across the nine `field(...)` declarations of one block
- Mutation: `field(default=…, init=False)` becomes `field(default=…, init=True)`
- Survives because: nothing asserted the internal state cannot be seeded. With `init=True` a caller
  can construct a `Timeline` that already claims a duration it never measured. Grouped as one entry
  because the nine lines are one contiguous declaration block with one cause; the first fix asserted
  only four of the nine fields and left five alive, which is why the assertion now enumerates all of
  them rather than sampling.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheShapeOfTheObjects::test_the_private_accumulators_are_not_constructor_arguments`

## S-06 · KILLED · The timings object is not asserted immutable

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:37
- Mutants: 1 surviving at this location
- Mutation: `@dataclass(frozen=True, slots=True)` becomes `@dataclass(frozen=False, slots=True)`
- Survives because: no test attempted to mutate a `TurnTimings`. The same shape recurred in three
  earlier reviews and is recorded in gap register entry G-25; the sweep that closed it for `src/domain`
  does not cover adapters, so this module was outside it.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheShapeOfTheObjects::test_the_timings_object_is_immutable`

## S-07 · KILLED · The event flags are not asserted keyword-only

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:115
- Mutants: 1 surviving at this location
- Mutation: `def observed(self, kind: str, *, text: bool = False, tool: str = "")` becomes the same
  signature with `/` in place of `*`, turning the keyword-only marker into a positional-only one
- Survives because: every call site already passes them by keyword, so loosening the signature broke
  nothing. `text: bool` and `tool: str` are adjacent parameters of different types and are exactly
  the pair a positional call gets backwards.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheShapeOfTheObjects::test_the_event_flags_are_keyword_only`

## S-08 · KILLED · The attribution default is only observable on an empty turn

- Track: semantic
- Location: `src/adapters/outbound/telemetry/latency.py`:105
- Mutants: 1, hand-authored — cosmic-ray does not substitute string literals
- Mutation: `_longest_gap_after: str = field(default="start", …)` becomes `field(default="", …)`
- Survives because: `observed` overwrites the field from `_previous_kind` on every event, so only a
  turn with no events at all reads the default — and the one test covering that case asserted the gap
  magnitude and not the attribution. A failed invocation is precisely the zero-event case, so the
  field would have shipped blank on the turns most likely to be investigated.
- Verdict: KILLED by `tests/unit/test_latency.py::TestTheContentionFigure::test_a_turn_with_no_events_reports_no_gap`

## S-09 · OPEN · Defaults on `_started` and `_last` are unreachable through the intended path

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:100 and :101
- Mutants: 4 surviving — `default=0.0` becomes `1.0` and `-1.0` on each of the two fields
- Mutation: `_started: float = field(default=0.0, init=False)` becomes `field(default=1.0, …)`
- Survives because: `start()` assigns both fields from the clock before any measurement, so the
  declared default is overwritten on every path the only caller takes. It remains observable through
  the public API — `Timeline().finish()` without `start()` reads it — which is why this is OPEN
  rather than equivalent.
- Verdict: OPEN

Deliberately not resolved. Closing it means asserting the behaviour of a timeline that was never
started, which is a state the module has no meaning for; the honest fix is to make `finish()` refuse
an unstarted timeline, and that is a design change rather than a test. Left open with the reasoning
recorded so the next reviewer does not re-derive it.

## S-10 · EQUIVALENT · Operator substitutions inside a type annotation

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:142
- Mutants: 22 surviving at this location
- Mutation: `def _since(self, instant: float | None) -> int | None:` with `|` replaced by each of
  `+ - * / // % ** >> << & ^` in both the parameter and the return annotation
- Survives because: annotations are strings under `from __future__ import annotations` and are never
  evaluated at runtime. This is the first of ADR-013's two known equivalence classes.
- Verdict: EQUIVALENT

## S-11 · EQUIVALENT · The dataclass slots argument

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:37 and :87
- Mutants: 2 surviving, one per decorator
- Mutation: `slots=True` becomes `slots=False`
- Survives because: `slots` changes attribute storage and not behaviour. This is ADR-013's second
  known equivalence class.
- Verdict: EQUIVALENT

## S-12 · EQUIVALENT · Negating the typing-only import guard

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:31
- Mutants: 1 surviving at this location
- Mutation: `if TYPE_CHECKING:` becomes `if not TYPE_CHECKING:`
- Survives because: the guarded block imports `Callable` for annotations only. Negated, the import
  executes at runtime and succeeds, producing an unused binding and no behavioural difference. Same
  class as S-10: a construct that exists for the type checker and not for the interpreter.
- Verdict: EQUIVALENT

## S-13 · EQUIVALENT · Subtraction replaced by modulo on two clock deltas

- Track: mechanical
- Location: `src/adapters/outbound/telemetry/latency.py`:125 and :148
- Mutants: 2 surviving, one per location
- Mutation: `gap = now - self._last` becomes `gap = now % self._last`
- Survives because: for any two readings of a monotonic clock where `self._last <= now < 2 * self._last`,
  `now % self._last` and `now - self._last` are the same value. With a clock origin in the tens of
  thousands of seconds and advances of a few seconds, that condition always holds, so no realistic
  input distinguishes them.
- Verdict: EQUIVALENT

This pair is worth reading alongside S-01. Before the clock was moved off zero these two mutants were
*killed* — by `ZeroDivisionError` on `now % 0.0`, which is killing a mutant for a reason unrelated to
the logic under test. Fixing S-01 exposed them honestly rather than introducing them, and a
"regression" from 45 survivors to 37 at that step was the suite becoming more truthful, not worse.
Reaching them for real would need a clock origin smaller than the advances measured, which contradicts
what `time.monotonic` does.
