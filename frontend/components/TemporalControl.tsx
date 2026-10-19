// SPDX-License-Identifier: MIT-0
'use client';

import { Fragment, useEffect, useRef, useState, type FormEvent } from 'react';

import { formatIsoDate, formatIsoDateRange, formatIsoDateRangeShort, formatIsoInstant } from '@/lib/exact-format';
import { isCalendarPreset, type TemporalPresetId } from '@/lib/page-bindings';
import type {
  AnyTemporalControl,
  PageGenerationV1,
  TemporalControlV1,
  TemporalControlV2,
  TemporalPresetV2,
  TemporalSelectionV1,
  VintageControlV2,
} from '@/lib/presentation-v2';

const DAY = 86_400_000;

function dayIndex(date: string): number {
  return Date.parse(`${date}T00:00:00Z`) / DAY;
}

function dayCount(start: string, end: string): number {
  return dayIndex(end) - dayIndex(start) + 1;
}

function range(control: TemporalControlV1): string {
  return formatIsoDateRange(control.resolved.window.start, control.resolved.window.end);
}

function validCustomSelection(start: string, end: string, bounds: NonNullable<TemporalControlV1['custom']['bounds']>): boolean {
  if (!start || !end || start > end || start < bounds.earliest_start || end > bounds.latest_end) return false;
  const duration = dayCount(start, end);
  return Number.isSafeInteger(duration) && duration >= bounds.min_duration_days && duration <= bounds.max_duration_days;
}

/** What the page's data is as of: its window end and, when known, its generation's build time. */
function asOfCopy(control: TemporalControlV1, generation: PageGenerationV1 | undefined): string {
  const asOf = `Data as of ${formatIsoDate(control.resolved.window.end)}`;
  if (generation === undefined) return `${asOf}.`;
  return 'built_at' in generation
    ? `${asOf}, built ${formatIsoInstant(generation.built_at)}.`
    : `${asOf}, built before ${formatIsoInstant(generation.built_before)}.`;
}

function unavailablePresetCopy(label: string): string {
  return `${label} is not available for the currently published coverage. Choose another available reporting window.`;
}

/** Position of a window inside the published bounds, as rail percentages. */
export function railPlacement(
  window: { readonly start: string; readonly end: string },
  bounds: { readonly earliest_start: string; readonly latest_end: string },
): { readonly left: number; readonly right: number } {
  const span = Math.max(1, dayIndex(bounds.latest_end) - dayIndex(bounds.earliest_start));
  const clamp = (value: number) => Math.min(100, Math.max(0, value));
  return {
    left: clamp(((dayIndex(window.start) - dayIndex(bounds.earliest_start)) / span) * 100),
    right: clamp(((dayIndex(window.end) - dayIndex(bounds.earliest_start)) / span) * 100),
  };
}

/** What a version-2 preset button names under its label: its period, its dates, or its state. */
export function presetDetail(preset: TemporalPresetV2): string | null {
  // A week reads as its dates, like a rolling window ("Sep 8–14"); a month or quarter reads as
  // its server period label ("August 2026"), never a date range the browser built.
  if (preset.period_label !== null && preset.id !== 'last_complete_week') return preset.period_label;
  if (preset.window !== null) return formatIsoDateRangeShort(preset.window.start, preset.window.end);
  return null;
}

/**
 * The version-2 date control (ia §8.1): rolling presets, a divider, then calendar presets. Each
 * option shows its server label and, under it, the dates or period it serves now; an
 * unavailable option shows its server reason inside the option, never in a tooltip. Below a
 * 560 px container the same choices are a labelled select. A choice isn't a route change, so
 * focus stays on the control and returns to the chosen option once the new window arrives.
 */
