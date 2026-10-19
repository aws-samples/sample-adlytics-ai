// SPDX-License-Identifier: MIT-0
/**
 * No synthesised policy may grant an AgentCore action that bypasses the repository Runtime.
 *
 * `InvokeAgentRuntimeCommand` and `InvokeAgentRuntimeCommandShell` execute shell commands
 * directly inside an AgentCore Runtime session's microVM — same container, same filesystem, no
 * model reasoning. They exist for agentic coding, where running tests and inspecting a workspace
 * deterministically is the point. Here they are the opposite of the point: this platform's
 * whole correctness story is that every number a user sees came from a tool that computed it,
 * and a caller with a shell in the Runtime session is outside that guarantee entirely.
 *
 * **The capability cannot be removed, only the permission withheld.** AgentCore exposes these
 * command APIs separately from normal Runtime invocation. Withholding them is the control, so it
 * needs a gate rather than a convention.
 *
 * **Why a gate and not just a Deny on the one role that matters.** The realistic mistake is not
 * somebody deliberately allowing shell execution. It is that `bedrock-agentcore:Invoke*` looks
 * like a tidy way to cover a normal Runtime call and silently grants command execution as well.
 * A wildcard is how this arrives, so the check has to resolve wildcards rather than compare
 * strings.
 *
 * A permissions boundary was considered and declined: a role carries exactly one, and this
 * repository deploys into the customer's account where their platform team may already mandate
 * it. Out-of-band privilege changes are their governance problem and an SCP is their instrument.
 * This gate covers everything *we* deploy, which is the half that is ours.
 */



import { DEPLOY_ORDER } from '../lib/config';
import { NOT_YET_BUILT, everyTemplate } from './support/templates';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import {
  type Statement,
  actionsOf,
  grants,
  invokeSourceGrants,
  policyDocuments,
} from './support/invoke-sources';




/**
 * The actions no policy may grant, by their real names.
 *
 * Stated concretely rather than as a prefix pattern because the matcher below resolves a
 * *granted* wildcard against these names. `bedrock-agentcore:Invoke*` has to be caught, and it
 * only is if there is a literal name for it to match.
 */
const FORBIDDEN_ACTIONS = [
  'bedrock-agentcore:InvokeAgentRuntimeCommand',
  'bedrock-agentcore:InvokeAgentRuntimeCommandShell',
];

/*
 * `NOT_YET_BUILT` and `everyTemplate()` used to be declared here. They are now imported from
 * `support/templates`, which is where they were extracted to when a second cross-stack gate needed
 * the same coverage — and which claimed in its own docstring that "a new stack has to be added once
 * and both gates pick it up". That was not true: this file kept a byte-for-byte copy of the builder,
 * so a new stack had to be added twice and the second omission would have been silent. Found while
 * adding `IngestionStack`.
 */

/*
 * `Statement`, `policyDocuments`, `actionsOf` and `grants` live in `support/invoke-sources`, which
 * the invocation-source gate below shares with this one.
 */
/** Which forbidden actions an `Allow` statement reaches, wildcards resolved. */
function forbiddenGrants(statement: Statement): string[] {
  if (statement.Effect !== 'Allow') return [];
  return FORBIDDEN_ACTIONS.filter((action) =>
    actionsOf(statement.Action).some((pattern) => grants(pattern, action)),
  );
}

