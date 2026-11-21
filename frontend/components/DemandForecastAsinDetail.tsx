// SPDX-License-Identifier: MIT-0
'use client';
import { useState } from 'react';
import { formatIsoDate, formatIsoDateRange, formatIsoDateRangeShort, formatIsoInstantShort, formatRatio } from '@/lib/exact-format';
import {
  askQuestions,
  detailAnswer,
  displayTargets,
  figureNumber,
  figureText,
  INFO,
  maseGapCopy,
  measureCopy,
  measureLowerCopy,
  modelCopy,
  plural,
  productSubject,
  reasonNextStep,
  reasonSentence,
  targetCopy,
  weekRuns,
  weekStateCopy,
  type DemandForecastDetail,
  type DemandForecastStatus,
  type Horizon,
  type RecentWeek,
  type Target,
  type TargetDetail,
  type ViewKey,
} from '@/lib/demand-forecast';
import { AnswerSentence } from './AnswerSentence';
import { ForecastBandChart, ForecastBandLegend } from './ChartPrimitives';
import { seedAssistantQuestion } from './ChatPanel';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import {
  DEMAND_FORECAST_NO_FORECAST_TITLE,
  FreshnessPill,
  noForecastYetText,
  readyStatus,
  ScopeCard,
  StaleBanner,
  WeekControl,
  type Loaded,
} from './DemandForecastScope';
import { InfoTip } from './InfoTip';
import { PageAbsence } from './PageAbsence';
import { StateCallout } from './StatePanels';

/**
 * One product's stored forecast, in place of the list (design S2 to S4; forecasting LLD "Demand
 * forecast page"). One measure shows at a time (D6), defaulting to the list's status target. A
 * forecast measure shows its answer, tiles, chart and dated forecast table; a withheld one shows
 * its reason, next step and what is known week by week. Every figure is the server's display
 * string, and no low or high total is ever shown.
 */

/** E-NOTIN's title (ABS-DF-2). */
export function notInForecastTitle(asin: string): string {
  return `${asin} isn't in this forecast`;
}
/** E-NOTIN (ABS-DF-2); without the count before the current list has loaded. */
export function notInForecastText(itemCount: number | null): string {
  return itemCount === null
    ? "This ASIN isn't in this forecast. Check the ASIN, or choose a product from the list."
    : `This forecast covers ${plural(itemCount, 'product', 'products')} with Vendor Central sales history. Check the ASIN, or choose a product from the list.`;
}

export function DemandForecastAsinDetail({ detail, itemCount, onBack, onRetryStatus, onViewWeeks, status, statusTarget, viewWeeks }: {
  readonly detail: DemandForecastDetail;
  readonly itemCount: number | null;
  readonly onBack: () => void;
  readonly onRetryStatus: () => void;
  readonly onViewWeeks: (weeks: number) => void;
  readonly status: Loaded<DemandForecastStatus>;
  readonly statusTarget: Target;
  readonly viewWeeks: number;
}) {
  const [measure, setMeasure] = useState<Target>(statusTarget);
  if (detail.outcome === 'not_in_forecast') {
    return (
      <StateCallout title={notInForecastTitle(detail.asin)} actions={<button type="button" className="btn" onClick={onBack}>Back to the list</button>}>
        {notInForecastText(itemCount)}
      </StateCallout>
    );
  }
  const { horizon, provenance, targets } = detail;
  if (detail.outcome === 'unavailable' || targets === null || horizon === null || provenance === null) {
    const next = readyStatus(status)?.next_new_input_check_at ?? null;
    return <PageAbsence title={DEMAND_FORECAST_NO_FORECAST_TITLE} message={noForecastYetText(next === null ? null : formatIsoInstantShort(next))}/>;
  }
  const row = targets[measure];
  const key = String(viewWeeks) as ViewKey;
  const forecast = row.chosen_model !== null && row.bands !== null && row.p50_totals[key] !== null;
  const recent = detail.recent_weeks;
  return (
    <div className="df-product">
      <p className="df-asin">ASIN {detail.asin}{detail.title_truncated ? <span className="sr-only">, name shortened</span> : null}</p>
      <ScopeCard>
        <div className="df-pills">
          <FreshnessPill freshness={detail.freshness} provenance={provenance} status={status} onRetryStatus={onRetryStatus}/>
        </div>
        {forecast ? <WeekControl horizon={horizon} viewWeeks={viewWeeks} onViewWeeks={onViewWeeks}/> : null}
        {recent === null ? null : (
          <div className="df-measures">
            <div className="seg" role="group" aria-label="Measure">
              {displayTargets.map((target) => (
                <button key={target} type="button" aria-pressed={target === measure} onClick={() => setMeasure(target)}>{targetCopy[target]}</button>
              ))}
            </div>
            <InfoTip title={INFO.measures.title}>{INFO.measures.text}</InfoTip>
          </div>
        )}
      </ScopeCard>
      {detail.freshness.state === 'stale' ? <StaleBanner freshness={detail.freshness} provenance={provenance} status={readyStatus(status)}/> : null}
      {forecast
        ? <Forecast detail={detail} horizon={horizon} measure={measure} row={row} viewWeeks={viewWeeks}/>
        : <Withheld detail={detail} measure={measure} status={readyStatus(status)}/>}
    </div>
  );
}

