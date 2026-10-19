# Prompt Administration — low-level design

## 1. Wire contracts

| Contract | Responsibility |
|---|---|
| `PromptSlotProjection.v1` | Slot id, display name (amended 2026-10-07), purpose, output class, silence condition summary, active version or none, retirement state. Read-only; slot declarations are owned by the insight-contract schema. |
| `PromptVersionRecord.v2` | Immutable: slot id, version, prompt template, model routing hint, `created_by`, `created_at`, calibration result (pending/passed/failed with case names). |
| `CreatePromptVersionRequest.v1` | Slot id, prompt template, model routing hint. Returns the new version identity; triggers calibration. |
| `ActivatePromptVersionRequest.v1` | Slot id, version, expected current version (optimistic lock). Refused unless calibration passed. |
| `RetireSlotRequest.v1` | Slot id, expected active version. |
| `PromptActivationEvent.v1` | Append-only: slot, from-version, to-version, operator identity, at. |

### Exact API routes

All routes are `POST` behind the operator authorizer, following the quality-review admin
convention (session cookie plus CSRF header; the authorizer supplies operator identity).

| Route | Accepted body | Returns |
|---|---|---|
| `/api/prompt-registry/slots` | `{}` | `PromptSlotProjection.v1` roster. |
| `/api/prompt-registry/versions` | `{slot_id}` | Version records for one slot, newest first. |
| `/api/prompt-registry/create-version` | `{slot_id, prompt_template, model_routing_hint?, tier?, fragment_digests?}` | The new version identity; calibration starts. |
| `/api/prompt-registry/activate` | `{slot_id, version, expected_active_version}` | The activation event, or a typed conflict/unpassed-calibration refusal. |
| `/api/prompt-registry/retire` | `{slot_id, expected_active_version, tier?}` | The retirement event, or a typed conflict. |

> **Amended 2026-09-24 (ADR-076 slice D).** The registry lifecycle is slot × tier: each slot
> carries one activation pointer per delivery tier (`card`, `deep`), and every version record
> belongs to exactly one tier. `create-version` accepts an optional `tier` (default `card`)
> and, for tooling-composed versions, a `fragment_digests` object (`foundation`, `tier_core`,
> `slot_block`; 64-hex sha256 each) recording the git-sourced fragments that rendered the
> prompt; a version created without digests is marked **unsourced** (the operator emergency
> loop, unchanged). `activate` derives the tier from the named version record, so a request
> can never move the wrong tier's pointer; `expected_active_version` locks against that
> tier's pointer. `retire` accepts an optional `tier` (default `card`). Read shapes:
> `PromptVersionRecord.v1` gains `tier` plus either `fragment_digests` or `unsourced: true`;
> `PromptSlotProjection.v1` gains `active_deep_version` (version numbers remain one dense
> per-slot sequence shared across tiers, so `newest_version` stays tier-blind). The
> `model_routing_hint` vocabulary is unchanged and orthogonal: the route selects the
> configured model id; the tier selects which surface's pointer governs.

> **Amended 2026-10-01 (ADR-069 amendment, B1).** `PromptVersionRecord.v1` becomes
> `PromptVersionRecord.v2`. `created_by` replaces `author` and is a closed union:
> `{kind: "operator_email", email}`, `{kind: "operator_not_found"}`,
> `{kind: "operator_unavailable"}`, `{kind: "system", name: "contract-seed"}`, or
> `{kind: "not_recorded"}`. The email is resolved from the stored Cognito subject at read time
> and never stored; the subject never appears in the response; `name` is a wire-only closed
> value. `created_at` is an ISO 8601 string, and is `null` exactly for `not_recorded`: records
> written before 2026-10-01 carry no author and are not backfilled. A contract-seeded v1 carries
> the system author.

Explicitly forbidden everywhere: operator identity in the body (the authorizer owns it), slot
declaration fields, version mutation fields, unknown fields. A non-operator caller receives
the same nondisclosing response as an unknown route.

