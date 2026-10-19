// SPDX-License-Identifier: MIT-0
/**
 * Fail if a module ADR-016 recorded as L1-only has gained a stable L2.
 *
 * ADR-016 decided the data layer is hand-written Cfn resources, on evidence that
 * these modules export nothing but Cfn* at the pinned aws-cdk-lib. That evidence
 * expires on every library bump, and a rule asking a human to re-run a probe is a
 * rule that silently stops happening — so it is a check instead.
 *
 * A failure here is not a defect. It means an L2 landed and the decision should be
 * reopened for that service, which is a good problem and a deliberate one to
 * surface loudly rather than discover months later.
 */

// aws-glue left this list on 2026-10-16, when aws-cdk-lib 2.273.0 shipped a stable Glue L2; ADR-016
// is reopened for Glue and records the evidence.
const MODULES = [
  'aws-athena',
  'aws-lakeformation',
  'aws-s3tables',
];

// Scoped to the Policy resources rather than the whole module: aws-bedrockagentcore
// ships stable L2s for other areas, so asserting the module is L1 would be false.
//
// The test is the same everywhere — a name that mentions Policy and does not start
// with Cfn. An earlier version allowlisted the known Cfn names instead, which
// reported CfnResourcePolicy as a graduated L2 purely for being absent from the
// list. Deriving from the Cfn prefix cannot drift as the module grows.

const version = require('aws-cdk-lib/package.json').version;
console.log('CONSTRUCT LEVEL CHECK');
console.log('='.repeat(60));
console.log(`  aws-cdk-lib: ${version}`);

const graduated = [];

for (const moduleName of MODULES) {
  const exports = Object.keys(require(`aws-cdk-lib/${moduleName}`));
  const stable = exports.filter((name) => !name.startsWith('Cfn'));
  console.log(
    `  ${moduleName.padEnd(20)} ${String(exports.length).padStart(3)} exports, ` +
      `${stable.length === 0 ? 'L1 only' : `L2 present: ${stable.join(', ')}`}`,
  );
  if (stable.length > 0) {
    graduated.push({ moduleName, stable });
  }
}

const agentcore = Object.keys(require('aws-cdk-lib/aws-bedrockagentcore'));

// The AgentCore Policy area left this list on 2026-10-16, when aws-cdk-lib 2.273.0 shipped its
// stable L2 (PolicyEngine, Policy, PolicyStatement, ...); ADR-016 is reopened for it. No AgentCore
// area is tracked as L1 only now; add one here with its pattern if a decision records one.
const L1_AREAS = [];

for (const { label, pattern } of L1_AREAS) {
  const stable = agentcore.filter((name) => pattern.test(name) && !name.startsWith('Cfn'));
  console.log(
    `  ${label.padEnd(20)} ${stable.length === 0 ? 'L1 only' : `L2 present: ${stable.join(', ')}`}`,
  );
  if (stable.length > 0) {
    graduated.push({ moduleName: `aws-bedrockagentcore (${label})`, stable });
  }
}

if (graduated.length > 0) {
  console.error('\nA stable L2 has landed where ADR-016 recorded L1 only:');
  for (const { moduleName, stable } of graduated) {
    console.error(`  ${moduleName}: ${stable.join(', ')}`);
  }
  console.error(
    '\nReopen docs/decisions/adr-016-data-layer-construct-levels.md for that service,\n' +
      'then update this check and .kiro/steering/infra-style-cdk.md together.',
  );
  process.exit(1);
}

console.log('\nEvery module ADR-016 recorded as L1 only is still L1 only.');
