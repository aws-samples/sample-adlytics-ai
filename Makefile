.DEFAULT_GOAL := help

# The protected evaluation probe always targets the current evaluation families and requires
# the exact result-link stack.
# Every AWS-touching target requires PROFILE explicitly. No default: a defaulted
# profile in a template customers deploy silently targets whichever account that
# name resolves to on the machine running it.
define require_profile
@test -n "$(PROFILE)" || (echo "PROFILE is required, e.g. make $@ PROFILE=my-profile"; exit 1)
@test "$(PROFILE)" != "default" || (echo "PROFILE must name an explicit nondefault profile"; exit 1)
endef

# Same defect class as a defaulted profile. ENV names the deployment: it is a
# segment of every SSM parameter path, part of the secret name, and the tail of
# every stack ID. Defaulting it to 'dev' means an operator whose environment is
# 'acme-staging' and who omits ENV reads and writes a different environment's
# configuration while every command still succeeds.
define require_env
@test -n "$(ENV)" || (echo "ENV is required, e.g. make $@ ENV=my-env"; exit 1)
endef
.PHONY: help check check-links check-diagrams check-design-manifest require-approved-design-manifest check-model check-steering check-config-access check-prompt-text check-conformed-columns check-observability-bans check-exception-handling check-aws-pagination check-publication-boundary check-license-headers check-debt-markers check-run-record-coverage check-format report-retire-markers \
        check-bakeability \
        install lint format typecheck lint-boundaries validate-contracts validate-page-schemas test test-integration \
        check-lambda-layer-reproducibility verify verify-clean verify-clean-run gate-status publication-gate release-gate install-hooks uninstall-hooks publish pr merge-pr accept-ingestion-v2 accept-campaign-estate-local accept-evaluation-link print-deploy-flags accept-evaluation-custody regress-agent mutate mutate-next mutation-status mutation-verdict dead-code dead-code-allowlist build-lambdas build-frontend \
        generate-design-manifest generate-schemas check-schemas generate-fixtures check-fixtures \
        generate-tool-schemas check-tool-schemas generate-trace-contract check-trace-contract generate-data-flow-registry check-data-flow-coverage generate-execution-graph-protocol check-execution-graph-protocol check-execution-graph-deployable generate-alarm-destinations check-alarm-destinations generate-producer-packs check-producer-packs \
        generate-parameter-contract check-parameter-contract \
        frontend-typecheck frontend-test create-user provision-quality-review-users accept-quality-review backfill-record-expiry supersede-orphan-units migrate-review-groups rotate-origin-verify accept-edge accept-guardrail accept-managed-harness accept-reprocess-telemetry infra-typecheck infra-test check-alarm-coverage check-probe-resource-names synth diff deploy deploy-all destroy-all \
        dev-state check-alarm-window check-serving-refusals check-staged-tree diff-then-deploy deploy-all-locked check-source-key deploy-lock-force-release

# ---------------------------------------------------------------------------
# Documentation checks
# ---------------------------------------------------------------------------

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

check: check-links check-diagrams check-design-manifest check-insight-matrix check-model check-lineage check-schemas check-fixtures check-tool-schemas check-trace-contract check-data-flow-coverage check-execution-graph-protocol check-alarm-destinations check-producer-packs check-parameter-contract validate-page-schemas check-bakeability check-steering check-coverage-matrix check-config-access check-prompt-text check-conformed-columns check-construct-levels check-observability-bans check-exception-handling check-aws-pagination check-publication-boundary check-license-headers check-debt-markers check-run-record-coverage check-format ## Run every documentation check

check-links: ## Validate internal links and heading anchors in every tracked markdown file (plus local working documents when present)
	@uv run python scripts/check-links.py

check-diagrams: ## Validate Mermaid blocks, SVG diagram files and their image references in README.md and docs/ (plus local working documents when present)
	@test -d scripts/node_modules || (cd scripts && npm install --silent)
	@node scripts/check-diagrams.mjs

check-design-manifest: ## Verify the committed Decision Workspace design-manifest projection
	@uv run python -m adapters.jobs.design_manifest --check

require-approved-design-manifest: check-design-manifest ## Refuse implementation until the parent disposition is approved
	@uv run python -m adapters.jobs.design_manifest --check --require-approved

generate-design-manifest: ## Generate the committed Decision Workspace design manifest
	@uv run python -m adapters.jobs.design_manifest
check-insight-matrix: ## Verify the committed insight deterministic-logic matrix against contracts/insights
	@PYTHONPATH=src uv run python -m adapters.jobs.insight_matrix --check
generate-insight-matrix: ## Generate the committed insight deterministic-logic matrix
	@PYTHONPATH=src uv run python -m adapters.jobs.insight_matrix

check-model: ## Audit canonical model naming, ownership and domain consistency
	@uv run python scripts/audit-model.py
# Runs under uv because it parses YAML. validate-contracts checks a contract against the
# AMC column registry; this checks it against the canonical model and the metric
# dictionary, which is the half that produces a plausible number rather than a failure.
check-lineage: ## Trace every contract column to a canonical fact and every metric to a contract
	@uv run python scripts/check-lineage.py

check-steering: ## Verify steering front matter and plain-text format
	@uv run python scripts/check-steering.py

check-coverage-matrix: ## Report which recorded decisions have no test
	@uv run python scripts/check-coverage-matrix.py

check-construct-levels: ## Verify the modules ADR-016 recorded as L1-only still are
	@cd infra && node scripts/check-construct-levels.cjs

check-config-access: ## Verify only adapters/config.py reads the environment
	@uv run python scripts/check-config-access.py

check-observability-bans: ## Verify no banned telemetry API usage in src/
	@uv run python scripts/check-observability-bans.py

check-exception-handling: ## Verify every broad or alarm-class except block in src/ propagates its error
	@uv run python scripts/check-exception-handling.py
check-aws-pagination: ## Refuse an AWS list or describe call in src/ or validation/ that reads only its first page
	@uv run python scripts/check-aws-pagination.py

check-publication-boundary: ## Refuse custody file references, account IDs and literal profiles in tracked content
	@uv run python scripts/check-publication-boundary.py

check-prompt-text: ## Verify no prompt text lives under src/ (registry is the only path)
	@uv run python scripts/check-prompt-text.py

check-license-headers: ## Verify exact SPDX markers on authored and owned generated sources
	@uv run python scripts/check-license-headers.py

check-debt-markers: ## Refuse task markers in tracked code and retire markers without a condition and a tracked owner
	@uv run python scripts/check-debt-markers.py

report-retire-markers: ## List every retire marker with its file:line, removal condition and owning doc
	@uv run python scripts/check-debt-markers.py --report

check-run-record-coverage: ## Refuse a Makefile test or scan target that bypasses the run recorder
	@uv run python scripts/check-run-record-coverage.py

# Runs under uv because it reads the column list from the domain rather than restating it.
# check-lineage cannot cover this: it walks a contract's field mappings, and these columns
# are projected from contract-level metadata, so they are invisible to it.
check-conformed-columns: ## Verify every fact carries basis_key and source_timezone
	@uv run python scripts/check-conformed-columns.py

