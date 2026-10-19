# ADR-096: Declare internal and modelled metrics in the metric dictionary
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The response-curve and forecasting design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
The simulator shows figures that aren't measurements: modelled attributed revenue over a horizon, the spend it is modelled on, the change under a proposed plan, and the modelled ROAS of each. It also needs measures that only the pipeline should see, such as `pacing_spend` and Ads API traffic-date sales.
The repository's rule is "one fact, one home": every metric is declared in `contracts/shared/metric-dictionary.yaml`, and code reads the definition rather than restating it. But today every entry reaches three consumers (curation precompute, the agent tools and the Context Engine), and no key hides an entry from them. A modelled figure also has no time class that fits it, because it describes a horizon, not a measured day.
## Decision
**The metric dictionary gains an `exposure` key and a `modelled_metrics:` section, and every consumer filters on them.**
1. **`exposure ∈ {governed, internal}`.** The default is `governed`, so no existing entry changes. The catalog tool, the measurement tool, the Context Engine and curation precompute each skip `internal` entries, with a test per consumer. `pacing_spend` and the Ads API traffic-date measures are `internal`.
2. **`modelled_metrics:`.** Entries carry `source: scenario_domain`, `measurement_basis: [modelled]`, a `models_metric` link to the measured metric they model (for example AMC 14-day attributed revenue), and their own `horizon_class`. The shared fact time class isn't extended.
3. **Operands and ratios.**
   - Operands: `attributed_revenue_14d_modelled`, `ad_spend_curve_basis_modelled` (fee-inclusive, the [ADR-017](adr-017-roas-definition.md) basis) and their changes.
   - Ratios: `roas_attributed_14d_modelled` and `return_on_change_attributed_14d_modelled`.

   Each ratio is declared as numerator and denominator, as today's ratios are.
4. **Observed figures** the simulator shows reuse the existing `roas_attributed_14d` and `ntb_revenue_share`.
## Consequences
- This changes what every agent consumer can see, so it is decided against [ADR-032](adr-032-tools-are-shaped-by-operation-not-by-metric.md) (tools shaped by operation) and [ADR-062](adr-062-route-claims-by-semantic-authority.md) (semantic authority).
- An agent can't answer a question with a modelled figure in v1. Agent-run scenarios are out of scope.
- A new consumer of the dictionary must implement the `exposure` filter, or its test fails.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Define modelled figures in the simulator's code | Breaks "never put a metric definition in code" |
| Add them as ordinary entries | The agent and precompute would treat modelled figures and internal inputs as governed measurements |
| Extend the shared fact time class with a horizon kind | Changes the measured-fact model for a non-measured figure |
| **An `exposure` key, and a separate `modelled_metrics` section** | **Chosen** |
## Evidence
- The dictionary header names curation precompute, the tools and the Context Engine as consumers [verified 2026-10-06, repository file `contracts/shared/metric-dictionary.yaml`, lines 3 to 5]. No key in `_METRIC_KEYS` hides an entry [verified 2026-10-07, repository file `src/adapters/outbound/metric_dictionary.py`, line 244].
- The dictionary declares a ratio as numerator and denominator with a `measurement_basis` list [verified 2026-10-06, `metric-dictionary.yaml`, lines 953 to 969].
## Review trigger
An agent use case for modelled figures, or a second internal-only consumer.
