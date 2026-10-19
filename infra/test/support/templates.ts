// SPDX-License-Identifier: MIT-0
/**
 * Every stack the app builds, synthesised from one complete configuration.
 *
 * Extracted from `iam-forbidden-actions.test.ts` when a second cross-stack gate needed the same
 * coverage. **The value here is not the synthesis, it is `NOT_YET_BUILT`**: a gate that enumerates
 * stacks in its own file stops covering the next stack somebody adds, silently, and that is exactly
 * the failure a per-stack test file has. Keeping the list in one place means a new stack has to be
 * added once and both gates pick it up.
 *
 * Instantiates `ObservabilityStack` unconditionally, unlike `bin/app.ts`, which builds it only when
 * span ingestion is on. A gate that skipped it would stop covering the stack the moment the flag
 * flipped, which is backwards: the flag decides whether spans are ingested, never whether the
 * stack's policies are safe.
 */
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AgentStack } from '../../lib/stacks/agent-stack';
import { AgentQualityProcessorStack } from '../../lib/stacks/agent-quality-processor-stack';
import { AgentQualityStack } from '../../lib/stacks/agent-quality-stack';
import { ApiStack } from '../../lib/stacks/api-stack';
import { ControlPlaneAuthorityStoreStack } from '../../lib/stacks/control-plane-authority-store-stack';
import { AuthStack } from '../../lib/stacks/auth-stack';
import { DataV2Stack } from '../../lib/stacks/data-v2-stack';
import { DependenciesStack } from '../../lib/stacks/dependencies-stack';
import { EvaluationEvidenceStack } from '../../lib/stacks/evaluation-evidence-stack';
import { EvaluationInputStack } from '../../lib/stacks/evaluation-input-stack';
import { EvaluationOutboxDispatchStack } from '../../lib/stacks/evaluation-outbox-dispatch-stack';
import { EvaluationResultLinkV3Stack } from '../../lib/stacks/evaluation-result-link-v3-stack';
import { EvaluationQualityStack } from '../../lib/stacks/evaluation-quality-stack';
import { EvaluationV4Stack } from '../../lib/stacks/evaluation-v4-stack';
import { EdgeSecurityStack } from '../../lib/stacks/edge-security-stack';
import { FrontendStack } from '../../lib/stacks/frontend-stack';
import { HarnessStack } from '../../lib/stacks/harness-proof-stack';
import { IngestionStack } from '../../lib/stacks/ingestion-stack';
import { InsightCacheStack } from '../../lib/stacks/insight-cache-stack';
import { InsightContinuationStack } from '../../lib/stacks/insight-continuation-stack';
import { InsightPrecomputeStack } from '../../lib/stacks/insight-precompute-stack';
import { InsightPromptCalibrationStack } from '../../lib/stacks/insight-prompt-calibration-stack';
import { InsightPromptRegistryStack } from '../../lib/stacks/insight-prompt-registry-stack';
import { InsightPromptRegistrySeedStack } from '../../lib/stacks/insight-prompt-registry-seed-stack';
import { MemoryStack } from '../../lib/stacks/memory-stack';
import { ObservabilityStack } from '../../lib/stacks/observability-stack';
import { OperationalStatusProcessorStack } from '../../lib/stacks/operational-status-processor-stack';
import { OperationalStatusStoreStack } from '../../lib/stacks/operational-status-store-stack';
import { PublisherArtifactStoreStack } from '../../lib/stacks/publisher-artifact-store-stack';
import { PublisherStack } from '../../lib/stacks/publisher-stack';
import { RecordIntegrityStack } from '../../lib/stacks/record-integrity-stack';
import { TokenServiceStack } from '../../lib/stacks/token-service-stack';
import { AsyncFailureBoundsAspect } from '../../lib/mixins/async-failure-bounds';
import { ScanDispositionsAspect } from '../../lib/mixins/scan-dispositions';
import { TEST_CONFIG, configWith } from './config';

