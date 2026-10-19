// SPDX-License-Identifier: MIT-0

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  harnessExecutionRoleName,
  harnessName,
  harnessProviderRoleName,
  harnessTokenAudience,
  paramPath,
  routeClientId,
  tokenCredentialProviderName,
} from '../config';
import { modelInvokeStatements } from '../bedrock-model-access';
import { loadGeneratedTools } from '../constructs/agent/generated-tools';
import { cloudFormationExecutionRoleArn } from '../constructs/agent-quality/record-integrity';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import {
  applyRemovalPolicy,
  applyStandardTags,
  requestTagCondition,
  standardTags,
  tagOwnResourceCondition,
} from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface HarnessStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Immutable managed-conversation prompt artifact. */
  readonly promptPath: string;
  /** Generated Gateway tool contract staged by make build-lambdas. */
  readonly toolSchemaPath: string;
  /** Directory holding the staged Python source tree (the Harness provider's code). */
  readonly sourceAssetPath: string;
}

/** The custom resource type that owns the Harness through the control-plane API. */
export const HARNESS_RESOURCE_TYPE = 'Custom::AgentCoreHarness';

/** The scope the Harness's Gateway tool requests at the token service's `/token` (ADR-077 §4). */
export const GATEWAY_TOKEN_SCOPE = 'adlytics-gateway';

/**
 * The actions CloudFormation's own `AWS::BedrockAgentCore::Harness` handlers hold across create,
 * update and delete (resource provider registry, read 2026-09-26), minus managed memory, which
 * this resource refuses. CreateHarness provisions the underlying runtime, its endpoint and
 * workload identity with the caller's credentials, which is why runtime and identity actions
 * appear here at all.
 */
export const HARNESS_PROVIDER_AGENTCORE_ACTIONS = [
  'bedrock-agentcore:CreateAgentRuntime',
  'bedrock-agentcore:CreateAgentRuntimeEndpoint',
  'bedrock-agentcore:CreateHarness',
  'bedrock-agentcore:CreateWorkloadIdentity',
  'bedrock-agentcore:DeleteAgentRuntime',
  'bedrock-agentcore:DeleteAgentRuntimeEndpoint',
  'bedrock-agentcore:DeleteHarness',
  'bedrock-agentcore:DeleteWorkloadIdentity',
  'bedrock-agentcore:GetAgentRuntime',
  'bedrock-agentcore:GetAgentRuntimeEndpoint',
  'bedrock-agentcore:GetHarness',
  'bedrock-agentcore:GetMemory',
  'bedrock-agentcore:ListTagsForResource',
  'bedrock-agentcore:TagResource',
  'bedrock-agentcore:UntagResource',
  'bedrock-agentcore:UpdateAgentRuntime',
  'bedrock-agentcore:UpdateAgentRuntimeEndpoint',
  'bedrock-agentcore:UpdateHarness',
] as const;

/** The service-linked role a Harness runtime's workload identity uses (observed in dev 2026-09-26). */
const RUNTIME_IDENTITY_SERVICE = 'runtime-identity.bedrock-agentcore.amazonaws.com';

/**
 * Destroyable primary managed Harness over the governed Gateway tool surface.
 *
 * The Harness is owned by a Lambda-backed custom resource rather than `CfnHarness`, because
 * CloudFormation's Harness schema rejects the Gateway tool's `TOKEN_EXCHANGE` grant that ADR-077
 * §4 requires, while the control-plane API accepts it (ADR-077, amended 2026-09-26). One owner:
 * the custom resource creates, updates and deletes the whole Harness.
 */
export class HarnessStack extends cdk.Stack {
  public readonly harness: cdk.CustomResource;

