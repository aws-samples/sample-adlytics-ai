# ADR-037: A fact several contracts write has no per-row non-null invariant

Date: 2026-08-19
Status: Accepted

Amends the read path introduced with ADR-028 and the multi-writer write path recorded in ADR-033.
Supersedes nothing.

## Context

Every one of the fifty declared metrics plans. Executing all fifty against deployed dev for the
first time — `tests/integration/test_every_metric_executes.py` — six of them raised `InvariantError`,
which alarms:

- `viewable_impressions`, `viewable_impressions_estimated`, `viewable_rate`, `measurable_impressions`
  failed with `ADLY-MEASURE-0001: A result row carries no value for the measure.`
- `cost_per_ntb` and `cost_per_purchase` failed with
  `ADLY-MEASURE-0001: A monetary result row does not say what currency it is in.`

Planning had proved the model. Only executing proved the SQL, and these two failures are different
faults with one cause.

**`fct_ad_exposure` is written by four contracts.** `amc-dsp-impressions` supplies impressions and
spend, `amc-dsp-clicks` supplies clicks, `amc-dsp-views` supplies viewability, `amc-reach-frequency`
supplies reach. Each writes only its own columns, so the table is a union and any row is null in the
columns its own writer never supplied. Measured in dev, of 16 rows: `impressions` 15 non-null,
`clicks` 14, `viewable_impressions` 7, `measurable_impressions` 7, `currency_code` 15.

`SUM` over a group whose every contributing row came from a different writer returns NULL, not zero.
`figures.py` treated a null measure as "the model and the data disagreeing", reasoning that every
measure a plan can name is declared non-nullable. That reasoning is sound **per contract** and false
**per table** — `amc-dsp-views` does declare `viewable_impressions` non-nullable, and that binds only
the rows it writes.

The second failure is a plain gap. `carried_columns` was `()` unconditionally on the drill-across
path, with no comment where every sibling field has reasoning. `cost_per_purchase` and `cost_per_ntb`
are the only cross-fact metrics denominated in money; every other drill-across is a ratio and needs
no currency. So the monetary cross-fact combination had never executed.

## Decision

**Which columns may legitimately be absent is declared from the contracts, not detected from the
rows.** Data loss and a column this row's writer never supplied are identical at read time, so no
inspection of a result can separate them.

1. `FactShape.partially_written_columns` names the columns that fewer than all the contracts writing
   the fact supply. Computed in `FactShape.for_fact`, which is the only place that knows how many
   writers there are. Empty for a single-writer fact, which is seven of the eight curated tables.
2. `QueryPlan` carries it, by the same route `suppression_columns` already travels. A drill-across
   carries the union of both sides'.
3. A null measure in that set yields **no figure** and is counted in `undefined_rows` — the treatment
   a withheld side already gets, for the same reason. A null measure outside it still raises, so the
   invariant is preserved everywhere it was already correct.
4. `_one_currency` skips a row that states no currency when `currency_code` is partially written, and
   returns `None` when no row states one at all. Refusing on one silent row discarded every other
   group in the answer, on the single-fact path where such a row genuinely arrives.
5. **A drill-across may not carry a column at all, and `QueryPlan.__post_init__` now refuses one.**
   The obvious fix for the currency failure was to carry `currency_code` from the numerator. It was
   tried, and it is wrong: the drill-across composer builds every projection from `group_by` alone,
   so setting `carried_columns` widens `selected_columns` without widening the statement — and the
   adapter pairs those by position, with `strict=False`. Every aggregate shifted one place. The
   currency received the numerator's summed spend, and the withheld-row count received the
   denominator's purchase count, which reads as a withholding that never happened. It surfaced as
   "this result spans more than one currency", the two currencies being decimal strings. A comment
   would not have been enough, because the symptom points nowhere near the cause.
6. The dropped-row narrative sentence enumerates the reasons **this plan** could have produced, and
   says "figure" rather than "ratio". It previously said "their denominator was zero ... which leaves
   the ratio undefined" for a viewable-impressions count, which is neither a ratio nor a denominator.

Not zero, in every case. A day nobody measured viewability on did not have zero viewable
impressions, exactly as a campaign that spent nothing did not achieve a ROAS of zero.

## Consequences

