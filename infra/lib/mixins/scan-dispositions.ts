// SPDX-License-Identifier: MIT-0
/**
 * Template scanner dispositions, carried in the templates themselves.
 *
 * The IaC scanners (Checkov and cfn-nag, run by ASH over the templates
 * `scripts/export-scan-templates.ts` writes) flag rules this sample does not follow, by design
 * or because the rule cannot apply. Each such rule has a reviewed disposition in
 * `docs/security-dispositions.md`, and this Aspect writes it onto the resources it covers:
 * Checkov reads `Metadata.checkov.skip[{id, comment}]` and cfn-nag reads
 * `Metadata.cfn_nag.rules_to_suppress[{id, reason}]`. Each comment starts with the disposition ID,
 * so a skipped record traces to its reason.
 *
 * There are two tables:
 *
 * - `BY_TYPE` holds a rule whose reason holds for every resource of a type. The rule flags
 *   something every such resource has, whichever resource it is: service-key encryption the
 *   rule does not credit, Lambda outside a VPC, no function DLQ, no reserved concurrency, no
 *   bucket access logging, or a Log4J rule where nothing runs Java.
 * - `BY_PATH` holds a rule whose reason is about one resource, keyed by the stack class and the
 *   resource's construct path inside the stack. A new resource of the same type is therefore still
 *   scanned. `test/scan-dispositions.test.ts` fails when a key matches no resource, so a renamed
 *   construct cannot keep a stale entry.
 *
 * Metadata only: no property changes, so applying the Aspect changes no deployed behaviour.
 * Applied once to the app (`bin/app.ts`) and to every cross-stack test app
 * (`test/support/templates.ts`).
 */
import { CfnResource, IAspect, Stack } from 'aws-cdk-lib';
import { IConstruct } from 'constructs';

/** One suppressed rule: the scanner, its rule ID, and the reason, which starts with the ID. */
export interface ScanDisposition {
  readonly scanner: 'checkov' | 'cfn_nag';
  readonly id: string;
  readonly reason: string;
}

const SERVICE_ENCRYPTION = 'A-IAC-SERVICE-ENCRYPTION-001: encrypted at rest with an AWS service key (ADR-018); the rule credits only a customer-managed key';
const DESIGN = 'A-IAC-DESIGN-BASELINE-001 (NOT AFFECTED):';

function checkov(id: string, reason: string): ScanDisposition {
  return { scanner: 'checkov', id, reason };
}

function cfnNag(id: string, reason: string): ScanDisposition {
  return { scanner: 'cfn_nag', id, reason };
}

/** Rules whose reason holds for every resource of the type. */
export const BY_TYPE: Readonly<Record<string, readonly ScanDisposition[]>> = {
  'AWS::SQS::Queue': [checkov('CKV_AWS_27', SERVICE_ENCRYPTION)],
  'AWS::Logs::LogGroup': [checkov('CKV_AWS_158', SERVICE_ENCRYPTION)],
  'AWS::DynamoDB::Table': [checkov('CKV_AWS_119', SERVICE_ENCRYPTION)],
  'AWS::SecretsManager::Secret': [checkov('CKV_AWS_149', SERVICE_ENCRYPTION)],
  'AWS::Lambda::Function': [
    checkov('CKV_AWS_173', SERVICE_ENCRYPTION),
    checkov('CKV_AWS_117', `${DESIGN} no function reaches a private network resource, so a VPC has nothing to protect`),
    checkov('CKV_AWS_116', `${DESIGN} queue and stream consumers fail to source DLQs or failure destinations (AsyncFailureBoundsAspect), synchronous callers receive the error, and scheduled functions alarm on Errors and re-run from durable state on the next tick`),
    checkov('CKV_AWS_115', `${DESIGN} reserved concurrency is a deployer sizing choice; WAF rate limits bound request volume`),
  ],
  'AWS::S3::Bucket': [
    checkov('CKV_AWS_18', `${DESIGN} server access logging would add another data-bearing bucket; CloudTrail data events are the deployer's control (A-S3-LOGGING-001)`),
  ],
  'AWS::WAFv2::WebACL': [
    checkov('CKV_AWS_192', 'A-WAF-LOG4J-001: nothing behind the web ACL runs Java (Python functions, JavaScript CloudFront functions)'),
  ],
};

const UNVERSIONED = `${DESIGN} the bucket holds no first copy of its data`;
const TOKEN_API = `${DESIGN} the token-service API: OIDC discovery and JWKS are public by protocol, /token authenticates the client, and a WAF is attached`;