  constructor(scope: Construct, id: string, props: HarnessStackProps) {
    super(scope, id, props);
    const { config } = props;
    const gatewayId = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'gatewayId'),
    );
    const gatewayArn = this.formatArn({
      service: 'bedrock-agentcore',
      resource: 'gateway',
      resourceName: gatewayId,
    });
    const memoryArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'memoryArn'),
    );
    // CloudFormation re-resolves an SSM-typed parameter on every deploy, so a changed strategy
    // digest sends this resource an Update, and the provider's UpdateHarness re-derives the
    // retrieval configuration, as AWS directs after a BYO memory's strategies change
    // (ADR-147 decision 4; AgentCore harness-memory guide). A property, never a spec member.
    const memoryStrategyRevision = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'memoryStrategyRevision'),
    );
    const tokenIssuerUrl = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'tokenIssuerUrl'),
    );
    const tokenCredentialProviderArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'tokenCredentialProviderArn'),
    );
    const generated = loadGeneratedTools(props.toolSchemaPath);
    const systemPrompt = fs.readFileSync(props.promptPath, 'utf-8').trim();
    if (systemPrompt.length === 0) throw new Error('Managed conversation prompt is empty.');
    const promptDigest = `sha256:${crypto.createHash('sha256').update(systemPrompt).digest('hex')}`;
    const allowedTools = [
      ...generated.catalog.map((tool) => `@adlytics/catalog___${tool.name}`),
      ...generated.measurement.map((tool) => `@adlytics/measurement___${tool.name}`),
    ];

    const role = new iam.Role(this, 'ExecutionRole', {
      // Named: the agent client secret admits this role by ARN before the role exists.
      roleName: harnessExecutionRoleName(config.envName),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Assumed only by the primary managed Harness.',
    });
    for (const statement of modelInvokeStatements(
      this,
      'InvokeConfiguredModels',
      [config.agent.deepDive.modelId],
      ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    )) role.addToPolicy(statement);
    // No Gateway IAM grant (ADR-077 §4): the Gateway authorizes CUSTOM_JWT, and the Harness
    // reaches it only with the token its tool exchanges at the token service.
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'UseConversationMemory',
      actions: [
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:DeleteEvent',
        'bedrock-agentcore:GetEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:RetrieveMemoryRecords',
      ],
      resources: [memoryArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'DenyDirectCommands',
      effect: iam.Effect.DENY,
      actions: [
        'bedrock-agentcore:InvokeAgentRuntimeCommand',
        'bedrock-agentcore:InvokeAgentRuntimeCommandShell',
      ],
      resources: ['*'],
    }));
    this.addHarnessRuntimePolicies(role, config);

    // The whole CreateHarness request in the API's own camelCase shape. The provider validates the
    // top level and the service the rest; `UpdateHarness` receives every mutable member of it.
    const spec = {
      harnessName: harnessName(config.envName),
      executionRoleArn: role.roleArn,
      // ADR-077 §2 (amended 2026-09-26): the Harness admits only a token the token service minted
      // for the route. The Harness audience refuses a Cognito token and a Gateway-audience token;
      // the route client refuses any other client; SigV4 is refused whenever a JWT authorizer is
      // declared.
      authorizerConfiguration: {
        customJWTAuthorizer: {
          discoveryUrl: `${tokenIssuerUrl}/.well-known/openid-configuration`,
          allowedAudience: [harnessTokenAudience(config.envName)],
          allowedClients: [routeClientId(config.envName)],
        },
      },
      model: {
        bedrockModelConfig: {
          modelId: config.agent.deepDive.modelId,
          maxTokens: config.agent.deepDive.maxTokens,
          temperature: 0,
          apiFormat: 'converse_stream',
        },
      },
      // The retained MemoryStack publishes this exact resource. Never use
      // managedMemoryConfiguration here: that would create conversation state as a side effect of
      // this DESTROY stack, and the provider refuses it. If that explicit parameter is ever
      // removed, memory must be disabled.
      memory: { agentCoreMemoryConfiguration: { arn: memoryArn } },
      tools: [
        {
          type: 'agentcore_gateway',
          name: 'adlytics',
          config: {
            agentCoreGateway: {
              gatewayArn,
              // ADR-077 §4: the Harness exchanges its inbound (route-minted) token at the token
              // service's `/token` for a Gateway-audience token. The reason this resource is not
              // a CfnHarness: CloudFormation's schema rejects this grant type.
              outboundAuth: {
                oauth: {
                  providerArn: tokenCredentialProviderArn,
                  scopes: [GATEWAY_TOKEN_SCOPE],
                  grantType: 'TOKEN_EXCHANGE',
                },
              },
            },
          },
        },
      ],
      allowedTools,
      systemPrompt: [{ text: systemPrompt }],
      maxIterations: config.agent.deepDive.maxIterations,
      maxTokens: config.agent.deepDive.maxTokens,
      timeoutSeconds: config.agent.deepDive.timeoutSeconds,
      // The Tags aspect cannot reach a resource an API call creates, so the set travels here.
      tags: standardTags(config),
    };

    const provider = this.harnessProvider(config, props.sourceAssetPath, role, gatewayArn, memoryArn);
    const providerPolicy = this.providerInvokeBoundary(provider);
    this.harness = new cdk.CustomResource(this, 'ManagedHarness', {
      serviceToken: provider.functionArn,
      resourceType: HARNESS_RESOURCE_TYPE,
      // One JSON string: CloudFormation delivers every property value as text, so a nested
      // structure would arrive with its numbers stringified.
      // The runtime's own log group holds every conversation and raw tool result, and AgentCore
      // creates it with no retention; the provider applies this one before reporting READY.
      properties: {
        HarnessSpec: this.toJsonString(spec),
        RuntimeLogRetentionDays: String(config.harnessRuntimeLogRetentionDays),
        MemoryStrategyRevision: memoryStrategyRevision,
      },
      // Above the provider's 15-minute timeout, so a provider that never answers fails the
      // deploy in 20 minutes rather than CloudFormation's default hour.
      serviceTimeout: cdk.Duration.minutes(20),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // The execution role and its policy exist before the Harness assumes it, and outlive it.
    this.harness.node.addDependency(role);
    // The provider is invocable only by the deployment before the Harness is first created, and
    // until after its delete.
    this.harness.node.addDependency(providerPolicy);

    new ssm.StringParameter(this, 'ParamHarnessArn', {
      parameterName: paramPath(config.envName, 'harnessArn'),
      stringValue: this.harness.getAttString('Arn'),
      description: 'Primary managed Harness invoked by the verified Conversation API.',
    });
    new ssm.StringParameter(this, 'ParamHarnessPromptDigest', {
      parameterName: paramPath(config.envName, 'harnessPromptDigest'),
      stringValue: promptDigest,
      description: 'Digest of the managed conversation narration and finalization prompt.',
    });
    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
    applyStandardTags(this, config);
  }

  /**
   * The arm64 Python function behind `Custom::AgentCoreHarness`.
   *
   * A plain Lambda-backed provider rather than `cr.Provider`, whose framework functions are x86
   * Node. Its role holds `HARNESS_PROVIDER_AGENTCORE_ACTIONS`, bounded as `harnessOwnership`
   * describes, plus passing the one execution role to AgentCore.
   */
  private harnessProvider(
    config: AppConfig,
    sourceAssetPath: string,
    executionRole: iam.Role,
    gatewayArn: string,
    memoryArn: string,
  ): lambda.Function {
    const functionName = `adlytics-${config.envName}-HarnessProvider`;
    // Declared rather than left to `logRetention`, which provisions an x86_64 singleton function.
    const logGroup = new logs.LogGroup(this, 'HarnessProviderLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const providerRole = new iam.Role(this, 'HarnessProviderRole', {
      roleName: harnessProviderRoleName(config.envName),
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Creates, updates and deletes the managed Harness for its custom resource.',
    });
    providerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WriteProviderLogs',
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [`${logGroup.logGroupArn}:*`],
    }));
    providerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'RetainHarnessRuntimeLogs',
      actions: ['logs:CreateLogGroup', 'logs:PutRetentionPolicy', 'logs:TagResource'],
      // Only this Harness's runtime groups: `harness_<name>-<suffix>-DEFAULT`.
      resources: [
        `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/harness_${harnessName(config.envName)}-*`,
      ],
    }));
    providerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadBackHarnessRuntimeLogs',
      // DescribeLogGroups authorizes against the account's log groups, not one name; it returns
      // group settings, never events.
      actions: ['logs:DescribeLogGroups'],
      resources: [`arn:${this.partition}:logs:${this.region}:${this.account}:log-group:*`],
    }));
    for (const statement of this.harnessOwnership(config, gatewayArn, memoryArn)) {
      providerRole.addToPolicy(statement);
    }
    providerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'PassHarnessExecutionRole',
      actions: ['iam:PassRole'],
      resources: [executionRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' } },
    }));
    providerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CreateRuntimeIdentityServiceLinkedRole',
      actions: ['iam:CreateServiceLinkedRole'],
      resources: [
        `arn:${this.partition}:iam::${this.account}:role/aws-service-role/${RUNTIME_IDENTITY_SERVICE}/*`,
      ],
      conditions: { StringEquals: { 'iam:AWSServiceName': RUNTIME_IDENTITY_SERVICE } },
    }));
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      ssm.StringParameter.valueForStringParameter(
        this,
        paramPath(config.envName, 'dependenciesLayerArn'),
      ),
    );
    const provider = new lambda.Function(this, 'HarnessProvider', {
      functionName,
      role: providerRole,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(sourceAssetPath),
      handler: 'adapters.jobs.harness_resource.handler.handler',
      layers: [dependencies],
      environment: tracingEnvironment(config),
      // 256 MB: the work is a handful of control-plane calls and a poll every ten seconds; memory
      // buys nothing on a path that waits on AgentCore, and it runs only on deploys.
      memorySize: 256,
      // Lambda's maximum. A create provisions a runtime and takes minutes; the handler stops
      // polling two minutes before this so the FAILED response still goes out.
      timeout: cdk.Duration.minutes(15),
      logGroup,
    });
    grantSpanExport(provider);
    return provider;
  }

  /**
   * What the provider may do to AgentCore (H-IAM-AGENTCORE-CREATE-001). No IAM key bounds the name
   * or content of a Harness, so every create must carry exactly the standard tag set. The runtime
   * and identity creates AgentCore makes on the provider's behalf present unnamed ARNs
   * (`runtime/*`, `…/workload-identity/*`) and do not satisfy `aws:ViaAWSService`, so they are
   * bounded by the tag set alone (dev creates, 2026-10-04). Everything else is scoped to this
   * Harness's own name, plus Get/Update/Delete on the Gateway and Memory the HarnessSpec
   * references, which CloudTrail records on those calls. `CreateAgentRuntimeEndpoint` carries no
   * tag condition: CloudTrail records no event of its own for it, so which tags its check sees is
   * unknown, and a refused one fails the create; it is granted on the unnamed `runtime/*` ARN
   * AgentCore presents, which is the residual.
   */
  private harnessOwnership(config: AppConfig, gatewayArn: string, memoryArn: string): iam.PolicyStatement[] {
    const name = harnessName(config.envName);
    const agentcoreArn = (resource: string): string =>
      `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:${resource}`;
    const action = (...names: string[]): string[] => names.map((entry) => `bedrock-agentcore:${entry}`);
    const identities = [
      agentcoreArn('workload-identity-directory/default'),
      agentcoreArn(`workload-identity-directory/default/workload-identity/harness_${name}-*`),
    ];
    return [
      // No resource type for the create; with a typed resource the create is refused.
      new iam.PolicyStatement({
        sid: 'CreateTheHarnessTagged',
        actions: action('CreateHarness'),
        resources: ['*'],
        conditions: requestTagCondition(config),
      }),
      // Tag-on-create is authorized inside the create, against an ARN CloudTrail does not show.
      new iam.PolicyStatement({
        sid: 'TagWithTheStandardSet',
        actions: action('TagResource'),
        resources: ['*'],
        conditions: tagOwnResourceCondition(config),
      }),
      // Tag-bound only. AgentCore's on-behalf runtime create carries the exact tag set but does not
      // satisfy `aws:ViaAWSService=true`: with that term the first dev create was refused (CloudTrail
      // CreateAgentRuntime AccessDenied, invokedBy bedrock-agentcore.amazonaws.com, the three tags
      // present, 2026-10-04).
      new iam.PolicyStatement({
        sid: 'CreateTheHarnessRuntimeTagged',
        actions: action('CreateAgentRuntime'),
        resources: ['*'],
        conditions: requestTagCondition(config),
      }),
      // Tag-bound only, on the unnamed identity ARN AgentCore presents: the dev create was refused
      // on `workload-identity-directory/default/workload-identity/*` under the name-scoped,
      // `aws:ViaAWSService` grant (2026-10-04), and the runtime create showed the on-behalf calls do
      // not satisfy that key.
      new iam.PolicyStatement({
        sid: 'CreateTheHarnessIdentityTagged',
        actions: action('CreateWorkloadIdentity'),
        resources: [
          agentcoreArn('workload-identity-directory/default'),
          agentcoreArn('workload-identity-directory/default/workload-identity/*'),
        ],
        conditions: requestTagCondition(config),
      }),
      new iam.PolicyStatement({
        sid: 'OwnTheHarness',
        actions: action('GetHarness', 'UpdateHarness', 'DeleteHarness', 'ListTagsForResource', 'UntagResource'),
        resources: [agentcoreArn(`harness/${name}-*`)],
      }),
      // Each of these three has only the `harness` resource type, so this grants nothing on the
      // Gateway or Memory themselves; it admits the calls if AgentCore also authorizes them
      // against the ARNs CloudTrail lists on them.
      new iam.PolicyStatement({
        sid: 'OwnTheHarnessReferences',
        actions: action('GetHarness', 'UpdateHarness', 'DeleteHarness'),
        resources: [gatewayArn, memoryArn],
      }),
      // AgentCore's on-behalf runtime create also authorizes the runtime's DEFAULT endpoint, against
      // the unnamed `runtime/*` ARN, under the CreateAgentRuntime CloudTrail event (refused under a
      // name-scoped grant in the first dev create, 2026-10-04). That ARN cannot be named in advance,
      // so this one action is granted on it, with no tag condition (see the method comment).
      new iam.PolicyStatement({
        sid: 'CreateTheHarnessRuntimeEndpoint',
        actions: action('CreateAgentRuntimeEndpoint'),
        resources: [agentcoreArn('runtime/*')],
      }),
      new iam.PolicyStatement({
        sid: 'OwnTheHarnessRuntime',
        actions: action(
          'GetAgentRuntime',
          'UpdateAgentRuntime',
          'DeleteAgentRuntime',
          'GetAgentRuntimeEndpoint',
          'UpdateAgentRuntimeEndpoint',
          'DeleteAgentRuntimeEndpoint',
        ),
        resources: [
          agentcoreArn(`runtime/harness_${name}-*`),
          agentcoreArn(`runtime/harness_${name}-*/runtime-endpoint/*`),
        ],
      }),
      new iam.PolicyStatement({
        sid: 'OwnTheHarnessIdentity',
        actions: action('DeleteWorkloadIdentity'),
        resources: identities,
      }),
      new iam.PolicyStatement({
        sid: 'ReadTheConversationMemory',
        actions: action('GetMemory'),
        resources: [memoryArn],
      }),
    ];
  }

  /**
   * The provider acts on any callback it is handed, so who may invoke it is who may create,
   * replace or delete the Harness. The same Deny the evaluation gate's provider carries confines
   * that to the stack's CloudFormation execution role and Lambda's own service principal, which
   * runs CloudFormation's queued async event. Any other invocation Lambda makes on its own behalf
   * would pass too. This provider is not one of the record-integrity watched functions, so a code
   * change to it raises no alarm.
   */
  private providerInvokeBoundary(provider: lambda.Function): cdk.CfnResource {
    return new cdk.CfnResource(this, 'ProviderResourcePolicy', {
      type: 'AWS::Lambda::ResourcePolicy',
      properties: {
        ResourceArn: provider.functionArn,
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [{
            Sid: 'OnlyTheDeployment',
            Effect: 'Deny',
            Principal: '*',
            Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
            Resource: provider.functionArn,
            Condition: {
              ArnNotEquals: { 'aws:PrincipalArn': cloudFormationExecutionRoleArn(this) },
              StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
            },
          }],
        },
      },
    });
  }

  private addHarnessRuntimePolicies(role: iam.Role, config: AppConfig): void {
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'ManagedImagePublicToken',
      actions: ['ecr-public:GetAuthorizationToken', 'sts:GetServiceBearerToken'],
      resources: ['*'],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessXRayAccess',
      actions: [
        'xray:GetSamplingRules',
        'xray:GetSamplingTargets',
        'xray:PutTelemetryRecords',
        'xray:PutTraceSegments',
      ],
      resources: ['*'],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessLogGroupAccess',
      actions: ['logs:CreateLogGroup', 'logs:DescribeLogStreams'],
      resources: [
        `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
      ],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessLogStreamAccess',
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`,
      ],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessLogDiscovery',
      // No logs:PutResourcePolicy: with it this role could let X-Ray write any log group, the
      // evaluation result group included (H-EVALUATION-RESULT-001). Without it the runtime's own
      // attempt to open its group is refused, and its spans reach `aws/spans` through
      // ObservabilityStack's XRaySpanDelivery, which exists only with ADLYTICS_TRANSACTION_SEARCH
      // on (ADR-009; dev acceptance 2026-10-02).
      actions: ['logs:DescribeLogGroups'],
      resources: ['*'],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessMetrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'HarnessWorkloadIdentity',
      actions: [
        'bedrock-agentcore:GetWorkloadAccessToken',
        'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
      ],
      resources: [
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/harness_${harnessName(config.envName)}-*`,
      ],
    }));
    this.addGatewayTokenExchangePolicies(role, config);
  }

  /**
   * ADR-077 S4: what the Harness needs because its Gateway tool exchanges the user's Harness token
   * at the token service instead of signing with SigV4.
   *
   * Probe P2 showed AgentCore Identity reads the agent client secret as this role, which is why
   * the secret's resource policy names it (TokenServiceStack).
   */
  private addGatewayTokenExchangePolicies(role: iam.Role, config: AppConfig): void {
    const agentClientSecretArn = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'agentClientSecretArn'),
    );
    const agentcoreArn = (resource: string): string =>
      `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:${resource}`;
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'ExchangeForGatewayToken',
      actions: ['bedrock-agentcore:GetResourceOauth2Token'],
      resources: [
        agentcoreArn('token-vault/default'),
        agentcoreArn(
          `token-vault/default/oauth2credentialprovider/${tokenCredentialProviderName(config.envName)}`,
        ),
        agentcoreArn('workload-identity-directory/default'),
        agentcoreArn(
          `workload-identity-directory/default/workload-identity/harness_${harnessName(config.envName)}-*`,
        ),
      ],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadAgentClientSecret',
      actions: ['secretsmanager:GetSecretValue'],
      resources: [agentClientSecretArn],
    }));
  }
}
