# Test run records — what a recorded run holds and which records a gate accepts

| | |
|---|---|
| **Level** | L2 — quality register |
| **Status** | Live contract; a field changes only with `scripts/run_record.py` (or `scripts/mutation.py` for the mutation sections) in the same change |
| **Last updated** | 2026-10-20 |
| **Owns** | the run-record layout, every field of run.json, tests.json, steps.json, status.json and manifest.json, the live status states, the immutability rule, which records a gate accepts or reuses, the mutation fingerprint, the mutation record files, the semantic proposals format and the mutation coverage map |
| **Conventions** | [Documentation conventions](conventions.md) |

A claim that tests pass needs evidence tied to the exact tree it was made on. Every recorded test
or gate target writes one record: a directory created when the run starts, kept current while it
runs, and never edited, overwritten or merged once it is sealed. Records are local evidence, gitignored under `.runs/`, and never
committed. `scripts/run_record.py` writes, verifies and finds them; [scripts/README.md](../scripts/README.md#run_recordpy)
lists its commands.

## Layout

```text
.runs/<kind>/<UTC start %Y%m%dT%H%M%S.%fZ>-<first 12 hex of the tree>/
  run.json        the run: command, tree, timing, totals, delta; status running until sealed
  status.json     the heartbeat: last update, current step, progress counts
  console.log     the child's stdout and stderr, appended to disk as streamed to the terminal
  tests.json      test kinds: one entry per test (written when the run ends)
  steps.json      gate kinds: one entry per make target or check (written when the run ends)
  raw/            the runner's own reports (junit.xml, pytest.json, vitest.json, jest.json)
  manifest.json   sha256 and size of every other file; its presence seals the record
```

The root is `$RUN_RECORD_ROOT` when set, else `<repository root>/.runs`. Only a directory whose
name matches that pattern is a record, so other files under a kind directory are never read as
one.

## Kinds

| Kind | Written by | Holds |
|---|---|---|
| `pytest-unit` | `make test` | tests.json |
| `frontend-test` | `make frontend-test` | tests.json |
| `infra-test` | `make infra-test` | tests.json |
| `verify` | `make verify` | steps.json; its `test` step names the pytest-unit record it produced |
| `verify-clean` | `make verify-clean` | steps.json; one step whose child records are every record the clean export wrote |
| `publication-gate` | `make publication-gate [BASE=<rev>]` (default `public/main`) | steps.json (the preconditions, then every check); run.json `base`, `refused`, and `gitleaks` added to the common `tool_versions`; export.log holds the export checks' output |
| `release-gate` | `make release-gate` | steps.json (preconditions, verify-clean, ash, checkout, then the report-only mutation-summary); run.json `ash` holds each scan's summary, `mutation_summary` the summary's output and `verify_clean_reused` the `verify-clean/<dir>` record reused instead of running it ([which record](#which-verify-clean-record-the-release-gate-reuses)), else `null`, and `ash` and `container_runtime` are added to the common `tool_versions`; raw/ash/ holds the three ASH `reports/` directories and their logs |
| `mutation` | `make mutate`, `make mutate-next` | steps.json (the baseline, then the tool or one step per semantic proposal) and the files [Mutation records](#mutation-records) lists |
| the target's name | `make test-integration` (`--format pytest`), `make check-alarm-coverage` and `make check-probe-resource-names` (`--format jest-json`) | tests.json |
| the target's name | every `make accept-*`, `make regress-agent`, `make dead-code` (`--format exit`) | console.log and the exit code; tests.json is empty |

Every Makefile test or scan target writes a record. `make check-run-record-coverage`, part of
`make check`, refuses a target that runs tests, a probe or a scanner without the recorder
(scripts/README.md names how it classifies a target).

The canonical command of each test kind and the steps of each gate kind live only in
`CANONICAL` and `CANONICAL_GATES` in `scripts/run_record.py`. A make target passes no command, so
its record is `canonical: true`. A run given its own command (`run --kind K --format F -- argv`)
is always `canonical: false`, whatever its kind; this is how a narrowed or scratch run is recorded
without ever standing in for the full suite. Use a `scratch-` kind for such runs. `--format exit`
records a command that writes no runner report: its outcome is `pass` exactly when it exits 0.

## run.json

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `kind` | the kind directory the record is in |
| `status` | `running` from the start until the record is sealed, then `passed` (outcome `pass`) or `failed`; records sealed before live status have none |
| `pid` | the process that writes the record, whose liveness decides `stalled` against `abandoned` ([Live status](#live-status)) |
| `parent` | the gate record, as `<kind>/<dir>`, this run ran under (`$RUN_RECORD_PARENT`, which each gate sets for its children), else `null` |
| `canonical` | true only when the command came from `CANONICAL` (test kinds) or the steps equal `CANONICAL_GATES` (gate kinds) |
| `argv`, `cwd` | the command as given, with `{staging}` and `{workers}` unexpanded, and its working directory relative to the repository root |
| `workers` | canonical `pytest-unit` runs only: the pytest-xdist worker count the run used, `0` for one process ([Parallel unit runs](#parallel-unit-runs)) |
| `utc_start`, `utc_end`, `duration_s` | wall-clock bounds (UTC, microseconds) and the elapsed seconds |
| `head`, `tree`, `branch` | `HEAD`, `HEAD^{tree}` and the branch of the repository the command ran in |
| `source_commit` | `$RUN_RECORD_SOURCE_COMMIT` when set (the checkout commit a clean export was made from), else `head` |
| `context` | `clean-export` when `$RUN_RECORD_SOURCE_COMMIT` is set, else `checkout` |
| `porcelain_before`, `porcelain_after` | `git status --porcelain` line counts before and after the command |
| `clean` | true only when both counts are 0 |
| `exit_code` | the child's exit code (128 + signal when it was killed); for a gate, the failing step's |
| `outcome` | `pass` only when the exit code is 0, the runner report was parsed, and no test failed or errored; for a gate, when every step exited 0 |
| `report_missing` | true when the runner report was absent or unreadable; such a run never passes |
| `tool_versions` | python, pytest (pytest kinds), node, the vitest, jest and ts-jest versions installed under the run's `node_modules`, uv, git and the platform |
| `totals` | `{tests, passed, failed, skipped, errors}`; `null` for a gate |
| `durations` | `{p50, p95, max}` seconds, nearest-rank, over the tests (over the steps for a gate) |
| `slowest` | the 20 longest tests (steps for a gate) as `{node_id, duration_s}`, longest first |
| `child_records` | gate kinds only: every record, as `<kind>/<dir>`, that appeared under the root during a step |
| `delta` | the change against the latest verified record of the same kind, or `null` when there is none (below) |

### delta

| Field | Meaning |
|---|---|
| `previous`, `previous_tree` | the earlier record as `<kind>/<dir>` and its tree |
| `totals_change` | each total minus the earlier one; `null` for a gate |
| `newly_failing` | node IDs that fail or error now and passed or were skipped before |
| `newly_passing` | node IDs that pass now and failed or errored before |
| `added`, `removed` | node IDs (step names for a gate) present in only one of the two records |
| `p95_change_s` | p95 now minus p95 before, in seconds |

## Live status

`run`, `gate`, the publication gate and the release gate create the record directory when they
start, with run.json `status: running`, an empty console.log and a first status.json. console.log
is opened append-only and every chunk the child writes is appended as it arrives. A heartbeat
thread rewrites status.json every `HEARTBEAT_INTERVAL_S` seconds, and a gate rewrites it when its
step changes. Both constants, and `STALE_AFTER_S`, live only in `scripts/run_record.py`.

| status.json field | Meaning |
|---|---|
| `status` | `running` while the run is in progress; the final status once sealed |
| `pid`, `utc_start` | as in run.json |
| `updated_utc`, `updated_epoch` | when the heartbeat last wrote the file |
| `step` | gates only: the make target, check or stage running now (`ash/<scan>` inside the release gate's ASH stage) |
| `progress` | pytest: `{runner, collected, done, passed, failed, skipped, errors}` from `tests/unit/run_record_report.py`, which rewrites `$RUN_RECORD_PYTEST_PROGRESS` at most once a second; jest and vitest: `{files_passed, files_failed}`, plus `tests_done` and `tests_failed` from vitest's per-file counts, read from the console stream; gates: steps, checks or stages done and total; `null` when the runner reports nothing |
| `console_bytes` | the console.log size so far |

`scripts/run_record.py status` (`make gate-status`) derives each record's state:

| State | When |
|---|---|
| `passed`, `failed` | sealed, and the record verifies |
| `invalid` | sealed, and the record does not verify |
| `running` | not sealed, and the heartbeat is at most `STALE_AFTER_S` old |
| `stalled` | not sealed, the heartbeat is older, and the process still exists |
| `abandoned` | not sealed, the heartbeat is older, and the process is gone (killed, crashed or the machine stopped) |

It prints every running and stalled record and the most recent abandoned and finished ones, with
the kind, elapsed time, heartbeat age, step, progress and, for unsealed records, the last lines of
console.log. Only `passed` and `failed` records verify, so a running, stalled or abandoned record
is never a delta baseline, never returned by `find` and never accepted by a gate. An abandoned
record stays on disk as the evidence of the run that never finished.

## tests.json

One entry per test, in run order; under parallel workers, the order the reports reached the
controller.

| Field | Meaning |
|---|---|
| `node_id` | pytest: the pytest node ID. TypeScript: `<file>::<describe titles joined with " > ">::<title>`, the describe segment omitted when there is none |
| `file` | the test file, relative to the repository root |
| `summary` | pytest: the first line of the test's docstring, else the test name. TypeScript: the test title |
| `decision_ids` | the arguments of the test's `covers` markers; always `[]` for TypeScript, which the coverage matrix does not link |
| `outcome` | `passed`, `failed`, `skipped` or `error` (a pytest setup or teardown failure, a module that failed to collect, a TypeScript file that failed to run) |
| `duration_s` | the call-phase duration in seconds |
| `retries` | the runner's retry count; always 0 for pytest |

For pytest, `tests/unit/run_record_report.py` (registered by `tests/unit/conftest.py`) writes these
entries when the recorder sets `RUN_RECORD_PYTEST_REPORT`. It removes the variable from the environment first, so a pytest run
started by a test never overwrites the outer report, and does nothing when the variable is absent.

## Parallel unit runs

The canonical `pytest-unit` command runs under pytest-xdist with `--dist loadfile`: every test
module runs whole in one worker, so its module and session fixtures behave as they do in one
process, and every worker collects the full suite, so import-time effects are the same in each.
The worker count is bounded, never `-n auto`:

- by default, physical cores minus `PYTEST_WORKERS_RESERVED` (2), capped at `PYTEST_WORKERS_CAP`
  (4), and at least 1;
- `PYTEST_WORKERS=<n>` overrides it, from 0 (one process, no workers) up to the cap, as in
  `make test PYTEST_WORKERS=2`; any other value is refused with exit 2 before a record is made.

Both constants live only in `scripts/run_record.py`, and run.json `workers` records the count used.
The command names `tests/unit` (`UNIT_TESTS`) instead of relying on `testpaths`. The xdist
controller collects nothing, and pytest loads a conftest at startup only under a named path, so
naming the directory is what makes the controller load `tests/unit/conftest.py`. Without it the
controller registers no report plugin and cannot rebuild a worker's warning when the warning's
category is defined in a test module. `tests/unit/test_run_record.py` fails if a unit-marked test
is added outside `tests/unit`.
Under xdist the report plugin runs in the controller: each worker sends back the metadata of the
tests it collected, and the controller writes the one report and progress file, so tests.json,
the totals, status.json `progress` and raw/junit.xml hold the same tests and counts as a one-process
run. The one exception is a module that fails to collect: one process stops before any test runs,
while xdist runs the other modules, so the totals differ, but both runs record the error and fail.
Integration tests (`make test-integration`) and mutation runs stay in one process.

## steps.json

- `verify`, `verify-clean`: one entry per make target the gate ran, stopping at the first
  failure: `{name, argv, exit_code, duration_s, child_records}`.
- `publication-gate`, `release-gate`: one entry per precondition and per check or stage:
  `{name, code, exit_code, duration_s, refusals, child_records}`, where `refusals` is a list of
  `{code, detail}`; release-gate's verify-clean entry adds `reused` when it reused a record. Every check or stage runs whatever an earlier one found; a failed
  precondition stops the rest. release-gate's last entry, `mutation-summary`, carries
  `report_only: true`, `code: null` and a `warning`, and never changes the outcome.
- `mutation`: `{name, argv, exit_code, duration_s}` per step ([Mutation records](#mutation-records)).
  A semantic proposal's `exit_code` is that of its mapped test run, so a killed proposal records
  the failing run's nonzero code. A proposal that was never tested (its patch did not apply or
  touched another file) records `exit_code: null` and `not_run: true`; `argv` names the command
  it would have run. Neither changes the run's `exit_code` or `outcome`.

## Immutability

- The record directory is created with `os.mkdir` when the run starts; when it already exists the
  run is refused (`run` and `gate` exit 3, before the child starts) and nothing in it is touched.
- Until the record is sealed only run.json and status.json (each rewritten whole through a
  temporary file) and console.log (appended) change.
- Sealing writes tests.json or steps.json, raw/, the final status.json and run.json, then
  `manifest.json` (`{schema: 1, files: {<path>: {sha256, bytes}}}`), which lists every other file.
  Then every file is made 0444 and every directory 0555. A sealed record never changes again.
- `scripts/run_record.py verify <dir>` exits 0 only when every listed file exists with its size
  and sha256, no unlisted or non-regular file is present, and run.json's `status`, when present,
  is `passed` or `failed`. A record that does not verify is never used: not as a delta baseline,
  not by `find`, not by a gate.
- `run` and `gate` exit with the child's code. When the child passed but the record could not be
  sealed they exit 70, so a pass without a record is never reported as one; when the record
  cannot be created they exit 70 without starting the child.

## Which records the publication gate accepts

The publication gate requires, for each of `pytest-unit`, `frontend-test` and `infra-test`, a
record that verifies, is `canonical`, `clean`, has outcome `pass`, and whose `tree` equals the
pushed `HEAD^{tree}`. `make verify-clean` writes all three from a clean export of HEAD, so it runs
before the publication gate. A narrowed run (`canonical: false`), a run on a dirty tree, and a
record that is not sealed (running, stalled or abandoned) never satisfy it.

## Which verify-clean record the release gate reuses

The release gate's verify-clean stage reuses the newest `verify-clean` record under
`<repository root>/.runs` that verifies, is `canonical`, `clean`, has outcome `pass`, whose `tree`
equals `HEAD^{tree}`, and that names at least one child record, every one of which verifies too.
It names that record, as `verify-clean/<dir>`, in run.json `verify_clean_reused` and the step's
`reused` field, and lists it with its child records in the step's `child_records`. When no record qualifies it runs `make verify-clean` under the
recorder, so the run it makes is reusable by the next release gate of the same tree.

## Mutation fingerprint

`scripts/mutation.py` computes one fingerprint per unit of `mutation-priorities.yaml`, from HEAD
only; the working tree never counts.

| Field | Value |
|---|---|
| `unit_blobs` | `{path: blob}` for every file the unit mutates, as `git rev-parse HEAD:<path>` names it |
| `test_blobs` | the same for every test file mapped to the unit |
| `tool` | `{name, version}`: `cosmic-ray` and its installed version for a python unit; `stryker` and the version of the package's `node_modules/@stryker-mutator/core` for a TypeScript one (`not-installed` when absent) |
| `operator_config_sha256` | python: the sha256 of the static cosmic-ray config template in `scripts/mutation.py`; TypeScript: the sha256 of the package's tracked `stryker.config.json` in HEAD |
| `semantic_set_version` | the first 12 hex of the sha256 of the bullets under "Which track owns which mutation" in `.kiro/steering/meta-testing-mutation.md`, whitespace-normalised and joined by newlines; every command refuses when the section or its bullets are missing |

The digest is the sha256 of the fingerprint as canonical JSON (sorted keys, no whitespace). A
unit is `current` when its coverage-map entry carries that digest, `stale` when it carries
another, `missing` when it has no entry, and `untested` when it maps no tests. `make mutate`
skips a current unit with `current (record <record>), skipping` and has no option that forces a
rerun. Any changed unit file, mapped test file, tool version, operator config or semantic
category makes the entry stale; moving a unit between tiers or reordering the file does not.

## Mutation records

Every mechanical and every semantic run writes a `mutation` record. Beside the common fields,
run.json holds `unit`, `tier`, `language`, `track` (`mechanical` or `semantic`), `fingerprint`,
`digest`, and `counts` (`generated`, `killed`, `survived`, `timeout`, `no_coverage`, `errors`).
`canonical` is always false, because a mutation run has no canonical command. The wall-clock
bounds, `duration_s` and `porcelain_before` start before the baseline. `report_missing` is true
when the tool wrote no report (the cosmic-ray dump, Stryker's `mutation.json`); `tool_versions`
adds the tool's own version (mechanical runs). `delta` is always `null`, because the coverage map
carries the history. `outcome` is `pass` when the tool completed and wrote its report: survivors
never fail a run, and the score is never a threshold (ADR-013).

| File | Holds |
|---|---|
| `results.json` | one entry per mutant (per proposal for a semantic run): `file`, `line`, `operator`, `mutation`, `status` (`killed`, `survived`, `timeout`, `no-coverage` or `error`) and `killed_by` |
| `survivors.json` | the survivor groups this run produced, with their coverage-map ids and verdicts |
| `semantic-worksheet.md` | mechanical runs: the survivors, the semantic categories and a blank proposals template |
| `raw/` | python: `cosmic-ray.toml`, `session.sqlite` and `cosmic-ray-dump.jsonl` (the JSON lines `cosmic-ray dump` prints, one `[work item, result]` per job); TypeScript: the per-run `stryker.config.json` and the json reporter's `mutation.json`; semantic: `proposals.yaml` |

A cosmic-ray killing test is the first `FAILED` or `ERROR` node ID in the killed job's pytest
output. A StrykerJS one is `<package>/<test file>::<test name>`, from the report's `killedBy`. A
writable copy of each worksheet goes to `.runs/mutation/worksheets/<unit>-<record>.md`.

### Semantic proposals

`make mutate UNIT=<path> SEMANTIC=<file>` reads a YAML list. Every item has exactly `id`,
`category` (one of the worksheet's categories), `description` and `patch`, a unified diff against
HEAD that touches only the unit's files. The run needs a current mechanical entry, and skips when
the same proposals file (by sha256) is already recorded for the current digest. Each proposal is
checked with `git apply --check`, applied, tested with the unit's mapped tests and reverted with
`git apply -R`; a failing test run kills it. A proposal that does not apply, or that touches
another file, is recorded as `error`.

## Coverage map

`.runs/mutation/coverage-map.json` is the one writable file of the mutation tooling: local,
gitignored, written atomically (a temporary file, then a rename), and never a record. It is
`{schema: 1, units: {<unit>: entry}}`, one entry per unit:

| Field | Meaning |
|---|---|
| `unit`, `tier`, `language` | from mutation-priorities.yaml at the last mechanical run |
| `fingerprint`, `digest` | the fingerprint of the last completed mechanical run |
| `record`, `counts` | that run's record (`mutation/<dir>`) and counts; a run whose tool failed leaves the entry unchanged |
| `survivors` | every survivor group ever seen, never deleted or renumbered (below) |
| `semantic_runs` | `{proposals_sha256, digest, record, counts}` per semantic run |

A survivor is `{id, track, file, location, operator, mutation, count, verdict, present,
first_seen, last_seen, history}`, plus `test` when KILLED and `reason` when EQUIVALENT. Mutants
are grouped by file, line and operator, and the group's first mutation text names it. Ids run
from `S-01` upward within the unit, across both tracks. A later run matches an earlier survivor
by (track, file, operator, mutation text), not by line, so moved code keeps its ids and verdicts:

- A match that survives again is `present` at its new location; a KILLED one reopens as OPEN.
- An OPEN survivor whose mutation is now killed becomes KILLED, naming the killing test.
- A survivor the run no longer generates is no longer `present` and keeps its verdict.

`make mutation-verdict` changes one verdict in place: KILLED requires `TEST=` naming a test whose
file exists, and EQUIVALENT requires `REASON=`. Every change is appended to the survivor's
`history`.
