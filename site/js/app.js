/*
 * Music Companion frontend.
 *
 * No build step, no framework -- sql.js (SQLite compiled to WASM) loads
 * site/public/music.sqlite (a slim export built by etl/build_public_db.py,
 * with the bulky raw-JSON staging tables stripped out) directly in the
 * browser and every view below is just a SQL query run against it. Hash
 * routing (#/artist/1, #/song/2, ...) keeps navigation linkable and
 * back-button friendly without any router library.
 */

// Take manual control of scroll position on navigation/reload instead of
// letting the browser restore wherever it last was -- render() below
// decides when a fresh top-of-page is warranted (real navigation) vs.
// when the current position should be left alone (pagination, a
// same-route re-render).
if ("scrollRestoration" in history) history.scrollRestoration = "manual";

let db;
const app = document.getElementById("app");

// Bumped on every render() so an async enrichment fetch that resolves
// after the user has already navigated elsewhere knows to discard itself
// instead of writing into a page that's no longer showing.
let renderToken = 0;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// scrobbles.played_at is stored as a UTC ISO8601 string (correctly, by
// etl/lastfm_pull.py). Displaying it required converting to the viewer's
// own local time -- naively slicing the raw string just showed UTC
// verbatim, which reads as "an hour behind" (or however far off) for
// anyone not literally in UTC, most visibly during British Summer Time.
function formatLocalDateTime(isoString) {
  const d = new Date(isoString);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function query(sql, params = []) {
  const result = db.exec(sql, params);
  if (!result.length) return [];
  const { columns, values } = result[0];
  return values.map((row) => Object.fromEntries(columns.map((c, i) => [c, row[i]])));
}

function statCard(kind, value, label, href) {
  return `<a class="stat-card ${kind}" href="${href}"><div class="stat-value">${value}</div><div class="stat-label">${esc(label)}</div></a>`;
}

function renderNotFound(kind) {
  app.innerHTML = `<div class="error-box">${esc(kind)} not found. <a href="#/">Go home</a></div>`;
}

// ---------------------------------------------------------------------
// Theme toggle (light / dark / system, persisted in localStorage)
// ---------------------------------------------------------------------
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem("theme");
  } catch {
    /* ignore */
  }
  applyTheme(stored);

  document.getElementById("theme-toggle").addEventListener("click", () => {
    // No system-preference fallback -- matches the token restructure (harmonised with Citadel):
    // no data-theme attribute always means dark, unconditionally, there's no longer an
    // @media(prefers-color-scheme:dark) bucket for "system says dark" to be distinct from.
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* ignore */
    }
    render(); // re-render so any chart currently on screen redraws in the new theme's colors
  });
}

// ---------------------------------------------------------------------
// Small shared helpers for the browse/list pages
// ---------------------------------------------------------------------
function paginationHtml(page, totalPages) {
  return `
    <div class="pagination">
      <button data-action="prev" ${page <= 1 ? "disabled" : ""}>← Prev</button>
      <span>Page ${page.toLocaleString()} of ${totalPages.toLocaleString()}</span>
      <button data-action="next" ${page >= totalPages ? "disabled" : ""}>Next →</button>
    </div>
  `;
}

// `scope` defaults to the whole page since every browse page has at most
// one of these widgets active at a time -- but the artist page can have
// two independent bar-list panels (songs, albums) expanded simultaneously,
// each with its own pagination/sort controls, so their own re-render
// passes scope this to their own container rather than the whole page
// (otherwise a page-wide querySelector(All) would grab -- or, for
// wirePagination's querySelector, ONLY ever grab -- whichever panel's
// controls happen to come first in the DOM, regardless of which panel's
// button was actually clicked).
function wirePagination(state, totalPages, rerender, scope = app) {
  const prev = scope.querySelector('.pagination button[data-action="prev"]');
  const next = scope.querySelector('.pagination button[data-action="next"]');
  // Re-rendering swaps innerHTML, which loses scroll position if the page
  // shrinks and the browser clamps it -- so restore exactly where the
  // reader was, instead of forcing back to the top on every page turn.
  const turn = (delta) => {
    const scrollY = window.scrollY;
    state.page = Math.min(totalPages, Math.max(1, state.page + delta));
    rerender();
    window.scrollTo(0, scrollY);
  };
  if (prev) prev.addEventListener("click", () => turn(-1));
  if (next) next.addEventListener("click", () => turn(1));
}

// Selector is any [data-sort] element, not just table th's -- the bar-list
// panels (renderArtistSongsPanel, renderArtistAlbumsPanel) use the same
// data-sort attribute on plain buttons to keep a sort control without
// pulling in a table. See wirePagination above for why `scope` matters.
function wireSortableHeaders(state, rerender, ascByDefault = [], scope = app) {
  scope.querySelectorAll("[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sort === key) {
        state.dir = state.dir === "asc" ? "desc" : "asc";
      } else {
        state.sort = key;
        state.dir = ascByDefault.includes(key) ? "asc" : "desc";
      }
      state.page = 1;
      rerender();
    });
  });
}

// Set right before a re-render triggered by actually typing in a search
// box, so wireSearchInput below knows to restore focus/caret there --
// and, just as importantly, knows NOT to when the re-render came from
// something unrelated (a pagination click, a sort-header click). An
// unconditional .focus() here used to fire on every re-render, which
// made the browser auto-scroll the page to bring the input into view
// any time "Next" was clicked -- the scroll jump this variable fixes.
let restoreSearchFocus = false;

