// SPDX-License-Identifier: MIT-0
/**
 * The static Athena start budget (plan S1, 3.3, F2, F4): the pure request validator over the
 * shared case table, the synth wrapper's refusals at each batch stack, and the synthesized
 * reserved concurrency of every budgeted function. Every accepted case runs beside its refused
 * control, in this test, through this language's validator; the Python twin runs the same file.
 */
import { readFileSync } from 'node:fs';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import type { AppConfig } from '../lib/config';
import {
  ATHENA_START_BUDGET_REFUSALS,
  ATHENA_START_BUDGET_ROW_IDS,
  AthenaStartBudgetRequest,
  athenaStartBudgetRows,
  validateAthenaStartBudget,
  validateAthenaStartBudgetRequest,
} from '../lib/config/athena-start-budget';
import { ATHENA_START_BUDGET } from '../lib/generated/execution-graph-protocol';
import { IngestionStack } from '../lib/stacks/ingestion-stack';
import { InsightPrecomputeStack } from '../lib/stacks/insight-precompute-stack';
import { PublisherStack } from '../lib/stacks/publisher-stack';
import { TEST_CONFIG, configWith } from './support/config';
import { everyTemplate, resourcesOfType } from './support/templates';

type Props = Record<string, unknown>;

interface CaseRow {
  readonly row_id: string;
  readonly reserved_concurrency: number | null | boolean;
  readonly start_cap: number;
}

interface BudgetCase {
  readonly id: string;
  readonly rows: readonly CaseRow[];
  readonly batch_athena_budget: number;
  readonly chat_reserve_active_dml: number;
  readonly active_dml_quota: number;
  readonly certified_query_cap: number;
  readonly expect: { readonly verdict: 'accepted' | 'refused'; readonly reason?: string; readonly total?: number };
  readonly control?: string;
}

const CASES_PATH = path.join(
  __dirname, '..', '..', 'tests', 'fixtures', 'athena-start-budget', 'cases.json',
);
const CASES = JSON.parse(readFileSync(CASES_PATH, 'utf8')) as readonly BudgetCase[];
const BY_ID = new Map(CASES.map((item) => [item.id, item]));
const PUBLISHER_INPUT = path.join(__dirname, '..', '..', 'presentation', 'pages', 'v2', 'pages.yaml');
const TOP_LEVEL = [
  'batch_athena_budget',
  'chat_reserve_active_dml',
  'active_dml_quota',
  'certified_query_cap',
] as const;

function request(item: BudgetCase): AthenaStartBudgetRequest {
  return {
    // A boolean stays a boolean, exactly as the Python suite reads it, so it is refused.
    rows: item.rows.map((row) => ({
      rowId: row.row_id,
      reservedConcurrency: row.reserved_concurrency as number | null,
      startCap: row.start_cap,
    })),
    batchAthenaBudget: item.batch_athena_budget,
    chatReserveActiveDml: item.chat_reserve_active_dml,
    activeDmlQuota: item.active_dml_quota,
    certifiedQueryCap: item.certified_query_cap,
  };
}

/** Every scalar two cases disagree on: a row field or a top-level limit. */
function scalarDifferences(left: BudgetCase, right: BudgetCase): string[] {
  if (left.rows.length !== right.rows.length) return ['rows.length'];
  const fields = ['row_id', 'reserved_concurrency', 'start_cap'] as const;
  const rows = left.rows.flatMap((row, index) =>
    fields
      .filter((field) => row[field] !== right.rows[index]?.[field])
      .map((field) => `rows[${index}].${field}`),
  );
  return [...rows, ...TOP_LEVEL.filter((name) => left[name] !== right[name])];
}

function expectVerdict(item: BudgetCase): void {
  const verdict = validateAthenaStartBudgetRequest(request(item));
  if (item.expect.verdict === 'accepted') {
    expect(verdict).toEqual({ verdict: 'accepted', total: item.expect.total });
  } else {
    expect(verdict).toEqual(expect.objectContaining({ verdict: 'refused', reason: item.expect.reason }));
  }
}

