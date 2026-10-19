// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import {
  API_PREFIX,
  AppConfig,
  clientSecretName,
  paramPath,
  semanticV2AthenaWorkgroupName,
  semanticV2StateTableName,
} from '../lib/config';
import { ApiStack } from '../lib/stacks/api-stack';
import { AuthStack, REVIEW_CLI_CALLBACK_URL } from '../lib/stacks/auth-stack';
import { FrontendStack } from '../lib/stacks/frontend-stack';
import { TEST_CONFIG } from './support/config';
import { ROUTE_ALLOWLIST } from './support/templates';

/**
 * A local alias so the assertions below read unchanged. Why a configuration is built
 * for tests rather than read from lib/config, and why `apiOriginAttached` is true
 * here but false as shipped, is in test/support/config.ts.
 */
const config: AppConfig = TEST_CONFIG;
type Props = Record<string, unknown>;

function only<T>(items: T[], what: string): T {
  expect(items).toHaveLength(1);
  const [first] = items;
  if (first === undefined) throw new Error(`expected exactly one ${what}`);
  return first;
}

function functionByName(template: Template, name: string): Props {
  return only(
    Object.values(template.findResources('AWS::Lambda::Function'))
      .map((resource) => (resource as { Properties: Props }).Properties)
      .filter((properties) => properties.FunctionName === name),
    `Lambda named ${name}`,
  );
}

function roleLogicalId(template: Template, functionName: string): string {
  const role = functionByName(template, functionName).Role as
    | { 'Fn::GetAtt'?: unknown[] }
    | undefined;
  const logicalId = role?.['Fn::GetAtt']?.[0];
  if (typeof logicalId !== 'string') throw new Error(`${functionName} has no role logical id`);
  return logicalId;
}

function statementsForFunction(template: Template, functionName: string): Props[] {
  const role = roleLogicalId(template, functionName);
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .map((resource) => (resource as { Properties: Props }).Properties)
    .filter((properties) =>
      ((properties.Roles ?? []) as Props[]).some((candidate) => candidate.Ref === role),
    )
    .flatMap(
      (properties) =>
        (((properties.PolicyDocument as Props).Statement ?? []) as Props[]),
    );
}

function actions(statement: Props): string[] {
  const value = statement.Action;
  return (Array.isArray(value) ? value : [value]).filter(
    (action): action is string => typeof action === 'string',
  );
}

function templates(
  withConfig: AppConfig = config,
): { auth: Template; api: Template; frontend: Template } {
  const config = withConfig;
  const app = new cdk.App();
  const env = { account: config.account, region: config.region };
  const auth = new AuthStack(app, 'Auth', { config, env });
  const api = new ApiStack(app, 'Api', {
    config,
    env,
    sourceAssetPath: __dirname,
  });
  const frontend = new FrontendStack(app, 'Frontend', { config, env, routeAllowlistPath: ROUTE_ALLOWLIST });
  return {
    auth: Template.fromStack(auth),
    api: Template.fromStack(api),
    frontend: Template.fromStack(frontend),
  };
}

