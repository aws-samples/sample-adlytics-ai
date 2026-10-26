// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';
import * as nodePath from 'node:path';

import * as logs from 'aws-cdk-lib/aws-logs';
import {
  ConfigurationIncompleteError,
  DEPLOY_ORDER,
  ENV_NAME_MAX_LENGTH,
  PARAMETER_PATH_SEGMENTS,
  PARAMS,
  ParamKey,
  UNSET,
  agentRouteGuardrailName,
  agentQualityAggregateTableName,
  agentQualityAuditTableName,
  agentQualityFindingsTableName,
  agentQualityProcessorFunctionName,
  agentQualityProcessorRoleName,
  clientSecretName,
  conversationFunctionRoleName,
  conversationFunctionTimeoutSeconds,
  defaultConfig,
  evaluationDeliveriesTableName,
  evaluationOutboxDispatcherRoleName,
  evaluationResultIngressRoleName,
  evaluationResultLinkerRoleName,
  evaluationVerdictsTableName,
  evaluationWriterReceiptsTableName,
  evaluationWriterRoleName,
  getConfig,
  harnessName,
  harnessTokenAudience,
  routeClientId,
  TOKEN_SERVICE_STAGE,
  tokenIssuerRoleName,
  tokenRouteExchangeRoleName,
  agentClientId,
  agentClientSecretName,
  gatewayTokenAudience,
  tokenCredentialProviderName,
  EVALUATION_FAMILIES,
  evaluationExecutionRoleName,
  evaluationGateProviderFunctionName,
  evaluationGateProviderRoleName,
  evaluationReleaseParameterName,
  evaluationResultLogGroupName,
  evaluationResultSubscriptionName,
  onlineEvaluationGateName,
  operationalStatusFunctionName,
  operationalStatusProjectionTableName,
  operationalStatusQueueName,
  pageContextTableName,
  paramPath,
  qualityReviewFunctionName,
  qualityReviewFunctionRoleName,
  semanticV2AthenaWorkgroupName,
  semanticV2StateTableName,
} from '../lib/config';
import { ATHENA_START_BUDGET } from '../lib/generated/execution-graph-protocol';

/**
 * Every environment variable `defaultConfig` reads, so the assertions below are about the committed
 * values and not about the shell they ran in.
 *
 * **This covered only the two CDK variables, and the three it missed are the ones an operator is
 * guaranteed to have exported.** `ADLYTICS_LOGIN_DOMAIN_PREFIX` and both model ids are *required* to
 * deploy — synth refuses without them — so anyone running the suite in the shell they just deployed
 * from saw three failures about values they had supplied correctly. A test that fails depending on
 * your shell is one people learn to ignore, which is worse than not having it.
 *
 * Kept as a literal list because `defaultConfig` reads `process.env` inline and there is no declared
 * set to import. The obligation that comes with that: a new environment variable read by the config
 * module must be added here, or these assertions quietly start describing the machine.
 */
const DEPLOYMENT_ENV = [
  'CDK_DEFAULT_ACCOUNT',
  'CDK_DEFAULT_REGION',
  'ADLYTICS_LOGIN_DOMAIN_PREFIX',
  'ADLYTICS_QUICK_CHAT_MODEL_ID',
  'ADLYTICS_DEEP_DIVE_MODEL_ID',
  'ADLYTICS_MEMORY_MODEL_ID',
  'ADLYTICS_INSIGHT_QUICK_MODEL_ID',
  'ADLYTICS_INSIGHT_DEEP_MODEL_ID',
  'ADLYTICS_PAGE_NARRATIVE_MODEL_ID',
  'ADLYTICS_NARRATIVE_ORIGIN_ATTACHED',
  'ADLYTICS_PRODUCER_ONBOARDING_ENABLED',
  'ADLYTICS_LANDING_PIPELINE_ROLE_ARN',
  'ADLYTICS_LANDING_EXTERNAL_ID',
  'ADLYTICS_TRANSACTION_SEARCH',
  'ADLYTICS_ONLINE_EVALUATION_PROVISIONED',
  'ADLYTICS_ONLINE_EVALUATION_V3_REQUESTED',
  'ADLYTICS_ONLINE_EVALUATION_V4_PROVISIONED',
  'ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED',
  'ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT',
  'ADLYTICS_EVALUATION_INPUT_PROVISIONED',
  'ADLYTICS_OPERATIONAL_STATUS_PROVISIONED',
  'ADLYTICS_RECEIPT_RETENTION_DAYS',
  'ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY',
  'ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES',
  'ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES',
  'ADLYTICS_ORIGIN_VERIFY_SECRET_VERSION_ID',
  'ADLYTICS_STAFFED_ALARM_TOPIC_ARN',
  'ADLYTICS_API_ORIGIN_ATTACHED',
  'ADLYTICS_CONVERSATION_ORIGIN_ATTACHED',
  'ADLYTICS_GATEWAY_GUARDRAILS',
  'ADLYTICS_QUALITY_REVIEW_VALIDATION_PASSWORD_AUTH',
  'ADLYTICS_SPAN_INDEXING_PERCENT',
  'ADLYTICS_TRACE_SAMPLE_RATIO',
] as const;

/**
 * Every test starts from none of the variables above, whatever the shell exported, and the shell's
 * values come back afterwards (O-TEST-TRANSPORT-001). A test that needs a value sets it itself.
 */
