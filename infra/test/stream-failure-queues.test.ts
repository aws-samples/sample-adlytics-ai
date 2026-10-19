// SPDX-License-Identifier: MIT-0
/**
 * The disposition CLI's consumer set names exactly the synthesized stream failure queues (D2-5).
 *
 * `attempts_failures.py` holds `STREAM_FAILURE_QUEUE_SUFFIXES` and `STREAM_FAILURE_QUEUE_STACKS`.
 * A suffix no stack synthesizes would leave the operator unable to drain a paging queue; a
 * synthesized failure queue with no suffix would have no disposition at all; two queues with one
 * name would leave the CLI's stack-and-suffix resolution ambiguous. Every entry and every queue
 * is kept as an occurrence, so a duplicate is counted rather than overwritten.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { everyTemplate } from './support/templates';

const CLI = path.join(
  __dirname, '..', '..', 'src', 'adapters', 'jobs', 'operational_status', 'attempts_failures.py',
);
const PREFIX = 'adlytics-dev';

/** Every `"key": "value"` entry of one Python mapping constant, duplicates kept. */
function pythonEntries(constant: string): [string, string][] {
  const source = fs.readFileSync(CLI, 'utf8');
  const body = new RegExp(`${constant}: Final\\[Mapping\\[str, str\\]\\] = \\{([^}]*)\\}`).exec(source)?.[1];
  if (body === undefined) {
    throw new Error(`${constant} not found in attempts_failures.py`);
  }
  return [...body.matchAll(/"([^"]+)":\s*"([^"]+)"/g)].map((m) => [String(m[1]), String(m[2])]);
}

type Resource = { Type: string; Properties?: Record<string, unknown> };

/** One synthesized on-failure destination queue: its name and the stack (template key) owning it. */
interface FailureQueue {
  readonly name: string;
  readonly stackKey: string;
}

/** Every distinct on-failure destination queue resource, one occurrence per resource. */
function synthesizedFailureQueues(): FailureQueue[] {
  const found = new Map<string, FailureQueue>();
  for (const [stackKey, template] of Object.entries(everyTemplate())) {
    const resources = ((template as Record<string, unknown>).Resources ?? {}) as Record<string, Resource>;
    for (const resource of Object.values(resources)) {
      if (resource.Type !== 'AWS::Lambda::EventSourceMapping') continue;
      const destination = (resource.Properties?.DestinationConfig as
        { OnFailure?: { Destination?: { 'Fn::GetAtt'?: [string, string] } } } | undefined)
        ?.OnFailure?.Destination?.['Fn::GetAtt'];
      if (destination === undefined) continue;
      const queue = resources[destination[0]];
      expect(queue?.Type).toBe('AWS::SQS::Queue');
      found.set(`${stackKey}/${destination[0]}`, { name: String(queue?.Properties?.QueueName), stackKey });
    }
  }
  return [...found.values()];
}

/** Template keys are the stack class names; the CLI names the deployed stack prefix. */
function cliStackName(stackKey: string): string {
  return `Adlytics-${stackKey.replace(/Stack$/, '')}`;
}

/** Every way the CLI roster and the synthesized failure queues disagree; empty when they match. */
function rosterViolations(
  suffixes: readonly [string, string][],
  stacks: readonly [string, string][],
  queues: readonly FailureQueue[],
): string[] {
  const found: string[] = [];
  const count = <T>(values: readonly T[], value: T): number => values.filter((v) => v === value).length;
  const consumers = suffixes.map(([consumer]) => consumer);
  for (const consumer of new Set(consumers)) {
    if (count(consumers, consumer) !== 1) found.push(`${consumer}: listed ${count(consumers, consumer)} times`);
  }
  const suffixValues = suffixes.map(([, suffix]) => suffix);
  for (const suffix of new Set(suffixValues)) {
    if (count(suffixValues, suffix) !== 1) found.push(`${suffix}: suffix of ${count(suffixValues, suffix)} entries`);
  }
  if (JSON.stringify(stacks.map(([consumer]) => consumer)) !== JSON.stringify(consumers)) {
    found.push('the stack and suffix mappings name different consumers');
  }
  const owningStack = new Map(stacks);
  for (const [consumer, suffix] of suffixes) {
    const name = `${PREFIX}${suffix}`;
    const owners = queues.filter((queue) => queue.name === name).map((queue) => cliStackName(queue.stackKey));
    if (owners.length !== 1) {
      found.push(`${consumer}: ${name} is synthesized ${owners.length} times (${owners.join(', ')})`);
    } else if (owners[0] !== owningStack.get(consumer)) {
      found.push(`${consumer}: ${name} is in ${owners[0]}, the CLI names ${owningStack.get(consumer)}`);
    }
  }
  const named = new Set(suffixValues.map((suffix) => `${PREFIX}${suffix}`));
  for (const queue of queues) {
    if (!named.has(queue.name)) found.push(`${queue.name} (${queue.stackKey}) has no CLI suffix`);
  }
  return found;
}

describe('stream failure queues (D2-5)', () => {
  const suffixes = pythonEntries('STREAM_FAILURE_QUEUE_SUFFIXES');
  const stacks = pythonEntries('STREAM_FAILURE_QUEUE_STACKS');
  const synthesized = synthesizedFailureQueues();

  it('names six consumers', () => {
    expect(suffixes).toHaveLength(6);
  });

  it('maps every consumer to exactly one synthesized failure queue in its named stack, and back', () => {
    expect(synthesized).toHaveLength(6);
    expect(rosterViolations(suffixes, stacks, synthesized)).toStrictEqual([]);
  });

  it('counts a duplicated failure-queue name rather than collapsing it', () => {
    const [first] = synthesized;
    if (first === undefined) throw new Error('no synthesized failure queue');
    const duplicate = { name: first.name, stackKey: 'PublisherStack' };
    const owners = [cliStackName(first.stackKey), 'Adlytics-Publisher'].join(', ');
    const consumer = suffixes.find(([, suffix]) => `${PREFIX}${suffix}` === first.name)?.[0];
    expect(rosterViolations(suffixes, stacks, [...synthesized, duplicate])).toStrictEqual([
      `${String(consumer)}: ${first.name} is synthesized 2 times (${owners})`,
    ]);
  });

  it('counts a duplicated CLI entry rather than collapsing it', () => {
    const [suffix] = suffixes;
    const [stack] = stacks;
    if (suffix === undefined || stack === undefined) throw new Error('no CLI entry');
    expect(rosterViolations([...suffixes, suffix], [...stacks, stack], synthesized)).toStrictEqual([
      `${suffix[0]}: listed 2 times`,
      `${suffix[1]}: suffix of 2 entries`,
    ]);
  });
});
