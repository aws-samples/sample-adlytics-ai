// SPDX-License-Identifier: MIT-0
/**
 * Fictional `ReachAndFunnelArtifact` v1 fixtures. No value describes a real customer.
 *
 * These stay hand-built, not serializer bytes. Each is one designed state (an absence reason,
 * preset, outcome or scheme) that the decoder and view tests mutate further, and several (no
 * shoppers, no sales, no spend, currency mismatch, no prior, change unavailable) have no state in
 * the backend's designed-state matrix (`tests/unit/test_reach.py`), so converting them means new
 * backend states plus an export step per fixture. They insert keys in roster order, so they
 * can't catch a wire-order dependence on their own. The wire
 * encoding is pinned by the real-bytes fixtures instead: `reach-month-coarse-wire.json` (the
 * bake serializer's sorted keys, pinned byte-for-byte by `test_reach.py`) and
 * `reach-served-week-v2.json` (the handler's served envelope, pinned by
 * `test_reach_presentation.py`).
 */

type Json = Record<string, unknown>;

export const cloneReachFixture = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

const figure = (value: string, unit: 'count' | 'currency' | 'multiplier' | 'share', displayText: string, currencyCode: string | null = null): Json => ({
  value,
  unit,
  currency_code: currencyCode,
  display_text: displayText,
});

const absent = (reason: string, text: string): Json => ({ absence: { reason, text } });
const change = (value: string, text: string): Json => ({ value, text });
const absentChange = (reason: string, text: string): Json => absent(reason, text);

const GROUPS = [
  ['dsp_only', 'Saw DSP only'],
  ['sponsored_only', 'Saw sponsored ads only'],
  ['dsp_and_sponsored', 'Saw both'],
] as const;
const TILES = [
  ['overlap_share', "Saw both within 14 days of their first ad · Amazon account IDs · a lower bound: DSP impressions Amazon couldn't link to an account aren't matched", '30.00%'],
  ['overlap_share_all_ids', 'Saw both within 14 days of their first ad · all ID types · overlap is understated', '25.93%'],
  ['dsp_only_reach_share', 'Saw DSP only within 14 days of their first ad · Amazon account IDs', '20.00%'],
] as const;
const FUNNEL_COLUMNS = [
  ['search_rate', 'Search rate'],
  ['purchaser_rate', 'Purchaser rate'],
  ['revenue_per_exposed_shopper', 'Sales per shopper reached'],
  ['purchaser_rate_index_vs_sponsored_only', 'Purchaser rate index vs sponsored ads only'],
] as const;
const FUNNEL_CAVEAT = [
  'This shows association, not cause. Shoppers were not randomly assigned to groups.',
  'Shoppers can see an ad after they buy, so groups that include DSP retargeting may look stronger.',
  'Sponsored ads appear when shoppers search, so sponsored groups start closer to buying.',
];
const FREQUENCY_CAVEAT = [
  ...FUNNEL_CAVEAT,
  'Shoppers can see more ads after they buy, so higher buckets may look stronger.',
];
const BUCKETS = {
  fine: [
    ['1', '1 ad'], ['2', '2 ads'], ['3', '3 ads'], ['4', '4 ads'], ['5', '5 ads'],
    ['6', '6 ads'], ['7', '7 ads'], ['8', '8 ads'], ['9', '9 ads'], ['10_plus', '10 or more'],
  ],
  coarse: [
    ['1', '1 ad'], ['2', '2 ads'], ['3', '3 ads'], ['4_6', '4 to 6'],
    ['7_9', '7 to 9'], ['10_plus', '10 or more'],
  ],
} as const;
const AXIS = {
  coarse: 'Ads seen within 14 days of the first ad, in coarse buckets: 1, 2, 3, 4 to 6, 7 to 9, then 10 or more',
  fine: 'Ads seen within 14 days of the first ad: 1 to 9, then 10 or more',
} as const;

const REACH_ARTIFACT_REVISION = 'r'.repeat(43);

