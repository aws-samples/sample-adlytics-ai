// SPDX-License-Identifier: MIT-0
/**
 * The conversation route's transport and the permissions that make it reachable.
 *
 * ADR-038 chose a Lambda Function URL with `AuthType: AWS_IAM` and `InvokeMode: RESPONSE_STREAM`,
 * reached only through CloudFront's Origin Access Control. Three of the things that decision
 * depends on fail silently, which is why they are asserted here rather than trusted.
 *
 * **The two-statement permission.** Since October 2025 a new Function URL requires both
 * `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`
 * [verified 2026-08-22, docs.aws.amazon.com/lambda/latest/dg/urls-auth.html]. A probe that granted
 * only the first returned 403 through CloudFront while succeeding for an IAM user — the user's
 * identity-based policy already carried `InvokeFunction`, and a service principal has none. The
 * failure is a plain 403 with nothing naming the missing action.
 *
 * **What the L2 omits.** `FunctionUrlOrigin.withOriginAccessControl` emits only the
 * `InvokeFunctionUrl` statement [verified 2026-08-22 by reading aws-cdk-lib 2.265.0,
 * `aws-cloudfront-origins/lib/function-url-origin.js`, `addInvokePermission`]. So the obvious
 * construct produces a stack that synthesises, deploys, and 403s on every request. If a future
 * bump makes the L2 emit both, this test still passes — it asserts the statements exist, not who
 * wrote them.
 *
 * **The streaming mode.** `RESPONSE_STREAM` is the entire reason this transport was chosen over
 * the HTTP API. Buffered would collect a whole deep dive and send it at once, which is
 * indistinguishable from the 29s-timeout problem ADR-038 set out to avoid, except slower to notice.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  API_PATH_PATTERN,
  CONVERSATION_PATH_PATTERN,
  LAMBDA_WEB_ADAPTER_VERSION,
  agentQualityAuditTableName,
  conversationFunctionRoleName,
  conversationFunctionTimeoutSeconds,
  lambdaWebAdapterLayerArn,
  pageContextTableName,
  paramPath,
  semanticV2StateTableName,
} from '../lib/config';
import { ApiStack } from '../lib/stacks/api-stack';
import { TEST_CONFIG } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';
import { guardrailProfileArns } from '../lib/constructs/agent/route-guardrail';

const templates = everyTemplate();
const api = templates.ApiStack as Record<string, unknown>;
const agent = templates.AgentStack as Record<string, unknown>;
const CONVERSATION_FUNCTION = 'adlytics-dev-ConversationFunction';

/**
 * The single element of a list, failing if there is not exactly one.
 *
 * Used instead of `list[0]` with a non-null assertion. `noUncheckedIndexedAccess` is on, and the
 * assertion operator would silence the checker while leaving an empty list to read as a pass on
 * `undefined` — which is the exact failure these tests exist to catch.
 */
function only<T>(items: T[], what: string): T {
  expect(items).toHaveLength(1);
  const [first] = items;
  if (first === undefined) throw new Error(`expected exactly one ${what}`);
  return first;
}

function functionByName(name: string): Record<string, unknown> {
  return only(
    resourcesOfType(api, 'AWS::Lambda::Function').filter(
      (properties) => properties.FunctionName === name,
    ),
    `Lambda named ${name}`,
  );
}

function functionLogicalId(name: string): string {
  type Resource = { Type?: unknown; Properties?: Record<string, unknown> };
  const resources = (api.Resources ?? {}) as Record<string, Resource>;
  const found = Object.entries(resources).filter(
    ([, resource]) =>
      resource.Type === 'AWS::Lambda::Function' && resource.Properties?.FunctionName === name,
  );
  expect(found).toHaveLength(1);
  const logicalId = found[0]?.[0];
  if (logicalId === undefined) throw new Error(`Lambda named ${name} has no logical id`);
  return logicalId;
}

function functionUrls(name: string): Record<string, unknown>[] {
  const logicalId = functionLogicalId(name);
  return resourcesOfType(api, 'AWS::Lambda::Url').filter((properties) =>
    JSON.stringify(properties.TargetFunctionArn).includes(logicalId)
  );
}

function roleLogicalId(functionName: string): string {
  const role = functionByName(functionName).Role as { 'Fn::GetAtt'?: unknown[] } | undefined;
  const logicalId = role?.['Fn::GetAtt']?.[0];
  if (typeof logicalId !== 'string') throw new Error(`${functionName} has no role logical id`);
  return logicalId;
}

