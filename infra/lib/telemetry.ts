// SPDX-License-Identifier: MIT-0
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';

import type { AppConfig } from './config';

/**
 * The environment every Python Lambda needs for the in-process ADOT bootstrap and Powertools.
 *
 * `ADL_TRACING_ENABLED` is taken from the same flag that provisions Transaction Search. One switch
 * rather than two, because reaching the X-Ray OTLP endpoint *requires* Transaction Search: exporting
 * without it would produce spans that are billed and discarded, and two flags could disagree.
 *
 * `ADL_TRACE_SAMPLE_RATIO` is the recording rate, distinct from the indexing rate. Transaction Search
 * exists to ingest 100% of spans so a trace is never broken, and a broken trace is worse than a
 * short one during an investigation: the missing hop reads as a fast hop. Cost is controlled by
 * indexing less, not by recording less.
 *
 * `ADL_AWS_REGION` is what the SDK reads to address the OTLP endpoint; without it the bootstrap
 * treats tracing as disabled regardless of the flag, so the two travel together.
 */
export function tracingEnvironment(config: AppConfig): Record<string, string> {
  return {
    ADL_ENVIRONMENT: config.envName,
    ADL_AWS_REGION: config.region,
    ADL_TRACING_ENABLED: String(config.transactionSearchEnabled),
    ADL_TRACE_SAMPLE_RATIO: String(config.traceSampleRatio),
    POWERTOOLS_METRICS_NAMESPACE: 'Adlytics',
    POWERTOOLS_LOG_LEVEL: 'INFO',
  };
}

/**
 * Let a function export spans to the X-Ray OTLP endpoint.
 *
 * Written inline rather than by attaching `AWSXrayWriteOnlyAccess`, which is what AWS's own
 * walkthrough suggests. That managed policy also carries `xray:GetSamplingRules`,
 * `GetSamplingTargets` and `GetSamplingStatisticSummaries` — X-Ray's *centralised* sampling. This
 * helper applies only to Lambda functions, whose sampler is local and configured by
 * `traceSampleRatio`; it does not describe the managed AgentCore Runtime. Granting the reads to these
 * functions would imply a sampling model that is not in play.
 *
 * Unconditional, even when tracing is off. The grant permits an export that a disabled SDK never
 * attempts, and IAM that appears and disappears with a feature flag produces a diff on every toggle
 * and an outage whenever the two are deployed out of step.
 *
 * No resource scoping, because these actions support none — X-Ray's own authorization reference
 * lists `*` for both. Stated here so the wildcard reads as a service constraint rather than an
 * oversight.
 */
export function grantSpanExport(fn: lambda.Function): void {
  fn.addToRolePolicy(
    new iam.PolicyStatement({
      actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
      resources: ['*'],
    }),
  );
}
