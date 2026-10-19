# ADR-097: Ship one first-party connector per source as its own stack
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner decided it on 2026-10-06. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would amend the architecture constraint "External extraction stays outside the application boundary", [ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md)'s premise that producers are owned outside this repository, and the data-contract steering's "we do not run extraction". Each stays in force until this is accepted. The account and trust rules are [ADR-098](adr-098-allow-same-account-connectors-with-one-landing-role-per-producer.md)'s.
## Context
Adlytics consumes governed landings but ships no extractor. No AMC, Vendor Central or Amazon Ads extractor exists in the repository today. Each customer would have to build and run their own producers before the product shows anything.
The Amazon Ads API is a new required source, and its read-only boundary depends on what the producer sends. The OAuth scope can't make a call read-only: the API defines no read-only scope, and the forecast operation accepts only the write-labelled one ([ADR-091](adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md) Evidence). Read-only rests on the customer's Viewer grant and a pre-send allowlist in the producer, the only two controls that can stop a write. If the producer isn't ours, the allowlist isn't tested here.
## Decision
**The repository ships one connector per source. Each is its own deployable with its own CDK stack and dependency lock, and joins the application only through the governed handoff.**
1. **Three connectors:**
   - Amazon Ads API;
   - Vendor Central over SP-API, Brand Analytics included;
   - AMC, for consistency, because no AMC extractor exists.
2. **Layout.** Each lives under `connectors/<source>/`, with its own CDK stack and its own locked dependencies. The application's `pyproject.toml` doesn't change for a connector.
3. **The handoff is the only joint.**
   - The connector writes under `incoming/<source_system>/` and writes its registration last.
   - The Finalizer validates the landing and copies the bytes unchanged into the raw zone.
   - The manifest event starts the load.

   `src/` never imports a connector or any ad-platform client, enforced by an import-linter contract.
4. **Read-only before sending.** The Amazon Ads connector reads the governed operation allowlist before every call and refuses anything off it, including any `replanningSettings`. Its pre-send tests live in the connector.
5. **Bring-your-own producers stay supported.** They use the same handoff and pass the same Finalizer checks.
6. **Built from specs and fixtures.** Connectors are built against the published API specifications and recorded fixtures. Live validation happens at first customer onboarding.
## Consequences
- A new top-level `connectors/` folder, and a dependency lock per connector.
- The architecture document's section 3 and the repository-structure document change when the first connector ships.
- The first live validation of each connector is a customer onboarding step. Until then, each connector is proven only against fixtures.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| Customers build their own producers to the published contracts | The read-only pre-send check is untested here, and every customer repeats the same work |
| One extractor inside the application | Couples the application's deployment and dependencies to every source's API client and credentials |
| **One first-party connector per source, each its own deployable and stack, joined only through the governed handoff** | **Chosen** (owner, 2026-10-06) |
## Open questions
- The Viewer-grant probe for each account type (vendor, seller, DSP advertiser) must pass before that account type is enabled.
## Evidence
- No AMC, SP-API or Amazon Ads extractor is in `src`, `infra/lib` or `scripts` [verified 2026-10-06, repository search].
- The architecture document lists "External extraction stays outside the application boundary" as a constraint [verified 2026-10-06, repository file `docs/architecture.md`, line 26].
- The authorizing user's permissions decide what an Ads API call may do [verified 2026-10-05, Amazon Ads permissions guide]. The scope facts are in [ADR-091](adr-091-show-amazon-dsp-forecasts-side-by-side-and-gate-amazon-estimates-on-a-terms-record.md)'s Evidence.
## Review trigger
A source whose connector can't be decoupled from the application, or a customer who needs a connector in another account. The second is already supported under ADR-098.
