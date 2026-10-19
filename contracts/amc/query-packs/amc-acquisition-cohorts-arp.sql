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
cohort_adid AS (
  SELECT user_id, cohort_month AS cohort_start, acquisition_source_set
  FROM cohort
  WHERE user_id_type = 'adUserId'
),
repeats AS (
  SELECT p.user_id, ROUND(ABS(SECONDS_BETWEEN(ch.cohort_start, DATE_TRUNC('MONTH', p.purchase_date_utc))) / 2629800.0) AS k,
         COUNT(DISTINCT p.purchase_id) AS purchases,
         SUM(p.unit_price * p.purchase_units_sold) AS revenue,
         MAX(p.currency_code) AS currency, MIN(p.currency_code) AS currency_min
  FROM amazon_retail_purchases p JOIN cohort_adid ch ON ch.user_id = p.user_id
  WHERE DATE_TRUNC('MONTH', p.purchase_date_utc) > ch.cohort_start
    AND p.marketplace_name = CUSTOM_PARAMETER('arp_marketplace_name')
  GROUP BY p.user_id, ch.cohort_start, DATE_TRUNC('MONTH', p.purchase_date_utc)
),
first_repeat AS (SELECT user_id, MIN(k) AS first_k FROM repeats WHERE k >= 1 GROUP BY user_id),
months AS (SELECT DISTINCT k FROM repeats WHERE k BETWEEN 1 AND 12),
cells AS (
  SELECT ch.user_id, ch.cohort_start, ch.acquisition_source_set, m.k
  FROM cohort_adid ch CROSS JOIN months m
  WHERE ROUND(ABS(SECONDS_BETWEEN(ch.cohort_start, CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP))) / 2629800.0) >= m.k
)
SELECT x.cohort_start AS cohort_month, x.k AS months_since_acquisition, x.acquisition_source_set,
  COUNT(DISTINCT x.user_id) AS cohort_buyers_account_ids_check,
  COUNT(DISTINCT CASE WHEN r.user_id IS NOT NULL THEN x.user_id END) AS repeat_buyers,
  COUNT(DISTINCT CASE WHEN f.first_k = x.k THEN x.user_id END) AS first_repeat_buyers,
  COUNT(DISTINCT CASE WHEN f.first_k <= x.k THEN x.user_id END) AS cumulative_repeat_buyers,
  SUM(r.purchases) AS repeat_purchases, SUM(r.revenue) AS repeat_revenue,
  MAX(r.currency) AS currency_code, MIN(r.currency_min) AS currency_code_min,
  'month' AS cohort_grain,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM cells x
LEFT JOIN repeats r ON r.user_id = x.user_id AND r.k = x.k
LEFT JOIN first_repeat f ON f.user_id = x.user_id
GROUP BY x.cohort_start, x.k, x.acquisition_source_set
