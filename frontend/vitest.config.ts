// SPDX-License-Identifier: MIT-0
import { defineConfig } from 'vitest/config';
export default defineConfig({test:{environment:'jsdom',include:['test/**/*.test.{ts,tsx}'],pool:'threads',setupFiles:['./test/setup.ts'],restoreMocks:true},resolve:{alias:{'@':new URL('.',import.meta.url).pathname}}});