function statementsForFunction(functionName: string): Record<string, unknown>[] {
  type Resource = { Type?: unknown; Properties?: Record<string, unknown> };
  const resources = (api.Resources ?? {}) as Record<string, Resource>;
  const role = roleLogicalId(functionName);
  return Object.values(resources)
    .filter((resource) => resource.Type === 'AWS::IAM::Policy')
    .map((resource) => resource.Properties ?? {})
    .filter((properties) =>
      ((properties.Roles ?? []) as { Ref?: unknown }[]).some(
        (candidate) => candidate.Ref === role,
      ),
    )
    .flatMap(
      (properties) =>
        (((properties.PolicyDocument as Record<string, unknown>).Statement ?? []) as Record<
          string,
          unknown
        >[]),
    );
}

function actions(statement: Record<string, unknown>): string[] {
  const value = statement.Action;
  return (Array.isArray(value) ? value : [value]).filter(
    (action): action is string => typeof action === 'string',
  );
}

/** Lambda permissions naming the CloudFront service principal. */
function cloudFrontPermissions(): Record<string, unknown>[] {
  const logicalId = functionLogicalId(CONVERSATION_FUNCTION);
  return resourcesOfType(api, 'AWS::Lambda::Permission').filter(
    (properties) =>
      properties.Principal === 'cloudfront.amazonaws.com' &&
      JSON.stringify(properties.FunctionName).includes(logicalId),
  );
}

describe('the conversation Function URL', () => {
  it('is declared at all', () => {
    // The assertion that keeps every other one in this file honest. `no-public-endpoints.test.ts`
    // iterates Function URLs and asserts each is AWS_IAM, which passed for months while no URL
    // existed. A gate over an empty list is not a gate.
    expect(functionUrls(CONVERSATION_FUNCTION)).toHaveLength(1);
  });

  it('requires IAM auth and streams the response', () => {
    const url = only(functionUrls(CONVERSATION_FUNCTION), 'conversation Function URL');
    expect(url.AuthType).toBe('AWS_IAM');
    expect(url.InvokeMode).toBe('RESPONSE_STREAM');
  });

  it('is not given a CORS configuration', () => {
    // One origin serves the app and this route, so every request is same-origin and no preflight
    // is ever issued. A CORS block here would be dead settings implying a cross-origin design —
    // and `AllowOrigins: ['*']` on a Function URL is the shape that turns one into a public API.
    const url = only(functionUrls(CONVERSATION_FUNCTION), 'conversation Function URL');
    expect(url.Cors).toBeUndefined();
  });
});

describe('CloudFront is granted both actions a Function URL needs', () => {
  it('grants exactly two permissions, and no more', () => {
    // Two, not one: the count is the assertion. A third would mean something widened the grant.
    expect(cloudFrontPermissions()).toHaveLength(2);
  });

  it('grants InvokeFunctionUrl scoped to the URL auth type', () => {
    const granted = only(
      cloudFrontPermissions().filter(
        (properties) => properties.Action === 'lambda:InvokeFunctionUrl',
      ),
      'InvokeFunctionUrl grant',
    );
    // Ties the grant to the URL rather than to the function, so it cannot be used to invoke the
    // function directly.
    expect(granted.FunctionUrlAuthType).toBe('AWS_IAM');
  });

  it('grants InvokeFunction confined to invocations arriving through the URL', () => {
    const granted = only(
      cloudFrontPermissions().filter((properties) => properties.Action === 'lambda:InvokeFunction'),
      'InvokeFunction grant',
    );
    // Without this the statement is a general grant to invoke the function, which is a broader
    // thing than the route needs and would survive the URL being deleted.
    expect(granted.InvokedViaFunctionUrl).toBe(true);
  });

  it('scopes every grant to one distribution', () => {
    // A `SourceArn` is what separates "our distribution may call this" from "the CloudFront
    // service may call this", and the second means any distribution in any account.
    for (const properties of cloudFrontPermissions()) {
      expect(properties.SourceArn).toBeDefined();
      expect(JSON.stringify(properties.SourceArn)).toContain(':cloudfront::');
      expect(JSON.stringify(properties.SourceArn)).toContain('distribution/');
    }
  });
});

