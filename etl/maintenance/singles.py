"""
Singles filed as albums. Last.fm / Spotify treat a single as its own release, so a lead single
("Heretic", "The Chant", "Amazonia") turns into an album beside the studio album it came from --
its plays filed there, sometimes its own copy of the song. This finds them from your own data
(MusicBrainz release types are only extra evidence, when already cached):

  an album S is a single from album L (same artist) when every track of S -- the songs filed
  under it and the songs played from it -- is also on L (same title once edition words are set
  aside, and the same version tags, so a remix single never matches the original), S is small
  (<= MAX_TRACKS), and its title is one of those tracks (the lead single -- without that, a small
  "album" is usually a real album or compilation you've only played a song or two from). L is the
  bigger release, not a compilation or live album.

Folding one (merge.fold_single, via the batch action "foldSingle"): S's own songs merge into
their twins on L, then S merges into L -- plays move, the single's title and editions are
aliased to L so future imports land there. Journaled like any merge; undo puts it all back.
High tier (arrives ticked) only when nothing argues against it: no vinyl copy of the single,
MusicBrainz doesn't call it an Album, no version tags or edition words (remaster, deluxe,
soundtrack...) in the single's title.
"""
import json
import re
from collections import defaultdict

from titles import base_key, split_title, track_key, variant_tags

MAX_TRACKS = 6
EDITION_WORDS = re.compile(r"\b(remaster(ed)?|deluxe|edition|anniversary|soundtrack|motion picture|expanded)\b", re.I)
COMPILATION_RE = re.compile(r"\b(greatest hits|best of|the very best|the collection|collection|anthology|essential|definitive|"
                            r"ultimate|gold|singles|hits|retrospective|rarities)\b", re.I)


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _track(title: str) -> tuple[str, frozenset]:
    """What makes two song rows one track here: the title without edition words, and its version tags."""
    return track_key(title) or base_key(title), frozenset(variant_tags(title))


def _mb_types(c) -> dict[str, tuple[str | None, list]]:
    """release-group mbid -> (primaryType, secondaryTypes), from whatever MusicBrainz data is cached."""
    out = {}
    for key, payload in c.execute("SELECT key, payload_json FROM mb_cache WHERE key LIKE 'browse:release-groups:%' OR key LIKE 'lookup:release-group:%'"):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        for rg in data if isinstance(data, list) else [data] if isinstance(data, dict) else []:
            if rg and rg.get("mbid"):
                out[rg["mbid"]] = (rg.get("primaryType"), rg.get("secondaryTypes") or [])
    return out


