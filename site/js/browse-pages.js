// ---------------------------------------------------------------------
// Artists (#/artists) and Songs (#/songs) -- the Albums page's design
// (js/albums-page.js) for the other two lists: search, filters with live
// counts, sorts, "Show more". Artists is a wall of faces (each one's most
// played cover); Songs a ranked list. A genre bubble on an artist page
// opens Artists in that genre (#/artists?genre=), on a song page Songs.
// Styles: css/albums-page.css (al-) + css/browse-pages.css (ab-).
// ---------------------------------------------------------------------
const browseCache = { artists: null, songs: null, db: null };

// read ?genre= (a genre bubble) once, then tidy the address
function browseHashGenre(st, route) {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  if (!params.has("genre")) return;
  st.genre = params.get("genre") || null;
  st.q = ""; st.shown = st.pageSize;
  Object.keys(st).forEach((k) => { if (st[k] instanceof Set) st[k].clear(); });
  ["decade", "firstYear", "version"].forEach((k) => { if (k in st) st[k] = null; });
  history.replaceState(history.state, "", `#/${route}`);
}

// chips for one filter, counted against the other filters as they are
function browseChips(rows, pass, key, f, st, { sort = "count", limit = 0, label = (k) => k, on = (k) => st[key] === k } = {}) {
  const c = {};
  rows.filter((r) => pass(r, key)).forEach((r) => [].concat(f(r)).filter(Boolean).forEach((k) => { c[k] = (c[k] || 0) + 1; }));
  let e = Object.entries(c);
  e = sort === "key" ? e.sort((a, b) => a[0].localeCompare(b[0])) : sort === "keyDesc" ? e.sort((a, b) => b[0].localeCompare(a[0])) : sort === "none" ? e : e.sort((a, b) => b[1] - a[1]);
  if (limit && !st.more?.[key]) e = e.slice(0, limit).concat(e.slice(limit).filter(([k]) => on(k)));
  return { chips: e.map(([k, n]) => `<button class="al-chip${on(k) ? " on" : ""}" data-chip="${key}" data-val="${esc(k)}">${esc(label(k))} <i>${apFmt(n)}</i></button>`).join(""),
    more: limit && Object.keys(c).length > limit ? `<button class="linkish" data-more="${key}">${st.more?.[key] ? "fewer" : `+${Object.keys(c).length - limit} more`}</button>` : "" };
}

function browseFacet(title, chips) {
  return chips.chips ? `<div class="al-facet"><span class="al-f-title">${title}</span><div class="al-chips">${chips.chips}${chips.more}</div></div>` : "";
}

// ---------------------------------------------------------------------
// Artists
// ---------------------------------------------------------------------
const artistsState = { q: "", genre: null, has: new Set(), firstYear: null, sort: "plays", win: "month", pageSize: 60, shown: 60, more: {}, filtersOpen: null };
const ARTIST_SORTS = [["plays", "Most played"], ["recent", "Recently played"], ["new", "Newest discoveries"], ["records", "Most records"], ["live", "Most seen live"], ["name", "A–Z"]];
const ARTIST_HAS = { vinyl: "On vinyl", live: "Seen live", notlive: "Not seen live" };