> **Amended 2026-10-05 (ADR-078, path-scoped body-size exclusion).** Every prompt-registry route
> refuses a request body over 32,768 bytes with `413 {"error":"request_too_large"}`, before the
> body is parsed and before any registry, store or queue call. The count is the body's UTF-8
> length, or its decoded length when the event is base64-encoded; invalid base64 is the existing
> `400 invalid_request`. The refusal logs one WARNING `refused` line carrying only the status, the
> byte count and the ceiling, and records the span as `refused` with status 413. At the edge, the
> web ACL already refuses a create-version body over 32,768 bytes and any other route's body over
> 8,192 bytes, so the handler refusal is defence in depth.
>
> The administration page enforces the same limit before it submits: over 32,768 UTF-8 bytes of
> request body, Save is disabled and the page says "This prompt is too long to save. Shorten it
> by N bytes (about N characters of plain text).", linked to the editor with `aria-describedby`
> and announced through a `role="status"` region. A 413 shows the same message. A 403 with an
> HTML body is the edge's block page, which `callApi` returns instead of starting a session
> refresh (ADR-007 amendment, 2026-10-05); the page then says "This request was blocked before it
> reached the service. If the prompt is long, shorten it and try again." and keeps the editor
> content.

Creating a version returns `202 {slot_id, version, outcome:"pending"}` after one transaction
persists the immutable version and its content-free retained outbox identity. The API attempts an
immediate queue send and conditionally removes the outbox only after SQS accepts it; a scheduled
dispatcher drains retained work after an API crash or queue failure. Normal clients poll
`/versions` for that exact version with bounded backoff; polling never starts model work. The
separately labelled recovery extension `/api/prompt-registry/extensions/calibrate` accepts only
`{slot_id, version}` and conditionally re-admits work only when the stored version is still
pending and has no active worker lease. It returns the current pending/passed/failed state, reloads
no caller content, and is not a browser workflow or replacement for the five canonical routes.

## 2. Storage

One DynamoDB table, on-demand, PITR:

- `SLOT#{slot_id} / VERSION#{n}` — write-once version record (condition: attribute_not_exists).
- `SLOT#{slot_id} / POINTER` — active version; every change is a transaction with an
  `EVENT#{ulid}` append carrying the activation event.
- `SLOT#{slot_id} / CALIBRATION_OUTBOX#{n}` — content-free undispatched work, created in
  the same transaction as `VERSION#{n}` and removed conditionally only after SQS accepts it. The
  API attempts immediate dispatch; the worker's one-minute scheduled pass drains anything left by
  an API crash or queue failure. Send-before-delete replay may duplicate a queue message, which the
  worker lease converges.
- Version numbers are dense integers assigned by the API, never by the caller.
- *Amended 2026-10-01 (B1):* each version record written since then carries its attribution:
  `author_kind` (`operator` or `system`), then `operator` (the authorizer-verified Cognito
  subject) or `system_author` (`contract-seed`), and `created_at` (ISO 8601 UTC). A record with
  none of the four is legacy; any other combination is malformed, and the routes answer it as
  unavailable. No email address is stored.
- A pending version may carry one worker lease reference and expiry. Each receive mints a fresh
  attempt reference. Claim is a conditional update requiring `pending` and an absent/expired lease;
  completion requires that exact reference and an expiry later than completion time, then removes
  it in the same transaction that writes the result and per-case rows. A terminal redelivery is
  acknowledged without a model call; an active lease is retried after queue visibility, never
  acknowledged as complete.
- *Amended 2026-09-24 (ADR-076 slice D):* `SLOT#{slot_id} / POINTER` remains the **card**
  tier's pointer (retained records stay readable unmigrated); the deep tier's pointer is
  `SLOT#{slot_id} / POINTER#deep`. Version records carry a `tier` attribute (absent reads
  as `card`) and, for composed versions, the `fragment_digests` map; the pointer replicates
  the composed version's combined fragment digest so the generation logs can stamp it.

