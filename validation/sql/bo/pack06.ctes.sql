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