const filledInsight = (id: string, text: string): Json => ({
  artifact_revision: REACH_ARTIFACT_REVISION,
  evidence_line: 'Found by a fixed rule from the figures above. Not written by an AI model.',
  id,
  output_class: 'perspective',
  prompt_version: 'deterministic-v1',
  state: 'filled',
  text,
});

const silentInsight = (id: string): Json => ({
  absence_class: 'evaluated-quiet',
  absence_reason: 'No fictional finding cleared the declared threshold.',
  artifact_revision: REACH_ARTIFACT_REVISION,
  evidence_line: null,
  id,
  output_class: 'perspective',
  prompt_version: 'deterministic-v1',
  state: 'silent',
  text: null,
});

function insights(label: string, states: readonly boolean[] = [true, true, true]): Json[] {
  const values = [
    ['reach-overlap', `Among Amazon account IDs reached in ${label}, DSP-only reach was associated with the served overlap.`],
    ['full-funnel-association', `Among Amazon account IDs reached in ${label}, the served purchaser rates show association, not cause.`],
    ['frequency-saturation', `Among Amazon account IDs reached in ${label}, the served frequency curve stopped clearly rising after 3 ads.`],
  ] as const;
  return values.map(([id, slotText], index) => states[index] ? filledInsight(id, slotText) : silentInsight(id));
}

function overlap(label: string, grain: 'month' | 'week'): Json {
  return {
    state: 'figures',
    text: null,
    all_ids_label: 'All ID types',
    subtitle: `Shoppers reached in ${label}, grouped by the ads they saw within 14 days of their first ad in that ${grain}.`,
    window_notes: [
      "These groups use each shopper's 14-day window, so they don't match Amazon's own period reach reports.",
      `Ads seen before the ${grain} began aren't counted.`,
    ],
    tiles: TILES.map(([metric, tileLabel, displayText], index) => ({
      metric,
      label: tileLabel,
      current: figure(index === 1 ? '0.2593' : index === 2 ? '0.2000' : '0.3000', 'share', displayText),
      prior: figure(index === 1 ? '0.2500' : index === 2 ? '0.2100' : '0.2900', 'share', index === 1 ? '25.00%' : index === 2 ? '21.00%' : '29.00%'),
      change: change(index === 2 ? '-1.00' : index === 1 ? '0.93' : '1.00', index === 2 ? '−1.00 points' : index === 1 ? '+0.93 points' : '+1.00 points'),
    })),
    groups: GROUPS.map(([exposureGroup, groupLabel], index) => ({
      exposure_group: exposureGroup,
      label: groupLabel,
      account: figure(String((index + 2) * 1000), 'count', `${index + 2}000 served`),
      all_ids: figure(String((index + 2) * 1250), 'count', `${index + 2}250 served`),
      prior_account: figure(String((index + 2) * 900), 'count', `${index + 2}900 served`),
      change: change('100', '+100'),
    })),
  };
}

function funnel(): Json {
  return {
    state: 'figures',
    text: null,
    caveat: [...FUNNEL_CAVEAT],
    columns: FUNNEL_COLUMNS.map(([metric, label]) => ({ metric, label })),
    rows: GROUPS.map(([exposureGroup, label], index) => ({
      exposure_group: exposureGroup,
      label,
      figures: {
        search_rate: figure(`0.${index + 1}500`, 'share', `${index + 1}5.00%`),
        purchaser_rate: figure(`0.0${index + 4}00`, 'share', `${index + 4}.00%`),
        revenue_per_exposed_shopper: figure(`${index + 2}.5000`, 'currency', `${index + 2}.50 USD`, 'USD'),
        purchaser_rate_index_vs_sponsored_only: figure(`${index + 1}.0000`, 'multiplier', `${index + 1}.00x`),
      },
    })),
    index_note: 'may understate the difference between groups',
  };
}

