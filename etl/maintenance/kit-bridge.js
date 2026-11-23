import { initKit } from '/vendor/citadel-kit/kit.js';

const DEFAULT_ORIGINS = [
  'http://192.168.0.66:8080',
  'http://127.0.0.1:8080',
  'http://localhost:8080',
  'http://100.127.118.96:8080',
];

const origins = [...DEFAULT_ORIGINS];
if (location.hostname === '127.0.0.1' || location.hostname === 'localhost') {
  try {
    if (document.referrer) {
      const ref = new URL(document.referrer);
      if ((ref.hostname === '127.0.0.1' || ref.hostname === 'localhost') && !origins.includes(ref.origin)) {
        origins.push(ref.origin);
      }
    }
  } catch { /* ignore */ }
}

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

const kit = initKit({ origins, storagePrefix: 'mc-maint' });
window.mcKit = kit;
document.dispatchEvent(new CustomEvent('mc-kit:ready'));
