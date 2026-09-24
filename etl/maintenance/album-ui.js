/*
 * Album review components shared by albums.html and vinyl.html: side-by-side comparison with a
 * pick-the-primary merge, the MusicBrainz identity toolkit (search / browse the artist's
 * discography / local near-matches / manual id, with mbid-conflict -> compare-and-merge), and
 * the keep-radio + merge-checkbox table. Needs shared.js first.
 */

function thumb(a) {
  return a.coverStatus === "ok" ? `<img class="thumb" src="/covers/${a.albumId}.jpg" alt="" loading="lazy" />` : `<span class="thumb none">—</span>`;
}
function mbTypeText(a) {
  if (!a.mb) return "";
  const types = [a.mb.primaryType, ...(a.mb.secondaryTypes || [])].filter(Boolean).join("/");
  return `<span class="meta">${esc(types)}${a.mb.firstReleaseDate ? ` · ${esc(a.mb.firstReleaseDate.slice(0, 4))}` : ""}</span>`;
}
function vinylBadges(a) {
  return (a.vinyl || []).map((v) => `<span class="badge vinyl" title="${esc([v.format, v.label, v.catalogNumber].filter(Boolean).join(" · "))}">vinyl</span>${v.discogsReleaseId ? " " + discogsLink(v.discogsReleaseId, "discogs") : ""}`).join(" ");
}
// "2018–2026" (full dates on hover) -- keeps merge-table rows to a single line.
function playedRange(a) {
  if (!a.lastPlayed) return "never";
  const f = a.firstPlayed.slice(0, 4), l = a.lastPlayed.slice(0, 4);
  return f === l ? f : `${f}–${l}`;
}
function tagBadges(a) {
  return a.tags && a.tags.length ? `<span class="t-tags">${a.tags.map((t) => `<span class="badge">${esc(t)}</span>`).join("")}</span>` : "";
}
function overlapPct(a, b) {
  const ka = new Set(a.trackKeys || []), kb = new Set(b.trackKeys || []);
  const union = new Set([...ka, ...kb]);
  if (!union.size) return null;
  let shared = 0; for (const k of ka) if (kb.has(k)) shared++;
  return Math.round((100 * shared) / union.size);
}
function mergeToast(res, verb = "Merged") {
  const d = res.data;
  const names = d.merged ? d.merged.map((m) => m.title) : [d.absorbedTitle];
  toast(`${verb} ${names.map((n) => `<b>${esc(n)}</b>`).join(", ")} into <b>${esc(d.canonicalTitle)}</b>`,
    { undo: { kind: "merge", id: d.logIds || d.logId } });
  notifyChanged();
}

