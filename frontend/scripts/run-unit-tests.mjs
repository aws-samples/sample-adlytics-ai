// SPDX-License-Identifier: MIT-0
import { readdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const output = join(root, '.test-dist');
const compiler = join(root, 'node_modules', 'typescript', 'bin', 'tsc');
let status = 1;

try {
  rmSync(output, { force: true, recursive: true });
  const compiled = spawnSync(process.execPath, [compiler, '-p', 'tsconfig.test.json'], {
    cwd: root,
    stdio: 'inherit',
  });
  if (compiled.status === 0) {
    const tests = readdirSync(join(output, 'test'))
      .filter((name) => name.endsWith('.test.js'))
      .sort()
      .map((name) => join(output, 'test', name));
    const tested = spawnSync(process.execPath, ['--test', ...tests], {
      cwd: root,
      stdio: 'inherit',
    });
    status = tested.status ?? 1;
  } else {
    status = compiled.status ?? 1;
  }
} finally {
  rmSync(output, { force: true, recursive: true });
}

process.exitCode = status;
