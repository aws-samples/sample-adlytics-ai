// SPDX-License-Identifier: MIT-0
'use client';

import type React from 'react';

import type { PublicConversationBlock } from '@/lib/assistant-response';

function PublicBlock({ block }: { readonly block: PublicConversationBlock }) {
  switch (block.kind) {
    case 'preface':
      return <p className="public-block-stream__preface">{block.text}</p>;
    case 'paragraph':
      return <p>{block.text}</p>;
    case 'grouped_list':
      return (
        <section aria-label={block.heading}>
          <h3>{block.heading}</h3>
          <ul>{block.items.map((item, index) => <li key={index}>{item}</li>)}</ul>
        </section>
      );
    case 'governed_table':
      return (
        <div aria-label={`${block.caption} table`} className="public-block-stream__table-scroll" role="region" tabIndex={0}>
          <table>
            <caption>{block.caption}</caption>
            <thead>
              <tr>{block.columns.map((column, index) => <th key={index} scope="col">{column}</th>)}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => cellIndex === 0
                    ? <th key={cellIndex} scope="row">{cell}</th>
                    : <td key={cellIndex}>{cell}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case 'notice':
      return (
        <section aria-label={block.title} className="public-block-stream__notice" data-notice-kind={block.notice_kind}>
          <h3>{block.title}</h3>
          <p>{block.text}</p>
        </section>
      );
  }
}

export function PublicBlockStream({ blocks }: { readonly blocks: readonly PublicConversationBlock[] }) {
  // Blocks are verified and released together, so the stagger is presentation only: the full
  // content is in the DOM immediately for assistive technology, and reduced motion disables it.
  return (
    <article aria-label="Assistant response" className="public-block-stream">
      {blocks.map((block, index) => (
        <div
          className="public-block-reveal"
          key={index}
          style={{ '--reveal-delay': `${Math.min(index, 12) * 140}ms` } as React.CSSProperties}
        >
          <PublicBlock block={block} />
        </div>
      ))}
    </article>
  );
}
