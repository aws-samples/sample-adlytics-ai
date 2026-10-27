// SPDX-License-Identifier: MIT-0
'use client';

import { fetchAudiencesPart } from '@/lib/presentation-v2';
import { useCallback, useEffect, useId, useRef, useState } from 'react';

import type { AudiencesPart, AudiencesPartRequest, CampaignIndexEntry } from '@/lib/audiences';

/** AUD-PAGE-FETCH-FAILED (owner approval A2): a part fetch failed. */
export const AUD_PAGE_FETCH_FAILED = "These segments didn't load. Try again.";
/** AUD-PAGE-REVISION-CHANGED (owner approval A2): a part came from a newer publication. */
export const AUD_PAGE_REVISION_CHANGED = 'This page was just updated. Showing the latest figures.';
/** Said in the status region when the reloaded page no longer lists the chosen campaign. */
export const AUD_CAMPAIGN_GONE = 'The campaign you chose isn’t in the updated page. Choose a campaign.';

/**
 * What the browser re-requests after a part named a newer publication (design 01 §7): the same
 * campaign by its label, or the same profile path and page. Module state, because the page
 * reloads its parent and re-mounts; a second mismatch in a row shows the failed state.
 */
export type AudiencesResume =
  | { readonly kind: 'campaign'; readonly label: string; readonly mismatches: number }
  | { readonly kind: 'profile'; readonly path: string; readonly page: number; readonly mismatches: number };
let resume: AudiencesResume | null = null;
/** Read the pending re-request once, so it applies to exactly one reloaded page. */
export function takeAudiencesResume(): AudiencesResume | null {
  const pending = resume;
  resume = null;
  return pending;
}
/** Clear any pending re-request (a fresh visit to the tab). */
export function clearAudiencesResume(): void {
  resume = null;
}

/** One part fetch bound to the loaded parent: the part, or why it isn't shown. */
export type PartState<T extends AudiencesPart> =
  | { readonly kind: 'idle' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly part: T }
  | { readonly kind: 'failed' };

export interface PartContext {
  readonly session: string;
  /** The loaded parent's publication revision. */
  readonly revision: string;
  /** Reload the parent; the page re-mounts and re-requests by `resume`. */
  readonly reload: () => void;
  /** Mismatches already seen in a row for this selection (0 on a fresh selection). */
  readonly mismatches: number;
}

/**
 * Fetch parts for one loaded parent and keep each one only when it names that parent's
 * revision. A part of another revision is discarded: the parent reloads once, and the same
 * selection is asked for again; a second mismatch in a row is the failed state, never a loop.
 */
export function usePart<T extends AudiencesPart>(context: PartContext, onMismatch: (mismatches: number) => AudiencesResume) {
  const { mismatches, reload, revision, session } = context;
  const [state, setState] = useState<PartState<T>>({ kind: 'idle' });
  const cache = useRef(new Map<string, T>());
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const request = useCallback(async (part: AudiencesPartRequest, options: { readonly prefetch?: boolean } = {}) => {
    const key = JSON.stringify(part);
    const cached = cache.current.get(key);
    if (cached !== undefined) {
      if (options.prefetch !== true) setState({ kind: 'ready', part: cached });
      return;
    }
    const controller = new AbortController();
    if (options.prefetch !== true) {
      active.current?.abort();
      active.current = controller;
      setState({ kind: 'loading' });
    }
    try {
      const served = await fetchAudiencesPart(session, part, controller.signal);
      if (served.parent_revision !== revision) {
        if (options.prefetch === true) return;
        if (mismatches >= 1) {
          setState({ kind: 'failed' });
          return;
        }
        resume = onMismatch(mismatches + 1);
        reload();
        return;
      }
      cache.current.set(key, served.part as T);
      if (options.prefetch !== true) setState({ kind: 'ready', part: served.part as T });
    } catch {
      if (controller.signal.aborted || options.prefetch === true) return;
      setState({ kind: 'failed' });
    }
  }, [mismatches, onMismatch, reload, revision, session]);
  return { request, state };
}

/**
 * The campaign index shards after the parent's first: fetched once, one after another, when
 * `load` is first called. `shards` stays null until every shard came back under the parent's
 * revision; a shard of another revision reloads the parent, and a failure lets a later call retry.
 */
