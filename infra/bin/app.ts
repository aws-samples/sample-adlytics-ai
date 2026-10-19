#!/usr/bin/env node
// SPDX-License-Identifier: MIT-0
/**
 * CDK app entrypoint.
 *
 * The environment is selected by name with `-c env=<name>`, and the config it
 * resolves to is typed — see lib/config. Any well-formed name works; there is no
 * fixed set, and there is no default: the name is required. `getConfig` validates
 * it and refuses an incomplete configuration, naming every gap, so a synth cannot
 * produce a template that deploys somewhere unintended.
 *
 * No stack receives another stack's constructs. Every cross-stack value travels
 * through SSM (ADR-012), which is why the stacks below take only `config` and why
 * they can be deployed and destroyed independently. Deploy order is stated once,
 * in DEPLOY_ORDER, because SSM no longer lets CloudFormation infer it.
 */
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as path from 'node:path';

import { ConfigurationIncompleteError, EDGE_SECURITY_REGION, getConfig } from '../lib/config';
import { AgentStack } from '../lib/stacks/agent-stack';
import { AgentQualityProcessorStack } from '../lib/stacks/agent-quality-processor-stack';
import { AgentQualityStack } from '../lib/stacks/agent-quality-stack';
import { ApiStack } from '../lib/stacks/api-stack';
import { ControlPlaneAuthorityStoreStack } from '../lib/stacks/control-plane-authority-store-stack';
import { AuthStack } from '../lib/stacks/auth-stack';
import { DataV2Stack } from '../lib/stacks/data-v2-stack';
import { DependenciesStack } from '../lib/stacks/dependencies-stack';
import { EdgeSecurityStack } from '../lib/stacks/edge-security-stack';
import { EvaluationEvidenceStack } from '../lib/stacks/evaluation-evidence-stack';
import { EvaluationInputStack } from '../lib/stacks/evaluation-input-stack';
import { EvaluationOutboxDispatchStack } from '../lib/stacks/evaluation-outbox-dispatch-stack';
import { EvaluationQualityStack } from '../lib/stacks/evaluation-quality-stack';
import { EvaluationResultLinkV3Stack } from '../lib/stacks/evaluation-result-link-v3-stack';
import { EvaluationV4Stack } from '../lib/stacks/evaluation-v4-stack';
import { FrontendStack } from '../lib/stacks/frontend-stack';
import { HarnessStack } from '../lib/stacks/harness-proof-stack';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { InsightCacheStack } from '../lib/stacks/insight-cache-stack';
import { InsightContinuationStack } from '../lib/stacks/insight-continuation-stack';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { InsightPromptCalibrationStack } from '../lib/stacks/insight-prompt-calibration-stack';
import { InsightPromptRegistryStack } from '../lib/stacks/insight-prompt-registry-stack';
import { InsightPromptRegistrySeedStack } from '../lib/stacks/insight-prompt-registry-seed-stack';
import { MemoryStack } from '../lib/stacks/memory-stack';
import { ObservabilityStack } from '../lib/stacks/observability-stack';
import { OperationalStatusProcessorStack } from '../lib/stacks/operational-status-processor-stack';
import { OperationalStatusStoreStack } from '../lib/stacks/operational-status-store-stack';
import { PublisherArtifactStoreStack } from '../lib/stacks/publisher-artifact-store-stack';
import { PublisherStack } from '../lib/stacks/publisher-stack';
import { RecordIntegrityStack } from '../lib/stacks/record-integrity-stack';
import { TokenServiceStack } from '../lib/stacks/token-service-stack';
import { orderStacksForDeployment } from '../lib/deploy-order';
import { AsyncFailureBoundsAspect } from '../lib/mixins/async-failure-bounds';
import { ScanDispositionsAspect } from '../lib/mixins/scan-dispositions';

const app = new cdk.App();

/**
 * Required, with no default.
 *
 * A defaulted name is the same hazard as a defaulted profile: an operator whose
 * environment is `acme-staging` and who omits it would synthesise against
 * `/adlytics/dev/...` and `Adlytics-*-dev`, and every command would still
 * succeed. Absent must fail here, before anything is synthesised.
 */
