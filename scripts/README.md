# scripts

Run the documentation and repository-consistency checks with `make check` from the
repository root; `make help` lists the current targets, so this document does not
copy a count or roster that can drift. Each check exits non-zero on a finding.

`check-lineage.py` is the one that runs under `uv` rather than system python,
because it parses YAML.

`check-diagrams.mjs` needs Node dependencies pinned in `scripts/package.json`
(`mermaid`, `jsdom`). `make check-diagrams` installs them on first run.

## check-design-manifest

Generates the committed Decision Workspace manifest from `docs/ui/design-manifest.yaml` and
validates its README/mockup/gallery revision alignment, parent disposition, shared-owner
closure, and SHA-256 digests. `make check-design-manifest` verifies the committed projection;
`make require-approved-design-manifest` additionally refuses to authorize implementation until
the declared parent disposition is `approved_for_implementation`.

```bash
make generate-design-manifest
make check-design-manifest
make require-approved-design-manifest
```

## check-coverage-matrix.py

Reports which recorded decisions have no test referencing them, and fails on a
test that references a decision that does not exist.

```bash
python3 scripts/check-coverage-matrix.py [--strict]
```

Decision IDs are read from where each family is declared — `ADR-NNN` from
`docs/decisions/`, `G-NN` and `F-NN` from `docs/decision-coverage-registry.md`.
Test references come from
`@pytest.mark.covers(...)` markers, parsed with the AST rather than by running
pytest, so it is fast and has no import side effects.

> **There is deliberately no per-test ID.** The pytest node ID is already a
> stable, unique identifier derived from the code, so it cannot drift. A
> hand-assigned ID would be a second identifier for the same thing, maintained by
> hand, and therefore eventually wrong.

Two behaviours worth knowing:

- **Exits 0 when decisions are merely unreferenced.** That is a backlog, not a
  build failure. `--strict` flips it, for once coverage is expected to be
  complete.
- **Exits non-zero on an unknown ID.** A marker naming `ADR-999` is a typo or a
  deleted decision, and both are defects.

Open questions (`Q-NN`) are excluded on purpose. An unresolved question has no
decision behind it, so counting it would inflate the gap with items no test can
close. When a `Q` resolves it becomes an ADR, which is tracked.

## check-steering.py

Enforces the mechanically decidable steering-file contract for every Markdown file
in `.kiro/steering/`.

```bash
uv run python scripts/check-steering.py
```

| Check | Rule enforced |
|---|---|
| **Front matter** | file opens with one exact, terminated YAML block with unique keys |
| **Mode schema** | `inclusion` is `always`, `auto`, `fileMatch`, or `manual`, with only the fields valid for that mode |
| **Auto identity** | every `auto` file has a unique lowercase dashed `name` |
| **File matching** | patterns form a nonempty unique list, stay inside the repository, and currently match a file |
| **Description** | every description is a unique one-sentence concern and mode-appropriate activation boundary |
| **Plain text** | body contains no Markdown tables, Markdown links, or bold text |
| **Path citations** | a prose path under a known top-level directory exists, is tracked (not merely present on disk), is not gitignored custody, and any `§N` after it matches a heading |

The checker enforces format and deterministic front-matter properties. Semantic
actionability, concern ownership, copied facts, and Kiro auto-selection quality
remain review responsibilities defined by `.kiro/steering/meta-style-steering.md`.
Anything a script can decide belongs in this gate rather than duplicated steering
prose.

Front matter is exempt from the plain-text check, and fenced blocks are excluded
so examples are not mistaken for body formatting.

## check-links.py

Validates every internal link and heading anchor in every tracked markdown file,
plus everything under `.kiro/` (and local working documents when present). Without
git it falls back to the directories that carry published markdown.

```bash
python3 scripts/check-links.py
```

Catches the two failure modes markdown tooling misses: a link to a file that was
renamed or deleted, and a section anchor that stopped matching after a heading was
renumbered. Links inside code fences are skipped as illustrative.

