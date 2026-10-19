SELECT g.exposure_source_set, g.identity_space,
  COUNT(DISTINCT g.user_id) AS shoppers_reached,
  SUM(g.imps) AS impressions, SUM(g.dsp_cost_raw) AS dsp_cost_raw,
  SUM(g.sponsored_spend_raw) AS sponsored_spend_raw,
  MAX(g.dsp_currency) AS dsp_currency, MIN(g.dsp_currency_min) AS dsp_currency_min,
  MAX(g.sponsored_currency) AS sponsored_currency,
  MIN(g.sponsored_currency_min) AS sponsored_currency_min,
  COUNT(DISTINCT CASE WHEN o.searched = 1 THEN g.user_id END) AS branded_search_shoppers,
  COUNT(DISTINCT CASE WHEN o.viewed = 1 THEN g.user_id END) AS detail_page_view_shoppers,
  COUNT(DISTINCT CASE WHEN o.purchased = 1 THEN g.user_id END) AS purchasers,
  COUNT(DISTINCT CASE WHEN o.ntb = 1 THEN g.user_id END) AS ntb_purchasers,
  SUM(o.purchases) AS purchases, SUM(o.revenue) AS revenue_incl_halo,
  MAX(o.purchase_currency) AS purchase_currency,
  MIN(o.purchase_currency_min) AS purchase_currency_min,
  CUSTOM_PARAMETER('sponsored_marketplace_name') AS marketplace_name,
  BUILT_IN_PARAMETER('TIME_WINDOW_START') AS time_window_start,
  BUILT_IN_PARAMETER('TIME_WINDOW_END') AS time_window_end
FROM grouped g LEFT JOIN outcomes o ON g.user_id = o.user_id
GROUP BY g.exposure_source_set, g.identity_space
