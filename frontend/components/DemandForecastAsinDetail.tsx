// SPDX-License-Identifier: MIT-0
'use client';
import { formatIsoDate } from '@/lib/exact-format';
import {
  accuracyClassCopy,
  figureText,
  modelCopy,
  skipReasonCopy,
  targetCopy,
  targets,
  type DemandForecastDetail,
  type ViewKey,
} from '@/lib/demand-forecast';

/**
 * One product's stored forecast, in place under the list (forecasting LLD "Demand forecast page",
 * item 6). Every figure is the server's display string; the stored p50 total is the publisher's
 * own, and no low or high total is ever shown.
 */
export function DemandForecastAsinDetail({ detail, viewWeeks }: {
  readonly detail: DemandForecastDetail;
  readonly viewWeeks: number;
}) {
  if (detail.outcome === 'not_in_forecast') {
    return <p role="status">{detail.asin} isn&apos;t in this forecast. Check the ASIN, or pick a product from the list.</p>;
  }
  if (detail.outcome === 'unavailable' || detail.targets === null || detail.horizon === null) {
    return <p role="status">No forecast has been published yet.</p>;
  }
  const grain = detail.horizon.grain;
  const weeks = detail.horizon.weeks.slice(0, viewWeeks);
  const key = String(viewWeeks) as ViewKey;
  return (
    <section aria-labelledby="forecast-detail-heading">
      <h2 id="forecast-detail-heading">{detail.asin}: {detail.title ?? 'Title not available'}</h2>
      {targets.map((target) => {
        const row = detail.targets?.[target];
        if (row === undefined) return null;
        if (row.chosen_model === null || row.bands === null) {
          return (
            <section key={target} aria-label={targetCopy[target]}>
              <h3>{targetCopy[target]}</h3>
              <p>Not forecast: {row.withheld_reason === null ? 'no reason recorded' : skipReasonCopy[row.withheld_reason]}.</p>
            </section>
          );
        }
        const total = row.p50_totals[key];
        const bands = row.bands;
        return (
          <section key={target} aria-label={targetCopy[target]}>
            <h3>{targetCopy[target]} forecast, {modelCopy[row.chosen_model]}</h3>
            {total ? <p>Forecast total (p50), next {viewWeeks} {grain}s: {figureText(total, target, row.currency_code)}</p> : null}
            {row.accuracy ? (
              <p>
                {accuracyClassCopy[row.accuracy.accuracy_class]}. In testing, the actual was inside the range in{' '}
                {row.accuracy.weeks_in_range} of {row.accuracy.weeks_scored} test {grain}s.
              </p>
            ) : null}
            <table>
              <caption>{targetCopy[target]} by {grain}, forecast</caption>
              <thead><tr><th scope="col">{grain} starting</th><th scope="col">Low (p10)</th><th scope="col">Forecast (p50)</th><th scope="col">High (p90)</th></tr></thead>
              <tbody>
                {weeks.map((start, index) => {
                  const low = bands.p10[index];
                  const mid = bands.p50[index];
                  const high = bands.p90[index];
                  if (!low || !mid || !high) return null;
                  return (
                    <tr key={start}>
                      <th scope="row">{formatIsoDate(start)}</th>
                      <td>{figureText(low, target, row.currency_code)}</td>
                      <td>{figureText(mid, target, row.currency_code)}</td>
                      <td>{figureText(high, target, row.currency_code)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <table>
              <caption>{targetCopy[target]}, recent {grain}s observed</caption>
              <thead><tr><th scope="col">{grain} starting</th><th scope="col">Observed</th><th scope="col">Shipped below ordered</th></tr></thead>
              <tbody>
                {(detail.recent_weeks ?? []).map((week) => {
                  const observed = week.targets[target];
                  return (
                    <tr key={week.week_start}>
                      <th scope="row">{formatIsoDate(week.week_start)}</th>
                      <td>{observed.value === null ? 'Not known' : figureText(observed.value, target, row.currency_code)}</td>
                      <td>{week.stockout === null ? 'Not known' : week.stockout ? 'Yes' : 'No'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        );
      })}
      <p>This forecast continues the observed pattern. It isn&apos;t a causal prediction and excludes promotions.</p>
    </section>
  );
}
