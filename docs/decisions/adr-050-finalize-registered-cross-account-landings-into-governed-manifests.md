# ADR-050: Finalize registered cross-account landings into governed manifests

Date: 2026-08-24
Status: Accepted

> **Proposed amendments, 2026-10-07.** [ADR-089](adr-089-onboard-api-sources-through-governed-operation-packs-and-resolve-campaigns-at-read-time.md) (Proposed) would amend points 3, 5 and 7 for governed API operation packs: one result object or the listed parts of one delivered report, field-policy validation with source rules as pack data, and operation, request and terms digests in the manifest. [ADR-098](adr-098-allow-same-account-connectors-with-one-landing-role-per-producer.md) (Proposed) would amend points 1, 2 and 8: same-account connector principals, one landing role per registered producer limited to its own `incoming/<source_system>/*` prefix, and a Finalizer prefix check. [ADR-097](adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md) (Proposed) would change the premise that producers are owned outside this repository. This decision stays in force, unchanged, until they are accepted.
> **Proposed amendment, 2026-10-06.** [ADR-110](adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) (Proposed) would extend point 5: the Finalizer also checks period alignment and governed input parameters. It's coordinated with the forecasting design's own amendment of this ADR. This decision stays in force, unchanged, until ADR-110 is accepted.

> **Proposed amendment, 2026-10-06.** [ADR-110](adr-110-declare-query-pack-parameters-period-policy-and-cadence-lag.md) (Proposed) would extend point 5: the Finalizer also checks period alignment and governed input parameters. It's coordinated with the forecasting design's own amendment of this ADR. This decision stays in force, unchanged, until ADR-110 is accepted.

## Context

Adlytics AI is deployed once per customer in that customer's AWS account. The extraction pipeline runs in a different AWS account and is owned outside this repository. One customer pipeline must be able to deliver every supported source without assuming a different destination role for each query pack or dataset.

Semantic V2 does not begin with an arbitrary uploaded file. It begins with one immutable, VersionId-bound source object and a governed `manifest.json` written last at the canonical raw prefix. Requiring the external pipeline to reproduce canonical key construction, header discovery, checksums, source-pack binding, query-pack digests, object copying and manifest ordering would make correctness depend on a second implementation outside the product boundary.

The access topology and completion protocol therefore need to reduce producer work without giving the external pipeline authority over governed manifests or downstream services.

## Decision

**Producer onboarding is false by default. Every deployment keeps the canonical-manifest Semantic V2 ingestion core; only an explicitly enabled deployment creates one Adlytics-owned landing role that the customer's exact external pipeline role may assume and the resources that finalize a registration into the canonical manifest-bound delivery.**

`ADLYTICS_PRODUCER_ONBOARDING_ENABLED` accepts only exact `true` or `false` and is `false` when absent. Disabled means no producer trust, producer IAM, registration resources, Finalizer, producer alarms, producer parameters, or producer outputs. The manifest event, ingestion queue/dead-letter queue, load function, core alarms and alarm topic remain.