function wireSearchInput(id, state, rerender) {
  const input = document.getElementById(id);
  if (!input) return;
  let debounce;
  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.q = input.value.trim();
      state.page = 1;
      restoreSearchFocus = true;
      rerender();
    }, 200);
  });
  if (restoreSearchFocus) {
    restoreSearchFocus = false;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function sortHeader(key, label, state, numeric = false) {
  const active = state.sort === key;
  const arrow = active ? `<span class="arrow">${state.dir === "asc" ? "↑" : "↓"}</span>` : "";
  return `<th data-sort="${key}" class="${numeric ? "num " : ""}${active ? "sorted" : ""}">${esc(label)}${arrow}</th>`;
}

// Compact sort control for the bar-list panels below -- same [data-sort]
// attribute wireSortableHeaders already wires, just on plain buttons
// instead of table headers, so an expanded list can offer the same sort
// options without dropping into a table to do it.
function sortToggle(options, state) {
  return `
    <div class="sort-toggle">
      ${options.map(([key, label]) => {
        const active = state.sort === key;
        const arrow = active ? (state.dir === "asc" ? " ↑" : " ↓") : "";
        return `<button type="button" data-sort="${key}" class="sort-toggle-btn${active ? " active" : ""}">${esc(label)}${arrow}</button>`;
      }).join("")}
    </div>
  `;
}

// Renders rows in the same bar-chart shape used by the artist page's
// top-10 preview (renderArtist) -- shared so the "Show more" expansion
// below is visually identical to the preview it replaces, not a
// differently-styled table.
function barRowsHtml(rows, maxValue, routePrefix) {
  return rows.map((r) => `
    <div class="bar-row" onclick="location.hash='${routePrefix}${r.id}'">
      <div>
        <div class="bar-label">${esc(r.title)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${((r.plays / maxValue) * 100).toFixed(0)}%"></div></div>
      </div>
      <div class="bar-count">${r.plays.toLocaleString()}</div>
    </div>
  `).join("");
}

// ---------------------------------------------------------------------
// Home: overview stats + a headline chart + jump-in points
// ---------------------------------------------------------------------
const homeState = { granularity: "year" };

// Independent of the chart above -- its own Week/Month/Year/All time
// window (not Day/Month/Year/All: a single day of scrobbles makes for a
// pretty thin "most played" list), also defaulting to Year.
const MOST_PLAYED_WINDOWS = {
  week: { label: "Week", rangeModifier: "-7 days" },
  month: { label: "Month", rangeModifier: "-30 days" },
  year: { label: "Year", rangeModifier: "-12 months" },
  all: { label: "All", rangeModifier: null },
};
const MOST_PLAYED_WINDOW_ORDER = ["week", "month", "year", "all"];
const mostPlayedState = { window: "year" };

function renderHome() {
  const stats = query(`
    SELECT
      (SELECT count(*) FROM artists) AS artists,
      (SELECT count(*) FROM vinyl_holdings) AS vinyl,
      (SELECT count(*) FROM scrobbles) AS scrobbles,
      (SELECT count(*) FROM setlists) AS setlists,
      (SELECT count(*) FROM songs) AS songs,
      (SELECT count(*) FROM venues) AS venues,
      (SELECT count(DISTINCT label) FROM vinyl_holdings WHERE label IS NOT NULL AND label != '') AS labels,
      (SELECT count(DISTINCT aa.artist_id) FROM vinyl_holdings v JOIN album_artists aa ON aa.album_id = v.album_id) AS vinyl_bands,
      (SELECT count(DISTINCT artist_id) FROM setlists) AS live_bands
  `)[0];

  const mostPlayedWindow = MOST_PLAYED_WINDOWS[mostPlayedState.window];
  const topArtists = query(`
    SELECT ar.id, ar.name, count(*) AS plays
    FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id
    ${mostPlayedWindow.rangeModifier ? `WHERE s.played_at >= datetime('now', '${mostPlayedWindow.rangeModifier}')` : ""}
    GROUP BY ar.id ORDER BY plays DESC LIMIT 10
  `);

  const recentVinyl = query(`
    SELECT v.id AS holding_id, al.id AS album_id, al.cover_status, al.title, ar.name AS artist_name, v.date_added, v.format
    FROM vinyl_holdings v
    JOIN albums al ON al.id = v.album_id
    JOIN artists ar ON ar.id = al.artist_id
    WHERE v.date_added IS NOT NULL AND v.date_added != ''
    ORDER BY v.date_added DESC LIMIT 8
  `);

  const g = GRANULARITIES[homeState.granularity];
  const chartRows = query(`
    SELECT strftime('${g.fmt}', played_at) AS bucket, count(*) AS c
    FROM scrobbles
    ${g.rangeModifier ? `WHERE played_at >= datetime('now', '${g.rangeModifier}')` : ""}
    GROUP BY bucket ORDER BY bucket
  `);

  app.innerHTML = `
    <div class="stat-groups">
      <section class="stat-group stat-group--scrobble">
        <h2 class="hud">Global Stats</h2>
        <div class="stat-grid">
          ${statCard("scrobble", stats.scrobbles.toLocaleString(), "Total songs played", "#/scrobbles")}
          ${statCard("scrobble", stats.songs.toLocaleString(), "Unique tracks", "#/songs")}
          ${statCard("scrobble", stats.artists.toLocaleString(), "Total artists", "#/artists")}
        </div>
      </section>
      <section class="stat-group stat-group--live">
        <h2 class="hud">Live Shows</h2>
        <div class="stat-grid">
          ${statCard("live", stats.setlists, "Shows attended", "#/shows")}
          ${statCard("live", stats.venues, "Different venues", "#/venues")}
          ${statCard("live", stats.live_bands, "Unique bands", "#/shows")}
        </div>
      </section>
      <section class="stat-group stat-group--vinyl">
        <h2 class="hud">Physical Media</h2>
        <div class="stat-grid">
          ${statCard("vinyl", stats.vinyl, "Records owned", "#/vinyl")}
          ${statCard("vinyl", stats.labels, "Record labels", "#/vinyl")}
          ${statCard("vinyl", stats.vinyl_bands, "Unique bands", "#/vinyl")}
        </div>
      </section>
    </div>

    <div class="section">
      <h2>Listening activity</h2>
      <div id="home-chart-toolbar"></div>
      <div id="home-chart"></div>
    </div>

    <div class="section">
      <h2>Most played</h2>
      <div id="most-played-tabs" class="tabs-only"></div>
      <div class="pill-list">
        ${topArtists.map((a) => `
          <a class="pill" href="#/artist/${a.id}">${esc(a.name)} <span class="count">${a.plays.toLocaleString()}</span></a>
        `).join("") || '<div class="subtle">Nothing played in this window yet.</div>'}
      </div>
    </div>

    <div class="section">
      <h2>Latest record buys</h2>
      <div class="vinyl-tile-grid">
        ${recentVinyl.map((v) => `
          <div class="vinyl-tile" data-album-id="${v.album_id}" data-holding-id="${v.holding_id}">
            <img class="vinyl-tile-cover" data-album-id="${v.album_id}" data-cover-status="${v.cover_status || ""}" alt="" loading="lazy" />
            <div class="vinyl-tile-title">${esc(v.title)}</div>
            <div class="vinyl-tile-sub">${esc(v.artist_name)}</div>
            <div class="vinyl-tile-meta">${esc(v.format || "")} · ${esc((v.date_added || "").slice(0, 10))}</div>
          </div>
        `).join("") || '<div class="subtle">No dated additions yet.</div>'}
      </div>
    </div>
  `;

  renderChartToolbar(document.getElementById("home-chart-toolbar"), homeState, renderHome, { center: true });
  renderTimeWindowTabs(
    document.getElementById("most-played-tabs"),
    MOST_PLAYED_WINDOWS,
    MOST_PLAYED_WINDOW_ORDER,
    mostPlayedState.window,
    (key) => { mostPlayedState.window = key; renderHome(); },
    { center: true }
  );
  renderBarChart(
    document.getElementById("home-chart"),
    chartRows.map((r) => ({
      label: bucketTickLabel(homeState.granularity, r.bucket),
      tooltipLabel: humanBucketLabel(homeState.granularity, r.bucket),
      value: r.c,
      key: r.bucket,
    })),
    {
      color: "var(--accent-scrobble)",
      onClick: (d) => {
        // Drill into the Scrobbles browse page, pre-filtered to this bucket.
        scrobblesState.granularity = homeState.granularity;
        scrobblesState.periodFilter = { key: d.key, label: humanBucketLabel(homeState.granularity, d.key) };
        scrobblesState.page = 1;
        location.hash = "#/scrobbles";
      },
    }
  );
  app.querySelectorAll("img.vinyl-tile-cover").forEach((img) => attachCoverArt(img, img.dataset.albumId, img.dataset.coverStatus));
  app.querySelectorAll(".vinyl-tile[data-holding-id]").forEach((el) => {
    el.addEventListener("click", () => { location.hash = `#/vinyl/${el.dataset.holdingId}`; });
  });
}

// ---------------------------------------------------------------------
// Browse: Artists (#/artists) -- searchable, sortable, paginated
// ---------------------------------------------------------------------
const artistsState = { q: "", sort: "scrobbles", dir: "desc", page: 1 };

function renderArtistsBrowse() {
  const st = artistsState;
  const params = [];
  let where = "";
  if (st.q) {
    where = "WHERE ar.name LIKE ? COLLATE NOCASE";
    params.push(`%${st.q}%`);
  }

  const total = query(`SELECT count(*) AS c FROM artists ar ${where}`, params)[0].c;
  const pageSize = 50;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  st.page = Math.min(Math.max(1, st.page), totalPages);

  const sortCol = { name: "ar.name", scrobbles: "scrobbles", vinyl: "vinyl_count", shows: "shows" }[st.sort] || "scrobbles";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT ar.id, ar.name, ar.mbid,
      (SELECT count(*) FROM scrobbles WHERE artist_id = ar.id) AS scrobbles,
      (SELECT count(DISTINCT v.id) FROM vinyl_holdings v JOIN album_artists aa ON aa.album_id = v.album_id WHERE aa.artist_id = ar.id) AS vinyl_count,
      (SELECT count(*) FROM setlists WHERE artist_id = ar.id) AS shows
    FROM artists ar
    ${where}
    ORDER BY ${sortCol} ${dir}
    LIMIT ${pageSize} OFFSET ${(st.page - 1) * pageSize}
  `, params);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Artists</h1><div class="subtle">${total.toLocaleString()} artists</div></div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search artists…" value="${esc(st.q)}" />
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("name", "Artist", st)}
          ${sortHeader("scrobbles", "Scrobbles", st, true)}
          ${sortHeader("vinyl", "Vinyl", st, true)}
          ${sortHeader("shows", "Shows", st, true)}
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-id="${r.id}">
              <td class="row-title">${esc(r.name)}</td>
              <td class="num">${r.scrobbles.toLocaleString()}</td>
              <td class="num">${r.vinyl_count}</td>
              <td class="num">${r.shows}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
    ${paginationHtml(st.page, totalPages)}
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/artist/${tr.dataset.id}`; }));
  wireSortableHeaders(st, renderArtistsBrowse, ["name"]);
  wirePagination(st, totalPages, renderArtistsBrowse);
  wireSearchInput("browse-search", st, renderArtistsBrowse);
}