function Tile({ children, sub, title, tip }: {
  readonly children: string;
  readonly sub: string | null;
  readonly title: string;
  readonly tip: { readonly title: string; readonly text: string };
}) {
  return (
    <div className="tile static">
      <div className="k"><span>{title}</span><InfoTip title={tip.title} className="end">{tip.text}</InfoTip></div>
      <div className="v">{children}</div>
      {sub === null ? null : <div className="prior">{sub}</div>}
    </div>
  );
}

/** S2: a forecast measure. */
function Forecast({ detail, horizon, measure, row, viewWeeks }: {
  readonly detail: DemandForecastDetail;
  readonly horizon: Horizon;
  readonly measure: Target;
  readonly row: TargetDetail;
  readonly viewWeeks: number;
}) {
  const key = String(viewWeeks) as ViewKey;
  const weeks = horizon.weeks.slice(0, viewWeeks);
  const first = weeks[0];
  const last = weeks[weeks.length - 1];
  const total = row.p50_totals[key];
  const bands = row.bands;
  const model = row.chosen_model;
  if (first === undefined || last === undefined || total === null || bands === null || model === null) return null;
  const at = viewWeeks - 1;
  const low = bands.p10[at];
  const likely = bands.p50[at];
  const high = bands.p90[at];
  const code = row.currency_code;
  const accuracy = row.accuracy;
  const scored = accuracy !== null && accuracy.weeks_scored > 0;
  const recent = detail.recent_weeks ?? [];
  const recentFirst = recent[0]?.week_start;
  const recentLast = recent[recent.length - 1]?.week_start;
  const subject = productSubject(detail);
  const recentSpan = recentFirst === undefined || recentLast === undefined ? '' : `, recent weeks ${formatIsoDateRange(recentFirst, recentLast)}`;
  const chartName = `${measureCopy[measure]} by week for ${subject}${recentSpan}, forecast ${formatIsoDateRange(first, last)}`;
  return (
    <>
      <AnswerSentence answer={detailAnswer(detail, measure, viewWeeks)}/>
      <div className="tiles detail-tiles">
        <Tile title="Likely total" tip={INFO.total} sub={`${viewWeeks} weeks from ${formatIsoDateRangeShort(first, first)}`}>
          {figureText(total, measure, code)}
        </Tile>
        {low === undefined || likely === undefined || high === undefined ? null : (
          <Tile title={`Week of ${formatIsoDate(last)}`} tip={INFO.range} sub={`Low ${figureNumber(low, measure, code)} · High ${figureNumber(high, measure, code)} · last week of the view`}>
            {`Likely ${figureNumber(likely, measure, code)}`}
          </Tile>
        )}
        <Tile title="Inside the range in testing" tip={INFO.tested(accuracy, measure)} sub={`Made by ${modelCopy[model]}`}>
          {scored ? `${accuracy.weeks_in_range} of ${accuracy.weeks_scored} weeks` : 'Not enough history to measure'}
        </Tile>
        {detail.stockout_weeks_last_13 === null ? null : (
          <Tile title="Weeks shipped less than ordered" tip={INFO.short} sub="of the last 13 weeks">{String(detail.stockout_weeks_last_13)}</Tile>
        )}
      </div>
      <section className="df-chart-block" aria-labelledby="df-chart-title">
        <h2 id="df-chart-title" className="df-h">{measureCopy[measure]} by week: recent history and {viewWeeks}-week forecast</h2>
        <ForecastBandLegend
          reported={recent.some((week) => week.targets[measure].state === 'observed')}
          zero={recent.some((week) => week.targets[measure].state === 'inferred_zero')}
        />
        <ForecastBandChart
          ariaLabel={chartName}
          dividerLabel={`Forecast starts ${formatIsoDateRangeShort(first, first)}`}
          recent={recent.map((week) => ({ week: week.week_start, state: week.targets[measure].state, value: week.targets[measure].value?.value ?? null }))}
          forecast={{
            weeks,
            low: bands.p10.slice(0, viewWeeks).map((shown) => shown.value),
            likely: bands.p50.slice(0, viewWeeks).map((shown) => shown.value),
            high: bands.p90.slice(0, viewWeeks).map((shown) => shown.value),
          }}
        />
      </section>
      <p className="df-ask">
        <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(askQuestions.product(detail.asin))}>Ask about this product</button>
        <button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(askQuestions.range(detail.asin, viewWeeks, horizon))}>Explain the range</button>
      </p>
      <DataTable
        title={`Forecast, ${viewWeeks} weeks starting ${formatIsoDate(first)}, ${measureLowerCopy[measure]}`}
        note={<>Low, likely and high for each week<InfoTip title={INFO.range.title}>{INFO.range.text}</InfoTip></>}
      >
        <DtHead><DtRow><DtColumn>Week starting</DtColumn><DtColumn>Low</DtColumn><DtColumn>Likely</DtColumn><DtColumn>High</DtColumn></DtRow></DtHead>
        <DtBody>
          {weeks.map((week, index) => {
            const lowWeek = bands.p10[index];
            const likelyWeek = bands.p50[index];
            const highWeek = bands.p90[index];
            if (lowWeek === undefined || likelyWeek === undefined || highWeek === undefined) return null;
            return (
              <DtRow key={week}>
                <DtRowHeader>{formatIsoDate(week)}</DtRowHeader>
                <DtCell label="Low">{figureNumber(lowWeek, measure, code)}</DtCell>
                <DtCell label="Likely">{figureNumber(likelyWeek, measure, code)}</DtCell>
                <DtCell label="High">{figureNumber(highWeek, measure, code)}</DtCell>
              </DtRow>
            );
          })}
        </DtBody>
      </DataTable>
      <RecentWeeks measure={measure} recent={recent} currencyCode={code}/>
      <details className="df-method">
        <summary>How this forecast was made</summary>
        <p>
          {model === 'chronos2'
            ? `Made by ${modelCopy[model]}: in testing it beat both simpler methods.`
            : `Made by ${modelCopy[model]}. The weekly run selected this forecast from the methods that passed their checks.`}
        </p>
        {accuracy === null ? null : (
          <p>
            {accuracy.mase !== null
              ? `Average weekly miss in testing: ${formatRatio(accuracy.mase)} the product's usual week-to-week change.`
              : accuracy.mase_gap === null ? null : maseGapCopy[accuracy.mase_gap]}
            <InfoTip title={INFO.miss.title}>{INFO.miss.text}</InfoTip>
          </p>
        )}
        <p>It continues this product&apos;s recent weekly sales and page views. Promotions, price changes and ad plans you make aren&apos;t in it.</p>
      </details>
    </>
  );
}