describe('the conversation function is packaged for the web adapter', () => {
  it('uses the startup script as its handler', () => {
    // Under the adapter the handler is a script the exec wrapper runs, not a Python symbol.
    // `scripts/build-lambdas.sh` copies it to the asset root and fails the build if it is not
    // executable, because a non-executable script presents as a readiness-check timeout.
    Template.fromJSON(api).hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'run.sh',
      Architectures: ['arm64'],
    });
  });

  it('attaches the arm64 adapter layer for its own region', () => {
    const expected = lambdaWebAdapterLayerArn(TEST_CONFIG.region);
    // The arm64 variant specifically. An x86_64 layer on an arm64 function fails at init rather
    // than at build, which is the worst place to learn about a packaging mistake.
    expect(expected).toContain('LambdaAdapterLayerArm64');
    expect(expected).toContain(TEST_CONFIG.region);
    expect(expected).toContain(`:${LAMBDA_WEB_ADAPTER_VERSION}`);

    const layers = resourcesOfType(api, 'AWS::Lambda::Function')
      .flatMap((properties) => (Array.isArray(properties.Layers) ? properties.Layers : []))
      .map((layer) => JSON.stringify(layer));
    expect(layers).toContain(JSON.stringify(expected));
  });

  it('runs the adapter in streaming mode and keeps readiness off the request path', () => {
    const conversation = functionByName('adlytics-dev-ConversationFunction');
    const variables = ((conversation.Environment as Record<string, unknown>).Variables ?? {}) as
      Record<string, unknown>;
    expect(variables.AWS_LAMBDA_EXEC_WRAPPER).toBe('/opt/bootstrap');
    expect(variables.AWS_LWA_INVOKE_MODE).toBe('response_stream');
    // Not `/`, which is a path a browser could reach through CloudFront.
    expect(variables.AWS_LWA_READINESS_CHECK_PATH).toBe('/health');
  });

  it('neither names nor receives permission to read the Cognito client secret', () => {
    const functionName = 'adlytics-dev-ConversationFunction';
    const conversation = functionByName(functionName);
    const variables = ((conversation.Environment as Record<string, unknown>).Variables ?? {}) as
      Record<string, unknown>;
    expect(Object.keys(variables)).not.toContain('ADL_COGNITO_CLIENT_SECRET');
    expect(variables.ADL_COGNITO_CLIENT_SECRET_ID).toBeUndefined();

    const granted = statementsForFunction(functionName).flatMap(actions);
    expect(granted).not.toContain('secretsmanager:GetSecretValue');
  });

  it('refuses to build an adapter ARN for a region with no arm64 layer', () => {
    // AWS publishes the adapter in China as x86_64 only [verified 2026-08-22], and every function
    // here is arm64. That is a real incompatibility, so it fails at synth with the reason rather
    // than at init with a layer-not-found.
    expect(() => lambdaWebAdapterLayerArn('cn-north-1')).toThrow(/x86_64 only/);
    expect(() => lambdaWebAdapterLayerArn('cn-northwest-1')).toThrow();
  });
});

describe('the conversation function holds no AgentCore authority (ADR-077 §2)', () => {
  function statements(): Record<string, unknown>[] {
    return statementsForFunction(CONVERSATION_FUNCTION);
  }

  const actionsOf = (value: unknown): string[] =>
    (Array.isArray(value) ? value : [value]).filter((v): v is string => typeof v === 'string');

  it('allows no bedrock-agentcore action at all', () => {
    /**
     * The route reaches the Harness only with a route-minted bearer token, so an Allow here would
     * reopen H-RUNTIME-INVOKE-001: this role could drive the agent as any user. The Memory grants
     * are gone too, because under per-user memory they would read every user's memory.
     */
    const allowed = statements()
      .filter((statement) => statement.Effect === 'Allow')
      .flatMap((statement) => actionsOf(statement.Action))
      .filter((action) => action.startsWith('bedrock-agentcore:'));
    expect(allowed).toStrictEqual([]);
  });

  it('denies command execution and every Harness or Runtime invocation explicitly', () => {
    /**
     * A Deny with no matching Allow, kept deliberately. A broad `bedrock-agentcore:*` policy
     * attached to this role later is a realistic mistake, and a Deny cannot be overridden by a
     * subsequent Allow. The command actions execute inside the agent container; the invoke actions
     * would let this role act as any user.
     */
    const deny = only(
      statements().filter((statement) => statement.Effect === 'Deny'),
      'Deny statement',
    );
    expect(deny.Action).toStrictEqual([
      'bedrock-agentcore:InvokeAgentRuntimeCommand',
      'bedrock-agentcore:InvokeAgentRuntimeCommandShell',
      'bedrock-agentcore:InvokeHarness',
      'bedrock-agentcore:InvokeAgentRuntime',
    ]);
    expect(deny.Resource).toBe('*');
  });

  it('carries no minimum-lifetime refusal setting (ADR-077 §2, amended)', () => {
    const conversation = functionByName(CONVERSATION_FUNCTION);
    const variables = ((conversation.Environment as Record<string, unknown>).Variables ?? {}) as
      Record<string, unknown>;
    expect(Object.keys(variables).filter((key) => key.includes('FRESHNESS'))).toStrictEqual([]);
  });
});

