// SPDX-License-Identifier: MIT-0
/**
 * The README's Lambda concurrency prerequisite, pinned to what the shipped composition reserves.
 *
 * Lambda keeps 100 of an account's concurrent executions unreserved, so an account whose applied
 * quota is below 100 + the environment's reservations fails at the first stack that reserves. The
 * README stated 33 / 133 while the composition a customer reaches once producer onboarding is on
 * (the only way data reaches their lake) reserves 35, so a quota of 133 or 134 met the documented
 * prerequisite and still failed (review F3, 2026-10-19). This test synthesizes the stacks
 * `bin/app.ts` composes under a customer's configuration, sums every positive
 * `ReservedConcurrentExecutions`, and requires the README to state exactly those figures.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DEPLOY_ORDER, defaultConfig } from '../lib/config';
import type { AppConfig } from '../lib/config';
import { AgentQualityProcessorStack } from '../lib/stacks/agent-quality-processor-stack';
import { AgentQualityStack } from '../lib/stacks/agent-quality-stack';
import { AgentStack } from '../lib/stacks/agent-stack';
import { ApiStack } from '../lib/stacks/api-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { DataV2Stack } from '../lib/stacks/data-v2-stack';
import { DependenciesStack } from '../lib/stacks/dependencies-stack';
import { EdgeSecurityStack } from '../lib/stacks/edge-security-stack';
import { FrontendStack } from '../lib/stacks/frontend-stack';
import { HarnessStack } from '../lib/stacks/harness-proof-stack';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { InsightCacheStack } from '../lib/stacks/insight-cache-stack';
import { InsightContinuationStack } from '../lib/stacks/insight-continuation-stack';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { InsightPromptCalibrationStack } from '../lib/stacks/insight-prompt-calibration-stack';
import { InsightPromptRegistrySeedStack } from '../lib/stacks/insight-prompt-registry-seed-stack';
import { InsightPromptRegistryStack } from '../lib/stacks/insight-prompt-registry-stack';
import { MemoryStack } from '../lib/stacks/memory-stack';
import { PublisherArtifactStoreStack } from '../lib/stacks/publisher-artifact-store-stack';
import { PublisherStack } from '../lib/stacks/publisher-stack';
import { RecordIntegrityStack } from '../lib/stacks/record-integrity-stack';
import { TokenServiceStack } from '../lib/stacks/token-service-stack';
import { TEST_CONFIG, configWith } from './support/config';
import {
  MANAGED_CONVERSATION_PROMPT,
  MEMORY_PROMPTS,
  ROUTE_ALLOWLIST,
  resourcesOfType,
} from './support/templates';
const ASSET = __dirname;
const REPOSITORY = path.join(__dirname, '..', '..');
const CONTRACTS = path.join(REPOSITORY, 'contracts');
const TOOLS = path.join(CONTRACTS, 'generated', 'tools.json');
/**
 * Stacks `bin/app.ts` composes only behind a flag a customer's first deployment leaves off. Listed
 * so a new unconditional stack cannot escape the sum: the coverage assertion below fails until it
 * is composed here or listed here with its flag.
 */
