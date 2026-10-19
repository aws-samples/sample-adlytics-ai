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
    AND GAP_SECONDS(f.first_exposure_hour, t.touch_hour) < 1209600
)
