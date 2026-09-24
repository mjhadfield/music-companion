// Songs > Recording ids: song mbids Last.fm gave us that are really *track* ids (or recordings
// MusicBrainz has since merged) -> the recording, in reviewed batches. See api/recordings.py.

function mountRecordings(root) {
  root.innerHTML = `
    <div class="card">
      <div class="toolbar">
        <label>Look up the next <input type="number" data-role="limit" value="200" min="1" max="1000" style="width:80px" /> on MusicBrainz (most-played songs first, about a second each)</label>
        <button class="primary" data-role="start">Look up</button>
        <span class="spacer"></span>
        <span class="meta" data-role="counts"></span>
      </div>
      <div data-role="sweep"></div>
      <p class="meta" style="margin:10px 0 0">Last.fm's song ids are sometimes a MusicBrainz <b>recording</b> (the song itself) and sometimes a <b>track</b> — that song's slot on one particular release, which links nowhere and never matches anything. Each lookup fetches one edition's tracklist, which sorts every song played from it; songs no edition can sort are then asked about directly. Converting gives each song its recording id and keeps the track id listed under it.</p>
    </div>
    <div data-role="convert"></div>
    <div data-role="bytitle"></div>
    <div data-role="dupes"></div>
    <div data-role="invalid"></div>`;
  const $ = (r) => root.querySelector(`[data-role="${r}"]`);

  $("start").addEventListener("click", async (e) => {
    e.target.disabled = true;
    await runSweep($("sweep"), "song-recordings", parseInt($("limit").value, 10) || 200);
    e.target.disabled = false;
    load();
  });

  const songCell = (s) => `<b>${esc(s.title)}</b>
    <div class="meta"><a href="/songs.html#dupes/${s.artistId}">${esc(s.artistName)}</a> · ${plural(s.plays, "play")}</div>`;
  const was = (s) => s.kind === "track"
    ? `track <span class="mono">${esc(s.mbid.slice(0, 8))}</span>${s.release ? ` on ${mbLink("release", s.release, "this edition")}` : ""}`
    : s.kind === "title" ? `<span class="mono">${esc(s.mbid.slice(0, 8))}</span> <span class="meta">(not on that edition — matched by title)</span>`
    : `${mbLink("recording", s.mbid)} <span class="meta">(merged on MusicBrainz since)</span>`;

  const convertAction = async (act, ids) => {
    if (act === "keep") return keep(ids);
    const { ok, data } = await api("/api/songs/recording-ids/convert", { songIds: ids });
    if (!ok) { toast(esc(data.message), { error: true }); return; }
    toast(`${plural(data.converted, "song")} converted${data.skipped.length ? ` · ${data.skipped.length} skipped (${esc(data.skipped[0].reason)})` : ""}`,
      data.undo ? { undo: data.undo } : {});
    notifyChanged(); load();
  };

  function renderByTitle(list, total) {
    if (!list.length) { $("bytitle").innerHTML = ""; return; }
    mountTickList($("bytitle"), list.map((s) => ({
      id: s.songId, ticked: true,
      cells: [songCell(s), `<span class="mono">${esc(s.mbid.slice(0, 8))}</span> <span class="meta">(not on the edition it was played from)</span>`, "→",
        `${mbLink("recording", s.recording, esc(s.matchedTitle))}<div class="meta">the only track with this title on ${mbLink("release", s.release, "the edition you played")}</div>`],
    })), {
      head: `Matched by title on the edition you played <span class="meta">${fmtNum(total)}${total > list.length ? ` · showing ${fmtNum(list.length)}` : ""}</span>`,
      cols: ["Song", "Stored id", "", "Recording on the edition you played"],
      buttons: [["convert", "Convert ticked", "good"], ["keep", "Leave ticked as they are"]],
      onAction: convertAction,
    });
  }

  function renderConvert(list, total) {
    if (!list.length) { $("convert").innerHTML = ""; return; }
    mountTickList($("convert"), list.map((s) => ({
      id: s.songId, ticked: true,
      cells: [songCell(s), was(s), "→", mbLink("recording", s.recording)],
    })), {
      head: `Ready to convert <span class="meta">${fmtNum(total)}${total > list.length ? ` · showing ${fmtNum(list.length)}` : ""}</span>`,
      cols: ["Song", "Stored id is a", "", "Recording (the song)"],
      buttons: [["convert", "Convert ticked", "good"], ["keep", "Leave ticked as they are"]],
      onAction: convertAction,
    });
  }

  function renderInvalid(list, total) {
    if (!list.length) { $("invalid").innerHTML = ""; return; }
    mountTickList($("invalid"), list.map((s) => ({ id: s.songId, ticked: false, cells: [songCell(s), `<span class="mono">${esc(s.mbid)}</span>`] })), {
      head: `Not on MusicBrainz <span class="meta">${fmtNum(total)}</span>`,
      cols: ["Song", "Stored id (MusicBrainz knows no recording or track with it)"],
      buttons: [["clear", "Clear the id of ticked"], ["keep", "Leave ticked as they are"]],
      onAction: async (act, ids) => {
        if (act === "keep") return keep(ids);
        const { ok, data } = await api("/api/songs/recording-ids/clear", { songIds: ids });
        if (!ok) { toast(esc(data.message), { error: true }); return; }
        toast(`Id cleared on ${plural(data.cleared, "song")}`, data.undo ? { undo: data.undo } : {});
        notifyChanged(); load();
      },
    });
  }

  async function keep(ids) {
    const { ok, data } = await api("/api/songs/recording-ids/keep", { songIds: ids });
    if (!ok) { toast(esc(data.message), { error: true }); return; }
    toast(`${plural(data.kept, "song")} left as they are`, { undo: data.undo });
    load();
  }

  function renderDupes(groups, total) {
    const el = $("dupes");
    if (!groups.length) { el.innerHTML = ""; return; }
    el.innerHTML = `<h3 style="margin:22px 0 4px">One recording, several songs <span class="meta">${fmtNum(total)}</span></h3>
      <p class="meta" style="margin:0 0 10px">MusicBrainz says these are the same recording. Pick the one to keep — it takes the recording id, and every play, live sighting and track id moves onto it.</p>
      <div class="kbd-help"><span><kbd>j</kbd>/<kbd>k</kbd> move</span><span><kbd>m</kbd> merge</span><span><kbd>x</kbd> keep separate</span><span><kbd>s</kbd> skip</span></div>
      <div data-role="dupe-list">${groups.map((g, gi) => {
        const keepOne = g.members.find((m) => m.role === "holder") || [...g.members].sort((a, b) => b.plays - a.plays)[0];
        return `<div class="review-item" data-g="${gi}">
          <div class="row"><div><span class="meta">MusicBrainz:</span> ${mbLink("recording", g.recording, "recording")}</div>
            <div class="actions">${g.sameArtist ? keyBtn("m", "Merge", "good", 'data-role="merge"') : ""}
              ${keyBtn("x", "Keep separate", "", 'data-role="keep"')}${keyBtn("s", "Skip", "", 'data-role="skip"')}</div></div>
          ${g.sameArtist ? "" : `<div class="ev-why">These songs belong to different artists — merge the artists first (Artists › Duplicates), or keep them separate.</div>`}
          ${g.members.map((m) => `<label class="candidate" style="cursor:pointer">
            <span><input type="radio" name="rkeep-${gi}" value="${m.songId}" ${m === keepOne ? "checked" : ""} ${g.sameArtist ? "" : "disabled"} /> ${songCell(m)}</span>
            <span class="meta">${m.role === "holder" ? "already has the recording id" : was(m)}</span></label>`).join("")}
        </div>`;
      }).join("")}</div>`;
    const listEl = el.querySelector("[data-role='dupe-list']");
    const keys = mountReviewKeys(listEl);
    listEl.querySelectorAll(".review-item").forEach((item) => {
      const g = groups[+item.dataset.g];
      item.querySelector("[data-role='skip']").addEventListener("click", () => keys.skip(item));
      item.querySelector("[data-role='keep']").addEventListener("click", async (e) => {
        e.target.disabled = true;
        const { ok, data } = await api("/api/songs/recording-ids/keep", { songIds: g.members.filter((m) => m.role !== "holder").map((m) => m.songId) });
        if (!ok) { toast(esc(data.message), { error: true }); e.target.disabled = false; return; }
        toast("Kept separate", { undo: data.undo, timeout: 6000 });
        keys.advance(item);
      });
      item.querySelector("[data-role='merge']")?.addEventListener("click", async (e) => {
        const canonicalId = +item.querySelector("input[type=radio]:checked").value;
        e.target.disabled = true;
        const { ok, data } = await api("/api/songs/recording-ids/merge",
          { canonicalId, absorbedIds: g.members.map((m) => m.songId).filter((id) => id !== canonicalId), recording: g.recording });
        if (!ok) { toast(esc(data.message), { error: true, timeout: 9000 }); e.target.disabled = false; return; }
        toast(`Merged into <b>${esc(data.canonicalTitle)}</b>`, { undo: data.undo });
        notifyChanged();
        keys.advance(item);
      });
    });
  }

  async function load() {
    $("counts").textContent = "Loading…";
    const { ok, data } = await api("/api/songs/recording-ids");
    if (!ok) { $("counts").textContent = data.message; return; }
    const c = data.counts;
    $("counts").textContent = `${fmtNum(c.songs)} songs with an id · ${fmtNum(c.confirmed)} confirmed recordings · ${fmtNum(c.pending)} not sorted yet`
      + (c.pending ? ` (${fmtNum(c.editionsToFetch)} editions + ${fmtNum(c.directToAsk)} direct lookups to go)` : "");
    $("start").disabled = !c.pending;
    renderConvert(data.convert, c.convert);
    renderByTitle(data.byTitle, c.byTitle);
    renderDupes(data.duplicates, c.duplicates);
    renderInvalid(data.invalid, c.invalid);
    if (c.pending && !c.convert && !c.byTitle && !c.duplicates && !c.invalid) {
      $("convert").innerHTML = `<div class="empty" style="margin-top:18px">Nothing looked up yet — start with a batch above; the results appear here to review.</div>`;
    }
  }
  document.addEventListener("mc:undone", () => { if (root.offsetParent !== null) load(); });
  return { load };
}