# The gate over contracts/. Rules are in src/domain/data_contracts/validation.py and are
# unit tested, so this target is a thin runner rather than the logic. Named as the gate by
# the data-contract-definition steering.
validate-contracts: ## Validate every data contract against the AMC column registry
	@uv run python -m adapters.jobs.contract_validation
# Analytical page declarations are presentation contracts, not source data contracts. The gate
# resolves their metric keys through the governed catalog and compares every declared page, grain,
# section and evidence action with the generated frontend page-binding projection.
validate-page-schemas: ## Validate analytical page schemas and static rendering bindings
	@uv run python -m adapters.jobs.presentation_validation
# ADR-070 point 8: a declared preset is a checked promise. Proves every declared preset of
# every artifact-served page fits one per-pack bake snapshot and the baker's time budget.
check-bakeability: ## Prove every declared artifact-served preset fits its bake bounds
	@uv run python -m adapters.jobs.preset_artifact_publisher.bakeability
# The generated schemas are checked against their governed declarations so they cannot drift.
# contracts/generated/tables.json is consumed by the V2 data plane, and readers validate against
# the Pydantic row models in src/domain/data_contracts/generated_rows.py.
generate-schemas: ## Generate table definitions and row models from the contracts
	@uv run python -m adapters.jobs.schema_generation
check-schemas: ## Fail if a generated schema artefact is stale against the contracts
	@uv run python -m adapters.jobs.schema_generation --check
# Fixtures are source-shaped and generated for the same reason the schema is: a hand-written
# fixture is a third source of truth, and when it drifts the tests keep passing against a
# shape that no longer exists. Committed, so a change to a source shape shows up in a diff.
generate-fixtures: ## Generate source-shaped test fixtures from the contracts
	@uv run python -m adapters.jobs.fixture_generation
check-fixtures: ## Fail if a generated fixture is stale, missing or orphaned
	@uv run python -m adapters.jobs.fixture_generation --check
# A tool's input schema is what the MODEL reads to decide what to send, and Gateway validates
# against it before the function is invoked. Declared once in
# src/adapters/inbound/tool_handlers/schemas.py, generated into contracts/generated/tools.json,
# and read by AgentStack — so it cannot exist in two places.
generate-tool-schemas: ## Generate the Gateway tool definitions from the tool contracts
	@uv run python -m adapters.jobs.tool_schema_generation
check-tool-schemas: ## Fail if the generated tool definitions are stale against the contracts
	@uv run python -m adapters.jobs.tool_schema_generation --check
# Static business spans are declared once with a typed source marker, parent policy, and closed
# metadata projection. The checker also enforces direct lifecycle-log and OTel import boundaries.
generate-trace-contract: ## Generate static span constants from the observability contract
	@uv run python -m adapters.jobs.trace_contract
check-trace-contract: ## Validate trace declarations, generated constants, and source conformance
	@uv run python -m adapters.jobs.trace_contract --check
# The operational movement roster is generated into pure runtime source and checked against the
# pre-existing processor declaration without synth, credentials, or AWS access.
generate-data-flow-registry: ## Generate the pure operational data-flow runtime registry
	@uv run python -m adapters.jobs.data_flow_registry
check-data-flow-coverage: ## Validate exact movement coverage and generated runtime source
	@uv run python -m adapters.jobs.data_flow_registry --check
# The execution-graph protocol (control-plane authority S1): one contract, a pure Python and a CDK
# projection, and the decision table's totality proof (DECISION_TABLE_NOT_TOTAL fails generation).
generate-execution-graph-protocol: ## Generate the execution-graph protocol projections
	@uv run python -m adapters.jobs.data_flow_registry.execution_graphs
check-execution-graph-protocol: ## Fail if the execution-graph projections are stale or the table is not total
	@uv run python -m adapters.jobs.data_flow_registry.execution_graphs --check
# Slice 7 deploy gate (D40): the same check, and a null write_unit_batching allowance is refused
# with WRITE_UNIT_BATCHING_PENDING_PROBE, so no deploy path ships a build without the derived value.
check-execution-graph-deployable: ## Fail the deploy gate if the projections are stale or the batching allowance is null
	@uv run python -m adapters.jobs.data_flow_registry.execution_graphs --check --deployable
# The staffed alarm destination (traceability FR-10): one policy and the two destination arms,
# projected to Python and CDK; a blank field or another acknowledgement group fails generation.
generate-alarm-destinations: ## Generate the staffed alarm destination projections
	@uv run python -m adapters.jobs.data_flow_registry.alarm_destinations
check-alarm-destinations: ## Fail if the alarm destination projections are stale or the policy is incomplete
	@uv run python -m adapters.jobs.data_flow_registry.alarm_destinations --check
# Producer packs bind hand-authored SQL to the exact data contract and Semantic V2 source pack.
# The generated registry is the distributable projection; any source change must rotate its digest.
generate-producer-packs: ## Generate the governed AMC producer-pack registry
	@uv run python -m adapters.jobs.producer_pack_generation
check-producer-packs: ## Fail if producer packs or their governed bindings are invalid or stale
	@uv run python -m adapters.jobs.producer_pack_generation --check
# TypeScript owns PARAMS; Python operators consume this committed projection so no SSM path is
# hand-built on both sides of the language boundary.
generate-parameter-contract: ## Generate the cross-language SSM parameter path projection
	@cd infra && node scripts/generate-parameter-contract.cjs
check-parameter-contract: ## Fail if the generated PARAMS projection is stale
	@cd infra && node scripts/generate-parameter-contract.cjs --check

# ---------------------------------------------------------------------------
# Python backend
# ---------------------------------------------------------------------------

install: ## Install Python dependencies exactly as locked (uv manages Python 3.14)
	@uv sync --all-extras --dev --frozen

relock: ## Deliberately update uv.lock, then review the diff before committing
	@uv lock
	@echo "uv.lock rewritten. Review the diff: dependency changes are a decision, not a side effect."

lint: ## Ruff lint
	@uv run ruff check src tests

format: ## Ruff format, in place
	@uv run ruff format src tests
check-format: ## Ruff format, check only: fail on any file `make format` would rewrite
	@uv run ruff format --check src tests

typecheck: ## Pyright, strict mode
	@uv run pyright

lint-boundaries: ## import-linter — the hexagonal dependency gate
	@uv run lint-imports --config pyproject.toml

# Every test and gate target writes an immutable run record under .runs/ (docs/test-run-records.md).
# The canonical commands and gate steps live in scripts/run_record.py (CANONICAL, CANONICAL_GATES).
test: ## Unit tests on bounded parallel workers, recorded; PYTEST_WORKERS=0..4 overrides the count (docs/test-run-records.md)
	@uv run python scripts/run_record.py run --kind pytest-unit

# Not part of `verify`: needs real AWS resources. Tests skip rather than fail when
# credentials are absent, so the unit loop stays credential-free. See ADR-014.
test-integration: ## Adapter tests against real AWS resources, recorded: make test-integration PROFILE=...
	$(call require_profile)
	@AWS_PROFILE=$(PROFILE) uv run python scripts/run_record.py run --kind $@ --format pytest -- \
		python -m pytest -m integration '--junitxml={staging}/junit.xml'

