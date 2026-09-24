/*
 * Shared helpers + small UI components for every maintenance page.
 * Plain <script src> include, no module system -- matches the rest of the project's
 * no-build-step convention. Components are plain functions that take elements and callbacks.
 */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmtNum = (n) => (n ?? 0).toLocaleString();
const fmtDate = (iso) => (iso ? String(iso).slice(0, 10) : "—");
const plural = (n, word, pl) => `${fmtNum(n)} ${n === 1 ? word : (pl || word + "s")}`;

// -- API ------------------------------------------------------------------------------------

// Always resolves (never throws on HTTP errors): { ok, status, data }. `data.message` is the
// human-readable error text whenever !ok.
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", body: JSON.stringify(body) };
  try {
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok && !data.message) data.message = data.error || `HTTP ${res.status}`;
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { message: `Network error: ${err.message}` } };
  }
}

// Tells every live component (activity feeds, nav publish dot, tab counts) that data changed.
function notifyChanged() { document.dispatchEvent(new CustomEvent("mc:changed")); }

// -- Links: every MBID shown anywhere is a link to its MusicBrainz page ---------------------

const MB_KINDS = { artist: "artist", album: "release-group", "release-group": "release-group", release: "release", song: "recording", recording: "recording" };

function mbUrl(kind, mbid) { return `https://musicbrainz.org/${MB_KINDS[kind] || kind}/${encodeURIComponent(mbid)}`; }

function mbLink(kind, mbid, text) {
  if (!mbid) return `<span class="badge warn">no mbid</span>`;
  return `<a class="mb" href="${mbUrl(kind, mbid)}" target="_blank" rel="noopener" title="${esc(mbid)}">${esc(text || mbid.slice(0, 8))} ↗</a>`;
}

function discogsLink(releaseId, text) {
  return releaseId ? `<a class="mb" href="https://www.discogs.com/release/${encodeURIComponent(releaseId)}" target="_blank" rel="noopener">${esc(text || "discogs " + releaseId)} ↗</a>` : "";
}

// -- Job log (refresh / publish / sweeps) ---------------------------------------------------

