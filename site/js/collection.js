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
  Smo: ["Smoke", "colour"], Sun: ["Sunburst", "colour"], Amb: ["Amber", "colour"], Cry: ["Clear", "colour"],
};
const MEDIA_RE = /^(\d+)?x?(LP|12"|10"|7"|Vinyl|Box Set|CD|Cass)$/i;
const DISC_COLOURS = {
  red: "#b3262e", blue: "#2c63c9", yellow: "#e2bd3c", green: "#2e8f55", orange: "#dd7426", purple: "#7a3cc4",
  violet: "#8a4fd6", white: "#e9e6df", black: "#111", clear: "rgba(225,228,236,.28)", transparent: "rgba(225,228,236,.28)",
  gold: "#c9a227", silver: "#b9bcc6", pink: "#e27fac", rose: "#df8da6", brown: "#6f4428", grey: "#7c7f86", gray: "#7c7f86",
  turquoise: "#2bb5b0", smoke: "rgba(110,110,120,.6)", bone: "#e8dcc2", cream: "#eee2c4", magenta: "#c2378f", teal: "#23857f",
  aqua: "#46b7d6", beige: "#dccfb0", maroon: "#6d1f2a", oxblood: "#5a1a1f", "sea blue": "#2a6fa0", crimson: "#a4162c",
  amber: "#d9921f",
};
const GRADES = [
  [/^mint/i, "M", 8], [/^near mint/i, "NM", 7], [/^very good plus/i, "VG+", 6], [/^very good/i, "VG", 5],
  [/^good plus/i, "G+", 4], [/^good/i, "G", 3], [/^fair/i, "F", 2], [/^poor/i, "P", 1], [/generic|no cover|not graded/i, "—", 0],
];
// Format facets: a friendly name -> does this record have it?
const FORMAT_FACETS = {
  "Coloured": (r) => r.colours.length > 0 && !(r.colours.length === 1 && r.colours[0] === "black" && r.discEffect !== "translucent"),
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
  const fx = r.discEffect;
  if (fx === "split") return `${grooves}, linear-gradient(90deg, ${base} 50%, ${cols[1] || "#e9e6df"} 50%)`;
  if (fx === "translucent") return `${grooves}, color-mix(in srgb, ${base} 55%, transparent)`;
  if (fx === "solid") return `${grooves}, ${base}`;
  if (fx === "splatter" || fx === "marbled" || fx === "swirl") {
    const b = cols[1] || "#e9e6df", c = cols[2] || b;
    if (fx === "splatter") return splatter(base, b, c, grooves);
    return `${grooves}, conic-gradient(from 40deg, ${base}, ${b}, ${c}, ${base}, ${b}, ${base})`;
  }
  if (r.colours.includes("splatter")) return splatter(base, cols[1] || "#e9e6df", cols[2] || cols[1] || "#e9e6df", grooves);
  if (r.colours.includes("marbled") || cols.length > 1) {
    const b = cols[1] || "#e9e6df";
    return `${grooves}, conic-gradient(from 40deg, ${base}, ${b}, ${base}, ${b}, ${base})`;
  }
  return `${grooves}, ${base}`;
}

// Splatter: bold flecks of the second (and third) colour, scattered over the base.
const SPLATS = [[72, 22, 6], [86, 44, 5], [78, 70, 7], [64, 52, 4], [90, 62, 3], [70, 86, 4], [82, 30, 3], [22, 30, 6], [35, 70, 5], [45, 40, 3], [60, 12, 3], [93, 50, 2]];
function splatter(base, b, c, grooves) {
  const dots = SPLATS.map(([x, y, r], i) => `radial-gradient(circle at ${x}% ${y}%, ${i % 3 === 2 ? c : b} 0 ${r}%, transparent ${r + 0.8}%)`);
  return `${dots.join(", ")}, ${grooves}, ${base}`;
}

function gradeOf(cond) {
  for (const [re, short, score] of GRADES) if (re.test(cond || "")) return { short, score, label: cond };
  return cond ? { short: cond, score: 0, label: cond } : null;
}
const starsHtml = (n) => (n ? `<span class="stars" title="Your rating: ${n}/5">${"★".repeat(n)}<span class="off">${"★".repeat(5 - n)}</span></span>` : "");
const jsonOr = (s, fallback) => { try { return s ? JSON.parse(s) : fallback; } catch { return fallback; } };
// The comparison form of a track title: case, accents, apostrophes ("Tomorrows" = "Tomorrow's"),
// "&" vs "and", bracketed and " - " suffixes (remasters, live...) all ignored.
// A " - …" ending is only dropped when it's an edition note ("- 2009 Remaster", "- Live at ..."),
// never part of the title itself ("95 - N.A.S.T.Y.").
const EDITION_TAIL = /\s+-\s+(?=.*\b(remaster(ed)?|live|version|mix|edit|demo|mono|stereo|single|bonus|edition|re-?record(ing)?|session|take|acoustic|instrumental|\d{4})\b).*$/i;
const normTitle = (t) => String(t || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/\s*[([].*?[)\]]/g, "").replace(EDITION_TAIL, "").replace(/['’`´]/g, "").replace(/&/g, " and ")
  .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const compactTitle = (t) => normTitle(t).replace(/ /g, "");
// A different recording, not just another edition: live, remix, demo, acoustic, instrumental --
// read only from the title's bracketed / " - " tail ("Live Wire" is just a title). -> tag or "".
function variantOf(title) {
  const t = String(title || "");
  const tails = [...t.matchAll(/[([]([^)\]]*)[)\]]/g)].map((m) => m[1]);
  const dash = t.indexOf(" - ");
  if (dash > 0) tails.push(t.slice(dash + 3));
  const tail = tails.join(" ").toLowerCase();
  if (/\b(live|unplugged)\b/.test(tail)) return "live";
  if (/\bremix\b|\bmix\b/.test(tail) && !/\boriginal mix\b/.test(tail)) return "remix";
  if (/\bdemo\b/.test(tail)) return "demo";
  if (/\bacoustic\b/.test(tail)) return "acoustic";
  if (/\binstrumental\b/.test(tail)) return "instrumental";
  return "";
}
// The grouping key for "the same recording": the title without edition notes, plus its variant.
const trackKey = (t) => { const v = variantOf(t); return normTitle(t) + (v ? `#${v}` : ""); };
// Similarity of two compact titles, 0-1 (edit distance) -- for "Seperate" vs "Separate".
function titleSimilarity(a, b) {
  if (!a || !b) return 0;
  const m = a.length, n = b.length;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = cur;
  }
  return 1 - prev[n] / Math.max(m, n);
}

