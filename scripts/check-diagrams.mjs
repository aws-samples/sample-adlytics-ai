// SPDX-License-Identifier: MIT-0
/**
 * Parse inline Mermaid blocks and validate SVG diagram files and their image references in
 * README.md and docs/ (plus local working documents when present).
 *
 * Diagrams drawn in SVG are hand-authored files in a diagrams/ directory beside the one
 * Markdown document that owns them, referenced from it as a Markdown image. GitHub and
 * GitLab strip inline <svg> from rendered Markdown (evidence: docs/conventions.md §8), so
 * an inline SVG is a finding. Mermaid
 * is permitted only for explicitly captioned Sequence and Call-flow diagrams. A broken or
 * misclassified diagram otherwise stays invisible until someone opens the document.
 *
 * Setup:  cd scripts && npm install
 * Usage:  node scripts/check-diagrams.mjs
 * Exits non-zero on any finding.
 */

import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// docs/ is scanned, plus local working documents when present. Several local
// planning documents carry diagrams; skipping them would drop them from this check
// without reporting anything, which is the failure mode this script exists to
// prevent. The root README.md and its diagrams/ directory are the landing page.
const SCAN_DIRS = [path.join(ROOT, 'docs'), path.join(ROOT, 'internal')];
const ROOT_MARKDOWN = [path.join(ROOT, 'README.md')];
const ROOT_DIAGRAMS = path.join(ROOT, 'diagrams');
const DIAGRAM_DIR = 'diagrams';
const SVG_NS = 'http://www.w3.org/2000/svg';
const BACKGROUND_FILL = '#ffffff';

// Mermaid needs a DOM even just to parse. The SVG validator uses DOMParser from
// the same existing jsdom dependency, so no renderer or additional package is
// needed for either diagram format.
const dom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;

// Node 21 added a built-in `navigator` global defined as a getter, so plain
// assignment throws "Cannot set property navigator of #<Object> which has only a
// getter" — silently fine on Node 20 and fatal on the 24 LTS line this repo now
// pins. defineProperty replaces the accessor rather than writing through it.
Object.defineProperty(globalThis, 'navigator', {
  value: dom.window.navigator,
  configurable: true,
  writable: true,
});

const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false });

function filesWithSuffix(dir, suffix) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...filesWithSuffix(p, suffix));
    else if (entry.name.endsWith(suffix)) out.push(p);
  }
  return out;
}

function lineAt(markdown, index) {
  return markdown.slice(0, index).split('\n').length;
}

function addFinding(findings, file, format, block, line, message) {
  findings.push({
    file: path.relative(ROOT, file),
    format,
    block,
    line,
    message,
  });
}

function isExternalReference(value) {
  return value.trim() !== '' && !value.trim().startsWith('#');
}

function hasExternalCssReference(value) {
  const urlMatches = value.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi);
  for (const match of urlMatches) {
    if (isExternalReference(match[2])) return true;
  }
  return /@import\b/i.test(value);
}

function normalizedText(element) {
  return element.textContent.replace(/\s+/g, ' ').trim();
}

// The first drawn element must be an opaque white rect covering the whole viewBox,
// so the diagram stays legible when the host page is dark.
function hasBackgroundRect(root, viewBoxNumbers) {
  const first = [...root.children].find(
    (element) => !['title', 'desc', 'defs', 'metadata'].includes(element.localName.toLowerCase()),
  );
  if (first?.localName.toLowerCase() !== 'rect') return false;
  const [minX, minY, width, height] = viewBoxNumbers;
  const number = (name, fallback) => Number(first.getAttribute(name) ?? fallback);
  const opaque = ['opacity', 'fill-opacity'].every(
    (name) => !first.hasAttribute(name) || Number(first.getAttribute(name)) === 1,
  );
  return (
    number('x', 0) === minX &&
    number('y', 0) === minY &&
    number('width', NaN) === width &&
    number('height', NaN) === height &&
    first.getAttribute('fill')?.toLowerCase() === BACKGROUND_FILL &&
    !first.hasAttribute('style') &&
    !first.hasAttribute('transform') &&
    opaque
  );
}