let shellEnv = new Map<string, string | undefined>();
beforeEach(() => {
  shellEnv = new Map(DEPLOYMENT_ENV.map((name) => [name, process.env[name]]));
  for (const name of DEPLOYMENT_ENV) delete process.env[name];
});
afterEach(() => {
  for (const [name, value] of shellEnv) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

function withoutCdkEnv<T>(body: () => T): T {
  const saved = new Map(DEPLOYMENT_ENV.map((name) => [name, process.env[name]]));
  for (const name of DEPLOYMENT_ENV) delete process.env[name];
  try {
    return body();
  } finally {
    for (const [name, value] of saved) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

const CORE_ENVIRONMENT_SUPPLIED: Readonly<Record<string, string>> = {
  CDK_DEFAULT_ACCOUNT: '111122223333',
  CDK_DEFAULT_REGION: 'eu-west-1',
  ADLYTICS_QUICK_CHAT_MODEL_ID: 'test.quick-chat-model-v1:0',
  ADLYTICS_DEEP_DIVE_MODEL_ID: 'test.deep-dive-model-v1:0',
  ADLYTICS_MEMORY_MODEL_ID: 'test.memory-model-v1:0',
  ADLYTICS_PAGE_NARRATIVE_MODEL_ID: 'test.page-narrative-model-v1:0',
  ADLYTICS_LOGIN_DOMAIN_PREFIX: 'test-login-domain',
  ADLYTICS_RECEIPT_RETENTION_DAYS: '90',
  ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: '8',
  ADLYTICS_EDGE_WAF_RATE_LIMIT_PER_FIVE_MINUTES: '2000',
  ADLYTICS_USER_POOL_WAF_RATE_LIMIT_PER_FIVE_MINUTES: '300',
};

function withDeploymentEnv<T>(values: Readonly<Record<string, string>>, body: () => T): T {
  const saved = { ...process.env };
  try {
    for (const name of DEPLOYMENT_ENV) delete process.env[name];
    Object.assign(process.env, values);
    return body();
  } finally {
    process.env = saved;
  }
}

describe('the environment is a parameter, not a type', () => {
  it('accepts any well-formed name, not a fixed pair', () => {
    // This repository once typed the environment as 'dev' | 'prod', which made a
    // customer's own environment name a source edit and made our two names look
    // like the only legal ones. There is now one code path, and it takes a name.
    for (const envName of ['dev', 'prod', 'sandbox', 'acme-staging', 'eu2']) {
      expect(defaultConfig(envName).envName).toBe(envName);
      expect(paramPath(envName, 'apiId')).toBe(`/adlytics/${envName}/api/id`);
      expect(() => getConfig(envName)).not.toThrow(/[Uu]nknown environment/);
    }
  });

  it('rejects a name that could not survive becoming an AWS identifier', () => {
    // The name lands in an SSM parameter path, the client secret name and every
    // stack ID. Verified limits and the reasoning are in lib/config: stack names
    // are alphanumerics and hyphens starting with a letter, SSM paths are case
    // sensitive and reject spaces.
    const malformed = [
      'Dev', // uppercase: a case-sensitive SSM path that reads as a typo
      'my env', // SSM rejects a name containing a space outright
      'dev/prod', // would silently add a hierarchy level to the path
      '1dev', // a stack name must start with an alphabetic character
      'dev_1', // legal in SSM, illegal in a stack name
      '', // the empty string, which would collapse the path segment
      'a'.repeat(ENV_NAME_MAX_LENGTH + 1),
    ];
    for (const envName of malformed) {
      expect(() => getConfig(envName)).toThrow(/Invalid environment name/);
      expect(() => defaultConfig(envName)).toThrow(/must start with a letter/);
    }
  });

  it('derives Agent Quality physical names and typed parameter paths centrally', () => {
    expect(agentQualityAuditTableName('dev')).toBe('adlytics-dev-agent-quality-audit');
    expect(agentQualityAggregateTableName('dev')).toBe('adlytics-dev-agent-quality-aggregate');
    expect(agentQualityProcessorFunctionName('dev')).toBe('adlytics-dev-AgentQualityProcessor');
    expect(paramPath('dev', 'agentQualityAuditTableName')).toBe(
      '/adlytics/dev/agent_quality/audit-table',
    );
  });

  it('derives each ADR-080 record table and every fixed writer and reader role name', () => {
    expect(agentQualityFindingsTableName('dev')).toBe('adlytics-dev-agent-quality-findings');
    expect(evaluationDeliveriesTableName('dev')).toBe('adlytics-dev-evaluation-deliveries');
    expect(evaluationWriterReceiptsTableName('dev')).toBe(
      'adlytics-dev-evaluation-writer-receipts',
    );
    expect(evaluationVerdictsTableName('dev')).toBe('adlytics-dev-evaluation-verdicts');
    const roles = [
      agentQualityProcessorRoleName,
      evaluationOutboxDispatcherRoleName,
      evaluationWriterRoleName,
      evaluationResultIngressRoleName,
      evaluationResultLinkerRoleName,
      qualityReviewFunctionRoleName,
    ];
    expect(roles.map((role) => role('dev'))).toEqual([
      'adlytics-dev-agent-quality-processor',
      'adlytics-dev-evaluation-outbox-dispatcher',
      'adlytics-dev-evaluation-writer',
      'adlytics-dev-evaluation-result-ingress',
      'adlytics-dev-evaluation-result-linker',
      'adlytics-dev-quality-review',
    ]);
    // IAM role names are at most 64 characters; the longest legal environment must still fit.
    const longest = 'a'.repeat(ENV_NAME_MAX_LENGTH);
    for (const role of roles) expect(role(longest).length).toBeLessThanOrEqual(64);
  });
  it('names the one review function and publishes the public review client (ADR-078)', () => {
    expect(qualityReviewFunctionName('dev')).toBe('adlytics-dev-QualityReview');
    expect(qualityReviewFunctionName('a'.repeat(ENV_NAME_MAX_LENGTH)).length).toBeLessThanOrEqual(64);
    expect(PARAMS.userPoolPublicClientId).toStrictEqual({
      area: 'auth',
      name: 'public-client-id',
      producedBy: 'AuthStack',
    });
    expect(paramPath('dev', 'userPoolPublicClientId')).toBe('/adlytics/dev/auth/public-client-id');
  });
  it('offers no authorizer result cache setting', () => {
    // Every caller presents the same identity source, the origin-verify header (ADR-078), so a
    // cached result would be shared across callers; there is nothing a TTL could safely enable.
    expect(defaultConfig('dev').auth).not.toHaveProperty('authorizerCacheSeconds');
  });
  it('validates at every point the name becomes an identifier', () => {
    // Not only in getConfig. A hand-built AppConfig — the stack tests do exactly
    // this — reaches these two without passing the gate, and a malformed name must
    // fail at synth rather than produce an invalid path or an unusable stack.
    expect(() => paramPath('Dev', 'apiId')).toThrow(/Invalid environment name/);
    expect(() => clientSecretName('my env')).toThrow(/Invalid environment name/);
  });
});

describe('what this repository ships', () => {
  /**
   * The assertions that stop a bring-up leaving working local values behind.
   *
   * Both defects this replaces were committed post-deploy state, not mistakes of
   * reasoning: `apiOriginAttached: true` and a real `loginDomainPrefix`. Each broke
   * a customer's first deploy — one with an opaque CloudFormation parameter error
   * for an SSM parameter that does not exist yet, the other with a collision
   * against this project's live login domain, which is globally unique.
   *
   * If one of these fails, a concrete value has been committed. Do not update the
   * expectation.
   */
  it('ships one stream retry bound for every environment: 100 attempts, 1 h (ADR-133)', () => {
    withoutCdkEnv(() => {
      for (const envName of ['dev', 'prod']) {
        expect(defaultConfig(envName).asyncFailureBounds).toStrictEqual({
          streamMaxRetryAttempts: 100,
          streamMaxRecordAgeSeconds: 3600,
        });
      }
    });
  });

  it('ships the static Athena start budget with both limits read from the contract (S1, F4)', () => {
    withoutCdkEnv(() => {
      const shipped = defaultConfig('dev');
      expect(shipped.athenaStartBudget).toStrictEqual({
        bakerStartCap: 10,
        insightStartCap: 4,
        batchAthenaBudget: ATHENA_START_BUDGET.batch_athena_budget,
        chatReserveActiveDml: ATHENA_START_BUDGET.chat_reserve_active_dml,
      });
      expect(ATHENA_START_BUDGET).toStrictEqual({
        active_dml_quota: 200,
        batch_athena_budget: 41,
        chat_reserve_active_dml: 20,
      });
      // The CHAT fan-out width is a separate knob that no batch budget reads, and it has no
      // shipped fallback (D7): without the environment it is the typed unset `NaN`.
      expect(shipped.athenaQueryBatchMaxConcurrency).toBeNaN();
    });
  });
  it('takes the general Athena query-batch admission from the environment, explicit 8 (D7)', () => {
    withDeploymentEnv(CORE_ENVIRONMENT_SUPPLIED, () => {
      const config = getConfig('dev');
      expect(config.athenaQueryBatchMaxConcurrency).toBe(8);
      // The value never moves the static start budget or insight precompute's 2 x 4.
      expect(config.athenaStartBudget).toStrictEqual({
        bakerStartCap: 10,
        insightStartCap: 4,
        batchAthenaBudget: 41,
        chatReserveActiveDml: 20,
      });
    });
  });
  it('accepts a legal non-8 admission inside 1 to the CHAT reserve', () => {
    for (const legal of ['1', '3', '20']) {
      withDeploymentEnv(
        { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: legal },
        () => {
          const config = getConfig('dev');
          expect(config.athenaQueryBatchMaxConcurrency).toBe(Number(legal));
          expect(config.athenaStartBudget.insightStartCap).toBe(4);
          expect(config.athenaStartBudget.bakerStartCap).toBe(10);
        },
      );
    }
  });
  it('refuses to synthesise without the Athena query-batch admission, and names the variable', () => {
    for (const absent of [undefined, '', '  ']) {
      const values: Record<string, string> = { ...CORE_ENVIRONMENT_SUPPLIED };
      delete values.ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY;
      if (absent !== undefined) values.ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY = absent;
      withDeploymentEnv(values, () => {
        const error = captureIncomplete('dev');
        expect(error.problems).toEqual([{ path: 'athenaQueryBatchMaxConcurrency', kind: 'unset' }]);
        expect(error.format()).toContain('ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY');
      });
    }
  });
  it('refuses an Athena query-batch admission outside a base-10 whole number from 1 to 20', () => {
    for (const malformed of ['0', '-1', '+8', '8.0', '8.5', '1e1', '08', '0x8', 'eight', '21', ' 8']) {
      withDeploymentEnv(
        { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY: malformed },
        () => {
          expect(() => defaultConfig('dev')).toThrow(
            /ADLYTICS_ATHENA_QUERY_BATCH_MAX_CONCURRENCY must be a whole number from 1 to 20/,
          );
        },
      );
    }
  });

  it('ships the token-service limits and a turn-length Harness token (ADR-077 §2–§3)', () => {
    withoutCdkEnv(() => {
      const shipped = defaultConfig('dev');
      expect(shipped.tokenService).toStrictEqual({
        clockSkewSeconds: 30,
        publicRouteThrottle: {
          discovery: { rateLimit: 10, burstLimit: 20 },
          jwks: { rateLimit: 10, burstLimit: 20 },
          token: { rateLimit: 20, burstLimit: 40 },
        },
        wafRateLimitPerFiveMinutes: 1000,
      });
      // One turn: the 180 s deep-dive budget plus the route's 30 s.
      expect(conversationFunctionTimeoutSeconds(shipped)).toBe(210);
      // The minimum-lifetime refusal is gone, not merely unused.
      expect(Object.keys(shipped.agent)).not.toContain('harnessTokenFreshnessBufferSeconds');
    });
  });

  it('ships every per-deployment value unset', () => {
    withoutCdkEnv(() => {
      const shipped = defaultConfig('dev');
      expect(shipped.account).toBe(UNSET);
      expect(shipped.region).toBe(UNSET);
      expect(shipped.auth.loginDomainPrefix).toBe(UNSET);
      expect(shipped.landing).toEqual({ enabled: false });
      // ADR-081 §5: no retention ships. NaN is the typed UNSET for a required number.
      expect(shipped.receiptRetentionDays).toBeNaN();
    });
  });

  it('ships the same unset values under any name', () => {
    // The values are not a per-name profile. A customer deploying 'acme-staging'
    // gets exactly what a customer deploying 'prod' gets.
    withoutCdkEnv(() => {
      for (const envName of ['dev', 'prod', 'acme-staging']) {
        const shipped = defaultConfig(envName);
        expect(shipped.account).toBe(UNSET);
        expect(shipped.region).toBe(UNSET);
        expect(shipped.auth.loginDomainPrefix).toBe(UNSET);
        expect(shipped.landing).toEqual({ enabled: false });
      }
    });
  });

  it('accepts a complete core configuration without producer credentials', () => {
    withDeploymentEnv(CORE_ENVIRONMENT_SUPPLIED, () => {
      expect(getConfig('dev').landing).toEqual({ enabled: false });
    });
  });

  it('takes the receipt retention in whole days from the environment (ADR-081 §5)', () => {
    withDeploymentEnv(CORE_ENVIRONMENT_SUPPLIED, () => {
      expect(getConfig('dev').receiptRetentionDays).toBe(90);
    });
  });

  it('refuses to synthesise without a receipt retention, and names the variable', () => {
    // No default: the receipt carries the principal, so how long it is kept is the operator's
    // stated decision. Empty counts as absent, as it does for every UNSET string.
    for (const absent of [undefined, '', '  ']) {
      const values: Record<string, string> = { ...CORE_ENVIRONMENT_SUPPLIED };
      delete values.ADLYTICS_RECEIPT_RETENTION_DAYS;
      if (absent !== undefined) values.ADLYTICS_RECEIPT_RETENTION_DAYS = absent;
      withDeploymentEnv(values, () => {
        const error = captureIncomplete('dev');
        expect(error.problems).toEqual([{ path: 'receiptRetentionDays', kind: 'unset' }]);
        expect(error.format()).toContain('ADLYTICS_RECEIPT_RETENTION_DAYS');
      });
    }
  });

  it('refuses a receipt retention that is not a positive whole number of days', () => {
    for (const malformed of ['0', '-1', '90.5', '1e2', '090', 'ninety', '1000000']) {
      withDeploymentEnv(
        { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_RECEIPT_RETENTION_DAYS: malformed },
        () => {
          expect(() => defaultConfig('dev')).toThrow(
            /ADLYTICS_RECEIPT_RETENTION_DAYS must be a positive whole number of days/,
          );
        },
      );
    }
  });

  it('keeps producer onboarding disabled unless the exact flag is true', () => {
    withDeploymentEnv(
      {
        ...CORE_ENVIRONMENT_SUPPLIED,
        ADLYTICS_LANDING_PIPELINE_ROLE_ARN:
          'arn:aws:iam::444455556666:role/stale-source-pipeline',
        ADLYTICS_LANDING_EXTERNAL_ID: 'stale-external-id',
      },
      () => {
        expect(getConfig('dev').landing).toEqual({ enabled: false });
      },
    );
  });

  it('refuses malformed producer onboarding flags', () => {
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_PRODUCER_ONBOARDING_ENABLED: 'yes' },
      () => {
        expect(() => defaultConfig('dev')).toThrow(
          /ADLYTICS_PRODUCER_ONBOARDING_ENABLED must be exactly "true" or "false"/,
        );
      },
    );
  });

  it('requires both producer credentials only when onboarding is enabled', () => {
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_PRODUCER_ONBOARDING_ENABLED: 'true' },
      () => {
        expect(captureIncomplete('dev').problems).toEqual([
          { path: 'landing.pipelineRoleArn', kind: 'unset' },
          { path: 'landing.externalId', kind: 'unset' },
        ]);
      },
    );
  });

  it('refuses to synthesise without the long-term memory override model, and names it (ADR-147)', () => {
    const values: Record<string, string> = { ...CORE_ENVIRONMENT_SUPPLIED };
    delete values.ADLYTICS_MEMORY_MODEL_ID;
    withDeploymentEnv(values, () => {
      const error = captureIncomplete('dev');
      expect(error.problems).toStrictEqual([{ path: 'agent.memoryModelId', kind: 'unset' }]);
      expect(error.format().split('\n')).toContain(
        '  agent.memoryModelId   comes from ADLYTICS_MEMORY_MODEL_ID; the long-term memory ' +
          'override model, no fallback',
      );
    });
  });

  it('never falls the memory override model back to a chat model (ADR-147)', () => {
    // A fallback would silently move memory extraction onto whatever model the chat moves to.
    const values: Record<string, string> = { ...CORE_ENVIRONMENT_SUPPLIED };
    delete values.ADLYTICS_MEMORY_MODEL_ID;
    expect(values.ADLYTICS_DEEP_DIVE_MODEL_ID).toBe('test.deep-dive-model-v1:0');
    withDeploymentEnv(values, () => {
      expect(captureIncomplete('dev').problems).toStrictEqual([
        { path: 'agent.memoryModelId', kind: 'unset' },
      ]);
    });
  });

  it('reads the memory override model from its own variable (ADR-147)', () => {
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_MEMORY_MODEL_ID: 'us.test.memory-model-v1:0' },
      () => {
        expect(getConfig('dev').agent.memoryModelId).toBe('us.test.memory-model-v1:0');
      },
    );
  });

  it('gives the insight models their own override, falling back to the agent values', () => {
    // ADR-076 Phase 1: absent an override, the insight seam equals the agent seam — the
    // pre-ADR wiring exactly. With the override, the two families diverge and the chat
    // agent's configuration is untouched.
    withDeploymentEnv(CORE_ENVIRONMENT_SUPPLIED, () => {
      const config = getConfig('dev');
      expect(config.insight.quickModelId).toBe(config.agent.quickChat.modelId);
      expect(config.insight.deepModelId).toBe(config.agent.deepDive.modelId);
    });
    withDeploymentEnv(
      {
        ...CORE_ENVIRONMENT_SUPPLIED,
        ADLYTICS_INSIGHT_QUICK_MODEL_ID: 'test.insight-quick-model-v1:0',
        ADLYTICS_INSIGHT_DEEP_MODEL_ID: 'test.insight-deep-model-v1:0',
      },
      () => {
        const config = getConfig('dev');
        expect(config.insight.quickModelId).toBe('test.insight-quick-model-v1:0');
        expect(config.insight.deepModelId).toBe('test.insight-deep-model-v1:0');
        expect(config.agent.quickChat.modelId).toBe(CORE_ENVIRONMENT_SUPPLIED.ADLYTICS_QUICK_CHAT_MODEL_ID);
        expect(config.agent.deepDive.modelId).toBe(CORE_ENVIRONMENT_SUPPLIED.ADLYTICS_DEEP_DIVE_MODEL_ID);
      },
    );
  });

  it('ships online evaluation absent and inactive', () => {
    withDeploymentEnv(CORE_ENVIRONMENT_SUPPLIED, () => {
      expect(getConfig('dev').onlineEvaluation).toEqual({
        provisioned: false,
        samplingPercentage: 10,
        sessionTimeoutMinutes: 1,
      });
      expect(getConfig('dev').onlineEvaluationV4).toEqual({ executionRequested: false });
      expect(getConfig('dev')).not.toHaveProperty('onlineEvaluationV3');
      expect(getConfig('dev').evaluationInput).toEqual({ provisioned: false });
      expect(getConfig('dev').evaluationResultRetentionDays).toBe(logs.RetentionDays.TWO_WEEKS);
      expect(getConfig('dev').harnessRuntimeLogRetentionDays).toBe(90);
      expect(getConfig('dev').evaluationResultDeadlineSeconds).toBe(1800);
      expect(getConfig('dev').operationalStatus).toEqual({
        provisioned: false,
        stage1LifecycleReconcileBatchSize: 10,
        stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
      });
    });
  });

  it('provisions operational status only for dev with the exact boolean flag', () => {
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_OPERATIONAL_STATUS_PROVISIONED: 'true' },
      () => expect(getConfig('dev').operationalStatus).toEqual({
        provisioned: true,
        stage1LifecycleReconcileBatchSize: 10,
        stage1OutboxMaxConsecutiveFailedDeliveryAttempts: 3,
      }),
    );
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_OPERATIONAL_STATUS_PROVISIONED: 'true' },
      () => expect(() => defaultConfig('staging')).toThrow(/only for ENV=dev/),
    );
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_OPERATIONAL_STATUS_PROVISIONED: 'yes' },
      () => expect(() => defaultConfig('dev')).toThrow(/must be exactly/),
    );
  });

  it('provisions online evaluation only in dev and takes V4 execution from one dev-only flag (ADR-079)', () => {
    const provisioned = {
      ...CORE_ENVIRONMENT_SUPPLIED,
      ADLYTICS_TRANSACTION_SEARCH: 'true',
      ADLYTICS_EVALUATION_INPUT_PROVISIONED: 'true',
      ADLYTICS_ONLINE_EVALUATION_PROVISIONED: 'true',
    };
    withDeploymentEnv(provisioned, () => {
      expect(getConfig('dev').onlineEvaluation.provisioned).toBe(true);
      expect(getConfig('dev').onlineEvaluationV4).toEqual({ executionRequested: false });
    });
    withDeploymentEnv({ ...provisioned, ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED: 'true' }, () => {
      expect(getConfig('dev').onlineEvaluationV4).toEqual({ executionRequested: true });
    });
    withDeploymentEnv(
      provisioned,
      () => expect(() => defaultConfig('staging')).toThrow(/Online evaluation may be provisioned only for ENV=dev/),
    );
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED: 'true' },
      () => expect(() => defaultConfig('staging')).toThrow(/V4 execution may be requested only for ENV=dev/),
    );
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_ONLINE_EVALUATION_V4_REQUESTED: 'true' },
      () => expect(() => getConfig('dev')).toThrow(/V4 execution requires ADLYTICS_ONLINE_EVALUATION_PROVISIONED/),
    );
  });
  it('ignores the retired enablement, retention-confirmation, capture, and V3 flags (ADR-079)', () => {
    withDeploymentEnv(
      {
        ...CORE_ENVIRONMENT_SUPPLIED,
        ADLYTICS_ONLINE_EVALUATION_V3_REQUESTED: 'true',
        ADLYTICS_ONLINE_EVALUATION_V4_PROVISIONED: 'true',
        ADLYTICS_ONLINE_EVALUATION_V3_ENABLED: 'true',
        ADLYTICS_ONLINE_EVALUATION_V3_RETENTION_CONFIRMED: 'true',
        ADLYTICS_ONLINE_EVALUATION_V4_ENABLED: 'true',
        ADLYTICS_ONLINE_EVALUATION_V4_RETENTION_CONFIRMED: 'true',
        ADLYTICS_EVALUATION_CAPTURE_ENABLED: 'true',
      },
      () => {
        const config = defaultConfig('staging');
        expect(config.onlineEvaluation.provisioned).toBe(false);
        expect(config.onlineEvaluationV4).toEqual({ executionRequested: false });
        expect(config.evaluationInput).toEqual({ provisioned: false });
      },
    );
  });
  it('refuses malformed online evaluation flags and sampling', () => {
    withDeploymentEnv(
      { ...CORE_ENVIRONMENT_SUPPLIED, ADLYTICS_ONLINE_EVALUATION_PROVISIONED: 'yes' },
      () => expect(() => defaultConfig('dev')).toThrow(/must be exactly/),
    );
    withDeploymentEnv(
      {
        ...CORE_ENVIRONMENT_SUPPLIED,
        ADLYTICS_TRANSACTION_SEARCH: 'true',
        ADLYTICS_EVALUATION_INPUT_PROVISIONED: 'true',
        ADLYTICS_ONLINE_EVALUATION_PROVISIONED: 'true',
        ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT: '0',
      },
      () => expect(() => getConfig('dev')).toThrow(/non-zero whole sampling percentage/),
    );
    withDeploymentEnv(
      {
        ...CORE_ENVIRONMENT_SUPPLIED,
        ADLYTICS_ONLINE_EVALUATION_SAMPLE_PERCENT: '101',
      },
      () => expect(() => defaultConfig('dev')).toThrow(/whole number between 0 and 100/),
    );
  });

  it('ships the edge with neither origin attached', () => {
    // False is the only value that can ship. Each flag makes FrontendStack read a parameter that
    // no first deploy has yet created: the API id, and the conversation function's URL.
    const saved = { ...process.env };
    try {
      delete process.env.ADLYTICS_API_ORIGIN_ATTACHED;
      delete process.env.ADLYTICS_CONVERSATION_ORIGIN_ATTACHED;
      for (const envName of ['dev', 'prod', 'acme-staging']) {
        expect(defaultConfig(envName).edge.apiOriginAttached).toBe(false);
        expect(defaultConfig(envName).edge.conversationOriginAttached).toBe(false);
      }
    } finally {
      process.env = saved;
    }
  });

  it('attaches an edge origin only when the environment says so explicitly', () => {
    // The half that makes the two-pass safe. These used to be edited in this file and reverted,
    // and a missed revert leaves the committed value disagreeing with the live distribution —
    // where a redeploy silently *detaches* a working origin.
    const saved = { ...process.env };
    try {
      process.env.ADLYTICS_API_ORIGIN_ATTACHED = 'true';
      process.env.ADLYTICS_CONVERSATION_ORIGIN_ATTACHED = 'true';
      expect(defaultConfig('dev').edge.apiOriginAttached).toBe(true);
      expect(defaultConfig('dev').edge.conversationOriginAttached).toBe(true);

      // Refused rather than coerced, like every other flag: an operator who writes `yes` has a
      // wrong model of this, and accepting it teaches them the wrong thing about the rest.
      process.env.ADLYTICS_API_ORIGIN_ATTACHED = 'yes';
      expect(() => defaultConfig('dev')).toThrow(/must be exactly/);
    } finally {
      process.env = saved;
    }
  });

  it('takes the account and region from the deploying profile, never a literal', () => {
    // The CDK env vars are the account and region of the profile the CLI was
    // given, so --profile stays the single statement of where a deploy lands.
    const saved = { ...process.env };
    try {
      process.env.CDK_DEFAULT_ACCOUNT = '111122223333';
      process.env.CDK_DEFAULT_REGION = 'eu-west-1';
      const config = defaultConfig('dev');
      expect(config.account).toBe('111122223333');
      expect(config.region).toBe('eu-west-1');
    } finally {
      process.env = saved;
    }
  });

  it('ships the reviewed frozen Stage-1 ingestion contract with recovery enabled after the fence', () => {
    // The pre-switch fence shipped `stage1RecoveryEnabled: false`; the retained passing
    // `verify-pre-switch` report (write discipline §2.5) authorized the flip to true, which stays
    // true in every later handoff state.
    const ingestion = defaultConfig('dev').ingestion;
    expect(Object.isFrozen(ingestion)).toBe(true);
    expect(ingestion).toEqual({
      projectionHandoffState: 'QUEUED_ACTIVE',
      stage1RecoveryEnabled: true,
      // S6: dev ships the legacy reconcile enabled; only P6(d')'s own commit changes it.
      legacyPlanIntentReconcileEnabled: true,
      maxUnits: 15,
      maxAcceptedAttemptsPerUnit: 10,
      maxAcceptedAttemptsPerPlan: 32,
      maxGroupMembers: 4,
      maxPointersPerTable: 37,
      maxObjectRefsPerUnit: 37,
      maxRowsPerUnit: 50000,
      maxRowsPerDelivery: 20000,
      athenaMaxStatementBytes: 200000,
      maxEncodedIdentityBytes: 128,
      maxPartitionIdentityBytes: 256,
      tableWriterTimeoutSeconds: 900,
      tableWriterMemoryMiB: 2048,
      tableWriterCleanupReserveMs: 60000,
      tableWriterConcurrency: 8,
      tableLeaseDurationSeconds: 930,
      semanticWriteVisibilityMarginSeconds: 60,
      tableAttemptReconcileBatchSize: 10,
      tableAttemptReconcileBaseBackoffSeconds: 60,
      tableAttemptReconcileMaxBackoffSeconds: 900,
      planIntentReconcileBatchSize: 50,
      planIntentReconcileBaseBackoffSeconds: 60,
      planIntentReconcileMaxBackoffSeconds: 900,
      drainObservationIntervalSeconds: 300,
    });
  });

  it('pins every Lambda to arm64', () => {
    // ADR-005. Held in config so no individual stack can diverge.
    expect(defaultConfig('dev').lambdaArchitecture).toBe('arm64');
    expect(defaultConfig('acme-staging').lambdaArchitecture).toBe('arm64');
  });

  it('pins the Python runtime to 3.14', () => {
    expect(defaultConfig('dev').pythonRuntime).toBe('python3.14');
    expect(defaultConfig('acme-staging').pythonRuntime).toBe('python3.14');
  });
});