// Each entry in `lines` is already one complete line -- styled by a light heuristic on its text.
function appendLines(logEl, lines) {
  for (const rawLine of lines) {
    const line = rawLine.replace(/\n$/, "");
    const div = document.createElement("div");
    div.className = "log-line";
    if (/^(={5,}|What's new|Scrobbles:|Setlists:|Vinyl holdings:|New artists)/.test(line.trim())) div.className += " header";
    else if (line.includes("⚠") || line.trim().startsWith("!")) div.className += " warn";
    else if (line.trim() === "") div.className += " dim";
    div.textContent = line;
    logEl.appendChild(div);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

// Polls /status/<jobId> until done, appending new lines to logEl; onProgress([done,total]) for sweeps.
async function pollJob(jobId, logEl, onDone, onProgress) {
  let since = 0;
  while (true) {
    const res = await fetch(`/status/${jobId}?since=${since}`);
    const data = await res.json();
    if (data.lines && data.lines.length) {
      appendLines(logEl, data.lines);
      since = data.total;
    }
    if (onProgress && data.progress) onProgress(data.progress);
    if (data.done) { onDone(data); return; }
    await new Promise((r) => setTimeout(r, 500));
  }
}

// A capped, cancellable background sweep with a progress bar and collapsible log.
// `box` gets filled with the UI; resolves when the sweep ends.
function runSweep(box, kind, limit, extra = {}) {
  return new Promise(async (resolve) => {
    box.innerHTML = `
      <div class="job-status"><span class="spinner"></span> <span data-role="text">Starting…</span>
        <progress value="0" max="1"></progress><button class="small" data-role="cancel">Cancel</button></div>
      <details><summary class="meta">Log</summary><div class="log" data-role="log"></div></details>`;
    const text = box.querySelector("[data-role='text']");
    const bar = box.querySelector("progress");
    const cancel = box.querySelector("[data-role='cancel']");
    const { ok, data } = await api("/api/sweeps/start", { kind, limit, ...extra });
    if (!ok) { box.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; resolve(false); return; }
    cancel.addEventListener("click", () => { cancel.disabled = true; api("/api/sweeps/cancel", { jobId: data.jobId }); });
    pollJob(data.jobId, box.querySelector("[data-role='log']"), (result) => {
      box.querySelector(".spinner").remove();
      cancel.remove();
      text.textContent = result.returncode === 0 ? "Done" : "Failed — see log";
      notifyChanged();
      resolve(result.returncode === 0);
    }, ([done, total]) => {
      bar.max = Math.max(total, 1); bar.value = done;
      text.textContent = `Working… ${done}/${total}`;
    });
  });
}

// -- Toasts + undo --------------------------------------------------------------------------

function toast(message, { undo, error, timeout } = {}) {
  let host = document.getElementById("toasts");
  if (!host) { host = document.createElement("div"); host.id = "toasts"; document.body.appendChild(host); }
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span class="msg">${message}</span>${undo ? `<button class="small" data-role="undo">Undo</button>` : ""}<button class="small" data-role="close">✕</button>`;
  el.querySelector("[data-role='close']").addEventListener("click", () => el.remove());
  if (undo) {
    el.querySelector("[data-role='undo']").addEventListener("click", async (e) => {
      e.target.disabled = true;
      const ok = await undoAction(undo.kind, undo.id);
      if (ok) { el.remove(); undo.onUndone?.(); }
      else e.target.disabled = false;
    });
  }
  host.appendChild(el);
  setTimeout(() => el.remove(), timeout || (undo ? 12000 : 5000));
  return el;
}

// `id` may be an array of merge log ids (a group merge) -- undone together, newest first.
async function undoAction(kind, id) {
  // kind "batch": id is [{kind, id}, ...] -- a mixed reviewed batch (merges + edits + marks)
  const body = kind === "batch" ? { kind, items: id } : Array.isArray(id) ? { kind, ids: id } : { kind, id };
  const { ok, data } = await api("/api/history/undo", body);
  if (!ok) { toast(esc(data.message), { error: true, timeout: 9000 }); return false; }
  // Pages listen for this and reload what they're showing -- an undo from a toast must never
  // leave the view displaying the pre-undo state.
  document.dispatchEvent(new CustomEvent("mc:undone"));
  toast(kind === "merge" ? `Undone — restored <b>${esc(data.restoredName)}</b>` : kind === "batch" ? `Undone — ${esc(data.name)} reverted` : `Undone — <b>${esc(data.name)}</b> restored`);
  notifyChanged();
  return true;
}

// "<artist> also releases as <MusicBrainz artist>": that credit then counts as the local artist in
// every check, search and lookup (e.g. The Jimi Hendrix Experience -> Jimi Hendrix). `name` may be
// omitted (the server looks it up). Returns true on success.
async function addMbAlias(artistId, mbid, name) {
  const { ok, data } = await api("/api/artists/mb-alias", { artistId, mbid, name });
  if (!ok) { toast(esc(data.message), { error: true, timeout: 9000 }); return false; }
  toast(`<b>${esc(data.name)}</b> now counts as <b>${esc(data.artistName)}</b>`, { undo: { kind: "mbalias", id: data.id } });
  notifyChanged();
  return true;
}
// A button that adds every mbid of a MusicBrainz credit as an alias of the local artist.
function creditAliasButton(credit) {
  return `<button class="small good" data-credit-alias='${esc(JSON.stringify(credit))}' title="Treat this MusicBrainz credit as ${esc(credit.artistName)} everywhere (checks, searches, lookups, imports)">Count “${esc(credit.name)}” as ${esc(credit.artistName)}</button>`;
}
// Wires any creditAliasButton inside `root`; onDone() after the aliases are added.
function wireCreditAliasButtons(root, onDone) {
  root.querySelectorAll("[data-credit-alias]").forEach((b) => b.addEventListener("click", async () => {
    const c = JSON.parse(b.dataset.creditAlias);
    b.disabled = true;
    let ok = true;
    for (const mbid of c.mbids) ok = (await addMbAlias(c.artistId, mbid, c.mbids.length === 1 ? c.name : undefined)) && ok;
    if (ok) onDone?.(); else b.disabled = false;
  }));
}

// Human summary of a merge's rowsMoved, e.g. "812 scrobbles, 4 songs".
function movedSummary(rowsMoved) {
  return Object.entries(rowsMoved || {})
    .filter(([k, v]) => v > 0 && !k.includes("collisions") && !k.includes("alias"))
    .map(([k, v]) => `${fmtNum(v)} ${k.replace(/_/g, " ")}`).join(", ");
}

// -- Activity feed (merge_log + edit_log, with undo) ----------------------------------------

function mountActivity(el, { entityType, limit = 25 } = {}) {
  async function load() {
    const params = new URLSearchParams({ limit });
    if (entityType) params.set("entityType", entityType);
    const { ok, data } = await api(`/api/history?${params}`);
    if (!ok) { el.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
    if (!data.items.length) { el.innerHTML = `<div class="empty">Nothing yet.</div>`; return; }
    el.innerHTML = `<ul class="activity">${data.items.map((i) => {
      let what;
      if (i.kind === "merge") {
        what = `Merged ${i.entityType} <span class="who">${esc(i.absorbedName)}</span> into <span class="who">${esc(i.canonicalName)}</span>`
          + (movedSummary(i.rowsMoved) ? ` <span class="meta">— ${esc(movedSummary(i.rowsMoved))}</span>` : "");
      } else if (i.changes._genres) {
        const d = i.changes._genres;
        const n = (k) => (d[k] || []).length;
        what = `Genres on <span class="who">${esc(i.name)}</span>: ${[n("inserted") + n("updated") && `${n("inserted") + n("updated")} added`, n("deleted") && `${n("deleted")} removed`].filter(Boolean).join(", ") || "updated"}`;
      } else if (i.changes._genreRule) {
        const d = i.changes._genreRule;
        what = d.action === "hide" ? `Hid genre <span class="who">${esc(i.name)}</span> everywhere`
          : d.action === "merge" ? `Merged genre <span class="who">${esc(i.name)}</span> into another`
          : `Restored genre <span class="who">${esc(i.name)}</span>`;
      } else if (i.changes._created) {
        what = `Added album <span class="who">${esc(i.name)}</span> from MusicBrainz ${i.changes._created.mbid ? mbLink("album", i.changes._created.mbid) : ""}`;
      } else if (i.changes._split) {
        const d = i.changes._split;
        what = `Split <span class="who">${esc(i.name)}</span> back out of <span class="who">${esc(d.fromTitle)}</span> <span class="meta">— ${plural(d.scrobbleIds.length, "play")}, ${plural(d.songIds.length, "song")}${d.vinylIds.length ? ", " + plural(d.vinylIds.length, "pressing") : ""}</span>`;
      } else if (i.changes._relink) {
        const r = i.changes._relink;
        what = `Re-linked ${plural(r.setlistSongIds.length, "live play")} of <span class="who">${esc(i.name)}</span> to <span class="who">${esc(r.toTitle || "#" + r.to)}</span>${r.createdSong ? " <span class='meta'>(new song)</span>" : ""}`;
      } else {
        const parts = Object.entries(i.changes).filter(([k]) => !k.startsWith("cover_")).map(([k, [o, n]]) =>
          k === "mbid" ? `mbid ${o ? mbLink(i.entityType, o) : "∅"} → ${n ? mbLink(i.entityType, n) : "∅"}`
          : k === "mb_release_id" ? `pressing ${o ? mbLink("release", o) : "∅"} → ${n ? mbLink("release", n) : "∅"}`
          : k === "album_id" ? `moved to another album (#${esc(o)} → #${esc(n)})`
          : `${esc(k)} “${esc(o ?? "∅")}” → “${esc(n ?? "∅")}”`);
        what = `Edited ${i.entityType} <span class="who">${esc(i.name)}</span>: ${parts.join("; ") || "cover reset"}`;
      }
      const btn = i.undoable ? `<button class="small" data-kind="${i.kind}" data-id="${i.id}">Undo</button>` : (i.undoneAt ? `<span class="badge">undone</span>` : "");
      return `<li class="${i.undoneAt ? "undone" : ""}"><time>${esc(i.at.slice(5, 16).replace("T", " "))}</time><span class="what">${what}</span>${btn}</li>`;
    }).join("")}</ul>`;
    el.querySelectorAll("button[data-kind]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      if (!(await undoAction(b.dataset.kind, parseInt(b.dataset.id, 10)))) b.disabled = false;
    }));
  }
  document.addEventListener("mc:changed", load);
  load();
  return { reload: load };
}

// -- Top nav + publish ----------------------------------------------------------------------

const COMPANION_URL = "http://192.168.0.66:8642/";

function mountNav(active) {
  const header = document.getElementById("topnav");
  const links = [["/", "Home"], ["/artists.html", "Artists"], ["/vinyl.html", "Vinyl"], ["/albums.html", "Albums"], ["/songs.html", "Songs"], ["/genres.html", "Genres"], ["/inbox.html", "Inbox"]];
  header.className = "topnav";
  header.innerHTML = `
    <a class="brand" href="/">Music <b>Maintenance</b></a>
    <nav>${links.map(([href, label, soon]) =>
      `<a href="${href}" ${label === active ? 'aria-current="page"' : ""} ${soon ? 'class="soon" title="Coming in a later phase"' : ""}>${label}</a>`).join("")}</nav>
    <span class="spacer"></span>
    <button class="publish-btn small" id="publish-btn" title="Rebuild the public database the Companion site serves"><span class="dot"></span> Publish</button>
    <a class="companion" href="${COMPANION_URL}">Music Companion ↗</a>`;
  const btn = header.querySelector("#publish-btn");

  async function refreshStatus() {
    const { ok, data } = await api("/api/publish-status");
    if (!ok) return;
    btn.classList.toggle("dirty", data.unpublished);
    btn.title = data.disabled ? data.reason : (data.unpublished ? "Unpublished changes — click to rebuild the public database" : "Public database is up to date");
    btn.disabled = data.disabled;
    if (data.disabled && !document.querySelector(".test-banner")) {
      header.insertAdjacentHTML("beforebegin", `<div class="test-banner">TEST INSTANCE — scratch database, publishing disabled</div>`);
    }
  }
  btn.addEventListener("click", async () => {
    let drawer = document.getElementById("publish-drawer");
    if (!drawer) { drawer = document.createElement("div"); drawer.id = "publish-drawer"; document.body.appendChild(drawer); }
    drawer.innerHTML = `<div class="job-status"><span class="spinner"></span> <span data-role="text">Rebuilding public database…</span><span class="spacer"></span><button class="small" data-role="close">✕</button></div><div class="log"></div>`;
    drawer.querySelector("[data-role='close']").addEventListener("click", () => drawer.remove());
    btn.disabled = true;
    const { ok, data } = await api("/build", {});
    if (!ok) { drawer.querySelector("[data-role='text']").textContent = data.message; drawer.querySelector(".spinner").remove(); btn.disabled = false; return; }
    pollJob(data.jobId, drawer.querySelector(".log"), (result) => {
      drawer.querySelector(".spinner")?.remove();
      drawer.querySelector("[data-role='text']").textContent = result.returncode === 0 ? "Published" : `Exited with code ${result.returncode}`;
      btn.disabled = false;
      refreshStatus();
    });
  });
  document.addEventListener("mc:changed", refreshStatus);
  refreshStatus();
}

// -- Tabs (hash-routed: page.html#duplicates deep-links) -------------------------------------

// tabs: {name: onFirstShow()} -- each panel's loader runs once, the first time it's shown.
// A hash like #missing/23 shows the "missing" tab and then calls onSubpath("missing", "23") --
// deep links into a tab (e.g. a specific artist), whether arriving fresh or on a same-page hash change.
function mountTabs(tabsEl, handlers, { onSubpath } = {}) {
  const buttons = [...tabsEl.querySelectorAll("button[data-tab]")];
  const shown = new Set();
  function show(hash, push) {
    let [name, sub] = String(hash || "").split("/");
    if (!buttons.some((b) => b.dataset.tab === name)) { name = buttons[0].dataset.tab; sub = undefined; }
    for (const b of buttons) b.setAttribute("aria-selected", String(b.dataset.tab === name));
    document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    if (push) history.replaceState(null, "", `#${name}`);
    if (!shown.has(name)) { shown.add(name); if (!sub) handlers[name]?.(); }
    if (sub) onSubpath?.(name, sub);
    document.dispatchEvent(new CustomEvent("mc:tab", { detail: name }));
  }
  buttons.forEach((b) => b.addEventListener("click", () => show(b.dataset.tab, true)));
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  show(location.hash.slice(1));
  return {
    show: (name) => show(name, true),
    setCount(name, n) {
      const b = buttons.find((x) => x.dataset.tab === name);
      if (!b) return;
      let c = b.querySelector(".count");
      if (!c) { c = document.createElement("span"); c.className = "count"; b.appendChild(c); }
      c.textContent = n == null ? "" : fmtNum(n);
    },
    current: () => buttons.find((b) => b.getAttribute("aria-selected") === "true")?.dataset.tab,
  };
}

// -- Typeahead ------------------------------------------------------------------------------
// Results render in a top-layer popover, positioned from the input's own rect -- so no
// ancestor's clip-path/overflow can ever cut them off (the bug the old in-card dropdowns had).
// fetchItems(q) -> [{...}]; renderItem(item) -> html; onPick(item). Arrow keys + Enter work.

function mountTypeahead(input, { fetchItems, renderItem, onPick, minChars = 1, debounce = 200 }) {
  const pop = document.createElement("div");
  pop.className = "ta-pop";
  pop.setAttribute("popover", "manual");
  document.body.appendChild(pop);
  let items = [], hl = -1, timer = null, seq = 0;

  function place() {
    const r = input.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 12, above = r.top - 12;
    // Open upwards when the input is near the bottom of the window and there's more room above.
    const up = below < 220 && above > below;
    const room = Math.max(120, Math.min(320, up ? above : below));
    const width = Math.max(r.width, 320);
    pop.style.maxHeight = `${room}px`;
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - width - 8))}px`;
    pop.style.top = up ? "auto" : `${r.bottom + 4}px`;
    pop.style.bottom = up ? `${window.innerHeight - r.top + 4}px` : "auto";
  }
  const isOpen = () => pop.matches(":popover-open");
  function open() { place(); if (!isOpen()) pop.showPopover(); }
  function close() { if (isOpen()) pop.hidePopover(); hl = -1; }
  function paint() {
    pop.innerHTML = items.length
      ? items.map((it, i) => `<div class="ta-row${i === hl ? " hl" : ""}" data-i="${i}">${renderItem(it)}</div>`).join("")
      : `<div class="ta-empty">No matches.</div>`;
  }
  function pick(i) {
    const it = items[i];
    if (!it) return;
    close();
    input.value = "";
    onPick(it);
  }
  pop.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the input
  pop.addEventListener("click", (e) => {
    if (e.target.closest("a")) return; // let MB links inside a row open normally
    const row = e.target.closest(".ta-row");
    if (row) pick(parseInt(row.dataset.i, 10));
  });
  input.setAttribute("autocomplete", "off");
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < minChars) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      const result = await fetchItems(q);
      if (mine !== seq) return; // a newer keystroke's results win
      items = result || []; hl = items.length ? 0 : -1;
      paint(); open();
    }, debounce);
  });
  input.addEventListener("keydown", (e) => {
    if (!isOpen()) return;
    if (e.key === "ArrowDown") { hl = Math.min(items.length - 1, hl + 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { hl = Math.max(0, hl - 1); paint(); e.preventDefault(); }
    else if (e.key === "Enter") { pick(hl); e.preventDefault(); }
    else if (e.key === "Escape") { close(); e.preventDefault(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  window.addEventListener("scroll", () => isOpen() && place(), true);
  window.addEventListener("resize", () => isOpen() && place());
  return { close, destroy: () => pop.remove() };
}

// -- Ticked bulk list -----------------------------------------------------------------------
// A table of rows with a checkbox each, a header box that mirrors them (all / none / some), and
// bulk buttons acting on the ticked ids. rows: [{id, ticked, cells: [html]}]; buttons: [[act,
// label, cls]]; onAction(act, ids) (awaited). Used by Albums > Editions and Songs > Recording ids.
function mountTickList(el, rows, { head, cols, buttons, onAction }) {
  el.innerHTML = `<div class="card flush" style="margin-top:18px">
    <div class="inbox-tools ed-tools"><h3 style="margin:0">${head}</h3><span class="spacer"></span>
      <label class="meta"><input type="checkbox" data-role="all" /> <span data-role="ticked"></span></label>
      ${buttons.map(([role, label, cls]) => `<button class="small ${cls || ""}" data-act="${role}">${label}</button>`).join("")}</div>
    <table class="ed-table"><thead><tr><th></th>${cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr><td><input type="checkbox" data-id="${r.id}" ${r.ticked ? "checked" : ""} /></td>${r.cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  const all = el.querySelector("[data-role='all']");
  const boxes = () => [...el.querySelectorAll("tbody input[type=checkbox]")];
  const sync = () => {
    const n = boxes().filter((b) => b.checked).length;
    all.checked = n > 0 && n === boxes().length; all.indeterminate = n > 0 && n < boxes().length;
    el.querySelector("[data-role='ticked']").textContent = `${fmtNum(n)} ticked`;
    el.querySelectorAll("[data-act]").forEach((b) => { b.disabled = n === 0; });
  };
  all.addEventListener("change", () => { boxes().forEach((b) => { b.checked = all.checked; }); sync(); });
  el.onchange = (e) => { if (e.target.dataset.id) sync(); }; // one slot: the list re-renders into the same element
  el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    await onAction(b.dataset.act, boxes().filter((x) => x.checked).map((x) => +x.dataset.id));
    b.disabled = false;
  }));
  sync();
}

