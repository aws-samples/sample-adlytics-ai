// SPDX-License-Identifier: MIT-0
/**
 * ADR-078 edge entry: the distribution web ACL, the user pool web ACL, and the origin-verify lock.
 *
 * Acceptance item 10 is proven here: synth refuses an authorizer result TTL above zero, and the
 * web ACL ARN read is a plain parameter in us-east-1 and a custom resource pinned to us-east-1 in
 * any other application region.
 */
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';

import * as logs from 'aws-cdk-lib/aws-logs';

import {
  AppConfig,
  EDGE_DEFAULT_MAX_BODY_BYTES,
  ORIGIN_VERIFY_HEADER,
  PROMPT_CREATE_VERSION_MAX_BODY_BYTES,
  getConfig,
  originVerifySecretName,
  paramPath,
} from '../lib/config';
import {
  GuardedWebAcl,
  GuardedWebAclProps,
  REDACTED_HEADERS,
  WebAclRule,
  renderBodySize,
} from '../lib/constructs/edge/guarded-web-acl';
import { ApiStack } from '../lib/stacks/api-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { EdgeSecurityStack, PROMPT_CREATE_VERSION_ROUTE } from '../lib/stacks/edge-security-stack';
import { FrontendStack } from '../lib/stacks/frontend-stack';
import { TEST_CONFIG } from './support/config';
import { ROUTE_ALLOWLIST } from './support/templates';

type Props = Record<string, unknown>;

const inRegion = (region: string): AppConfig => ({ ...TEST_CONFIG, region });

function frontend(config: AppConfig): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new FrontendStack(app, 'Frontend', {
      config,
      env: { account: config.account, region: config.region },
      routeAllowlistPath: ROUTE_ALLOWLIST,
    }),
  );
}

let edge: Template;
let auth: Template;
let api: Template;
let apiStack: ApiStack;
let otherRegionFrontend: Template;
let edgeRegionFrontend: Template;

beforeAll(() => {
  const app = new cdk.App();
  const env = { account: TEST_CONFIG.account, region: TEST_CONFIG.region };
  const edgeStack = new EdgeSecurityStack(app, 'EdgeSecurity', {
    config: TEST_CONFIG,
    env: { account: TEST_CONFIG.account, region: 'us-east-1' },
  });
  const authStack = new AuthStack(app, 'Auth', { config: TEST_CONFIG, env });
  apiStack = new ApiStack(app, 'Api', { config: TEST_CONFIG, env, sourceAssetPath: __dirname });
  // Every stack exists before the first synth, which freezes the app's construct tree.
  edge = Template.fromStack(edgeStack);
  auth = Template.fromStack(authStack);
  api = Template.fromStack(apiStack);
  otherRegionFrontend = frontend(TEST_CONFIG);
  edgeRegionFrontend = frontend(inRegion('us-east-1'));
});

function acls(template: Template): Props[] {
  return Object.values(template.findResources('AWS::WAFv2::WebACL')).map(
    (resource) => (resource as { Properties: Props }).Properties,
  );
}

function ruleModes(acl: Props): Record<string, string> {
  return Object.fromEntries(
    (acl.Rules as Props[]).map((rule) => [
      rule.Name as string,
      JSON.stringify(rule.OverrideAction ?? rule.Action),
    ]),
  );
}

function rule(acl: Props, name: string): Props {
  const found = (acl.Rules as Props[]).find((candidate) => candidate.Name === name);
  if (found === undefined) throw new Error(`no rule ${name}`);
  return found;
}

/** sha256 of each ACL's rendered `Rules` under TEST_CONFIG, read from the 438b7edd renderer. */
// pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
const EDGE_RULES_SHA256 = '7494630e05620069a48c776da9e0137484791c6a9e108521a6a1c09b584ff1c5';
// pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
const USER_POOL_RULES_SHA256 = '5b74b5fe1e1687504b93ce00882cbf7a72cfd9b0d56e0c8f1810a00920cf680e';

