ntb_rows AS (
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
/* cohort-set end */