let _tableCache = null;
const _columnCache = {};
function hasColumn(table, column) {
  _columnCache[table] ||= new Set(query(`PRAGMA table_info(${table})`).map((r) => r.name));
  return _columnCache[table].has(column);
}
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
           v.date_added, v.discogs_release_id, v.mb_release_id${hasColumn("vinyl_holdings", "pressing_year") ? ", v.pressing_year AS csv_pressing_year, v.disc_colour" : ""}
           ${hasColumn("vinyl_holdings", "cover_file") ? ", v.cover_file, v.display_title, v.release_year" : ""},
           al.title, al.year, al.cover_status, al.cover_updated_at, al.mbid AS album_mbid, ar.id AS artist_id, ar.name AS artist_name, ar.sort_name,
           (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays
           ${details ? ", d.country, d.released, d.year AS discogs_year, d.format_descriptions, d.format_text, d.identifiers, d.companies, d.tracklist, d.discogs_notes, d.styles" : ""}
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
    const pressingYear = r.discogs_year || r.csv_pressing_year || null;
    const markedReissue = f.reissue || desc.some((d) => /reissue|repress|remaster/i.test(d));
    // A reissue whose album year isn't earlier than the pressing: that "album year" is really the
    // pressing's own (not corrected in maintenance yet) -- the original year is unknown, not that.
    // A copy that's its own release (Electric Ladyland Part 1, a picture disc) can carry its own
    // title, first-release year and cover (set in maintenance); otherwise it's the album's.
    const albumYear = r.release_year || r.year;
    const originalYear = markedReissue && pressingYear && albumYear && albumYear >= pressingYear ? null : albumYear;
    const rec = {
      ...r, title: r.display_title || r.title, albumTitle: r.title, ownLook: Boolean(r.cover_file || r.display_title || r.release_year), label: labels.join(" / ") || null, labels, fmt: f, discs: f.discs, pressing_year: pressingYear,
      // a reissue: the format says so, or this pressing came out well after the album did
      year: originalYear, yearUnconfirmed: originalYear == null && Boolean(albumYear),
      reissue: markedReissue || Boolean(pressingYear && originalYear && pressingYear >= originalYear + 2),
      tagCodes: new Set(f.tags.map((t) => t.code)), descText: `${desc.join(" ")} ${r.format_text || ""}`,
      genres: genresByAlbum[r.album_id] || [], decade: originalYear ? `${Math.floor(originalYear / 10) * 10}s` : null,
      pressDecade: pressingYear ? `${Math.floor(pressingYear / 10) * 10}s` : null,
      addedYear: (r.date_added || "").slice(0, 4) || null, grade: gradeOf(r.media_condition), sleeveGrade: gradeOf(r.sleeve_condition),
      sortArtist: String(r.sort_name || r.artist_name || "").replace(/^the\s+/i, ""),
    };
    const own = jsonOr(r.disc_colour, null); // set by hand in maintenance: wins over what the format says
    rec.colours = own?.colours?.length ? own.colours : recordColours(f.tags, r.format_text);
    rec.discEffect = own?.effect || null;
    rec.colourSetByHand = Boolean(own);
    return rec;
  });
  _collection = { db, records };
  return records;
}

const collectionState = {
  view: "wall", q: "", sort: "artist", dir: null, group: "decade", groupDir: null, insights: false, filtersOpen: false, // dir null = the sort's natural direction
  facets: { genre: new Set(), decade: new Set(), pressed: new Set(), format: new Set(), label: new Set(), country: new Set(), grade: new Set(), rating: new Set() },
  open: null, more: {},
};
const FACET_OF = {
  genre: (r) => r.genres, decade: (r) => [r.decade || "Not confirmed"], label: (r) => r.labels,
  country: (r) => (r.country ? [r.country] : []), grade: (r) => (r.grade ? [r.grade.short] : []),
  pressed: (r) => (r.pressDecade ? [r.pressDecade] : []), rating: (r) => (r.rating ? ["★".repeat(r.rating)] : []),
  format: (r) => Object.keys(FORMAT_FACETS).filter((k) => FORMAT_FACETS[k](r)),
};

// Does a record pass every filter? `skip` leaves one facet out -- for that facet's own counts.
function passes(r, st, skip = null) {
  if (st.q) {
    const q = st.q.toLowerCase();
    if (![r.title, r.artist_name, r.label, r.catalog_number, ...r.genres].some((x) => String(x || "").toLowerCase().includes(q))) return false;
  }
  for (const [facet, sel] of Object.entries(st.facets)) {
    if (facet === skip || !sel.size) continue;
    const vals = FACET_OF[facet](r);
    if (facet === "genre" || facet === "format") { if (![...sel].every((s) => vals.includes(s))) return false; } // narrowing: all of them
    else if (!vals.some((v) => sel.has(v))) return false; // either of them
  }
  return true;
}

