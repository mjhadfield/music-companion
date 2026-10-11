// ---------------------------------------------------------------------
// The artist page (#/artist/<id>). Built from the album page's parts (hero,
// stat boxes, cards, cover rows -- css/album-page.css) plus its own
// (css/artist-page.css, ar- prefix).
// ---------------------------------------------------------------------
const artistPageState = { songsAll: false, discoSort: "plays" };

function renderArtist(id) {
  const artist = query(`SELECT * FROM artists WHERE id = ?`, [id])[0];
  if (!artist) return renderNotFound("Artist");
  const token = renderToken;
  // the About: stored by maintenance (artist_about -- fetched once, edited by hand); an artist without a
  // row yet falls back to the live lookup (the slow one: asked for first, filled in when it lands)
  const stored = storedAbout(id);
  const enrichment = stored ? Promise.resolve(null) : getArtistEnrichment(artist);

  const hist = query(`SELECT count(*) AS n, min(played_at) AS first, max(played_at) AS last FROM scrobbles WHERE artist_id = ?`, [id])[0];
  const listened = listenTime("WHERE s.artist_id = ?", [id]);   // "≈ 84 h"
  const rank = hist.n ? query(`SELECT count(*) + 1 AS r FROM (SELECT artist_id FROM scrobbles GROUP BY artist_id HAVING count(*) > ?)`, [hist.n])[0].r : null;
  const artistCount = query(`SELECT count(DISTINCT artist_id) AS c FROM scrobbles`)[0].c;
  const byYear = Object.fromEntries(query(`SELECT strftime('%Y', played_at) AS y, count(*) AS c FROM scrobbles WHERE artist_id = ? GROUP BY y`, [id]).map((r) => [r.y, r.c]));
  const years = [];
  if (hist.first) for (let y = +hist.first.slice(0, 4); y <= new Date().getFullYear(); y++) years.push({ y: String(y), c: byYear[y] || 0 });
  const yearPeak = years.reduce((m, y) => (y.c > (m?.c || 0) ? y : m), null);
  const yearMax = Math.max(1, ...years.map((y) => y.c));

  // their albums (credited, played or owned), most played first
  const albums = query(`
    SELECT al.id, al.title, al.year, al.cover_status, al.cover_updated_at,
      (SELECT count(*) FROM scrobbles s WHERE s.album_id = al.id) AS plays,
      (SELECT count(*) FROM vinyl_holdings v WHERE v.album_id = al.id) AS vinyl
    FROM album_artists aa JOIN albums al ON al.id = aa.album_id WHERE aa.artist_id = ?`, [id]).filter((a) => a.plays || a.vinyl);
  albums.sort((a, b) => b.plays - a.plays);
  const albumIds = new Set(albums.map((a) => a.id));
  const covers = albums.filter((a) => a.cover_status === "ok").slice(0, 4);

  const songs = query(`
    SELECT so.id, so.title, al.title AS album, count(*) AS plays
    FROM scrobbles s JOIN songs so ON so.id = s.song_id LEFT JOIN albums al ON al.id = so.album_id
    WHERE s.artist_id = ? GROUP BY so.id ORDER BY plays DESC`, [id]);
  const liveCount = {};
  for (const r of query(`SELECT ss.song_id, count(DISTINCT ss.setlist_id) AS n FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id WHERE so.artist_id = ? GROUP BY ss.song_id`, [id])) liveCount[r.song_id] = r.n;

  const shows = query(`
    SELECT sl.id, sl.event_date, sl.tour_name, v.name AS venue, v.city, v.id AS venue_id,
      (SELECT count(*) FROM setlist_songs ss WHERE ss.setlist_id = sl.id) AS songs
    FROM setlists sl LEFT JOIN venues v ON v.id = sl.venue_id WHERE sl.artist_id = ? ORDER BY sl.event_date DESC`, [id]);
  const records = loadCollection().filter((r) => r.artist_id === id || albumIds.has(r.album_id) || r.parts.some((x) => albumIds.has(x.album_id))).sort((a, b) => (a.year || 9999) - (b.year || 9999));

  // similar artists: the most of their genres in common, rarer genres counting for more; ones you've played
  const myGenres = query(`SELECT genre_id FROM artist_genres WHERE artist_id = ?`, [id]).map((r) => r.genre_id);
  let similar = [];
  if (myGenres.length) {
    const marks = myGenres.map(() => "?").join(",");
    const artistsWith = Object.fromEntries(query(`SELECT genre_id, count(*) AS n FROM artist_genres WHERE genre_id IN (${marks}) GROUP BY genre_id`, myGenres).map((r) => [r.genre_id, r.n]));
    const total = query(`SELECT count(DISTINCT artist_id) AS n FROM artist_genres`)[0].n || 1;
    similar = query(`
      SELECT ar.id, ar.name, group_concat(DISTINCT ag.genre_id) AS gids, group_concat(DISTINCT ge.name) AS genres,
        (SELECT count(*) FROM scrobbles s WHERE s.artist_id = ar.id) AS plays
      FROM artist_genres ag JOIN artists ar ON ar.id = ag.artist_id JOIN genres ge ON ge.id = ag.genre_id
      WHERE ag.genre_id IN (${marks}) AND ar.id != ? AND lower(ar.name) NOT IN ('various', 'various artists')
      GROUP BY ar.id HAVING plays > 0`, [...myGenres, id]);
    similar.forEach((a) => { const g = String(a.gids).split(",").map(Number); a.shared = g.length; a.score = g.reduce((n, x) => n + Math.log(1 + total / (artistsWith[x] || 1)), 0); });
    similar.sort((a, b) => b.score - a.score || b.plays - a.plays);
    similar = similar.slice(0, 8);
    similar.forEach((a) => { a.cover = query(`SELECT al.id, al.cover_status, al.cover_updated_at, al.title FROM scrobbles s JOIN albums al ON al.id = s.album_id
      WHERE s.artist_id = ? AND al.cover_status = 'ok' GROUP BY al.id ORDER BY count(*) DESC LIMIT 1`, [a.id])[0]; });
  }

  const st = artistPageState;
  const maxSong = songs[0]?.plays || 1;
  const songRows = (list, offset = 0) => list.map((s, i) => `
    <li data-song="${s.id}">
      <span class="ap-pos">${offset + i + 1}</span>
      <span class="ar-st"><b>${esc(s.title)}${liveCount[s.id] ? ` <i class="livedot" title="Heard live at ${plural(liveCount[s.id], "show")}">●</i>` : ""}</b>${s.album ? `<span>${esc(s.album)}</span>` : ""}</span>
      <span class="ap-plays">${apFmt(s.plays)}</span>
      <i class="ap-bar" style="--w:${(s.plays / maxSong).toFixed(3)}"></i>
    </li>`).join("");
  const disco = [...albums].sort(st.discoSort === "year" ? (a, b) => (a.year || 9999) - (b.year || 9999) || b.plays - a.plays : (a, b) => b.plays - a.plays);
  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  const span = shows.length ? `${shows[shows.length - 1].event_date.slice(0, 4)}${shows[0].event_date.slice(0, 4) !== shows[shows.length - 1].event_date.slice(0, 4) ? `–${shows[0].event_date.slice(0, 4)}` : ""}` : "";

  app.classList.add("wide");
  app.innerHTML = `
    <div class="ap ar">
      <section class="ap-hero">
        <div class="ar-mosaic ar-n${covers.length >= 4 ? 4 : covers.length ? 1 : 0}">${covers.length >= 4
          ? covers.map((c) => apCover(c.id, c.cover_status, c.cover_updated_at, c.title, "")).join("")
          : covers.length ? apCover(covers[0].id, "ok", covers[0].cover_updated_at, covers[0].title, "") : `<div class="ap-noart">${esc(artist.name.replace(/^the\s+/i, "").slice(0, 1))}</div>`}</div>
        <div class="ap-info">
          <div class="hud">Artist${albums.length ? ` · ${plural(albums.length, "album")}` : ""}</div>
          <h1 class="ap-title">${esc(artist.name)}</h1>
          ${stored ? aboutFactsHtml(stored) : ""}
          ${genreTagsHtml(artistGenreNames(id), "", "artists")}
          <div class="ap-kpis">
            ${kpi(apFmt(hist.n), `plays${listened ? ` · ${listened}` : ""}`, "accent")}
            ${kpi(rank ? `#${apFmt(rank)}` : "—", rank ? `of ${apFmt(artistCount)} artists` : "not played yet")}
            ${kpi(hist.first ? apMonthYear(hist.first) : "—", "first played")}
            ${kpi(hist.last ? relativeDay(hist.last) : "—", "last played")}
          </div>
          <div class="ap-links">
            ${records.length ? `<a class="ap-chip vinyl" href="#/vinyl?q=${encodeURIComponent(artist.name)}">● ${plural(records.length, "record")}</a>` : ""}
            ${shows.length ? `<a class="ap-chip live" href="#ar-live" data-scroll="ar-live">● Seen live ${shows.length}×</a>` : ""}
            ${artist.mbid ? `<a class="ap-chip" href="https://musicbrainz.org/artist/${artist.mbid}" target="_blank" rel="noopener">MusicBrainz ↗</a>` : ""}
            ${stored ? aboutLinkChips(stored) : `<span data-role="wiki-chip"></span>`}
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">
          ${songs.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Top songs</h2><span class="subtle">${plural(songs.length, "song")} played</span></div>
            <ol class="ap-tracks ar-songs">${songRows(songs.slice(0, st.songsAll ? songs.length : 10))}</ol>
            ${songs.length > 10 ? `<button class="ar-more" data-act="songs">${st.songsAll ? "Show the top 10" : `Show all ${apFmt(songs.length)}`}</button>` : ""}
          </section>` : ""}

          ${albums.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Discography</h2><div class="ar-sort">${[["plays", "Most played"], ["year", "By year"]].map(([k, t]) =>
              `<button data-disco="${k}" aria-pressed="${st.discoSort === k}">${t}</button>`).join("")}</div></div>
            <div class="ar-disco">${disco.map((a) => `<a href="#/album/${a.id}" title="${esc(a.title)}">${apCover(a.id, a.cover_status, a.cover_updated_at, a.title, "ap-more-art")}
              <b>${esc(a.title)}</b><span class="subtle">${a.vinyl ? `<span class="vinyl-dot" title="On vinyl">●</span> ` : ""}${a.plays ? `${apFmt(a.plays)} plays` : "not played"}</span>${a.year ? `<span class="subtle">${a.year}</span>` : ""}</a>`).join("")}</div>
          </section>` : ""}

          ${shows.length ? `<section class="ap-card" id="ar-live">
            <div class="ap-head"><h2>Seen live</h2><span class="subtle">${plural(shows.length, "show")}${span ? `, ${span}` : ""}</span></div>
            <ol class="ar-shows">${shows.map((s) => `<li><a href="#/setlist/${s.id}">
              <span class="ar-date"><b>${+s.event_date.slice(8, 10)} ${AP_MON[+s.event_date.slice(5, 7) - 1]}</b>${esc(s.event_date.slice(0, 4))}</span>
              <span class="ar-venue"><b>${esc(s.venue || "Unknown venue")}</b><span>${esc([s.city, s.tour_name].filter(Boolean).join(" · "))}</span></span>
              <span class="subtle">${s.songs ? plural(s.songs, "song") : ""}</span></a></li>`).join("")}</ol>
          </section>` : ""}
        </div>

        <aside class="ap-side">
          ${hist.n ? `<section class="ap-card">
            <div class="ap-head"><h2>Your listening</h2>${yearPeak ? `<span class="subtle">most in ${esc(yearPeak.y)}</span>` : ""}</div>
            <div class="ap-years">${years.map((y) => `<i class="${y === yearPeak ? "pk" : ""}" style="--h:${y.c ? Math.max(6, (y.c / yearMax) * 100).toFixed(1) : 0}%" title="${esc(y.y)}: ${apFmt(y.c)} plays"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>${esc(years[0]?.y || "")}</span><span class="pk">${yearPeak ? `${apFmt(yearPeak.c)} in ${esc(yearPeak.y)}` : ""}</span><span>${esc(years[years.length - 1]?.y || "")}</span></div>
          </section>` : ""}
          ${stored ? aboutCardHtml(stored) : `<div data-role="about"></div>`}
          ${records.length ? `<section class="ap-card">
            <div class="ap-head"><h2>On your shelf</h2><a class="subtle" href="#/vinyl?q=${encodeURIComponent(artist.name)}">in the collection →</a></div>
            <ol class="ar-records">${records.map((r) => `<li><a href="#/vinyl/${r.id}">${coverImg(r, "ar-rec-art")}
              <span><b>${esc(r.title)}</b><span class="subtle">${[r.year, r.reissue ? `${r.pressing_year || ""} ${r.reissueWord}`.trim() : "original press", r.grade?.short].filter(Boolean).map(esc).join(" · ")}</span></span>
              ${starsHtml(r.rating)}</a></li>`).join("")}</ol>
          </section>` : ""}
        </aside>
      </div>

      ${similar.length ? `<section class="ap-card ap-shelf-card">
        <div class="ap-head"><h2>Similar artists</h2><span class="subtle">by shared genres</span></div>
        <div class="ap-shelf">${similar.map((a) => `<a href="#/artist/${a.id}" title="${esc(a.genres.split(",").map(genreName).join(" · "))}">
          ${a.cover ? apCover(a.cover.id, "ok", a.cover.cover_updated_at, a.cover.title, "ap-more-art") : `<div class="ap-more-art ap-noart">${esc(a.name.replace(/^the\s+/i, "").slice(0, 1))}</div>`}
          <b>${esc(a.name)}</b><span class="subtle">${apFmt(a.plays)} plays · ${a.shared} in common</span></a>`).join("")}</div>
      </section>` : ""}
    </div>`;

  app.querySelectorAll("[data-song]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.song}`; }));
  app.querySelector("[data-act='songs']")?.addEventListener("click", () => { st.songsAll = !st.songsAll; renderArtist(id); });
  app.querySelectorAll("[data-disco]").forEach((b) => b.addEventListener("click", () => { st.discoSort = b.dataset.disco; renderArtist(id); }));
  app.querySelectorAll("[data-scroll]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); document.getElementById(a.dataset.scroll)?.scrollIntoView({ behavior: "smooth" }); }));

  if (stored) wireAboutCard();
  enrichment.then((info) => {
    if (token !== renderToken || !info) return;
    if (info.pageUrl) app.querySelector("[data-role='wiki-chip']").outerHTML = `<a class="ap-chip" href="${esc(info.pageUrl)}" target="_blank" rel="noopener">Wikipedia ↗</a>`;
    if (!info.extract) return;
    app.querySelector("[data-role='about']").outerHTML = aboutCardHtml({ text: info.extract, source: "wikipedia", wiki_url: info.pageUrl, thumbnail_url: info.thumbnail });
    wireAboutCard();
  });
}

// -- About ------------------------------------------------------------------------------------
function storedAbout(id) {
  if (!hasTable("artist_about")) return null;
  return query(`SELECT * FROM artist_about WHERE artist_id = ?`, [id])[0] || null;
}
// "Finnish melodic death metal band · formed 1997 · Joensuu, Finland" (MusicBrainz)
function aboutFactsHtml(a) {
  const tag = a.tagline ? a.tagline.charAt(0).toUpperCase() + a.tagline.slice(1) : "";
  const where = [a.begin_area, a.area].filter((x, i, l) => x && l.indexOf(x) === i).join(", ");
  const when = a.formed ? `${a.type === "Person" ? "born" : "formed"} ${a.formed.slice(0, 4)}${a.ended ? `, ${a.type === "Person" ? "died" : "split"} ${a.ended.slice(0, 4)}` : ""}` : "";
  const bits = [tag, when, where].filter(Boolean);
  return bits.length ? `<div class="ar-tagline subtle">${bits.map(esc).join(" · ")}</div>` : "";
}
const ABOUT_CHIPS = ["Wikipedia", "Official site", "Bandcamp"];
function aboutLinkChips(a) {
  const links = a.links_json ? JSON.parse(a.links_json) : {};
  if (a.wiki_url) links.Wikipedia = a.wiki_url;
  return ABOUT_CHIPS.filter((k) => links[k]).map((k) => `<a class="ap-chip" href="${esc(links[k])}" target="_blank" rel="noopener">${esc(k)} ↗</a>`).join("");
}
function aboutCardHtml(a) {
  if (!a.text) return "";
  const credit = a.source === "wikipedia" ? `From Wikipedia, <a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noopener">CC BY-SA</a>`
    : a.source === "lastfm" ? "From Last.fm (user-contributed, CC BY-SA)" : "";
  return `<section class="ap-card ar-about">
    <div class="ap-head"><h2>About</h2>${a.wiki_url ? `<a class="subtle" href="${esc(a.wiki_url)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ""}</div>
    <div class="ar-about-body">${a.thumbnail_url ? `<img src="${esc(a.thumbnail_url)}" alt="" />` : ""}<p>${esc(a.text)}</p></div>
    <button class="ar-more" data-act="about">Read more</button>
    ${credit ? `<div class="ar-credit subtle">${credit}</div>` : ""}
  </section>`;
}
function wireAboutCard() {
  const card = app.querySelector(".ar-about");
  if (!card) return;
  const p = card.querySelector("p");
  if (p.scrollHeight <= p.clientHeight + 2) card.querySelector("[data-act='about']").remove();
  card.querySelector("[data-act='about']")?.addEventListener("click", (e) => { card.classList.toggle("open"); e.target.textContent = card.classList.contains("open") ? "Less" : "Read more"; });
}
