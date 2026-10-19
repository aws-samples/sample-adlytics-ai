// SPDX-License-Identifier: MIT-0
/**
 * The token service (ADR-077 §3). Every security-critical property is compared exactly (§7): a
 * widened Deny condition, an added principal, a fourth public method, or a loosened WAF rule each
 * fails here rather than deploying.
 */
import {
  conversationFunctionTimeoutSeconds,
  paramPath,
} from '../lib/config';
import { TEST_CONFIG } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';

type Props = Record<string, unknown>;
type Resource = {
  Type?: unknown;
  Properties?: Props;
  DependsOn?: unknown;
  DeletionPolicy?: unknown;
  UpdateReplacePolicy?: unknown;
};

const template = everyTemplate().TokenServiceStack as Props;
const resources = (template.Resources ?? {}) as Record<string, Resource>;
const ACCOUNT = TEST_CONFIG.account;
const REGION = TEST_CONFIG.region;

function only<T>(items: T[], what: string): T {
  expect(items).toHaveLength(1);
  const [item] = items;
  if (item === undefined) throw new Error(`expected exactly one ${what}`);
  return item;
}

function logicalIdsOfType(type: string): string[] {
  return Object.entries(resources)
    .filter(([, resource]) => resource.Type === type)
    .map(([logicalId]) => logicalId);
}

function logicalIdWhere(type: string, match: (properties: Props) => boolean): string {
  return only(
    Object.entries(resources)
      .filter(([, resource]) => resource.Type === type && match(resource.Properties ?? {}))
      .map(([logicalId]) => logicalId),
    `${type} match`,
  );
}

function propertiesOf(logicalId: string): Props {
  const properties = resources[logicalId]?.Properties;
  if (properties === undefined) throw new Error(`${logicalId} has no properties`);
  return properties;
}

/** The SSM-typed template parameter that resolves a given path. */
function parameterFor(path: string): string {
  const parameters = (template.Parameters ?? {}) as Record<string, Props>;
  return only(
    Object.entries(parameters)
      .filter(([, parameter]) =>
        parameter.Type === 'AWS::SSM::Parameter::Value<String>' && parameter.Default === path)
      .map(([logicalId]) => logicalId),
    `parameter for ${path}`,
  );
}

const roleArnOf = (name: string): unknown => ({
  'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:iam::${ACCOUNT}:role/${name}`]],
});

const keyId = logicalIdsOfType('AWS::KMS::Key');
const signingKey = only(keyId, 'signing key');
const restApi = only(logicalIdsOfType('AWS::ApiGateway::RestApi'), 'REST API');
const stage = only(logicalIdsOfType('AWS::ApiGateway::Stage'), 'stage');
const agentClientSecret = only(logicalIdsOfType('AWS::SecretsManager::Secret'), 'agent client secret');
const credentialProvider = only(
  logicalIdsOfType('AWS::BedrockAgentCore::OAuth2CredentialProvider'),
  'credential provider',
);
const routeExchange = logicalIdWhere(
  'AWS::Lambda::Function',
  (p) => p.FunctionName === 'adlytics-dev-TokenRouteExchange',
);
const issuer = logicalIdWhere(
  'AWS::Lambda::Function',
  (p) => p.FunctionName === 'adlytics-dev-TokenIssuer',
);
const issuerUrl = {
  'Fn::Join': ['', [
    'https://',
    { Ref: restApi },
    `.execute-api.${REGION}.`,
    { Ref: 'AWS::URLSuffix' },
    '/v1',
  ]],
};
/** The issuer URL with a path appended, as CloudFormation renders a template-literal join. */
const issuerPath = (path: string): unknown => ({
  'Fn::Join': ['', [
    'https://',
    { Ref: restApi },
    `.execute-api.${REGION}.`,
    { Ref: 'AWS::URLSuffix' },
    `/v1${path}`,
  ]],
});

function roleOf(functionLogicalId: string): string {
  const role = (propertiesOf(functionLogicalId).Role as { 'Fn::GetAtt'?: unknown[] })['Fn::GetAtt']?.[0];
  if (typeof role !== 'string') throw new Error(`${functionLogicalId} has no role`);
  return role;
}

function identityStatements(roleLogicalId: string): Props[] {
  return resourcesOfType(template, 'AWS::IAM::Policy')
    .filter((policy) => ((policy.Roles ?? []) as Props[]).some((role) => role.Ref === roleLogicalId))
    .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
}