const SORTS = {
  added: { label: "Recently added", desc: true, fn: (a, b) => String(b.date_added || "").localeCompare(String(a.date_added || "")) },
  artist: { label: "Artist A–Z", fn: (a, b) => a.sortArtist.localeCompare(b.sortArtist) || (a.year || 0) - (b.year || 0) || a.title.localeCompare(b.title, undefined, { numeric: true }) },
  title: { label: "Title A–Z", fn: (a, b) => a.title.localeCompare(b.title) },
  year: { label: "Original year", fn: (a, b) => (a.year || 9999) - (b.year || 9999) },
  pressing: { label: "Pressing year", fn: (a, b) => (a.pressing_year || 9999) - (b.pressing_year || 9999) },
  rating: { label: "Your rating", desc: true, fn: (a, b) => (b.rating || 0) - (a.rating || 0) || String(b.date_added).localeCompare(String(a.date_added)) },
  plays: { label: "Most played", desc: true, fn: (a, b) => b.plays - a.plays },
};
const GROUPS = {
  none: { label: "No grouping" },
  artist: { label: "Artist", key: (r) => (r.sortArtist[0] || "#").toUpperCase().replace(/[^A-Z]/, "#") },
  decade: { label: "Decade", key: (r) => r.decade || "Unknown year" },
  genre: { label: "Genre", key: (r) => r.genres[0] || "No genre yet" },
  label: { label: "Label", key: (r) => r.label || "No label" },
  added: { label: "Year added", key: (r) => r.addedYear || "Unknown" },
};

// Is the list running high-to-low / Z-A? Each sort has a natural way round (newest added first,
// artists A-Z); its direction button flips it. The groups have their own button: decades, years
// added, labels… A-Z / oldest first, genres biggest first, until flipped.
const isDesc = (st) => (st.dir ? st.dir === "desc" : Boolean(SORTS[st.sort].desc));
const isGroupDesc = (st) => (st.groupDir ? st.groupDir === "desc" : st.group === "genre");
const dirLabel = (btn, desc, what) => {
  btn.innerHTML = desc ? "↓ Desc" : "↑ Asc";
  btn.title = `${what}: ${desc ? "descending" : "ascending"} — click to flip`;
};
function sortRecords(list, st) {
  const out = [...list].sort(SORTS[st.sort].fn);
  return isDesc(st) === Boolean(SORTS[st.sort].desc) ? out : out.reverse();
}

function applyHashFilters() {
  const m = (location.hash.split("?")[1] || "");
  if (!m) return;
  const p = new URLSearchParams(m);
  const st = collectionState;
  if (p.get("genre")) { Object.values(st.facets).forEach((s) => s.clear()); st.q = ""; st.facets.genre.add(p.get("genre")); }
  if (p.get("q")) { Object.values(st.facets).forEach((s) => s.clear()); st.q = p.get("q"); }
  history.replaceState(history.state, "", "#/vinyl"); // applied once -- a later re-render mustn't reset the filters again
  lastRenderedHash = location.hash;
}

