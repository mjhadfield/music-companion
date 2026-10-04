// ---------------------------------------------------------------------
// Live: the shows hub (#/shows), its insights (#/shows/insights), a night out
// (#/event/<date>), one act's set (#/setlist/<id>) and a venue (#/venue/<id>).
// Styles: css/live.css (lv- prefix), plus the Vinyl page's parts (coll-, ins-)
// in the live colour.
//
// The unit is a night out -- every set you saw on one day (you're only ever
// in one place a day), its lineup inside. Where a night happened and what
// kind of night it was come from livePlaces() / liveData(): today derived
// (a festival's stages folded into its site, a festival weekend = days in
// a row at one place with a big lineup); when venues gain real links
// (name changes, stages -> site), festivals, capacity and type in the
// database, those two functions read them and everything else follows.
// ---------------------------------------------------------------------
const liveState = { view: "timeline", q: "", sort: "newest", kind: "all", facets: { year: new Set(), city: new Set(), place: new Set() }, filtersOpen: false };
const liveCache = { db: null };
const LV_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const lvDay = (date) => new Date(`${date}T12:00:00`);
const lvLongDate = (date) => { const d = lvDay(date); return `${LV_WEEKDAYS[d.getDay()]} ${d.getDate()} ${AP_MON[d.getMonth()]} ${d.getFullYear()}`; };
const lvDaysBetween = (a, b) => Math.round((lvDay(b) - lvDay(a)) / 86400000);

// venue -> the place it belongs to. A stage at a festival site ("Main Stage", Castle Donington) is
// part of that site. (Venue names over the years -- Carling / O2 Academy Brixton -- are left as they
// are until they're linked in maintenance.)
function livePlaces(venues) {
  const kindCol = hasColumn("venues", "kind"), capCol = hasColumn("venues", "capacity");
  const isStage = (v) => /\bstage\b/i.test(v.name || "");
  const out = {};
  for (const v of venues) {
    let parent = v;
    if (isStage(v)) {
      const site = venues.filter((x) => x.city === v.city && !isStage(x)).sort((a, b) => b.sets - a.sets)[0];
      if (site) parent = site;
    }
    out[v.id] = { id: parent.id, name: parent.name, city: parent.city, country: parent.country, stage: parent === v ? null : v.name,
      kind: kindCol ? parent.kind : null, capacity: capCol ? parent.capacity : null };
  }
  return out;
}

function liveData() {
  if (liveCache.db === db) return liveCache;
  const sets = query(`
    SELECT sl.id, sl.event_date AS date, sl.tour_name AS tour, sl.artist_id, ar.name AS artist, sl.venue_id, v.name AS venue, v.city, v.country,
      (SELECT count(*) FROM setlist_songs ss WHERE ss.setlist_id = sl.id) AS songs,
      (SELECT count(*) FROM setlist_songs ss WHERE ss.setlist_id = sl.id AND ss.is_cover) AS covers
    FROM setlists sl JOIN artists ar ON ar.id = sl.artist_id LEFT JOIN venues v ON v.id = sl.venue_id ORDER BY sl.event_date, sl.id`);
  const venues = query(`SELECT v.*, count(sl.id) AS sets FROM venues v LEFT JOIN setlists sl ON sl.venue_id = v.id GROUP BY v.id`);
  const places = livePlaces(venues);
  sets.forEach((s) => { s.place = places[s.venue_id] || { id: 0, name: "Unknown venue", city: s.city, country: s.country }; });

  // each artist's face: their most played album with a cover, else a covered album of theirs
  const artistIds = [...new Set(sets.map((s) => s.artist_id))];
  const marks = artistIds.map(() => "?").join(",");
  const faces = {}, plays = {};
  for (const r of query(`SELECT s.artist_id, s.album_id, al.cover_updated_at, count(*) AS c FROM scrobbles s JOIN albums al ON al.id = s.album_id
      WHERE al.cover_status = 'ok' AND s.artist_id IN (${marks}) GROUP BY s.artist_id, s.album_id`, artistIds)) {
    if (!faces[r.artist_id] || r.c > faces[r.artist_id].c) faces[r.artist_id] = { id: r.album_id, v: r.cover_updated_at, c: r.c };
  }
  for (const r of query(`SELECT aa.artist_id, al.id, al.cover_updated_at FROM album_artists aa JOIN albums al ON al.id = aa.album_id
      WHERE al.cover_status = 'ok' AND aa.artist_id IN (${marks})`, artistIds)) faces[r.artist_id] ||= { id: r.id, v: r.cover_updated_at, c: 0 };
  for (const r of query(`SELECT artist_id, count(*) AS c, min(played_at) AS first FROM scrobbles WHERE artist_id IN (${marks}) GROUP BY artist_id`, artistIds)) plays[r.artist_id] = r;

  // nights: every set on a day
  const byDate = new Map();
  for (const s of sets) (byDate.get(s.date) || byDate.set(s.date, []).get(s.date)).push(s);
  const events = [...byDate.entries()].map(([date, list]) => {
    list.sort((a, b) => b.songs - a.songs || a.artist.localeCompare(b.artist));  // the longest set first: usually the headliner
    const tally = {};
    list.forEach((s) => { tally[s.place.id] = (tally[s.place.id] || 0) + 1; });
    const place = list.find((s) => String(s.place.id) === Object.entries(tally).sort((a, b) => b[1] - a[1])[0][0]).place;
    return { date, year: date.slice(0, 4), sets: list, place, city: place.city, country: place.country, headliner: list[0],
      songs: list.reduce((n, s) => n + s.songs, 0), covers: list.reduce((n, s) => n + s.covers, 0), kind: "gig", festival: null };
  });
  // festivals: days in a row at one place with a lineup, or one day with five or more acts
  for (let i = 0; i < events.length;) {
    let j = i + 1;
    while (j < events.length && events[j].place.id === events[i].place.id && lvDaysBetween(events[j - 1].date, events[j].date) === 1) j++;
    const run = events.slice(i, j);
    const big = Math.max(...run.map((e) => e.sets.length));
    if ((run.length > 1 && big >= 3) || big >= 5) {
      const fest = { key: run[0].date, name: run[0].place.name, place: run[0].place, year: run[0].year, days: run.map((e) => e.date) };
      run.forEach((e, k) => Object.assign(e, { kind: "festival", festival: fest, day: k + 1 }));
    }
    i = j;
  }
  events.forEach((e, k) => { e.nth = k + 1; });
  Object.assign(liveCache, { db, sets, events, places, venues, faces, plays, byDate: Object.fromEntries(events.map((e) => [e.date, e])) });
  return liveCache;
}

const lvFace = (L, artistId, name, cls = "") => {
  const f = L.faces[artistId];
  return f ? `<img class="${cls}" src="${esc(coverUrl(f.id, f.v))}" alt="" loading="lazy" />` : `<div class="${cls} ap-noart">${esc(String(name || "?").replace(/^the\s+/i, "").slice(0, 1))}</div>`;
};
const lvTitle = (e) => (e.festival ? e.festival.name : e.headliner.artist);
const lvKindLabel = (e) => (e.festival ? (e.festival.days.length > 1 ? `Festival · day ${e.day} of ${e.festival.days.length}` : "Festival") : "Gig");
// a night in a list of nights at one place: who headlined (a festival's title would just repeat the place)
const lvWho = (e) => `${e.headliner.artist}${e.sets.length > 1 ? ` +${e.sets.length - 1}` : ""}`;
const lvSpan = (days) => (days >= 730 ? `${Math.round(days / 365.25)} years` : `${Math.round(days / 30.4)} months`);

// ---------------------------------------------------------------------
// The hub
// ---------------------------------------------------------------------
function liveFiltered(L, st, skip = null) {
  const q = st.q.toLowerCase();
  const setOk = (s) => !q || [s.artist, s.venue, s.place.name, s.city, s.tour].some((x) => String(x || "").toLowerCase().includes(q));
  const out = [];
  for (const e of L.events) {
    if (st.kind !== "all" && e.kind !== st.kind) continue;
    if (skip !== "year" && st.facets.year.size && !st.facets.year.has(e.year)) continue;
    if (skip !== "city" && st.facets.city.size && !st.facets.city.has(e.city)) continue;
    if (skip !== "place" && st.facets.place.size && !st.facets.place.has(e.place.name)) continue;
    const hits = e.sets.filter(setOk);
    if (hits.length) out.push({ ...e, hits });
  }
  return out;
}

