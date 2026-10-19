// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  evaluationInputDeadLetterQueueName,
  evaluationOutboxDispatcherFunctionName,
  evaluationResultIngressV3FunctionName,
  evaluationResultLinkerV3FunctionName,
  evaluationResultProjectionV3DeadLetterQueueName,
  evaluationWriterFunctionName,
  ParamKey,
  paramPath,
} from '../config';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';
import {
  EvaluationV4PromptPaths,
  evaluatorV4Name,
  evaluatorV4Specs,
  EvaluatorV4Spec,
} from './evaluation-v4-stack';

/**
 * Where AgentCore publishes one metric per evaluator, named by the evaluator's name, for verdicts
 * that carry a numeric score only: a categorical evaluator has no series [verified 2026-09-29,
 * dev]. Used here for the average score of the two numerical scales.
 */
export const EVALUATION_METRIC_NAMESPACE = 'Bedrock-AgentCore/Evaluations';

/**
 * The result linker's own count of every newly linked verdict, by `Dimension` and `Label` and by
 * `Dimension` alone (`report_evaluation_verdict`). Covers all three evaluators uniformly.
 */
export const VERDICT_METRIC = { namespace: 'Adlytics', name: 'EvaluationVerdict' } as const;

/** The dispatch outcomes the no-dispatch alarm reads; `EvaluationDispatchOutcome` in Python. */
export const DISPATCH_OUTCOMES = {
  dispatched: 'dispatched',
  notEnabled: 'evaluation_not_enabled',
} as const;

/** A scale label in the form the linker stores and counts it, e.g. `Partially Grounded`. */
export function normalizedLabel(label: string): string {
  return label.trim().toLowerCase().split(/\s+/).join('_');
}

/**
 * The no-dispatch alarm (ADR-079 §3): turns admitted, none dispatched, and none withheld as not
 * enabled. A disabled family withholds each admitted row as `evaluation_not_enabled`, so it does
 * not satisfy this. What does: a dispatcher that has stopped running, whether or not a family is
 * enabled; an enabled family's dispatcher that sends nothing; and a period in which every admitted
 * row was withheld because the answer was not received or its delivery never recorded.
 */
export const NO_DISPATCH_EXPRESSION =
  'IF(FILL(admitted, 0) > 0 AND FILL(dispatched, 0) == 0 AND FILL(notEnabled, 0) == 0, 1, 0)';

/**
 * One evaluator's worst-label share, or no value at all below the minimum verdict count: with no
 * false branch, CloudWatch returns no data point [verified 2026-09-29, CloudWatch metric math
 * `IF`], and the alarm then reads insufficient data rather than OK.
 */
export function worstLabelShareExpression(minimumVerdicts: number, idSuffix = ''): string {
  const [verdicts, worst] = [`verdicts${idSuffix}`, `worst${idSuffix}`];
  return `IF(${verdicts} >= ${minimumVerdicts}, FILL(${worst}, 0) / ${verdicts})`;
}

export interface EvaluationQualityStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** The same prompt files `EvaluationV4Stack` reads, so both derive the same evaluator names. */
  readonly promptPaths: EvaluationV4PromptPaths;
}

/**
 * Evaluation quality over time (ADR-079 §3, §5): a dashboard, one worst-label alarm per current
 * evaluator, and the no-dispatch alarm.
 *
 * The scores are AgentCore's own per-evaluator metrics; nothing here re-emits them. The list and
 * drill-down surface is the review API's `turns` and `turn` routes (ADR-078), never this
 * dashboard: it carries no turn identifier. Last in DEPLOY_ORDER, because it names what the
 * evaluation stacks before it deploy. Destroyable: it holds no record.
 */
export class EvaluationQualityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: EvaluationQualityStackProps) {
    super(scope, id, props);
    const { config } = props;
    if (!config.onlineEvaluation.provisioned) {
      throw new Error('EvaluationQualityStack requires online evaluation.');
    }
    const read = (key: ParamKey): string =>
      ssm.StringParameter.valueForStringParameter(this, paramPath(config.envName, key));
    const serviceName = read('evaluationInputServiceName');
    const alarmAction = new actions.SnsAction(
      sns.Topic.fromTopicArn(this, 'AlarmTopic', read('ingestionAlarmTopicArn')),
    );
    const named = (slug: string): string => `adlytics-${config.envName}-evaluation-${slug}`;

