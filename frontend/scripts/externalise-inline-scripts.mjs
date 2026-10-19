// SPDX-License-Identifier: MIT-0
/**
 * Move Next.js's inline bootstrap scripts into files, so `script-src 'self'` holds.
 *
 * The App Router embeds its Flight payload as inline `<script>self.__next_f.push(...)`
 * blocks. React needs them to hydrate, and a CSP without 'unsafe-inline', a hash or a
 * nonce blocks them — the page then throws React error #412 and renders nothing
 * interactive.
 *
 * A nonce is not available: nonces require per-request generation, and a static export
 * has no request. Hashes are available but couple the CSP to the build — the payload
 * embeds chunk filenames, so every frontend change would need the CloudFront response
 * headers policy redeployed in lockstep or the site breaks. Externalising the scripts
 * leaves the CSP a constant.
 *
 * Ordering is preserved: the blocks are concatenated in document order and emitted as
 * one classic script at the position of the first, so the pushes still run in sequence
 * and at the same point in the document.
 */

import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, relative } from 'node:path';

const OUT_DIR = 'out';
const CHUNK_DIR = join(OUT_DIR, '_next', 'static', 'chunks');

/** Next Flight bootstrap blocks, with or without Next 16's async serialization. */
const INLINE_SCRIPT = /<script(?:\s+async="")?>([\s\S]*?)<\/script>/g;

const PLACEHOLDER = '__adlytics_inline_script_placeholder__';

function externalise(htmlPath) {
  const original = readFileSync(htmlPath, 'utf8');
  const blocks = [];

  // Replacement uses a function so a `$` in the payload is never read as a
  // substitution pattern. The Flight payload is full of them.
  const stripped = original.replace(INLINE_SCRIPT, (_match, body) => {
    const first = blocks.length === 0;
    blocks.push(body.endsWith(';') ? body : `${body};`);
    return first ? PLACEHOLDER : '';
  });

  if (blocks.length === 0) {
    return null;
  }

  const code = blocks.join('\n');
  const hash = createHash('sha256').update(code).digest('hex').slice(0, 16);
  const chunkName = `inline.${hash}.js`;

  mkdirSync(CHUNK_DIR, { recursive: true });
  writeFileSync(join(CHUNK_DIR, chunkName), code);

  // Root-relative so it resolves the same from any page depth.
  const src = `/_next/static/chunks/${chunkName}`;
  writeFileSync(
    htmlPath,
    stripped.replace(PLACEHOLDER, () => `<script src="${src}"></script>`),
  );

  return { chunkName, blocks: blocks.length, bytes: code.length };
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

const htmlFiles = findHtml(OUT_DIR);
if (htmlFiles.length === 0) {
  console.error(`no HTML found under ${OUT_DIR}/ — did next build run?`);
  process.exit(1);
}

let touched = 0;
for (const file of htmlFiles) {
  const result = externalise(file);
  if (result !== null) {
    touched += 1;
    console.log(
      `  ${relative(OUT_DIR, file)}: ${result.blocks} inline block(s) -> ${result.chunkName} (${result.bytes} B)`,
    );
  }
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
  const remaining = readFileSync(file, 'utf8').match(INLINE_SCRIPT);
  if (remaining !== null) {
    console.error(`${file} still has ${remaining.length} inline script(s)`);
    process.exit(1);
  }
}

console.log(`externalised inline scripts in ${touched} of ${htmlFiles.length} page(s)`);
