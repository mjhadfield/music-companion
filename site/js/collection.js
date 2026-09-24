// ---------------------------------------------------------------------
// The Collection (#/vinyl, #/vinyl/<holdingId> opens one record) -- the vinyl you own, to browse:
// a cover wall (default), a shelf of spines filed A-Z, or a list; combinable facets with live
// counts; insights; and a record drawer with the pressing, its condition, your notes, your
// history with the album and its tracklist.
//
// Everything is computed client-side from one query (a couple of hundred records). Genres and
// Discogs pressing detail (album_genres / vinyl_details) are optional: until the maintenance
// sweeps have run and the database has been published, the page simply shows less.
// ---------------------------------------------------------------------

// Discogs' format shorthand -> meaning. Mirrors FORMAT_TAGS in etl/maintenance/api/vinyl.py.
const FORMAT_TAGS = {
  RE: ["Reissue", "reissue"], RP: ["Repress", "reissue"], RM: ["Remastered", "reissue"],
  Gat: ["Gatefold", "feature"], Ltd: ["Limited", "feature"], Num: ["Numbered", "feature"],
  180: ["180g", "feature"], 200: ["200g", "feature"], Emb: ["Embossed", "feature"], Etch: ["Etched", "feature"],
  "S/Sided": ["Single-sided", "feature"], RSD: ["Record Store Day", "feature"], TP: ["Test pressing", "feature"],
  Promo: ["Promo", "feature"], Pic: ["Picture disc", "feature"], Club: ["Club edition", "feature"],
  Unofficial: ["Unofficial", "warn"], Dlx: ["Deluxe", "feature"], Box: ["Box set", "feature"],
  Comp: ["Compilation", "kind"], Album: ["Album", "kind"], EP: ["EP", "kind"], Single: ["Single", "kind"],
  Mono: ["Mono", "feature"], Stereo: ["Stereo", "feature"], Quad: ["Quadraphonic", "feature"],
  Red: ["Red", "colour"], Blu: ["Blue", "colour"], Yel: ["Yellow", "colour"], Gre: ["Green", "colour"],
  Ora: ["Orange", "colour"], Pur: ["Purple", "colour"], Whi: ["White", "colour"], Bla: ["Black", "colour"],
  Cle: ["Clear", "colour"], Tra: ["Transparent", "colour"], Gol: ["Gold", "colour"], Sil: ["Silver", "colour"],
  Pin: ["Pink", "colour"], Ros: ["Rose", "colour"], Bro: ["Brown", "colour"], Gry: ["Grey", "colour"],
  Mar: ["Marbled", "colour"], Spl: ["Splatter", "colour"], Vio: ["Violet", "colour"], Tur: ["Turquoise", "colour"],
  Smo: ["Smoke", "colour"], Sun: ["Sunburst", "colour"],
};
const MEDIA_RE = /^(\d+)?x?(LP|12"|10"|7"|Vinyl|Box Set|CD|Cass)$/i;
const DISC_COLOURS = {
  red: "#b3262e", blue: "#2c63c9", yellow: "#e2bd3c", green: "#2e8f55", orange: "#dd7426", purple: "#7a3cc4",
  violet: "#8a4fd6", white: "#e9e6df", black: "#111", clear: "rgba(225,228,236,.28)", transparent: "rgba(225,228,236,.28)",
  gold: "#c9a227", silver: "#b9bcc6", pink: "#e27fac", rose: "#df8da6", brown: "#6f4428", grey: "#7c7f86", gray: "#7c7f86",
  turquoise: "#2bb5b0", smoke: "rgba(110,110,120,.6)", bone: "#e8dcc2", cream: "#eee2c4", magenta: "#c2378f", teal: "#23857f",
  aqua: "#46b7d6", beige: "#dccfb0", maroon: "#6d1f2a", oxblood: "#5a1a1f", "sea blue": "#2a6fa0", crimson: "#a4162c",
};
const GRADES = [
  [/^mint/i, "M", 8], [/^near mint/i, "NM", 7], [/^very good plus/i, "VG+", 6], [/^very good/i, "VG", 5],
  [/^good plus/i, "G+", 4], [/^good/i, "G", 3], [/^fair/i, "F", 2], [/^poor/i, "P", 1], [/generic|no cover|not graded/i, "—", 0],
];
// Format facets: a friendly name -> does this record have it?
const FORMAT_FACETS = {
  "Coloured": (r) => r.colours.length > 0 && !(r.colours.length === 1 && r.colours[0] === "black"),
  "180g": (r) => r.tagCodes.has("180") || r.tagCodes.has("200") || /180|200 ?g/i.test(r.descText),
  "Gatefold": (r) => r.tagCodes.has("Gat") || /gatefold/i.test(r.descText),
  "Limited": (r) => r.tagCodes.has("Ltd") || /limited/i.test(r.descText),
  "Original press": (r) => !r.reissue,
  "Reissue": (r) => r.reissue,
  "Multi-disc": (r) => r.discs > 1,
  "Picture disc": (r) => r.tagCodes.has("Pic") || /picture/i.test(r.descText),
  "7″ / 10″": (r) => /7"|10"/.test(r.format || ""),
  "Compilation": (r) => r.tagCodes.has("Comp"),
  "Record Store Day": (r) => r.tagCodes.has("RSD"),
};

function parseFormat(fmt) {
  const media = [], tags = [];
  for (const part of String(fmt || "").split(/\s*\+\s*/)) {
    for (const tok of part.split(",").map((t) => t.trim()).filter(Boolean)) {
      if (MEDIA_RE.test(tok)) { media.push(tok); continue; }
      const [label, kind] = FORMAT_TAGS[tok] || [tok, "other"];
      if (!tags.some((t) => t.code === tok)) tags.push({ code: tok, label, kind });
    }
  }
  const discs = media.reduce((n, m) => n + (parseInt(m, 10) || 1), 0) || 1;
  return { media: media.join(" + ") || null, tags, discs, reissue: tags.some((t) => t.kind === "reissue") };
}

function recordColours(tags, formatText) {
  const out = tags.filter((t) => t.kind === "colour").map((t) => t.label.toLowerCase());
  const text = String(formatText || "").toLowerCase();
  for (const name of Object.keys(DISC_COLOURS)) if (new RegExp(`\\b${name}\\b`).test(text) && !out.includes(name)) out.push(name);
  return out.filter((c) => DISC_COLOURS[c] || c === "marbled" || c === "splatter" || c === "sunburst");
}

// CSS background for the disc peeking out of the sleeve: real colour where the format names one.
function discBackground(r) {
  const cols = r.colours.map((c) => DISC_COLOURS[c]).filter(Boolean);
  const grooves = "repeating-radial-gradient(circle at 50% 50%, rgba(255,255,255,.05) 0 1px, transparent 1px 3px)";
  const base = cols[0] || "#0d0d0f";
  if (r.colours.includes("splatter")) {
    const dot = cols[1] || "#e9e6df";
    return `radial-gradient(circle at 30% 35%, ${dot} 0 3%, transparent 4%), radial-gradient(circle at 70% 60%, ${dot} 0 4%, transparent 5%), radial-gradient(circle at 45% 75%, ${dot} 0 2%, transparent 3%), ${grooves}, ${base}`;
  }
  if (r.colours.includes("marbled") || cols.length > 1) {
    const b = cols[1] || "#e9e6df";
    return `${grooves}, conic-gradient(from 40deg, ${base}, ${b}, ${base}, ${b}, ${base})`;
  }
  return `${grooves}, ${base}`;
}

function gradeOf(cond) {
  for (const [re, short, score] of GRADES) if (re.test(cond || "")) return { short, score, label: cond };
  return cond ? { short: cond, score: 0, label: cond } : null;
}
const starsHtml = (n) => (n ? `<span class="stars" title="Your rating: ${n}/5">${"★".repeat(n)}<span class="off">${"★".repeat(5 - n)}</span></span>` : "");
const jsonOr = (s, fallback) => { try { return s ? JSON.parse(s) : fallback; } catch { return fallback; } };
const normTitle = (t) => String(t || "").toLowerCase().replace(/\s*[([].*?[)\]]/g, "").replace(/\s+-\s+.*$/, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();

let _tableCache = null;
function hasTable(name) {
  if (!_tableCache) _tableCache = new Set(query("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").map((r) => r.name));
  return _tableCache.has(name);
}

let _collection = null; // built once per loaded database
function loadCollection() {
  if (_collection && _collection.db === db) return _collection.records;
  const details = hasTable("vinyl_details");
  const rows = query(`
    SELECT v.id, v.album_id, v.label, v.catalog_number, v.format, v.media_condition, v.sleeve_condition, v.rating, v.notes,
           v.date_added, v.discogs_release_id, v.mb_release_id,
           al.title, al.year, al.cover_status, al.mbid AS album_mbid, ar.id AS artist_id, ar.name AS artist_name, ar.sort_name,
           (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays
           ${details ? ", d.country, d.released, d.year AS pressing_year, d.format_descriptions, d.format_text, d.identifiers, d.companies, d.tracklist, d.discogs_notes, d.styles" : ""}
    FROM vinyl_holdings v
    JOIN albums al ON al.id = v.album_id
    JOIN artists ar ON ar.id = al.artist_id
    ${details ? "LEFT JOIN vinyl_details d ON d.holding_id = v.id" : ""}
  `);
  const genresByAlbum = {};
  if (hasTable("album_genres")) {
    for (const g of query(`SELECT ag.album_id, ge.name FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id
                           WHERE ag.album_id IN (SELECT album_id FROM vinyl_holdings)
                           ORDER BY ag.source = 'manual' DESC, coalesce(ag.votes, 0) DESC, ge.name`)) {
      (genresByAlbum[g.album_id] ||= []).push(g.name);
    }
  }
  const records = rows.map((r) => {
    const f = parseFormat(r.format);
    const desc = jsonOr(r.format_descriptions, []);
    const labels = [...new Set(String(r.label || "").split(/\s*,\s*/).filter(Boolean))];
    const rec = {
      ...r, label: labels.join(" / ") || null, labels, fmt: f, discs: f.discs, reissue: f.reissue || desc.some((d) => /reissue|repress|remaster/i.test(d)),
      tagCodes: new Set(f.tags.map((t) => t.code)), descText: `${desc.join(" ")} ${r.format_text || ""}`,
      genres: genresByAlbum[r.album_id] || [], decade: r.year ? `${Math.floor(r.year / 10) * 10}s` : null,
      addedYear: (r.date_added || "").slice(0, 4) || null, grade: gradeOf(r.media_condition), sleeveGrade: gradeOf(r.sleeve_condition),
      sortArtist: String(r.sort_name || r.artist_name || "").replace(/^the\s+/i, ""),
    };
    rec.colours = recordColours(f.tags, r.format_text);
    return rec;
  });
  _collection = { db, records };
  return records;
}

const collectionState = {
  view: "wall", q: "", sort: "added", group: "none", insights: false,
  facets: { genre: new Set(), decade: new Set(), format: new Set(), label: new Set(), country: new Set(), grade: new Set() },
  minRating: 0, open: null, more: {},
};
const FACET_OF = {
  genre: (r) => r.genres, decade: (r) => (r.decade ? [r.decade] : []), label: (r) => r.labels,
  country: (r) => (r.country ? [r.country] : []), grade: (r) => (r.grade ? [r.grade.short] : []),
  format: (r) => Object.keys(FORMAT_FACETS).filter((k) => FORMAT_FACETS[k](r)),
};

// Does a record pass every filter? `skip` leaves one facet out -- for that facet's own counts.
function passes(r, st, skip = null) {
  if (st.q) {
    const q = st.q.toLowerCase();
    if (![r.title, r.artist_name, r.label, r.catalog_number, ...r.genres].some((x) => String(x || "").toLowerCase().includes(q))) return false;
  }
  if (st.minRating && (r.rating || 0) < st.minRating) return false;
  for (const [facet, sel] of Object.entries(st.facets)) {
    if (facet === skip || !sel.size) continue;
    const vals = FACET_OF[facet](r);
    if (facet === "genre" || facet === "format") { if (![...sel].every((s) => vals.includes(s))) return false; } // narrowing: all of them
    else if (!vals.some((v) => sel.has(v))) return false; // either of them
  }
  return true;
}

const SORTS = {
  added: { label: "Recently added", fn: (a, b) => String(b.date_added || "").localeCompare(String(a.date_added || "")) },
  artist: { label: "Artist A–Z", fn: (a, b) => a.sortArtist.localeCompare(b.sortArtist) || (a.year || 0) - (b.year || 0) },
  title: { label: "Title A–Z", fn: (a, b) => a.title.localeCompare(b.title) },
  year: { label: "Original year", fn: (a, b) => (a.year || 9999) - (b.year || 9999) },
  pressing: { label: "Pressing year", fn: (a, b) => (a.pressing_year || 9999) - (b.pressing_year || 9999) },
  rating: { label: "Your rating", fn: (a, b) => (b.rating || 0) - (a.rating || 0) || String(b.date_added).localeCompare(String(a.date_added)) },
  plays: { label: "Most played", fn: (a, b) => b.plays - a.plays },
};
const GROUPS = {
  none: { label: "No grouping" },
  artist: { label: "Artist", key: (r) => (r.sortArtist[0] || "#").toUpperCase().replace(/[^A-Z]/, "#") },
  decade: { label: "Decade", key: (r) => r.decade || "Unknown year" },
  genre: { label: "Genre", key: (r) => r.genres[0] || "No genre yet" },
  label: { label: "Label", key: (r) => r.label || "No label" },
  added: { label: "Year added", key: (r) => r.addedYear || "Unknown" },
};

function applyHashFilters() {
  const m = (location.hash.split("?")[1] || "");
  if (!m) return;
  const p = new URLSearchParams(m);
  const st = collectionState;
  if (p.get("genre")) { Object.values(st.facets).forEach((s) => s.clear()); st.q = ""; st.facets.genre.add(p.get("genre")); }
  if (p.get("q")) { Object.values(st.facets).forEach((s) => s.clear()); st.q = p.get("q"); }
  history.replaceState(null, "", "#/vinyl"); // applied once -- a later re-render mustn't reset the filters again
  lastRenderedHash = location.hash;
}

function renderCollection(holdingId = null) {
  applyHashFilters();
  app.classList.add("wide"); // the cover wall wants the room; render() drops this on other pages
  const all = loadCollection();
  const st = collectionState;
  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="coll-head">
      <div><div class="hud">vinyl</div><h1>The Collection</h1></div>
      <div class="coll-actions">
        <button class="coll-btn" data-role="dig" title="Pull a random record from what's showing">⟳ Crate dig</button>
        <div class="seg" role="tablist" aria-label="View">${[["wall", "▦ Wall"], ["shelf", "▤ Shelf"], ["list", "≡ List"]].map(([v, l]) =>
          `<button role="tab" data-view="${v}" aria-selected="${st.view === v}">${l}</button>`).join("")}</div>
      </div>
    </div>
    <div class="coll-stats" data-role="stats"></div>
    <div class="coll-tools">
      <input type="search" id="coll-search" placeholder="Search title, artist, label, cat#, genre…" value="${esc(st.q)}" />
      <label>Sort <select data-role="sort">${Object.entries(SORTS).map(([k, s]) => `<option value="${k}" ${k === st.sort ? "selected" : ""}>${s.label}</option>`).join("")}</select></label>
      <label>Group <select data-role="group">${Object.entries(GROUPS).map(([k, g]) => `<option value="${k}" ${k === st.group ? "selected" : ""}>${g.label}</option>`).join("")}</select></label>
      <button class="coll-btn filters-toggle" data-role="filters">Filters</button>
    </div>
    <div class="coll-facets" data-role="facets"></div>
    <div class="coll-count" data-role="count"></div>
    <div data-role="results"></div>
    <details class="coll-insights" ${st.insights ? "open" : ""}><summary><span class="hud">insights</span> What's on the shelf</summary><div data-role="insights"></div></details>
  `;
  const $ = (r) => app.querySelector(`[data-role="${r}"]`);
  app.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { st.view = b.dataset.view; app.querySelectorAll("[data-view]").forEach((x) => x.setAttribute("aria-selected", x === b)); update(); }));
  let t = null;
  app.querySelector("#coll-search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { st.q = e.target.value.trim(); update(); }, 150); });
  $("sort").addEventListener("change", (e) => { st.sort = e.target.value; update(); });
  $("group").addEventListener("change", (e) => { st.group = e.target.value; update(); });
  $("filters").addEventListener("click", () => app.querySelector(".coll-facets").classList.toggle("open"));
  $("dig").addEventListener("click", () => {
    const pool = all.filter((r) => passes(r, st));
    if (pool.length) openRecord(pool[Math.floor(Math.random() * pool.length)].id, { dig: true });
  });
  app.querySelector(".coll-insights").addEventListener("toggle", (e) => { st.insights = e.target.open; if (st.insights) renderInsights($("insights"), all.filter((r) => passes(r, st))); });

  function update() {
    const shown = all.filter((r) => passes(r, st)).sort(SORTS[st.sort].fn);
    renderStats($("stats"), shown, all.length);
    renderFacets($("facets"), all, update);
    const active = Object.values(st.facets).reduce((n, s) => n + s.size, 0) + (st.minRating ? 1 : 0);
    $("filters").textContent = active ? `Filters (${active})` : "Filters";
    $("count").innerHTML = shown.length === all.length ? `${plural(all.length, "record")}`
      : `${plural(shown.length, "record")} of ${all.length} · <button class="linkish" data-role="clear">clear filters</button>`;
    $("count").querySelector("[data-role='clear']")?.addEventListener("click", () => {
      Object.values(st.facets).forEach((s) => s.clear()); st.minRating = 0; st.q = ""; app.querySelector("#coll-search").value = ""; update();
    });
    const host = $("results");
    if (!shown.length) host.innerHTML = `<div class="empty-state">No records match — try removing a filter.</div>`;
    else if (st.view === "shelf") renderShelf(host, [...shown].sort(SORTS.artist.fn));
    else if (st.view === "list") renderList(host, groupRecords(shown, st.group));
    else renderWall(host, groupRecords(shown, st.group));
    if (st.insights) renderInsights($("insights"), shown);
    collectionState.visible = shown.map((r) => r.id);
  }
  collectionState.refresh = update;
  update();
  if (holdingId) openRecord(holdingId, { replace: false });
}

function plural(n, word) { return `${Number(n).toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function groupRecords(list, group) {
  if (group === "none") return [{ key: null, items: list }];
  const out = new Map();
  for (const r of list) {
    const k = GROUPS[group].key(r);
    if (!out.has(k)) out.set(k, []);
    out.get(k).push(r);
  }
  const keys = [...out.keys()];
  if (group === "decade" || group === "added") keys.sort();
  else if (group !== "genre") keys.sort((a, b) => a.localeCompare(b));
  else keys.sort((a, b) => out.get(b).length - out.get(a).length);
  return keys.map((k) => ({ key: k, items: out.get(k) }));
}

function renderStats(host, shown, total) {
  const uniq = (f) => new Set(shown.flatMap((r) => [].concat(f(r))).filter(Boolean)).size;
  const decades = {};
  shown.forEach((r) => { if (r.decade) decades[r.decade] = (decades[r.decade] || 0) + 1; });
  const topDecade = Object.entries(decades).sort((a, b) => b[1] - a[1])[0];
  const genreCounts = {};
  shown.forEach((r) => r.genres.slice(0, 2).forEach((g) => { genreCounts[g] = (genreCounts[g] || 0) + 1; }));
  const topGenres = Object.entries(genreCounts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const gsum = topGenres.reduce((n, [, c]) => n + c, 0);
  const countries = uniq((r) => r.country);
  host.innerHTML = `
    <div class="cs"><b>${shown.length}</b><span>${shown.length === total ? "records" : `of ${total}`}</span></div>
    <div class="cs"><b>${uniq((r) => r.artist_id)}</b><span>artists</span></div>
    <div class="cs"><b>${uniq((r) => r.labels)}</b><span>labels</span></div>
    ${countries ? `<div class="cs"><b>${countries}</b><span>countries</span></div>` : ""}
    <div class="cs"><b>${topDecade ? topDecade[0] : "—"}</b><span>top decade</span></div>
    <div class="cs cs-genres">${topGenres.length ? `<div class="gmix">${topGenres.map(([g, c], i) => `<i style="flex:${c}; --i:${i}" title="${esc(g)} · ${c}"></i>`).join("")}</div>
      <span>${topGenres.slice(0, 4).map(([g]) => esc(g)).join(" · ")}</span>` : `<span class="subtle">genres appear once they've been fetched</span>`}</div>`;
  void gsum;
}

function renderFacets(host, all, onChange) {
  const st = collectionState;
  const counts = (facet) => {
    const c = {};
    for (const r of all) if (passes(r, st, facet)) for (const v of FACET_OF[facet](r)) c[v] = (c[v] || 0) + 1;
    for (const v of st.facets[facet]) c[v] = c[v] || 0; // a selected value stays visible even at 0
    return c;
  };
  const ORDER = { decade: (a, b) => a[0].localeCompare(b[0]), grade: (a, b) => GRADE_ORDER.indexOf(a[0]) - GRADE_ORDER.indexOf(b[0]) };
  const row = (facet, title, limit = 12) => {
    const entries = Object.entries(counts(facet)).sort(ORDER[facet] || ((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])));
    if (!entries.length) return "";
    const open = st.more[facet];
    const visible = open ? entries : entries.slice(0, limit);
    return `<div class="facet"><span class="f-title">${title}</span><div class="f-chips">${visible.map(([v, n]) =>
      `<button class="fchip${st.facets[facet].has(v) ? " on" : ""}" data-facet="${facet}" data-val="${esc(v)}" ${n || st.facets[facet].has(v) ? "" : "disabled"}>${esc(v)} <i>${n}</i></button>`).join("")}
      ${entries.length > limit ? `<button class="linkish" data-more="${facet}">${open ? "less" : `+${entries.length - limit} more`}</button>` : ""}</div></div>`;
  };
  const extraActive = ["grade", "label", "country"].some((f) => st.facets[f].size) || st.minRating;
  const extraOpen = st.moreFilters || extraActive;
  host.innerHTML = row("genre", "Genre", 10) + row("decade", "Decade", 12) + row("format", "Format", 12)
    + `<div class="facet-more"${extraOpen ? "" : " hidden"}>${row("grade", "Condition", 9) + row("label", "Label", 10) + row("country", "Pressed in", 10)
    + `<div class="facet"><span class="f-title">Rating</span><div class="f-chips">${[5, 4, 3].map((n) =>
      `<button class="fchip${st.minRating === n ? " on" : ""}" data-rating="${n}">${"★".repeat(n)}${n < 5 ? "+" : ""}</button>`).join("")}</div></div>`}</div>
    ${extraActive ? "" : `<button class="linkish more-filters" data-role="more-filters">${extraOpen ? "Fewer filters ▴" : "More filters — condition, label, country, rating ▾"}</button>`}`;
  host.querySelector("[data-role='more-filters']")?.addEventListener("click", () => { st.moreFilters = !st.moreFilters; onChange(); });
  host.querySelectorAll("[data-facet]").forEach((b) => b.addEventListener("click", () => {
    const s = st.facets[b.dataset.facet];
    s.has(b.dataset.val) ? s.delete(b.dataset.val) : s.add(b.dataset.val);
    onChange();
  }));
  host.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => { st.more[b.dataset.more] = !st.more[b.dataset.more]; onChange(); }));
  host.querySelectorAll("[data-rating]").forEach((b) => b.addEventListener("click", () => { st.minRating = st.minRating === +b.dataset.rating ? 0 : +b.dataset.rating; onChange(); }));
}
const GRADE_ORDER = ["M", "NM", "VG+", "VG", "G+", "G", "F", "P", "—"];

function coverImg(r, cls = "") {
  return r.cover_status === "ok" ? `<img class="${cls}" src="public/covers/${r.album_id}.jpg" alt="" loading="lazy" />` : `<div class="${cls} noart">${esc(r.title.slice(0, 1))}</div>`;
}
const colourDot = (r) => (r.colours.length && FORMAT_FACETS.Coloured(r) ? `<i class="cdot" style="background:${discBackground(r)}" title="${esc(r.colours.join(" / "))} vinyl"></i>` : "");

function renderWall(host, groups) {
  host.innerHTML = groups.map((g) => `${g.key !== null ? `<h3 class="coll-group">${esc(g.key)} <span>${g.items.length}</span></h3>` : ""}
    <div class="wall">${g.items.map((r) => `
      <button class="rec" data-id="${r.id}" title="${esc(r.title)} — ${esc(r.artist_name)}">
        <span class="rec-art"><span class="rec-disc" style="background:${discBackground(r)}"><i></i></span>${coverImg(r, "rec-cover")}</span>
        <span class="rec-title">${esc(r.title)}</span>
        <span class="rec-artist">${esc(r.artist_name)}</span>
        <span class="rec-meta">${starsHtml(r.rating)}<span>${r.year || ""}</span>${colourDot(r)}${r.discs > 1 ? `<span class="tagl">${r.discs}LP</span>` : ""}</span>
      </button>`).join("")}</div>`).join("");
  host.querySelectorAll(".rec").forEach((b) => b.addEventListener("click", () => openRecord(+b.dataset.id)));
}

function renderList(host, groups) {
  host.innerHTML = `<div class="table-scroll"><table class="data-table coll-table"><thead><tr><th></th><th>Title</th><th>Artist</th><th class="num">Year</th>
      <th>Label · cat#</th><th>Format</th><th>Condition</th><th>Rating</th><th>Added</th></tr></thead><tbody>
    ${groups.map((g) => `${g.key !== null ? `<tr class="grp"><td colspan="9">${esc(g.key)} <span class="subtle">${g.items.length}</span></td></tr>` : ""}
      ${g.items.map((r) => `<tr data-id="${r.id}"><td>${coverImg(r, "lthumb")}</td><td class="row-title">${esc(r.title)}</td><td>${esc(r.artist_name)}</td>
        <td class="num">${r.year || ""}</td><td>${esc([r.label, r.catalog_number].filter(Boolean).join(" · "))}</td>
        <td>${esc(r.fmt.media || "")} ${colourDot(r)} ${r.reissue ? '<span class="subtle">RE</span>' : ""}</td>
        <td>${r.grade ? esc(r.grade.short) : ""}${r.sleeveGrade ? ` <span class="subtle">/ ${esc(r.sleeveGrade.short)}</span>` : ""}</td>
        <td>${starsHtml(r.rating)}</td><td class="nowrap">${esc((r.date_added || "").slice(0, 10))}</td></tr>`).join("")}`).join("")}
    </tbody></table></div>`;
  host.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openRecord(+tr.dataset.id)));
}

