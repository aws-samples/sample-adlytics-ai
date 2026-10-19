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
| **Suppression markers** | in `.py`, `.ts`, `.tsx`, `.js` and `.mjs` files, a Bandit `nosec` names only reviewed test IDs (`B310`, `B505`, `B506`, `B608`) and a Semgrep `nosemgrep` names namespaced rule IDs; a bare or misspelt marker would suppress every finding on its line. Each reviewed marker's reason is in `docs/security-dispositions.md` |

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
