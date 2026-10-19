// SPDX-License-Identifier: MIT-0
import { join } from 'node:path';

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

import audiencesFixture from '../test/fixtures/audiences-complete-v2.json';
import audiencesInRosterUnavailableFixture from '../test/fixtures/audiences-in-roster-unavailable-v2.json';
import audiencesNotObservedFixture from '../test/fixtures/audiences-not-observed-v2.json';
import newCustomersRolling from '../test/fixtures/new-customers-7d-v2.json';
import newCustomersPending from '../test/fixtures/new-customers-pending-v2.json';
import {
  cloneReachFixture,
  reachCompleteFineFixture,
  reachNoArtifactPendingEnvelope,
  reachNotReturnedFixture,
} from '../test/fixtures/reach-v1-fixtures';
import { cloneRetention, retentionFixture, retentionTwoMarketFixture, RETENTION_REVISION } from '../test/fixtures/retention-v1-fixtures';
import scorecardWeekly from '../test/fixtures/scorecard-7d-v2.json';
import scorecardPending from '../test/fixtures/scorecard-pending-v2.json';

const canonicalRoutes = ['/campaigns/'] as const;

const window = { end: '2026-08-16', start: '2026-08-10' };
const reachContextExpiry = '2099-10-16T12:00:00+00:00';

function reachEnvelope(sessionId: string, fixture: Record<string, unknown> = reachCompleteFineFixture) {
  const artifact = cloneReachFixture(fixture);
  const market = (artifact.markets as Record<string, unknown>[])[0];
  const frequency = market?.frequency as Record<string, unknown> | undefined;
  const firstBucket = (frequency?.buckets as Record<string, unknown>[] | undefined)?.[0];
  const firstRate = firstBucket?.purchaser_rate as Record<string, unknown> | undefined;
  if (firstRate?.display_text === '4.00%') firstRate.value = '0.0400';
  artifact.page_context_expires_at = reachContextExpiry;
  return {
    envelope_version: 2,
    kind: 'page',
    page: {
      actions: [],
      artifact,
      artifact_id: 'reach',
      artifact_revision: 'r'.repeat(43),
      artifact_type: 'ReachAndFunnelArtifact',
      artifact_version: 1,
      kind: 'reach',
      page_context_expires_at: reachContextExpiry,
      page_context_label: artifact.page_context_label,
      page_id: 'reach',
      route: '/reach/',
      starter_questions: artifact.starter_questions,
    },
    session_id: sessionId,
    temporal: {
      comparison: { policy: 'prior_calendar_period', prior_period_label: 'August 2026', status: 'available' },
      custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
      mode: 'selectable',
      presets: [
        { availability: 'available', id: 'last_complete_week', label: 'Latest measured week', period_label: 'Week of 14–20 Sep 2026', reason_code: null, reason_text: null, window: { end: '2026-09-20', start: '2026-09-14' } },
        { availability: 'available', id: 'last_complete_month', label: 'Latest measured month', period_label: 'September 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-09-01' } },
        { availability: 'available', id: 'last_complete_quarter', label: 'Latest measured quarter', period_label: 'Q3 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-07-01' } },
      ],
      resolved: {
        as_of_month: null,
        display_grain: 'month',
        period_label: 'September 2026',
        reporting_window_status: 'preferred_default',
        selection: { kind: 'default', preset_id: null },
        window: { end: '2026-09-30', start: '2026-09-01' },
      },
      version: 2,
    },
  };
}

function audiencesEnvelope(sessionId: string, fixture: Record<string, unknown>) {
  return {
    envelope_version: 2,
    kind: 'page',
    page: {
      actions: [], artifact: fixture, artifact_id: 'audiences', artifact_revision: 'r'.repeat(43),
      artifact_type: 'AudienceProfileArtifact', artifact_version: 1, kind: 'audiences',
      page_context_expires_at: fixture.page_context_expires_at,
      page_context_label: fixture.page_context_label,
      page_id: 'audiences', route: '/audiences/', starter_questions: fixture.starter_questions,
    },
    session_id: sessionId,
    temporal: {
      comparison: { policy: 'prior_calendar_period', prior_period_label: 'August 2026', status: 'unavailable' },
      custom: { availability: 'unavailable', bounds: null, reason_code: 'custom_disabled' },
      mode: 'selectable',
      presets: [{ availability: 'available', id: 'last_complete_month', label: 'Latest measured month', period_label: 'September 2026', reason_code: null, reason_text: null, window: { end: '2026-09-30', start: '2026-09-01' } }],
      resolved: { as_of_month: null, display_grain: 'month', period_label: 'September 2026', reporting_window_status: 'preferred_default', selection: { kind: 'default', preset_id: null }, window: { end: '2026-09-30', start: '2026-09-01' } },
      version: 2,
    },
  };
}

