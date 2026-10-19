// SPDX-License-Identifier: MIT-0
/** Strict wire parser for the ADR-076 insight-deep-dive.v1 dialog response. */

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const STATES = new Set(['filled', 'pending', 'unavailable']);

export interface InsightDeepAnalysisV1 {
  readonly candidate_readings: readonly string[];
  readonly cited_figures: readonly string[];
  readonly discriminating_evidence: string;
  readonly what_stands_out: string;
}

export interface InsightDeepDiveV1 {
  readonly absence_reason: string | null;
  readonly artifact_revision: string;
  readonly deep: InsightDeepAnalysisV1 | null;
  readonly deep_prompt_version: string;
  readonly period: { readonly end: string; readonly start: string };
  readonly slot_id: string;
  readonly state: 'filled' | 'pending' | 'unavailable';
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('invalid deep response');
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, fields: readonly string[]): void {
  if (Object.keys(value).length !== fields.length || fields.some((field) => !(field in value))) {
    throw new Error('invalid deep response fields');
  }
}

function nonblank(value: unknown): string {
  if (typeof value !== 'string' || value.trim() === '') throw new Error('invalid deep response text');
  return value;
}

function strings(value: unknown, minimum = 0): readonly string[] {
  if (!Array.isArray(value) || value.length < minimum || !value.every((item) => typeof item === 'string' && item.trim() !== '')) {
    throw new Error('invalid deep response list');
  }
  return value;
}

function analysis(raw: unknown): InsightDeepAnalysisV1 {
  const value = object(raw);
  exact(value, ['what_stands_out', 'candidate_readings', 'discriminating_evidence', 'cited_figures']);
  const readings = strings(value.candidate_readings, 1);
  if (readings.length > 3) throw new Error('invalid deep reading count');
  return {
    what_stands_out: nonblank(value.what_stands_out),
    candidate_readings: readings,
    discriminating_evidence: nonblank(value.discriminating_evidence),
    cited_figures: strings(value.cited_figures),
  };
}

export function parseInsightDeepDiveV1(raw: unknown): InsightDeepDiveV1 {
  const value = object(raw);
  exact(value, ['slot_id', 'state', 'deep', 'absence_reason', 'deep_prompt_version', 'artifact_revision', 'period']);
  const state = nonblank(value.state);
  if (!STATES.has(state)) throw new Error('invalid deep response state');
  const period = object(value.period);
  exact(period, ['start', 'end']);
  const start = nonblank(period.start);
  const end = nonblank(period.end);
  if (!DATE.test(start) || !DATE.test(end) || end < start) throw new Error('invalid deep response period');
  const deep = value.deep === null ? null : analysis(value.deep);
  const absence = value.absence_reason === null ? null : nonblank(value.absence_reason);
  if ((state === 'filled') !== (deep !== null) || (state === 'filled') === (absence !== null)) {
    throw new Error('invalid deep response shape');
  }
  return {
    slot_id: nonblank(value.slot_id),
    state: state as InsightDeepDiveV1['state'],
    deep,
    absence_reason: absence,
    deep_prompt_version: nonblank(value.deep_prompt_version),
    artifact_revision: nonblank(value.artifact_revision),
    period: { start, end },
  };
}
