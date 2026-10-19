// SPDX-License-Identifier: MIT-0
/**
 * ADR-079: one family's evaluation enablement gate, `Custom::AgentCoreOnlineEvaluation`.
 *
 * The gate provider is the only owner of the family's online-evaluation configuration and the
 * only writer of its release record. This construct declares what the gate needs and nothing it
 * decides: the execution role the evaluation service assumes, the arm64 Python provider and its
 * role, and the custom resource carrying the whole desired state as one JSON string, `GateSpec`,
 * whose members are exactly `src/domain/evaluation/gate.py`'s `_SPEC_MEMBERS`.
 */
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  EvaluationFamily,
  evaluationExecutionRoleName,
  evaluationGateProviderFunctionName,
  evaluationGateProviderRoleName,
  evaluationReleaseParameterName,
  evaluationResultIngressV3FunctionName,
  evaluationResultSubscriptionName,
  onlineEvaluationGateName,
  ParamKey,
  paramPath,
} from '../../config';
import { modelInvokeStatements } from '../../bedrock-model-access';
import { requestTagCondition, standardTags, tagOwnResourceCondition } from '../../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../../telemetry';
import { cloudFormationExecutionRoleArn } from './record-integrity';

/** The parameter `EvaluationEvidenceStack` publishes each family's owned result group name under. */
export const RESULT_GROUP_PARAMETER: Readonly<Record<EvaluationFamily, ParamKey>> = {
  v4: 'evaluationV4ResultLogGroupName',
};

/** The custom resource type the gate provider serves. */
export const EVALUATION_GATE_RESOURCE_TYPE = 'Custom::AgentCoreOnlineEvaluation';

/** `gate.py` `GATE_SPEC_SCHEMA`. */
export const GATE_SPEC_SCHEMA = 'EvaluationGateSpec.v1';

/** `gate.py` `SPEC_PROPERTY` and `DIGEST_PROPERTY`. */
export const GATE_SPEC_PROPERTY = 'GateSpec';
export const GATE_DIGEST_PROPERTY = 'SpecDigest';

/** The service principal that assumes the execution role and receives it through PassRole. */
const AGENTCORE_SERVICE = 'bedrock-agentcore.amazonaws.com';

/** One evaluator the gate configures, with the exact instructions its `Evaluator` was given. */
export interface GateEvaluator {
  readonly evaluator: agentcore.Evaluator;
  readonly evaluatorName: string;
  readonly revision: string;
  readonly modelId: string;
  /** The exact `instructions` string passed to the evaluator; its SHA-256 is the prompt digest. */
  readonly instructions: string;
  /** The key naming this evaluator in the calibration corpus's `expected` maps. */
  readonly corpusDimension: string;
}

export interface EvaluationGateProps {
  readonly config: AppConfig;
  readonly family: EvaluationFamily;
  readonly description: string;
  readonly executionRequested: boolean;
  readonly evaluators: readonly GateEvaluator[];
  /** The family's fictional calibration corpus (`validation/evaluation-<family>-cases.json`). */
  readonly corpusPath: string;
  /** Directory holding the staged Python source tree (the provider's code). */
  readonly sourceAssetPath: string;
}

/** One calibration case in `gate.py`'s `_CASE_MEMBERS` shape. */
export interface GateCalibrationCase {
  readonly name: string;
  /** The case's calibration kind, verbatim; `gate.py` owns the closed set and the per-evaluator rule. */
  readonly kind: string;
  readonly question: string;
  readonly grounded_context: string;
  readonly candidate: string;
  readonly expected: Record<string, string | string[]>;
}

interface CorpusCase {
  readonly name: string;
  readonly kind?: unknown;
  readonly question: string;
  readonly trusted_assertions: readonly string[];
  /**
   * The exact grounded context the evaluator reads, when a case must exercise the production
   * document (GroundedEvaluationContext.v2, ADR-083) rather than a bulleted list of assertions.
   */
  readonly grounded_context?: string;
  readonly candidate: string;
  readonly expected: Record<string, string | string[]>;
}

