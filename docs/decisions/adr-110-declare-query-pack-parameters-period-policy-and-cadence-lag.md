# ADR-110: Declare query-pack parameters, period policy and cadence lag, and check them at the Finalizer

Date: 2026-10-06
Status: Proposed. The owner approved the business-outcomes design on 2026-10-06. Implemented in the
repository (2026-10-08) in the pack model, the producer planner and the Finalizer; no pack declares a
non-default value yet, so every existing pack digest is unchanged. Not deployed.
Amends, once accepted, point 5 of
[ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md) (what the Finalizer
validates), coordinated with the forecasting design's own ADR-050 amendment. The current design is in
[Business outcome measurement](../concepts/business-outcome-measurement.md).

## Context

The new shopper packs need four things today's query-pack model doesn't declare.

- **Periods.** Week, month and quarter deliveries of one pack overlap in time, and the load rejects a
  delivery whose period intersects another for the same contract and option set
  (`src/domain/curation/assertions.py:574`).
- **Parameters.** A cross-source shopper set needs one marketplace on both sides, but DSP filters on
  `campaign_country` and sponsored ads on `marketplace_name`. The AMC steering says to use governed query-pack
  parameters and never string-template SQL. Today's option set holds only execution options and the window
  (`src/domain/producer/planning.py:84-88`).
- **Latency.** Some packs land 16 days after period end, others 2. Pending-versus-missing states need to
  know which ([ADR-107](adr-107-derive-paid-feature-availability-from-delivery-evidence.md)).
- **Suppression and bar.** The boolean `rejectSuppressedRows` (`src/domain/producer/models.py:88`) can't say
  "discard withheld rows", and nothing declares the expected privacy bar
  ([ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md)).

## Decision

**A governed query pack may declare `inputParameters`, `periodPolicy`, `cadenceLagDays`, `suppressionPolicy`
and `expectedBar`. Every field is optional with a stated default, and the Finalizer checks them.**

| Field | Default when absent |
|---|---|
| `periodPolicy` (grains and alignment: ISO week, calendar month, calendar quarter) | today's window behaviour; the pack is daily |
| `cadenceLagDays` | 2; required when `accessTier = paid_feature` |
| `inputParameters` | none |
| `suppressionPolicy` (`reject_delivery` or `discard_suppressed_rows`) | from `rejectSuppressedRows`: `true` reads as `reject_delivery` |
| `expectedBar` (2 or 100) | 2 |

1. **Period grain joins the option set,** so week and month deliveries are separate option sets and the
   overlap check passes. The Finalizer rejects a registration whose period doesn't align to the declared
   grain.
2. **Parameters resolve from governed mappings.** The adapter loads `contracts/shared/commerce-identities.yaml`
   and passes the resolved values in `PlanLandingRequest.commerce_identities`; the domain planner never reads
   a file. The values join the option set. The Finalizer re-resolves them and rejects a mismatch.
3. **No list-valued filter varies between runs,** so the differencing guard never sees a changing filter.
4. Existing packs, and the forecasting design's new daily AMC packs, take the defaults and don't change.
5. **Each field joins the pack digest only when it differs from its default,** so declaring a default
   (the paid `amc-retail-purchases` pack's `cadenceLagDays: 2`) leaves the digest, and the installed
   governance every landed manifest is checked against, unchanged.
6. **Alignment is a property of the grain.** `periodPolicy` declares only `grains`: a week is an ISO
   week, a month a calendar month, a quarter a calendar quarter, all on the UTC day. Each grain has one
   alignment, so a second declaration could only disagree with it.
7. **A parameter declares its AMC `dataType` and the `commerceIdentity` namespace that fills it.** The
   Finalizer resolves the registration's marketplace through the bare-value aliases, as the load
   resolves a manifest's, then reads the namespace's one value for that marketplace on the period's
   first and last day. Query-parameter namespaces are never matched by bare value. A parameter derived
   from the period (an ASI snapshot bound, `as_of_month`) adds a source kind with the first pack that
   needs it.

## Consequences

- One model change can carry these five fields and the forecasting design's two (`source_scope_namespace`,
  `watermark_policy`), with one schema version bump. Whichever design lands second rebases. These five
  landed first, with no bump: the generated `producer-packs.json` is byte-identical because no pack
  declares a non-default value. The one bump went with the first change that altered the projection:
  the first shopper pack, `amc-new-to-brand-buyers-platform` (2026-10-09), moved `producer-packs.json`
  to `schemaVersion` 2. Forecasting's fields join version 2.
