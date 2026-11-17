// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath } from '../config';
import { FORECAST_SCHEDULE } from '../stacks/demand-forecast-stack';

/**
 * The demand forecast read API's configuration and grant, one home for both readers (ADR-163):
 * the HTTP function in `ApiStack` and the forecast tool function in `AgentStack`.
 *
 * The table name is read from the parameter `DemandForecastDataStack` publishes; the schedule is
 * the daily check's own constant, so the status route's next-check times follow the schedule the
 * Scheduler runs.
 */
export function demandForecastReaderEnvironment(
  scope: Construct,
  config: AppConfig,
): Record<string, string> {
  return {
    ADL_DEMAND_FORECAST_TABLE: forecastTableName(scope, config),
    ADL_DEMAND_FORECAST_MARKETPLACE_KEY: config.demandForecast.marketplaceKey,
    ADL_DEMAND_FORECAST_CHECK_SCHEDULE: FORECAST_SCHEDULE,
  };
}

/**
 * Allow `dynamodb:GetItem` on the `SET#` and `GEN#` keys of the forecast table only: no write, no
 * `SLOT#` marker, no query or scan, no S3. The table uses AWS-owned encryption, so no KMS grant.
 */
export function grantDemandForecastRead(
  fn: lambda.Function,
  scope: Construct,
  config: AppConfig,
): void {
  fn.addToRolePolicy(
    new iam.PolicyStatement({
      sid: 'ReadDemandForecast',
      actions: ['dynamodb:GetItem'],
      resources: [
        cdk.Stack.of(scope).formatArn({
          service: 'dynamodb',
          resource: 'table',
          resourceName: forecastTableName(scope, config),
        }),
      ],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*', 'GEN#*'] } },
    }),
  );
}

function forecastTableName(scope: Construct, config: AppConfig): string {
  return ssm.StringParameter.valueForStringParameter(
    scope,
    paramPath(config.envName, 'demandForecastTableName'),
  );
}
