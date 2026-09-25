// Songs > Splits / Tracklists / Wrong album. See api/songtools.py.

const sgRow = (m) => `<span class="sg-row${m.setlistCount && !m.scrobbleCount ? " live-only" : ""}" title="${esc(m.albumTitle || "no album")}">${esc(m.title)}
  <i>${m.scrobbleCount ? plural(m.scrobbleCount, "play") : ""}${m.scrobbleCount && m.setlistCount ? " · " : ""}${m.setlistCount ? `<b class="livedot">●</b> ${plural(m.setlistCount, "show")}` : ""}</i>
  ${(m.tags || []).map((t) => `<span class="badge warn">${esc(t)}</span>`).join("")}</span>`;

// -- Splits ------------------------------------------------------------------------------------
function mountSplits(outer) {
  outer.innerHTML = `<div data-role="variants"></div><div data-role="splits"></div>`;
  const vroot = outer.querySelector("[data-role='variants']"), root = outer.querySelector("[data-role='splits']");
  // live / remix / demo recordings that were merged into the studio song -- ticked to un-merge
  async function loadVariants() {
    const { ok, data } = await api("/api/songs/variant-merges");
    if (!ok || !data.count) { vroot.innerHTML = ""; return; }
    vroot.innerHTML = `<p class="meta" style="margin:0 0 4px">A live, remix or demo recording is a different track from the studio song — merged into it, its plays and shows disappear inside the studio one. These merges folded one into a plain song; the ticked ones are un-merged back into their own song (with their plays and shows), each on its own.</p>
      <div data-role="list"></div>`;
    mountTickList(vroot.querySelector("[data-role='list']"), data.items.map((x) => ({
      id: x.logId, ticked: !x.gone && x.tags.includes("live"), // a "2004 Remix" is often just the remaster -- your call
      cells: [`<b>${esc(x.absorbed)}</b><div class="meta">${esc(x.artist || "")}${x.album ? ` · ${esc(x.album)}` : ""}</div>`,
        `${x.tags.map((t) => `<span class="badge accent">${esc(t)}</span>`).join(" ")}<div class="meta">${plural(x.plays, "play")}${x.shows ? ` · ${plural(x.shows, "show")}` : ""}</div>`,
        x.gone ? `<span class="meta">“${esc(x.canonical)}” was merged away since — undo that first</span>`
          : `<b>${esc(x.canonical)}</b><div class="meta">${esc(x.canonicalAlbum || "")} · merged ${esc(String(x.mergedAt).slice(0, 10))}</div>`],
    })), {
      head: `Live, demo & remix versions merged into studio songs <span class="meta">${fmtNum(data.count)}</span>`,
      cols: ["Merged-away recording", "Kind", "Merged into"],
      buttons: [["unmerge", "Un-merge ticked", "good"]],
      onAction: async (_act, ids) => {
        const { ok: ok2, data: d2 } = await api("/api/songs/undo-variant-merges", { ids });
        if (!ok2) { toast(esc(d2.message), { error: true, timeout: 9000 }); return; }
        const failed = d2.failed.length ? ` · ${plural(d2.failed.length, "couldn't be undone", "couldn't be undone")}: ${d2.failed.map((f) => esc(f.error)).join("; ")}` : "";
        toast(`${plural(d2.undone.length, "recording")} un-merged${failed}`, { undo: d2.undo ? { ...d2.undo, onUndone: () => { loadVariants(); load(); } } : undefined, error: !d2.undone.length, timeout: failed ? 12000 : undefined });
        notifyChanged(); loadVariants(); load();
      },
    });
  }
  async function load() {
    root.innerHTML = `<div class="empty">Loading…</div>`;
    const { ok, data } = await api("/api/songs/splits");
    if (!ok) { root.innerHTML = `<div class="empty">${esc(data.message)}</div>`; return; }
    if (!data.count) { root.innerHTML = `<div class="empty">No splits — every track's live plays and scrobbles are on the same song.</div>`; return; }
    root.innerHTML = `<p class="meta" style="margin:16px 0 4px">These tracks have their live sightings on one song row (setlist.fm's plain title) and their scrobbles on another (a remaster, a "feat." spelling…). Merging each into one fixes every live count, dot and stat for it. The ${data.clean} where every row is the same recording are ticked; ones with a live, remix or demo version are left for you.</p>
      <div data-role="list"></div>`;
    mountTickList(root.querySelector("[data-role='list']"), data.splits.map((x, i) => ({
      id: i, ticked: x.clean,
      cells: [`<b>${esc(x.baseTitle)}</b><div class="meta">${esc(x.artistName)}</div>`,
        `<div class="sg-rows">${x.members.map(sgRow).join("")}</div>`,
        `→ <b>${esc(x.members.find((m) => m.songId === x.primary).title)}</b><div class="meta">${plural(x.plays, "play")} · ${plural(x.shows, "show")}</div>`],
    })), {
      head: `Live ↔ scrobbled splits <span class="meta">${fmtNum(data.count)}</span>`,
      cols: ["Track", "Its song rows", "Merged into"],
      buttons: [["merge", "Merge ticked", "good"]],
      onAction: async (_act, idx) => {
        const items = idx.flatMap((i) => data.splits[i].absorbed.map((a) => ({ type: "merge", absorbedId: a, canonicalId: data.splits[i].primary })));
        const { ok: ok2, data: d2 } = await api("/api/songs/apply", { items });
        if (!ok2) { toast(esc(d2.message), { error: true, timeout: 9000 }); return; }
        toast(`${plural(idx.length, "track")} merged (${plural(d2.applied, "song row")})`, { undo: { kind: "batch", id: d2.undo } });
        notifyChanged(); load();
      },
    });
  }
  return { load: () => { loadVariants(); return load(); } };
}