const NONE = [{ Priority: 0, Type: 'NONE' }];
const EXEMPT_ROUTE = {
  AndStatement: {
    Statements: [
      {
        ByteMatchStatement: {
          FieldToMatch: { Method: {} },
          PositionalConstraint: 'EXACTLY',
          SearchString: 'POST',
          TextTransformations: NONE,
        },
      },
      {
        ByteMatchStatement: {
          FieldToMatch: { UriPath: {} },
          PositionalConstraint: 'EXACTLY',
          SearchString: '/api/prompt-registry/create-version',
          TextTransformations: NONE,
        },
      },
      {
        SizeConstraintStatement: {
          FieldToMatch: { QueryString: {} },
          ComparisonOperator: 'EQ',
          Size: 0,
          TextTransformations: NONE,
        },
      },
    ],
  },
};
const bodySize = (size: number): Props => ({
  SizeConstraintStatement: {
    FieldToMatch: { Body: { OversizeHandling: 'MATCH' } },
    ComparisonOperator: 'GT',
    Size: size,
    TextTransformations: NONE,
  },
});

/** The paths of every AND statement that sits directly inside another AND, which AWS WAF refuses. */
function andInsideAnd(node: unknown, at = '$', parentIsAnd = false): string[] {
  if (Array.isArray(node)) return node.flatMap((item, index) => andInsideAnd(item, `${at}[${index}]`, parentIsAnd));
  if (node === null || typeof node !== 'object') return [];
  return Object.entries(node as Props).flatMap(([key, value]) => {
    if (key === 'AndStatement') {
      const statements = (value as Props).Statements;
      return [...(parentIsAnd ? [at] : []), ...andInsideAnd(statements, `${at}.AndStatement.Statements`, true)];
    }
    return andInsideAnd(value, `${at}.${key}`, false);
  });
}

/** Every `SizeConstraintStatement` under a rendered statement, with the path to it. */
function sizeConstraints(node: unknown, at = '$'): { at: string; statement: Props }[] {
  if (Array.isArray(node)) return node.flatMap((item, index) => sizeConstraints(item, `${at}[${index}]`));
  if (node === null || typeof node !== 'object') return [];
  return Object.entries(node as Props).flatMap(([key, value]) => [
    ...(key === 'SizeConstraintStatement' ? [{ at: `${at}.${key}`, statement: value as Props }] : []),
    ...sizeConstraints(value, `${at}.${key}`),
  ]);
}

/** A one-rule edge ACL, for the construct guards. */
function guardedAcl(props: Partial<GuardedWebAclProps> & { rules: readonly WebAclRule[] }): void {
  const stack = new cdk.Stack(new cdk.App(), 'Guard', { env: { account: '111122223333', region: 'us-east-1' } });
  new GuardedWebAcl(stack, 'Acl', {
    name: 'guard',
    scope: 'CLOUDFRONT',
    logRetention: logs.RetentionDays.ONE_DAY,
    ...props,
  });
}

describe('the distribution web ACL (EdgeSecurityStack)', () => {
  it('is CLOUDFRONT-scoped with the six rules blocking, in order', () => {
    const [acl] = acls(edge);
    expect(acls(edge)).toHaveLength(1);
    expect(acl).toEqual(expect.objectContaining({ Scope: 'CLOUDFRONT', DefaultAction: { Allow: {} } }));
    expect(ruleModes(acl!)).toEqual({
      AwsIpReputation: '{"None":{}}',
      AwsKnownBadInputs: '{"None":{}}',
      AwsCommonRuleSet: '{"None":{}}',
      PromptCreateBodyCeiling: '{"Block":{}}',
      BodySizeOutsidePromptCreate: '{"Block":{}}',
      RateLimitPerSourceIp: '{"Block":{}}',
    });
    expect((acl!.Rules as Props[]).map((candidate) => [candidate.Name, candidate.Priority])).toEqual([
      ['AwsIpReputation', 0],
      ['AwsKnownBadInputs', 1],
      ['AwsCommonRuleSet', 2],
      ['PromptCreateBodyCeiling', 3],
      ['BodySizeOutsidePromptCreate', 4],
      ['RateLimitPerSourceIp', 5],
    ]);
    const rate = (acl!.Rules as Props[]).find((rule) => rule.Name === 'RateLimitPerSourceIp')!;
    expect((rate.Statement as Props).RateBasedStatement).toEqual({
      Limit: 2000,
      AggregateKeyType: 'IP',
      EvaluationWindowSec: 300,
    });
  });

  it('never samples requests, because a sample carries every header unredacted', () => {
    const [acl] = acls(edge);
    const configs = [
      acl!.VisibilityConfig,
      ...(acl!.Rules as Props[]).map((rule) => rule.VisibilityConfig),
    ] as Props[];
    for (const visibility of configs) expect(visibility.SampledRequestsEnabled).toBe(false);
  });

  it('logs only counted or blocked requests, with the credential headers redacted', () => {
    edge.hasResourceProperties('AWS::WAFv2::LoggingConfiguration', {
      RedactedFields: REDACTED_HEADERS.map((name) => ({ SingleHeader: { Name: name } })),
      LoggingFilter: {
        DefaultBehavior: 'DROP',
        Filters: [
          Match.objectLike({
            Behavior: 'KEEP',
            Conditions: [
              { ActionCondition: { Action: 'COUNT' } },
              { ActionCondition: { Action: 'EXCLUDED_AS_COUNT' } },
              { ActionCondition: { Action: 'BLOCK' } },
            ],
          }),
        ],
      },
    });
    expect(REDACTED_HEADERS).toEqual([ORIGIN_VERIFY_HEADER, 'authorization', 'cookie']);
    edge.hasResourceProperties('AWS::Logs::LogGroup', { LogGroupName: 'aws-waf-logs-adlytics-dev-edge' });
  });

  it('publishes its ARN in us-east-1 for FrontendStack', () => {
    edge.hasResourceProperties('AWS::SSM::Parameter', {
      Name: paramPath('dev', 'edgeWebAclArn'),
      Value: { 'Fn::GetAtt': [Match.anyValue(), 'Arn'] },
    });
  });

  it('refuses to synthesise outside us-east-1', () => {
    const app = new cdk.App();
    expect(
      () =>
        new EdgeSecurityStack(app, 'Elsewhere', {
          config: TEST_CONFIG,
          env: { account: TEST_CONFIG.account, region: 'eu-west-1' },
        }),
    ).toThrow(/must be deployed to us-east-1/);
  });
});

