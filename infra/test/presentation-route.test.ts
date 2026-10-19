// SPDX-License-Identifier: MIT-0
import {
  insightCacheTableName,
  pageContextTableName,
  semanticV2AthenaWorkgroupName,
  semanticV2DatabaseName,
  semanticV2StateTableName,
} from '../lib/config';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { dataBucketName } from '../lib/mixins/storage';
import { ApiStack } from '../lib/stacks/api-stack';
import { configWith, TEST_CONFIG } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';

type Props = Record<string, unknown>;

const api = everyTemplate().ApiStack as Props;
const stackResources = api.Resources as Props;

function only<T>(items: T[], what: string): T {
  expect(items).toHaveLength(1);
  const [first] = items;
  if (first === undefined) throw new Error(`expected exactly one ${what}`);
  return first;
}

function functionByName(name: string): Props {
  return only(
    Object.values(stackResources)
      .filter((resource): resource is Props => typeof resource === 'object' && resource !== null)
      .filter((resource) => resource.Type === 'AWS::Lambda::Function')
      .map((resource) => resource.Properties as Props)
      .filter((properties) => properties.FunctionName === name),
    `Lambda named ${name}`,
  );
}

function roleLogicalId(functionName: string): string {
  const role = functionByName(functionName).Role as { 'Fn::GetAtt'?: unknown[] } | undefined;
  const logicalId = role?.['Fn::GetAtt']?.[0];
  if (typeof logicalId !== 'string') throw new Error(`${functionName} has no role logical id`);
  return logicalId;
}

function statementsForFunction(functionName: string): Props[] {
  const role = roleLogicalId(functionName);
  return Object.values(stackResources)
    .filter((resource): resource is Props => typeof resource === 'object' && resource !== null)
    .filter((resource) => resource.Type === 'AWS::IAM::Policy')
    .map((resource) => resource.Properties as Props)
    .filter((properties) =>
      ((properties.Roles ?? []) as Props[]).some((candidate) => candidate.Ref === role),
    )
    .flatMap((properties) =>
      (((properties.PolicyDocument as Props).Statement ?? []) as Props[]),
    );
}

function actions(statement: Props): string[] {
  const value = statement.Action;
  return (Array.isArray(value) ? value : [value]).filter(
    (action): action is string => typeof action === 'string',
  );
}

