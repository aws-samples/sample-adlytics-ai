firsts_p AS (
  /* one window per (shopper, ad product), started by an impression in the period */
  SELECT user_id, ad_product_type, MIN(touch_hour) AS first_exposure_hour
  FROM touches
  WHERE imps > 0
    AND touch_hour < BUILT_IN_PARAMETER('TIME_WINDOW_END')
  GROUP BY user_id, ad_product_type
),
w_touches_p AS (
  SELECT t.user_id, t.user_id_type, t.ad_product_type, t.touch_hour, t.imps,
         t.dsp_cost_raw, t.sponsored_spend_raw, t.exposure_source, t.currency,
         f.first_exposure_hour
  FROM touches t JOIN firsts_p f
    ON f.user_id = t.user_id AND f.ad_product_type = t.ad_product_type
  WHERE t.touch_hour >= f.first_exposure_hour
    AND GAP_SECONDS(f.first_exposure_hour, t.touch_hour) < 1209600
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
  FROM w_touches_p GROUP BY user_id, ad_product_type
),
bucketed_p AS (
  SELECT g.user_id, g.ad_product_type, g.first_exposure_hour, g.identity_space, g.imps,
         g.dsp_cost_raw, g.sponsored_spend_raw, g.dsp_currency, g.dsp_currency_min,
         g.sponsored_currency, g.sponsored_currency_min,
         CASE WHEN g.imps = 1 THEN '1' WHEN g.imps = 2 THEN '2' WHEN g.imps = 3 THEN '3'
              WHEN g.imps = 4 THEN '4' WHEN g.imps = 5 THEN '5' WHEN g.imps = 6 THEN '6'
              WHEN g.imps = 7 THEN '7' WHEN g.imps = 8 THEN '8' WHEN g.imps = 9 THEN '9'
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
    AND GAP_SECONDS(b.first_exposure_hour, o.event_hour) < 1209600
  GROUP BY b.user_id, b.ad_product_type
),
conv AS (
  /* conversion_id stays inside the CTE, as the verified attribution pack does */
  SELECT conversion_id, user_id, event_dt_hour_utc AS event_hour,
         total_product_sales AS revenue
  FROM conversions
  WHERE event_category = 'purchase' AND user_id IS NOT NULL
    AND marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
),
matched AS (
  SELECT c.conversion_id, c.user_id, c.event_hour, c.revenue, t.touch_hour,
         t.exposure_source, t.ad_product_type
  FROM conv c JOIN touches t ON t.user_id = c.user_id
  WHERE t.touch_hour <= c.event_hour
    AND GAP_SECONDS(t.touch_hour, c.event_hour) < 1209600
),
ranked AS (
  SELECT conversion_id, user_id, event_hour, revenue, ad_product_type,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_hour ASC, exposure_source ASC, ad_product_type ASC
         ) AS first_rank,
         ROW_NUMBER() OVER (
           PARTITION BY conversion_id
           ORDER BY touch_hour DESC, exposure_source ASC, ad_product_type ASC
         ) AS last_rank,
         COUNT(ad_product_type) OVER (PARTITION BY conversion_id) AS touch_count
  FROM matched
),
credited AS (
  SELECT user_id, ad_product_type, event_hour, 'first_touch_v1' AS attribution_model_key,
         1.0 AS credit, revenue AS credited_revenue
  FROM ranked WHERE first_rank = 1
  UNION ALL
  SELECT user_id, ad_product_type, event_hour, 'last_touch_v1', 1.0, revenue
  FROM ranked WHERE last_rank = 1
  UNION ALL
  SELECT user_id, ad_product_type, event_hour, 'linear_v1',
         1.0 / touch_count, revenue / touch_count
  FROM ranked
),
credited_in_window AS (
  /* credit on ad product p is kept only when the conversion falls inside W for p */
  SELECT b.ad_product_type, b.frequency_bucket, b.identity_space, c.attribution_model_key,
         c.credit, c.credited_revenue
  FROM credited c JOIN bucketed_p b
    ON b.user_id = c.user_id AND b.ad_product_type = c.ad_product_type
  WHERE c.event_hour >= b.first_exposure_hour
    AND GAP_SECONDS(b.first_exposure_hour, c.event_hour) < 1209600
)