describe('the path-scoped body-size exclusion (ADR-078 §1, refined 2026-10-05)', () => {
  it('counts only SizeRestrictions_BODY inside the common rule set, with no scope-down', () => {
    const group = (rule(acls(edge)[0]!, 'AwsCommonRuleSet').Statement as Props).ManagedRuleGroupStatement as Props;
    expect(group.RuleActionOverrides).toEqual([{ Name: 'SizeRestrictions_BODY', ActionToUse: { Count: {} } }]);
    expect(group).not.toHaveProperty('ScopeDownStatement');
    for (const name of ['AwsIpReputation', 'AwsKnownBadInputs']) {
      const other = (rule(acls(edge)[0]!, name).Statement as Props).ManagedRuleGroupStatement as Props;
      expect(other).not.toHaveProperty('RuleActionOverrides');
    }
  });

  it('caps the exact create-version route at 32,768 bytes', () => {
    const ceiling = rule(acls(edge)[0]!, 'PromptCreateBodyCeiling');
    expect(ceiling.Action).toEqual({ Block: {} });
    // One flat AND: the exact route's three statements beside the size statement.
    expect(ceiling.Statement).toEqual({
      AndStatement: { Statements: [...EXEMPT_ROUTE.AndStatement.Statements, bodySize(32768)] },
    });
    expect(PROMPT_CREATE_VERSION_MAX_BODY_BYTES).toBe(32768);
  });

  it('nests no AND directly inside an AND, which AWS WAF refuses at deploy', () => {
    // Positive control: the shape the first dev deploy sent, refused by WAF on 2026-10-06.
    const refused = { AndStatement: { Statements: [EXEMPT_ROUTE, bodySize(32768)] } };
    expect(andInsideAnd(refused)).toEqual(['$.AndStatement.Statements[0]']);
    // An AND under a NOT under an AND is accepted, so the outside rule keeps its shape.
    expect(andInsideAnd({ AndStatement: { Statements: [bodySize(8192), { NotStatement: { Statement: EXEMPT_ROUTE } }] } })).toEqual([]);
    for (const acl of [...acls(edge), ...acls(auth)]) {
      expect(andInsideAnd(acl.Rules)).toEqual([]);
    }
  });

  it('refuses a body over 8,192 bytes on every other request, with no label match', () => {
    const outside = rule(acls(edge)[0]!, 'BodySizeOutsidePromptCreate');
    expect(outside.Action).toEqual({ Block: {} });
    expect(outside.Statement).toEqual({
      AndStatement: { Statements: [bodySize(8192), { NotStatement: { Statement: EXEMPT_ROUTE } }] },
    });
    expect(JSON.stringify(outside)).not.toContain('LabelMatchStatement');
    expect(EDGE_DEFAULT_MAX_BODY_BYTES).toBe(8192);
  });

  it('sets OversizeHandling MATCH on the body field of every body size constraint, never on the statement', () => {
    for (const name of ['PromptCreateBodyCeiling', 'BodySizeOutsidePromptCreate']) {
      const found = sizeConstraints(rule(acls(edge)[0]!, name).Statement);
      const onBody = found.filter(({ statement }) => (statement.FieldToMatch as Props).Body !== undefined);
      expect(onBody).toHaveLength(1);
      for (const { at, statement } of onBody) {
        expect([at, ((statement.FieldToMatch as Props).Body as Props).OversizeHandling]).toEqual([at, 'MATCH']);
        expect([at, Object.keys(statement).sort()]).toEqual([
          at,
          ['ComparisonOperator', 'FieldToMatch', 'Size', 'TextTransformations'],
        ]);
      }
    }
  });

  it('inspects the first 32 KB of a CloudFront request body', () => {
    expect(acls(edge)[0]!.AssociationConfig).toEqual({
      RequestBody: { CLOUDFRONT: { DefaultSizeInspectionLimit: 'KB_32' } },
    });
  });

  it('exempts a route the API stack declares, so the two cannot drift apart', () => {
    const routeKeys = Object.values(api.findResources('AWS::ApiGatewayV2::Route')).map(
      (resource) => (resource as { Properties: Props }).Properties.RouteKey,
    );
    expect(routeKeys).toContain(`${PROMPT_CREATE_VERSION_ROUTE.method} ${PROMPT_CREATE_VERSION_ROUTE.path}`);
    expect(routeKeys).toContain('POST /api/prompt-registry/create-version');
  });

  it('leaves the user pool ACL without overrides, body-size rules or an inspection limit', () => {
    const [acl] = acls(auth);
    expect(acl).not.toHaveProperty('AssociationConfig');
    expect(JSON.stringify(acl)).not.toContain('RuleActionOverrides');
    expect(sizeConstraints(acl!.Rules)).toEqual([]);
  });

  it('refuses at synth a body-size statement whose body field drops OversizeHandling MATCH', () => {
    const ceiling: WebAclRule = {
      kind: 'body-size',
      name: 'Ceiling',
      maxBodyBytes: 1024,
      route: { only: PROMPT_CREATE_VERSION_ROUTE },
      mode: 'block',
    };
    const withoutField = (size: number) => {
      const rendered = renderBodySize(size).sizeConstraintStatement as unknown as Props;
      return { sizeConstraintStatement: { ...rendered, fieldToMatch: { body: {} } } } as never;
    };
    const onStatement = (size: number) => {
      const rendered = renderBodySize(size).sizeConstraintStatement as unknown as Props;
      return {
        sizeConstraintStatement: { ...rendered, oversizeHandling: 'MATCH' },
      } as never;
    };
    // Positive control: the production renderer passes the same guard.
    expect(() => guardedAcl({ rules: [ceiling] })).not.toThrow();
    expect(() => guardedAcl({ rules: [ceiling], bodySizeRenderOverride: withoutField })).toThrow(
      /rule Ceiling: a body size constraint must set fieldToMatch\.body\.oversizeHandling to MATCH/,
    );
    expect(() => guardedAcl({ rules: [ceiling], bodySizeRenderOverride: onStatement })).toThrow(
      /rule Ceiling: oversizeHandling is set outside fieldToMatch\.body/,
    );
  });

  it.each([
    ['a trailing slash', '/api/prompt-registry/create-version/'],
    ['a query', '/api/prompt-registry/create-version?v=1'],
    ['an escape', '/api/prompt-registry/create%2Dversion'],
    ['a doubled slash', '/api//prompt-registry/create-version'],
    ['a backslash', '/api\\prompt-registry/create-version'],
    ['a dot segment', '/api/./prompt-registry/create-version'],
    ['a dot-dot segment', '/api/x/../prompt-registry/create-version'],
    ['no leading slash', 'api/prompt-registry/create-version'],
  ])('refuses at synth an exempt path with %s', (_why, path) => {
    const rules: WebAclRule[] = [
      { kind: 'body-size', name: 'Ceiling', maxBodyBytes: 1024, route: { only: { method: 'POST', path } }, mode: 'block' },
    ];
    expect(() => guardedAcl({ rules })).toThrow(/exact route path .* is not canonical/);
  });

  it.each([
    ['0', 0, undefined],
    ['above the 16 KB default limit', 16_385, undefined],
    ['above the KB_32 limit', 32_769, 'KB_32' as const],
  ])('refuses at synth a maxBodyBytes of %s', (_why, maxBodyBytes, bodyInspectionLimit) => {
    const rules: WebAclRule[] = [
      { kind: 'body-size', name: 'Ceiling', maxBodyBytes, route: { except: PROMPT_CREATE_VERSION_ROUTE }, mode: 'block' },
    ];
    expect(() => guardedAcl({ rules, ...(bodyInspectionLimit === undefined ? {} : { bodyInspectionLimit }) })).toThrow(
      /rule Ceiling: maxBodyBytes \d+ must be a whole number from 1 to the body inspection limit/,
    );
  });

  it('accepts a maxBodyBytes at the inspection limit (positive control)', () => {
    const rules: WebAclRule[] = [
      { kind: 'body-size', name: 'Ceiling', maxBodyBytes: 32_768, route: { only: PROMPT_CREATE_VERSION_ROUTE }, mode: 'block' },
    ];
    expect(() => guardedAcl({ rules, bodyInspectionLimit: 'KB_32' })).not.toThrow();
  });

  it('renders an all-requests ceiling as one bare size statement on the body (ADR-150)', () => {
    const stack = new cdk.Stack(new cdk.App(), 'AllRequests', { env: { account: '111122223333', region: 'us-east-1' } });
    new GuardedWebAcl(stack, 'Acl', {
      name: 'all-requests',
      scope: 'REGIONAL',
      logRetention: logs.RetentionDays.ONE_DAY,
      rules: [{ kind: 'body-size', name: 'BodySizeAll', maxBodyBytes: 8192, route: { all: true }, mode: 'count' }],
    });
    const [acl] = acls(Template.fromStack(stack));
    const ceiling = rule(acl!, 'BodySizeAll');
    expect(ceiling.Action).toEqual({ Count: {} });
    expect(ceiling.Statement).toEqual(bodySize(8192));
    expect(JSON.stringify(ceiling)).not.toMatch(/AndStatement|NotStatement|ByteMatchStatement/);
  });

  it('still refuses an all-requests ceiling whose body field drops OversizeHandling MATCH', () => {
    const ceiling: WebAclRule = { kind: 'body-size', name: 'BodySizeAll', maxBodyBytes: 1024, route: { all: true }, mode: 'block' };
    const withoutField = (size: number) => {
      const rendered = renderBodySize(size).sizeConstraintStatement as unknown as Props;
      return { sizeConstraintStatement: { ...rendered, fieldToMatch: { body: {} } } } as never;
    };
    // Positive control: the production renderer passes the same guard.
    expect(() => guardedAcl({ rules: [ceiling] })).not.toThrow();
    expect(() => guardedAcl({ rules: [ceiling], bodySizeRenderOverride: withoutField })).toThrow(
      /rule BodySizeAll: a body size constraint must set fieldToMatch\.body\.oversizeHandling to MATCH/,
    );
  });

  it('renders the edge and user pool rules byte-for-byte as before the all-requests scope (ADR-150)', () => {
    // Pinned against the renderer at 438b7edd, before the `{ all: true }` scope existed.
    const digest = (acl: Props): string =>
      crypto.createHash('sha256').update(JSON.stringify(acl.Rules)).digest('hex');
    expect(acls(edge).map(digest)).toEqual([EDGE_RULES_SHA256]);
    expect(acls(auth).map(digest)).toEqual([USER_POOL_RULES_SHA256]);
  });

  it('refuses at synth a body inspection limit on a REGIONAL ACL', () => {
    expect(() => guardedAcl({ rules: [], scope: 'REGIONAL', bodyInspectionLimit: 'KB_32' })).toThrow(
      /bodyInspectionLimit applies only to a CLOUDFRONT ACL, not REGIONAL/,
    );
  });
});

