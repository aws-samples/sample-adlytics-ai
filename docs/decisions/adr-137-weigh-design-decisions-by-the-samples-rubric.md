# ADR-137: Weigh design decisions by the sample's rubric
Date: 2026-10-11
Status: Proposed (2026-10-09). It rests on the owner's direction of 2026-10-09 to weigh decisions
as an aws-sample that follows strong design patterns where they are efficient, on the owner's
approval of 2026-10-08 that review effort scales with risk, and on the owner's approval, on
2026-10-09, of the workflow retrospective's recommendations. It is not accepted: acceptance is the
owner's review of this file.
## Context
Between 2026-10-06 and 2026-10-09, designs were approved before anyone had checked they could be
built at the seams they named. Review loops ran to their round limits, decisions were reversed in
later rounds, and infrastructure was built and then retired. A weighting rubric was written down
only after several rounds of one design chain, and it lived in a working file no other design
read. Until 2026-10-08 every review got the same effort, whatever the change could break.
## Decision
**Every design, plan and decision record weighs its options by the rubric in
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric) from round 1, and
states which numbered items decided it.** Review effort scales with risk, as
[delivery practice §6](../delivery-practice.md#6-review-effort-scales-with-risk) states.

Decided by rubric items (1) and (3): review keeps its full effort wherever data integrity is at
stake, and one order of criteria weighed once from round 1 is a simpler loop than one whose
criteria a later round can reweigh. The rule applies to this ADR and to every decision recorded
with it.
## Enforced by
Review. A reviewer refuses a decision that names no rubric item, or that a higher item contradicts.
No gate can check this, so the coverage matrix lists no test for it.
## Consequences
- A reviewer argues from the same ordered criteria as the author, so a later round can still find
  a fault but can't reweigh the criteria.
- A change that is only UI layout or documentation, and makes or changes no data-correctness,
  security or control-plane decision, ends at one approval, so its loop is shorter; every other
  change, including a decision record that makes such a decision, keeps two reviewers and two
  rounds.
- The rubric's order is itself a decision. Changing it amends this ADR and the delivery-practice
  doc in one change.
## Alternatives considered
- **Weigh each decision on its own merits.** Rejected by (3): that is what reversed decisions in
  later rounds and built infrastructure that was then retired.
- **Put the rubric in `CONTRIBUTING.md` or `docs/conventions.md`.** Rejected by (3): one covers the
  contributor workflow and the other document notation; a reader looks for design practice in a
  design-practice doc.
## Review trigger
A decision reversed after approval on a criterion the rubric did not weigh, or a second
maintainer whose review needs differ.
