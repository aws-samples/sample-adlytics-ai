// SPDX-License-Identifier: MIT-0
import type { Metadata } from 'next';

import { NotFoundApp, notFoundTitle } from '@/features/PresentationApp';

export const metadata: Metadata = { title: notFoundTitle };

/** Exported as `/404.html`, which CloudFront serves every path outside the manifest (ADR-123). */
export default function NotFound() {
  return <NotFoundApp />;
}
