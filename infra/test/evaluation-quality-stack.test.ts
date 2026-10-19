// SPDX-License-Identifier: MIT-0
/**
 * ADR-079 §3 and §5: the evaluation quality dashboard, the per-evaluator worst-label alarms, and
 * the no-dispatch alarm (Agent Quality Loop target acceptance item 11: the no-dispatch expression
 * has a unit test for an enabled family with completing turns and no sends).
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { paramPath } from '../lib/config';
import {
  DISPATCH_OUTCOMES,
  EVALUATION_METRIC_NAMESPACE,
  EvaluationQualityStack,
  NO_DISPATCH_EXPRESSION,
  normalizedLabel,
  VERDICT_METRIC,
  worstLabelShareExpression,
} from '../lib/stacks/evaluation-quality-stack';
import { EvaluationV4Stack, evaluatorV4Name, evaluatorV4Specs } from '../lib/stacks/evaluation-v4-stack';
import { TEST_CONFIG } from './support/config';
import { EVALUATION_CORPORA, EVALUATOR_V4_PROMPTS } from './support/templates';

const SRC = path.join(__dirname, '..', '..', 'src');

type Props = Record<string, unknown>;
type Values = Record<string, number | undefined>;

const config = {
  ...TEST_CONFIG,
  onlineEvaluation: { ...TEST_CONFIG.onlineEvaluation, provisioned: true },
};

/**
 * Evaluate one data point of a CloudWatch metric-math expression the way CloudWatch documents it:
 * `FILL(x, 0)` fills a missing point, `AND` is logical, and an `IF` with no false branch yields no
 * point. Only the operators these two expressions use are supported, so a new operator fails here.
 */
function evaluate(expression: string, values: Values): number | undefined {
  const names = ['verdicts', 'worst', 'admitted', 'dispatched', 'notEnabled'];
  const scope = Object.fromEntries(names.map((name) => [name, values[name]]));
  const js = expression.replace(/FILL\((\w+), 0\)/g, '($1 ?? 0)').replace(/\bAND\b/g, '&&');
  if (/\b(?!IF\b)[A-Z]{2,}\b/.test(js)) throw new Error(`unsupported operator in ${expression}`);
  const iff = (condition: unknown, whenTrue: number, whenFalse?: number): number | undefined =>
    condition ? whenTrue : whenFalse;
  // Sloppy-mode `with`, so each metric id reads as a variable, missing ones as undefined.
  return new Function('scope', 'IF', `with (scope) { return ${js}; }`)(scope, iff) as
    | number
    | undefined;
}

let template: Template;
beforeAll(() => {
  const app = new cdk.App();
  template = Template.fromStack(
    new EvaluationQualityStack(app, 'EvaluationQuality', {
      config,
      env: { account: config.account, region: config.region },
      promptPaths: EVALUATOR_V4_PROMPTS,
    }),
  );
});

function alarms(): Props[] {
  return Object.values(template.findResources('AWS::CloudWatch::Alarm')).map(
    (resource) => (resource as { Properties: Props }).Properties,
  );
}

describe('the no-dispatch expression', () => {
  it('fires for an enabled family with completing turns and no sends', () => {
    expect(evaluate(NO_DISPATCH_EXPRESSION, { admitted: 3 })).toBe(1);
    expect(evaluate(NO_DISPATCH_EXPRESSION, { admitted: 3, dispatched: 0, notEnabled: 0 })).toBe(1);
  });

  it('stays quiet when turns are sent, when the family is disabled, or when nothing completes', () => {
    expect(evaluate(NO_DISPATCH_EXPRESSION, { admitted: 3, dispatched: 1 })).toBe(0);
    expect(evaluate(NO_DISPATCH_EXPRESSION, { admitted: 3, notEnabled: 3 })).toBe(0);
    expect(evaluate(NO_DISPATCH_EXPRESSION, {})).toBe(0);
  });
});

describe('the worst-label share expression', () => {
  const expression = worstLabelShareExpression(5);

  it('is the worst label share once the minimum verdicts landed', () => {
    expect(evaluate(expression, { verdicts: 10, worst: 3 })).toBeCloseTo(0.3);
    expect(evaluate(expression, { verdicts: 5 })).toBe(0);
  });

  it('has no data point below the minimum, so a quiet period never reads healthy', () => {
    expect(evaluate(expression, { verdicts: 4, worst: 4 })).toBeUndefined();
    expect(evaluate(expression, {})).toBeUndefined();
  });
});

