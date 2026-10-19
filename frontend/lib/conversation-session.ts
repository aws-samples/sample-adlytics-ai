// SPDX-License-Identifier: MIT-0
/** Opaque browser conversation selector used only to resume the owner-bound Harness session. */

export const CONVERSATION_SESSION_STORAGE_KEY = 'adlytics.harness-session.v1';

const SESSION_ID = /^conversation-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
let volatileSession: string | null = null;

export function isConversationSessionId(value: unknown): value is string {
  return typeof value === 'string' && SESSION_ID.test(value);
}

/** Restore one opaque selector after reload, or create it without persisting any chat content. */
export function conversationSessionId(): string {
  if (volatileSession !== null) return volatileSession;
  try {
    const stored = localStorage.getItem(CONVERSATION_SESSION_STORAGE_KEY);
    if (isConversationSessionId(stored)) {
      volatileSession = stored;
      return volatileSession;
    }
  } catch {
    // Browser storage is optional; the in-memory selector still supports this page lifetime.
  }

  volatileSession = `conversation-${crypto.randomUUID()}`;
  try {
    localStorage.setItem(CONVERSATION_SESSION_STORAGE_KEY, volatileSession);
  } catch {
    // Browser storage is optional; no transcript or identity is substituted when it is unavailable.
  }
  return volatileSession;
}

/** Remove the local selector on explicit sign-out so another browser user starts a new conversation. */
export function clearConversationSessionId(): void {
  volatileSession = null;
  try {
    localStorage.removeItem(CONVERSATION_SESSION_STORAGE_KEY);
  } catch {
    // Browser storage is optional; in-memory continuity has already been cleared.
  }
}