describe('AuthStack', () => {
  it('retains the user pool', () => {
    // The reason this stack exists. Destroying an HTTP API is a redeploy;
    // destroying a user pool deletes every account and no restore brings them back.
    templates().auth.hasResource('AWS::Cognito::UserPool', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  it('declares the two ADR-078 review groups and none of the retired per-level ones', () => {
    const groups = Object.values(templates().auth.findResources('AWS::Cognito::UserPoolGroup'))
      .map((group) => (group as { Properties: { GroupName: string } }).Properties.GroupName)
      .sort();
    expect(groups).toEqual([
      'audit-attribution',
      'operations-run-requesters',
      'operations-status-read',
      'prompt-admin',
      'quality-reviewer',
    ]);
  });

  it('projects prompt administration and operational status reads from distinct Cognito groups', () => {
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolGroup', {
      GroupName: 'prompt-admin',
    });
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolGroup', {
      GroupName: 'operations-status-read',
    });
  });

  it('does not allow self sign-up', () => {
    templates().auth.hasResourceProperties('AWS::Cognito::UserPool', {
      AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
    });
  });

  it('issues a client secret for the confidential BFF client', () => {
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      GenerateSecret: true,
    });
  });

  it('enables only the authorization code grant', () => {
    // The implicit grant returns tokens in a URL fragment, which is precisely
    // what this architecture exists to prevent.
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthFlows: ['code'],
    });
  });

  it('enables no password-based auth flow on any client outside dev validation', () => {
    // InitiateAuth cannot sign in a federated user, so enabling a password flow
    // would quietly foreclose corporate SSO — see ADR-007.
    const clients = Object.values(
      templates().auth.findResources('AWS::Cognito::UserPoolClient'),
    ) as { Properties: { ExplicitAuthFlows?: string[] } }[];
    expect(clients).toHaveLength(2);
    for (const client of clients) {
      const flows = client.Properties.ExplicitAuthFlows ?? [];
      expect(flows.filter((flow) => flow.includes('PASSWORD'))).toHaveLength(0);
    }
  });

  it('registers the exact callback URL the handler derives', () => {
    // Cognito compares this string on both the authorize and the token call. A
    // trailing slash difference is a rejected login, not a warning.
    //
    // The host arrives at deploy from frontend/public-base-url, so only the path
    // is assertable here — and the path is the half that has to agree with the
    // Python handler. Match.anyValue() over the host is not laxness: asserting a
    // Ref's logical ID would break on any unrelated construct reorder.
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      CallbackURLs: [{ 'Fn::Join': ['', [Match.anyValue(), '/api/auth/callback']] }],
    });
  });

  it('enables token revocation so logout can mean something', () => {
    templates().auth.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      EnableTokenRevocation: true,
    });
  });

  it('stores the client secret in Secrets Manager, not an environment variable', () => {
    templates().auth.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: clientSecretName('dev'),
    });
  });

  it('publishes the parameters consumers read', () => {
    const template = templates().auth;
    for (const key of ['userPoolId', 'userPoolClientId', 'userPoolPublicClientId', 'authDomain'] as const) {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: paramPath('dev', key),
      });
    }
  });

  it('reads the client secret through its own function, writing only its own log group (ADR-085)', () => {
    const template = templates().auth;
    const resources = (template.toJSON() as Props).Resources as Record<string, { Type: string; Properties?: Props }>;
    expect(Object.entries(resources)
      .filter(([, resource]) => resource.Type === 'AWS::IAM::Role'
        && resource.Properties?.ManagedPolicyArns !== undefined)
      .map(([id]) => id)).toEqual([]);
    const logsId = only(
      Object.entries(resources)
        .filter(([id, resource]) => resource.Type === 'AWS::Logs::LogGroup' && id.startsWith('ClientSecretReadLogs'))
        .map(([id]) => id),
      'ClientSecretReadLogs',
    );
    expect(functionByName(template, 'adlytics-dev-ClientSecretRead').LoggingConfig)
      .toEqual({ LogGroup: { Ref: logsId } });
    const [readId, read] = only(
      Object.entries(resources).filter(([, resource]) => resource.Type === 'Custom::AWS'),
      'AwsCustomResource',
    );
    // The response carries the secret, so the function must never log it.
    const update = JSON.stringify(read.Properties?.Update);
    expect(update).toContain('describeUserPoolClient');
    expect(update).toContain('\\"logApiResponseData\\":false');
    const secret = only(
      Object.values(resources).filter((resource) => resource.Type === 'AWS::SecretsManager::Secret'),
      'client secret',
    );
    expect(secret.Properties?.SecretString).toEqual({ 'Fn::GetAtt': [readId, 'UserPoolClient.ClientSecret'] });
  });
});