function frequency(label: string, scheme: 'coarse' | 'fine'): Json {
  const buckets = BUCKETS[scheme];
  const cells = Object.fromEntries(buckets.map(([bucket]) => [
    bucket,
    absent('not_available_per_channel', 'Not available per channel'),
  ]));
  return {
    state: 'figures',
    text: null,
    expected_after: null,
    caveat: [...FREQUENCY_CAVEAT],
    bucket_scheme: scheme,
    axis_label: AXIS[scheme],
    buckets: buckets.map(([bucket, bucketLabel], index) => ({
      bucket,
      label: bucketLabel,
      // The first exact values deliberately disagree with their display text. Rendering tests
      // must keep "4.00%" and "2.50x", never derive text from these decimals.
      purchaser_rate: figure(index === 0 ? '0.9876' : `0.0${index + 4}00`, 'share', index === 0 ? '4.00%' : `${index + 4}.00%`),
      platform_return: figure(index === 0 ? '9.8765' : `${index + 2}.5000`, 'multiplier', index === 0 ? '2.50x' : `${index + 2}.50x`),
    })),
    per_channel: {
      label: `Credited revenue ÷ spend · linear model · 14-day lookback from each shopper's first ad · includes brand halo · ${label}`,
      unavailable_reason: "one purchase can follow ads on several channels, and we can't split its credit fairly here. See the platform view.",
      foot: "Attributed sales aren't added across channels, because one purchase can be credited to more than one channel.",
      platform_label: 'Platform view · sales ÷ spend on these shoppers in their 14-day window · all ads · includes brand halo · Amazon account IDs',
      rows: [
        { ad_product_key: 'sponsored_products', label: 'Sponsored Products', cells: cloneReachFixture(cells) },
        { ad_product_key: 'amazon_dsp', label: 'Amazon DSP', cells: cloneReachFixture(cells) },
      ],
    },
  };
}

function unavailableFrequency(state: 'needs_campaigns' | 'outside_declared_periods' | 'pending', label: string): Json {
  const copy = state === 'needs_campaigns'
    ? 'Needs at least 4 active campaigns'
    : state === 'outside_declared_periods'
      ? 'Frequency is measured for complete months and quarters. Choose Latest measured month to see it.'
      : `Figures for ${label} are expected after 16 Oct 2026.`;
  return {
    state,
    text: copy,
    expected_after: state === 'pending' ? '2026-10-16' : null,
    caveat: [],
    bucket_scheme: null,
    axis_label: null,
    buckets: [],
    per_channel: null,
  };
}

interface ArtifactOptions {
  readonly end: string;
  readonly frequencyState?: 'figures' | 'needs_campaigns' | 'outside_declared_periods' | 'pending';
  readonly grain: 'month' | 'quarter' | 'week';
  readonly label: string;
  readonly marketOutcome?: 'complete' | 'empty' | 'insufficient';
  readonly preset: 'last_complete_month' | 'last_complete_quarter' | 'last_complete_week';
  readonly scheme?: 'coarse' | 'fine';
  readonly start: string;
}

function artifact(options: ArtifactOptions): Json {
  const marketOutcome = options.marketOutcome ?? 'complete';
  const frequencyState = options.frequencyState ?? 'figures';
  const hasOverlap = marketOutcome === 'complete';
  const quarter = options.grain === 'quarter';
  const overlapSection = !hasOverlap ? null : quarter ? {
    state: 'outside_declared_periods',
    text: 'Overlap is measured for complete weeks and months. Choose Latest measured month to see it.',
    all_ids_label: 'All ID types',
    subtitle: null,
    window_notes: [],
    tiles: [],
    groups: [],
  } : overlap(options.label, options.grain);
  const funnelSection = !hasOverlap ? null : quarter ? {
    state: 'outside_declared_periods',
    text: 'Full funnel is measured for complete weeks and months. Choose Latest measured month to see it.',
    caveat: [],
    columns: [],
    rows: [],
    index_note: null,
  } : funnel();
  const frequencySection = frequencyState === 'figures'
    ? frequency(options.label, options.scheme ?? 'fine')
    : unavailableFrequency(frequencyState, options.label);
  const slotStates = [hasOverlap && !quarter, hasOverlap && !quarter, frequencyState === 'figures'];
  const market = {
    marketplace_key: 'fictional_market',
    outcome: marketOutcome,
    overlap: overlapSection,
    funnel: funnelSection,
    frequency: frequencySection,
    insights: insights(options.label, slotStates),
  };
  const pending = frequencyState === 'pending';
  return {
    type: 'ReachAndFunnelArtifact',
    version: 1,
    outcome: marketOutcome,
    preset: options.preset,
    period: { start: options.start, end: options.end, grain: options.grain, label: options.label },
    markets: [market],
    boundary: [
      'Figures are for complete weeks, months or quarters only.',
      'Rates read Amazon account IDs only.',
      'This shows association, not cause.',
    ],
    evidence: {
      pre_exposure_exclusion: 'pending_step_b',
      fence_check: 'structural check, not the exact pack text',
    },
    evidence_status: {
      version: 2,
      slots: pending ? [{
        marketplace_key: 'fictional_market',
        slot_label: 'Frequency',
        role_label: 'This period',
        missing_dates: [{ start: options.start, end: options.end }],
        reason_text: (frequencySection as Json).text,
        recovery_text: null,
      }] : [],
    },
    page_context_label: `Reach and funnel · ${options.label}`,
    page_context_expires_at: null,
    starter_questions: [
      `Which exposure group's purchaser rate was highest in ${options.label}, and what is it associated with?`,
    ],
  };
}