function retentionEnvelope(sessionId: string, fixture: Record<string, unknown>) {
  return {
    envelope_version: 2,
    kind: 'page',
    page: {
      actions: [], artifact: fixture, artifact_id: 'retention', artifact_revision: RETENTION_REVISION,
      artifact_type: 'RetentionCohortArtifact', artifact_version: 1, kind: 'retention',
      page_context_expires_at: fixture.page_context_expires_at,
      page_context_label: fixture.page_context_label,
      page_id: 'retention', route: '/retention/', starter_questions: fixture.starter_questions,
    },
    session_id: sessionId,
    temporal: {
      mode: 'fixed',
      resolved: { as_of_month: '2026-09', label_text: 'Cohorts through September 2026', period_label: 'September 2026', window: { end: '2026-09-30', start: '2025-09-01' } },
      version: 2,
    },
  };
}

function v2WindowUnavailable(sessionId: string) {
  return {
    envelope_version: 2,
    kind: 'window_unavailable',
    reason_code: 'fictional_window_unavailable',
    recovery: { action_id: 'choose_covered_period', no_self_service_reason: null },
    session_id: sessionId,
    temporal: {
      comparison: { policy: 'optional_equal_prior', reason_code: null, status: 'not_requested' },
      custom: {
        availability: 'available',
        bounds: { earliest_start: '2026-08-01', latest_end: '2026-08-21', max_duration_days: 90, min_duration_days: 1 },
        reason_code: null,
      },
      mode: 'selectable',
      presets: [
        { availability: 'available', id: '1d', label: '1D', reason_code: null },
        { availability: 'available', id: '7d', label: '7D', reason_code: null },
        { availability: 'available', id: '14d', label: '14D', reason_code: null },
        { availability: 'available', id: '30d', label: '30D', reason_code: null },
      ],
      resolved: {
        display_grain: 'day',
        reporting_window_status: 'preferred_default',
        selection: { kind: 'default', preset_id: null },
        window,
      },
      version: 1,
    },
  };
}

async function mockAuthenticatedV2(page: Page): Promise<string[]> {
  const accepts: string[] = [];
  await page.route('**/api/me', (route) => route.fulfill({
    body: JSON.stringify({
      can_manage_prompts: false,
      can_read_operational_status: false,
      operational_status_provisioned: false,
      subject: 'fictional-subject',
    }),
    contentType: 'application/json',
    status: 200,
  }));
  await page.route('**/api/presentation/**', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const payload: unknown = route.request().postDataJSON();
    const sessionId = typeof payload === 'object' && payload !== null &&
      'session_id' in payload && typeof payload.session_id === 'string'
      ? payload.session_id
      : 'conversation-fictional';
    return route.fulfill({
      body: JSON.stringify(v2WindowUnavailable(sessionId)),
      contentType: 'application/json',
      status: 200,
    });
  });
  return accepts;
}

for (const route of canonicalRoutes) {
  test(`${route} accepts only a V2 presentation response`, async ({ page }) => {
    const accepts = await mockAuthenticatedV2(page);
    const response = await page.goto(route);

    expect(response?.ok()).toBe(true);
    await expect(page.locator('main')).toBeVisible();
    expect(await page.locator('script:not([src])').count()).toBe(0);
    await expect(page.locator('main [role="alert"], main [role="status"]').first()).toBeVisible();
    await expect.poll(() => accepts.length).toBe(1);
    expect(accepts).toEqual(['application/vnd.adlytics.presentation-v2+json']);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  });
}

test('the exported not-found page needs no inline style CSP allowance', async ({ page }) => {
  // The page CloudFront serves every path outside the manifest (ADR-123); the 404 status is set
  // at the edge, so this static server answers 200. No /api/me answer: the signed-out shell.
  const response = await page.goto('/404.html');
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  // The served markup is what `style-src 'self'` judges; styles set later through the CSSOM, such
  // as Next's route announcer, need no allowance.
  const html = await response!.text();
  expect(html).not.toMatch(/<style|\sstyle=/);
  expect(await page.locator('script:not([src])').count()).toBe(0);
  expect(await page.locator('style').count()).toBe(0);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});


