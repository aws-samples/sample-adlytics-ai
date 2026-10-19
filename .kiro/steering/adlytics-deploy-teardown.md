---
inclusion: auto
name: adlytics-deploy-teardown
description: Use when deploying, updating, signing in to, or destroying an Adlytics AI environment with the make deploy-all, create-user or destroy-all targets.
---

# Deploy and tear down

Profile, environment and identity rules are owned by meta-aws-operations. The targets own their
own steps; never restate or reorder them.

## Before the first deploy

- Check every row of the README Prerequisites table with the user before running a target that
  reaches AWS, including its Athena concurrent-query row and its edge rate-limit row. Those rows
  link the contract and the .env.example settings that own the values; never state a quota or
  limit from memory.
- Copy .env.example to .env and fill it with the user; never invent a value it asks for.

## Deploy or update

- Run make deploy-all without APPROVE first. Show the user the account, Region and environment
  line it prints and the diff, and run it again with APPROVE=1 only after the user confirms that
  account is the dedicated one they intend.
- Before an update, read the one-time step stated beside each stack in DEPLOY_ORDER
  (infra/lib/config/index.ts); make deploy-all does not stop for them.
- A src/ change on an environment others use deploys with make deploy-all-locked, never with a
  single-stack make deploy (docs/decisions/adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md).
- Signing in and checking the pages is a manual step: ask the user to do it and report what they
  saw. A finished deploy command is not evidence that the site works.

## Tear down

- Run make destroy-all only after the user names the environment and confirms the account, and
  pass the environment as CONFIRM.
- Never pass INCLUDE_ACCOUNT_SETTINGS=1 unless the user asks for it by name; it removes an
  account-wide setting other workloads may rely on.
- The README "Clean up" table lists the resources that survive destroy-all. Deleting any of them,
  or turning off its deletion protection, needs the user's explicit approval for that resource.