describe('the conversation function exchanges each turn at the route exchange (ADR-077 §3)', () => {
  const parameterFor = (path: string): string => {
    const parameters = (api.Parameters ?? {}) as Record<string, Record<string, unknown>>;
    return only(
      Object.entries(parameters)
        .filter(([, parameter]) =>
          parameter.Type === 'AWS::SSM::Parameter::Value<String>' && parameter.Default === path)
        .map(([logicalId]) => logicalId),
      `parameter for ${path}`,
    );
  };
  const exchangeArn = (): unknown => ({
    Ref: parameterFor(paramPath('dev', 'tokenRouteExchangeFunctionArn')),
  });

  it('runs under the role the route-exchange resource policy names', () => {
    /**
     * TokenServiceStack's Deny admits exactly `adlytics-<env>-conversation-function` by
     * `aws:PrincipalArn`, so a generated role name would lock the route out of every turn.
     */
    type Resource = { Type?: unknown; Properties?: Record<string, unknown> };
    const role = (api.Resources as Record<string, Resource>)[roleLogicalId(CONVERSATION_FUNCTION)];
    expect(role?.Type).toBe('AWS::IAM::Role');
    expect(role?.Properties?.RoleName).toBe(conversationFunctionRoleName('dev'));
    expect(conversationFunctionRoleName('dev')).toBe('adlytics-dev-conversation-function');
  });

  it('may invoke exactly the route-exchange function and no other Lambda', () => {
    const lambdaStatements = statementsForFunction(CONVERSATION_FUNCTION)
      .filter((statement) => actions(statement).some((action) => action.startsWith('lambda:')));
    expect(lambdaStatements).toStrictEqual([{
      Sid: 'ExchangeUserTokenForHarnessToken',
      Effect: 'Allow',
      Action: 'lambda:InvokeFunction',
      Resource: exchangeArn(),
    }]);
  });

  it('names the route-exchange function by its published ARN', () => {
    const conversation = functionByName(CONVERSATION_FUNCTION);
    const variables = ((conversation.Environment as Record<string, unknown>).Variables ?? {}) as
      Record<string, unknown>;
    expect(variables.ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION).toStrictEqual(exchangeArn());
  });

  it('times out at the turn budget the Harness token lifetime is derived from', () => {
    // 600 s deep dive in the test config plus 30 s: the same helper feeds the token lifetime.
    expect(conversationFunctionTimeoutSeconds(TEST_CONFIG)).toBe(630);
    expect(functionByName(CONVERSATION_FUNCTION).Timeout).toBe(630);
  });
});

describe('the managed Harness invocation boundary', () => {
  it('declares no AgentCore resource policy, because the Harness JWT authorizer is the boundary', () => {
    // ADR-077 §2 moves the boundary from this role's identity policy to the Harness's
    // `customJWTAuthorizer` (asserted exactly in harness-proof-stack.test.ts). A resource policy
    // here would be a second, IAM-principal path to the same Harness.
    expect(resourcesOfType(api, 'AWS::BedrockAgentCore::ResourcePolicy')).toHaveLength(0);
  });
});

