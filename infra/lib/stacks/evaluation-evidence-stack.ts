// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  EvaluationFamily,
  evaluationInputLogGroupName,
  evaluationOutboxTableName,
  evaluationResultLogGroupName,
  ParamKey,
  paramPath,
} from '../config';
import {
  RecordFamily,
  recordTable as recordTableFor,
  recordTablePolicy,
} from '../constructs/agent-quality/record-integrity';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

export interface EvaluationEvidenceStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

/** Retained released-response evidence, the durable evaluation outbox, and the evaluation record tables. */
export class EvaluationEvidenceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EvaluationEvidenceStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.evaluationInput.provisioned) {
      throw new Error('Evaluation evidence requires evaluation input provisioning.');
    }

    const inputLogs = new logs.LogGroup(this, 'EvaluationInputLogs', {
      logGroupName: evaluationInputLogGroupName(config.envName),
      retention: config.logRetentionDays,
    });
    const outbox = new dynamodb.Table(this, 'EvaluationOutbox', {
      tableName: evaluationOutboxTableName(config.envName),
      partitionKey: { name: 'admission_ref', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    outbox.addGlobalSecondaryIndex({
      indexName: 'dispatch-ready',
      partitionKey: { name: 'ready_partition', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'ready_sort', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // ADR-080 §1: one table per evaluation record family, each with exactly one writer. The key
    // schema is the audit table's, so readers change table, not query. The KEYS_ONLY streams feed
    // RecordIntegrityStack's item-change pipes (ADR-080 §4).
    const recordTable = (id: string, family: RecordFamily): dynamodb.Table => {
      const table = recordTableFor(config.envName, family);
      return new dynamodb.Table(this, id, {
        tableName: table.tableName,
        partitionKey: { name: 'audit_id', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'item_key', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.AWS_MANAGED,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        deletionProtection: true,
        // ADR-081 §5: each record expires with its turn's receipt; the writer stamps it.
        timeToLiveAttribute: 'expires_at',
        stream: dynamodb.StreamViewType.KEYS_ONLY,
        // ADR-080 §2: only the family's writer puts, nothing mutates or copies, and only this
        // stack's deployment changes the table.
        resourcePolicy: recordTablePolicy(this, table),
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
    };
    const streamArn = (table: dynamodb.Table): string => {
      if (table.tableStreamArn === undefined) throw new Error(`${table.node.id} stream is required.`);
      return table.tableStreamArn;
    };
    const deliveries = recordTable('EvaluationDeliveries', 'deliveries');
    const writerReceipts = recordTable('EvaluationWriterReceipts', 'writerReceipts');
    const verdicts = recordTable('EvaluationVerdicts', 'verdicts');

    // ADR-079, observability §3.7: one owned result group per current family, outside the
    // service-reserved prefix, so its retention and deletion protection are this stack's. No
    // `encryptionKey`: a customer-managed key does not bound the group's readers, and the gate
    // asserts that no key is associated. The evaluation service writes to an existing group as-is.
    const families: readonly (readonly [EvaluationFamily, string, ParamKey])[] =
      config.onlineEvaluation.provisioned
        ? [['v4', 'EvaluationResultsV4', 'evaluationV4ResultLogGroupName'] as const]
        : [];
    for (const [family, id, key] of families) {
      const group = new logs.LogGroup(this, id, {
        logGroupName: evaluationResultLogGroupName(config.envName, family),
        retention: config.evaluationResultRetentionDays,
        deletionProtectionEnabled: true,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      new ssm.StringParameter(this, `Param${id}`, {
        parameterName: paramPath(config.envName, key),
        stringValue: group.logGroupName,
        description: `Stack-owned ${family} online-evaluation result group (ADR-079).`,
      });
    }

    for (const [id, key, value, description] of [
      [
        'ParamLogGroup',
        'evaluationInputLogGroupName',
        inputLogs.logGroupName,
        'Retained released-response evaluation input log group.',
      ],
      [
        'ParamOutboxTable',
        'evaluationOutboxTableName',
        outbox.tableName,
        'Retained durable content-bearing evaluation outbox table.',
      ],
      [
        'ParamOutboxTableArn',
        'evaluationOutboxTableArn',
        outbox.tableArn,
        'Evaluation outbox table ARN for least-privilege producer and dispatcher access.',
      ],
      [
        'ParamDeliveriesTable',
        'evaluationDeliveriesTableName',
        deliveries.tableName,
        'Retained evaluation delivery receipts; the outbox dispatcher is the only writer.',
      ],
      [
        'ParamDeliveriesTableArn',
        'evaluationDeliveriesTableArn',
        deliveries.tableArn,
        'Evaluation deliveries table ARN for its writer and readers.',
      ],
      [
        'ParamWriterReceiptsTable',
        'evaluationWriterReceiptsTableName',
        writerReceipts.tableName,
        'Retained evaluation writer receipts; the evaluation writer is the only writer.',
      ],
      [
        'ParamWriterReceiptsTableArn',
        'evaluationWriterReceiptsTableArn',
        writerReceipts.tableArn,
        'Evaluation writer receipts table ARN for its writer and readers.',
      ],
      [
        'ParamVerdictsTable',
        'evaluationVerdictsTableName',
        verdicts.tableName,
        'Retained evaluation verdicts; the result linker is the only writer.',
      ],
      [
        'ParamVerdictsTableArn',
        'evaluationVerdictsTableArn',
        verdicts.tableArn,
        'Evaluation verdicts table ARN for its writer and readers.',
      ],
      [
        'ParamDeliveriesStreamArn',
        'evaluationDeliveriesStreamArn',
        streamArn(deliveries),
        'KEYS_ONLY evaluation deliveries stream ARN for the item-change tamper pipe.',
      ],
      [
        'ParamWriterReceiptsStreamArn',
        'evaluationWriterReceiptsStreamArn',
        streamArn(writerReceipts),
        'KEYS_ONLY evaluation writer receipts stream ARN for the item-change tamper pipe.',
      ],
      [
        'ParamVerdictsStreamArn',
        'evaluationVerdictsStreamArn',
        streamArn(verdicts),
        'KEYS_ONLY evaluation verdicts stream ARN for the item-change tamper pipe.',
      ],
    ] as const) {
      new ssm.StringParameter(this, id, {
        parameterName: paramPath(config.envName, key),
        stringValue: value,
        description,
      });
    }

    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);
  }
}
