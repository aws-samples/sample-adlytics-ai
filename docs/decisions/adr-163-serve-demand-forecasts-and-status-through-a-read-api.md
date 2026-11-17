# ADR-163: Serve demand forecasts and their status through a read API

Date: 2026-11-17

Status: Accepted (2026-11-17), owner decision OQ-30. Built by slice 3 of the demand forecast; not yet
deployed or live-accepted. Amends [ADR-115](adr-115-organise-the-app-into-customer-question-hubs-from-one-navigation-file.md)
(a customer tab served by a read API).

## Context

The weekly demand forecast publishes an immutable generation and a retained `CURRENT` pointer in one
DynamoDB table. Planners need a page, the assistant needs tools, and later external agents need the
same figures. Baked presentation artifacts would serve only the page, and the owner asked for one API
that the page, the tools and later agents share. Nothing may be computed at read time: every figure
served must be one the publisher stored.

## Decision

1. **Two read-only routes on the existing authenticated HTTP API.** `GET /api/demand-forecast` returns
   one precomputed page of the ranked roster, or one product's detail when `asin` is given.
   `GET /api/demand-forecast/status` returns the current generation, freshness, the last forecast run,
   any hold or rollback, and the next check times. Both sit behind the session authorizer and are
   open to any signed-in user, like every page route; neither is under `/api/operations/`.
2. **One read service.** The HTTP function and the forecast Gateway tool function run the same
   `ForecastReadService` over a reader port that has no write method. Every item it reads is keyed by
   the generation named in one strongly consistent `CURRENT` read, in at most two rounds.
3. **Each product's item stores its list row.** The publisher already builds one unranked roster row
   per product; it now stores that row in the product's `ASIN#` item too, so the detail and the tools
   read the stored 4-, 8- and 13-week p50 totals instead of summing bands. An item without the row
   (a generation published before slice 3) is refused `FORECAST_STORE_INCOMPLETE`; there is no
   fallback sum.
4. **Nothing computed.** The service returns stored values. Its only arithmetic is server display
   rounding of one stored value ([ADR-084](adr-084-display-monetary-values-at-currency-scale-from-a-server-display-value.md)):
   a whole number for unit targets, two places for money targets. Status facts (age, next checks,
   hold) come from the pointer, the alarm's own age function and the check schedule.
5. **Freshness.** A generation is stale when its age passes `stale_after_days` (the alarm's
   threshold), or the last forecast run failed or was held. Each cause is listed.
6. **Tools never page.** `forecast_list_asins` returns page 1 with `match_count`, `page_count` and
   `truncated`, and finds any product by exact ASIN. Paging stays on the page and the API.
7. **Every failure has a forecast code.** Each entrypoint ends with a boundary that maps any other
   exception to `FORECAST_READ_FAILED`; a request deadline maps a slow store to
   `FORECAST_STORE_UNAVAILABLE`; a malformed tool invocation returns a typed refusal.
8. **Writes stay operator commands.** Re-run and rollback remain `make demand-forecast-run` and
   `make demand-forecast-rollback`. MCP exposure is a later decision.
9. **A new navigation kind.** `navigation.yaml` gains `api_routes`: a customer tab with no artifact,
   whose page calls the read API.

## Rejected options

- **Baked presentation artifacts for the page** (the slice plan before OQ-30): the tools and later
  agents could not share them, and the page would join the bake roster it does not need.
- **Summing bands at read time** (`roster_entry()` in the reader): a figure computed in the API.
- **Recording the latest outcome of every daily check on `CURRENT`** (`last_outcome`): status would
  show no-ops and input-not-ready holds, but it changes the publisher's outcome transaction, which
  slice 2 owns and is accepting on dev. Status reports `last_attempt`, the last forecast run.
- **Tools calling the HTTP API:** the tool function holds no browser session, so this needs a second
  credential path.
- **Signed cursors for tool paging:** a MAC key and turn state for one tool.
- **An entitlement for the status route:** planners would not see why a forecast is stale.

## Consequences

- The page, the assistant and later agents see the same figures from the same reads.
- A rollback to a generation published before slice 3 serves the list but refuses the detail and the
  per-product tools until a slice-3 generation is published.
- Two more Lambda functions (HTTP and tool), each with `dynamodb:GetItem` on the `SET#` and `GEN#`
  leading keys of the forecast table only.
- Designs and limits are owned by the [forecasting low-level design](../capabilities/forecasting/low-level-design.md#read-api).
