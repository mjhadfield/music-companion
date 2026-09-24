// Music DB Explorer -- a read-only SQL browser for data/music.sqlite. Vanilla JS, no build step, no
// dependencies (matches how the rest of this project is put together). Everything here is a convenience
// on top of the server: the server enforces read-only and the row/time limits regardless of what this does.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const CHEVRON = '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>';

const sql = $('#sql'), ac = $('#ac'), runBtn = $('#run'), csvBtn = $('#csv'), limitSel = $('#limit');
const status = $('#status'), errorEl = $('#error'), results = $('#results');

let schema = {};          // table name -> { columns, foreign_keys, row_count }
let lastResult = null;    // the most recent successful query's {columns, rows, ...}, for CSV export

// ---- maximise: the Query and Results cards can each expand to fill the workspace (query+results+recent's own
// area) without covering the Tables sidebar, which lives outside .workspace entirely. -----------------------
const EXPAND_D = 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5', SHRINK_D = 'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5';
const workspace = $('.workspace');

function setMax(card, on) {
  card.classList.toggle('is-max', on);
  workspace.classList.toggle('has-max', on);
  const btn = card.querySelector('[data-max]');
  btn.setAttribute('aria-expanded', String(on));
  btn.setAttribute('aria-label', on ? 'Restore' : 'Maximise');
  btn.title = on ? 'Restore' : 'Maximise';
  btn.querySelector('path').setAttribute('d', on ? SHRINK_D : EXPAND_D);
}

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-max]');
  if (btn) { setMax(btn.closest('.card'), !btn.closest('.card').classList.contains('is-max')); return; }
  if (workspace.classList.contains('has-max') && !e.target.closest('.card')) {
    setMax(workspace.querySelector('.card.is-max'), false);   // click on the dimmed backdrop
  }
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const max = workspace.querySelector('.card.is-max');
  if (max) setMax(max, false);
});

// ---- schema sidebar -----------------------------------------------------------------------------------------

function fmtBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${i === 0 ? n : n.toFixed(1)} ${u[i]}`;
}

function findTable(name) {
  const want = String(name || '').toLowerCase();
  return Object.keys(schema).find(t => t.toLowerCase() === want) || null;
}

function renderTables(filter = '') {
  const f = filter.trim().toLowerCase();
  const names = Object.keys(schema).sort();
  const blocks = names.map(t => {
    const info = schema[t];
    const tableMatches = !f || t.toLowerCase().includes(f);
    const cols = info.columns.filter(c => tableMatches || c.name.toLowerCase().includes(f));
    if (f && !cols.length) return '';
    const fkCols = new Set(info.foreign_keys.map(fk => fk.from));
    const colsHtml = cols.map(c => `<li class="col" data-table="${esc(t)}" data-col="${esc(c.name)}" title="${esc(c.type || '')}${c.notnull ? ' · not null' : ''}">
        <span class="col__name">${esc(c.name)}</span>
        ${c.pk ? '<span class="col__pk" title="Primary key">PK</span>' : ''}
        ${fkCols.has(c.name) ? '<span class="col__fk" title="Foreign key">FK</span>' : ''}
        <span class="col__type">${esc(c.type || '')}</span>
      </li>`).join('');
    return `<details class="table"${f ? ' open' : ''}>
      <summary class="table__head">${CHEVRON}<span class="table__name" data-table="${esc(t)}">${esc(t)}</span><span class="table__count">${info.row_count.toLocaleString()}</span></summary>
      <ul class="cols">${colsHtml}</ul>
    </details>`;
  }).join('');
  $('#tables').innerHTML = blocks || '<p class="empty">No matches.</p>';
}

function insertAtCursor(text) {
  const start = sql.selectionStart, end = sql.selectionEnd;
  sql.value = sql.value.slice(0, start) + text + sql.value.slice(end);
  const pos = start + text.length;
  sql.setSelectionRange(pos, pos);
  sql.focus();
}

$('#tables').addEventListener('click', e => {
  const name = e.target.closest('.table__name');
  if (name) {
    e.preventDefault();       // don't let the click also toggle the <details> open/closed
    e.stopPropagation();
    const table = name.dataset.table;
    if (!sql.value.trim()) {
      sql.value = `SELECT *\nFROM ${table}\nLIMIT 100`;
      sql.setSelectionRange(sql.value.length, sql.value.length);
      sql.focus();
    } else {
      insertAtCursor(table);
    }
    return;
  }
  const col = e.target.closest('.col');
  if (col) insertAtCursor(col.dataset.col);
});

$('#schema-filter').addEventListener('input', e => renderTables(e.target.value));

// ---- autocomplete: table/column/alias-aware ------------------------------------------------------------------

const KEYWORDS = ['SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'LIKE', 'IS', 'NULL', 'ORDER BY', 'GROUP BY',
  'HAVING', 'LIMIT', 'OFFSET', 'JOIN', 'LEFT JOIN', 'INNER JOIN', 'ON', 'AS', 'DISTINCT', 'COUNT', 'SUM', 'AVG',
  'MIN', 'MAX', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'ASC', 'DESC', 'UNION', 'UNION ALL', 'WITH', 'EXISTS',
  'COALESCE', 'CAST', 'STRFTIME', 'SUBSTR', 'LENGTH', 'ROUND', 'GROUP_CONCAT', 'BETWEEN'];
// so "FROM artists a" / "JOIN artists AS a" register "a" -> artists, and a plain "FROM artists" registers itself.
const ALIAS_RE = /\b(?:from|join)\s+([a-zA-Z_]\w*)(?:\s+(?:as\s+)?([a-zA-Z_]\w*))?/gi;
const NOT_AN_ALIAS = new Set(['on', 'where', 'group', 'order', 'having', 'limit', 'join', 'left', 'right', 'inner',
  'outer', 'cross', 'union', 'set', 'values', 'and', 'or', 'as']);

function aliasesIn(text) {
  const map = {};
  let m;
  ALIAS_RE.lastIndex = 0;
  while ((m = ALIAS_RE.exec(text))) {
    const table = findTable(m[1]);
    if (!table) continue;
    const alias = m[2] && !NOT_AN_ALIAS.has(m[2].toLowerCase()) ? m[2] : table;
    map[alias.toLowerCase()] = table;
  }
  return map;
}

/** The identifier at/just before `pos`: {start, prefix, partial} — "a.na|me" at the cursor -> prefix "a", partial "na". */
function currentToken(text, pos) {
  let start = pos;
  while (start > 0 && /[A-Za-z0-9_.]/.test(text[start - 1])) start--;
  const token = text.slice(start, pos);
  const dot = token.lastIndexOf('.');
  return dot === -1 ? { start, prefix: '', partial: token } : { start: start + dot + 1, prefix: token.slice(0, dot), partial: token.slice(dot + 1) };
}

function suggestionsAt(text, pos) {
  const { prefix, partial } = currentToken(text, pos);
  const p = partial.toLowerCase();
  if (prefix) {
    const table = aliasesIn(text)[prefix.toLowerCase()] || findTable(prefix);
    if (!table) return [];
    return (schema[table].columns || [])
      .filter(c => c.name.toLowerCase().startsWith(p))
      .map(c => ({ kind: 'col', text: c.name, hint: c.pk ? 'PK' : (c.type || '') }));
  }
  const out = [];
  for (const t of Object.keys(schema)) {
    if (t.toLowerCase().startsWith(p)) out.push({ kind: 'tbl', text: t, hint: `${schema[t].row_count.toLocaleString()} rows` });
  }
  const aliases = aliasesIn(text);
  for (const [alias, table] of Object.entries(aliases)) {
    if (alias.startsWith(p) && alias !== table.toLowerCase()) out.push({ kind: 'als', text: alias, hint: table });
  }
  if (p.length >= 2) {
    for (const k of KEYWORDS) if (k.toLowerCase().startsWith(p)) out.push({ kind: 'kw', text: k, hint: '' });
  }
  return out.slice(0, 40);
}

const KIND_LABEL = { tbl: 'T', col: 'C', als: 'A', kw: 'K' };
let acItems = [], acActive = 0;

function closeAc() { ac.hidden = true; acItems = []; }

function renderAc(items) {
  acItems = items; acActive = 0;
  if (!items.length) return closeAc();
  ac.innerHTML = items.map((it, i) => `<div class="ac__item${i === 0 ? ' is-active' : ''}" data-i="${i}">
      <span class="ac__kind">${KIND_LABEL[it.kind]}</span>${esc(it.text)}<small>${esc(it.hint)}</small></div>`).join('');
  positionAc();
  ac.hidden = false;
}

function markActive() {
  [...ac.children].forEach((el, i) => el.classList.toggle('is-active', i === acActive));
  ac.children[acActive]?.scrollIntoView({ block: 'nearest' });
}

function acceptSuggestion(item) {
  const { start } = currentToken(sql.value, sql.selectionStart);
  const end = sql.selectionStart;
  const insert = item.text + (item.kind === 'tbl' || item.kind === 'als' ? '' : ' ');
  sql.value = sql.value.slice(0, start) + insert + sql.value.slice(end);
  const pos = start + insert.length;
  sql.setSelectionRange(pos, pos);
  closeAc();
}

// ---- caret pixel position, for the dropdown -- mirrors the textarea's text in a hidden div and measures
// where a marker placed at the cursor lands. Standard technique for "where is the caret" in a plain <textarea>.
const MIRRORED_STYLES = ['boxSizing', 'width', 'fontFamily', 'fontSize', 'fontWeight', 'letterSpacing', 'lineHeight',
  'padding', 'border', 'whiteSpace', 'wordWrap', 'textIndent'];

function caretOffset(el, pos) {
  const div = document.createElement('div');
  const cs = getComputedStyle(el);
  for (const p of MIRRORED_STYLES) div.style[p] = cs[p];
  Object.assign(div.style, { position: 'absolute', visibility: 'hidden', top: '0', left: '-9999px', whiteSpace: 'pre-wrap', wordWrap: 'break-word', height: 'auto' });
  document.body.appendChild(div);
  div.textContent = el.value.slice(0, pos);
  const marker = document.createElement('span');
  marker.textContent = '​';
  div.appendChild(marker);
  const offset = { top: marker.offsetTop, left: marker.offsetLeft, lineHeight: parseFloat(cs.lineHeight) || 18 };
  document.body.removeChild(div);
  return offset;
}

function positionAc() {
  const { top, left, lineHeight } = caretOffset(sql, sql.selectionStart);
  const box = sql.getBoundingClientRect(), wrapBox = sql.parentElement.getBoundingClientRect();
  ac.style.left = `${box.left - wrapBox.left + left - sql.scrollLeft}px`;
  ac.style.top = `${box.top - wrapBox.top + top + lineHeight - sql.scrollTop}px`;
}

let suppressAc = false;   // set for exactly the one 'input' event a newline itself fires, so dismissing with Enter doesn't just show a fresh list on the blank line it lands on

function scheduleAc() {
  if (suppressAc) { suppressAc = false; return; }
  if (Object.keys(schema).length === 0) return;
  renderAc(suggestionsAt(sql.value, sql.selectionStart));
}

sql.addEventListener('input', scheduleAc);
sql.addEventListener('click', closeAc);
sql.addEventListener('blur', () => setTimeout(closeAc, 150));   // delayed so a click on a suggestion still registers

sql.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); closeAc(); run(); return; }
  if (ac.hidden) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); acActive = (acActive + 1) % acItems.length; markActive(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); acActive = (acActive - 1 + acItems.length) % acItems.length; markActive(); }
  else if (e.key === 'Tab') { e.preventDefault(); acceptSuggestion(acItems[acActive]); }
  else if (e.key === 'Enter') { closeAc(); suppressAc = true; }   // ignore the suggestion and start a new line, same as if it weren't showing
  else if (e.key === 'Escape') { e.preventDefault(); closeAc(); }
});

ac.addEventListener('click', e => {
  const item = e.target.closest('.ac__item');
  if (item) acceptSuggestion(acItems[Number(item.dataset.i)]);
});

// ---- running a query ------------------------------------------------------------------------------------------

/** Back to the just-opened look: no results, no error, no status, CSV disabled. Shared by the preset buttons
 *  (a fresh preset invalidates whatever was on screen before it) and by Clear, which also empties the editor. */
function resetResults() {
  results.innerHTML = '<div class="empty">Run a query to see results here.</div>';
  errorEl.hidden = true; errorEl.textContent = '';
  status.textContent = ''; lastResult = null; csvBtn.disabled = true;
}

function renderResults(data) {
  if (!data.rows.length) {
    results.innerHTML = '<div class="empty">No rows.</div>';
    lastResult = null; csvBtn.disabled = true;
    return;
  }
  lastResult = data; csvBtn.disabled = false;
  const numeric = data.columns.map((_, i) => data.rows.every(r => r[i] === null || typeof r[i] === 'number'));
  const head = data.columns.map(c => `<th>${esc(c)}</th>`).join('');
  const body = data.rows.map(row => '<tr>' + row.map((v, i) => {
    if (v === null) return '<td class="null">NULL</td>';
    const text = String(v);
    return `<td${numeric[i] ? ' class="num"' : ''} title="${esc(text)}">${esc(text)}</td>`;
  }).join('') + '</tr>').join('');
  const note = data.truncated
    ? `<div class="results__note">Showing the first ${data.rows.length.toLocaleString()} rows — the query returned more. Narrow it, or raise the row limit above.</div>` : '';
  results.innerHTML = `${note}<div class="tablewrap"><table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