describe('the signing key', () => {
  it('is an RSA 2048 signing key under the environment alias', () => {
    const key = propertiesOf(signingKey);
    expect(key.KeySpec).toBe('RSA_2048');
    expect(key.KeyUsage).toBe('SIGN_VERIFY');
    expect(key.EnableKeyRotation).toBeUndefined();
    const alias = only(resourcesOfType(template, 'AWS::KMS::Alias'), 'alias');
    expect(alias).toStrictEqual({
      AliasName: 'alias/adlytics-dev-token-signing',
      TargetKeyId: { 'Fn::GetAtt': [signingKey, 'Arn'] },
    });
  });

  it('denies kms:Sign to every principal but the two token roles, in the key policy', () => {
    // Compared whole: an added Allow, a narrowed Deny action, or a condition naming a different
    // role all change this document. The root statement is the CDK default key policy.
    expect((propertiesOf(signingKey).KeyPolicy as Props)).toStrictEqual({
      Version: '2012-10-17',
      Statement: [
        {
          Action: 'kms:*',
          Effect: 'Allow',
          Principal: {
            AWS: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:iam::${ACCOUNT}:root`]] },
          },
          Resource: '*',
        },
        {
          Sid: 'OnlyTheTokenFunctionsSign',
          Effect: 'Deny',
          Principal: { AWS: '*' },
          Action: 'kms:Sign',
          Resource: '*',
          Condition: {
            ArnNotEquals: {
              'aws:PrincipalArn': [
                roleArnOf('adlytics-dev-token-route-exchange'),
                roleArnOf('adlytics-dev-token-issuer'),
              ],
            },
          },
        },
      ],
    });
  });

  it('grants kms:Sign to exactly the two token roles and kms:GetPublicKey only to the issuer', () => {
    const keyArn = { 'Fn::GetAtt': [signingKey, 'Arn'] };
    const kmsStatements = (role: string): Props[] =>
      identityStatements(role).filter((statement) => JSON.stringify(statement.Action).includes('kms:'));

    expect(kmsStatements(roleOf(routeExchange))).toStrictEqual([
      { Sid: 'SignHarnessTokens', Effect: 'Allow', Action: 'kms:Sign', Resource: keyArn },
    ]);
    expect(kmsStatements(roleOf(issuer))).toStrictEqual([
      { Sid: 'ReadSigningPublicKey', Effect: 'Allow', Action: 'kms:GetPublicKey', Resource: keyArn },
      { Sid: 'SignGatewayTokens', Effect: 'Allow', Action: 'kms:Sign', Resource: keyArn },
    ]);
    const signers = resourcesOfType(template, 'AWS::IAM::Policy')
      .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[])
      .filter((statement) => statement.Effect === 'Allow' && JSON.stringify(statement.Action).includes('kms:Sign'))
      .map((statement) => statement.Sid)
      .sort();
    expect(signers).toStrictEqual(['SignGatewayTokens', 'SignHarnessTokens']);
  });
});

describe('the route exchange', () => {
  it('carries exactly one resource policy: a Deny for every principal but the Conversation role', () => {
    const functionArn = { 'Fn::GetAtt': [routeExchange, 'Arn'] };
    expect(only(resourcesOfType(template, 'AWS::Lambda::ResourcePolicy'), 'resource policy'))
      .toStrictEqual({
        ResourceArn: functionArn,
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [{
            Sid: 'OnlyTheConversationRoute',
            Effect: 'Deny',
            Principal: '*',
            Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
            Resource: functionArn,
            Condition: {
              ArnNotEquals: { 'aws:PrincipalArn': roleArnOf('adlytics-dev-conversation-function') },
            },
          }],
        },
      });
  });

  it('has no AWS::Lambda::Permission, which would be a second writer of that policy', () => {
    const targeting = resourcesOfType(template, 'AWS::Lambda::Permission')
      .filter((permission) => JSON.stringify(permission.FunctionName).includes(routeExchange));
    expect(targeting).toStrictEqual([]);
    // The issuer's three API Gateway grants exist, so the filter above is not vacuous.
    expect(resourcesOfType(template, 'AWS::Lambda::Permission')).toHaveLength(3);
  });

  it('is an arm64 function under its named role with the token-service environment', () => {
    const fn = propertiesOf(routeExchange);
    expect(fn.Handler).toBe('adapters.inbound.token_service.route_exchange.handler');
    expect(fn.Architectures).toStrictEqual(['arm64']);
    expect(fn.Runtime).toBe('python3.14');
    expect(fn.MemorySize).toBe(1024);
    expect(fn.Timeout).toBe(10);
    expect(propertiesOf(roleOf(routeExchange)).RoleName).toBe('adlytics-dev-token-route-exchange');
  });
});

describe('both token functions', () => {
  const expectedEnvironment = (): Props => ({
    ADL_ENVIRONMENT: 'dev',
    ADL_AWS_REGION: REGION,
    ADL_TRACING_ENABLED: 'false',
    ADL_TRACE_SAMPLE_RATIO: '1',
    POWERTOOLS_METRICS_NAMESPACE: 'Adlytics',
    POWERTOOLS_LOG_LEVEL: 'INFO',
    ADL_COGNITO_USER_POOL_ID: { Ref: parameterFor(paramPath('dev', 'userPoolId')) },
    ADL_COGNITO_CLIENT_ID: { Ref: parameterFor(paramPath('dev', 'userPoolClientId')) },
    ADL_TOKEN_ISSUER: issuerUrl,
    ADL_TOKEN_SIGNING_KEY_ID: { Ref: signingKey },
    ADL_HARNESS_TOKEN_AUDIENCE: 'adlytics-dev-harness',
    ADL_ROUTE_CLIENT_ID: 'adlytics-dev-conversation-route',
    ADL_HARNESS_TOKEN_LIFETIME_SECONDS: String(
      conversationFunctionTimeoutSeconds(TEST_CONFIG) + TEST_CONFIG.tokenService.clockSkewSeconds,
    ),
    ADL_GATEWAY_TOKEN_AUDIENCE: 'adlytics-dev-gateway',
    ADL_AGENT_CLIENT_ID: 'adlytics-dev-agent',
    // The ARN, never the value: an environment variable is readable with GetFunctionConfiguration.
    ADL_AGENT_CLIENT_SECRET_ID: { Ref: agentClientSecret },
  });

  it.each([
    ['route exchange', () => routeExchange],
    ['issuer', () => issuer],
  ])('%s receives exactly the narrow token-service environment', (_name, id) => {
    const variables = ((propertiesOf(id()).Environment as Props).Variables ?? {}) as Props;
    expect(variables).toStrictEqual(expectedEnvironment());
  });

  it('derives a Harness token lifetime of one turn plus skew from config', () => {
    // 600 s deep dive + 30 s route work + 45 s skew in the test config: a stack literal fails.
    const variables = ((propertiesOf(routeExchange).Environment as Props).Variables ?? {}) as Props;
    expect(variables.ADL_HARNESS_TOKEN_LIFETIME_SECONDS).toBe('675');
  });

  it('declares exactly the two named roles and one explicit log group per function', () => {
    const roleNames = resourcesOfType(template, 'AWS::IAM::Role').map((role) => role.RoleName);
    expect(roleNames.sort()).toStrictEqual([
      'adlytics-dev-token-issuer',
      'adlytics-dev-token-route-exchange',
    ]);
    const logGroups = resourcesOfType(template, 'AWS::Logs::LogGroup').map((group) => group.LogGroupName);
    expect(logGroups.sort()).toStrictEqual([
      '/aws/lambda/adlytics-dev-TokenIssuer',
      '/aws/lambda/adlytics-dev-TokenRouteExchange',
    ]);
    expect(propertiesOf(issuer).Handler).toBe('adapters.inbound.token_service.issuer.handler');
    expect(propertiesOf(issuer).Architectures).toStrictEqual(['arm64']);
  });
});

describe('the public issuer', () => {
  /** Full path of an API Gateway resource, walking its parents to the root. */
  function pathOf(resourceRef: unknown): string {
    const segments: string[] = [];
    let current = resourceRef as Props;
    while (typeof current.Ref === 'string') {
      const properties = propertiesOf(current.Ref);
      segments.unshift(String(properties.PathPart));
      current = properties.ParentId as Props;
    }
    expect(current).toStrictEqual({ 'Fn::GetAtt': [restApi, 'RootResourceId'] });
    return `/${segments.join('/')}`;
  }

  it('is a regional REST API with no CloudWatch account role', () => {
    expect(propertiesOf(restApi).EndpointConfiguration).toStrictEqual({ Types: ['REGIONAL'] });
    expect(logicalIdsOfType('AWS::ApiGateway::Account')).toStrictEqual([]);
    expect(logicalIdsOfType('AWS::ApiGateway::Authorizer')).toStrictEqual([]);
    expect(logicalIdsOfType('AWS::ApiGateway::ApiKey')).toStrictEqual([]);
  });

  it('serves exactly two GETs and POST /token, unauthenticated and proxied to the issuer', () => {
    const integrationUri = {
      'Fn::Join': ['', [
        'arn:',
        { Ref: 'AWS::Partition' },
        `:apigateway:${REGION}:lambda:path/2015-03-31/functions/`,
        { 'Fn::GetAtt': [issuer, 'Arn'] },
        '/invocations',
      ]],
    };
    const methods = resourcesOfType(template, 'AWS::ApiGateway::Method')
      .map((method) => ({ path: pathOf(method.ResourceId), method }))
      .sort((a, b) => a.path.localeCompare(b.path));
    expect(methods).toStrictEqual(
      ([
        ['/.well-known/openid-configuration', 'GET'],
        ['/jwks.json', 'GET'],
        ['/token', 'POST'],
      ] as const).map(([path, httpMethod]) => ({
        path,
        method: {
          HttpMethod: httpMethod,
          AuthorizationType: 'NONE',
          ApiKeyRequired: false,
          ResourceId: expect.anything(),
          RestApiId: { Ref: restApi },
          Integration: { Type: 'AWS_PROXY', IntegrationHttpMethod: 'POST', Uri: integrationUri },
        },
      })),
    );
  });

  it('throttles each method from config on stage v1', () => {
    const settings = propertiesOf(stage);
    expect(settings.StageName).toBe('v1');
    const { discovery, jwks, token } = TEST_CONFIG.tokenService.publicRouteThrottle;
    expect(settings.MethodSettings).toStrictEqual([
      {
        DataTraceEnabled: false,
        HttpMethod: 'GET',
        ResourcePath: '/~1.well-known~1openid-configuration',
        ThrottlingBurstLimit: discovery.burstLimit,
        ThrottlingRateLimit: discovery.rateLimit,
      },
      {
        DataTraceEnabled: false,
        HttpMethod: 'GET',
        ResourcePath: '/~1jwks.json',
        ThrottlingBurstLimit: jwks.burstLimit,
        ThrottlingRateLimit: jwks.rateLimit,
      },
      {
        DataTraceEnabled: false,
        HttpMethod: 'POST',
        ResourcePath: '/~1token',
        ThrottlingBurstLimit: token.burstLimit,
        ThrottlingRateLimit: token.rateLimit,
      },
    ]);
    // Each route its own ceiling: the test config gives all three distinct values.
    expect(new Set([discovery.rateLimit, jwks.rateLimit, token.rateLimit]).size).toBe(3);
  });

  it('lets API Gateway invoke the issuer only for those three methods on that stage', () => {
    const sourceArn = (path: string, httpMethod = 'GET'): unknown => ({
      'Fn::Join': ['', [
        'arn:',
        { Ref: 'AWS::Partition' },
        `:execute-api:${REGION}:${ACCOUNT}:`,
        { Ref: restApi },
        '/',
        { Ref: stage },
        `/${httpMethod}${path}`,
      ]],
    });
    const permissions = resourcesOfType(template, 'AWS::Lambda::Permission')
      .sort((a, b) => JSON.stringify(a.SourceArn).localeCompare(JSON.stringify(b.SourceArn)));
    expect(permissions).toStrictEqual([
      {
        Action: 'lambda:InvokeFunction',
        FunctionName: { 'Fn::GetAtt': [issuer, 'Arn'] },
        Principal: 'apigateway.amazonaws.com',
        SourceArn: sourceArn('/.well-known/openid-configuration'),
      },
      {
        Action: 'lambda:InvokeFunction',
        FunctionName: { 'Fn::GetAtt': [issuer, 'Arn'] },
        Principal: 'apigateway.amazonaws.com',
        SourceArn: sourceArn('/jwks.json'),
      },
      {
        Action: 'lambda:InvokeFunction',
        FunctionName: { 'Fn::GetAtt': [issuer, 'Arn'] },
        Principal: 'apigateway.amazonaws.com',
        SourceArn: sourceArn('/token', 'POST'),
      },
    ]);
  });

  it('builds the issuer from the API id, never the stage, so no function depends on it', () => {
    // `api.url` names the stage, which depends on the methods and so on the issuer function.
    const environments = JSON.stringify([routeExchange, issuer].map((id) => propertiesOf(id).Environment));
    expect(environments).not.toContain(stage);
    expect(environments).not.toContain('Deployment');
  });
});

describe('AWS WAF on the issuer stage', () => {
  const metric = (name: string): Props => ({
    CloudWatchMetricsEnabled: true,
    MetricName: `adlytics-dev-token-issuer-${name}`,
    SampledRequestsEnabled: true,
  });

  it('allows by default and applies a per-IP rate rule and the IP-reputation group', () => {
    const acl = only(resourcesOfType(template, 'AWS::WAFv2::WebACL'), 'web ACL');
    expect(acl.Scope).toBe('REGIONAL');
    expect(acl.DefaultAction).toStrictEqual({ Allow: {} });
    expect(acl.VisibilityConfig).toStrictEqual(metric('acl'));
    expect(acl.Rules).toStrictEqual([
      {
        Name: 'RateLimitPerSourceIp',
        Priority: 0,
        Action: { Block: {} },
        Statement: {
          RateBasedStatement: {
            Limit: TEST_CONFIG.tokenService.wafRateLimitPerFiveMinutes,
            AggregateKeyType: 'IP',
            EvaluationWindowSec: 300,
          },
        },
        VisibilityConfig: metric('rate-limit'),
      },
      {
        Name: 'AwsIpReputation',
        Priority: 1,
        OverrideAction: { None: {} },
        Statement: {
          ManagedRuleGroupStatement: {
            VendorName: 'AWS',
            Name: 'AWSManagedRulesAmazonIpReputationList',
          },
        },
        VisibilityConfig: metric('ip-reputation'),
      },
    ]);
  });

  it('is associated with stage v1 of this API, after the stage exists', () => {
    const association = only(logicalIdsOfType('AWS::WAFv2::WebACLAssociation'), 'association');
    const acl = only(logicalIdsOfType('AWS::WAFv2::WebACL'), 'web ACL');
    expect(propertiesOf(association)).toStrictEqual({
      ResourceArn: {
        'Fn::Join': ['', [
          'arn:',
          { Ref: 'AWS::Partition' },
          `:apigateway:${REGION}::/restapis/`,
          { Ref: restApi },
          '/stages/v1',
        ]],
      },
      WebACLArn: { 'Fn::GetAtt': [acl, 'Arn'] },
    });
    expect(resources[association]?.DependsOn).toStrictEqual([stage]);
  });
});

describe('the agent client registration (ADR-077 S4)', () => {
  /** Properties without the standard tags, which the tagging mixin owns and its own suite asserts. */
  function untagged(logicalId: string): Props {
    const { Tags: _tags, ...rest } = propertiesOf(logicalId);
    return rest;
  }

  it('generates the client secret at deploy as JSON under client_secret, destroyed with the stack', () => {
    // Compared whole: a literal `SecretString`, a shorter or punctuated secret, or a different
    // key each changes this document.
    expect(untagged(agentClientSecret)).toStrictEqual({
      Name: 'adlytics/dev/identity/agent-client-secret',
      Description: 'Agent OAuth client secret presented at the token-service /token (ADR-077).',
      GenerateSecretString: {
        SecretStringTemplate: '{}',
        GenerateStringKey: 'client_secret',
        PasswordLength: 64,
        ExcludePunctuation: true,
      },
    });
    expect(resources[agentClientSecret]?.DeletionPolicy).toBe('Delete');
    expect(resources[agentClientSecret]?.UpdateReplacePolicy).toBe('Delete');
  });

  it('denies GetSecretValue to every principal but the three readers P2 observed', () => {
    // Compared whole: an added reader, a widened action, or a dropped condition each fails here.
    // The CloudFormation execution role is the exact default-qualifier ARN, never a pattern, so a
    // bootstrap under another qualifier cannot read the secret.
    const policy = only(resourcesOfType(template, 'AWS::SecretsManager::ResourcePolicy'), 'policy');
    expect(policy).toStrictEqual({
      SecretId: { Ref: agentClientSecret },
      ResourcePolicy: {
        Version: '2012-10-17',
        Statement: [{
          Sid: 'OnlyObservedReaders',
          Effect: 'Deny',
          Principal: { AWS: '*' },
          Action: 'secretsmanager:GetSecretValue',
          Resource: '*',
          Condition: {
            ArnNotEquals: {
              'aws:PrincipalArn': [
                roleArnOf('adlytics-dev-token-issuer'),
                roleArnOf('adlytics-dev-harness-execution'),
                {
                  'Fn::Sub': `arn:\${AWS::Partition}:iam::${ACCOUNT}:role/`
                    + `cdk-hnb659fds-cfn-exec-role-${ACCOUNT}-${REGION}`,
                },
              ],
            },
          },
        }],
      },
    });
    expect(logicalIdsOfType('AWS::SecretsManager::RotationSchedule')).toStrictEqual([]);
  });

  it('lets only the issuer read the secret, by exact ARN, and never DescribeSecret', () => {
    const secretStatements = resourcesOfType(template, 'AWS::IAM::Policy')
      .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[])
      .filter((statement) => JSON.stringify(statement.Action).includes('secretsmanager:'));
    expect(secretStatements).toStrictEqual([{
      Sid: 'ReadAgentClientSecret',
      Effect: 'Allow',
      Action: 'secretsmanager:GetSecretValue',
      Resource: { Ref: agentClientSecret },
    }]);
    expect(identityStatements(roleOf(issuer))).toContainEqual(secretStatements[0]);
    expect(JSON.stringify(identityStatements(roleOf(routeExchange)))).not.toContain('secretsmanager:');
  });

  it('registers a CustomOauth2 provider for the agent client that exchanges with no actor token', () => {
    expect(untagged(credentialProvider)).toStrictEqual({
      Name: 'adlytics-dev-token-service',
      CredentialProviderVendor: 'CustomOauth2',
      Oauth2ProviderConfigInput: {
        CustomOauth2ProviderConfig: {
          OauthDiscovery: {
            // Explicit metadata: AgentCore Identity refuses a custom provider without an
            // authorization endpoint, and the token endpoint must be this issuer's `/token`.
            AuthorizationServerMetadata: {
              Issuer: issuerUrl,
              AuthorizationEndpoint: issuerPath('/authorize'),
              TokenEndpoint: issuerPath('/token'),
            },
          },
          ClientId: 'adlytics-dev-agent',
          // EXTERNAL and a reference: the secret value never reaches the template.
          // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
          ClientSecretSource: 'EXTERNAL',
          ClientSecretConfig: { SecretId: { Ref: agentClientSecret }, JsonKey: 'client_secret' },
          ClientAuthenticationMethod: 'CLIENT_SECRET_POST',
          OnBehalfOfTokenExchangeConfig: {
            GrantType: 'TOKEN_EXCHANGE',
            TokenExchangeGrantTypeConfig: { ActorTokenContent: 'NONE' },
          },
        },
      },
    });
  });

  it('creates the provider after the stage that serves its discovery document', () => {
    expect(resources[credentialProvider]?.DependsOn).toStrictEqual([stage]);
    expect(resources[credentialProvider]?.DeletionPolicy).toBe('Delete');
  });
});

describe('published parameters', () => {
  it('publishes the issuer URL, route-exchange ARN, provider ARN and secret ARN through typed paths', () => {
    const parameters = resourcesOfType(template, 'AWS::SSM::Parameter')
      .map((parameter) => ({ Name: parameter.Name, Value: parameter.Value }))
      .sort((a, b) => String(a.Name).localeCompare(String(b.Name)));
    expect(parameters).toStrictEqual([
      {
        Name: '/adlytics/dev/identity/agent-client-secret-arn',
        Value: { Ref: agentClientSecret },
      },
      {
        Name: '/adlytics/dev/identity/token-credential-provider-arn',
        Value: { 'Fn::GetAtt': [credentialProvider, 'CredentialProviderArn'] },
      },
      { Name: '/adlytics/dev/identity/token-issuer-url', Value: issuerUrl },
      {
        Name: '/adlytics/dev/identity/token-route-exchange-function-arn',
        Value: { 'Fn::GetAtt': [routeExchange, 'Arn'] },
      },
      {
        Name: '/adlytics/dev/identity/token-signing-key-arn',
        Value: { 'Fn::GetAtt': [signingKey, 'Arn'] },
      },
    ]);
    expect(paramPath('dev', 'tokenIssuerUrl')).toBe('/adlytics/dev/identity/token-issuer-url');
    expect(paramPath('dev', 'tokenCredentialProviderArn')).toBe(
      '/adlytics/dev/identity/token-credential-provider-arn',
    );
    expect(paramPath('dev', 'agentClientSecretArn')).toBe(
      '/adlytics/dev/identity/agent-client-secret-arn',
    );
  });

  it('destroys every resource with the stack', () => {
    for (const [logicalId, resource] of Object.entries(resources)) {
      expect([logicalId, (resource as Props).DeletionPolicy]).toStrictEqual([logicalId, 'Delete']);
    }
  });
});
