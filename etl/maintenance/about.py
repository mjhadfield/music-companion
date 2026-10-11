"""The artist page's About, fetched here once and stored (public table artist_about) instead of
looked up live in every visitor's browser.

Where the text comes from, in order:
  1. Wikipedia -- the page MusicBrainz points at. MusicBrainz now links artists to *Wikidata*
     (rarely Wikipedia directly), so: MusicBrainz url-rels -> Wikidata sitelink -> English
     Wikipedia -> the article's lead section, as plain text (Mike trims it in the Workbench).
  2. Last.fm's bio (artist.getInfo), when there's no Wikipedia page -- smaller bands. A shared-name
     bio ("There are multiple artists with this name...") is skipped: it isn't about this artist.
The old site's guess -- a Wikipedia search for "<name> band", top hit taken -- is only ever
offered here as candidates to confirm (search()), never applied by itself.

MusicBrainz also gives the facts line and links: tagline (its disambiguation, e.g. "Finnish
melodic death metal band"), type, area, formed, official site / Bandcamp / Discogs...

A person's decision always survives a re-fetch: once edited (text changed, or a Wikipedia page
picked by hand), a fetch only refreshes the facts and original_text, and sets changed_upstream
when Wikipedia's text has moved on. Edits are journaled ({"_about": {before, after}}) and undone
by merge.undo_edit.
"""
import html
import json
import os
import re
import threading
import time
from urllib.parse import quote, unquote

import requests

import mbcache
import musicbrainz as mb
from common import connect as db_connect

UA = {"User-Agent": "MusicCompanionMaintenance/1.0 (mikehadfield89@gmail.com)"}
WIKI_MIN_INTERVAL = 0.25          # Wikimedia asks for a reasonable rate -- ~4 a second
TIMEOUT = 15
FIELDS = ("text", "original_text", "source", "edited", "wiki_url", "wiki_title", "thumbnail_url", "tagline", "area",
          "begin_area", "formed", "ended", "type", "links_json", "changed_upstream", "fetched_at", "updated_at")
SHARED_NAME_BIO = re.compile(r"^\s*there(?:'s| (?:are|is|were|have been|a))? (at least |more than |over )?[\w-]+\b[^.:]{0,40}\b(artists?|bands?|acts?|groups?|musicians?|projects?|people)\b", re.I)
# what's left of a pronunciation template in an extract: "Placebo ( plə-SEE-boh) are..."
LEFTOVER_PARENS = re.compile(r"\s*\((?:\s|;|,)[^()]{0,80}\)")

_wiki_lock = threading.Lock()
_wiki_last = 0.0


def _wiki_get(url: str, params: dict) -> dict:
    global _wiki_last
    with _wiki_lock:
        wait = WIKI_MIN_INTERVAL - (time.monotonic() - _wiki_last)
        if wait > 0:
            time.sleep(wait)
        _wiki_last = time.monotonic()
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def wiki_title_from_url(url: str | None) -> str | None:
    if not url or "wikipedia.org/wiki/" not in url:
        return None
    return unquote(url.split("/wiki/", 1)[1].split("#")[0]).replace("_", " ") or None


def wikidata_enwiki(qid_url: str) -> str | None:
    """A Wikidata item (its URL) -> its English Wikipedia article title, or None."""
    qid = qid_url.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"Q\d+", qid):
        return None
    def fetch():
        d = _wiki_get("https://www.wikidata.org/w/api.php",
                      {"action": "wbgetentities", "ids": qid, "props": "sitelinks", "sitefilter": "enwiki", "format": "json"})
        return ((d.get("entities") or {}).get(qid, {}).get("sitelinks") or {}).get("enwiki", {}).get("title")
    return mbcache._cached(f"lookup:wikidata-enwiki:{qid}", "lookup", fetch)


