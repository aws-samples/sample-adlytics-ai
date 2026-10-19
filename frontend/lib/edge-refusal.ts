// SPDX-License-Identifier: MIT-0

/**
 * A 403 whose media type is `text/html`: the edge web ACL's block page, not a session refusal
 * (ADR-007 amendment, 2026-10-05). The API's own refusals are JSON, so any other 403, including
 * one with no content type, is left to the session refresh path.
 */
export function isEdgeRefusal(response: Response): boolean {
  const mediaType = (response.headers.get('content-type') ?? '').split(';')[0]!.trim().toLowerCase();
  return response.status === 403 && mediaType === 'text/html';
}
