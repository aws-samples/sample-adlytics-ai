#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Build the Lambda artifacts and their shared dependency layer.
#
# Cross-compiled for arm64 rather than built on the host. Every function is arm64
# (ADR-005), and a layer containing x86_64 wheels fails at function init rather
# than at build — which is the worst place to find out.
#
# No Docker on the critical path. uv resolves platform-specific wheels directly,
# so a synth never needs a daemon running.
#
# Usage: scripts/build-lambdas.sh [output-dir]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build}"

PYTHON_VERSION="3.14"
TARGET_PLATFORM="aarch64-manylinux2014"

# **Nothing is treated as runtime-provided any more, and that reverses a deliberate choice.**
#
# This used to exclude boto3, botocore, s3transfer, jmespath, urllib3, python-dateutil and six on the
# grounds that Lambda supplies them and shipping copies costs ~25 MB for no behavioural gain. The
# second half was wrong, and it cost real time to find.
#
# Lambda's bundled botocore is **older than the one this repository resolves**. `InvokeHarness`
# accepts `traceParent` and `baggage` in botocore 1.43.73 — which `uv.lock` pins, which `types-boto3`
# describes, and which pyright therefore type-checks against — while the runtime's copy rejects both
# as unknown parameters. So a field the type checker approved failed at runtime, and the whole point
# of the typed-SDK rule is that an SDK mistake becomes a type error rather than a deployed failure.
# Relying on the runtime's SDK silently voids that guarantee for every AWS call, not just this one.
#
# AWS's own guidance points the same way: include the SDK in the deployment package for production to
# get version consistency and avoid runtime updates changing behaviour underneath the function.
#
# The rule is now simply "ship exactly what `uv export` resolved", which is also the only version of
# this rule that cannot drift. It costs roughly 25 MB against 250 MB of quota, of which the
# exclusions below already recovered far more.
RUNTIME_PROVIDED=()

# Pulled in transitively and never imported. A different list from the one above and for a
# different reason: those are provided by the runtime, these are genuinely unused by this code.
#
# `aws-opentelemetry-distro` depends on `opentelemetry-exporter-otlp`, a meta-package that installs
# both the gRPC and the HTTP exporters. Spans go over HTTP — `OTLPAwsSpanExporter` extends the HTTP
# exporter so it can carry a SigV4 `Authorization` header — so the gRPC half is dead weight, and it
# is the single largest thing in the layer at 17 MB of 61.
#
# Verified rather than assumed [2026-08-23]: after a full `configure_tracing`, no module whose name
# contains `grpc` appears in `sys.modules`. Not re-checked at build time, because the layer is
# cross-compiled for aarch64 and cannot be imported on the build host — so the guard is
# `tests/unit/test_tracing_bootstrap.py`, which fails if the exporter ever needs the gRPC path.
#
# `protobuf` and `googleapis-common-protos` are deliberately NOT here: the HTTP exporter still
# serialises protobuf, so those are load-bearing.
UNUSED_BY_DESIGN=(grpcio opentelemetry-exporter-otlp-proto-grpc)

# The OpenTelemetry instrumentations this code actually enables, by the suffix of their
# distribution name. An **allowlist**, not a blocklist, and that direction is the point.
#
# `aws-opentelemetry-distro` installs 45 instrumentation packages: Django, Flask, Kafka, MongoDB,
# Cassandra, Celery, Redis, SQLAlchemy, Tortoise ORM and more. This runs in Lambda and talks to AWS
# over boto3, so two of them are used and 43 are weight — and some drag real payloads behind them,
# which is how 17 MB of gRPC arrived even after `grpcio` was excluded by name: the grpc
# *instrumentation* depends on it.
#
# Safe to drop because nothing here uses `opentelemetry-instrument`, the auto-loader that discovers
# instrumentations through entry points. `bootstrap.configure_tracing` imports the two it wants,
# explicitly, and an instrumentation that is not imported is not consulted.
#
# Adding one in code means adding it here. That is deliberate friction: the failure is a loud
# `ModuleNotFoundError` at init rather than a silently absent span.
INSTRUMENTATIONS_USED=(botocore urllib3)

echo "building lambda artifacts into $OUT"
rm -rf "$OUT/layer" "$OUT/src" "$OUT/runtime"
mkdir -p "$OUT/layer/python" "$OUT/src" "$OUT/runtime"

echo "  resolving runtime dependencies"
uv export --no-dev --no-emit-project --no-hashes -o "$OUT/requirements.all.txt" >/dev/null

# Drop the runtime-provided and never-imported packages by name, keeping everything else.
awk_filter=""
for package in ${RUNTIME_PROVIDED[@]+"${RUNTIME_PROVIDED[@]}"} "${UNUSED_BY_DESIGN[@]}"; do
  awk_filter="${awk_filter}${awk_filter:+|}^${package}=="
