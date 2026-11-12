# Platform

**Land your Amazon Marketing Cloud (AMC) results in your own AWS account and serve them as governed figures, fast pages and a signed-in website, with one deploy command.**

One of the [Adlytics AI solutions](../../../README.md#solutions). It deploys with the full sample, which is sample code and not production-ready: read [Cost](../../../README.md#cost) and [Security considerations](../../../README.md#security-considerations) first.

This is the foundation every other solution runs on. It is for the builder who deploys and runs the sample, and for an engineer who wants to lift one block into another project. An analyst uses it to trace where a figure came from. [Who it is for](../../business-context.md#who-it-is-for) describes every reader.

## What it gives you

| Building block | What it gives you | Built on |
|---|---|---|
| [Data landing](#data-landing) | A cross-account drop point for your own extraction pipeline. A delivery that fails its data contract is refused before anything is published. | Amazon S3, AWS Lambda, Amazon SQS |
| [Governed data lake](#governed-data-lake) | Immutable, versioned tables, with a pointer to the version readers use. | Amazon S3, AWS Glue, Amazon Athena, Amazon DynamoDB |
| [Metric layer](#metric-layer) | One declared, tested definition per metric, shared by every page and the [analytics assistant](../analytics-assistant/README.md). | AWS Lambda, Amazon Athena |
| [Page baking](#page-baking) | Pages computed when data is published, so they load fast and every viewer sees the same figures. | AWS Lambda, Amazon SQS, Amazon S3, Amazon DynamoDB |
| [Secure edge](#secure-edge) | Invite-only sign-in, a web application firewall and server-side session tokens in front of the website and its APIs. | Amazon CloudFront, AWS WAF, Amazon Cognito, Amazon API Gateway |
| [One-command deploy](#one-command-deploy) | `make deploy-all` deploys every stack in order, and shows the target account, Region and changes before it deploys anything. | AWS CDK, AWS CloudFormation, AWS Systems Manager Parameter Store |

## Architecture

**Architecture diagram — Adlytics AI end to end, from your AMC data to pages and answers in the browser.**

![Architecture diagram: Adlytics AI end to end, from your AMC data to pages and answers in the browser. Your extraction pipeline, in a separate producer AWS account, runs the declared AMC SQL and lands each result in the deployment's data landing bucket, which accepts AMC query packs only and refuses a delivery that fails its data contract; Vendor Central reports can't be landed yet. A valid delivery is published as an immutable version in the data lake. The metric layer computes figures from it with tested SQL on Athena. Page baking builds pages from those figures, one page at a time by default; the opt-in operational status plane builds every page as one set. Insight narration on Amazon Bedrock writes the text of the insight cards that aren't fixed rules, from a page's own figures, and that text joins the served page by its revision. The analytics assistant calls the metric layer as tools. Pages, card text and answers reach the signed-in browser through the secure edge: CloudFront, AWS WAF and Cognito sign-in. Everything except your pipeline and the browser runs in one deployment AWS account created by make deploy-all.](diagrams/end-to-end.svg)

> **Takeaway.** Data moves one way, from your pipeline to the browser, and every figure on a page or in an answer starts in the tested metric layer, never in a model.

- Extraction stays outside the sample. Your pipeline runs in a second AWS account you own, so this deployment never holds your Amazon credentials. Vendor Central reports can't be landed yet ([Known limits](../../../README.md#known-limits)).
- Nothing is published until it passes its data contract, and a published version never changes. Readers follow a pointer to the current version.
- Pages and the assistant read the same metric layer, so a figure on a page and the same figure in chat come from the same code.
- By default, pages rebuild one at a time after new data is published. The opt-in operational status plane rebuilds every affected page as one set; it is live-accepted only in the dev environment.

### Data landing

- **Problem.** An outside pipeline has to deliver results without this deployment holding Amazon credentials, and a malformed file must never be half-loaded.
- **How it works.** Producer onboarding, off by default, creates a landing role your pipeline assumes from its own account with an external ID you choose. For each AMC query pack, your pipeline runs the pack's exact SQL, writes the result, then writes a registration object last. The deployment checks the registration against the pack and turns it into a governed manifest, which starts the load. This handshake accepts AMC query packs only; Vendor Central reports can't be landed through it.
- **Read more.** [Load your data](../../../README.md#load-your-data) (setup steps), [source acquisition](../../capabilities/source-acquisition/README.md), the [producer CLI](../../capabilities/operational-data-flow-status/operator-tools.md#producer-cli), [ADR-050](../../decisions/adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md), [AMC source facts](../../sources/amc.md).

### Governed data lake

- **Problem.** Totals stop adding up when a partial or repeated load mixes with good data, and nobody can say which version a figure came from.
- **How it works.** Each delivery is validated against its data contract, written to source-aligned tables and then to canonical tables (Apache Iceberg tables in the Glue Data Catalog, read through Athena), and published as an immutable version. A refused delivery leaves the last good version in place. A pointer, `CURRENT`, selects the version readers use.
- **Read more.** [Load path](../../capabilities/load-path/README.md), [data layer](../../capabilities/data-layer/README.md), [publication state](../../capabilities/publication-state/README.md), [canonical model](../../canonical-model.md), [understanding the data model](../../understanding-the-data-model.md), [ADR-029](../../decisions/adr-029-iceberg-for-the-curated-zone.md), [ADR-044](../../decisions/adr-044-materialize-source-aligned-data-and-centralize-canonical-publication.md).

### Metric layer

- **Problem.** The same metric computed two ways gives two answers, and a language model asked for a number can invent one.
- **How it works.** Every metric is declared once in [`contracts/shared/metric-dictionary.yaml`](../../../contracts/shared/metric-dictionary.yaml). The query path plans each request against those declarations, composes the SQL, runs it on Athena over the current version, and returns figures with their caveats (coverage, suppression, measurement basis). When the data can't support an answer, it refuses before any query runs.
- **Read more.** [Query path](../../capabilities/query-path/README.md), [provenance chain](../../concepts/provenance-chain.md), [semantic accuracy](../../concepts/semantic-accuracy.md).

### Page baking

- **Problem.** Computing a page each time someone opens it is slow, and two viewers could see different figures.
- **How it works.** A new published version starts one bake per page and window. The bake computes that page with the metric layer and writes it as an immutable page artifact. The page's pointer moves only when the page builds, with data or a designed absence that says what is missing. A failed bake leaves the page on its last good version, and a bake that keeps failing raises an alarm.
- **Default versus the operational plane.** By default a deployment refreshes pages one at a time, so for a short while two pages can show different publications. The set-at-once refresh, where every affected page moves together or none does, comes with the opt-in operational status plane, live-accepted only in the dev environment ([Known limits](../../../README.md#known-limits)).
- **Read more.** [Preset artifact publisher](../../ui/preset-artifact-publisher.md#the-pipeline), [first bake and recovery](../../ui/preset-artifact-publisher.md#first-bake-and-recovery), [Sizing for large catalogs](../../../README.md#sizing-for-large-catalogs), [ADR-070](../../decisions/adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md).

### Secure edge

- **Problem.** An analytics site over customer data must not be open to the internet, and a token stored in the browser can be stolen by injected script.
- **How it works.** Every request enters through CloudFront and an AWS WAF web ACL with managed rule sets, body-size rules and a per-IP rate limit. Users are invited; self sign-up is off. Cognito tokens stay on the server, the browser holds only an `HttpOnly` session cookie, and an authorizer checks every API request. Requests that bypass CloudFront are refused, and the site sends a Content Security Policy.
- **Read more.** [Edge entry](../../concepts/authentication.md#edge-entry-adr-078), [authentication](../../concepts/authentication.md), [ADR-007](../../decisions/adr-007-auth-cookie-bff.md), [ADR-008](../../decisions/adr-008-frontend-hosting.md).

### One-command deploy

- **Problem.** The sample is many CloudFormation stacks that must deploy in order, and a first deploy aimed at the wrong account is hard to undo.
- **How it works.** `make deploy-all` takes an explicit `PROFILE` and `ENV` with no default. It shows the account, Region and changes first, and deploys only when run again with `APPROVE=1`. Stacks pass values to each other through Systems Manager parameters, and stacks that hold data are kept apart from stacks that can be destroyed and redeployed.
- **Read more.** [Deploy](../../../README.md#deploy), [`scripts/deploy-all.sh`](../../../scripts/deploy-all.sh), [current stack layout](../../repo-structure.md#current-stack-layout), [ADR-012](../../decisions/adr-012-stack-composition-and-config.md), [ADR-136](../../decisions/adr-136-deploy-a-shared-asset-change-as-one-deploy-all-from-one-declared-source.md).

## Security and data residency

- **Your data stays in your account.** The sample includes no AMC or Vendor Central client, and your pipeline lands data from an account you own. Question screening uses a guardrail profile and the example model IDs are cross-Region inference profiles; both may process a request in another Region of the same geography.
- **Sign-in, firewall and browser policy.** Invite-only Cognito sign-in, AWS WAF at the edge and on sign-in, server-side tokens, and a Content Security Policy, as described in [Secure edge](#secure-edge).
- **Least-privilege roles.** Each Lambda function writes only its own log group ([ADR-085](../../decisions/adr-085-lambda-roles-write-only-their-own-log-group.md)), the landing role can be assumed only by the pipeline role you name, with your external ID, and analytical reads are scoped to this one deployment ([ADR-054](../../decisions/adr-054-scope-analytical-visibility-to-the-customer-deployment.md)).
- **Before production.** Read [Security considerations](../../../README.md#security-considerations): it lists what the sample does and what to change, such as your own domain and certificate, and customer-managed encryption keys ([ADR-018](../../decisions/adr-018-encryption-at-rest.md)).

## Deploy, operate and clean up

The platform deploys with the full solution; there is no separate install. Start at the [hub README](../../../README.md).

| Step | Where |
|---|---|
| Check the account, access to the three Amazon Bedrock models, and the Athena and Lambda quotas | [Prerequisites](../../../README.md#prerequisites) |
| Deploy with `make deploy-all` | [Quickstart](../../../README.md#quickstart), [Deploy](../../../README.md#deploy) |
| Sign in, then subscribe to the alarm topic, which has no subscriber until you add one | [Check the deployment](../../../README.md#check-the-deployment) |
| Turn on producer onboarding and land your first AMC results; pages stay on their designed empty states until then | [Load your data](../../../README.md#load-your-data) |
| Investigate alarms, dead letters and traces | [Operator tools](../../capabilities/operational-data-flow-status/operator-tools.md), [observability](../../concepts/observability.md) |
| Know what it costs while idle and in use | [Cost](../../../README.md#cost) |
| Remove it; resources that hold data are kept and bill until you delete them | [Clean up](../../../README.md#clean-up) |

These [Known limits](../../../README.md#known-limits) rows concern the platform: **Load Vendor Central reports**; **Load one delivery of more than 20,000 rows, or one table unit of more than 50,000**; **See AMC history older than 13 months, or data fresher than your pipeline lands it**; **See every page move to new data at the same moment**; and **Trust every operations signal**.

## Reuse it

Each block can be lifted on its own, with the blocks its "It needs" column names. The [Extend it](../../../README.md#extend-it) table says which file to edit and which generator to run for each change.

| To reuse | Take | It needs |
|---|---|---|
| Data landing and the governed lake | The data contracts in [`contracts/amc/`](../../../contracts/amc/) and [`contracts/vendor-central/`](../../../contracts/vendor-central/), and the `DataV2Stack` and `IngestionStack` stacks | The shared Lambda layer stack, `DependenciesStack`; an extraction pipeline of your own |
| The metric layer | [`contracts/shared/metric-dictionary.yaml`](../../../contracts/shared/metric-dictionary.yaml) and the [query path](../../capabilities/query-path/README.md) | The governed lake's published versions |
| Page baking | [`presentation/pages/v2/pages.yaml`](../../../presentation/pages/v2/pages.yaml) and the `PublisherArtifactStoreStack` and `PublisherStack` stacks | The metric layer and the governed lake |
| The secure edge | The `EdgeSecurityStack`, `FrontendStack` and `AuthStack` stacks | An API to protect (here `ApiStack`), but nothing from the data side; the CloudFront web ACL must be in us-east-1 |
| The deploy pattern | [`scripts/deploy-all.sh`](../../../scripts/deploy-all.sh) and the stack order in [`infra/lib/config/index.ts`](../../../infra/lib/config/index.ts) | AWS CDK v2 |

[Current stack layout](../../repo-structure.md#current-stack-layout) lists every stack and whether it keeps data.

## Design highlights

- **Figures come from code.** Every figure on a page or in chat starts in the metric layer; the model chooses tools and explains their results under the [no invented figures](../analytics-assistant/README.md#how-it-stays-trustworthy) rule ([solution strategy](../../architecture.md#5-solution-strategy)).
- **Publish, then point.** Data versions and page artifacts are both immutable, and each pointer moves only after its new version validates or builds, so a failure leaves the last good version serving ([publication state](../../capabilities/publication-state/README.md), [page isolation](../../ui/preset-artifact-publisher.md#page-isolation)).
- **Extraction outside the boundary.** Your pipeline lands data through a cross-account role and a registration handshake, so the deployment never holds Amazon credentials ([ADR-050](../../decisions/adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md)).

## Learn more

[Architecture overview](../../architecture.md) · [Documentation index](../../README.md) · [Repository structure](../../repo-structure.md) · [Validation results](../../../validation/RESULTS.md) · [Hub README](../../../README.md#solutions)