// Side-by-side comparison of two albums with keep radios. `identity` (optional) is applied to
// the survivor -- e.g. the release group that was being assigned when the mbid conflict came up.
let cmpSeq = 0;
async function renderAlbumCompare(slot, aId, bId, { onMerged, defaultKeep, keys = true, identity, summary: showSummary = true } = {}) {
  slot.innerHTML = `<div class="status-line">Loading comparison…</div>`;
  const { ok, data } = await api(`/api/albums/compare?a=${aId}&b=${bId}`);
  if (!ok) { slot.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
  const { a, b, overlap } = data;
  const score = (x) => [x.vinylCount > 0, !!x.mbid, !x.tags.length, x.scrobbleCount];
  const better = (x, y) => { const sx = score(x), sy = score(y); for (let i = 0; i < sx.length; i++) if (sx[i] !== sy[i]) return sx[i] > sy[i]; return true; };
  let keep = defaultKeep || (better(a, b) ? "a" : "b");
  const name = `akeep-${++cmpSeq}`;
  const mbCell = (x) => x.mbid ? `${mbLink("album", x.mbid, x.mbid)}<div>${mbTypeText(x)}</div>` : `<span class="badge warn">no mbid</span>`;
  const raw = (x) => Object.entries(x.rawTitles).filter(([, v]) => v.length).map(([src, v]) => `<div><span class="meta">${src}:</span> ${v.map(esc).join(" · ")}</div>`).join("") || "—";
  const rows = [
    { label: "Cover", a: thumb(a), b: thumb(b) },
    { label: "MusicBrainz", a: mbCell(a), b: mbCell(b), same: a.mbid && a.mbid === b.mbid },
    { label: "Year", a: a.year ?? "—", b: b.year ?? "—" },
    { label: "Scrobbles", a: fmtNum(a.scrobbleCount), b: fmtNum(b.scrobbleCount) },
    { label: "Played", a: `${fmtDate(a.firstPlayed)} → ${fmtDate(a.lastPlayed)}`, b: `${fmtDate(b.firstPlayed)} → ${fmtDate(b.lastPlayed)}` },
    { label: "Vinyl", a: vinylBadges(a) || "—", b: vinylBadges(b) || "—" },
    { label: "Tracks", a: fmtNum(a.songCount), b: fmtNum(b.songCount) },
    { label: "Titles seen", a: raw(a), b: raw(b) },
    { label: "Merged in", a: fmtNum(a.mergedIn), b: fmtNum(b.mergedIn) },
  ];
  const badges = [
    overlap.sameBaseTitle ? `<span class="badge high">same title without edition words</span>` : "",
    overlap.trackOverlapPct != null ? `<span class="badge ${overlap.trackOverlapPct >= 50 ? "high" : overlap.trackOverlapPct ? "medium" : ""}">${overlap.trackOverlapPct}% tracks shared (${overlap.tracks})</span>` : "",
    overlap.distinctMbids ? `<span class="badge warn">different MBIDs — the absorbed one is dropped</span>` : "",
    overlap.sequelMismatch ? `<span class="badge bad">different numbers — probably different albums</span>` : "",
    !overlap.sameArtist ? `<span class="badge bad">different artists — can't merge</span>` : "",
  ].join("");

  function paint() {
    const [keepX, awayX] = keep === "a" ? [a, b] : [b, a];
    slot.innerHTML = `
      ${showSummary ? `<div class="compare-summary">${badges}</div>` : ""}
      ${compareGrid(name, `<span>${esc(a.title)}</span>`, `<span>${esc(b.title)}</span>`, rows, keep)}
      <div class="compare-actions">
        <button class="danger small" data-role="merge" ${overlap.sameArtist ? "" : "disabled"}>Merge “${esc(awayX.title)}” into “${esc(keepX.title)}”</button>
        ${keys && overlap.sameArtist ? `${keyBtn("1", "Keep left & merge", "", 'data-role="keep-a"')}${keyBtn("2", "Keep right & merge", "", 'data-role="keep-b"')}` : ""}
        ${identity?.mbid ? `<span class="meta">The kept album gets ${mbLink("album", identity.mbid)}${identity.title ? ` and the title “${esc(identity.title)}”` : ""}.</span>` : `<span class="meta">Undoable from the activity feed.</span>`}
      </div>`;
    slot.querySelectorAll(`input[name="${name}"]`).forEach((r) => r.addEventListener("change", () => { keep = r.value; paint(); }));
    slot.querySelector("[data-role='merge']").addEventListener("click", () => doMerge(keep));
    slot.querySelector("[data-role='keep-a']")?.addEventListener("click", () => doMerge("a"));
    slot.querySelector("[data-role='keep-b']")?.addEventListener("click", () => doMerge("b"));
  }
  async function doMerge(side) {
    const [keepX, awayX] = side === "a" ? [a, b] : [b, a];
    slot.querySelectorAll("button").forEach((x) => (x.disabled = true));
    const res = await api("/api/albums/merge", { absorbedId: awayX.albumId, canonicalId: keepX.albumId, identity: identity || null });
    if (!res.ok) { slot.querySelectorAll("button").forEach((x) => (x.disabled = false)); toast(esc(res.data.message), { error: true }); return; }
    mergeToast(res);
    onMerged?.(res.data);
  }
  paint();
}

// A release-group candidate row: "Use this" (assign) or, if another local album already has it,
// "Compare & merge".
function rgRow(album, rg, host, { onDone, reason }) {
  const div = document.createElement("div");
  div.className = "candidate";
  const types = [rg.primaryType, ...(rg.secondaryTypes || [])].filter(Boolean).join("/");
  const isCurrent = album.mbid && rg.mbid === album.mbid;
  const linked = rg.linkedTo && rg.linkedTo.albumId !== album.albumId ? rg.linkedTo : null;
  div.innerHTML = `
    <div>
      <strong>${esc(rg.title)}</strong>${rg.disambiguation ? ` <span class="meta">(${esc(rg.disambiguation)})</span>` : ""} ${mbLink("album", rg.mbid)}
      <div class="meta">${esc(rg.artistCredit || "")}${types ? ` · ${esc(types)}` : ""}${rg.firstReleaseDate ? ` · ${esc(rg.firstReleaseDate)}` : ""}${rg.score != null ? ` · score ${rg.score}` : ""}${rg.similarity != null ? ` · title match ${rg.similarity}` : ""}</div>
      ${linked ? `<div class="meta warn-text">Already your album “${esc(linked.title)}”</div>` : ""}
    </div>
    <div class="actions"><button class="small ${linked ? "" : "good"}" ${isCurrent ? "disabled" : ""}>${isCurrent ? "Current" : linked ? "Compare & merge" : "Use this"}</button></div>`;
  const identity = { mbid: rg.mbid, title: rg.title, year: rg.firstReleaseDate ? parseInt(rg.firstReleaseDate.slice(0, 4), 10) : null };
  div.querySelector("button").addEventListener("click", () => {
    if (linked) renderAlbumCompare(host, album.albumId, linked.albumId, { keys: false, defaultKeep: "b", identity, onMerged: onDone });
    else assignAlbum(album, identity, host, reason, onDone);
  });
  return div;
}

async function assignAlbum(album, identity, host, reason, onDone) {
  const res = await api("/api/albums/assign-mbid", { albumId: album.albumId, mbid: identity.mbid, title: identity.title || undefined, year: identity.year || undefined, reason });
  if (res.status === 409 && res.data.conflictingAlbum) {
    host.innerHTML = `<div class="notice warn"><div class="headline">${esc(res.data.message)}</div><div class="meta">Same record? Pick which one to keep — it gets this MBID and title.</div><div data-role="cmp"></div></div>`;
    renderAlbumCompare(host.querySelector("[data-role='cmp']"), album.albumId, res.data.conflictingAlbum.albumId, { keys: false, identity, onMerged: onDone });
    return;
  }
  if (!res.ok) { toast(esc(res.data.message), { error: true }); return; }
  toast(`Assigned ${mbLink("album", res.data.mbid)} to <b>${esc(res.data.title)}</b>${res.data.resolvedFromRelease ? " (resolved from a release id)" : ""}`, { undo: res.data.editId ? { kind: "edit", id: res.data.editId } : undefined });
  notifyChanged();
  onDone?.(res.data);
}

// The full toolkit for one album's identity: tracks, near matches in your library, MB search,
// the artist's whole MB discography, manual id.
function renderAlbumResolve(slot, album, { onDone, reason = "assigned" } = {}) {
  slot.innerHTML = `
    <div class="resolve">
      <div><h3>Tracks played</h3><div data-role="tracks" class="meta">Loading…</div></div>
      <div><h3>Similar albums you already have</h3><div data-role="near" class="meta">Loading…</div></div>
      <div class="full">
        <h3>Search MusicBrainz ${album.artistMbid ? `<span class="meta">(within ${esc(album.artistName)})</span>` : ""}</h3>
        <div class="inline-form"><input type="text" data-role="q" value="${esc(album.baseTitle || album.title)}" /><button data-role="search">Search</button>
          <button data-role="browse" ${album.artistMbid ? "" : 'disabled title="The artist needs an MBID first"'}>Browse ${esc(album.artistName)}'s discography</button></div>
        <div data-role="results"></div>
      </div>
      <div class="full">
        <h3>Enter an MBID</h3>
        <div class="inline-form"><input type="text" data-role="manual" placeholder="release-group or release MBID / URL" /><button data-role="assign">Assign</button>
          <span class="meta">a release id is resolved to its release group automatically</span></div>
      </div>
      <div class="full" data-role="slot"></div>
    </div>`;
  const $ = (r) => slot.querySelector(`[data-role='${r}']`);
  const host = $("slot");

  api(`/api/albums/tracklist?id=${album.albumId}`).then(({ ok, data }) => {
    if (!ok) return;
    $("tracks").innerHTML = data.songs.length ? `<ol>${data.songs.slice(0, 15).map((s) => `<li>${esc(s.title)} <span class="meta">${fmtNum(s.scrobbleCount)}</span></li>`).join("")}</ol>` : "None recorded.";
  });
  api(`/api/albums/near-matches?albumId=${album.albumId}`).then(({ ok, data }) => {
    if (!ok) return;
    const el = $("near");
    if (!data.matches.length) { el.innerHTML = "None."; return; }
    el.innerHTML = "";
    for (const m of data.matches) {
      const row = document.createElement("div");
      row.className = "candidate";
      row.innerHTML = `<div>${esc(m.title)} ${m.year ? `<span class="meta">(${m.year})</span>` : ""} ${m.mbid ? mbLink("album", m.mbid) : ""} ${vinylBadges(m)}
        <div class="meta">${fmtNum(m.scrobbleCount)} plays · title match ${m.similarity}</div></div>
        <div class="actions"><button class="small">Compare & merge</button></div>`;
      row.querySelector("button").addEventListener("click", () => renderAlbumCompare(host, album.albumId, m.albumId, { keys: false, onMerged: onDone }));
      el.appendChild(row);
    }
  });

  async function search(q) {
    const el = $("results");
    if (!q) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="status-line">Searching…</div>`;
    const params = new URLSearchParams({ q, artistName: album.artistName });
    if (album.artistMbid) params.set("artistMbid", album.artistMbid);
    const { ok, data } = await api(`/api/albums/mb-search?${params}`);
    if (!ok) { el.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
    if (!data.candidates.length) {
      el.innerHTML = `<div class="status-line">No matches.${album.artistMbid ? " Try browsing the artist's whole discography instead." : ""}</div>`;
      return;
    }
    el.innerHTML = "";
    for (const c of data.candidates) el.appendChild(rgRow(album, c, host, { onDone, reason }));
  }

  async function browse() {
    const el = $("results");
    el.innerHTML = `<div class="status-line">Loading ${esc(album.artistName)}'s discography from MusicBrainz…</div>`;
    const { ok, data } = await api(`/api/albums/mb-discography?albumId=${album.albumId}`);
    if (!ok) { el.innerHTML = `<div class="status-line error">${esc(data.message)}</div>`; return; }
    const kinds = ["All", ...new Set(data.releaseGroups.map((g) => g.primaryType || "Other"))];
    let filter = "All";
    function paint() {
      const shown = data.releaseGroups.filter((g) => filter === "All" || (g.primaryType || "Other") === filter);
      el.innerHTML = `<div class="meta">${plural(data.count, "release group")} for ${esc(data.artistName)} ${mbLink("artist", data.artistMbid)}, closest title first</div>
        <div class="rg-types">${kinds.map((k) => `<button class="chip filter-chip" aria-pressed="${k === filter}" data-k="${esc(k)}">${esc(k)}</button>`).join("")}</div>`;
      el.querySelectorAll("[data-k]").forEach((b) => b.addEventListener("click", () => { filter = b.dataset.k; paint(); }));
      for (const g of shown.slice(0, 60)) el.appendChild(rgRow(album, g, host, { onDone, reason }));
    }
    paint();
  }

  $("search").addEventListener("click", () => search($("q").value.trim()));
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search($("q").value.trim()); });
  $("browse").addEventListener("click", browse);
  $("assign").addEventListener("click", () => {
    const m = $("manual").value.trim().match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (!m) { toast("That doesn't contain an MBID.", { error: true }); return; }
    assignAlbum(album, { mbid: m[0].toLowerCase() }, host, reason, onDone);
  });
  search(album.baseTitle || album.title);
}


