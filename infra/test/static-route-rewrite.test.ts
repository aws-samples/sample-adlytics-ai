// SPDX-License-Identifier: MIT-0
/**
 * ADR-123: `StaticRouteRewrite` serves only the routes in the committed route allowlist their
 * `index.html`, sends every other extensionless path the exported `/404.html`, and passes a path
 * with an extension unchanged. `NotFoundStatus` sends that page as 404. Node evaluates the code
 * the synthesized template deploys, so these prove the decision; the dev probe proves it runs on
 * CloudFront and records the status the viewer sees.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { FrontendStack, loadRouteAllowlist } from '../lib/stacks/frontend-stack';
import { TEST_CONFIG } from './support/config';
import { ROUTE_ALLOWLIST } from './support/templates';

type Request = { method: string; uri: string };
type Response = { statusCode: number; statusDescription?: string };

const stack = new FrontendStack(new cdk.App(), 'Frontend', {
  config: TEST_CONFIG,
  env: { account: TEST_CONFIG.account, region: TEST_CONFIG.region },
  routeAllowlistPath: ROUTE_ALLOWLIST,
});
const template = Template.fromStack(stack);
const functions = template.findResources('AWS::CloudFront::Function') as Record<
  string,
  { Properties: { FunctionCode: string; FunctionConfig: { Comment: string } } }
>;

/** The deployed source of the one function whose code contains `marker`, evaluated in Node. */
function deployed<T>(marker: string): T {
  const matches = Object.values(functions).filter((fn) => fn.Properties.FunctionCode.includes(marker));
  expect(matches).toHaveLength(1);
  return new Function(`${matches[0]!.Properties.FunctionCode}; return handler;`)() as T;
}

const rewrite = deployed<(event: { request: Request }) => Request>('request.uri = resolve(');
const status = deployed<(event: { request: Request; response: Response }) => Response>(
  'response.statusCode = 404',
);
const served = (uri: string, method = 'GET'): string => rewrite({ request: { method, uri } }).uri;
const sent = (uri: string, statusCode = 200, method = 'GET'): number =>
  status({ request: { method, uri }, response: { statusCode } }).statusCode;

const committed = loadRouteAllowlist(ROUTE_ALLOWLIST);

describe('StaticRouteRewrite (viewer request)', () => {
  it('reads every route from the committed allowlist, root included', () => {
    expect(committed.routes).toContain('/');
    expect(committed.routes.length).toBeGreaterThan(1);
  });

  it.each(committed.routes.map((route) => [route]))('serves manifest route %s its index.html', (route) => {
    expect(served(route)).toBe(`${route}index.html`);
    expect(served(route, 'HEAD')).toBe(`${route}index.html`);
  });

  it.each([
    ['/campaigns', '/campaigns/index.html'],
    ['/retail-health', '/retail-health/index.html'],
    ['/admin/prompts', '/admin/prompts/index.html'],
  ])('serves manifest route %s without its trailing slash from %s', (uri, expected) => {
    expect(served(uri)).toBe(expected);
  });

  it.each([
    '/route-that-does-not-exist',
    '/route-that-does-not-exist/',
    '/campaigns/extra/',
    '/campaign/',
    '/Campaigns/',
    '/admin/',
    '/admin',
    '/a/b/c',
    '/retail-detail/',
    '/channel-performance/',
  ])('serves unknown extensionless path %s the not-found page', (uri) => {
    expect(served(uri)).toBe('/404.html');
    expect(served(uri, 'HEAD')).toBe('/404.html');
  });

  it.each([
    '/404.html',
    '/index.html',
    '/campaigns/index.html',
    '/campaigns/index.txt',
    '/_next/static/chunks/app.js',
    '/theme-preference.9fe8ccc10b308643.js',
    '/icon.svg',
    '/missing.png',
  ])('passes %s, a path with an extension, unchanged', (uri) => {
    expect(served(uri)).toBe(uri);
  });

  it.each(['POST', 'PUT', 'DELETE', 'OPTIONS'])('leaves a %s request unchanged', (method) => {
    expect(served('/route-that-does-not-exist', method)).toBe('/route-that-does-not-exist');
    expect(served('/campaigns/', method)).toBe('/campaigns/');
  });
});

