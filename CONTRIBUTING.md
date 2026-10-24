# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional 
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary 
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check [existing open](https://github.com/aws-samples/adlytics-ai/issues), or [recently closed](https://github.com/aws-samples/adlytics-ai/issues?utf8=%E2%9C%93&q=is%3Aissue%20is%3Aclosed%20), issues to make sure somebody else hasn't already 
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Run the gates and say in the pull request which ones you ran. See [Run the gates](#run-the-gates).
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and 
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## What a change carries

**There is no remote CI and none is planned** ([ADR-015](docs/decisions/adr-015-no-remote-ci.md)). Nothing re-runs these after you push, so a pull request that has not been through them locally has not been through them at all.

### Run the gates

Outside contributors stop after these gates and open the pull request; maintainers then follow
[Maintainer publishing](#maintainer-publishing).

```bash
make install                                       # once per clone, and after uv.lock changes
(cd infra && npm ci) && (cd frontend && npm ci) && (cd scripts && npm ci)  # once per clone, and after a lockfile changes
make verify   # lint, typecheck, import boundaries, contracts, unit tests
make check    # every documentation and model gate
```

Changes under `infra/` also need `make infra-typecheck` and `make infra-test`. Changes under
`frontend/` also need `make frontend-typecheck` and `make frontend-test`, then `npm run lint` and
`npm run build` from `frontend/`. `make verify-clean` runs all of these except `make infra-typecheck`,
in a clean export of HEAD, so commit first.

Every test, acceptance and scan target (`make test`, `make verify`, `make frontend-test`,
`make infra-test`, `make test-integration`, every `make accept-*` and the gates below) writes a
write-once [run record](docs/test-run-records.md) under the gitignored `.runs/`, and `make check`
refuses a target that does not. A claim that tests pass cites the record for that exact tree. `make gate-status` shows
the runs in progress and the recent records.

### Deployed checks (maintainer-run)

The checks below need a deployed environment. A maintainer runs them against a development account before merging; you do not need an AWS account to open a pull request.

Changes that touch an AWS adapter need `make test-integration PROFILE=<PROFILE> ENV=<ENV>`, which runs against real resources rather than mocks ([ADR-014](docs/decisions/adr-014-aws-testing-seam.md)) — and an adapter covered only by unit tests is not covered.

**Changes to a data contract, the load path, or the ingestion trigger also need deployed V2
acceptance after the affected stacks are deployed:**

```bash
make accept-ingestion-v2 \
  PROFILE=<PROFILE> ENV=<ENV> DAY=<YYYY-MM-DD> GLANCE_VIEWS=<NONNEGATIVE_INTEGER>
```

This probe uses synthetic Vendor Central traffic, writes immutable version-bound evidence, and
requires exact successful attempts, `CURRENT` advancement, and unchanged dead-letter queue depth. It
is not an AMC source probe. A change to a claim about AMC itself must additionally run the narrow
owning probe under `validation/`. Only the non-identifying conclusion is recorded in
`validation/RESULTS.md` and its owning source document; exact execution and resource identifiers and
raw output are never committed. Source-validation credentials remain local and are never arguments or
committed configuration.

Every gate runs under the interpreter `.python-version` pins. If a gate fails for a reason the error does not explain, check you are not on a system Python.

### Record a decision as an ADR

A change that makes a decision adds an ADR under [docs/decisions/](docs/decisions/), numbered sequentially. A change that reverses one amends the ADR it reverses rather than leaving two documents disagreeing.

An ADR states what was decided, what was rejected, and why — that is its job, and no design doc should restate it. If your change only follows an existing decision, cite it and add nothing.

Designs, plans and ADRs weigh their options by the rubric in [delivery practice](docs/delivery-practice.md#5-the-decision-weighting-rubric) and name the items that decided them.

### Tag tests with the decision they verify

```python
@pytest.mark.covers("ADR-007", "G-14")
def test_something(): ...
```

`ADR-NNN` comes from `docs/decisions/`, and `G-NN` and `F-NN` from the [decision coverage registry](docs/decision-coverage-registry.md). `make check-coverage-matrix` fails on an ID declared nowhere, so a typo is caught; it cannot catch a test tagged with a decision it does not actually exercise, and an inflated matrix is worse than an empty one because it reports confidence that is not there.

### Review mutants when you change the domain

`src/domain/` holds the code where a surviving mutant is a wrong number rather than a wrong redirect. Mutation review has repeatedly caught what the other gates could not: a caveat that was never emitted, a frozen dataclass with no test asserting it was frozen, a total that was a sum of averages ([ADR-013](docs/decisions/adr-013-mutation-testing.md)).

The [mutation steering](.kiro/steering/meta-testing-mutation.md) owns when a mutation run happens. `mutation-priorities.yaml` lists every unit by priority tier with the tests mapped to it:

```bash
make mutate UNIT=src/domain/<module>.py   # one unit: cosmic-ray for Python, StrykerJS for TypeScript
make mutate-next N=5                      # the next missing or stale units by priority
make mutation-status                      # current, stale, missing, untested and OPEN units per tier
make mutation-verdict UNIT=<path> ID=S-01 VERDICT=KILLED TEST=<test id>
```

Each run writes a run record and updates the local coverage map, one entry per unit with its survivor verdicts ([test run records](docs/test-run-records.md#coverage-map)). A unit whose fingerprint is unchanged is skipped, not re-run. The score is never a threshold: triage each survivor to KILLED with the test that kills it, or EQUIVALENT with the reason class. Python runs mutate source in place, so run them on a clean tree.

### Working with data and figures

Two rules that are specific to this product and are the reason it exists:

- **No figure in an answer may originate from the model.** Every number comes from a tool result. A change that lets a model-authored figure reach a user is the one defect class this codebase is built to prevent.
- **Never assert on a bare number in a test.** Assert its units and its grain too. A correct figure at the wrong grain is the failure mode a reader cannot detect.

### Without a deployment

You do not need a deployment to contribute. After the one-time installs, `make verify` and
`make check` need no AWS profile and no network; keep it that way.

Using an AI assistant? See [Build with an AI assistant](docs/kiro.md); it is optional for building and deploying.

For deploying and loading data, see [Load your data](README.md#load-your-data). [Development
seeding](docs/concepts/dev-seeding.md) owns the fixture sets and how they land; every fixture job is
dev-only and lands immutable, version-bound deliveries. Before you change the load path or a
fixture, keep its deliberate traps (suppressed nulls, a restatement pair sharing one window,
option-dependent absences, negative units, mixed currency) and the two fixtures that exist only to
be rejected. Editing a fixture lands a new delivery; it never changes or resets one already landed.

### Maintainer publishing

Maintainers publishing upstream gate in three tiers, each on a clean tree, and change main only
through a pull request. This section owns which gate runs before which action; each script's
section in [scripts/README.md](scripts/README.md) owns what it checks and its refusal codes, and
[meta-delivery-git](.kiro/steering/meta-delivery-git.md#remotes-and-publication) owns what may
reach the public remote.

| Tier | Before | Gate |
|---|---|---|
| Commit | every commit | the pre-commit hook, `scripts/commit_gate.py` on the staged changes |
| Push | every push | `make publication-gate` over the pushed range, which `make publish` runs |
| Merge | every merge to main | `make release-gate` on the exact tree being merged, which `make merge-pr` requires |

A change reaches main from a feature branch based on the current public main:

```bash
make install-hooks                # once per clone: the commit tier (pre-commit) and the push tier (pre-push)
make verify-clean                 # the test records of HEAD's tree that the push gate requires
make publish                      # push tier: pushes the current feature branch, never main and never a tag
make pr                           # opens or updates the branch's pull request: its commits and gate records
make release-gate                 # merge tier, on the pull request's head; run make pr again to list its record
make merge-pr PR=<n> CONFIRM=yes  # after the owner's go: squash-merges, then fast-forwards local main
```

- Commit tier: the [hooks](scripts/README.md#commit_gatepy-and-githooks) are a fast early warning
  on staged changes, not the boundary; `make uninstall-hooks` removes them.
- Push tier: `make publish` fetches `public`, reuses a passing push-gate record of HEAD on that
  tip or runs `make publication-gate` itself, so a skipped or uninstalled hook never bypasses it.
  Run `make publication-gate` alone to check without pushing; `BASE` defaults to `public/main`, so
  run `git fetch public` first. The gate requires the test records verify-clean writes
  ([which records](docs/test-run-records.md#which-records-the-publication-gate-accepts)).
- Merge tier: `make release-gate` reuses a recorded verify-clean of the same tree instead of
  re-running it ([which record](docs/test-run-records.md#which-verify-clean-record-the-release-gate-reuses)).
  A new commit on the branch changes the tree, so the push and merge tiers run again for it.
- The owner's go: `CONFIRM=yes` on the make command line is the owner's go for that merge. It has
  no default, and a `CONFIRM` set in the environment or carried in an inherited `MAKEFLAGS` does
  not count. `make merge-pr` keeps the
  remote branch unless `DELETE_BRANCH=yes` is given the same way.
- `DRY_RUN=1` on `make publish`, `make pr` or `make merge-pr` runs every check and prints each
  command that would change something instead of running it.

### Maintainer tools

The publication and release gates need these tools at exactly these versions, and refuse any
other version; a row marked `any current` is required but not pinned. This table is the one place
each version is pinned; `scripts/tool_versions.py` reads it.

| Tool | Version | Used by |
|---|---|---|
| gitleaks | 8.30.1 | make publication-gate |
| ASH (automated-security-helper) | 3.7.0 | make release-gate |
| finch or Docker | any current | make release-gate (ASH container mode) |


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels ((enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any ['help wanted'](https://github.com/aws-samples/adlytics-ai/labels/help%20wanted) issues is a great place to start. 


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct). 
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact 
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.

### If a secret is pushed

`make publication-gate` exists so this never happens ([Run the gates](#run-the-gates)). If a secret
reaches the public repository anyway, treat it as compromised from the moment it was public:

1. Rotate first. Revoke or rotate the credential at its issuer, then review the issuer's access
   log for its use since it was published. A revoked credential grants no access, so a history
   rewrite may not be needed at all ([GitHub: removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)).
2. Remove it from history only with the repository owner's explicit approval for that rewrite and
   force push. A rewrite changes every later commit hash, and forks, clones and pull requests can
   still hold the commit; follow the GitHub procedure linked above.
3. Recommended for the repository, as a second line of defence: secret scanning with
   [push protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-push-protection),
   [Dependabot alerts](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/about-dependabot-alerts)
   and [Dependabot security updates](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates).
   A repository administrator enables them.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

We may ask you to sign a [Contributor License Agreement (CLA)](https://en.wikipedia.org/wiki/Contributor_License_Agreement) for larger changes.
