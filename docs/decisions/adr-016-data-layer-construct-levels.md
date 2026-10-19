# ADR-016: The data layer is hand-written L1, and no alpha package is adopted

Date: 2026-08-19
Status: Accepted — Decision scope clarified 2026-08-19, see the amendment note in Decision; reopened for Glue and the AgentCore Policy area 2026-10-16, see Review trigger

## Context

`.kiro/steering/infra-style-cdk.md` is dated 2026-08-17 and carries an explicit
instruction to re-check module stability before adopting a construct. It made two
claims:

- Athena and Lake Formation are L1 only.
- Glue, S3 Tables and the AgentCore Policy submodule are alpha.

Re-checked 2026-08-19 against the version this repository actually pins,
`aws-cdk-lib` 2.265.0, read from `infra/package.json` [verified]. The evidence is
the installed library in `infra/node_modules`, which cannot be stale relative to
what this repository compiles against, corroborated afterwards by the CDK API
reference.

The probe, re-runnable:

```
cd infra && node -e "
const m = require('aws-cdk-lib/aws-glue');
const keys = Object.keys(m).sort();
console.log(keys.filter(k => k.startsWith('Cfn') === false));
"
```

| Service | Module | Exports | L2 in `aws-cdk-lib` | Separate alpha package |
|---|---|---|---|---|
| Glue | `aws-cdk-lib/aws-glue` | 27, every one `Cfn*` | none | `@aws-cdk/aws-glue-alpha` 2.265.0-alpha.0 |
| Athena | `aws-cdk-lib/aws-athena` | 6, every one `Cfn*` | none | **none exists** |
| Lake Formation | `aws-cdk-lib/aws-lakeformation` | 7, every one `Cfn*` | none | **none exists** |
| S3 Tables | `aws-cdk-lib/aws-s3tables` | 5, every one `Cfn*` | none | `@aws-cdk/aws-s3tables-alpha` 2.265.0-alpha.0 |

All four module rows are [verified] — each module was required and its exports
enumerated, and the non-`Cfn` set is empty in every case. Each module also ships
the CDK's CFN-only disclaimer, which the API reference repeats:
[aws_glue](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_glue-readme.html),
[aws_athena](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_athena-readme.html),
[aws_lakeformation](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_lakeformation-readme.html),
[aws_s3tables](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_s3tables-readme.html).
T1 and T2 agree, so nothing here rests on a snippet.

**The steering is half right, and the wrong half matters.** Athena and Lake
Formation are exactly as described. Glue and S3 Tables are not "alpha" — the
service modules inside `aws-cdk-lib` are stable L1, and what is alpha is an
*optional, separately versioned L2 package* nobody is obliged to install. Read as
written, the steering says the data layer has no stable path. It has one, for all
four services.

**Neither alpha package is deprecated or frozen** [verified, npm registry
2026-08-19]. `@aws-cdk/aws-glue-alpha` and `@aws-cdk/aws-s3tables-alpha` are both
at 2.265.0-alpha.0, both published 2026-08-13, both declaring a peer of
`aws-cdk-lib` `^2.265.0`, and neither carries a deprecation notice on any
published version — 0 of 370 and 0 of 94 respectively. They track the stable line
release for release. That is a materially different situation from
`@aws-cdk/aws-apigatewayv2-alpha`, which the same steering file correctly calls
deprecated and frozen, and the steering's single "alpha" label collapses the two.
Registry read at `https://registry.npmjs.org/@aws-cdk%2Faws-glue-alpha` and
`https://registry.npmjs.org/@aws-cdk%2Faws-s3tables-alpha`.

**Negative results, recorded so nobody searches again.** There is no
`@aws-cdk/aws-athena-alpha` and no `@aws-cdk/aws-lakeformation-alpha`. Both
registry lookups returned 404 [verified].