function renderLiveHub() {
  app.classList.add("wide");
  setTopbarTitle("live", "Live shows");
  const st = liveState;
  const VIEWS = [["timeline", "▦ Timeline"], ["artists", "☺ Artists"], ["venues", "⌂ Venues"], ["songs", "♪ Songs"]];
  app.innerHTML = `
    <div class="lv">
      <div class="coll-head">
        <div class="coll-title"><div class="hud">live</div><h1>Live shows</h1></div>
        <div class="coll-actions">
          <a class="coll-btn" href="#/shows/insights" title="The bigger picture: records, your bucket list, discovered live, the live effect…">✦ Insights</a>
          <button class="coll-btn" data-role="random" title="Open a random night from what's showing">⟳ <span class="dig-word">Random </span>night</button>
          <div class="seg" role="tablist" aria-label="View">${VIEWS.map(([v, l]) => `<button role="tab" data-view="${v}" aria-selected="${st.view === v}">${l}</button>`).join("")}</div>
        </div>
      </div>
      <div class="coll-stats lv-stats" data-role="stats"></div>
      <div class="lv-years" data-role="years"></div>
      <div class="coll-tools">
        <input type="search" id="lv-search" placeholder="Search artist, venue, city, tour…" value="${esc(st.q)}" />
        <div class="seg lv-kind" aria-label="Kind">${[["all", "All"], ["gig", "Gigs"], ["festival", "Festivals"]].map(([k, l]) => `<button data-kind="${k}" aria-selected="${st.kind === k}">${l}</button>`).join("")}</div>
        <label data-role="sort-wrap">Sort <select data-role="sort"></select></label>
      </div>
      <div class="coll-filters${st.filtersOpen ? " open" : ""}">
        <button class="filters-bar" data-role="filters-bar" aria-expanded="${st.filtersOpen}"><span>Filters</span><span class="fb-active" data-role="fb-active"></span><svg class="i"><use href="#i-chevron"/></svg></button>
        <div class="coll-facets" data-role="facets"></div>
      </div>
      <div class="coll-count" data-role="count"></div>
      <div data-role="results"></div>
    </div>`;
  const $ = (r) => app.querySelector(`[data-role="${r}"]`);
  const L = liveData();
  const SORTS = {
    timeline: [["newest", "Newest first"], ["oldest", "Oldest first"], ["acts", "Biggest lineup"], ["songs", "Most songs"]],
    artists: [["seen", "Most seen"], ["songs", "Most songs heard"], ["recent", "Seen most recently"], ["plays", "Most played"], ["name", "A–Z"]],
    venues: [["nights", "Most nights"], ["recent", "Most recent"], ["name", "A–Z"]],
    songs: [["heard", "Most heard"], ["plays", "Most played by you"], ["never", "Heard live, never played"]],
  };

  const update = () => {
    const sorts = SORTS[st.view];
    if (!sorts.some(([k]) => k === st.sort)) st.sort = sorts[0][0];
    $("sort").innerHTML = sorts.map(([k, l]) => `<option value="${k}"${k === st.sort ? " selected" : ""}>${l}</option>`).join("");
    const shown = liveFiltered(L, st);
    const sets = shown.flatMap((e) => e.hits);
    renderLiveStats($("stats"), L, shown, sets);
    renderLiveYears($("years"), L, st, update);
    renderLiveFacets($("facets"), $("fb-active"), L, st, update);
    $("count").textContent = `${plural(shown.length, "night")} · ${plural(sets.length, "set")}`;
    const host = $("results");
    if (!shown.length) host.innerHTML = `<div class="empty">Nothing matches.</div>`;
    else if (st.view === "timeline") host.innerHTML = liveTimelineHtml(L, shown, st);
    else if (st.view === "artists") host.innerHTML = liveArtistsHtml(L, sets, st);
    else if (st.view === "venues") host.innerHTML = liveVenuesHtml(L, shown, st);
    else host.innerHTML = liveSongsHtml(sets, st);
    host.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", (e) => { if (!e.target.closest("a")) location.hash = el.dataset.href; }));
    host.querySelector("[data-act='more']")?.addEventListener("click", () => { st.songsShown = (st.songsShown || 60) + 120; update(); });
  };
  st.refresh = update;

  app.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { st.view = b.dataset.view; app.querySelectorAll("[data-view]").forEach((x) => x.setAttribute("aria-selected", x === b)); update(); }));
  app.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => { st.kind = b.dataset.kind; app.querySelectorAll("[data-kind]").forEach((x) => x.setAttribute("aria-selected", x === b)); update(); }));
  let t = null;
  app.querySelector("#lv-search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { st.q = e.target.value.trim(); update(); }, 150); });
  $("sort").addEventListener("change", (e) => { st.sort = e.target.value; update(); });
  $("filters-bar").addEventListener("click", () => {
    st.filtersOpen = !st.filtersOpen;
    app.querySelector(".coll-filters").classList.toggle("open", st.filtersOpen);
    $("filters-bar").setAttribute("aria-expanded", st.filtersOpen);
  });
  $("random").addEventListener("click", () => { const pool = liveFiltered(L, st); if (pool.length) location.hash = `#/event/${pool[Math.floor(Math.random() * pool.length)].date}`; });
  update();
}

function renderLiveStats(host, L, shown, sets) {
  const artists = new Set(sets.map((s) => s.artist_id)).size;
  const places = new Set(shown.map((e) => e.place.id)).size;
  const fests = new Set(shown.filter((e) => e.festival).map((e) => e.festival.key)).size;
  const songs = sets.reduce((n, s) => n + s.songs, 0);
  const gigs = shown.filter((e) => !e.festival).length;
  const years = {};
  shown.forEach((e) => { years[e.year] = (years[e.year] || 0) + 1; });
  const top = Object.entries(years).sort((a, b) => b[1] - a[1])[0];
  host.innerHTML = `
    <div class="cs"><b>${apFmt(shown.length)}</b><span>nights out</span></div>
    <div class="cs"><b>${apFmt(sets.length)}</b><span>sets seen</span></div>
    <div class="cs"><b>${apFmt(artists)}</b><span>artists</span></div>
    <div class="cs"><b>${apFmt(places)}</b><span>venues</span></div>
    <div class="cs"><b>${apFmt(fests)}</b><span>festivals</span></div>
    <div class="cs"><b>${apFmt(songs)}</b><span>songs heard</span></div>
    <div class="cs"${top ? ` title="${plural(top[1], "night")}"` : ""}><b>${top ? esc(top[0]) : "—"}</b><span>busiest year</span></div>
    <div class="cs cs-genres"><div class="split lv-split"><i style="flex:${gigs || 0.0001}"></i><i style="flex:${shown.length - gigs || 0.0001}"></i></div>
      <span>${apFmt(gigs)} gigs · ${apFmt(shown.length - gigs)} festival days</span></div>`;
}

// nights a year, every year from your first; festival days in full colour. A bar filters to its year.
function renderLiveYears(host, L, st, update) {
  const counts = {};
  liveFiltered(L, st, "year").forEach((e) => { const c = (counts[e.year] ||= { gig: 0, festival: 0 }); c[e.kind] += 1; });
  const first = +L.events[0].year, last = new Date().getFullYear();
  const max = Math.max(1, ...Object.values(counts).map((c) => c.gig + c.festival));
  const ys = [];
  for (let y = first; y <= last; y++) ys.push(String(y));
  host.innerHTML = ys.map((y) => {
    const c = counts[y] || { gig: 0, festival: 0 }, n = c.gig + c.festival, on = st.facets.year.has(y);
    return `<button class="lv-yr${on ? " on" : ""}${st.facets.year.size && !on ? " dim" : ""}" data-year="${y}" title="${y}: ${plural(c.gig, "gig")}, ${plural(c.festival, "festival day")}"${n ? "" : " disabled"}>
      <span class="lv-yr-n">${n || ""}</span>
      <span class="lv-yr-bar" style="--h:${((n / max) * 100).toFixed(1)}%"><i class="f" style="flex:${c.festival}"></i><i class="g" style="flex:${c.gig}"></i></span>
      <span class="lv-yr-l${+y % 4 ? " minor" : ""}">’${y.slice(2)}</span></button>`;
  }).join("");
  host.querySelectorAll("[data-year]").forEach((b) => b.addEventListener("click", () => {
    const y = b.dataset.year;
    st.facets.year.has(y) ? st.facets.year.delete(y) : st.facets.year.add(y);
    update();
  }));
}

function renderLiveFacets(host, active, L, st, update) {
  const facet = (key, title, fn, sortKey = false) => {
    const c = {};
    liveFiltered(L, st, key).forEach((e) => { const k = fn(e); if (k) c[k] = (c[k] || 0) + 1; });
    st.facets[key].forEach((k) => { c[k] ??= 0; });
    const entries = Object.entries(c).sort(sortKey ? (a, b) => b[0].localeCompare(a[0]) : (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    return `<div class="facet"><span class="f-title">${title}</span><div class="f-chips">${entries.map(([k, n]) =>
      `<button class="fchip${st.facets[key].has(k) ? " on" : ""}" data-f="${key}" data-v="${esc(k)}"${n || st.facets[key].has(k) ? "" : " disabled"}>${esc(k)} <i>${n}</i></button>`).join("")}</div></div>`;
  };
  host.innerHTML = facet("year", "Year", (e) => e.year, true) + facet("city", "City", (e) => e.city) + facet("place", "Venue", (e) => e.place.name);
  const on = Object.values(st.facets).reduce((n, s) => n + s.size, 0);
  active.innerHTML = on ? `<b>${on}</b>${esc(Object.values(st.facets).flatMap((s) => [...s]).join(" · "))}` : "";
  host.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => {
    const s = st.facets[b.dataset.f];
    s.has(b.dataset.v) ? s.delete(b.dataset.v) : s.add(b.dataset.v);
    update();
  }));
}

