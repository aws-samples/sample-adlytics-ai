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

test('an upper- or mixed-case script tag, with whitespace or a slash before its attributes, is still checked and moved', () => {
  // HTML tag and attribute names are case-insensitive, so the browser runs every one of
  // these and `script-src 'self'` blocks it. Before the parser, the check saw none of them.
  const blocked = [
    '<SCRIPT>a()</SCRIPT>',
    '<Script Type="Module">a()</sCrIpT >',
    '<script\nid="n">a()</script\t>',
    '<script/id="s">a()</script>',
    '<template><SCRIPT id="t">a()</SCRIPT></template>',
  ];
  for (const page of blocked) {
    assert.equal(blockedInlineScripts(page).length, 1, page);
    const { html, files } = externaliseHtml(page);
    assert.deepEqual(blockedInlineScripts(html), [], page);
    assert.equal(files.length, 1, page);
    assert.equal(files[0]?.code.replace(/;$/, ''), 'a()', page);
  }
  const { html, files: [file] } = externaliseHtml('<SCRIPT ID="x">a()\r\nb()</SCRIPT>');
  assert.ok(file);
  assert.equal(file.code, 'a()\r\nb()', 'the body is copied byte for byte');
  assert.equal(html, `<SCRIPT ID="x" src="/_next/static/chunks/${file.name}"></script>`);
});

test('src and type are read as attributes, so a quoted " src=" or ">" in another value neither hides nor splits a script', () => {
  assert.equal(blockedInlineScripts('<script data-x=" src=y">a()</script>').length, 1);
  assert.equal(blockedInlineScripts('<script data-x=" type=application/json">a()</script>').length, 1);
  const { html, files } = externaliseHtml('<script data-x="a>b">a()</script>');
  const [file] = files;
  assert.equal(files.length, 1);
  assert.ok(file);
  assert.equal(file.code, 'a()');
  assert.equal(html, `<script data-x="a>b" src="/_next/static/chunks/${file.name}"></script>`);
});

test('an overlapping <scr<script>ipt> is not a script element, and removing a block never forms one', () => {
  // The browser reads `<scr<script>` as one start tag named "scr<script", so only `first()`
  // runs here. A pattern match took `<script>second()</script>` for a Flight block, removed
  // it, and joined `<scr` to `ipt>`: the output then held a new inline `<script>third()`.
  const tail = '<scr<script>second()</script>ipt>third()</script>';
  const page = `<script>first()</script>${tail}`;
  assert.equal(blockedInlineScripts(page).length, 1);
  const { html, files: [file], flightBlocks } = externaliseHtml(page);
  assert.equal(flightBlocks, 1);
  assert.ok(file);
  assert.equal(file.code, 'first();');
  assert.equal(html, `<script src="/_next/static/chunks/${file.name}"></script>${tail}`);
  assert.deepEqual(blockedInlineScripts(html), []);
});

test('a script formed by joining the text around a removed Flight block fails the closing check', () => {
  // `<<script>` is a literal `<` then a real script. Removing that second Flight block joins
  // the `<` to `script>c()`, so the rewritten page holds an inline script the original did
  // not. The rewrite can't see it; the closing check, run on the output, refuses it.
  const page = '<script>a()</script><<script>b()</script>script>c()</script>';
  assert.equal(blockedInlineScripts(page).length, 2);
  const { html } = externaliseHtml(page);
  assert.equal(blockedInlineScripts(html).length, 1);
});

test('an inline import map has no external form, so it is left for the build check to refuse', () => {
  const page = '<script type="importmap">{"imports":{}}</script>';
  const { html, files } = externaliseHtml(page);
  assert.equal(html, page);
  assert.equal(files.length, 0);
  assert.equal(blockedInlineScripts(html).length, 1);
});
