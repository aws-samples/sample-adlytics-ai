---
inclusion: auto
name: meta-research
description: Use when researching or validating a claim that later design, implementation, diagnosis, or documentation will rely on, including source selection, conflicting evidence, negative results, and stopping criteria.
---

# Research method

The universal evidence guardrail is owned by meta-ai-collaboration. Probe execution,
evidence custody, and publication are owned by validation-probes. Failure reproduction
and diagnostic experiments are owned by meta-root-cause-analysis.

## Frame the claim

- State the exact claim, target, scope, version, and date before comparing evidence.
- Separate normative claims about a contract or design from observed claims about an
  implementation or deployment. Evidence for one does not establish the other.

## Evidence authority follows the claim

- Select evidence by what can establish the claim, never by a universal source hierarchy.
- Use deployed inspection or a governed probe for observed deployed behavior. Limit the
  conclusion to the target, request, version, account, and time actually exercised.
- Use the current official contract or schema for promised external behavior, repository
  source and tests for repository implementation, and accepted decisions for normative
  project design. None proves conformance at another boundary.
- Treat practitioner material, summaries, and search results as discovery aids. Read the
  source capable of establishing the claim before relying on it.
- Never use model recollection as evidence.

## Conflicting evidence

- When normative and observed evidence differ, record the specific conformance gap.
  Never discard either side as the loser.
- When sources address different scopes, versions, or dates, qualify the claims instead
  of reporting a contradiction.
- If decision-relevant evidence still conflicts for the same claim and authority
  boundary, stop the dependent decision and record an open question in the document
  that owns the fact.
- A governed probe may resolve only the scope it exercises. It cannot establish a
  universal external contract from one observation.

## Research results

- Record a negative result when an expected source is absent, inaccessible, or
  insufficient. Put the durable conclusion or open question in the owning document.
- Cite the specific source that establishes every relied-on external claim. Citation
  and licensing format follow the owning documentation convention.
- Stop when the claim has evidence from the authority capable of establishing it and
  no known unresolved evidence contradicts that same claim. Agreement between weaker
  sources does not substitute for the required authority.
