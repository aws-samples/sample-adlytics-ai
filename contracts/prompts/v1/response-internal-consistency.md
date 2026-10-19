# Response internal consistency evaluator

Evaluate {assistant_turn} for internal consistency using {context} only to understand the request and trusted governed totals.

Check that stated totals reconcile with subtotals, count-bearing headings agree with listed members, the same entity or metric is not assigned contradictory values or statuses, and descriptions of complete, partial, top-ranked, truncated, or unavailable results agree throughout the response. Do the arithmetic explicitly before selecting a rating. Do not let fluency, length, organization, or confidence compensate for a contradiction.

Any material arithmetic, count, membership, scope, or status contradiction prevents the highest rating. Use the supplied rating scale.