/** Any directory will do: `Code.fromAsset` only needs one to exist at synth. */
const ASSET = __dirname;
const CONTRACTS = path.join(__dirname, '..', '..', '..', 'contracts');
const GENERATED_CONTRACTS = path.join(CONTRACTS, 'generated');
/** The committed route allowlist `FrontendStack` reads at synth (ADR-123). */
export const ROUTE_ALLOWLIST = path.join(GENERATED_CONTRACTS, 'route-allowlist.json');
const PUBLISHER_INPUT = path.join(
  __dirname,
  '..',
  '..',
  '..',
  'presentation',
  'pages',
  'v2',
  'pages.yaml',
);
/** The canonical managed-conversation system prompt the Harness reads at synth. */
export const MANAGED_CONVERSATION_PROMPT = path.join(
  CONTRACTS,
  'prompts',
  'v1',
  'managed-conversation.md',
);
/** The fictional calibration corpus the evaluation gate carries (ADR-079). */
export const EVALUATION_CORPORA = {
  v4: path.join(__dirname, '..', '..', '..', 'validation', 'evaluation-v4-cases.json'),
};
/** The long-term memory override prompt contracts (ADR-147). */
export const MEMORY_PROMPTS = {
  extraction: path.join(CONTRACTS, 'prompts', 'v1', 'memory-context-extraction.md'),
  consolidation: path.join(CONTRACTS, 'prompts', 'v1', 'memory-context-consolidation.md'),
};
export const EVALUATOR_V4_PROMPTS = {
  groundedCorrectness: path.join(CONTRACTS, 'prompts', 'v1', 'grounded-response-correctness.md'),
  internalConsistency: path.join(CONTRACTS, 'prompts', 'v1', 'response-internal-consistency.md'),
  helpfulness: path.join(CONTRACTS, 'prompts', 'v1', 'released-response-helpfulness-v4.md'),
};


/**
 * Stacks in `DEPLOY_ORDER` that do not exist yet.
 *
 * This list must only ever shrink. A coverage assertion fails when a stack lands and is not added
 * to `everyTemplate()`, which is what stops a new stack quietly escaping every gate at once.
 */
export const NOT_YET_BUILT = ['MlStack'];

export function everyTemplate(): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(everyStack()).map(([name, stack]) => [name, Template.fromStack(stack).toJSON()]),
  );
}

