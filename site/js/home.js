// ---------------------------------------------------------------------
// Home (#/). On a phone it's four screen-sized pages that snap one to the
// next: the overview tiles, listening activity, most played, latest record
// buys. On a desktop, listening activity and most played are one panel
// sharing one Day/Week/Month/Year/All switch. Styles: css/home.css.
// ---------------------------------------------------------------------
const HP_WINDOWS = {
  day: { label: "Day", mod: "-1 day", since: "last 24 hours" },
  week: { label: "Week", mod: "-7 days", since: "last 7 days" },
  month: { label: "Month", mod: "-30 days", since: "last 30 days" },
  year: { label: "Year", mod: "-12 months", since: "last 12 months" },
  all: { label: "All", mod: null, since: "all time" },
};
const HP_PERIODS = ["day", "week", "month", "year", "all"];
const HP_KINDS = { artists: "Artists", albums: "Albums", songs: "Songs" };
const HP_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const HP_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const hpState = { win: "month", kind: "artists" };
const hpCache = { db: null, map: new Map() };

// scrobbles are stored in UTC; hours, days and dates are shown in the viewer's own time
const hpTz = () => `${-new Date().getTimezoneOffset()} minutes`;
const hpWhere = (key, col = "s.played_at") => (HP_WINDOWS[key].mod ? `WHERE ${col} >= datetime('now', '${HP_WINDOWS[key].mod}')` : "");
const hpFmt = (n) => Number(n || 0).toLocaleString();
const hpReduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function hpCached(key, fn) {
  if (hpCache.db !== db) { hpCache.db = db; hpCache.map.clear(); }
  if (!hpCache.map.has(key)) hpCache.map.set(key, fn());
  return hpCache.map.get(key);
}

function hpArt(albumId, version, title, cls = "") {
  return albumId
    ? `<span class="hp-art ${cls}"><img src="${esc(coverUrl(albumId, version))}" alt="" loading="lazy" /></span>`
    : `<span class="hp-art hp-noart ${cls}">${esc(String(title || "?").replace(/^the\s+/i, "").slice(0, 1))}</span>`;
}

const hpArrow = '<svg class="hp-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
const hpChevron = '<svg class="hp-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
const HP_ICONS = {
  disc: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M6 12a6 6 0 0 1 6-6"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  person: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  note: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  album: '<rect x="3" y="3" width="18" height="18" rx="1"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="0.8"/>',
};
const hpIcon = (name, cls = "") => `<svg class="hp-ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${HP_ICONS[name]}</svg>`;

