// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as xray from 'aws-cdk-lib/aws-xray';
import { Construct } from 'constructs';
import { AppConfig } from '../config';
import { applyStandardTags } from '../mixins/tagging';

/**
 * Account-wide observability configuration, and nothing that produces telemetry.
 *
 * **It holds one account-wide setting and its X-Ray delivery resource policy.** The
 * scope is wrong for anywhere else — Transaction Search is configured per *account*,
 * not per application, so it does not belong in a stack that owns application
 * resources. The ordering matters too: this deploys before `AgentStack`, because
 * AgentCore cannot deliver spans to a per-agent log group unless Transaction Search
 * is already on, and a span destination is fixed when the agent is created.
 *
 * **Nothing here is instrumentation.** In-process spans remain with the code that does
 * the work. The conversation route explicitly passes its server trace as
 * `InvokeHarness.traceParent`; no baggage is carried. A Gateway Lambda target receives
 * no inherited parent trace or edge correlation, so it starts a separate tool trace
 * and MCP/AWS request-local correlation. This stack decides only whether spans are
 * *ingested and searchable*.
 *
 * DESTROY rather than RETAIN, and deliberately so. These resources configure delivery
 * and search; they do not store telemetry. This stack creates no log group, sets no log
 * retention, and grants no operator read access. Retention and reader policy for the
 * managed span groups are external to it.
 *
 * **At most one environment per account may enable ingestion, and the stack name understates
 * that.** The id carries `<env>` like every other stack, but what it holds does not: the
 * X-Ray configuration is an account-and-region singleton, and the logs resource policy is keyed
 * on a fixed name. A second environment in the same account with the flag on contends for both,
 * and whichever deploys last wins silently. There is no synth-time guard because a stack cannot
 * see another stack's deployed state — so this is a constraint to know rather than one the
 * tooling enforces. Account-level ingestion requires explicit per-account coordination.
 */
export interface ObservabilityStackProps extends cdk.StackProps {
  readonly config: AppConfig;
}

export class ObservabilityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    const { config } = props;

    // X-Ray delivers spans into CloudWatch Logs on the account's behalf, so the account has to
    // grant it that. This is service-delivery-only permission: `logs:PutLogEvents` for
    // `xray.amazonaws.com`, not application or operator read access. Without it the configuration
    // below fails to create outright, with
    // `XRay does not have permission to call PutLogEvents on the aws/spans Log Group`
    // [verified 2026-08-19, by exactly that failed deploy].
    //
    // Both log groups are required, not just the spans one: Application Signals writes to its
    // own and X-Ray needs the same grant there. The two conditions are confused-deputy
    // protection — they pin the caller to this account's X-Ray, so another account cannot
    // induce X-Ray to write into these log groups.
    const spanDelivery = new logs.CfnResourcePolicy(this, 'XRaySpanDelivery', {
      policyName: 'TransactionSearchAccess',
      policyDocument: JSON.stringify({
        Version: '2012-10-17',
        Statement: [
          {
            Sid: 'TransactionSearchXRayAccess',
            Effect: 'Allow',
            Principal: { Service: 'xray.amazonaws.com' },
            Action: 'logs:PutLogEvents',
            Resource: [
              `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:aws/spans:*`,
              `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/application-signals/data:*`,
            ],
            Condition: {
              ArnLike: {
                'aws:SourceArn': `arn:${this.partition}:xray:${this.region}:${this.account}:*`,
              },
              StringEquals: { 'aws:SourceAccount': this.account },
            },
          },
        ],
      }),
    });

    // Unconditional, because the existence of this stack *is* the decision. A stack that
    // renders nothing when ingestion is off would be an empty `Resources` section, which
    // CloudFormation rejects — so the shipped default would ship an undeployable stack. The
    // flag is therefore read in `bin/app.ts`, which declines to instantiate this at all.
    //
    // A singleton per account and region. Only this stack may create it: a second definition
    // anywhere would contend for the same account-level setting, and the last deploy would
    // silently win.
    const search = new xray.CfnTransactionSearchConfig(this, 'TransactionSearch', {
      indexingPercentage: config.transactionSearchIndexingPercentage,
    });

    // Ordered explicitly. CloudFormation infers nothing here because neither resource
    // references the other — they are joined only by the account-level permission one needs and
    // the other grants, which is invisible to the dependency graph.
    search.addDependency(spanDelivery);

    applyStandardTags(this, config);

    new cdk.CfnOutput(this, 'OutSpanIndexingPercentage', {
      value: String(config.transactionSearchIndexingPercentage),
      description:
        'What percentage of ingested spans is indexed as trace summaries. Lower this before ' +
        'turning ingestion off: it is the expensive half and reducing it keeps the spans.',
    });
  }
}
