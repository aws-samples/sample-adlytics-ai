// SPDX-License-Identifier: MIT-0
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

import {
  AppConfig,
  EDGE_DEFAULT_MAX_BODY_BYTES,
  ENTITLEMENT_CLAIM,
  MCP_TOOL_USE_LEVEL,
  externalMcpGatewayName,
} from '../../config';
import { GuardedWebAcl, WebAclRule, WebAclRuleMode } from '../edge/guarded-web-acl';
import { GeneratedTools } from './generated-tools';

/**
 * The mode every external MCP web ACL rule takes (ADR-150, count-then-block per ADR-078).
 *
 * `count` for pass 1, which deploys the gate with no tool targets; the pass-2 switch to `block` is
 * a one-line commit here, made only after pass 1 showed the ACL sees gateway traffic and matches
 * nothing legitimate. `buildExternalMcpGateway` refuses targets while any rule still counts.
 */
export const EXTERNAL_MCP_WAF_MODE: WebAclRuleMode = 'count';

/**
 * The external MCP gateway's rules, in the edge's order (`edgeWebAclRules`).
 *
 * The edge's managed groups, with the common rule set's permanent `SizeRestrictions_BODY` count
 * override, then the edge's 8,192-byte ceiling on every request (the gateway has no route to
 * exempt), then the per-source-IP rate. The CloudFront-only parts of the edge (viewer function,
 * origin-verify) have no counterpart: the gateway is the origin and has no bypass hostname.
 */
export function externalMcpWebAclRules(
  config: AppConfig,
  mode: WebAclRuleMode = EXTERNAL_MCP_WAF_MODE,
): readonly WebAclRule[] {
  return [
    { kind: 'managed', name: 'AwsIpReputation', managedGroup: 'AWSManagedRulesAmazonIpReputationList', mode },
    { kind: 'managed', name: 'AwsKnownBadInputs', managedGroup: 'AWSManagedRulesKnownBadInputsRuleSet', mode },
    {
      kind: 'managed',
      name: 'AwsCommonRuleSet',
      managedGroup: 'AWSManagedRulesCommonRuleSet',
      mode,
      countRules: ['SizeRestrictions_BODY'],
    },
    {
      kind: 'body-size',
      name: 'BodySizeAll',
      maxBodyBytes: EDGE_DEFAULT_MAX_BODY_BYTES,
      route: { all: true },
      mode,
    },
    {
      kind: 'rate',
      name: 'RateLimitPerSourceIp',
      limitPerFiveMinutes: config.externalMcp.wafRateLimitPerFiveMinutes,
      mode,
    },
  ];
}

/** The two Gateway target descriptions, shared with the in-app gateway so they cannot drift. */
export interface TargetDescriptions {
  readonly catalog: string;
  readonly measurement: string;
}

export interface ExternalMcpGatewayProps {
  readonly config: AppConfig;
  /** The ACL rules, `externalMcpWebAclRules(config)` in production. */
  readonly rules: readonly WebAclRule[];
  /** The generated tool definitions and the external-client rule (`loadGeneratedTools`). */
  readonly tools: GeneratedTools;
  readonly targetDescriptions: TargetDescriptions;
  /** The same tool functions the in-app gateway invokes; their logic is not forked. */
  readonly catalogFunction: lambda.IFunction;
  readonly measurementFunction: lambda.IFunction;
  /** The interceptor whose handler requires a bearer on every JSON-RPC method. */
  readonly interceptorFunction: lambda.IFunction;
  /** The user pool whose access tokens the gateway admits. */
  readonly userPoolId: string;
  /** The one Cognito client those tokens must have been issued to (`mcpClientId`). */
  readonly mcpClientId: string;
}

export interface ExternalMcpGateway {
  readonly gateway: agentcore.Gateway;
  readonly acl: GuardedWebAcl;
}

