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
firsts AS (
  /* the first exposure must fall inside the period; the extension only completes the window */
  SELECT user_id, ad_product_type, MIN(touch_hour) AS first_exposure_hour
  FROM touches
  WHERE imps > 0
    AND touch_hour < BUILT_IN_PARAMETER('TIME_WINDOW_END')
  GROUP BY user_id, ad_product_type
),
w_touches AS (
  SELECT t.user_id, t.user_id_type, t.exposure_source, t.ad_product_type, t.touch_hour,
         t.imps, t.dsp_cost_raw, t.sponsored_spend_raw, t.currency, f.first_exposure_hour
  FROM touches t JOIN firsts f ON f.user_id = t.user_id AND f.ad_product_type = t.ad_product_type
  WHERE t.touch_hour >= f.first_exposure_hour
    AND ABS(SECONDS_BETWEEN(f.first_exposure_hour, t.touch_hour)) < 1209600
),
grouped_p AS (
  SELECT user_id, ad_product_type, MIN(first_exposure_hour) AS first_exposure_hour,
         CASE WHEN MAX(user_id_type) = 'adUserId' THEN 'account'
              ELSE 'device_or_browser' END AS identity_space,
         SUM(imps) AS imps, SUM(dsp_cost_raw) AS dsp_cost_raw,
         SUM(sponsored_spend_raw) AS sponsored_spend_raw,
         MAX(CASE WHEN exposure_source = 'dsp' THEN currency END) AS dsp_currency,
         MIN(CASE WHEN exposure_source = 'dsp' THEN currency END) AS dsp_currency_min,
         MAX(CASE WHEN exposure_source = 'sponsored' THEN currency END) AS sponsored_currency,
         MIN(CASE WHEN exposure_source = 'sponsored' THEN currency END) AS sponsored_currency_min
  FROM w_touches GROUP BY user_id, ad_product_type
),
bucketed_p AS (
  SELECT g.user_id, g.ad_product_type, g.first_exposure_hour, g.identity_space, g.imps,
         g.dsp_cost_raw, g.sponsored_spend_raw, g.dsp_currency, g.dsp_currency_min,
         g.sponsored_currency, g.sponsored_currency_min,
         CASE WHEN g.imps = 1 THEN '1' WHEN g.imps = 2 THEN '2' WHEN g.imps = 3 THEN '3'
              WHEN g.imps BETWEEN 4 AND 6 THEN '4_6' WHEN g.imps BETWEEN 7 AND 9 THEN '7_9'
              WHEN g.imps >= 10 THEN '10_plus' END AS frequency_bucket
  FROM grouped_p g
),
outcome_events AS (
  /* event grain; conversions has one row per conversion_id */
  SELECT user_id, event_dt_hour_utc AS event_hour, event_category, event_subtype,
         new_to_brand, total_product_sales, purchase_currency
  FROM TABLE(EXTEND_TIME_WINDOW('conversions', 'P0D', 'P14D'))
  WHERE user_id IS NOT NULL
    AND marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
),
outcomes_p AS (
  SELECT b.user_id, b.ad_product_type,
    MAX(CASE WHEN o.event_category = 'purchase' THEN 1 ELSE 0 END) AS purchased,
    MAX(CASE WHEN o.event_category = 'purchase' AND o.new_to_brand THEN 1 ELSE 0 END) AS ntb,
    SUM(CASE WHEN o.event_category = 'purchase' THEN 1 ELSE 0 END) AS purchases,
    SUM(CASE WHEN o.event_category = 'purchase' THEN o.total_product_sales ELSE 0 END) AS revenue,
    MAX(CASE WHEN o.event_category = 'purchase' THEN o.purchase_currency END) AS purchase_currency,
    MIN(CASE WHEN o.event_category = 'purchase' THEN o.purchase_currency END) AS purchase_currency_min
  FROM bucketed_p b JOIN outcome_events o ON o.user_id = b.user_id
  WHERE o.event_hour >= b.first_exposure_hour
    AND ABS(SECONDS_BETWEEN(b.first_exposure_hour, o.event_hour)) < 1209600
  GROUP BY b.user_id, b.ad_product_type
)
SELECT b.ad_product_type, b.frequency_bucket, b.identity_space, 'coarse' AS bucket_scheme,
  'window' AS row_kind,
  COUNT(DISTINCT b.user_id) AS shoppers,
  SUM(b.imps) AS impressions, SUM(b.dsp_cost_raw) AS dsp_window_spend,
  SUM(b.sponsored_spend_raw) AS sponsored_window_spend,
  MAX(b.dsp_currency) AS dsp_currency, MIN(b.dsp_currency_min) AS dsp_currency_min,
  MAX(b.sponsored_currency) AS sponsored_currency,
  MIN(b.sponsored_currency_min) AS sponsored_currency_min,
  COUNT(DISTINCT CASE WHEN o.purchased = 1 THEN b.user_id END) AS purchasers,
  COUNT(DISTINCT CASE WHEN o.ntb = 1 THEN b.user_id END) AS ntb_purchasers,
  SUM(o.purchases) AS purchases, SUM(o.revenue) AS revenue_incl_halo,
  MAX(o.purchase_currency) AS currency_code,
  MIN(o.purchase_currency_min) AS purchase_currency_min,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM bucketed_p b LEFT JOIN outcomes_p o
  ON b.user_id = o.user_id AND b.ad_product_type = o.ad_product_type
GROUP BY b.ad_product_type, b.frequency_bucket, b.identity_space
