// SPDX-License-Identifier: MIT-0
import type { Answer } from '@/lib/answer';

/** The page's answer sentence above its first figure (UI refresh 1.5): one paragraph. */
export function AnswerSentence({ answer }: { readonly answer: Answer | null }) {
  if (answer === null || answer.text === '') return null;
  return <p className={`answer-sentence ${answer.kind}`} data-slot="answer">{answer.text}</p>;
}
