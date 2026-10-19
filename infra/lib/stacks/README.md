# Stacks

One file per stack, split by change cadence and removal policy rather than by
service. Grouping by service would put a bucket that must never be deleted in the
same stack as something that changes weekly.

**Which stack holds what, how often each changes and which retain on delete is
owned by [`docs/repo-structure.md`](../../../docs/repo-structure.md) §3.** It is not
restated here, because a second table is a second thing to keep in step and this
one was wrong for a while — it claimed seven stacks after eleven existed.

**The deploy sequence is `DEPLOY_ORDER` in [`lib/config`](../config/index.ts) and
nowhere else.** [`lib/deploy-order.ts`](../deploy-order.ts) turns it into order-only stack
dependencies, so `make deploy-all` follows it; a human still decides when to run it. The
customer-facing walk through it is in the [repository README](../../../README.md#deploy).

Rules that apply to every stack here, from ADR-005, ADR-012 and the CDK steering:

- **Every cross-stack value goes through SSM** under `/adlytics/<env>/...`. Never
  pass a construct reference across a stack boundary and never hand-write
  `Fn::ImportValue`: exports cannot be updated while imported, and cannot cross
  regions — which CloudFront's `us-east-1` certificate requirement makes
  mandatory.
- **Never put a RETAIN resource in a DESTROY stack.** A stack is only as safe as
  its most dangerous member.
- **The raw zone bucket lives in `DataV2Stack`**, which is RETAIN. Its name is
  published to SSM at `/adlytics/<env>/data/v2/raw-bucket`, and that name is the
  one identifier the producer that operates extraction, outside this repository,
  has to configure.
- **No stack creates a KMS key for data.** Data buckets are SSE-S3 per
  [ADR-018](../../../docs/decisions/adr-018-encryption-at-rest.md). Setting
  `config.data.encryption` to `kms_cmk` refuses at synth
  (`lib/mixins/storage.ts`) rather than deploying buckets no consumer can read.
  The one key in the platform is `TokenServiceStack`'s asymmetric signing key
  ([ADR-077](../../../docs/decisions/adr-077-carry-the-end-user-identity-to-every-hop-through-token-exchange.md)
  §3), which encrypts nothing.
- Per-environment values come from `lib/config`, never from `cdk.json` context.
- Every Lambda is `arm64` with the `python3.14` runtime. Config types
  (`lambdaArchitecture`, `pythonRuntime`) admit only those values; stacks read the
  runtime from config, and read the architecture from it or name `ARM_64` directly.
- Athena is L1 only and requires an explicit `BytesScannedCutoffPerQuery`.
- `FrontendStack` uses Origin Access Control, not the legacy Origin Access
  Identity, and must expose a response-headers policy — CSP is load-bearing for
  the cookie auth model in ADR-007.
- Conditional stacks are composed in `bin/app.ts` behind a config flag; the
  repo-structure table names each flag. `ObservabilityStack` is gated on
  `config.transactionSearchEnabled` because its resources *are* the account-wide
  Transaction Search setting and its span-delivery logs policy, and CloudFormation
  rejects a template with an empty `Resources` section. Turning the flag off later does not delete
  it; that takes `cdk destroy Adlytics-Observability-<env> --exclusively` (without `--exclusively`, every later stack is destroyed too).
