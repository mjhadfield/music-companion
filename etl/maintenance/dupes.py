"""
Song duplicates for review -- shared by the artist workbench and the library-wide queue.

  edition   every member is the same recording spelled differently ("Still Loving You" /
            "Still Loving You - 2015 - Remaster" / "(2015 - Remaster)"). The only class that can
            arrive pre-ticked, and only when the members share an album (the existing rule).
  variant   a live / remix / edit / demo / acoustic version alongside the plain song. Versions stay
            separate songs by default; shown so a human can merge one deliberately (reversible), or
            say "not the same" so it stops coming back.
  possible  near-identical titles the exact grouping can't join: spacing-blind ("Good Morning" /
            "Goodmorning"), a short suffix ("Total Hate" / "Total Hate '95"), or a near spelling
            ("Seperate"), a dropped article ("A Song for the Dead"). Pairs only, never pre-ticked.
            Only the words that differ are compared (a shared "- Live at the Majestic…" tail can't
            make "Rooster" look like "Brother"), and a suffix only pairs when it's a number or year
            -- "Harvest" / "Harvest Moon" and "Vermilion" / "Vermilion Pt. 2" never do.

A group is also flagged "split" when its live sightings and its plays sit on different rows
(setlist.fm's plain "War Pigs" vs Last.fm's "War Pigs - 2009 Remaster") -- the merges that fix
every live count.
"""
import re
from collections import defaultdict

from rapidfuzz import fuzz, process

from api.songs import _best_target, _groups_for, _in, nonsong_kind, song_profiles
from titles import base_key, sequel_marker, split_title, track_key

ARTICLES = {"a", "an", "the"}
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
SPELLING_MIN = 85          # on the words that differ, once shared leading/trailing words are set aside
_YEAR_SUFFIX = re.compile(r"['’‘`]\d{2}$")   # "Total Hate '95": a year, not part 95
_FOLD_EXTRA = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ł": "l", "ð": "d", "þ": "th"})


def _words(key: str) -> list[str]:
    """Comparison words: Nordic letters folded, "n" = "and", Roman numerals = digits."""
    out = []
    for w in key.translate(_FOLD_EXTRA).split():
        w = "and" if w == "n" else ROMAN.get(w, w)
        out.append(w)
    return out