// ---------------------------------------------------------------------
// Browse: Shows attended (#/shows)
// ---------------------------------------------------------------------
const showsState = { q: "", sort: "event_date", dir: "desc", granularity: "all", periodFilter: null };

function renderShowsBrowse() {
  const st = showsState;
  const g = GRANULARITIES[st.granularity];

  const searchParams = [];
  let searchWhere = "";
  if (st.q) {
    searchWhere = "(ar.name LIKE ? COLLATE NOCASE OR ven.name LIKE ? COLLATE NOCASE OR ven.city LIKE ? COLLATE NOCASE)";
    searchParams.push(`%${st.q}%`, `%${st.q}%`, `%${st.q}%`);
  }

  const listWhereParts = searchWhere ? [searchWhere] : [];
  const listParams = [...searchParams];
  if (st.periodFilter) {
    listWhereParts.push(`strftime('${g.fmt}', sl.event_date) = ?`);
    listParams.push(st.periodFilter.key);
  }
  const where = listWhereParts.length ? `WHERE ${listWhereParts.join(" AND ")}` : "";
  const sortCol = { event_date: "sl.event_date", artist: "ar.name", venue: "ven.name" }[st.sort] || "sl.event_date";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT sl.id, sl.event_date, ar.id AS artist_id, ar.name AS artist_name,
           ven.name AS venue_name, ven.city, sl.tour_name
    FROM setlists sl
    JOIN artists ar ON ar.id = sl.artist_id
    LEFT JOIN venues ven ON ven.id = sl.venue_id
    ${where}
    ORDER BY ${sortCol} ${dir}
  `, listParams);

  const chartWhereParts = [];
  if (searchWhere) chartWhereParts.push(searchWhere);
  if (g.rangeModifier) chartWhereParts.push(`sl.event_date >= date('now', '${g.rangeModifier}')`);
  const chartWhere = chartWhereParts.length ? `WHERE ${chartWhereParts.join(" AND ")}` : "";
  const chartRows = query(`
    SELECT strftime('${g.fmt}', sl.event_date) AS bucket, count(*) AS c
    FROM setlists sl
    JOIN artists ar ON ar.id = sl.artist_id
    LEFT JOIN venues ven ON ven.id = sl.venue_id
    ${chartWhere}
    GROUP BY bucket ORDER BY bucket
  `, searchParams);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Shows attended</h1><div class="subtle">${rows.length.toLocaleString()} shows</div></div>
    <div class="section">
      <h2>Shows</h2>
      <div id="shows-chart-toolbar"></div>
      <div id="shows-chart"></div>
    </div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search artist or venue…" value="${esc(st.q)}" />
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("event_date", "Date", st)}
          ${sortHeader("artist", "Artist", st)}
          ${sortHeader("venue", "Venue", st)}
          <th>Tour</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-id="${r.id}">
              <td class="nowrap">${esc(r.event_date)}</td>
              <td class="row-title">${esc(r.artist_name)}</td>
              <td>${esc(r.venue_name || "")}${r.city ? `<div class="row-sub">${esc(r.city)}</div>` : ""}</td>
              <td>${esc(r.tour_name || "")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/setlist/${tr.dataset.id}`; }));
  wireSortableHeaders(st, renderShowsBrowse, ["artist", "venue"]);
  wireSearchInput("browse-search", st, renderShowsBrowse);

  renderChartToolbar(document.getElementById("shows-chart-toolbar"), st, renderShowsBrowse);
  renderBarChart(
    document.getElementById("shows-chart"),
    chartRows.map((r) => ({
      label: bucketTickLabel(st.granularity, r.bucket),
      tooltipLabel: humanBucketLabel(st.granularity, r.bucket),
      value: r.c,
      key: r.bucket,
    })),
    {
      color: "var(--accent-live)",
      selectedKey: st.periodFilter?.key,
      onClick: (d) => { toggleBucketFilter(st, st.granularity, d.key); renderShowsBrowse(); },
    }
  );
}

// ---------------------------------------------------------------------
// Browse: Scrobbles (#/scrobbles) -- the big one, paginated
// ---------------------------------------------------------------------
const scrobblesState = { q: "", sort: "played_at", dir: "desc", page: 1, granularity: "all", periodFilter: null };

function renderScrobblesBrowse() {
  const st = scrobblesState;
  const g = GRANULARITIES[st.granularity];

  // The search term scopes both the list AND the chart (so searching
  // "gojira" shows Gojira's activity, not the whole library's); the
  // period filter (a clicked bar) only scopes the list -- the chart
  // needs to keep showing every bucket so there's something to click.
  const searchParams = [];
  let searchWhere = "";
  if (st.q) {
    searchWhere = "(ar.name LIKE ? COLLATE NOCASE OR so.title LIKE ? COLLATE NOCASE)";
    searchParams.push(`%${st.q}%`, `%${st.q}%`);
  }

  const listWhereParts = searchWhere ? [searchWhere] : [];
  const listParams = [...searchParams];
  if (st.periodFilter) {
    listWhereParts.push(`strftime('${g.fmt}', s.played_at) = ?`);
    listParams.push(st.periodFilter.key);
  }
  const where = listWhereParts.length ? `WHERE ${listWhereParts.join(" AND ")}` : "";

  const total = query(`
    SELECT count(*) AS c FROM scrobbles s
    JOIN artists ar ON ar.id = s.artist_id
    JOIN songs so ON so.id = s.song_id
    ${where}
  `, listParams)[0].c;
  const pageSize = 50;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  st.page = Math.min(Math.max(1, st.page), totalPages);

  const sortCol = { played_at: "s.played_at", artist: "ar.name", track: "so.title" }[st.sort] || "s.played_at";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT s.played_at, ar.id AS artist_id, ar.name AS artist_name, so.id AS song_id, so.title AS track_title
    FROM scrobbles s
    JOIN artists ar ON ar.id = s.artist_id
    JOIN songs so ON so.id = s.song_id
    ${where}
    ORDER BY ${sortCol} ${dir}
    LIMIT ${pageSize} OFFSET ${(st.page - 1) * pageSize}
  `, listParams);

  const chartWhereParts = [];
  if (searchWhere) chartWhereParts.push(searchWhere);
  if (g.rangeModifier) chartWhereParts.push(`s.played_at >= datetime('now', '${g.rangeModifier}')`);
  const chartWhere = chartWhereParts.length ? `WHERE ${chartWhereParts.join(" AND ")}` : "";

  const chartRows = query(`
    SELECT strftime('${g.fmt}', s.played_at) AS bucket, count(*) AS c
    FROM scrobbles s
    JOIN artists ar ON ar.id = s.artist_id
    JOIN songs so ON so.id = s.song_id
    ${chartWhere}
    GROUP BY bucket ORDER BY bucket
  `, searchParams);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Scrobbles</h1><div class="subtle">${total.toLocaleString()} plays</div></div>
    <div class="section">
      <h2>Activity</h2>
      <div id="scrobbles-chart-toolbar"></div>
      <div id="scrobbles-chart"></div>
    </div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search artist or track…" value="${esc(st.q)}" />
      <div class="filter-count">${total.toLocaleString()} matching</div>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("played_at", "Played", st)}
          ${sortHeader("artist", "Artist", st)}
          ${sortHeader("track", "Track", st)}
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-artist-id="${r.artist_id}" data-song-id="${r.song_id}">
              <td>${esc(formatLocalDateTime(r.played_at))}</td>
              <td>${esc(r.artist_name)}</td>
              <td class="row-title">${esc(r.track_title)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
    ${paginationHtml(st.page, totalPages)}
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/song/${tr.dataset.songId}`; }));
  wireSortableHeaders(st, renderScrobblesBrowse, ["artist", "track"]);
  wirePagination(st, totalPages, renderScrobblesBrowse);
  wireSearchInput("browse-search", st, renderScrobblesBrowse);

  renderChartToolbar(document.getElementById("scrobbles-chart-toolbar"), st, renderScrobblesBrowse);
  renderBarChart(
    document.getElementById("scrobbles-chart"),
    chartRows.map((r) => ({
      label: bucketTickLabel(st.granularity, r.bucket),
      tooltipLabel: humanBucketLabel(st.granularity, r.bucket),
      value: r.c,
      key: r.bucket,
    })),
    {
      color: "var(--accent-scrobble)",
      selectedKey: st.periodFilter?.key,
      onClick: (d) => { toggleBucketFilter(st, st.granularity, d.key); st.page = 1; renderScrobblesBrowse(); },
    }
  );
}

