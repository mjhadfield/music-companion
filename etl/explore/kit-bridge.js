import { initKit } from '/vendor/citadel-kit/kit.js';

const DEFAULT_ORIGINS = [
  'http://192.168.0.66:8080',
  'http://127.0.0.1:8080',
  'http://localhost:8080',
  'http://100.127.118.96:8080',
];

const kit = initKit({ origins: DEFAULT_ORIGINS, storagePrefix: 'mc-explore' });
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