describe('the presentation function', () => {
  it('creates exactly one configured Lambda from the shared asset and layer', () => {
    const presentation = functionByName('adlytics-dev-PresentationFunction');
    const auth = functionByName('adlytics-dev-AuthFunction');

    expect(presentation.Handler).toBe('adapters.inbound.presentation_handlers.handler.handler');
    expect(presentation.Architectures).toEqual([TEST_CONFIG.lambdaArchitecture]);
    expect(presentation.Runtime).toBe(TEST_CONFIG.pythonRuntime);
    expect(presentation.MemorySize).toBe(1024);
    // The API Gateway integration ceiling: a wide-window detail response with its bounded
    // insight first-fill was observed exhausting the previous 20s before its caller's timeout.
    expect(presentation.Timeout).toBe(29);
    // The request-path generation transport must stay inside the API envelope: the
    // constructor now admits the worker's patient 60s, so this pin is what keeps a
    // misconfigured request path from reintroducing the 2026-09-24 bodyless-500 class.
    const transport = Number(
      (presentation.Environment as { Variables: Props }).Variables
        .ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS,
    );
    expect(transport).toBeLessThanOrEqual(25);
    expect(transport).toBeLessThan(Number(presentation.Timeout));
    expect(presentation.Code).toEqual(auth.Code);
    expect(presentation.Layers).toEqual(auth.Layers);

    const variables = (presentation.Environment as { Variables: Props }).Variables;
    expect(Object.keys(variables)).not.toContain('ADL_COGNITO_CLIENT_SECRET');
    expect(variables.ADL_COGNITO_CLIENT_SECRET_ID).toBeUndefined();
    expect(JSON.stringify(variables.ADL_PAGE_CONTEXT_TABLE)).toContain(
      'SsmParameterValueadlyticsdevagentpagecontexttable',
    );
    expect(JSON.stringify(variables.ADL_SEMANTIC_V2_BUCKET)).toContain(
      'SsmParameterValueadlyticsdevdatav2bucket',
    );
    expect(JSON.stringify(variables.ADL_INSIGHT_CACHE_TABLE)).toContain(
      'SsmParameterValueadlyticsdevinsightcachetable',
    );
    expect(resourcesOfType(api, 'AWS::DynamoDB::Table')).not.toContainEqual(
      expect.objectContaining({ TableName: insightCacheTableName(TEST_CONFIG.envName) }),
    );
    expect(JSON.stringify(variables.ADL_SEMANTIC_V2_CURRENT_DATABASE)).toContain(
      'SsmParameterValueadlyticsdevdatav2canonicalcurrentgluedatabase',
    );
    expect(JSON.stringify(variables.ADL_PRESET_ARTIFACT_BUCKET)).toContain(
      'SsmParameterValueadlyticsdevpublisherartifactbucketname',
    );
    expect(JSON.stringify(variables.ADL_PRESET_ARTIFACT_POINTER_TABLE)).toContain(
      'SsmParameterValueadlyticsdevpublisherartifactpointertablename',
    );
  });

  it('names the graph table for the preset pointer cases only where the graph authority exists', () => {
    // S6 §2.9: presentation reads the mode item and `SERVING#`; the grants are §2.11 row 8.
    const graphTable = (provisioned: boolean): unknown => {
      const template = Template.fromStack(new ApiStack(new cdk.App(), 'ApiGraphTable', {
        config: configWith({
          operationalStatus: { ...TEST_CONFIG.operationalStatus, provisioned },
        }),
        env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
        sourceAssetPath: __dirname,
      })).toJSON() as Props;
      const presentation = only(
        resourcesOfType(template, 'AWS::Lambda::Function')
          .filter((fn) => fn.FunctionName === 'adlytics-dev-PresentationFunction'),
        'presentation function',
      );
      return (presentation.Environment as { Variables: Props }).Variables.ADL_EXECUTION_GRAPH_TABLE;
    };
    expect(graphTable(true)).toBe('adlytics-dev-execution-graph');
    // Control: without the graph authority no graph table is named.
    expect(graphTable(false)).toBeUndefined();
  });

  it('generates insights with the configured insight models, never the chat agent seam', () => {
    // ADR-076 Phase 1: the fixture's insight values differ from the agent values exactly
    // so a rewire back to config.agent.* fails here rather than passing coincidentally.
    const presentation = functionByName('adlytics-dev-PresentationFunction');
    const variables = (presentation.Environment as { Variables: Props }).Variables;
    expect(variables.ADL_INSIGHT_QUICK_MODEL_ID).toBe(TEST_CONFIG.insight.quickModelId);
    expect(variables.ADL_INSIGHT_DEEP_MODEL_ID).toBe(TEST_CONFIG.insight.deepModelId);
    const bedrock = statementsForFunction('adlytics-dev-PresentationFunction').filter(
      (statement) => actions(statement).includes('bedrock:InvokeModel'),
    );
    // A-IAM-MODEL-001: one statement per model; each fixture ID is bare, so in this Region only.
    expect(bedrock).toHaveLength(2);
    expect(bedrock.map((statement) => statement.Sid))
      .toEqual(['InvokeInsightModelsModel0', 'InvokeInsightModelsModel1']);
    const resources = JSON.stringify(bedrock.map((statement) => statement.Resource));
    expect(resources).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.quickModelId}`);
    expect(resources).toContain(`:eu-west-1::foundation-model/${TEST_CONFIG.insight.deepModelId}`);
    expect(resources).not.toContain('inference-profile');
    expect(resources).not.toContain(TEST_CONFIG.agent.quickChat.modelId);
    expect(resources).not.toContain(TEST_CONFIG.agent.deepDive.modelId);
  });

  it('exports spans without receiving the Cognito secret grant', () => {
    const presentation = statementsForFunction('adlytics-dev-PresentationFunction');
    const granted = presentation.flatMap(actions);
    expect(granted).toContain('xray:PutTraceSegments');
    expect(granted).toContain('xray:PutTelemetryRecords');
    expect(granted).not.toContain('secretsmanager:GetSecretValue');
  });
});

describe('the protected presentation routes', () => {
  it('creates every exact POST route on one integration and the existing authorizer', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const expected = [
      'POST /api/presentation/overview',
      'POST /api/presentation/amazon-channel-performance',
      'POST /api/presentation/campaigns',
      'POST /api/presentation/campaign-detail',
      'POST /api/presentation/campaign-metric-focus',
      'POST /api/presentation/portfolio-metric-focus',
      'POST /api/presentation/retail-metric-focus',
      'POST /api/presentation/insight-deep-dive',
      'POST /api/presentation/marketplace-comparison',
      'POST /api/presentation/data-health',
      'POST /api/presentation/retail-health',
      'POST /api/presentation/retail-health-product',
      'POST /api/presentation/scorecard',
      'POST /api/presentation/new-customers',
      'POST /api/presentation/reach',
      'POST /api/presentation/retention',
      'POST /api/presentation/audiences',
    ];
    const presentation = expected.map((key) =>
      only(routes.filter((route) => route.RouteKey === key), key),
    );

    const authorizerRef = {
      Ref: only(
        Object.entries(stackResources)
          .filter(([, resource]) => (resource as Props).Type === 'AWS::ApiGatewayV2::Authorizer')
          .map(([logicalId]) => logicalId),
        'session authorizer',
      ),
    };
    // A route is gated only when it is CUSTOM and bound to the one session authorizer.
    const ungated = (candidates: Props[]) =>
      candidates
        .filter((route) => route.AuthorizationType !== 'CUSTOM'
          || JSON.stringify(route.AuthorizerId) !== JSON.stringify(authorizerRef))
        .map((route) => route.RouteKey);
    expect(ungated(presentation)).toEqual([]);
    // Paired controls: the same check reports an Audiences route that lacks the authorizer.
    const audiences = only(
      presentation.filter((route) => route.RouteKey === 'POST /api/presentation/audiences'),
      'audiences route',
    );
    const unbound = { ...audiences };
    delete unbound.AuthorizerId;
    expect(ungated([{ ...unbound, AuthorizationType: 'NONE' }])).toEqual(['POST /api/presentation/audiences']);
    expect(ungated([unbound])).toEqual(['POST /api/presentation/audiences']);
    for (const route of presentation) {
      expect(route.Target).toEqual(presentation[0]?.Target);
    }

    const integrations = resourcesOfType(api, 'AWS::ApiGatewayV2::Integration').filter((resource) =>
      JSON.stringify(resource.IntegrationUri).includes('PresentationFunction'),
    );
    expect(integrations).toHaveLength(1);
  });

  it('serves Retail health product detail on its own path and no retired retail-product route (ADR-125)', () => {
    const keys = resourcesOfType(api, 'AWS::ApiGatewayV2::Route').map((route) => route.RouteKey);
    expect(keys).toContain('POST /api/presentation/retail-health-product');
    expect(keys.filter((key) => String(key).endsWith('/presentation/retail-product'))).toEqual([]);
  });
  it('serves the Scorecard route on the presentation authorizer, exactly as Retail health', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const authorizerIds = Object.entries(stackResources)
      .filter(([, resource]) => (resource as Props).Type === 'AWS::ApiGatewayV2::Authorizer')
      .map(([logicalId]) => logicalId);
    const authorizerId = only(authorizerIds, 'presentation authorizer');
    const route = (key: string) => only(routes.filter((item) => item.RouteKey === key), key);
    // Paired control: an existing presentation route carries the same type and authorizer.
    const control = route('POST /api/presentation/retail-health');
    expect(control.AuthorizationType).toBe('CUSTOM');
    expect(control.AuthorizerId).toEqual({ Ref: authorizerId });
    const added = route('POST /api/presentation/scorecard');
    expect(added.AuthorizationType).toBe('CUSTOM');
    expect(added.AuthorizerId).toEqual({ Ref: authorizerId });
    expect(added.AuthorizerId).toEqual(control.AuthorizerId);
    expect(added.Target).toEqual(control.Target);
  });
  it('serves the New customers route on the presentation authorizer, exactly as the Scorecard', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const route = (key: string) => only(routes.filter((item) => item.RouteKey === key), key);
    // Paired control: the Scorecard route carries the same type, authorizer and integration.
    const control = route('POST /api/presentation/scorecard');
    const added = route('POST /api/presentation/new-customers');
    expect(added.AuthorizationType).toBe('CUSTOM');
    expect(added.AuthorizerId).toEqual(control.AuthorizerId);
    expect(added.Target).toEqual(control.Target);
    // Exactly one New customers route: no GET, no second method, no prefix twin.
    const keys = routes.map((item) => String(item.RouteKey));
    expect(keys.filter((key) => key.includes('/presentation/new-customers'))).toEqual([
      'POST /api/presentation/new-customers',
    ]);
  });
  it('serves Retention on the shared presentation authorizer with no metric-focus route', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const route = (key: string) => only(routes.filter((item) => item.RouteKey === key), key);
    const control = route('POST /api/presentation/new-customers');
    const added = route('POST /api/presentation/retention');
    expect(added.AuthorizationType).toBe('CUSTOM');
    expect(added.AuthorizerId).toEqual(control.AuthorizerId);
    expect(added.Target).toEqual(control.Target);
    const keys = routes.map((item) => String(item.RouteKey));
    expect(keys.filter((key) => key.includes('/presentation/retention'))).toEqual([
      'POST /api/presentation/retention',
    ]);
  });
  it('serves Audiences on the shared presentation authorizer with no metric-focus route', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const route = (key: string) => only(routes.filter((item) => item.RouteKey === key), key);
    const control = route('POST /api/presentation/retention');
    const added = route('POST /api/presentation/audiences');
    expect(added.AuthorizationType).toBe('CUSTOM');
    expect(added.AuthorizerId).toEqual(control.AuthorizerId);
    expect(added.Target).toEqual(control.Target);
    const keys = routes.map((item) => String(item.RouteKey));
    expect(keys.filter((key) => key.includes('/presentation/audiences'))).toEqual([
      'POST /api/presentation/audiences',
    ]);
  });
  it('serves Reach on the shared presentation authorizer with no metric-focus route', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    const route = (key: string) => only(routes.filter((item) => item.RouteKey === key), key);
    // Paired control: the New customers route carries the same type, authorizer and integration.
    const control = route('POST /api/presentation/new-customers');
    const added = route('POST /api/presentation/reach');
    expect(added.AuthorizationType).toBe('CUSTOM');
    expect(added.AuthorizerId).toEqual(control.AuthorizerId);
    expect(added.Target).toEqual(control.Target);
    // Exactly one Reach route: no GET, no second method, and no metric-focus route yet.
    const keys = routes.map((item) => String(item.RouteKey));
    expect(keys.filter((key) => key.includes('/presentation/reach'))).toEqual([
      'POST /api/presentation/reach',
    ]);
  });
  it('builds no Scorecard metric focus route until slice 9b serves the focus', () => {
    const keys = resourcesOfType(api, 'AWS::ApiGatewayV2::Route').map((route) => String(route.RouteKey));
    expect(keys.filter((key) => key.endsWith('/presentation/scorecard-metric-focus'))).toEqual([]);
    // Paired control: the same filter finds the served Scorecard route.
    expect(keys.filter((key) => key.endsWith('/presentation/scorecard'))).toEqual(['POST /api/presentation/scorecard']);
  });
  it('keeps exactly one payload-v2 Cookie authorizer', () => {
    const authorizers = resourcesOfType(api, 'AWS::ApiGatewayV2::Authorizer');
    expect(authorizers).toHaveLength(1);
    expect(authorizers[0]?.AuthorizerPayloadFormatVersion).toBe('2.0');
    expect(authorizers[0]?.AuthorizerType).toBe('REQUEST');
  });
});

describe('the presentation IAM boundary', () => {
  const presentation = statementsForFunction('adlytics-dev-PresentationFunction');
  const dataPlane = presentation.filter((statement) =>
    actions(statement).some((action) => /^(athena|dynamodb|glue|s3):/.test(action)),
  );

  it('grants only the exact table and object reads/writes the handlers need', () => {
    const page = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes(pageContextTableName(TEST_CONFIG.envName)),
    );
    expect(page).toHaveLength(1);
    expect(actions(page[0] ?? {})).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem']);

    const cache = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes('SsmParameterValueadlyticsdevinsightcachetablearn'),
    );
    expect(cache).toHaveLength(1);
    expect(actions(cache[0] ?? {})).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem']);

    const registry = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes('insightpromptregistrytablearn'),
    );
    expect(registry).toHaveLength(1);
    expect(actions(registry[0] ?? {})).toEqual(['dynamodb:GetItem']);

    const publications = semanticV2StateTableName(TEST_CONFIG.envName, 'publications');
    const publication = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes(publications),
    );
    expect(publication).toHaveLength(2);
    const byAction = new Map(
      publication.map((statement) => [actions(statement).join(','), statement]),
    );
    expect(actions(byAction.get('dynamodb:GetItem,dynamodb:TransactGetItems') ?? {})).toEqual([
      'dynamodb:GetItem',
      'dynamodb:TransactGetItems',
    ]);
    expect(JSON.stringify(byAction.get('dynamodb:Query')?.Resource)).toContain(
      `table/${publications}/index/scope-current`,
    );

    const successful = `${dataBucketName(TEST_CONFIG, 'semantic-v2')}/immutable/canonical/successful/*`;
    const objects = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes(successful),
    );
    expect(objects).toHaveLength(1);
    expect(actions(objects[0] ?? {})).toEqual(['s3:GetObject']);

    const pointer = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes(
        'SsmParameterValueadlyticsdevpublisherartifactpointertablearn',
      ),
    );
    expect(pointer).toHaveLength(1);
    expect(actions(pointer[0] ?? {})).toEqual(['dynamodb:GetItem', 'dynamodb:TransactGetItems']);

    const artifactObjects = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes(
        'SsmParameterValueadlyticsdevpublisherartifactbucketname',
      ),
    );
    expect(artifactObjects).toHaveLength(1);
    expect(actions(artifactObjects[0] ?? {})).toEqual(['s3:GetObject']);
    const artifactResources = JSON.stringify(artifactObjects[0]?.Resource);
    expect(artifactResources).toContain('/pages/*');
    expect(artifactResources).toContain('/custody/*');

    // S6 part 2 (§2.11 row 8): the graph mode item and the serving bindings, GetItem only.
    const graph = dataPlane.filter((statement) =>
      JSON.stringify(statement.Resource).includes('table/adlytics-dev-execution-graph"'),
    );
    expect(graph).toEqual([
      {
        Sid: 'ReadGraphServingBindings',
        Effect: 'Allow',
        Action: 'dynamodb:GetItem',
        Resource: graph[0]?.Resource,
        Condition: {
          'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SERVING#*', 'MODE#ingestion'] },
        },
      },
    ]);
    // D7 hold: case 4 is dropped, so no REVISION# grant on any table.
    expect(JSON.stringify(dataPlane)).not.toContain('REVISION#');
    expect(dataPlane).toHaveLength(15);
    for (const statement of dataPlane) {
      expect(statement.Resource).not.toBe('*');
      expect(statement.Resource).not.toEqual(['*']);
    }
  });

  it('allows only interactive Athena, current Glue, curated S3, and scoped results access', () => {
    const granted = dataPlane.flatMap(actions);
    expect(granted).toEqual(
      expect.arrayContaining([
        'athena:StartQueryExecution',
        'athena:GetQueryExecution',
        'athena:GetQueryResults',
        'athena:StopQueryExecution',
        'glue:GetDatabase',
        'glue:GetTable',
        'glue:GetTables',
        'glue:GetPartitions',
        's3:GetObject',
        's3:ListBucket',
        's3:PutObject',
        's3:AbortMultipartUpload',
        's3:GetBucketLocation',
      ]),
    );
    const rendered = JSON.stringify(dataPlane);
    expect(rendered).toContain(
      `workgroup/${semanticV2AthenaWorkgroupName(TEST_CONFIG.envName, 'interactive')}`,
    );
    expect(rendered).toContain(
      `database/${semanticV2DatabaseName(TEST_CONFIG.envName, 'current')}`,
    );
    expect(rendered).toContain(`${dataBucketName(TEST_CONFIG, 'v2-results')}/interactive/*`);
    expect(rendered).not.toContain('/batch/*');
    expect(rendered).not.toContain('source-aligned');
    expect(rendered).not.toContain('canonical-candidate');
    for (const forbidden of [
      'dynamodb:Scan',
      'dynamodb:DeleteItem',
      'glue:CreateTable',
      'glue:UpdateTable',
      'glue:DeleteTable',
    ]) {
      expect(granted).not.toContain(forbidden);
    }
  });

  it('leaves auth and authorizer roles free of data-plane IAM', () => {
    for (const functionName of ['adlytics-dev-AuthFunction', 'adlytics-dev-AuthorizerFunction']) {
      const granted = statementsForFunction(functionName).flatMap(actions);
      expect(granted.length).toBeGreaterThan(0);
      expect(granted.some((action) => /^(athena|dynamodb|glue|s3):/.test(action))).toBe(false);
    }
  });
});
