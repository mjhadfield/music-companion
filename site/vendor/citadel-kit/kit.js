// Citadel Theme Kit runtime (contract v2).
// Receives live `citadel-theme` (v: 2) messages from Citadel, validates origin and CSS tokens,
// applies data-mode / data-theme / data-family + inline custom properties + --accent-ink,
// and fires `kit:change` on document. Standalone apps can also toggle mode/theme locally.

const VALID_MODES = new Set(['day', 'night']);
const VALID_THEMES = new Set(['citadel', 'lcars', 'lcars25', 'borg']);
const VALID_FAMILIES = new Set(['citadel', 'lcars', 'borg']);
const TOKEN_NAME_RE = /^--[a-z0-9-]+$/;
const BLOCKED_TOKENS = new Set(['--bg-image', '--bg-dim', '--bg-size', '--bg-repeat', '--bg-pos']);
const MAX_VALUE_LEN = 200;

function familyOf(themeId) {
  return themeId === 'lcars25' ? 'lcars' : (VALID_FAMILIES.has(themeId) ? themeId : 'citadel');
}

function parseHexRgb(str) {
  const s = String(str || '').trim();
  let m = s.match(/^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/);
  if (m) {
    const h = m[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  m = s.match(/^#([0-9a-fA-F]{3})$/);
  if (m) {
    const h = m[1];
    return [parseInt(h[0] + h[0], 16), parseInt(h[1] + h[1], 16), parseInt(h[2] + h[2], 16)];
  }
  m = s.match(/^rgba?\(\s*([\d.]+)\s*[,\s]\s*([\d.]+)\s*[,\s]\s*([\d.]+)/);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  return null;
}

function computeAccentInk(root) {
  const accent = getComputedStyle(root).getPropertyValue('--accent').trim();
  const rgb = parseHexRgb(accent);
  if (!rgb) return null;
  const lin = c => {
    const x = c / 255;
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  };
  const lum = 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
  return lum > 0.36 ? '#07060a' : '#ffffff';
}

function isSafeValue(v) {
  if (typeof v !== 'string' || !v || v.length > MAX_VALUE_LEN) return false;
  const low = v.toLowerCase();
  if (low.includes('url(') || low.includes('expression(') || low.includes(';') || low.includes('{') || low.includes('}')) {
    return false;
  }
  return true;
}

/**
 * Initialise the Citadel Theme Kit on a page.
 * @param {object} [opts]
 * @param {string[]} [opts.origins] Allowed Citadel parent origins (location.origin is always allowed for sub-frames).
 * @param {string} [opts.storagePrefix] Prefix for standalone localStorage keys (`<prefix>-mode`, `<prefix>-theme`).
 */
export function initKit({ origins = [], storagePrefix = 'citadel-app' } = {}) {
  const root = document.documentElement;
  const embedded = window.self !== window.top;
  if (embedded) root.classList.add('embedded');

  const allowed = new Set([location.origin, ...origins.filter(Boolean)]);
  let pendingMsg = null;
  let appliedVars = [];
  let state = {
    embedded,
    mode: root.dataset.mode === 'day' ? 'day' : 'night',
    theme: VALID_THEMES.has(root.dataset.theme) ? root.dataset.theme : 'citadel',
    family: VALID_FAMILIES.has(root.dataset.family) ? root.dataset.family : familyOf(root.dataset.theme),
    style: 'standard',
    vars: null,
    bg: false,
    glow: true,
  };

  function clearVars() {
    for (const k of appliedVars) root.style.removeProperty(k);
    appliedVars = [];
    root.style.removeProperty('--accent-ink');
  }

  function updateAccentInk() {
    const ink = computeAccentInk(root);
    if (ink) root.style.setProperty('--accent-ink', ink);
  }

  function emit() {
    document.dispatchEvent(new CustomEvent('kit:change', { detail: { ...state } }));
  }

  function apply({ mode, theme, family, style = 'standard', vars = null, bg = false, glow = true } = {}) {
    const m = VALID_MODES.has(mode) ? mode : state.mode;
    const t = VALID_THEMES.has(theme) ? theme : state.theme;
    const f = VALID_FAMILIES.has(family) ? family : familyOf(t);

    root.dataset.mode = m;
    root.dataset.theme = t;
    root.dataset.family = f;
    root.classList.toggle('has-bg', !!bg);
    root.classList.toggle('no-glow', glow === false);

    clearVars();
    const safeVars = {};
    if (vars && typeof vars === 'object') {
      for (const [k, v] of Object.entries(vars)) {
        if (!TOKEN_NAME_RE.test(k) || BLOCKED_TOKENS.has(k) || !isSafeValue(v)) continue;
        root.style.setProperty(k, v);
        appliedVars.push(k);
        safeVars[k] = v;
      }
    }
    updateAccentInk();

    state = {
      embedded,
      mode: m,
      theme: t,
      family: f,
      style: typeof style === 'string' && style ? style : 'standard',
      vars: appliedVars.length ? safeVars : null,
      bg: !!bg,
      glow: glow !== false,
    };
    emit();
    return state;
  }

  function handleMessage(origin, msg) {
    if (!msg || msg.type !== 'citadel-theme' || msg.v !== 2) return false;
    if (!allowed.has(origin)) {
      pendingMsg = { origin, msg };
      return false;
    }
    if (!VALID_MODES.has(msg.mode) || !VALID_THEMES.has(msg.theme)) return false;
    pendingMsg = null;
    apply({
      mode: msg.mode,
      theme: msg.theme,
      family: msg.family,
      style: msg.style,
      vars: msg.vars,
      bg: msg.bg,
      glow: msg.glow,
    });
    return true;
  }

  window.addEventListener('message', e => { handleMessage(e.origin, e.data); });

  // Compute initial --accent-ink for pre-paint defaults
  updateAccentInk();

  function setMode(m) {
    const next = m === 'day' ? 'day' : 'night';
    if (!embedded) {
      try { localStorage.setItem(`${storagePrefix}-mode`, next); } catch { /* private window */ }
    }
    return apply({ mode: next, theme: state.theme, family: state.family, style: state.style, vars: state.vars, bg: state.bg, glow: state.glow });
  }

  function setTheme(t) {
    const next = VALID_THEMES.has(t) ? t : 'citadel';
    if (!embedded) {
      try { localStorage.setItem(`${storagePrefix}-theme`, next); } catch { /* private window */ }
    }
    return apply({ mode: state.mode, theme: next, family: familyOf(next), style: 'standard', vars: null, bg: false, glow: true });
  }

  function toggleMode() {
    return setMode(state.mode === 'day' ? 'night' : 'day');
  }

  function getState() {
    return { ...state };
  }

  function addOrigins(more = []) {
    for (const o of more) if (o) allowed.add(o);
    if (pendingMsg && allowed.has(pendingMsg.origin)) {
      const p = pendingMsg;
      pendingMsg = null;
      handleMessage(p.origin, p.msg);
    }
  }

  return { getState, setMode, setTheme, toggleMode, apply, addOrigins };
}
