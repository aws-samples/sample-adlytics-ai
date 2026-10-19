// SPDX-License-Identifier: MIT-0
/** Validate the authenticated command-center UI without printing page or credential values. */

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

import { changeChipChecks } from './change_chip_checks.mjs';
import { rosterRoutes } from './roster_routes.mjs';
import { shellKeyboardChecks } from './shell_keyboard_checks.mjs';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const AxeBuilder = require('@axe-core/playwright').default;
const { chromium } = require('@playwright/test');

const BASE_URL = (process.env.ADL_BASE_URL?.trim() ?? '').replace(/\/$/, '');
const refresh = process.env.ADL_REFRESH?.trim() ?? '';
const csrf = process.env.ADL_CSRF?.trim() ?? '';
if (!BASE_URL || !refresh || !csrf) {
  throw new Error('Source validation/.env.local (ADL_BASE_URL, ADL_REFRESH, ADL_CSRF) before running this probe.');
}
let deepSectionsObserved = 0;

async function expectNoAxeViolations(page, surface) {
  const accessibility = await new AxeBuilder({ page }).analyze();
  const violationIds = accessibility.violations.map((violation) => violation.id);
  if (violationIds.length > 0) {
    throw new Error(`${surface} accessibility violations: ${violationIds.join(', ')}`);
  }
}

/**
 * Insight-presentation contract §3: every insight card opens the insight dialog; the
 * switcher hops the page roster without closing; Escape returns focus to the opening
 * card; axe passes on the rendered dialog. Model-authored text is never pattern-checked
 * here (ADR-082): its quality is measured by calibration and evaluation.
 */
async function insightDialogChecks(page, surface) {
  const openers = page.locator('.narr-card .open-insight');
  const opener = openers.first();
  await opener.waitFor({ timeout: 30_000 });
  if (await openers.count() < 2) {
    throw new Error(`${surface} carries fewer than two insight cards; the dialog hop cannot be exercised.`);
  }
  // textContent, because the focus-return poll below reads textContent too.
  const openerLabel = (await opener.textContent()) ?? '';
  await opener.click();
  const dialog = page.locator('[role="dialog"][aria-labelledby="insight-focus-heading"]');
  await dialog.waitFor({ timeout: 30_000 });
  // Design revision 2026-09-25.1: single-column dialog with one customer-word
  // provenance strip; the old Provenance rail no longer exists.
  await dialog.locator('.prov-strip').waitFor({ timeout: 10_000 });
  // The ADR-076 deep section starts on a filled card's dialog open; axe must run
  // against the EXTENDED dialog, so wait for its terminal render (a filled
  // standout or the designed-absence line). Movers dialogs carry no section, so
  // presence is soft here and the walk-level floor below requires at least one.
  const deepSection = dialog.locator('.deep-section');
  const deepPresent = await deepSection.first().waitFor({ timeout: 10_000 })
    .then(() => true).catch(() => false);
  if (deepPresent) {
    // Terminal render only: the staged preparation caption also carries `quiet`,
    // so exclude it or a loading section would attest as terminal.
    await deepSection.locator('.deep-standout, p.quiet:not(.deep-progress)').first().waitFor({ timeout: 90_000 });
    deepSectionsObserved += 1;
  }
  await expectNoAxeViolations(page, `${surface} insight focus`);
  const heading = dialog.locator('h2').first();
  const before = await heading.innerText();
  await dialog.getByRole('button', { name: 'Next insight' }).click();
  await heading.filter({ hasNotText: before }).waitFor({ timeout: 10_000 });
  if (await dialog.count() !== 1) throw new Error(`${surface} insight switcher closed the dialog.`);
  await dialog.getByRole('button', { name: 'Previous insight' }).click();
  await heading.filter({ hasText: before }).waitFor({ timeout: 10_000 });
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'detached', timeout: 10_000 });
  await page.waitForFunction(
    (text) => document.activeElement?.textContent?.includes(text) ?? false,
    openerLabel,
    { timeout: 5_000 },
  ).catch(() => { throw new Error(`${surface} insight dialog did not return focus to its opening card.`); });
}