export function useIndexShards(context: PartContext, count: number) {
  const { reload, revision, session } = context;
  const [shards, setShards] = useState<readonly CampaignIndexEntry[] | null>(null);
  const requested = useRef(false);
  const load = useCallback(async () => {
    if (requested.current || count < 2) return;
    requested.current = true;
    const loaded: CampaignIndexEntry[] = [];
    try {
      for (let shard = 1; shard < count; shard += 1) {
        const served = await fetchAudiencesPart(session, { kind: 'campaign_index', shard });
        if (served.parent_revision !== revision || served.part.kind !== 'campaign_index') {
          reload();
          return;
        }
        loaded.push(...served.part.entries);
      }
      setShards(loaded);
    } catch {
      // The first shard's campaigns stay listed; the next call tries again.
      requested.current = false;
    }
  }, [count, reload, revision, session]);
  return { load, shards };
}

/** Previous/Next for one paged table, with the server's paging text verbatim. */
export function Pager({ label, page, pageCount, pagingText, onPage }: {
  readonly label: string;
  readonly page: number;
  readonly pageCount: number;
  readonly pagingText: string | null;
  readonly onPage: (page: number) => void;
}) {
  if (pageCount < 2) return null;
  return <div className="pager">
    <button type="button" aria-label={`Previous segments, ${label}`} disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button>
    {pagingText === null ? null : <span>{pagingText}</span>}
    <button type="button" aria-label={`Next segments, ${label}`} disabled={page + 1 >= pageCount} onClick={() => onPage(page + 1)}>Next</button>
  </div>;
}

/** The failed state: the browser's row with a retry. */
export function PartFailed({ onRetry }: { readonly onRetry: () => void }) {
  return <div className="callout" role="status"><p>{AUD_PAGE_FETCH_FAILED}</p><button type="button" onClick={onRetry}>Try again</button></div>;
}

/**
 * A WAI-ARIA combobox over the campaign index: typing narrows the list by the server's
 * campaign names, on the client (a display filter, never a rank, ADR-071 decision 4).
 */
export function CampaignCombobox({ entries, selected, onFocusFirst, onSelect }: {
  readonly entries: readonly CampaignIndexEntry[];
  readonly selected: CampaignIndexEntry | null;
  readonly onFocusFirst: () => void;
  readonly onSelect: (entry: CampaignIndexEntry) => void;
}) {
  const id = useId();
  const listId = `${id}-list`;
  const [query, setQuery] = useState(selected?.campaign_label ?? '');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const focused = useRef(false);
  const needle = query.trim().toLocaleLowerCase();
  const shown = needle === '' || needle === selected?.campaign_label.toLocaleLowerCase()
    ? entries
    : entries.filter((entry) => entry.campaign_label.toLocaleLowerCase().includes(needle));
  const choose = (entry: CampaignIndexEntry) => {
    setQuery(entry.campaign_label);
    setOpen(false);
    onSelect(entry);
  };
  return <div className="combobox">
    <label htmlFor={`${id}-input`}>Campaign</label>
    <input
      id={`${id}-input`}
      role="combobox"
      aria-autocomplete="list"
      aria-expanded={open}
      aria-controls={listId}
      aria-activedescendant={open && shown[active] !== undefined ? `${id}-${shown[active]!.campaign_ref}` : undefined}
      value={query}
      onFocus={() => {
        if (!focused.current) {
          focused.current = true;
          onFocusFirst();
        }
        setOpen(true);
      }}
      onChange={(event) => { setQuery(event.currentTarget.value); setActive(0); setOpen(true); }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setActive((value) => Math.min(value + 1, Math.max(shown.length - 1, 0))); }
        if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => Math.max(value - 1, 0)); }
        if (event.key === 'Enter' && open && shown[active] !== undefined) { event.preventDefault(); choose(shown[active]!); }
        if (event.key === 'Escape') setOpen(false);
      }}
    />
    <ul id={listId} role="listbox" aria-label="Campaigns" hidden={!open}>
      {shown.map((entry, index) => <li
        id={`${id}-${entry.campaign_ref}`}
        key={entry.campaign_ref}
        role="option"
        aria-selected={index === active}
        onMouseDown={(event) => { event.preventDefault(); choose(entry); }}
      >{entry.campaign_label}</li>)}
    </ul>
  </div>;
}
