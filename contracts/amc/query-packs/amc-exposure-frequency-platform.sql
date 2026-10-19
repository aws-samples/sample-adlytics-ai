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
  SELECT user_id, MIN(touch_hour) AS first_exposure_hour
  FROM touches
  WHERE imps > 0
    AND touch_hour < BUILT_IN_PARAMETER('TIME_WINDOW_END')
  GROUP BY user_id
),
w_touches AS (
  SELECT t.user_id, t.user_id_type, t.exposure_source, t.ad_product_type, t.touch_hour,
         t.imps, t.dsp_cost_raw, t.sponsored_spend_raw, t.currency, f.first_exposure_hour
  FROM touches t JOIN firsts f ON f.user_id = t.user_id
  WHERE t.touch_hour >= f.first_exposure_hour
    AND ABS(SECONDS_BETWEEN(f.first_exposure_hour, t.touch_hour)) < 1209600
),
exposed AS (
  /* one row per shopper, built only from touches inside the shopper's window W */
  SELECT user_id, MIN(first_exposure_hour) AS first_exposure_hour,
         MAX(user_id_type) AS user_id_type,
         MAX(CASE WHEN exposure_source = 'dsp' AND imps > 0 THEN 1 ELSE 0 END) AS saw_dsp,
         MAX(CASE WHEN exposure_source = 'sponsored' AND imps > 0 THEN 1 ELSE 0 END) AS saw_sponsored,
         SUM(imps) AS imps, SUM(dsp_cost_raw) AS dsp_cost_raw,
         SUM(sponsored_spend_raw) AS sponsored_spend_raw,
         MAX(CASE WHEN exposure_source = 'dsp' THEN currency END) AS dsp_currency,
         MIN(CASE WHEN exposure_source = 'dsp' THEN currency END) AS dsp_currency_min,
         MAX(CASE WHEN exposure_source = 'sponsored' THEN currency END) AS sponsored_currency,
         MIN(CASE WHEN exposure_source = 'sponsored' THEN currency END) AS sponsored_currency_min
  FROM w_touches GROUP BY user_id
),
grouped AS (
  SELECT e.user_id, e.first_exposure_hour, e.user_id_type, e.saw_dsp, e.saw_sponsored,
         e.imps, e.dsp_cost_raw, e.sponsored_spend_raw, e.dsp_currency, e.dsp_currency_min,
         e.sponsored_currency, e.sponsored_currency_min,
         CASE WHEN e.saw_dsp = 1 AND e.saw_sponsored = 1 THEN 'dsp+sponsored'
              WHEN e.saw_dsp = 1 THEN 'dsp' ELSE 'sponsored' END AS exposure_source_set,
         CASE WHEN e.user_id_type = 'adUserId' THEN 'account'
              ELSE 'device_or_browser' END AS identity_space
  FROM exposed e
),
outcome_events AS (
  /* event grain; conversions has one row per conversion_id */
  SELECT user_id, event_dt_hour_utc AS event_hour, event_category, event_subtype,
         new_to_brand, total_product_sales, purchase_currency
  FROM TABLE(EXTEND_TIME_WINDOW('conversions', 'P0D', 'P14D'))
  WHERE user_id IS NOT NULL
    AND marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
),
outcomes AS (
  /* purchases from the first-exposure hour; searches and page views strictly after it */
  SELECT g.user_id,
    MAX(CASE WHEN o.event_subtype = 'searchConversion'
              AND o.event_hour > g.first_exposure_hour THEN 1 ELSE 0 END) AS searched,
    MAX(CASE WHEN o.event_subtype = 'detailPageView'
              AND o.event_hour > g.first_exposure_hour THEN 1 ELSE 0 END) AS viewed,
    MAX(CASE WHEN o.event_category = 'purchase' THEN 1 ELSE 0 END) AS purchased,
    MAX(CASE WHEN o.event_category = 'purchase' AND o.new_to_brand THEN 1 ELSE 0 END) AS ntb,
    SUM(CASE WHEN o.event_category = 'purchase' THEN 1 ELSE 0 END) AS purchases,
    SUM(CASE WHEN o.event_category = 'purchase' THEN o.total_product_sales ELSE 0 END) AS revenue,
    MAX(CASE WHEN o.event_category = 'purchase' THEN o.purchase_currency END) AS purchase_currency,
    MIN(CASE WHEN o.event_category = 'purchase' THEN o.purchase_currency END) AS purchase_currency_min
  FROM grouped g JOIN outcome_events o ON o.user_id = g.user_id
  WHERE o.event_hour >= g.first_exposure_hour
    AND ABS(SECONDS_BETWEEN(g.first_exposure_hour, o.event_hour)) < 1209600
  GROUP BY g.user_id
),
bucketed AS (
  /* fine scheme, written in full with no ELSE: every shopper has imps >= 1 */
  SELECT g.user_id, g.identity_space, g.imps, g.dsp_cost_raw, g.sponsored_spend_raw,
         g.dsp_currency, g.dsp_currency_min, g.sponsored_currency, g.sponsored_currency_min,
         CASE WHEN g.imps = 1 THEN '1' WHEN g.imps = 2 THEN '2' WHEN g.imps = 3 THEN '3'
              WHEN g.imps = 4 THEN '4' WHEN g.imps = 5 THEN '5' WHEN g.imps = 6 THEN '6'
              WHEN g.imps = 7 THEN '7' WHEN g.imps = 8 THEN '8' WHEN g.imps = 9 THEN '9'
              WHEN g.imps >= 10 THEN '10_plus' END AS frequency_bucket
  FROM grouped g
)
SELECT b.frequency_bucket, b.identity_space, 'fine' AS bucket_scheme,
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
FROM bucketed b LEFT JOIN outcomes o ON b.user_id = o.user_id
GROUP BY b.frequency_bucket, b.identity_space