describe('the completeness gate', () => {
  it('refuses a configuration whose values are still placeholders', () => {
    // A placeholder must never synthesise: it would deploy somewhere unintended
    // and look fine doing it. True of every name until a human fills it in, which
    // is why `make synth ENV=dev` now fails.
    withoutCdkEnv(() => {
      for (const envName of ['dev', 'prod', 'sandbox']) {
        expect(() => getConfig(envName)).toThrow(/is not usable/);
      }
    });
  });

  it('names every gap at once rather than the first', () => {
    // Discovering the next missing value on the following synth turns one
    // correction into an afternoon of them.
    withoutCdkEnv(() => {
      try {
        getConfig('sandbox');
        throw new Error('expected getConfig to throw');
      } catch (error) {
        const message = (error as Error).message;
        expect(message).toContain('account is unset');
        expect(message).toContain('region is unset');
        expect(message).toContain('auth.loginDomainPrefix is unset');
        expect(message).toContain('receiptRetentionDays is unset');
      }
    });
  });

  it('rejects a localhost value even when it is present', () => {
    // The failure this project has hit repeatedly: a localhost value is present,
    // so nothing reads as missing, and the deploy succeeds pointing at nothing.
    // Injected through the account, which is the one string in the tree a caller
    // can influence — the guard walks every string regardless of key.
    const saved = { ...process.env };
    try {
      process.env.CDK_DEFAULT_ACCOUNT = 'localhost';
      expect(() => getConfig('dev')).toThrow(/localhost/);
    } finally {
      process.env = saved;
    }
  });

  it('treats cookieDomain as legitimately empty under host_only', () => {
    // Host-only cookies carry no Domain attribute at all, which is required on an
    // AWS-provided hostname since cloudfront.net is a public suffix. The exemption
    // is stated as a condition rather than a permanent exception, so it must not
    // appear as a gap while that condition holds.
    withoutCdkEnv(() => {
      expect(defaultConfig('dev').auth.cookieScope).toBe('host_only');
      const error = captureIncomplete('dev');
      expect(error.problems.map((p) => p.path)).not.toContain('auth.cookieDomain');
    });
  });
});