/** The recent weeks with how each is known (P1-13), in a closed disclosure. */
function RecentWeeks({ currencyCode, measure, recent }: {
  readonly currencyCode: string | null;
  readonly measure: Target;
  readonly recent: readonly RecentWeek[];
}) {
  if (recent.length === 0) return null;
  return (
    <details className="df-method df-recent">
      <summary>Recent weeks, with reporting status</summary>
      <DataTable
        title={`Recent weeks, ${measureLowerCopy[measure]}`}
        note={<>How we know each week<InfoTip title={INFO.state.title}>{INFO.state.text}</InfoTip></>}
      >
        <DtHead><DtRow><DtColumn>Week starting</DtColumn><DtColumn>{measureCopy[measure]}</DtColumn><DtColumn>How we know</DtColumn><DtColumn>Shipped less than ordered</DtColumn></DtRow></DtHead>
        <DtBody>
          {recent.map((week) => {
            const shown = week.targets[measure];
            return (
              <DtRow key={week.week_start}>
                <DtRowHeader>{formatIsoDate(week.week_start)}</DtRowHeader>
                <DtCell label={measureCopy[measure]}>{shown.value === null ? 'Not known' : figureNumber(shown.value, measure, currencyCode)}</DtCell>
                <DtCell label="How we know">{weekStateCopy[shown.state]}</DtCell>
                <DtCell label="Shipped less than ordered">{week.stockout === null ? 'Not known' : week.stockout ? 'Yes' : 'No'}</DtCell>
              </DtRow>
            );
          })}
        </DtBody>
      </DataTable>
    </details>
  );
}