function renderCollection(holdingId = null) {
  applyHashFilters();
  app.classList.add("wide"); // the cover wall wants the room; render() drops this on other pages
  setTopbarTitle("vinyl", "The Collection"); // on a phone the title sits in the top bar, in place of the artist search
  const all = loadCollection();
  const st = collectionState;
  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="coll-head">
      <div class="coll-title"><div class="hud">vinyl</div><h1>The Collection</h1></div>
      <div class="coll-actions">
        <button class="coll-btn" data-role="dig" title="Pull a random record from what's showing">⟳ <span class="dig-word">Crate </span>dig</button>
        <div class="seg" role="tablist" aria-label="View">${[["wall", "▦ Wall"], ["shelf", "▤ Shelf"], ["list", "≡ List"]].map(([v, l]) =>
          `<button role="tab" data-view="${v}" aria-selected="${st.view === v}">${l}</button>`).join("")}</div>
      </div>
    </div>
    <div class="coll-stats" data-role="stats"></div>
    <div class="coll-tools">
      <input type="search" id="coll-search" placeholder="Search title, artist, label, cat#, genre…" value="${esc(st.q)}" />
      <label>Sort <select data-role="sort">${Object.entries(SORTS).map(([k, s]) => `<option value="${k}" ${k === st.sort ? "selected" : ""}>${s.label}</option>`).join("")}</select>
        <button type="button" class="coll-btn dir-btn" data-role="dir"></button></label>
      <label>Group <select data-role="group">${Object.entries(GROUPS).map(([k, g]) => `<option value="${k}" ${k === st.group ? "selected" : ""}>${g.label}</option>`).join("")}</select>
        <button type="button" class="coll-btn dir-btn" data-role="group-dir"></button></label>
    </div>
    <div class="coll-filters${st.filtersOpen ? " open" : ""}">
      <button class="filters-bar" data-role="filters-bar" aria-expanded="${st.filtersOpen}"><span>Filters</span><span class="fb-active" data-role="fb-active"></span><svg class="i"><use href="#i-chevron"/></svg></button>
      <div class="coll-facets" data-role="facets"></div>
    </div>
    <div class="coll-count" data-role="count"></div>
    <div data-role="results"></div>
    <details class="coll-insights" ${st.insights ? "open" : ""}><summary><span class="hud">insights</span> What's on the shelf</summary><div data-role="insights"></div></details>
  `;
  const $ = (r) => app.querySelector(`[data-role="${r}"]`);
  app.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { st.view = b.dataset.view; app.querySelectorAll("[data-view]").forEach((x) => x.setAttribute("aria-selected", x === b)); update(); }));
  let t = null;
  app.querySelector("#coll-search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { st.q = e.target.value.trim(); update(); }, 150); });
  $("sort").addEventListener("change", (e) => { st.sort = e.target.value; st.dir = null; update(); });
  $("dir").addEventListener("click", () => { st.dir = isDesc(st) ? "asc" : "desc"; update(); });
  $("group").addEventListener("change", (e) => { st.group = e.target.value; st.groupDir = null; update(); });
  $("group-dir").addEventListener("click", () => { st.groupDir = isGroupDesc(st) ? "asc" : "desc"; update(); });
  // collapsed by default to one "Filters" bar, on its own line above the count
  const toggleFilters = () => {
    st.filtersOpen = !st.filtersOpen;
    app.querySelector(".coll-filters").classList.toggle("open", st.filtersOpen);
    $("filters-bar").setAttribute("aria-expanded", st.filtersOpen);
  };
  $("filters-bar").addEventListener("click", toggleFilters);
  $("dig").addEventListener("click", () => {
    const pool = all.filter((r) => passes(r, st));
    if (pool.length) openRecord(pool[Math.floor(Math.random() * pool.length)].id, { dig: true });
  });
  app.querySelector(".coll-insights").addEventListener("toggle", (e) => { st.insights = e.target.open; if (st.insights) renderInsights($("insights"), all.filter((r) => passes(r, st))); });

  function update() {
    const shown = sortRecords(all.filter((r) => passes(r, st)), st);
    dirLabel($("dir"), isDesc(st), "Sort");
    dirLabel($("group-dir"), isGroupDesc(st), "Groups");
    $("group-dir").hidden = st.group === "none" || st.view === "shelf"; // the shelf is always filed A-Z
    renderStats($("stats"), shown, all.length);
    renderFacets($("facets"), all, update);
    const chosen = Object.values(st.facets).flatMap((s) => [...s]);
    $("fb-active").innerHTML = chosen.length ? `<b>${chosen.length}</b> ${esc(chosen.join(" · "))}` : "";
    $("count").innerHTML = shown.length === all.length ? `${plural(all.length, "record")}`
      : `${plural(shown.length, "record")} of ${all.length} · <button class="linkish" data-role="clear">clear filters</button>`;
    $("count").querySelector("[data-role='clear']")?.addEventListener("click", () => {
      Object.values(st.facets).forEach((s) => s.clear()); st.q = ""; app.querySelector("#coll-search").value = ""; update();
    });
    const host = $("results");
    const shelf = st.view === "shelf" ? [...shown].sort(SORTS.artist.fn) : null;
    const groups = shelf ? null : groupRecords(shown, st.group, isGroupDesc(st));
    if (!shown.length) host.innerHTML = `<div class="empty-state">No records match — try removing a filter.</div>`;
    else if (shelf) renderShelf(host, shelf);
    else if (st.view === "list") renderList(host, groups);
    else renderWall(host, groups);
    if (st.insights) renderInsights($("insights"), shown);
    // previous / next (and swipes) follow the records in the order they're on screen
    collectionState.visible = (shelf || groups.flatMap((g) => g.items)).map((r) => r.id);
  }
  collectionState.refresh = update;
  update();
  if (holdingId) openRecord(holdingId, { replace: false });
}

function plural(n, word) { return `${Number(n).toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function groupRecords(list, group, desc = false) {
  if (group === "none") return [{ key: null, items: list }];
  const out = new Map();
  for (const r of list) {
    const k = GROUPS[group].key(r);
    if (!out.has(k)) out.set(k, []);
    out.get(k).push(r);
  }
  const keys = [...out.keys()];
  const unknown = (k) => /^(unknown|no )/i.test(String(k)); // "Unknown year", "No label"… always last
  const size = (k) => out.get(k).length;
  if (group === "genre") keys.sort((a, b) => unknown(a) - unknown(b) || (desc ? size(b) - size(a) : size(a) - size(b)) || a.localeCompare(b)); // by how many
  else keys.sort((a, b) => unknown(a) - unknown(b) || (desc ? -1 : 1) * String(a).localeCompare(String(b)));
  return keys.map((k) => ({ key: k, items: out.get(k) }));
}

function renderStats(host, shown, total) {
  const uniq = (f) => new Set(shown.flatMap((r) => [].concat(f(r))).filter(Boolean)).size;
  const top = (f) => {
    const c = {};
    shown.forEach((r) => { const k = f(r); if (k) c[k] = (c[k] || 0) + 1; });
    return Object.entries(c).sort((a, b) => b[1] - a[1])[0];
  };
  const topDecade = top((r) => r.decade), topPressed = top((r) => r.pressDecade);
  const reissues = shown.filter((r) => r.reissue).length;
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
    <div class="cs" title="The decade most of these albums first came out"><b>${topDecade ? topDecade[0] : "—"}</b><span>Top Released</span></div>
    <div class="cs" title="The decade most of these records were pressed"><b>${topPressed ? topPressed[0] : "—"}</b><span>Top Pressed</span></div>
    <div class="cs"><b>${shown.length - reissues}</b><span>original presses</span></div>
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
    if (facet === "rating") for (let n = 1; n <= 5; n++) c["★".repeat(n)] = c["★".repeat(n)] || 0; // every rating, 5★ to 1★, always
    return c;
  };
  const ORDER = { decade: (a, b) => a[0].localeCompare(b[0]), pressed: (a, b) => a[0].localeCompare(b[0]),
    grade: (a, b) => GRADE_ORDER.indexOf(a[0]) - GRADE_ORDER.indexOf(b[0]), rating: (a, b) => b[0].length - a[0].length };
  const row = (facet, title, limit = 12) => {
    const entries = Object.entries(counts(facet)).sort(ORDER[facet] || ((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])));
    if (!entries.length) return "";
    const open = st.more[facet];
    const visible = open ? entries : entries.slice(0, limit);
    return `<div class="facet"><span class="f-title">${title}</span><div class="f-chips">${visible.map(([v, n]) =>
      `<button class="fchip${st.facets[facet].has(v) ? " on" : ""}" data-facet="${facet}" data-val="${esc(v)}" ${n || st.facets[facet].has(v) ? "" : "disabled"}>${esc(v)} <i>${n}</i></button>`).join("")}
      ${entries.length > limit ? `<button class="linkish" data-more="${facet}">${open ? "less" : `+${entries.length - limit} more`}</button>` : ""}</div></div>`;
  };
  host.innerHTML = row("genre", "Genre", 12) + row("decade", "Released", 12) + row("pressed", "Pressed", 12) + row("format", "Format", 12)
    + row("grade", "Condition", 9) + row("rating", "Rating", 5) + row("label", "Label", 10) + row("country", "Pressed in", 10);
  host.querySelectorAll("[data-facet]").forEach((b) => b.addEventListener("click", () => {
    const s = st.facets[b.dataset.facet];
    s.has(b.dataset.val) ? s.delete(b.dataset.val) : s.add(b.dataset.val);
    onChange();
  }));
  host.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => { st.more[b.dataset.more] = !st.more[b.dataset.more]; onChange(); }));
}
const GRADE_ORDER = ["M", "NM", "VG+", "VG", "G+", "G", "F", "P", "—"];