function artistsBase() {
  if (browseCache.db !== db) Object.assign(browseCache, { db, artists: null, songs: null });
  if (browseCache.artists) return browseCache.artists;
  const plays = {}, vinyl = {}, shows = {}, faces = {}, genres = {};
  for (const r of query(`SELECT artist_id, count(*) AS c, min(played_at) AS first, max(played_at) AS last FROM scrobbles GROUP BY artist_id`)) plays[r.artist_id] = r;
  for (const r of query(`SELECT aa.artist_id, count(DISTINCT v.id) AS c FROM vinyl_holdings v JOIN album_artists aa ON aa.album_id = v.album_id GROUP BY aa.artist_id`)) vinyl[r.artist_id] = r.c;
  for (const r of query(`SELECT artist_id, count(*) AS c FROM setlists GROUP BY artist_id`)) shows[r.artist_id] = r.c;
  for (const r of query(`SELECT s.artist_id, s.album_id, al.cover_updated_at, count(*) AS c FROM scrobbles s JOIN albums al ON al.id = s.album_id WHERE al.cover_status = 'ok' GROUP BY s.artist_id, s.album_id`)) {
    if (!faces[r.artist_id] || r.c > faces[r.artist_id].c) faces[r.artist_id] = { id: r.album_id, v: r.cover_updated_at, c: r.c };
  }
  for (const r of query(`SELECT aa.artist_id, al.id, al.cover_updated_at FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE al.cover_status = 'ok'`)) faces[r.artist_id] ||= { id: r.id, v: r.cover_updated_at, c: 0 };
  if (hasTable("artist_genres")) {
    for (const r of query(`SELECT ag.artist_id, ge.name FROM artist_genres ag JOIN genres ge ON ge.id = ag.genre_id ORDER BY ag.albums DESC`)) (genres[r.artist_id] ||= []).push(r.name);
  }
  const rows = query(`SELECT id, name, sort_name FROM artists`).map((a) => ({
    id: a.id, name: a.name, sortKey: String(a.sort_name || a.name).replace(/^the\s+/i, "").toLowerCase(), key: a.name.toLowerCase(),
    plays: plays[a.id]?.c || 0, first: plays[a.id]?.first || null, last: plays[a.id]?.last || null,
    vinyl: vinyl[a.id] || 0, shows: shows[a.id] || 0, face: faces[a.id] || null, genres: genres[a.id] || [],
  })).filter((a) => (a.plays || a.vinyl || a.shows) && !/^various( artists)?$/i.test(a.name));
  rows.forEach((a) => { a.firstYear = a.first ? a.first.slice(0, 4) : null; });
  return (browseCache.artists = rows);
}