Everything under `.kiro/` is in scope because it links into `docs/`, and a broken
link there fails silently — it surfaces as the agent quietly losing a rule, or
being sent to a file that no longer exists, rather than as a visibly broken page.

It also fails when a tracked file links to a gitignored path, or to a file that
exists locally but was never staged: both resolve on this machine and rot for
every clone.

## check-debt-markers.py

Keeps debt in code visible and owned. In tracked code under `src/`, `infra/lib/`, `infra/bin/`,
`frontend/` and `scripts/` (Markdown, lockfiles, generated and vendored paths excluded) it refuses
the four task-marker words and checks every `retire:` comment marker. Code meant to be deleted, such
as a dual-format read, a compatibility shim or a legacy-path branch, carries one marker line:

```text
# retire: <removal condition>; owner: <tracked doc path>[#anchor]; id: <RETIREMENT-ID>
```

The owner is the tracked document that decides the condition (an ADR, a design doc or a retirement
record under `docs/`). It must be a tracked Markdown file under `docs/`, held by git as a regular
file and reached through no symbolic link, so untracked content cannot stand in as the record; an
`#anchor` must name one of its headings (the `check-links.py` slug rule); and the owning record, the
anchored section or the whole document, must carry one retirement clause line per retired file:

```text
- retire-id: <RETIREMENT-ID>; path: <the marker's repository path>; when: <removal trigger>
```

The identifier (upper case, hyphenated, such as `RETIRE-V1-READ`) binds marker and record, so a
document that only mentions the path, or states no trigger, does not own the code, and the two
condition texts can be worded independently. A marker without a condition, an owner or an
identifier, or failing any of those, fails. An in-scope file the gate cannot read (invalid UTF-8,
or missing from the checkout) is a finding too, so no tracked code passes unexamined. `make check`
runs it; `make report-retire-markers` lists every marker with its file:line, condition, owner and
identifier.

## check-publication-boundary.py

Scans every tracked text file (lockfiles and files `.gitignore` already claims are
skipped) for what must never be published.

```bash
uv run python scripts/check-publication-boundary.py
```

| Check | Rule enforced |
|---|---|
| **Custody files** | no reference to a specific file under `internal/`, `.kiro/specs/`, or a `local-*.md` steering file; naming the directory or glob is designation and passes |
| **Account IDs** | no 12-digit account-shaped value except AWS documentation placeholders, repeated-digit placeholders, and the public Lambda Web Adapter publisher account |
| **Profiles** | no literal value after `PROFILE=`, `AWS_PROFILE=`, or a command-line `--profile`; commands carry a placeholder |
| **Encoded JWTs** | no encoded JWT or JWE header literal (`eyJ…` followed by a second segment); fixtures build tokens from readable parts with `tests/unit/fictional_tokens.py` |
| **Generated-looking secrets** | no quoted literal of 20+ base64url characters (letters, digits, `-`, `_`) with letters in both cases and two or more digit runs between letters; a fictional secret is spelled as words (`Fictional-Agent-Secret-For-Tests`). A backstop for that convention, not a secrets scanner: standard-base64 (`+`, `/`, `=`), single-case and single-digit-run values, unquoted `KEY=value` or `key: value` assignments and AWS access key IDs pass it, and are left to the publication scan's secrets rules |
| **Suppression markers** | in `.py`, `.ts`, `.tsx`, `.js` and `.mjs` files, a Bandit `nosec` names only reviewed test IDs (`B105`, `B106`, `B310`, `B505`, `B506`, `B608`) and a Semgrep `nosemgrep` names namespaced rule IDs; a bare or misspelt marker would suppress every finding on its line. Each reviewed marker's reason is in `docs/security-dispositions.md` |

It complements `check-links.py`, which sees only markdown links.

> **The slug rule that bites.** GitHub maps *each* space to a hyphen rather than
> collapsing runs, so `## 6. Internal structure — the canonical model` becomes
> `#6-internal-structure--the-canonical-model` with a double hyphen once the em-dash
> is stripped and both surrounding spaces survive. Collapsing whitespace produces a
> plausible-looking slug that never matches.

