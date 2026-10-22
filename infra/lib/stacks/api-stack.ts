// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { modelInvokeStatements } from '../bedrock-model-access';
import {
  API_ROUTES,
  MODE_KEY,
  OPERATOR_ONLY_DENIED_ACTIONS,
  OPERATOR_ONLY_PREFIXES,
  WRITER_ACTIONS,
} from '../generated/execution-graph-protocol';
import { grantSpanExport, tracingEnvironment } from '../telemetry';
import {
  agentQualityAuditTableName,
  API_PREFIX,
  AppConfig,
  clientSecretName,
  conversationFunctionRoleName,
  conversationFunctionTimeoutSeconds,
  lambdaWebAdapterLayerArn,
  ORIGIN_VERIFY_HEADER,
  originVerifySecretName,
  pageContextTableName,
  paramPath,
  qualityReviewFunctionRoleName,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
} from '../config';
import { guardrailProfileArns } from '../constructs/agent/route-guardrail';
import { executionGraphAuthority } from './execution-graph-authority';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { dataBucketName } from '../mixins/storage';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import {
  PAGE_ROUTES,
  PageRoute,
  buildPageRouteAlarm,
  pageRouteKey,
} from '../constructs/observability/page-route-alarms';
import { buildServingRefusalAlarm } from '../constructs/observability/serving-refusal-alarm';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyStandardTags } from '../mixins/tagging';

/**
 * HTTP API, the auth Lambda, and the REQUEST authorizer in front of it.
 *
 * DESTROY. Everything here can be rebuilt from source, which is exactly why the
 * user pool lives in AuthStack instead — see the stack composition steering.
 *
 * Auth values arrive through SSM, so this stack can be destroyed and recreated
 * without a CloudFormation dependency on AuthStack and without touching a user.
 */
/** A table another stack owns, by its published name and ARN. */
interface TableRef {
  readonly name: string;
  readonly arn: string;
}

/**
 * The stage access-log record (ADR-081 §4). `reviewer` and `attribution` are set by the authorizer on
 * the review routes only, so every other record leaves them empty and never names an end user. It
 * never references the context's general `subject`, a selector, a correlation identifier, content,
 * or a header. HTTP APIs document no authorizer-latency variable [verified 2026-09-28,
 * https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-logging-variables.html], so
 * the response and integration latencies stand in for where the time went.
 */

const REVIEWER_ACCESS_LOG_FORMAT = {
  requestId: '$context.requestId',
  routeKey: '$context.routeKey',
  status: '$context.status',
  reviewer: '$context.authorizer.reviewer',
  attribution: '$context.authorizer.attribution',
  authorizerError: '$context.authorizer.error',
  responseLatency: '$context.responseLatency',
  integrationLatency: '$context.integrationLatency',
} as const;

/**
 * S2 (BF-5): the operational status reader's reserved concurrency. The execution explanation runs
 * in this function, so paging can never take more than this many executions from the account
 * pool that the lifecycle functions (run-request admission, ingestion) draw from, and every
 * explanation read of the graph table is bounded by it too.
 */
export const OPERATIONAL_STATUS_RESERVED_CONCURRENCY = 5;

export interface ApiStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Directory holding the Python source tree. */
  readonly sourceAssetPath: string;
}

export class ApiStack extends cdk.Stack {
  private routeAuthorizer?: apigwv2.IHttpRouteAuthorizer;
  public readonly api: apigwv2.HttpApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // Every stack, without exception — see mixins/tagging. Applied through an Aspect, so
    // position in the constructor does not matter and every later resource is covered.
    applyStandardTags(this, props.config);
    const { config } = props;

