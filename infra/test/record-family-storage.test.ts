// SPDX-License-Identifier: MIT-0
/**
 * ADR-080 §1 and §3: one retained table per audit and evaluation record family, each written by
 * exactly one fixed-name role that can read back its own write, and read only by the roles that
 * project it.
 */
import {
  agentQualityAuditTableName,
  agentQualityFindingsTableName,
  agentQualityProcessorRoleName,
  conversationFunctionRoleName,
  evaluationDeliveriesTableName,
  evaluationOutboxDispatcherRoleName,
  evaluationResultIngressRoleName,
  evaluationResultLinkerRoleName,
  evaluationVerdictsTableName,
  evaluationWriterReceiptsTableName,
  evaluationWriterRoleName,
  paramPath,
  ParamKey,
  qualityReviewFunctionName,
  qualityReviewFunctionRoleName,
} from '../lib/config';
import { dynamoGrants, roleLogicalId, rolesThatPutInto } from './support/iam';
import { everyTemplate, resourcesOfType } from './support/templates';

const templates = everyTemplate();
const template = (name: string): Record<string, unknown> =>
  templates[name] as Record<string, unknown>;
const path = (key: ParamKey): string => paramPath('dev', key);
const AUDIT_TABLE = `table/${agentQualityAuditTableName('dev')}`;
const AUDIT_INDEX = `${AUDIT_TABLE}/index/correlation-audit`;
const TURNS_INDEX = `${AUDIT_TABLE}/index/turns-by-day`;

const RECORD_TABLES = [
  ['AgentQualityStack', agentQualityAuditTableName('dev')],
  ['AgentQualityStack', agentQualityFindingsTableName('dev')],
  ['EvaluationEvidenceStack', evaluationDeliveriesTableName('dev')],
  ['EvaluationEvidenceStack', evaluationWriterReceiptsTableName('dev')],
  ['EvaluationEvidenceStack', evaluationVerdictsTableName('dev')],
] as const;

/** The receipts table alone carries the correlation and day indexes; their shapes are asserted by agent-quality-stack.test.ts. */
const correlationIndex = (name: string): Record<string, unknown> =>
  name === agentQualityAuditTableName('dev')
    ? {
        AttributeDefinitions: [
          { AttributeName: 'audit_id', AttributeType: 'S' },
          { AttributeName: 'item_key', AttributeType: 'S' },
          { AttributeName: 'correlation_id', AttributeType: 'S' },
          { AttributeName: 'correlation_audit_id', AttributeType: 'S' },
          { AttributeName: 'turns_day', AttributeType: 'S' },
          { AttributeName: 'turns_created_at', AttributeType: 'S' },
        ],
        GlobalSecondaryIndexes: expect.any(Array),
      }
    : {};

