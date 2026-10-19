// SPDX-License-Identifier: MIT-0
/** H-IAM-AGENTCORE-CREATE-001: the tag conditions are derived from the one standard tag set. */
import { requestTagCondition, standardTags, tagOwnResourceCondition } from '../lib/mixins/tagging';
import { configWith, TEST_CONFIG } from './support/config';

describe('the request-tag conditions', () => {
  it('require exactly the standard tag set on the request', () => {
    expect(requestTagCondition(TEST_CONFIG)).toStrictEqual({
      StringEquals: {
        'aws:RequestTag/Application': 'adlytics-ai',
        'aws:RequestTag/Environment': TEST_CONFIG.envName,
        'aws:RequestTag/ManagedBy': 'cdk',
      },
      'ForAllValues:StringEquals': { 'aws:TagKeys': ['Application', 'Environment', 'ManagedBy'] },
    });
  });

  it('follow the standard tag set, so the environment is never a literal', () => {
    const other = configWith({ envName: 'other' });
    const condition = requestTagCondition(other);
    expect(condition.StringEquals).toStrictEqual(Object.fromEntries(
      Object.entries(standardTags(other)).map(([key, value]) => [`aws:RequestTag/${key}`, value]),
    ));
    expect(condition['ForAllValues:StringEquals']).toStrictEqual({ 'aws:TagKeys': Object.keys(standardTags(other)) });
    expect(tagOwnResourceCondition(other).StringEqualsIfExists).toStrictEqual({
      'aws:ResourceTag/Application': standardTags(other).Application,
      'aws:ResourceTag/Environment': 'other',
    });
  });

  it('add to TagResource only the guard against another application or environment', () => {
    const condition = tagOwnResourceCondition(TEST_CONFIG);
    expect(Object.keys(condition).sort())
      .toStrictEqual(['ForAllValues:StringEquals', 'StringEquals', 'StringEqualsIfExists']);
    const { StringEqualsIfExists: guard, ...request } = condition;
    expect(request).toStrictEqual(requestTagCondition(TEST_CONFIG));
    expect(guard).toStrictEqual({
      'aws:ResourceTag/Application': 'adlytics-ai',
      'aws:ResourceTag/Environment': TEST_CONFIG.envName,
    });
  });
});