// Timeline: nights as cards, a year at a time
function liveTimelineHtml(L, shown, st) {
  const list = [...shown];
  const by = { newest: (a, b) => b.date.localeCompare(a.date), oldest: (a, b) => a.date.localeCompare(b.date),
    acts: (a, b) => b.sets.length - a.sets.length || b.date.localeCompare(a.date), songs: (a, b) => b.songs - a.songs || b.date.localeCompare(a.date) }[st.sort];
  list.sort(by);
  const grouped = st.sort === "newest" || st.sort === "oldest";
  const card = (e) => {
    const d = lvDay(e.date);
    const others = e.sets.slice(1).map((s) => s.artist);
    const lineup = e.festival ? `${plural(e.sets.length, "act")}: ${e.sets.slice(0, 6).map((s) => s.artist).join(" · ")}${e.sets.length > 6 ? ` +${e.sets.length - 6}` : ""}`
      : others.length ? `with ${others.join(", ")}` : e.headliner.tour || "";
    return `<div class="lv-ev${e.festival ? " fest" : ""}" data-href="#/event/${e.date}" role="link" tabindex="0">
      <div class="lv-date"><b>${d.getDate()}</b><span>${AP_MON[d.getMonth()]}</span><span>${LV_WEEKDAYS[d.getDay()].slice(0, 3)}</span>${grouped ? "" : `<span>${e.year}</span>`}</div>
      <div class="lv-ev-body">
        <div class="lv-kind">${esc(lvKindLabel(e))}</div>
        <div class="lv-ev-title">${esc(lvTitle(e))}</div>
        ${lineup ? `<div class="lv-lineup">${esc(lineup)}</div>` : ""}
        <div class="lv-where">${esc([e.festival && e.festival.name === e.place.name ? null : e.place.name, e.city].filter(Boolean).join(" · "))}</div>
        <div class="lv-ev-foot"><span class="lv-faces">${e.sets.slice(0, 6).map((s) => lvFace(L, s.artist_id, s.artist, "lv-face")).join("")}</span>
          <span class="subtle">${e.songs ? plural(e.songs, "song") : ""}</span></div>
      </div>
    </div>`;
  };
  if (!grouped) return `<div class="lv-grid">${list.map(card).join("")}</div>`;
  const years = [];
  for (const e of list) { const g = years[years.length - 1]; if (g && g.y === e.year) g.list.push(e); else years.push({ y: e.year, list: [e] }); }
  return years.map((g) => `<h3 class="coll-group">${g.y}<span>${plural(g.list.length, "night")} · ${plural(g.list.reduce((n, e) => n + e.sets.length, 0), "set")}</span></h3>
    <div class="lv-grid">${g.list.map(card).join("")}</div>`).join("");
}

// Artists: everyone you've seen, as covers
function liveArtistsHtml(L, sets, st) {
  const by = {};
  for (const s of sets) {
    const a = (by[s.artist_id] ||= { id: s.artist_id, name: s.artist, seen: 0, songs: 0, first: s.date, last: s.date, plays: L.plays[s.artist_id]?.c || 0 });
    a.seen += 1; a.songs += s.songs;
    if (s.date < a.first) a.first = s.date;
    if (s.date > a.last) a.last = s.date;
  }
  const list = Object.values(by).sort({
    seen: (a, b) => b.seen - a.seen || b.songs - a.songs, songs: (a, b) => b.songs - a.songs, recent: (a, b) => b.last.localeCompare(a.last),
    plays: (a, b) => b.plays - a.plays, name: (a, b) => a.name.localeCompare(b.name),
  }[st.sort]);
  return `<div class="lv-artists">${list.map((a) => `<a class="lv-artist" href="#/artist/${a.id}">
    ${lvFace(L, a.id, a.name, "lv-art")}
    <b>${esc(a.name)}</b>
    <span class="lv-seen">${a.seen > 1 ? `Seen ${a.seen}×` : "Seen once"}</span>
    <span class="subtle">${a.first.slice(0, 4)}${a.last.slice(0, 4) !== a.first.slice(0, 4) ? `–${a.last.slice(0, 4)}` : ""} · ${a.songs ? plural(a.songs, "song") : "no setlist"}</span>
  </a>`).join("")}</div>`;
}

// Venues: each place (a festival's stages inside it), nights there, who you saw
function liveVenuesHtml(L, shown, st) {
  const by = {};
  for (const e of shown) {
    const p = (by[e.place.id] ||= { ...e.place, nights: 0, sets: 0, first: e.date, last: e.date, artists: new Set(), stages: {}, fest: 0 });
    p.nights += 1; p.sets += e.hits.length;
    if (e.festival) p.fest += 1;
    if (e.date < p.first) p.first = e.date;
    if (e.date > p.last) p.last = e.date;
    e.hits.forEach((s) => { p.artists.add(s.artist); if (s.place.stage) p.stages[s.place.stage] = (p.stages[s.place.stage] || 0) + 1; });
  }
  const list = Object.values(by).sort({ nights: (a, b) => b.nights - a.nights || b.sets - a.sets, recent: (a, b) => b.last.localeCompare(a.last), name: (a, b) => a.name.localeCompare(b.name) }[st.sort]);
  const max = Math.max(1, ...list.map((p) => p.nights));
  return `<div class="lv-venues">${list.map((p) => `<div class="lv-venue" data-href="#/venue/${p.id}" role="link" tabindex="0">
    <div class="lv-v-main"><b>${esc(p.name)}</b><span class="subtle">${esc([p.city, p.country !== "United Kingdom" ? p.country : null].filter(Boolean).join(", "))}${p.kind ? ` · ${esc(p.kind)}` : ""}${p.capacity ? ` · ${apFmt(p.capacity)} capacity` : ""}</span>
      ${Object.keys(p.stages).length ? `<span class="lv-stages">${Object.entries(p.stages).sort((a, b) => b[1] - a[1]).map(([n, c]) => `<i>${esc(n)} ${c}</i>`).join("")}</span>` : ""}</div>
    <div class="lv-v-bar"><i style="--w:${(p.nights / max).toFixed(3)}"></i><span>${plural(p.nights, "night")}${p.fest ? ` · ${p.fest === p.nights ? "festival" : `${p.fest} festival`}` : ""}</span></div>
    <div class="lv-v-who subtle">${plural(p.artists.size, "artist")} · ${p.first.slice(0, 4)}${p.last.slice(0, 4) !== p.first.slice(0, 4) ? `–${p.last.slice(0, 4)}` : ""}<span>${esc([...p.artists].slice(0, 4).join(", "))}${p.artists.size > 4 ? "…" : ""}</span></div>
  </div>`).join("")}</div>`;
}

// Songs: everything you've heard live, with how much you play it
function liveSongsHtml(sets, st) {
  if (!sets.length) return "";
  const ids = sets.map((s) => s.id);
  const rows = query(`
    SELECT so.id, so.title, ar.id AS artist_id, ar.name AS artist, count(DISTINCT ss.setlist_id) AS heard, max(ss.is_cover) AS cover,
      (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays, min(sl.event_date) AS first
    FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id JOIN artists ar ON ar.id = so.artist_id JOIN setlists sl ON sl.id = ss.setlist_id
    WHERE ss.setlist_id IN (${ids.map(() => "?").join(",")}) GROUP BY so.id`, ids);
  let list = rows;
  if (st.sort === "never") list = rows.filter((r) => !r.plays).sort((a, b) => b.heard - a.heard || a.title.localeCompare(b.title));
  else list.sort(st.sort === "plays" ? (a, b) => b.plays - a.plays : (a, b) => b.heard - a.heard || b.plays - a.plays);
  const n = st.songsShown || 60, max = Math.max(1, ...list.map((r) => r.heard));
  return `<ol class="ap-tracks lv-songs">${list.slice(0, n).map((r, i) => `<li data-href="#/song/${r.id}">
      <span class="ap-pos">${i + 1}</span>
      <span class="ar-st"><b>${esc(r.title)}${r.cover ? ` <i class="lv-cover-tag">cover</i>` : ""}</b><span>${esc(r.artist)} · first heard ${esc(apMonthYear(r.first))}</span></span>
      <span class="lv-heard">${r.heard}×</span>
      <span class="ap-plays" title="Your plays">${r.plays ? apFmt(r.plays) : "—"}</span>
      <i class="ap-bar" style="--w:${(r.heard / max).toFixed(3)}"></i>
    </li>`).join("")}</ol>
    ${list.length > n ? `<div class="al-more"><button class="coll-btn" data-act="more">Show more</button><span class="subtle">${apFmt(n)} of ${apFmt(list.length)}</span></div>` : ""}
    <div class="subtle lv-songs-key">Heard live × · your plays</div>`;
}

