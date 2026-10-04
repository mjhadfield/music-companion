// ---------------------------------------------------------------------
// The album page (#/album/<id>). Borrows from the home page (the stat
// boxes, mini charts, ranked bars) and the Vinyl page (your copies).
// Styles: css/album-page.css (ap- prefix).
// ---------------------------------------------------------------------
const apFmt = (n) => Number(n || 0).toLocaleString();
const AP_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const apMonthYear = (iso) => { const d = new Date(iso); return `${AP_MON[d.getMonth()]} ${d.getFullYear()}`; };

function apCover(albumId, status, version, title, cls = "") {
  return status === "ok"
    ? `<img class="${cls}" src="${esc(coverUrl(albumId, version))}" alt="" loading="lazy" />`
    : `<div class="${cls} ap-noart">${esc(String(title || "?").replace(/^the\s+/i, "").slice(0, 1))}</div>`;
}

// the tracklist split where the record splits: sides of your pressing (A1, B2…) or discs of MusicBrainz's release
function apGroups(ref, matched, albumId) {
  if (ref.pressing) {
    const groups = [];
    for (const t of matched.tracks) {
      const side = (String(t.pos || "").match(/^[A-Z]+/) || [""])[0];
      const g = groups[groups.length - 1];
      if (g && g.key === side) g.tracks.push(t); else groups.push({ key: side, label: side ? `Side ${side}` : "", tracks: [t] });
    }
    return groups.length > 1 ? groups : [{ key: "", label: "", tracks: matched.tracks }];
  }
  const discs = query(`SELECT disc FROM album_tracklists WHERE album_id = ? ORDER BY position`, [albumId]).map((r) => r.disc || 1);
  const groups = [];
  matched.tracks.forEach((t, i) => {
    const d = discs[i] || 1;
    const g = groups[groups.length - 1];
    if (g && g.key === d) g.tracks.push(t); else groups.push({ key: d, label: `Disc ${d}`, tracks: [t] });
  });
  return groups.length > 1 ? groups : [{ key: "", label: "", tracks: matched.tracks }];
}

