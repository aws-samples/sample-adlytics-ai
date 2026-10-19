// SPDX-License-Identifier: MIT-0
/**
 * The P6 drain's expected-alarm list names only alarms the app synthesizes (S6-1).
 *
 * `validation/probe_ingestion_traceability_p6.py` holds `EXPECTED_DRAIN_ALARMS`, the stop-rule
 * input for the drain and the post-GRAPH hold window. A name that no stack synthesizes would turn
 * a real alarm into an unexpected one (a false stop) or hide it, so every entry must equal an
 * `AWS::CloudWatch::Alarm` name in the synthesized templates.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { everyTemplate } from './support/templates';

const P6 = path.join(__dirname, '..', '..', 'validation', 'probe_ingestion_traceability_p6.py');

/** Read the string entries of one Python tuple constant (`EXPECTED_DRAIN_ALARMS` and the like). */
function drainAlarms(constant: string): string[] {
  const source = fs.readFileSync(P6, 'utf8');
  const body = new RegExp(`${constant}: Final = \\(([^)]*)\\)`).exec(source)?.[1];
  if (body === undefined) {
    throw new Error(`${constant} not found in the P6 probe`);
  }
  return [...body.matchAll(/"([^"]+)"/g)].map((match) => String(match[1]));
}

function synthesizedAlarmNames(): Set<string> {
  const names = new Set<string>();
  for (const template of Object.values(everyTemplate())) {
    const resources = ((template as Record<string, unknown>).Resources ?? {}) as Record<
      string,
      { Type: string; Properties?: Record<string, unknown> }
    >;
    for (const resource of Object.values(resources)) {
      if (resource.Type === 'AWS::CloudWatch::Alarm') {
        names.add(String(resource.Properties?.AlarmName));
      }
    }
  }
  return names;
}

describe('P6 expected drain alarms (S6-1)', () => {
  it.each(['EXPECTED_DRAIN_ALARMS', 'CONDITIONAL_DRAIN_ALARMS'])('%s names only synthesized alarms', (constant) => {
    const expected = drainAlarms(constant);
    expect(expected.length).toBeGreaterThan(0);
    const synthesized = synthesizedAlarmNames();
    const missing = expected.filter((suffix) => !synthesized.has(`adlytics-dev-${suffix}`));
    expect(missing).toStrictEqual([]);
  });
});
