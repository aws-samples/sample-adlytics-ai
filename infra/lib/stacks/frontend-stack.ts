// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

import {
  API_PATH_PATTERN,
  AppConfig,
  CONVERSATION_PATH_PATTERN,
  EDGE_SECURITY_REGION,
  ORIGIN_VERIFY_HEADER,
  originVerifySecretName,
  paramPath,
} from '../config';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyStandardTags } from '../mixins/tagging';

/**
 * The viewer-request check on both API behaviours (ADR-078 §5): only a canonical path reaches an
 * API origin. CloudFront normalizes a path only to choose a behaviour and sends the raw path to the
 * origin (authentication.md, edge entry), and API Gateway folds these
 * spellings into a route, so each is refused here: an empty segment (`//`), a `.` or `..` segment,
 * a backslash, and any percent escape. No API route needs one; every path parameter is a canonical
 * UUID. That the function sees the raw path is proven by the review API probe's path variants.
 */
export const CANONICAL_API_PATH_CODE = `
function handler(event) {
  var uri = event.request.uri;
  if (uri.indexOf('//') !== -1 || uri.indexOf('%') !== -1 || uri.indexOf('\\\\') !== -1 ||
      /\\/\\.\\.?(\\/|$)/.test(uri)) {
    return { statusCode: 400, statusDescription: 'Bad Request' };
  }
  return event.request;
}
`;

/** The exported page every unknown extensionless path is served (ADR-123). */
export const NOT_FOUND_PAGE = '/404.html';

/** The committed `contracts/generated/route-allowlist.json`, validated on read. */
export interface RouteAllowlist {
  /** Canonical slash-delimited routes, `/` included, each served from its `index.html`. */
  readonly routes: readonly string[];
}

/**
 * Read the route allowlist `presentation_validation.navigation` projects from
 * `presentation/navigation.yaml` (ADR-115, ADR-123 decision 1). Validated rather than trusted: the
 * file is committed, and a malformed route would otherwise reach a deployed function as a page
 * nobody can open, with nothing naming the file.
 */
export function loadRouteAllowlist(filePath: string): RouteAllowlist {
  const artifact = 'contracts/generated/route-allowlist.json';
  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch (error) {
    throw new Error(`Could not read ${artifact} at ${filePath}: ${(error as Error).message}`);
  }
  const document = parsed as { not_found_page?: unknown; routes?: unknown; version?: unknown };
  const keys = typeof parsed === 'object' && parsed !== null ? Object.keys(parsed).sort() : [];
  if (keys.join(',') !== 'not_found_page,routes,version' || document.version !== 1) {
    throw new Error(`${artifact} requires exactly version 1, not_found_page and routes.`);
  }
  if (document.not_found_page !== NOT_FOUND_PAGE) {
    throw new Error(`${artifact} must name ${NOT_FOUND_PAGE} as its not-found page.`);
  }
  const routes = document.routes;
  const canonical = (route: unknown): route is string =>
    typeof route === 'string' && /^\/(?:[a-z0-9-]+\/)*$/.test(route);
  if (!Array.isArray(routes) || !routes.every(canonical) || new Set(routes).size !== routes.length) {
    throw new Error(`${artifact} routes must be unique canonical slash-delimited paths.`);
  }
  if (!routes.includes('/')) throw new Error(`${artifact} routes must include /.`);
  return { routes };
}

/**
 * Where a static request is served from (ADR-123 decision 1), shared by both functions below so the
 * request rewrite and the response status can't disagree. A path whose last segment has a `.`
 * passes unchanged; an allowlisted route, with or without its trailing slash, is served from its
 * `index.html`; every other extensionless path is served the not-found page.
 */
function staticResolution(allowlist: RouteAllowlist): string {
  return `
var ROUTES = ${JSON.stringify(allowlist.routes)};
var NOT_FOUND_PAGE = ${JSON.stringify(NOT_FOUND_PAGE)};
function resolve(uri) {
  var leaf = uri.substring(uri.lastIndexOf('/') + 1);
  if (leaf.indexOf('.') !== -1) {
    return uri;
  }
  var route = uri.endsWith('/') ? uri : uri + '/';
  return ROUTES.indexOf(route) !== -1 ? route + 'index.html' : NOT_FOUND_PAGE;
}
`;
}