describe('NotFoundStatus (viewer response, ADR-123 decision 3)', () => {
  it('sends the rewritten not-found page as 404, keeping the body', () => {
    const response = status({
      request: { method: 'GET', uri: '/404.html' },
      response: { statusCode: 200 },
    });
    // No body field: CloudFront returns the cached or origin body unchanged.
    expect(response).toEqual({ statusCode: 404, statusDescription: 'Not Found' });
  });

  it.each(['/route-that-does-not-exist', '/campaigns/extra/', '/a/b/c'])(
    'sends 404 when the event carries the original unknown path %s',
    (uri) => {
      expect(sent(uri)).toBe(404);
      expect(sent(uri, 200, 'HEAD')).toBe(404);
    },
  );

  it.each([
    ...committed.routes,
    ...committed.routes.map((route) => `${route}index.html`),
    '/campaigns',
    '/_next/static/chunks/app.js',
  ])('leaves %s at 200', (uri) => {
    expect(sent(uri)).toBe(200);
  });

  it.each([
    ['/404.html', 304],
    ['/route-that-does-not-exist', 304],
    ['/missing.png', 403],
    ['/campaigns/', 403],
  ])('leaves %s with status %s unchanged', (uri, statusCode) => {
    expect(sent(uri, statusCode)).toBe(statusCode);
  });
});

describe('the distribution wiring', () => {
  it('associates the rewrite on the request and the status on the response of the default behaviour', () => {
    const ids = Object.fromEntries(
      Object.entries(functions).map(([id, fn]) => [fn.Properties.FunctionConfig.Comment, id]),
    );
    const distribution = Object.values(template.findResources('AWS::CloudFront::Distribution'))[0] as {
      Properties: {
        DistributionConfig: {
          CustomErrorResponses?: unknown;
          DefaultCacheBehavior: { FunctionAssociations: unknown[] };
        };
      };
    };
    const config = distribution.Properties.DistributionConfig;
    expect(config.DefaultCacheBehavior.FunctionAssociations).toEqual([
      {
        EventType: 'viewer-request',
        FunctionARN: {
          'Fn::GetAtt': [
            ids['Serve manifest routes their index document and other paths the not-found page.'],
            'FunctionARN',
          ],
        },
      },
      {
        EventType: 'viewer-response',
        FunctionARN: {
          'Fn::GetAtt': [ids['Send the not-found page with status 404 (ADR-123 decision 3).'], 'FunctionARN'],
        },
      },
    ]);
    // ADR-123 decision 4: still no distribution-wide error responses.
    expect(config.CustomErrorResponses).toBeUndefined();
  });
});

describe('loadRouteAllowlist', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'route-allowlist-'));
  afterAll(() => fs.rmSync(directory, { force: true, recursive: true }));
  const written = (name: string, value: unknown): string => {
    const file = path.join(directory, `${name}.json`);
    fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value));
    return file;
  };
  const valid = { not_found_page: '/404.html', routes: ['/', '/campaigns/'], version: 1 };

  it('reads a valid allowlist', () => {
    expect(loadRouteAllowlist(written('valid', valid))).toEqual({ routes: ['/', '/campaigns/'] });
  });

  it.each([
    ['unreadable JSON', '{', /Could not read contracts\/generated\/route-allowlist.json/],
    ['another version', { ...valid, version: 2 }, /requires exactly version 1/],
    ['an extra field', { ...valid, extra: true }, /requires exactly version 1/],
    ['another not-found page', { ...valid, not_found_page: '/index.html' }, /must name \/404.html/],
    ['a route without its trailing slash', { ...valid, routes: ['/', '/campaigns'] }, /canonical/],
    ['a dot segment', { ...valid, routes: ['/', '/../'] }, /canonical/],
    ['a duplicate route', { ...valid, routes: ['/', '/campaigns/', '/campaigns/'] }, /unique/],
    ['no root', { ...valid, routes: ['/campaigns/'] }, /must include \//],
  ])('refuses %s', (name, value, message) => {
    expect(() => loadRouteAllowlist(written(name.replace(/\W+/g, '-'), value))).toThrow(message);
  });
});
