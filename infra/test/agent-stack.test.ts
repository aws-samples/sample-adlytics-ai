// SPDX-License-Identifier: MIT-0
/**
 * AgentStack: the two tool functions, and the IAM boundary between them.
 *
 * The assertion this file exists for is the **asymmetry**. Two functions in one stack, built by the
 * same factory from the same asset, and one of them must hold no data permissions at all. That is
 * the whole reason they are not collocated, it is invisible in a diff that adds a grant to the
 * wrong one, and CloudFormation will happily deploy the widened version.
 *
 * It also doubles as the synth-time check for the stack, the way `data-stack.test.ts` does: what
 * the repository ships is `UNSET`, so `make synth` cannot run on a fresh checkout and this is the
 * only place the template is built from a complete configuration.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { AgentStack } from '../lib/stacks/agent-stack';
import {
  agentClientId,
  gatewayPolicyName,
  gatewayTokenAudience,
  pageContextTableName,
  paramPath,
  policyEngineName,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
} from '../lib/config';
import type { AppConfig } from '../lib/config';
import { dataBucketName } from '../lib/mixins/storage';
import { tracingEnvironment } from '../lib/telemetry';
import { TEST_CONFIG, configWith } from './support/config';

/** Any directory will do: `Code.fromAsset` only needs one to exist at synth. */
const ASSET = __dirname;

/**
 * The exact files staged into the Lambda source asset by `make build-lambdas`.
 *
 * Gateway and both tool functions must consume one snapshot rather than mutable repository source.
 */
const STAGED_CONTRACTS = path.join(__dirname, '..', '..', 'build', 'src', 'contracts');
const TOOL_SCHEMA = path.join(STAGED_CONTRACTS, 'generated', 'tools.json');

function synth(config: AppConfig = TEST_CONFIG): Template {
  const app = new cdk.App();
  const stack = new AgentStack(app, 'Adlytics-Agent-test', {
    config,
    sourceAssetPath: ASSET,
    toolSchemaPath: TOOL_SCHEMA,
  });
  return Template.fromStack(stack);
}

type Resources = Record<string, { Type: string; Properties?: Record<string, unknown> }>;

function resources(template: Template): Resources {
  return template.toJSON().Resources as Resources;
}

/** One function's properties, failing the test rather than returning undefined. */
function toolFunction(template: Template, name: string): Record<string, unknown> {
  const found = functionsByName(template)[name];
  if (found === undefined) {
    throw new Error(`no function named ${name} in the template`);
  }
  return found;
}

/** The environment variables of one function. */
function variables(template: Template, name: string): Record<string, string> {
  const environment = toolFunction(template, name).Environment as
    | { Variables?: Record<string, string> }
    | undefined;
  return environment?.Variables ?? {};
}

function functionsByName(template: Template): Record<string, Record<string, unknown>> {
  const found: Record<string, Record<string, unknown>> = {};
  for (const resource of Object.values(resources(template))) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    const properties = resource.Properties ?? {};
    found[String(properties.FunctionName)] = properties;
  }
  return found;
}

/**
 * Every inline policy document as one string.
 *
 * Rendered rather than pattern-matched because an ARN built from `this.partition` becomes an
 * `Fn::Join`, which `Match` cannot see inside. Containment over the rendered text is what lets a
 * grant be asserted absent.
 */
function policyText(template: Template): string {
  const documents = Object.values(resources(template))
    .filter((resource) => resource.Type === 'AWS::IAM::Policy')
    .map((resource) => (resource.Properties ?? {}).PolicyDocument);
  return JSON.stringify(documents);
}

/** The policy fields these tests inspect after synthesis. */
type PolicyStatement = {
  Action?: unknown;
  Resource?: unknown;
  Condition?: unknown;
};

/** Every inline policy statement, so a grant can be asserted against its own resource. */
function allStatements(template: Template): PolicyStatement[] {
  const found: PolicyStatement[] = [];
  for (const resource of Object.values(resources(template))) {
    if (resource.Type !== 'AWS::IAM::Policy') continue;
    const document = (resource.Properties ?? {}).PolicyDocument as
      | { Statement?: PolicyStatement[] }
      | undefined;
    found.push(...(document?.Statement ?? []));
  }
  return found;
}

/** Inline statements attached to one Lambda role, identified by its stable construct-id prefix. */
type RoleName = 'MeasurementTool' | 'CatalogTool' | 'GatewayInterceptor';

