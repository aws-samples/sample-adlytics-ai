// SPDX-License-Identifier: MIT-0
/**
 * Render every IAM policy document the way IAM counts it, and check each role against IAM's size
 * limits with headroom.
 *
 * Limits (AWS IAM quotas page, https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html,
 * [verified 2026-10-10]):
 * - the aggregate of every inline policy on a role cannot exceed 10,240 characters;
 * - each customer-managed policy cannot exceed 6,144 characters;
 * - IAM does not count white space toward either limit;
 * - a role holds at most 20 managed policies by default (adjustable to 25).
 *
 * A synthesized template holds intrinsics (`Fn::Join`, `Ref`, `Fn::GetAtt`, SSM parameter
 * references), not the strings IAM receives, so its JSON length is not the size IAM checks, and
 * the difference runs either way: a short `Ref` becomes a long ARN, and a `Fn::Join` wrapper
 * disappears. `PolicyRenderer` substitutes each intrinsic with
 * the string CloudFormation passes to IAM:
 * - pseudo parameters from the deployment context (partition `aws`, the stack's region and the
 *   12-digit account);
 * - a resource reference from the resource's own name property, rendered into its ARN, URL or id
 *   by type (`REF_SHAPES`, `ATTRIBUTE_SHAPES`);
 * - an SSM parameter reference (`AWS::SSM::Parameter::Value<String>`, how stacks here share
 *   values) by resolving the producing stack's `AWS::SSM::Parameter` of that name, in that
 *   stack's context.
 * Where CloudFormation or the service generates part of the value (a generated physical name, a
 * KMS key id, a secret's random suffix, an AgentCore id), the renderer uses a placeholder of the
 * documented or observed length, marked as an estimate in the shape tables. Anything else (an
 * unknown resource type or attribute, `Fn::Sub`, `Fn::If`, an unresolved SSM parameter) throws,
 * naming what it could not render: an estimate that silently skipped a value would under-count.
 */

/** IAM's aggregate inline-policy limit per role, in characters. */
export const ROLE_INLINE_LIMIT = 10_240;
/** IAM's per-document limit for a customer-managed policy, in characters. */
export const MANAGED_POLICY_LIMIT = 6_144;
/** IAM's default number of managed policies attached to one role. */
export const MANAGED_POLICIES_PER_ROLE = 20;
/**
 * The share of each limit kept free. A role at the limit fails the deploy that adds its next
 * statement, and the next statement is not small: the Ingestion role's statements render 95 to
 * 816 characters each (test synth, 2026-10-10), and a new path usually adds several. A tenth of the inline limit (1,024 characters) fits about
 * three typical statements, and also absorbs the placeholder lengths the renderer estimates.
 */
export const HEADROOM_FRACTION = 0.1;
export const ROLE_INLINE_THRESHOLD = Math.floor(ROLE_INLINE_LIMIT * (1 - HEADROOM_FRACTION));
export const MANAGED_POLICY_THRESHOLD = Math.floor(MANAGED_POLICY_LIMIT * (1 - HEADROOM_FRACTION));
/**
 * Logical ids of CDK overflow policies that are expected. Empty: CDK moves statements that pass
 * its own size estimate out of a role's default policy into `<Role>OverflowPolicy<n>`, a managed
 * policy no stack declares, which hides how close the role is to the limit. A grant that needs
 * room goes into an explicit, named policy instead.
 */
export const EXPECTED_OVERFLOW_POLICIES: readonly string[] = [];
const OVERFLOW_POLICY = /OverflowPolicy\d+[0-9A-F]{8}$/;

type Json = unknown;
interface Resource {
  Type: string;
  Properties?: Record<string, Json>;
}
interface Template {
  Resources?: Record<string, Resource>;
  Parameters?: Record<string, { Type?: string; Default?: Json }>;
}

