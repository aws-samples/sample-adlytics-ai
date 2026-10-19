// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as path from 'node:path';
import { Template } from 'aws-cdk-lib/assertions';
import type { AppConfig } from '../lib/config';
import { EvaluationV4Stack } from '../lib/stacks/evaluation-v4-stack';
import { configWith } from './support/config';
import { EVALUATION_CORPORA } from './support/templates';
function v4Config(): AppConfig {
  return configWith({
    transactionSearchEnabled: true,
    evaluationInput: { provisioned: true },
    onlineEvaluation: {
      provisioned: true,
      samplingPercentage: 100,
      sessionTimeoutMinutes: 1,
    },
    onlineEvaluationV4: {
      executionRequested: false,
    },
  });
}
function synth(config: AppConfig = v4Config()): Template {
  const app = new cdk.App();
  const root = path.join(__dirname, '..', '..', 'contracts', 'prompts', 'v1');
  const stack = new EvaluationV4Stack(app, 'Adlytics-EvaluationV4-test', {
    config,
    env: { account: config.account, region: config.region },
    promptPaths: {
      groundedCorrectness: path.join(root, 'grounded-response-correctness.md'),
      internalConsistency: path.join(root, 'response-internal-consistency.md'),
      helpfulness: path.join(root, 'released-response-helpfulness-v4.md'),
    },
    calibrationCorpusPath: EVALUATION_CORPORA.v4,
    sourceAssetPath: __dirname,
  });
  return Template.fromStack(stack);
}
type Resource = { Type: string; Properties: Record<string, unknown>; DeletionPolicy?: string; UpdateReplacePolicy?: string };
const evaluators = (template: Template): Resource[] =>
  Object.values(template.toJSON().Resources as Record<string, Resource>)
    .filter((resource) => resource.Type === 'AWS::BedrockAgentCore::Evaluator');
const modelOf = (evaluator: Resource): unknown =>
  (evaluator.Properties.EvaluatorConfig as {
    LlmAsAJudge: { ModelConfig: { BedrockEvaluatorModelConfig: { ModelId: unknown } } };
  }).LlmAsAJudge.ModelConfig.BedrockEvaluatorModelConfig.ModelId;
describe('EvaluationV4Stack', () => {
  it('creates three isolated dimensions and hands their configuration to the gate', () => {
    const template = synth();
    template.resourceCountIs('AWS::BedrockAgentCore::Evaluator', 3);
    template.resourceCountIs('AWS::BedrockAgentCore::OnlineEvaluationConfig', 0);
    template.resourceCountIs('Custom::AgentCoreOnlineEvaluation', 1);
    template.resourceCountIs('AWS::SSM::Parameter', 0);
    expect(evaluators(template).map((e) => e.Properties.EvaluatorName)).toEqual([
      expect.stringMatching(/^adlytics_dev_grounded_correctness_v4_[0-9a-f]{8}$/),
      expect.stringMatching(/^adlytics_dev_internal_consistency_v4_[0-9a-f]{8}$/),
      expect.stringMatching(/^adlytics_dev_helpfulness_v4_[0-9a-f]{8}$/),
    ]);
  });
  it('uses the stronger model for correctness and the quick model for helpfulness', () => {
    expect(evaluators(synth()).map(modelOf)).toEqual([
      'test.deep-dive-model-v1:0',
      'test.deep-dive-model-v1:0',
      'test.quick-chat-model-v1:0',
    ]);
  });
  it('rates grounded correctness categorically with Not Applicable, the others numerically', () => {
    const scales = evaluators(synth()).map((evaluator) =>
      (evaluator.Properties.EvaluatorConfig as {
        LlmAsAJudge: { RatingScale: { Categorical?: { Label: string }[]; Numerical?: { Label: string; Value: number }[] } };
      }).LlmAsAJudge.RatingScale);
    expect(scales[0]!.Numerical).toBeUndefined();
    expect(scales[0]!.Categorical!.map((option) => option.Label)).toEqual([
      'Grounded',
      'Partially Grounded',
      'Unsupported',
      'Not Applicable',
    ]);
    expect(scales[1]!.Categorical).toBeUndefined();
    expect(scales[1]!.Numerical!.map((option) => [option.Label, option.Value])).toEqual([
      ['Consistent', 1],
      ['Minor Issue', 0.5],
      ['Contradictory', 0],
    ]);
    expect(scales[2]!.Categorical).toBeUndefined();
    expect(scales[2]!.Numerical!.map((option) => option.Label)).toEqual([
      'Excellent',
      'Good',
      'Needs Improvement',
      'Poor',
    ]);
  });
  it('retains versioned evaluator definitions while the gate switches the configuration', () => {
    for (const evaluator of evaluators(synth())) {
      expect(evaluator.DeletionPolicy).toBe('Retain');
      expect(evaluator.UpdateReplacePolicy).toBe('Retain');
    }
  });
  it('refuses to synthesize without online evaluation provisioned', () => {
    expect(() => synth(configWith({
      ...v4Config(),
      onlineEvaluation: { provisioned: false, samplingPercentage: 100, sessionTimeoutMinutes: 1 },
    }))).toThrow(/requires online evaluation and Transaction Search/);
  });
});
