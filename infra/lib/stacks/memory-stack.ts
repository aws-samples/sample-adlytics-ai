// SPDX-License-Identifier: MIT-0
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import type * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { invocableModelArn, modelInvokeStatements } from '../bedrock-model-access';
import { AppConfig, memoryName, paramPath } from '../config';
import { applyRemovalPolicy, applyStandardTags } from '../mixins/tagging';

/** The service's bound on an override prompt (`appendToPrompt`), after trimming. */
const OVERRIDE_PROMPT_MAX_CHARACTERS = 30_000;

/**
 * AgentCore Memory: the short-term events of a conversation and the three long-term strategies
 * consolidated from them.
 *
 * **RETAIN, and its own stack for that reason alone.** This holds conversation history, while the
 * direct Runtime and request-serving functions are disposable. The composition boundary prevents a
 * Runtime replacement from deleting conversation state.
 *
 * **The strategies must exist before any real traffic and this is the only chance to get that
 * right.** Long-term memory processes only events created *after* a strategy reaches `ACTIVE`, so
 * adding one later does not backfill — a strategy introduced after launch is blind to every
 * conversation that came before it ([Conversational Runtime LLD](../../../docs/capabilities/conversational-runtime/low-level-design.md)
 * §6, AgentCore platform constraints).
 * All three are therefore declared here at environment creation rather than added when first
 * wanted.
 *
 * **One of the three is built-in with overrides** (ADR-147): the advertiser-context strategy keeps
 * preferences, goals, business context and vocabulary, and its extraction and consolidation
 * prompts forbid storing any measured result. A figure recalled from memory was restated as the
 * user's records after a governed tool refused; memory is context, never evidence.
 */
export interface MemoryStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** The override prompt contracts, read once at synth (`contracts/prompts/v1/memory-context-*.md`). */
  readonly promptPaths: { readonly extraction: string; readonly consolidation: string };
}

export class MemoryStack extends cdk.Stack {
  /** Conversation state, keyed by actor and session. */
  public readonly memory: agentcore.Memory;

  constructor(scope: Construct, id: string, props: MemoryStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prompts = {
      extraction: readOverridePrompt(props.promptPaths.extraction),
      consolidation: readOverridePrompt(props.promptPaths.consolidation),
    };
    const strategies = this.strategies(config, prompts);

    this.memory = new agentcore.Memory(this, 'Conversations', {
      memoryName: memoryName(config.envName),
      description: 'Conversation events and the long-term records consolidated from them',
      // Per-event at write time and not extendable afterwards, so this is a decision rather than
      // a setting. AgentConfig.memoryExpirationDays carries the reasoning.
      expirationDuration: cdk.Duration.days(config.agent.memoryExpirationDays),
      memoryStrategies: strategies,
      // No customer-managed key, matching `data.encryption` for now. Worth knowing before adding
      // one: event *metadata* is not encrypted with a CMK regardless, which is why the Conversational
      // Runtime LLD §6 (AgentCore platform constraints) says nothing sensitive belongs in metadata.
      // A key here would not change that.
    });

    // Every resource RETAIN except the parameter below, which the mixin excludes deliberately: a
    // retained parameter pointing at a deleted memory is not a saved asset, it is a blocked
    // redeploy.
    //
    // This also retains the service role the L2 creates for the memory, which is the right outcome
    // and worth stating because it looks like over-reach. A retained memory whose execution role
    // was deleted cannot consolidate anything — the long-term strategies stop working while the
    // events sit there — so the role is part of what makes retention meaningful rather than an
    // orphan left behind with it.
    applyRemovalPolicy(this, cdk.RemovalPolicy.RETAIN);
    applyStandardTags(this, config);

    this.grantOverrideModel(config);
    this.publishParameters(config, strategyRevision(strategies, config.agent.memoryModelId));
    this.publishOutputs();
  }

