/*
 * Song duplicate groups as review items -- shared by the artist workbench (artist.html) and the
 * whole-library queue (Songs > Duplicates). A group never changes anything by itself: Queue merge
 * and Not the same / Keep separate add an action to the page's basket (shared.js createBasket),
 * which dry-runs the lot before anything is applied as one undoable batch. Needs shared.js and
 * album-ui.js first.
 */

// Keep-radio and merge-tick stay consistent: the kept row can't also be merged away.
function wireKeep(el) {
  el.querySelectorAll("input[type=radio]").forEach((r) => r.addEventListener("change", () => {
    el.querySelectorAll("tbody tr").forEach((tr) => {
      const keep = tr.dataset.id === r.value;
      tr.classList.toggle("primary", keep);
      const cb = tr.querySelector("input[type=checkbox]");
      if (keep) cb.checked = false;
      cb.disabled = keep;
    });
  }));
  el.querySelector("input[type=radio]:checked")?.dispatchEvent(new Event("change"));
}

const playsCell = (n, shows) => `${fmtNum(n)}${shows ? ` <span class="meta">+${fmtNum(shows)} live</span>` : ""}`;
const KIND_TEXT = { edition: ["Same recording", "ok"], variant: ["Different versions", ""], possible: ["Possible duplicate", "accent"] };
const REASON_TEXT = { spacing: "spaced or punctuated differently", article: "differs only by “the” / “a”", suffix: "one adds a number or year", spelling: "a near spelling" };

// Two rows with one title are told apart by their album in the basket's list.
const songName = (m, kept) => `“${esc(m.title)}”${m.title.toLowerCase() === kept.title.toLowerCase() ? ` <span class="meta">(${esc(m.albumTitle || "no album")})</span>` : ""}`;

