// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

import { ORIGIN_VERIFY_HEADER } from '../../config';

/**
 * Whether a rule only counts its matches or blocks them (ADR-078, authentication.md edge entry).
 *
 * Every rule starts at `count`, except a rule that only re-imposes a refusal the ACL already
 * applies in block mode (ADR-078 §1, amended 2026-10-05). A rule moves to `block` in a committed
 * change only after a full live probe run through the protected resource shows no match on
 * legitimate traffic.
 */
export type WebAclRuleMode = 'count' | 'block';

/** An AWS managed rule group, by its vendor name. */
export interface ManagedWebAclRule {
  readonly kind: 'managed';
  readonly name: string;
  readonly managedGroup: string;
  readonly mode: WebAclRuleMode;
  /**
   * Rules inside the group whose action is overridden to count (`ruleActionOverrides`). Every
   * other rule in the group keeps its own action. Absent or empty renders no override.
   */
  readonly countRules?: readonly string[];
}

/** A per-source-IP rate rule over AWS WAF's five-minute window. */
export interface RateWebAclRule {
  readonly kind: 'rate';
  readonly name: string;
  readonly limitPerFiveMinutes: number;
  readonly mode: WebAclRuleMode;
}

/**
 * One exact request shape: the method, the exact path with no transformation, and an empty query
 * string. Any other spelling of the path fails the match.
 */
export interface ExactRoute {
  readonly method: 'POST';
  readonly path: string;
}

/**
 * A request-body size ceiling, scoped to one exact route (`only`), to every request but that
 * route (`except`), or to every request (`all`, for a resource with no route to exempt, such as
 * the external MCP gateway, ADR-150). A body over the ACL's inspection limit matches
 * (`oversizeHandling: MATCH`), so the ceiling never fails open (ADR-078 §1, path-scoped body-size
 * exclusion).
 */
export interface BodySizeWebAclRule {
  readonly kind: 'body-size';
  readonly name: string;
  readonly maxBodyBytes: number;
  readonly route:
    | { readonly only: ExactRoute }
    | { readonly except: ExactRoute }
    | { readonly all: true };
  readonly mode: WebAclRuleMode;
}

export type WebAclRule = ManagedWebAclRule | RateWebAclRule | BodySizeWebAclRule;

/** The CloudFront body inspection limits AWS WAF accepts, in its own spelling. */
export type BodyInspectionLimit = 'KB_16' | 'KB_32' | 'KB_48' | 'KB_64';

/** AWS WAF's default body inspection limit for CloudFront, in bytes. */
const DEFAULT_BODY_INSPECTION_BYTES = 16_384;

const INSPECTION_LIMIT_BYTES: Record<BodyInspectionLimit, number> = {
  KB_16: 16_384,
  KB_32: 32_768,
  KB_48: 49_152,
  KB_64: 65_536,
};

/**
 * Renders the body-size statement. The only code that builds one: `oversizeHandling` lives on the
 * body field, never on the statement, because `SizeConstraintStatement` has no such property and
 * the field's default, `CONTINUE`, would measure an oversize body as the limit.
 */
export function renderBodySize(maxBodyBytes: number): wafv2.CfnWebACL.StatementProperty {
  return {
    sizeConstraintStatement: {
      fieldToMatch: { body: { oversizeHandling: 'MATCH' } },
      comparisonOperator: 'GT',
      size: maxBodyBytes,
      textTransformations: [{ priority: 0, type: 'NONE' }],
    },
  };
}

/**
 * The headers no WAF log record may hold a value of (ADR-078 acceptance item 7): the origin-verify
 * secret, a bearer, and the session cookies.
 */
export const REDACTED_HEADERS = [ORIGIN_VERIFY_HEADER, 'authorization', 'cookie'] as const;

export interface GuardedWebAclProps {
  /** The web ACL's name, which also prefixes every metric and names the log group. */
  readonly name: string;
  readonly scope: 'CLOUDFRONT' | 'REGIONAL';
  readonly rules: readonly WebAclRule[];
  readonly logRetention: logs.RetentionDays;
  /**
   * The body inspection limit for CloudFront (`AssociationConfig`). Only a `CLOUDFRONT` ACL takes
   * it; absent leaves AWS WAF's 16 KB default and renders nothing.
   */
  readonly bodyInspectionLimit?: BodyInspectionLimit;
  /**
   * Test seam only: replaces `renderBodySize`, so a test can prove the post-render guard refuses a
   * body-size statement without `oversizeHandling: MATCH`. Production callers never set it.
   */
  readonly bodySizeRenderOverride?: (maxBodyBytes: number) => wafv2.CfnWebACL.StatementProperty;
}