describe('forbidden AgentCore actions', () => {
  const built = everyTemplate();

  it.each(Object.keys(built))('%s grants no direct command execution', (name) => {
    const offending = policyDocuments(built[name])
      .flat()
      .flatMap((statement) =>
        forbiddenGrants(statement).map((action) => ({
          action,
          granted: actionsOf(statement.Action),
        })),
      );

    expect(offending).toEqual([]);
  });

  /**
   * An `Allow` paired with `NotAction` allows everything it does not list, so it grants command
   * execution unless the list happens to name it. Refused outright rather than inspected: it
   * cannot be shown safe by reading it, and nothing in this repository has a reason to use one.
   */
  it.each(Object.keys(built))('%s uses no Allow/NotAction statement', (name) => {
    const inverted = policyDocuments(built[name])
      .flat()
      .filter((statement) => statement.Effect === 'Allow' && statement.NotAction !== undefined);

    expect(inverted).toEqual([]);
  });

  /**
   * The managed Harness explicitly denies direct Runtime commands alongside its allowed operations.
   *
   * The broad no-grant gate above proves the current templates do not expose commands. This denial
   * protects the Harness role if a future policy is widened while keeping its managed tool path intact.
   */
  it('HarnessStack explicitly denies direct Runtime commands', () => {
    const denied = FORBIDDEN_ACTIONS.filter((action) =>
      policyDocuments(built.HarnessStack)
        .flat()
        .some(
          (entry) =>
            entry.Effect === 'Deny' &&
            actionsOf(entry.Action).some((pattern) => grants(pattern, action)),
        ),
    );

    expect(denied).toEqual(FORBIDDEN_ACTIONS);
  });

  it('covers every stack the deploy order names', () => {
    const covered = new Set([...Object.keys(built), ...NOT_YET_BUILT]);
    const uncovered = DEPLOY_ORDER.filter((name) => !covered.has(name));

    expect(uncovered).toEqual([]);
  });

  /**
   * The assertions above all pass if `policyDocuments` finds nothing at all.
   *
   * That is the failure this gate is most likely to suffer and least likely to reveal: a walk
   * that silently returns an empty list reports every stack clean, forever, and looks like a
   * green suite. So the walk is required to reach a statement whose presence is independently
   * known — `AgentStack` grants the measurement function `athena:StartQueryExecution` in an
   * inline role policy, which is the nested shape the walk exists to reach.
   */
  it('actually reaches inline role policies', () => {
    const actions = policyDocuments(built.AgentStack)
      .flat()
      .flatMap((statement) => actionsOf(statement.Action));

    expect(actions).toContain('athena:StartQueryExecution');
    expect(actions).toContain('dynamodb:GetItem');
  });
});

/**
 * The wildcard resolution, against the patterns a mistake would actually be written as.
 *
 * Kept separate from the template assertions because this is the part with no margin: if
 * `grants` under-matches, the gate above passes on a template that hands out shell access. The
 * `Invoke*` and `InvokeAgentRuntime*` are realistic accidental wildcard grants and must both
 * include command execution in the matcher.
 */
describe('wildcard resolution', () => {
  const command = 'bedrock-agentcore:InvokeAgentRuntimeCommand';
  const shell = 'bedrock-agentcore:InvokeAgentRuntimeCommandShell';

  it.each([
    '*',
    'bedrock-agentcore:*',
    'bedrock-agentcore:Invoke*',
    'bedrock-agentcore:InvokeAgentRuntime*',
    'bedrock-agentcore:InvokeAgentRuntimeCommand*',
    'bedrock-agentcore:invokeagentruntimecommand',
  ])('%s is caught', (pattern) => {
    expect(grants(pattern, command)).toBe(true);
  });

  it.each(['*', 'bedrock-agentcore:*', 'bedrock-agentcore:Invoke*'])(
    '%s also reaches the shell variant',
    (pattern) => {
      expect(grants(pattern, shell)).toBe(true);
    },
  );

  it.each([
    'bedrock-agentcore:InvokeAgentRuntime',
    'bedrock-agentcore:InvokeGateway',
    'athena:StartQueryExecution',
  ])('%s is permitted', (pattern) => {
    expect(grants(pattern, command)).toBe(false);
    expect(grants(pattern, shell)).toBe(false);
  });

  /**
   * `InvokeAgentRuntime` is a prefix of `InvokeAgentRuntimeCommand`, so a matcher doing substring
   * or `startsWith` comparison would falsely classify ordinary Runtime invocation as command
   * execution. Asserted in both directions for that reason.
   */
  it('does not treat a prefix as a wildcard', () => {
    expect(grants('bedrock-agentcore:InvokeAgentRuntime', command)).toBe(false);
    expect(grants('bedrock-agentcore:InvokeAgentRuntimeCommand', shell)).toBe(false);
  });
});