describe('EvaluationQualityStack', () => {
  const specs = evaluatorV4Specs(config, EVALUATOR_V4_PROMPTS);

  it('alarms once per current evaluator on its worst-label share, without data below the minimum', () => {
    for (const spec of specs) {
      const alarm = alarms().find(
        (candidate) =>
          candidate.AlarmName === `adlytics-dev-evaluation-${spec.corpusDimension.replace(/_/g, '-')}-worst-label`,
      );
      expect(alarm).toBeDefined();
      expect(alarm!).toEqual(
        expect.objectContaining({
          Threshold: 0.2,
          ComparisonOperator: 'GreaterThanThreshold',
          EvaluationPeriods: 1,
          TreatMissingData: 'missing',
        }),
      );
      const metrics = alarm!.Metrics as Props[];
      expect(metrics.find((metric) => metric.Expression !== undefined)!.Expression).toBe(
        worstLabelShareExpression(5),
      );
      const stat = (id: string): { Metric: Props; Stat: string; Period: number } =>
        metrics.find((metric) => metric.Id === id)!.MetricStat as {
          Metric: Props;
          Stat: string;
          Period: number;
        };
      for (const id of ['worst', 'verdicts']) {
        expect(stat(id).Stat).toBe('Sum');
        expect(stat(id).Period).toBe(6 * 3600);
        expect(stat(id).Metric).toEqual(
          expect.objectContaining({ Namespace: VERDICT_METRIC.namespace, MetricName: VERDICT_METRIC.name }),
        );
      }
      expect(stat('verdicts').Metric.Dimensions).toEqual([{ Name: 'Dimension', Value: spec.corpusDimension }]);
      expect(stat('worst').Metric.Dimensions).toEqual([
        { Name: 'Dimension', Value: spec.corpusDimension },
        { Name: 'Label', Value: normalizedLabel(spec.alarmLabel) },
      ]);
    }
  });

  it('names evaluators exactly as the evaluation stack creates them', () => {
    const app = new cdk.App();
    const v4 = Template.fromStack(
      new EvaluationV4Stack(app, 'EvaluationV4', {
        config: { ...config, transactionSearchEnabled: true },
        env: { account: config.account, region: config.region },
        promptPaths: EVALUATOR_V4_PROMPTS,
        calibrationCorpusPath: EVALUATION_CORPORA.v4,
        sourceAssetPath: __dirname,
      }),
    );
    const created = Object.values(v4.findResources('AWS::BedrockAgentCore::Evaluator')).map(
      (resource) => (resource as { Properties: Props }).Properties.EvaluatorName,
    );
    expect(specs.map((spec) => evaluatorV4Name(config, spec))).toEqual(created);
    const body = JSON.stringify(template.findResources('AWS::CloudWatch::Dashboard'));
    for (const spec of specs.filter((candidate) => candidate.scale.kind === 'numerical')) {
      expect(body).toContain(evaluatorV4Name(config, spec));
    }
    expect(body).toContain(EVALUATION_METRIC_NAMESPACE);
  });

  it('counts the same labels, and reads the same dispatch outcomes, as the Python side', () => {
    const quality = fs.readFileSync(path.join(SRC, 'domain', 'evaluation', 'quality.py'), 'utf8');
    for (const spec of specs) {
      const declared = quality.match(new RegExp(`"${spec.corpusDimension}": frozenset\\(\\s*\\{([^}]*)\\}`));
      expect(declared).not.toBeNull();
      const python = [...declared![1]!.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]).sort();
      expect(spec.scale.options.map((option) => normalizedLabel(option.label)).sort()).toEqual(python);
      expect(python).toContain(normalizedLabel(spec.alarmLabel));
      // The linker recovers the dimension from the name; the name shape must be the one it reads.
      expect(spec.dimension).toBe(`${spec.corpusDimension}_v4`);
      expect(evaluatorV4Name(config, spec)).toMatch(
        new RegExp(`^adlytics_[a-z0-9_]+?_${spec.corpusDimension}_v4_[0-9a-f]{8}$`),
      );
    }
    expect(quality).toContain('r")_v4_[0-9a-f]{8}$"');
    const reporting = fs.readFileSync(
      path.join(SRC, 'adapters', 'outbound', 'telemetry', 'reporting.py'),
      'utf8',
    );
    expect(reporting).toContain(`DISPATCHED = "${DISPATCH_OUTCOMES.dispatched}"`);
    expect(reporting).toContain(`EVALUATION_NOT_ENABLED = "${DISPATCH_OUTCOMES.notEnabled}"`);
    expect(reporting).toContain('name="EvaluationAdmission"');
    expect(reporting).toContain(`name="${VERDICT_METRIC.name}"`);
  });

  it('alarms when admitted turns go undispatched for three consecutive periods', () => {
    const alarm = alarms().find((candidate) => candidate.AlarmName === 'adlytics-dev-evaluation-no-dispatch')!;
    expect(alarm).toEqual(
      expect.objectContaining({
        Threshold: 1,
        EvaluationPeriods: 3,
        DatapointsToAlarm: 3,
        TreatMissingData: 'notBreaching',
      }),
    );
    const metrics = alarm.Metrics as Props[];
    expect(metrics.find((metric) => metric.Expression !== undefined)!.Expression).toBe(
      NO_DISPATCH_EXPRESSION,
    );
    const admitted = metrics.find((metric) => metric.Id === 'admitted')!;
    expect((admitted.MetricStat as { Metric: Props }).Metric).toEqual({
      Namespace: 'Adlytics',
      MetricName: 'EvaluationAdmission',
    });
  });

  it('sends every alarm to the published alarm topic', () => {
    expect(alarms()).toHaveLength(4);
    for (const alarm of alarms()) expect(alarm.AlarmActions).toHaveLength(1);
    const parameters = (template.toJSON().Parameters ?? {}) as Record<string, { Default?: unknown }>;
    const defaults = Object.values(parameters).map((parameter) => parameter.Default);
    expect(defaults).toContain(paramPath('dev', 'ingestionAlarmTopicArn'));
    expect(defaults).toContain(paramPath('dev', 'evaluationInputServiceName'));
  });

  it('renders one dashboard that carries no turn identifier', () => {
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
    const body = JSON.stringify(template.findResources('AWS::CloudWatch::Dashboard'));
    expect(body).toContain(VERDICT_METRIC.name);
    expect(body).not.toMatch(/audit_id|correlation_id|traceId/);
  });

  it('refuses to synthesise without online evaluation', () => {
    const app = new cdk.App();
    expect(
      () =>
        new EvaluationQualityStack(app, 'Off', {
          config: TEST_CONFIG,
          env: { account: config.account, region: config.region },
          promptPaths: EVALUATOR_V4_PROMPTS,
        }),
    ).toThrow(/requires online evaluation/);
  });
});