function statementsForRole(template: Template, roleName: RoleName): PolicyStatement[] {
  return Object.values(resources(template))
    .filter((resource) => resource.Type === 'AWS::IAM::Policy')
    .filter((resource) =>
      (((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? []).some((role) =>
        role.Ref.includes(roleName),
      ),
    )
    .flatMap((resource) => {
      const document = (resource.Properties ?? {}).PolicyDocument as {
        Statement?: PolicyStatement[];
      };
      return document.Statement ?? [];
    });
}

function statementActions(statement: PolicyStatement): string[] {
  const action = statement.Action;
  return (Array.isArray(action) ? action : [action]).filter(
    (value): value is string => typeof value === 'string',
  );
}

function statementResourceTexts(statement: PolicyStatement): string[] {
  const resource = statement.Resource;
  return (Array.isArray(resource) ? resource : [resource]).map(
    (value) => JSON.stringify(value) ?? '',
  );
}

function hasExactActions(statement: PolicyStatement, actions: readonly string[]): boolean {
  return (
    JSON.stringify(statementActions(statement).toSorted()) ===
    JSON.stringify(actions.toSorted())
  );
}

function roleActions(template: Template, roleName: RoleName): string[] {
  return statementsForRole(template, roleName).flatMap(statementActions);
}

/** The role each inline policy is attached to, by logical id. */
function policyRoleRefs(template: Template): string[] {
  return Object.values(resources(template))
    .filter((resource) => resource.Type === 'AWS::IAM::Policy')
    .flatMap((resource) => ((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? [])
    .map((role) => role.Ref);
}

/** Every action any inline policy grants, flattened. */
function policyActions(template: Template): string[] {
  const actions: string[] = [];
  for (const resource of Object.values(resources(template))) {
    if (resource.Type !== 'AWS::IAM::Policy') continue;
    const document = (resource.Properties ?? {}).PolicyDocument as
      | { Statement?: { Action?: string | string[] }[] }
      | undefined;
    for (const statement of document?.Statement ?? []) {
      const action = statement.Action;
      if (typeof action === 'string') actions.push(action);
      else if (Array.isArray(action)) actions.push(...action);
    }
  }
  return actions;
}

describe('the two tool functions', () => {
  it('declares exactly two isolated domains and the Gateway interceptor', () => {
    // ADR-083 retired the presentation finalizer: the answer streams as text, so no Lambda
    // publishes it.
    synth().resourceCountIs('AWS::Lambda::Function', 3);
  });

  it('names them from the environment', () => {
    expect(Object.keys(functionsByName(synth())).sort()).toEqual([
      'adlytics-dev-CatalogTool',
      'adlytics-dev-GatewayInterceptor',
      'adlytics-dev-MeasurementTool',
    ]);
  });

  it('points each at its own entry point', () => {
    const template = synth();
    expect(toolFunction(template, 'adlytics-dev-MeasurementTool').Handler).toBe(
      'adapters.inbound.tool_handlers.measurement_entrypoint.handler',
    );
    expect(toolFunction(template, 'adlytics-dev-CatalogTool').Handler).toBe(
      'adapters.inbound.tool_handlers.catalog_entrypoint.handler',
    );
    expect(JSON.stringify(template.toJSON())).not.toContain('presentation_entrypoint');
  });

  it('builds every function arm64 on the configured runtime', () => {
    // ADR-005. An ADOT layer is published per architecture, and the wrong variant fails at
    // function init in AWS rather than at build — so this is asserted rather than assumed.
    for (const properties of Object.values(functionsByName(synth()))) {
      expect(properties.Architectures).toEqual(['arm64']);
      expect(properties.Runtime).toBe('python3.14');
    }
  });

  it('takes the runtime from config rather than hardcoding it', () => {
    const functions = functionsByName(synth(configWith({ pythonRuntime: 'python3.14' })));
    expect(Object.values(functions).every((p) => p.Runtime === 'python3.14')).toBe(true);
  });

  it('gives the measurement function longer than the adapter waits on Athena', () => {
    // The query adapter abandons a statement at 30s. A function timeout at or below that would
    // kill the caller before its own error path could report why.
    const measurement = toolFunction(synth(), 'adlytics-dev-MeasurementTool');
    expect(Number(measurement.Timeout)).toBeGreaterThan(30);
  });

  it('declares no layer of its own and attaches the shared one', () => {
    // It used to declare a `LayerVersion` over the same asset `ApiStack` did — one closure published
    // twice, on version streams that had already drifted to v4 and v2. The layer now lives in
    // `DependenciesStack`; this stack resolves its ARN. Zero is the assertion.
    const template = synth();
    template.resourceCountIs('AWS::Lambda::LayerVersion', 0);
    for (const properties of Object.values(template.findResources('AWS::Lambda::Function'))) {
      // Matched without hyphens: CDK builds the dynamic-reference parameter's logical id from the
      // SSM path with non-alphanumerics stripped, so `dependencies-layer-arn` renders as
      // `dependencieslayerarn`. Asserting the hyphenated form passes only by accident of substring.
      expect(JSON.stringify((properties.Properties as { Layers?: unknown }).Layers)).toContain(
        'dependencieslayerarn',
      );
    }
  });
});

describe('the retired evidence receipt channel', () => {
  it('leaves no key, grant, or environment reference behind', () => {
    // Receipts were the in-loop binding mechanism ADR-061 records as rejected; the KMS key that
    // authenticated them cost one call per tool result with no consumer left to verify it.
    const template = synth();
    template.resourceCountIs('AWS::KMS::Key', 0);
    template.resourceCountIs('AWS::KMS::Alias', 0);
    for (const name of ['MeasurementTool', 'CatalogTool'] as const) {
      // The one KMS action a tool holds reads the token-service verification key (ADR-077 §5);
      // no receipt Encrypt, GenerateMac, or Sign survives.
      expect(
        roleActions(template, name).filter((action) => action.startsWith('kms:')),
      ).toStrictEqual(['kms:GetPublicKey']);
      expect(variables(template, `adlytics-dev-${name}`)).not.toHaveProperty(
        'ADL_EVIDENCE_RECEIPT_KEY_ARN',
      );
    }
  });
});

describe('log groups', () => {
  it('declares one per function with retention from config', () => {
    const template = synth();
    template.resourceCountIs('AWS::Logs::LogGroup', 3);
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/aws/lambda/adlytics-dev-GatewayInterceptor',
      RetentionInDays: 14,
    });
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/aws/lambda/adlytics-dev-MeasurementTool',
      RetentionInDays: 14,
    });
  });

  it('takes retention from config rather than a constant', () => {
    const template = synth(configWith({ logRetentionDays: 30 }));
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/aws/lambda/adlytics-dev-CatalogTool',
      RetentionInDays: 30,
    });
  });

  it('provisions no retention-setting singleton', () => {
    // The `logRetention` property would add an x86_64 Node function to an arm64 stack. Asserted by
    // the absence of a nodejs runtime anywhere, which is what that helper drags in.
    const runtimes = Object.values(functionsByName(synth())).map((p) => String(p.Runtime));
    expect(runtimes.filter((r) => r.startsWith('nodejs'))).toEqual([]);
  });
});

describe('the transient page-context table', () => {
  it('has the exact disposable point-lookup shape', () => {
    const found = Object.values(resources(synth())).find(
      (resource) =>
        resource.Type === 'AWS::DynamoDB::Table' &&
        resource.Properties?.TableName === pageContextTableName('dev'),
    ) as
      | {
          Properties: Record<string, unknown>;
          DeletionPolicy?: string;
          UpdateReplacePolicy?: string;
        }
      | undefined;
    if (found === undefined) throw new Error('no page-context table in the template');

    expect(found.Properties).toMatchObject({
      TableName: pageContextTableName('dev'),
      AttributeDefinitions: [{ AttributeName: 'lookup_key', AttributeType: 'S' }],
      KeySchema: [{ AttributeName: 'lookup_key', KeyType: 'HASH' }],
      BillingMode: 'PAY_PER_REQUEST',
      SSESpecification: { SSEEnabled: true },
      TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
    });
    expect(found.Properties).not.toHaveProperty('GlobalSecondaryIndexes');
    expect(found.Properties).not.toHaveProperty('LocalSecondaryIndexes');
    expect(found.DeletionPolicy).toBe('Delete');
    expect(found.UpdateReplacePolicy).toBe('Delete');
  });
});

