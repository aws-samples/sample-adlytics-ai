// SPDX-License-Identifier: MIT-0
/**
 * The demand forecast read API's browser decoders, fetch result and copy (ADR-163, ADR-084). A
 * decoder that accepts an unknown code would render it as text; one that accepts a money figure
 * without two places would show it at the wrong scale; a fetcher that reports an aborted request
 * as an outage would show Try again for a request the page cancelled itself.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, test } from 'vitest';
import {
  accuracyClassCopy,
  failureFamilyCopy,
  fetchDemandForecastDetail,
  fetchDemandForecastList,
  fetchDemandForecastStatus,
  figureNumber,
  figureText,
  freshnessCauseCopy,
  holdCopy,
  modelCopy,
  parseDemandForecastDetail,
  parseDemandForecastList,
  parseDemandForecastRefusal,
  parseDemandForecastStatus,
  maseGapCopy,
  maseGaps,
  reasonNextStepCopy,
  reasonSentenceCopy,
  runOutcomeCopy,
  skipReasonCopy,
  weekStateCopy,
  weekStates,
} from '../lib/demand-forecast';
import detail from './fixtures/demand-forecast-detail.json';
import notForecast from './fixtures/demand-forecast-detail-not-forecast.json';
import listed from './fixtures/demand-forecast-list.json';
import emptyFilter from './fixtures/demand-forecast-list-empty-filter.json';
import refusal from './fixtures/demand-forecast-refusal.json';
import noGeneration from './fixtures/demand-forecast-status-no-generation.json';
import stale from './fixtures/demand-forecast-status-stale.json';

const generated = JSON.parse(readFileSync(join(process.cwd(), '../contracts/generated/demand-forecast-api.json'), 'utf8')) as {
  enums: Record<string, string[]>;
};
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('every wire fixture decodes', () => {
  assert.equal(parseDemandForecastList(listed).entries.length, 3);
  assert.equal(parseDemandForecastList(emptyFilter).match_count, 0);
  assert.equal(parseDemandForecastDetail(detail).outcome, 'selected');
  assert.equal(parseDemandForecastDetail(notForecast).outcome, 'not_forecast');
  assert.deepEqual(parseDemandForecastStatus(stale).freshness.causes, ['age_over_limit', 'last_run_held']);
  assert.equal(parseDemandForecastStatus(noGeneration).generation, null);
  assert.equal(parseDemandForecastRefusal(refusal).refused, 'PAGE_INVALID');
});

test('an unknown code fails decoding instead of rendering', () => {
  const unknown = structuredClone(stale) as { last_run: { outcome: string } };
  unknown.last_run.outcome = 'no_op_same_input';
  assert.throws(() => parseDemandForecastStatus(unknown), /last_run.outcome/);
  const extra = { ...structuredClone(listed), extra: 1 };
  assert.throws(() => parseDemandForecastList(extra), /list/);
});

test('a money figure must carry two places and a unit figure none', () => {
  const money = structuredClone(listed) as { entries: Array<{ targets: Record<string, { p50_totals: Record<string, { display: string }> }> }> };
  const first = money.entries[0];
  assert.ok(first);
  const revenue = first.targets.revenue_shipped?.p50_totals['4'];
  assert.ok(revenue);
  revenue.display = '4938';
  assert.throws(() => parseDemandForecastList(money), /display/);
  const units = structuredClone(listed) as typeof money;
  const ordered = units.entries[0]?.targets.units_ordered?.p50_totals['4'];
  assert.ok(ordered);
  ordered.display = '80.00';
  assert.throws(() => parseDemandForecastList(units), /display/);
});

test('figureText renders the server display: units grouped with their word, money with its code', () => {
  assert.equal(figureText({ value: '1234.5000', display: '1235' }, 'units_ordered', null), '1,235 units');
  assert.equal(figureText({ value: '-2.5000', display: '-3' }, 'units_shipped', null), '-3 units');
  assert.equal(figureNumber({ value: '1234.5000', display: '1235' }, 'units_ordered', null), '1,235');
  assert.equal(figureNumber({ value: '16048.5000', display: '16048.50' }, 'revenue_shipped', 'USD'), '16,048.50 USD');
  assert.equal(figureText({ value: '12.3000', display: '12.30' }, 'revenue_shipped', 'USD'), '12.30 USD');
  assert.equal(figureText({ value: '-1234.5000', display: '-1234.50' }, 'cogs_shipped', 'USD'), '-1,234.50 USD');
  assert.throws(() => figureText({ value: '12.3000', display: '12.30' }, 'revenue_shipped', null), /currency/);
});

test('every copy map has exactly the generated enum keys', () => {
  const maps: Array<[string, Record<string, unknown>]> = [
    ['SkipReason', skipReasonCopy], ['ModelName', modelCopy], ['RunOutcome', runOutcomeCopy],
    ['FailureFamily', failureFamilyCopy], ['FreshnessCause', freshnessCauseCopy],
    ['AccuracyClass', accuracyClassCopy], ['Hold', holdCopy], ['SkipReason', reasonSentenceCopy],
    ['SkipReason', reasonNextStepCopy],
  ];
  for (const [name, map] of maps) {
    assert.deepEqual(Object.keys(map).sort(), [...(generated.enums[name] ?? [])].sort(), name);
  }
  // Week states and MASE gaps are decoder enums with no generated list: pin them to the decoder's.
  assert.deepEqual(Object.keys(weekStateCopy).sort(), [...weekStates].sort());
  assert.deepEqual(Object.keys(maseGapCopy).sort(), [...maseGaps].sort());
});

function respond(status: number, body: unknown, seen: string[] = []): void {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    seen.push(String(input));
    return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;
}

test('the fetchers return ok, refused or invalid through one typed result', async () => {
  const seen: string[] = [];
  respond(200, listed, seen);
  const ok = await fetchDemandForecastList({ status: 'shown', page: 2 });
  assert.equal(ok.kind, 'ok');
  assert.equal(seen[0], '/api/demand-forecast?status=shown&page=2');
  respond(400, refusal);
  const refused = await fetchDemandForecastDetail('B0FIXTUR01');
  assert.deepEqual(refused.kind === 'refused' ? [refused.status, refused.refusal.refused] : null, [400, 'PAGE_INVALID']);
  respond(503, { schema_version: 1, refused: 'FORECAST_STORE_UNAVAILABLE', code: 'ADLY-FORECAST_READ-0009', detail: 'x' });
  const unavailable = await fetchDemandForecastStatus();
  assert.equal(unavailable.kind === 'refused' ? unavailable.refusal.refused : null, 'FORECAST_STORE_UNAVAILABLE');
  respond(200, { schema_version: 1 });
  assert.equal((await fetchDemandForecastStatus()).kind, 'invalid');
  respond(502, '<html>bad gateway</html>');
  assert.equal((await fetchDemandForecastStatus()).kind, 'invalid');
});

test('an aborted request rejects with its AbortError, never an invalid result', async () => {
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    await new Promise((_resolve, reject) => {
      // As the real fetch: an already-aborted signal rejects at once.
      if (init?.signal?.aborted) reject(new DOMException('aborted', 'AbortError'));
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
    return new Response('{}');
  }) as typeof fetch;
  const controller = new AbortController();
  const pending = fetchDemandForecastStatus(controller.signal);
  controller.abort();
  await assert.rejects(pending, (error: unknown) => error instanceof DOMException && error.name === 'AbortError');
});