// ---------------------------------------------------------------------
// Artist page's expandable "most played songs" panel: starts as just a
// "Show more" button under the top-10 bars; expanded, it's a searchable,
// paginated (20/page) table over every song scrobbled from this artist.
// ---------------------------------------------------------------------
const artistSongsState = { artistId: null, expanded: false, q: "", sort: "plays", dir: "desc", page: 1 };

function renderArtistSongsPanel() {
  const container = document.getElementById("artist-songs-panel");
  if (!container) return; // navigated away before this ran
  const st = artistSongsState;

  // The top-10 bars and the expanded searchable table show the same
  // information two different ways -- only one should be visible at once.
  const topBars = document.getElementById("artist-top-songs");
  if (topBars) topBars.hidden = st.expanded;

  if (!st.expanded) {
    container.innerHTML = `<button class="show-more-btn" id="show-more-songs">Show more ↓</button>`;
    document.getElementById("show-more-songs").addEventListener("click", () => {
      st.expanded = true;
      st.page = 1;
      renderArtistSongsPanel();
    });
    return;
  }

  const pageSize = 20;
  const params = [st.artistId];
  let where = "s.artist_id = ?";
  if (st.q) {
    where += " AND so.title LIKE ? COLLATE NOCASE";
    params.push(`%${st.q}%`);
  }

  const countRows = query(`
    SELECT so.id FROM scrobbles s JOIN songs so ON so.id = s.song_id
    WHERE ${where} GROUP BY so.id
  `, params);
  const total = countRows.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  st.page = Math.min(Math.max(1, st.page), totalPages);

  const sortCol = { title: "so.title", plays: "plays" }[st.sort] || "plays";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT so.id, so.title, count(*) AS plays
    FROM scrobbles s JOIN songs so ON so.id = s.song_id
    WHERE ${where}
    GROUP BY so.id
    ORDER BY ${sortCol} ${dir}
    LIMIT ${pageSize} OFFSET ${(st.page - 1) * pageSize}
  `, params);

  container.innerHTML = `
    <div class="filter-bar">
      <input type="text" id="artist-songs-search" placeholder="Search songs…" value="${esc(st.q)}" />
      ${sortToggle([["plays", "Plays"], ["title", "Title"]], st)}
      <div class="filter-count">${total.toLocaleString()} songs</div>
    </div>
    <div class="bar-list">${barRowsHtml(rows, st.maxPlays, "#/song/")}</div>
    ${paginationHtml(st.page, totalPages)}
    <button class="show-less-btn" id="show-less-songs">Show less ↑</button>
  `;

  wireSortableHeaders(st, renderArtistSongsPanel, ["title"], container);
  wirePagination(st, totalPages, renderArtistSongsPanel, container);
  wireSearchInput("artist-songs-search", st, renderArtistSongsPanel);
  document.getElementById("show-less-songs").addEventListener("click", () => {
    st.expanded = false;
    renderArtistSongsPanel();
  });
}

// Artist page's expandable "digital listening" panel -- same shape as
// renderArtistSongsPanel above, grouped by album instead of song. Existing
// as its own list (not just the top-10 bars) matters here specifically:
// a "Deluxe Edition" duplicate splitting off a handful of scrobbles would
// otherwise fall below the top 10 and stay invisible.
const artistAlbumsState = { artistId: null, expanded: false, q: "", sort: "plays", dir: "desc", page: 1 };

function renderArtistAlbumsPanel() {
  const container = document.getElementById("artist-albums-panel");
  if (!container) return; // navigated away before this ran
  const st = artistAlbumsState;

  const topBars = document.getElementById("artist-top-albums");
  if (topBars) topBars.hidden = st.expanded;

  if (!st.expanded) {
    container.innerHTML = `<button class="show-more-btn" id="show-more-albums">Show more ↓</button>`;
    document.getElementById("show-more-albums").addEventListener("click", () => {
      st.expanded = true;
      st.page = 1;
      renderArtistAlbumsPanel();
    });
    return;
  }

  const pageSize = 20;
  const params = [st.artistId];
  let where = "s.artist_id = ?";
  if (st.q) {
    where += " AND al.title LIKE ? COLLATE NOCASE";
    params.push(`%${st.q}%`);
  }

  const countRows = query(`
    SELECT al.id FROM scrobbles s JOIN albums al ON al.id = s.album_id
    WHERE ${where} GROUP BY al.id
  `, params);
  const total = countRows.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  st.page = Math.min(Math.max(1, st.page), totalPages);

  const sortCol = { title: "al.title", plays: "plays" }[st.sort] || "plays";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT al.id, al.title, count(*) AS plays
    FROM scrobbles s JOIN albums al ON al.id = s.album_id
    WHERE ${where}
    GROUP BY al.id
    ORDER BY ${sortCol} ${dir}
    LIMIT ${pageSize} OFFSET ${(st.page - 1) * pageSize}
  `, params);

  container.innerHTML = `
    <div class="filter-bar">
      <input type="text" id="artist-albums-search" placeholder="Search albums…" value="${esc(st.q)}" />
      ${sortToggle([["plays", "Plays"], ["title", "Title"]], st)}
      <div class="filter-count">${total.toLocaleString()} albums</div>
    </div>
    <div class="bar-list">${barRowsHtml(rows, st.maxPlays, "#/album/")}</div>
    ${paginationHtml(st.page, totalPages)}
    <button class="show-less-btn" id="show-less-albums">Show less ↑</button>
  `;

  wireSortableHeaders(st, renderArtistAlbumsPanel, ["title"], container);
  wirePagination(st, totalPages, renderArtistAlbumsPanel, container);
  wireSearchInput("artist-albums-search", st, renderArtistAlbumsPanel);
  document.getElementById("show-less-albums").addEventListener("click", () => {
    st.expanded = false;
    renderArtistAlbumsPanel();
  });
}