/** Throw-and-capture, typed, so a test can assert on the structure and not a string. */
function captureIncomplete(envName: string): ConfigurationIncompleteError {
  try {
    getConfig(envName);
  } catch (error) {
    if (error instanceof ConfigurationIncompleteError) {
      return error;
    }
    throw error;
  }
  throw new Error(`expected getConfig(${envName}) to refuse an incomplete configuration`);
}

describe('the completeness gate reads as a guard, not a crash', () => {
  it('throws a type the entrypoint can distinguish from a defect', () => {
    // The whole point of the named type. An incomplete configuration is an expected
    // outcome with a known fix and prints no stack trace; anything else thrown
    // during synth is a bug and keeps its trace. Before the split, a fresh checkout
    // reported a missing login prefix as an uncaught exception.
    withoutCdkEnv(() => {
      expect(captureIncomplete('dev')).toBeInstanceOf(ConfigurationIncompleteError);
    });
  });

  it('classifies an unfilled placeholder as a fresh checkout', () => {
    withoutCdkEnv(() => {
      const error = captureIncomplete('dev');
      expect(error.isFreshCheckout).toBe(true);
      expect(error.format()).toContain('expected on a fresh checkout');
    });
  });

  it('does not call a localhost value expected', () => {
    // A placeholder is normal on a fresh clone; a value that resolved to localhost
    // is a fault. Presenting them alike would teach an operator to ignore the one
    // that matters.
    const saved = { ...process.env };
    try {
      process.env.CDK_DEFAULT_ACCOUNT = 'localhost';
      const error = captureIncomplete('dev');
      expect(error.isFreshCheckout).toBe(false);
      expect(error.format()).not.toContain('expected on a fresh checkout');
      expect(error.format()).toContain('did not resolve');
    } finally {
      process.env = saved;
    }
  });

  it('states that the list is complete, and where to fix it', () => {
    withoutCdkEnv(() => {
      const error = captureIncomplete('sandbox');
      const rendered = error.format();
      expect(error.problems.length).toBeGreaterThan(1);
      expect(rendered).toContain('are listed above, not just the first');
      expect(rendered).toContain('infra/lib/config/index.ts');
      expect(rendered).toContain('DEPLOY_ORDER in infra/lib/config/index.ts');
      expect(rendered).toContain('Nothing was synthesised');
    });
  });

  /**
   * Every value config reads from the environment, except the one a case wants left missing.
   *
   * Both tests below assert message formatting for a state with exactly one problem, so every
   * other environment-sourced value has to be supplied. Listed in one place because three
   * separate additions to `AgentConfig` each broke both tests: the premise is deliberate and
   * worth keeping, but reconstructing it by hand at two call sites is what kept breaking.
   *
   * A new environment-sourced value belongs in this list. Leaving it out fails both tests with a
   * count of two, which names the omission rather than hiding it.
   */
  const ENVIRONMENT_SUPPLIED: Readonly<Record<string, string>> = {
    ...CORE_ENVIRONMENT_SUPPLIED,
    ADLYTICS_PRODUCER_ONBOARDING_ENABLED: 'true',
    ADLYTICS_LANDING_PIPELINE_ROLE_ARN:
      'arn:aws:iam::444455556666:role/adlytics-source-pipeline',
    ADLYTICS_LANDING_EXTERNAL_ID: 'test-deployment-external-id',
  };

  /** Run `body` with every environment value present except `omitted`. */
  function withOneProblem(omitted: string, body: () => void): void {
    const saved = { ...process.env };
    try {
      for (const [key, value] of Object.entries(ENVIRONMENT_SUPPLIED)) {
        if (key !== omitted) process.env[key] = value;
      }
      delete process.env[omitted];
      body();
    } finally {
      process.env = saved;
    }
  }

  it('accepts only one exact cross-account IAM role principal', () => {
    const saved = { ...process.env };
    try {
      Object.assign(process.env, ENVIRONMENT_SUPPLIED);
      const invalid = [
        'arn:aws:iam::444455556666:root',
        'arn:aws:sts::444455556666:assumed-role/source/session',
        'arn:aws:iam::444455556666:role/*',
        'arn:aws:iam::111122223333:role/same-account',
      ];
      for (const principal of invalid) {
        process.env.ADLYTICS_LANDING_PIPELINE_ROLE_ARN = principal;
        expect(() => getConfig('dev')).toThrow(/exact IAM role|different AWS account/);
      }
      process.env.ADLYTICS_LANDING_PIPELINE_ROLE_ARN =
        'arn:aws:iam::444455556666:role/path/source-pipeline';
      expect(getConfig('dev').landing).toEqual({
        enabled: true,
        pipelineRoleArn: 'arn:aws:iam::444455556666:role/path/source-pipeline',
        externalId: 'test-deployment-external-id',
      });
    } finally {
      process.env = saved;
    }
  });

  it('omits the completeness note when there is only one problem', () => {
    // "All 1 are listed above" is the kind of line that makes software read as
    // unfinished, in the message a new operator sees first.
    withOneProblem('ADLYTICS_LOGIN_DOMAIN_PREFIX', () => {
      const error = captureIncomplete('dev');
      expect(error.problems).toHaveLength(1);
      expect(error.format()).not.toContain('listed above');
    });
  });

  it('pluralises the message it carries', () => {
    // Cosmetic, and it was "1 problem(s)", which reads as unfinished software in
    // the one message a new operator is most likely to see first.
    withOneProblem('ADLYTICS_LOGIN_DOMAIN_PREFIX', () => {
      const one = captureIncomplete('dev');
      expect(one.problems).toHaveLength(1);
      expect(one.message).toContain('1 problem:');
      expect(one.message).not.toContain('problems');
    });
    // Two or more, whatever the count: what is asserted is the plural form, and pinning the exact
    // number would make every new environment-sourced value edit this test for no reason.
    withoutCdkEnv(() => {
      expect(captureIncomplete('dev').message).toMatch(/\b(?:[2-9]|[1-9]\d+) problems:/);
    });
  });
});