/** Where each stack deploys: what pseudo parameters and generated names render as. */
export interface DeploymentContext {
  readonly account: string;
  readonly partition: string;
  readonly regionOf: (stack: string) => string;
  readonly stackNameOf: (stack: string) => string;
}

/** The characters IAM counts: the document as JSON with every white-space character removed. */
export function documentSize(document: Json): number {
  return JSON.stringify(document).replace(/\s/g, '').length;
}

/** Placeholder text of a given length, for a value the service generates. */
function generated(length: number): string {
  return 'X'.repeat(length);
}

const NAME_KEYS: Record<string, string> = {
  'AWS::BedrockAgentCore::Evaluator': 'EvaluatorName',
  'AWS::BedrockAgentCore::Gateway': 'Name',
  'AWS::BedrockAgentCore::Memory': 'Name',
  'AWS::Bedrock::Guardrail': 'Name',
  'AWS::Cognito::UserPool': 'UserPoolName',
  'AWS::DynamoDB::Table': 'TableName',
  'AWS::Events::EventBus': 'Name',
  'AWS::IAM::Role': 'RoleName',
  'AWS::Lambda::Function': 'FunctionName',
  'AWS::Logs::LogGroup': 'LogGroupName',
  'AWS::S3::Bucket': 'BucketName',
  'AWS::SNS::Topic': 'TopicName',
  'AWS::SQS::Queue': 'QueueName',
  'AWS::SSM::Parameter': 'Name',
  // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
  'AWS::SecretsManager::Secret': 'Name',
  'AWS::StepFunctions::StateMachine': 'StateMachineName',
};

interface Named {
  readonly name: string;
  readonly region: string;
  readonly account: string;
  readonly partition: string;
}

/** What `Ref` returns for a resource, by type (CloudFormation return values). */
const REF_SHAPES: Record<string, (n: Named) => string> = {
  'AWS::DynamoDB::Table': (n) => n.name,
  'AWS::Events::EventBus': (n) => n.name,
  'AWS::IAM::Role': (n) => n.name,
  'AWS::Lambda::Function': (n) => n.name,
  'AWS::Logs::LogGroup': (n) => n.name,
  'AWS::S3::Bucket': (n) => n.name,
  'AWS::SSM::Parameter': (n) => n.name,
  'AWS::SQS::Queue': (n) => `https://sqs.${n.region}.amazonaws.com/${n.account}/${n.name}`,
  'AWS::SNS::Topic': (n) => `arn:${n.partition}:sns:${n.region}:${n.account}:${n.name}`,
  // Estimate: Secrets Manager appends a hyphen and six random characters to the name.
  'AWS::SecretsManager::Secret': (n) =>
    `arn:${n.partition}:secretsmanager:${n.region}:${n.account}:secret:${n.name}-${generated(6)}`,
  'AWS::StepFunctions::StateMachine': (n) =>
    `arn:${n.partition}:states:${n.region}:${n.account}:stateMachine:${n.name}`,
  // Estimate: a key id is a 36-character UUID.
  'AWS::KMS::Key': () => generated(36),
  // Estimate: a user pool id is `<region>_` and nine characters.
  'AWS::Cognito::UserPool': (n) => `${n.region}_${generated(9)}`,
};

/** Estimate: AgentCore ids are the resource name, a hyphen and ten characters. */
const agentCoreId = (n: Named): string => `${n.name}-${generated(10)}`;