function coverImg(r, cls = "") {
  if (r.cover_file) return `<img class="${cls}" src="public/covers/${encodeURIComponent(r.cover_file)}" alt="" loading="lazy" />`; // this copy's own
  return r.cover_status === "ok" ? `<img class="${cls}" src="${esc(coverUrl(r.album_id, r.cover_updated_at))}" alt="" loading="lazy" />` : `<div class="${cls} noart">${esc(r.title.slice(0, 1))}</div>`;
}
const colourDot = (r) => (r.colours.length && FORMAT_FACETS.Coloured(r) ? `<i class="cdot" style="background:${discBackground(r)}" title="${esc(r.colours.join(" / "))} vinyl"></i>` : "");

// One record as a tile -- the collection wall's, and (details: true) the home page's, which adds
// the key facts: what kind of pressing, its notable features and colour, label, country, when bought.
function recTileHtml(r, { details = false } = {}) {
  const press = r.reissue
    ? `<span class="rec-press" title="${r.year ? `A ${r.pressing_year || ""} reissue of the ${r.year} album` : "A reissue — the album's original year isn't confirmed yet"}">${r.pressing_year ? `${r.pressing_year} ` : ""}reissue</span>`
    : details ? `<span class="rec-press og">original press</span>` : "";
  let extra = "";
  if (details) {
    const feats = ["Limited", "180g", "Gatefold", "Picture disc", "Record Store Day"].filter((k) => FORMAT_FACETS[k](r));
    if (r.discs > 1) feats.unshift(`${r.discs}LP`);
    const colour = FORMAT_FACETS.Coloured(r) ? r.colours.map((c) => c[0].toUpperCase() + c.slice(1)).join(" / ") + (r.discEffect && r.discEffect !== "solid" ? ` ${r.discEffect}` : "") : "";
    extra = `
        <span class="rec-keys">${colour ? `<span class="ktag colour">${colourDot(r)}${esc(colour)}</span>` : ""}${feats.slice(0, 3).map((f) => `<span class="ktag">${esc(f)}</span>`).join("")}</span>
        <span class="rec-label">${esc([r.labels[0], r.country].filter(Boolean).join(" · "))}</span>
        <span class="rec-added" title="Added ${esc((r.date_added || "").slice(0, 10))}">added ${esc(relativeDay(r.date_added))}</span>`;
  }
  return `
      <button class="rec${details ? " rec-detailed" : ""}" data-id="${r.id}" title="${esc(r.title)} — ${esc(r.artist_name)}">
        <span class="rec-art"><span class="rec-disc" style="background:${discBackground(r)}"><i></i></span>${coverImg(r, "rec-cover")}</span>
        <span class="rec-title">${esc(r.title)}</span>
        <span class="rec-artist">${esc(r.artist_name)}</span>
        <span class="rec-meta">${starsHtml(r.rating)}<span>${r.year || ""}</span>${details ? "" : colourDot(r)}${!details && r.discs > 1 ? `<span class="tagl">${r.discs}LP</span>` : ""}</span>
        ${press}${extra}
      </button>`;
}

