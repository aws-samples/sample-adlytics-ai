WITH seg_exposure AS (
  /* one row per campaign, matched segment and shopper; an impression repeats once per matched segment */
  SELECT campaign_country, campaign_id_string, behavior_segment_id, user_id,
         SUM(impressions) AS imps, SUM(total_cost) AS cost_raw,
         MAX(currency_iso_code) AS currency, MIN(currency_iso_code) AS currency_min
  FROM dsp_impressions_by_matched_segments
  WHERE user_id IS NOT NULL
    AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')
  GROUP BY campaign_country, campaign_id_string, behavior_segment_id, user_id
),
dsp_outcomes AS (
  /* DSP only, per shopper and campaign, so one campaign's segment row never takes another's purchases */
  SELECT user_id, campaign_id_string, SUM(purchases) AS purchases,
         SUM(product_sales) AS revenue, MAX(purchase_currency) AS purchase_currency,
         MIN(purchase_currency) AS purchase_currency_min
  FROM amazon_attributed_events_by_conversion_time
  WHERE ad_product_type IS NULL AND conversion_event_category = 'purchase' AND user_id IS NOT NULL
    AND conversion_event_marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  GROUP BY user_id, campaign_id_string
)
SELECT s.campaign_country, s.campaign_id_string, s.behavior_segment_id,
  SUM(s.imps) AS impressions, SUM(s.cost_raw) AS dsp_spend,
  MAX(s.currency) AS dsp_currency, MIN(s.currency_min) AS dsp_currency_min,
  COUNT(DISTINCT s.user_id) AS segment_reach,
  COUNT(DISTINCT CASE WHEN o.purchases > 0 THEN s.user_id END) AS segment_purchasers,
  SUM(o.purchases) AS segment_purchases, SUM(o.revenue) AS segment_attributed_revenue,
  MAX(o.purchase_currency) AS purchase_currency,
  MIN(o.purchase_currency_min) AS purchase_currency_min,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM seg_exposure s
LEFT JOIN dsp_outcomes o ON o.user_id = s.user_id AND o.campaign_id_string = s.campaign_id_string
GROUP BY s.campaign_country, s.campaign_id_string, s.behavior_segment_id