describe('the environment both functions read', () => {
  it('pins the V2 interactive environment without inactive data settings', () => {
    const env = variables(synth(), 'adlytics-dev-MeasurementTool');
    expect(env.ADL_ATHENA_WORKGROUP).toBe(semanticV2AthenaWorkgroupName('dev', 'interactive'));
    expect(env.ADL_SEMANTIC_V2_CURRENT_DATABASE).toEqual({
      Ref: expect.stringMatching(/^SsmParameterValue.*(?:current|database)/i),
    });
    expect(env.ADL_SEMANTIC_V2_CURRENT_DATABASE).not.toEqual(
      env.ADL_SEMANTIC_V2_BUCKET,
    );
    expect(env.ADL_ATHENA_BATCH_WORKGROUP).toBeUndefined();
    expect(env.ADL_SEMANTIC_V2_SOURCE_DATABASE).toBeUndefined();
    expect(env.ADL_SEMANTIC_V2_CANDIDATE_DATABASE).toBeUndefined();
    expect(env.ADL_GLUE_DATABASE).toBeUndefined();
    expect(env.ADL_FRESHNESS_TABLE).toBeUndefined();
    expect(env.ADL_RUN_LEDGER_TABLE).toBeUndefined();
    expect(env.ADL_PAGE_CONTEXT_TABLE).toBe(pageContextTableName('dev'));
    expect(env.ADL_SEMANTIC_V2_BUCKET).toEqual({
      Ref: expect.stringContaining('SsmParameterValueadlyticsdevdatav2bucket'),
    });
    expect(env.ADL_SEMANTIC_ATTEMPTS_TABLE).toBe(semanticV2StateTableName('dev', 'attempts'));
    expect(env.ADL_SEMANTIC_LEASES_TABLE).toBe(semanticV2StateTableName('dev', 'leases'));
    expect(env.ADL_SEMANTIC_PUBLICATIONS_TABLE).toBe(
      semanticV2StateTableName('dev', 'publications'),
    );
  });

  it('carries the result-reuse age from config', () => {
    const env = variables(
      synth(configWith({ athenaResultReuseMaxAgeMinutes: 5 })),
      'adlytics-dev-MeasurementTool',
    );
    expect(env.ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES).toBe('5');
  });

  it('carries the bounded query fan-out from config', () => {
    const env = variables(
      synth(configWith({ athenaQueryBatchMaxConcurrency: 3 })),
      'adlytics-dev-MeasurementTool',
    );
    expect(env.ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY).toBe('3');
  });

  it('never names the Cognito client secret', () => {
    // Neither function is the BFF. They load through `settings_without_client_secret`, and naming a
    // secret they hold no grant to read would put an unusable ARN in their environment and imply
    // they have something to do with authentication.
    const template = synth();
    for (const name of ['adlytics-dev-MeasurementTool', 'adlytics-dev-CatalogTool']) {
      const env = variables(template, name);
      expect(env).not.toHaveProperty('ADL_COGNITO_CLIENT_SECRET_ID');
      expect(env).not.toHaveProperty('ADL_COGNITO_CLIENT_SECRET');
    }
  });

  it('gives both functions the same environment', () => {
    // Configuration resolves as a whole, and withholding a string is not an authorization
    // mechanism — which table a code path may touch is settled by IAM.
    const template = synth();
    expect(variables(template, 'adlytics-dev-MeasurementTool')).toEqual(
      variables(template, 'adlytics-dev-CatalogTool'),
    );
  });
});

