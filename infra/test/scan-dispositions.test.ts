// SPDX-License-Identifier: MIT-0
/**
 * The template scanner dispositions (`lib/mixins/scan-dispositions.ts`) land where they claim.
 *
 * Over every stack the app builds (`support/templates.ts`):
 *
 * 1. Each `BY_PATH` key names exactly one resource, so a renamed construct cannot keep a stale
 *    entry (its finding would silently return as open), and no key covers two resources.
 * 2. Every resource of a `BY_TYPE` type carries each of that type's rules, and a `BY_PATH`
 *    resource carries its own rules too. Checkov reads `Metadata.checkov.skip`, cfn-nag reads
 *    `Metadata.cfn_nag.rules_to_suppress`.
 * 3. Every reason starts with a disposition ID, so a skipped record traces to its register entry.
 * 4. The Aspect writes only metadata. A positive control: a fixture stack's queue gains the skip
 *    and keeps exactly the properties it had without the Aspect.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { BY_PATH, BY_TYPE, ScanDispositionsAspect, dispositionKey, type ScanDisposition } from '../lib/mixins/scan-dispositions';
import { everyStack } from './support/templates';

type Json = Record<string, unknown>;

const stacks = everyStack();
const templates = Object.fromEntries(
  Object.entries(stacks).map(([name, stack]) => [name, Template.fromStack(stack).toJSON() as Json]),
);

/** Every resource in every stack, with its stack name, logical ID, disposition key and template entry. */
const resources = Object.entries(stacks).flatMap(([name, stack]) =>
  stack.node.findAll()
    .filter((c): c is cdk.CfnResource => c instanceof cdk.CfnResource)
    .map((c) => {
      const logicalId = stack.resolve(c.logicalId) as string;
      const entry = (templates[name]!.Resources as Record<string, Json>)[logicalId]!;
      return { name, logicalId, key: dispositionKey(c), type: c.cfnResourceType, entry };
    }));

function skipped(entry: Json, d: ScanDisposition): boolean {
  const metadata = (entry.Metadata ?? {}) as Json;
  if (d.scanner === 'checkov') {
    const skips = ((metadata.checkov as Json | undefined)?.skip ?? []) as { id: string; comment: string }[];
    return skips.some((s) => s.id === d.id && s.comment === d.reason);
  }
  const rules = ((metadata.cfn_nag as Json | undefined)?.rules_to_suppress ?? []) as { id: string; reason: string }[];
  return rules.some((r) => r.id === d.id && r.reason === d.reason);
}

describe('scan dispositions', () => {
  it.each(Object.keys(BY_PATH))('%s names exactly one resource', (key) => {
    expect(resources.filter((r) => r.key === key).map((r) => `${r.name}/${r.logicalId}`)).toHaveLength(1);
  });

  it('puts every BY_TYPE rule on every resource of its type', () => {
    const missing = resources.flatMap((r) => (BY_TYPE[r.type] ?? [])
      .filter((d) => !skipped(r.entry, d))
      .map((d) => `${r.name}/${r.logicalId}: ${d.scanner} ${d.id}`));
    expect(missing).toEqual([]);
    for (const type of Object.keys(BY_TYPE)) {
      expect(resources.some((r) => r.type === type)).toBe(true);
    }
  });

  it('puts every BY_PATH rule on its resource', () => {
    const missing = resources.flatMap((r) => (BY_PATH[r.key] ?? [])
      .filter((d) => !skipped(r.entry, d))
      .map((d) => `${r.name}/${r.logicalId}: ${d.scanner} ${d.id}`));
    expect(missing).toEqual([]);
  });

  it('starts every reason with a disposition ID', () => {
    const all = [...Object.values(BY_TYPE), ...Object.values(BY_PATH)].flat();
    expect(all.filter((d) => !/^[A-Z]-[A-Z0-9-]+-\d{3}\b/.test(d.reason))).toEqual([]);
  });

  it('exempts the ADR-149 staging bucket from versioning by path, not by type', () => {
    const checkovSkips = (entry: Json): { id: string; comment: string }[] =>
      (((entry.Metadata ?? {}) as Json).checkov as Json | undefined)?.skip as never ?? [];
    const staging = resources.filter((r) => r.key === 'DataV2Stack:SemanticV2StagingBucket/Resource');
    expect(staging).toHaveLength(1);
    const entry = staging[0]!.entry;
    expect((entry.Properties as Json).VersioningConfiguration).toBeUndefined();
    expect(checkovSkips(entry)).toContainEqual({
      id: 'CKV_AWS_21',
      comment: BY_PATH['DataV2Stack:SemanticV2Bucket/Resource']![0]!.reason,
    });
    // Control: the versioned raw bucket carries no CKV_AWS_21 skip, so the rule is not by type.
    const raw = resources.filter((r) => r.key === 'DataV2Stack:SemanticV2RawBucket/Resource');
    expect(raw).toHaveLength(1);
    expect((raw[0]!.entry.Properties as Json).VersioningConfiguration).toEqual({ Status: 'Enabled' });
    expect(checkovSkips(raw[0]!.entry).map((skip) => skip.id)).not.toContain('CKV_AWS_21');
  });
  it('writes metadata only, with a paired control', () => {
    const build = (withAspect: boolean): Json => {
      const app = new cdk.App();
      const stack = new cdk.Stack(app, 'Fixture');
      new sqs.Queue(stack, 'Queue', { enforceSSL: true });
      if (withAspect) cdk.Aspects.of(app).add(new ScanDispositionsAspect());
      const queues = Template.fromStack(stack).findResources('AWS::SQS::Queue');
      return Object.values(queues)[0] as Json;
    };
    const plain = build(false);
    const disposed = build(true);
    expect(plain.Metadata).toBeUndefined();
    expect(disposed.Metadata).toEqual({ checkov: { skip: [{ id: 'CKV_AWS_27', comment: BY_TYPE['AWS::SQS::Queue']![0]!.reason }] } });
    expect({ ...disposed, Metadata: undefined }).toEqual({ ...plain, Metadata: undefined });
  });
});
