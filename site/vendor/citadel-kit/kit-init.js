// Pre-paint Citadel theme kit initialiser. Plain script (not a module): runs before CSS paints
// so framed and standalone apps never flash the wrong theme or mode.
// Optional attribute on the <script> tag: data-storage-prefix="myapp" (default: "citadel-app").
try {
  var r = document.documentElement;
  var script = document.currentScript;
  var prefix = (script && script.getAttribute('data-storage-prefix')) || 'citadel-app';
  var embedded = window.self !== window.top;
  if (embedded) r.classList.add('embedded');

  var p = new URLSearchParams(location.search);
  var qMode = p.get('citadel_mode');
  var qTheme = p.get('citadel_theme');
  var ls = localStorage;

  var m = (qMode === 'day' || qMode === 'night')
    ? qMode
    : (!embedded && ls.getItem(prefix + '-mode') === 'day' ? 'day' : 'night');
  var t = (qTheme === 'citadel' || qTheme === 'lcars' || qTheme === 'lcars25' || qTheme === 'borg')
    ? qTheme
    : (!embedded ? (ls.getItem(prefix + '-theme') || 'citadel') : 'citadel');
  if (t !== 'citadel' && t !== 'lcars' && t !== 'lcars25' && t !== 'borg') t = 'citadel';
  var f = t === 'lcars25' ? 'lcars' : t;

  r.dataset.mode = m;
  r.dataset.theme = t;
  r.dataset.family = f;
} catch (e) {}
