// SPDX-License-Identifier: MIT-0
/**
 * Every role stays under IAM's policy-size limits with headroom, and no CDK overflow policy hides
 * how close one is (retrospective P3; review 041fe47b R041-2).
 *
 * The Ingestion role's inline policies passed IAM's 10,240-character aggregate and the deploy
 * rolled back; the limit was found only at deploy time, and CDK splits only a role's default
 * policy, at its own estimate, so neither CDK nor the stack tests saw the aggregate. This test
 * renders every policy document of `everyTemplate()` (credential-free, every optional stack
 * provisioned) the way IAM counts it (`support/iam-policy-size.ts`, which cites the IAM quotas
 * page) and fails a role whose inline aggregate, or a managed policy whose document, is within
 * `HEADROOM_FRACTION` of its limit, a role over the managed-policy count, and any CDK overflow
 * policy. `scripts/check-iam-policy-size.ts` applies the same check to a `cdk synth` output.
 *
 * Positive controls plant an oversized role, an oversized managed policy (on a role, on a group
 * only, and unattached: each managed document is measured on its own), an inline policy on no
 * role and a CDK spill, and each fails; the same shapes cut below the threshold pass.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as iam from 'aws-cdk-lib/aws-iam';
import {
  DeploymentContext,
  MANAGED_POLICY_THRESHOLD,
  ROLE_INLINE_LIMIT,
  ROLE_INLINE_THRESHOLD,
  documentSize,
  managedPolicySizes,
  policySizeFindings,
  roleBudgets,
  unexpectedOverflowPolicies,
} from './support/iam-policy-size';
import { TEST_CONFIG } from './support/config';
import { everyTemplate } from './support/templates';

const CONTEXT: DeploymentContext = {
  account: TEST_CONFIG.account,
  partition: 'aws',
  // As templates.ts deploys them: EdgeSecurity in us-east-1, every other stack in the config's.
  regionOf: (stack) => (stack === 'EdgeSecurityStack' ? 'us-east-1' : TEST_CONFIG.region),
  stackNameOf: (stack) => `Adlytics-${stack.replace(/Stack$/, '')}-${TEST_CONFIG.envName}`,
};

type Templates = Parameters<typeof policySizeFindings>[0];

describe('IAM policy size (P3, R041-2)', () => {
  const templates = everyTemplate() as Templates;

  it('keeps every role and managed policy under its limit with headroom, and no overflow', () => {
    const budgets = roleBudgets(templates, CONTEXT);
    // Reported, not asserted: the reader sees how close the largest roles are.
    console.log(
      [
        `IAM policy size: ${budgets.length} roles; inline threshold ${ROLE_INLINE_THRESHOLD} of ${ROLE_INLINE_LIMIT} characters`,
        ...budgets
          .slice(0, 5)
          .map((budget) => `  ${budget.role}: inline ${budget.inlineTotal}, managed [${budget.managed.map((item) => item.size).join(', ')}]`),
      ].join('\n'),
    );
    expect(budgets.length).toBeGreaterThan(50);
    expect(policySizeFindings(templates, CONTEXT)).toStrictEqual([]);
  });

  it('holds the Ingestion role well under the limit, with its storage grants in named policies', () => {
    const ingestion = roleBudgets(templates, CONTEXT).find((budget) =>
      /^IngestionStack\/IngestionExecutionRole[0-9A-F]{8}$/.test(budget.role),
    );
    expect(ingestion).toBeDefined();
    expect(ingestion?.inlineTotal).toBeLessThanOrEqual(ROLE_INLINE_THRESHOLD);
    // GraphRunNodesPolicy is the provisioned graph's `QueryGraphRunNodes` (S6 part 2, row 3).
    expect(ingestion?.managed.map((item) => item.policy.replace(/[0-9A-F]{8}$/, '')).sort()).toStrictEqual([
      // ADR-149: the inline composition's staging grants (the test app composes inline).
      'IngestionStack/DeliveryStagingPolicy',
      'IngestionStack/GraphRunNodesPolicy',
      'IngestionStack/IdentityEvidencePolicy',
      'IngestionStack/SemanticPublicationKeysPolicy',
      'IngestionStack/SemanticV2StoragePolicy',
    ]);
  });

  describe('positive controls', () => {
    const statement = (index: number): iam.PolicyStatement =>
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`arn:aws:s3:::adlytics-dev-planted-bucket-${TEST_CONFIG.account}/prefix-${index}/*`],
      });
    /** `count` statements for a managed document: 45 pass its threshold, 10 stay well under. */
    const managedGrants = (count: number): iam.PolicyStatement[] =>
      Array.from({ length: count }, (_, i) => statement(1_000 + i));

    /** One role carrying `count` inline statements of about 150 characters across two policies. */
    function plantedRole(count: number, managedStatements = 0): Templates {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Planted', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      const role = new iam.Role(stack, 'PlantedRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      const half = Math.ceil(count / 2);
      for (const [id, range] of [['First', [0, half]], ['Second', [half, count]]] as const) {
        const statements = Array.from({ length: range[1] - range[0] }, (_, i) => statement(range[0] + i));
        if (statements.length > 0) new iam.Policy(stack, id, { roles: [role], statements });
      }
      if (managedStatements > 0) {
        new iam.ManagedPolicy(stack, 'PlantedManaged', {
          roles: [role],
          statements: managedGrants(managedStatements),
        });
      }
      return { Planted: Template.fromStack(stack).toJSON() } as Templates;
    }

    it('fails an oversized inline aggregate and passes the same role cut below the threshold', () => {
      const oversized = policySizeFindings(plantedRole(80), CONTEXT);
      expect(oversized).toHaveLength(1);
      expect(oversized[0]).toMatch(
        new RegExp(`^Planted/PlantedRole[0-9A-F]{8}: inline policies total \\d+ characters, over the ${ROLE_INLINE_THRESHOLD}-character threshold`),
      );
      expect(policySizeFindings(plantedRole(40), CONTEXT)).toStrictEqual([]);
    });

    it('fails an oversized managed policy and passes a small one', () => {
      const findings = policySizeFindings(plantedRole(2, 45), CONTEXT);
      expect(findings).toHaveLength(1);
      expect(findings[0]).toMatch(
        new RegExp(`^Planted/PlantedManaged[0-9A-F]{8}: managed policy is \\d+ characters, over the ${MANAGED_POLICY_THRESHOLD}-character threshold`),
      );
      expect(policySizeFindings(plantedRole(2, 10), CONTEXT)).toStrictEqual([]);
    });

    /** One managed policy of `count` statements that names no role: unattached, or on a group. */
    function plantedUnattachedManaged(count: number, attach: 'none' | 'group'): Templates {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Loose', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      const policy = new iam.ManagedPolicy(stack, 'LooseManaged', { statements: managedGrants(count) });
      if (attach === 'group') new iam.Group(stack, 'LooseGroup').addManagedPolicy(policy);
      return { Loose: Template.fromStack(stack).toJSON() } as Templates;
    }

    it.each(['none', 'group'] as const)(
      'fails an oversized managed policy that names no role (%s) and passes a small one',
      (attach) => {
        const oversized = plantedUnattachedManaged(45, attach);
        const resources = Object.values(oversized.Loose?.Resources ?? {});
        expect(resources.filter((resource) => resource.Type === 'AWS::IAM::ManagedPolicy')).toHaveLength(1);
        expect(resources.some((resource) => resource.Properties?.Roles !== undefined)).toBe(false);
        const sizes = managedPolicySizes(oversized, CONTEXT);
        expect(sizes).toHaveLength(1);
        expect(sizes[0]?.roles).toStrictEqual([]);

        const findings = policySizeFindings(oversized, CONTEXT);
        expect(findings).toHaveLength(1);
        expect(findings[0]).toMatch(
          new RegExp(`^Loose/LooseManaged[0-9A-F]{8}: managed policy is \\d+ characters, over the ${MANAGED_POLICY_THRESHOLD}-character threshold`),
        );
        expect(policySizeFindings(plantedUnattachedManaged(10, attach), CONTEXT)).toStrictEqual([]);
      },
    );

    it('reports a managed policy attached to two roles once', () => {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Shared', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      const roles = ['One', 'Two'].map(
        (id) => new iam.Role(stack, id, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') }),
      );
      new iam.ManagedPolicy(stack, 'SharedManaged', { roles, statements: managedGrants(45) });
      const templates = { Shared: Template.fromStack(stack).toJSON() } as Templates;

      expect(managedPolicySizes(templates, CONTEXT)[0]?.roles).toHaveLength(2);
      expect(policySizeFindings(templates, CONTEXT)).toHaveLength(1);
    });

    it('refuses an inline policy that names no role, rather than pass it unmeasured', () => {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Grouped', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      new iam.Policy(stack, 'GroupInline', {
        groups: [new iam.Group(stack, 'InlineGroup')],
        statements: [new iam.PolicyStatement({ actions: ['s3:GetObject'], resources: ['*'] })],
      });
      const templates = { Grouped: Template.fromStack(stack).toJSON() } as Templates;

      const findings = policySizeFindings(templates, CONTEXT);
      expect(findings).toHaveLength(1);
      expect(findings[0]).toMatch(
        /^Grouped\/GroupInline[0-9A-F]{8}: inline policy names no role, so no measured aggregate counts it$/,
      );
    });

    it('fails a CDK overflow policy', () => {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Spilled', {
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      });
      const role = new iam.Role(stack, 'SpilledRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      for (let index = 0; index < 120; index += 1) {
        role.addToPrincipalPolicy(
          new iam.PolicyStatement({
            actions: [`s3:GetObject`],
            resources: [`arn:aws:s3:::adlytics-dev-spilled-bucket-${index}/a-long-enough-prefix-${index}/*`],
            conditions: { StringEquals: { 'aws:ResourceAccount': TEST_CONFIG.account } },
          }),
        );
      }
      const templates = { Spilled: Template.fromStack(stack).toJSON() } as Templates;

      const overflow = unexpectedOverflowPolicies(templates);
      expect(overflow.length).toBeGreaterThan(0);
      expect(overflow.filter((id) => !/^Spilled\/SpilledRoleOverflowPolicy\d+[0-9A-F]{8}$/.test(id))).toStrictEqual([]);
      const findings = policySizeFindings(templates, CONTEXT);
      for (const id of overflow) {
        expect(findings).toContain(`${id}: CDK overflow policy; move the role's grants into a named policy`);
      }
    });

    it('refuses to render an intrinsic it has no shape for, rather than under-count it', () => {
      const templates = {
        Odd: {
          Resources: {
            Role: { Type: 'AWS::IAM::Role', Properties: { RoleName: 'adlytics-dev-odd' } },
            Thing: { Type: 'AWS::Fictional::Thing', Properties: {} },
            Policy: {
              Type: 'AWS::IAM::Policy',
              Properties: {
                Roles: [{ Ref: 'Role' }],
                PolicyDocument: { Statement: [{ Resource: { 'Fn::GetAtt': ['Thing', 'Arn'] } }] },
              },
            },
          },
        },
      } as Templates;

      expect(() => policySizeFindings(templates, CONTEXT)).toThrow(
        'Odd: cannot render Fn::GetAtt AWS::Fictional::Thing.Arn',
      );
    });

    it('counts characters without white space, as IAM does', () => {
      expect(documentSize({ Sid: 'a b', Action: ['x', 'y'] })).toBe('{"Sid":"ab","Action":["x","y"]}'.length);
    });
  });
});