const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  await context.addCookies([
    { name: 'adl_refresh', value: refresh, url: BASE_URL, httpOnly: true, secure: true, sameSite: 'Lax' },
    { name: 'adl_csrf', value: csrf, url: BASE_URL, httpOnly: false, secure: true, sameSite: 'Lax' },
  ]);
  const page = await context.newPage();
  const browserErrors = [];
  const failedRequests = [];
  const portfolioResponses = [];
  const chatFailures = [];
  const conversationSessionIds = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname !== '/api/chat' || request.method() !== 'POST') return;
    try {
      const body = request.postDataJSON();
      if (typeof body?.session_id === 'string') conversationSessionIds.push(body.session_id);
    } catch {
      // The browser probe validates the request only when it is the documented JSON chat shape.
    }
  });
  page.on('console', (message) => {
    if (
      message.type() === 'error' &&
      !/Failed to load resource: the server responded with a status of 403/.test(message.text())
    ) {
      browserErrors.push(message.text().slice(0, 120));
    }
  });
  page.on('pageerror', (error) => browserErrors.push(error.name));
  page.on('response', async (response) => {
    const pathname = new URL(response.url()).pathname;
    if (pathname === '/api/presentation/campaigns') {
      let errorClass = null;
      if (response.status() >= 400) {
        try {
          const body = await response.json();
          if (typeof body?.error_class === 'string') errorClass = body.error_class;
        } catch {
          // The probe retains only a bounded error classification when one is present.
        }
      }
      portfolioResponses.push({ errorClass, status: response.status() });
    }
    if (pathname === '/api/chat' && response.status() >= 400) {
      let description = 'unreadable';
      try {
        description = (await response.text()).slice(0, 512);
      } catch {
        // The response may be unavailable if the page closes before Playwright reads it.
      }
      chatFailures.push({ description, status: response.status() });
    }
    if (response.status() >= 400) {
      failedRequests.push(`${pathname}:${response.status()}`);
    }
  });

  // 1 · portfolio band, windows, and outcome agree with the returned rows.
  await page.goto(`${BASE_URL}/campaigns/`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Campaigns', level: 1 }).waitFor();
  await page.getByRole('radiogroup', { name: 'Available reporting periods' }).waitFor();
  const readyPortfolio = page.getByRole('heading', { name: 'Campaigns', level: 2 });
  // The page-level absence renders through the shared PageAbsence callout (ADR-139); the body
  // outcomes keep their `.state-note`.
  const closedPortfolio = page.locator('.state-note', { hasText: /No campaigns for this reporting window|Campaign evidence needs another window/ })
    .or(page.locator('main section.callout', { hasText: 'Campaign portfolio is unavailable for this period' }));
  await Promise.race([
    readyPortfolio.waitFor({ timeout: 60_000 }),
    closedPortfolio.first().waitFor({ timeout: 60_000 }),
  ]);
  const campaignReady = await readyPortfolio.count() > 0;
  const campaignRows = await page.locator('table.camp-table tbody tr').count();
  if (campaignReady !== (campaignRows > 0)) {
    const outcome = portfolioResponses.length === 1
      ? portfolioResponses[0]
      : { errorClass: null, status: 'none-or-multiple' };
    throw new Error(
      `Campaign portfolio outcome and available rows disagree: status=${outcome.status}, ` +
      `error_class=${outcome.errorClass ?? 'none'}.`,
    );
  }
  let campaignChips = 'not-observed';
  let estateReach = 'not-observed';
  let displayPaging = 'not-observed';
  if (campaignReady) {
    const insightCards = await page.locator('.narr-row .narr-card').count();
    const tiles = await page.locator('.tiles .tile').count();
    if (insightCards < 4 || tiles < 4) {
      throw new Error(`Portfolio composition incomplete: insight_cards=${insightCards}, tiles=${tiles}.`);
    }
    // ADR-121: every tile shows the server chip with the tone its declared direction gives.
    campaignChips = await changeChipChecks(page, 'Campaigns', 'main');
    // Display paging (owner mandate 2026-09-23): ten rows per page over the baked
    // 50-row segments; page turns inside a segment are request-free.
    if (campaignRows > 10) {
      throw new Error(`Display paging did not bound the table: rows=${campaignRows}.`);
    }
    const pager = page.getByRole('navigation', { name: 'Campaign table position and paging' });
    await pager.waitFor({ timeout: 10_000 });
    const pagerText = await pager.innerText();
    if (!/Rows \d+–\d+ of [\d,]+/.test(pagerText) || !/Page \d+ of \d+/.test(pagerText) || !/sorted by /.test(pagerText)) {
      throw new Error(`Campaign pagination state is malformed: ${pagerText}`);
    }
    const nextRows = page.getByRole('button', { name: 'Next 10 rows' });
    if (!(await nextRows.isEnabled())) {
      throw new Error('The governed visible estate did not expose its required tail pages.');
    }
    // Walk the loaded segment: pages 2–5 need no portfolio request (the last slice
    // fires the sibling prefetch, which is the one request the crossing then reuses).
    const requestsBeforePaging = portfolioResponses.length;
    for (let step = 0; step < 4; step += 1) {
      await nextRows.click();
    }
    await pager.getByText(/Page 5 of \d+/).waitFor({ timeout: 10_000 });
    // Crossing into the tail segment.
    await nextRows.click();
    await pager.getByText(/Rows 51–\d+ of [\d,]+/).waitFor({ timeout: 60_000 });
    const tailRows = await page.locator('table.camp-table tbody tr').count();
    if (tailRows <= 0 || tailRows > 10) {
      throw new Error(`Campaign tail page cardinality is invalid: tail=${tailRows}.`);
    }
    const boundaryRequests = portfolioResponses.length - requestsBeforePaging;
    if (boundaryRequests !== 1) {
      throw new Error(`Display paging request discipline violated: ${boundaryRequests} portfolio requests for one boundary walk.`);
    }
    // Backward crossing lands on the previous segment's last slice, never its first.
    await page.getByRole('button', { name: 'Previous 10 rows' }).click();
    await pager.getByText(/Rows 41–50 of [\d,]+/).waitFor({ timeout: 60_000 });
    for (let step = 0; step < 4; step += 1) {
      await page.getByRole('button', { name: 'Previous 10 rows' }).click();
    }
    await pager.getByText(/Page 1 of \d+/).waitFor({ timeout: 10_000 });
    displayPaging = 'ok';
    const estateFilter = page.getByLabel('Filter campaigns by name');
    await estateFilter.fill('northstar');
    const rosterMatches = await page.getByRole('list', { name: 'Matching campaigns' })
      .locator('button').count();
    await estateFilter.fill('');
    estateReach = rosterMatches > 0 ? 'ok' : 'filter-no-match';
  }
  await expectNoAxeViolations(page, 'Portfolio');

  // The incident escaped defaults-only acceptance. Exercise one non-default baked variant.
  const campaignFourteenDays = page.getByRole('radio', { name: '14D' });
  await campaignFourteenDays.click();
  await page.getByRole('radio', { name: '14D', checked: true }).waitFor({ timeout: 60_000 });
  await readyPortfolio.waitFor({ timeout: 60_000 });
  await expectNoAxeViolations(page, 'Portfolio 14D preset');

  // The 1D preset is a served candidate: a one-day portfolio with its one-day prior.
  const campaignOneDay = page.getByRole('radio', { name: '1D' });
  await campaignOneDay.click();
  await page.getByRole('radio', { name: '1D', checked: true }).waitFor({ timeout: 60_000 });
  await readyPortfolio.waitFor({ timeout: 60_000 });
  await expectNoAxeViolations(page, 'Portfolio 1D preset');
  await page.getByRole('radio', { name: '7D' }).click();
  await page.getByRole('radio', { name: '7D', checked: true }).waitFor({ timeout: 60_000 });
  await readyPortfolio.waitFor({ timeout: 60_000 });

  // 2 · assistant answers, and a reload preserves the opaque conversation selector.
  const composer = page.getByLabel('Ask the workspace');
  await composer.fill('Which metrics can you report on?');
  await page.getByRole('button', { name: 'Submit' }).click();
  await page.locator('.assistant .msg.agent').last().waitFor({ timeout: 120_000 });
  const assistantMessages = await page.locator('.assistant .msg.agent').count();
  if (assistantMessages < 1) throw new Error('The assistant rendered no response.');
  if (await page.getByText(/could not complete this response/i).count()) {
    throw new Error('The assistant rendered its failure state.');
  }

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Campaigns', level: 1 }).waitFor();
  const reloadedComposer = page.getByLabel('Ask the workspace');
  await reloadedComposer.fill(
    'Which declared advertising metric from my previous question measures spend?',
  );
  await page.getByRole('button', { name: 'Submit' }).click();
  await page.locator('.assistant .msg.agent').last().waitFor({ timeout: 120_000 });
  if (await page.getByText(/could not complete this response/i).count()) {
    throw new Error('The assistant rendered its failure state after reload.');
  }
  if (conversationSessionIds.length !== 2 || conversationSessionIds[0] !== conversationSessionIds[1]) {
    throw new Error('The browser reload did not preserve the opaque conversation selector.');
  }

  // 3 · the shell's keyboard path (ADR-118 decisions 2 and 3): skip links, the rail tooltip,
  //     the menu sheet below 881 px, and the assistant docked at every width with no launcher.
  const shellKeyboard = await shellKeyboardChecks(page, { expectNoAxeViolations });

  // 4 · campaign detail and the metric-focus modal.
  await page.setViewportSize({ width: 1500, height: 1000 });
  let campaignDetail = false;
  let metricFocus = false;
  let evidenceStatusSheet = 'not-observed';
  let coverageMissingDates = 'not-observed';
  let campaignSwitcher = 'not-observed';
  const insightFocusSurfaces = [];
  if (campaignReady) {
    const gapCampaign = 'Northstar Sponsored | Flight 30';
    const gapFilter = page.getByLabel('Filter campaigns by name');
    await gapFilter.fill(gapCampaign);
    const gapMatch = page.getByRole('list', { name: 'Matching campaigns' })
      .getByRole('button', { name: new RegExp(gapCampaign) });
    await gapMatch.click();
    const gapRow = page.locator('table.camp-table .name button', { hasText: gapCampaign });
    await gapRow.waitFor({ timeout: 60_000 });
    const openedName = (await gapRow.innerText()).split('\n')[0].trim();
    await gapRow.click();
    // The detail fills the surface alone: its identity moves into the title band,
    // the portfolio table leaves the page, and the breadcrumb carries the way back.
    await page.getByRole('heading', { name: openedName, level: 1 }).waitFor({ timeout: 30_000 });
    await page.getByRole('heading', { name: 'Delivery evidence', level: 2 }).waitFor();
    if (await page.locator('table.camp-table').count() > 0) {
      throw new Error('The portfolio table is still rendered beneath the open campaign detail.');
    }
    const detailTiles = await page.locator('.campaign-command-center .tiles .tile').count();
    if (detailTiles < 4) throw new Error(`Campaign detail composition incomplete: tiles=${detailTiles}.`);
    // B4 (ADR-084): the spend tile shows the server's two-place display value with its code.
    const spendTileText = (await page.locator('.campaign-command-center .tile', {
      has: page.locator('.ask', { hasText: 'Open the focus view for ad spend' }),
    }).locator('p.v').innerText()).trim();
    if (!/^-?[\d,]+\.\d{2} [A-Z]{3}$/.test(spendTileText)) {
      throw new Error(`Campaign detail spend tile is not a two-place amount: ${JSON.stringify(spendTileText)}.`);
    }
    // B2 regression guard: with the assistant docked, every insight card tag stays inside
    // its card. The assistant is docked at every width; open it with the top-bar toggle if closed.
    await page.setViewportSize({ width: 1440, height: 900 });
    const assistantPanel = page.getByRole('complementary', { name: 'Assistant' });
    if (await assistantPanel.count() === 0) {
      await page.getByRole('button', { name: 'Assistant', exact: true }).click();
    }
    await assistantPanel.waitFor({ timeout: 10_000 });
    await page.locator('.campaign-command-center .narr-card .tag').first().waitFor({ timeout: 30_000 });
    const overflowingTags = await page.evaluate(() => {
      const tolerance = 0.5;
      const offending = [];
      for (const tag of document.querySelectorAll('.narr-card .tag')) {
        const card = tag.closest('.narr-card');
        if (card === null) continue;
        const tagBox = tag.getBoundingClientRect();
        const cardBox = card.getBoundingClientRect();
        const contained = tag.scrollWidth <= tag.clientWidth
          && tagBox.left >= cardBox.left - tolerance
          && tagBox.right <= cardBox.right + tolerance
          && tagBox.top >= cardBox.top - tolerance
          && tagBox.bottom <= cardBox.bottom + tolerance;
        if (!contained) offending.push((tag.textContent ?? '').trim());
      }
      return offending;
    });
    if (overflowingTags.length > 0) {
      throw new Error(`Insight card tags overflow their cards: ${overflowingTags.join(', ')}.`);
    }
    await page.setViewportSize({ width: 1500, height: 1000 });
    campaignDetail = true;
    // Owner mandate 2026-09-23: the in-detail campaign switcher walks the opened-from
    // ordering with a position readout naming it, and swaps render in place.
    const switcherNav = page.getByRole('navigation', { name: /Switch campaign within the roster ordered by/ });
    await switcherNav.waitFor({ timeout: 10_000 });
    await page.locator('.band-title .eyebrow', { hasText: /· \d+ of [\d,]+ by / }).waitFor({ timeout: 10_000 });
    await page.getByLabel('Jump to another campaign by name').waitFor({ timeout: 10_000 });
    // The open campaign is the shell's h1.
    const detailHeading = page.locator('#page-heading');
    const forward = switcherNav.getByRole('button', { name: /Next campaign by/ });
    const backward = switcherNav.getByRole('button', { name: /Previous campaign by/ });
    if (await forward.isEnabled()) {
      await forward.click();
      await detailHeading.filter({ hasNotText: openedName }).waitFor({ timeout: 60_000 });
      await backward.click();
      await detailHeading.filter({ hasText: openedName }).waitFor({ timeout: 60_000 });
    } else {
      await backward.click();
      await detailHeading.filter({ hasNotText: openedName }).waitFor({ timeout: 60_000 });
      await forward.click();
      await detailHeading.filter({ hasText: openedName }).waitFor({ timeout: 60_000 });
    }
    campaignSwitcher = 'ok';
    // The detail's own insight cards open the insight dialog.
    await insightDialogChecks(page, 'Campaign detail');
    insightFocusSurfaces.push('campaign-detail');
    // ADR-072, ADR-119: when delivery is insufficient, the card is an opener into the
    // "Coverage for this window" sheet, which renders only the served block; codes never render.
    const statusOpener = page.locator('.campaign-command-center .see-why', {
      hasText: 'See why delivery evidence is insufficient',
    });
    if (await statusOpener.count() === 0) {
      throw new Error('The governed coverage-gap campaign did not render its insufficiency opener.');
    }
    await statusOpener.first().click();
    const sheet = page.getByRole('dialog', { name: 'Coverage for this window' });
    await sheet.waitFor({ timeout: 30_000 });
    await sheet.getByRole('region', { name: 'What each figure needs' }).waitFor({ timeout: 10_000 });
    // Record the missing dates the sheet names, so the dev run shows real dates where they exist.
    const named = (await sheet.locator('td[data-label="Missing dates"]').allInnerTexts())
      .map((text) => text.trim())
      .filter((text) => text !== 'None');
    coverageMissingDates = named.length === 0 ? 'none-named' : [...new Set(named)].join('|');
    const sheetText = await sheet.innerText();
    if (/incomplete_daily_series|missing_current_publication|missing_comparator_publication|empty_covered|series_total_divergence|incompatible_scope/.test(sheetText)) {
      throw new Error('The evidence status sheet rendered a raw condition code.');
    }
    await expectNoAxeViolations(page, 'Evidence status sheet');
    await page.keyboard.press('Escape');
    await sheet.waitFor({ state: 'detached', timeout: 10_000 });
    evidenceStatusSheet = 'ok';
    await page.locator('.campaign-command-center .tile:not(.empty) .ask').first().click();
    await page.getByRole('dialog').waitFor({ timeout: 30_000 });
    await expectNoAxeViolations(page, 'Metric focus');
    await page.keyboard.press('Escape');
    metricFocus = true;
    await expectNoAxeViolations(page, 'Campaign detail');
    // The crumb's tab item closes the open campaign and returns to the portfolio without a
    // route change.
    await page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', { name: 'Campaigns', exact: true }).click();
    await readyPortfolio.waitFor({ timeout: 30_000 });
  }

  // 5 · prompt administration for the operator entitlement.
  await page.goto(`${BASE_URL}/admin/prompts/`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Prompt administration', level: 1 }).waitFor();
  const adminRoster = page.getByRole('region', { name: 'Insight prompts' });
  const adminClosed = page.locator('.callout', { hasText: /Not authorized|Prompt administration is unavailable|No declared prompt slots/ });
  await Promise.race([
    adminRoster.waitFor({ timeout: 60_000 }),
    adminClosed.first().waitFor({ timeout: 60_000 }),
  ]);
  const adminReady = await adminRoster.count() > 0;
  if (adminReady) {
    await adminRoster.locator('tbody tr').first().waitFor();
    await page.getByRole('region', { name: 'Versions, active first' }).locator('tbody tr').first().waitFor({ timeout: 60_000 });
  }
  await expectNoAxeViolations(page, 'Prompt administration');

  // 6 · Retail Health must render the mandated composition with real baked data
  //     for the default and both alternate presets (mockup design-revision 2026-09-15.2).
  await page.goto(`${BASE_URL}/retail-health/`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Retail health', level: 1 }).waitFor();
  await page.getByRole('radiogroup', { name: 'Available reporting periods' }).waitFor();
  const retailProducts = page.getByRole('heading', { name: 'Products', level: 2 });
  await retailProducts.waitFor({ timeout: 60_000 });
  const retailTiles = await page.locator('.retail-health .tiles .tile').count();
  const retailInsights = await page.locator('.retail-health .narr-card').count();
  // Placement (owner feedback 2026-09-23): no insight card shares a tile grid row;
  // section insights render as full-width rows below their tiles.
  const retailGridInsights = await page.locator('.retail-health .tiles .narr-card').count();
  const retailSectionInsights = await page.locator('.retail-health .narr-card.section-insight').count();
  if (retailGridInsights !== 0 || retailSectionInsights !== 3) {
    throw new Error(`Retail insight placement is wrong: in_grid=${retailGridInsights}, section_rows=${retailSectionInsights}.`);
  }
  const retailTrendPanel = await page.locator('.retail-health .panel svg.chart').count();
  const retailSparks = await page.locator('.retail-health table.camp-table svg.spark').count();
  const retailDeltas = await page.locator('.retail-health .tiles .chg').count();
  const retailChips = await changeChipChecks(page, 'Retail health', '.retail-health');
  if (retailTiles < 7 || retailInsights !== 4 || retailTrendPanel < 1 || retailSparks < 1 || retailDeltas < 7) {
    throw new Error(
      `Retail composition incomplete: tiles=${retailTiles}, insight_cards=${retailInsights}, ` +
      `trend_charts=${retailTrendPanel}, table_sparks=${retailSparks}, delta_chips=${retailDeltas}.`,
    );
  }
  await page.getByText(/reconcile to the vendor.s reported totals|differ from the vendor.s reported totals/).waitFor();
  await page.getByText('Retail actuals are not campaign outcomes.').waitFor();
  // The trend metric switcher is a client column selection with no re-request.
  await page.getByRole('group', { name: 'Retail trend metric' }).getByRole('button', { name: 'Returns' }).click();
  await page.getByRole('heading', { name: 'Customer returns', level: 2 }).waitFor();
  const retailFourteenDays = page.getByRole('radio', { name: '14D' });
  await retailFourteenDays.click();
  await page.getByRole('radio', { name: '14D', checked: true }).waitFor({ timeout: 60_000 });
  await retailProducts.waitFor({ timeout: 60_000 });
  await expectNoAxeViolations(page, 'Retail Health 14D preset');
  // The docked assistant must never cover the preset controls: the 1D card itself
  // receives a real pointer click (this exact click was intercepted before the fix).
  const retailOneDay = page.getByRole('radio', { name: '1D' });
  await retailOneDay.click();
  await page.getByRole('radio', { name: '1D', checked: true }).waitFor({ timeout: 60_000 });
  await retailProducts.waitFor({ timeout: 60_000 });
  await expectNoAxeViolations(page, 'Retail Health 1D preset');

  // 7 · The hardened metric-focus dialog (shared contract §7): every tile opens the
  //     dialog, the switcher hops without closing, Escape returns focus to the opening
  //     tile, and one designed-absence state renders per surface.
  let portfolioFocus = false;
  let retailFocus = false;
  const focusDialogChecks = async (surface, openerName) => {
    const opener = page.locator('.tile:not(.empty) .ask', { hasText: openerName }).first();
    await opener.waitFor({ timeout: 30_000 });
    await opener.click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor({ timeout: 30_000 });
    // Block 1: the identity head serves the dictionary definition line.
    await dialog.locator('.defline').waitFor({ timeout: 60_000 });
    // Block 3: the server-computed facts strip.
    await dialog.locator('.facts .fact').first().waitFor({ timeout: 30_000 });
    // Block 4: the drivers block (entries or its designed absence).
    await dialog.locator('.drivers').waitFor({ timeout: 30_000 });
    await expectNoAxeViolations(page, `${surface} metric focus`);
    // The switcher hops to the neighboring metric without closing: the heading changes,
    // the dialog stays open, and the next metric's definition line arrives.
    const heading = dialog.locator('h2').first();
    const before = await heading.innerText();
    await dialog.getByRole('button', { name: 'Next metric' }).click();
    await heading.filter({ hasNotText: before }).waitFor({ timeout: 60_000 });
    await dialog.locator('.defline').waitFor({ timeout: 60_000 });
    await dialog.locator('.facts .fact').first().waitFor({ timeout: 60_000 });
    if (await dialog.count() !== 1) throw new Error(`${surface} focus switcher closed the dialog.`);
    await dialog.getByRole('button', { name: 'Previous metric' }).click();
    await heading.filter({ hasText: before }).waitFor({ timeout: 60_000 });
    await dialog.locator('.defline').waitFor({ timeout: 60_000 });
    // Escape closes and returns focus to the opening tile.
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'detached', timeout: 10_000 });
    // Focus is restored on the next animation frame after the dialog unmounts.
    await page.waitForFunction(
      (text) => document.activeElement?.textContent?.includes(text) ?? false,
      await opener.innerText(),
      { timeout: 5_000 },
    ).catch(() => { throw new Error(`${surface} focus dialog did not return focus to its opening tile.`); });
  };

  await page.goto(`${BASE_URL}/campaigns/`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Campaigns', level: 1 }).waitFor();
  if (await readyPortfolio.count() > 0 || campaignReady) {
    await readyPortfolio.waitFor({ timeout: 60_000 });
    await focusDialogChecks('Portfolio', /Open the focus view for/i);
    // Designed absence: impressions carry no per-campaign rows, so the drivers block
    // states why in plain words rather than rendering an empty panel.
    await page.locator('.tile:not(.empty) .ask', { hasText: 'Open the focus view for impressions' }).click();
    const impressionsDialog = page.getByRole('dialog');
    await impressionsDialog.waitFor({ timeout: 30_000 });
    await impressionsDialog.locator('.drivers .quiet').waitFor({ timeout: 60_000 });
    await page.keyboard.press('Escape');
    await impressionsDialog.waitFor({ state: 'detached', timeout: 10_000 });
    portfolioFocus = true;
    // The portfolio insight cards open the insight dialog.
    await insightDialogChecks(page, 'Portfolio');
    insightFocusSurfaces.push('campaign-portfolio');
  }

  await page.goto(`${BASE_URL}/retail-health/`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Retail health', level: 1 }).waitFor();
  await retailProducts.waitFor({ timeout: 60_000 });
  await focusDialogChecks('Retail', /Open the focus view for customer returns/i);
  // Designed absence: fulfilment rate's ordered-units operand is not carried on the
  // artifact, so its decomposition states why in plain words.
  await page.locator('.tile:not(.empty) .ask', { hasText: 'Open the focus view for fulfilment rate' }).click();
  const fulfilmentDialog = page.getByRole('dialog');
  await fulfilmentDialog.waitFor({ timeout: 30_000 });
  await fulfilmentDialog.locator('.drivers .quiet').waitFor({ timeout: 60_000 });
  // Block 7: the retail basis boundary renders inside the dialog.
  await fulfilmentDialog.locator('.focus-boundary').waitFor({ timeout: 10_000 });
  await page.keyboard.press('Escape');
  await fulfilmentDialog.waitFor({ state: 'detached', timeout: 10_000 });
  retailFocus = true;
  // The retail insight cards (section rows and the table-lead movers card) open the dialog.
  await insightDialogChecks(page, 'Retail');
  insightFocusSurfaces.push('retail-health');

  // The rebuilt retail page renders the narrative sections and derived columns.
  const retailSections = await page.locator('.retail-health .metric-section').count();
  if (retailSections < 4) throw new Error(`Retail narrative sections incomplete: sections=${retailSections}.`);
  await page.locator('table.camp-table th', { hasText: 'Return rate' }).waitFor();
  await page.locator('table.camp-table th', { hasText: 'Amazon margin' }).waitFor();

  // 8 · The not-found page (ADR-123 decision 2): an unknown path shows it signed in. Signed out,
  //     the served page is the 404 document and the visitor is then sent to sign-in, as
  //     frontend-auth-session requires. The status is recorded (404, or the recorded 200 fallback
  //     of decision 3), and every allowlisted route still resolves. Separate pages, so the 404
  //     isn't a failed request above.
  const unknownPaths = ['/probe-not-found/', '/probe-not-found/nested'];
  const notFoundStatuses = [];
  const signedInNotFound = await context.newPage();
  // Their 404 responses log a console error by design, so these pages watch runtime errors only.
  signedInNotFound.on('pageerror', (error) => browserErrors.push(`not-found signed in: ${error.name}`));
  for (const unknownPath of unknownPaths) {
    const response = await signedInNotFound.goto(`${BASE_URL}${unknownPath}`, { waitUntil: 'domcontentloaded' });
    notFoundStatuses.push(response?.status() ?? 0);
  }
  await signedInNotFound.getByRole('heading', { name: 'Page not found', level: 1 }).waitFor();
  await signedInNotFound.getByRole('complementary', { name: 'Assistant' }).waitFor({ timeout: 30_000 });
  await signedInNotFound.getByRole('button', { name: 'Sign out' }).waitFor();
  await expectNoAxeViolations(signedInNotFound, 'Not-found page signed in');
  // A dotted path passes the edge to its object, so an exported index.html is its route's page.
  const dottedRoute = await signedInNotFound.goto(`${BASE_URL}/campaigns/index.html`, { waitUntil: 'domcontentloaded' });
  if (dottedRoute?.status() !== 200) throw new Error(`/campaigns/index.html returned ${dottedRoute?.status() ?? 0}.`);
  await signedInNotFound.getByRole('heading', { name: 'Campaigns', level: 1 }).waitFor({ timeout: 30_000 });
  const signedOutContext = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  const signedOutNotFound = await signedOutContext.newPage();
  signedOutNotFound.on('pageerror', (error) => browserErrors.push(`not-found signed out: ${error.name}`));
  // The login request is awaited from before the navigation: the redirect can follow the
  // session check before the shell is ever inspected, so the served document is read instead.
  const signInRequested = signedOutNotFound.waitForRequest(
    (request) => new URL(request.url()).pathname === '/api/auth/login',
    { timeout: 30_000 },
  );
  // The document is fetched without running it as well, because the page may be gone before
  // its body can be read from the navigation.
  const signedOutFetch = await signedOutContext.request.get(`${BASE_URL}${unknownPaths[0]}`);
  notFoundStatuses.push(signedOutFetch.status());
  const signedOutDocument = await signedOutFetch.text();
  const signedOutResponse = await signedOutNotFound.goto(`${BASE_URL}${unknownPaths[0]}`, { waitUntil: 'commit' });
  notFoundStatuses.push(signedOutResponse?.status() ?? 0);
  if (!signedOutDocument.includes('Page not found') || !signedOutDocument.includes('data-assistant="absent"')) {
    throw new Error('The signed-out unknown path was not served the not-found page without the assistant.');
  }
  await signInRequested.catch(() => {
    throw new Error('The signed-out not-found page did not send the visitor to sign-in.');
  });
  await signedOutContext.close();
  if (!notFoundStatuses.every((status) => status === 404) && !notFoundStatuses.every((status) => status === 200)) {
    throw new Error(`Not-found statuses are inconsistent: ${notFoundStatuses.join(',')}.`);
  }
  const allowlist = JSON.parse(readFileSync(new URL('../contracts/generated/route-allowlist.json', import.meta.url), 'utf8'));
  for (const route of allowlist.routes) {
    const status = (await context.request.get(`${BASE_URL}${route}`)).status();
    if (status !== 200) throw new Error(`Allowlisted route ${route} returned ${status}.`);
  }
  await signedInNotFound.close();
  const notFound = `not_found_status=${notFoundStatuses[0]}, allowlisted_routes=${allowlist.routes.length}`;

  // 9 · Every other allowlisted analytical page (D2 seam sweep finding 11): the list is the generated
  //     route allowlist minus the routes the blocks above walk in depth, so a page added to the
  //     contracts is covered without editing this probe. Each renders data or its page-level
  //     absence (ADR-139): page API 200, no PageIssue, one persistent status region carrying the
  //     absence message, axe-clean in light and dark.
  //     `rosterRoutes` refuses an empty or stale list; its unit test pins the allowlist's page
  //     routes to the bake roster's pages.
  const rosterOutcomes = { page: 0, window_unavailable: 0 };
  for (const route of rosterRoutes(allowlist.routes)) {
    const api = `/api/presentation${route.replace(/\/$/, '')}`;
    const routePage = await context.newPage();
    routePage.on('pageerror', (error) => browserErrors.push(`${route}: ${error.name}`));
    const served = routePage.waitForResponse((response) => new URL(response.url()).pathname === api, { timeout: 90_000 });
    await routePage.goto(`${BASE_URL}${route}`, { waitUntil: 'domcontentloaded' });
    const response = await served.catch(() => { throw new Error(`${route} did not request ${api}.`); });
    if (response.status() !== 200) throw new Error(`${route} page API returned ${response.status()}.`);
    const body = await response.json();
    if (body?.kind !== 'page' && body?.kind !== 'window_unavailable') throw new Error(`${route} page API kind is ${String(body?.kind)}.`);
    await routePage.locator('main h1').first().waitFor({ timeout: 60_000 });
    await routePage.waitForFunction(() => document.querySelectorAll('main [aria-busy="true"], main .skel').length === 0, null, { timeout: 120_000 });
    if (await routePage.locator('main [role="alert"]').count() !== 0) throw new Error(`${route} rendered a page issue.`);
    const status = routePage.locator('main p.sr-only[role="status"]');
    if (await status.count() !== 1) throw new Error(`${route} does not carry exactly one page status region.`);
    if (body.kind === 'window_unavailable') {
      const message = String(body.message ?? '');
      const callout = routePage.locator('main section.callout p', { hasText: message });
      if (!message || await callout.count() !== 1) throw new Error(`${route} did not render its served absence message.`);
      if (!((await status.textContent()) ?? '').endsWith(message)) throw new Error(`${route} status region does not announce its absence.`);
    }
    rosterOutcomes[body.kind] += 1;
    for (const theme of ['light', 'dark']) {
      await routePage.locator('html').evaluate((element, next) => { element.dataset.theme = next; }, theme);
      await expectNoAxeViolations(routePage, `${route} (${theme})`);
    }
    await routePage.close();
  }
  const rosterPages = `roster_pages=data:${rosterOutcomes.page}+absence:${rosterOutcomes.window_unavailable}`;
  const unexpectedRequests = failedRequests.filter((request) => request !== '/api/me:403');
  if (browserErrors.length > 0 || unexpectedRequests.length > 0) {
    throw new Error(
      `Browser failures: console=${browserErrors.join('|') || 'none'}, ` +
      `requests=${unexpectedRequests.join('|') || 'none'}, ` +
      `chat=${JSON.stringify(chatFailures)}`,
    );
  }

  if (deepSectionsObserved === 0) {
    throw new Error('No insight dialog rendered its ADR-076 deep section; the extended-dialog acceptance is unmet.');
  }

  console.log(
    `live browser accepted: campaign_rows=${campaignRows}, campaign_state=${campaignReady ? 'ready' : 'unavailable'}, ` +
    `chat=active, reload_selector=preserved, assistant_messages=${assistantMessages}, ` +
    `accessibility_violations=0, ${shellKeyboard}, ` +
    `campaign_detail=${campaignDetail ? 'ok' : 'not-observed'}, card_tags=${campaignDetail ? 'contained' : 'not-observed'}, metric_focus=${metricFocus ? 'ok' : 'not-observed'}, ` +
    `evidence_status_sheet=${evidenceStatusSheet}, coverage_missing_dates=${coverageMissingDates}, estate_reach=${estateReach}, ` +
    `portfolio_focus=${portfolioFocus ? 'ok' : 'not-observed'}, retail_focus=${retailFocus ? 'ok' : 'not-observed'}, ` +
    `display_paging=${displayPaging}, campaign_switcher=${campaignSwitcher}, ` +
    `insight_focus=${insightFocusSurfaces.join('+') || 'not-observed'}, ` +
    `deep_sections=${deepSectionsObserved}, ` +
    `campaign_chips=${campaignChips}, retail_chips=${retailChips}, ` +
    `prompt_admin=${adminReady ? 'ready' : 'closed'}, retail_health=ready, alternate_presets=ok, ${notFound}, ${rosterPages}`,
  );
} finally {
  await browser.close();
}
