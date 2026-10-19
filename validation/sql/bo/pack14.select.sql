SELECT x.cohort_month, x.k AS months_since_acquisition, x.acquisition_source_set,
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
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM cells x
LEFT JOIN repeats r ON r.user_id = x.user_id AND r.k = x.k
LEFT JOIN first_repeat f ON f.user_id = x.user_id
GROUP BY x.cohort_month, x.k, x.acquisition_source_set
