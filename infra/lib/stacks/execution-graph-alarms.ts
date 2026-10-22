// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { AppConfig } from '../config';
import { BAKE_TASKS, PROTOCOL_VERSION } from '../generated/execution-graph-protocol';
import { ExecutionGraphAuthority } from './execution-graph-authority';

/**
 * S5: the execution-graph protocol alarms and the stuck-flow detector's read-only grant.
 *
 * Gauges come from `adapters/outbound/telemetry/graph_metrics.py`, in the `Adlytics` namespace
 * with exactly the closed dimensions `Protocol` and `AuthorityScope`. The live stuck-flow alarm
 * treats missing data as breaching: the detector emits a value only after a complete strongly
 * consistent sweep, so silence means it could not read every shard and is itself the signal.
 * Every action is the selected staffed destination, on both the `ALARM` and the `OK` transition
 * of the two stuck-flow alarms (so `ALARM -> OK` reaches the destination, P5 and P7).
 *
 * Plan 6.2 (DB26, decision S4): the four bake gauges ride the same one-minute sweep. They treat
 * missing data as missing: a sweep that cannot read emits nothing, which the live stuck-flow alarm
 * already turns into `ALARM`, so a bake alarm never reads silence as success. Addendum A2 adds two
 * event counts from the Baker (telemetry loss, trace incompleteness); an event count is absent
 * when nothing happened, so those two treat missing data as not breaching, as an arrival alarm
 * does. The bake and precompute dead letters keep their existing arrival alarms (no duplicate),
 * and there is no cleanup alarm (D10). ADR-139's `GraphServingWithheld` is a 0/1 gauge the
 * finalizer emits per finalized cohort (and the Release, `1` only, for a withheld cohort it
 * settles, ADR-140); it ignores missing data, so silence keeps the current state and only a fresh
 * non-withheld commit's `0` clears an `ALARM`. The two blocked windows derive from `BAKE_TASKS`
 * exactly as `stuck_flow.py` derives its gauges' thresholds, so the texts cannot go stale.
 */
export const GRAPH_METRIC_NAMESPACE = 'Adlytics';
export const STUCK_FLOW_METRIC = 'OpenFlowsPastProtocolCeiling';
export const UNCLOCKED_FLOW_METRIC = 'OpenFlowsWithoutRootClock';
export const PROMOTE_AGE_METRIC = 'PromoteObligationAgeSeconds';
export const BAKE_TASK_FAILED_METRIC = 'GraphBakeTaskFailed';
export const BAKE_RETRY_EXHAUSTED_METRIC = 'GraphBakeRetryExhausted';
export const BAKE_PRECOMPUTE_BLOCKED_METRIC = 'GraphBakePrecomputeBlocked';
export const BAKE_FINALIZER_BLOCKED_METRIC = 'GraphBakeFinalizerBlocked';
export const BAKE_TELEMETRY_LOSS_METRIC = 'GraphBakeTelemetryLoss';
export const BAKE_TRACE_INCOMPLETE_METRIC = 'GraphBakeTraceIncomplete';
export const SERVING_WITHHELD_METRIC = 'GraphServingWithheld';
/** ADR-142: both bake entries emit these, so they carry no `Graph` prefix. */
export const BAKE_ISOLATED_REFUSAL_LIMIT_METRIC = 'BakeIsolatedRefusalLimit';
export const BAKE_TASK_OVER_BUDGET_METRIC = 'BakeTaskOverBudget';
/** The bake rows are evaluated each sweep (one a minute). */
export const BAKE_ALARM_PERIOD_SECONDS = 60;
/**
 * DB26/TD-279: the designed longest healthy gap from precompute readiness to its contribution
 * (one dispatcher schedule, one pass, one precompute claim lease), as
 * `domain/operations/bake_scheduling.py` derives it.
 */
