// ---------------------------------------------------------------------
// The song page (#/song/<id>). Built from the album page's parts
// (css/album-page.css) and the artist page's (css/artist-page.css), plus its
// own (css/song-page.css, sp- prefix).
// ---------------------------------------------------------------------
const SP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function spVariantLabel(v) {
  if (!v) return "Studio";
  if (v.startsWith("lang-")) return `${v[5].toUpperCase()}${v.slice(6)} version`;
  return v[0].toUpperCase() + v.slice(1);
}

function renderSong(id) {
  const song = query(`SELECT so.*, ar.name AS artist_name FROM songs so JOIN artists ar ON ar.id = so.artist_id WHERE so.id = ?`, [id])[0];
  if (!song) return renderNotFound("Song");

  // the song is every row of it not merged yet ("War Pigs" + "War Pigs - 2009 Remaster"); a live or
  // remix recording is another version, shown beside it
  const key = trackKey(song.title), base = normTitle(song.title);
  const theirs = query(`SELECT so.id, so.title, so.album_id, (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays FROM songs so WHERE so.artist_id = ?`, [song.artist_id]);
  const rows = theirs.filter((x) => x.id === id || trackKey(x.title) === key);
  const ids = rows.map((x) => x.id), marks = ids.map(() => "?").join(",");
  const versionsByKey = new Map();
  for (const x of theirs) {
    if (ids.includes(x.id) || !base || normTitle(x.title) !== base) continue;
    const k = trackKey(x.title);
    const v = versionsByKey.get(k) || { id: x.id, title: x.title, top: -1, variant: variantOf(x.title), plays: 0, shows: 0 };
    v.plays += x.plays;
    if (x.plays > v.top) Object.assign(v, { id: x.id, title: x.title, top: x.plays });  // named after its most played row
    versionsByKey.set(k, v);
  }
  const versions = [...versionsByKey.values()].sort((a, b) => b.plays - a.plays);
  versions.forEach((v) => { v.shows = query(`SELECT count(DISTINCT setlist_id) AS n FROM setlist_songs WHERE song_id = ?`, [v.id])[0].n; });

  const plays = query(`SELECT played_at FROM scrobbles WHERE song_id IN (${marks}) ORDER BY played_at`, ids).map((r) => r.played_at);
  const n = plays.length, first = plays[0], last = plays[n - 1];
  // where it ranks among the artist's songs (rows grouped the same way)
  const byKey = {};
  theirs.forEach((x) => { const k = trackKey(x.title); byKey[k] = (byKey[k] || 0) + x.plays; });
  const rank = n ? Object.values(byKey).filter((p) => p > n).length + 1 : null;
  const songCount = Object.values(byKey).filter((p) => p > 0).length;
  const artistPlays = theirs.reduce((s, x) => s + x.plays, 0);

  const byYear = {};
  const hours = Array(24).fill(0), days = Array(7).fill(0);
  for (const p of plays) {
    const d = new Date(p);
    byYear[d.getFullYear()] = (byYear[d.getFullYear()] || 0) + 1;
    hours[d.getHours()] += 1;
    days[(d.getDay() + 6) % 7] += 1;
  }
  const years = [];
  if (first) for (let y = new Date(first).getFullYear(); y <= new Date().getFullYear(); y++) years.push({ y: String(y), c: byYear[y] || 0 });
  const yearPeak = years.reduce((m, y) => (y.c > (m?.c || 0) ? y : m), null);
  const yearMax = Math.max(1, ...years.map((y) => y.c));
  const hourMax = Math.max(1, ...hours), dayMax = Math.max(1, ...days);
  const peakHour = hours.indexOf(Math.max(...hours));
  const partOfDay = (h) => (h < 5 ? "late at night" : h < 12 ? "in the morning" : h < 17 ? "in the afternoon" : h < 22 ? "in the evening" : "late at night");

  // its album: where it's filed, else where you play it from most
  const playedFrom = query(`SELECT al.id, al.title, al.year, al.cover_status, al.cover_updated_at, count(*) AS plays
    FROM scrobbles s JOIN albums al ON al.id = s.album_id WHERE s.song_id IN (${marks}) GROUP BY al.id ORDER BY plays DESC`, ids);
  const albumId = song.album_id || playedFrom[0]?.id || null;
  const album = albumId ? query(`SELECT al.*, ar.name AS artist_name FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?`, [albumId])[0] : null;
  let track = null, trackHtml = "";
  if (album) {
    const groups = albumSongGroups(album.id);
    const ref = albumReferenceTracklist(album.id);
    const mine = (s) => s && [...s.rowIds].some((x) => ids.includes(x));
    if (ref) {
      const matched = matchTracklist(ref.tracks, groups, album.id);
      const at = matched.tracks.findIndex((t) => mine(t.song) && !t.again);
      if (at >= 0) track = { pos: matched.tracks[at].pos, n: at + 1, of: matched.tracks.length, dur: matched.tracks[at].dur, pressing: ref.pressing };
      const max = Math.max(1, ...groups.map((g) => g.plays));
      const row = (t, i) => {
        const s = t.again ? null : t.song, p = s ? s.plays : 0, here = i === at;
        return `<li class="${here ? "sp-here" : p ? "" : "ap-unplayed"}"${t.song && !here ? ` data-song="${t.song.id}"` : ""}>
          <span class="ap-pos">${esc(t.pos || String(i + 1))}</span><span class="ap-tt">${esc(t.title)}</span>
          <span class="ap-plays">${p ? apFmt(p) : "—"}</span><span class="ap-dur">${esc(t.dur || "")}</span>
          ${p ? `<i class="ap-bar" style="--w:${(p / max).toFixed(3)}"></i>` : ""}</li>`;
      };
      // split by side or disc, as on the album page; a disc you've never played from folds away
      let i = 0;
      trackHtml = apGroups(ref, matched, album.id).map((g, _, all) => {
        const start = i; i += g.tracks.length;
        const body = `<ol class="ap-tracks sp-album-tracks">${g.tracks.map((t, k) => row(t, start + k)).join("")}</ol>`;
        const quiet = all.length > 1 && !g.tracks.some((t) => t.song && t.song.plays) && !(at >= start && at < i);
        return quiet ? `<details class="ap-group ap-quiet"><summary>${esc(g.label)} <span class="subtle">· ${g.tracks.length} tracks, none played</span></summary>${body}</details>`
          : `${g.label ? `<div class="ap-group-label">${esc(g.label)}</div>` : ""}${body}`;
      }).join("") + `
      <div class="sp-src subtle">Tracklist from ${esc(ref.source)}</div>`;
    }
  }
  const trackWord = track ? (track.pressing && /^[A-Z]+\d+$/.test(track.pos || "") ? `Side ${track.pos.match(/^[A-Z]+/)[0]}, track ${track.pos.match(/\d+$/)[0]}` : `Track ${track.n}`) : null;
  const record = album ? loadCollection().find((r) => r.album_id === album.id || r.parts.some((p) => p.album_id === album.id)) : null;

  // heard live: every show it was played at, where in the set
  const live = query(`
    SELECT sl.id, sl.event_date, sl.tour_name, v.name AS venue, v.city, ar.id AS performer_id, ar.name AS performer, ss.position, ss.set_name, ss.is_cover, ss.cover_of_artist_text,
      (SELECT count(*) FROM setlist_songs x WHERE x.setlist_id = sl.id) AS total,
      (SELECT min(position) FROM setlist_songs x WHERE x.setlist_id = sl.id) AS lo,
      (SELECT max(position) FROM setlist_songs x WHERE x.setlist_id = sl.id) AS hi,
      (SELECT count(*) FROM setlist_songs x WHERE x.setlist_id = sl.id AND x.position < ss.position) + 1 AS nth
    FROM setlist_songs ss JOIN setlists sl ON sl.id = ss.setlist_id JOIN artists ar ON ar.id = sl.artist_id LEFT JOIN venues v ON v.id = sl.venue_id
    WHERE ss.song_id IN (${marks}) ORDER BY sl.event_date DESC`, ids);
  const showsSeen = new Set(live.map((r) => r.id)).size;
  const artistShows = query(`SELECT count(*) AS n FROM setlists WHERE artist_id = ?`, [song.artist_id])[0].n;
  const opened = live.filter((r) => r.position === r.lo).length, closed = live.filter((r) => r.position === r.hi).length;

  // often played alongside: other artists' songs within half an hour of it, most often first
  const alongside = n ? query(`
    SELECT s2.song_id AS id, so.title, ar.id AS artist_id, ar.name AS artist, count(DISTINCT s1.id) AS together,
      (SELECT al.id || '|' || coalesce(al.cover_updated_at, '') FROM scrobbles s3 JOIN albums al ON al.id = s3.album_id WHERE s3.song_id = s2.song_id AND al.cover_status = 'ok' LIMIT 1) AS cover
    FROM scrobbles s1 JOIN scrobbles s2 ON s2.played_at BETWEEN strftime('%Y-%m-%dT%H:%M:%S+00:00', s1.played_at, '-30 minutes') AND strftime('%Y-%m-%dT%H:%M:%S+00:00', s1.played_at, '+30 minutes')
    JOIN songs so ON so.id = s2.song_id JOIN artists ar ON ar.id = so.artist_id
    WHERE s1.song_id IN (${marks}) AND so.artist_id != ? AND lower(ar.name) NOT IN ('various', 'various artists')
    GROUP BY s2.song_id ORDER BY together DESC, so.title LIMIT 8`, [...ids, song.artist_id]).filter((x) => x.together >= 2) : [];

  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  // its length: the song's own, else the tracklist line it was matched to
  const lenMs = hasColumn("songs", "length_ms") ? query(`SELECT max(length_ms) AS l FROM songs WHERE id IN (${marks})`, ids)[0].l : null;
  const lenText = lenMs ? fmtLength(lenMs) : track?.dur || "";
  const listened = listenTime(`WHERE s.song_id IN (${marks})`, ids);
  const tooShort = !n && (secsOf(lenText) || 99) <= TOO_SHORT_SECS;
  const hud = ["Song", trackWord && album ? `${trackWord} on ${album.title}` : null, lenText || null].filter(Boolean).join(" · ");
  const variant = variantOf(song.title);

  app.classList.add("wide");
  app.innerHTML = `
    <div class="ap sp">
      <section class="ap-hero">
        ${album ? `<a href="#/album/${album.id}">${apCover(album.id, album.cover_status, album.cover_updated_at, album.title, "ap-cover")}</a>` : `<div class="ap-cover ap-noart">${esc(song.title.slice(0, 1))}</div>`}
        <div class="ap-info">
          <div class="hud">${esc(hud)}</div>
          <h1 class="ap-title">${esc(song.title)}</h1>
          <div class="ap-artist"><a href="#/artist/${song.artist_id}">${esc(song.artist_name)}</a>${album ? ` <span class="subtle">·</span> <a class="sp-from" href="#/album/${album.id}">${esc(album.title)}</a>${album.year ? ` <span class="subtle">(${album.year})</span>` : ""}` : ""}</div>
          ${album ? genreTagsHtml(albumGenreNames(album.id), "", "songs") : ""}
          <div class="ap-kpis">
            ${kpi(tooShort ? "—" : apFmt(n), tooShort ? "too short to scrobble" : `plays${listened ? ` · ${listened}` : ""}`, "accent")}
            ${kpi(rank ? `#${apFmt(rank)}` : "—", rank ? `of ${apFmt(songCount)} ${song.artist_name} songs` : "not played yet")}
            ${kpi(first ? apMonthYear(first) : "—", "first played")}
            ${kpi(last ? relativeDay(last) : "—", "last played")}
          </div>
          <div class="ap-links">
            ${record ? `<a class="ap-chip vinyl" href="#/vinyl/${record.id}">● On vinyl</a>` : `<span class="ap-chip muted">Not on vinyl</span>`}
            ${live.length ? `<a class="ap-chip live" href="#sp-live" data-scroll="sp-live">● Heard live ${showsSeen}×</a>` : ""}
            ${variant ? `<span class="ap-chip">${esc(spVariantLabel(variant))}</span>` : ""}
            ${song.mbid ? `<a class="ap-chip" href="https://musicbrainz.org/recording/${song.mbid}" target="_blank" rel="noopener">MusicBrainz ↗</a>` : ""}
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">
          ${album ? `<section class="ap-card">
            <div class="ap-head"><h2>On the album</h2><a class="subtle" href="#/album/${album.id}">${esc(album.title)} →</a></div>
            ${trackHtml || `<div class="subtle">No tracklist for ${esc(album.title)} yet.</div>`}
          </section>` : ""}

          ${live.length ? `<section class="ap-card" id="sp-live">
            <div class="ap-head"><h2>Heard live</h2><span class="subtle">${plural(showsSeen, "show")}${(() => { const own = new Set(live.filter((r) => r.performer_id === song.artist_id).map((r) => r.id)).size;
              return artistShows > 1 ? ` · ${own === artistShows ? "every one" : apFmt(own)} of your ${apFmt(artistShows)} ${esc(song.artist_name)} shows` : ""; })()}</span></div>
            ${opened || closed ? `<div class="sp-livefacts">${opened ? `<span>Opened ${plural(opened, "show")}</span>` : ""}${closed ? `<span>Closed ${plural(closed, "show")}</span>` : ""}</div>` : ""}
            <ol class="ar-shows">${live.map((r) => {
              const where = r.position === r.lo ? "Opener" : r.position === r.hi ? "Closer" : /encore/i.test(r.set_name || "") ? "Encore" : null;
              return `<li><a href="#/setlist/${r.id}">
                <span class="ar-date"><b>${+r.event_date.slice(8, 10)} ${AP_MON[+r.event_date.slice(5, 7) - 1]}</b>${esc(r.event_date.slice(0, 4))}</span>
                <span class="ar-venue"><b>${esc(r.venue || "Unknown venue")}</b><span>${esc([r.performer_id !== song.artist_id ? r.performer : null, r.city, r.tour_name].filter(Boolean).join(" · "))}${r.is_cover ? " · as a cover" : ""}</span></span>
                <span class="sp-slot">${where ? `<i class="sp-tag">${where}</i>` : ""}<span class="subtle">${r.total ? `${r.nth} of ${r.total}` : ""}</span></span></a></li>`;
            }).join("")}</ol>
          </section>` : ""}

          ${versions.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Other versions</h2><span class="subtle">${plural(versions.length, "recording")} you've played or heard</span></div>
            <ol class="ap-tracks ar-songs">${versions.map((v) => `<li data-song="${v.id}">
              <span class="sp-vtag">${esc(spVariantLabel(v.variant))}</span>
              <span class="ar-st"><b>${esc(v.title)}${v.shows ? ` <i class="livedot" title="Heard live at ${plural(v.shows, "show")}">●</i>` : ""}</b></span>
              <span class="ap-plays">${v.plays ? apFmt(v.plays) : "—"}</span></li>`).join("")}</ol>
          </section>` : ""}
        </div>

        <aside class="ap-side">
          ${n ? `<section class="ap-card">
            <div class="ap-head"><h2>Your listening</h2>${yearPeak ? `<span class="subtle">most in ${esc(yearPeak.y)}</span>` : ""}</div>
            <div class="ap-years">${years.map((y) => `<i class="${y === yearPeak ? "pk" : ""}" style="--h:${y.c ? Math.max(6, (y.c / yearMax) * 100).toFixed(1) : 0}%" title="${esc(y.y)}: ${apFmt(y.c)} plays"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>${esc(years[0]?.y || "")}</span><span class="pk">${yearPeak ? `${apFmt(yearPeak.c)} in ${esc(yearPeak.y)}` : ""}</span><span>${esc(years[years.length - 1]?.y || "")}</span></div>
            ${artistPlays ? `<div class="sp-share"><span style="--w:${(n / artistPlays).toFixed(3)}"></span></div>
            <div class="subtle sp-share-cap">${(100 * n / artistPlays).toFixed(n / artistPlays < 0.1 ? 1 : 0)}% of your ${esc(song.artist_name)} plays</div>` : ""}
          </section>` : ""}

          ${n >= 5 ? `<section class="ap-card">
            <div class="ap-head"><h2>When you play it</h2><span class="subtle">mostly ${partOfDay(peakHour)}</span></div>
            <div class="sp-hours">${hours.map((c, h) => `<i class="${h === peakHour ? "pk" : ""}" style="--h:${c ? Math.max(6, (c / hourMax) * 100).toFixed(1) : 0}%" title="${String(h).padStart(2, "0")}:00 · ${plural(c, "play")}"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>
            <div class="sp-days">${days.map((c, d) => `<span title="${SP_DAYS[d]} · ${plural(c, "play")}"><i style="--a:${(c / dayMax).toFixed(3)}"></i>${SP_DAYS[d][0]}</span>`).join("")}</div>
          </section>` : ""}

          ${playedFrom.length > 1 || (playedFrom.length && playedFrom[0].id !== albumId) ? `<section class="ap-card">
            <div class="ap-head"><h2>Played from</h2><span class="subtle">${plural(playedFrom.length, "release")}</span></div>
            <ol class="ar-records">${playedFrom.map((a) => `<li><a href="#/album/${a.id}">${apCover(a.id, a.cover_status, a.cover_updated_at, a.title, "ar-rec-art")}
              <span><b>${esc(a.title)}</b><span class="subtle">${a.year || ""}</span></span><span class="ap-plays">${apFmt(a.plays)}</span></a></li>`).join("")}</ol>
          </section>` : ""}
        </aside>
      </div>

      ${alongside.length ? `<section class="ap-card ap-shelf-card">
        <div class="ap-head"><h2>Often played alongside</h2><span class="subtle">other artists, within half an hour of it</span></div>
        <div class="ap-shelf">${alongside.map((x) => { const [cid, cv] = String(x.cover || "").split("|");
          return `<a href="#/song/${x.id}" title="${esc(x.title)} — ${esc(x.artist)}">
          ${cid ? apCover(+cid, "ok", cv, x.title, "ap-more-art") : `<div class="ap-more-art ap-noart">${esc(x.title.slice(0, 1))}</div>`}
          <b>${esc(x.title)}</b><span class="subtle">${esc(x.artist)} · ${x.together}×</span></a>`; }).join("")}</div>
      </section>` : ""}
    </div>`;

  app.querySelectorAll("[data-song]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.song}`; }));
  app.querySelectorAll("[data-scroll]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); document.getElementById(a.dataset.scroll)?.scrollIntoView({ behavior: "smooth" }); }));
}
