/*
 * Artist review components shared by artists.html and vinyl.html: side-by-side comparison with a
 * pick-the-primary merge, MBID assignment (an mbid already on another local artist -> compare and
 * merge right there), and the full resolve toolkit (what was scrobbled, MusicBrainz search with
 * discography-vs-your-albums checks on the top results, manual id, merge into an existing artist).
 * Needs shared.js first.
 */

let compareSeq = 0;

// Renders a comparison of artists aId/bId into `slot`, with keep radios and a merge button.
// Keys 1/2 (data-key) merge keeping left/right. onMerged(result) after a successful merge.
async function renderArtistCompare(slot, aId, bId, { onMerged, defaultKeep, keys = true, summary: showSummary = true } = {}) {
  slot.innerHTML = `<div class="status-line">Loading comparison…</div>`;
  const { ok, data } = await api(`/api/artists/compare?a=${aId}&b=${bId}`);
  if (!ok) { slot.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
  const { a, b, overlap } = data;
  let keep = defaultKeep || ((a.mbid && !b.mbid) ? "a" : (b.mbid && !a.mbid) ? "b" : (a.scrobbleCount >= b.scrobbleCount ? "a" : "b"));
  const name = `keep-${++compareSeq}`;

  const mbCell = (x) => x.mbid
    ? `${mbLink("artist", x.mbid, x.mbid)}${x.mb ? `<div class="meta">${esc([x.mb.name, x.mb.disambiguation, x.mb.type, x.mb.country, x.mb.beginDate].filter(Boolean).join(" · "))}</div>` : ""}`
    : `<span class="badge warn">no mbid</span>`;
  const list = (items, fmt) => items.length ? `<ol>${items.map(fmt).join("")}</ol>` : `<span class="muted">none</span>`;
  const raw = (x) => Object.entries(x.rawNames).filter(([, v]) => v.length).map(([src, v]) => `<div><span class="meta">${src}:</span> ${v.map(esc).join(" · ")}</div>`).join("") || "—";
  const rows = [
    { label: "MusicBrainz", a: mbCell(a), b: mbCell(b), same: a.mbid && a.mbid === b.mbid },
    { label: "Scrobbles", a: fmtNum(a.scrobbleCount), b: fmtNum(b.scrobbleCount) },
    { label: "Played", a: `${fmtDate(a.firstPlayed)} → ${fmtDate(a.lastPlayed)}`, b: `${fmtDate(b.firstPlayed)} → ${fmtDate(b.lastPlayed)}` },
    { label: "Albums / songs", a: `${fmtNum(a.albumCount)} / ${fmtNum(a.songCount)}`, b: `${fmtNum(b.albumCount)} / ${fmtNum(b.songCount)}` },
    { label: "Vinyl / shows", a: `${fmtNum(a.vinylCount)} / ${fmtNum(a.setlistCount)}`, b: `${fmtNum(b.vinylCount)} / ${fmtNum(b.setlistCount)}` },
    { label: "Top songs", a: list(a.topSongs, (s) => `<li>${esc(s.title)} <span class="meta">${fmtNum(s.plays)}</span></li>`), b: list(b.topSongs, (s) => `<li>${esc(s.title)} <span class="meta">${fmtNum(s.plays)}</span></li>`) },
    { label: "Top albums", a: list(a.topAlbums, albumLi), b: list(b.topAlbums, albumLi) },
    { label: "Names seen", a: raw(a), b: raw(b) },
  ];
  const summary = [
    overlap.sameNormalizedName ? `<span class="badge high">same name once normalised</span>` : "",
    `<span class="badge ${overlap.songs ? "high" : ""}" title="${esc(overlap.songTitles.join(" · "))}">${plural(overlap.songs, "shared song title")}</span>`,
    `<span class="badge ${overlap.albums ? "high" : ""}" title="${esc(overlap.albumTitles.join(" · "))}">${plural(overlap.albums, "shared album title")}</span>`,
    overlap.distinctMbids ? `<span class="badge bad">two different MBIDs — different artists</span>` : "",
  ].join("");

  function paint() {
    const [keepX, awayX] = keep === "a" ? [a, b] : [b, a];
    slot.innerHTML = `
      ${showSummary ? `<div class="compare-summary">${summary}</div>` : ""}
      ${overlap.songTitles.length ? `<div class="meta" style="margin-top:4px">Shared songs: ${overlap.songTitles.map(esc).join(" · ")}</div>` : ""}
      ${compareGrid(name, `<span>${esc(a.name)}</span>`, `<span>${esc(b.name)}</span>`, rows, keep)}
      <div class="compare-actions">
        <button class="danger small" data-role="merge" ${overlap.distinctMbids ? "disabled" : ""}>Merge “${esc(awayX.name)}” into “${esc(keepX.name)}”</button>
        ${keys && !overlap.distinctMbids ? `${keyBtn("1", "Keep left & merge", "", 'data-role="keep-a"')}${keyBtn("2", "Keep right & merge", "", 'data-role="keep-b"')}` : ""}
        <span class="meta">${overlap.distinctMbids ? "Correct one of the MBIDs first if one is wrong." : "Undoable from the activity feed."}</span>
      </div>`;
    slot.querySelectorAll(`input[name="${name}"]`).forEach((r) => r.addEventListener("change", () => { keep = r.value; paint(); }));
    slot.querySelector("[data-role='merge']").addEventListener("click", () => doMerge(keep));
    slot.querySelector("[data-role='keep-a']")?.addEventListener("click", () => doMerge("a"));
    slot.querySelector("[data-role='keep-b']")?.addEventListener("click", () => doMerge("b"));
  }

  async function doMerge(side) {
    const [keepX, awayX] = side === "a" ? [a, b] : [b, a];
    slot.querySelectorAll("button").forEach((x) => (x.disabled = true));
    const res = await api("/api/artists/merge", { absorbedId: awayX.artistId, canonicalId: keepX.artistId });
    if (!res.ok) {
      slot.querySelectorAll("button").forEach((x) => (x.disabled = false));
      toast(esc(res.data.message), { error: true });
      return;
    }
    const moved = movedSummary(res.data.rowsMoved);
    toast(`Merged <b>${esc(awayX.name)}</b> into <b>${esc(keepX.name)}</b>${moved ? ` — ${esc(moved)}` : ""}`, { undo: { kind: "merge", id: res.data.logId } });
    notifyChanged();
    onMerged?.(res.data);
  }
  paint();
}

function albumLi(al) {
  return `<li>${esc(al.title)}${al.year ? ` <span class="meta">(${al.year})</span>` : ""} ${al.vinyl ? `<span class="badge vinyl">vinyl</span>` : ""} ${al.mbid ? mbLink("album", al.mbid) : ""} <span class="meta">${fmtNum(al.plays)}</span></li>`;
}

// Assigns an mbid; on conflict (already another local artist's) shows the comparison so the
// two can be merged right there. onDone() after either outcome succeeds.
async function assignArtistMbid(artist, mbid, slot, reason, onDone) {
  const res = await api("/api/artists/assign-mbid", { artistId: artist.artistId, mbid, reason });
  if (res.status === 409 && res.data.conflictingArtist) {
    const other = res.data.conflictingArtist;
    slot.innerHTML = `<div class="notice warn"><div class="headline">${esc(res.data.message)}</div><div class="meta">If they're the same act, merge them — the one with the MBID is pre-selected to keep.</div><div data-role="cmp"></div></div>`;
    renderArtistCompare(slot.querySelector("[data-role='cmp']"), artist.artistId, other.artistId, { defaultKeep: "b", keys: false, onMerged: onDone });
    return;
  }
  if (!res.ok) { toast(esc(res.data.message), { error: true }); return; }
  toast(`Assigned ${mbLink("artist", mbid)} to <b>${esc(res.data.name)}</b>`, { undo: res.data.editId ? { kind: "edit", id: res.data.editId } : undefined });
  notifyChanged();
  onDone?.(res.data);
}

// The artist's actual scrobbles (newest first) as a compact table -- for low-volume artists,
// what was played is the quickest way to tell who they really are.
async function renderScrobbles(el, artistId, limit = 50) {
  el.innerHTML = `<div class="status-line">Loading…</div>`;
  const { ok, data } = await api(`/api/artists/scrobbles?id=${artistId}&limit=${limit}`);
  if (!ok) { el.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
  if (!data.scrobbles.length) { el.innerHTML = `<div class="meta">No scrobbles.</div>`; return; }
  el.innerHTML = `<div class="plays-list"><table class="data"><tbody>${data.scrobbles.map((x) => `
    <tr><td><time>${esc(fmtDate(x.playedAt))}</time></td><td>${esc(x.track)}</td><td class="meta">${x.album ? esc(x.album) : "<span class='muted'>no album</span>"}</td></tr>`).join("")}
  </tbody></table></div>${data.total > data.scrobbles.length ? `<div class="meta">…and ${fmtNum(data.total - data.scrobbles.length)} older</div>` : ""}`;
}

// A clickable "N scrobbles" that toggles the scrobble list into `target`.
function playsButton(artistId, count, target) {
  const btn = document.createElement("button");
  btn.className = "link meta";
  btn.title = "Show what was scrobbled";
  btn.textContent = `${plural(count, "scrobble")} ▾`;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (target.childElementCount) { target.innerHTML = ""; btn.textContent = `${plural(count, "scrobble")} ▾`; return; }
    btn.textContent = `${plural(count, "scrobble")} ▴`;
    renderScrobbles(target, artistId);
  });
  return btn;
}

