repeats AS (
  SELECT c.user_id, MONTH_INDEX(ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc)) AS k,
         COUNT(*) AS purchases, SUM(c.total_product_sales) AS revenue,
         MAX(c.purchase_currency) AS purchase_currency,
         MIN(c.purchase_currency) AS purchase_currency_min
  FROM conversions c JOIN cohort ch ON ch.user_id = c.user_id
  WHERE c.event_category = 'purchase'
    AND DATE_TRUNC('MONTH', c.event_date_utc) > ch.cohort_month
    AND c.marketplace_name = CUSTOM_PARAMETER('sponsored_marketplace_name')
  GROUP BY c.user_id, ch.cohort_month, DATE_TRUNC('MONTH', c.event_date_utc)
),
first_repeat AS (SELECT user_id, MIN(k) AS first_k FROM repeats WHERE k >= 1 GROUP BY user_id),
months AS (SELECT DISTINCT k FROM repeats WHERE k BETWEEN 1 AND 12),
cells AS (
  SELECT ch.user_id, ch.user_id_type, ch.cohort_month, ch.acquisition_source_set, m.k
  FROM cohort ch CROSS JOIN months m
  WHERE MONTH_INDEX(ch.cohort_month, CAST(CUSTOM_PARAMETER('as_of_month') AS TIMESTAMP)) >= m.k
)