describe('the public review CLI client (ADR-078)', () => {
  const client = (template: Template, name: string): Props =>
    only(
      Object.values(template.findResources('AWS::Cognito::UserPoolClient'))
        .map((resource) => (resource as { Properties: Props }).Properties)
        .filter((properties) => properties.ClientName === name),
      `client ${name}`,
    );

  it('is a secretless authorization-code client with openid only and one loopback callback', () => {
    const properties = client(templates().auth, 'adlytics-review-cli-dev');
    expect(properties).toStrictEqual({
      UserPoolId: expect.any(Object),
      ClientName: 'adlytics-review-cli-dev',
      GenerateSecret: false,
      ExplicitAuthFlows: ['ALLOW_REFRESH_TOKEN_AUTH'],
      AllowedOAuthFlows: ['code'],
      AllowedOAuthFlowsUserPoolClient: true,
      AllowedOAuthScopes: ['openid'],
      CallbackURLs: ['http://localhost:8765/callback'],
      SupportedIdentityProviders: ['COGNITO'],
      PreventUserExistenceErrors: 'ENABLED',
      EnableTokenRevocation: true,
      AccessTokenValidity: 15,
      IdTokenValidity: 15,
      // Cognito's floor, in minutes.
      RefreshTokenValidity: 60,
      TokenValidityUnits: { AccessToken: 'minutes', IdToken: 'minutes', RefreshToken: 'minutes' },
    });
    expect(REVIEW_CLI_CALLBACK_URL).toBe('http://localhost:8765/callback');
    // Never the scope that lets a token call Cognito's own user APIs.
    expect(JSON.stringify(properties)).not.toContain('aws.cognito.signin.user.admin');
  });

  it('adds only IAM-authenticated ADMIN_USER_PASSWORD_AUTH, and only under the dev validation flag', () => {
    const validation = templates({
      ...config,
      auth: { ...config.auth, qualityReviewValidationPasswordAuthEnabled: true },
    }).auth;
    expect(client(validation, 'adlytics-review-cli-dev').ExplicitAuthFlows).toEqual([
      'ALLOW_ADMIN_USER_PASSWORD_AUTH',
      'ALLOW_REFRESH_TOKEN_AUTH',
    ]);
    // The BFF client's validation flow needs its secret; neither enables a flow needing neither.
    expect(client(validation, 'adlytics-bff-dev').ExplicitAuthFlows).toEqual([
      'ALLOW_USER_PASSWORD_AUTH',
      'ALLOW_REFRESH_TOKEN_AUTH',
    ]);
  });

  it('has its own managed login branding after the domain', () => {
    const template = templates().auth;
    const branding = Object.values(template.findResources('AWS::Cognito::ManagedLoginBranding')) as {
      Properties: { ClientId: { Ref: string }; UseCognitoProvidedValues: boolean };
      DependsOn?: string[];
    }[];
    const clientIds = Object.entries(template.findResources('AWS::Cognito::UserPoolClient'))
      .map(([logicalId]) => logicalId)
      .sort();
    expect(branding.map((style) => style.Properties.ClientId.Ref).sort()).toEqual(clientIds);
    const domain = Object.keys(template.findResources('AWS::Cognito::UserPoolDomain'));
    for (const style of branding) {
      expect(style.Properties.UseCognitoProvidedValues).toBe(true);
      expect(style.DependsOn).toEqual(expect.arrayContaining(domain));
    }
  });
});

