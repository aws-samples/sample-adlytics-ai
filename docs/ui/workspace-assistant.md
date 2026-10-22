# Workspace assistant

| | |
|---|---|
| **Status** | Approved shared interaction invariant for the Decision Workspace |
| **Owns** | Assistant availability, layout, request boundary, streaming response composition, and relationship to page evidence |
| **Parent** | [Decision Workspace UI](README.md) · [Conversational Runtime](../capabilities/conversational-runtime/README.md) |

## 1. Purpose

The generative AI assistant is available throughout the authenticated Decision Workspace. A user may ask a question from any page without first selecting a page control or enabling a page-specific mode.

The assistant is a workspace capability, not a page-specific renderer. Deterministic pages show server-owned artifacts; the assistant answers independently through the globally configured, server-governed tool surface.

## 2. Layout behavior

| Layout | Assistant behavior |
|---|---|
| Wide | Open supporting sidebar beside the primary workspace. It is resizable and collapsible. |
| Regular | Collapsible overlay that preserves the current page and focus. |
| Compact | Collapsible modal sheet with focus containment and restoration. |

Collapsing the assistant never discards page evidence, changes an analytical request, or ends the conversation. A collapsed affordance remains visible on every authenticated page.

## 3. Request and tool boundary

The browser sends `ConversationRequest.v3`: the user question, the opaque conversation session and, on a presentation tab or campaign detail, the page's `artifact_revision` as an opaque `page_context_reference`, which the server redeems for the labels it recorded when it served the page ([ADR-120](../decisions/adr-120-carry-page-context-to-chat-as-a-server-redeemed-reference.md)). It does not send page metrics, raw page artifacts, canonical entity identifiers, query plans, selected filters, opaque page capabilities, or authorization context.

The server verifies identity and invokes the globally configured managed Harness. The Harness may use any globally available server-governed tool needed to answer the question. Tool authorization, semantic resolution, exact publication selection, metric/grain validation, qualification, and response release remain server-side responsibilities.

A page does not add a second tool restriction. A page may offer suggested questions as ordinary user text, but suggestions neither authorize a tool nor narrow the assistant's global capability.

## 4. Streaming response composition

The target interaction is a continuous conversation, not a buffered report. One assistant turn may stream in this ordered form:

1. active status and a user-visible Cancel control;
2. prose delta that begins answering the user;
3. one or more bounded evidence cards immediately after the claim they support;
4. resumed prose, then an explicitly labeled Perspective or Hypothesis when useful;
5. a final disposition: `answered`, `partially_answered`, `clarification_required`, `limited`, or `refused`.

Cards remain inside their assistant message in the conversation transcript. They never become a detached results drawer, a page overlay, or a replacement for the assistant's prose. An answer with no safe evidence card instead explains the limit or asks the required clarification.

A public evidence card is a bounded server-side projection of a recognized, valid shaped tool result. It may contain safe display labels, exact metric/value pairs, a declared period/grain, source/basis, qualification, a compact trend series where the artifact permits it, and an existing generated action. It never exposes raw tool names, tool IDs, database labels, timings, query plans, canonical identifiers, arbitrary JSON, or browser-provided page context. Unknown or malformed result content fails closed and produces no card.

Readability (UI refresh 2.6, OD-UX-8, presentation only; no prompt change and no parsing of model text):
- Under each finished answer, a collapsed "How this was answered" lists the turn's V4 `tool` events
  in order, each as a plain step mapped from its declared governed tool name
  (`TOOL_STEP_LABELS` in `frontend/components/ChatPanel.tsx`, pinned to
  `contracts/generated/tools.json` by a test) followed by that tool name. Evidence cards still never
  show tool names; the names appear only inside this trace, as ADR-065 allows.
- The turn's support reference (`done.audit_id`, ADR-081 §2) sits behind one "Copy reference"
  button instead of printing the raw token.
- Answer tables keep their keyboard-scrollable region and show an edge shadow while columns remain
  out of view, so a 360 px column signals that it scrolls.

Visual cards follow the workspace [visual-evidence policy](README.md#4-visual-evidence-and-temporal-controls): an observation remains prose, while the card makes the supporting trend or comparison easy to scan. Perspective and Hypothesis are visually and semantically separate from observed facts.

## 5. Page-owned visual narration

A graph-scoped Perspective on a deterministic page is not hidden assistant context and does not turn the page into a chat session. It is a separate server-owned narration projection bound to the page visual's resolved artifact revision. The server may use governed narration to produce its Observed, Perspective, Hypothesis, or Limit blocks; the browser provides neither chart data nor page authority.

The global assistant remains available to discuss a visual, but it receives only ordinary user text. A suggested question is not an automatic visual handoff and does not authorize a tool or supply a selected entity, period, or artifact.

## 6. Relationship to page actions

Page actions remain deterministic UI controls derived from page artifacts and generated bindings. They change page state only when a visible approved control exists. The assistant may explain evidence and may render an existing public action block when it resolves through the established action policy, but the assistant does not receive browser-held page authority and cannot manufacture a page selection or recovery operation.

## 7. Acceptance criteria

- The assistant is reachable from every authenticated route in wide, regular, and compact layouts.
- Wide layout supports pointer and keyboard resize plus collapse/restore.
- Collapse, Cancel, and overlay dismissal preserve focus and the appropriate conversation state.
- Streaming prose remains readable while a validated evidence card is inserted into the same assistant turn.
- Cards render only from bounded server-derived public projections and preserve their returned scope, period, basis, and qualification.
- Every graph-scoped narration block is bound to its resolved visual/artifact revision, renders Observed separately from Perspective/Hypothesis, and falls back to Limit when a point of view is unsupported.
- The same question can use the global governed tool surface regardless of the current page.
- Browser network payloads contain no page artifact, canonical selector, token, query plan, or tool authorization field.
- A page remains understandable and actionable when the assistant is collapsed or unavailable.
