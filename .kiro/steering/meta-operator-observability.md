---
inclusion: fileMatch
fileMatchPattern: ["**/src/adapters/jobs/reprocess_semantic_v2/**", "**/src/adapters/jobs/ingest_delivery/**", "**/src/adapters/jobs/preset_artifact_publisher/**", "**/validation/probe_stage_latency.py"]
description: Governs trace bootstrap, structured progress, heartbeat, terminal evidence, and cancellation lineage when editing or diagnosing long-running AWS operator jobs and background processing workflows.
---

# Operator and background-process observability

The general telemetry, redaction, lifecycle-level, provider-diagnostic, and error-routing rules
are owned by backend-observability-lambda. ADOT rationale and span export are owned by ADR-009;
target selection and identity are owned by meta-aws-operations. This file owns only the
long-running operator-process additions.

- Mark every hand-authored business span with the typed `trace_boundary` decorator; never
  discover tracing intent from docstring prose or a manually maintained function inventory. The
  governed span registry owns metadata and parent rules, while the decorator colocates the
  implementing function with its registered span identity.
- Initialize the repository ADOT bootstrap before constructing an AWS SDK client, from explicit
  caller-supplied operator settings. Always flush spans in a `finally` block before process exit.
- Wrap one invocation in a root business-process span and every independently resumable contract
  or partition in a child span. Auto-instrumented SDK spans never substitute for these business
  boundaries.
- Emit structured start, bounded-progress, heartbeat, cancellation, and terminal lifecycle records
  through the shared reporting seam. Never use process existence, a submitted provider request, or
  unchanged stdout as evidence of progress.
- Every progress and span record carries the operation ID, execution ID, predecessor execution ID
  when present, plan digest, current stage, completed count, and total count. Never carry source
  rows, SQL, measurements, credentials, or customer identifiers.
- A heartbeat reports the last completed durable stage and monotonic progress counts; it never
  advances progress merely because the process is alive or polling.
- Handle termination as a typed cancelled terminal outcome, flush telemetry, and print the exact
  predecessor execution ID required for recovery. Never restart with an unrelated lineage.
- Acceptance must read one exported trace and the matching structured terminal record before
  declaring operator telemetry enabled; configuration or a successful command is not evidence.
