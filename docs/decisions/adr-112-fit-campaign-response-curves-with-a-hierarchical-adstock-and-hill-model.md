# ADR-112: Fit campaign response curves with a hierarchical adstock-and-Hill model
Date: 2026-10-07
Status: Proposed. Not accepted or implemented; acceptance waits on the hosted run-time test of [ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md). The owner chose the curve engine on 2026-10-06. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
A brand manager wants to know how attributed revenue would change if a campaign's spend changed. That needs a response curve per campaign: revenue against spend, with carryover, diminishing returns and an uncertainty band.
Four facts shape the choice:
- **The curve must learn from delivered spend, not budget.** Amazon averages a sponsored daily budget over the month, lets a single day overspend it, and lets portfolios share unspent budget ([ADR-093](adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md)). So a budget is a ceiling, not the treatment.
- **Most campaigns can't support their own curve.** Many are small or barely vary their spend. Pooling them with similar campaigns is the standard remedy, and the label must say so. The prototype didn't show that pooling helped (see Evidence).
- **The outcome is attributed.** AMC credits conversions from exposures up to 14 days earlier. A fitted carryover mixes that attribution lag with true carryover. Nothing measured here is incremental.
- **No managed service was found for this.** A search of the AWS Solutions Library found no managed response-curve or marketing-mix modelling service, and Amazon's own forecasting APIs don't fit a curve on this product's outcome basis.
## Decision
**Fit one hierarchical Bayesian adstock-and-Hill model per platform each week, from delivered spend and attributed revenue, built in PyMC from PyMC-Marketing's components.**
1. **Data.** One row per campaign per week, Monday to Sunday on the AMC day. The x is AMC fee-inclusive spend and the y is AMC 14-day attributed revenue. A per-platform `CurveInputBinding` names both, so another platform supplies its own. Missing is never zero: an unknown outcome is empty, and a paused campaign's spend is a true zero.
2. **Structure.** Geometric adstock and Hill saturation, with no intercept. Season, controls, promotions and peaks scale the media response rather than add a free level, so a free level can't make cuts look cheap. The curve passes through zero.
3. **Pooling.** Campaign → channel → channel group, from the media taxonomy ([ADR-052](adr-052-separate-ecosystem-platform-channel-and-ad-product.md)). Single-child levels collapse. A campaign with little variation leans on its group and is labelled `pooled_only`. Pooling's benefit wasn't shown on synthetic data (see Evidence); its levels are policy that the statistical review approves before activation.
4. **Seasonality.** One joint fit covers two blocks that don't overlap: about 11 months of Ads API v1 attributed sales before the AMC window, and the AMC window itself. They share the per-channel seasonal terms. Whether the earlier block's media term is linear or a Hill tied to the window's shape is a versioned policy, set from the prototype's checks.
5. **Capping.** Days that hit the budget are excluded in the primary fit and included as-is in a sensitivity fit. They are never a control.
6. **No posterior is reused as a prior.** Each weekly fit starts from the same priors, so the same evidence is never counted twice.
7. **One evaluation function.** The adstock and Hill arithmetic over stored draws lives once, in the domain layer. The chart and the simulator both read it, and a parity test checks it against the library's transforms.
8. **Sampler and fallbacks.** nutpie first, then PyMC's own NUTS (No-U-Turn Sampler), then Meridian in its own image, with its structural differences recorded.
9. **Labels.** Every curve is labelled attributed, not incremental. An optional customer lift result calibrates it; without one it is `uncalibrated`.
## Consequences
- Fitting needs a scientific stack (PyMC-Marketing, PyMC, PyTensor, nutpie, numba) in a separate, exactly pinned dependency group. It is installed only in the job image and in a separate check target, never in a Lambda.
- Every curve runs the gates of [ADR-113](adr-113-publish-a-response-curve-only-after-held-out-identification-and-fragility-gates.md), and is used only within the total eligibility they allow. A weakly identified curve, or one whose held-out test passed only on its channel's pooled weeks, drives cuts only. A `pooled_only` curve may drive increases and cuts within support, as a fitted one may.
- On synthetic data the curves read too high when hidden demand moves both spend and revenue. Curves are labelled attributed, and budget cuts are read cautiously (ADR-113).
- The x86_64 host of ADR-094 avoids the missing compiled PyTensor wheel on Linux ARM64.
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| PyMC-Marketing's packaged multidimensional marketing-mix model (MMM) | It pools through `dims`, but it doesn't show nested campaign-in-channel pooling, and a crossed grid would be mostly empty |
| **A hierarchical PyMC model built from PyMC-Marketing's adstock and Hill components** | **Chosen.** It keeps the library's tested transforms and lets us state the hierarchy and structure exactly |
| Google Meridian, campaigns as geos | Every campaign shares one curve shape per channel and differs only in scale. It needs Python 3.11 to 3.13 and recommends a GPU. Kept as the last fallback |
| Meta Robyn | R, ridge regression with no pooling; unstable on sparse campaigns |
| LightweightMMM | Archived; its README points to Meridian |
| Amazon's MPO playbook (least-squares curve per campaign) | No carryover, controls or pooling. Kept only as an internal benchmark, never in a total: a proposed default, adopted under the delegated approval and still open for the owner |
## Open questions
- **Can a budget cut be read from the hours it removes?** That needs AMC attributed revenue by traffic hour, and whether Amazon stops serving when out of budget. Both are [unverified]; a planned probe of intraday return and out-of-budget serving settles them. Until then, ADR-113's comparator reads cuts.
- Shared open items, including the policy starting values for the priors, adstock length and pooling levels, and AMC's multi-campaign credit rule, are listed in [Response curves and budget simulation §6](../concepts/response-curves-and-budget-simulation.md#6-open-items).
## Evidence
- No AWS managed MMM or response-curve service was found in the Solutions Library [verified 2026-10-05, AWS Solutions Library directory search]. Amazon Forecast is closed to new customers [verified 2026-10-05, Amazon Forecast developer guide].
- PyMC-Marketing 1.2.0 was released 2026-09-29 under Apache-2.0 and declares Python 3.12 to 3.14 [verified 2026-10-05, PyPI JSON]. PyTensor 3.3.3 has no compiled cp314 Linux aarch64 wheel; x86_64 has one [verified 2026-10-05, PyPI file list].
- Meridian varies only the media coefficient by geo; adstock and Hill parameters are per channel [verified 2026-10-05, Meridian model specification]. It requires Python 3.11 to 3.13 and recommends a GPU [verified 2026-10-05, Meridian README].
- LightweightMMM is archived [verified 2026-10-05, GitHub repository metadata]. Robyn is R with ridge regression [verified 2026-10-05, Robyn features page].
- Sponsored budgets average over the month, and a day may overspend [verified 2026-10-05, Amazon Ads average-daily-budget guide]. Portfolio sharing is in [ADR-093](adr-093-treat-overlap-clusters-and-sharing-pools-as-joint-units.md)'s Evidence.
- AMC attributes conversions on conversion date from exposures up to 14 days earlier [repo 2026-10-06: the AMC contract `contracts/amc/amc-attributed-conversion.yaml` declares it; no AMC page was read for this claim].
- Offline prototype on fictional data, local Apple-silicon runs, 2026-10-06 ([prototype summary](../../validation/response-curve-prototype.md), Response curves). These are measurements on fictional data, not on customer data.
  - **Failed:** convergence (group R-hat, which compares the sampler's chains and should be near 1, ≤ 1.01 and effective sample size ≥ 400 in 2 of 10 worlds, with the between-channel spread set by its prior); interval coverage (64.1%, with marginal ROAS a median 13.4% high, in the best-case worlds); **pooling's benefit** (on flat campaigns, median absolute relative error 0.40 pooled against 0.25 unpooled); saturating truths reaching a usable state (2.8% against 50%); intended labels (63.0% against 90%); and fragile campaigns flagged (6 of 10 worlds against 80%).
  - **Hidden demand:** where it moved spend and revenue together, marginal ROAS read 14% to 35% high, with 28% to 42% coverage.
  - **Gates:** the held-out benchmark and identification gates left 372 of 600 curves `weakly_identified`.
  - **Repeatability:** one pair of cold 50-campaign fits on macOS gave identical diagnostics. Linux wasn't run.
## Review trigger
A platform whose campaigns need a curve shape the hierarchy can't express, a sampler that fails the hosted test, or a lift result that disagrees with the curves beyond their bands.