// -- Genre chips --------------------------------------------------------------------------
// An album's genres, editable: × removes (remembered -- a refresh won't bring it back), "+ genre"
// adds one by hand (typeahead over known genres, or a new name). Every change undoable.
// genres: [{genreId, name, source, votes}]. onChange() after an edit or its undo.
const GENRE_SOURCE = { musicbrainz: "MusicBrainz", discogs: "Discogs style", manual: "added by you" };
function mountGenreChips(el, albumId, genres, { onChange } = {}) {
  el.classList.add("genre-chips");
  el.innerHTML = `${genres.map((g) => `<span class="gchip ${g.source}" title="${esc(GENRE_SOURCE[g.source] || g.source)}${g.votes ? ` · ${g.votes} vote${g.votes === 1 ? "" : "s"}` : ""}">${esc(g.name)}<button data-remove="${g.genreId}" title="Remove from this album">×</button></span>`).join("")}
    <span class="gadd"><input type="text" placeholder="+ genre" aria-label="Add a genre" /></span>`;
  async function send(body, message) {
    const { ok, data } = await api("/api/genres/edit", { albumId, ...body });
    if (!ok) { toast(esc(data.message), { error: true }); return; }
    if (data.undo) toast(message, { undo: { ...data.undo, onUndone: onChange } });
    notifyChanged();
    onChange?.();
  }
  el.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => {
    const g = genres.find((x) => x.genreId === +b.dataset.remove);
    send({ remove: [g.genreId] }, `Removed “${esc(g.name)}” — it won't come back on a refresh`);
  }));
  const input = el.querySelector(".gadd input");
  mountTypeahead(input, {
    fetchItems: async (q) => {
      const found = (await api(`/api/genres/search?q=${encodeURIComponent(q)}`)).data.genres || [];
      const exact = found.some((g) => g.name === q.trim().toLowerCase());
      return [...found, ...(exact || !q.trim() ? [] : [{ genreId: null, name: q.trim().toLowerCase(), albums: 0, isNew: true }])];
    },
    renderItem: (g) => `<span>${esc(g.name)}</span><span class="meta">${g.isNew ? "new genre" : plural(g.albums, "album")}</span>`,
    onPick: (g) => send({ add: [g.name] }, `Added “${esc(g.name)}”`),
  });
}