/** Rules whose reason is about one resource: `<stack class>:<construct path in the stack>`. */
export const BY_PATH: Readonly<Record<string, readonly ScanDisposition[]>> = {
  'ApiStack:ExecutionCursorMacKey/Resource': [
    checkov('CKV_AWS_7', 'A-KMS-HMAC-ROTATION-001: an HMAC_256 key; KMS supports no automatic or on-demand rotation for HMAC keys'),
    cfnNag('F19', 'A-KMS-HMAC-ROTATION-001: an HMAC_256 key; KMS supports no automatic or on-demand rotation for HMAC keys'),
  ],
  'TokenServiceStack:SigningKey/Resource': [
    cfnNag('F19', 'A-KMS-SIGNING-KEY-001: an asymmetric RSA_2048 signing key; KMS cannot rotate asymmetric keys automatically'),
  ],
  'TokenServiceStack:IssuerApi/Default/.well-known/openid-configuration/GET/Resource': [checkov('CKV_AWS_59', TOKEN_API)],
  'TokenServiceStack:IssuerApi/Default/jwks.json/GET/Resource': [checkov('CKV_AWS_59', TOKEN_API)],
  'TokenServiceStack:IssuerApi/Default/token/POST/Resource': [checkov('CKV_AWS_59', TOKEN_API)],
  'TokenServiceStack:IssuerApi/DeploymentStage.v1/Resource': [
    checkov('CKV_AWS_73', `${DESIGN} X-Ray tracing on the stage is an operational setting`),
    checkov('CKV_AWS_76', `${DESIGN} stage access logging is an operational setting`),
    checkov('CKV_AWS_120', `${DESIGN} stage caching is an operational setting`),
  ],
  'FrontendStack:SiteBucket/Resource': [checkov('CKV_AWS_21', UNVERSIONED)],
  'FrontendStack:Distribution/Resource': [
    checkov('CKV_AWS_174', `${DESIGN} the default *.cloudfront.net certificate fixes the minimum protocol policy at TLSv1`),
    checkov('CKV_AWS_86', `${DESIGN} distribution access logging would add another data-bearing bucket; CloudTrail is the deployer's control`),
  ],
  'DataV2Stack:SemanticV2Bucket/Resource': [checkov('CKV_AWS_21', UNVERSIONED)],
  'DataV2Stack:SemanticV2AthenaResultsBucket/Resource': [checkov('CKV_AWS_21', UNVERSIONED)],
  // ADR-149 (ING-INFRA, 2026-10-21): staged units are re-rendered from the immutable objects.
  'DataV2Stack:SemanticV2StagingBucket/Resource': [checkov('CKV_AWS_21', UNVERSIONED)],
  'AgentStack:PageContextTable/Resource': [
    checkov('CKV_AWS_28', `${DESIGN} a TTL cache of page context; nothing in it is a first copy`),
  ],
  'HarnessStack:ExecutionRole/DefaultPolicy/Resource': [
    checkov('CKV_AWS_107', `${DESIGN} ecr-public:GetAuthorizationToken, which the managed AgentCore runtime requires`),
  ],
  'IngestionStack:AlarmTopic/Resource': [
    checkov('CKV_AWS_26', `${DESIGN} the topic carries alarm notifications only and requires TLS; a customer-managed key would contradict ADR-018`),
  ],
};

/** `<stack class>:<construct path in the stack>`, the key `BY_PATH` uses. */
export function dispositionKey(resource: CfnResource): string {
  const stack = Stack.of(resource);
  return `${stack.constructor.name}:${resource.node.path.slice(stack.node.path.length + 1)}`;
}

export class ScanDispositionsAspect implements IAspect {
  visit(node: IConstruct): void {
    if (!(node instanceof CfnResource)) return;
    const dispositions = [...(BY_TYPE[node.cfnResourceType] ?? []), ...(BY_PATH[dispositionKey(node)] ?? [])];
    const skips = dispositions.filter((d) => d.scanner === 'checkov').map((d) => ({ id: d.id, comment: d.reason }));
    const suppressions = dispositions.filter((d) => d.scanner === 'cfn_nag').map((d) => ({ id: d.id, reason: d.reason }));
    if (skips.length > 0) node.addMetadata('checkov', { skip: skips });
    if (suppressions.length > 0) node.addMetadata('cfn_nag', { rules_to_suppress: suppressions });
  }
}
