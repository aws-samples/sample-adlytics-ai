// SPDX-License-Identifier: MIT-0
const fs = require('node:fs');
const path = require('node:path');

require('ts-node/register');
const {
  ENV_NAME_MAX_LENGTH,
  ENV_NAME_PATTERN,
  PARAMETER_PATH_SEGMENTS,
  PARAMS,
} = require('../lib/config/index.ts');

const outputPath = path.resolve(__dirname, '../../contracts/generated/parameter-paths.json');
const expected = `${JSON.stringify(
  {
    version: 1,
    environmentName: {
      pattern: ENV_NAME_PATTERN.source,
      maxLength: ENV_NAME_MAX_LENGTH,
    },
    pathSegments: PARAMETER_PATH_SEGMENTS,
    parameters: PARAMS,
  },
  null,
  2,
)}\n`;

if (process.argv.includes('--check')) {
  let actual;
  try {
    actual = fs.readFileSync(outputPath, 'utf8');
  } catch {
    console.error(`Generated parameter contract is missing: ${outputPath}`);
    process.exit(1);
  }
  if (actual !== expected) {
    console.error('Generated parameter contract is stale. Run make generate-parameter-contract.');
    process.exit(1);
  }
} else {
  fs.writeFileSync(outputPath, expected, 'utf8');
}
