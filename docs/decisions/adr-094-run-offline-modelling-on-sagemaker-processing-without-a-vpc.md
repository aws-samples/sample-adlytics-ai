# ADR-094: Run offline modelling on SageMaker Processing, x86_64, without a VPC
Date: 2026-10-07
Status: Proposed. Not accepted or implemented. **Acceptance waits on the hosted run-time test.** The owner chose the host on 2026-10-06 and accepted the retained image stack on 2026-10-07. The rest of the design was approved on the owner's behalf on 2026-10-07, under authority the owner delegated; the owner hasn't reviewed the final design yet. It would record an exception to [ADR-005](adr-005-compute-runtime-baseline.md)'s Graviton rule; ADR-005 stays in force, unchanged, until this is accepted. The design is summarised in [Response curves and budget simulation](../concepts/response-curves-and-budget-simulation.md).
## Context
Curve fits ([ADR-112](adr-112-fit-campaign-response-curves-with-a-hierarchical-adstock-and-hill-model.md)) and Chronos-2 forecasts ([ADR-092](adr-092-forecast-the-chronos-2-baseline-with-known-in-advance-covariates.md)) are offline batch work. They need minutes to tens of minutes of CPU and a few gigabytes of memory. The forecasting architecture already names a SageMaker Processing job. The repository has no VPC today.
ADR-005 puts compute on ARM64 Graviton, but SageMaker Processing offers no Graviton instance type in us-east-1. AWS Batch on Fargate runs ARM64, but it needs subnets, and so the repository's first VPC, with endpoints that cost money every hour.
## Decision
**Curve and baseline jobs run as SageMaker Processing jobs on ml.c5 instances, x86_64, with network isolation on and no VPC.**
1. **One job family.** Curve fits and Chronos-2 batches share one Processing job family. The default size is ml.c5.2xlarge (8 vCPU, 16 GiB).
2. **Submitted by the control plane.** The execution-graph control plane's executor adapter calls SageMaker directly. There is no Step Functions state machine.
3. **Network isolation.** Every job sets `EnableNetworkIsolation = true` with no `VpcConfig`. The container makes no outbound call. SageMaker copies the declared S3 inputs in and the outputs out, using the execution role. Model weights are baked into the image, so nothing is downloaded at run time.
4. **The x86_64 exception to ADR-005**, recorded here, because Processing has no Graviton type in us-east-1. Job images are built for `linux/amd64`.
5. **Retained images.** The job images' ECR repositories live in a retained `MlImageStack`. Every fit records its image digest, so an old revision can re-run exactly.
6. **Quota as a prerequisite.** A new account's `ml.c5.2xlarge` Processing quota defaults to zero, so a quota increase is a deployment prerequisite. Until it exists, the capacity pool reports `POOL_QUOTA_UNKNOWN` and admits nothing.
7. **Logs.** Every new role writes only its own log group, following [ADR-085](adr-085-lambda-roles-write-only-their-own-log-group.md)'s rule.
8. **Scale-up path.** AWS Batch on Fargate ARM64 in an isolated VPC is the documented path if SageMaker billed hours pass the one-host bound below. It is not built in v1.
## Consequences
- v1 has no VPC, subnets, security groups or endpoints, and no fixed network cost.
- At 5,000 campaigns, with weekly-anchored backtests, the projection is about 29.1 to 47.7 job-hours a month. That range holds only if every source restates at most 28 days back and no reconstructed promotion or coupon covariate is active. If those covariates are active, it can reach about 105.3 job-hours.
- One scenario inside the range, about 44.1 job-hours, costs about $17.98 a month on ml.c5.2xlarge. The comparable Fargate figure is about $35.83, including the private network.
- The hosted test must show SageMaker billed hours stay under the one-host bound for the measured mix, where SageMaker and Fargate cost the same. Across the weekly-anchored projections the bound runs from about 76.2 to about 135.2 h, and each bound pairs with its own start budget per job, for start-up and image pull. The pairs are in the [prototype summary's Hosting projection table](../../validation/response-curve-prototype.md#hosting-projection).
- One Chronos-2 batch at the proposed size must fit in 16 GiB.
- Isolation is enforced by the job request and checked by an adapter assertion before each submit. An IAM condition would be stronger, but whether Processing exposes one is [unverified].
## Alternatives considered
| Alternative | Why not, or why |
|---|---|
| AWS Batch on Fargate ARM64 in an isolated VPC | ADR-005-aligned, but adds the first VPC and a fixed endpoint cost. At the expected scale it projects more expensive. Kept as the documented scale-up path |
| **SageMaker Processing, x86_64, no VPC, network isolation on** | **Chosen** (owner, 2026-10-06): managed, no customer network, no fixed endpoint cost |
| Step Functions `CreateProcessingJob.sync` to submit jobs | A second workflow authority, whose own retries and timeouts can disagree with the control plane's. Replaced by the control plane's executor adapter ([ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md)) |
| ECR in a destroyable stack | Old revisions couldn't re-run exactly. Replaced by a retained image stack (owner, 2026-10-07) |
## Open questions
- **Does network isolation behave for Processing as the documentation states for training and inference?** The hosted test checks the isolated S3 copies. If it doesn't, the fallback is a VPC-attached Processing job with only an S3 gateway endpoint, costed before the change.
- Checking isolation in code is one of the dev-only controls listed in [ADR-114](adr-114-run-curve-and-forecast-jobs-as-execution-graph-workloads.md)'s open questions, to be re-decided by the owner before activation.
- The Processing price for ml.c5.xlarge isn't sourced, so the smaller size isn't priced.
## Evidence
- SageMaker Processing has no Graviton instance type in us-east-1 [verified 2026-10-05, AWS Price List].
- `EnableNetworkIsolation` is a Processing `NetworkConfig` setting [verified 2026-10-06, SageMaker API reference, `NetworkConfig`].
- With isolation on, training and inference containers make no outbound calls, even to S3, hold no credentials, and SageMaker performs the S3 transfers [verified 2026-10-06, SageMaker developer guide, "Run Training and Inference Containers in Internet-Free Mode"]. The same behaviour for Processing is [unverified] until the hosted test.
- ml.c5.2xlarge has 8 vCPU and 16 GiB [verified 2026-10-06, SageMaker instance-types table].
- The `ml.c5.2xlarge for processing job usage` quota has an AWS default of 0 [verified 2026-10-07, Service Quotas API, us-east-1].
- Prices: ml.c5.2xlarge Processing $0.408 an hour [verified 2026-10-05, AWS Price List]; Fargate ARM64 per vCPU-hour and GB-hour [verified 2026-10-05, AWS Price List, effective 2026-07-01]; interface endpoints $0.01 per endpoint-hour [verified 2026-10-06, AWS Price List, effective 2026-09-01].
- The job-hour figures are projections, arithmetic on local Apple-silicon timings from the offline prototype of 2026-10-06 ([prototype summary](../../validation/response-curve-prototype.md), Hosting projection). Host speed and start-up are [unverified].
## Review trigger
The hosted test missing the one-host bound or the start budget; isolation not holding for Processing; or SageMaker adding a Graviton Processing type.