describe('the conversation function atomically admits selected evaluation work', () => {
  /** Collect DynamoDB statements attached specifically to the ConversationFunction role. */
  function dynamoStatements(): Record<string, unknown>[] {
    type Resource = { Type?: unknown; Properties?: Record<string, unknown> };
    const stackResources = (api.Resources ?? {}) as Record<string, Resource>;
    const conversation = Object.values(stackResources).find(
      (resource) =>
        resource.Type === 'AWS::Lambda::Function' &&
        resource.Properties?.FunctionName === 'adlytics-dev-ConversationFunction',
    );
    const role = (conversation?.Properties?.Role as { 'Fn::GetAtt'?: unknown[] } | undefined)?.[
      'Fn::GetAtt'
    ]?.[0];
    if (typeof role !== 'string') throw new Error('conversation function has no role logical id');

    return Object.values(stackResources)
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .map((resource) => resource.Properties ?? {})
      .filter((properties) =>
        ((properties.Roles ?? []) as { Ref?: unknown }[]).some(
          (candidate) => candidate.Ref === role,
        ),
      )
      .flatMap((properties) => {
        const statements = ((properties.PolicyDocument as Record<string, unknown>).Statement ??
          []) as Record<string, unknown>[];
        return statements.filter((statement) =>
          (Array.isArray(statement.Action) ? statement.Action : [statement.Action]).some(
            (action) => typeof action === 'string' && action.startsWith('dynamodb:'),
          ),
        );
      });
  }

  const asStrings = (value: unknown): string[] =>
    (Array.isArray(value) ? value : [value]).filter((v): v is string => typeof v === 'string');

  it('writes standalone audit or atomically writes audit plus retained outbox', () => {
    // The third DynamoDB statement is the page-context read, asserted on its own below.
    const statements = dynamoStatements()
      .filter((statement) => statement.Sid !== 'RedeemPageContextRecords');
    expect(statements).toHaveLength(2);
    const audit = only(statements.filter((statement) =>
      asStrings(statement.Action).includes('dynamodb:PutItem')
      && !asStrings(statement.Action).includes('dynamodb:TransactWriteItems')),
    'terminal-audit statement');
    expect(asStrings(audit.Action)).toEqual(['dynamodb:PutItem', 'dynamodb:GetItem']);
    const transaction = only(statements.filter((statement) =>
      asStrings(statement.Action).includes('dynamodb:TransactWriteItems')),
    'evaluation handoff transaction');
    expect(asStrings(transaction.Action)).toEqual([
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:TransactWriteItems',
    ]);
    expect(JSON.stringify(transaction.Resource).toLowerCase()).toContain(
      'evaluationoutboxtablearn',
    );

    const environment = JSON.stringify(functionByName(CONVERSATION_FUNCTION).Environment);
    expect(environment).toContain('ADL_EVALUATION_OUTBOX_TABLE');
    expect(environment).not.toContain('ADL_EVALUATION_QUEUE_URL');
    const resources = JSON.stringify(statements.map((statement) => statement.Resource));
    expect(resources).not.toContain(semanticV2StateTableName(TEST_CONFIG.envName, 'publications'));
    expect(resources).not.toContain(agentQualityAuditTableName(TEST_CONFIG.envName));
  });

  it('renders the outbox handoff exactly when the evaluation plane is provisioned (ADR-079)', () => {
    // The route admits every released turn whenever the plane exists; the dispatcher's release
    // records, not a route setting, decide what reaches evaluation.
    const unprovisioned = Template.fromStack(new ApiStack(new cdk.App(), 'ApiWithoutEvaluation', {
      config: TEST_CONFIG,
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      sourceAssetPath: __dirname,
    })).toJSON() as Record<string, unknown>;
    const conversation = resourcesOfType(unprovisioned, 'AWS::Lambda::Function')
      .find((fn) => fn.FunctionName === CONVERSATION_FUNCTION);
    expect(JSON.stringify(conversation?.Environment)).not.toContain('ADL_EVALUATION_OUTBOX_TABLE');
    expect(JSON.stringify(unprovisioned).toLowerCase()).not.toContain('evaluationoutboxtable');
    expect(JSON.stringify(functionByName(CONVERSATION_FUNCTION).Environment)).toContain('ADL_EVALUATION_OUTBOX_TABLE');
  });
  it('never sends evaluation content directly to SQS after the terminal event', () => {
    const granted = statementsForFunction(CONVERSATION_FUNCTION).flatMap(actions);
    expect(granted).not.toContain('sqs:SendMessage');
  });

  it('reads only page# records from the page-context table, with one GetItem (ADR-120)', () => {
    const redeem = only(
      dynamoStatements().filter((statement) => statement.Sid === 'RedeemPageContextRecords'),
      'page-context redemption statement',
    );
    expect(redeem.Effect).toBe('Allow');
    expect(asStrings(redeem.Action)).toEqual(['dynamodb:GetItem']);
    expect(JSON.stringify(redeem.Resource)).toContain(
      `:table/${pageContextTableName(TEST_CONFIG.envName)}`,
    );
    expect(redeem.Condition).toStrictEqual({
      'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['page#*'] },
    });
    const pageContextStatements = dynamoStatements().filter((statement) =>
      JSON.stringify(statement.Resource).includes(pageContextTableName(TEST_CONFIG.envName)));
    expect(pageContextStatements).toStrictEqual([redeem]);
    const variables = ((functionByName(CONVERSATION_FUNCTION).Environment as Record<string, unknown>)
      .Variables ?? {}) as Record<string, unknown>;
    const presentation = ((functionByName('adlytics-dev-PresentationFunction').Environment as
      Record<string, unknown>).Variables ?? {}) as Record<string, unknown>;
    // The same published table name the presentation function writes `page#` records into.
    expect(variables.ADL_PAGE_CONTEXT_TABLE).toStrictEqual(presentation.ADL_PAGE_CONTEXT_TABLE);
    expect(variables.ADL_PAGE_CONTEXT_TABLE).toBeDefined();
  });

  it('never grants a wildcard over the DynamoDB action namespace', () => {
    for (const action of dynamoStatements().flatMap((statement) => asStrings(statement.Action))) {
      expect(action).not.toContain('*');
    }
  });
});