/** What `Fn::GetAtt` returns, by `<type>.<attribute>`. */
const ATTRIBUTE_SHAPES: Record<string, (n: Named) => string> = {
  'AWS::DynamoDB::Table.Arn': (n) => `arn:${n.partition}:dynamodb:${n.region}:${n.account}:table/${n.name}`,
  // Estimate: a stream label is a 23-character timestamp.
  'AWS::DynamoDB::Table.StreamArn': (n) =>
    `arn:${n.partition}:dynamodb:${n.region}:${n.account}:table/${n.name}/stream/${generated(23)}`,
  'AWS::IAM::Role.Arn': (n) => `arn:${n.partition}:iam::${n.account}:role/${n.name}`,
  'AWS::KMS::Key.Arn': (n) => `arn:${n.partition}:kms:${n.region}:${n.account}:key/${generated(36)}`,
  'AWS::Lambda::Function.Arn': (n) => `arn:${n.partition}:lambda:${n.region}:${n.account}:function:${n.name}`,
  'AWS::Logs::LogGroup.Arn': (n) => `arn:${n.partition}:logs:${n.region}:${n.account}:log-group:${n.name}:*`,
  'AWS::S3::Bucket.Arn': (n) => `arn:${n.partition}:s3:::${n.name}`,
  'AWS::SNS::Topic.TopicArn': (n) => `arn:${n.partition}:sns:${n.region}:${n.account}:${n.name}`,
  'AWS::SQS::Queue.Arn': (n) => `arn:${n.partition}:sqs:${n.region}:${n.account}:${n.name}`,
  'AWS::SQS::Queue.QueueName': (n) => n.name,
  'AWS::SQS::Queue.QueueUrl': (n) => `https://sqs.${n.region}.amazonaws.com/${n.account}/${n.name}`,
  'AWS::StepFunctions::StateMachine.Arn': (n) =>
    `arn:${n.partition}:states:${n.region}:${n.account}:stateMachine:${n.name}`,
  'AWS::Cognito::UserPool.Arn': (n) =>
    `arn:${n.partition}:cognito-idp:${n.region}:${n.account}:userpool/${n.region}_${generated(9)}`,
  'AWS::BedrockAgentCore::Evaluator.EvaluatorArn': (n) =>
    `arn:${n.partition}:bedrock-agentcore:${n.region}:${n.account}:evaluator/${agentCoreId(n)}`,
  'AWS::BedrockAgentCore::Evaluator.EvaluatorId': agentCoreId,
  'AWS::BedrockAgentCore::Gateway.GatewayArn': (n) =>
    `arn:${n.partition}:bedrock-agentcore:${n.region}:${n.account}:gateway/${agentCoreId(n)}`,
  'AWS::BedrockAgentCore::Gateway.GatewayIdentifier': agentCoreId,
  'AWS::BedrockAgentCore::Gateway.GatewayUrl': (n) =>
    `https://${agentCoreId(n)}.gateway.bedrock-agentcore.${n.region}.amazonaws.com/mcp`,
  'AWS::BedrockAgentCore::Memory.MemoryArn': (n) =>
    `arn:${n.partition}:bedrock-agentcore:${n.region}:${n.account}:memory/${agentCoreId(n)}`,
  'AWS::BedrockAgentCore::Memory.MemoryId': agentCoreId,
  // Estimate: a guardrail id is twelve characters.
  'AWS::Bedrock::Guardrail.GuardrailArn': (n) =>
    `arn:${n.partition}:bedrock:${n.region}:${n.account}:guardrail/${generated(12)}`,
  'AWS::Bedrock::Guardrail.GuardrailId': () => generated(12),
};

/** Substitutes intrinsics with the strings CloudFormation passes to IAM. */
export class PolicyRenderer {
  private readonly parameters = new Map<string, { stack: string; value: Json }>();

  constructor(
    private readonly templates: Readonly<Record<string, Template>>,
    private readonly context: DeploymentContext,
  ) {
    for (const [stack, template] of Object.entries(templates)) {
      for (const resource of Object.values(template.Resources ?? {})) {
        const name = resource.Properties?.Name;
        if (resource.Type === 'AWS::SSM::Parameter' && typeof name === 'string') {
          this.parameters.set(name, { stack, value: resource.Properties?.Value });
        }
      }
    }
  }

