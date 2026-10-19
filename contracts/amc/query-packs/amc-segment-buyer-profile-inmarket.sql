WITH touches AS (
  /* exposure-set start */
  SELECT user_id, user_id_type, 'dsp' AS exposure_source, 'amazon_dsp' AS ad_product_type,
         impression_dt_hour_utc AS touch_hour, impressions AS imps,
         total_cost AS dsp_cost_raw, 0 AS sponsored_spend_raw, currency_iso_code AS currency
  FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P0D', 'P14D'))
  WHERE user_id IS NOT NULL AND impressions > 0
    AND campaign_country = CUSTOM_PARAMETER('dsp_campaign_country')
  UNION ALL
  SELECT user_id, user_id_type, 'sponsored', ad_product_type,
         event_dt_hour_utc, impressions, 0, spend, currency_iso_code
  FROM TABLE(EXTEND_TIME_WINDOW('sponsored_ads_traffic', 'P0D', 'P14D'))
  WHERE user_id IS NOT NULL AND ad_product_type IS NOT NULL
    AND (impressions > 0 OR spend > 0)
    AND marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  /* exposure-set end */
),
exposed AS (
  /* after the fence: Amazon account IDs with an impression in the period (design 5.5; P-BO-4 step 2 pending) */
  SELECT DISTINCT user_id
  FROM touches
  WHERE user_id_type = 'adUserId'
    AND imps > 0
    AND touch_hour < BUILT_IN_PARAMETER('TIME_WINDOW_END')
),
snapshot AS (
  /* one region, one category table, one snapshot: the month's first Thursday, a 2-day UTC range */
  SELECT user_id, segment_id
  FROM audience_segments_amer_inmarket_snapshot
  WHERE segment_marketplace_id = CUSTOM_PARAMETER('segment_marketplace_id')
    AND snapshot_datetime >= CAST(CUSTOM_PARAMETER('snapshot_start') AS TIMESTAMP)
    AND snapshot_datetime < CAST(CUSTOM_PARAMETER('snapshot_end') AS TIMESTAMP)
),
profiled AS (
  /* exposed shoppers present in at least one row of the snapshot */
  SELECT DISTINCT e.user_id FROM exposed e JOIN snapshot s ON s.user_id = e.user_id
),
buyers AS (
  /* profiled shoppers with a purchase in the month: an inner join, so buyers are a subset */
  SELECT c.user_id, MAX(CASE WHEN c.new_to_brand THEN 1 ELSE 0 END) AS ntb
  FROM conversions c JOIN profiled p ON p.user_id = c.user_id
  WHERE c.event_category = 'purchase'
    AND c.marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  GROUP BY c.user_id
)
SELECT 'segment' AS row_kind, s.segment_id,
  COUNT(DISTINCT s.user_id) AS segment_exposed_shoppers,
  COUNT(DISTINCT b.user_id) AS segment_buyers,
  COUNT(DISTINCT CASE WHEN b.ntb = 1 THEN b.user_id END) AS segment_buyers_new_to_brand,
  NULL AS profiled_exposed_total, NULL AS profiled_buyers_total,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM snapshot s JOIN profiled p ON p.user_id = s.user_id
LEFT JOIN buyers b ON b.user_id = s.user_id
GROUP BY s.segment_id
UNION ALL
SELECT 'profiled_total' AS row_kind, NULL AS segment_id,
  NULL AS segment_exposed_shoppers, NULL AS segment_buyers, NULL AS segment_buyers_new_to_brand,
  COUNT(DISTINCT p.user_id) AS profiled_exposed_total,
  COUNT(DISTINCT b.user_id) AS profiled_buyers_total,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM profiled p LEFT JOIN buyers b ON b.user_id = p.user_id
