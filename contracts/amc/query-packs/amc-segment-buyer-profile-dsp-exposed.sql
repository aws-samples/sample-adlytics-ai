WITH snapshot AS (
  /* one row per DSP-exposed shopper and behaviour segment they belonged to at an impression in the period */
  SELECT user_id, behavior_segment_id
  FROM dsp_impressions_by_user_segments
  WHERE user_id IS NOT NULL AND impressions > 0
    AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')
  GROUP BY user_id, behavior_segment_id
),
profiled AS (
  /* both bases come from this one table, so no ID-space restriction is needed */
  SELECT DISTINCT user_id FROM snapshot
),
buyers AS (
  /* profiled shoppers with a purchase in the month: an inner join, so buyers are a subset */
  SELECT c.user_id, MAX(CASE WHEN c.new_to_brand THEN 1 ELSE 0 END) AS ntb
  FROM conversions c JOIN profiled p ON p.user_id = c.user_id
  WHERE c.event_category = 'purchase'
    AND c.marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  GROUP BY c.user_id
)
SELECT 'segment' AS row_kind, s.behavior_segment_id,
  COUNT(DISTINCT s.user_id) AS segment_exposed_shoppers,
  COUNT(DISTINCT b.user_id) AS segment_buyers,
  COUNT(DISTINCT CASE WHEN b.ntb = 1 THEN b.user_id END) AS segment_buyers_new_to_brand,
  NULL AS profiled_exposed_total, NULL AS profiled_buyers_total,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM snapshot s
LEFT JOIN buyers b ON b.user_id = s.user_id
GROUP BY s.behavior_segment_id
UNION ALL
SELECT 'profiled_total' AS row_kind, NULL AS behavior_segment_id,
  NULL AS segment_exposed_shoppers, NULL AS segment_buyers, NULL AS segment_buyers_new_to_brand,
  COUNT(DISTINCT p.user_id) AS profiled_exposed_total,
  COUNT(DISTINCT b.user_id) AS profiled_buyers_total,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM profiled p LEFT JOIN buyers b ON b.user_id = p.user_id