describe('ApiStack', () => {
  it('creates no Cognito resources', () => {
    // Values arrive through SSM. A construct reference across the boundary would
    // reintroduce the export deadlock ADR-012 exists to avoid.
    templates().api.resourceCountIs('AWS::Cognito::UserPool', 0);
    templates().api.resourceCountIs('AWS::Cognito::UserPoolClient', 0);
  });

  it('runs every function on arm64', () => {
    const functions = templates().api.findResources('AWS::Lambda::Function');
    const architectures = Object.values(functions).map(
      (fn) => (fn as { Properties: { Architectures?: string[] } }).Properties.Architectures,
    );
    expect(architectures.length).toBeGreaterThan(0);
    for (const architecture of architectures) {
      expect(architecture).toEqual(['arm64']);
    }
  });

  it('declares no layer of its own and attaches the shared one', () => {
    // Architecture compatibility moved to `dependencies-stack.test.ts` with the resource. What
    // matters here is that this stack no longer publishes a second copy of the same closure.
    templates().api.resourceCountIs('AWS::Lambda::LayerVersion', 0);
  });

  it('uses a REQUEST authorizer with a simple response and the origin-verify identity source', () => {
    // The built-in JWT authorizer reads a bearer token from a header and has no
    // cookie support. A simple response keeps the decision scoped to the invoked
    // route rather than emitting a policy that could be broader.
    //
    // ADR-078: the identity source is the header CloudFront adds, never a credential, because a
    // request carries a cookie or a bearer and API Gateway answers 401 when a source is missing.
    const authorizers = Object.values(templates().api.findResources('AWS::ApiGatewayV2::Authorizer')) as {
      Properties: Props;
    }[];
    expect(authorizers).toHaveLength(1);
    const properties = authorizers[0]!.Properties;
    expect(properties).toEqual(expect.objectContaining({
      AuthorizerType: 'REQUEST',
      EnableSimpleResponses: true,
      AuthorizerPayloadFormatVersion: '2.0',
    }));
    expect(properties.IdentitySource).toEqual(['$request.header.x-adl-origin-verify']);
  });

  it('disables the authorizer result cache', () => {
    // The TTL is a revocation staleness window, and API Gateway caches only for an authorizer
    // with an identity source. Zero, with no configuration that can raise it.
    templates().api.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerResultTtlInSeconds: 0,
    });
  });

  it('gives the authorizer both client ids, each from AuthStack', () => {
    const variables = (functionByName(templates().api, 'adlytics-dev-AuthorizerFunction')
      .Environment as { Variables: Props }).Variables;
    expect(variables.ADL_COGNITO_CLIENT_ID).toEqual({ Ref: expect.stringContaining('SsmParameter') });
    expect(variables.ADL_COGNITO_PUBLIC_CLIENT_ID).toEqual({ Ref: expect.stringContaining('SsmParameter') });
    expect(variables.ADL_COGNITO_PUBLIC_CLIENT_ID).not.toEqual(variables.ADL_COGNITO_CLIENT_ID);
    const parameters = (templates().api.toJSON().Parameters ?? {}) as Record<string, { Default?: unknown }>;
    const ref = (variables.ADL_COGNITO_PUBLIC_CLIENT_ID as { Ref: string }).Ref;
    expect(parameters[ref]?.Default).toBe(paramPath('dev', 'userPoolPublicClientId'));
  });

  it('protects /me and leaves the session lifecycle routes open', () => {
    const routes = Object.values(templates().api.findResources('AWS::ApiGatewayV2::Route')) as {
      Properties: { RouteKey: string; AuthorizationType?: string };
    }[];
    const byKey = new Map(routes.map((route) => [route.Properties.RouteKey, route.Properties]));

    expect(byKey.get(`GET ${API_PREFIX}/me`)?.AuthorizationType).toBe('CUSTOM');

    // Unauthenticated by necessity: login and callback precede a session, refresh
    // runs when the token has already expired, logout is tearing it down. Each
    // performs its own CSRF checks — see docs/concepts/authentication.md §2.
    for (const key of [
      `GET ${API_PREFIX}/auth/login`,
      `GET ${API_PREFIX}/auth/callback`,
      `POST ${API_PREFIX}/auth/refresh`,
      `POST ${API_PREFIX}/auth/logout`,
    ]) {
      expect(byKey.has(key)).toBe(true);
      expect(byKey.get(key)?.AuthorizationType ?? 'NONE').toBe('NONE');
    }
  });

  it('configures no CORS at all', () => {
    // CloudFront serves the frontend and this API from one origin, so every
    // request is same-origin and no preflight is ever issued. CORS settings here
    // would be dead configuration implying a cross-origin design.
    const apis = Object.values(templates().api.findResources('AWS::ApiGatewayV2::Api')) as {
      Properties: { CorsConfiguration?: unknown };
    }[];
    for (const api of apis) {
      expect(api.Properties.CorsConfiguration).toBeUndefined();
    }
  });

  it('scopes cookies host-only, which is the only legal scope here', () => {
    const functions = Object.values(templates().api.findResources('AWS::Lambda::Function')) as {
      Properties: { Environment?: { Variables: Record<string, unknown> } };
    }[];
    for (const fn of functions) {
      const variables = fn.Properties.Environment?.Variables ?? {};
      expect(variables.ADL_COOKIE_SCOPE).toBe('host_only');
      expect(variables.ADL_COOKIE_DOMAIN).toBe('');
    }
  });

  it('grants the API Athena authority only on the interactive workgroup (SEMANTIC-M1)', () => {
    const rendered = JSON.stringify([
      ...Object.values(templates().api.findResources('AWS::IAM::Policy')),
      ...Object.values(templates().api.findResources('AWS::IAM::ManagedPolicy')),
    ]);
    // Paired control: the CHAT workgroup the API does query is present in the same text.
    expect(rendered).toContain(`workgroup/${semanticV2AthenaWorkgroupName('dev', 'interactive')}`);
    for (const kind of ['batch', 'bake', 'insight-precompute'] as const) {
      expect(rendered).not.toContain(semanticV2AthenaWorkgroupName('dev', kind));
    }
  });

  it('gives every Settings-resolving API function the V2 active names', () => {
    const functions = Object.values(templates().api.findResources('AWS::Lambda::Function')) as {
      Properties: { Environment?: { Variables: Record<string, unknown> } };
    }[];
    for (const fn of functions) {
      const variables = fn.Properties.Environment?.Variables ?? {};
      expect(variables.ADL_ATHENA_WORKGROUP).toBe(
        semanticV2AthenaWorkgroupName('dev', 'interactive'),
      );
      expect(variables.ADL_ATHENA_BATCH_WORKGROUP).toBeUndefined();
      expect(variables.ADL_GLUE_DATABASE).toBeUndefined();
      expect(variables.ADL_SEMANTIC_ATTEMPTS_TABLE).toBe(
        semanticV2StateTableName('dev', 'attempts'),
      );
      expect(variables.ADL_SEMANTIC_LEASES_TABLE).toBe(
        semanticV2StateTableName('dev', 'leases'),
      );
      expect(variables.ADL_SEMANTIC_PUBLICATIONS_TABLE).toBe(
        semanticV2StateTableName('dev', 'publications'),
      );
    }
  });

  it('gives Auth and Authorizer no data-plane IAM', () => {
    const policies = Object.values(templates().api.findResources('AWS::IAM::Policy')) as {
      Properties: {
        Roles?: { Ref: string }[];
        PolicyDocument: { Statement: { Action?: string | string[] }[] };
      };
    }[];
    for (const roleName of ['AuthFunction', 'AuthorizerFunction']) {
      const actions = policies
        .filter((policy) =>
          (policy.Properties.Roles ?? []).some((role) => role.Ref.includes(roleName)),
        )
        .flatMap((policy) => policy.Properties.PolicyDocument.Statement)
        .flatMap((statement) =>
          Array.isArray(statement.Action) ? statement.Action : [statement.Action ?? ''],
        );
      expect(actions.length).toBeGreaterThan(0);
      for (const action of actions) {
        expect(action).not.toMatch(/^(athena|dynamodb|glue|lakeformation|s3):/);
      }
    }
  });

  it('gives managed login a branding style, without which its pages 403', () => {
    // Managed login v2 renders from a branding style. A pool with the newer domain
    // and no branding answers 403 "Login pages unavailable" from /login while
    // /oauth2/authorize still redirects normally, so every resource reads healthy
    // and only a human signing in discovers it.
    templates().auth.hasResourceProperties('AWS::Cognito::ManagedLoginBranding', {
      UseCognitoProvidedValues: true,
    });
    // One per client: the BFF client and the review CLI client.
    templates().auth.resourceCountIs('AWS::Cognito::ManagedLoginBranding', 2);
  });

  it('exposes outputs for validation without creating exports', () => {
    // An output with an exportName becomes a CloudFormation export, which cannot
    // be changed or deleted while another stack imports it. That deadlock is why
    // ADR-012 moved cross-stack values to SSM, so outputs here stay describable
    // values only. Asserting the absence is what stops one being added by habit.
    for (const template of [templates().auth, templates().api, templates().frontend]) {
      const outputs = template.toJSON().Outputs ?? {};
      expect(Object.keys(outputs).length).toBeGreaterThan(0);
      const exported = Object.entries(outputs as Record<string, { Export?: unknown }>)
        .filter(([, output]) => output.Export !== undefined)
        .map(([name]) => name);
      expect(exported).toEqual([]);
    }
  });

  it('carries the API prefix on every route, because CloudFront never strips it', () => {
    // The bug this replaces: routes were declared as /me and /auth/login while the
    // Python dispatcher expected /api/me and /api/auth/login. CloudFront forwards
    // the prefix verbatim, so API Gateway 404ed before any function ran. Both sides
    // were self-consistent, so nothing failed until a real request arrived.
    const routes = Object.values(
      templates().api.findResources('AWS::ApiGatewayV2::Route'),
    ) as { Properties: { RouteKey: string } }[];

    expect(routes.length).toBeGreaterThan(0);
    for (const route of routes) {
      expect(route.Properties.RouteKey).toMatch(
        new RegExp(`^(GET|POST|PUT|PATCH|DELETE) \\${API_PREFIX}/`),
      );
    }
  });

  it('points the browser at the public origin, never at execute-api', () => {
    // A cookie set on the execute-api host would not come back to CloudFront, so
    // no URL the browser ever sees may name it. The authorizer's IAM source ARN
    // does name the execute-api service, which is why this checks the environment
    // rather than the whole template.
    const functions = Object.values(templates().api.findResources('AWS::Lambda::Function')) as {
      Properties: { Environment?: { Variables: Record<string, unknown> } };
    }[];
    for (const fn of functions) {
      const variables = fn.Properties.Environment?.Variables ?? {};
      for (const key of ['ADL_APP_BASE_URL', 'ADL_API_BASE_URL']) {
        // Now a deploy-time Ref to frontend/public-base-url rather than a literal.
        // What is still assertable, and still the point, is that nothing here names
        // execute-api: the value is a reference, and it is the same one for both.
        expect(variables[key]).toEqual({ Ref: expect.stringContaining('SsmParameter') });
      }
      expect(variables.ADL_APP_BASE_URL).toEqual(variables.ADL_API_BASE_URL);
    }
  });

  it('grants only Auth permission to read the exact client secret resource', () => {
    const api = templates().api;
    const functionNames = [
      'adlytics-dev-AuthFunction',
      'adlytics-dev-AuthorizerFunction',
      'adlytics-dev-PresentationFunction',
      'adlytics-dev-ConversationFunction',
    ];

    for (const functionName of functionNames) {
      const secretStatements = statementsForFunction(api, functionName).filter((statement) =>
        actions(statement).includes('secretsmanager:GetSecretValue'),
      );
      // ADR-078: the auth handler and the authorizer also read the origin-verify secret, and
      // nothing else does.
      const resources = secretStatements.map((statement) => JSON.stringify(statement.Resource));
      for (const resource of resources) expect(resource).not.toContain('"*"');
      if (functionName === 'adlytics-dev-AuthFunction') {
        expect(resources).toHaveLength(2);
        expect(resources.filter((r) => r.includes('bff-client-secret'))).toHaveLength(1);
        expect(resources.filter((r) => r.includes('edge/origin-verify-*'))).toHaveLength(1);
      } else if (functionName === 'adlytics-dev-AuthorizerFunction') {
        expect(resources).toHaveLength(1);
        expect(resources[0]).toContain('edge/origin-verify-*');
      } else {
        expect(secretStatements).toEqual([]);
      }
    }
  });

  it('names the client secret only in the Auth environment', () => {
    // No function receives the secret itself. Only Auth receives its identifier because only Auth
    // exchanges codes, refreshes sessions, and revokes tokens.
    const api = templates().api;
    const functionNames = [
      'adlytics-dev-AuthFunction',
      'adlytics-dev-AuthorizerFunction',
      'adlytics-dev-PresentationFunction',
      'adlytics-dev-ConversationFunction',
    ];

    for (const functionName of functionNames) {
      const fn = functionByName(api, functionName);
      const variables = ((fn.Environment as Props | undefined)?.Variables ?? {}) as Props;
      expect(Object.keys(variables)).not.toContain('ADL_COGNITO_CLIENT_SECRET');
      if (functionName === 'adlytics-dev-AuthFunction') {
        expect(variables.ADL_COGNITO_CLIENT_SECRET_ID).toBe(clientSecretName('dev'));
      } else {
        expect(variables.ADL_COGNITO_CLIENT_SECRET_ID).toBeUndefined();
      }
    }
  });

  it('never puts localhost in a function environment', () => {
    // The failure this project has hit repeatedly. A localhost URL deploys
    // cleanly and redirects users somewhere that does not exist.
    const rendered = JSON.stringify(templates().api.toJSON());
    expect(rendered).not.toContain('localhost');
    expect(rendered).not.toContain('127.0.0.1');
  });
});


