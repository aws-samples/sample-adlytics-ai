// SPDX-License-Identifier: MIT-0
/** Explicit physical Iceberg field identity in the generated V2 schema. */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { loadGeneratedV2Storage } from '../lib/constructs/data-lake/generated-tables';

type JsonObject = Record<string, any>;
const SOURCE = path.join(__dirname, '..', '..', 'contracts', 'generated', 'v2-tables.json');

function changed(body: (document: JsonObject) => void): string {
  const document = JSON.parse(fs.readFileSync(SOURCE, 'utf-8')) as JsonObject;
  body(document);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'adlytics-v2-schema-'));
  const target = path.join(directory, 'v2-tables.json');
  fs.writeFileSync(target, JSON.stringify(document));
  return target;
}

describe('generated physical field ids', () => {
  it('loads the governed artifact with every id equal to the Glue ordinal', () => {
    const generated = loadGeneratedV2Storage(SOURCE);
    for (const table of [
      ...generated.sourceAlignedTables,
      ...generated.canonicalCandidateTables,
      ...generated.canonicalCurrentTables,
    ]) {
      expect(table.columns.map((column) => column.fieldId)).toEqual(
        table.columns.map((_column, index) => index + 1),
      );
    }
  });

  it('refuses a field id that differs from the ordinal Glue will assign', () => {
    const target = changed((document) => {
      document.canonicalCandidateTables[0].columns[0].fieldId = 2;
    });

    expect(() => loadGeneratedV2Storage(target)).toThrow(/Glue will assign ordinal 1/);
  });

  it('refuses repeated physical field identities', () => {
    const target = changed((document) => {
      document.canonicalCandidateTables[0].columns[1].fieldId = 1;
    });

    expect(() => loadGeneratedV2Storage(target)).toThrow(/repeats a physical fieldId/);
  });
});