describe('the ADR-080 record tables', () => {
  it.each(RECORD_TABLES)('%s retains %s protected, on demand, and point-in-time recoverable', (stack, name) => {
    const resources = (template(stack).Resources ?? {}) as Record<string, Record<string, unknown>>;
    const [table] = Object.values(resources).filter(
      (resource) =>
        resource.Type === 'AWS::DynamoDB::Table'
        && (resource.Properties as Record<string, unknown>).TableName === name,
    );
    expect(table).toBeDefined();
    expect(table?.DeletionPolicy).toBe('Retain');
    expect(table?.UpdateReplacePolicy).toBe('Retain');
    expect(table?.Properties).toStrictEqual({
      TableName: name,
      KeySchema: [
        { AttributeName: 'audit_id', KeyType: 'HASH' },
        { AttributeName: 'item_key', KeyType: 'RANGE' },
      ],
      AttributeDefinitions: [
        { AttributeName: 'audit_id', AttributeType: 'S' },
        { AttributeName: 'item_key', AttributeType: 'S' },
      ],
      BillingMode: 'PAY_PER_REQUEST',
      SSESpecification: { SSEEnabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      DeletionProtectionEnabled: true,
      StreamSpecification: { StreamViewType: 'KEYS_ONLY' },
      // ADR-081 §5: every record expires on `expires_at`, whole epoch seconds, that its one
      // writer stamps from the receipt's creation time plus the declared retention.
      TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
      // ADR-080 §2; its exact statements are asserted by record-integrity.test.ts.
      ResourcePolicy: { PolicyDocument: expect.any(Object) },
      Tags: expect.any(Array),
      ...correlationIndex(name),
    });
  });

  it('publishes every table name and ARN from its owning retained stack', () => {
    const published = (stack: string): string[] =>
      resourcesOfType(template(stack), 'AWS::SSM::Parameter').map((parameter) => String(parameter.Name));
    expect(published('AgentQualityStack')).toEqual(expect.arrayContaining([
      path('agentQualityFindingsTableName'),
      path('agentQualityFindingsTableArn'),
    ]));
    expect(published('EvaluationEvidenceStack')).toEqual(expect.arrayContaining([
      path('evaluationDeliveriesTableName'),
      path('evaluationDeliveriesTableArn'),
      path('evaluationWriterReceiptsTableName'),
      path('evaluationWriterReceiptsTableArn'),
      path('evaluationVerdictsTableName'),
      path('evaluationVerdictsTableArn'),
    ]));
  });
});

describe('every table has exactly one writer, and it reads back its own write (ADR-080 §3)', () => {
  const writersOf = (paramKey: ParamKey, physical: string) =>
    rolesThatPutInto(templates, (table) =>
      table === '*' || table === path(paramKey) || table === `table/${physical}`);

  it.each([
    ['agentQualityAuditTableArn', agentQualityAuditTableName('dev'), conversationFunctionRoleName('dev')],
    ['agentQualityFindingsTableArn', agentQualityFindingsTableName('dev'), agentQualityProcessorRoleName('dev')],
    ['evaluationDeliveriesTableArn', evaluationDeliveriesTableName('dev'), evaluationOutboxDispatcherRoleName('dev')],
    ['evaluationWriterReceiptsTableArn', evaluationWriterReceiptsTableName('dev'), evaluationWriterRoleName('dev')],
    ['evaluationVerdictsTableArn', evaluationVerdictsTableName('dev'), evaluationResultLinkerRoleName('dev')],
  ] as const)('%s is written only by %s', (paramKey, physical, writer) => {
    expect(writersOf(paramKey, physical)).toStrictEqual(new Set([writer]));
  });

  it('the processor reads receipts and its stream, and writes and reads back findings', () => {
    const receipts = [path('agentQualityAuditTableArn'), path('agentQualityAuditStreamArn')];
    expect(dynamoGrants(template('AgentQualityProcessorStack'), agentQualityProcessorRoleName('dev')))
      .toStrictEqual(new Set([
        ...['GetItem', 'DescribeStream', 'GetRecords', 'GetShardIterator', 'ListStreams'].flatMap(
          (action) => receipts.map((table) => `dynamodb:${action} ${table}`),
        ),
        `dynamodb:PutItem ${path('agentQualityFindingsTableArn')}`,
        `dynamodb:GetItem ${path('agentQualityFindingsTableArn')}`,
        `dynamodb:PutItem ${path('agentQualityAggregateTableArn')}`,
        `dynamodb:GetItem ${path('agentQualityAggregateTableArn')}`,
      ]));
  });

  it('the dispatcher transitions the outbox, reads delivery dispositions, and writes and reads back delivery receipts', () => {
    const outbox = path('evaluationOutboxTableArn');
    const deliveries = path('evaluationDeliveriesTableArn');
    expect(dynamoGrants(template('EvaluationOutboxDispatchStack'), evaluationOutboxDispatcherRoleName('dev')))
      .toStrictEqual(new Set([
        // ADR-079: the ADR-063 delivery companion decides whether a row is dispatched.
        `dynamodb:GetItem ${path('agentQualityAuditTableArn')}`,
        `dynamodb:Query ${outbox}/index/dispatch-ready`,
        `dynamodb:GetItem ${outbox}`,
        `dynamodb:GetItem ${deliveries}`,
        `dynamodb:UpdateItem ${outbox}`,
        `dynamodb:PutItem ${deliveries}`,
        `dynamodb:TransactWriteItems ${outbox}`,
        `dynamodb:TransactWriteItems ${deliveries}`,
      ]));
  });

  it('the evaluation writer writes and reads back only its writer receipts', () => {
    const table = path('evaluationWriterReceiptsTableArn');
    expect(dynamoGrants(template('EvaluationInputStack'), evaluationWriterRoleName('dev')))
      .toStrictEqual(new Set([`dynamodb:PutItem ${table}`, `dynamodb:GetItem ${table}`]));
  });

  it('the result linker writes and reads back only its verdicts', () => {
    const table = path('evaluationVerdictsTableArn');
    expect(dynamoGrants(template('EvaluationResultLinkV3Stack'), evaluationResultLinkerRoleName('dev')))
      .toStrictEqual(new Set([`dynamodb:PutItem ${table}`, `dynamodb:GetItem ${table}`]));
  });
});

describe('readers hold only the reads their projection makes', () => {
  it('the result ingress reads receipts, their correlation index, writer receipts, and delivery receipts', () => {
    const receipts = path('agentQualityAuditTableArn');
    expect(dynamoGrants(template('EvaluationResultLinkV3Stack'), evaluationResultIngressRoleName('dev')))
      .toStrictEqual(new Set([
        `dynamodb:GetItem ${receipts}`,
        // ADR-079: the turn's delivery receipt admits the result and supplies its provenance.
        `dynamodb:GetItem ${path('evaluationDeliveriesTableArn')}`,
        `dynamodb:GetItem ${path('evaluationWriterReceiptsTableArn')}`,
        `dynamodb:Query ${receipts}/index/correlation-audit`,
      ]));
  });

  it('the review function reads the receipt, the day index, and only the families it projects', () => {
    // ADR-078: one function serves `turn` and `turns`. Exact reads on the receipt, delivery, and
    // writer receipt;
    // key-bounded queries on the two receipts indexes, one receipt's findings, and one verdict
    // per evaluator. No Scan, no base-table Query on receipts, and no write.
    expect(dynamoGrants(template('ApiStack'), qualityReviewFunctionRoleName('dev')))
      .toStrictEqual(new Set([
        `dynamodb:GetItem ${AUDIT_TABLE}`,
        `dynamodb:GetItem ${path('evaluationDeliveriesTableArn')}`,
        `dynamodb:GetItem ${path('evaluationWriterReceiptsTableArn')}`,
        `dynamodb:Query ${AUDIT_INDEX}`,
        `dynamodb:Query ${TURNS_INDEX}`,
        `dynamodb:Query ${path('agentQualityFindingsTableArn')}`,
        `dynamodb:Query ${path('evaluationVerdictsTableArn')}`,
      ]));
  });
});
describe('fixed role names', () => {
  it.each([
    ['AgentQualityProcessorStack', agentQualityProcessorRoleName('dev'), 'adlytics-dev-AgentQualityProcessor'],
    ['EvaluationOutboxDispatchStack', evaluationOutboxDispatcherRoleName('dev'), 'adlytics-dev-EvaluationOutboxDispatcher'],
    ['EvaluationInputStack', evaluationWriterRoleName('dev'), 'adlytics-dev-EvaluationWriter'],
    ['EvaluationResultLinkV3Stack', evaluationResultIngressRoleName('dev'), 'adlytics-dev-EvaluationResultIngressV3'],
    ['EvaluationResultLinkV3Stack', evaluationResultLinkerRoleName('dev'), 'adlytics-dev-EvaluationResultLinkerV3'],
    ['ApiStack', qualityReviewFunctionRoleName('dev'), qualityReviewFunctionName('dev')],
  ] as const)('%s runs its function under the fixed role %s', (stack, roleName, functionName) => {
    const role = roleLogicalId(template(stack), roleName);
    expect(role).toBeDefined();
    const fn = resourcesOfType(template(stack), 'AWS::Lambda::Function').find(
      (candidate) => candidate.FunctionName === functionName,
    );
    expect(fn?.Role).toStrictEqual({ 'Fn::GetAtt': [role, 'Arn'] });
  });
});

describe('each function names its family tables through published parameters', () => {
  const variables = (stack: string, functionName: string): Record<string, unknown> => {
    const fn = resourcesOfType(template(stack), 'AWS::Lambda::Function').find(
      (candidate) => candidate.FunctionName === functionName,
    );
    return ((fn?.Environment as { Variables?: Record<string, unknown> } | undefined)?.Variables ?? {});
  };
  const parameterRef = (stack: string, key: ParamKey): unknown => {
    const parameters = (template(stack).Parameters ?? {}) as Record<string, { Default?: unknown }>;
    const logicalId = Object.entries(parameters).find(([, value]) => value.Default === path(key))?.[0];
    expect(logicalId).toBeDefined();
    return { Ref: logicalId };
  };

  it.each([
    ['AgentQualityProcessorStack', 'adlytics-dev-AgentQualityProcessor', 'ADL_AGENT_QUALITY_FINDINGS_TABLE', 'agentQualityFindingsTableName'],
    ['EvaluationOutboxDispatchStack', 'adlytics-dev-EvaluationOutboxDispatcher', 'ADL_EVALUATION_DELIVERIES_TABLE', 'evaluationDeliveriesTableName'],
    ['EvaluationInputStack', 'adlytics-dev-EvaluationWriter', 'ADL_EVALUATION_WRITER_RECEIPTS_TABLE', 'evaluationWriterReceiptsTableName'],
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultIngressV3', 'ADL_EVALUATION_WRITER_RECEIPTS_TABLE', 'evaluationWriterReceiptsTableName'],
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultIngressV3', 'ADL_EVALUATION_DELIVERIES_TABLE', 'evaluationDeliveriesTableName'],
    // ADR-079: the dispatcher reads the turn's delivery companion from the receipts table.
    ['EvaluationOutboxDispatchStack', 'adlytics-dev-EvaluationOutboxDispatcher', 'ADL_AGENT_QUALITY_AUDIT_TABLE', 'agentQualityAuditTableName'],
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultLinkerV3', 'ADL_EVALUATION_VERDICTS_TABLE', 'evaluationVerdictsTableName'],
    ['ApiStack', 'adlytics-dev-QualityReview', 'ADL_AGENT_QUALITY_AUDIT_TABLE', 'agentQualityAuditTableName'],
    ['ApiStack', 'adlytics-dev-QualityReview', 'ADL_AGENT_QUALITY_FINDINGS_TABLE', 'agentQualityFindingsTableName'],
    ['ApiStack', 'adlytics-dev-QualityReview', 'ADL_EVALUATION_DELIVERIES_TABLE', 'evaluationDeliveriesTableName'],
    ['ApiStack', 'adlytics-dev-QualityReview', 'ADL_EVALUATION_WRITER_RECEIPTS_TABLE', 'evaluationWriterReceiptsTableName'],
    ['ApiStack', 'adlytics-dev-QualityReview', 'ADL_EVALUATION_VERDICTS_TABLE', 'evaluationVerdictsTableName'],
  ] as const)('%s %s reads %s from %s', (stack, functionName, variable, key) => {
    expect(variables(stack, functionName)[variable]).toStrictEqual(parameterRef(stack, key));
  });

  it.each([
    ['EvaluationInputStack', 'adlytics-dev-EvaluationWriter'],
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultLinkerV3'],
  ] as const)('%s %s no longer names the receipts table', (stack, functionName) => {
    expect(variables(stack, functionName)).not.toHaveProperty('ADL_AGENT_QUALITY_AUDIT_TABLE');
  });
});

describe('every record writer receives the declared receipt retention (ADR-081 §5)', () => {
  const variables = (stack: string, functionName: string): Record<string, unknown> => {
    const fn = resourcesOfType(template(stack), 'AWS::Lambda::Function').find(
      (candidate) => candidate.FunctionName === functionName,
    );
    expect(fn).toBeDefined();
    return ((fn?.Environment as { Variables?: Record<string, unknown> } | undefined)?.Variables ?? {});
  };
  const conversationFunction = (): Record<string, unknown> => {
    const role = roleLogicalId(template('ApiStack'), conversationFunctionRoleName('dev'));
    const fn = resourcesOfType(template('ApiStack'), 'AWS::Lambda::Function').find(
      (candidate) => JSON.stringify(candidate.Role) === JSON.stringify({ 'Fn::GetAtt': [role, 'Arn'] }),
    );
    expect(fn).toBeDefined();
    return ((fn?.Environment as { Variables?: Record<string, unknown> } | undefined)?.Variables ?? {});
  };

  it('the conversation route, which writes receipts and their companions, has it', () => {
    expect(conversationFunction().ADL_RECEIPT_RETENTION_DAYS).toBe('90');
  });

  it.each([
    ['AgentQualityProcessorStack', 'adlytics-dev-AgentQualityProcessor'],
    ['EvaluationOutboxDispatchStack', 'adlytics-dev-EvaluationOutboxDispatcher'],
    ['EvaluationInputStack', 'adlytics-dev-EvaluationWriter'],
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultLinkerV3'],
  ] as const)('%s %s has it', (stack, functionName) => {
    expect(variables(stack, functionName).ADL_RECEIPT_RETENTION_DAYS).toBe('90');
  });

  it.each([
    ['EvaluationResultLinkV3Stack', 'adlytics-dev-EvaluationResultIngressV3'],
    ['ApiStack', 'adlytics-dev-QualityReview'],
  ] as const)('the reader %s %s does not, because it compares a stored expiry with its clock', (stack, functionName) => {
    expect(variables(stack, functionName)).not.toHaveProperty('ADL_RECEIPT_RETENTION_DAYS');
  });
});