  render(stack: string, value: Json): Json {
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return value;
    }
    if (Array.isArray(value)) return value.map((item) => this.render(stack, item));
    if (value === null || typeof value !== 'object') return value;
    const entries = Object.entries(value as Record<string, Json>);
    const [only] = entries;
    if (entries.length === 1 && only !== undefined) {
      const [key, argument] = only;
      if (key === 'Ref') return this.ref(stack, String(argument));
      if (key === 'Fn::GetAtt') return this.getAtt(stack, argument);
      if (key === 'Fn::Join') return this.join(stack, argument);
      if (key.startsWith('Fn::')) throw new Error(`${stack}: cannot render ${key}`);
    }
    return Object.fromEntries(entries.map(([key, item]) => [key, this.render(stack, item)]));
  }

  private resource(stack: string, logicalId: string): Resource {
    const resource = this.templates[stack]?.Resources?.[logicalId];
    if (resource === undefined) throw new Error(`${stack}: no resource ${logicalId}`);
    return resource;
  }

  private named(stack: string, logicalId: string): Named {
    const resource = this.resource(stack, logicalId);
    const key = NAME_KEYS[resource.Type];
    const declared = key === undefined ? undefined : resource.Properties?.[key];
    // Estimate: CloudFormation generates `<stack>-<logical id>-<12 characters>`, at most 64.
    const name =
      declared === undefined
        ? `${`${this.context.stackNameOf(stack)}-${logicalId}`.slice(0, 51)}-${generated(12)}`
        : this.render(stack, declared);
    if (typeof name !== 'string') throw new Error(`${stack}: ${logicalId} name is not a string`);
    return {
      name,
      region: this.context.regionOf(stack),
      account: this.context.account,
      partition: this.context.partition,
    };
  }

  private ref(stack: string, target: string): Json {
    switch (target) {
      case 'AWS::Partition':
        return this.context.partition;
      case 'AWS::Region':
        return this.context.regionOf(stack);
      case 'AWS::AccountId':
        return this.context.account;
      case 'AWS::URLSuffix':
        return 'amazonaws.com';
      default:
        break;
    }
    const template = this.templates[stack];
    const resource = template?.Resources?.[target];
    if (resource !== undefined) {
      const shape = REF_SHAPES[resource.Type];
      if (shape === undefined) throw new Error(`${stack}: cannot render Ref of ${resource.Type}`);
      return shape(this.named(stack, target));
    }
    const parameter = template?.Parameters?.[target];
    if (parameter?.Type === 'AWS::SSM::Parameter::Value<String>' && typeof parameter.Default === 'string') {
      const produced = this.parameters.get(parameter.Default);
      if (produced === undefined) {
        throw new Error(`${stack}: no stack produces SSM parameter ${parameter.Default}`);
      }
      return this.render(produced.stack, produced.value);
    }
    throw new Error(`${stack}: cannot render Ref ${target}`);
  }

  private getAtt(stack: string, argument: Json): Json {
    const [logicalId, attribute] = Array.isArray(argument)
      ? argument.map(String)
      : String(argument).split('.', 2);
    if (logicalId === undefined || attribute === undefined) {
      throw new Error(`${stack}: malformed Fn::GetAtt ${JSON.stringify(argument)}`);
    }
    const type = this.resource(stack, logicalId).Type;
    const shape = ATTRIBUTE_SHAPES[`${type}.${attribute}`];
    if (shape === undefined) throw new Error(`${stack}: cannot render Fn::GetAtt ${type}.${attribute}`);
    return shape(this.named(stack, logicalId));
  }

  private join(stack: string, argument: Json): string {
    if (!Array.isArray(argument) || typeof argument[0] !== 'string' || !Array.isArray(argument[1])) {
      throw new Error(`${stack}: malformed Fn::Join ${JSON.stringify(argument)}`);
    }
    const separator = argument[0];
    return (argument[1] as Json[])
      .map((part) => {
        const rendered = this.render(stack, part);
        if (typeof rendered !== 'string') {
          throw new Error(`${stack}: Fn::Join part renders to ${JSON.stringify(rendered)}`);
        }
        return rendered;
      })
      .join(separator);
  }
}