test('/scorecard/ renders a version-2 calendar presentation from the static export', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  // Registered after the catch-all, so Playwright tries it first for the Scorecard parent read.
  const bodies: unknown[] = [];
  await page.route('**/api/presentation/scorecard', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const payload: unknown = route.request().postDataJSON();
    bodies.push(payload);
    const sessionId = typeof payload === 'object' && payload !== null &&
      'session_id' in payload && typeof payload.session_id === 'string'
      ? payload.session_id
      : 'conversation-fictional';
    return route.fulfill({
      body: JSON.stringify({ ...scorecardWeekly, session_id: sessionId }),
      contentType: 'application/json',
      status: 200,
    });
  });
  const response = await page.goto('/scorecard/');

  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'Scorecard' })).toBeVisible();
  await expect(page.getByRole('radio', { name: /7 days/ })).toBeChecked();
  // A heading only the complete served body carries: the strict decoder accepted the fixture.
  await expect(page.getByRole('heading', { level: 2, name: 'Amazon · US marketplace · USD' })).toBeVisible();
  expect(await page.locator('script:not([src])').count()).toBe(0);
  await expect.poll(() => accepts.length).toBe(1);
  expect(accepts).toEqual(['application/vnd.adlytics.presentation-v2+json']);
  expect(bodies).toHaveLength(1);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test('/new-customers/ renders a version-2 calendar presentation from the static export', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  // Registered after the catch-all, so Playwright tries it first for the New customers read.
  const bodies: unknown[] = [];
  await page.route('**/api/presentation/new-customers', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const payload: unknown = route.request().postDataJSON();
    bodies.push(payload);
    const sessionId = typeof payload === 'object' && payload !== null &&
      'session_id' in payload && typeof payload.session_id === 'string'
      ? payload.session_id
      : 'conversation-fictional';
    return route.fulfill({
      body: JSON.stringify({ ...newCustomersRolling, session_id: sessionId }),
      contentType: 'application/json',
      status: 200,
    });
  });
  const response = await page.goto('/new-customers/');

  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'New customers' })).toBeVisible();
  await expect(page.getByRole('radio', { name: /7 days/ })).toBeChecked();
  // A table only the complete served body carries: the strict decoder accepted the fixture.
  await expect(page.getByRole('region', { name: 'New-to-brand purchases per channel' })).toBeVisible();
  expect(await page.locator('script:not([src])').count()).toBe(0);
  await expect.poll(() => accepts.length).toBe(1);
  expect(accepts).toEqual(['application/vnd.adlytics.presentation-v2+json']);
  expect(bodies).toHaveLength(1);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test('/reach/ renders the manifest-owned calendar page, proves narrow overflow accessibility and captures visual states', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  const bodies: unknown[] = [];
  let fixture: Record<string, unknown> = reachCompleteFineFixture;
  await page.route('**/api/presentation/reach', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const payload: unknown = route.request().postDataJSON();
    bodies.push(payload);
    const sessionId = typeof payload === 'object' && payload !== null &&
      'session_id' in payload && typeof payload.session_id === 'string'
      ? payload.session_id
      : 'conversation-fictional';
    return route.fulfill({
      body: JSON.stringify(reachEnvelope(sessionId, fixture)),
      contentType: 'application/json',
      status: 200,
    });
  });
  await page.setViewportSize({ height: 900, width: 1280 });
  const response = await page.goto('/reach/');

  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'Reach and funnel' })).toBeVisible();
  await expect(page.getByText('How many shoppers your ads reached, how often, and what they did next')).toBeVisible();
  await expect(page.getByRole('radio', { name: /Latest measured month/ })).toBeChecked();
  await expect(page.getByRole('heading', { level: 2, name: 'Overlap between DSP and sponsored ads' })).toBeVisible();
  await expect.poll(() => accepts.length).toBe(1);
  expect(accepts).toEqual(['application/vnd.adlytics.presentation-v2+json']);
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toEqual({ session_id: expect.any(String) });
  const html = await response!.text();
  expect(html).not.toMatch(/<style|\sstyle=/);
  expect(await page.locator('script:not([src])').count()).toBe(0);

  const screenshotDir = process.env.REACH_SCREENSHOT_DIR;
  const expectAxeClean = async () => {
    const results = await new AxeBuilder({ page })
      .options({ rules: { 'color-contrast': { enabled: true } } })
      .analyze();
    expect(results.violations).toEqual([]);
  };
  const capture = async (
    state: 'complete' | 'not-returned',
    size: 'wide' | 'narrow',
    width: number,
    theme: 'light' | 'dark',
  ) => {
    await page.setViewportSize({ height: 900, width });
    await page.locator('html').evaluate((element, nextTheme) => { element.dataset.theme = nextTheme; }, theme);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    if (screenshotDir !== undefined) {
      await page.screenshot({
        animations: 'disabled',
        fullPage: true,
        path: join(screenshotDir, `reach-${state}-${size}-${theme}.png`),
      });
    }
  };

  for (const theme of ['light', 'dark'] as const) {
    await capture('complete', 'wide', 1280, theme);
    await expectAxeClean();
  }
  for (const theme of ['light', 'dark'] as const) {
    await capture('complete', 'narrow', 768, theme);
    await expectAxeClean();
  }

  const chart = page.getByRole('img', { name: 'Purchaser rate by number of ads seen' });
  expect(await chart.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await chart.focus();
  await expect(chart).toBeFocused();
  const focusOutline = await chart.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: style.outlineWidth };
  });
  expect(focusOutline.style).not.toBe('none');
  expect(focusOutline.width).not.toBe('0px');

  await chart.evaluate((element) => { element.blur(); element.removeAttribute('tabindex'); });
  const focusControl = await new AxeBuilder({ page }).withRules(['scrollable-region-focusable']).analyze();
  expect(focusControl.violations.map((violation) => violation.id)).toContain('scrollable-region-focusable');
  await chart.evaluate((element) => element.setAttribute('tabindex', '0'));

  await page.evaluate(() => {
    const control = document.createElement('p');
    control.id = 'reach-contrast-control';
    control.textContent = 'Contrast control';
    control.style.cssText = 'background-color:#fff!important;color:#fff!important;font-size:16px!important;padding:8px';
    document.querySelector('main')?.prepend(control);
  });
  await expect(page.locator('#reach-contrast-control')).toBeVisible();
  const contrastControl = await new AxeBuilder({ page })
    .include('#reach-contrast-control')
    .withRules(['color-contrast'])
    .options({ rules: { 'color-contrast': { enabled: true } } })
    .analyze();
  const contrastResult = [
    ...contrastControl.violations,
    ...contrastControl.incomplete,
  ].find((result) => result.id === 'color-contrast');
  expect(contrastResult?.nodes.some((node) => node.target.includes('#reach-contrast-control'))).toBe(true);
  await page.locator('#reach-contrast-control').evaluate((element) => element.remove());

  fixture = reachNotReturnedFixture;
  await page.reload();
  await expect(page.locator('.reach-chart-item.gap')).toBeVisible();
  for (const theme of ['light', 'dark'] as const) {
    await capture('not-returned', 'wide', 1280, theme);
    await capture('not-returned', 'narrow', 768, theme);
  }
});

