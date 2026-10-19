# Saturation — low-level design

## 6. Interfaces and contracts

The domain fit/evaluation contract receives already-authorized history and returns typed curve parameters, uncertainty, diagnostic state, and a bounded serving outcome. It imports no AWS SDK. Storage and serving must preserve fit identity, input provenance, diagnostics, and current-pointer state.

## 7. Concepts applied

- [Data Layer](../data-layer/README.md)
- [Publication State](../publication-state/README.md)
- [Observability](../../concepts/observability.md)

## 8. Decisions

No implementation decision is accepted. A future package/model compatibility spike and artifact contract are prerequisites.

## 9. Risks and open questions

- Causal allocation claims require a separate identification contract.
- Fit convergence, history eligibility, uncertainty communication, and acceptance criteria remain unapproved.