**A second AgentCore area is L1 only, found 2026-08-22 and now gated: `Harness`.**
It is reachable only as `CfnHarness` [verified 2026-08-22 against 2.265.0], and it
matters more than a missing L2 usually would. `CfnHarness` is the entire managed
agent loop declared as one resource — model, system prompt, the gateway it reads
tools from, memory, conversation truncation, and `maxIterations`/`maxTokens` — with
only `executionRoleArn`, `harnessName` and `model` required. So the agentic layer is
built around a resource that has no L2, and an L2 arriving would change how
`AgentStack` should be written rather than merely offering nicer syntax. The gate now
asserts it, scoped by name the same way Policy is, and the mechanism was verified to
*fail* against `Gateway`, which does ship stable L2s.

**One finding outside the data layer**, because the same steering sentence covers
it. `aws-cdk-lib/aws-bedrockagentcore` at 2.265.0 has submodules `common`,
`evaluation`, `gateway`, `identity`, `memory`, `network`, `runtime` and `tools` —
there is no `policy` submodule, and Policy is reachable only as `CfnPolicy` and
`CfnPolicyEngine` [verified]. So AgentCore Policy is L1 only, not alpha, while
other AgentCore areas do ship stable L2 constructs. Recorded here rather than
acted on; it belongs to the agentic layer.

Neither `FoundationStack` nor `DataStack` exists yet — `infra/lib/stacks` holds
`api-stack.ts`, `auth-stack.ts` and `frontend-stack.ts` [verified]. This decision
is therefore made before the code, not retrofitted onto it.

## Decision

**The Glue, Athena, Lake Formation and S3 Tables resources** in `FoundationStack`
and `DataStack` are written entirely in L1 `Cfn*` constructs from `aws-cdk-lib`
2.265.0. No alpha package is added to `infra/package.json`.

> **Amendment, 2026-08-19.** This sentence originally read "`FoundationStack` and
> `DataStack` are written entirely in L1", which over-reached beyond the evidence
> above. The Context probe, the bullets below and the Review trigger all cover
> exactly four services, and `infra/scripts/check-construct-levels.cjs` gates
> exactly those four plus AgentCore Policy and Harness. S3 and KMS appear in none of them, have
> mature stable L2s, and `FrontendStack` already uses `s3.Bucket` — so the literal
> reading would have put two idioms for the same resource type in one repository
> and required hand-setting encryption, versioning, public-access blocking and TLS
> enforcement on every bucket, each one a chance to omit one. Scoped, not reversed:
> the four services below are unchanged.
>
> **Resources with a stable L2 in `aws-cdk-lib` use it.** For S3 and KMS
> specifically, `grant*` is permitted: those grants are narrower and better tested
> than the equivalent hand-written policy, and hand-authored bucket policies are
> where IAM actually goes wrong. The breadth risk this decision guards against
> lives in Glue and Lake Formation, where a grant is its own resource type and the
> IAM policy is a separate second thing to write. Note that
> [ADR-018](adr-018-encryption-at-rest.md) removes the KMS key from V1 entirely, so
> in practice `FoundationStack` is S3 buckets and SSM parameters.

- **Glue** — `CfnDatabase`, `CfnTable`, `CfnCrawler`, `CfnJob`,
  `CfnDataQualityRuleset`, `CfnTableOptimizer` for Iceberg compaction. Hand-written.
- **Athena** — `CfnWorkGroup` and `CfnDataCatalog`. Hand-written.
- **Lake Formation** — `CfnDataLakeSettings`, `CfnPrincipalPermissions`, `CfnTag`,
  `CfnTagAssociation`. Note that a Lake Formation grant is its own resource in
  this module rather than an IAM statement, so the grant and the IAM policy are
  two separate things to write, not one.
- **S3 Tables** — `CfnTableBucket`, `CfnNamespace`, `CfnTable`,
  `CfnTableBucketPolicy`, `CfnTablePolicy`. Hand-written.
- **Every IAM policy for these resources is written explicitly.** L1 constructs
  have no `grant*` methods, so there is no implicit policy to inherit and no
  chance of one widening silently on a library bump.

Cross-stack values still go through SSM per [ADR-012](adr-012-stack-composition-and-config.md);
nothing here changes that, and L1 does not make it harder — a `Cfn*` resource's
`ref` and `attr*` are plain tokens.

## Consequences

