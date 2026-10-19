// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import { Construct } from 'constructs';

import type { AppConfig } from '../../config';

/**
 * The Bedrock guardrail the conversation route applies to each inbound question.
 *
 * Layer 1 of [ADR-041](../../../../docs/decisions/adr-041-guardrails-are-policy-at-the-gateway-and-applyguardrail-in-the-route.md),
 * as amended on 2026-09-30. ADR-083 retired the outbound check, so this guardrail assesses only
 * the question, before the Harness runs.
 *
 * **Why the route and not the model call.** Managed Harness performs model work under its own
 * execution role, while the route owns the inbound question. `ApplyGuardrail` is applied there,
 * before the token exchange and the Harness, and cannot be weakened by a model request.
 *
 * **What it does not cover.** The prompt-attack filter does not evaluate tool results, so it is
 * no defence against injection carried in data (G-05).
 *
 * **Standard tier, through the geography's guardrail profile.** Calibrated on 2026-09-30 against the
 * fictional set in `validation/guardrail-calibration-cases.json`: the Classic tier at HIGH blocked
 * 7 of 38 benign questions and missed 3 of 16 attacks, while Standard with prompt attack and
 * misconduct at LOW blocked none and missed 1. Standard is the only tier that detects
 * system-prompt leakage, and it requires a guardrail profile; assessments run within the
 * profile's geography in this account. `make accept-guardrail` replays the set after every deploy.
 *
 * **No contextual grounding filter.** The route holds no grounding source for a question, and a
 * filter without one would report success because it evaluated nothing.
 */
export interface RouteGuardrailOptions {
  readonly config: AppConfig;
  /** Explicit physical name keeps the API legacy bridge and Agent canonical owner distinct. */
  readonly physicalName: string;
}

/**
 * Harmful-content categories, set by calibration rather than by default.
 *
 * `MEDIUM` for violence, hate, sexual content, and insults. This is a measurement product answering
 * questions about advertising performance, so the realistic risk is a false positive on ordinary
 * commercial language, not a genuine safety failure. A filter that blocks questions about ad spend
 * is a filter someone turns off.
 *
 * `MISCONDUCT` at `LOW` on input, which blocks only high-confidence detections. On the Standard
 * tier "which campaigns should we kill" and "attack the category with aggressive bidding" score
 * MEDIUM misconduct confidence (calibration, 2026-09-30).
 *
 * `PROMPT_ATTACK` at `LOW` on input. At `HIGH`, every low-confidence detection was blocked,
 * including benign formatting and correction requests such as "Reply with one short sentence
 * only."; a direct attack by an entitled user reaches only that user's own session, because tools
 * verify identity and scope on the server (ADR-077, ADR-054). Its output strength must be `NONE`:
 * Bedrock rejects a prompt-attack filter on the output side. Output strengths are otherwise unused,
 * since the route assesses only the question.
 */
const CONTENT_FILTERS: readonly { type: string; input: string; output: string }[] = [
  { type: 'VIOLENCE', input: 'MEDIUM', output: 'MEDIUM' },
  { type: 'HATE', input: 'MEDIUM', output: 'MEDIUM' },
  { type: 'SEXUAL', input: 'MEDIUM', output: 'MEDIUM' },
  { type: 'INSULTS', input: 'MEDIUM', output: 'MEDIUM' },
  { type: 'MISCONDUCT', input: 'LOW', output: 'MEDIUM' },
  { type: 'PROMPT_ATTACK', input: 'LOW', output: 'NONE' },
];

/** The safeguard tier for content filters and prompt attacks. */
export const GUARDRAIL_TIER = 'STANDARD';

/** One geography's guardrail profile and the Regions it may route an assessment to. */
interface GuardrailProfile {
  readonly id: string;
  readonly destinations: readonly string[];
}

const US_PROFILE: GuardrailProfile = {
  id: 'us.guardrail.v1:0',
  destinations: ['us-east-1', 'us-east-2', 'us-west-2'],
};
const EU_PROFILE: GuardrailProfile = {
  id: 'eu.guardrail.v1:0',
  destinations: ['eu-central-1', 'eu-west-1', 'eu-west-3', 'eu-north-1', 'eu-south-1', 'eu-south-2'],
};

/**
 * The guardrail profile for each source Region this deployment is declared for, with the
 * destination Regions AWS lists for it
 * ([guardrail profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-cross-region-support.html),
 * read 2026-09-30; the US rows re-read 2026-10-01). A destination added later is outside the
 * grant, so an assessment routed there is denied and the route fails closed. The profile keeps an assessment inside its geography; the caller needs
 * `ApplyGuardrail` on the profile in every destination. A Region absent here fails synth rather
 * than guessing a geography.
 */
