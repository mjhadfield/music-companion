"""An artist's About (about.py): look at it, fetch it, edit or trim it, pick another Wikipedia page.
Every change a person makes is journaled (undo kind "edit"); fetching only touches rows nobody edited."""
import about
from api.core import ApiError, read_conn, route, write_tx


def _payload(c, artist_id: int) -> dict:
    ar = c.execute("SELECT id, name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not ar:
        raise ApiError("artist not found", 404)
    return {"artistId": ar[0], "name": ar[1], "mbid": ar[2], "about": about.row(c, artist_id)}


@route("GET", "/api/about")
def get_about(req):
    with read_conn() as c:
        return _payload(c, req.int("artistId", required=True))


@route("GET", "/api/about/status")
def status(req):
    with read_conn() as c:
        one = lambda q: c.execute(q).fetchone()[0]
        return {"rows": one("SELECT count(*) FROM artist_about"),
                "wikipedia": one("SELECT count(*) FROM artist_about WHERE source = 'wikipedia'"),
                "lastfm": one("SELECT count(*) FROM artist_about WHERE source = 'lastfm'"),
                "noText": one("SELECT count(*) FROM artist_about WHERE text IS NULL"),
                "edited": one("SELECT count(*) FROM artist_about WHERE edited = 1"),
                "changedUpstream": one("SELECT count(*) FROM artist_about WHERE changed_upstream = 1"),
                "toFetch": one("""SELECT count(*) FROM artists ar WHERE lower(ar.name) NOT IN ('various', 'various artists')
                    AND NOT EXISTS (SELECT 1 FROM artist_about ab WHERE ab.artist_id = ar.id)
                    AND (EXISTS (SELECT 1 FROM scrobbles s WHERE s.artist_id = ar.id)
                         OR EXISTS (SELECT 1 FROM album_artists aa JOIN vinyl_holdings v ON v.album_id = aa.album_id WHERE aa.artist_id = ar.id)
                         OR EXISTS (SELECT 1 FROM setlists sl WHERE sl.artist_id = ar.id))""")}


@route("GET", "/api/about/search")
def search(req):
    """Wikipedia pages the artist's name might mean -- to pick from when MusicBrainz knows none (or the wrong one)."""
    with read_conn() as c:
        name = c.execute("SELECT name FROM artists WHERE id = ?", (req.int("artistId", required=True),)).fetchone()
    if not name:
        raise ApiError("artist not found", 404)
    return {"results": about.search(name[0])}


@route("POST", "/api/about/fetch", mutating=True)
def fetch(req):
    """Fetches (again). With wikiUrl: that Wikipedia page, picked by hand -- an edit (journaled, undoable)."""
    artist_id = req.int("artistId", required=True)
    wiki_url = (req.body.get("wikiUrl") or "").strip() or None
    with read_conn() as c:
        ar = c.execute("SELECT name, mbid FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not ar:
        raise ApiError("artist not found", 404)
    if wiki_url and not about.wiki_title_from_url(wiki_url):
        raise ApiError("That isn't a Wikipedia article link (https://en.wikipedia.org/wiki/...).")
    f = about.fetch(ar[1], ar[0], wiki_url)  # network, outside any transaction
    if wiki_url and not f.get("wiki_url"):
        raise ApiError("Wikipedia has no article at that link (or it's a disambiguation page).", 404)
    with write_tx() as c:
        if wiki_url:
            edit_id = about.edit(c, artist_id, {"wiki": f}, "About: Wikipedia page picked")
            what = "picked" if edit_id else "unchanged"
        else:
            edit_id, what = None, about.store_fetched(c, artist_id, f)
        out = _payload(c, artist_id)
    return {**out, "result": what, "editId": edit_id}


@route("POST", "/api/about/edit", mutating=True)
def edit(req):
    """{artistId, text} saves the shown text (empty = no About); {artistId, reset: true} goes back to the fetched text."""
    artist_id = req.int("artistId", required=True)
    changes = {}
    if "text" in req.body:
        changes["text"] = str(req.body.get("text") or "")
    if req.body.get("reset"):
        changes["reset"] = True
    if not changes:
        raise ApiError("nothing to change")
    with write_tx() as c:
        try:
            edit_id = about.edit(c, artist_id, changes, "About: reset to the fetched text" if changes.get("reset") else "About edited")
        except ValueError as exc:
            raise ApiError(str(exc), 404)
        return {**_payload(c, artist_id), "editId": edit_id}