describe('tool caller verification (ADR-077 §5)', () => {
  const TOOLS = [
    ['adlytics-dev-MeasurementTool', 'MeasurementTool'],
    ['adlytics-dev-CatalogTool', 'CatalogTool'],
  ] as const;

  /** The logical id of the one deploy-time SSM parameter reading `parameterPath`. */
  function ssmParameter(template: Template, parameterPath: string): string {
    const parameters = (template.toJSON().Parameters ?? {}) as Record<string, Record<string, unknown>>;
    const found = Object.entries(parameters)
      .filter(([, parameter]) =>
        parameter.Type === 'AWS::SSM::Parameter::Value<String>' && parameter.Default === parameterPath)
      .map(([id]) => id);
    expect(found).toHaveLength(1);
    return found[0] as string;
  }

  function callerVariables(template: Template, name: string): Record<string, unknown> {
    const env = variables(template, name);
    return Object.fromEntries(
      ['ADL_TOKEN_ISSUER', 'ADL_TOKEN_SIGNING_KEY_ARN', 'ADL_GATEWAY_TOKEN_AUDIENCE', 'ADL_AGENT_CLIENT_ID']
        .filter((key) => key in env)
        .map((key) => [key, env[key]]),
    );
  }

  it.each(TOOLS)('gives %s exactly the four caller-verification values', (name) => {
    const template = synth();
    expect(callerVariables(template, name)).toStrictEqual({
      ADL_TOKEN_ISSUER: { Ref: ssmParameter(template, paramPath('dev', 'tokenIssuerUrl')) },
      ADL_TOKEN_SIGNING_KEY_ARN: {
        Ref: ssmParameter(template, paramPath('dev', 'tokenSigningKeyArn')),
      },
      ADL_GATEWAY_TOKEN_AUDIENCE: gatewayTokenAudience('dev'),
      ADL_AGENT_CLIENT_ID: agentClientId('dev'),
    });
  });

  it.each(TOOLS)('lets %s read the public key of exactly the signing key', (_name, role) => {
    const template = synth();
    const keyStatements = statementsForRole(template, role).filter((statement) =>
      statementActions(statement).some((action) => action.startsWith('kms:')),
    );
    expect(keyStatements).toStrictEqual([
      {
        Sid: 'ReadCallerVerificationKey',
        Effect: 'Allow',
        Action: 'kms:GetPublicKey',
        Resource: { Ref: ssmParameter(template, paramPath('dev', 'tokenSigningKeyArn')) },
      },
    ]);
  });

  it('grants kms:GetPublicKey to no role but the two tools', () => {
    const template = synth();
    expect(statementsForRole(template, 'GatewayInterceptor').flatMap(statementActions)).not.toContain(
      'kms:GetPublicKey',
    );
    const holders = Object.values(resources(template))
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .filter((resource) =>
        JSON.stringify((resource.Properties ?? {}).PolicyDocument).includes('kms:GetPublicKey'))
      .flatMap((resource) => ((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? [])
      .map((role) => role.Ref.replace(/ServiceRole.*$/, ''))
      .sort();
    expect(holders).toEqual(['CatalogTool', 'MeasurementTool']);
  });
});

describe('the IAM boundary between the two functions', () => {
  it('grants the measurement function the V2 current read actions', () => {
    const actions = roleActions(synth(), 'MeasurementTool');
    for (const required of [
      'athena:StartQueryExecution',
      'athena:GetQueryResults',
      'glue:GetTable',
      'glue:GetPartitions',
      's3:GetObject',
      'dynamodb:TransactGetItems',
    ]) {
      expect(actions).toContain(required);
    }
  });

  it('grants measurement a scope-current Query for governed window defaulting', () => {
    // An unnamed reporting window is answered for the latest published coverage, which is a
    // coverage-inventory read on the scope-current index; without the grant the defaulting
    // read fails live while every unit test stays green.
    const statements = statementsForRole(synth(), 'MeasurementTool').filter((statement) =>
      JSON.stringify(statement.Action).includes('dynamodb:Query'),
    );
    expect(
      statements.some((statement) =>
        JSON.stringify(statement.Resource).includes('/index/scope-current'),
      ),
    ).toBe(true);
  });

  it('scopes measurement S3 to current, successful, and interactive prefixes', () => {
    const template = synth();
    const measurement = statementsForRole(template, 'MeasurementTool');
    const semantic = dataBucketName(TEST_CONFIG, 'semantic-v2');
    const results = dataBucketName(TEST_CONFIG, 'v2-results');
    expect(JSON.stringify(measurement)).toContain(
      semanticV2AthenaWorkgroupName('dev', 'interactive'),
    );
    expect(JSON.stringify(measurement)).toContain(semanticV2DatabaseName('dev', 'current'));

    const semanticObjects = measurement.filter(
      (statement) =>
        hasExactActions(statement, ['s3:GetObject']) &&
        JSON.stringify(statement.Resource).includes(`${semantic}/canonical/current/*`),
    );
    expect(semanticObjects).toHaveLength(1);
    expect(statementResourceTexts(semanticObjects[0] ?? {})).toEqual([
      expect.stringContaining(`${semantic}/canonical/current/*`),
      expect.stringContaining(`${semantic}/immutable/canonical/successful/*`),
    ]);
    expect(semanticObjects[0]?.Condition).toBeUndefined();

    const semanticList = measurement.filter(
      (statement) =>
        hasExactActions(statement, ['s3:ListBucket']) &&
        statement.Condition !== undefined &&
        JSON.stringify(statement.Resource).includes(semantic),
    );
    expect(semanticList).toHaveLength(1);
    expect(statementResourceTexts(semanticList[0] ?? {})).toHaveLength(1);
    expect(statementResourceTexts(semanticList[0] ?? {})[0]).not.toContain(`${semantic}/`);
    expect(semanticList[0]?.Condition).toEqual({
      StringLike: { 's3:prefix': ['canonical/current/*'] },
    });

    const resultObjects = measurement.filter(
      (statement) =>
        hasExactActions(statement, [
          's3:GetObject',
          's3:PutObject',
          's3:AbortMultipartUpload',
        ]) && JSON.stringify(statement.Resource).includes(`${results}/interactive/*`),
    );
    expect(resultObjects).toHaveLength(1);
    expect(statementResourceTexts(resultObjects[0] ?? {})).toEqual([
      expect.stringContaining(`${results}/interactive/*`),
    ]);
    expect(resultObjects[0]?.Condition).toBeUndefined();

    const resultList = measurement.filter(
      (statement) =>
        hasExactActions(statement, ['s3:ListBucket']) &&
        statement.Condition !== undefined &&
        JSON.stringify(statement.Resource).includes(results),
    );
    expect(resultList).toHaveLength(1);
    expect(statementResourceTexts(resultList[0] ?? {})).toHaveLength(1);
    expect(statementResourceTexts(resultList[0] ?? {})[0]).not.toContain(`${results}/`);
    expect(resultList[0]?.Condition).toEqual({
      StringLike: { 's3:prefix': ['interactive/*'] },
    });

    const locations = measurement.filter((statement) =>
      hasExactActions(statement, ['s3:GetBucketLocation']),
    );
    expect(locations).toHaveLength(1);
    expect(statementResourceTexts(locations[0] ?? {})).toEqual([
      expect.stringContaining(semantic),
      expect.stringContaining(results),
    ]);
    for (const resource of statementResourceTexts(locations[0] ?? {})) {
      expect(resource).not.toContain(`${semantic}/`);
      expect(resource).not.toContain(`${results}/`);
    }

    const rendered = JSON.stringify(measurement);
    for (const forbidden of [
      `${semantic}/source-aligned/*`,
      `${semantic}/canonical/candidate/*`,
      `${semantic}/immutable/source-aligned/*`,
      `${semantic}/immutable/canonical/candidate/*`,
      `${results}/batch/*`,
      `${results}/*`,
    ]) {
      expect(rendered).not.toContain(forbidden);
    }
    expect(roleActions(template, 'CatalogTool').some((action) => action.startsWith('s3:'))).toBe(
      false,
    );
  });

  it('uses deploy-time SSM values without granting either Lambda Parameter Store access', () => {
    const template = synth();
    expect(roleActions(template, 'MeasurementTool')).not.toContain('ssm:GetParameter');
    expect(roleActions(template, 'CatalogTool')).not.toContain('ssm:GetParameter');
  });

  it('grants no write to Glue or to the V2 current objects', () => {
    // A read path holding a write grant is how a schema changes without a deploy.
    const actions = policyActions(synth());
    for (const forbidden of [
      'glue:UpdateTable',
      'glue:CreateTable',
      'glue:DeleteTable',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    ]) {
      expect(actions).not.toContain(forbidden);
    }
  });

  it('grants only measurement an exact page-context GetItem', () => {
    const measurement = statementsForRole(synth(), 'MeasurementTool').filter((statement) =>
      JSON.stringify(statement.Resource).includes('PageContextTable'),
    );
    expect(measurement).toHaveLength(1);
    expect(measurement[0]?.Action).toEqual('dynamodb:GetItem');

    const catalog = statementsForRole(synth(), 'CatalogTool').filter((statement) =>
      JSON.stringify(statement.Resource).includes('PageContextTable'),
    );
    expect(catalog).toEqual([]);
  });

  it('transaction-reads only V2 publications and no other semantic authority', () => {
    const rendered = policyText(synth());
    for (const kind of ['attempts', 'leases'] as const) {
      expect(rendered).not.toContain(semanticV2StateTableName('dev', kind));
    }
    const publications = semanticV2StateTableName('dev', 'publications');
    const statements = allStatements(synth()).filter((statement) =>
      JSON.stringify(statement.Resource).includes(publications),
    );
    // Exact table reads, plus the scope-current index Query that governed window
    // defaulting needs. Nothing writes and nothing touches attempts or leases.
    expect(statements).toHaveLength(2);
    const actions = statements.flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );
    expect(actions.sort()).toEqual([
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:TransactGetItems',
    ]);
    const queryStatement = statements.find((statement) =>
      JSON.stringify(statement.Action).includes('dynamodb:Query'),
    );
    expect(JSON.stringify(queryStatement?.Resource)).toContain('/index/scope-current');
  });

  it('grants no query authority on the V2 batch workgroup', () => {
    const rendered = policyText(synth());
    expect(rendered).not.toContain(semanticV2AthenaWorkgroupName('dev', 'batch'));
  });

  it('grants the catalog function telemetry export and nothing else', () => {
    /**
     * The asymmetry, asserted by what is granted rather than by whether anything is.
     *
     * This used to assert that `catalog` carried *no* inline policy at all, which held while it
     * needed no AWS access whatsoever. Span export changed that: a hop with no exported span is a
     * hole in every trace, and a missing hop reads as a fast hop — the opposite of the truth when it
     * is the slow one. So the function now carries exactly one grant.
     *
     * Rewritten rather than deleted, and the new form is stronger. "No policy" was a proxy for the
     * thing that actually matters — that capability introspection can never reach the lake — and a
     * proxy stops protecting anything the moment a legitimate grant arrives. This states the real
     * rule: whatever `catalog` is allowed to do, it is never allowed to read data.
     */
    const template = synth();
    const catalogActions = Object.values(resources(template))
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .filter((resource) =>
        (((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? []).some((role) =>
          /CatalogTool/.test(role.Ref),
        ),
      )
      .flatMap((resource) => {
        const document = (resource.Properties ?? {}).PolicyDocument as {
          Statement?: { Action?: string | string[] }[];
        };
        return (document.Statement ?? []).flatMap((statement) =>
          Array.isArray(statement.Action) ? statement.Action : [statement.Action ?? ''],
        );
      });

    // Plus the caller-verification key read every tool holds (ADR-077 §5), which reads no data,
    // and stream writes on its own log group only (ADR-085; log-write-scope.test.ts pins the shape).
    expect(catalogActions.toSorted()).toEqual([
      'kms:GetPublicKey',
      'logs:CreateLogStream',
      'logs:PutLogEvents',
      'xray:PutTelemetryRecords',
      'xray:PutTraceSegments',
    ]);
    // Catalog reads only bundled declarations. It has neither analytical-data nor DynamoDB access.
    for (const action of catalogActions) {
      expect(action).not.toMatch(/^(athena|dynamodb|glue|s3|lakeformation):/);
    }

    // Retained from the original: each tool function carries exactly one inline policy, so a second
    // one appearing on either is a change somebody has to justify rather than one that slips in.
    const roles = policyRoleRefs(template);
    expect(roles.filter((role) => /CatalogTool/.test(role))).toHaveLength(1);
    expect(roles.filter((role) => /MeasurementTool/.test(role))).toHaveLength(1);
  });

  it('grants no wildcard resource except where the service supports none', () => {
    /**
     * One exemption, named by action rather than waived generally.
     *
     * `xray:PutTraceSegments` and `xray:PutTelemetryRecords` operate on no resource — a statement
     * scoping either to an ARN simulates as `implicitDeny` [verified 2026-08-23 with
     * `iam simulate-custom-policy`], so `*` is the only form that works. Exempting them by name
     * keeps the check meaningful for every other action, which is the point: a blanket waiver here
     * would silently permit the next `s3:*` on `*`.
     */
    const scopeless = [
      'xray:PutTraceSegments',
      'xray:PutTelemetryRecords',
      // Same class, same evidence. `bedrock:InvokeGuardrailChecks` takes its guardrail
      // configuration inline rather than naming a guardrail resource, so there is no ARN to scope
      // to: the statement scoped to a guardrail ARN simulates as `implicitDeny` and on `*` as
      // `allowed` [verified 2026-08-24 with `iam simulate-custom-policy`]. Named individually
      // rather than by relaxing the rule, so the check keeps meaning something for the rest.
      'bedrock:InvokeGuardrailChecks',
    ];

    for (const resource of Object.values(resources(synth()))) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      const document = (resource.Properties ?? {}).PolicyDocument as {
        Statement?: { Action?: string | string[]; Resource?: unknown }[];
      };
      for (const statement of document.Statement ?? []) {
        const actions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action ?? ''];
        if (actions.every((action) => scopeless.includes(action))) continue;
        expect(statement.Resource).not.toBe('*');
        expect(statement.Resource).not.toEqual(['*']);
      }
    }
  });
});

describe('the gateway and its targets', () => {
  it('declares one gateway and one target per domain', () => {
    // One target per *domain*, not per tool. Gateway prefixes the tool name with the target name,
    // and the function dispatches on what is left, so five tools reach two targets.
    const template = synth();
    template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
    template.resourceCountIs('AWS::BedrockAgentCore::GatewayTarget', 2);
  });

  it('names each target the prefix the handler strips', () => {
    /**
     * Load-bearing, not cosmetic.
     *
     * Gateway sends `${targetName}___${toolName}`, and `gateway.strip_target_prefix` removes it.
     * Renaming a target changes what arrives on the wire, so the names are asserted here rather
     * than left to a reader to notice.
     */
    const names = Object.values(resources(synth()))
      .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::GatewayTarget')
      .map((resource) => String((resource.Properties ?? {}).Name));
    expect(names.sort()).toEqual(['catalog', 'measurement']);
  });

  it('carries every generated tool definition, split by domain', () => {
    /**
     * Read from the staged artefact, so this asserts the exact file the deploy reads.
     *
     * The names come from that file rather than from a literal list here. A hand-written list
     * asserted only the tools somebody remembered to add to it — three measurement tools shipped
     * while this named one, and the two it omitted could have been dropped from the template
     * without failing. The split is on the name prefix because that is already how ownership is
     * decided: a tool belongs to the handler whose enum contains its name.
     */
    const declared: string[] = (
      JSON.parse(fs.readFileSync(TOOL_SCHEMA, 'utf8')) as { tools: { name: string }[] }
    ).tools.map((tool) => tool.name);
    const rendered = JSON.stringify(synth().toJSON());

    expect(declared.length).toBeGreaterThan(0);
    expect(declared.filter((name) => name.startsWith('measurement_')).length).toBeGreaterThan(0);
    // ADR-083: the finalizer is retired, so every declared tool is catalog or measurement.
    expect(
      declared.filter((name) => !name.startsWith('catalog_') && !name.startsWith('measurement_')),
    ).toEqual([]);
    expect(rendered).not.toContain('presentation_publish');
    for (const tool of declared) {
      expect(rendered).toContain(tool);
    }
  });

  it('never puts the clock or the correlation id in an input schema', () => {
    // A model-supplied correlation id is fabricable (G-36) and a model-supplied computed_at would
    // let it date an answer to an instant the answer was not computed at. The Python declaration
    // refuses both at construction; this is the same assertion on what actually reaches AWS.
    const rendered = JSON.stringify(synth().toJSON());
    expect(rendered).not.toContain('correlation_id');
    expect(rendered).not.toContain('computed_at');
  });

});

describe('the gateway authorizer and REQUEST interceptor (ADR-077 §4, §5)', () => {
  /** The one resource of a type, as its logical id and properties. */
  function onlyResource(template: Template, type: string): [string, Record<string, unknown>] {
    const found = Object.entries(resources(template)).filter(([, resource]) => resource.Type === type);
    expect(found).toHaveLength(1);
    const [id, resource] = found[0] as [string, { Properties?: Record<string, unknown> }];
    return [id, resource.Properties ?? {}];
  }

  function functionLogicalId(template: Template, functionName: string): string {
    const found = Object.entries(resources(template))
      .filter(([, resource]) => resource.Type === 'AWS::Lambda::Function')
      .filter(([, resource]) => resource.Properties?.FunctionName === functionName)
      .map(([id]) => id);
    expect(found).toHaveLength(1);
    return found[0] as string;
  }

  function ssmParameterFor(template: Template, parameterPath: string): string {
    const parameters = (template.toJSON().Parameters ?? {}) as Record<string, Record<string, unknown>>;
    const found = Object.entries(parameters)
      .filter(([, parameter]) =>
        parameter.Type === 'AWS::SSM::Parameter::Value<String>' && parameter.Default === parameterPath)
      .map(([id]) => id);
    expect(found).toHaveLength(1);
    return found[0] as string;
  }

  const INTERCEPTOR = 'adlytics-dev-GatewayInterceptor';

  it('admits only a token-service token for the Gateway audience and the agent client', () => {
    /**
     * Compared exactly. The discovery URL is the issuer read from SSM plus the well-known path, so
     * a Cognito discovery URL, a Harness audience, an added client, or a dropped list each fails.
     * The switch from `AWS_IAM` is an in-place update of the same gateway.
     */
    const template = synth();
    const [, gateway] = onlyResource(template, 'AWS::BedrockAgentCore::Gateway');
    const issuer = ssmParameterFor(template, paramPath('dev', 'tokenIssuerUrl'));
    expect(gateway.AuthorizerType).toBe('CUSTOM_JWT');
    expect(gateway.AuthorizerConfiguration).toStrictEqual({
      CustomJWTAuthorizer: {
        DiscoveryUrl: {
          'Fn::Join': ['', [{ Ref: issuer }, '/.well-known/openid-configuration']],
        },
        AllowedAudience: [gatewayTokenAudience('dev')],
        AllowedClients: [agentClientId('dev')],
      },
    });
    expect(gatewayTokenAudience('dev')).toBe('adlytics-dev-gateway');
    expect(agentClientId('dev')).toBe('adlytics-dev-agent');
  });

  it('declares exactly one REQUEST interceptor, passing headers, on the interceptor function', () => {
    const template = synth();
    const [, gateway] = onlyResource(template, 'AWS::BedrockAgentCore::Gateway');
    expect(gateway.InterceptorConfigurations).toStrictEqual([
      {
        InputConfiguration: { PassRequestHeaders: true },
        InterceptionPoints: ['REQUEST'],
        Interceptor: {
          Lambda: { Arn: { 'Fn::GetAtt': [functionLogicalId(template, INTERCEPTOR), 'Arn'] } },
        },
      },
    ]);
  });

  it('lets the gateway role invoke the interceptor in exactly one statement', () => {
    const template = synth();
    const [, gateway] = onlyResource(template, 'AWS::BedrockAgentCore::Gateway');
    const gatewayRole = (gateway.RoleArn as { 'Fn::GetAtt': [string, string] })['Fn::GetAtt'][0];
    const interceptorId = functionLogicalId(template, INTERCEPTOR);
    const gatewayStatements = Object.values(resources(template))
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .filter((resource) =>
        (((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? []).some(
          (role) => role.Ref === gatewayRole,
        ),
      )
      .flatMap(
        (resource) =>
          ((resource.Properties ?? {}).PolicyDocument as { Statement: Record<string, unknown>[] })
            .Statement,
      );
    const onInterceptor = gatewayStatements.filter((statement) =>
      JSON.stringify(statement.Resource).includes(interceptorId),
    );
    const interceptorArn = { 'Fn::GetAtt': [interceptorId, 'Arn'] };
    expect(onInterceptor).toStrictEqual([
      {
        Action: 'lambda:InvokeFunction',
        Effect: 'Allow',
        Resource: [interceptorArn, { 'Fn::Join': ['', [interceptorArn, ':*']] }],
      },
    ]);

    // No other role's policy names it, and no resource policy opens it to another principal.
    const elsewhere = Object.values(resources(template))
      .filter((resource) => resource.Type === 'AWS::IAM::Policy')
      .filter(
        (resource) =>
          !(((resource.Properties ?? {}).Roles as { Ref: string }[]) ?? []).some(
            (role) => role.Ref === gatewayRole,
          ),
      )
      .filter((resource) => JSON.stringify(resource.Properties).includes(interceptorId));
    expect(elsewhere).toEqual([]);
    const permissions = Object.values(resources(template))
      .filter((resource) => resource.Type === 'AWS::Lambda::Permission')
      .filter((resource) => JSON.stringify(resource.Properties).includes(interceptorId));
    expect(permissions).toEqual([]);
  });

  it('builds the interceptor arm64 with the tracing environment, its handler, and 5 s', () => {
    const fn = toolFunction(synth(), INTERCEPTOR);
    expect(fn.Handler).toBe('adapters.inbound.gateway_interceptor.handler.handler');
    expect(fn.Architectures).toStrictEqual(['arm64']);
    expect(fn.Runtime).toBe('python3.14');
    expect(fn.Timeout).toBe(5);
    expect(fn.MemorySize).toBe(512);
    expect((fn.Environment as { Variables: Record<string, string> }).Variables).toStrictEqual(
      tracingEnvironment(TEST_CONFIG),
    );
    expect(JSON.stringify(fn.Layers)).toContain('dependencieslayerarn');
  });

  it('gives the interceptor span export and no other grant', () => {
    // It reads a bearer; any data, key, or AgentCore grant here would make that bearer's holder
    // a principal with more than the tools' authority.
    // Stream writes on its own log group only (ADR-085; log-write-scope.test.ts pins the shape).
    expect(roleActions(synth(), 'GatewayInterceptor').toSorted()).toStrictEqual([
      'logs:CreateLogStream',
      'logs:PutLogEvents',
      'xray:PutTelemetryRecords',
      'xray:PutTraceSegments',
    ]);
  });
});

describe('the gateway policy engine (ADR-041 layer 2)', () => {
  /**
   * Enabled, in a region that supports it.
   *
   * `TEST_CONFIG` is `eu-west-1`, which does not — so every test here moves the region as well,
   * and the pairing is what keeps the region gate exercised rather than theoretical.
   */
  const enabled = (): Template =>
    synth(
      configWith({
        region: 'us-east-1',
        gatewayGuardrails: { enabled: true, enforcement: 'LOG_ONLY' },
      }),
    );

  it('creates nothing at all when the feature is off', () => {
    // The shipped state. Zero rather than "not configured": a policy engine nobody asked for is a
    // resource with a cost and a blast radius, and the flag is what a customer sets deliberately.
    const template = synth();
    template.resourceCountIs('AWS::BedrockAgentCore::PolicyEngine', 0);
    template.resourceCountIs('AWS::BedrockAgentCore::Policy', 0);
    const guardrailChecks = allStatements(template).filter((entry) =>
      statementActions(entry).includes('bedrock:InvokeGuardrailChecks'),
    );
    expect(guardrailChecks).toEqual([]);
  });

  it('says so in an output when the feature is off', () => {
    // A stated absence rather than a missing output. "Is layer 2 running" should be answerable
    // from the console without reading the template.
    const outputs = synth().findOutputs('OutGatewayPolicyEngine');
    expect((outputs.OutGatewayPolicyEngine as { Value?: unknown }).Value).toBe('(not enabled)');
  });

  it('creates one engine and one policy when enabled', () => {
    // One policy: the base permit. A content-inspecting policy was written and probed and cannot
    // exist — a guardrail cannot be scoped to a Lambda target on an MCP gateway (ADR-042).
    const template = enabled();
    template.resourceCountIs('AWS::BedrockAgentCore::PolicyEngine', 1);
    template.resourceCountIs('AWS::BedrockAgentCore::Policy', 1);
  });

  it('writes no guardrail statement, because none can be scoped to our targets', () => {
    /**
     * The assertion that keeps a negative finding from being quietly reversed.
     *
     * A `when guardrails` statement needs a `context.{input|output}.<field>` data path, and a
     * target action on an MCP gateway declares no context fields at all — every path tried,
     * including a deliberately bogus one and one backed by a declared `outputSchema`, failed
     * identically `[verified 2026-08-24 against dev]`. So a guardrail policy here does not fail a
     * review, it fails a deploy.
     *
     * If this test ever fails, either AWS changed the surface or someone reintroduced a statement
     * that cannot stabilise. ADR-042 is the decision to reopen.
     */
    const rendered = JSON.stringify(enabled().toJSON());
    expect(rendered).not.toContain('when guardrails');
    expect(rendered).not.toContain('BedrockGuardrails::');
    expect(rendered).not.toContain('suppressOutput');
  });

  it('renders policyEngineConfiguration onto the gateway through the escape hatch', () => {
    /**
     * The assertion the escape hatch exists for.
     *
     * The `Gateway` L2 does not render this property, so it is set on `node.defaultChild`. An
     * escape hatch that silently stops applying is worse than not having one: the control
     * disappears while the code that appears to install it is still there and still compiles. This
     * asserts the property reaches the template, which is the only thing that proves it applied.
     */
    enabled().hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
      PolicyEngineConfiguration: {
        Mode: 'LOG_ONLY',
        Arn: Match.anyValue(),
      },
    });
  });

  it('ships the base permit ACTIVE, and the gateway LOG_ONLY, which are different switches', () => {
    /**
     * Two independent switches. The gateway carries `LOG_ONLY` or `ENFORCE`; each policy carries
     * its own `enforcementMode` whose CloudFormation default is `ACTIVE`. ADR-041 said "enforcement
     * mode is configuration" before that distinction was known, so both are set explicitly.
     *
     * The base permit is `ACTIVE` deliberately: Cedar is forbid-wins, so a permit cannot weaken
     * anything, while a base permit in `LOG_ONLY` would be inert exactly when a promotion to
     * `ENFORCE` needs it.
     */
    const policies = Object.values(resources(enabled()))
      .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::Policy')
      .map((resource) => resource.Properties ?? {});
    expect(policies).toHaveLength(1);
    expect(String(policies[0]?.Name)).toBe(gatewayPolicyName('dev', 'base_permit'));
    expect(String(policies[0]?.EnforcementMode)).toBe('ACTIVE');
  });

  it('suppresses analyzer findings only where being permissive is the purpose', () => {
    /**
     * The Cedar analyzer refuses the base permit under the default `FAIL_ON_ANY_FINDINGS` as
     * "Overly Permissive" `[verified 2026-08-24]`, and the finding is correct — that statement is
     * deliberately permissive because Cedar denies by default and something has to permit.
     *
     * Asserted rather than left implicit because the waiver must stay attached to the one policy
     * that earns it. AWS's own walkthrough sets `IGNORE_ALL_FINDINGS` on every policy, which would
     * mean a genuinely malformed statement shipped silently.
     */
    const policies = Object.values(resources(enabled()))
      .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::Policy')
      .map((resource) => resource.Properties ?? {});
    expect(String(policies[0]?.ValidationMode)).toBe('IGNORE_ALL_FINDINGS');
  });

  it('permits by default in the base policy, because Cedar denies by default', () => {
    const statements = Object.values(resources(enabled()))
      .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::Policy')
      .map((resource) => JSON.stringify((resource.Properties ?? {}).Definition));

    expect(
      statements.some((s) =>
        s.includes('permit (principal, action, resource is AgentCore::Gateway)'),
      ),
    ).toBe(true);
  });

  it('puts the base permit in the cedar definition form', () => {
    /**
     * `cedar` and `policy` are two languages, not one property spelled two ways.
     *
     * Both are `{ statement: string }` on the CloudFormation type, which is what made them look
     * interchangeable — ADR-041 recorded them as such. `cedar` is strict Cedar and rejects
     * `when guardrails` with *"unexpected token `guardrails`"*; `policy` is AgentCore's dialect and
     * is the only one that admits it `[verified 2026-08-24]`. The base permit is strict Cedar and
     * gains nothing from the superset.
     */
    const definitions = Object.values(resources(enabled()))
      .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::Policy')
      .map((resource) => (resource.Properties ?? {}).Definition as Record<string, unknown>);
    expect(definitions[0]).toHaveProperty('Cedar');
  });

  it('names the engine and every policy in the only form the API accepts', () => {
    /**
     * `^[A-Za-z][A-Za-z0-9_]*$` — letters, digits and underscores, starting with a letter. No
     * hyphens, which every other name this repository derives uses.
     *
     * **Asserted here because the first attempt was rejected by change-set validation rather than
     * by synth** [verified 2026-08-24]. That is the expensive place to learn it: the template is
     * valid, `tsc` is happy, the tests pass, and the failure arrives against a live account. The
     * pattern is now a synth-time assertion, and `policyEngineName`/`gatewayPolicyName` are the
     * only places the transformation happens.
     */
    const acceptable = /^[A-Za-z][A-Za-z0-9_]*$/;
    const named = Object.values(resources(enabled()))
      .filter(
        (resource) =>
          resource.Type === 'AWS::BedrockAgentCore::Policy' ||
          resource.Type === 'AWS::BedrockAgentCore::PolicyEngine',
      )
      .map((resource) => String((resource.Properties ?? {}).Name));

    expect(named).toHaveLength(2);
    for (const name of named) {
      expect(name).toMatch(acceptable);
    }
    // The helpers themselves, against an environment name that contains a hyphen — which
    // `assertValidEnvName` permits and which is what makes the substitution necessary rather than
    // cosmetic.
    expect(policyEngineName('acme-prod')).toMatch(acceptable);
    expect(gatewayPolicyName('acme-prod', 'base_permit')).toMatch(acceptable);
  });

  it('grants the gateway role what the engine needs, and nothing that writes', () => {
    /**
     * Two grants for two reasons: reading the engine and its policies at attach time, and calling
     * Bedrock Guardrails at evaluation time. Both were found from a real failure or a simulation
     * rather than from the CDK types, and AWS's own documentation covers them with
     * `bedrock-agentcore:*` on `*`.
     *
     * The absence assertions are the substance. A gateway role that could create, update or delete
     * a policy could change the control that governs it, which is the one thing the layer must not
     * be able to do to itself.
     */
    const actions = policyActions(enabled());
    expect(actions).toContain('bedrock-agentcore:GetPolicyEngine');
    expect(actions).toContain('bedrock-agentcore:AuthorizeAction');
    expect(actions).toContain('bedrock:InvokeGuardrailChecks');

    // No read grant on individual policies. `AuthorizeAction` is the whole data plane and the
    // engine reads its own policies server-side, so a `GetPolicy` here would be a permission
    // nothing exercises.
    expect(actions).not.toContain('bedrock-agentcore:GetPolicy');
    expect(actions).not.toContain('bedrock-agentcore:ListPolicySummaries');

    for (const forbidden of [
      'bedrock-agentcore:CreatePolicy',
      'bedrock-agentcore:UpdatePolicy',
      'bedrock-agentcore:DeletePolicy',
      'bedrock-agentcore:UpdatePolicyEngine',
      'bedrock-agentcore:DeletePolicyEngine',
      'bedrock-agentcore:StartPolicyGeneration',
    ]) {
      expect(actions).not.toContain(forbidden);
    }
  });

  it('isolates one scopeless guardrail-check grant when enabled', () => {
    const guardrailChecks = allStatements(enabled()).filter((entry) =>
      statementActions(entry).includes('bedrock:InvokeGuardrailChecks'),
    );
    expect(guardrailChecks).toHaveLength(1);
    expect(statementActions(guardrailChecks[0] ?? {})).toEqual([
      'bedrock:InvokeGuardrailChecks',
    ]);
    expect(guardrailChecks[0]?.Resource).toBe('*');
  });

  it('scopes the engine read to the engine', () => {
    // Not `*`, and deliberately not the individual policy ARNs either: naming those would make the
    // role's policy depend on the policies, which depend on the gateway, which must depend on the
    // role's policy. See the construct for the cycle.
    const readStatement = allStatements(enabled()).find((statement) => {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      return actions.includes('bedrock-agentcore:GetPolicyEngine');
    });
    expect(readStatement).toBeDefined();
    expect(readStatement?.Resource).not.toBe('*');
    expect(JSON.stringify(readStatement?.Resource)).toContain('PolicyEngineArn');
  });

  it('makes the gateway wait for the grant it validates the engine under', () => {
    /**
     * The dependency a template review cannot catch.
     *
     * Without it CloudFormation updates the gateway and the role's policy in parallel, the gateway
     * validates the engine before the statement authorising it exists, and the update fails with
     * `AccessDenied` on `GetPolicyEngine` — with the grant plainly present in the template. Two
     * deploys established that `[verified 2026-08-24]`.
     */
    const template = enabled();
    const gateway = Object.entries(resources(template)).find(
      ([, resource]) => resource.Type === 'AWS::BedrockAgentCore::Gateway',
    );
    const dependsOn = (gateway?.[1] as { DependsOn?: string[] | string } | undefined)?.DependsOn;
    const names = Array.isArray(dependsOn) ? dependsOn : [dependsOn ?? ''];
    expect(names.some((name) => name.includes('ServiceRoleDefaultPolicy'))).toBe(true);
  });

  it('refuses at synth in a region where the control would do nothing', () => {
    /**
     * The `kms_cmk` shape. A policy engine in an unsupported region deploys, reports healthy and
     * evaluates nothing, so the failure is silent and arrives as an absence much later.
     *
     * `eu-west-1` is `TEST_CONFIG`'s own region and is genuinely unsupported
     * [verified 2026-08-24], so this is not a contrived value.
     */
    expect(() =>
      synth(configWith({ gatewayGuardrails: { enabled: true, enforcement: 'LOG_ONLY' } })),
    ).toThrow(/does not support guardrails in AgentCore Policy/);
  });

  it('retains nothing when enabled either', () => {
    // The removal suite below runs against the default config, where none of this exists. Neither
    // the engine nor a policy may arrive carrying a RETAIN policy into a DESTROY stack.
    const retained = Object.entries(resources(enabled()))
      .filter(([, r]) => (r as { DeletionPolicy?: string }).DeletionPolicy === 'Retain')
      .map(([name, r]) => `${name} (${r.Type})`);
    expect(retained).toEqual([]);
  });
});

describe('removal', () => {
  it('retains nothing', () => {
    /**
     * DESTROY throughout, and this caught a real one.
     *
     * Everything here is rebuildable from source, and the stores it reads live in DataV2Stack,
     * which is RETAIN. The Gateway L2's *default* authorizer creates its own
     * Cognito user pool with `DeletionPolicy: Retain` — a RETAIN resource in a DESTROY stack, which
     * the composition rule forbids because a stack is only as safe as its most dangerous member.
     * An explicit authorizer (`GatewayAuthorizer.usingCustomJwt`, ADR-077 §4) is what keeps this
     * passing.
     */
    const retained = Object.entries(resources(synth()))
      .filter(([, resource]) => (resource as { DeletionPolicy?: string }).DeletionPolicy === 'Retain')
      .map(([name, resource]) => `${name} (${resource.Type})`);
    expect(retained).toEqual([]);
  });

  it('creates no Cognito user pool', () => {
    // AuthStack owns the one user pool, and it is RETAIN because it holds real accounts. A second
    // one arriving from a construct default is a resource nobody chose.
    synth().resourceCountIs('AWS::Cognito::UserPool', 0);
  });
});

describe('parameters', () => {
  it('publishes the page-context table name for API consumers', () => {
    const published = Object.values(resources(synth())).find(
      (resource) =>
        resource.Type === 'AWS::SSM::Parameter' &&
        resource.Properties?.Name === paramPath('dev', 'pageContextTableName'),
    );
    expect(published).toBeDefined();
    expect(JSON.stringify(published?.Properties?.Value)).toContain('PageContextTable');
  });

});

describe('the route Guardrail intervention alarm', () => {
  type Query = {
    Id: string;
    Expression?: string;
    MetricStat?: { Metric: { Namespace: string; MetricName: string; Dimensions: unknown[] } };
  };

  function alarm(template: Template): Record<string, unknown> {
    const found = Object.values(template.findResources('AWS::CloudWatch::Alarm')).map(
      (resource) => (resource as { Properties: Record<string, unknown> }).Properties,
    );
    // The route alarm, and one function-errors alarm per function (alarm-coverage.test.ts).
    expect(found.map((properties) => properties.AlarmName).sort()).toEqual([
      'adlytics-dev-CatalogTool-function-errors',
      'adlytics-dev-GatewayInterceptor-function-errors',
      'adlytics-dev-MeasurementTool-function-errors',
      'adlytics-dev-guardrail-intervention-rate',
    ]);
    return found.find((properties) => properties.AlarmName === 'adlytics-dev-guardrail-intervention-rate')!;
  }

  it('alarms on the share of questions declined, above a volume floor', () => {
    const properties = alarm(synth());
    expect(properties).toMatchObject({
      AlarmName: 'adlytics-dev-guardrail-intervention-rate',
      Threshold: 0.2,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    });
    const queries = properties.Metrics as Query[];
    const expression = queries.find((query) => query.Expression !== undefined);
    expect(expression?.Expression).toBe(
      'IF(invocations >= 20, intervened / invocations, 0)',
    );
    const names = queries
      .filter((query) => query.MetricStat !== undefined)
      .map((query) => [query.Id, query.MetricStat!.Metric.MetricName]);
    expect(names.sort()).toEqual([
      ['intervened', 'InvocationsIntervened'],
      ['invocations', 'Invocations'],
    ]);
  });

  it('counts only this guardrail and the version the route applies', () => {
    const template = synth();
    const guardrailId = Object.keys(template.findResources('AWS::Bedrock::Guardrail'))[0]!;
    for (const query of alarm(template).Metrics as Query[]) {
      if (query.MetricStat === undefined) continue;
      expect(query.MetricStat.Metric.Namespace).toBe('AWS/Bedrock/Guardrails');
      expect(query.MetricStat.Metric.Dimensions).toEqual(
        expect.arrayContaining([
          { Name: 'GuardrailArn', Value: { 'Fn::GetAtt': [guardrailId, 'GuardrailArn'] } },
          { Name: 'GuardrailVersion', Value: 'DRAFT' },
        ]),
      );
    }
  });

  it('notifies the shared alarm topic', () => {
    const actions = JSON.stringify(alarm(synth()).AlarmActions);
    expect(actions).toMatch(/SsmParameterValue.*ingestion.*alarm.*topic/i);
  });
});
describe('outputs', () => {
  it('names both functions for an operator', () => {
    const template = synth();
    expect(template.findOutputs('OutMeasurementFunctionName')).toBeDefined();
    const catalogOutput = template.findOutputs('OutCatalogFunctionName')
      .OutCatalogFunctionName as { Description?: string } | undefined;
    expect(catalogOutput?.Description).toContain('No business-data reads');
  });

  it('creates no CloudFormation export', () => {
    // An export cannot be changed or deleted while another stack imports it, which is the deadlock
    // ADR-012 replaced with SSM.
    const outputs = synth().toJSON().Outputs as Record<string, { Export?: unknown }>;
    for (const output of Object.values(outputs ?? {})) {
      expect(output.Export).toBeUndefined();
    }
  });

  it('matches nothing unexpected', () => {
    synth().hasOutput('OutMeasurementFunctionName', Match.objectLike({}));
  });
});
