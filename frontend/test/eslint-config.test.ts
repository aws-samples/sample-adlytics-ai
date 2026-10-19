// SPDX-License-Identifier: MIT-0
// @vitest-environment node
/**
 * A-CVE-BRACES-DEV-001: `@next/eslint-plugin-next` calls its glob library (fast-glob, aliased to
 * tinyglobby in package.json `overrides`) only to resolve `settings.next.rootDir`. No composed
 * config sets it, so lint never runs that call; this keeps it that way.
 */
import { describe, expect, it } from 'vitest';

type ConfigObject = { settings?: Record<string, unknown>; plugins?: Record<string, unknown> };

async function composedConfig(): Promise<ConfigObject[]> {
  // Imported at run time: the config is untyped JavaScript, outside the TypeScript project.
  const loaded = (await import(new URL('../eslint.config.mjs', import.meta.url).href)) as {
    default: ConfigObject[];
  };
  return loaded.default;
}

describe('the composed ESLint config', () => {
  it('loads the Next plugin, so the guard below is about the config lint really runs', async () => {
    const config = await composedConfig();
    expect(config.some((entry) => entry.plugins?.['@next/next'] !== undefined)).toBe(true);
  });

  it('sets no settings.next, so the plugin never resolves a rootDir glob', async () => {
    const config = await composedConfig();
    expect(config.filter((entry) => entry.settings?.next !== undefined)).toEqual([]);
  });
});