/** SHA-256 hex of text as UTF-8, the digest form the release record carries. */
export function sha256Hex(text: string): string {
  return crypto.createHash('sha256').update(text, 'utf-8').digest('hex');
}

/** The evaluator name the service accepts: a letter, then at most 47 letters, digits, or underscores. */
export const EVALUATOR_NAME_PATTERN = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** An evaluator's name and the revision the gate, the release record, and each verdict carry. */
export interface EvaluatorIdentity {
  readonly evaluatorName: string;
  readonly revision: string;
}

/**
 * An evaluator's identity, derived from exactly what it judges with. The revision is the first
 * eight hex digits of SHA-256 over the instructions and the model ID, and it ends the name, so a
 * changed prompt or model is a new evaluator name: CloudFormation replaces the evaluator (a new
 * ID) rather than updating the live one in place, and a verdict can never carry the provenance of
 * the definition it was not produced by. A name the service would refuse fails synth.
 */
export function evaluatorIdentity(
  envName: string,
  dimension: string,
  instructions: string,
  modelId: string,
): EvaluatorIdentity {
  const revision = sha256Hex(`${instructions}\n${modelId}`).slice(0, 8);
  const evaluatorName = `adlytics_${envName.replace(/-/g, '_')}_${dimension}_${revision}`;
  if (!EVALUATOR_NAME_PATTERN.test(evaluatorName)) {
    throw new Error(
      `Evaluator name ${evaluatorName} (${evaluatorName.length} characters) does not match `
      + `${EVALUATOR_NAME_PATTERN.source}; shorten the environment name.`,
    );
  }
  return { evaluatorName, revision };
}

/**
 * Read a corpus and rename each expectation from its corpus dimension to the evaluator name the
 * gate calibrates. An expectation naming no configured evaluator, or a case without a `kind`,
 * fails synth rather than going unjudged; the provider enforces the closed kind set and the
 * per-evaluator kind and case rules.
 */
export function loadCalibration(
  corpusPath: string,
  evaluators: readonly Pick<GateEvaluator, 'corpusDimension' | 'evaluatorName'>[],
): GateCalibrationCase[] {
  const document = JSON.parse(fs.readFileSync(corpusPath, 'utf-8')) as { cases?: unknown };
  if (!Array.isArray(document.cases)) {
    throw new Error(`${corpusPath} has no cases list.`);
  }
  const byDimension = new Map(evaluators.map((e) => [e.corpusDimension, e.evaluatorName]));
  return (document.cases as CorpusCase[]).map((entry) => ({
    name: entry.name,
    kind: caseKind(corpusPath, entry),
    question: entry.question,
    grounded_context:
      entry.grounded_context ??
      entry.trusted_assertions.map((assertion) => `- ${assertion}`).join('\n'),
    candidate: entry.candidate,
    expected: Object.fromEntries(
      Object.entries(entry.expected).map(([dimension, labels]) => {
        const evaluatorName = byDimension.get(dimension);
        if (evaluatorName === undefined) {
          throw new Error(`${corpusPath} case ${entry.name} expects unknown dimension ${dimension}.`);
        }
        return [evaluatorName, labels];
      }),
    ),
  }));
}

function caseKind(corpusPath: string, entry: CorpusCase): string {
  if (typeof entry.kind !== 'string' || entry.kind.length === 0) {
    throw new Error(`${corpusPath} case ${entry.name} has no calibration kind.`);
  }
  return entry.kind;
}

/**
 * The execution role, the provider, and the gate custom resource for one family.
 *
 * Deploy order (ADR-079): the owned result group comes from `EvaluationEvidenceStack` and the
 * ingress subscription filter from `EvaluationResultLinkV3Stack`, both earlier in DEPLOY_ORDER, so
 * the gate's readback of them runs against deployed state.
 */
export class EvaluationGate extends Construct {
  public readonly resource: cdk.CustomResource;
  public readonly executionRole: iam.Role;
  public readonly provider: lambda.Function;