def find_singles(c, artist_ids: list[int] | None = None, album_ids: list[int] | None = None) -> list[dict]:
    """Every single-filed-as-album, most played first. Restrict to some artists or albums."""
    where, args = "", []
    if artist_ids is not None:
        if not artist_ids:
            return []
        where, args = f"WHERE al.artist_id IN ({_in(artist_ids)})", list(artist_ids)
    albums = {r[0]: {"albumId": r[0], "artistId": r[1], "title": r[2], "mbid": r[3], "year": r[4]} for r in c.execute(
        f"SELECT al.id, al.artist_id, al.title, al.mbid, al.year FROM albums al {where}", args)}
    if not albums:
        return []
    artists = sorted({a["artistId"] for a in albums.values()})
    credited = defaultdict(set)                                        # album -> artists it credits
    for album_id, artist_id in c.execute(f"SELECT album_id, artist_id FROM album_artists WHERE artist_id IN ({_in(artists)})", artists):
        credited[album_id].add(artist_id)
    filed = defaultdict(list)                                          # album -> [(song id, title)] filed under it
    holds = defaultdict(lambda: defaultdict(set))                      # artist -> track -> albums it's filed on
    for sid, artist_id, album_id, title in c.execute(
            f"SELECT id, artist_id, album_id, title FROM songs WHERE artist_id IN ({_in(artists)}) AND album_id IS NOT NULL", artists):
        filed[album_id].append((sid, title))
        holds[artist_id][_track(title)].add(album_id)
    played = defaultdict(dict)                                         # album -> {song id: plays from it}
    titles = {}
    for album_id, sid, title, home, n in c.execute(
            f"""SELECT sc.album_id, s.id, s.title, s.album_id, count(*) FROM scrobbles sc JOIN songs s ON s.id = sc.song_id
                WHERE s.artist_id IN ({_in(artists)}) AND sc.album_id IS NOT NULL GROUP BY 1, 2""", artists):
        played[album_id][sid] = n
        titles[sid] = (title, home)
    vinyl = dict(c.execute("SELECT album_id, count(*) FROM vinyl_holdings GROUP BY album_id").fetchall())
    mb = _mb_types(c)
    sizes = {a: len(v) for a, v in filed.items()}
    dismissed = {(a, b) for a, b in c.execute("SELECT entity_id_a, entity_id_b FROM duplicate_dismissals WHERE entity_type = 'album'")}

    def unsuitable(album: dict) -> str | None:
        """Why an album can't be the studio album a single came from."""
        tags = split_title(album["title"])[1]
        types = mb.get(album["mbid"] or "", (None, []))
        if "live" in tags or "Live" in types[1]:
            return "live"
        if COMPILATION_RE.search(album["title"] or "") or "Compilation" in types[1]:
            return "compilation"
        return None

    out = []
    for s in albums.values():
        if album_ids is not None and s["albumId"] not in album_ids:
            continue
        artist = s["artistId"]
        own = filed.get(s["albumId"], [])
        tracks = {}                                                    # track -> {title, songId, filedHere, plays}
        for sid, title in own:
            t = tracks.setdefault(_track(title), {"title": title, "songIds": [], "ownIds": [], "plays": 0, "filedHere": True})
            t["songIds"].append(sid)
            t["ownIds"].append(sid)                                    # filed under the single: these merge into the twin
        for sid, n in played.get(s["albumId"], {}).items():
            title, home = titles[sid]
            t = tracks.setdefault(_track(title), {"title": title, "songIds": [], "ownIds": [], "plays": 0, "filedHere": home == s["albumId"]})
            t["plays"] += n
            if sid not in t["songIds"]:
                t["songIds"].append(sid)
        if not tracks or len(tracks) > MAX_TRACKS:
            continue
        key = base_key(s["title"])
        title_track = any(k[0] == key for k in tracks)
        if not title_track:
            continue
        # albums (of this artist, other than S) that hold every one of S's tracks
        homes = None
        for k in tracks:
            h = {a for a in holds[artist].get(k, set()) if a != s["albumId"] and artist in credited.get(a, set()) and a in albums}
            homes = h if homes is None else homes & h
            if not homes:
                break
        if not homes:
            continue
        homes = [a for a in homes if sizes.get(a, 0) > len(tracks) and not unsuitable(albums[a])
                 and (min(a, s["albumId"]), max(a, s["albumId"])) not in dismissed]
        if not homes:
            continue
        target = albums[sorted(homes, key=lambda a: (not albums[a]["mbid"], -sizes.get(a, 0), albums[a]["year"] or 9999, a))[0]]
        target_songs = {}
        for sid, title in filed.get(target["albumId"], []):
            target_songs.setdefault(_track(title), (sid, title))
        rows = []
        for k, t in tracks.items():
            twin = target_songs.get(k)
            rows.append({"title": t["title"], "plays": t["plays"], "songIds": t["songIds"], "ownIds": t["ownIds"], "filedHere": bool(t["ownIds"]),
                         "twin": {"songId": twin[0], "title": twin[1]} if twin else None})
        mb_type = mb.get(s["mbid"] or "", (None, []))
        reasons = []
        if vinyl.get(s["albumId"]):
            reasons.append(f"you own {vinyl[s['albumId']]} copy of it on vinyl -- a real single in your collection")
        if mb_type[0] == "Album":
            reasons.append("MusicBrainz calls it an Album")
        if variant_tags(s["title"]):
            reasons.append(f"its title says {'/'.join(sorted(variant_tags(s['title'])))}")
        if EDITION_WORDS.search(s["title"] or ""):
            reasons.append("its title reads like an edition of an album (remaster / deluxe / soundtrack...)")
        out.append({"key": f"sg:{s['albumId']}", "singleId": s["albumId"], "title": s["title"], "mbid": s["mbid"], "year": s["year"],
                    "artistId": artist, "target": {"albumId": target["albumId"], "title": target["title"], "mbid": target["mbid"],
                                                    "year": target["year"], "songs": sizes.get(target["albumId"], 0)},
                    "tracks": sorted(rows, key=lambda r: -r["plays"]), "plays": sum(r["plays"] for r in rows),
                    "songsToMerge": sum(1 for r in rows if r["filedHere"]), "titleTrack": title_track,
                    "mbType": mb_type[0], "vinyl": vinyl.get(s["albumId"], 0), "tier": "review" if reasons else "high", "reasons": reasons})
    out.sort(key=lambda x: (x["tier"] != "high", -x["plays"], x["singleId"]))
    return out