describe('the guardrail the route applies at both ends of a turn', () => {
  /**
   * Layers 1 and 4 of ADR-041, as amended by ADR-042.
   *
   * The Guardrail lives in AgentStack so both the repository Runtime and inbound routes consume
   * one earlier deploy interface without a Runtime/API cycle. ID, ARN, and DRAFT version arrive
   * through typed SSM parameters.
   */
  function bedrockStatements(): Record<string, unknown>[] {
    return statementsForFunction(CONVERSATION_FUNCTION).filter((statement) =>
      actions(statement).some((action) => action.startsWith('bedrock:'))
    );
  }

  const asStrings = (value: unknown): string[] =>
    (Array.isArray(value) ? value : [value]).filter((v): v is string => typeof v === 'string');

  /** The conversation function's environment, identified the way this file already does. */
  function conversationEnvironment(): string {
    const conversation = functionByName(CONVERSATION_FUNCTION);
    return JSON.stringify(
      (conversation.Environment as Record<string, unknown>)?.Variables ?? {},
    );
  }

  it('declares exactly one guardrail', () => {
    expect(resourcesOfType(agent, 'AWS::Bedrock::Guardrail')).toHaveLength(1);
  });

  it('creates no guardrail version, because ApplyGuardrail accepts DRAFT', () => {
    // A version resource would be a second thing able to lag behind the first, and nothing here
    // wants to run an older guardrail than the one just deployed. The constraint does not
    // generalise: `CfnEnforcedGuardrailConfiguration` refuses `DRAFT`.
    expect(resourcesOfType(agent, 'AWS::Bedrock::GuardrailVersion')).toHaveLength(0);
    expect(conversationEnvironment()).toContain('ADL_GUARDRAIL_VERSION');
    expect(conversationEnvironment()).toContain('guardrailversion');
  });

  it('filters harmful content at calibrated strengths, and prompt attack inbound only', () => {
    /**
     * `PROMPT_ATTACK` must carry `outputStrength: NONE`. A prompt attack is a property of an input,
     * and Bedrock rejects a configuration asking for it on the output side — so this is the one
     * filter whose two strengths legitimately differ, and asserting it keeps a well-meaning
     * "tighten everything to HIGH" edit from failing the deploy.
     */
    const guardrail = only(resourcesOfType(agent, 'AWS::Bedrock::Guardrail'), 'guardrail');
    const filters = (guardrail.ContentPolicyConfig as { FiltersConfig?: Record<string, string>[] })
      .FiltersConfig;
    const byType = new Map((filters ?? []).map((f) => [f.Type, f]));

    expect([...byType.keys()].sort()).toEqual([
      'HATE',
      'INSULTS',
      'MISCONDUCT',
      'PROMPT_ATTACK',
      'SEXUAL',
      'VIOLENCE',
    ]);
    expect(byType.get('PROMPT_ATTACK')?.OutputStrength).toBe('NONE');
    // LOW on prompt attack and misconduct, from the 2026-09-30 calibration: at HIGH, benign
    // formatting requests were blocked, and on the Standard tier "kill the campaign" scores MEDIUM
    // misconduct. `make accept-guardrail` holds the deployed guardrail to the calibration set.
    expect(byType.get('PROMPT_ATTACK')?.InputStrength).toBe('LOW');
    expect(byType.get('MISCONDUCT')?.InputStrength).toBe('LOW');
    // MEDIUM on the other content categories: a filter that blocks questions about advertising is a
    // filter someone turns off.
    expect(byType.get('VIOLENCE')?.InputStrength).toBe('MEDIUM');
  });

  it('runs on the Standard tier through the guardrail profile of its geography', () => {
    // Standard is the only tier that detects system-prompt leakage, and it requires a profile.
    const guardrail = only(resourcesOfType(agent, 'AWS::Bedrock::Guardrail'), 'guardrail');
    const content = guardrail.ContentPolicyConfig as {
      ContentFiltersTierConfig?: { TierName?: string };
    };
    expect(content.ContentFiltersTierConfig?.TierName).toBe('STANDARD');
    expect(JSON.stringify(guardrail.CrossRegionConfig)).toMatch(
      /guardrail-profile\/(us|eu)\.guardrail\.v1:0/,
    );
  });

  it('blocks credentials and direct identifiers rather than masking them', () => {
    /**
     * `BLOCK`, never `ANONYMIZE`. Masking lets the answer through with tags where a value should be,
     * and an answer containing `{EMAIL}` in place of a figure is a corrupted answer presented as a
     * good one. This product withholds with a reason instead.
     */
    const guardrail = only(resourcesOfType(agent, 'AWS::Bedrock::Guardrail'), 'guardrail');
    const entities = (
      guardrail.SensitiveInformationPolicyConfig as {
        PiiEntitiesConfig?: Record<string, string>[];
      }
    ).PiiEntitiesConfig;

    expect(entities?.length).toBeGreaterThan(0);
    for (const entity of entities ?? []) {
      expect(entity.Action).toBe('BLOCK');
    }
    const types = (entities ?? []).map((entity) => entity.Type);
    expect(types).toContain('AWS_SECRET_KEY');
    expect(types).toContain('CREDIT_DEBIT_CARD_NUMBER');
    // `NAME` and `ADDRESS` are deliberately absent: campaign names, brand names and ASIN titles are
    // the substance of what this product returns, so a filter firing on every one of them produces
    // interventions nobody reads — which looks like coverage.
    expect(types).not.toContain('NAME');
    expect(types).not.toContain('ADDRESS');
  });

  it('declares no contextual grounding filter, because there is no grounding source', () => {
    /**
     * ADR-041's layer 4 was a grounding check against tool output. `toolResultMetadata` never
     * arrives on the Runtime stream (G-48), so the route holds nothing to ground against. A filter
     * with no source returns an empty assessment — a control that reports success because it
     * evaluated nothing, which is the worst of the three possible states.
     *
     * ADR-042 makes a grounding source the trigger for adding it back.
     */
    const guardrail = only(resourcesOfType(agent, 'AWS::Bedrock::Guardrail'), 'guardrail');
    expect(guardrail.ContextualGroundingPolicyConfig).toBeUndefined();
  });

  it('grants ApplyGuardrail scoped to that guardrail and nothing else on Bedrock', () => {
    // Scoped, unlike `bedrock:InvokeGuardrailChecks` which `AgentStack` must grant on `*` because it
    // names no resource. `ApplyGuardrail` names the guardrail it applies, so the honest form is
    // available and is used.
    const granted = bedrockStatements();
    expect(granted).toHaveLength(1);
    expect(asStrings(granted[0]?.Action)).toEqual(['bedrock:ApplyGuardrail']);
    expect(granted[0]?.Resource).not.toBe('*');
    const resources = JSON.stringify(granted[0]?.Resource);
    expect(resources).toContain('guardrailarn');
    // The profile in each Region it may route to, and nothing broader.
    const profiles = resources.match(/guardrail-profile\/(us|eu)\.guardrail\.v1:0/g) ?? [];
    expect(profiles.length).toBeGreaterThan(0);
    expect(new Set(profiles).size).toBe(1);
  });

  it('grants no model invocation to this route', () => {
    /**
     * The route never calls a model — managed Harness does, under the Harness execution role. A
     * grant here would make it look as though the narration could be produced locally, which is the guarantee "tools compute, model narrates" depends on.
     */
    const actions = bedrockStatements().flatMap((statement) => asStrings(statement.Action));
    expect(actions).not.toContain('bedrock:InvokeModel');
    expect(actions).not.toContain('bedrock:InvokeModelWithResponseStream');
    for (const action of actions) {
      expect(action).not.toContain('*');
    }
  });

  it('passes the guardrail id to the runtime by reference, never as a literal', () => {
    // A `Ref`/`GetAtt`, so the value cannot drift from the resource. A literal would be a second
    // statement of the same fact and would silently name a guardrail from a previous deploy.
    expect(conversationEnvironment()).toContain('ADL_GUARDRAIL_ID');
    expect(conversationEnvironment()).toContain('guardrailid');
  });
});

