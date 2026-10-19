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
    AND GAP_SECONDS(g.first_exposure_hour, o.event_hour) < 1209600
  GROUP BY g.user_id
)
