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
import {
  DemandForecastStack,
  forecastImageExcludes,
  processingJobPrefix,
} from '../lib/stacks/demand-forecast-stack';
import { DemandForecastDataStack } from '../lib/stacks/demand-forecast-data-stack';
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

  test('the input builder holds no forecast-table grant and reads only the batch workgroup', () => {
    const list = statements(template, 'ForecastInputFunctionExecutionRole');
    const athena = list.find((statement) =>
      ([] as string[]).concat(statement.Action).includes('athena:StartQueryExecution'),
    );
    expect(JSON.stringify(athena?.Resource)).toContain('workgroup/adlytics-dev-v2-batch');
    expect(JSON.stringify(list)).not.toContain('athena:GetQueryResults');
  });
});

describe('DemandForecastDataStack', () => {
  test('the processing bucket is retained, versioned, private and expires runs/', () => {
    const app = new cdk.App();
    const stack = new DemandForecastDataStack(app, 'DemandForecastData', {
      config: TEST_CONFIG,
      env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
    });
    const template = Template.fromStack(stack);
    template.hasResource('AWS::S3::Bucket', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        BucketName: `adlytics-dev-demand-forecast-${TEST_CONFIG.account}`,
        VersioningConfiguration: { Status: 'Enabled' },
        PublicAccessBlockConfiguration: Match.objectLike({ BlockPublicAcls: true }),
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({
              Id: 'expire-forecast-runs',
              Prefix: 'runs/',
              ExpirationInDays: 90,
              NoncurrentVersionExpiration: { NoncurrentDays: 90 },
            }),
          ]),
        },
      }),
    });
  });
});