describe('validateAthenaStartBudgetRequest over the shared case table', () => {
  it.each(CASES.map((item) => [item.id, item] as const))('%s gives its expected verdict', (_, item) => {
    expectVerdict(item);
  });

  const accepted = CASES.filter((item) => item.expect.verdict === 'accepted');
  it.each(accepted.map((item) => [item.id, item] as const))(
    '%s runs beside exactly one refused control differing in one scalar',
    (_, item) => {
      expect(item.control).toBeDefined();
      const control = BY_ID.get(item.control ?? '');
      if (control === undefined) throw new Error(`${item.id} names a missing control`);
      expect(control.expect.verdict).toBe('refused');
      expect(control.control).toBeUndefined();
      expect(scalarDifferences(item, control)).toHaveLength(1);
      // Both halves of the pair run here, through this suite's own validator.
      expectVerdict(item);
      expectVerdict(control);
    },
  );

  it('carries the required cases and at least one refusal per closed reason', () => {
    const required = [
      'defaults-active', 'baker-drift-3', 'defaults-quiesced', 'load-unreserved', 'load-negative',
      'raised-q-16', 'raised-q-16-budget-52', 'operator-row-two-processes', 'budget-over-headroom',
      'chat-reserve-zero', 'start-cap-integral-float', 'start-cap-fraction',
    ];
    expect(required.filter((id) => !BY_ID.has(id))).toEqual([]);
    const reasons = new Set(CASES.map((item) => item.expect.reason).filter(Boolean));
    expect(ATHENA_START_BUDGET_REFUSALS.filter((reason) => !reasons.has(reason))).toEqual([]);
    expect(accepted.length).toBeGreaterThanOrEqual(4);
  });

  it('control: the pairing check fails a control that differs in two scalars', () => {
    const base = BY_ID.get('defaults-active') as BudgetCase;
    const twoApart: BudgetCase = { ...base, batch_athena_budget: 40, chat_reserve_active_dml: 19 };
    expect(scalarDifferences(base, twoApart)).toHaveLength(2);
    expect(scalarDifferences(base, base)).toHaveLength(0);
  });

  it('reports the defaults refusal values, an operator row beside a reprocess run, and the headroom term', () => {
    const verdict = (id: string) => validateAthenaStartBudgetRequest(request(BY_ID.get(id) as BudgetCase));
    expect(verdict('baker-drift-3')).toEqual({ verdict: 'refused', reason: 'SUM_OVER_BUDGET', observed: 51, limit: 41 });
    expect(verdict('operator-row-two-processes')).toEqual({ verdict: 'refused', reason: 'SUM_OVER_BUDGET', observed: 42, limit: 41 });
    expect(verdict('budget-over-headroom')).toEqual({ verdict: 'refused', reason: 'BUDGET_OVER_CHAT_HEADROOM', observed: 181, limit: 180 });
    expect(verdict('load-unreserved')).toEqual({ verdict: 'refused', reason: 'UNRESERVED', observed: 'inline_ingestion', limit: null });
  });
});

function budgetConfig(overrides: Partial<AppConfig['athenaStartBudget']>, base: AppConfig = TEST_CONFIG): AppConfig {
  return { ...base, athenaStartBudget: { ...base.athenaStartBudget, ...overrides } };
}

function synthInsight(config: AppConfig): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new InsightPrecomputeStack(app, 'InsightBudget', {
      config,
      env: { account: config.account, region: config.region },
      sourceAssetPath: __dirname,
    }),
  );
}

function synthPublisher(config: AppConfig): Template {
  const app = new cdk.App();
  return Template.fromStack(
    new PublisherStack(app, 'PublisherBudget', {
      config,
      env: { account: config.account, region: config.region },
      sourceAssetPath: __dirname,
      publisherInputPath: PUBLISHER_INPUT,
    }),
  );
}

describe('validateAthenaStartBudget at synth', () => {
  it('accepts the default config at 41 <= 41 <= 180, with the limits read from the contract', () => {
    expect(validateAthenaStartBudget(TEST_CONFIG)).toBe(41);
    expect(TEST_CONFIG.athenaStartBudget).toEqual({
      bakerStartCap: 10,
      insightStartCap: 4,
      batchAthenaBudget: ATHENA_START_BUDGET.batch_athena_budget,
      chatReserveActiveDml: ATHENA_START_BUDGET.chat_reserve_active_dml,
    });
    expect(ATHENA_START_BUDGET.active_dml_quota - ATHENA_START_BUDGET.chat_reserve_active_dml).toBe(180);
    expect(athenaStartBudgetRows(TEST_CONFIG).map((row) => row.rowId)).toEqual([...ATHENA_START_BUDGET_ROW_IDS]);
    // The paired positive control for every throw below: both batch stacks synthesize.
    expect(() => synthInsight(TEST_CONFIG)).not.toThrow();
    expect(() => synthPublisher(TEST_CONFIG)).not.toThrow();
  });

  it.each([
    ['bakerStartCap 11', { bakerStartCap: 11 }, /SUM_OVER_BUDGET \(observed 43, limit 41\)/],
    ['bakerStartCap 9', { bakerStartCap: 9 }, /BELOW_CERTIFIED_QUERY_CAP \(observed 9, limit 10\)/],
    ['insightStartCap 0', { insightStartCap: 0 }, /NON_POSITIVE_CAP/],
    ['a fractional cap 10.5', { bakerStartCap: 10.5 }, /NON_INTEGER/],
    ['batchAthenaBudget 42', { batchAthenaBudget: 42 }, /athenaStartBudget\.batchAthenaBudget is 42.*batch_athena_budget is 41/],
    ['chatReserveActiveDml 19', { chatReserveActiveDml: 19 }, /athenaStartBudget\.chatReserveActiveDml is 19.*chat_reserve_active_dml is 20/],
  ] as const)('control: %s throws at the insight precompute stack naming the term', (_, overrides, message) => {
    expect(() => synthInsight(budgetConfig(overrides))).toThrow(message);
  });

  it('control: the publisher stack refuses the same over-budget Baker cap', () => {
    expect(() => synthPublisher(budgetConfig({ bakerStartCap: 11 }))).toThrow(/SUM_OVER_BUDGET/);
  });

  it('accepts a quiesced handoff whose load function renders reserved concurrency 0', () => {
    const quiesced = configWith({
      landing: { enabled: false },
      ingestion: { ...TEST_CONFIG.ingestion, projectionHandoffState: 'INLINE_QUIESCED' },
    });
    const app = new cdk.App();
    const ingestion = Template.fromStack(
      new IngestionStack(app, 'IngestionQuiesced', {
        config: quiesced,
        env: { account: quiesced.account, region: quiesced.region },
        sourceAssetPath: __dirname,
      }),
    ).toJSON() as Props;
    const load = resourcesOfType(ingestion, 'AWS::Lambda::Function').find(
      (fn) => fn.Handler === 'adapters.inbound.ingestion_handlers.load.handler',
    );
    expect(load?.ReservedConcurrentExecutions).toBe(0);
    // The wrapper evaluates the configured maximum (2), so the quiesced deployment synthesizes.
    expect(validateAthenaStartBudget(quiesced)).toBe(41);
    expect(() => synthInsight(quiesced)).not.toThrow();
  });
});