/** One role's policies, rendered and measured. */
export interface RoleBudget {
  readonly role: string;
  readonly inline: readonly { policy: string; size: number }[];
  readonly managed: readonly { policy: string; size: number }[];
  /** Managed policies attached by ARN on the role itself (AWS-managed or imported). */
  readonly attachedByArn: number;
  readonly inlineTotal: number;
}

/**
 * One customer-managed policy document, measured on its own: IAM's per-document limit applies
 * whether the policy attaches to a role, a group, a user or nothing yet.
 */
export interface ManagedPolicySize {
  readonly policy: string;
  readonly size: number;
  /** The roles it attaches to, as role budget keys; empty when `Roles` names none. */
  readonly roles: readonly string[];
}

/** Every policy in `templates`, measured: role budgets, managed documents, and unmeasured inline policies. */
interface Measurement {
  readonly budgets: RoleBudget[];
  readonly managed: ManagedPolicySize[];
  /** `AWS::IAM::Policy` resources that name no role, so no role aggregate counts them. */
  readonly inlineWithoutRole: string[];
}

/** Measure every role's inline and managed policies across all `templates`. */
export function roleBudgets(
  templates: Readonly<Record<string, Template>>,
  context: DeploymentContext,
): RoleBudget[] {
  return measure(templates, context).budgets;
}

/** Measure every `AWS::IAM::ManagedPolicy` document across all `templates`, attached or not. */
export function managedPolicySizes(
  templates: Readonly<Record<string, Template>>,
  context: DeploymentContext,
): ManagedPolicySize[] {
  return measure(templates, context).managed;
}

function measure(templates: Readonly<Record<string, Template>>, context: DeploymentContext): Measurement {
  const renderer = new PolicyRenderer(templates, context);
  const managedDocuments: ManagedPolicySize[] = [];
  const inlineWithoutRole: string[] = [];
  const byName = new Map<string, string>();
  const budgets = new Map<
    string,
    { inline: { policy: string; size: number }[]; managed: { policy: string; size: number }[]; byArn: number }
  >();
  const budget = (key: string) => {
    let entry = budgets.get(key);
    if (entry === undefined) {
      entry = { inline: [], managed: [], byArn: 0 };
      budgets.set(key, entry);
    }
    return entry;
  };
  for (const [stack, template] of Object.entries(templates)) {
    for (const [logicalId, resource] of Object.entries(template.Resources ?? {})) {
      if (resource.Type !== 'AWS::IAM::Role') continue;
      const key = `${stack}/${logicalId}`;
      const name = renderer.render(stack, { Ref: logicalId });
      if (typeof name === 'string') byName.set(name, key);
      const entry = budget(key);
      for (const policy of (resource.Properties?.Policies ?? []) as Record<string, Json>[]) {
        entry.inline.push({
          policy: `${key}#${String(policy.PolicyName)}`,
          size: documentSize(renderer.render(stack, policy.PolicyDocument)),
        });
      }
      entry.byArn += ((resource.Properties?.ManagedPolicyArns ?? []) as Json[]).length;
    }
  }
  for (const [stack, template] of Object.entries(templates)) {
    for (const [logicalId, resource] of Object.entries(template.Resources ?? {})) {
      const inline = resource.Type === 'AWS::IAM::Policy';
      if (!inline && resource.Type !== 'AWS::IAM::ManagedPolicy') continue;
      const size = documentSize(renderer.render(stack, resource.Properties?.PolicyDocument));
      const roles = (resource.Properties?.Roles ?? []) as Json[];
      const attachedTo: string[] = [];
      if (!inline) managedDocuments.push({ policy: `${stack}/${logicalId}`, size, roles: attachedTo });
      else if (roles.length === 0) inlineWithoutRole.push(`${stack}/${logicalId}`);
      for (const role of roles) {
        const local =
          typeof role === 'object' && role !== null && 'Ref' in role
            ? `${stack}/${String((role as { Ref: Json }).Ref)}`
            : undefined;
        const rendered = renderer.render(stack, role);
        const key =
          local !== undefined && budgets.has(local)
            ? local
            : (byName.get(String(rendered)) ?? `imported role ${String(rendered)}`);
        const entry = budget(key);
        (inline ? entry.inline : entry.managed).push({ policy: `${stack}/${logicalId}`, size });
        attachedTo.push(key);
      }
    }
  }
  const roleBudgetList = [...budgets.entries()]
    .map(([role, entry]) => ({
      role,
      inline: entry.inline,
      managed: entry.managed,
      attachedByArn: entry.byArn,
      inlineTotal: entry.inline.reduce((sum, item) => sum + item.size, 0),
    }))
    .sort((a, b) => b.inlineTotal - a.inlineTotal || a.role.localeCompare(b.role));
  return { budgets: roleBudgetList, managed: managedDocuments, inlineWithoutRole };
}

