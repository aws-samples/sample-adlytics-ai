// SPDX-License-Identifier: MIT-0
/**
 * The external MCP gateway for Amazon Quick (ADR-150), off and on.
 *
 * Off is what every environment ships, so the first block proves the flag adds nothing: the
 * resource counts equal today's and no Quick-only name, variable or parameter appears. On, the
 * assertions pin each control the design relies on to refuse a caller who is not an entitled user
 * of this deployment's pool: the JWT authorizer's client, scope and claim; the strict interceptor;
 * the web ACL in front with `FAIL_CLOSE`; and an IAM boundary that lets the gateway invoke exactly
 * three functions and gives the tools no new grant. Pass 2's targets are built in a bare stack,
 * because the shipped rule mode is `count` and synth refuses targets behind a counting ACL.
 */
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import {
  AppConfig,
  ENTITLEMENT_CLAIM,
  MCP_TOOL_USERS_GROUP,
  MCP_TOOL_USE_LEVEL,
  externalMcpGatewayName,
  mcpClientName,
  paramPath,
  quickOauthCallbackUrl,
} from '../lib/config';
import {
  EXTERNAL_MCP_WAF_MODE,
  buildExternalMcpGateway,
  externalMcpWebAclRules,
} from '../lib/constructs/agent/external-mcp-gateway';
import { loadGeneratedTools } from '../lib/constructs/agent/generated-tools';
import { GuardedWebAcl, REDACTED_HEADERS } from '../lib/constructs/edge/guarded-web-acl';
import { AgentStack } from '../lib/stacks/agent-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { TEST_CONFIG, configWith } from './support/config';

type Props = Record<string, unknown>;
type Resource = { Type: string; Properties?: Props };
type Statement = { Action?: unknown; Effect?: unknown; Resource?: unknown };

/** Any directory will do: `Code.fromAsset` only needs one to exist at synth. */
const ASSET = __dirname;
/** The staged tool contract the deployed functions carry, as `agent-stack.test.ts` reads it. */
const TOOL_SCHEMA = path.join(__dirname, '..', '..', 'build', 'src', 'contracts', 'generated', 'tools.json');

const PASS_1 = { enabled: true, targetsAttached: false, wafRateLimitPerFiveMinutes: 1000 } as const;
const ON = configWith({ externalMcp: PASS_1 });
const ENV = { account: TEST_CONFIG.account, region: TEST_CONFIG.region };
const RULE_NAMES = ['AwsIpReputation', 'AwsKnownBadInputs', 'AwsCommonRuleSet', 'BodySizeAll', 'RateLimitPerSourceIp'];
const STRICT_HANDLER = 'adapters.inbound.gateway_interceptor.handler.require_bearer_handler';

function agent(config: AppConfig): Template {
  return Template.fromStack(
    new AgentStack(new cdk.App(), 'Adlytics-Agent-test', {
      config,
      env: ENV,
      sourceAssetPath: ASSET,
      toolSchemaPath: TOOL_SCHEMA,
    }),
  );
}

function auth(config: AppConfig): Template {
  return Template.fromStack(new AuthStack(new cdk.App(), 'Adlytics-Auth-test', { config, env: ENV }));
}

function resources(template: Template): Record<string, Resource> {
  return template.toJSON().Resources as Record<string, Resource>;
}

function ofType(template: Template, type: string): [string, Props][] {
  return Object.entries(resources(template))
    .filter(([, resource]) => resource.Type === type)
    .map(([id, resource]) => [id, resource.Properties ?? {}]);
}

function only(template: Template, type: string, match: (properties: Props) => boolean): [string, Props] {
  const found = ofType(template, type).filter(([, properties]) => match(properties));
  if (found.length !== 1) throw new Error(`expected one ${type}, found ${found.length}`);
  return found[0]!;
}

function functionId(template: Template, suffix: string): string {
  return only(template, 'AWS::Lambda::Function', (p) => p.FunctionName === `adlytics-dev-${suffix}`)[0];
}

function externalGateway(template: Template): [string, Props] {
  return only(template, 'AWS::BedrockAgentCore::Gateway', (p) => p.Name === externalMcpGatewayName('dev'));
}

/** Every statement of every inline policy attached to one role. */
function roleStatements(template: Template, roleId: string): Statement[] {
  return ofType(template, 'AWS::IAM::Policy')
    .filter(([, p]) => ((p.Roles as { Ref: string }[]) ?? []).some((role) => role.Ref === roleId))
    .flatMap(([, p]) => (p.PolicyDocument as { Statement: Statement[] }).Statement);
}

