// SPDX-License-Identifier: MIT-0
/**
 * The demand-forecast engine's IAM and workflow (design §6e, §6d; approval I-2).
 *
 * A grant without its isolation or instance condition would let a run start an unisolated or
 * larger job; a Processing role able to write outside `candidate/` could overwrite staged input;
 * an image asset outside `image-paths.txt` would move the processor identity for code the job never
 * runs; `ProbeStart` outside dev would let a staged input bypass `SelectInput`.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { OPERATIONAL_METRICS } from '../lib/generated/operational-metrics';
import {
  DemandForecastStack,
  FORECAST_SCHEDULE,
  PUBLISHER_READ_KEYS,
  STALE_AFTER_DAYS,
  forecastImageExcludes,
  processingJobPrefix,
} from '../lib/stacks/demand-forecast-stack';
import { TEST_CONFIG, configWith } from './support/config';

type Statement = {
  Sid?: string;
  Action: string | string[];
  Resource: unknown;
  Condition?: Record<string, Record<string, unknown>>;
};

function synth(envName = 'dev'): Template {
  const app = new cdk.App();
  const config = configWith({ envName });
  const stack = new DemandForecastStack(app, 'DemandForecast', {
    config,
    env: { account: config.account, region: config.region },
    sourceAssetPath: __dirname,
  });
  return Template.fromStack(stack);
}

/**
 * Evaluate one period of a CloudWatch metric-math expression built from `IF`, `FILL`, `<`,
 * numbers and metric IDs; a metric absent from `values` has no datapoint that period.
 */
function evaluateMath(expression: string, values: Record<string, number>): number {
  const tokens = expression.match(/[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[(),<]/g) ?? [];
  let at = 0;
  const take = (expected?: string): string => {
    const token = tokens[at++];
    if (token === undefined || (expected !== undefined && token !== expected)) {
      throw new Error(`expected ${expected ?? 'a token'} at ${at - 1} in ${expression}`);
    }
    return token;
  };
  const call = (arity: number): number[] => {
    take('(');
    const args: number[] = [];
    for (let index = 0; index < arity; index += 1) {
      if (index > 0) take(',');
      args.push(compare());
    }
    take(')');
    return args;
  };
  const operand = (): number => {
    const token = take();
    if (/^\d/.test(token)) return Number(token);
    if (token === 'IF') {
      const [condition, then, otherwise] = call(3) as [number, number, number];
      return condition !== 0 ? then : otherwise;
    }
    if (token === 'FILL') {
      take('(');
      const id = take();
      take(',');
      const fallback = Number(take());
      take(')');
      return values[id] ?? fallback;
    }
    const value = values[token];
    if (value === undefined) throw new Error(`${token} has no datapoint, so the expression has none`);
    return value;
  };
  const compare = (): number => {
    const left = operand();
    if (tokens[at] !== '<') return left;
    take('<');
    return left < operand() ? 1 : 0;
  };
  const result = compare();
  if (at !== tokens.length) throw new Error(`unparsed ${tokens.slice(at).join(' ')}`);
  return result;
}

function statements(template: Template, rolePrefix: string): Statement[] {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.entries(policies)
    .filter(([id]) => id.startsWith(rolePrefix))
    .flatMap(([, policy]) => policy.Properties.PolicyDocument.Statement as Statement[]);
}

function bySid(list: Statement[], sid: string): Statement {
  const found = list.find((statement) => statement.Sid === sid);
  if (found === undefined) throw new Error(`no statement ${sid}`);
  return found;
}

function functionEnvironment(template: Template, name: string): Record<string, unknown> {
  const functions = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: name },
  });
  return Object.values(functions)[0]?.Properties.Environment.Variables as Record<string, unknown>;
}
/** IAM's StringLike: `*` matches any run of characters, `/` included. */
function glob(pattern: string): RegExp {
  const escaped = pattern.replace(/[.+^${}()|[\]\\?]/g, '\\$&').replace(/\*/g, '.*');
  return new RegExp(`^${escaped}$`);
}
function definition(template: Template): Record<string, unknown> {
  const machine = Object.values(template.findResources('AWS::StepFunctions::StateMachine'))[0];
  const body = machine?.Properties.DefinitionString;
  const joined = (body['Fn::Join'][1] as unknown[])
    .map((part) => (typeof part === 'string' ? part : 'TOKEN'))
    .join('');
  return JSON.parse(joined) as Record<string, unknown>;
}

