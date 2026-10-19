// SPDX-License-Identifier: MIT-0
/** Ensure every adjacent TypeScript compiler emission has its owning source marker. */

const fs = require('node:fs');
const path = require('node:path');

const MARKER = '// SPDX-License-Identifier: MIT-0';
const INFRA_ROOT = path.resolve(__dirname, '..');
const EMISSION_ROOTS = ['bin', 'lib', 'test'];

function emittedFiles(directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...emittedFiles(entryPath));
    } else if (entry.isFile() && (entry.name.endsWith('.js') || entry.name.endsWith('.d.ts'))) {
      files.push(entryPath);
    }
  }
  return files;
}

function owningSource(emittedPath) {
  if (emittedPath.endsWith('.d.ts')) {
    return `${emittedPath.slice(0, -'.d.ts'.length)}.ts`;
  }
  return `${emittedPath.slice(0, -'.js'.length)}.ts`;
}

function sourceMarker(sourcePath) {
  const lines = fs.readFileSync(sourcePath, 'utf8').split(/\r?\n/);
  const markerIndex = lines[0].startsWith('#!') ? 1 : 0;
  if (lines[markerIndex] !== MARKER) {
    throw new Error(`Owning TypeScript source has no valid SPDX marker: ${sourcePath}`);
  }
  return lines[markerIndex];
}

function ensureMarker(text, marker) {
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const finalNewline = text.endsWith('\n');
  const lines = text.split(/\r?\n/);
  if (finalNewline) {
    lines.pop();
  }
  const filtered = lines.filter((line) => line !== marker);
  const markerIndex = filtered[0]?.startsWith('#!') ? 1 : 0;
  filtered.splice(markerIndex, 0, marker);
  return `${filtered.join(newline)}${finalNewline ? newline : ''}`;
}

const pairs = EMISSION_ROOTS.flatMap((rootName) => emittedFiles(path.join(INFRA_ROOT, rootName)))
  .map((emittedPath) => ({ emittedPath, sourcePath: owningSource(emittedPath) }))
  .filter(({ sourcePath }) => fs.existsSync(sourcePath));

const validated = pairs.map((pair) => ({ ...pair, marker: sourceMarker(pair.sourcePath) }));
let changed = 0;
for (const { emittedPath, marker } of validated) {
  const current = fs.readFileSync(emittedPath, 'utf8');
  const updated = ensureMarker(current, marker);
  if (updated !== current) {
    fs.writeFileSync(emittedPath, updated, 'utf8');
    changed += 1;
  }
}

console.log(`SPDX post-build: ${validated.length} emissions checked, ${changed} updated.`);