// ---------------------------------------------------------------------
// Artist: the hub page -- shows attended, vinyl owned, top songs, top
// albums (the last two "all media": scrobbles regardless of format)
// ---------------------------------------------------------------------
function renderArtist(id) {
  const artist = query(`SELECT * FROM artists WHERE id = ?`, [id])[0];
  if (!artist) return renderNotFound("Artist");

  // Kick off the slowest thing on this page -- the Wikipedia/MusicBrainz
  // bio lookup -- before anything else, not after. Everything below this
  // (vinyl/scrobble/song/setlist counts) is a synchronous, local,
  // sub-millisecond sql.js query; the enrichment fetch is a real network
  // round trip, and it's also the first thing shown on the page, so it
  // should be the first thing requested, not the last.
  const token = renderToken;
  const enrichmentPromise = getArtistEnrichment(artist);

  const vinylCount = query(`
    SELECT count(*) AS c FROM vinyl_holdings v
    JOIN album_artists aa ON aa.album_id = v.album_id
    WHERE aa.artist_id = ?
  `, [id])[0].c;

  const scrobbleCount = query(`SELECT count(*) AS c FROM scrobbles WHERE artist_id = ?`, [id])[0].c;
  const liveCount = query(`SELECT count(*) AS c FROM setlists WHERE artist_id = ?`, [id])[0].c;

  const topSongs = query(`
    SELECT so.id, so.title, count(*) AS plays
    FROM scrobbles s JOIN songs so ON so.id = s.song_id
    WHERE s.artist_id = ?
    GROUP BY so.id ORDER BY plays DESC LIMIT 10
  `, [id]);
  const maxPlays = topSongs.length ? topSongs[0].plays : 1;

  const topAlbums = query(`
    SELECT al.id, al.title, count(*) AS plays
    FROM scrobbles s JOIN albums al ON al.id = s.album_id
    WHERE s.artist_id = ?
    GROUP BY al.id ORDER BY plays DESC LIMIT 5
  `, [id]);
  const maxAlbumPlays = topAlbums.length ? topAlbums[0].plays : 1;

  const vinylRows = query(`
    SELECT DISTINCT al.id AS album_id, al.cover_status, al.title, al.year, v.format, v.media_condition
    FROM vinyl_holdings v
    JOIN album_artists aa ON aa.album_id = v.album_id
    JOIN albums al ON al.id = v.album_id
    WHERE aa.artist_id = ?
    ORDER BY al.year
  `, [id]);

  const setlistRows = query(`
    SELECT sl.id, sl.event_date, ven.name AS venue_name, ven.city, sl.tour_name
    FROM setlists sl LEFT JOIN venues ven ON ven.id = sl.venue_id
    WHERE sl.artist_id = ?
    ORDER BY sl.event_date DESC
  `, [id]);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="artist-header">
      <h1>${esc(artist.name)}${artist.mbid ? '<span class="artist-mbid-badge" title="Matched via MusicBrainz">MBID</span>' : ""}</h1>
    </div>
    <div class="badge-row">
      <div class="badge vinyl${vinylCount ? "" : " disabled"}" id="badge-vinyl">${vinylCount} on vinyl</div>
      <div class="badge scrobble${scrobbleCount ? "" : " disabled"}" id="badge-scrobble">${scrobbleCount.toLocaleString()} scrobbles</div>
      <div class="badge live${liveCount ? "" : " disabled"}" id="badge-live">Seen live ${liveCount}×</div>
    </div>
    ${genreTagsHtml(artistGenreNames(id), "From their albums")}

    <div id="about-panel"></div>

    <div class="section">
      <h2>Shows attended</h2>
      ${setlistRows.length ? setlistRows.map((sl) => `
        <div class="list-item" onclick="location.hash='#/setlist/${sl.id}'">
          <div>
            <div class="list-title">${esc(sl.venue_name || "Unknown venue")}</div>
            <div class="list-sub">${esc(sl.city || "")}${sl.tour_name ? " · " + esc(sl.tour_name) : ""}</div>
          </div>
          <div class="list-right">${esc(sl.event_date)}</div>
        </div>
      `).join("") : `<p class="subtle">Not yet seen</p>`}
    </div>

    <div class="section">
      <h2>On the shelf</h2>
      ${vinylRows.length ? vinylRows.map((v) => `
        <div class="vinyl-card" data-album-id="${v.album_id}" style="cursor:pointer">
          <img class="cover-thumb" data-album-id="${v.album_id}" data-cover-status="${v.cover_status || ""}" alt="" loading="lazy" />
          <div class="vinyl-body">
            <div class="title">${esc(v.title)}${v.year ? ` <span class="subtle">(${v.year})</span>` : ""}</div>
            <div class="meta">${esc(v.format || "")}${v.media_condition ? " · " + esc(v.media_condition) : ""}</div>
          </div>
        </div>
      `).join("") : `<p class="subtle">No records owned yet</p>`}
    </div>

    ${topSongs.length ? `
      <div class="section">
        <h2>Top songs (all media)</h2>
        <div id="artist-top-songs">${barRowsHtml(topSongs, maxPlays, "#/song/")}</div>
        <div id="artist-songs-panel"></div>
      </div>
    ` : ""}

    ${topAlbums.length ? `
      <div class="section">
        <h2>Top albums (all media)</h2>
        <div id="artist-top-albums">${barRowsHtml(topAlbums, maxAlbumPlays, "#/album/")}</div>
        <div id="artist-albums-panel"></div>
      </div>
    ` : ""}
  `;

  app.querySelectorAll("img.cover-thumb").forEach((img) => attachCoverArt(img, img.dataset.albumId, img.dataset.coverStatus));
  app.querySelectorAll(".vinyl-card[data-album-id]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/album/${el.dataset.albumId}`; }));

  // Badges jump to the matching browse page, pre-filtered to this artist
  // (a badge for a count of zero is inert -- nothing to filter down to).
  if (vinylCount) {
    document.getElementById("badge-vinyl").addEventListener("click", () => {
      location.hash = `#/vinyl?q=${encodeURIComponent(artist.name)}`;
    });
  }
  if (scrobbleCount) {
    document.getElementById("badge-scrobble").addEventListener("click", () => {
      scrobblesState.q = artist.name; scrobblesState.periodFilter = null; scrobblesState.page = 1;
      location.hash = "#/scrobbles";
    });
  }
  if (liveCount) {
    document.getElementById("badge-live").addEventListener("click", () => {
      showsState.q = artist.name; showsState.periodFilter = null;
      location.hash = "#/shows";
    });
  }

  if (topSongs.length) {
    artistSongsState.artistId = id;
    artistSongsState.expanded = false;
    artistSongsState.q = "";
    artistSongsState.sort = "plays";
    artistSongsState.dir = "desc";
    artistSongsState.page = 1;
    // Bar widths in the expanded list stay relative to this artist's #1
    // most-played song, same reference the top-10 preview uses -- so a
    // page 2 entry's bar means the same thing as a top-10 entry's, rather
    // than rescaling to whatever's biggest on the current page/search.
    artistSongsState.maxPlays = maxPlays;
    renderArtistSongsPanel();
  }

  if (topAlbums.length) {
    artistAlbumsState.artistId = id;
    artistAlbumsState.expanded = false;
    artistAlbumsState.q = "";
    artistAlbumsState.sort = "plays";
    artistAlbumsState.dir = "desc";
    artistAlbumsState.page = 1;
    artistAlbumsState.maxPlays = maxAlbumPlays;
    renderArtistAlbumsPanel();
  }

  // The fetch itself was already kicked off at the top of this function;
  // this just wires its result up once both it and the DOM below are
  // ready. `token` (captured up top, before any of the SQL queries) still
  // guards against writing into a page the user has since navigated away
  // from.
  const aboutPanel = document.getElementById("about-panel");
  aboutPanel.innerHTML = '<div class="about-skeleton">Loading more about this artist…</div>';
  enrichmentPromise.then((info) => {
    if (token !== renderToken) return;
    if (!info.extract && !info.tags.length) {
      aboutPanel.innerHTML = "";
      return;
    }
    aboutPanel.innerHTML = `
      <div class="about-panel">
        ${info.thumbnail ? `<img class="about-thumb" src="${esc(info.thumbnail)}" alt="" />` : ""}
        <div>
          ${info.extract ? `<div class="about-text">${esc(info.extract)}</div>` : ""}
          ${info.tags.length ? `<div class="genre-pills">${info.tags.map((t) => `<span class="genre-pill">${esc(t)}</span>`).join("")}</div>` : ""}
          ${info.pageUrl ? `<div class="about-source"><a href="${esc(info.pageUrl)}" target="_blank" rel="noopener">Wikipedia ↗</a></div>` : ""}
        </div>
      </div>
    `;
  });
}

