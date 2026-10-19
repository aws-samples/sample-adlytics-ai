// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

import {
  type AppConfig,
  gatewayPolicyName,
  policyEngineName,
  toolGatewayName,
} from '../../config';

/**
 * Layer 2 of [ADR-041](../../../../docs/decisions/adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md):
 * a Cedar policy engine evaluating every call that crosses the tool gateway.
 *
 * **What this delivers, and what it turned out not to.** The engine executes at the gateway, outside
 * any code the model can influence, and every tool routes through the gateway — so it is the right
 * place for an authorization decision and a new tool is governed without anyone remembering to add
 * it. What it cannot do here is inspect content: **no `when guardrails` statement can be written
 * against a Lambda target on an MCP gateway**, because every guardrail needs a
 * `context.{input|output}.<field>` data path and a target action declares no context fields at all.
 * [ADR-042](../../../../docs/decisions/adr-042-guardrails-at-the-gateway-do-not-reach-a-lambda-target.md)
 * records the probes, including that declaring an `outputSchema` does not change it.
 *
 * So layer 2 today is **deterministic Cedar authorization, shipped inert**. The base permit is the
 * only statement, `ENFORCE` is unset, and content inspection is carried by layers 1, 3 and 4.
 *
 * **Off by default, and that is not only about region support.** Every tool call costs an
 * `AuthorizeAction` evaluation, so while the only policy is a blanket permit the engine is latency
 * and spend on the critical path for no benefit. It is here because it is the prerequisite for any
 * Cedar policy worth writing and because promoting to `ENFORCE` should be a configuration change
 * rather than a project.
 *
 * **What it does not cover, stated here so it is not assumed.** AgentCore's built-in `shell` and
 * `file_operations` do not traverse the gateway, so nothing here applies to them; they are excluded
 * by `allowedTools` on the harness instead (Conversational Runtime LLD §6, AgentCore platform
 * constraints). `InvokeAgentRuntimeCommand` and its shell variant skip
 * the model loop entirely and are excluded by IAM, as ADR-038 records.
 *
 * **Cedar is hand-written here, and the generator ADR-041 called for is not built.** It was deferred
 * until the engine was known to evaluate our target shape, which was the right order: the shape it
 * evaluates admits one statement, and generating one blanket permit from a declarative source would
 * be machinery around nothing. ADR-042 records generation as reopening when there is a second
 * statement to generate.
 */
export class GatewayPolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GatewayPolicyError';
    Object.setPrototypeOf(this, GatewayPolicyError.prototype);
  }
}

/**
 * Regions where guardrails in policy are supported.
 *
 * `[verified 2026-08-24]` — the availability table in
 * [Guardrails in policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-guardrails-in-policies.html).
 * Every other region in that table is marked unsupported, including Ohio, Oregon, Ireland,
 * Frankfurt, Paris, Mumbai, Singapore, Seoul, Canada, São Paulo and GovCloud West.
 *
 * **A dated list rather than a runtime lookup, because there is no API that answers this.** It
 * will go stale, and the failure mode of a stale list is the safe one: a region AWS has since
 * added is refused at synth with a message naming this constant, which is a build failure someone
 * can act on. The unsafe direction — assuming support and deploying a control that silently does
 * nothing — is the one this exists to prevent.
 *
 * The owning statement of this fact, with the same date and source, is
 * docs/capabilities/conversational-runtime/low-level-design.md §6 (AgentCore platform
 * constraints). Q-01 records `us-east-1` as the supported region; that is narrower than the five
 * listed here rather than wrong about it.
 */
export const POLICY_GUARDRAIL_REGIONS: readonly string[] = [
  'us-east-1', // US East (N. Virginia)
  'eu-west-2', // Europe (London)
  'eu-north-1', // Europe (Stockholm)
  'ap-southeast-2', // Asia Pacific (Sydney)
  'ap-northeast-1', // Asia Pacific (Tokyo)
];

export interface GatewayPolicyOptions {
  readonly config: AppConfig;
  /** The tool gateway. Its `CfnGateway` is reached to set a property the L2 does not render. */
  readonly gateway: agentcore.Gateway;
}

/**
 * Attach a policy engine to the tool gateway, or do nothing when the feature is off.
 *
 * Returns the engine so a caller can publish its identifiers; `undefined` when disabled, which is
 * the state a checkout ships in.
 */