function roleOf(properties: Props, key: 'RoleArn' | 'Role'): string {
  return (properties[key] as { 'Fn::GetAtt': [string, string] })['Fn::GetAtt'][0];
}

/** The exact invoke statement the L2 grants a gateway role on one function, `:*` its qualifiers. */
function invokeOnly(id: string): Statement {
  const arn = { 'Fn::GetAtt': [id, 'Arn'] };
  return { Action: 'lambda:InvokeFunction', Effect: 'Allow', Resource: [arn, { 'Fn::Join': ['', [arn, ':*']] }] };
}

function webAcl(template: Template): Props {
  return only(template, 'AWS::WAFv2::WebACL', (p) => p.Name === externalMcpGatewayName('dev'))[1];
}

/** The same rules rendered by the shared construct, under the same ACL name, as the reference. */
function renderedRules(config: AppConfig, mode: 'count' | 'block'): unknown {
  const stack = new cdk.Stack(new cdk.App(), 'Reference', { env: ENV });
  new GuardedWebAcl(stack, 'Reference', {
    name: externalMcpGatewayName('dev'),
    scope: 'REGIONAL',
    rules: externalMcpWebAclRules(config, mode),
    logRetention: logs.RetentionDays.ONE_DAY,
  });
  return (ofType(Template.fromStack(stack), 'AWS::WAFv2::WebACL')[0]![1] as Props).Rules;
}

/** A bare stack holding only the external gateway, for the pass-2 shape and the refusal. */
function bare(config: AppConfig, mode: 'count' | 'block'): Template {
  const stack = new cdk.Stack(new cdk.App(), 'Bare', { env: ENV });
  const fn = (id: string): lambda.Function =>
    new lambda.Function(stack, id, {
      functionName: `adlytics-dev-${id}`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context):\n    return event\n'),
    });
  buildExternalMcpGateway(stack, {
    config,
    rules: externalMcpWebAclRules(config, mode),
    tools: loadGeneratedTools(TOOL_SCHEMA),
    targetDescriptions: { catalog: 'catalog target', measurement: 'measurement target' },
    catalogFunction: fn('CatalogTool'),
    measurementFunction: fn('MeasurementTool'),
    interceptorFunction: fn('ExternalMcpInterceptor'),
    userPoolId: 'eu-west-1_example',
    mcpClientId: 'mcp-client-example',
  });
  return Template.fromStack(stack);
}

describe('flag OFF: the external MCP gateway adds nothing (ADR-150)', () => {
  const off = agent(TEST_CONFIG);
  const offAuth = auth(TEST_CONFIG);

  it('keeps the agent stack at one gateway, two targets and three functions', () => {
    expect(ofType(off, 'AWS::BedrockAgentCore::Gateway')).toHaveLength(1);
    expect(ofType(off, 'AWS::BedrockAgentCore::GatewayTarget')).toHaveLength(2);
    expect(ofType(off, 'AWS::Lambda::Function')).toHaveLength(3);
    expect(ofType(off, 'AWS::WAFv2::WebACL')).toEqual([]);
    expect(ofType(off, 'AWS::WAFv2::WebACLAssociation')).toEqual([]);
    expect(Object.keys(resources(off)).filter((id) => id.includes('ExternalMcp'))).toEqual([]);
    expect(Object.keys(off.toJSON().Outputs ?? {})).not.toContain('OutExternalMcpGatewayUrl');
  });

  it('gives no function the MCP client id and publishes no external gateway URL', () => {
    for (const [, properties] of ofType(off, 'AWS::Lambda::Function')) {
      const variables = (properties.Environment as { Variables?: Props } | undefined)?.Variables ?? {};
      expect(Object.keys(variables)).not.toContain('ADL_MCP_CLIENT_ID');
    }
    const names = ofType(off, 'AWS::SSM::Parameter').map(([, p]) => p.Name);
    expect(names).not.toContain(paramPath('dev', 'externalMcpGatewayUrl'));
    expect(JSON.stringify(off.toJSON())).not.toContain('mcp-client-id');
  });

  it('keeps the auth stack at two clients, five groups and two brandings', () => {
    expect(ofType(offAuth, 'AWS::Cognito::UserPoolClient').map(([, p]) => p.ClientName)).toEqual([
      'adlytics-bff-dev',
      'adlytics-review-cli-dev',
    ]);
    expect(ofType(offAuth, 'AWS::Cognito::UserPoolGroup').map(([, p]) => p.GroupName)).toEqual([
      'quality-reviewer',
      'audit-attribution',
      'prompt-admin',
      'operations-status-read',
      'operations-run-requesters',
    ]);
    expect(ofType(offAuth, 'AWS::Cognito::ManagedLoginBranding')).toHaveLength(2);
    const names = ofType(offAuth, 'AWS::SSM::Parameter').map(([, p]) => p.Name);
    expect(names).not.toContain(paramPath('dev', 'mcpClientId'));
  });
});