// -- Tracklists --------------------------------------------------------------------------------
function mountTracklists(root) {
  root.innerHTML = `<div class="card tl-fetch">
      <div class="toolbar"><span><b>Original tracklists from MusicBrainz</b> for albums not on vinyl <span class="meta" data-role="tl-status"></span></span>
        <span class="spacer" style="flex:1"></span>
        <label>Next <input type="number" data-role="tl-limit" value="200" min="1" max="1000" style="width:80px" /> albums</label>
        <button class="primary" data-role="tl-start">Fetch tracklists</button></div>
      <div data-role="tl-sweep"></div>
      <p class="meta" style="margin:8px 0 0">The album as first released (its earliest official release), most played first, ~2 requests each — so remaster and deluxe extras show as bonus tracks on the site's album pages. Vinyl albums use your own pressing instead.</p>
    </div>
    <div class="card flush disco-shell">
      <div class="disco-top"><input type="search" class="search-box" data-role="filter" placeholder="Filter albums…" />
        <select data-role="source"><option value="vinyl">Your vinyl</option><option value="all">Vinyl + MusicBrainz tracklists</option></select>
        <label class="meta"><input type="checkbox" data-role="reviewed" /> show reviewed</label><span class="meta" data-role="total"></span></div>
      <div class="disco-body">
        <aside class="disco-side"><div class="side-head"><h3>Your vinyl</h3><span class="mono meta" data-role="count"></span></div>
          <div class="side-list" data-role="queue"><div class="meta" style="padding:8px 10px">Loading…</div></div></aside>
        <div class="disco-main" data-role="view"><div class="empty">Pick a record: its pressing's tracklist, line by line — the song rows behind each track, and anything filed under the album that isn't on it.</div></div>
      </div></div>`;
  const $ = (r) => root.querySelector(`[data-role='${r}']`);
  let queue = [], current = null;
  async function loadStatus() {
    const { ok, data } = await api("/api/songs/tracklist-status");
    if (!ok) return;
    $("tl-status").textContent = `· ${fmtNum(data.fetched)} fetched · ${fmtNum(data.toFetch)} to go${data.onEditionId ? ` · ${fmtNum(data.onEditionId)} waiting on Albums › Editions` : ""}`;
    $("tl-start").disabled = !data.toFetch;
  }
  $("tl-start").addEventListener("click", async (e) => {
    e.target.disabled = true;
    await runSweep($("tl-sweep"), "album-tracklists", parseInt($("tl-limit").value, 10) || 200);
    loadStatus(); loadQueue();
  });
  $("source").addEventListener("change", () => loadQueue());
  async function loadQueue() {
    loadStatus();
    const qs = new URLSearchParams({ ...($("reviewed").checked ? { showReviewed: "1" } : {}), source: $("source").value });
    const { ok, data } = await api(`/api/songs/tracklist-queue?${qs}`);
    if (!ok) return;
    queue = data.items;
    renderQueue();
  }
  function renderQueue() {
    const q = $("filter").value.trim().toLowerCase();
    const list = queue.filter((a) => !q || `${a.artistName} ${a.title}`.toLowerCase().includes(q));
    $("count").textContent = fmtNum(list.length);
    $("total").textContent = `${fmtNum(queue.filter((a) => a.toMerge || a.extra).length)} with something to do`;
    $("queue").innerHTML = list.map((a) => `<div class="q-row${a.albumId === current ? " current" : ""}" data-id="${a.albumId}">
      <span>${esc(a.title)}<div class="meta">${esc(a.artistName)}${a.vinyl ? "" : " · MusicBrainz"}</div></span>
      <span class="n">${a.toMerge ? `${a.toMerge} ⇄` : ""}${a.toMerge && a.extra ? " · " : ""}${a.extra ? `${a.extra} +` : ""}${!a.toMerge && !a.extra ? "✓" : ""}</span></div>`).join("");
    $("queue").querySelectorAll(".q-row").forEach((r) => r.addEventListener("click", () => open(+r.dataset.id)));
  }
  $("filter").addEventListener("input", renderQueue);
  $("reviewed").addEventListener("change", loadQueue);

  async function open(albumId) {
    current = albumId;
    renderQueue();
    const view = $("view");
    view.innerHTML = `<div class="empty">Loading…</div>`;
    const { ok, data } = await api(`/api/songs/tracklist?albumId=${albumId}`);
    if (!ok || current !== albumId) return;
    const a = data.album;
    view.innerHTML = `<div class="tl-head"><div><h2>${esc(a.title)}</h2><div class="meta"><a href="/albums.html#discography/${a.artistId}">${esc(a.artistName)}</a>${a.year ? ` · ${a.year}` : ""} ${a.mbid ? mbLink("album", a.mbid) : ""}</div></div>
        <span class="spacer" style="flex:1"></span>
        <button class="small" data-role="merge" disabled>Merge ticked tracks</button>
        <button class="small good" data-role="done">${a.reviewed ? "Reviewed ✓" : "Mark album reviewed"}</button></div>
      <table class="ed-table tl-table"><thead><tr><th></th><th>#</th><th>Track (this pressing)</th><th>Your song rows</th><th class="num">Plays</th></tr></thead><tbody>
      ${data.lines.map((ln, i) => `<tr class="${ln.rows.length ? "" : "no-song"}">
        <td>${ln.rows.length > 1 ? `<input type="checkbox" data-line="${i}" ${ln.clean ? "checked" : ""} />` : ""}</td>
        <td class="mono meta">${esc(ln.position || "")}</td>
        <td>${esc(ln.title)}${ln.how === "close title" ? ` <span class="badge" title="Matched despite a different spelling">close title</span>` : ln.how === "recording id" ? ` <span class="badge ok" title="Same MusicBrainz recording">recording id</span>` : ""}</td>
        <td>${ln.rows.length ? `<div class="sg-rows">${ln.rows.map(sgRow).join("")}</div>` : `<span class="meta">no song — never played or seen live</span>`}</td>
        <td class="num">${ln.plays || ""}${ln.shows ? ` <b class="livedot" title="${plural(ln.shows, "show")}">●</b>` : ""}</td></tr>`).join("")}
      </tbody></table>
      ${data.wrongAlbum.length ? `<div class="tl-sec"><h3>Filed here by mistake?</h3>${data.wrongAlbum.map((w) => wrongAlbumHtml(w)).join("")}</div>` : ""}
      ${data.extra.length ? `<div class="tl-sec"><h3>Filed under this album, not on this pressing <span class="meta">${data.extra.length}</span></h3>
        <p class="meta">Usually bonus tracks from a deluxe or digital edition — fine where they are. A song another of your records lists is marked, in case it's really from there.</p>
        ${data.extra.map((e) => `<div class="tl-extra">${sgRow(e)}${e.homes.length ? ` <span class="badge accent">on your ${e.homes.map((h) => `<a href="/albums.html#discography/${a.artistId}">${esc(h.title)}</a>`).join(", ")}</span>` : ""}</div>`).join("")}</div>` : ""}`;
    const boxes = () => [...view.querySelectorAll("input[data-line]")];
    const sync = () => { const n = boxes().filter((b) => b.checked).length; view.querySelector("[data-role='merge']").disabled = !n; view.querySelector("[data-role='merge']").textContent = n ? `Merge ${n} ticked track${n === 1 ? "" : "s"}` : "Merge ticked tracks"; };
    view.addEventListener("change", sync);
    sync();
    view.querySelector("[data-role='merge']").addEventListener("click", async () => {
      const items = boxes().filter((b) => b.checked).flatMap((b) => {
        const ln = data.lines[+b.dataset.line];
        return ln.rows.filter((r) => r.songId !== ln.primary).map((r) => ({ type: "merge", absorbedId: r.songId, canonicalId: ln.primary }));
      });
      const { ok: ok2, data: d2 } = await api("/api/songs/apply", { items });
      if (!ok2) { toast(esc(d2.message), { error: true, timeout: 9000 }); return; }
      toast(`${esc(a.title)}: ${plural(d2.applied, "song row")} merged`, { undo: { kind: "batch", id: d2.undo } });
      notifyChanged(); open(albumId); loadQueue();
    });
    view.querySelector("[data-role='done']").addEventListener("click", async () => {
      const { ok: ok2, data: d2 } = await api("/api/songs/tracklist-reviewed", { albumId });
      if (!ok2) { toast(esc(d2.message), { error: true }); return; }
      toast(`${esc(a.title)} marked reviewed`, d2.undo ? { undo: d2.undo } : {});
      loadQueue();
      const next = queue.find((x) => x.albumId !== albumId && !x.reviewed && (x.toMerge || x.extra));
      if (next) open(next.albumId);
    });
    wireWrongAlbum(view, () => { open(albumId); loadQueue(); });
  }
  return { load: loadQueue, open };
}