export function DateControlV2({ control, defaultPreset, onFocusRestored, onSelect, pending = false, restoreFocus = false }: {
  readonly control: TemporalControlV2;
  readonly defaultPreset: TemporalPresetId;
  readonly onFocusRestored?: (() => void) | undefined;
  readonly onSelect: (selection: TemporalSelectionV1 | undefined) => void;
  readonly pending?: boolean;
  readonly restoreFocus?: boolean;
}) {
  const options = useRef(new Map<string, HTMLInputElement>());
  const select = useRef<HTMLSelectElement | null>(null);
  const selection = control.resolved.selection;
  const selected = selection.kind === 'preset' ? selection.preset_id : selection.kind === 'default' ? defaultPreset : null;
  useEffect(() => {
    if (!restoreFocus || pending) return;
    const option = selected === null ? undefined : options.current.get(selected);
    // The narrow select is the visible control below 560 px; focus whichever is rendered. A
    // resolved preset that is unavailable is disabled and can't take focus, so focus falls to
    // the select, then to the first choosable option, never to nowhere (gate F16).
    const takes = (element: HTMLInputElement | HTMLSelectElement | null | undefined) =>
      element !== null && element !== undefined && !element.disabled && element.offsetParent !== null;
    const enabled = [...options.current.values()].find((element) => takes(element));
    const fallback = select.current !== null && !select.current.disabled ? select.current : undefined;
    const target = takes(option) ? option : takes(select.current) ? select.current : enabled ?? fallback;
    target?.focus();
    onFocusRestored?.();
  }, [onFocusRestored, pending, restoreFocus, selected]);
  const choose = (preset: TemporalPresetId) => {
    if (preset === selected) return;
    onSelect({ kind: 'preset', preset_id: preset });
  };
  const firstCalendar = control.presets.findIndex((preset) => isCalendarPreset(preset.id));
  return <div className="controls">
    <fieldset className="datectl dc-wide" aria-busy={pending}>
      <legend>Window</legend>
      <div className="seg">
        {control.presets.map((preset, index) => {
          const detail = presetDetail(preset);
          const unavailable = preset.availability === 'unavailable';
          return <Fragment key={preset.id}>
            {index === firstCalendar && index > 0 ? <span className="seg-div" aria-hidden="true" /> : null}
            <label className="seg-opt">
              <input
                ref={(element) => { if (element === null) options.current.delete(preset.id); else options.current.set(preset.id, element); }}
                type="radio"
                name="preset"
                value={preset.id}
                checked={preset.id === selected}
                disabled={pending || unavailable}
                onChange={() => choose(preset.id)}
              />
              <span className="l">{preset.label}</span>
              {detail === null ? null : <span className="d">{detail}</span>}
              {preset.reason_text === null ? null : <span className="reason">{preset.reason_text}</span>}
            </label>
          </Fragment>;
        })}
      </div>
    </fieldset>
    <div className="select-wrap dc-narrow">
      <label htmlFor="sel-preset">Window</label>
      <select
        id="sel-preset"
        ref={select}
        value={selected ?? ''}
        disabled={pending}
        onChange={(event) => choose(event.currentTarget.value as TemporalPresetId)}
      >
        {control.presets.map((preset) => {
          const detail = presetDetail(preset);
          const parts = [preset.label, detail, preset.reason_text].filter((part): part is string => part !== null);
          return <option key={preset.id} value={preset.id} disabled={preset.availability === 'unavailable'}>{parts.join(' · ')}</option>;
        })}
      </select>
    </div>
  </div>;
}

/** Retention's latest vintage: the server's static label, never a selector. */
export function VintageControl({ control }: { readonly control: VintageControlV2 }) {
  if (control.resolved === null) return null;
  return <div className="controls"><p className="scope-band">{control.resolved.label_text}</p></div>;
}
export function TemporalControl({ control, defaultPreset, generation, onFocusRestored, onSelect, pending = false, refusal, restoreFocus = false }: {
  readonly control: AnyTemporalControl;
  /** The page's declared default; a version-2 preset control marks it when omitted. */
  readonly defaultPreset?: TemporalPresetId | undefined;
  /** The served generation's build time (S6 §2.9), when the page carries one. */
  readonly generation?: PageGenerationV1 | undefined;
  readonly onFocusRestored?: () => void;
  readonly onSelect: (selection: TemporalSelectionV1 | undefined) => void;
  readonly pending?: boolean;
  readonly refusal?: { readonly reason: string; readonly recovery: string } | null;
  readonly restoreFocus?: boolean;
}) {
  if (!('presets' in control)) return <VintageControl control={control} />;
  if (control.version === 2) {
    if (defaultPreset === undefined) throw new TypeError('A version-2 date control needs its default preset.');
    return <DateControlV2
      control={control}
      defaultPreset={defaultPreset}
      onFocusRestored={onFocusRestored}
      onSelect={onSelect}
      pending={pending}
      restoreFocus={restoreFocus}
    />;
  }
  return <TemporalControlV1View
    control={control}
    generation={generation}
    onFocusRestored={onFocusRestored}
    onSelect={onSelect}
    pending={pending}
    refusal={refusal}
    restoreFocus={restoreFocus}
  />;
}

