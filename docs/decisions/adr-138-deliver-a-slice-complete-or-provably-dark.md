# ADR-138: Deliver a slice complete or provably dark
Date: 2026-10-11
Status: Proposed (2026-10-09). It rests on the owner's direction of 2026-10-08 to deliver complete
functional slices in dev without the risk of half-feature delivery, on the owner's simplified
delivery approved the same day, and on the owner's approval, on 2026-10-09, of the workflow
retrospective's recommendations. It is not accepted: acceptance is the owner's review of this file.
## Context
Work delivered layer by layer produced half features: a backend with no page, a page with no data,
and decoders that had to accept an old and a new format at once until the old one was retired.
Each half needed a transitional path that later had to be found and removed. The simplified
delivery kept the binding definitions and dropped the steps that served no business requirement,
such as live dark-proofs for code with no caller.
## Decision
**A merged slice is either complete or provably dark, as
[delivery practice §7](../delivery-practice.md#7-complete-or-provably-dark) defines them, including
the simplified-delivery amendments.** Anything else stays unmerged.

Decided by rubric items (1) and (4) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a half feature
reachable by a customer can show a wrong number, and merging layer by layer needs a transitional
dual path. The simplified-delivery amendments are decided by (3): they drop proofs that cannot
catch wrong data, and keep every proof that can.
## Enforced by
Review, with existing tests as partial controls. No test reads a slice's entry points and checks
that each is closed, so the coverage matrix lists none for this decision.
- Navigation and route projections:
  `test_data_pipeline_status_is_dark_until_its_navigation_entry_is_added` in
  `tests/unit/test_navigation_manifest.py` proves one page absent from the route manifest and the
  route allowlist and, as its positive control, present once its entry is added. It is marked for
  ADR-115's navigation rule and exercises neither a route's refusal nor a schedule.
- Schedules: the worker schedule assertion in `infra/test/ingestion-stack.test.ts` holds five
  worker schedules disabled. It has no positive control, belongs to no one slice, and is a Jest
  test, which has no `covers` mechanism.
- Review checks the rest for each merged slice: that a dark slice's plan names its entry points
  and closes each, as delivery practice §7 defines, with a positive control where wrong data could
  be shown, and that a complete slice passed its live acceptance. A merge gate cannot read a live
  acceptance.
## Consequences
- A slice merges later, when every layer is ready, or merges dark with its proofs.
- A slice that could show wrong data carries a positive-control test for each entry point it has;
  code with no caller carries only a unit test.
- No transitional dual reads: the API, any re-bake and the frontend land together.
## Alternatives considered
- **Merge each layer when it is ready.** Rejected by (1) and (4): it ships half features and the
  dual paths they need.
- **A live dark-proof for every dark element.** Rejected in the simplified delivery, by (3): for
  code with no caller it adds a step that can't catch wrong data, because none can be shown.
## Review trigger
A user other than the owner on a shared environment, where a dark slice merged early could be
reached by someone the proofs did not consider.
