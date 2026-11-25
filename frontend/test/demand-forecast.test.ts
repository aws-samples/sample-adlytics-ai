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
  FORECAST_START,
  failureFamilyCopy,
  forecastHistoryState,
  forecastSearch,
  isAsin,
  looksLikeName,
  nameErrorText,
  notInForecastInlineText,
  readForecastEntry,
  readForecastLink,
  shortlistEntries,
  startCopy,
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
    ['Hold', holdCopy], ['SkipReason', reasonSentenceCopy],
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

type WireList = typeof listed;
/** A page 1 of `count` forecast products, ranked 1 to `count`, built from the fixture's first row. */
function rankedList(count: number): WireList {
  const list = structuredClone(listed);
  const [first] = list.entries;
  assert.ok(first);
  list.entries = Array.from({ length: count }, (_unused, index) => ({
    ...structuredClone(first), rank: index + 1, asin: `B0RANK${String(index + 1).padStart(4, '0')}`, title: `Fictional product ${index + 1}`,
  }));
  list.item_count = count;
  list.match_count = count;
  return list;
}

test('Check these first is ranks 1 to 10 with a forecast, in served order, and only from page 1 unfiltered', () => {
  const twelve = parseDemandForecastList(rankedList(12));
  assert.deepEqual(shortlistEntries(twelve).map((entry) => entry.rank), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  // The fixture: rank 3 has no ordered-units forecast, so it is dropped and nothing replaces it.
  assert.deepEqual(shortlistEntries(parseDemandForecastList(listed)).map((entry) => entry.asin), ['B0FIXTUR02', 'B0FIXTUR01']);
  assert.deepEqual(shortlistEntries(parseDemandForecastList({ ...structuredClone(listed), query: { status: 'shown', page: 1 } })), []);
  assert.deepEqual(shortlistEntries(parseDemandForecastList({ ...rankedList(12), page: 2, query: { status: null, page: 2 } })), []);
  const withheld = rankedList(12);
  withheld.entries.slice(0, 10).forEach((entry) => {
    (entry.targets.units_ordered as { chosen_model: string | null; withheld_reason: string | null }).chosen_model = null;
    (entry.targets.units_ordered as { chosen_model: string | null; withheld_reason: string | null }).withheld_reason = 'INSUFFICIENT_CONTEXT';
  });
  assert.deepEqual(shortlistEntries(parseDemandForecastList(withheld)), []);
});

test('the name rule runs only on input that fails the ASIN check; a 10-letter word is still sent', () => {
  for (const name of ['Blender', 'ABC', 'Northstar Home Product 8', 'B0FIXTUR36X']) {
    assert.equal(isAsin(name), false, name);
    assert.equal(looksLikeName(name), true, name);
  }
  assert.equal(isAsin('B0FIX'), false);
  assert.equal(looksLikeName('B0FIX'), false);
  assert.equal(isAsin('TOOTHBRUSH'), true);
  assert.equal(isAsin('b0fixtur36'), true);
});

test('a link opens one view only when it names exactly one valid route value (B-04)', () => {
  assert.deepEqual(readForecastLink(''), FORECAST_START);
  assert.deepEqual(readForecastLink('?utm=x'), FORECAST_START);
  assert.deepEqual(readForecastLink('?asin=b0fixtur36&utm=x'), { view: 'product', asin: 'B0FIXTUR36', from: 'start', depth: 1 });
  assert.deepEqual(readForecastLink('?view=all'), { view: 'all', asin: null, from: 'start', depth: 1 });
  for (const bad of ['?asin=B0FIX', '?asin=', '?view=list', '?asin=B0FIXTUR36&asin=B0FIXTUR08', '?view=all&view=all', '?asin=B0FIXTUR36&view=all']) {
    assert.equal(readForecastLink(bad), 'invalid', bad);
  }
});

test('the history entry is read strictly and written beside every other key (B-02)', () => {
  const product = { view: 'product', asin: 'B0FIXTUR36', from: 'all', depth: 2 } as const;
  const written = forecastHistoryState({ __NA: true, sentinel: 'kept' }, product);
  assert.deepEqual(written, { __NA: true, sentinel: 'kept', adlyticsForecast: product });
  assert.deepEqual(readForecastEntry(written), product);
  assert.deepEqual(forecastHistoryState(null, FORECAST_START), { adlyticsForecast: FORECAST_START });
  assert.equal(readForecastEntry(null), null);
  assert.equal(readForecastEntry({ adlyticsForecast: { ...product, depth: 1 } }), null);
  assert.equal(readForecastEntry({ adlyticsForecast: { ...product, asin: 'B0FIX' } }), null);
  assert.equal(readForecastEntry({ adlyticsForecast: { view: 'all', asin: null, from: 'start', depth: 1 } })?.view, 'all');
  assert.equal(forecastSearch(product), '?asin=B0FIXTUR36');
  assert.equal(forecastSearch({ view: 'all', asin: null, from: 'start', depth: 1 }), '?view=all');
  assert.equal(forecastSearch(FORECAST_START), '');
});

test('the opening view copy reads the served measure, count and example', () => {
  assert.equal(startCopy.basis(13), 'Largest forecast demand, ordered by 13-week likely units ordered');
  assert.equal(startCopy.empty('units_ordered'), "No product has an ordered-units forecast in this update, so there's no demand to rank.");
  assert.equal(startCopy.reasonShow(3, 'ASIN_INACTIVE'), 'Show the 3 products with no forecast: no recent sales');
  assert.equal(startCopy.browse(61), 'Browse all 61 products');
  assert.equal(startCopy.browse(null), 'Browse all products');
  assert.equal(nameErrorText('B0FIXTUR36'), "Search finds a product by its ASIN, 10 letters or digits, for example B0FIXTUR36. Searching by product name isn't available here; pick a product from Check these first or Browse all.");
  assert.equal(nameErrorText(null), "Search finds a product by its ASIN, 10 letters or digits. Searching by product name isn't available here; pick a product from Check these first or Browse all.");
  assert.equal(notInForecastInlineText('B0NOTREAL0', 61, false), "B0NOTREAL0 isn't in this forecast. This forecast covers 61 products with Vendor Central sales history. Check the ASIN, or pick a product below.");
  assert.equal(notInForecastInlineText('TOOTHBRUSH', 61, true), "TOOTHBRUSH isn't in this forecast. This forecast covers 61 products with Vendor Central sales history. Searching by product name isn't available here; pick a product from Check these first or Browse all.");
  assert.equal(notInForecastInlineText('B0NOTREAL0', null, false), "B0NOTREAL0 isn't in this forecast. Check the ASIN, or pick a product below.");
});