// -- Wrong album -------------------------------------------------------------------------------
function wrongAlbumHtml(w) {
  const evidence = w.split
    ? (w.numbered ? `The album it's filed under is numbered differently — “${esc(w.rawTitle)}” isn't “${esc(w.fromTitle)}”.`
      : `“${esc(w.rawTitle)}” doesn't look like “${esc(w.fromTitle)}”, and there's no album of that name in your library.`)
    : `${w.onTarget} of ${plural(w.songs, "song")} ${w.onTarget === 1 ? "is" : "are"} on your “${esc(w.toTitle)}”${w.hereHasTracklist ? `, ${w.onHere} on this pressing's tracklist` : ""}.`;
  const action = w.split ? `Split into its own album “${esc(w.toTitle)}”` : `Move ${plural(w.plays, "play")} to “${esc(w.toTitle)}”`;
  return `<div class="review-item wa-item" data-wa='${esc(JSON.stringify({ from: w.fromAlbumId, to: w.toAlbumId, raw: w.rawTitle, split: w.split, title: w.toTitle }))}'>
    <div class="row"><div>
      ${w.strong ? `<span class="badge high">strong</span> ` : w.numbered ? `<span class="badge medium">numbered differently</span> ` : ""}
      <b>${plural(w.plays, "play")}</b> that arrived as <b>“${esc(w.rawTitle)}”</b> are filed under <b>${esc(w.fromTitle)}</b>
      <div class="meta">${evidence}${w.mergedIn ? ` They came in with an album merge on ${esc(w.mergedIn.at.slice(0, 10))}.` : ""}</div>
      ${w.sample?.length ? `<div class="meta">e.g. ${w.sample.map(esc).join(" · ")}</div>` : ""}
    </div>
    <div class="actions">${keyBtn("m", action, "good", 'data-role="wa-go"')}${keyBtn("x", "It's fine here", "", 'data-role="wa-ok"')}${keyBtn("s", "Skip", "", 'data-role="wa-skip"')}</div></div></div>`;
}
function wireWrongAlbum(host, onDone, keys) {
  host.querySelectorAll(".wa-item").forEach((item) => {
    const w = JSON.parse(item.dataset.wa);
    item.querySelector("[data-role='wa-go']").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      const res = w.split ? await api("/api/songs/split-plays", { albumId: w.from, rawTitle: w.raw, title: w.title })
        : await api("/api/songs/move-plays", { fromAlbumId: w.from, toAlbumId: w.to, rawTitle: w.raw });
      if (!res.ok) { toast(esc(res.data.message), { error: true, timeout: 9000 }); e.currentTarget.disabled = false; return; }
      toast(w.split ? `Split out as “${esc(res.data.title)}” — ${plural(res.data.scrobbles, "play")}` : `${plural(res.data.scrobbles, "play")} moved to “${esc(w.title)}”${res.data.aliases ? " · future imports follow" : ""}`,
        { undo: res.data.undo });
      notifyChanged();
      keys ? keys.advance(item) : onDone();
    });
    item.querySelector("[data-role='wa-ok']").addEventListener("click", async () => {
      const res = await api("/api/songs/wrong-album-ok", { albumId: w.from, rawTitle: w.raw });
      if (!res.ok) { toast(esc(res.data.message), { error: true }); return; }
      toast("Remembered — won't be suggested again", res.data.undo ? { undo: res.data.undo, timeout: 5000 } : {});
      keys ? keys.advance(item) : onDone();
    });
    item.querySelector("[data-role='wa-skip']").addEventListener("click", () => (keys ? keys.skip(item) : null));
  });
}
function mountWrongAlbums(root) {
  let keys = null;
  async function load() {
    root.innerHTML = `<div class="empty">Loading…</div>`;
    const { ok, data } = await api("/api/songs/wrong-albums");
    if (!ok) { root.innerHTML = `<div class="empty">${esc(data.message)}</div>`; return; }
    const strong = data.groups.filter((g) => g.strong || g.numbered || !g.split);
    const look = data.groups.filter((g) => !(g.strong || g.numbered || !g.split));
    root.innerHTML = `<p class="meta" style="margin:0 0 10px">Plays that arrived under another album's name but are filed under this one — usually an old album merge into the wrong album. Moving them also re-points the import alias, so future plays land right. Nothing moves until you say; everything can be undone.</p>
      <div class="kbd-help"><span><kbd>j</kbd>/<kbd>k</kbd> move</span><span><kbd>m</kbd> move / split</span><span><kbd>x</kbd> it's fine here</span><span><kbd>s</kbd> skip</span></div>
      <div data-role="list">
      ${strong.length ? `<h3 class="wa-h">Likely misfiled <span class="meta">${strong.length}</span></h3>${strong.map(wrongAlbumHtml).join("")}` : ""}
      ${look.length ? `<h3 class="wa-h">Worth a look <span class="meta">${look.length} — often just a deluxe or anniversary edition's name, fine where it is</span></h3>${look.map(wrongAlbumHtml).join("")}` : ""}
      ${data.groups.length ? "" : `<div class="empty">Nothing looks misfiled.</div>`}</div>`;
    keys = mountReviewKeys(root.querySelector("[data-role='list']"));
    wireWrongAlbum(root, load, keys);
  }
  document.addEventListener("mc:undone", () => { if (root.offsetParent !== null) load(); });
  return { load };
}