export function attachGatewayPolicyEngine(
  scope: Construct,
  options: GatewayPolicyOptions,
): agentcore.CfnPolicyEngine | undefined {
  const { config, gateway } = options;
  if (!config.gatewayGuardrails.enabled) {
    return undefined;
  }

  assertRegionSupportsPolicyGuardrails(config);

  const engine = new agentcore.CfnPolicyEngine(scope, 'ToolGatewayPolicyEngine', {
    name: policyEngineName(config.envName),
    description: 'Cedar policies evaluated on every call crossing the tool gateway. ADR-041.',
  });

  // Policies before the grant, because the grant names each policy's own ARN and cannot be written
  // without them. Order here is source order only — CloudFormation orders by the references
  // between resources plus the explicit dependency below.
  // The grant before the policies, and the policies after the gateway. That ordering is the one
  // thing here that cannot be rearranged — see `grantEngineAccess` for the cycle it avoids.
  grantEngineAccess(gateway, engine, config);

  basePermitPolicy(scope, engine, config);

  // **No guardrail policy, and its absence is a finding rather than an omission.** A `when
  // guardrails` statement cannot be written against a Lambda target on an MCP gateway: every
  // guardrail requires a `context.{input|output}.<field>` data path, and a target action declares
  // no context fields at all. ADR-042 records what was probed and what it costs.
  attachEngineToGateway(gateway, engine, config);

  return engine;
}

/**
 * Refuse at synth in a region where this control would deploy and do nothing.
 *
 * The same shape as `data.encryption = 'kms_cmk'` in `mixins/storage.ts`, and for the same reason:
 * the failure is otherwise silent and arrives much later as an absence rather than an error. A
 * policy engine in an unsupported region is worse than no policy engine, because every stack
 * reports healthy and the control is simply not running.
 *
 * At the point of use rather than in `lib/config`, following that precedent. Config states what
 * was chosen; the construct that would emit the resource is what knows whether it can.
 *
 * The unsupported-region path has never been exercised against a real deploy — G-10, one
 * environment — so what is verified here is the refusal, not the degradation behind it.
 */
function assertRegionSupportsPolicyGuardrails(config: AppConfig): void {
  if (POLICY_GUARDRAIL_REGIONS.includes(config.region)) {
    return;
  }
  throw new GatewayPolicyError(
    `gatewayGuardrails.enabled is true, but ${config.region} does not support guardrails in ` +
      'AgentCore Policy.\n' +
      `Supported: ${POLICY_GUARDRAIL_REGIONS.join(', ')} [verified 2026-08-24].\n` +
      'Deploying here would create a policy engine that evaluates nothing while every stack ' +
      'reported healthy, so synth refuses instead. Either deploy into a supported region, or ' +
      'set ADLYTICS_GATEWAY_GUARDRAILS=false and let layers 1, 3 and 4 carry the coverage.\n' +
      'If AWS has since added this region, POLICY_GUARDRAIL_REGIONS in ' +
      'infra/lib/constructs/agent/gateway-policy.ts is the list to update.',
  );
}

/**
 * Point the gateway at the engine, through the one property its L2 does not render.
 *
 * **This is the first escape hatch in this repository, so it states its own terms.** `CfnGateway`
 * accepts `policyEngineConfiguration` — `{ arn, mode }` — and the `Gateway` L2 passes exactly
 * eleven properties to it, none of them this one `[verified 2026-08-24, reading the compiled L2's
 * own construction]`. So the property is set on `node.defaultChild`.
 *
 * Three things make that acceptable rather than a workaround:
 *
 * 1. **It is typed.** `CfnGateway` exposes a real setter whose type is the property interface, so
 *    a shape change is a build error. `addPropertyOverride` was available and was not used: it
 *    takes a string path and would turn the same change into a silent no-op.
 * 2. **A test asserts the rendered template carries it.** An escape hatch that quietly stops
 *    applying is worse than not having one, because the control disappears while the code that
 *    appears to install it is still there.
 * 3. **Staying on the L2 is worth it.** The L2 is what creates the service role, renders the
 *    authorizer and validates the gateway name; hand-writing `CfnGateway` to gain one property
 *    would mean re-implementing all of that, and `check-construct-levels` records Gateway as a
 *    module where a stable L2 exists and should be used.
 *
 * `make check-construct-levels` fails if a stable L2 lands for this property, and names ADR-041 —
 * which is the signal to delete this function's body rather than to widen it.
 */
function attachEngineToGateway(
  gateway: agentcore.Gateway,
  engine: agentcore.CfnPolicyEngine,
  config: AppConfig,
): void {
  const rendered = gateway.node.defaultChild;
  if (!(rendered instanceof agentcore.CfnGateway)) {
    // Refused rather than coerced. If the L2's child is no longer a `CfnGateway`, silently
    // skipping would deploy a gateway with no policy engine and report success.
    throw new GatewayPolicyError(
      "The Gateway L2's default child is not a CfnGateway, so the policy engine cannot be " +
        'attached. The construct has changed shape; do not deploy a gateway that reports a ' +
        'policy engine it does not have.',
    );
  }
  rendered.policyEngineConfiguration = {
    arn: engine.attrPolicyEngineArn,
    mode: config.gatewayGuardrails.enforcement,
  };
}

