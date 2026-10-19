---
inclusion: auto
name: meta-root-cause-analysis
description: Use when diagnosing a failure, investigating an unexpected result, or planning a temporary non-production diagnostic experiment.
---

# Root cause analysis

Research method is meta-research. Evidence markers are always-on in
meta-ai-collaboration. What telemetry exists and how correlation IDs propagate is
backend-observability-lambda. Which test tier proves what is
meta-testing-strategy. None of that is restated here.

## Establish the failure before touching anything

- Before changing code or configuration, capture the existing exact failure.
  Reproduce it only when the target and method are authorized and do not increase
  impact.
- State observed and expected as concrete values, both of them. A status code, a
  string, a row count.
- Never edit a file during triage. An edit made while diagnosing destroys the
  reproduction you have not finished reading.
- Bound the symptom before opening the code you suspect. Establish which hop
  fails first; reading code chosen by intuition is how the wrong layer gets
  fixed.

## Read provider diagnostics before intervention

- For any provider or SDK failure, retrieve the structured diagnostic for the
  exact reproduction before changing code, IAM, deployment configuration, or
  resource settings.
- Record the full provider-diagnostic projection that aws_failure_context in
  src/adapters/outbound/aws_errors.py returns — never a partial or paraphrased
  subset.
- Treat that record as the first hypothesis boundary. Never infer a missing
  permission or resource from an implicit-deny simulator result when the
  provider diagnostic has not been read.

## Build the trace hop by hop

- Walk the request path in order and record status and identity at every hop.
  Never infer a hop's behaviour from the hop before it.
- For any request through the edge, account for CloudFront, API Gateway, the
  authorizer, the handler and the outbound dependency. Name the first hop whose
  behaviour deviates from expected.
- Take the correlation ID from the failing response and fetch the log lines for
  that exact request. Never reason from logs you did not correlate to the
  reproduction.
- Query the deployed resource's own configuration rather than the template you
  believe you deployed. Drift is the failure being diagnosed often enough.
- When a hop cannot be observed, say which one and mark it unverified. Never fill
  an unobservable hop with a plausible mechanism.

## Separate cause from coincidence

- Name the mechanism. "401 because the identity source is absent, so API Gateway
  rejects before invoking the authorizer" passes. "Auth is misconfigured" does
  not.
- Show the cause accounts for every symptom. A cause explaining some of them
  means two faults, or the wrong cause.
- Rule out the other candidates explicitly and record how. Stopping at the first
  plausible cause is how the second one ships.
- When tests passed while the system was broken, explain why, and treat that as a
  second finding with its own fix. A green suite over a real defect is a gap in
  the suite.

## Research before proposing

- Check whether a recorded decision already covers this.

## Bounded diagnostic experiments

- If deployed configuration and the provider diagnostic do not settle the cause,
  propose the smallest temporary non-production experiment that can disprove or
  confirm one hypothesis.
- Any temporary permission, broad policy, or configuration experiment requires
  explicit user approval and must name its exact scope, impact, and reversion.
- Run one temporary validation only, revert immediately, verify the deployed
  reversion, and record the conclusion as evidence.
- Never use a wildcard or broad policy experiment when an exact provider
  diagnostic identifies the action and resource to test.

## Present fixes with tradeoffs

- For a material, irreversible, or design-changing fix, present the viable
  options, their tradeoffs, and a recommendation.
- For an approved low-risk fix with one evidence-supported remedy, implement it
  directly and report the evidence.
- State what an option does not fix. A symptom fix that leaves the cause in place
  is legitimate when labelled as one, and never otherwise.
- Never present a fix that weakens a security boundary alongside one that does
  not without saying which is which.

## Validate before completion

- Run the focused regression and the validation gates required by the changed
  surface. Do not claim a fix is validated until the original failure evidence
  and required gate evidence have been read.
- Label unimplemented options analysed, not validated, so the evidence behind
  the options is never implied to be equal.
- Add regression coverage when the cause belongs to a testable repository
  boundary. If the cause is external state, deployment configuration, or provider
  behaviour, strengthen the owning acceptance or attestation instead.