function relativeDay(iso) {
  if (!iso) return "";
  const d = new Date(iso), days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  if (days < 30) return `${Math.floor(days / 7)} week${days < 14 ? "" : "s"} ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}

// The home page's "Latest record buys": the newest additions, opened in place (no page change).
function latestRecordsHtml(n = 8) {
  const recent = loadCollection().filter((r) => r.date_added).sort(SORTS.added.fn).slice(0, n);
  return recent.length ? `<div class="wall home-wall">${recent.map((r) => recTileHtml(r, { details: true })).join("")}</div>` : '<div class="subtle">No dated additions yet.</div>';
}
function wireLatestRecords(host) {
  const ids = [...host.querySelectorAll(".rec[data-id]")].map((b) => +b.dataset.id);
  host.querySelectorAll(".rec[data-id]").forEach((b) => b.addEventListener("click", () => {
    collectionState.visible = ids; // ← → step through these
    openRecord(+b.dataset.id);
  }));
}

function renderWall(host, groups) {
  host.innerHTML = groups.map((g) => `${g.key !== null ? `<h3 class="coll-group">${esc(g.key)} <span>${g.items.length}</span></h3>` : ""}
    <div class="wall">${g.items.map((r) => recTileHtml(r)).join("")}</div>`).join("");
  host.querySelectorAll(".rec").forEach((b) => b.addEventListener("click", () => openRecord(+b.dataset.id)));
}

function renderList(host, groups) {
  host.innerHTML = `<div class="table-scroll"><table class="data-table coll-table"><thead><tr><th></th><th>Title</th><th>Artist</th><th class="num">Year</th>
      <th class="num">Pressed</th><th>Label · cat#</th><th>Format</th><th>Condition</th><th>Rating</th><th>Added</th></tr></thead><tbody>
    ${groups.map((g) => `${g.key !== null ? `<tr class="grp"><td colspan="10">${esc(g.key)} <span class="subtle">${g.items.length}</span></td></tr>` : ""}
      ${g.items.map((r) => `<tr data-id="${r.id}"><td>${coverImg(r, "lthumb")}</td><td class="row-title">${esc(r.title)}</td><td>${esc(r.artist_name)}</td>
        <td class="num">${r.year || ""}</td><td class="num">${r.pressing_year || ""}${r.reissue ? ' <span class="subtle">RE</span>' : ""}</td><td>${esc([r.label, r.catalog_number].filter(Boolean).join(" · "))}</td>
        <td>${esc(r.fmt.media || "")} ${colourDot(r)}</td>
        <td>${r.grade ? esc(r.grade.short) : ""}${r.sleeveGrade ? ` <span class="subtle">/ ${esc(r.sleeveGrade.short)}</span>` : ""}</td>
        <td>${starsHtml(r.rating)}</td><td class="nowrap">${esc((r.date_added || "").slice(0, 10))}</td></tr>`).join("")}`).join("")}
    </tbody></table></div>`;
  host.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openRecord(+tr.dataset.id)));
}

// Shelf: spines filed A-Z by artist, each in its cover's own dominant colour.
const spineColours = (() => { try { return JSON.parse(localStorage.getItem("mc-spines") || "{}"); } catch { return {}; } })();
function spineColourFor(r, el) {
  const key = `${r.album_id}@${r.cover_updated_at || ""}`; // a replaced cover gets its colour worked out again
  if (spineColours[key]) { el.style.setProperty("--spine", spineColours[key]); return; }
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
      spineColours[key] = col;
      el.style.setProperty("--spine", col);
      try { localStorage.setItem("mc-spines", JSON.stringify(spineColours)); } catch { /* storage full or blocked -- just recompute next time */ }
    } catch { /* cross-origin or decode issue: keep the default spine */ }
  };
  img.src = coverUrl(r.album_id, r.cover_updated_at);
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
    ${bars("Released (original decade)", tally((r) => r.decade || "Not confirmed"), "decade", { sort: "key" })}
    ${bars("Pressed (decade)", tally((r) => r.pressDecade), "pressed", { sort: "key" })}
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
  // Opening the drawer is its own history step, so the phone's Back (or ✕) closes it and leaves
  // you where you were; stepping between records replaces that step rather than piling up.
  const onVinyl = location.hash.startsWith("#/vinyl");
  if (!collectionState.open && history.state?.drawer == null) {
    if (onVinyl && location.hash.startsWith("#/vinyl/")) history.replaceState(history.state, "", "#/vinyl"); // deep link: the collection goes underneath
    navDepth += 1;
    history.pushState({ depth: navDepth, drawer: id }, "", onVinyl ? `#/vinyl/${id}` : location.href); // elsewhere (home) it opens in place
  } else if (replace || history.state?.drawer != null) {
    history.replaceState({ ...history.state, drawer: id }, "", onVinyl ? `#/vinyl/${id}` : location.href);
  }
  collectionState.open = id;
  lastRenderedHash = location.hash; // opening a record isn't a navigation -- don't jump to the top later
  let shell = document.querySelector(".drawer-shell");
  if (!shell) {
    shell = document.createElement("div");
    shell.className = "drawer-shell";
    shell.innerHTML = `<div class="drawer-backdrop"></div><aside class="drawer" role="dialog" aria-modal="true" aria-label="Record"><div class="drawer-bar">
      <button class="icon-btn" data-role="prev" title="Previous (←)">‹</button><button class="icon-btn" data-role="next" title="Next (→)">›</button>
      <span class="drawer-pos" data-role="pos"></span><span class="spacer"></span><button class="icon-btn" data-role="close" title="Close (Esc)" aria-label="Close">✕</button></div><div class="drawer-body"></div></aside>`;
    document.body.appendChild(shell);
    const close = () => closeRecord();
    shell.querySelector(".drawer-backdrop").addEventListener("click", close);
    shell.querySelector("[data-role='close']").addEventListener("click", close);
    shell.querySelector("[data-role='prev']").addEventListener("click", () => stepRecord(-1));
    shell.querySelector("[data-role='next']").addEventListener("click", () => stepRecord(1));
    document.addEventListener("keydown", drawerKeys);
    wireSwipe(shell.querySelector(".drawer-body"));
  }
  requestAnimationFrame(() => shell.classList.add("open"));
  document.body.classList.add("drawer-open");
  const body = shell.querySelector(".drawer-body");
  const ids = location.hash.startsWith("#/vinyl") ? collectionState.visible || [] : [];
  const pos = ids.indexOf(id);
  shell.querySelector("[data-role='pos']").textContent = pos >= 0 && ids.length > 1 ? `${pos + 1} / ${ids.length}` : "";
  body.innerHTML = recordDetailHtml(r, all);
  body.scrollTop = 0;
  body.querySelectorAll("[data-genre]").forEach((b) => b.addEventListener("click", () => {
    if (!location.hash.startsWith("#/vinyl")) { closeRecord(false); location.hash = `#/vinyl?genre=${encodeURIComponent(b.dataset.genre)}`; return; }
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
  if (restoreHash && history.state?.drawer != null) { history.back(); return; } // popstate below does the closing
  shell.classList.remove("open");
  document.body.classList.remove("drawer-open");
  collectionState.open = null;
  if (restoreHash && location.hash.startsWith("#/vinyl/")) { history.replaceState(history.state, "", "#/vinyl"); lastRenderedHash = location.hash; }
  setTimeout(() => { if (!shell.classList.contains("open")) shell.remove(); document.removeEventListener("keydown", drawerKeys); }, 250);
}

// Back out of an open drawer: close it over the page that's already there -- no re-render, so
// the collection keeps its scroll position and filters.
window.addEventListener("popstate", () => {
  if (!collectionState.open || history.state?.drawer != null) return;
  if (location.hash !== lastRenderedHash && location.hash.split("?")[0] === "#/vinyl") skipRender = location.hash;
  lastRenderedHash = location.hash;
  closeRecord(false);
});

function stepRecord(dir) {
  const ids = collectionState.visible || [];
  const i = ids.indexOf(collectionState.open);
  if (i < 0 || !ids.length) return;
  openRecord(ids[(i + dir + ids.length) % ids.length]);
  // the next record slides in from the side it came from
  document.querySelector(".drawer-body")?.animate?.([{ transform: `translateX(${dir * 48}px)`, opacity: 0.2 }, { transform: "none", opacity: 1 }],
    { duration: 220, easing: "cubic-bezier(.2,.7,.3,1)" });
}

// Swipe left / right in the drawer for the next / previous record. The record follows the finger
// once the gesture is clearly sideways; vertical scrolling is left alone.
function wireSwipe(el) {
  let x0 = 0, y0 = 0, t0 = 0, dx = 0, mode = null; // mode: null (undecided) | "x" | "y"
  el.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1 || e.target.closest("input, textarea, select")) { mode = "y"; return; }
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = Date.now(); dx = 0; mode = null;
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (mode === "y") return;
    const mx = e.touches[0].clientX - x0, my = e.touches[0].clientY - y0;
    if (!mode) {
      if (Math.abs(mx) < 10 && Math.abs(my) < 10) return;
      mode = Math.abs(mx) > Math.abs(my) * 1.3 ? "x" : "y";
      if (mode === "y") return;
    }
    dx = mx;
    el.style.transition = "none";
    el.style.transform = `translateX(${dx * 0.6}px)`;
    el.style.opacity = String(1 - Math.min(Math.abs(dx) / 600, 0.5));
  }, { passive: true });
  el.addEventListener("touchend", () => {
    if (mode !== "x") return;
    mode = null;
    el.style.transition = "transform .18s ease, opacity .18s ease";
    el.style.transform = ""; el.style.opacity = "";
    const fast = Math.abs(dx) > 40 && Date.now() - t0 < 300;
    if (Math.abs(dx) > 80 || fast) {
      el.style.transition = "";
      stepRecord(dx < 0 ? 1 : -1);
    }
  });
  el.addEventListener("touchcancel", () => { mode = null; el.style.transform = ""; el.style.opacity = ""; });
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
  const colourName = r.colours.map((c) => c[0].toUpperCase() + c.slice(1)).join(" / ") + (r.discEffect && r.discEffect !== "solid" ? ` ${r.discEffect}` : "");
  const colour = r.colourSetByHand ? colourName : r.format_text || colourName;
  return `${[...labels].map((l) => `<span class="pbadge">${esc(l)}</span>`).join("")}${colour && FORMAT_FACETS.Coloured(r) ? `<span class="pbadge colour"><i class="cdot" style="background:${discBackground(r)}"></i>${esc(colour)}</span>` : colour ? `<span class="pbadge">${esc(colour)}</span>` : ""}`;
}

