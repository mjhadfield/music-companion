import { initKit } from '../vendor/citadel-kit/kit.js';

const DEFAULT_ORIGINS = [
  'http://192.168.0.66:8080',
  'http://127.0.0.1:8080',
  'http://localhost:8080',
  'http://100.127.118.96:8080',
];

// When running on loopback during local dev/scratch testing, also allow Citadel's scratch ports.
const origins = [...DEFAULT_ORIGINS];
if (location.hostname === '127.0.0.1' || location.hostname === 'localhost') {
  for (const h of ['127.0.0.1', 'localhost']) {
    for (const p of [18080, 18081, 19080, 19081]) {
      origins.push(`http://${h}:${p}`);
    }
  }
}

const isFramed = window.self !== window.top;
const framedByCitadel = (() => {
  if (!isFramed) return false;
  if (!document.referrer) return true;
  try {
    return origins.includes(new URL(document.referrer).origin);
  } catch {
    return true;
  }
})();

// Migrate legacy standalone site theme ("light" / "dark") before initKit reads storage;
// standalone site defaults to Day when no choice has been saved yet.
if (!isFramed) {
  try {
    if (!localStorage.getItem('mc-site-mode')) {
      const legacy = localStorage.getItem('theme');
      localStorage.setItem('mc-site-mode', legacy === 'dark' ? 'night' : 'day');
    }
  } catch { /* storage blocked */ }
}

const kit = initKit({ origins, storagePrefix: 'mc-site' });

// Framed by a non-Citadel page (e.g. Mike's personal website terminal): honour ?theme= / ?citadel_mode=
// for the first paint, then keep the local Day/Night toggle without writing localStorage.
if (isFramed && !framedByCitadel) {
  const qs = new URLSearchParams(location.search);
  const want = qs.get('citadel_mode') || (qs.get('theme') === 'dark' ? 'night' : qs.get('theme') === 'light' ? 'day' : 'day');
  document.documentElement.dataset.mode = want === 'night' ? 'night' : 'day';
}

window.mcKit = {
  ...kit,
  framedByCitadel,
  isFramed,
};

document.dispatchEvent(new CustomEvent('mc-kit:ready'));
