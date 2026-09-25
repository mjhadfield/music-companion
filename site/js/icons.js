// Inline SVG sprite, same pattern as Citadel's own web/js/icons.js. Use: <svg class="i"><use href="#i-home"/></svg>
const paths = {
  home:   '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  disc:   '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M6 12a6 6 0 0 1 6-6"/>',
  back:   '<path d="M15 18l-6-6 6-6"/>',
  chevron:'<path d="M6 9l6 6 6-6"/>',
  moon:   '<path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a7 7 0 0 0 10.7 10.7z"/>',
  sun:    '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
  bolt:   '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="M20 20l-5.5-5.5"/>',
  gear:   '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
};

const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
svg.setAttribute('width', '0'); svg.setAttribute('height', '0');
svg.setAttribute('aria-hidden', 'true'); svg.style.position = 'absolute';
svg.innerHTML = Object.entries(paths)
  .map(([k, d]) => `<symbol id="i-${k}" viewBox="0 0 24 24">${d}</symbol>`).join('');
document.body.prepend(svg);