// A table of albums with a keep-radio and a merge checkbox per row, plus the merge controls.
function mergeTable(albums, { checkedAll, primary, mergeKey, compact, highlight, onMerged }) {
  const wrap = document.createElement("div");
  const state = {
    primary: primary ?? (checkedAll ? albums[0].albumId : null),
    checked: new Set(checkedAll ? albums.map((x) => x.albumId) : []),
  };
  const byId = Object.fromEntries(albums.map((x) => [x.albumId, x]));
  const name = `prim-${++cmpSeq}`;

  function paint() {
    const p = byId[state.primary];
    const included = albums.filter((x) => state.checked.has(x.albumId) && x.albumId !== state.primary);
    const mbids = new Set(albums.filter((x) => state.checked.has(x.albumId) || x.albumId === state.primary).map((x) => x.mbid).filter(Boolean));
    wrap.innerHTML = `
      <div class="table-scroll"><table class="data merge">
        <thead><tr><th title="Keep this one">Keep</th><th title="Merge into the kept one">Merge</th><th></th><th>Title</th><th>Year</th><th>MusicBrainz</th><th class="num">Plays</th><th>Vinyl</th><th class="num">Tracks</th>${compact ? "" : `<th class="num">Shared</th>`}<th>Played</th></tr></thead>
        <tbody>${albums.map((x) => {
          const isP = x.albumId === state.primary;
          const on = state.checked.has(x.albumId);
          const ov = p && !isP && !compact ? overlapPct(p, x) : null;
          return `<tr data-id="${x.albumId}" class="${isP ? "primary" : ""} ${!isP && !on && !compact ? "excluded" : ""} ${x.albumId === highlight ? "flash" : ""}">
            <td><input type="radio" name="${name}" value="${x.albumId}" ${isP ? "checked" : ""} /></td>
            <td><input type="checkbox" data-id="${x.albumId}" ${on && !isP ? "checked" : ""} ${isP ? "disabled" : ""} /></td>
            <td>${thumb(x)}</td>
            <td><span class="t-title">${esc(x.title)}</span>${tagBadges(x)}${x.mergedIn ? ` <span class="meta" title="versions already merged into this one">+${x.mergedIn} merged</span>` : ""}</td>
            <td>${x.year ?? "<span class='muted'>—</span>"}</td>
            <td>${x.mbid ? mbLink("album", x.mbid) : `<span class="badge warn">none</span>`} ${mbTypeText(x)}</td>
            <td class="num">${fmtNum(x.scrobbleCount)}</td>
            <td>${vinylBadges(x)}</td>
            <td class="num">${fmtNum(x.songCount)}</td>
            ${compact ? "" : `<td class="num">${isP ? "" : ov == null ? "—" : ov + "%"}</td>`}
            <td class="meta nowrap" title="${x.lastPlayed ? `${fmtDate(x.firstPlayed)} → ${fmtDate(x.lastPlayed)}` : ""}">${playedRange(x)}</td>
          </tr>`;
        }).join("")}</tbody>
      </table></div>
      <div class="merge-foot">
        ${included.length && p ? `
          <label class="meta">Title after merge <input type="text" data-role="title" value="${esc(p.title)}" /></label>
          ${p.tags.length ? `<button class="small" data-role="clean">Use “${esc(p.baseTitle)}”</button>` : ""}
          <label class="meta">Year <input type="number" data-role="year" value="${p.year ?? ""}" /></label>
          ${mergeKey ? keyBtn(mergeKey, `Merge ${included.length} into “${esc(p.title)}”`, "danger", 'data-role="merge"') : `<button class="small danger" data-role="merge">Merge ${included.length} into “${esc(p.title)}”</button>`}
          ${mbids.size > 1 ? `<span class="badge warn" title="Only one MBID can survive — the kept album's (or, if it has none, the first merged one's)">${mbids.size} different MBIDs — only the kept one survives</span>` : ""}
          ${!p.mbid && mbids.size ? `<span class="meta">the kept album inherits an MBID from a merged one</span>` : ""}
        ` : `<span class="meta">${p ? "Tick the albums to merge into the kept one." : "Pick the album to keep (radio), then tick what to merge into it."}</span>`}
      </div>`;
    wrap.querySelectorAll(`input[name="${name}"]`).forEach((r) => r.addEventListener("change", () => {
      state.primary = parseInt(r.value, 10);
      state.checked.delete(state.primary);
      if (checkedAll) albums.forEach((x) => x.albumId !== state.primary && state.checked.add(x.albumId));
      paint();
    }));
    wrap.querySelectorAll("input[type='checkbox'][data-id]").forEach((c) => c.addEventListener("change", () => {
      const id = parseInt(c.dataset.id, 10);
      c.checked ? state.checked.add(id) : state.checked.delete(id);
      paint();
    }));
    wrap.querySelector("[data-role='clean']")?.addEventListener("click", () => { wrap.querySelector("[data-role='title']").value = p.baseTitle; });
    wrap.querySelector("[data-role='merge']")?.addEventListener("click", () => doMerge(included.map((x) => x.albumId)));
    // Enter in the title/year fields merges too (keys like "m" would just type there).
    wrap.querySelectorAll("[data-role='title'], [data-role='year']").forEach((inp) => inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); doMerge(included.map((x) => x.albumId)); }
    }));
  }

  async function doMerge(absorbedIds) {
    const p = byId[state.primary];
    const title = wrap.querySelector("[data-role='title']").value.trim();
    const year = parseInt(wrap.querySelector("[data-role='year']").value, 10);
    const identity = {};
    if (title && title !== p.title) identity.title = title;
    if (year && year !== p.year) identity.year = year;
    wrap.querySelectorAll("button, input").forEach((x) => (x.disabled = true));
    const res = await api("/api/albums/merge-group", { canonicalId: p.albumId, absorbedIds, identity: Object.keys(identity).length ? identity : null });
    if (!res.ok) { wrap.querySelectorAll("button, input").forEach((x) => (x.disabled = false)); toast(esc(res.data.message), { error: true }); return; }
    mergeToast(res);
    onMerged?.(res.data);
  }
  paint();
  return wrap;
}