    const authParam = (
      key: 'userPoolId' | 'userPoolClientId' | 'userPoolPublicClientId' | 'authDomain',
    ): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));

    // Published by FrontendStack, read at deploy rather than stated in config.
    // The distribution domain does not exist until CloudFront creates it, so the
    // alternative is a human copying it between passes — and a copied value goes
    // stale without ever failing. Resolution happens under the deployer's
    // credentials, so no function needs ssm:GetParameter for this.
    const publicOrigin = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publicBaseUrl'),
    );

    const agentQualityAuditTable = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'agentQualityAuditTableName'),
    );
    const operationalStatusProjection = config.operationalStatus.provisioned
      ? {
          name: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'operationalStatusProjectionTableName'),
          ),
          arn: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'operationalStatusProjectionTableArn'),
          ),
        }
      : undefined;
    const pageContextTable = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'pageContextTableName'),
    );
    const semanticV2Bucket = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'semanticV2BucketName'),
    );
    const semanticV2CurrentDatabase = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'semanticV2CurrentDatabase'),
    );
    const insightPromptRegistryTable = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightPromptRegistryTableName'),
    );
    const insightPromptRegistryArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightPromptRegistryTableArn'),
    );

    const insightCacheArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightCacheTableArn'),
    );
    const insightCacheName = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightCacheTableName'),
    );
    const insightPromptCalibrationQueueUrl = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightPromptCalibrationQueueUrl'),
    );
    const insightPromptCalibrationQueueArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightPromptCalibrationQueueArn'),
    );
    const insightContinuationQueueUrl = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightContinuationQueueUrl'),
    );
    const insightContinuationQueueArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'insightContinuationQueueArn'),
    );
    const presetArtifactBucket = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publisherArtifactBucketName'),
    );
    const presetArtifactPointerTable = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publisherArtifactPointerTableName'),
    );
    const presetArtifactPointerTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publisherArtifactPointerTableArn'),
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

    // One nonsecret environment for every API function. Auth adds the client-secret identifier
    // below because it alone exchanges codes, refreshes sessions, and revokes tokens.
    const baseEnvironment: Record<string, string> = {
      ADL_ENVIRONMENT: config.envName,
      ADL_AWS_REGION: config.region,
      ADL_COGNITO_USER_POOL_ID: authParam('userPoolId'),
      ADL_COGNITO_CLIENT_ID: authParam('userPoolClientId'),
      ADL_COGNITO_LOGIN_DOMAIN: authParam('authDomain'),
      // Identical by design: one CloudFront origin serves the app and the API, so
      // the cookie the API sets is the cookie the app sends back.
      ADL_APP_BASE_URL: publicOrigin,
      ADL_API_BASE_URL: publicOrigin,
      ADL_COOKIE_SCOPE: config.auth.cookieScope,
      ADL_COOKIE_DOMAIN: config.auth.cookieDomain,
      ADL_REFRESH_COOKIE_MAX_AGE_SECONDS: String(config.auth.refreshCookieMaxAgeSeconds),
      // The one query setting the shared runtime requires. Ingestion uses its explicit V2 batch
      // setting below; API handlers do not query but resolve the same Settings model.
      ADL_ATHENA_WORKGROUP: semanticV2AthenaWorkgroupName(config.envName, 'interactive'),
      ADL_SEMANTIC_V2_BUCKET: semanticV2Bucket,
      ADL_SEMANTIC_ATTEMPTS_TABLE: semanticV2StateTableName(config.envName, 'attempts'),
      ADL_SEMANTIC_LEASES_TABLE: semanticV2StateTableName(config.envName, 'leases'),
      ADL_SEMANTIC_PUBLICATIONS_TABLE: semanticV2StateTableName(config.envName, 'publications'),
      // 60 minutes, well under Athena's seven-day ceiling. A reused answer is presented
      // as current, so this is the window in which a stale figure could be called live.
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
    const presentationGraph = executionGraphAuthority(config, this);
    const presentationEnvironment: Record<string, string> = {
      ...baseEnvironment,
      ADL_PAGE_CONTEXT_TABLE: pageContextTable,
      ADL_INSIGHT_CACHE_TABLE: insightCacheName,
      ADL_INSIGHT_CONTINUATION_QUEUE_URL: insightContinuationQueueUrl,
      ADL_INSIGHT_PROMPT_REGISTRY_TABLE: insightPromptRegistryTable,
      ADL_INSIGHT_QUICK_MODEL_ID: config.insight.quickModelId,
      ADL_INSIGHT_DEEP_MODEL_ID: config.insight.deepModelId,
      // The application joins every admitted slot. Connect/read limits are separate transport
      // failure boundaries, not a reserved product budget; the 29-second Lambda/API envelope may
      // end first, especially after live-detail measurement. Observed direct calls are ~1–4 seconds.
      ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS: '20',
      ADL_INSIGHT_GENERATION_MAX_CONCURRENCY: String(config.athenaQueryBatchMaxConcurrency),
      ADL_SEMANTIC_V2_CURRENT_DATABASE: semanticV2CurrentDatabase,
      // Retained preset artifacts: pages served from the retained store resolve the pointer
      // table and read immutable bodies/custody manifests rather than querying at request time.
      ADL_PRESET_ARTIFACT_BUCKET: presetArtifactBucket,
      ADL_PRESET_ARTIFACT_POINTER_TABLE: presetArtifactPointerTable,
      // S6 §2.9: preset pages read the graph mode item and `SERVING#<grant>` where the graph
      // authority is provisioned. The read grants are §2.11 row 8 (slice 6).
      ...(presentationGraph === undefined
        ? {}
        : { ADL_EXECUTION_GRAPH_TABLE: presentationGraph.tableName }),
    };
    // ADR-078: the authorizer and the auth handler each make the origin-verify check first.
    const originVerifyEnvironment: Record<string, string> = {
      ADL_ORIGIN_VERIFY_SECRET_ID: originVerifySecretName(config.envName),
      ADL_ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS: String(config.edge.originVerifyMaxCacheAgeSeconds),
      ADL_ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS: String(
        config.edge.originVerifyRereadIntervalSeconds,
      ),
    };
    const authEnvironment: Record<string, string> = {
      ...baseEnvironment,
      ...originVerifyEnvironment,
      ADL_COGNITO_CLIENT_SECRET_ID: clientSecretName(config.envName),
      // ADR-115 decision 8: `/me` grants Data pipeline status only where its API exists, from the
      // same value that creates the operational status routes below.
      ADL_OPERATIONAL_STATUS_PROVISIONED: String(config.operationalStatus.provisioned),
    };

    const authFunction = this.pythonFunction('AuthFunction', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.auth_handlers.handlers.handler',
      environment: authEnvironment,
      // 1024 MB, uniform across the platform. The token exchange is one HTTPS round trip, but RSA
      // verification and pydantic model construction are CPU-bound, and CPU scales with memory —
      // this is the latency dial for a login, which a user is waiting on.
      memoryMb: 1024,
      // Below API Gateway's 29s integration timeout, and comfortably above the
      // adapter's own 5s per-request budget plus two retries.
      timeoutSeconds: 20,
    });

    const authorizerFunction = this.pythonFunction('AuthorizerFunction', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.auth_handlers.authorizer.handler',
      environment: {
        ...baseEnvironment,
        ...originVerifyEnvironment,
        // ADR-078: the only client whose access token is admitted as a bearer, on the two
        // review routes only. The cookie stays bound to ADL_COGNITO_CLIENT_ID.
        ADL_COGNITO_PUBLIC_CLIENT_ID: authParam('userPoolPublicClientId'),
      },
      // 1024 MB, and this is the function the change matters most for. It runs on *every*
      // authorised request, so its cold start is on the critical path of everything — and at 256 MB
      // it had the least CPU in the platform while doing CPU-bound work: RSA signature verification
      // against a cached JWKS. Four times the memory for a fraction of the duration is usually
      // cost-neutral or better, because Lambda bills memory × time.
      memoryMb: 1024,
      // Deliberately short. This runs on every authorised request, so a slow
      // authorizer is slow for everything.
      timeoutSeconds: 5,
    });

    const presentationFunction = this.pythonFunction('PresentationFunction', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      handler: 'adapters.inbound.presentation_handlers.handler.handler',
      environment: presentationEnvironment,
      // 1024 MB because presentation joins page context with immutable canonical objects on the
      // request path; CPU scales with memory, and the user waits for the complete rendered payload.
      memoryMb: 1024,
      // At API Gateway's 29s integration ceiling. Wide-window detail combines concurrent
      // measurement with joined parallel insight calls; provider and platform ceilings remain
      // independent failure boundaries, so this is not a completion guarantee.
      timeoutSeconds: 29,
    });
    if (presentationGraph !== undefined) {
      // §2.11 row 8: the strong reads of the mode item and `SERVING#<grant>/BINDING`, and
      // nothing else in the graph table. No `REVISION#` grant: case 4 is dropped (D7 Q1).
      presentationFunction.addToRolePolicy(new iam.PolicyStatement({
        sid: 'ReadGraphServingBindings',
        actions: ['dynamodb:GetItem'],
        resources: [presentationGraph.tableArn],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SERVING#*', MODE_KEY.pk] },
        },
      }));
    }

    // S2: the execution explanation reads graph authority as well as the projection.
    const explanationAuthority = operationalStatusProjection === undefined
      ? undefined
      : {
          graphTable: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'executionGraphTableName'),
          ),
          graphTableArn: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'executionGraphTableArn'),
          ),
          rawBucket: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'semanticV2RawBucketName'),
          ),
        };
    const operationalStatusFunction = operationalStatusProjection === undefined
      || explanationAuthority === undefined
      ? undefined
      : this.pythonFunction('OperationalStatusFunction', {
          config,
          dependencies,
          sourceAssetPath: props.sourceAssetPath,
          handler: 'adapters.inbound.operational_status.handler.handler',
          environment: {
            ...tracingEnvironment(config),
            ADL_OPERATIONAL_STATUS_TABLE: operationalStatusProjection.name,
            ADL_EXECUTION_GRAPH_TABLE: explanationAuthority.graphTable,
            ADL_RAW_BUCKET: explanationAuthority.rawBucket,
          },
          // Bounded keyed projection reads are short; 512 MiB protects Python cold-start latency.
          memoryMb: 512,
          timeoutSeconds: 10,
          reservedConcurrency: OPERATIONAL_STATUS_RESERVED_CONCURRENCY,
        });
    if (
      operationalStatusFunction !== undefined
      && operationalStatusProjection !== undefined
      && explanationAuthority !== undefined
    ) {
      operationalStatusFunction.addToRolePolicy(new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'],
        resources: [
          operationalStatusProjection.arn,
          `${operationalStatusProjection.arn}/index/*`,
        ],
      }));
      this.grantExplanationReads(operationalStatusFunction, explanationAuthority);
    }

    // S1 control-plane authority: the run-request routes (owner-gate item 5). The function is the
    // admission writer: exactly the generated item actions on the live table (a transaction needs
    // each member's own action) and read-only exact-version access to the raw zone. No probe table
    // appears in this stack (V-15).
    const runRequestsFunction = operationalStatusProjection === undefined
      ? undefined
      : this.runRequestsFunction(config, dependencies, props.sourceAssetPath);
    // ADR-080 §1: each record family is read from the one table its single writer owns. The
    // findings table always exists (AgentQualityStack precedes this stack); the evaluation tables
    // exist only where the evaluation plane is provisioned (EvaluationEvidenceStack precedes it).
    const read = (key: Parameters<typeof paramPath>[1]): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const findingsTable = {
      name: read('agentQualityFindingsTableName'),
      arn: read('agentQualityFindingsTableArn'),
    };
    const evaluationTables = config.evaluationInput.provisioned
      ? {
          deliveries: {
            name: read('evaluationDeliveriesTableName'),
            arn: read('evaluationDeliveriesTableArn'),
          },
          writerReceipts: {
            name: read('evaluationWriterReceiptsTableName'),
            arn: read('evaluationWriterReceiptsTableArn'),
          },
          verdicts: {
            name: read('evaluationVerdictsTableName'),
            arn: read('evaluationVerdictsTableArn'),
          },
        }
      : undefined;
    const qualityReviewFunction = this.qualityReviewFunction(
      config,
      dependencies,
      props.sourceAssetPath,
      baseEnvironment,
      agentQualityAuditTable,
      findingsTable,
      evaluationTables,
    );

    const promptCalibrationEnqueueEnvironment: Record<string, string> = {
      ...baseEnvironment,
      ADL_INSIGHT_PROMPT_REGISTRY_TABLE: insightPromptRegistryTable,
      ADL_INSIGHT_PROMPT_CALIBRATION_QUEUE_URL: insightPromptCalibrationQueueUrl,
    };
    const promptRegistryReadEnvironment: Record<string, string> = {
      ...baseEnvironment,
      ADL_INSIGHT_PROMPT_REGISTRY_TABLE: insightPromptRegistryTable,
    };
    const promptAdminFunctions = {
      // Create durably admits the immutable version identity; recovery conditionally
      // re-admits only pending work. Model calls live in the queued calibration worker.
      create: this.pythonFunction('PromptAdminCreateFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptCalibrationEnqueueEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'create-version' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
      calibrate: this.pythonFunction('PromptAdminCalibrateFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptCalibrationEnqueueEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'calibrate' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
      activate: this.pythonFunction('PromptAdminActivateFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptRegistryReadEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'activate' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
      retire: this.pythonFunction('PromptAdminRetireFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptRegistryReadEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'retire' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
      slots: this.pythonFunction('PromptAdminSlotsFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptRegistryReadEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'slots' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
      versions: this.pythonFunction('PromptAdminVersionsFunction', {
        config, dependencies, sourceAssetPath: props.sourceAssetPath,
        handler: 'adapters.inbound.prompt_admin.handler.handler',
        environment: { ...promptRegistryReadEnvironment, ADL_PROMPT_ADMIN_ROUTE: 'versions' },
        memoryMb: 512, timeoutSeconds: 10,
      }),
    };
    // Transactional writes authorize as their underlying item actions (ConditionCheckItem,
    // PutItem, UpdateItem, DeleteItem), never as a TransactWriteItems action.
    promptAdminFunctions.create.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:ConditionCheckItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'], resources: [insightPromptRegistryArn],
    }));
    promptAdminFunctions.calibrate.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:DeleteItem'], resources: [insightPromptRegistryArn],
    }));
    for (const fn of [promptAdminFunctions.create, promptAdminFunctions.calibrate]) {
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['sqs:SendMessage'], resources: [insightPromptCalibrationQueueArn],
      }));
    }
    promptAdminFunctions.retire.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:DeleteItem', 'dynamodb:PutItem'], resources: [insightPromptRegistryArn],
    }));
    promptAdminFunctions.activate.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:ConditionCheckItem', 'dynamodb:PutItem'], resources: [insightPromptRegistryArn],
    }));
    promptAdminFunctions.slots.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:BatchGetItem'], resources: [insightPromptRegistryArn],
    }));
    promptAdminFunctions.versions.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:BatchGetItem', 'dynamodb:Query'], resources: [insightPromptRegistryArn],
    }));
    // Only the versions route resolves a stored author subject to its email (B1); it holds
    // subjects read from the registry, so it needs one point read and never a list.
    promptAdminFunctions.versions.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminGetUser'],
      resources: [cdk.Stack.of(this).formatArn({
        service: 'cognito-idp', resource: 'userpool', resourceName: authParam('userPoolId'),
      })],
    }));

    const publicationsTable = semanticV2StateTableName(config.envName, 'publications');
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem'],
        resources: [insightCacheArn],
      }),
    );
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [insightPromptRegistryArn],
      }),
    );
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['sqs:SendMessage'],
        resources: [insightContinuationQueueArn],
      }),
    );
    for (const statement of modelInvokeStatements(
      this,
      'InvokeInsightModels',
      [config.insight.quickModelId, config.insight.deepModelId],
      ['bedrock:InvokeModel'],
    )) presentationFunction.addToRolePolicy(statement);
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${pageContextTableName(config.envName)}`,
        ],
      }),
    );
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${publicationsTable}`,
        ],
      }),
    );
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${publicationsTable}/index/scope-current`,
        ],
      }),
    );
    // Read-only retained-artifact access: pointer resolution plus immutable body and custody
    // manifest reads. The baker alone writes; presentation never holds a write on this store.
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:TransactGetItems'],
        resources: [presetArtifactPointerTableArn],
      }),
    );
    presentationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [
          `arn:${this.partition}:s3:::${presetArtifactBucket}/pages/*`,
          `arn:${this.partition}:s3:::${presetArtifactBucket}/custody/*`,
        ],
      }),
    );
    this.grantPresentationCuratedRead(config, presentationFunction);

    // Only the auth function exchanges authorization codes, refreshes sessions, and revokes
    // tokens, so it alone can resolve the confidential BFF client secret.
    const secretArn = cdk.Stack.of(this).formatArn({
      service: 'secretsmanager',
      resource: 'secret',
      resourceName: `${clientSecretName(config.envName)}-*`,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    authFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [secretArn],
      }),
    );
    // ADR-078: FrontendStack owns the origin-verify secret; these two functions alone read it.
    const originVerifySecretArn = cdk.Stack.of(this).formatArn({
      service: 'secretsmanager',
      resource: 'secret',
      resourceName: `${originVerifySecretName(config.envName)}-*`,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    for (const fn of [authFunction, authorizerFunction]) {
      fn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['secretsmanager:GetSecretValue'],
          resources: [originVerifySecretArn],
        }),
      );
    }
    this.alarmOnOriginVerifyRefusals(config, [authFunction, authorizerFunction]);

    this.api = new apigwv2.HttpApi(this, 'HttpApi', {
      apiName: `adlytics-${config.envName}`,
      // No CORS. CloudFront serves the frontend and this API from one origin, so
      // every request is same-origin and no preflight is ever issued. Configuring
      // CORS anyway would be dead settings that imply a cross-origin design.
    });
    // ADR-081 §4, observability §3.8: the stage access log, into AgentQualityStack's retained group.
    // The escape hatch, because the L2 creates `$default` implicitly and replacing it with an
    // explicit stage would recreate the stage every route is served from.
    const defaultStage = this.api.defaultStage?.node.defaultChild;
    if (!(defaultStage instanceof apigwv2.CfnStage)) {
      throw new Error('The HTTP API has no $default stage to write the reviewer access log.');
    }
    defaultStage.accessLogSettings = {
      destinationArn: read('agentQualityReviewerAccessLogGroupArn'),
      format: JSON.stringify(REVIEWER_ACCESS_LOG_FORMAT),
    };

    const guardrailId = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'guardrailId'),
    );
    const guardrailArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'guardrailArn'),
    );
    const guardrailVersion = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'guardrailVersion'),
    );
    const conversationUrl = this.conversationRoute(
      props,
      baseEnvironment,
      dependencies,
      guardrailId,
      guardrailArn,
      guardrailVersion,
    );

    // Every function in this stack exports spans, so every one needs the grant. Listed rather than
    // folded into `pythonFunction`, so a reader can see which roles carry it.
    for (const fn of [
      authFunction,
      authorizerFunction,
      presentationFunction,
      qualityReviewFunction,
      promptAdminFunctions.create,
      promptAdminFunctions.calibrate,
      promptAdminFunctions.activate,
      promptAdminFunctions.retire,
      promptAdminFunctions.slots,
      promptAdminFunctions.versions,
      ...(operationalStatusFunction === undefined ? [] : [operationalStatusFunction]),
      ...(runRequestsFunction === undefined ? [] : [runRequestsFunction]),
    ]) {
      grantSpanExport(fn);
    }

    const pageRoutes = this.addRoutes(
      authFunction,
      authorizerFunction,
      presentationFunction,
      operationalStatusFunction,
      qualityReviewFunction,
      promptAdminFunctions,
      config,
    );
    // ADR-148: detailed route metrics on exactly the page routes, which the page alarms read. The
    // stage depends on each page route, so an update never names a route key that does not exist.
    defaultStage.routeSettings = Object.fromEntries(
      pageRoutes.map(([page]) => [pageRouteKey(page.path), { DetailedMetricsEnabled: true }]),
    );
    for (const [, route] of pageRoutes) {
      defaultStage.addResourceDependency(route.node.defaultChild as apigwv2.CfnRoute);
    }
    if (runRequestsFunction !== undefined) {
      this.addRunRequestRoutes(runRequestsFunction, config);
    }
    this.publishParameters(config, publicOrigin, conversationUrl);
    this.publishOutputs(authFunction, authorizerFunction);
    const staffed = staffedAlarmAction(this, config);
    applyFunctionErrorAlarms(this, config, staffed);
    // ADR-139 layer 4: a grant-serving refusal past the settle bound is a write-side defect.
    buildServingRefusalAlarm(this, config, staffed);
    // ADR-148: each page's alarm holds until an observed success clears it.
    for (const route of PAGE_ROUTES) buildPageRouteAlarm(this, config, this.api, route, staffed);
  }

  /**
   * The conversation function, its Function URL, and CloudFront's permission to call it.
   *
   * ADR-038: this route does not go through the HTTP API. A deep dive streams for tens of seconds
   * and HTTP API's integration timeout is a fixed 29s that cannot be raised, so the transport is a
   * Function URL with `InvokeMode: RESPONSE_STREAM` reached only through CloudFront.
   *
   * Returns the URL so `publishParameters` can hand it to `FrontendStack`.
   */
  private conversationRoute(
    props: ApiStackProps,
    baseEnvironment: Record<string, string>,
    dependencies: lambda.ILayerVersion,
    guardrailId: string,
    guardrailArn: string,
    guardrailVersion: string,
  ): lambda.FunctionUrl {
    const { config } = props;
    const harnessArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'harnessArn'),
    );
    const harnessPromptDigest = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'harnessPromptDigest'),
    );
    const auditTable = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'agentQualityAuditTableName'),
    );
    const auditTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'agentQualityAuditTableArn'),
    );
    // ADR-079: the route admits every released turn whenever the evaluation plane exists; the
    // dispatcher's release records, not the route, decide what reaches evaluation.
    const evaluationOutbox = config.evaluationInput.provisioned
      ? {
          name: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'evaluationOutboxTableName'),
          ),
          arn: ssm.StringParameter.valueForStringParameter(
            this,
            paramPath(config.envName, 'evaluationOutboxTableArn'),
          ),
        }
      : undefined;

    // ADR-077 §3: the route exchange function's resource policy denies every principal whose
    // `aws:PrincipalArn` is not this role, and TokenServiceStack deploys first, so the name is fixed.
    const conversationRole = lambdaRole(this, 'ConversationFunctionRole', {
      roleName: conversationFunctionRoleName(config.envName),
    });
    const tokenRouteExchangeFunctionArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'tokenRouteExchangeFunctionArn'),
    );

    const conversationFunction = this.pythonFunction('ConversationFunction', {
      config,
      role: conversationRole,
      // The same layer the auth functions use, not a second one. `make build-lambdas` produces a
      // single dependency closure for the whole repository, so a second `LayerVersion` over the
      // same asset would upload an identical 30 MB and count twice against the unzipped quota that
      // covers a function and all its layers combined.
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      // A startup script, not a Python symbol. Under the Lambda Web Adapter the handler is what
      // the exec wrapper runs to bring the server up; `scripts/build-lambdas.sh` places it at the
      // root of the asset because that is where Lambda resolves a script handler from.
      handler: 'run.sh',
      environment: {
        ...baseEnvironment,
        ADL_HARNESS_ARN: harnessArn,
        ADL_HARNESS_PROMPT_DIGEST: harnessPromptDigest,
        ADL_AGENT_QUALITY_AUDIT_TABLE: auditTable,
        // The route writes every receipt and its companions, each expiring this many days after
        // the receipt's creation (ADR-081 §5).
        ADL_RECEIPT_RETENTION_DAYS: String(config.receiptRetentionDays),
        ...(evaluationOutbox === undefined
          ? {}
          : { ADL_EVALUATION_OUTBOX_TABLE: evaluationOutbox.name }),
        ADL_QUICK_CHAT_MODEL_ID: config.agent.quickChat.modelId,
        ADL_DEEP_DIVE_MODEL_ID: config.agent.deepDive.modelId,
        // The managed Harness owns the immutable post-plan budgets. Only model token/time profiles
        // are provided here; browser and edge cannot select an iteration ceiling.
        ADL_QUICK_CHAT_MAX_TOKENS: String(config.agent.quickChat.maxTokens),
        ADL_QUICK_CHAT_TIMEOUT_SECONDS: String(config.agent.quickChat.timeoutSeconds),
        ADL_DEEP_DIVE_MAX_ITERATIONS: String(config.agent.deepDive.maxIterations),
        ADL_DEEP_DIVE_MAX_TOKENS: String(config.agent.deepDive.maxTokens),
        ADL_DEEP_DIVE_TIMEOUT_SECONDS: String(config.agent.deepDive.timeoutSeconds),
        // ADR-077 §2: each turn exchanges the verified Cognito token here for a Harness token.
        ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION: tokenRouteExchangeFunctionArn,
        // ADR-120 decision 9: the route redeems the browser's page reference from this table.
        ADL_PAGE_CONTEXT_TABLE: ssm.StringParameter.valueForStringParameter(
          this,
          paramPath(config.envName, 'pageContextTableName'),
        ),
        // The guardrail layer 1 applies to the question. Its version is published by AgentStack
        // and remains DRAFT because ApplyGuardrail accepts DRAFT; no independently lagging version
        // resource is needed.
        ADL_GUARDRAIL_ID: guardrailId,
        ADL_GUARDRAIL_VERSION: guardrailVersion,
        // Hands the adapter control at init. Without it the layer is inert and Lambda tries to
        // load `run.sh` as a Python module.
        AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
        // The whole point of this transport. Buffered would collect the entire turn and send it
        // at once, which is the behaviour ADR-038 exists to avoid.
        AWS_LWA_INVOKE_MODE: 'response_stream',
        // Its own path, not `/`. A readiness probe that answered real requests would keep
        // reporting healthy once the app was not.
        AWS_LWA_READINESS_CHECK_PATH: '/health',
      },
      // 1024 MB. CPU scales with memory and this function is almost entirely waiting on the
      // harness, but the wait is a streamed parse: every event is decoded and re-encoded as SSE
      // on this thread, so too little CPU shows up as gaps between tokens rather than as a slow
      // start. Chosen for the smoothness of the stream, which is the thing a user perceives.
      memoryMb: 1024,
      // Above the deep-dive budget rather than below it. This is the one function with no caller
      // to sit under — CloudFront's origin read timeout governs the edge, and a function that
      // stopped before the Harness did would abandon a turn the customer has already paid for.
      // The same figure bounds a Harness token's lifetime in TokenServiceStack.
      timeoutSeconds: conversationFunctionTimeoutSeconds(config),
    });

    conversationFunction.addLayers(
      lambda.LayerVersion.fromLayerVersionArn(
        this,
        'LambdaWebAdapter',
        lambdaWebAdapterLayerArn(config.region),
      ),
    );

    // ADR-077 §2–§3: the one way this role obtains a Harness credential is to present a live user
    // token to the route exchange. Scoped to that one function.
    conversationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ExchangeUserTokenForHarnessToken',
        actions: ['lambda:InvokeFunction'],
        resources: [tokenRouteExchangeFunctionArn],
      }),
    );

    // No AgentCore Allow on this role. ADR-077 §2: the route invokes the Harness only with a
    // route-minted bearer token, and the Memory grants are gone because under per-user memory they
    // would read every user's memory. The Deny survives a broad policy attached later.
    conversationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.DENY,
        actions: [
          'bedrock-agentcore:InvokeAgentRuntimeCommand',
          'bedrock-agentcore:InvokeAgentRuntimeCommandShell',
          // ADR-077: the route calls the Harness only with a route-minted bearer; this role must
          // never invoke it.
          'bedrock-agentcore:InvokeHarness',
          'bedrock-agentcore:InvokeAgentRuntime',
        ],
        resources: ['*'],
      }),
    );

    // The managed route writes a standalone audit receipt when evaluation is not selected. When
    // selected, one transaction writes the audit, content-free admission, and retained outbox row.
    conversationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:PutItem', 'dynamodb:GetItem'],
        resources: [auditTableArn],
      }),
    );
    if (evaluationOutbox !== undefined) {
      conversationFunction.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:TransactWriteItems'],
          resources: [auditTableArn, evaluationOutbox.arn],
        }),
      );
    }

    // The guardrail applies to the inbound question only; ADR-083 retired the outbound check.
    // Scoped to the ARN, unlike `bedrock:InvokeGuardrailChecks` which `AgentStack` has to grant on
    // `*` because it takes its configuration inline and names no resource. `ApplyGuardrail` names
    // the guardrail it applies, so a scoped statement is both possible and the honest form: this
    // function may evaluate against the guardrail this stack created, and no other.
    //
    // No `bedrock:InvokeModel`. This route never calls a model — Harness does that inside its own
    // loop under the Harness execution role — and a grant here would make it look as though the
    // narration could be produced locally, which is the guarantee `tools compute, model narrates`
    // depends on.
    // The guardrail runs on the Standard tier through its geography's guardrail profile, so the
    // caller also needs `ApplyGuardrail` on the profile in every Region it may route an assessment
    // to. `config.region`, matching how `routeGuardrail` chose the profile.
    conversationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:ApplyGuardrail'],
        resources: [
          guardrailArn,
          ...guardrailProfileArns(this.partition, config.region, this.account),
        ],
      }),
    );

    // ADR-120 decision 9: the route redeems a page reference at the request boundary, so it reads
    // `page#` records and nothing else in the page-context table: one `GetItem`, narrowed by
    // partition key. `dynamodb:LeadingKeys` must use `ForAllValues` (DynamoDB developer guide,
    // "Specifying conditions"), and `StringLike` matches the `*` wildcard (IAM condition
    // operators), so `artifact#`, capability and selection items stay unreadable to this role.
    conversationFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'RedeemPageContextRecords',
        actions: ['dynamodb:GetItem'],
        resources: [
          `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${pageContextTableName(config.envName)}`,
        ],
        conditions: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['page#*'] },
        },
      }),
    );

    grantSpanExport(conversationFunction);
    this.conversationTerminalQuery(config, conversationFunction);
    const url = conversationFunction.addFunctionUrl({
      // Never NONE. An unauthenticated Function URL is invocable by anyone who finds it, and
      // `infra/test/no-public-endpoints.test.ts` fails the build if this changes.
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
      invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
    });

    this.allowCloudFront(config, conversationFunction, 'Conversation');
    return url;
  }

  /**
   * The saved Logs Insights query over the one terminal line every turn writes (observability
   * §3.9): given a support reference or a correlation ID, it names the turn's outcome and
   * whether its receipt was persisted, without any content.
   */
  private conversationTerminalQuery(config: AppConfig, fn: lambda.Function): void {
    new logs.QueryDefinition(this, 'ConversationTurnTerminalQuery', {
      queryDefinitionName: `adlytics-${config.envName}/conversation-turn-terminal`,
      logGroups: [fn.logGroup],
      queryString: new logs.QueryString({
        fields: [
          '@timestamp',
          'audit_id',
          'correlation_id',
          'managed_terminal',
          'publication_outcome',
          'audit_state',
          'evaluation_state',
          'investigation_next',
          'trace_id',
        ],
        filterStatements: ['operation = "conversation.turn"', 'phase = "terminal"'],
        sort: '@timestamp desc',
      }),
    });
  }
  /**
   * Let CloudFront's Origin Access Control invoke the Function URL, and nothing else.
   *
   * **Two statements, not one, and this is the failure that cost the most to find.** Since October
   * 2025 a new Function URL requires both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`
   * [verified 2026-08-22 against the Lambda function URL auth documentation]. A probe granting only
   * the first returned 403 for every request through CloudFront while succeeding for an IAM user —
   * because the user's identity-based policy already carried `InvokeFunction` and a service
   * principal has no identity-based policy at all.
   *
   * Written by hand rather than through `FunctionUrlOrigin.withOriginAccessControl`, which emits
   * only the `InvokeFunctionUrl` statement [verified 2026-08-22 by reading aws-cdk-lib 2.265.0].
   * The L2 would synthesise cleanly, deploy cleanly, and 403 on every request.
   *
   * Scoped to this account's distribution by `SourceArn`. Without it the grant is to the CloudFront
   * *service*, which means any distribution in any account could invoke this function.
   */
  private allowCloudFront(
    config: AppConfig,
    fn: lambda.Function,
    idPrefix: string,
  ): void {
    // Read rather than derived: a distribution id is service-generated. Published by
    // FrontendStack's first pass, which DEPLOY_ORDER puts well before this stack.
    const distributionId = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'distributionId'),
    );
    const sourceArn = `arn:${cdk.Aws.PARTITION}:cloudfront::${config.account}:distribution/${distributionId}`;

    // Statement one. `functionUrlAuthType` is what ties the grant to the URL rather than to the
    // function, so it cannot be used to invoke the function directly.
    new lambda.CfnPermission(this, `${idPrefix}CloudFrontInvokeFunctionUrl`, {
      functionName: fn.functionArn,
      action: 'lambda:InvokeFunctionUrl',
      principal: 'cloudfront.amazonaws.com',
      functionUrlAuthType: 'AWS_IAM',
      sourceArn,
    });

    // Statement two, the one the L2 omits. `invokedViaFunctionUrl` confines it to invocations that
    // arrived through the URL, so this is not a general grant to invoke the function.
    new lambda.CfnPermission(this, `${idPrefix}CloudFrontInvokeFunction`, {
      functionName: fn.functionArn,
      action: 'lambda:InvokeFunction',
      principal: 'cloudfront.amazonaws.com',
      invokedViaFunctionUrl: true,
      sourceArn,
    });
  }

  /**
   * A Python function with everything that must not vary taken from config.
   *
   * Architecture and runtime are never written inline: one stack diverging from
   * the rest is how an ADOT layer arrives in the wrong architecture and fails at
   * function init rather than at build.
   */
  /**
   * ADR-078: one function serves `turn` and `turns`, routing by route key, and rechecks every
   * section against the review levels the authorizer verified. One function is safe because both
   * routes read the same record families: `turns` reads the receipts day index, `turn` everything
   * else below, and neither writes, scans, or reaches a log, queue, or other store.
   *
   * The role has a fixed name so the receipts table's read deny can name it (ADR-080 §1).
   */
  private qualityReviewFunction(
    config: AppConfig,
    dependencies: lambda.ILayerVersion,
    sourceAssetPath: string,
    baseEnvironment: Record<string, string>,
    auditTableName: string,
    findingsTable: TableRef,
    evaluationTables: { deliveries: TableRef; writerReceipts: TableRef; verdicts: TableRef } | undefined,
  ): lambda.Function {
    const role = lambdaRole(this, 'QualityReviewRole', {
      roleName: qualityReviewFunctionRoleName(config.envName),
    });
    // The id is the function's name suffix: `adlytics-<env>-QualityReview`, which is what
    // qualityReviewFunctionName renders for the function-change alarm (ADR-080 §4).
    const fn = this.pythonFunction('QualityReview', {
      config,
      dependencies,
      sourceAssetPath,
      role,
      handler: 'adapters.inbound.quality_review.handler.handler',
      environment: {
        ...baseEnvironment,
        ADL_AGENT_QUALITY_AUDIT_TABLE: auditTableName,
        ADL_AGENT_QUALITY_FINDINGS_TABLE: findingsTable.name,
        ...(evaluationTables === undefined
          ? {}
          : {
              ADL_EVALUATION_DELIVERIES_TABLE: evaluationTables.deliveries.name,
              ADL_EVALUATION_WRITER_RECEIPTS_TABLE: evaluationTables.writerReceipts.name,
              ADL_EVALUATION_VERDICTS_TABLE: evaluationTables.verdicts.name,
            }),
        // A dispatched turn with no verdict past this reads `overdue` (ADR-079). Config, not a
        // table, so it is set whether or not the evaluation plane is provisioned.
        ADL_EVALUATION_RESULT_DEADLINE_SECONDS: String(config.evaluationResultDeadlineSeconds),
      },
      // Bounded keyed reads and one bounded index page; 512 MiB protects Python cold start.
      memoryMb: 512,
      timeoutSeconds: 10,
    });

    const receipts = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${agentQualityAuditTableName(config.envName)}`;
    // Exact reads: the receipt and its companions, and one delivery and writer receipt by key.
    // Findings and verdicts are read only by the key-bounded queries below.
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'],
      resources: [
        receipts,
        ...(evaluationTables === undefined
          ? []
          : [evaluationTables.deliveries.arn, evaluationTables.writerReceipts.arn]),
      ],
    }));
    // Key-bounded queries: correlation discovery, the `turns` day index, the findings of one
    // receipt, and one verdict per evaluator. No other index and no base-table Query on receipts.
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [
        `${receipts}/index/correlation-audit`,
        `${receipts}/index/turns-by-day`,
        findingsTable.arn,
        ...(evaluationTables === undefined ? [] : [evaluationTables.verdicts.arn]),
      ],
    }));
    return fn;
  }

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
      /** An explicitly named role, when another stack's policy must name it before it exists. */
      role?: iam.IRole;
      /** A reserved-concurrency bound; absent leaves the function on the unreserved pool. */
      reservedConcurrency?: number;
    },
  ): lambda.Function {
    const functionName = `adlytics-${options.config.envName}-${id}`;

    // The log group is declared rather than left to the `logRetention` property.
    // That property provisions a Node singleton function to call PutRetentionPolicy
    // at deploy time, which lands an x86_64 function in a stack where every
    // function is meant to be arm64 — caught by the arm64 assertion in the tests.
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: options.config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // This helper declares the group, so it grants on it, for a passed role or its own (ADR-085).
    const role = options.role ?? lambdaRole(this, `${id}ServiceRole`);
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
      ...(options.reservedConcurrency === undefined
        ? {}
        : { reservedConcurrentExecutions: options.reservedConcurrency }),
      logGroup,
    });
  }

  /** Grant only the curated CURRENT read path used by analytical presentation pages. */
  private grantPresentationCuratedRead(config: AppConfig, fn: lambda.Function): void {
    const semantic = dataBucketName(config, 'semantic-v2');
    const results = dataBucketName(config, 'v2-results');
    const database = semanticV2DatabaseName(config.envName, 'current');

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
        conditions: { StringLike: { 's3:prefix': ['canonical/current/*'] } },
      }),
    );
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
        conditions: { StringLike: { 's3:prefix': ['interactive/*'] } },
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
  }

  /** The S1 run-request admission function; the authorizer enforces the requester entitlement. */
  private runRequestsFunction(
    config: AppConfig,
    dependencies: lambda.ILayerVersion,
    sourceAssetPath: string,
  ): lambda.Function {
    const table = {
      name: ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'executionGraphTableName'),
      ),
      arn: ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'executionGraphTableArn'),
      ),
    };
    const rawBucket = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'semanticV2RawBucketName'),
    );
    const fn = this.pythonFunction('RunRequestsFunction', {
      config,
      dependencies,
      sourceAssetPath,
      handler: 'adapters.inbound.run_requests.handler.handler',
      environment: {
        ...tracingEnvironment(config),
        ADL_EXECUTION_GRAPH_TABLE: table.name,
        ADL_RAW_BUCKET: rawBucket,
      },
      memoryMb: 512,
      timeoutSeconds: 20,
    });
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: [...WRITER_ACTIONS],
      resources: [table.arn],
    }));
    // Owner-gate item 6: the mode item and command receipts belong to the bound operator
    // profile alone. DynamoDB evaluates each transaction member's own action and leading key,
    // so this Deny refuses a MODE#/COMMAND# write inside TransactWriteItems too.
    fn.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.DENY,
      actions: [...OPERATOR_ONLY_DENIED_ACTIONS],
      resources: [table.arn],
      conditions: {
        'ForAnyValue:StringLike': {
          'dynamodb:LeadingKeys': OPERATOR_ONLY_PREFIXES.map((prefix) => `${prefix}*`),
        },
      },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [`arn:${cdk.Aws.PARTITION}:s3:::${rawBucket}/*`],
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:ListBucketVersions'],
      resources: [`arn:${cdk.Aws.PARTITION}:s3:::${rawBucket}`],
    }));
    // S6 entry item 4: rows 9 and 13 in epoch 1 read the legacy authority (`LegacyPlans`): the
    // coordinator table names from their exact SSM paths, then the successor owner and plan
    // META under `WRITE_PLAN#`, strongly. Read-class actions only.
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ResolveLegacyPlanTables',
      actions: ['ssm:GetParameter'],
      resources: ['semantic-publications-table', 'semantic-leases-table'].map((name) =>
        cdk.Stack.of(this).formatArn({
          service: 'ssm',
          resource: 'parameter',
          resourceName: `adlytics/${config.envName}/data/v2/${name}`,
        }),
      ),
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadLegacyPlanAuthority',
      actions: ['dynamodb:GetItem'],
      resources: [
        cdk.Stack.of(this).formatArn({
          service: 'dynamodb',
          resource: 'table',
          resourceName: semanticV2StateTableName(config.envName, 'publications'),
        }),
      ],
      conditions: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['WRITE_PLAN#*'] },
      },
    }));
    return fn;
  }

  /**
   * S2 (BF-5): the explanation reads graph authority read-only, with no write action of any kind
   * on the live table, and signs its stateless cursors with one KMS HMAC key. The key's alias is
   * derived from the graph table name (`alias/<table>-cursor-mac`), as the function derives it,
   * so the cursor key adds no configuration key. No cursor table exists.
   */
  private grantExplanationReads(
    fn: lambda.Function,
    authority: { graphTable: string; graphTableArn: string; rawBucket: string },
  ): void {
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadExecutionGraphAuthority',
      actions: ['dynamodb:GetItem', 'dynamodb:BatchGetItem'],
      resources: [authority.graphTableArn],
    }));
    // Exact-version HeadObject of canonical raw manifests only; no other raw object is readable.
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadManifestVersion',
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [`arn:${cdk.Aws.PARTITION}:s3:::${authority.rawBucket}/*/manifest.json`],
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ListManifestVersions',
      actions: ['s3:ListBucket', 's3:ListBucketVersions'],
      resources: [`arn:${cdk.Aws.PARTITION}:s3:::${authority.rawBucket}`],
    }));
    const cursorKey = new kms.Key(this, 'ExecutionCursorMacKey', {
      description: 'HMAC key for the stateless execution-explanation cursors (S2, BF-5).',
      keySpec: kms.KeySpec.HMAC_256,
      keyUsage: kms.KeyUsage.GENERATE_VERIFY_MAC,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    cursorKey.addAlias(`alias/${authority.graphTable}-cursor-mac`);
    fn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'SignStatelessCursors',
      actions: ['kms:GenerateMac', 'kms:VerifyMac'],
      resources: [cursorKey.keyArn],
    }));
  }

  private requireRouteAuthorizer(): apigwv2.IHttpRouteAuthorizer {
    if (this.routeAuthorizer === undefined) {
      throw new Error('The session authorizer is created by addRoutes, before any other route.');
    }
    return this.routeAuthorizer;
  }

  /** `POST` and `GET` run-request routes behind the existing session authorizer. */
  private addRunRequestRoutes(fn: lambda.Function, _config: AppConfig): void {
    const integration = new integrations.HttpLambdaIntegration('RunRequestsIntegration', fn);
    for (const route of API_ROUTES) {
      const [method, path] = route.split(' ') as [string, string];
      this.api.addRoutes({
        path,
        methods: [method === 'POST' ? apigwv2.HttpMethod.POST : apigwv2.HttpMethod.GET],
        integration,
        authorizer: this.requireRouteAuthorizer(),
      });
    }
  }

  private addRoutes(
    authFunction: lambda.Function,
    authorizerFunction: lambda.Function,
    presentationFunction: lambda.Function,
    operationalStatusFunction: lambda.Function | undefined,
    qualityReviewFunction: lambda.Function,
    promptAdminFunctions: { create: lambda.Function; calibrate: lambda.Function; activate: lambda.Function; retire: lambda.Function; slots: lambda.Function; versions: lambda.Function },
    config: AppConfig,
  ): ReadonlyArray<[PageRoute, apigwv2.HttpRoute]> {
    const authIntegration = new integrations.HttpLambdaIntegration(
      'AuthIntegration',
      authFunction,
    );

    // Unauthenticated by necessity, not by oversight. Login and callback run
    // before a session exists; refresh runs precisely when the access token has
    // expired, so the authorizer would deny it; logout is tearing the session
    // down. All four defend themselves — see docs/concepts/authentication.md §2.
    this.api.addRoutes({
      path: `${API_PREFIX}/auth/login`,
      methods: [apigwv2.HttpMethod.GET],
      integration: authIntegration,
    });
    this.api.addRoutes({
      path: `${API_PREFIX}/auth/callback`,
      methods: [apigwv2.HttpMethod.GET],
      integration: authIntegration,
    });
    this.api.addRoutes({
      path: `${API_PREFIX}/auth/refresh`,
      methods: [apigwv2.HttpMethod.POST],
      integration: authIntegration,
    });
    this.api.addRoutes({
      path: `${API_PREFIX}/auth/logout`,
      methods: [apigwv2.HttpMethod.POST],
      integration: authIntegration,
    });

    const sessionAuthorizer = new apigwv2.HttpAuthorizer(this, 'CookieAuthorizer', {
      httpApi: this.api,
      type: apigwv2.HttpAuthorizerType.LAMBDA,
      authorizerName: `adlytics-cookie-${config.envName}`,
      authorizerUri: `arn:${cdk.Aws.PARTITION}:apigateway:${cdk.Aws.REGION}:lambda:path/2015-03-31/functions/${authorizerFunction.functionArn}/invocations`,
      payloadFormatVersion: apigwv2.AuthorizerPayloadVersion.VERSION_2_0,
      enableSimpleResponses: true,
      // No result cache, and synth refuses one (`refuseAuthorizerResultCache`). Every caller
      // presents the same identity source, the origin-verify header, so a cached result would
      // share one caller's decision with every other caller.
      resultsCacheTtl: cdk.Duration.seconds(0),
      // ADR-078: the header CloudFront adds to every API origin request. API Gateway answers a
      // request without it with 401 and never invokes the authorizer, which then compares the
      // value in constant time before any other check. The credential stays out of the identity
      // source: a request carries a cookie or a bearer, never both.
      identitySource: [`$request.header.${ORIGIN_VERIFY_HEADER}`],
    });
    this.refuseAuthorizerResultCache();

    authorizerFunction.addPermission('ApiGatewayInvoke', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: cdk.Stack.of(this).formatArn({
        service: 'execute-api',
        resource: this.api.apiId,
        resourceName: `authorizers/${sessionAuthorizer.authorizerId}`,
      }),
    });

    const authorizer = new ExistingAuthorizer(sessionAuthorizer.authorizerId);
    this.routeAuthorizer = authorizer;
    this.api.addRoutes({
      path: `${API_PREFIX}/me`,
      methods: [apigwv2.HttpMethod.GET],
      integration: authIntegration,
      authorizer,
    });

    // ADR-078: one integration, two exact POST routes. The authorizer admits a bearer on these two
    // route keys only; the function routes by route key.
    const qualityReviewIntegration = new integrations.HttpLambdaIntegration(
      'QualityReviewIntegration',
      qualityReviewFunction,
    );
    for (const route of ['turn', 'turns']) {
      this.api.addRoutes({
        path: `${API_PREFIX}/quality-review/${route}`,
        methods: [apigwv2.HttpMethod.POST],
        integration: qualityReviewIntegration,
        authorizer,
      });
    }

    const promptAdminRoutes = [
      ['create-version', promptAdminFunctions.create],
      ['activate', promptAdminFunctions.activate],
      ['retire', promptAdminFunctions.retire],
    ] as const;
    for (const [command, fn] of promptAdminRoutes) {
      this.api.addRoutes({
        path: `${API_PREFIX}/prompt-registry/${command}`,
        methods: [apigwv2.HttpMethod.POST],
        integration: new integrations.HttpLambdaIntegration(`PromptRegistry${command}Integration`, fn),
        authorizer,
      });
    }
    // Recovery for a durable pending calibration is deliberately outside the browser
    // contract. The five canonical registry routes remain the only UI-facing surface.
    this.api.addRoutes({
      path: `${API_PREFIX}/prompt-registry/extensions/calibrate`,
      methods: [apigwv2.HttpMethod.POST],
      integration: new integrations.HttpLambdaIntegration('PromptRegistryCalibrationExtensionIntegration', promptAdminFunctions.calibrate),
      authorizer,
    });
    for (const [projection, fn] of [
      ['slots', promptAdminFunctions.slots],
      ['versions', promptAdminFunctions.versions],
    ] as const) {
      this.api.addRoutes({
        path: `${API_PREFIX}/prompt-registry/${projection}`,
        methods: [apigwv2.HttpMethod.POST],
        integration: new integrations.HttpLambdaIntegration(`PromptRegistry${projection}Integration`, fn),
        authorizer,
      });
    }

    if (operationalStatusFunction !== undefined) {
      const operationalStatusIntegration = new integrations.HttpLambdaIntegration(
        'OperationalStatusIntegration',
        operationalStatusFunction,
      );
      for (const path of [
        `${API_PREFIX}/operations/data-flow-definitions`,
        `${API_PREFIX}/operations/data-flows`,
        `${API_PREFIX}/operations/data-flows/{flow_id}`,
        `${API_PREFIX}/operations/data-flows/{flow_id}/events`,
      ]) {
        this.api.addRoutes({
          path,
          methods: [apigwv2.HttpMethod.GET],
          integration: operationalStatusIntegration,
          authorizer,
        });
      }
      // S2 (FR-8): the protected execution explanation, the same function and entitlement.
      this.api.addRoutes({
        path: `${API_PREFIX}/operations/execution-explanations`,
        methods: [apigwv2.HttpMethod.POST],
        integration: operationalStatusIntegration,
        authorizer,
      });
    }

    const presentationIntegration = new integrations.HttpLambdaIntegration(
      'PresentationIntegration',
      presentationFunction,
    );
    // ADR-148: the page roster, with each route's ADR comment, lives with the page alarms.
    return PAGE_ROUTES.map((page): [PageRoute, apigwv2.HttpRoute] => {
      const [route] = this.api.addRoutes({
        path: page.path,
        methods: [apigwv2.HttpMethod.POST],
        integration: presentationIntegration,
        authorizer,
      });
      if (route === undefined) throw new Error(`No HTTP API route was created for ${page.path}.`);
      return [page, route];
    });
  }


  /**
   * Human-facing values for `aws cloudformation describe-stacks`.
   *
   * Deliberately without `exportName`, so none of these becomes a
   * CloudFormation export. An export cannot be changed or deleted while another
   * stack imports it, which is the deadlock ADR-012 replaced with SSM. These are
   * for operators and post-deploy checks only.
   */
  /**
   * ADR-078: synth fails if any authorizer in this stack caches its result.
   *
   * A validation rather than a constant, so an escape hatch or a later authorizer that sets a TTL
   * fails the synth instead of shipping a cache keyed on a value every caller shares.
   */
  private refuseAuthorizerResultCache(): void {
    this.node.addValidation({
      validate: () =>
        this.node
          .findAll()
          .filter((node): node is apigwv2.CfnAuthorizer => node instanceof apigwv2.CfnAuthorizer)
          .filter((authorizer) => {
            // An explicit zero only: an omitted TTL would leave the result to a service default.
            const ttl = this.resolve(authorizer.authorizerResultTtlInSeconds) as unknown;
            return ttl !== 0;
          })
          .map(
            (authorizer) =>
              `Authorizer ${authorizer.node.path} does not declare a result TTL of 0; ADR-078 ` +
              'requires a TTL of 0.',
          ),
    });
  }

  /**
   * ADR-078: one alarm over every origin-verify refusal the authorizer or the auth handler makes.
   *
   * A request with no header never reaches either function (API Gateway answers 401), so this
   * counts the requests that sent the header name with a wrong value, and requests to the four
   * auth routes that bypassed the edge. Neither happens on the edge path, so one is worth a look.
   */
  private alarmOnOriginVerifyRefusals(config: AppConfig, functions: lambda.Function[]): void {
    const metric = { namespace: 'Adlytics/Edge', name: 'OriginVerifyRefused' };
    functions.forEach((fn, index) => {
      new logs.MetricFilter(this, `OriginVerifyRefusedFilter${index}`, {
        logGroup: fn.logGroup,
        filterPattern: logs.FilterPattern.stringValue('$.category', '=', 'origin_verify_refused'),
        metricNamespace: metric.namespace,
        metricName: metric.name,
        metricValue: '1',
      });
    });
    const alarm = new cloudwatch.Alarm(this, 'OriginVerifyRefusedAlarm', {
      alarmName: `adlytics-${config.envName}-origin-verify-refused`,
      alarmDescription:
        'A request reached the authorizer or an auth route without the edge origin-verify ' +
        'value: someone is calling the execute-api hostname directly (ADR-078).',
      metric: new cloudwatch.Metric({
        namespace: metric.namespace,
        metricName: metric.name,
        statistic: cloudwatch.Stats.SUM,
        period: cdk.Duration.minutes(5),
      }),
      threshold: 0,
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

  private publishOutputs(
    authFunction: lambda.Function,
    authorizerFunction: lambda.Function,
  ): void {
    new cdk.CfnOutput(this, 'OutApiId', {
      value: this.api.apiId,
      description: 'HTTP API behind the CloudFront /api/* behaviour.',
    });
    new cdk.CfnOutput(this, 'OutApiEndpoint', {
      value: this.api.apiEndpoint,
      description: 'Origin only. The browser must never be sent here directly.',
    });
    new cdk.CfnOutput(this, 'OutAuthFunctionName', {
      value: authFunction.functionName,
      description: 'Handler for every auth route.',
    });
    new cdk.CfnOutput(this, 'OutAuthorizerFunctionName', {
      value: authorizerFunction.functionName,
      description: 'REQUEST authorizer reading the session cookie.',
    });
  }

  private publishParameters(
    config: AppConfig,
    publicOrigin: string,
    conversationUrl: lambda.FunctionUrl,
  ): void {
    new ssm.StringParameter(this, 'ParamApiBaseUrl', {
      parameterName: paramPath(config.envName, 'apiBaseUrl'),
      // The public origin, not the execute-api host. Consumers build browser URLs
      // from this, and the browser must never be sent to API Gateway directly:
      // a cookie set on execute-api would not come back to CloudFront.
      //
      // A copy of frontend/public-base-url, and therefore a snapshot: if the
      // distribution is ever replaced, this goes stale until ApiStack redeploys.
      // ADR-012 accepts that — a changed parameter does not redeploy consumers.
      stringValue: publicOrigin,
    });
    new ssm.StringParameter(this, 'ParamApiId', {
      parameterName: paramPath(config.envName, 'apiId'),
      stringValue: this.api.apiId,
    });
    new ssm.StringParameter(this, 'ParamConversationFunctionUrl', {
      parameterName: paramPath(config.envName, 'conversationFunctionUrl'),
      // The full URL including the scheme, as Lambda reports it. `FrontendStack` needs only the
      // host, and derives it there rather than this publishing a half-value: a parameter holding
      // a hostname would be a different thing from what Lambda calls the function URL, and the
      // difference is exactly the kind that gets copied wrongly once.
      stringValue: conversationUrl.url,
    });
  }
}

/**
 * Attach an authorizer that was created separately.
 *
 * `HttpAuthorizer` is built directly rather than through
 * `HttpLambdaAuthorizer` because that helper does not expose
 * `enableSimpleResponses`, and a simple response is what keeps the decision
 * scoped to the invoked route instead of emitting an IAM policy that could be
 * broader than intended.
 */
class ExistingAuthorizer implements apigwv2.IHttpRouteAuthorizer {
  constructor(private readonly authorizerId: string) {}

  public bind(): apigwv2.HttpRouteAuthorizerConfig {
    return {
      authorizerId: this.authorizerId,
      authorizationType: 'CUSTOM',
    };
  }
}
