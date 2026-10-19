// SPDX-License-Identifier: MIT-0
/**
 * Every resource name a validation probe reads is one some stack synthesizes (S6B-1).
 *
 * meta-aws-operations: a runtime name is derived from its owning configuration, never guessed. A
 * guessed alarm, queue or function name does not fail the probe; it turns a real resource into an
 * unexpected one, or waits on one that never exists, and the probe still reports a verdict. The
 * collector (`support/probe-resource-names.ts`) reads every `validation/**\/*.py` file as text and
 * this test fails listing `path:line suffix` for each fully literal candidate absent from the
 * synthesized `adlytics-<env>-` name universe of `everyTemplate()`. Dynamic candidates (a
 * placeholder in the suffix, a prefix ending in `-`, a non-literal constant entry) are counted and
 * printed, and `# probe-owned-resource: <reason>` exemptions are listed with their reasons. The
 * checked and dynamic sets must equal the committed `support/probe-resource-inventory.ts`, so a
 * name that turns dynamic or stops being collected fails rather than silently leaving coverage.
 *
 * `p6-expected-alarms.test.ts` stays: it pins one probe's two tuples exactly, which this broader
 * collector also covers.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  Collected,
  collectCandidates,
  inventoryDrift,
  inventoryOf,
  missingNames,
  synthesizedNameSuffixes,
} from './support/probe-resource-names';
import { PROBE_NAME_INVENTORY } from './support/probe-resource-inventory';
import { TEST_CONFIG } from './support/config';
import { everyTemplate } from './support/templates';

const REPOSITORY = path.join(__dirname, '..', '..');
const VALIDATION = path.join(REPOSITORY, 'validation');

function pythonFiles(directory: string): string[] {
  return fs
    .readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) => {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) return entry.name === '__pycache__' ? [] : pythonFiles(full);
      return entry.name.endsWith('.py') ? [full] : [];
    })
    .sort();
}

function collectAll(): Collected[] {
  return pythonFiles(VALIDATION).map((file) =>
    collectCandidates(path.relative(REPOSITORY, file), fs.readFileSync(file, 'utf8')),
  );
}

describe('probe resource names (S6B-1)', () => {
  const universe = synthesizedNameSuffixes(everyTemplate(), TEST_CONFIG.envName);

  it('reads a name universe holding a known alarm, queue, FIFO queue and token identifier', () => {
    expect(universe.size).toBeGreaterThan(100);
    const known = ['ingestion-oldest', 'ingestion-dlq', 'semantic-write.fifo', 'harness', 'agent'];
    expect(known.filter((name) => !universe.has(name))).toStrictEqual([]);
  });

  it('every literal probe resource name is synthesized by some stack', () => {
    const collected = collectAll();
    const candidates = collected.flatMap((file) => file.candidates);
    const dynamic = collected.flatMap((file) => file.dynamic);
    const exempt = collected.flatMap((file) => file.exempt);
    // The counts are reported; the next test holds both sets to the committed inventory.
    console.log(
      [
        `probe resource names: ${candidates.length} literal candidates checked,`,
        `${dynamic.length} dynamic candidates skipped, ${exempt.length} probe-owned exemptions`,
        ...exempt.map((item) => `  probe-owned ${item.path}:${item.line} ${item.suffix} (${item.reason})`),
      ].join('\n'),
    );
    expect(collected.flatMap((file) => file.staleMarkers)).toStrictEqual([]);
    expect(collected.flatMap((file) => missingNames(file, universe))).toStrictEqual([]);
  });

  it('collects exactly the committed checked and dynamic inventory', () => {
    // A drifted entry is listed; edit support/probe-resource-inventory.ts in the same change.
    expect(inventoryDrift(PROBE_NAME_INVENTORY, inventoryOf(collectAll()))).toStrictEqual([]);
  });

  describe('positive control', () => {
    const fixture = [
      'import uuid',
      'NOPE = f"adlytics-{env}-nope-alarm"',
      'KNOWN = f"adlytics-{environment}-ingestion-oldest"',
      'LITERAL_ENV = "adlytics-dev-ingestion-dlq"',
      'DYNAMIC = f"adlytics-{env}-calibration-{uuid.uuid4()}"',
      'PREFIX = f"adlytics-{env}-ingestion-"',
      'FIXTURE_ALARMS: Final = (',
      '    "ingestion-oldest",  # a comment, with a comma',
      '    "nope-two",',
      '    *("generated-" + x for x in ("a", "b")),',
      ')',
      'OWN_QUEUES = [',
      '    # probe-owned-resource: the fixture probe creates this queue and deletes it',
      '    "fixture-scratch",',
      ']',
      'COUNTER = f"adlytics-{env}-ingestion-dlq:visible"',
      'AUDIENCE = "adlytics-{environment}-harness"',
      'GROUP = f"/aws/lambda/adlytics-{env}-TableWriter"',
      'NOPE_GROUP = f"/aws/lambda/adlytics-{env}-NopeFunction"',
      'NOPE_ROLE = f"arn:aws:iam::{account}:role/adlytics-{env}-nope-role"',
      'WAF = f"aws-waf-logs-adlytics-{env}-edge"',
      '',
    ].join('\n');

    it('fails a non-existent suffix from either rule and passes an existing one', () => {
      const collected = collectCandidates('fixture.py', fixture);
      expect(missingNames(collected, universe)).toStrictEqual([
        'fixture.py:2 nope-alarm',
        'fixture.py:9 nope-two',
        'fixture.py:19 NopeFunction',
        'fixture.py:20 nope-role',
      ]);
      expect(collected.candidates.map((item) => item.suffix).sort()).toStrictEqual([
        'NopeFunction',
        'TableWriter',
        'edge',
        'harness',
        'ingestion-dlq',
        'ingestion-dlq',
        'ingestion-oldest',
        'ingestion-oldest',
        'nope-alarm',
        'nope-role',
        'nope-two',
      ]);
    });

    it('counts placeholder, prefix and non-literal entries as dynamic', () => {
      const collected = collectCandidates('fixture.py', fixture);
      expect(collected.dynamic.map((item) => `${item.line} ${item.suffix}`)).toStrictEqual([
        '5 calibration-{uuid.uuid4()}',
        '6 ingestion-',
        '10 *("generated-" + x for x in ("a", "b"))',
      ]);
    });

    it('lists a probe-owned exemption with its reason and checks nothing else under it', () => {
      const collected = collectCandidates('fixture.py', fixture);
      expect(collected.exempt).toStrictEqual([
        {
          path: 'fixture.py',
          line: 14,
          suffix: 'fixture-scratch',
          reason: 'the fixture probe creates this queue and deletes it',
        },
      ]);
      expect(collected.staleMarkers).toStrictEqual([]);
    });

    it('fails a checked name that turns dynamic or stops being collected, and passes no change', () => {
      const before = 'KNOWN = f"adlytics-{env}-ingestion-oldest"\nDLQ = "adlytics-dev-ingestion-dlq"\n';
      const committed = inventoryOf([collectCandidates('fixture.py', before)]);
      // The same name built from a placeholder: the literal check can no longer reach it.
      const turnedDynamic = before.replace('ingestion-oldest', 'ingestion-{kind}');
      // A collector or probe change that no longer yields the name at all.
      const dropped = before.replace('"adlytics-dev-ingestion-dlq"', 'queue_name("ingestion-dlq")');

      expect(inventoryDrift(committed, committed)).toStrictEqual([]);
      expect(
        inventoryDrift(committed, inventoryOf([collectCandidates('fixture.py', before)])),
      ).toStrictEqual([]);
      expect(
        inventoryDrift(committed, inventoryOf([collectCandidates('fixture.py', turnedDynamic)])),
      ).toStrictEqual([
        'no longer checked: fixture.py "ingestion-oldest"',
        'newly dynamic: fixture.py "ingestion-{kind}"',
      ]);
      expect(
        inventoryDrift(committed, inventoryOf([collectCandidates('fixture.py', dropped)])),
      ).toStrictEqual(['no longer checked: fixture.py "ingestion-dlq"']);
    });

    it('counts a duplicated entry, so one of two identical names cannot vanish', () => {
      const twice = 'A = "adlytics-dev-ingestion-dlq"\nB = "adlytics-dev-ingestion-dlq"\n';
      const committed = inventoryOf([collectCandidates('fixture.py', twice)]);
      const once = inventoryOf([collectCandidates('fixture.py', 'A = "adlytics-dev-ingestion-dlq"\n')]);

      expect(committed.checked).toHaveLength(2);
      expect(inventoryDrift(committed, once)).toStrictEqual([
        'no longer checked: fixture.py "ingestion-dlq"',
      ]);
    });

    it('reads a multiline literal: a missing name fails and a synthesized one passes', () => {
      const multiline = [
        'NOPE = f"""',
        '    alarm: adlytics-{env}-nope-multiline-alarm',
        '"""',
        'KNOWN = f"""',
        '    alarm: adlytics-{env}-ingestion-oldest',
        '"""',
        'NOPE_FIELD = f"adlytics-{',
        '    env',
        '}-nope-field-alarm"',
        'KNOWN_FIELD = f"adlytics-{',
        '    env',
        '}-ingestion-dlq"',
        '# a comment is not a read: "adlytics-dev-nope-comment"',
        '',
      ].join('\n');
      const collected = collectCandidates('fixture.py', multiline);

      expect(missingNames(collected, universe)).toStrictEqual([
        'fixture.py:2 nope-multiline-alarm',
        'fixture.py:7 nope-field-alarm',
      ]);
      expect(collected.candidates.map((item) => `${item.line} ${item.suffix}`)).toStrictEqual([
        '2 nope-multiline-alarm',
        '5 ingestion-oldest',
        '7 nope-field-alarm',
        '10 ingestion-dlq',
      ]);
      expect(collected.dynamic).toStrictEqual([]);
    });

    it('reads a nested replacement field: a missing name fails and a synthesized one passes', () => {
      const nested = [
        `NOPE = f"adlytics-{ {'selected': env}['selected'] }-nope-nested-alarm"`,
        `KNOWN = f"adlytics-{ {'selected': env}['selected'] }-ingestion-oldest"`,
        `NOPE_BRACE = f"adlytics-{ {'}': env}['}'] }-nope-brace-alarm"`,
        `KNOWN_BRACE = f"adlytics-{ {'}': env}['}'] }-ingestion-dlq"`,
        `LITERAL_BRACES = f"adlytics-{{env}}-not-a-placeholder"`,
        '',
      ].join('\n');
      const collected = collectCandidates('fixture.py', nested);

      expect(missingNames(collected, universe)).toStrictEqual([
        'fixture.py:1 nope-nested-alarm',
        'fixture.py:3 nope-brace-alarm',
      ]);
      expect(collected.candidates.map((item) => `${item.line} ${item.suffix}`)).toStrictEqual([
        '1 nope-nested-alarm',
        '2 ingestion-oldest',
        '3 nope-brace-alarm',
        '4 ingestion-dlq',
      ]);
      expect(collected.dynamic).toStrictEqual([]);
    });

    it('reads past a comment in a replacement field: a missing name fails and a synthesized one passes', () => {
      // Python 3.14 evaluates NOPE to adlytics-dev-nope-comment-field-alarm: the `}` after `#` is
      // comment text, so the field closes at the brace on the next line. In a format spec `#` is
      // text (`{n:#x}`), not a comment.
      const commented = [
        'NOPE = f"""adlytics-{env # } comment',
        '}-nope-comment-field-alarm"""',
        'KNOWN = f"""adlytics-{env # } comment',
        '}-ingestion-oldest"""',
        'NOPE_SINGLE = f"adlytics-{env # } comment',
        '}-nope-single-comment-alarm"',
        'KNOWN_SINGLE = f"adlytics-{env # } comment',
        '}-ingestion-dlq"',
        'SPEC = f"adlytics-{n:#x}-nope-spec-alarm"',
        '',
      ].join('\n');
      const collected = collectCandidates('fixture.py', commented);

      expect(missingNames(collected, universe)).toStrictEqual([
        'fixture.py:1 nope-comment-field-alarm',
        'fixture.py:5 nope-single-comment-alarm',
        'fixture.py:9 nope-spec-alarm',
      ]);
      expect(collected).toStrictEqual({
        candidates: [
          { path: 'fixture.py', line: 1, suffix: 'nope-comment-field-alarm' },
          { path: 'fixture.py', line: 3, suffix: 'ingestion-oldest' },
          { path: 'fixture.py', line: 5, suffix: 'nope-single-comment-alarm' },
          { path: 'fixture.py', line: 7, suffix: 'ingestion-dlq' },
          { path: 'fixture.py', line: 9, suffix: 'nope-spec-alarm' },
        ],
        dynamic: [],
        exempt: [],
        staleMarkers: [],
      });
    });

    it('counts a placeholder that never closes as dynamic rather than dropping it', () => {
      const collected = collectCandidates('fixture.py', 'X = "adlytics-{env-nope-alarm"\n');
      expect(collected.candidates).toStrictEqual([]);
      expect(collected.dynamic.map((item) => `${item.line} ${item.suffix}`)).toStrictEqual([
        '1 {env-nope-alarm',
      ]);
    });

    it('refuses a literal it cannot follow rather than skipping the name in it', () => {
      const refusal = (source: string): string => {
        try {
          collectCandidates('fixture.py', source);
        } catch (error) {
          return (error as Error).message;
        }
        return 'no refusal';
      };
      const unread = 'unsupported or unterminated string literal; the probe-name collector cannot read it';

      expect(refusal('X = 1\nY = "adlytics-{env}-nope\n"\n')).toBe(`fixture.py:2 ${unread}`);
      expect(refusal('Y = """adlytics-{env}-nope\n')).toBe(`fixture.py:1 ${unread}`);
      // The same names, terminated: read, not refused.
      const single = 'X = 1\nY = "adlytics-{env}-nope"\n';
      const triple = 'Y = """adlytics-{env}-nope\n"""\n';
      expect(refusal(single)).toBe('no refusal');
      expect(refusal(triple)).toBe('no refusal');
      expect(collectCandidates('fixture.py', single).candidates).toStrictEqual([
        { path: 'fixture.py', line: 2, suffix: 'nope' },
      ]);
      expect(collectCandidates('fixture.py', triple).candidates).toStrictEqual([
        { path: 'fixture.py', line: 1, suffix: 'nope' },
      ]);
    });

    it('refuses an exemption marker without a reason', () => {
      expect(() =>
        collectCandidates('fixture.py', '# probe-owned-resource:\nX = "adlytics-dev-own"\n'),
      ).toThrow('fixture.py:2 probe-owned-resource marker without a reason');
    });

    it('reports a marker that exempts nothing as stale', () => {
      const collected = collectCandidates(
        'fixture.py',
        '# probe-owned-resource: nothing below it\n\nX = "adlytics-dev-ingestion-oldest"\n',
      );
      expect(collected.staleMarkers).toStrictEqual(['fixture.py:1']);
    });
  });
});