const PRECOMPUTE_READY_SECONDS = BAKE_TASKS.worker_schedule_seconds
  + BAKE_TASKS.worker_pass_seconds + BAKE_TASKS.precompute_claim_lease_seconds;
/** `stuck_flow.PRECOMPUTE_BLOCKED_AFTER`: readiness plus one detector period (240 s). */
export const PRECOMPUTE_BLOCKED_AFTER_SECONDS =
  PRECOMPUTE_READY_SECONDS + BAKE_TASKS.worker_schedule_seconds;
/**
 * `stuck_flow.FINALIZER_BLOCKED_AFTER`: the armed finalizer's redelivery, its control claim and
 * one detector period (270 s).
 */
export const FINALIZER_BLOCKED_AFTER_SECONDS = PRECOMPUTE_READY_SECONDS
  + BAKE_TASKS.control_claim_seconds + BAKE_TASKS.worker_schedule_seconds;
/**
 * Fifteen promoter passes (one a minute in `GRAPH`): a wakeup that old is not waiting for the
 * next pass, it is stranded. Held items are skipped while the hold is set (r5, R4-6).
 */
export const PROMOTE_AGE_ALARM_SECONDS = 15 * 60;
/** The detector runs every minute; five minutes without a complete sweep alarms. */
export const STUCK_FLOW_PERIOD_SECONDS = 5 * 60;

export interface GraphAlarmSpec {
  readonly id: string;
  readonly suffix: string;
  readonly metricName: string;
  readonly scope: 'live' | 'probe';
  readonly threshold: number;
  readonly periodSeconds: number;
  readonly missing: cloudwatch.TreatMissingData;
  readonly notifyOk: boolean;
  readonly description: string;
}