function apTracklistHtml(albumId, songs) {
  const ref = albumReferenceTracklist(albumId);
  const max = Math.max(1, ...songs.map((x) => x.plays));
  const row = (t, n) => {
    const s = t.again ? null : t.song;
    const plays = s ? s.plays : 0;
    return `<li class="${s && plays ? "" : "ap-unplayed"}"${t.song ? ` data-song="${t.song.id}"` : ""}>
      <span class="ap-pos">${esc(t.pos || String(n))}</span>
      <span class="ap-tt">${esc(t.title)}${s?.shows ? ` <i class="livedot" title="Heard live at ${apFmt(s.shows)} show${s.shows === 1 ? "" : "s"}">●</i>` : ""}</span>
      <span class="ap-plays">${t.again ? `<span title="Counted on its first line above">↑</span>` : plays ? apFmt(plays) : "—"}</span>
      <span class="ap-dur">${esc(t.dur || "")}</span>
      ${plays ? `<i class="ap-bar" style="--w:${(plays / max).toFixed(3)}"></i>` : ""}
    </li>`;
  };
  if (!ref) {
    return songs.length ? `<section class="ap-card"><div class="ap-head"><h2>Songs you've played</h2><span class="subtle">no tracklist for this album yet</span></div>
      <ol class="ap-tracks">${songs.map((x, i) => row({ pos: "", title: x.title, song: x }, i + 1)).join("")}</ol></section>` : "";
  }
  const matched = matchTracklist(ref.tracks, songs, albumId);
  const groups = apGroups(ref, matched, albumId);
  // another-language version beside your pressing (Carolus Rex: Swedish LP + English version); a recording on both counts once
  const version = ref.pressing ? versionTracklist(albumId) : null;
  const other = version ? matchTracklist(version.tracks, songs, albumId, version.variant) : null;
  const onMain = new Set(songs.filter((x) => !matched.unlisted.includes(x)).map((x) => x.id));
  if (other) other.tracks.forEach((t) => { if (t.song && onMain.has(t.song.id)) t.again = true; });
  const unlisted = other ? matched.unlisted.filter((x) => other.unlisted.includes(x)) : matched.unlisted;
  // the count leaves out a folded-away disc (the DVD of a CD + DVD edition)
  const counted = groups.filter((g) => groups.length === 1 || g.tracks.some((t) => t.song && t.song.plays)).flatMap((g) => g.tracks);
  const played = counted.filter((t) => t.song && !t.again && t.song.plays).length;
  let n = 0;
  return `<section class="ap-card">
    <div class="ap-head"><h2>Tracklist</h2><span class="subtle">${apFmt(played)} of ${apFmt(counted.length)} played · from ${esc(ref.source)}</span></div>
    ${groups.map((g) => {
      const any = g.tracks.some((t) => t.song && t.song.plays);
      const body = `<ol class="ap-tracks">${g.tracks.map((t) => row(t, ++n)).join("")}</ol>`;
      // a disc you've never played from (the DVD of a CD + DVD edition) folds away
      return !any && groups.length > 1
        ? `<details class="ap-group ap-quiet"><summary>${esc(g.label)} <span class="subtle">· ${g.tracks.length} tracks, none played</span></summary>${body}</details>`
        : `${g.label ? `<div class="ap-group-label">${esc(g.label)}</div>` : ""}${body}`;
    }).join("")}
    ${other ? `<div class="ap-group-label">${esc(version.label)} <span class="subtle">· the same album in another language</span></div>
      <ol class="ap-tracks">${other.tracks.map((t) => row(t, ++n)).join("")}</ol>` : ""}
    ${(() => {  // played from the album but not on this tracklist: folded into one bar
      const extra = unlisted.filter((x) => x.plays);
      return extra.length ? `<details class="ap-group ap-quiet ap-extras"><summary>Deluxe &amp; digital extras <span class="subtle">· ${plural(extra.length, "track")}, ${plural(extra.reduce((n, x) => n + x.plays, 0), "play")}</span></summary>
        <ol class="ap-tracks">${extra.map((x) => row({ pos: "+", title: x.title, song: x }, 0)).join("")}</ol></details>` : "";
    })()}
  </section>`;
}