- A new commerce-identity namespace, `amc.segment_marketplace_id`, is needed for the paid audience packs
  ([ADR-103](adr-103-model-audience-segments-as-a-source-namespaced-overlapping-dimension.md)).
- The registration option set carries every option, but an AMC publication lens hashes only the
  family-naming ones, `period_grain` and `pack_variant` (`AMC_LENS_OPTIONS`, `src/domain/semantics/publication.py`).
  Execution options are fixed by the pack, the window is the delivery's own bucket, and each parameter is
  the governed value for the population that the Finalizer re-checks, so none splits a family. A unit test
  fails on any registration option outside those classes until it gets a lens decision (2026-10-09).
- A pack may declare pre-built **variants** (2026-10-11, wave 2b plan D9 and D10): an optional `variants`
  list, each `{variant, packVersion, queryFile, outputColumns, trigger}`, default none, so every existing
  pack is unchanged. A variant runs under its own pack version, its id joins the registration option set
  as `pack_variant` (so its rows are their own publication family and a cross-variant comparison is
  refused), and the Finalizer resolves its binding by pack id and version across current, retained and
  variant bindings. `trigger: manual` runs only when a registration names it; `degrade_next_period`
  switches on, and stays on until Step B, from the period after a delivery whose graded loss passed the
  degrade threshold. The load decides which one a period expects from the previous period's published
  cell-loss evidence (`domain.producer.variants.select_pack_variant`) and quarantines any other with
  `PACK_VARIANT_MISMATCH`. Pack 5's touch-only variant is the first (`manual`, off). An automatic switch
  holds across a gap in publications: any earlier period under the variant keeps it due, else the latest
  earlier base period's grade decides, and a state the load can't read accepts nothing. Serving reads a
  period from the one series holding its CURRENT publication; a period two series hold, or a comparison
  across series, is refused with `PACK_VARIANT_MISMATCH`, and a plan pinning a variant's row values (pack
  5's `row_kind = exposure` or `outcome`, declared in the contract's `expected_cells`) binds that variant's
  lens (2026-10-11).
- The cohort packs (14 and 15, [ADR-104](adr-104-define-acquisition-cohorts-by-amazons-new-to-brand-flag.md),
  2026-10-11) add the period-derived source kind point 7 anticipated and one grain. `periodPolicy` may
  declare `custom`, a cohort vintage: the 13 whole calendar months ending with its `as_of_month`, and a
  pack's only grain. An input parameter takes exactly one of `commerceIdentity` and `periodValue`;
  `periodValue: as_of_month` fills the parameter from the registration's own period as
  `YYYY-MM-01 00:00:00`, the form the 2026-10-09 probes cast to a TIMESTAMP, needs grains `[custom]`, and is
  re-derived by the Finalizer. Pack 15 reads the ARP marketplace from a new query-parameter namespace,
  `amc.arp_marketplace_name`, pinned to the registry's `AMAZON.COM` form.

## Alternatives considered

- **Rolling 90-day windows.** Overlap within one option set, so the load rejects them.
- **Template the marketplace into SQL.** Forbidden by the AMC contract steering, and the query digest would
  vary per marketplace.
- **Required fields.** Would force a change to every existing pack and to the forecasting design's packs.

## Review trigger

Reopen when a pack needs a list-valued parameter, or when AMC changes its availability SLA.
