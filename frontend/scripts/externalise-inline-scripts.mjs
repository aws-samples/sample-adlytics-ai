// SPDX-License-Identifier: MIT-0
/**
 * Move Next.js's inline bootstrap scripts into files, so `script-src 'self'` holds.
 *
 * The App Router embeds its Flight payload as inline `<script>self.__next_f.push(...)`
 * blocks. React needs them to hydrate, and a CSP without 'unsafe-inline', a hash or a
 * nonce blocks them — the page then throws React error #412 and renders nothing
 * interactive.
 *
 * Next 16.4 with Turbopack also emits one bootstrap script with attributes,
 * `<script id="_R_">(globalThis["TURBOPACK"] ...).push({"otherChunks": ...})`, which names
 * the runtime chunks to load. Blocked, the app never starts and every page stays on
 * "Checking session…". Any inline script that carries attributes is therefore moved to
 * its own file in place, keeping its attributes, so it still runs at the same point.
 *
 * A nonce is not available: nonces require per-request generation, and a static export
 * has no request. Hashes are available but couple the CSP to the build — the payload
 * embeds chunk filenames, so every frontend change would need the CloudFront response
 * headers policy redeployed in lockstep or the site breaks. Externalising the scripts
 * leaves the CSP a constant.
 *
 * Ordering is preserved: the Flight blocks are concatenated in document order and
 * emitted as one classic script at the position of the first, so the pushes still run in
 * sequence and at the same point in the document. Every other inline script becomes a
 * classic `src` script at its own position, so it runs where it did.
 *
 * Script elements are found by an HTML parser (jsdom's, spec-conformant), not a pattern, so
 * tag and attribute names match case-insensitively, `src` and `type` are read as attributes
 * rather than substrings, and text that only looks like a tag (`<scr<script>ipt>`) is not
 * taken for one. The page is rebuilt in one pass from the parser's source offsets, so every
 * byte outside a moved script, and every moved body, is copied unchanged.
 *
 * The closing check parses each rewritten page again and reads every script element, not
 * just the shapes moved here: any inline script the CSP would block fails the build,
 * including one the rewrite itself formed by joining the text around a removed block.
 */

import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM, VirtualConsole } from 'jsdom';

const OUT_DIR = 'out';
const CHUNK_DIR = join(OUT_DIR, '_next', 'static', 'chunks');

/**
 * Script types the browser runs, so `script-src` applies to them (HTML "prepare the script
 * element"): no type, a JavaScript MIME type, `module`, and the inline-only `importmap` and
 * `speculationrules`. Anything else is a data block the CSP doesn't govern.
 */
const JAVASCRIPT_MIME = /^(?:(?:text|application)\/(?:x-)?(?:javascript|ecmascript)|text\/(?:jscript|livescript)|text\/javascript1\.[0-5])$/;
const EXECUTED_NON_MIME = new Set(['module', 'importmap', 'speculationrules']);

/**
 * Every script element the parser builds from `html`, in source order, with its exact source
 * text. Template contents are included: inert until script copies them into the document,
 * where they then run under the same CSP.
 */
function scriptElements(html) {
  const dom = new JSDOM(html, { includeNodeLocations: true, virtualConsole: new VirtualConsole() });
  try {
    const { HTMLTemplateElement } = dom.window;
    const roots = [dom.window.document];
    const scripts = [];
    for (let index = 0; index < roots.length; index += 1) {
      for (const element of roots[index].querySelectorAll('script, template')) {
        if (element instanceof HTMLTemplateElement) {
          roots.push(element.content);
          continue;
        }
        const location = dom.nodeLocation(element);
        if (!location?.startTag) throw new Error('a script element has no source location');
        const open = location.startTag;
        // A script still open at end of file is never run (HTML "in text" insertion mode, EOF).
        const body = location.endTag === undefined
          ? ''
          : html.slice(open.endOffset, location.endTag.startOffset);
        scripts.push({
          start: location.startOffset,
          end: location.endOffset,
          startTag: html.slice(open.startOffset, open.endOffset),
          body,
          attributes: [...element.attributes].map(({ name, value }) => ({ name, value })),
          hasSrc: element.hasAttribute('src'),
          type: (element.getAttribute('type') ?? '').trim().toLowerCase(),
        });
      }
    }
    return scripts.sort((a, b) => a.start - b.start);
  } finally {
    dom.window.close();
  }
}

/** Whether `script-src 'self'` blocks this element: it runs, has no `src` and has a body. */
function blockedByCsp({ hasSrc, body, type }) {
  if (hasSrc || body.length === 0) return false;
  return type === '' || JAVASCRIPT_MIME.test(type) || EXECUTED_NON_MIME.has(type);
}