function songGroupItem(g, { basket, keys, showArtist = false }) {
  const sameTitle = (ids, kept) => ids.some((i) => g.members.find((m) => m.songId === i).title.toLowerCase() === kept.title.toLowerCase());
  const el = document.createElement("div");
  el.className = "review-item wb-item";
  el.dataset.kind = g.kind;
  const primary = g.members.find((m) => m.songId === g.primary);
  const [kindLabel, kindCls] = KIND_TEXT[g.kind];
  const tagged = [...new Set(g.members.flatMap((m) => m.tags))];
  const notSame = g.kind === "variant" ? "Keep separate" : "Not the same";
  const note = g.kind === "variant" ? "A live / remix / edit / demo version next to the song. Versions are kept as their own songs unless you merge one here (it can be split back out later)."
    : g.kind === "possible" ? `Titles that look like one song — ${esc(REASON_TEXT[g.reason] || g.reason)}. Check before merging.${tagged.length ? ` <span class="warn-text">One is a ${esc(tagged.join("/"))} version — versions stay separate unless you merge them.</span>` : ""}`
    : g.include.length ? "The same recording spelled differently, on the same album — ticked for you." : "The same recording spelled differently.";
  el.innerHTML = `<div class="row"><div><div class="title">${showArtist ? `<a href="/artist.html?id=${g.artistId}" class="artist-link">${esc(g.artistName)}</a> — ` : ""}“${esc(primary.title)}” <span class="badge ${kindCls}">${kindLabel}</span>${g.split ? ' <span class="badge warn" title="Its live shows and its plays are on different rows">live split</span>' : ""}</div>
      <div class="meta">${note}</div></div>
      <div class="actions">${keyBtn("m", "Queue merge", "good")}${keyBtn("d", notSame)}${keyBtn("s", "Skip")}</div></div>
    <div class="table-scroll"><table class="data"><thead><tr><th>Keep</th><th>Merge</th><th>Title</th><th class="num">Plays</th><th>Album / played from</th><th>Played</th></tr></thead><tbody>
    ${g.members.map((m) => `<tr data-id="${m.songId}"><td><input type="radio" name="p-${esc(g.key)}" value="${m.songId}" ${m.songId === g.primary ? "checked" : ""}></td>
      <td><input type="checkbox" value="${m.songId}" ${g.include.includes(m.songId) ? "checked" : ""}></td>
      <td>${esc(m.title)} ${m.tags.map((t) => `<span class="badge">${esc(t)}</span>`).join("")} ${m.mbid ? mbLink("song", m.mbid) : ""}
        ${m.spellings.filter((x) => x !== m.title).length ? `<div class="spell">arrived as ${m.spellings.filter((x) => x !== m.title).map((x) => `“${esc(x)}”`).join(", ")}</div>` : ""}</td>
      <td class="num">${playsCell(m.plays, m.shows)}</td>
      <td>${esc(m.albumTitle || "—")}${m.playedFrom.filter((t) => t !== m.albumTitle).length ? `<div class="spell">also from ${m.playedFrom.filter((t) => t !== m.albumTitle).map(esc).join(", ")}</div>` : ""}</td>
      <td class="meta">${m.firstPlayed ? `${m.firstPlayed.slice(0, 4)}–${m.lastPlayed.slice(0, 4)}` : "—"}</td></tr>`).join("")}</tbody></table></div>
    <div class="namebox">Call the kept song <input type="text" data-role="name" value="${esc(primary.title)}"> <span class="meta">old spellings keep routing to it</span></div>`;
  wireKeep(el);
  el.querySelectorAll("input[type=radio]").forEach((r) => r.addEventListener("change", () => {
    el.querySelector("[data-role='name']").value = g.members.find((m) => String(m.songId) === r.value).title;
  }));
  el.querySelector("[data-key='m']").addEventListener("click", () => {
    const keep = parseInt(el.querySelector("input[type=radio]:checked").value, 10);
    const absorbed = [...el.querySelectorAll("input[type=checkbox]:checked")].map((c) => parseInt(c.value, 10)).filter((i) => i !== keep);
    if (!absorbed.length) { toast("Tick at least one song to merge into the one you keep.", { error: true }); return; }
    const kept = g.members.find((m) => m.songId === keep);
    const name = el.querySelector("[data-role='name']").value.trim();
    basket.add(g.key, { type: "mergeSongs", canonicalId: keep, absorbedIds: absorbed, ...(name && name !== kept.title ? { title: name } : {}) },
      `Merge ${absorbed.map((i) => songName(g.members.find((m) => m.songId === i), kept)).join(", ")} into “<b>${esc(name || kept.title)}</b>”${sameTitle(absorbed, kept) ? ` <span class="meta">(${esc(kept.albumTitle || "no album")})</span>` : ""}`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='d']").addEventListener("click", () => {
    basket.add(g.key, { type: "dismiss", entityType: "song", ids: g.members.map((m) => m.songId) }, `${g.kind === "variant" ? "Keep as separate versions" : "Not the same song"}: ${g.members.map((m) => `“${esc(m.title)}”`).join(" / ")}`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='s']").addEventListener("click", () => keys.skip(el));
  return el;
}

// -- An album with no MusicBrainz id, and the album-suggest sweep's candidates for it -------------
// One id per album, so a radio; only a high-tier top suggestion arrives selected. An id another
// album already holds can't be assigned (the database refuses two albums one identity) -- that's
// a duplicate album, so it offers Compare instead.
function evidenceText(ev) {
  const bits = [];
  if (ev.titleMatch) bits.push(ev.titleMatch === "exact" ? "same title" : `title match ${ev.titleScore}`);
  if (ev.trackOverlap != null) bits.push(`${ev.trackOverlap}% of the songs you've played from it are on it (${ev.tracksMatched}/${ev.tracksPlayed})`);
  else if (ev.titleMatch) bits.push("no tracklist checked");
  if (ev.types) bits.push(ev.types);
  if (ev.firstReleaseDate) bits.push(`first released ${ev.firstReleaseDate}`);
  if (ev.disambiguation) bits.push(ev.disambiguation);
  return bits.join(" · ");
}

function albumSuggestItem(m, { basket, keys, profile, showArtist = false, onResolved }) {
  const el = document.createElement("div");
  el.className = "review-item wb-item";
  const name = `as-${m.albumId}`;
  const pick = m.suggestions[0] && m.suggestions[0].tier === "high" && !m.suggestions[0].evidence?.alreadyLinkedTo ? m.suggestions[0].suggestionId : null;
  el.innerHTML = `<div class="row"><div><div class="title">${showArtist ? `<a href="/artist.html?id=${m.artistId}">${esc(m.artistName)}</a> — ` : ""}${esc(m.title)}</div>
      <div class="meta">${plural(m.plays, "play")}${m.vinyl ? ` · ${plural(m.vinyl, "record")} owned` : ""} · no MusicBrainz id</div></div>
      <div class="actions">${m.suggestions.length ? keyBtn("a", "Queue selected", "good") + keyBtn("r", "None of these") : ""}${keyBtn("f", "Find…")}${keyBtn("x", "Not on MusicBrainz")}${keyBtn("s", "Skip")}</div></div>
    ${m.suggestions.map((s) => {
      const taken = s.evidence?.alreadyLinkedTo;
      const conflict = s.evidence?.typeConflict;
      return `<label class="sugg" data-sid="${s.suggestionId}">
        <input type="radio" name="${name}" value="${s.suggestionId}" ${s.suggestionId === pick ? "checked" : ""} ${taken ? "disabled" : ""}>
        <div><b>${esc(s.label || s.mbid)}</b> ${mbLink("album", s.mbid)} <span class="badge ${s.tier === "high" ? "ok" : s.tier === "medium" ? "warn" : ""}">${esc(s.tier)}</span>
          ${conflict ? `<span class="warn-text">${esc(conflict)}</span>` : ""}
          <div class="ev">${esc(evidenceText(s.evidence || {}))}</div>
          ${taken ? `<div class="warn-text">Already the id of your “${esc(taken.title)}” — that looks like the same album. <button type="button" class="small" data-role="compare" data-other="${taken.albumId}">Compare…</button></div>`
            : s.label && s.label !== m.title ? `<span class="meta"><input type="checkbox" data-role="tidy"> and rename it “${esc(s.label)}”</span>` : ""}
        </div></label>`;
    }).join("")}
    <div class="slot"></div>`;
  const slot = el.querySelector(".slot");
  const done = () => { el.classList.add("done"); keys.advance(el); onResolved?.(); };
  const dropMine = () => { basket.remove(`mb:${m.albumId}`); m.suggestions.forEach((s) => basket.remove(`rej:${s.suggestionId}`)); };
  el.querySelector("[data-key='a']")?.addEventListener("click", () => {
    const r = el.querySelector(`input[name="${name}"]:checked`);
    if (!r) { toast("Pick one of the suggestions first (or Find… / None of these).", { error: true }); return; }
    const s = m.suggestions.find((x) => String(x.suggestionId) === r.value);
    const tidy = r.closest(".sugg").querySelector("[data-role='tidy']")?.checked;
    dropMine();
    basket.add(`mb:${m.albumId}`, { type: "assignAlbumMbid", albumId: m.albumId, mbid: s.mbid, suggestionId: s.suggestionId, ...(tidy ? { title: s.label } : {}) },
      `Identify “${esc(m.title)}” as MusicBrainz “<b>${esc(s.label || s.mbid)}</b>”${tidy ? " and rename it to match" : ""}`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='r']")?.addEventListener("click", () => {
    dropMine();
    m.suggestions.forEach((s, i) => basket.add(`rej:${s.suggestionId}`, { type: "rejectSuggestion", suggestionId: s.suggestionId },
      `Not “${esc(s.label || s.mbid)}” for “${esc(m.title)}”`, i === 0 ? el : null));
    keys.advance(el);
  });
  el.querySelector("[data-key='x']").addEventListener("click", () => {
    dropMine();
    basket.add(`mb:${m.albumId}`, { type: "mark", entityType: "album", entityId: m.albumId, mark: "no-mbid" },
      `“${esc(m.title)}” isn't on MusicBrainz — stop suggesting ids for it`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='f']").addEventListener("click", () => {
    renderAlbumResolve(slot, profile || { albumId: m.albumId, title: m.title, baseTitle: m.baseTitle || m.title, artistName: m.artistName, artistMbid: m.artistMbid, mbid: null },
      { reason: "assigned", onDone: done });
  });
  el.querySelectorAll("[data-role='compare']").forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    renderAlbumCompare(slot, m.albumId, parseInt(b.dataset.other, 10), { onMerged: done, keys: false });
  }));
  el.querySelector("[data-key='s']").addEventListener("click", () => keys.skip(el));
  return el;
}

