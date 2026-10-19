# Decision Workspace design-readiness review

> **Superseded 2026-09-15.** This review records the six-page `2026-09-08.1` candidate as a historical snapshot. The Channel Robustness, Product Evidence, Marketplace Movement, Evidence Health, and Media Efficiency page packages were retired from the frontend and removed from source control on 2026-09-15; their presentation APIs remain deployed. References to those packages below are retained as plain text. The current roster is owned by [Decision Workspace UI](README.md).

| | |
|---|---|
| **Review snapshot** | 2026-09-08 |
| **Scope** | The six page packages under `docs/ui/`, their README/HLD/LLD layers, and their linked static mockups |
| **Purpose** | Record implementation-blocking gaps, manifest closure, and mockup/spec reconciliation work without making up missing artifact contracts |
| **Review baseline** | Candidate revision `2026-09-08.1`; [Decision Workspace UI](README.md) and [documentation conventions](../conventions.md) |
| **Parent disposition** | Approved for implementation on 2026-09-08 for manifest revision `2026-09-08.1` and the declared six-page closure |

## Review summary

Every reviewed page has the required package layers: a business-purpose `README.md`, `high-level-design.md`, `low-level-design.md`, and one linked `mockup.html`. A separate “business README” is neither required nor desirable: the package README is its business contract owner.

Three mockups were corrected in this review where the current contracts made the result unambiguous:

- **Channel Robustness** now shows the parent-issued daily-evidence control, makes the selected daily view explicitly post-redemption, and labels narration as planned.
- **Evidence Health** now shows exactly the two V1 checks, removes a false recovery affordance for usable evidence, discloses source/grain/load metadata, and labels narration as planned.
- **Marketplace Movement** now represents active V1 as table-and-disclosure evidence rather than an unsupported visual/narration surface.

The candidate revision resolves the dual V1/V2 declaration migration, exact inclusive Custom policy, and Media parent comparison/cardinality/transaction boundary. It does not authorize production implementation or acceptance evidence until the parent UI owner records a dated approval or superseding disposition for the generated manifest closure.

## Historical review findings

