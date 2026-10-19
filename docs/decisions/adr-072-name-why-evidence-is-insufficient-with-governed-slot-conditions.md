# ADR-072: Name why evidence is insufficient with governed per-slot conditions

Date: 2026-09-17
Status: Accepted — design mandated (product decision, 2026-09-17); implemented and live-verified (dev acceptance 2026-09-17, re-exercised 2026-09-24)

> **Proposed amendment, 2026-10-07.** [ADR-119](adr-119-serve-window-coverage-as-one-server-written-evidence-status-block.md) (Proposed) would serve a shared `EvidenceStatus.v2` block on every v2 artifact, with server labels, missing dates, reason and recovery text, so the coverage sheet stops mapping slot and role codes in the browser. This decision stays in force, unchanged, until ADR-119 is accepted.

## Context

The campaign detail's delivery extension reduces a fixed cohort of per-slot evidence
(spend, impressions, clicks, CTR, CPC, CPM — each in current, prior, and daily roles)
through a strict acceptance predicate: exact coverage, no holes, no suppression, complete
daily series in both windows, matching grain, basis, units, and reconciled
aggregate-versus-daily totals. Each slot passes or fails that predicate for one specific,
knowable reason — and then the reducer discards every one of those reasons, collapsing to a
single `insufficient` outcome with a generic `change_period` recovery and an empty
`evidence` list. No structured log line records the per-slot result either.

The cost of that opacity is recorded evidence, not speculation. The 2026-09-17
investigation (validation/RESULTS.md) initially misdiagnosed a fixture-coverage gap as a
serving defect precisely because every dev campaign returned the same opaque word while the
portfolio showed the same metrics populated; establishing the real cause required reading
the reducer end to end and querying the lake directly. A customer facing the same word has
strictly less to go on: "insufficient — change period" does not distinguish "your daily
rows have holes on three days" from "this source never delivered for this campaign," and
those demand different actions. An unactionable refusal is trust-preserving but not useful;
the enterprise outcome (product decision, 2026-09-17) requires refusals a customer can act on.

The attribution side of the same detail already carries per-slot conditions on the wire
(`empty_covered` and its siblings, in the domain's closed condition vocabulary and the
frontend's strict decoder), so the pattern exists; delivery simply never adopted it. The
page's standing rendering rule also constrains the fix: designed absences render "plain
words, never a reason code" — the code grammar belongs to the wire and the logs, and the
words a user reads must be served, not client-derived.

## Decision

**Every slot-level refusal names its condition from a closed vocabulary, the artifact
carries those conditions with safe counts, the server serves the plain-language reading,
and an insufficient card opens a detail sheet that shows it.**

1. **Typed refusal instead of silent `None`.** The delivery acceptance predicate returns
   either an accepted value or a typed refusal naming one condition from a closed, domain-
   owned vocabulary (the delivery counterpart of the attribution condition enum) plus safe
   integer context — for example an incomplete daily series carries days present and days
   expected. Conditions describe evidence states (coverage hole, suppressed rows, missing
   days, basis mismatch, grain mismatch, aggregate-series divergence, never-loaded source),
   never predicate internals. Raw row values never travel with a refusal — counts and dates
   only, within the existing custody rules.
2. **The artifact carries per-slot status.** The delivery block's `evidence` field — already
   declared as "ordered slot evidence and qualification" and served empty today — is
   honored: one ordered record per (slot, role) with its outcome (accepted or its refusal
   condition and counts). The overall outcome reduction is unchanged; what changes is that
   `insufficient` arrives with its itemized why.
3. **Served words, not client-mapped codes.** For each refusal condition the server also
   serves a plain-language explanation line and a concrete recovery reading (what is
   missing, and what would fill it — the same "state why and what would fill it" convention
   the designed-absence states already follow). The browser renders served words verbatim;
   it never maintains its own code-to-copy table. Codes stay on the wire and in logs; words
   are what users see. This preserves, rather than amends, the page's "never a reason code"
   rendering rule.
4. **The insufficient card is an opener.** An insufficient delivery (or attribution) card
   gains the page's standard affordance: it opens a detail sheet — same dialog/sheet
   mechanics, focus custody, and compact full-height presentation as the metric-focus
   dialog — listing each metric's status in plain words with its recovery reading. A card
   is never a dead end; the existing tile rule ("a tile is never a dead button") extends to
   refusal states.
5. **One structured log line per insufficient reduction.** The handler emits a single
   structured lifecycle event carrying the per-slot conditions whenever a delivery or
   attribution reduction lands on `insufficient` — closing the recorded observability gap
   (no log line existed) independently of any UI work.
6. **Attribution renders through the same sheet.** Attribution's existing per-slot
   conditions gain the same served plain-language readings and render in the same sheet;
   the vocabulary stays owned where it lives today, and no second rendering pattern is
   introduced.

## Consequences

A refusal becomes a diagnosis: the user learns which metrics blocked the view, why, and
what would fill them; an operator gets the same answer from one log line instead of an
end-to-end code read and a lake query. The 2026-09-17 misdiagnosis class is eliminated for
users and operators alike.

The closed condition vocabulary becomes a public contract surface: adding a condition is a
versioned artifact change with frontend decoder lockstep, exactly like every other closed
vocabulary on this page. Served explanation lines are governed copy — owned server-side
beside the condition definitions, not scattered in components.

The predicate refactor (bare `None` → typed refusal) touches the delivery reducer and its
tests but does not change what is accepted: acceptance criteria are identical, and a
refusal that names its reason refuses exactly what silent `None` refused.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep the opaque outcome | The recorded misdiagnosis cost; an unactionable customer refusal; the information is already computed and then thrown away — opacity buys nothing. |
| Free-text refusal reasons | Ungoverned copy on a governed wire; untestable, untranslatable, and drifts per call site. Closed vocabulary plus served words keeps both halves governed. |
| Client-side code-to-copy mapping | Duplicates governed copy into the browser, drifts from the server's vocabulary, and violates the served-words rule the page already follows for absences. |
| Expose the predicate's internal check names | Implementation-shaped, unactionable ("row_count mismatch"), and freezes refactoring; conditions must describe evidence states, not code paths. |
| Logs only, no artifact change | Fixes the operator, abandons the user; the enterprise requirement is user-actionable refusals. |

## Review trigger

- A third surface (beyond campaign delivery and attribution) needs itemized insufficiency —
  extract the shared condition-reading mechanics into a shared contract rather than cloning.
- Recovery readings start naming actions the product cannot execute — recovery must stay
  within the "executable server-authorized controls or explicit no-self-service outcomes"
  guardrail.
