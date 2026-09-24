// Albums > Editions: albums still identified by one edition (a Last.fm release id) -> their
// release group, in reviewed batches. See api/editions.py.

function mountEditions(root) {
  root.innerHTML = `
    <div class="card">
      <div class="toolbar">
        <label>Look up the next <input type="number" data-role="limit" value="200" min="1" max="1000" style="width:80px" /> albums on MusicBrainz (most played first, about a second each)</label>
        <button class="primary" data-role="start">Look up</button>
        <span class="spacer"></span>
        <span class="meta" data-role="counts"></span>
      </div>
      <div data-role="sweep"></div>
      <p class="meta" style="margin:10px 0 0">These albums were identified by one specific edition (a remaster, a country's pressing) rather than the album itself — the reason one album could turn into several. Converting sets each to its MusicBrainz <b>release group</b> and keeps the edition listed under it, so no detail is lost and future imports of any edition land on the one album.</p>
    </div>
    <div data-role="convert"></div>
    <div data-role="dupes"></div>
    <div data-role="invalid"></div>`;
  const $ = (r) => root.querySelector(`[data-role="${r}"]`);
  let keys = null;

  $("start").addEventListener("click", async (e) => {
    e.target.disabled = true;
    await runSweep($("sweep"), "album-editions", parseInt($("limit").value, 10) || 200);
    e.target.disabled = false;
    load();
  });

  const albumCell = (a) => `<b>${esc(a.title)}</b>${a.year ? ` <span class="meta">(${a.year})</span>` : ""}
    <div class="meta"><a href="/albums.html#discography/${a.artistId}">${esc(a.artistName)}</a> · ${plural(a.plays, "play")}${a.vinyl ? ` · <span class="badge vinyl">vinyl</span>` : ""}</div>`;
  const rgCell = (g) => `${mbLink("release-group", g.rg, g.rgTitle || "release group")}
    <div class="meta">${[g.rgType, ...(g.rgSecondary || [])].filter(Boolean).map(esc).join(" + ") || ""}${g.rgFirst ? ` · first released ${esc(g.rgFirst.slice(0, 4))}` : ""}${g.rgCredit ? ` · ${esc(g.rgCredit)}` : ""}</div>`;

  function renderConvert(list, total) {
    if (!list.length) { $("convert").innerHTML = ""; return; }
    mountTickList($("convert"), list.map((a) => ({
      id: a.albumId, ticked: a.clean,
      cells: [albumCell(a), mbLink("release", a.release, "edition"), "→", rgCell(a),
        a.flags.length ? `<span class="ev-why">${a.flags.map(esc).join("; ")}</span>` : `<span class="badge ok">checks out</span>`],
    })), {
      head: `Ready to convert <span class="meta">${fmtNum(total)}${total > list.length ? ` · showing ${fmtNum(list.length)}` : ""}</span>`,
      cols: ["Album", "Stored as", "", "Release group (the album)", "Check"],
      buttons: [["convert", "Convert ticked", "good"], ["keep", "Leave ticked as they are"]],
      onAction: async (act, ids) => {
        if (act === "keep") return keepAlbums(ids);
        const { ok, data } = await api("/api/albums/editions/convert", { albumIds: ids });
        if (!ok) { toast(esc(data.message), { error: true }); return; }
        toast(`${plural(data.converted, "album")} converted${data.skipped.length ? ` · ${data.skipped.length} skipped (${esc(data.skipped[0].reason)})` : ""}`,
          data.undo ? { undo: data.undo } : {});
        notifyChanged(); load();
      },
    });
  }

  function renderInvalid(list, total) {
    if (!list.length) { $("invalid").innerHTML = ""; return; }
    mountTickList($("invalid"), list.map((a) => ({
      id: a.albumId, ticked: false,
      cells: [albumCell(a), mbLink("release", a.mbid)],
    })), {
      head: `Not on MusicBrainz <span class="meta">${fmtNum(total)}</span>`,
      cols: ["Album", "Stored id (MusicBrainz doesn't know it)"],
      buttons: [["clear", "Clear the MBID of ticked"], ["keep", "Leave ticked as they are"]],
      onAction: async (act, ids) => {
        if (act === "keep") return keepAlbums(ids);
        const { ok, data } = await api("/api/albums/editions/clear", { albumIds: ids });
        if (!ok) { toast(esc(data.message), { error: true }); return; }
        toast(`MBID cleared on ${plural(data.cleared, "album")} — they're under Missing MBID now`, data.undo ? { undo: data.undo } : {});
        notifyChanged(); load();
      },
    });
  }

  async function keepAlbums(ids) {
    const { ok, data } = await api("/api/albums/editions/keep", { albumIds: ids });
    if (!ok) { toast(esc(data.message), { error: true }); return false; }
    toast(`${plural(data.kept, "album")} left as they are`, { undo: data.undo });
    load();
    return true;
  }

  function renderDupes(groups, total) {
    const el = $("dupes");
    if (!groups.length) { el.innerHTML = ""; return; }
    el.innerHTML = `<h3 style="margin:22px 0 4px">One album, several rows <span class="meta">${fmtNum(total)}</span></h3>
      <p class="meta" style="margin:0 0 10px">These albums are editions of the same MusicBrainz release group. Pick the one to keep — it takes the release group, and every edition, play and pressing moves onto it.</p>
      <div class="kbd-help"><span><kbd>j</kbd>/<kbd>k</kbd> move</span><span><kbd>m</kbd> merge</span><span><kbd>x</kbd> keep separate</span><span><kbd>s</kbd> skip</span></div>
      <div data-role="dupe-list">${groups.map((g, gi) => {
        const keep = g.members.find((m) => m.role === "holder") || [...g.members].sort((a, b) => (b.vinyl - a.vinyl) || (b.plays - a.plays))[0];
        return `<div class="review-item" data-g="${gi}">
          <div class="row"><div><span class="meta">MusicBrainz:</span> ${rgCell(g)}</div>
            <div class="actions">${g.sameArtist ? keyBtn("m", "Merge", "good", 'data-role="merge"') : ""}
              ${keyBtn("x", "Keep separate", "", 'data-role="keep"')}${keyBtn("s", "Skip", "", 'data-role="skip"')}</div></div>
          ${g.sameArtist ? "" : `<div class="ev-why">These rows belong to different artists — merge the artists first (Artists › Duplicates), or keep them separate.</div>`}
          ${g.members.map((m) => `<label class="candidate" style="cursor:pointer">
            <span><input type="radio" name="keep-${gi}" value="${m.albumId}" ${m === keep ? "checked" : ""} ${g.sameArtist ? "" : "disabled"} /> ${albumCell(m)}</span>
            <span class="meta">${m.role === "holder" ? "already has the release group" : `stored as ${mbLink("release", m.release, "an edition")}`}</span></label>`).join("")}
        </div>`;
      }).join("")}</div>`;
    const listEl = el.querySelector("[data-role='dupe-list']");
    keys = mountReviewKeys(listEl);
    listEl.querySelectorAll(".review-item").forEach((item) => {
      const g = groups[+item.dataset.g];
      item.querySelector("[data-role='skip']").addEventListener("click", () => keys.skip(item));
      item.querySelector("[data-role='keep']").addEventListener("click", async (e) => {
        e.target.disabled = true;
        const { ok, data } = await api("/api/albums/editions/keep", { albumIds: g.members.filter((m) => m.role === "edition").map((m) => m.albumId) });
        if (!ok) { toast(esc(data.message), { error: true }); e.target.disabled = false; return; }
        toast("Kept separate", { undo: data.undo, timeout: 6000 });
        keys.advance(item);
      });
      item.querySelector("[data-role='merge']")?.addEventListener("click", async (e) => {
        const canonicalId = +item.querySelector("input[type=radio]:checked").value;
        e.target.disabled = true;
        const { ok, data } = await api("/api/albums/editions/merge",
          { canonicalId, absorbedIds: g.members.map((m) => m.albumId).filter((id) => id !== canonicalId), rg: g.rg });
        if (!ok) { toast(esc(data.message), { error: true, timeout: 9000 }); e.target.disabled = false; return; }
        toast(`Merged into <b>${esc(data.canonicalTitle)}</b>`, { undo: data.undo });
        notifyChanged();
        keys.advance(item);
      });
    });
  }

  async function load() {
    $("counts").textContent = "Loading…";
    const { ok, data } = await api("/api/albums/editions");
    if (!ok) { $("counts").textContent = data.message; return; }
    const c = data.counts;
    $("counts").textContent = c.candidates
      ? `${fmtNum(c.candidates)} albums identified by an edition · ${fmtNum(c.toLookUp)} not looked up yet`
      : "Every album is identified by its release group.";
    $("start").disabled = !c.toLookUp;
    renderConvert(data.convert, c.convert);
    renderDupes(data.duplicates, c.duplicates);
    renderInvalid(data.invalid, c.invalid);
    if (c.candidates && !c.convert && !c.duplicates && !c.invalid) {
      $("convert").innerHTML = `<div class="empty" style="margin-top:18px">Nothing looked up yet — start with a batch above; the results appear here to review.</div>`;
    }
  }
  document.addEventListener("mc:undone", () => { if (root.offsetParent !== null) load(); });
  return { load };
}
