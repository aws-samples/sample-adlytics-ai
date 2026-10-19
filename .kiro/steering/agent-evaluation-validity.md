---
inclusion: fileMatch
fileMatchPattern: ["contracts/prompts/**", "validation/**", "src/adapters/jobs/evaluation_writer/**", "src/domain/evaluation/**"]
description: Governs released-response evaluation fidelity, evaluator calibration, invariant precedence, and scenario evidence when editing evaluator prompts, calibration corpora, evaluation domain code, or the evaluation writer.
---

# Evaluation validity

Decisions: the audit loop and its regression path in ADR-059; the streamed
answer and evaluation after release in ADR-083. Claim-authority scope for evaluators is owned by
agent-semantic-authority and is not restated here.

## Evaluate what the user received

- The evaluated answer is the exact concatenated streamed text the receipt
  counts, retrieved from the evaluation custody boundary. Never evaluate a
  projection, a regeneration, or a partial stream as if it were the response
  the user received.
- Only complete turns are evaluated. Never admit a turn that failed, was
  refused, or lost its client, and never let an evaluation record imply an
  answer the user did not receive in full.

## Calibration

- Every evaluator dimension ships with a fictional calibration corpus that
  declares an expected outcome per case, and the corpus contains at least one
  known-correct case, one contradiction case, and one fluent-but-unsupported
  case.
- Rerun the calibration corpus before deploying any change to an evaluator
  prompt, its rating scale, or its model routing. A failed expected outcome
  blocks the change.
- Calibration corpora are fictional. Never copy a live question, answer,
  identifier, or measurement into one.

## Scores never outrank invariants

- A model-scored result never overrides a failed deterministic invariant, such
  as a failed calibration gate or a receipt check. Never close a finding or
  mark a regression fixed on a soft score alone.
- Absent evaluation delivery is an acceptance failure or pending observation,
  never evidence of health; ADR-059 owns this semantics. Never report an
  evaluation dimension healthy from delivery metrics alone.

## Behavior changes require scenario evidence

- Before promoting a change to the agent prompt contract, a tool description,
  or the agent model identifier, run the regression path ADR-059 names and
  record expected-versus-actual outcomes with the change. Never promote such
  a change on unit tests alone.
