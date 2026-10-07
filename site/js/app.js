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

// Genre names are stored lower case ("heavy metal") and shown with capitals ("Heavy Metal") wherever
// the page doesn't already set them in capitals. Display only: filters and links keep the stored name.
const GENRE_SMALL_WORDS = new Set(["and", "of", "the", "n", "&"]);
const GENRE_ACRONYMS = { "r&b": "R&B", edm: "EDM", idm: "IDM", ebm: "EBM", aor: "AOR", nwobhm: "NWOBHM", uk: "UK", us: "US", dj: "DJ" };
function genreName(name) {
  return String(name ?? "").split(" ").map((w, i) => {
    const lw = w.toLowerCase();
    if (GENRE_ACRONYMS[lw]) return GENRE_ACRONYMS[lw];
    if (i > 0 && GENRE_SMALL_WORDS.has(lw)) return lw;
    return w.replace(/(^|[-/])(\p{L})/gu, (m, sep, c) => sep + c.toUpperCase()); // post-punk → Post-Punk
  }).join(" ");
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
  // the button shows the mode you're in: a moon in dark mode (the default), a sun in light
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const dark = theme !== "light";
    btn.innerHTML = `<svg class="i"><use href="#i-${dark ? "moon" : "sun"}"/></svg>`;
    btn.title = dark ? "Dark mode — switch to light" : "Light mode — switch to dark";
  }
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem("theme");
  } catch {
    /* ignore */
  }
  applyTheme(stored || "light"); // light until you choose otherwise

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

// Selector is any [data-sort] element, not just table th's, so a plain
// button can carry a sort too. See wirePagination above for why `scope` matters.
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

// ---------------------------------------------------------------------
// Home (#/): renderHome lives in js/home.js
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// Browse: Scrobbles (#/scrobbles) -- the big one, paginated
// ---------------------------------------------------------------------
const scrobblesState = { q: "", sort: "played_at", dir: "desc", page: 1, granularity: "all", periodFilter: null, range: null };

