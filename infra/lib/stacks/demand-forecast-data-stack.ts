// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import type { AppConfig } from '../config';
import { createDataBucket, demandForecastLifecycle } from '../mixins/storage';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface DemandForecastDataStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/**
 * The retained demand-forecast processing bucket (approval I-1, slice 1).
 *
 * Holds each run's staged weekly input under `runs/<run_id>/input/<attempt>/` and each shard
 * attempt's candidate under `runs/<run_id>/shards/<k>/attempts/<attempt>/candidate/`. Private,
 * TLS-only, SSE-S3 and versioned, with every `runs/` object expiring (design §6a). Slice 2 adds
 * the forecast table and the SSM parameters it lists; until then `DemandForecastStack` derives
 * the bucket's deterministic name with `dataBucketName()`.
 */
export class DemandForecastDataStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DemandForecastDataStackProps) {
    super(scope, id, props);
    const { config } = props;
    createDataBucket(this, 'ProcessingBucket', {
      config,
      purpose: 'demand-forecast',
      versioned: true,
      lifecycleRules: demandForecastLifecycle(config),
    });
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }
}
