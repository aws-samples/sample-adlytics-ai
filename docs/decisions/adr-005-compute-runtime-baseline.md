# ADR-005 — Compute and runtime baseline

| | |
|---|---|
| **Status** | Accepted — superseded in part by ADR-015 |
| **Date** | 2026-08-18 |
| **Decision** | **Python 3.14 and Node 24 LTS, every Lambda on arm64 Graviton, and tool functions collocated one-per-domain rather than one-per-tool.** Packaging is zip plus layers, with container images as the escape hatch when total unzipped size exceeds 250 MB. |
| **Related** | [architecture](../architecture.md) §8 · [repo-structure](../repo-structure.md) · [ADR-006](adr-006-agentcore-agentic-layer.md) |


> **Superseded in part by [ADR-015](adr-015-no-remote-ci.md) (2026-08-18).** This decision assumed a remote CI pipeline. There is none: `make` is the gate and a human runs it. The packaging decision itself is unchanged — only where it runs, and that it no longer uses a Lambda base image.

> **Proposed exceptions, 2026-10-07.** [ADR-094](adr-094-run-offline-modelling-on-sagemaker-processing-without-a-vpc.md) (Proposed) would record an x86_64 exception for the SageMaker Processing modelling jobs, because Processing has no Graviton instance type in us-east-1. [ADR-090](adr-090-evaluate-budget-scenarios-at-request-time-over-stored-draws.md) (Proposed) would ship `numpy` in its own layer, attached only to the simulator presentation function and two publishers. This decision stays in force, unchanged, until they are accepted.

## Context

Four compute questions were unsettled and each one is expensive to reverse once functions exist: which language versions, which CPU architecture, how many Lambda functions the tool layer becomes, and how heavy dependencies get packaged.

The tool-count question is the sharpest. The outcome set implies on the order of twenty tools across five families. One Lambda per tool means twenty cold-start surfaces, twenty sets of configuration, twenty alarms, and twenty deployment artifacts. One Lambda for everything means a single execution role holding every permission the platform needs.

## Decision

### Runtime versions — current LTS on both sides

| Runtime | Version | Lambda identifier | Supported to |
|---|---|---|---|
| Python | **3.14** | `python3.14` | Jun 2029 |
| Node | **24 LTS** | `nodejs24.x` | Apr 2028 |

Verified 2026-08-18. This supersedes the Python 3.13 target previously recorded in `architecture.md` and `repo-structure.md`.

**Python 3.15 and Node 26 entered Lambda public preview on 2026-08-15** — three days before this ADR. They are the next upgrade, not the current pick. Preview runtimes carry no production support commitment.

One breaking detail to carry: `nodejs24.x` **dropped callback-style handlers entirely.** Async/await only.

Type checking is **pyright**, not mypy — Pylance is pyright, so the editor and CI give the same answer rather than two tools disagreeing about the same file. The tradeoff accepted is mypy's richer plugin ecosystem.

### Architecture — arm64 Graviton everywhere