const GUARDRAIL_PROFILES: Readonly<Record<string, GuardrailProfile>> = {
  'us-east-1': US_PROFILE,
  'us-east-2': US_PROFILE,
  'us-west-2': US_PROFILE,
  'eu-central-1': EU_PROFILE,
  'eu-west-1': EU_PROFILE,
};

function profileFor(region: string): GuardrailProfile {
  const profile = GUARDRAIL_PROFILES[region];
  if (profile === undefined) {
    throw new Error(`No guardrail profile is declared for source Region ${region}.`);
  }
  return profile;
}

/** The guardrail profile ARN the guardrail in `region` runs through. */
export function guardrailProfileArn(partition: string, region: string, account: string): string {
  return `arn:${partition}:bedrock:${region}:${account}:guardrail-profile/${profileFor(region).id}`;
}

/** The guardrail profile ARNs a caller in `region` must be allowed to apply through. */
export function guardrailProfileArns(partition: string, region: string, account: string): string[] {
  const profile = profileFor(region);
  return profile.destinations.map(
    (destination) =>
      `arn:${partition}:bedrock:${destination}:${account}:guardrail-profile/${profile.id}`,
  );
}

/**
 * The identifier classes a question must never carry, and `BLOCK` rather than `ANONYMIZE`.
 *
 * **Chosen so that a detection is a finding rather than noise.** Every one is a credential or a
 * direct personal identifier, and none has any business appearing in a question about aggregated
 * advertising measurement. Blocking them on the question also keeps them out of long-term Memory
 * and the Harness's own logs, which retain what the question carries.
 *
 * `NAME` and `ADDRESS` are available and deliberately absent. Campaign names, brand names and ASIN
 * titles are the substance of what this product returns, and a filter firing on every one of them
 * produces a stream of interventions nobody reads — which is worse than no filter, because it looks
 * like coverage.
 *
 * `BLOCK`, not `ANONYMIZE`. Masking would send the model a question with tags in place of what the
 * user wrote, which it would then answer as if the user had written the tags.
 */
const PII_ENTITIES: readonly string[] = [
  'CREDIT_DEBIT_CARD_NUMBER',
  'US_SOCIAL_SECURITY_NUMBER',
  'EMAIL',
  'PHONE',
  'AWS_ACCESS_KEY',
  'AWS_SECRET_KEY',
  'PASSWORD',
];

/**
 * Create the route guardrail under an explicit physical name.
 *
 * The caller supplies the physical name because a guardrail cannot be renamed in place, and the
 * deployed name carries the `-agent` suffix from the ownership bridge that preceded it.
 *
 * **No `CfnGuardrailVersion`.** `ApplyGuardrail` accepts `DRAFT` — its `guardrailVersion` pattern is
 * `(|([1-9][0-9]{0,7})|(DRAFT))` — so a version resource is optional here, and it would add a second
 * thing that can lag behind the first. Nothing in this deployment wants to run an older guardrail
 * than the one it just deployed, and everything else in this repository treats the deploy as the
 * statement of what is live. Note the constraint does not generalise:
 * `CfnEnforcedGuardrailConfiguration` documents its version as numerical and not `DRAFT`.
 */
export function routeGuardrail(
  scope: Construct,
  options: RouteGuardrailOptions,
): bedrock.CfnGuardrail {
  const { config, physicalName } = options;
  const stack = cdk.Stack.of(scope);

  return new bedrock.CfnGuardrail(scope, 'RouteGuardrail', {
    name: physicalName,
    description:
      'Applied by the conversation route to each inbound question. ADR-041 (amended 2026-09-30), ADR-083.',
    // Required properties, and they are deliberately not the text a user sees. The route never
    // relays these: an intervention becomes a `REFUSED` event whose wording the domain owns, so
    // that one voice explains a withheld answer. These exist because the API requires them, and
    // they are written to be recognisable in a log if one ever does leak.
    blockedInputMessaging: 'This question was declined by a content guardrail.',
    blockedOutputsMessaging: 'This answer was withheld by a content guardrail.',
    contentPolicyConfig: {
      filtersConfig: CONTENT_FILTERS.map((filter) => ({
        type: filter.type,
        inputStrength: filter.input,
        outputStrength: filter.output,
      })),
      contentFiltersTierConfig: { tierName: GUARDRAIL_TIER },
    },
    crossRegionConfig: {
      // `config.region` rather than the stack's: it is the Region every application stack is
      // deployed to (`bin/app.ts`), and it is concrete at synth where an environment-agnostic
      // stack's Region is a token no geography can be chosen from.
      guardrailProfileArn: guardrailProfileArn(stack.partition, config.region, stack.account),
    },
    sensitiveInformationPolicyConfig: {
      piiEntitiesConfig: PII_ENTITIES.map((type) => ({ type, action: 'BLOCK' })),
    },
    // No `contextualGroundingPolicyConfig`. See the module docstring: there is no grounding source
    // for a question, and a filter with nothing to evaluate reports success.
  });
}