/** CDK overflow policies present in `templates` and not in `EXPECTED_OVERFLOW_POLICIES`. */
export function unexpectedOverflowPolicies(templates: Readonly<Record<string, Template>>): string[] {
  return Object.entries(templates).flatMap(([stack, template]) =>
    Object.entries(template.Resources ?? {})
      .filter(
        ([logicalId, resource]) =>
          (resource.Type === 'AWS::IAM::ManagedPolicy' || resource.Type === 'AWS::IAM::Policy') &&
          OVERFLOW_POLICY.test(logicalId) &&
          !EXPECTED_OVERFLOW_POLICIES.includes(`${stack}/${logicalId}`),
      )
      .map(([logicalId]) => `${stack}/${logicalId}`),
  );
}

/** One line per role or policy over its threshold, plus every unexpected overflow policy. */
export function policySizeFindings(
  templates: Readonly<Record<string, Template>>,
  context: DeploymentContext,
): string[] {
  const findings: string[] = [];
  const measured = measure(templates, context);
  for (const budget of measured.budgets) {
    if (budget.inlineTotal > ROLE_INLINE_THRESHOLD) {
      const parts = budget.inline.map((item) => `${item.policy}=${item.size}`).join(', ');
      findings.push(
        `${budget.role}: inline policies total ${budget.inlineTotal} characters, over the ` +
          `${ROLE_INLINE_THRESHOLD}-character threshold (limit ${ROLE_INLINE_LIMIT}); ${parts}`,
      );
    }
    const attached = budget.managed.length + budget.attachedByArn;
    if (attached > MANAGED_POLICIES_PER_ROLE) {
      findings.push(
        `${budget.role}: ${attached} managed policies attached, over IAM's default ` +
          `${MANAGED_POLICIES_PER_ROLE} per role`,
      );
    }
  }
  // Each managed document once, whatever it attaches to: a document with no `Roles` (attached
  // to a group or user, or not yet attached) is held to the same per-document limit.
  for (const item of measured.managed) {
    if (item.size > MANAGED_POLICY_THRESHOLD) {
      findings.push(
        `${item.policy}: managed policy is ${item.size} characters, over the ` +
          `${MANAGED_POLICY_THRESHOLD}-character threshold (limit ${MANAGED_POLICY_LIMIT})`,
      );
    }
  }
  // An inline policy on no role is in no role aggregate, and this check measures no group or
  // user aggregate, so it is refused rather than passed unmeasured.
  for (const policy of measured.inlineWithoutRole) {
    findings.push(`${policy}: inline policy names no role, so no measured aggregate counts it`);
  }
  for (const overflow of unexpectedOverflowPolicies(templates)) {
    findings.push(`${overflow}: CDK overflow policy; move the role's grants into a named policy`);
  }
  return findings;
}
