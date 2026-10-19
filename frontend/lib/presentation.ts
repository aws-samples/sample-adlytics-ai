// SPDX-License-Identifier: MIT-0
/**
 * Shared presentation primitives used by every rendered route.
 *
 * Page-specific artifact decoders live beside their page (`campaign-attribution.ts`); this
 * module holds only the types and errors the transport and shell share.
 */
import { conversationSessionId, isConversationSessionId } from './conversation-session';

declare const instantBrand: unique symbol;
/** An RFC 3339 UTC instant that has passed a page decoder's exact check. */
export type IsoInstant = string & { readonly [instantBrand]: true };

export interface DateWindow {
  readonly end: string;
  readonly start: string;
}

/** Closed error classes the API reports; the UI maps each to plain-language copy. */
export type PresentationErrorClass =
  | 'dependency'
  | 'domain'
  | 'invariant'
  | 'validation';

export class PresentationResponseError extends Error {
  constructor() {
    super('The presentation response was not valid.');
    this.name = 'PresentationResponseError';
  }
}

export class PresentationRequestError extends Error {
  readonly code: string | null;
  readonly errorClass: PresentationErrorClass;
  /**
   * The refusal's validated server `error` text, shown by `PageIssue` for a `domain` refusal
   * (server-owned copy). `Error.message` stays generic, so the text never reaches a log.
   */
  readonly serverMessage: string | null;
  constructor(
    errorClass: PresentationErrorClass = 'invariant',
    code: string | null = null,
    serverMessage: string | null = null,
  ) {
    super('Presentation data is unavailable.');
    this.name = 'PresentationRequestError';
    this.errorClass = errorClass;
    this.code = code;
    this.serverMessage = serverMessage;
  }
}

/** Whether a server-issued capability can still be redeemed at the given clock reading. */
export function isCapabilityExpired(
  expiresAt: IsoInstant,
  currentTime: () => number = Date.now,
) {
  try {
    const now = currentTime();
    const expiry = Date.parse(expiresAt);
    return !Number.isFinite(now) || !Number.isFinite(expiry) || expiry <= now;
  } catch {
    return true;
  }
}

export { conversationSessionId, isConversationSessionId };
