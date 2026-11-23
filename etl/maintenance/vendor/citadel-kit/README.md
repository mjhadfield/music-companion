# Citadel Theme Kit

Zero-build theme kit for apps framed by Citadel. Vendored into each app by `Citadel/tools/sync_kit.py` (never edit inside `vendor/citadel-kit/` — edit `Citadel/web/kit/` and resync).

## Adopt in ~10 lines

```html
<link rel="stylesheet" href="/vendor/citadel-kit/kit-tokens.css">
<script src="/vendor/citadel-kit/kit-init.js" data-storage-prefix="myapp"></script>
<link rel="stylesheet" href="/vendor/citadel-kit/kit.css">
<link rel="stylesheet" href="/css/app.css">
```

```js
import { initKit } from '/vendor/citadel-kit/kit.js';

const kit = initKit({ origins: state.citadel_origins, storagePrefix: 'myapp' });
document.addEventListener('kit:change', ({ detail }) => { /* optional hook */ });
// Standalone Day/Night toggle:
modeBtn.addEventListener('click', () => kit.toggleMode());
```

## Primitives (`ck-`)

- `.ck-app` — full-viewport page shell with theme-appropriate ground and `.has-bg` transparency.
- `.ck-header` — top app bar (Citadel HUD hairline, LCARS pill brand + segmented beam, LCARS 25 command bezel, Borg crosshair bar).
- `.ck-rail` / `.ck-rail__item` — vertical side navigation rail (collapses cleanly on phones); mark active with `.is-active` or `[aria-current="page"]`.
- `.ck-panel` — content surface / card (`.ck-panel__head`, `.ck-panel__body`).
- `.ck-btn` — action button, plus `.ck-btn--primary`, `.ck-btn--quiet`, `.ck-btn--danger`.
- `.ck-tabs` — horizontal tab bar with `.ck-tab` items (`.is-active` or `[aria-selected="true"]`).
- `.ck-input` / `.ck-select` — text inputs, search fields, textareas and select dropdowns.
- `.ck-table` — bordered tabular data table with themed header and row hover.
- `.ck-dialog` — native `<dialog>` modal with themed frame and backdrop.
- `.ck-chip` — small inline status or category badge (`.ck-chip--ok`, `.ck-chip--warn`, `.ck-chip--bad`).
