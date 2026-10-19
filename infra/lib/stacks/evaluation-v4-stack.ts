// SPDX-License-Identifier: MIT-0
import * as fs from 'node:fs';

import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';

import { AppConfig } from '../config';
import {
  EvaluationGate,
  evaluatorIdentity,
  EvaluatorIdentity,
  GateEvaluator,
} from '../constructs/agent-quality/evaluation-gate';
import { staffedAlarmAction } from '../constructs/observability/async-failure';
import { applyFunctionErrorAlarms } from '../mixins/function-error-alarms';
import { applyStandardTags } from '../mixins/tagging';

export interface EvaluationV4PromptPaths {
  readonly groundedCorrectness: string;
  readonly internalConsistency: string;
  readonly helpfulness: string;
}

/**
 * One evaluator's rating scale. A categorical scale records a label and no score; a numerical
 * one records a label and its declared value.
 */
type EvaluatorScale =
  | { readonly kind: 'categorical'; readonly options: agentcore.CategoricalRatingOption[] }
  | { readonly kind: 'numerical'; readonly options: agentcore.NumericalRatingOption[] };
export interface EvaluationV4StackProps extends cdk.StackProps {
  readonly config: AppConfig;
  readonly promptPaths: EvaluationV4PromptPaths;
  /** The V4 fictional calibration corpus, `validation/evaluation-v4-cases.json`. */
  readonly calibrationCorpusPath: string;
  /** Directory holding the staged Python source tree (the gate provider's code). */
  readonly sourceAssetPath: string;
}

/** One V4 evaluator's definition: what it judges with, its scale, and its worst label. */
export interface EvaluatorV4Spec {
  readonly id: string;
  /** The name segment before the revision. */
  readonly dimension: string;
  readonly corpusDimension: string;
  /** The label whose share of verdicts the quality alarm watches (ADR-079 §5). */
  readonly alarmLabel: string;
  readonly description: string;
  readonly promptPath: string;
  readonly modelId: string;
  readonly scale: EvaluatorScale;
}

/**
 * The V4 evaluators, stated once: `EvaluationV4Stack` creates them and `EvaluationQualityStack`
 * names their metrics, and both derive each evaluator's name from these same inputs.
 */
export function evaluatorV4Specs(
  config: AppConfig,
  promptPaths: EvaluationV4PromptPaths,
): readonly EvaluatorV4Spec[] {
  return [
    {
      id: 'GroundedCorrectnessV4R2',
      dimension: 'grounded_correctness_v4',
      corpusDimension: 'grounded_correctness',
      alarmLabel: 'Unsupported',
      description: 'Checks released claims against protected governed evidence and obligations.',
      promptPath: promptPaths.groundedCorrectness,
      modelId: config.agent.deepDive.modelId,
      // Categorical, so a claim-free turn can be Not Applicable: the service records a label and
      // no score for it (ADR-082; the dev probe of 2026-09-27 settled the scale choice).
      scale: {
        kind: 'categorical',
        options: [
          {
            label: 'Grounded',
            definition: 'Every material claim, including any figure derived from the evidence, is supported and complete.',
          },
          { label: 'Partially Grounded', definition: 'Core claims are supported but a material gap remains.' },
          {
            label: 'Unsupported',
            definition:
              'A material claim, or a figure, conflicts with or is absent from trusted evidence, or is derived incorrectly or across incompatible bases.',
          },
          {
            label: 'Not Applicable',
            definition: 'The response makes no platform or customer claim and none was required, such as a clarifying question.',
          },
        ],
      },
    },
    {
      id: 'InternalConsistencyV4R2',
      dimension: 'internal_consistency_v4',
      corpusDimension: 'internal_consistency',
      alarmLabel: 'Contradictory',
      description: 'Checks arithmetic, collection counts, scope, and status consistency.',
      promptPath: promptPaths.internalConsistency,
      modelId: config.agent.deepDive.modelId,
      scale: {
        kind: 'numerical',
        options: [
          { value: 1, label: 'Consistent', definition: 'All totals, memberships, scopes, and statuses reconcile.' },
          { value: 0.5, label: 'Minor Issue', definition: 'A minor inconsistency does not change the core answer.' },
          { value: 0, label: 'Contradictory', definition: 'A material count, arithmetic, membership, scope, or status conflict exists.' },
        ],
      },
    },
    {
      id: 'HelpfulnessV4R2',
      dimension: 'helpfulness_v4',
      corpusDimension: 'helpfulness',
      alarmLabel: 'Poor',
      description: 'Judges usefulness and communication independently from factual correctness.',
      promptPath: promptPaths.helpfulness,
      modelId: config.agent.quickChat.modelId,
      scale: {
        kind: 'numerical',
        options: [
          { value: 1, label: 'Excellent', definition: 'Direct, natural, useful, and appropriately qualified.' },
          { value: 0.75, label: 'Good', definition: 'Useful with minor clarity or focus issues.' },
          { value: 0.5, label: 'Needs Improvement', definition: 'Partly useful but materially incomplete or awkward.' },
          { value: 0, label: 'Poor', definition: 'Does not help the user or presents an inappropriate response.' },
        ],
      },
    },
  ];
}

