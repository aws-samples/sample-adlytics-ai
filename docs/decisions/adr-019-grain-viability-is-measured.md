# ADR-019: Grain viability is measured per customer, not declared

Date: 2026-08-19
Status: Accepted

> **Proposed amendment, 2026-10-06.** [ADR-099](adr-099-treat-distinct-shopper-counts-as-a-new-kind-of-fact.md) (Proposed) would add a scoped exception for distinct shopper counts: each served grain is its own extraction, viability is measured as missing expected cells over expected cells, withdrawal needs 20 or more cells and a coarser served grain, and the differencing-guard risk is accepted and measured. This decision stays in force, unchanged, until ADR-099 is accepted, and afterwards for additive measures in existing packs.

## Context

AMC releases an output row only when enough distinct users sit behind it. The bar comes from the strictest column in the output: 2 distinct users when everything is `NONE` or `LOW`, 100 when anything is `MEDIUM` or `HIGH` [verified, [sources/amc](../sources/amc.md) §5].

That splits into two questions, and only one is answerable at design time.

**Structural feasibility** — is this grain legal given the threshold class of every column it needs? Provable today from `contracts/shared/amc-columns.yaml`, which records the class of all 1024 transcribed columns.

**Volumetric feasibility** — do enough real users land in each bucket? This depends entirely on one advertiser's traffic volume. It cannot be answered from documentation, and it cannot be answered from the AMC sandbox either: Amazon states the synthetic data carries no realistic distribution and no outliers [verified, [sandbox guide](https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-cloud/amc-sandbox)]. A production instance we borrowed would answer it for *that* advertiser and nobody else.

**So a statically declared grain is a guess about a customer we have not met.** Since this platform is deployed into each customer's own AWS account, the guess is wrong at different points for each of them.

**Two facts make the problem tractable.**

First, **AMC measures its own lossiness and will tell us.** With `filteredMetricsDiscriminatorColumn` set, every row carries a boolean saying whether values were removed, `filteredReasonColumn` carries the shortfall, and `distinctUserCountColumn` carries the count used. A load reports its own suppression rate, derived from that customer's data.

Second, and less obvious: **with the discriminator set, coarse aggregates survive suppression even where fine breakdowns do not.** Sub-threshold rows are not dropped; their offending dimension is nulled and the rows re-aggregate with `SUM` [verified]. The metric total is preserved and only the breakdown is lost:

| Grain | Reliable |
|---|---|
| campaign × city | No — some cities are `NULL` |
| campaign | **Yes** — summing every row, including the nulled ones, gives the true total |

So one extraction at the finest declared grain yields correct answers at every coarser grain. Deriving downward is sound; the suppression flag marks exactly where downward is necessary.

## Decision

**Extraction always sets the discriminator, and always happens at the finest declared grain.** This is already a contract rule; this decision makes it load-bearing rather than hygienic, because everything below depends on the merge-not-delete behaviour it buys.

**Coarser grains are derived, never re-extracted.** A second query at a coarser grain would cost a `createReport` slot, add execution history to AMC's cross-execution differencing guard, and answer a question the first query already answered.

**Every load computes a suppression rate per partition** — suppressed rows over total rows — and writes it to the freshness registry beside coverage. Per-customer automatically, because it is derived from their own loads rather than configured.

**`Coverage.valid_grains` becomes a derivation, not a declaration.** The catalog tool reports the grains that are viable *for this customer*, computed from measured suppression against a configured threshold. A grain whose loss exceeds the withdrawal threshold is not offered, so the agent cannot request it — the same refusal mechanism as `roas_blended` having no campaign grain, driven by data instead of by structure.

**Thresholds are configuration, with conservative defaults.** A degraded grain is reported with its loss; a withdrawn grain is not offered at all. Starting values are 5% suppressed rows to degrade and 20% to withdraw, chosen to err toward refusing rather than answering and to be tuned once real distributions are visible.

## Consequences

**`Coverage` changes meaning, and every consumer must expect it to move.** A grain's availability can change between loads. A tool that cached `valid_grains` would offer a grain the data no longer supports, so coverage is read per request — which the design already requires for freshness.

**One extraction serves many grains**, which reduces pressure on the binding constraint. `createReport` is one call per minute and AMC executions often take 15 minutes, so not re-querying for a coarser cut is a material saving rather than an elegance.

**The agent gains a new kind of honest answer.** "That breakdown loses 34% of rows for your volume, here it is by week instead" is now expressible, and it is derived rather than hand-written.

**A threshold is a policy choice we are making on the customer's behalf**, and 5% and 20% are asserted rather than derived. They are configuration precisely because they will be wrong for someone.

**This does not rescue a structurally illegal grain.** A `HIGH` column still cannot be compared to a literal, and no measurement changes that. Structural feasibility remains a contract-time check enforced by `make validate-contracts`.

**V1 sits in the free zone regardless.** Every planned V1 output column is `NONE` or `LOW`, so the bar is 2 distinct users and suppression should be near zero. This decision matters at the moment someone asks for geography or an hourly grain — which is exactly when it stops being theoretical.

## Alternatives rejected

**Declare valid grains statically per contract.** The status quo, and it encodes one advertiser's volume into a contract shipped to every customer. Rejected because the failure is silent for whoever has less traffic than whoever wrote the contract.

**Re-extract at several grains and keep whichever survives.** Rejected on three counts: it multiplies `createReport` calls against a one-per-minute limit, it adds overlapping executions over the same sources and windows which is precisely what AMC's differencing guard rejects, and it is unnecessary because a discriminator-set extraction already contains the coarse answer.

**Ask the customer what grain their volume supports.** They do not know, and the honest form of the question requires the analysis this decision automates.

**Probe once at onboarding and freeze the answer.** Tempting and cheaper. Rejected because volume is seasonal and campaign mixes change, so a grain viable in November may not be in February, and a frozen answer degrades silently in the direction of over-promising.

## Review trigger

Reopen when the first real customer load produces a suppression rate above the degrade threshold on a grain we expected to be free. That either means the thresholds are wrong or a contract declared an escalating column nobody noticed, and both are worth knowing early.
