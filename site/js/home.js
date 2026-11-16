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
const HP_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
// win: the period's size (calendar periods: midnight to midnight, Mon–Sun, the 1st to the last, Jan–Dec),
// or "custom" for dates picked on the calendar (custom: { start, end } local midnights, end exclusive);
// offset: how many periods back the ‹ › arrows have stepped (0 = the one we're in now)
const hpState = { win: "month", kind: "artists", offset: 0, custom: null };
const hpCache = { db: null, map: new Map() };

// scrobbles are stored in UTC; hours, days and dates are shown in the viewer's own time
const hpTz = () => `${-new Date().getTimezoneOffset()} minutes`;
const hpFmt = (n) => Number(n || 0).toLocaleString();

// ---- the period on show ------------------------------------------------
const hpPad = (n) => String(n).padStart(2, "0");
const hpYmd = (d) => `${d.getFullYear()}-${hpPad(d.getMonth() + 1)}-${hpPad(d.getDate())}`;
const hpIso = (d) => d.toISOString().slice(0, 19);                     // compares with played_at (UTC)
const hpMs = (s) => Date.parse(/(Z|[+-]\d\d:\d\d)$/.test(s) ? s : `${s}Z`);  // played_at / hpIso -> ms
const hpDay = (d) => `${HP_DAYS[d.getDay()].slice(0, 3)} ${d.getDate()} ${HP_MON[d.getMonth()]}`;

function hpFirstPlay() {
  return hpCached("first", () => {
    const f = query(`SELECT min(played_at) AS f FROM scrobbles`)[0]?.f;
    return f ? new Date(hpMs(f)) : null;
  });
}

// the bounds [start, end) of a period, as local Dates (null for all time)
function hpBounds(win, offset, now = new Date()) {
  if (win === "all") return { start: null, end: null };
  if (win === "custom") {     // ‹ › move a picked range by its own length
    const { start, end } = hpState.custom, n = hpDaysBetween(start, end) * offset;
    return { start: hpAddDays(start, -n), end: hpAddDays(end, -n) };
  }
  const y = now.getFullYear(), m = now.getMonth(), d = now.getDate();
  if (win === "day") return { start: new Date(y, m, d - offset), end: new Date(y, m, d - offset + 1) };
  if (win === "week") { const mon = d - ((now.getDay() + 6) % 7) - 7 * offset; return { start: new Date(y, m, mon), end: new Date(y, m, mon + 7) }; }
  if (win === "month") return { start: new Date(y, m - offset, 1), end: new Date(y, m - offset + 1, 1) };
  return { start: new Date(y - offset, 0, 1), end: new Date(y - offset + 1, 0, 1) };
}

const hpAddDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const hpDaysBetween = (a, b) => Math.round((Date.UTC(b.getFullYear(), b.getMonth(), b.getDate()) - Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
// "3 – 9 Jun 2026", "28 May – 3 Jun 2026", "28 Dec 2025 – 3 Jan 2026" (first and last day, both included)
function hpSpanLabel(a, b) {
  if (a.getFullYear() !== b.getFullYear()) return `${a.getDate()} ${HP_MON[a.getMonth()]} ${a.getFullYear()} – ${b.getDate()} ${HP_MON[b.getMonth()]} ${b.getFullYear()}`;
  if (a.getMonth() !== b.getMonth()) return `${a.getDate()} ${HP_MON[a.getMonth()]} – ${b.getDate()} ${HP_MON[b.getMonth()]} ${b.getFullYear()}`;
  return `${a.getDate()} – ${b.getDate()} ${HP_MON[b.getMonth()]} ${b.getFullYear()}`;
}

// how many periods back is the one holding `when` (a new size keeps your place: Nov 2025 -> Year = 2025)
function hpOffsetFor(win, when, now = new Date()) {
  if (win === "all" || win === "custom" || !when) return 0;
  const days = (a, b) => hpDaysBetween(b, a);
  const k = win === "day" ? days(now, when)
    : win === "week" ? Math.floor((days(now, when) + ((when.getDay() + 6) % 7) - ((now.getDay() + 6) % 7)) / 7)
    : win === "month" ? (now.getFullYear() - when.getFullYear()) * 12 + now.getMonth() - when.getMonth()
    : now.getFullYear() - when.getFullYear();
  return Math.max(0, k);
}

// Everything about one period: bounds and what to call it.
//   tag: a short name ("this week", "week of 2 Nov", "October 2025") -- the navigator, "#1 · …", the From Home pill
//   inText: the same in a sentence ("this week", "in the week of 2 Nov", "in October 2025", "ever")
//   dates: the exact dates, when the tag doesn't already say them
function hpRange(win, offset = hpState.offset, inner = false) {
  if (win === "all" || win === "custom") offset = 0;   // a picked range moves itself (hpStep), it has no offset
  const now = new Date();
  const { start, end } = hpBounds(win, offset, now);
  // unit: what one bar of the chart is (an hour, a day, a month, a year)
  const unit = { day: "hour", week: "day", month: "day", year: "month", all: "year" }[win];
  const r = { win, offset, start, end, unit, perHour: win === "day", startIso: start ? hpIso(start) : null, endIso: end ? hpIso(end) : null, key: `${win}:${offset}` };
  const yr = (d) => (d.getFullYear() === now.getFullYear() ? "" : ` ${d.getFullYear()}`);
  if (win === "all") {
    const f = hpFirstPlay();
    return Object.assign(r, { tag: "all time", inText: "ever", vsText: "", dates: f ? `since ${HP_MON[f.getMonth()]} ${f.getFullYear()}` : "" });
  }
  const last = new Date(end.getTime() - 1);                             // the period's last moment, for "– Sun 15 Nov"
  const fullDay = `${hpDay(start)} ${start.getFullYear()}`;
  if (win === "custom") {
    const n = hpDaysBetween(start, end);
    Object.assign(r, { key: `custom:${hpYmd(start)}:${hpYmd(end)}`, perHour: n === 1,
      unit: n === 1 ? "hour" : n <= 62 ? "day" : n <= 1100 ? "month" : "year",
      tag: n === 1 ? fullDay : hpSpanLabel(start, last), dates: n === 1 ? "" : `${n} days`,
      inText: n === 1 ? `on ${fullDay}` : `between ${start.getDate()} ${HP_MON[start.getMonth()]}${start.getFullYear() === last.getFullYear() ? "" : ` ${start.getFullYear()}`} and ${last.getDate()} ${HP_MON[last.getMonth()]} ${last.getFullYear()}`,
      vsText: n === 1 ? "vs the day before" : `vs the ${n} days before` });
    return r;
  }
  if (win === "day") Object.assign(r, offset < 2 ? { tag: offset ? "yesterday" : "today", dates: fullDay } : { tag: fullDay, inText: `on ${fullDay}`, dates: "" });
  else if (win === "week") Object.assign(r, { tag: offset === 0 ? "this week" : offset === 1 ? "last week" : `week of ${start.getDate()} ${HP_MON[start.getMonth()]}${yr(start)}`,
    dates: hpSpanLabel(start, last) });
  else if (win === "month") {
    const name = `${HP_MONTHS[start.getMonth()]} ${start.getFullYear()}`;
    Object.assign(r, offset < 2 ? { tag: offset ? "last month" : "this month", dates: name } : { tag: name, dates: "" });
  } else Object.assign(r, offset < 2 ? { tag: offset ? "last year" : "this year", dates: String(start.getFullYear()) } : { tag: String(start.getFullYear()), dates: "" });
  r.inText ||= /^(today|yesterday|this |last )/.test(r.tag) ? r.tag : `in ${r.tag.startsWith("week") ? "the " : ""}${r.tag}`;
  r.vsText = inner ? "" : `vs ${hpRange(win, offset + 1, true).tag}`;
  return r;
}
// the chart's buckets for a period: one per hour, day, month or year (r.unit) across it
function hpBuckets(r) {
  const out = [];
  const now = new Date();
  const start = r.start || new Date(hpFirstPlay()?.getFullYear() ?? now.getFullYear(), 0, 1);
  const end = r.end || now;
  const yr = (d) => (d.getFullYear() === now.getFullYear() ? "" : ` ${d.getFullYear()}`);
  if (r.unit === "hour") {
    for (let i = 0; i < 24; i++) out.push({ key: `${hpYmd(start)} ${hpPad(i)}`, label: `${hpPad(i)}:00`, full: `${hpPad(i)}:00` });
  } else if (r.unit === "day") {
    const short = hpDaysBetween(start, end) <= 7;   // a week reads Mon Tue Wed…, longer runs by date
    for (let d = new Date(start); d < end; d = hpAddDays(d, 1)) {
      const date = `${d.getDate()} ${HP_MON[d.getMonth()]}`;
      out.push({ key: hpYmd(d), label: short ? HP_DAYS[d.getDay()].slice(0, 3) : date, full: `${HP_DAYS[d.getDay()].slice(0, 3)} ${date}${yr(d)}` });
    }
  } else if (r.unit === "month") {
    for (let d = new Date(start.getFullYear(), start.getMonth(), 1); d < end; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) {
      const multi = start.getFullYear() !== new Date(end.getTime() - 1).getFullYear();
      out.push({ key: `${d.getFullYear()}-${hpPad(d.getMonth() + 1)}`, label: multi && d.getMonth() === 0 ? String(d.getFullYear()) : HP_MON[d.getMonth()], full: `${HP_MON[d.getMonth()]} ${d.getFullYear()}` });
    }
  } else {
    for (let y = start.getFullYear(); y <= new Date(end.getTime() - 1).getFullYear(); y++) out.push({ key: String(y), label: String(y), full: String(y) });
  }
  return out;
}
// "WHERE …" (on scrobbles s) and its params for a period
const hpRangeWhere = (r, col = "s.played_at") => (r.startIso ? [`WHERE ${col} >= ? AND ${col} < ?`, [r.startIso, r.endIso]] : ["", []]);
// plays per day (per hour for a Day) over a period, counting only the part of it that has happened yet --
// shared with Scrobbles, so the two figures agree
function hpAverage(plays, startIso, endIso, perHour) {
  const now = Date.now();
  const first = startIso || query(`SELECT min(played_at) AS f FROM scrobbles`)[0]?.f;
  if (!first) return 0;
  const e = endIso ? Math.min(now, hpMs(endIso)) : now;
  return plays / Math.max(1, (e - hpMs(first)) / (perHour ? 3600000 : 86400000));
}
// what a Home stat links with: the exact period (and the one before, for the rank arrows)
function hpLinkQuery(r) {
  if (!r.startIso) return "range=all";
  const prev = hpBounds(r.win, r.offset + 1).start;
  // Scrobbles picks its chart from `win`: a picked range passes the size its bars match
  const win = r.win !== "custom" ? r.win : { hour: "day", day: "month", month: "year", year: "all" }[r.unit];
  return new URLSearchParams({ win, from: r.startIso, to: r.endIso, prev: hpIso(prev), tag: r.tag, in: r.inText, vs: r.vsText }).toString();
}
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
        (SELECT count(*) FROM scrobbles WHERE played_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days')) AS plays30,
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
    const recent = "strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days')";
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
                      row_number() OVER (PARTITION BY artist_id ORDER BY albums DESC, vinyl_albums DESC${agVotesOrder("artist_genres")}) AS rn FROM artist_genres) WHERE rn = 1)
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
  if (!ind) return;
  if (!on || !on.offsetWidth) { ind.style.width = "0px"; return; }   // none chosen (a picked range), or not showing yet
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
function hpActivity(r) {
  return hpCached(`act:${r.key}`, () => {
    const tz = hpTz();
    const [w, wp] = hpRangeWhere(r);
    const fmt = { hour: "%Y-%m-%d %H", day: "%Y-%m-%d", month: "%Y-%m", year: "%Y" }[r.unit];
    const counts = Object.fromEntries(query(`SELECT strftime('${fmt}', s.played_at, '${tz}') AS b, count(*) AS c FROM scrobbles s ${w} GROUP BY b`, wp).map((x) => [x.b, x.c]));
    const series = hpBuckets(r).map((b) => ({ ...b, value: counts[b.key] || 0 }));

    const k = query(`SELECT count(*) AS plays, count(DISTINCT s.artist_id) AS artists, count(DISTINCT s.song_id) AS songs FROM scrobbles s ${w}`, wp)[0];
    k.newArtists = r.startIso ? query(`SELECT count(*) AS c FROM (SELECT artist_id, min(played_at) AS f FROM scrobbles GROUP BY artist_id) WHERE f >= ? AND f < ?`, wp)[0].c : null;
    k.perDay = hpAverage(k.plays, r.startIso, r.endIso, r.perHour);
    k.listened = listenTime(w, wp);                                // "≈ 41 h" of listening in the period

    const hours = Object.fromEntries(query(`SELECT CAST(strftime('%H', s.played_at, '${tz}') AS INT) AS h, count(*) AS c FROM scrobbles s ${w} GROUP BY h`, wp).map((x) => [x.h, x.c]));
    // a single day has no weekly shape: use its week
    let ww = w, wwp = wp;
    if (r.unit === "hour") {
      const s = hpBounds("week", hpOffsetFor("week", r.start));
      [ww, wwp] = hpRangeWhere({ startIso: hpIso(s.start), endIso: hpIso(s.end) });
    }
    const wd = Object.fromEntries(query(`SELECT CAST(strftime('%w', s.played_at, '${tz}') AS INT) AS d, count(*) AS c FROM scrobbles s ${ww} GROUP BY d`, wwp).map((x) => [x.d, x.c]));
    return {
      series, k,
      hours: Array.from({ length: 24 }, (_, h) => [`${hpPad(h)}:00`, hours[h] || 0]),
      weekdays: [1, 2, 3, 4, 5, 6, 0].map((d) => [HP_DAYS[d], wd[d] || 0]),
      weekNote: r.unit !== "hour" ? "" : hpOffsetFor("week", r.start) ? "that week" : "this week",
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

function hpActivityHtml(r) {
  const a = hpActivity(r);
  const { series, k } = a;
  const most = Math.max(0, ...series.map((b) => b.value));   // 0: nothing played in this period
  const max = Math.max(1, most);
  const { step, top } = hpNice(max);
  const peak = most ? series.findIndex((b) => b.value === most) : -1;
  const every = Math.ceil(series.length / (window.innerWidth < 640 ? 6 : 10));
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  const stagger = Math.min(40, 420 / series.length);
  const hourPeak = a.hours.reduce((m, h) => (h[1] > m[1] ? h : m), a.hours[0]);
  const dayPeak = a.weekdays.reduce((m, d) => (d[1] > m[1] ? d : m), a.weekdays[0]);
  // each stat opens the list behind it, for this period (shown there as a removable "From Home" filter)
  // the listening time gets its own line under "plays": a year's or all time's hours don't fit beside it
  const kpi = (n, label, i, href, title, dec = false, sub = "") => `<a class="hp-kpi" href="${href}" title="${esc(title)}" style="--i:${i}"><b ${dec ? "" : `data-count="${Math.round(n)}" data-delay="${i * 60}"`}>${dec ? n : hpFmt(Math.round(n))}</b><span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</a>`;
  const during = r.inText;
  const q = hpLinkQuery(r);
  const unit = r.perHour ? "hour" : "day";
  return `
    <div class="hp-kpis">
      ${kpi(k.plays, "plays", 0, `#/songs?${q}`, `The songs you played ${during}${k.listened ? ` — ${k.listened.replace("≈ ", "about ")} of listening` : ""}`, false, k.listened || "")}
      ${kpi(k.perDay < 10 ? k.perDay.toFixed(1) : Math.round(k.perDay), `average per ${unit}`, 1, `#/scrobbles?${q}`, `Every play ${during}, ${unit} by ${unit}`, k.perDay < 10)}
      ${kpi(k.artists, "artists", 2, `#/artists?${q}`, `The artists you played ${during}`)}
      ${k.newArtists != null ? kpi(k.newArtists, "new artists", 3, `#/artists?${q}&new=1`, `Artists you played for the first time ${during}`)
        : kpi(k.songs, "different tracks", 3, `#/songs?${q}`, "Every song you've played")}
    </div>
    <div class="hp-chart-wrap">
      <div class="hp-chart">
        ${ticks.map((v) => `<div class="hp-tick" style="bottom:${((v / top) * 100).toFixed(2)}%"><span>${hpFmt(v)}</span></div>`).join("")}
        <div class="hp-bars">${series.map((b, i) => `<button type="button" class="hp-bar${i === peak ? " pk" : ""}" data-key="${esc(b.key)}" style="--h:${((b.value / top) * 100).toFixed(2)}%; --d:${Math.round(i * stagger)}ms" title="${esc(b.full)}: ${hpFmt(b.value)} plays" aria-label="${esc(b.full)}: ${hpFmt(b.value)} plays"></button>`).join("")}</div>
      </div>
      <div class="hp-xl">${series.map((b, i) => `<span>${i % every === 0 ? esc(b.label) : ""}</span>`).join("")}</div>
      <div class="hp-peak">${peak >= 0 ? `busiest: <b>${esc(series[peak].full)}</b> · ${hpFmt(most)} plays` : `No plays ${esc(during)}`}</div>
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

function hpWireActivity(host, r) {
  // a bar drills into Scrobbles, filtered to that bucket (in local time, like the bars)
  const g = { hour: "day", day: "month", month: "year", year: "all" }[r.unit];
  host.querySelectorAll(".hp-bar").forEach((b) => b.addEventListener("click", () => {
    const bucket = r.unit === "hour" ? `${b.dataset.key}:00` : b.dataset.key;
    scrobblesState.granularity = g;
    scrobblesState.periodFilter = { key: bucket, label: humanBucketLabel(g, bucket), tz: hpTz() };
    scrobblesState.page = 1;
    location.hash = "#/scrobbles";
  }));
}

// ---- most played -------------------------------------------------------
function hpMost(kind, r) {
  return hpCached(`most:${kind}:${r.key}`, () => {
    const [w, wp] = hpRangeWhere(r);
    const total = query(`SELECT count(*) AS c FROM scrobbles s ${w}`, wp)[0].c;
    let rows;
    if (kind === "artists") {
      rows = query(`SELECT ar.id, ar.name AS title, count(*) AS plays FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id ${w} GROUP BY ar.id ORDER BY plays DESC LIMIT 10`, wp);
      rows.forEach((x) => {
        // the album of theirs you played most in this period: its title is the row's second line (every view's
        // rows have the same two lines) and its cover the artist's picture
        const top = query(`SELECT al.id, al.title, al.cover_status, al.cover_updated_at FROM scrobbles s JOIN albums al ON al.id = s.album_id
                           WHERE s.artist_id = ? ${r.startIso ? "AND s.played_at >= ? AND s.played_at < ?" : ""} GROUP BY al.id ORDER BY count(*) DESC LIMIT 1`, [x.id, ...wp])[0];
        // no cover for it: the cover of the album of theirs you play most of all
        const c = top?.cover_status === "ok" ? top : query(`SELECT al.id, al.cover_updated_at FROM scrobbles s JOIN albums al ON al.id = s.album_id
                         WHERE s.artist_id = ? AND al.cover_status = 'ok' GROUP BY al.id ORDER BY count(*) DESC LIMIT 1`, [x.id])[0];
        Object.assign(x, { href: `#/artist/${x.id}`, sub: top ? `top album: ${top.title}` : "", cover: c?.id, ver: c?.cover_updated_at });
      });
    } else if (kind === "albums") {
      rows = query(`SELECT al.id, al.title, ar.name AS sub, al.cover_status, al.cover_updated_at, count(*) AS plays
                    FROM scrobbles s JOIN albums al ON al.id = s.album_id JOIN artists ar ON ar.id = al.artist_id
                    ${w} GROUP BY al.id ORDER BY plays DESC LIMIT 10`, wp);
      rows.forEach((x) => Object.assign(x, { href: `#/album/${x.id}`, cover: x.cover_status === "ok" ? x.id : null, ver: x.cover_updated_at }));
    } else {
      rows = query(`SELECT so.id, so.title, ar.name AS sub, al.id AS album_id, al.cover_status, al.cover_updated_at, count(*) AS plays
                    FROM scrobbles s JOIN songs so ON so.id = s.song_id JOIN artists ar ON ar.id = so.artist_id LEFT JOIN albums al ON al.id = so.album_id
                    ${w} GROUP BY so.id ORDER BY plays DESC LIMIT 10`, wp);
      rows.forEach((x) => Object.assign(x, { href: `#/song/${x.id}`, cover: x.cover_status === "ok" ? x.album_id : null, ver: x.cover_updated_at }));
    }
    return { total, rows };
  });
}

function hpMostHtml(kind, r) {
  const { total, rows } = hpMost(kind, r);
  if (!rows.length) return `<div class="subtle hp-empty">Nothing played ${esc(r.inText)}.</div>`;
  const [first, ...rest] = rows;
  const max = first.plays;
  const share = total ? Math.round((first.plays / total) * 100) : 0;
  return `
    <a class="hp-hero" href="${first.href}" style="--i:0">
      <span class="hp-rk">1</span>
      ${hpArt(first.cover, first.ver, first.title, "hp-art-xl")}
      <span class="hp-hero-txt">
        <span class="hp-hero-rank">Top ${{ artists: "artist", albums: "album", songs: "song" }[kind]}<span class="hp-hero-when"> · ${esc(r.tag)}</span></span>
        <b class="hp-hero-title" title="${esc(first.title)}">${esc(first.title)}</b>
        <span class="hp-hero-sub">${esc(first.sub || "")}</span>
        <span class="hp-hero-share">${share}% of everything you played</span>
      </span>
      <span class="hp-hero-num"><b data-count="${first.plays}">${hpFmt(first.plays)}</b>plays</span>
      <i class="hp-rbar" style="--w:100%"></i>
    </a>
    <ol class="hp-rows" start="2">
      ${rest.map((r, i) => `
        <li style="--i:${i + 1}"><a href="${r.href}">
          <span class="hp-rk">${i + 2}</span>
          ${hpArt(r.cover, r.ver, r.title)}
          <span class="hp-rt"><b>${esc(r.title)}</b><span>${esc(r.sub || "")}</span></span>
          <span class="hp-rc" data-count="${r.plays}" data-delay="${(i + 1) * 40}">${hpFmt(r.plays)}</span>
          <i class="hp-rbar" style="--w:${((r.plays / max) * 100).toFixed(1)}%"></i>
        </a></li>`).join("")}
      ${Array.from({ length: Math.max(0, 9 - rest.length) }, () => `<li class="hp-pad" aria-hidden="true"><a><span class="hp-rk"></span><span class="hp-art"></span><span class="hp-rt"><b>&nbsp;</b><span></span></span></a></li>`).join("")}
    </ol>`;
}

// ---- the page ----------------------------------------------------------
// Phone: the screen you're on (overview, activity, most played, record buys) is kept in this history
// step, so Back from a page you opened returns you to it. Tapping Home is a new step: the top.
function hpRememberScreen() {
  const scroller = document.getElementById("hp-scroller");
  if (!scroller || getComputedStyle(scroller).overflowY === "visible") return;  // desktop: the page itself scrolls
  const pages = [...scroller.querySelectorAll(".hp-page")];
  const topOf = (el) => el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const at = history.state?.hpPage;
  if (at && pages[at]) {
    scroller.style.scrollSnapType = "none";  // straight there, no animated snap from the top
    scroller.scrollTop = topOf(pages[at]);
    requestAnimationFrame(() => { scroller.style.scrollSnapType = ""; });
  }
  let t = null;
  scroller.addEventListener("scroll", () => {
    clearTimeout(t);
    t = setTimeout(() => {
      if (document.body.dataset.route !== "home" || history.state?.drawer != null) return;
      const y = scroller.scrollTop;
      const i = pages.reduce((best, pg, k) => (Math.abs(topOf(pg) - y) < Math.abs(topOf(pages[best]) - y) ? k : best), 0);
      if ((history.state?.hpPage || 0) !== i) history.replaceState({ ...history.state, hpPage: i }, "", location.href);
    }, 150);
  }, { passive: true });
}
// On a phone the home page is its own full-height scroller (so the browser's bars stay put and every page is
// exactly one screen): its height is the window less the top bar and the bottom tab bar, measured.
function hpMeasureChrome() {
  const top = document.querySelector(".topbar")?.offsetHeight || 0;
  const bar = document.querySelector(".tabbar");
  const bottom = bar && getComputedStyle(bar).display !== "none" ? bar.offsetHeight : 0;
  document.documentElement.style.setProperty("--hp-chrome", `${top + bottom}px`);
}

// ---- the pager: ‹ This week › -- the middle opens a calendar to pick any dates ---------------------
// (on a phone it also holds Day…All, which the desktop header shows beside it)
const hpNavIcon = (d) => `<svg class="hp-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="${d === "back" ? "M15 5l-7 7 7 7" : "M9 5l7 7-7 7"}"/></svg>`;
const hpCalIcon = '<svg class="hp-ic hp-nav-cal" viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15.5" rx="1.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>';
const hpCanGoBack = (r) => { const f = hpFirstPlay(); return !!(r.start && f && r.start > f); };
const hpCanGoOn = (r) => !!(r.end && r.end <= new Date());   // nothing to see past now
const hpPeriodSeg = (cls = "") => hpSegHtml(`hp-period ${cls}`, HP_PERIODS, Object.fromEntries(HP_PERIODS.map((k) => [k, HP_WINDOWS[k].label])), hpState.win, { label: "Period" });
function hpNavHtml() {
  return `<div class="hp-nav" role="group" aria-label="Which period">
    <button type="button" class="hp-nav-btn" data-step="back" aria-label="Earlier" title="Earlier (← key)">${hpNavIcon("back")}</button>
    <button type="button" class="hp-nav-txt" data-pick aria-haspopup="dialog" aria-expanded="false" title="Pick dates">
      <span class="hp-nav-lbl" aria-live="polite"></span>${hpCalIcon}${hpChevron}
    </button>
    <button type="button" class="hp-nav-btn" data-step="next" aria-label="Later" title="Later (→ key)">${hpNavIcon("next")}</button>
    <div class="hp-pop" role="dialog" aria-label="Pick a period" hidden>
      ${hpPeriodSeg("hp-pop-sizes")}
      <div class="hp-cal"></div>
    </div>
  </div>`;
}
function hpSyncControls() {
  const r = hpRange(hpState.win);
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  app.querySelectorAll(".hp-nav").forEach((n) => {
    n.querySelector('[data-step="back"]').disabled = !hpCanGoBack(r);
    n.querySelector('[data-step="next"]').disabled = !hpCanGoOn(r);
    n.querySelector(".hp-nav-lbl").innerHTML = `<b>${esc(cap(r.tag))}</b>${r.dates ? `<span>${esc(r.dates)}</span>` : ""}`;
  });
  // the size switches (a picked range lights none of them)
  app.querySelectorAll(".hp-seg.hp-period").forEach((s) => {
    let moved = false;
    s.querySelectorAll("button").forEach((x) => {
      const on = x.dataset.k === hpState.win;
      if ((x.getAttribute("aria-pressed") === "true") !== on) moved = true;
      x.setAttribute("aria-pressed", String(on));
    });
    if (moved) hpSegPlace(s);
  });
}
// show the period in hpState: both halves swap, every control follows
function hpRefresh() {
  const act = document.getElementById("hp-act-body"), most = document.getElementById("hp-most-body");
  if (!act || !most) return;
  const r = hpRange(hpState.win);
  hpSwap(act, hpActivityHtml(r), () => hpWireActivity(act, r));
  hpSwap(most, hpMostHtml(hpState.kind, r));
  hpSyncControls();
}
function hpStep(how) {
  const r = hpRange(hpState.win);
  if (how === "back" ? !hpCanGoBack(r) : !hpCanGoOn(r)) return;
  if (hpState.win === "custom") {   // a picked range moves by its own length
    const b = hpBounds("custom", how === "back" ? 1 : -1);
    hpState.custom = { start: b.start, end: b.end };
  } else hpState.offset = Math.max(0, hpState.offset + (how === "back" ? 1 : -1));
  hpRefresh();
}
// a new size keeps your place: stepped back to Nov 2025, Year shows 2025 (a picked range: where it starts)
function hpSetPeriod(win) {
  const was = hpRange(hpState.win);
  hpState.offset = (was.offset || was.win === "custom") && was.start ? hpOffsetFor(win, was.start) : 0;
  hpState.win = win;
  hpPickClose();
  hpRefresh();
}

// ---- the calendar: tap the first day, then the last (or the same one again for a single day) ----
const hpPick = { nav: null, view: null, a: null, b: null, hover: null };
function hpPickOpen(nav) {
  if (hpPick.nav === nav) return hpPickClose();
  hpPickClose();
  const r = hpRange(hpState.win);
  const today = new Date();
  // start from the period on show (its days already marked), or this month
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const a = r.start ? new Date(r.start) : null;
  const b = r.end ? new Date(Math.min(+hpAddDays(r.end, -1), +todayStart)) : null;   // this month: up to today
  const shown = b && b < today ? b : today;
  Object.assign(hpPick, { nav, view: new Date(shown.getFullYear(), shown.getMonth(), 1), a, b, hover: null });
  const pop = nav.querySelector(".hp-pop");
  pop.hidden = false;
  nav.querySelector("[data-pick]").setAttribute("aria-expanded", "true");
  hpCalRender();
  pop.querySelectorAll(".hp-seg").forEach((s) => hpSegPlace(s, true));   // measured now that it shows
}
function hpPickClose() {
  if (!hpPick.nav) return;
  hpPick.nav.querySelector(".hp-pop").hidden = true;
  hpPick.nav.querySelector("[data-pick]").setAttribute("aria-expanded", "false");
  hpPick.nav = null;
}
function hpCalRender() {
  const host = hpPick.nav?.querySelector(".hp-cal");
  if (!host) return;
  const { view, a, b, hover } = hpPick;
  const y = view.getFullYear(), m = view.getMonth();
  const now = new Date(), today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const f = hpFirstPlay(), firstDay = f ? new Date(f.getFullYear(), f.getMonth(), f.getDate()) : today;
  const end = b || (a && hover) || a;
  const lo = a && end ? (a <= end ? a : end) : null, hi = a && end ? (a <= end ? end : a) : null;
  const lead = (new Date(y, m, 1).getDay() + 6) % 7, dim = new Date(y, m + 1, 0).getDate();
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push("<span></span>");
  for (let d = 1; d <= dim; d++) {
    const day = new Date(y, m, d), t = +day;
    const off = day > today || day < firstDay;
    const cls = [lo && t >= +lo && t <= +hi && "in", lo && t === +lo && "lo", hi && t === +hi && "hi", t === +today && "today"].filter(Boolean).join(" ");
    cells.push(`<button type="button" class="${cls}" data-day="${hpYmd(day)}"${off ? " disabled" : ""}>${d}</button>`);
  }
  const n = lo ? hpDaysBetween(lo, hi) + 1 : 0;
  const note = !a ? "Tap the first day" : !b ? `From ${hpDay(a)} ${a.getFullYear()}: now the last day` : n === 1 ? `${hpDay(lo)} ${lo.getFullYear()}` : `${hpSpanLabel(lo, hi)} · ${n} days`;
  host.innerHTML = `
    <div class="hp-cal-head">
      <button type="button" class="hp-cal-mv" data-mv="-1" aria-label="Previous month"${new Date(y, m, 1) <= firstDay ? " disabled" : ""}>${hpNavIcon("back")}</button>
      <b>${HP_MONTHS[m]} ${y}</b>
      <button type="button" class="hp-cal-mv" data-mv="1" aria-label="Next month"${new Date(y, m + 1, 1) > today ? " disabled" : ""}>${hpNavIcon("next")}</button>
    </div>
    <div class="hp-cal-dow">${["M", "T", "W", "T", "F", "S", "S"].map((x) => `<span>${x}</span>`).join("")}</div>
    <div class="hp-cal-grid">${cells.join("")}</div>
    <div class="hp-cal-foot">
      <span class="hp-cal-note">${esc(note)}</span>
      <button type="button" class="hp-cal-btn" data-cal="cancel">Cancel</button>
      <button type="button" class="hp-cal-btn go" data-cal="show"${a ? "" : " disabled"}>Show</button>
    </div>`;
}
function hpCalWire(nav) {
  const cal = nav.querySelector(".hp-cal");
  const at = (ymd) => { const [y, m, d] = ymd.split("-").map(Number); return new Date(y, m - 1, d); };
  cal.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.dataset.mv) {
      hpPick.view = new Date(hpPick.view.getFullYear(), hpPick.view.getMonth() + Number(btn.dataset.mv), 1);
    } else if (btn.dataset.day) {
      const d = at(btn.dataset.day);
      if (!hpPick.a || hpPick.b) Object.assign(hpPick, { a: d, b: null });
      else Object.assign(hpPick, { b: d });
    } else if (btn.dataset.cal === "cancel") return hpPickClose();
    else if (btn.dataset.cal === "show") {
      const { a } = hpPick, b = hpPick.b || a;
      const lo = a <= b ? a : b, hi = a <= b ? b : a;
      hpState.win = "custom";
      hpState.custom = { start: lo, end: hpAddDays(lo, hpDaysBetween(lo, hi) + 1) };
      hpState.offset = 0;
      hpPickClose();
      return hpRefresh();
    }
    hpCalRender();
  });
  // with a mouse, the range follows the pointer once the first day is picked
  cal.addEventListener("mouseover", (e) => {
    const day = e.target.closest("[data-day]")?.dataset.day;
    if (!day || !hpPick.a || hpPick.b || hpPick.hover && hpYmd(hpPick.hover) === day) return;
    hpPick.hover = at(day);
    hpCalRender();
  });
}

function renderHome() {
  app.classList.add("wide");
  const o = hpOverview();
  const since = o.s.first_play ? new Date(o.s.first_play) : null;
  const range = hpRange(hpState.win);
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
          <div class="hp-listen-ctrls">${hpNavHtml()}${hpPeriodSeg()}</div>
        </div>
        <div class="hp-listen-body">
          <section class="hp-page hp-activity" id="hp-activity" aria-label="Listening activity">
            <h2 class="hp-h2">Listening activity</h2>
            <div class="hp-ctrl hp-ctrl-phone">${hpNavHtml()}</div>
            <div class="hp-swap" id="hp-act-body">${hpActivityHtml(range)}</div>
            <a class="hp-cue" href="#hp-most" data-scroll="hp-most">Most played ${hpChevron}</a>
          </section>

          <section class="hp-page hp-most" id="hp-most" aria-label="Most played">
            <h2 class="hp-h2">Most played</h2>
            <div class="hp-card">
              <div class="hp-ctrl hp-ctrl-stack">
                ${hpSegHtml("hp-kind", Object.keys(HP_KINDS), HP_KINDS, hpState.kind, { icons: { artists: "person", albums: "album", songs: "note" }, label: "Show" })}
                <div class="hp-ctrl-phone">${hpNavHtml()}</div>
              </div>
              <div class="hp-swap hp-mp" id="hp-most-body">${hpMostHtml(hpState.kind, range)}</div>
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
  hpWireActivity(act, range);
  hpSegWire("hp-period", (k) => hpSetPeriod(k));
  hpSegWire("hp-kind", (k) => { hpState.kind = k; hpSwap(most, hpMostHtml(k, hpRange(hpState.win))); });
  hpPick.nav = null;   // a fresh page: no calendar open
  app.querySelectorAll(".hp-nav").forEach((n) => {
    n.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || b.disabled || b.closest(".hp-pop")) return;
      if (b.dataset.step) hpStep(b.dataset.step);
      else if (b.hasAttribute("data-pick")) hpPickOpen(n);
    });
    hpCalWire(n);
  });
  hpSyncControls();
  // ← → step through time too (not while typing, and not with a modifier: those belong to the browser)
  if (!window._hpKeys) {
    window._hpKeys = true;
    // the calendar closes on a tap outside it, or Escape
    document.addEventListener("pointerdown", (e) => { if (hpPick.nav && !hpPick.nav.contains(e.target)) hpPickClose(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && hpPick.nav) { hpPickClose(); return; }
      if ((e.key !== "ArrowLeft" && e.key !== "ArrowRight") || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (document.body.dataset.route !== "home" || !document.getElementById("hp-act-body")) return;
      if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
      e.preventDefault();
      hpStep(e.key === "ArrowLeft" ? "back" : "next");
    });
  }

  hpRememberScreen();

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