const MONTH: ArtifactOptions = {
  end: '2026-09-30',
  grain: 'month',
  label: 'September 2026',
  preset: 'last_complete_month',
  start: '2026-09-01',
};

export const reachCompleteFineFixture = artifact(MONTH);
export const reachCompleteCoarseFixture = artifact({ ...MONTH, scheme: 'coarse' });
export const reachWeekFixture = artifact({
  end: '2026-09-20',
  frequencyState: 'outside_declared_periods',
  grain: 'week',
  label: 'Week of 14–20 Sep 2026',
  preset: 'last_complete_week',
  start: '2026-09-14',
});
export const reachQuarterFixture = artifact({
  end: '2026-09-30',
  grain: 'quarter',
  label: 'Q3 2026',
  preset: 'last_complete_quarter',
  scheme: 'coarse',
  start: '2026-07-01',
});
export const reachPendingFixture = artifact({ ...MONTH, frequencyState: 'pending' });
export const reachNeedsCampaignsFixture = artifact({ ...MONTH, frequencyState: 'needs_campaigns' });

export const reachNeedsDspFixture = (() => {
  const result = artifact(MONTH);
  const market = (result.markets as Json[])[0] as Json;
  market.overlap = {
    state: 'needs_dsp', text: 'Needs DSP and sponsored ads on one AMC instance', all_ids_label: 'All ID types', subtitle: null,
    window_notes: [], tiles: [], groups: [],
  };
  market.funnel = {
    state: 'needs_dsp', text: 'Needs DSP and sponsored ads on one AMC instance', caveat: [],
    columns: [], rows: [], index_note: null,
  };
  market.insights = insights(MONTH.label, [false, false, true]);
  return result;
})();

export const reachNotReturnedFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const buckets = ((market.frequency as Json).buckets as Json[]);
  (buckets[3] as Json).purchaser_rate = absent('not_returned', 'too few shoppers, or none');
  (buckets[3] as Json).platform_return = absent('not_returned', 'too few shoppers, or none');
  return result;
})();

export const reachNoShoppersFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const tile = (((market.overlap as Json).tiles as Json[])[0] as Json);
  tile.current = absent('no_shoppers', 'No shoppers were reached in this group.');
  tile.change = absentChange('change_unavailable', "No change is shown, because a figure isn't available.");
  return result;
})();

export const reachNoSalesFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const row = (((market.funnel as Json).rows as Json[])[0] as Json);
  (row.figures as Json).revenue_per_exposed_shopper = absent('no_sales', 'No sales were recorded for this group.');
  return result;
})();

export const reachNoSpendFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const bucket = (((market.frequency as Json).buckets as Json[])[0] as Json);
  bucket.platform_return = absent('no_spend', 'No spend was recorded for these shoppers.');
  return result;
})();