// How well one MusicBrainz candidate's discography matches the albums we have for this artist.
function albumMatchHtml(m) {
  if (!m.albumsChecked) return `<span class="muted">no local albums to compare</span>`;
  return m.albumsMatched.length
    ? `<span class="ok-text">matched ${m.albumsMatched.length} of ${m.albumsChecked} of your albums</span>: ${m.albumsMatched.slice(0, 5).map(esc).join(" · ")}`
    : `<span class="warn-text">none of your ${m.albumsChecked} albums found in its discography</span>${m.releaseGroupCount != null ? ` (${fmtNum(m.releaseGroupCount)} release groups)` : ""}`;
}

// Top N search results get their discography checked automatically (sequentially -- MusicBrainz
// is 1 request/second anyway, and each result is cached); the rest get a button to check on demand.
const AUTO_ALBUM_CHECKS = 3;

// The full manual toolkit for one artist: what's been played, MB search, manual id, merge.
function renderArtistResolve(slot, artist, { onDone, allowNoMbid = true, reason = "assigned" } = {}) {
  slot.innerHTML = `
    <div class="resolve">
      <div><h3>What's been played</h3><div data-role="played" class="meta">Loading…</div></div>
      <div><h3>Top albums</h3><div data-role="albums" class="meta">Loading…</div></div>
      <div class="full">
        <h3>Search MusicBrainz</h3>
        <div class="inline-form"><input type="text" data-role="q" value="${esc(artist.name)}" /><button data-role="search">Search</button></div>
        <div data-role="results"></div>
      </div>
      <div>
        <h3>Enter an MBID</h3>
        <div class="inline-form"><input type="text" data-role="manual" placeholder="artist MBID or musicbrainz.org URL" /><button data-role="assign">Assign</button></div>
      </div>
      <div>
        <h3>Same as an artist you already have?</h3>
        <input type="text" data-role="merge-q" placeholder="Search your artists…" />
      </div>
      <div class="full"><h3>Also releases as <span class="meta" style="text-transform:none">— other MusicBrainz artists that count as ${esc(artist.name)} (a band name, a “&amp; band” credit…)</span></h3>
        <div data-role="aliases" class="meta">Loading…</div></div>
      <div class="full" data-role="slot"></div>
      ${allowNoMbid ? `<div class="full"><button class="small" data-role="nombid">Not on MusicBrainz — stop suggesting</button></div>` : ""}
    </div>`;
  const $ = (r) => slot.querySelector(`[data-role='${r}']`);
  const conflictSlot = $("slot");

  renderScrobbles($("played"), artist.artistId, 25);
  let ownMbid = null;
  const paintAliases = (list) => {
    const el = $("aliases");
    el.innerHTML = list.length ? list.map((x) => `<span class="chip">${esc(x.name || x.mbid)} ${mbLink("artist", x.mbid)} <button data-alias="${x.id}" title="Remove">✕</button></span>`).join(" ")
      : `none — add one from the search results below (“Also releases as”)`;
    el.querySelectorAll("[data-alias]").forEach((b) => b.addEventListener("click", async () => {
      const r = await api("/api/artists/mb-alias-remove", { id: parseInt(b.dataset.alias, 10) });
      if (!r.ok) { toast(esc(r.data.message), { error: true }); return; }
      toast(`<b>${esc(r.data.name)}</b> no longer counts as ${esc(artist.name)}`);
      notifyChanged();
      b.closest(".chip").remove();
    }));
  };
  api(`/api/artists/detail?id=${artist.artistId}`).then(({ ok, data }) => {
    if (!ok) return;
    ownMbid = data.mbid;
    paintAliases(data.aliases || []);
    $("albums").innerHTML = data.topAlbums.length ? `<ol>${data.topAlbums.map(albumLi).join("")}</ol>` : "None.";
  });

  let searchSeq = 0;
  async function checkAlbums(evEl, mbid) {
    evEl.innerHTML = `<span class="muted">checking your albums against its discography…</span>`;
    const { ok, data } = await api(`/api/artists/album-match?artistId=${artist.artistId}&mbid=${encodeURIComponent(mbid)}`);
    evEl.innerHTML = ok ? albumMatchHtml(data) : `<span class="error">${esc(data.message)}</span>`;
  }
  async function search(q) {
    const el = $("results");
    const mine = ++searchSeq;
    if (!q) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="status-line">Searching…</div>`;
    const { ok, data } = await api(`/api/artists/mb-search?q=${encodeURIComponent(q)}`);
    if (!ok) { el.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
    if (!data.candidates.length) { el.innerHTML = `<div class="status-line">No matches.</div>`; return; }
    el.innerHTML = "";
    for (const c of data.candidates) {
      const div = document.createElement("div");
      div.className = "candidate";
      const years = c.beginDate ? ` · ${esc(c.beginDate)}–${c.ended ? esc(c.endDate || "") : ""}` : "";
      div.innerHTML = `
        <div class="cand-main">
          <strong>${esc(c.name)}</strong>${c.disambiguation ? ` <span class="meta">(${esc(c.disambiguation)})</span>` : ""} ${mbLink("artist", c.mbid)}
          <div class="meta">${esc(c.type || "")}${c.country ? ` · ${esc(c.country)}` : ""}${years} · MB score ${c.score}</div>
          <div class="meta" data-role="albums-ev"></div>
          ${c.alreadyLinkedTo ? `<div class="meta warn-text">Already linked to your artist “${esc(c.alreadyLinkedTo.name)}”</div>` : ""}
        </div>
        <div class="actions"><button class="small ${c.alreadyLinkedTo ? "" : "good"}">${c.alreadyLinkedTo ? "Compare & merge" : "Assign"}</button>
          ${c.alreadyLinkedTo ? "" : `<button class="small" data-role="as-alias" title="Keep ${esc(artist.name)}'s own MBID, and also count this MusicBrainz artist as ${esc(artist.name)}">Also releases as</button>`}</div>`;
      div.querySelector("[data-role='as-alias']")?.addEventListener("click", async () => {
        if (await addMbAlias(artist.artistId, c.mbid, c.name)) {
          const r = await api(`/api/artists/mb-aliases?artistId=${artist.artistId}`);
          if (r.ok) paintAliases(r.data.aliases);
        }
      });
      div.querySelector("button").addEventListener("click", () => {
        if (c.alreadyLinkedTo) {
          renderArtistCompare(conflictSlot, artist.artistId, c.alreadyLinkedTo.artistId, { defaultKeep: "b", keys: false, onMerged: onDone });
        } else {
          assignArtistMbid(artist, c.mbid, conflictSlot, reason, onDone);
        }
      });
      el.appendChild(div);
      c._ev = div.querySelector("[data-role='albums-ev']");
    }
    data.candidates.forEach((c, i) => {
      if (i < AUTO_ALBUM_CHECKS) return;
      c._ev.innerHTML = `<button class="link meta">check against your albums</button>`;
      c._ev.querySelector("button").addEventListener("click", () => checkAlbums(c._ev, c.mbid));
    });
    for (const c of data.candidates.slice(0, AUTO_ALBUM_CHECKS)) {
      if (mine !== searchSeq || !slot.isConnected) return; // a newer search, or the panel closed
      await checkAlbums(c._ev, c.mbid);
    }
  }
  $("search").addEventListener("click", () => search($("q").value.trim()));
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search($("q").value.trim()); });
  $("assign").addEventListener("click", () => {
    const m = $("manual").value.trim().match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (!m) { toast("That doesn't contain an MBID.", { error: true }); return; }
    assignArtistMbid(artist, m[0].toLowerCase(), conflictSlot, reason, onDone);
  });
  mountTypeahead($("merge-q"), {
    fetchItems: async (q) => (await api(`/api/artists/local-search?q=${encodeURIComponent(q)}&excludeId=${artist.artistId}`)).data.results || [],
    renderItem: (r) => `<span>${esc(r.name)} ${r.mbid ? `<span class="badge ok">mbid</span>` : ""}</span><span class="meta">${fmtNum(r.scrobbleCount)} plays · match ${r.score}</span>`,
    onPick: (r) => renderArtistCompare(conflictSlot, artist.artistId, r.artistId, { keys: false, onMerged: onDone }),
  });
  $("nombid")?.addEventListener("click", async () => {
    await markNoMbid(artist.artistId);
    toast(`<b>${esc(artist.name)}</b> marked as not on MusicBrainz`);
    onDone?.();
  });
  search(artist.name);
}

async function markNoMbid(artistId) {
  await api("/api/review/mark", { entityType: "artist", entityId: artistId, mark: "no-mbid" });
  notifyChanged();
}
