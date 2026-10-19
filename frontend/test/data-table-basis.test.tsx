// SPDX-License-Identifier: MIT-0
/**
 * The stacked-row basis rule (ADR-118 decision 5): a typed-absence cell drops its coverage
 * part, so "6 of 7 days" never sits beside "—". This overrides the approved mockup state that
 * still shows both on a sponsored row's viewable rate.
 */
import assert from 'node:assert/strict';
import { render } from '@testing-library/react';
import { test } from 'vitest';
import { DataTable, DtBody, DtCell, DtColumn, DtHead, DtRow, DtRowHeader, stackedBasis } from '../components/DataTable';

const BASIS = { coverage: '6 of 7 days', text: 'Amazon DSP only' };

test('A typed-absence cell keeps its basis text and drops the coverage part', () => {
  assert.equal(stackedBasis(BASIS, false), 'Amazon DSP only · 6 of 7 days');
  assert.equal(stackedBasis(BASIS, true), 'Amazon DSP only');
  assert.equal(stackedBasis({ coverage: '6 of 7 days', text: null }, true), null);
  assert.equal(stackedBasis({ coverage: null, text: null }, false), null);
});

test('A sponsored row\'s "—" viewable rate never shows "6 of 7 days" when rows stack', () => {
  // Fictional channels: a DSP row with a partly reported viewable rate and a sponsored row
  // that can't report it.
  const view = render(<DataTable title="Delivery per channel">
    <DtHead><DtRow>
      <DtColumn>Channel</DtColumn>
      <DtColumn className="num" basis="Amazon DSP only · 6 of 7 days">Viewable rate</DtColumn>
    </DtRow></DtHead>
    <DtBody>
      <DtRow><DtRowHeader>Amazon DSP</DtRowHeader><DtCell basis={BASIS} className="num" label="Viewable rate">68%</DtCell></DtRow>
      <DtRow><DtRowHeader>Sponsored Products</DtRowHeader><DtCell absent basis={BASIS} className="num" label="Viewable rate">
        <span aria-hidden="true">—</span><span className="sr-only">Not available: sponsored ads don&apos;t report viewability</span>
      </DtCell></DtRow>
    </DtBody>
  </DataTable>);
  const cells = view.container.querySelectorAll('td[data-label="Viewable rate"]');
  assert.equal(cells[0]?.querySelector('.basis-inline')?.textContent, 'Amazon DSP only · 6 of 7 days');
  assert.equal(cells[1]?.querySelector('.basis-inline')?.textContent, 'Amazon DSP only');
  assert.equal(cells[1]?.textContent?.includes('6 of 7 days'), false);
  assert.ok(cells[1]?.textContent?.includes('—'));
  assert.equal(view.container.querySelector('th .basis')?.textContent, 'Amazon DSP only · 6 of 7 days');
});