describe('the count-mode refusal before targets attach (ADR-150)', () => {
  const pass2 = configWith({ externalMcp: { ...PASS_1, targetsAttached: true } });

  it('refuses targets behind a counting ACL, naming every counting rule', () => {
    expect(() => bare(pass2, 'count')).toThrow(
      'The external MCP gateway attaches its tool targets only behind a blocking web ACL (ADR-150); ' +
        `these rules still count: ${RULE_NAMES.join(', ')}.`,
    );
  });

  it('synthesises two targets behind a blocking ACL, the managed count override included (control)', () => {
    const rules = externalMcpWebAclRules(pass2, 'block');
    expect(rules.find((rule) => rule.name === 'AwsCommonRuleSet')).toMatchObject({
      countRules: ['SizeRestrictions_BODY'],
    });
    expect(ofType(bare(pass2, 'block'), 'AWS::BedrockAgentCore::GatewayTarget')).toHaveLength(2);
  });

  it('ships every rule in count for pass 1', () => {
    expect(EXTERNAL_MCP_WAF_MODE).toBe('count');
  });
});

describe('flag ON, pass 1: the gate with no targets (ADR-150)', () => {
  const on = agent(ON);
  const off = agent(TEST_CONFIG);
  const [gatewayId, gateway] = externalGateway(on);
  const interceptorId = functionId(on, 'ExternalMcpInterceptor');

  it('adds a second gateway and no target', () => {
    expect(ofType(on, 'AWS::BedrockAgentCore::Gateway')).toHaveLength(2);
    const targets = ofType(on, 'AWS::BedrockAgentCore::GatewayTarget');
    expect(targets).toHaveLength(2);
    expect(JSON.stringify(targets)).not.toContain(gatewayId);
  });

  it('admits only an mcp-tool-users access token for the MCP client, scope openid', () => {
    // The two SSM-backed parameters AuthStack publishes, read at deploy.
    const parameter = (pattern: RegExp): string => JSON.stringify(gateway).match(pattern)![0];
    const userPoolId = parameter(/SsmParameterValueadlyticsdevauthuserpoolid[A-Za-z0-9]+Parameter/);
    const mcpClient = parameter(/SsmParameterValueadlyticsdevauthmcpclientid[A-Za-z0-9]+Parameter/);
    expect(gateway.AuthorizerType).toBe('CUSTOM_JWT');
    expect(gateway.AuthorizerConfiguration).toStrictEqual({
      CustomJWTAuthorizer: {
        DiscoveryUrl: {
          'Fn::Join': [
            '',
            [`https://cognito-idp.${ON.region}.amazonaws.com/`, { Ref: userPoolId }, '/.well-known/openid-configuration'],
          ],
        },
        AllowedClients: [{ Ref: mcpClient }],
        AllowedScopes: ['openid'],
        CustomClaims: [
          {
            InboundTokenClaimName: ENTITLEMENT_CLAIM,
            InboundTokenClaimValueType: 'STRING_ARRAY',
            AuthorizingClaimMatchValue: {
              ClaimMatchOperator: 'CONTAINS',
              ClaimMatchValue: { MatchValueString: MCP_TOOL_USE_LEVEL },
            },
          },
        ],
      },
    });
    expect(ENTITLEMENT_CLAIM).toBe('quality_review_levels');
    expect(MCP_TOOL_USE_LEVEL).toBe('mcp_tool_use');
  });

  it('states both MCP versions and the external-client rule, with no search tool or debug detail', () => {
    const rule = loadGeneratedTools(TOOL_SCHEMA).externalClientRule;
    expect(gateway.ProtocolConfiguration).toStrictEqual({
      Mcp: { SupportedVersions: ['2025-06-18', '2025-03-26'], Instructions: rule },
    });
    expect(gateway).not.toHaveProperty('ExceptionLevel');
  });

  it('fails closed when WAF cannot be reached', () => {
    expect(gateway.WafConfiguration).toStrictEqual({ FailureMode: 'FAIL_CLOSE' });
  });

  it('runs one REQUEST interceptor with headers, on the bearer-on-every-method entry point', () => {
    expect(gateway.InterceptorConfigurations).toStrictEqual([
      {
        InputConfiguration: { PassRequestHeaders: true },
        InterceptionPoints: ['REQUEST'],
        Interceptor: { Lambda: { Arn: { 'Fn::GetAtt': [interceptorId, 'Arn'] } } },
      },
    ]);
    const [, fn] = only(on, 'AWS::Lambda::Function', (p) => p.FunctionName === 'adlytics-dev-ExternalMcpInterceptor');
    expect(fn.Handler).toBe(STRICT_HANDLER);
    expect([fn.MemorySize, fn.Timeout, fn.Architectures]).toStrictEqual([512, 5, ['arm64']]);
  });

  it('associates its own regional web ACL with exactly this gateway', () => {
    const [aclId] = only(on, 'AWS::WAFv2::WebACL', (p) => p.Name === externalMcpGatewayName('dev'));
    expect(ofType(on, 'AWS::WAFv2::WebACLAssociation').map(([, p]) => p)).toStrictEqual([
      { ResourceArn: { 'Fn::GetAtt': [gatewayId, 'GatewayArn'] }, WebACLArn: { 'Fn::GetAtt': [aclId, 'Arn'] } },
    ]);
    expect(webAcl(on).Scope).toBe('REGIONAL');
    expect(webAcl(on).DefaultAction).toStrictEqual({ Allow: {} });
  });

  it("runs the edge's rules in the edge's order, every one counting, the ceiling on every request", () => {
    const rules = webAcl(on).Rules as Props[];
    expect(rules).toStrictEqual(renderedRules(ON, 'count'));
    expect(rules.map((rule) => [rule.Name, rule.OverrideAction ?? rule.Action])).toStrictEqual([
      ['AwsIpReputation', { Count: {} }],
      ['AwsKnownBadInputs', { Count: {} }],
      ['AwsCommonRuleSet', { Count: {} }],
      ['BodySizeAll', { Count: {} }],
      ['RateLimitPerSourceIp', { Count: {} }],
    ]);
    expect(rules[3]!.Statement).toStrictEqual({
      SizeConstraintStatement: {
        FieldToMatch: { Body: { OversizeHandling: 'MATCH' } },
        ComparisonOperator: 'GT',
        Size: 8192,
        TextTransformations: [{ Priority: 0, Type: 'NONE' }],
      },
    });
    expect((rules[4]!.Statement as Props).RateBasedStatement).toMatchObject({ Limit: 1000, AggregateKeyType: 'IP' });
  });

  it('logs matched requests only, with the credential headers redacted and sampling off', () => {
    const [, logging] = ofType(on, 'AWS::WAFv2::LoggingConfiguration')[0]!;
    expect(logging.RedactedFields).toStrictEqual(REDACTED_HEADERS.map((name) => ({ SingleHeader: { Name: name } })));
    expect(REDACTED_HEADERS).toEqual(expect.arrayContaining(['authorization', 'cookie']));
    const visibility = JSON.stringify(webAcl(on)).match(/"SampledRequestsEnabled":(true|false)/g) ?? [];
    expect(visibility.length).toBe(6);
    expect(new Set(visibility)).toStrictEqual(new Set(['"SampledRequestsEnabled":false']));
    expect(ofType(on, 'AWS::Logs::LogGroup').map(([, p]) => p.LogGroupName)).toContain(
      'aws-waf-logs-adlytics-dev-external-mcp',
    );
  });

  it('lets the gateway role invoke exactly the interceptor, and nothing else', () => {
    expect(roleStatements(on, roleOf(gateway, 'RoleArn'))).toStrictEqual([invokeOnly(interceptorId)]);
  });

  it('opens no function by resource policy', () => {
    expect(ofType(on, 'AWS::Lambda::Permission')).toEqual([]);
  });

  it('gives the interceptor only its own log writes and the in-app interceptor span export', () => {
    const roleFor = (template: Template, name: string): string =>
      roleOf(only(template, 'AWS::Lambda::Function', (p) => p.FunctionName === `adlytics-dev-${name}`)[1], 'Role');
    const strict = roleStatements(on, roleFor(on, 'ExternalMcpInterceptor'));
    const inApp = roleStatements(on, roleFor(on, 'GatewayInterceptor'));
    const logGroup = (template: Template, name: string): string =>
      only(template, 'AWS::Logs::LogGroup', (p) => p.LogGroupName === `/aws/lambda/adlytics-dev-${name}`)[0];
    expect(JSON.stringify(strict)).toBe(
      JSON.stringify(inApp).split(logGroup(on, 'GatewayInterceptor')).join(logGroup(on, 'ExternalMcpInterceptor')),
    );
    // The one `*`: X-Ray's PutTraceSegments and PutTelemetryRecords define no resource type.
    const wildcards = strict.filter((statement) => statement.Resource === '*');
    expect(wildcards).toStrictEqual([
      { Action: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'], Effect: 'Allow', Resource: '*' },
    ]);
  });

  it('gives the tool roles no new grant, and no cognito-idp action anywhere', () => {
    for (const name of ['CatalogTool', 'MeasurementTool']) {
      const role = (template: Template): string =>
        roleOf(only(template, 'AWS::Lambda::Function', (p) => p.FunctionName === `adlytics-dev-${name}`)[1], 'Role');
      expect(roleStatements(on, role(on))).toStrictEqual(roleStatements(off, role(off)));
    }
    expect(JSON.stringify(on.toJSON())).not.toContain('cognito-idp:');
  });

  it('gives both tools the MCP client id from the parameter AuthStack publishes, and nothing else new', () => {
    for (const name of ['CatalogTool', 'MeasurementTool']) {
      const variables = (template: Template): Props =>
        ((only(template, 'AWS::Lambda::Function', (p) => p.FunctionName === `adlytics-dev-${name}`)[1]
          .Environment as { Variables: Props }).Variables);
      const { ADL_MCP_CLIENT_ID: mcpClient, ...rest } = variables(on);
      expect(rest).toStrictEqual(variables(off));
      expect(JSON.stringify(mcpClient)).toMatch(/^\{"Ref":"SsmParameterValueadlyticsdevauthmcpclientid[A-Za-z0-9]+Parameter"\}$/);
    }
    expect(JSON.stringify(on.toJSON().Parameters)).toContain(paramPath('dev', 'mcpClientId'));
  });

  it('publishes the gateway URL to its parameter and an output', () => {
    const [, parameter] = only(on, 'AWS::SSM::Parameter', (p) => p.Name === paramPath('dev', 'externalMcpGatewayUrl'));
    expect(parameter.Value).toStrictEqual({ 'Fn::GetAtt': [gatewayId, 'GatewayUrl'] });
    expect(on.toJSON().Outputs.OutExternalMcpGatewayUrl.Value).toStrictEqual({ 'Fn::GetAtt': [gatewayId, 'GatewayUrl'] });
  });
});

describe('flag ON, pass 2 shape: the targets behind a blocking ACL (ADR-150)', () => {
  const pass2 = configWith({ externalMcp: { ...PASS_1, targetsAttached: true } });
  const template = bare(pass2, 'block');
  const tools = loadGeneratedTools(TOOL_SCHEMA);
  const [, gateway] = externalGateway(template);

  it('attaches catalog and measurement, each tool description ending with the rule', () => {
    const targets = ofType(template, 'AWS::BedrockAgentCore::GatewayTarget').map(([, p]) => p);
    expect(targets.map((target) => target.Name)).toEqual(['catalog', 'measurement']);
    expect(targets.map((target) => target.Description)).toEqual(['catalog target', 'measurement target']);
    const definitions = targets.flatMap(
      (target) =>
        ((target.TargetConfiguration as Props).Mcp as { Lambda: { ToolSchema: { InlinePayload: Props[] } } }).Lambda
          .ToolSchema.InlinePayload,
    );
    const declared = [...tools.catalog, ...tools.measurement];
    expect(definitions.map((definition) => definition.Name)).toEqual(declared.map((definition) => definition.name));
    for (const [index, definition] of definitions.entries()) {
      expect(definition.Description).toBe(`${declared[index]!.description} ${tools.externalClientRule}`);
    }
  });

  it('lets the gateway role invoke exactly the interceptor and the two tools', () => {
    const ids = ['ExternalMcpInterceptor', 'CatalogTool', 'MeasurementTool'].map(
      (name) => only(template, 'AWS::Lambda::Function', (p) => p.FunctionName === `adlytics-dev-${name}`)[0],
    );
    const statements = roleStatements(template, roleOf(gateway, 'RoleArn'));
    expect(new Set(statements.map((statement) => JSON.stringify(statement)))).toStrictEqual(
      new Set(ids.map((id) => JSON.stringify(invokeOnly(id)))),
    );
    expect(statements).toHaveLength(3);
    expect(ofType(template, 'AWS::Lambda::Permission')).toEqual([]);
  });

  it('blocks on every rule', () => {
    const rules = webAcl(template).Rules as Props[];
    expect(rules.map((rule) => rule.OverrideAction ?? rule.Action)).toStrictEqual([
      { None: {} },
      { None: {} },
      { None: {} },
      { Block: {} },
      { Block: {} },
    ]);
  });
});

describe('the in-app target descriptions are the external ones (ADR-150)', () => {
  it('reads one home for both gateways', () => {
    const app = new cdk.App();
    const pass2 = configWith({ externalMcp: { ...PASS_1, targetsAttached: true } });
    // AgentStack refuses pass 2 while the shipped mode counts, which is the refusal under test above;
    // the in-app descriptions are read from the flag-off stack instead.
    expect(() => new AgentStack(app, 'Refused', { config: pass2, env: ENV, sourceAssetPath: ASSET, toolSchemaPath: TOOL_SCHEMA })).toThrow(
      /attaches its tool targets only behind a blocking web ACL/,
    );
    const descriptions = ofType(agent(TEST_CONFIG), 'AWS::BedrockAgentCore::GatewayTarget').map(([, p]) => p.Description);
    expect(descriptions).toEqual([
      'What can be asked. Reads no business data; writes transient grounding.',
      'Figures from the curated lake, with everything that qualifies them.',
    ]);
  });
});

describe('flag ON: the Amazon Quick sign-in client (ADR-150)', () => {
  // The dev validation password flag on, to prove it never reaches this client.
  const template = auth(configWith({ externalMcp: PASS_1, auth: { ...TEST_CONFIG.auth, qualityReviewValidationPasswordAuthEnabled: true } }));
  const [clientId, client] = only(template, 'AWS::Cognito::UserPoolClient', (p) => p.ClientName === mcpClientName('dev'));

  it('is a public authorization-code client for openid, called back only at Quick', () => {
    expect(client.GenerateSecret).toBe(false);
    expect(client.AllowedOAuthFlows).toStrictEqual(['code']);
    expect(client.AllowedOAuthFlowsUserPoolClient).toBe(true);
    expect(client.AllowedOAuthScopes).toStrictEqual(['openid']);
    expect(client.CallbackURLs).toStrictEqual([quickOauthCallbackUrl(TEST_CONFIG.region)]);
    expect(client).not.toHaveProperty('LogoutURLs');
    expect(quickOauthCallbackUrl('us-east-1')).toBe('https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback');
  });

  it('has no password, SRP, custom or choice-based flow, even with the dev validation flag on', () => {
    expect(client.ExplicitAuthFlows).toStrictEqual(['ALLOW_REFRESH_TOKEN_AUTH']);
  });

  it('lives 15 minutes per access and ID token, 12 hours per refresh, with revocation on', () => {
    expect([client.AccessTokenValidity, client.IdTokenValidity, client.RefreshTokenValidity]).toStrictEqual([15, 15, 720]);
    expect(client.TokenValidityUnits).toStrictEqual({ AccessToken: 'minutes', IdToken: 'minutes', RefreshToken: 'minutes' });
    expect(client.EnableTokenRevocation).toBe(true);
    expect(client.PreventUserExistenceErrors).toBe('ENABLED');
  });

  it('has its own managed login branding, the mcp-tool-users group and its published id', () => {
    const brandings = ofType(template, 'AWS::Cognito::ManagedLoginBranding').filter(
      ([, p]) => JSON.stringify(p.ClientId) === JSON.stringify({ Ref: clientId }),
    );
    expect(brandings.map(([, p]) => p.UseCognitoProvidedValues)).toStrictEqual([true]);
    expect(ofType(template, 'AWS::Cognito::UserPoolGroup').map(([, p]) => p.GroupName)).toContain(MCP_TOOL_USERS_GROUP);
    const [, parameter] = only(template, 'AWS::SSM::Parameter', (p) => p.Name === paramPath('dev', 'mcpClientId'));
    expect(parameter.Value).toStrictEqual({ Ref: clientId });
  });
});