**What this costs, plainly.** L1 is verbose and unhelpful. Property names are the
CloudFormation ones, not the CDK's, so `tableInput` nests where an L2 would have
flattened. There are no defaults, so encryption, retention and removal policy must
be set on every resource — which is what `infra/lib/mixins` exists for, and this
decision raises its importance from convenient to load-bearing. And IAM is entirely
ours: an over-broad `Resource: "*"` in the data layer is now a thing a human has to
not write, with no library defending against it.

**Why the trade is right here anyway.** The data layer is precisely where an
inherited `grant*` is worst — it is the layer holding customer data, and a grant
whose breadth nobody chose is the failure mode that matters. Writing the policies
by hand makes the blast radius of each one visible in review. The verbosity buys
something.

**The pin consequence, avoided rather than accepted.** No alpha dependency means
no exact-version pin to maintain in lockstep. Had either been adopted, the pin
would have to be exact with no caret, and because the peer is `aws-cdk-lib`
`^2.265.0`, a library bump and an alpha bump become one change that cannot be split
— which is a real constraint, since the two lines version independently by design
([ADR-005](adr-005-compute-runtime-baseline.md)).

**What stays true only for now.** Every finding above is about `aws-cdk-lib`
2.265.0 on 2026-08-19 and about nothing else. Both alpha packages are healthy and
actively released, so the likeliest future is that one or both graduate into
`aws-cdk-lib` as stable L2s. This ADR is dated for that reason.

## Alternatives rejected

**Adopt `@aws-cdk/aws-glue-alpha`.** It is the strongest candidate — genuinely
useful L2s for databases, tables and jobs, actively maintained, not deprecated.
Rejected because alpha means breaking changes between minor versions, and the Glue
catalogue is the one part of `DataStack` that gets touched on every contract change
([ADR-012](adr-012-stack-composition-and-config.md) marks it as changing often).
A construct API that breaks under us on the most frequently edited stack is the
worst place to take that risk, and the L1 surface for what we need is small.

**Adopt `@aws-cdk/aws-s3tables-alpha`.** Same reasoning, weaker case: the S3 Tables
L1 surface is five resources, so the L2 saves less and the alpha exposure is the
same.

**Wrap the L1s in our own L2 constructs now.** Tempting, and eventually right.
Rejected as premature: an abstraction written before there is a second caller
encodes one caller's shape. Revisit when `DataStack` has repeated the same L1 group
twice, and put it in `infra/lib/constructs` when it does.

**Take a third-party L2 from Construct Hub.** Each module README suggests this.
Rejected on supply chain: a community construct in the data layer generates the IAM
this decision is specifically about writing ourselves, and it is not versioned with
`aws-cdk-lib`.

**Wait for the L2s to stabilise before starting the data layer.** Rejected because
the wait has no end date, and L1 to L2 is a mechanical refactor with synth-diff
coverage, unlike the reverse.

## Review trigger

An `aws-cdk-lib` bump. Re-run the probe above for all four services on every bump.
A non-empty non-`Cfn` export list on any of them means a stable L2 has landed and
this decision should be reopened for that service.

**Triggered 2026-10-16 (UTC), reopened for Glue and the AgentCore Policy area.** The bump to
`aws-cdk-lib` 2.273.0 was made to fix A-CVE-CDK-BUNDLED-001: it is the first release found that
bundles `brace-expansion` 5.0.12. It ships stable L2s in `aws-cdk-lib/aws-glue` (103 exports,
including `Database`, `S3Table` and `Catalog`) and in `aws-cdk-lib/aws-bedrockagentcore` for
Policy (`PolicyEngine`, `Policy`, `PolicyStatement` and others), read from the installed library by
`make check-construct-levels` [verified 2026-10-16]. Athena, Lake Formation and S3 Tables are
still L1 only.

The decision for those two areas is open and has not been made. Until it is, nothing changes:
the existing `glue.Cfn*` and `agentcore.CfnPolicy*` resources stay as written. Moving a deployed
resource to an L2 can change its logical ID or properties, so it is a deployed-architecture change
that a security upgrade does not carry. `infra/scripts/check-construct-levels.cjs` no longer
asserts L1-only for these two areas, because that premise is now false. It still asserts it for
Athena, Lake Formation and S3 Tables.