function renderArtistsBrowse() {
  const st = artistsState;
  browseHashGenre(st, "artists");
  browseHashRange(st, "artists");
  const wk = st.range?.win || st.win;
  const W = playWindow("artist_id", wk), win = PLAY_WINDOWS[wk];
  artistsBase().forEach((a) => { a.p = W.cur.get(a.id) || 0; a.pp = W.prev.get(a.id) || 0; });
  let rows = wk === "all" && !st.range ? artistsBase() : artistsBase().filter((a) => a.p);  // a window (or a Home stat) lists who you played
  if (st.range?.newOnly) rows = rows.filter((a) => a.first && a.first >= W.start);  // new: first ever play in the window
  const q = st.q.toLowerCase();
  const hasOk = (a) => [...st.has].every((h) => (h === "vinyl" ? a.vinyl : h === "live" ? a.shows : !a.shows));
  const pass = (a, skip) => (!q || a.key.includes(q)) && (skip === "genre" || !st.genre || a.genres.includes(st.genre))
    && (skip === "has" || hasOk(a)) && (skip === "firstYear" || !st.firstYear || a.firstYear === st.firstYear);
  const shown = rows.filter((a) => pass(a));
  shown.sort({
    plays: (a, b) => b.p - a.p || b.vinyl - a.vinyl || b.shows - a.shows,
    recent: (a, b) => String(b.last || "").localeCompare(String(a.last || "")),
    new: (a, b) => String(b.first || "").localeCompare(String(a.first || "")),
    records: (a, b) => b.vinyl - a.vinyl || b.p - a.p,
    live: (a, b) => b.shows - a.shows || b.p - a.p,
    name: (a, b) => a.sortKey.localeCompare(b.sortKey),
  }[st.sort]);
  const genreChips = browseChips(rows, pass, "genre", (a) => a.genres, st, { limit: 14, label: genreName });
  const hasChips = browseChips(rows, pass, "has", (a) => [a.vinyl ? "vinyl" : null, a.shows ? "live" : "notlive"], st, { sort: "none", label: (k) => ARTIST_HAS[k], on: (k) => st.has.has(k) });
  const yearChips = browseChips(rows, pass, "firstYear", (a) => a.firstYear, st, { sort: "keyDesc" });
  const activeText = [st.genre && genreName(st.genre), ...[...st.has].map((h) => ARTIST_HAS[h]), st.firstYear && `first played ${st.firstYear}`].filter(Boolean);
  const plays = shown.reduce((n, a) => n + a.p, 0);
  const charted = st.sort === "plays";
  const move = charted ? chartMoves(shown, artistsBase().filter((a) => pass(a)), W.start, win, { quiet: true }) : () => "";
  const firstYearMin = rows.reduce((m, a) => (a.firstYear && (!m || a.firstYear < m) ? a.firstYear : m), null);

  app.classList.add("wide");
  app.innerHTML = `
    <div class="al ab">
      ${browseSwitch("artists")}
      <div class="al-head">
        <div><div class="hud">Artists</div><h1>${st.genre ? esc(genreName(st.genre)) : st.range?.newOnly ? "New artists" : "Every artist"}</h1>
          <div class="subtle">${plural(shown.length, st.range?.newOnly ? "new artist" : "artist")} · ${plural(plays, "play")}${win.since ? ` in the ${esc(win.since)}` : ""}${st.genre ? ` · <button class="linkish" data-act="all-genres">all genres</button>` : ""}</div></div>
        <div class="al-tools">
          <input type="search" id="al-search" placeholder="Search artists…" value="${esc(st.q)}" />
          ${windowSeg(st)}
          <select id="al-sort" aria-label="Sort">${ARTIST_SORTS.map(([k, t]) => `<option value="${k}"${k === st.sort ? " selected" : ""}>${t}</option>`).join("")}</select>
        </div>
      </div>
      <details class="al-facets"${st.filtersOpen ? " open" : ""}>
        <summary>Filters${activeText.length ? ` · <b>${esc(activeText.join(" · "))}</b>` : ""}</summary>
        ${browseFacet("Genre", genreChips)}
        ${browseFacet("Also", hasChips)}
        ${browseFacet("First played", yearChips)}
        ${firstYearMin ? `<div class="subtle ab-note">Your plays go back to ${esc(firstYearMin)}, so “first played ${esc(firstYearMin)}” includes everyone you already loved.</div>` : ""}
      </details>
      ${charted && shown.length ? windowLegend(win) : ""}
      <div class="al-wall ab-wall">${shown.slice(0, st.shown).map((a) => `
        <a class="al-card" href="#/artist/${a.id}">${move(a)}
          ${a.face ? `<img class="al-art" src="${esc(coverUrl(a.face.id, a.face.v))}" alt="" loading="lazy" />` : `<div class="al-art ap-noart">${esc(a.name.replace(/^the\s+/i, "").slice(0, 1))}</div>`}
          <b>${esc(a.name)}</b>
          <span class="al-meta">${a.p ? `${apFmt(a.p)}<span class="ab-pw"> plays</span>` : "not played"}${abIcons(a.vinyl, a.shows, `Seen live ${a.shows}×`)}</span>
          ${a.genres.length ? `<span class="al-artist">${esc(a.genres.slice(0, 2).map(genreName).join(" · "))}</span>` : ""}
        </a>`).join("") || `<div class="empty">No artists match.</div>`}</div>
      ${shown.length > st.shown ? `<div class="al-more"><button class="coll-btn" data-act="show-more">Show ${Math.min(120, shown.length - st.shown)} more</button>
        <span class="subtle">showing ${apFmt(st.shown)} of ${apFmt(shown.length)}</span></div>` : ""}
    </div>`;
  wireWindowSeg(st, renderArtistsBrowse);
  wireBrowse(st, renderArtistsBrowse, { has: (v) => (st.has.has(v) ? st.has.delete(v) : (v === "live" && st.has.delete("notlive"), v === "notlive" && st.has.delete("live"), st.has.add(v))) });
}

// ---------------------------------------------------------------------
// Songs: one entry per song (its remaster rows folded in, as on the song page); a live or
// remix recording is its own entry
// ---------------------------------------------------------------------
const songsState = { q: "", genre: null, decade: null, version: null, has: new Set(), sort: "plays", win: "month", pageSize: 100, shown: 100, more: {}, filtersOpen: null };
const SONG_SORTS = [["plays", "Most played"], ["recent", "Recently played"], ["new", "Newest discoveries"], ["live", "Most heard live"], ["title", "A–Z"], ["artist", "Artist"]];
const SONG_HAS = { live: "Heard live", notlive: "Never heard live", vinyl: "On vinyl", unplayed: "Heard live, never played" };