describe('the distribution (FrontendStack)', () => {
  function distribution(template: Template): Props {
    const [resource] = Object.values(template.findResources('AWS::CloudFront::Distribution'));
    return ((resource as { Properties: Props }).Properties.DistributionConfig) as Props;
  }

  /**
   * A-IAC-BASELINE-001: with the default *.cloudfront.net certificate CloudFront fixes the viewer
   * policy at TLSv1, and CDK ignores `minimumProtocolVersion` with a warning. The stack must not
   * state a TLS minimum it does not deliver; the README's "Viewer TLS" row says what to do.
   */
  const IGNORED_TLS = "Ignoring 'minimumProtocolVersion'";
  function tlsWarnings(stack: cdk.Stack): { id: string; entry: { data: unknown } }[] {
    return Annotations.fromStack(stack).findWarning('*', Match.stringLikeRegexp(IGNORED_TLS)) as never;
  }
  function probeDistribution(props: Partial<cloudfront.DistributionProps>): cdk.Stack {
    const stack = new cdk.Stack(new cdk.App(), 'Probe', {
      env: { account: '111122223333', region: 'us-east-1' },
    });
    new cloudfront.Distribution(stack, 'D', {
      defaultBehavior: { origin: new origins.HttpOrigin('origin.example.test') },
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      ...props,
    });
    return stack;
  }

  it('warns when a TLS minimum is set without a custom certificate', () => {
    const warnings = tlsWarnings(probeDistribution({}));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]?.id).toBe('/Probe/D');
    expect(String(warnings[0]?.entry.data)).toMatch(
      /^Ignoring 'minimumProtocolVersion': it has no effect without a custom 'certificate'\./,
    );
  });

  it('applies the TLS minimum when a custom certificate and domain are set', () => {
    const stack = probeDistribution({
      certificate: acm.Certificate.fromCertificateArn(
        new cdk.Stack(new cdk.App(), 'Unused'), 'C',
        'arn:aws:acm:us-east-1:111122223333:certificate/00000000-0000-4000-8000-000000000001',
      ),
      domainNames: ['www.example.test'],
    });
    expect(tlsWarnings(stack)).toEqual([]);
    const [resource] = Object.values(Template.fromStack(stack).findResources('AWS::CloudFront::Distribution'));
    const config = (resource as { Properties: Props }).Properties.DistributionConfig as Props;
    expect((config.ViewerCertificate as Props).MinimumProtocolVersion).toBe('TLSv1.2_2021');
  });

  it('states no viewer TLS minimum the default certificate would ignore', () => {
    const stack = new FrontendStack(new cdk.App(), 'Frontend', {
      config: inRegion('us-east-1'),
      env: { account: TEST_CONFIG.account, region: 'us-east-1' },
      routeAllowlistPath: ROUTE_ALLOWLIST,
    });
    expect(tlsWarnings(stack)).toEqual([]);
    expect(distribution(edgeRegionFrontend).ViewerCertificate).toBeUndefined();
  });

  it('in us-east-1, reads the web ACL ARN as a plain parameter', () => {
    const config = distribution(edgeRegionFrontend);
    expect(config.WebACLId).toEqual({ Ref: expect.stringContaining('SsmParameter') });
    const parameters = (edgeRegionFrontend.toJSON().Parameters ?? {}) as Record<string, { Default?: unknown }>;
    expect(parameters[(config.WebACLId as { Ref: string }).Ref]?.Default).toBe(
      paramPath('dev', 'edgeWebAclArn'),
    );
    edgeRegionFrontend.resourceCountIs('Custom::AWS', 0);
  });

  it('in another region, reads it through one SDK call pinned to us-east-1', () => {
    const config = distribution(otherRegionFrontend);
    expect(config.WebACLId).toEqual({ 'Fn::GetAtt': [expect.any(String), 'Parameter.Value'] });
    const [read] = Object.values(otherRegionFrontend.findResources('Custom::AWS')) as { Properties: Props }[];
    // Rendered as a JSON string property the custom resource handler parses.
    const call = JSON.parse(read!.Properties.Create as string) as Props;
    expect(call).toEqual(
      expect.objectContaining({
        service: 'SSM',
        action: 'getParameter',
        region: 'us-east-1',
        parameters: { Name: paramPath('dev', 'edgeWebAclArn') },
      }),
    );
    // Least privilege: the one parameter, in us-east-1.
    const statements = Object.values(otherRegionFrontend.findResources('AWS::IAM::Policy')).flatMap(
      (policy) => ((policy as { Properties: Props }).Properties.PolicyDocument as Props).Statement as Props[],
    );
    const ssmStatements = statements.filter((statement) => JSON.stringify(statement.Action).includes('ssm:'));
    expect(ssmStatements).toHaveLength(1);
    expect(ssmStatements[0]!.Action).toBe('ssm:GetParameter');
    expect(JSON.stringify(ssmStatements[0]!.Resource)).toContain(':ssm:us-east-1:');
    expect(JSON.stringify(ssmStatements[0]!.Resource)).toContain('parameter/adlytics/dev/edge/web-acl-arn');
  });

  it('adds the origin-verify header to the API origin only, from the secret it owns', () => {
    const origins = distribution(edgeRegionFrontend).Origins as Props[];
    const withHeader = origins.filter((origin) => origin.OriginCustomHeaders !== undefined);
    expect(withHeader).toHaveLength(1);
    expect(JSON.stringify(withHeader[0]!.DomainName)).toContain('execute-api');
    const [header] = withHeader[0]!.OriginCustomHeaders as Props[];
    expect(header!.HeaderName).toBe(ORIGIN_VERIFY_HEADER);
    const value = JSON.stringify(header!.HeaderValue);
    expect(value).toContain('{{resolve:secretsmanager:');
    expect(value).toContain(':SecretString:current::}}');
    edgeRegionFrontend.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: originVerifySecretName('dev'),
      GenerateSecretString: Match.objectLike({ GenerateStringKey: 'current', ExcludePunctuation: true }),
    });
  });

  it('pins the reference to a version only during a rotation', () => {
    const versionId = '0f1e2d3c-4b5a-4968-8776-655443322110';
    const pinned = frontend({
      ...inRegion('us-east-1'),
      edge: { ...TEST_CONFIG.edge, originVerifySecretVersionId: versionId },
    });
    const origins = distribution(pinned).Origins as Props[];
    const header = (origins.find((origin) => origin.OriginCustomHeaders)!.OriginCustomHeaders as Props[])[0]!;
    expect(JSON.stringify(header.HeaderValue)).toContain(`:SecretString:current::${versionId}}}`);
  });
});

