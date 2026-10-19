// SPDX-License-Identifier: MIT-0
/**
 * Write every synthesised stack template to a directory outside the repository, for the IaC
 * scanners (cfn-nag, Checkov) to read.
 *
 * The scanners skip `cdk.out`, where CDK writes templates, and a dev synth carries the real
 * account ID. This exports `everyTemplate()` instead: the test configuration (account
 * 111122223333, eu-west-1, every optional stack provisioned), so the output names no real
 * identifier. These are test-config templates, not dev: they include resources a us-east-1 dev
 * synth does not have, so their finding counts are not comparable with a dev scan; compare
 * per-rule dispositions instead.
 *
 * A Lambda asset's `S3Key` is the asset's SHA-256 content hash plus `.zip` (aws-cdk-lib's asset
 * manifest builder), which a secret scanner reads as a high-entropy key. The scan input carries a
 * fixed placeholder in its place (`scanSafeTemplate`); nothing else in a template changes.
 *
 * It also writes `<OUT>/.ash/.ash.yaml`: `.ash/templates.ash.yaml` plus one line-bound secret
 * suppression for each line whose shape is a reviewed false positive (`secretSuppressions`). A
 * secret-scanner match on any other line, such as a literal credential in a Lambda environment,
 * stays open and fails the scan.
 *
 * Usage (after `make build-lambdas`):
 *   cd infra && npx ts-node scripts/export-scan-templates.ts <OUT>
 *
 * Refuses a missing OUT and an OUT that resolves inside the repository, so scan input is never
 * committed by accident.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { everyTemplate } from '../test/support/templates';

const REPOSITORY = fs.realpathSync(path.resolve(__dirname, '..', '..'));

/** A CDK asset object key: a 64-hex content hash and the asset's packaging extension. */
const ASSET_KEY = /^[0-9a-f]{64}\.(zip|json)$/;

function fail(message: string): never {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

/**
 * A deep copy of `template` with every asset-hash `S3Key` string replaced by `cdk-asset.zip` (or
 * `cdk-asset.json`). A token-valued key (`Fn::Join`, `Fn::Sub`) and any other string are left as
 * they are.
 */
export function scanSafeTemplate(template: unknown): unknown {
  if (Array.isArray(template)) return template.map(scanSafeTemplate);
  if (template === null || typeof template !== 'object') return template;
  return Object.fromEntries(Object.entries(template as Record<string, unknown>).map(([key, item]) => {
    const match = key === 'S3Key' && typeof item === 'string' ? ASSET_KEY.exec(item) : null;
    return [key, match === null ? scanSafeTemplate(item) : `cdk-asset.${match[1]}`];
  }));
}

/** How many asset-hash `S3Key` strings `scanSafeTemplate` replaces in `template`. */
export function assetHashKeyCount(template: unknown): number {
  if (Array.isArray(template)) return template.reduce((sum: number, item) => sum + assetHashKeyCount(item), 0);
  if (template === null || typeof template !== 'object') return 0;
  return Object.entries(template as Record<string, unknown>).reduce((sum, [key, item]) =>
    sum + (key === 'S3Key' && typeof item === 'string' && ASSET_KEY.test(item) ? 1 : assetHashKeyCount(item)), 0);
}

/** One ASH suppression bound to a single line of one exported template. */
export interface LineSuppression {
  readonly path: string;
  readonly rule_id: string;
  readonly line_start: number;
  readonly line_end: number;
  readonly reason: string;
}

/** detect-secrets' Base64HighEntropyString limit: only a string scoring above it is reported. */
const BASE64_ENTROPY_LIMIT = 4.5;

/** Keys whose value is a SHA-256 digest of committed content (A-ENTROPY-IDENTIFIER-001). */
const DIGEST_KEYS = new Set(['SpecDigest', 'contractDigest', 'ADL_PUBLISHER_DEPLOYMENT_GENERATION']);

/** Template sections whose keys are CDK logical IDs. */
const LOGICAL_ID_SECTIONS = ['Resources', 'Parameters', 'Outputs', 'Conditions', 'Mappings', 'Rules'];

function shannonEntropy(value: string): number {
  const counts = new Map<string, number>();
  for (const char of value) counts.set(char, (counts.get(char) ?? 0) + 1);
  return [...counts.values()].reduce((sum, count) => sum - (count / value.length) * Math.log2(count / value.length), 0);
}

/**
 * Line-bound secret suppressions for one exported template, `text` as written. A line is
 * suppressed only when its whole content is one of these reviewed shapes:
 * - a CDK logical ID declared in the template, as a section key (`"Id": {`), a `"Ref": "Id"` or a
 *   bare array item (`DependsOn`, `Fn::GetAtt`), whose entropy detect-secrets would report;
 * - `"LogGroupName": "/aws/lambda/<name>"` for a FunctionName literal in the same template;
 * - a 64-hex SHA-256 digest under a key in `DIGEST_KEYS`;
 * - `"ADL_<NAME>_TOKEN_AUDIENCE": "adlytics-<env>-<name>"`, a public token audience;
 * - the user pool client's `ClientSecretSource` field set to `EXTERNAL`, a field name and enum
 *   value.
 */
export function secretSuppressions(file: string, text: string): LineSuppression[] {
  const template = JSON.parse(text) as Record<string, Record<string, { Type?: string; Properties?: Record<string, unknown> }>>;
  const logicalIds = new Set(LOGICAL_ID_SECTIONS.flatMap((section) => Object.keys(template[section] ?? {})));
  const functionNames = new Set(Object.values(template.Resources ?? {})
    .filter((resource) => resource.Type === 'AWS::Lambda::Function')
    .map((resource) => resource.Properties?.FunctionName)
    .filter((name): name is string => typeof name === 'string'));
  const suppressions: LineSuppression[] = [];
  const add = (line: number, rule: string, reason: string): void => {
    suppressions.push({ path: file, rule_id: rule, line_start: line, line_end: line, reason });
  };
  text.split('\n').forEach((raw, index) => {
    const line = raw.trim();
    const n = index + 1;
    const id = (/^"([^"]+)": \{$/.exec(line) ?? /^"Ref": "([^"]+)"$/.exec(line) ?? /^"([^"]+)",?$/.exec(line))?.[1];
    if (id !== undefined && logicalIds.has(id)) {
      if (shannonEntropy(id) > BASE64_ENTROPY_LIMIT) {
        add(n, 'SECRET-BASE64-HIGH-ENTROPY-STRING', 'A-ENTROPY-IDENTIFIER-001: CDK logical ID');
      }
      return;
    }
    const logGroup = /^"LogGroupName": "(\/aws\/lambda\/([^"]+))",?$/.exec(line);
    if (logGroup !== null && functionNames.has(logGroup[2]!) && shannonEntropy(logGroup[1]!) > BASE64_ENTROPY_LIMIT) {
      add(n, 'SECRET-BASE64-HIGH-ENTROPY-STRING', 'A-ENTROPY-IDENTIFIER-001: Lambda log group name');
      return;
    }
    const digest = /^"([A-Za-z_]+)": "[0-9a-f]{64}",?$/.exec(line);
    if (digest !== null && DIGEST_KEYS.has(digest[1]!)) {
      add(n, 'SECRET-HEX-HIGH-ENTROPY-STRING', `A-ENTROPY-IDENTIFIER-001: SHA-256 digest in ${digest[1]}`);
    } else if (/^"ADL_[A-Z_]+_TOKEN_AUDIENCE": "adlytics-[a-z0-9]+-[a-z0-9-]+",?$/.test(line)) {
      add(n, 'CKV_SECRET_6', 'A-ENTROPY-IDENTIFIER-001: public token audience');
      // pragma: allowlist nextline secret (A-PROTOCOL-CONSTANT-001)
    } else if (/^"ClientSecretSource": "EXTERNAL",?$/.test(line)) {
      add(n, 'SECRET-SECRET-KEYWORD', 'A-PROTOCOL-CONSTANT-001: the ClientSecretSource field name, not a value');
    }
  });
  return suppressions;
}

