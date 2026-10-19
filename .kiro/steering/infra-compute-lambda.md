---
inclusion: auto
name: infra-compute-lambda
description: Use when defining or changing AWS Lambda infrastructure in CDK, including function boundaries, IAM, runtime or architecture configuration, packaging and layers, artifact builds, memory, or timeouts.
---

# Lambda in CDK

Decisions: ADR-005. Packaging rationale: docs/repo-structure.md §3; quota
figures: scripts/build-lambdas.sh (checked every build).
General CDK rules are in infra-style-cdk.

## One function per domain

- Define one function per domain, never one per tool. Several Gateway MCP targets
  route to one function.
- Never collocate two tools unless their IAM statements are identical. The
  function holds the union, so any difference widens blast radius silently and
  permanently.

## Runtime and architecture come from config

- Read architecture and runtime from lib/config; never write an architecture or
  runtime version inline in a stack, or one stack will drift from the rest.
- Never target a Lambda runtime that is in public preview.

## Packaging

- Build the artifact with make build-lambdas and reference its output with
  lambda.Code.fromAsset. Never build at synth time.
- Run make build-lambdas before any cdk deploy that changes Python. cdk deploy
  uploads whatever the asset path already holds, so a code change deploys as a
  no-op and CloudFormation reports success. The symptom is infrastructure that
  is plainly correct in the template and behaviour that is plainly absent at
  runtime, with every gate green — because the tests ran against the source and
  the function did not.
- Ship exactly what the lock file resolved, including the AWS SDK. Never treat a
  package as runtime-provided. The runtime-provided SDK version is not governed
  by our lock file or the type stubs, so relying on it lets the type checker
  approve calls the deployed runtime may reject — which voids the typed-SDK
  guarantee for every AWS call, not just the one that fails.
- Install the layer with --no-deps. The export is already a fully resolved
  closure, and without it every excluded package returns as a transitive
  dependency, which makes an exclusion list decorative rather than real.
- Put dependencies in one layer for the platform, published by its own stack and
  attached through a published version ARN. Split per domain only when the
  closures genuinely differ: two layers over one built asset publish the same
  bytes twice on independently drifting version streams, so each stack can assert
  it has "one layer" while the platform has two that disagree.
- Precompile bytecode into the artifact. Use hash-based invalidation, not
  timestamp: packaging
  rewrites mtimes, and a timestamp-validated file is silently treated as stale.
- Exclude a dependency the code never imports, and prefer an allowlist where a
  package family is large. Verify the exclusion against loaded modules rather
  than reasoning about it, and let the failure be an import error at init.
- Never treat a layer as headroom. Lambda's unzipped size quota covers the
  function and all its layers combined, so a layer cannot rescue an oversized
  function.
- Switch to DockerImageFunction, the escape hatch ADR-005 records, before the combined
  unzipped size approaches that quota. Quota figures are owned by
  scripts/build-lambdas.sh, which measures every build against them and warns on
  approach; never restate one here, because AWS moves them.

## Configuration

- Never leave memorySize at the CDK default. The chosen value carries a comment
  saying what it was chosen for.
- Size for the slowest thing on the critical path, not for each function's own
  work. A function that runs on every authorised request sets the floor for
  everything behind it, so starving the smallest one is what makes the whole path
  slow. More memory for less duration is frequently cost-neutral, because the
  bill is memory multiplied by time.
- Set timeout below the caller's timeout, never above it. A function outliving
  its caller burns cost and returns nothing.
- Grant least privilege per function. Never attach a managed policy where an
  inline statement would do.
