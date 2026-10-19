// SPDX-License-Identifier: MIT-0
/** ADR-050 cross-account landing, Finalizer isolation, status, and onboarding outputs. */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  paramPath,
  producerRegistrationStatusTableName,
  semanticV2StateTableName,
} from '../lib/config';
import type { AppConfig, EnabledLandingConfig } from '../lib/config';
import { dataBucketName } from '../lib/mixins/storage';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { INGESTION_ERROR_OPERATIONS } from '../lib/stacks/ingestion-stage1';
import { TEST_CONFIG, configWith } from './support/config';
import { expectArrivalAlarm } from './support/async-failure';

const FINALIZER = 'adapters.inbound.producer_handlers.finalizer.handler';
const INGESTION = 'adapters.inbound.ingestion_handlers.load.handler';
const DISABLED_CONFIG: AppConfig = configWith({ landing: { enabled: false } });

function enabledLanding(config: AppConfig): EnabledLandingConfig {
  if (!config.landing.enabled) throw new Error('producer test config must enable onboarding');
  return config.landing;
}

const TEST_LANDING = enabledLanding(TEST_CONFIG);
type Props = Record<string, any>;

function synth(config: AppConfig = TEST_CONFIG): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new IngestionStack(app, 'Adlytics-Ingestion-test', {
      config,
      env: { account: config.account, region: config.region },
      sourceAssetPath: __dirname,
    }),
  );
}

function resources(template: Template, type: string): Props[] {
  return Object.values(template.findResources(type)).map(
    (resource) => (resource as Props).Properties as Props,
  );
}

function only<T>(items: readonly T[], label: string): T {
  if (items.length !== 1 || items[0] === undefined) {
    throw new Error(`expected one ${label}, found ${items.length}`);
  }
  return items[0];
}

function finalizer(template: Template): Props {
  return only(
    resources(template, 'AWS::Lambda::Function').filter((item) => item.Handler === FINALIZER),
    'Finalizer',
  );
}

function finalizerStatements(template: Template): Props[] {
  const role = ((finalizer(template).Role as Props)['Fn::GetAtt'] as string[])[0];
  return resources(template, 'AWS::IAM::Policy')
    .filter((policy) => JSON.stringify(policy.Roles).includes(`\"Ref\":\"${role}\"`))
    .flatMap((policy) => ((policy.PolicyDocument as Props).Statement ?? []) as Props[]);
}

function actions(statement: Props): string[] {
  const value = statement.Action;
  return (Array.isArray(value) ? value : [value]).filter(
    (item): item is string => typeof item === 'string',
  );
}

function actionsOn(selected: Props[], resource: string): string[] {
  return [
    ...new Set(
      selected
        .filter((statement) => JSON.stringify(statement.Resource).includes(resource))
        .flatMap(actions),
    ),
  ].sort();
}

describe('one destination-owned landing role', () => {
  it('trusts the exact external role with external id and permits only incoming PutObject', () => {
    const template = synth();
    const entries = Object.entries(template.findResources('AWS::IAM::Role'));
    const [logicalId, resource] = only(
      entries.filter(([, item]) => ((item as Props).Properties as Props).RoleName === 'adlytics-dev-landing'),
      'landing role',
    );
    const role = (resource as Props).Properties as Props;
    const trust = only(
      ((role.AssumeRolePolicyDocument as Props).Statement ?? []) as Props[],
      'landing trust',
    );
    expect((trust.Principal as Props).AWS).toBe(TEST_LANDING.pipelineRoleArn);
    expect((trust.Condition as Props).StringEquals).toEqual({
      'sts:ExternalId': TEST_LANDING.externalId,
    });
    const policy = only(
      resources(template, 'AWS::IAM::Policy').filter((item) =>
        JSON.stringify(item.Roles).includes(`\"Ref\":\"${logicalId}\"`),
      ),
      'landing policy',
    );
    const statements = ((policy.PolicyDocument as Props).Statement ?? []) as Props[];
    expect(statements.flatMap(actions)).toEqual(['s3:PutObject']);
    expect(JSON.stringify(statements)).toContain('/incoming/*');
    expect(JSON.stringify(statements)).not.toContain('/amc/*');
  });
});