describe('DemandForecastStack', () => {
  const template = synth();

  test('the state machine may create only isolated jobs of the configured type and runtime', () => {
    const create = bySid(statements(template, 'ForecastStateMachineRole'), 'CreateIsolatedProcessingJob');
    expect(create.Action).toBe('sagemaker:CreateProcessingJob');
    expect(create.Condition).toEqual({
      Bool: { 'sagemaker:NetworkIsolation': 'true' },
      'ForAllValues:StringEquals': { 'sagemaker:InstanceTypes': ['ml.m5.xlarge'] },
      NumericLessThanEquals: { 'sagemaker:MaxRuntimeInSeconds': 3600 },
    });
    const pass = bySid(statements(template, 'ForecastStateMachineRole'), 'PassProcessingRole');
    expect(pass.Condition).toEqual({
      StringEquals: { 'iam:PassedToService': 'sagemaker.amazonaws.com' },
    });
  });

  test('the template declares the Processing quota the deploy preflight reads', () => {
    expect(template.toJSON().Metadata).toEqual({
      'Adlytics::ServiceQuotaRequirements': [
        { serviceCode: 'sagemaker', quotaName: 'ml.m5.xlarge for processing job usage', minimum: 8 },
      ],
    });
  });
  test('the Processing role writes only candidate objects and is trusted only for its jobs', () => {
    const list = statements(template, 'ForecastProcessingRole');
    const put = list.filter((statement) => ([] as string[]).concat(statement.Action).includes('s3:PutObject'));
    expect(put).toHaveLength(1);
    expect(JSON.stringify(put[0]?.Resource)).toContain('/runs/*/shards/*/attempts/*/candidate/*');
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'adlytics-dev-demand-forecast-processing',
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: [
          Match.objectLike({
            Principal: { Service: 'sagemaker.amazonaws.com' },
            Condition: {
              StringEquals: { 'aws:SourceAccount': TEST_CONFIG.account },
              ArnLike: {
                'aws:SourceArn': Match.anyValue(),
              },
            },
          }),
        ],
      }),
    });
  });

  test('every job runs network-isolated with the artifact mounted and its shard in the name', () => {
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    const map = states.Forecast as Record<string, unknown>;
    expect(map.MaxConcurrency).toBe(8);
    const run = (map.ItemProcessor as { States: Record<string, Record<string, unknown>> }).States
      .RunShard as Record<string, unknown>;
    const args = run.Arguments as Record<string, unknown>;
    expect(args.NetworkConfig).toEqual({ EnableNetworkIsolation: true });
    expect(args.StoppingCondition).toEqual({ MaxRuntimeInSeconds: 3600 });
    expect(String(args.ProcessingJobName)).toContain("$formatNumber($states.input.shard, '000')");
    expect(run.Retry).toEqual([
      {
        ErrorEquals: ['SageMaker.AmazonSageMakerException'],
        MaxAttempts: 3,
        IntervalSeconds: 60,
        BackoffRate: 2,
        JitterStrategy: 'FULL',
      },
    ]);
    expect(String(run.Output)).toContain("'shard'");
    expect(definition(template).TimeoutSeconds).toBe(86_400);
  });

  test('every job is told the processor-code identity the input builder writes into manifests', () => {
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    const run = ((states.Forecast as Record<string, unknown>).ItemProcessor as {
      States: Record<string, Record<string, unknown>>;
    }).States.RunShard as Record<string, unknown>;
    const app = (run.Arguments as Record<string, unknown>).AppSpecification as Record<string, unknown>;
    const passed = /'--processor-code-identity', '([0-9a-f]{64})'/.exec(String(app.ContainerArguments));
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'adlytics-dev-DemandForecastInput' },
    });
    const env = Object.values(functions)[0]?.Properties.Environment.Variables;
    expect(passed?.[1]).toBe(env.ADL_DEMAND_FORECAST_PROCESSOR_CODE_IDENTITY);
  });

  test('ProbeStart exists in dev and nowhere else', () => {
    expect(definition(template).StartAt).toBe('ProbeStart');
    const staging = definition(synth('staging'));
    expect(staging.StartAt).toBe('SelectInput');
    expect(Object.keys(staging.States as object)).not.toContain('ProbeStart');
  });

  test('a job name fits SageMaker 63 characters for an 8-character env and two-digit counts', () => {
    const name = `${processingJobPrefix('abcdefgh')}${'f'.repeat(12)}-s099-r99a99`;
    expect(name.length).toBeLessThanOrEqual(63);
  });

  test('the image asset is built from exactly image-paths.txt', () => {
    const excludes = forecastImageExcludes();
    expect(excludes[0]).toBe('**');
    expect(excludes).toContain('!src/adapters/jobs/demand_forecast/requirements.lock');
    expect(excludes.every((line, index) => index === 0 || line.startsWith('!'))).toBe(true);
  });

  test('the staged image context holds exactly the listed paths', () => {
    const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'df-asset-'));
    const app = new cdk.App({ outdir });
    new DemandForecastStack(app, 'DemandForecastStaged', {
      config: TEST_CONFIG,
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
      sourceAssetPath: __dirname,
    });
    app.synth();
    const staged = fs
      .readdirSync(outdir)
      .map((name) => path.join(outdir, name))
      .find((dir) => fs.existsSync(path.join(dir, 'src/adapters/jobs/demand_forecast/Dockerfile')));
    expect(staged).toBeDefined();
    const files: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else files.push(path.relative(staged as string, full));
      }
    };
    walk(staged as string);
    const listed = forecastImageExcludes()
      .slice(1)
      .map((line) => line.slice(1));
    expect(files.sort()).toEqual([...listed].sort());
    fs.rmSync(outdir, { recursive: true, force: true });
  });

  test('the input builder reads the pointer and claims only slot markers, and emits one namespace', () => {
    const list = statements(template, 'ForecastInputFunctionExecutionRole');
    const athena = list.find((statement) =>
      ([] as string[]).concat(statement.Action).includes('athena:StartQueryExecution'),
    );
    expect(JSON.stringify(athena?.Resource)).toContain('workgroup/adlytics-dev-v2-batch');
    expect(JSON.stringify(list)).not.toContain('athena:GetQueryResults');
    expect(bySid(list, 'ReadForecastPointerAndSlot')).toMatchObject({
      Action: 'dynamodb:GetItem',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*', 'SLOT#*'] } },
    });
    expect(bySid(list, 'ClaimWeeklySlot')).toMatchObject({
      Action: 'dynamodb:PutItem',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SLOT#*'] } },
    });
    const writes = list.filter((statement) =>
      ([] as string[])
        .concat(statement.Action)
        .some((action) => /^dynamodb:(PutItem|UpdateItem|DeleteItem|BatchWriteItem|ConditionCheckItem)$/.test(action)),
    );
    expect(writes.map((statement) => statement.Sid)).toEqual(['ClaimWeeklySlot']);
    expect(bySid(list, 'EmitForecastMetrics').Condition).toEqual({
      StringEquals: { 'cloudwatch:namespace': OPERATIONAL_METRICS.ForecastRunOutcome.namespace },
    });
    const env = functionEnvironment(template, 'adlytics-dev-DemandForecastInput');
    expect(JSON.stringify(env.ADL_DEMAND_FORECAST_TABLE)).toContain('datademandforecasttablename');
  });

  test('the publisher grants are the design §6e set, statement for statement', () => {
    const list = statements(template, 'ForecastPublisherFunctionExecutionRole');
    const sids = list.map((statement) => statement.Sid).filter((sid) => sid !== undefined);
    expect(sids).toEqual([
      'ReadCandidatesManifestAndTitles',
      'WriteRosterParts',
      'ListReadKeys',
      'ReadForecastStore',
      'WriteGenerationItems',
      'CommitPointerAndReceipts',
      'CheckGenerationMeta',
      'EmitForecastMetrics',
    ]);
    expect(bySid(list, 'WriteGenerationItems')).toMatchObject({
      Action: 'dynamodb:PutItem',
      Condition: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GEN#*'] } },
    });
    expect(bySid(list, 'CommitPointerAndReceipts')).toMatchObject({
      Action: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'],
      Condition: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['SET#*'] },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      },
    });
    expect(bySid(list, 'CheckGenerationMeta')).toMatchObject({
      Action: 'dynamodb:ConditionCheckItem',
      Condition: {
        'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['GEN#*'] },
        StringEquals: { 'dynamodb:EnclosingOperation': 'TransactWriteItems' },
      },
    });
    const put = list.filter((statement) => ([] as string[]).concat(statement.Action).includes('s3:PutObject'));
    expect(put).toHaveLength(1);
    expect(JSON.stringify(put[0]?.Resource)).toContain('/runs/*/shards/*/attempts/*/roster-part.jsonl.gz');
    expect(JSON.stringify(list)).not.toContain('generations/');
    const fn = Object.values(
      template.findResources('AWS::Lambda::Function', {
        Properties: { FunctionName: 'adlytics-dev-DemandForecastPublisher' },
      }),
    )[0];
    expect(fn?.Properties.MemorySize).toBe(6144);
    expect(fn?.Properties.Timeout).toBe(900);
  });

  test('no statement in the stack writes SET# keys outside a transaction', () => {
    const all = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (policy) => policy.Properties.PolicyDocument.Statement as Statement[],
    );
    const direct = all.filter((statement) => {
      const actions = ([] as string[]).concat(statement.Action);
      const writes = actions.some((action) => action === 'dynamodb:PutItem' || action === 'dynamodb:UpdateItem');
      const keys = statement.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'];
      const reachesSet = keys === undefined || ([] as string[]).concat(keys as string[]).some((key) => key.startsWith('SET#') || key === '*');
      const enclosed = statement.Condition?.StringEquals?.['dynamodb:EnclosingOperation'] === 'TransactWriteItems';
      return writes && reachesSet && !enclosed;
    });
    expect(direct).toEqual([]);
  });

  test('the publisher may list only the keys it reads, so a missing key is NoSuchKey', () => {
    const list = bySid(statements(template, 'ForecastPublisherFunctionExecutionRole'), 'ListReadKeys');
    expect(list.Action).toBe('s3:ListBucket');
    expect(list.Condition).toEqual({ StringLike: { 's3:prefix': [...PUBLISHER_READ_KEYS] } });
    const allowed = (key: string): boolean => PUBLISHER_READ_KEYS.some((pattern) => glob(pattern).test(key));
    // Paired control: each read key is listable, a staged input shard or another bucket path is not.
    expect(allowed('runs/abc-2026-11-19/shards/0/attempts/r0a0/candidate/series.jsonl.gz')).toBe(true);
    expect(allowed('runs/abc-2026-11-19/shards/0/attempts/r0a0/roster-part.jsonl.gz')).toBe(true);
    expect(allowed('runs/abc-2026-11-19/input/r0a0/manifest.json')).toBe(true);
    expect(allowed('runs/abc-2026-11-19/input/r0a0/titles.json')).toBe(true);
    expect(allowed('runs/abc-2026-11-19/input/r0a0/target/shard=0/part-0')).toBe(false);
    expect(allowed('runs/')).toBe(false);
    expect(allowed('')).toBe(false);
    expect(allowed('other/runs/x/input/r0a0/manifest.json')).toBe(false);
  });

  test('publish and commit run after the forecast and retry the store and S3 errors', () => {
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    expect(Object.keys(states)).not.toContain('Held');
    expect(Object.keys(states)).not.toContain('Forecasted');
    expect(states.Forecast?.Next).toBe('Publish');
    expect(states.Publish?.Next).toBe('Commit');
    expect(states.Publish?.MaxConcurrency).toBe(4);
    const shard = (states.Publish?.ItemProcessor as { States: Record<string, Record<string, unknown>> })
      .States.PublishShard as Record<string, unknown>;
    const storeRetry = {
      ErrorEquals: ['ForecastStoreThrottled', 'ObjectStoreError'],
      MaxAttempts: 3,
      IntervalSeconds: 30,
      BackoffRate: 2,
    };
    expect(shard.Retry).toContainEqual(storeRetry);
    expect(states.Commit?.Retry).toContainEqual(storeRetry);
    expect(states.RecordOutcome?.Type).toBe('Task');
    expect(String((states.RecordOutcome?.Arguments as Record<string, unknown>).Payload)).toContain(
      "'redrive_count': $states.context.Execution.RedriveCount",
    );
    // Every caught failure reaches RecordOutcome; only a map failure with no shard fails.
    for (const name of ['SelectInput', 'BuildInput', 'Commit']) {
      const catches = states[name]?.Catch as Array<Record<string, unknown>>;
      expect(catches.map((item) => item.Next)).toEqual(['RecordOutcome']);
    }
    for (const name of ['Forecast', 'Publish']) {
      const catches = states[name]?.Catch as Array<Record<string, unknown>>;
      expect(catches.map((item) => [item.ErrorEquals, item.Next])).toEqual([
        [['ShardFailed'], 'RecordOutcome'],
        [['States.ALL'], 'Failed'],
      ]);
    }
  });

  test('every state name is unique across the machine, map iterations included', () => {
    // Step Functions refuses a definition that reuses a name at any depth (DUPLICATE_STATE_NAME),
    // which failed the first dev deploy of the Publish map; synth alone does not check it.
    const names: string[] = [];
    const walk = (states: Record<string, Record<string, unknown>>): void => {
      for (const [name, state] of Object.entries(states)) {
        names.push(name);
        const inner = (state.ItemProcessor ?? state.Iterator) as Record<string, unknown> | undefined;
        if (inner !== undefined) walk(inner.States as Record<string, Record<string, unknown>>);
        for (const branch of (state.Branches ?? []) as Array<Record<string, unknown>>) {
          walk(branch.States as Record<string, Record<string, unknown>>);
        }
      }
    };
    walk(definition(template).States as Record<string, Record<string, unknown>>);
    expect(names.filter((name, index) => names.indexOf(name) !== index)).toEqual([]);
    expect(names).toEqual(expect.arrayContaining(['RunShardFailed', 'PublishShardFailed']));
  });

  test('a select no-op or hold passes its run identity to its RecordOutcome receipt', () => {
    // R1-12: acceptance 2 reads the unchanged input fingerprint from both daily no-op receipts.
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    const choices = states.Decide?.Choices as Array<Record<string, unknown>>;
    const receiptOnly = choices.find((choice) => choice.Next === 'RecordOutcome');
    expect(receiptOnly?.Output).toBe(
      "{% {'step': 'select', 'outcome': $states.input.outcome, 'category': $states.input.category, 'run_identity': $states.input.run_identity} %}",
    );
    expect(String((states.RecordOutcome?.Arguments as Record<string, unknown>).Payload)).toContain(
      "'run_identity': $states.input.run_identity",
    );
  });

  test('the commit passes the operator reason select assigned, for its receipt', () => {
    // Dev loop, criterion 8: an operator RETRY_FAILED run published with no reason on its receipt.
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    const payload = (states.Commit?.Arguments as Record<string, Record<string, unknown>>).Payload;
    expect(payload?.operator_reason).toBe('{% $operator_reason %}');
    expect(
      (states.SelectInput?.Assign as Record<string, unknown> | undefined)?.operator_reason,
    ).toBe('{% $states.result.Payload.operator_reason %}');
  });

  test('the dev probe continues from Forecast into Publish and Commit', () => {
    const states = definition(template).States as Record<string, Record<string, unknown>>;
    const probe = (states.ProbeStart?.Choices as Array<Record<string, unknown>>)[0];
    expect(probe?.Next).toBe('Forecast');
    // Ruling 2026-11-16 R1-1: a probe failure is recorded against the staged fence and run.
    expect(probe?.Assign).toMatchObject({
      run_fingerprint: '{% $states.input.probe.run_fingerprint %}',
      expected_fence: '{% $states.input.probe.expected_fence %}',
      operator_reason: null,
    });
  });

  test('the daily schedule starts the one machine with its execution ID and scheduled time', () => {
    template.hasResourceProperties('AWS::Scheduler::Schedule', {
      ScheduleExpression: FORECAST_SCHEDULE,
      ScheduleExpressionTimezone: 'Etc/UTC',
      Target: Match.objectLike({
        Arn: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ':scheduler:::aws-sdk:sfn:startExecution']] },
        RetryPolicy: Match.objectLike({ MaximumRetryAttempts: 0 }),
        Input: {
          'Fn::Join': [
            '',
            [
              '{"StateMachineArn":"',
              { Ref: Match.stringLikeRegexp('^ForecastStateMachine') },
              '","Name":"<aws.scheduler.execution-id>","Input":"{\\"scheduled_time\\":\\"<aws.scheduler.scheduled-time>\\"}"}',
            ],
          ],
        },
      }),
    });
    expect(FORECAST_SCHEDULE).toBe('cron(0 13 * * ? *)');
    const list = statements(template, 'ForecastScheduleRole');
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ Sid: 'StartForecastExecution', Action: 'states:StartExecution' });
    expect(JSON.stringify(list[0]?.Resource)).toMatch(/^\{"Ref":"ForecastStateMachine[0-9A-F]+"\}$/);
  });

  test('every DemandForecast alarm reads a generated operational metric on the staffed action', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const byName = (suffix: string): Record<string, unknown> => {
      const found = Object.values(alarms).find((alarm) => alarm.Properties.AlarmName === `adlytics-dev-${suffix}`);
      if (found === undefined) throw new Error(`no alarm ${suffix}`);
      return found.Properties as Record<string, unknown>;
    };
    const names = Object.values(alarms)
      .map((alarm) => String(alarm.Properties.AlarmName))
      .filter((name) => name.startsWith('adlytics-dev-DemandForecast') && !name.endsWith('-function-errors'));
    // The publisher's Errors alarm comes from applyFunctionErrorAlarms, like the input builder's.
    expect(Object.values(alarms).map((alarm) => alarm.Properties.AlarmName)).toEqual(
      expect.arrayContaining([
        'adlytics-dev-DemandForecastInput-function-errors',
        'adlytics-dev-DemandForecastPublisher-function-errors',
      ]),
    );
    expect(names.sort()).toEqual(
      [
        'DemandForecastHeld',
        'DemandForecastInferredZeroShare',
        'DemandForecastRunFailed',
        'DemandForecastStale',
        'DemandForecastUnknownDays',
      ].map((name) => `adlytics-dev-${name}`),
    );
    const generated = (entry: { namespace: string; name: string; statistic: string }): Record<string, unknown> => ({
      Namespace: entry.namespace,
      MetricName: entry.name,
    });
    const metrics = (suffix: string) =>
      byName(suffix).Metrics as Array<{ Id: string; Expression?: string; MetricStat?: Record<string, unknown> }>;
    // A sparse metric is read through FILL(id, 0), so every day has a datapoint and the alarm
    // clears the day after its last breach.
    const filled = (
      suffix: string,
      id: string,
      entry: { namespace: string; name: string; statistic: string },
      dimensions: Record<string, unknown> = {},
    ) => {
      const list = metrics(suffix);
      expect(list).toHaveLength(2);
      expect(list.find((item) => item.Expression !== undefined)?.Expression).toBe(`FILL(${id}, 0)`);
      expect(list.find((item) => item.Id === id)?.MetricStat).toMatchObject({
        Metric: { ...generated(entry), ...dimensions },
        Stat: entry.statistic,
        Period: 86_400,
      });
      return byName(suffix);
    };
    expect(
      filled('DemandForecastHeld', 'held', OPERATIONAL_METRICS.ForecastRunOutcome, {
        Dimensions: [{ Name: 'outcome', Value: 'held_quality' }],
      }),
    ).toMatchObject({
      Threshold: 1,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    });
    expect(byName('DemandForecastStale')).toMatchObject({
      Threshold: STALE_AFTER_DAYS,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 2,
      DatapointsToAlarm: 2,
      TreatMissingData: 'breaching',
    });
    expect(filled('DemandForecastInferredZeroShare', 'share', OPERATIONAL_METRICS.InferredZeroWeekShare)).toMatchObject({
      Threshold: 5,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    });
    const failed = metrics('DemandForecastRunFailed');
    expect(failed.find((item) => item.Expression !== undefined)?.Expression).toBe(
      'FILL(failed, 0) + FILL(executionsFailed, 0) + FILL(executionsTimedOut, 0) + FILL(executionsAborted, 0)',
    );
    expect(failed.find((item) => item.Id === 'failed')?.MetricStat).toMatchObject({
      Metric: {
        ...generated(OPERATIONAL_METRICS.ForecastRunOutcome),
        Dimensions: [{ Name: 'outcome', Value: 'failed' }],
      },
      Stat: OPERATIONAL_METRICS.ForecastRunOutcome.statistic,
    });
    for (const [id, name] of [
      ['executionsFailed', 'ExecutionsFailed'],
      ['executionsTimedOut', 'ExecutionsTimedOut'],
      ['executionsAborted', 'ExecutionsAborted'],
    ] as const) {
      expect(failed.find((item) => item.Id === id)?.MetricStat).toMatchObject({
        Metric: { Namespace: 'AWS/States', MetricName: name, Dimensions: [{ Name: 'StateMachineArn', Value: { Ref: expect.stringMatching(/^ForecastStateMachine/) } }] },
      });
    }
    expect(byName('DemandForecastRunFailed')).toMatchObject({ Threshold: 1, TreatMissingData: 'notBreaching' });
    const unknown = metrics('DemandForecastUnknownDays');
    expect(unknown.find((item) => item.Expression !== undefined)?.Expression).toBe('DIFF(unknownSalesDays)');
    expect(unknown.find((item) => item.Id === 'unknownSalesDays')?.MetricStat).toMatchObject({
      Metric: generated(OPERATIONAL_METRICS.UnknownSalesDays),
      Stat: OPERATIONAL_METRICS.UnknownSalesDays.statistic,
    });
    expect(byName('DemandForecastUnknownDays')).toMatchObject({ Threshold: 0, ComparisonOperator: 'GreaterThanThreshold' });
    for (const name of names) {
      const props = Object.values(alarms).find((alarm) => alarm.Properties.AlarmName === name)?.Properties;
      expect(props?.AlarmActions).toHaveLength(1);
    }
  });

  // Ruling 2026-11-16 R1-4: a set never published must not page while its checks run, and a
  // stopped check or a stale forecast must. The synthesized expression is evaluated for one day.
  test.each([
    ['never published, checks running', { checks: 1 }, false],
    ['checks stopped', {}, true],
    ['forecast older than stale_after_days', { checks: 1, age: STALE_AFTER_DAYS + 1 }, true],
    ['fresh forecast', { checks: 1, age: 3 }, false],
  ] as const)('the Stale alarm on a day where %s', (_case, day: Record<string, number>, breaching) => {
    const alarm = Object.values(template.findResources('AWS::CloudWatch::Alarm')).find(
      (found) => found.Properties.AlarmName === 'adlytics-dev-DemandForecastStale',
    )?.Properties as { Metrics: Array<{ Id: string; Expression?: string; MetricStat?: Record<string, unknown> }>; Threshold: number };
    const ids = alarm.Metrics.filter((item) => item.Expression === undefined);
    expect(Object.fromEntries(ids.map((item) => [item.Id, item.MetricStat]))).toMatchObject({
      checks: {
        Metric: { Namespace: OPERATIONAL_METRICS.UnknownSalesDays.namespace, MetricName: OPERATIONAL_METRICS.UnknownSalesDays.name },
        Stat: 'SampleCount',
      },
      age: {
        Metric: { Namespace: OPERATIONAL_METRICS.ForecastCurrentAgeDays.namespace, MetricName: OPERATIONAL_METRICS.ForecastCurrentAgeDays.name },
        Stat: OPERATIONAL_METRICS.ForecastCurrentAgeDays.statistic,
      },
    });
    const expression = alarm.Metrics.find((item) => item.Expression !== undefined)?.Expression ?? '';
    expect(evaluateMath(expression, day) > alarm.Threshold).toBe(breaching);
  });

  // A plain sparse metric keeps a 1-of-1 alarm on its last real datapoint, because CloudWatch
  // evaluates the most recent real datapoints in its evaluation range and skips notBreaching.
  // Through FILL, a day with no datapoint reads 0, so the alarm clears a day after the breach.
  test.each([
    ['DemandForecastHeld', 'no hold', {}, false],
    ['DemandForecastHeld', 'a held run', { held: 1 }, true],
    ['DemandForecastInferredZeroShare', 'no published generation', {}, false],
    ['DemandForecastInferredZeroShare', 'a generation over 5% inferred zero', { share: 5.96 }, true],
    ['DemandForecastInferredZeroShare', 'a generation under 5% inferred zero', { share: 0.45 }, false],
  ] as const)('%s on a day with %s', (name, _case, day: Record<string, number>, breaching) => {
    const alarm = Object.values(template.findResources('AWS::CloudWatch::Alarm')).find(
      (found) => found.Properties.AlarmName === `adlytics-dev-${name}`,
    )?.Properties as { Metrics?: Array<{ Expression?: string }>; Threshold: number; ComparisonOperator: string };
    const expression = alarm.Metrics?.find((item) => item.Expression !== undefined)?.Expression;
    expect(expression).toBeDefined();
    const value = evaluateMath(expression ?? '', day);
    const breaches: Record<string, (value: number, threshold: number) => boolean> = {
      GreaterThanOrEqualToThreshold: (v, t) => v >= t,
      GreaterThanThreshold: (v, t) => v > t,
    };
    expect(breaches[alarm.ComparisonOperator]?.(value, alarm.Threshold)).toBe(breaching);
  });

  test('the Stale threshold is the contract stale_after_days', () => {
    const contract = fs.readFileSync(
      path.join(__dirname, '..', '..', 'contracts', 'forecasting', 'demand-forecast.yaml'),
      'utf8',
    );
    expect(Number(/^\s+stale_after_days:\s*(\d+)\s*$/m.exec(contract)?.[1])).toBe(STALE_AFTER_DAYS);
  });
});
