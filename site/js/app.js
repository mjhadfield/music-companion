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
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const current = document.documentElement.dataset.theme || (prefersDark ? "dark" : "light");
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

function wirePagination(state, totalPages, rerender) {
  const prev = app.querySelector('.pagination button[data-action="prev"]');
  const next = app.querySelector('.pagination button[data-action="next"]');
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

function wireSortableHeaders(state, rerender, ascByDefault = []) {
  app.querySelectorAll("th[data-sort]").forEach((th) => {
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
      (SELECT count(*) FROM venues) AS venues
  `)[0];

  const mostPlayedWindow = MOST_PLAYED_WINDOWS[mostPlayedState.window];
  const topArtists = query(`
    SELECT ar.id, ar.name, count(*) AS plays
    FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id
    ${mostPlayedWindow.rangeModifier ? `WHERE s.played_at >= datetime('now', '${mostPlayedWindow.rangeModifier}')` : ""}
    GROUP BY ar.id ORDER BY plays DESC LIMIT 10
  `);

  const recentVinyl = query(`
    SELECT al.id AS album_id, al.title, ar.name AS artist_name, v.date_added, v.format
    FROM vinyl_holdings v
    JOIN albums al ON al.id = v.album_id
    JOIN artists ar ON ar.id = al.artist_id
    WHERE v.date_added IS NOT NULL AND v.date_added != ''
    ORDER BY v.date_added DESC LIMIT 6
  `);

  const g = GRANULARITIES[homeState.granularity];
  const chartRows = query(`
    SELECT strftime('${g.fmt}', played_at) AS bucket, count(*) AS c
    FROM scrobbles
    ${g.rangeModifier ? `WHERE played_at >= datetime('now', '${g.rangeModifier}')` : ""}
    GROUP BY bucket ORDER BY bucket
  `);

  app.innerHTML = `
    <div class="stat-grid">
      ${statCard("", stats.songs.toLocaleString(), "Unique Songs Listened", "#/songs")}
      ${statCard("", stats.artists.toLocaleString(), "Total Artists", "#/artists")}
      ${statCard("live", stats.setlists, "Shows attended", "#/shows")}
      ${statCard("live", stats.venues, "Different venues", "#/venues")}
      ${statCard("vinyl", stats.vinyl, "Records owned", "#/vinyl")}
      ${statCard("scrobble", stats.scrobbles.toLocaleString(), "Total tracks played", "#/scrobbles")}
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
      ${recentVinyl.map((v) => `
        <div class="list-item" onclick="location.hash='#/album/${v.album_id}'">
          <div>
            <div class="list-title">${esc(v.title)}</div>
            <div class="list-sub">${esc(v.artist_name)} · ${esc(v.format || "")}</div>
          </div>
          <div class="list-right">${esc((v.date_added || "").slice(0, 10))}</div>
        </div>
      `).join("") || '<div class="subtle">No dated additions yet.</div>'}
    </div>
  `;

  renderChartToolbar(document.getElementById("home-chart-toolbar"), homeState, renderHome);
  renderTimeWindowTabs(
    document.getElementById("most-played-tabs"),
    MOST_PLAYED_WINDOWS,
    MOST_PLAYED_WINDOW_ORDER,
    mostPlayedState.window,
    (key) => { mostPlayedState.window = key; renderHome(); }
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
// Browse: Vinyl (#/vinyl) -- small enough to show in full, with cover art
// ---------------------------------------------------------------------
const vinylState = { q: "", sort: "date_added", dir: "desc", granularity: "year", periodFilter: null };

function renderVinylBrowse() {
  const st = vinylState;
  const g = GRANULARITIES[st.granularity];

  // Same split as the scrobbles page: the search term scopes the chart
  // too (so searching an artist shows their own acquisition history),
  // the period filter (a clicked bar) only scopes the list.
  const searchParams = [];
  let searchWhere = "";
  if (st.q) {
    searchWhere = "(al.title LIKE ? COLLATE NOCASE OR ar.name LIKE ? COLLATE NOCASE)";
    searchParams.push(`%${st.q}%`, `%${st.q}%`);
  }

  const listWhereParts = ["v.date_added IS NOT NULL", "v.date_added != ''"];
  const listParams = [];
  if (searchWhere) { listWhereParts.push(searchWhere); listParams.push(...searchParams); }
  if (st.periodFilter) {
    listWhereParts.push(`strftime('${g.fmt}', v.date_added) = ?`);
    listParams.push(st.periodFilter.key);
  }
  const where = `WHERE ${listWhereParts.join(" AND ")}`;
  const sortCol = { title: "al.title", artist: "ar.name", year: "al.year", date_added: "v.date_added" }[st.sort] || "v.date_added";
  const dir = st.dir === "asc" ? "ASC" : "DESC";

  const rows = query(`
    SELECT v.id, al.id AS album_id, al.title, al.year, ar.id AS artist_id, ar.name AS artist_name,
           v.media_condition, v.date_added
    FROM vinyl_holdings v
    JOIN albums al ON al.id = v.album_id
    JOIN artists ar ON ar.id = al.artist_id
    ${where}
    ORDER BY ${sortCol} ${dir} NULLS LAST
  `, listParams);

  const chartWhereParts = ["v.date_added IS NOT NULL", "v.date_added != ''"];
  if (searchWhere) chartWhereParts.push(searchWhere);
  if (g.rangeModifier) chartWhereParts.push(`v.date_added >= datetime('now', '${g.rangeModifier}')`);
  const chartRows = query(`
    SELECT strftime('${g.fmt}', v.date_added) AS bucket, count(*) AS c
    FROM vinyl_holdings v
    JOIN albums al ON al.id = v.album_id
    JOIN artists ar ON ar.id = al.artist_id
    WHERE ${chartWhereParts.join(" AND ")}
    GROUP BY bucket ORDER BY bucket
  `, searchParams);

  app.innerHTML = `
    <button class="back-link" onclick="history.back()" title="Back" aria-label="Back">←</button>
    <div class="page-header"><h1>Vinyl</h1><div class="subtle">${rows.length.toLocaleString()} records</div></div>
    <div class="section">
      <h2>Added</h2>
      <div id="vinyl-chart-toolbar"></div>
      <div id="vinyl-chart"></div>
    </div>
    <div class="filter-bar">
      <input type="text" id="browse-search" placeholder="Search title or artist…" value="${esc(st.q)}" />
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("title", "Title", st)}
          ${sortHeader("artist", "Artist", st)}
          ${sortHeader("year", "Year", st, true)}
          <th>Condition</th>
          ${sortHeader("date_added", "Added", st)}
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-album-id="${r.album_id}">
              <td class="row-title">${esc(r.title)}</td>
              <td>${esc(r.artist_name)}</td>
              <td class="num">${r.year || ""}</td>
              <td>${esc(r.media_condition || "")}</td>
              <td class="nowrap">${esc((r.date_added || "").slice(0, 10))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;

  app.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/album/${tr.dataset.albumId}`; }));
  wireSortableHeaders(st, renderVinylBrowse, ["title", "artist"]);
  wireSearchInput("browse-search", st, renderVinylBrowse);

  renderChartToolbar(document.getElementById("vinyl-chart-toolbar"), st, renderVinylBrowse);
  renderBarChart(
    document.getElementById("vinyl-chart"),
    chartRows.map((r) => ({
      label: bucketTickLabel(st.granularity, r.bucket),
      tooltipLabel: humanBucketLabel(st.granularity, r.bucket),
      value: r.c,
      key: r.bucket,
    })),
    {
      color: "var(--accent-vinyl)",
      selectedKey: st.periodFilter?.key,
      onClick: (d) => { toggleBucketFilter(st, st.granularity, d.key); renderVinylBrowse(); },
    }
  );
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
      <div class="filter-count">${total.toLocaleString()} songs</div>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          ${sortHeader("title", "Song", st)}
          ${sortHeader("plays", "Plays", st, true)}
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-id="${r.id}">
              <td class="row-title">${esc(r.title)}</td>
              <td class="num">${r.plays.toLocaleString()}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
    ${paginationHtml(st.page, totalPages)}
    <button class="show-less-btn" id="show-less-songs">Show less ↑</button>
  `;

  container.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/song/${tr.dataset.id}`; }));
  wireSortableHeaders(st, renderArtistSongsPanel, ["title"]);
  wirePagination(st, totalPages, renderArtistSongsPanel);
  wireSearchInput("artist-songs-search", st, renderArtistSongsPanel);
  document.getElementById("show-less-songs").addEventListener("click", () => {
    st.expanded = false;
    renderArtistSongsPanel();
  });
}

// ---------------------------------------------------------------------
// Artist: the hub page -- vinyl owned, most-played songs, shows attended
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

  const vinylRows = query(`
    SELECT DISTINCT al.id AS album_id, al.mbid, al.title, al.year, v.format, v.media_condition
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

    <div id="about-panel"></div>

    ${topSongs.length ? `
      <div class="section">
        <h2>Most played songs</h2>
        <div id="artist-top-songs">
          ${topSongs.map((s) => `
            <div class="bar-row" onclick="location.hash='#/song/${s.id}'">
              <div>
                <div class="bar-label">${esc(s.title)}</div>
                <div class="bar-track"><div class="bar-fill" style="width:${((s.plays / maxPlays) * 100).toFixed(0)}%"></div></div>
              </div>
              <div class="bar-count">${s.plays.toLocaleString()}</div>
            </div>
          `).join("")}
        </div>
        <div id="artist-songs-panel"></div>
      </div>
    ` : ""}

    ${vinylRows.length ? `
      <div class="section">
        <h2>On the shelf</h2>
        ${vinylRows.map((v) => `
          <div class="vinyl-card" data-album-id="${v.album_id}" style="cursor:pointer">
            <img class="cover-thumb" data-mbid="${v.mbid || ""}" alt="" loading="lazy" />
            <div class="vinyl-body">
              <div class="title">${esc(v.title)}${v.year ? ` <span class="subtle">(${v.year})</span>` : ""}</div>
              <div class="meta">${esc(v.format || "")}${v.media_condition ? " · " + esc(v.media_condition) : ""}</div>
            </div>
          </div>
        `).join("")}
      </div>
    ` : ""}

    ${setlistRows.length ? `
      <div class="section">
        <h2>Shows attended</h2>
        ${setlistRows.map((sl) => `
          <div class="list-item" onclick="location.hash='#/setlist/${sl.id}'">
            <div>
              <div class="list-title">${esc(sl.venue_name || "Unknown venue")}</div>
              <div class="list-sub">${esc(sl.city || "")}${sl.tour_name ? " · " + esc(sl.tour_name) : ""}</div>
            </div>
            <div class="list-right">${esc(sl.event_date)}</div>
          </div>
        `).join("")}
      </div>
    ` : ""}
  `;

  app.querySelectorAll("img.cover-thumb").forEach((img) => attachCoverArt(img, img.dataset.mbid));
  app.querySelectorAll(".vinyl-card[data-album-id]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/album/${el.dataset.albumId}`; }));

  // Badges jump to the matching browse page, pre-filtered to this artist
  // (a badge for a count of zero is inert -- nothing to filter down to).
  if (vinylCount) {
    document.getElementById("badge-vinyl").addEventListener("click", () => {
      vinylState.q = artist.name; vinylState.periodFilter = null;
      location.hash = "#/vinyl";
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
    renderArtistSongsPanel();
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
      <img class="album-cover-large" data-mbid="${album.mbid || ""}" alt="" />
      <div>
        <h1>${esc(album.title)}</h1>
        <div class="subtle"><span class="link-text" onclick="location.hash='#/artist/${album.artist_id}'">${esc(album.artist_name)}</span>${album.year ? ` · ${album.year}` : ""}</div>
      </div>
    </div>

    <div class="section">
      <h2>${holdings.length > 1 ? "Your copies" : "Your copy"}</h2>
      ${holdings.map((h) => `
        <div class="vinyl-card">
          <div class="vinyl-body">
            <div class="title">${esc(h.format || "")}</div>
            <div class="meta">${[h.label, h.catalog_number].filter(Boolean).map(esc).join(" · ")}</div>
            <div class="meta">${[h.media_condition, h.sleeve_condition ? `${h.sleeve_condition} sleeve` : null].filter(Boolean).map(esc).join(" / ")}</div>
            ${h.notes ? `<div class="meta">${esc(h.notes)}</div>` : ""}
            <div class="meta subtle">Added ${esc((h.date_added || "").slice(0, 10))}</div>
          </div>
        </div>
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
  if (img) attachCoverArt(img, album.mbid);
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
  if (artistMatch) return renderArtist(Number(artistMatch[1]));
  if (songMatch) return renderSong(Number(songMatch[1]));
  if (setlistMatch) return renderSetlist(Number(setlistMatch[1]));
  if (albumMatch) return renderAlbum(Number(albumMatch[1]));
  if (venueMatch) return renderVenue(Number(venueMatch[1]));
  if (hash.startsWith("#/artists")) return renderArtistsBrowse();
  if (hash.startsWith("#/vinyl")) return renderVinylBrowse();
  if (hash.startsWith("#/shows")) return renderShowsBrowse();
  if (hash.startsWith("#/scrobbles")) return renderScrobblesBrowse();
  if (hash.startsWith("#/songs")) return renderSongsBrowse();
  if (hash.startsWith("#/venues")) return renderVenuesBrowse();
  return renderHome();
}

// ---------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------
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

async function boot() {
  initTheme();
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

    showWelcomeScreen();
  } catch (err) {
    app.innerHTML = `<div class="error-box">Couldn't load the database.<br><span class="subtle">${esc(err.message)}</span></div>`;
    console.error(err);
  }
}

/** One-time pause between "database loaded" and actually showing the
 * app -- introduces the site before handing over to the home page. */
function showWelcomeScreen() {
  app.innerHTML = `
    <div class="welcome-screen">
      <div class="welcome-card">
        <p class="welcome-text">This site is an archive of my music history.<br><br>Every song I've listened to on Spotify, every show I've been to, every record in my collection.<br><br>Have a look around, almost everything is clickable.</p>
        <button id="welcome-ok" class="welcome-ok-btn">Okay</button>
      </div>
    </div>
  `;
  document.getElementById("welcome-ok").addEventListener("click", () => {
    setupSearch();
    window.addEventListener("hashchange", render);
    render();
  });
}

boot();