## check-diagrams.mjs

Parses every inline ` ```mermaid ` block and validates every SVG diagram file and its
image reference in `README.md` and `docs/` (plus local working documents when present),
and fails on any finding, reporting the file, block number and approximate line.

```bash
node scripts/check-diagrams.mjs
```

| Check | Rule enforced |
|---|---|
| **Mermaid** | only `sequenceDiagram` or `flowchart`/`graph`, under a bold caption naming a Sequence or Call-flow diagram, and parseable by `mermaid.parse` |
| **No inline SVG** | no `<svg>` in Markdown outside code, and no `<img>` pointing at an `.svg` |
| **SVG file** | lives in a `diagrams/` directory and meets the docs/conventions.md §6 file requirements, including the full-`viewBox` white background `<rect>` |
| **Image reference** | a Markdown image targeting `diagrams/<name>.svg` beside the referencing doc, alone on its line, after a bold caption and before a `> **Takeaway.` block, with alt equal to the file's title and description |
| **Ownership** | every diagram file is referenced by exactly one document; an unreferenced file is a finding |

A diagram is invisible to Markdown tooling until someone opens the doc, so a broken
one, or one GitHub silently strips, would otherwise go unnoticed. `check-links.py`
separately confirms each referenced file exists and is tracked.

## audit-model.py

Cross-document consistency audit for the canonical model. Exits non-zero on any
finding, so it can run in CI.

```bash
python3 scripts/audit-model.py
```

What it checks:

| Check | Rule enforced |
|---|---|
| **Ownership** | every `dim_*` / `fct_*` / `snap_*` referenced anywhere in `docs/` (plus local working documents when present) is defined in `canonical-model.md` |
| **Bus matrix** | every fact defined in `canonical-model.md` has a row in `capabilities/data-layer/architecture.md`, and vice versa |
| **Domain agreement** | a fact is assigned to the same domain in `canonical-model.md`, `capabilities/data-layer/architecture.md` and ADR-003 |
| **Grain in names** | no name encodes grain (`_day`, `_daily`, `_hour`…) |
| **Channel neutrality** | conformed dimensions carry no channel token; facts only as a subtype suffix |
| **ASIN leakage** | no conformed dimension is named after a channel-specific identifier |

It understands two things that would otherwise produce false positives:

- **Counter-examples.** The docs teach by contrast — ``"`dim_product` ✓ · `dim_asin` ✗"`` and
  ``"`fct_retail_sales`, not `fct_vendor_central_sales`"``. Identifiers written as
  deliberate bad examples are excluded.
- **Channel subtype facts.** A fact ending in a channel token (e.g.
  `fct_attributed_conversion_amazon`) is reached by drill-across and is not
  expected to have a bus matrix row.

Run it after any change to the model or its naming. It catches the class of drift
that is invisible on review: a rename applied in two docs out of three.

## check-lineage.py

Traces every column from source dataset to canonical fact to metric. Exits non-zero
on any disagreement.

```bash
uv run python scripts/check-lineage.py     # or: make check-lineage
```

Three artefacts have to agree and nothing checked that they did:

| Artefact | Claims |
|---|---|
| `contracts/*/*.yaml` | what each contract writes, and where each column comes from |
| `docs/canonical-model.md` | what each fact's columns actually are |
| `contracts/shared/metric-dictionary.yaml` | what metrics read from those facts |

`make validate-contracts` is the complementary gate: it checks a contract against the
AMC column *registry*, so it catches a source column that does not exist. It cannot
see the other direction.

| Check | Rule enforced |
|---|---|
| **Table declared** | a contract's `target_table` is defined in `canonical-model.md` |
| **Column declared** | every `target_column` a contract writes is declared by that table |
| **Metric parity** | `canonical-model.md` §5 and the dictionary name the same metric set |
| **Metric backing** | every metric reads a fact some contract writes, and a column it produces |
| **Ratio grain** | a ratio's `valid_grains` is a subset of **both** its operands' |

> **The ratio-grain check is the one that earns its place.** Three ROAS metrics
> declared a `product` grain their denominator could not supply, and `roas_blended`
> also declared `channel`, which retail revenue has no dimension for. Each would have
> passed the `valid_grains` allowlist and then divided figures that do not correspond —
> a plausible number, which is the exact failure the allowlist exists to prevent.

Two deliberate exemptions, both narrow:

- **Load bookkeeping** — `report_type`, `period_start`, `report_options_hash`,
  `retrieved_at`, the three suppression columns and friends are provenance rather than
  analytical columns, so `canonical-model.md` does not enumerate them per fact.
- **Deferred metrics** — a metric under `deferred:` is declared and knowingly not
  computable, so it is exempt from the backing check but *not* from parity. It must
  still be documented, or nobody reviewed it.

Run it after any change to a contract, the canonical model, or the metric dictionary.
It catches the drift that is invisible on review: a contract and a doc that describe
different schemas while each looks internally correct.

## run_record.py

Writes, verifies and finds the immutable run records under the gitignored `.runs/`;
[docs/test-run-records.md](../docs/test-run-records.md) owns the layout and every field. `make test`,
`make frontend-test`, `make infra-test`, `make verify` and `make verify-clean` call it with no
command, so their records are canonical. A narrowed run passes its own command after `--`, names
the report format, and is recorded `canonical: false`; the argv writes its report to `{staging}`,
which the recorder replaces with its staging directory.

```bash
uv run python scripts/run_record.py run --kind scratch-pytest --format pytest -- \
  python -m pytest -m unit tests/unit/test_run_record.py
uv run python scripts/run_record.py run --kind scratch-vitest --format vitest-json --cwd frontend -- \
  npx vitest run test/display-value.test.ts --reporter=json '--outputFile.json={staging}/vitest.json'
uv run python scripts/run_record.py verify .runs/<kind>/<record>
uv run python scripts/run_record.py find --kind pytest-unit --tree "$(git rev-parse 'HEAD^{tree}')" --passing --canonical --clean
make gate-status RECENT=5 LINES=5   # run_record.py status: in-progress and recent records
```

`--format exit` records a command without a runner report (an acceptance probe, a report-only
scan); the Makefile's acceptance targets use it with their own name as the kind.

A record exists from the moment its run starts ([live status](../docs/test-run-records.md#live-status)):
`make gate-status` shows what is running now, its step, progress and last log lines, and which
records were abandoned. `run` and `gate` exit with the child's code, 3 when the record directory
already exists, and 70 when the record cannot be created (the child never starts) or the child
passed but the record could not be sealed. `verify` exits 0 only when the record is sealed and
matches its manifest; `find` exits 1 when no verified record matches.

## check-run-record-coverage.py

`make check-run-record-coverage` (part of `make check`) refuses a Makefile target that runs tests
or a scan without writing a run record. A target is a test or scan target when its name is `test`,
`test-*`, `*-test`, `verify`, `verify-clean`, `accept-*`, `regress-*`, `mutate`, `mutate-*`,
or `*-gate`, or when its recipe runs pytest, jest, vitest, `npm test`, cosmic-ray,
Stryker, `ash scan`, gitleaks, pip-audit, `npm audit` or vulture. Its recipe must then run
`scripts/run_record.py run|gate`, the publication or release gate, or
`scripts/mutation.py run|next`. The script's `EXEMPT` names each matched target that writes no
record, with its reason; a stale exemption fails too.

## mutation.py

Mutation coverage, one unit of the root `mutation-priorities.yaml` per run: cosmic-ray for
python, StrykerJS for `frontend/` and `infra/` (each package's tracked `stryker.config.json`).
The [mutation steering](../.kiro/steering/meta-testing-mutation.md) owns when it runs. Every run
writes a `mutation` run record and updates the local coverage map;
[docs/test-run-records.md](../docs/test-run-records.md#mutation-fingerprint) owns the
fingerprint, the record files, the proposals format and the map. A unit whose entry is current is
skipped, and nothing forces a rerun.

```bash
make mutate UNIT=src/domain/identity/caller.py                         # mechanical pass
make mutate UNIT=src/domain/identity/caller.py SEMANTIC=proposals.yaml # semantic proposals
make mutate-next N=3                                                   # next missing or stale units
make mutation-status                                                   # per tier; report only
make mutation-verdict UNIT=src/domain/identity/caller.py ID=S-01 VERDICT=EQUIVALENT \
  REASON="dataclass slots argument"
uv run python scripts/mutation.py propose > /tmp/priorities.yaml       # import-mapped proposal
```

A run refuses a dirty tracked tree and a red baseline of the mapped tests. After a python run it
restores the unit from HEAD if cosmic-ray left it mutated, and says so; any other dirty file
stops it with exit 4 and is left untouched. `propose` prints the D7 tiers with each unit mapped to
the test files that import it (python: AST imports and module-name strings; TypeScript: import
specifiers through the `@/` alias, relative paths and index files). It printed the first
`mutation-priorities.yaml`, which is authored from then on.

## verify-clean.sh

`make verify-clean` exports exactly what HEAD tracks into a temporary directory and runs
`make verify`, `make check`, `make infra-test` and the frontend gates there, so a file that is
ignored, untracked or only present locally cannot make them pass. Its run records name HEAD's
tree; the publication gate requires them, and the release gate reuses the verify-clean record
([test run records](../docs/test-run-records.md#which-records-the-publication-gate-accepts)).

## publication_gate.py

`make publication-gate [BASE=<rev>]` runs it before every push to the public remote; BASE defaults
to `public/main` (`DEFAULT_BASE`). It refuses a
dirty tree, a BASE that HEAD does not descend from, and a gitleaks other than the pinned one, then
runs every check on BASE..HEAD whatever an earlier one found, and prints one line per check or per
finding with its refusal code. The module docstring lists every code. The last check is
`check-publication.sh`, the export stage. Every run writes a `publication-gate` run record. The
gate reads two operator-local inputs: the gitignored `local-denylist.txt` (one literal per line,
matched case-insensitively and never printed; the gate refuses with `DENYLIST_MISSING` when it is
absent and `DENYLIST_EMPTY` when it has no entry, so a clone without one cannot pass the push
tier) and the email allowlist
(`git config --add publication.allowedEmail <email>`, falling back to `user.email`). The denylist
also matches patch headers, so listing an internal tool's name there refuses a commit that adds
its output directory on any clone; a tracked ignore rule would publish the name.

The gate reads no remote and scans only `BASE..HEAD`. It proves BASE is an ancestor of HEAD, not
that BASE is the current public main, so run `git fetch public` first (`make publish` fetches and
passes the tip itself); a later BASE leaves the commits before it unscanned. Tags are outside the range and are never
pushed to the public remote ([meta-delivery-git](../.kiro/steering/meta-delivery-git.md)).

## commit_gate.py and .githooks/

The tracked hooks in `.githooks/` are active only after the owner runs `make install-hooks` once
per clone. `scripts/install-hooks.sh` writes one stub per hook into the repository's own hooks
directory (`$(git rev-parse --git-common-dir)/hooks/`, shared by every worktree); each stub runs the
tracked hook of the checkout it fires in. It never sets or unsets `core.hooksPath` and changes no
git configuration, so a system-level hooksPath set by the machine's security tooling stays in force
and runs these hooks after its own. It is idempotent, refuses to overwrite a hook it did not write
(naming it, installing nothing), and `make uninstall-hooks` removes only its own stubs. No agent or
gate runs it.

- `pre-commit` runs `commit_gate.py` on the index, not the working tree: the pinned gitleaks on the
  staged changes, the publication gate's forbidden-path and ignore rules on staged additions and
  changes, `local-denylist.txt` on the staged diff, the author and committer identity git will
  record, and `ruff check` plus `ruff format --check` on staged `.py` files under `src/` and
  `tests/`. It reuses the publication gate's rules, so the two tiers cannot disagree on a path, a
  denylist entry or an email. The module docstring lists every code. An absent or empty denylist
  prints a warning here; the push tier refuses it.
- `pre-push` runs `make publication-gate` once per distinct BASE of the pushed refs: the remote's
  current tip of the ref, or `<remote>/main` for a ref the remote lacks. It refuses a pushed ref
  that is not HEAD (the gate examines HEAD), a remote tip this clone has not fetched, and any tag.
  A ref deletion is not gated.

`git commit --no-verify` and `git push --no-verify` skip the hooks; [CONTRIBUTING](../CONTRIBUTING.md#run-the-gates)
says why that never bypasses the push tier.

## release_gate.py

`make release-gate` runs it on a pull request's head before `make merge-pr`, on a clean
checkout ([CONTRIBUTING](../CONTRIBUTING.md#run-the-gates) owns the order). It refuses a dirty tree, a missing ASH pin, and a missing
`uvx` or container runtime (finch or Docker), then runs every stage whatever an earlier one
found: verify-clean, whose run records the publication gate requires; three ASH container
scans of a `git archive HEAD` export (trivy-repo, ASH's default source scanners, and the templates
`infra/scripts/export-scan-templates.ts` writes, with cdk-nag, cfn-nag, checkov and
detect-secrets); and a check that HEAD, its tree and the working tree did not change. ASH refuses
on a run that exits non-zero, a scanner reporting MISSING or ERROR, a requested scanner that did
not run, a report from another ASH version, and any open CRITICAL or HIGH finding after the
committed `.ash/` suppressions; a finding is printed as scanner, severity, rule, file and count.
The module docstring lists every code. The verify-clean stage reuses a recorded verify-clean of HEAD's tree
([which record](../docs/test-run-records.md#which-verify-clean-record-the-release-gate-reuses)),
naming it on a `verify-clean reused` line, and runs `make verify-clean` only when there is none. Last, it prints `scripts/mutation.py status --summary`,
which is report only: its failure is a warning and never changes the exit code. The scan staging
lives under the run-record root (`.runs/.staging/`, removed afterwards) because a container
runtime VM may share only the home directory. ASH, the template export and the mutation summary
run without any `ADLYTICS_*` or `AWS_*` variable and without the other variables Finch forwards
into its VM (`CONTAINER_FORWARDED`), because scanning a tree needs no AWS access; the ASH command
mounts only the export and the output directory, never `~/.aws`. Every run writes a `release-gate` run record with
each step, the ASH summaries, the three ASH `reports/` directories and the child records.

## pr_flow.py

The push and merge path to the public remote, in three subcommands. The remote and its main come
from the publication gate's `DEFAULT_BASE` (`public/main`); the remote's URL stays in the
operator's git configuration, and the GitHub OWNER/REPO given to `gh` is read from it at run time.
The module docstring lists every refusal code. Each subcommand removes every `ADLYTICS_*` and
`AWS_*` variable first, and passes every line it prints through the publication gate's denylist
redaction.

```bash
make publish [DRY_RUN=1]
make pr [TITLE=<subject>] [DRY_RUN=1]
make merge-pr PR=<n> CONFIRM=yes [DELETE_BRANCH=yes] [DRY_RUN=1]
```

- `publish` pushes HEAD to `refs/heads/<current branch>` with no tags and no force, so the remote
  accepts only a fast-forward. It refuses a dirty tree, a detached HEAD, main, any `release/*`
  branch, and a branch whose history does not contain the freshly fetched `public/main`. It reuses
  a passing, canonical, clean `publication-gate` record of HEAD on that tip, or runs
  `make publication-gate` with it as BASE. It then runs the gate's denylist and identity checks
  again on the range, and the denylist on the branch name, because a pushed branch name is public.
  After the push it checks that the remote branch is HEAD and that the remote has no tags.
- `pr` needs the remote branch to equal HEAD. It edits the open pull request of the branch from
  this repository, never one from a fork, or creates one into main. The title is `TITLE`, else
  the subject of the branch's only commit, else the branch name. The title becomes the squash
  commit's subject on main. The body lists the commits after `public/main` and the newest sealed
  verify-clean, publication-gate and release-gate record of HEAD's tree, as a path under the
  checkout and its outcome. The title and body pass the denylist before `gh` runs.
- `merge-pr` runs `gh pr merge <n> --squash --match-head-commit <HEAD>` only when `CONFIRM` is
  exactly `yes`, the tree is clean, and the pull request is open, merges into main, has local HEAD
  as its head, is mergeable and not behind, blocked or a draft, and has GitHub's main in its
  history. It also needs a sealed, passing, canonical, clean `release-gate` record of the head's
  tree, and needs the squash's title, body and commit messages to pass the denylist. After the
  merge it fetches main and refuses when main did not move or its new commit's tree is not the
  head's tree. Otherwise it fast-forwards local main, and deletes the remote branch only with
  `DELETE_BRANCH=yes`. `make merge-pr` passes `CONFIRM` and `DELETE_BRANCH` only when they are
  given on the make command line, so an exported variable is never the owner's go.
- `DRY_RUN=1` still runs the read-only queries (`git status`, `rev-parse`, `ls-remote`,
  `gh pr list`, `gh pr view`) and every check, so it reports the refusals a real run would. It
  prints each command that would change something (the fetch, the gate, the push, `gh pr create`,
  `gh pr edit`, `gh pr merge`, the fast-forward and the branch delete) instead of running it.

`tests/unit/test_pr_flow.py` runs against a local bare repository and a `gh` stub; no test reaches
GitHub or pushes.

## tool_versions.py

Reads the [Maintainer tools](../CONTRIBUTING.md#maintainer-tools) table, the one place a binary
tool's version is pinned, for the gates that refuse any other version. A row marked
`any current` is required but not pinned, and `pinned()` refuses it.
`uv run python scripts/tool_versions.py gitleaks` prints the pin.

## infra/scripts/check-iam-policy-size.ts

Applies the IAM policy-size check of `infra/test/iam-policy-size.test.ts` (which `make infra-test`
runs on the test configuration) to a `cdk synth` output directory, such as the credential-free dev
synth. It renders each policy document as IAM counts it and fails a role whose inline aggregate,
or a managed policy whose document, is within the headroom of IAM's limit, and any CDK overflow
policy. Reads templates only; calls no AWS API.

```bash
cd infra && npx ts-node scripts/check-iam-policy-size.ts <SYNTH_DIR> --account <12 digits> --region <region>
```

## infra/scripts/export-scan-templates.ts

Writes every synthesised stack template (`everyTemplate()`, the infra test configuration) to a
directory outside the repository, for IaC scanners such as cfn-nag and Checkov, which skip
`cdk.out`. Run `make build-lambdas` first.

```bash
cd infra && npx ts-node scripts/export-scan-templates.ts /tmp/scan-templates
```

These are test-configuration templates (account `111122223333`, `eu-west-1`, every optional stack
provisioned), not a dev synth: they include resources a us-east-1 dev synth does not have, so
finding counts are not comparable with a scan of dev templates; compare per-rule dispositions. The
script refuses a missing directory argument and any directory inside the repository. Each CDK
asset `S3Key` that is a content hash (`<64 hex>.zip` or `.json`) is written as `cdk-asset.zip` or
`cdk-asset.json`, so a secret scanner does not read the hash as a key (A-ASSET-HASH-001); nothing
else in a template changes, which `infra/test/export-scan-templates.test.ts` pins. The script also
writes `<OUT>/.ash/.ash.yaml`, the ASH config for scanning `<OUT>`: `.ash/templates.ash.yaml` plus
one secret suppression per template line whose whole content is a reviewed false-positive shape
(a CDK logical ID, a Lambda log group name, a named SHA-256 digest, a public token audience or the
`ClientSecretSource` field). A secret-scanner match on any other line stays open.
