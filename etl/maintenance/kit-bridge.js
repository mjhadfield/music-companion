import { initKit } from '/vendor/citadel-kit/kit.js';

// Migrate legacy standalone maintenance theme ("light" / "dark") once.
if (window.self === window.top) {
  try {
    if (!localStorage.getItem('mc-maint-mode')) {
      const legacy = localStorage.getItem('theme');
      if (legacy === 'light') localStorage.setItem('mc-maint-mode', 'day');
      else if (legacy === 'dark') localStorage.setItem('mc-maint-mode', 'night');
    }
  } catch { /* ignore */ }
}

// Allowed Citadel origins come from /api/publish-status (shared.js calls mcKit.addOrigins);
// kit.js queues any early postMessage until addOrigins() arrives.
const kit = initKit({ origins: [], storagePrefix: 'mc-maint' });
window.mcKit = kit;
document.dispatchEvent(new CustomEvent('mc-kit:ready'));
