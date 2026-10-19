# Build with an AI assistant

| | |
|---|---|
| **Level** | L1 — contributor guide |
| **Status** | Current |
| **Last updated** | 2026-10-18 |
| **Owns** | the starter prompts for an AI coding assistant, and how the committed steering set is organised |
| **Conventions** | [Documentation conventions](conventions.md) |

This repository ships guidance for AI coding assistants. [Kiro](https://kiro.dev/) loads the files
in `.kiro/steering/` automatically; other assistants read [AGENTS.md](../AGENTS.md), which points
back here. The guidance keeps an assistant inside the same rules a human contributor follows: every
figure comes from a governed tool, contracts are changed before code, the local gates prove a
change, and nothing reaches AWS without an explicit profile and environment.

## What the steering set covers

| Files | Loaded | Covers |
|---|---|---|
| `adlytics-overview.md`, `adlytics-safety.md`, `meta-ai-collaboration.md` | Every turn | Where facts live, contract-first changes, data and credential safety, Git safety, evidence and the no-invented-figures invariant |
| `adlytics-deploy-teardown.md`, `meta-aws-operations.md` | When the task deploys, updates or destroys an environment, or runs an AWS command | The `make deploy-all` and `make destroy-all` runbook and the explicit-profile rule |
| `agent-*`, `backend-*`, `data-*`, `frontend-*`, `infra-*`, `validation-probes.md`, `amc-validation-workflow.md` | When a matching file is open, or the task matches | Rules for that layer of the code, and for AMC source validation |
| `meta-testing-mutation.md` | Only when you reference it in chat as `#meta-testing-mutation` | The mutation review method |
| The other `meta-*` files | When a matching file is open, or the task matches | Documentation, research, root-cause analysis, testing, Git delivery and steering authoring |

Files named `local-*.md` are personal bindings (your AWS profile, your remotes) and are gitignored;
create your own when a committed rule asks for a value only you know.

## Starter prompts

1. **Deploy a trial environment.** "Check my machine and account against the README prerequisites,
   then deploy Adlytics AI to a new environment called `trial` with my `<profile>` profile. Show me
   the diff before you deploy."
2. **Load your first data.** "Explain which AMC query packs and Vendor Central reports I need for
   the Campaigns and Retail health pages, and how my pipeline should land them. Link the contracts."
3. **Add a metric.** "Add a metric for `<definition>` to the metric dictionary, regenerate what
   depends on it, and run the gates. Tell me which pages and tools can use it."
4. **Explain a number.** "On the Campaigns page, ad spend for the last 7 days is `<value>`. Trace
   where that figure comes from, from the landed delivery to the page."
5. **Tear down.** "Destroy the `trial` environment, then list every retained resource that is
   still billing and what I must do before deleting each one."

Each prompt maps to a section of the [README](../README.md): prerequisites and quickstart, load
your data, extend it, and clean up. The assistant still asks before anything that deploys, deletes,
or weakens a security control.
