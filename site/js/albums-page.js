// ---------------------------------------------------------------------
// Albums (#/albums, #/albums?genre=heavy%20metal) -- every album you've
// played or own, as a cover wall: search, genre and decade filters,
// sorting. A genre bubble anywhere on the site opens it filtered to that
// genre. Reached from the Artists / Albums / Songs switch on those pages.
// Styles: css/albums-page.css (al- prefix).
// ---------------------------------------------------------------------
const albumsState = { q: "", genre: null, decade: null, sort: "plays", win: "month", pageSize: 60, shown: 60, moreGenres: false };
const ALBUM_SORTS = [["plays", "Most played"], ["recent", "Recently played"], ["title", "A–Z"], ["artist", "Artist"], ["year", "Newest"]];
const albumsCache = { db: null, rows: null, genres: null };

// every album you've played or own, once per database
function albumsBase() {
  if (albumsCache.db === db) return albumsCache;
  const rows = query(`
    SELECT al.id, al.title, al.year, al.cover_status, al.cover_updated_at, ar.id AS artist_id, ar.name AS artist,
           coalesce(p.plays, 0) AS plays, p.first, p.last, coalesce(v.n, 0) AS vinyl
    FROM albums al JOIN artists ar ON ar.id = al.artist_id
    LEFT JOIN (SELECT album_id, count(*) AS plays, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE album_id IS NOT NULL GROUP BY album_id) p ON p.album_id = al.id
    LEFT JOIN (SELECT album_id, count(*) AS n FROM vinyl_holdings GROUP BY album_id) v ON v.album_id = al.id
    WHERE p.plays > 0 OR v.n > 0`);
  const genres = {};
  if (hasTable("album_genres")) {
    for (const g of query(`SELECT ag.album_id, ge.name FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id`)) (genres[g.album_id] ||= []).push(g.name);
  }
  rows.forEach((r) => { r.genres = genres[r.id] || []; r.decade = r.year ? `${Math.floor(r.year / 10) * 10}s` : null; r.key = `${r.title} ${r.artist}`.toLowerCase(); });
  Object.assign(albumsCache, { db, rows });
  return albumsCache;
}

