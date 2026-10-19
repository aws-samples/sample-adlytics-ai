// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { AppConfig, clientSecretName, paramPath } from '../config';
import { GuardedWebAcl, WebAclRule } from '../constructs/edge/guarded-web-acl';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyStandardTags } from '../mixins/tagging';

/**
 * Cognito user pool, app client and managed login domain.
 *
 * Separate from ApiStack, and RETAIN, because destroying an HTTP API is a
 * redeploy while destroying a user pool deletes every account in it and no
 * restore brings the users back. Blast radius decides the boundary before
 * logical grouping does — see ADR-012 and the stack composition steering.
 *
 * Everything downstream reads this through SSM. There is no construct reference
 * across the boundary, so ApiStack can be torn down and rebuilt without
 * touching a single user.
 */
/**
 * The user pool's own rules, in evaluation order (ADR-078, authentication.md edge entry).
 *
 * IP reputation and a per-source-IP rate rule only. The request-content groups the distribution
 * runs are left out: Cognito forwards no body for managed login pages and never exposes a
 * username, password, or other PII to the rules, so they would inspect nothing a sign-in carries.
 * Both block since 2026-09-29, after the count run matched no request (validation/RESULTS.md,
 * edge slice).
 */
export function userPoolWebAclRules(config: AppConfig): readonly WebAclRule[] {
  return [
    { kind: 'managed', name: 'AwsIpReputation', managedGroup: 'AWSManagedRulesAmazonIpReputationList', mode: 'block' },
    {
      kind: 'rate',
      name: 'RateLimitPerSourceIp',
      limitPerFiveMinutes: config.auth.wafRateLimitPerFiveMinutes,
      mode: 'block',
    },
  ];
}
/** The review CLI's one loopback callback; the CLI listens on this fixed port (ADR-078). */
export const REVIEW_CLI_CALLBACK_URL = 'http://localhost:8765/callback';

export interface AuthStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath?: string;
}