function TemporalControlV1View({ control, generation, onFocusRestored, onSelect, pending = false, refusal, restoreFocus = false }: {
  readonly control: TemporalControlV1;
  readonly generation?: PageGenerationV1 | undefined;
  readonly onFocusRestored?: (() => void) | undefined;
  readonly onSelect: (selection: TemporalSelectionV1 | undefined) => void;
  readonly pending?: boolean;
  readonly refusal?: { readonly reason: string; readonly recovery: string } | null | undefined;
  readonly restoreFocus?: boolean;
}) {
  const [customOpen, setCustomOpen] = useState(false);
  const [customDraft, setCustomDraft] = useState(() => ({
    end: control.resolved.window.end,
    start: control.resolved.window.start,
    windowEnd: control.resolved.window.end,
    windowStart: control.resolved.window.start,
  }));
  const draft = customDraft.windowStart === control.resolved.window.start && customDraft.windowEnd === control.resolved.window.end
    ? customDraft
    : {
      end: control.resolved.window.end,
      start: control.resolved.window.start,
      windowEnd: control.resolved.window.end,
      windowStart: control.resolved.window.start,
    };
  const { end, start } = draft;
  const [invalid, setInvalid] = useState(false);
  const presetControls = useRef(new Map<string, HTMLButtonElement>());
  const customControl = useRef<HTMLButtonElement | null>(null);
  const custom = control.custom;
  const selection = control.resolved.selection;
  const customSelected = selection.kind === 'custom';
  const resolvedDays = dayCount(control.resolved.window.start, control.resolved.window.end);

  // A preset change isn't a route change (ia §2): focus lands on the resolved selection once the
  // new window arrives. While pending every option is disabled and can't hold focus.
  useEffect(() => {
    if (!restoreFocus || pending) return;
    const selected = selection.preset_id;
    (selected === null ? customControl.current : presetControls.current.get(selected))?.focus();
    onFocusRestored?.();
  }, [onFocusRestored, pending, restoreFocus, selection.preset_id]);

  const submitCustom = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (custom.availability !== 'available' || custom.bounds === null || !validCustomSelection(start, end, custom.bounds)) { setInvalid(true); return; }
    setInvalid(false);
    onSelect({ end, kind: 'custom', start });
  };

  const bounds = custom.availability === 'available' ? custom.bounds : null;
  const placement = bounds === null ? null : railPlacement(control.resolved.window, bounds);

  return <>
    <div className="windows" role="radiogroup" aria-label="Available reporting periods" aria-busy={pending}>
      {control.presets.map((preset) => {
        const checked = selection.kind === 'preset' && selection.preset_id === preset.id;
        const unavailable = preset.availability === 'unavailable';
        return <button
          key={preset.id}
          ref={(element) => { if (element === null) presetControls.current.delete(preset.id); else presetControls.current.set(preset.id, element); }}
          type="button"
          className="win-card"
          role="radio"
          aria-label={preset.label}
          aria-checked={checked}
          aria-describedby={unavailable ? `period-status-${preset.id}` : undefined}
          disabled={pending || unavailable}
          onClick={() => onSelect({ kind: 'preset', preset_id: preset.id })}
        >
          <span className="w">{preset.label}</span>
          <span className="s">{checked ? range(control) : unavailable ? 'Not published' : 'Published'}</span>
        </button>;
      })}
      {bounds !== null && placement !== null ? <button
        ref={customControl}
        type="button"
        className="win-slider"
        disabled={pending}
        aria-label="Custom period"
        aria-expanded={customOpen}
        data-selected={customSelected}
        onClick={() => setCustomOpen((open) => !open)}
      >
        <span className="s"><span className="w">Custom</span><span>{customSelected ? `${range(control)} · ${resolvedDays} days` : 'Choose dates'}</span></span>
        <span className="rail" aria-hidden="true">
          <span className="range" style={{ left: `${placement.left}%`, right: `${100 - placement.right}%` }} />
          <span className="knob a" style={{ left: `${placement.left}%` }} />
          <span className="knob b" style={{ left: `${placement.right}%` }} />
        </span>
        <span className="s"><span>{formatIsoDate(bounds.earliest_start)}</span><span>{formatIsoDate(bounds.latest_end)}</span></span>
      </button> : null}
      {bounds !== null && customOpen ? <form onSubmit={submitCustom} className="win-custom" aria-label="Custom reporting period">
        <label>Start<input type="date" min={bounds.earliest_start} max={bounds.latest_end} value={start} onChange={(event) => setCustomDraft({ ...draft, start: event.currentTarget.value })} /></label>
        <label>End<input type="date" min={bounds.earliest_start} max={bounds.latest_end} value={end} onChange={(event) => setCustomDraft({ ...draft, end: event.currentTarget.value })} /></label>
        <button className="btn" type="submit" disabled={pending}>Apply</button>
        {invalid ? <span role="alert">Choose an ordered date range within the published bounds and allowed duration.</span> : null}
      </form> : null}
    </div>
    <p className="footnote win-note">
      <span>Server-resolved reporting scope</span>
      {' · '}{range(control)}{' · '}{resolvedDays} {resolvedDays === 1 ? 'day' : 'days'}.
      {' '}{asOfCopy(control, generation)}
      {' '}Windows anchor to the published coverage this page was built from, never to today. Changing the window re-resolves every card, table, chart, and insight on this page.
    </p>
    {control.presets.filter((preset) => preset.availability === 'unavailable').map((preset) => <p key={preset.id} id={`period-status-${preset.id}`} className="footnote win-note">{unavailablePresetCopy(preset.label)}</p>)}
    {custom.availability === 'unavailable' ? <p className="footnote win-note">Custom reporting windows are not available for this page.</p> : null}
    {refusal ? <p role="status" aria-live="polite" className="state-note bad win-note"><span className="badge">Comparison unavailable</span>This reporting window cannot be compared safely. {refusal.recovery}</p> : null}
  </>;
}