def wikipedia_intro(title: str) -> dict | None:
    """The article's lead as plain text (paragraphs separated by blank lines) + a thumbnail and its
    canonical URL, following redirects. None if there's no such page or it's a disambiguation page."""
    def fetch():
        d = _wiki_get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1", "titles": title,
            "prop": "extracts|pageimages|pageprops|info", "exintro": "1", "explaintext": "1", "exsectionformat": "plain",
            "piprop": "thumbnail", "pithumbsize": "400", "ppprop": "disambiguation", "inprop": "url"})
        pages = (d.get("query") or {}).get("pages") or []
        if not pages or pages[0].get("missing") or "disambiguation" in (pages[0].get("pageprops") or {}):
            return None
        pg = pages[0]
        text = "\n\n".join(LEFTOVER_PARENS.sub("", p).strip() for p in (pg.get("extract") or "").split("\n") if p.strip())
        return {"title": pg.get("title"), "url": pg.get("fullurl") or f"https://en.wikipedia.org/wiki/{quote(pg.get('title', '').replace(' ', '_'))}",
                "text": text or None, "thumbnail": (pg.get("thumbnail") or {}).get("source")}
    return mbcache._cached(f"lookup:wikipedia-intro:{title}", "lookup", fetch)


def _clean_lastfm(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"<a [^>]*>Read more on Last\.fm</a>\.?", "", raw)
    text = re.sub(r"User-contributed text is available under the Creative Commons By-SA License; additional terms may apply\.?", "", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n|\n", text)]
    text = "\n\n".join(p for p in paras if p)
    if not text or SHARED_NAME_BIO.search(text):
        return None
    return text


def lastfm_bio(artist_mbid: str | None, name: str) -> dict | None:
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        return None
    def fetch():
        params = {"method": "artist.getInfo", "api_key": key, "format": "json", "autocorrect": "0"}
        params.update({"mbid": artist_mbid} if artist_mbid else {"artist": name})
        r = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, headers=UA, timeout=TIMEOUT)
        d = r.json() if r.ok else {}
        if d.get("error") and artist_mbid:  # Last.fm doesn't know every mbid -- by name, then
            r = requests.get("https://ws.audioscrobbler.com/2.0/", params={**params, "artist": name, "mbid": ""}, headers=UA, timeout=TIMEOUT)
            d = r.json() if r.ok else {}
        a = d.get("artist") or {}
        return {"text": _clean_lastfm((a.get("bio") or {}).get("content")), "url": a.get("url")}
    return mbcache._cached(f"lookup:lastfm-bio:{artist_mbid or name.lower()}", "lookup", fetch)


def fetch(artist_mbid: str | None, name: str, wiki_url: str | None = None) -> dict:
    """Everything for one artist's About (network, cached). `wiki_url` = a Wikipedia page picked by
    hand, used instead of the one MusicBrainz points at. -> {text, source, wiki_url, wiki_title,
    thumbnail_url, tagline, area, begin_area, formed, ended, type, links_json}"""
    facts = mbcache._cached(f"lookup:artist-about:{artist_mbid}", "lookup", lambda: mb.artist_about(artist_mbid)) if artist_mbid else None
    facts = facts or {}
    out = {"tagline": facts.get("tagline"), "type": facts.get("type"), "area": facts.get("area"), "begin_area": facts.get("beginArea"),
           "formed": facts.get("formed"), "ended": facts.get("ended"), "text": None, "source": None, "wiki_url": None,
           "wiki_title": None, "thumbnail_url": None}
    title = wiki_title_from_url(wiki_url) or wiki_title_from_url(facts.get("wikipedia"))
    if not title and facts.get("wikidata"):
        title = wikidata_enwiki(facts["wikidata"])
    links = dict(facts.get("links") or {})
    if title:
        page = wikipedia_intro(title)
        if page:
            out.update({"text": page["text"], "source": "wikipedia" if page["text"] else None, "wiki_url": page["url"],
                        "wiki_title": page["title"], "thumbnail_url": page["thumbnail"]})
            links = {"Wikipedia": page["url"], **links}
    if not out["text"]:
        bio = lastfm_bio(artist_mbid, name)
        if bio and bio.get("text"):
            out.update({"text": bio["text"], "source": "lastfm"})
            if bio.get("url"):
                links.setdefault("Last.fm", bio["url"])
    links.pop("Wikidata", None)  # only ever a way to the Wikipedia page
    out["links_json"] = json.dumps(links) if links else None
    return out