verify: ## Everything that gates a merge on the backend, in order, stopping at the first failure; recorded
	@uv run python scripts/run_record.py gate --kind verify
verify-clean: ## Run make verify, make check, make infra-test and the frontend gates in a clean export of HEAD (before every publication push), recorded
	@uv run python scripts/run_record.py gate --kind verify-clean
# The single step of the verify-clean gate record; run make verify-clean instead.
verify-clean-run:
	@bash scripts/verify-clean.sh
# Read-only: a record is written while its run is in progress (docs/test-run-records.md#live-status).
gate-status: ## List in-progress and recent run records: state, elapsed time, heartbeat age, progress, last log lines [RECENT=5] [LINES=5]
	@uv run python scripts/run_record.py status --recent "$(or $(RECENT),5)" --lines "$(or $(LINES),5)"

# Not part of `check`: it needs the pinned gitleaks and the run records of make verify-clean for this
# tree. BASE defaults to public/main, the public main the push builds on (DEFAULT_BASE in
# scripts/publication_gate.py); run git fetch public first.
publication-gate: ## Refuse a public push unless every commit since BASE is publishable, recorded: make publication-gate [BASE=<rev>] (before every push; default public/main)
	@uv run python scripts/publication_gate.py $(if $(BASE),--base "$(BASE)")
# Not part of `check` or `verify`: about an hour, and it needs the pinned ASH, uvx and finch or
# Docker. Run on a pull request's head before make merge-pr; the mutation summary never refuses.
release-gate: ## Release gate: verify-clean (reusing a sealed record of the same tree), full ASH container scan, mutation coverage summary (report only)
	@uv run python scripts/release_gate.py
# Run once per clone, by the owner. It writes stubs into the repository's own hooks directory and
# changes no git configuration, so a system-level hooksPath set by the machine's security tooling
# stays in force (scripts/install-hooks.sh). The hooks are the commit tier (scripts/commit_gate.py)
# and the push tier (make publication-gate).
install-hooks: ## Install the tracked commit and push hooks in .githooks/ for this clone, changing no git config
	@bash scripts/install-hooks.sh install
uninstall-hooks: ## Remove only the hook stubs make install-hooks wrote
	@bash scripts/install-hooks.sh uninstall
# The only push path: the current feature branch, never main, after the push gate (scripts/pr_flow.py).
publish: ## Push the current feature branch to the public remote after make publication-gate passes for it: make publish [DRY_RUN=1]
	@uv run python scripts/pr_flow.py publish $(if $(DRY_RUN),--dry-run)
pr: ## Open or update the pull request of the pushed feature branch, its body the commits and gate records: make pr [TITLE=...] [DRY_RUN=1]
	@uv run python scripts/pr_flow.py pr $(if $(TITLE),--title "$$TITLE") $(if $(DRY_RUN),--dry-run)
# The owner's go (CONTRIBUTING.md "Run the gates"). $(origin) drops an exported CONFIRM, and
# pr_flow.py checks make's own argv ($$PPID is make), since an inherited MAKEFLAGS assignment
# also has the origin "command line".
from_command_line = $(if $(filter command line,$(origin $(1))),$($(1)))
merge-pr: ## Squash-merge PR into main after make release-gate passed on its head, with the owner's go: make merge-pr PR=<n> CONFIRM=yes [DELETE_BRANCH=yes] [DRY_RUN=1]
	@uv run python scripts/pr_flow.py merge-pr --pr "$$PR" --confirm "$(call from_command_line,CONFIRM)" \
		--delete-branch "$(call from_command_line,DELETE_BRANCH)" \
		--make-argv "$$(ps -ww -o args= -p $$PPID 2>/dev/null)" $(if $(DRY_RUN),--dry-run)

# Not part of `verify` or `test-integration`: this is manual deployed acceptance against synthetic
# Vendor Central input, not AMC. It refuses every omitted target value before Python can make the
# probe's first AWS call (STS GetCallerIdentity).
accept-ingestion-v2: ## Deployed V2 ingestion acceptance (synthetic, not AMC): PROFILE=... ENV=... DAY=... GLANCE_VIEWS=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(DAY)" || (echo "DAY is required, e.g. DAY=2026-08-23"; exit 1)
	@test -n "$(GLANCE_VIEWS)" || (echo "GLANCE_VIEWS is required, e.g. GLANCE_VIEWS=85"; exit 1)
	@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_ingestion_cutover.py \
		--profile "$(PROFILE)" --env "$(ENV)" --day "$(DAY)" --glance-views "$(GLANCE_VIEWS)"

# Deterministic local falsification at the governed 1,000-campaign scale. Raw timings and body
# measurements remain under ignored evidence; no wall-clock threshold is asserted by unit tests.
accept-campaign-estate-local: ## Render and attest the governed 1,000-campaign segmented estate
	@mkdir -p validation/evidence
	@evidence_dir="$$(mktemp -d "$(CURDIR)/validation/evidence/campaign-estate.XXXXXX")"; rmdir "$$evidence_dir"; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/accept_campaign_estate_local.py --evidence-dir "$$evidence_dir"

# Manual protected-evaluation acceptance. It is dev-only and refuses omitted or production-like
# targets before the probe can call AWS. EVALUATION_RESULT_LINK_STACK is supplied by the operator:
# it is an exact CloudFormation target, never discovered by listing account resources. Raw protected
# evidence is retained only after a failure or explicit RETAIN_EVIDENCE=1; successful runs clean it.
accept-evaluation-link: ## Protected evaluation acceptance: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=... EVALUATION_RESULT_LINK_STACK=Adlytics-EvaluationResultLinkV3-dev [RETAIN_EVIDENCE=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(EVALUATION_RESULT_LINK_STACK)" || (echo "EVALUATION_RESULT_LINK_STACK is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "accept-evaluation-link accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-evaluation-link accepts only REGION=us-east-1"; exit 1)
	@test "$(EVALUATION_RESULT_LINK_STACK)" = "Adlytics-EvaluationResultLinkV3-dev" || (echo "EVALUATION_RESULT_LINK_STACK must be the exact dev result-link stack"; exit 1)
	@case "$(RETAIN_EVIDENCE)" in ""|1) ;; *) echo "RETAIN_EVIDENCE may only be 1 when protected evidence must be retained" >&2; exit 1;; esac
	@mkdir -p validation/evidence
	@set -eu; evidence_dir="$$(mktemp -d "$(CURDIR)/validation/evidence/evaluation-link.XXXXXX")"; \
	cleanup() { status=$$?; if [ $$status -eq 0 ] && [ "$(RETAIN_EVIDENCE)" != "1" ]; then rm -rf "$$evidence_dir"; else echo "protected evidence retained locally" >&2; fi; exit $$status; }; \
	trap cleanup EXIT; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_agent_quality_loop.py \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --result-link-stack "$(EVALUATION_RESULT_LINK_STACK)" \
		--evidence-dir "$$evidence_dir"

