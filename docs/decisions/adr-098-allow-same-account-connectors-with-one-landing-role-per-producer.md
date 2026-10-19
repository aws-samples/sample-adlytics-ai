# ADR-098: Allow same-account connectors with one landing role per producer
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner decided it on 2026-10-06. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would amend [ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md) points 1, 2 and 8, which stay in force, unchanged, until this is accepted.
## Context
[ADR-097](adr-097-ship-one-first-party-connector-per-source-as-its-own-stack.md) ships connectors with the application. The owner wants them in the same AWS account as Adlytics, each as its own stack. One account is simpler to deploy, operate and pay for.
ADR-050 was written for one external producer:
- point 1 refuses same-account principals;
- point 2 gives the one landing role `s3:PutObject` on all of `incoming/*`;
- point 8 serves every source through that one role.

With several connectors, one shared role would let a compromised Ads connector write `incoming/amc/…`. Pack digests are public, so the Finalizer would accept forged AMC data, which is the curves' x and y.
## Decision
**Connectors deploy into the Adlytics account, each as its own stack. Each registered producer gets its own landing role, limited to `incoming/<source_system>/*`. IAM, not an account boundary, provides the isolation.**
1. **Point 1, amended.** A same-account principal is accepted only when it is a connector execution role that this deployment's own connector stack creates and lists in the registered-producer configuration. External principals keep today's rules: one exact role ARN with its own external ID, and no wildcard, account root or STS session ARN.
2. **Point 2, amended.** Each landing role's only data permission is `s3:PutObject` on `incoming/<source_system>/*` in the retained raw bucket. No two roles share a prefix.
3. **Point 8, amended.** There is one landing role per registered producer, with its own trust and external ID. The Finalizer refuses a registration whose declared `source_system` differs from its own key prefix. IAM guarantees only the matching role can write that prefix, so each registration ties to one role.
4. **Isolation controls inside the account:**
   - the connector's own execution role holds no Adlytics runtime, data or KMS permission beyond writing its landing path;
   - one permission boundary on every connector role denies IAM and STS changes, assuming any role but its landing role, and every Adlytics data store;
   - each connector's credentials and external ID sit in a secret that only that connector's execution role can read;
   - a CDK assertion covers each control, and the Finalizer's prefix check is unit-tested.
5. **Residual risk, stated plainly.** A compromised connector shares the account. Its blast radius is limited by IAM policies, the permission boundary and resource policies, not by an account boundary. A later mistake in any of them could expose data that a separate account would have kept out of reach. Anyone with administrative rights in the account can change connectors and application alike. The CDK assertions catch drift only in what the repository synthesises.
6. **Separate-account producers remain possible** under the unchanged external-principal rule.
## Consequences
- `IngestionStack` creates one landing role per entry in a registered-producer list, in place of one role trusting the pipeline role. Today's role becomes the AMC entry, so an existing deployment keeps its AMC trust. This is on the owner-approved `infra/` list.
- A second external ID per producer to issue and rotate, handled as the pipeline role's is today.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| A separate customer account per producer | The strongest boundary, but more deploy and operating work. Still allowed for any producer |
| Role chaining through the existing pipeline role | A compromised producer gets the pipeline role's full authority. Dropped (owner, 2026-10-06) |
| One shared landing role for every connector | Producers aren't isolated from each other's prefixes |
| **Same account; one landing role per registered producer, each limited to its own prefix** | **Chosen** (owner, 2026-10-06) |
## Evidence
- ADR-050 point 1 refuses same-account principals, point 2 grants `incoming/*`, and point 8 uses one role for every source [verified 2026-10-06, repository file `docs/decisions/adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md`, Decision points 1, 2 and 8].
- The landing role trusts exactly one principal, `landing.pipelineRoleArn`, with an `sts:ExternalId` condition [verified 2026-10-06, repository file `infra/lib/stacks/ingestion-stack.ts`, lines 345 to 348].
## Review trigger
Any connector that needs a permission beyond its landing path; a finding that bypasses the permission boundary; or a customer requirement for account-level isolation as the default.