/**
 * The gateway's ARN as a name-prefix pattern, built entirely at synth.
 *
 * A gateway ARN embeds a service-generated suffix — `gateway/adlytics-dev-tools-thl9jgdbmi` — so it
 * cannot be reconstructed from the name, and the exact form is a `GetAtt`. That is unusable in the
 * one policy the gateway must wait for: it would make the policy depend on the gateway while the
 * gateway depends on the policy.
 *
 * The prefix keeps it scoped. `assertValidEnvName` bounds the environment name, `toolGatewayName`
 * owns the prefix, and no second gateway can carry it. The CDK Gateway L2 builds the same pattern
 * for the same reason, in the `aws:SourceArn` condition of the role it creates — so this is the
 * construct library's own answer rather than one invented here.
 */
function gatewayArnPattern(gateway: agentcore.Gateway, config: AppConfig): string {
  const { partition, region, account } = cdk.Stack.of(gateway);
  const name = toolGatewayName(config.envName);
  return `arn:${partition}:bedrock-agentcore:${region}:${account}:gateway/${name}*`;
}

/**
 * Everything the gateway's own role needs for the engine to work, and nothing more.
 *
 * **Three actions, and every one of them was found by a failed deploy rather than read from a
 * document.** AWS covers this with `bedrock-agentcore:*` on `*`, which hides all three; the set
 * below is the actual minimum, established one rollback at a time `[verified 2026-08-24 against
 * Adlytics-Agent-dev]`:
 *
 * | Action | Demanded by | When |
 * |---|---|---|
 * | `bedrock-agentcore:GetPolicyEngine` | *"Confirm this role has ... GetPolicyEngine permissions"* | attaching the configuration |
 * | `bedrock-agentcore:AuthorizeAction` | *"not authorized to perform ... on resource: policy-engine/..."* | every evaluation |
 * | `bedrock:InvokeGuardrailChecks` | the guardrails documentation | every guardrail in an evaluation |
 *
 * `AuthorizeAction` is the data plane in its entirety, which is worth knowing: the engine reads its
 * own policies server-side under that one call, so the gateway role needs no `GetPolicy` or
 * `ListPolicySummaries` grant. Two such actions were granted speculatively and removed once this
 * was understood — a read grant nothing exercises is a permission nobody can justify later.
 *
 * **Granting it is not enough; the gateway has to wait for it.** The first attempt had the grant
 * and failed identically, because CloudFormation updated the gateway and the role's policy in
 * parallel — so the gateway validated the engine before the statement authorising it existed. The
 * dependency below is what makes the grant precede the validation, and its absence is invisible in
 * a template review: the statement is right there, and the deploy still fails.
 *
 * **Scoped to the engine and not to the individual policies, and that is a cycle rather than an
 * oversight.** Naming each policy's ARN here would make the role's policy depend on the policies,
 * the gateway depend on the role's policy, and the guardrail policy depend on the gateway — which
 * is a dependency cycle CloudFormation refuses. Since the gateway must wait for this grant, this
 * grant cannot know about anything that waits for the gateway. If policy-level reads turn out to be
 * required, they belong in a second `iam.Policy` on the same role that the gateway does not depend
 * on, rather than widened into this one.
 *
 * **Calling Bedrock Guardrails.** The Policy data plane evaluates a guardrail through
 * `bedrock:InvokeGuardrailChecks` under Forward Access Session credentials derived from this same
 * role `[verified 2026-08-24, guardrails-in-policies "Before you begin"]`. This one takes `*`,
 * which the wildcard gate in `agent-stack.test.ts` otherwise forbids: the action takes its
 * guardrail configuration inline rather than naming a guardrail resource, so there is no ARN to
 * scope to — scoped to a guardrail ARN it simulates as `implicitDeny`, on `*` as `allowed`
 * `[verified 2026-08-24 with iam simulate-custom-policy]`. Same evidence and same exemption as the
 * two X-Ray actions already named there, added by name rather than by relaxing the rule.
 *
 * The role is the one the `Gateway` L2 creates for itself when none is passed — `props.role ??
 * this.createGatewayRole()`. Nothing else in this stack has a use for it.
 */