# The configuration flags that decide which stacks the app instantiates and what the evaluation
# stacks render: every boolean or numeric ADLYTICS_* input of infra/lib/config (envFlag,
# envPercentage, envRatio, envRequiredDays, envRequiredRateLimit). The two origin flags are left
# out because deploy-all.sh sets them itself, so a shell value never reaches its synth. String
# inputs (model IDs, domain prefix, landing role and external ID) are never printed.
DEPLOY_FLAG_NAMES := ADLYTICS_ONLINE_EVALUATION_PROVISIONED ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED \
	ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT ADLYTICS_OPERATIONAL_STATUS_PROVISIONED \
	ADLYTICS_EVALUATION_INPUT_PROVISIONED ADLYTICS_TRANSACTION_SEARCH ADLYTICS_SPAN_INDEXING_PERCENT \
	ADLYTICS_TRACE_SAMPLE_RATIO ADLYTICS_PRODUCER_ONBOARDING_ENABLED ADLYTICS_GATEWAY_GUARDRAILS \
	ADLYTICS_QUALITY_REVIEW_VALIDATION_PASSWORD_AUTH ADLYTICS_RECEIPT_RETENTION_DAYS \
	ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES \
	ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY
# The one configuration source for every deploy-shaped command: the shell, overlaid by .env
# exactly as scripts/deploy-all.sh loads it.
define load_deploy_env
set -a; [ ! -f .env ] || . ./.env; set +a
endef
define deploy_flags
for n in $(DEPLOY_FLAG_NAMES); do eval "v=\$${$$n:-}"; printf '%s=%s\n' "$$n" "$${v:-unset}"; done | paste -sd, -
endef

# Print the deploy configuration flags (names and boolean/numeric values only). Read-only.
print-deploy-flags: ## Print the boolean and numeric ADLYTICS_* deploy flags after loading .env
	@set -eu; $(load_deploy_env); $(deploy_flags)

# Read-only result-group custody acceptance (ADR-079). Dev-only, like accept-evaluation-link: it
# refuses omitted or production-like targets before the probe can call AWS. The stack list is the
# app's own `cdk list` under the deploy's configuration, never discovered by listing account
# resources. EXPECTED_FLAGS is the line print-deploy-flags printed before the deploy under test.
accept-evaluation-custody: ## Read-only result-group custody acceptance: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=... EXPECTED_FLAGS=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(EXPECTED_FLAGS)" || (echo "EXPECTED_FLAGS is required: the print-deploy-flags line recorded before the deploy"; exit 1)
	@test "$(ENV)" = "dev" || (echo "accept-evaluation-custody accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-evaluation-custody accepts only REGION=us-east-1"; exit 1)
	@set -eu; $(load_deploy_env); \
	export AWS_REGION="$(REGION)" AWS_DEFAULT_REGION="$(REGION)"; \
	test "$${ADLYTICS_ONLINE_EVALUATION_PROVISIONED:-}" = true || { echo "ADLYTICS_ONLINE_EVALUATION_PROVISIONED must be true, as for the deploy under test" >&2; exit 1; }; \
	test "$${ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED:-}" = true || { echo "ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED must be true, as for the deploy under test" >&2; exit 1; }; \
	flags="$$($(deploy_flags))"; \
	stacks="$$(cd infra && npx cdk list --profile "$(PROFILE)" -c env="$(ENV)" | paste -sd, -)"; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_evaluation_custody.py \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --family v4 --stacks "$$stacks" \
		--config-flags "$$flags" --expected-config-flags "$(EXPECTED_FLAGS)"
# Agent-mode regression (ADR-079 §5, ADR-082): the versioned fictional question set through the
# deployed dev route, scored by the released evaluators. The report is kept in local evidence
# custody because it is the baseline for the next run; pass BASELINE=<its path> to compare. After a
# successful run the raw turn frames beside it are removed unless RETAIN_EVIDENCE=1; a failed run
# keeps them for diagnosis.
regress-agent: ## Agent-mode regression: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=... EVALUATION_RESULT_LINK_STACK=Adlytics-EvaluationResultLinkV3-dev [BASELINE=validation/evidence/.../agent-regression-report.json] [RETAIN_EVIDENCE=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "regress-agent accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "regress-agent accepts only REGION=us-east-1"; exit 1)
	@test "$(EVALUATION_RESULT_LINK_STACK)" = "Adlytics-EvaluationResultLinkV3-dev" || (echo "EVALUATION_RESULT_LINK_STACK must be the exact dev result-link stack"; exit 1)
	@mkdir -p validation/evidence
	@case "$(RETAIN_EVIDENCE)" in ""|1) ;; *) echo "RETAIN_EVIDENCE may only be 1" >&2; exit 1;; esac
	@set -eu; evidence_dir="$$(mktemp -d "$(CURDIR)/validation/evidence/agent-regression.XXXXXX")"; \
	cleanup() { status=$$?; if [ $$status -eq 0 ] && [ "$(RETAIN_EVIDENCE)" != "1" ]; then find "$$evidence_dir" -type f ! -name agent-regression-report.json -delete; else echo "protected evidence retained locally" >&2; fi; exit $$status; }; \
	trap cleanup EXIT; \
	echo "report: $${evidence_dir#$(CURDIR)/}/agent-regression-report.json" >&2; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/regress_agent.py \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --result-link-stack "$(EVALUATION_RESULT_LINK_STACK)" \
		--evidence-dir "$${evidence_dir#$(CURDIR)/}" --verdict-timeout-seconds 1800 \
		$(if $(BASELINE),--baseline "$(BASELINE)",)