## 3. UI composition and states

> **Amended 2026-10-07 (information architecture Slice 1).** The two-panel `admin-grid` is replaced
> by one stacked column built from the shared table (ADR-118). `PromptSlotProjection.v1` gains
> `display_name`, the governed readable name each insight contract declares; the loader requires
> it, the slots route serves it beside `slot_id`, and the page refuses a roster without it, so the
> browser keeps no slot label map. The API stack deploys before the page that reads it.

One stacked column in the shell:

- **"Insight prompts" table**: one row per slot, headed by its display name (a button that selects
  the slot), with its active version and its other versions.
- **Selected slot section**: the display name as its heading, a retired pill when no version is
  active, the purpose and output class, and when the slot stays quiet. Below it, the
  **"Versions, active first" table**: version with authorship and model route, status, cases
  needing changes, and the actions View prompt and Activate (secondary, shown only once calibration
  passes). View prompt opens the version's read-only monospace text beneath the table. Activate and
  Retire slot open a confirmation dialog, which holds the only primary action and states the cache
  consequence. New version opens the editor.
- States that must be visibly distinct: no-slots, calibration pending, calibration failed
  (failing case names shown), activation conflict (pointer moved since load — reload prompt),
  unauthorized (route renders the shell's standard not-authorized state, never a broken page).
- No raw enum or reason code reaches the page; every state is plain words plus the next step.
- Each version line states its authorship in one sentence (amended 2026-10-01):
  - `operator_email`: "Created by {email} on {date and time} UTC."
  - `operator_not_found`: "Created on {date and time} UTC by an operator whose account could not
    be found."
  - `operator_unavailable`: "Created on {date and time} UTC by an operator. Their email address
    could not be loaded right now."
  - `system`: "Created by the deployment seed on {date and time} UTC."
  - `not_recorded`: "Created before authorship was recorded."
  The email renders as text, not a link. A record the decoder refuses shows the unavailable
  state.

## 4. Implementation seams

- Registry handlers are thin adapters; version-transition rules (write-once, calibration gate,
  optimistic pointer move) live in domain code, unit-tested without AWS.
- The calibration runner reuses the evaluation-validity calibration contract: fictional cases
  declared beside the slot's insight contract, one per family per prompt version — known-correct
  and exploratory (generated by the candidate prompt, must accept), derived-figure (a correctly
  derived figure, must accept), and contradiction, fluent-but-unsupported,
  miscomputed-derivation, and incompatible-basis (supplied drafts, must reject). The calibrated
  evaluator decides every structurally valid case; no text check precedes it (ADR-082). Each
  case is judged in its own call, and the calls run in parallel. The judge sees the evidence
  the generator had (the case's findings and window profile) and the narration, never the
  case's family or summary, which would name the verdict. It first writes a one-line check
  that recomputes every stated figure, then gives its disposition. A conclusion that follows
  necessarily from the supplied values counts as derived. Generator and judge get the
  production payload's evidence: the slot contract's purpose (never the case summary, which
  states the expected answer) and a window profile with a fictional aligned prior period,
  which the case findings name. Each derived-figure draft and its miscomputed sibling differ
  only in the figure, so a rejection of the sibling tests the arithmetic. The evidence behind
  these choices, and the measured pending rate, are in `validation/RESULTS.md` (2026-09-28).
- Calibration runs in a dedicated SQS worker, not the prompt-admin request. Its queue carries no
  prompt or model content and redrives at most three receives to a DLQ alarm. The 45-second read
  transport has a separate five-second connect bound; either candidate-generation call shares one
  parallel wave. The evaluator has a five-second connect and ten-second read bound, so both
  sequential waves consume at most 65 seconds before persistence inside an 85-second lease and
  90-second Lambda; queue visibility is 95 seconds. SDK retries remain disabled: an
  exhausted provider call returns the message for queue-level retry, where the attempt count is
  visible and bounded. Every failure is reported before retry. Thread fan-out propagates tracing
  context so generation and judge spans remain children of the calibration attempt.
- **Rollout of ADR-082 (a manual step; nothing enforces it).** The card output envelope no longer
  declares `cited_figures`, and every registry version composed before ADR-082 still instructs the
  model to fill it, so its generations are refused as malformed and the slot renders
  unavailable. Right after the deploy that ships this change, run `validation/ship_prompts.py`
  for every slot and both tiers with `--activate`. A version is activated only once calibration
  passes; until it is, that slot stays unavailable, and nothing else is affected.
- The insight generator's read of the activation pointer is one `GetItem` on a hot path and is
  cached with the same discipline as other configuration reads; the prompt version it used is
  stamped into the insight cache key and provenance record.

## 4b. Cross-cutting qualities

Latency budgets, authorization posture, retention, failure containment, observability, and
cost are owned by the [operational design](operational-design.md).

## 5. Tests

- Domain: version write-once refusal, activation refused on pending/failed calibration,
  optimistic-lock conflict, retirement semantics, dense version assignment.
- Adapter (integration tier): transactional pointer+event write, condition failures surfaced as
  typed conflicts, operator-boundary enforcement on every route.
- Authorship (amended 2026-10-01): a created version and a seeded v1 round-trip their author and
  creation time through the registry; every partial authorship row is malformed and the route
  answers 503 `unavailable` (never a conflict); the page renders each attribution kind's sentence
  and no wire enum (`not_recorded`, `contract-seed`).
- Frontend: roster/detail states including failed calibration and activation conflict; a pending
  created version polls `/versions` with bounded increasing intervals, stopping on terminal state,
  error, unmount, or budget exhaustion. Slot selection and unmount invalidate both polling and any
  in-flight create/activate/retire continuation before it may mutate UI state, reload, or start a
  poll; no admin navigation without operator authorization.

## 6. Prompt authoring standard

Every registry prompt version is authored against this standard and reviewed before its
calibration run; the registry API is the only path a prompt reaches production
(ADR-069) — prompt text never lives in `src/`.

1. **Role.** The platform speaks in one voice: the conversational analyst persona owned by
   `contracts/prompts/v1/managed-conversation.md`. A slot prompt restates that role in one
   sentence scoped to its surface ("You are the workspace's analyst, narrating one declared
   finding beside the evidence"), never a second persona.
2. **Intent.** One sentence naming the slot's single declared purpose — the one inference it
   exists to make — matching the contract's `purpose` field verbatim in meaning.
3. **Input contract.** The prompt describes what the findings payload contains and that it is
   the complete ground truth: every figure a narration states is a supplied value or validly
   derived from supplied values; entity display names, windows, and qualifications come only
   from it.
4. **Grounding rules.** State the figure rule (ADR-082): supplied values and figures validly
   derived from them (differences, percentage changes, shares, comparisons, labelled
   projections), carrying the qualifiers of the figures they came from, never combining
   incompatible basis, grain, window, or currency, and never a figure from outside the input;
   no external knowledge about this customer's data; no causal attribution beyond supplied
   findings; carry qualifications when supplied. The architectural guardrails (closed inputs,
   structured output and declared output class, silence) bound the call; figure faithfulness
   is measured by calibration and evaluation, never by a text check.
5. **Silence.** Restate the contract's silence condition and instruct that returning silence
   is a correct, complete answer.
6. **Output.** Restate the structured output schema, the output class (Observed narrates
   ranked facts; Perspective interprets supplied findings; Hypothesis proposes only a read
   this platform can serve), sentence budget, and tone: plain, specific, customer-readable
   labels, no hedging boilerplate, no restating what a chip already shows.
7. **Calibration.** Every version change reruns the slot's fictional corpus before
   activation; a corpus gains a case whenever a review finds a new failure mode.