describe('the user pool web ACL (AuthStack)', () => {
  it('is REGIONAL, blocking, and associated with the pool itself', () => {
    const [acl] = acls(auth);
    expect(acls(auth)).toHaveLength(1);
    expect(acl!.Scope).toBe('REGIONAL');
    expect(ruleModes(acl!)).toEqual({
      AwsIpReputation: '{"None":{}}',
      RateLimitPerSourceIp: '{"Block":{}}',
    });
    auth.hasResourceProperties('AWS::WAFv2::WebACLAssociation', {
      ResourceArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('^UserPool'), 'Arn'] },
    });
    auth.hasResourceProperties('AWS::WAFv2::LoggingConfiguration', {
      RedactedFields: REDACTED_HEADERS.map((name) => ({ SingleHeader: { Name: name } })),
    });
  });
});

describe('the origin-verify lock (ApiStack)', () => {
  it('gives both checking functions the secret name and the cache rule', () => {
    for (const name of ['adlytics-dev-AuthFunction', 'adlytics-dev-AuthorizerFunction']) {
      const fn = Object.values(api.findResources('AWS::Lambda::Function'))
        .map((resource) => (resource as { Properties: Props }).Properties)
        .find((properties) => properties.FunctionName === name)!;
      expect((fn.Environment as { Variables: Props }).Variables).toEqual(
        expect.objectContaining({
          ADL_ORIGIN_VERIFY_SECRET_ID: originVerifySecretName('dev'),
          ADL_ORIGIN_VERIFY_MAX_CACHE_AGE_SECONDS: '300',
          ADL_ORIGIN_VERIFY_REREAD_INTERVAL_SECONDS: '60',
        }),
      );
    }
  });

  it('alarms on every refusal either function logs', () => {
    const filters = Object.values(api.findResources('AWS::Logs::MetricFilter')).map(
      (resource) => (resource as { Properties: Props }).Properties,
    ).filter((filter) => JSON.stringify(filter).includes('origin_verify_refused'));
    expect(filters).toHaveLength(2);
    api.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'adlytics-dev-origin-verify-refused',
      Namespace: 'Adlytics/Edge',
      MetricName: 'OriginVerifyRefused',
      Threshold: 0,
      ComparisonOperator: 'GreaterThanThreshold',
    });
  });

  it('fails synth when any authorizer caches its result', () => {
    const app = new cdk.App();
    const env = { account: TEST_CONFIG.account, region: TEST_CONFIG.region };
    const stack = new ApiStack(app, 'CachingApi', { config: TEST_CONFIG, env, sourceAssetPath: __dirname });
    const authorizer = stack.node
      .findAll()
      .find((node): node is apigwv2.CfnAuthorizer => node instanceof apigwv2.CfnAuthorizer)!;
    authorizer.authorizerResultTtlInSeconds = 300;
    expect(() => app.synth()).toThrow(/ADR-078 requires a TTL of 0/);
  });

  it('fails synth when an authorizer leaves its TTL to the service default', () => {
    const app = new cdk.App();
    const env = { account: TEST_CONFIG.account, region: TEST_CONFIG.region };
    const stack = new ApiStack(app, 'DefaultTtlApi', { config: TEST_CONFIG, env, sourceAssetPath: __dirname });
    const authorizer = stack.node
      .findAll()
      .find((node): node is apigwv2.CfnAuthorizer => node instanceof apigwv2.CfnAuthorizer)!;
    authorizer.authorizerResultTtlInSeconds = undefined;
    expect(() => app.synth()).toThrow(/does not declare a result TTL of 0/);
  });

  it('synthesises with the TTL at zero', () => {
    expect(apiStack.node.validate()).toEqual([]);
  });
});