# Joined, read/invoke-only managed Harness acceptance. Every stack is an operator-supplied exact
# CloudFormation target; the runner never discovers account resources. Protected evidence is removed
# only after a complete pass unless RETAIN_EVIDENCE=1, and is always retained for failed/pending stages.
accept-managed-harness: ## Joined managed Harness acceptance: explicit dev profile/account/region and all exact *_STACK values [RETAIN_EVIDENCE=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(HARNESS_STACK)" || (echo "HARNESS_STACK is required"; exit 1)
	@test -n "$(API_STACK)" || (echo "API_STACK is required"; exit 1)
	@test -n "$(AGENT_STACK)" || (echo "AGENT_STACK is required"; exit 1)
	@test -n "$(AGENT_QUALITY_STACK)" || (echo "AGENT_QUALITY_STACK is required"; exit 1)
	@test -n "$(AGENT_QUALITY_PROCESSOR_STACK)" || (echo "AGENT_QUALITY_PROCESSOR_STACK is required"; exit 1)
	@test -n "$(EVALUATION_EVIDENCE_STACK)" || (echo "EVALUATION_EVIDENCE_STACK is required"; exit 1)
	@test -n "$(EVALUATION_INPUT_STACK)" || (echo "EVALUATION_INPUT_STACK is required"; exit 1)
	@test -n "$(EVALUATION_OUTBOX_DISPATCH_STACK)" || (echo "EVALUATION_OUTBOX_DISPATCH_STACK is required"; exit 1)
	@test -n "$(EVALUATION_STACK)" || (echo "EVALUATION_STACK is required"; exit 1)
	@test -n "$(EVALUATION_RESULT_LINK_STACK)" || (echo "EVALUATION_RESULT_LINK_STACK is required"; exit 1)
	@test -n "$(OBSERVABILITY_STACK)" || (echo "OBSERVABILITY_STACK is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "accept-managed-harness accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-managed-harness accepts only REGION=us-east-1"; exit 1)
	@test "$(HARNESS_STACK)" = "Adlytics-Harness-dev" || (echo "HARNESS_STACK must be the exact dev stack"; exit 1)
	@test "$(API_STACK)" = "Adlytics-Api-dev" || (echo "API_STACK must be the exact dev stack"; exit 1)
	@test "$(AGENT_STACK)" = "Adlytics-Agent-dev" || (echo "AGENT_STACK must be the exact dev stack"; exit 1)
	@test "$(AGENT_QUALITY_STACK)" = "Adlytics-AgentQuality-dev" || (echo "AGENT_QUALITY_STACK must be the exact dev stack"; exit 1)
	@test "$(AGENT_QUALITY_PROCESSOR_STACK)" = "Adlytics-AgentQualityProcessor-dev" || (echo "AGENT_QUALITY_PROCESSOR_STACK must be the exact dev stack"; exit 1)
	@test "$(EVALUATION_EVIDENCE_STACK)" = "Adlytics-EvaluationEvidence-dev" || (echo "EVALUATION_EVIDENCE_STACK must be the exact dev stack"; exit 1)
	@test "$(EVALUATION_INPUT_STACK)" = "Adlytics-EvaluationInput-dev" || (echo "EVALUATION_INPUT_STACK must be the exact dev stack"; exit 1)
	@test "$(EVALUATION_OUTBOX_DISPATCH_STACK)" = "Adlytics-EvaluationOutboxDispatch-dev" || (echo "EVALUATION_OUTBOX_DISPATCH_STACK must be the exact dev stack"; exit 1)
	@test "$(EVALUATION_STACK)" = "Adlytics-EvaluationV4-dev" || (echo "EVALUATION_STACK must be the exact active evaluation dev stack"; exit 1)
	@test "$(EVALUATION_RESULT_LINK_STACK)" = "Adlytics-EvaluationResultLinkV3-dev" || (echo "EVALUATION_RESULT_LINK_STACK must be the exact dev result-link stack"; exit 1)
	@test "$(OBSERVABILITY_STACK)" = "Adlytics-Observability-dev" || (echo "OBSERVABILITY_STACK must be the exact dev stack"; exit 1)
	@case "$(RETAIN_EVIDENCE)" in ""|1) ;; *) echo "RETAIN_EVIDENCE may only be 1" >&2; exit 1;; esac
	@mkdir -p validation/evidence
	@set -eu; evidence_dir="$$(mktemp -d "$(CURDIR)/validation/evidence/managed-harness.XXXXXX")"; \
	cleanup() { status=$$?; if [ $$status -eq 0 ] && [ "$(RETAIN_EVIDENCE)" != "1" ]; then rm -rf "$$evidence_dir"; else echo "protected managed Harness evidence retained locally" >&2; fi; exit $$status; }; \
	trap cleanup EXIT; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/accept_managed_harness.py \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --evidence-dir "$$evidence_dir" \
		--harness-stack "$(HARNESS_STACK)" --api-stack "$(API_STACK)" \
		--agent-stack "$(AGENT_STACK)" --quality-stack "$(AGENT_QUALITY_STACK)" \
		--quality-processor-stack "$(AGENT_QUALITY_PROCESSOR_STACK)" \
		--evaluation-evidence-stack "$(EVALUATION_EVIDENCE_STACK)" \
		--evaluation-input-stack "$(EVALUATION_INPUT_STACK)" \
		--evaluation-outbox-dispatch-stack "$(EVALUATION_OUTBOX_DISPATCH_STACK)" \
		--evaluation-stack "$(EVALUATION_STACK)" \
		--evaluation-result-link-stack "$(EVALUATION_RESULT_LINK_STACK)" \
		--observability-stack "$(OBSERVABILITY_STACK)"

# Dev-only reprocess telemetry acceptance. The probe runs and digest-binds its own dry plan before
# apply, then joins the completed lifecycle to one exported Transaction Search trace. Raw plans,
# subprocess output, deployment records, and span documents always remain in ignored local evidence.
accept-reprocess-telemetry: ## Reprocess telemetry acceptance: explicit dev target, scope, and lineage
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(DATA_STACK)" || (echo "DATA_STACK is required"; exit 1)
	@test -n "$(OBSERVABILITY_STACK)" || (echo "OBSERVABILITY_STACK is required"; exit 1)
	@test -n "$(BUCKET)" || (echo "BUCKET is required"; exit 1)
	@test -n "$(WINDOW_START)" || (echo "WINDOW_START is required"; exit 1)
	@test -n "$(WINDOW_END)" || (echo "WINDOW_END is required"; exit 1)
	@test -n "$(OPERATION_ID)" || (echo "OPERATION_ID is required"; exit 1)
	@test -n "$(CONTRACT)" || (echo "CONTRACT is required"; exit 1)
	@test -n "$(RETRIEVED_TOKEN)" || (echo "RETRIEVED_TOKEN is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "accept-reprocess-telemetry accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-reprocess-telemetry accepts only REGION=us-east-1"; exit 1)
	@test "$(DATA_STACK)" = "Adlytics-DataV2-dev" || (echo "DATA_STACK must be the exact dev stack"; exit 1)
	@test "$(OBSERVABILITY_STACK)" = "Adlytics-Observability-dev" || (echo "OBSERVABILITY_STACK must be the exact dev stack"; exit 1)
	@mkdir -p validation/evidence
	@set -eu; evidence_dir="$$(mktemp -d "$(CURDIR)/validation/evidence/reprocess-telemetry.XXXXXX")"; \
	uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_reprocess_telemetry.py \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --data-stack "$(DATA_STACK)" \
		--observability-stack "$(OBSERVABILITY_STACK)" --bucket "$(BUCKET)" \
		--window-start "$(WINDOW_START)" --window-end "$(WINDOW_END)" \
		--operation-id "$(OPERATION_ID)" --contract "$(CONTRACT)" \
		--retrieved-token "$(RETRIEVED_TOKEN)" --evidence-dir "$$evidence_dir" $(if $(PREDECESSOR_EXECUTION_ID),--predecessor-execution-id "$(PREDECESSOR_EXECUTION_ID)")

build-lambdas: ## Cross-compile the arm64 dependency layer and source tree into build/
	@bash scripts/build-lambdas.sh

# Runs AFTER the API stack is deployed because publication resolves the destination bucket and
# distribution through ADR-012. The static app itself keeps API calls same-origin and relative.
build-frontend: ## Publish the frontend: make build-frontend ENV=... PROFILE=... REGION=... ACCOUNT=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required, e.g. make $@ REGION=us-east-1"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@PROFILE="$(PROFILE)" REGION="$(REGION)" EXPECTED_ACCOUNT="$(ACCOUNT)" bash scripts/build-frontend.sh "$(ENV)"

frontend-typecheck: ## tsc --noEmit on the frontend
	@cd frontend && npm run typecheck

frontend-test: ## Vitest unit tests for the frontend, recorded
	@uv run python scripts/run_record.py run --kind frontend-test

# Self sign-up is off, so this is the only way an account is created. Cognito
# emails a temporary password and managed login forces a change, so the operator
# never learns the user's password. See docs/decisions/adr-007-auth-cookie-bff.md.
create-user: ## Invite a user: make create-user ENV=... EMAIL=you@example.com PROFILE=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(EMAIL)" || (echo "EMAIL is required, e.g. EMAIL=you@example.com"; exit 1)
	@PROFILE="$(PROFILE)" bash scripts/create-user.sh "$(ENV)" "$(EMAIL)"

# Persistent synthetic identities for the three Quality Review entitlement routes and denied case.
# Credentials are generated at provisioning and held only in dev SSM SecureString parameters. The
# app client remains authorization-code/BFF only: this target never enables password auth or mints tokens.
provision-quality-review-users: ## Persistent dev Quality Review users: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "provision-quality-review-users accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "provision-quality-review-users accepts only REGION=us-east-1"; exit 1)
	@uv run python validation/provision_quality_review_test_users.py --profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" --expected-account "$(ACCOUNT)"

# Browser-only acceptance through Cognito managed login and the existing BFF cookie callback.
accept-quality-review: ## Managed-login Quality Review matrix: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test "$(ENV)" = "dev" || (echo "accept-quality-review accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-quality-review accepts only REGION=us-east-1"; exit 1)
	@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_quality_review_api.py --profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" --expected-account "$(ACCOUNT)"

# ADR-080 §5 and ADR-081 §5: the one-time expiry backfill of every record table, after TTL is
# enabled and before the resource policies land. Refused, dry run included, once the policies are on. RETENTION_DAYS must equal the deployed
# ADLYTICS_RECEIPT_RETENTION_DAYS. DRY_RUN=1 reads and counts and writes nothing.
backfill-record-expiry: ## One-time record expiry backfill: PROFILE=... ENV=... REGION=... ACCOUNT=... RETENTION_DAYS=... LEGACY_MARGIN_HOURS=... [DRY_RUN=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(RETENTION_DAYS)" || (echo "RETENTION_DAYS is required: the deployed ADLYTICS_RECEIPT_RETENTION_DAYS"; exit 1)
	@test -n "$(LEGACY_MARGIN_HOURS)" || (echo "LEGACY_MARGIN_HOURS is required: hours until moved-family legacy items expire"; exit 1)
	@case "$(DRY_RUN)" in ""|1) ;; *) echo "DRY_RUN may only be 1" >&2; exit 1;; esac
	@uv run python -m adapters.jobs.backfill_record_expiry \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --retention-days "$(RETENTION_DAYS)" \
		--legacy-margin-hours "$(LEGACY_MARGIN_HOURS)" $(if $(DRY_RUN),--dry-run)