const OFF_BY_DEFAULT: Record<string, string> = {
  OperationalStatusStoreStack: 'operationalStatus.provisioned (ENV=dev only)',
  OperationalStatusProcessorStack: 'operationalStatus.provisioned (ENV=dev only)',
  ControlPlaneAuthorityStoreStack: 'operationalStatus.provisioned (ENV=dev only)',
  ObservabilityStack: 'transactionSearchEnabled',
  EvaluationEvidenceStack: 'evaluationInput.provisioned',
  EvaluationInputStack: 'evaluationInput.provisioned',
  EvaluationOutboxDispatchStack: 'evaluationInput.provisioned',
  EvaluationResultLinkV3Stack: 'onlineEvaluation.provisioned (ENV=dev only)',
  EvaluationV4Stack: 'onlineEvaluation.provisioned (ENV=dev only)',
  EvaluationQualityStack: 'onlineEvaluation.provisioned (ENV=dev only)',
};
/** A customer environment: the shipped ingestion config, every optional flag off. */
function customerConfig(landing: AppConfig['landing']): AppConfig {
  const config = configWith({
    envName: 'pubcheck',
    landing,
    ingestion: defaultConfig('pubcheck').ingestion,
  });
  // The flags OFF_BY_DEFAULT names, as a customer's first deployment has them.
  expect([
    config.operationalStatus.provisioned,
    config.transactionSearchEnabled,
    config.evaluationInput.provisioned,
    config.onlineEvaluation.provisioned,
  ]).toEqual([false, false, false, false]);
  return config;
}
/** Every stack `bin/app.ts` composes unconditionally, with the same props it passes. */
function composedStacks(config: AppConfig): Record<string, cdk.Stack> {
  const env = { account: config.account, region: config.region };
  const app = new cdk.App();
  return {
    DependenciesStack: new DependenciesStack(app, 'Dependencies', { config, env, layerAssetPath: ASSET }),
    DataV2Stack: new DataV2Stack(app, 'DataV2', {
      config,
      env,
      tablesPath: path.join(CONTRACTS, 'generated', 'v2-tables.json'),
    }),
    PublisherArtifactStoreStack: new PublisherArtifactStoreStack(app, 'PublisherArtifactStore', { config, env }),
    PublisherStack: new PublisherStack(app, 'Publisher', {
      config,
      env,
      sourceAssetPath: ASSET,
      publisherInputPath: path.join(REPOSITORY, 'presentation', 'pages', 'v2', 'pages.yaml'),
    }),
    IngestionStack: new IngestionStack(app, 'Ingestion', { config, env, sourceAssetPath: ASSET }),
    EdgeSecurityStack: new EdgeSecurityStack(app, 'EdgeSecurity', {
      config,
      env: { account: config.account, region: 'us-east-1' },
    }),
    FrontendStack: new FrontendStack(app, 'Frontend', { config, env, routeAllowlistPath: ROUTE_ALLOWLIST }),
    AuthStack: new AuthStack(app, 'Auth', { config, env }),
    TokenServiceStack: new TokenServiceStack(app, 'TokenService', { config, env, sourceAssetPath: ASSET }),
    MemoryStack: new MemoryStack(app, 'Memory', { config, env, promptPaths: MEMORY_PROMPTS }),
    InsightPromptRegistryStack: new InsightPromptRegistryStack(app, 'InsightPromptRegistry', { config, env }),
    InsightPromptRegistrySeedStack: new InsightPromptRegistrySeedStack(app, 'InsightPromptRegistrySeed', {
      config,
      env,
      sourceAssetPath: ASSET,
      insightContractsPath: path.join(CONTRACTS, 'insights'),
    }),
    AgentQualityStack: new AgentQualityStack(app, 'AgentQuality', { config, env }),
    AgentQualityProcessorStack: new AgentQualityProcessorStack(app, 'AgentQualityProcessor', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    AgentStack: new AgentStack(app, 'Agent', { config, env, sourceAssetPath: ASSET, toolSchemaPath: TOOLS }),
    HarnessStack: new HarnessStack(app, 'Harness', {
      config,
      env,
      toolSchemaPath: TOOLS,
      promptPath: MANAGED_CONVERSATION_PROMPT,
      sourceAssetPath: ASSET,
    }),
    RecordIntegrityStack: new RecordIntegrityStack(app, 'RecordIntegrity', { config, env }),
    InsightCacheStack: new InsightCacheStack(app, 'InsightCache', { config, env }),
    InsightContinuationStack: new InsightContinuationStack(app, 'InsightContinuation', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    InsightPromptCalibrationStack: new InsightPromptCalibrationStack(app, 'InsightPromptCalibration', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    ApiStack: new ApiStack(app, 'Api', { config, env, sourceAssetPath: ASSET }),
    InsightPrecomputeStack: new InsightPrecomputeStack(app, 'InsightPrecompute', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
  };
}
/** Positive reservations per stack; a stack that reserves nothing is omitted. */
function reservedByStack(stacks: Record<string, cdk.Stack>): Record<string, number> {
  const reserved: Record<string, number> = {};
  for (const [name, stack] of Object.entries(stacks)) {
    const total = resourcesOfType(Template.fromStack(stack).toJSON(), 'AWS::Lambda::Function')
      .map((fn) => fn.ReservedConcurrentExecutions)
      .filter((value): value is number => typeof value === 'number' && value > 0)
      .reduce((sum, value) => sum + value, 0);
    if (total > 0) reserved[name] = total;
  }
  return reserved;
}
function sum(reserved: Record<string, number>): number {
  return Object.values(reserved).reduce((total, value) => total + value, 0);
}
/** Lambda keeps this many of the account's concurrent executions unreserved. */
const UNRESERVED_FLOOR = 100;
describe('README Lambda concurrency prerequisite', () => {
  const onboarding: AppConfig['landing'] = TEST_CONFIG.landing;
  it('composes every stack in DEPLOY_ORDER or names the flag that leaves it out', () => {
    const composed = Object.keys(composedStacks(customerConfig(onboarding)));
    expect([...composed, ...Object.keys(OFF_BY_DEFAULT)].sort()).toEqual(
      [...DEPLOY_ORDER].sort(),
    );
  });
  it('states exactly what a customer environment reserves, and the quota that leaves 100 unreserved', () => {
    expect(onboarding.enabled).toBe(true);
    const withOnboarding = reservedByStack(composedStacks(customerConfig(onboarding)));
    const beforeOnboarding = reservedByStack(composedStacks(customerConfig({ enabled: false })));
    // Every reserving stack is in the application Region, where the quota is read.
    expect(withOnboarding).toEqual({
      PublisherStack: 11,
      IngestionStack: 16,
      AgentQualityProcessorStack: 2,
      InsightContinuationStack: 2,
      InsightPromptCalibrationStack: 2,
      InsightPrecomputeStack: 2,
    });
    // Producer onboarding adds only ProducerFinalizer's reservation of 2.
    expect(beforeOnboarding).toEqual({ ...withOnboarding, IngestionStack: 14 });
    const reserved = sum(withOnboarding);
    expect([reserved, sum(beforeOnboarding)]).toEqual([35, 33]);
    const readme = fs.readFileSync(path.join(REPOSITORY, 'README.md'), 'utf8');
    const quota = reserved + UNRESERVED_FLOOR;
    expect(readme).toContain(
      `One environment reserves ${reserved} concurrent executions for its functions once producer ` +
        `onboarding is on (${sum(beforeOnboarding)} before), and Lambda always keeps ` +
        `${UNRESERVED_FLOOR} of the account's concurrency unreserved.`,
    );
    expect(readme).toContain(`must therefore be at least ${quota}.`);
    expect(readme).toContain(`request an increase in Service Quotas if it is below ${quota};`);
  });
});