/** The inventory, in one place so the CDK test can compare every field with it. */
export const GRAPH_ALARMS: readonly GraphAlarmSpec[] = [
  {
    id: 'OpenFlowsPastProtocolCeiling',
    suffix: 'open-flows-past-ceiling',
    metricName: STUCK_FLOW_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: STUCK_FLOW_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.BREACHING,
    notifyOk: true,
    description:
      'A live flow obligation is past its root clock plus the v1 protocol ceiling, or the '
      + 'stuck-flow detector could not complete a sweep. Read the detector log for the per-shard '
      + 'evidence, then explain each counted flow with flow explain.',
  },
  {
    id: 'OpenFlowsWithoutRootClock',
    suffix: 'open-flows-without-root-clock',
    metricName: UNCLOCKED_FLOW_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: STUCK_FLOW_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: false,
    description:
      'A live flow obligation carries no root clock, so its due time cannot be computed. Every '
      + 'S5 writer stamps one; find the writer from the detector log.',
  },
  {
    id: 'PromoteObligationAge',
    suffix: 'promote-obligation-age',
    metricName: PROMOTE_AGE_METRIC,
    scope: 'live',
    threshold: PROMOTE_AGE_ALARM_SECONDS,
    periodSeconds: STUCK_FLOW_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: false,
    description:
      'A PROMOTE# wakeup that is not held has waited fifteen promoter passes. Check that the '
      + 'promoter schedule is enabled and its last pass completed.',
  },
  {
    id: 'GraphBakeTaskFailed',
    suffix: 'graph-bake-task-failed',
    metricName: BAKE_TASK_FAILED_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.MISSING,
    notifyOk: true,
    description:
      'A GRAPH bake task failed fatally and holds a BAKE_RECOVERY# obligation; its winner cannot '
      + 'finalize. Recovery owner: the graph task and its recovery item. Read the detector log for '
      + 'the item, then explain the run.',
  },
  {
    id: 'GraphBakeRetryExhausted',
    suffix: 'graph-bake-retry-exhausted',
    metricName: BAKE_RETRY_EXHAUSTED_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.MISSING,
    notifyOk: true,
    description:
      'A GRAPH bake task exhausted its receives on a dependency failure and holds a '
      + 'BAKE_RECOVERY# obligation. Recovery owner: the graph task and its recovery item. Read the '
      + 'detector log for the item, then explain the run.',
  },
  {
    id: 'GraphBakePrecomputeBlocked',
    suffix: 'graph-bake-precompute-blocked',
    metricName: BAKE_PRECOMPUTE_BLOCKED_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.MISSING,
    notifyOk: true,
    description:
      `A winner's precompute contribution is absent more than ${PRECOMPUTE_BLOCKED_AFTER_SECONDS} s `
      + 'after it was owed; its serving head stays ACTIVE. Read the detector log for the COMPLETE# '
      + 'item. With bake_readiness_at, the owners are InsightPrecompute and InsightContinuation. '
      + 'Without it, the owner is the Release: search its log for \'operation failed\' with '
      + 'error_code ADLY-GRAPH_PRECOMPUTE-0007 or -0006 and error_context.run_id = the run (a hit '
      + 'is a store-shape defect; stop and explain). No hit: read RUN#<run>/NODE#bake; a '
      + 'non-terminal bake is the Baker\'s redelivery. With a terminal bake and no hit, search '
      + '\'unexpected failure\' with operation execution_graph.release (a store-shape or code '
      + 'defect; stop and explain).',
  },
  {
    id: 'GraphBakeFinalizerBlocked',
    suffix: 'graph-bake-finalizer-blocked',
    metricName: BAKE_FINALIZER_BLOCKED_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.MISSING,
    notifyOk: true,
    description:
      'A winner\'s precompute contribution settled more than '
      + `${FINALIZER_BLOCKED_AFTER_SECONDS} s ago and its COMPLETE# remains. With `
      + 'bake_readiness_at, the owner is the finalizer DUE (no FINALIZED receipt); without it, the '
      + 'owner is the Release\'s completion (\'operation failed\' for the run). Read the detector '
      + 'log, then explain the run.',
  },
  {
    id: 'GraphBakeTelemetryLoss',
    suffix: 'graph-bake-telemetry-loss',
    metricName: BAKE_TELEMETRY_LOSS_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: true,
    description:
      'A GRAPH bake telemetry emission failed (task metrics, summary line or admission '
      + 'observation). The bake result is unchanged; its performance readout is incomplete. Read '
      + 'the baker WARNING telemetry_unavailable line for the cause.',
  },
  {
    id: 'GraphBakeTraceIncomplete',
    suffix: 'graph-bake-trace-incomplete',
    metricName: BAKE_TRACE_INCOMPLETE_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: true,
    description:
      'A succeeded Athena execution in a GRAPH bake has no GetQueryExecution statistics, so its '
      + 'athena.query span carries statistics_unavailable. Read the task_summary line for the task, '
      + 'then the span for the execution.',
  },
  {
    id: 'GraphServingWithheld',
    suffix: 'graph-serving-withheld',
    metricName: SERVING_WITHHELD_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.IGNORE,
    notifyOk: true,
    description:
      'A GRAPH cohort finalized with a failure entry (budget exhaustion, or a POST_GRANT '
      + 'invariant or stale precompute that the Release or finalizer settled), so its pages kept '
      + 'their previous (last-good) generation. There is no re-drive for the same input: '
      + 're-admitting it joins the finished run. The pages refresh, and this alarm clears, when '
      + 'the next publication\'s cohort finalizes without a failure. Read the Baker\'s finalize '
      + 'serving_withheld line, or the Release\'s serving_withheld WARNING (phase=complete), for '
      + 'the run and reasons (if failure_reasons is empty, its binding_kind, execution_attempts '
      + 'and stale fields name the cause), then follow the GraphServingWithheld entry in '
      + 'operator-tools.md.',
  },
  {
    id: 'OpenFlowsPastProtocolCeilingProbe',
    suffix: 'open-flows-past-ceiling-probe',
    metricName: STUCK_FLOW_METRIC,
    scope: 'probe',
    threshold: 1,
    periodSeconds: 60,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: true,
    description:
      'Probe scope only: a traceability probe placed a stuck-flow obligation in the dev probe '
      + 'table (P5). Expected during a recorded probe run; the probe clears it.',
  },
];

