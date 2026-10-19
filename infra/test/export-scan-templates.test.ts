// SPDX-License-Identifier: MIT-0
/**
 * A-ASSET-HASH-001: the scan export replaces each CDK asset content hash in an `S3Key` with a
 * fixed placeholder, and changes nothing else in any template.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { assetHashKeyCount, scanSafeTemplate, secretSuppressions, templateAshConfig } from '../scripts/export-scan-templates';
import { everyTemplate } from './support/templates';

const ASSET_KEY = /^[0-9a-f]{64}\.(zip|json)$/;

/** Every `[path, value]` leaf of a JSON value, so two templates compare leaf by leaf. */
function leaves(value: unknown, at = ''): [string, unknown][] {
  if (Array.isArray(value)) return value.flatMap((item, index) => leaves(item, `${at}[${index}]`));
  if (value !== null && typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, item]) => leaves(item, `${at}.${key}`));
  }
  return [[at, value]];
}

describe('scanSafeTemplate (A-ASSET-HASH-001)', () => {
  const templates = everyTemplate();

  it('finds asset-hash S3Key values to replace, and leaves none', () => {
    const before = Object.values(templates).reduce((sum: number, template) => sum + assetHashKeyCount(template), 0);
    expect(before).toBeGreaterThan(0);
    for (const template of Object.values(templates)) {
      const safe = scanSafeTemplate(template);
      expect(assetHashKeyCount(safe)).toBe(0);
      for (const [where, value] of leaves(safe)) {
        if (where.endsWith('.S3Key')) expect(String(value)).not.toMatch(ASSET_KEY);
      }
    }
  });

  it('changes only asset-hash S3Key strings, each to the placeholder of its extension', () => {
    for (const template of Object.values(templates)) {
      const before = leaves(template);
      const after = leaves(scanSafeTemplate(template));
      expect(after.map(([where]) => where)).toStrictEqual(before.map(([where]) => where));
      after.forEach(([where, value], index) => {
        const original = before[index]![1];
        if (where.endsWith('.S3Key') && typeof original === 'string' && ASSET_KEY.test(original)) {
          expect(value).toBe(`cdk-asset.${original.endsWith('.json') ? 'json' : 'zip'}`);
        } else {
          expect(value).toStrictEqual(original);
        }
      });
    }
  });

  it('keeps a token-valued or non-hash S3Key, and a hash under any other key', () => {
    const hash = 'a'.repeat(64);
    const token = { 'Fn::Join': ['', ['prefix/', { Ref: 'AssetParameter' }, '.zip']] };
    expect(scanSafeTemplate({
      Code: { S3Key: token },
      Other: { S3Key: 'handler.zip' },
      Json: { S3Key: `${hash}.json` },
      Description: `${hash}.zip`,
    })).toStrictEqual({
      Code: { S3Key: token },
      Other: { S3Key: 'handler.zip' },
      Json: { S3Key: 'cdk-asset.json' },
      Description: `${hash}.zip`,
    });
  });
});

