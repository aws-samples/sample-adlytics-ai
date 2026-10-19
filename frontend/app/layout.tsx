// SPDX-License-Identifier: MIT-0
import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';

import { ApplicationRoot } from '@/features/PresentationApp';
import './globals.css';

export const metadata: Metadata = {
  title: 'Adlytics AI',
  description: 'Conversational retail media intelligence',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" data-theme="system" suppressHydrationWarning>
      <body>
        <script defer src="/theme-preference.9fe8ccc10b308643.js" />
        <ApplicationRoot>{children}</ApplicationRoot>
      </body>
    </html>
  );
}
