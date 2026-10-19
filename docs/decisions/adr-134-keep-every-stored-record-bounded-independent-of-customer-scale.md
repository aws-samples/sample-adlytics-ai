# ADR-134: Keep every stored record bounded independent of customer scale
Date: 2026-10-11
Status: Proposed (2026-10-09). It rests on the owner's approval, on 2026-10-09, of the workflow
retrospective's recommendations, which named this decision. It is not accepted: acceptance is the
owner's review of this file. Implemented in code on `feat/hardening-no-silent-failure`
(`bc9ee823`, `6a9b61ae`) and not yet deployed, so its live checks on dev are pending.
## Context
Service limits were found only at real-shaped volume, after the failure, because nothing measured a
record or a listing at the size the estate can reach.
- One 57-campaign conversion bucket produced a 33,762-byte DynamoDB publication record against the
  32,000-byte bound, because the record listed every selected identity mapping inline. Every
  conversion delivery was refused and dead-lettered. `54628491` moved the list into a claim check.
- `DescribeStackResources` returns at most 100 resources with no continuation token. The API stack
  holds 144, so the frontend publish preflight could not find the Presentation function and refused
  every publish (`951e6630`). Thirteen more operator and probe reads had the same cap and could miss
  a resource with no signal; `bc9ee823` moved all of them to one paginated reader
  [repo: `src/adapters/outbound/cloudformation_stack.py`].
- Bound tests that pick their own size pass at a size the estate exceeds. The synthetic catalog's
  governed campaign estate is `BULK_CAMPAIGN_COUNT`
  [repo: `src/adapters/jobs/fixture_generation/catalog.py:51`].
## Decision
**A stored record's size never grows with customer data, every AWS listing reads every page, and
every bound is tested at the governed estate size.**
1. An item, message body or stream record whose content grows with customer data stores only its
   count and digest inline, and keeps the full document in Amazon S3 as a claim check that every
   reader verifies. The mechanism has one owner,
   [write-discipline §2.6](../capabilities/load-path/write-discipline.md#26-identity-mapping-claim-check-amendment-2026-10-09);
   this decision makes it the rule for every such record rather than one.
2. Every AWS list or describe call reads every page.
3. A bound test measures at the governed estate size, `BULK_CAMPAIGN_COUNT`, never at a
   hand-picked size.

Decided by rubric item (1) of
[delivery practice](../delivery-practice.md#5-the-decision-weighting-rubric): a record refused
for its size drops a delivery, and a listing cut at its first page undercounts with no signal.
Item (2) chose the claim check, a known pattern that removes the size failure rather than moving
it.
## Enforced by
- Clause 2, in part: `make check-aws-pagination`, part of `make check`. Over Python under `src/`
  and `validation/`, it refuses `describe_stack_resources` and any direct list or describe call
  whose name botocore's local paginator models mark paginatable in any service, unless the call
  loops on its response token or carries a stated justification. It does not examine a bound
  method passed as a value, a manual loop that stops before its token runs out, or any AWS call
  outside those trees or in another language, such as the TypeScript infrastructure and shell
  scripts. Review owns those
  [repo: `scripts/check-aws-pagination.py`].
- Clause 3, in part: `test_publication_record_stays_within_the_item_bound_at_the_governed_campaign_estate`
  in `tests/unit/test_semantic_sidecar.py` measures the publication record at
  `BULK_CAMPAIGN_COUNT`. It does so through the helper `assert_within_bound`
  [repo: `tests/unit/scale_envelope.py`], which measures at exactly the maximum its caller passes
  and fails naming the unit and grain. The helper accepts any positive maximum, so nothing refuses
  a bound test that passes a hand-picked size; review owns that.
- Clause 1: review, plus each record's bound test under clause 3. No gate finds a new record that
  grows with customer data.

Tests that exercise clauses 2 and 3 carry `pytest.mark.covers("ADR-134")`.
## Consequences
- A record that could outgrow its bound adds an S3 write before it commits and a verified read
  after, and its readers fail closed on an absent or mismatched document.
- A listing over many pages costs more calls. A call that reads one exact target, not a listing,
  says so in its justification, which the gate lists on every run.
- Raising `BULK_CAMPAIGN_COUNT` raises every bound test that passes it, so a larger estate is
  checked without editing those tests.
## Alternatives considered
- **Raise or split the item bound.** Rejected by (1): it moves the failure to a larger customer
  instead of removing it.
- **Paginate where a cap has been hit.** Rejected by (1): the 100-resource cap failed silently, so
  the next one would be found the same way.
## Review trigger
A store whose record bound is not a fixed byte limit, or a governed estate dimension other than
campaign count that a record grows with.