# TD-50: disposition the orphan legacy plan units as superseded, each with recorded evidence, before
# the GRAPH drain proof. A dry run by default (reads and writes the evidence file only); APPLY=1
# commits the proven plans and refuses while a semantic-write message exists.
supersede-orphan-units: ## Orphan plan-unit disposition: PROFILE=... ENV=... REGION=... ACCOUNT=... EVIDENCE_DIR=... [APPLY=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required"; exit 1)
	@case "$(APPLY)" in ""|1) ;; *) echo "APPLY may only be 1" >&2; exit 1;; esac
	@uv run python -m adapters.jobs.supersede_orphan_units \
		--profile "$(PROFILE)" --env "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --evidence-dir "$(EVIDENCE_DIR)" $(if $(APPLY),--apply)
migrate-review-groups: ## One-time move of retired review-group members into quality-reviewer: PROFILE=... ENV=... REGION=... ACCOUNT=... [DRY_RUN=1]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@case "$(DRY_RUN)" in ""|1) ;; *) echo "DRY_RUN may only be 1" >&2; exit 1;; esac
	@uv run python -m adapters.jobs.migrate_review_groups \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" $(if $(DRY_RUN),--dry-run)

# ADR-078 edge acceptance items 4, 7, 8, 9 (docs/concepts/authentication.md). EXPECT_MODE is the
# mode every web ACL rule must read back in; COUNT_SINCE adds the count-to-block evidence.
accept-edge: ## Edge WAF + origin-verify acceptance: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=... EXPECT_MODE=count|block [COUNT_SINCE=ISO]
	$(call require_profile)
	$(call require_env)
	@test "$(ENV)" = "dev" || (echo "accept-edge accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-edge accepts only REGION=us-east-1"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@case "$(EXPECT_MODE)" in count|block) ;; *) echo "EXPECT_MODE must be count or block" >&2; exit 1;; esac
	@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_edge_entry.py --profile "$(PROFILE)" --environment "$(ENV)" \
		--region "$(REGION)" --expected-account "$(ACCOUNT)" --expect-mode "$(EXPECT_MODE)" \
		$(if $(COUNT_SINCE),--count-since "$(COUNT_SINCE)")

# ADR-041 (amended 2026-09-30): replay the fictional calibration set against the deployed route
# Guardrail and fail outside the calibrated bounds. Run after every AgentStack deploy.
accept-guardrail: ## Deployed route Guardrail acceptance: PROFILE=... ENV=dev REGION=us-east-1 ACCOUNT=...
	$(call require_profile)
	$(call require_env)
	@test "$(ENV)" = "dev" || (echo "accept-guardrail accepts only ENV=dev"; exit 1)
	@test "$(REGION)" = "us-east-1" || (echo "accept-guardrail accepts only REGION=us-east-1"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run python validation/probe_guardrail_calibration.py --profile "$(PROFILE)" \
		--expected-account "$(ACCOUNT)" --accept

# ADR-078: one secret write of the origin-verify rotation, coupled to a FrontendStack redeploy
# between the two steps (docs/concepts/authentication.md, edge entry). Prints version ids only.
rotate-origin-verify: ## Origin-verify rotation step: PROFILE=... ENV=... REGION=... ACCOUNT=... STEP=begin|finish
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@case "$(STEP)" in begin|finish) ;; *) echo "STEP must be begin or finish" >&2; exit 1;; esac
	@uv run python -m adapters.jobs.rotate_origin_verify \
		--profile "$(PROFILE)" --environment "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --step "$(STEP)"
MIN_CONFIDENCE ?= 60

dead-code: ## Report dead-code candidates: make dead-code [MIN_CONFIDENCE=100]
	@echo "vulture --min-confidence $(MIN_CONFIDENCE)"
	@uv run python scripts/run_record.py run --kind $@ --format exit -- uv run vulture --min-confidence $(MIN_CONFIDENCE) || true

dead-code-allowlist: ## Regenerate the vulture allowlist from current findings
	@uv run vulture --make-whitelist > scripts/vulture-allowlist.py || true
	@echo "wrote scripts/vulture-allowlist.py — review every line before committing"

# When these run: .kiro/steering/meta-testing-mutation.md. One unit per run (ADR-013). cosmic-ray
# mutates python source in place and restores it; scripts/mutation.py refuses a dirty tree and
# checks it afterwards.
mutate: ## Mutate ONE unit: make mutate UNIT=<path> [SEMANTIC=<proposals.yaml>]
	@test -n "$(UNIT)" || (echo "UNIT is required, e.g. make mutate UNIT=src/domain/identity/caller.py"; exit 1)
	@uv run python scripts/mutation.py run --unit "$(UNIT)" $(if $(SEMANTIC),--semantic "$(SEMANTIC)")
