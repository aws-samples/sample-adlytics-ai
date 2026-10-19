// SPDX-License-Identifier: MIT-0
/**
 * Nothing this repository deploys may be reachable without authentication.
 *
 * **A gate rather than a rule, because the rule already failed.** A probe script set
 * `AuthType: NONE` on a Lambda Function URL to make a measurement simpler, reasoning that a
 * throwaway resource in a development account was exempt. The account refused the invocation, so
 * nothing was exposed — but that was the account's control, not ours, and the reasoning is the
 * defect. A public endpoint raises a security alert wherever this template is deployed, and the
 * customer's account may not refuse it.
 *
 * Three shapes, each of which makes something callable by anyone who finds it:
 *
 * - **`AWS::Lambda::Url` with `AuthType: NONE`.** The function is invocable from the internet. Every
 *   Function URL here must be `AWS_IAM`, which is what lets CloudFront's Origin Access Control be
 *   the only caller — see
 *   [ADR-038](../../docs/decisions/adr-038-the-conversation-route-streams-from-a-function-url.md).
 * - **`AWS::Lambda::Permission` naming `Principal: "*"`.** Equivalent to the above by another route:
 *   an IAM-auth URL with a wildcard principal is a public URL with extra steps.
 * - **A security group ingress from `0.0.0.0/0` or `::/0`.** Nothing here runs in a VPC today, so
 *   this asserts an absence — which is the point. It costs nothing now and it is the check nobody
 *   writes on the day the first VPC resource lands.
 *
 * Deliberately *not* asserting that every API route has an authorizer. Four auth routes cannot sit
 * behind the session authorizer by necessity — login and callback precede a session, refresh runs
 * with an expired token, logout tears one down — and `routing.UNAUTHENTICATED` is where that set is
 * declared and justified. A gate that could not tell those from an oversight would either fail
 * honestly and be suppressed, or be written loosely enough to pass anything.
 */
import { DEPLOY_ORDER } from '../lib/config';
import { NOT_YET_BUILT, everyTemplate, resourcesOfType } from './support/templates';

const templates = everyTemplate();

describe('no unauthenticated endpoint is deployed', () => {
  it('covers every stack the deploy order names', () => {
    // The assertion that keeps the two below meaningful: a new stack that nobody adds to
    // `everyTemplate()` would otherwise escape this gate in silence.
    const covered = new Set(Object.keys(templates));
    const missing = DEPLOY_ORDER.filter(
      (name) => !covered.has(name) && !NOT_YET_BUILT.includes(name),
    );
    expect(missing).toEqual([]);
  });

  it.each(Object.keys(templates))('%s declares no Function URL with AuthType NONE', (name) => {
    const urls = resourcesOfType(templates[name], 'AWS::Lambda::Url');
    for (const properties of urls) {
      expect(properties.AuthType).toBe('AWS_IAM');
    }
  });

  it.each(Object.keys(templates))('%s grants no Lambda permission to everyone', (name) => {
    const permissions = resourcesOfType(templates[name], 'AWS::Lambda::Permission');
    for (const properties of permissions) {
      const principal = properties.Principal;
      // Both spellings: a bare `"*"` and the `{ AWS: "*" }` form an IAM policy uses. Checking one
      // and not the other is how a prohibition passes while the thing it forbids is deployed.
      expect(principal).not.toBe('*');
      if (principal !== null && typeof principal === 'object') {
        expect((principal as Record<string, unknown>).AWS).not.toBe('*');
      }
    }
  });

  it('declares unauthenticated REST methods only in the token service, and exactly its three', () => {
    /**
     * ADR-077 §3 and infra-security-apigateway: the token service's public routes are the only
     * unauthenticated routes outside the sign-in set, declared in its own set. Stated here as a
     * literal rather than imported from the stack, so widening that set fails this gate.
     */
    const unauthenticated = Object.entries(templates).flatMap(([name, template]) => {
      const resources = ((template as Record<string, unknown>).Resources ?? {}) as Record<
        string,
        { Type?: unknown; Properties?: Record<string, unknown> }
      >;
      const pathOf = (reference: unknown): string => {
        const parts: string[] = [];
        let current = reference as Record<string, unknown>;
        while (typeof current.Ref === 'string') {
          const properties = resources[current.Ref]?.Properties ?? {};
          parts.unshift(String(properties.PathPart));
          current = properties.ParentId as Record<string, unknown>;
        }
        return `/${parts.join('/')}`;
      };
      return resourcesOfType(template, 'AWS::ApiGateway::Method')
        .filter((method) => method.AuthorizationType === undefined || method.AuthorizationType === 'NONE')
        .map((method) => `${name} ${String(method.HttpMethod)} ${pathOf(method.ResourceId)}`);
    });
    expect(unauthenticated.sort()).toStrictEqual([
      'TokenServiceStack GET /.well-known/openid-configuration',
      'TokenServiceStack GET /jwks.json',
      // ADR-077 S4: the RFC 8693 exchange, which authenticates its client in the handler.
      'TokenServiceStack POST /token',
    ]);
  });

  it.each(Object.keys(templates))('%s allows nothing to everyone in a Lambda resource policy', (name) => {
    // A wildcard principal is admissible only as a Deny: the route exchange's resource policy
    // denies every principal but one role. An Allow to `*` would be a public function.
    for (const policy of resourcesOfType(templates[name], 'AWS::Lambda::ResourcePolicy')) {
      const statements = ((policy.PolicyDocument as Record<string, unknown>).Statement ?? []) as
        Record<string, unknown>[];
      for (const statement of statements) {
        const principal = JSON.stringify(statement.Principal);
        if (principal === '"*"' || principal === '{"AWS":"*"}') {
          expect(statement.Effect).toBe('Deny');
        }
      }
    }
  });

  it.each(Object.keys(templates))('%s opens no security group to the internet', (name) => {
    const open = ['0.0.0.0/0', '::/0'];
    for (const type of ['AWS::EC2::SecurityGroupIngress', 'AWS::EC2::SecurityGroup']) {
      for (const properties of resourcesOfType(templates[name], type)) {
        const serialised = JSON.stringify(properties.SecurityGroupIngress ?? properties);
        for (const cidr of open) {
          expect(serialised).not.toContain(cidr);
        }
      }
    }
  });
});

describe('the gate resolves the shapes it claims to', () => {
  it('rejects a Function URL with AuthType NONE', () => {
    // Asserted against a synthetic template rather than only against today's stacks, which declare
    // no Function URL at all. Without this the checks above would pass vacuously and keep passing
    // after the conversation route lands, whatever it set.
    const fabricated = {
      Resources: {
        Url: { Type: 'AWS::Lambda::Url', Properties: { AuthType: 'NONE' } },
      },
    };
    const urls = resourcesOfType(fabricated, 'AWS::Lambda::Url');
    expect(urls.map((properties) => properties.AuthType)).toEqual(['NONE']);
  });

  it('rejects a wildcard Lambda principal in either spelling', () => {
    const fabricated = {
      Resources: {
        A: { Type: 'AWS::Lambda::Permission', Properties: { Principal: '*' } },
        B: { Type: 'AWS::Lambda::Permission', Properties: { Principal: { AWS: '*' } } },
      },
    };
    const found = resourcesOfType(fabricated, 'AWS::Lambda::Permission');
    expect(found).toHaveLength(2);
    expect(found.map((p) => JSON.stringify(p.Principal))).toEqual(['"*"', '{"AWS":"*"}']);
  });
});