function renderAlbum(id) {
  const album = query(`SELECT al.*, ar.name AS artist_name, ar.id AS artist_id, ar.mbid AS artist_mbid FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?`, [id])[0];
  if (!album) return renderNotFound("Album");
  const scope = albumScope(id), marks = scope.map(() => "?").join(",");
  const credits = query(`SELECT ar.id, ar.name FROM album_artists aa JOIN artists ar ON ar.id = aa.artist_id WHERE aa.album_id = ? ORDER BY aa.position`, [id]);
  const sets = albumSetsContaining(id);
  const copies = loadCollection().filter((r) => r.album_id === id || sets.includes(r.album_id));
  const parts = scope.slice(1).map((pid) => query("SELECT id, title, year FROM albums WHERE id = ?", [pid])[0]).filter(Boolean);
  const songs = albumSongGroups(id);
  const genres = parts.length && !albumGenreNames(id).length ? [...new Set(parts.flatMap((p) => albumGenreNames(p.id)))] : albumGenreNames(id);

  const hist = query(`SELECT count(*) AS n, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE album_id IN (${marks})`, scope)[0];
  const byYear = Object.fromEntries(query(`SELECT strftime('%Y', played_at) AS y, count(*) AS c FROM scrobbles WHERE album_id IN (${marks}) GROUP BY y`, scope).map((r) => [r.y, r.c]));
  const years = [];  // every year from the first play to now, quiet ones included
  if (hist.first) for (let y = +hist.first.slice(0, 4); y <= new Date().getFullYear(); y++) years.push({ y: String(y), c: byYear[y] || 0 });
  // where it ranks among the artist's albums, and its share of their plays
  const theirs = query(`SELECT al.id, al.title, al.year, al.cover_status, al.cover_updated_at,
      (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
      (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) AS vinyl
    FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE aa.artist_id = ? ORDER BY plays DESC, al.year`, [album.artist_id]);
  const ranked = theirs.filter((a) => a.plays);
  const rank = ranked.findIndex((a) => a.id === id) + 1;
  const artistPlays = query(`SELECT count(*) AS c FROM scrobbles WHERE artist_id = ?`, [album.artist_id])[0].c;
  const more = theirs.filter((a) => a.id !== id && a.plays >= 10).slice(0, 6);  // albums you've really played (10+)

  // live: the album's songs you've heard at shows
  const live = songs.filter((x) => x.shows);
  const showIds = [...new Set(live.flatMap((x) => [...x.setlists]))];
  const shows = showIds.length ? query(`SELECT sl.id, sl.event_date, v.name AS venue, v.city, ar.name AS performer FROM setlists sl LEFT JOIN venues v ON v.id = sl.venue_id
      JOIN artists ar ON ar.id = sl.artist_id WHERE sl.id IN (${showIds.map(() => "?").join(",")}) ORDER BY sl.event_date`, showIds) : [];
  const lastShow = shows[shows.length - 1] || null;

  // similar albums: other artists' albums sharing the most of this one's genres (one per artist), ones you've played or own
  const genreIds = query(`SELECT genre_id FROM album_genres WHERE album_id IN (${marks})`, scope).map((r) => r.genre_id);
  const similar = [];
  if (genreIds.length) {
    // a shared genre counts for more the rarer it is in your library ("rock and roll" says more than "rock")
    const albumsWith = Object.fromEntries(query(`SELECT genre_id, count(*) AS n FROM album_genres WHERE genre_id IN (${genreIds.map(() => "?").join(",")}) GROUP BY genre_id`, genreIds).map((r) => [r.genre_id, r.n]));
    const total = query(`SELECT count(DISTINCT album_id) AS n FROM album_genres`)[0].n || 1;
    const weight = (gid) => Math.log(1 + total / (albumsWith[gid] || 1));
    const cands = query(`
        SELECT al.id, al.title, al.year, al.cover_status, al.cover_updated_at, ar.id AS artist_id, ar.name AS artist,
               group_concat(DISTINCT ag.genre_id) AS gids, group_concat(DISTINCT ge.name) AS genres,
               (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
               (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) AS vinyl
        FROM album_genres ag JOIN albums al ON al.id = ag.album_id JOIN artists ar ON ar.id = al.artist_id JOIN genres ge ON ge.id = ag.genre_id
        WHERE ag.genre_id IN (${genreIds.map(() => "?").join(",")}) AND al.artist_id NOT IN (SELECT artist_id FROM album_artists WHERE album_id = ?)
          AND lower(ar.name) NOT IN ('various', 'various artists')   -- a compilation isn't "another artist"
        GROUP BY al.id HAVING plays > 0 OR vinyl > 0`, [...genreIds, id]);
    cands.forEach((a) => { const g = String(a.gids).split(",").map(Number); a.shared = g.length; a.score = g.reduce((n, x) => n + weight(x), 0); });
    cands.sort((a, b) => b.score - a.score || b.plays - a.plays);
    const seen = new Set();
    for (const a of cands) {
      if (seen.has(a.artist_id)) continue;
      seen.add(a.artist_id);
      similar.push(a);
      if (similar.length === 8) break;
    }
  }

  const yearPeak = years.reduce((m, y) => (y.c > (m?.c || 0) ? y : m), null);
  const yearMax = Math.max(1, ...years.map((y) => y.c));
  // heard live: one row per song, one column per show (a year per column when there are many), a dot where it was played
  const liveHtml = () => {
    if (!live.length) return "";
    const byYear = shows.length > 12;
    const cols = byYear ? [...new Set(shows.map((s) => s.event_date.slice(0, 4)))].map((y) => ({ key: y, label: `’${y.slice(2)}`,
        tip: `${y}: ${shows.filter((s) => s.event_date.startsWith(y)).map((s) => s.venue).join(", ")}`, ids: shows.filter((s) => s.event_date.startsWith(y)).map((s) => s.id) }))
      : shows.map((s) => ({ key: s.id, label: `’${s.event_date.slice(2, 4)}`, tip: `${s.event_date} · ${s.performer} · ${[s.venue, s.city].filter(Boolean).join(", ")}`, ids: [s.id], href: `#/setlist/${s.id}` }));
    const span = shows.length ? `${shows[0].event_date.slice(0, 4)}${shows[shows.length - 1].event_date.slice(0, 4) !== shows[0].event_date.slice(0, 4) ? `–${shows[shows.length - 1].event_date.slice(0, 4)}` : ""}` : "";
    return `<section class="ap-card">
      <div class="ap-head"><h2>Heard live</h2><span class="subtle">${plural(live.length, "song")} at ${plural(shows.length, "show")}${span ? `, ${span}` : ""}</span></div>
      <div class="ap-livegrid" style="--cols:${cols.length}">
        <span></span>${cols.map((c) => c.href ? `<a class="ap-lg-col" href="${c.href}" title="${esc(c.tip)}">${esc(c.label)}</a>` : `<span class="ap-lg-col" title="${esc(c.tip)}">${esc(c.label)}</span>`).join("")}<span></span>
        ${live.sort((a, b) => b.shows - a.shows || b.plays - a.plays).map((x) => `<span class="ap-lg-song" data-song="${x.id}">${esc(x.title)}</span>${cols.map((c) => {
          const n = c.ids.filter((sid) => x.setlists.has(sid)).length;
          return `<span class="ap-lg-cell${n ? " on" : ""}" title="${esc(c.tip)}">${n ? (byYear && n > 1 ? n : "●") : ""}</span>`;
        }).join("")}<span class="ap-lg-n">${x.shows}×</span>`).join("")}
      </div>
      ${lastShow ? `<a class="ap-lastshow" href="#/setlist/${lastShow.id}">Last heard ${esc(apMonthYear(lastShow.event_date))} · ${esc(lastShow.performer)} at ${esc([lastShow.venue, lastShow.city].filter(Boolean).join(", "))} →</a>` : ""}
    </section>`;
  };
  // a row of album covers across the page
  const shelf = (title, link, albums, sub) => albums.length ? `<section class="ap-card ap-shelf-card">
      <div class="ap-head"><h2>${title}</h2>${link || ""}</div>
      <div class="ap-shelf">${albums.map((a) => `<a href="#/album/${a.id}" title="${esc(a.title)}${a.artist ? ` — ${esc(a.artist)}` : ""}">${apCover(a.id, a.cover_status, a.cover_updated_at, a.title, "ap-more-art")}
        <b>${esc(a.title)}</b><span class="subtle">${sub(a)}</span></a>`).join("")}</div></section>` : "";
  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  const yearText = album.year || parts.map((p) => p.year).filter(Boolean)[0] || "";

  app.classList.add("wide");
  app.innerHTML = `
    <div class="ap">
      <section class="ap-hero">
        ${apCover(album.id, album.cover_status, album.cover_updated_at, album.title, "ap-cover")}
        <div class="ap-info">
          <div class="hud">${parts.length ? "A set of albums" : "Album"}${yearText ? ` · ${yearText}` : ""}</div>
          <h1 class="ap-title">${esc(album.title)}</h1>
          <div class="ap-artist">${credits.map((c) => `<a href="#/artist/${c.id}">${esc(c.name)}</a>`).join(" · ")}</div>
          ${parts.length ? `<div class="subtle">A set of ${parts.map((p) => `<a href="#/album/${p.id}">${esc(p.title)}</a>${p.year ? ` (${p.year})` : ""}`).join(" + ")}</div>` : ""}
          ${genreTagsHtml(genres)}
          <div class="ap-kpis">
            ${kpi(apFmt(hist.n), "plays", "accent")}
            ${kpi(rank ? `#${rank}` : "—", rank ? `of ${ranked.length} albums` : "not played yet")}
            ${kpi(hist.first ? apMonthYear(hist.first) : "—", "first played")}
            ${kpi(hist.last ? relativeDay(hist.last) : "—", "last played")}
          </div>
          <div class="ap-links">
            ${copies.length ? `<a class="ap-chip vinyl" href="#/vinyl/${copies[0].id}">● On vinyl${copies.length > 1 ? ` ×${copies.length}` : ""}</a>` : `<span class="ap-chip muted">Not in your collection</span>`}
            ${album.mbid ? `<a class="ap-chip" href="https://musicbrainz.org/release-group/${album.mbid}" target="_blank" rel="noopener">MusicBrainz ↗</a>` : ""}
            ${copies[0]?.discogs_release_id ? `<a class="ap-chip" href="https://www.discogs.com/release/${copies[0].discogs_release_id}" target="_blank" rel="noopener">Discogs ↗</a>` : ""}
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">${apTracklistHtml(id, songs) || `<section class="ap-card"><p class="subtle">Nothing played from this album yet, and no tracklist.</p></section>`}${liveHtml()}</div>
        <aside class="ap-side">
          ${hist.n ? `<section class="ap-card">
            <div class="ap-head"><h2>Your listening</h2>${artistPlays ? `<span class="subtle">${Math.round((hist.n / artistPlays) * 100)}% of your ${esc(album.artist_name)} plays</span>` : ""}</div>
            <div class="ap-years">${years.map((y) => `<i class="${y === yearPeak ? "pk" : ""}" style="--h:${y.c ? Math.max(6, (y.c / yearMax) * 100).toFixed(1) : 0}%" title="${esc(y.y)}: ${apFmt(y.c)} plays"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>${esc(years[0]?.y || "")}</span><span class="pk">${yearPeak ? `${apFmt(yearPeak.c)} in ${esc(yearPeak.y)}` : ""}</span><span>${esc(years[years.length - 1]?.y || "")}</span></div>
          </section>` : ""}

          ${copies.length ? `<section class="ap-card">
            <div class="ap-head"><h2>${copies.length > 1 ? "Your copies" : "Your copy"}</h2><a class="subtle" href="#/vinyl">the collection →</a></div>
            ${copies.map((r) => `<a class="ap-copy" href="#/vinyl/${r.id}">
              ${r.album_id !== id ? `<div class="pc-set">Part of the set <b>${esc(r.title)}</b></div>` : ""}
              <div class="ap-copy-top">${r.cover_file ? coverImg(r, "ap-copy-art") : ""}<div>
                <div class="rd-line"><b>${esc(r.label || "Unknown label")}</b>${r.country ? ` · ${esc(r.country)}` : ""}${r.pressing_year ? ` · ${r.pressing_year}` : ""} ${starsHtml(r.rating)}</div>
                <div class="pbadges">${r.reissue ? `<span class="pbadge">${esc(r.reissueWord)}</span>` : `<span class="pbadge og">Original press</span>`}${pressingBadges(r)}</div></div></div>
              ${meter(r.grade, "Record")}${meter(r.sleeveGrade, "Sleeve")}
              ${r.notes ? `<div class="subtle pc-note">“${esc(r.notes)}”</div>` : ""}
            </a>`).join("")}
          </section>` : ""}

          ${more.length ? `<section class="ap-card">
            <div class="ap-head"><h2>More by ${esc(album.artist_name)}</h2><a class="subtle" href="#/artist/${album.artist_id}">all →</a></div>
            <div class="ap-more">${more.map((a) => `<a href="#/album/${a.id}" title="${esc(a.title)}">${apCover(a.id, a.cover_status, a.cover_updated_at, a.title, "ap-more-art")}
              <b>${esc(a.title)}</b><span class="subtle">${a.year || ""}${a.year && a.plays ? " · " : ""}${a.plays ? `${apFmt(a.plays)} plays` : ""}${a.vinyl ? ` <span class="vinyl-dot" title="On vinyl">●</span>` : ""}</span></a>`).join("")}</div>
          </section>` : ""}
        </aside>
      </div>
      ${shelf("Similar albums from other artists", `<span class="subtle">by shared genres</span>`, similar,
        (a) => `<span title="${esc(a.genres.split(",").map(genreName).join(" · "))}">${esc(a.artist)} · ${a.shared} in common</span>${a.vinyl ? ` <span class="vinyl-dot" title="On vinyl">●</span>` : ""}`)}
    </div>`;

  app.querySelectorAll("[data-song]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.song}`; }));
}
