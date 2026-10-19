// SPDX-License-Identifier: MIT-0
'use client';

import { memo } from 'react';
import Markdown, { type Components, type Options, type UrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * The agent's streamed Markdown answer (ADR-083), built as React elements from a syntax tree.
 *
 * Raw HTML is never rendered: an HTML node becomes its literal text, so a campaign named
 * `Q4 <Holiday>` reads as written. Images are never loaded; their alt text is shown instead.
 * Links survive only for http, https, and mailto, and open in a new tab.
 */

const SAFE_PROTOCOLS: ReadonlySet<string> = new Set(['http:', 'https:', 'mailto:']);

/** Keep an absolute http, https, or mailto URL; blank everything else, relative URLs included. */
export const safeMarkdownUrl: UrlTransform = (url) => {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return '';
  }
  return SAFE_PROTOCOLS.has(parsed.protocol) ? parsed.href : '';
};

/** The host an http(s) link points at, or empty for mailto and anything unparsable. */
function linkHost(href: string): string {
  try {
    const url = new URL(href);
    return url.protocol === 'mailto:' ? '' : url.host;
  } catch {
    return '';
  }
}

interface SyntaxNode {
  type: string;
  value?: string;
  depth?: number;
  data?: { hProperties?: Record<string, unknown> };
  children?: SyntaxNode[];
}

/** Parents whose children are inline (phrasing) content in the Markdown syntax tree. */
const PHRASING_PARENTS: ReadonlySet<string> = new Set([
  'delete', 'emphasis', 'heading', 'link', 'linkReference', 'paragraph', 'strong', 'tableCell',
]);

/** Replace every raw HTML node with its literal text; block HTML becomes a paragraph. */
function remarkHtmlAsText() {
  const visit = (node: SyntaxNode): void => {
    const children = node.children;
    if (children === undefined) return;
    const phrasing = PHRASING_PARENTS.has(node.type);
    children.forEach((child, index) => {
      if (child.type === 'html') {
        const literal: SyntaxNode = { type: 'text', value: child.value ?? '' };
        children[index] = phrasing ? literal : { type: 'paragraph', children: [literal] };
      } else {
        visit(child);
      }
    });
  };
  return (tree: SyntaxNode) => visit(tree);
}

/**
 * Re-level headings by rank so the answer's outline never skips a level under the panel's h2.
 *
 * The answer's shallowest heading level becomes depth 1, the next depth 2, and so on. Each
 * heading keeps its written level as an `assistant-markdown__depth-N` class, which is what
 * styles it, so only the semantics change and the look stays the same.
 */
function remarkOutlineHeadings() {
  const collect = (node: SyntaxNode, headings: SyntaxNode[]): void => {
    if (node.type === 'heading') headings.push(node);
    node.children?.forEach((child) => collect(child, headings));
  };
  return (tree: SyntaxNode) => {
    const headings: SyntaxNode[] = [];
    collect(tree, headings);
    const ranks = [...new Set(headings.map((heading) => heading.depth ?? 1))].sort((a, b) => a - b);
    for (const heading of headings) {
      const written = heading.depth ?? 1;
      heading.data = {
        ...heading.data,
        hProperties: { ...heading.data?.hProperties, className: [`assistant-markdown__depth-${written}`] },
      };
      heading.depth = ranks.indexOf(written) + 1;
    }
  };
}

interface HastLike {
  readonly type: string;
  readonly tagName?: string;
  readonly value?: string;
  readonly children?: readonly HastLike[];
}

function plainText(node: HastLike): string {
  if (node.type === 'text') return node.value ?? '';
  return (node.children ?? []).map(plainText).join('');
}

/** Name a table's scroll region by its column headers, e.g. "Table: Campaign, Sales". */
function tableLabel(node: HastLike | undefined): string {
  const head = node?.children?.find((child) => child.tagName === 'thead');
  const row = head?.children?.find((child) => child.tagName === 'tr');
  const headers = (row?.children ?? [])
    .filter((cell) => cell.tagName === 'th')
    .map((cell) => plainText(cell).trim())
    .filter(Boolean);
  return headers.length === 0 ? 'Table' : `Table: ${headers.join(', ')}`;
}

const REMARK_PLUGINS: NonNullable<Options['remarkPlugins']> = [remarkGfm, remarkHtmlAsText, remarkOutlineHeadings];

/** Drop react-markdown's syntax-tree `node` prop so it never reaches the DOM. */
function domProps<P extends { node?: unknown }>(props: P): Omit<P, 'node'> {
  const rest: Partial<P> = { ...props };
  delete rest.node;
  return rest as Omit<P, 'node'>;
}

// Headings shift below the panel's own h2 so the answer never claims the page outline.
const COMPONENTS: Components = {
  h1: (props) => <h3 {...domProps(props)} />,
  h2: (props) => <h4 {...domProps(props)} />,
  h3: (props) => <h5 {...domProps(props)} />,
  h4: (props) => <h6 {...domProps(props)} />,
  h5: (props) => <h6 {...domProps(props)} />,
  h6: (props) => <h6 {...domProps(props)} />,
  a: (props) => {
    const { href, children, ...rest } = domProps(props);
    if (!href) return <span>{children}</span>;
    // The destination host is always shown: link text is model-written and could be steered by
    // data-borne content, so the reader sees where a link goes before following it.
    const host = linkHost(href);
    return (
      <>
        <a {...rest} href={href} rel="noopener noreferrer nofollow" target="_blank">
          {children}
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
        {host && <span className="assistant-markdown__link-host"> ({host})</span>}
      </>
    );
  },
  img: ({ alt }) => (alt ? <span className="assistant-markdown__image-alt">{alt}</span> : null),
  table: (props) => (
    <div aria-label={tableLabel(props.node)} className="assistant-markdown__table-scroll" role="region" tabIndex={0}>
      <table {...domProps(props)} />
    </div>
  ),
  th: (props) => <th scope="col" {...domProps(props)} />,
};

function AssistantMarkdownView({ text }: { readonly text: string }) {
  return (
    <article aria-label="Assistant response" className="assistant-markdown">
      <Markdown
        components={COMPONENTS}
        remarkPlugins={REMARK_PLUGINS}
        skipHtml
        urlTransform={safeMarkdownUrl}
      >
        {text}
      </Markdown>
    </article>
  );
}

/** Memoized so typing in the composer or a progress update never re-parses a settled answer. */
export const AssistantMarkdown = memo(AssistantMarkdownView);
