// SPDX-License-Identifier: MIT-0
import { everyTemplate, resourcesOfType } from './support/templates';

const api = everyTemplate().ApiStack as Record<string, unknown>;

type Resource = Record<string, unknown>;

function roleFor(functionName: string): string {
  const functionResource = resourcesOfType(api, 'AWS::Lambda::Function').find(
    (resource) => resource.FunctionName === `adlytics-dev-PromptAdmin${functionName}Function`,
  ) as { Role?: { 'Fn::GetAtt'?: unknown[] } } | undefined;
  const role = functionResource?.Role?.['Fn::GetAtt']?.[0];
  if (typeof role !== 'string') throw new Error(`prompt-admin ${functionName} role is absent`);
  return role;
}

function statementsFor(functionName: string): Resource[] {
  const role = roleFor(functionName);
  return resourcesOfType(api, 'AWS::IAM::Policy')
    .filter((resource) =>
      ((resource.Roles as { Ref?: string }[] | undefined) ?? []).some((candidate) => candidate.Ref === role),
    )
    .flatMap((resource) =>
      ((resource.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
    );
}

function actionsFor(functionName: string): string[] {
  return statementsFor(functionName)
    .flatMap((statement) => {
      const action = statement.Action;
      return Array.isArray(action) ? action : [action];
    })
    .filter((action): action is string => typeof action === 'string');
}

function functionEnvironment(functionName: string): Resource | undefined {
  const functionResource = resourcesOfType(api, 'AWS::Lambda::Function').find(
    (resource) => resource.FunctionName === `adlytics-dev-PromptAdmin${functionName}Function`,
  );
  return (functionResource?.Environment as { Variables?: Resource } | undefined)?.Variables;
}

describe('the protected prompt-registry commands', () => {
  it('uses the custom authorizer on the five canonical routes and the separate calibration extension', () => {
    const routes = resourcesOfType(api, 'AWS::ApiGatewayV2::Route');
    for (const command of ['slots', 'versions', 'create-version', 'activate', 'retire']) {
      expect(routes.find((route) => route.RouteKey === `POST /api/prompt-registry/${command}`)).toEqual(
        expect.objectContaining({ AuthorizationType: 'CUSTOM' }),
      );
    }
    expect(routes.find((route) => route.RouteKey === 'POST /api/prompt-registry/extensions/calibrate')).toEqual(
      expect.objectContaining({ AuthorizationType: 'CUSTOM' }),
    );
    for (const legacy of ['create', 'calibrate', 'activate', 'retire']) {
      expect(routes.find((route) => route.RouteKey === `POST /api/prompt-admin/${legacy}`)).toBeUndefined();
    }
  });

  it('assigns the canonical create-version handler route and keeps recovery distinctly named', () => {
    expect(functionEnvironment('Create')?.ADL_PROMPT_ADMIN_ROUTE).toBe('create-version');
    expect(functionEnvironment('Calibrate')?.ADL_PROMPT_ADMIN_ROUTE).toBe('calibrate');
  });

  it('queues create and recovery with no model authority on the request path', () => {
    // Transactional writes authorize as their underlying item actions, never as a
    // TransactWriteItems action — the wrong shape denied the first live create-version.
    const expected: Record<string, string[]> = {
      Create: ['dynamodb:GetItem', 'dynamodb:ConditionCheckItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'sqs:SendMessage'],
      Calibrate: ['dynamodb:GetItem', 'dynamodb:DeleteItem', 'sqs:SendMessage'],
      Activate: ['dynamodb:GetItem', 'dynamodb:ConditionCheckItem', 'dynamodb:PutItem'],
      Retire: ['dynamodb:DeleteItem', 'dynamodb:PutItem'],
    };
    for (const [command, permissions] of Object.entries(expected)) {
      const actions = actionsFor(command);
      expect(actions).toEqual(expect.arrayContaining(permissions));
      expect(actions).not.toContain('dynamodb:Scan');
    }
    for (const command of ['Create', 'Calibrate']) {
      const variables = functionEnvironment(command);
      expect(variables?.ADL_INSIGHT_PROMPT_CALIBRATION_QUEUE_URL).toBeDefined();
      expect(variables?.ADL_INSIGHT_QUICK_MODEL_ID).toBeUndefined();
      expect(variables?.ADL_INSIGHT_DEEP_MODEL_ID).toBeUndefined();
      expect(actionsFor(command)).not.toContain('bedrock:InvokeModel');
    }
    for (const command of ['Activate', 'Retire']) {
      expect(actionsFor(command)).not.toContain('bedrock:InvokeModel');
      expect(actionsFor(command)).not.toContain('sqs:SendMessage');
    }
  });

  it('gives the roster and version readers only their respective DynamoDB read actions', () => {
    expect(actionsFor('Slots')).toEqual(expect.arrayContaining(['dynamodb:BatchGetItem']));
    expect(actionsFor('Versions')).toEqual(expect.arrayContaining(['dynamodb:BatchGetItem', 'dynamodb:Query']));
    for (const command of ['Slots', 'Versions']) {
      const actions = actionsFor(command);
      expect(actions).not.toContain('dynamodb:Scan');
      expect(actions).not.toContain('bedrock:InvokeModel');
      expect(actions).not.toContain('dynamodb:TransactWriteItems');
    }
  });

  it('lets only the versions reader look up one Cognito user, on the user pool', () => {
    const parameters = (api.Parameters ?? {}) as Record<string, { Default?: unknown }>;
    const [poolParameter] = Object.entries(parameters)
      .filter(([, parameter]) => String(parameter.Default).endsWith('/user-pool-id'))
      .map(([name]) => name);
    expect(poolParameter).toBeDefined();
    const cognito = statementsFor('Versions').filter((statement) =>
      JSON.stringify(statement.Action).includes('cognito-idp:'),
    );
    // The stack's region and account are concrete in the test app, so the ARN joins the
    // partition, the userpool prefix, and the deploy-time pool id from SSM.
    expect(cognito).toEqual([
      expect.objectContaining({
        Action: 'cognito-idp:AdminGetUser',
        Effect: 'Allow',
        Resource: {
          'Fn::Join': [
            '',
            [
              'arn:',
              { Ref: 'AWS::Partition' },
              expect.stringMatching(/^:cognito-idp:[a-z0-9-]+:\d{12}:userpool\/$/),
              { Ref: poolParameter },
            ],
          ],
        },
      }),
    ]);
    for (const command of ['Slots', 'Create', 'Calibrate', 'Activate', 'Retire']) {
      expect(actionsFor(command).filter((action) => action.startsWith('cognito-idp:'))).toEqual([]);
    }
  });

  it('grants no Cognito list or wildcard action anywhere in the API stack', () => {
    const granted = resourcesOfType(api, 'AWS::IAM::Policy')
      .flatMap((resource) =>
        ((resource.PolicyDocument as { Statement?: Resource[] } | undefined)?.Statement ?? []),
      )
      .flatMap((statement) => {
        const action = statement.Action;
        return Array.isArray(action) ? action : [action];
      })
      .filter((action): action is string => typeof action === 'string');
    expect(granted).not.toContain('cognito-idp:ListUsers');
    expect(granted).not.toContain('cognito-idp:*');
    expect(granted.filter((action) => action.startsWith('cognito-idp:'))).toEqual([
      'cognito-idp:AdminGetUser',
    ]);
  });
});