/**
 * The external MCP gateway Amazon Quick connects to (ADR-150): a second AgentCore Gateway over the
 * same two tool functions, admitting only a user's own Cognito access token.
 *
 * - **Inbound** is `CUSTOM_JWT` on the user pool: `client_id` must be the MCP client, the scope
 *   `openid`, and the closed levels claim must contain `mcp_tool_use`. No shared service identity
 *   exists on this path, so one Quick user can never act as another.
 * - **The interceptor** forwards the bearer on every method and refuses a request without one, and
 *   each tool verifies the same token again (`CallerVerifier`, Cognito branch).
 * - **The web ACL** fronts the gateway, `FAIL_CLOSE` if WAF cannot be reached.
 * - **Targets** attach only with `externalMcp.targetsAttached`, and only behind a blocking ACL.
 *   Each tool definition's description gains the external-client rule; nothing else differs from
 *   the in-app tools.
 * - `exceptionLevel` is unset, so errors are sanitised; no `searchType`, so `tools/list` returns
 *   exactly the declared tools.
 *
 * The gateway service role is the L2's own. It gains `lambda:InvokeFunction` on exactly the
 * interceptor and, with targets, the two tool functions; nothing else.
 */
export function buildExternalMcpGateway(scope: Construct, props: ExternalMcpGatewayProps): ExternalMcpGateway {
  const { config } = props;
  const counting = props.rules.filter((rule) => rule.mode === 'count').map((rule) => rule.name);
  if (config.externalMcp.targetsAttached && counting.length > 0) {
    throw new Error(
      'The external MCP gateway attaches its tool targets only behind a blocking web ACL (ADR-150); ' +
        `these rules still count: ${counting.join(', ')}.`,
    );
  }

  const name = externalMcpGatewayName(config.envName);
  const issuer = `https://cognito-idp.${config.region}.amazonaws.com/${props.userPoolId}`;
  const gateway = new agentcore.Gateway(scope, 'ExternalMcpGateway', {
    gatewayName: name,
    description: 'External MCP surface over the governed tools for Amazon Quick (ADR-150).',
    authorizerConfiguration: agentcore.GatewayAuthorizer.usingCustomJwt({
      discoveryUrl: `${issuer}/.well-known/openid-configuration`,
      allowedClients: [props.mcpClientId],
      allowedScopes: ['openid'],
      customClaims: [
        agentcore.GatewayCustomClaim.withStringArrayValue(
          ENTITLEMENT_CLAIM,
          [MCP_TOOL_USE_LEVEL],
          agentcore.CustomClaimOperator.CONTAINS,
        ),
      ],
    }),
    // Stated, because the L2 default is one older version and semantic search, which would add a
    // search tool beside the five declared ones.
    protocolConfiguration: new agentcore.McpProtocolConfiguration({
      supportedVersions: [
        agentcore.MCPProtocolVersion.MCP_2025_06_18,
        agentcore.MCPProtocolVersion.MCP_2025_03_26,
      ],
      instructions: props.tools.externalClientRule,
    }),
  });
  // Already the service default; stated so the template says what happens when WAF is unreachable.
  (gateway.node.defaultChild as agentcore.CfnGateway).wafConfiguration = { failureMode: 'FAIL_CLOSE' };

  gateway.addInterceptor(
    agentcore.LambdaInterceptor.forRequest(props.interceptorFunction, { passRequestHeaders: true }),
  );

  const acl = new GuardedWebAcl(scope, 'ExternalMcpWebAcl', {
    name,
    scope: 'REGIONAL',
    rules: props.rules,
    logRetention: config.logRetentionDays,
  });
  new wafv2.CfnWebACLAssociation(scope, 'ExternalMcpWebAclAssociation', {
    resourceArn: gateway.gatewayArn,
    webAclArn: acl.acl.attrArn,
  });

  if (config.externalMcp.targetsAttached) {
    const withRule = (definitions: agentcore.ToolDefinition[]): agentcore.ToolDefinition[] =>
      definitions.map((definition) => ({
        ...definition,
        description: `${definition.description} ${props.tools.externalClientRule}`,
      }));
    // Named as the in-app targets are: the name is the prefix the tool handler strips.
    agentcore.GatewayTarget.forLambda(scope, 'ExternalMcpCatalogTarget', {
      gateway,
      gatewayTargetName: 'catalog',
      description: props.targetDescriptions.catalog,
      lambdaFunction: props.catalogFunction,
      toolSchema: agentcore.ToolSchema.fromInline(withRule(props.tools.catalog)),
    });
    agentcore.GatewayTarget.forLambda(scope, 'ExternalMcpMeasurementTarget', {
      gateway,
      gatewayTargetName: 'measurement',
      description: props.targetDescriptions.measurement,
      lambdaFunction: props.measurementFunction,
      toolSchema: agentcore.ToolSchema.fromInline(withRule(props.tools.measurement)),
    });
  }

  return { gateway, acl };
}