def _differing(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    """The words left once the shared leading and trailing words are set aside."""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    return a[i:len(a) - j], b[i:len(b) - j]


def _sequel(title: str) -> str | None:
    """titles.sequel_marker, except an apostrophe year ('95) is a year: "Total Hate '95" may be
    "Total Hate" (a possible pair, for a human to judge), where "Vermilion Pt. 2" never is "Vermilion"."""
    return None if _YEAR_SUFFIX.search(split_title(title)[0].strip()) else sequel_marker(title)


def _dismissed(c) -> set[tuple[int, int]]:
    return {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'song'")}


def _judge(a: dict, b: dict) -> tuple[str, float] | None:
    """Why two titles look like one song, or None. Only ever a clear, small difference."""
    if a["sequel"] != b["sequel"]:
        return None                                               # "Vermilion" / "Vermilion Pt. 2"
    if a["compact"] == b["compact"]:
        return "spacing", 100.0                                   # "Gold Dust" / "Golddust"
    da, db = _differing(a["words"], b["words"])
    if not da or not db:
        extra = da or db
        if set(extra) <= ARTICLES:
            return "article", 97.0                                # "A Song for the Dead" / "Song for the Dead"
        if len(extra) <= 2 and all(w.isdigit() and len(w) <= 4 for w in extra) and len(a["key"]) >= 4 and len(b["key"]) >= 4:
            return "suffix", 95.0                                 # "Total Hate" / "Total Hate '95"
        return None                                               # "Harvest" / "Harvest Moon" -- different songs
    sa, sb = " ".join(da), " ".join(db)
    if min(len(sa), len(sb)) < 3:
        return None
    score = fuzz.ratio(sa, sb)
    return ("spelling", round(score, 1)) if score >= SPELLING_MIN else None   # "Comin'" / "Coming", "Parabol" / "Parabola"


def _entry(sid: int, title: str) -> dict | None:
    """What _judge compares for one song title (None for non-songs: intros, medleys...)."""
    k = track_key(title) or base_key(title)
    if not k or nonsong_kind(title):
        return None
    words = _words(k)
    return {"id": sid, "key": k, "words": words, "compact": "".join(words), "sequel": _sequel(title)}


def near_titles(title: str, others: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The songs among `others` (id, title) that a new `title` is a possible duplicate of, with
    why -- the Inbox's "possibly the same as" for a newly imported song."""
    me = _entry(0, title)
    if not me or len(me["compact"]) < 4:
        return []
    out = []
    for sid, t in others:
        e = _entry(sid, t)
        if e and len(e["compact"]) >= 4 and e["key"] != me["key"]:
            verdict = _judge(me, e)
            if verdict:
                out.append((sid, verdict[0]))
    return out


def possible_pairs(c, artist_ids: list[int], exact_groups: dict[int, list[dict]] | None = None) -> dict[int, list[dict]]:
    """artist -> near-identical song pairs the exact grouping misses: {a, b, reason, score}. A song
    already in an exact group is represented by that group's first member, so a near-match to a
    group is shown once, not once per member."""
    if not artist_ids:
        return {}
    dismissed = _dismissed(c)
    rep = {}
    for groups in (exact_groups or {}).values():
        for g in groups:
            for sid in g["songIds"]:
                rep[sid] = min(g["songIds"])
    by_artist = defaultdict(list)
    for sid, title, artist_id in c.execute(f"SELECT id, title, artist_id FROM songs WHERE artist_id IN ({_in(artist_ids)})", artist_ids):
        if rep.get(sid, sid) != sid:
            continue
        e = _entry(sid, title)
        if e:
            by_artist[artist_id].append(e)
    out = {}
    for artist_id, songs in by_artist.items():
        if len(songs) < 2:
            continue
        found: dict[tuple[int, int], tuple[str, float]] = {}
        compacts = [s["compact"] for s in songs]
        for i, s in enumerate(songs):
            if len(s["compact"]) < 4:
                continue
            for _m, _sc, j in process.extract(s["compact"], compacts, scorer=fuzz.ratio, score_cutoff=75, limit=8):
                if j == i or len(songs[j]["compact"]) < 4 or songs[j]["key"] == s["key"]:
                    continue
                pair = (min(s["id"], songs[j]["id"]), max(s["id"], songs[j]["id"]))
                if pair in dismissed or pair in found:
                    continue
                verdict = _judge(s, songs[j])
                if verdict:
                    found[pair] = verdict
        if found:
            out[artist_id] = [{"a": a, "b": b, "reason": r, "score": sc} for (a, b), (r, sc) in found.items()]
    return out


def raw_spellings(c, song_ids: list[int], per: int = 4) -> dict[int, list[str]]:
    """How each song arrived: distinct Last.fm / setlist.fm spellings, most frequent first."""
    out = defaultdict(list)
    if not song_ids:
        return out
    for i in range(0, len(song_ids), 800):
        chunk = song_ids[i:i + 800]
        for sid, raw, _n in c.execute(
                f"""SELECT song_id, raw, sum(n) FROM (
                        SELECT song_id, raw_track_text AS raw, count(*) n FROM scrobbles WHERE song_id IN ({_in(chunk)}) GROUP BY 1, 2
                        UNION ALL SELECT song_id, raw_song_text, count(*) FROM setlist_songs WHERE song_id IN ({_in(chunk)}) GROUP BY 1, 2)
                    GROUP BY 1, 2 ORDER BY 1, 3 DESC""", chunk + chunk):
            if raw and len(out[sid]) < per:
                out[sid].append(raw)
    return out


def _member(p: dict, spellings: dict) -> dict:
    return {"songId": p["songId"], "title": p["title"], "tags": p["tags"], "edition": p["edition"], "plays": p["scrobbleCount"],
            "shows": p["setlistCount"], "albumId": p["albumId"], "albumTitle": p["albumTitle"], "albumYear": p["albumYear"],
            "mbid": p["mbid"], "firstPlayed": (p["firstPlayed"] or "")[:10] or None, "lastPlayed": (p["lastPlayed"] or "")[:10] or None,
            "playedFrom": [a["title"] for a in p["scrobbleAlbums"][:3]], "spellings": spellings.get(p["songId"], [])}


def review_groups(c, artist_ids: list[int] | None = None) -> list[dict]:
    """Every open duplicate group (and possible pair) for these artists (all when None), each
    {key, kind, artistId, members, primary, include, split, plays, reason?}. Most played first."""
    if artist_ids is None:
        artist_ids = [r[0] for r in c.execute("SELECT DISTINCT artist_id FROM songs")]
    exact = _groups_for(c, artist_ids)
    near = possible_pairs(c, artist_ids, exact)
    ids = {i for gs in exact.values() for g in gs for i in g["songIds"]} | {i for ps in near.values() for p in ps for i in (p["a"], p["b"])}
    prof = song_profiles(c, list(ids))
    spellings = raw_spellings(c, list(ids))
    out = []
    for artist_id, groups in exact.items():
        for g in groups:
            members = [prof[i] for i in g["songIds"] if i in prof]
            if len(members) < 2:
                continue
            primary = _best_target(members)
            kind = "edition" if all(not m["tags"] for m in members) else "variant"
            # pre-ticked only when every member is the same recording AND they share an album
            include = [m["songId"] for m in members if kind == "edition" and g["albumId"] and m["songId"] != primary["songId"]]
            split = any(m["setlistCount"] and not m["scrobbleCount"] for m in members) and any(m["scrobbleCount"] for m in members)
            # every member in the key: a song joining a group makes it a new item (an artist marked
            # reviewed comes back to the queue)
            out.append({"key": "g:" + ",".join(map(str, sorted(g["songIds"]))), "kind": kind, "artistId": artist_id, "primary": primary["songId"], "include": include,
                        "split": split, "commonAlbum": g["albumId"], "plays": sum(m["scrobbleCount"] for m in members),
                        "shows": sum(m["setlistCount"] for m in members), "members": [_member(m, spellings) for m in members]})
    for artist_id, pairs in near.items():
        for p in pairs:
            if p["a"] not in prof or p["b"] not in prof:
                continue
            members = [prof[p["a"]], prof[p["b"]]]
            primary = _best_target(members)
            out.append({"key": f"p:{p['a']}:{p['b']}", "kind": "possible", "artistId": artist_id, "primary": primary["songId"], "include": [],
                        "split": False, "commonAlbum": None, "reason": p["reason"], "score": p["score"],
                        "plays": sum(m["scrobbleCount"] for m in members), "shows": sum(m["setlistCount"] for m in members),
                        "members": [_member(m, spellings) for m in members]})
    out.sort(key=lambda g: (-g["plays"], g["key"]))
    return out