describe('the configuration', () => {
  const supplied: Record<string, string> = {
    CDK_DEFAULT_ACCOUNT: '111122223333',
    CDK_DEFAULT_REGION: 'eu-west-1',
    ADLYTICS_QUICK_CHAT_MODEL_ID: 'test.quick-chat-model-v1:0',
    ADLYTICS_DEEP_DIVE_MODEL_ID: 'test.deep-dive-model-v1:0',
    ADLYTICS_MEMORY_MODEL_ID: 'test.memory-model-v1:0',
    ADLYTICS_LOGIN_DOMAIN_PREFIX: 'test-login-domain',
    ADLYTICS_RECEIPT_RETENTION_DAYS: '90',
    ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: '8',
    ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES: '2000',
    ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES: '300',
  };
  function withEnv<T>(values: Record<string, string>, body: () => T): T {
    const saved = { ...process.env };
    try {
      for (const key of Object.keys(process.env)) if (key.startsWith('ADLYTICS_')) delete process.env[key];
      Object.assign(process.env, values);
      return body();
    } finally {
      process.env = saved;
    }
  }

  it('requires both rate limits, with no default, and names each variable', () => {
    const { ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES: _edge, ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES: _pool, ...rest } =
      supplied;
    withEnv(rest, () => {
      expect(() => getConfig('dev')).toThrow(/edge\.wafRateLimitPerFiveMinutes.*auth\.wafRateLimitPerFiveMinutes|auth\.wafRateLimitPerFiveMinutes.*edge\.wafRateLimitPerFiveMinutes/s);
    });
    withEnv(supplied, () => {
      const config = getConfig('dev');
      expect(config.edge.wafRateLimitPerFiveMinutes).toBe(2000);
      expect(config.auth.wafRateLimitPerFiveMinutes).toBe(300);
      expect(config.edge).not.toHaveProperty('originVerifySecretVersionId');
    });
  });

  it.each(['9', '0', '1.5', 'many', '2000000001'])('refuses a rate limit of %s', (value) => {
    withEnv({ ...supplied, ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES: value }, () => {
      expect(() => getConfig('dev')).toThrow(/whole number of requests from 10 to 2000000000/);
    });
  });

  it('accepts only a version id as the rotation pin', () => {
    withEnv({ ...supplied, ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID: 'AWSCURRENT' }, () => {
      expect(() => getConfig('dev')).toThrow(/must be a Secrets Manager version id/);
    });
  });
});