async function run() {
  const q = sql.value;
  runBtn.disabled = true; runBtn.textContent = 'Running…';
  try {
    const res = await fetch('/api/query', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: q, limit: Number(limitSel.value) || 500 }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.hidden = false; errorEl.textContent = data.error || `Request failed (HTTP ${res.status})`;
      results.innerHTML = ''; lastResult = null; csvBtn.disabled = true; status.textContent = '';
      return;
    }
    errorEl.hidden = true;
    renderResults(data);
    status.innerHTML = `<b>${data.row_count.toLocaleString()}</b> row${data.row_count === 1 ? '' : 's'} · ${data.elapsed_ms} ms${data.truncated ? ' · truncated' : ''}`;
    remember(q);
  } catch (err) {
    errorEl.hidden = false; errorEl.textContent = `Couldn't reach the server: ${err.message}`;
  } finally {
    runBtn.disabled = false; runBtn.textContent = 'Run';
  }
}

runBtn.addEventListener('click', run);

$('#clear').addEventListener('click', () => {
  sql.value = '';
  closeAc();
  resetResults();
  sql.focus();
});

csvBtn.addEventListener('click', () => {
  if (!lastResult) return;
  const cell = v => (v === null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const lines = [lastResult.columns.map(cell).join(','), ...lastResult.rows.map(r => r.map(cell).join(','))];
  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `music-db-query-${Date.now()}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

// ---- example queries ------------------------------------------------------------------------------------------

const PRESETS = [
  { label: 'Top artists', sql: 'SELECT a.name, COUNT(*) AS plays\nFROM scrobbles s\nJOIN artists a ON a.id = s.artist_id\nGROUP BY a.id\nORDER BY plays DESC\nLIMIT 20' },
  { label: 'Recent gigs', sql: 'SELECT s.event_date, a.name AS artist, v.name AS venue, v.city\nFROM setlists s\nJOIN artists a ON a.id = s.artist_id\nLEFT JOIN venues v ON v.id = s.venue_id\nORDER BY s.event_date DESC\nLIMIT 20' },
  { label: 'Vinyl by label', sql: 'SELECT label, COUNT(*) AS records\nFROM vinyl_holdings\nGROUP BY label\nORDER BY records DESC\nLIMIT 20' },
  { label: 'Albums missing an MBID', sql: 'SELECT a.name AS artist, al.title, al.year\nFROM albums al\nJOIN artists a ON a.id = al.artist_id\nWHERE al.mbid IS NULL\nORDER BY al.year DESC\nLIMIT 20' },
  { label: 'Scrobbles per year', sql: "SELECT strftime('%Y', played_at) AS year, COUNT(*) AS plays\nFROM scrobbles\nGROUP BY year\nORDER BY year" },
];

$('#presets').innerHTML = PRESETS.map((p, i) => `<button class="preset" type="button" data-i="${i}">${esc(p.label)}</button>`).join('');
$('#presets').addEventListener('click', e => {
  const b = e.target.closest('.preset');
  if (!b) return;
  sql.value = PRESETS[Number(b.dataset.i)].sql;
  resetResults();
  sql.focus();
});

// ---- recent queries (this browser only -- a convenience, not shared or sent anywhere) -------------------------

const RECENT_KEY = 'musicdb-explore-recent';

function loadRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch { return []; }
}

function renderRecent(list) {
  const card = $('#recent-card');
  if (!list.length) { card.hidden = true; return; }
  card.hidden = false;
  $('#recent').innerHTML = list.map((q, i) => `<li data-i="${i}" title="${esc(q)}">${esc(q.replace(/\s+/g, ' ').slice(0, 90))}</li>`).join('');
}

function remember(q) {
  const trimmed = q.trim();
  if (!trimmed) return;
  try {
    const list = [trimmed, ...loadRecent().filter(x => x !== trimmed)].slice(0, 20);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    renderRecent(list);
  } catch { /* private browsing / storage blocked: recent history just won't persist */ }
}

$('#recent').addEventListener('click', e => {
  const li = e.target.closest('li');
  if (!li) return;
  const q = loadRecent()[Number(li.dataset.i)];
  if (q != null) { sql.value = q; sql.focus(); }
});

// Collapsed by default -- clicking the header expands/collapses it, same disclosure pattern as a schema table.
const recentToggle = $('#recent-toggle'), recentBody = $('#recent-body');
recentToggle.addEventListener('click', () => {
  const open = recentToggle.getAttribute('aria-expanded') === 'true';
  recentToggle.setAttribute('aria-expanded', String(!open));
  recentBody.hidden = open;
});

// ---- boot -------------------------------------------------------------------------------------------------------

renderRecent(loadRecent());
(async () => {
  try {
    const res = await fetch('/api/schema');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'failed to load the schema');
    schema = data.tables;
    $('#table-count').textContent = `${Object.keys(schema).length} tables`;
    $('#db-size').textContent = fmtBytes(data.db_bytes);
    renderTables();
  } catch (err) {
    $('#tables').innerHTML = `<p class="empty">Couldn't load the schema: ${esc(err.message)}</p>`;
  }
})();