const envName: unknown = app.node.tryGetContext('env');
if (typeof envName !== 'string' || envName.trim() === '') {
  throw new Error(
    'The environment name is required and has no default. Pass it as CDK context:\n' +
      '  make synth ENV=<name> PROFILE=<profile>\n' +
      '  make diff ENV=<name> PROFILE=<profile>\n' +
      '  make deploy STACK=Adlytics-<Area>-<name> ENV=<name> PROFILE=<profile> APPROVE=1\n' +
      'Directly: npx cdk synth -c env=<name> --profile <profile>',
  );
}

/**
 * An incomplete configuration is a guard doing its job, not a defect, so it is
 * reported as a message and nothing else. Every other error keeps its stack trace,
 * because a trace is what you want when the cause is unknown and noise when it is
 * not — and before this split, a fresh checkout reported a missing login prefix as
 * an uncaught exception indistinguishable from a bug.
 */
function resolveConfig(name: string): ReturnType<typeof getConfig> {
  try {
    return getConfig(name);
  } catch (error) {
    if (error instanceof ConfigurationIncompleteError) {
      console.error(error.format());
      process.exit(1);
    }
    throw error;
  }
}

const config = resolveConfig(envName);

const env = { account: config.account, region: config.region };

/**
 * Built by `make build-lambdas`. Referenced rather than bundled at synth so no
 * Docker daemon sits on the critical path — see the compute steering.
 */
const buildRoot = path.join(__dirname, '..', '..', 'build');
const stagedSourcePath = path.join(buildRoot, 'src');
const stagedContractsPath = path.join(stagedSourcePath, 'contracts');
const stagedPublisherInputPath = path.join(
  stagedSourcePath,
  'presentation',
  'pages',
  'v2',
  'pages.yaml',
);

/**
 * Generated by `make generate-schemas` from the Semantic V2 data contracts, and committed.
 *
 * Resolved here rather than inside DataV2Stack so this file stays the only place that knows
 * the repository layout, the same as `buildRoot` above.
 */
const generatedV2TablesPath = path.join(
  __dirname,
  '..',
  '..',
  'contracts',
  'generated',
  'v2-tables.json',
);

/**
 * Generated from `presentation/navigation.yaml` by `presentation_validation.navigation` and
 * committed; `make validate-page-schemas` rejects it when stale. `StaticRouteRewrite` serves only
 * these routes (ADR-123).
 */
const routeAllowlistPath = path.join(
  __dirname,
  '..',
  '..',
  'contracts',
  'generated',
  'route-allowlist.json',
);
/** The exact generated tool contract packaged into the Lambda source asset. */
const generatedToolsPath = path.join(stagedContractsPath, 'generated', 'tools.json');
const managedConversationPromptPath = path.join(
  __dirname,
  '..',
  '..',
  'contracts',
  'prompts',
  'v1',
  'managed-conversation.md',
);
const evaluatorV4PromptPaths = {
  groundedCorrectness: path.join(
    __dirname, '..', '..', 'contracts', 'prompts', 'v1', 'grounded-response-correctness.md',
  ),
  internalConsistency: path.join(
    __dirname, '..', '..', 'contracts', 'prompts', 'v1', 'response-internal-consistency.md',
  ),
  helpfulness: path.join(
    __dirname, '..', '..', 'contracts', 'prompts', 'v1', 'released-response-helpfulness-v4.md',
  ),
};
/** The long-term memory override prompts (ADR-147), read once at synth like the Harness prompt. */
const memoryPromptPaths = {
  extraction: path.join(
    __dirname, '..', '..', 'contracts', 'prompts', 'v1', 'memory-context-extraction.md',
  ),
  consolidation: path.join(
    __dirname, '..', '..', 'contracts', 'prompts', 'v1', 'memory-context-consolidation.md',
  ),
};
/** The fictional calibration corpus the evaluation gate runs before it enables (ADR-079). */
const evaluationV4CorpusPath = path.join(__dirname, '..', '..', 'validation', 'evaluation-v4-cases.json');