/** The evaluator name each spec yields, from the exact prompt bytes and model it judges with. */
export function evaluatorV4Identity(
  envName: string,
  spec: EvaluatorV4Spec,
): EvaluatorIdentity & { readonly instructions: string } {
  // Read once: the evaluator receives exactly these bytes and the release record their digest.
  const instructions = fs.readFileSync(spec.promptPath, 'utf-8');
  return { ...evaluatorIdentity(envName, spec.dimension, instructions, spec.modelId), instructions };
}

/** The evaluator name each spec yields; the one derivation both stacks use. */
export function evaluatorV4Name(config: AppConfig, spec: EvaluatorV4Spec): string {
  return evaluatorV4Identity(config.envName, spec).evaluatorName;
}

/**
 * V4 grounded multidimensional evaluation: three retained evaluators and their enablement gate
 * (ADR-079), which alone owns the V4 online-evaluation configuration and release record.
 */
export class EvaluationV4Stack extends cdk.Stack {
  public readonly gate: EvaluationGate;
  private readonly envName: string;

  constructor(scope: Construct, id: string, props: EvaluationV4StackProps) {
    super(scope, id, props);
    const { config } = props;
    this.envName = config.envName;
    if (!config.onlineEvaluation.provisioned || !config.transactionSearchEnabled) {
      throw new Error('EvaluationV4Stack requires online evaluation and Transaction Search.');
    }

    const evaluators = evaluatorV4Specs(config, props.promptPaths).map((spec) =>
      this.llmEvaluator(spec),
    );
    this.gate = new EvaluationGate(this, 'GateV4', {
      config,
      family: 'v4',
      description: 'Grounded correctness, internal consistency, and helpfulness of released responses.',
      executionRequested: config.onlineEvaluationV4.executionRequested,
      evaluators,
      corpusPath: props.calibrationCorpusPath,
      sourceAssetPath: props.sourceAssetPath,
    });
    applyFunctionErrorAlarms(this, config, staffedAlarmAction(this, config));
    applyStandardTags(this, config);
  }

  private llmEvaluator(options: EvaluatorV4Spec): GateEvaluator {
    const { evaluatorName, revision, instructions } = evaluatorV4Identity(this.envName, options);
    const evaluator = new agentcore.Evaluator(this, options.id, {
      evaluatorName,
      description: options.description,
      level: agentcore.EvaluationLevel.TRACE,
      evaluatorConfig: agentcore.EvaluatorConfig.llmAsAJudge({
        instructions,
        modelId: options.modelId,
        inferenceConfig: { maxTokens: 700, temperature: 0 },
        ratingScale:
          options.scale.kind === 'categorical'
            ? agentcore.EvaluatorRatingScale.categorical(options.scale.options)
            : agentcore.EvaluatorRatingScale.numerical(options.scale.options),
      }),
    });
    evaluator.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    return {
      evaluator,
      evaluatorName,
      revision,
      modelId: options.modelId,
      instructions,
      corpusDimension: options.corpusDimension,
    };
  }
}