function grantEngineAccess(
  gateway: agentcore.Gateway,
  engine: agentcore.CfnPolicyEngine,
  config: AppConfig,
): void {
  const { role } = gateway;
  if (role === undefined) {
    throw new GatewayPolicyError(
      'The gateway has no execution role, so the policy engine has no identity to read its ' +
        'policies or evaluate guardrails under. The L2 creates one when none is passed; if that ' +
        'has changed, pass a role explicitly rather than deploying an engine that cannot work.',
    );
  }

  role.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: [
        // Attach-time validation. The gateway reads the engine when the configuration is set.
        'bedrock-agentcore:GetPolicyEngine',
        // The evaluation itself. These two are the whole data plane: the engine reads its own
        // policies server-side under them, which is why no `GetPolicy` grant is needed and why two
        // speculative read actions were removed once they were identified.
        //
        // Enumerated rather than wildcarded, and neither appears in any public API model or in the
        // service authorization reference — both were named by a rejected deploy. A
        // `bedrock-agentcore:*Authorize*` grant would have covered them in one line and would have
        // left nobody able to say what it actually permits.
        'bedrock-agentcore:AuthorizeAction',
        'bedrock-agentcore:PartiallyAuthorizeActions',
      ],
      // Both, because `AuthorizeAction` is authorized against the engine *and* against the gateway
      // it is attached to — established by two separate rollbacks naming each in turn.
      //
      // The gateway is a name-prefix pattern rather than its own ARN, and that is forced: a gateway
      // ARN embeds a service-generated suffix so it cannot be reconstructed, and the exact form is a
      // `GetAtt` — which would make this policy depend on the gateway while the gateway already
      // depends on this policy. The CDK Gateway L2 builds the same pattern, for the same reason, in
      // the `aws:SourceArn` condition of the role it creates.
      resources: [engine.attrPolicyEngineArn, gatewayArnPattern(gateway, config)],
    }),
  );

  role.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ['bedrock:InvokeGuardrailChecks'],
      resources: ['*'],
    }),
  );

  // The dependency the first deploy proved necessary. Construct-level rather than resource-level:
  // it is the role's `DefaultPolicy` that carries the statements, and reaching that child by name
  // would break silently if CDK renamed it, while depending on the role covers every resource in
  // its subtree.
  const rendered = gateway.node.defaultChild;
  if (rendered instanceof cdk.CfnResource) {
    rendered.node.addDependency(role);
  }
}

/**
 * Permit everything the guardrail policies do not forbid.
 *
 * **The single highest-risk resource in this change, and it is the one that does nothing today.**
 * Cedar denies by default: if no policy matches, the answer is DENY. So a policy engine the
 * gateway is honouring — `ENFORCE` — refuses every tool call unless something permits it, and the
 * symptom is every question failing while all eleven stacks report healthy. AWS's own walkthrough
 * adds this policy for exactly that reason.
 *
 * It ships now rather than with the promotion to `ENFORCE`, so that promotion is a one-line
 * configuration change that cannot forget it.
 *
 * `ACTIVE` rather than `LOG_ONLY`, unlike the guardrail policy below. Cedar is forbid-wins, so a
 * permit can never override a denial and this cannot weaken anything — while a base permit in
 * `LOG_ONLY` would be inert exactly when it is needed.
 *
 * **`IGNORE_ALL_FINDINGS`, and it is the one policy here that earns it.** The Cedar analyzer refuses
 * this statement under the default `FAIL_ON_ANY_FINDINGS` — *"Overly Permissive: Policy Engine will
 * allow every request for the specified principal ... action (Any Future Tools) and resource
 * (gateway/\*)"* `[verified 2026-08-24, the rejected create on Adlytics-Agent-dev]`. The finding is
 * correct: this policy is deliberately overly permissive, because Cedar denies by default and
 * something has to permit. Suppressing a true finding is only defensible where the finding is the
 * intent, which is why the guardrail policy keeps strict validation instead of inheriting this.
 */
function basePermitPolicy(
  scope: Construct,
  engine: agentcore.CfnPolicyEngine,
  config: AppConfig,
): agentcore.CfnPolicy {
  return new agentcore.CfnPolicy(scope, 'ToolGatewayBasePermit', {
    name: gatewayPolicyName(config.envName, 'base_permit'),
    policyEngineId: engine.attrPolicyEngineId,
    description: 'Permits any gateway action no guardrail policy forbids. Cedar denies by default.',
    enforcementMode: 'ACTIVE',
    validationMode: 'IGNORE_ALL_FINDINGS',
    definition: {
      cedar: { statement: 'permit (principal, action, resource is AgentCore::Gateway);' },
    },
  });
}
