// SPDX-License-Identifier: MIT-0
/**
 * ADR-078 §5: the CloudFront function on the API behaviours refuses every spelling API Gateway
 * folds into a route, and passes every canonical API path. Spellings API Gateway does not fold, such
 * as a trailing slash or a case change, pass here and are refused by routing. Node evaluates the
 * source to prove the decision; the live deploy and probe prove it runs on CloudFront.
 */
import { CANONICAL_API_PATH_CODE } from '../lib/stacks/frontend-stack';

type Result = { statusCode?: number; uri?: string };
// The deployed source itself, evaluated in Node.
const handler = new Function(`${CANONICAL_API_PATH_CODE}; return handler;`)() as (
  event: { request: { uri: string } },
) => Result;
const decide = (uri: string): Result => handler({ request: { uri } });

describe('the canonical API path check', () => {
  it.each([
    '/api/quality-review/turn',
    '/api/quality-review/turns',
    '/api/me',
    '/api/chat',
    '/api/operations/data-flows/123e4567-e89b-42d3-a456-426614174000/events',
    '/api/presentation/campaign-detail',
    '/api/quality-review/turns/',
    '/api/quality-review/v1.2',
  ])('passes %s unchanged', (uri) => {
    expect(decide(uri)).toEqual({ uri });
  });

  it.each([
    '/api//quality-review/turns',
    '/api/quality-review//turns',
    '/api/quality-review/./turns',
    '/api/quality-review/validation/../turns',
    '/api/quality-review/..',
    '/api/quality-review/.',
    '/api/quality-review%2Fturns',
    '/api/quality-review/turn%73',
    '/api/quality-review/%2e%2e/turns',
    '/api/quality-review\\turns',
    '/validation/../api/quality-review/turn',
    '/api/quality-review/../chat',
  ])('refuses %s with 400', (uri) => {
    expect(decide(uri)).toEqual({ statusCode: 400, statusDescription: 'Bad Request' });
  });
});

/**
 * ADR-078 §1 path-scoped body-size exclusion: the eight path-confusion variants of the exempt
 * create-version route. A doubled slash, an escape, a backslash and a dot segment are refused here;
 * a trailing slash and a case change pass here and fail the WAF exact match, so the 8 KB rule
 * applies, and routing refuses them. A query string is not part of the URI this function sees; the
 * WAF exempt route requires an empty query string.
 */
describe('the exempt create-version route variants', () => {
  const EXACT = '/api/prompt-registry/create-version';

  it('passes the exact route unchanged (positive control)', () => {
    expect(decide(EXACT)).toEqual({ uri: EXACT });
  });

  it.each([
    '/api/prompt-registry/create-version/',
    '/API/prompt-registry/create-version',
    '/api/Prompt-Registry/create-version',
  ])('passes %s, which the WAF exact match and routing refuse', (uri) => {
    expect(decide(uri)).toEqual({ uri });
    expect(uri).not.toBe(EXACT);
  });

  it('passes the exact path with ?v=1, whose query only the WAF empty-query match refuses', () => {
    // CloudFront hands the function the query separately; the URI is the exact path.
    expect(handler({ request: { uri: EXACT, querystring: { v: { value: '1' } } } as never })).toEqual({
      uri: EXACT,
      querystring: { v: { value: '1' } },
    });
  });

  it.each([
    '/api//prompt-registry/create-version',
    '/api/prompt-registry/create%2Dversion',
    '/api/./prompt-registry/create-version',
    '/api/x/../prompt-registry/create-version',
    '/api/prompt-registry\\create-version',
  ])('refuses %s with 400', (uri) => {
    expect(decide(uri)).toEqual({ statusCode: 400, statusDescription: 'Bad Request' });
  });
});