These findings prompted the design-baseline decisions below. They are retained as traceability evidence; the authoritative current disposition is the [cross-page acceptance matrix](#cross-page-acceptance-matrix--candidate-design-revision-2026-09-081).

| Page package | Layer coverage | Blocking gap | Correct owner and required closure | Mockup state |
|---|---|---|---|---|
| [Campaign Portfolio](campaign-portfolio/README.md) | Complete | The V2 delivery section lists metric groups but does not define a closed public list/detail payload: required/nullable fields, typed delivery outcomes and recovery, comparison shape, visual identity, narration-reference fallback, and exact series row shape remain open. | [LLD §6](campaign-portfolio/low-level-design.md#v2-delivery-contract): add the versioned artifact/envelope and strict-decoder acceptance before V2 delivery implementation. | The current static target also selects 14D while showing May 6–12 and seven points; reconcile its period, visual points, exact-data table, and selected control in the same change as the V2 contract. |
| Channel Robustness | Complete | The page accepts an explicit visible window but does not define the server-declared selectable windows, Custom bounds, or typed refusal/unavailable behavior required for an interactive temporal control. | LLD §6: version the parent response/control contract; then add browser acceptance for returned controls, refusal, and resolution. | Reconciled for capability-gated daily detail and planned narration. A temporal-control mockup must wait for the closed response contract. |
| Product Evidence | Complete | V1 accepts a visible window but does not return available-window metadata; it also lacks a visual identity/exact-data contract and an artifact-bound narration reference. The static target’s 14D control and charts therefore exceed the public V1 contract. | LLD §6–7: define a successor visual/temporal artifact and its renderer acceptance, or reduce the mockup to the current table renderer. Use [Visual Narration Harness](visual-narration-harness.md) only after a page reference is defined. | Open: update only after the successor contract decision. Its current 14D/May 6–12/seven-point mismatch must be corrected with that change. |
| Marketplace Movement | Complete | If a user-changeable visible window is intended, V1 lacks the returned allowed-window capability/list and closed refusal/expiry semantics. | LLD §6: add a versioned temporal selection contract; otherwise state that V1 has no user-changeable window in the README/HLD and mockup. | Reconciled to V1: no unsupported visual or narration. |
| Evidence Health | Complete | The page’s artifact-state table does not define route transport states—initial loading, session failure, malformed payload, dependency failure, and retry/no-retry rendering. These are distinct from evidence-condition states. The intended fixed-window versus selectable-window behavior is also not explicit. | LLD §6: add the page transport-state and temporal-control contract, referring to the shared policy rather than duplicating it. | Reconciled to the two-check V1 artifact, no-self-service recovery rules, required provenance, `loaded_at`, and planned narration. |
| Media Efficiency | Complete | The proposed route has no page declaration, scope-list response envelope, exact closed list/artifact outcomes, allowed-window representation, recovery/action vocabulary, exact dated-series rows, implementation binding, handler, strict decoder, or proof. | LLD §6 and §9: first specify `MediaEfficiencyArtifact.v1` and its parent scope/window-list artifact, then materialize the declared schema/code/test seams. | Open target only. Do not treat its selected scope, fixed windows, delivered chart, or narration as implementation-ready until the parent-list and artifact contracts close. |

## Cross-page acceptance matrix — candidate design revision `2026-09-08.1`

This matrix is the approved implementation baseline. The parent disposition recorded in this document and [Decision Workspace UI](README.md) applies to generated manifest revision `2026-09-08.1` and its declared six-page closure. A mockup is visual-reference evidence only; the build/acceptance columns remain pending until their returned test or non-production results are read.

| Page | Revision and mockup alignment | Temporal/artifact contract | Visual and narration state | Required build and acceptance evidence | Cutover ID | Current status |
|---|---|---|---|---|---|---|
| [Campaign Portfolio](campaign-portfolio/README.md) | README, package design, mock metadata, and gallery use `2026-09-08.1`; selected mock state is 7D with 1D unavailable. | `PresentationResult.v2`, `TemporalControl.v1`, required equal prior, new list/capability set per window, `CampaignAttributionDetail.v2`. | Delivery/purchase exact-data visuals; planned Observed/Limit narration only. | V2 schema/binding, reducer/handler/custody, strict frontend decoder/controller, 1D and Custom refusal, exact data, browser/a11y, non-production route result. | `CUT-CAMPAIGN-01` | Approved baseline; implementation pending |
| Channel Robustness | Revision matched; mock shows 7D parent and post-redemption child. | `PresentationResult.v2`, selectable hybrid control, required equal prior, child preserves parent temporal resolution. | Daily exact-data visual; planned Observed/Limit narration only. | V2 transport migration, production browser flow, parent/child decode, expiry refresh, stale cancellation, browser/a11y, non-production route result. | `CUT-CHANNEL-01` | Approved baseline; browser implementation pending |
| Product Evidence | Revision matched; mock shows 7D, a 1D non-trend rule, and bounded Custom. | `PresentationResult.v2`, optional comparison, window-bound product capability, `RetailProductArtifact.v2` visual extension. | Separate sales/traffic exact-data visuals; planned visual Limit; no causal blend. | V2 schema/binding, artifact/custody/decoder, 1D non-trend, Custom/refusal, split-evidence browser/a11y, non-production route result. | `CUT-PRODUCT-01` | Approved baseline; successor implementation pending |
| Marketplace Movement | Revision matched; mock shows 7D hybrid control and table-only V1. | `PresentationResult.v2`, required equal prior, V1 artifact outcomes remain unchanged. | No chart or page narration; native-currency pair table is the visual reference. | V2 transport migration, strict artifact binding, Custom/refusal, four outcomes, no FX/rank regression, browser/a11y, non-production route result. | `CUT-MARKETPLACE-01` | Approved baseline; implementation pending |
| Evidence Health | Revision matched; mock shows 7D hybrid control, two checks, and no comparison. | `PresentationResult.v2`, exact-source selectable control, no comparator, closed transport-state table. | No history chart; planned narration is deterministic Evidence/Limit only. | V2 transport migration, two-check source fidelity, temporal refusal, transport/page-state split, browser/a11y, non-production route result. | `CUT-HEALTH-01` | Approved baseline; implementation pending |
| Media Efficiency | Revision matched; mock begins with parent scope/window list then a selected 7D child. | `MediaEfficiencyScopeList.v1`, `PresentationResult.v2`, `MediaEfficiencyArtifact.v1`, opaque scope/window capabilities. | Exact-data delivery visual; typed audience absence; planned Observed/Limit narration only. | Page declaration/binding, list/artifact domain/handler/custody, strict frontend flow, cancellation, cohort boundaries, browser/a11y, non-production parent/child route result. | `CUT-MEDIA-01` | Approved baseline; new implementation pending |

## Shared design baseline gates

| Gate | Design baseline | Build/acceptance requirement | Current status |
|---|---|---|---|
| Revision binding | The parent mockup policy requires README revision = mock metadata = gallery revision; all six pages use `2026-09-08.1`. | Add the documented check and verify it for every future revision. | Approved manifest closure current |
| Hybrid temporal controls | [TemporalControl.v1](temporal-control-contract.md) defines the common request/result, invalidation, and accessibility behavior; page profiles define route constraints. | Versioned schema, handler, strict decoder, controller, browser, and non-production evidence for every page. | Approved baseline; implementation pending |
| Theme preference and restrained glass | [ThemePreference.v1](theme-preference-contract.md) defines System/Light/Dark selection, local presentation-only persistence, root token overrides, accessible chooser behavior, and opaque glass fallback. | Shared shell tokens/control, local preference handling, keyboard/a11y, light/dark/OS behavior, no analytical-state mutation, and shell browser acceptance. | Approved baseline; implementation pending |
| Visual narration | The parent policy distinguishes no visual, planned deterministic Observed/Limit, and enabled narration. | Shared Harness, page-minted reference, custody/output validation, cancellation/fallback, browser tests, and non-production proof. | Approved baseline; implementation pending |
| Cutover inventory | [Workspace implementation cutover](implementation-cutover.md#21-route-and-component-cutover-inventory) defines a `CUT-*` record for every route/shared boundary. | Verify source paths/CSS/tests/deletion scope and read replacement acceptance before retirement. | Design ready; inventory discovery pending |

## Historical blocker record and disposition

| Original gap | Design disposition in revision `2026-09-08.1` | Remaining build gate |
|---|---|---|
| Mockup/spec revision binding | Closed as a design rule in [Decision Workspace UI](README.md#7-durable-mockup-policy); all six page README/mock/gallery values match this revision. | Implement the automated equality check before a later revision is accepted. |
| Visual narration dependency | Closed as a design boundary: every current mockup uses planned deterministic Evidence/Observed/Limit treatment, not enabled Perspective/Hypothesis behavior. | Implement and validate the [Visual Narration Harness](visual-narration-harness.md#7-delivery-requirements) before enabling a page reference. |
| Cutover inventory | Closed as a planning structure in [Workspace implementation cutover](implementation-cutover.md#21-route-and-component-cutover-inventory). | Discover verified source paths/CSS/tests and record acceptance before each retirement. |

## Closure workflow

1. Resolve each blocker in the owning document; do not relax an LLD to match a fictional mockup.
2. Update that page’s mockup in the same change, including wide/regular/compact and exact-data review evidence where a visual is introduced.
3. Record the page-level specification/mockup revision binding after the shared policy exists.
4. Run the package’s documented contract, browser, link, and diagram checks. A static mockup or successful build alone is not delivery evidence.

## Adopted closure strategy

The following design decisions are the implementation-spec baseline for revision `2026-09-08.1`. They turn each historical blocker into a bounded implementation task. The specification must preserve these ownership boundaries and update the mockup in the same change as its owning contract.

### Shared foundations first

| Foundation | Design closure | Owning file | Acceptance evidence |
|---|---|---|---|
| Page design revision binding | Add a human-readable `Design revision` to each page `README.md` and a matching `meta[name="design-revision"]` to its `mockup.html`. The parent policy records the matching requirement and the gallery surfaces the revision. A revision advances only when its README/HLD/LLD and mockup describe the same approved behavior. | [Decision Workspace UI](README.md#7-durable-mockup-policy), then each child package and [mockup gallery](mockups/index.html) | A check rejects a missing or mismatched child README/mockup revision; review confirms the gallery shows the matching revision. |
| Theme preference and restrained glass | Add one shared shell trigger, System/Light/Dark preference contract, token overrides, high-contrast chrome treatment, and opaque fallback. The preference remains local presentation state only. | [Theme preference and restrained-glass contract](theme-preference-contract.md) and shared shell/primitive records | Browser and manual evidence prove no analytical-state mutation, keyboard/focus behavior, system/explicit themes, contrast, fallback, and responsive assistant continuity. |
| Visual narration | Complete the shared Harness before any page enables Perspective or Hypothesis. Each page first declares an eligible visual ID, an opaque revision-bound narration reference, and its deterministic Observed/Limit fallback; it never sends page values or prompts from the browser. | [Visual Narration Harness](visual-narration-harness.md) and the individual page LLD | Unit custody/output validation, browser cancellation/fallback tests, and the required non-production Harness integration result. |
| Cutover inventory | Add a route-by-route inventory: legacy component, CSS/selector, test, replacement route/component, owner, and the acceptance evidence required before removal. Retire a legacy item only after its replacement route is accepted. | [Workspace implementation cutover](implementation-cutover.md) | The inventory is complete for every changed route, and the documented acceptance result exists before any deletion. |

### Reusable page-contract patterns

These are shared design-contract patterns, not a shared public payload schema. The parent UI document keeps only the invariant; every page LLD owns its exact versioned fields and enum vocabulary.

| Pattern | Proposed minimum closure |
|---|---|
| **Temporal control** | A page that exposes a selector returns: resolved current window; comparison validity/window when supported; display grain; ordered permitted choices with a safe display label and opaque choice reference or concrete declared request shape; Custom availability plus min/max bounds and re-grain/refusal behavior; and a closed unavailable/refused outcome. The browser sends only the declared selection input and displays the returned resolved scope. |
| **Visual evidence** | A visual is declared only when its artifact contains a stable visual ID, source/basis/unit/grain/qualification, ordered exact rows or points, and an exact-data alternative. The browser maps returned values to pixels but derives no metrics. |
| **Transport state** | Page LLDs distinguish initial loading, request superseded/cancelled, session/authorization failure, custody/expiry refresh, dependency failure, malformed/unknown artifact, and retry eligibility. These states remain distinct from analytical outcomes such as `qualified` or `insufficient`. |
| **Mockup state coverage** | Each child mockup shows the ready state plus the page-specific blocked/unavailable path. Loading and page-issue treatments may use the shared shell treatment, but child pages identify their trigger, retained context, and recovery behavior. |

### Page-by-page closure plans

#### Campaign Portfolio

1. **Close the V2 artifact before code.** In [Campaign Portfolio LLD §6](campaign-portfolio/low-level-design.md#v2-delivery-contract), specify a versioned selected-detail envelope that preserves V1 fields and adds: a closed delivery outcome; ordered summary and comparison slots; ordered dated delivery points; typed DSP-quality availability; evidence/qualification for every returned slot; and a visual declaration with an exact-data alternative. Define required versus nullable values and the recovery/disposition for every unavailable or insufficient result.
2. **Define the V2 visual boundary.** Give each supported visual a stable ID, explicit metric cohort, resolved period/grain, and only then an optional narration-reference field. Until the shared Harness is accepted, mockups show deterministic Observed/Limit rather than delivered Perspective/Hypothesis behavior.
3. **Return temporal choices from the list artifact.** Add the page-specific temporal control pattern before enabling the mockup’s window selector. Detail redemption restores the selection snapshot; it must not accept a new browser-provided period.
4. **Align the mockup only after the contract decision.** Choose one returned period and make the selected control, displayed dates, plotted points, summary text, and exact table represent that same period.
5. **Accept with strict fixtures.** Test V1 preservation, V2 slot order, source/basis/grain, DSP-versus-Sponsored absence, series reconciliation, capability expiry, temporal refusal, exact-data access, and stale request/narration cancellation.

#### Channel Robustness

1. **Version the parent temporal response.** Extend the parent V2 response in Channel Robustness LLD §6 with the page’s returned temporal-control declaration. Preserve the existing server-derived comparison rule and refuse arbitrary browser comparators.
2. **Keep the child boundary unchanged.** The row capability remains the sole input for selected daily evidence; a new parent temporal selection clears the old child and its narration state before issuing fresh row capabilities.
3. **Add browser acceptance.** Cover default, allowed selection, Custom refusal/re-grain, response supersession, expired row capability, parent refresh, keyboard focus restoration, and the nondisclosing invalid-selection path.
4. **Enable narration last.** Mint a reference only for an already rendered parent matrix or child visual after the shared Harness closes; otherwise retain the mockup’s planned label and deterministic Limit.

#### Product Evidence

1. **Use the selected successor path.** `RetailProductArtifact.v2` adds the temporal and visual declarations while preserving V1 table behavior and evidence semantics. The package keeps separate sales and traffic series, and the mockup marks the resulting visuals as a fictional V2 target.
2. **Implement the selected successor in Product Evidence LLD §6–7.** Return a page-specific temporal declaration from discovery; restore the selected window through the opaque selection snapshot; define separate visual IDs and exact-data rows for sales and traffic; preserve distinct basis/qualification; and allow narration references only per eligible visual.
3. **Constrain the assistant projection.** The assistant card must preserve the basis and qualification of each value rather than place retail revenue inside a traffic-only evidence card.
4. **Accept the split evidence contract.** Reject client conversion/causal calculations, incompatible sales/traffic presentation, absent exact alternatives, mismatched safe selection signatures, and stale responses after a changed window or product.

#### Marketplace Movement

1. **Implement the shared hybrid control.** Marketplace returns the page profile from [TemporalControl.v1](temporal-control-contract.md): server-declared preset availability, bounded Custom, a required equal-length prior comparator, and a distinct temporal-unavailable result. The mockup shows the 7D selected state without changing V1’s native-currency/table-only boundary.
2. **Treat analytical extensions as successors.** A dated visual, narration, FX conversion, cross-market magnitude comparison, or generated action still requires its own artifact contract. Hybrid controls do not add any of those behaviors.
3. **Accept V1 with hybrid transport.** Cover all declared controls, Custom bounds/refusal/unavailability, exact-period binding, stale response suppression, native-currency preservation after every new window, accessible control/error behavior, and proof that neither a changed date nor an added control creates a dated trend, FX comparison, action, or browser arithmetic.

#### Evidence Health

1. **Add the route controller contract.** In Evidence Health LLD §6, preserve the compact transport-state table: loading; successful closed artifact; session/authorization issue; malformed artifact/page issue; dependency failure; and explicit no-retry versus authorized retry. Keep these outside the two-check analytical condition union.
2. **Implement the hybrid exact-source control.** Evidence Health returns server-declared preset availability and bounded Custom through [TemporalControl.v1](temporal-control-contract.md), always with `comparison: none`. A Custom change clears/reloads the complete current-state artifact. An unsupported selection is a temporal result, not a health diagnosis or silent refresh.
3. **Keep recovery closed.** Available and qualified checks render neither action nor no-self-service recovery. Blocked checks render exactly one declared action candidate or one no-self-service disposition; neither becomes a browser control until a generated action is authorized.
4. **Accept source fidelity.** Browser tests verify two ordered checks, AMC Sponsored-ad-exposed-user provenance, equal source/population/grain, `loaded_at`, denominator anchors, non-additivity, and separate transport versus analytical state rendering.

#### Media Efficiency

1. **Start with the parent list artifact, not the chart.** Define a versioned scope/window-list response in Media Efficiency LLD §6: closed `choices|empty|refusal` outcomes; safe scope signatures; opaque scope/window references; returned window declarations; expiry/revision/custody behavior; reasons; and refresh-list disposition.
2. **Define `MediaEfficiencyArtifact.v1` as the child contract.** Close the top-level envelope, outcome/recovery vocabulary, delivery and DSP-viewability slot order, typed audience unavailability, exact dated rows, evidence metadata, visual IDs, exact-data alternative, and eligible narration-reference rules. Keep all ratio calculation and audience compatibility enforcement server-side.
3. **Materialize the declared seams in order.** Add the page declaration and generated binding, domain records/reducer/serializer, capability persistence and handler validation, strict frontend decoders/controller, route view, fixture/unit/contract tests, then browser and non-production acceptance.
4. **Use the staged mockup as implementation criteria.** The parent scope/window list is the first render state; the ready cohort, exact-data chart, and planned narration fallback are then rendered only from the strictly decoded child contract.
5. **Accept cohort boundaries.** Test that delivery and DSP viewability can be available while audience is typed unavailable; that observed and estimated viewability never merge; and that no sponsored substitution, population combination, browser ratio, or client aggregate is possible.

### Adopted delivery sequence

1. Implement the shared `PresentationResult.v2`/`TemporalControl.v1` transport, revision-binding check, narration fallback boundary, and cutover-inventory discovery workflow.
2. Deliver the existing-artifact pages in dependency order: Marketplace Movement, Evidence Health, then Channel Robustness, recording each route’s browser and non-production acceptance.
3. Deliver Campaign Portfolio V2 and Product Evidence V2 successor artifacts, controllers, mockup-aligned exact-data visuals, and their acceptance evidence.
4. Deliver Media Efficiency’s parent scope-list, child artifact, capability custody, typed cohort states, and end-to-end parent/child acceptance.
5. For every route, complete its `CUT-*` inventory, read the matching non-production acceptance result, then mark it eligible for legacy retirement.

## Baseline verification record

This record verifies the documentation and fictional-reference baseline only; it is not evidence of production implementation or route acceptance.

| Check | Result |
|---|---|
| Internal links and anchors | `make check-links` resolved 1,201 internal links. |
| Diagrams | `make check-diagrams` accepted 4 Mermaid blocks and 49 inline SVG diagrams. |
| Revision binding | All six page README values, mock metadata values, and gallery labels match `2026-09-08.1`. |
| Mockup markup | All six page mockups parse successfully. |
| Exact-data reconciliation | Campaign delivery reconciles to `$12,980` spend and `31,660` clicks; Channel parent/child selected spend reconciles to `$42,920`; Product sales/traffic totals reconcile to `$18,420`, `624`, and `8,910`; Media delivery reconciles to `$43,290` spend and `33,130` clicks. |
| Media scope state | `window_unavailable` remains distinct from `MediaEfficiencyScopeList.v1` outcomes, which are only `choices` or `empty`. |
| Theme/glass reference | All six mockups expose one System-selected shell trigger and three labelled chooser options; CSS validates explicit System/Light/Dark token paths, opaque chooser fallbacks, and opaque inline chart canvases. |

The **Approved baseline; implementation pending** matrix statuses above are frozen at this snapshot and were not updated after supersession; current page status is owned by [Decision Workspace UI](README.md).