AWS documents up to 40% better price-performance for Graviton across workload classes that
include web applications and microservices; that is a service-level maximum, not a measured claim
for this solution ([AWS Graviton](https://aws.amazon.com/ec2/graviton/level-up-with-graviton/)).
This workload is almost entirely I/O-bound request handling and batch Python, and the stack has no
x86-specific dependency.

Three consequences that are not optional:

- **CI must build `aarch64` wheels.** The artifact build installs against an arm64 Lambda base image. This reinforces the existing decision to build in CI rather than use `aws-lambda-python-alpha`; it does not complicate it.
- **ADOT layers are architecture-specific.** They publish as `aws-otel-python-<amd64|arm64>-ver-...`. The wrong variant fails at init, not at build.
- **The PyMC saturation fit on SageMaker Processing needs confirming, not assuming.** It is the one place a missing `aarch64` wheel would genuinely hurt. `pyarrow` and `polars` have good arm64 support.

### Tool functions — collocated by domain

**One Lambda per domain, not one per tool.** Hexagonal isolation stays in code: each tool remains an independently testable unit under `src/adapters/inbound/tool_handlers/`, and the domain logic it calls has no AWS dependency at all. Collocation is purely a *deployment* decision.

Several MCP targets in AgentCore Gateway route to one function, which dispatches on tool name.

**The boundary is domain and permission set together.** Same domain plus materially similar access collocates. A genuine permission difference is grounds to split, because collocation gives the function the *union* of its tools' permissions.

> **This knowingly trades least privilege for operability, and the trade must be bounded.** [architecture](../architecture.md) §7 requires each function to hold only the permissions it needs. A domain function holds slightly more than any one of its tools needs. That is acceptable at domain granularity and would not be at platform granularity — which is exactly why "one Lambda for all tools" is rejected below.
>
> **The IAM role can never express per-user scoping**, because IAM does not know our users. Row-level authorization is therefore enforced in tool-adapter code against the caller identity, never by the execution role. See [ADR-006](adr-006-agentcore-agentic-layer.md) for how that identity arrives.

### Packaging — zip and layers, container as escape hatch

**Layers do not raise the size limit.** The 250 MB unzipped quota covers the function *and all its layers combined*, it is a hard limit, and a maximum of five layers may be attached. Direct upload is capped at 50 MB zipped; larger artifacts go via S3.

So layers buy three things — a small function artifact, one shared copy of a dependency across collocated functions, and faster deploys — and they buy **no headroom whatsoever**.

The rule that follows:

| Situation | Packaging |
|---|---|
| Default | zip, dependencies in a shared layer per domain |
| Total unzipped (function + layers) approaches 250 MB | **container image**, 10 GB limit |
| Docker required at synth time | never — build in CI, reference with `Code.fromAsset` |

The curation path with `pyarrow` and `polars` is the likely first container case, and it is deferred along with the S3-to-canonical ETL. Every function in the near-term scaffold is light, so zip is correct for all of them today.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **One Lambda per tool** | Twenty cold-start surfaces, twenty configurations and twenty alarms for a platform with one team. The operational cost is paid continuously; the least-privilege benefit is mostly theoretical because tools within a domain read the same stores anyway |
| **One Lambda for all tools** | A single execution role spanning Athena, DynamoDB, Bedrock and S3. Blast radius of any code defect becomes the whole data plane, and the cold-start artifact grows with every tool added |
| **x86_64** | No dependency requires it, and it costs roughly a third more for the same throughput |
| **Python 3.15 / Node 26** | Public preview as of 2026-08-15. No production support commitment |
| **mypy** | The editor would disagree with CI, and the disagreeing one blocks the merge |
| **Layers to escape the size limit** | Factually does not work — layers share the same 250 MB budget. Worth recording because it is a common and expensive misconception |
| **`aws-lambda-python-alpha`** | Alpha, and puts a Docker daemon on the critical path of every synth. Unacceptable for a stack deployed into a customer account |

## Consequences

- Runtime upgrades become a scheduled activity. Python 3.15 and Node 26 leave preview within roughly a year, and 3.14 support ends Jun 2029.
- Every CI artifact build is architecture-pinned. A developer on x86 cannot produce a deployable artifact locally without cross-building — CI is the only supported path, which is already the rule.
- Domain boundaries now have a deployment consequence, so the domain decomposition in [ADR-003](adr-003-canonical-model-decomposition.md) is load-bearing for infrastructure as well as for the data model. Moving a tool between domains is a redeploy of two functions.
- Per-domain layers must be versioned and cannot drift from the function that consumes them.
- Cold starts improve for the common case and worsen for the rare one: a domain function is larger than a single-tool function would be, so when it does cold start it is slower.

## Review trigger

Revisit when any of these becomes true:

- a domain function's unzipped size passes roughly 200 MB, making the 250 MB ceiling a live risk
- two tools in one domain need materially different IAM permissions, which means the domain boundary is wrong or the split rule applies
- measured cold start on a domain function exceeds the latency budget in [Q-12](../decision-coverage-registry.md#question-register-q-nn)
- an `aarch64` wheel proves unavailable for a required dependency
