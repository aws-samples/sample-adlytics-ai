SELECT b.frequency_bucket, b.identity_space,
  COUNT(DISTINCT b.user_id) AS shoppers,
  SUM(b.imps) AS impressions, SUM(b.dsp_cost_raw) AS dsp_cost_raw,
  SUM(b.sponsored_spend_raw) AS sponsored_spend_raw,
  MAX(b.dsp_currency) AS dsp_currency, MIN(b.dsp_currency_min) AS dsp_currency_min,
  MAX(b.sponsored_currency) AS sponsored_currency,
  MIN(b.sponsored_currency_min) AS sponsored_currency_min,
  COUNT(DISTINCT CASE WHEN o.purchased = 1 THEN b.user_id END) AS purchasers,
  COUNT(DISTINCT CASE WHEN o.ntb = 1 THEN b.user_id END) AS ntb_purchasers,
  SUM(o.purchases) AS purchases, SUM(o.revenue) AS revenue_incl_halo,
  MAX(o.purchase_currency) AS purchase_currency,
  MIN(o.purchase_currency_min) AS purchase_currency_min,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM bucketed b LEFT JOIN outcomes o ON b.user_id = o.user_id
GROUP BY b.frequency_bucket, b.identity_space