/**
 * ADR-142 (owner approval A1): the two bake alarms in the publisher stack, which every
 * environment deploys, so the legacy entry's refusals alarm wherever there is no graph
 * authority. Both are event counts (`BakeIsolatedRefusalLimit` per limit or dependency refusal
 * group, `BakeTaskOverBudget` per over-budget bake), absent when nothing happened, so missing
 * data is not breaching. `BakeIsolatedRefusalData` (history gaps, incomplete answers, source
 * refusals) is counted and not alarmed: the user sees a precise note, so it stays a product
 * signal.
 */
export const BAKE_ALARMS: readonly GraphAlarmSpec[] = [
  {
    id: 'BakeIsolatedRefusalLimit',
    suffix: 'bake-isolated-refusal-limit',
    metricName: BAKE_ISOLATED_REFUSAL_LIMIT_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: true,
    description:
      'A page, section or campaign hit a stated bake limit or a dependency that stayed down, and '
      + 'users see an absence there; every other page published. Read the baker isolated_refusal '
      + 'line for the entry, page and reason. For dependency_unavailable: once the dependency is '
      + 'healthy, run a declarative re-bake (operator-tools.md); otherwise the page keeps its '
      + 'last-good body until the next landing, because freshness is landing-driven. For '
      + 'quarter_manifest_absent: run the quarter-manifest backfill with its readback '
      + '(quarter_manifest_backfill), then re-bake.',
  },
  {
    id: 'BakeTaskOverBudget',
    suffix: 'bake-task-over-budget',
    metricName: BAKE_TASK_OVER_BUDGET_METRIC,
    scope: 'live',
    threshold: 1,
    periodSeconds: BAKE_ALARM_PERIOD_SECONDS,
    missing: cloudwatch.TreatMissingData.NOT_BREACHING,
    notifyOk: true,
    description:
      'A page\'s bake ran past its certified seconds. Users see no difference: nothing was cut. '
      + 'Find the baker over_budget line (entry, page, seconds, budget_seconds) and decide: a '
      + 'defect (a slow path nobody expected), or data beyond the envelope the budget was '
      + 'certified at.',
  },
];

/** One alarm from one spec: the shared metric construction every graph and bake alarm uses. */
export function alarmFromSpec(
  scope: Construct,
  config: AppConfig,
  spec: GraphAlarmSpec,
  action: cloudwatch.IAlarmAction,
): cloudwatch.Alarm {
  const alarm = new cloudwatch.Alarm(scope, spec.id, {
    alarmName: `adlytics-${config.envName}-${spec.suffix}`,
    alarmDescription: spec.description,
    metric: new cloudwatch.Metric({
      namespace: GRAPH_METRIC_NAMESPACE,
      metricName: spec.metricName,
      dimensionsMap: { Protocol: PROTOCOL_VERSION, AuthorityScope: spec.scope },
      statistic: cloudwatch.Stats.MAXIMUM,
      period: cdk.Duration.seconds(spec.periodSeconds),
    }),
    threshold: spec.threshold,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    evaluationPeriods: 1,
    datapointsToAlarm: 1,
    treatMissingData: spec.missing,
  });
  alarm.addAlarmAction(action);
  if (spec.notifyOk) alarm.addOkAction(action);
  return alarm;
}

/** One alarm per spec, every action on the selected staffed destination. */
export function buildGraphAlarms(
  scope: Construct,
  config: AppConfig,
  topic: sns.ITopic,
): cloudwatch.Alarm[] {
  const action = new actions.SnsAction(topic);
  return GRAPH_ALARMS.map((spec) => alarmFromSpec(scope, config, spec, action));
}