describe('secretSuppressions (A-ENTROPY-IDENTIFIER-001, A-PROTOCOL-CONSTANT-001)', () => {
  // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
  const id = 'FictionalLogicalIdWithMixedCaseQuickBrownFoxJumpsVZ';
  const text = `${JSON.stringify({
    Parameters: { [id]: { Type: 'AWS::SSM::Parameter::Value<String>' } },
    Resources: {
      Fn: {
        Type: 'AWS::Lambda::Function',
        DependsOn: [id],
        Properties: {
          FunctionName: 'adlytics-dev-ExecutionGraphStuckFlowDetector',
          Environment: {
            Variables: {
              TOPIC: { Ref: id },
              SpecDigest: 'a1'.repeat(32),
              ADL_GATEWAY_TOKEN_AUDIENCE: 'adlytics-dev-gateway',
              // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
              API_KEY: 'FictionalCredentialValueForThisTestOnly',
              OtherDigest: 'b2'.repeat(32),
              OTHER_AUDIENCE: 'adlytics-dev-gateway',
            },
          },
        },
      },
      // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
      Logs: { Type: 'AWS::Logs::LogGroup', Properties: { LogGroupName: '/aws/lambda/adlytics-dev-ExecutionGraphStuckFlowDetector' } },
      // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
      Client: { Type: 'AWS::Cognito::UserPoolClient', Properties: { ClientSecretSource: 'EXTERNAL', ClientSecret: 'EXTERNAL' } },
    },
  }, null, 1)}\n`;
  const lines = text.split('\n');
  const lineOf = (needle: string): number => lines.findIndex((line) => line.trim() === needle) + 1;
  const suppressions = secretSuppressions('Stack.template.json', text);
  const rulesAt = (needle: string): string[] =>
    suppressions.filter((entry) => entry.line_start === lineOf(needle)).map((entry) => entry.rule_id);

  it('binds each entry to one line of the named template', () => {
    for (const entry of suppressions) {
      expect(entry).toMatchObject({ path: 'Stack.template.json', line_end: entry.line_start });
      expect(entry.reason).toMatch(/^A-(ENTROPY-IDENTIFIER|PROTOCOL-CONSTANT)-001: /);
    }
  });

  it('suppresses the reviewed shapes, each under its rule', () => {
    expect(rulesAt(`"${id}": {`)).toStrictEqual(['SECRET-BASE64-HIGH-ENTROPY-STRING']);
    expect(rulesAt(`"${id}"`)).toStrictEqual(['SECRET-BASE64-HIGH-ENTROPY-STRING']);
    expect(rulesAt(`"Ref": "${id}"`)).toStrictEqual(['SECRET-BASE64-HIGH-ENTROPY-STRING']);
    // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
    expect(rulesAt('"LogGroupName": "/aws/lambda/adlytics-dev-ExecutionGraphStuckFlowDetector"'))
      .toStrictEqual(['SECRET-BASE64-HIGH-ENTROPY-STRING']);
    expect(rulesAt(`"SpecDigest": "${'a1'.repeat(32)}",`)).toStrictEqual(['SECRET-HEX-HIGH-ENTROPY-STRING']);
    expect(rulesAt('"ADL_GATEWAY_TOKEN_AUDIENCE": "adlytics-dev-gateway",')).toStrictEqual(['CKV_SECRET_6']);
    // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
    expect(rulesAt('"ClientSecretSource": "EXTERNAL",')).toStrictEqual(['SECRET-SECRET-KEYWORD']);
  });

  it('leaves a credential-shaped value, an unlisted digest key and other fields open', () => {
    for (const needle of [
      // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
      '"API_KEY": "FictionalCredentialValueForThisTestOnly",',
      `"OtherDigest": "${'b2'.repeat(32)}",`,
      '"OTHER_AUDIENCE": "adlytics-dev-gateway"',
      // pragma: allowlist nextline secret (A-TEST-LITERALS-001)
      '"ClientSecret": "EXTERNAL"',
      '"FunctionName": "adlytics-dev-ExecutionGraphStuckFlowDetector",',
    ]) {
      expect(lineOf(needle)).toBeGreaterThan(0);
      expect(rulesAt(needle)).toStrictEqual([]);
    }
  });

  it('appends line-bound entries to a base config that has no path-wide suppression', () => {
    const base = fs.readFileSync(path.join(__dirname, '..', '..', '.ash', 'templates.ash.yaml'), 'utf8');
    expect(base.trimEnd()).toMatch(/\n {2}suppressions:$/);
    const config = templateAshConfig(base, suppressions);
    const entries = config.slice(base.trimEnd().length + 1).trimEnd().split(/\n(?= {4}- path: )/);
    expect(entries).toHaveLength(suppressions.length);
    for (const entry of entries) expect(entry).toMatch(/^ {4}- path: "Stack\.template\.json"\n {6}rule_id: \S+\n {6}line_start: \d+\n {6}line_end: \d+\n {6}reason: ".+"$/);
    expect(templateAshConfig(base, [])).toBe(`${base.trimEnd()} []\n`);
  });

  it('suppresses every rule on the exported templates', () => {
    const rules = new Set(Object.entries(everyTemplate()).flatMap(([name, template]) =>
      secretSuppressions(`${name}.template.json`, `${JSON.stringify(scanSafeTemplate(template), null, 1)}\n`)
        .map((entry) => entry.rule_id)));
    expect([...rules].sort()).toStrictEqual([
      'CKV_SECRET_6', 'SECRET-BASE64-HIGH-ENTROPY-STRING', 'SECRET-HEX-HIGH-ENTROPY-STRING', 'SECRET-SECRET-KEYWORD',
    ]);
  });
});
