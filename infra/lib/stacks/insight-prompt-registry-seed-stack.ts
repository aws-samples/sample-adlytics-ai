// SPDX-License-Identifier: MIT-0
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath } from '../config';
import { grantOwnLogWrites, lambdaRole } from '../mixins/logging';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import { grantSpanExport, tracingEnvironment } from '../telemetry';

export interface InsightPromptRegistrySeedStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly sourceAssetPath: string;
  /** Directory holding the exact insight contract set the packaged seed handler will load. */
  readonly insightContractsPath: string;
}

/**
 * Stable content digest for a whole contract directory.
 *
 * The directory analogue of `publisherDeploymentGeneration`: content-derived, never
 * clock-derived. Relative paths are sorted and folded into the hash alongside each file's
 * bytes, so adding, editing, renaming, or removing any contract changes the digest while an
 * unchanged directory always reproduces it. Used as a custom-resource property so every
 * insight-contract change re-fires the idempotent seed handler exactly once per deploy.
 */
export function insightContractSetDigest(contractsDir: string): string {
  const relativeFiles: string[] = [];
  const walk = (relative: string): void => {
    for (const entry of readdirSync(path.join(contractsDir, relative), { withFileTypes: true })) {
      const child = relative === '' ? entry.name : `${relative}/${entry.name}`;
      if (entry.isDirectory()) walk(child);
      else relativeFiles.push(child);
    }
  };
  walk('');
  const hash = createHash('sha256');
  for (const relative of relativeFiles.sort()) {
    // The path and a NUL separator participate in the hash so moving bytes between files
    // cannot collide with the unmoved set.
    hash.update(relative);
    hash.update('\0');
    hash.update(readFileSync(path.join(contractsDir, relative)));
    hash.update('\0');
  }
  return hash.digest('hex');
}

/** Disposable deployment custom resource that idempotently seeds reviewed v1 prompt records. */
export class InsightPromptRegistrySeedStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightPromptRegistrySeedStackProps) {
    super(scope, id, props);
    const { config } = props;
    const read = (key: Parameters<typeof paramPath>[1]): string => ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const registryName = read('insightPromptRegistryTableName');
    const registryArn = read('insightPromptRegistryTableArn');
    const dependencies = lambda.LayerVersion.fromLayerVersionArn(this, 'PythonDependencies', read('dependenciesLayerArn'));
    const logGroup = new logs.LogGroup(this, 'SeedLogs', { logGroupName: `/aws/lambda/adlytics-${config.envName}-InsightPromptRegistrySeed`, retention: config.logRetentionDays, removalPolicy: cdk.RemovalPolicy.DESTROY });
    const role = lambdaRole(this, 'SeedServiceRole');
    grantOwnLogWrites(role, logGroup);
    const seed = new lambda.Function(this, 'Seed', {
      functionName: `adlytics-${config.envName}-InsightPromptRegistrySeed`,
      role,
      runtime: new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON), architecture: lambda.Architecture.ARM_64,
      code: lambda.Code.fromAsset(props.sourceAssetPath), handler: 'adapters.jobs.prompt_registry_seed.handler.handler', layers: [dependencies],
      environment: { ...tracingEnvironment(config), ADL_INSIGHT_PROMPT_REGISTRY_TABLE: registryName },
      memorySize: 512, timeout: cdk.Duration.seconds(30), logGroup,
    });
    grantSpanExport(seed);
    // TransactWriteItems authorizes each item's underlying action, so the Put-only seed
    // transaction needs dynamodb:PutItem — the same finding the ingestion role recorded
    // (validation/RESULTS.md, Semantic V2 ingestion acceptance 2026-09-02).
    seed.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:TransactWriteItems'], resources: [registryArn] }));
    // The framework's onEvent function writes only its own declared group (ADR-085). No
    // providerFunctionName: naming the framework function would replace it and its ServiceToken.
    const providerLogs = new logs.LogGroup(this, 'SeedProviderLogs', {
      retention: config.logRetentionDays,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const providerRole = lambdaRole(this, 'SeedProviderRole');
    grantOwnLogWrites(providerRole, providerLogs);
    const provider = new cr.Provider(this, 'SeedProvider', {
      onEventHandler: seed,
      frameworkOnEventRole: providerRole,
      logGroup: providerLogs,
    });
    // The digest is synth-computed from the exact contract bytes, so any insight-contract
    // add/edit/removal changes the property and re-fires the idempotent seed handler.
    new cdk.CustomResource(this, 'SeedV1Contracts', {
      serviceToken: provider.serviceToken,
      properties: { contractSet: 'insight-v1', contractDigest: insightContractSetDigest(props.insightContractsPath) },
    });
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }
}