def search(name: str) -> list[dict]:
    """Wikipedia pages a name might mean -- candidates for a person to pick, never applied by itself."""
    d = _wiki_get("https://en.wikipedia.org/w/api.php", {
        "action": "query", "format": "json", "formatversion": "2", "list": "search", "srsearch": f"{name} band OR musician",
        "srlimit": "6", "srprop": "snippet"})
    return [{"title": r["title"], "url": f"https://en.wikipedia.org/wiki/{quote(r['title'].replace(' ', '_'))}",
             "snippet": html.unescape(re.sub(r"<[^>]+>", "", r.get("snippet") or ""))} for r in (d.get("query") or {}).get("search") or []]


# -- storing -------------------------------------------------------------------------------------

def row(conn, artist_id: int) -> dict | None:
    r = conn.execute(f"SELECT {', '.join(FIELDS)} FROM artist_about WHERE artist_id = ?", (artist_id,)).fetchone()
    return dict(zip(FIELDS, r)) if r else None


def store_fetched(conn, artist_id: int, f: dict) -> str:
    """Writes a fetch. A row nobody has edited takes it whole; an edited one keeps its text and
    page, refreshes the facts, and is flagged when the source text changed. -> what happened."""
    cur = row(conn, artist_id)
    facts = {k: f[k] for k in ("tagline", "type", "area", "begin_area", "formed", "ended", "links_json")}
    if cur is None:
        conn.execute(f"""INSERT INTO artist_about (artist_id, text, original_text, source, wiki_url, wiki_title, thumbnail_url,
                         {', '.join(facts)}, fetched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, {', '.join('?' * len(facts))}, datetime('now'), datetime('now'))""",
                     (artist_id, f["text"], f["text"], f["source"], f["wiki_url"], f["wiki_title"], f["thumbnail_url"], *facts.values()))
        return "new" if f["text"] else "facts only"
    if not cur["edited"]:
        conn.execute(f"""UPDATE artist_about SET text = ?, original_text = ?, source = ?, wiki_url = ?, wiki_title = ?, thumbnail_url = ?,
                         {', '.join(f'{k} = ?' for k in facts)}, changed_upstream = 0, fetched_at = datetime('now'), updated_at = datetime('now')
                         WHERE artist_id = ?""",
                     (f["text"], f["text"], f["source"], f["wiki_url"], f["wiki_title"], f["thumbnail_url"], *facts.values(), artist_id))
        return "updated" if f["text"] != cur["text"] else "unchanged"
    # edited: hands off the text. Only the same page's new wording counts as "changed upstream".
    same_page = (f["wiki_url"] or None) == (cur["wiki_url"] or None) or cur["source"] == "manual"
    changed = bool(f["text"]) and same_page and f["text"] != cur["original_text"]
    conn.execute(f"""UPDATE artist_about SET {', '.join(f'{k} = ?' for k in facts if k != 'links_json')},
                     original_text = CASE WHEN ? THEN ? ELSE original_text END, changed_upstream = changed_upstream OR ?,
                     fetched_at = datetime('now') WHERE artist_id = ?""",
                 (*[v for k, v in facts.items() if k != "links_json"], changed, f["text"], changed, artist_id))
    return "kept your edit (Wikipedia changed)" if changed else "kept your edit"