/** S3: a withheld measure: why, what to do, and what is known week by week (G-06). */
function Withheld({ detail, measure, status }: {
  readonly detail: DemandForecastDetail;
  readonly measure: Target;
  readonly status: DemandForecastStatus | null;
}) {
  const targets = detail.targets;
  if (targets === null) return null;
  const reason = targets[measure].withheld_reason;
  const shared = displayTargets.every((target) => targets[target].withheld_reason === reason);
  const next = status?.next_new_input_check_at ?? null;
  const recent = detail.recent_weeks ?? [];
  const code = targets[measure].currency_code;
  return (
    <>
      <StateCallout
        title={shared ? 'No forecast for this product' : `No ${measureLowerCopy[measure]} forecast for this product`}
        actions={<button type="button" className="ask-chip" onClick={() => seedAssistantQuestion(askQuestions.why(detail.asin))}>Ask why this product has no forecast</button>}
      >
        {reason === null ? (
          <span className="df-line">This measure isn&apos;t forecast for this product. The weeks below show what is known.</span>
        ) : (
          <>
            <span className="df-line">{reasonSentence(reason, measure)}</span>
            <span className="df-line">{reasonNextStep(reason, next)}</span>
          </>
        )}
      </StateCallout>
      {recent.length === 0 ? null : (
        <section className="df-strip-block" aria-labelledby="df-strip-title">
          <h2 id="df-strip-title" className="df-h">Last {recent.length} weeks, {measureLowerCopy[measure]}</h2>
          <ul className="df-strip">
            {weekRuns(recent, measure).map((run) => (
              <li key={run.weeks[0]?.week_start ?? run.range} className={`df-run ${run.state}`}>
                <span className="df-run-dates">{run.range}</span>
                <span className="df-run-state">
                  {weekStateCopy[run.state]}
                  {run.state === 'unknown' ? <InfoTip title={INFO.state.title} className="end">{INFO.state.text}</InfoTip> : null}
                </span>
                {run.state === 'observed' ? (
                  <span className="df-run-values">
                    {run.weeks.map((week) => {
                      const value = week.targets[measure].value;
                      return value === null ? null : (
                        <span key={week.week_start}>{formatIsoDateRangeShort(week.week_start, week.week_start)}: {figureNumber(value, measure, code)}</span>
                      );
                    })}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