// -- A single filed as an album (singles.py) ----------------------------------------------------
// Folding it: its own copies of songs merge into their twins on the album, then the single merges
// into the album -- plays move, its title and editions route there from now on. Not a single =
// a remembered album dismissal for that pair.
function singleItem(x, { basket, keys, showArtist = false }) {
  const el = document.createElement("div");
  el.className = "review-item wb-item";
  const to = x.target;
  el.innerHTML = `<div class="row"><div>
      <div class="title">${showArtist ? `<a href="/artist.html?id=${x.artistId}">${esc(x.artistName)}</a> — ` : ""}“${esc(x.title)}” is a single from <b>${esc(to.title)}</b>
        ${x.mbid ? mbLink("album", x.mbid) : ""} ${x.mbType ? `<span class="badge">MusicBrainz: ${esc(x.mbType)}</span>` : ""}</div>
      <div class="meta">${plural(x.plays, "play")} filed on the single${x.songsToMerge ? ` · ${plural(x.songsToMerge, "song")} of its own to merge` : ""} · ${esc(to.title)}${to.year ? ` (${to.year})` : ""} has ${plural(to.songs, "song")} ${to.mbid ? mbLink("album", to.mbid) : ""}</div>
      ${x.reasons.length ? `<div class="warn-text">Check: ${x.reasons.map(esc).join("; ")}</div>` : ""}</div>
      <div class="actions">${keyBtn("m", "Fold into album", "good")}${keyBtn("d", "Not a single")}${keyBtn("s", "Skip")}</div></div>
    <div class="table-scroll"><table class="data"><thead><tr><th>Track on the single</th><th class="num">Plays from it</th><th>Folding it</th></tr></thead><tbody>
    ${x.tracks.map((t) => `<tr><td>${esc(t.title)}</td><td class="num">${fmtNum(t.plays)}</td>
      <td class="meta">${t.filedHere ? `its own copy merges into “${esc(t.twin.title)}” on ${esc(to.title)}` : `already on ${esc(to.title)} — the plays move there`}</td></tr>`).join("")}</tbody></table></div>`;
  el.querySelector("[data-key='m']").addEventListener("click", () => {
    basket.add(x.key, { type: "foldSingle", singleId: x.singleId, albumId: to.albumId },
      `Fold the single “${esc(x.title)}” into “<b>${esc(to.title)}</b>”`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='d']").addEventListener("click", () => {
    basket.add(x.key, { type: "dismiss", entityType: "album", ids: [x.singleId, to.albumId] }, `“${esc(x.title)}” isn't a single from “${esc(to.title)}”`, el);
    keys.advance(el);
  });
  el.querySelector("[data-key='s']").addEventListener("click", () => keys.skip(el));
  return el;
}