describe('registration finalization is not publication authority', () => {
  it('routes exact registration keys to a separate queue and Finalizer', () => {
    const template = synth();
    const rule = only(
      resources(template, 'AWS::Events::Rule').filter(
        (item) => item.Name === 'adlytics-dev-registration-landed',
      ),
      'registration rule',
    );
    expect(((rule.EventPattern as Props).detail as Props).object.key).toEqual([
      { wildcard: 'incoming/*/*/*/registration.json' },
    ]);
    expect(JSON.stringify(rule.Targets)).toContain('RegistrationQueue');
    const work = only(
      resources(template, 'AWS::SQS::Queue').filter(
        (item) => item.QueueName === 'adlytics-dev-registration',
      ),
      'registration queue',
    );
    expect((work.RedrivePolicy as Props).maxReceiveCount).toBe(3);
    expect(finalizer(template).ReservedConcurrentExecutions).toBe(2);
    const mapping = only(
      resources(template, 'AWS::Lambda::EventSourceMapping').filter((item) =>
        JSON.stringify(item.EventSourceArn).includes('RegistrationQueue'),
      ),
      'registration event source mapping',
    );
    expect(mapping.BatchSize).toBe(1);
    expect(mapping.ScalingConfig).toEqual({ MaximumConcurrency: 2 });
  });

  it('uses inline log permissions instead of a managed Lambda policy', () => {
    const template = synth();
    const [roleLogicalId] = ((finalizer(template).Role as Props)['Fn::GetAtt'] as string[]);
    if (roleLogicalId === undefined) throw new Error('Finalizer role reference is missing');
    const role = (template.findResources('AWS::IAM::Role')[roleLogicalId] as Props)
      .Properties as Props;
    expect(role.ManagedPolicyArns).toBeUndefined();
    const selected = finalizerStatements(template);
    expect(selected.flatMap(actions)).toEqual(
      expect.arrayContaining(['logs:CreateLogStream', 'logs:PutLogEvents']),
    );
  });

  it('grants only exact incoming reads, create-once AMC writes, and status transitions', () => {
    const template = synth();
    const selected = finalizerStatements(template);
    const raw = dataBucketName(TEST_CONFIG, 'semantic-v2-raw');
    expect(actionsOn(selected, `${raw}/incoming/*`)).toEqual(['s3:GetObjectVersion']);
    expect(actionsOn(selected, `${raw}/amc/*`)).toEqual(['s3:GetObject', 's3:PutObject']);
    expect(actionsOn(selected, producerRegistrationStatusTableName('dev'))).toEqual([
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]);
    const rendered = JSON.stringify(selected);
    expect(rendered).not.toContain('athena:');
    expect(rendered).not.toContain('glue:');
    expect(rendered).not.toContain(semanticV2StateTableName('dev', 'publications'));
    for (const denied of ['s3:DeleteObject', 's3:ListBucket', 'sqs:SendMessage']) {
      expect(selected.flatMap(actions)).not.toContain(denied);
    }
  });

  it('creates destroyable operational status and publishes onboarding seams', () => {
    const template = synth();
    const table = only(resources(template, 'AWS::DynamoDB::Table'), 'registration table');
    expect(table.TableName).toBe(producerRegistrationStatusTableName('dev'));
    expect(table.KeySchema).toEqual([{ AttributeName: 'attempt_id', KeyType: 'HASH' }]);
    // O-DDB-PITR-001: the table is completion authority and cannot be rebuilt.
    expect(table.PointInTimeRecoverySpecification).toStrictEqual({ PointInTimeRecoveryEnabled: true });
    const parameters = resources(template, 'AWS::SSM::Parameter');
    expect(parameters.map((item) => item.Name).sort()).toEqual(
      [
        paramPath('dev', 'ingestionAlarmTopicArn'),
        paramPath('dev', 'landingIncomingPrefix'),
        paramPath('dev', 'landingRoleArn'),
        paramPath('dev', 'providerFenceRecoveryFunctionArn'),
        paramPath('dev', 'providerFenceRecoveryOperatorRoleArn'),
        paramPath('dev', 'registrationStatusTableName'),
        paramPath('dev', 'semanticProjectionOperatorRoleArn'),
        paramPath('dev', 'stage1PreswitchVerificationRoleArn'),
        paramPath('dev', 'semanticWriteQueueArn'),
        paramPath('dev', 'semanticWriteDlqArn'),
        paramPath('dev', 'deliveryFunctionArn'),
      ].sort(),
    );
    const environment = (finalizer(template).Environment as Props).Variables as Props;
    expect(environment.ADL_REGISTRATION_STATUS_TABLE).toBe(
      producerRegistrationStatusTableName('dev'),
    );
  });
});