// Insights (#/shows/insights): its own page, over the nights the hub's filters leave showing --
// the year bars and gigs / festivals here set the same filters
function renderLiveInsightsPage() {
  app.classList.add("wide");
  setTopbarTitle("live", "Insights");
  const st = liveState, L = liveData();
  app.innerHTML = `
    <div class="lv">
      <div class="coll-head">
        <div class="coll-title"><div class="hud">live</div><h1>Your live life</h1></div>
        <div class="coll-actions">
          <a class="coll-btn" href="#/shows">▦ Live shows</a>
          <div class="seg lv-kind" aria-label="Kind">${[["all", "All"], ["gig", "Gigs"], ["festival", "Festivals"]].map(([k, l]) => `<button data-kind="${k}" aria-selected="${st.kind === k}">${l}</button>`).join("")}</div>
        </div>
      </div>
      <div class="lv-years" data-role="years"></div>
      <div class="coll-count" data-role="count"></div>
      <div data-role="insights"></div>
    </div>`;
  const $ = (r) => app.querySelector(`[data-role="${r}"]`);
  const update = () => {
    const shown = liveFiltered(L, st), sets = shown.flatMap((e) => e.hits);
    renderLiveYears($("years"), L, st, update);
    const active = [st.q ? `“${st.q}”` : null, ...Object.values(st.facets).flatMap((x) => [...x])].filter(Boolean);
    $("count").innerHTML = `${plural(shown.length, "night")} · ${plural(sets.length, "set")}${active.length ? ` · filtered to ${esc(active.join(" · "))} <button class="linkish" data-act="clear">clear</button>` : ""}`;
    $("count").querySelector("[data-act='clear']")?.addEventListener("click", () => { st.q = ""; Object.values(st.facets).forEach((x) => x.clear()); update(); });
    renderLiveInsights($("insights"), L, shown, sets);
  };
  app.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => { st.kind = b.dataset.kind; app.querySelectorAll("[data-kind]").forEach((x) => x.setAttribute("aria-selected", x === b)); update(); }));
  update();
}

function renderLiveInsights(host, L, shown, sets) {
  const bars = (title, entries, { href = null, limit = 10, fmt = (n) => n } = {}) => {
    const e = entries.slice(0, limit), max = Math.max(1, ...e.map((x) => x[1]));
    return `<div class="ins"><h3>${title}</h3>${e.length ? e.map(([k, n, link]) => `<${link || href ? `a href="${esc(link || href(k))}"` : "div"} class="ibar">
      <span class="ik">${esc(k)}</span><span class="iv"><i style="width:${(100 * n) / max}%"></i></span><span class="in">${fmt(n)}</span></${link || href ? "a" : "div"}>`).join("") : `<div class="subtle">—</div>`}</div>`;
  };
  const count = (list, f) => { const c = {}; list.forEach((x) => [].concat(f(x)).filter(Boolean).forEach((k) => { c[k] = (c[k] || 0) + 1; })); return c; };
  const seenBy = {};
  sets.forEach((s) => { (seenBy[s.artist_id] ||= { name: s.artist, n: 0, dates: [] }).n += 1; seenBy[s.artist_id].dates.push(s.date); });
  const mostSeen = Object.entries(seenBy).sort((a, b) => b[1].n - a[1].n).map(([id, a]) => [a.name, a.n, `#/artist/${id}`]);
  // the bucket list: most played, never seen
  const seenIds = new Set(L.sets.map((s) => s.artist_id));
  const bucket = query(`SELECT ar.id, ar.name, count(*) AS c FROM scrobbles s JOIN artists ar ON ar.id = s.artist_id
    WHERE lower(ar.name) NOT IN ('various', 'various artists') GROUP BY ar.id ORDER BY c DESC LIMIT 60`).filter((a) => !seenIds.has(a.id)).slice(0, 10);
  // discovered live: barely played (under 20 plays -- a stray playlist or radio play doesn't count)
  // before you first saw them, and how much since; a mainstay is still in your rotation a year on
  const scrobbleStart = query(`SELECT min(played_at) AS d FROM scrobbles`)[0].d?.slice(0, 10);
  const firstSeen = Object.entries(seenBy).map(([id, a]) => ({ id: +id, name: a.name, seen: [...a.dates].sort()[0] }))
    .filter((a) => scrobbleStart && lvDaysBetween(scrobbleStart, a.seen) >= 90 && L.plays[a.id]);  // plays only go back so far: 90 days of history first
  const discovered = firstSeen.length ? query(`
    SELECT x.id, (SELECT count(*) FROM scrobbles s WHERE s.artist_id = x.id AND s.played_at < x.seen) AS before,
      (SELECT count(*) FROM scrobbles s WHERE s.artist_id = x.id AND s.played_at >= x.seen) AS after,
      (SELECT count(*) FROM scrobbles s WHERE s.artist_id = x.id AND s.played_at >= date('now', '-365 days')) AS lastYear
    FROM (${firstSeen.map(() => "SELECT ? AS id, ? AS seen").join(" UNION ALL ")}) x`, firstSeen.flatMap((a) => [a.id, a.seen]))
    .map((r) => ({ ...r, ...firstSeen.find((a) => a.id === r.id) }))
    .filter((r) => r.before < 20 && r.after > r.before)
    .map((r) => ({ ...r, mainstay: r.after >= 100 && r.lastYear >= 25 && lvDaysBetween(r.seen, new Date().toISOString().slice(0, 10)) > 365 }))
    .sort((a, b) => b.after - a.after) : [];
  // the live effect: plays in the 90 days after seeing them vs the 90 before
  const after = sets.filter((s) => scrobbleStart && lvDaysBetween(scrobbleStart, s.date) >= 90 && lvDaysBetween(s.date, new Date().toISOString().slice(0, 10)) > 30);
  const effect = after.length ? query(`
    SELECT sl.id, (SELECT count(*) FROM scrobbles s WHERE s.artist_id = sl.artist_id AND s.played_at >= date(sl.event_date, '-90 days') AND s.played_at < sl.event_date) AS before,
      (SELECT count(*) FROM scrobbles s WHERE s.artist_id = sl.artist_id AND s.played_at >= sl.event_date AND s.played_at < date(sl.event_date, '+90 days')) AS after
    FROM setlists sl WHERE sl.id IN (${after.map(() => "?").join(",")})`, after.map((s) => s.id)) : [];
  const setById = Object.fromEntries(sets.map((s) => [s.id, s]));
  const boosted = effect.map((r) => ({ ...r, s: setById[r.id], gain: r.after - r.before })).filter((r) => r.gain > 0).sort((a, b) => b.gain - a.gain).slice(0, 8);
  const totBefore = effect.reduce((n, r) => n + r.before, 0), totAfter = effect.reduce((n, r) => n + r.after, 0);
  // covers
  const coverRows = sets.length ? query(`SELECT cover_of_artist_text AS who, count(*) AS c FROM setlist_songs WHERE is_cover AND setlist_id IN (${sets.map(() => "?").join(",")}) GROUP BY who ORDER BY c DESC`, sets.map((s) => s.id)) : [];
  // records
  const longest = [...sets].sort((a, b) => b.songs - a.songs)[0];
  const biggest = [...shown].sort((a, b) => b.sets.length - a.sets.length)[0];
  const dates = shown.map((e) => e.date).sort();
  let gap = null;
  for (let i = 1; i < dates.length; i++) { const g = lvDaysBetween(dates[i - 1], dates[i]); if (!gap || g > gap.days) gap = { days: g, from: dates[i - 1], to: dates[i] }; }
  const mostIn = Object.entries(count(shown, (e) => e.year)).sort((a, b) => b[1] - a[1])[0];
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const months = count(shown, (e) => MONTHS[+e.date.slice(5, 7) - 1]);
  const days = count(shown, (e) => LV_WEEKDAYS[lvDay(e.date).getDay()]);
  const evLink = (e, text) => `<a href="#/event/${e.date}">${esc(text)}</a>`;
  host.innerHTML = `<div class="ins-grid">
    <div class="ins ins-wide lv-records"><h3>Records</h3><div class="lv-rec-grid">
      ${dates.length ? `<div><span>First night</span><b>${evLink(shown.find((e) => e.date === dates[0]), `${lvTitle(shown.find((e) => e.date === dates[0]))}, ${apMonthYear(dates[0])}`)}</b></div>` : ""}
      ${longest ? `<div><span>Longest set</span><b><a href="#/setlist/${longest.id}">${esc(longest.artist)}, ${plural(longest.songs, "song")}</a></b></div>` : ""}
      ${biggest ? `<div><span>Biggest day</span><b>${evLink(biggest, `${plural(biggest.sets.length, "act")}, ${lvTitle(biggest)} ${biggest.year}`)}</b></div>` : ""}
      ${mostIn ? `<div><span>Busiest year</span><b>${esc(mostIn[0])}, ${plural(mostIn[1], "night")}</b></div>` : ""}
      ${gap ? `<div><span>Longest wait</span><b>${lvSpan(gap.days)}, ${esc(apMonthYear(gap.from))} – ${esc(apMonthYear(gap.to))}</b></div>` : ""}
      <div><span>Covers heard</span><b>${apFmt(sets.reduce((n, s) => n + s.covers, 0))}</b></div>
    </div></div>
    ${bars("Most seen", mostSeen)}
    <div class="ins"><h3>The bucket list</h3><div class="subtle lv-ins-note">Your most played artists you've never seen live</div>
      ${bucket.map((a) => `<a class="ibar" href="#/artist/${a.id}"><span class="ik">${esc(a.name)}</span><span class="iv"><i style="width:${(100 * a.c) / (bucket[0]?.c || 1)}%"></i></span><span class="in">${apFmt(a.c)}</span></a>`).join("")}</div>
    <div class="ins"><h3>Discovered live</h3><div class="subtle lv-ins-note">${plural(discovered.length, "artist")} you'd played fewer than 20 times before you first saw them (your plays begin ${esc(apMonthYear(scrobbleStart))}, so shows from three months on)${discovered.filter((a) => a.mainstay).length ? ` · ${discovered.filter((a) => a.mainstay).length} became mainstays` : ""}</div>
      ${discovered.slice(0, 10).map((a) => `<a class="ibar lv-effect" href="#/artist/${a.id}" title="First seen ${esc(lvLongDate(a.seen))}; ${a.lastYear} plays in the last year">
        <span class="ik lv-ik2"><span>${esc(a.name)}</span><span class="subtle">seen ${esc(apMonthYear(a.seen))}${a.mainstay ? ` <i class="lv-mainstay">mainstay</i>` : ""}</span></span>
        <span class="in">${a.before} → <b class="lv-hot">${apFmt(a.after)}</b></span></a>`).join("") || `<div class="subtle">—</div>`}
      ${discovered.length ? `<div class="subtle lv-ins-note">plays before → since</div>` : ""}</div>
    <div class="ins"><h3>The live effect</h3><div class="subtle lv-ins-note">Plays of an act in the 90 days after seeing them vs the 90 before${effect.length ? ` — ${apFmt(totBefore)} → <b class="lv-hot">${apFmt(totAfter)}</b> across ${plural(effect.length, "set")}` : ""}</div>
      ${boosted.map((r) => `<a class="ibar lv-effect" href="#/event/${r.s.date}"><span class="ik lv-ik2"><span>${esc(r.s.artist)}</span><span class="subtle">${esc(apMonthYear(r.s.date))}</span></span><span class="in">${r.before} → <b class="lv-hot">${r.after}</b></span></a>`).join("") || `<div class="subtle">—</div>`}</div>
    ${bars("Cities", Object.entries(count(shown, (e) => e.city)).sort((a, b) => b[1] - a[1]), { fmt: (n) => `${n}` })}
    ${bars("Covers you've heard", coverRows.map((r) => [r.who || "Unknown", r.c]), { limit: 8 })}
    ${bars("Months you go out", MONTHS.map((m) => [m, months[m] || 0]).filter((x) => x[1]), { limit: 12 })}
    ${bars("Nights of the week", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d) => [d, days[d] || 0]), { limit: 7 })}
  </div>`;
}

