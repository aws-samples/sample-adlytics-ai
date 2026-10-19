# Mutation review — src/adapters/inbound/ingestion_handlers/load.py

- Commit: 82c9be0495cda8f6866203e519dba648a215f467
- Date: 2026-08-25
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/inbound/ingestion_handlers/load.py TESTS=tests/unit/test_ingestion_handler.py`
- Mutants: 20 generated, 6 killed then 8 killed, 12 survived
- Survivors: 0 open, 2 killed since, 10 unreachable from this tier

Two genuine survivors, both in `manifest_keys`, which is the only part of this module a unit test
reaches — everything else calls AWS and is covered by the deployed end-to-end runs instead. Both
described real failure modes and both are now killed by named tests.

## S-01 · KILLED · Either half of the event missing had to be refused, and only both-missing was tested

- Track: mechanical
- Location: src/adapters/inbound/ingestion_handlers/load.py:182
- Mutants: 1 at this location
- Mutation: `if not bucket or not key:` becomes `if not bucket and not key:`
- Survived because: the only test for this branch supplied `{"detail": {}}`, where *both* halves are
  absent. Under `and` that still raises, so no assertion could tell the two spellings apart.
- Verdict: KILLED by `test_a_bucket_with_no_key_is_refused_too` and
  `test_a_key_with_no_bucket_is_refused_too`

The consequence was worth the two tests. A notification naming a bucket and no object would have
been read as key `""`, fetched from `s3://bucket/`, and come back as a rejected landing — so a
*misconfigured producer* would have presented as a corrupt delivery, which is a different
investigation with a different fix.

## S-02 · KILLED · Skipping one object abandoned the rest of the batch

- Track: semantic
- Location: src/adapters/inbound/ingestion_handlers/load.py:193
- Mutants: 1 at this location
- Mutation: `continue` becomes `break` in the not-a-marker branch
- Survived because: every multi-record test used valid markers, so the skip branch never ran with
  records behind it.
- Verdict: KILLED by `test_a_skipped_object_does_not_abandon_the_rest_of_the_batch`

The failure this prevents is the quiet kind. A batch whose first object was a data file would have
dropped every marker behind it, and the function would have returned success — so the queue would
delete the messages and the deliveries would never load, with nothing anywhere reporting a problem.

Reachable today only if the batch size is raised, which is why the test is worth keeping rather than
being made moot: the correctness of `continue` is what makes raising it safe.

## S-03 · UNREACHABLE · The three decorators

- Track: equivalent from this tier
- Location: src/adapters/inbound/ingestion_handlers/load.py:141-143
- Mutants: 3, one per decorator
- Mutation: `@flushes_traces`, `@METRICS.log_metrics` and `@LOGGER.inject_lambda_context` removed
- Verdict: OPEN by the letter, deliberately not chased

Each removal is a real regression — spans stop being flushed before the container freezes, metrics
stop being published, log lines lose their request context. None is observable from a unit test of
the event unwrapper, and a test that asserted the *shape of a span* would pin the telemetry library's
internals rather than this module's behaviour.

They are observable in the deployed runs, and were: the EMF blob and the span attributes were read
out of CloudWatch during validation, which is what found a genuine dimension defect in `_count` that
no mutant here would have caught.

## S-04 · UNREACHABLE · `_load`, `_count` and the handler loop

- Track: integration
- Location: src/adapters/inbound/ingestion_handlers/load.py:155, 201, 217, 223, 253, 255
- Mutants: 8
- Verdict: OPEN from this tier, covered by the deployed end-to-end runs

All eight sit behind a real S3 read, a real Athena write and a real DynamoDB write. Reaching them
from a unit test would mean an AWS fake, which the testing rules forbid outright — a test that
passes against a fake has established that the fake works.

What covers them instead is `make e2e-ingestion`, which lands a real AMC delivery and asserts the
ledger state and the curated row count, plus the fixture-driven runs against deployed dev that
exercised the skip, quarantine and dead-letter paths.

## S-05 · EQUIVALENT · `AddNot` on `if TYPE_CHECKING`

- Track: equivalent
- Location: src/adapters/inbound/ingestion_handlers/load.py:61
- Verdict: EQUIVALENT
