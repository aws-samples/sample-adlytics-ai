# Theme preference and restrained-glass contract

| | |
|---|---|
| **Type** | Shared UI shell interface concept |
| **Status** | Design baseline for the implementation specification; static mockups illustrate the control but do not execute it |
| **Owns** | Dynamic System/Light/Dark preference, persistence boundary, effective-theme resolution, restrained-glass tokens, and shell acceptance |
| **Parent** | [Decision Workspace UI](README.md) |

## 1. Purpose and boundary

The Decision Workspace shell supports a user-selectable color theme while preserving governed analytical behavior. The preference is presentation-only: it changes shell and component tokens, never a route, URL, presentation request, temporal selection, artifact, capability, qualification, exact value, assistant conversation, or tool authority.

The visual polish is **restrained glass**. It adds depth to workspace chrome and assistant surfaces without reducing evidence legibility. Tables, exact-data disclosures, metric values, chart canvases, typed state content, and accessibility-critical text remain grounded on opaque evidence surfaces.

## 2. Preference and effective-theme model

```text
ThemePreference.v1 = "system" | "light" | "dark"
EffectiveTheme = "light" | "dark"
```

- `system` is the default. Its effective theme follows the operating-system `prefers-color-scheme` result and reacts to a later system change.
- `light` and `dark` override the operating-system result immediately.
- The chosen `ThemePreference.v1` persists only as a local, presentation-only browser preference. It contains no customer, analytical, identity, capability, or conversation data and is never sent to an API.
- The application applies `data-theme="system"`, `data-theme="light"`, or `data-theme="dark"` to the document root. The CSS token layer resolves the effective palette. No component owns a second palette or hardcoded theme color.
- Initial server-rendered or static markup defaults to `system`; production prevents a visible theme flash by resolving the stored local preference before first interactive paint where the framework permits it.

## 3. App-shell theme control

The control is one labelled app-shell button that opens a mutually exclusive theme chooser:

```text
Theme trigger button
  visible label: "Theme: System" | "Theme: Light" | "Theme: Dark"
  aria-haspopup="menu"
  aria-expanded=<open state>
  aria-controls=<theme menu ID>

Theme menu
  role="menu"
  options: role="menuitemradio"
  values: System, Light, Dark
  exactly one aria-checked="true"
```

The trigger belongs in the shared top-bar action region, immediately before the reporting-period summary. It is the sole page-level theme entry point; the sidebar, assistant, and individual evidence modules do not duplicate it.

Keyboard and focus behavior:

1. **Enter** or **Space** opens the chooser from the trigger.
2. **Arrow keys**, **Home**, and **End** move among options; **Enter** or **Space** selects one.
3. **Escape** closes the chooser and restores focus to the trigger.
4. Selecting an option updates the root `data-theme`, persists the local preference, announces the new effective preference, closes the chooser, and returns focus to the trigger.
5. Selected state is exposed through text and `aria-checked`; it is never color-only.

Static mockups show a System-selected trigger and a visual reference for the chooser. They contain no script or preference persistence.

## 4. Token and glass model

The shared stylesheet owns all light and dark values. It defines semantic glass tokens for:

- chrome surface and border;
- assistant/inline-evidence surface and border;
- highlight and shadow;
- blur strength; and
- fully opaque fallback surfaces.

Components consume only those tokens. The restrained glass treatment applies to the sidebar, top bar, assistant inspector/overlay/sheet, theme chooser, assistant bubble, and inline evidence card. It does not apply transparent/blurred backgrounds to exact-data tables, metric cards, chart cards, state cards, or table wrappers.

`backdrop-filter` is progressive enhancement only. Without support, or where transparency is reduced, every glass surface falls back to a high-opacity or opaque token with the same border, contrast, and hierarchy. The app never relies on blur, translucency, shadow, or color alone to communicate selection, state, or data quality.

## 5. Responsive and assistant behavior

The theme control remains in the shared top bar in wide, regular, and compact layouts. At narrow width it wraps inside the top-bar action region before the period text; it does not disappear, move into the assistant, or overlap the skip-link target. Changing theme does not open, close, reposition, reload, or otherwise alter the assistant inspector, its streaming state, or its focus-management behavior.

## 6. Shared acceptance

The shell theme work is complete only when unit/browser/manual evidence proves:

1. System, Light, and Dark each resolve the correct token set; System reacts to an operating-system preference change.
2. A local theme preference persists across a reload but is not sent to any page, assistant, or analytical API request.
3. The app-shell trigger and chooser support keyboard operation, visible focus, correct radio semantics, Escape/focus restoration, and screen-reader announcement.
4. Light and dark themes preserve readable contrast, non-color state cues, exact-data access, chart labels, assistant cards, and selected-control affordance.
5. Chrome glass surfaces use opaque fallbacks when blur/transparency support is absent or reduced; evidence-bearing surfaces remain opaque in every theme.
6. Theme changes do not refresh artifacts, clear capabilities, change reporting windows, mutate assistant state, or create a stale-response race.
7. Wide, regular, and compact layouts preserve the same control, reading order, assistant behavior, and evidence access.
8. The matching mockup revision, gallery reference, shell tests, accessibility review, and non-production browser journey are recorded before route retirement.
