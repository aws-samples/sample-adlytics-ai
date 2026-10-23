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
 * The closing check reads every `<script>` element, not just the shapes moved here: any
 * inline script the CSP would block fails the build.
 */

import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT_DIR = 'out';
const CHUNK_DIR = join(OUT_DIR, '_next', 'static', 'chunks');

/** Every script element. Attributes are captured so a `src` or data-block type is seen. */
const SCRIPT_ELEMENT = /<script\b([^>]*)>([\s\S]*?)<\/script>/g;

/** Next Flight bootstrap blocks, with or without Next 16's async serialization. */
const FLIGHT_ATTRIBUTES = /^(?:\s+async="")?$/;

const HAS_SRC = /\ssrc\s*=/i;
const TYPE_ATTRIBUTE = /\stype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i;

/**
 * Script types the browser runs, so `script-src` applies to them (HTML "prepare the script
 * element"): no type, a JavaScript MIME type, `module`, and the inline-only `importmap` and
 * `speculationrules`. Anything else is a data block the CSP doesn't govern.
 */
const JAVASCRIPT_MIME = /^(?:(?:text|application)\/(?:x-)?(?:javascript|ecmascript)|text\/(?:jscript|livescript)|text\/javascript1\.[0-5])$/;
const EXECUTED_NON_MIME = new Set(['module', 'importmap', 'speculationrules']);

const PLACEHOLDER = '__adlytics_inline_script_placeholder__';

function scriptType(attributes) {
  const match = TYPE_ATTRIBUTE.exec(attributes);
  if (match === null) return '';
  return (match[1] ?? match[2] ?? match[3] ?? '').trim().toLowerCase();
}

/** Whether `script-src 'self'` blocks this element: it runs, has no `src` and has a body. */
function blockedByCsp(attributes, body) {
  if (HAS_SRC.test(attributes) || body.length === 0) return false;
  const type = scriptType(attributes);
  return type === '' || JAVASCRIPT_MIME.test(type) || EXECUTED_NON_MIME.has(type);
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

  // Replacement uses a function so a `$` in the payload is never read as a
  // substitution pattern. The Flight payload is full of them.
  const stripped = original.replace(SCRIPT_ELEMENT, (match, attributes, body) => {
    if (!blockedByCsp(attributes, body)) return match;
    if (FLIGHT_ATTRIBUTES.test(attributes)) {
      const first = flight.length === 0;
      flight.push(body.endsWith(';') ? body : `${body};`);
      return first ? PLACEHOLDER : '';
    }
    const type = scriptType(attributes);
    if (type === 'importmap' || type === 'speculationrules') {
      // Neither has an external form; leave it for the closing check to refuse.
      return match;
    }
    const file = chunkFile(body);
    files.push(file);
    return `<script${attributes} src="${chunkSrc(file.name)}"></script>`;
  });

  let html = stripped;
  if (flight.length > 0) {
    const file = chunkFile(flight.join('\n'));
    files.unshift(file);
    html = stripped.replace(PLACEHOLDER, () => `<script src="${chunkSrc(file.name)}"></script>`);
  }
  return { html, files, flightBlocks: flight.length };
}

/** A short description of each inline script in `html` that `script-src 'self'` blocks. */
export function blockedInlineScripts(html) {
  const found = [];
  for (const [, attributes, body] of html.matchAll(SCRIPT_ELEMENT)) {
    if (blockedByCsp(attributes, body)) {
      found.push(`<script${attributes}>${body.slice(0, 60)}`);
    }
  }
  return found;
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
