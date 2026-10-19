// SPDX-License-Identifier: MIT-0

import completeFixture from './audiences-complete-v2.json';

type Json = Record<string, unknown>;

export function audiencesFixture(outcome: 'complete' | 'empty' | 'insufficient' = 'complete'): Json {
  const fixture = structuredClone(completeFixture) as Json;
  fixture.outcome = outcome;
  fixture.starter_questions = outcome === 'complete' ? fixture.starter_questions : [];
  const market = (fixture.markets as Json[])[0]!;
  market.outcome = outcome;
  return fixture;
}
