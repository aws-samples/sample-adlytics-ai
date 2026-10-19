// SPDX-License-Identifier: MIT-0
'use client';

/** Progressive ADR-076 deep section shared by insight-card and metric-focus dialogs. */

import { useEffect, useRef, useState } from 'react';

import type { InsightDeepDiveV1 } from '@/lib/insight-deep-dive';

const REFETCH_MILLIS = 5_000;
const MAX_REFETCHES = 6;
// Deterministic staged captions while the deep analysis prepares (product decision
// 2026-09-25): first opens legitimately run tens of seconds on the deep model, and a
// bare skeleton reads as stalled. The stages mirror what the pipeline really does
// (evidence assembly, generation, citation verification) without claiming live
// progress; the timer advances them locally and holds on the last.
const PREPARATION_STAGES = [
  'Gathering the delivered evidence for this view…',
  'Weighing the size and direction of each movement…',
  'Drafting candidate readings…',
  'Checking each cited figure against the delivered values…',
  'Still working — the first open of a deep analysis can take a little longer…',
] as const;
const STAGE_MILLIS = 4_000;
// Survives dialog unmount: §3.1 mandates a repeat open of a completed identity
// render immediately. The key includes surface identity, slot/metric, and window;
// the server remains the authority and rejects any stale period/artifact identity.
const completed = new Map<string, InsightDeepDiveV1>();
const MAX_COMPLETED = 64;

function retainCompleted(key: string, value: InsightDeepDiveV1): void {
  // Module lifetime can span many page revisions in a long-lived tab; keep the
  // repeat-open optimization bounded and discard the oldest completed identity.
  completed.delete(key);
  completed.set(key, value);
  if (completed.size > MAX_COMPLETED) {
    const oldest = completed.keys().next().value;
    if (oldest !== undefined) completed.delete(oldest);
  }
}

export interface InsightDeepSurface {
  /** Stable content identity (surface + selection + metric/slot + resolved window). */
  readonly cacheKey: string;
  /** The card artifact revision this analysis must match before it can render. */
  readonly expectedArtifactRevision: string;
  /** Fetch the deep section; every call stays same-origin with cookie credentials. */
  readonly fetch: (signal: AbortSignal) => Promise<InsightDeepDiveV1>;
  /** The page's resolved window; another window is refused before rendering. */
  readonly window: { readonly end: string; readonly start: string };
}

type State =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly value: InsightDeepDiveV1 }
  | { readonly kind: 'unavailable'; readonly reason: string };

export function InsightDeepSection({ surface }: { readonly surface: InsightDeepSurface }) {
  // A retained completed identity is read during render so a repeat open serves
  // immediately without a state round-trip; the effect only fetches identities the
  // retention map does not hold.
  const retained = completed.get(surface.cacheKey);
  // The fetched state carries the identity it was resolved for; a key mismatch
  // derives 'loading' during render, so the effect never needs a synchronous reset.
  const [fetched, setState] = useState<{ readonly key: string; readonly state: State }>({ key: '', state: { kind: 'loading' } });
  const state: State = retained !== undefined
    ? { kind: 'ready', value: retained }
    : fetched.key === surface.cacheKey ? fetched.state : { kind: 'loading' };
  // The preparation caption is keyed by identity like the fetched state, so a
  // surface change restarts the stages without a synchronous reset.
  const [staged, setStage] = useState({ key: '', step: 0 });
  const stage = staged.key === surface.cacheKey ? staged.step : 0;
  const loading = state.kind === 'loading';
  useEffect(() => {
    if (!loading) return undefined;
    const interval = setInterval(() => {
      setStage((prior) => ({
        key: surface.cacheKey,
        step: Math.min(
          (prior.key === surface.cacheKey ? prior.step : 0) + 1,
          PREPARATION_STAGES.length - 1,
        ),
      }));
    }, STAGE_MILLIS);
    return () => clearInterval(interval);
  }, [loading, surface.cacheKey]);
  // The re-fetch budget survives effect restarts (the surface object is rebuilt on
  // every parent render), so the declared six-poll bound holds per open, per
  // identity — not per effect instance.
  const pollBudget = useRef({ count: 0, key: '' });

  useEffect(() => {
    if (completed.get(surface.cacheKey) !== undefined) return undefined;
    if (pollBudget.current.key !== surface.cacheKey) {
      pollBudget.current = { count: 0, key: surface.cacheKey };
    }
    const controller = new AbortController();
    const publish = (next: State) => setState({ key: surface.cacheKey, state: next });
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      void surface.fetch(controller.signal).then((value) => {
        if (controller.signal.aborted) return;
        if (
          value.period.start !== surface.window.start ||
          value.period.end !== surface.window.end ||
          value.artifact_revision !== surface.expectedArtifactRevision
        ) {
          publish({ kind: 'unavailable', reason: 'This deep analysis belongs to another page revision or reporting window. Close this view and refresh the page.' });
          return;
        }
        if (value.state === 'filled') {
          retainCompleted(surface.cacheKey, value);
          publish({ kind: 'ready', value });
          return;
        }
        if (value.state === 'pending' && pollBudget.current.count < MAX_REFETCHES) {
          pollBudget.current.count += 1;
          publish({ kind: 'loading' });
          timer = setTimeout(load, REFETCH_MILLIS);
          return;
        }
        publish({
          kind: 'unavailable',
          reason: value.absence_reason ?? 'The deep analysis is not available right now. The insight above remains usable.',
        });
      }).catch(() => {
        if (!controller.signal.aborted) {
          publish({ kind: 'unavailable', reason: 'The deep analysis is not available right now. The insight above remains usable.' });
        }
      });
    };
    load();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [surface]);

  if (state.kind === 'loading') return <section className="deep-section" aria-busy="true" aria-live="polite">
    <h3>Deep analysis</h3>
    <p className="quiet deep-progress">{PREPARATION_STAGES[stage]}</p>
    <span className="skel h-line" />
    <span className="skel h-line" />
  </section>;
  if (state.kind === 'unavailable') return <section className="deep-section" aria-live="polite">
    <h3>Deep analysis</h3>
    <p className="quiet">{state.reason}</p>
  </section>;
  const value = state.value;
  const deep = value.deep;
  if (deep === null) return null;
  // Full-width band (design revision 2026-09-25.1): the standout and each reading
  // render as sibling cells — three-up wide, stacked narrow — with the discriminator
  // beneath. Citations never render: grounding is verified server-side, and this
  // section speaks only customer words.
  const readingHeadings = ['Reading one', 'Reading two', 'Reading three'];
  return <section className="deep-section" aria-label="Deep analysis" aria-live="polite">
    <h3>Deep analysis</h3>
    <div className="deep-cols">
      <div className="deep-cell deep-standout"><h4>What stands out</h4><p>{deep.what_stands_out}</p></div>
      {deep.candidate_readings.map((reading, index) => (
        <div className="deep-cell" key={`${index}-${reading}`}>
          <h4>{readingHeadings[index] ?? `Reading ${index + 1}`}</h4>
          <p>{reading}</p>
        </div>
      ))}
    </div>
    <p className="deep-discriminator"><strong>What would tell these apart:</strong> {deep.discriminating_evidence}</p>
  </section>;
}