// First in DEPLOY_ORDER: it depends on nothing, and every stack that declares a Lambda reads the
// layer ARN it publishes.
new DependenciesStack(app, `Adlytics-Dependencies-${envName}`, {
  config,
  env,
  layerAssetPath: path.join(buildRoot, 'layer'),
});

new DataV2Stack(app, `Adlytics-DataV2-${envName}`, {
  config,
  env,
  tablesPath: generatedV2TablesPath,
});

if (config.operationalStatus.provisioned) {
  new OperationalStatusStoreStack(app, `Adlytics-OperationalStatusStore-${envName}`, {
    config,
    env,
  });
  new OperationalStatusProcessorStack(app, `Adlytics-OperationalStatusProcessor-${envName}`, {
    config,
    env,
    sourceAssetPath: stagedSourcePath,
  });
  // The retained execution-graph authority store (S1), before the API that admits into it.
  new ControlPlaneAuthorityStoreStack(app, `Adlytics-ControlPlaneAuthorityStore-${envName}`, {
    config,
    env,
  });
}

new PublisherArtifactStoreStack(app, `Adlytics-PublisherArtifactStore-${envName}`, {
  config,
  env,
});

new PublisherStack(app, `Adlytics-Publisher-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
  publisherInputPath: stagedPublisherInputPath,
});

// Ingestion follows retained DataV2 and precedes every canonical reader, as DEPLOY_ORDER declares.
new IngestionStack(app, `Adlytics-Ingestion-${envName}`, {
  config,
  env,
  sourceAssetPath: path.join(buildRoot, 'src'),
});

// ADR-078: the distribution's web ACL. The one stack not in the application region, because a
// `CLOUDFRONT`-scope ACL exists only in us-east-1; before FrontendStack, which reads its ARN.
new EdgeSecurityStack(app, `Adlytics-EdgeSecurity-${envName}`, {
  config,
  env: { account: config.account, region: EDGE_SECURITY_REGION },
});

new FrontendStack(app, `Adlytics-Frontend-${envName}`, { config, env, routeAllowlistPath });

new AuthStack(app, `Adlytics-Auth-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

// After AuthStack, whose pool the route exchange verifies against, and before the Harness and API
// that read its issuer URL and route-exchange ARN — per DEPLOY_ORDER (ADR-077 §3).
new TokenServiceStack(app, `Adlytics-TokenService-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

// RETAIN state; it is isolated from disposable Runtime generations and created with the environment.
new MemoryStack(app, `Adlytics-Memory-${envName}`, { config, env, promptPaths: memoryPromptPaths });

new InsightPromptRegistryStack(app, `Adlytics-InsightPromptRegistry-${envName}`, { config, env });
new InsightPromptRegistrySeedStack(app, `Adlytics-InsightPromptRegistrySeed-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
  // The staged copy, not the repository one: the digest must identify the exact bytes the
  // packaged seed handler will load at runtime.
  insightContractsPath: path.join(stagedContractsPath, 'insights'),
});


new AgentQualityStack(app, `Adlytics-AgentQuality-${envName}`, { config, env });

new AgentQualityProcessorStack(app, `Adlytics-AgentQualityProcessor-${envName}`, {
  config,
  env,
  sourceAssetPath: path.join(buildRoot, 'src'),
});

// Before AgentStack, because AgentCore cannot deliver spans to a per-agent log group unless
// account-wide Transaction Search is already on, and a span destination is fixed at agent
// creation. Holds configuration only and produces no telemetry itself.
//
// **Instantiated only when span ingestion is on, and that is not a shortcut.** The stack's one
// resource *is* the account-wide setting, so with ingestion off there is nothing for it to
// hold — and a stack with an empty `Resources` section is rejected by CloudFormation, which
// would make the shipped default undeployable. Turning the flag on adds the stack; turning it
// off later means `cdk destroy Adlytics-Observability-<env> --exclusively`, because synthesising nothing does
// not delete what is already deployed.
if (config.transactionSearchEnabled) {
  new ObservabilityStack(app, `Adlytics-Observability-${envName}`, { config, env });
}

