import { initKit } from '/vendor/citadel-kit/kit.js';

// Allowed Citadel origins come from /api/schema (app.js calls mcKit.addOrigins);
// kit.js queues any early postMessage until addOrigins() arrives.
const kit = initKit({ origins: [], storagePrefix: 'mc-explore' });
window.mcKit = kit;

const btn = document.getElementById('theme-toggle');
if (btn) {
  if (kit.getState().embedded) {
    btn.hidden = true;
  } else {
    const sync = () => {
      const day = kit.getState().mode === 'day';
      btn.textContent = day ? 'Night' : 'Day';
      btn.title = day ? 'Switch to Night' : 'Switch to Day';
      btn.setAttribute('aria-label', btn.title);
    };
    btn.addEventListener('click', () => {
      kit.setMode(kit.getState().mode === 'day' ? 'night' : 'day');
      sync();
    });
    document.addEventListener('kit:change', sync);
    sync();
  }
}