export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    // Every stack, without exception — see mixins/tagging. Applied through an Aspect, so
    // position in the constructor does not matter and every later resource is covered.
    applyStandardTags(this, props.config);
    const { config, sourceAssetPath } = props;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `adlytics-${config.envName}`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      signInCaseSensitive: false,
      standardAttributes: { email: { required: true, mutable: false } },
      // Every account is created by an administrator. This is a single-tenant
      // platform for one advertiser's staff, so open sign-up would be a defect
      // rather than a feature.
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true, email: false },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      // RETAIN is the whole reason this stack exists. Never relax it for a dev
      // environment either: a dev pool still holds the accounts people test with,
      // and a habit formed in dev is the habit applied in prod.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      // Managed login requires Essentials or above. The Lite tier only offers the
      // classic hosted UI, which lacks passkey support — see docs/decisions/adr-007-auth-cookie-bff.md.
    });

    const reviewTokenLogs = new logs.LogGroup(this, 'QualityReviewTokenLogs', {
      logGroupName: `/aws/lambda/adlytics-${config.envName}-QualityReviewToken`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const reviewTokenRole = lambdaRole(this, 'QualityReviewTokenServiceRole');
    grantOwnLogWrites(reviewTokenRole, reviewTokenLogs);
    const reviewToken = new lambda.Function(this, 'QualityReviewToken', {
      functionName: `adlytics-${config.envName}-QualityReviewToken`,
      role: reviewTokenRole,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON),
      architecture: lambda.Architecture.ARM_64,
      code: lambda.Code.fromAsset(sourceAssetPath ?? __dirname),
      handler: 'adapters.jobs.quality_review_token.handler.handler',
      memorySize: 512,
      timeout: cdk.Duration.seconds(10),
      logGroup: reviewTokenLogs,
    });
    reviewToken.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    // ADR-078: the two review groups. The three per-level groups they replaced are gone; an
    // environment that still has members in them runs `make migrate-review-groups` first, then
    // waits one access-token lifetime before deploying this, since the verifiers now refuse the
    // retired levels.
    this.userPool.addGroup('QualityReviewer', { groupName: 'quality-reviewer' });
    this.userPool.addGroup('AuditAttribution', { groupName: 'audit-attribution' });
    this.userPool.addGroup('PromptAdmin', { groupName: 'prompt-admin' });
    this.userPool.addGroup('OperationsStatusRead', { groupName: 'operations-status-read' });
    // S1 (owner-gate item 5): may request ingestion runs on demand through the run-request routes.
    this.userPool.addGroup('OperationsRunRequesters', { groupName: 'operations-run-requesters' });
    this.userPool.addTrigger(
      cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
      reviewToken,
      cognito.LambdaVersion.V2_0,
    );

    // One origin for everything. The browser never sees the API Gateway host, so
    // the callback must land on CloudFront — that is where the cookie has to be set.
    //
    // Read from the parameter FrontendStack publishes, resolved at deploy. Stating
    // it in config would mean a human copying the distribution domain across a
    // deploy boundary, and a stale copy here does not fail: it sends users to a
    // callback Cognito will reject, which looks like a broken login, not a
    // misconfiguration. FrontendStack must therefore deploy first — DEPLOY_ORDER.
    const origin = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'publicBaseUrl'),
    );

    this.userPoolClient = this.userPool.addClient('BffClient', {
      userPoolClientName: `adlytics-bff-${config.envName}`,
      // A confidential client with a secret. The exchange happens server-side in
      // the BFF, never in the browser, so a secret is both safe and correct here.
      generateSecret: true,
      authFlows: {
        // No password flow outside dev. InitiateAuth cannot sign in a federated user, so
        // enabling one would foreclose corporate SSO while looking like a convenience — see
        // ADR-007. In dev only, the validation flag enables USER_PASSWORD_AUTH for synthetic
        // validation identities; it needs this client's secret (ADR-078). No adminUserPassword.
        userSrp: false,
        userPassword: config.auth.qualityReviewValidationPasswordAuthEnabled,
        custom: false,
      },
      oAuth: {
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false },
        // Implicit grant returns tokens in a URL fragment, which is the exact
        // thing this architecture exists to prevent.
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        // Must match the handler's derived redirect_uri character for character.
        // Cognito compares this string on both the authorize and token calls.
        callbackUrls: [`${origin}/api/auth/callback`],
        logoutUrls: [origin],
      },
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      // Required for logout to mean anything. Without revocation, clearing the
      // cookies leaves a copied refresh token working until it expires.
      accessTokenValidity: cdk.Duration.minutes(15),
      idTokenValidity: cdk.Duration.minutes(15),
      refreshTokenValidity: cdk.Duration.days(config.auth.refreshTokenValidityDays),
    });

    const domain = this.userPool.addDomain('ManagedLogin', {
      cognitoDomain: { domainPrefix: config.auth.loginDomainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    // Managed login v2 renders its pages from a branding style, and a client without
    // one serves 403 "Login pages unavailable" from /login while /oauth2/authorize
    // still redirects normally. Nothing in the pool, the client or the domain reports
    // as unhealthy, so the failure only appears when a human tries to sign in.
    //
    // UseCognitoProvidedValues takes Cognito's defaults, which is the whole styling
    // requirement satisfied without committing this repository to a colour scheme
    // customers would have to unpick. Supplying Settings or Assets is how a customer
    // brands it later; the two are mutually exclusive with this flag.
    const branding = new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: this.userPool.userPoolId,
      clientId: this.userPoolClient.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    // The domain is what activates managed login; branding created before it exists
    // is rejected.
    branding.node.addDependency(domain);

    const publicClient = this.reviewCliClient(config);
    // The same managed login pages, for the same reason: without a style the CLI's sign-in 403s.
    const publicBranding = new cognito.CfnManagedLoginBranding(this, 'PublicClientManagedLoginBranding', {
      userPoolId: this.userPool.userPoolId,
      clientId: publicClient.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    publicBranding.node.addDependency(domain);

    // The client secret goes to Secrets Manager rather than to a Lambda
    // environment variable. An environment variable is readable by anyone holding
    // lambda:GetFunctionConfiguration, which is a far wider set of principals than
    // should ever see this value.
    const clientSecret = new secretsmanager.Secret(this, 'BffClientSecret', {
      secretName: clientSecretName(config.envName),
      description: 'Cognito app client secret for the BFF confidential client',
      secretStringValue: cdk.SecretValue.resourceAttribute(
        this.clientSecretRead(config).getResponseField('UserPoolClient.ClientSecret'),
      ),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.attachWebAcl(config);
    this.publishParameters(config, domain, clientSecret, publicClient);
    this.publishOutputs(domain, origin);
    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
  }
  /**
   * ADR-078: a regional web ACL associated directly with the user pool. Cognito sends each managed
   * login and unauthenticated user pool API request to AWS WAF and enforces the verdict itself, so
   * no API layer sits in front of the pool. Here rather than in a stack of its own because a
   * regional ACL must be in the pool's region and the pool is created in this stack.
   */
  /**
   * Read the BFF client's secret once, through an explicit SDK call (ADR-085).
   *
   * This is the call `UserPoolClient.userPoolClientSecret` makes, but that getter builds its
   * `AwsCustomResource` with no role, log group or name, so its singleton function took
   * `AWSLambdaBasicExecutionRole` (log writes on `*`). Declared here, the function writes only
   * its own group. The response carries the secret, so its logging is hidden.
   */
  private clientSecretRead(config: AppConfig): cr.AwsCustomResource {
    const functionName = `adlytics-${config.envName}-ClientSecretRead`;
    const logGroup = new logs.LogGroup(this, 'ClientSecretReadLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = lambdaRole(this, 'ClientSecretReadRole');
    grantOwnLogWrites(role, logGroup);
    return new cr.AwsCustomResource(this, 'ClientSecretRead', {
      functionName,
      logGroup,
      role,
      onUpdate: {
        service: 'CognitoIdentityServiceProvider',
        action: 'describeUserPoolClient',
        parameters: {
          UserPoolId: this.userPool.userPoolId,
          ClientId: this.userPoolClient.userPoolClientId,
        },
        physicalResourceId: cr.PhysicalResourceId.of(this.userPoolClient.userPoolClientId),
        logging: cr.Logging.withDataHidden(),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [this.userPool.userPoolArn] }),
      installLatestAwsSdk: false,
    });
  }

  private attachWebAcl(config: AppConfig): void {
    const acl = new GuardedWebAcl(this, 'UserPoolWebAcl', {
      name: `adlytics-${config.envName}-user-pool`,
      scope: 'REGIONAL',
      rules: userPoolWebAclRules(config),
      logRetention: config.logRetentionDays,
    });
    new wafv2.CfnWebACLAssociation(this, 'UserPoolWebAclAssociation', {
      resourceArn: this.userPool.userPoolArn,
      webAclArn: acl.acl.attrArn,
    });
  }

  /**
   * ADR-078: the public PKCE client a human reviewer's local CLI signs in with. Its access tokens
   * are admitted as a bearer on the two review routes only; the cookie stays the browser path.
   *
   * No secret, because a CLI cannot keep one. Authorization code only, `openid` only (never
   * `aws.cognito.signin.user.admin`, which would let the token call Cognito's user APIs), and one
   * loopback callback. Outside dev its only explicit auth flow is refresh; in dev the validation
   * flag adds IAM-authenticated ADMIN_USER_PASSWORD_AUTH so live probes need no browser. Refresh
   * lives the shortest Cognito allows, and the authorizer verifies tokens locally, so removing a
   * group or revoking a token ends access when the current 15-minute access token expires.
   */
  private reviewCliClient(config: AppConfig): cognito.UserPoolClient {
    return this.userPool.addClient('ReviewCliClient', {
      userPoolClientName: `adlytics-review-cli-${config.envName}`,
      generateSecret: false,
      authFlows: {
        userSrp: false,
        userPassword: false,
        custom: false,
        user: false,
        adminUserPassword: config.auth.qualityReviewValidationPasswordAuthEnabled,
      },
      oAuth: {
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false, clientCredentials: false },
        scopes: [cognito.OAuthScope.OPENID],
        // Stated, because the L2 defaults an OAuth client's callback to https://example.com.
        callbackUrls: [REVIEW_CLI_CALLBACK_URL],
        // None: a CLI has no signed-out page, and the L2 default is none.
      },
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      // The BFF client's access and ID lifetimes; refresh at Cognito's 60-minute floor.
      accessTokenValidity: cdk.Duration.minutes(15),
      idTokenValidity: cdk.Duration.minutes(15),
      refreshTokenValidity: cdk.Duration.minutes(60),
    });
  }


  /**
   * Human-facing values for `aws cloudformation describe-stacks`.
   *
   * Deliberately without `exportName`, so none of these becomes a
   * CloudFormation export. An export cannot be changed or deleted while another
   * stack imports it, which is the deadlock ADR-012 replaced with SSM. These are
   * for operators and post-deploy checks only.
   */
  private publishOutputs(domain: cognito.UserPoolDomain, origin: string): void {
    new cdk.CfnOutput(this, 'OutUserPoolId', {
      value: this.userPool.userPoolId,
      description: 'RETAIN. Deleting this deletes every user account.',
    });
    new cdk.CfnOutput(this, 'OutUserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Confidential BFF client. Its secret is in Secrets Manager.',
    });
    new cdk.CfnOutput(this, 'OutLoginDomain', {
      value: domain.domainName,
      description: 'Cognito managed login host. Globally unique.',
    });
    new cdk.CfnOutput(this, 'OutCallbackUrl', {
      value: `${origin}/api/auth/callback`,
      description: 'Must match what the handler builds, character for character.',
    });
  }

  /**
   * Publish everything a consumer needs, and nothing it does not.
   *
   * The client secret is published as an ARN rather than a value: it lives in the
   * pool and is fetched by the function that needs it. An SSM StringParameter is
   * not encrypted, so putting the secret itself here would leave it readable to
   * anyone with parameter read access.
   */
  private publishParameters(
    config: AppConfig,
    domain: cognito.UserPoolDomain,
    clientSecret: secretsmanager.Secret,
    publicClient: cognito.UserPoolClient,
  ): void {
    const put = (key: Parameters<typeof paramPath>[1], value: string): void => {
      new ssm.StringParameter(this, `Param${key}`, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
      });
    };

    put('userPoolId', this.userPool.userPoolId);
    put('userPoolClientId', this.userPoolClient.userPoolClientId);
    put('userPoolPublicClientId', publicClient.userPoolClientId);
    put('userPoolClientSecretName', clientSecret.secretName);
    put('authDomain', `${domain.domainName}.auth.${config.region}.amazoncognito.com`);
  }
}
