# ADR-158: Store demand forecasts as immutable generations under one fenced CURRENT

Date: 2026-11-11
Status: Accepted (2026-11-11), approval DR-7. Built in dev by slice 2 of the demand forecast, and
read by slice 3's read API; not yet deployed outside dev or live-accepted. For this solution it
amends the [forecasting LLD](../capabilities/forecasting/low-level-design.md)'s per-series pointer.

## Context

The page list, the four chat tools and a product comparison need one consistent roster from one run.
A run that fails or is held must leave the last good forecast readable. Two runs, a retry and an
operator rollback can race for the same pointer.

## Decision

1. **One pointer per forecast set.** A forecast set is one marketplace, distributor view and selling
   program. It has one `CURRENT` item. A generation is the whole accepted output of one run, every
   product and target included, the withheld ones with their reasons.
2. **Generations are immutable.** Each product's `ASIN#` item, each precomputed list `PAGE#` item and
   the generation's `META` are written once with `attribute_not_exists` conditions, `META` last. A
   generation is bound to one job attempt per shard by its identity.
3. **The pointer and its receipt change together.** Every change to `CURRENT` is one
   `TransactWriteItems` call with its `RUN#` receipt and, on publish, a check of the generation's
   digest. A fence on `CURRENT` refuses a stale writer, which records `superseded` instead.
4. **Rollback is fenced too.** `make demand-forecast-rollback` points `CURRENT` back at a
   once-published generation through the same transaction and fence, with an operator reason.
5. **Each outcome carries a retry disposition.** A deterministic outcome (a quality hold, an
   artifact mismatch) is `suppress`, so the next daily check does not re-run the same input; any
   other failure is `retry`. `src/domain/forecasting/publish.py` owns the outcomes and dispositions.
6. **Readers read through `ForecastReaderPort`**, which has no write method
   (`src/ports/outbound/forecast_store.py`). Every item a read uses is keyed by the generation one
   strongly consistent `CURRENT` read names ([ADR-163](adr-163-serve-demand-forecasts-and-status-through-a-read-api.md)).

## Consequences

- A failed or held run leaves the last generation current, shown with its age and the cause.
- No pointer change exists without its receipt, and no receipt describes a pointer state that did
  not happen.
- Generations are retained; the table has point-in-time recovery and deletion protection on.

## Alternatives

- One pointer per series (the LLD's form): one run could leave products from different runs on one
  page, and adds a stale state per product that a weekly 13-week outlook doesn't need.
- Overwrite the forecast in place: a failed run could leave a partial forecast, and nothing to roll
  back to.