// ---- the four tiles ---------------------------------------------------
function hpOverview() {
  return hpCached("overview", () => {
    const s = query(`
      SELECT (SELECT count(*) FROM scrobbles) AS plays,
        (SELECT min(played_at) FROM scrobbles) AS first_play,
        (SELECT count(*) FROM scrobbles WHERE played_at >= datetime('now', '-30 days')) AS plays30,
        (SELECT count(*) FROM artists) AS artists,
        (SELECT count(*) FROM songs) AS songs,
        (SELECT count(*) FROM setlists) AS shows,
        (SELECT count(*) FROM venues) AS venues,
        (SELECT count(DISTINCT artist_id) FROM setlists) AS live_bands`)[0];

    // vinyl: the collection's own model (reissue detection, decades, genres) from collection.js
    const recs = loadCollection();
    const tally = (f) => {
      const c = {};
      recs.forEach((r) => [].concat(f(r)).filter(Boolean).forEach((k) => { c[k] = (c[k] || 0) + 1; }));
      return Object.entries(c).sort((a, b) => b[1] - a[1]);
    };
    const originals = recs.filter((r) => !r.reissue).length;
    const vinyl = {
      count: recs.length, originals, reissues: recs.length - originals,
      topArtist: tally((r) => r.artist_name)[0] || null,
      topGenre: tally((r) => r.genres.slice(0, 2))[0] || null,
      topLabel: tally((r) => r.labels[0])[0] || null,
      decades: tally((r) => r.decade).filter(([d]) => /^\d/.test(d)).sort((a, b) => a[0].localeCompare(b[0])),
    };

    const years = query(`SELECT CAST(substr(event_date, 1, 4) AS INT) AS y, count(*) AS c FROM setlists WHERE event_date IS NOT NULL GROUP BY y ORDER BY y`);
    const showYears = [];
    if (years.length) {
      const byYear = Object.fromEntries(years.map((r) => [r.y, r.c]));
      for (let y = years[0].y; y <= Math.max(years[years.length - 1].y, new Date().getFullYear()); y++) showYears.push([y, byYear[y] || 0]);
    }
    const live = {
      topVenue: query(`SELECT v.name, count(*) AS c FROM setlists st JOIN venues v ON v.id = st.venue_id GROUP BY v.id ORDER BY c DESC LIMIT 1`)[0] || null,
      mostSeen: query(`SELECT ar.name, count(*) AS c FROM setlists st JOIN artists ar ON ar.id = st.artist_id GROUP BY ar.id ORDER BY c DESC, max(st.event_date) DESC LIMIT 1`)[0] || null,
      last: query(`SELECT ar.name, st.event_date, v.name AS venue FROM setlists st JOIN artists ar ON ar.id = st.artist_id LEFT JOIN venues v ON v.id = st.venue_id ORDER BY st.event_date DESC LIMIT 1`)[0] || null,
      years: showYears,
    };

    const firsts = (col) => `SELECT ${col}, min(played_at) AS f FROM scrobbles GROUP BY ${col}`;
    const recent = "datetime('now', '-30 days')";
    const artists = query(`
      SELECT (SELECT count(*) FROM (${firsts("artist_id")}) WHERE f >= ${recent}) AS new30,
        (SELECT count(DISTINCT artist_id) FROM scrobbles WHERE played_at >= ${recent}) AS played30`)[0];
    artists.topEver = query(`SELECT ar.name FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id GROUP BY ar.id ORDER BY count(*) DESC LIMIT 1`)[0]?.name;
    artists.top30 = query(`SELECT ar.name FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id WHERE s.played_at >= ${recent} GROUP BY ar.id ORDER BY count(*) DESC LIMIT 1`)[0]?.name;
    // the artist you discovered this month and played most
    artists.topNew = query(`SELECT ar.name FROM scrobbles s JOIN (${firsts("artist_id")}) x ON x.artist_id = s.artist_id JOIN artists ar ON ar.id = s.artist_id
                            WHERE x.f >= ${recent} GROUP BY ar.id ORDER BY count(*) DESC LIMIT 1`)[0]?.name;
    // what the last 30 days sounded like: each artist's main genre, weighted by plays
    artists.genres = hasTable("artist_genres") ? query(`
      WITH main AS (SELECT artist_id, genre_id FROM (SELECT artist_id, genre_id,
                      row_number() OVER (PARTITION BY artist_id ORDER BY albums DESC, vinyl_albums DESC) AS rn FROM artist_genres) WHERE rn = 1)
      SELECT ge.name, count(*) AS c FROM scrobbles s JOIN main m ON m.artist_id = s.artist_id JOIN genres ge ON ge.id = m.genre_id
      WHERE s.played_at >= ${recent} GROUP BY ge.id ORDER BY c DESC`) : [];

    const songs = query(`
      SELECT (SELECT count(*) FROM (${firsts("song_id")}) WHERE f >= ${recent}) AS new30,
        (SELECT count(DISTINCT song_id) FROM scrobbles WHERE played_at >= ${recent}) AS played30`)[0];
    // how familiar this month's listening was: each play is the nth time you'd heard that track
    const nth = Object.fromEntries(query(`
      WITH n AS (SELECT played_at, row_number() OVER (PARTITION BY song_id ORDER BY played_at) AS k FROM scrobbles)
      SELECT CASE WHEN k = 1 THEN 'first' WHEN k <= 10 THEN 'early' WHEN k <= 49 THEN 'known' ELSE 'loved' END AS b, count(*) AS c
      FROM n WHERE played_at >= ${recent} GROUP BY b`).map((r) => [r.b, r.c]));
    songs.familiarity = [["First listens", nth.first || 0], ["2nd–10th play", nth.early || 0], ["11th–49th play", nth.known || 0], ["50th play or more", nth.loved || 0]];
    songs.topEver = query(`SELECT so.title FROM scrobbles s JOIN songs so ON so.id = s.song_id GROUP BY so.id ORDER BY count(*) DESC LIMIT 1`)[0]?.title;
    songs.top30 = query(`SELECT so.title FROM scrobbles s JOIN songs so ON so.id = s.song_id WHERE s.played_at >= ${recent} GROUP BY so.id ORDER BY count(*) DESC LIMIT 1`)[0]?.title;
    songs.topNew = query(`SELECT so.title FROM scrobbles s JOIN (${firsts("song_id")}) x ON x.song_id = s.song_id JOIN songs so ON so.id = s.song_id
                          WHERE x.f >= ${recent} GROUP BY so.id ORDER BY count(*) DESC LIMIT 1`)[0]?.title;
    return { s, vinyl, live, artists, songs };
  });
}