describe('FrontendStack', () => {
  it('permits styles only from the static application origin', () => {
    templates().frontend.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: {
        SecurityHeadersConfig: {
          ContentSecurityPolicy: {
            ContentSecurityPolicy: Match.stringLikeRegexp(
              "(?:^|; )style-src 'self'(?:;|$)",
            ),
            Override: true,
          },
        },
      },
    });
    const rendered = JSON.stringify(templates().frontend.toJSON());
    expect(rendered).not.toContain("style-src 'self' 'unsafe-inline'");
  });

  it('defines no custom error responses', () => {
    /**
     * The trap this whole design has to avoid.
     *
     * Custom error responses live on the distribution, not on a cache behaviour,
     * so they cannot be scoped to the static site. The usual SPA mapping rewrites
     * 403 to index.html with a 200 — and 403 is what triggers a token refresh.
     * That mapping would turn every expired session into a successful response
     * containing HTML, with nothing reporting an error.
     */
    const distributions = Object.values(
      templates().frontend.findResources('AWS::CloudFront::Distribution'),
    ) as { Properties: { DistributionConfig: { CustomErrorResponses?: unknown } } }[];
    expect(distributions).toHaveLength(1);
    expect(distributions[0]!.Properties.DistributionConfig.CustomErrorResponses).toBeUndefined();
  });

  it('keeps the bucket private and reaches it through OAC', () => {
    templates().frontend.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
    // The bucket's own OAC, asserted by its origin type rather than by counting. A bare count had
    // to be edited when the conversation route added a second OAC for its Function URL, and
    // bumping the number to 2 would have asserted only that two exist — not that the S3 one is
    // still among them, which is the actual claim.
    templates().frontend.hasResourceProperties('AWS::CloudFront::OriginAccessControl', {
      OriginAccessControlConfig: {
        OriginAccessControlOriginType: 's3',
        SigningBehavior: 'always',
        SigningProtocol: 'sigv4',
      },
    });
  });

  it('serves the API from the same distribution', () => {
    // The single origin is what makes the session cookies same-origin rather than
    // merely same-site, and what removes the custom-domain requirement of F-03.
    const distribution = Object.values(
      templates().frontend.findResources('AWS::CloudFront::Distribution'),
    )[0] as {
      Properties: {
        DistributionConfig: {
          CacheBehaviors?: { PathPattern: string; CachePolicyId: string }[];
          Origins: unknown[];
        };
      };
    };
    const behaviours = distribution.Properties.DistributionConfig.CacheBehaviors ?? [];
    expect(behaviours.map((behaviour) => behaviour.PathPattern)).toContain('/api/*');
    // Three: the site bucket, the HTTP API, and the conversation Function URL. The chat route is a
    // separate origin by necessity — see ADR-038 — and its ordering against `/api/*` is asserted in
    // `conversation-route.test.ts`, which owns that concern.
    expect(distribution.Properties.DistributionConfig.Origins).toHaveLength(3);
  });

  it('rewrites static directory GET and HEAD routes only on the default behaviour', () => {
    templates().frontend.hasResourceProperties('AWS::CloudFront::Function', {
      AutoPublish: true,
      // The allowlist resolution itself is proven in static-route-rewrite.test.ts (ADR-123).
      FunctionCode: Match.stringLikeRegexp("method !== 'GET'.*method !== 'HEAD'[\\s\\S]*request\\.uri = resolve\\("),
      FunctionConfig: {
        Runtime: 'cloudfront-js-2.0',
      },
    });

    const distribution = Object.values(
      templates().frontend.findResources('AWS::CloudFront::Distribution'),
    )[0] as {
      Properties: {
        DistributionConfig: {
          CacheBehaviors?: { PathPattern: string; FunctionAssociations?: unknown }[];
          DefaultCacheBehavior: { FunctionAssociations?: { EventType: string }[] };
        };
      };
    };
    // ADR-123: the allowlist rewrite on the request, the not-found status on the response.
    expect(
      distribution.Properties.DistributionConfig.DefaultCacheBehavior.FunctionAssociations,
    ).toEqual([
      expect.objectContaining({ EventType: 'viewer-request' }),
      expect.objectContaining({ EventType: 'viewer-response' }),
    ]);
    // The API behaviours carry only the canonical-path check (ADR-078 §5), never the rewrite.
    const functions = templates().frontend.findResources('AWS::CloudFront::Function') as Record<
      string,
      { Properties: { FunctionCode: string } }
    >;
    const canonical = Object.keys(functions).filter((id) =>
      functions[id]!.Properties.FunctionCode.includes("statusCode: 400"));
    expect(canonical).toHaveLength(1);
    const behaviours = distribution.Properties.DistributionConfig.CacheBehaviors ?? [];
    expect(behaviours.map((behaviour) => behaviour.PathPattern)).toEqual(['/api/chat*', '/api/*']);
    for (const behaviour of behaviours) {
      expect(behaviour.FunctionAssociations).toEqual([
        {
          EventType: 'viewer-request',
          FunctionARN: { 'Fn::GetAtt': [canonical[0], 'FunctionARN'] },
        },
      ]);
    }
  });

  it('disables caching on the API behaviour', () => {
    // A cached authorizer response would serve one user's session to another.
    const cachingDisabled = '4135ea2d-6df8-44a3-9df3-4b5a84be39ad';
    const distribution = Object.values(
      templates().frontend.findResources('AWS::CloudFront::Distribution'),
    )[0] as {
      Properties: {
        DistributionConfig: { CacheBehaviors?: { PathPattern: string; CachePolicyId: string }[] };
      };
    };
    const apiBehaviour = (distribution.Properties.DistributionConfig.CacheBehaviors ?? []).find(
      (behaviour) => behaviour.PathPattern === '/api/*',
    );
    expect(apiBehaviour?.CachePolicyId).toBe(cachingDisabled);
  });

  it('publishes the public origin for consumers to build URLs from', () => {
    templates().frontend.hasResourceProperties('AWS::SSM::Parameter', {
      Name: paramPath('dev', 'publicBaseUrl'),
    });
  });
});