describe('the create-version body ceiling', () => {
  it('is one value at the edge, in the handler and on the admin page', () => {
    const root = path.join(__dirname, '..', '..');
    const handler = fs.readFileSync(path.join(root, 'src', 'adapters', 'inbound', 'prompt_admin', 'handler.py'), 'utf8');
    const page = fs.readFileSync(path.join(root, 'frontend', 'components', 'PromptAdminPage.tsx'), 'utf8');
    const grouped = PROMPT_CREATE_VERSION_MAX_BODY_BYTES.toLocaleString('en-US').replace(',', '_');
    expect(grouped).toBe('32_768');
    expect(handler).toContain(`\nMAX_BODY_BYTES = ${grouped}\n`);
    expect(page).toContain(`export const PROMPT_BODY_MAX_BYTES = ${grouped};`);
  });
});

describe('the header name', () => {
  it('is the one the Python checks read', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'src', 'domain', 'identity', 'origin_verify.py'),
      'utf8',
    );
    expect(source).toContain(`ORIGIN_VERIFY_HEADER = "${ORIGIN_VERIFY_HEADER}"`);
  });

  it('names the secret the rotation job writes', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'src', 'adapters', 'jobs', 'rotate_origin_verify', '__main__.py'),
      'utf8',
    );
    expect(originVerifySecretName('dev')).toBe('adlytics/dev/edge/origin-verify');
    expect(source).toContain('return f"adlytics/{environment}/edge/origin-verify"');
  });
});