// one figure on a tile; `wide` ones (names) take the whole row, `extra` ones go first when the tile is short
function hpStat(value, label, { title = "", wide = false, extra = false, desk = false } = {}) {
  const cls = ["hp-stat", wide && "wide", extra && "extra", desk && "desk"].filter(Boolean).join(" ");
  return `<div class="${cls}"${title ? ` title="${esc(title)}"` : ""}><b>${value}</b><span>${esc(label)}</span></div>`;
}
const hpStatsCap = (text) => `<div class="hp-stats-cap">${esc(text)}</div>`;

// "1950s" with a small s under the captions' capitals
const hpDecade = (d) => `${esc(String(d).slice(0, 4))}<span class="hp-lc">s</span>`;
const hpUnit = (n, unit) => `${hpFmt(n)} ${unit}${n === 1 ? "" : "s"}`;

// a tiny column chart: [[label, value]], the biggest one lit. Labels are text; `label(l)` renders one as
// html under the chart (the peak's in the caption, the first and last at the ends).
function hpMiniBars(pairs, { caption = "", ends = true, endLabels = null, unit = "play", label = esc } = {}) {
  if (!pairs.length) return "";
  const max = Math.max(1, ...pairs.map((p) => p[1]));
  const peak = pairs.findIndex((p) => p[1] === max);
  const capText = caption.replace(/<[^>]+>/g, "");
  return `<div class="hp-mini" role="img" aria-label="${esc(capText)}">
    <div class="hp-mini-bars">${pairs.map(([l, v], i) => `<i class="${i === peak ? "pk" : ""}" style="--h:${Math.max(v ? 6 : 0, (v / max) * 100).toFixed(1)}%; --i:${i}" title="${esc(l)}: ${hpUnit(v, unit)}"></i>`).join("")}</div>
    <div class="hp-mini-cap">${ends ? `<span>${endLabels?.[0] ?? label(pairs[0][0])}</span><span class="pk">${caption}</span><span>${endLabels?.[1] ?? label(pairs[pairs.length - 1][0])}</span>` : `<span class="pk">${caption}</span>`}</div>
  </div>`;
}

// a single stacked bar: [[label, value]]
function hpSplit(parts, caption, unit = "play") {
  const total = parts.reduce((n, p) => n + p[1], 0) || 1;
  return `<div class="hp-mini hp-mini-split" role="img" aria-label="${esc(caption)}">
    <div class="hp-split">${parts.map(([l, v], i) => `<i style="flex:${v}; --i:${i}" title="${esc(l)}: ${hpUnit(v, unit)} (${Math.round((v / total) * 100)}%)"></i>`).join("")}</div>
    <div class="hp-mini-cap"><span class="pk">${esc(caption)}</span></div>
  </div>`;
}

