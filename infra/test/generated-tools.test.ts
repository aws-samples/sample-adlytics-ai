// SPDX-License-Identifier: MIT-0
/**
 * The external-client figure rule the generated tool contract carries (ADR-150).
 *
 * The external MCP gateway appends it to every tool description it serves, so a file without it
 * must refuse at synth, naming the generator, rather than build a gateway that tells an external
 * model nothing about recomputing or totalling the figures.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { GeneratedToolsError, loadGeneratedTools } from '../lib/constructs/agent/generated-tools';

type JsonObject = Record<string, unknown>;

const SOURCE = path.join(__dirname, '..', '..', 'contracts', 'generated', 'tools.json');

function changed(body: (document: JsonObject) => void): string {
  const document = JSON.parse(fs.readFileSync(SOURCE, 'utf-8')) as JsonObject;
  body(document);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'adlytics-tools-'));
  const target = path.join(directory, 'tools.json');
  fs.writeFileSync(target, JSON.stringify(document));
  return target;
}

describe('the external-client rule in the generated tool contract', () => {
  it('returns the committed rule beside the split tools (positive control)', () => {
    const committed = JSON.parse(fs.readFileSync(SOURCE, 'utf-8')) as JsonObject;
    const generated = loadGeneratedTools(SOURCE);
    expect(generated.externalClientRule).toBe(committed.external_client_rule);
    expect(generated.externalClientRule).toContain('metadata.measurement_basis');
    expect(generated.catalog.map((tool) => tool.name)).toEqual([
      'catalog_list_metrics',
      'catalog_describe_metric',
    ]);
    expect(generated.measurement).toHaveLength(3);
  });

  it.each([
    ['missing', (document: JsonObject) => delete document.external_client_rule],
    ['empty', (document: JsonObject) => (document.external_client_rule = '')],
    ['blank', (document: JsonObject) => (document.external_client_rule = '   ')],
    ['not a string', (document: JsonObject) => (document.external_client_rule = ['rule'])],
  ])('refuses a %s rule, naming the generator', (_label, mutate) => {
    const target = changed((document) => {
      mutate(document);
    });
    expect(() => loadGeneratedTools(target)).toThrow(GeneratedToolsError);
    expect(() => loadGeneratedTools(target)).toThrow(
      `${target} has no non-empty \`external_client_rule\` string. ` +
        'Run `make generate-tool-schemas` to produce it.',
    );
  });
});