describe('cross-stack parameter contract (ADR-012)', () => {
  it('builds paths from the complete projected layout', () => {
    expect(PARAMETER_PATH_SEGMENTS).toEqual([
      'adlytics',
      '{environment}',
      '{area}',
      '{name}',
    ]);
    expect(paramPath('dev', 'apiBaseUrl')).toBe('/adlytics/dev/api/base-url');
    expect(paramPath('acme-staging', 'userPoolId')).toBe(
      '/adlytics/acme-staging/auth/user-pool-id',
    );
  });

  it('separates environments so one account can hold several', () => {
    expect(paramPath('dev', 'apiBaseUrl')).not.toBe(paramPath('prod', 'apiBaseUrl'));
    expect(paramPath('dev', 'apiBaseUrl')).not.toBe(paramPath('sandbox', 'apiBaseUrl'));
    expect(clientSecretName('dev')).not.toBe(clientSecretName('sandbox'));
  });

  it('derives Semantic V2 state, workgroup, and published paths from one vocabulary', () => {
    expect(semanticV2StateTableName('dev', 'publications')).toBe(
      'adlytics-dev-v2-semantic-publications',
    );
    expect(semanticV2AthenaWorkgroupName('dev', 'interactive')).toBe(
      'adlytics-dev-v2-interactive',
    );
    expect(semanticV2AthenaWorkgroupName('prod', 'batch')).toBe('adlytics-prod-v2-batch');
    expect(paramPath('dev', 'semanticV2PublicationsTableName')).toBe(
      '/adlytics/dev/data/v2/semantic-publications-table',
    );
    expect(paramPath('dev', 'semanticV2AthenaWorkgroupInteractive')).toBe(
      '/adlytics/dev/data/v2/athena-workgroup-interactive',
    );
    expect(paramPath('dev', 'semanticV2AthenaWorkgroupBatch')).toBe(
      '/adlytics/dev/data/v2/athena-workgroup-batch',
    );
    expect(semanticV2AthenaWorkgroupName('dev', 'bake')).toBe('adlytics-dev-v2-bake');
    expect(semanticV2AthenaWorkgroupName('dev', 'insight-precompute')).toBe(
      'adlytics-dev-v2-insight-precompute',
    );
    expect(paramPath('dev', 'semanticV2AthenaWorkgroupBake')).toBe(
      '/adlytics/dev/data/v2/athena-workgroup-bake',
    );
    expect(paramPath('dev', 'semanticV2AthenaWorkgroupInsightPrecompute')).toBe(
      '/adlytics/dev/data/v2/athena-workgroup-insight-precompute',
    );
  });

  it('derives operational status names and typed parameter paths centrally', () => {
    expect(operationalStatusProjectionTableName('dev')).toBe('adlytics-dev-operational-status');
    expect(operationalStatusQueueName('dev')).toBe('adlytics-dev-operational-status.fifo');
    expect(operationalStatusQueueName('dev', true)).toBe(
      'adlytics-dev-operational-status-dlq.fifo',
    );
    expect(operationalStatusFunctionName('dev', 'Projector')).toBe(
      'adlytics-dev-OperationalStatusProjector',
    );
    expect(paramPath('dev', 'operationalStatusProjectionTableName')).toBe(
      '/adlytics/dev/operations/status-projection-table-name',
    );
    expect(paramPath('dev', 'operationalStatusQueueUrl')).toBe(
      '/adlytics/dev/operations/status-queue-url',
    );
  });

  it('derives retained agent table names and parameter paths together', () => {
    expect(pageContextTableName('dev')).toBe('adlytics-dev-page-context');
    // Not the retired `_harness` name: the custom resource creates before CloudFormation deletes
    // the retired CfnHarness, so an equal name would collide (ADR-077, amended 2026-09-26).
    expect(harnessName('acme-staging')).toBe('adlytics_acme_staging_agent');
    expect(harnessName('dev')).not.toBe('adlytics_dev_harness');
    expect(paramPath('dev', 'harnessArn')).toBe('/adlytics/dev/agent/harness-arn');
    expect(paramPath('dev', 'pageContextTableName')).toBe(
      '/adlytics/dev/agent/page-context-table',
    );
  });
  it('names each evaluation family\'s gate resources, outside the reserved result prefix (ADR-079)', () => {
    expect(EVALUATION_FAMILIES).toEqual(['v4']);
    // The dispatcher's reader refuses a family Python does not hold current, at import (ADR-079), so
    // the family list infra renders must equal `CURRENT_EVALUATION_FAMILIES` in release.py.
    const release = fs.readFileSync(
      nodePath.join(__dirname, '..', '..', 'src', 'domain', 'evaluation', 'release.py'),
      'utf-8',
    );
    const current = /^CURRENT_EVALUATION_FAMILIES:[^=]*=\s*\(([^)]*)\)/m.exec(release);
    expect(current).not.toBeNull();
    const pythonFamilies = [...current![1]!.matchAll(/EvaluationFamilyV1\.V(\d+)/g)].map((m) => `v${m[1]}`);
    expect(pythonFamilies).toEqual([...EVALUATION_FAMILIES]);
    expect(evaluationResultLogGroupName('acme-staging', 'v4')).toBe('/adlytics/acme-staging/evaluation/results-v4');
    expect(evaluationReleaseParameterName('acme-staging', 'v4')).toBe('/adlytics/acme-staging/evaluation/release-v4');
    // New names: the retired CloudFormation configurations were `_helpfulness_v3` and `_quality_v4`.
    expect(onlineEvaluationGateName('acme-staging', 'v4')).toBe('adlytics_acme_staging_released_v4');
    expect(evaluationGateProviderFunctionName('acme-staging', 'v4')).toBe('adlytics-acme-staging-EvaluationGate-v4');
    expect(evaluationGateProviderRoleName('acme-staging', 'v4')).toBe('adlytics-acme-staging-evaluation-gate-v4');
    expect(evaluationExecutionRoleName('acme-staging', 'v4')).toBe('adlytics-acme-staging-evaluation-execution-v4');
    expect(evaluationResultSubscriptionName('acme-staging', 'v4')).toBe(
      'adlytics-acme-staging-evaluation-result-ingress-v4',
    );
    expect(PARAMS).not.toHaveProperty('evaluationV3ResultLogGroupName');
    expect(PARAMS.evaluationV4ResultLogGroupName).toEqual({
      area: 'evaluation', name: 'result-log-group-v4', producedBy: 'EvaluationEvidenceStack',
    });
    // Every name fits its service limit at the longest environment name.
    const longest = 'a'.repeat(ENV_NAME_MAX_LENGTH);
    for (const family of EVALUATION_FAMILIES) {
      expect(onlineEvaluationGateName(longest, family)).toMatch(/^[a-zA-Z][a-zA-Z0-9_]{0,47}$/);
      expect(evaluationGateProviderFunctionName(longest, family).length).toBeLessThanOrEqual(64);
      expect(evaluationGateProviderRoleName(longest, family).length).toBeLessThanOrEqual(64);
      expect(evaluationExecutionRoleName(longest, family).length).toBeLessThanOrEqual(64);
      expect(evaluationResultLogGroupName(longest, family).startsWith('/aws/')).toBe(false);
    }
    expect(() => evaluationResultLogGroupName('Bad_Name', 'v4')).toThrow(/Invalid environment name/);
  });
  it('deploys the result link after the evidence and dispatch stacks and before the gate (ADR-079)', () => {
    const at = (name: (typeof DEPLOY_ORDER)[number]): number => DEPLOY_ORDER.indexOf(name);
    expect(at('EvaluationResultLinkV3Stack')).toBe(at('EvaluationOutboxDispatchStack') + 1);
    expect(at('EvaluationEvidenceStack')).toBeLessThan(at('EvaluationResultLinkV3Stack'));
    expect(at('EvaluationResultLinkV3Stack')).toBeLessThan(at('EvaluationV4Stack'));
    expect(DEPLOY_ORDER as readonly string[]).not.toContain('EvaluationV3Stack');
  });

  it('produces a unique path for every registered key', () => {
    // Two keys colliding would mean one stack silently overwriting another's value.
    const keys = Object.keys(PARAMS) as ParamKey[];
    const paths = keys.map((k) => paramPath('dev', k));
    expect(new Set(paths).size).toBe(keys.length);
  });

  it('names a producing stack for every parameter', () => {
    // Without this the deploy order stops being derivable from the registry.
    for (const spec of Object.values(PARAMS)) {
      expect(DEPLOY_ORDER).toContain(spec.producedBy);
    }
  });

  it('places the semantic writer after storage and before canonical readers', () => {
    expect(DEPLOY_ORDER.indexOf('DataV2Stack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('IngestionStack'),
    );
    expect(DEPLOY_ORDER.indexOf('IngestionStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('AgentStack'),
    );
    expect(DEPLOY_ORDER.indexOf('IngestionStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('ApiStack'),
    );
  });

  it('places the operational store before its processor and API reader', () => {
    expect(PARAMS.semanticV2AttemptsStreamArn.producedBy).toBe('DataV2Stack');
    expect(PARAMS.operationalStatusProjectionTableName.producedBy).toBe(
      'OperationalStatusStoreStack',
    );
    expect(PARAMS.operationalStatusQueueUrl.producedBy).toBe(
      'OperationalStatusProcessorStack',
    );
    for (const [before, after] of [
      ['DataV2Stack', 'OperationalStatusProcessorStack'],
      ['OperationalStatusStoreStack', 'OperationalStatusProcessorStack'],
      ['OperationalStatusStoreStack', 'ApiStack'],
    ] as const) {
      expect(DEPLOY_ORDER.indexOf(before)).toBeLessThan(DEPLOY_ORDER.indexOf(after));
    }
  });

  it('places the page-context producer before its API consumer', () => {
    expect(PARAMS.pageContextTableName.producedBy).toBe('AgentStack');
    expect(DEPLOY_ORDER.indexOf(PARAMS.pageContextTableName.producedBy)).toBeLessThan(
      DEPLOY_ORDER.indexOf('ApiStack'),
    );
  });

  it('places the primary Harness after its Gateway and before the Conversation API', () => {
    expect(PARAMS.harnessArn.producedBy).toBe('HarnessStack');
    expect(DEPLOY_ORDER.indexOf('AgentStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('HarnessStack'),
    );
    expect(DEPLOY_ORDER.indexOf('HarnessStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('ApiStack'),
    );
    expect(DEPLOY_ORDER.indexOf('ApiStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('EvaluationV4Stack'),
    );
  });

  it('orders retained evaluation evidence, disposable transport, the result link, API, then the gate', () => {
    expect(PARAMS.evaluationInputLogGroupName.producedBy).toBe('EvaluationEvidenceStack');
    expect(PARAMS.evaluationOutboxTableName.producedBy).toBe('EvaluationEvidenceStack');
    expect(PARAMS.evaluationOutboxTableArn.producedBy).toBe('EvaluationEvidenceStack');
    expect(PARAMS.evaluationInputQueueUrl.producedBy).toBe('EvaluationInputStack');
    expect(PARAMS.evaluationInputQueueArn.producedBy).toBe('EvaluationInputStack');
    expect(PARAMS.evaluationInputServiceName.producedBy).toBe('EvaluationInputStack');
    for (const [before, after] of [
      ['EvaluationEvidenceStack', 'EvaluationInputStack'],
      ['EvaluationInputStack', 'EvaluationOutboxDispatchStack'],
      ['EvaluationOutboxDispatchStack', 'EvaluationResultLinkV3Stack'],
      ['EvaluationResultLinkV3Stack', 'ApiStack'],
      ['ApiStack', 'EvaluationV4Stack'],
    ] as const) {
      expect(DEPLOY_ORDER.indexOf(before)).toBeLessThan(DEPLOY_ORDER.indexOf(after));
    }
  });

  it('places record tamper detection after every stack whose parameters it reads (ADR-080 §4)', () => {
    const reads = [
      'agentQualityAuditStreamArn',
      'agentQualityFindingsStreamArn',
      'evaluationDeliveriesStreamArn',
      'evaluationWriterReceiptsStreamArn',
      'evaluationVerdictsStreamArn',
      'ingestionAlarmTopicArn',
    ] as const;
    for (const key of reads) {
      expect(DEPLOY_ORDER.indexOf(PARAMS[key].producedBy))
        .toBeLessThan(DEPLOY_ORDER.indexOf('RecordIntegrityStack'));
    }
  });

  it('places retained publisher state before its disposable transport and consumers', () => {
    for (const [before, after] of [
      ['DataV2Stack', 'PublisherStack'],
      ['PublisherArtifactStoreStack', 'PublisherStack'],
      ['PublisherStack', 'ApiStack'],
    ] as const) {
      expect(DEPLOY_ORDER.indexOf(before)).toBeLessThan(DEPLOY_ORDER.indexOf(after));
    }
    for (const key of [
      'publisherArtifactBucketName',
      'publisherArtifactPointerTableName',
      'publisherArtifactPointerTableArn',
    ] as const) {
      expect(PARAMS[key].producedBy).toBe('PublisherArtifactStoreStack');
    }
    for (const key of ['publisherBakeQueueUrl', 'publisherBakeQueueArn'] as const) {
      expect(PARAMS[key].producedBy).toBe('PublisherStack');
    }
  });

  it('keeps the deployed guardrail name stable, because a rename replaces the resource', () => {
    expect(agentRouteGuardrailName('dev')).toBe('adlytics-dev-conversation-agent');
  });

  it('places the primary Harness before the Conversation API', () => {
    const harness = DEPLOY_ORDER.indexOf('HarnessStack');
    for (const producer of [
      'ObservabilityStack',
      'AgentStack',
    ] as const) {
      expect(DEPLOY_ORDER.indexOf(producer)).toBeLessThan(harness);
    }
    expect(harness).toBeLessThan(DEPLOY_ORDER.indexOf('ApiStack'));
    expect(PARAMS.measurementFunctionArn.producedBy).toBe('AgentStack');
    for (const key of ['guardrailId', 'guardrailArn', 'guardrailVersion'] as const) {
      expect(PARAMS[key].producedBy).toBe('AgentStack');
      expect(DEPLOY_ORDER.indexOf(PARAMS[key].producedBy)).toBeLessThan(
        DEPLOY_ORDER.indexOf('ApiStack'),
      );
    }
    expect(PARAMS.harnessArn.producedBy).toBe('HarnessStack');
  });

  it('places the token service right after AuthStack and before its consumers (ADR-077 §3)', () => {
    const tokenService = DEPLOY_ORDER.indexOf('TokenServiceStack');
    expect(tokenService).toBe(DEPLOY_ORDER.indexOf('AuthStack') + 1);
    for (const consumer of ['AgentStack', 'HarnessStack', 'ApiStack'] as const) {
      expect(tokenService).toBeLessThan(DEPLOY_ORDER.indexOf(consumer));
    }
    expect(PARAMS.tokenIssuerUrl).toStrictEqual({
      area: 'identity',
      name: 'token-issuer-url',
      producedBy: 'TokenServiceStack',
    });
    expect(PARAMS.tokenRouteExchangeFunctionArn).toStrictEqual({
      area: 'identity',
      name: 'token-route-exchange-function-arn',
      producedBy: 'TokenServiceStack',
    });
    expect(paramPath('dev', 'tokenRouteExchangeFunctionArn')).toBe(
      '/adlytics/dev/identity/token-route-exchange-function-arn',
    );
    expect(PARAMS.tokenCredentialProviderArn).toStrictEqual({
      area: 'identity',
      name: 'token-credential-provider-arn',
      producedBy: 'TokenServiceStack',
    });
    expect(PARAMS.agentClientSecretArn).toStrictEqual({
      area: 'identity',
      name: 'agent-client-secret-arn',
      producedBy: 'TokenServiceStack',
    });
  });

  it('derives every token-service name from the environment in one place', () => {
    expect(harnessTokenAudience('acme-staging')).toBe('adlytics-acme-staging-harness');
    expect(routeClientId('acme-staging')).toBe('adlytics-acme-staging-conversation-route');
    expect(conversationFunctionRoleName('acme-staging')).toBe(
      'adlytics-acme-staging-conversation-function',
    );
    expect(tokenRouteExchangeRoleName('acme-staging')).toBe(
      'adlytics-acme-staging-token-route-exchange',
    );
    expect(tokenIssuerRoleName('acme-staging')).toBe('adlytics-acme-staging-token-issuer');
    expect(TOKEN_SERVICE_STAGE).toBe('v1');
    expect(gatewayTokenAudience('acme-staging')).toBe('adlytics-acme-staging-gateway');
    expect(agentClientId('acme-staging')).toBe('adlytics-acme-staging-agent');
    expect(agentClientSecretName('acme-staging')).toBe(
      'adlytics/acme-staging/identity/agent-client-secret',
    );
    expect(tokenCredentialProviderName('acme-staging')).toBe('adlytics-acme-staging-token-service');
  });

  it('creates the distribution before the user pool that points at it', () => {
    // Cognito's callback URL must be the CloudFront host, which does not exist
    // until the distribution does. Without a custom domain something has to be
    // built before its consumer knows its name — see EdgeConfig.apiOriginAttached.
    expect(DEPLOY_ORDER.indexOf('FrontendStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('AuthStack'),
    );
    expect(DEPLOY_ORDER.indexOf('AuthStack')).toBeLessThan(DEPLOY_ORDER.indexOf('ApiStack'));
  });

  it('separates stack creation order from artefact build order', () => {
    /**
     * Two different orderings, easily conflated.
     *
     * The frontend *stack* is created early, because the distribution's domain is
     * an input to Cognito. The frontend *artefact* is built last, because a static
     * export bakes in the API URL and building before the API exists produces a
     * bundle that points nowhere and looks correct — the localhost defect ADR-012
     * exists to prevent.
     *
     * DEPLOY_ORDER describes stacks only. The artefact build is `make
     * build-frontend`, which runs after everything here.
     */
    expect(DEPLOY_ORDER.indexOf('FrontendStack')).toBeLessThan(
      DEPLOY_ORDER.indexOf('ApiStack'),
    );
    expect(PARAMS.publicBaseUrl.producedBy).toBe('FrontendStack');
    expect(PARAMS.apiBaseUrl.producedBy).toBe('ApiStack');
  });
});