function hpTilesHtml(o) {
  const { s, vinyl, live, artists, songs } = o;
  const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
  const peakDecade = vinyl.decades.reduce((a, b) => (b[1] > (a?.[1] || 0) ? b : a), null);
  const peakYear = live.years.reduce((a, b) => (b[1] > (a?.[1] || 0) ? b : a), null);
  const g = artists.genres;
  const gTotal = g.reduce((n, r) => n + r.c, 0);
  const gParts = g.slice(0, 4).map((r) => [genreName(r.name), r.c]).concat(g.length > 4 ? [["Other", g.slice(4).reduce((n, r) => n + r.c, 0)]] : []);
  const lastShow = live.last ? new Date(live.last.event_date) : null;
  const tile = (cls, href, icon, num, name, mini, stats) => `
    <a class="hp-tile ${cls}" href="${href}">
      ${hpIcon(icon, "hp-tile-mark")}
      <div class="hp-tile-head">
        <div><div class="hp-tile-num">${num}</div><div class="hp-tile-name">${name}</div></div>
        ${hpIcon(icon, "hp-tile-ic")}
      </div>
      ${mini}
      <div class="hp-stats">${stats}</div>
    </a>`;
  return `<div class="hp-tiles">
    ${tile("hp-vinyl", "#/vinyl", "disc", hpFmt(vinyl.count), "Vinyl records",
      hpMiniBars(vinyl.decades, { caption: peakDecade ? `mostly ${hpDecade(peakDecade[0])}` : "", label: hpDecade, unit: "record" }),
      hpStat(hpFmt(vinyl.originals), "originals")
      + hpStat(hpFmt(vinyl.reissues), "reissues")
      + hpStat(esc(vinyl.topArtist?.[0] || "—"), vinyl.topArtist ? `most owned · ${vinyl.topArtist[1]}×` : "most owned", { wide: true })
      + hpStat(esc(genreName(vinyl.topGenre?.[0] || "—")), "top genre", { wide: true, extra: true, title: vinyl.topGenre ? `${vinyl.topGenre[1]} records` : "" })
      + hpStat(esc(vinyl.topLabel?.[0] || "—"), vinyl.topLabel ? `top label · ${vinyl.topLabel[1]}×` : "top label", { wide: true, desk: true }))}
    ${tile("hp-live", "#/shows", "bolt", hpFmt(s.shows), "Live shows",
      hpMiniBars(live.years.map(([y, c]) => [String(y), c]), { caption: peakYear ? `${peakYear[1]} in ${peakYear[0]}` : "", unit: "show" }),
      hpStat(hpFmt(s.venues), "venues")
      + hpStat(hpFmt(s.live_bands), "bands")
      + hpStat(esc(live.mostSeen?.name || "—"), live.mostSeen ? `seen most · ${live.mostSeen.c}×` : "seen most", { wide: true })
      + hpStat(esc(live.last?.name || "—"), lastShow ? `last show · ${HP_MON[lastShow.getMonth()]} ${lastShow.getFullYear()}` : "last show",
        { wide: true, extra: true, title: live.last ? `${live.last.name} at ${live.last.venue || "?"}, ${live.last.event_date}` : "" })
      + hpStat(esc(live.topVenue?.name || "—"), live.topVenue ? `top venue · ${live.topVenue.c}×` : "top venue", { wide: true, desk: true }))}
    ${tile("hp-scrob", "#/artists", "person", hpFmt(s.artists), "Unique artists",
      gParts.length ? hpSplit(gParts, `${gParts[0][0]} · ${pct(gParts[0][1], gTotal)}%`) : "",
      hpStatsCap("Last 30 days")
      + hpStat(hpFmt(artists.new30), "new")
      + hpStat(hpFmt(artists.played30), "played")
      + hpStat(esc(artists.top30 || "—"), "most played", { wide: true })
      + hpStat(esc(artists.topNew || "—"), "top new artist", { wide: true, extra: true })
      + hpStat(esc(artists.topEver || "—"), "most played ever", { wide: true, desk: true }))}
    ${tile("hp-scrob", "#/songs", "note", hpFmt(s.songs), "Unique tracks",
      hpSplit(songs.familiarity, `${pct(songs.familiarity[0][1], s.plays30)}% first listens`),
      hpStatsCap("Last 30 days")
      + hpStat(hpFmt(songs.new30), "new")
      + hpStat(hpFmt(songs.played30), "played")
      + hpStat(esc(songs.top30 || "—"), "most played", { wide: true })
      + hpStat(esc(songs.topNew || "—"), "top new track", { wide: true, extra: true })
      + hpStat(esc(songs.topEver || "—"), "most played ever", { wide: true, desk: true }))}
  </div>`;
}

// ---- segmented controls with a sliding highlight -----------------------
function hpSegHtml(cls, keys, labels, active, { icons = null, label = "" } = {}) {
  return `<div class="hp-seg ${cls}" role="group" aria-label="${esc(label)}">
    <i class="hp-seg-ind" aria-hidden="true"></i>
    ${keys.map((k) => `<button type="button" data-k="${k}" aria-pressed="${k === active}">${icons ? hpIcon(icons[k]) : ""}<span>${esc(labels[k])}</span></button>`).join("")}
  </div>`;
}
function hpSegPlace(seg, instant = false) {
  const on = seg.querySelector('[aria-pressed="true"]');
  const ind = seg.querySelector(".hp-seg-ind");
  if (!on || !ind) return;
  if (instant) ind.style.transition = "none";
  ind.style.width = `${on.offsetWidth}px`;
  ind.style.transform = `translateX(${on.offsetLeft}px)`;
  if (instant) { void ind.offsetWidth; ind.style.transition = ""; }
}
// every control with this class shows the same choice (the period one appears once per section on a phone)
function hpSegWire(cls, onPick) {
  const segs = [...app.querySelectorAll(`.hp-seg.${cls}`)];
  segs.forEach((seg) => {
    hpSegPlace(seg, true);
    seg.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      if (b.getAttribute("aria-pressed") === "true") return;
      segs.forEach((s) => {
        s.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.k === b.dataset.k)));
        hpSegPlace(s, s !== seg);
      });
      onPick(b.dataset.k);
    }));
  });
}