function renderScrobblesBrowse() {
  const st = scrobblesState;
  // from Home's "average per day": #/scrobbles?range=month -- every play behind it, the chart by day
  const hp = new URLSearchParams(location.hash.split("?")[1] || "").get("range");
  if (hp && HP_WINDOWS[hp]) {
    const mod = HP_WINDOWS[hp].mod;
    st.range = { win: hp, since: mod ? query(`SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) AS d`, [mod])[0].d : null };
    st.granularity = { day: "day", week: "month", month: "month", year: "year", all: "all" }[hp];
    Object.assign(st, { periodFilter: null, q: "", page: 1, sort: "played_at", dir: "desc" });
    history.replaceState(history.state, "", "#/scrobbles");
  }
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
  if (st.range?.since) {
    listWhereParts.push("s.played_at >= ?");
    listParams.push(st.range.since);
  }
  if (st.periodFilter) {
    // a bucket picked on the home chart is in local time (tz: its UTC offset modifier); this page's own are UTC
    listWhereParts.push(`strftime('${g.fmt}', s.played_at${st.periodFilter.tz ? `, '${st.periodFilter.tz}'` : ""}) = ?`);
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
  if (g.rangeModifier) chartWhereParts.push(`s.played_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '${g.rangeModifier}')`);
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
    <div class="page-header"><h1>Scrobbles</h1><div class="subtle">${total.toLocaleString()} plays${st.range && !st.periodFilter && !st.q ? (() => {
      // divided as Home divides it, so the two figures agree
      const days = { week: 7, month: 30, year: 365 }[st.range.win] || Math.max(1, (Date.now() - new Date(query("SELECT min(played_at) AS d FROM scrobbles")[0].d)) / 86400000);
      const per = st.range.win === "day" ? total / 24 : total / days;
      return ` · ${per < 10 ? per.toFixed(1) : Math.round(per).toLocaleString()} ${st.range.win === "day" ? "an hour" : "a day"} on average`;
    })() : ""}</div>
      ${st.range ? `<div class="ab-range" title="From Home's listening activity"><span><i>From Home</i> <b>${esc(HP_WINDOWS[st.range.win].since)}</b></span>
        <button type="button" data-act="clear-range" aria-label="Remove this filter" title="Remove this filter">✕</button></div>` : ""}</div>
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
  app.querySelector("[data-act='clear-range']")?.addEventListener("click", () => { st.range = null; st.page = 1; renderScrobblesBrowse(); });
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
// Album: the release detail a vinyl entry links to -- every physical
// copy owned (pressings/variants can differ -- catalog#, color, condition)
// plus whatever tracks we know from that album, with cover art.
// ---------------------------------------------------------------------
// The album page's songs: split into the album's own tracklist and bonus / other tracks when a
// reference tracklist is known (a pressing you own), else just the songs, most played first.
// renderAlbum (#/album/<id>) lives in js/album-page.js

// ---------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------
// Tracks the last hash actually rendered, so render() can tell a real
// navigation (fresh page, hash changed) from a same-route re-render
// (the theme toggle calls render() to redraw charts in the new colors,
// without the hash changing) -- only the former should reset scroll.
let lastRenderedHash = null;

// How many in-app steps deep this history entry is (kept in history.state), so Back never
// leaves the site: a deep-linked page's Back goes home instead.
let navDepth = 0;
let skipRender = null; // a hash change already handled in place (closing the record drawer)
window.addEventListener("popstate", () => { if (history.state?.depth != null) navDepth = history.state.depth; });
// The phone's "hard refresh": every file the page uses (and the database) fetched fresh -- bypassing the
// browser's cache, which plain reloads on a phone keep reusing -- then the page reloads with them.
async function hardRefresh() {
  const btn = document.getElementById("hard-refresh");
  btn?.classList.add("spinning");
  const same = (u) => { try { return new URL(u, location.href).origin === location.origin; } catch { return false; } };
  const urls = new Set([location.pathname || "./", "index.html", "public/music.sqlite.size", "public/music.sqlite",
    ...[...document.scripts].map((s) => s.src).filter((u) => u && same(u)),
    ...[...document.querySelectorAll('link[rel="stylesheet"]')].map((l) => l.href).filter(same)]);
  await Promise.all([...urls].map((u) => fetch(u, { cache: "reload" }).catch(() => null)));
  location.reload();
}

// The top bar's back button names where it goes ("‹ Gojira"): the heading of each page is remembered
// by its history depth as you leave it. Opened straight onto a page, back goes Home (goBack).
const backLabels = [];
function rememberBackLabel() {
  if (lastRenderedHash === null) return;
  const h = app.querySelector("h1");
  backLabels[navDepth] = document.body.dataset.route === "home" ? "Home" : h ? h.innerText.split("\n")[0].replace(/MBID$/, "").trim() : "Back";
}
function updateBackButton() {
  const label = navDepth > 0 ? backLabels[navDepth - 1] || "Back" : "Home";
  const btn = document.getElementById("topbar-back");
  btn.querySelector(".tb-label").textContent = label;
  btn.title = `Back to ${label}`;
  btn.setAttribute("aria-label", `Back to ${label}`);
}

function goBack() {
  if (navDepth > 0) history.back();
  else location.hash = "#/";
}

/** The mobile topbar can carry the page's own title in place of the artist search (the
 * Collection has its own search). Cleared on every render; a page sets it after. */
function setTopbarTitle(hud, title) {
  const el = document.getElementById("topbar-title");
  el.innerHTML = hud ? `<span class="hud">${esc(hud)}</span><span class="tt-name">${esc(title)}</span><svg class="i tt-search" aria-label="Search"><use href="#i-search"/></svg>` : "";
  document.body.classList.toggle("has-topbar-title", Boolean(hud));
}

function render() {
  if (skipRender !== null && skipRender === location.hash) { skipRender = null; return; }
  skipRender = null;
  renderToken += 1;
  const hash = location.hash || "#/";
  rememberBackLabel();
  if (history.state?.depth == null) history.replaceState({ depth: lastRenderedHash === null ? 0 : navDepth + 1 }, "", location.href);
  navDepth = history.state.depth;
  updateBackButton();
  document.body.dataset.route = hash === "#/" || hash === "#" ? "home" : hash.split(/[/?]/)[1] || "home";
  document.body.classList.remove("search-open");
  setTopbarTitle(null);
  if (hash !== lastRenderedHash) window.scrollTo(0, 0);
  lastRenderedHash = hash;
  const artistMatch = hash.match(/^#\/artist\/(\d+)/);
  const songMatch = hash.match(/^#\/song\/(\d+)/);
  const setlistMatch = hash.match(/^#\/setlist\/(\d+)/);
  const albumMatch = hash.match(/^#\/album\/(\d+)/);
  const venueMatch = hash.match(/^#\/venue\/(\d+)/);
  const recordMatch = hash.match(/^#\/vinyl\/(\d+)/);
  const eventMatch = hash.match(/^#\/event\/(\d{4}-\d{2}-\d{2})/);
  app.classList.remove("wide");
  if (!recordMatch && typeof closeRecord === "function") closeRecord(false); // leaving the collection: no drawer left behind
  if (artistMatch) return renderArtist(Number(artistMatch[1]));
  if (songMatch) return renderSong(Number(songMatch[1]));
  if (setlistMatch) return renderSetlist(Number(setlistMatch[1]));
  if (albumMatch) return renderAlbum(Number(albumMatch[1]));
  if (venueMatch) return renderVenue(Number(venueMatch[1]));
  if (hash.startsWith("#/albums")) return renderAlbumsBrowse();
  if (hash.startsWith("#/artists")) return renderArtistsBrowse();
  if (recordMatch) return renderCollection(Number(recordMatch[1]));
  if (hash.startsWith("#/vinyl")) return renderCollection();
  if (eventMatch) return renderEvent(eventMatch[1]);
  if (hash.startsWith("#/shows/insights")) return renderLiveInsightsPage();
  if (hash.startsWith("#/shows")) return renderLiveHub();
  if (hash.startsWith("#/scrobbles")) return renderScrobblesBrowse();
  if (hash.startsWith("#/songs")) return renderSongsBrowse();
  if (hash.startsWith("#/venues")) { liveState.view = "venues"; history.replaceState(history.state, "", "#/shows"); lastRenderedHash = "#/shows"; return renderLiveHub(); }  // the venues list is the hub's Venues view now
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
  // a page showing its title up top (Vinyl, Live): tapping the title swaps it for the search box
  document.getElementById("topbar-title").addEventListener("click", () => {
    document.body.classList.add("search-open");
    document.getElementById("search-input").focus();
  });
  function reflectRoute() {
    const hash = location.hash || "#/";
    const route = hash === "#/" ? "home" : hash.startsWith("#/vinyl") ? "vinyl" : hash.startsWith("#/shows") ? "shows"
      : /^#\/(songs|albums|artists|scrobbles)\b/.test(hash) ? "digital" : null;  // Digital: the Songs / Albums / Artists lists
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
    if (!e.target.closest(".search-wrap, #topbar-title") && !input.value.trim()) document.body.classList.remove("search-open");
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
  const [resp, knownTotal] = await Promise.all([fetch(url, { cache: "no-cache" }), totalPromise]);
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

/** Embedded: theme is driven entirely by Citadel (initial value via a ?theme= query
 * param, then postMessage on every load and change) -- the local toggle button is
 * removed outright so it can never be clicked and fight the parent. Standalone: the
 * existing self-contained initTheme() (localStorage + matchMedia fallback) is untouched.
 *
 * Citadel's custom themes (Settings -> Appearance) arrive as `vars`: the same CSS variable
 * names this page already uses (the palettes were harmonised), so they're applied as-is.
 * `bg` means Citadel draws a background picture behind this frame: the page's own backdrop
 * goes see-through so it shows. color-scheme is pinned to the mode so the browser never
 * paints an opaque backdrop under a transparent frame of a different scheme. */
let citadelVars = [];
function applyCitadelLook(msg) {
  applyTheme(msg.theme);
  const root = document.documentElement;
  root.style.colorScheme = msg.theme;
  citadelVars.forEach((name) => root.style.removeProperty(name));
  citadelVars = [];
  if (msg.vars && typeof msg.vars === "object") {
    for (const [name, value] of Object.entries(msg.vars)) {
      if (!/^--[a-z0-9-]+$/.test(name) || typeof value !== "string" || value.length > 200) continue;
      root.style.setProperty(name, value);
      citadelVars.push(name);
    }
  }
  root.classList.toggle("citadel-bg", !!msg.bg);
}

function initEmbeddedTheme() {
  document.getElementById("theme-toggle")?.remove();
  const initial = new URLSearchParams(location.search).get("theme");
  if (initial === "light" || initial === "dark") applyCitadelLook({ theme: initial });
  window.addEventListener("message", (event) => {
    if (event.origin !== CITADEL_ORIGIN) return;
    if (event.data?.type === "citadel-theme" && (event.data.theme === "light" || event.data.theme === "dark")) {
      applyCitadelLook(event.data);
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
    // "no-cache": the browser asks the server each time whether its copy is still current (a quick "not modified"
    // when it is) -- so a published database always arrives, rather than an old one the browser decided to keep
    const sizePromise = fetch("public/music.sqlite.size", { cache: "no-cache" })
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
