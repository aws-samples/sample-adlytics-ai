---
inclusion: fileMatch
fileMatchPattern: ["src/adapters/jobs/fixture_generation/**", "src/adapters/jobs/land_fixtures/**", "contracts/shared/synthetic-entity-catalog.yaml"]
description: Governs fixture generation and dev-lake seeding — delivery-shaped output, declared scenario shapes, landing path, and immutability — when editing the fixture generator, the landing job, or the synthetic entity catalog.
---

# Fixture generation and dev seeding

Strategy, tiers, and rationale: docs/concepts/dev-seeding.md. Provenance classes
and identifier rules are owned by ADR-051 and meta-testing-strategy; never
restate them here.

- Generate source deliveries, never canonical rows. Seed data enters only
  through the raw landing and event-driven ingestion path a real producer uses;
  never write directly to Iceberg, the canonical zones, the publications table,
  or the artifact store.
- Every generated value comes from a scenario shape declared in the synthetic
  entity catalog. Never emit a random or undeclared value.
- Never emit a row set that violates the shape invariants owned by
  docs/concepts/dev-seeding.md §2 (every-day coverage, identity pairing,
  ratio ranges).
- A structural gap between a fixture and its documented source grammar is a
  generator defect, fixed at the generator with the source doc cited — never
  accommodated in an ingestion or contract change.
- A new delivery is immutable: advance the pinned retrieved_at revision and
  land plus reprocess under a new operation id. Never mutate a landed fixture
  delivery in place.