/** The viewer-request function on the default behaviour: rewrite static GET and HEAD paths. */
export function staticRouteRewriteCode(allowlist: RouteAllowlist): string {
  return `${staticResolution(allowlist)}
function handler(event) {
  var request = event.request;
  var method = request.method;
  if (method !== 'GET' && method !== 'HEAD') {
    return request;
  }
  request.uri = resolve(request.uri);
  return request;
}
`;
}

/**
 * The viewer-response function on the default behaviour (ADR-123 decision 3): a successful response
 * that is the not-found page is sent as 404, keeping the cached body. The CloudFront guide says the
 * event's request is the one received from the viewer, and doesn't say whether it carries the
 * viewer-request rewrite, so the decision holds either way: the rewritten `/404.html` and the
 * original unknown path both resolve to the not-found page. Any other status (a 304, an S3 403)
 * passes unchanged.
 */
export function notFoundStatusCode(allowlist: RouteAllowlist): string {
  return `${staticResolution(allowlist)}
function handler(event) {
  var response = event.response;
  var method = event.request.method;
  if ((method === 'GET' || method === 'HEAD') && response.statusCode === 200 &&
      resolve(event.request.uri) === NOT_FOUND_PAGE) {
    response.statusCode = 404;
    response.statusDescription = 'Not Found';
  }
  return response;
}
`;
}

export interface FrontendStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** The committed `contracts/generated/route-allowlist.json`, resolved by `bin/app.ts`. */
  readonly routeAllowlistPath: string;
}

/**
 * S3 plus one CloudFront distribution serving both the frontend and the API.
 *
 * The single origin is the point. With the frontend on `d111.cloudfront.net` and
 * the API on `execute-api...`, those are cross-site and `SameSite=Lax` cookies are
 * never sent — which is why F-03 concluded custom domains were mandatory. Routing
 * `/api/*` through the same distribution makes every request same-origin, which is
 * strictly stronger and needs no custom domain at all.
 *
 * Deployed twice when bringing an environment up: once without the API origin, to
 * learn the distribution's domain, then again once the API exists. See
 * `EdgeConfig.apiOriginAttached`.
 */