def edit(conn, artist_id: int, changes: dict, reason: str = "About edited") -> int | None:
    """A person's change to one artist's About -- any of: text (None = hide the About), wiki
    (a fetched page, from fetch(..., wiki_url)), reset (back to the fetched text). Journaled.
    -> edit id, or None when nothing changed."""
    name = conn.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not name:
        raise ValueError("artist not found")
    before = row(conn, artist_id)
    after = dict(before or {k: None for k in FIELDS})
    if "wiki" in changes:  # a page picked by hand: its text, link and picture
        w = changes["wiki"]
        after.update({k: w[k] for k in ("text", "wiki_url", "wiki_title", "thumbnail_url")})
        after.update({"original_text": w["text"], "source": "wikipedia", "edited": 1})
        links = json.loads(after.get("links_json") or "{}")
        links = {"Wikipedia": w["wiki_url"], **{k: v for k, v in links.items() if k != "Wikipedia"}}
        after["links_json"] = json.dumps(links)
    if "text" in changes:
        t = changes["text"]
        t = "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", t or "") if p.strip()) or None
        after["text"] = t
        after["edited"] = 0 if (t == after.get("original_text") and "wiki" not in changes and not (before or {}).get("edited")) else 1
        if after.get("source") is None and t:
            after["source"] = "manual"
    if changes.get("reset"):
        after.update({"text": after.get("original_text"), "edited": 0})
    after["changed_upstream"] = 0
    if before and all(before.get(k) == after.get(k) for k in FIELDS if k not in ("updated_at", "fetched_at")):
        return None
    after["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    _write(conn, artist_id, after)
    return conn.execute("INSERT INTO edit_log (entity_type, entity_id, entity_name, changes_json, reason) VALUES ('artist', ?, ?, ?, ?)",
                        (artist_id, name[0], json.dumps({"_about": {"before": before, "after": after}}), reason)).lastrowid


def _write(conn, artist_id: int, values: dict | None) -> None:
    conn.execute("DELETE FROM artist_about WHERE artist_id = ?", (artist_id,))
    if values is not None:
        conn.execute(f"INSERT INTO artist_about (artist_id, {', '.join(FIELDS)}) VALUES (?, {', '.join('?' * len(FIELDS))})",
                     (artist_id, *[values.get(k) if k not in ("edited", "changed_upstream") else int(values.get(k) or 0) for k in FIELDS]))


def undo_edit(conn, artist_id: int, j: dict) -> None:
    _write(conn, artist_id, j["before"])


# -- the sweep -----------------------------------------------------------------------------------

def sweep(limit: int, log, progress, cancelled, refresh: bool = False) -> None:
    """Fetches About for artists you've played, own or seen that don't have one yet (most played
    first). About 1 MusicBrainz request (1/s) + 1-2 Wikimedia requests per artist."""
    conn = db_connect()
    try:
        where = "" if refresh else "AND NOT EXISTS (SELECT 1 FROM artist_about ab WHERE ab.artist_id = ar.id)"
        targets = conn.execute(f"""
            SELECT ar.id, ar.name, ar.mbid FROM artists ar
            WHERE lower(ar.name) NOT IN ('various', 'various artists') {where}
              AND (EXISTS (SELECT 1 FROM scrobbles s WHERE s.artist_id = ar.id)
                   OR EXISTS (SELECT 1 FROM album_artists aa JOIN vinyl_holdings v ON v.album_id = aa.album_id WHERE aa.artist_id = ar.id)
                   OR EXISTS (SELECT 1 FROM setlists sl WHERE sl.artist_id = ar.id))
            ORDER BY (SELECT count(*) FROM album_artists aa JOIN vinyl_holdings v ON v.album_id = aa.album_id WHERE aa.artist_id = ar.id) DESC,
                     (SELECT count(*) FROM scrobbles s WHERE s.artist_id = ar.id) DESC
            LIMIT ?""", (limit,)).fetchall()
        log(f"Fetching About for {len(targets)} artist(s) — most played first (about 1–2 seconds each).")
        tally: dict[str, int] = {}
        for i, (aid, name, mbid) in enumerate(targets):
            if cancelled():
                log("Cancelled.")
                break
            progress(i, len(targets))
            try:
                f = fetch(mbid, name)
                what = store_fetched(conn, aid, f)
                conn.commit()
                tally[f["source"] or "none"] = tally.get(f["source"] or "none", 0) + 1
                log(f"  · {name}: {f['source'] or 'no text'}{' — ' + f['wiki_title'] if f.get('wiki_title') else ''} ({what})")
            except Exception as exc:  # one artist failing shouldn't end the sweep
                conn.rollback()
                log(f"  ! {name}: {exc}")
        progress(len(targets), len(targets))
        log(f"Done — Wikipedia {tally.get('wikipedia', 0)}, Last.fm {tally.get('lastfm', 0)}, nothing {tally.get('none', 0)}. "
            "Edit any of them in the Workbench; Publish for the site to use them.")
    finally:
        conn.close()