mutate-next: ## Mutate the next missing or stale units by priority: make mutate-next [N=5]
	@uv run python scripts/mutation.py next --n "$(or $(N),5)"
mutation-status: ## Mutation coverage per priority tier: current, stale, missing, untested, OPEN (report only)
	@uv run python scripts/mutation.py status
mutation-verdict: ## Set one survivor's verdict: make mutation-verdict UNIT= ID= VERDICT= [TEST=] [REASON=]
	@test -n "$(UNIT)" || (echo "UNIT is required"; exit 1)
	@test -n "$(ID)" || (echo "ID is required, e.g. ID=S-01"; exit 1)
	@test -n "$(VERDICT)" || (echo "VERDICT is required: KILLED, EQUIVALENT or OPEN"; exit 1)
	@uv run python scripts/mutation.py verdict --unit "$(UNIT)" --id "$(ID)" --verdict "$(VERDICT)" \
		$(if $(TEST),--test "$(TEST)") $(if $(REASON),--reason "$(REASON)")

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

infra-typecheck: ## tsc --noEmit on the CDK app
	@cd infra && npm run typecheck

check-lambda-layer-reproducibility: ## Build the committed Python layer twice and require identical CDK assets
	@uv run python scripts/check-lambda-layer-reproducibility.py

infra-test: check-lambda-layer-reproducibility ## CDK unit tests against reproducibly staged deployment artifacts, recorded
	@uv run python scripts/run_record.py run --kind infra-test

# T2 (ADR-133 clause 4): a whole-app synth, so it stays in the infra tier rather than make check;
# make infra-test runs it too. Credential-free and needs no build/ (placeholder asset).
check-alarm-coverage: ## Fail if a function or an operation can fail without an alarm on the staffed destination
	@uv run python scripts/run_record.py run --kind $@ --format jest-json --cwd infra -- \
		npx jest --runInBand test/alarm-coverage.test.ts --json '--outputFile={staging}/jest.json'

# S6B-1: a whole-app synth like check-alarm-coverage, so it stays in the infra tier; make infra-test
# runs it too. Reads validation/**/*.py as text; credential-free, no AWS.
check-probe-resource-names: ## Fail if a validation probe names a resource or token identifier no stack synthesizes
	@uv run python scripts/run_record.py run --kind $@ --format jest-json --cwd infra -- \
		npx jest test/probe-resource-names.test.ts --json '--outputFile={staging}/jest.json'

synth: ## CDK synth: make synth ENV=... PROFILE=...
	$(call require_profile)
	$(call require_env)
	@set -eu; $(load_deploy_env); cd infra && npx cdk synth --profile "$(PROFILE)" -c env="$(ENV)"

diff: ## CDK diff: make diff ENV=... PROFILE=...
	$(call require_profile)
	$(call require_env)
	@set -eu; $(load_deploy_env); cd infra && npx cdk diff --profile "$(PROFILE)" -c env="$(ENV)"

deploy: check-execution-graph-deployable ## CDK deploy ONE stack: make deploy STACK=Adlytics-Auth-<env> ENV=... PROFILE=... APPROVE=1
	$(call require_profile)
	$(call require_env)
	@test -n "$(STACK)" || (echo "STACK is required, e.g. STACK=Adlytics-Auth-$(ENV)"; exit 1)
	@test -n "$(APPROVE)" || (echo "This stack may contain IAM or security-group changes."; \
	  echo "Review them first:  make diff ENV=$(ENV) PROFILE=$(PROFILE)"; \
	  echo "Then re-run with:   make deploy STACK=$(STACK) ENV=$(ENV) PROFILE=$(PROFILE) APPROVE=1"; exit 1)
	@aws sts get-caller-identity --profile "$(PROFILE)"
	@set -eu; $(load_deploy_env); cd infra && npx cdk deploy "$(STACK)" --exclusively \
	  --profile "$(PROFILE)" -c env="$(ENV)" --require-approval never

# Every stack in DEPLOY_ORDER, the frontend's second pass when needed, the frontend build, and an
# optional invited user. Reads .env when present. Safe to re-run.
deploy-all: ## Deploy or update a whole environment (diff only without APPROVE=1): make deploy-all ENV=... PROFILE=... [APPROVE=1] [EMAIL=...]
	@PROFILE="$(PROFILE)" ENV="$(ENV)" APPROVE="$(APPROVE)" EMAIL="$(EMAIL)" bash scripts/deploy-all.sh

# Shared-environment deploy and state tools (docs/capabilities/operational-data-flow-status/
# operator-tools.md#shared-environment-deploy-and-state-tools). Each tool's stop is its exit code.
# The stack set is the app's own `cdk list --long --json` under the deploy configuration, written
# beside the record, so each stack is read in its own Region; nothing lists the account. The deploy
# lock's targets add the CDK bootstrap stack in each of those Regions (validation/dev_operations.py
# lock-targets). Every input is required and passed through, never defaulted. EVIDENCE_DIR,
# LOCK_FILE and LOG_FILE name ignored durable paths outside /tmp.
define write_cdk_list
$(load_deploy_env); (cd infra && npx cdk list --long --json --profile "$(PROFILE)" -c env="$(ENV)") > "$$listing_file" || { echo "cdk list --long --json failed; nothing was read" >&2; exit 2; }; test -s "$$listing_file" || { echo "cdk list printed nothing: $$listing_file" >&2; exit 2; }
endef
define write_lock_targets
$(write_cdk_list); uv run python validation/dev_operations.py lock-targets --cdk-list "$$listing_file" --env "$(ENV)" --expected-account "$(ACCOUNT)" --out "$$targets_file" || { echo "the lock targets were refused; see the RESULT line" >&2; exit 2; }
endef

dev-state: ## Read-only state: alarms not OK, failure queues, stacks, lock: PROFILE=... ENV=... REGION=... ACCOUNT=... EVIDENCE_DIR=... LOCK_FILE=... [DISPOSITIONS=...]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required: an ignored durable directory"; exit 1)
	@test -n "$(LOCK_FILE)" || (echo "LOCK_FILE is required: the deploy lock file to report"; exit 1)
	@set -eu; listing_file="$(EVIDENCE_DIR)/dev-state-cdk-list-$$(date -u +%Y%m%dT%H%M%SZ).json"; \
	$(write_cdk_list); \
	uv run python validation/dev_operations.py state --profile "$(PROFILE)" --env "$(ENV)" \
		--region "$(REGION)" --expected-account "$(ACCOUNT)" --evidence-dir "$(EVIDENCE_DIR)" \
		--cdk-list "$$listing_file" --lock-file "$(LOCK_FILE)" \
		$(if $(DISPOSITIONS),--dispositions "$(DISPOSITIONS)")