test('/retention/ renders monthly, quarterly and status states with blocking accessibility evidence', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  const bodies: unknown[] = [];
  let fixture = retentionFixture();
  await page.route('**/api/presentation/retention', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const body: unknown = route.request().postDataJSON();
    bodies.push(body);
    const sessionId = typeof body === 'object' && body !== null && 'session_id' in body && typeof body.session_id === 'string'
      ? body.session_id : 'conversation-fictional';
    return route.fulfill({ body: JSON.stringify(retentionEnvelope(sessionId, fixture)), contentType: 'application/json', status: 200 });
  });
  await page.setViewportSize({ height: 900, width: 1280 });
  const response = await page.goto('/retention/');
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'Retention' })).toBeVisible();
  await expect(page.getByText('Cohorts through September 2026', { exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Cohorts as of' })).toHaveCount(0);
  await expect(page.locator('.retention-cell.not_returned > .retention-status')).toBeVisible();
  await expect(page.locator('.retention-cell.not_yet > .retention-status').first()).toBeVisible();
  expect(bodies[0]).toEqual({ session_id: expect.any(String) });
  expect(accepts[0]).toBe('application/vnd.adlytics.presentation-v2+json');

  const screenshotDir = process.env.UI_SCREENSHOT_DIR;
  const axeClean = async () => {
    const results = await new AxeBuilder({ page }).include('.retention-view').options({ rules: { 'color-contrast': { enabled: true } } }).analyze();
    expect(results.violations).toEqual([]);
    expect(results.incomplete).toEqual([]);
  };
  const capture = async (state: 'complete-monthly' | 'quarterly' | 'not-returned' | 'all-absent' | 'two-market', size: 'wide' | 'narrow', width: number, theme: 'light' | 'dark') => {
    await page.setViewportSize({ height: 900, width });
    await page.locator('html').evaluate((element, next) => { element.dataset.theme = next; }, theme);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await axeClean();
    if (screenshotDir !== undefined) await page.screenshot({ animations: 'disabled', fullPage: true, path: join(screenshotDir, `retention-${state}-${size}-${theme}.png`) });
  };
  const captureState = async (state: 'complete-monthly' | 'quarterly' | 'not-returned' | 'all-absent' | 'two-market') => {
    for (const [size, width] of [['wide', 1280], ['narrow', 768]] as const) {
      for (const theme of ['light', 'dark'] as const) await capture(state, size, width, theme);
    }
  };

  await captureState('complete-monthly');
  const region = page.getByRole('region', { name: /Share of each cohort/ });
  await page.setViewportSize({ height: 900, width: 768 });
  expect(await region.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await region.focus();
  await expect(region).toBeFocused();

  await page.evaluate(() => {
    const control = document.createElement('p');
    control.id = 'retention-contrast-control'; control.textContent = 'Contrast control';
    control.style.cssText = 'background-color:#fff!important;color:#fff!important;font-size:16px!important;padding:8px';
    document.querySelector('main')?.prepend(control);
  });
  const contrast = await new AxeBuilder({ page }).include('#retention-contrast-control').withRules(['color-contrast']).analyze();
  const finding = [...contrast.violations, ...contrast.incomplete].find((item) => item.id === 'color-contrast');
  expect(finding?.nodes.some((node) => node.target.includes('#retention-contrast-control'))).toBe(true);
  await page.locator('#retention-contrast-control').evaluate((element) => element.remove());

  fixture = cloneRetention(retentionFixture());
  const quarterlyMarket = (fixture.markets as Record<string, unknown>[])[0]!;
  quarterlyMarket.cohort_grain = 'quarter';
  quarterlyMarket.scope_band = 'Cohorts as of September 2026, grouped by quarter: acquired Q4 2025 to Q2 2026, with repeat quarters through Q3 2026.';
  quarterlyMarket.grain_note = 'Cohorts are grouped by quarter from Q4 2026, because too many monthly cells had too few shoppers.';
  const quarterlyTriangle = quarterlyMarket.triangle as Record<string, unknown>;
  for (const grid of quarterlyTriangle.standard as Record<string, unknown>[]) {
    for (const row of grid.rows as Record<string, unknown>[]) {
      row.label = 'Q2 2026';
      const cells = row.cells as Record<string, unknown>[];
      row.cells = cells.slice(0, 3);
      for (const cell of row.cells as Record<string, unknown>[]) cell.column_label = `Quarter ${cell.offset}`;
    }
  }
  const quarterlyCurve = quarterlyMarket.curve as Record<string, unknown>;
  for (const line of quarterlyCurve.lines as Record<string, unknown>[]) {
    for (const point of line.points as Record<string, unknown>[]) point.label = `Quarter ${point.offset}`;
    line.points = (line.points as Record<string, unknown>[]).slice(0, 3);
  }
  await page.reload();
  await expect(page.getByText(/grouped by quarter from Q4 2026/)).toBeVisible();
  await captureState('quarterly');

  fixture = cloneRetention(retentionFixture());
  const firstCell = (((((fixture.markets as Record<string, unknown>[])[0]!.triangle as Record<string, unknown>).standard as Record<string, unknown>[])[1]!.rows as Record<string, unknown>[])[0]!.cells as Record<string, unknown>[])[0]!;
  Object.assign(firstCell, { cohort_size: null, rate: null, state: 'not_returned', text: 'Not available' });
  await page.reload();
  await expect(page.locator('.retention-cell.not_returned > .retention-status').first()).toBeVisible();
  await captureState('not-returned');

  fixture = cloneRetention(retentionFixture());
  const allAbsent = (((fixture.markets as Record<string, unknown>[])[0]!.triangle as Record<string, unknown>).standard as Record<string, unknown>[])[1]!;
  for (const row of allAbsent.rows as Record<string, unknown>[]) {
    row.cells = [];
    row.absent_text = 'No cohort was measured for this acquisition group.';
  }
  await page.reload();
  await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Month 1' })).toHaveCount(0);
  await captureState('all-absent');

  // Two marketplaces: each leads with its server-named heading, and axe's landmark-unique and
  // heading rules run against the supported multi-market shape.
  fixture = retentionTwoMarketFixture();
  await page.reload();
  for (const market of ['US', 'UK'] as const) {
    await expect(page.getByRole('heading', { level: 2, name: `${market} marketplace` })).toBeVisible();
    await expect(page.getByRole('region', { name: `${market} marketplace Repeat purchase by cohort` })).toBeVisible();
  }
  await captureState('two-market');
});

test('/audiences/ renders paid-unavailable, paid, DSP and campaign-reason states with blocking accessibility evidence', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  const bodies: unknown[] = [];
  let fixture = structuredClone(audiencesNotObservedFixture) as Record<string, unknown>;
  await page.route('**/api/presentation/audiences', (route) => {
    accepts.push(route.request().headers().accept ?? '');
    const body: unknown = route.request().postDataJSON(); bodies.push(body);
    const sessionId = typeof body === 'object' && body !== null && 'session_id' in body && typeof body.session_id === 'string' ? body.session_id : 'conversation-fictional';
    return route.fulfill({ body: JSON.stringify(audiencesEnvelope(sessionId, fixture)), contentType: 'application/json', status: 200 });
  });
  await page.setViewportSize({ height: 900, width: 1280 });
  const response = await page.goto('/audiences/');
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { level: 1, name: 'Audiences' })).toBeVisible();
  await expect(page.getByText('Showing September 2026 (Latest measured month).')).toBeVisible();
  await expect(page.getByRole('radio', { name: 'DSP-exposed buyers only' })).toBeChecked();
  await expect(page.getByRole('radio', { name: 'New-to-brand buyers' })).toBeVisible();
  await expect(page.getByText('7.00 USD')).toBeVisible();
  await expect(page.getByRole('group', { name: 'Window' })).toHaveCount(0);
  expect(bodies[0]).toEqual({ session_id: expect.any(String) });
  expect(accepts[0]).toBe('application/vnd.adlytics.presentation-v2+json');

  const screenshotDir = process.env.UI_SCREENSHOT_DIR;
  const axeClean = async () => {
    const result = await new AxeBuilder({ page }).include('.audiences-view').options({ rules: { 'color-contrast': { enabled: true } } }).analyze();
    expect(result.violations).toEqual([]); expect(result.incomplete).toEqual([]);
  };
  const captureState = async (state: 'paid-unavailable' | 'complete-paid' | 'DSP-default' | 'campaign-reasons' | 'insufficient-campaigns' | 'in-roster-unavailable' | 'in-roster-unavailable-new-to-brand') => {
    for (const [size, width] of [['wide', 1280], ['narrow', 768]] as const) for (const theme of ['light', 'dark'] as const) {
      await page.setViewportSize({ height: 900, width });
      await page.locator('html').evaluate((element, next) => { element.dataset.theme = next; }, theme);
      await axeClean();
      if (screenshotDir !== undefined) await page.screenshot({ animations: 'disabled', fullPage: true, path: join(screenshotDir, `audiences-${state}-${size}-${theme}.png`) });
    }
  };
  await expect(page.getByRole('note')).toContainText('Available with the paid feature');
  await page.getByRole('radio', { name: 'Amazon audience segments (paid)' }).click();
  await expect(page.locator('.audience-profile').getByRole('status')).toContainText('Needs the Amazon Insights paid feature');
  await captureState('paid-unavailable');

  fixture = structuredClone(audiencesFixture) as Record<string, unknown>;
  await page.reload();
  await expect(page.getByRole('radio', { name: 'DSP-exposed buyers only' })).toBeChecked();
  await captureState('DSP-default');

  await page.evaluate(() => {
    const control = document.createElement('p'); control.id = 'audiences-contrast-control'; control.textContent = 'Contrast control';
    control.style.cssText = 'background-color:#fff!important;color:#fff!important;font-size:16px!important;padding:8px'; document.querySelector('main')?.prepend(control);
  });
  const contrast = await new AxeBuilder({ page }).include('#audiences-contrast-control').withRules(['color-contrast']).analyze();
  const finding = [...contrast.violations, ...contrast.incomplete].find((item) => item.id === 'color-contrast');
  expect(finding?.nodes.some((node) => node.target.includes('#audiences-contrast-control'))).toBe(true);
  await page.locator('#audiences-contrast-control').evaluate((element) => element.remove());

  fixture = structuredClone(audiencesFixture) as Record<string, unknown>;
  const market = (fixture.markets as Record<string, unknown>[])[0]!;
  const [paid, dsp] = market.views as Record<string, unknown>[];
  market.default_view = 'asi_all_exposed'; paid!.paid_state = 'observed'; paid!.callout = null;
  const dspDelivery = ((dsp!.deliveries as Record<string, unknown>[])[0])!;
  for (const delivery of paid!.deliveries as Record<string, unknown>[]) {
    delivery.returned = true; delivery.total_returned = true; delivery.total_text = null;
    delivery.profiled_buyers_total = 120; delivery.profiled_buyers_total_text = '120'; delivery.profiled_exposed_total = 2400; delivery.profiled_exposed_total_text = '2400'; delivery.period_text = null;
    delivery.paid_state = { expected_after: '2026-10-16', feature_observed: true, latest_available_period: { end: '2026-09-30', start: '2026-09-01' }, period_published: true };
    delivery.insights = structuredClone(dspDelivery.insights);
  }
  paid!.rows = (paid!.deliveries as Record<string, unknown>[]).flatMap((delivery) => (dsp!.rows as Record<string, unknown>[]).map((row) => ({ ...structuredClone(row), profile_path: delivery.profile_path })));
  await page.reload();
  await expect(page.getByRole('radio', { name: 'Amazon audience segments (paid)' })).toBeChecked();
  await expect(page.locator('.audience-insights .kind').filter({ hasText: 'In-market' })).toBeVisible();
  await expect(page.locator('.audience-insights .kind').filter({ hasText: 'Lifestyle' })).toBeVisible();
  await page.getByRole('radio', { name: 'New-to-brand buyers' }).click();
  await expect(page.getByRole('radio', { name: 'New-to-brand buyers' })).toBeChecked();
  await expect(page.getByRole('columnheader', { name: 'New-to-brand buyers' })).toHaveCount(2);
  await captureState('complete-paid');

  fixture = structuredClone(audiencesFixture) as Record<string, unknown>;
  const campaign = (((fixture.markets as Record<string, unknown>[])[0]!.campaign_audiences as Record<string, unknown>).rows as Record<string, unknown>[])[0]!;
  campaign.segment_cpm = null; campaign.cpm_text = 'Not available: no impressions came back for this segment.';
  campaign.segment_roas_attributed = null; campaign.segment_roas_attributed_text = null; campaign.roas_text = "Not available: attributed sales didn't come back for this segment.";
  await page.reload();
  await expect(page.getByText('Not available: no impressions came back for this segment.')).toBeVisible();
  await captureState('campaign-reasons');

  fixture = structuredClone(audiencesInRosterUnavailableFixture) as Record<string, unknown>;
  fixture.outcome = 'insufficient';
  fixture.starter_questions = [];
  ((fixture.markets as Record<string, unknown>[])[0]!).outcome = 'insufficient';
  await page.reload();
  const monthScope = page.getByText('Showing September 2026 (Latest measured month).');
  const campaignSection = page.getByRole('heading', { name: 'DSP campaigns by segment' }).locator('..');
  await expect(monthScope).toBeVisible();
  await expect(campaignSection).toBeVisible();
  expect(await monthScope.evaluate((scope) => {
    const section = document.querySelector('.audience-campaigns');
    return section !== null && Boolean(scope.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING);
  })).toBe(true);
  await captureState('insufficient-campaigns');

  fixture = structuredClone(audiencesInRosterUnavailableFixture) as Record<string, unknown>;
  await page.reload();
  await expect(page.getByRole('radio', { name: 'Amazon audience segments (paid)' })).toBeChecked();
  const totalReason = "Not available: this month's profiled total didn't come back, so no share or index is computed.";
  const inMarketRow = page.getByRole('table', { name: /In-market/ }).getByRole('row', { name: /Segment 41001/ });
  const expectOneReason = async () => {
    await expect(inMarketRow.getByText(totalReason, { exact: true })).toHaveCount(1);
    await expect(inMarketRow.locator('td[aria-describedby] > span[aria-hidden="true"]')).toHaveCount(2);
  };
  await expectOneReason();
  await captureState('in-roster-unavailable');
  await page.getByRole('radio', { name: 'New-to-brand buyers' }).click();
  await expect(page.getByRole('columnheader', { name: 'New-to-brand buyers' })).toHaveCount(2);
  await expectOneReason();
  await expect(inMarketRow).not.toContainText('16');
  await captureState('in-roster-unavailable-new-to-brand');
});