// Swap a block's contents: the old lifts away, the new rises in (children stagger via --i) and its numbers count up.
function hpSwap(host, html, after) {
  const put = () => {
    host.innerHTML = html;
    if (after) after();
    if (hpReduced()) return;
    host.classList.add("hp-pre");
    void host.offsetWidth;
    host.classList.remove("hp-pre", "hp-out");
    hpCountUp(host);
  };
  clearTimeout(host._hpSwap);
  if (hpReduced() || !host.childElementCount) return put();
  host.classList.add("hp-out");
  host._hpSwap = setTimeout(put, 180);
}
function hpCountUp(host) {
  const els = [...host.querySelectorAll("[data-count]")];
  if (!els.length) return;
  const t0 = performance.now();
  const step = (now) => {
    let running = false;
    for (const el of els) {
      const delay = Number(el.dataset.delay || 0);
      const t = Math.min(1, Math.max(0, (now - t0 - delay) / 700));
      if (t < 1) running = true;
      el.textContent = hpFmt(Math.round(Number(el.dataset.count) * (1 - Math.pow(1 - t, 3))));
    }
    if (running && host.isConnected) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---- listening activity ------------------------------------------------
function hpActivity(win) {
  return hpCached(`act:${win}`, () => {
    const tz = hpTz();
    const w = hpWhere(win);
    const pad = (n) => String(n).padStart(2, "0");
    const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const now = new Date();
    let fmt;
    const buckets = [];
    if (win === "day") {
      fmt = "%Y-%m-%d %H";
      for (let i = 23; i >= 0; i--) {
        const d = new Date(now.getTime() - i * 3600000);
        buckets.push({ key: `${ymd(d)} ${pad(d.getHours())}`, label: `${pad(d.getHours())}:00`, full: `${pad(d.getHours())}:00` });
      }
    } else if (win === "week" || win === "month") {
      fmt = "%Y-%m-%d";
      const n = win === "week" ? 7 : 30;
      for (let i = n - 1; i >= 0; i--) {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
        const date = `${d.getDate()} ${HP_MON[d.getMonth()]}`;
        buckets.push({ key: ymd(d), label: win === "week" ? HP_DAYS[d.getDay()].slice(0, 3) : date, full: `${HP_DAYS[d.getDay()].slice(0, 3)} ${date}` });
      }
    } else if (win === "year") {
      fmt = "%Y-%m";
      for (let i = 12; i >= 0; i--) {
        const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        buckets.push({ key: `${d.getFullYear()}-${pad(d.getMonth() + 1)}`, label: HP_MON[d.getMonth()], full: `${HP_MON[d.getMonth()]} ${d.getFullYear()}` });
      }
    } else {
      fmt = "%Y";
      const first = Number(query(`SELECT strftime('%Y', min(played_at)) AS y FROM scrobbles`)[0]?.y || now.getFullYear());
      for (let y = first; y <= now.getFullYear(); y++) buckets.push({ key: String(y), label: String(y), full: String(y) });
    }
    const counts = Object.fromEntries(query(`SELECT strftime('${fmt}', s.played_at, '${tz}') AS b, count(*) AS c FROM scrobbles s ${w} GROUP BY b`).map((r) => [r.b, r.c]));
    const series = buckets.map((b) => ({ ...b, value: counts[b.key] || 0 }));

    const k = query(`SELECT count(*) AS plays, count(DISTINCT s.artist_id) AS artists, count(DISTINCT s.song_id) AS songs, min(s.played_at) AS first FROM scrobbles s ${w}`)[0];
    const mod = HP_WINDOWS[win].mod;
    k.newArtists = mod ? query(`SELECT count(*) AS c FROM (SELECT artist_id, min(played_at) AS f FROM scrobbles GROUP BY artist_id) WHERE f >= datetime('now', ?)`, [mod])[0].c : null;
    const days = { day: 1, week: 7, month: 30, year: 365 }[win] || Math.max(1, (now - new Date(k.first)) / 86400000);
    k.perDay = win === "day" ? k.plays / 24 : k.plays / days;

    const hours = Object.fromEntries(query(`SELECT CAST(strftime('%H', s.played_at, '${tz}') AS INT) AS h, count(*) AS c FROM scrobbles s ${w} GROUP BY h`).map((r) => [r.h, r.c]));
    const weekWhere = win === "day" ? hpWhere("week") : w; // a single day has no weekly shape: use the past week
    const wd = Object.fromEntries(query(`SELECT CAST(strftime('%w', s.played_at, '${tz}') AS INT) AS d, count(*) AS c FROM scrobbles s ${weekWhere} GROUP BY d`).map((r) => [r.d, r.c]));
    return {
      series, k,
      hours: Array.from({ length: 24 }, (_, h) => [`${pad(h)}:00`, hours[h] || 0]),
      weekdays: [1, 2, 3, 4, 5, 6, 0].map((d) => [HP_DAYS[d], wd[d] || 0]),
      weekNote: win === "day" ? "past 7 days" : "",
    };
  });
}

// The chart's scale: gridlines at a round step, and headroom of just under a tenth above the tallest bar
// (not up to the next round number, which could leave half the chart empty).
function hpNice(max) {
  const raw = Math.max(max, 1) / 5;
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  return { step, top: Math.max(max, 1) * 1.08 };
}

function hpActivityHtml(win) {
  const a = hpActivity(win);
  const { series, k } = a;
  const max = Math.max(1, ...series.map((b) => b.value));
  const { step, top } = hpNice(max);
  const peak = series.findIndex((b) => b.value === max);
  const every = Math.ceil(series.length / (window.innerWidth < 640 ? 6 : 10));
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  const stagger = Math.min(40, 420 / series.length);
  const hourPeak = a.hours.reduce((m, h) => (h[1] > m[1] ? h : m), a.hours[0]);
  const dayPeak = a.weekdays.reduce((m, d) => (d[1] > m[1] ? d : m), a.weekdays[0]);
  const kpi = (n, label, i, dec = false) => `<div class="hp-kpi" style="--i:${i}"><b ${dec ? "" : `data-count="${Math.round(n)}" data-delay="${i * 60}"`}>${dec ? n : hpFmt(Math.round(n))}</b><span>${esc(label)}</span></div>`;
  return `
    <div class="hp-kpis">
      ${kpi(k.plays, "plays", 0)}
      ${kpi(k.perDay < 10 ? k.perDay.toFixed(1) : Math.round(k.perDay), win === "day" ? "an hour, on average" : "a day, on average", 1, k.perDay < 10)}
      ${kpi(k.artists, "artists", 2)}
      ${k.newArtists != null ? kpi(k.newArtists, "new artists", 3) : kpi(k.songs, "different tracks", 3)}
    </div>
    <div class="hp-chart-wrap">
      <div class="hp-chart">
        ${ticks.map((v) => `<div class="hp-tick" style="bottom:${((v / top) * 100).toFixed(2)}%"><span>${hpFmt(v)}</span></div>`).join("")}
        <div class="hp-bars">${series.map((b, i) => `<button type="button" class="hp-bar${i === peak ? " pk" : ""}" data-key="${esc(b.key)}" style="--h:${((b.value / top) * 100).toFixed(2)}%; --d:${Math.round(i * stagger)}ms" title="${esc(b.full)}: ${hpFmt(b.value)} plays" aria-label="${esc(b.full)}: ${hpFmt(b.value)} plays"></button>`).join("")}</div>
      </div>
      <div class="hp-xl">${series.map((b, i) => `<span>${i % every === 0 ? esc(b.label) : ""}</span>`).join("")}</div>
      <div class="hp-peak">busiest: <b>${esc(series[peak].full)}</b> · ${hpFmt(max)} plays</div>
    </div>
    <div class="hp-shape">
      <div class="hp-shape-panel">
        <h3>By hour</h3>
        ${hpMiniBars(a.hours, { caption: `peak ${hourPeak[0]}`, endLabels: ["0h", "23h"] })}
      </div>
      <div class="hp-shape-panel">
        <h3>By weekday${a.weekNote ? ` <small>${esc(a.weekNote)}</small>` : ""}</h3>
        ${hpMiniBars(a.weekdays, { caption: `${dayPeak[0]}s`, endLabels: ["Mon", "Sun"] })}
      </div>
    </div>`;
}

function hpWireActivity(host, win) {
  // a bar drills into Scrobbles, filtered to that bucket (in local time, like the bars)
  const g = win === "week" ? "month" : win;
  host.querySelectorAll(".hp-bar").forEach((b) => b.addEventListener("click", () => {
    const bucket = win === "day" ? `${b.dataset.key}:00` : b.dataset.key;
    scrobblesState.granularity = g;
    scrobblesState.periodFilter = { key: bucket, label: humanBucketLabel(g, bucket), tz: hpTz() };
    scrobblesState.page = 1;
    location.hash = "#/scrobbles";
  }));
}

// ---- most played -------------------------------------------------------
function hpMost(kind, win) {
  return hpCached(`most:${kind}:${win}`, () => {
    const w = hpWhere(win);
    const total = query(`SELECT count(*) AS c FROM scrobbles s ${w}`)[0].c;
    let rows;
    if (kind === "artists") {
      rows = query(`SELECT ar.id, ar.name AS title, count(*) AS plays FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id ${w} GROUP BY ar.id ORDER BY plays DESC LIMIT 10`);
      rows.forEach((r) => {
        // an artist's picture: the cover of the album of theirs you play most
        const c = query(`SELECT al.id, al.cover_updated_at FROM scrobbles s JOIN albums al ON al.id = s.album_id
                         WHERE s.artist_id = ? AND al.cover_status = 'ok' GROUP BY al.id ORDER BY count(*) DESC LIMIT 1`, [r.id])[0];
        Object.assign(r, { href: `#/artist/${r.id}`, sub: "", cover: c?.id, ver: c?.cover_updated_at });
      });
    } else if (kind === "albums") {
      rows = query(`SELECT al.id, al.title, ar.name AS sub, al.cover_status, al.cover_updated_at, count(*) AS plays
                    FROM scrobbles s JOIN albums al ON al.id = s.album_id JOIN artists ar ON ar.id = al.artist_id
                    ${w} GROUP BY al.id ORDER BY plays DESC LIMIT 10`);
      rows.forEach((r) => Object.assign(r, { href: `#/album/${r.id}`, cover: r.cover_status === "ok" ? r.id : null, ver: r.cover_updated_at }));
    } else {
      rows = query(`SELECT so.id, so.title, ar.name AS sub, al.id AS album_id, al.cover_status, al.cover_updated_at, count(*) AS plays
                    FROM scrobbles s JOIN songs so ON so.id = s.song_id JOIN artists ar ON ar.id = so.artist_id LEFT JOIN albums al ON al.id = so.album_id
                    ${w} GROUP BY so.id ORDER BY plays DESC LIMIT 10`);
      rows.forEach((r) => Object.assign(r, { href: `#/song/${r.id}`, cover: r.cover_status === "ok" ? r.album_id : null, ver: r.cover_updated_at }));
    }
    return { total, rows };
  });
}

function hpMostHtml(kind, win) {
  const { total, rows } = hpMost(kind, win);
  if (!rows.length) return `<div class="subtle hp-empty">Nothing played in the ${esc(HP_WINDOWS[win].since)}.</div>`;
  const [first, ...rest] = rows;
  const max = first.plays;
  const share = total ? Math.round((first.plays / total) * 100) : 0;
  return `
    <a class="hp-hero" href="${first.href}" style="--i:0">
      ${hpArt(first.cover, first.ver, first.title, "hp-art-xl")}
      <div class="hp-hero-txt">
        <span class="hp-hero-rank">#1 · ${esc(HP_WINDOWS[win].since)}</span>
        <b class="hp-hero-title">${esc(first.title)}</b>
        ${first.sub ? `<span class="hp-hero-sub">${esc(first.sub)}</span>` : ""}
        <span class="hp-hero-num"><b data-count="${first.plays}">${hpFmt(first.plays)}</b> plays</span>
        <span class="hp-hero-share">${share}% of everything you played</span>
      </div>
    </a>
    <ol class="hp-rows" start="2">
      ${rest.map((r, i) => `
        <li style="--i:${i + 1}"><a href="${r.href}">
          <span class="hp-rk">${i + 2}</span>
          ${hpArt(r.cover, r.ver, r.title)}
          <span class="hp-rt"><b>${esc(r.title)}</b>${r.sub ? `<span>${esc(r.sub)}</span>` : ""}</span>
          <span class="hp-rc" data-count="${r.plays}" data-delay="${(i + 1) * 40}">${hpFmt(r.plays)}</span>
          <i class="hp-rbar" style="--w:${((r.plays / max) * 100).toFixed(1)}%"></i>
        </a></li>`).join("")}
    </ol>`;
}

// ---- the page ----------------------------------------------------------
// On a phone the home page is its own full-height scroller (so the browser's bars stay put and every page is
// exactly one screen): its height is the window less the top bar and the bottom tab bar, measured.
function hpMeasureChrome() {
  const top = document.querySelector(".topbar")?.offsetHeight || 0;
  const bar = document.querySelector(".tabbar");
  const bottom = bar && getComputedStyle(bar).display !== "none" ? bar.offsetHeight : 0;
  document.documentElement.style.setProperty("--hp-chrome", `${top + bottom}px`);
}

function renderHome() {
  app.classList.add("wide");
  const o = hpOverview();
  const since = o.s.first_play ? new Date(o.s.first_play) : null;
  const periodSeg = () => hpSegHtml("hp-period", HP_PERIODS, Object.fromEntries(HP_PERIODS.map((k) => [k, HP_WINDOWS[k].label])), hpState.win, { label: "Period" });
  app.innerHTML = `
    <div class="hp" id="hp-scroller">
      <section class="hp-page hp-home" aria-label="Overview">
        <a class="hp-total" href="#/scrobbles">
          <span class="hud">Total songs played</span>
          <span class="hp-total-num" data-count="${o.s.plays}">${hpFmt(o.s.plays)}</span>
          <span class="hp-total-sub">${since ? `since ${HP_MON[since.getMonth()]} ${since.getFullYear()} · ` : ""}<b>${hpFmt(o.s.plays30)}</b> in the last 30 days ${hpArrow}</span>
        </a>
        ${hpTilesHtml(o)}
        <a class="hp-cue" href="#hp-activity" data-scroll="hp-activity">Listening activity ${hpChevron}</a>
      </section>

      <div class="hp-listen">
        <div class="hp-listen-head">
          <h2 class="hp-h2">Your listening</h2>
          ${periodSeg()}
        </div>
        <div class="hp-listen-body">
          <section class="hp-page hp-activity" id="hp-activity" aria-label="Listening activity">
            <h2 class="hp-h2">Listening activity</h2>
            <div class="hp-ctrl hp-ctrl-phone">${periodSeg()}</div>
            <div class="hp-swap" id="hp-act-body">${hpActivityHtml(hpState.win)}</div>
            <a class="hp-cue" href="#hp-most" data-scroll="hp-most">Most played ${hpChevron}</a>
          </section>

          <section class="hp-page hp-most" id="hp-most" aria-label="Most played">
            <h2 class="hp-h2">Most played</h2>
            <div class="hp-card">
              <div class="hp-ctrl hp-ctrl-stack">
                ${hpSegHtml("hp-kind", Object.keys(HP_KINDS), HP_KINDS, hpState.kind, { icons: { artists: "person", albums: "album", songs: "note" }, label: "Show" })}
                <div class="hp-ctrl-phone">${periodSeg()}</div>
              </div>
              <div class="hp-swap hp-mp" id="hp-most-body">${hpMostHtml(hpState.kind, hpState.win)}</div>
            </div>
            <a class="hp-cue" href="#hp-records" data-scroll="hp-records">Latest record buys ${hpChevron}</a>
          </section>
        </div>
      </div>

      <section class="hp-page hp-records" id="hp-records" aria-label="Latest record buys">
        <div class="hp-head"><h2 class="hp-h2">Latest record buys</h2><a class="section-link" href="#/vinyl"><span class="hp-long">The whole collection</span><span class="hp-short">All ${hpFmt(o.vinyl.count)}</span> →</a></div>
        <div id="hp-latest">${latestRecordsHtml(8)}</div>
      </section>
    </div>`;

  hpMeasureChrome();
  hpCountUp(app.querySelector(".hp-total"));
  wireLatestRecords(document.getElementById("hp-latest"));
  app.querySelectorAll("[data-scroll]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault(); // a #fragment would be taken as a route
    document.getElementById(a.dataset.scroll).scrollIntoView({ behavior: hpReduced() ? "auto" : "smooth" });
  }));

  const act = document.getElementById("hp-act-body");
  const most = document.getElementById("hp-most-body");
  hpWireActivity(act, hpState.win);
  hpSegWire("hp-period", (k) => {
    hpState.win = k;
    hpSwap(act, hpActivityHtml(k), () => hpWireActivity(act, k));
    hpSwap(most, hpMostHtml(hpState.kind, k));
  });
  hpSegWire("hp-kind", (k) => { hpState.kind = k; hpSwap(most, hpMostHtml(k, hpState.win)); });

  // the sliding highlights are measured from the buttons: measure again once the fonts are in, and on resize
  const remeasure = () => {
    if (document.body.dataset.route !== "home") return;
    hpMeasureChrome();
    app.querySelectorAll(".hp-seg").forEach((s) => hpSegPlace(s, true));
  };
  document.fonts?.ready.then(remeasure);
  if (!window._hpResize) {
    window._hpResize = true;
    window.addEventListener("resize", remeasure);
  }
}
