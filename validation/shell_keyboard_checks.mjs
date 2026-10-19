// SPDX-License-Identifier: MIT-0
/**
 * The shell's keyboard path (ADR-118 decisions 2 and 3): skip links, the rail tooltip, the
 * slide-out menu sheet and the docked assistant toggle. probe_frontend_browser.mjs runs it on the
 * deployed page; the local conformance harness runs the same function against fictional fixtures.
 * It throws on the first failed check and returns a sanitized summary. It leaves the viewport at
 * 768 px; the caller restores its own.
 */

const WIDE = { width: 1440, height: 900 };
const RAIL = { width: 1280, height: 900 };
const NARROW = { width: 768, height: 900 };

async function activeElement(page) {
  return page.evaluate(() => {
    const element = document.activeElement;
    return {
      id: element?.id ?? '',
      label: element?.getAttribute('aria-label') ?? '',
      text: (element?.textContent ?? '').trim().slice(0, 60),
    };
  });
}

/** Put the sequential focus starting point at the top of the document, as on a fresh page. */
async function focusDocumentStart(page) {
  await page.evaluate(() => {
    const body = document.body;
    body.setAttribute('tabindex', '-1');
    body.focus();
    body.removeAttribute('tabindex');
  });
}

async function skipLinkChecks(page) {
  await page.setViewportSize(WIDE);
  await page.getByRole('complementary', { name: 'Assistant' }).waitFor({ timeout: 10_000 });
  await focusDocumentStart(page);
  await page.keyboard.press('Tab');
  let focused = await activeElement(page);
  if (focused.text !== 'Skip to main content') {
    throw new Error(`The first Tab stop is not "Skip to main content" (found "${focused.text}").`);
  }
  const box = await page.getByRole('link', { name: 'Skip to main content' }).boundingBox();
  if (box === null || box.x < 0 || box.width === 0) throw new Error('"Skip to main content" is not visible on focus.');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.activeElement?.id === 'main-content', null, { timeout: 5_000 })
    .catch(() => { throw new Error('"Skip to main content" did not move focus to main.'); });
  await focusDocumentStart(page);
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  focused = await activeElement(page);
  if (focused.text !== 'Skip to assistant') {
    throw new Error(`The second Tab stop is not "Skip to assistant" (found "${focused.text}").`);
  }
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.activeElement?.id === 'chat-question', null, { timeout: 5_000 })
    .catch(() => { throw new Error('"Skip to assistant" did not move focus to the assistant composer.'); });
}

/** WCAG 1.4.13: the rail tooltip shows on keyboard focus and Escape dismisses it. */
async function railTooltipChecks(page) {
  await page.setViewportSize(RAIL);
  const nav = page.getByRole('navigation', { name: 'Hubs' });
  await page.waitForFunction(
    () => Math.round(document.querySelector('nav.hubnav')?.getBoundingClientRect().width ?? 0) === 88,
    null,
    { timeout: 10_000 },
  ).catch(() => { throw new Error('The navigation is not the 88 px rail at 1280 px.'); });
  const links = nav.locator('a.hub');
  const count = await links.count();
  if (count === 0) throw new Error('The rail shows no hubs.');
  const link = links.first();
  const label = (await link.getAttribute('aria-label')) ?? '';
  // Keyboard modality, so programmatic focus matches :focus-visible as a Tab would.
  await focusDocumentStart(page);
  await page.keyboard.press('Tab');
  await link.focus();
  const tip = link.locator('.tip');
  const shown = await tip.evaluate((node) => getComputedStyle(node).display);
  if (shown === 'none') throw new Error('The rail tooltip did not show on keyboard focus.');
  if (((await tip.textContent()) ?? '').trim() !== label) throw new Error('The rail tooltip does not carry the full hub label.');
  await page.keyboard.press('Escape');
  if (await tip.evaluate((node) => getComputedStyle(node).display) !== 'none') {
    throw new Error('Escape did not dismiss the rail tooltip.');
  }
  return count;
}

/** Below 881 px: the menu sheet opens from the Menu button, traps focus and returns it on Escape. */
async function menuSheetChecks(page, expectNoAxeViolations) {
  await page.setViewportSize(NARROW);
  const menu = page.getByRole('button', { name: 'Menu', exact: true });
  await menu.waitFor({ timeout: 10_000 });
  await menu.focus();
  await page.keyboard.press('Enter');
  const sheet = page.getByRole('dialog', { name: 'Menu' });
  await sheet.waitFor({ timeout: 10_000 });
  if ((await activeElement(page)).label !== 'Close menu') throw new Error('The menu sheet did not move focus to Close menu.');
  if (await sheet.getByRole('navigation', { name: 'Hubs' }).getByRole('link').count() === 0) {
    throw new Error('The menu sheet lists no hubs.');
  }
  await expectNoAxeViolations(page, 'Menu sheet');
  await page.keyboard.press('Escape');
  await sheet.waitFor({ state: 'detached', timeout: 10_000 });
  if ((await activeElement(page)).label !== 'Menu') throw new Error('Closing the menu sheet did not return focus to Menu.');
}

/** The assistant is docked at every width: the top-bar toggle closes and reopens it, no dialog. */
async function dockedAssistantChecks(page) {
  await page.setViewportSize(NARROW);
  const panel = page.getByRole('complementary', { name: 'Assistant' });
  await panel.waitFor({ timeout: 10_000 });
  if (await page.getByRole('dialog').count() > 0) throw new Error('The assistant opened as a dialog below 881 px.');
  const toggle = page.getByRole('button', { name: 'Assistant', exact: true });
  if (await toggle.getAttribute('aria-expanded') !== 'true') throw new Error('The assistant toggle does not report the open panel.');
  // The shell learns the panel's state after it renders, so the attribute is polled.
  const expanded = (value) => page.waitForFunction(
    (expected) => document.getElementById('assistant-toggle')?.getAttribute('aria-expanded') === expected,
    value,
    { timeout: 5_000 },
  );
  await toggle.focus();
  await page.keyboard.press('Enter');
  await panel.waitFor({ state: 'detached', timeout: 10_000 });
  await expanded('false').catch(() => { throw new Error('The assistant toggle does not report the closed panel.'); });
  await toggle.focus();
  await page.keyboard.press('Enter');
  await panel.waitFor({ timeout: 10_000 });
  await expanded('true').catch(() => { throw new Error('The assistant toggle does not report the reopened panel.'); });
}

export async function shellKeyboardChecks(page, { expectNoAxeViolations }) {
  await skipLinkChecks(page);
  const railHubs = await railTooltipChecks(page);
  await menuSheetChecks(page, expectNoAxeViolations);
  await dockedAssistantChecks(page);
  return `skip_links=ok, rail_tooltip=ok(${railHubs} hubs), menu_sheet=ok, docked_assistant=ok`;
}