  /**
   * Let the memory's own role invoke the override model, and nothing else.
   *
   * The role the L2 created and already deployed is kept: no role churn on a RETAIN stack, and no
   * change to the memory's role ARN (ADR-147 decision 3). Its trust is the L2's: the AgentCore
   * service principal under `aws:SourceAccount` and an `aws:SourceArn` limited to this memory's
   * name. The grant is the A-IAM-MODEL-001 shape: the profile in this Region, and its foundation
   * model only through that profile. No managed policy: AWS's memory inference policy allows every
   * model and profile in the account. These are the role's only model statements: the override
   * strategy is declared through `SemanticOverrideStrategy`, which drops the L2's own wildcard
   * `bedrock:InvokeModel*` grant.
   *
   * Dropping that grant also dropped the ordering it carried (`grant(...)?.applyBefore(memory)`),
   * so the memory resource is made to depend on the policy here. Without it CloudFormation may
   * create the memory first, and a create that rolls back between the two would retain a memory
   * whose role cannot invoke its override model.
   */
  private grantOverrideModel(config: AppConfig): void {
    const role = this.memory.executionRole;
    if (role === undefined) {
      throw new Error('MemoryStack: the memory has no execution role to grant the override model on.');
    }
    const resource = memoryResource(this.memory);
    for (const statement of modelInvokeStatements(
      this,
      'InvokeMemoryModel',
      [config.agent.memoryModelId],
      ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    )) {
      const { statementAdded, policyDependable } = role.addToPrincipalPolicy(statement);
      if (!statementAdded || policyDependable === undefined) {
        throw new Error('MemoryStack: the memory execution role did not accept the override model grant.');
      }
      resource.node.addDependency(policyDependable);
    }
  }

  /**
   * The three long-term strategies, one built-in with overrides, and what each namespace is keyed
   * by.
   *
   * **The namespace is where per-user scoping actually lives.** `actorId` is the authenticated
   * Cognito subject, and a namespace template interpolating `{actorId}` is what keeps one user's
   * consolidated memory out of another's context.
   *
   * Only the summary is keyed by session, and the asymmetry is the point: a summary is *of* one
   * conversation, while a stated preference and a learned fact are meant to outlive the
   * conversation that produced them. Keying preferences by session would discard them at the end
   * of every chat, which is the failure that makes long-term memory look broken rather than absent.
   *
   * The advertiser-context strategy replaced the managed semantic one under a new name and
   * namespace, so the figure-carrying records in `/semantics/` leave the Harness's retrieval at
   * cutover instead of sharing a namespace with the new strategy (ADR-147 decision 1).
   */
  private strategies(config: AppConfig, prompts: OverridePrompts): agentcore.ManagedMemoryStrategy[] {
    const model = { modelArn: invocableModelArn(this, config.agent.memoryModelId) };
    return [
      new agentcore.ManagedMemoryStrategy(agentcore.MemoryStrategyType.USER_PREFERENCE, {
        strategyName: 'user_preference',
        description: 'How this user wants figures presented, and what they habitually ask for',
        namespaces: ['/preferences/{actorId}'],
      }),
      new agentcore.ManagedMemoryStrategy(agentcore.MemoryStrategyType.SUMMARIZATION, {
        strategyName: 'summary',
        description: 'A running summary of one conversation, so it survives session recycling',
        namespaces: ['/summaries/{actorId}/{sessionId}'],
      }),
      new SemanticOverrideStrategy(agentcore.MemoryStrategyType.SEMANTIC, {
        strategyName: 'advertiser_context',
        description:
          'Durable context about this advertiser: preferences, goals, business context and ' +
          'vocabulary. Never measured results.',
        namespaces: ['/context/{actorId}'],
        customExtraction: { model, appendToPrompt: prompts.extraction },
        customConsolidation: { model, appendToPrompt: prompts.consolidation },
      }),
    ];
  }

  /**
   * The memory's ARN, for explicitly authorized conversation-state consumers.
   *
   * Through SSM rather than a construct reference because consumers are in another stack, which
   * ADR-012 settles. The split allows the Runtime to be replaced without touching retained state.
   *
   * The strategy revision goes the same way. The Harness carries it as a custom-resource property,
   * so a strategy change sends the Harness an update that re-derives its retrieval configuration;
   * AWS directs an `UpdateHarness` after a BYO memory's strategies change (ADR-147 decision 4).
   */
  private publishParameters(config: AppConfig, revision: string): void {
    new ssm.StringParameter(this, 'ParamMemoryArn', {
      parameterName: paramPath(config.envName, 'memoryArn'),
      stringValue: this.memory.memoryArn,
      description: 'AgentCore Memory holding conversation events and long-term records.',
    });
    new ssm.StringParameter(this, 'ParamMemoryStrategyRevision', {
      parameterName: paramPath(config.envName, 'memoryStrategyRevision'),
      stringValue: revision,
      description:
        'Digest of the declared long-term strategies; a change refreshes the Harness retrieval ' +
        'configuration.',
    });
  }

