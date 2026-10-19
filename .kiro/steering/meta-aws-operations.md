---
inclusion: auto
name: meta-aws-operations
description: Use when running or designing local AWS CLI, CDK, or operator actions, inspecting live AWS resources, deploying CloudFormation, changing IAM or AWS resources, or validating deployed state.
---

# AWS operations

## Local operator identity

- Every local AWS CLI or CDK command MUST use an explicit, caller-supplied named
  profile. Never use a default profile, ambient credentials, or an instance role
  for a local operator action.
- A repository validation or operator SDK session MUST receive a caller-supplied
  explicit profile; never fall back to a default profile. The operator's own
  profile binding is local configuration, bound per meta-style-steering, never
  in a committed file.
- Before any mutation, verify the caller identity with STS using that profile and compare it with the intended target.

## Deployed workload identity

- Application and infrastructure runtime code MUST use the deployed service execution role; the developer-profile prohibition for runtime code is always-on in meta-ai-collaboration.
- Grant the workload role only the actions and resources its deployed path requires.

## Operator interfaces and evidence

- A repository command that reaches AWS MUST require every target-affecting input it needs: profile, environment, region, expected account, and exact resource target where applicable. Do not default a target-affecting input.
- Never persist a workspace operator profile, account, region, or environment in application code, CDK configuration, Make targets, package scripts, or checked-in command examples.
- Inspect deployment ownership through the exact CloudFormation stack. Never enumerate account resources to infer what this repository deployed.
- For runtime state not held by CloudFormation, query the exact named resource. Derive its name from the owning configuration helper or published contract; never guess it.
- Before disabling or replacing a live dependency, verify that the replacement synthesizes with complete inputs and has an executable acceptance path.
- A completed command is not deployed-state evidence. Read the returned stack, resource, or acceptance state before reporting success.