test('/audiences/ rejects every query string before fetching', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  const response = await page.goto('/audiences/?preset=last_complete_month&extra=1');
  expect(response?.ok()).toBe(true);
  await expect(page.locator('main [role="alert"]')).toContainText('This page request is no longer available.');
  expect(accepts).toEqual([]);
});

for (const query of ['asof=2026-09', 'as_of_month=2026-09', 'asof=2026-9&extra=1']) {
  test(`/retention/ gives ?${query} an explicit zero-fetch validation error`, async ({ page }) => {
    const accepts = await mockAuthenticatedV2(page);
    const response = await page.goto(`/retention/?${query}`);
    expect(response?.ok()).toBe(true);
    await expect(page.locator('main [role="alert"]')).toContainText('This page request is no longer available.');
    expect(accepts).toEqual([]);
  });
}

test('/reach/ renders an explicit validation error for an unsupported URL preset', async ({ page }) => {
  const accepts = await mockAuthenticatedV2(page);
  const response = await page.goto('/reach/?preset=7d');

  expect(response?.ok()).toBe(true);
  await expect(page.locator('main [role="alert"]')).toContainText('This page request is no longer available. Refresh the page before selecting again.');
  expect(accepts).toEqual([]);
  expect(new URL(page.url()).searchParams.get('preset')).toBe('7d');
});