check-alarm-window: ## Stop on an unexpected ALARM since SINCE: PROFILE=... ENV=... REGION=... ACCOUNT=... SINCE=<ISO> RECOVER_MINUTES=... EXPECTED=<a,b|none> EVIDENCE_DIR=... [DISPOSITIONS=...]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(SINCE)" || (echo "SINCE is required: the window start, e.g. 2026-01-01T00:00:00Z"; exit 1)
	@test -n "$(RECOVER_MINUTES)" || (echo "RECOVER_MINUTES is required"; exit 1)
	@test -n "$(EXPECTED)" || (echo "EXPECTED is required: comma-separated alarm names, or none"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required: an ignored durable directory"; exit 1)
	@uv run python validation/dev_operations.py alarm-window --profile "$(PROFILE)" --env "$(ENV)" \
		--region "$(REGION)" --expected-account "$(ACCOUNT)" --evidence-dir "$(EVIDENCE_DIR)" \
		--since "$(SINCE)" --recover-minutes "$(RECOVER_MINUTES)" --expected "$(EXPECTED)" \
		$(if $(DISPOSITIONS),--dispositions "$(DISPOSITIONS)")

check-serving-refusals: ## D2 ops check O3: every grant-bearing CURRENT served, no past-settle refusal, neither serving alarm in ALARM: PROFILE=... ENV=... REGION=... ACCOUNT=... SINCE=<ISO> EVIDENCE_DIR=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(SINCE)" || (echo "SINCE is required: the acceptance window start, e.g. 2026-01-01T00:00:00Z"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required: an ignored durable directory"; exit 1)
	@uv run python validation/dev_operations.py serving-refusals --profile "$(PROFILE)" --env "$(ENV)" \
		--region "$(REGION)" --expected-account "$(ACCOUNT)" --evidence-dir "$(EVIDENCE_DIR)" \
		--since "$(SINCE)"

check-staged-tree: ## Fail (81) unless build/src equals HEAD's src/ and contracts/ (local, no AWS)
	@bash scripts/staged-tree-check.sh "$(CURDIR)"

diff-then-deploy: ## Under scripts/deploy-locked.sh: diff one stack, or deploy it if the diff equals the read one: PROFILE=... ENV=... REGION=... ACCOUNT=... STACK=... EVIDENCE_DIR=... MODE=diff-only|deploy [READ_DIFF=...]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(STACK)" || (echo "STACK is required"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required: an ignored durable directory"; exit 1)
	@case "$(MODE)" in diff-only) ;; deploy) test -n "$(READ_DIFF)" || { echo "READ_DIFF is required with MODE=deploy"; exit 1; } ;; \
	  *) echo "MODE is required: diff-only or deploy"; exit 1 ;; esac
	@bash scripts/diff-then-deploy.sh --profile "$(PROFILE)" --env "$(ENV)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --stack "$(STACK)" --evidence-dir "$(EVIDENCE_DIR)" \
		$(if $(filter deploy,$(MODE)),--deploy "$(READ_DIFF)",--diff-only)

deploy-all-locked: ## make deploy-all under the deploy lock, from one declared source: PROFILE=... ENV=... REGION=... ACCOUNT=... LOCK_FILE=... LOG_FILE=... SOURCE_SHA=... [SOURCE_BRANCH=...] [APPROVE=1] [EMAIL=...]
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(LOCK_FILE)" || (echo "LOCK_FILE is required: an ignored durable path"; exit 1)
	@test -n "$(LOG_FILE)" || (echo "LOG_FILE is required: an ignored durable path"; exit 1)
	@test -n "$(SOURCE_SHA)" || (echo "SOURCE_SHA is required: the declared deploy source"; exit 1)
	@set -eu; profile_region="$$(aws configure get region --profile "$(PROFILE)" || true)"; \
	test "$$profile_region" = "$(REGION)" || { echo "REGION $(REGION) is not profile $(PROFILE)'s Region ($$profile_region), which scripts/deploy-all.sh deploys to" >&2; exit 1; }; \
	utc="$$(date -u +%Y%m%dT%H%M%SZ)"; listing_file="$(LOG_FILE).cdk-list-$$utc.json"; \
	targets_file="$(LOG_FILE).targets-$$utc.txt"; \
	$(write_lock_targets); \
	bash scripts/deploy-locked.sh --profile "$(PROFILE)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --env "$(ENV)" --lock-file "$(LOCK_FILE)" \
		--log-file "$(LOG_FILE)" --targets-file "$$targets_file" --source-sha "$(SOURCE_SHA)" \
		$(if $(SOURCE_BRANCH),--source-branch "$(SOURCE_BRANCH)") --label deploy-all -- \
		env PROFILE="$(PROFILE)" ENV="$(ENV)" APPROVE="$(APPROVE)" EMAIL="$(EMAIL)" bash scripts/deploy-all.sh

check-source-key: ## One source key across every deployed stack, each equal to this synth: PROFILE=... ENV=... REGION=... ACCOUNT=... EVIDENCE_DIR=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required: an ignored durable directory"; exit 1)
	@set -eu; listing_file="$(EVIDENCE_DIR)/source-key-cdk-list-$$(date -u +%Y%m%dT%H%M%SZ).json"; \
	$(write_cdk_list); \
	uv run python validation/dev_operations.py source-key --profile "$(PROFILE)" --env "$(ENV)" \
		--region "$(REGION)" --expected-account "$(ACCOUNT)" --evidence-dir "$(EVIDENCE_DIR)" \
		--cdk-list "$$listing_file"

deploy-lock-force-release: ## Remove a stale deploy lock after its checks pass, logging FORCED_RELEASE: PROFILE=... ENV=... REGION=... ACCOUNT=... LOCK_FILE=... LOG_FILE=... OWNER_PID=... REASON=...
	$(call require_profile)
	$(call require_env)
	@test -n "$(REGION)" || (echo "REGION is required"; exit 1)
	@test -n "$(ACCOUNT)" || (echo "ACCOUNT is required"; exit 1)
	@test -n "$(LOCK_FILE)" || (echo "LOCK_FILE is required"; exit 1)
	@test -n "$(LOG_FILE)" || (echo "LOG_FILE is required"; exit 1)
	@test -n "$(OWNER_PID)" || (echo "OWNER_PID is required: the pid the stale lock names"; exit 1)
	@test -n "$(REASON)" || (echo "REASON is required: why the lock is stale"; exit 1)
	@set -eu; utc="$$(date -u +%Y%m%dT%H%M%SZ)"; listing_file="$(LOG_FILE).cdk-list-$$utc.json"; \
	targets_file="$(LOG_FILE).targets-$$utc.txt"; \
	$(write_lock_targets); \
	bash scripts/deploy-locked.sh --force-release --profile "$(PROFILE)" --region "$(REGION)" \
		--expected-account "$(ACCOUNT)" --env "$(ENV)" --lock-file "$(LOCK_FILE)" \
		--log-file "$(LOG_FILE)" --targets-file "$$targets_file" --owner-pid "$(OWNER_PID)" \
		--reason "$(REASON)"

# Reverse DEPLOY_ORDER. Retained data survives and is listed at the end for manual deletion.
destroy-all: ## Destroy a whole environment: make destroy-all ENV=... PROFILE=... CONFIRM=<ENV> [INCLUDE_ACCOUNT_SETTINGS=1]
	@PROFILE="$(PROFILE)" ENV="$(ENV)" CONFIRM="$(CONFIRM)" \
	  INCLUDE_ACCOUNT_SETTINGS="$(INCLUDE_ACCOUNT_SETTINGS)" bash scripts/destroy-all.sh