/** Next Flight bootstrap blocks, with or without Next 16's async serialization. */
function isFlight({ attributes }) {
  return attributes.length === 0
    || (attributes.length === 1 && attributes[0].name === 'async' && attributes[0].value === '');
}

function chunkFile(code) {
  const hash = createHash('sha256').update(code).digest('hex').slice(0, 16);
  return { name: `inline.${hash}.js`, code };
}

// Root-relative so it resolves the same from any page depth.
const chunkSrc = (name) => `/_next/static/chunks/${name}`;

/**
 * Externalise one page's inline scripts. Returns the rewritten HTML and the chunk files
 * it now references; `files` is empty when the page had nothing to move.
 */
export function externaliseHtml(original) {
  const flight = [];
  const files = [];
  // Untouched source runs alternate with replacements, so the page is rebuilt in one pass.
  const parts = [];
  let cursor = 0;
  let flightPart = -1;

  for (const script of scriptElements(original)) {
    if (!blockedByCsp(script)) continue;
    if (script.start < cursor) throw new Error('script elements overlap in the source');
    let replacement;
    if (isFlight(script)) {
      // The first block's slot receives the combined script once every block is read.
      if (flight.length === 0) flightPart = parts.length + 1;
      flight.push(script.body.endsWith(';') ? script.body : `${script.body};`);
      replacement = '';
    } else if (script.type === 'importmap' || script.type === 'speculationrules') {
      // Neither has an external form; leave it for the closing check to refuse.
      continue;
    } else {
      const file = chunkFile(script.body);
      files.push(file);
      // The start tag minus its `>`, so every attribute is kept as written.
      replacement = `${script.startTag.slice(0, -1)} src="${chunkSrc(file.name)}"></script>`;
    }
    parts.push(original.slice(cursor, script.start), replacement);
    cursor = script.end;
  }
  parts.push(original.slice(cursor));

  if (flight.length > 0) {
    const file = chunkFile(flight.join('\n'));
    files.unshift(file);
    parts[flightPart] = `<script src="${chunkSrc(file.name)}"></script>`;
  }
  return { html: parts.join(''), files, flightBlocks: flight.length };
}

/** A short description of each inline script in `html` that `script-src 'self'` blocks. */
export function blockedInlineScripts(html) {
  return scriptElements(html)
    .filter(blockedByCsp)
    .map((script) => `${script.startTag}${script.body.slice(0, 60)}`);
}

/** Recursive walk rather than a glob: fs.globSync needs Node 22 and this runs on 20. */
function findHtml(dir) {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...findHtml(path));
    } else if (entry.name.endsWith('.html')) {
      found.push(path);
    }
  }
  return found;
}

function main() {
  const htmlFiles = findHtml(OUT_DIR);
  if (htmlFiles.length === 0) {
    console.error(`no HTML found under ${OUT_DIR}/ — did next build run?`);
    process.exit(1);
  }

  let touched = 0;
  for (const file of htmlFiles) {
    const result = externaliseHtml(readFileSync(file, 'utf8'));
    if (result.files.length === 0) continue;
    touched += 1;
    mkdirSync(CHUNK_DIR, { recursive: true });
    for (const chunk of result.files) writeFileSync(join(CHUNK_DIR, chunk.name), chunk.code);
    writeFileSync(file, result.html);
    const bytes = result.files.reduce((sum, chunk) => sum + chunk.code.length, 0);
    console.log(
      `  ${relative(OUT_DIR, file)}: ${result.flightBlocks} inline block(s) and `
        + `${result.files.length - (result.flightBlocks > 0 ? 1 : 0)} attributed script(s) -> `
        + `${result.files.map((chunk) => chunk.name).join(', ')} (${bytes} B)`,
    );
  }

  // A build that suddenly emits none means Next changed how it bootstraps, and the CSP
  // question needs revisiting rather than silently passing.
  if (touched === 0) {
    console.error('no inline scripts found in any page; verify the CSP is still correct');
    process.exit(1);
  }

  // Leaving one behind is the failure this script exists to prevent, and it is silent
  // in every check except a browser console.
  for (const file of htmlFiles) {
    const remaining = blockedInlineScripts(readFileSync(file, 'utf8'));
    if (remaining.length > 0) {
      console.error(`${file} still has ${remaining.length} inline script(s) the CSP blocks:`);
      for (const script of remaining) console.error(`  ${script}`);
      process.exit(1);
    }
  }

  console.log(`externalised inline scripts in ${touched} of ${htmlFiles.length} page(s)`);
}

const invokedDirectly = process.argv[1] !== undefined
  && fileURLToPath(import.meta.url) === process.argv[1];
if (invokedDirectly) main();
