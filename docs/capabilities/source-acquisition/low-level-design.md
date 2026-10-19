# Source Acquisition — low-level design

## 6. Interfaces and contracts

| Interface | Contract |
|---|---|
| Producer onboarding | Explicit flag, exact external role ARN, and external ID; no trust resources exist while disabled. |
| Governed pack | Versioned metadata, one SQL statement, bindings, expected header, size limit, and deterministic digests. Optional [ADR-110](../../decisions/adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) fields, each with a default and each in the digest only when it differs from that default: `suppressionPolicy` (`rejectSuppressedRows: true` reads as `reject_delivery`), `periodPolicy.grains` (`week`, `month`, `quarter`, or `custom` alone for a cohort vintage), `cadenceLagDays` (2; required on a paid-feature pack), `inputParameters` (name → `dataType` and either a `commerceIdentity` namespace or `periodValue: as_of_month`, derived from the registration's period) and `expectedBar` (2 or 100). Optional `retainedVersions`: the exact bindings (`packVersion`, `packDigest`, `queryDigest`, `contractDigest`, `sourcePackDigest`) of earlier versions of the pack, recorded when a version is superseded, so deliveries those versions produced still load, re-land and reprocess. It isn't in the digest, because it says what the load accepts, not what the producer runs; the producer only ever runs the current version. Optional `variants` ([ADR-110](../../decisions/adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) consequences): pre-built alternatives, each `{variant, packVersion, queryFile, outputColumns, trigger}` with `trigger` `manual` or `degrade_next_period`; a registration naming one carries `pack_variant` in its option set, the Finalizer binds it by pack id and version, `producer describe` lists it, and the load accepts only the variant the period expects (`PACK_VARIANT_MISMATCH` otherwise). Default none. `installed_governance` (`src/adapters/producer/packs.py`) is the one set of current and retained bindings that the load, ingest and reprocess composition roots check a manifest against. A pack whose contract maps a column `present_from_pack_version` later than the pack's own version is refused ([Load path, pack-versioned columns](../load-path/low-level-design.md#pack-versioned-columns)). |
| Registration options | The pack's execution options and window bounds, plus `period_grain` for a periodic pack and one entry per input parameter. The producer planner copies parameter values from `PlanLandingRequest.commerce_identities`, which the adapter resolves from `contracts/shared/commerce-identities.yaml`. |
| Finalizer checks (ADR-110) | A periodic registration names a declared grain (`REGISTRATION_PERIOD_GRAIN`) and covers exactly one ISO week, calendar month or calendar quarter in UTC (`REGISTRATION_PERIOD_ALIGNMENT`). Each parameter equals the value the Finalizer re-resolves from the same bridge (`REGISTRATION_PARAMETER`); no single governed value refuses it (`REGISTRATION_PARAMETER_UNRESOLVED`). The pack check binds declared parameters to the SQL's `CUSTOM_PARAMETER` references both ways (`PACK_PARAMETER_UNDECLARED`, `PACK_PARAMETER_UNUSED`). |
| Incoming landing | One result object followed by a final registration object under [Source Acquisition](README.md). |
| Finalized delivery | Unchanged destination bytes, exact destination VersionId, and canonical manifest written last. |
| Status | Exact attempt identity and typed terminal/finalized outcome; never analytical authority. |

## 7. Concepts applied

- [Data-Flow Completion and Service Levels](../../concepts/data-flow-completion-and-service-levels.md) — registration and manifest milestones use the shared clock and outcome semantics.
- [Provenance Chain](../../concepts/provenance-chain.md) — immutable evidence and manifest-last completion.
- [Semantic Accuracy](../../concepts/semantic-accuracy.md) — source evidence precedes publication authority.
- [Observability](../../concepts/observability.md) — bounded operational reporting.

## 8. Decisions

- [ADR-030](../../decisions/adr-030-the-raw-zone-landing-contract.md)
- [ADR-043](../../decisions/adr-043-a-landed-manifest-is-the-trigger-and-serialisation-is-the-correctness-requirement.md)
- [ADR-050](../../decisions/adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md)
- [ADR-074](../../decisions/adr-074-define-authority-backed-data-flow-completion-and-service-level-objectives.md)
- [ADR-110](../../decisions/adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) (Proposed; implemented in the model, planner and Finalizer)

## 9. Risks and open questions

- Producer activation awaits the customer-supplied external role and external ID.
- Disabling an enabled producer path removes only destroyable transport and status resources; retained source objects remain.
- Source-specific multipart behavior must be declared by the governed pack, never inferred.
- Cross-region producers must target the destination region explicitly.