// -- Linked artists (merge.link_artists) -------------------------------------------------------------
// A solo act and its band (Ace Frehley / Kiss), an offshoot (KK's Priest / Judas Priest), a singer
// and the band he fronts (Myles Kennedy / Slash): Live sets then offers the linked artist's
// recordings for this performer's live songs. Each change is applied at once as a one-step batch
// (undo from the toast or Activity). links: [{artistId, name}]; onChange() after a change or undo.
function mountArtistLinks(el, artistId, links, onChange) {
  el.innerHTML = `<span class="meta">Linked artists:</span>
    ${links.map((l) => `<span class="chip link-chip"><a href="/artist.html?id=${l.artistId}">${esc(l.name)}</a>
      <button class="linkish" data-unlink="${l.artistId}" title="Unlink ${esc(l.name)}" aria-label="Unlink ${esc(l.name)}">✕</button></span>`).join("") || `<span class="meta">none</span>`}
    <input type="search" class="link-q" placeholder="+ Link an artist…" />`;
  const apply = async (action, text) => {
    const res = await api("/api/batch/apply", { actions: [action], label: "Linked artists", artistId });
    if (!res.ok) { toast(esc(res.data.message), { error: true, timeout: 9000 }); return; }
    toast(text, { undo: { kind: "batchv2", id: res.data.batchId, onUndone: onChange } });
    onChange?.();
  };
  el.querySelectorAll("[data-unlink]").forEach((b) => b.addEventListener("click", () => {
    const other = parseInt(b.dataset.unlink, 10);
    apply({ type: "unlinkArtists", artistId, linkedArtistId: other }, `Unlinked <b>${esc(links.find((l) => l.artistId === other)?.name || "")}</b>`);
  }));
  mountTypeahead(el.querySelector(".link-q"), {
    minChars: 2,
    fetchItems: async (q) => ((await api(`/api/search?q=${encodeURIComponent(q)}`)).data?.artists || [])
      .filter((a) => a.artistId !== artistId && !links.some((l) => l.artistId === a.artistId)),
    renderItem: (a) => `<b>${esc(a.name)}</b>`,
    onPick: (a) => apply({ type: "linkArtists", artistId, linkedArtistId: a.artistId }, `Linked <b>${esc(a.name)}</b> — Live sets can now use their recordings`),
  });
}
