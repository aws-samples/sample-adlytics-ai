// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

import {
  agentClientId,
  agentClientSecretName,
  AppConfig,
  harnessExecutionRoleName,
  conversationFunctionRoleName,
  conversationFunctionTimeoutSeconds,
  gatewayTokenAudience,
  harnessTokenAudience,
  paramPath,
  routeClientId,
  TOKEN_SERVICE_STAGE,
  tokenCredentialProviderName,
  tokenIssuerRoleName,
  tokenRouteExchangeRoleName,
} from '../config';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

/**
 * The token service's own unauthenticated set (ADR-077 §3; infra-security-apigateway).
 *
 * These are the only unauthenticated routes outside ApiStack's sign-in set, and each carries its
 * own stage throttle. The two GETs serve public key material. `POST /token` is unauthenticated at
 * API Gateway only: it is the RFC 8693 exchange, which authenticates its caller in the handler by
 * `client_secret_post` before any key work (ADR-077 S4).
 */
export const TOKEN_SERVICE_UNAUTHENTICATED_ROUTES = [
  { path: '/.well-known/openid-configuration', method: 'GET', throttle: 'discovery' },
  { path: '/jwks.json', method: 'GET', throttle: 'jwks' },
  { path: '/token', method: 'POST', throttle: 'token' },
] as const;

export interface TokenServiceStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Directory holding the Python source tree. */
  readonly sourceAssetPath: string;
}

/**
 * The minimal token issuer ADR-077 §3 requires because Cognito has no token-exchange grant.
 *
 * DESTROY. It holds a signing key, two functions, a client secret and a credential provider. A
 * rebuilt key is a new `kid` and a new key ARN: the Harness and Gateway authorizers pick it up
 * from the key set, but the tools pin the `kid` and read the key ARN at deploy, so AgentStack must
 * be redeployed after it. A rebuilt secret is re-read by the provider by ARN; tokens live for one
 * turn, so no token outlives a rebuild.
 *
 * Two functions from one package, each issuing one audience, and the only two principals allowed
 * to sign:
 * - the route exchange, invoked directly by the Conversation Function and by nothing else — its
 *   resource policy denies every other principal — minting Harness tokens;
 * - the public issuer, behind a regional REST API with AWS WAF on its stage, serving discovery and
 *   the key set that the authorizers fetch, and the `/token` exchange that mints Gateway tokens.
 *
 * The issuer owns its client registrations, so the agent client's secret and the AgentCore OAuth2
 * credential provider that presents it live here too (ADR-077 S4).
 *
 * Every policy that names another role names its deterministic ARN, so this stack deploys before
 * ApiStack creates the Conversation Function role.
 */
export class TokenServiceStack extends cdk.Stack {
  public readonly signingKey: kms.Key;
  public readonly api: apigateway.RestApi;

