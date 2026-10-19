// SPDX-License-Identifier: MIT-0
/**
 * Shared state components (ia §7.9, §8.3): a callout that names what is missing in plain
 * words with the next step, and the page error, which keeps the hub header above it and
 * offers Try again and a way to the root tab.
 */
'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

import { locateRoute, rootTab } from '@/lib/route-manifest';

export function StateCallout({
  actions,
  children,
  headingLevel = 2,
  live,
  title,
  tone = 'neutral',
}: {
  readonly actions?: ReactNode;
  readonly children: ReactNode;
  readonly headingLevel?: 2 | 3;
  /** `alert` for a failure the user just caused or must act on; `status` for a calm update. */
  readonly live?: 'alert' | 'status';
  readonly title: string;
  readonly tone?: 'neutral' | 'warn' | 'bad';
}) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <section className={`callout ${tone}`} {...(live === undefined ? {} : { role: live })}>
      <Heading>{title}</Heading>
      <p>{children}</p>
      {actions === undefined ? null : <div className="acts">{actions}</div>}
    </section>
  );
}

/**
 * "Go to {root tab}": the root route's own tab label, so it reads "Go to Overview" once that
 * ships. On the root tab itself the link would lead back here, so it isn't offered.
 */
export function GoToRootLink() {
  const pathname = usePathname();
  const root = rootTab();
  if (locateRoute(pathname ?? '/')?.tab.tab_id === root.tab_id) return null;
  return <Link className="btn" href="/">Go to {root.label}</Link>;
}

export function PageError({ message, onRetry }: { readonly message: string; readonly onRetry?: () => void }) {
  return (
    <StateCallout
      live="alert"
      title="This page isn't available right now"
      tone="bad"
      actions={<>
        {onRetry === undefined ? null : <button type="button" className="btn" onClick={onRetry}>Try again</button>}
        <GoToRootLink />
      </>}
    >
      {message}
    </StateCallout>
  );
}
