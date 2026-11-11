# ADR-160: Mount the JumpStart Chronos-2 artifact and enforce isolation in IAM

Date: 2026-11-11

Status: Accepted (2026-11-11), approval DR-9. Built in dev by slice 1 of the demand forecast;
not yet served. Cites [ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md),
which stays Proposed: this decision takes the parts the weekly demand forecast needs (SageMaker
Processing, x86_64, no VPC) and replaces ADR-094 point 3 (weights baked into the image) and its
isolation-in-code consequence.

## Context

The weekly Vendor demand forecast runs Chronos-2 in a SageMaker Processing job, one job per shard
of at most 2,000 ASINs. The job must read no network, run only code and weights the run can name,
and give the same forecast for the same input and policy whatever order rows arrive in.

## Decision

1. **Weights come from the JumpStart artifact, mounted read-only.** The job mounts the configured
   SageMaker JumpStart prefix of `pytorch-forecasting-chronos-2` (`demandForecast.modelArtifactUri`)
   at `/opt/ml/processing/model`. No weights are in the image.
2. **The artifact's digest is checked before the model loads.** `artifact_roster.roster_digest()`
   hashes every file's path, size and SHA-256. The operator prints it once with
   `make demand-forecast-artifact-digest PROFILE= ENV=` and sets `demandForecast.modelArtifactDigest`;
   synth refuses a missing value, and the job fails `MODEL_ARTIFACT_MISMATCH` on any other digest.
3. **The runtime is one exact lock on Python 3.14.** The `forecasting` dependency group is pinned
   to the approved package set (approval D-1), exported alone to a hashed `requirements.lock` and
   installed with `pip --require-hashes --no-deps` on one pinned `python:3.14-slim` amd64 manifest
   digest (approval D-2). `make check-forecast-lock` refuses any other package set. The image holds
   only the paths `src/adapters/jobs/demand_forecast/image-paths.txt` lists, so its CDK asset hash
   is the processor-code identity in the run fingerprint. The state machine passes that hash to
   every job, and the job fails `RUN_IDENTITY_MISMATCH` before the model loads when the manifest
   names another processor, policy, lock, model or schema set, or a run fingerprint that doesn't
   recompute from its parts.
4. **Batching and numbers are policy.** `contracts/forecasting/demand-forecast.yaml` declares
   cross-learning, the batch size of 256, the sort key and the shard size (`engine`), and the
   four-place half-even storage with its check order (`numeric`). Each section is versioned and the
   file's SHA-256 is in every run fingerprint.
5. **Isolation is enforced in IAM, not only requested.** The state machine role may call
   `sagemaker:CreateProcessingJob` only with `sagemaker:NetworkIsolation = true`, the configured
   instance type and a runtime no longer than the configured one. The Processing role may write only
   `runs/*/shards/*/attempts/*/candidate/*`. The container runs as uid 10001 with no capabilities.

## Consequences

- Forecasts are deterministic for a given roster and policy. Because cross-learning forecasts each
  batch jointly, a product entering or leaving the roster can change the forecasts of the others in
  its batch and of later batches in its shard (approval P-12).
- A new artifact, lock, policy or image is a new run fingerprint, so a run starts even when no
  source publication changed.
- Every account needs a SageMaker Processing quota of at least `maxParallelJobs` (8) for
  `ml.m5.xlarge`; the documented default is 0 (approval I-8).
- `accelerate` stays in the lock because `chronos-forecasting` requires it. Its checkpoint-loading
  advisory is recorded NOT AFFECTED (O-ACCELERATE-CHECKPOINT-001 in
  [security dispositions](../security-dispositions.md)): the job never calls those loaders and reads
  only the digest-checked mount.

## Evidence

Dev, synthetic data, 2026-11-11: the network-isolated self-check loaded the weights on the approved
lock (Python 3.14.10, `chronos-forecasting` 2.3.2, `torch` 2.14.1+cpu), ran as uid 10001, gave
finite, ordered forecasts with unknown context weeks, two identical passes, and a mounted-copy
digest equal to the operator command's. No fallback image was needed. A 2,000-ASIN shard (staged
scale fixture, Chronos-2 and backtest) took 450 s, 479 s and 454 s from `CreateProcessingJob` to
job end, with a peak RSS of at most 2.57 GiB, inside the 1,800 s bound, so `shard_max_asins` stays 2,000 (P-18;
[validation results](../../validation/RESULTS.md#demand-forecast-slice-1-engine-and-weekly-input-on-dev--2026-11-11)).

## Alternatives

- Bake the weights into the image (ADR-094 point 3): a larger image and no AWS-published provenance.
- Request isolation in the job request only: a mistaken request would run with network access.
- `cross_learning: false`: no roster effect, but not the setting the source engine validated.
