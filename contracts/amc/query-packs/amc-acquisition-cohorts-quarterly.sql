WITH ntb_rows AS (
  SELECT user_id, user_id_type, conversion_event_date_utc AS d,
         CASE WHEN ad_product_type IS NULL THEN 'dsp' ELSE 'sponsored' END AS credit_source
  FROM amazon_attributed_events_by_conversion_time
  WHERE conversion_event_category = 'purchase' AND purchases > 0 AND new_to_brand
    AND user_id IS NOT NULL
    AND conversion_event_marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
),
/* cohort-set start */
first_ntb AS (SELECT user_id, MIN(d) AS acq_date FROM ntb_rows GROUP BY user_id),
cohort AS (
  SELECT f.user_id, MAX(n.user_id_type) AS user_id_type,
         DATE_TRUNC('MONTH', f.acq_date) AS cohort_month,
         CASE WHEN MAX(CASE WHEN n.credit_source = 'dsp' THEN 1 ELSE 0 END) = 1
               AND MAX(CASE WHEN n.credit_source = 'sponsored' THEN 1 ELSE 0 END) = 1 THEN 'dsp+sponsored'
              WHEN MAX(CASE WHEN n.credit_source = 'dsp' THEN 1 ELSE 0 END) = 1 THEN 'dsp'
              ELSE 'sponsored' END AS acquisition_source_set
  FROM first_ntb f JOIN ntb_rows n ON n.user_id = f.user_id AND n.d = f.acq_date
  GROUP BY f.user_id, f.acq_date
)
/* cohort-set end */,
cohort_quarters AS (
  SELECT user_id, user_id_type, DATE_TRUNC('QUARTER', cohort_month) AS cohort_quarter,
         acquisition_source_set
  FROM cohort
),
repeats AS (
  SELECT c.user_id, ROUND(ABS(SECONDS_BETWEEN(ch.cohort_quarter, DATE_TRUNC('QUARTER', c.event_date_utc))) / 7889400.0) AS k,
         COUNT(*) AS purchases, SUM(c.total_product_sales) AS revenue,
         MAX(c.purchase_currency) AS purchase_currency,
         MIN(c.purchase_currency) AS purchase_currency_min
  FROM conversions c JOIN cohort_quarters ch ON ch.user_id = c.user_id
  WHERE c.event_category = 'purchase'
    AND DATE_TRUNC('QUARTER', c.event_date_utc) > ch.cohort_quarter
    AND c.marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  GROUP BY c.user_id, ch.cohort_quarter, DATE_TRUNC('QUARTER', c.event_date_utc)
),
first_repeat AS (SELECT user_id, MIN(k) AS first_k FROM repeats WHERE k >= 1 GROUP BY user_id),
months AS (SELECT DISTINCT k FROM repeats WHERE k BETWEEN 1 AND 3),
cells AS (
  SELECT ch.user_id, ch.user_id_type, ch.cohort_quarter, ch.acquisition_source_set, m.k
  FROM cohort_quarters ch CROSS JOIN months m
  WHERE ROUND(ABS(SECONDS_BETWEEN(ch.cohort_quarter, CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP))) / 2629800.0) >= 3 * m.k + 2
    AND ROUND(ABS(SECONDS_BETWEEN(ch.cohort_quarter, CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP))) / 2629800.0) <= 12
)
SELECT x.cohort_quarter AS cohort_month, x.k AS months_since_acquisition, x.acquisition_source_set,
  COUNT(DISTINCT x.user_id) AS cohort_buyers_new_to_brand_12mo,
  COUNT(DISTINCT CASE WHEN x.user_id_type = 'adUserId' THEN x.user_id END) AS cohort_buyers_account_ids,
  COUNT(DISTINCT CASE WHEN r.user_id IS NOT NULL THEN x.user_id END) AS repeat_buyers,
  COUNT(DISTINCT CASE WHEN x.user_id_type = 'adUserId' AND r.user_id IS NOT NULL
                      THEN x.user_id END) AS repeat_buyers_account_ids,
  COUNT(DISTINCT CASE WHEN f.first_k = x.k THEN x.user_id END) AS first_repeat_buyers,
  COUNT(DISTINCT CASE WHEN f.first_k <= x.k THEN x.user_id END) AS cumulative_repeat_buyers,
  COUNT(DISTINCT CASE WHEN x.user_id_type = 'adUserId' AND f.first_k <= x.k
                      THEN x.user_id END) AS cumulative_repeat_buyers_account_ids,
  SUM(r.purchases) AS repeat_purchases, SUM(r.revenue) AS repeat_revenue,
  MAX(r.purchase_currency) AS purchase_currency, MIN(r.purchase_currency_min) AS purchase_currency_min,
  'quarter' AS cohort_grain,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM cells x
LEFT JOIN repeats r ON r.user_id = x.user_id AND r.k = x.k
LEFT JOIN first_repeat f ON f.user_id = x.user_id
GROUP BY x.cohort_quarter, x.k, x.acquisition_source_set