- **Forty-eight of the fifty execute against dev; two fail loudly.** The sweep went 44, then 48. The
  four viewability metrics are fixed. `cost_per_purchase` and `cost_per_ntb` still cannot state a
  currency and are recorded as strict expected failures in
  `tests/integration/test_every_metric_executes.py`, so the day a currency can cross a join that
  file fails and says so. They fail rather than answer, which is the correct end state until G-35 is
  fixed: the alternative reached by carrying the column was an answer with every aggregate shifted
  one position.
- `fct_ad_exposure` now has no per-row non-null invariant on its measures, and that is the truth
  about a union table rather than a weakening. A genuinely corrupt row in it cannot be distinguished
  from a not-yet-delivered one, so the platform reports the absence instead of claiming to detect
  corruption it cannot see. Every other fact keeps the strict check.
- The narrative names several possible reasons for a dropped row rather than one. `undefined_rows`
  is a count and does not record which applied to which row; listing what is possible is honest
  about that, and picking one is not.
- **Recorded, not fixed — G-34.** `_side_value` treats a null drill-across side as a genuine zero,
  which ADR-035 chose deliberately: a campaign with no attributed revenue really earned none, and
  that is the wasted-spend row a user most needs. On a union table that reasoning acquires a second
  case it cannot distinguish — a key whose rows exist but whose spend was never delivered also
  arrives null, and calling it zero fabricates a figure. Separating them needs a per-side row count
  in the composed statement, so it is a change to the SQL and the plan rather than to how a null is
  read. Left as it is because the alternative today would drop legitimate wasted-spend rows, which
  is the worse error.
- The dropped-row sentence had no unit test before this change. It does now.
- **Recorded, not fixed — G-35.** A cross-fact monetary metric cannot report its currency, because
  carrying a column across the join needs the composer's cardinality decided first. Adding
  `currency_code` to each side's `GROUP BY` without adding it to the join key splits that side into
  one row per (key, currency) while the join still matches on key alone, giving two output rows for
  one grain value — and `fct_ad_exposure` has a null-currency row, so the split is not hypothetical.
  Adding it to the join key instead drops every row whose denominator states no currency, which for
  a count denominator is all of them. Both faults live in the same three expressions — each side's
  `GROUP BY`, the `COALESCE`d keys, and the `ON` — so they have to move together.
- **`_as_row` pairs columns with values positionally using `strict=False`, and that is what let the
  misalignment be silent.** The plan invariant closes it for the case that exists. The wider
  question — whether the composer and the mapper should be forced to agree structurally rather than
  by two lists happening to match — is left open and is the reason the invariant is on the plan
  rather than a comment in the adapter.

## Alternatives rejected

**`SUM(COALESCE(col, 0))`.** One line, and it makes every metric execute. It also reports a day with
no viewability delivery as having zero viewable impressions, which is the plausible wrong number this
platform exists to avoid, and it would have hidden the fault instead of fixing it.

**Detect partial writing from the rows.** Impossible, and worth stating: a null is a null. The
information exists only in the contracts, which is why the declaration has to come from there.

**Drop the non-null invariant everywhere.** Simpler, one code path. Rejected because for the seven
single-writer facts a null measure genuinely is a broken load, and losing that check to fix an
unrelated table would trade a real signal for convenience.

**Refuse a metric whose fact is a union.** Would have refused the entire viewability family and both
cost-per metrics rather than answering them. The data is there and the question is answerable; a
refusal would be failing towards an absence that is not there.

**Split `fct_ad_exposure` into one table per contract.** Removes the cause outright. Rejected as far
out of proportion: it changes the canonical model, breaks every metric reading the fact, and
`ctr` — clicks over impressions — would then need a join across two tables to compute what is one
row today.

## Review trigger

Revisit when G-34 or G-35 is addressed. Both need the drill-across statement to carry more than the
grain and the six aggregates — a per-side row count for G-34, a currency for G-35 — so whichever is
done first decides how the composer projects an extra column, and the plan invariant added here has
to be relaxed deliberately rather than deleted in passing.

Revisit if a second fact acquires several writers. The rule is derived from the contracts and needs
no edit to cover one, but the assumption that partial writing is rare — one fact of eight — is what
makes the looser invariant an acceptable trade.
