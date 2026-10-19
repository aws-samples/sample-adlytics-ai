---
inclusion: fileMatch
fileMatchPattern: ["validation/**"]
description: Governs validation-probe authorship, explicit target inputs, result interpretation, local evidence custody, and sanitized publication when editing probes or results under `validation/`.
---

# Validation probes and evidence

AMC-specific workflow (recovery-first submission, instance limits) is owned by
amc-validation-workflow and is not restated here.

## Probes

- Use an existing governed probe when it can settle the question; create a new
  one only when none can. Keep one disputed fact per probe.
- A probe receives every target-affecting input explicitly, per
  meta-aws-operations' operator-interface rule; never bake an operator identity
  into a probe.
- Validate a probe's terminal state or exact returned output against the owning
  contract before drawing a conclusion; acknowledgement-is-not-evidence is
  always-on in meta-ai-collaboration.
- Give every check that can pass a control, run in the same execution, that must
  fail on the defect the check guards. Record both outcomes; a check whose
  control did not run is reported as not proven, never as passed.

## Evidence custody and publication

- Keep credentials, raw output, journals, tokens, and operational identifiers
  local and ignored: RESULTS.local.md, evidence/, and findings/ are that custody.
  Never commit them.
- Publish only dated, sanitized conclusions in validation/RESULTS.md, marked per
  the evidence rules in meta-ai-collaboration, and fold each conclusion into the
  owning contract or design doc — RESULTS.md records that it happened; the owner
  records the fact.
- Never copy a live identifier, name, ASIN, account value, or measurement into a
  published conclusion or a committed fixture.
