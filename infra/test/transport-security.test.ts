// SPDX-License-Identifier: MIT-0
/**
 * Every queue, topic and bucket in every stack refuses non-TLS requests (M-SQS-TLS-001).
 *
 * CDK's `enforceSSL` is per construct and off by default, and queues have no mixin that sets it,
 * so two stacks shipped four queues without it. This gate reads every synthesised template
 * through `everyTemplate()`, so a stack added later is covered the moment it is built.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { everyTemplate } from './support/templates';
import { tlsDenyMissing } from './support/transport-security';

type Build = (stack: cdk.Stack, enforceSSL: boolean) => void;

const BUILDERS: Record<string, { build: Build; line: RegExp }> = {
  queue: {
    build: (stack, enforceSSL) => new sqs.Queue(stack, 'Q', { enforceSSL }),
    line: /^Probe\/Q[0-9A-F]{8}: no aws:SecureTransport deny$/,
  },
  topic: {
    build: (stack, enforceSSL) => new sns.Topic(stack, 'T', { enforceSSL }),
    line: /^Probe\/T[0-9A-F]{8}: no aws:SecureTransport deny$/,
  },
  bucket: {
    build: (stack, enforceSSL) => new s3.Bucket(stack, 'B', { enforceSSL }),
    line: /^Probe\/B[0-9A-F]{8}: no aws:SecureTransport deny$/,
  },
};

function probe(build: Build, enforceSSL: boolean): string[] {
  const stack = new cdk.Stack(new cdk.App(), 'Probe', {
    env: { account: '111122223333', region: 'us-east-1' },
  });
  build(stack, enforceSSL);
  return tlsDenyMissing('Probe', Template.fromStack(stack).toJSON());
}

describe('the TLS-only deny matcher', () => {
  it.each(Object.entries(BUILDERS))('refuses a %s built without enforceSSL', (_kind, builder) => {
    const missing = probe(builder.build, false);
    expect(missing).toHaveLength(1);
    expect(missing[0]).toMatch(builder.line);
  });

  it.each(Object.entries(BUILDERS))('accepts a %s built with enforceSSL', (_kind, builder) => {
    expect(probe(builder.build, true)).toEqual([]);
  });
});

/** A hand-written TLS-only Deny of `action`, scoped to `resource`. */
function tlsDeny(action: string, resource: unknown): Record<string, unknown> {
  return {
    Version: '2012-10-17',
    Statement: [
      {
        Effect: 'Deny',
        Principal: { AWS: '*' },
        Action: action,
        Resource: resource,
        Condition: { Bool: { 'aws:SecureTransport': 'false' } },
      },
    ],
  };
}

function handWrittenQueue(resource: (queue: sqs.CfnQueue) => unknown): string[] {
  const stack = new cdk.Stack(new cdk.App(), 'Probe', {
    env: { account: '111122223333', region: 'us-east-1' },
  });
  const queue = new sqs.CfnQueue(stack, 'Q');
  new sqs.CfnQueuePolicy(stack, 'QPolicy', {
    queues: [queue.ref],
    policyDocument: tlsDeny('sqs:*', resource(queue)),
  });
  return tlsDenyMissing('Probe', Template.fromStack(stack).toJSON());
}

function handWrittenBucket(resource: (bucket: s3.CfnBucket) => unknown): string[] {
  const stack = new cdk.Stack(new cdk.App(), 'Probe', {
    env: { account: '111122223333', region: 'us-east-1' },
  });
  const bucket = new s3.CfnBucket(stack, 'B');
  new s3.CfnBucketPolicy(stack, 'BPolicy', {
    bucket: bucket.ref,
    policyDocument: tlsDeny('s3:*', resource(bucket)),
  });
  return tlsDenyMissing('Probe', Template.fromStack(stack).toJSON());
}

describe('the TLS-only deny must cover the resource it is attached to', () => {
  it('refuses a queue Deny scoped to another queue', () => {
    expect(
      handWrittenQueue(() => 'arn:aws:sqs:us-east-1:111122223333:fictional-other-queue'),
    ).toEqual(['Probe/Q: no aws:SecureTransport deny']);
  });

  it('accepts a queue Deny that names the attached queue', () => {
    expect(handWrittenQueue((queue) => queue.attrArn)).toEqual([]);
  });

  it('accepts a queue Deny on every resource', () => {
    expect(handWrittenQueue(() => '*')).toEqual([]);
  });

  it('refuses a bucket Deny that leaves the objects out', () => {
    expect(handWrittenBucket((bucket) => bucket.attrArn)).toEqual([
      'Probe/B: no aws:SecureTransport deny',
    ]);
  });

  it('accepts a bucket Deny that covers the bucket and its objects', () => {
    expect(
      handWrittenBucket((bucket) => [bucket.attrArn, cdk.Fn.join('', [bucket.attrArn, '/*'])]),
    ).toEqual([]);
  });
});

describe('every synthesised stack', () => {
  it('denies plaintext access to every queue, topic and bucket', () => {
    const templates = everyTemplate();
    const missing = Object.entries(templates).flatMap(([name, template]) =>
      tlsDenyMissing(name, template as Record<string, unknown>),
    );
    expect(missing).toEqual([]);
  });
});