  constructor(scope: Construct, id: string, props: EvaluationGateProps) {
    super(scope, id);
    const { config, family } = props;
    const stack = cdk.Stack.of(this);
    const read = (key: ParamKey): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const inputLogGroup = read('evaluationInputLogGroupName');
    const inputServiceName = read('evaluationInputServiceName');
    // The group EvaluationEvidenceStack created and published, read rather than recomputed, so
    // the gate binds and grants exactly the group that exists.
    const resultGroupParameter = RESULT_GROUP_PARAMETER[family];
    const resultGroupName = read(resultGroupParameter);
    const logGroupArn = (name: string): string => stack.formatArn({
      service: 'logs',
      resource: 'log-group',
      resourceName: name,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    const resultGroupArn = logGroupArn(resultGroupName);
    const releaseParameterName = evaluationReleaseParameterName(config.envName, family);
    const releaseParameterArn = stack.formatArn({
      service: 'ssm',
      resource: 'parameter',
      // The name starts with '/', and a parameter ARN is `parameter/<name without the slash>`.
      resourceName: releaseParameterName.slice(1),
    });
    const configName = onlineEvaluationGateName(config.envName, family);
    const ingressFunctionName = evaluationResultIngressV3FunctionName(config.envName);
    const subscriptionName = evaluationResultSubscriptionName(config.envName, family);

    this.executionRole = this.evaluationExecutionRole(
      props,
      configName,
      inputLogGroup,
      logGroupArn,
      resultGroupArn,
    );
    this.provider = this.gateProvider(props, releaseParameterArn, resultGroupArn, configName);
    const providerPolicy = this.providerInvokeBoundary(this.provider);

    const evaluators = props.evaluators.map((entry) => ({
      evaluator_id: entry.evaluator.evaluatorId,
      evaluator_name: entry.evaluatorName,
      revision: entry.revision,
      model_id: entry.modelId,
      prompt_digest: sha256Hex(entry.instructions),
    }));
    const calibration = loadCalibration(props.corpusPath, props.evaluators);
    const spec = {
      schema_version: GATE_SPEC_SCHEMA,
      family,
      config_name: configName,
      description: props.description,
      execution_requested: props.executionRequested,
      execution_role_arn: this.executionRole.roleArn,
      input_log_group_names: [inputLogGroup],
      input_service_names: [inputServiceName],
      sampling_percentage: config.onlineEvaluation.samplingPercentage,
      session_timeout_minutes: config.onlineEvaluation.sessionTimeoutMinutes,
      result_log_group_name: resultGroupName,
      result_log_group_arn: resultGroupArn,
      result_retention_days: Number(config.evaluationResultRetentionDays),
      ingress_subscription_filter_name: subscriptionName,
      ingress_function_arn: stack.formatArn({
        service: 'lambda',
        resource: 'function',
        resourceName: ingressFunctionName,
        arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
      }),
      release_parameter_name: releaseParameterName,
      evaluators,
      calibration,
      // The Tags aspect cannot reach a configuration an API call creates, and the provider role
      // may create only with exactly this set (H-IAM-AGENTCORE-CREATE-001).
      tags: standardTags(config),
    };
    // Every synth-time-static input, so any change to one reruns the gate even where the rendered
    // GateSpec would differ only inside a deploy-time token.
    const specDigest = sha256Hex(JSON.stringify({
      family,
      config_name: configName,
      description: props.description,
      execution_requested: props.executionRequested,
      execution_role_name: evaluationExecutionRoleName(config.envName, family),
      evaluators: evaluators.map((entry) => ({
        evaluator_name: entry.evaluator_name,
        revision: entry.revision,
        model_id: entry.model_id,
        prompt_digest: entry.prompt_digest,
      })),
      calibration,
      sampling_percentage: config.onlineEvaluation.samplingPercentage,
      session_timeout_minutes: config.onlineEvaluation.sessionTimeoutMinutes,
      // The name itself is a deploy-time token; its parameter path is the static input, and a
      // changed value changes the rendered GateSpec.
      result_log_group_parameter: paramPath(config.envName, resultGroupParameter),
      result_retention_days: Number(config.evaluationResultRetentionDays),
      ingress_subscription_filter_name: subscriptionName,
      ingress_function_name: ingressFunctionName,
      release_parameter_name: releaseParameterName,
      tags: standardTags(config),
    }));

    this.resource = new cdk.CustomResource(this, 'OnlineEvaluationGate', {
      serviceToken: this.provider.functionArn,
      resourceType: EVALUATION_GATE_RESOURCE_TYPE,
      // One JSON string: CloudFormation delivers every property value as text, so a nested
      // structure would arrive with its numbers and booleans stringified.
      properties: {
        [GATE_SPEC_PROPERTY]: stack.toJsonString(spec),
        [GATE_DIGEST_PROPERTY]: specDigest,
      },
      // Above the provider's 15-minute timeout, so a provider that never answers fails the deploy
      // in 20 minutes rather than CloudFormation's default hour.
      serviceTimeout: cdk.Duration.minutes(20),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // Both roles' policies exist before the gate runs and outlive its delete. A dependency on a
    // role covers its default policy, which is a child construct.
    this.resource.node.addDependency(this.executionRole);
    this.resource.node.addDependency(this.provider.role!);
    // The provider is invocable only by the deployment before the gate first runs, and until
    // after its delete.
    this.resource.node.addDependency(providerPolicy);
  }

  /**
   * The provider acts on any callback it is handed, so who may invoke it is who may enable
   * evaluation and write the release record under the one role the release-record rule exempts.
   * This Deny confines that to the stack's CloudFormation execution role, which CloudFormation
   * invokes the provider as, and Lambda's own service principal, which runs that queued async
   * event [verified 2026-09-27, the first dev gate deploy and a probe stack]. Any other invocation
   * Lambda makes on its own behalf would pass too; the only one in view, an event-source mapping,
   * needs a source the provider's role cannot read, and creating one raises the function-change
   * alarm (ADR-079). It is the whole resource policy, as on the token service's route exchange: an
   * `AWS::Lambda::Permission` would be a second writer.
   */
  private providerInvokeBoundary(provider: lambda.Function): cdk.CfnResource {
    return new cdk.CfnResource(this, 'ProviderResourcePolicy', {
      type: 'AWS::Lambda::ResourcePolicy',
      properties: {
        // `ResourceArn`, the registry schema's property (see EvaluationResultLinkV3Stack).
        ResourceArn: provider.functionArn,
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [{
            Sid: 'OnlyTheDeployment',
            Effect: 'Deny',
            Principal: '*',
            Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
            Resource: provider.functionArn,
            // CloudFormation invokes asynchronously, and Lambda's service principal then runs the
            // event and is evaluated again; without its exemption every callback is dropped
            // [verified 2026-09-27, the first gate deploy and a dev probe].
            Condition: {
              ArnNotEquals: { 'aws:PrincipalArn': cloudFormationExecutionRoleArn(cdk.Stack.of(this)) },
              StringNotEquals: { 'aws:PrincipalServiceName': 'lambda.amazonaws.com' },
            },
          }],
        },
      },
    });
  }

  /**
   * The role the evaluation service assumes for this family. The trust is the retired
   * CloudFormation configuration L2's, narrowed from any configuration or evaluator in the account
   * to this family's own; the grants are that L2's set with the write narrowed from the reserved
   * `/aws/bedrock-agentcore/evaluations/*` prefix to the owned group alone.
   */
  private evaluationExecutionRole(
    props: EvaluationGateProps,
    configName: string,
    inputLogGroup: string,
    logGroupArn: (name: string) => string,
    resultGroupArn: string,
  ): iam.Role {
    const { config, family } = props;
    const stack = cdk.Stack.of(this);
    const agentcoreArn = (resource: string): string =>
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:${resource}`;
    const role = new iam.Role(this, 'ExecutionRole', {
      // Fixed: the gate spec and its readback name it before the configuration exists.
      roleName: evaluationExecutionRoleName(config.envName, family),
      // Only this family's configuration (its ID is the name plus a service suffix, as the
      // provider's grant assumes) and this family's evaluators: the role is the owned group's
      // only writer, so no other configuration or evaluator in the account may act as it.
      assumedBy: new iam.ServicePrincipal(AGENTCORE_SERVICE, {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': stack.account,
            'aws:ResourceAccount': stack.account,
          },
          ArnLike: {
            'aws:SourceArn': [
              agentcoreArn(`online-evaluation-config/${configName}-*`),
              ...props.evaluators.map((entry) => entry.evaluator.evaluatorArn),
            ],
          },
        },
      }),
      description: `Assumed only by AgentCore evaluation for the ${family} family.`,
    });
    const spans = logGroupArn('aws/spans');
    const input = logGroupArn(inputLogGroup);
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'DescribeLogGroups',
      actions: ['logs:DescribeLogGroups'],
      resources: ['*'],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'QueryEvaluationInput',
      actions: ['logs:StartQuery', 'logs:GetQueryResults'],
      resources: [spans, `${spans}:*`, input, `${input}:*`],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'IndexSpans',
      actions: ['logs:DescribeIndexPolicies', 'logs:PutIndexPolicy'],
      resources: [spans, `${spans}:*`],
    }));
    // The owned group only, and nothing under the reserved prefix. CreateOnlineEvaluationConfig
    // refuses a role without `logs:CreateLogGroup` on the named group even when it exists
    // [verified 2026-09-27, the dev gate deploy's ValidationException]; scoped to this one name,
    // the grant can create nothing else. It does let the service recreate the group at service
    // defaults if the owned group is ever deleted, which lifting its deletion protection and
    // deleting it both alarm on; observability §3.7 owns the response.
    role.addToPolicy(new iam.PolicyStatement({
      sid: 'WriteOwnedResultGroup',
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [resultGroupArn, `${resultGroupArn}:*`],
    }));
    // The evaluator models only (CloudTrail shows this role invoking only them, 2026-10-04): their
    // profiles in this Region, and each profile's foundation model in any Region only through it.
    const modelIds = [...new Set(props.evaluators.map((entry) => entry.modelId))];
    for (const statement of modelInvokeStatements(
      stack,
      'InvokeJudgeModels',
      modelIds,
      ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    )) role.addToPolicy(statement);
    return role;
  }

  /**
   * The arm64 Python function behind the gate. A plain Lambda-backed provider, as the Harness's,
   * rather than `cr.Provider`, whose framework functions are x86 Node.
   */
  private gateProvider(
    props: EvaluationGateProps,
    releaseParameterArn: string,
    resultGroupArn: string,
    configName: string,
  ): lambda.Function {
    const { config, family } = props;
    const stack = cdk.Stack.of(this);
    const functionName = evaluationGateProviderFunctionName(config.envName, family);
    // Declared rather than left to `logRetention`, which provisions an x86_64 singleton function.
    const logGroup = new logs.LogGroup(this, 'ProviderLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, 'ProviderRole', {
      // Fixed: the release-record tamper rule exempts exactly this role by ARN.
      roleName: evaluationGateProviderRoleName(config.envName, family),
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Owns the ${family} online-evaluation configuration and release record.`,
    });
    const configArn = (suffix: string): string =>
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:online-evaluation-config/${suffix}`;
    const evaluatorArns = props.evaluators.map((entry) => entry.evaluator.evaluatorArn);
    const modelIds = [...new Set(props.evaluators.map((entry) => entry.modelId))];
    const statements = [
      new iam.PolicyStatement({
        sid: 'WriteProviderLogs',
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`${logGroup.logGroupArn}:*`],
      }),
      // No resource type; the request-tag keys are the only ones that bound a create (AWS service
      // authorization reference, bedrock-agentcore v1.4, read 2026-10-04). It also lists
      // `aws:ResourceTag`, which cannot bound a resource that does not exist yet. So the create
      // must carry exactly the standard tag set (H-IAM-AGENTCORE-CREATE-001).
      new iam.PolicyStatement({
        sid: 'CreateTheConfiguration',
        actions: ['bedrock-agentcore:CreateOnlineEvaluationConfig'],
        resources: ['*'],
        conditions: requestTagCondition(config),
      }),
      // Tag-on-create: the service authorizes TagResource inside the create, against an ARN
      // CloudTrail does not show, so it cannot be name-scoped. The provider never tags directly.
      new iam.PolicyStatement({
        sid: 'TagWithTheStandardSet',
        actions: ['bedrock-agentcore:TagResource'],
        resources: ['*'],
        conditions: tagOwnResourceCondition(config),
      }),
      new iam.PolicyStatement({
        sid: 'OwnTheConfiguration',
        actions: [
          'bedrock-agentcore:GetOnlineEvaluationConfig',
          'bedrock-agentcore:UpdateOnlineEvaluationConfig',
          'bedrock-agentcore:DeleteOnlineEvaluationConfig',
        ],
        resources: [configArn(`${configName}-*`)],
      }),
      new iam.PolicyStatement({
        sid: 'ReadAndCalibrateEvaluators',
        actions: ['bedrock-agentcore:GetEvaluator', 'bedrock-agentcore:Evaluate'],
        resources: evaluatorArns,
      }),
      // `Evaluate` runs the judge under this role (CloudTrail: invokedBy
      // bedrock-agentcore.amazonaws.com, verified 2026-10-04). Held at the evaluator models: their
      // profiles in this Region, and the profiles' foundation models in any Region only through them.
      ...modelInvokeStatements(stack, 'InvokeEvaluatorModelsForCalibration', modelIds, ['bedrock:InvokeModel']),
      new iam.PolicyStatement({
        sid: 'PassTheExecutionRole',
        actions: ['iam:PassRole'],
        resources: [this.executionRole.roleArn],
        conditions: { StringEquals: { 'iam:PassedToService': AGENTCORE_SERVICE } },
      }),
      new iam.PolicyStatement({
        sid: 'WriteTheReleaseRecord',
        actions: ['ssm:PutParameter', 'ssm:GetParameters', 'ssm:DeleteParameter'],
        resources: [releaseParameterArn],
      }),
      // Before a create, the provider looks the fixed name up so it never touches the release
      // record of a configuration another logical resource owns. A list takes no resource-level
      // scope: the AWS service authorization reference lists no resource type for it [verified
      // 2026-10-02: https://servicereference.us-east-1.amazonaws.com/v1/bedrock-agentcore/bedrock-agentcore.json].
      new iam.PolicyStatement({
        sid: 'FindTheConfigurationByName',
        actions: ['bedrock-agentcore:ListOnlineEvaluationConfigs'],
        resources: ['*'],
      }),
      new iam.PolicyStatement({
        sid: 'ReadBackTheResultGroup',
        actions: ['logs:DescribeSubscriptionFilters', 'logs:GetDataProtectionPolicy', 'logs:GetTransformer'],
        resources: [resultGroupArn, `${resultGroupArn}:*`],
      }),
      // Account-wide describes: these list calls take no resource-level scope.
      new iam.PolicyStatement({
        sid: 'ReadBackAccountLogsControls',
        actions: [
          'logs:DescribeLogGroups',
          'logs:DescribeAccountPolicies',
          'logs:DescribeDeliveryDestinations',
          'logs:DescribeExportTasks',
          'logs:DescribeResourcePolicies',
        ],
        resources: ['*'],
      }),
    ];
    for (const statement of statements) role.addToPolicy(statement);
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, 'dependenciesLayerArn')),
    );
    const provider = new lambda.Function(this, 'Provider', {
      functionName,
      role,
      description: `Evaluation enablement gate for the ${family} family (ADR-079).`,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[config.lambdaArchitecture],
      code: lambda.Code.fromAsset(props.sourceAssetPath),
      handler: 'adapters.jobs.evaluation_gate.handler.handler',
      layers: [dependencies],
      environment: tracingEnvironment(config),
      // 256 MB: a handful of control-plane calls, readbacks, and sequential Evaluate calls; the
      // path waits on AgentCore, so memory buys nothing, and it runs only on deploys.
      memorySize: 256,
      // Lambda's maximum. The handler stops starting work four minutes before it, so a gate still
      // converging or calibrating answers FAILED instead of timing out.
      timeout: cdk.Duration.minutes(15),
      logGroup,
      // CloudFormation invokes the provider asynchronously. A run that times out sends no
      // response, and a Lambda retry of it could then write a release record while the rollback
      // that timeout started deletes or disables the configuration, so there is none.
      retryAttempts: 0,
    });
    grantSpanExport(provider);
    return provider;
  }
}
