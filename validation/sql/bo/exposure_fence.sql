touches AS (
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
)