done
# Two passes. The first drops the excluded packages and every instrumentation; the second adds back
# only the instrumentations named above, plus the base package that carries their shared machinery.
grep -vE "$awk_filter" "$OUT/requirements.all.txt" \
  | grep -vE '^\s*#|^$' \
  | grep -vE '^opentelemetry-instrumentation-' > "$OUT/requirements.txt"
for name in "${INSTRUMENTATIONS_USED[@]}"; do
  line=$(grep -E "^opentelemetry-instrumentation-${name}==" "$OUT/requirements.all.txt" || true)
  if [ -z "$line" ]; then
    echo "ERROR: opentelemetry-instrumentation-${name} is enabled in code but not resolved." >&2
    exit 1
  fi
  echo "$line" >> "$OUT/requirements.txt"
done

echo "  installing for $TARGET_PLATFORM / python $PYTHON_VERSION"
# `--no-deps` is what makes the exclusion lists above actually work, and its absence was a real
# defect. `uv export` already emits a **fully resolved** closure with every transitive dependency
# pinned, so nothing needs re-resolving. Without `--no-deps`, uv resolved each remaining line's
# dependencies again and pulled the excluded packages straight back in: `jmespath`, `urllib3`,
# `dateutil` and `six` were all shipping despite being listed as runtime-provided, and every
# instrumentation returned as a dependency of `aws-opentelemetry-distro`.
#
# The trade is that a genuine dependency dropped by a filter now fails at import rather than being
# silently restored. That is the direction to fail in: an unused 17 MB package is invisible, and a
# missing one is a `ModuleNotFoundError` at init.
uv pip install \
  -r "$OUT/requirements.txt" \
  --target "$OUT/layer/python" \
  --python-platform "$TARGET_PLATFORM" \
  --python-version "$PYTHON_VERSION" \
  --no-installer-metadata \
  --no-deps \
  --quiet

# Console launchers are generated with the build interpreter's absolute path. Lambda imports these
# distributions as modules and has no caller for their generated commands, so remove the commands
# and their exact wheel RECORD rows rather than shipping a checkout-specific interpreter contract.
echo "  pruning generated console scripts"
uv run --project "$ROOT" python "$ROOT/scripts/lambda_layer_packaging.py" \
  prune-console-scripts "$OUT/layer/python"

echo "  copying source"
# Only the three hexagonal roots. Nothing else in the repo belongs in a function.
for package in domain ports adapters; do
  cp -R "$ROOT/src/$package" "$OUT/src/$package"
done

# The data contracts and the metric dictionary, which a tool function reads at import to build
# its catalogue and its fact shapes. They are declarations rather than code, which is why they
# live outside src/ — but a function that cannot read them cannot answer anything, so they ship.
#
# Placed beside the packages rather than inside one, so `contracts_root()` finds them by the same
# upward search that finds them in a checkout. 356K, against a 250 MB unzipped quota.
echo "  copying data contracts"
cp -R "$ROOT/contracts" "$OUT/src/contracts"

# Deterministic analytical page declarations are loaded by the Presentation Lambda at container
# initialization. They are executable governance beside the data contracts, not frontend assets.
echo "  copying presentation declarations"
# Both immutable declaration generations must travel with the Presentation Lambda during
# negotiated rollout. Copy them explicitly so a missing generation fails packaging instead
# of silently changing the deployed compatibility state.
mkdir -p "$OUT/src/presentation/pages"
for generation in v1 v2; do
  test -f "$ROOT/presentation/pages/$generation/pages.yaml"
  cp -R "$ROOT/presentation/pages/$generation" "$OUT/src/presentation/pages/$generation"
done

# Bind the publisher functions' configured generation to every deterministic byte that can
# affect rendering. A content digest comment preserves pages.yaml's runtime YAML semantics.
# PublisherStack strips this one trailer line before hashing the DeployBackfill property, so
# only a declaration change re-fires the deploy backfill; a code-only deploy does not (R5-1).
echo "  binding publisher deployment generation"
publisher_input="$OUT/src/presentation/pages/v2/pages.yaml"
publisher_runtime_digest=$(
  uv run --project "$ROOT" python -m \
    adapters.jobs.preset_artifact_publisher.deployment_generation \
    --root "$OUT/src" --publisher-input "$publisher_input"
)
printf '\n# publisher-runtime-generation: %s\n' "$publisher_runtime_digest" >> "$publisher_input"