/** The stacks behind `everyTemplate()`, for a gate that also reads the construct tree. */
export function everyStack(): Record<string, cdk.Stack> {
  const config = TEST_CONFIG;
  const evaluationConfig = configWith({
    transactionSearchEnabled: true,
    operationalStatus: {
      provisioned: true,
      stage1LifecycleReconcileBatchSize: 10,
      stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
    },
    evaluationInput: { provisioned: true },
    onlineEvaluation: {
      provisioned: true,
      samplingPercentage: 10,
      sessionTimeoutMinutes: 1,
    },
  });
  const env = { account: config.account, region: config.region };
  const app = new cdk.App();
  // As bin/app.ts applies it: a violating stack throws at Template.fromStack.
  cdk.Aspects.of(app).add(new AsyncFailureBoundsAspect());
  cdk.Aspects.of(app).add(new ScanDispositionsAspect());
  const stacks = {
    DependenciesStack: new DependenciesStack(app, 'Dependencies', {
      config,
      env,
      layerAssetPath: ASSET,
    }),
    DataV2Stack: new DataV2Stack(app, 'DataV2', {
      config,
      env,
      tablesPath: path.join(GENERATED_CONTRACTS, 'v2-tables.json'),
    }),
    OperationalStatusStoreStack: new OperationalStatusStoreStack(app, 'OperationalStatusStore', {
      config: evaluationConfig,
      env,
    }),
    ControlPlaneAuthorityStoreStack: new ControlPlaneAuthorityStoreStack(
      app,
      'ControlPlaneAuthorityStore',
      { config: evaluationConfig, env },
    ),
    OperationalStatusProcessorStack: new OperationalStatusProcessorStack(
      app,
      'OperationalStatusProcessor',
      { config: evaluationConfig, env, sourceAssetPath: ASSET },
    ),
    PublisherArtifactStoreStack: new PublisherArtifactStoreStack(app, 'PublisherArtifactStore', {
      config,
      env,
    }),
    PublisherStack: new PublisherStack(app, 'Publisher', {
      config,
      env,
      sourceAssetPath: ASSET,
      publisherInputPath: PUBLISHER_INPUT,
    }),
    EdgeSecurityStack: new EdgeSecurityStack(app, 'EdgeSecurity', {
      config,
      env: { account: config.account, region: 'us-east-1' },
    }),
    FrontendStack: new FrontendStack(app, 'Frontend', { config, env, routeAllowlistPath: ROUTE_ALLOWLIST }),
    AuthStack: new AuthStack(app, 'Auth', { config, env }),
    TokenServiceStack: new TokenServiceStack(app, 'TokenService', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    ObservabilityStack: new ObservabilityStack(app, 'Observability', { config, env }),
    AgentStack: new AgentStack(app, 'Agent', {
      config,
      env,
      sourceAssetPath: ASSET,
      toolSchemaPath: path.join(GENERATED_CONTRACTS, 'tools.json'),
    }),
    HarnessStack: new HarnessStack(app, 'Harness', {
      config,
      env,
      toolSchemaPath: path.join(GENERATED_CONTRACTS, 'tools.json'),
      promptPath: MANAGED_CONVERSATION_PROMPT,
      sourceAssetPath: ASSET,
    }),
    MemoryStack: new MemoryStack(app, 'Memory', { config, env, promptPaths: MEMORY_PROMPTS }),
    InsightPromptRegistryStack: new InsightPromptRegistryStack(app, 'InsightPromptRegistry', { config, env }),
    InsightPromptRegistrySeedStack: new InsightPromptRegistrySeedStack(app, 'InsightPromptRegistrySeed', { config, env, sourceAssetPath: ASSET, insightContractsPath: path.join(CONTRACTS, 'insights') }),
    AgentQualityStack: new AgentQualityStack(app, 'AgentQuality', { config, env }),
    AgentQualityProcessorStack: new AgentQualityProcessorStack(app, 'AgentQualityProcessor', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    // Synthesized with online evaluation provisioned so the template carries the owned result group.
    EvaluationEvidenceStack: new EvaluationEvidenceStack(app, 'EvaluationEvidence', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
    }),
    EvaluationInputStack: new EvaluationInputStack(app, 'EvaluationInput', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
      sourceAssetPath: ASSET,
    }),
    EvaluationOutboxDispatchStack: new EvaluationOutboxDispatchStack(
      app,
      'EvaluationOutboxDispatch',
      {
        config: evaluationConfig,
        env: { account: evaluationConfig.account, region: evaluationConfig.region },
        sourceAssetPath: ASSET,
      },
    ),
    EvaluationV4Stack: new EvaluationV4Stack(app, 'EvaluationV4', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
      promptPaths: EVALUATOR_V4_PROMPTS,
      calibrationCorpusPath: EVALUATION_CORPORA.v4,
      sourceAssetPath: ASSET,
    }),
    EvaluationQualityStack: new EvaluationQualityStack(app, 'EvaluationQuality', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
      promptPaths: EVALUATOR_V4_PROMPTS,
    }),
    EvaluationResultLinkV3Stack: new EvaluationResultLinkV3Stack(app, 'EvaluationResultLinkV3', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
      sourceAssetPath: ASSET,
    }),
    // Synthesized with evaluation input provisioned so the template watches all five tables.
    RecordIntegrityStack: new RecordIntegrityStack(app, 'RecordIntegrity', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
    }),
    InsightCacheStack: new InsightCacheStack(app, 'InsightCache', { config, env }),
    InsightContinuationStack: new InsightContinuationStack(app, 'InsightContinuation', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    InsightPromptCalibrationStack: new InsightPromptCalibrationStack(
      app,
      'InsightPromptCalibration',
      { config, env, sourceAssetPath: ASSET },
    ),
    ApiStack: new ApiStack(app, 'Api', {
      config: evaluationConfig,
      env: { account: evaluationConfig.account, region: evaluationConfig.region },
      sourceAssetPath: ASSET,
    }),
    InsightPrecomputeStack: new InsightPrecomputeStack(app, 'InsightPrecompute', {
      config,
      env,
      sourceAssetPath: ASSET,
    }),
    // S3: synthesized with the operational plane provisioned, as dev deploys it, so the template
    // carries the graph-authority workers and the mode-item grants.
    IngestionStack: new IngestionStack(app, 'Ingestion', {
      config: configWith({ operationalStatus: evaluationConfig.operationalStatus }),
      env,
      sourceAssetPath: ASSET,
    }),
  };
  return stacks;
}

/** Walk a synthesised template, yielding every object that has the given resource type. */
export function resourcesOfType(node: unknown, type: string): Record<string, unknown>[] {
  const found: Record<string, unknown>[] = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (value === null || typeof value !== 'object') return;
    const record = value as Record<string, unknown>;
    if (record.Type === type && typeof record.Properties === 'object' && record.Properties) {
      found.push(record.Properties as Record<string, unknown>);
    }
    Object.values(record).forEach(visit);
  };
  visit(node);
  return found;
}