    const evaluators = evaluatorV4Specs(config, props.promptPaths).map((spec) => ({
      spec,
      name: evaluatorV4Name(config, spec),
    }));
    const quality = config.evaluationQuality;
    const period = cdk.Duration.hours(quality.worstLabel.periodHours);
    const verdictMetric = (dimension: string, label?: string): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: VERDICT_METRIC.namespace,
        metricName: VERDICT_METRIC.name,
        dimensionsMap: { Dimension: dimension, ...(label === undefined ? {} : { Label: label }) },
        statistic: cloudwatch.Stats.SUM,
        period,
      });
    const scoreMetric = (name: string): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: EVALUATION_METRIC_NAMESPACE,
        metricName: name,
        dimensionsMap: { 'service.name': serviceName },
        statistic: cloudwatch.Stats.AVERAGE,
        period: cdk.Duration.hours(1),
        label: 'average score',
      });

    const worstShare = new Map<string, cloudwatch.MathExpression>();
    const alarms: cloudwatch.Alarm[] = [];
    // One graph shows every evaluator's share, and metric ids must be unique within a graph, so
    // each evaluator's ids carry its index; each alarm holds one evaluator and needs none.
    const share = (spec: EvaluatorV4Spec, idSuffix: string): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression: worstLabelShareExpression(quality.worstLabel.minimumVerdicts, idSuffix),
        usingMetrics: {
          [`verdicts${idSuffix}`]: verdictMetric(spec.corpusDimension),
          [`worst${idSuffix}`]: verdictMetric(spec.corpusDimension, normalizedLabel(spec.alarmLabel)),
        },
        label: `${spec.corpusDimension}: ${spec.alarmLabel} share`,
        period,
      });
    for (const [index, { spec }] of evaluators.entries()) {
      worstShare.set(spec.corpusDimension, share(spec, `_${index}`));
      const alarm = new cloudwatch.Alarm(this, `${spec.id}WorstLabel`, {
        alarmName: named(`${spec.corpusDimension.replace(/_/g, '-')}-worst-label`),
        alarmDescription:
          `More than ${quality.worstLabel.shareThreshold * 100}% of ${spec.corpusDimension} ` +
          `verdicts were ${spec.alarmLabel} over ${quality.worstLabel.periodHours} hours. List the ` +
          'turns through the review API `turns` route. A trend, never a release gate.',
        metric: share(spec, ''),
        threshold: quality.worstLabel.shareThreshold,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        // Fewer verdicts than the minimum is no evidence either way (agent-evaluation-validity).
        treatMissingData: cloudwatch.TreatMissingData.MISSING,
      });
      alarm.addAlarmAction(alarmAction);
      alarms.push(alarm);
    }

    const noDispatch = this.noDispatchAlarm(config, named);
    noDispatch.addAlarmAction(alarmAction);
    alarms.push(noDispatch);

    this.dashboard(config, { evaluators, verdictMetric, scoreMetric, worstShare, alarms });
    applyStandardTags(this, config);
    applyRemovalPolicy(this, cdk.RemovalPolicy.DESTROY);
  }

  private noDispatchAlarm(config: AppConfig, named: (slug: string) => string): cloudwatch.Alarm {
    const { periodMinutes, periods } = config.evaluationQuality.noDispatch;
    const period = cdk.Duration.minutes(periodMinutes);
    const adlytics = (metricName: string, outcome?: string): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: 'Adlytics',
        metricName,
        ...(outcome === undefined ? {} : { dimensionsMap: { Outcome: outcome } }),
        statistic: cloudwatch.Stats.SUM,
        period,
      });
    return new cloudwatch.Alarm(this, 'NoDispatch', {
      alarmName: named('no-dispatch'),
      alarmDescription:
        `Released turns were admitted to the evaluation outbox and none was dispatched, in each of ` +
        `${periods} consecutive ${periodMinutes}-minute periods, and none was withheld as not ` +
        'enabled (ADR-079 §3). Check the dashboard coverage graph first: every row withheld as ' +
        'answer not received or delivery unknown also satisfies this; otherwise the dispatcher is ' +
        'not running or not sending.',
      metric: new cloudwatch.MathExpression({
        expression: NO_DISPATCH_EXPRESSION,
        usingMetrics: {
          admitted: adlytics('EvaluationAdmission'),
          dispatched: adlytics('EvaluationDispatch', DISPATCH_OUTCOMES.dispatched),
          notEnabled: adlytics('EvaluationDispatch', DISPATCH_OUTCOMES.notEnabled),
        },
        label: 'Admitted, none dispatched',
        period,
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: periods,
      datapointsToAlarm: periods,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }

  private dashboard(
    config: AppConfig,
    parts: {
      evaluators: readonly { spec: EvaluatorV4Spec; name: string }[];
      verdictMetric: (dimension: string, label?: string) => cloudwatch.Metric;
      scoreMetric: (name: string) => cloudwatch.Metric;
      worstShare: Map<string, cloudwatch.MathExpression>;
      alarms: cloudwatch.Alarm[];
    },
  ): void {
    const hour = cdk.Duration.hours(1);
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `adlytics-${config.envName}-evaluation-quality`,
      defaultInterval: cdk.Duration.days(7),
    });
    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        width: 24,
        height: 2,
        markdown:
          '## Evaluation quality (ADR-079)\n' +
          'Verdict counts are the result linker\'s, per evaluator dimension and label; average ' +
          'scores are AgentCore\'s, for the two numerical scales. To find and read a turn, use ' +
          'the review API `turns` and `turn` routes.',
      }),
    );
    // One row per evaluator: verdicts by label, and the average score where the scale has one.
    for (const { spec, name } of parts.evaluators) {
      const labels = spec.scale.options.map((option) => option.label);
      const byLabel = new cloudwatch.GraphWidget({
        title: `${spec.corpusDimension}: verdicts by label`,
        width: spec.scale.kind === 'numerical' ? 12 : 24,
        stacked: true,
        left: labels.map((label) =>
          parts.verdictMetric(spec.corpusDimension, normalizedLabel(label)).with({ period: hour, label }),
        ),
      });
      if (spec.scale.kind === 'numerical') {
        dashboard.addWidgets(
          byLabel,
          new cloudwatch.GraphWidget({
            title: `${spec.corpusDimension}: average score`,
            width: 12,
            leftYAxis: { min: 0, max: 1 },
            left: [parts.scoreMetric(name)],
          }),
        );
      } else {
        dashboard.addWidgets(byLabel);
      }
    }
    const adlytics = (metricName: string, label: string, outcome?: string): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: 'Adlytics',
        metricName,
        ...(outcome === undefined ? {} : { dimensionsMap: { Outcome: outcome } }),
        statistic: cloudwatch.Stats.SUM,
        period: hour,
        label,
      });
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Worst-label share per evaluator (alarm input)',
        width: 12,
        leftYAxis: { min: 0, max: 1 },
        left: [...parts.worstShare.values()],
      }),
      new cloudwatch.GraphWidget({
        title: 'Coverage: admitted, dispatched, withheld, verdicts',
        width: 12,
        left: [
          adlytics('EvaluationAdmission', 'admitted'),
          adlytics('EvaluationDispatch', 'dispatched', 'dispatched'),
          adlytics('EvaluationDispatch', 'not enabled', 'evaluation_not_enabled'),
          adlytics('EvaluationDispatch', 'answer not received', 'answer_not_received'),
          adlytics('EvaluationDispatch', 'delivery unknown', 'delivery_unknown'),
          adlytics('EvaluationDispatch', 'retry', 'retry'),
        ],
        right: [
          ...parts.evaluators.map(({ spec }) =>
            parts
              .verdictMetric(spec.corpusDimension)
              .with({ period: hour, label: `${spec.corpusDimension} verdicts` }),
          ),
          // A verdict from an evaluator outside the closed vocabulary: an in-flight result of a
          // retired family while it drains, or a naming drift the parity test should have caught.
          parts.verdictMetric('other').with({ period: hour, label: 'unrecognized evaluator' }),
        ],
      }),
    );
    const lambdaErrors = [
      evaluationOutboxDispatcherFunctionName(config.envName),
      evaluationWriterFunctionName(config.envName),
      evaluationResultIngressV3FunctionName(config.envName),
      evaluationResultLinkerV3FunctionName(config.envName),
    ].map(
      (functionName) =>
        new cloudwatch.Metric({
          namespace: 'AWS/Lambda',
          metricName: 'Errors',
          dimensionsMap: { FunctionName: functionName },
          statistic: cloudwatch.Stats.SUM,
          period: hour,
          label: functionName.replace(`adlytics-${config.envName}-`, ''),
        }),
    );
    const deadLetters = [
      evaluationInputDeadLetterQueueName(config.envName),
      evaluationResultProjectionV3DeadLetterQueueName(config.envName),
    ].map(
      (queueName) =>
        new cloudwatch.Metric({
          namespace: 'AWS/SQS',
          metricName: 'ApproximateNumberOfMessagesVisible',
          dimensionsMap: { QueueName: queueName },
          statistic: cloudwatch.Stats.MAXIMUM,
          period: hour,
          label: queueName.replace(`adlytics-${config.envName}-`, ''),
        }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Pipeline: function errors and dead letters',
        width: 12,
        left: lambdaErrors,
        right: deadLetters,
      }),
      new cloudwatch.AlarmStatusWidget({
        title: 'Evaluation alarms',
        width: 12,
        alarms: parts.alarms,
      }),
    );
  }
}
