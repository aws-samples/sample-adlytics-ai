// SPDX-License-Identifier: MIT-0
import { webcrypto } from 'node:crypto';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

Object.defineProperty(globalThis, 'crypto', {
  configurable: true,
  value: webcrypto,
});

afterEach(() => {
  // A file that opts into the node environment has no DOM to reset.
  if (typeof document === 'undefined') return;
  cleanup();
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});
