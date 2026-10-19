# ADR-089: Onboard API sources through governed operation packs, and resolve campaigns at read time
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. The owner chose the identity model below on 2026-10-06. The rest of the response-curve and forecasting design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would amend [ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md) points 3, 5 and 7, and refine [ADR-053](adr-053-resolve-campaign-identity-and-hierarchy-before-measurement.md). Both stay in force, unchanged, until this is accepted. The trust points of ADR-050 are [ADR-098](adr-098-allow-same-account-connectors-with-one-landing-role-per-producer.md)'s.
## Context
The Amazon Ads API is a new required source (owner, 2026-10-06). The owner asked for no special path: a versioned contract, landing in the raw zone, and event-driven ingestion like every other source. But Source Acquisition knows only AMC SQL today:
- a pack needs SQL, a query digest and expected columns;
- the Finalizer and the pack generator hard-code two AMC-only rules;
- ADR-050 lands one `result.csv`.

Ads API results are JSON, often nested, and reports arrive as gzip parts. Amazon adds fields without notice.
The Ads API also speaks a different campaign ID from the one our AMC contracts key on. On AMC `sponsored_ads_traffic`, `campaign_id` is the Ads API ID and `campaign_id_string` is the console ID, and `campaign_key` is built from the console ID. So an Ads API row hashed the usual way would make a second, different campaign.

This ADR records two linked choices: how API sources land, and how their campaigns resolve. They are accepted or reversed together. The landing rule decides that Ads API packs carry campaign references rather than keys, and read-time resolution is what makes those references usable.
## Decision
**API sources land through governed operation packs on the existing Finalizer, raw zone and load path. Ads API rows keep their own campaign ID and are matched to AMC campaigns at read time.**
1. **Operation packs.** A `GovernedApiOperationPack` sits beside `GovernedQueryPack`, generated from `contracts/<source_system>/operations/*.yaml`. It carries:
   - its source-pack identity;
   - operation, request-template and response-schema digests;
   - `required_fields`;
   - a field policy, `additive` by default;
   - `source_scope_namespace` and `watermark_policy`.

   Source rules become pack data, so the Finalizer's and generator's `source_system == "amc"` branches are removed. AMC's behaviour is unchanged.
2. **Landing (amends ADR-050 point 3).**
   - There is one landing per operation, source scope and capture run, so landings don't grow with the campaign count.
   - Reports and exports land exactly as delivered, as their listed parts.
   - List and query results land as one JSON-lines object, with per-call and per-page digests.
   - A version-3 registration carries exactly one of a query pack or an operation pack.
3. **Finalizer checks (amends point 5).** The allowlist and the optional terms record resolve per `source_system`. The Finalizer refuses a missing required path. An unknown path is accepted, recorded and alarmed, never a rejection. `exact` field sets are allowed only with a cited fixture.
4. **Manifest digests (amends point 7).** The manifest adds the operation, request-template, request-parameter and terms digests beside the source-pack digest.
5. **Readers.**
   - Two new `SourceFormat` values, `json_records` and `json_records_gzip`, under the rule "a new reader value, never a pipeline".
   - One canonical path notation: dot-separated, with `[]` after every array segment. The validator refuses the other form.
   - A `record_path` is a chain of nested arrays. Ancestor fields are addressable by prefix.
   - Drift and `exact` apply to every unknown path, scalars included.
6. **Marketplace from the delivery.** An identity field may read `from_delivery: marketplace`, resolved by a static country alias.
7. **Campaign references, not identities.** Ads API packs map the campaign ID as a `source_campaign_ref` (namespace, ID, qualified source scope) and derive no `campaign_key`. `amc-campaign-sponsored` also maps AMC's `campaign_id`, cast to a string, as the alias `amazon_ads.campaign_id`. It is never a join key.
8. **Read-time resolution (refines ADR-053).** The domain functions `resolve_campaign` and `resolve_scope` run where data is read: the input builder, the snapshot publisher and the semantic views.
   - A reference resolves only on two-sided evidence: the AMC alias carries the ID, and the same profile's own current campaign listing carries it.
   - The profile is recorded as the alias's source account.
   - A many-to-one in either direction is a `campaign_conflict`, never merged.
   - Unmatched and conflicting rows are counted on the status plane, never dropped silently.
   - Arrival order doesn't matter, and nothing is held or recompiled.
