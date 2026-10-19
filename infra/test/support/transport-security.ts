// SPDX-License-Identifier: MIT-0
/**
 * Every queue, topic and bucket must refuse non-TLS requests (M-SQS-TLS-001).
 *
 * CDK opts each construct into a TLS-only resource policy with `enforceSSL: true`, and its default
 * is off, so one forgotten property leaves a resource reachable over plaintext. This matcher reads
 * the synthesised template, not the construct, so it holds whichever way the policy was written,
 * and it requires the Deny's Resource to name the resource the policy is attached to.
 */

type CfnTemplate = Record<string, unknown>;
type CfnResource = { Type?: unknown; Properties?: Record<string, unknown> };
type TlsRule = {
  policyType: string;
  attach: string;
  actions: readonly string[];
  /** The resource references the Deny must name, each alone or covered by a `*` Resource. */
  resources: (logicalId: string) => readonly unknown[];
};

const arnOf = (logicalId: string): unknown => ({ 'Fn::GetAtt': [logicalId, 'Arn'] });

/** The TLS-only Deny each type must carry, in the form CDK's `enforceSSL` renders it. */
const TLS_DENY: Record<string, TlsRule> = {
  'AWS::SQS::Queue': {
    policyType: 'AWS::SQS::QueuePolicy',
    attach: 'Queues',
    actions: ['sqs:*'],
    resources: (id) => [arnOf(id)],
  },
  // A bucket Deny must cover the bucket and its objects.
  'AWS::S3::Bucket': {
    policyType: 'AWS::S3::BucketPolicy',
    attach: 'Bucket',
    actions: ['s3:*'],
    resources: (id) => [arnOf(id), { 'Fn::Join': ['', [arnOf(id), '/*']] }],
  },
  // CDK's topic enforceSSL denies Publish only (aws-sns/lib/topic-base.js createSSLPolicyDocument);
  // a hand-written sns:* Deny is at least as strong, so either is accepted.
  'AWS::SNS::Topic': {
    policyType: 'AWS::SNS::TopicPolicy',
    attach: 'Topics',
    actions: ['sns:Publish', 'sns:*'],
    resources: (id) => [{ Ref: id }],
  },
};

const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);

function attachesTo(value: unknown, logicalId: string): boolean {
  return list(value).some(
    (entry) =>
      entry !== null &&
      typeof entry === 'object' &&
      (entry as Record<string, unknown>).Ref === logicalId,
  );
}

function isAnyPrincipal(principal: unknown): boolean {
  if (principal === '*') return true;
  if (principal === null || typeof principal !== 'object') return false;
  const record = principal as Record<string, unknown>;
  return Object.keys(record).length === 1 && record.AWS === '*';
}

function isTlsCondition(condition: unknown): boolean {
  return JSON.stringify(condition) === JSON.stringify({ Bool: { 'aws:SecureTransport': 'false' } });
}

function coversResource(resource: unknown, required: readonly unknown[]): boolean {
  const named = list(resource).map((entry) => JSON.stringify(entry));
  if (named.includes(JSON.stringify('*'))) return true;
  return required.every((reference) => named.includes(JSON.stringify(reference)));
}

function deniesPlaintext(
  statement: Record<string, unknown>,
  rule: TlsRule,
  logicalId: string,
): boolean {
  return (
    statement.Effect === 'Deny' &&
    list(statement.Action).some(
      (action) => typeof action === 'string' && rule.actions.includes(action),
    ) &&
    isAnyPrincipal(statement.Principal) &&
    isTlsCondition(statement.Condition) &&
    coversResource(statement.Resource, rule.resources(logicalId))
  );
}

/** Return `"<stack>/<logicalId>: no aws:SecureTransport deny"` for each uncovered resource. */
export function tlsDenyMissing(stackName: string, template: CfnTemplate): string[] {
  const resources = (template.Resources ?? {}) as Record<string, CfnResource>;
  const missing: string[] = [];
  for (const [logicalId, resource] of Object.entries(resources)) {
    const rule = typeof resource.Type === 'string' ? TLS_DENY[resource.Type] : undefined;
    if (rule === undefined) continue;
    const covered = Object.values(resources).some((policy) => {
      if (policy.Type !== rule.policyType) return false;
      if (!attachesTo(policy.Properties?.[rule.attach], logicalId)) return false;
      const document = policy.Properties?.PolicyDocument as { Statement?: unknown } | undefined;
      return list(document?.Statement ?? []).some(
        (statement) =>
          statement !== null &&
          typeof statement === 'object' &&
          deniesPlaintext(statement as Record<string, unknown>, rule, logicalId),
      );
    });
    if (!covered) missing.push(`${stackName}/${logicalId}: no aws:SecureTransport deny`);
  }
  return missing;
}
