// SPDX-License-Identifier: MIT-0
import type { PresentationResultV2 } from '../../lib/presentation-v2';

export const CAMPAIGN_PERIOD = { end: '2026-08-16', start: '2026-08-10' } as const;
export const CAMPAIGN_SELECTION_REFERENCE = 'c'.repeat(43);
export const CAMPAIGN_SESSION = 'conversation-campaign-fictional';

const temporal = {
  comparison: { policy: 'optional_equal_prior' as const, reason_code: null, status: 'not_requested' as const },
  custom: { availability: 'available' as const, bounds: { earliest_start: '2026-08-01', latest_end: '2026-08-21', max_duration_days: 90, min_duration_days: 1 }, reason_code: null },
  mode: 'selectable' as const,
  presets: [
    { availability: 'available' as const, id: '1d' as const, label: '1D' as const, reason_code: null },
    { availability: 'available' as const, id: '7d' as const, label: '7D' as const, reason_code: null },
    { availability: 'available' as const, id: '14d' as const, label: '14D' as const, reason_code: null },
    { availability: 'available' as const, id: '30d' as const, label: '30D' as const, reason_code: null },
  ],
  resolved: { display_grain: 'day' as const, reporting_window_status: 'preferred_default' as const, selection: { kind: 'default' as const, preset_id: null }, window: CAMPAIGN_PERIOD },
  version: 1 as const,
};

export function campaignListFixture(
  sessionId: string = CAMPAIGN_SESSION,
): PresentationResultV2 {
  return {
    envelope_version: 2,
    kind: 'page',
    page: {
      actions: [{ action_id: 'inspect-channel-context', target_route: '/channel-performance/' }],
      artifact: {
        choices: [{
          capability: { expires_at: '2099-08-17T12:00:00+00:00', selection_reference: CAMPAIGN_SELECTION_REFERENCE },
          signature: {
            channel: 'Sponsored Products',
            effective_name: 'Fictional summer campaign',
            effective_name_status: 'available',
            effective_name_valid_from: '2026-07-01',
            effective_name_valid_to: null,
            marketplace: 'United States',
            platform: 'Amazon Ads',
          },
        }],
        evidence: [{ actual_coverage: [CAMPAIGN_PERIOD], coverage_holes: [], freshness_qualifiers: [], requested_period: CAMPAIGN_PERIOD, source: 'Governed campaign catalog' }],
        kind: 'campaign_list',
        outcome: 'choices',
        period: CAMPAIGN_PERIOD,
        reason_codes: [],
        recovery_actions: [],
        truncated: false,
      },
      artifact_id: 'campaign_catalog',
      artifact_revision: 'a'.repeat(43),
      artifact_type: 'CampaignAttribution',
      artifact_version: 1,
      kind: 'campaign_list',
      page_context: {
        expires_at: '2099-08-17T12:00:00+00:00',
        label: 'Campaigns · Aug 15\u201321, 2026',
        reference: 'a'.repeat(43),
        starter_questions: [],
      },
      page_id: 'campaigns',
      route: '/campaigns/',
    },
    session_id: sessionId,
    temporal,
  };
}
