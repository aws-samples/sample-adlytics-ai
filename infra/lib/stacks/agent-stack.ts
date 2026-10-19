// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  agentClientId,
  agentRouteGuardrailName,
  gatewayTokenAudience,
  pageContextTableName,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
  toolGatewayName,
} from '../config';
import { loadGeneratedTools } from '../constructs/agent/generated-tools';
import { attachGatewayPolicyEngine } from '../constructs/agent/gateway-policy';
import { routeGuardrail } from '../constructs/agent/route-guardrail';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { dataBucketName } from '../mixins/storage';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

/** `ApplyGuardrail` accepts `DRAFT`; see `routeGuardrail` for why no version resource exists. */
const GUARDRAIL_VERSION = 'DRAFT';

/**
 * The tool functions an AgentCore Gateway invokes, one per domain.
 *
 * DESTROY. Everything here is rebuildable from source, and nothing in it holds data — permanent
 * Semantic V2 storage and state live in the RETAIN `DataV2Stack`.
 *
 * Deploys after `ObservabilityStack`, per `DEPLOY_ORDER`. That order is not cosmetic: a span
 * destination is fixed when an agent is created, so account-wide Transaction Search has to be on
 * first or the agent this stack will eventually host can never deliver spans at all.
 *
 * **Two functions, and the split is an IAM boundary rather than tidiness.** The collocation rule is
 * one Lambda per domain with several Gateway targets routing to it, and its stated exception is that
 * tools whose permissions differ materially must not share a function, because the function holds
 * the union of them. Both write only transient nonce-bound grounding. `measurement` additionally
 * reads Athena, Glue, S3 and serving state; `catalog` reads no business data because its declarations
 * ship in the artefact. Collocating them would hand capability introspection the lake read grants,
 * permanently, to answer "what metrics exist" — the widening the exception prevents.
 *
 * **The tool definitions are generated, never written here.** A tool's input schema is what the
 * model reads to decide what to send, and what Gateway validates against before the function is
 * invoked. It is declared once in `src/adapters/inbound/tool_handlers/schemas.py`, beside the code
 * that dereferences those names, and generated into `contracts/generated/tools.json`. This stack
 * reads that file. Writing the schema here as well would be one fact in two homes, and it would
 * drift the first time an argument changed.
 *
 * **Stack boundary.** `AgentStack` owns the JWT-authorized Gateway, its REQUEST interceptor, tool
 * targets, canonical Guardrail,
 * measurement operation identity, and opaque presentation-selection records. Retained evidence
 * lives in dedicated RETAIN stacks.
 */
export interface AgentStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Directory holding the Python source tree, including the bundled `contracts/`. */
  readonly sourceAssetPath: string;
  /**
   * Exact staged `contracts/generated/tools.json` included in `sourceAssetPath`.
   *
   * Resolved in `bin/app.ts` like every other asset path, so this file stays free of any knowledge
   * of the repository layout.
   */
  readonly toolSchemaPath: string;
}

export class AgentStack extends cdk.Stack {
  /** Short-lived semantic lookup context available to the measurement tool. */
  public readonly pageContextTable: dynamodb.Table;

  /** Answers a measurement question. Reads the curated lake. */
  public readonly measurementFunction: lambda.Function;

  /** Answers what can be asked at all. Reads no business data; writes transient grounding. */
  public readonly catalogFunction: lambda.Function;

  /**
   * The Gateway REQUEST interceptor (ADR-077 §5). Forwards the caller's bearer to the target and
   * strips reserved `_adl_` arguments; holds no grant but span export.
   */
  public readonly interceptorFunction: lambda.Function;

  /** The Agent-owned canonical DRAFT Guardrail applied by Runtime and inbound routes. */
  public readonly guardrail: ReturnType<typeof routeGuardrail>;

  /** The MCP surface an agent reaches the tools through. */
  public readonly gateway: agentcore.Gateway;