/** ADR-142: the publisher stack's two bake alarms, on its staffed action. */
export function buildBakeAlarms(
  scope: Construct,
  config: AppConfig,
  action: cloudwatch.IAlarmAction,
): cloudwatch.Alarm[] {
  return BAKE_ALARMS.map((spec) => alarmFromSpec(scope, config, spec, action));
}

/**
 * The detector's whole graph grant: a strong `Query` of the `DUE#` partitions and nothing else
 * (the mode-item read is `grantModeItemRead`). No write action on any item.
 */
export function grantDueShardRead(principal: iam.IGrantable, authority: ExecutionGraphAuthority): void {
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'QueryDueShards',
    actions: ['dynamodb:Query'],
    resources: [authority.tableArn],
    conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['DUE#*'] } },
  }));
}

/**
 * Plan 6.1 (F9): the one read the bake gauges add, a strong `GetItem` of a grant's precompute
 * contribution (`COHORT#<grant_id>/CONTRIBUTION#precompute`). No `Query`, no write.
 */
export function grantBakeContributionRead(principal: iam.IGrantable, authority: ExecutionGraphAuthority): void {
  principal.grantPrincipal.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ReadBakeContributions',
    actions: ['dynamodb:GetItem'],
    resources: [authority.tableArn],
    conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['COHORT#*'] } },
  }));
}

/** S6 part 2 (§2.11 row 5b): the POST_GRANT invariant counter `graph_metrics.py` emits. */
export const POST_GRANT_INVARIANT_METRIC = 'GraphPostGrantInvariant';
/**
 * Every closed `Signal` value the invariant counter carries, one per fail-closed emitter: the
 * dispatcher's oversize envelope (D23), TableWriter's `publish` invariant (§2.6), the baker's
 * per-intent invariant (§2.8) and the shared consumer's invariant settlement (§2.8).
 */
export const POST_GRANT_INVARIANT_SIGNALS = [
  'post_grant_oversize',
  'publish_invariant',
  'bake_intent_invariant',
  'consumer_invariant',
] as const;

/**
 * §2.11 row 5b: one alarm, `adlytics-<env>-graph-post-grant-invariant`, over the counter in one
 * namespace with no function dimension, so every emitter feeds it. CloudWatch matches a metric
 * by its exact dimensions, so the alarm sums one metric per closed signal; a missing signal
 * fills 0. On the staffed topic (AGG3-9, AGG3-14).
 */
export function buildPostGrantInvariantAlarm(
  scope: Construct,
  config: AppConfig,
  topic: sns.ITopic,
): cloudwatch.Alarm {
  const period = cdk.Duration.minutes(1);
  const usingMetrics = Object.fromEntries(
    POST_GRANT_INVARIANT_SIGNALS.map((signal, index) => [
      `s${index}`,
      new cloudwatch.Metric({
        namespace: GRAPH_METRIC_NAMESPACE,
        metricName: POST_GRANT_INVARIANT_METRIC,
        dimensionsMap: { Protocol: PROTOCOL_VERSION, Signal: signal },
        statistic: cloudwatch.Stats.SUM,
        period,
      }),
    ]),
  );
  const alarm = new cloudwatch.Alarm(scope, 'GraphPostGrantInvariant', {
    alarmName: `adlytics-${config.envName}-graph-post-grant-invariant`,
    alarmDescription:
      'A POST_GRANT path failed closed: an oversize envelope, a publish invariant, a bake intent '
      + 'invariant or a consumer invariant settlement. The run serves its previous generation; '
      + 'read the emitter\'s terminal log for the node, then explain the run.',
    metric: new cloudwatch.MathExpression({
      expression: Object.keys(usingMetrics).map((id) => `FILL(${id}, 0)`).join(' + '),
      usingMetrics,
      period,
      label: 'GraphPostGrantInvariant (all signals)',
    }),
    threshold: 1,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    evaluationPeriods: 1,
    treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
  });
  alarm.addAlarmAction(new actions.SnsAction(topic));
  return alarm;
}
