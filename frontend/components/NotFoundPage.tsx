// SPDX-License-Identifier: MIT-0
'use client';

import Link from 'next/link';

import { notFoundLabel, rootTab, visibleHubs } from '@/lib/route-manifest';
import { PAGE_HEADING_ID } from './Dialog';

/**
 * The not-found page (ADR-123 decision 2, ia §6.3 #6): static, with no data and no session. It
 * offers the root tab, which reads "Go to Overview" once Overview ships, and the customer hubs
 * from the manifest. Operator hubs aren't listed, because a signed-out visit can't know the
 * session's entitlements.
 */
export function NotFoundPage() {
  const root = rootTab();
  const hubs = visibleHubs({}).filter((hub) => hub.audience === 'customer');
  return (
    <div className="nf">
      <h1 id={PAGE_HEADING_ID} tabIndex={-1}>{notFoundLabel}</h1>
      <p>This page doesn&apos;t exist or has moved.</p>
      <p><Link className="btn primary" href="/">Go to {root.label}</Link></p>
      <h2>Hubs</h2>
      <ul>
        {hubs.map((hub) => (
          <li key={hub.hub_id}><Link href={hub.tabs[0]?.route ?? '/'}>{hub.label}</Link></li>
        ))}
      </ul>
    </div>
  );
}