# The conversation function's handler. Under the Lambda Web Adapter the handler is a startup script
# rather than a Python symbol, and Lambda resolves a script handler from the root of the deployment
# package — so it cannot stay at the path it is version-controlled under.
#
# `cp -p` to preserve the executable bit. Without it the wrapper cannot exec the script and the
# function fails at init, which presents as a readiness-check timeout with no error of its own.
echo "  copying the streaming entry point"
cp -p "$ROOT/src/adapters/inbound/conversation/run.sh" "$OUT/src/run.sh"
if [ ! -x "$OUT/src/run.sh" ]; then
  echo "ERROR: $OUT/src/run.sh is not executable. The conversation function will fail at init." >&2
  exit 1
fi

# Any bytecode carried over from the developer's checkout goes, so what ships is compiled below by
# one interpreter with one invalidation mode rather than being a mix.
find "$OUT/src" "$OUT/runtime" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT/src" "$OUT/runtime" -name '*.pyc' -delete 2>/dev/null || true

# Precompile every module that ships, and this is the largest cold-start win available here.
#
# **Lambda's /var/task and /opt are read-only.** Python therefore cannot write a `.pyc` beside a
# `.py` at runtime: it compiles from source in memory on every cold start and cannot cache the
# result for the next one. 1,700 source files — pydantic, cryptography, the OTel SDK — were being
# parsed and compiled on every single cold start of every function.
#
# `unchecked-hash` is the load-bearing flag. The default invalidation mode stamps the source's
# mtime and size into the `.pyc`, and packaging rewrites mtimes — so a timestamp-validated `.pyc`
# is treated as stale and silently recompiled, which looks exactly like doing nothing. An
# unchecked-hash `.pyc` is used without consulting the source at all, which is correct precisely
# because a deployment artefact is immutable.
#
# Compiled with the project interpreter, whose version must match the runtime: bytecode carries a
# version-specific magic number, and a mismatch is silently ignored rather than failing.
echo "  precompiling bytecode"
compile_python="$(cd "$ROOT" && uv run python -c 'import sys; print(sys.executable)')"
"$compile_python" - <<'PYVERSION'
import sys
expected = "3.14"
actual = f"{sys.version_info.major}.{sys.version_info.minor}"
if actual != expected:
    sys.exit(
        f"ERROR: bytecode would be compiled by Python {actual} for a {expected} runtime. "
        "A magic-number mismatch is ignored silently, so this fails the build instead."
    )
PYVERSION
# Compile each artifact root separately because the dependency layer and function packages mount at
# different deployed paths. `-s` removes the exact local root before `-p` adds that deployed root,
# keeping traceback filenames useful without embedding the checkout in marshalled code objects.
"$compile_python" -m compileall -q -f --invalidation-mode unchecked-hash \
  -s "$OUT/layer/python" -p "/opt/python" "$OUT/layer/python" >/dev/null
"$compile_python" -m compileall -q -f --invalidation-mode unchecked-hash \
  -s "$OUT/src" -p "/var/task" "$OUT/src" >/dev/null
"$compile_python" -m compileall -q -f --invalidation-mode unchecked-hash \
  -s "$OUT/runtime" -p "/var/task" "$OUT/runtime" >/dev/null

compiled=$(find "$OUT/src" "$OUT/layer/python" "$OUT/runtime" -name '*.pyc' | wc -l | tr -d ' ')
sources=$(find "$OUT/src" "$OUT/layer/python" "$OUT/runtime" -name '*.py' | wc -l | tr -d ' ')
echo "    $compiled of $sources modules precompiled"
if [ "$compiled" -lt $((sources / 2)) ]; then
  echo "ERROR: most modules did not precompile; cold start would pay to compile them." >&2
  exit 1
fi

# Fail the build rather than the deploy if the wheels came out for the wrong
# architecture. A native module built for the host is the single most likely
# packaging mistake here, and it is silent until function init.
echo "  verifying architecture"
wrong=$(find "$OUT/layer/python" "$OUT/runtime" -name '*.so' -exec sh -c 'file "$1" | grep -qi "aarch64\|ARM" || echo "$1"' _ {} \; | head -5)
if [ -n "$wrong" ]; then
  echo "ERROR: these native modules are not aarch64:" >&2
  echo "$wrong" >&2
  exit 1
fi

layer_size=$(du -sm "$OUT/layer" | cut -f1)
src_size=$(du -sm "$OUT/src" | cut -f1)
runtime_size=$(du -sm "$OUT/runtime" | cut -f1)
total=$((layer_size + src_size))

echo
echo "  layer:   ${layer_size} MB"
echo "  source:  ${src_size} MB"
echo "  runtime: ${runtime_size} MB self-contained"
echo "  Lambda total: ${total} MB unzipped (quota is 250 MB for function + all layers)"

if [ "$total" -gt 200 ]; then
  echo "WARNING: approaching the 250 MB quota. A layer cannot rescue an oversized" >&2
  echo "function — switch to DockerImageFunction, which allows 10 GB." >&2
fi

echo "done"