// ---------------------------------------------------------------------
// Song: the "rabbit hole" page -- live count, scrobble count, vinyl status
// ---------------------------------------------------------------------
function renderSong(id) {
  const song = query(`
    SELECT so.*, ar.name AS artist_name
    FROM songs so JOIN artists ar ON ar.id = so.artist_id
    WHERE so.id = ?
  `, [id])[0];
  if (!song) return renderNotFound("Song");

  const scrobbleCount = query(`SELECT count(*) AS c FROM scrobbles WHERE song_id = ?`, [id])[0].c;

  const liveRows = query(`
    SELECT ss.is_cover, ss.cover_of_artist_text, sl.id AS setlist_id, sl.event_date,
           ven.name AS venue_name, ven.city, perf.name AS performer_name
    FROM setlist_songs ss
    JOIN setlists sl ON sl.id = ss.setlist_id
    JOIN artists perf ON perf.id = sl.artist_id
    LEFT JOIN venues ven ON ven.id = sl.venue_id
    WHERE ss.song_id = ?
    ORDER BY sl.event_date DESC
  `, [id]);

  const onVinyl = song.album_id
    ? query(`SELECT 1 FROM vinyl_holdings WHERE album_id = ? LIMIT 1`, [song.album_id]).length > 0
    : false;

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <h1>${esc(song.title)}</h1>
    <div class="subtle">${esc(song.artist_name)}</div>

    <div class="badge-row">
      <div class="badge vinyl${onVinyl ? "" : " disabled"}" id="badge-vinyl">${onVinyl ? "Owned on vinyl" : "Not on vinyl"}</div>
      <div class="badge scrobble${scrobbleCount ? "" : " disabled"}" id="badge-scrobble">${scrobbleCount.toLocaleString()} scrobbles</div>
      <div class="badge live${liveRows.length ? "" : " disabled"}" id="badge-live">Heard live ${liveRows.length}×</div>
    </div>

    ${liveRows.length ? `
      <div class="section" id="song-live-section">
        <h2>Live performances</h2>
        ${liveRows.map((r) => `
          <div class="list-item" onclick="location.hash='#/setlist/${r.setlist_id}'">
            <div>
              <div class="list-title">${esc(r.venue_name || "Unknown venue")}</div>
              <div class="list-sub">
                ${esc(r.city || "")}
                ${r.is_cover ? ` · cover, originally ${esc(r.cover_of_artist_text || "")}` : ""}
                ${r.performer_name !== song.artist_name ? ` · performed by ${esc(r.performer_name)}` : ""}
              </div>
            </div>
            <div class="list-right">${esc(r.event_date)}</div>
          </div>
        `).join("")}
      </div>
    ` : '<div class="section subtle">Never heard live (yet).</div>'}
  `;

  // Vinyl and scrobbles jump to their respective browse page, pre-filtered
  // to this song; "heard live" scrolls to the performance list already on
  // this page, since there's no separate per-song page for that. A badge
  // at zero is inert -- there's nothing to filter down to or scroll to.
  if (onVinyl) {
    document.getElementById("badge-vinyl").addEventListener("click", () => { location.hash = `#/album/${song.album_id}`; });
  }
  if (scrobbleCount) {
    document.getElementById("badge-scrobble").addEventListener("click", () => {
      scrobblesState.q = song.title; scrobblesState.periodFilter = null; scrobblesState.page = 1;
      location.hash = "#/scrobbles";
    });
  }
  if (liveRows.length) {
    document.getElementById("badge-live").addEventListener("click", () => {
      document.getElementById("song-live-section").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
}

// ---------------------------------------------------------------------
// Setlist: full show detail
// ---------------------------------------------------------------------
function renderSetlist(id) {
  const setlist = query(`
    SELECT sl.*, ar.name AS artist_name, ven.id AS venue_id, ven.name AS venue_name, ven.city, ven.country
    FROM setlists sl
    JOIN artists ar ON ar.id = sl.artist_id
    LEFT JOIN venues ven ON ven.id = sl.venue_id
    WHERE sl.id = ?
  `, [id])[0];
  if (!setlist) return renderNotFound("Setlist");

  const songs = query(`
    SELECT ss.*, so.title
    FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id
    WHERE ss.setlist_id = ?
    ORDER BY ss.position
  `, [id]);

  let lastSetName = Symbol("unset");
  const songsHtml = songs.map((s) => {
    let header = "";
    if (s.set_name !== lastSetName) {
      header = `<div class="set-name-label">${esc(s.set_name || "Set")}</div>`;
      lastSetName = s.set_name;
    }
    return `
      ${header}
      <div class="setlist-song-row" onclick="location.hash='#/song/${s.song_id}'">
        <div class="pos">${s.position}.</div>
        <div>${esc(s.title)} ${s.is_cover ? `<span class="cover-tag">cover of ${esc(s.cover_of_artist_text || "")}</span>` : ""}</div>
      </div>
    `;
  }).join("");

  const venueLabel = esc(setlist.venue_name || "Unknown venue");
  const venueHtml = setlist.venue_id
    ? `<span class="link-text" onclick="location.hash='#/venue/${setlist.venue_id}'">${venueLabel}</span>`
    : venueLabel;

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <h1><span class="link-text" onclick="location.hash='#/artist/${setlist.artist_id}'">${esc(setlist.artist_name)}</span></h1>
    <div class="subtle">
      ${esc(setlist.event_date)} · ${venueHtml}${setlist.city ? ", " + esc(setlist.city) : ""}
      ${setlist.tour_name ? " · " + esc(setlist.tour_name) : ""}
    </div>
    <div class="setlist-songs">${songsHtml || '<div class="subtle">No songs recorded for this setlist.</div>'}</div>
  `;
}

// ---------------------------------------------------------------------
// Venue: every show attended there
// ---------------------------------------------------------------------
function renderVenue(id) {
  const venue = query(`SELECT * FROM venues WHERE id = ?`, [id])[0];
  if (!venue) return renderNotFound("Venue");

  const shows = query(`
    SELECT sl.id, sl.event_date, ar.id AS artist_id, ar.name AS artist_name, sl.tour_name
    FROM setlists sl JOIN artists ar ON ar.id = sl.artist_id
    WHERE sl.venue_id = ?
    ORDER BY sl.event_date DESC
  `, [id]);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <h1>${esc(venue.name)}</h1>
    <div class="subtle">${[venue.city, venue.country].filter(Boolean).map(esc).join(", ")}</div>

    <div class="badge-row">
      <div class="badge live">${shows.length} show${shows.length === 1 ? "" : "s"} attended</div>
    </div>

    <div class="section">
      <h2>Shows</h2>
      ${shows.map((sl) => `
        <div class="list-item" onclick="location.hash='#/setlist/${sl.id}'">
          <div>
            <div class="list-title">${esc(sl.artist_name)}</div>
            <div class="list-sub">${esc(sl.tour_name || "")}</div>
          </div>
          <div class="list-right">${esc(sl.event_date)}</div>
        </div>
      `).join("") || '<div class="subtle">No shows recorded.</div>'}
    </div>
  `;
}

// ---------------------------------------------------------------------
// Album: the release detail a vinyl entry links to -- every physical
// copy owned (pressings/variants can differ -- catalog#, color, condition)
// plus whatever tracks we know from that album, with cover art.
// ---------------------------------------------------------------------
function renderAlbum(id) {
  const album = query(`
    SELECT al.*, ar.name AS artist_name, ar.id AS artist_id
    FROM albums al JOIN artists ar ON ar.id = al.artist_id
    WHERE al.id = ?
  `, [id])[0];
  if (!album) return renderNotFound("Album");

  const holdings = query(`SELECT * FROM vinyl_holdings WHERE album_id = ? ORDER BY date_added`, [id]);

  const tracks = query(`
    SELECT so.id, so.title, (SELECT count(*) FROM scrobbles WHERE song_id = so.id) AS plays
    FROM songs so WHERE so.album_id = ?
    ORDER BY plays DESC
  `, [id]);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="album-header">
      <img class="album-cover-large" data-album-id="${album.id}" data-cover-status="${album.cover_status || ""}" alt="" />
      <div>
        <h1>${esc(album.title)}</h1>
        <div class="subtle"><span class="link-text" onclick="location.hash='#/artist/${album.artist_id}'">${esc(album.artist_name)}</span>${album.year ? ` · ${album.year}` : ""}</div>
        ${genreTagsHtml(albumGenreNames(id))}
      </div>
    </div>

    <div class="section">
      <h2>${holdings.length > 1 ? "Your copies" : "Your copy"}</h2>
      ${loadCollection().filter((r) => r.album_id === id).map((r) => `
        <button class="pressing-card" data-holding="${r.id}">
          <div class="rd-line"><b>${esc(r.label || "Unknown label")}</b>${r.catalog_number ? ` · <span class="mono">${esc(r.catalog_number)}</span>` : ""}${r.country ? ` · ${esc(r.country)}` : ""}${r.pressing_year ? ` · ${r.pressing_year}` : ""} ${starsHtml(r.rating)}</div>
          <div class="pbadges">${pressingBadges(r)}</div>
          ${meter(r.grade, "Record")}${meter(r.sleeveGrade, "Sleeve")}
          ${r.notes ? `<div class="subtle pc-note">“${esc(r.notes)}”</div>` : ""}
          <div class="subtle">Added ${esc((r.date_added || "").slice(0, 10))} · open in the collection →</div>
        </button>
      `).join("") || '<div class="subtle">No holding details recorded.</div>'}
    </div>

    ${tracks.length ? `
      <div class="section">
        <h2>Tracks</h2>
        ${tracks.map((t) => `
          <div class="list-item" data-song-id="${t.id}">
            <div class="list-title">${esc(t.title)}</div>
            <div class="list-right">${t.plays.toLocaleString()} plays</div>
          </div>
        `).join("")}
      </div>
    ` : ""}
  `;

  const img = app.querySelector("img.album-cover-large");
  if (img) attachCoverArt(img, album.id, album.cover_status);
  app.querySelectorAll("[data-holding]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/vinyl/${el.dataset.holding}`; }));
  app.querySelectorAll("[data-song-id]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.songId}`; }));
}