1. When onboarding is enabled, the trust policy names one exact external IAM role ARN and requires one deployment-specific external ID. Those two values are required only in the enabled configuration. Configuration refuses wildcards, account-root principals, STS session ARNs, same-account principals and missing values.
2. The assumed role can call only `s3:PutObject` under `incoming/*` in the retained raw bucket. It cannot read, list, overwrite governed prefixes, delete objects, publish a canonical manifest, send internal queue messages, or access DynamoDB, Glue or Athena.
3. The external pipeline writes one AMC result at `incoming/<source-system>/<source-dataset>/<execution-id>/result.csv`, then writes `registration.json` at the same prefix last. The registration binds the exact result VersionId and the governed producer artifact that ran.
4. S3 object events route only `incoming/*/registration.json` through a registration-specific EventBridge rule and internal standard SQS queue with its own dead-letter queue.
5. A separate Finalizer Lambda reads the exact registration VersionId from the event and the exact result VersionId from the registration. It validates source identity, explicit window and options, installed pack identity, query digest and result shape before creating governed output.
6. The Finalizer copies the exact result bytes unchanged into the existing canonical raw prefix, binds the destination VersionId, and writes `manifest.json` last. The existing Semantic V2 manifest event, ingestion queue, load function, publication gates and `CURRENT` authority remain unchanged.
7. The generated manifest records the installed query-pack, query, data-contract and source-pack digests. The external pipeline declares only the query-pack identity/version and the digest of the SQL it actually ran; Adlytics resolves and binds the other governed artifacts.
8. One landing role serves every registered source and query pack in the deployment. Source and pack admission is enforced by Finalizer validation, not by multiplying IAM roles or treating session tags as authorization.
9. Finalization state is operational and destroyable with `IngestionStack`. It is keyed by exact registration key and VersionId and records terminal rejection or the exact generated manifest and result versions. It is not publication authority and cannot make data readable.
10. Direct canonical-manifest landing remains an advanced operator path for trusted in-account tooling. The cross-account landing role cannot use it.

## Consequences

The producer owns extraction, AMC authentication, workflow scheduling, polling and result download. It does not own Adlytics manifests, internal messaging, semantic publication or freshness authority.

A valid registration can be retried safely because every read is VersionId-bound and every generated object is immutable. A duplicate event converges on the recorded exact output. A changed registration or result is a new S3 version and therefore a distinct attempt rather than an overwrite hidden behind a key.

Invalid source metadata, an unknown or stale pack, a query-digest mismatch, a missing VersionId, an unexpected header, or a misplaced object is a terminal registration rejection. Dependency failures remain retryable queue work. Neither outcome advances Semantic V2 `CURRENT`.

`IngestionStack` always owns the rebuildable canonical-manifest rule, queue/dead-letter queue, Semantic V2 load function, core alarms and alarm topic. When producer onboarding is enabled, the same destroyable stack also owns the landing role, registration rule, queue/dead-letter queue, Finalizer, operational status table and producer alarms. `DataV2Stack` continues to own the retained raw bucket and analytical authority.

Disabling onboarding after an enabled deployment proposes deletion of every destroyable producer resource. Queued registrations and operational status state are deleted with those resources; landed raw objects remain because the retained raw bucket belongs to `DataV2Stack`. A diff must therefore be reviewed before changing an enabled deployment to disabled.

When enabled, the role ARN, raw bucket name and `incoming/` prefix are producer onboarding outputs. The exact external pipeline role ARN and deployment-specific external ID are conditionally required inputs with no defaults; the external ID is exchanged with the producer out of band and is not published by the stack.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| One destination role per source, producer or query pack | One customer pipeline would need role-selection logic and repeated trust/IAM operations in a single-tenant deployment; semantic admission already has a governed registry |
| Grant the external role directly in the bucket policy | Spreads producer trust into retained storage policy and gives up the destination-owned session boundary used for revocation and audit |
| Let the producer write canonical manifests | Duplicates canonical key, checksum, header, digest and VersionId rules outside Adlytics and grants the external pipeline authority to trigger ingestion directly |
| Send cross-account SQS messages | Adds queue policy, region and message-contract surface while S3 already supplies a versioned completion event inside the destination account |
| Infer a delivery from a bare CSV | Dataset, window, options, workflow and governance identity cannot be inferred safely from bytes or a filename |
| One incoming role plus session-tag authorization | Session identity is useful audit evidence, but producer-controlled tags are not a substitute for governed pack validation |
| Reuse the Semantic V2 ingestion queue and Lambda for registrations | Registration finalization and semantic publication have different IAM, retry, state and failure vocabularies; collocation would give both functions the union |

## Review trigger

Revisit if the deployment becomes multi-tenant, one producer must be isolated from another producer in the same customer account, a source cannot produce one bounded result object, or S3 ceases to provide the immutable VersionId evidence the protocol requires.