  private publishOutputs(): void {
    new cdk.CfnOutput(this, 'OutMemoryId', {
      value: this.memory.memoryId,
      description: 'AgentCore Memory for conversations. RETAIN: a stack delete leaves it behind.',
    });
    new cdk.CfnOutput(this, 'OutMemoryExpirationDays', {
      value: String(this.stackConfigExpirationDays),
      description:
        'Event retention. Applied per event at write time, so raising it does not extend ' +
        'anything already written.',
    });
  }

  /** Read back from the construct so the output cannot disagree with what was deployed. */
  private get stackConfigExpirationDays(): number {
    return this.memory.expirationDuration?.toDays() ?? 0;
  }
}

interface OverridePrompts {
  readonly extraction: string;
  readonly consolidation: string;
}

/**
 * The built-in semantic strategy with overrides, declared without the L2's own model grant.
 *
 * `ManagedMemoryStrategy.grant` would give the memory role `bedrock:InvokeModel*` on the override
 * model: a wildcard action beyond the two this role is reviewed for, and one an aws-cdk-lib
 * upgrade could widen. `grantOverrideModel` grants those two in the A-IAM-MODEL-001 shape instead,
 * so this declaration grants nothing. The memory-stack and bedrock-model-access suites pin the
 * role's complete policy, so an L2 that grants through another path turns them red.
 */
class SemanticOverrideStrategy extends agentcore.ManagedMemoryStrategy {
  public override grant(_grantee: iam.IGrantable): iam.Grant | undefined {
    return undefined;
  }
}

/**
 * The `AWS::BedrockAgentCore::Memory` resource inside the L2. The L2 keeps it private and names it
 * `Memory` rather than `Resource`, so `node.defaultChild` does not find it; found by type, and
 * exactly one.
 */
function memoryResource(memory: agentcore.Memory): agentcore.CfnMemory {
  const found = memory.node.children.filter(
    (child): child is agentcore.CfnMemory => child instanceof agentcore.CfnMemory,
  );
  const [resource] = found;
  if (found.length !== 1 || resource === undefined) {
    throw new Error(`MemoryStack: expected one AWS::BedrockAgentCore::Memory in the L2, found ${found.length}.`);
  }
  return resource;
}

/** Read one override prompt contract, refusing a text the service would reject. */
function readOverridePrompt(promptPath: string): string {
  const text = fs.readFileSync(promptPath, 'utf-8').trim();
  if (text.length === 0) {
    throw new Error(`Memory override prompt ${promptPath} is empty.`);
  }
  if (text.length > OVERRIDE_PROMPT_MAX_CHARACTERS) {
    throw new Error(`Memory override prompt ${promptPath} exceeds 30,000 characters.`);
  }
  return text;
}

function sha256(text: string): string {
  return crypto.createHash('sha256').update(text, 'utf-8').digest('hex');
}

/**
 * The digest of what the strategies declare: each one's name, type and namespaces, and for a
 * strategy with overrides the model ID and the digest of each override prompt it carries, read
 * from the strategy rather than from the contract files. The model ID is the configured one every
 * override names; its ARN is a token until deploy, so it is not read back. Computed, never input.
 */
function strategyRevision(
  strategies: readonly agentcore.ManagedMemoryStrategy[],
  modelId: string,
): string {
  const promptDigest = (override: { readonly appendToPrompt: string } | undefined): string | undefined =>
    override === undefined ? undefined : `sha256:${sha256(override.appendToPrompt)}`;
  const declared = [...strategies]
    .sort((a, b) => a.strategyName.localeCompare(b.strategyName))
    .map((strategy) => {
      const extraction = promptDigest(strategy.extractionOverride);
      const consolidation = promptDigest(strategy.consolidationOverride);
      return {
        name: strategy.strategyName,
        type: strategy.strategyType,
        namespaces: [...strategy.namespaces],
        ...(extraction === undefined && consolidation === undefined ? {} : { modelId }),
        ...(extraction === undefined ? {} : { extractionPromptDigest: extraction }),
        ...(consolidation === undefined ? {} : { consolidationPromptDigest: consolidation }),
      };
    });
  return `sha256:${sha256(JSON.stringify(declared))}`;
}
