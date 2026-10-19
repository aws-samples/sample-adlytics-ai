// SPDX-License-Identifier: MIT-0
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const vitest = join(root, 'node_modules', 'vitest', 'vitest.mjs');
const testNames = [
  'committed bindings cover exactly the six static routes with section objects',
  'signed-out auth gate is accessible and exposes only managed-login Sign in',
  'session service failures expose retry instead of claiming signed out',
  'each authenticated route calls exactly its bound frontend data function',
  'reporting window selects one exact custom period',
  'partial default is explicit and retains the governed preferred duration',
  'campaign and product selection pass only their opaque reference',
  'authenticated routes expose loading, error, and unavailable states',
  'section objects render only exact role, name, and variant metric slots',
  'page content labels suppressed and partial coverage truthfully',
  'mobile assistant is a modal dialog with backdrop, Escape, and focus restoration',
  'desktop assistant is a nonmodal aside with exact separator keyboard ARIA',
  'theme changes do not steal focus from the theme control',
  'retained shell keeps chat in memory while persisting preferences only',
  'chat blocks same-tick duplicate submits without erasing prior history',
  'chat aborts its active transport when unmounted',
  'chat nests structured response headings beneath its panel heading',
  'chat renders typed progress without exposing transport details',
  'chat renders safe refusal and generic failure messages while emitting only a closed stream category',
  'chat supports the legacy MediaQueryList listener API',
  'home uses governed display copy, honest snapshot sections, and Workspace chat custody',
  'active page expiry downgrades to Workspace before the next assistant request',
  'an in-flight answer keeps submitted provenance and the next turn uses only the new ref',
  'regular layout uses a nonmodal overlay with Escape, backdrop, and focus restoration',
  'classified page errors use closed copy rather than backend descriptions',
  'channel v2 renders supplied period relation, model ties, exact values, and accessible controls',
  'channel selection uses only opaque capabilities and ignores a stale daily revision',
  'expired channel capabilities remain disabled and never cross the API boundary',
  'campaign detail retains the safe signature while loading and renders only supplied exact values',
  'campaign list renders covered-empty and insufficient outcomes with deterministic recovery',
  'changing campaign clears detail context and ignores a stale detail response',
  'invalid campaign selection automatically returns to a fresh list',
  'expired campaign capability stays opaque, disabled, and refreshes instead of selecting',
  'represented campaign zero remains a dated value rather than an empty state',
  'ambiguous campaign list withholds every colliding selection',
  'insufficient campaign detail keeps partial values and supplied one-sided comparison reasons',
  'campaign controller preserves the chosen signature and rejects mismatched detail context',
  'chat commits one assistant article atomically only after the terminal result',
  'authenticated shell is accessible once governed evidence is ready',
  'Retail Product renders closed list states and selected comparison without public identifiers',
];

for (const testName of testNames) {
  const pattern = `^${testName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`;
  const result = spawnSync(
    process.execPath,
    [vitest, 'run', 'test/ui.test.tsx', '--testNamePattern', pattern, ...process.argv.slice(2)],
    { cwd: root, stdio: 'inherit' },
  );
  if (result.error !== undefined) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