/** Validate one SVG file; return its "<title>. <desc>" text, or null when unlabelled. */
function validateSvg(svgSource, findings, file) {
  const report = (message) => addFinding(findings, file, 'SVG', 1, 1, message);
  const svgDocument = new dom.window.DOMParser().parseFromString(svgSource, 'image/svg+xml');
  if (svgDocument.querySelector('parsererror')) {
    report('SVG is not valid XML.');
    return null;
  }

  const root = svgDocument.documentElement;
  if (root.localName?.toLowerCase() !== 'svg') {
    report('SVG root element is missing or invalid.');
    return null;
  }
  if (root.namespaceURI !== SVG_NS) {
    report(`root <svg> needs xmlns="${SVG_NS}"; a browser will not draw it as an image without it.`);
  }

  const viewBox = root.getAttribute('viewBox');
  const viewBoxValues = viewBox?.trim().split(/[\s,]+/) ?? [];
  const finiteNumber = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
  const validViewBox =
    viewBoxValues.length === 4 &&
    viewBoxValues.every((value) => finiteNumber.test(value)) &&
    Number(viewBoxValues[2]) > 0 &&
    Number(viewBoxValues[3]) > 0;
  if (!validViewBox) {
    report('root <svg> needs a viewBox of four finite numbers with positive width and height.');
  } else if (!hasBackgroundRect(root, viewBoxValues.map(Number))) {
    report(
      `the first drawn element must be a <rect> covering the whole viewBox with fill="${BACKGROUND_FILL}" and no opacity, style, or transform.`,
    );
  }

  if (root.getAttribute('role') !== 'img') {
    report('root <svg> needs role="img".');
  }
  if (root.getAttribute('focusable') !== 'false') {
    report('root <svg> needs focusable="false".');
  }

  let label = null;
  const labelledBy = root.getAttribute('aria-labelledby')?.trim();
  if (!labelledBy) {
    report('root <svg> needs aria-labelledby.');
  } else {
    const labelledElements = labelledBy
      .split(/\s+/)
      .map((id) => svgDocument.getElementById(id));
    const title = labelledElements.find(
      (element) => element?.localName?.toLowerCase() === 'title' && normalizedText(element) !== '',
    );
    const desc = labelledElements.find(
      (element) => element?.localName?.toLowerCase() === 'desc' && normalizedText(element) !== '',
    );
    if (!title || !desc) {
      report('aria-labelledby must reference non-empty local <title> and <desc> elements.');
    } else {
      label = `${normalizedText(title)}. ${normalizedText(desc)}`;
      if (/[[\]]/.test(label)) {
        report('<title> and <desc> must not contain square brackets; they become the image alt text.');
      }
    }
  }

  const ids = new Set();
  for (const element of svgDocument.querySelectorAll('[id]')) {
    const id = element.getAttribute('id');
    if (ids.has(id)) report(`id "${id}" is declared more than once.`);
    ids.add(id);
  }

  for (const element of svgDocument.querySelectorAll('*')) {
    const name = element.localName.toLowerCase();
    if (name === 'script' || name === 'foreignobject') {
      report(`<${element.localName}> is not allowed in a diagram.`);
    }

    for (const attribute of element.attributes) {
      const attributeName = attribute.name.toLowerCase();
      if (attributeName.startsWith('on')) {
        report(`event-handler attribute ${attribute.name} is not allowed.`);
      }
      if (
        (attributeName === 'href' || attributeName === 'xlink:href') &&
        isExternalReference(attribute.value)
      ) {
        report(`external ${attribute.name} reference is not allowed.`);
      }
      if (attributeName === 'style' && hasExternalCssReference(attribute.value)) {
        report('external URL reference in style is not allowed.');
      }
    }

    if (name === 'image') {
      const reference = element.getAttribute('href') ?? element.getAttribute('xlink:href');
      if (reference && isExternalReference(reference)) {
        report('external image reference is not allowed.');
      }
    }
    if (name === 'style' && hasExternalCssReference(element.textContent)) {
      report('external URL reference in <style> is not allowed.');
    }
  }

  return label;
}