test('campaign window recovery keeps internal response codes out of the command surface', async ({ page }) => {
  await mockAuthenticatedV2(page);
  await page.goto('/campaigns/');
  // The absence callout's paragraph; the persistent status region repeats it for announcement.
  await expect(page.locator('main section.callout p').getByText('Campaign reporting is unavailable for the selected period.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('fictional_window_unavailable');
  await expect(page.locator('body')).not.toContainText('reason_code');
});

/* ---------- ADR-139: page-level absence states (catalog `docs/ui/page-absence-copy.md`) ---------- */

const PAGE_STATUS = 'main p.sr-only[role="status"]';
const WAIT_FOR_SOURCE = { action_id: null, no_self_service_reason: 'wait_for_source_coverage' };

const REACH_ABSENCE = "Reach for September 2026 isn't ready to show yet. It's built from your Amazon Marketing Cloud sponsored ads data, plus Amazon DSP where you run it. When it's ready, you'll see the reach, frequency and full-funnel figures your data supports for this period. Other windows above are ready to view.";
const AUDIENCES_ABSENCE = "Audiences for September 2026 isn't ready to show yet. It's built from your Amazon Marketing Cloud audience segment data. When it's ready, you'll see which audience segments your buyers belong to, where there are enough shoppers to show them.";
const RETENTION_ABSENCE = "Retention isn't ready to show yet. It's built from your Amazon Marketing Cloud new-to-brand data. When it's ready, you'll see how many of the new customers your ads won come back to buy again, where there's enough evidence to show it.";

function reachAbsence(sessionId: string) {
  const temporal = structuredClone(reachNoArtifactPendingEnvelope.temporal) as Record<string, unknown>;
  const month = (temporal.presets as Record<string, unknown>[])[1]!;
  Object.assign(month, { reason_code: 'preset_unavailable', reason_text: 'Not available yet' });
  // The page's first request names no preset, so the server resolves its default.
  Object.assign(temporal.resolved as Record<string, unknown>, {
    reporting_window_status: 'preferred_default',
    selection: { kind: 'default', preset_id: null },
  });
  return {
    ...structuredClone(reachNoArtifactPendingEnvelope),
    message: REACH_ABSENCE,
    reason_code: 'preset_unavailable',
    recovery: WAIT_FOR_SOURCE,
    session_id: sessionId,
    temporal,
  };
}

function audiencesAbsence(sessionId: string) {
  const temporal = structuredClone(audiencesEnvelope(sessionId, audiencesFixture as Record<string, unknown>).temporal);
  Object.assign(temporal.presets[0]!, { availability: 'unavailable', reason_code: 'preset_unavailable', reason_text: 'Not available yet' });
  return {
    envelope_version: 2, expected_after: null, kind: 'window_unavailable', message: AUDIENCES_ABSENCE,
    reason_code: 'preset_unavailable', recovery: WAIT_FOR_SOURCE, session_id: sessionId, temporal,
  };
}

function retentionAbsence(sessionId: string) {
  return {
    envelope_version: 2, expected_after: null, kind: 'window_unavailable', message: RETENTION_ABSENCE,
    reason_code: 'vintage_unavailable', recovery: WAIT_FOR_SOURCE, session_id: sessionId,
    temporal: { mode: 'fixed', resolved: null, version: 2 },
  };
}

function sessionOf(payload: unknown): string {
  return typeof payload === 'object' && payload !== null && 'session_id' in payload && typeof payload.session_id === 'string'
    ? payload.session_id
    : 'conversation-fictional';
}

const ABSENCES = [
  { envelope: reachAbsence, message: REACH_ABSENCE, path: '/reach/', route: 'reach', title: 'Reach and funnel' },
  { envelope: audiencesAbsence, message: AUDIENCES_ABSENCE, path: '/audiences/', route: 'audiences', title: 'Audiences' },
  { envelope: retentionAbsence, message: RETENTION_ABSENCE, path: '/retention/', route: 'retention', title: 'Retention' },
  {
    envelope: (sessionId: string) => ({ ...scorecardPending, session_id: sessionId }),
    message: scorecardPending.message, path: '/scorecard/', route: 'scorecard', title: 'Scorecard',
  },
  {
    envelope: (sessionId: string) => ({ ...newCustomersPending, session_id: sessionId }),
    message: newCustomersPending.message, path: '/new-customers/', route: 'new-customers', title: 'New customers',
  },
] as const;

for (const absence of ABSENCES) {
  test(`${absence.path} renders its catalog absence in light and dark, axe-clean with contrast, announced by the one status region`, async ({ page }) => {
    await mockAuthenticatedV2(page);
    // Hold the parent response until the status region is captured, so its identity is observed
    // from the loading state through the absence (temporal contract §6, WCAG 4.1.3).
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => { release = resolve; });
    await page.route(`**/api/presentation/${absence.route}`, async (route) => {
      await held;
      const sessionId = sessionOf(route.request().postDataJSON());
      return route.fulfill({ body: JSON.stringify(absence.envelope(sessionId)), contentType: 'application/json', status: 200 });
    });
    await page.setViewportSize({ height: 900, width: 1280 });
    const response = await page.goto(absence.path);
    expect(response?.ok()).toBe(true);
    const region = page.locator(PAGE_STATUS);
    await expect(region).toHaveCount(1);
    await expect(region).toHaveText('');
    await expect(region).toHaveAttribute('aria-atomic', 'true');
    await page.evaluate((selector) => {
      (window as unknown as { adlyticsStatusRegion?: Element | null }).adlyticsStatusRegion = document.querySelector(selector);
    }, PAGE_STATUS);
    release();

    const callout = page.locator('main section.callout').filter({ has: page.getByRole('heading', { level: 2, name: absence.title }) });
    await expect(callout).toBeVisible();
    await expect(callout.locator('p')).toHaveText(absence.message);
    // No live role on the callout and no nested paragraph: the persistent region announces it.
    await expect(callout).not.toHaveAttribute('role', /.+/);
    await expect(callout.locator('p p')).toHaveCount(0);
    await expect(region).toHaveText(`${absence.title}. ${absence.message}`);
    // Same element instance from loading to absence: React never remounted the live region.
    expect(await page.evaluate((selector) => (
      (window as unknown as { adlyticsStatusRegion?: Element | null }).adlyticsStatusRegion === document.querySelector(selector)
    ), PAGE_STATUS)).toBe(true);

    const screenshotDir = process.env.UI_SCREENSHOT_DIR;
    for (const theme of ['light', 'dark'] as const) {
      await page.locator('html').evaluate((element, next) => { element.dataset.theme = next; }, theme);
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      const results = await new AxeBuilder({ page }).options({ rules: { 'color-contrast': { enabled: true } } }).analyze();
      expect(results.violations).toEqual([]);
      if (screenshotDir !== undefined) {
        await page.screenshot({ animations: 'disabled', fullPage: true, path: join(screenshotDir, `${absence.route}-absence-${theme}.png`) });
      }
    }
  });
}

test('/campaigns/ renders its unchanged browser absence copy in light and dark, axe-clean with contrast', async ({ page }) => {
  await mockAuthenticatedV2(page);
  await page.goto('/campaigns/');
  const callout = page.locator('main section.callout').filter({ has: page.getByRole('heading', { level: 2, name: 'Campaign portfolio is unavailable for this period' }) });
  await expect(callout).toBeVisible();
  await expect(callout.locator('p')).toHaveText('Campaign reporting is unavailable for the selected period. Choose another available period to view campaign results.');
  await expect(page.locator('main .state-note')).toHaveCount(0);
  await expect(page.locator(PAGE_STATUS)).toHaveText(/^Campaign portfolio is unavailable for this period\. Campaign reporting/);
  for (const theme of ['light', 'dark'] as const) {
    await page.locator('html').evaluate((element, next) => { element.dataset.theme = next; }, theme);
    const results = await new AxeBuilder({ page }).options({ rules: { 'color-contrast': { enabled: true } } }).analyze();
    expect(results.violations).toEqual([]);
  }
});

test('a serving refusal shows the server\'s own text, and the status region keeps its instance into the page issue', async ({ page }) => {
  await mockAuthenticatedV2(page);
  const served = "This page's latest data is still being checked before it's shown. Try again later.";
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/presentation/reach', async (route) => {
    await held;
    return route.fulfill({
      body: JSON.stringify({ code: 'ADLY-PRESENTATION-0002', error: served, error_class: 'domain' }),
      contentType: 'application/json',
      status: 409,
    });
  });
  await page.goto('/reach/');
  await expect(page.locator(PAGE_STATUS)).toHaveCount(1);
  await page.evaluate((selector) => {
    (window as unknown as { adlyticsStatusRegion?: Element | null }).adlyticsStatusRegion = document.querySelector(selector);
  }, PAGE_STATUS);
  release();
  await expect(page.locator('main [role="alert"]')).toContainText(served);
  await expect(page.locator(PAGE_STATUS)).toHaveText('');
  expect(await page.evaluate((selector) => (
    (window as unknown as { adlyticsStatusRegion?: Element | null }).adlyticsStatusRegion === document.querySelector(selector)
  ), PAGE_STATUS)).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
