// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';

import {
  ApplicationController,
  PresentationController,
  type PresentationRoute,
} from '@/components/PresentationController';
import { NotFoundPage } from '@/components/NotFoundPage';
import { PromptAdminPage } from '@/components/PromptAdminPage';
import { notFoundTitle } from '@/components/notFoundTitle';

export function ApplicationRoot({ children }: { readonly children: ReactNode }) {
  return <ApplicationController>{children}</ApplicationController>;
}

export function PromptAdministrationApp() {
  return <PromptAdminPage />;
}

export { notFoundTitle };

export function NotFoundApp() {
  return <NotFoundPage />;
}

export function PresentationApp({ route }: { readonly route: PresentationRoute }) {
  return <PresentationController route={route} />;
}