  /**
   * The Cedar engine evaluating every call across the gateway, when layer 2 is enabled.
   *
   * `undefined` when `gatewayGuardrails.enabled` is false, which is what a checkout ships. Held so
   * the outputs can name it: an operator reading the console needs to know whether the control is
   * present, and "no output" is a weaker answer than a stated absence.
   */
  public readonly policyEngine: agentcore.CfnPolicyEngine | undefined;

  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);
    const { config } = props;

    // Published by FrontendStack and read at deploy under the deployer's credentials, so no
    // function needs `ssm:GetParameter` for it. Present because configuration resolves as a whole:
    // a container that cannot resolve every key fails at init rather than on a later request.
    const publicOrigin = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publicBaseUrl'),
    );
    const authParam = (key: 'userPoolId' | 'userPoolClientId' | 'authDomain'): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const semanticV2Bucket = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'semanticV2BucketName'),
    );
    const semanticV2CurrentDatabase = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'semanticV2CurrentDatabase'),
    );

    // Read from SSM rather than declared here. `DependenciesStack` publishes one `LayerVersion` for
    // the whole platform, replacing a copy declared in this stack and another in `ApiStack` over the
    // *same* built asset — one closure, published twice, on independently drifting version streams.
    //
    // The ARN carries the version, so what a function attaches is pinned at deploy time. A layer
    // rebuilt after this stack was last deployed does not reach these functions until they are
    // redeployed, which is the safe direction.
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'dependenciesLayerArn'),
      ),
    );

    this.pageContextTable = new dynamodb.Table(this, 'PageContextTable', {
      tableName: pageContextTableName(config.envName),
      partitionKey: { name: 'lookup_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ADR-077 §5: every tool verifies its caller's Gateway token itself. The issuer and the signing
    // key's ARN come from TokenServiceStack through SSM; the audience and client are derived from
    // the environment exactly as the Gateway authorizer derives them.
    const tokenSigningKeyArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'tokenSigningKeyArn'),
    );
    const callerEnvironment: Record<string, string> = {
      ADL_TOKEN_ISSUER: ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'tokenIssuerUrl'),
      ),
      ADL_TOKEN_SIGNING_KEY_ARN: tokenSigningKeyArn,
      ADL_GATEWAY_TOKEN_AUDIENCE: gatewayTokenAudience(config.envName),
      ADL_AGENT_CLIENT_ID: agentClientId(config.envName),
    };

    const environment = {
      ...this.toolEnvironment(
        config,
        publicOrigin,
        authParam,
        semanticV2Bucket,
        semanticV2CurrentDatabase,
      ),
      ...callerEnvironment,
    };

    this.measurementFunction = this.pythonFunction('MeasurementTool', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.tool_handlers.measurement_entrypoint.handler',
      environment,
      // 1024 MB. Not a memory requirement — a latency one. At init this resolves configuration,
      // parses the metric dictionary and every data contract, and builds two adapters; per request
      // it maps rows into `Decimal`. CPU scales with memory, and a user is waiting on both.
      memoryMb: 1024,
      // Above the adapter's own 30 s Athena budget (`MAX_WAIT_SECONDS`) with room for init, the
      // result fetch and the freshness read. **Not verified against Gateway's own tool-call
      // timeout**, which the target contract does not state; if Gateway's is shorter this is the
      // wrong way round and should come down.
      timeoutSeconds: 60,
    });

    this.catalogFunction = this.pythonFunction('CatalogTool', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.tool_handlers.catalog_entrypoint.handler',
      environment,
      // 1024 MB, matching every other function in the platform. Uniform on purpose: CPU scales with
      // memory, so this is the init-latency dial, and these functions run hot in bursts while a user
      // waits. It parses the metric dictionary and every data contract at init like its sibling, and
      // the previous 512 MB gave that half the CPU for no benefit — a shorter duration at a higher
      // rate is frequently the cheaper of the two, not just the faster.
      memoryMb: 1024,
      // Short because the only outbound calls are one conditional DynamoDB PutItem and, on a
      // collision, one strongly consistent GetItem against the same key. A grounding-table
      // slowdown or outage is a real dependency failure and fails the answer closed; this budget
      // allows those bounded calls without making a broken capability request linger.
      timeoutSeconds: 15,
    });


    this.interceptorFunction = this.pythonFunction('GatewayInterceptor', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.gateway_interceptor.handler.handler',
      environment: tracingEnvironment(config),
      // 512 MB: it runs before every tool call a user waits on, so it is sized for a fast Python
      // cold start; the work itself is one bounded JSON transformation with no I/O.
      memoryMb: 512,
      // Short: no outbound call at all. A stuck interception fails the tool call quickly rather
      // than consuming the turn. The Gateway's own interceptor timeout is not documented
      // [unverified]; this stays well below every tool function's.
      timeoutSeconds: 5,
    });

    for (const fn of [
      this.measurementFunction,
      this.catalogFunction,
      this.interceptorFunction,
    ]) {
      grantSpanExport(fn);
    }

    // The verification key is read from KMS at the tool's first call, never from the public issuer
    // (ADR-077 §5), so each tool role may read the public half of exactly the signing key. The key
    // policy denies only kms:Sign to other principals, so this identity grant is sufficient.
    for (const fn of [this.measurementFunction, this.catalogFunction]) {
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: 'ReadCallerVerificationKey',
          actions: ['kms:GetPublicKey'],
          resources: [tokenSigningKeyArn],
        }),
      );
    }

    this.grantCuratedRead(config, this.measurementFunction);
    this.measurementFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [this.pageContextTable.tableArn],
      }),
    );

    // The one route guardrail; Runtime and inbound routes apply it through SSM (ADR-042).
    this.guardrail = routeGuardrail(this, {
      config,
      physicalName: agentRouteGuardrailName(config.envName),
    });
    this.alarmOnGuardrailInterventions(config);
    this.gateway = this.buildGateway(config, props.toolSchemaPath);

    // Layer 2, after the gateway and both its targets exist. A policy is evaluated against a
    // deployed gateway rather than against the construct, which is why AWS's own walkthrough
    // deploys the infrastructure before adding any policy. Does nothing when the feature is off,
    // which is the state a checkout ships in. ADR-041.
    this.policyEngine = attachGatewayPolicyEngine(this, { config, gateway: this.gateway });

    // `catalogFunction` may put immutable grounding evidence and read back only the same exact key
    // on collision. It has no Athena, Glue, S3, broad DynamoDB read, or business-data mutation
    // authority. The synth tests assert that exact asymmetry.

    applyStandardTags(this, config);
    this.publishParameters(config);
    this.publishOutputs();
    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
  }

  /**
   * Every `ADL_` value both functions read, as one object.
   *
   * One object rather than one per function, for the reason `ApiStack` states: they are the same
   * shape of consumer and two copies drift. Every key is set even where a function cannot use it,
   * because configuration resolves as a whole — and withholding a string is not an authorization
   * mechanism. Which table a code path may touch is settled by IAM and by which port it holds.
   *
   * `ADL_COGNITO_CLIENT_SECRET_ID` is deliberately **absent**. It is the one key these functions
   * must not carry: neither is the BFF, `settings_without_client_secret` is what they load through,
   * and naming a secret they have no grant to read would put an unusable ARN in their environment
   * and imply they have something to do with authentication.
   */
  private toolEnvironment(
    config: AppConfig,
    publicOrigin: string,
    authParam: (key: 'userPoolId' | 'userPoolClientId' | 'authDomain') => string,
    semanticV2Bucket: string,
    semanticV2CurrentDatabase: string,
  ): Record<string, string> {
    return {
      ADL_ENVIRONMENT: config.envName,
      ADL_AWS_REGION: config.region,
      ADL_COGNITO_USER_POOL_ID: authParam('userPoolId'),
      ADL_COGNITO_CLIENT_ID: authParam('userPoolClientId'),
      ADL_COGNITO_LOGIN_DOMAIN: authParam('authDomain'),
      ADL_APP_BASE_URL: publicOrigin,
      ADL_API_BASE_URL: publicOrigin,
      ADL_COOKIE_SCOPE: config.auth.cookieScope,
      ADL_COOKIE_DOMAIN: config.auth.cookieDomain,
      ADL_REFRESH_COOKIE_MAX_AGE_SECONDS: String(config.auth.refreshCookieMaxAgeSeconds),
      // Interactive only. Batch publication uses the explicit V2 setting on ingestion containers.
      ADL_ATHENA_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'interactive'),
      ADL_SEMANTIC_V2_CURRENT_DATABASE: semanticV2CurrentDatabase,
      ADL_PAGE_CONTEXT_TABLE: pageContextTableName(config.envName),
      ADL_SEMANTIC_V2_BUCKET: semanticV2Bucket,
      ADL_SEMANTIC_ATTEMPTS_TABLE: semanticV2StateTableName(config.envName, 'attempts'),
      ADL_SEMANTIC_LEASES_TABLE: semanticV2StateTableName(config.envName, 'leases'),
      ADL_SEMANTIC_PUBLICATIONS_TABLE: semanticV2StateTableName(config.envName, 'publications'),
      // Measured to hit: the same question ran in 2.37 s cold and 0.47 s reused, scanning 732
      // bytes then zero [verified 2026-08-22]. It is also a correctness window — Athena does not
      // check for changed source data until the age expires — which is why it is configuration
      // rather than a constant.
      ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES: String(config.athenaResultReuseMaxAgeMinutes),
      ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: String(config.athenaQueryBatchMaxConcurrency),
      POWERTOOLS_METRICS_NAMESPACE: 'Adlytics',
      // Tracing, from the same flag that enables Transaction Search. One switch rather than two,
      // because reaching the X-Ray OTLP endpoint *requires* Transaction Search — exporting without
      // it would produce spans that are billed and discarded, and two flags could disagree.
      ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
      // Recording rate, distinct from the indexing rate below it. Transaction Search exists to
      // ingest 100% of spans so a trace is never broken, and a broken trace is worse than a short
      // one during an investigation: the missing hop reads as a fast hop. Cost is controlled by
      // indexing less, not by recording less.
      ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
      POWERTOOLS_LOG_LEVEL: 'INFO',
    };
  }

  /**
   * Everything the measurement function needs to answer a question, and nothing more.
   *
   * One statement per service, each scoped to a resource built from a derived name at synth time.
   * That is why `dataBucketName`, `semanticV2AthenaWorkgroupName`,
   * `semanticV2DatabaseName` and `semanticV2StateTableName` exist as pure functions of the
   * environment name: a bucket name read from SSM is a deploy-time token, and a token in a policy
   * resource is what makes people reach for a wildcard instead.
   */
  private grantCuratedRead(config: AppConfig, fn: lambda.Function): void {
    const semantic = dataBucketName(config, 'semantic-v2');
    const results = dataBucketName(config, 'v2-results');
    const database = semanticV2DatabaseName(config.envName, 'current');

    // Read-only, and only the interactive workgroup. `StopQueryExecution` is included because the
    // adapter abandons a statement that outlives its budget, and a statement left running after
    // its caller has gone still scans and still bills.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'athena:StartQueryExecution',
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StopQueryExecution',
        ],
        resources: [
          `arn:${this.partition}:athena:${this.region}:${this.account}:workgroup/${semanticV2AthenaWorkgroupName(config.envName, 'interactive')}`,
        ],
      }),
    );

    // No `glue:UpdateTable` and no `glue:CreateTable`. A tool reads the catalogue to plan a
    // statement; the load path owns every write, and a read path holding a write grant is how a
    // schema changes without a deploy.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
        resources: [
          `arn:${this.partition}:glue:${this.region}:${this.account}:catalog`,
          `arn:${this.partition}:glue:${this.region}:${this.account}:database/${database}`,
          `arn:${this.partition}:glue:${this.region}:${this.account}:table/${database}/*`,
        ],
      }),
    );

    // S3 current and successful publication objects are read-only to a tool. Exact object reads need
    // no bucket enumeration; only the current serving prefix may be listed.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          `arn:${this.partition}:s3:::${semantic}/canonical/current/*`,
          `arn:${this.partition}:s3:::${semantic}/immutable/canonical/successful/*`,
        ],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${semantic}`],
        conditions: {
          StringLike: { 's3:prefix': ['canonical/current/*'] },
        },
      }),
    );

    // Write is required here and only here: Athena puts every result set in this bucket on the
    // caller's behalf, so a read-only grant fails the query rather than the download.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
        resources: [`arn:${this.partition}:s3:::${results}/interactive/*`],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${results}`],
        conditions: {
          StringLike: { 's3:prefix': ['interactive/*'] },
        },
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation'],
        resources: [
          `arn:${this.partition}:s3:::${semantic}`,
          `arn:${this.partition}:s3:::${results}`,
        ],
      }),
    );

    // Read the publication pointer and referenced records atomically. The transaction action is
    // scoped to the V2 publications table and carries no authority over attempts or leases.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${semanticV2StateTableName(config.envName, 'publications')}`,
        ],
      }),
    );
    // Governed window defaulting reads what IS published for a metric's sources, which is a
    // scope-current index Query — the same read the page presentation function holds. An
    // unnamed reporting window is answered for the latest published coverage rather than
    // refused, so this read sits on the request path.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${semanticV2StateTableName(config.envName, 'publications')}/index/scope-current`,
        ],
      }),
    );
  }

  /**
   * The MCP gateway and one target per domain, built from the generated tool definitions.
   *
   * **One target per domain rather than one per tool, and the tool name is what routes.** Gateway
   * sends `${targetName}___${toolName}` to the function, which strips the prefix and dispatches on
   * what is left — so a target carrying three tool definitions reaches three code paths in one
   * function. Target names are therefore `catalog` and `measurement`, and they are load-bearing:
   * they are the prefix the handler removes.
   *
   * **The authorizer is the token service's JWT (ADR-077 §4).** It admits only a token the token
   * service's `/token` exchange minted for the Gateway audience and the agent client, so SigV4, a
   * Cognito token, and a Harness-audience token are all refused. The issuer is read from SSM and
   * the discovery URL is built from it, exactly as the Harness authorizer builds its own. The
   * service cannot change an existing gateway's authorizer type, so the move from IAM replaced it.
   *
   * The L2 default was rejected for a specific reason: leaving `authorizerConfiguration` unset
   * makes it create its own Cognito user pool — verified by synthesising it — and that pool
   * renders with `DeletionPolicy: Retain`. A RETAIN resource in a DESTROY stack is what the
   * stack-composition rule forbids, and it would be a second user pool created by a default.
   *
   * **One REQUEST interceptor, with request headers passed (ADR-077 §5).** It forwards the
   * caller's `Authorization` to the target and strips reserved `_adl_` arguments. A Lambda target
   * receives that header in its client context (probe P3, verified 2026-09-26), and every tool
   * verifies it before it reads the call; analytical scope stays ADR-054's deployment-wide,
   * server-owned scope. The contract is owned by
   * `docs/capabilities/conversational-runtime/low-level-design.md` §6.
   */
  private buildGateway(config: AppConfig, toolSchemaPath: string): agentcore.Gateway {
    const tools = loadGeneratedTools(toolSchemaPath);
    const tokenIssuerUrl = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'tokenIssuerUrl'),
    );

    // Logical id and name changed with the move to CUSTOM_JWT: the service cannot change an existing
    // gateway's authorizer type, so the gateway is replaced (CloudFormation creates the new one
    // before deleting the old).
    const gateway = new agentcore.Gateway(this, 'JwtToolGateway', {
      gatewayName: toolGatewayName(config.envName),
      description: 'MCP surface over the measurement and catalog tools',
      authorizerConfiguration: agentcore.GatewayAuthorizer.usingCustomJwt({
        discoveryUrl: `${tokenIssuerUrl}/.well-known/openid-configuration`,
        allowedAudience: [gatewayTokenAudience(config.envName)],
        allowedClients: [agentClientId(config.envName)],
      }),
      // `exceptionLevel` is deliberately unset. The only value the L2 defines is `DEBUG`, which
      // returns granular target-configuration detail to the caller — and the caller here is a model
      // whose output reaches a user. Omitting it takes the documented default, which sanitises. A
      // refusal already carries its own reason through the envelope, which is the channel a user
      // should be reading.
    });

    // Named `catalog` and `measurement` because the target name becomes the prefix on every tool
    // name the function receives. Renaming one changes what arrives on the wire.
    agentcore.GatewayTarget.forLambda(this, 'CatalogTarget', {
      gateway,
      gatewayTargetName: 'catalog',
      description: 'What can be asked. Reads no business data; writes transient grounding.',
      lambdaFunction: this.catalogFunction,
      toolSchema: agentcore.ToolSchema.fromInline(tools.catalog),
    });

    agentcore.GatewayTarget.forLambda(this, 'MeasurementTarget', {
      gateway,
      gatewayTargetName: 'measurement',
      description: 'Figures from the curated lake, with everything that qualifies them.',
      lambdaFunction: this.measurementFunction,
      toolSchema: agentcore.ToolSchema.fromInline(tools.measurement),
    });


    // Headers are passed because the bearer is the one thing the interceptor exists to read; the
    // handler never logs a header or an argument value. The bind grants the gateway role
    // `lambda:InvokeFunction` on this function.
    gateway.addInterceptor(
      agentcore.LambdaInterceptor.forRequest(this.interceptorFunction, { passRequestHeaders: true }),
    );

    return gateway;
  }

  /**
   * A Python function with everything that must not vary taken from config.
   *
   * Architecture and runtime are never written inline: one stack diverging from the rest is how an
   * ADOT layer arrives in the wrong architecture and fails at function init rather than at build.
   */
  private pythonFunction(
    id: string,
    options: {
      config: AppConfig;
      dependencies: lambda.ILayerVersion;
      sourceAssetPath: string;
      handler: string;
      environment: Record<string, string>;
      memoryMb: number;
      timeoutSeconds: number;
    },
  ): lambda.Function {
    const functionName = `adlytics-${options.config.envName}-${id}`;

    // Declared rather than left to the `logRetention` property, which provisions a Node singleton
    // to call PutRetentionPolicy at deploy time — an x86_64 function in a stack where every
    // function is meant to be arm64, and caught by the architecture assertion in the tests.
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: options.config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // This helper declares the group, so it grants on it (ADR-085).
    const role = lambdaRole(this, `${id}ServiceRole`);
    grantOwnLogWrites(role, logGroup);
    return new lambda.Function(this, id, {
      functionName,
      role,
      runtime: new lambda.Runtime(options.config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: lambda.Architecture.ARM_64,
      code: lambda.Code.fromAsset(options.sourceAssetPath),
      handler: options.handler,
      layers: [options.dependencies],
      environment: options.environment,
      memorySize: options.memoryMb,
      timeout: cdk.Duration.seconds(options.timeoutSeconds),
      logGroup,
    });
  }

  /**
   * Human-facing values for `aws cloudformation describe-stacks`.
   *
   * Without `exportName`, so none becomes a CloudFormation export — an export cannot be changed or
   * deleted while another stack imports it, which is the deadlock ADR-012 replaced with SSM.
   */
  /**
   * The gateway's identity, for whatever connects to it.
   *
   * `gatewayId` has been a declared parameter with no producer since ADR-012 was written; this is
   * the stack that owes it. Both go through SSM rather than a CloudFormation export, because an
   * export cannot be changed while another stack imports it.
   */
  private publishParameters(config: AppConfig): void {
    // `gatewayUrl` is optional on the base type, because an *imported* gateway may not carry one.
    // A gateway created here always does. Refused rather than coerced: publishing an empty
    // parameter would make a consumer fail at connect time with nothing to point at, where this
    // fails at synth naming the cause.
    const { gatewayUrl } = this.gateway;
    if (gatewayUrl === undefined) {
      throw new Error(
        'The gateway synthesised without a URL, which a created gateway always has. ' +
          'The construct has changed shape; do not publish an empty parameter.',
      );
    }

    new ssm.StringParameter(this, 'ParamPageContextTable', {
      parameterName: paramPath(config.envName, 'pageContextTableName'),
      stringValue: this.pageContextTable.tableName,
      description: 'Short-lived semantic context keyed for presentation and measurement lookups.',
    });
    new ssm.StringParameter(this, 'ParamMeasurementFunctionArn', {
      parameterName: paramPath(config.envName, 'measurementFunctionArn'),
      stringValue: this.measurementFunction.functionArn,
      description: 'Exact measurement operation Lambda invoked by the repository Runtime.',
    });
    new ssm.StringParameter(this, 'ParamGuardrailId', {
      parameterName: paramPath(config.envName, 'guardrailId'),
      stringValue: this.guardrail.attrGuardrailId,
      description: 'Canonical conversation Guardrail identifier.',
    });
    new ssm.StringParameter(this, 'ParamGuardrailArn', {
      parameterName: paramPath(config.envName, 'guardrailArn'),
      stringValue: this.guardrail.attrGuardrailArn,
      description: 'Canonical conversation Guardrail ARN for exact IAM scope.',
    });
    new ssm.StringParameter(this, 'ParamGuardrailVersion', {
      parameterName: paramPath(config.envName, 'guardrailVersion'),
      stringValue: GUARDRAIL_VERSION,
      description: 'ApplyGuardrail-compatible version deployed with the canonical Guardrail.',
    });
    new ssm.StringParameter(this, 'ParamGatewayId', {
      parameterName: paramPath(config.envName, 'gatewayId'),
      stringValue: this.gateway.gatewayId,
      description: 'AgentCore Gateway carrying the measurement and catalog tools.',
    });
    new ssm.StringParameter(this, 'ParamGatewayUrl', {
      parameterName: paramPath(config.envName, 'gatewayUrl'),
      stringValue: gatewayUrl,
      description: 'MCP endpoint. Requires a token-service JWT for the Gateway audience (ADR-077).',
    });
  }

  /**
   * One alarm on the share of questions the route Guardrail declines.
   *
   * **A rate, not a count.** Calibration on 2026-09-30 left the deployed set blocking none of its
   * benign questions, so a rising share is either a burst of attacks or the filters drifting onto
   * ordinary questions; a count would grow with traffic and say neither. Which checks fired is on
   * each refusal's `guardrail_signals` log field, which is where the alarm sends the reader.
   *
   * **Scoped to this guardrail and version**, through the `GuardrailArn` and `GuardrailVersion`
   * dimensions, so a temporary calibration guardrail in the same account never counts. The route
   * applies this guardrail only to questions, so every invocation here is an inbound assessment.
   *
   * **A floor on volume.** Below `minimumInvocations` an hour the share is noise (one refusal of
   * three questions is 33%), so the expression reports 0 there. Threshold and floor are starting
   * values chosen for this alarm, not measured; `make accept-guardrail` replays 54 questions, 16 of
   * them attacks, so an acceptance run raises it. It then stays in ALARM for up to the hour and
   * cannot notify again, so a surge in that window is visible only in the refusal lines.
   */
  private alarmOnGuardrailInterventions(config: AppConfig): void {
    const minimumInvocations = 20;
    const dimensionsMap = {
      GuardrailArn: this.guardrail.attrGuardrailArn,
      GuardrailVersion: GUARDRAIL_VERSION,
    };
    const metric = (metricName: string): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: 'AWS/Bedrock/Guardrails',
        metricName,
        dimensionsMap,
        statistic: cloudwatch.Stats.SUM,
        period: cdk.Duration.hours(1),
      });
    const alarm = new cloudwatch.Alarm(this, 'GuardrailInterventionRateAlarm', {
      alarmName: `adlytics-${config.envName}-guardrail-intervention-rate`,
      alarmDescription:
        'More than a fifth of conversation questions in an hour were declined by the route ' +
        'Guardrail: an attack burst or filters drifting onto ordinary questions. The ' +
        'guardrail_signals field on each input_guardrail refusal names the checks that fired ' +
        '(ADR-041).',
      metric: new cloudwatch.MathExpression({
        expression: `IF(invocations >= ${minimumInvocations}, intervened / invocations, 0)`,
        usingMetrics: {
          intervened: metric('InvocationsIntervened'),
          invocations: metric('Invocations'),
        },
        label: 'Share of questions declined',
        period: cdk.Duration.hours(1),
      }),
      threshold: 0.2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(
      new actions.SnsAction(
        sns.Topic.fromTopicArn(
          this,
          'AlarmTopic',
          ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'ingestionAlarmTopicArn'),
          ),
        ),
      ),
    );
  }

  private publishOutputs(): void {
    new cdk.CfnOutput(this, 'OutMeasurementFunctionName', {
      value: this.measurementFunction.functionName,
      description: 'Gateway target for the measurement tools. Reads the curated lake.',
    });
    new cdk.CfnOutput(this, 'OutCatalogFunctionName', {
      value: this.catalogFunction.functionName,
      description: 'Gateway target for catalog tools. No business-data reads; grounding write only.',
    });
    new cdk.CfnOutput(this, 'OutGatewayUrl', {
      value: this.gateway.gatewayUrl ?? '(unavailable)',
      description: 'MCP endpoint an agent connects to. Custom JWT auth: a token-service Gateway token.',
    });
    // Stated either way. An operator asking "is layer 2 running" should get an answer rather than
    // inferring one from a missing output, and "not enabled" is a different fact from "enabled and
    // broken". ADR-041.
    new cdk.CfnOutput(this, 'OutGatewayPolicyEngine', {
      value: this.policyEngine?.attrPolicyEngineId ?? '(not enabled)',
      description: 'Cedar policy engine evaluating the tool gateway, or that layer 2 is off.',
    });
  }
}