describe('invocation sources (H-EVALUATION-RESULT-001 residual 1)', () => {
  function probe(actions: string[], onFunction = false): { id: string; lines: string[] } {
    const stack = new cdk.Stack(new cdk.App(), 'Probe', {
      env: { account: '111122223333', region: 'eu-west-1' },
    });
    const role = new iam.Role(stack, 'Role', { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
    const target = onFunction
      ? new lambda.Function(stack, 'Target', {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: 'index.handler',
        code: lambda.Code.fromInline('def handler(event, context): pass'),
      }).functionArn
      : '*';
    role.addToPolicy(new iam.PolicyStatement({ actions, resources: [target] }));
    const template = Template.fromStack(stack).toJSON() as Record<string, unknown>;
    const id = Object.entries(template.Resources as Record<string, { Type?: unknown; Properties?: Record<string, unknown> }>)
      .filter(([, resource]) => resource.Type === 'AWS::IAM::Policy'
        && JSON.stringify(resource.Properties?.Roles).includes('Role'))
      .map(([logicalId]) => logicalId)
      .find((logicalId) => logicalId.startsWith('RoleDefaultPolicy'));
    return { id: id ?? '', lines: invokeSourceGrants('Probe', template) };
  }

  it('refuses every update route a glob reaches, in the probe order', () => {
    const { id, lines } = probe(['lambda:Update*']);
    expect(lines).toEqual([
      `Probe/${id}: grants lambda:UpdateEventSourceMapping`,
      `Probe/${id}: grants lambda:UpdateFunctionCode`,
      `Probe/${id}: grants lambda:UpdateFunctionConfiguration`,
      `Probe/${id}: grants lambda:UpdateFunctionEventInvokeConfig`,
      `Probe/${id}: grants lambda:UpdateFunctionUrlConfig`,
      `Probe/${id}: grants lambda:UpdateAlias`,
    ]);
  });

  it('refuses an event-source mapping', () => {
    const { id, lines } = probe(['lambda:CreateEventSourceMapping']);
    expect(lines).toEqual([`Probe/${id}: grants lambda:CreateEventSourceMapping`]);
  });

  it('accepts invoking and reading a function', () => {
    expect(probe(['lambda:InvokeFunction', 'lambda:GetFunction'], true).lines).toEqual([]);
  });

  it('finds no invocation-source grant in any stack', () => {
    const built = everyTemplate();
    expect(Object.entries(built).flatMap(([name, template]) =>
      invokeSourceGrants(name, template as Record<string, unknown>))).toEqual([]);
  });
});

describe('SEMANTIC-M1 CHAT Athena isolation', () => {
  /** Every Athena action's resource text in one stack's policies, deduplicated. */
  function athenaResources(template: Record<string, unknown>): string[] {
    const found = new Set<string>();
    for (const document of policyDocuments(template)) {
      for (const statement of document) {
        if (!actionsOf(statement.Action).some((action) => action.startsWith('athena:'))) continue;
        const resource = (statement as { Resource?: unknown }).Resource;
        for (const target of [resource].flat()) found.add(JSON.stringify(target));
      }
    }
    return [...found];
  }

  it('keeps every CHAT principal (Api, Agent) on the interactive workgroup only', () => {
    const built = everyTemplate();
    for (const name of ['ApiStack', 'AgentStack']) {
      const resources = athenaResources(built[name] as Record<string, unknown>);
      // Paired control: the same reader finds the interactive workgroup each CHAT stack uses.
      expect([name, resources.some((text) => text.includes('adlytics-dev-v2-interactive'))]).toEqual([
        name,
        true,
      ]);
      expect([name, resources.filter((text) => !text.includes('adlytics-dev-v2-interactive'))]).toEqual([
        name,
        [],
      ]);
    }
    // Control: the reader is not blind to other workgroups; the publisher's baker holds BAKE.
    const publisher = athenaResources(built.PublisherStack as Record<string, unknown>);
    expect(publisher.some((text) => text.includes('adlytics-dev-v2-bake'))).toBe(true);
  });
});
