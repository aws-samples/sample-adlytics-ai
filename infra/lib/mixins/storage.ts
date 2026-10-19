// SPDX-License-Identifier: MIT-0
/**
 * Data-lake bucket construction, in one place.
 *
 * Encryption, TLS enforcement, public-access blocking and lifecycle are
 * cross-cutting concerns and are set here rather than at each call site, per the CDK
 * steering. A factory rather than an Aspect because encryption and lifecycle are
 * construct properties that legitimately differ by bucket purpose — an Aspect would
 * have to either apply one policy to all three or reach inside them, and neither
 * is honest.
 *
 * L2 `s3.Bucket` deliberately. ADR-016 scopes hand-written L1 to Glue, Athena, Lake
 * Formation and S3 Tables, which have no stable L2. S3 does, and hand-setting
 * encryption, versioning, public-access blocking and TLS on every `CfnBucket` would
 * be four chances per bucket to omit one.
 */

import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import type { AppConfig } from '../config';

/** What a bucket holds. Becomes a segment of its name, so these are an interface. */
export type DataBucketPurpose =
  | 'preset-artifacts'
  | 'v2-results'
  | 'semantic-v2'
  | 'semantic-v2-raw';

/**
 * S3's own limit. Asserted rather than trusted because the longest name this builds
 * is `adlytics-` + 24 + `-athena-results-` + 12 = 61, which clears it by two
 * characters — close enough that a future purpose name would break a deploy rather
 * than a build.
 */
const MAX_BUCKET_NAME_LENGTH = 63;

/**
 * Explicit physical names, not CDK-generated ones.
 *
 * A generated name changes if the logical ID does, and on a RETAIN bucket that
 * silently orphans the old bucket and starts writing to a new empty one. An explicit
 * name fails loudly on collision instead. The account ID supplies global uniqueness;
 * the region is deliberately absent, since one environment name maps to one region
 * and including it would exceed the length limit at the maximum name length.
 */
export function dataBucketName(config: AppConfig, purpose: DataBucketPurpose): string {
  const name = `adlytics-${config.envName}-${purpose}-${config.account}`;
  if (name.length > MAX_BUCKET_NAME_LENGTH) {
    throw new Error(
      `Bucket name ${JSON.stringify(name)} is ${name.length} characters, over S3's ` +
        `limit of ${MAX_BUCKET_NAME_LENGTH}. Shorten the environment name.`,
    );
  }
  return name;
}

/**
 * The encryption every data bucket gets.
 *
 * `kms_cmk` refuses at synth on purpose. The buckets would encrypt correctly while
 * no consumer holds `kms:Decrypt`, and Glue, Athena and Lake Formation each need
 * their own grant — so the first symptom would be an opaque `AccessDenied` on a
 * query rather than a build failure naming the gap. ADR-018 records customer-managed
 * keys as a deliberate later step; this is where that step lands.
 */
function bucketEncryption(config: AppConfig): s3.BucketEncryption {
  if (config.data.encryption === 'kms_cmk') {
    throw new Error(
      'config.data.encryption is "kms_cmk", which is not implemented yet.\n' +
        'ADR-018 defers customer-managed keys: the buckets would encrypt, but no\n' +
        'consumer holds kms:Decrypt and Glue, Athena and Lake Formation each need a\n' +
        'grant, so the first symptom would be AccessDenied on a query rather than\n' +
        'this message. Set "s3_managed", or implement the V2 grants first.',
    );
  }
  return s3.BucketEncryption.S3_MANAGED;
}

export interface DataBucketOptions {
  readonly config: AppConfig;
  readonly purpose: DataBucketPurpose;
  /** Versioning protects against an upstream overwrite. Wrong for Iceberg data. */
  readonly versioned: boolean;
  readonly lifecycleRules: readonly s3.LifecycleRule[];
  /**
   * Publish this bucket's object events to EventBridge.
   *
   * A boolean with no knowledge of any consumer, and that is the whole reason it is here rather
   * than a notification target. The raw bucket is first in `DEPLOY_ORDER` and RETAIN, so a direct
   * S3-to-SQS notification would need a queue ARN from a stack that deploys after it — and SSM
   * cannot run backwards, so that is the cycle ADR-012 says means the boundary is wrong.
   * EventBridge inverts the dependency: this bucket announces, and whoever listens owns the rule.
   *
   * Off by default because only one bucket needs it. Curated and results objects are written by
   * Athena during a load, so publishing them would emit events the platform has no use for and,
   * for curated, would fire on every compaction.
   */
  readonly eventBridgeEnabled?: boolean;
}