// -- Keyboard review list -------------------------------------------------------------------
// Any element with .review-item inside `container` is navigable with j/k; pressing a key
// clicks the active item's button carrying data-key="<that key>". Buttons show their key.

function mountReviewKeys(container, { onActivate } = {}) {
  let active = null;
  function items() { return [...container.querySelectorAll(".review-item:not(.done)")].filter((el) => el.offsetParent !== null); }
  function setActive(el, scroll = true) {
    if (active) active.classList.remove("active");
    active = el || null;
    if (active) {
      active.classList.remove("skipped"); // coming back to a skipped item makes it live again
      active.classList.add("active");
      if (scroll) active.scrollIntoView({ block: "nearest", behavior: "smooth" });
      onActivate?.(active);
    }
  }
  // Clicking an item's empty space selects it. Clicks on its own buttons/links/inputs don't --
  // they bubble here AFTER the button's handler has run, and re-selecting would undo whatever
  // the button just did to the selection (it's what made "Skip" appear to do nothing).
  container.addEventListener("click", (e) => {
    if (e.target.closest("button, a, input, select, textarea, label")) return;
    const item = e.target.closest(".review-item");
    if (item && item !== active && !item.classList.contains("done")) setActive(item, false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (container.offsetParent === null) return; // its tab isn't showing
    const list = items();
    if (!list.length) return;
    if (!active || !active.isConnected || active.classList.contains("done")) {
      setActive(list[0]);
      if (e.key === "j" || e.key === "k") { e.preventDefault(); return; } // first press just selects
    }
    const idx = list.indexOf(active);
    if (e.key === "j") { setActive(list[Math.min(list.length - 1, idx + 1)]); e.preventDefault(); return; }
    if (e.key === "k") { setActive(list[Math.max(0, idx - 1)]); e.preventDefault(); return; }
    const btn = active.querySelector(`[data-key="${CSS.escape(e.key)}"]:not(:disabled)`);
    if (btn) { btn.click(); e.preventDefault(); }
  });
  return {
    // After an item is resolved: mark it done and move to the next one.
    advance(item) {
      const list = items();
      const idx = list.indexOf(item);
      item.classList.add("done");
      item.classList.remove("active");
      const next = list[idx + 1] || list[idx - 1];
      if (active === item) active = null;
      if (next && next !== item) setActive(next);
    },
    // Leave an item for later: it dims (still there, still reachable with j/k or a click) and the
    // next one is selected. On the last item, selection stays put rather than jumping backwards.
    skip(item) {
      const list = items();
      const next = list[list.indexOf(item) + 1];
      item.classList.add("skipped");
      if (next) setActive(next);
      else { item.classList.remove("active"); if (active === item) active = null; toast("That was the last one here — skipped items stay dimmed until you come back to them"); }
    },
    setActive,
    reset() { active = null; },
  };
}

// Button html with its keyboard shortcut shown.
function keyBtn(key, label, cls = "", attrs = "") {
  return `<button class="small ${cls}" data-key="${esc(key)}" ${attrs}>${label} <kbd>${esc(key)}</kbd></button>`;
}

// -- Comparison grid ------------------------------------------------------------------------
// rows: [{label, a, b, same?}] (a/b are html). Header row has "keep this one" radios.
function compareGrid(name, headA, headB, rows, keep) {
  const cell = (side, html, extra = "") => `<div class="${extra}${keep === side ? " keep" : ""}">${html}</div>`;
  return `<div class="compare">
    <div class="label head"></div>
    ${cell("a", `<label class="pick"><input type="radio" name="${esc(name)}" value="a" ${keep === "a" ? "checked" : ""}/> Keep ${headA}</label>`, "head")}
    ${cell("b", `<label class="pick"><input type="radio" name="${esc(name)}" value="b" ${keep === "b" ? "checked" : ""}/> Keep ${headB}</label>`, "head")}
    ${rows.map((r) => `<div class="label">${esc(r.label)}</div>${cell("a", r.a ?? "—", r.same ? "same" : "")}${cell("b", r.b ?? "—", r.same ? "same" : "")}`).join("")}
  </div>`;
}
