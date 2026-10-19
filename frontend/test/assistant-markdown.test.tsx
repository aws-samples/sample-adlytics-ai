// SPDX-License-Identifier: MIT-0
import assert from 'node:assert/strict';
import axeCore from 'axe-core';
import { render, screen, within } from '@testing-library/react';
import { test } from 'vitest';
import { AssistantMarkdown, safeMarkdownUrl } from '@/components/assistant/AssistantMarkdown';

const answer = [
  '# Portfolio',
  '',
  'Sales for **Q4 <Holiday> push** rose; `ROAS` held.',
  '',
  '<div onclick="alert(1)">block html</div>',
  '',
  '> Coverage is partial.',
  '',
  '1. One',
  '2. Two',
  '',
  '| Campaign | Sales |',
  '| --- | ---: |',
  '| Spring | 10 |',
  '',
  'Mail [us](mailto:team@example.com), skip [local](/admin) and ![alt text](https://example.com/x.png).',
].join('\n');

test('renders GFM as inert React elements with no raw HTML, images, or unsafe links', async () => {
  const view = render(<AssistantMarkdown text={answer} />);
  const article = screen.getByRole('article', { name: 'Assistant response' });
  // Markdown headings sit below the panel's h2.
  const portfolio = within(article).getByRole('heading', { name: 'Portfolio' });
  assert.equal(portfolio.tagName, 'H3');
  assert.equal(portfolio.className, 'assistant-markdown__depth-1');
  assert.equal(article.querySelector('strong')?.textContent, 'Q4 <Holiday> push');
  assert.equal(article.querySelector('code')?.textContent, 'ROAS');
  assert.ok(within(article).getByText('<div onclick="alert(1)">block html</div>'));
  assert.equal(article.querySelectorAll('div[onclick], holiday, img, script').length, 0);
  assert.equal(article.querySelector('blockquote')?.textContent?.trim(), 'Coverage is partial.');
  assert.deepEqual(within(article).getAllByRole('listitem').map((item) => item.textContent), ['One', 'Two']);
  assert.ok(within(article).getByRole('region', { name: 'Table: Campaign, Sales' }));
  assert.deepEqual(within(article).getAllByRole('link').map((link) => link.getAttribute('href')), ['mailto:team@example.com']);
  assert.ok(within(article).getByText('local'));
  assert.ok(within(article).getByText('alt text'));
  try {
    const results = await axeCore.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    assert.deepEqual(results.violations, []);
  } finally {
    view.unmount();
    axeCore.reset();
  }
});

test('re-levels answer headings under the panel h2 with no skipped level, keeping each markdown level\'s look', async () => {
  const view = render(
    <>
      <h2>Assistant</h2>
      <AssistantMarkdown text={'## Advertising\n\nSpend.\n\n### Sponsored ads\n\nClicks.\n\n## Retail\n\nSales.'} />
    </>,
  );
  const article = screen.getByRole('article', { name: 'Assistant response' });
  // The answer's shallowest heading is h3; the depth class keeps the markdown level's look.
  assert.deepEqual(
    Array.from(article.querySelectorAll('h1, h2, h3, h4, h5, h6')).map((heading) => [heading.tagName, heading.className, heading.textContent]),
    [
      ['H3', 'assistant-markdown__depth-2', 'Advertising'],
      ['H4', 'assistant-markdown__depth-3', 'Sponsored ads'],
      ['H3', 'assistant-markdown__depth-2', 'Retail'],
    ],
  );
  try {
    const results = await axeCore.run(view.container, { runOnly: { type: 'rule', values: ['heading-order'] } });
    assert.deepEqual(results.violations, []);
  } finally {
    view.unmount();
    axeCore.reset();
  }
});

test('keeps only absolute http, https, and mailto URLs', () => {
  assert.equal(safeMarkdownUrl('https://example.com/a?b=1#c', 'href', { type: 'element', tagName: 'a', properties: {}, children: [] }), 'https://example.com/a?b=1#c');
  assert.equal(safeMarkdownUrl('HTTP://EXAMPLE.com', 'href', { type: 'element', tagName: 'a', properties: {}, children: [] }), 'http://example.com/');
  assert.equal(safeMarkdownUrl('mailto:team@example.com', 'href', { type: 'element', tagName: 'a', properties: {}, children: [] }), 'mailto:team@example.com');
  for (const url of ['javascript:alert(1)', ' javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,x', 'vbscript:x', 'ftp://example.com', '/admin', '#fn-1', 'example.com', '']) {
    assert.equal(safeMarkdownUrl(url, 'href', { type: 'element', tagName: 'a', properties: {}, children: [] }), '', url);
  }
});
test('shows the destination host beside every web link, and none for mail', () => {
  render(<AssistantMarkdown text={'See [the report](https://reports.example.com/q4) or [mail](mailto:team@example.com).'} />);
  const article = screen.getByRole('article', { name: 'Assistant response' });
  assert.equal(article.querySelectorAll('.assistant-markdown__link-host').length, 1);
  assert.equal(article.querySelector('.assistant-markdown__link-host')?.textContent, ' (reports.example.com)');
});