describe('the edge routes chat to the Function URL and signs it', () => {
  const frontend = templates.FrontendStack as Record<string, unknown>;

  /** The distribution's cache behaviours, in the order CloudFront will evaluate them. */
  function behaviours(): Record<string, unknown>[] {
    const distribution = only(
      resourcesOfType(frontend, 'AWS::CloudFront::Distribution'),
      'distribution',
    );
    const config = distribution.DistributionConfig as Record<string, unknown>;
    return (config.CacheBehaviors ?? []) as Record<string, unknown>[];
  }

  it('lists the chat pattern before the API pattern', () => {
    // The assertion this whole file exists for, after the two-statement permission. CloudFront takes
    // the *first* matching behaviour, not the most specific, so `/api/*` ahead of `/api/chat*` would
    // send every conversation to API Gateway and into the fixed 29s timeout — presenting as a chat
    // that always fails slowly rather than as a routing mistake.
    const patterns = behaviours().map((behaviour) => behaviour.PathPattern);
    expect(patterns).toEqual([
      CONVERSATION_PATH_PATTERN,
      API_PATH_PATTERN,
    ]);
    expect(patterns.indexOf(CONVERSATION_PATH_PATTERN)).toBeLessThan(
      patterns.indexOf(API_PATH_PATTERN),
    );
  });

  it('signs the conversation origin with an Origin Access Control', () => {
    const origins = resourcesOfType(frontend, 'AWS::CloudFront::Distribution').flatMap(
      (properties) =>
        ((properties.DistributionConfig as Record<string, unknown>).Origins ?? []) as Record<
          string,
          unknown
        >[],
    );
    // Two OACs in this stack: S3's for the site bucket, and this one. The conversation origin is
    // the one with a custom origin config, because a Function URL is reached over HTTPS.
    const signed = origins.filter(
      (origin) => origin.CustomOriginConfig !== undefined && origin.OriginAccessControlId,
    );
    expect(signed).toHaveLength(1);

    const oacs = resourcesOfType(frontend, 'AWS::CloudFront::OriginAccessControl').map(
      (properties) => properties.OriginAccessControlConfig as Record<string, unknown>,
    );
    const forLambda = oacs.filter(
      (config) => config.OriginAccessControlOriginType === 'lambda',
    );
    expect(forLambda).toHaveLength(1);
    // Always, not "when the viewer sent none". The viewer never sends an Authorization header here
    // (the session is a cookie), so NEVER would leave the origin request unsigned — which an
    // AWS_IAM Function URL rejects with a 403 that names nothing.
    for (const oac of forLambda) {
      expect(oac.SigningBehavior).toBe('always');
      expect(oac.SigningProtocol).toBe('sigv4');
    }
  });

  it('never forwards the two headers SigV4 owns', () => {
    const policies = resourcesOfType(frontend, 'AWS::CloudFront::OriginRequestPolicy').map(
      (properties) => properties.OriginRequestPolicyConfig as Record<string, unknown>,
    );
    const conversation = only(
      policies.filter((config) => String(config.Comment ?? '').includes('Conversation route')),
      'conversation origin request policy',
    );

    const headers = JSON.stringify(conversation.HeadersConfig).toLowerCase();
    // CloudFront overwrites Authorization with the signature it generates; forwarding the viewer's
    // value breaks the signature. And the signature covers Host, which must be the Function URL's
    // own hostname rather than the distribution's.
    expect(headers).not.toContain('authorization');
    expect(headers).not.toContain('"host"');
    // Refused by CloudFront in an allowlist with InvalidArgument, because it is consumed as signing
    // input rather than forwarded. Asserted so nobody re-adds it and gets an opaque deploy failure.
    expect(headers).not.toContain('x-amz-content-sha256');

    // The credential the route actually checks. Stripping cookies would answer every request 401.
    expect(String(conversation.CookiesConfig && JSON.stringify(conversation.CookiesConfig))).toContain(
      'all',
    );
    // The exact spelling `domain/identity/authorization.CSRF_HEADER` declares. Asserted as a
    // literal because the conventional `x-csrf-token` is a plausible near-miss that the edge would
    // strip, turning every mutating request into a 401 with a valid session attached.
    expect(headers).toContain('x-adl-csrf');
    // ADR-081 §2: the route mints the correlation ID; a client value never reaches it.
    expect(headers).not.toContain('x-correlation-id');
  });

  it('disables caching on the conversation behaviour', () => {
    // Every answer is per-user and per-session. A cached one is served to the next person who asks
    // the same question, which is a data-disclosure bug rather than a stale-content one.
    const cachingDisabled = '4135ea2d-6df8-44a3-9df3-4b5a84be39ad';
    const chat = only(
      behaviours().filter((behaviour) => behaviour.PathPattern === CONVERSATION_PATH_PATTERN),
      'chat behaviour',
    );
    expect(chat.CachePolicyId).toBe(cachingDisabled);
  });
});
describe('the saved terminal query (observability §3.9)', () => {
  it('reads only the conversation terminal line from the conversation function log group', () => {
    const query = only(resourcesOfType(api, 'AWS::Logs::QueryDefinition'), 'terminal query');
    expect(query.Name).toBe('adlytics-dev/conversation-turn-terminal');
    const [logGroup] = query.LogGroupNames as unknown[];
    expect(JSON.stringify(logGroup)).toContain('ConversationFunctionLogs');
    const text = String(query.QueryString);
    expect(text).toContain('filter operation = "conversation.turn"');
    expect(text).toContain('phase = "terminal"');
    for (const field of ['audit_id', 'correlation_id', 'managed_terminal', 'publication_outcome', 'audit_state']) {
      expect(text).toContain(field);
    }
  });
});

describe('guardrail profiles', () => {
  it('route us-east-1 only within the US geography and refuse an undeclared Region', () => {
    expect(guardrailProfileArns('aws', 'us-east-1', '111122223333')).toEqual([
      'arn:aws:bedrock:us-east-1:111122223333:guardrail-profile/us.guardrail.v1:0',
      'arn:aws:bedrock:us-east-2:111122223333:guardrail-profile/us.guardrail.v1:0',
      'arn:aws:bedrock:us-west-2:111122223333:guardrail-profile/us.guardrail.v1:0',
    ]);
    expect(() => guardrailProfileArns('aws', 'ap-south-1', '111122223333')).toThrow(
      'No guardrail profile is declared',
    );
  });
});