// Shelf: spines filed A-Z by artist, each in its cover's own dominant colour.
const spineColours = (() => { try { return JSON.parse(localStorage.getItem("mc-spines") || "{}"); } catch { return {}; } })();
function spineColourFor(r, el) {
  if (spineColours[r.album_id]) { el.style.setProperty("--spine", spineColours[r.album_id]); return; }
  if (r.cover_status !== "ok") return;
  const img = new Image();
  img.onload = () => {
    try {
      const c = document.createElement("canvas"); c.width = c.height = 12;
      const x = c.getContext("2d"); x.drawImage(img, 0, 0, 12, 12);
      const d = x.getImageData(0, 0, 12, 12).data;
      let R = 0, G = 0, B = 0, n = 0;
      for (let i = 0; i < d.length; i += 4) { R += d[i]; G += d[i + 1]; B += d[i + 2]; n++; }
      const col = `rgb(${Math.round(R / n)}, ${Math.round(G / n)}, ${Math.round(B / n)})`;
      spineColours[r.album_id] = col;
      el.style.setProperty("--spine", col);
      try { localStorage.setItem("mc-spines", JSON.stringify(spineColours)); } catch { /* storage full or blocked -- just recompute next time */ }
    } catch { /* cross-origin or decode issue: keep the default spine */ }
  };
  img.src = `public/covers/${r.album_id}.jpg`;
}
function renderShelf(host, list) {
  let letter = null;
  const parts = [];
  for (const r of list) {
    const L = (r.sortArtist[0] || "#").toUpperCase().replace(/[^A-Z]/, "#");
    if (L !== letter) { letter = L; parts.push(`<span class="shelf-letter">${esc(L)}</span>`); }
    parts.push(`<button class="spine" data-id="${r.id}" style="--w:${Math.min(r.discs, 3)}" aria-label="${esc(r.artist_name)} — ${esc(r.title)}">
      <span class="sp-artist">${esc(r.artist_name)}</span><span class="sp-title">${esc(r.title)}</span></button>`);
  }
  host.innerHTML = `<div class="shelf-note subtle">Filed A→Z by artist, the way they sit on the shelf · hover to pull one out</div><div class="shelf">${parts.join("")}</div>
    <div class="shelf-peek" hidden></div>`;
  const byId = Object.fromEntries(list.map((r) => [r.id, r]));
  const peek = host.querySelector(".shelf-peek");
  host.querySelectorAll(".spine").forEach((el) => {
    const r = byId[el.dataset.id];
    spineColourFor(r, el);
    el.addEventListener("click", () => openRecord(r.id));
    el.addEventListener("mouseenter", () => {
      peek.innerHTML = `${coverImg(r, "peek-cover")}<div><b>${esc(r.title)}</b><div class="subtle">${esc(r.artist_name)}${r.year ? ` · ${r.year}` : ""}</div>${starsHtml(r.rating)}</div>`;
      const b = el.getBoundingClientRect(), hb = host.getBoundingClientRect();
      peek.hidden = false;
      peek.style.left = `${Math.max(0, Math.min(b.left - hb.left - 60, hb.width - 250))}px`;
      peek.style.top = `${b.top - hb.top - 110}px`;
    });
    el.addEventListener("mouseleave", () => { peek.hidden = true; });
  });
}

