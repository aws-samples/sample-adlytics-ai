SELECT
  event_date_utc,
  ad_product_type,
  CASE
    WHEN ad_product_type IN ('sponsored_products', 'sponsored_brands') THEN 'search'
    WHEN ad_product_type = 'sponsored_television' THEN 'video'
    ELSE 'display'
  END AS channel_group_key,
  marketplace_name,
  attribution_model_key,
  SUM(conversion_credit) AS matched_purchase_credit,
  SUM(credited_revenue) AS revenue_attributed_deduplicated_incl_halo,
  purchase_currency,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM (
  WITH conversion_events AS (
    SELECT
      conversion_id,
      user_id,
      event_dt_utc AS conversion_time,
      event_date_utc AS event_date_utc,
      marketplace_name AS marketplace_name,
      total_product_sales AS conversion_revenue,
      purchase_currency
    FROM conversions
    WHERE event_category = 'purchase'
      AND user_id IS NOT NULL
      AND marketplace_name IS NOT NULL
      AND total_product_sales IS NOT NULL
      AND purchase_currency IS NOT NULL
    GROUP BY
      conversion_id,
      user_id,
      event_dt_utc,
      event_date_utc,
      marketplace_name,
      total_product_sales,
      purchase_currency
  ),
  traffic_events AS (
    SELECT
      user_id,
      campaign_id_string,
      'amazon_dsp' AS ad_product_type,
      impression_dt_utc AS touch_time,
      'impression' AS touch_type
    FROM TABLE(EXTEND_TIME_WINDOW('dsp_impressions', 'P14D', 'P0D'))
    WHERE user_id IS NOT NULL
      AND campaign_id_string IS NOT NULL
    UNION ALL
    SELECT
      user_id,
      campaign_id_string,
      'amazon_dsp' AS ad_product_type,
      click_dt_utc AS touch_time,
      'click' AS touch_type
    FROM TABLE(EXTEND_TIME_WINDOW('dsp_clicks', 'P14D', 'P0D'))
    WHERE user_id IS NOT NULL
      AND campaign_id_string IS NOT NULL
    UNION ALL
    SELECT
      user_id,
      campaign_id_string,
      ad_product_type,
      event_dt_utc AS touch_time,
      CASE WHEN clicks > 0 THEN 'click' ELSE 'impression' END AS touch_type
    FROM TABLE(EXTEND_TIME_WINDOW('sponsored_ads_traffic', 'P14D', 'P0D'))
    WHERE user_id IS NOT NULL
      AND campaign_id_string IS NOT NULL
      AND ad_product_type IS NOT NULL
      AND (clicks > 0 OR impressions > 0)
  ),
  matched AS (
    SELECT
      c.conversion_id,
      c.event_date_utc,
      c.marketplace_name,
      c.conversion_revenue,
      c.purchase_currency,
      t.campaign_id_string,
      t.ad_product_type,
      t.touch_time,
      t.touch_type
    FROM conversion_events c
    JOIN traffic_events t ON c.user_id = t.user_id
    WHERE SECONDS_BETWEEN(t.touch_time, c.conversion_time)
          BETWEEN 0 AND 14 * 24 * 60 * 60
  ),
  ranked AS (
    SELECT
      conversion_id,
      event_date_utc,
      marketplace_name,
      conversion_revenue,
      purchase_currency,
      campaign_id_string,
      ad_product_type,
      ROW_NUMBER() OVER (
        PARTITION BY conversion_id
        ORDER BY touch_time ASC, touch_type ASC, campaign_id_string ASC
      ) AS first_rank,
      ROW_NUMBER() OVER (
        PARTITION BY conversion_id
        ORDER BY touch_time DESC, touch_type ASC, campaign_id_string ASC
      ) AS last_rank,
      COUNT(campaign_id_string) OVER (PARTITION BY conversion_id) AS touch_count
    FROM matched
  ),
  credited AS (
    SELECT
      event_date_utc,
      ad_product_type,
      marketplace_name,
      purchase_currency,
      'first_touch_v1' AS attribution_model_key,
      1.0 AS conversion_credit,
      conversion_revenue AS credited_revenue
    FROM ranked
    WHERE first_rank = 1
    UNION ALL
    SELECT
      event_date_utc,
      ad_product_type,
      marketplace_name,
      purchase_currency,
      'last_touch_v1' AS attribution_model_key,
      1.0 AS conversion_credit,
      conversion_revenue AS credited_revenue
    FROM ranked
    WHERE last_rank = 1
    UNION ALL
    SELECT
      event_date_utc,
      ad_product_type,
      marketplace_name,
      purchase_currency,
      'linear_v1' AS attribution_model_key,
      CASE
        WHEN first_rank = 1 THEN
          1.0 - (touch_count - 1) * (1.0 / touch_count)
        ELSE 1.0 / touch_count
      END AS conversion_credit,
      CASE
        WHEN first_rank = 1 THEN
          conversion_revenue
          - (touch_count - 1) * (conversion_revenue / touch_count)
        ELSE conversion_revenue / touch_count
      END AS credited_revenue
    FROM ranked
  )
  SELECT
    event_date_utc,
    ad_product_type,
    marketplace_name,
    purchase_currency,
    attribution_model_key,
    conversion_credit,
    credited_revenue
  FROM credited
)
GROUP BY
  event_date_utc,
  ad_product_type,
  CASE
    WHEN ad_product_type IN ('sponsored_products', 'sponsored_brands') THEN 'search'
    WHEN ad_product_type = 'sponsored_television' THEN 'video'
    ELSE 'display'
  END,
  marketplace_name,
  attribution_model_key,
  purchase_currency