// ---------------------------------------------------------------------
// A night out (#/event/<date>): the lineup, every set, and what it did to your listening
// ---------------------------------------------------------------------
function renderEvent(date) {
  const L = liveData();
  const e = L.byDate[date];
  if (!e) return renderNotFound("Show");
  app.classList.add("wide");
  const ids = e.sets.map((s) => s.id);
  const songs = query(`
    SELECT ss.setlist_id, ss.position, ss.set_name, ss.is_cover, ss.cover_of_artist_text, so.id, so.title,
      (SELECT count(*) FROM scrobbles s WHERE s.song_id = so.id) AS plays,
      (SELECT min(sl2.event_date) FROM setlist_songs x JOIN setlists sl2 ON sl2.id = x.setlist_id WHERE x.song_id = so.id) AS first_heard
    FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id WHERE ss.setlist_id IN (${ids.map(() => "?").join(",")}) ORDER BY ss.position`, ids);
  const bySet = {};
  songs.forEach((r) => (bySet[r.setlist_id] ||= []).push(r));
  // how many times you'd seen each act, counting this one
  const times = {};
  for (const s of e.sets) {
    const all = L.sets.filter((x) => x.artist_id === s.artist_id);
    const k = all.findIndex((x) => x.id === s.id);
    times[s.id] = { nth: k + 1, of: all.length, prev: all[k - 1] || null, next: all[k + 1] || null };
  }
  // the live effect for this night's acts
  const effect = e.sets.length ? query(`
    SELECT sl.id, (SELECT count(*) FROM scrobbles s WHERE s.artist_id = sl.artist_id AND s.played_at >= date(sl.event_date, '-90 days') AND s.played_at < sl.event_date) AS before,
      (SELECT count(*) FROM scrobbles s WHERE s.artist_id = sl.artist_id AND s.played_at >= sl.event_date AND s.played_at < date(sl.event_date, '+90 days')) AS after
    FROM setlists sl WHERE sl.id IN (${ids.map(() => "?").join(",")})`, ids) : [];
  const scrobbleStart = query(`SELECT min(played_at) AS d FROM scrobbles`)[0].d?.slice(0, 10);
  const effectOn = scrobbleStart && lvDaysBetween(scrobbleStart, e.date) >= 90;  // 90 days of plays either side to compare
  const fx = Object.fromEntries(effect.map((r) => [r.id, r]));
  const fxMax = Math.max(1, ...effect.map((r) => Math.max(r.before, r.after)));
  const firstTimes = songs.filter((r) => r.first_heard === e.date).length;
  const nightsThere = L.events.filter((x) => x.place.id === e.place.id);
  const nthThere = nightsThere.findIndex((x) => x.date === e.date) + 1;
  const covers = e.sets.slice(0, 4);
  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  const others = e.sets.slice(1).map((s) => `<a href="#/artist/${s.artist_id}">${esc(s.artist)}</a>`);
  const open = e.sets.length <= 3;

  app.innerHTML = `
    <div class="ap lv-event">
      <section class="ap-hero">
        <div class="ar-mosaic ar-n${covers.length >= 4 ? 4 : 1}">${covers.length >= 4 ? covers.map((s) => lvFace(L, s.artist_id, s.artist)).join("") : lvFace(L, e.headliner.artist_id, e.headliner.artist)}</div>
        <div class="ap-info">
          <div class="hud">${esc(lvKindLabel(e))}</div>
          <h1 class="ap-title">${esc(lvTitle(e))}${e.festival ? ` <span class="lv-yr-title">${e.year}</span>` : ""}</h1>
          <div class="ap-artist">${e.festival ? "" : others.length ? `with ${others.join(", ")} · ` : ""}${esc(lvLongDate(e.date))}</div>
          <div class="lv-ev-place"><a href="#/venue/${e.place.id}">${esc(e.place.name)}</a>${e.city ? `, ${esc(e.city)}` : ""}${e.country && e.country !== "United Kingdom" ? `, ${esc(e.country)}` : ""}</div>
          <div class="ap-kpis">
            ${kpi(apFmt(e.sets.length), e.sets.length === 1 ? "act" : "acts", "lv-accent")}
            ${kpi(apFmt(e.songs), "songs heard")}
            ${kpi(apFmt(firstTimes), "first time live")}
            ${kpi(`#${e.nth}`, `of your ${apFmt(L.events.length)} nights`)}
          </div>
          <div class="ap-links">
            ${e.festival && e.festival.days.length > 1 ? e.festival.days.map((d, k) => d === e.date ? `<span class="ap-chip live on">Day ${k + 1}</span>`
              : `<a class="ap-chip live" href="#/event/${d}">Day ${k + 1} · ${LV_WEEKDAYS[lvDay(d).getDay()].slice(0, 3)}</a>`).join("") : ""}
            ${!e.festival && e.headliner.tour ? `<span class="ap-chip">${esc(e.headliner.tour)}</span>` : ""}
            <a class="ap-chip" href="#/venue/${e.place.id}">${nthThere > 1 ? `${ordinal(nthThere)} of ${nightsThere.length} nights here` : nightsThere.length > 1 ? `first of ${nightsThere.length} nights here` : "your only night here"}</a>
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">
          <section class="ap-card">
            <div class="ap-head"><h2>${e.sets.length > 1 ? "The lineup" : "The setlist"}</h2><span class="subtle">longest set first${e.sets.some((s) => s.place.stage) ? " · by stage" : ""}</span></div>
            ${e.sets.map((s) => {
              const list = bySet[s.id] || [], t = times[s.id];
              const max = Math.max(1, ...list.map((r) => r.plays));
              let lastSet = null;
              const rows = list.map((r, k) => {
                const label = r.set_name && r.set_name !== lastSet ? `<li class="lv-setname">${esc(r.set_name)}</li>` : "";
                lastSet = r.set_name || lastSet;
                return `${label}<li data-song="${r.id}">
                  <span class="ap-pos">${k + 1}</span>
                  <span class="ap-tt">${esc(r.title)}${r.is_cover ? ` <i class="lv-cover-tag">${esc(r.cover_of_artist_text ? `${r.cover_of_artist_text} cover` : "cover")}</i>` : ""}${r.first_heard === e.date ? ` <i class="lv-first" title="The first time you heard it live">first</i>` : ""}</span>
                  <span class="ap-plays" title="Your plays">${r.plays ? apFmt(r.plays) : "—"}</span><span></span>
                  ${r.plays ? `<i class="ap-bar" style="--w:${(r.plays / max).toFixed(3)}"></i>` : ""}</li>`;
              }).join("");
              return `<details class="lv-set"${open ? " open" : ""}>
                <summary>${lvFace(L, s.artist_id, s.artist, "lv-set-art")}
                  <span class="lv-set-who"><b>${esc(s.artist)}</b><span class="subtle">${esc([s.place.stage, s.tour].filter(Boolean).join(" · "))}${s.place.stage || s.tour ? " · " : ""}${t.of > 1 ? `${ordinal(t.nth)} of ${t.of} times you've seen them` : "the only time you've seen them"}</span></span>
                  <span class="lv-set-n">${s.songs ? plural(s.songs, "song") : "no setlist"}</span></summary>
                ${list.length ? `<ol class="ap-tracks lv-setlist">${rows}</ol>` : `<div class="subtle lv-noset">setlist.fm has no songs for this set.</div>`}
                <div class="lv-set-links"><a href="#/artist/${s.artist_id}">${esc(s.artist)} →</a><a href="#/setlist/${s.id}">Setlist page →</a>
                  ${t.prev ? `<a href="#/event/${t.prev.date}">Before: ${esc(apMonthYear(t.prev.date))}</a>` : ""}${t.next ? `<a href="#/event/${t.next.date}">Next: ${esc(apMonthYear(t.next.date))}</a>` : ""}</div>
              </details>`;
            }).join("")}
          </section>
        </div>

        <aside class="ap-side">
          ${effectOn ? `<section class="ap-card">
            <div class="ap-head"><h2>The live effect</h2><span class="subtle">plays, 90 days either side</span></div>
            <div class="lv-fx">${e.sets.filter((s) => fx[s.id] && (fx[s.id].before || fx[s.id].after)).sort((a, b) => (fx[b.id].after - fx[b.id].before) - (fx[a.id].after - fx[a.id].before)).slice(0, 10).map((s) => `
              <a href="#/artist/${s.artist_id}"><span class="lv-fx-name">${esc(s.artist)}</span>
                <span class="lv-fx-bars"><i class="b" style="--w:${(fx[s.id].before / fxMax).toFixed(3)}"></i><i class="a" style="--w:${(fx[s.id].after / fxMax).toFixed(3)}"></i></span>
                <span class="lv-fx-n">${fx[s.id].before} → <b>${fx[s.id].after}</b></span></a>`).join("") || `<div class="subtle">You didn't play any of them either side of the night.</div>`}</div>
            <div class="lv-fx-key subtle"><span><i class="b"></i>before</span><span><i class="a"></i>after</span></div>
          </section>` : ""}
          <section class="ap-card">
            <div class="ap-head"><h2>The venue</h2><a class="subtle" href="#/venue/${e.place.id}">all nights →</a></div>
            <div class="lv-venue-card"><b>${esc(e.place.name)}</b><span class="subtle">${esc([e.city, e.country].filter(Boolean).join(", "))}</span>
              ${e.place.kind || e.place.capacity ? `<span class="subtle">${esc([e.place.kind, e.place.capacity ? `${apFmt(e.place.capacity)} capacity` : null].filter(Boolean).join(" · "))}</span>` : ""}</div>
            <ol class="lv-mini">${nightsThere.slice(-8).reverse().map((x) => `<li class="${x.date === e.date ? "on" : ""}"><a href="#/event/${x.date}"><span>${esc(apMonthYear(x.date))}</span><b>${esc(lvWho(x))}</b></a></li>`).join("")}</ol>
          </section>
          ${(() => {
            const k = L.events.findIndex((x) => x.date === e.date), prev = L.events[k - 1], next = L.events[k + 1];
            return `<section class="ap-card"><div class="ap-head"><h2>Either side</h2></div><div class="lv-around">
              ${prev ? `<a href="#/event/${prev.date}"><span class="subtle">← ${esc(apMonthYear(prev.date))}</span><b>${esc(lvTitle(prev))}</b><span class="subtle">${esc(prev.festival ? lvWho(prev) : prev.place.name)}</span></a>` : "<span></span>"}
              ${next ? `<a href="#/event/${next.date}" class="r"><span class="subtle">${esc(apMonthYear(next.date))} →</span><b>${esc(lvTitle(next))}</b><span class="subtle">${esc(next.festival ? lvWho(next) : next.place.name)}</span></a>` : "<span></span>"}</div></section>`;
          })()}
        </aside>
      </div>
    </div>`;
  app.querySelectorAll("[data-song]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.song}`; }));
}

// ---------------------------------------------------------------------
// One act's set (#/setlist/<id>): the songs, where they came from, what changed since last
// time, and what seeing them did to your listening
// ---------------------------------------------------------------------
function renderSetlist(id) {
  const L = liveData();
  const s = L.sets.find((x) => x.id === id);
  if (!s) return renderNotFound("Setlist");
  const e = L.byDate[s.date];
  app.classList.add("wide");
  const songs = query(`
    SELECT ss.position, ss.set_name, ss.is_cover, ss.cover_of_artist_text, so.id, so.title, so.artist_id AS song_artist,
      al.id AS album_id, al.title AS album, al.year AS album_year, al.cover_status, al.cover_updated_at,
      (SELECT count(*) FROM scrobbles x WHERE x.song_id = so.id) AS plays,
      (SELECT count(*) FROM setlist_songs x JOIN setlists y ON y.id = x.setlist_id WHERE x.song_id = so.id AND y.event_date <= ?) AS nth,
      (SELECT count(*) FROM setlist_songs x WHERE x.song_id = so.id) AS heard
    FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id LEFT JOIN albums al ON al.id = so.album_id
    WHERE ss.setlist_id = ? ORDER BY ss.position`, [s.date, id]);
  const theirs = L.sets.filter((x) => x.artist_id === s.artist_id);
  const k = theirs.findIndex((x) => x.id === id), prev = theirs[k - 1] || null, next = theirs[k + 1] || null;
  const firsts = songs.filter((r) => r.nth === 1).length;
  const neverPlayed = songs.filter((r) => !r.plays).length;
  const max = Math.max(1, ...songs.map((r) => r.plays));

  // where the set came from: their own songs by album, covers apart
  const albums = new Map();
  for (const r of songs) {
    const key = r.is_cover || r.song_artist !== s.artist_id ? "cover" : r.album_id || "none";
    const g = albums.get(key) || { key, n: 0, id: r.album_id, title: key === "cover" ? "Covers" : key === "none" ? "Not on an album" : r.album, year: key === "cover" || key === "none" ? null : r.album_year,
      cover_status: r.cover_status, cover_updated_at: r.cover_updated_at };
    g.n += 1;
    albums.set(key, g);
  }
  const albumList = [...albums.values()].sort((a, b) => (a.key === "cover" || a.key === "none") - (b.key === "cover" || b.key === "none") || b.n - a.n);

  // versus the last time you saw them
  let diff = null;
  if (prev) {
    const before = new Set(query(`SELECT song_id FROM setlist_songs WHERE setlist_id = ?`, [prev.id]).map((r) => r.song_id));
    const now = new Set(songs.map((r) => r.id));
    const titles = Object.fromEntries(query(`SELECT so.id, so.title FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id WHERE ss.setlist_id = ?`, [prev.id]).map((r) => [r.id, r.title]));
    diff = { added: songs.filter((r) => !before.has(r.id)), kept: songs.filter((r) => before.has(r.id)), dropped: [...before].filter((x) => !now.has(x)).map((x) => ({ id: x, title: titles[x] })) };
  }

  // their plays a week, 13 weeks either side of the night
  const scrobbleStart = query(`SELECT min(played_at) AS d FROM scrobbles`)[0].d?.slice(0, 10);
  const weeks = [];
  if (scrobbleStart && lvDaysBetween(scrobbleStart, s.date) >= 90) {
    const rows = query(`SELECT CAST(floor((julianday(substr(played_at, 1, 10)) - julianday(?)) / 7) AS INTEGER) AS w, count(*) AS c FROM scrobbles
      WHERE artist_id = ? AND played_at >= date(?, '-91 days') AND played_at < date(?, '+91 days') GROUP BY w`, [s.date, s.artist_id, s.date, s.date]);
    const byW = Object.fromEntries(rows.map((r) => [r.w, r.c]));
    for (let w = -13; w <= 12; w++) weeks.push({ w, c: byW[w] || 0 });
  }
  const wMax = Math.max(1, ...weeks.map((x) => x.c));
  const before = weeks.filter((x) => x.w < 0).reduce((n, x) => n + x.c, 0), after = weeks.filter((x) => x.w >= 0).reduce((n, x) => n + x.c, 0);
  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  let lastSet = null;

  app.innerHTML = `
    <div class="ap lv-event lv-setpage">
      <section class="ap-hero">
        <a href="#/artist/${s.artist_id}">${lvFace(L, s.artist_id, s.artist, "ap-cover")}</a>
        <div class="ap-info">
          <div class="hud">Setlist · ${esc(lvKindLabel(e))}</div>
          <h1 class="ap-title"><a href="#/artist/${s.artist_id}">${esc(s.artist)}</a></h1>
          <div class="ap-artist">${s.tour ? `${esc(s.tour)} · ` : ""}${esc(lvLongDate(s.date))}</div>
          <div class="lv-ev-place"><a href="#/venue/${s.place.id}">${esc(s.place.name)}</a>${s.place.stage ? ` · ${esc(s.place.stage)}` : ""}${s.city ? `, ${esc(s.city)}` : ""}</div>
          <div class="ap-kpis">
            ${kpi(apFmt(songs.length), "songs", "lv-accent")}
            ${kpi(apFmt(firsts), "first time live")}
            ${kpi(apFmt(neverPlayed), "you've never played")}
            ${kpi(theirs.length > 1 ? `${ordinal(k + 1)}` : "Once", theirs.length > 1 ? `of ${theirs.length} times seen` : "the only time seen")}
          </div>
          <div class="ap-links">
            <a class="ap-chip live" href="#/event/${s.date}">● The night${e.sets.length > 1 ? ` · ${plural(e.sets.length - 1, "other act")}` : ""}</a>
            ${prev ? `<a class="ap-chip" href="#/setlist/${prev.id}">← ${esc(apMonthYear(prev.date))}</a>` : ""}
            ${next ? `<a class="ap-chip" href="#/setlist/${next.id}">${esc(apMonthYear(next.date))} →</a>` : ""}
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">
          <section class="ap-card">
            <div class="ap-head"><h2>The setlist</h2><span class="subtle">${plural(songs.length, "song")} · your plays</span></div>
            ${songs.length ? `<ol class="ap-tracks lv-setlist lv-setlist-full">${songs.map((r, i) => {
              const label = r.set_name && r.set_name !== lastSet ? `<li class="lv-setname">${esc(r.set_name)}</li>` : "";
              lastSet = r.set_name || lastSet;
              return `${label}<li data-song="${r.id}">
                <span class="ap-pos">${i + 1}</span>
                <span class="ar-st"><b>${esc(r.title)}${r.is_cover ? ` <i class="lv-cover-tag">${esc(r.cover_of_artist_text ? `${r.cover_of_artist_text} cover` : "cover")}</i>` : ""}${r.nth === 1 ? ` <i class="lv-first">first</i>` : ""}</b>
                  <span>${esc([r.album && !r.is_cover ? r.album : null, r.heard > 1 ? `heard live ${r.heard}× · ${ordinal(r.nth)} time` : "the only time you've heard it live"].filter(Boolean).join(" · "))}</span></span>
                <span class="ap-plays" title="Your plays">${r.plays ? apFmt(r.plays) : "—"}</span><span></span>
                ${r.plays ? `<i class="ap-bar" style="--w:${(r.plays / max).toFixed(3)}"></i>` : ""}</li>`;
            }).join("")}</ol>` : `<div class="subtle">setlist.fm has no songs for this set.</div>`}
          </section>

          ${diff && songs.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Since last time</h2><a class="subtle" href="#/setlist/${prev.id}">${esc(lvLongDate(prev.date))} · ${esc(prev.place.name)} →</a></div>
            <div class="lv-diff">
              <div><h3>New <span>${diff.added.length}</span></h3>${diff.added.map((r) => `<a href="#/song/${r.id}">${esc(r.title)}</a>`).join("") || `<span class="subtle">—</span>`}</div>
              <div><h3>Kept <span>${diff.kept.length}</span></h3>${diff.kept.map((r) => `<a href="#/song/${r.id}">${esc(r.title)}</a>`).join("") || `<span class="subtle">—</span>`}</div>
              <div><h3>Dropped <span>${diff.dropped.length}</span></h3>${diff.dropped.map((r) => `<a href="#/song/${r.id}">${esc(r.title)}</a>`).join("") || `<span class="subtle">—</span>`}</div>
            </div>
          </section>` : ""}
        </div>

        <aside class="ap-side">
          ${albumList.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Where the set came from</h2></div>
            <div class="lv-mix">${albumList.map((a, i) => `<i style="flex:${a.n};--i:${i}" title="${esc(a.title)}: ${plural(a.n, "song")}"></i>`).join("")}</div>
            <ol class="ar-records lv-from">${albumList.map((a, i) => `<li>${a.key === "cover" || a.key === "none" ? `<div><span class="lv-sw" style="--i:${i}"></span>
              <span><b>${esc(a.title)}</b></span><span class="ap-plays">${a.n}</span></div>` : `<a href="#/album/${a.id}"><span class="lv-sw" style="--i:${i}"></span>
              <span><b>${esc(a.title)}</b><span class="subtle">${a.year || ""}</span></span><span class="ap-plays">${a.n}</span></a>`}</li>`).join("")}</ol>
          </section>` : ""}

          ${weeks.length ? `<section class="ap-card">
            <div class="ap-head"><h2>The live effect</h2><span class="subtle">${apFmt(before)} → <b class="lv-hot">${apFmt(after)}</b> plays</span></div>
            <div class="lv-weeks">${weeks.map((x) => `<i class="${x.w >= 0 ? "a" : ""}${x.w === 0 ? " now" : ""}" style="--h:${x.c ? Math.max(5, (x.c / wMax) * 100).toFixed(1) : 0}%" title="${x.w < 0 ? `${-x.w} week${x.w === -1 ? "" : "s"} before` : x.w === 0 ? "the week of the show" : `${x.w} week${x.w === 1 ? "" : "s"} after`}: ${plural(x.c, "play")}"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>13 weeks before</span><span class="pk">the show</span><span>13 after</span></div>
          </section>` : ""}

          <section class="ap-card">
            <div class="ap-head"><h2>You and ${esc(s.artist)}</h2><a class="subtle" href="#/artist/${s.artist_id}">artist →</a></div>
            <ol class="lv-mini">${theirs.slice().reverse().map((x) => `<li class="${x.id === id ? "on" : ""}"><a href="#/setlist/${x.id}"><span>${esc(apMonthYear(x.date))}</span><b>${esc(x.place.name)}</b></a></li>`).join("")}</ol>
            ${L.plays[s.artist_id] ? `<div class="subtle lv-you">${apFmt(L.plays[s.artist_id].c)} plays since ${esc(apMonthYear(L.plays[s.artist_id].first))}</div>` : ""}
          </section>
        </aside>
      </div>
    </div>`;
  app.querySelectorAll("[data-song]").forEach((el) => el.addEventListener("click", () => { location.hash = `#/song/${el.dataset.song}`; }));
}

// ---------------------------------------------------------------------
// A venue (#/venue/<id>): every night there, who you saw, its stages. A stage opens its site.
// ---------------------------------------------------------------------
function renderVenue(id) {
  const L = liveData();
  const place = L.places[id];
  if (!place) return renderNotFound("Venue");
  const pid = place.id;
  app.classList.add("wide");
  const nights = L.events.filter((e) => e.sets.some((s) => s.place.id === pid));
  const sets = nights.flatMap((e) => e.sets.filter((s) => s.place.id === pid));
  const fest = nights.filter((e) => e.festival).length;
  const byArtist = {};
  sets.forEach((s) => { (byArtist[s.artist_id] ||= { id: s.artist_id, name: s.artist, n: 0, last: s.date }).n += 1; if (s.date > byArtist[s.artist_id].last) byArtist[s.artist_id].last = s.date; });
  const artists = Object.values(byArtist).sort((a, b) => b.n - a.n || b.last.localeCompare(a.last));
  const stages = {};
  sets.forEach((s) => { if (s.place.stage) stages[s.place.stage] = (stages[s.place.stage] || 0) + 1; });
  const ids = sets.map((s) => s.id);
  const topSongs = ids.length ? query(`SELECT so.id, so.title, ar.name AS artist, count(*) AS n FROM setlist_songs ss JOIN songs so ON so.id = ss.song_id JOIN artists ar ON ar.id = so.artist_id
    WHERE ss.setlist_id IN (${ids.map(() => "?").join(",")}) GROUP BY so.id HAVING n > 1 ORDER BY n DESC, so.title LIMIT 8`, ids) : [];
  const byYear = {};
  nights.forEach((e) => { byYear[e.year] = (byYear[e.year] || 0) + 1; });
  const years = [];
  if (nights.length) for (let y = +nights[0].year; y <= Math.max(+nights[nights.length - 1].year, new Date().getFullYear()); y++) years.push({ y: String(y), c: byYear[y] || 0 });
  const yMax = Math.max(1, ...years.map((y) => y.c));
  const peak = years.reduce((m, y) => (y.c && y.c >= (m?.c || 0) ? y : m), null);  // a tie goes to the latest
  const longest = [...sets].sort((a, b) => b.songs - a.songs)[0];
  const biggest = [...nights].sort((a, b) => b.sets.length - a.sets.length)[0];
  // other venues in the city
  const near = {};
  L.events.forEach((e) => { if (e.city === place.city && e.place.id !== pid) (near[e.place.id] ||= { ...e.place, n: 0 }).n += 1; });
  const nearby = Object.values(near).sort((a, b) => b.n - a.n).slice(0, 8);
  const faces = artists.slice(0, 4);
  const kpi = (value, label, cls = "") => `<div class="ap-kpi ${cls}"><b>${value}</b><span>${esc(label)}</span></div>`;
  const maps = `https://www.google.com/maps/search/${encodeURIComponent([place.name, place.city, place.country].filter(Boolean).join(", "))}`;

  app.innerHTML = `
    <div class="ap lv-event lv-venuepage">
      <section class="ap-hero">
        <div class="ar-mosaic ar-n${faces.length >= 4 ? 4 : faces.length ? 1 : 0}">${faces.length >= 4 ? faces.map((a) => lvFace(L, a.id, a.name)).join("")
          : faces.length ? lvFace(L, faces[0].id, faces[0].name) : `<div class="ap-noart">${esc(place.name.slice(0, 1))}</div>`}</div>
        <div class="ap-info">
          <div class="hud">${esc(["Venue", place.kind, fest ? (fest === nights.length ? "festival site" : "and festival site") : null].filter(Boolean).join(" · "))}</div>
          <h1 class="ap-title">${esc(place.name)}</h1>
          <div class="ap-artist">${esc([place.city, place.country].filter(Boolean).join(", "))}${place.capacity ? ` · ${apFmt(place.capacity)} capacity` : ""}</div>
          <div class="ap-kpis">
            ${kpi(apFmt(nights.length), nights.length === 1 ? "night" : "nights", "lv-accent")}
            ${kpi(apFmt(artists.length), artists.length === 1 ? "artist" : "artists seen")}
            ${kpi(nights.length ? apMonthYear(nights[0].date) : "—", "first night")}
            ${kpi(nights.length ? relativeDay(nights[nights.length - 1].date) : "—", "last night")}
          </div>
          <div class="ap-links">
            ${Object.entries(stages).sort((a, b) => b[1] - a[1]).map(([n, c]) => `<span class="ap-chip">${esc(n)} · ${c}</span>`).join("")}
            <a class="ap-chip" href="${esc(maps)}" target="_blank" rel="noopener">Map ↗</a>
          </div>
        </div>
      </section>

      <div class="ap-body">
        <div class="ap-main">
          <section class="ap-card">
            <div class="ap-head"><h2>Your nights here</h2><span class="subtle">${plural(nights.length, "night")} · ${plural(sets.length, "set")}</span></div>
            <ol class="ar-shows lv-nights">${nights.slice().reverse().map((e) => {
              const here = e.sets.filter((s) => s.place.id === pid);
              const d = lvDay(e.date);
              return `<li><a href="#/event/${e.date}">
                <span class="ar-date"><b>${d.getDate()} ${AP_MON[d.getMonth()]}</b>${e.year}</span>
                <span class="ar-venue"><b>${esc(e.festival ? `${e.festival.name} ${e.year}${e.festival.days.length > 1 ? ` · day ${e.day}` : ""}` : e.headliner.artist)}</b>
                  <span>${esc(e.festival ? `${plural(here.length, "act")}: ${here.slice(0, 5).map((s) => s.artist).join(", ")}${here.length > 5 ? "…" : ""}` : [here.slice(1).length ? `with ${here.slice(1).map((s) => s.artist).join(", ")}` : null, e.headliner.tour].filter(Boolean).join(" · "))}</span></span>
                <span class="lv-faces">${here.slice(0, 4).map((s) => lvFace(L, s.artist_id, s.artist, "lv-face")).join("")}</span></a></li>`;
            }).join("")}</ol>
          </section>
        </div>

        <aside class="ap-side">
          ${years.length > 1 ? `<section class="ap-card">
            <div class="ap-head"><h2>Nights a year</h2>${peak ? `<span class="subtle">most in ${esc(peak.y)}</span>` : ""}</div>
            <div class="ap-years lv-vyears">${years.map((y) => `<i class="${y === peak ? "pk" : ""}" style="--h:${y.c ? Math.max(8, (y.c / yMax) * 100).toFixed(1) : 0}%" title="${esc(y.y)}: ${plural(y.c, "night")}"></i>`).join("")}</div>
            <div class="ap-years-cap"><span>${esc(years[0].y)}</span><span>${esc(years[years.length - 1].y)}</span></div>
          </section>` : ""}
          <section class="ap-card">
            <div class="ap-head"><h2>Here</h2></div>
            <div class="lv-rec-grid lv-here">
              ${biggest && biggest.sets.length > 1 ? `<div><span>Biggest night</span><b><a href="#/event/${biggest.date}">${plural(biggest.sets.length, "act")}, ${esc(apMonthYear(biggest.date))}</a></b></div>` : ""}
              ${longest && longest.songs ? `<div><span>Longest set</span><b><a href="#/setlist/${longest.id}">${esc(longest.artist)}, ${plural(longest.songs, "song")}</a></b></div>` : ""}
              <div><span>Songs heard</span><b>${apFmt(sets.reduce((n, s) => n + s.songs, 0))}</b></div>
              ${fest ? `<div><span>Festival days</span><b>${apFmt(fest)}</b></div>` : ""}
              ${place.kind ? `<div><span>Type</span><b>${esc(place.kind)}</b></div>` : ""}
              ${place.capacity ? `<div><span>Capacity</span><b>${apFmt(place.capacity)}</b></div>` : ""}
            </div>
          </section>
          ${topSongs.length ? `<section class="ap-card">
            <div class="ap-head"><h2>Heard most here</h2></div>
            <ol class="lv-mini lv-topsongs">${topSongs.map((r) => `<li><a href="#/song/${r.id}"><span>${r.n}×</span><b>${esc(r.title)} <span class="subtle">${esc(r.artist)}</span></b></a></li>`).join("")}</ol>
          </section>` : ""}
        </aside>
      </div>

      ${artists.length ? `<section class="ap-card ap-shelf-card">
        <div class="ap-head"><h2>Who you saw here</h2><span class="subtle">${plural(artists.length, "artist")}, most seen first</span></div>
        <div class="lv-artists lv-here-artists">${artists.map((a, i) => `<a class="lv-artist" href="#/artist/${a.id}"${i >= 18 ? " hidden" : ""}>${lvFace(L, a.id, a.name, "lv-art")}
          <b>${esc(a.name)}</b><span class="lv-seen">${a.n > 1 ? `${a.n}× here` : "once"}</span></a>`).join("")}</div>
        ${artists.length > 18 ? `<button class="ar-more" data-act="all-artists">Show all ${apFmt(artists.length)}</button>` : ""}
      </section>` : ""}

      ${nearby.length ? `<section class="ap-card ap-shelf-card">
        <div class="ap-head"><h2>Elsewhere in ${esc(place.city)}</h2></div>
        <div class="lv-nearby">${nearby.map((p) => `<a href="#/venue/${p.id}"><b>${esc(p.name)}</b><span class="subtle">${plural(p.n, "night")}</span></a>`).join("")}</div>
      </section>` : ""}
    </div>`;
  app.querySelector("[data-act='all-artists']")?.addEventListener("click", (e) => { app.querySelectorAll(".lv-here-artists [hidden]").forEach((a) => { a.hidden = false; }); e.target.remove(); });
}

const ordinal = (n) => `${n}${["th", "st", "nd", "rd"][(n % 100 >= 11 && n % 100 <= 13) ? 0 : n % 10] || "th"}`;