// ---------------------------------------------------------------------
// Browse: Songs (#/songs) -- one row per song, not per play
// ---------------------------------------------------------------------
const songsState = { q: "", sort: "plays", dir: "desc", page: 1 };

function renderSongsBrowse() {
  const st = songsState;
  const params = [];
  let where = "";
  if (st.q) {
    where = "WHERE (so.title LIKE ? COLLATE NOCASE OR ar.name LIKE ? COLLATE NOCASE)";
    params.push(`%${st.q}%`, `%${st.q}%`);
  }

  const total = query(`SELECT count(*) AS c FROM songs so JOIN artists ar ON ar.id = so.artist_id ${where}`, params)[0].c;
  const pageSize = 50;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  st.page = Math.min(Math.max(1, st.page), totalPages);

  const sortCol = { title: "so.title", artist: "ar.name", plays: "plays" }[st.sort] || "plays";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT so.id, so.title, ar.name AS artist_name,
      (SELECT count(*) FROM scrobbles WHERE song_id = so.id) AS plays
    FROM songs so JOIN artists ar ON ar.id = so.artist_id
    ${where}
    ORDER BY ${sortCol} ${dir}
    LIMIT ${pageSize} OFFSET ${(st.page - 1) * pageSize}
  `, params);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Songs</h1><div class="subtle">${total.toLocaleString()} songs</div></div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search track or artist…" value="${esc(st.q)}" />
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("artist", "Artist", st)}
          ${sortHeader("title", "Track", st)}
          ${sortHeader("plays", "Played", st, true)}
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-id="${r.id}">
              <td>${esc(r.artist_name)}</td>
              <td class="row-title">${esc(r.title)}</td>
              <td class="num">${r.plays.toLocaleString()}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
    ${paginationHtml(st.page, totalPages)}
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/song/${tr.dataset.id}`; }));
  wireSortableHeaders(st, renderSongsBrowse, ["title", "artist"]);
  wirePagination(st, totalPages, renderSongsBrowse);
  wireSearchInput("browse-search", st, renderSongsBrowse);
}

// ---------------------------------------------------------------------
// Browse: Venues (#/venues) -- one row per venue
// ---------------------------------------------------------------------
const venuesState = { q: "", sort: "visits", dir: "desc" };

function renderVenuesBrowse() {
  const st = venuesState;
  const params = [];
  let where = "";
  if (st.q) {
    where = "WHERE (v.name LIKE ? COLLATE NOCASE OR v.city LIKE ? COLLATE NOCASE)";
    params.push(`%${st.q}%`, `%${st.q}%`);
  }
  const sortCol = { name: "v.name", city: "v.city", visits: "visits", last_visited: "last_visited" }[st.sort] || "visits";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT v.id, v.name, v.city, v.country, count(sl.id) AS visits, max(sl.event_date) AS last_visited
    FROM venues v LEFT JOIN setlists sl ON sl.venue_id = v.id
    ${where}
    GROUP BY v.id
    ORDER BY ${sortCol} ${dir} NULLS LAST
  `, params);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Venues</h1><div class="subtle">${rows.length.toLocaleString()} venues</div></div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search venue or city…" value="${esc(st.q)}" />
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("name", "Venue", st)}
          ${sortHeader("city", "City", st)}
          ${sortHeader("visits", "Visits", st, true)}
          ${sortHeader("last_visited", "Last visited", st)}
        </tr></thead>
        <tbody>
          ${rows.map((r, i) => `
            <tr data-index="${i}">
              <td class="row-title">${esc(r.name)}</td>
              <td>${esc(r.city || "")}${r.country ? `<div class="row-sub">${esc(r.country)}</div>` : ""}</td>
              <td class="num">${r.visits}</td>
              <td class="nowrap">${esc(r.last_visited || "")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      const venue = rows[Number(tr.dataset.index)];
      location.hash = `#/venue/${venue.id}`;
    });
  });
  wireSortableHeaders(st, renderVenuesBrowse, ["name", "city"]);
  wireSearchInput("browse-search", st, renderVenuesBrowse);
}

// ---------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------
// Tracks the last hash actually rendered, so render() can tell a real
// navigation (fresh page, hash changed) from a same-route re-render
// (the theme toggle calls render() to redraw charts in the new colors,
// without the hash changing) -- only the former should reset scroll.
let lastRenderedHash = null;