// Insights: small charts over what's showing; clicking a bar applies that filter.
function renderInsights(host, shown) {
  const tally = (f) => { const c = {}; shown.forEach((r) => [].concat(f(r)).filter(Boolean).forEach((v) => { c[v] = (c[v] || 0) + 1; })); return c; };
  const bars = (title, counts, facet, { sort = "count", limit = 10 } = {}) => {
    let e = Object.entries(counts);
    e = sort === "key" ? e.sort((a, b) => a[0].localeCompare(b[0])) : e.sort((a, b) => b[1] - a[1]);
    e = e.slice(0, limit);
    const max = Math.max(1, ...e.map(([, n]) => n));
    return `<div class="ins"><h3>${title}</h3>${e.length ? e.map(([k, n]) => `<button class="ibar" ${facet ? `data-facet="${facet}" data-val="${esc(k)}"` : "disabled"}>
      <span class="ik">${esc(k)}</span><span class="iv"><i style="width:${(100 * n) / max}%"></i></span><span class="in">${n}</span></button>`).join("") : `<div class="subtle">—</div>`}</div>`;
  };
  const orig = shown.filter((r) => !r.reissue).length;
  host.innerHTML = `<div class="ins-grid">
    <div class="ins ins-wide"><h3>Added to the collection</h3><div data-role="added-chart"></div></div>
    ${bars("By decade", tally((r) => r.decade), "decade", { sort: "key" })}
    ${bars("Genres", tally((r) => r.genres), "genre", { limit: 12 })}
    ${bars("Labels", tally((r) => r.labels), "label")}
    ${bars("Condition (record)", tally((r) => r.grade?.short), "grade")}
    ${bars("Pressed in", tally((r) => r.country), "country")}
    <div class="ins"><h3>Originals vs reissues</h3><div class="split"><i style="flex:${orig}"></i><i style="flex:${shown.length - orig}"></i></div>
      <div class="subtle">${orig} original pressing${orig === 1 ? "" : "s"} · ${shown.length - orig} reissue${shown.length - orig === 1 ? "" : "s"}</div></div>
  </div>`;
  const years = tally((r) => r.addedYear);
  const byMonth = Object.keys(years).length <= 2; // mostly catalogued in one go: months say more
  const buckets = byMonth ? tally((r) => (r.date_added || "").slice(0, 7) || null) : years;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const label = (k) => (byMonth ? `${MONTHS[+k.slice(5, 7) - 1]} ${k.slice(2, 4)}` : k);
  renderBarChart(host.querySelector("[data-role='added-chart']"),
    Object.keys(buckets).sort().map((k) => ({ label: label(k), tooltipLabel: label(k), value: buckets[k], key: k })),
    { color: "var(--accent-vinyl)", height: 130 });
  host.querySelectorAll(".ibar[data-facet]").forEach((b) => b.addEventListener("click", () => {
    collectionState.facets[b.dataset.facet].add(b.dataset.val);
    collectionState.refresh();
    app.querySelector("[data-role='results']").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

// ---------------------------------------------------------------------
// The record drawer
// ---------------------------------------------------------------------
function openRecord(id, { replace = true } = {}) {
  const all = loadCollection();
  const r = all.find((x) => x.id === id);
  if (!r) return;
  collectionState.open = id;
  if (replace) history.replaceState(null, "", `#/vinyl/${id}`);
  lastRenderedHash = location.hash; // opening a record isn't a navigation -- don't jump to the top later
  let shell = document.querySelector(".drawer-shell");
  if (!shell) {
    shell = document.createElement("div");
    shell.className = "drawer-shell";
    shell.innerHTML = `<div class="drawer-backdrop"></div><aside class="drawer" role="dialog" aria-modal="true" aria-label="Record"><div class="drawer-bar">
      <button class="icon-btn" data-role="prev" title="Previous (←)">‹</button><button class="icon-btn" data-role="next" title="Next (→)">›</button>
      <span class="spacer"></span><button class="icon-btn" data-role="close" title="Close (Esc)" aria-label="Close">✕</button></div><div class="drawer-body"></div></aside>`;
    document.body.appendChild(shell);
    const close = () => closeRecord();
    shell.querySelector(".drawer-backdrop").addEventListener("click", close);
    shell.querySelector("[data-role='close']").addEventListener("click", close);
    shell.querySelector("[data-role='prev']").addEventListener("click", () => stepRecord(-1));
    shell.querySelector("[data-role='next']").addEventListener("click", () => stepRecord(1));
    document.addEventListener("keydown", drawerKeys);
  }
  requestAnimationFrame(() => shell.classList.add("open"));
  document.body.classList.add("drawer-open");
  const body = shell.querySelector(".drawer-body");
  body.innerHTML = recordDetailHtml(r, all);
  body.scrollTop = 0;
  body.querySelectorAll("[data-genre]").forEach((b) => b.addEventListener("click", () => {
    const st = collectionState;
    Object.values(st.facets).forEach((s) => s.clear());
    st.facets.genre.add(b.dataset.genre);
    closeRecord();
    st.refresh?.();
  }));
  body.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openRecord(+b.dataset.open)));
  body.querySelectorAll("[data-song]").forEach((b) => b.addEventListener("click", () => { closeRecord(false); location.hash = `#/song/${b.dataset.song}`; }));
  body.querySelector("[data-role='album-page']")?.addEventListener("click", () => { closeRecord(false); location.hash = `#/album/${r.album_id}`; });
  body.querySelector("[data-role='artist-page']")?.addEventListener("click", () => { closeRecord(false); location.hash = `#/artist/${r.artist_id}`; });
}

function closeRecord(restoreHash = true) {
  const shell = document.querySelector(".drawer-shell");
  if (!shell) return;
  shell.classList.remove("open");
  document.body.classList.remove("drawer-open");
  collectionState.open = null;
  if (restoreHash && location.hash.startsWith("#/vinyl/")) { history.replaceState(null, "", "#/vinyl"); lastRenderedHash = location.hash; }
  setTimeout(() => { if (!shell.classList.contains("open")) shell.remove(); document.removeEventListener("keydown", drawerKeys); }, 250);
}

function stepRecord(dir) {
  const ids = collectionState.visible || [];
  const i = ids.indexOf(collectionState.open);
  if (i < 0 || !ids.length) return;
  openRecord(ids[(i + dir + ids.length) % ids.length]);
}
function drawerKeys(e) {
  if (!collectionState.open || e.target.closest("input, textarea, select")) return;
  if (e.key === "Escape") closeRecord();
  if (e.key === "ArrowRight") stepRecord(1);
  if (e.key === "ArrowLeft") stepRecord(-1);
}

function meter(g, label) {
  if (!g) return "";
  return `<div class="meter"><span class="m-label">${label}</span><span class="m-bar">${[1, 2, 3, 4, 5, 6, 7, 8].map((i) => `<i class="${i <= g.score ? "on" : ""}"></i>`).join("")}</span>
    <span class="m-grade" title="${esc(g.label)}">${esc(g.short)}</span></div>`;
}

function pressingBadges(r) {
  const desc = jsonOr(r.format_descriptions, []);
  const labels = new Set();
  if (r.fmt.media) labels.add(r.fmt.media);
  for (const t of r.fmt.tags) if (!["kind", "colour"].includes(t.kind) || t.code === "Comp") labels.add(t.label);
  for (const d of desc) if (!/^(LP|Album|Vinyl)$/i.test(d)) labels.add(d.replace(/^RE$/, "Reissue"));
  const colour = r.format_text || (r.colours.length ? r.colours.map((c) => c[0].toUpperCase() + c.slice(1)).join(" / ") : "");
  return `${[...labels].map((l) => `<span class="pbadge">${esc(l)}</span>`).join("")}${colour && FORMAT_FACETS.Coloured(r) ? `<span class="pbadge colour"><i class="cdot" style="background:${discBackground(r)}"></i>${esc(colour)}</span>` : colour ? `<span class="pbadge">${esc(colour)}</span>` : ""}`;
}

function recordDetailHtml(r, all) {
  const others = all.filter((x) => x.album_id === r.album_id && x.id !== r.id);
  const hist = query(`SELECT count(*) AS n, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE album_id = ?`, [r.album_id])[0];
  const songs = query(`
    SELECT so.id, so.title, (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays,
           (SELECT count(DISTINCT ss.setlist_id) FROM setlist_songs ss WHERE ss.song_id = so.id) AS shows
    FROM songs so WHERE so.album_id = ? OR so.id IN (SELECT DISTINCT song_id FROM scrobbles WHERE album_id = ? AND song_id IS NOT NULL)
    ORDER BY plays DESC`, [r.album_id, r.album_id]);
  const live = songs.filter((s) => s.shows > 0);
  const liveShows = live.length ? query(`SELECT count(DISTINCT ss.setlist_id) AS n FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id
                                         WHERE so.id IN (${live.map(() => "?").join(",")})`, live.map((s) => s.id))[0].n : 0;
  const bySong = {};
  songs.forEach((s) => { if (!bySong[normTitle(s.title)]) bySong[normTitle(s.title)] = s; });
  const discogsTracks = jsonOr(r.tracklist, []).filter((t) => (t.type || "track") === "track" && t.title);
  const tracks = discogsTracks.length
    ? discogsTracks.map((t) => ({ pos: t.position, title: t.title, dur: t.duration, song: bySong[normTitle(t.title)] }))
    : songs.map((s) => ({ pos: "", title: s.title, song: s }));
  const ids = jsonOr(r.identifiers, []);
  const matrix = ids.filter((i) => /matrix|runout/i.test(i.type || ""));
  const barcode = ids.find((i) => /barcode/i.test(i.type || ""));
  const companies = jsonOr(r.companies, []);
  const pressedBy = companies.filter((c) => /pressed by|manufactured by|made by/i.test(c.role || "")).map((c) => `${c.role}: ${c.name}`);
  const pressingYear = r.pressing_year || (r.released || "").slice(0, 4);
  const fmtDate = (s) => (s ? new Date(s).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "");
  const top = songs[0]?.plays ? songs[0] : null;
  return `
    <div class="rd-head">
      <div class="rd-art"><span class="rec-disc big" style="background:${discBackground(r)}"><i></i></span>${coverImg(r, "rd-cover")}</div>
      <div class="rd-id">
        <h2>${esc(r.title)}</h2>
        <div><button class="linkish big" data-role="artist-page">${esc(r.artist_name)}</button>${r.year ? ` · ${r.year}` : ""}</div>
        <div class="rd-rating">${starsHtml(r.rating) || `<span class="subtle">not rated</span>`}</div>
        ${r.genres.length ? `<div class="rd-genres">${r.genres.map((g) => `<button class="gtag" data-genre="${esc(g)}" title="Show all ${esc(g)} records">${esc(g)}</button>`).join("")}</div>` : ""}
      </div>
    </div>

    <section class="rd-sec">
      <h3>This pressing</h3>
      <div class="rd-line"><b>${esc(r.label || "Unknown label")}</b>${r.catalog_number ? ` · <span class="mono">${esc(r.catalog_number)}</span>` : ""}${r.country ? ` · ${esc(r.country)}` : ""}${pressingYear ? ` · ${esc(pressingYear)}` : ""}
        ${r.year && pressingYear && String(r.year) !== String(pressingYear) ? `<span class="subtle"> (original ${r.year})</span>` : ""}</div>
      <div class="pbadges">${pressingBadges(r)}</div>
      ${meter(r.grade, "Record")}${meter(r.sleeveGrade, "Sleeve")}
      ${r.notes ? `<blockquote class="rd-note">${esc(r.notes).replace(/\n/g, "<br>")}<cite>— your note</cite></blockquote>` : ""}
      ${matrix.length || barcode || pressedBy.length || r.discogs_notes ? `<details class="rd-more"><summary>Runout, barcode & credits</summary>
        ${matrix.length ? `<div class="kv"><span>Matrix / runout</span><div>${matrix.map((m) => `<div class="mono">${esc(m.value)}${m.description ? ` <span class="subtle">${esc(m.description)}</span>` : ""}</div>`).join("")}</div></div>` : ""}
        ${barcode ? `<div class="kv"><span>Barcode</span><div class="mono">${esc(barcode.value)}</div></div>` : ""}
        ${pressedBy.length ? `<div class="kv"><span>Made by</span><div>${pressedBy.map(esc).join("<br>")}</div></div>` : ""}
        ${r.discogs_notes ? `<div class="kv"><span>Release notes</span><div class="subtle">${esc(r.discogs_notes).replace(/\n/g, "<br>")}</div></div>` : ""}
      </details>` : ""}
      <div class="rd-links">Added ${esc(fmtDate(r.date_added))}
        ${r.discogs_release_id ? ` · <a href="https://www.discogs.com/release/${r.discogs_release_id}" target="_blank" rel="noopener">Discogs ↗</a>` : ""}
        ${r.mb_release_id ? ` · <a href="https://musicbrainz.org/release/${r.mb_release_id}" target="_blank" rel="noopener">MusicBrainz ↗</a>` : r.album_mbid ? ` · <a href="https://musicbrainz.org/release-group/${r.album_mbid}" target="_blank" rel="noopener">MusicBrainz ↗</a>` : ""}</div>
    </section>

    ${others.length ? `<section class="rd-sec"><h3>Also in your collection</h3>${others.map((o) => `<button class="rd-other" data-open="${o.id}">
      <b>${esc([o.country, o.pressing_year || (o.released || "").slice(0, 4)].filter(Boolean).join(" ") || o.label || "Another pressing")}</b>
      <span class="subtle">${esc([o.label, o.catalog_number].filter(Boolean).join(" · "))}${o.grade ? ` · ${esc(o.grade.short)}` : ""}</span></button>`).join("")}</section>` : ""}

    <section class="rd-sec">
      <h3>Your history with it</h3>
      <div class="rd-hist">
        <div><b>${hist.n.toLocaleString()}</b><span>plays</span></div>
        ${hist.first ? `<div><b>${esc(hist.first.slice(0, 4))}</b><span>first played</span></div>` : ""}
        ${live.length ? `<div class="live"><b>${live.length}</b><span>song${live.length === 1 ? "" : "s"} heard live · ${plural(liveShows, "show")}</span></div>` : ""}
      </div>
      ${top ? `<div class="subtle">Most played: <button class="linkish" data-song="${top.id}">${esc(top.title)}</button> · ${plural(top.plays, "play")}</div>` : ""}
    </section>

    ${tracks.length ? `<section class="rd-sec"><h3>Tracklist <span class="subtle">plays${live.length ? " · ● heard live" : ""}</span></h3>
      <ol class="rd-tracks">${tracks.map((t) => `<li>${t.pos ? `<span class="pos">${esc(t.pos)}</span>` : ""}
        ${t.song ? `<button class="linkish" data-song="${t.song.id}">${esc(t.title)}</button>` : `<span>${esc(t.title)}</span>`}
        <span class="tr-right">${t.song?.shows ? `<i class="livedot" title="Heard live at ${plural(t.song.shows, "show")}">●</i>` : ""}${t.song ? `<span class="subtle">${t.song.plays}</span>` : ""}${t.dur ? `<span class="subtle dur">${esc(t.dur)}</span>` : ""}</span></li>`).join("")}</ol></section>` : ""}

    <div class="rd-foot"><button class="coll-btn" data-role="album-page">Open album page →</button></div>`;
}

// ---------------------------------------------------------------------
// Genre tags elsewhere in the app (album and artist pages). A tag links into the collection,
// filtered to it -- later, to a genre page of its own.
// ---------------------------------------------------------------------
function albumGenreNames(albumId) {
  if (!hasTable("album_genres")) return [];
  return query(`SELECT ge.name FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id WHERE ag.album_id = ?
                ORDER BY ag.source = 'manual' DESC, coalesce(ag.votes, 0) DESC, ge.name`, [albumId]).map((r) => r.name);
}
function artistGenreNames(artistId, limit = 8) {
  if (!hasTable("artist_genres")) return [];
  return query(`SELECT ge.name FROM artist_genres ag JOIN genres ge ON ge.id = ag.genre_id WHERE ag.artist_id = ?
                ORDER BY ag.vinyl_albums * 2 + ag.albums DESC, ge.name LIMIT ?`, [artistId, limit]).map((r) => r.name);
}
function genreTagsHtml(names, label = "") {
  if (!names.length) return "";
  return `<div class="genre-row">${label ? `<span class="subtle">${esc(label)}</span>` : ""}${names.map((g) =>
    `<a class="gtag" href="#/vinyl?genre=${encodeURIComponent(g)}" title="Your ${esc(g)} records">${esc(g)}</a>`).join("")}</div>`;
}
