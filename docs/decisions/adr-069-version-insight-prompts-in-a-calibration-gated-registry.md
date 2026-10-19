# ADR-069: Version insight prompts in a calibration-gated registry

Date: 2026-09-15
Status: Accepted 2026-09-15 — implemented and live on dev: registry, calibration-gated activation, and the Prompt Administration page; per-tier pointers (ADR-076) implemented in the registry, while the page still manages the card tier only

> **Amendment, 2026-09-28 — calibration runs behind durable work.** Creating a version
> atomically persists it `pending` with a retained content-free outbox identity. The API attempts
> immediate queue delivery; a scheduled dispatcher drains work left by an API crash or queue
> failure. Prompt and model content remain in the retained registry. The
> worker conditionally leases the pending version with a fresh attempt reference, runs the
> fictional corpus outside the HTTP deadline, and completes only before that exact lease expires. SQS retries the whole bounded run at most three
> receives; an acknowledgement-loss replay observes the terminal version and performs no model
> call. The normal operator path polls the existing version projection with bounded backoff and
> never invokes recovery. The separately labelled recovery extension only re-admits pending work
> whose lease is absent or expired. Activation remains impossible until the existing terminal
> calibration transaction records `passed`; there is still no override.
>
> **Amendment, 2026-10-01 — version authorship.** Decision 1's author is the authorizer-verified
> Cognito subject, or the declared system author `contract-seed` for a contract-seeded v1, with
> the creation time, both written once on the version record. Records written before 2026-10-01
> carry no author and are not backfilled; they read as "created before authorship was
> recorded". The display resolves the subject to the operator's email address through Cognito
> `AdminGetUser` at read time, on the versions route's role only, and never stores or logs the
> address. Why read time, not write time: version records are write-once and retained
> indefinitely, so a stored address could never be erased after the user leaves, while deleting
> the Cognito user ends a read-time disclosure within the 15-minute cache; the subject is
> already the registry's only identifier, so no new data class lands at rest; and `AdminGetUser`
> needs the subject in hand, where `ListUsers` would let the role enumerate every address.

> **Amendment, 2026-09-25 — status reconciled with the shipped implementation.** The registry
> store (`src/adapters/outbound/dynamodb/insight_prompt_registry.py`, with per-tier pointer items),
> the calibration runner (`src/adapters/outbound/bedrock/insight_calibration.py`), the seed job
> (`src/adapters/jobs/prompt_registry_seed/`), and their stacks (`InsightPromptRegistryStack` and
> `InsightPromptRegistrySeedStack` in `infra/bin/app.ts`) are implemented, and the operator page is
> served at `/admin/prompts/` (`frontend/components/PromptAdminPage.tsx`). The page rendered its
> roster and lifecycle panel in the 2026-09-15 browser acceptance, the create → calibrate →
> activate path was first exercised live on 2026-09-17, and both tiers of every slot were later
> activated through the same path ([validation results](../../validation/RESULTS.md);
> `validation/probe_prompt_registry.py`). The page does not yet render the deep tier.

> **Amendment, 2026-09-24 — tier lifecycles, composed sources, and outcome calibration
> ([ADR-076](adr-076-tier-insight-narration-across-card-and-deep-dive-models.md)).**
> Three changes under ADR-076, which owns the details. The write-once version record
> (decision point 1) gains the digests of the git-sourced fragments that rendered a
> composed version; operator-authored direct versions remain possible through the same
> create → calibrate → activate path and are marked unsourced. The
> one-mutable-activation-pointer-per-slot model becomes one pointer per slot **per tier**:
> a card prompt and a deep-dive prompt for the same slot are distinct lifecycle
> identities. The calibration gate (decision point 3) changes what "passed" attests — from
> classifying fictional case summaries to outcome calibration over narrations generated
> through the production envelope, judged deterministic-failures-first with independently
> calibrated evaluator dimensions.

## Context

ADR-068 gives every insight slot a task prompt, and the operator requirement is a managed
lifecycle: create, edit, and retire prompts through an admin surface without a deployment.
A naive CRUD API would break three platform invariants at once: provenance (a cached insight
must always answer "which prompt wrote this"), calibration (the evaluation rules require
calibration cases to pass before a prompt change ships), and reproducibility (an overwritten
prompt makes yesterday's insight unexplainable).

## Decision

**Prompts live in a registry of immutable versions with one mutable activation pointer per
slot; "update" mints a version, activation is gated on that version's calibration passing, and
nothing is ever destroyed.**

1. A version record is write-once: slot, dense server-assigned version number, prompt template,
   model routing hint, author, calibration result. Editing a prompt creates the next version.
2. Activation moves the slot's pointer to a calibration-passed version and appends an
   activation event (who, when, from, to) in the same transaction. Rollback is activation of a
   prior passed version — no separate mechanism. An optimistic lock refuses a pointer move the
   operator did not base on the current state.
3. Calibration is the deploy gate, registry-shaped: each version carries fictional cases per
   the evaluation-validity contract (known-correct, contradiction, fluent-but-unsupported), and
   the API refuses to activate an unpassed version. There is no override flag.
4. Retirement clears the pointer so pages render the slot's absence state; version history and
   activation events remain readable because cached insights and audit records reference them.
   Delete does not exist as a capability.
5. The registry sits behind the operator authorization boundary (ADR-060 precedent), enforced
   on every route independently of what any UI shows. The generation path reads only the
   activation pointer and stamps the version it used into the insight's cache record.
6. The prompt version is part of the insight cache key (ADR-068), so activation invalidates
   exactly one slot's cache and an old cached insight remains attributable to the exact prompt
   that produced it.

## Consequences

Operators get a no-deploy edit loop — draft, calibrate, activate, roll back — while every
guarantee a git-reviewed prompt had is preserved in registry form: immutability, a quality
gate, attribution, and history. The [Prompt Administration](../ui/prompt-administration/README.md)
page package owns the surface; this ADR owns why its API refuses what it refuses.

Slot declarations (purpose, inputs, silence condition) stay in the insight contract and are
not editable through this registry; the registry manages only the prompt a declared slot runs.
Changing what a slot *is* remains a reviewed contract change.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Prompts only in git, deployed with code | Loses the operator edit loop the product requires; every wording iteration becomes a deployment. The registry keeps git's guarantees without its cadence. |
| Overwrite-in-place CRUD | Destroys provenance and reproducibility; a cached insight would cite a prompt that no longer exists. |
| Calibration as a warning rather than a gate | A soft gate is a skipped gate; the evaluation rules already forbid promoting prompt changes on judgment alone. |
| Hard delete for retired slots | Cached insights and audit records reference version identities; destroying the referent orphans the audit trail. |

## Review trigger

- Slot declarations themselves need operator editing, which is a governance change to the
  insight contract, not a registry extension.
- Prompt volume or team size argues for draft/review states beyond the single-operator
  lifecycle, which adds a workflow layer on the same storage.