export const reachCurrencyMismatchFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const bucket = (((market.frequency as Json).buckets as Json[])[0] as Json);
  bucket.platform_return = absent('currency_mismatch', "Spend and sales are in different currencies, so return isn't shown.");
  return result;
})();

export const reachNoPriorFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const overlapSection = market.overlap as Json;
  for (const tile of overlapSection.tiles as Json[]) {
    tile.prior = null;
    tile.change = absentChange('prior_unavailable', "The previous month isn't available.");
  }
  for (const group of overlapSection.groups as Json[]) {
    group.prior_account = null;
    group.change = absentChange('prior_unavailable', "The previous month isn't available.");
  }
  return result;
})();

export const reachChangeUnavailableFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const group = (((market.overlap as Json).groups as Json[])[0] as Json);
  group.prior_account = absent('not_returned', 'too few shoppers, or none');
  group.change = absentChange('change_unavailable', "No change is shown, because a figure isn't available.");
  return result;
})();

export const reachPackVariantMismatchFixture = (() => {
  const result = cloneReachFixture(reachCompleteFineFixture);
  const market = (result.markets as Json[])[0] as Json;
  const overlapSection = market.overlap as Json;
  for (const tile of overlapSection.tiles as Json[]) {
    tile.prior = null;
    tile.change = absentChange('pack_variant_mismatch', "No change is shown, because a figure isn't available.");
  }
  for (const group of overlapSection.groups as Json[]) {
    group.prior_account = null;
    group.change = absentChange('pack_variant_mismatch', "No change is shown, because a figure isn't available.");
  }
  return result;
})();

export const reachEmptyFixture = artifact({ ...MONTH, frequencyState: 'pending', marketOutcome: 'empty' });
export const reachInsufficientFixture = artifact({ ...MONTH, frequencyState: 'pending', marketOutcome: 'insufficient' });

function unavailableTemporal(reasonText: string): Json {
  return {
    version: 2,
    mode: 'selectable',
    presets: [
      { id: 'last_complete_week', label: 'Latest measured week', availability: 'available', reason_code: null, reason_text: null, window: { start: '2026-09-14', end: '2026-09-20' }, period_label: 'Week of 14–20 Sep 2026' },
      { id: 'last_complete_month', label: 'Latest measured month', availability: 'unavailable', reason_code: 'artifact_unavailable', reason_text: reasonText, window: { start: '2026-09-01', end: '2026-09-30' }, period_label: 'September 2026' },
      { id: 'last_complete_quarter', label: 'Latest measured quarter', availability: 'available', reason_code: null, reason_text: null, window: { start: '2026-07-01', end: '2026-09-30' }, period_label: 'Q3 2026' },
    ],
    custom: { availability: 'unavailable', bounds: null, reason_code: 'preset_artifacts_only' },
    comparison: { policy: 'prior_calendar_period', prior_period_label: 'August 2026', status: 'available' },
    resolved: {
      as_of_month: null,
      display_grain: 'month',
      period_label: 'September 2026',
      reporting_window_status: 'user_selected',
      selection: { kind: 'preset', preset_id: 'last_complete_month' },
      window: { start: '2026-09-01', end: '2026-09-30' },
    },
  };
}

export const reachNoArtifactPendingEnvelope = {
  envelope_version: 2,
  expected_after: null,
  kind: 'window_unavailable',
  message: 'Figures for September 2026 are expected after 16 Oct 2026.',
  reason_code: 'artifact_pending',
  recovery: { action_id: null, no_self_service_reason: 'September 2026 has not completed its publication cadence.' },
  session_id: 'fictional-session',
  temporal: unavailableTemporal("September 2026 isn't measured yet"),
} as const;

export const reachNoArtifactMissingEnvelope = {
  envelope_version: 2,
  expected_after: null,
  kind: 'window_unavailable',
  message: "Figures for September 2026 didn't arrive. The latest available is August 2026.",
  reason_code: 'artifact_missing',
  recovery: { action_id: null, no_self_service_reason: 'September 2026 has no current publication.' },
  session_id: 'fictional-session',
  temporal: unavailableTemporal("September 2026 didn't arrive"),
} as const;
