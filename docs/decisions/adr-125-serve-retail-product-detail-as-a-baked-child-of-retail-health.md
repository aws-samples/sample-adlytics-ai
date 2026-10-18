# ADR-125: Serve retail product detail as a baked child of Retail health
Date: 2026-10-07
Status: Proposed. The owner chose on 2026-10-07 to build the product drill-in as a child artifact of
Retail health. The new API route's `infra/` change is approved within its own build slice, dev only,
recorded by the workflow orchestrator under the owner's delegated overnight authorization (2026-10-07).
Not implemented.

> **Proposed amendment, 2026-10-18.** [ADR-145](adr-145-serve-ten-thousand-campaigns-with-a-sharded-roster-and-streamed-writes.md) (Proposed) would move each product-detail child's body and custody digests out of the parent custody's `product_children` set into the publication's sharded sibling digest index, which every source's siblings use. The parent custody would keep the served products and their server-only references, the closed-set rule would be checked when the children are staged, and the product-detail read would verify the child through the index. This decision stays as written until ADR-145 is accepted.

## Context

Retail health rows aren't interactive today [repo: `frontend/components/RetailHealthView.tsx:351-354`].
A request-time Product Evidence path exists but was never rendered: the `retail-product` route
[repo: `infra/lib/stacks/api-stack.ts:1267`], its handler path
[repo: `src/adapters/inbound/presentation_handlers/handler.py:494`] and module
`src/domain/presentation/retail_product.py`. It sits outside the publish-time pattern of
[ADR-070](adr-070-serve-declared-preset-windows-from-publish-time-artifacts.md). Retail health serves at
most 50 product rows [repo: `src/domain/presentation/retail_health.py:33`], and every Amazon Vendor
Central retail metric is valid at `product` grain
[repo: `contracts/shared/metric-dictionary.yaml:505-718`]. Ad spend has no `product` grain (`:121`).
Parts of `retail_product.py` are sound and already safety-reviewed: the browser-safe display signature
and the opaque row capability (`:142-171`), typed per-metric reasons (`:65-75`, `:354-379`), the daily
series (`:333-344`) and the boundary text (`:25-29`).

## Decision

**A Retail health product row opens product detail in place. Product detail is a child artifact of
`retail-health`, baked with its parent in the same publication, and the old request-time path retires
in the same slice.**

1. **Page.** `retail-health` becomes `mode: selectable_parent_child` with a second artifact,
   `RetailProductDetail` v1. Detail opens on `/retail-health/`, on the parent's window and freshness,
   with one history marker, so Back closes it.
2. **Bake.** For each preset, one child body per served product row, in the same publication as the
   parent. The parent custody gains a closed `product_children: { <product_ref>: { body_digest,
   custody_digest } }` set, written and digest-checked before the pointer advances. `product_ref` never
   leaves the server.
3. **Rows and request.** Each row gains a `selection_reference` capability and a safe display signature.
   `POST /api/presentation/retail-health-product` takes the capability and the parent's temporal
   selection, refuses a window that differs from the parent's, and reads the child through the parent
   pointer's custody. A missing child or digest mismatch is the malformed-artifact refusal, never a
   fallback. The path is named apart from the retiring v1 id `retail-product-detail`, so the cleanup
   can't catch it.
4. **Content.** The same four tile groups as Retail health, under the same headings: demand and
   conversion; sales and fulfilment, with average selling price; traffic value and margin, with revenue
   per glance view, cost of goods shipped and the Amazon margin proxy; returns, with return reasons as a
   typed absence. Then the Retail health insights that name the product, with dialog stats the server
   writes for this product; shipped revenue and glance views by day; ad-attributed purchases and revenue
   per channel, each with its own change, with no total, no product ROAS and nothing added to shipped
   revenue; a pointer to Gateway products for new-to-brand purchases; and Amazon Marketing Cloud (AMC)
   retail purchases as a paid feature. Each tile opens its daily focus through `retail-metric-focus`
   with the same capability.
5. **Next step.** The `retail-movers` card gains an `open_product_detail` step for the product it names
   ([ADR-122](adr-122-declare-next-step-targets-as-a-typed-union-checked-against-navigation.md)).
6. **Retires in the same slice**
   ([ADR-117](adr-117-keep-pages-and-apis-only-on-merit-and-retire-a-replaced-surface-with-its-replacement.md)
   decision 2): `RETAIL_PRODUCT_PATH` and its handler code
   [repo: `handler.py:494`, `:519`, `:540`, `:589`, `:2719`, `:4251`, `:4666`]; the route
   `api-stack.ts:1267`; the v2 `retail-detail` and v1 `retail-product-detail` page entries; the frontend
   `retail-detail` and `/retail-detail/` bindings [repo: `frontend/lib/page-bindings.ts:14`, `:30`,
   `:152`, `:178`]; every symbol that references them; and `retail_product.py`, after the reused types
   move into the child's module.

## Consequences

- Up to 150 more stored bodies per publication (50 rows × 3 presets) and one AMC product query per
  preset.
- A product page always shows the parent's window, so its figures add up to the Retail health tiles.
- A preset change re-serves the parent and re-opens the product when exactly one row matches its
  signature; otherwise detail closes and the list says why.
- There's never a second path to product detail.

## Alternatives considered

- **Revive the request-time `retail-product` API.** Outside ADR-070's pattern, and on its own window.
- **A separate `/products/` page.** A second window and a second list for the same rows.
- **Add product columns to the Retail health table.** It would scroll sideways at 832 px.

## Review trigger

Reopen if Retail health serves more than 50 rows, or Amazon Vendor Central adds a per-product field
with its own window.