function render() {
  renderToken += 1;
  const hash = location.hash || "#/";
  if (hash !== lastRenderedHash) window.scrollTo(0, 0);
  lastRenderedHash = hash;
  const artistMatch = hash.match(/^#\/artist\/(\d+)/);
  const songMatch = hash.match(/^#\/song\/(\d+)/);
  const setlistMatch = hash.match(/^#\/setlist\/(\d+)/);
  const albumMatch = hash.match(/^#\/album\/(\d+)/);
  const venueMatch = hash.match(/^#\/venue\/(\d+)/);
  const recordMatch = hash.match(/^#\/vinyl\/(\d+)/);
  app.classList.remove("wide");
  if (!recordMatch && typeof closeRecord === "function") closeRecord(false); // leaving the collection: no drawer left behind
  if (artistMatch) return renderArtist(Number(artistMatch[1]));
  if (songMatch) return renderSong(Number(songMatch[1]));
  if (setlistMatch) return renderSetlist(Number(setlistMatch[1]));
  if (albumMatch) return renderAlbum(Number(albumMatch[1]));
  if (venueMatch) return renderVenue(Number(venueMatch[1]));
  if (hash.startsWith("#/artists")) return renderArtistsBrowse();
  if (recordMatch) return renderCollection(Number(recordMatch[1]));
  if (hash.startsWith("#/vinyl")) return renderCollection();
  if (hash.startsWith("#/shows")) return renderShowsBrowse();
  if (hash.startsWith("#/scrobbles")) return renderScrobblesBrowse();
  if (hash.startsWith("#/songs")) return renderSongsBrowse();
  if (hash.startsWith("#/venues")) return renderVenuesBrowse();
  return renderHome();
}

// ---------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------
/** Bottom tab bar for phones (CSS-gated to small viewports, see style.css) -- reflects the
 * current hash route via aria-current, and the Search tab just focuses the existing topbar
 * search input rather than duplicating search UI. */
function setupTabbar() {
  const bar = document.querySelector(".tabbar");
  if (!bar) return;
  bar.hidden = false;
  document.getElementById("tabbar-search").addEventListener("click", (e) => {
    e.preventDefault();
    document.getElementById("search-input").focus();
    document.getElementById("search-input").scrollIntoView({ behavior: "smooth" });
  });
  function reflectRoute() {
    const hash = location.hash || "#/";
    const route = hash === "#/" ? "home" : hash.startsWith("#/vinyl") ? "vinyl" : hash.startsWith("#/shows") ? "shows" : null;
    bar.querySelectorAll("a[data-route]").forEach((a) => {
      if (a.dataset.route === route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
  }
  window.addEventListener("hashchange", reflectRoute);
  reflectRoute();
}

function setupSearch() {
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");

  input.addEventListener("input", () => {
    const term = input.value.trim();
    if (!term) {
      results.hidden = true;
      return;
    }
    const rows = query(`
      SELECT ar.id, ar.name,
        (SELECT count(*) FROM scrobbles WHERE artist_id = ar.id) AS scrobbles,
        (SELECT count(*) FROM setlists WHERE artist_id = ar.id) AS shows
      FROM artists ar
      WHERE ar.name LIKE ? COLLATE NOCASE
      ORDER BY scrobbles DESC LIMIT 15
    `, [`%${term}%`]);

    results.innerHTML = rows.length
      ? rows.map((r) => `
          <div class="search-result-row" data-id="${r.id}">
            <span>${esc(r.name)}</span>
            <span class="meta">${r.scrobbles.toLocaleString()} plays · ${r.shows} shows</span>
          </div>
        `).join("")
      : '<div class="search-result-row"><span class="meta">No matches</span></div>';
    results.hidden = false;
  });

  results.addEventListener("click", (e) => {
    const row = e.target.closest(".search-result-row");
    if (row && row.dataset.id) {
      location.hash = `#/artist/${row.dataset.id}`;
      results.hidden = true;
      input.value = "";
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) results.hidden = true;
  });
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
/** Fetches a URL, reporting real byte-level progress via onProgress(loaded,
 * total) as chunks arrive -- total is 0 if the server didn't send
 * Content-Length (falls back to an indeterminate bar). Falls back to a
 * plain, non-streaming fetch if the runtime doesn't support readable
 * response streams at all. */
async function fetchWithProgress(url, onProgress, totalPromise) {
  // Fired alongside the fetch itself (not awaited first) so the tiny
  // sidecar lookup never delays starting the real download.
  const [resp, knownTotal] = await Promise.all([fetch(url), totalPromise]);
  if (!resp.ok) throw new Error(`Fetching database failed: HTTP ${resp.status}`);

  if (!resp.body || !resp.body.getReader) {
    const buffer = await resp.arrayBuffer();
    onProgress(buffer.byteLength, buffer.byteLength);
    return buffer;
  }

  // Prefer the known-good total (from a sidecar file written at build
  // time) over the response's own Content-Length. A static host that
  // gzips this file on the wire -- GitHub Pages does, since it's a
  // large, very compressible file -- reports the *compressed* size in
  // Content-Length, while the bytes actually handed to the reader below
  // are already decompressed. Comparing decompressed bytes-so-far
  // against a compressed total means the percentage (and the "X MB / Y
  // MB" figure) never reflects the real download -- which is exactly
  // the bug this sidesteps entirely.
  const total = knownTotal || Number(resp.headers.get("content-length")) || 0;
  const reader = resp.body.getReader();
  const chunks = [];
  let loaded = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    onProgress(loaded, total);
  }

  const buffer = new Uint8Array(loaded);
  let offset = 0;
  for (const chunk of chunks) {
    buffer.set(chunk, offset);
    offset += chunk.length;
  }
  return buffer.buffer;
}

function updateLoadingProgress(loaded, total) {
  const fill = document.getElementById("progress-fill");
  const text = document.getElementById("progress-text");
  if (!fill || !text) return;

  const loadedMB = (loaded / 1e6).toFixed(1);
  if (total > 0) {
    const pct = Math.min(100, Math.round((loaded / total) * 100));
    fill.classList.remove("indeterminate");
    fill.style.width = `${pct}%`;
    text.textContent = `${pct}% · ${loadedMB} MB / ${(total / 1e6).toFixed(1)} MB`;
  } else {
    fill.classList.add("indeterminate");
    text.textContent = `${loadedMB} MB loaded…`;
  }
}

const CITADEL_ORIGIN = "http://192.168.0.66:8080";

/** True when this page is running inside Citadel's #music iframe rather than opened
 * directly in its own tab -- drives whether the local theme toggle is shown at all. */
function isEmbedded() {
  return window.self !== window.top;
}

/** Embedded: theme is driven entirely by Citadel's own toggle (initial value via a
 * ?theme= query param, live changes via postMessage) -- the local toggle button is
 * removed outright so it can never be clicked and fight the parent. Standalone: the
 * existing self-contained initTheme() (localStorage + matchMedia fallback) is untouched. */
function initEmbeddedTheme() {
  document.getElementById("theme-toggle")?.remove();
  const initial = new URLSearchParams(location.search).get("theme");
  if (initial === "light" || initial === "dark") applyTheme(initial);
  window.addEventListener("message", (event) => {
    if (event.origin !== CITADEL_ORIGIN) return;
    if (event.data?.type === "citadel-theme" && (event.data.theme === "light" || event.data.theme === "dark")) {
      applyTheme(event.data.theme);
      if (db) render(); // db not loaded yet (e.g. message arrives mid-boot) -- nothing on screen to redraw
    }
  });
}

async function boot() {
  if (isEmbedded()) initEmbeddedTheme(); else initTheme();
  try {
    // Independent downloads -- run them side by side rather than making
    // the (much bigger) database wait behind the small WASM runtime.
    const sqlJsPromise = initSqlJs({
      locateFile: (file) => `https://cdn.jsdelivr.net/npm/sql.js@1.10.3/dist/${file}`,
    });
    // Real, uncompressed byte count written by etl/build_public_db.py --
    // see the comment in fetchWithProgress for why Content-Length alone
    // can't be trusted for this. Missing/failed fetch just means the
    // progress bar falls back to whatever Content-Length says.
    const sizePromise = fetch("public/music.sqlite.size")
      .then((r) => (r.ok ? r.text() : "0"))
      .then((t) => Number(t.trim()) || 0)
      .catch(() => 0);
    const bufferPromise = fetchWithProgress("public/music.sqlite", updateLoadingProgress, sizePromise);

    const [SQL, buffer] = await Promise.all([sqlJsPromise, bufferPromise]);

    const progressText = document.getElementById("progress-text");
    if (progressText) progressText.textContent = "Opening database…";

    db = new SQL.Database(new Uint8Array(buffer));

    document.getElementById("footer-status").textContent =
      `Database loaded (${(buffer.byteLength / 1e6).toFixed(1)} MB), queried entirely in your browser.`;

    startApp();
  } catch (err) {
    app.innerHTML = `<div class="error-box">Couldn't load the database.<br><span class="subtle">${esc(err.message)}</span></div>`;
    console.error(err);
  }
}

/** Wires up the app and shows the page the URL asks for (home by default). */
function startApp() {
  setupSearch();
  setupTabbar();
  window.addEventListener("hashchange", render);
  render();
}

boot();