/** `.ash/templates.ash.yaml` (ending in `suppressions:`) with each entry appended as a YAML item. */
export function templateAshConfig(base: string, suppressions: readonly LineSuppression[]): string {
  if (suppressions.length === 0) return `${base.trimEnd()} []\n`;
  const items = suppressions.map((entry) => [
    `    - path: ${JSON.stringify(entry.path)}`,
    `      rule_id: ${entry.rule_id}`,
    `      line_start: ${entry.line_start}`,
    `      line_end: ${entry.line_end}`,
    `      reason: ${JSON.stringify(entry.reason)}`,
  ].join('\n'));
  return `${base.trimEnd()}\n${items.join('\n')}\n`;
}

/** Resolve OUT through any symlinks, including those of a not-yet-created directory's parents. */
function resolvedOut(raw: string): string {
  let existing = path.resolve(raw);
  const missing: string[] = [];
  while (!fs.existsSync(existing)) {
    missing.unshift(path.basename(existing));
    const parent = path.dirname(existing);
    if (parent === existing) break;
    existing = parent;
  }
  return path.join(fs.realpathSync(existing), ...missing);
}

function main(argv: readonly string[]): void {
  const [raw] = argv;
  if (raw === undefined || raw.trim() === '') fail('export-scan-templates: OUT is required');
  const out = resolvedOut(raw);
  const relative = path.relative(REPOSITORY, out);
  if (relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative))) {
    fail('export-scan-templates: OUT must be outside the repository');
  }
  fs.mkdirSync(out, { recursive: true });
  const templates = everyTemplate();
  let replaced = 0;
  const suppressions: LineSuppression[] = [];
  for (const [name, template] of Object.entries(templates)) {
    replaced += assetHashKeyCount(template);
    const file = `${name}.template.json`;
    const text = `${JSON.stringify(scanSafeTemplate(template), null, 1)}\n`;
    fs.writeFileSync(path.join(out, file), text);
    suppressions.push(...secretSuppressions(file, text));
  }
  fs.mkdirSync(path.join(out, '.ash'), { recursive: true });
  fs.writeFileSync(
    path.join(out, '.ash', '.ash.yaml'),
    templateAshConfig(fs.readFileSync(path.join(REPOSITORY, '.ash', 'templates.ash.yaml'), 'utf8'), suppressions),
  );
  process.stdout.write(
    `export-scan-templates: wrote ${Object.keys(templates).length} templates, `
    + `replaced ${replaced} asset-hash S3Key values, wrote ${suppressions.length} line-bound secret suppressions\n`,
  );
}

if (require.main === module) main(process.argv.slice(2));
