// SPDX-License-Identifier: MIT-0
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { isEdgeRefusal } from '@/lib/edge-refusal';
import type { ManifestTab } from '@/lib/route-manifest';
import { callApi } from '@/lib/session';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader } from './DataTable';
import { Dialog } from './Dialog';
import { useVisibleTabs } from './NextStepContext';
import { useApplicationState } from './PresentationController';
import { StateCallout } from './StatePanels';

/**
 * The prompt-registry request-body ceiling, in UTF-8 bytes (ADR-078, path-scoped body-size
 * exclusion). Equal to the edge's PROMPT_CREATE_VERSION_MAX_BODY_BYTES, asserted by the infra tests.
 */
export const PROMPT_BODY_MAX_BYTES = 32_768;

const PROMPT_BLOCKED_MESSAGE =
  'This request was blocked before it reached the service. If the prompt is long, shorten it and try again.';

function plural(count: number, noun: string): string {
  return `${count.toLocaleString('en-US')} ${noun}${count === 1 ? '' : 's'}`;
}

function promptTooLongMessage(overBytes: number): string {
  const over = Math.max(1, overBytes);
  return `This prompt is too long to save. Shorten it by ${plural(over, 'byte')} (about ${plural(over, 'character')} of plain text).`;
}

function requestBytes(body: Record<string, string | number | null>): number {
  return new TextEncoder().encode(JSON.stringify(body)).length;
}

const CALIBRATION_POLL_DELAYS_MILLIS = [2_000, 3_000, 5_000, 8_000, ...Array<number>(22).fill(13_000)];

type Slot = {
  readonly slot_id: string;
  /** The contract's readable name (owner UX decision 11), served beside the id. */
  readonly display_name: string;
  readonly purpose: string;
  readonly output_class: string;
  readonly silence_condition: string;
  readonly active_version: number | null;
  readonly newest_version: number | null;
  readonly newest_calibration: string | null;
  readonly retired: boolean;
};

// PromptVersionRecord.v2: who created a version, resolved to an email address at read time.
// The Cognito subject never reaches the browser, and the system `name` is wire-only.
type CreatedBy =
  | { readonly kind: 'operator_email'; readonly email: string }
  | { readonly kind: 'operator_not_found' }
  | { readonly kind: 'operator_unavailable' }
  | { readonly kind: 'system'; readonly name: 'contract-seed' }
  | { readonly kind: 'not_recorded' };

type Version = {
  readonly version: number;
  readonly prompt: string;
  readonly model_route: 'quick' | 'deep';
  readonly created_by: CreatedBy;
  readonly created_at: string | null;
  readonly calibration: 'pending' | 'passed' | 'failed';
  readonly calibration_cases: ReadonlyArray<{ readonly kind: string; readonly outcome: string }>;
};

type VersionHistory = {
  readonly slot_id: string;
  readonly active_version: number | null;
  readonly retired: boolean;
  readonly versions: ReadonlyArray<Version>;
};

type ViewState = 'loading' | 'ready' | 'unavailable' | 'conflict' | 'calibration-required';
type Confirmation = { readonly action: 'activate' | 'retire'; readonly version: number } | null;

function calibrationText(value: string | null): string {
  if (value === 'passed') return 'Calibration passed';
  if (value === 'failed') return 'Calibration needs changes';
  if (value === 'pending') return 'Calibration is still running';
  return 'No prompt version has been created';
}

/** The roster's "Active version" cell. */
function activeText(slot: Pick<Slot, 'active_version' | 'newest_version' | 'retired'>): string {
  if (slot.retired) return 'Retired, no version is active';
  if (slot.active_version !== null) return `Version ${slot.active_version}`;
  return slot.newest_version === null ? 'No versions yet' : 'None active';
}

/** The roster's "Other versions" cell: the newest version when it isn't the active one. */
function otherText(slot: Pick<Slot, 'active_version' | 'newest_version' | 'newest_calibration'>): string {
  if (slot.newest_version === null || slot.newest_version === slot.active_version) return 'None';
  return `Version ${slot.newest_version}: ${calibrationText(slot.newest_calibration).toLowerCase()}`;
}