export class FrontendStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly bucket: s3.Bucket;
  /** ADR-078: the value CloudFront adds to every API origin request, `{"current", "previous"}`. */
  public readonly originVerifySecret: secretsmanager.Secret;
  private readonly canonicalApiPath: cloudfront.Function;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    // Every stack, without exception — see mixins/tagging. Applied through an Aspect, so
    // position in the constructor does not matter and every later resource is covered.
    applyStandardTags(this, props.config);
    const { config } = props;

    this.bucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // The site is a build artefact; nothing here is a source of truth.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      // No autoDeleteObjects. CDK's helper for it is a core custom-resource provider whose role
      // takes AWSLambdaBasicExecutionRole (log writes on `*`) and accepts no role of its own
      // (M-IAM-DISPATCH-LOGS-001). S3 refuses to delete a non-empty bucket, so the purge the
      // helper did is a step of `make destroy-all` (scripts/destroy-all.sh), which empties this
      // bucket before the Frontend stack is destroyed (ADR-085). A bare `cdk destroy` of this
      // stack fails on a built site.
    });

    // Applied to the static behaviour only. A CSP on a JSON API response does
    // nothing useful, and ADR-008 chose CloudFront over Amplify precisely because
    // this policy is configurable — the cookie design depends on it.
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            // Same-origin only. There is no cross-origin API to allow.
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
          ].join('; '),
        },
        strictTransportSecurity: {
          override: true,
          accessControlMaxAge: cdk.Duration.days(365),
          includeSubdomains: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { override: true, frameOption: cloudfront.HeadersFrameOption.DENY },
        referrerPolicy: {
          override: true,
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
        },
      },
    });

    // ADR-078, authentication.md edge entry. Generated once; rotated only by the operator procedure
    // there, never automatically, because each rotation must be coupled to a redeploy of this
    // stack. Anyone who can read the distribution config can read the current value.
    this.originVerifySecret = new secretsmanager.Secret(this, 'OriginVerifySecret', {
      secretName: originVerifySecretName(config.envName),
      description: 'Origin-verify header CloudFront adds to every API origin request (ADR-078)',
      generateSecretString: {
        secretStringTemplate: '{}',
        generateStringKey: 'current',
        passwordLength: 64,
        excludePunctuation: true,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.canonicalApiPath = new cloudfront.Function(this, 'CanonicalApiPath', {
      comment: 'Refuse a non-canonical API path before it reaches an API origin (ADR-078).',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(CANONICAL_API_PATH_CODE),
    });

    // ADR-123: only manifest routes reach their index document; any other extensionless path is
    // served the exported not-found page, sent as 404 by the viewer-response function.
    const allowlist = loadRouteAllowlist(props.routeAllowlistPath);
    const staticRouteRewrite = new cloudfront.Function(this, 'StaticRouteRewrite', {
      comment: 'Serve manifest routes their index document and other paths the not-found page.',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(staticRouteRewriteCode(allowlist)),
    });
    const notFoundStatus = new cloudfront.Function(this, 'NotFoundStatus', {
      comment: 'Send the not-found page with status 404 (ADR-123 decision 3).',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(notFoundStatusCode(allowlist)),
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `adlytics-${config.envName}`,
      // ADR-078: every request, to every behaviour, passes the edge web ACL first.
      webAclId: this.edgeWebAclArn(config),
      defaultRootObject: 'index.html',
      defaultBehavior: {
        // The cast is friction between `exactOptionalPropertyTypes` and
        // aws-cdk-lib's IBucket, which declares `isWebsite?: boolean`. Not a
        // design compromise: OAC is the access path and the bucket stays private.
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket as s3.IBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: securityHeaders,
        functionAssociations: [
          {
            function: staticRouteRewrite,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
          {
            function: notFoundStatus,
            eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE,
          },
        ],
      },
      // Chat first, then the API. **The order is load-bearing.** CloudFront compares the request
      // path against each behaviour in the order they are listed and the first match wins — it does
      // not prefer the more specific pattern. `/api/*` would therefore swallow `/api/chat`, sending
      // every conversation to API Gateway and into the 29s timeout ADR-038 exists to escape. The
      // failure would look like a slow chat rather than a misrouted one.
      additionalBehaviors: {
        ...(config.edge.conversationOriginAttached ? this.conversationBehaviour(config) : {}),
        ...(config.edge.apiOriginAttached ? this.apiBehaviour(config) : {}),
      },

      // Deliberately NO errorResponses. Custom error responses live on the
      // distribution, not on a cache behaviour, so there is no way to scope them to
      // the static site — and the usual SPA mapping rewrites 403 to index.html with
      // a 200. Our authorizer denies with 403, and 403 is what triggers a token
      // refresh. That mapping would turn every expired session into a successful
      // response containing HTML, and nothing would report an error.
      //
      // Static deep links and unknown paths are handled by `StaticRouteRewrite` and
      // `NotFoundStatus` on the default behaviour (ADR-123). They act only on static GET/HEAD
      // paths; API and chat use separate cache behaviours and therefore never encounter them, so
      // the authorizer's 403 reaches the client unchanged.

      // No minimumProtocolVersion. This distribution uses the default *.cloudfront.net certificate,
      // for which CloudFront fixes the viewer security policy at TLSv1 whatever is set
      // (CloudFormation ViewerCertificate reference,
      // https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-cloudfront-distribution-viewercertificate.html
      // [verified 2026-10-02]), and CDK ignores the prop with a warning. A TLS 1.2 minimum needs a
      // custom domain: set `certificate` (ACM, us-east-1) and `domainNames` together with
      // `minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021`.
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      enableLogging: false,
    });

    this.publishParameters(config);
    this.publishOutputs();
  }

  /**
   * Route `/api/*` to the HTTP API.
   *
   * Caching is disabled and the whole viewer request is forwarded except the Host
   * header. Both matter: a cached authorizer response would serve one user's
   * session to another, and stripping the `Cookie` header would leave the
   * authorizer with no credential to check at all — the failure F-03 warns about.
   * The Host header must not be forwarded, because API Gateway rejects a Host it
   * does not own.
   */
  private apiBehaviour(config: AppConfig): Record<string, cloudfront.BehaviorOptions> {
    const apiId = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'apiId'),
    );

    return {
      [API_PATH_PATTERN]: {
        origin: new origins.HttpOrigin(`${apiId}.execute-api.${config.region}.amazonaws.com`, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
          // ADR-078: the authorizer's identity source and the auth routes' first check. CloudFront
          // overwrites a viewer header of the same name, so a viewer cannot supply it.
          customHeaders: { [ORIGIN_VERIFY_HEADER]: this.originVerifyValue(config) },
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        functionAssociations: [
          { function: this.canonicalApiPath, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
    };
  }

  /**
   * The current origin-verify value, as a dynamic reference CloudFormation resolves at deploy.
   *
   * Versionless unless a rotation pins a version (`EdgeConfig.originVerifySecretVersionId`):
   * CloudFormation re-reads a dynamic reference only when the resource holding it changes, so the
   * pin is what makes the rotation redeploy pick up the new value.
   */
  private originVerifyValue(config: AppConfig): string {
    const versionId = config.edge.originVerifySecretVersionId;
    return cdk.SecretValue.secretsManager(this.originVerifySecret.secretArn, {
      jsonField: 'current',
      ...(versionId === undefined ? {} : { versionId }),
    }).unsafeUnwrap();
  }

  /**
   * The edge web ACL's ARN, which `EdgeSecurityStack` publishes in us-east-1.
   *
   * In us-east-1 it is a plain parameter read. In any other application region it is one SDK call
   * pinned to us-east-1, the cross-region parameter read the stack-composition rules allow. Its
   * physical id is the parameter name, so it reads on create and whenever the name changes, and
   * NOT when the web ACL behind the parameter is replaced. Outside us-east-1, replacing the edge
   * web ACL therefore also needs a change to this read's inputs, or the distribution keeps the old
   * ACL; in us-east-1 the plain parameter read follows the new ARN on the next deploy.
   *
   * The SDK call runs in CDK's singleton Node function, a deliberate exception to "every Lambda is
   * arm64": a template has no other way to read a value in another region, and the function
   * exists only outside us-east-1. Its log group is declared, so it carries the platform retention
   * rather than never expiring, and its role writes only that group (ADR-085).
   */
  private edgeWebAclArn(config: AppConfig): string {
    const name = paramPath(config.envName, 'edgeWebAclArn');
    if (this.region === EDGE_SECURITY_REGION) {
      return ssm.StringParameter.valueForStringParameter(this, name);
    }
    const functionName = `adlytics-${config.envName}-EdgeWebAclArnRead`;
    const logGroup = new logs.LogGroup(this, 'EdgeWebAclArnReadLogs', {
      logGroupName: `/aws/lambda/${functionName}`,
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const role = lambdaRole(this, 'EdgeWebAclArnReadRole');
    grantOwnLogWrites(role, logGroup);
    const read = new cr.AwsCustomResource(this, 'EdgeWebAclArnRead', {
      functionName,
      logGroup,
      role,
      onUpdate: {
        service: 'SSM',
        action: 'getParameter',
        parameters: { Name: name },
        region: EDGE_SECURITY_REGION,
        physicalResourceId: cr.PhysicalResourceId.of(name),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['ssm:GetParameter'],
          resources: [
            this.formatArn({
              service: 'ssm',
              region: EDGE_SECURITY_REGION,
              resource: 'parameter',
              resourceName: name.slice(1),
            }),
          ],
        }),
      ]),
      installLatestAwsSdk: false,
    });
    return read.getResponseField('Parameter.Value');
  }

  /**
   * Route the chat path to the conversation Function URL, signed with Origin Access Control.
   *
   * ADR-038. A Function URL rather than the HTTP API because a deep dive streams for tens of
   * seconds and HTTP API's integration timeout is a fixed 29s.
   *
   * **The origin is signed, not merely pointed at.** The Function URL is `AuthType: AWS_IAM`, so an
   * unsigned request to it is refused — which is what makes a URL that is technically on the
   * internet unreachable by anything but this distribution. OAC signs each origin request with
   * SigV4 using CloudFront's own identity; `ApiStack` grants that identity the two actions a
   * Function URL requires and scopes them to this distribution's ARN.
   *
   * Written with an explicit OAC and a plain HTTP origin rather than
   * `FunctionUrlOrigin.withOriginAccessControl`. That helper needs an `IFunctionUrl` object, which
   * does not cross a stack boundary, and it emits its own — incomplete — invoke permission, which
   * would be a third statement contradicting the two `ApiStack` writes deliberately.
   */
  private conversationBehaviour(config: AppConfig): Record<string, cloudfront.BehaviorOptions> {
    // Published by ApiStack. The host is taken from the URL rather than published separately, so
    // there is only ever one statement of where this function lives.
    const functionUrl = ssm.StringParameter.valueForStringParameter(
      this,
      paramPath(config.envName, 'conversationFunctionUrl'),
    );
    const domainName = cdk.Fn.select(2, cdk.Fn.split('/', functionUrl));

    const oac = new cloudfront.FunctionUrlOriginAccessControl(this, 'ConversationOac', {
      // Always, rather than only when the viewer sent no Authorization header. The viewer never
      // sends one here — the session is a cookie — and `NEVER` would leave the origin request
      // unsigned, which an AWS_IAM Function URL rejects.
      signing: cloudfront.Signing.SIGV4_ALWAYS,
    });

    return {
      [CONVERSATION_PATH_PATTERN]: {
        origin: new SignedHttpOrigin(domainName, oac, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
          // The idle-between-bytes budget, not the total. A turn streams for tens of seconds but
          // emits an event roughly every second, so what matters is the longest silence — the model
          // thinking between tool calls. 60s is the ceiling without a service quota increase, and
          // requiring a per-customer quota change is a cost ADR-038 declined for the whole design.
          readTimeout: cdk.Duration.seconds(60),
          keepaliveTimeout: cdk.Duration.seconds(60),
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        // A conversation is never cached. Every answer is per-user and per-session, and a cached
        // one would be served to the next person asking the same question.
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: this.conversationOriginRequestPolicy(),
        functionAssociations: [
          { function: this.canonicalApiPath, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
    };
  }

  /**
   * What to forward to the conversation origin, and the two headers that must not be.
   *
   * A custom policy rather than `ALL_VIEWER_EXCEPT_HOST_HEADER`, which the API behaviour uses,
   * because two headers make a SigV4-signed origin fail in ways that read as a permissions problem:
   *
   * - **`Authorization` must not be forwarded.** CloudFront overwrites it with the SigV4 signature
   *   it generates. Forwarding the viewer's value puts it in the signed request and the signature no
   *   longer matches, giving a 403 that names nothing.
   * - **`Host` must not be forwarded.** The signature covers the host, and it has to be the Function
   *   URL's own hostname rather than the distribution's.
   *
   * `x-amz-content-sha256` is deliberately absent from this list even though the origin needs it:
   * CloudFront **refuses** it in an allowlist with `InvalidArgument`, because it consumes the header
   * as signing input rather than forwarding it [verified 2026-08-22]. The browser must still send a
   * SHA-256 of the request body so that CloudFront's own signature covers the payload.
   */
  private conversationOriginRequestPolicy(): cloudfront.OriginRequestPolicy {
    return new cloudfront.OriginRequestPolicy(this, 'ConversationOriginRequestPolicy', {
      comment: 'Conversation route: everything the turn needs, minus what SigV4 owns',
      headerBehavior: cloudfront.OriginRequestHeaderBehavior.allowList(
        'content-type',
        // The CSRF double-submit header, spelled exactly as `domain/identity/authorization`
        // declares it and `frontend/lib/session.ts` sends it. A near-miss here — `x-csrf-token`,
        // the conventional name — is stripped at the edge, so every mutating request fails the
        // double-submit and reports 401 with the cookie present and correct.
        'x-adl-csrf',
        'sec-fetch-site',
        'origin',
        'accept',
        // No `x-correlation-id`: the conversation route mints its own per request and returns
        // it (ADR-081 §2), so a client value is stripped here rather than ignored downstream.
      ),
      // Cookies carry the session, so all of them. There is no single-cookie selection that also
      // keeps the CSRF cookie, and both are required by the same check.
      cookieBehavior: cloudfront.OriginRequestCookieBehavior.all(),
      queryStringBehavior: cloudfront.OriginRequestQueryStringBehavior.none(),
    });
  }

  /**
   * Human-facing values for `aws cloudformation describe-stacks`.
   *
   * Deliberately without `exportName`. An output with one becomes a
   * CloudFormation export, which cannot be changed or deleted while any stack
   * imports it — the deadlock ADR-012 replaced with SSM. Without one it is
   * describable and nothing more, so these are for operators and post-deploy
   * checks, never a mechanism for one stack to read another.
   */
  private publishOutputs(): void {
    new cdk.CfnOutput(this, 'OutPublicBaseUrl', {
      value: `https://${this.distribution.distributionDomainName}`,
      description: 'The single origin serving both the app and the API.',
    });
    new cdk.CfnOutput(this, 'OutDistributionId', {
      value: this.distribution.distributionId,
      description: 'Distribution to invalidate after a frontend build.',
    });
    new cdk.CfnOutput(this, 'OutSiteBucketName', {
      value: this.bucket.bucketName,
      description: 'Bucket the built site is synced to.',
    });
  }

  private publishParameters(config: AppConfig): void {
    new ssm.StringParameter(this, 'ParamPublicBaseUrl', {
      parameterName: paramPath(config.envName, 'publicBaseUrl'),
      // What the browser talks to, and the only origin cookies are ever set on.
      stringValue: `https://${this.distribution.distributionDomainName}`,
    });
    new ssm.StringParameter(this, 'ParamDistributionId', {
      parameterName: paramPath(config.envName, 'distributionId'),
      stringValue: this.distribution.distributionId,
    });
    new ssm.StringParameter(this, 'ParamBucketName', {
      parameterName: paramPath(config.envName, 'frontendBucketName'),
      stringValue: this.bucket.bucketName,
    });
  }
}

/**
 * An HTTP origin that CloudFront signs with SigV4 using an Origin Access Control.
 *
 * A local subclass because there is no L2 for this shape. `S3BucketOrigin` and `FunctionUrlOrigin`
 * both attach an OAC, but each needs the construct it fronts — an `IBucket`, an `IFunctionUrl` —
 * and neither crosses a stack boundary. `HttpOrigin` takes the hostname this stack actually has and
 * has no way to carry an OAC id, so the id is injected into the rendered origin here.
 *
 * The override is three lines and adds one property to what the base class already produces. It
 * deliberately does **not** grant an invoke permission: `FunctionUrlOrigin` does, and grants only
 * `lambda:InvokeFunctionUrl` of the two a Function URL requires. `ApiStack` writes both statements
 * itself, where the function is, and a permission written here as well would be a third statement
 * with different conditions.
 */
class SignedHttpOrigin extends origins.HttpOrigin {
  constructor(
    domainName: string,
    private readonly originAccessControl: cloudfront.IOriginAccessControlRef,
    props?: origins.HttpOriginProps,
  ) {
    super(domainName, props);
  }

  public bind(
    scope: Construct,
    options: cloudfront.OriginBindOptions,
  ): cloudfront.OriginBindConfig {
    const bound = super.bind(scope, options);
    if (bound.originProperty === undefined) {
      // Cannot happen with the current base class, and is thrown rather than defaulted because the
      // alternative is an origin rendered with no OAC id — which deploys, and then 403s on every
      // request against an AWS_IAM Function URL with nothing naming the cause.
      throw new Error('HttpOrigin.bind produced no origin property to attach an OAC to.');
    }
    return {
      ...bound,
      originProperty: {
        ...bound.originProperty,
        originAccessControlId: this.originAccessControl.originAccessControlRef.originAccessControlId,
      },
    };
  }
}