/**
 * A web ACL with a default allow, the given rules in order, and logging of matched requests only.
 *
 * Shared by the distribution's `CLOUDFRONT` ACL and the user pool's regional ACL, which follow the
 * same count-then-block rule and the same logging rule. WAFv2 has no L2 construct.
 *
 * - **Sampled requests are off.** A sampled request carries every header unredacted, and log
 *   redaction does not apply to sampling [verified 2026-09-29, `AWS::WAFv2::LoggingConfiguration`
 *   `RedactedFields`]. Metrics still count each rule's matches.
 * - **Only counted or blocked requests are logged,** with the three credential headers redacted. A
 *   matched request is what a count-to-block decision needs; a log of every allowed request would
 *   be a second access log holding nothing the decision uses.
 */
export class GuardedWebAcl extends Construct {
  public readonly acl: wafv2.CfnWebACL;
  public readonly logGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: GuardedWebAclProps) {
    super(scope, id);
    const visibility = (metric: string): wafv2.CfnWebACL.VisibilityConfigProperty => ({
      cloudWatchMetricsEnabled: true,
      metricName: `${props.name}-${metric}`,
      sampledRequestsEnabled: false,
    });

    if (props.bodyInspectionLimit !== undefined && props.scope !== 'CLOUDFRONT') {
      throw new Error(
        `GuardedWebAcl ${props.name}: bodyInspectionLimit applies only to a CLOUDFRONT ACL, not ${props.scope}.`,
      );
    }
    const inspectionBytes =
      props.bodyInspectionLimit === undefined
        ? DEFAULT_BODY_INSPECTION_BYTES
        : INSPECTION_LIMIT_BYTES[props.bodyInspectionLimit];
    const sizeRenderer = props.bodySizeRenderOverride ?? renderBodySize;
    const rules = props.rules.map((rule, priority) => {
      if (rule.kind === 'body-size') checkBodySizeRule(rule, inspectionBytes);
      const rendered = renderRule(rule, priority, visibility, sizeRenderer);
      if (rule.kind === 'body-size') checkOversizeHandling(rule.name, rendered.statement);
      return rendered;
    });

    this.acl = new wafv2.CfnWebACL(this, 'WebAcl', {
      name: props.name,
      scope: props.scope,
      defaultAction: { allow: {} },
      visibilityConfig: visibility('acl'),
      rules,
      ...(props.bodyInspectionLimit === undefined
        ? {}
        : {
            associationConfig: {
              requestBody: { CLOUDFRONT: { defaultSizeInspectionLimit: props.bodyInspectionLimit } },
            },
          }),
    });

    // AWS WAF requires the `aws-waf-logs-` prefix, and a `CLOUDFRONT` ACL's group must be in
    // us-east-1, which is where the stack owning that ACL is deployed.
    const logGroupName = `aws-waf-logs-${props.name}`;
    this.logGroup = new logs.LogGroup(this, 'Logs', {
      logGroupName,
      retention: props.logRetention,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const logging = new wafv2.CfnLoggingConfiguration(this, 'Logging', {
      resourceArn: this.acl.attrArn,
      // The group ARN without the `:*` suffix `LogGroup.logGroupArn` carries; AWS WAF refuses it.
      logDestinationConfigs: [
        cdk.Stack.of(this).formatArn({
          service: 'logs',
          resource: 'log-group',
          resourceName: logGroupName,
          arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
        }),
      ],
      // The L1 types `singleHeader` as `any` and passes it through, so its body is CloudFormation-cased.
      redactedFields: REDACTED_HEADERS.map((name) => ({ singleHeader: { Name: name } })),
      loggingFilter: {
        DefaultBehavior: 'DROP',
        Filters: [
          {
            Behavior: 'KEEP',
            Requirement: 'MEETS_ANY',
            Conditions: [
              { ActionCondition: { Action: 'COUNT' } },
              // A rule inside a managed group whose action the group override turns into a count.
              { ActionCondition: { Action: 'EXCLUDED_AS_COUNT' } },
              { ActionCondition: { Action: 'BLOCK' } },
            ],
          },
        ],
      },
    });
    logging.node.addDependency(this.logGroup);
  }
}

/**
 * Refuses an exempt path that is not one canonical spelling: it must start with `/`, hold no `?`,
 * `%`, `//` or `\`, have no `.` or `..` segment, and not end with `/`.
 */
export function checkCanonicalRoutePath(path: string): void {
  const segments = path.split('/').slice(1);
  const refused =
    !path.startsWith('/') ||
    path.length < 2 ||
    /[?%\\]/.test(path) ||
    path.includes('//') ||
    path.endsWith('/') ||
    segments.some((segment) => segment === '.' || segment === '..');
  if (refused) {
    throw new Error(`GuardedWebAcl: exact route path ${JSON.stringify(path)} is not canonical.`);
  }
}

function checkBodySizeRule(rule: BodySizeWebAclRule, inspectionBytes: number): void {
  if (!Number.isInteger(rule.maxBodyBytes) || rule.maxBodyBytes < 1 || rule.maxBodyBytes > inspectionBytes) {
    throw new Error(
      `GuardedWebAcl rule ${rule.name}: maxBodyBytes ${rule.maxBodyBytes} must be a whole number from 1 to ` +
        `the body inspection limit, ${inspectionBytes} bytes.`,
    );
  }
  if ('all' in rule.route) return;
  checkCanonicalRoutePath('only' in rule.route ? rule.route.only.path : rule.route.except.path);
}

/**
 * The post-render guard: every size constraint on the body inside a body-size rule carries
 * `oversizeHandling: 'MATCH'` on its body field, and no `oversizeHandling` key sits anywhere else.
 * It stops a later edit from dropping the field, which would let an oversize body fail open.
 */
function checkOversizeHandling(ruleName: string, statement: unknown): void {
  const fail = (why: string): never => {
    throw new Error(`GuardedWebAcl rule ${ruleName}: ${why}`);
  };
  const walk = (node: unknown, insideBody: boolean): void => {
    if (Array.isArray(node)) {
      for (const item of node) walk(item, false);
      return;
    }
    if (node === null || typeof node !== 'object') return;
    const entries = Object.entries(node as Record<string, unknown>);
    for (const [key, value] of entries) {
      if (key === 'oversizeHandling' && !insideBody) fail('oversizeHandling is set outside fieldToMatch.body.');
      if (key === 'sizeConstraintStatement') {
        const field = (value as { fieldToMatch?: { body?: { oversizeHandling?: unknown } } }).fieldToMatch;
        if (field?.body !== undefined && field.body.oversizeHandling !== 'MATCH') {
          fail('a body size constraint must set fieldToMatch.body.oversizeHandling to MATCH.');
        }
      }
      walk(value, key === 'body');
    }
  };
  walk(statement, false);
}

/**
 * The exact route as its three statements. An `only` rule puts them beside the size statement in
 * one AND, because AWS WAF refuses an AND nested directly inside an AND ("Your nested statement is
 * not valid", read 2026-10-06 from `CheckCapacity` and the failed dev deploy). An `except` rule
 * wraps them in their own AND under the NOT, which WAF accepts.
 */
function exactRouteStatements(route: ExactRoute): wafv2.CfnWebACL.StatementProperty[] {
  const none = [{ priority: 0, type: 'NONE' }];
  return [
    {
      byteMatchStatement: {
        fieldToMatch: { method: {} },
        positionalConstraint: 'EXACTLY',
        searchString: route.method,
        textTransformations: none,
      },
    },
    {
      byteMatchStatement: {
        fieldToMatch: { uriPath: {} },
        positionalConstraint: 'EXACTLY',
        searchString: route.path,
        textTransformations: none,
      },
    },
    {
      sizeConstraintStatement: {
        fieldToMatch: { queryString: {} },
        comparisonOperator: 'EQ',
        size: 0,
        textTransformations: none,
      },
    },
  ];
}

function renderRule(
  rule: WebAclRule,
  priority: number,
  visibility: (metric: string) => wafv2.CfnWebACL.VisibilityConfigProperty,
  sizeRenderer: (maxBodyBytes: number) => wafv2.CfnWebACL.StatementProperty,
): wafv2.CfnWebACL.RuleProperty {
  const metric = rule.name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
  if (rule.kind === 'body-size') {
    const size = sizeRenderer(rule.maxBodyBytes);
    const statement: wafv2.CfnWebACL.StatementProperty =
      'all' in rule.route
        ? size
        : 'only' in rule.route
        ? { andStatement: { statements: [...exactRouteStatements(rule.route.only), size] } }
        : {
            andStatement: {
              statements: [
                size,
                {
                  notStatement: {
                    statement: { andStatement: { statements: exactRouteStatements(rule.route.except) } },
                  },
                },
              ],
            },
          };
    return {
      name: rule.name,
      priority,
      action: rule.mode === 'block' ? { block: {} } : { count: {} },
      statement,
      visibilityConfig: visibility(metric),
    };
  }
  if (rule.kind === 'rate') {
    return {
      name: rule.name,
      priority,
      action: rule.mode === 'block' ? { block: {} } : { count: {} },
      statement: {
        rateBasedStatement: {
          limit: rule.limitPerFiveMinutes,
          aggregateKeyType: 'IP',
          evaluationWindowSec: 300,
        },
      },
      visibilityConfig: visibility(metric),
    };
  }
  return {
    name: rule.name,
    priority,
    // A rule group's own actions stand under `none`; `count` overrides every one of them to count.
    overrideAction: rule.mode === 'block' ? { none: {} } : { count: {} },
    statement: {
      managedRuleGroupStatement: {
        vendorName: 'AWS',
        name: rule.managedGroup,
        ...(rule.countRules === undefined || rule.countRules.length === 0
          ? {}
          : { ruleActionOverrides: rule.countRules.map((name) => ({ name, actionToUse: { count: {} } })) }),
      },
    },
    visibilityConfig: visibility(metric),
  };
}
