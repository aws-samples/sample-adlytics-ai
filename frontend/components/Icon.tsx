// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';

/**
 * One hand-authored inline SVG icon set for the shell (ia §7.1 item 1, F45). Icons are
 * decoration: every control that carries one also carries its accessible name.
 */
const PATHS = {
  overview: <><rect x="3" y="3" width="7" height="8" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="15" width="7" height="6" rx="1.5" /></>,
  performance: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  reach: <><circle cx="9" cy="12" r="6" /><circle cx="15" cy="12" r="6" /></>,
  customers: <><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" /><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.9.7 3.1 2.5 3.5 5.2" /></>,
  retail: <><path d="M3 9h18l-1.5 11h-15z" /><path d="M8 9V7a4 4 0 0 1 8 0v2" /></>,
  plan: <><path d="M3 17l5-5 4 4 8-8" /><path d="M15 8h5v5" /></>,
  operations: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></>,
  assistant: <><path d="M4 5h16v11H9l-5 4z" /><path d="M8.5 10.5h7" /></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  theme: <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" />,
  signout: <path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10" />,
  // Assistant width (WCAG 2.2 SC 2.5.7): outward chevrons widen, inward chevrons narrow.
  wider: <path d="M8 7l-5 5 5 5M16 7l5 5-5 5" />,
  narrower: <path d="M3 7l5 5-5 5M21 7l-5 5 5 5" />,
  // Change chip direction (ADR-121): the server's direction, never a sign the browser reads.
  up: <path d="M12 19V5M6 11l6-6 6 6" />,
  down: <path d="M12 5v14M6 13l6 6 6-6" />,
  flat: <path d="M5 12h14" />,
  // "Back to …" from a detail open in place (ia §7.3a item 1).
  back: <path d="M15 6l-6 6 6 6" />,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export function Icon({ name, small = false }: { readonly name: IconName; readonly small?: boolean }) {
  return (
    <svg className={small ? 'icon sm' : 'icon'} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  );
}