function recordDetailHtml(r, all) {
  const others = all.filter((x) => x.album_id === r.album_id && x.id !== r.id);
  const hist = query(`SELECT count(*) AS n, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE album_id = ?`, [r.album_id])[0];
  const songs = albumSongGroups(r.album_id);
  const live = songs.filter((x) => x.shows > 0);
  const liveShows = new Set(live.flatMap((x) => [...x.setlists])).size;
  const discogsTracks = jsonOr(r.tracklist, []).filter((t) => (t.type || "track") === "track" && t.title);
  const matched = matchTracklist(discogsTracks, songs);
  const tracks = discogsTracks.length ? matched.tracks : songs.map((x) => ({ pos: "", title: x.title, song: x }));
  // anything you've played from this album that this pressing's tracklist doesn't list -- so the
  // tracklist accounts for every play the album page counts
  const unlisted = matched.unlisted.filter((x) => x.plays);
  const ids = jsonOr(r.identifiers, []);
  const matrix = ids.filter((i) => /matrix|runout/i.test(i.type || ""));
  const barcode = ids.find((i) => /barcode/i.test(i.type || ""));
  const companies = jsonOr(r.companies, []);
  const pressedBy = companies.filter((c) => /pressed by|manufactured by|made by/i.test(c.role || "")).map((c) => `${c.role}: ${c.name}`);
  const pressingYear = r.pressing_year || (r.released || "").slice(0, 4);
  const pressKind = r.reissue ? "reissue" : "original press";
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
        <span class="press-kind ${r.reissue ? "re" : "og"}">${pressKind}${r.reissue && r.year ? ` of the ${r.year} album` : r.reissue ? " · original year not confirmed yet" : ""}</span></div>
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
      ${coverImg(o, "rd-other-art")}<span class="rd-other-text">
      ${o.title !== r.title ? `<b>${esc(o.title)}</b>` : ""}
      <b class="${o.title !== r.title ? "subtle" : ""}">${esc([o.country, o.pressing_year || (o.released || "").slice(0, 4)].filter(Boolean).join(" ") || o.label || "Another pressing")}</b>
      <span class="subtle">${esc([o.label, o.catalog_number].filter(Boolean).join(" · "))}${o.grade ? ` · ${esc(o.grade.short)}` : ""}</span></span></button>`).join("")}</section>` : ""}

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
        <span class="tr-right">${t.song?.shows && !t.again ? `<i class="livedot" title="Heard live at ${plural(t.song.shows, "show")}">●</i>` : ""}${t.song && !t.again ? `<span class="subtle">${t.song.plays}</span>` : t.again ? `<span class="subtle" title="Counted on its first line above">↑</span>` : ""}${t.dur ? `<span class="subtle dur">${esc(t.dur)}</span>` : ""}</span></li>`).join("")}</ol>
      ${unlisted.length ? `<div class="rd-unlisted"><div class="subtle">Also played from this album — not on this pressing's tracklist:</div>
        <ol class="rd-tracks">${unlisted.map((x) => `<li><button class="linkish" data-song="${x.id}">${esc(x.title)}</button>
          <span class="tr-right">${x.shows ? `<i class="livedot" title="Heard live at ${plural(x.shows, "show")}">●</i>` : ""}<span class="subtle">${x.plays}</span></span></li>`).join("")}</ol></div>` : ""}</section>` : ""}

    <div class="rd-foot"><button class="coll-btn" data-role="album-page">Open album page →</button></div>`;
}


// An album's songs, one entry per track -- shared by the record drawer and the album page so the
// two always agree. Every song filed under the album or scrobbled from it; rows for the same track
// not merged yet ("War Pigs - 2009 Remaster" + the setlist's "War Pigs") count together; links go
// to the plain-titled row. -> [{id, title, plays, shows, setlists}] most played first.
function albumSongGroups(albumId) {
  const rows = query(`
    SELECT so.id, so.title, so.mbid, (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays
    FROM songs so WHERE so.album_id = ? OR so.id IN (SELECT DISTINCT song_id FROM scrobbles WHERE album_id = ? AND song_id IS NOT NULL)
    ORDER BY plays DESC`, [albumId, albumId]);
  const setlistsOf = {};
  if (rows.length) {
    for (const x of query(`SELECT song_id, setlist_id FROM setlist_songs WHERE song_id IN (${rows.map(() => "?").join(",")})`, rows.map((x) => x.id))) {
      (setlistsOf[x.song_id] ||= []).push(x.setlist_id);
    }
  }
  // One entry per track, not per song row: a track's plays and live sightings are often split
  // across rows not merged yet ("War Pigs - 2009 Remaster" has the scrobbles, the setlist's plain
  // "War Pigs" the shows) -- count them all. Links go to the plain-titled row when there is one.
  const byKey = new Map();
  for (const x of rows) {
    const k = trackKey(x.title); // remasters fold together; a live / remix / demo version stays its own
    let g = byKey.get(k);
    if (!g) byKey.set(k, (g = { rows: [], plays: 0, setlists: new Set(), mbids: new Set() }));
    g.rows.push(x);
    if (x.mbid) g.mbids.add(x.mbid);
    g.plays += x.plays;
    (setlistsOf[x.id] || []).forEach((id) => g.setlists.add(id));
  }
  const songs = [...byKey.values()].map((g) => {
    const plain = g.rows.find((x) => !/\s-\s|[([]/.test(x.title)) || g.rows.find((x) => !EDITION_TAIL.test(x.title));
    const best = plain || g.rows[0];
    return { id: best.id, title: best.title, plays: g.plays, shows: g.setlists.size, setlists: g.setlists, mbids: g.mbids, variant: variantOf(best.title) };
  }).sort((a, b) => b.plays - a.plays);
  return songs;
}

// A pressing's tracklist -> your songs (albumSongGroups entries). Same title, else -- one clear
// winner only -- spacing-blind ("Good Morning" = "Goodmorning"), a prefix ("Wheels Of Confusion" /
// "... / The Straightener") or a near-identical spelling ("Seperate"). A song listed twice (a
// deluxe "Snowblind (Live)" after "Snowblind") is counted on its first line only (again: true).
// -> {tracks: [{pos, title, dur, song, again}], unlisted: [songs not on it]}
function matchTracklist(pressingTracks, songs) {
  const bySong = Object.fromEntries(songs.map((x) => [trackKey(x.title), x]));
  const used = new Set();
  const matchTrack = (title, recording) => {
    const k = normTitle(title), c = k.replace(/ /g, ""), v = variantOf(title);
    const free = () => songs.filter((x) => !used.has(x.id) && (x.variant || "") === v); // a live track only matches live songs
    const only = (list) => (list.length === 1 ? list[0] : null);
    let song = (recording && songs.find((x) => x.mbids?.has(recording))) // the same MusicBrainz recording, whatever it's called
      || bySong[k + (v ? `#${v}` : "")]
      || only(free().filter((x) => compactTitle(x.title) === c))
      || (k.length >= 4 ? only(free().filter((x) => normTitle(x.title).startsWith(`${k} `) || k.startsWith(`${normTitle(x.title)} `))) : null);
    if (!song && c.length >= 5) {
      const scored = free().map((x) => [titleSimilarity(c, compactTitle(x.title)), x]).filter(([sc]) => sc >= 0.85).sort((a, b) => b[0] - a[0]);
      if (scored.length === 1 || (scored.length > 1 && scored[0][0] - scored[1][0] >= 0.08)) song = scored[0][1];
    }
    // a live album's tracklist says "Ace of Spades", your plays say "Ace Of Spades - Live": that's
    // the track -- but only when the album has no studio version of it (then the live one's a bonus)
    if (!song && !v && !songs.some((x) => !x.variant && normTitle(x.title) === k)) {
      song = only(songs.filter((x) => !used.has(x.id) && x.variant && normTitle(x.title) === k));
    }
    const again = Boolean(song && used.has(song.id));
    if (song) used.add(song.id);
    return { song: song || null, again };
  };
  const tracks = pressingTracks.map((t) => ({ pos: t.position, title: t.title, dur: t.duration, ...matchTrack(t.title, t.recordingMbid) }));
  return { tracks, unlisted: songs.filter((x) => !used.has(x.id)) };
}