// After ObservabilityStack and before ApiStack, per DEPLOY_ORDER. Unconditional, unlike the stack
// above it: the transaction-search flag decides whether spans are *ingested*, never whether the
// tool functions exist. Turning ingestion off must lose traces, not the platform.
new AgentStack(app, `Adlytics-Agent-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
  toolSchemaPath: generatedToolsPath,
});

new HarnessStack(app, `Adlytics-Harness-${envName}`, {
  config,
  env,
  toolSchemaPath: generatedToolsPath,
  promptPath: managedConversationPromptPath,
  sourceAssetPath: stagedSourcePath,
});

if (config.evaluationInput.provisioned) {
  new EvaluationEvidenceStack(app, `Adlytics-EvaluationEvidence-${envName}`, {
    config,
    env,
  });
  new EvaluationInputStack(app, `Adlytics-EvaluationInput-${envName}`, {
    config,
    env,
    sourceAssetPath: path.join(buildRoot, 'src'),
  });
  new EvaluationOutboxDispatchStack(app, `Adlytics-EvaluationOutboxDispatch-${envName}`, {
    config,
    env,
    sourceAssetPath: path.join(buildRoot, 'src'),
  });
}

// ADR-080 §4: after every retained record table, per DEPLOY_ORDER. Unconditional: the receipts and
// findings tables always exist, and it watches the evaluation tables only when they are composed.
new RecordIntegrityStack(app, `Adlytics-RecordIntegrity-${envName}`, { config, env });

new InsightCacheStack(app, `Adlytics-InsightCache-${envName}`, { config, env });

new InsightContinuationStack(app, `Adlytics-InsightContinuation-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

new InsightPromptCalibrationStack(app, `Adlytics-InsightPromptCalibration-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

new ApiStack(app, `Adlytics-Api-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

new InsightPrecomputeStack(app, `Adlytics-InsightPrecompute-${envName}`, {
  config,
  env,
  sourceAssetPath: stagedSourcePath,
});

// ADR-079: the result link and the evaluation gate are provisioned together. They share only the
// owned result-group names EvaluationEvidenceStack publishes and fixed names from lib/config; the
// deploy order between them is DEPLOY_ORDER's.
if (config.onlineEvaluation.provisioned) {
  new EvaluationResultLinkV3Stack(app, `Adlytics-EvaluationResultLinkV3-${envName}`, {
    config,
    env,
    sourceAssetPath: path.join(buildRoot, 'src'),
  });
  // The V3 family was retired 2026-09-27 (ADR-079); V4 is the only family.
  new EvaluationV4Stack(app, `Adlytics-EvaluationV4-${envName}`, {
    config,
    env,
    promptPaths: evaluatorV4PromptPaths,
    calibrationCorpusPath: evaluationV4CorpusPath,
    sourceAssetPath: stagedSourcePath,
  });
  // ADR-079 §3, §5: last in DEPLOY_ORDER; reads the same prompt files for the evaluator names.
  new EvaluationQualityStack(app, `Adlytics-EvaluationQuality-${envName}`, {
    config,
    env,
    promptPaths: evaluatorV4PromptPaths,
  });
}

// Remaining stacks land here as each area is built: Ml.
// Each takes `config` only.

// After every stack exists: `cdk deploy --all` (make deploy-all) then follows DEPLOY_ORDER.
orderStacksForDeployment(app);

// T1 (ADR-133): synth fails on any async consumer without bounded retries, a failure
// destination and an arrival alarm on it.
cdk.Aspects.of(app).add(new AsyncFailureBoundsAspect());
// Template scanner dispositions as resource metadata (metadata only; docs/security-dispositions.md).
cdk.Aspects.of(app).add(new ScanDispositionsAspect());

app.synth();