const BUDGETED_HANDLERS = {
  'adapters.jobs.preset_artifact_publisher.baker.handler': 2,
  'adapters.inbound.ingestion_handlers.table_writer.handler': 8,
  'adapters.inbound.ingestion_handlers.load.handler': 2,
  'adapters.jobs.provider_attempt_reconciler.handler': 1,
  'adapters.jobs.insight_precompute.handler.handler': 2,
} as const;

/** Throw unless each budgeted handler is synthesized exactly once at its row's reservation. */
function expectReservedConcurrency(templates: Record<string, unknown>, expected: Record<string, number>): void {
  const functions = Object.values(templates).flatMap((template) =>
    resourcesOfType(template as Record<string, unknown>, 'AWS::Lambda::Function'),
  );
  for (const [handler, reserved] of Object.entries(expected)) {
    const found = functions.filter((fn) => fn.Handler === handler);
    if (found.length !== 1 || found[0]?.ReservedConcurrentExecutions !== reserved) {
      throw new Error(
        `${handler}: expected one function reserved ${reserved}, found ` +
          JSON.stringify(found.map((fn) => fn.ReservedConcurrentExecutions)),
      );
    }
  }
}

describe('synthesized reserved concurrency equals each budget row', () => {
  const templates = everyTemplate();

  it('Baker 2, TableWriter 8, load 2, reconciler 1, insight precompute 2, matching the rows', () => {
    expectReservedConcurrency(templates, BUDGETED_HANDLERS);
    const rows = new Map(athenaStartBudgetRows(TEST_CONFIG).map((row) => [row.rowId, row.reservedConcurrency]));
    expect([
      rows.get('baker'), rows.get('table_writer'), rows.get('inline_ingestion'),
      rows.get('provider_attempt_reconciler'), rows.get('insight_precompute'),
    ]).toEqual(Object.values(BUDGETED_HANDLERS));
  });

  it('bake ESM maximum concurrency 2 and Enqueue reserved 8', () => {
    const publisher = templates.PublisherStack as Record<string, unknown>;
    const bake = resourcesOfType(publisher, 'AWS::Lambda::EventSourceMapping').filter((item) =>
      JSON.stringify(item.EventSourceArn).includes('BakeQueue'),
    );
    expect(bake.map((item) => item.ScalingConfig)).toEqual([{ MaximumConcurrency: 2 }]);
    expectReservedConcurrency(templates, { 'adapters.jobs.preset_artifact_publisher.enqueue.handler': 8 });
  });

  it('control: the comparison fails on a template whose Baker reservation drifted to 3', () => {
    const mutated = JSON.parse(JSON.stringify(templates)) as Record<string, Record<string, Props>>;
    const resources = (mutated.PublisherStack?.Resources ?? {}) as Record<string, Props>;
    const baker = Object.values(resources).find(
      (resource) =>
        resource.Type === 'AWS::Lambda::Function' &&
        (resource.Properties as Props).Handler === 'adapters.jobs.preset_artifact_publisher.baker.handler',
    );
    (baker?.Properties as Props).ReservedConcurrentExecutions = 3;
    expect(() => expectReservedConcurrency(mutated, BUDGETED_HANDLERS)).toThrow(/baker\.handler: expected one function reserved 2/);
  });
});