describe('producer onboarding disabled', () => {
  it('omits every producer resource and keeps the complete manifest ingestion core', () => {
    const template = synth(DISABLED_CONFIG);
    const rendered = JSON.stringify(template.toJSON());

    expect(
      resources(template, 'AWS::Lambda::Function').map((item) => item.Handler).sort(),
    ).toEqual(
      [
        INGESTION,
        'adapters.inbound.ingestion_handlers.table_writer.handler',
        'adapters.jobs.plan_intent_reconciler.handler',
        'adapters.jobs.projection_handoff.handler',
        'adapters.jobs.provider_attempt_reconciler.handler',
        'adapters.jobs.provider_fence_recovery.handler',
      ].sort(),
    );
    expect(
      resources(template, 'AWS::Events::Rule').map((item) => item.Name).sort(),
    ).toEqual(
      [
        'adlytics-dev-manifest-landed',
        'adlytics-dev-PlanIntentReconcilerSchedule',
        'adlytics-dev-ProviderAttemptReconcilerSchedule',
      ].sort(),
    );
    expect(
      resources(template, 'AWS::SQS::Queue').map((item) => item.QueueName).sort(),
    ).toEqual(
      [
        'adlytics-dev-ingestion',
        'adlytics-dev-ingestion-dlq',
        'adlytics-dev-semantic-write.fifo',
        'adlytics-dev-semantic-write-dlq.fifo',
      ].sort(),
    );
    // The core three, the I2 queue-age pair, one typed-error alarm per ingestion operation
    // (Metrics Insights has no IN or OR), the operation catch-all and one function-errors alarm
    // per function; no registration alarm while onboarding is disabled.
    expect(
      resources(template, 'AWS::CloudWatch::Alarm').map((item) => item.AlarmName).sort(),
    ).toEqual(
      [
        'adlytics-dev-alarming-errors-by-operation',
        ...[
          'IngestionFunction',
          'PlanIntentReconciler',
          'ProjectionHandoffProvider',
          'ProviderAttemptReconciler',
          'ProviderFenceRecoveryFunction',
          'TableWriter',
        ].map((name) => `adlytics-dev-${name}-function-errors`),
        'adlytics-dev-ingestion-blocked',
        'adlytics-dev-ingestion-dead-lettered',
        'adlytics-dev-ingestion-oldest',
        'adlytics-dev-semantic-write-dead-lettered',
        'adlytics-dev-semantic-write-oldest',
        ...INGESTION_ERROR_OPERATIONS.map(
          (operation) => `adlytics-dev-ingestion-alarming-errors-${operation.replace(/[._]/g, '-')}`,
        ),
      ].sort(),
    );
    expect(resources(template, 'AWS::DynamoDB::Table')).toEqual([]);
    expect(resources(template, 'AWS::SSM::Parameter').map((item) => item.Name).sort()).toEqual(
      [
        paramPath('dev', 'ingestionAlarmTopicArn'),
        paramPath('dev', 'providerFenceRecoveryFunctionArn'),
        paramPath('dev', 'providerFenceRecoveryOperatorRoleArn'),
        paramPath('dev', 'semanticProjectionOperatorRoleArn'),
        paramPath('dev', 'stage1PreswitchVerificationRoleArn'),
        paramPath('dev', 'semanticWriteQueueArn'),
        paramPath('dev', 'semanticWriteDlqArn'),
        paramPath('dev', 'deliveryFunctionArn'),
      ].sort(),
    );

    expect(rendered).not.toContain('ProducerFinalizer');
    expect(rendered).not.toContain('registration-landed');
    expect(rendered).not.toContain('adlytics-dev-landing');
    expect(rendered).not.toContain(TEST_LANDING.pipelineRoleArn);
    expect(rendered).not.toContain(TEST_LANDING.externalId);
    expect(rendered).not.toContain('/incoming/*');
    expect(rendered).not.toContain(paramPath('dev', 'landingRoleArn'));
    expect(rendered).not.toContain(paramPath('dev', 'landingIncomingPrefix'));
    expect(rendered).not.toContain(paramPath('dev', 'registrationStatusTableName'));
  });
});
describe('ingestion dead-letter arrivals (ADR-133)', () => {
  it('alarms on each delivery and registration dead-letter arrival', () => {
    const template = synth().toJSON();
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-ingestion-dead-lettered',
      queueName: 'adlytics-dev-ingestion-dlq',
      staffed: { arm: 'inStackTopic', topicName: 'adlytics-dev-ingestion-alarms' },
    });
    expectArrivalAlarm(template, {
      alarmName: 'adlytics-dev-registration-dead-lettered',
      queueName: 'adlytics-dev-registration-dlq',
      staffed: { arm: 'inStackTopic', topicName: 'adlytics-dev-ingestion-alarms' },
    });
  });
});