  constructor(scope: Construct, id: string, props: TokenServiceStackProps) {
    super(scope, id, props);
    applyStandardTags(this, props.config);
    const { config } = props;

    const read = (key: 'userPoolId' | 'userPoolClientId' | 'dependenciesLayerArn'): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const roleArn = (name: string): string =>
      `arn:${this.partition}:iam::${this.account}:role/${name}`;
    const routeExchangeRoleArn = roleArn(tokenRouteExchangeRoleName(config.envName));
    const issuerRoleArn = roleArn(tokenIssuerRoleName(config.envName));
    const conversationRoleArn = roleArn(conversationFunctionRoleName(config.envName));

    // Asymmetric keys do not rotate automatically, so rotation is a new key and a new `kid`
    // (Conversational Runtime LLD §6); manual rotation is the documented path for asymmetric keys
    // (https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html). The key policy's
    // account statement is the KMS default, which makes IAM policies effective for the key and
    // grants no principal access by itself
    // (https://docs.aws.amazon.com/kms/latest/developerguide/key-policy-default.html); signing is
    // confined by the OnlyTheTokenFunctionsSign Deny below [both verified 2026-10-02].
    this.signingKey = new kms.Key(this, 'SigningKey', {
      description: 'Signs token-service JWTs (ADR-077). Only the two token functions may sign.',
      alias: `alias/adlytics-${config.envName}-token-signing`,
      keySpec: kms.KeySpec.RSA_2048,
      keyUsage: kms.KeyUsage.SIGN_VERIFY,
      // Nothing is encrypted under this key, so a pending deletion protects no data: the shortest
      // window KMS allows only bounds how long a destroyed stack's key lingers.
      pendingWindow: cdk.Duration.days(7),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // Alongside the default account-root statement. An identity policy alone can grant kms:Sign
    // within the account, so the Deny is what confines signing to the two token roles; it survives
    // a broad grant attached later. A negated operator over a list denies a principal matching
    // none of the values, so each role is admitted and nothing else is.
    this.signingKey.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'OnlyTheTokenFunctionsSign',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()],
      actions: ['kms:Sign'],
      resources: ['*'],
      conditions: { ArnNotEquals: { 'aws:PrincipalArn': [routeExchangeRoleArn, issuerRoleArn] } },
    }));

    // The agent client's secret, generated at deploy so no value is ever in a template or an
    // environment variable (infra-security-apigateway). DESTROY with the rest of the token
    // service: a rebuilt secret is a new client credential, and the provider reads it by ARN.
    const agentClientSecret = new secretsmanager.Secret(this, 'AgentClientSecret', {
      secretName: agentClientSecretName(config.envName),
      description: 'Agent OAuth client secret presented at the token-service /token (ADR-077).',
      generateSecretString: {
        secretStringTemplate: '{}',
        generateStringKey: 'client_secret',
        passwordLength: 64,
        excludePunctuation: true,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    // Exactly the readers probe P2 observed on 2026-09-26 (CloudTrail GetSecretValue): the issuer,
    // which compares the presented secret; the Harness execution role, which AgentCore Identity
    // assumes to present it at `/token`; and the CDK CloudFormation execution role, which the
    // credential provider's own handler uses on create and update. An identity policy alone can
    // grant GetSecretValue within the account, so this Deny is what confines the value.
    agentClientSecret.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'OnlyObservedReaders',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()],
      actions: ['secretsmanager:GetSecretValue'],
      resources: ['*'],
      conditions: {
        ArnNotEquals: {
          'aws:PrincipalArn': [
            issuerRoleArn,
            roleArn(harnessExecutionRoleName(config.envName)),
            this.cloudFormationExecutionRoleArn(),
          ],
        },
      },
    }));

    this.api = new apigateway.RestApi(this, 'IssuerApi', {
      restApiName: `adlytics-${config.envName}-token-issuer`,
      description: 'Public token-service issuer: discovery, key set, and /token (ADR-077 §3).',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      // Account-level API Gateway logging settings belong to the account owner, not this stack.
      cloudWatchRole: false,
      deployOptions: {
        stageName: TOKEN_SERVICE_STAGE,
        methodOptions: Object.fromEntries(
          TOKEN_SERVICE_UNAUTHENTICATED_ROUTES.map((route) => {
            const throttle = config.tokenService.publicRouteThrottle[route.throttle];
            return [
              `${route.path}/${route.method}`,
              { throttlingRateLimit: throttle.rateLimit, throttlingBurstLimit: throttle.burstLimit },
            ];
          }),
        ),
      },
    });

    // Built from the API id, not `api.url`. The URL names the stage, the stage depends on the
    // deployment, the deployment on the methods, and the methods on the issuer function; a
    // function environment holding that URL would close the loop. The id depends on nothing here.
    const issuerUrl =
      `https://${this.api.restApiId}.execute-api.${this.region}.${this.urlSuffix}/${TOKEN_SERVICE_STAGE}`;

    const dependencies = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PythonDependencies',
      read('dependenciesLayerArn'),
    );
    // One environment for both entry points: `TokenServiceSettings` reads the same narrow set.
    const environment: Record<string, string> = {
      ...tracingEnvironment(config),
      ADL_COGNITO_USER_POOL_ID: read('userPoolId'),
      ADL_COGNITO_CLIENT_ID: read('userPoolClientId'),
      ADL_TOKEN_ISSUER: issuerUrl,
      ADL_TOKEN_SIGNING_KEY_ID: this.signingKey.keyId,
      ADL_HARNESS_TOKEN_AUDIENCE: harnessTokenAudience(config.envName),
      ADL_ROUTE_CLIENT_ID: routeClientId(config.envName),
      // ADR-077 §2: the longest a turn can run, plus skew, independent of the user token's life.
      ADL_HARNESS_TOKEN_LIFETIME_SECONDS: String(
        conversationFunctionTimeoutSeconds(config) + config.tokenService.clockSkewSeconds,
      ),
      // ADR-077 S4: the `/token` exchange. The secret's ARN, never its value; the issuer reads the
      // value from Secrets Manager.
      ADL_GATEWAY_TOKEN_AUDIENCE: gatewayTokenAudience(config.envName),
      ADL_AGENT_CLIENT_ID: agentClientId(config.envName),
      ADL_AGENT_CLIENT_SECRET_ID: agentClientSecret.secretArn,
    };

    const routeExchange = this.pythonFunction('TokenRouteExchange', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      roleName: tokenRouteExchangeRoleName(config.envName),
      handler: 'adapters.inbound.token_service.route_exchange.handler',
      environment,
      // 1024 MB, like the cookie authorizer: this runs on every chat turn before the Harness is
      // called, and RSA verification is CPU-bound, so its cold start is on the user's wait.
      memoryMb: 1024,
      // One JWKS fetch at most, one verification, and one KMS Sign. Well under the route's own
      // budget, so a stuck exchange fails the turn quickly instead of consuming it.
      timeoutSeconds: 10,
    });
    routeExchange.addToRolePolicy(new iam.PolicyStatement({
      sid: 'SignHarnessTokens',
      actions: ['kms:Sign'],
      resources: [this.signingKey.keyArn],
    }));

    // Within one account an identity policy alone can grant lambda:InvokeFunction, so this Deny is
    // what confines the exchange to the Conversation Function role. It is the whole resource
    // policy: an AWS::Lambda::Permission on this function would be a second writer of the same
    // policy, which AWS warns against mixing, so none is ever added.
    new cdk.CfnResource(this, 'RouteExchangeResourcePolicy', {
      type: 'AWS::Lambda::ResourcePolicy',
      properties: {
        ResourceArn: routeExchange.functionArn,
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [{
            Sid: 'OnlyTheConversationRoute',
            Effect: 'Deny',
            Principal: '*',
            Action: ['lambda:InvokeFunction', 'lambda:InvokeAsync'],
            Resource: routeExchange.functionArn,
            Condition: { ArnNotEquals: { 'aws:PrincipalArn': conversationRoleArn } },
          }],
        },
      },
    });

    const issuer = this.pythonFunction('TokenIssuer', {
      config,
      dependencies,
      sourceAssetPath: props.sourceAssetPath,
      roleName: tokenIssuerRoleName(config.envName),
      handler: 'adapters.inbound.token_service.issuer.handler',
      environment,
      // 512 MB: two static JSON documents and the `/token` exchange, the key set and client secret
      // cached per container. Enough CPU for a fast Python cold start and one RSA verification.
      memoryMb: 512,
      // Below API Gateway's 29 s integration timeout; on a cold container `/token` makes one
      // Secrets Manager read, one GetPublicKey and one KMS Sign.
      timeoutSeconds: 10,
    });
    issuer.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadSigningPublicKey',
      actions: ['kms:GetPublicKey'],
      resources: [this.signingKey.keyArn],
    }));
    issuer.addToRolePolicy(new iam.PolicyStatement({
      sid: 'SignGatewayTokens',
      actions: ['kms:Sign'],
      resources: [this.signingKey.keyArn],
    }));
    // A statement rather than `grantRead`, which also adds DescribeSecret: the issuer needs only
    // the value it compares the presented `client_secret` against.
    issuer.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ReadAgentClientSecret',
      actions: ['secretsmanager:GetSecretValue'],
      resources: [agentClientSecret.secretArn],
    }));

    for (const fn of [routeExchange, issuer]) grantSpanExport(fn);

    // Proxy integration, scoped to each method, with no test-invoke permission: the console test
    // path is not a caller this API serves.
    const integration = new apigateway.LambdaIntegration(issuer, {
      proxy: true,
      allowTestInvoke: false,
    });
    for (const route of TOKEN_SERVICE_UNAUTHENTICATED_ROUTES) {
      this.api.root.resourceForPath(route.path).addMethod(route.method, integration, {
        // Public by design: authorizers fetch the key routes before any caller identity exists,
        // and `/token` authenticates its OAuth client in the handler, which no API Gateway
        // authorizer type can do for `client_secret_post`.
        authorizationType: apigateway.AuthorizationType.NONE,
        apiKeyRequired: false,
      });
    }

    this.attachWaf(config);

    // ADR-077 S4: AgentCore Identity's registration of the agent client, which the Harness tool's
    // outbound auth names. It authenticates at `/token` with the secret above; the Harness token
    // is the subject and no actor token is sent.
    const credentialProvider = new agentcore.CfnOAuth2CredentialProvider(
      this,
      'TokenCredentialProvider',
      {
        name: tokenCredentialProviderName(config.envName),
        credentialProviderVendor: 'CustomOauth2',
        oauth2ProviderConfigInput: {
          customOauth2ProviderConfig: {
            // Explicit metadata rather than the discovery URL. AgentCore Identity refuses a custom
            // provider "with no Authorization Endpoint information" even for token exchange, which
            // never calls one (observed 2026-09-26). The discovery document rightly omits it
            // (RFC 8414 §2: only for grants that use it), so the provider names a nominal
            // `/authorize` that the API does not route and nothing ever calls.
            oauthDiscovery: {
              authorizationServerMetadata: {
                issuer: issuerUrl,
                authorizationEndpoint: `${issuerUrl}/authorize`,
                tokenEndpoint: `${issuerUrl}/token`,
              },
            },
            clientId: agentClientId(config.envName),
            // pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
            clientSecretSource: 'EXTERNAL',
            clientSecretConfig: { secretId: agentClientSecret.secretArn, jsonKey: 'client_secret' },
            clientAuthenticationMethod: 'CLIENT_SECRET_POST',
            onBehalfOfTokenExchangeConfig: {
              grantType: 'TOKEN_EXCHANGE',
              tokenExchangeGrantTypeConfig: { actorTokenContent: 'NONE' },
            },
          },
        },
      },
    );
    // The discovery URL is a string, so CloudFormation cannot infer that the stage serving it
    // must exist first, and the service may fetch the document at create.
    credentialProvider.node.addDependency(this.api.deploymentStage);

    new ssm.StringParameter(this, 'ParamTokenIssuerUrl', {
      parameterName: paramPath(config.envName, 'tokenIssuerUrl'),
      stringValue: issuerUrl,
      description: 'Token-service issuer (ADR-077 §3); the discovery URL is built from it.',
    });
    new ssm.StringParameter(this, 'ParamTokenRouteExchangeFunctionArn', {
      parameterName: paramPath(config.envName, 'tokenRouteExchangeFunctionArn'),
      stringValue: routeExchange.functionArn,
      description: 'Route exchange invoked directly by the Conversation Function (ADR-077 §3).',
    });
    new ssm.StringParameter(this, 'ParamTokenSigningKeyArn', {
      parameterName: paramPath(config.envName, 'tokenSigningKeyArn'),
      stringValue: this.signingKey.keyArn,
      description: 'Token-service signing key; tools read its public half (ADR-077 §5).',
    });
    new ssm.StringParameter(this, 'ParamTokenCredentialProviderArn', {
      parameterName: paramPath(config.envName, 'tokenCredentialProviderArn'),
      stringValue: credentialProvider.attrCredentialProviderArn,
      description: 'AgentCore OAuth2 credential provider for the agent client (ADR-077 S4).',
    });
    new ssm.StringParameter(this, 'ParamAgentClientSecretArn', {
      parameterName: paramPath(config.envName, 'agentClientSecretArn'),
      stringValue: agentClientSecret.secretArn,
      description: 'Agent client secret the credential provider presents at /token (ADR-077 S4).',
    });

    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }

  /**
   * AWS WAF on the issuer stage: a per-IP rate rule and the AWS IP-reputation group.
   *
   * On the stage rather than behind CloudFront, because a distribution in front would leave the
   * API's own endpoint reachable without it (ADR-077 §3). WAFv2 has no L2 construct.
   */
  /**
   * The exact CloudFormation execution role this stack deploys with, from its own synthesizer.
   *
   * The credential provider's deployment handler reads the agent client secret as this role
   * (probe P2), so the secret's Deny must admit it. Taken from the bound synthesizer rather than
   * a name pattern, so a bootstrap under another qualifier gets no read. Any other synthesizer
   * fails synth: the reader would then be unknown.
   */
  private cloudFormationExecutionRoleArn(): string {
    const synthesizer = this.synthesizer;
    if (!(synthesizer instanceof cdk.DefaultStackSynthesizer)) {
      throw new Error(
        'TokenServiceStack needs the DefaultStackSynthesizer to name the one CloudFormation '
        + 'execution role that may read the agent client secret.',
      );
    }
    // The synthesizer returns the ARN with CloudFormation pseudo-parameter placeholders.
    return cdk.Fn.sub(synthesizer.cloudFormationExecutionRoleArn);
  }

  private attachWaf(config: AppConfig): void {
    const metricPrefix = `adlytics-${config.envName}-token-issuer`;
    const visibility = (name: string): wafv2.CfnWebACL.VisibilityConfigProperty => ({
      cloudWatchMetricsEnabled: true,
      metricName: `${metricPrefix}-${name}`,
      sampledRequestsEnabled: true,
    });
    const acl = new wafv2.CfnWebACL(this, 'IssuerWebAcl', {
      name: metricPrefix,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: visibility('acl'),
      rules: [
        {
          name: 'RateLimitPerSourceIp',
          priority: 0,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: config.tokenService.wafRateLimitPerFiveMinutes,
              aggregateKeyType: 'IP',
              evaluationWindowSec: 300,
            },
          },
          visibilityConfig: visibility('rate-limit'),
        },
        {
          name: 'AwsIpReputation',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesAmazonIpReputationList',
            },
          },
          visibilityConfig: visibility('ip-reputation'),
        },
      ],
    });
    const association = new wafv2.CfnWebACLAssociation(this, 'IssuerWebAclAssociation', {
      resourceArn:
        `arn:${this.partition}:apigateway:${this.region}::/restapis/${this.api.restApiId}/stages/${TOKEN_SERVICE_STAGE}`,
      webAclArn: acl.attrArn,
    });
    // The ARN is a string, so CloudFormation cannot infer that the stage must exist first.
    association.node.addDependency(this.api.deploymentStage);
  }

  /** One arm64 Python function with its own named role and an explicitly declared log group. */
  private pythonFunction(
    id: string,
    options: {
      config: AppConfig;
      dependencies: lambda.ILayerVersion;
      sourceAssetPath: string;
      roleName: string;
      handler: string;
      environment: Record<string, string>;
      memoryMb: number;
      timeoutSeconds: number;
    },
  ): lambda.Function {
    const functionName = `adlytics-${options.config.envName}-${id}`;
    // Named, because the key policy and the route resource policy identify principals by
    // `aws:PrincipalArn`, which needs a name known before the role exists.
    const role = lambdaRole(this, `${id}Role`, { roleName: options.roleName });
    // Declared rather than left to `logRetention`, which provisions an x86_64 singleton function.
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: options.config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    grantOwnLogWrites(role, logGroup);
    return new lambda.Function(this, id, {
      functionName,
      role,
      runtime: new lambda.Runtime(options.config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: { arm64: lambda.Architecture.ARM_64 }[options.config.lambdaArchitecture],
      code: lambda.Code.fromAsset(options.sourceAssetPath),
      handler: options.handler,
      layers: [options.dependencies],
      environment: options.environment,
      memorySize: options.memoryMb,
      timeout: cdk.Duration.seconds(options.timeoutSeconds),
      logGroup,
    });
  }
}