// MusicBrainz's original release of the album (the album-tracklists sweep), for albums not on vinyl.
function musicbrainzTracklist(albumId) {
  if (!hasTable("album_tracklists")) return null;
  const tracks = query(`SELECT number, title, recording_mbid, length_ms FROM album_tracklists WHERE album_id = ? ORDER BY position`, [albumId]);
  if (!tracks.length) return null;
  const src = query(`SELECT release_date, country, format FROM album_tracklist_sources WHERE album_id = ?`, [albumId])[0] || {};
  const mmss = (ms) => (ms ? `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}` : null);
  const what = [(src.release_date || "").slice(0, 4), src.country && src.country !== "XW" ? src.country : null, src.format].filter(Boolean).join(" ");
  return {
    tracks: tracks.map((t) => ({ position: t.number, title: t.title, duration: mmss(t.length_ms), recordingMbid: t.recording_mbid })),
    source: `the original release${what ? ` (${what})` : ""} · MusicBrainz`,
  };
}

// The album page's reference tracklist, from a pressing you own: an original press if you have
// one, else the shortest (closest to the album proper -- a deluxe reissue's extras are the bonus).
function albumReferenceTracklist(albumId) {
  const presses = loadCollection().filter((r) => r.album_id === albumId)
    .map((r) => ({ r, tracks: jsonOr(r.tracklist, []).filter((t) => (t.type || "track") === "track" && t.title) }))
    .filter((x) => x.tracks.length);
  if (!presses.length) return musicbrainzTracklist(albumId);
  presses.sort((a, b) => (a.r.reissue - b.r.reissue) || (a.tracks.length - b.tracks.length));
  const { r, tracks } = presses[0];
  const what = [r.reissue ? `${r.pressing_year || ""} reissue` : "original press", r.country].filter(Boolean).join(", ").trim();
  return { tracks, source: `your ${what} pressing`, holdingId: r.id, pressing: true };
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