/**
 * Turn on EventBridge delivery by writing the L1 property, not the L2 one.
 *
 * **The L2's `eventBridgeEnabled` renders a custom resource, not a bucket property.** It injects a
 * Lambda function, an IAM role and an `AWSLambdaBasicExecutionRole` managed policy, then calls
 * `s3:PutBucketNotification` at deploy time — verified by diffing it against deployed `dev`. That
 * is CDK working as designed: notifications added incrementally, or from another stack, cannot be a
 * template property.
 *
 * Here they can, and three of this repository's own rules say they should be. `createDataBucket`
 * already refuses `autoDeleteObjects` because it injects a Lambda into a RETAIN data bucket, and
 * this is the same machinery for a smaller reason. The Lambda rules forbid a managed policy where
 * an inline statement would do. And a CDK-managed function carries a runtime CDK chooses, which
 * drifts on a minor bump and is the one thing the runtime pin exists to prevent.
 *
 * `AWS::S3::Bucket` has `NotificationConfiguration.EventBridgeConfiguration` natively, so the
 * escape hatch renders one property and no compute. Safe against the L2 only because nothing calls
 * `addEventNotification` on these buckets: if anything ever does, the L2 would write the same
 * property through its custom resource and the two would fight.
 */
function publishToEventBridge(bucket: s3.Bucket): void {
  const rendered = bucket.node.defaultChild as s3.CfnBucket;
  rendered.notificationConfiguration = { eventBridgeConfiguration: { eventBridgeEnabled: true } };
}

/**
 * A private, encrypted, TLS-only bucket with an explicit name and lifecycle.
 *
 * No `autoDeleteObjects`: that injects a Lambda which empties the bucket on stack
 * deletion, which is precisely the behaviour a RETAIN data bucket exists to prevent.
 * `FrontendStack` uses it legitimately because a site bundle is a build artefact.
 */
export function createDataBucket(
  scope: Construct,
  id: string,
  options: DataBucketOptions,
): s3.Bucket {
  const { config, purpose, versioned, lifecycleRules, eventBridgeEnabled } = options;

  const bucket = new s3.Bucket(scope, id, {
    bucketName: dataBucketName(config, purpose),
    encryption: bucketEncryption(config),
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    // Denies any request arriving over plain HTTP. Amazon's own SP-API guidance is
    // that report content must never be stored unencrypted, even temporarily.
    enforceSSL: true,
    minimumTLSVersion: 1.2,
    versioned,
    // Stated here as well as by the stack-level aspect. The aspect is what guarantees
    // it across every resource; this is what makes it visible at the one construct
    // where getting it wrong destroys customer data.
    removalPolicy: RemovalPolicy.RETAIN,
    lifecycleRules: [...lifecycleRules],
  });

  if (eventBridgeEnabled) {
    publishToEventBridge(bucket);
  }
  return bucket;
}

/**
 * Lifecycle for the permanent Semantic V2 raw zone.
 *
 * Transition rather than expire, because raw is the only artefact that proves what
 * upstream actually delivered and the manifest is authoritative about what arrived —
 * deleting the object it describes undercuts that. Glacier Instant Retrieval keeps
 * millisecond access, so a reprocess does not need a restore job first.
 */
export function rawZoneLifecycle(config: AppConfig): s3.LifecycleRule[] {
  const { rawExpireAfterDays } = config.data;

  return [
    {
      id: 'transition-cold-source-objects',
      enabled: true,
      transitions: [
        {
          storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
          transitionAfter: Duration.days(config.data.rawTransitionAfterDays),
        },
      ],
      // Conditional spread rather than `expiration: undefined`, which
      // exactOptionalPropertyTypes rejects — and which would also read as "no
      // opinion" where 'never' is a decision.
      ...(rawExpireAfterDays === 'never'
        ? {}
        : { expiration: Duration.days(rawExpireAfterDays) }),
      noncurrentVersionExpiration: Duration.days(
        config.data.rawExpireNoncurrentVersionsAfterDays,
      ),
    },
    abortIncompleteUploads(config),
  ];
}

/** Lifecycle for Athena results, which are disposable by definition. */
export function athenaResultsLifecycle(config: AppConfig): s3.LifecycleRule[] {
  return [
    {
      id: 'expire-query-results',
      enabled: true,
      expiration: Duration.days(config.data.athenaResultsExpireAfterDays),
    },
    abortIncompleteUploads(config),
  ];
}

/** Lifecycle for retained immutable preset-artifact bodies. */
export function publisherArtifactLifecycle(config: AppConfig): s3.LifecycleRule[] {
  return [
    {
      id: 'expire-noncurrent-artifact-versions',
      enabled: true,
      noncurrentVersionExpiration: Duration.days(
        config.data.rawExpireNoncurrentVersionsAfterDays,
      ),
    },
    abortIncompleteUploads(config),
  ];
}

/**
 * Applied to every data bucket.
 *
 * An interrupted upload leaves parts that are billed as storage and appear in no
 * object listing, so on a bucket receiving automated writes the cost accrues with
 * nothing to see.
 */
function abortIncompleteUploads(config: AppConfig): s3.LifecycleRule {
  return {
    id: 'abort-incomplete-multipart-uploads',
    enabled: true,
    abortIncompleteMultipartUploadAfter: Duration.days(
      config.data.abortIncompleteUploadsAfterDays,
    ),
  };
}
