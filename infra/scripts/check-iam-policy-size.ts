// SPDX-License-Identifier: MIT-0
/**
 * Apply the IAM policy-size check (`test/support/iam-policy-size.ts`) to a `cdk synth` output
 * directory, such as the credential-free dev synth.
 *
 * `test/iam-policy-size.test.ts` runs the check on `everyTemplate()` in `make infra-test`. This
 * runs it on the templates an environment actually deploys, whose names and optional stacks can
 * differ from the test configuration. It reads `*.template.json` only and calls no AWS API.
 *
 * Usage:
 *   cd infra && npx ts-node scripts/check-iam-policy-size.ts <SYNTH_DIR> --account <12 digits> --region <region>
 *
 * The account and region are what the synth rendered for (the credential-free synth uses a
 * placeholder account); they only set the length of rendered ARNs. Prints the five largest roles
 * and every finding; exits 1 on a finding and 2 on a usage error.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  ROLE_INLINE_LIMIT,
  ROLE_INLINE_THRESHOLD,
  policySizeFindings,
  roleBudgets,
} from '../test/support/iam-policy-size';

function usage(message: string): never {
  process.stderr.write(
    `${message}\nusage: check-iam-policy-size.ts <SYNTH_DIR> --account <12 digits> --region <region>\n`,
  );
  process.exit(2);
}

function main(argv: readonly string[]): void {
  const [directory, ...rest] = argv;
  if (directory === undefined || directory.startsWith('--')) usage('SYNTH_DIR is required');
  const flags = new Map<string, string>();
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    const value = rest[index + 1];
    if ((flag !== '--account' && flag !== '--region') || value === undefined) {
      usage(`unexpected argument ${String(flag)}`);
    }
    flags.set(flag, value);
  }
  const account = flags.get('--account');
  const region = flags.get('--region');
  if (account === undefined || !/^\d{12}$/.test(account)) usage('--account must be 12 digits');
  if (region === undefined || !/^[a-z]{2}(-[a-z]+)+-\d$/.test(region)) usage('--region must name a region');
  if (!fs.statSync(directory, { throwIfNoEntry: false })?.isDirectory()) usage(`${directory} is not a directory`);

  const files = fs.readdirSync(directory).filter((name) => name.endsWith('.template.json')).sort();
  if (files.length === 0) usage(`${directory} holds no *.template.json`);
  const templates = Object.fromEntries(
    files.map((name) => [
      name.replace(/\.template\.json$/, ''),
      JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8')) as Record<string, unknown>,
    ]),
  );
  const context = { account, partition: 'aws', regionOf: () => region, stackNameOf: (stack: string) => stack };
  const budgets = roleBudgets(templates, context);
  const findings = policySizeFindings(templates, context);
  const lines = [
    `IAM policy size: ${files.length} templates, ${budgets.length} roles; inline threshold ${ROLE_INLINE_THRESHOLD} of ${ROLE_INLINE_LIMIT} characters`,
    ...budgets
      .slice(0, 5)
      .map((budget) => `  ${budget.role}: inline ${budget.inlineTotal} [${budget.inline.map((item) => `${item.policy}=${item.size}`).join(', ')}], managed [${budget.managed.map((item) => item.size).join(', ')}]`),
    ...findings.map((finding) => `FINDING ${finding}`),
    findings.length === 0 ? 'IAM policy size: no findings' : `IAM policy size: ${findings.length} finding(s)`,
  ];
  process.stdout.write(`${lines.join('\n')}\n`);
  process.exit(findings.length === 0 ? 0 : 1);
}

main(process.argv.slice(2));