function renderAlbumsBrowse() {
  const st = albumsState;
  // a genre (or decade) in the address -- from a genre bubble -- sets the filter
  const params = new URLSearchParams((location.hash.split("?")[1] || ""));
  if (params.has("genre")) { st.genre = params.get("genre") || null; st.decade = null; st.q = ""; st.shown = 60; history.replaceState(history.state, "", "#/albums"); }
  // the window: plays in it (Week / Month / Year / All time) and rank movement, as on Songs and Artists
  const W = playWindow("album_id", st.win), win = PLAY_WINDOWS[st.win];
  albumsBase().rows.forEach((r) => { r.p = W.cur.get(r.id) || 0; r.pp = W.prev.get(r.id) || 0; });
  const rows = st.win === "all" ? albumsBase().rows : albumsBase().rows.filter((r) => r.p);
  const q = st.q.toLowerCase();
  const pass = (r, skip) => (!q || r.key.includes(q)) && (skip === "genre" || !st.genre || r.genres.includes(st.genre)) && (skip === "decade" || !st.decade || r.decade === st.decade);
  const shown = rows.filter((r) => pass(r));
  const by = {
    plays: (a, b) => b.p - a.p || b.vinyl - a.vinyl,
    recent: (a, b) => String(b.last || "").localeCompare(String(a.last || "")),
    title: (a, b) => a.title.localeCompare(b.title),
    artist: (a, b) => a.artist.localeCompare(b.artist) || (a.year || 0) - (b.year || 0),
    year: (a, b) => (b.year || 0) - (a.year || 0) || b.p - a.p,
  }[st.sort];
  shown.sort(by);
  // the chips count what you'd get with each one, the other filters as they are
  const tally = (skip, f) => { const c = {}; rows.filter((r) => pass(r, skip)).forEach((r) => [].concat(f(r)).filter(Boolean).forEach((k) => { c[k] = (c[k] || 0) + 1; })); return c; };
  const genreCounts = Object.entries(tally("genre", (r) => r.genres)).sort((a, b) => b[1] - a[1]);
  if (st.genre && !genreCounts.some(([g]) => g === st.genre)) genreCounts.unshift([st.genre, 0]);
  const decadeCounts = Object.entries(tally("decade", (r) => r.decade)).sort((a, b) => a[0].localeCompare(b[0]));
  const genreChips = (st.moreGenres ? genreCounts : genreCounts.slice(0, 14));
  const plays = shown.reduce((n, r) => n + r.p, 0);
  const charted = st.sort === "plays";
  const move = charted ? chartMoves(shown, albumsBase().rows.filter((r) => pass(r)), W.start, win, { quiet: true }) : () => "";

  app.classList.add("wide");
  app.innerHTML = `
    <div class="al">
      ${browseSwitch("albums")}
      <div class="al-head">
        <div><div class="hud">Albums</div><h1>${st.genre ? esc(genreName(st.genre)) : "Every album"}</h1>
          <div class="subtle">${plural(shown.length, "album")} · ${plural(plays, "play")}${win.since ? ` in the ${esc(win.since)}` : ""}${st.genre ? ` · <button class="linkish" data-act="all-genres">all genres</button>` : ""}</div></div>
        <div class="al-tools">
          <input type="search" id="al-search" placeholder="Search album or artist…" value="${esc(st.q)}" />
          ${windowSeg(st)}
          <select id="al-sort" aria-label="Sort">${ALBUM_SORTS.map(([k, t]) => `<option value="${k}"${k === st.sort ? " selected" : ""}>${t}</option>`).join("")}</select>
        </div>
      </div>
      <details class="al-facets"${st.filtersOpen ? " open" : ""}>
        <summary>Filters${st.genre || st.decade ? ` · <b>${esc([st.genre && genreName(st.genre), st.decade].filter(Boolean).join(" · "))}</b>` : ""}</summary>
        <div class="al-facet"><span class="al-f-title">Genre</span><div class="al-chips">${genreChips.map(([g, n]) =>
          `<button class="al-chip${g === st.genre ? " on" : ""}" data-genre="${esc(g)}">${esc(genreName(g))} <i>${n}</i></button>`).join("")}
          ${genreCounts.length > 14 ? `<button class="linkish" data-act="more-genres">${st.moreGenres ? "fewer" : `+${genreCounts.length - 14} more`}</button>` : ""}</div></div>
        <div class="al-facet"><span class="al-f-title">Released</span><div class="al-chips">${decadeCounts.map(([d, n]) =>
          `<button class="al-chip${d === st.decade ? " on" : ""}" data-decade="${esc(d)}">${esc(d)} <i>${n}</i></button>`).join("")}</div></div>
      </details>
      ${charted && shown.length ? windowLegend(win) : ""}
      <div class="al-wall">${shown.slice(0, st.shown).map((r) => `
        <a class="al-card" href="#/album/${r.id}">${move(r)}
          ${apCover(r.id, r.cover_status, r.cover_updated_at, r.title, "al-art")}
          <b>${esc(r.title)}</b>
          <span class="al-artist">${esc(r.artist)}</span>
          <span class="al-meta">${[r.year, r.p ? `${apFmt(r.p)} plays` : "not played"].filter(Boolean).join(" · ")}${r.vinyl ? ` <span class="vinyl-dot" title="On vinyl">●</span>` : ""}</span>
        </a>`).join("") || `<div class="empty">No albums match.</div>`}</div>
      ${shown.length > st.shown ? `<div class="al-more"><button class="coll-btn" data-act="show-more">Show ${Math.min(120, shown.length - st.shown)} more</button>
        <span class="subtle">showing ${apFmt(st.shown)} of ${apFmt(shown.length)}</span></div>` : ""}
    </div>`;

  const rerender = () => renderAlbumsBrowse();
  wireWindowSeg(st, rerender);
  app.querySelector(".al-facets").addEventListener("toggle", (e) => { st.filtersOpen = e.target.open; });
  app.querySelectorAll("[data-genre]").forEach((b) => b.addEventListener("click", () => { st.genre = st.genre === b.dataset.genre ? null : b.dataset.genre; st.shown = 60; rerender(); }));
  app.querySelectorAll("[data-decade]").forEach((b) => b.addEventListener("click", () => { st.decade = st.decade === b.dataset.decade ? null : b.dataset.decade; st.shown = 60; rerender(); }));
  app.querySelector("[data-act='all-genres']")?.addEventListener("click", () => { st.genre = null; rerender(); });
  app.querySelector("[data-act='more-genres']")?.addEventListener("click", () => { st.moreGenres = !st.moreGenres; rerender(); });
  app.querySelector("[data-act='show-more']")?.addEventListener("click", () => { st.shown += 120; rerender(); });
  document.getElementById("al-sort").addEventListener("change", (e) => { st.sort = e.target.value; st.shown = 60; rerender(); });
  wireSearchInput("al-search", st, rerender);
}

// Artists · Albums · Songs: the three lists, one switch (no home tile needed for albums)
function browseSwitch(current) {
  return `<nav class="browse-switch" aria-label="Browse">${[["artists", "Artists"], ["albums", "Albums"], ["songs", "Songs"]].map(([k, t]) =>
    `<a href="#/${k}"${k === current ? ' aria-current="page"' : ""}>${t}</a>`).join("")}</nav>`;
}