function songsBase() {
  if (browseCache.db !== db) Object.assign(browseCache, { db, artists: null, songs: null });
  if (browseCache.songs) return browseCache.songs;
  const plays = {}, heard = {}, genres = {};
  for (const r of query(`SELECT song_id, count(*) AS c, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE song_id IS NOT NULL GROUP BY song_id`)) plays[r.song_id] = r;
  for (const r of query(`SELECT song_id, setlist_id FROM setlist_songs`)) (heard[r.song_id] ||= new Set()).add(r.setlist_id);
  const onVinyl = new Set(query(`SELECT album_id FROM vinyl_holdings${hasTable("album_parts") ? " UNION SELECT part_album_id FROM album_parts WHERE album_id IN (SELECT album_id FROM vinyl_holdings)" : ""}`).map((r) => r.album_id));
  if (hasTable("album_genres")) for (const r of query(`SELECT ag.album_id, ge.name FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id`)) (genres[r.album_id] ||= []).push(r.name);
  const rows = query(`SELECT so.id, so.title, so.artist_id, ar.name AS artist, so.album_id, al.title AS album, al.year, al.cover_status, al.cover_updated_at
    FROM songs so JOIN artists ar ON ar.id = so.artist_id LEFT JOIN albums al ON al.id = so.album_id`);
  const byKey = new Map();
  for (const r of rows) {
    const p = plays[r.id], h = heard[r.id];
    if (!p && !h) continue;
    const k = `${r.artist_id}|${trackKey(r.title)}`;
    let g = byKey.get(k);
    if (!g) byKey.set(k, (g = { rows: [], plays: 0, first: null, last: null, shows: new Set() }));
    g.rows.push({ ...r, plays: p?.c || 0 });
    g.plays += p?.c || 0;
    if (p && (!g.first || p.first < g.first)) g.first = p.first;
    if (p && (!g.last || p.last > g.last)) g.last = p.last;
    h?.forEach((x) => g.shows.add(x));
  }
  const out = [...byKey.values()].map((g) => {
    const best = g.rows.find((x) => !/\s-\s|[([]/.test(x.title)) || [...g.rows].sort((a, b) => b.plays - a.plays)[0];
    const withAlbum = g.rows.find((x) => x.album_id && x.id === best.id) || [...g.rows].sort((a, b) => b.plays - a.plays).find((x) => x.album_id) || best;
    const v = variantOf(best.title);
    return { ids: g.rows.map((x) => x.id), id: best.id, title: best.title, artist_id: best.artist_id, artist: best.artist, album_id: withAlbum.album_id, album: withAlbum.album, year: withAlbum.year,
      cover_status: withAlbum.cover_status, cover_updated_at: withAlbum.cover_updated_at, plays: g.plays, first: g.first, last: g.last, shows: g.shows.size,
      vinyl: g.rows.some((x) => onVinyl.has(x.album_id)), genres: genres[withAlbum.album_id] || [], decade: withAlbum.year ? `${Math.floor(withAlbum.year / 10) * 10}s` : null,
      version: v ? (v.startsWith("lang-") ? "Other language" : v[0].toUpperCase() + v.slice(1)) : "Studio",
      key: `${best.title} ${best.artist} ${withAlbum.album || ""}`.toLowerCase() };
  });
  return (browseCache.songs = out);
}

// each song's plays in a window and the window before, its remaster rows added together
function songsWindow(win) {
  const songs = songsBase(), W = playWindow("song_id", win);
  if (W.songs?.list === songs) return W.songs;
  const cur = new Map(), prev = new Map();
  for (const x of songs) {
    let c = 0, p = 0;
    for (const id of x.ids) { c += W.cur.get(id) || 0; p += W.prev.get(id) || 0; }
    if (c) cur.set(x, c);
    if (p) prev.set(x, p);
  }
  return (W.songs = { list: songs, start: W.start, cur, prev });
}

function renderSongsBrowse() {
  const st = songsState;
  browseHashGenre(st, "songs");
  browseHashRange(st, "songs");
  const wk = st.range?.win || st.win;
  const W = songsWindow(wk), win = PLAY_WINDOWS[wk];
  songsBase().forEach((x) => { x.p = W.cur.get(x) || 0; x.pp = W.prev.get(x) || 0; });
  const rows = wk === "all" && !st.range ? songsBase() : songsBase().filter((x) => x.p);  // a window (or a Home stat) lists what you played
  const q = st.q.toLowerCase();
  const hasOk = (s) => [...st.has].every((h) => (h === "live" ? s.shows : h === "notlive" ? !s.shows : h === "vinyl" ? s.vinyl : s.shows && !s.plays));
  const pass = (s, skip) => (!q || s.key.includes(q)) && (skip === "genre" || !st.genre || s.genres.includes(st.genre))
    && (skip === "decade" || !st.decade || s.decade === st.decade) && (skip === "version" || !st.version || s.version === st.version) && (skip === "has" || hasOk(s));
  const shown = rows.filter((s) => pass(s));
  shown.sort({
    plays: (a, b) => b.p - a.p || String(b.last || "").localeCompare(String(a.last || "")),
    recent: (a, b) => String(b.last || "").localeCompare(String(a.last || "")),
    new: (a, b) => String(b.first || "").localeCompare(String(a.first || "")),
    live: (a, b) => b.shows - a.shows || b.p - a.p,
    title: (a, b) => a.title.localeCompare(b.title),
    artist: (a, b) => a.artist.localeCompare(b.artist) || b.p - a.p,
  }[st.sort]);
  const charted = st.sort === "plays";
  const move = charted ? chartMoves(shown, songsBase().filter((x) => pass(x)), W.start, win) : () => "";
  const genreChips = browseChips(rows, pass, "genre", (s) => s.genres, st, { limit: 14, label: genreName });
  const decadeChips = browseChips(rows, pass, "decade", (s) => s.decade, st, { sort: "key" });
  const versionChips = browseChips(rows, pass, "version", (s) => s.version, st);
  const hasChips = browseChips(rows, pass, "has", (s) => [s.shows ? "live" : "notlive", s.vinyl ? "vinyl" : null, s.shows && !s.plays ? "unplayed" : null], st,
    { sort: "none", label: (k) => SONG_HAS[k], on: (k) => st.has.has(k) });
  const activeText = [st.genre && genreName(st.genre), st.decade, st.version, ...[...st.has].map((h) => SONG_HAS[h])].filter(Boolean);
  const plays = shown.reduce((n, s) => n + s.p, 0);
  const max = Math.max(1, ...shown.map((s) => s.p));

  app.classList.add("wide");
  app.innerHTML = `
    <div class="al ab">
      ${browseSwitch("songs")}
      <div class="al-head">
        <div><div class="hud">Songs</div><h1>${st.genre ? esc(genreName(st.genre)) : "Every song"}</h1>
          <div class="subtle">${plural(shown.length, "song")} · ${plural(plays, "play")}${win.since ? ` in the ${esc(win.since)}` : ""}${st.genre ? ` · <button class="linkish" data-act="all-genres">all genres</button>` : ""}</div></div>
        <div class="al-tools">
          <input type="search" id="al-search" placeholder="Search song, artist or album…" value="${esc(st.q)}" />
          ${windowSeg(st)}
          <select id="al-sort" aria-label="Sort">${SONG_SORTS.map(([k, t]) => `<option value="${k}"${k === st.sort ? " selected" : ""}>${t}</option>`).join("")}</select>
        </div>
      </div>
      <details class="al-facets"${st.filtersOpen ? " open" : ""}>
        <summary>Filters${activeText.length ? ` · <b>${esc(activeText.join(" · "))}</b>` : ""}</summary>
        ${browseFacet("Genre", genreChips)}
        ${browseFacet("Released", decadeChips)}
        ${browseFacet("Version", versionChips)}
        ${browseFacet("Also", hasChips)}
      </details>
      ${charted && shown.length ? windowLegend(win) : ""}
      <ol class="ab-songs">${shown.slice(0, st.shown).map((s, i) => `
        <li><a href="#/song/${s.id}">
          <span class="ab-pos"><b>${i + 1}</b>${move(s)}</span>
          ${s.album_id ? apCover(s.album_id, s.cover_status, s.cover_updated_at, s.album, "ab-thumb") : `<div class="ab-thumb ap-noart">${esc(s.title.slice(0, 1))}</div>`}
          <span class="ab-tt"><b><span class="ab-name">${esc(s.title)}</span>${s.version !== "Studio" ? `<i class="ab-tag">${esc(s.version)}</i>` : ""}${abIcons(s.vinyl, s.shows, `Heard live at ${plural(s.shows, "show")}`)}</b>
            <span>${esc(s.artist)}${s.album ? ` · ${esc(s.album)}` : ""}${s.year ? ` (${s.year})` : ""}</span></span>
          <span class="ab-meter"><span class="ab-n">${s.p ? apFmt(s.p) : "—"}</span><span class="ab-track"><i style="--w:${(s.p / max).toFixed(4)}"></i></span></span>
        </a></li>`).join("") || `<div class="empty">No songs match.</div>`}</ol>
      ${shown.length > st.shown ? `<div class="al-more"><button class="coll-btn" data-act="show-more">Show ${Math.min(200, shown.length - st.shown)} more</button>
        <span class="subtle">showing ${apFmt(st.shown)} of ${apFmt(shown.length)}</span></div>` : ""}
    </div>`;
  wireWindowSeg(st, renderSongsBrowse);
  wireBrowse(st, renderSongsBrowse, {
    has: (v) => {
      if (st.has.has(v)) return st.has.delete(v);
      if (v === "live" || v === "unplayed") st.has.delete("notlive");
      if (v === "notlive") { st.has.delete("live"); st.has.delete("unplayed"); }
      st.has.add(v);
    },
  });
}

// on vinyl / seen or heard live, as the tab bar's icons
const abIcons = (vinyl, live, liveTitle) => (vinyl || live ? `<span class="ab-icons">${vinyl ? `<span class="ab-ic vinyl" title="${typeof vinyl === "number" ? plural(vinyl, "record") : "On vinyl"}"><svg class="i"><use href="#i-disc"/></svg>${typeof vinyl === "number" && vinyl > 1 ? vinyl : ""}</span>` : ""}${live ? `<span class="ab-ic live" title="${esc(liveTitle)}"><svg class="i"><use href="#i-bolt"/></svg>${live > 1 ? live : ""}</span>` : ""}</span>` : "");

// ---------------------------------------------------------------------
// Week / Month / Year / All time -- shared by Songs, Albums and Artists. The same rolling windows
// as the home page; an entry's rank is compared with the window before (all time: with where it
// stood 30 days ago -- a week barely moves an all-time chart).
// ---------------------------------------------------------------------
const PLAY_WINDOWS = {
  day: { label: "Day", mod: "-1 day", prevMod: "-2 days", since: "last 24 hours", vs: "vs the 24 hours before" },  // from Home only
  week: { label: "Week", mod: "-7 days", prevMod: "-14 days", since: "last 7 days", vs: "vs the 7 days before" },
  month: { label: "Month", mod: "-30 days", prevMod: "-60 days", since: "last 30 days", vs: "vs the 30 days before" },
  year: { label: "Year", mod: "-12 months", prevMod: "-24 months", since: "last 12 months", vs: "vs the 12 months before" },
  all: { label: "All time", mod: "-30 days", vs: "vs 30 days ago" },
};

// plays per song / album / artist (`col` of scrobbles) in a window and the window before
// -> { start, cur: Map(id -> n), prev: Map(id -> n) }
function playWindow(col, win) {
  if (browseCache.db !== db) Object.assign(browseCache, { db, artists: null, songs: null });
  browseCache.windows ||= {};
  const key = `${col}|${win}`;
  if (browseCache.windows[key]?.db === db) return browseCache.windows[key];
  const w = PLAY_WINDOWS[win];
  const iso = (mod) => query(`SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) AS d`, [mod])[0].d;
  const start = iso(w.mod), cur = new Map(), prev = new Map();
  if (win === "all") {
    for (const r of query(`SELECT ${col} AS k, count(*) AS n, sum(played_at >= ?) AS recent FROM scrobbles WHERE ${col} IS NOT NULL GROUP BY k`, [start])) {
      cur.set(r.k, r.n);
      if (r.n - r.recent) prev.set(r.k, r.n - r.recent);
    }
  } else {
    for (const r of query(`SELECT ${col} AS k, sum(played_at >= ?) AS cur, sum(played_at < ?) AS prev FROM scrobbles WHERE played_at >= ? AND ${col} IS NOT NULL GROUP BY k`,
      [start, start, iso(w.prevMod)])) {
      if (r.cur) cur.set(r.k, r.cur);
      if (r.prev) prev.set(r.k, r.prev);
    }
  }
  return (browseCache.windows[key] = { db, start, cur, prev });
}

// competition ranks (a tie shares its rank) of a list already sorted by n, highest first
function abRanks(list, n) {
  const out = new Map();
  list.forEach((x, i) => out.set(x, i && n(x) === n(list[i - 1]) ? out.get(list[i - 1]) : i + 1));
  return out;
}

// rank movement: each entry's rank (by window plays, x.p) against the window before's whole chart
// (x.pp, same filters) -> (entry) => badge html. On a cover wall "no change" says nothing.
function chartMoves(shown, before, start, win, { quiet = false } = {}) {
  const now = abRanks(shown, (x) => x.p);
  const was = abRanks(before.filter((x) => x.pp).sort((a, b) => b.pp - a.pp), (x) => x.pp);
  return (x) => {
    if (!x.p) return "";
    const r = was.get(x);
    if (!r) return `<i class="ab-mv new" title="${x.first && x.first >= start ? `First played in the ${esc(win.since || "last 30 days")}` : "Back — not played in the window before"}">new</i>`;
    const d = r - now.get(x);
    return d > 0 ? `<i class="ab-mv up" title="Up ${d} (was ${r}) ${esc(win.vs)}">▲${d}</i>` : d < 0 ? `<i class="ab-mv down" title="Down ${-d} (was ${r}) ${esc(win.vs)}">▼${-d}</i>`
      : quiet ? "" : `<i class="ab-mv same" title="No change ${esc(win.vs)}">–</i>`;
  };
}
// the switch -- or, arriving from a Home stat, that stat's range as a removable pill instead
const windowSeg = (st) => (st.range
  ? `<div class="ab-range" title="From Home's listening activity"><span><i>From Home</i> <b>${esc(PLAY_WINDOWS[st.range.win].since || "all time")}</b>${st.range.newOnly ? " · new artists" : ""}</span>
      <button type="button" data-act="clear-range" aria-label="Remove this filter" title="Remove this filter">✕</button></div>`
  : `<div class="seg ab-win" role="tablist" aria-label="When">${["week", "month", "year", "all"].map((k) =>
    `<button role="tab" data-win="${k}" aria-selected="${st.win === k}">${PLAY_WINDOWS[k].label}</button>`).join("")}</div>`);
// a link from a Home stat: #/songs?range=month, #/artists?range=week&new=1 -- applied once, filters cleared,
// so the list is exactly what the stat counted
function browseHashRange(st, route) {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const win = params.get("range");
  if (!win || !PLAY_WINDOWS[win]) return;
  st.range = { win, newOnly: params.get("new") === "1" };
  st.q = ""; st.genre = null; st.shown = st.pageSize; st.sort = "plays";
  Object.keys(st).forEach((k) => { if (st[k] instanceof Set) st[k].clear(); });
  ["decade", "firstYear", "version"].forEach((k) => { if (k in st) st[k] = null; });
  history.replaceState(history.state, "", `#/${route}`);
}
const windowLegend = (win) => `<div class="ab-legend subtle">▲▼ rank ${esc(win.vs)}</div>`;
function wireWindowSeg(st, rerender) {
  app.querySelectorAll("[data-win]").forEach((b) => b.addEventListener("click", () => { st.win = b.dataset.win; st.shown = st.pageSize; rerender(); }));
  app.querySelector("[data-act='clear-range']")?.addEventListener("click", () => { st.range = null; st.shown = st.pageSize; rerender(); });
}

// the controls both pages share
function wireBrowse(st, rerender, toggles = {}) {
  app.querySelector(".al-facets").addEventListener("toggle", (e) => { st.filtersOpen = e.target.open; });
  app.querySelectorAll("[data-chip]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.chip, v = b.dataset.val;
    if (toggles[k]) toggles[k](v); else st[k] = st[k] === v ? null : v;
    st.shown = st.pageSize; rerender();
  }));
  app.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => { st.more[b.dataset.more] = !st.more[b.dataset.more]; rerender(); }));
  app.querySelector("[data-act='all-genres']")?.addEventListener("click", () => { st.genre = null; rerender(); });
  app.querySelector("[data-act='show-more']")?.addEventListener("click", () => { st.shown += st.pageSize === 100 ? 200 : 120; rerender(); });
  document.getElementById("al-sort").addEventListener("change", (e) => { st.sort = e.target.value; st.shown = st.pageSize; rerender(); });
  wireSearchInput("al-search", st, () => { st.shown = st.pageSize; rerender(); });
}