9. **DSP advertiser scopes** come from a new `ads-accounts` contract, so DSP rows can resolve their scope.
10. **Declared write sets.** Every new contract declares the tables it writes and its dependencies (control groups, predecessors). That feeds the planned ingestion release design's disjoint-table waves and release fence, which isn't in this repository yet. No design here assumes parallel writes within one physical table.
## Consequences
- A second API source needs a contract folder, operation contracts, an allowlist and registry rows. It needs no Finalizer or loader code change, and a code search asserts there's no `source_system ==` literal.
- A campaign that never served in the AMC window has no alias. It reads as unmatched until AMC shows it, and has no curve anyway.
- The AMC column registry marks `campaign_id` "do not use" on that table. The registry note gains one sentence naming the alias use.
- Load concurrency is expected to stay as it is: one manifest per invocation, parallel across physical tables and serialized within each. That behaviour is [unverified in this repository], to be confirmed on this branch (Evidence).
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| A special loader for Amazon Ads | The owner refused a special path; Walmart, Target, Google and Meta would need their own |
| **One generalisation, as data, for every API source** | **Chosen** |
| Re-key AMC campaigns on the numeric ID | Breaks existing keys and history, and goes against AMC's guidance to use the string ID |
| Hold unresolved deliveries and recompile when the alias arrives | Too much new machinery (owner, 2026-10-06) |
| Make the Ads API the campaign identity owner | Only reverses the ordering problem |
| **AMC as the bridge, matched at read time, with a conflict check** | **Chosen** (owner, 2026-10-06) |
## Open questions
- **DSP campaign IDs.** Does the Ads API v1 DSP campaign ID equal AMC's DSP `campaign_id_string`? A planned probe of recorded responses checks it. Until then, DSP references stay unmatched and DSP keeps curves only.
- **DSP advertiser IDs.** Does `ads-accounts` carry the same DSP advertiser ID the DSP operations take? It is assumed equal and checked from a recorded fixture before the contract is written.
- Campaign-ID uniqueness across profiles is [unverified]; the conflict check handles it either way.
## Evidence
- `GovernedQueryPack` requires SQL, a query digest and expected columns [verified 2026-10-06, repository file `src/domain/producer/models.py`, lines 67 to 107]. The Finalizer's AMC-only rules sit under `if pack.source_system == "amc"` [verified 2026-10-06, `src/domain/producer/validation.py`, lines 277 to 290].
- On `sponsored_ads_traffic`, `campaign_id` is the Ads API ID and `campaign_id_string` the console ID; the registry marks `campaign_id` `deprecated_for_use` [verified 2026-10-06, `docs/sources/amc.md` line 130 and `contracts/shared/amc-columns.yaml` lines 149 to 166].
- Ads API v1 reports offer `GZIP_JSON` and list up to 100 downloadable parts [verified 2026-10-06, Amazon Ads merged OpenAPI specification, `ReportFormat` and `ReportPart`].
- The account-management accounts list returns DSP advertiser IDs, profile IDs, entity IDs and country codes [snippet 2026-10-06, Amazon Ads account-management page; the page didn't render for fetch].
- The load path prepares every contract a manifest resolves, plans one unit per physical table, and serializes writes per table [unverified in this repository; from a local load-path investigation of 2026-10-06 that read a later, unmerged commit].
## Review trigger
A second API source that needs a code change in the Finalizer or loader; a probe showing IDs aren't unique within a profile; or a source whose campaign identity can't be bridged by a same-record alias.