const MERMAID_BLOCK = /```mermaid\n([\s\S]*?)```/g;
const SVG_OPEN = /<svg\b/gi;
const SVG_IMAGE = /!\[([^\]]*)\]\(([^)\s]*\.svg)(?:\s[^)]*)?\)/gi;
const HTML_SVG_IMAGE = /<img\b[^>]*\bsrc\s*=\s*["']?[^"'\s>]*\.svg\b/gi;
const findings = [];
const totals = { mermaid: 0, svgFiles: 0, svgImages: 0 };

function immediatelyPrecedingBoldCaption(markdown, index) {
  const precedingParagraph = markdown.slice(0, index).trimEnd().split(/\r?\n[ \t]*\r?\n/).at(-1);
  const match = precedingParagraph?.match(/^\s*\*\*([^*\n]+?)\*\*/);
  return match?.[1].trim() || null;
}

function immediatelyFollowingTakeaway(markdown) {
  return /^\s*> \*\*Takeaway\./.test(markdown);
}

function maskMarkdownCode(markdown) {
  return markdown.replace(/```[\s\S]*?```|`[^`\n]*`/g, (match) => match.replace(/[^\n]/g, ' '));
}

function firstMeaningfulMermaidLine(source) {
  return source
    .split('\n')
    .map((line) => line.trim())
    .find((line) => line !== '' && !line.startsWith('%%')) ?? '';
}

const scanned = [
  ...ROOT_MARKDOWN.filter((file) => fs.existsSync(file)),
  ...SCAN_DIRS.filter((directory) => fs.existsSync(directory))
    .flatMap((directory) => filesWithSuffix(directory, '.md'))
    .sort(),
];

// Every diagram file in scope, mapped to its "<title>. <desc>" label and the
// references found to it. A file in scope is a candidate even when nothing links it.
const diagrams = new Map();
const svgFiles = [
  ...(fs.existsSync(ROOT_DIAGRAMS) ? filesWithSuffix(ROOT_DIAGRAMS, '.svg') : []),
  ...SCAN_DIRS.filter((directory) => fs.existsSync(directory)).flatMap((directory) =>
    filesWithSuffix(directory, '.svg'),
  ),
].sort();
for (const file of svgFiles) {
  totals.svgFiles++;
  if (path.basename(path.dirname(file)) !== DIAGRAM_DIR) {
    addFinding(findings, file, 'SVG', 1, 1, `a diagram file must live in a ${DIAGRAM_DIR}/ directory beside the document that owns it.`);
  }
  const label = validateSvg(fs.readFileSync(file, 'utf8'), findings, file);
  diagrams.set(file, { label, references: [] });
}

for (const file of scanned) {
  const markdown = fs.readFileSync(file, 'utf8');
  const mermaidBlocks = [...markdown.matchAll(MERMAID_BLOCK)];

  for (const [index, match] of mermaidBlocks.entries()) {
    totals.mermaid++;
    const line = lineAt(markdown, match.index);
    const caption = immediatelyPrecedingBoldCaption(markdown, match.index);
    const firstLine = firstMeaningfulMermaidLine(match[1]);
    const isSequence = firstLine === 'sequenceDiagram';
    const isCallFlow = /^(?:flowchart|graph)\b/i.test(firstLine);

    if (isSequence && !caption?.includes('Sequence diagram')) {
      addFinding(
        findings,
        file,
        'Mermaid',
        index + 1,
        line,
        'sequenceDiagram requires an immediately preceding bold caption containing "Sequence diagram".',
      );
    } else if (isCallFlow && !caption?.includes('Call-flow diagram')) {
      addFinding(
        findings,
        file,
        'Mermaid',
        index + 1,
        line,
        'flowchart/graph requires an immediately preceding bold caption containing "Call-flow diagram".',
      );
    } else if (!isSequence && !isCallFlow) {
      addFinding(
        findings,
        file,
        'Mermaid',
        index + 1,
        line,
        `Mermaid is allowed only for sequenceDiagram or flowchart/graph call flows; first meaningful line is "${firstLine || '(empty)'}".`,
      );
    }

    try {
      await mermaid.parse(match[1]);
    } catch (err) {
      addFinding(
        findings,
        file,
        'Mermaid',
        index + 1,
        line,
        String(err.message).split('\n')[0],
      );
    }
  }

  // Masking keeps offsets, so a match in the masked copy indexes the original.
  const diagramMarkdown = maskMarkdownCode(markdown);
  for (const match of diagramMarkdown.matchAll(SVG_OPEN)) {
    addFinding(
      findings,
      file,
      'SVG',
      0,
      lineAt(markdown, match.index),
      `inline <svg> is stripped by GitHub and GitLab; move it to ${DIAGRAM_DIR}/<name>.svg and reference it as a Markdown image.`,
    );
  }
  for (const match of diagramMarkdown.matchAll(HTML_SVG_IMAGE)) {
    addFinding(
      findings,
      file,
      'SVG',
      0,
      lineAt(markdown, match.index),
      'reference a diagram with Markdown image syntax, not <img>, so the link checker sees it.',
    );
  }

  let imageBlock = 0;
  for (const match of diagramMarkdown.matchAll(SVG_IMAGE)) {
    imageBlock++;
    totals.svgImages++;
    const line = lineAt(markdown, match.index);
    const end = match.index + match[0].length;
    const original = markdown.slice(match.index, end);
    const [, alt, target] = original.match(new RegExp(SVG_IMAGE.source, 'i'));
    const report = (message) => addFinding(findings, file, 'SVG image', imageBlock, line, message);

    // Without a blank line before it the image renders inline at the end of the caption.
    const precededByBlankLine = /(?:^|\n)[ \t]*\n$/.test(markdown.slice(0, match.index));
    const restOfLine = markdown.slice(end).match(/^[^\n]*/)[0];
    if (!precededByBlankLine || restOfLine.trim() !== '') {
      report('a diagram image must be alone on its line, with a blank line before it.');
    }
    if (!immediatelyPrecedingBoldCaption(markdown, match.index)) {
      report('a diagram image requires an immediately preceding non-empty bold caption.');
    }
    if (!immediatelyFollowingTakeaway(markdown.slice(end))) {
      report('a diagram image requires an immediately following "> **Takeaway." block.');
    }

    if (/^[a-z][a-z0-9+.-]*:|^\/|[?#]/i.test(target)) {
      report(`"${target}" must be a relative path to a file in this repository.`);
      continue;
    }
    const resolved = path.resolve(path.dirname(file), target);
    if (path.dirname(resolved) !== path.join(path.dirname(file), DIAGRAM_DIR)) {
      report(`"${target}" must be in the ${DIAGRAM_DIR}/ directory beside this document.`);
    }
    const diagram = diagrams.get(resolved);
    if (!diagram) {
      report(`"${target}" is not a diagram file in scope.`);
      continue;
    }
    diagram.references.push(`${path.relative(ROOT, file)}:${line}`);
    if (diagram.label !== null && alt.replace(/\s+/g, ' ').trim() !== diagram.label) {
      report(`alt text must equal the diagram's "<title>. <desc>": "${diagram.label}".`);
    }
  }
}

for (const [file, { references }] of diagrams) {
  if (references.length !== 1) {
    addFinding(
      findings,
      file,
      'SVG',
      1,
      1,
      references.length === 0
        ? 'diagram file is not referenced by any document.'
        : `diagram file must be referenced by exactly one document; found ${references.join(', ')}.`,
    );
  }
}

console.log('DIAGRAM CHECK — MERMAID + SVG FILES');
console.log('='.repeat(60));
console.log(`  Mermaid blocks:    ${totals.mermaid}`);
console.log(`  SVG diagram files: ${totals.svgFiles}`);
console.log(`  SVG image refs:    ${totals.svgImages}`);
console.log('  mode:              enforcement (only captioned sequence and call-flow Mermaid is allowed)');
console.log();

if (findings.length) {
  console.log(`FINDINGS (${findings.length})`);
  for (const finding of findings) {
    console.log(
      `    ✗ ${finding.file} ${finding.format} #${finding.block} (line ~${finding.line})`,
    );
    console.log(`      ${finding.message}`);
  }
  process.exit(1);
}

console.log('All Mermaid and SVG diagrams meet enforcement, safety, and accessibility checks.');
