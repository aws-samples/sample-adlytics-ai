# Publication State — low-level design

## 6. Interfaces and contracts

| Interface | Shape |
|---|---|
| Attempt event | Immutable attempt identity, event identity, and typed outcome evidence. |
| Lease | Canonical scope or sibling set plus holder/expiry, returning a monotonic fence or refusal. |
| Success record | Immutable publication identity, validation evidence, exact buckets, and byte identity. |
| `CURRENT` transition | Governed scope set, immutable successes, and current fence in one all-or-none condition. |
| Current resolution | Required scopes/buckets to an attested fixed set and exact normalized coverage, or refusal. |

## 7. Concepts applied

- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md)
- [Provenance Chain](../../concepts/provenance-chain.md)
- [Semantic Accuracy](../../concepts/semantic-accuracy.md)
- [Slowly Changing Dimensions](../../concepts/slowly-changing-dimensions.md)

## 8. Decisions

- [ADR-045](../../decisions/adr-045-separate-delivery-commerce-marketplace-and-product-identities.md)
- [ADR-047](../../decisions/adr-047-separate-attempts-leases-publications-and-coverage.md)
- [ADR-049](../../decisions/adr-049-make-validation-and-grounded-narration-part-of-publication-accuracy.md)
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)

## 9. Risks and open questions

- A discovery index may lag; strong base-selector reads remain authoritative.
- A failed pointer transition may leave immutable state unreachable but cannot expose it for serving.
- An older successful publication may remain current after a newer attempt fails by design.
- Query Path decides how an exact coverage hole is refused or qualified.
