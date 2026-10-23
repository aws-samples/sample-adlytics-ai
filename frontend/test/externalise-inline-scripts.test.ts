// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import { test } from 'vitest';
import { blockedInlineScripts, externaliseHtml } from '../scripts/externalise-inline-scripts.mjs';

// The shape Next 16.4.2 with Turbopack exports (out/scorecard/index.html, 2026-10-23): async
// chunk scripts, the attributed Turbopack bootstrap `<script id="_R_">`, then Flight blocks.
// Before this fix the `_R_` script stayed inline, `script-src 'self'` blocked it on dev, and
// every page stayed on "Checking session…".
const TURBOPACK_BOOTSTRAP =
  '(globalThis["TURBOPACK"] || (globalThis["TURBOPACK"] = [])).push({"otherChunks":["static/chunks/1bo2yxf_m5mt5.js","static/chunks/41hyml_n2xjkj.js"],"runtimeModuleIds":[94553]});';
const FLIGHT_1 = 'self.__next_f.push([1,"0:{\\"P\\":null,\\"$\\":\\"$L1\\"}"])';
const FLIGHT_2 = 'self.__next_f.push([1,"1:[\\"$\\",\\"main\\"]"])';
const PAGE = [
  '<!DOCTYPE html><html><head>',
  '<script src="/_next/static/chunks/41hyml_n2xjkj.js" async=""></script>',
  '<script src="/_next/static/chunks/0cz1d0mv5g_q7.js" noModule=""></script>',
  '<script defer="" src="/theme-preference.9fe8ccc10b308643.js"></script>',
  '</head><body><div>Checking session…</div>',
  `<script id="_R_">${TURBOPACK_BOOTSTRAP}</script>`,
  '<script src="/_next/static/chunks/1bo2yxf_m5mt5.js" async=""></script>',
  `<script>${FLIGHT_1}</script><script async="">${FLIGHT_2}</script>`,
  '</body></html>',
].join('');

test('the exported page shape has no inline script the CSP blocks once externalised', () => {
  assert.equal(blockedInlineScripts(PAGE).length, 3);
  const { html, files, flightBlocks } = externaliseHtml(PAGE);
  assert.deepEqual(blockedInlineScripts(html), []);
  assert.equal(flightBlocks, 2);
  assert.equal(files.length, 2);
});

test('the Turbopack bootstrap keeps its attributes and its position, with its exact bytes', () => {
  const { html, files } = externaliseHtml(PAGE);
  const bootstrap = files.find((file) => file.code === TURBOPACK_BOOTSTRAP);
  assert.ok(bootstrap, 'the bootstrap file holds the inline body byte for byte');
  assert.match(bootstrap.name, /^inline\.[0-9a-f]{16}\.js$/);
  const tag = `<script id="_R_" src="/_next/static/chunks/${bootstrap.name}"></script>`;
  assert.ok(html.includes(tag));
  assert.ok(html.indexOf('Checking session') < html.indexOf(tag));
  assert.ok(html.indexOf(tag) < html.indexOf('1bo2yxf_m5mt5.js" async'));
});

test('Flight blocks still become one script, in document order, at the first block', () => {
  const { html, files } = externaliseHtml(PAGE);
  const flight = files.find((file) => file.code.includes('__next_f'));
  assert.ok(flight);
  assert.equal(flight.code, `${FLIGHT_1};\n${FLIGHT_2};`);
  assert.ok(flight.code.includes('"$L1'), 'a `$` in the payload is never read as a substitution');
  assert.equal(html.split(`src="/_next/static/chunks/${flight.name}"`).length, 2);
  assert.ok(!html.includes('__adlytics_inline_script_placeholder__'));
});

test('only scripts the browser runs inline count as blocked', () => {
  assert.deepEqual(blockedInlineScripts('<script src="/a.js"></script>'), []);
  assert.deepEqual(blockedInlineScripts('<script id="x"></script>'), []);
  assert.deepEqual(blockedInlineScripts('<script type="application/json">{"a":1}</script>'), []);
  assert.deepEqual(blockedInlineScripts('<script type="application/ld+json">{"a":1}</script>'), []);
  assert.equal(blockedInlineScripts('<script type="text/javascript">a()</script>').length, 1);
  assert.equal(blockedInlineScripts('<script type="module">a()</script>').length, 1);
  assert.equal(blockedInlineScripts("<script nonce='n' data-x>a()</script>").length, 1);
});

test('an inline import map has no external form, so it is left for the build check to refuse', () => {
  const page = '<script type="importmap">{"imports":{}}</script>';
  const { html, files } = externaliseHtml(page);
  assert.equal(html, page);
  assert.equal(files.length, 0);
  assert.equal(blockedInlineScripts(html).length, 1);
});
