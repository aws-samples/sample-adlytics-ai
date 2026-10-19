# ADR-064: Retire planner-era contracts and anchor the audit trajectory

Date: 2026-09-14
Status: Accepted — implemented in code; deployed dev acceptance recorded in `validation/RESULTS.md`; amended 2026-09-26 by ADR-080 (partly implemented; current state in ADR-080's status line); amended 2026-09-29 by [ADR-083](adr-083-stream-the-agent-answer-and-govern-it-by-audit-and-evaluation.md): the trajectory has no finalizer step and no plan selection (deployment state in ADR-083's status line)

> **Amended 2026-09-26 by [ADR-080](adr-080-confine-each-audit-and-evaluation-record-family-to-one-writer.md) — where companion items live (partly implemented; current state in ADR-080's status line).**
> The item shapes and keys below do not change, but findings, evaluation delivery receipts, writer
> receipts, and evaluation results will each move to their own table with one writer. The audit table
> will keep the receipt, its delivery disposition, and its evaluation admission. Until then all six
> families share the audit table.

## Context

The managed Harness architecture ([ADR-056](adr-056-publish-conversation-as-evidence-bound-semantic-blocks.md),
[ADR-061](adr-061-bind-agent-authored-presentations-to-governed-evidence.md),
[ADR-062](adr-062-route-claims-by-semantic-authority.md)) replaced a server-side planner that
proposed turn plans, drafted responses, assessed reasoning, and stored epistemic records. The
planner's contracts did not leave with it. `ConversationAuditRecord.v2` still carried objective
summaries, a terminal authority source, evidence and prompt-receipt references, and an
initial-plan refusal code that the managed runtime wrote as empty values on every turn.
`PresentationPlan.v1` remained decodable beside `PresentationPlan.v2`. The KMS-authenticated
evidence receipt that ADR-061 records as a rejected in-loop binding mechanism was still minted on
every governed tool result and never verified. Forty-nine source modules — planning, drafting,
assessment, investigation, evidence bundles and their materialization, the publisher, the
epistemic store and its DynamoDB table — were reachable from no deployed entrypoint. An
API-owned bridge Guardrail from the ADR-042 ownership cutover still deployed beside the one the
routes apply.

Two investigative gaps sat underneath the debt. The audit receipt could not be joined to its
trace: the API and Harness spans share a trace, but the receipt held no trace identifier, and
Gateway does not propagate trace context to tool Lambdas, so a tool call's own trace was
reachable from nothing the receipt retained. And evaluation linkage stored one result per
admission under `EVALUATION_RESULT#<admission>`, so when the V4 online evaluation began scoring
each released turn on three dimensions beside the V3 judge, the second result for an admission
was a byte conflict rather than a record, and its scale labels ("Partially Grounded",
"Contradictory") were misfiled as evaluator failures by a four-point helpfulness enum.

## Decision

**Delete the planner-era contracts and code rather than widen them, and make the audit receipt
the anchor from which every other record about a turn is reachable.**

1. `ConversationAuditRecord.v3` is managed-only. It drops every planner slot and every enum
   member no writer produces, and adds the turn's OpenTelemetry `trace_id`, the typed error and
   service codes behind a dependency failure, and a `trajectory`: every tool call in the order the
   Harness made it, including each rejected or errored finalizer attempt with its violation
   codes. A governed step is anchored by the correlation ID the tool minted inside its response
   and stamped on its own span; a finalizer step by the Gateway message reference the finalizer
   echoes back (`PresentationFinalizerResult.v3`). Either reaches that call's trace from the
   receipt. Each governed step records whether the accepted plan selected anything from it. The
   writer stamps `managed-harness.v2`; receipts written by `managed-harness.v1` read through the
   V3 contract with an unanchored trajectory and no trace, which is exactly what that runtime
   recorded. Planner-era receipts are retired, not read.
2. `AuditFinding.v3` gains an optional step anchor, and the processor derives two step-level
   findings: a finalizer attempt the server rejected, and a governed result the accepted plan
   never used.
3. `EvaluationResult.v2` is keyed by admission and evaluator
   (`EVALUATION_RESULT#<admission>#<evaluator>`), retains the evaluator's name, and keeps the
   evaluator's own normalized scale label. The ingress admits a record by its released
   online-evaluation configuration ID and reads evaluator identity from the record's evaluator
   ARN and name, so the V4 result group is a second subscription to the same ingress and no
   per-evaluator deployment parameter exists.
4. The three Quality Review projections are reshaped around the questions they answer. `audit`
   is what happened and the chain of custody: identity (audit, correlation, trace), provenance
   (prompt digest, Guardrail version, contract versions), outcome, trajectory, publication, and
   the ADR-063 delivery companion. `findings` is the defect pattern: each finding beside the
   trajectory step that produced it and the provenance a fix must be tested against.
   `evaluation` is how good the released answer was and whether the pipeline that judged it ran:
   turn outcome first, then the protected chain with the hop that is missing, then one result
   per evaluator.
5. The evidence receipt channel (adapter, settings, KMS key, grants, environment), the
   `PresentationPlan.v1` family, the ApiStack bridge Guardrail, the EpistemicEvidence stack, and
   the forty-nine unreached modules with their tests are deleted. Retirement is decided by an
   import-reachability pass from every deployed handler and build root, not by judgment about
   whether code looks used.

## Consequences

An investigator holding an audit ID can reach the turn trace, each tool call's own trace, the
finalizer attempts the model needed, which governed results the answer actually used, whether
the user received the answer, and how four evaluators scored it, without a second identifier.
The audit table gains no new item families; one key composition changes, with dual-read of the
stored shape. Roughly twenty thousand lines of unreached code and their tests are gone, and
`import-linter` plus the reachability pass are the gates that keep it that way.

The `Adlytics-EpistemicEvidence-<env>` stack is no longer defined by this repository, and the
deployed dev stack was deleted as an operator action once nothing read its parameter. Its table was
`RETAIN`, so the empty `adlytics-dev-epistemic-evidence` table outlived the stack and was deleted
under its own approval as a separate data-deletion decision.

The per-evaluator result read is a key-bounded `Query` on the audit table where the single-result
read was a `GetItem`, so the evaluation review function's grant widened to match the findings
function's. The live route check, not the unit tests, is what caught that the grant had not moved
with the read.

An inbound Guardrail intervention is declined before a turn exists and leaves a structured log
and trace rather than an audit record. The enum member that implied otherwise is gone; recording
that outcome as a receipt is a separate decision.

Stored `managed-harness.v1` receipts and `EvaluationResult.v1` results lose nothing they held,
but they cannot gain what was never recorded: their trajectories are unanchored and their results
carry no evaluator name.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Widen `ConversationAuditRecord.v2` around its empty planner slots | Every reader would keep validating and projecting fields the runtime cannot populate; the contract would describe a system that no longer exists. |
| Keep the evidence receipt dormant | One KMS `GenerateMac` per tool result with no consumer; ADR-061 already records the mechanism as rejected. |
| Defer per-evaluator results until V4 promotes | The V4 configuration was already scoring every sampled turn; the route would have reported one of four judgments as the evaluation and dropped three at the store. |
| Publish V4 evaluator IDs and names as SSM parameters, one ingress per family | Three more parameters and a second ingress deployment to carry facts every result record already states about itself. |
| A server-injected custody channel so the finalizer could verify evidence in-loop | Gateway Lambda targets receive no turn context; ADR-061's post-Harness verification already binds the plan to observed custody. |
| Record a `harness_version` in provenance | `CfnHarness` exposes no version attribute; reading it at runtime would add a control-plane call and IAM grant to every turn. Provenance is the prompt digest, Guardrail version, and contract versions. |
| Delete modules by inspection | Inspection had already missed forty-nine of them; only the import closure from deployed roots is a defensible boundary. |