/** A version's status pill: Active, or where its calibration stands. */
function statusPill(version: Pick<Version, 'calibration'>, isActive: boolean): { readonly className: string; readonly text: string } {
  if (isActive) return { className: 'pill accent', text: 'Active' };
  if (version.calibration === 'passed') return { className: 'pill', text: 'Ready to activate' };
  if (version.calibration === 'failed') return { className: 'pill warn', text: 'Needs changes' };
  return { className: 'pill pending', text: 'Calibration running' };
}

/** The active version leads, then every other version newest first (owner UX decision 11). */
function activeFirst(versions: readonly Version[], active: number | null): readonly Version[] {
  return [...versions].sort((left, right) => {
    if (left.version === active) return -1;
    if (right.version === active) return 1;
    return right.version - left.version;
  });
}

function plainLabel(value: string): string {
  return value.replaceAll('-', ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

function modelRouteLabel(value: Version['model_route']): string {
  return value === 'quick' ? 'Quick model' : 'Deep analysis model';
}

const CREATED_AT_FORMAT = new Intl.DateTimeFormat('en-US', {
  dateStyle: 'long',
  timeStyle: 'short',
  timeZone: 'UTC',
});

function createdByText(version: Pick<Version, 'created_by' | 'created_at'>): string {
  if (version.created_by.kind === 'not_recorded' || version.created_at === null) {
    return 'Created before authorship was recorded.';
  }
  const created = CREATED_AT_FORMAT.format(new Date(version.created_at));
  switch (version.created_by.kind) {
    case 'operator_email':
      return `Created by ${version.created_by.email} on ${created} UTC.`;
    case 'operator_not_found':
      return `Created on ${created} UTC by an operator whose account could not be found.`;
    case 'operator_unavailable':
      return `Created on ${created} UTC by an operator. Their email address could not be loaded right now.`;
    case 'system':
      return `Created by the deployment seed on ${created} UTC.`;
  }
}

function hasExactKeys(value: object, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function isCreatedBy(value: unknown): value is CreatedBy {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as { kind?: unknown; email?: unknown; name?: unknown };
  switch (candidate.kind) {
    case 'operator_email':
      return hasExactKeys(value, ['kind', 'email'])
        && typeof candidate.email === 'string' && Boolean(candidate.email.trim());
    case 'system':
      return hasExactKeys(value, ['kind', 'name']) && candidate.name === 'contract-seed';
    case 'operator_not_found':
    case 'operator_unavailable':
    case 'not_recorded':
      return hasExactKeys(value, ['kind']);
    default:
      return false;
  }
}

/** The server's UTC ISO 8601 instant (`datetime.isoformat()` after `astimezone(UTC)`). */
const CREATED_AT_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$/;

/** `created_at` is null exactly for a version created before authorship was recorded. */
function hasCreatedAt(createdBy: CreatedBy, createdAt: unknown): boolean {
  if (createdBy.kind === 'not_recorded') return createdAt === null;
  return typeof createdAt === 'string'
    && CREATED_AT_INSTANT.test(createdAt)
    && !Number.isNaN(new Date(createdAt).valueOf());
}

function isVersion(value: unknown): value is Version {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<Record<keyof Version, unknown>>;
  return typeof candidate.version === 'number'
    && typeof candidate.prompt === 'string'
    && (candidate.model_route === 'quick' || candidate.model_route === 'deep')
    && isCreatedBy(candidate.created_by)
    && hasCreatedAt(candidate.created_by, candidate.created_at)
    && (candidate.calibration === 'pending' || candidate.calibration === 'passed' || candidate.calibration === 'failed')
    && Array.isArray(candidate.calibration_cases);
}

function isSlot(value: unknown): value is Slot {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<Record<keyof Slot, unknown>>;
  return typeof candidate.slot_id === 'string'
    && typeof candidate.display_name === 'string' && Boolean(candidate.display_name.trim())
    && typeof candidate.purpose === 'string'
    && typeof candidate.output_class === 'string'
    && typeof candidate.silence_condition === 'string'
    && typeof candidate.retired === 'boolean';
}

/**
 * Slots that narrate one tab's figures, by the tab they belong to. Such a slot is listed only
 * while its tab is in the session's manifest, so a contract never shows before its page does
 * (New customers LLD §10 "Prompt-admin pairing", plan D18). Every other slot is always listed.
 */
const TAB_OF_SLOT: ReadonlyMap<string, string> = new Map([
  ['network-scorecard', 'scorecard'],
  ['portfolio-acquisition', 'new-customers'],
  ['reach-overlap', 'reach'],
  ['full-funnel-association', 'reach'],
  ['frequency-saturation', 'reach'],
  ['retention-shift', 'retention'],
  ['segment-over-index', 'audiences'],
]);

/** The roster, or null when the response isn't the slots projection. */
function slotsOf(body: unknown, tabs: readonly ManifestTab[]): readonly Slot[] | null {
  if (!body || typeof body !== 'object') return null;
  const slots = (body as { slots?: unknown }).slots;
  if (!Array.isArray(slots) || !slots.every(isSlot)) return null;
  return slots.filter((slot) => {
    const tab = TAB_OF_SLOT.get(slot.slot_id);
    return tab === undefined || tabs.some((candidate) => candidate.tab_id === tab);
  });
}

function isHistory(value: unknown): value is VersionHistory {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<VersionHistory>;
  return typeof candidate.slot_id === 'string'
    && (candidate.active_version === null || typeof candidate.active_version === 'number')
    && typeof candidate.retired === 'boolean'
    && Array.isArray(candidate.versions)
    && candidate.versions.every(isVersion);
}

export function PromptAdminPage() {
  const { canManagePrompts } = useApplicationState();
  const tabs = useVisibleTabs();
  const [slots, setSlots] = useState<readonly Slot[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState<VersionHistory | null>(null);
  const [state, setState] = useState<ViewState>('loading');
  const [showNewVersion, setShowNewVersion] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [modelRoute, setModelRoute] = useState<'quick' | 'deep'>('quick');
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [actionPending, setActionPending] = useState(false);
  // A refusal of the last request that keeps the page and the editor content: the handler's 413
  // or the edge's HTML block page.
  const [refusal, setRefusal] = useState<string | null>(null);
  const calibrationPoll = useRef<{
    token: number;
    timer: ReturnType<typeof setTimeout> | null;
  }>({ token: 0, timer: null });
  const mutation = useRef({ token: 0, mounted: true });

  const mutationIsCurrent = useCallback(
    (token: number): boolean => mutation.current.mounted && mutation.current.token === token,
    [],
  );

  const fetchSlots = useCallback(async (): Promise<readonly Slot[]> => {
    const response = await callApi('/api/prompt-registry/slots', 'POST', {});
    if (response.status === 409) throw new Error('registry conflict');
    if (!response.ok) throw new Error('registry unavailable');
    const slots = slotsOf(await response.json(), tabs);
    if (slots === null) throw new Error('registry response invalid');
    return slots;
  }, [tabs]);

  const fetchHistory = useCallback(async (slotId: string): Promise<VersionHistory> => {
    const response = await callApi('/api/prompt-registry/versions', 'POST', { slot_id: slotId });
    if (response.status === 409) throw new Error('version conflict');
    if (!response.ok) throw new Error('versions unavailable');
    const body: unknown = await response.json();
    if (!isHistory(body) || body.slot_id !== slotId) throw new Error('version response invalid');
    return body;
  }, []);

  const reload = useCallback(async (
    preferredSlot: string | null,
    mutationToken?: number,
  ): Promise<boolean> => {
    const current = (): boolean => mutationToken === undefined || mutationIsCurrent(mutationToken);
    if (!canManagePrompts || !current()) return false;
    try {
      const roster = await fetchSlots();
      if (!current()) return false;
      const slotId = roster.some((slot) => slot.slot_id === preferredSlot)
        ? preferredSlot
        : roster[0]?.slot_id ?? null;
      const nextHistory = slotId === null ? null : await fetchHistory(slotId);
      if (!current()) return false;
      setSlots(roster);
      setSelected(slotId);
      setHistory(nextHistory);
      setState('ready');
      return true;
    } catch {
      if (current()) setState('unavailable');
      return false;
    }
  }, [canManagePrompts, fetchHistory, fetchSlots, mutationIsCurrent]);

  useEffect(() => {
    if (!canManagePrompts) return;
    let active = true;
    const bootstrap = async () => {
      try {
        const slotsResponse = await callApi('/api/prompt-registry/slots', 'POST', {});
        if (slotsResponse.status === 409) {
          if (active) setState('conflict');
          return;
        }
        if (!slotsResponse.ok) throw new Error('registry unavailable');
        const roster = slotsOf(await slotsResponse.json(), tabs);
        if (roster === null) throw new Error('registry response invalid');
        const slotId = roster[0]?.slot_id ?? null;
        if (slotId === null) {
          if (active) {
            setSlots(roster);
            setState('ready');
          }
          return;
        }
        const historyResponse = await callApi('/api/prompt-registry/versions', 'POST', { slot_id: slotId });
        if (historyResponse.status === 409) {
          if (active) setState('conflict');
          return;
        }
        if (!historyResponse.ok) throw new Error('versions unavailable');
        const historyBody: unknown = await historyResponse.json();
        if (!isHistory(historyBody) || historyBody.slot_id !== slotId) {
          throw new Error('version response invalid');
        }
        if (active) {
          setSlots(roster);
          setSelected(slotId);
          setHistory(historyBody);
          setState('ready');
        }
      } catch {
        if (active) setState('unavailable');
      }
    };
    void bootstrap();
    return () => { active = false; };
  }, [canManagePrompts, tabs]);

  const stopCalibrationPoll = useCallback(() => {
    calibrationPoll.current.token += 1;
    if (calibrationPoll.current.timer !== null) {
      clearTimeout(calibrationPoll.current.timer);
      calibrationPoll.current.timer = null;
    }
  }, []);

  const startCalibrationPoll = useCallback((slotId: string, version: number) => {
    stopCalibrationPoll();
    const token = calibrationPoll.current.token;
    let attempt = 0;
    const poll = async () => {
      if (calibrationPoll.current.token !== token) return;
      try {
        const next = await fetchHistory(slotId);
        if (calibrationPoll.current.token !== token) return;
        setHistory(next);
        const exact = next.versions.find((item) => item.version === version);
        if (exact === undefined) throw new Error('queued version absent');
        if (exact.calibration !== 'pending') {
          const roster = await fetchSlots();
          if (calibrationPoll.current.token !== token) return;
          setSlots(roster);
          calibrationPoll.current.timer = null;
          return;
        }
        if (attempt >= CALIBRATION_POLL_DELAYS_MILLIS.length) {
          calibrationPoll.current.timer = null;
          return;
        }
        const delay = CALIBRATION_POLL_DELAYS_MILLIS[attempt] ?? 13_000;
        attempt += 1;
        calibrationPoll.current.timer = setTimeout(() => { void poll(); }, delay);
      } catch {
        if (calibrationPoll.current.token === token) setState('unavailable');
      }
    };
    const delay = CALIBRATION_POLL_DELAYS_MILLIS[attempt] ?? 2_000;
    attempt += 1;
    calibrationPoll.current.timer = setTimeout(() => { void poll(); }, delay);
  }, [fetchHistory, fetchSlots, stopCalibrationPoll]);

  useEffect(() => {
    const currentMutation = mutation.current;
    currentMutation.mounted = true;
    return () => {
      currentMutation.mounted = false;
      currentMutation.token += 1;
      stopCalibrationPoll();
    };
  }, [stopCalibrationPoll]);

  const selectSlot = (slotId: string) => {
    mutation.current.token += 1;
    setActionPending(false);
    stopCalibrationPoll();
    const token = calibrationPoll.current.token;
    setSelected(slotId);
    setHistory(null);
    setConfirmation(null);
    void fetchHistory(slotId).then((next) => {
      if (calibrationPoll.current.token === token) setHistory(next);
    }).catch(() => {
      if (calibrationPoll.current.token === token) setState('unavailable');
    });
  };

  const mutate = async (
    path: string,
    body: Record<string, string | number | null>,
    token: number,
  ): Promise<Record<string, unknown> | null> => {
    try {
      const response = await callApi(path, 'POST', body);
      if (!mutationIsCurrent(token)) return null;
      if (response.status === 413) {
        setRefusal(promptTooLongMessage(requestBytes(body) - PROMPT_BODY_MAX_BYTES));
        return null;
      }
      if (isEdgeRefusal(response)) {
        setRefusal(PROMPT_BLOCKED_MESSAGE);
        return null;
      }
      if (response.status === 409) {
        const detail: unknown = await response.json();
        if (!mutationIsCurrent(token)) return null;
        if (
          detail !== null
          && typeof detail === 'object'
          && (detail as { error?: unknown }).error === 'calibration_required'
        ) {
          setState('calibration-required');
          return null;
        }
        setState('conflict');
        return null;
      }
      if (!response.ok) throw new Error('prompt lifecycle unavailable');
      const result: unknown = await response.json();
      if (!mutationIsCurrent(token)) return null;
      return result !== null && typeof result === 'object'
        ? result as Record<string, unknown>
        : {};
    } catch {
      if (mutationIsCurrent(token)) setState('unavailable');
      return null;
    }
  };

  const beginMutation = (): number => {
    mutation.current.token += 1;
    setActionPending(true);
    setRefusal(null);
    return mutation.current.token;
  };

  const createBody = (slotId: string): Record<string, string> => (modelRoute === 'quick'
    ? { slot_id: slotId, prompt_template: prompt }
    : { slot_id: slotId, prompt_template: prompt, model_routing_hint: modelRoute });
  const overBytes = selected === null ? 0 : requestBytes(createBody(selected)) - PROMPT_BODY_MAX_BYTES;
  const promptStatus = overBytes > 0 ? promptTooLongMessage(overBytes) : refusal;

  const endMutation = (token: number): void => {
    if (mutationIsCurrent(token)) setActionPending(false);
  };

  const createVersion = async () => {
    if (selected === null || !prompt.trim()) return;
    const slotId = selected;
    const body = createBody(slotId);
    if (requestBytes(body) > PROMPT_BODY_MAX_BYTES) return;
    const token = beginMutation();
    try {
      const created = await mutate('/api/prompt-registry/create-version', body, token);
      if (created === null || typeof created.version !== 'number' || !mutationIsCurrent(token)) {
        return;
      }
      setPrompt('');
      setShowNewVersion(false);
      if (await reload(slotId, token) && mutationIsCurrent(token)) {
        startCalibrationPoll(slotId, created.version);
      }
    } finally {
      endMutation(token);
    }
  };

  const activate = async (version: number, expected: number | null) => {
    if (selected === null) return;
    const slotId = selected;
    const token = beginMutation();
    try {
      const result = await mutate('/api/prompt-registry/activate', {
        slot_id: slotId,
        version,
        expected_active_version: expected,
      }, token);
      if (result !== null && mutationIsCurrent(token)) {
        setConfirmation(null);
        await reload(slotId, token);
      }
    } finally {
      endMutation(token);
    }
  };

  const retire = async (version: number) => {
    if (selected === null) return;
    const slotId = selected;
    const token = beginMutation();
    try {
      const result = await mutate('/api/prompt-registry/retire', {
        slot_id: slotId,
        expected_active_version: version,
      }, token);
      if (result !== null && mutationIsCurrent(token)) {
        setConfirmation(null);
        await reload(slotId, token);
      }
    } finally {
      endMutation(token);
    }
  };

  const [viewing, setViewing] = useState<number | null>(null);
  const viewerHeading = useRef<HTMLHeadingElement>(null);
  const confirmOrigin = useRef<HTMLButtonElement | null>(null);

  if (!canManagePrompts) {
    return <StateCallout title="Not authorized" tone="bad">You do not have permission to manage governed prompts.</StateCallout>;
  }
  if (state === 'calibration-required') {
    return <StateCallout title="Activation is not available" tone="warn" live="alert" actions={<button type="button" className="btn" onClick={() => void reload(selected)}>Reload prompt state</button>}>
      The selected version must complete and pass calibration before it can be active. Reload the prompt state for the latest calibration result.
    </StateCallout>;
  }
  if (state === 'unavailable') {
    return <StateCallout title="Prompt administration is unavailable" tone="bad" live="alert" actions={<button type="button" className="btn" onClick={() => void reload(selected)}>Try again</button>}>
      Prompt state could not be loaded or changed. Try again when the registry service recovers.
    </StateCallout>;
  }
  if (state === 'conflict') {
    return <StateCallout title="Prompt state changed" tone="warn" live="alert" actions={<button type="button" className="btn" onClick={() => void reload(selected)}>Reload prompt state</button>}>
      Another operator changed the active version. Reload the roster before making a lifecycle decision.
    </StateCallout>;
  }
  if (slots === null) {
    return <div className="admin-stack" role="status" aria-label="Loading prompt administration">
      <span className="skel table" /><span className="skel card" />
    </div>;
  }
  if (slots.length === 0) {
    return <StateCallout title="No declared prompt slots">There are no governed prompt slots to review.</StateCallout>;
  }
  const selectedSlot = slots.find((slot) => slot.slot_id === selected) ?? slots[0];
  if (selectedSlot === undefined) return null;
  const activeVersion = history?.active_version ?? selectedSlot.active_version;
  const retired = history?.retired ?? selectedSlot.retired;
  const versions = history === null ? [] : activeFirst(history.versions, activeVersion);
  const viewed = versions.find((version) => version.version === viewing) ?? versions[0] ?? null;
  const openConfirmation = (next: NonNullable<Confirmation>, origin: HTMLButtonElement) => {
    confirmOrigin.current = origin;
    setConfirmation(next);
  };
  const viewPrompt = (version: number) => {
    setViewing(version);
    window.requestAnimationFrame(() => viewerHeading.current?.focus());
  };

  return <div className="admin-stack">
    <DataTable className="text" title="Insight prompts" note="Each slot writes one kind of insight card.">
      <DtHead><DtRow><DtColumn className="l">Slot</DtColumn><DtColumn className="l">Active version</DtColumn><DtColumn className="l">Other versions</DtColumn></DtRow></DtHead>
      <DtBody>{slots.map((slot) => <DtRow key={slot.slot_id} aria-selected={slot.slot_id === selectedSlot.slot_id}>
        <DtRowHeader className="row-pick">
          <button type="button" aria-current={slot.slot_id === selectedSlot.slot_id ? 'true' : undefined} onClick={() => { setViewing(null); selectSlot(slot.slot_id); }}>{slot.display_name}</button>
        </DtRowHeader>
        <DtCell label="Active version" className="l">{activeText(slot)}</DtCell>
        <DtCell label="Other versions" className="l">{otherText(slot)}</DtCell>
      </DtRow>)}</DtBody>
    </DataTable>
    <section className="card section" aria-labelledby="prompt-admin-title">
      <div className="section-head">
        <h2 id="prompt-admin-title">{selectedSlot.display_name}</h2>
        {retired ? <span className="pill warn">Retired, no version is active</span> : null}
      </div>
      <p className="sec-line">{selectedSlot.purpose} Output: {plainLabel(selectedSlot.output_class)}.</p>
      <p className="sec-line dim">When it stays quiet: {selectedSlot.silence_condition}</p>
      {history === null ? <div aria-label="Loading version history" role="status"><span className="skel table" /></div> : <>
        {versions.length === 0
          ? <p className="sec-line">No versions yet. Create a version to start calibration.</p>
          : <DataTable className="text" title="Versions, active first">
            <DtHead><DtRow><DtColumn className="l">Version</DtColumn><DtColumn className="l">Status</DtColumn><DtColumn className="l">Cases needing changes</DtColumn><DtColumn className="l">Actions</DtColumn></DtRow></DtHead>
            <DtBody>{versions.map((version) => {
              const isActive = version.version === activeVersion;
              const failed = version.calibration_cases.filter((item) => item.outcome === 'failed');
              const canActivate = version.calibration === 'passed' && !isActive;
              const pill = statusPill(version, isActive);
              return <DtRow key={version.version}>
                <DtRowHeader>
                  Version {version.version}
                  <span className="sub">{createdByText(version)}</span>
                  <span className="sub">Model: {modelRouteLabel(version.model_route)}.</span>
                </DtRowHeader>
                <DtCell label="Status" className="l"><span className={pill.className}>{pill.text}</span></DtCell>
                <DtCell label="Cases needing changes" className="l">{version.calibration === 'pending'
                  ? 'Calibration is still running'
                  : failed.length === 0 ? 'None' : `${failed.length} of ${version.calibration_cases.length}: ${failed.map((item) => plainLabel(item.kind)).join(', ')}`}</DtCell>
                <DtCell label="Actions" className="l"><span className="row-acts">
                  <button type="button" className="btn" aria-label={`View prompt: version ${version.version}`} aria-pressed={viewed?.version === version.version} onClick={() => viewPrompt(version.version)}>View prompt</button>
                  {canActivate ? <button type="button" className="btn" disabled={actionPending} aria-label={`Activate version ${version.version}`} onClick={(event) => openConfirmation({ action: 'activate', version: version.version }, event.currentTarget)}>Activate</button> : null}
                </span></DtCell>
              </DtRow>;
            })}</DtBody>
          </DataTable>}
        {viewed === null ? null : <section className="prompt-view" aria-labelledby="prompt-view-heading">
          <h3 id="prompt-view-heading" ref={viewerHeading} tabIndex={-1}>Version {viewed.version} prompt</h3>
          {/* A long governed prompt scrolls, and a scrollable region must be reachable by
              keyboard: focusable, with a role and name for what the region holds. */}
          <pre className="prompt-text" tabIndex={0} role="region" aria-label={`Version ${viewed.version} prompt text`}>{viewed.prompt}</pre>
        </section>}
        <div className="acts">
          <button type="button" className="btn" disabled={actionPending} aria-expanded={showNewVersion} onClick={() => setShowNewVersion((open) => !open)}>New version</button>
          {activeVersion === null ? null : <button type="button" className="btn danger" disabled={actionPending} onClick={(event) => openConfirmation({ action: 'retire', version: activeVersion }, event.currentTarget)}>Retire slot</button>}
        </div>
        {showNewVersion && <div className="new-version">
          <h3>New version</h3>
          <label className="field-label" htmlFor="prompt-template">Prompt template</label>
          <textarea className="prompt-text" id="prompt-template" value={prompt} aria-describedby={promptStatus === null ? undefined : 'prompt-request-status'} onChange={(event) => { setPrompt(event.target.value); setRefusal(null); }} />
          <label className="field-label" htmlFor="prompt-model-route">Model route</label>
          <div className="acts">
            <select id="prompt-model-route" value={modelRoute} onChange={(event) => setModelRoute(event.target.value as 'quick' | 'deep')}><option value="quick">Quick model</option><option value="deep">Deep analysis model</option></select>
            <button type="button" className="btn primary" disabled={actionPending || !prompt.trim() || overBytes > 0} onClick={() => void createVersion()}>Create version and run calibration</button>
          </div>
        </div>}
        {/* Always mounted, so a change of message is announced. */}
        <p className="sec-line" id="prompt-request-status" role="status">{promptStatus}</p>
      </>}
    </section>
    {confirmation === null ? null : <Dialog
      labelledBy="prompt-confirm-heading"
      onClose={() => setConfirmation(null)}
      opener={confirmOrigin}
      header={<>
        <h2 id="prompt-confirm-heading">{confirmation.action === 'activate' ? `Activate version ${confirmation.version}?` : 'Retire this slot?'}</h2>
        <span className="sub">{selectedSlot.display_name}</span>
      </>}
    >
      {confirmation.action === 'activate'
        ? <p>Activating version {confirmation.version} regenerates only this slot’s cached insights; other slots are unchanged.</p>
        : <p>Retiring this slot leaves prompt history intact and makes this slot unavailable in future generated insights.</p>}
      <div className="acts">
        {confirmation.action === 'activate'
          ? <button type="button" className="btn primary" disabled={actionPending} onClick={() => void activate(confirmation.version, activeVersion)}>Activate version {confirmation.version}</button>
          : <button type="button" className="btn primary danger" disabled={actionPending} onClick={() => { if (activeVersion !== null) void retire(activeVersion); }}>Retire slot</button>}
        <button type="button" className="btn" disabled={actionPending} onClick={() => setConfirmation(null)}>Cancel</button>
      </div>
    </Dialog>}
  </div>;
}
